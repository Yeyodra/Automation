# getunikey farm

Automation hub farm for **[GetUniKey](https://www.getunikey.ai)** — Google OAuth signup → API key → smoke chat → save results.

**9router:** build OpenAI Compatible provider **manually** — see **[9ROUTER.md](./9ROUTER.md)**.  
Farm does **not** auto-inject keys into 9router.

| Doc | Content |
|-----|---------|
| **[API.md](./API.md)** | Full API: OAuth, token create, `/v1`, usage, playground/drawing/video |
| **[9ROUTER.md](./9ROUTER.md)** | How to create OpenAI Compatible node + connection shape |
| **[MODELS.md](./MODELS.md)** | Model list + chat/vision matrix |

---

## Pipeline (per account)

```
Google email|password (HUD list or file)
  → Camoufox: /sign-up?aff=… → Google login → session
  → POST /api/token/ + POST /api/token/{id}/key
  → smoke: POST /v1/chat/completions (Bearer key)
  → usage: GET billing/usage + /api/usage/token (non-fatal)
  → save results/  (no 9router inject)
```

---

## Hub / HUD

```powershell
python -m jobs list
python app.py                          # Job=getunikey
python -m jobs run getunikey -- -n 0 -c 1 -y
```

### HUD fields (job `getunikey`)

| Field | Role |
|-------|------|
| **Gmail** | Multiline `email\|password` (or `email:password`) |
| **RefURL** | Start URL; empty → `https://www.getunikey.ai/sign-up?aff=bTOY` |
| **-n** | Disabled — `n` = list size (`-n 0` = entire pool) |
| **-c / everyN** | Concurrency + WARP wave |

---

## Env (`GETUNIKEY_*`)

| Key | Default | Meaning |
|-----|---------|---------|
| `GETUNIKEY_ACCOUNTS_LIST` | — | HUD multiline list |
| `GETUNIKEY_ACCOUNTS_FILE` | `google_accounts.txt` | File fallback |
| `GETUNIKEY_REFERRAL_URL` | sign-up?aff=bTOY | Register start URL |
| `GETUNIKEY_BROWSER_HEADLESS` | `true` | `false` = show browser |
| `GETUNIKEY_SMOKE_TEST` | `true` | After key → `/v1/chat` |
| `GETUNIKEY_SMOKE_MODEL` | `qwen/qwen3.6-flash` | Smoke model (cheap) |
| `GETUNIKEY_SMOKE_REQUIRE` | `true` | Fail account if smoke fails |
| `GETUNIKEY_TOKEN_NAME` | `prod` | Token display name |

Hub maps shared `HEADLESS` → `GETUNIKEY_HEADLESS`; farm **ignores** that and uses `GETUNIKEY_BROWSER_HEADLESS` only.

---

## Results

```text
farms/getunikey/results/
  apikeys.txt              # key \t email \t user_id \t batch
  credentials.txt
  used_google.txt
  batch_*/accounts.json    # full row + smoke_* + usage_*
  batch_*/apikeys.txt
```

Fresh account (with aff): **quota/gift ≈ 1_500_000**, `used_quota ≈ 0`.

---

## 9router (summary)

```
Name:     GetUniKey / Unikey
Prefix:   guk or uk
API Type: Chat Completions
Base URL: https://www.getunikey.ai/v1
API Key:  <from results/apikeys.txt>
Model ID: gpt-5.6-sol
```

Details + SQLite connection shape: **[9ROUTER.md](./9ROUTER.md)**.

---

## Notes

- Google **2FA** not supported.
- Wrong password / account not found → may mark email used.
- Workspace Terms interstitial handled (scroll + Continue).
- Pre-OAuth `session` cookie is short; only long session (≥~350) counts as logged-in.
- Image/video catalog IDs are **not** chat endpoints — see MODELS.md.
