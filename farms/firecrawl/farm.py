"""
Firecrawl farm — Google OAuth signup.

Flow per account:
  1. Load Google account (email:password from pool file)
  2. Camoufox browser → firecrawl.dev → "Continue with Google"
  3. Google login (email → password → optional TOS speedbump)
  4. OAuth consent auto-granted → redirect back to Firecrawl
  5. Supabase session set → onboarding (accept terms + profile + credits)
  6. Extract API key from /api/user/team
  7. Save result (email + apiKey + teamId)

Config: FIRECRAWL_* env keys (hub .env maps shared → FIRECRAWL_*).
Run:    python -m jobs run firecrawl -- -n 5 -c 1 -y
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed. pip install camoufox[geoip]", flush=True)
    sys.exit(1)

def _is_on_firecrawl(url: str) -> bool:
    """Check hostname, not substring (Google URLs contain firecrawl.dev in params)."""
    try:
        host = urlparse(url).hostname or ""
        return host.endswith("firecrawl.dev")
    except Exception:
        return False


# ── Config ───────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()

def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")

HEADLESS = _env_bool("FIRECRAWL_HEADLESS", True)
CONCURRENT = int(_env("FIRECRAWL_CONCURRENT", "1") or "1")
ACCOUNT_TIMEOUT_S = int(_env("FIRECRAWL_ACCOUNT_TIMEOUT", "180") or "180")

# Google accounts pool file: one line = email:password or email|password
GOOGLE_ACCOUNTS_FILE = Path(
    _env("FIRECRAWL_GOOGLE_ACCOUNTS", str(_ROOT / "google_accounts.txt"))
)

# WARP
WARP_EVERY_N = max(0, int(_env("FIRECRAWL_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0"))
WARP_SETTLE_S = max(3.0, float(_env("WARP_SETTLE_AFTER") or "8"))

# Results
RESULTS_ROOT = Path(_env("FIRECRAWL_RESULTS_DIR", str(_ROOT / "results")))
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR = _ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Firecrawl
FIRECRAWL_LOGIN_URL = "https://www.firecrawl.dev/signin"
FIRECRAWL_ONBOARDING_URL = "https://www.firecrawl.dev/onboarding"

# ── WARP ─────────────────────────────────────────────────────────────────────
_warp_ok_counter = 0
_warp_lock = asyncio.Lock() if sys.platform != "win32" else None  # lazy init


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


# ── Google Account Pool ──────────────────────────────────────────────────────
def load_google_accounts() -> list[tuple[str, str]]:
    """Load email:password pairs from pool file."""
    if not GOOGLE_ACCOUNTS_FILE.is_file():
        print(f"[POOL] WARN: {GOOGLE_ACCOUNTS_FILE} not found", flush=True)
        return []
    accounts: list[tuple[str, str]] = []
    for line in GOOGLE_ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # formats: email:pass  or  email|pass  or  email\tpass
        for sep in (":", "|", "\t"):
            if sep in line:
                parts = line.split(sep, 1)
                if len(parts) == 2 and "@" in parts[0]:
                    accounts.append((parts[0].strip(), parts[1].strip()))
                    break
    return accounts


def _load_used_emails() -> set[str]:
    """Emails already farmed (from results)."""
    used: set[str] = set()
    for f in RESULTS_ROOT.glob("batch_*/accounts.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for row in data:
                if isinstance(row, dict):
                    e = (row.get("google_email") or row.get("email") or "").lower()
                    if e:
                        used.add(e)
        except Exception:
            pass
    # Also check used_emails.txt
    uf = RESULTS_ROOT / "used_emails.txt"
    if uf.is_file():
        for line in uf.read_text(encoding="utf-8").splitlines():
            e = line.strip().lower()
            if e and not e.startswith("#"):
                used.add(e)
    return used


# ── Browser Helpers ──────────────────────────────────────────────────────────
async def launch_browser():
    kwargs: dict[str, Any] = {
        "headless": HEADLESS,
        "humanize": 0.5,
        "os": random.choice(["windows", "macos"]),
        "locale": "en-US",
        "geoip": True,
        "block_webrtc": True,
    }
    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(60000)
    return manager, browser, page


async def screenshot(page, attempt: int, tag: str) -> None:
    try:
        path = SCREENSHOT_DIR / f"firecrawl_{attempt}_{tag}.png"
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


async def safe_click(page, selector: str, timeout: int = 10000) -> bool:
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        return True
    except Exception:
        return False


async def safe_fill(page, selector: str, value: str, timeout: int = 10000) -> bool:
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        await loc.fill(value)
        return True
    except Exception:
        return False


# ── Core Flow ────────────────────────────────────────────────────────────────
async def farm_one_account(
    attempt: int,
    google_email: str,
    google_password: str,
) -> dict | None:
    """Run full Firecrawl signup for one Google account. Returns result dict or None."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{attempt}] start  google_login  {google_email}", flush=True)

    manager, browser, page = await launch_browser()
    try:
        result = await _do_firecrawl_flow(page, attempt, google_email, google_password)
        return result
    except asyncio.TimeoutError:
        await screenshot(page, attempt, "timeout")
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{attempt}] fail  timeout  {google_email}", flush=True)
        return None
    except Exception as e:
        await screenshot(page, attempt, "error")
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{attempt}] fail  {type(e).__name__}: {e}  {google_email}", flush=True)
        return None
    finally:
        try:
            await browser.close()
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


