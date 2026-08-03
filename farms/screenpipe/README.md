# ScreenPipe Cloud Token Farmer

Pure HTTP account farmer for [ScreenPipe](https://screenpipe.com) — Clerk signup + mail.tm OTP + JWT token.  
**No browser, no external deps** — stdlib only (urllib, http.cookiejar, threading).

---

## Pipeline (per account)

```
mail.tm: create temp email → get bearer token
  → Clerk: GET /v1/client (cookie init)
  → Clerk: POST sign_ups (email + password)
  → Clerk: POST prepare_verification (email_code strategy)
  → mail.tm: poll messages → extract 6-digit OTP
  → Clerk: POST attempt_verification (OTP)
  → Clerk: POST sessions/{id}/tokens → JWT (60s)
  → Smoke: POST api.screenpipe.com/v1/chat/completions
  → Save results/batch_*/accounts.json
```

---

## Hub / HUD

```powershell
python -m jobs list
python -m jobs run screenpipe -- -n 3 -c 1 -y
# With WARP rotation every 3 successes:
python -m jobs run screenpipe --warp-every-n 3 -- -n 10 -c 3 -y
```

### CLI flags

| Flag | Default | Meaning |
|------|---------|---------|
| `-n` / `--count` | 1 | Accounts to create |
| `-c` / `--concurrent` | 1 | Thread concurrency |
| `-y` / `--yes` | false | Skip confirmation prompt |

---

## Env (`SCREENPIPE_*`)

| Key | Default | Meaning |
|-----|---------|---------|
| `SCREENPIPE_SMOKE_TEST` | `true` | Verify JWT works after creation |
| `SCREENPIPE_SMOKE_MODEL` | `claude-sonnet-5` | Model for smoke test |
| `SCREENPIPE_OTP_TIMEOUT` | `90` | Seconds to wait for OTP email |
| `SCREENPIPE_OTP_POLL_INTERVAL` | `5` | Poll interval (seconds) |
| `SCREENPIPE_PASSWORD` | (auto) | Override password for all accounts |
| `SCREENPIPE_INJECT_DB` | `true` | Auto-inject to 9router SQLite after success |
| `SCREENPIPE_WARP_EVERY_N` | `0` | WARP rotate after N successes (0=off) |
| `SCREENPIPE_CONCURRENT` | `1` | Override concurrent threads |

Hub shared keys (`WARP_EVERY_N`, `CONCURRENT`, `WARP_SETTLE_AFTER`) also apply as fallbacks.

---

## Results format

```
farms/screenpipe/results/
  batch_YYYYMMDD_HHMMSS_hex/
    accounts.json    # [{email, mail_password, screenpipe_password, session_id, jwt, smoke, ...}]
    accounts.txt     # email\tsession_id\tjwt_prefix...
    failed.json      # [{attempt, error, at}]
    batch_meta.json  # batch config snapshot
```

### accounts.json row

```json
{
  "email": "spXXX@web-library.net",
  "mail_password": "hex",
  "screenpipe_password": "xK9abcdef12!qZgh34",
  "session_id": "sess_xxx",
  "sign_up_id": "sua_xxx",
  "jwt": "eyJ...",
  "smoke": {"ok": true, "model": "claude-sonnet-5", "content": "Hello!"},
  "created_at": "2026-08-02T...",
  "batch_id": "batch_..."
}
```

---

## API usage (after farming)

JWT is valid ~60 seconds. Use it directly:

```bash
curl -X POST https://api.screenpipe.com/v1/chat/completions \
  -H "Authorization: Bearer <jwt>" \
  -H "User-Agent: screenpipe-app/2.5.149" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}]}'
```

For longer-lived access, refresh the JWT from the session (see below).

---

## Token refresh flow (sign-in → session → JWT)

Session lives ~7 days. JWT lives ~60s. To get a fresh JWT:

```
POST clerk.screenpipe.com/v1/client/sign_ins?_clerk_js_version=5.56.0
  Body: identifier=<email>&strategy=password&password=<password>
  → status=complete → created_session_id=sess_xxx

POST clerk.screenpipe.com/v1/client/sessions/{sess_id}/tokens?_clerk_js_version=5.56.0
  → { "jwt": "eyJ..." }
```

Headers: same Clerk headers (Origin, Referer, User-Agent, cookies from /v1/client init).

Store `email + screenpipe_password + session_id` from results for refresh without re-signup.

---

## Notes / Limitations

- **mail.tm domains rotate** — code fetches `/domains` dynamically; `web-library.net` is current default fallback.
- **No browser needed** — entire flow is pure HTTP (urllib + http.cookiejar).
- **Clerk cookies required** — the `__client` cookie from step 3 must persist across steps 4-8 (handled by CookieJar).
- **Password avoids HIBP** — UUID-based generation (`xK9{hex}!qZ{hex}`) passes Clerk's pwned-password check.
- **Rate limits** — Clerk and mail.tm may rate-limit; use `-c 1` for safety, scale up carefully.
- **JWT expiry** — 60 seconds; for continuous use, implement refresh loop using stored credentials.
- **WARP integration** — after each success, checks `WARP_EVERY_N`; drains in-flight threads then rotates IP.
