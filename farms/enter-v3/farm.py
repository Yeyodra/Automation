#!/usr/bin/env python3
"""
Enter/Converge farm — v3 (fresh path).

Auth   : full BROWSER flow, reused verbatim from ../enter/farm.py
         (Camoufox: gift landing -> CTA -> FPJS -> risk-session -> Auth0
          identifier+captcha -> OTP -> password -> callback -> gateway JWT).
Post-auth: pure HTTP v3 (postauth_v3.py), reversed from live HARs:
         referral/claim -> users/info -> workspaces -> onboarding/config (v2|v3)
         -> onboarding/complete (3-field) -> api-keys.

Why hybrid: the pre-auth anti-bot (Cloudflare, Turnstile, FingerprintJS,
risk-session<->state binding) only survives a real browser; post-auth is clean
REST and is done over urllib. See README.md.

Hub contract: CLI -n -c -y, line logs, one OK line per account, hub venv only.
Legacy farm stays untouched at ../enter/ as the reference/fallback.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

# ── Hub path + env bootstrap (mirror legacy farm) ─────────────────────────────
_ROOT = Path(__file__).resolve().parent          # farms/enter-v3
_HUB = _ROOT.parent.parent                        # Automation/
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_HUB / ".env", override=False)        # hub wins
load_dotenv(_ROOT / ".env", override=False)       # farm-local gaps only

# Load legacy farm by explicit file path under a private name: both files are
# named farm.py, so a plain `import farm` would alias this module instead.
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

sys.path.insert(0, str(_ROOT))
import postauth_v3 as P  # noqa: E402

# Override legacy click_text_button: its "Continue" regex also matches Auth0's
# "Continue with Google"/"Continue with Apple", so the browser fell into
# accounts.google.com instead of submitting the email form. Here the primary
# submit (data-action-button-primary / value=default) always wins, and social
# providers are excluded outright.
_SOCIAL_RE = re.compile(r"google|apple|facebook|microsoft|github|continue with|sign in with", re.I)


async def _safe_click_text_button(page, keywords):
    for kw in keywords:
        try:
            loc = page.get_by_role("button", name=re.compile(kw, re.I))
            for i in range(min(await loc.count(), 8)):
                b = loc.nth(i)
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _SOCIAL_RE.search(txt):
                    continue
                try:
                    if await b.get_attribute("data-provider"):
                        continue
                    if (await b.get_attribute("data-action-button-secondary")) == "true":
                        continue
                except Exception:
                    pass
                await b.click()
                return txt or kw
        except Exception:
            pass
    for kw in keywords:
        try:
            loc = page.locator(
                f"button:has-text('{kw}'), [role=button]:has-text('{kw}')"
            )
            for i in range(min(await loc.count(), 8)):
                b = loc.nth(i)
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _SOCIAL_RE.search(txt):
                    continue
                try:
                    if await b.get_attribute("data-provider"):
                        continue
                except Exception:
                    pass
                await b.click()
                return txt or kw
        except Exception:
            continue
    try:
        primary = page.locator(
            "button[data-action-button-primary='true']:not([aria-hidden='true'])"
        )
        if await primary.count() > 0 and await primary.first.is_visible():
            txt = ((await primary.first.inner_text()) or "").strip()
            await primary.first.click()
            return txt or "primary-submit"
    except Exception:
        pass
    return None


L.click_text_button = _safe_click_text_button


# The page ALSO renders a honeypot submit (opacity:0, pointer-events:none,
# aria-hidden=true, tabindex=-1) that still matches the naive
# [name=action][value=default] selector and for which Playwright reports
# is_visible()=True. Clicking it hangs in actionability checks and never
# submits. So: match ONLY data-action-button-primary / _button-signup-id and
# re-verify with computed style (opacity / pointer-events / rect / disabled).
_PRIMARY_SEL = (
    "button[data-action-button-primary='true'], "
    "button._button-signup-id"
)


async def _primary_candidates(page):
    try:
        loc = page.locator(_PRIMARY_SEL)
        n = await loc.count()
    except Exception:
        return []
    out = []
    for i in range(min(n, 6)):
        b = loc.nth(i)
        try:
            info = await b.evaluate(
                """el => {
                    const cs = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return {
                        hidden: el.getAttribute('aria-hidden'),
                        op: cs.opacity, pe: cs.pointerEvents,
                        w: r.width, h: r.height, dis: !!el.disabled,
                    };
                }""",
                timeout=8000,
            )
        except Exception:
            continue
        if not isinstance(info, dict):
            continue
        if info.get("hidden") == "true":
            continue
        try:
            if float(info.get("op") or "1") <= 0:
                continue
        except Exception:
            pass
        if (info.get("pe") or "") == "none":
            continue
        if (info.get("w") or 0) <= 0 or (info.get("h") or 0) <= 0:
            continue
        if info.get("dis"):
            continue
        out.append(b)
    return out


async def _primary_submit(page, attempt, timeout: float = 25.0) -> bool:
    """Click the REAL Continue and confirm the page actually submits.

    Strategies in order: Enter key (implicit submission from the filled input:
    no hit-test, bypasses overlays; proven 3/3 on the password page) ->
    native click -> el.click() -> form.requestSubmit(btn).
    Returns True only when the URL moves; otherwise retries until timeout.
    """
    start = (page.url or "").split("?")[0]
    deadline = time.monotonic() + timeout
    round_no = 0
    while time.monotonic() < deadline:
        round_no += 1
        try:
            cur = (page.url or "").split("?")[0]
        except Exception:
            cur = start
        if cur != start:
            alog(attempt, "already navigated, submit done")
            return True
        cands = await _primary_candidates(page)
        if not cands:
            await asyncio.sleep(0.5)
            continue
        if round_no > 1:
            has_tok, mount = await _captcha_state(page)
            if mount and not has_tok:
                alog(attempt, "captcha appeared late, waiting token")
                await _wait_captcha_token(page, timeout=30, attempt=attempt, label="late")
        b = cands[0]
        dispatched = ""
        errs: list[str] = []
        for mode in ("enter", "click", "js", "requestSubmit"):
            try:
                if mode == "enter":
                    try:
                        inp = page.locator(
                            'form[data-form-primary="true"] input:not([type="hidden"])'
                        ).first
                        await inp.focus(timeout=3000)
                        await inp.press("Enter", timeout=3000)
                    except Exception:
                        await page.keyboard.press("Enter")
                elif mode == "click":
                    try:
                        await b.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    await b.click(timeout=5000)
                elif mode == "js":
                    await b.evaluate("(el) => el.click()")
                else:
                    await b.evaluate(
                        "(el) => { const f = el.form || el.closest('form');"
                        " if (f && f.requestSubmit) { f.requestSubmit(el); }"
                        " else if (f) { f.submit(); } }"
                    )
                dispatched = mode
                alog(attempt, f"submit trying {mode}")
                break
            except Exception as e:
                errs.append(f"{mode}:{type(e).__name__}")
                continue
        if not dispatched:
            alog(attempt, f"submit round {round_no} failed [{','.join(errs)}] url={cur[:70]}")
            await asyncio.sleep(0.5)
            continue
        for _ in range(12):  # ~6s to observe navigation (accept = 1-3s)
            try:
                u = (page.url or "").split("?")[0]
            except Exception:
                u = start
            if u != start:
                alog(attempt, f"submit ok via {dispatched}")
                return True
            await asyncio.sleep(0.5)
        alog(attempt, f"submit via {dispatched} did not navigate, retrying")
    return False


async def _captcha_state(page):
    try:
        st = await page.evaluate(
            """() => {
                const tok = document.querySelector('input[name="captcha"], [name="cf-turnstile-response"]');
                const hasTok = !!(tok && tok.value && tok.value.length > 20);
                const mount = !!(document.querySelector(
                    '[data-sitekey], .cf-turnstile, #cf-turnstile, [name="cf-turnstile-response"],'
                    ' #ulp-auth0-v2-captcha, div[data-captcha-provider],'
                    ' iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'));
                return [hasTok, mount];
            }""",
            timeout=8000,
        )
        return (bool(st[0]), bool(st[1]))
    except Exception:
        return (False, False)


def _on_app_url(u: str) -> bool:
    try:
        return urlparse(u or "").netloc == urlparse(L.APP_HOST).netloc
    except Exception:
        return False


async def _wait_password_ready(page, attempt: int, timeout: float = 150.0) -> bool:
    deadline = time.monotonic() + timeout
    grace_until = time.monotonic() + 10
    last_beat = 0.0
    while time.monotonic() < deadline:
        has_tok, mount = await _captcha_state(page)
        if has_tok:
            return True
        now = time.monotonic()
        if not mount and now >= grace_until:
            alog(attempt, "no captcha on password page, submitting directly")
            return True
        if now - last_beat >= 10:
            last_beat = now
            alog(attempt, f"waiting password token ({max(0.0, deadline - now):.0f}s left)")
        await asyncio.sleep(0.5)
    return False


async def _wait_captcha_token(
    page, timeout: float = 60.0, attempt: int = 0,
    label: str = "captcha", heartbeat: float = 10.0,
) -> bool:
    # The Auth0 submit handler silently preventDefaults when this token is
    # empty, so submitting before it exists only burns the verify windows.
    deadline = time.monotonic() + timeout
    last_beat = 0.0
    while time.monotonic() < deadline:
        try:
            val = await page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="captcha"], [name="cf-turnstile-response"]');
                    return el && el.value ? el.value.length : 0;
                }"""
            )
            if int(val or 0) > 20:
                return True
        except Exception:
            pass
        now = time.monotonic()
        if attempt and heartbeat > 0 and now - last_beat >= heartbeat:
            last_beat = now
            left = max(0, deadline - now)
            alog(attempt, f"waiting {label} token ({left:.0f}s left)")
        await asyncio.sleep(0.5)
    return False


