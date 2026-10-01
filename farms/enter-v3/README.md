# Enter/Converge — v3 (fresh path)

Tujuan: farm akun Enter Pro versi baru (v3), hasil akhir satu `ek_` API key +
workspace id, siap push ke NvRouter. Ini **fresh path** terpisah dari
`../enter/` (yang tetap utuh sebagai referensi/fallback).

Docs: `docs/automation.md` (arsitektur + operasi), `docs/handoff.md`
(kronologi develop), `docs/endpoints.md` (referensi API live-tested),
`docs/nvrouter-enter.md` (merge evidence pack provider NvRouter).

## Kenapa hybrid

Pre-auth versi baru masih dilindungi Cloudflare + Turnstile + FingerprintJS +
binding `risk-session` ↔ Auth0 `state`. Recon live membuktikan:
`GET /` direct tanpa `cf_clearance` → **403**, dan `risk_session_id` palsu bikin
`/u/signup/identifier` → **400**. Jadi pre-auth **wajib browser**.

Post-auth (`api.enter.pro`) adalah REST bersih: cukup `Authorization: Bearer`
(tanpa cookie browser, tanpa `X-Enter-GA-Context` — terverifikasi 200). Jadi
post-auth **pakai HTTP** biar cepat & tidak flaky.

Karena itu: **auth = browser (do_signup_v3 milik farm ini, reuse core legacy)**,
**post-auth = HTTP v3**. Status 2026-09-20: full run `ok=1/1` → `ek_` key
tersimpan (`canary13.log`, 191s).

## Mailbox — enowX (bukan legacy)

Mailbox default = **enowX** (`http://127.0.0.1:1430`), bukan mode legacy. Adapter
`enowx_mail.py` pakai API enowX:
- `GET /api/mail/domains` → daftar domain
- `POST /api/mail/addresses {"domain":..}` → mint alamat
- `GET /api/mail/messages?address=..` → baca inbox

OTP Enter datang dari `noreply@converge.ai`, subject `Verify your email`, kode
6-digit di dalam `<code>NNNNNN</code>`. Farm ini pakai enowX langsung
(`_wait_otp` → `enowx_mail.wait_for_otp`).

**Domain tidak semuanya menerima.** Proven 2026-09-20: `novacontigencia.xyz`
(OTP ~4–7 detik), `myamya.tech` (mail masuk). Proven gagal:
`supplementwiki.org` (submit OK, nihil setelah 6+ menit). Karena itu farm pin
`ENTER_ENOWX_DOMAIN` di `.env` lokal (lihat `.env.example`). Kalau Enter
memblokir domain karena overuse, rotasi ke domain proven lain per lane.

Legacy `rotate`/gptmail/emailqu hanya dipakai kalau
`ENTER_EMAIL_PROVIDER != enowx`.

## Referral chain ("estafet") — hindari bonus disunat

Masalah: `POST /referral/claim` **selalu** balas `200 "claim referral code
successfully"`, tapi server **diam-diam menahan bonus invitee** kalau code itu
sudah dipakai terlalu sering. Bukti: `HTTPToolkit_2026-08-03.har` → claim 200,
`credits_balance.total` tetap **100** (harusnya 200). Jadi response claim
**tidak bisa dipercaya** — satu-satunya sinyal valid adalah credits.

Solusi: **rantai referral**. Tiap akun punya `referral_code` sendiri (dari
`users/info`), jadi:

```
akun #1 pakai ENTER_GIFT_CODE  ->  dapat referral_code miliknya
akun #2 pakai referral_code #1 ->  dapat referral_code miliknya
akun #3 pakai referral_code #2 ->  ...
```

Tiap claim pakai code **fresh**, jadi tetap di bawah threshold throttle.

Aktifkan:

```bash
ENTER_GIFT_CHAIN=1
ENTER_GIFT_CODE=<seed code untuk akun pertama>
# atau tanpa env: farm.py -n 5 -c 1 --chain -y
```

Aturan penting:
- **Wajib serial** — `-c` dipaksa ke `1` (link N butuh `referral_code` dari N-1).
- Seed wajib ada: `ENTER_GIFT_CODE`, atau resume dari `next_gift` di state file.
- State disimpan di `results/referral_chain.json` (`next_gift`, `index`,
  `used_codes`) → run bisa **dilaneskan** (resume) tanpa kehilangan rantai.
