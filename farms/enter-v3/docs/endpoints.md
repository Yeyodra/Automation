# enter-v3 — Endpoint Reference

Basis: `https://api.enter.pro` + app `https://enter.converge.ai`.
Status: ✅ = dipanggil live & 200 (2026-09-20), ◐ = dari kode NvRouter
(`enter_converge.go`), belum di-hit langsung dari farm.
🆕 = live-verified 2026-10-01 dengan `ek_` key hasil farm `enter-v3-google`.

## Auth & header

| Kredensial | Pakai di | Header |
|---|---|---|
| JWT gateway (`/auth/session?include=access_token`) | post-auth chain | `Authorization: Bearer <jwt>` |
| `ek_...` API key | models, chat, workspaces | `Authorization: Bearer <ek_...>` (`x-api-key` → 401) |

Header wajib untuk `ek_`: `X-Workspace-ID: <id numerik>`, `Origin:
https://enter.converge.ai`, `Referer: https://enter.converge.ai/`,
`Accept: application/json`. Tanpa `X-Workspace-ID` → 400
`invalid_request_error`. Semua response envelope `{"code":0,...}`.

Workspace id = `data.workspaces[0].id` (**numerik**, mis. `10000510046`),
bukan `public_id` UUID.

## ✅ GET workspaces/{id}/models — list all models

```http
GET /code/api/v1/workspaces/{id}/models
```

`data: {current_plan_type, models:[{id,name,description,speed,
intelligence,cost,is_default,is_newest,access_state}]}`.

🆕 Live 2026-10-01 (plan **free**): **15 models** —
`auto`, `claude-opus-5.5`, `claude-sonnet-5.5`, `gpt-6-astra`, `gpt-6.1-sol`,
`gpt-6-luna`, `gemini-3.8-flash`, `kimi-k3`, `glm-5.3`, `glm-5.3-flash`,
`qwen-3.8-max`, `qwen-3.7-plus`, `minimax-m3`, `deepseek-v4.1-flash`,
`deepseek-v4-pro`.

**⚠️ Daftar ini BUKAN katalog penuh — jangan pakai buat enumerasi.** Banyak
model hidup hanya muncul di `ai-capability/models` (di bawah). Model yang
ter-delist dari sini sering **masih bisa dipanggil** (lihat bagian wire ID).

Field `cost` di list ini **menyesatkan** — jangan pakai buat estimasi billing:
`gpt-6-astra` dan `gpt-6.1-sol` dua-duanya ber-label `"High"`, padahal terukur
astra **~5x lebih mahal** dari sol. Pakai rate terukur di bawah.

## ✅ GET /code/api/v1/ai-capability/models — katalog PENUH (58 model) 🆕

```http
GET /code/api/v1/ai-capability/models
```

**Ini sumber otoritatif daftar model** — 58 entri (vs 15 di list workspace),
termasuk model yang sudah delist dari `/models`. Tiap entri:
`{protocol, id, name, description, type, price_tier, speed, logo}`.

`protocol` menentukan endpoint + dialect yang dipakai:

| `protocol` | Jumlah | Endpoint |
|---|---|---|
| `openai_chat_completions` | 20 | `POST /code/api/v1/chat/completions` |
| `openai_responses` | 9 | `POST /code/api/v1/chat/completions` |
| `anthropic_messages` | 11 | `POST /code/api/v1/messages` (+ `anthropic-version`) |
| `google_gemini_generate_content` | 3 | `POST /code/api/v1/chat/completions` |
| `typesafe_decisions` | 1 | (khusus) |
| `null` (Image/Video/Music) | 14 | non-LLM |

**⚠️ `protocol` menentukan cara hit.** Contoh nyata: `google/gemini-3.1-flash-lite-preview`
dan `google/gemini-3.1-pro-preview` ber-protocol **`anthropic_messages`** (bukan
`google_gemini_generate_content`), jadi harus lewat `/messages`, bukan `/chat/completions`.

Model LLM (58 entri, dikelompokkan):

