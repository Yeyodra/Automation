#!/usr/bin/env python3
"""
ScreenPipe Cloud token farmer — pure HTTP (Clerk + mail.tm).

Flow per account:
  1. POST api.mail.tm/accounts → temp email
  2. POST api.mail.tm/token → mail bearer
  3. GET clerk.screenpipe.com/v1/client → __client cookie
  4. POST clerk sign_ups → sign_up_attempt (sua_xxx)
  5. POST prepare_verification → sends 6-digit OTP
  6. GET api.mail.tm/messages → poll OTP
  7. POST attempt_verification → status=complete, session
  8. POST sessions/{id}/tokens → JWT (60s)
  9. Smoke: POST api.screenpipe.com/v1/chat/completions

Hub: farms/screenpipe — env SCREENPIPE_* (mapped from Automation/.env).
Run:    python -m jobs run screenpipe -- -n 3 -c 1 -y
WARP: hub injects WARP_EVERY_N (1:1 with -c); farm rotates via core.warp after OK.

CLI: -n / --count, -c / --concurrent, -y / --yes
Log: [HH:MM:SS] [<id>] <step>  message  <email@domain>
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_HUB = _ROOT.parent.parent
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── Config helpers ───────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int = 0) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── 9Router DB injection ─────────────────────────────────────────────────────
INJECT_DB = _env_bool("SCREENPIPE_INJECT_DB", True)


def _resolve_9router_db() -> Path | None:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    else:
        base = Path.home()
        return Path(base) / ".9router" / "db" / "data.sqlite"
    return Path(base) / "9router" / "db" / "data.sqlite"


def _inject_to_9router(result: dict, attempt: int) -> bool:
    if not INJECT_DB:
        return False
    db_path = _resolve_9router_db()
    if not db_path or not db_path.is_file():
        _log(attempt, "inject", f"9router DB not found: {db_path}")
        return False

    import sqlite3
    import uuid

    email = result["email"]
    now = _now_iso()
    data_json = json.dumps({
        "accessToken": result["jwt"],
        "refreshToken": result["session_id"],
        "testStatus": "active",
        "expiresIn": 60,
        "providerSpecificData": {
            "email": email,
            "password": result["screenpipe_password"],
            "sessionId": result["session_id"],
        },
    })

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        cur = conn.cursor()

        cur.execute(
            "SELECT id, priority FROM providerConnections WHERE provider = ? AND email = ?",
            ("screenpipe", email),
        )
        row = cur.fetchone()

        if row:
            cur.execute(
                "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?",
                (data_json, now, row[0]),
            )
        else:
            cur.execute(
                "SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider = ?",
                ("screenpipe",),
            )
            max_pri = cur.fetchone()[0] or 0
            cur.execute(
                """INSERT INTO providerConnections(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), "screenpipe", "oauth", email, email, max_pri + 1, 1, data_json, now, now),
            )

        conn.commit()
        conn.close()
        _log(attempt, "inject", f"9router DB OK ({'update' if row else 'insert'})", email)
        return True
    except Exception as e:
        _log(attempt, "inject", f"9router DB error: {type(e).__name__}: {e}")
        return False


# ── Product constants ────────────────────────────────────────────────────────
CLERK_BASE = "https://clerk.screenpipe.com"
CLERK_JS_VERSION = "5.56.0"
CLERK_PK = "pk_live_Y2xlcmsuc2NyZWVucGlwZS5jb20k"
MAIL_TM_BASE = "https://api.mail.tm"
SCREENPIPE_API = "https://api.screenpipe.com/v1"
SCREENPIPE_ORIGIN = "https://screenpipe.com"
SCREENPIPE_UA = "screenpipe-app/2.5.149"

SMOKE_TEST = _env_bool("SCREENPIPE_SMOKE_TEST", True)
SMOKE_MODEL = _env("SCREENPIPE_SMOKE_MODEL") or "claude-sonnet-5"
OTP_TIMEOUT = max(30, _env_int("SCREENPIPE_OTP_TIMEOUT") or 90)
OTP_POLL_INTERVAL = max(2, _env_int("SCREENPIPE_OTP_POLL_INTERVAL") or 5)
PASSWORD_OVERRIDE = _env("SCREENPIPE_PASSWORD")

