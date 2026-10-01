# Enter/Converge — v3-google (OAuth variant)

Sama produknya dengan `../enter-v3`, tapi pre-auth pakai **Google OAuth**,
bukan alur email OTP. Jadi: **tidak ada mailbox, tidak ada OTP, tidak ada
halaman password, tidak ada Turnstile** di step identifier. Tinggal klik
"Continue with Google" → Google signin → consent → callback → JWT gateway.

Post-auth **identik** dengan `../enter-v3` (`postauth_v3.py` dipakai apa adanya):
`referral/claim` → `users/info` → `workspaces` → `onboarding/config` →
`onboarding/complete` → `workspaces/{id}/api-keys` → `ek_` key.

## Arsitektur (sama seperti enter-v3)

`enter-v3` bukan implementasi mandiri — dia **lapisan tipis** di atas browser
core legacy `../enter/farm.py`, di-load by path lewat `_load_legacy_farm()`:

```
enter-v3/farm.py:46   def _load_legacy_farm(): ...
enter-v3/farm.py:61   L = _load_legacy_farm()
enter-v3/farm.py:365  v, e = await L.extract_fpjs(page, attempt)
enter-v3/farm.py:368  rs = await asyncio.to_thread(L._get_risk_session_id, ...)
enter-v3/farm.py:493  tokens = await L._fetch_gateway_session(page)
enter-v3/farm.py:785  manager, _browser, page = await L.launch_browser(proxy_url)
```

`enter-v3` hanya menulis ulang 2 hal: `do_signup_v3` (pre-auth email) dan
`postauth_v3.py` (post-auth, karena legacy masih host `converge-ai.us.auth0.com`).
Farm ini mengikuti arsitektur itu persis:

| Bagian | Sumber |
|---|---|
| `launch_browser`, `goto_with_retry`, `extract_fpjs`, `_get_risk_session_id`, `_fetch_gateway_session`, `next_proxy`, `wait_url` | reuse legacy via `L` (sama seperti enter-v3) |
| `do_signup_google` (pre-auth **Google OAuth**) | ditulis di farm ini — **inilah bedanya** |
| `postauth_v3.py` | copy apa adanya dari `../enter-v3` |
| `do_signup_and_oauth` legacy | **tidak dipakai** (sama seperti enter-v3) |

## Alur (dari HAR)

```
[BROWSER = do_signup_google di farm.py]
  gift landing (?gift=..&inviter=..&inviteeReward=..)
  -> FPJS extract -> risk-session (POST /code/api/v1/auth/risk-session)
  -> /auth/login?return_to=/&risk_session_id=..
  -> /authorize (PKCE S256) -> /u/login/identifier?state=..
  -> klik form[data-provider="google"] (hidden state + connection=google-oauth2)
  -> accounts.google.com/v3/signin/identifier  (email)
  -> (password hanya kalau Google minta)
  -> speedbump/workspacetermsofservice  (kalau muncul)
  -> signin/oauth/consent  (Continue/Allow)
  -> auth.converge.ai/login/callback?code=.. -> /authorize/resume
  -> enter.converge.ai/auth/callback?code=.. -> /workspace
  -> GET /auth/session?include=access_token  (JWT)
        |
        v
[HTTP = postauth_v3.py, sama persis dengan enter-v3]
  POST referral/claim?code=<gift>   (nonfatal)
  GET  users/info
  GET  workspaces
  GET  onboarding/config
  POST onboarding/complete          {"role","team_size","build_intent"}
  POST workspaces/{id}/api-keys
  GET  referral/rewards             (best-effort)
```

## Bukti reverse (HAR 2026-10-01)

| HAR | Dipakai untuk |
|-----|---------------|
| `google.har` | login path (`prompt=login`); risk-session + authorize + callback + `/auth/session` |
| `signup google.har` | signup path dengan `invite_code` di body risk-session |
| `signup google denied.har` | signature `access_denied` di `/authorize/resume` → `/auth/callback?error=access_denied` |

Kontrak terverifikasi dari HAR:
- `POST /auth/risk-session` body sama: `{"fp_event_id","visitor_id","platform":"web"}`
  (+ `invite_code` di jalur signup).
- Provider form = **form secondary**, bukan primary:
  `<form data-provider="google" data-form-secondary="true">` dengan
  `<input name="connection" value="google-oauth2">`. Tombolnya
  `data-action-button-secondary="true"` — justru yang **dikecualikan** farm email
  (`_SOCIAL_RE` + filter `data-provider`). Di farm ini provider form itu
  **target** klik, bukan dihindari.
- `/auth/session?include=access_token` balikan `{user:{sub:"google-oauth2|..."}, accessToken}`.
- `access_denied` = Google menolak; muncul sebagai 403 JSON di `/auth/callback`.

