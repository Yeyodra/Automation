#!/usr/bin/env python3
"""
Re-auth dead Grok CLI tokens in 9router via xAI Device OAuth (same path as grok2api).

Why:
  - PKCE authorize → "Failed to generate authentication code / Access denied"
  - Existing refresh_tokens return invalid_grant (revoked)
  - Device OAuth (auth.x.ai/oauth2/device/code) still works (200)

Creds:
  1) email+password from farms/grok/results/batch_*/accounts*.json
  2) fallback: hub ACCOUNT_PASSWORD / GROK_PASSWORD

Usage:
  python reauth_device_oauth.py --limit 1 --dry-run
  python reauth_device_oauth.py --limit 5 -c 1
  python reauth_device_oauth.py --all -c 2 --only-expired
  python reauth_device_oauth.py --email user@domain.com
  python reauth_device_oauth.py --all -c 3 --warp-every-n 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

_ROOT = Path(__file__).resolve().parent
_HUB = _ROOT.parent.parent
_RESULTS = _ROOT / "results"

# Ensure hub env is visible when run standalone
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

# Map hub shared keys (HEADLESS, ACCOUNT_PASSWORD, …) → GROK_* before farm import
from core.env import build_job_env  # noqa: E402

for _k, _v in build_job_env("GROK_", _ROOT).items():
    if _k not in os.environ or not str(os.environ.get(_k) or "").strip():
        os.environ[_k] = str(_v)

# Import farm helpers after env load
sys.path.insert(0, str(_ROOT))
import farm as grok  # noqa: E402

from core.warp_policy import WarpPolicy  # noqa: E402
from vps_push import VpsBatchPusher  # noqa: E402

CLIENT_ID = grok.XAI_CLIENT_ID
# Match grok2api default scope (no conversations:* — still accepted by device endpoint)
DEVICE_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access"
)
DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = grok.XAI_TOKEN

DEFAULT_DB = Path(os.environ.get("APPDATA", "")) / "9router" / "db" / "data.sqlite"
DEFAULT_PASSWORD = (
    os.environ.get("GROK_PASSWORD")
    or os.environ.get("ACCOUNT_PASSWORD")
    or ""
).strip()


def _http_json(url: str, form: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    body = urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"_raw": raw, "error": f"http_{e.code}"}
        data["_http_status"] = e.code
        return data


def start_device_code() -> dict[str, Any]:
    data = _http_json(
        DEVICE_URL,
        {"client_id": CLIENT_ID, "scope": DEVICE_SCOPE},
    )
    if not data.get("device_code") or not data.get("user_code"):
        raise RuntimeError(f"device/code failed: {data}")
    return data


def poll_device_token(
    device_code: str,
    interval_s: float = 5.0,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    sleep_s = max(3.0, float(interval_s or 5))
    while time.monotonic() < deadline:
        data = _http_json(
            TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            },
        )
        if data.get("access_token"):
            return data
        err = (data.get("error") or "").lower()
        if err == "authorization_pending":
            time.sleep(sleep_s)
            continue
        if err == "slow_down":
            sleep_s = min(30.0, sleep_s + 2.0)
            time.sleep(sleep_s)
            continue
        if err in ("access_denied", "expired_token", "invalid_grant"):
            raise RuntimeError(f"device poll denied: {data}")
        # unexpected
        if data.get("_http_status") and data.get("_http_status") >= 400:
            raise RuntimeError(f"device poll error: {data}")
        time.sleep(sleep_s)
    raise TimeoutError("device code poll timed out")


def tokens_from_oauth_response(data: dict[str, Any], email_fallback: str = "") -> dict[str, Any]:
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if not access or not refresh:
        raise RuntimeError(f"token response missing tokens: {list(data.keys())}")
    expires_in = int(data.get("expires_in") or 21600)
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in
    expires_at_iso = (
        datetime.fromtimestamp(expires_at, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    email = email_fallback
    id_token = data.get("id_token") or ""
    if id_token:
        try:
            import base64

            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            email = payload.get("email") or email
        except Exception:
            pass
    out = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at_iso,
        "expires_in": expires_in,
        "email": email,
        "client_id": CLIENT_ID,
        "auth_mode": "device_oauth",
        "scope": data.get("scope") or DEVICE_SCOPE,
    }
    if id_token:
        out["id_token"] = id_token
    return out


def load_password_map() -> dict[str, str]:
    """email(lower) -> password from all farm batch results."""
    out: dict[str, str] = {}
    if not _RESULTS.is_dir():
        return out
    for batch in _RESULTS.iterdir():
        if not batch.is_dir() or not batch.name.startswith("batch_"):
            continue
        for f in batch.glob("accounts*.json"):
            try:
                arr = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            for acc in arr:
                if not isinstance(acc, dict):
                    continue
                em = (acc.get("email") or "").strip().lower()
                pw = (acc.get("password") or "").strip()
                if em and pw and em not in out:
                    out[em] = pw
    return out


def load_targets(
    db_path: Path,
    *,
    only_expired: bool,
    include_unavailable: bool,
    email_filter: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, isActive, data FROM providerConnections WHERE provider = 'grok-cli'"
    )
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for row_id, email, is_active, raw in cur.fetchall():
        em = (email or "").strip()
        if email_filter and em.lower() != email_filter.lower():
            continue
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        st = (data.get("testStatus") or "").lower()
        exp_raw = data.get("expiresAt")
        exp_dt: datetime | None = None
        if isinstance(exp_raw, (int, float)):
            exp_dt = datetime.fromtimestamp(exp_raw / (1000 if exp_raw > 1e12 else 1), timezone.utc)
        elif isinstance(exp_raw, str) and exp_raw:
            try:
                exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            except Exception:
                exp_dt = None
        expired = exp_dt is None or exp_dt <= now
        if only_expired and not expired and st not in ("unavailable", "error"):
            # still try if lastError mentions invalid_grant / revoked
            last_err = (data.get("lastError") or "").lower()
            if "invalid_grant" not in last_err and "revoked" not in last_err:
                continue
        if not include_unavailable and st == "unavailable":
            # still reauth unavailable — that is the whole point when tokens are dead
            pass
        rows.append(
            {
                "id": row_id,
                "email": em,
                "isActive": is_active,
                "data": data,
                "expired": expired,
                "testStatus": st,
            }
        )
    conn.close()
    # Prefer expired / unavailable first
    rows.sort(
        key=lambda r: (
            0 if r["testStatus"] in ("unavailable", "error") else 1,
            0 if r["expired"] else 1,
            r["email"],
        )
    )
    if limit is not None and limit > 0:
        rows = rows[:limit]
    return rows


def update_9router_row(
    db_path: Path,
    row_id: str,
    data: dict[str, Any],
    tokens: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data = dict(data)
    data["accessToken"] = tokens["access_token"]
    data["refreshToken"] = tokens["refresh_token"]
    data["expiresAt"] = tokens["expires_at"]
    data["expiresIn"] = tokens.get("expires_in") or 21600
    data["scope"] = tokens.get("scope") or data.get("scope") or DEVICE_SCOPE
    data["testStatus"] = "active"
    data["lastError"] = None
    data["lastErrorAt"] = None
    data["errorCode"] = None
    data["lastRefreshAt"] = now
    data["backoffLevel"] = 0
    # clear model locks
    for k in list(data.keys()):
        if str(k).startswith("modelLock_"):
            del data[k]
    psd = dict(data.get("providerSpecificData") or {})
    psd["authMethod"] = tokens.get("auth_mode") or "device_oauth"
    if tokens.get("id_token"):
        psd["idToken"] = tokens["id_token"]
    if tokens.get("email"):
        psd["email"] = tokens["email"]
    data["providerSpecificData"] = psd

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE providerConnections SET data = ?, updatedAt = ?, isActive = 1 WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), now, row_id),
        )
        conn.commit()
    finally:
        conn.close()



def delete_9router_row(db_path: Path, row_id: str, email: str = "") -> None:
    """Hard-delete a grok-cli connection from 9router DB."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM providerConnections WHERE id = ?", (row_id,))
        conn.commit()
        print(
            f"[db] DELETED grok-cli id={row_id} email={email or '?'}",
            flush=True,
        )
    finally:
        conn.close()


