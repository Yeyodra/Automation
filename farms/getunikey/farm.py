#!/usr/bin/env python3
"""
GetUniKey farmer — Google OAuth path (HAR 2026-07-22).

Flow per account:
  1. Pop Google account from google_accounts.txt (email:password)
  2. Camoufox → /sign-up?aff= → Google OAuth → session cookie + user id
  3. POST /api/token/ + list + POST /api/token/{id}/key → save API key

Hub: farms/getunikey — env GETUNIKEY_* (mapped from Automation/.env).
Run:    python -m jobs run getunikey -- -n 1 -c 1 -y
WARP: hub injects WARP_EVERY_N (1:1 with -c); farm rotates via core.warp after OK.

CLI: -n / --count, -c / --concurrent, -y / --yes
Log: [HH:MM:SS] [<id>] <step>  message  <email@domain>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None  # type: ignore[misc, assignment]


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


# ── Product (HAR 2026-07-22) ─────────────────────────────────────────────────
BASE_URL = "https://www.getunikey.ai"
GOOGLE_CLIENT_ID = (
    "190146626926-416bbh8g0ft25u7rll5a82k2plk4atel.apps.googleusercontent.com"
)
OAUTH_REDIRECT = f"{BASE_URL}/oauth/google"
DEFAULT_REFERRAL_URL = f"{BASE_URL}/sign-up?aff=bTOY"
# Full register start URL (HUD / env). Empty → hardcoded default.
REFERRAL_URL = (_env("GETUNIKEY_REFERRAL_URL") or DEFAULT_REFERRAL_URL).strip()
if not REFERRAL_URL.startswith("http"):
    REFERRAL_URL = DEFAULT_REFERRAL_URL


def _aff_from_referral(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    try:
        q = parse_qs(urlparse(url).query)
        aff = (q.get("aff") or [""])[0].strip()
        if aff:
            return aff
    except Exception:
        pass
    return _env("GETUNIKEY_AFF") or "bTOY"


AFF_CODE = _aff_from_referral(REFERRAL_URL)
# Hub maps shared HEADLESS → GETUNIKEY_HEADLESS; we use a dedicated key.
# Default headless=true; set GETUNIKEY_BROWSER_HEADLESS=false to debug Google UI.
HEADLESS = _env_bool("GETUNIKEY_BROWSER_HEADLESS", True)
STUB = _env_bool("GETUNIKEY_STUB", False)
# After reveal key: POST /v1/chat/completions
SMOKE_TEST = _env_bool("GETUNIKEY_SMOKE_TEST", True)
SMOKE_MODEL = _env("GETUNIKEY_SMOKE_MODEL") or "qwen/qwen3.6-flash"
# false = still save key on smoke fail (e.g. platform USD quota 不足)
SMOKE_REQUIRE = _env_bool("GETUNIKEY_SMOKE_REQUIRE", False)
TOKEN_NAME = _env("GETUNIKEY_TOKEN_NAME") or "prod"
# 9router inject: manual / later (build provider node first) — not auto in farm
GOTO_TIMEOUT_MS = max(30_000, _env_int("GETUNIKEY_GOTO_TIMEOUT_MS") or 90_000)
GOOGLE_TIMEOUT_S = max(60.0, float(_env("GETUNIKEY_GOOGLE_TIMEOUT") or "180") or 180.0)
SPAWN_DELAY = max(0.0, float(_env("GETUNIKEY_SPAWN_DELAY") or "3") or 3.0)

# HUD may inject multiline list via GETUNIKEY_ACCOUNTS_LIST (email|pass or email:pass)
ACCOUNTS_LIST_RAW = _env("GETUNIKEY_ACCOUNTS_LIST")
_accounts_raw = _env("GETUNIKEY_ACCOUNTS_FILE") or str(_ROOT / "google_accounts.txt")
ACCOUNTS_FILE = Path(_accounts_raw)
if not ACCOUNTS_FILE.is_absolute():
    ACCOUNTS_FILE = _ROOT / ACCOUNTS_FILE

RESULTS_ROOT = _ROOT / "results"
SCREENSHOT_DIR = _ROOT / "screenshots"
USED_GOOGLE_FILE = RESULTS_ROOT / "used_google.txt"
GLOBAL_KEYS_TXT = RESULTS_ROOT / "apikeys.txt"
GLOBAL_CREDS_TXT = RESULTS_ROOT / "credentials.txt"

WARP_EVERY_N = max(
    0,
    _env_int("GETUNIKEY_WARP_EVERY_N") or _env_int("WARP_EVERY_N") or 0,
)
CONCURRENT = max(1, _env_int("GETUNIKEY_CONCURRENT") or _env_int("CONCURRENT") or 1)
WARP_SETTLE_AFTER = max(0.0, float(_env("WARP_SETTLE_AFTER") or "8") or 8.0)

BATCH_ID = ""
BATCH_DIR: Path = RESULTS_ROOT
RESULTS_JSON: Path = RESULTS_ROOT / "accounts.json"
RESULTS_TXT: Path = RESULTS_ROOT / "accounts.txt"
FAILED_JSON: Path = RESULTS_ROOT / "failed.json"

_success_since_warp = 0
_warp_rotate_lock = threading.Lock()
_warp_drain_owner: int | None = None
_in_flight = 0
_if_lock: asyncio.Lock | None = None
_can_start: asyncio.Event | None = None
_results_lock = asyncio.Lock()
_pool_lock = threading.Lock()
_account_pool: list["GoogleAccount"] = []
_used_emails: set[str] = set()


@dataclass(frozen=True)
class GoogleAccount:
    email: str
    password: str


def _parse_account_line(line: str) -> GoogleAccount | None:
    """email|password or email:password (first separator wins)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "|" in line:
        email, _, password = line.partition("|")
    elif ":" in line:
        email, _, password = line.partition(":")
    else:
        return None
    email, password = email.strip(), password.strip()
    if email and password:
        return GoogleAccount(email=email, password=password)
    return None