WARP_EVERY_N = max(
    0,
    _env_int("SCREENPIPE_WARP_EVERY_N") or _env_int("WARP_EVERY_N") or 0,
)
CONCURRENT = max(1, _env_int("SCREENPIPE_CONCURRENT") or _env_int("CONCURRENT") or 1)
WARP_SETTLE_AFTER = max(0.0, float(_env("WARP_SETTLE_AFTER") or "8") or 8.0)

RESULTS_ROOT = _ROOT / "results"
BATCH_ID = ""
BATCH_DIR: Path = RESULTS_ROOT
RESULTS_JSON: Path = RESULTS_ROOT / "accounts.json"
RESULTS_TXT: Path = RESULTS_ROOT / "accounts.txt"
FAILED_JSON: Path = RESULTS_ROOT / "failed.json"

_success_since_warp = 0
_warp_lock = threading.Lock()
_warp_drain_owner: int | None = None
_results_lock = threading.Lock()
_in_flight = 0
_if_lock = threading.Lock()
_can_start = threading.Event()
_can_start.set()


# ── Logging (hub contract) ───────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(attempt: int, step: str, msg: str, email: str = "") -> None:
    tail = f"  <{email}>" if email else ""
    print(f"[{_ts()}] [{attempt}] {step}  {msg}{tail}", flush=True)


# ── Batch init ───────────────────────────────────────────────────────────────
def init_batch(max_accounts: int, concurrent: int) -> str:
    global BATCH_ID, BATCH_DIR, RESULTS_JSON, RESULTS_TXT, FAILED_JSON

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BATCH_ID = f"batch_{stamp}_{secrets.token_hex(3)}"
    BATCH_DIR = RESULTS_ROOT / BATCH_ID
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON = BATCH_DIR / "accounts.json"
    RESULTS_TXT = BATCH_DIR / "accounts.txt"
    FAILED_JSON = BATCH_DIR / "failed.json"
    for p, empty in ((RESULTS_JSON, "[]"), (FAILED_JSON, "[]"), (RESULTS_TXT, "")):
        if not p.exists():
            p.write_text(empty + ("\n" if empty == "[]" else ""), encoding="utf-8")

    meta = {
        "batch_id": BATCH_ID,
        "started_at": _now_iso(),
        "product": "screenpipe.com",
        "max_accounts": max_accounts,
        "concurrent": concurrent,
        "smoke_test": SMOKE_TEST,
        "smoke_model": SMOKE_MODEL,
    }
    (BATCH_DIR / "batch_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[screenpipe] batch={BATCH_ID} dir={BATCH_DIR}", flush=True)
    return BATCH_ID


# ── Result persistence ───────────────────────────────────────────────────────
def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def save_ok(result: dict) -> None:
    with _results_lock:
        rows: list = []
        if RESULTS_JSON.is_file():
            try:
                rows = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        rows.append(result)
        RESULTS_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        email = result.get("email", "")
        _append_line(RESULTS_TXT, f"{email}\t{result.get('session_id','')}\t{result.get('jwt','')[:40]}...")


