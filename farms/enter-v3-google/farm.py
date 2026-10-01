#!/usr/bin/env python3
"""
Enter/Converge farm — v3-google (OAuth variant).

Same product as ../enter-v3, but the pre-auth uses **Google OAuth** instead of
the email OTP flow. No mailbox, no OTP, no password page, no Turnstile on the
identifier step — just: gift landing -> FPJS -> risk-session -> Auth0 login
identifier -> "Continue with Google" -> Google signin -> consent -> callback
-> gateway JWT.

Architecture mirrors ../enter-v3 exactly (which is itself a thin layer over the
shared browser core in ../enter/farm.py):

  Auth      : browser (Camoufox) — reuse legacy launch_browser / goto_with_retry /
              extract_fpjs / _get_risk_session_id / _fetch_gateway_session / next_proxy
              verbatim, exactly as enter-v3 does. Only the signup driver is replaced.
  Post-auth : pure HTTP (postauth_v3.py, unchanged) — referral/claim ->
              users/info -> workspaces -> onboarding/config -> onboarding/complete
              -> api-keys -> ek_ key.

The only real difference from enter-v3 is the auth driver: this farm clicks the
Google provider form (form[data-provider="google"] / connection=google-oauth2)
and drives accounts.google.com, whereas enter-v3 fills email -> OTP -> password.

Reversed from live HAR captures:
  - google.har            : login path, prompt=login, risk-session + authorize + callback
  - signup google.har     : signup path with invite_code in risk-session
  - signup google denied.har : /authorize/resume -> access_denied signature

Accounts come from a pool file (firecrawl-style): one `email|password` per line.

Hub contract: CLI -n -c -y, line logs, one OK line per account, hub venv only.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

# ── Hub path + env bootstrap (mirror enter-v3) ────────────────────────────────
_ROOT = Path(__file__).resolve().parent          # farms/enter-v3-google
_HUB = _ROOT.parent.parent                        # Automation/
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_HUB / ".env", override=False)        # hub wins
load_dotenv(_ROOT / ".env", override=False)       # farm-local gaps only

# Legacy import mutates os.environ (its own load_dotenv), so snapshot now and
# restore after: precedence stays hub > farm-local > legacy.
_ENV_BEFORE_LEGACY = dict(os.environ)


# Load the legacy farm by explicit file path under a private name: several files
# are named farm.py, so a plain `import farm` would alias the wrong module.
# This is the same mechanism enter-v3 uses — the browser core lives there.
def _load_legacy_farm():
    import importlib.util

    legacy_path = _ROOT.parent / "enter" / "farm.py"
    if not legacy_path.is_file():
        raise FileNotFoundError(f"legacy farm missing: {legacy_path}")
    spec = importlib.util.spec_from_file_location("_enter_legacy_farm", legacy_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load legacy farm from {legacy_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_enter_legacy_farm"] = mod
    spec.loader.exec_module(mod)
    return mod


L = _load_legacy_farm()

# Undo the legacy farm's env leakage: keys it added are dropped, keys we had
# are restored. Hub/farm-local values therefore always win over legacy's .env.
for _k in [k for k in os.environ if k not in _ENV_BEFORE_LEGACY]:
    del os.environ[_k]
os.environ.update(_ENV_BEFORE_LEGACY)

sys.path.insert(0, str(_ROOT))
import postauth_v3 as P  # noqa: E402


# ── Config ────────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


ACCOUNT_GAP = _env_float("ENTER_ACCOUNT_GAP", 30.0)
SPAWN_DELAY = _env_float("ENTER_SPAWN_DELAY", 20.0)
MAX_ACCOUNTS = _env_int("ENTER_MAX_ACCOUNTS", 1)
CONCURRENT = _env_int("ENTER_CONCURRENT", 1)
ACCOUNT_TIMEOUT_S = max(120, _env_int("ENTER_ACCOUNT_TIMEOUT", 600))
POSTAUTH_TIMEOUT_S = max(60, _env_int("ENTER_POSTAUTH_TIMEOUT", 180))
GOOGLE_STEP_TIMEOUT_S = max(20, _env_int("ENTER_GOOGLE_STEP_TIMEOUT", 45))

# Google account pool: one `email|password` (or email:password / tab) per line.
GOOGLE_ACCOUNTS_FILE = Path(
    _env("ENTER_GOOGLE_ACCOUNTS", str(_ROOT / "google_accounts.txt"))
)

RESULTS_ROOT = Path(_env("ENTER_RESULTS_DIR", str(_ROOT / "results")))
SCREENSHOT_DIR = Path(_env("ENTER_SCREENSHOT_DIR", str(_ROOT / "screenshots")))

GIFT_CODE = _env("ENTER_GIFT_CODE") or getattr(L, "GIFT_CODE", "")
INVITER = _env("ENTER_INVITER") or getattr(L, "INVITER", "")
INVITEE_REWARD = _env("ENTER_INVITEE_REWARD", str(getattr(L, "INVITEE_REWARD", "100")))

# Referral "estafet": account #1 claims the seed code, then each account's own
# referral_code becomes the next account's gift. Because every claim uses a fresh
# code, the server-side throttle that silently withholds the invitee bonus on
# heavily-reused codes is avoided.
GIFT_CHAIN = _env_int("ENTER_GIFT_CHAIN", 0) > 0
CHAIN_FALLBACK_SEED = _env_int("ENTER_GIFT_CHAIN_FALLBACK", 1) > 0
CHAIN_STATE_FILE = Path(
    _env("ENTER_CHAIN_STATE", str(RESULTS_ROOT / "referral_chain.json"))
)

BATCH_ID = ""
BATCH_DIR: Path = RESULTS_ROOT
RESULTS_JSON: Path = RESULTS_ROOT / "accounts.json"
CREDS_TXT: Path = RESULTS_ROOT / "credentials.txt"
CREDS_KEYS_TXT: Path = RESULTS_ROOT / "apikeys.txt"
GLOBAL_CREDS_TXT = Path(_env("ENTER_GLOBAL_CREDS", str(RESULTS_ROOT / "all_credentials.txt")))
GLOBAL_KEYS_TXT = Path(_env("ENTER_GLOBAL_KEYS", str(RESULTS_ROOT / "all_apikeys.txt")))
USED_GOOGLE_FILE = Path(
    _env("ENTER_GOOGLE_USED_FILE", str(RESULTS_ROOT / "used_google.txt"))
)

# ── Proxy pool (multi-warp SOCKS5 / any proxy) ────────────────────────────────
# Farm-local on purpose: legacy _load_proxy_pool() resolves paths against
# farms/enter (L._ROOT), so it would read ../enter/proxy.txt instead of ours.
PROXY_FILE = Path(_env("ENTER_PROXY_FILE", str(_ROOT / "proxy.txt")))
PROXY_BRIDGE = _env("ENTER_PROXY_BRIDGE", "").strip().lower() in ("1", "true", "yes", "on")
PROXY_BRIDGE_SLOTS = max(1, _env_int("ENTER_PROXY_BRIDGE_SLOTS", 4))
PROXY_BRIDGE_BASE_PORT = max(1, _env_int("ENTER_PROXY_BRIDGE_BASE_PORT", 60000))
CHECK_EXIT_IP = _env("ENTER_CHECK_EXIT_IP", "1").strip().lower() in ("1", "true", "yes", "on")
EXIT_IP_TIMEOUT = max(5.0, _env_float("ENTER_EXIT_IP_TIMEOUT", 20.0))

_results_lock = asyncio.Lock()
_vps_pusher = None
_used_exit_ips: dict[str, int] = {}

_chain_lock = asyncio.Lock()
_chain_next_gift = ""
_chain_used: set[str] = set()
_chain_index = 0


def _load_chain_state() -> None:
    global _chain_next_gift, _chain_index
    if not CHAIN_STATE_FILE.is_file():
        return
    try:
        st = json.loads(CHAIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(st, dict):
        return
    _chain_next_gift = str(st.get("next_gift") or "").strip()
    _chain_index = int(st.get("index") or 0)
    used = st.get("used_codes")
    if isinstance(used, list):
        _chain_used.update(str(c).strip() for c in used if str(c).strip())


def _save_chain_state() -> None:
    CHAIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": GIFT_CODE,
        "next_gift": _chain_next_gift,
        "index": _chain_index,
        "used_codes": sorted(_chain_used),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    CHAIN_STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


async def _chain_claim_gift(attempt: int) -> str:
    if not GIFT_CHAIN:
        return GIFT_CODE
    async with _chain_lock:
        if _chain_next_gift:
            gift = _chain_next_gift
        elif _chain_index == 0:
            gift = GIFT_CODE
        elif CHAIN_FALLBACK_SEED and GIFT_CODE:
            slog(
                "CHAIN",
                f"chain broke after {_chain_index} link(s); falling back to seed "
                f"{GIFT_CODE} (bonus may be withheld) - re-seed for the next run",
            )
            gift = GIFT_CODE
        else:
            raise RuntimeError(
                f"referral chain broke after {_chain_index} link(s): no next gift. "
                "Re-seed with ENTER_GIFT_CODE or clear "
                f"{CHAIN_STATE_FILE} to start over."
            )
        if not gift:
            raise RuntimeError(
                "referral chain has no seed: set ENTER_GIFT_CODE or resume with "
                f"a next_gift in {CHAIN_STATE_FILE}"
            )
        if gift in _chain_used:
            slog("CHAIN", f"warning: gift {gift} already used earlier in this chain")
        return gift


async def _chain_advance(attempt: int, gift: str, own_code: str, landed: bool | None) -> None:
    if not GIFT_CHAIN:
        return
    global _chain_next_gift, _chain_index
    async with _chain_lock:
        _chain_used.add(gift)
        _chain_index += 1
        if not own_code:
            slog("CHAIN", f"#{_chain_index} produced no referral_code; chain broken at {gift}")
            _chain_next_gift = ""
        else:
            if landed is False:
                slog("CHAIN", f"#{_chain_index} invitee bonus NOT credited (credits<200) via {gift}")
            else:
                slog("CHAIN", f"#{_chain_index} link ok: {gift} -> next {own_code}")
            _chain_next_gift = own_code
        _save_chain_state()


def _load_proxy_pool_local() -> list[tuple[str, str]]:
    """Parse PROXY_FILE (and ENTER_PROXY_POOL) into [(url, id)].

    Accepts multi-warp's warp-pool.txt verbatim: `socks5://127.0.0.1:40001`.
    Rejects entries that do not look like a proxy (e.g. an ek_ API key that
    someone dropped in the file) instead of turning them into http://ek_... .
    """
    entries: list[tuple[str, str]] = []
    if PROXY_FILE.is_file():
        for raw in PROXY_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            ent = L._parse_proxy_entry(line)
            if not ent:
                continue
            url, pid = ent
            parsed = urlparse(url)
            if not parsed.hostname or not parsed.port:
                slog("PROXY", f"skip (not host:port): {url[:40]}")
                continue
            if parsed.scheme.startswith("ek_"):
                slog("PROXY", "skip (looks like an api key, not a proxy)")
                continue
            entries.append((url, pid))
    for part in _env("ENTER_PROXY_POOL").split(","):
        ent = L._parse_proxy_entry(part.strip())
        if ent:
            entries.append(ent)
    if _env("ENTER_PROXY_SHUFFLE", "").strip().lower() in ("1", "true", "yes", "on"):
        random.shuffle(entries)
    return entries


def _probe_exit_ip(proxy_url: str) -> str:
    """Exit IP as Cloudflare sees it — IPv6 first, because WARP reuses IPv4
    anycast exits but hands out distinct IPv6 per registration."""
    import socks  # PySocks, hub venv

    host = urlparse(proxy_url).hostname or "127.0.0.1"
    port = urlparse(proxy_url).port or 1080
    s = socks.socksocket()
    try:
        s.set_proxy(socks.SOCKS5, host, port)
        s.settimeout(EXIT_IP_TIMEOUT)
        s.connect(("auth.converge.ai", 443))
        ss = ssl.create_default_context().wrap_socket(s, server_hostname="auth.converge.ai")
        ss.sendall(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: auth.converge.ai\r\nConnection: close\r\nUser-Agent: Mozilla/5.0\r\n\r\n")
        buf = b""
        while True:
            chunk = ss.recv(65536)
            if not chunk:
                break
            buf += chunk
        ss.close()
        body = buf.decode("utf-8", "replace").split("\r\n\r\n", 1)[-1]
        for line in body.splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
        return ""
    except Exception:
        return ""
    finally:
        try:
            s.close()
        except Exception:
            pass


def _audit_proxy_pool(pool: list[tuple[str, str]]) -> None:
    """Log exit IP per proxy and flag duplicates.

    WARP anycast reuses exits, so a duplicate is a warning: it means two
    accounts can share one IP even though the farm round-robins the pool.
    """
    if not pool or not CHECK_EXIT_IP:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(pool))) as ex:
        ips = list(ex.map(lambda e: _probe_exit_ip(e[0]), pool))
    seen: dict[str, list[str]] = {}
    for (url, _pid), ip in zip(pool, ips):
        tag = url.replace("socks5://", "s5:").replace("http://", "h:")
        if not ip:
            slog("PROXY", f"{tag} -> unreachable")
            continue
        slog("PROXY", f"{tag} -> {ip}")
        seen.setdefault(ip, []).append(tag)
    dupes = {ip: tags for ip, tags in seen.items() if len(tags) > 1}
    for ip, tags in dupes.items():
        slog("PROXY", f"WARN duplicate exit {ip} shared by {len(tags)} ports: {', '.join(tags)}")
    slog("PROXY", f"pool={len(pool)} reachable={len(ips) - ips.count('')} unique_exit_ips={len(seen)}")


# ── Logging ───────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def slog(tag: str, msg: str) -> None:
    print(f"[{_ts()}] [{tag}] {msg}", flush=True)


def alog(attempt: int, msg: str) -> None:
    print(f"[{_ts()}] [{attempt}] {msg}", flush=True)


def emit_progress(attempt: int, step: str, message: str, email: str = "") -> None:
    tail = f"  <{email}>" if email else ""
    print(f"[{_ts()}] [{attempt}] {step}  {message}{tail}".rstrip(), flush=True)


def emit_success(attempt: int, email: str, message: str = "ok") -> None:
    print(f"[{_ts()}] [{attempt}] OK  {message}  <{email}>", flush=True)


def emit_failed(attempt: int, message: str, email: str = "") -> None:
    tail = f"  <{email}>" if email else ""
    print(f"[{_ts()}] [{attempt}] FAIL  {message}{tail}".rstrip(), flush=True)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Google account pool ───────────────────────────────────────────────────────
def load_google_accounts() -> list[tuple[str, str]]:
    """Load email/password pairs. Formats: email|pass, email:pass, email<TAB>pass."""
    if not GOOGLE_ACCOUNTS_FILE.is_file():
        return []
    out: list[tuple[str, str]] = []
    for raw in GOOGLE_ACCOUNTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("|", "\t", ":"):
            if sep in line:
                email, _, pw = line.partition(sep)
                email, pw = email.strip(), pw.strip()
                if "@" in email and pw:
                    out.append((email, pw))
                break
    return out


def _load_used_google() -> set[str]:
    """Emails already farmed (results + used file)."""
    used: set[str] = set()
    if USED_GOOGLE_FILE.is_file():
        for line in USED_GOOGLE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            e = line.strip().lower()
            if e and "@" in e and not e.startswith("#"):
                used.add(e)
    if RESULTS_ROOT.is_dir():
        for p in RESULTS_ROOT.glob("batch_*/accounts.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for row in data if isinstance(data, list) else []:
                e = (row.get("google_email") or row.get("email") or "").lower()
                if e:
                    used.add(e)
    return used


def _persist_used_google(email: str) -> None:
    _append(USED_GOOGLE_FILE, email.lower())


# ── Browser auth (Google OAuth) ───────────────────────────────────────────────
# HAR google.har: the identifier page carries a SECONDARY provider form:
#   <form data-provider="google" data-form-secondary="true" method="post">
#     <input type="hidden" name="state" value="hKFo...">
#     <input type="hidden" name="connection" value="google-oauth2">
#     <button type="submit" data-provider="google" data-action-button-secondary="true">
# Clicking that button POSTs state+connection=google-oauth2 -> 302 to
# accounts.google.com/o/oauth2/auth (entry 366 -> 367).
_GOOGLE_FORM_SEL = (
    'form[data-provider="google"] button[type="submit"]',
    'form[data-provider="google"] button',
    'button[data-provider="google"]',
    'form[data-provider="google"]',
)

_GOOGLE_EMAIL_SEL = (
    "#identifierId",
    'input[type="email"]',
    'input[name="identifier"]',
    'input[name="Email"]',
)

_GOOGLE_PW_SEL = (
    'input[type="password"]',
    'input[name="Passwd"]',
    'input[name="password"]',
)

_GOOGLE_NEXT_SEL = (
    "#identifierNext button",
    "#identifierNext",
    "#passwordNext button",
    "#passwordNext",
)

# Google's NORMAL password page lives at /v3/signin/challenge/pwd (proven by
# google.har entry 402: source-path=%2Fv3%2Fsignin%2Fch...). So "challenge" in
# the URL is NOT a 2FA signal — only these second-factor subtypes are.
_GOOGLE_2FA_MARKERS = (
    "challenge/ipp",       # phone number prompt
    "challenge/totp",      # authenticator app
    "challenge/az",        # account recovery
    "challenge/dp",        # device prompt
    "challenge/iap",       # in-app prompt
    "challenge/sk",        # security key
    "challenge/pk",        # passkey
    "challenge/recaptcha",  # reCAPTCHA gate
    "challenge/ootp",      # offline one-time passcode
    "challenge/selection",  # method chooser
    "challenge/webauthn",
)

_TOS_SEL = (
    'button:has-text("I understand")',
    'button:has-text("Accept")',
    'button:has-text("Saya memahami")',
    'button:has-text("Continue")',
    'button:has-text("Got it")',
)

_CONSENT_SEL = (
    'button:has-text("Continue")',
    'button:has-text("Allow")',
    'button:has-text("Lanjutkan")',
    'button:has-text("Izinkan")',
    "#submit_approve_access",
    'button:has-text("Accept")',
)


def _host_of(u: str) -> str:
    try:
        return urlparse(u or "").netloc.lower()
    except Exception:
        return ""


def _is_google_url(u: str) -> bool:
    return _host_of(u).endswith("google.com") or _host_of(u).endswith("google.co.id")


def _on_enter_host(u: str) -> bool:
    try:
        return _host_of(u) == _host_of(L.APP_HOST)
    except Exception:
        return False


async def _click_first(page, selectors, *, timeout: int = 4000, scroll: bool = False) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            if scroll:
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
            await loc.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _fill_google_field(page, selectors, value: str, attempt: int, label: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            await loc.click(timeout=4000)
            await loc.fill("")
            await loc.type(value, delay=random.randint(25, 60))
            alog(attempt, f"google {label} filled via {sel}")
            return True
        except Exception:
            continue
    return False


async def _click_google_next(page, attempt: int) -> None:
    if await _click_first(page, _GOOGLE_NEXT_SEL, timeout=5000):
        return
    # Fallback: implicit submission via Enter on the focused field.
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


async def _goto_login_identifier(page, attempt: int) -> str:
    """gift landing -> FPJS -> risk-session -> /auth/login -> /u/login/identifier.

    Mirrors enter-v3's _goto_signup_identifier but targets the LOGIN identifier
    page (google.har entry 341 and signup google.har entry 172 both land on
    /u/login/identifier, since the account is created by Google OAuth).
    Returns the Auth0 state param.
    """
    q = {"gift": GIFT_CODE, "inviteeReward": INVITEE_REWARD}
    if INVITER:
        q["inviter"] = INVITER
    await L.goto_with_retry(
        page, f"{L.APP_HOST}/?{urlencode(q)}", attempt, label="g_landing", warp_on_fail=False
    )
    await asyncio.sleep(2.5)

    v, e = await L.extract_fpjs(page, attempt)
    if not (v and e):
        raise RuntimeError("FPJS unavailable - aborting (random ids => access_denied)")
    rs = await asyncio.to_thread(L._get_risk_session_id, GIFT_CODE, v, e)
    if not rs:
        raise RuntimeError("risk-session creation failed")

    login_url = f"{L.APP_HOST}/auth/login?{urlencode({'return_to': '/', 'risk_session_id': rs})}"
    await L.goto_with_retry(page, login_url, attempt, label="g_gateway", warp_on_fail=False)

    for _ in range(60):
        u = page.url or ""
        if "/u/" in u and ("login" in u or "signup" in u):
            break
        await asyncio.sleep(0.5)

    state = ""
    try:
        state = L.parse_qs(L.urlparse(page.url).query).get("state", [""])[0]
    except Exception:
        state = ""
    if not state:
        raise RuntimeError(f"no auth0 state in url: {page.url[:90]}")

    await L.goto_with_retry(
        page, f"{L.AUTH_HOST}/u/login/identifier?state={state}", attempt,
        label="g_identifier", warp_on_fail=False,
    )
    await asyncio.sleep(1.5)
    alog(attempt, f"login identifier ready state={state[:16]}..")
    return state


async def _click_google_provider(page, attempt: int) -> bool:
    """Click the Google provider button and confirm we left for Google.

    Native click submits the form; if the click is swallowed we fall back to
    requestSubmit on the provider form (the same trick the email farm uses for
    its honeypot-proof primary submit).
    """
    start = (page.url or "").split("?")[0]
    for mode in ("click", "js", "requestSubmit"):
        try:
            if mode == "click":
                if not await _click_first(page, _GOOGLE_FORM_SEL, timeout=6000):
                    continue
            elif mode == "js":
                ok = await page.evaluate(
                    """() => {
                        const f = document.querySelector('form[data-provider="google"]');
                        if (!f) return false;
                        const b = f.querySelector('button[type="submit"], button');
                        if (!b) return false;
                        b.click();
                        return true;
                    }"""
                )
                if not ok:
                    continue
            else:
                ok = await page.evaluate(
                    """() => {
                        const f = document.querySelector('form[data-provider="google"]');
                        if (!f) return false;
                        const b = f.querySelector('button[type="submit"], button');
                        if (f.requestSubmit) { f.requestSubmit(b || undefined); }
                        else { f.submit(); }
                        return true;
                    }"""
                )
                if not ok:
                    continue
        except Exception:
            continue
        for _ in range(20):  # ~10s to reach Google
            await asyncio.sleep(0.5)
            u = page.url or ""
            if _is_google_url(u) or (u.split("?")[0] != start and "accounts.google" in u):
                alog(attempt, f"google provider submitted via {mode}")
                return True
        alog(attempt, f"google provider via {mode} did not navigate, retrying")
    return False


async def _handle_google_step(page, attempt: int, url: str) -> str:
    """One interstitial decision on accounts.google.com. Returns an action tag."""
    if "speedbump/workspacetermsofservice" in url:
        alog(attempt, "google workspace TOS")
        for _ in range(4):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            if await _click_first(page, _TOS_SEL, timeout=4000, scroll=True):
                break
            await asyncio.sleep(0.5)
        return "tos"

    if "signin/oauth/consent" in url or "signin/oauth/id" in url:
        alog(attempt, "google oauth consent")
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        if await _click_first(page, _CONSENT_SEL, timeout=4000, scroll=True):
            return "consent"
        # JS fallback across localized labels.
        try:
            await page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')];
                    for (const b of btns) {
                        const t = (b.innerText || b.value || '').toLowerCase();
                        if (t.includes('continue') || t.includes('allow') ||
                            t.includes('lanjutkan') || t.includes('izinkan') ||
                            t.includes('accept')) {
                            b.scrollIntoView({block:'center'}); b.click(); return true;
                        }
                    }
                    return false;
                }"""
            )
        except Exception:
            pass
        return "consent"

    if "challenge" in url or "signin/challenge" in url:
        if any(marker in url for marker in _GOOGLE_2FA_MARKERS):
            return "challenge"
        return "wait"

    return "wait"


