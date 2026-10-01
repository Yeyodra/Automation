# NvRouter `enter-converge` — Consolidated Reference

Merge dari 4 evidence pack (`.omo/reports/p*-enter-*.md`, 2026-09-20),
divalidasi silang lawan source + git history. Modul NvRouter:
`github.com/mydisha/keirouter/backend`. Tidak ada package `backend/api` —
HTTP API ada di package `gateway`.

Koreksi dari merge: migrasi 0033 = **24 baris alias** (bukan 27).

## 1. Identitas & registrasi

- ID `enter-converge`, alias `ec`, `DialectOpenAI`, `AuthKind api_key`,
  `BaseURL https://api.enter.pro/code/api/v1` (`connectors/catalog.go:230`).
- `DefaultRegistry` (`registry.go:85-89`): konektor dedicated
  `NewEnterConverge(base)` + `RegisterLiveModelSource` + `RegisterQuotaSource`
  — satu-satunya provider dialek-OpenAI yang mendaftarkan keduanya.
- Struct `EnterConverge{base, codec, antCodec, workspaces cache+mutex,
  signatures}` (`enter_converge.go:72-79`, 709 baris).

## 2. Auth & headers

`headers()` (`:94-106`): `Authorization: Bearer ek_`, `Origin` +
`Referer https://enter.converge.ai`, UA Chrome/131, `Accept:
application/json`, `X-Workspace-ID` bila ada, lalu merge `creds.Headers`.
`messageHeaders()` (`:113-127`): strip header caller, cuma `accept` yang
boleh lewat, paksa `anthropic-version: 2023-06-01` (konstanta di
`connectors/anthropic.go:17`) + `x-workspace-id` lowercase.
Live: header `x-api-key` → 401 (observasi, **tanpa pin code/test**).

## 3. Workspace resolve

Prioritas: `Extra["workspaceId"]` (camel, menang) → `Extra["workspace_id"]`
→ wajib prefix `ek_` → cache 30 mnt per key → `GET {base}/workspaces`
(tanpa header workspace) → entry pertama, id numerik-atau-string
(`parseEnterWorkspaceID` `:136-156`). Envelope: `data.workspaces[{id}]`;
id wire **numerik** (mis. `10000510046`), bukan UUID `public_id`.
Dipakai: prepare, Chat/Stream dua dialek, Validate, ListModels, FetchQuota.

## 4. Trinitas ID (jangan ketuker)

- **bare** (client): `gpt-5.6-sol` — tabel kurasi `models.go:55-66` (29 ID).
- **wire** (upstream): `openai/gpt-5.6-sol` — via `EnterNativeModelID`
  (`:43-48`), unknown passthrough. Tabel penuh `:28-39`.
- **canonical reverse**: `EnterCanonicalModelID` (`:59-64`) — dipakai
  `gateway/resolve.go:109-111` + import `modelLock_` + migrasi 0033
  (**24 baris**, cuma opus-4.6/sonnet-4.5 untuk claude — model claude baru
  tidak punya baris migrasi).

## 5. Jalur OpenAI

`prepare()` (`:254-284`): workspace dulu → clone (nil reasoning/temp,
strip 11 key sampling, deep-copy messages) → native id → flag stream →
prefetch images → `RenderRequestForProvider` → strip ulang → `gpt-*`:
`max_tokens`→`max_completion_tokens` (match pada **requested model**).
`Chat` (`:366`): `doJSON POST chat/completions` + `ParseResponse`;
`openStream` (`:426`) + `scanOpenAISSE`; `StreamRaw` openai-only.
**Retry cuma 502**, ≤3x, backoff 100ms×attempt. `enterError` (`:343-353`):
402 → quota-exhausted scope-model, `RetryAfter` ~10 tahun.
Helper: `httpclient.go` — `doJSON:161`, `doJSONMethod:258`,
`openStreamWithClient:432`, `scanOpenAISSE:541`, `sseScanner:601`,
`bearer:830`, `mergeHeaders:889`, `joinURL:901`.

## 6. Jalur Anthropic

`enterUsesMessages` (`:108-111`): native id prefix `anthropic/claude-`.
`DialectForRequest` (`:87-92`) → `core.ResolveRequestDialect`
(`core/connector.go:58-63`). `POST {base}/messages`; non-stream
`antCodec.ParseResponse`, stream `scanEnterAnthropicSSE` (`:446-547`):
skip `{}`/ping (emit `ChunkPing`), wajib `message_stop`, `nexus_usage`
di-skip, konten-setelah-stop = integrity error, finish ditahan sampai akhir,
usage terakumulasi. Thinking-signature: `remember` saat response/stream,
`lookup` + strip saat clone kecuali provider+model cocok
(`signature_provenance.go:24-67`; clone `:200-225`). Native server tools
ditolak pre-dispatch (`anthropic.go:36-43`, scope-request).
Capability (`capability/tables.go:66-74`): 7 model claude, semua
`{Vision, Reasoning, NoSearch, MaxOutput 64000}`, budget vs adaptive.
**Direct-stream dikecualikan** (`pipeline.go:42`) — satu konektor dua
dialek per-model, ditambah `StreamRaw` openai-only.