def load_google_accounts(path: Path = ACCOUNTS_FILE) -> list[GoogleAccount]:
    """HUD list (GETUNIKEY_ACCOUNTS_LIST) wins; else file email|pass or email:pass."""
    raw = (ACCOUNTS_LIST_RAW or "").strip()
    if raw:
        src_lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    elif path.is_file():
        src_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        return []
    out: list[GoogleAccount] = []
    seen: set[str] = set()
    for line in src_lines:
        acc = _parse_account_line(line)
        if not acc:
            continue
        key = acc.email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(acc)
    return out


def _load_used() -> set[str]:
    if not USED_GOOGLE_FILE.is_file():
        return set()
    return {
        ln.strip().lower()
        for ln in USED_GOOGLE_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }


def _mark_used(email: str) -> None:
    USED_GOOGLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USED_GOOGLE_FILE.open("a", encoding="utf-8") as f:
        f.write(email.lower() + "\n")
    _used_emails.add(email.lower())


def _pop_account() -> GoogleAccount | None:
    with _pool_lock:
        while _account_pool:
            acc = _account_pool.pop(0)
            if acc.email.lower() in _used_emails:
                continue
            return acc
    return None


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log_step(attempt: int, step: str, msg: str, email: str = "") -> None:
    tail = f"  <{email}>" if email else ""
    print(f"[{_ts()}] [{attempt}] {step}  {msg}{tail}", flush=True)


def _effective_warp_every_n() -> int:
    if WARP_EVERY_N <= 0:
        return 0
    return max(WARP_EVERY_N, CONCURRENT) if CONCURRENT > 0 else WARP_EVERY_N


def _ensure_async_gates() -> tuple[asyncio.Lock, asyncio.Event]:
    global _if_lock, _can_start
    if _if_lock is None:
        _if_lock = asyncio.Lock()
    if _can_start is None:
        _can_start = asyncio.Event()
        _can_start.set()
    return _if_lock, _can_start


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


async def _maybe_warp_after_success(attempt: int) -> None:
    global _success_since_warp, _warp_drain_owner
    every = _effective_warp_every_n()
    if every <= 0:
        return

    should_rotate = False
    with _warp_rotate_lock:
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
            f"[{attempt}] WARP every_n: wave complete {every}/{every} "
            f"→ drain then rotate…",
            flush=True,
        )

    if not should_rotate:
        return

    if_lock, can_start = _ensure_async_gates()
    can_start.clear()
    last_log = 0.0
    try:
        drain_deadline = time.time() + 180.0
        while True:
            async with if_lock:
                n_if = _in_flight
            if n_if <= 1:
                break
            if time.time() >= drain_deadline:
                print(
                    f"[{attempt}] WARP every_n: drain timeout "
                    f"(in_flight={n_if}) — rotate anyway",
                    flush=True,
                )
                break
            now = time.time()
            if now - last_log >= 5.0:
                print(
                    f"[{attempt}] WARP every_n: waiting peers "
                    f"(in_flight={n_if})…",
                    flush=True,
                )
                last_log = now
            await asyncio.sleep(0.5)

        print(f"[{attempt}] WARP every_n: drain ok → rotate", flush=True)
        ok = await asyncio.to_thread(_rotate_warp_sync, attempt)
        if ok and WARP_SETTLE_AFTER > 0:
            print(
                f"[{attempt}] WARP every_n: settle {WARP_SETTLE_AFTER:.0f}s…",
                flush=True,
            )
            await asyncio.sleep(WARP_SETTLE_AFTER)
    finally:
        can_start.set()
        with _warp_rotate_lock:
            if _warp_drain_owner == attempt:
                _warp_drain_owner = None


def init_batch(max_accounts: int, concurrent: int) -> str:
    global BATCH_ID, BATCH_DIR, RESULTS_JSON, RESULTS_TXT, FAILED_JSON
    global _account_pool, _used_emails

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _used_emails = _load_used()
    all_acc = load_google_accounts()
    _account_pool = [a for a in all_acc if a.email.lower() not in _used_emails]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BATCH_ID = _env("GETUNIKEY_BATCH_ID") or f"batch_{stamp}_{secrets.token_hex(3)}"
    BATCH_DIR = RESULTS_ROOT / BATCH_ID
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON = BATCH_DIR / "accounts.json"
    RESULTS_TXT = BATCH_DIR / "accounts.txt"
    FAILED_JSON = BATCH_DIR / "failed.json"
    for p, empty in (
        (RESULTS_JSON, "[]"),
        (FAILED_JSON, "[]"),
        (RESULTS_TXT, ""),
    ):
        if not p.exists():
            p.write_text(empty + ("\n" if empty == "[]" else ""), encoding="utf-8")
    if not GLOBAL_KEYS_TXT.exists():
        GLOBAL_KEYS_TXT.write_text("# api_key\temail\tuser_id\tbatch_id\n", encoding="utf-8")
    if not GLOBAL_CREDS_TXT.exists():
        GLOBAL_CREDS_TXT.write_text(
            "# email|google_password|api_key|user_id|token_id|token_name|created_at|batch_id\n",
            encoding="utf-8",
        )
    meta = {
        "batch_id": BATCH_ID,
        "started_at": _now_iso(),
        "product": "getunikey.ai",
        "aff": AFF_CODE,
        "headless": HEADLESS,
        "max_accounts": max_accounts,
        "concurrent": concurrent,
        "accounts_file": str(ACCOUNTS_FILE),
        "pool_available": len(_account_pool),
        "used_already": len(_used_emails),
    }
    (BATCH_DIR / "batch_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[getunikey] batch={BATCH_ID} dir={BATCH_DIR}", flush=True)
    print(
        f"[getunikey] pool={len(_account_pool)} used={len(_used_emails)} "
        f"aff={AFF_CODE!r} headless={HEADLESS}",
        flush=True,
    )
    return BATCH_ID


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