def save_fail(row: dict) -> None:
    with _results_lock:
        rows: list = []
        if FAILED_JSON.is_file():
            try:
                rows = json.loads(FAILED_JSON.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        rows.append(row)
        FAILED_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


# ── WARP integration ─────────────────────────────────────────────────────────
def _effective_warp_every_n() -> int:
    if WARP_EVERY_N <= 0:
        return 0
    return max(WARP_EVERY_N, CONCURRENT)


def _rotate_warp_sync(attempt: int) -> bool:
    try:
        from core.warp import WarpClient

        w = WarpClient(log=lambda m: print(f"[{attempt}] {m}", flush=True))
        r = w.rotate_ip(force=True)
        print(f"[{attempt}] WARP every_n rotate: {r}", flush=True)
        return bool(getattr(r, "ok", False))
    except Exception as e:
        print(f"[{attempt}] WARP every_n error: {type(e).__name__}: {e}", flush=True)
        return False


def _maybe_warp_after_success(attempt: int) -> None:
    global _success_since_warp, _warp_drain_owner
    every = _effective_warp_every_n()
    if every <= 0:
        return

    should_rotate = False
    with _warp_lock:
        _success_since_warp += 1
        n = _success_since_warp
        if n < every:
            print(
                f"[{attempt}] WARP every_n: success {n}/{every} (wave c={CONCURRENT})",
                flush=True,
            )
            return
        if _warp_drain_owner is not None:
            print(
                f"[{attempt}] WARP every_n: success {n}/{every} "
                f"(drain owned by #{_warp_drain_owner})",
                flush=True,
            )
            return
        _warp_drain_owner = attempt
        _success_since_warp = 0
        should_rotate = True
        print(
            f"[{attempt}] WARP every_n: wave complete {every}/{every} → drain then rotate…",
            flush=True,
        )

    if not should_rotate:
        return

    _can_start.clear()
    try:
        # Drain: wait for in-flight to reach 1 (us)
        deadline = time.time() + 180.0
        while True:
            with _if_lock:
                n_if = _in_flight
            if n_if <= 1:
                break
            if time.time() >= deadline:
                print(f"[{attempt}] WARP every_n: drain timeout (in_flight={n_if})", flush=True)
                break
            time.sleep(0.5)

        print(f"[{attempt}] WARP every_n: drain ok → rotate", flush=True)
        ok = _rotate_warp_sync(attempt)
        if ok and WARP_SETTLE_AFTER > 0:
            print(f"[{attempt}] WARP every_n: settle {WARP_SETTLE_AFTER:.0f}s…", flush=True)
            time.sleep(WARP_SETTLE_AFTER)
    finally:
        _can_start.set()
        with _warp_lock:
            if _warp_drain_owner == attempt:
                _warp_drain_owner = None


# ── HTTP helpers (stdlib) ────────────────────────────────────────────────────
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)


def _http_json(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict | list | str]:
    """Generic HTTP → (status, parsed_json_or_raw)."""
    hdrs = headers or {}
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    do_open = opener.open if opener else urllib.request.urlopen
    try:
        with do_open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            st = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        st = e.code
    if not raw:
        return st, {}
    try:
        return st, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return st, raw[:2000]


def _gen_password() -> str:
    """UUID-based password unlikely to be in HIBP."""
    if PASSWORD_OVERRIDE:
        return PASSWORD_OVERRIDE
    return f"xK9{secrets.token_hex(4)}!qZ{secrets.token_hex(2)}"


# ── mail.tm helpers ──────────────────────────────────────────────────────────
def _mailtm_get_domain() -> str:
    """Fetch first available domain from mail.tm."""
    st, body = _http_json("GET", f"{MAIL_TM_BASE}/domains", headers={"Accept": "application/json"})
    if st == 200 and isinstance(body, dict):
        members = body.get("hydra:member") or body.get("member") or []
        if isinstance(members, list) and members:
            return members[0].get("domain", "web-library.net")
    if st == 200 and isinstance(body, list) and body:
        return body[0].get("domain", "web-library.net")
    return "web-library.net"


