# Enter / Converge farm

Port of Coverage/enter-farm. Email default: **gptmail** (no IMAP).

| Item | Value |
|------|--------|
| Job | `enter` / `enter-farm` |
| Prefix | `ENTER_` |
| Email | `gptmail` default (override `ENTER_EMAIL_MODE`) |
| WARP | hub `core.warp` everyN 1:1 `-c` |

## Hub `.env`

```env
ENTER_EMAIL_MODE=gptmail
ENTER_GIFT_CODE=XXXX
ENTER_HEADLESS=false
# shared GPTMAIL_* maps → ENTER_GPTMAIL_*
GPTMAIL_API=https://mail.chatgpt.org.uk
```

Keep `EMAIL_MODE=domain` for grok/outlook if needed — enter uses **ENTER_EMAIL_MODE** override.

## Run

```powershell
python -m jobs run enter -- -n 1 -c 1 -y --headed
```