async def _fill_first(page, selectors, value):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            await loc.click(timeout=3000)
            await loc.fill("")
            await loc.type(value, delay=random.randint(20, 45))
            return True
        except Exception:
            continue
    return False


async def _goto_signup_identifier(page, attempt):
    q = {"gift": L.GIFT_CODE, "inviteeReward": L.INVITEE_REWARD}
    if L.INVITER:
        q["inviter"] = L.INVITER
    await L.goto_with_retry(
        page, f"{L.APP_HOST}/?{urlencode(q)}", attempt, label="v3_landing", warp_on_fail=False
    )
    await asyncio.sleep(2.5)
    v, e = await L.extract_fpjs(page, attempt)
    if not (v and e):
        raise RuntimeError("FPJS unavailable - aborting (random ids => access_denied)")
    rs = await asyncio.to_thread(L._get_risk_session_id, L.GIFT_CODE, v, e)
    if not rs:
        raise RuntimeError("risk-session creation failed")
    login_url = f"{L.APP_HOST}/auth/login?{urlencode({'return_to': '/', 'risk_session_id': rs})}"
    await L.goto_with_retry(page, login_url, attempt, label="v3_gateway", warp_on_fail=False)
    for _ in range(60):
        u = page.url or ""
        if "/u/" in u and ("signup" in u or "login" in u):
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
        page, f"{L.AUTH_HOST}/u/signup/identifier?state={state}", attempt,
        label="v3_signup_identifier", warp_on_fail=False,
    )
    await asyncio.sleep(1.5)
    alog(attempt, f"signup identifier ready state={state[:16]}..")
    return state