```
openai_responses : gpt-6.1-sol, gpt-6-sol, gpt-6-astra, gpt-6-luna,
                   gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna,
                   gpt-5.5, gpt-5.4
openai_chat      : gpt-5.4-pro, gpt-5.2-pro, deepseek-v4-pro,
                   deepseek-v4-flash, deepseek-v4-flash-vision-exp,
                   qwen-3.8-max, qwen-3.7-plus, qwen-3.7-max, qwen-3.6-plus,
                   qwen-3.6-max-preview, kimi-k3, kimi-k2.7-code, kimi-k2.6,
                   kimi-k2.5, glm-5.2, glm-5.1, glm-5, minimax-m3,
                   minimax-m2.7, minimax-m2.5
anthropic_messages: claude-opus-5.5, claude-opus-5, claude-opus-4.8/4.7/4.6,
                   claude-sonnet-5.5, claude-sonnet-5, claude-sonnet-4.6/4.5,
                   gemini-3.1-flash-lite-preview, gemini-3.1-pro-preview
gemini_generate  : gemini-3.8-flash, gemini-3.6-flash, gemini-3.5-flash
non-LLM          : kling-3.0, seedance-2/1.0-pro, seedream-5/4.5,
                   wan-2.7, happyhorse-1.0, gpt-image-2, suno-v5,
                   mureka-v8, nano-banana (gemini-3-pro-image-preview,
                   gemini-3.1-flash-image-preview), veo-3.1, sora-2
```

## ❌ "claude-fable" — TIDAK ADA (dicek 2026-10-01) 🆕

`fable` **tidak ada** di 58 entri `ai-capability/models`, dan tidak ada di
bundle JS app maupun HAR mana pun. 27 varian nama diprobe
(`claude-fable`, `anthropic/claude-fable`, `claude-fable-{1,2,3,3.5,4,4.5,5,5.5,6}`,
`claude-5-fable`, `fable-5`, …) → **semua 400 `unsupported model`**.

Jangan pakai nama ini. Claude yang beneran ada = `claude-opus-5.5` &
`claude-sonnet-5.5` (paling baru), turun sampai `claude-sonnet-4.5`.

## ✅ POST /code/api/v1/chat/completions — hit model

```http
POST /code/api/v1/chat/completions
{
  "model": "openai/gpt-6-astra",
  "messages": [{"role": "user", "content": "..."}],
  "max_completion_tokens": 64,
  "stream": false
}
```

Aturan (terverifikasi live 2026-10-01 untuk `gpt-6-astra`, `gpt-6.1-sol`,
`gpt-5.6-sol`):
- **wire ID WAJIB prefix vendor**: `gpt-6-astra` → 400 `unsupported model`;
  `openai/gpt-6-astra` → 200. Berlaku untuk semua vendor (`anthropic/`,
  `openai/`, `google/`, `alibaba/`, …).
- **Model yang sudah delist dari `/models` tetap bisa dipakai** kalau prefix
  bener: `gpt-5.6-sol` (tidak di list workspace) → `openai/gpt-5.6-sol` = 200.
  Cek ketersediaan lewat `ai-capability/models`, bukan `/models`.
- **Endpoint ditentukan `protocol`**, bukan asumsi vendor. `protocol` di
  `ai-capability/models` = endpoint utama yang di-route:
  `openai_responses` / `openai_chat_completions` → `/chat/completions`;
  `anthropic_messages` → `/messages`.
  **Bukti 2026-10-01:** `google/gemini-3.1-pro-preview`
  (protocol=`anthropic_messages`) → `/chat/completions` = **502 Bad gateway**,
  sedangkan `/messages` = 200.
- **Claude BISA dipanggil lewat DUA-DUANYA** (diverifikasi 2026-10-01):
  `anthropic/claude-sonnet-5.5` via `/chat/completions` → **200** (dialect
  OpenAI, balas `chatcmpl-...`), dan via `/messages` → **200** (dialect
  Anthropic, balas `msg_...`). Jadi claude **tidak** terkunci ke Anthropic saja.
  `anthropic-version` **tidak wajib** di `/messages` (200 dengan & tanpa).
  Pakai `protocol` sebagai penunjuk jalur yang "resmi", bukan satu-satunya.
