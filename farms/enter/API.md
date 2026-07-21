# Enter Pro API (`ek_` key)

Dokumentasi endpoint yang sudah diuji lewat farm + probe + HAR web (Juli 2026).  
Base host: `https://api.enter.pro`

---

## Auth

```http
Authorization: Bearer ek_...
Origin: https://enter.converge.ai
Referer: https://enter.converge.ai/
Accept: application/json
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
```

| Header | Wajib? | Catatan |
|--------|--------|---------|
| `Authorization: Bearer ek_…` | Ya | API key dari create key / `accounts.json` |
| `Origin` + `Referer` | **Ya** (praktis) | Tanpa ini Cloudflare sering **403 Error 1010** |
| `User-Agent` (browser-like) | **Ya** (praktis) | UA default Python/`urllib` → **403 CF 1010** `browser_signature_banned` |
| `X-Workspace-ID` | **Ya untuk chat** | Numeric workspace id, mis. `10000155856` |
| `Content-Type: application/json` | POST only | |

**Bukan** OpenAI key di `agent-api.converge.ai` — host itu minta JWT session user, bukan `ek_`.

Key didapat dari farm:

```text
results/batch_*/accounts.json  →  api_key.key
results/batch_*/accounts.txt   →  kolom api_key
results/all_apikeys.txt        →  ek_\temail\tworkspace_id\tbatch
```

---

## Dua surface: `ek_` API vs Web UI

| | API key (`ek_`) | Web project chat |
|--|-----------------|------------------|
| Auth | `Bearer ek_…` | `Bearer eyJ…` (Auth0 JWT session) |
| Chat path | `POST /code/api/v1/chat/completions` | `POST /code/api/v1/projects/{id}/thread/chat` |
| Set model | field `model` di body chat | `POST /code/api/v1/projects/{id}/model` dulu |
| Model format | **`vendor/slug`** | id polos di project: `claude-opus-4.8` |
| Model catalog | `/code/api/v1/models` (14) **atau** `/ai-capability/models` (44) | tab AI Models = capability list |

Web **tidak** mengirim `model` di body chat. Alur HAR:

```http
POST /code/api/v1/projects/{project_id}/model
{"model":"claude-sonnet-5"}

POST /code/api/v1/projects/{project_id}/thread/chat
{"chat_id":"…","prompt":"hii","attachments":[]}
```

Path project (`/model`, `/thread/chat`) butuh **JWT session** — `ek_` biasanya **403/401**.  
Jangan samakan: model yang hidup di UI web **belum tentu** hidup di `ek_` completions (contoh: Opus 4.8 / Sonnet 5 → web OK, `ek_` **502**).

---

## 1. List models

Ada **dua** endpoint list. Jangan campur.

### 1.1 Short list (code / legacy)

```http
GET /code/api/v1/models
```

~**14** id polos (`auto`, `minimax-m3`, `claude-opus-4.8`, …).  
`X-Workspace-ID` tidak wajib.  
Id di sini **bukan** string chat — chat butuh `vendor/slug` (lihat §1.3 / §2).

### 1.2 Full catalog (web **AI Models** tab)

```http
GET /code/api/v1/ai-capability/models
```

~**44** model (LLM + Video + Image + Music).  
`id` sudah format **`vendor/slug`** — ini yang dipakai chat `ek_` (untuk type `LLM`).

Contoh field:

```json
{
  "id": "anthropic/claude-opus-4.8",
  "name": "Claude Opus 4.8",
  "type": "LLM",
  "price_tier": "High"
}
```

| `type` | Chat `/chat/completions`? |
|--------|---------------------------|
| `LLM` | Ya (smoke) |
| `Video` / `Image` / `Music` | Bukan chat text — endpoint lain / web |

Snapshot probe (Juli 2026) disimpan di `results/ai_capability_models.json` bila ada.

### 1.3 Mapping short id → chat id

| `id` di `/models` | `model` di chat / capability |
|-------------------|------------------------------|
| `minimax-m3` | `minimax/minimax-m3` |
| `gpt-5.6-luna` | `openai/gpt-5.6-luna` |
| `deepseek-v4-pro` | `deepseek/deepseek-v4-pro` |
| `claude-opus-4.8` | `anthropic/claude-opus-4.8` |
| `claude-sonnet-5` | `anthropic/claude-sonnet-5` |
| `glm-5.2` | `z-ai/glm-5.2` |
| `kimi-k2.7-code` | `moonshotai/kimi-k2.7-code` |
| `qwen-3.7-plus` | `alibaba/qwen-3.7-plus` |

