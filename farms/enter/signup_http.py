#!/usr/bin/env python3
"""
Hybrid signup: Camoufox only for Turnstile solve, HTTP for the rest.

Replaces do_signup_and_oauth() in farm.py with a much lighter flow:
  1. HTTP: GET /authorize → 302 → extract state + cookies
  2. CAMOUFOX: /u/signup/identifier (email + Turnstile) → submit → close browser
  3. HTTP: POST /u/signup/challenge (OTP code)
  4. HTTP: POST /u/signup/password
  5. HTTP: follow 302 → extract ?code= → POST /oauth/token

Import and call: tokens = await hybrid_signup(email, password, proxy_url, attempt)
"""
from __future__ import annotations

import asyncio
import hashlib
import base64
import json
import os
import re
import secrets
import string
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

# ── Config (same as farm.py, import or env) ──────────────────────────────────
AUTH_HOST = os.environ.get("ENTER_AUTH_HOST", "https://converge-ai.us.auth0.com")
API_HOST = os.environ.get("ENTER_API_HOST", "https://api.enter.pro")
APP_HOST = os.environ.get("ENTER_APP_HOST", "https://enter.converge.ai")
CLIENT_ID = os.environ.get("ENTER_CLIENT_ID", "anCisSaaIA36fTZ2DUMiTMro3bYuptrf")
AUDIENCE = os.environ.get("ENTER_AUDIENCE", "https://api.enter.pro")
SCOPE = os.environ.get("ENTER_SCOPE", "openid profile email offline_access")
REDIRECT_URI = os.environ.get("ENTER_REDIRECT_URI", APP_HOST)
TOKEN_URL = f"{AUTH_HOST}/oauth/token"
AUTHORIZE_URL = f"{AUTH_HOST}/authorize"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADLESS = os.environ.get("ENTER_HEADLESS", "true").lower() in ("1", "true", "yes")


# ── PKCE ─────────────────────────────────────────────────────────────────────
def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── HTTP session (cookie jar) ────────────────────────────────────────────────
class HTTPSession:
    """Minimal session with cookie jar + redirect control."""

    def __init__(self, proxy_url: str | None = None):
        self.jar = CookieJar()
        handlers: list = [urllib.request.HTTPCookieProcessor(self.jar)]
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({
                "http": proxy_url,
                "https": proxy_url,
            }))
        # NoRedirect handler so we can inspect 302s
        handlers.append(NoRedirectHandler())
        self.opener = urllib.request.build_opener(*handlers)

    def get(self, url: str, *, follow: bool = False, timeout: int = 30) -> HTTPResp:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"})
        return self._do(req, follow=follow, timeout=timeout)

    def post_form(self, url: str, data: dict, *, referer: str = "", timeout: int = 30) -> HTTPResp:
        body = urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": AUTH_HOST,
            "Referer": referer or url,
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
        return self._do(req, follow=False, timeout=timeout)

    def post_json(self, url: str, data: dict, *, headers: dict | None = None, timeout: int = 30) -> dict:
        body = json.dumps(data).encode("utf-8")
        hdrs = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
        try:
            resp = self.opener.open(req, timeout=timeout)
            return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"POST {url} → {e.code}: {e.read().decode()[:300]}") from e

    def _do(self, req: urllib.request.Request, *, follow: bool, timeout: int) -> "HTTPResp":
        try:
            resp = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return HTTPResp(e.code, dict(e.headers), body, e.geturl() or req.full_url)
        status = getattr(resp, "status", getattr(resp, "code", 200))
        body = resp.read().decode("utf-8", errors="replace")
        hdrs = dict(resp.headers) if hasattr(resp, "headers") else {}
        url = getattr(resp, "url", "") or req.full_url
        r = HTTPResp(status, hdrs, body, url)
        if follow and r.is_redirect:
            loc = r.location
            if loc and not loc.startswith("http"):
                loc = f"{AUTH_HOST}{loc}"
            if loc:
                return self.get(loc, follow=True, timeout=timeout)
        return r


