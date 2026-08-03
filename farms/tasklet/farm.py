"""
Tasklet farm — pure HTTP magic link signup via exzork mailer.

Flow per account:
  1. Create random mailbox (exzork API)
  2. Request magic link (Tasklet API)
  3. Poll exzork for email → extract token
  4. Verify token → get PIN
  5. SignIn with PIN + secret → sessionToken
  6. Create organization + claim daily bonus
  7. Save result

Rate limit: per-IP ~20 signups/window. WARP shared IPs are pre-exhausted.
Fix: curl_cffi (Chrome TLS), proxy pool rotation, 429 detection + auto-rotate.

Config: TASKLET_* env keys (hub .env maps shared → TASKLET_*).
Run:    python -m jobs run tasklet -- -n 5 -c 3 -y
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ── HTTP clients ─────────────────────────────────────────────────────────────
try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. pip install httpx", flush=True)
    sys.exit(1)

try:
    from curl_cffi.requests import AsyncSession as CffiSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# ── Config ───────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


CONCURRENT = int(_env("TASKLET_CONCURRENT", "1") or "1")
ACCOUNT_TIMEOUT_S = int(_env("TASKLET_ACCOUNT_TIMEOUT", "180") or "180")

# Exzork mailer
EXZORK_API = _env("EXZORK_API", "https://mailer.exzork.me")
EXZORK_KEY = _env("EXZORK_API_KEY", "")
EXZORK_DOMAIN = _env("TASKLET_EXZORK_DOMAIN") or _env("EXZORK_DOMAIN", "")

# Tasklet
TASKLET_API = "https://api.tasklet.ai"

# WARP
WARP_EVERY_N = max(0, int(_env("TASKLET_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0"))
WARP_SETTLE_S = max(3.0, float(_env("WARP_SETTLE_AFTER") or "8"))

# Proxy pool (residential/mobile — WARP is useless for tasklet)
PROXY_FILE = _env("TASKLET_PROXY_FILE") or _env("PROXY_FILE", "")
PROXY_POOL_ENV = _env("TASKLET_PROXY_POOL") or _env("PROXY_POOL", "")
PROXY_ROTATE_EVERY = max(1, int(_env("TASKLET_PROXY_ROTATE_EVERY", "15") or "15"))

RELAY_URLS_RAW = _env("TASKLET_RELAY_URLS", "")
RELAY_ROTATE_EVERY = max(1, int(_env("TASKLET_RELAY_ROTATE_EVERY", "18") or "18"))

# Timing jitter (seconds)
JITTER_INTER_STEP_MIN = float(_env("TASKLET_JITTER_STEP_MIN", "1.5") or "1.5")
JITTER_INTER_STEP_MAX = float(_env("TASKLET_JITTER_STEP_MAX", "4.0") or "4.0")
JITTER_INTER_SIGNUP_MIN = float(_env("TASKLET_JITTER_SIGNUP_MIN", "3.0") or "3.0")
JITTER_INTER_SIGNUP_MAX = float(_env("TASKLET_JITTER_SIGNUP_MAX", "8.0") or "8.0")

# Results
RESULTS_ROOT = Path(_env("TASKLET_RESULTS_DIR", str(_ROOT / "results")))
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

# Browser impersonate targets for TLS diversity
_IMPERSONATE_TARGETS = [
    "chrome131", "chrome136", "chrome142", "chrome145", "chrome146",
]


# ── Relay Pool (serverless signIn proxies on diverse ASNs) ───────────────────
_relay_list: list[str] = []
_relay_idx = 0
_relay_ok_count = 0


def _init_relays() -> None:
    global _relay_list
    if RELAY_URLS_RAW:
        _relay_list = [u.strip().rstrip("/") for u in RELAY_URLS_RAW.split(",") if u.strip()]
    if _relay_list:
        print(f"[tasklet] loaded {len(_relay_list)} relay(s) (rotate every {RELAY_ROTATE_EVERY})", flush=True)


def _get_signin_url() -> str:
    """Return the URL to POST signIn to. Uses relay if available, else direct."""
    if _relay_list:
        return _relay_list[_relay_idx % len(_relay_list)]
    return f"{TASKLET_API}/api/signIn"


def _rotate_relay(reason: str = "") -> None:
    global _relay_idx, _relay_ok_count
    if not _relay_list:
        return
    _relay_idx += 1
    _relay_ok_count = 0
    idx = _relay_idx % len(_relay_list)
    print(f"[relay] rotated to #{idx} ({_relay_list[idx][:50]}) {reason}", flush=True)


# ── Proxy Pool ───────────────────────────────────────────────────────────────
_proxy_list: list[str] = []
_proxy_idx = 0
_proxy_ok_count: dict[int, int] = {}
_proxy_lock: asyncio.Lock | None = None


def _load_proxy_list() -> list[str]:
    """Load proxy URLs from file and/or env. Format: one URL per line (socks5://..., http://...)."""
    proxies: list[str] = []
    # From file
    fp = PROXY_FILE
    if not fp:
        default = _ROOT / "proxies.txt"
        if default.is_file():
            fp = str(default)
    if fp and Path(fp).is_file():
        for line in Path(fp).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
    # From env (comma-separated)
    if PROXY_POOL_ENV:
        for part in PROXY_POOL_ENV.split(","):
            part = part.strip()
            if part:
                proxies.append(part)
    return proxies


def _init_proxies() -> None:
    global _proxy_list
    _proxy_list = _load_proxy_list()
    if _proxy_list:
        random.shuffle(_proxy_list)
        print(f"[tasklet] loaded {len(_proxy_list)} proxies (rotate every {PROXY_ROTATE_EVERY})", flush=True)


def _get_proxy() -> str | None:
    """Get current proxy URL. Returns None if no proxies configured."""
    if not _proxy_list:
        return None
    return _proxy_list[_proxy_idx % len(_proxy_list)]


def _rotate_proxy(reason: str = "") -> str | None:
    """Advance to next proxy in pool."""
    global _proxy_idx
    if not _proxy_list:
        return None
    _proxy_idx += 1
    p = _proxy_list[_proxy_idx % len(_proxy_list)]
    print(f"[proxy] rotated to #{_proxy_idx % len(_proxy_list)} {reason}", flush=True)
    return p


# ── WARP ─────────────────────────────────────────────────────────────────────
_warp_ok_counter = 0


def _effective_warp_every_n() -> int:
    if WARP_EVERY_N <= 0:
        return 0
    return max(1, CONCURRENT)


async def _maybe_warp_after_success() -> None:
    global _warp_ok_counter
    every = _effective_warp_every_n()
    if every <= 0:
        return
    _warp_ok_counter += 1
    if _warp_ok_counter >= every:
        _warp_ok_counter = 0
        try:
            from core.warp import WarpClient
            w = WarpClient(log=print)
            print("[WARP] rotating IP...", flush=True)
            w.rotate_ip(force=True)
            await asyncio.sleep(WARP_SETTLE_S)
            print("[WARP] settled", flush=True)
        except Exception as e:
            print(f"[WARP] rotate failed: {e}", flush=True)


# ── curl_cffi session factory ────────────────────────────────────────────────
_TASKLET_HEADERS = {
    "Origin": "https://tasklet.ai",
    "Referer": "https://tasklet.ai/login",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _make_tasklet_session(proxy: str | None = None) -> CffiSession | None:
    """Create fresh curl_cffi session with Chrome impersonation. Returns None if curl_cffi unavailable."""
    if not HAS_CURL_CFFI:
        return None
    target = random.choice(_IMPERSONATE_TARGETS)
    kwargs: dict = {
        "impersonate": target,
        "timeout": 30,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return CffiSession(**kwargs)


# ── Exzork Mailer ────────────────────────────────────────────────────────────
def _exzork_headers() -> dict[str, str]:
    return {"X-API-Key": EXZORK_KEY, "Content-Type": "application/json"}


async def create_mailbox(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{EXZORK_API}/api/v1/mailboxes",
        headers=_exzork_headers(),
        json={"random": True, "domain": EXZORK_DOMAIN},
    )
    resp.raise_for_status()
    data = resp.json()
    if "mailbox" in data:
        return data["mailbox"]["address"]
    if "mailboxes" in data and data["mailboxes"]:
        return data["mailboxes"][0]["address"]
    raise ValueError(f"Unexpected mailbox response: {data}")


async def poll_magic_link_email(client: httpx.AsyncClient, address: str, timeout: int = 75) -> str:
    """Poll exzork for tasklet magic link email. Returns token from URL."""
    start = time.time()
    while time.time() - start < timeout:
        resp = await client.get(
            f"{EXZORK_API}/api/v1/mailboxes/{address}/messages?limit=5&offset=0",
            headers=_exzork_headers(),
        )
        if resp.status_code == 200:
            data = resp.json()
            messages = data.get("messages", []) if isinstance(data, dict) else data
            for msg in messages:
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                full = await client.get(
                    f"{EXZORK_API}/api/v1/messages/{msg_id}",
                    headers=_exzork_headers(),
                )
                if full.status_code == 200:
                    body = json.dumps(full.json())
                    match = re.search(
                        r"https://tasklet\.ai[^\s\"'<>]*[?&]token=([A-Za-z0-9_\-]+)", body
                    )
                    if match:
                        return match.group(1)
        await asyncio.sleep(3)
    raise TimeoutError(f"No magic link email for {address} within {timeout}s")


# ── Tasklet API (curl_cffi with fallback to httpx) ───────────────────────────

async def _tasklet_post(url: str, payload: dict, proxy: str | None = None,
                        extra_headers: dict | None = None) -> tuple[int, dict | None]:
    """POST to Tasklet API. Uses curl_cffi if available, else httpx.
    Returns (status_code, json_body_or_None)."""
    headers = dict(_TASKLET_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    if HAS_CURL_CFFI:
        async with _make_tasklet_session(proxy) as s:
            resp = await s.post(url, json=payload, headers=headers)
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = None
            return status, body
    else:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = None
            return status, body


async def tasklet_request_magic_link(email: str, secret: str, proxy: str | None = None) -> bool:
    status, _ = await _tasklet_post(
        f"{TASKLET_API}/api/auth/magic-link/request",
        {"email": email, "magicLinkSecret": secret},
        proxy=proxy,
    )
    return status == 200


async def tasklet_verify_magic_link(token: str, proxy: str | None = None) -> str | None:
    status, body = await _tasklet_post(
        f"{TASKLET_API}/api/auth/magic-link/verify",
        {"token": token},
        proxy=proxy,
    )
    if status != 200 or not body:
        return None
    return body.get("pin")


async def tasklet_sign_in(secret: str, pin: str, proxy: str | None = None) -> tuple[str | None, dict | None]:
    """Returns ('429', None) on rate limit, (None, None) on other failure, (None, data) on success."""
    signin_url = _get_signin_url()
    status, body = await _tasklet_post(
        signin_url,
        {
            "type": "magic_link",
            "magicLinkSecret": secret,
            "pin": pin,
            "attributionHistory": [],
            "allowDuplicate": False,
        },
        proxy=proxy,
    )
    if status == 429:
        return "429", None
    if status != 200 or not body:
        return None, None
    if body.get("type") != "success":
        return None, None
    return None, body


async def tasklet_create_org(session_token: str, name: str, proxy: str | None = None) -> dict | None:
    status, body = await _tasklet_post(
        f"{TASKLET_API}/api/organization/create",
        {"name": f"{name}'s organization"},
        proxy=proxy,
        extra_headers={"Authorization": f"Bearer {session_token}"},
    )
    if status != 200 or not body:
        return None
    return body


async def tasklet_claim_daily_bonus(session_token: str, org_id: str, proxy: str | None = None) -> bool:
    status, body = await _tasklet_post(
        f"{TASKLET_API}/api/billing/claimDailyBonus",
        {"organizationId": org_id},
        proxy=proxy,
        extra_headers={"Authorization": f"Bearer {session_token}"},
    )
    return status == 200 and bool(body) and body.get("claimed", False)


async def tasklet_get_credits(session_token: str, org_id: str, proxy: str | None = None) -> int:
    status, body = await _tasklet_post(
        f"{TASKLET_API}/api/billing/creditGrants",
        {"organizationId": org_id},
        proxy=proxy,
        extra_headers={"Authorization": f"Bearer {session_token}"},
    )
    if status != 200 or not body:
        return 0
    return body.get("totalAvailable", 0)


# ── Jitter ───────────────────────────────────────────────────────────────────
async def _jitter_step() -> None:
    """Random delay between steps within a signup flow."""
    await asyncio.sleep(random.uniform(JITTER_INTER_STEP_MIN, JITTER_INTER_STEP_MAX))


# ── Core Farm Logic ──────────────────────────────────────────────────────────
_ip_ok_counter = 0  # signups on current IP/proxy


async def farm_one_account(idx: int) -> dict | str | None:
    """Farm one account. Returns result dict, '429' string, or None."""
    global _ip_ok_counter
    ts = lambda: datetime.now().strftime("%H:%M:%S")
    proxy = _get_proxy()

    # Step 1: Create mailbox (exzork — no fingerprint concern, use httpx)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            email = await create_mailbox(client)
        except Exception as e:
            print(f"[{ts()}] [{idx}] FAIL mailbox: {e}", flush=True)
            return None
    print(f"[{ts()}] [{idx}] start  {email}", flush=True)

    # Step 2: Request magic link
    await _jitter_step()
    secret = str(uuid.uuid4())
    ok = await tasklet_request_magic_link(email, secret, proxy=proxy)
    if not ok:
        print(f"[{ts()}] [{idx}] FAIL magic link request  {email}", flush=True)
        return None

    # Step 3: Poll for email (exzork — httpx)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            token = await poll_magic_link_email(client, email, timeout=75)
        except TimeoutError:
            print(f"[{ts()}] [{idx}] FAIL email timeout  {email}", flush=True)
            return None
    print(f"[{ts()}] [{idx}] got_token  {email}", flush=True)

    # Step 4: Verify
    await _jitter_step()
    pin = await tasklet_verify_magic_link(token, proxy=proxy)
    if not pin:
        print(f"[{ts()}] [{idx}] FAIL verify  {email}", flush=True)
        return None

    # Step 5: SignIn
    await _jitter_step()
    err, sign_in = await tasklet_sign_in(secret, pin, proxy=proxy)
    if err == "429":
        print(f"[{ts()}] [{idx}] 429 RATE LIMITED  {email}", flush=True)
        return "429"
    if not sign_in:
        print(f"[{ts()}] [{idx}] FAIL signIn  {email}", flush=True)
        return None

    session_token = sign_in["sessionToken"]
    user_id = sign_in["userId"]
    print(f"[{ts()}] [{idx}] signed_in  user={user_id}", flush=True)

    # Step 6: Profile + org + bonus (post-signup, less timing-sensitive)
    status, profile = await _tasklet_post(
        f"{TASKLET_API}/api/profile", {},
        proxy=proxy,
        extra_headers={"Authorization": f"Bearer {session_token}"},
    )
    name = "User"
    orgs = []
    if status == 200 and profile:
        name = profile.get("name", "User")
        orgs = profile.get("organizations", [])

    if orgs:
        org_id = orgs[0]["organizationId"]
        ws_id = orgs[0].get("workspaces", [{}])[0].get("workspaceId", "")
    else:
        org = await tasklet_create_org(session_token, name, proxy=proxy)
        if not org:
            print(f"[{ts()}] [{idx}] FAIL org create  {email}", flush=True)
            # Still return partial — we have sessionToken
            org_id = ""
            ws_id = ""
        else:
            org_id = org.get("organizationId", "")
            ws_id = org.get("workspaceId", "")
    if org_id:
        print(f"[{ts()}] [{idx}] org={org_id}", flush=True)

    bonus = False
    total_credits = 0
    if org_id:
        bonus = await tasklet_claim_daily_bonus(session_token, org_id, proxy=proxy)
        total_credits = await tasklet_get_credits(session_token, org_id, proxy=proxy)

    result = {
        "email": email,
        "userId": user_id,
        "sessionToken": session_token,
        "organizationId": org_id,
        "workspaceId": ws_id,
        "totalCredits": total_credits,
        "dailyBonusClaimed": bonus,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    _ip_ok_counter += 1
    print(f"[{ts()}] [{idx}] OK  {email}  credits={total_credits}", flush=True)
    return result


# ── Save Results ─────────────────────────────────────────────────────────────
def save_result(batch_dir: Path, result: dict) -> None:
    accounts_file = batch_dir / "accounts.json"
    data: list[dict] = []
    if accounts_file.is_file():
        try:
            data = json.loads(accounts_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.append(result)
    accounts_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── 9Router DB Inject ────────────────────────────────────────────────────────
def _resolve_9router_db() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "9router" / "db" / "data.sqlite"
    return Path.home() / ".9router" / "db" / "data.sqlite"


def inject_to_9router(result: dict) -> bool:
    try:
        import sqlite3
    except ImportError:
        return False

    db_path = _resolve_9router_db()
    if not db_path or not db_path.is_file():
        return False

    try:
        conn_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        name = result["email"].split("@")[0]
        data_blob = json.dumps({
            "apiKey": result["sessionToken"],
            "testStatus": "active",
            "providerSpecificData": {
                "workspaceId": result.get("workspaceId", ""),
                "organizationId": result.get("organizationId", ""),
                "userId": result.get("userId", ""),
                "totalCredits": result.get("totalCredits", 0),
            },
        })

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider = ?",
            ("tasklet",),
        ).fetchone()
        priority = (row[0] if row else 0) + 1

        db.execute(
            """INSERT INTO providerConnections(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 data=excluded.data, updatedAt=excluded.updatedAt""",
            (conn_id, "tasklet", "apikey", name, result["email"], priority, 1, data_blob, now, now),
        )
        db.commit()
        db.close()
        return True
    except Exception as e:
        print(f"[9router] inject failed: {e}", flush=True)
        return False


# ── Batch Runner ─────────────────────────────────────────────────────────────
async def run_farm(count: int, concurrent: int) -> None:
    global _ip_ok_counter

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [tasklet] farming {count} accounts (exzork domain={EXZORK_DOMAIN})", flush=True)
    if HAS_CURL_CFFI:
        print(f"[{ts}] [tasklet] curl_cffi OK (Chrome TLS impersonation)", flush=True)
    else:
        print(f"[{ts}] [tasklet] WARNING: curl_cffi not installed, using httpx (bot TLS fingerprint)", flush=True)

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = RESULTS_ROOT / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    fail_count = 0
    rate_limited = False
    sem = asyncio.Semaphore(concurrent)

    async def worker(idx: int) -> None:
        nonlocal ok_count, fail_count, rate_limited

        if rate_limited:
            return

        async with sem:
            if rate_limited:
                return

            try:
                result = await asyncio.wait_for(
                    farm_one_account(idx),
                    timeout=ACCOUNT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                t = datetime.now().strftime("%H:%M:%S")
                print(f"[{t}] [{idx}] FAIL outer timeout", flush=True)
                result = None

            # Handle 429
            if result == "429":
                fail_count += 1
                # Try relay rotation first (different ASN)
                if _relay_list:
                    _rotate_relay(reason=f"429 at signup #{ok_count + 1}")
                    return
                # Then proxy rotation
                if _proxy_list:
                    _rotate_proxy(reason=f"429 at signup #{ok_count + 1}")
                    _ip_ok_counter = 0
                    return
                # Last resort: WARP (usually useless for tasklet)
                rotated = False
                if WARP_EVERY_N > 0:
                    try:
                        from core.warp import WarpClient
                        w = WarpClient(log=print)
                        print("[429] rotating WARP...", flush=True)
                        w.rotate_ip(force=True)
                        await asyncio.sleep(WARP_SETTLE_S)
                        _ip_ok_counter = 0
                        rotated = True
                    except Exception as e:
                        print(f"[429] WARP rotate failed: {e}", flush=True)
                if not rotated:
                    rate_limited = True
                    t = datetime.now().strftime("%H:%M:%S")
                    print(f"[{t}] [HALT] 429 rate limited, no relay/proxy/WARP available. Stopping.", flush=True)
                return

            if result and isinstance(result, dict):
                save_result(batch_dir, result)
                if inject_to_9router(result):
                    t = datetime.now().strftime("%H:%M:%S")
                    print(f"[{t}] [{idx}] 9router injected", flush=True)
                ok_count += 1

                # Proactive relay rotation before hitting ASN limit
                global _relay_ok_count
                if _relay_list:
                    _relay_ok_count += 1
                    if _relay_ok_count >= RELAY_ROTATE_EVERY:
                        _rotate_relay(reason=f"proactive at {_relay_ok_count} signups")
                elif _proxy_list and _ip_ok_counter >= PROXY_ROTATE_EVERY:
                    _rotate_proxy(reason=f"proactive at {_ip_ok_counter} signups")
                    _ip_ok_counter = 0
                else:
                    await _maybe_warp_after_success()
            else:
                fail_count += 1

    tasks = []
    for i in range(1, count + 1):
        if rate_limited:
            break
        tasks.append(asyncio.create_task(worker(i)))
        if i < count:
            await asyncio.sleep(random.uniform(JITTER_INTER_SIGNUP_MIN, JITTER_INTER_SIGNUP_MAX))

    await asyncio.gather(*tasks, return_exceptions=True)

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] [DONE] ok={ok_count} fail={fail_count} batch={batch_id}", flush=True)
    print(f"[{ts}] [DONE] results: {batch_dir}", flush=True)
    if rate_limited:
        print(f"[{ts}] [DONE] stopped early: IP rate limited (429)", flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tasklet magic-link farm (pure HTTP)")
    p.add_argument("-n", "--count", type=int, default=int(_env("TASKLET_MAX_ACCOUNTS", "5")))
    p.add_argument("-c", "--concurrent", type=int, default=CONCURRENT)
    p.add_argument("-y", "--yes", action="store_true", help="Non-interactive")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    count = args.count
    concurrent = args.concurrent

    if not EXZORK_KEY:
        print("ERROR: EXZORK_API_KEY not set", flush=True)
        sys.exit(1)
    if not EXZORK_DOMAIN:
        print("ERROR: TASKLET_EXZORK_DOMAIN or EXZORK_DOMAIN not set", flush=True)
        sys.exit(1)

    _init_proxies()
    _init_relays()

    if not args.yes:
        print(f"  Tasklet farm (magic link): {count} accounts, concurrent={concurrent}")
        print(f"  Domain: {EXZORK_DOMAIN}")
        print(f"  Relays: {len(_relay_list) or 'none (direct)'}")
        print(f"  Proxies: {len(_proxy_list) or 'none (direct)'}")
        print(f"  curl_cffi: {'yes' if HAS_CURL_CFFI else 'NO (install for Chrome TLS)'}")
        confirm = input("  Start? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.", flush=True)
            return

    print(f"[tasklet] plan: n={count} c={concurrent} proxies={len(_proxy_list)} rotate_every={PROXY_ROTATE_EVERY}", flush=True)
    asyncio.run(run_farm(count, concurrent))


if __name__ == "__main__":
    main()