Id polos di body chat → sering **400 unsupported model**.

### Curl

```bash
# short
curl -s "https://api.enter.pro/code/api/v1/models" \
  -H "Authorization: Bearer ek_YOUR_KEY" \
  -H "Origin: https://enter.converge.ai" \
  -H "Referer: https://enter.converge.ai/" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# full (web AI Models)
curl -s "https://api.enter.pro/code/api/v1/ai-capability/models" \
  -H "Authorization: Bearer ek_YOUR_KEY" \
  -H "Origin: https://enter.converge.ai" \
  -H "Referer: https://enter.converge.ai/" \
  -H "User-Agent: Mozilla/5.0 …"
```

---

## 2. Chat (OpenAI-compatible)

```http
POST /code/api/v1/chat/completions
```

### Headers tambahan

```http
X-Workspace-ID: 10000155856
Content-Type: application/json
User-Agent: Mozilla/5.0 …
```

Tanpa `X-Workspace-ID` → **400** `"X-Workspace-ID header is required"`  
Tanpa browser UA → **403** CF 1010

### Body

```json
{
  "model": "minimax/minimax-m3",
  "messages": [
    { "role": "user", "content": "Say hi" }
  ],
  "max_tokens": 64,
  "stream": false
}
```

**OpenAI / GPT models:** pakai `max_completion_tokens` (bukan `max_tokens`):

```json
{
  "model": "openai/gpt-5.6-luna",
  "messages": [{ "role": "user", "content": "hi" }],
  "max_completion_tokens": 64
}
```

### Response (contoh)

```json
{
  "id": "…",
  "object": "chat.completion",
  "model": "MiniMax/MiniMax-M3",
  "choices": [
    {
      "index": 0,
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "…"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 180,
    "completion_tokens": 32,
    "total_tokens": 212
  }
}
```

### Curl

```bash
curl -s -X POST "https://api.enter.pro/code/api/v1/chat/completions" \
  -H "Authorization: Bearer ek_YOUR_KEY" \
  -H "Origin: https://enter.converge.ai" \
  -H "Referer: https://enter.converge.ai/" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" \
  -H "X-Workspace-ID: YOUR_WORKSPACE_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "minimax/minimax-m3",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 32
  }'
```

### PowerShell

```powershell
$key = "ek_...."
$ws  = "10000155856"
$body = @{
  model = "minimax/minimax-m3"
  messages = @(@{ role = "user"; content = "hi" })
  max_tokens = 32
} | ConvertTo-Json -Depth 5

curl.exe -s -X POST "https://api.enter.pro/code/api/v1/chat/completions" `
  -H "Authorization: Bearer $key" `
  -H "Origin: https://enter.converge.ai" `
  -H "Referer: https://enter.converge.ai/" `
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" `
  -H "X-Workspace-ID: $ws" `
  -H "Content-Type: application/json" `
  -d $body
```

### Python

```python
import json
import urllib.request

KEY = "ek_...."
WS = "10000155856"

