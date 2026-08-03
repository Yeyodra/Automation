"""
Patch to integrate hybrid signup into farm.py.

Apply by adding to farm.py:
  from signup_http import hybrid_signup

Then replace _do_register_body with _do_register_body_hybrid below.
"""

# ── Drop-in replacement for _do_register_body ────────────────────────────────
# Copy this function into farm.py, replacing the existing _do_register_body.
# Keep the old one as _do_register_body_browser for fallback.

PATCH_INSTRUCTIONS = """
=== How to patch farm.py ===

1. Add import at top of farm.py (after existing imports):

    from signup_http import hybrid_signup

2. Rename existing function:

    async def _do_register_body(...)  →  async def _do_register_body_browser(...)

3. Add new hybrid version:

    async def _do_register_body(attempt: int, email_addr: str, password: str, proxy_url: str | None, proxy_id: str) -> dict:
        \"\"\"Hybrid: Camoufox for Turnstile only, HTTP for OTP/password/token.\"\"\"
        try:
            tokens = await hybrid_signup(
                email_addr=email_addr,
                password=password,
                proxy_url=proxy_url,
                attempt=attempt,
                otp_callback=lambda email, since: wait_otp_imap(email, since_ts=since, page=None, attempt=attempt),
                log_fn=alog,
            )
            enter_meta = enter_post_auth_setup(tokens["access_token"], GIFT_CODE)
            api_data = (enter_meta.get("api_key") or {}).get("data") or {}
            alog(attempt, f"API key created name={api_data.get('name')} id={api_data.get('id')}")
            return {
                "email": email_addr,
                "password": password,
                "gift_code": GIFT_CODE,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "attempt": attempt,
                "proxy": proxy_url or "direct",
                "workspace_id": enter_meta.get("workspace_id"),
                "tokens": {
                    "access_token": tokens.get("access_token"),
                    "refresh_token": tokens.get("refresh_token"),
                    "id_token": tokens.get("id_token"),
                    "expires_in": tokens.get("expires_in"),
                },
                "api_key": enter_meta.get("api_key"),
                "referral_claim": enter_meta.get("referral_claim"),
                "onboarding": enter_meta.get("onboarding"),
                "mode": "hybrid_http",
            }
        except Exception as e:
            # Fallback to full browser if hybrid fails
            alog(attempt, f"hybrid failed ({e}), falling back to browser")
            return await _do_register_body_browser(attempt, email_addr, password, proxy_url, proxy_id)

4. (Optional) Add env toggle:

    USE_HYBRID = _env("ENTER_USE_HYBRID", "true").lower() in ("1", "true", "yes")

    Then in _do_register_body, check USE_HYBRID first:
        if not USE_HYBRID:
            return await _do_register_body_browser(...)

=== Benefits ===

- Browser alive only ~10-15s (Turnstile) instead of ~60-90s (full flow)
- OTP wait happens without browser open (saves RAM)
- Password + OAuth exchange are pure HTTP (no DOM races)
- Fallback to full browser if HTTP steps fail
"""

if __name__ == "__main__":
    print(PATCH_INSTRUCTIONS)