def is_permanent_access_denied(msg: str) -> bool:
    """True when xAI permanently rejects this account's device/refresh auth."""
    m = (msg or "").lower()
    if "access denied" in m:
        return True
    if "invalid_grant" in m and "denied" in m:
        return True
    return False


async def dismiss_cookies_hard(page, attempt: int = 0) -> None:
    """Cookie modal blocks Next/Login — hammer accept before form clicks."""
    for _ in range(4):
        await grok.dismiss_cookie_banner(page)
        await grok.click_text_button(
            page,
            [
                "Accept All Cookies",
                "Accept All",
                "Accept all cookies",
                "Reject All",
                "Allow All",
                "I Accept",
            ],
        )
        try:
            for sel in (
                'button[aria-label="Close"]',
                'button[aria-label="close"]',
            ):
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=1500)
                    break
        except Exception:
            pass
        try:
            body = (await page.inner_text("body"))[:800].lower()
        except Exception:
            body = ""
        if "accept all cookies" not in body and "cookie settings" not in body:
            return
        await asyncio.sleep(0.45)
    if attempt:
        print(f"[{attempt}] WARN: cookie banner may still be open", flush=True)


def http_refresh_token(refresh_token: str) -> dict[str, Any] | None:
    """POST refresh_token grant. None if revoked/invalid."""
    if not refresh_token:
        return None
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("access_token"):
            return data
        return None
    except urllib.error.HTTPError:
        return None


