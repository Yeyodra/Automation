# enter-v3 — Handoff Develop (2026-09-20)

Kronologi pembangunan fresh path `farms/enter-v3/`. Legacy `farms/enter/`
tidak pernah diubah (di-load via importlib by path, nama modul privat).

## 1. Reverse HAR (3 file)

| HAR | Hasil |
|---|---|
| `newversion.har` | flow tanpa referral; password POST → session **~8 detik** |
| `suksesreferal.har` | referral penuh: `claim?code=` 200, post-auth v3 urut, `ek_` key; password POST → session **~7 detik** |
| `domainnotallow.har` | domain-block = Auth0 password-step 400 `custom-script-error-code_extensibility_error`, teks "This email domain is not allowed" |

Kontrak terkunci: `onboarding/complete` = 3 field `{role, team_size,
build_intent}`; `flow_version` live = `v3`; claim body `{}`/`null` → 200
`data:null`; api-keys pakai workspace id **numerik** (bukan `public_id` UUID);
semua response envelope `{"code":0,...}`.

## 2. Scaffold fresh path

- `postauth_v3.py` — HTTP murni, standalone (tanpa import legacy).
- `farm.py` — CLI hub (`-n -c -y`), worker loop, `do_signup_v3` milik sendiri
  (legacy `do_signup_and_oauth` diabaikan: race 13s + klik "Continue" nyasar ke Google).
- `enowx_mail.py` — adapter mailbox enowX (mint + poll OTP dari `<code>`).
- Registrasi `enter-v3` di `jobs/registry.py`; `README.md`, `.env.example`.

## 3. Canary runs & bug per run

- **canary8** — stuck setelah Turnstile `Success!` (token len 688). Reverse HTML
  (entry signup/identifier 83KB): **dua tombol** `value="default"` — honeypot
  (`opacity:0`, `pointer-events:none`, `aria-hidden`, `tabindex=-1`) + tombol
  asli (`data-action-button-primary`, `_button-signup-id`). Selector lama match
  keduanya; Playwright `is_visible()=True` untuk opacity:0 → klik hantu 30s.
  Fix: selector primary-only + filter computed-style + `requestSubmit` +
  verifikasi navigasi (`_primary_submit`). canary9: `submit ok via click`.
- **canary9** — email + OTP lolos, lalu "log mati". Browser ternyata sudah di
  `/workspace`. Akar: legacy `_is_enter_app_url` cuma match path `/` persis →
  wait 60s+20s buta lalu navigasi balik sia-sia. Fix: `_on_app()` (semua path
  app host) + heartbeat tiap 10s di semua wait sunyi.
- **OTP tidak datang** — `supplementwiki.org` submit OK tapi nihil 6+ menit;
  `novacontigencia.xyz` OTP 4–7 detik, `myamya.tech` mail masuk. Bukan bug
  farm — routing inbound per-domain. Fix: `ENTER_ENOWX_DOMAIN` pin di farm
  `.env`.
- **canary12** — browser full lolos (gateway session established) lalu
  `TypeError: post_auth_setup() takes 1-2 args but 3 given` — `proxy`
  keyword-only di-pass positional. Fix satu baris.
- **canary13** — **`ok=1/1` pertama** (`ek_e103...`, 191s). Gap OTP→password
  142 detik tanpa log.
- **Bedah gap 142s** — password page **tidak ada widget Turnstile**
  (risk-based, kadang muncul kadang tidak). Optimasi yang salah (tunggu token
  150s) bikin kasus no-widget makin parah. Fix: `_wait_password_ready` —
  gate token hanya bila mount ada, else submit langsung setelah grace.
- **canary15** — sukses, tapi 128 detik sunyi lagi + browser "di /workspace".
  Bedah: window depan = **orphan run lama** (close gagal diam-diam saat
  abort); strategi pointer gagal diam-diam (overlay/animasi); satu-satunya
  yang lolos `keyboard Enter` (implicit submission, tanpa hit-test) 3/3 run.
  Fix: order Enter-first; exception per round di-log (1 baris/round);
  timeout eksplisit di evaluate; app-guard (sudah navigasi → langsung True);
  gate handler legacy pada mount (klik koordinat butanya pernah submit form
  diam-diam); close browser eksplisit + log.