async def _do_firecrawl_flow(
    page, attempt: int, google_email: str, google_password: str
) -> dict | None:
    """Inner flow: navigate → Google OAuth → onboarding → extract API key."""

    # Step 1: Navigate to Firecrawl signin
    _log(attempt, "navigate", "firecrawl.dev/signin", google_email)
    await page.goto(FIRECRAWL_LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Step 2: Click "Continue with Google" button
    _log(attempt, "click_google", "finding Google OAuth button", google_email)
    google_clicked = False
    for selector in (
        'button:has-text("Google")',
        'a:has-text("Google")',
        'button:has-text("Continue with Google")',
        'a:has-text("Continue with Google")',
        '[data-provider="google"]',
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                google_clicked = True
                break
        except Exception:
            continue

    if not google_clicked:
        # Fallback: JS click anything mentioning Google
        google_clicked = await page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button, a, [role="button"]')];
            for (const b of btns) {
                const txt = (b.innerText || b.textContent || '').toLowerCase();
                if (txt.includes('google')) { b.click(); return true; }
            }
            return false;
        }""")

    if not google_clicked:
        await screenshot(page, attempt, "no_google_btn")
        _log(attempt, "fail", "no Google button found", google_email)
        return None

    # Step 3: Google login — wait for accounts.google.com
    _log(attempt, "google_login", "waiting for Google login page", google_email)
    await page.wait_for_url("**/accounts.google.com/**", timeout=15000)
    await asyncio.sleep(2)

    # Enter email
    _log(attempt, "google_email", "entering email", google_email)
    email_filled = await safe_fill(page, 'input[type="email"]', google_email, timeout=10000)
    if not email_filled:
        # Sometimes it's identifier input
        email_filled = await safe_fill(page, '#identifierId', google_email, timeout=5000)
    if not email_filled:
        await screenshot(page, attempt, "no_email_input")
        _log(attempt, "fail", "cannot find Google email input", google_email)
        return None

    # Click Next
    await asyncio.sleep(0.5)
    await _click_next_google(page)
    await asyncio.sleep(3)

    # Check if already redirected (Google auto-consented without password)
    if _is_on_firecrawl(page.url):
        _log(attempt, "auto_consent", "Google auto-granted, skipped password", google_email)
    elif "accounts.google.com" in page.url:
        _log(attempt, "google_password", "entering password", google_email)
        pw_filled = False
        for sel in ('input[type="password"]', 'input[name="Passwd"]', 'input[aria-label="Enter your password"]'):
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=10000)
                await loc.click()
                await loc.fill(google_password)
                pw_filled = True
                break
            except Exception:
                continue

        if not pw_filled:
            await screenshot(page, attempt, "no_pw_input")
            _log(attempt, "fail", "cannot find Google password input", google_email)
            return None

        await asyncio.sleep(0.5)
        await _click_next_google(page)
        await asyncio.sleep(3)

    # Step 4: Handle Google interstitials until we land on firecrawl.dev
    _log(attempt, "interstitials", "handling Google pages", google_email)
    deadline = time.time() + 60
    while time.time() < deadline:
        await asyncio.sleep(1)
        url = page.url

        if _is_on_firecrawl(url):
            break

        if "speedbump/workspacetermsofservice" in url:
            _log(attempt, "google_tos", "accepting workspace TOS", google_email)
            await _accept_workspace_tos(page)
            await asyncio.sleep(3)
            continue

        if "signin/oauth/consent" in url or "signin/oauth/id" in url:
            _log(attempt, "oauth_consent", "handling consent", google_email)
            for btn_sel in (
                'button:has-text("Lanjutkan")',
                'button:has-text("Continue")',
                'button:has-text("Allow")',
                'button:has-text("Izinkan")',
                '#submit_approve_access',
            ):
                try:
                    loc = page.locator(btn_sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        break
                except Exception:
                    continue
            else:
                await page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button, [role="button"]')];
                    for (const b of btns) {
                        const txt = (b.innerText || '').toLowerCase();
                        if (txt.includes('lanjutkan') || txt.includes('continue') || txt.includes('allow') || txt.includes('izinkan')) {
                            b.click(); return;
                        }
                    }
                }""")
            await asyncio.sleep(3)
            continue

        if "challenge" in url:
            _log(attempt, "fail", "Google 2FA/challenge detected", google_email)
            await screenshot(page, attempt, "2fa_challenge")
            return None

    if not _is_on_firecrawl(page.url):
        await screenshot(page, attempt, "no_redirect")
        _log(attempt, "fail", f"not redirected to firecrawl (url={page.url[:80]})", google_email)
        return None

    # Wait for auth callback exchange to finish (sets sb-* cookies)
    _log(attempt, "auth_settle", f"url={page.url[:80]}", google_email)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    # Wait until URL settles past /auth/callback
    for _ in range(20):
        cur = page.url
        if _is_on_firecrawl(cur) and "/auth/callback" not in cur:
            break
        await asyncio.sleep(1)
    await page.wait_for_load_state("domcontentloaded", timeout=15000)
    await asyncio.sleep(2)
    _log(attempt, "settled", f"url={page.url[:80]}", google_email)

    url = page.url

    # Step 6: Complete onboarding if landed there
    if "onboarding" in url:
        _log(attempt, "onboarding", "completing onboarding steps", google_email)
        await _complete_onboarding(page, attempt, google_email)
        await asyncio.sleep(2)

    # Step 7: Extract API key via /api/user/team
    _log(attempt, "extract_key", "fetching API key", google_email)
    api_data = await _fetch_api_key(page)

    if not api_data or not api_data.get("apiKey"):
        # Try navigating to app dashboard and retry
        _log(attempt, "retry_key", "navigating to app, retrying key fetch", google_email)
        try:
            await page.goto("https://www.firecrawl.dev/app", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            api_data = await _fetch_api_key(page)
        except Exception:
            pass

    if not api_data or not api_data.get("apiKey"):
        await screenshot(page, attempt, "no_api_key")
        _log(attempt, "fail", "could not extract API key", google_email)
        return None

    # Success!
    ts = datetime.now().strftime("%H:%M:%S")
    api_key = api_data["apiKey"]
    team_id = api_data.get("teamId", "")
    print(f"[{ts}] [{attempt}] OK  apiKey={api_key[:12]}...  {google_email}", flush=True)

    return {
        "google_email": google_email,
        "api_key": api_key,
        "team_id": team_id,
        "api_keys": api_data.get("apiKeys", []),
        "farmed_at": datetime.now(timezone.utc).isoformat(),
    }


async def _click_next_google(page) -> None:
    """Click Next/Submit on Google login forms."""
    for sel in (
        '#identifierNext',
        '#passwordNext',
        'button:has-text("Next")',
        'button:has-text("Berikutnya")',  # Indonesian locale
        'div#identifierNext',
        'div#passwordNext',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                return
        except Exception:
            continue
    # Fallback: press Enter
    await page.keyboard.press("Enter")


async def _accept_workspace_tos(page) -> None:
    """Accept Google Workspace Terms of Service speedbump.
    
    The 'I understand' button is typically below the fold — scroll first.
    """
    # Scroll to bottom so button becomes visible
    for _ in range(5):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)

        # Try clicking the button
        for sel in (
            'button:has-text("I understand")',
            'button:has-text("Accept")',
            'button:has-text("Saya memahami")',
            'button:has-text("Continue")',
            '#accept',
            'button:has-text("Got it")',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    return
            except Exception:
                continue

        # JS fallback: scroll + click
        clicked = await page.evaluate("""() => {
            window.scrollTo(0, document.body.scrollHeight);
            const btns = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')];
            for (const b of btns) {
                const txt = (b.innerText || b.value || '').toLowerCase();
                if (txt.includes('i understand') || txt.includes('understand') || 
                    txt.includes('accept') || txt.includes('got it')) {
                    b.scrollIntoView(); b.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            return


async def _complete_onboarding(page, attempt: int, email: str) -> None:
    await asyncio.sleep(2)

    for step in range(8):
        url = page.url
        if "onboarding" not in url:
            break

        _log(attempt, "onboarding_step", f"step={step} url={url.split('?')[-1][:40]}", email)

        # Select a role if role list visible (e.g. "Developer / Engineer")
        try:
            for role in ("Developer", "Researcher", "Founder"):
                loc = page.locator(f'text="{role}"').first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    await asyncio.sleep(0.5)
                    break
        except Exception:
            pass

        await page.evaluate("""() => {
            window.scrollTo(0, document.body.scrollHeight);
            // Custom toggle: button with aria-label containing "Terms of Service" or "I agree"
            const btns = document.querySelectorAll('button[aria-label]');
            for (const b of btns) {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('terms of service') || label.includes('i agree') || label.includes('privacy policy')) {
                    b.scrollIntoView({block: 'center'});
                    b.click();
                }
            }
            // Also try role="switch" fallback
            const switches = document.querySelectorAll('button[role="switch"]');
            for (const sw of switches) {
                const state = sw.getAttribute('aria-checked') || sw.getAttribute('data-state') || '';
                if (state !== 'true' && state !== 'checked') {
                    sw.scrollIntoView({block: 'center'});
                    sw.click();
                }
            }
        }""")
        await asyncio.sleep(0.5)

        # Click Continue/Next
        await _click_onboarding_next(page)
        await asyncio.sleep(3)


async def _click_onboarding_next(page, skip: bool = False) -> None:
    """Click the primary action button on onboarding pages."""
    keywords = ["Get Started", "Continue", "Next", "Skip", "Start", "Submit", "Done"]
    if skip:
        keywords = ["Skip", "Continue", "Next", "Done"]

    for kw in keywords:
        try:
            loc = page.get_by_role("button", name=kw)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click()
                return
        except Exception:
            continue

    # Fallback: click first visible button-like thing that's not a nav link
    await page.evaluate("""(keywords) => {
        const btns = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')];
        for (const kw of keywords) {
            for (const b of btns) {
                const txt = (b.innerText || b.value || '').trim().toLowerCase();
                if (txt.includes(kw.toLowerCase())) { b.click(); return; }
            }
        }
        // Last resort: first visible button
        for (const b of btns) {
            const rect = b.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) { b.click(); return; }
        }
    }""", keywords)


async def _fetch_api_key(page) -> dict | None:
    try:
        result = await page.evaluate("""async () => {
            try {
                const resp = await fetch('/api/user/team', {credentials: 'include'});
                const text = await resp.text();
                try { return JSON.parse(text); } catch(e) { return {_raw: text, _status: resp.status}; }
            } catch(e) { return {_error: e.message}; }
        }""")
        if result and "_raw" in (result or {}):
            print(f"[DEBUG] /api/user/team raw: {str(result)[:200]}", flush=True)
            return None
        if result and "_error" in (result or {}):
            print(f"[DEBUG] /api/user/team error: {result['_error']}", flush=True)
            return None
        return result
    except Exception as e:
        print(f"[DEBUG] _fetch_api_key exception: {e}", flush=True)
        return None


# ── Logging ──────────────────────────────────────────────────────────────────
def _log(attempt: int, step: str, message: str, email: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    suffix = f"  {email}" if email else ""
    print(f"[{ts}] [{attempt}] {step}  {message}{suffix}", flush=True)


# ── Batch / Results ──────────────────────────────────────────────────────────
def init_batch(count: int, concurrent: int) -> tuple[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = secrets.token_hex(3)
    batch_id = f"{stamp}_{short}"
    batch_dir = RESULTS_ROOT / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "accounts.json").write_text("[]\n", encoding="utf-8")
    meta = {
        "batch_id": batch_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "count": count,
        "concurrent": concurrent,
    }
    (batch_dir / "batch_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[BATCH] id={batch_id} dir={batch_dir}", flush=True)
    return batch_id, batch_dir


def save_result(batch_dir: Path, result: dict) -> None:
    """Append result to batch accounts.json and persist used email."""
    accounts_file = batch_dir / "accounts.json"
    try:
        data = json.loads(accounts_file.read_text(encoding="utf-8"))
    except Exception:
        data = []
    data.append(result)
    accounts_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # Also write one-liner to accounts.txt
    txt_file = batch_dir / "accounts.txt"
    line = f"{result['google_email']}|{result['api_key']}|{result['team_id']}\n"
    with open(txt_file, "a", encoding="utf-8") as f:
        f.write(line)

    # Persist to global used_emails
    uf = RESULTS_ROOT / "used_emails.txt"
    with open(uf, "a", encoding="utf-8") as f:
        f.write(result["google_email"].lower() + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────
import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Firecrawl Google-OAuth farm")
    p.add_argument("-n", "--count", type=int, default=1, help="accounts this run")
    p.add_argument("-c", "--concurrent", type=int, default=1, help="parallel workers")
    p.add_argument("-y", "--yes", action="store_true", help="non-interactive")
    return p.parse_args(argv)


async def run_farm(count: int, concurrent: int) -> None:
    global CONCURRENT
    CONCURRENT = concurrent

    # Load pool
    all_accounts = load_google_accounts()
    if not all_accounts:
        print("[FATAL] No Google accounts found. Create google_accounts.txt (email:password per line)", flush=True)
        sys.exit(1)

    # Filter out already-used
    used = _load_used_emails()
    available = [(e, p) for e, p in all_accounts if e.lower() not in used]
    if not available:
        print(f"[FATAL] All {len(all_accounts)} accounts already used. Add more to google_accounts.txt", flush=True)
        sys.exit(1)

    actual_count = min(count, len(available))
    if actual_count < count:
        print(f"[WARN] Only {actual_count} unused accounts available (requested {count})", flush=True)

    batch_id, batch_dir = init_batch(actual_count, concurrent)
    accounts_to_farm = available[:actual_count]

    ok_count = 0
    fail_count = 0
    sem = asyncio.Semaphore(concurrent)

    async def worker(idx: int, email: str, password: str) -> None:
        nonlocal ok_count, fail_count
        async with sem:
            result = await asyncio.wait_for(
                farm_one_account(idx, email, password),
                timeout=ACCOUNT_TIMEOUT_S,
            )
            if result:
                save_result(batch_dir, result)
                ok_count += 1
                await _maybe_warp_after_success()
            else:
                fail_count += 1

    tasks = []
    for i, (email, password) in enumerate(accounts_to_farm, 1):
        tasks.append(asyncio.create_task(worker(i, email, password)))
        if i < len(accounts_to_farm):
            await asyncio.sleep(random.uniform(1.5, 3.0))

    await asyncio.gather(*tasks, return_exceptions=True)

    # Summary
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] [DONE] ok={ok_count} fail={fail_count} batch={batch_id}", flush=True)
    print(f"[{ts}] [DONE] results: {batch_dir}", flush=True)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    count = args.count
    concurrent = args.concurrent

    if not args.yes:
        print(f"  Firecrawl farm: {count} accounts, concurrent={concurrent}")
        confirm = input("  Start? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.", flush=True)
            return

    print(f"[firecrawl] plan: n={count} c={concurrent}", flush=True)
    asyncio.run(run_farm(count, concurrent))


if __name__ == "__main__":
    main()
