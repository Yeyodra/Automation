# GetUniKey — model catalog & capability matrix

Source: `GET /v1/models` + live `POST /v1/chat/completions` smokes (2026-07-22), key farmed via Google OAuth farm.

Base: `https://www.getunikey.ai/v1`  
Auth: `Authorization: Bearer <api_key>`

---

## 1. Full catalog (~31)

| # | Model ID | owned_by | Chat smoke | Vision (local image) | Notes |
|---|----------|----------|------------|----------------------|--------|
| 1 | `gpt-5.6-sol` | openai | ✅ | ✅ | **Default smoke / 9router** |
| 2 | `gpt-5.6-terra` | openai | ✅ | ✅ | |
| 3 | `gpt-5.6-luna` | openai | ✅ | ✅ | |
| 4 | `gpt-5.6` | openai | ✅ | ✅ | |
| 5 | `gpt-5.5` | openai | ✅ | ✅ | |
| 6 | `gpt-5.4` | openai | ✅ | ✅ | |
| 7 | `gpt-image-2` | openai | ❌ 403 | ❌ 403 | Image gen; quota pre-deduct |
| 8 | `openai/gpt-5.4-image-2` | openrouter | ❌ 404 | ❌ 404 | Not chat tool endpoint |
| 9 | `google/gemini-3.5-flash` | openrouter | ✅ | ✅ | |
| 10 | `google/gemini-3.1-pro-preview` | openrouter | ⚠ empty | ✅ | Chat sometimes empty content |
| 11 | `google/gemini-3.1-flash-image` | openrouter | ❌ 404 | ❌ 404 | Image |
| 12 | `google/gemini-3.1-flash-lite` | openrouter | ✅ | ✅ | |
| 13 | `google/gemini-3-pro-image` | openrouter | ❌ 429 | ❌ 429 | Image / rate |
| 14 | `x-ai/grok-4.3` | openrouter | ✅ | ✅ | |
| 15 | `deepseek/deepseek-v4-pro` | openrouter | ✅ | ❌ 404 | No image input on gateway |
| 16 | `deepseek/deepseek-v4-flash` | openrouter | ✅ | — | |
| 17 | `z-ai/glm-5.2` | openrouter | ✅ | ⚠ | |
| 18 | `z-ai/glm-5.1` | openrouter | ✅ | — | |
| 19 | `z-ai/glm-5-turbo` | openrouter | ✅ | — | |
| 20 | `minimax/minimax-m3` | openrouter | ❌ 500 | — | Upstream |
| 21 | `qwen/qwen3.7-max` | openrouter | ✅ | ❌ 404 | No image input |
| 22 | `qwen/qwen3.7-plus` | openrouter | ✅ | — | |
| 23 | `qwen/qwen3.6-plus` | openrouter | ✅ | ✅ | |
| 24 | `qwen/qwen3.6-flash` | openrouter | ✅ | — | |
| 25 | `moonshotai/kimi-k2.7-code` | openrouter | ❌ 500 | — | Upstream |
| 26 | `bytedance/seedance-2.0-fast` | openrouter | ❌ 404 | ❌ | **Video** (web `/api/video/generate`) |
| 27 | `claude-opus-4-6` | claude | ✅ | ✅ | |
| 28 | `claude-opus-4-7` | claude | ✅ | ✅ | |
| 29 | `claude-opus-4-8` | claude | ✅ | ✅ | Strong vision |
| 30 | `kwaivgi/kling-v3.0-pro` | openrouter | ❌ 404 | ❌ | Video/image gen |
| 31 | `moonshotai/kimi-k3` | openrouter | ❌ 500 | ❌ 500 | Upstream |

Chat smoke summary: **21 OK / 10 fail** (of 31).

---

## 2. Recommended for 9router

### Default

```text
gpt-5.6-sol
```

### Chat pool (enable)

All ✅ chat rows above (GPT-5.6 family, Claude Opus 4.6–4.8, Gemini flash/lite, Grok, DeepSeek, GLM, Qwen text, etc.).

### Disable / do not alias as chat

- Any `*image*`
- `seedance*`, `kling*`
- Flaky 500s if you care about reliability: `minimax-m3`, `kimi-*`

---

## 3. Vision request shape

```http
POST /v1/chat/completions
Authorization: Bearer <key>
```

```json
{
  "model": "gpt-5.6-sol",
  "messages": [{
    "role": "user",
    "content": [
      { "type": "text", "text": "Describe this image in one sentence." },
      { "type": "image_url", "image_url": { "url": "data:image/png;base64,..." } }
    ]
  }],
  "stream": false,
  "max_tokens": 80
}
```

`image_url.url` may also be a public `https://…` image URL.

---

## 4. Web-only models (playground / drawing)

Not the same as `/v1` IDs:

| Web | Path | Example model string |
|-----|------|----------------------|
| Playground | `POST /pg/chat/completions` | `gpt-5.6-sol`, `claude-opus-4-8` + `"group":"unikey"` |
| Drawing | `POST /api/drawing/generate` | `Nano Banana` |
| Video | `POST /api/video/generate` | `bytedance/seedance-2.0-fast` |

These need **session**, not Bearer key → not for OpenAI Compatible 9router node.

---

## 5. Re-probe

```powershell
# list
curl -s -H "Authorization: Bearer $KEY" https://www.getunikey.ai/v1/models

# single chat
curl -s -X POST https://www.getunikey.ai/v1/chat/completions `
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" `
  -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"OK"}],"stream":false,"max_tokens":8}'
```

Catalog can change over time; re-run smoke when GetUniKey updates models.
