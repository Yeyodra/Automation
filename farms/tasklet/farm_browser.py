"""
Tasklet farm — Google OAuth signup + onboarding.

Flow per account:
  1. Load Google account (email:password from pool file)
  2. Camoufox browser → tasklet.ai/login → "Continue with Google"
  3. Google login (email → password → optional TOS speedbump → consent)
  4. Wait for redirect to tasklet.ai/oauth2callback?code=...
  5. HTTP: POST /api/signIn (exchange code → sessionToken)
  6. HTTP: POST /api/organization/create (auto pro_trial 7d, 5M credits)
  7. HTTP: POST /api/billing/claimDailyBonus (+600K credits)
  8. Save result (sessionToken + credits info)

Config: TASKLET_* env keys (hub .env maps shared → TASKLET_*).
Run:    python -m jobs run tasklet -- -n 5 -c 1 -y
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. pip install httpx", flush=True)
    sys.exit(1)


# ── Config ───────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


HEADLESS = _env_bool("TASKLET_HEADLESS", True)
CONCURRENT = int(_env("TASKLET_CONCURRENT", "1") or "1")
ACCOUNT_TIMEOUT_S = int(_env("TASKLET_ACCOUNT_TIMEOUT", "180") or "180")

# Google accounts pool file
GOOGLE_ACCOUNTS_FILE = Path(
    _env("TASKLET_GOOGLE_ACCOUNTS", str(_ROOT / "google_accounts.txt"))
)

# WARP
WARP_EVERY_N = max(0, int(_env("TASKLET_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0"))
WARP_SETTLE_S = max(3.0, float(_env("WARP_SETTLE_AFTER") or "8"))

# Results
RESULTS_ROOT = Path(_env("TASKLET_RESULTS_DIR", str(_ROOT / "results")))
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR = _ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Tasklet
TASKLET_LOGIN_URL = "https://tasklet.ai/login"
TASKLET_API_BASE = "https://api.tasklet.ai"


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
        path = SCREENSHOT_DIR / f"tasklet_{attempt}_{tag}.png"
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


# ── Google OAuth Browser Flow ────────────────────────────────────────────────
async def google_oauth_get_code(page, browser, email: str, password: str, attempt: int) -> dict | None:
    """Browser flow: login → Google OAuth → onboarding. Returns signIn response dict or None."""
    ts = lambda: datetime.now().strftime("%H:%M:%S")

    # Intercept /api/signIn response to capture sessionToken
    sign_in_data: dict = {}

    async def _capture_sign_in(route):
        response = await route.fetch()
        body = await response.body()
        try:
            data = json.loads(body)
            if data.get("type") == "success":
                sign_in_data.update(data)
        except Exception:
            pass
        await route.fulfill(response=response)

    await page.route("**/api/signIn", _capture_sign_in)

    # 1. Navigate to tasklet login
    await page.goto(TASKLET_LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(random.randint(1500, 2500))
    await screenshot(page, attempt, "login_page")

    # 2. Click "Continue with Google" — handle popup OR redirect
    google_btn = page.locator(
        "button:has-text('Google'), "
        "a:has-text('Google'), "
        "[data-provider='google'], "
        "button:has-text('Continue with Google'), "
        "a:has-text('Continue with Google')"
    )
    if not await google_btn.count():
        print(f"[{ts()}] [{attempt}] FAIL no Google button", flush=True)
        await screenshot(page, attempt, "no_google_btn")
        return None

    # Try popup first, fall back to same-tab redirect
    google_page = None
    pages_before = page.context.pages[:]
    await google_btn.first.click()
    await page.wait_for_timeout(3000)

    google_page = None
    for p in page.context.pages:
        if p not in pages_before and "accounts.google.com" in (p.url or ""):
            google_page = p
            break
    if not google_page:
        if "accounts.google.com" in page.url:
            google_page = page
        else:
            await page.wait_for_timeout(3000)
            for p in page.context.pages:
                if p not in pages_before:
                    google_page = p
                    break
    if not google_page:
        google_page = page
        try:
            await page.wait_for_url("**/accounts.google.com/**", timeout=10000)
        except Exception:
            pass

    await google_page.wait_for_load_state("load")
    await google_page.bring_to_front()
    await google_page.wait_for_timeout(2000)
    print(f"[{ts()}] [{attempt}] google page: {google_page.url[:80]}", flush=True)

    await screenshot(google_page, attempt, "google_signin")

    # 3. Google email input
    try:
        email_input = google_page.locator('#identifierId, input[type="email"], input[name="identifier"]').first
        await email_input.wait_for(state="visible", timeout=15000)
        await email_input.fill(email)
        await google_page.wait_for_timeout(500)
        await google_page.locator('#identifierNext button, #identifierNext').first.click()
        await google_page.wait_for_timeout(random.randint(2000, 3500))
    except Exception as e:
        print(f"[{ts()}] [{attempt}] FAIL google email: {e}", flush=True)
        await screenshot(google_page, attempt, "google_email_fail")
        return None

    # 4. Google password input
    try:
        pass_input = google_page.locator('input[type="password"], input[name="Passwd"]').first
        await pass_input.wait_for(state="visible", timeout=15000)
        await pass_input.fill(password)
        await google_page.wait_for_timeout(500)
        await google_page.locator('#passwordNext button, #passwordNext').first.click()
        await google_page.wait_for_timeout(random.randint(2500, 4000))
    except Exception as e:
        print(f"[{ts()}] [{attempt}] FAIL google password: {e}", flush=True)
        await screenshot(google_page, attempt, "google_pass_fail")
        return None

    # 5. Handle TOS speedbump (Workspace terms of service — "I understand")
    try:
        accept_btn = google_page.locator(
            "button:has-text('I understand'), "
            "button:has-text('I agree'), "
            "button:has-text('Accept'), "
            "#accept"
        )
        await accept_btn.first.wait_for(state="visible", timeout=8000)
        await accept_btn.first.click()
        print(f"[{ts()}] [{attempt}] TOS accepted", flush=True)
        await google_page.wait_for_timeout(random.randint(2000, 3000))
    except Exception:
        pass

    # 6. Handle OAuth consent screen — scroll down then click Continue/Lanjutkan
    try:
        await google_page.wait_for_timeout(2000)
        await google_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await google_page.wait_for_timeout(1000)

        consent_btn = google_page.locator(
            "button:has-text('Continue'), "
            "button:has-text('Lanjutkan'), "
            "button:has-text('Allow'), "
            "button:has-text('Izinkan'), "
            "#submit_approve_access"
        )
        await consent_btn.first.wait_for(state="visible", timeout=10000)
        await consent_btn.first.click()
        print(f"[{ts()}] [{attempt}] consent granted", flush=True)
        await google_page.wait_for_timeout(random.randint(2000, 3000))
    except Exception:
        pass

    # 7. Popup done → wait for main page to redirect. If stuck on login, re-do OAuth.
    await page.bring_to_front()
    await page.wait_for_timeout(random.randint(5000, 7000))

    if "/login" in page.url or "Sign in" in (await page.title()):
        print(f"[{ts()}] [{attempt}] main page still login, re-clicking Google", flush=True)
        google_btn2 = page.locator(
            "button:has-text('Google'), "
            "a:has-text('Google'), "
            "button:has-text('Sign in with Google')"
        )
        if await google_btn2.count() > 0:
            pages_before2 = page.context.pages[:]
            await google_btn2.first.click()
            await page.wait_for_timeout(3000)

            retry_page = None
            for p in page.context.pages:
                if p not in pages_before2 and "accounts.google.com" in (p.url or ""):
                    retry_page = p
                    break
            if retry_page:
                await retry_page.wait_for_load_state("load")
                await retry_page.bring_to_front()
                await retry_page.wait_for_timeout(2000)

                # Could be account chooser OR consent screen
                account_row = retry_page.locator(
                    f"li:has-text('{email}'), "
                    f"div[data-identifier='{email}'], "
                    f"*:has-text('{email}'):not(body):not(html)"
                ).first
                try:
                    await account_row.wait_for(state="visible", timeout=5000)
                    await account_row.click()
                    print(f"[{ts()}] [{attempt}] account chooser: selected", flush=True)
                    await retry_page.wait_for_timeout(3000)
                except Exception:
                    pass

                # After account chooser (or directly) → consent screen: scroll + click
                await retry_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await retry_page.wait_for_timeout(1000)
                consent_retry = retry_page.locator(
                    "button:has-text('Continue'), "
                    "button:has-text('Lanjutkan'), "
                    "button:has-text('Allow'), "
                    "button:has-text('Izinkan')"
                )
                try:
                    await consent_retry.first.wait_for(state="visible", timeout=10000)
                    await consent_retry.first.click()
                    print(f"[{ts()}] [{attempt}] retry consent granted", flush=True)
                except Exception:
                    await screenshot(retry_page, attempt, "retry_fail")

                await page.bring_to_front()
                await page.wait_for_timeout(5000)

    await page.wait_for_timeout(3000)

    # 8. Handle "Create organization" screen (appears right after OAuth)
    try:
        create_org_btn = page.locator("button:has-text('Create organization')")
        await create_org_btn.first.wait_for(state="visible", timeout=10000)
        await create_org_btn.first.click()
        print(f"[{ts()}] [{attempt}] onboarding: Create organization clicked", flush=True)
        await page.wait_for_timeout(3000)
    except Exception:
        pass

    # 9. Handle "Help improve Tasklet" → click "Get started"
    try:
        get_started = page.locator("button:has-text('Get started'), button:has-text('Mulai')")
        await get_started.first.wait_for(state="visible", timeout=10000)
        await get_started.first.click()
        print(f"[{ts()}] [{attempt}] onboarding: Get started clicked", flush=True)
        await page.wait_for_timeout(2000)
    except Exception:
        pass

    # 10. Handle any remaining onboarding step
    try:
        next_btn = page.locator(
            "button:has-text('Get started'), "
            "button:has-text('Continue'), "
            "button:has-text('Lanjutkan'), "
            "button:has-text('Next'), "
            "button:has-text('Done'), "
            "button:has-text('Skip')"
        )
        if await next_btn.count() > 0:
            await next_btn.first.click()
            print(f"[{ts()}] [{attempt}] onboarding: next step clicked", flush=True)
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    await page.wait_for_timeout(2000)
    await screenshot(page, attempt, "post_onboarding")

    if sign_in_data and sign_in_data.get("sessionToken"):
        return sign_in_data

    # Fallback: try localStorage
    token = await _extract_session_token(page)
    if token:
        return {"sessionToken": token, "type": "success"}

    return None


# ── Tasklet API (pure HTTP) ──────────────────────────────────────────────────
async def tasklet_sign_in(client: httpx.AsyncClient, code: str) -> dict | None:
    """Exchange OAuth code for session token."""
    resp = await client.post(
        f"{TASKLET_API_BASE}/api/signIn",
        json={
            "type": "oauth2code",
            "provider": "google",
            "code": code,
            "attributionHistory": [],
            "allowDuplicate": False,
        },
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("type") != "success":
        return None
    return data


async def tasklet_create_org(client: httpx.AsyncClient, token: str, name: str) -> dict | None:
    """Create organization → triggers pro_trial."""
    resp = await client.post(
        f"{TASKLET_API_BASE}/api/organization/create",
        json={"name": f"{name}'s organization"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        return None
    return resp.json()


async def tasklet_claim_daily_bonus(client: httpx.AsyncClient, token: str, org_id: str) -> bool:
    """Claim daily bonus credits."""
    resp = await client.post(
        f"{TASKLET_API_BASE}/api/billing/claimDailyBonus",
        json={"organizationId": org_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        return False
    return resp.json().get("claimed", False)


async def tasklet_get_credits(client: httpx.AsyncClient, token: str, org_id: str) -> dict | None:
    """Get credit grants for verification."""
    resp = await client.post(
        f"{TASKLET_API_BASE}/api/billing/creditGrants",
        json={"organizationId": org_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        return None
    return resp.json()


# ── Extract session from browser ─────────────────────────────────────────────
async def _extract_session_token(page) -> str | None:
    """Search localStorage, cookies, and JSON blobs for the 128-char hex sessionToken."""
    import re
    token_re = re.compile(r"[0-9a-f]{64,}", re.IGNORECASE)

    all_storage = await page.evaluate("""() => {
        const out = {};
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            out[k] = localStorage.getItem(k);
        }
        return out;
    }""")

    # Direct key match
    for key in ("sessionToken", "session_token", "token", "auth_token", "access_token"):
        val = (all_storage or {}).get(key, "")
        if val and len(val) > 40:
            return val

    # Search all values (may be nested JSON)
    for k, v in (all_storage or {}).items():
        if not v:
            continue
        # Plain hex token
        m = token_re.search(v)
        if m and len(m.group()) >= 64:
            return m.group()
        # JSON-wrapped
        try:
            obj = json.loads(v)
            if isinstance(obj, dict):
                for jk, jv in obj.items():
                    if "token" in jk.lower() or "session" in jk.lower():
                        if isinstance(jv, str) and len(jv) >= 64:
                            return jv
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: cookies
    cookies = await page.context.cookies()
    for c in cookies:
        if "token" in c["name"].lower() or "session" in c["name"].lower():
            if len(c["value"]) >= 64:
                return c["value"]

    return None


# ── Core Farm Logic ──────────────────────────────────────────────────────────
async def farm_one_account(idx: int, email: str, password: str) -> dict | None:
    ts = lambda: datetime.now().strftime("%H:%M:%S")
    print(f"[{ts()}] [{idx}] start  {email}", flush=True)

    manager = browser = page = None
    try:
        manager, browser, page = await launch_browser()

        sign_in = await google_oauth_get_code(page, browser, email, password, idx)
        if not sign_in or not sign_in.get("sessionToken"):
            print(f"[{ts()}] [{idx}] FAIL no session  {email}", flush=True)
            return None

        token = sign_in["sessionToken"]
        user_id = sign_in.get("userId", "")
        name = sign_in.get("name", "User")
        print(f"[{ts()}] [{idx}] got_token  user={user_id}  {email}", flush=True)

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {token}"}

            prof_resp = await client.post(
                f"{TASKLET_API_BASE}/api/profile", json=None, headers=headers
            )
            if prof_resp.status_code == 200:
                profile = prof_resp.json()
                user_id = profile.get("userId", user_id)
                name = profile.get("name", name)
                orgs = profile.get("organizations", [])
            else:
                orgs = []

            if orgs:
                org_id = orgs[0]["organizationId"]
                ws_id = orgs[0].get("workspaces", [{}])[0].get("workspaceId", "")
            else:
                org = await tasklet_create_org(client, token, name)
                if not org:
                    print(f"[{ts()}] [{idx}] FAIL org create  {email}", flush=True)
                    return None
                org_id = org["organizationId"]
                ws_id = org["workspaceId"]
            print(f"[{ts()}] [{idx}] org={org_id}", flush=True)

            bonus = await tasklet_claim_daily_bonus(client, token, org_id)
            if bonus:
                print(f"[{ts()}] [{idx}] daily_bonus claimed", flush=True)

            credits_info = await tasklet_get_credits(client, token, org_id)
            total_credits = credits_info.get("totalAvailable", 0) if credits_info else 0

        result = {
            "google_email": email,
            "userId": user_id,
            "sessionToken": token,
            "name": name,
            "organizationId": org_id,
            "workspaceId": ws_id,
            "totalCredits": total_credits,
            "dailyBonusClaimed": bonus,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        print(f"[{ts()}] [{idx}] OK  {email}  credits={total_credits}", flush=True)
        return result

    except asyncio.TimeoutError:
        print(f"[{ts()}] [{idx}] FAIL timeout  {email}", flush=True)
        if page:
            await screenshot(page, idx, "timeout")
        return None
    except Exception as e:
        print(f"[{ts()}] [{idx}] FAIL {e}  {email}", flush=True)
        if page:
            await screenshot(page, idx, "error")
        return None
    finally:
        if manager:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


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

    # Track used emails
    uf = RESULTS_ROOT / "used_emails.txt"
    with open(uf, "a", encoding="utf-8") as f:
        f.write(result["google_email"] + "\n")


# ── Batch Runner ─────────────────────────────────────────────────────────────
async def run_farm(count: int, concurrent: int) -> None:
    all_accounts = load_google_accounts()
    used = _load_used_emails()
    available = [(e, p) for e, p in all_accounts if e.lower() not in used]

    if not available:
        print("[POOL] no unused accounts available", flush=True)
        return

    accounts_to_farm = available[:count]
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [tasklet] farming {len(accounts_to_farm)} accounts (pool={len(all_accounts)}, used={len(used)})", flush=True)

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = RESULTS_ROOT / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    fail_count = 0
    sem = asyncio.Semaphore(concurrent)

    async def worker(idx: int, email: str, password: str) -> None:
        nonlocal ok_count, fail_count
        async with sem:
            try:
                result = await asyncio.wait_for(
                    farm_one_account(idx, email, password),
                    timeout=ACCOUNT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                t = datetime.now().strftime("%H:%M:%S")
                print(f"[{t}] [{idx}] FAIL outer timeout  {email}", flush=True)
                result = None
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

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] [DONE] ok={ok_count} fail={fail_count} batch={batch_id}", flush=True)
    print(f"[{ts}] [DONE] results: {batch_dir}", flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tasklet Google-OAuth farm")
    p.add_argument("-n", "--count", type=int, default=int(_env("TASKLET_MAX_ACCOUNTS", "5")))
    p.add_argument("-c", "--concurrent", type=int, default=CONCURRENT)
    p.add_argument("-y", "--yes", action="store_true", help="Non-interactive")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    count = args.count
    concurrent = args.concurrent

    if not args.yes:
        print(f"  Tasklet farm: {count} accounts, concurrent={concurrent}")
        confirm = input("  Start? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.", flush=True)
            return

    print(f"[tasklet] plan: n={count} c={concurrent}", flush=True)
    asyncio.run(run_farm(count, concurrent))


if __name__ == "__main__":
    main()
