# Outlook farm (HAR flow)

Microsoft account signup via **Camoufox UI**, aligned with captured `outlook.har`:

```
signup.live.com → email (EASI) → OTP (IMAP) → password → birth → name
→ HUMAN press-hold (core.px_hold) → create → skip passkey → save
```

| Item | Value |
|------|--------|
| Job | `outlook` / `outlook-farm` |
| Prefix | `OUTLOOK_` |
| Email | `domain` \| `plus_trick` (IMAP only) |
| Captcha | in-process `core.px_hold` (no `:8877`) |
| Default stub | `OUTLOOK_STUB=false` (full browser) |

## Hub `.env`

```env
IMAP_USER=...
IMAP_PASS=...
EMAIL_DOMAIN=your-catchall.example
ACCOUNT_PASSWORD=YourPass1!

OUTLOOK_HEADLESS=false
OUTLOOK_STUB=false
```

## Run

```powershell
python -m jobs list
python -m jobs run outlook -- -n 1 -c 1 -y
```

Results: `farms/outlook/results/batch_<id>/`

## Modules

- `core.mail` — email + IMAP OTP
- `core.px_hold` — Press & Hold (same page)
- Camoufox — browser FP

Stub: `OUTLOOK_STUB=true` = reserve email only.
