# Tasklet farm

Pure HTTP magic link signup via exzork mailer → 300K credits/day per account.  
No browser, no Google OAuth. ~10s per account, fully parallelizable.

---

## Pipeline (per account)

```
exzork: create random mailbox (random@sub.domain.tld)
  → POST /api/auth/magic-link/request {email, magicLinkSecret}
  → poll exzork for email → extract ?token= from link
  → POST /api/auth/magic-link/verify {token} → {pin}
  → POST /api/signIn {type:"magic_link", magicLinkSecret, pin} → sessionToken
  → POST /api/organization/create → org_id
  → POST /api/billing/claimDailyBonus → +300K
  → save result
```

---

## Hub / HUD

```powershell
# CLI:
python -m jobs run tasklet --warp-every-n 3 -- -n 10 -c 5 -y
```

- `-n` = number of accounts to create (no pool file needed — mailboxes auto-generated)
- `-c` = concurrent workers
- No Gmail list required

---

## Env (`TASKLET_*`)

| Key | Default | Meaning |
|-----|---------|---------|
| `EXZORK_API_KEY` | _(required)_ | Exzork mailer API key (`tm_...`) |
| `TASKLET_EXZORK_DOMAIN` | falls back to `EXZORK_DOMAIN` | Subdomain for mailboxes (wildcard claim) |
| `TASKLET_CONCURRENT` | `1` | Parallel workers |
| `TASKLET_ACCOUNT_TIMEOUT` | `120` | Per-account timeout (seconds) |
| `TASKLET_RESULTS_DIR` | `results/` | Output directory |
| `TASKLET_WARP_EVERY_N` | `0` (off) | WARP rotate after N OK |

### Hub `.env` example

```env
EXZORK_API_KEY=tm_5222ede48a6a2e2ddaeb5667af2ccc829de28713ec09c87e
TASKLET_EXZORK_DOMAIN=mail.wowoanjay.my.id
```

**Note:** domain must be claimed as wildcard (`*.wowoanjay.my.id`) in exzork, then use a subdomain (e.g. `mail.wowoanjay.my.id`) for mailbox creation.

---

## Results

```
farms/tasklet/results/
  batch_YYYYMMDD_HHMMSS/
    accounts.json          ← array of result objects
```

### Result object

```json
{
  "email": "f1ercrbvca@mail.wowoanjay.my.id",
  "userId": "u_...",
  "sessionToken": "128-char hex",
  "organizationId": "org_...",
  "workspaceId": "ws_...",
  "totalCredits": 300000,
  "dailyBonusClaimed": true,
  "timestamp": "2026-08-02T14:47:32Z"
}
```

---

## API (post-farm usage)

Base: `https://api.tasklet.ai`  
Auth: `Authorization: Bearer <sessionToken>`

### Auth endpoints

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| POST | `/api/auth/magic-link/request` | `{email, magicLinkSecret}` | Send magic link email |
| POST | `/api/auth/magic-link/verify` | `{token}` | Verify token → get PIN |
| POST | `/api/signIn` | `{type:"magic_link", magicLinkSecret, pin, allowDuplicate}` | Complete sign-in → sessionToken |
| POST | `/api/signIn` | `{type:"oauth2code", provider:"google", code, allowDuplicate}` | Google OAuth sign-in |

### Core endpoints

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| POST | `/api/profile` | `null` | Get user + orgs + trial info |
| POST | `/api/organization/create` | `{name:"..."}` | Create org (triggers plan) |
| POST | `/api/billing/claimDailyBonus` | `{organizationId}` | +300 credits (resets midnight UTC) |
| POST | `/api/billing/creditGrants` | `{organizationId}` | List credit grants + totalAvailable |
| POST | `/api/sendChatMessage` | `{agentId, message, modelConfig, workspaceId, ...}` | Send chat to model |
| POST | `/api/workspaces/getUsage` | `{workspaceId}` | Usage stats |

### Available models (via `modelConfig.model`)

| Category | Models |
|----------|--------|
| Claude | `claude_haiku_4_5`, `claude_sonnet_4_6`, `claude_sonnet_5`, `claude_opus_4_6`, `claude_opus_4_7`, `claude_opus_4_8`, `claude_opus_4_8_fast`, `claude_opus_5`, `claude_fable_5` |
| GPT | `gpt_5_5`, `gpt_5_5_fast`, `gpt_5_6_sol`, `gpt_5_6_terra`, `gpt_5_6_luna` |
| Gemini | `gemini_flash_3_preview`, `gemini_flash_3_5`, `gemini_flash_3_6`, `gemini_flash_lite_3_1`, `gemini_flash_lite_3_5`, `gemini_pro_3_1_preview` |
| Other | `grok_4_5`, `kimi_k3`, `muse_spark_1_1` |

### sendChatMessage example

```json
{
  "agentId": "new",
  "message": "hello",
  "timezone": "America/Los_Angeles",
  "fileIds": [],
  "intelligence": "advanced",
  "modelConfig": {
    "model": "claude_opus_5",
    "thinkingEffort": "low",
    "chatHistory": "default",
    "serviceTier": "standard",
    "preset": "basic"
  },
  "agentConfig": {"preview": true},
  "workspaceId": "ws_..."
}
```

Response: `{"agentId": "a_..."}` — chat result arrives via WebSocket (`wss://api.tasklet.ai/api/sync`).

---

## Pricing / Credits

| Plan | Monthly | Daily bonus | Notes |
|------|---------|-------------|-------|
| Free (email signup) | $0 | 300/day | Limited usage, 10 automation executions |
| Starter | $25 | 600/day | 10K credits/month |
| Pro | $100 | 600/day | 40K credits/month |
| Pro Trial (personal Gmail OAuth only) | free 7 days | 600/day | 5M trial credits |

- **Email magic link signup** → Free plan (300/day)
- **Personal Gmail OAuth** → pro_trial (5M + 600/day = 5.6M for 7 days)
- **GSuite/Workspace OAuth** → Free plan (300/day only, no trial)
- Daily bonus resets at midnight UTC

---

## Backup: Google OAuth browser flow

`farm_browser.py` — original Camoufox-based flow for Google OAuth signup. Use when:
- You need pro_trial credits (5M) with personal Gmail accounts
- Email magic link is rate-limited or blocked

```powershell
# Run browser backup directly:
python farms/tasklet/farm_browser.py -n 5 -c 1 -y
```

Requires `google_accounts.txt` with `email|password` lines.

---

## OAuth config (Google) — browser backup only

| Key | Value |
|-----|-------|
| Client ID | `252828688609-4s8sdku4s84rlp4b6k1irb1fcf0aplhm.apps.googleusercontent.com` |
| Redirect URI | `https://tasklet.ai/oauth2callback` |
| Scopes | `email profile` |
| Response type | `code` |
| Prompt | `select_account` |

---

## Known issues / Notes

- **Email signup = Free plan only** — 300 daily bonus, no trial. For pro_trial (5M), use browser backup with personal Gmail.
- **Exzork domain must be wildcard** — claim `*.domain.tld`, use `sub.domain.tld` for mailboxes.
- **Email retention** — exzork deletes messages after 1 hour. Farm polls within 60s so this is fine.
- **No `/api/models` endpoint** — model list is enforced server-side via validation error on `sendChatMessage`.
- **WebSocket** (`/api/sync`) delivers chat responses — `sendChatMessage` only returns `agentId`, actual response streams via WS.
- **Magic link secret** expires after 1 hour. Farm uses it immediately so no issue.
- **`allowDuplicate: false`** — if email already tied to Google/Microsoft OAuth, signIn rejects. Set `true` to force separate email account.