- Tiap akun di-verifikasi: `credits_total` **200** = bonus masuk, **100** =
  disunat (di-log `CREDITS` + disimpan di `accounts.json`).
- Kalau ada akun tanpa `referral_code` → rantai putus. Default
  (`ENTER_GIFT_CHAIN_FALLBACK=1`) lanjut pakai seed; set `0` untuk gagal keras.

### Bonus inviter: TERBUKTI masuk (probe 2026-10-01)

Probe 3 akun berurutan (`enter-v3-google`, seed `SY2V0NYTVG`, direct IP) —
saldo diukur ulang lewat `ek_` **setelah** rantai jalan:

| Akun | Claim code | Code-nya dipakai oleh | Credit akhir |
|---|---|---|---|
| mala1 | `SY2V0NYTVG` (seed) | mala2 | **300** |
| mala2 | `ADR8ZIZYUU` (mala1) | mala3 | **300** |
| mala3 | `201Q2W0C1W` (mala2) | — (belum) | **200** |

300 = 100 base + 100 invitee + **100 inviter**. Jadi `inviter_reward:100` **bukan**
cuma config — saldonya benar-benar naik saat code kita dipakai orang lain. Akun
induk seed (`prayoga1`) juga terukur naik 100 → 200.

**Konsekuensi untuk skala:** tiap akun di tengah rantai bernilai **300 credit**
(2× akun terakhir). Rantai N akun = `200·N + 100·(N−1)` credit total. Bonus
inviter datang **terlambat** (saat link berikutnya jalan), jadi akun terakhir
rantai selalu 200 sampai ada link baru.

## Alur

```
[BROWSER = do_signup_v3 di farm.py; core ../enter/farm.py reuse tanpa diubah]
  gift landing (?gift=..&inviter=..&inviteeReward=..)
  -> FPJS extract -> risk-session -> /auth/login
  -> state param dibaca -> langsung /u/signup/identifier (tanpa race CTA)
  -> Auth0: signup/identifier (+captcha) -> email OTP -> signup/password
  -> /authorize/resume -> /auth/callback -> app (/workspace) -> /auth/session (JWT)
  Jebakan yang sudah di-fix di layer farm ini:
  - klik "Continue" jangan match "Continue with Google" (regex + social exclude)
  - tombol honeypot opacity:0/pointer-events:none (klik nyangkut) -> selector
    hanya data-action-button-primary + filter computed-style + verifikasi
    navigasi (_primary_submit)
  - legacy _is_enter_app_url cuma match path "/" persis -> terima semua path
    enter.converge.ai sebagai callback-ok (+ heartbeat log tiap 10s)
  - submit order: Enter dulu (implicit submission, bypass overlay; 3/3 run
    password lolos via enter), baru click/js/requestSubmit. Total run 205s
    -> 82s (canary17).
  - Turnstile password risk-based (kadang tidak ada) -> submit hanya di-gate
    token kalau mount-nya ada; handler legacy di-skip kalau tidak ada mount
    (klik koordinat butanya pernah submit form diam-diam).
  - browser orphan dari run abort (window JUGGLER menumpuk, bikin bingung
    screenshot + makan CPU) -> close eksplisit + log "browser closed".
        |
        v  (browser ditutup; 3 screen onboarding role/team/intent TIDAK diklik —
            HAR membuktikan nol API call, hanya POST complete di bawah)
[HTTP = postauth_v3.py, v3 reversed]
  POST referral/claim?code=<gift>     (nonfatal; 200 TIDAK berarti bonus masuk)
  GET  users/info                     (must_verify_email=false, merge check;
                                       ambil referral_code milik akun ini)
  GET  workspaces                     (fail-closed bila kosong)
  GET  onboarding/config              (terima flow_version v2 ATAU v3)
  POST onboarding/complete            {"role","team_size","build_intent"}  (skip bila sudah completed)
  POST workspaces/{id}/api-keys       {"name","scope","reveal_policy"}
  GET  workspaces/{id}/credits        (verifikasi bonus: 200 = masuk, 100 = disunat)
  GET  referral/rewards               (best-effort)
```

## Bukti reverse (3 HAR 2026-09-20)