async def approve_device_in_browser(
    email: str,
    password: str,
    verify_url: str,
    attempt: int,
) -> bool:
    """Sign-in first (full farm login drive), then device page until Device Authorized.

    Restored reliable path (pre-fast/lean). Device-first lean login was timing out.
    """
    manager = None
    approved = False
    try:
        manager, browser, page = await grok.launch_browser(None)
        print(f"[{attempt}] browser up for {email} headless={grok.HEADLESS}", flush=True)

        # 1) Establish session on sign-in (same path that previously got OKs)
        await page.goto(grok.SIGNIN_URL, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.0)
        await dismiss_cookies_hard(page, attempt)
        await grok.recover_page_load_error(page, attempt)
        await dismiss_cookies_hard(page, attempt)
        await grok.click_login_with_email(page)
        await asyncio.sleep(0.5)
        await dismiss_cookies_hard(page, attempt)

        ok = await grok.drive_email_password_login(page, email, password, attempt)
        await dismiss_cookies_hard(page, attempt)
        await grok.screenshot(page, attempt, "reauth_after_login")

        try:
            still_login = (
                await page.locator('input[type="password"]').count() > 0
                or await page.locator(
                    "text=/Log in with your email|Log into your account/i"
                ).count()
                > 0
            )
        except Exception:
            still_login = not ok

        if still_login:
            print(f"[{attempt}] login incomplete — retry after cookie dismiss", flush=True)
            await dismiss_cookies_hard(page, attempt)
            await grok.click_login_with_email(page)
            await asyncio.sleep(0.4)
            ok = await grok.drive_email_password_login(page, email, password, attempt)
            await grok.screenshot(page, attempt, "reauth_after_login_retry")

        if not ok:
            print(
                f"[{attempt}] WARN: login drive returned False — trying device page",
                flush=True,
            )

        # 2) Device verification (user_code prefilled)
        await page.goto(verify_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.2)
        await dismiss_cookies_hard(page, attempt)
        await grok.recover_page_load_error(page, attempt)
        await grok.handle_turnstile(page, attempt, max_wait=12)

        for _ in range(12):
            await dismiss_cookies_hard(page, attempt)
            if await page.locator('input[type="email"], input[type="password"]').count() > 0:
                await grok.click_login_with_email(page)
                await asyncio.sleep(0.3)
                await dismiss_cookies_hard(page, attempt)
                await grok.drive_email_password_login(page, email, password, attempt)
                await asyncio.sleep(0.8)
                await page.goto(verify_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.0)
                await dismiss_cookies_hard(page, attempt)

            await grok.handle_turnstile(page, attempt, max_wait=6)
            body = ""
            try:
                body = (await page.inner_text("body"))[:600].lower()
            except Exception:
                pass
            if any(
                x in body
                for x in (
                    "device authorized",
                    "has been authorized",
                    "you can close this window",
                    "return to your terminal",
                    "authorization complete",
                    "already authorized",
                )
            ):
                print(f"[{attempt}] device page looks approved", flush=True)
                approved = True
                break

            await grok.click_text_button(
                page,
                [
                    "Continue",
                    "Allow",
                    "Authorize",
                    "Approve",
                    "Confirm",
                    "Yes",
                    "Accept",
                    "Next",
                    "Login with email",
                    "Log in with email",
                ],
                exclude=["Google", "Deny", "Cancel", "Sign out", "Cookies Settings"],
            )
            await asyncio.sleep(1.0)

        await grok.screenshot(page, attempt, "reauth_device_done")
        if approved:
            await asyncio.sleep(2.0)
        return approved
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


