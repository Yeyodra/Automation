# ScreenPipe Cloud API Reference

OpenAI-compatible endpoint proxied through ScreenPipe Cloud.

---

## Base URL

```
https://api.screenpipe.com/v1
```

## Required Headers

| Header | Value | Notes |
|--------|-------|-------|
| `Authorization` | `Bearer <jwt>` | 60s Clerk session JWT |
| `User-Agent` | `screenpipe-app/2.5.149` | **MANDATORY** — Cloudflare 403 without it |
| `Content-Type` | `application/json` | Standard |

---

## Endpoints

### `POST /v1/chat/completions`

Standard OpenAI chat completions. Supports streaming (SSE) and non-streaming.

**Request:**

```json
{
  "model": "claude-sonnet-5",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"}
  ],
  "stream": false
}
```

**Response (non-streaming):**

```json
{
  "choices": [
    {
      "message": {
        "content": "Hello! How can I help you?",
        "role": "assistant",
        "tool_calls": []
      }
    }
  ],
  "usage": {
    "prompt_tokens": 196,
    "completion_tokens": 128,
    "total_tokens": 324,
    "prompt_tokens_details": {"cached_tokens": 0},
    "cache_creation_input_tokens": 0
  }
}
```

**Response (streaming):** Standard SSE `data: {...}` chunks, `data: [DONE]` at end.

**Notes:**
- `id` field may be absent in responses
- `tool_calls` may be empty array `[]` or absent
- No `finish_reason` in some responses

### `GET /v1/models`

Returns model list (only if using gateway; Cloud API does not expose this).

### `POST /v1/chat/completions` with tools

```json
{
  "model": "claude-sonnet-5",
  "messages": [{"role": "user", "content": "what's 2+2?"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "calculator",
      "description": "Evaluate math expression",
      "parameters": {
        "type": "object",
        "properties": {
          "expression": {"type": "string"}
        },
        "required": ["expression"]
      }
    }
  }]
}
```

Tool calling is supported — response will include `tool_calls` array when model decides to use tools.

---

### `GET /v1/usage`

Returns account tier, daily usage, limits, and accessible models.

**Request:** No body needed — just auth headers.

```bash
curl https://api.screenpipe.com/v1/usage \
  -H "Authorization: Bearer $JWT" \
  -H "User-Agent: screenpipe-app/2.5.149"
```

**Response:**

```json
{
  "tier": "subscribed",
  "used_today": 3,
  "limit_today": 1000000,
  "remaining": 999997,
  "resets_at": "2026-08-03T00:00:00.000Z",
  "model_access": [
    "auto", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5-mini", "gpt-5-nano", "gpt-5.6", "gpt-5.6-sol",
    "gpt-5.6-terra", "gpt-5.5", "gpt-5.5-pro", "gpt-5.4",
    "gpt-5.4-pro", "claude-sonnet-5", "claude-opus-5",
    "claude-fable-5", "screenpipe-event-classifier"
  ],
  "upsell_banner": false,
  "credits_balance": 0,
  "cost_limit_reached": false,
  "upgrade_eligible": false,
  "hosted_ai": {
    "plan": "business",
    "trial": true,
    "included_credits": 600,
    "used_credits": 1,
    "remaining_credits": 599,
    "model_access": ["...same as above..."],
    "upgrade_url": null,
    "can_buy_credits": false,
    "byok_supported": true
  }
}
```

**Key fields:**

| Field | Meaning |
|-------|---------|
| `tier` | `anonymous` (no auth) / `subscribed` (logged in) |
| `used_today` | Requests made today |
| `limit_today` | Daily cap (1M for subscribed, 25 for anon) |
| `remaining` | `limit_today - used_today` |
| `resets_at` | When daily counter resets (midnight UTC) |
| `model_access` | Array of allowed model IDs |
| `hosted_ai.plan` | `business` (trial on new accounts) |
| `hosted_ai.trial` | `true` = trial period active |
| `hosted_ai.included_credits` | Total credits in plan (600 for trial) |
| `hosted_ai.used_credits` | Credits consumed |
| `hosted_ai.remaining_credits` | Credits left |

**Without auth (anonymous):**

```json
{
  "tier": "anonymous",
  "used_today": 0,
  "limit_today": 25,
  "remaining": 25,
  "model_access": ["auto"],
  "upgrade_options": {
    "login": {"benefit": "+25 daily..."}
  }
}
```

---

## Models

Source: `GET /v1/models` + brute-force testing (live, 2026-08-02). **36+ confirmed working models**. Anonymous = `auto` only.

### Full Model Table