## Pakai

```bash
# hub venv saja (jangan bikin venv lokal)
python -m jobs list                 # harus ada enter-v3-google
python -m jobs run enter-v3-google -- -n 1 -c 1 -y

# dry-run lewat hub
python -m jobs run enter-v3-google --dry-run -- -n 1 -c 1 -y
```

Langsung tanpa hub:

```bash
.venv/Scripts/python.exe farms/enter-v3-google/farm.py -n 1 -c 1 -y
```

Satu akun = satu baris `OK` di stdout. Output batch di `results/batch_*/`
(`accounts.json`, `credentials.txt`, `apikeys.txt`) plus file global append-only.
Akun yang sudah dipakai dicatat di `results/used_google.txt` dan di-skip.

## Pool akun Google

`google_accounts.txt`, satu baris `email|password` (juga terima `email:password`
atau `email<TAB>password`). Override path lewat `ENTER_GOOGLE_ACCOUNTS`.
Kalau pool habis / semua sudah dipakai, farm exit dengan pesan jelas.

## Referral chain ("estafet") — bonus inviter terbukti masuk

`POST /referral/claim` selalu balas `200 "successfully"` walau server menahan
bonus (lihat `enter-v3/README.md`). Karena itu farm ini **memverifikasi** bonus
lewat `GET workspaces/{id}/credits` (200 = masuk, 100 = disunat) dan menyimpan
`referral_code` / `credits_total` / `invitee_bonus_landed` di `accounts.json`.

Mode rantai: akun #1 pakai `ENTER_GIFT_CODE`, lalu `referral_code` tiap akun
jadi gift akun berikutnya. **Wajib serial** (`-c` dipaksa 1).

```bash
.venv/Scripts/python.exe farms/enter-v3-google/farm.py -n 3 -c 1 --chain -y
# atau: ENTER_GIFT_CHAIN=1
```

Probe terverifikasi 2026-10-01 (3 akun, seed `SY2V0NYTVG`, direct IP):

| Akun | Claim | Code dipakai oleh | Credit |
|---|---|---|---|
| mala1 | seed | mala2 | **300** |
| mala2 | mala1 | mala3 | **300** |
| mala3 | mala2 | — | **200** |

**300 = 100 base + 100 invitee + 100 inviter.** Bonus inviter nyata, bukan
config. Akun induk seed (`prayoga1`) juga naik 100 → 200. State rantai di
`results/referral_chain.json` (resume antar-run). Kalau rantai putus, default
lanjut pakai seed (`ENTER_GIFT_CHAIN_FALLBACK=1`); set `0` untuk gagal keras.

### Referral tip — seed buat akun Google berikutnya

Tiap run, akun **terakhir** yang sukses dicatat sebagai **tip**. Code-nya belum
diklaim siapa pun, jadi itu gift yang benar untuk akun Google berikutnya yang lo
tambahkan ke pool. Muncul di log:

```
[CHAIN] TIP referral for the next Google account:
[CHAIN]   code : X7KAVIQYNV
[CHAIN]   link : https://enter.converge.ai/?gift=X7KAVIQYNV&inviteeReward=100&inviter=...
[CHAIN]   from : mala5@gpspindwaal.com
```

Juga ditulis ke `results/referral_tip.txt` (`code=`, `link=`, `from=`, `name=`)
dan per-akun di log: `[n] TIP  newest referral <CODE> (use for the next Google account)`.

Jadi alurnya: habiskan pool → ambil tip → set `ENTER_GIFT_CODE` ke tip itu (atau
biarkan rantai resume otomatis, karena `next_gift` = tip) → tambah Google baru →
lanjut.

## Cartethyia Postgres inject

Setiap `ek_` key yang sukses **langsung** di-upsert ke Postgres Cartethyia
(`provider_accounts`, provider id `enterconverge`) — jadi key baru langsung
routable tanpa import manual. Selain itu farm tetap menulis txt/json seperti biasa.

Implementasi: `core/cartethyia.py` (shared, dipakai juga oleh `enter-v3`).

Yang harus cocok dengan Cartethyia (kalau tidak, row-nya tidak terpakai):

| Kolom | Isi |
|---|---|
| `credential_ciphertext` | AES-256-GCM, layout `iv(12)‖authTag(16)‖ct`, key `CARTETHYIA_ENCRYPTION_KEY` |
| `credential_fingerprint` | `HMAC-SHA256(key, secret)` hex — sama dengan `hashSecret()` |
| `auth_state.workspaceId` | workspace id numerik (adapter baca dari sini) |
| `tenant_id` | `NULL` = shared pool-wide (memang begitu untuk key hasil farm) |