async def save_ok(result: dict) -> None:
    async with _results_lock:
        rows: list = []
        if RESULTS_JSON.is_file():
            try:
                rows = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(result)
        RESULTS_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

        email = str(result.get("email") or "")
        gpass = str(result.get("google_password") or "")
        key = str(result.get("api_key") or "")
        uid = str(result.get("user_id") or "")
        tid = str(result.get("token_id") or "")
        tname = str(result.get("token_name") or TOKEN_NAME)
        created = str(result.get("created_at") or _now_iso())

        _append_line(RESULTS_TXT, f"{email}\t{key}\t{uid}\t{tid}")
        _append_line(
            BATCH_DIR / "credentials.txt",
            "|".join([email, gpass, key, uid, tid, tname, created]),
        )
        if key:
            _append_line(BATCH_DIR / "apikeys.txt", key)
            _append_line(GLOBAL_KEYS_TXT, f"{key}\t{email}\t{uid}\t{BATCH_ID}")
        _append_line(
            GLOBAL_CREDS_TXT,
            "|".join([email, gpass, key, uid, tid, tname, created, BATCH_ID]),
        )


async def save_fail(row: dict) -> None:
    async with _results_lock:
        rows: list = []
        if FAILED_JSON.is_file():
            try:
                rows = json.loads(FAILED_JSON.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(row)
        FAILED_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


async def screenshot(page: Any, attempt: int, tag: str) -> None:
    try:
        path = SCREENSHOT_DIR / f"getunikey_{attempt}_{tag}.png"
        await page.screenshot(path=str(path), full_page=True)
        _log_step(attempt, "shot", str(path.name))
    except Exception as e:
        _log_step(attempt, "shot", f"fail {e}")


async def launch_browser():
    if AsyncCamoufox is None:
        raise RuntimeError("camoufox not installed — pip install camoufox[geoip] && camoufox fetch")
    kwargs: dict[str, Any] = {
        "headless": HEADLESS,
        "humanize": 0.5,
        "os": random.choice(["windows", "macos", "linux"]),
        "locale": "en-US",
        "geoip": True,
        "block_webrtc": True,
    }
    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(max(60_000, GOTO_TIMEOUT_MS + 15_000))
    return manager, browser, page


async def _close_browser(manager: Any) -> None:
    if manager is None:
        return
    try:
        await manager.__aexit__(None, None, None)
    except Exception:
        pass


async def fill_first(page: Any, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=4000)
            await loc.click(timeout=5000)
            await page.wait_for_timeout(200)
            # Google MUI often ignores fill(); clear + type + press Sequentially
            try:
                await loc.fill("")
            except Exception:
                await loc.press("Control+a")
                await loc.press("Backspace")
            await loc.press_sequentially(value, delay=random.randint(40, 90))
            # verify something landed
            try:
                got = await loc.input_value(timeout=1000)
                if got and len(got) >= min(3, len(value)):
                    return True
            except Exception:
                return True
        except Exception:
            continue
    # last resort: focused element via keyboard
    try:
        await page.keyboard.type(value, delay=random.randint(40, 90))
        return True
    except Exception:
        return False


async def click_first(page: Any, selectors: list[str], timeout: int = 5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _pick_google_page(browser: Any, page: Any, attempt: int) -> Any:
    """Use the tab that actually shows accounts.google.com (popup or same page)."""
    deadline = time.time() + 45.0
    last_urls: list[str] = []
    while time.time() < deadline:
        pages = list(getattr(browser, "pages", None) or [])
        if not pages:
            pages = [page]
        last_urls = []
        for p in pages:
            try:
                u = p.url or ""
            except Exception:
                u = ""
            last_urls.append(u[:80])
            if "accounts.google.com" in u:
                if p is not page:
                    _log_step(attempt, "google", "switched to Google tab")
                try:
                    await p.bring_to_front()
                except Exception:
                    pass
                return p
        # also wait current page navigation
        try:
            u = page.url or ""
            if "accounts.google.com" in u:
                return page
        except Exception:
            pass
        await asyncio.sleep(0.4)
    _log_step(attempt, "google", f"no Google tab yet urls={last_urls!r}")
    return page


async def _google_login(page: Any, attempt: int, acc: GoogleAccount, browser: Any = None) -> Any:
    """Fill Google identifier + password. Returns the page that finished OAuth."""
    email, password = acc.email, acc.password
    deadline = time.time() + GOOGLE_TIMEOUT_S

    if browser is not None:
        page = await _pick_google_page(browser, page, attempt)

    # Wait hard for email field (Google v3 identifier)
    _log_step(attempt, "google", "waiting email field…", email)
    email_sels = [
        "#identifierId",
        "input[type='email']",
        "input[name='identifier']",
        "input[autocomplete='username']",
    ]
    got_email = False
    while time.time() < deadline:
        if browser is not None:
            page = await _pick_google_page(browser, page, attempt)
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        if "getunikey.ai" in url and "accounts.google" not in url:
            if await _session_ready(page):
                return page
        # password already?
        try:
            if await page.locator("input[type='password'], input[name='Passwd']").count() > 0:
                if await page.locator("input[type='password'], input[name='Passwd']").first.is_visible():
                    break
        except Exception:
            pass
        try:
            await page.wait_for_selector(
                "#identifierId, input[type='email'], input[name='identifier']",
                timeout=3000,
                state="visible",
            )
        except Exception:
            await page.wait_for_timeout(400)
            continue
        ok = await fill_first(page, email_sels, email)
        if ok:
            _log_step(attempt, "google", "email filled", email)
            await page.wait_for_timeout(400)
            # Next: button or Enter
            if not await click_first(
                page,
                [
                    "#identifierNext",
                    "#identifierNext button",
                    "button:has-text('Next')",
                    "button:has-text('Berikutnya')",
                    "div[role='button']:has-text('Next')",
                ],
                timeout=4000,
            ):
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
            got_email = True
            await page.wait_for_timeout(2000)
            break
        _log_step(attempt, "google", "email fill retry…", email)
        await page.wait_for_timeout(800)
    if not got_email:
        # maybe already past email
        try:
            if await page.locator("input[type='password'], input[name='Passwd']").count() == 0:
                raise RuntimeError("Google email field not found / fill failed")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Google email field not found / fill failed: {e}") from e

    # Password step
    _log_step(attempt, "google", "waiting password field…", email)
    pass_sels = [
        "input[name='Passwd']",
        "input[type='password']",
        "#password input",
        "input[autocomplete='current-password']",
    ]
    got_pass = False
    while time.time() < deadline:
        if browser is not None:
            page = await _pick_google_page(browser, page, attempt)
        try:
            body = (await page.content())[:5000].lower()
        except Exception:
            body = ""
        if "couldn" in body and "find your google account" in body:
            raise RuntimeError("Google: account not found")
        if "couldn’t find your google account" in body or "couldn't find your google account" in body:
            raise RuntimeError("Google: account not found")
        try:
            await page.wait_for_selector(
                "input[name='Passwd'], input[type='password']",
                timeout=3000,
                state="visible",
            )
        except Exception:
            url = ""
            try:
                url = page.url or ""
            except Exception:
                pass
            if "getunikey.ai" in url and await _session_ready(page):
                return page
            await page.wait_for_timeout(500)
            continue
        ok = await fill_first(page, pass_sels, password)
        if ok:
            _log_step(attempt, "google", "password filled", email)
            await page.wait_for_timeout(400)
            if not await click_first(
                page,
                [
                    "#passwordNext",
                    "#passwordNext button",
                    "button:has-text('Next')",
                    "button:has-text('Berikutnya')",
                    "div[role='button']:has-text('Next')",
                ],
                timeout=4000,
            ):
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
            got_pass = True
            await page.wait_for_timeout(2500)
            break
        _log_step(attempt, "google", "password fill retry…", email)
        await page.wait_for_timeout(800)
    if not got_pass:
        raise RuntimeError("Google password field not found / fill failed")

    # Post-password: workspace ToS + OAuth consent ("Login ke getunikey.ai" / Lanjutkan).
    last_url = ""
    stuck_ticks = 0
    while time.time() < deadline:
        if browser is not None:
            for p in list(getattr(browser, "pages", None) or [page]):
                try:
                    u = p.url or ""
                    if "getunikey.ai" in u:
                        page = p
                        break
                    if "accounts.google.com" in u:
                        page = p
                except Exception:
                    pass
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        if url != last_url:
            last_url = url
            stuck_ticks = 0
            _log_step(attempt, "google", f"page {url[:90]}", email)
        else:
            stuck_ticks += 1

        if "getunikey.ai" in url:
            if await _session_ready(page):
                return page
            await page.wait_for_timeout(800)
            if await _session_ready(page):
                return page

        if "accounts.google.com" in url or "google.com" in url:
            try:
                body = (await page.content())[:12000].lower()
            except Exception:
                body = ""
            if "wrong password" in body or "incorrect password" in body:
                raise RuntimeError("Google: wrong password")
            hard_2fa = any(
                x in body
                for x in (
                    "2-step verification",
                    "2-step",
                    "enter the code",
                    "authenticator app",
                    "get a verification code",
                )
            ) and (
                await page.locator(
                    "input[type='tel'], input[id*='code'], input[name*='code'], "
                    "input[aria-label*='code' i], input[autocomplete='one-time-code']"
                ).count()
                > 0
            )
            if hard_2fa:
                await screenshot(page, attempt, "2fa")
                raise RuntimeError("Google: 2FA/challenge required (not supported)")

            # Dedicated consent click (EN + ID). Never click Batal/Cancel.
            clicked_label = await _google_click_consent(page)
            if clicked_label:
                _log_step(
                    attempt, "google", f"consent click {clicked_label!r}", email
                )
                await page.wait_for_timeout(2000)
                continue

            if stuck_ticks > 0 and stuck_ticks % 6 == 0:
                await screenshot(page, attempt, f"stuck_{stuck_ticks}")
                _log_step(attempt, "google", "still on Google interstitial…", email)

        await page.wait_for_timeout(700)

    await screenshot(page, attempt, "oauth_timeout")
    raise RuntimeError("Google OAuth did not return to getunikey / no session")


async def _google_click_consent(page: Any) -> str:
    """Click primary OAuth/ToS CTA. Prefer Lanjutkan/Continue; never Batal/Cancel."""
    # Exact labels first:
    # - Workspace Education speedbump: "I understand"
    # - OAuth consent: Lanjutkan / Continue / Allow
    labels = (
        "I understand",
        "Saya mengerti",
        "Saya paham",
        "Lanjutkan",
        "Continue",
        "Allow",
        "Confirm",
        "I agree",
        "Accept",
        "Agree",
        "Berikutnya",
        "Next",
        "Ya",
        "Setuju",
        "Saya setuju",
        "Konfirmasi",
    )
    deny = ("batal", "cancel", "tolak", "deny", "back", "kembali")

    # Workspace terms often needs scroll before primary CTA is enabled/visible
    try:
        url0 = (page.url or "").lower()
        if "workspaceterms" in url0 or "speedbump" in url0:
            for _ in range(3):
                try:
                    await page.mouse.wheel(0, 1200)
                except Exception:
                    pass
                await page.wait_for_timeout(200)
                try:
                    await page.evaluate(
                        """() => {
                          const se = document.scrollingElement || document.documentElement;
                          se.scrollTop = se.scrollHeight;
                          const cards = document.querySelectorAll('[role="main"], .JQ5tlb, .tTmh9');
                          cards.forEach(c => { try { c.scrollTop = c.scrollHeight; } catch(e){} });
                        }"""
                    )
                except Exception:
                    pass
    except Exception:
        pass

    for label in labels:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=label, exact=True)
                if await loc.count() == 0:
                    loc = page.get_by_role(role, name=label, exact=False)
                if await loc.count() == 0:
                    continue
                btn = loc.last
                if not await btn.is_visible(timeout=800):
                    continue
                await btn.scroll_into_view_if_needed(timeout=2000)
                await btn.click(timeout=4000, force=True)
                return label
            except Exception:
                continue

    # CSS / text selectors (avoid broad jsname which hits Cancel too)
    sels = [
        "#submit_approve_access",
        "button:has-text('I understand')",
        "button:has-text('Saya mengerti')",
        "button:has-text('Lanjutkan')",
        "button:has-text('Continue')",
        "button:has-text('Allow')",
        "div[role='button']:has-text('I understand')",
        "div[role='button']:has-text('Lanjutkan')",
        "div[role='button']:has-text('Continue')",
        "span:has-text('I understand')",
        "span:has-text('Lanjutkan')",
        "span:has-text('Continue')",
    ]
    for sel in sels:
        try:
            loc = page.locator(sel).last
            if await loc.count() == 0:
                continue
            if not await loc.is_visible(timeout=600):
                continue
            txt = (await loc.inner_text(timeout=500) or "").strip().lower()
            if any(d in txt for d in deny):
                continue
            await loc.scroll_into_view_if_needed(timeout=2000)
            await loc.click(timeout=4000, force=True)
            return txt or sel
        except Exception:
            continue

    # JS: only primary-looking blue/submit, never cancel
    try:
        js_ok = await page.evaluate(
            """() => {
              const deny = /^(batal|cancel|tolak|deny|back|kembali)$/i;
              const allow = /^(i understand|saya mengerti|saya paham|lanjutkan|continue|allow|confirm|i agree|accept|agree|berikutnya|next|ya|setuju|saya setuju|konfirmasi)$/i;
              const nodes = [...document.querySelectorAll(
                'button, div[role="button"], span[role="button"], input[type="submit"]'
              )];
              const scored = [];
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
                if (!t || t.length > 48) continue;
                if (deny.test(t)) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
                let score = 0;
                if (allow.test(t)) score += 10;
                if (/i understand|mengerti|paham|lanjut|continue|allow|agree/i.test(t)) score += 5;
                if (el.id === 'submit_approve_access') score += 20;
                // primary filled button (Google blue-ish)
                const bg = st.backgroundColor || '';
                if (bg.includes('26, 115, 232') || bg.includes('11, 87, 208') || bg.includes('1a73e8')) score += 8;
                if (score > 0) scored.push({el, t, score});
              }
              scored.sort((a, b) => b.score - a.score);
              if (!scored.length) return '';
              const best = scored[0];
              best.el.scrollIntoView({block: 'center', inline: 'nearest'});
              best.el.click();
              return best.t;
            }"""
        )
        if js_ok:
            return str(js_ok)
    except Exception:
        pass
    return ""


def _session_cookie_from_list(cookies: list) -> str:
    best = ""
    for c in cookies or []:
        if c.get("name") != "session":
            continue
        val = str(c.get("value") or "")
        if len(val) > len(best):
            best = val
    return best


async def _all_cookies(page: Any) -> list:
    try:
        return await page.context.cookies()
    except Exception:
        return []


async def _session_ready(page: Any) -> bool:
    """Auth session after OAuth is long (~400+). Pre-login session is ~150-230 — ignore those."""
    cookies = await _all_cookies(page)
    val = _session_cookie_from_list(cookies)
    # HAR: pre-oauth ~176-224, post-oauth ~432-488
    return len(val) >= 350


async def _get_session_cookie(page: Any) -> str:
    cookies = await _all_cookies(page)
    val = _session_cookie_from_list(cookies)
    if len(val) < 350:
        raise RuntimeError(
            f"session cookie missing/too short after OAuth (len={len(val)})"
        )
    return val


async def oauth_via_browser(
    page: Any, attempt: int, acc: GoogleAccount, browser: Any = None
) -> tuple[str, int, dict]:
    """
    Drive Google OAuth. Returns (session, user_id, user_self_dict).
    """
    email = acc.email
    signup = REFERRAL_URL
    oauth_user: dict[str, Any] = {}

    async def _on_response(resp: Any) -> None:
        nonlocal oauth_user
        try:
            u = resp.url or ""
            if "/api/oauth/google" in u and resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict) and data.get("success"):
                    d = data.get("data")
                    if isinstance(d, dict):
                        oauth_user = d
        except Exception:
            pass

    page.on("response", _on_response)
    # catch popup/new-tab OAuth responses too
    if browser is not None:
        def _bind_page(p: Any) -> None:
            try:
                p.on("response", _on_response)
            except Exception:
                pass

        try:
            browser.on("page", _bind_page)
        except Exception:
            pass

    _log_step(attempt, "nav", f"open {signup[:80]}", email)
    await page.goto(signup, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
    await page.wait_for_timeout(1500)

    # Prefer clicking Google; fallback: build OAuth URL from /api/oauth/state
    clicked = await click_first(
        page,
        [
            "button:has-text('Google')",
            "a:has-text('Google')",
            "[class*='google' i]",
            "button:has-text('Sign up with Google')",
            "button:has-text('Continue with Google')",
            "text=Google",
        ],
        timeout=4000,
    )
    if not clicked:
        _log_step(attempt, "oauth", "no Google button — direct authorize URL", email)
        state_url = f"{BASE_URL}/api/oauth/state?aff={AFF_CODE}"
        resp = await page.request.get(state_url)
        data = await resp.json()
        state = (data.get("data") if isinstance(data, dict) else None) or ""
        if not state:
            raise RuntimeError(f"oauth state empty: {data!r}")
        qs = urlencode(
            {
                "client_id": GOOGLE_CLIENT_ID,
                "redirect_uri": OAUTH_REDIRECT,
                "response_type": "code",
                "scope": "openid profile email",
                "state": state,
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        await page.goto(
            f"https://accounts.google.com/o/oauth2/v2/auth?{qs}",
            wait_until="domcontentloaded",
            timeout=GOTO_TIMEOUT_MS,
        )
    else:
        _log_step(attempt, "oauth", "clicked Google", email)
        # Give navigation / popup a moment before looking for Google UI
        await page.wait_for_timeout(1500)
        try:
            await page.wait_for_url(
                re.compile(r"accounts\.google\.com|getunikey\.ai"),
                timeout=30_000,
            )
        except Exception:
            pass

    page = await _google_login(page, attempt, acc, browser=browser)

    # Wait for long auth session (code exchange may lag behind URL change)
    wait_deadline = time.time() + 45.0
    while time.time() < wait_deadline and not await _session_ready(page):
        if browser is not None:
            for p in list(getattr(browser, "pages", None) or [page]):
                try:
                    if "getunikey.ai" in (p.url or ""):
                        page = p
                        break
                except Exception:
                    pass
        await page.wait_for_timeout(500)

    session = await _get_session_cookie(page)
    _log_step(attempt, "oauth", f"session ok len={len(session)}", email)

    # Headless race: /api/oauth/google response may land AFTER session cookie.
    # Poll briefly for captured oauth body; always re-read oauth_user each loop.
    data: dict[str, Any] = {}
    uid = 0
    for try_i in range(8):
        if oauth_user and isinstance(oauth_user, dict):
            data = dict(oauth_user)
            uid = int(oauth_user.get("id") or 0)
        if uid > 0:
            break
        # Prefer browser cookies (same jar as Camoufox) over bare urllib
        try:
            resp = await page.request.get(f"{BASE_URL}/api/user/self")
            me0 = await resp.json()
            if isinstance(me0, dict) and me0.get("success") and isinstance(me0.get("data"), dict):
                d = me0["data"]
                if int(d.get("id") or 0) > 0:
                    data = d
                    uid = int(d["id"])
                    break
        except Exception:
            pass
        me1 = await api_json(
            "GET",
            f"{BASE_URL}/api/user/self",
            session=session,
            user_id=None,
        )
        if me1.get("success") and isinstance(me1.get("data"), dict):
            d = me1["data"]
            if int(d.get("id") or 0) > 0:
                data = d
                uid = int(d["id"])
                break
        await asyncio.sleep(0.5)

    # OAuth payload alone is enough (HAR: /api/oauth/google returns id)
    if uid <= 0 and oauth_user:
        uid = int(oauth_user.get("id") or 0)
        if uid > 0:
            data = dict(oauth_user)

    if uid <= 0:
        raise RuntimeError(
            f"no user id after oauth: oauth={oauth_user!r} self={data!r}"
        )

    # Enrich profile (optional — don't fail create-key if this flakes under headless)
    try:
        me = await api_json(
            "GET", f"{BASE_URL}/api/user/self", session=session, user_id=uid
        )
        if me.get("success") and isinstance(me.get("data"), dict):
            data = me["data"]
    except Exception as e:
        _log_step(attempt, "user", f"self enrich skip: {e}", email)
        if not data:
            data = dict(oauth_user) if oauth_user else {"id": uid}
    return session, uid, data


def _http_json(
    method: str,
    url: str,
    *,
    session: str,
    user_id: int | None,
    body: dict | None = None,
    timeout: float = 60.0,
) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GetUniKeyFarm/1.0",
        "Cookie": f"session={session}",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/console/token",
    }
    if user_id is not None:
        headers["New-Api-User"] = str(user_id)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            raise RuntimeError(f"HTTP {e.code} {url}: {raw[:300]}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bad json {url}: {raw[:300]}") from e


async def api_json(
    method: str,
    url: str,
    *,
    session: str,
    user_id: int | None,
    body: dict | None = None,
) -> dict:
    return await asyncio.to_thread(
        _http_json, method, url, session=session, user_id=user_id, body=body
    )


async def create_api_key(
    attempt: int, session: str, user_id: int, email: str
) -> tuple[str, int, str]:
    """Create token + reveal key. Returns (api_key, token_id, token_name)."""
    payload = {
        "name": TOKEN_NAME,
        "remain_quota": 0,
        "expired_time": -1,
        "unlimited_quota": True,
        "model_limits_enabled": False,
        "model_limits": "",
        "allow_ips": "",
        "group": "",
        "cross_group_retry": False,
    }
    _log_step(attempt, "token", f"create name={TOKEN_NAME}", email)
    created = await api_json(
        "POST",
        f"{BASE_URL}/api/token/",
        session=session,
        user_id=user_id,
        body=payload,
    )
    if not created.get("success"):
        raise RuntimeError(f"token create failed: {created!r}")

    listed = await api_json(
        "GET",
        f"{BASE_URL}/api/token/?p=1&size=20",
        session=session,
        user_id=user_id,
    )
    items = ((listed.get("data") or {}).get("items")) or []
    if not items:
        raise RuntimeError(f"token list empty after create: {listed!r}")
    # prefer matching name, else newest id
    match = None
    for it in items:
        if str(it.get("name") or "") == TOKEN_NAME:
            match = it
            break
    if match is None:
        match = max(items, key=lambda x: int(x.get("id") or 0))
    token_id = int(match.get("id") or 0)
    if token_id <= 0:
        raise RuntimeError(f"bad token id: {match!r}")

    _log_step(attempt, "token", f"reveal id={token_id}", email)
    revealed = await api_json(
        "POST",
        f"{BASE_URL}/api/token/{token_id}/key",
        session=session,
        user_id=user_id,
    )
    key = ((revealed.get("data") or {}).get("key")) or ""
    if not key:
        raise RuntimeError(f"token key empty: {revealed!r}")
    return key, token_id, str(match.get("name") or TOKEN_NAME)


def _bearer_json(
    method: str,
    path: str,
    api_key: str,
    body: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict | str]:
    """Call public /v1/* with Bearer key. Returns (status, json_or_raw)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "GetUniKeyFarm-Smoke/1.0",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            st = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        st = e.code
    if not raw:
        return st, {}
    try:
        return st, json.loads(raw)
    except json.JSONDecodeError:
        return st, raw[:500]


def smoke_test_api_key(api_key: str) -> dict[str, Any]:
    """
    Verify farmed key via OpenAI-compat API (docs: POST /v1/chat/completions).
    Returns {ok, model, content, status, error?}.
    """
    model = SMOKE_MODEL
    # optional: pick first model if preferred missing
    st_m, models_body = _bearer_json("GET", "/v1/models", api_key, timeout=30.0)
    if st_m == 200 and isinstance(models_body, dict):
        data = models_body.get("data") or []
        ids: list[str] = []
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict):
                    mid = m.get("id") or m.get("model") or m.get("model_name")
                    if mid:
                        ids.append(str(mid))
        if model not in ids and ids:
            # keep preferred if present; else first catalog id
            if not any(model == x or model in x for x in ids):
                model = ids[0]

    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Hello! Reply with one short word only."}
        ],
        "stream": False,
        "max_tokens": 32,
    }
    st, resp = _bearer_json("POST", "/v1/chat/completions", api_key, body=body)
    if st != 200:
        return {
            "ok": False,
            "model": model,
            "status": st,
            "error": str(resp)[:300],
        }
    content = ""
    if isinstance(resp, dict):
        try:
            content = (
                ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            )
        except Exception:
            content = ""
    if not str(content).strip():
        return {
            "ok": False,
            "model": model,
            "status": st,
            "error": f"empty content: {str(resp)[:200]}",
        }
    return {
        "ok": True,
        "model": model,
        "status": st,
        "content": str(content).strip()[:120],
        "usage": (resp.get("usage") if isinstance(resp, dict) else None),
    }


async def smoke_test_api_key_async(
    attempt: int, api_key: str, email: str
) -> dict[str, Any]:
    _log_step(attempt, "smoke", f"POST /v1/chat/completions model={SMOKE_MODEL}", email)
    result = await asyncio.to_thread(smoke_test_api_key, api_key)
    if result.get("ok"):
        _log_step(
            attempt,
            "smoke",
            f"OK model={result.get('model')} reply={result.get('content')!r}",
            email,
        )
    else:
        _log_step(
            attempt,
            "smoke",
            f"FAIL status={result.get('status')} err={result.get('error')}",
            email,
        )
    return result


def fetch_key_usage(api_key: str) -> dict[str, Any]:
    """Bearer usage snapshot after smoke (non-fatal if CF/endpoint fails)."""
    out: dict[str, Any] = {"ok": False}
    # OpenAI-compat billing usage (USD-ish total_usage)
    st, body = _bearer_json(
        "GET", "/v1/dashboard/billing/usage", api_key, timeout=30.0
    )
    out["billing_status"] = st
    if st == 200 and isinstance(body, dict):
        out["ok"] = True
        out["total_usage"] = body.get("total_usage")
        out["billing"] = {
            k: body.get(k) for k in ("object", "total_usage") if k in body
        }
    else:
        out["billing_error"] = str(body)[:200]

    st2, body2 = _bearer_json("GET", "/api/usage/token", api_key, timeout=30.0)
    out["token_status"] = st2
    if st2 == 200 and isinstance(body2, dict):
        data = body2.get("data") if isinstance(body2.get("data"), dict) else body2
        if isinstance(data, dict):
            out["ok"] = True
            out["token_used"] = data.get("total_used")
            out["token_granted"] = data.get("total_granted")
            out["token_available"] = data.get("total_available")
            out["token_name"] = data.get("name")
    else:
        out["token_error"] = str(body2)[:200]
    return out


async def fetch_key_usage_async(
    attempt: int, api_key: str, email: str
) -> dict[str, Any]:
    _log_step(attempt, "usage", "GET billing/usage + /api/usage/token", email)
    result = await asyncio.to_thread(fetch_key_usage, api_key)
    if result.get("ok"):
        _log_step(
            attempt,
            "usage",
            f"OK total_usage={result.get('total_usage')} "
            f"token_used={result.get('token_used')} "
            f"token_available={result.get('token_available')}",
            email,
        )
    else:
        _log_step(
            attempt,
            "usage",
            f"skip/fail billing={result.get('billing_status')} "
            f"token={result.get('token_status')}",
            email,
        )
    return result

async def farm_one(attempt: int) -> bool:
    global _in_flight
    if_lock, can_start = _ensure_async_gates()
    await can_start.wait()
    async with if_lock:
        _in_flight += 1

    acc = _pop_account()
    email = acc.email if acc else ""
    manager = None
    page: Any = None
    try:
        if acc is None:
            _log_step(attempt, "fail", "no Google accounts left in pool")
            await save_fail(
                {
                    "attempt": attempt,
                    "error": "empty_pool",
                    "at": _now_iso(),
                }
            )
            return False

        email = acc.email
        _log_step(attempt, "start", "Starting", email)

        if STUB:
            _log_step(attempt, "stub", "GETUNIKEY_STUB=true — fake OK", email)
            _mark_used(email)
            await save_ok(
                {
                    "email": email,
                    "google_password": acc.password,
                    "api_key": "STUB_KEY",
                    "user_id": 0,
                    "token_id": 0,
                    "token_name": TOKEN_NAME,
                    "aff": AFF_CODE,
                    "created_at": _now_iso(),
                    "batch_id": BATCH_ID,
                    "stub": True,
                }
            )
            _log_step(attempt, "OK", "Stub account", email)
            await _maybe_warp_after_success(attempt)
            return True

        manager, browser, page = await launch_browser()
        session, user_id, me = await oauth_via_browser(
            page, attempt, acc, browser=browser
        )
        _log_step(
            attempt,
            "user",
            f"id={user_id} quota={me.get('quota')} gift={me.get('gift_quota')}",
            email,
        )

        api_key, token_id, tname = await create_api_key(attempt, session, user_id, email)

        smoke: dict[str, Any] = {"ok": None, "skipped": True}
        if SMOKE_TEST:
            smoke = await smoke_test_api_key_async(attempt, api_key, email)
            err_s = str(smoke.get("error") or "")
            if not smoke.get("ok") and (
                "insufficient_user_quota" in err_s
                or "额度不足" in err_s
                or "remaining quota" in err_s.lower()
            ):
                _log_step(
                    attempt,
                    "smoke",
                    "quota USD empty (gift/internal units ≠ billable $) — key still saved",
                    email,
                )

        # Usage snapshot (Bearer) — non-fatal; try even if smoke failed
        usage: dict[str, Any] = {}
        try:
            usage = await fetch_key_usage_async(attempt, api_key, email)
        except Exception as e:
            usage = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            _log_step(attempt, "usage", f"error {usage['error']}", email)

        _mark_used(email)
        result = {
            "email": email,
            "google_password": acc.password,
            "api_key": api_key,
            "user_id": user_id,
            "token_id": token_id,
            "token_name": tname,
            "username": me.get("username"),
            "display_name": me.get("display_name"),
            "quota": me.get("quota"),
            "gift_quota": me.get("gift_quota"),
            "used_quota": me.get("used_quota"),
            "aff_code": me.get("aff_code"),
            "aff": AFF_CODE,
            "smoke_ok": smoke.get("ok"),
            "smoke_model": smoke.get("model"),
            "smoke_reply": smoke.get("content"),
            "smoke_error": smoke.get("error"),
            "usage_after_smoke": usage.get("total_usage"),
            "usage_token_used": usage.get("token_used"),
            "usage_token_available": usage.get("token_available"),
            "usage_ok": usage.get("ok"),
            "created_at": _now_iso(),
            "batch_id": BATCH_ID,
        }
        await save_ok(result)

        # Optional hard-fail AFTER save so key is never lost
        if (
            SMOKE_TEST
            and SMOKE_REQUIRE
            and not smoke.get("ok")
            and smoke.get("skipped") is not True
        ):
            _log_step(
                attempt,
                "fail",
                f"smoke required but failed (key saved …{api_key[-6:]}) "
                f"status={smoke.get('status')} err={smoke.get('error')}",
                email,
            )
            return False

        smoke_tag = ""
        if SMOKE_TEST:
            smoke_tag = " smoke=OK" if smoke.get("ok") else " smoke=FAIL"
        _log_step(
            attempt,
            "OK",
            f"key …{api_key[-6:]} id={token_id}{smoke_tag}",
            email,
        )
        await _maybe_warp_after_success(attempt)
        return True

    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _log_step(attempt, "fail", msg, email)
        if page is not None:
            try:
                await screenshot(page, attempt, "fail")
            except Exception:
                pass
        await save_fail(
            {
                "attempt": attempt,
                "email": email,
                "error": msg,
                "at": _now_iso(),
            }
        )
        # Only burn account on permanent credential errors (not interstitials/timeouts)
        if acc and any(
            x in msg.lower()
            for x in ("wrong password", "account not found", "2fa")
        ):
            _mark_used(acc.email)
        return False
    finally:
        await _close_browser(manager)
        async with if_lock:
            _in_flight -= 1


async def run_batch(count: int, concurrent: int) -> int:
    """count<=0 means run entire unused pool (Google list size)."""
    global CONCURRENT
    CONCURRENT = max(1, concurrent)
    # init with large target so meta records intent; real n from pool
    init_batch(max(count, 1), CONCURRENT)
    if not STUB and not _account_pool:
        print(
            "[getunikey] ERROR: no unused Google accounts "
            "(HUD list / google_accounts.txt empty or all used)",
            flush=True,
        )
        return 2

    pool_n = len(_account_pool) if not STUB else max(count, 1)
    # 0 / negative → entire pool; else cap at pool
    if count <= 0:
        n = pool_n
    else:
        n = min(count, pool_n)
        if n < count and not STUB:
            print(
                f"[getunikey] WARN: requested n={count} but pool only {n} — running {n}",
                flush=True,
            )
    if n <= 0:
        return 2

    print(
        f"[getunikey] run n={n} (pool={pool_n}) c={CONCURRENT} "
        f"referral={REFERRAL_URL!r} aff={AFF_CODE!r}",
        flush=True,
    )

    sem = asyncio.Semaphore(CONCURRENT)
    ok_n = 0
    fail_n = 0

    async def _wrap(i: int) -> None:
        nonlocal ok_n, fail_n
        if SPAWN_DELAY > 0 and i > 1:
            await asyncio.sleep(SPAWN_DELAY * (i - 1) / max(1, CONCURRENT))
        async with sem:
            if await farm_one(i):
                ok_n += 1
            else:
                fail_n += 1

    await asyncio.gather(*(_wrap(i) for i in range(1, n + 1)))
    print(
        f"[{_ts()}] [hub] batch done ok={ok_n} fail={fail_n} n={n} c={CONCURRENT} "
        f"dir={BATCH_DIR}",
        flush=True,
    )
    return 0 if fail_n == 0 else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GetUniKey farm — Google OAuth")
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=0,
        help="accounts to farm; 0 = entire unused pool (default)",
    )
    p.add_argument(
        "-c",
        "--concurrent",
        type=int,
        default=_env_int("GETUNIKEY_CONCURRENT") or _env_int("CONCURRENT") or 1,
    )
    p.add_argument("-y", "--yes", action="store_true", help="non-interactive")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    n = int(args.count)  # 0 = all pool
    c = max(1, int(args.concurrent))
    every = _effective_warp_every_n()
    pool_n = len(
        [
            a
            for a in load_google_accounts()
            if a.email.lower() not in _load_used()
        ]
    )
    list_src = "HUD_LIST" if (ACCOUNTS_LIST_RAW or "").strip() else ACCOUNTS_FILE.name
    print(
        f"[getunikey] n={n if n > 0 else 'pool'} c={c} warp_every_n={every} "
        f"headless={HEADLESS} aff={AFF_CODE!r} stub={STUB} pool~{pool_n} "
        f"src={list_src} referral={REFERRAL_URL[:60]}",
        flush=True,
    )
    if not args.yes and sys.stdin.isatty():
        label = str(n) if n > 0 else f"all {pool_n}"
        ans = input(f"Run {label} accounts (c={c})? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted", flush=True)
            return 2
    return asyncio.run(run_batch(n, c))


if __name__ == "__main__":
    raise SystemExit(main())
