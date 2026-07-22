# GetUniKey → 9router (OpenAI Compatible provider)

Farm **does not** auto-inject into 9router. Build the provider node manually, then add keys (UI or later script).

Reference backup shape: `9router-backup-2026-07-22T15-20-24-897Z.json`  
(node **Unikey**, prefix **uk**).

---

## 1. Create provider node (UI)

**Add OpenAI Compatible** (not Grok CLI / Antigravity / etc.):

| Field | Value |
|-------|--------|
| **Name** | `GetUniKey` or `Unikey` |
| **Prefix** | short unique, e.g. `guk` or `uk` |
| **API Type** | **Chat Completions** |
| **Base URL** | `https://www.getunikey.ai/v1` |
| **API Key (for Check)** | one farmed key from `results/apikeys.txt` |
| **Model ID (optional)** | `gpt-5.6-sol` (proven 200) |

Click **Check** → expect 200 → **Create**.

### Do not use

- `https://api.openai.com/v1` (OpenAI default)
- Base without `/v1` (`https://www.getunikey.ai` alone often wrong)
- **Responses** API type first (farm/smoke uses Chat Completions)

---

## 2. What 9router stores (SQLite / backup)

### `providerNodes` (one row per custom OpenAI node)

```json
{
  "id": "openai-compatible-chat-<uuid>",
  "type": "openai-compatible",
  "name": "Unikey",
  "prefix": "uk",
  "apiType": "chat",
  "baseUrl": "https://www.getunikey.ai/v1",
  "createdAt": "…",
  "updatedAt": "…"
}
```

**Important:** connection rows use **`provider` = this `id`**, not the friendly name.

### `providerConnections` (one row per API key)

Example from backup (redacted):

```json
{
  "id": "<uuid>",
  "provider": "openai-compatible-chat-02d4fa66-191e-4d0e-8512-3c01b2a0328a",
  "authType": "apikey",
  "name": "pepd",
  "email": null,
  "priority": 1,
  "isActive": true,
  "defaultModel": "gpt-5.6-sol",
  "apiKey": "<farmed_key>",
  "testStatus": "active",
  "providerSpecificData": {
    "prefix": "uk",
    "apiType": "chat",
    "baseUrl": "https://www.getunikey.ai/v1",
    "nodeName": "Unikey",
    "connectionProxyEnabled": false,
    "connectionProxyUrl": "",
    "connectionNoProxy": ""
  }
}
```

DB path (Windows): `%APPDATA%\9router\db\data.sqlite`  
Table: `providerConnections` columns roughly:  
`id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt`  
(with JSON blob in `data` for key + metadata — same pattern as enter/grok inject).

Always set **`testStatus": "active"`** so the pool is usable without Test One-by-One.

---

## 3. Recommended models on the node

### Safe default (chat)

- **`gpt-5.6-sol`** — farm smoke + vision OK

### Solid chat (smoke all-models OK)

`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.6`, `gpt-5.5`, `gpt-5.4`,  
`claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`,  
`google/gemini-3.5-flash`, `google/gemini-3.1-flash-lite`,  
`x-ai/grok-4.3`,  
`deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4-flash`,  
`z-ai/glm-5.2`, `z-ai/glm-5.1`, `z-ai/glm-5-turbo`,  
`qwen/qwen3.7-max`, `qwen/qwen3.7-plus`, `qwen/qwen3.6-plus`, `qwen/qwen3.6-flash`

### Do not route as chat

| ID | Reason |
|----|--------|
| `*-image*`, `gpt-image-2` | image gen / 404 on chat |
| `bytedance/seedance-*`, `kwaivgi/kling-*` | video |
| flaky 500s | `minimax/minimax-m3`, `moonshotai/kimi-*` (at probe time) |

Full matrix: [MODELS.md](./MODELS.md).

---

## 4. Keys from farm

After HUD/CLI farm run:

```text
farms/getunikey/results/apikeys.txt
# api_key \t email \t user_id \t batch_id

farms/getunikey/results/batch_*/accounts.json
# full row: api_key, quota, gift_quota, smoke_*, usage_*
```

Add each key as a **connection** under the GetUniKey OpenAI Compatible node (UI bulk or future inject script).

### Optional connection metadata

```json
"providerSpecificData": {
  "email": "user@domain.com",
  "userId": 339,
  "tokenId": 154
}
```

---

## 5. Manual smoke before trusting a key

```powershell
$key = "<from apikeys.txt>"

# models
curl -s -H "Authorization: Bearer $key" https://www.getunikey.ai/v1/models

# chat
curl -s -X POST https://www.getunikey.ai/v1/chat/completions `
  -H "Authorization: Bearer $key" -H "Content-Type: application/json" `
  -d "{\"model\":\"gpt-5.6-sol\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":false,\"max_tokens\":16}"
```

Farm already runs smoke + usage after create when `GETUNIKEY_SMOKE_TEST=true`.

---

## 6. Later: auto-inject (not enabled)

When you want farm → SQLite again:

- Resolve `provider` = `providerNodes.id` for your prefix/name  
- Insert `authType=apikey`, `testStatus=active`, `apiKey`, `defaultModel`  
- Dedup on same `provider` + `apiKey`  
- Pattern reference: `farms/enter/farm.py` `inject_to_9router` (enter-converge)

Until then: **manual provider + manual/scripted key add only**.

---

## 7. Checklist

- [ ] Create OpenAI Compatible node (`/v1`, Chat Completions)  
- [ ] Check with one farmed key + `gpt-5.6-sol`  
- [ ] Set default model  
- [ ] Import more keys from `results/apikeys.txt`  
- [ ] Alias only chat-capable models (skip image/video IDs)  
- [ ] Confirm `testStatus=active` on connections  