| HAR | Dipakai untuk |
|-----|---------------|
| `newversion.har` | flow tanpa referral; signup + onboarding + api key |
| `suksesreferal.har` | **referral penuh** → `referral/claim?code=` 200, post-auth v3 urut, `ek_` key |
| `domainnotallow.har` | signature domain-block di step **password** (400 `extensibility_error`) |

Kontrak yang sudah terverifikasi (source + HAR saling cocok):
- `onboarding/complete` payload resmi = **3 field** `{role, team_size, build_intent}`
  (source `onboarding-runtime`: `return{role:e.role,team_size:e.teamSize,build_intent:e.buildIntent}`).
- `gift` == `code` == `inviteCode` (landing → return_to → claim).
- `flow_version` live = **v3**; legacy farm gagal di cek `!= "v2"`.
- domain-block muncul di **password step**, bukan identifier.

## Pakai

```bash
# hub venv saja (jangan bikin venv lokal)
python -m jobs list                 # harus ada enter-v3
python -m jobs run enter-v3 -- -n 1 -c 1 -y

# dry-run lewat hub
python -m jobs run enter-v3 --dry-run --warp-every-n 1 -- -n 1 -c 1 -y
```

Langsung tanpa hub:

```bash
.venv/Scripts/python.exe farms/enter-v3/farm.py -n 1 -c 1 -y

# referral chain (estafet): -c dipaksa 1
.venv/Scripts/python.exe farms/enter-v3/farm.py -n 5 -c 1 --chain -y
```

Satu akun = satu baris `OK` di stdout (buat progress HUD). Output batch di
`results/batch_*/` (`accounts.json`, `credentials.txt`, `apikeys.txt`) plus file
global append-only. `accounts.json` menyimpan `referral_code`, `credits_total`,
dan `invitee_bonus_landed` per akun.

## Cartethyia Postgres inject

Setiap `ek_` key yang sukses langsung di-upsert ke Postgres Cartethyia
(`provider_accounts`, provider id `enterconverge`), jadi key baru langsung
routable tanpa import manual. Farm tetap menulis txt/json seperti biasa.

Implementasi: `core/cartethyia.py` (shared — dipakai juga oleh `enter-v3-google`).
`credential_ciphertext` = AES-256-GCM (`iv(12)‖authTag(16)‖ct`) dengan
`CARTETHYIA_ENCRYPTION_KEY`; `credential_fingerprint` = `HMAC-SHA256(key, secret)`;
`auth_state.workspaceId` = workspace id numerik. Verifikasi byte-compatible
dilakukan dengan mendekripsi row yang sudah ada di DB.

**Fail-soft** (DB mati / key hilang → warning, farm lanjut) dan **idempoten**
(key sama tidak bikin row kembar). Matikan: `ENTER_CARTETHYIA_INJECT=0`.

## Env (lihat `.env.example`)

Hub `.env` menang (`load_dotenv(override=False)`). Wajib ada `ENTER_GIFT_CODE`
(kecuali resume rantai dari `results/referral_chain.json`). Default onboarding v3
HAR-proven: `founder / just_me / other`.

Kunci rantai: `ENTER_GIFT_CHAIN`, `ENTER_GIFT_CHAIN_FALLBACK`, `ENTER_CHAIN_STATE`.
Kunci verifikasi bonus: `ENTER_BASE_SIGNUP_CREDITS`, `ENTER_REFERRAL_BONUS_TOTAL`.
Kunci inject: `ENTER_CARTETHYIA_INJECT`, `ENTER_CARTETHYIA_ENV_FILE`.

## Yang TIDAK dipakai

- `../enter/signup_http.py` + `do_signup_http` — full-HTTP lama: host masih
  `converge-ai.us.auth0.com`, endpoint OTP/password beda, dan mentok CF/Turnstile.
- Klik browser untuk onboarding role/team/intent — tidak menghasilkan API call.
- Mengubah `../enter/farm.py` — utuh sebagai fallback; semua override di farm ini.

## Catatan operasional

- Farm ini **serial per proses** (`-c 1`). Skala via beberapa lane supervisor
  terisolasi (referral + proxy + prefix beda), bukan naikkan `-c`.
- Proxy wajib **sticky** selama satu akun (browser + risk-session + post-auth
  satu egress).
- `domain_not_allowed` = satu-satunya alasan blacklist permanen domain.
