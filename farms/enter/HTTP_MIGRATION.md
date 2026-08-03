# Enter Farm — HTTP Migration Plan

## Architecture: Hybrid (Camoufox Turnstile + HTTP rest)

### Current vs New

| Step | Current (full browser) | New (hybrid) |
|------|----------------------|--------------|
| risk_session_id | HTTP ✅ | HTTP (no change) |
| /authorize → state+cookies | Browser page.goto | HTTP GET + cookie jar |
| /u/signup/identifier (email) | Browser fill+click | **Camoufox minimal** (Turnstile) |
| Wait OTP | HTTP (gptmail API) ✅ | HTTP (no change) |
| /u/signup/challenge (OTP) | Browser fill+click | **HTTP POST** |
| /u/signup/password | Browser fill+click | **HTTP POST** |
| OAuth redirect → code | Browser URL watch | **HTTP follow 302** |
| /oauth/token exchange | HTTP ✅ | HTTP (no change) |
| post-auth setup | HTTP ✅ | HTTP (no change) |

### Key Discovery (Probed & Confirmed)

1. Auth0 UL accepts `application/x-www-form-urlencoded` POST with:
   - `state` (from /authorize 302)
   - `username` or `email` (signup identifier field name = `email`)
   - `captcha` (Turnstile token)
   - `action=default`

2. Session is cookie-based (`auth0` + `did` cookies from /authorize)

3. After identifier submit → 302 to `/u/signup/challenge?state=X`
4. After OTP submit → 302 to `/u/signup/password?state=X`  
5. After password submit → 302 to `redirect_uri?code=XXX&state=YYY`

### Turnstile Strategy

Keep Camoufox **only for the signup identifier page**:
- Navigate to `/u/signup/identifier?state=X`
- Fill email
- Let Camoufox auto-pass Turnstile (like current flow)
- Submit form
- **Intercept the response** (302 Location or rendered challenge page)
- Close browser immediately
- Continue with HTTP for all remaining steps

### Form Fields (confirmed from HTML)

#### Signup Identifier (`/u/signup/identifier`)
```
state=<auth0_state>
email=<email_address>        # field name is "email" 
captcha=<turnstile_token>    # populated by CF widget
action=default
js-available=true
webauthn-available=false
is-brave=false
webauthn-platform-available=false
```

#### Challenge / OTP (`/u/signup/challenge`)
```
state=<auth0_state>
code=<6_digit_otp>
action=default
```

#### Password (`/u/signup/password`)
```
state=<auth0_state>
password=<password>
re-enter-password=<password>   # if present
action=default
```

### Benefits

- RAM: 300-500MB/worker → ~50MB (browser only during Turnstile, ~5-10s)
- Speed: 60-90s/account → 25-40s
- Concurrency: 3 workers (16GB) → 10-15 workers
- Reliability: No DOM race conditions for OTP/password steps