async def do_signup_v3(page, email_addr, password, attempt):
    """Full browser signup against the v3 Auth0 pages, using selectors reversed
    from the live HAR. Replaces the legacy flow to avoid its hardcoded 13s
    /auth/login race and its "Continue" click that hits the Google button.
    """
    state = await _goto_signup_identifier(page, attempt)

    if not await _fill_first(page, ['input#email', 'input[name="email"]'], email_addr):
        raise RuntimeError("could not fill email on signup identifier")
    alog(attempt, "email filled")
    if not await L.handle_turnstile(page, attempt, max_wait=60, require_token=True, use_global_limit=True):
        raise RuntimeError("Turnstile solve failed before email submit")
    await _wait_captcha_token(page, timeout=30, attempt=attempt, label="email")
    otp_since = time.time()
    if not await _primary_submit(page, attempt):
        raise RuntimeError("signup identifier submit button not found")
    alog(attempt, "email submitted")
    await asyncio.sleep(2.0)
    await _check_domain_block(page, "after_email")

    for i in range(60):
        if "challenge" in page.url or "password" in page.url:
            break
        if i % 20 == 19:
            alog(attempt, f"waiting challenge url={(page.url or '').split('?')[0][:70]}")
        await asyncio.sleep(0.5)
    alog(attempt, f"post-email url={page.url.split('?')[0][:60]}")
    await _check_domain_block(page, "challenge")

    if "password" not in page.url:
        alog(attempt, f"waiting OTP (timeout={L.OTP_TIMEOUT_S}s)")
        code = await _wait_otp(email_addr, otp_since, attempt)
        if not await _fill_first(
            page,
            ['input#code', 'input[name="code"]', 'input[autocomplete="one-time-code"]'],
            code,
        ):
            raise RuntimeError("could not fill OTP code")
        if not await _primary_submit(page, attempt):
            raise RuntimeError("OTP submit button not found")
        alog(attempt, "OTP submitted")
        await asyncio.sleep(2.0)

    for i in range(60):
        if "password" in page.url:
            break
        if i % 20 == 19:
            alog(attempt, f"waiting password url={(page.url or '').split('?')[0][:70]}")
        await asyncio.sleep(0.5)
    if "password" not in page.url:
        raise RuntimeError(f"expected password page, got {page.url.split('?')[0][:70]}")

    if not await _fill_first(page, ['input#password', 'input[name="password"]'], password):
        raise RuntimeError("could not fill password")
    _, pw_mount = await _captcha_state(page)
    if pw_mount:
        await L.handle_turnstile(
            page, attempt, max_wait=10, require_token=False,
            password=password, use_global_limit=True, allow_remount=False,
        )
    else:
        alog(attempt, "no captcha mount, skipping turnstile clicks")
    if not await _wait_password_ready(page, attempt, timeout=150):
        raise RuntimeError("password page never became submittable")
    if not await _primary_submit(page, attempt):
        raise RuntimeError("password submit button not found")
    alog(attempt, "password submitted")
    await asyncio.sleep(2.0)
    await _check_domain_block(page, "after_password")

    # Legacy _is_enter_app_url only matches path EXACTLY "/"; the app lands on
    # /workspace after callback, so any enter.converge.ai page counts as back.
    def _on_app(u: str) -> bool:
        try:
            return urlparse(u or "").netloc == urlparse(L.APP_HOST).netloc
        except Exception:
            return False

    for i in range(120):
        u = page.url or ""
        if "/auth/callback" in u or "code=" in u or L._is_enter_app_url(u) or _on_app(u):
            break
        if i % 20 == 19:
            alog(attempt, f"waiting callback url={u.split('?')[0][:70]}")
        await asyncio.sleep(0.5)
    if await L.page_has_domain_block(page):
        raise RuntimeError("domain_not_allowed: post password")

    u = page.url or ""
    if not (_on_app(u) or "/auth/callback" in u or "code=" in u):
        try:
            await L.wait_url(
                page,
                lambda x: _on_app(x) or "/auth/callback" in x or "code=" in x,
                20,
            )
        except Exception:
            await L.goto_with_retry(page, f"{L.APP_HOST}/", attempt, label="callback_return")

    tokens = await L._fetch_gateway_session(page)
    tokens.update({
        "refresh_token": "", "id_token": "", "token_type": "Bearer",
        "scope": L.SCOPE, "email_from_id": "",
    })
    alog(attempt, "authenticated gateway session established")
    return tokens

