# enter-v3 — Automation

Fresh-path farmer akun Enter/Converge (v3). Output per akun: satu `ek_` API key
+ workspace id, siap push ke NvRouter. Terpisah dari `farms/enter/` (legacy,
tidak diubah, jadi referensi/fallback).

Hasil live (2026-09-20): `ok=1/1`, total run **~82 detik** (canary17).

## Kenapa hybrid

Pre-auth dilindungi Cloudflare + Turnstile + FingerprintJS + binding
`risk-session` ↔ Auth0 `state`. Recon live: `GET /` tanpa `cf_clearance` →
403, `risk_session_id` palsu → `/u/signup/identifier` 400. Jadi pre-auth
**wajib browser** (Camoufox).

Post-auth (`api.enter.pro`) REST bersih: cukup `Authorization: Bearer`
(JWT gateway), tanpa cookie browser. Jadi post-auth **HTTP murni** (~1–3 dtk).

## Alur

```
[BROWSER — do_signup_v3 di farm.py; core legacy reuse tanpa diubah]
  gift landing (?gift=&inviter=&inviteeReward=)
  -> FPJS extract -> risk-session -> /auth/login
  -> baca state dari URL -> langsung /u/signup/identifier (tanpa race CTA)
  -> identifier (email + Turnstile) -> email OTP (enowX) -> password (+Turnstile risk-based)
  -> /authorize/resume -> /auth/callback -> app (/workspace) -> /auth/session (JWT)
  -> browser ditutup
        |
        v
[HTTP — postauth_v3.py]
  POST referral/claim?code=<gift>      (nonfatal)
  GET  users/info                      (must_verify_email=false, merge check)
  GET  workspaces                      (fail-closed bila kosong; pakai id NUMERIK)
  GET  onboarding/config               (terima flow_version v2 ATAU v3)
  POST onboarding/complete             {"role","team_size","build_intent"} (skip bila completed)
  POST workspaces/{id}/api-keys        {"name","scope","reveal_policy"}
  GET  referral/rewards                (best-effort)
```

3 screen onboarding UI (role/team/intent di `/workspace`) **tidak diklik** —
HAR membuktikan nol API call; satu-satunya call adalah `POST complete`.

## Mailbox — enowX

Default `ENTER_EMAIL_PROVIDER=enowx`, service lokal (`ENTER_ENOWX_BASE`,
default `http://127.0.0.1:1430`). Adapter `enowx_mail.py`:
mint address → poll inbox → ekstrak OTP 6-digit dari `<code>NNNNNN</code>`
(from `noreply@converge.ai`, subject `Verify your email`).

**Domain tidak semuanya menerima** (proven 2026-09-20):

| Domain | Status |
|---|---|
| `novacontigencia.xyz` | OK — OTP ~4–13 detik |
| `myamya.tech` | OK — mail masuk |
| `supplementwiki.org` | GAGAL — submit OK, nihil setelah 6+ menit |
| `samaltour.site` | belum teruji |

Pin via `ENTER_ENOWX_DOMAIN` di farm `.env` (lihat `.env.example`).
Rotasi antar domain proven per lane bila Enter memblokir domain overuse.

## Env penting

| Key | Default | Keterangan |
|---|---|---|
| `ENTER_GIFT_CODE` | — (wajib) | referral gift |
| `ENTER_INVITER` | — | nama inviter |
| `ENTER_ENOWX_DOMAIN` | acak `domains[0]` | pin domain proven |
| `ENTER_ONBOARDING_ROLE` / `_TEAM_SIZE` / `ENTER_BUILD_INTENT` | `founder` / `just_me` / `other` | HAR-proven |
| `ENTER_API_KEY_NAME` / `_SCOPE` / `_REVEAL` | `farm` / `all` / `create_only` | — |
| `ENTER_OTP_TIMEOUT` | 180 | detik tunggu OTP |
| `ENTER_ACCOUNT_TIMEOUT` | 600 | — |
| `ENTER_ACCOUNT_GAP` / `ENTER_SPAWN_DELAY` | 30 / 20 | pacing |

Hub `.env` menang (`load_dotenv(override=False)`); farm `.env` hanya untuk gap.

## Menjalankan

```bash
# hub venv saja, jangan bikin venv lokal
python -m jobs list                  # harus ada enter-v3
python -m jobs run enter-v3 -- -n 1 -c 1 -y

# langsung (debug headed)
.venv/Scripts/python.exe farms/enter-v3/farm.py -n 1 -c 1 -y --headed
```

Satu akun = satu baris `OK` di stdout (progress HUD).
Output: `results/batch_*/{accounts.json,credentials.txt,apikeys.txt,batch_meta.json}`
plus file global append-only. Breakdown waktu tipikal (82s): landing→FPJS ~9s,
OTP tunggu mail ~13s, grace captcha ~10s, callback→session ~3s, post-auth ~1s.

## Operasional

- Serial per proses (`-c 1`). Skala via beberapa lane terisolasi
  (referral + proxy + domain beda), bukan naikkan `-c`.
- Proxy wajib sticky satu akun (browser + risk-session + post-auth satu egress).
- NvRouter push skip by design bila `NINEROUTER_VPS_EVERY_N` off.
- Jangan commit kredensial. Jangan restart farm tiap N akun.
- Tutup window browser lama (orphan dari run abort bikin bingung + makan CPU);
  farm kini close eksplisit + log `browser closed`.

## Troubleshooting

| Gejala | Penyebab | Fix (sudah di code) |
|---|---|---|
| Klik "Continue" malah buka Google OAuth | match teks "Continue" kena "Continue with Google" | regex + exclude social/`data-provider` |
| Stuck setelah Turnstile Success | tombol honeypot `opacity:0`/`pointer-events:none` tetap match selector & `is_visible()=True` | selector hanya `data-action-button-primary`, filter computed-style, `requestSubmit`, verifikasi navigasi |
| Log sunyi 80s di callback padahal sudah `/workspace` | legacy `_is_enter_app_url` cuma match path `/` persis | terima semua path `enter.converge.ai` + heartbeat tiap 10s |
| OTP tidak datang | domain enowX tidak menerima | pin `ENTER_ENOWX_DOMAIN` ke domain proven |
| Password 2+ menit / submit tidak jalan | Turnstile password risk-based (kadang tidak ada); klik pointer kena overlay | gate token hanya bila mount ada; **Enter dulu** (implicit submission, bypass overlay), baru click/js/requestSubmit |
| Window JUGGLER menumpuk | `browser.close()` gagal diam-diam saat abort | close eksplisit + log; tutup manual sisa orphan |

Detail kronologi develop: `handoff.md`. Referensi API: `endpoints.md`.