- model `gpt-*`: `max_tokens` → `max_completion_tokens`; strip
  `temperature/top_p/top_k/min_p/typical_p/repetition_penalty/
  frequency_penalty/presence_penalty/reasoning_effort/thinking/reasoning`.
- Live: balas `ASTRA_OK`, TTFT ~1.4s, `usage.completion_tokens=6`.

## ✅ POST /code/api/v1/messages — jalur Anthropic (Claude) 🆕

```http
POST /code/api/v1/messages
anthropic-version: 2023-06-01
{
  "model": "anthropic/claude-sonnet-5.5",
  "max_tokens": 64,
  "messages": [{"role": "user", "content": "..."}]
}
```

Live 2026-10-01 → 200 (2.6s). Response balikin `model` internal, mis.
`MaaS_Cl_Sonnet_5.5_20260928_ULT` / `MaaS_Cl_Opus_5.5_20260922_ULT` — berguna
buat mastiin varian mana yang beneran melayani. `claude-sonnet-5.5` (bare,
tanpa prefix) **juga 200** di jalur ini; tapi tetap pakai prefix biar konsisten.

Header `anthropic-version` **opsional** (200 dengan maupun tanpa). Ini jalur
"resmi" untuk claude (sesuai `protocol`), tapi claude juga jalan di
`/chat/completions` — lihat catatan di atas.

## ✅ Credits — GET workspaces/{id}/credits/dashboard 🆕

```http
GET /code/api/v1/workspaces/{id}/credits/dashboard
```

Live 2026-10-01 → 200. `data.credits_balance = {total, breakdown:{monthly,
purchased,bonus}, status, low_credits_threshold}`. Akun farm baru =
**200 credit, semuanya `bonus`** (one-time, bukan bulanan).

- Alias `GET .../credits` → 200, bentuk sama (ada `data.credits` juga).
- `GET .../credits/balance` → **404** `project not found` (jangan pakai).
- `GET .../subscription/status` → 200; `data.status.entitlement` berisi
  `daily_credits: 100`, `monthly_build_credits: 0`, `monthly_ai_credits: 0`,
  `ai_all_trial_limit: 1`, dan daftar `ai_all_models`.

### Rate credit terukur (live 2026-10-01)

Credit **token-based** (bukan per-request). Diukur dari selisih
`credits_balance.total` sebelum/sesudah call:

| Model | Rate | 200 credit ≈ | 1M token butuh |
|---|---|---|---|
| `gpt-6.1-sol` | ~0.93k credit / 1M tok (blended) | ~215k token | ~5 akun |
| `gpt-6-astra` | ~4.9k credit / 1M **output** tok | ~40k token (out) | ~23 akun |
| `gpt-5.6-sol` (delisted) | ~5.7k credit / 1M **output** tok | ~35k token (out) | ~28 akun |

Contoh terukur: astra 39 in + 391 out = **1.99 credit**; sol 39 in + 443 out =
**0.45 credit**; 5.6-sol 16 in + 780 out = **4.44 credit**.

**⚠️ 200 credit itu puluhan–ratusan ribu token (k), BUKAN jutaan (M).** Untuk
1M token butuh ~5 akun (sol) sampai ~23 akun (astra).

Catatan: `gpt-6-astra` ber-`access_state.min_plan_type: "basic"` + badge
`BASIC`, tapi **HTTP 200 di plan free** — entah grace/trial atau badge-nya
tidak di-enforce. Jangan dijadiin tulang punggung.

## ✅ Post-auth chain (JWT Bearer)

Urutan persis HAR sukses:

```
POST /code/api/v1/referral/claim?code=<gift>   body {} -> 200 data:null (nonfatal)
GET  /code/api/v1/users/info                   -> must_verify_email=false; merge_action=candidate_pending + candidate + tanpa block = fatal
GET  /code/api/v1/workspaces                   -> data.workspaces[] (fail-closed bila kosong)
GET  /code/api/v1/onboarding/config            -> {completed, flow_version: v2|v3}
POST /code/api/v1/onboarding/complete          {"role":"founder","team_size":"just_me","build_intent":"other"}
                                               -> data.completed=true (skip bila config.completed)
POST /code/api/v1/workspaces/{id}/api-keys    {"name":"farm","scope":"all","reveal_policy":"create_only"}
                                               -> data.{id,key,...} (key hanya reveal saat create)
GET  /code/api/v1/referral/rewards             -> {invitee_reward,inviter_reward} (best-effort)
```

