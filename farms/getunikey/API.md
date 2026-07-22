# GetUniKey — API & product reference

Reverse-engineered from HAR (2026-07-22) + live probes with farmed keys.  
Base host: **`https://www.getunikey.ai`**

Stack: New-API / one-api style (session cookie + `New-Api-User` for web; Bearer key for `/v1`).

---

## 1. Two auth surfaces

| Surface | Auth | Used for |
|---------|------|----------|
| **Web / console** | `Cookie: session=…` + header **`New-Api-User: <user_id>`** | Signup OAuth, create token, playground chat, drawing, video |
| **OpenAI-compatible API** | **`Authorization: Bearer <api_key>`** | `/v1/models`, `/v1/chat/completions`, billing usage |

Farm produces the **API key**. 9router provider uses **Bearer only**.

---

## 2. Account register (Google OAuth)

No email/password register in captures — **Google OAuth only**.

### Flow

```
GET  /sign-up?aff=<CODE>                 # referral landing (optional aff)
GET  /api/status                         # flags: google_oauth, register_enabled, …
GET  /api/user/logout                    # clear prior session
GET  /api/oauth/state?aff=<CODE>         # → state string + Set-Cookie session (short)
→ browser Google OAuth
GET  /oauth/google?code=…&state=…        # SPA shell
GET  /api/oauth/google?code=…&state=…    # ← actual login/register
     Set-Cookie: session=<long auth ~400+ chars>
     body: { id, username: "google_N", display_name, group, role, status }
GET  /api/user/self                      # profile + quota (needs New-Api-User after id known)
```

### Google authorize (if building URL manually)

```
https://accounts.google.com/o/oauth2/v2/auth
  ?client_id=190146626926-416bbh8g0ft25u7rll5a82k2plk4atel.apps.googleusercontent.com
  &redirect_uri=https://www.getunikey.ai/oauth/google
  &response_type=code
  &scope=openid profile email
  &state=<from /api/oauth/state>
```

### Session cookie lengths (important)

| Phase | Approx `session` length |
|-------|-------------------------|
| Pre-OAuth / anon | ~150–230 |
| Post-OAuth (auth) | **≥ ~350–500** |

Do **not** treat short pre-login cookies as logged-in.

### Fresh account quota (with aff, e.g. `bTOY`)

From farm + HAR:

| Field | Fresh value |
|-------|-------------|
| `quota` | **1_500_000** |
| `gift_quota` | **1_500_000** |
| `used_quota` | **0** |
| `request_count` | **0** |

Without aff (older capture): gift/quota **500_000**.

---

## 3. Create API key (web session)

```http
POST /api/token/
Cookie: session=…
New-Api-User: <user_id>
Content-Type: application/json

{
  "name": "prod",
  "remain_quota": 0,
  "expired_time": -1,
  "unlimited_quota": true,
  "model_limits_enabled": false,
  "model_limits": "",
  "allow_ips": "",
  "group": "",
  "cross_group_retry": false
}
```

Response: `{ "success": true }` — **no raw key, no id**.

```http
GET /api/token/?p=1&size=20
```

→ `data.items[].id`, masked `key`.

```http
POST /api/token/{id}/key
```

```json
{ "success": true, "data": { "key": "<full_api_key>" } }
```

Key string is opaque (~48 chars in farm samples). Use as Bearer value **as-is** (no forced `sk-` prefix required by gateway).

---

## 4. OpenAI-compatible API (for 9router)

### Base URL

```text
https://www.getunikey.ai/v1
```

### Chat (primary)

```http
POST /v1/chat/completions
Authorization: Bearer <api_key>
Content-Type: application/json

{
  "model": "gpt-5.6-sol",
  "messages": [{ "role": "user", "content": "Hello!" }],
  "stream": false,
  "max_tokens": 32
}
```

Also supported (docs UI): `POST /v1/responses` with `{ "model", "input", "stream" }`.

### Models

```http
GET /v1/models
Authorization: Bearer <api_key>
```

~**31** models (snapshot 2026-07-22). See [MODELS.md](./MODELS.md).

### Usage / billing (Bearer)

| Endpoint | Notes |
|----------|--------|
| `GET /v1/dashboard/billing/usage` | `{ "total_usage": <number> }` |
| `GET /dashboard/billing/usage` | same |
| `GET /v1/dashboard/billing/subscription` | soft/hard limit USD fields |
| `GET /api/usage/token` | per-token `total_used` / `total_granted` / `total_available` |

`GET /api/user/self` with Bearer → **Unauthorized** (session only).

---

## 5. Web playground / media (session only — not 9router chat)

From HAR `21-55` (logged-in user):

### Playground chat

```http
POST /pg/chat/completions
Cookie + New-Api-User
{ "model", "group": "unikey", "messages", "stream": true, … }
```

SSE chunks. **Different path** from `/v1/chat/completions`.

### Image

```http
POST /api/drawing/generate
multipart: prompt, model (e.g. "Nano Banana"), aspect_ratio, resolution [, ref_image_url]
```

Poll: `GET /api/drawing/generations`, `GET /api/drawing/generations/{id}`.

### Video

```http
POST /api/video/generate
multipart: prompt, model (e.g. bytedance/seedance-2.0-fast), aspect_ratio, duration
```

Results on OSS: `unikeys.oss-cn-hongkong.aliyuncs.com/...`

---

## 6. Vision (chat multimodal)

Works on many **text** models via OpenAI vision content parts:

```json
"content": [
  { "type": "text", "text": "Describe this image…" },
  { "type": "image_url", "image_url": { "url": "data:image/png;base64,…" } }
]
```

HTTPS image URLs also work on the same models.  
Models named `*-image*`, `seedance`, `kling` are **not** chat-vision endpoints (404 / gen-only).

Proven vision (local base64 smoke): GPT-5.6*, Claude Opus 4.6–4.8, Gemini flash/lite, Grok 4.3, Qwen 3.6-plus, etc.  
See conversation probes / [MODELS.md](./MODELS.md).

---

## 7. Related docs

- [README.md](./README.md) — farm hub usage  
- [9ROUTER.md](./9ROUTER.md) — **build OpenAI Compatible provider in 9router**  
- [MODELS.md](./MODELS.md) — catalog + chat/vision matrix  