| Model ID | Provider | Context | Speed | Intelligence | Cost Tier | Best For |
|----------|----------|---------|-------|--------------|-----------|----------|
| `auto` | screenpipe | 200K | fast | highest | **free** | general, pipes, chat |
| `gpt-5.6-sol` | openai | 1.05M | slow | highest | high | hard reasoning, agentic coding |
| `gpt-5.6-terra` | openai | 1.05M | medium | highest | medium | professional work, coding |
| `gpt-5.6-luna` | openai | 1.05M | fast | high | low | high-volume, extraction, classification |
| `gpt-5.6` | openai | 1.05M | — | — | — | base GPT-5.6 |
| `gpt-5.5` | openai | 1.05M | fast | highest | high | complex reasoning, coding |
| `gpt-5.5-pro` | openai | 1.05M | slow | highest | **very_high** | hardest coding/analysis |
| `gpt-5.4` | openai | 1.05M | medium | highest | high | professional work |
| `gpt-5.4-pro` | openai | 1.05M | slow | highest | **very_high** | hard reasoning |
| `gpt-5.4-mini` | openai | 400K | fast | high | low | coding, subagents, high-volume |
| `gpt-5.4-nano` | openai | 400K | fast | standard | low | classification, extraction, ranking |
| `gpt-5-mini` | openai | — | — | — | — | GPT-5 small |
| `gpt-5-nano` | openai | — | — | — | — | GPT-5 smallest |
| `claude-sonnet-5` | anthropic | 1M | medium | highest | high | agentic work, coding, tool use |
| `claude-opus-5` | anthropic | 1M | slow | highest | **very_high** | frontier reasoning, agentic coding |
| `claude-fable-5` | anthropic | 1M | slow | highest | **very_high** | hardest interactive work |
| `screenpipe-event-classifier` | screenpipe | — | — | — | — | internal event classification |

### Credit Weight (`query_weight`)

Each model has a `query_weight` that multiplies credit consumption:

| Model | Weight | Meaning |
|-------|--------|---------|
| `auto` | 0 | Free |
| `gpt-5.6-luna` / `gpt-5.4-mini` / `gpt-5.4-nano` | 1 | Cheapest paid |
| `gpt-5.6-terra` / `gpt-5.4` / `claude-sonnet-5` | 3 | Medium |
| `claude-opus-5` | 5 | Expensive |
| `gpt-5.6-sol` / `gpt-5.5` | 6 | Expensive |
| `claude-fable-5` | 10 | Very expensive |
| `gpt-5.5-pro` / `gpt-5.4-pro` | 36 | Most expensive |

**With 600 trial credits:**
- `auto` (weight 0) = **unlimited** (free tier)
- `gpt-5.6-luna` (weight 1) = ~600 heavy requests
- `claude-sonnet-5` (weight 3) = ~200 requests
- `claude-opus-5` (weight 5) = ~120 requests
- `gpt-5.5-pro` (weight 36) = ~16 requests

### Recommendations

| Use case | Model | Why |
|----------|-------|-----|
| Farming smoke test | `auto` | Free, no credits consumed |
| Everyday coding | `gpt-5.6-luna` or `gpt-5.4-mini` | Fast, cheap (weight 1) |
| Quality reasoning | `gpt-5.6-terra` or `claude-sonnet-5` | Balance (weight 3) |
| Maximum capability | `gpt-5.6-sol` or `claude-opus-5` | Best output, expensive |
| Stretch credits | `auto` | Weight 0, literally free |
| Hidden gems (free?) | `gemini-3.2-pro` / `deepseek-v4.5` / `kimi-k3` | Not in official list, credit cost unknown |

### Hidden Models (undocumented, confirmed working 2026-08-02)

These models are NOT listed in `/v1/models` but accept requests and return valid completions:

| Model ID | Provider | Arena Rank (Aug 2026) | Notes |
|----------|----------|----------------------|-------|
| `gemini-3.2-pro` | Google | #10 | 2M context, latest Gemini |
| `gemini-3.1-pro` | Google | #15 | Science leader |
| `gemini-3-pro` | Google | #12 | Mainline Gemini 3 |
| `gemini-3.6-flash` | Google | #15 | Fast Gemini |
| `gemini-3.5-flash` | Google | #18 | Previous flash |
| `gemini-3-flash` | Google | #25 | Base flash |
| `deepseek-v4-pro` | DeepSeek | #49 | Open-weight value leader |
| `deepseek-v4.5` | DeepSeek | #43 | Latest DeepSeek |
| `deepseek-v4-flash` | DeepSeek | #79 | Cheap/fast |
| `deepseek-v3.2` | DeepSeek | #82 | Previous gen |
| `llama-4-maverick` | Meta | — | Open-weight |
| `kimi-k3` | Moonshot | #11 | Coding #1 (arena) |
| `kimi-k3-max` | Moonshot | #11 | Max variant |
| `kimi-k2.6` | Moonshot | #41 | Previous Kimi |
| `minimax-m3` | MiniMax | #68 | Open-weight agentic |
| `glm-5` | Zhipu | #50 | Chinese frontier |
| `claude-sonnet-4-6` | Anthropic | #29 | Previous Claude Sonnet |
| `claude-sonnet-4-5` | Anthropic | #54 | Older Sonnet |
| `claude-haiku-4-5` | Anthropic | — | Fast/cheap Claude |

