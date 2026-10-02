#!/usr/bin/env python3
"""
Railway farm, Google OAuth signup/login (standalone).

Railway has no email/password registration: "register" == the first Google
OAuth login. One account, one sticky egress, one browser, then pure-HTTP
onboarding over Railway's cookie-authenticated internal GraphQL.

Flow per account (verified from the capture HAR, see README.md):

  1. Load a Google account (email|password) from the pool file.
  2. Pick one proxy (1 account = 1 egress: browser + every post-auth HTTP call).
  3. Camoufox → `GET https://backboard.railway.com/login/google?state=<b64>`
     → 302 → accounts.google.com/o/oauth2/v2/auth (Railway holds the PKCE
     verifier in a cookie, so the browser just follows the redirect).
  4. Drive accounts.google.com: email → Next → password (challenge/pwd is a
     NORMAL page, not 2FA) → workspace-TOS speedbump → OAuth consent → callback.
  5. Callback 302 → https://railway.com/new sets the session cookies
     (rw.session, rw.session.sig, rw.authenticated, rw.authenticated.sig).
  6. Post-auth over HTTP (cookie header, no bearer token):
       query me                 → user id + workspaces[0].id + plan
       mutation userTermsUpdate → accept terms
       mutation fairUseAgree    → {"agree": true}
       query workspace / freePlanBalance → plan/trial verification
  7. Save result (email + password + cookie string + ids + plan).

Fail-soft: if GraphQL onboarding fails, the cookies are still saved with
`onboarding_ok=false`never throw away an account we already logged in.

Config: RAILWAY_* env keys (hub .env maps shared → RAILWAY_*).
Run:    python -m jobs run railway -- -n 5 -c 1 -y
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ── Paths / env bootstrap ─────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent          # farms/railway
_HUB = _ROOT.parent.parent                       # Automation/
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)   # hub env wins; farm fills gaps
except ImportError:
    _env_path = _ROOT / ".env"
    if _env_path.is_file():
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed. pip install camoufox[geoip]", flush=True)
    sys.exit(1)

sys.path.insert(0, str(_ROOT))
try:
    import graphql as G  # noqa: E402  (farms/railway/graphql.py)
except Exception:  # pragma: no cover - only if the module is broken
    G = None  # type: ignore[assignment]


# ── Config ────────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


HEADLESS = _env_bool("RAILWAY_HEADLESS", True)
CONCURRENT = max(1, _env_int("RAILWAY_CONCURRENT", 1))
ACCOUNT_TIMEOUT_S = max(120, _env_int("RAILWAY_ACCOUNT_TIMEOUT", 420))
GOOGLE_STEP_TIMEOUT_S = max(20, _env_int("RAILWAY_GOOGLE_STEP_TIMEOUT", 45))
CHECK_EXIT_IP = _env_bool("RAILWAY_CHECK_EXIT_IP", True)
EXIT_IP_TIMEOUT = max(5.0, _env_float("RAILWAY_EXIT_IP_TIMEOUT", 20.0))
CARTETHYIA_INJECT = _env_int("RAILWAY_CARTETHYIA_INJECT", 0) > 0

# Google account pool: one `email|password` (also email:password / tab) per line.
GOOGLE_ACCOUNTS_FILE = Path(
    _env("RAILWAY_GOOGLE_ACCOUNTS", str(_ROOT / "google_accounts.txt"))
)

# Proxy pool (sticky: 1 account = 1 egress for browser + all post-auth HTTP).
PROXY_FILE = Path(_env("RAILWAY_PROXY_FILE", str(_ROOT / "proxy.txt")))
PROXY_SHUFFLE = _env_bool("RAILWAY_PROXY_SHUFFLE", False)

# Results
RESULTS_ROOT = Path(_env("RAILWAY_RESULTS_DIR", str(_ROOT / "results")))
SCREENSHOT_DIR = Path(_env("RAILWAY_SCREENSHOT_DIR", str(_ROOT / "screenshots")))
USED_GOOGLE_FILE = Path(
    _env("RAILWAY_GOOGLE_USED_FILE", str(RESULTS_ROOT / "used_google.txt"))
)
GOOGLE_DEAD_FILE = Path(
    _env("RAILWAY_GOOGLE_DEAD_FILE", str(RESULTS_ROOT / "google_dead.txt"))
)
GLOBAL_CREDS_TXT = Path(
    _env("RAILWAY_GLOBAL_CREDS", str(RESULTS_ROOT / "all_credentials.txt"))
)

# WARP
WARP_EVERY_N = max(0, int(_env("RAILWAY_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0"))
WARP_SETTLE_S = max(3.0, _env_float("WARP_SETTLE_AFTER", "8"))

# Railway endpoints / OAuth constants (proven in the HAR, see README.md).
RAILWAY_BACKBOARD = "https://backboard.railway.com"
COOKIE_NAMES = ("rw.session", "rw.session.sig", "rw.authenticated", "rw.authenticated.sig")

CREDS_HEADER = "# google_email|google_password|cookie|user_id|workspace_id|plan|created_at"

RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ── Logging ──────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(attempt: int, step: str, message: str, email: str = "") -> None:
    """Hub contract: [HH:MM:SS] [<n>] <step>  <msg>  <email>"""
    suffix = f"  {email}" if email else ""
    print(f"[{_ts()}] [{attempt}] {step}  {message}{suffix}", flush=True)


def slog(tag: str, message: str) -> None:
    print(f"[{_ts()}] [{tag}] {message}", flush=True)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── WARP (hub injects WARP_EVERY_N; farm rotates in-process after N OKs) ───────
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
    """Emails already farmed (used file + every batch accounts.json)."""
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
    if email:
        _append(USED_GOOGLE_FILE, email.lower())


def _mark_google_dead(email: str, reason: str) -> None:
    if not email:
        return
    _persist_used_google(email)
    _append(GOOGLE_DEAD_FILE, f"{email.lower()}\t{reason[:160]}")


def _is_access_denied(msg: str) -> bool:
    low = (msg or "").lower()
    return "access_denied" in low or "access denied" in low


# ── Proxy pool ────────────────────────────────────────────────────────────────
def _normalize_proxy_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    return f"http://{raw}"


def _parse_proxy_entry(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    opt_id = ""
    if "#" in line and "://" in line.split("#", 1)[0]:
        line, opt_id = line.rsplit("#", 1)
        line, opt_id = line.strip(), opt_id.strip()
    url = _normalize_proxy_url(line)
    if not url:
        return None
    u = urlparse(url)
    if not u.hostname:
        return None
    return url, opt_id or ""


def _load_proxy_pool_local() -> list[tuple[str, str]]:
    """Parse RAILWAY_PROXY_FILE (and RAILWAY_PROXY_POOL) into [(url, id)]."""
    entries: list[tuple[str, str]] = []
    if PROXY_FILE.is_file():
        for raw in PROXY_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            ent = _parse_proxy_entry(raw)
            if ent:
                entries.append(ent)
    for part in _env("RAILWAY_PROXY_POOL").split(","):
        ent = _parse_proxy_entry(part.strip())
        if ent:
            entries.append(ent)
    if PROXY_SHUFFLE:
        random.shuffle(entries)
    return entries


def _parse_proxy(proxy_url: str) -> dict[str, str]:
    u = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    conf: dict[str, str] = {
        "server": f"{u.scheme}://{u.hostname}:{u.port or (1080 if u.scheme.startswith('socks') else 8080)}"
    }
    if u.username:
        conf["username"] = u.username
    if u.password:
        conf["password"] = u.password
    return conf


def _probe_exit_ip(proxy_url: str) -> str:
    """Exit IP as Cloudflare sees it through `proxy_url`. Fail-soft ('' on error).

    Measured against `backboard.railway.com/cdn-cgi/trace` — the very host the
    farm posts GraphQL to, so it reports the IP Railway actually sees.

    Why not an IPv4-only echo like api.ipify.org: WARP reuses IPv4 anycast exits
    but hands out a distinct IPv6 per registration, so an IPv4-only probe makes
    a healthy multi-warp pool look full of duplicates (this is the same reason
    farms/enter-v3-google uses /cdn-cgi/trace).

    NOTE: `https://railway.com/cdn-cgi/trace` returns 404 — only the backboard
    host serves it.
    """
    try:
        import httpx

        kwargs: dict[str, Any] = {
            "timeout": EXIT_IP_TIMEOUT,
            "headers": {"User-Agent": "Mozilla/5.0"},
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        resp = httpx.get(f"{RAILWAY_BACKBOARD}/cdn-cgi/trace", **kwargs)
        if resp.status_code != 200:
            return ""
        for line in resp.text.splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
    except Exception:
        return ""
    return ""


def _audit_proxy_pool(pool: list[tuple[str, str]]) -> None:
    """Log exit IP per proxy and flag duplicates (WARP anycast reuses exits)."""
    if not pool or not CHECK_EXIT_IP:
        return
    from concurrent.futures import ThreadPoolExecutor

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
    for ip, tags in {ip: t for ip, t in seen.items() if len(t) > 1}.items():
        slog("PROXY", f"WARN duplicate exit {ip} shared by {len(tags)} ports: {', '.join(tags)}")
    slog(
        "PROXY",
        f"pool={len(pool)} reachable={len(ips) - ips.count('')} unique_exit_ips={len(seen)}",
    )


class ProxyPicker:
    """Round-robin, one proxy per account (sticky for the whole account)."""

    def __init__(self, pool: list[tuple[str, str]]) -> None:
        self._pool = list(pool)
        self._idx = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._pool)

    async def next(self) -> tuple[str | None, str]:
        async with self._lock:
            if not self._pool:
                return None, ""
            url, pid = self._pool[self._idx % len(self._pool)]
            self._idx += 1
            return url, pid


# ── Browser ───────────────────────────────────────────────────────────────────
async def launch_browser(proxy_url: str | None):
    host = (urlparse(proxy_url).hostname or "") if proxy_url else ""
    local_proxy = host in ("127.0.0.1", "localhost", "::1")
    # Pass a literal proxy IP to skip Camoufox's 5s geoip preflight; local
    # bridges disable geoip entirely (preflight cannot see the upstream IP).
    geoip: Any = True
    if proxy_url:
        try:
            import ipaddress

            geoip = str(ipaddress.ip_address(host))
        except ValueError:
            geoip = False if local_proxy else True
        if local_proxy:
            geoip = False
    kwargs: dict[str, Any] = {
        "args": ["--ignore-certificate-errors"],
        "headless": HEADLESS,
        "humanize": 0.5,
        "os": random.choice(["windows", "macos"]),
        "locale": "en-US",
        "geoip": geoip,
        "block_webrtc": True,
    }
    if proxy_url:
        kwargs["proxy"] = _parse_proxy(proxy_url)
    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(60000)
    return manager, browser, page


async def screenshot(page, attempt: int, tag: str) -> None:
    try:
        path = SCREENSHOT_DIR / f"railway_{attempt}_{tag}.png"
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


# ── Google sign-in helpers (ported from farms/enter-v3-google) ─────────────────
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
# Google's NORMAL password page lives at /v3/signin/challenge/pwd, so "challenge"
# in the URL is NOT a 2FA signal, only these second-factor subtypes are.
_GOOGLE_2FA_MARKERS = (
    "challenge/ipp",        # phone number prompt
    "challenge/totp",       # authenticator app
    "challenge/az",         # account recovery
    "challenge/dp",         # device prompt
    "challenge/iap",        # in-app prompt
    "challenge/sk",         # security key
    "challenge/pk",         # passkey
    "challenge/recaptcha",  # reCAPTCHA gate
    "challenge/ootp",       # offline one-time passcode
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
    h = _host_of(u)
    return h.endswith("google.com") or h.endswith("google.co.id")


def _on_railway(u: str) -> bool:
    return _host_of(u).endswith("railway.com")


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


async def _fill_google_field(
    page, selectors, value: str, attempt: int, label: str, timeout: float | None = None
) -> bool:
    """Type into the first visible matching field, POLLING until timeout.

    Google renders each interstitial asynchronously, so the field often does not
    exist yet when the URL already changed. A single immediate attempt makes the
    run die with "could not fill google password" while the page is still loading.
    """
    deadline = time.monotonic() + max(1.0, timeout if timeout is not None else GOOGLE_STEP_TIMEOUT_S)
    while True:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click(timeout=4000)
                await loc.fill("")
                await loc.type(value, delay=random.randint(25, 60))
                _log(attempt, "google_fill", f"{label} filled via {sel}")
                return True
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def _click_google_next(page, attempt: int) -> None:
    if await _click_first(page, _GOOGLE_NEXT_SEL, timeout=5000):
        return
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


async def _handle_google_step(page, attempt: int, url: str) -> str:
    """One interstitial decision on accounts.google.com. Returns an action tag."""
    if "speedbump/workspacetermsofservice" in url:
        _log(attempt, "google_tos", "accepting workspace TOS")
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
        _log(attempt, "oauth_consent", "handling consent")
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        if await _click_first(page, _CONSENT_SEL, timeout=4000, scroll=True):
            return "consent"
        # JS fallback across localized labels ("Lanjutkan" / "Izinkan").
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


# ── Railway OAuth entry ───────────────────────────────────────────────────────
def build_login_entry_url() -> str:
    """`/login/google?state=<b64>`base64 (standard, padded) of the short state.

    The state only carries next/posthogSessionId/attribution; Railway re-encodes
    it (adding referrer/redirectUrl/nonce) when it 302s to Google, and keeps the
    PKCE verifier server-side in the `rw.code_verifier` cookie.
    """
    inner = (
        "next=%2Fdashboard"
        f"&posthogSessionId={uuid.uuid4()}"
        "&attribution=%7B%22referringDomain%22%3A%22%24direct%22%2C%22landingPath%22%3A%22%2F%22%7D"
    )
    state = base64.b64encode(inner.encode("utf-8")).decode("ascii")
    return f"{RAILWAY_BACKBOARD}/login/google?state={state}"


async def _collect_cookies(context) -> dict[str, str]:
    """Filter the rw.* session cookies out of the browser context.

    The four cookies live on two hosts (rw.session* host-only on
    backboard.railway.com, rw.authenticated* on railway.com); the same value is
    sent to both, so a name-keyed map is enough.
    """
    out: dict[str, str] = {}
    try:
        cookies = await context.cookies()
    except Exception:
        return out
    for ck in cookies:
        name = ck.get("name") or ""
        if name in COOKIE_NAMES and ck.get("value"):
            out.setdefault(name, str(ck["value"]))
    return out


def cookie_header(cookies: dict[str, str]) -> str:
    """`rw.session=..; rw.session.sig=..; rw.authenticated=true; rw.authenticated.sig=..`"""
    return "; ".join(f"{n}={cookies[n]}" for n in COOKIE_NAMES if cookies.get(n))


# ── Railway post-auth (cookie GraphQL) ────────────────────────────────────────
def _post_auth(cookie: str, proxy_url: str | None, attempt: int, email: str) -> dict[str, Any]:
    """Run the onboarding GraphQL sequence. Returns a fail-soft meta dict."""
    meta: dict[str, Any] = {
        "onboarding_ok": False,
        "user_id": "",
        "workspace_id": "",
        "plan": "",
        "free_plan_balance": None,
        "errors": [],
    }
    if G is None:
        meta["errors"].append("graphql module unavailable")
        return meta

    def log(msg: str) -> None:
        _log(attempt, "graphql", msg, email)

    # 1) me → identity + workspace id + plan
    resp = G.railway_gql(cookie, "me", proxy=proxy_url, log=log)
    me = G.gql_data(resp).get("me") if resp else None
    if isinstance(me, dict):
        meta["user_id"] = str(me.get("id") or "")
        ws = me.get("workspaces")
        if isinstance(ws, list) and ws and isinstance(ws[0], dict):
            meta["workspace_id"] = str(ws[0].get("id") or "")
            meta["plan"] = str(ws[0].get("plan") or "")
    else:
        err = G.error_text(resp) if resp else "no response"
        meta["errors"].append(f"me: {err or 'empty'}")
        log(f"me failed ({err or 'empty'}), keeping cookies anyway")

    # 2) accept terms (mutation, no variables)
    resp = G.railway_gql(cookie, "userTermsUpdate", proxy=proxy_url, log=log)
    if not resp or not G.gql_data(resp).get("userTermsUpdate"):
        meta["errors"].append(f"userTermsUpdate: {G.error_text(resp) or 'no data'}")
    else:
        log("terms accepted (userTermsUpdate)")

    # 3) fair use agreement
    resp = G.railway_gql(cookie, "fairUseAgree", {"agree": True}, proxy=proxy_url, log=log)
    if not resp or G.gql_data(resp).get("fairUseAgree") is not True:
        meta["errors"].append(f"fairUseAgree: {G.error_text(resp) or 'not true'}")
    else:
        log("fair use agreed (fairUseAgree)")

    ws_id = meta["workspace_id"]
    if ws_id:
        # 4) workspace → plan verification (also carries isTrialing/billing state)
        resp = G.railway_gql(cookie, "workspace", {"workspaceId": ws_id}, proxy=proxy_url, log=log)
        ws_obj = G.gql_data(resp).get("workspace") if resp else None
        if isinstance(ws_obj, dict):
            cust = ws_obj.get("customer") if isinstance(ws_obj.get("customer"), dict) else {}
            if cust:
                meta["is_trialing"] = bool(cust.get("isTrialing"))
        else:
            meta["errors"].append(f"workspace: {G.error_text(resp) or 'no data'}")

        # 5) freePlanBalance → remaining credits + trial days (optional, nice to keep)
        resp = G.railway_gql(
            cookie, "freePlanBalance", {"workspaceId": ws_id}, proxy=proxy_url, log=log
        )
        ws_obj = G.gql_data(resp).get("workspace") if resp else None
        if isinstance(ws_obj, dict) and isinstance(ws_obj.get("customer"), dict):
            meta["free_plan_balance"] = ws_obj["customer"]
            bal = ws_obj["customer"]
            log(
                "plan balance: remaining="
                f"{bal.get('remainingUsageCreditBalance')} trial_days={bal.get('trialDaysRemaining')}"
            )
        else:
            meta["errors"].append(f"freePlanBalance: {G.error_text(resp) or 'no data'}")

    meta["onboarding_ok"] = not meta["errors"]
    return meta


def _inject_cartethyia(workspace_id: str, email: str, attempt: int) -> None:
    """Optional Cartethyia push.

    Railway accounts are cookie-only (no `ek_` API key), so the enterconverge
    injector does not apply; this stays an explicit, logged no-op so the env key
    is honest instead of silently doing nothing.
    """
    if not CARTETHYIA_INJECT:
        return
    _log(
        attempt,
        "cartethyia",
        "skipped: Railway has no ek_ API key (cookie-only session)",
        email,
    )


# ── One account ───────────────────────────────────────────────────────────────
async def _do_railway_flow(
    page, attempt: int, email: str, password: str, proxy_url: str | None
) -> dict[str, Any]:
    """Browser half: OAuth login → landing on railway.com. Returns cookies + url."""
    entry = build_login_entry_url()
    _log(attempt, "navigate", "backboard.railway.com/login/google (OAuth entry)", email)
    await page.goto(entry, wait_until="domcontentloaded", timeout=60000)

    # Railway 302s straight to Google; wait for the hand-off.
    deadline = time.monotonic() + GOOGLE_STEP_TIMEOUT_S
    while time.monotonic() < deadline:
        if _is_google_url(page.url):
            break
        await asyncio.sleep(0.4)
    if not _is_google_url(page.url):
        raise RuntimeError(f"did not reach accounts.google.com (url={page.url[:90]})")
    _log(attempt, "google_authorize", "at accounts.google.com", email)

    # Email → Next
    if not await _fill_google_field(page, _GOOGLE_EMAIL_SEL, email, attempt, "email"):
        raise RuntimeError("could not fill google email")
    await asyncio.sleep(0.6)
    await _click_google_next(page, attempt)
    await asyncio.sleep(2.5)

    # Drive interstitials until we are back on railway.com.
    pw_done = False
    outer_deadline = time.monotonic() + max(90.0, GOOGLE_STEP_TIMEOUT_S * 4)
    while time.monotonic() < outer_deadline:
        u = page.url or ""
        if "error=access_denied" in u:
            raise RuntimeError("access_denied at callback (Google denied the request)")
        if _on_railway(u) and not _is_google_url(u):
            break

        if _is_google_url(u):
            if "pwd" in u:  # /v3/signin/challenge/pwd, NORMAL password page
                if pw_done:
                    await asyncio.sleep(1.0)
                    continue
                pw_done = True
                _log(attempt, "google_password", "entering password", email)
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

    if "error=access_denied" in (page.url or ""):
        raise RuntimeError("access_denied at callback")
    if not _on_railway(page.url or ""):
        raise RuntimeError(f"no railway redirect (url={page.url[:90]})")

    # Let the callback 302 → railway.com/new settle and the session cookies land.
    _log(attempt, "auth_settle", f"url={page.url[:80]}", email)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    for _ in range(20):
        if _on_railway(page.url or "") and "/login/google" not in (page.url or ""):
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(1.5)

    cookies = await _collect_cookies(page.context)
    if not cookies.get("rw.session"):
        raise RuntimeError(f"session cookie missing (have: {', '.join(cookies) or 'none'})")

    return {"cookies": cookies, "landed_url": page.url, "cookie_header": cookie_header(cookies)}


async def farm_one_account(
    attempt: int, google_email: str, google_password: str, picker: ProxyPicker
) -> dict[str, Any] | None:
    """Full Railway signup for one Google account. Returns result dict or None."""
    _log(attempt, "start", "railway google oauth", google_email)

    proxy_url, proxy_id = await picker.next()
    if proxy_url and CHECK_EXIT_IP:
        exit_ip = await asyncio.to_thread(_probe_exit_ip, proxy_url)
        _log(attempt, "egress", f"ip={exit_ip or 'unreachable'} via {proxy_id or proxy_url[:40]}")
    elif not proxy_url:
        _log(attempt, "egress", "direct (local IP)")

    manager = browser = page = None
    try:
        manager, browser, page = await launch_browser(proxy_url)
        try:
            flow = await asyncio.wait_for(
                _do_railway_flow(page, attempt, google_email, google_password, proxy_url),
                timeout=ACCOUNT_TIMEOUT_S,
            )
        finally:
            # Free the browser before the (slower) HTTP post-auth phase.
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
                browser = None
            if manager is not None:
                try:
                    await manager.__aexit__(None, None, None)
                except Exception:
                    pass
                manager = None

        cookie = flow["cookie_header"]
        _log(attempt, "cookies", f"captured {len(flow['cookies'])} rw.* cookies", google_email)

        # Post-auth over HTTP, same egress as the browser.
        _log(attempt, "onboarding", "userTermsUpdate / fairUseAgree / me / workspace", google_email)
        meta = await asyncio.to_thread(_post_auth, cookie, proxy_url, attempt, google_email)

        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if meta["onboarding_ok"]:
            _log(
                attempt, "OK",
                f"ws={meta['workspace_id']} plan={meta['plan'] or '?'} user={meta['user_id']}",
                google_email,
            )
        else:
            # Cookies are still valid and worth keeping, onboarding is best-effort.
            _log(
                attempt, "OK",
                f"onboarding_ok=false ({'; '.join(meta['errors'])[:120]})",
                google_email,
            )

        result = {
            "google_email": google_email,
            "google_password": google_password,
            "cookie": cookie,
            "user_id": meta["user_id"],
            "workspace_id": meta["workspace_id"],
            "plan": meta["plan"],
            "created_at": created_at,
            "onboarding_ok": meta["onboarding_ok"],
            "onboarding_errors": meta["errors"],
            "free_plan_balance": meta["free_plan_balance"],
            "is_trialing": meta.get("is_trialing"),
            "landed_url": flow["landed_url"],
            "proxy": proxy_url or "direct",
            "attempt": attempt,
        }
        return result

    except asyncio.TimeoutError:
        if browser is not None and page is not None:
            await screenshot(page, attempt, "timeout")
        _log(attempt, "fail", f"account timeout after {ACCOUNT_TIMEOUT_S}s", google_email)
        return None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        try:
            if browser is not None and page is not None:
                await screenshot(page, attempt, "error")
        except Exception:
            pass
        if _is_access_denied(msg):
            _log(attempt, "fail", "access_denied (Google refused; marking dead, no retry)", google_email)
            _mark_google_dead(google_email, msg)
        else:
            _log(attempt, "fail", msg, google_email)
        return None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


# ── Batch / results ───────────────────────────────────────────────────────────
def init_batch(count: int, concurrent: int, pool_size: int) -> tuple[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = secrets.token_hex(3)
    batch_id = f"{stamp}_{short}"
    batch_dir = RESULTS_ROOT / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "accounts.json").write_text("[]\n", encoding="utf-8")
    (batch_dir / "credentials.txt").write_text(CREDS_HEADER + "\n", encoding="utf-8")
    meta = {
        "batch_id": batch_id,
        "variant": "railway",
        "auth": "google-oauth2",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": count,
        "concurrent": concurrent,
        "proxy_pool_size": pool_size,
    }
    (batch_dir / "batch_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    if not GLOBAL_CREDS_TXT.exists():
        GLOBAL_CREDS_TXT.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_CREDS_TXT.write_text(CREDS_HEADER + "\n", encoding="utf-8")
    slog("BATCH", f"id={batch_id} dir={batch_dir}")
    return batch_id, batch_dir


_results_lock = asyncio.Lock()


async def save_result(batch_dir: Path, result: dict[str, Any]) -> None:
    async with _results_lock:
        accounts_file = batch_dir / "accounts.json"
        try:
            data = json.loads(accounts_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        except Exception:
            data = []
        data.append(result)
        accounts_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        row = "|".join(
            str(x) for x in (
                result.get("google_email", ""),
                result.get("google_password", ""),
                result.get("cookie", ""),
                result.get("user_id", ""),
                result.get("workspace_id", ""),
                result.get("plan", ""),
                result.get("created_at", ""),
            )
        )
        _append(batch_dir / "credentials.txt", row)
        _append(GLOBAL_CREDS_TXT, row)
        _persist_used_google(result.get("google_email", ""))
        slog("SAVE", f"{result.get('google_email', '')} ws={result.get('workspace_id', '')} -> {batch_dir}")


# ── CLI / main ────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Railway Google-OAuth farm")
    p.add_argument("-n", "--count", type=int, default=1, help="accounts this run")
    p.add_argument("-c", "--concurrent", type=int, default=1, help="parallel workers")
    p.add_argument("-y", "--yes", action="store_true", help="non-interactive")
    p.add_argument("--headless", action="store_true", help="force headless")
    p.add_argument("--headed", action="store_true", help="force headed")
    return p.parse_args(argv)


async def run_farm(count: int, concurrent: int) -> int:
    global CONCURRENT
    CONCURRENT = max(1, concurrent)

    all_accounts = load_google_accounts()
    if not all_accounts:
        print(
            f"[FATAL] No Google accounts found in {GOOGLE_ACCOUNTS_FILE}. "
            "Create google_accounts.txt (one email|password per line).",
            flush=True,
        )
        return 1

    used = _load_used_google()
    available = [(e, p) for e, p in all_accounts if e.lower() not in used]
    if not available:
        print(
            f"[FATAL] All {len(all_accounts)} Google accounts already used "
            f"({USED_GOOGLE_FILE}). Add more to {GOOGLE_ACCOUNTS_FILE}.",
            flush=True,
        )
        return 1

    actual = min(count, len(available))
    if actual < count:
        print(f"[WARN] only {actual} unused accounts available (requested {count})", flush=True)

    pool = _load_proxy_pool_local()
    if pool:
        _audit_proxy_pool(pool)
    else:
        slog("PROXY", "no proxy pool -> every account egresses from the local IP")

    picker = ProxyPicker(pool)
    batch_id, batch_dir = init_batch(actual, CONCURRENT, len(pool))
    slog(
        "CFG",
        f"headless={HEADLESS} concurrency={CONCURRENT} "
        f"warp_every_n={_effective_warp_every_n() or 'off'} "
        f"google_pool={len(available)}/{len(all_accounts)} proxy_pool={len(pool)}",
    )

    sem = asyncio.Semaphore(CONCURRENT)
    ok_count = 0
    fail_count = 0

    async def worker(idx: int, email: str, password: str) -> None:
        nonlocal ok_count, fail_count
        async with sem:
            try:
                result = await farm_one_account(idx, email, password, picker)
            except Exception as e:
                _log(idx, "fail", f"worker crash: {type(e).__name__}: {e}", email)
                result = None
            if result:
                await save_result(batch_dir, result)
                ok_count += 1
                await _maybe_warp_after_success()
            else:
                fail_count += 1

    tasks = []
    for i, (email, password) in enumerate(available[:actual], 1):
        tasks.append(asyncio.create_task(worker(i, email, password)))
        if i < actual:
            await asyncio.sleep(random.uniform(1.5, 3.0))
    await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n[{_ts()}] [DONE] ok={ok_count} fail={fail_count} batch={batch_id}", flush=True)
    print(f"[{_ts()}] [DONE] results: {batch_dir}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    global HEADLESS
    args = parse_args(argv)
    if args.headless:
        HEADLESS = True
    if args.headed:
        HEADLESS = False

    if not args.yes:
        print(f"  Railway farm: {args.count} accounts, concurrent={args.concurrent}")
        confirm = input("  Start? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.", flush=True)
            return 0

    print(f"[railway] plan: n={args.count} c={args.concurrent}", flush=True)
    return asyncio.run(run_farm(args.count, args.concurrent))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