async def reauth_one(
    target: dict[str, Any],
    password: str,
    db_path: Path,
    attempt: int,
    dry_run: bool,
    *,
    try_refresh_first: bool = True,
    fast: bool = True,
    delete_on_access_denied: bool = True,
) -> tuple[bool, str, str, dict[str, Any] | None]:
    """Try HTTP refresh first; on revoke use Device OAuth.

    Returns (ok, message, action, tokens|None).
    action: '' | 'updated' | 'deleted' | 'failed'.
    """
    email = target["email"]
    if dry_run:
        return True, "dry-run", "", None

    data0 = target.get("data") or {}
    rt = data0.get("refreshToken") or data0.get("refresh_token") or ""
    row_id = target["id"]

    def _maybe_delete(reason: str) -> tuple[bool, str, str, None]:
        if not delete_on_access_denied:
            return False, reason, "failed", None
        try:
            delete_9router_row(db_path, row_id, email)
            return False, f"{reason} -> DELETED from 9router", "deleted", None
        except Exception as e:
            return False, f"{reason} (delete failed: {e})", "failed", None

    # Phase A — endpoint (no browser)
    if try_refresh_first and rt:
        data = await asyncio.to_thread(http_refresh_token, rt)
        if data and data.get("access_token"):
            tokens = tokens_from_oauth_response(data, email_fallback=email)
            if not tokens.get("refresh_token") and rt:
                tokens["refresh_token"] = rt
            update_9router_row(db_path, row_id, data0, tokens)
            return True, f"refresh-ok exp={tokens['expires_at']}", "updated", tokens

    # Phase B — Device OAuth (revoked)
    try:
        dev = await asyncio.to_thread(start_device_code)
    except Exception as e:
        return False, f"device/code: {e}", "failed", None

    device_code = dev["device_code"]
    user_code = dev.get("user_code")
    verify = dev.get("verification_uri_complete") or (
        f"{dev.get('verification_uri')}?user_code={user_code}"
    )
    interval = float(dev.get("interval") or 5)
    print(
        f"[{attempt}] device user_code={user_code} verify={verify[:80]}…",
        flush=True,
    )

    try:
        approved = await approve_device_in_browser(email, password, verify, attempt)
    except Exception as e:
        return False, f"browser: {e}", "failed", None

    if not approved:
        print(f"[{attempt}] WARN: approved UI not confirmed — polling anyway", flush=True)

    try:
        data = await asyncio.to_thread(poll_device_token, device_code, interval, 120.0)
    except Exception as e:
        msg = f"poll: {e}"
        if is_permanent_access_denied(msg):
            return _maybe_delete(msg)
        return False, msg, "failed", None

    try:
        tokens = tokens_from_oauth_response(data, email_fallback=email)
        update_9router_row(db_path, row_id, data0, tokens)
        return True, f"ok exp={tokens['expires_at']}", "updated", tokens
    except Exception as e:
        return False, f"save: {e}", "failed", None