async def do_signup_google(page, email_addr: str, password: str, attempt: int) -> dict:
    """Full browser signup via Google OAuth, ending on the gateway JWT.

    Steps: gift landing -> FPJS -> risk-session -> Auth0 login identifier ->
    Continue with Google -> Google email -> Google password (if asked) ->
    Workspace TOS / consent interstitials -> auth.converge.ai/login/callback ->
    /authorize/resume -> enter.converge.ai/auth/callback -> /auth/session.
    """
    await _goto_login_identifier(page, attempt)

    if not await _click_google_provider(page, attempt):
        raise RuntimeError("google provider button not found / did not redirect")

    await asyncio.sleep(1.5)
    if not await _fill_google_field(page, _GOOGLE_EMAIL_SEL, email_addr, attempt, "email"):
        raise RuntimeError("could not fill google email")
    await asyncio.sleep(0.6)
    await _click_google_next(page, attempt)
    await asyncio.sleep(2.5)

    # Drive interstitials until the app callback comes back.
    deadline = time.monotonic() + max(60.0, GOOGLE_STEP_TIMEOUT_S * 3)
    pw_done = False
    while time.monotonic() < deadline:
        u = page.url or ""
        if "/auth/callback" in u or "code=" in u or _on_enter_host(u):
            break
        if "error=access_denied" in u:
            raise RuntimeError("access_denied at callback (Google denied the request)")

        if _is_google_url(u):
            if "pwd" in u:
                if pw_done:
                    await asyncio.sleep(1.0)
                    continue
                pw_done = True
                if not await _fill_google_field(page, _GOOGLE_PW_SEL, password, attempt, "password"):
                    raise RuntimeError("could not fill google password")
                await asyncio.sleep(0.6)
                await _click_google_next(page, attempt)
                await asyncio.sleep(2.5)
                continue
            action = await _handle_google_step(page, attempt, u)
            if action == "challenge":
                raise RuntimeError("google 2FA/challenge detected")
            await asyncio.sleep(1.5)
            continue

        await asyncio.sleep(1.0)

    for i in range(120):
        u = page.url or ""
        if "/auth/callback" in u or "code=" in u or _on_enter_host(u):
            break
        if i % 20 == 19:
            alog(attempt, f"waiting callback url={u.split('?')[0][:70]}")
        await asyncio.sleep(0.5)

    if "error=access_denied" in (page.url or ""):
        raise RuntimeError("access_denied at callback")

    u = page.url or ""
    if not (_on_enter_host(u) or "/auth/callback" in u or "code=" in u):
        try:
            await L.wait_url(
                page,
                lambda x: _on_enter_host(x) or "/auth/callback" in x or "code=" in x,
                20,
            )
        except Exception:
            await L.goto_with_retry(page, f"{L.APP_HOST}/", attempt, label="g_callback_return")

    tokens = await L._fetch_gateway_session(page)
    tokens.update({
        "refresh_token": "", "id_token": "", "token_type": "Bearer",
        "scope": L.SCOPE, "email_from_id": "",
    })
    alog(attempt, "authenticated gateway session established")
    return tokens