**Total confirmed working: 36+ models** (17 official + 19 hidden)

### Blocked Models (tested, 403/401)

| Model ID | Status | Notes |
|----------|--------|-------|
| `claude-opus-4-6` / `4-7` / `4-8` | 403 | Opus below v5 blocked |
| `qwen3.7-max` / `qwen3.6-*` / `qwen3.5-flash` | 403 | Qwen blocked |
| `grok-4.3` / `grok-4.20` / `grok-4` | 403 | Grok blocked |
| `llama-5` | 403 | Llama 5 blocked |
| `glm-5.2` / `mistral-medium-3.5` / `mistral-large-3` | 401 | Not routed |

### Health Status

`/v1/models` includes live health per model:

```json
"health": {
  "status": "healthy",       // healthy | degraded | down
  "error_rate_5m": 0,        // 0-1 (percentage)
  "requests_5m": 31          // recent traffic
}
```

### Tier Limits

```json
"tier_limits": {
  "dailyQueries": 1000000,
  "rpm": 1000,
  "freeRpm": 1000,
  "allowedModels": ["auto", "gpt-5.6-luna", ...]
}
```

**Unsupported model IDs → HTTP 400.**



---

## Authentication Flow (for consumers)

JWT token expires in ~60 seconds. Refresh before each API call:

```python
import urllib.request, json, http.cookiejar

# 1. Init Clerk client (get cookies)
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0"),
    ("Origin", "https://screenpipe.com"),
    ("Referer", "https://screenpipe.com/"),
]
opener.open("https://clerk.screenpipe.com/v1/client?_clerk_js_version=5.56.0")

# 2. Sign in
data = f"identifier={email}&strategy=password&password={password}".encode()
req = urllib.request.Request(
    "https://clerk.screenpipe.com/v1/client/sign_ins?_clerk_js_version=5.56.0",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
resp = json.loads(opener.open(req).read())
session_id = resp["response"]["created_session_id"]

# 3. Get JWT (repeat this every ~50s)
req = urllib.request.Request(
    f"https://clerk.screenpipe.com/v1/client/sessions/{session_id}/tokens?_clerk_js_version=5.56.0",
    data=b"",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
jwt = json.loads(opener.open(req).read())["jwt"]

# 4. Call API
api_req = urllib.request.Request(
    "https://api.screenpipe.com/v1/chat/completions",
    data=json.dumps({
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hello"}]
    }).encode(),
    headers={
        "Authorization": f"Bearer {jwt}",
        "User-Agent": "screenpipe-app/2.5.149",
        "Content-Type": "application/json",
    },
)
result = json.loads(urllib.request.urlopen(api_req).read())
print(result["choices"][0]["message"]["content"])
```

---

## Error Responses

| HTTP | Meaning | Common cause |
|------|---------|--------------|
| 400 | Bad request | Invalid model ID |
| 401 | Unauthorized | JWT expired (>60s) or missing |
| 403 | Forbidden | Wrong/missing `User-Agent` header (Cloudflare) |
| 429 | Rate limited | Too many requests |
| 500 | Server error | Upstream ScreenPipe issue |

---

## Rate Limits

| Resource | Limit | Notes |
|----------|-------|-------|
| Clerk sign-in attempts | 100 / 60 min | Per user lockout |
| Clerk sign-up | Unknown | No CAPTCHA, public |
| API completions | Unknown | Free tier, likely token-based |
| JWT refresh | Unlimited | Within session lifetime (7 days) |

---

## Comparison with Direct OpenAI/Anthropic

| Feature | ScreenPipe Cloud | Direct API |
|---------|-----------------|------------|
| Cost | Free (account creation) | Pay per token |
| Auth | Clerk JWT (60s, auto-refresh) | Static API key |
| Models | 5-7 top models | Provider-specific |
| Tool calling | ✅ | ✅ |
| Streaming | ✅ (SSE) | ✅ |
| Context | Up to 1M tokens | Varies |
| User-Agent required | ✅ (`screenpipe-app/2.5.149`) | ❌ |
| Rate limit | Unknown (free tier) | Pay-as-you-go |

---

## curl Examples

### Basic chat

```bash
curl -X POST https://api.screenpipe.com/v1/chat/completions \
  -H "Authorization: Bearer $JWT" \
  -H "User-Agent: screenpipe-app/2.5.149" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hello"}]}'
```

### Streaming

```bash
curl -N -X POST https://api.screenpipe.com/v1/chat/completions \
  -H "Authorization: Bearer $JWT" \
  -H "User-Agent: screenpipe-app/2.5.149" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-5","messages":[{"role":"user","content":"explain quantum computing"}],"stream":true}'
```

### With system prompt

```bash
curl -X POST https://api.screenpipe.com/v1/chat/completions \
  -H "Authorization: Bearer $JWT" \
  -H "User-Agent: screenpipe-app/2.5.149" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6-sol","messages":[{"role":"system","content":"You are a Python expert."},{"role":"user","content":"write a fibonacci function"}]}'
```