async def _maybe_warp_after_ok(
    warp: WarpPolicy,
    warp_every_n: int,
    can_start: asyncio.Event,
    in_flight_lock: asyncio.Lock,
    in_flight: dict[str, int],
) -> None:
    """Increment OK counter; if everyN hit, ONE worker drains peers then rotates.

    Bug fixed: concurrent OKs all seeing next_n>=everyN used to each wait for
    in_flight<=1 while counting themselves → permanent deadlock (stuck hours).
    """
    # Serialize drain ownership with warp lock + a module-level flag on the policy
    owner = False
    with warp._lock:  # noqa: SLF001
        warp._success_since += 1
        n = warp._success_since
        if n < warp.every_n:
            print(f"[warp-policy] success {n}/{warp.every_n} (no rotate yet)", flush=True)
            return
        # Claim drain only if nobody else is draining
        if getattr(warp, "_drain_busy", False):
            # Another worker is draining; our increment already counted — leave rotate to them
            print(
                f"[warp-policy] success {n}/{warp.every_n} (drain already owned)",
                flush=True,
            )
            return
        warp._drain_busy = True
        owner = True
        print(
            f"[warp-policy] success {n}/{warp.every_n} -> claim drain/rotate",
            flush=True,
        )

    if not owner:
        return

    can_start.clear()
    try:
        # Wait until only this worker remains in-flight (timeout 15 min hard)
        deadline = time.monotonic() + 900.0
        while True:
            async with in_flight_lock:
                cur = in_flight["n"]
            if cur <= 1:
                break
            if time.monotonic() > deadline:
                print(
                    f"[warp-policy] WARN drain timeout (in_flight={cur}) — rotate anyway",
                    flush=True,
                )
                break
            await asyncio.sleep(0.35)
        # Reset counter + rotate (on_success would double-count; call rotate directly)
        with warp._lock:  # noqa: SLF001
            warp._success_since = 0
        await asyncio.to_thread(warp.rotate, f"every {warp_every_n} successes")
    finally:
        with warp._lock:  # noqa: SLF001
            warp._drain_busy = False
        can_start.set()


async def worker(
    sem: asyncio.Semaphore,
    target: dict[str, Any],
    pw_map: dict[str, str],
    default_pw: str,
    db_path: Path,
    idx: int,
    dry_run: bool,
    stats: dict[str, int],
    stats_lock: asyncio.Lock,
    warp: WarpPolicy | None,
    can_start: asyncio.Event,
    in_flight_lock: asyncio.Lock,
    in_flight: dict[str, int],
    warp_every_n: int,
    try_refresh_first: bool,
    fast: bool,
    delete_on_access_denied: bool,
    vps_pusher: VpsBatchPusher | None,
) -> None:
    email = target["email"]
    pw = pw_map.get(email.lower()) or default_pw

    # Wait while WARP is rotating / draining
    await can_start.wait()

    async with sem:
        await can_start.wait()
        async with in_flight_lock:
            in_flight["n"] += 1
        try:
            if not pw:
                msg = "no password (not in results, ACCOUNT_PASSWORD empty)"
                print(f"[{idx}] FAIL {email} — {msg}", flush=True)
                async with stats_lock:
                    stats["fail"] += 1
                return
            # Progress contract: [id] START|OK|DEL|FAIL …
            print(
                f"[{idx}] START {email} status={target.get('testStatus')} "
                f"expired={target.get('expired')} pw_src="
                f"{'results' if email.lower() in pw_map else 'env'}",
                flush=True,
            )
            ok, msg, action, tokens = await reauth_one(
                target, pw, db_path, idx, dry_run,
                try_refresh_first=try_refresh_first,
                fast=fast,
                delete_on_access_denied=delete_on_access_denied,
            )
            tag = "OK" if ok else ("DEL" if action == "deleted" else "FAIL")
            print(f"[{idx}] {tag} {email} — {msg}", flush=True)
            async with stats_lock:
                if ok:
                    stats["ok"] += 1
                elif action == "deleted":
                    stats["deleted"] = stats.get("deleted", 0) + 1
                else:
                    stats["fail"] += 1

            # VPS merge push every N OK (does not full-replace DB)
            if ok and tokens and vps_pusher is not None and not dry_run:
                await asyncio.to_thread(vps_pusher.add, email, tokens)

            # WARP every-N on OK only. Single drain-owner to avoid multi-OK deadlock.
            if ok and warp is not None and warp_every_n > 0 and not dry_run:
                await _maybe_warp_after_ok(
                    warp, warp_every_n, can_start, in_flight_lock, in_flight
                )
        finally:
            async with in_flight_lock:
                in_flight["n"] = max(0, in_flight["n"] - 1)