## ◐ Endpoint lain (kontrak NvRouter)

- ✅ `GET ai-capability/models` — **live 2026-10-01**, katalog penuh 58 model
  (lihat di atas). Ini cara validasi kredensial + enumerasi model.
- ✅ `GET workspaces/{id}/credits/dashboard` / `.../credits` /
  `.../subscription/status` — live 2026-10-01 (lihat bagian Credits).
- Signature error: 402 → quota exhausted; 502 → retry ≤3x (100ms×attempt).

## Mapping bare ID → wire ID

Aturan: **bare ID selalu 400, wire ID = `<vendor>/<bare>`.** Vendor ditentukan
`protocol` di `ai-capability/models`, bukan tebakan.

| Vendor prefix | Model |
|---|---|
| `openai/` | `gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-astra`, `gpt-6-luna`, `gpt-5.6-sol/terra/luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-pro`, `gpt-5.2-pro`, `gpt-image-2`, `sora-2` |
| `anthropic/` | `claude-opus-5.5/5/4.8/4.7/4.6`, `claude-sonnet-5.5/5/4.6/4.5` (+ `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite-preview` — ber-protocol anthropic!) |
| `google/` | `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3-pro-image-preview`, `gemini-3.1-flash-image-preview`, `veo-3.1` |
| `alibaba/` | `qwen-3.8-max`, `qwen-3.7-plus/max`, `qwen-3.6-plus/max-preview`, `wan-2.7`, `happyhorse-1.0` |
| `deepseek/` | `deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-v4-flash-vision-exp` |
| `moonshotai/` | `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `kimi-k2.5` |
| `z-ai/` | `glm-5.3`, `glm-5.3-flash`, `glm-5.2`, `glm-5.1`, `glm-5` |
| `minimax/` | `minimax-m3`, `minimax-m2.7`, `minimax-m2.5` |
| `doubao/` | `seedream-5`, `seedream-4.5`, `seedance-2`, `seedance-1.0-pro` |
| `kuaishou/` | `kling-3.0` |
| `suno/` | `suno-v5` |
| `mureka/` | `mureka-v8` |
| `typesafe/` | `jev-1.13` |
| (tanpa prefix) | `auto` |

⚠️ `auto` adalah satu-satunya bare ID yang valid tanpa prefix.

## Discovery & capability gap (temuan report P2/P4, 2026-09-20)

- `ListModels` NvRouter walk + cocokkan allowlist kurasi → model live
  ter-drop dari discovery. **Fix 2026-10-01: ambil daftar dari
  `ai-capability/models` (58 entri), bukan dari `/models` (15 entri).**
  `/models` sudah tidak layak jadi sumber allowlist.
- Capability claude di enter, semua `{Vision, Reasoning, NoSearch,
  MaxOutput 64000}`; `claude-budget` (opus-5/sonnet-5/sonnet-4.5) vs
  `claude-adaptive` (opus-4.8/4.7/4.6, sonnet-4.6). **Baru 2026-10-01:**
  `claude-opus-5.5` + `claude-sonnet-5.5` belum ada di mapping mana pun.
- Migrasi 0033 cuma 24 baris — claude baru tidak punya baris merge cooldown.

Detail konsolidasi: `nvrouter-enter.md`.

## Error signature

| Sinyal | Arti |
|---|---|
| 401 | auth salah (cek Bearer `ek_` vs header lain) |
| 400 `X-Workspace-ID header is required` | tambah header workspace |
| 400 `unsupported model` | pakai wire ID ber-prefix `<vendor>/<bare>`; atau model memang tidak ada (mis. `claude-fable`) |
| 404 `project not found` | endpoint salah (mis. `credits/balance`) |
| Auth0 password-step 400 `custom-script-error-code_extensibility_error` + "email domain is not allowed" | domain email diblokir permanen |
| callback 403 `access_denied` + "The user denied the authorization request" | Google OAuth ditolak (akun/consent), bukan bug farm |