Kredensial + `DATABASE_URL` dibaca dari `Cartethyia\.env`
(override: `CARTETHYIA_ENCRYPTION_KEY`, `CARTETHYIA_DATABASE_URL`/`DATABASE_URL`,
atau `CARTETHYIA_ENV_FILE`).

**Fail-soft:** driver tidak ada / DB mati / key tidak ada → warning saja, farm
tetap mencatat hasilnya. Upsert idempoten: key yang sama tidak bikin row kembar
(unique index `(provider_id, coalesce(tenant_id), credential_fingerprint)`).

Matikan dengan `ENTER_CARTETHYIA_INJECT=0`.

## Proxy pool — multi-warp (handle "too many signup")Auth0 rate-limit itu **per-IP**, jadi N akun dari 1 IP bakal kena
`Too many signup attempts`. Farm ini round-robin proxy pool, **satu akun = satu
proxy sticky** (browser + risk-session + post-auth satu egress).

`proxy.txt` (gitignored), satu proxy per baris. Output multi-warp
(`warp-pool.txt`) masuk **apa adanya**:

```
socks5://127.0.0.1:40001
socks5://127.0.0.1:40002
...
```

Tanpa file ini, semua akun keluar dari IP lokal — dan itu penyebab rate-limit.

### multi-warp di Windows (terverifikasi 2026-10-01)

Repo [Micolaabdi/multi-warp](https://github.com/Micolaabdi/multi-warp) bilang
Windows Docker Desktop "not supported as-is". **Tapi di mesin ini jalan**: Docker
Desktop kasih `/dev/net/tun` + `NET_ADMIN` ke container, dan port publish ke host OK.

```bash
git clone https://github.com/Micolaabdi/multi-warp
python scripts/generate-compose.py -n 5 -o docker-compose.yml   # lihat catatan bug di bawah
mkdir -p data-n1 .. data-n5 && docker compose up -d
# tunggu ~75s, lalu cek exit IP tiap port
```

**⚠️ Bug upstream:** `generate-compose.py` nulis file tanpa `encoding=`, jadi di
Windows em-dash di header jadi byte `0x97` (bukan UTF-8) dan
`docker compose` gagal dengan `go-yaml load error ... invalid leading UTF-8 octet`.
Regenerate lewat Python dengan `write_text(text, encoding="utf-8")`.

### Temuan penting: IPv4 dobel, IPv6 unik

Terukur di mesin ini (5 node):

```
unique IPv4 exit : 2 / 5     <- Cloudflare anycast reuse (seperti yang repo bilang)
unique IPv6 exit : 5 / 5     <- tiap registrasi WARP dapat IPv6 sendiri
```

Yang penting: **Enter lihat IPv6-nya**. Dicek lewat `auth.converge.ai/cdn-cgi/trace`
(host yang dipakai signup) → 5 IP unik. Sama untuk `enter.converge.ai` dan
`api.enter.pro`. Jadi pool 5 node = **5 egress unik buat Enter**, walau IPv4-nya
cuma 2. Karena itu `_probe_exit_ip()` sengaja baca `/cdn-cgi/trace` (bukan
`api.ipify.org` yang cuma IPv4) — kalau pakai ipify, lo bakal salah kira pool-nya
dobel padahal unik.

Loop `recreate` ala `mw.sh` (wipe `data-nX` + recreate sampai unik) tetap berguna
kalau lo butuh IPv4 unik juga, tapi buat Enter nggak perlu: IPv6 sudah unik.

## Env (lihat `.env.example`)

Hub `.env` menang (`load_dotenv(override=False)`). Kunci khusus farm ini:
`ENTER_GOOGLE_ACCOUNTS`, `ENTER_GOOGLE_USED_FILE`, `ENTER_GOOGLE_STEP_TIMEOUT`.
Sisanya sama seperti enter-v3 (`ENTER_GIFT_CODE`, `ENTER_HEADLESS`, onboarding, dll).

## Catatan

- Farm ini **serial per proses** (`-c 1`). Skala via beberapa lane supervisor
  terisolasi, bukan naikkan `-c`.
- Proxy wajib **sticky** selama satu akun. Farm ini **mem-pin** `_current_proxy`
  ke egress browser supaya POST risk-session keluar dari IP yang sama
  (`enter-v3` tidak mem-pin ini; legacy mendokumentasikannya sebagai wajib).
- `access_denied` biasanya bukan bug farm — Google menolak OAuth request
  (akun baru/berisiko, atau consent ditolak). Ganti akun, jangan retry buta.
- Akun Google yang belum pernah login dari profil browser baru akan kena
  verifikasi tambahan; siapkan akun yang "warm".