async def async_main(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"ERROR: DB not found: {db_path}", flush=True)
        return 1

    default_pw = (args.password or DEFAULT_PASSWORD).strip()
    pw_map = load_password_map()
    print(f"Password map from results: {len(pw_map)} emails", flush=True)
    print(f"Default password set: {bool(default_pw)}", flush=True)
    print(f"Headless (farm): {grok.HEADLESS}", flush=True)

    # Hub HUD: -n 0 means full pool (--all). Env WARP_EVERY_N if CLI everyN=0.
    if not args.all and args.limit is not None and int(args.limit) <= 0:
        args.all = True
        args.limit = None
    if int(getattr(args, "warp_every_n", 0) or 0) <= 0:
        env_every = (
            os.environ.get("WARP_EVERY_N")
            or os.environ.get("GROK_WARP_EVERY_N")
            or "0"
        ).strip()
        try:
            args.warp_every_n = max(0, int(env_every or "0"))
        except ValueError:
            pass

    limit = None if args.all else (args.limit if args.limit is not None else 1)
    targets = load_targets(
        db_path,
        only_expired=args.only_expired,
        include_unavailable=True,
        email_filter=args.email,
        limit=limit,
    )
    print(f"Targets (revoked/expired only): {len(targets)}", flush=True)
    if not targets:
        print("Nothing to reauth.", flush=True)
        return 0

    have_pw = sum(1 for t in targets if (t["email"].lower() in pw_map) or default_pw)
    print(f"With usable password: {have_pw}/{len(targets)}", flush=True)

    if args.dry_run:
        for i, t in enumerate(targets[:20], 1):
            src = "results" if t["email"].lower() in pw_map else ("env" if default_pw else "NONE")
            print(
                f"  [{i}] {t['email']} status={t['testStatus']} expired={t['expired']} pw={src}",
                flush=True,
            )
        if len(targets) > 20:
            print(f"  … +{len(targets) - 20} more", flush=True)
        return 0

    conc = max(1, min(8, int(args.concurrent)))
    # 1:1 with -c when everyN > 0 (hub farm convention).
    # Prefer CLI --warp-every-n; else hub-injected WARP_EVERY_N / GROK_WARP_EVERY_N.
    warp_cli = int(getattr(args, "warp_every_n", 0) or 0)
    if warp_cli <= 0:
        for _ek in ("WARP_EVERY_N", "GROK_WARP_EVERY_N"):
            try:
                warp_cli = max(0, int((os.environ.get(_ek) or "0").strip() or "0"))
            except ValueError:
                warp_cli = 0
            if warp_cli > 0:
                break
    warp_every_n = max(0, warp_cli)
    if warp_every_n > 0 and warp_every_n != conc:
        print(
            f"[warp] everyN {warp_every_n} forced → {conc} (1:1 with -c)",
            flush=True,
        )
        warp_every_n = conc

    warp: WarpPolicy | None = None
    if warp_every_n > 0:
        warp = WarpPolicy(every_n=warp_every_n, log=lambda m: print(m, flush=True))
        print(f"[warp] ensure connected… everyN={warp_every_n} c={conc}", flush=True)
        ok_conn = await asyncio.to_thread(warp.ensure_connected)
        print(f"[warp] connected={ok_conn}", flush=True)
        # Optional pre-rotate for clean IP before long batch
        if args.warp_rotate:
            await asyncio.to_thread(warp.rotate, "pre-batch")
    else:
        print("[warp] off", flush=True)

    # VPS credential merge (batch every N OK)
    vps_every = max(1, int(getattr(args, "vps_push_every", 10) or 10))
    vps_on = bool(getattr(args, "vps_push", False)) or bool(
        (os.environ.get("GROK_VPS_PUSH") or "").strip().lower() in ("1", "true", "yes", "on")
    )
    vps_pusher: VpsBatchPusher | None = None
    if vps_on and not args.dry_run:
        vps_pusher = VpsBatchPusher(
            every=vps_every,
            log=lambda m: print(m, flush=True),
            enabled=True,
        )
    elif args.dry_run:
        print("[vps-push] dry-run — push disabled", flush=True)
    else:
        print(
            "[vps-push] off (use --vps-push or GROK_VPS_PUSH=1 + GROK_VPS_HOST/PASS)",
            flush=True,
        )

    sem = asyncio.Semaphore(conc)
    stats = {"ok": 0, "fail": 0, "deleted": 0}
    lock = asyncio.Lock()
    can_start = asyncio.Event()
    can_start.set()
    in_flight_lock = asyncio.Lock()
    in_flight = {"n": 0}

    print(
        f"Starting reauth: n={len(targets)} c={conc} warp_every_n={warp_every_n}",
        flush=True,
    )
    tasks = [
        worker(
            sem, t, pw_map, default_pw, db_path, i, False, stats, lock,
            warp, can_start, in_flight_lock, in_flight, warp_every_n,
            args.try_refresh_first, args.fast, args.delete_on_access_denied,
            vps_pusher,
        )
        for i, t in enumerate(targets, 1)
    ]
    await asyncio.gather(*tasks)
    if vps_pusher is not None:
        await asyncio.to_thread(vps_pusher.flush_remaining)
        print(
            f"[vps-push] summary flushes={vps_pusher.flushes} "
            f"pushed={vps_pusher.pushed} pruned_local={getattr(vps_pusher, 'pruned', 0)} "
            f"errors={vps_pusher.errors}",
            flush=True,
        )
    print("=" * 50, flush=True)
    print(f"DONE ok={stats['ok']} deleted={stats.get('deleted', 0)} fail={stats['fail']} total={len(targets)}", flush=True)
    return 0 if stats["fail"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Reauth 9router grok-cli via Device OAuth")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="9router data.sqlite path")
    ap.add_argument("-n", "--limit", type=int, default=None, help="Max accounts (default 1)")
    ap.add_argument("--all", action="store_true", help="All matching accounts")
    ap.add_argument("-c", "--concurrent", type=int, default=1, help="Parallel browsers")
    ap.add_argument("--email", default=None, help="Single email only")
    ap.add_argument(
        "--only-expired",
        action="store_true",
        default=True,
        help="Only expired / invalid_grant (default on)",
    )
    ap.add_argument(
        "--include-valid",
        action="store_true",
        help="Also reauth non-expired accounts",
    )
    ap.add_argument(
        "--warp-every-n",
        type=int,
        default=0,
        help="Rotate WARP IP every N OK reauths (forced 1:1 with -c when >0)",
    )
    ap.add_argument(
        "--warp-rotate",
        action="store_true",
        help="Rotate WARP once before batch starts",
    )
    ap.add_argument("--password", default="", help="Override default ACCOUNT_PASSWORD")
    ap.add_argument(
        "--fast",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Deprecated no-op (kept for CLI); always uses reliable sign-in path",
    )
    ap.add_argument(
        "--try-refresh-first",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="HTTP refresh first; browser only on revoke (default on)",
    )
    ap.add_argument(
        "--delete-on-access-denied",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="DELETE grok-cli row from 9router when poll returns Access denied (default on)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--vps-push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Push OK credentials to VPS 9router (merge by email). Default: env GROK_VPS_PUSH",
    )
    ap.add_argument(
        "--vps-push-every",
        type=int,
        default=int(os.environ.get("GROK_VPS_PUSH_EVERY") or "10"),
        help="Flush to VPS every N OK accounts (default 10)",
    )
    # HUD always has -y for farms; accept as no-op so exit!=2
    ap.add_argument(
        "-y",
        "--yes",
        "--non-interactive",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = ap.parse_args()
    if args.vps_push is None:
        args.vps_push = (os.environ.get("GROK_VPS_PUSH") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    if args.include_valid:
        args.only_expired = False
    if args.email:
        args.all = True
        args.limit = 1
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("Interrupted", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