req = urllib.request.Request(
    "https://api.enter.pro/code/api/v1/chat/completions",
    data=json.dumps({
        "model": "minimax/minimax-m3",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32,
    }).encode(),
    headers={
        "Authorization": f"Bearer {KEY}",
        "Origin": "https://enter.converge.ai",
        "Referer": "https://enter.converge.ai/",
        "X-Workspace-ID": WS,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    },
    method="POST",
)
print(urllib.request.urlopen(req, timeout=90).read().decode())
```

---

## 2b. Model status lewat `ek_` chat (smoke, Juli 2026)

Sumber: `GET /ai-capability/models` lalu 1 hit `/chat/completions` per LLM.  
Bukan jaminan selamanya — upstream bisa 502 sewaktu-waktu.

### OK (contoh yang lolos)

```text
openai/gpt-5.6-sol | openai/gpt-5.6-terra | openai/gpt-5.6-luna
openai/gpt-5.5 | openai/gpt-5.4 | openai/gpt-5.4-pro | openai/gpt-5.2-pro
alibaba/qwen-3.7-plus | alibaba/qwen-3.7-max
alibaba/qwen-3.6-plus | alibaba/qwen-3.6-max-preview
anthropic/claude-opus-4.6
anthropic/claude-sonnet-4.5
deepseek/deepseek-v4-pro
minimax/minimax-m3 | minimax/minimax-m2.7 | minimax/minimax-m2.5
moonshotai/kimi-k2.7-code | moonshotai/kimi-k2.6 | moonshotai/kimi-k2.5
z-ai/glm-5.2 | z-ai/glm-5.1 | z-ai/glm-5
```

### FAIL **502** origin (ada di UI, mati di `ek_` completions)

```text
anthropic/claude-opus-4.8
anthropic/claude-opus-4.7
anthropic/claude-sonnet-5
anthropic/claude-sonnet-4.6
google/gemini-3.5-flash
google/gemini-3.1-pro-preview
google/gemini-3.1-flash-lite-preview
```

Catatan:

- **502** = request diterima, upstream/gateway model down — **bukan** salah param body (salah param biasanya **400**).
- Web project path bisa tetap OK untuk model yang 502 di `ek_` (gateway beda + JWT).
- Claude “aman” di `ek_` saat probe: **`anthropic/claude-opus-4.6`**, **`anthropic/claude-sonnet-4.5`**.

Prefer model murah untuk smoke massal: `minimax/minimax-m3`, `openai/gpt-5.6-luna`.

---

## 3. Sisa credit / usage

### 3.1 Ringkas

```http
GET /code/api/v1/workspaces/{workspace_id}/credits
```

```json
{
  "code": 0,
  "message": "get workspace credits successfully",
  "data": { "credits": 199.99 }
}
```

### 3.2 Dashboard (detail)

```http
GET /code/api/v1/workspaces/{workspace_id}/credits/dashboard
```

| Path JSON | Arti |
|-----------|------|
| `data.credits_balance.total` | Sisa total |
| `data.credits_balance.breakdown.bonus` | Bonus (referral dll) |
| `data.credits_balance.breakdown.daily` | Daily |
| `data.credits_balance.breakdown.monthly` | Monthly |
| `data.credits_balance.breakdown.purchase` | Beli |
| `data.credits_balance.status` | mis. `normal` |
| `data.enter_ai_balance` | Saldo Enter AI (bisa `disabled`) |
| `data.usage_history` | Riwayat project (bisa kosong) |

### 3.3 Subscription / plan

```http
GET /code/api/v1/workspaces/{workspace_id}/subscription/status
```

Contoh free: `plan_type: free`, `daily_credits` di entitlement, daftar `ai_all_models` (trial) — **bukan** full capability list.

### 3.4 Referral rewards

```http
GET /code/api/v1/referral/rewards
```

```json
{
  "code": 0,
  "data": {
    "invitee_reward": 100,
    "inviter_reward": 100
  }
}
```

### Curl usage

```bash
WS=10000155856
KEY=ek_YOUR_KEY
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

curl -s "https://api.enter.pro/code/api/v1/workspaces/$WS/credits" \
  -H "Authorization: Bearer $KEY" \
  -H "Origin: https://enter.converge.ai" \
  -H "Referer: https://enter.converge.ai/" \
  -H "User-Agent: $UA"

curl -s "https://api.enter.pro/code/api/v1/workspaces/$WS/credits/dashboard" \
  -H "Authorization: Bearer $KEY" \
  -H "Origin: https://enter.converge.ai" \
  -H "Referer: https://enter.converge.ai/" \
  -H "User-Agent: $UA"
```

---

## 4. Endpoint lain (user / workspace)

Semua `GET`, auth sama (`Bearer ek_` + Origin + browser UA).  
`X-Workspace-ID` biasanya tidak perlu kecuali disebut.

| Method | Path | Keterangan |
|--------|------|------------|
| GET | `/code/api/v1/users/info` | Info user (email masked) |
| GET | `/code/api/v1/workspaces` | List workspace (+ `id`, `public_id`, …) |
| GET | `/code/api/v1/workspaces/{id}/subscription/status` | Plan / entitlement |
| GET | `/code/api/v1/workspaces/{id}/credits` | Sisa credit |
| GET | `/code/api/v1/workspaces/{id}/credits/dashboard` | Credit + breakdown |
| GET | `/code/api/v1/referral/rewards` | Reward referral |
| GET | `/code/api/v1/models` | List model pendek (~14) |
| GET | `/code/api/v1/ai-capability/models` | Full catalog web (~44) |

### Tidak bisa pakai API key

| Path | Response |
|------|----------|
| `…/workspaces/{id}/api-keys` | **403** — butuh user session JWT |
| `…/projects/{id}/model` | JWT session (web) |
| `…/projects/{id}/thread/chat` | JWT session (web) |
| `https://agent-api.converge.ai/v1/*` | **401** dengan `ek_` — butuh OAuth JWT |