# Auth0 sometimes renders /u/login/identifier ("Welcome") instead of
# /u/signup/identifier; that page names the email field "username", which the
# legacy selector list never tried, so the email was left empty.
_EMAIL_EXTRA_SELECTORS = (
    'input[name="username"]',
    'input#username',
)


async def _safe_fill_input(page, selectors, value):
    merged = list(selectors)
    for sel in _EMAIL_EXTRA_SELECTORS:
        if sel not in merged:
            merged.append(sel)
    return await L._legacy_fill_input(page, merged, value)


L._legacy_fill_input = L.fill_input
L.fill_input = _safe_fill_input


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


# ── Farm-specific config ──────────────────────────────────────────────────────
ACCOUNT_GAP = _env_float("ENTER_ACCOUNT_GAP", 30.0)
SPAWN_DELAY = _env_float("ENTER_SPAWN_DELAY", 20.0)
MAX_ACCOUNTS = _env_int("ENTER_MAX_ACCOUNTS", 1)
CONCURRENT = _env_int("ENTER_CONCURRENT", 1)
ACCOUNT_TIMEOUT_S = max(120, _env_int("ENTER_ACCOUNT_TIMEOUT", 600))
POSTAUTH_TIMEOUT_S = max(60, _env_int("ENTER_POSTAUTH_TIMEOUT", 180))