**Koreksi 2026-10-01 (live, `ek_` key):** routing di atas adalah *pilihan*
NvRouter, bukan batasan upstream. Server Enter **menerima claude di dua
dialect**: `anthropic/claude-sonnet-5.5` via `/chat/completions` → 200 (balas
`chatcmpl-`), dan via `/messages` → 200 (balas `msg_`). `anthropic-version`
opsional. Jadi `enterUsesMessages` tetap benar sebagai kebijakan, tapi
**bukan karena upstream menolak** dialect OpenAI untuk claude.
Sebaliknya, `protocol` di `ai-capability/models` memang **memaksa** jalur:
`google/gemini-3.1-pro-preview` (protocol=`anthropic_messages`) →
`/chat/completions` = **502 Bad gateway**.

## 7. Models / quota / validate

- `ListModels`: `GET ai-capability/models` + walk rekursif cocokkan
  allowlist native. **Gap: 5 model live baru ter-drop** (allowlist dari
  tabel kurasi) — discovery takkan menemukan `gpt-6-astra` dkk sampai
  map+tabel diupdate.
- `Validate`: endpoint sama, 2xx = valid.
- `FetchQuota`: `credits/dashboard` (fallback `credits`) →
  `credits_balance.total`; plan dari `subscription/status`; kosong →
  pesan "connected, but empty" (bukan error).

## 8. Akun & admin

- Create/bulk/validate: key wajib `ek_`; `workspace_id` kosong →
  auto-resolve + re-seal vault (`admin.go:724,792-803,919,1083,1143`).
- `resolveEnterWorkspace` (`:3273`): type-assert konektor →
  `ResolveWorkspace`. `validateAccountCredentials` (`:3302`): probe 15 dtk.
- Import: `{provider, authType:apiKey, apiKey:ek_...,
  providerSpecificData:{workspaceId|workspace_id}, modelLock_<wire>:ts}`
  — dua ejaan workspace diterima, tersimpan snake; export selalu camel +
  emit `modelLock_<wire>` (`admin_foreign_import.go:407-450`,
  `admin_foreign_export.go:20-52`).
- Dispatch: `Target{Provider,Model}`; 402 cooldown **cuma model itu**,
  akun tetap jalan (`dispatch_test.go:268`).
- Generic: `toolsanitize` (rakit tool-call streaming), `normalizer`
  (relevansi enter: nol), SSRF (`httputil/ssrf.go`, dipakai fetch image
  + gate base_url admin). Vault: `APIKey=ek_`, `Metadata={workspace_id}`.

## 9. Sejarah evolusi (8 commit, by git)

| Tanggal | Commit | Isi |
|---|---|---|
| 04 Agu 26 | `ecd7754` | v1 OpenAI-only, 447 baris, 16 file — TANPA map |
| 05 Agu 26 | `ac60ba7` | SELURUH map native↔canonical + hook resolve |
| 08 Agu 26 | `f198219` | Claude via messages (+152), tanpa signature |
| 08 Agu 26 | `43e7807`/`7d9817e` | `nexus_usage`, oversized→integrity |
| 08 Agu 26 | `c71f884` | tweak test 3 baris |
| 09 Agu 26 | `a60be08` | signature + DialectForRequest + capability (platform, 33 file) |
| 09 Agu 26 | `1155ba8`/`b28ad21` | ping keepalive, empty metadata (terakhir) |

Pola: maturitas = pengerasan SSE/stream, bukan rewrite. Mapping lahir
belakangan sebagai layer terpisah; signature urusan platform.

## 10. Test map

`enter_converge_test.go` (538): httptest + `ek_test`/`workspace_id:ws`
— provenance, tool-reject, dialect, headers-strip, canonical-id,
transport/stream SSE (ping/nexus/oversized/stop/content/finish),
workspace-cache, retry-502, fragmented-tools, 402-scope, quota-envelope,
image-proxy/MIME/SSRF. Admin: `admin_foreign_import_enter_test.go`
(validate/create/lock valid-kanonik/malformed/export).
Capability/pipeline/dispatch/resolve: kasus enter di
`capability_test:163`, `strip_test:36`, `pipeline_test:14,53`,
`dispatch_test:268`, `resolve_targets_test:55`, `models_test:318`,
`catalog_test:19`.

## 11. Open questions

1. Pin `x-api-key → 401` (observasi live, tanpa pin code).
2. Baris migrasi untuk claude baru (opus-5/sonnet-5/opus-4.8/4.7/sonnet-4.6).
3. Envelope workspaces tanpa `code` vs quota dengan `code:0` — toleransi?
4. Bulk import tanpa workspace auto-resolve — disengaja?
5. `nexus_usage` untuk metering (sekarang di-skip)?

Sumber mentah: `.omo/reports/p1-enter-core-*.md`,
`p2-enter-openai-*.md`, `p3-enter-anthropic-*.md`,
`p4-enter-admin-platform-*.md`.