# ── Batch / results ───────────────────────────────────────────────────────────
def init_batch(n: int, c: int) -> str:
    global BATCH_ID, BATCH_DIR, RESULTS_JSON, CREDS_TXT, CREDS_KEYS_TXT
    pool = _load_proxy_pool_local()
    if PROXY_BRIDGE and pool:
        L._proxy_pool = pool
        L._spawn_proxy_bridge(c)
        pool = list(L._proxy_pool)
    else:
        L._proxy_pool = pool
    if pool:
        _audit_proxy_pool(pool)
    else:
        slog("PROXY", "no proxy pool -> every account egresses from the local IP "
                      "(this is what triggers Auth0 'too many signup attempts')")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BATCH_ID = _env("ENTER_BATCH_ID") or f"batch_{stamp}_{secrets.token_hex(3)}"
    BATCH_DIR = RESULTS_ROOT / BATCH_ID
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON = BATCH_DIR / "accounts.json"
    CREDS_TXT = BATCH_DIR / "credentials.txt"
    CREDS_KEYS_TXT = BATCH_DIR / "apikeys.txt"
    for p, empty in ((RESULTS_JSON, "[]"), (CREDS_TXT, ""), (CREDS_KEYS_TXT, "")):
        if not p.exists():
            p.write_text(empty + ("\n" if empty else ""), encoding="utf-8")
    if not GLOBAL_CREDS_TXT.exists():
        GLOBAL_CREDS_TXT.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_CREDS_TXT.write_text(
            "# google_email|google_password|api_key|workspace_id|key_id|key_name|created_at"
            "|batch_id|referral_code|credits_total\n",
            encoding="utf-8",
        )
    if not GLOBAL_KEYS_TXT.exists():
        GLOBAL_KEYS_TXT.write_text(
            "# one api key per line (successful farms only)\n", encoding="utf-8"
        )
    meta = {
        "batch_id": BATCH_ID,
        "variant": "enter-v3-google",
        "auth": "google-oauth2",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gift_code": GIFT_CODE,
        "gift_chain": GIFT_CHAIN,
        "max_accounts": n,
        "concurrent": c,
        "proxy_pool_size": len(pool),
    }
    (BATCH_DIR / "batch_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    slog("BATCH", f"id={BATCH_ID} dir={BATCH_DIR}")
    return BATCH_ID


async def save_result(result: dict) -> None:
    async with _results_lock:
        try:
            rows = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
        rows.append(result)
        RESULTS_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

        email = result.get("google_email") or result.get("email", "")
        pw = result.get("google_password") or result.get("password", "")
        ws = result.get("workspace_id", "")
        key = (result.get("api_key") or {}).get("key", "")
        key_id = (result.get("api_key") or {}).get("id", "")
        key_name = (result.get("api_key") or {}).get("name", "")
        created = result.get("created_at", "")
        ref_code = result.get("referral_code", "")
        credits = result.get("credits_total")
        credits_s = "" if credits is None else str(credits)
        _append(CREDS_TXT, "|".join(
            str(x) for x in [email, pw, key, ws, key_id, key_name, created, ref_code, credits_s]
        ))
        if key:
            _append(CREDS_KEYS_TXT, key)
        _append(GLOBAL_CREDS_TXT, "|".join(
            str(x) for x in
            [email, pw, key, ws, key_id, key_name, created, BATCH_ID, ref_code, credits_s]
        ))
        if key:
            _append(GLOBAL_KEYS_TXT, f"{key}\t{email}\t{ws}\t{BATCH_ID}")
        _persist_used_google(email)
        slog("SAVE", f"{email} ws={ws} key={key[:12]}... -> {CREDS_TXT}")


async def _push_ninerouter(result: dict) -> bool:
    """Best-effort remote 9router push (hub pusher). Returns pushed bool."""
    if _vps_pusher is None:
        return True  # nothing to push == success for accounting
    key = (result.get("api_key") or {}).get("key", "")
    if not key:
        return False
    try:
        from core.ninerouter import make_credential  # type: ignore

        tokens = result.get("tokens") or {}
        email = result.get("google_email") or result.get("email", "")
        data_obj = {
            "displayName": email,
            "apiKey": key,
            "testStatus": "active",
            "providerSpecificData": {
                "workspaceId": result.get("workspace_id", ""),
                "email": email,
            },
            "lastError": None,
            "lastErrorAt": None,
        }
        if tokens.get("access_token"):
            data_obj["accessToken"] = tokens["access_token"]
        cred = make_credential(L.NINEROUTER_PROVIDER, email, data_obj)
        return bool(_vps_pusher.queue(cred))
    except Exception as e:
        slog("9ROUTER", f"push error: {type(e).__name__}: {e}")
        return False


# ── One account ───────────────────────────────────────────────────────────────
async def do_account(
    attempt: int, account_q: asyncio.Queue, slot_q: asyncio.Queue, results: list
) -> dict | None:
    global GIFT_CODE
    email = ""
    proxy_url = proxy_id = None
    manager = _browser = None
    proxy_token = None
    exit_ip = ""
    slot = None
    t_start = time.time()
    try:
        slot = await slot_q.get()
        email, google_pw = await account_q.get()
        emit_progress(attempt, "START", f"slot={slot} google oauth account", email)
        gift = await _chain_claim_gift(attempt)

        proxy_url, proxy_id = await L.next_proxy()
        # Pin the risk-session POST to the same egress as the browser. Legacy
        # documents this as required (browser + risk-session + post-auth must be
        # one IP); enter-v3 omits the pin, we make it explicit here.
        try:
            proxy_token = L._current_proxy.set(proxy_url or None)
        except Exception:
            proxy_token = None

        if proxy_url and CHECK_EXIT_IP:
            exit_ip = await asyncio.to_thread(_probe_exit_ip, proxy_url)
            if exit_ip:
                _used_exit_ips[exit_ip] = _used_exit_ips.get(exit_ip, 0) + 1
                dup = " (DUPLICATE IP)" if _used_exit_ips[exit_ip] > 1 else ""
                alog(attempt, f"egress ip={exit_ip}{dup}")
            else:
                alog(attempt, "egress ip probe failed (proxy may be down)")
        elif not proxy_url:
            alog(attempt, "no proxy -> direct egress (local IP)")

        # _goto_login_identifier builds the landing URL from the module GIFT_CODE,
        # so the per-account chain code has to be pushed in for the duration.
        _prev_gift = GIFT_CODE
        GIFT_CODE = gift
        try:
            manager, _browser, page = await L.launch_browser(proxy_url)
            try:
                tokens = await asyncio.wait_for(
                    do_signup_google(page, email, google_pw, attempt),
                    timeout=ACCOUNT_TIMEOUT_S,
                )
            finally:
                if _browser is not None:
                    try:
                        await _browser.close()
                        alog(attempt, "browser closed")
                    except Exception as e:
                        alog(attempt, f"browser close: {type(e).__name__}")
                if manager is not None:
                    try:
                        await manager.__aexit__(None, None, None)
                    except Exception:
                        pass
        finally:
            GIFT_CODE = _prev_gift

        access_token = tokens.get("access_token", "")
        if not access_token:
            raise RuntimeError("no access token from gateway session")
        alog(attempt, "gateway session established (browser closed)")

        # Post-auth v3 over HTTP — identical to enter-v3 (postauth_v3.py).
        emit_progress(attempt, "POSTAUTH", "claim/info/workspaces/onboarding/api-key", email)
        meta = await asyncio.to_thread(
            P.post_auth_setup, access_token, gift, proxy=proxy_url
        )
        key_data = (meta.get("api_key") or {}).get("data") or {}
        if not key_data.get("key"):
            raise RuntimeError("post-auth did not return an api key")

        own_code = str(meta.get("referral_code") or "")
        credits_total = meta.get("credits_total")
        bonus_landed = meta.get("invitee_bonus_landed")
        await _chain_advance(attempt, gift, own_code, bonus_landed)
        if bonus_landed is False:
            emit_progress(
                attempt, "CREDITS",
                f"invitee bonus NOT credited (total={credits_total}) via {gift}", email,
            )
        elif bonus_landed is True:
            emit_progress(attempt, "CREDITS", f"bonus landed (total={credits_total})", email)

        result = {
            "google_email": email,
            "google_password": google_pw,
            "email": email,
            "password": google_pw,
            "gift_code": gift,
            "referral_code": own_code,
            "credits_total": credits_total,
            "invitee_bonus_landed": bonus_landed,
            "variant": "enter-v3-google",
            "auth": "google-oauth2",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempt": attempt,
            "proxy": proxy_url or "direct",
            "exit_ip": exit_ip or ("direct" if not proxy_url else ""),
            "workspace_id": meta.get("workspace_id"),
            "flow_version": meta.get("flow_version"),
            "onboarding_skipped": meta.get("onboarding_skipped"),
            "referral_claim_error": meta.get("referral_claim_error"),
            "tokens": {
                "access_token": access_token,
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": tokens.get("expires_at", ""),
                "scope": tokens.get("scope", ""),
            },
            "api_key": {
                "id": key_data.get("id"),
                "key": key_data.get("key"),
                "name": key_data.get("name"),
                "scope": key_data.get("scope"),
                "reveal_policy": key_data.get("reveal_policy"),
            },
        }
        await save_result(result)
        pushed = await _push_ninerouter(result)
        emit_success(attempt, email, "ok" if pushed else "ok (push pending)")
        await L._maybe_warp_after_success(attempt)  # noqa: SLF001 (hub warp hook)
        alog(attempt, f"done in {time.time() - t_start:.0f}s")
        return result

    except asyncio.TimeoutError:
        emit_failed(attempt, f"account timeout after {ACCOUNT_TIMEOUT_S}s", email)
        return None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        emit_failed(attempt, msg, email)
        try:
            await L.save_failed_to_file(attempt, email, msg)
        except Exception:
            pass
        low = msg.lower()
        if any(x in low for x in ("too many", "rate limit", "try again later")):
            try:
                await L._trip_rate_limit(attempt, msg[:160])  # noqa: SLF001
            except Exception:
                pass
        return None
    finally:
        if proxy_token is not None:
            try:
                L._current_proxy.reset(proxy_token)
            except Exception:
                pass
        if ACCOUNT_GAP > 0:
            g = ACCOUNT_GAP + random.uniform(0, min(8.0, ACCOUNT_GAP * 0.3))
            await asyncio.sleep(g)
        if slot is not None:
            slot_q.put_nowait(slot)


# ── CLI ───────────────────────────────────────────────────────────────────────
async def main() -> None:
    import argparse

    global MAX_ACCOUNTS, CONCURRENT, SPAWN_DELAY, ACCOUNT_GAP, _vps_pusher, GIFT_CHAIN

    ap = argparse.ArgumentParser(
        description="Enter/Converge v3-google farmer (Google OAuth auth + HTTP v3 post-auth)"
    )
    ap.add_argument("-n", "--count", type=int, default=None,
                    help="accounts this run (default: all unused in the pool file)")
    ap.add_argument("-c", "--concurrent", type=int, default=None)
    ap.add_argument("-y", "--yes", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--spawn-delay", type=float, default=None)
    ap.add_argument("--account-gap", type=float, default=None)
    ap.add_argument(
        "--chain", action="store_true",
        help="referral chain: account N's referral_code becomes account N+1's gift",
    )
    args = ap.parse_args()

    if args.chain:
        GIFT_CHAIN = True

    if args.headless:
        L.HEADLESS = True
    if args.headed:
        L.HEADLESS = False
    if args.spawn_delay is not None:
        SPAWN_DELAY = max(0.0, args.spawn_delay)
    if args.account_gap is not None:
        ACCOUNT_GAP = max(0.0, args.account_gap)

    if L.AUTH_MODE != "browser":
        print("ERROR: enter-v3-google requires ENTER_AUTH_MODE=browser", flush=True)
        sys.exit(1)

    pool = load_google_accounts()
    if not pool:
        print(
            f"ERROR: no Google accounts in {GOOGLE_ACCOUNTS_FILE} "
            "(one email|password per line)",
            flush=True,
        )
        sys.exit(1)
    used = _load_used_google()
    available = [(e, p) for e, p in pool if e.lower() not in used]
    if not available:
        print(f"ERROR: all {len(pool)} Google accounts already used", flush=True)
        sys.exit(1)

    if GIFT_CHAIN:
        _load_chain_state()
        if not GIFT_CODE and not _chain_next_gift:
            print(
                "ERROR: ENTER_GIFT_CHAIN needs a seed: set ENTER_GIFT_CODE (first link) "
                f"or resume with next_gift in {CHAIN_STATE_FILE}",
                flush=True,
            )
            sys.exit(1)

    n = args.count if args.count is not None else len(available)
    c = args.concurrent if args.concurrent is not None else CONCURRENT
    n = max(1, n)
    c = max(1, min(c, n))
    if n > len(available):
        print(f"[WARN] only {len(available)} unused accounts available (requested {n})", flush=True)
        n = len(available)
    c = max(1, min(c, n))
    if GIFT_CHAIN and c != 1:
        print(
            f"NOTE: referral chain is serial; forcing concurrency {c} -> 1 "
            "(each link needs the previous account's referral_code)",
            flush=True,
        )
        c = 1
    CONCURRENT = c

    init_batch(n, c)

    every_n = L._effective_warp_every_n()  # noqa: SLF001
    slog("CFG", f"variant=enter-v3-google auth=google-oauth2 headless={L.HEADLESS} "
                f"gift={GIFT_CODE or '-'} chain={'on' if GIFT_CHAIN else 'off'} "
                f"inviter={INVITER or '-'} concurrency={c} "
                f"warp_every_n={every_n or 'off'} google_pool={len(available)}/{len(pool)} "
                f"proxy_pool={len(L._proxy_pool)}")
    if GIFT_CHAIN:
        slog("CHAIN", f"next_gift={_chain_next_gift or GIFT_CODE} state={CHAIN_STATE_FILE}")
    if L.NINEROUTER_VPS_EVERY_N > 0:
        try:
            from core.ninerouter import NinerouterPusher  # type: ignore

            _vps_pusher = NinerouterPusher(provider=L.NINEROUTER_PROVIDER, every_n=L.NINEROUTER_VPS_EVERY_N)
            slog("9ROUTER", f"VPS push enabled every_n={L.NINEROUTER_VPS_EVERY_N} host={_vps_pusher.host}")
        except Exception as e:
            slog("9ROUTER", f"pusher init skipped: {type(e).__name__}: {e}")

    account_q: asyncio.Queue = asyncio.Queue()
    for acct in available[:n]:
        account_q.put_nowait(acct)

    slot_q: asyncio.Queue = asyncio.Queue()
    for s in range(c):
        slot_q.put_nowait(s)

    results: list = []
    tasks = []
    for i in range(1, n + 1):
        tasks.append(asyncio.create_task(do_account(i, account_q, slot_q, results)))
        if SPAWN_DELAY > 0 and i < n:
            await asyncio.sleep(SPAWN_DELAY + random.uniform(0, min(5.0, SPAWN_DELAY * 0.25)))

    outcomes = await asyncio.gather(*tasks)
    results.extend([r for r in outcomes if r])

    if _vps_pusher is not None:
        try:
            _vps_pusher.flush()
            s = _vps_pusher.stats
            slog("9ROUTER", f"VPS push final: pushed={s['pushed']} failed={s['failed']}")
        except Exception as e:
            slog("9ROUTER", f"flush error: {type(e).__name__}: {e}")

    ok = len(results)
    slog("DONE", f"ok={ok}/{n} batch={BATCH_DIR}")
    print(f"[DONE] ok={ok}/{n} batch={BATCH_DIR}", flush=True)
    try:
        L._stop_proxy_bridge()  # noqa: SLF001
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(main())