RESULTS_ROOT = Path(_env("ENTER_RESULTS_DIR", str(_ROOT / "results")))
SCREENSHOT_DIR = Path(_env("ENTER_SCREENSHOT_DIR", str(_ROOT / "screenshots")))

GIFT_CODE = _env("ENTER_GIFT_CODE")
INVITER = _env("ENTER_INVITER")
INVITEE_REWARD = _env("ENTER_INVITEE_REWARD", "100")

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

_results_lock = asyncio.Lock()
_vps_pusher = None

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


def init_batch(n: int, c: int) -> str:
    global BATCH_ID, BATCH_DIR, RESULTS_JSON, CREDS_TXT, CREDS_KEYS_TXT
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
            "# email|password|api_key|workspace_id|key_id|key_name|created_at|batch_id"
            "|referral_code|credits_total\n",
            encoding="utf-8",
        )
    if not GLOBAL_KEYS_TXT.exists():
        GLOBAL_KEYS_TXT.write_text(
            "# one api key per line (successful farms only)\n", encoding="utf-8"
        )
    meta = {
        "batch_id": BATCH_ID,
        "variant": "enter-v3",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gift_code": GIFT_CODE,
        "gift_chain": GIFT_CHAIN,
        "max_accounts": n,
        "concurrent": c,
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

        email = result.get("email", "")
        pw = result.get("password", "")
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
        data_obj = {
            "displayName": result.get("email", ""),
            "apiKey": key,
            "testStatus": "active",
            "providerSpecificData": {
                "workspaceId": result.get("workspace_id", ""),
                "email": result.get("email", ""),
            },
            "lastError": None,
            "lastErrorAt": None,
        }
        if tokens.get("access_token"):
            data_obj["accessToken"] = tokens["access_token"]
        cred = make_credential(L.NINEROUTER_PROVIDER, result.get("email", ""), data_obj)
        return bool(_vps_pusher.queue(cred))
    except Exception as e:
        slog("9ROUTER", f"push error: {type(e).__name__}: {e}")
        return False


# ── Domain-block detection that ALSO covers the password step ─────────────────
# domainnotallow.har proved the block surfaces on the PASSWORD step as a 400
# with data-error-code="custom-script-error-code_extensibility_error".
_DOMAIN_BLOCK_ERRCODE = "custom-script-error-code_extensibility_error"
_DOMAIN_BLOCK_MARKERS = (
    "this email domain is not allowed to sign up",
    "email domain is not allowed",
    "domain is not allowed",
    "domain_not_allowed",
    "email provider is not allowed",
    "not allowed to sign up",
)


def _looks_like_domain_block(text: str) -> str | None:
    low = (text or "").lower()
    if _DOMAIN_BLOCK_ERRCODE in low:
        return _DOMAIN_BLOCK_ERRCODE
    for marker in _DOMAIN_BLOCK_MARKERS:
        if marker in low:
            return marker
    return None


async def _check_domain_block(page, where: str) -> None:
    try:
        body = await page.content()
    except Exception:
        body = ""
    marker = _looks_like_domain_block(body)
    if marker:
        raise RuntimeError(f"domain_not_allowed: {marker} ({where})")


# ── Mailbox providers: enowX | dottrick | fluffy | legacy ─────────────────────
EMAIL_PROVIDER = _env("ENTER_EMAIL_PROVIDER", "enowx").lower()
PROVIDER_MODULES = {"enowx": "enowx_mail", "dottrick": "dottrick_mail", "fluffy": "fluffy_mail"}


def _provider_module():
    name = PROVIDER_MODULES.get(EMAIL_PROVIDER)
    if not name:
        raise RuntimeError(
            f"ENTER_EMAIL_PROVIDER={EMAIL_PROVIDER!r} has no adapter "
            f"(expected one of: {', '.join(PROVIDER_MODULES)})"
        )
    return __import__(name)


async def _make_mailbox(attempt: int, slot: int) -> str:
    if EMAIL_PROVIDER in PROVIDER_MODULES:
        return await asyncio.to_thread(_provider_module().create_address)
    return await L.generate_email(worker_slot=slot)


async def _wait_otp(email: str, since_ts: float, attempt: int) -> str:
    if EMAIL_PROVIDER in PROVIDER_MODULES:
        return await asyncio.to_thread(
            _provider_module().wait_for_otp, email,
            since_ts=since_ts - 20, timeout=L.OTP_TIMEOUT_S,
            log=lambda m: alog(attempt, m),
        )
    return await L.wait_otp_imap(email, since_ts=since_ts - 20, attempt=attempt)


# ── One account ───────────────────────────────────────────────────────────────
async def do_account(attempt: int, slot_q: asyncio.Queue, results: list) -> dict | None:
    slot = await slot_q.get()
    email = ""
    proxy_url = proxy_id = None
    manager = _browser = None
    t_start = time.time()
    try:
        emit_progress(attempt, "START", f"slot={slot} acquiring mailbox ({EMAIL_PROVIDER})")
        email = await _make_mailbox(attempt, slot)
        emit_progress(attempt, "START", f"slot={slot}", email)

        password = L.ACCOUNT_PASSWORD
        proxy_url, proxy_id = await L.next_proxy()
        gift = await _chain_claim_gift(attempt)

        if EMAIL_PROVIDER in PROVIDER_MODULES:
            # legacy browser flow awaits wait_otp_imap internally; redirect it.
            mod = _provider_module()

            async def _otp_bridge(addr, since_ts=None, page=None, attempt=0):
                return await asyncio.to_thread(
                    mod.wait_for_otp, addr, since_ts=since_ts,
                    timeout=L.OTP_TIMEOUT_S, log=lambda m: alog(attempt, m),
                )

            L.wait_otp_imap = _otp_bridge

        # _goto_signup_identifier builds the landing URL from L.GIFT_CODE, so the
        # per-account chain code has to be pushed into the legacy module.
        _prev_legacy_gift = L.GIFT_CODE
        L.GIFT_CODE = gift
        try:
            manager, _browser, page = await L.launch_browser(proxy_url)
            # Browser auth (reused verbatim). Emits its own alog diagnostics.
            tokens = await asyncio.wait_for(
                do_signup_v3(page, email, password, attempt),
                timeout=ACCOUNT_TIMEOUT_S,
            )
            # Extra guard: password-step domain block can appear before callback.
            await _check_domain_block(page, "post-signup")
        finally:
            L.GIFT_CODE = _prev_legacy_gift
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

        access_token = tokens.get("access_token", "")
        if not access_token:
            raise RuntimeError("no access token from gateway session")
        alog(attempt, "gateway session established (browser closed)")

        # Post-auth v3 over HTTP (urllib), proxy-affine to the browser egress.
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
            "email": email,
            "password": password,
            "gift_code": gift,
            "referral_code": own_code,
            "credits_total": credits_total,
            "invitee_bonus_landed": bonus_landed,
            "variant": "enter-v3",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempt": attempt,
            "proxy": proxy_url or "direct",
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
        # domain-block / rate-limit side effects
        cat, _stage = L._terminal_category(msg)  # noqa: SLF001
        if cat == "identifier_domain_blocked":
            try:
                L.gptmail_block_domain(
                    email.split("@")[-1] if "@" in email else "",
                    reason=msg[:120], worker_slot=slot,
                )
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
        if ACCOUNT_GAP > 0:
            g = ACCOUNT_GAP + random.uniform(0, min(8.0, ACCOUNT_GAP * 0.3))
            await asyncio.sleep(g)
        slot_q.put_nowait(slot)


# ── CLI ───────────────────────────────────────────────────────────────────────
async def main() -> None:
    import argparse

    global MAX_ACCOUNTS, CONCURRENT, SPAWN_DELAY, ACCOUNT_GAP, _vps_pusher, GIFT_CHAIN

    ap = argparse.ArgumentParser(description="Enter/Converge v3 farmer (hybrid: browser auth + HTTP post-auth)")
    ap.add_argument("-n", "--count", type=int, default=None)
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
        print("ERROR: enter-v3 requires ENTER_AUTH_MODE=browser", flush=True)
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
    elif not GIFT_CODE:
        print("ERROR: set ENTER_GIFT_CODE (referral gift)", flush=True)
        sys.exit(1)

    n = args.count if args.count is not None else MAX_ACCOUNTS
    c = args.concurrent if args.concurrent is not None else CONCURRENT
    n = max(1, n)
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
    slog("CFG", f"variant=enter-v3 mode={L.EMAIL_MODE} headless={L.HEADLESS} "
                f"gift={GIFT_CODE or '-'} chain={'on' if GIFT_CHAIN else 'off'} "
                f"inviter={INVITER or '-'} concurrency={c} "
                f"warp_every_n={every_n or 'off'}")
    if GIFT_CHAIN:
        slog("CHAIN", f"next_gift={_chain_next_gift or GIFT_CODE} state={CHAIN_STATE_FILE}")
    if L.NINEROUTER_VPS_EVERY_N > 0:
        try:
            from core.ninerouter import NinerouterPusher  # type: ignore

            _vps_pusher = NinerouterPusher(provider=L.NINEROUTER_PROVIDER, every_n=L.NINEROUTER_VPS_EVERY_N)
            slog("9ROUTER", f"VPS push enabled every_n={L.NINEROUTER_VPS_EVERY_N} host={_vps_pusher.host}")
        except Exception as e:
            slog("9ROUTER", f"pusher init skipped: {type(e).__name__}: {e}")

    slot_q: asyncio.Queue = asyncio.Queue()
    for s in range(c):
        slot_q.put_nowait(s)

    results: list = []
    tasks = []
    for i in range(1, n + 1):
        tasks.append(asyncio.create_task(do_account(i, slot_q, results)))
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