---

## 5. Create API key (session JWT only)

Dipakai farmer setelah login (bukan `ek_`):

```http
POST /code/api/v1/workspaces/{workspace_id}/api-keys
Authorization: Bearer <oauth_access_token>
Content-Type: application/json

{
  "name": "farm",
  "scope": "all",
  "reveal_policy": "create_only"
}
```

`create_only` = plaintext key hanya di response create (simpan langsung).

---

## 6. Error umum

| HTTP | Penyebab | Fix |
|------|----------|-----|
| **403** CF 1010 `browser_signature_banned` | Missing Origin **atau** non-browser UA | `Origin` + `Referer` + Chrome-like `User-Agent` |
| **400** `X-Workspace-ID header is required` | Chat tanpa header | Set `X-Workspace-ID` |
| **400** `unsupported model` | Model id salah format | Pakai `vendor/slug` dari capability list |
| **400** `max_tokens` unsupported | Model OpenAI | Ganti `max_completion_tokens` |
| **502** `origin_bad_gateway` | Upstream model/gateway down | Retry / ganti model (bukan salah body) |
| **403** API keys / project thread | Pakai `ek_` di path JWT-only | Pakai session JWT |
| **401** agent-api | `ek_` di agent-api | Pakai JWT / host yang benar |

Auth0 login di browser + MITM (HTTP Toolkit) sering `error=access_denied&error_description=risk_control_blocked`.  
Tangkap model mapping via **DevTools Network** setelah login normal (tanpa intercept Auth0), atau parse HAR — jangan MITM di `auth.converge.ai`.

---

## 7. Cheat sheet

| Butuh | Call |
|-------|------|
| List model pendek | `GET /code/api/v1/models` |
| List model web (AI Models) | `GET /code/api/v1/ai-capability/models` |
| Chat | `POST /code/api/v1/chat/completions` + `X-Workspace-ID` + browser UA |
| Sisa credit | `GET …/workspaces/{ws}/credits` |
| Detail usage | `GET …/workspaces/{ws}/credits/dashboard` |
| Plan | `GET …/workspaces/{ws}/subscription/status` |

Workspace id: field `workspace_id` di `accounts.json` / kolom ke-3 `all_apikeys.txt`, atau `data.workspaces[0].id` dari list workspaces.

---

## 8. Output file (setelah farm sukses)

Hanya akun **sukses** (punya `ek_` key) yang di-append ke agregat:

```text
results/all_apikeys.txt
  ek_<key>\t<email>\t<workspace_id>\t<batch_id>
```

Probe/smoke opsional:

```text
results/ai_capability_models.json
results/smoke_chat_*.json
results/smoke_credits_*.json
results/smoke_capability_*.json
```

---

## 9. Quick smoke (1 key)

```python
# list capability + one cheap chat
import json, urllib.request

KEY, WS = "ek_....", "10000155856"
H = {
    "Authorization": f"Bearer {KEY}",
    "Origin": "https://enter.converge.ai",
    "Referer": "https://enter.converge.ai/",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "X-Workspace-ID": WS,
}
req = urllib.request.Request(
    "https://api.enter.pro/code/api/v1/ai-capability/models", headers=H
)
print(urllib.request.urlopen(req, timeout=60).read()[:200])

H["Content-Type"] = "application/json"
body = json.dumps({
    "model": "minimax/minimax-m3",
    "messages": [{"role": "user", "content": "ok"}],
    "max_tokens": 8,
}).encode()
req = urllib.request.Request(
    "https://api.enter.pro/code/api/v1/chat/completions",
    data=body, headers=H, method="POST",
)
print(urllib.request.urlopen(req, timeout=90).read().decode())
```