class HTTPResp:
    def __init__(self, status: int, headers: dict, body: str, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    @property
    def is_redirect(self) -> bool:
        return self.status in (301, 302, 303, 307, 308)

    @property
    def location(self) -> str:
        return self.headers.get("Location") or self.headers.get("location") or ""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses as-is instead of following them."""
    def http_error_302(self, req, fp, code, msg, headers):
        # Wrap as a normal response object
        fp.status = code
        fp.headers = headers
        return fp
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


# ── Risk session ─────────────────────────────────────────────────────────────
def get_risk_session_id(session: HTTPSession) -> str | None:
    vid = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    eid = f"{int(time.time() * 1000)}.{''.join(secrets.choice(string.ascii_letters) for _ in range(6))}"
    try:
        data = session.post_json(
            f"{API_HOST}/code/api/v1/auth/risk-session",
            {"fp_event_id": eid, "visitor_id": vid, "platform": "web"},
            headers={"Origin": APP_HOST, "Referer": f"{APP_HOST}/"},
        )
        return data.get("data", {}).get("risk_session_id")
    except Exception:
        return None


# ── Step 1: Authorize → state + cookies ──────────────────────────────────────
def start_authorize(session: HTTPSession, verifier: str, challenge: str, risk_session_id: str | None = None) -> str:
    """GET /authorize, capture 302, extract state from Location header."""
    params = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "audience": AUDIENCE,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(24),
        "auth0Client": base64.b64encode(
            json.dumps({"name": "auth0-react", "version": "2.10.0"}).encode()
        ).decode(),
    }
    if risk_session_id:
        params["risk_session_id"] = risk_session_id
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    resp = session.get(url)
    # 302 → /u/login/identifier?state=XXX
    if not resp.is_redirect:
        raise RuntimeError(f"authorize: expected 302, got {resp.status}")
    loc = resp.location
    m = re.search(r"state=([A-Za-z0-9_\-=]+)", loc)
    if not m:
        raise RuntimeError(f"authorize: no state in Location: {loc[:200]}")
    state = m.group(1)
    # Visit login page to establish cookies fully
    login_url = f"{AUTH_HOST}/u/login/identifier?state={state}"
    session.get(login_url, follow=False)
    return state


# ── Step 2: Camoufox Turnstile solve + email submit ─────────────────────────
async def turnstile_signup_identifier(
    email_addr: str,
    state: str,
    proxy_url: str | None = None,
    attempt: int = 0,
    headless: bool = HEADLESS,
) -> dict:
    """Launch Camoufox, navigate to signup identifier page, fill email,
    solve Turnstile, submit. Returns cookies + state for HTTP continuation.

    Returns: {"cookies": {name: value, ...}, "next_url": str, "state": str}
    """
    from camoufox.async_api import AsyncCamoufox

    signup_url = f"{AUTH_HOST}/u/signup/identifier?state={state}"

    kwargs: dict[str, Any] = {"headless": headless, "geoip": True}
    if proxy_url:
        kwargs["proxy"] = {"server": proxy_url}

    async with AsyncCamoufox(**kwargs) as browser:
        page = await browser.new_page()
        await page.goto(signup_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)

        # Fill email
        filled = False
        for sel in ['input[name="email"]', 'input[type="email"]', 'input[inputmode="email"]']:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.fill(email_addr)
                    filled = True
                    break
            except Exception:
                continue
        if not filled:
            raise RuntimeError("signup_identifier: could not fill email")

        # Wait for Turnstile to solve (Camoufox auto-pass)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            tok_len = await page.evaluate("""() => {
                const el = document.querySelector('[name="cf-turnstile-response"], [name="captcha"], textarea[name="captcha"]');
                return el && el.value ? el.value.length : 0;
            }""")
            if tok_len and tok_len > 20:
                break
            # Click turnstile checkbox if visible
            try:
                iframe = page.frame_locator("iframe[src*='challenges.cloudflare']")
                box = iframe.locator("input[type='checkbox'], .cb-i")
                if await box.count() > 0:
                    await box.first.click()
            except Exception:
                pass
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError("signup_identifier: Turnstile timeout (60s)")

        # Submit form
        try:
            btn = page.locator('button[type="submit"][name="action"][value="default"], button:has-text("Continue"), button[type="submit"]')
            await btn.first.click(timeout=5000)
        except Exception:
            await page.evaluate("document.querySelector('form').submit()")

        # Wait for navigation to challenge page
        await asyncio.sleep(2.0)
        for _ in range(15):
            url = page.url
            if "challenge" in url or "password" in url or "code=" in url:
                break
            await asyncio.sleep(0.5)

        final_url = page.url

        # Extract cookies for HTTP continuation
        cookies_list = await page.context.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list if "auth0" in c.get("domain", "")}

        # Extract state from current URL if changed
        m = re.search(r"state=([A-Za-z0-9_\-=]+)", final_url)
        new_state = m.group(1) if m else state

    # Detect which step we landed on
    step = "unknown"
    if "challenge" in final_url or "verification" in final_url:
        step = "otp"
    elif "password" in final_url:
        step = "password"
    elif "code=" in final_url:
        step = "oauth_callback"

    return {"cookies": cookies, "next_url": final_url, "state": new_state, "step": step}


# ── Step 3: HTTP POST OTP challenge ──────────────────────────────────────────
def submit_otp_challenge(session: HTTPSession, state: str, otp_code: str) -> HTTPResp:
    """POST OTP to /u/signup/challenge (email-verification step)."""
    # Auth0 UL uses two possible URL patterns for OTP:
    #   /u/signup/challenge?state=X  (email verification)
    #   /u/signup/email-verification?state=X  (alternative)
    url = f"{AUTH_HOST}/u/signup/challenge?state={state}"
    data = {
        "state": state,
        "code": otp_code,
        "action": "default",
    }
    resp = session.post_form(url, data, referer=url)
    # If 400 with error, try alternative URL
    if resp.status >= 400 and "invalid" not in resp.body.lower():
        alt_url = f"{AUTH_HOST}/u/signup/email-verification?state={state}"
        resp = session.post_form(alt_url, data, referer=alt_url)
    return resp


# ── Step 4: HTTP POST password ───────────────────────────────────────────────
def submit_password(session: HTTPSession, state: str, password: str) -> HTTPResp:
    """POST password to /u/signup/password (set-password step)."""
    url = f"{AUTH_HOST}/u/signup/password?state={state}"
    data = {
        "state": state,
        "password": password,
        "re-enter-password": password,
        "action": "default",
    }
    resp = session.post_form(url, data, referer=url)
    # Fallback: some tenants use /u/signup/complete
    if resp.status == 404:
        alt_url = f"{AUTH_HOST}/u/signup/complete?state={state}"
        resp = session.post_form(alt_url, data, referer=alt_url)
    return resp


# ── Error extraction from Auth0 UL HTML ──────────────────────────────────────
def _extract_error(html: str) -> str:
    """Pull error message from Auth0 UL response HTML."""
    # Auth0 UL puts errors in <div id="prompt-alert" ...><p class="...">MSG</p></div>
    m = re.search(r'id="prompt-alert"[^>]*>.*?<p[^>]*>(.*?)</p>', html, re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()[:200]
    # Fallback: data-error-code attribute
    m = re.search(r'data-error-code="([^"]+)"', html)
    if m:
        return m.group(1)
    return html[:200]


# ── Step 5: Extract OAuth code from redirect ─────────────────────────────────
def extract_oauth_code(resp: HTTPResp) -> str:
    """Extract ?code=XXX from 302 Location or from response URL."""
    loc = resp.location or resp.url
    m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", loc)
    if m:
        return m.group(1)
    # If response body contains redirect meta or JS
    m = re.search(r'code=([A-Za-z0-9_\-]+)', resp.body)
    if m:
        return m.group(1)
    raise RuntimeError(f"No OAuth code found. status={resp.status} url={loc[:200]} body={resp.body[:300]}")


# ── Step 6: Token exchange ───────────────────────────────────────────────────
def exchange_code_for_tokens(session: HTTPSession, code: str, verifier: str) -> dict:
    """POST /oauth/token with authorization_code grant."""
    body = urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": APP_HOST,
        "User-Agent": UA,
        "Auth0-Client": base64.b64encode(
            json.dumps({"name": "auth0-react", "version": "2.10.0"}).encode()
        ).decode(),
    })
    try:
        resp = session.opener.open(req, timeout=30)
        data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token exchange failed: {e.code} {e.read().decode()[:300]}") from e
    if not data.get("access_token"):
        raise RuntimeError(f"token exchange: no access_token in response: {list(data.keys())}")
    return data


# ── Main hybrid flow ─────────────────────────────────────────────────────────
async def hybrid_signup(
    email_addr: str,
    password: str,
    proxy_url: str | None = None,
    attempt: int = 0,
    otp_callback=None,
    log_fn=None,
) -> dict:
    """Full signup flow: Camoufox for Turnstile, HTTP for rest.

    otp_callback: async callable(email, since_ts) -> str (6-digit OTP)
    log_fn: callable(attempt, msg) for logging

    Returns: {"access_token": ..., "refresh_token": ..., "id_token": ..., ...}
    """
    def _log(msg: str):
        if log_fn:
            log_fn(attempt, msg)
        else:
            print(f"[{attempt}] {msg}", flush=True)

    session = HTTPSession(proxy_url)

    # 1. Risk session
    _log("risk_session...")
    rs_id = get_risk_session_id(session)
    if rs_id:
        _log(f"risk_session: {rs_id[:20]}...")
    else:
        _log("risk_session: FAILED (continuing)")

    # 2. Authorize → state + cookies
    _log("authorize...")
    verifier, challenge = generate_pkce_pair()
    state = start_authorize(session, verifier, challenge, rs_id)
    _log(f"state: {state[:30]}...")

    # 3. Camoufox: signup identifier (email + Turnstile)
    _log("camoufox: signup identifier + turnstile...")
    otp_since = time.time()
    result = await turnstile_signup_identifier(email_addr, state, proxy_url, attempt)
    _log(f"turnstile done → {result['next_url'][:80]}")

    # Transfer browser cookies to HTTP session.
    # The browser navigated through Auth0 which may have rotated session cookies.
    # Inject them into the jar so subsequent HTTP POSTs carry the right session.
    for name, value in result["cookies"].items():
        session.jar.set_cookie(_make_cookie(name, value, "converge-ai.us.auth0.com"))

    state = result["state"]
    step = result.get("step", "otp")

    # If browser already landed on OAuth callback (rare: no OTP/password needed)
    if step == "oauth_callback":
        m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", result["next_url"])
        if m:
            _log("browser landed on OAuth callback directly")
            tokens = exchange_code_for_tokens(session, m.group(1), verifier)
            return tokens

    # 4. Wait for OTP (skip if browser landed on password already)
    if step == "otp":
        _log("waiting OTP...")
        if otp_callback:
            otp_code = await otp_callback(email_addr, otp_since - 20)
        else:
            raise RuntimeError("No otp_callback provided")
        _log(f"OTP received: {otp_code}")

        # 5. Submit OTP via HTTP
        _log("HTTP: submit OTP...")
        resp = submit_otp_challenge(session, state, otp_code)
        _log(f"OTP response: status={resp.status} redirect={resp.is_redirect}")

        # Handle response: 302→password, or rendered password page, or error
        if resp.is_redirect:
            loc = resp.location
            if "code=" in loc:
                _log("OTP → OAuth callback (no password step)")
                code = extract_oauth_code(resp)
                tokens = exchange_code_for_tokens(session, code, verifier)
                return tokens
            # Follow to password page (establishes session for next POST)
            if not loc.startswith("http"):
                loc = f"{AUTH_HOST}{loc}"
            session.get(loc)
            # Update state if URL changed
            m = re.search(r"state=([A-Za-z0-9_\-=]+)", loc)
            if m:
                state = m.group(1)
        elif resp.status >= 400:
            # Check for specific errors
            if "invalid" in resp.body.lower() or "expired" in resp.body.lower():
                raise RuntimeError(f"OTP rejected: {_extract_error(resp.body)}")
            raise RuntimeError(f"OTP submit failed: {resp.status} {resp.body[:300]}")
        # else: 200 = rendered inline password page, continue

    # 6. Submit password via HTTP
    _log("HTTP: submit password...")
    resp = submit_password(session, state, password)
    _log(f"password response: status={resp.status} redirect={resp.is_redirect}")

    # 7. Follow redirect chain to get OAuth code
    if resp.is_redirect:
        loc = resp.location
        # Chase redirects until we find ?code= (max 5 hops)
        for _ in range(5):
            if "code=" in loc:
                break
            if not loc.startswith("http"):
                loc = f"{AUTH_HOST}{loc}"
            resp = session.get(loc)
            if resp.is_redirect:
                loc = resp.location
            elif "code=" in resp.url:
                loc = resp.url
                break
            else:
                break
        code = extract_oauth_code(HTTPResp(resp.status, resp.headers, resp.body, loc))
    elif resp.status >= 400:
        raise RuntimeError(f"password submit failed: {resp.status} {_extract_error(resp.body)}")
    else:
        # Check if final URL contains code (inline redirect via JS/meta)
        code = extract_oauth_code(resp)

    # 8. Exchange code for tokens
    _log("HTTP: token exchange...")
    tokens = exchange_code_for_tokens(session, code, verifier)
    _log(f"tokens OK (expires_in={tokens.get('expires_in')})")

    return tokens


# ── Cookie helper ────────────────────────────────────────────────────────────
def _make_cookie(name: str, value: str, domain: str):
    """Create a cookielib-compatible cookie for injection into CookieJar."""
    from http.cookiejar import Cookie
    return Cookie(
        version=0, name=name, value=value,
        port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
        path="/", path_specified=True,
        secure=True, expires=int(time.time()) + 86400,
        discard=False, comment=None, comment_url=None,
        rest={"HttpOnly": ""}, rfc2109=False,
    )


# ── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    async def _test():
        # Dry run: just test authorize + state extraction (no real signup)
        session = HTTPSession()
        verifier, challenge = generate_pkce_pair()
        rs_id = get_risk_session_id(session)
        print(f"risk_session_id: {rs_id}")
        state = start_authorize(session, verifier, challenge, rs_id)
        print(f"state: {state}")
        print(f"cookies: {len(session.jar)}")
        print("OK — authorize + state extraction works via HTTP")

    asyncio.run(_test())
