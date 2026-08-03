#!/usr/bin/env python3
"""
Full end-to-end test of hybrid signup (1 account).

Usage:
  python run_hybrid_test.py
  python run_hybrid_test.py --headed     # see the browser during Turnstile
  python run_hybrid_test.py --proxy socks5://127.0.0.1:40001

Requires: camoufox, same .env as farm.py (GPTMAIL_API, ENTER_GIFT_CODE, etc.)
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import string
import sys
import time
import urllib.request
from pathlib import Path

# Load .env same as farm.py
_ROOT = Path(__file__).resolve().parent
_HUB = _ROOT.parent.parent
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

def _load_env():
    for ep in (_ROOT / ".env", _HUB / ".env"):
        if ep.is_file():
            for line in ep.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

from signup_http import hybrid_signup, HTTPSession, UA

# ── Gptmail OTP fetcher (same logic as farm.py) ─────────────────────────────
GPTMAIL_API = os.environ.get("ENTER_GPTMAIL_API", os.environ.get("GPTMAIL_API", "https://mail.chatgpt.org.uk")).rstrip("/")
GPTMAIL_PREFIX = os.environ.get("ENTER_GPTMAIL_PREFIX", "ent")
GIFT_CODE = os.environ.get("ENTER_GIFT_CODE", "")
OTP_TIMEOUT = int(os.environ.get("ENTER_OTP_TIMEOUT_S", "120"))


def _gptmail_headers() -> dict:
    return {"User-Agent": UA, "Accept": "application/json"}


def _http_json(url: str, headers: dict | None = None, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers or _gptmail_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def gptmail_create_inbox() -> tuple[str, str]:
    """Create a random gptmail inbox. Returns (email, token)."""
    # Get domains
    data = _http_json(f"{GPTMAIL_API}/api/domains/public")
    domains = []
    for d in (data.get("data") or {}).get("domains") or []:
        name = (d.get("domain_name") or "").strip().lower()
        if name and d.get("is_active") in (None, 1, True, "1"):
            domains.append(name)
    if not domains:
        raise RuntimeError(f"gptmail: no domains available")
    domain = secrets.choice(domains)

    # Generate random local part
    local = GPTMAIL_PREFIX + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    email = f"{local}@{domain}"

    # Create inbox (gptmail auto-creates on first check, but let's get a token)
    # The farm uses /api/inbox/create or just polls /api/inbox/{email}/messages
    # Try create endpoint first
    try:
        body = json.dumps({"email": email}).encode()
        req = urllib.request.Request(
            f"{GPTMAIL_API}/api/inbox/create",
            data=body,
            headers={**_gptmail_headers(), "Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
        token = (result.get("data") or {}).get("token") or ""
    except Exception:
        token = ""  # Some gptmail versions auto-create; token not mandatory

    print(f"[GPTMAIL] created: {email} (domain={domain})")
    return email, token


def gptmail_wait_otp(email: str, since_ts: float, timeout: int = OTP_TIMEOUT) -> str:
    """Poll gptmail for OTP code from Auth0/Converge."""
    deadline = time.time() + timeout
    checked = 0
    while time.time() < deadline:
        try:
            data = _http_json(f"{GPTMAIL_API}/api/inbox/{email}/messages")
            messages = (data.get("data") or {}).get("messages") or data.get("messages") or []
            for msg in messages:
                # Look for Auth0 verification code
                subject = (msg.get("subject") or "").lower()
                body_text = msg.get("text") or msg.get("body") or msg.get("html") or ""
                if "verif" in subject or "code" in subject or "converge" in subject.lower():
                    # Extract 6-digit code
                    import re
                    m = re.search(r"\b(\d{6})\b", body_text)
                    if m:
                        code = m.group(1)
                        print(f"[OTP] found: {code} (from: {subject[:40]})")
                        return code
        except Exception as e:
            if checked == 0:
                print(f"[OTP] poll error: {e}")
        checked += 1
        time.sleep(3 if checked < 5 else 5)
    raise RuntimeError(f"OTP timeout ({timeout}s) for {email}")


async def gptmail_otp_callback(email: str, since_ts: float) -> str:
    """Async wrapper for OTP wait (runs in thread to not block)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, gptmail_wait_otp, email, since_ts)


# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hybrid signup test (1 account)")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    parser.add_argument("--proxy", type=str, default="", help="Proxy URL (socks5://...)")
    parser.add_argument("--email", type=str, default="", help="Use specific email (skip gptmail create)")
    args = parser.parse_args()

    if args.headed:
        os.environ["ENTER_HEADLESS"] = "false"

    proxy = args.proxy or None
    password = "Hx" + secrets.token_urlsafe(12) + "!1"

    # Create gptmail inbox
    if args.email:
        email = args.email
        print(f"[*] Using provided email: {email}")
    else:
        email, _token = gptmail_create_inbox()

    print(f"[*] Email: {email}")
    print(f"[*] Password: {password}")
    print(f"[*] Proxy: {proxy or 'direct'}")
    print(f"[*] Gift code: {GIFT_CODE or '(none)'}")
    print(f"[*] Headless: {os.environ.get('ENTER_HEADLESS', 'true')}")
    print()

    try:
        tokens = await hybrid_signup(
            email_addr=email,
            password=password,
            proxy_url=proxy,
            attempt=1,
            otp_callback=gptmail_otp_callback,
            log_fn=lambda attempt, msg: print(f"  [{attempt}] {msg}", flush=True),
        )
        print()
        print("=" * 60)
        print("SUCCESS!")
        print("=" * 60)
        print(f"  access_token: {tokens.get('access_token', '')[:40]}...")
        print(f"  refresh_token: {tokens.get('refresh_token', '')[:30]}...")
        print(f"  expires_in: {tokens.get('expires_in')}")
        print()

        # Optionally do post-auth (referral + api key)
        if GIFT_CODE:
            print("[*] Running post-auth setup (referral + API key)...")
            # Import from farm.py if available, else inline
            try:
                sys.path.insert(0, str(_ROOT))
                from farm import enter_post_auth_setup
                meta = enter_post_auth_setup(tokens["access_token"], GIFT_CODE)
                api_key = (meta.get("api_key") or {}).get("data", {}).get("key", "")
                ws_id = meta.get("workspace_id", "")
                print(f"  workspace_id: {ws_id}")
                print(f"  api_key: {api_key[:20]}..." if api_key else "  api_key: (none)")
                # Save result
                result = {"email": email, "password": password, "api_key": api_key, "workspace_id": ws_id}
                out_file = _ROOT / "results" / "hybrid_test_result.json"
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                print(f"  saved: {out_file}")
            except ImportError:
                print("  (farm.py not importable, skipping post-auth)")
        else:
            print("[*] No GIFT_CODE set, skipping post-auth")

    except Exception as e:
        print()
        print(f"FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