- **canary16/17** — **`ok=1/1`, 205s → 82s**. Fase password 128s → 7s.
  Semua submit `via enter`. `browser closed` ke-log.

## 4. Verifikasi API key live

- `GET workspaces/{id}/models` (Bearer `ek_`) → 200, **18 models**.
  Header `x-api-key` → 401 (wajib Bearer).
- `POST /code/api/v1/chat/completions` — butuh `X-Workspace-ID` +
  `Origin/Referer`; `gpt-*` pakai `max_completion_tokens`, strip
  temperature/top_p (sesuai kontrak NvRouter `enter_converge.go`).
- Bare `gpt-6-astra` → 400 `unsupported model`; **`openai/gpt-6-astra` → 200,
  balas `ASTRA_OK`** (TTFT ~1.4s). Wire ID perlu prefix vendor.
- Temuan: `enterNativeModels` NvRouter belum kenal model baru
  (`gpt-6-astra`, `gemini-3.6-flash`, `qwen-3.8-max`, `deepseek-v4-*`,
  `auto`, ...) — perlu update mapping.

## 4b. Verifikasi ulang + koreksi (2026-10-01, dari farm `enter-v3-google`)

Pakai `ek_` key hasil OAuth Google (`prayoga2`/`prayoga3`). Detail lengkap di
`endpoints.md`.

- `GET workspaces/{id}/models` → 200, **15 models** (bukan 18; daftar berubah).
  **Bukan katalog penuh.**
- 🆕 `GET ai-capability/models` → 200, **58 model** — sumber otoritatif, punya
  `protocol` per entri. Ini yang harus dipakai untuk enumerasi, bukan `/models`.
- 🆕 `credits/dashboard` → 200: akun baru = **200 credit, semua `bonus`**.
  `credits/balance` → 404. Rate terukur: sol ~0.93k/1M tok, astra ~4.9k/1M
  output, 5.6-sol ~5.7k/1M output. **200 credit = puluhan–ratusan ribu token.**
- 🆕 Model delist masih hidup: `gpt-5.6-sol` tidak di `/models` tapi
  `openai/gpt-5.6-sol` → 200.
- ⚠️ **Koreksi**: `anthropic/claude-*` **jalan di dua dialect** —
  `/chat/completions` (200, balas `chatcmpl-`) DAN `/messages` (200, balas
  `msg_`). `anthropic-version` opsional. Jadi claude tidak terkunci ke Anthropic.
  Tapi `protocol` tetap penunjuk jalur resmi: `gemini-3.1-pro-preview`
  (protocol=anthropic_messages) → `/chat/completions` = **502**.
- ❌ `claude-fable` tidak ada (27 varian diprobe → 400 semua; tidak ada di 58
  entri katalog). Claude terbaru = `claude-opus-5.5` + `claude-sonnet-5.5`.

## 5. Sisa / pending

- NvRouter push off by design (`NINEROUTER_VPS_EVERY_N` off) — set env bila
  mau push beneran.
- Grace captcha 15s → 10s (belum ke-run; ekspektasi total ~75s).
- Kandidat hemat berikutnya: OTP wait (tergantung mail datang, di luar kontrol).
- Jangan commit kredensial; log canary di luar repo (Temp).

## 6. Referensi NvRouter (evidence pack 2026-09-20)

Empat report explore (`enowx/.omo/reports/p*-enter-*.md`) di-merge ke
`docs/nvrouter-enter.md`: identitas/registrasi, auth+workspace, trinitas ID,
jalur OpenAI/Anthropic + SSE hardening per-commit, models/quota, akun/admin,
capability/pipeline/dispatch, sejarah 8 commit (v1 OpenAI-only → mapping →
messages → signature platform → SSE fixes), test map, open questions.
