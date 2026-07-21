#!/usr/bin/env python3
"""Outlook / Microsoft account farmer — dual mailbox mode.

Modes (OUTLOOK_MAILBOX):
  outlook_com (default) — HAR HTTPToolkit_2026-07-21:
    /signup?… → CheckAvailable @outlook.com (type=Live)
    → password → birth → name → HUMAN hold → CreateAccount
    → NO IMAP OTP (SuggestedAccountType=OUTLOOK)

  easi — older HAR (custom domain):
    signup.live.com → MemberName @catch-all → SendOtt → IMAP OTP
    → birth/name → HUMAN → CreateAccount (type=EASI)

Hub: -n/-c/-y · OUTLOOK_* · WARP disabled for this job · progress OK/fail lines
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from core.mail import (  # noqa: E402
    ImapConfig,
    UsedEmailStore,
    generate_email,
    read_otp_imap,
    extract_microsoft_otp,
)
from core.px_hold import gate_visible, solve_px_hold_on_page  # noqa: E402


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── Config ───────────────────────────────────────────────────────────────────
PASSWORD = _env("OUTLOOK_PASSWORD") or _env("ACCOUNT_PASSWORD") or "ChangeMe1!"
HEADLESS = _env_bool("OUTLOOK_HEADLESS", False)  # headed better for PX hold
MAX_ACCOUNTS = max(1, _env_int("OUTLOOK_MAX_ACCOUNTS", 1))
CONCURRENT = max(1, _env_int("OUTLOOK_CONCURRENT", 1))
SPAWN_DELAY = max(0.0, float(_env("OUTLOOK_SPAWN_DELAY") or "2") or 0.0)
# Pause after each account finishes (OK or fail) before next worker starts
BETWEEN_WAIT = max(0.0, float(_env("OUTLOOK_BETWEEN_WAIT") or "0") or 0.0)
UI = (_env("OUTLOOK_UI") or "log").lower()

# outlook_com = @outlook.com Live (default) | easi = custom domain + IMAP OTP
MAILBOX = (_env("OUTLOOK_MAILBOX") or "outlook_com").lower().replace("-", "_")
if MAILBOX in ("outlook", "live", "outlookcom", "com"):
    MAILBOX = "outlook_com"
if MAILBOX not in ("outlook_com", "easi"):
    MAILBOX = "outlook_com"

EMAIL_MODE = (_env("OUTLOOK_EMAIL_MODE") or "domain").lower()
if EMAIL_MODE not in ("domain", "plus_trick"):
    EMAIL_MODE = "domain"

OTP_TIMEOUT = max(30, _env_int("OUTLOOK_OTP_TIMEOUT", 180))
OTP_FROM = _env("OUTLOOK_OTP_FROM") or "accountprotection.microsoft.com"
OTP_SUBJECT_HINT = _env("OUTLOOK_OTP_SUBJECT") or "Verify your email"
ACCOUNT_TIMEOUT_S = max(120, _env_int("OUTLOOK_ACCOUNT_TIMEOUT", 600))

COUNTRY = (_env("OUTLOOK_COUNTRY") or "ID").upper()
BIRTH_YEAR_MIN = _env_int("OUTLOOK_BIRTH_YEAR_MIN", 1988)
BIRTH_YEAR_MAX = _env_int("OUTLOOK_BIRTH_YEAR_MAX", 2002)

WARP_EVERY_N = max(
    0,
    _env_int("OUTLOOK_WARP_EVERY_N", 0) or _env_int("WARP_EVERY_N", 0),
)
PX_HOLD_MIN = float(_env("OUTLOOK_PX_HOLD_MIN") or "4")
PX_HOLD_MAX = float(_env("OUTLOOK_PX_HOLD_MAX") or "50")
PX_MAX_ATTEMPTS = max(1, _env_int("OUTLOOK_PX_MAX_ATTEMPTS", 3))

RESULTS_ROOT = Path(_env("OUTLOOK_RESULTS_DIR", str(_ROOT / "results")))
USED_EMAILS_FILE = Path(
    _env("OUTLOOK_USED_EMAILS_FILE", str(RESULTS_ROOT / "used_emails.txt"))
)
SCREENSHOT_DIR = Path(_env("OUTLOOK_SCREENSHOT_DIR", str(_ROOT / "screenshots")))
STUB_MODE = _env_bool("OUTLOOK_STUB", False)

# HAR 2026-07-21: OAuth-style /signup shows "New email | @outlook.com" form
# (bare /signup?lic=1 alone often shows plain "Email" EASI-style field)
_CLIENT_ID = "9199bf20-a13f-4107-85dc-02114787ef48"
_COBRAND = "ab0455a0-8d03-46b9-b18b-df2f57b9e44c"


def _default_entry_outlook_com() -> str:
    """Build signup URL that yields Live @outlook.com new-email UI (HAR)."""
    uaid = secrets.token_hex(16)
    # sru = oauth authorize return; opid/opidt session-specific → omit (page mints)
    sru = (
        "https://login.live.com/oauth20_authorize.srf?"
        f"lc=1033&client_id={_CLIENT_ID}&cobrandid={_COBRAND}"
        f"&mkt=EN-US&uaid={uaid}&opignore=1"
    )
    from urllib.parse import quote

    return (
        "https://signup.live.com/signup?"
        f"sru={quote(sru, safe='')}"
        f"&mkt=EN-US&uiflavor=web&lw=1&fl=dob%2cflname%2cwld"
        f"&cobrandid={_COBRAND}&client_id={_CLIENT_ID}"
        f"&uaid={uaid}&suc={_CLIENT_ID}&fluent=2&lic=1"
    )


_DEFAULT_ENTRY_EASI = "https://signup.live.com/?lic=1"

if MAILBOX == "outlook_com":
    ENTRY_URL = (
        _env("OUTLOOK_ENTRY_URL")
        or _env("OUTLOOK_SIGNUP_URL")
        or _default_entry_outlook_com()
    )
else:
    ENTRY_URL = (
        _env("OUTLOOK_ENTRY_URL")
        or _env("OUTLOOK_SIGNUP_URL")
        or _DEFAULT_ENTRY_EASI
    )
SIGNUP_URL = ENTRY_URL
OUTLOOK_COM_DOMAIN = (_env("OUTLOOK_COM_DOMAIN") or "outlook.com").lstrip("@").lower()
OUTLOOK_LOCAL_LEN = max(6, min(20, _env_int("OUTLOOK_LOCAL_LEN", 10)))

FIRST_NAMES = [
    "Nathan", "Cherly", "Galuh", "Rendy", "Sinta", "Bagas", "Dinda", "Fajar",
    "Maman", "Rina", "Andi", "Sari", "Budi", "Dewi", "Eko", "Fitri",
]
LAST_NAMES = [
    "Kusumo", "Halimah", "Pratama", "Wijaya", "Santoso", "Nugroho", "Saputra",
    "novela", "Rahman", "Putri", "Hidayat", "Lestari",
]

IMAP = ImapConfig.from_prefix("OUTLOOK_")
_email_store = UsedEmailStore(USED_EMAILS_FILE)
_ALPHANUM = string.ascii_lowercase + string.digits


def _crypto_local(n: int) -> str:
    return "".join(secrets.choice(_ALPHANUM) for _ in range(n))


def generate_outlook_com_email() -> str:
    """Unique local@outlook.com; reserve in used_emails store."""
    # Prefer letter-start (HAR: novela1924) + digits tail
    for _ in range(200):
        letters = "".join(secrets.choice(string.ascii_lowercase) for _ in range(max(4, OUTLOOK_LOCAL_LEN - 4)))
        digits = "".join(secrets.choice(string.digits) for _ in range(4))
        local = letters + digits
        addr = f"{local}@{OUTLOOK_COM_DOMAIN}"
        if _email_store.reserve(addr):
            return addr
    raise RuntimeError("Could not generate unique @outlook.com after max tries")


def allocate_email() -> str:
    """Mailbox-mode email: @outlook.com Live or EASI catch-all."""
    if MAILBOX == "outlook_com":
        return generate_outlook_com_email()
    return generate_email(IMAP, _email_store, mode=EMAIL_MODE)

# batch paths set in run_batch
BATCH_DIR: Path = RESULTS_ROOT
RESULTS_JSON: Path = RESULTS_ROOT / "accounts.json"
RESULTS_TXT: Path = RESULTS_ROOT / "accounts.txt"
FAILED_JSON: Path = RESULTS_ROOT / "failed.json"
# all successes across batches (append-only)
ALL_ACCOUNTS_TXT = Path(
    _env("OUTLOOK_ALL_ACCOUNTS_FILE", str(RESULTS_ROOT / "accounts.txt"))
)
ALL_ACCOUNTS_JSON = Path(
    _env("OUTLOOK_ALL_ACCOUNTS_JSON", str(RESULTS_ROOT / "accounts.json"))
)
_save_lock = asyncio.Lock()


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(attempt: int, step: str, message: str = "", email: str = "") -> None:
    msg = message.strip()
    if email and f"<{email}>" not in msg:
        msg = f"{msg}  <{email}>".strip()
    print(f"[{_ts()}] [{attempt}] {step:<16} {msg}".rstrip(), flush=True)


def _parse_cli(argv: list[str]) -> tuple[int | None, int | None, bool]:
    count: int | None = None
    conc: int | None = None
    yes = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(
                "outlook farm\n"
                "  -n/--count N       accounts\n"
                "  -c/--concurrent N  workers\n"
                "  -y/--yes           non-interactive\n"
                "  OUTLOOK_MAILBOX=outlook_com|easi  (default outlook_com)\n"
                "  OUTLOOK_STUB=true  email-only stub (no browser)\n"
                "  OUTLOOK_HEADLESS   default false (PX hold)\n",
                flush=True,
            )
            sys.exit(0)
        if a in ("-y", "--yes"):
            yes = True
            i += 1
            continue
        if a in ("-n", "--count", "--max") and i + 1 < len(argv):
            count = max(1, int(argv[i + 1]))
            i += 2
            continue
        if a in ("-c", "--concurrent") and i + 1 < len(argv):
            conc = max(1, int(argv[i + 1]))
            i += 2
            continue
        i += 1
    return count, conc, yes


def _effective_warp_every_n(concurrent: int) -> int:
    if WARP_EVERY_N <= 0:
        return 0
    return max(1, concurrent)


def _make_warp_policy(concurrent: int):
    every = _effective_warp_every_n(concurrent)
    if every <= 0:
        return None
    try:
        from core.warp_policy import WarpPolicy

        return WarpPolicy(
            every_n=every,
            log=lambda m: print(f"[warp] {m}", flush=True),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[warp] policy unavailable: {e}", flush=True)
        return None


def _parse_proxy(url: str) -> dict[str, Any] | None:
    """Playwright/Camoufox proxy dict, or None if URL unusable."""
    raw = (url or "").strip()
    if not raw or raw.lower() in ("direct", "none", "off", "0", "false", "null"):
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    u = urlparse(raw)
    host = (u.hostname or "").strip()
    if not host or host.lower() in ("none", "null", "undefined"):
        return None
    scheme = (u.scheme or "http").lower()
    server = f"{scheme}://{host}"
    if u.port:
        server += f":{u.port}"
    out: dict[str, Any] = {"server": server}
    if u.username:
        out["username"] = unquote(u.username)
    if u.password:
        out["password"] = unquote(u.password)
    return out


def _load_proxy_url() -> str | None:
    """Resolve proxy URL. Default = direct (no proxy).

    Opt-in only:
      OUTLOOK_PROXY=http://user:pass@host:port
      OUTLOOK_USE_PROXY=true  → first line of proxies.txt / PROXY_FILE
    Explicit off: OUTLOOK_PROXY=direct | OUTLOOK_NO_PROXY=true
    """
    # hard off
    if _env_bool("OUTLOOK_NO_PROXY", False):
        return None

    raw = _env("OUTLOOK_PROXY") or _env("OUTLOOK_PROXY_POOL") or ""
    if raw:
        first = raw.split(",")[0].strip().lstrip("\ufeff")
        if first.lower() in ("", "direct", "none", "off", "0", "false", "null"):
            return None
        if _parse_proxy(first) is None:
            print(f"[proxy] WARN: invalid OUTLOOK_PROXY, using direct: {first[:60]!r}", flush=True)
            return None
        return first

    # auto file only if explicitly enabled (HUD default = no proxies.txt)
    if not _env_bool("OUTLOOK_USE_PROXY", False):
        return None

    pfile = _env("OUTLOOK_PROXY_FILE") or _env("PROXY_FILE")
    if not pfile:
        cand = _ROOT / "proxies.txt"
        if cand.is_file():
            pfile = str(cand)
    if pfile and Path(pfile).is_file():
        for line in Path(pfile).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            cand_url = line.split("#", 1)[0].strip()
            if _parse_proxy(cand_url) is not None:
                return cand_url
            print(f"[proxy] WARN: skip bad line: {cand_url[:60]!r}", flush=True)
    return None


def wait_otp_sync(email: str, *, since_ts: float | None = None) -> str | None:
    return read_otp_imap(
        IMAP,
        email,
        timeout=OTP_TIMEOUT,
        since_ts=since_ts,
        extract=extract_microsoft_otp,
        from_filter=OTP_FROM,
        subject_hint=OTP_SUBJECT_HINT,
        microsoft_mode=True,
        log=lambda m: print(m, flush=True),
    )


def _random_profile() -> dict[str, Any]:
    day = random.randint(1, 28)
    month = random.randint(1, 12)
    year = random.randint(BIRTH_YEAR_MIN, BIRTH_YEAR_MAX)
    return {
        "first": random.choice(FIRST_NAMES),
        "last": random.choice(LAST_NAMES),
        "day": day,
        "month": month,
        "year": year,
        "birth_har": f"{day:02d}:{month:02d}:{year}",
    }


def _ensure_password(pw: str) -> str:
    """MSA wants upper+lower+digit-ish; pad if hub password too weak."""
    p = pw or "ChangeMe1!"
    if len(p) < 8:
        p = p + "Aa1!"
    if not re.search(r"[A-Z]", p):
        p = "A" + p
    if not re.search(r"[a-z]", p):
        p = p + "a"
    if not re.search(r"\d", p):
        p = p + "1"
    return p


# ── Browser UI helpers ───────────────────────────────────────────────────────

async def launch_browser(proxy_url: str | None):
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("ERROR: camoufox not installed (hub .venv)", flush=True)
        raise

    kwargs: dict[str, Any] = {
        "headless": HEADLESS,
        "humanize": 0.5,
        "os": random.choice(["windows", "macos", "linux"]),
        "locale": "en-US",
        "geoip": True,
        "block_webrtc": True,
    }
    pdict = _parse_proxy(proxy_url) if proxy_url else None
    if pdict:
        kwargs["proxy"] = pdict
    # never pass proxy={{server: http://None}} — Camoufox error "Failed to connect to proxy: http://None"
    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(60000)
    return manager, browser, page


async def screenshot(page, attempt: int, tag: str) -> None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"outlook_{attempt}_{tag}.png"
        await page.screenshot(path=str(path), full_page=True)
        _log(attempt, "shot", str(path))
    except Exception as e:
        _log(attempt, "shot", f"fail: {e}")


async def _sleep(a: float = 0.4, b: float = 1.2) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def _fill_first(page, selectors: list[str], value: str, attempt: int = 0) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            # Fluent UI: input may be "hidden" to a11y checks but still usable
            try:
                await loc.wait_for(state="attached", timeout=4000)
            except Exception:
                pass
            visible = False
            try:
                visible = await loc.is_visible()
            except Exception:
                visible = True
            if not visible:
                # still try force if it has a box
                try:
                    box = await loc.bounding_box()
                    if not box or box["width"] < 2:
                        continue
                except Exception:
                    continue
            try:
                await loc.click(timeout=4000, force=True)
            except Exception:
                try:
                    await loc.focus(timeout=2000)
                except Exception:
                    pass
            try:
                await loc.fill("")
            except Exception:
                pass
            try:
                await loc.fill(value, timeout=6000)
            except Exception:
                # type fallback (React controlled inputs)
                await loc.press("Control+a")
                await loc.type(value, delay=30)
            # verify something was typed
            try:
                got = await loc.input_value(timeout=1500)
                if got and (value in got or got in value or len(got) >= min(3, len(value))):
                    return True
            except Exception:
                return True
            return True
        except Exception:
            continue
    return False


async def _click_next(page) -> bool:
    for sel in (
        '#iSignupAction',
        'button:has-text("Next")',
        'input[type="submit"][value="Next"]',
        'button[type="submit"]',
        '#nextButton',
        'button:has-text("Create account")',
        'button:has-text("Yes")',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=4000)
                return True
        except Exception:
            continue
    # role-based
    try:
        loc = page.get_by_role("button", name=re.compile(r"^(Next|Create|Yes|Continue)$", re.I))
        if await loc.count() > 0:
            await loc.first.click(timeout=4000)
            return True
    except Exception:
        pass
    return False


async def _click_option_by_name(page, option_name: str) -> bool:
    """Click open listbox option by exact/contains name. No index fallback."""
    await asyncio.sleep(0.35)
    for name_sel in (
        page.get_by_role("option", name=re.compile(rf"^{re.escape(option_name)}$", re.I)),
        page.get_by_role("option", name=re.compile(rf"^{re.escape(option_name)}\b", re.I)),
        page.locator(f'[role="option"]:text-is("{option_name}")'),
        page.locator(f'[role="option"]:has-text("{option_name}")'),
    ):
        try:
            if await name_sel.count() > 0 and await name_sel.first.is_visible():
                await name_sel.first.click(timeout=3000)
                await asyncio.sleep(0.4)
                return True
        except Exception:
            continue
    return False


async def _pick_fluent(
    page,
    trigger_sels: list[str],
    option_index: int,
    *,
    option_name: str | None = None,
    allow_index_fallback: bool = True,
) -> bool:
    """Fluent UI combobox — open then pick by name (preferred) or index."""
    for ts in trigger_sels:
        try:
            loc = page.locator(ts).first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            await loc.click(timeout=4000)
            await asyncio.sleep(0.45)
        except Exception:
            continue

        if option_name:
            if await _click_option_by_name(page, option_name):
                return True
            # Named pick failed — do NOT index into wrong listbox (e.g. Country)
            if not allow_index_fallback:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                continue

        if not allow_index_fallback:
            continue

        opts = page.locator('[role="option"]')
        try:
            await opts.first.wait_for(state="visible", timeout=4000)
        except Exception:
            await asyncio.sleep(0.5)
        try:
            n = await opts.count()
            if n:
                idx = min(max(0, option_index), n - 1)
                await opts.nth(idx).click(timeout=3000)
                await asyncio.sleep(0.4)
                return True
        except Exception:
            continue
    return False


async def _open_month_dropdown(page) -> bool:
    """Open ONLY the Month control — never Country (first combobox on page)."""
    # 1) Explicit month APIs
    for sel in (
        '#BirthMonthDropdown',
        '[aria-label="Birth month"]',
        '[aria-label="Month"]',
        'button:has-text("Month")',
        '[name="BirthMonth"]',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=4000)
                await asyncio.sleep(0.4)
                return True
        except Exception:
            continue

    # 2) role=combobox named Month
    try:
        loc = page.get_by_role("combobox", name=re.compile(r"month", re.I))
        if await loc.count() > 0:
            await loc.first.click(timeout=4000)
            await asyncio.sleep(0.4)
            return True
    except Exception:
        pass

    # 3) Among comboboxes, find one whose label/text is exactly Month (placeholder)
    try:
        found = await page.evaluate(
            r"""() => {
              const boxes = [...document.querySelectorAll(
                '[role="combobox"], button[aria-haspopup="listbox"], button[aria-haspopup="true"]'
              )];
              for (const el of boxes) {
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 20 || !r.width) continue;
                const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                  .trim().toLowerCase();
                // Month control shows "Month" or a month name — NOT country names
                if (t === 'month' || /^(january|february|march|april|may|june|july|august|september|october|november|december)$/.test(t)) {
                  el.click();
                  return true;
                }
              }
              // Birth row: three small controls — leftmost is Month (after wide Country)
              const small = boxes.filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 50 && r.width < 200 && r.height > 20 && r.height < 60;
              });
              // Prefer the leftmost small combobox that is NOT under Country label
              small.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
              for (const el of small) {
                const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (t.includes('country') || t.includes('region')) continue;
                if (t === 'day' || /^\d{1,2}$/.test(t)) continue; // Day control
                // first remaining small = Month
                el.click();
                return true;
              }
              return false;
            }"""
        )
        if found:
            await asyncio.sleep(0.45)
            return True
    except Exception:
        pass
    return False


async def _pick_month(page, month_name: str) -> bool:
    """Select birth month by name only. Never touches Country dropdown."""
    if not await _open_month_dropdown(page):
        return False
    # List must contain January..December — if not, we opened wrong control
    try:
        opts = page.locator('[role="option"]')
        await opts.first.wait_for(state="visible", timeout=4000)
        sample = ""
        try:
            sample = (await opts.first.inner_text()).strip().lower()
        except Exception:
            pass
        # Country list starts with Afghanistan etc.; month list with January
        n = await opts.count()
        texts = []
        for i in range(min(n, 5)):
            try:
                texts.append((await opts.nth(i).inner_text()).strip().lower())
            except Exception:
                pass
        joined = " ".join(texts)
        if "afghanistan" in joined or "albania" in joined or "antigua" in joined:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            return False
        if not any(
            m in joined
            for m in ("january", "february", "march", "april", "month")
        ) and "jan" not in joined:
            # still try click by name
            pass
    except Exception:
        pass

    if await _click_option_by_name(page, month_name):
        return True
    # type-ahead: some Fluent lists support keyboard type
    try:
        await page.keyboard.type(month_name[:3], delay=80)
        await asyncio.sleep(0.2)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)
        return True
    except Exception:
        return False


async def _on_birth_step(page) -> bool:
    t = await _page_text(page)
    if "add some details" in t or "birthdate" in t or "country/region" in t:
        return True
    for sel in (
        'button:has-text("Month")',
        '[aria-label="Month"]',
        '#BirthMonthDropdown',
        'text=Enter your birthdate',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:
            continue
    return False


async def _on_name_step(page) -> bool:
    for sel in ('#firstNameInput', 'input[name="FirstName"]', '#lastNameInput'):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:
            continue
    return False


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


async def _fill_birth_step(page, profile: dict[str, Any], attempt: int, email: str) -> None:
    """Fill country + month + day + year; verify leave birth step before return."""
    month_i = int(profile["month"]) - 1  # 0-based
    day_i = int(profile["day"]) - 1
    month_name = _MONTH_NAMES[month_i]
    year = str(profile["year"])

    # Country/Region — avoid default first item (Afghanistan)
    country = COUNTRY
    country_labels = {
        "ID": "Indonesia",
        "US": "United States",
        "GB": "United Kingdom",
        "SG": "Singapore",
        "MY": "Malaysia",
        "AU": "Australia",
        "PH": "Philippines",
    }
    cname = country_labels.get(country, "Indonesia")
    ok_c = await _pick_fluent(
        page,
        [
            '[aria-label*="Country" i]',
            '#CountryDropdown',
            'button:has-text("Country")',
            '[name="Country"]',
            'div[role="combobox"]:near(:text("Country"))',
        ],
        0,
        option_name=cname,
    )
    _log(attempt, "birth", f"country={cname} ok={ok_c}", email)
    await _sleep(0.3, 0.6)

    # Month ONLY — never fall back to first combobox (that is Country)
    ok_m = await _pick_month(page, month_name)
    if not ok_m:
        ok_m = await _pick_fluent(
            page,
            [
                '#BirthMonthDropdown',
                '[aria-label="Birth month"]',
                '[aria-label="Month"]',
                'button:has-text("Month")',
                '[name="BirthMonth"]',
            ],
            month_i,
            option_name=month_name,
            allow_index_fallback=False,  # never index into Country list
        )
    _log(attempt, "birth", f"month={month_name} ok={ok_m}", email)
    await _sleep(0.3, 0.6)

    ok_d = await _pick_fluent(
        page,
        [
            '#BirthDayDropdown',
            '[aria-label="Birth day"]',
            '[aria-label="Day"]',
            'button:has-text("Day")',
            '[name="BirthDay"]',
        ],
        day_i,
        option_name=str(profile["day"]),
        allow_index_fallback=True,
    )
    _log(attempt, "birth", f"day={profile['day']} ok={ok_d}", email)
    await _sleep(0.3, 0.6)

    ok_y = await _fill_first(
        page,
        [
            'input[name="BirthYear"]',
            '[aria-label="Birth year"]',
            '[aria-label="Year"]',
            '#BirthYear',
            'input[placeholder*="Year" i]',
        ],
        year,
        attempt,
    )
    _log(attempt, "birth", f"year={year} ok={ok_y}", email)

    if not (ok_m and ok_d and ok_y):
        await screenshot(page, attempt, "birth_partial")
        # one more aggressive pass
        for _ in range(2):
            if not ok_m:
                ok_m = await _pick_month(page, month_name)
            if not ok_d:
                ok_d = await _pick_fluent(
                    page,
                    ['button:has-text("Day")', '[aria-label="Day"]', '#BirthDayDropdown'],
                    day_i,
                    option_name=str(profile["day"]),
                    allow_index_fallback=True,
                )
            if not ok_y:
                ok_y = await _fill_first(
                    page,
                    ['input[name="BirthYear"]', '[aria-label="Year"]', '#BirthYear'],
                    year,
                    attempt,
                )
            if ok_m and ok_d and ok_y:
                break
            await _sleep(0.5, 0.8)

    await _sleep()
    await _click_next(page)
    await _sleep(1.5, 2.5)

    # Must leave birth step — retry Next if validation error
    for retry in range(4):
        if await _on_name_step(page) or await gate_visible(page):
            return
        t = await _page_text(page)
        if "enter your birthdate" in t or await _on_birth_step(page):
            _log(attempt, "birth", f"validation retry {retry + 1}", email)
            await _pick_month(page, month_name)
            await _pick_fluent(
                page,
                ['button:has-text("Day")', '[aria-label="Day"]', '#BirthDayDropdown'],
                day_i,
                option_name=str(profile["day"]),
                allow_index_fallback=True,
            )
            await _fill_first(
                page,
                ['input[name="BirthYear"]', '[aria-label="Year"]', '#BirthYear'],
                year,
                attempt,
            )
            await _click_next(page)
            await _sleep(1.5, 2.5)
            continue
        # moved somewhere else
        return

    if await _on_birth_step(page):
        await screenshot(page, attempt, "birth_stuck")
        raise RuntimeError("birth step incomplete (month/day/year)")


async def _page_text(page) -> str:
    try:
        return (await page.inner_text("body")).lower()
    except Exception:
        return ""


async def _dismiss_noise(page) -> None:
    for sel in (
        '#onetrust-accept-btn-handler',
        'button:has-text("Accept")',
        'button:has-text("Got it")',
        'button:has-text("No thanks")',
        'button:has-text("Skip")',
        'button:has-text("Not now")',
        'button:has-text("Cancel")',
        'button:has-text("Maybe later")',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                txt = (await loc.inner_text()).lower()
                # don't click Next-looking
                if "next" in txt:
                    continue
                await loc.click(timeout=1500)
                await asyncio.sleep(0.3)
        except Exception:
            continue


async def _wait_for_any(page, selectors: list[str], timeout_s: float = 20.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return sel
            except Exception:
                continue
        if await gate_visible(page):
            return "__px_gate__"
        await asyncio.sleep(0.4)
    return None


# ── HAR UI flow ──────────────────────────────────────────────────────────────

async def _fill_member_name(page, email: str, attempt: int) -> str:
    """Fill signup email field.

    Correct @outlook.com UI (HAR / OAuth entry):
      [ New email ________ ] [ @outlook.com v ]  → fill LOCAL part only
    Wrong bare entry:
      [ Email ____________ ]  → full address (EASI)
    """
    local = email.split("@")[0]
    domain = email.split("@")[-1].lower() if "@" in email else ""

    if MAILBOX == "outlook_com":
        # Ensure domain dropdown shows @outlook.com (already default on correct URL)
        for sel in (
            'button:has-text("@outlook.com")',
            'button:has-text("outlook.com")',
            '#LiveDomainBoxList',
            '[name="LiveDomain"]',
            '[aria-label*="Domain" i]',
            'div[role="combobox"]:has-text("outlook")',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                txt = (await loc.inner_text()).lower()
                if "outlook" not in txt and "hotmail" not in txt:
                    await loc.click(timeout=2000)
                    await _sleep(0.3, 0.5)
                    opt = page.get_by_role(
                        "option", name=re.compile(r"@?outlook\.com", re.I)
                    )
                    if await opt.count() > 0:
                        await opt.first.click(timeout=2000)
                break
            except Exception:
                continue

        # LOCAL only — field label "New email", domain is separate dropdown
        fill_val = local
        # Role/placeholder first (Fluent often has no name=MemberName early)
        ok = False
        for getter in (
            lambda: page.get_by_placeholder(re.compile(r"new\s*email", re.I)),
            lambda: page.get_by_label(re.compile(r"new\s*email", re.I)),
            lambda: page.get_by_role("textbox", name=re.compile(r"new\s*email|email", re.I)),
        ):
            try:
                loc = getter().first
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=5000, force=True)
                await loc.fill(fill_val, timeout=6000)
                ok = True
                break
            except Exception:
                try:
                    loc = getter().first
                    await loc.click(force=True)
                    await page.keyboard.type(fill_val, delay=25)
                    ok = True
                    break
                except Exception:
                    continue

        if not ok:
            ok = await _fill_first(
                page,
                [
                    'input[name="MemberName"]',
                    'input#MemberName',
                    'input[aria-label*="New email" i]',
                    'input[placeholder*="New email" i]',
                    'input[aria-label*="new email" i]',
                    'input[placeholder*="New email" i]',
                    'input[placeholder*="email" i]',
                    'input[type="email"]',
                    # last resort: first visible text input in main form (not search)
                    'form input[type="text"]',
                    'input[type="text"]',
                ],
                fill_val,
                attempt,
            )

        # Frames (some MS pages nest)
        if not ok:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    loc = fr.get_by_placeholder(re.compile(r"new\s*email|email", re.I)).first
                    if await loc.count() > 0:
                        await loc.click(force=True)
                        await loc.fill(fill_val)
                        ok = True
                        break
                except Exception:
                    continue

        if not ok:
            # dump debug: how many inputs on page
            try:
                n_in = await page.locator("input").count()
                _log(attempt, "email", f"debug inputs_on_page={n_in}", email)
            except Exception:
                pass
            await screenshot(page, attempt, "no_email_field")
            raise RuntimeError("New email field not found (@outlook.com UI)")
        return fill_val

    # EASI: full custom-domain address
    ok = await _fill_first(
        page,
        [
            'input[name="MemberName"]',
            'input#MemberName',
            'input[type="email"]',
            'input[name="loginfmt"]',
            'input[aria-label*="email" i]',
            'input[placeholder*="email" i]',
        ],
        email,
        attempt,
    )
    if not ok:
        await screenshot(page, attempt, "no_email_field")
        raise RuntimeError("email field not found")
    return email


async def _handle_email_taken_outlook_com(page, email: str, attempt: int) -> str:
    """If username taken, pick a suggestion or regenerate local@outlook.com."""
    txt = await _page_text(page)
    taken = any(
        x in txt
        for x in (
            "already",
            "not available",
            "someone already",
            "try another",
            "isn't available",
            "is taken",
            "unavailable",
        )
    )
    if not taken:
        return email

    # Click first suggestion if UI shows them
    for sel in (
        '[role="option"]',
        'button:has-text("@outlook.com")',
        'a:has-text("@outlook.com")',
        'li:has-text("@outlook.com")',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                t = (await loc.inner_text()).strip()
                m = re.search(r"[\w.+-]+@outlook\.com", t, re.I)
                if m:
                    new_email = m.group(0).lower()
                    _email_store.reserve(new_email)
                    await loc.click(timeout=2500)
                    _log(attempt, "email", f"suggestion → {new_email}", new_email)
                    await _sleep(0.8, 1.5)
                    return new_email
        except Exception:
            continue

    # Regenerate and refill
    for _ in range(5):
        new_email = generate_outlook_com_email()
        _log(attempt, "email", f"retry available → {new_email}", new_email)
        await _fill_member_name(page, new_email, attempt)
        await _sleep()
        await _click_next(page)
        await _sleep(1.5, 2.5)
        txt = await _page_text(page)
        if not any(
            x in txt
            for x in ("already", "not available", "isn't available", "is taken", "unavailable")
        ):
            return new_email
    await screenshot(page, attempt, "email_taken")
    raise RuntimeError("email not available (@outlook.com)")


async def do_signup_ui(
    page,
    email: str,
    password: str,
    profile: dict[str, Any],
    attempt: int,
) -> dict[str, Any]:
    """Drive signup.live.com UI. outlook_com vs easi early path differs."""
    password = _ensure_password(password)
    since_otp = time.time() - 5
    working_email = email

    _log(attempt, "nav", f"goto mailbox={MAILBOX}", email)
    await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=90000)
    await _sleep(2.0, 4.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await _dismiss_noise(page)
    # Wait for signup form (proxy can be slow / interstitial)
    form_ok = await _wait_for_any(
        page,
        [
            'input[name="MemberName"]',
            'input#MemberName',
            'input[aria-label*="New email" i]',
            'input[placeholder*="New email" i]',
            'input[type="email"]',
            'text=Create your Microsoft account',
            'text=Enter your new email',
            'text=Enter your email',
        ],
        35.0,
    )
    if not form_ok:
        await screenshot(page, attempt, "no_signup_form")
        raise RuntimeError("signup form not loaded (slow proxy / blocked page)")

    txt = await _page_text(page)
    if "create one" in txt or "sign up" in txt:
        for label in ("Create one", "Create account", "Sign up free"):
            try:
                loc = page.get_by_role("link", name=re.compile(label, re.I))
                if await loc.count() > 0:
                    await loc.first.click(timeout=3000)
                    await _sleep(1.5, 3.0)
                    break
            except Exception:
                continue

    # ── Email ───────────────────────────────────────────────────────────────
    _log(attempt, "email", f"fill MemberName ({MAILBOX})", email)
    await _fill_member_name(page, working_email, attempt)
    await _sleep()
    await _click_next(page)
    await _sleep(2.0, 3.5)

    if MAILBOX == "outlook_com":
        working_email = await _handle_email_taken_outlook_com(page, working_email, attempt)
        # no IMAP OTP for Live @outlook.com
    else:
        txt = await _page_text(page)
        if any(x in txt for x in ("already", "not available", "someone already", "try another")):
            await screenshot(page, attempt, "email_taken")
            raise RuntimeError("email not available")

        # ── OTP (EASI only) ─────────────────────────────────────────────────
        otp_sels = [
            'input[name="VerificationCode"]',
            'input#VerificationCode',
            'input[name="iOttText"]',
            'input[aria-label*="code" i]',
            'input[placeholder*="code" i]',
            'input[maxlength="6"]',
            'input[maxlength="7"]',
        ]
        hit = await _wait_for_any(
            page,
            otp_sels + ['input[name="Password"]', 'input[type="password"]'],
            25.0,
        )
        if hit and hit != "__px_gate__" and "password" not in (hit or "").lower():
            _log(attempt, "wait_otp", f"IMAP poll ({OTP_TIMEOUT}s)", working_email)
            loop = asyncio.get_event_loop()
            otp = await loop.run_in_executor(
                None, lambda: wait_otp_sync(working_email, since_ts=since_otp)
            )
            if not otp:
                await screenshot(page, attempt, "otp_timeout")
                raise RuntimeError("OTP timeout (IMAP)")
            _log(attempt, "otp", f"got {otp}", working_email)
            filled = await _fill_first(page, otp_sels, otp, attempt)
            if not filled:
                await _fill_first(page, ['input[type="tel"]', 'input[type="text"]'], otp, attempt)
            await _sleep()
            await _click_next(page)
            await _sleep(2.0, 3.5)

    # ── Password ────────────────────────────────────────────────────────────
    # outlook_com: required (HAR CreateAccount has Password). easi: may skip to birth.
    pwd_sels = [
        'input[name="Password"]',
        'input[type="password"]',
        'input#PasswordInput',
    ]
    pwd_deadline = time.monotonic() + (20.0 if MAILBOX == "outlook_com" else 8.0)
    got_password = False
    while time.monotonic() < pwd_deadline:
        if await gate_visible(page):
            await _handle_px(page, attempt, working_email)
            break
        if MAILBOX == "easi" and (await _on_birth_step(page) or await _on_name_step(page)):
            _log(attempt, "password", "skipped (already on birth/name)", working_email)
            break
        found_pwd = False
        for sel in pwd_sels:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    found_pwd = True
                    break
            except Exception:
                continue
        if found_pwd:
            _log(attempt, "password", "fill Password", working_email)
            ok = await _fill_first(page, pwd_sels, password, attempt)
            if ok:
                got_password = True
                await _sleep()
                await _click_next(page)
                await _sleep(1.5, 2.5)
            break
        # still on email step?
        if await _on_birth_step(page) or await _on_name_step(page):
            break
        await asyncio.sleep(0.35)
    else:
        if MAILBOX == "outlook_com" and not got_password and not await _on_birth_step(page):
            if not await _on_name_step(page) and not await gate_visible(page):
                await screenshot(page, attempt, "no_password")
                # one more try
                if not await _fill_first(page, pwd_sels, password, attempt):
                    raise RuntimeError("password field not found (@outlook.com)")
                await _click_next(page)
                await _sleep(1.5, 2.5)

    if await gate_visible(page):
        await _handle_px(page, attempt, working_email)

    # re-bind email for rest of flow
    email = working_email

    # ── Birth date ("Add some details") ─────────────────────────────────────
    _log(attempt, "birth", profile["birth_har"], email)
    hit = await _wait_for_any(
        page,
        [
            'button:has-text("Month")',
            '[aria-label="Month"]',
            '#BirthMonthDropdown',
            '[aria-label="Birth month"]',
            'input[name="BirthYear"]',
            '#BirthYear',
            'text=Add some details',
            'text=Enter your birthdate',
            '#firstNameInput',
            'input[name="FirstName"]',
        ],
        25.0,
    )
    if await gate_visible(page):
        await _handle_px(page, attempt, email)

    # Only fill birth if still on details step (not already on name)
    if await _on_name_step(page) and not await _on_birth_step(page):
        _log(attempt, "birth", "skipped (already on name)", email)
    else:
        if not hit and not await _on_birth_step(page):
            await screenshot(page, attempt, "no_birth_step")
        await _fill_birth_step(page, profile, attempt, email)

    if await gate_visible(page):
        await _handle_px(page, attempt, email)

    # ── Name (only after birth step actually passed) ────────────────────────
    if not await _on_name_step(page):
        # wait for name fields; if still birth → fail hard
        for _ in range(15):
            if await _on_name_step(page) or await gate_visible(page):
                break
            if await _on_birth_step(page):
                await screenshot(page, attempt, "still_birth")
                raise RuntimeError("still on birth step after fill")
            await asyncio.sleep(0.5)

    _log(attempt, "name", f"{profile['first']} {profile['last']}", email)
    if await _on_name_step(page):
        ok_fn = await _fill_first(
            page,
            ['#firstNameInput', 'input[name="FirstName"]', '[aria-label*="First" i]'],
            profile["first"],
            attempt,
        )
        ok_ln = await _fill_first(
            page,
            ['#lastNameInput', 'input[name="LastName"]', '[aria-label*="Last" i]'],
            profile["last"],
            attempt,
        )
        if not (ok_fn and ok_ln):
            await screenshot(page, attempt, "name_fill_fail")
            raise RuntimeError("name fields not filled")
        await _sleep()
        await _click_next(page)
        await _sleep(3.0, 6.0)
    elif not await gate_visible(page):
        await screenshot(page, attempt, "no_name_step")
        # may have jumped to PX or success — continue
        pass

    # ── PX Human hold (HAR risk challenge) — at most ONE successful solve ──
    # Bug we hit: solved=True then loop re-called hold because residual UI text
    # still matched gate_visible → second hold failed and killed the account.
    px_cleared = False
    if await gate_visible(page) or "prove you're human" in await _page_text(page):
        px_cleared = await _handle_px(page, attempt, email)
        await _sleep(2.0, 3.5)
        if px_cleared:
            await _click_next(page)
            await _sleep(2.0, 3.5)

    # Only re-solve if still a REAL active gate (not residual text after success)
    if not px_cleared and await gate_visible(page):
        px_cleared = await _handle_px(page, attempt, email)
        await _sleep(2.0, 3.0)
        await _click_next(page)
        await _sleep(2.0, 3.0)
    elif px_cleared and await gate_visible(page):
        # false positive residual UI — do not re-hold; click next / wait nav
        _log(attempt, "px_hold", "already cleared — skip re-hold", email)
        await _click_next(page)
        await _sleep(2.0, 3.0)

    # ── Passkey / interrupt skip ────────────────────────────────────────────
    await _dismiss_noise(page)
    txt = await _page_text(page)
    if "passkey" in txt or "fingerprint" in txt or "face" in txt:
        _log(attempt, "passkey", "skip interrupt", email)
        for label in ("Skip", "No thanks", "Cancel", "Not now", "Maybe later"):
            try:
                loc = page.get_by_role("button", name=re.compile(label, re.I))
                if await loc.count() > 0:
                    await loc.first.click(timeout=2500)
                    await _sleep(1.0, 2.0)
                    break
            except Exception:
                continue

    # ── Success heuristics ──────────────────────────────────────────────────
    await _sleep(2.0, 4.0)
    url = page.url.lower()
    txt = await _page_text(page)
    success_hints = (
        "account.microsoft.com" in url
        or "outlook.live.com" in url
        or "office.com" in url
        or "privacynotice" in url
        or "stay signed in" in txt
        or "proofs" in url
        or "account.live.com" in url
    )
    fail_hints = (
        "couldn't create" in txt
        or "try again later" in txt
        or "unusual activity" in txt
        or "blocked" in txt and "account" in txt
    )
    if fail_hints and not success_hints:
        await screenshot(page, attempt, "create_blocked")
        raise RuntimeError("create blocked / unusual activity")

    if not success_hints:
        # stay signed in?
        try:
            yes = page.get_by_role("button", name=re.compile(r"^Yes$", re.I))
            if await yes.count() > 0:
                await yes.first.click(timeout=3000)
                await _sleep(2.0, 3.0)
                url = page.url.lower()
                success_hints = "account.microsoft" in url or "outlook" in url
        except Exception:
            pass

    if not success_hints:
        await screenshot(page, attempt, "uncertain")
        # soft success if no error and left signup
        if "signup.live.com" not in url:
            success_hints = True
        else:
            raise RuntimeError(f"signup incomplete url={page.url[:80]}")

    _log(attempt, "OK", f"account created ({MAILBOX})", email)
    return {
        "email": email,
        "password": password,
        "mailbox": MAILBOX,
        "first_name": profile["first"],
        "last_name": profile["last"],
        "birth": profile["birth_har"],
        "country": COUNTRY,
        "final_url": page.url,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "attempt": attempt,
    }


async def _handle_px(page, attempt: int, email: str) -> bool:
    """Solve HUMAN hold once. Returns True if cleared (or no gate). Raises on hard fail."""
    if not await gate_visible(page) and "prove you're human" not in await _page_text(page):
        return True
    _log(attempt, "px_hold", "HUMAN press-and-hold", email)
    r = await solve_px_hold_on_page(
        page,
        timeout_s=180,  # long bars need room for 3 x ~50s holds
        max_attempts=PX_MAX_ATTEMPTS,
        hold_min=PX_HOLD_MIN,
        hold_max=PX_HOLD_MAX,
        wait_gate_s=20,
        bake_s=3.0,
    )
    # Re-check DOM — never trust cookie-only "solved" if card still visible
    still_gate = await gate_visible(page) or "let's prove you're human" in await _page_text(
        page
    )
    _log(
        attempt,
        "px_hold",
        f"solved={r.solved} gate_reached={r.gate_reached} still_on_screen={still_gate} "
        f"attempts={r.attempts} target={r.target} rotated={r.px3_rotated} "
        f"err={r.error or '-'}",
        email,
    )
    if r.solved and not still_gate:
        await asyncio.sleep(1.5)
        return True
    if still_gate:
        # Honest fail — captcha card still up
        await screenshot(page, attempt, "px_still_on_screen")
        raise RuntimeError(
            r.error
            or "PX reported done but HUMAN challenge still visible on page"
        )
    if r.gate_reached and not r.solved:
        await screenshot(page, attempt, "px_fail")
        raise RuntimeError(r.error or "PX hold failed")
    return True


# ── Persist ──────────────────────────────────────────────────────────────────

async def save_account(row: dict[str, Any]) -> None:
    """Write success to current batch + global append-only files under results/."""
    line = f"{row.get('email')}:{row.get('password')}\n"
    async with _save_lock:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

        # per-batch JSON
        rows: list[dict] = []
        if RESULTS_JSON.is_file():
            try:
                rows = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    rows = []
            except Exception:
                rows = []
        rows.append(row)
        RESULTS_JSON.write_text(json.dumps(rows, indent=2), encoding="utf-8")

        # per-batch txt
        with open(RESULTS_TXT, "a", encoding="utf-8") as f:
            f.write(line)

        # global all-successes (one file, every OK ever)
        with open(ALL_ACCOUNTS_TXT, "a", encoding="utf-8") as f:
            f.write(line)

        # global JSON append (same schema as batch)
        all_rows: list[dict] = []
        if ALL_ACCOUNTS_JSON.is_file():
            try:
                all_rows = json.loads(ALL_ACCOUNTS_JSON.read_text(encoding="utf-8"))
                if not isinstance(all_rows, list):
                    all_rows = []
            except Exception:
                all_rows = []
        all_rows.append(row)
        ALL_ACCOUNTS_JSON.write_text(json.dumps(all_rows, indent=2), encoding="utf-8")


async def save_failed(attempt: int, email: str, error: str) -> None:
    async with _save_lock:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []
        if FAILED_JSON.is_file():
            try:
                rows = json.loads(FAILED_JSON.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    rows = []
            except Exception:
                rows = []
        rows.append(
            {
                "attempt": attempt,
                "email": email,
                "error": error,
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        FAILED_JSON.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def init_batch(n: int, c: int) -> str:
    global BATCH_DIR, RESULTS_JSON, RESULTS_TXT, FAILED_JSON
    bid = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + "".join(
        random.choices(string.hexdigits.lower(), k=6)
    )
    BATCH_DIR = RESULTS_ROOT / f"batch_{bid}"
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON = BATCH_DIR / "accounts.json"
    RESULTS_TXT = BATCH_DIR / "accounts.txt"
    FAILED_JSON = BATCH_DIR / "failed.json"
    meta = {
        "batch_id": bid,
        "n": n,
        "concurrent": c,
        "mailbox": MAILBOX,
        "email_mode": EMAIL_MODE if MAILBOX == "easi" else "outlook.com",
        "entry": ENTRY_URL[:120],
        "headless": HEADLESS,
        "stub": STUB_MODE,
        "started": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (BATCH_DIR / "batch_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return bid


# ── Worker ───────────────────────────────────────────────────────────────────

async def farm_one(attempt: int, *, total: int) -> bool:
    try:
        email = allocate_email()
    except Exception as e:
        _log(attempt, "fail", f"email gen: {e}")
        await save_failed(attempt, "", str(e))
        return False

    _log(attempt, "start", f"#{attempt}/{total}", email)
    _log(attempt, "email", f"mailbox={MAILBOX}", email)

    if STUB_MODE:
        await asyncio.sleep(0.1)
        _log(attempt, "stub", "OUTLOOK_STUB=true — no browser", email)
        _log(attempt, "OK", "stub (email reserved)", email)
        return True

    password = _ensure_password(PASSWORD)
    profile = _random_profile()
    proxy_url = _load_proxy_url()
    manager = None
    try:
        async def _body() -> dict[str, Any]:
            nonlocal manager
            pshow = "direct"
            if proxy_url:
                try:
                    u = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
                    pshow = f"{u.hostname}:{u.port or ''}"
                except Exception:
                    pshow = "proxy"
            _log(
                attempt,
                "browser",
                f"Camoufox headless={HEADLESS} proxy={pshow}",
                email,
            )
            manager, _browser, page = await launch_browser(proxy_url)
            return await do_signup_ui(page, email, password, profile, attempt)

        row = await asyncio.wait_for(_body(), timeout=float(ACCOUNT_TIMEOUT_S))
        row["proxy"] = proxy_url or "direct"
        await save_account(row)
        return True
    except Exception as e:
        err = str(e)[:300]
        _log(attempt, "fail", err, email)
        await save_failed(attempt, email, err)
        return False
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


async def run_batch(n: int, concurrent: int) -> int:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    known = _email_store.load(RESULTS_ROOT)
    bid = init_batch(n, concurrent)
    policy = _make_warp_policy(concurrent)
    fails = 0
    proxy_mode = _load_proxy_url() or "direct"

    # c=1: strict serial (no gather). c>1: semaphore-limited gather.
    concurrent = max(1, int(concurrent))

    async def run_one(attempt: int) -> bool:
        if SPAWN_DELAY > 0 and attempt > 1:
            await asyncio.sleep(SPAWN_DELAY)
        ok = await farm_one(attempt, total=n)
        if ok and policy is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, policy.on_success)
        if BETWEEN_WAIT > 0 and attempt < n:
            _log(
                attempt,
                "wait",
                f"between-account cool-down {BETWEEN_WAIT:.0f}s "
                f"(next #{attempt + 1}/{n})",
            )
            await asyncio.sleep(BETWEEN_WAIT)
        return ok

    print("=" * 60, flush=True)
    print("  Outlook Farmer (HAR flow)", flush=True)
    print("=" * 60, flush=True)
    print(f"  Stub       : {STUB_MODE}", flush=True)
    print(f"  Mailbox    : {MAILBOX}", flush=True)
    if MAILBOX == "outlook_com":
        print(f"  Domain     : @{OUTLOOK_COM_DOMAIN} (Live, no IMAP OTP)", flush=True)
        print(f"  Entry      : {ENTRY_URL[:70]}…", flush=True)
    else:
        print(f"  Email mode : {EMAIL_MODE} (EASI + IMAP OTP)", flush=True)
        if EMAIL_MODE == "domain":
            print(f"  Domain     : @{IMAP.email_domain}", flush=True)
        else:
            print(f"  Gmail base : {IMAP.gmail_base or IMAP.user}", flush=True)
        print(f"  IMAP       : {IMAP.user} @ {IMAP.host}:{IMAP.port}", flush=True)
    print(f"  Headless   : {HEADLESS}", flush=True)
    print(f"  Password   : {'*' * max(0, len(PASSWORD) - 2)}{PASSWORD[-2:]}", flush=True)
    print(f"  Count      : {n}", flush=True)
    print(f"  Concurrent : {concurrent}  ({'serial' if concurrent <= 1 else 'parallel'})", flush=True)
    print(f"  BetweenWait: {BETWEEN_WAIT:.0f}s (after each OK/fail)", flush=True)
    print(f"  Proxy      : {proxy_mode if proxy_mode == 'direct' else 'on (OUTLOOK_PROXY / USE_PROXY)'}", flush=True)
    print(f"  Known mail : {known}", flush=True)
    print(f"  WARP everyN: {_effective_warp_every_n(concurrent) or 'off'}", flush=True)
    print(f"  Batch      : {bid}", flush=True)
    print(f"  Results    : {BATCH_DIR}", flush=True)
    print("-" * 60, flush=True)
    print("  Note: log [N] = account attempt #N of -n (not concurrent workers)", flush=True)
    print("-" * 60, flush=True)

    if concurrent <= 1:
        # Strict serial: one browser at a time (c=1)
        for i in range(1, n + 1):
            ok = await run_one(i)
            if not ok:
                fails += 1
    else:
        sem = asyncio.Semaphore(concurrent)
        lock = asyncio.Lock()

        async def worker(attempt: int) -> None:
            nonlocal fails
            async with sem:
                ok = await run_one(attempt)
                if not ok:
                    async with lock:
                        fails += 1

        await asyncio.gather(*(worker(i) for i in range(1, n + 1)))

    ok_n = n - fails
    print(
        f"[done] outlook batch n={n} ok={ok_n} fail={fails} "
        f"stub={STUB_MODE} mailbox={MAILBOX}",
        flush=True,
    )
    return fails


def main() -> None:
    arg_n, arg_c, yes = _parse_cli(sys.argv[1:])
    n = arg_n if arg_n is not None else MAX_ACCOUNTS
    c = arg_c if arg_c is not None else CONCURRENT

    # IMAP only required for EASI (custom domain OTP)
    if MAILBOX == "easi":
        try:
            IMAP.require_mode(EMAIL_MODE)
        except RuntimeError as e:
            print(f"ERROR: {e}", flush=True)
            print(
                "  Hub .env: IMAP_USER, IMAP_PASS, EMAIL_DOMAIN (domain) "
                "or GMAIL_BASE (plus_trick)",
                flush=True,
            )
            sys.exit(1)

    if not STUB_MODE:
        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401
        except ImportError:
            print("ERROR: camoufox missing — use hub .venv", flush=True)
            sys.exit(1)

    if not yes and sys.stdin.isatty():
        try:
            raw = input(
                f"Run {n} x {MAILBOX}, concurrent {c}? [Y/n] "
            ).strip().lower()
            if raw in ("n", "no"):
                print("aborted", flush=True)
                sys.exit(1)
        except EOFError:
            pass

    fails = asyncio.run(run_batch(n, c))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