def _mailtm_create_account(domain: str) -> tuple[str, str]:
    """Create a temp email. Returns (email, password)."""
    local = f"sp{secrets.token_hex(6)}"
    email = f"{local}@{domain}"
    password = secrets.token_hex(12)
    payload = json.dumps({"address": email, "password": password}).encode()
    st, body = _http_json(
        "POST",
        f"{MAIL_TM_BASE}/accounts",
        body=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if st not in (200, 201):
        raise RuntimeError(f"mail.tm create failed: {st} {body}")
    return email, password


def _mailtm_get_token(email: str, password: str) -> str:
    """Get bearer token for mail.tm inbox."""
    payload = json.dumps({"address": email, "password": password}).encode()
    st, body = _http_json(
        "POST",
        f"{MAIL_TM_BASE}/token",
        body=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if st != 200 or not isinstance(body, dict):
        raise RuntimeError(f"mail.tm token failed: {st} {body}")
    token = body.get("token") or ""
    if not token:
        raise RuntimeError(f"mail.tm token empty: {body}")
    return token


def _mailtm_poll_otp(mail_token: str, timeout: float, interval: float) -> str:
    """Poll mail.tm messages for 6-digit OTP code."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, body = _http_json(
            "GET",
            f"{MAIL_TM_BASE}/messages",
            headers={
                "Authorization": f"Bearer {mail_token}",
                "Accept": "application/json",
            },
        )
        messages = []
        if isinstance(body, dict):
            messages = body.get("hydra:member") or body.get("member") or []
        elif isinstance(body, list):
            messages = body
        for msg in messages:
            # Check subject and text for 6-digit code
            subject = str(msg.get("subject") or "")
            intro = str(msg.get("intro") or "")
            text = str(msg.get("text") or "")
            for field in (subject, intro, text):
                match = re.search(r"\b(\d{6})\b", field)
                if match:
                    return match.group(1)
            # If message has an id, fetch full content
            msg_id = msg.get("id") or msg.get("@id", "").split("/")[-1]
            if msg_id and not text:
                st2, full = _http_json(
                    "GET",
                    f"{MAIL_TM_BASE}/messages/{msg_id}",
                    headers={
                        "Authorization": f"Bearer {mail_token}",
                        "Accept": "application/json",
                    },
                )
                if st2 == 200 and isinstance(full, dict):
                    for key in ("text", "html", "subject", "intro"):
                        val = str(full.get(key) or "")
                        m = re.search(r"\b(\d{6})\b", val)
                        if m:
                            return m.group(1)
        time.sleep(interval)
    raise RuntimeError(f"OTP not received within {timeout}s")


# ── Clerk flow (pure HTTP with cookies) ──────────────────────────────────────
@dataclass
class ClerkSession:
    email: str
    password: str
    sign_up_id: str
    session_id: str
    jwt: str
    created_at: str


def _clerk_opener() -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    """Build opener with cookie jar for Clerk session tracking."""
    jar = http.cookiejar.CookieJar()
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener = urllib.request.build_opener(handler)
    return opener, jar


def _clerk_headers() -> dict[str, str]:
    return {
        "Origin": SCREENPIPE_ORIGIN,
        "Referer": f"{SCREENPIPE_ORIGIN}/",
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    }


def _clerk_url(path: str) -> str:
    return f"{CLERK_BASE}{path}?_clerk_js_version={CLERK_JS_VERSION}"


def clerk_signup(attempt: int, email: str, password: str) -> ClerkSession:
    """Full Clerk signup flow: sign_up → verify email → get JWT."""
    opener, jar = _clerk_opener()
    hdrs = _clerk_headers()

    # Step 3: GET /v1/client → sets __client cookie
    _log(attempt, "clerk", "GET /v1/client (cookie init)", email)
    st, _ = _http_json(
        "GET", _clerk_url("/v1/client"), headers=hdrs, opener=opener
    )
    if st not in (200, 401):
        _log(attempt, "clerk", f"client init status={st} (continuing)", email)

    # Step 4: POST /v1/client/sign_ups
    _log(attempt, "clerk", "POST sign_ups", email)
    form_data = urllib.parse.urlencode({
        "email_address": email,
        "password": password,
    }).encode()
    sign_hdrs = {**hdrs, "Content-Type": "application/x-www-form-urlencoded"}
    st, body = _http_json(
        "POST",
        _clerk_url("/v1/client/sign_ups"),
        body=form_data,
        headers=sign_hdrs,
        opener=opener,
    )
    if st not in (200, 201) or not isinstance(body, dict):
        raise RuntimeError(f"sign_ups failed: {st} {body}")

    # Extract sign_up attempt ID
    response = body.get("response") or body
    client = body.get("client") or body
    sign_ups = (client.get("sign_up") if isinstance(client.get("sign_up"), dict) else None) or response
    if not sign_ups:
        sign_ups = response
    sua_id = sign_ups.get("id") or ""
    if not sua_id:
        # Try nested
        if isinstance(response, dict):
            sua_id = response.get("id") or ""
    if not sua_id:
        raise RuntimeError(f"no sign_up id in response: {body}")
    _log(attempt, "clerk", f"sign_up_id={sua_id}", email)

    # Step 5: POST prepare_verification
    _log(attempt, "clerk", "POST prepare_verification (email_code)", email)
    verify_form = urllib.parse.urlencode({"strategy": "email_code"}).encode()
    st, body = _http_json(
        "POST",
        _clerk_url(f"/v1/client/sign_ups/{sua_id}/prepare_verification"),
        body=verify_form,
        headers=sign_hdrs,
        opener=opener,
    )
    if st not in (200, 201):
        raise RuntimeError(f"prepare_verification failed: {st} {body}")
    _log(attempt, "clerk", "OTP email sent", email)

    return ClerkSession(
        email=email,
        password=password,
        sign_up_id=sua_id,
        session_id="",
        jwt="",
        created_at=_now_iso(),
    )


def clerk_verify_and_token(
    attempt: int, session: ClerkSession, otp: str, opener: urllib.request.OpenerDirector
) -> ClerkSession:
    """Step 7-8: verify OTP → get session → get JWT."""
    hdrs = {**_clerk_headers(), "Content-Type": "application/x-www-form-urlencoded"}

    # Step 7: POST attempt_verification
    _log(attempt, "clerk", f"POST attempt_verification code={otp}", session.email)
    form = urllib.parse.urlencode({"strategy": "email_code", "code": otp}).encode()
    st, body = _http_json(
        "POST",
        _clerk_url(f"/v1/client/sign_ups/{session.sign_up_id}/attempt_verification"),
        body=form,
        headers=hdrs,
        opener=opener,
    )
    if st not in (200, 201) or not isinstance(body, dict):
        raise RuntimeError(f"attempt_verification failed: {st} {body}")

    # Extract session_id from response
    response = body.get("response") or body
    client = body.get("client") or body
    # session_id could be in created_session_id or in client.sessions
    sess_id = ""
    if isinstance(response, dict):
        sess_id = response.get("created_session_id") or ""
    if not sess_id and isinstance(client, dict):
        sessions = client.get("sessions") or []
        if isinstance(sessions, list) and sessions:
            sess_id = sessions[0].get("id") or ""
    if not sess_id:
        # Look deeper
        sign_up = client.get("sign_up") if isinstance(client, dict) else None
        if isinstance(sign_up, dict):
            sess_id = sign_up.get("created_session_id") or ""
    if not sess_id:
        raise RuntimeError(f"no session_id after verification: {body}")
    _log(attempt, "clerk", f"session_id={sess_id}", session.email)

    # Step 8: POST sessions/{id}/tokens → JWT
    _log(attempt, "clerk", "POST tokens", session.email)
    st, body = _http_json(
        "POST",
        _clerk_url(f"/v1/client/sessions/{sess_id}/tokens"),
        body=b"",
        headers=hdrs,
        opener=opener,
    )
    if st != 200 or not isinstance(body, dict):
        raise RuntimeError(f"tokens failed: {st} {body}")
    jwt = body.get("jwt") or ""
    if not jwt:
        raise RuntimeError(f"empty JWT: {body}")
    _log(attempt, "clerk", f"JWT ok (len={len(jwt)})", session.email)

    return ClerkSession(
        email=session.email,
        password=session.password,
        sign_up_id=session.sign_up_id,
        session_id=sess_id,
        jwt=jwt,
        created_at=session.created_at,
    )


# ── Smoke test ───────────────────────────────────────────────────────────────
def smoke_test(attempt: int, jwt: str, email: str) -> dict[str, Any]:
    """POST /v1/chat/completions with JWT."""
    _log(attempt, "smoke", f"POST /v1/chat/completions model={SMOKE_MODEL}", email)
    payload = json.dumps({
        "model": SMOKE_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    st, body = _http_json(
        "POST",
        f"{SCREENPIPE_API}/chat/completions",
        body=payload,
        headers={
            "Authorization": f"Bearer {jwt}",
            "User-Agent": SCREENPIPE_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=90.0,
    )
    if st == 200 and isinstance(body, dict):
        content = ""
        choices = body.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content", "")
        _log(attempt, "smoke", f"OK reply={content[:60]!r}", email)
        return {"ok": True, "status": st, "model": SMOKE_MODEL, "content": content[:120]}
    _log(attempt, "smoke", f"FAIL status={st} body={str(body)[:200]}", email)
    return {"ok": False, "status": st, "error": str(body)[:300]}


# ── Main farm flow (one account) ─────────────────────────────────────────────
def farm_one(attempt: int) -> bool:
    global _in_flight
    _can_start.wait()
    with _if_lock:
        _in_flight += 1

    try:
        _log(attempt, "start", "Creating temp email")

        # 1. Get mail.tm domain + create account
        domain = _mailtm_get_domain()
        _log(attempt, "mail", f"domain={domain}")
        mail_email, mail_pass = _mailtm_create_account(domain)
        _log(attempt, "mail", f"created", mail_email)

        # 2. Get mail.tm bearer
        mail_token = _mailtm_get_token(mail_email, mail_pass)
        _log(attempt, "mail", "bearer ok", mail_email)

        # 3. Generate screenpipe password
        sp_password = _gen_password()

        # 4-5. Clerk signup + prepare verification
        # We need the opener to persist cookies across calls
        opener, jar = _clerk_opener()
        hdrs = _clerk_headers()

        # Step 3: cookie init
        _log(attempt, "clerk", "GET /v1/client (cookie init)", mail_email)
        st, _ = _http_json("GET", _clerk_url("/v1/client"), headers=hdrs, opener=opener)

        # Step 4: sign_ups
        _log(attempt, "clerk", "POST sign_ups", mail_email)
        form_data = urllib.parse.urlencode({
            "email_address": mail_email,
            "password": sp_password,
        }).encode()
        sign_hdrs = {**hdrs, "Content-Type": "application/x-www-form-urlencoded"}
        st, body = _http_json(
            "POST",
            _clerk_url("/v1/client/sign_ups"),
            body=form_data,
            headers=sign_hdrs,
            opener=opener,
        )
        if st not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"sign_ups failed: {st} {body}")

        # Extract sua_id
        response = body.get("response") or body
        client = body.get("client") or body
        sign_up = (client.get("sign_up") if isinstance(client, dict) and isinstance(client.get("sign_up"), dict) else None) or response
        sua_id = ""
        if isinstance(sign_up, dict):
            sua_id = sign_up.get("id") or ""
        if not sua_id and isinstance(response, dict):
            sua_id = response.get("id") or ""
        if not sua_id:
            raise RuntimeError(f"no sign_up id: {body}")
        _log(attempt, "clerk", f"sua_id={sua_id}", mail_email)

        # Step 5: prepare_verification
        _log(attempt, "clerk", "POST prepare_verification", mail_email)
        verify_form = urllib.parse.urlencode({"strategy": "email_code"}).encode()
        st, body = _http_json(
            "POST",
            _clerk_url(f"/v1/client/sign_ups/{sua_id}/prepare_verification"),
            body=verify_form,
            headers=sign_hdrs,
            opener=opener,
        )
        if st not in (200, 201):
            raise RuntimeError(f"prepare_verification failed: {st} {body}")
        _log(attempt, "clerk", "OTP sent", mail_email)

        # 6. Poll for OTP
        _log(attempt, "otp", f"polling (timeout={OTP_TIMEOUT}s)", mail_email)
        otp = _mailtm_poll_otp(mail_token, OTP_TIMEOUT, OTP_POLL_INTERVAL)
        _log(attempt, "otp", f"code={otp}", mail_email)

        # 7. Attempt verification
        _log(attempt, "clerk", f"POST attempt_verification code={otp}", mail_email)
        form = urllib.parse.urlencode({"strategy": "email_code", "code": otp}).encode()
        st, body = _http_json(
            "POST",
            _clerk_url(f"/v1/client/sign_ups/{sua_id}/attempt_verification"),
            body=form,
            headers=sign_hdrs,
            opener=opener,
        )
        if st not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"attempt_verification failed: {st} {body}")

        # Extract session_id
        response = body.get("response") or body
        client = body.get("client") or body
        sess_id = ""
        if isinstance(response, dict):
            sess_id = response.get("created_session_id") or ""
        if not sess_id and isinstance(client, dict):
            sessions = client.get("sessions") or []
            if isinstance(sessions, list) and sessions:
                sess_id = sessions[0].get("id") or ""
            sign_up2 = client.get("sign_up") if isinstance(client.get("sign_up"), dict) else None
            if not sess_id and isinstance(sign_up2, dict):
                sess_id = sign_up2.get("created_session_id") or ""
        if not sess_id:
            raise RuntimeError(f"no session_id: {body}")
        _log(attempt, "clerk", f"session={sess_id}", mail_email)

        # 8. Get JWT
        _log(attempt, "clerk", "POST tokens", mail_email)
        st, body = _http_json(
            "POST",
            _clerk_url(f"/v1/client/sessions/{sess_id}/tokens"),
            body=b"",
            headers=sign_hdrs,
            opener=opener,
        )
        if st != 200 or not isinstance(body, dict):
            raise RuntimeError(f"tokens failed: {st} {body}")
        jwt = body.get("jwt") or ""
        if not jwt:
            raise RuntimeError(f"empty JWT: {body}")
        _log(attempt, "clerk", f"JWT ok len={len(jwt)}", mail_email)

        # 9. Smoke test
        smoke_result: dict[str, Any] = {"skipped": True}
        if SMOKE_TEST:
            smoke_result = smoke_test(attempt, jwt, mail_email)

        # Save OK
        result = {
            "email": mail_email,
            "mail_password": mail_pass,
            "screenpipe_password": sp_password,
            "session_id": sess_id,
            "sign_up_id": sua_id,
            "jwt": jwt,
            "smoke": smoke_result,
            "created_at": _now_iso(),
            "batch_id": BATCH_ID,
        }
        save_ok(result)
        _inject_to_9router(result, attempt)
        _log(attempt, "OK", f"Account created + JWT", mail_email)

        # WARP hook
        _maybe_warp_after_success(attempt)
        return True

    except Exception as e:
        email_for_log = ""
        try:
            email_for_log = mail_email  # noqa: F841
        except NameError:
            pass
        _log(attempt, "FAIL", f"{type(e).__name__}: {e}", email_for_log)
        save_fail({
            "attempt": attempt,
            "error": f"{type(e).__name__}: {e}",
            "at": _now_iso(),
        })
        return False
    finally:
        with _if_lock:
            _in_flight -= 1


# ── Threading runner ─────────────────────────────────────────────────────────
def _worker(attempt: int, results: list, lock: threading.Lock) -> None:
    ok = farm_one(attempt)
    with lock:
        results.append(ok)


def run_batch(count: int, concurrent: int) -> tuple[int, int]:
    """Run `count` accounts with `concurrent` threads. Returns (ok, fail)."""
    results: list[bool] = []
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    for i in range(count):
        attempt = i + 1
        # Throttle: max `concurrent` active threads
        while True:
            alive = [t for t in threads if t.is_alive()]
            if len(alive) < concurrent:
                break
            time.sleep(0.3)
        t = threading.Thread(target=_worker, args=(attempt, results, lock), daemon=True)
        threads.append(t)
        t.start()
        # Small stagger
        if i < count - 1:
            time.sleep(0.5)

    for t in threads:
        t.join()

    ok = sum(1 for r in results if r)
    fail = sum(1 for r in results if not r)
    return ok, fail


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ScreenPipe Cloud token farmer (pure HTTP)"
    )
    parser.add_argument("-n", "--count", type=int, default=1, help="Number of accounts")
    parser.add_argument("-c", "--concurrent", type=int, default=1, help="Concurrency")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    count = max(1, args.count)
    concurrent = max(1, args.concurrent)

    if not args.yes:
        print(f"[screenpipe] Will create {count} accounts (c={concurrent})")
        print("[screenpipe] Press Enter to continue or Ctrl+C to abort...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

    init_batch(count, concurrent)
    print(f"[screenpipe] Starting {count} accounts, concurrency={concurrent}", flush=True)

    ok, fail = run_batch(count, concurrent)
    print(f"\n[screenpipe] Done: {ok} OK, {fail} FAIL (batch={BATCH_ID})", flush=True)
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
