# Railway farm, Google OAuth (`job id: railway`)

Farm akun Railway baru lewat **Google OAuth**, lalu jalankan onboarding
post-auth **lewat HTTP** (cookie-based, tanpa bearer token), lalu simpan hasil.

Farm ini **mandiri**: `AsyncCamoufox` sendiri, tidak me-load farm lain. Polanya
mengikuti `../firecrawl/farm.py` (struktur config `_env`, `launch_browser`,
`safe_fill`/`safe_click`, `_click_next_google`, handler consent/TOS, `_log`,
`init_batch`/`save_result`, CLI `-n -c -y`, `_maybe_warp_after_success`), dengan
helper Google diadaptasi dari `../enter-v3-google/farm.py` (`_fill_google_field`
yang **polling** sampai field muncul, `_GOOGLE_*_SEL`, `_GOOGLE_2FA_MARKERS`,
`_handle_google_step`, `_is_google_url`) dan proxy pool lokal
(`_load_proxy_pool_local` / `_probe_exit_ip` / `_audit_proxy_pool`).

> **"Register" = login OAuth pertama kali.** Railway tidak punya form
> register email/password. Akun Railway baru dibuat saat Google OAuth pertama
> berhasil, tidak ada langkah verifikasi email terpisah.

## Pakai via hub

```powershell
cd C:\Users\Novella\Documents\Github\Automation

# isi pool
copy farms\railway\google_accounts.txt.example farms\railway\google_accounts.txt   # lalu edit
copy farms\railway\proxy.txt.example       farms\railway\proxy.txt                 # opsional

# jalankan
python -m jobs run railway -- -n 5 -c 1 -y
python -m jobs list

# WARP everyN 1:1 dengan -c (hub memaksa everyN == -c)
python -m jobs run railway --warp-every-n 2 -- -n 6 -c 3 -y
```

Solo (dari folder farm, tetap pakai venv hub):

```powershell
..\..\.venv\Scripts\python.exe farm.py -n 1 -c 1 -y
```

## Alur (fakta terverifikasi dari HAR)

Sumber: `HTTPToolkit_2026-10-02_13-15.har` (794 entries, Chrome 154).
Query GraphQL proven diekstrak apa adanya ke `_har_queries.json` (jangan edit
manual, re-extract pakai snippet di bawah).

### Fase 1, entry login (bukan klik tombol)

```
GET https://backboard.railway.com/login/google?state=<base64>
  → 302
```

`state` = **base64 standar (padded)** dari string:

```
next=%2Fdashboard&posthogSessionId=<uuid4>&attribution=%7B%22referringDomain%22%3A%22%24direct%22%2C%22landingPath%22%3A%22%2F%22%7D
```

Response 302 men-set cookie **PKCE**:

| Cookie | Atribut |
|---|---|
| `rw.code_verifier` | `HttpOnly; Secure; SameSite=Lax; Max-Age 900` |
| `rw.code_verifier.sig` | idem |

→ PKCE **S256**, verifier disimpan di sisi Railway. Browser cukup ikut redirect.

### Fase 2, Google authorize (otomatis dari 302)

`GET https://accounts.google.com/o/oauth2/v2/auth` dengan:

| Param | Nilai |
|---|---|
| `client_id` | `659928376555-2940lug33ti1gjmhbeod11g156sag21v.apps.googleusercontent.com` |
| `redirect_uri` | `https://backboard.railway.com/login/google/callback` |
| `scope` | `https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile` |
| `response_type` | `code` |
| `access_type` | `offline` |
| `prompt` | `select_account consent` |
| `code_challenge_method` | `S256` |

### Fase 3, Google sign-in

```
/v3/signin/identifier      → email → Next
/v3/signin/challenge/pwd   → password  ← halaman NORMAL, BUKAN 2FA
speedbump/workspacetermsofservice        → klik "I understand" (opsional)
/signin/oauth/consent      → klik Continue/Allow  (label ID: "Lanjutkan"/"Izinkan")
  → redirect ke callback
```

Catatan penting: `challenge` di URL **bukan** sinyal 2FA, halaman password
normal juga berada di `/v3/signin/challenge/pwd`. Hanya marker berikut yang
berarti 2FA (→ `fail`, bukan retry): `challenge/ipp|totp|az|dp|iap|sk|pk|recaptcha|ootp|selection|webauthn`.

`access_denied` = Google menolak akun → akun ditandai **dead**
(`results/google_dead.txt`) + masuk `used_google.txt`, tidak di-retry.

### Fase 4, callback → sesi

```
GET https://backboard.railway.com/login/google/callback?state=..&code=..&scope=..&hd=..
  → 302  Location: https://railway.com/new
```

Cookie sesi yang di-set (**ini yang direkam**):

| Cookie | Host | Catatan |
|---|---|---|
| `rw.session` | host-only `backboard.railway.com` | iron-session, `rw_Fe26.2**…`, HttpOnly |
| `rw.session.sig` | host-only `backboard.railway.com` | HttpOnly |
| `rw.authenticated` | `Domain=railway.com` | `true` |
| `rw.authenticated.sig` | `Domain=railway.com` |, |

`rw.code_verifier(.sig)` dihapus (expired) di callback ini.

### Fase 5, post-auth via HTTP (cookie-based, TANPA bearer token)

Setelah `https://railway.com/new` termuat:

```
POST https://backboard.railway.com/graphql/internal?q=<operationName>
  Content-Type: application/json
  Origin:  https://railway.com
  Referer: https://railway.com/
  Cookie:  rw.session=..; rw.session.sig=..; rw.authenticated=true; rw.authenticated.sig=..
  body:    {"query": <dari _har_queries.json>, "variables": {...}, "operationName": "..."}
```

Urutan yang dijalankan `farm.py`:

| # | Operation | Variables | Gunanya |
|---|---|---|---|
| 1 | `query me` |, | `data.me.id` (user id), `data.me.workspaces[0].id` (workspace id), `plan` |
| 2 | `mutation userTermsUpdate` |, | accept terms |
| 3 | `mutation fairUseAgree` | `{"agree": true}` | fair-use agreement |
| 4 | `query workspace` | `{"workspaceId": <id>}` | verifikasi plan/trial (`customer.isTrialing`) |
| 5 | `query freePlanBalance` | `{"workspaceId": <id>}` | `remainingUsageCreditBalance`, `trialDaysRemaining` |

Catatan:

- `getMonorepoImportStatus` mengembalikan **"Not Authorized"** untuk akun baru
  itu normal, **bukan** error. Karena itu operation ini tidak dipakai.
- `wss://backboard.railway.com/graphql/internal` (subprotocol
  `graphql-transport-ws`) ada di HAR tapi **tidak** dipakai (rapuh).
- Post-auth **fail-soft**: kalau GraphQL gagal, cookie tetap disimpan dengan
  `onboarding_ok=false`. Akun yang sudah login tidak dibuang.

### Artefak kunci (ringkas)

| Item | Nilai |
|---|---|
| Entry login | `GET backboard.railway.com/login/google?state=<b64>` |
| state | base64 standar (padded) dari `next=%2Fdashboard&posthogSessionId=<uuid4>&attribution=…` |
| client_id | `659928376555-2940lug33ti1gjmhbeod11g156sag21v.apps.googleusercontent.com` |
| redirect_uri | `https://backboard.railway.com/login/google/callback` |
| PKCE | S256, cookie `rw.code_verifier(.sig)` |
| Cookie sesi | `rw.session`, `rw.session.sig`, `rw.authenticated`, `rw.authenticated.sig` |
| GraphQL | `POST backboard.railway.com/graphql/internal?q=<op>` |

### Re-extract query dari HAR

```python
import json, pathlib
har = json.load(open(r"C:\Users\Novella\Downloads\HAR\railweys\HTTPToolkit_2026-10-02_13-15.har", encoding="utf-8"))
want = ["me","userTermsUpdate","fairUseAgree","workspace","freePlanBalance","platformStatus"]
got = {}
for e in har["log"]["entries"]:
    r = e["request"]
    if r["method"] != "POST" or "backboard.railway.com" not in r["url"]: continue
    pd = r.get("postData")
    if not pd: continue
    try: b = json.loads(pd["text"])
    except Exception: continue
    if b.get("operationName") in want and b["operationName"] not in got:
        got[b["operationName"]] = {"query": b["query"], "variables": b.get("variables")}
pathlib.Path("farms/railway/_har_queries.json").write_text(json.dumps(got, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
print(list(got))
```

## Hasil

Batch folder `results/batch_<stamp>_<hex>/`:

| File | Isi |
|---|---|
| `accounts.json` | satu objek JSON per akun (lengkap) |
| `credentials.txt` | header + satu baris `\|`-separated per akun |
| `batch_meta.json` | id, count, concurrent, ukuran proxy pool |

File global append-only di `results/`:

| File | Isi |
|---|---|
| `all_credentials.txt` | semua kredensial, lintas batch |
| `used_google.txt` | email yang sudah dipakai (di-skip run berikutnya) |
| `google_dead.txt` | email + alasan yang ditolak Google (`access_denied`) |

Header kredensial:

```
# google_email|google_password|cookie|user_id|workspace_id|plan|created_at
```

`cookie` disimpan sebagai string header siap pakai:

```
rw.session=..; rw.session.sig=..; rw.authenticated=true; rw.authenticated.sig=..
```

## Env

Hub `.env` menang; `.env` farm hanya gap (`load_dotenv(override=False)`).
Shared hub keys dipetakan ke `RAILWAY_*` oleh `core/env.py` `_SHARED_MAP`.

| Key | Default | Arti |
|---|---|---|
| `RAILWAY_GOOGLE_ACCOUNTS` | `./google_accounts.txt` | pool Google (`email\|password`) |
| `RAILWAY_GOOGLE_USED_FILE` | `./results/used_google.txt` | email terpakai |
| `RAILWAY_PROXY_FILE` | `./proxy.txt` | pool proxy (1 akun = 1 egress) |
| `RAILWAY_PROXY_POOL` |, | proxy inline, koma-separated |
| `RAILWAY_HEADLESS` | `true` | headless Camoufox |
| `RAILWAY_CONCURRENT` | `1` | worker paralel |
| `RAILWAY_ACCOUNT_TIMEOUT` | `420` | ceiling per akun (detik) |
| `RAILWAY_GOOGLE_STEP_TIMEOUT` | `45` | ceiling per interstitial Google |
| `RAILWAY_CHECK_EXIT_IP` | `true` | audit exit IP tiap proxy |
| `RAILWAY_RESULTS_DIR` | `./results` | output batch |
| `RAILWAY_CARTETHYIA_INJECT` | `0` | opsional (lihat catatan) |
| `RAILWAY_WARP_EVERY_N` | `0` | hub inject; everyN efektif = `max(1, -c)` |

**Exit IP probe:** `_probe_exit_ip()` mengukur lewat
`https://backboard.railway.com/cdn-cgi/trace` (baris `ip=`), bukan echo IPv4-only
seperti ipify. Alasannya: WARP sering memberi **IPv4 anycast yang sama** tapi
**IPv6 unik** per registrasi, jadi probe IPv4-only bikin pool multi-warp terlihat
penuh duplikat. Endpoint ini juga host yang sama dengan GraphQL, jadi inilah IP
yang benar-benar dilihat Railway. (`https://railway.com/cdn-cgi/trace` → 404;
hanya host backboard yang menyajikannya.) Probe tetap fail-soft: error/status
non-200 → `""` dan proxy dilaporkan `unreachable`.

**Cartethyia inject:** Railway hanya punya sesi cookie, tidak ada `ek_` API key,
jadi `core/cartethyia.inject_account` (yang butuh `ek_`) tidak berlaku. Flag ini
tetap ada dan hanya mencatat skip eksplisit di log.

## Log contract (hub HUD)

```
[HH:MM:SS] [<n>] <step>  <msg>  <email>
```

`OK` = satu akun selesai (cookie tersimpan). `fail` = akun gagal. Step lain
(`navigate`, `google_authorize`, `google_password`, `oauth_consent`,
`onboarding`, …) hanya memperbarui label HUD.

## Verifikasi

```powershell
.\.venv\Scripts\python.exe -m jobs list
.\.venv\Scripts\python.exe -m jobs run railway --dry-run --warp-every-n 2 -- -n 2 -c 3 -y
.\.venv\Scripts\python.exe -c "import ast;ast.parse(open('farms/railway/farm.py',encoding='utf-8').read())"
```

## Relay egress (deploy via cookie)

Setiap akun Railway hasil farm dipakai untuk men-deploy **relay proxy
(buff-relay)** di Railway. Modul: `deploy_relay.py`.

### Mode multi-service (default: 5 relay per akun)

Satu akun = **satu project** = **N service** = **N TCP proxy publik**
(`host:port` + `AUTH_USER`/`AUTH_PASS`). Setiap service mendapat egress IP
Railway yang **berbeda**, jadi satu akun menghasilkan sampai 5 relay/IP.

Batas plan HOBBY: **2 project per workspace**, **5 service per project**
karena itu `--per-account` di-clamp ke **1..5** (default 5).

**Bukti live (satu project HOBBY, 4 service → 4 IP egress BERBEDA):**

| service | endpoint | egress IP |
|---|---|---|
| `iriguchi…` | `…:39674` | 152.55.177.123 |
| `acela…` | `…:21438` | 152.55.178.62 |
| `sakura…` | `…:10654` | 152.55.177.198 |
| `iriguchi…` | `…:40950` | 152.55.177.102 |

Jadi `-n 1 --per-account 5` = 1 akun → 1 project → sampai 5 relay → sampai 5 IP
unik. Tiap service dapat password acak sendiri (`secrets.token_urlsafe(12)`)
kalau `AUTH_PASS` tidak di-pin.

### Urutan yang TERVERIFIKASI (GraphQL internal, cookie-based)

| # | Operation | Gunanya |
|---|---|---|
| 1 | `query me` | ambil `me.workspaces[0].id` (workspace tempat project dibuat) |
| 2 | `mutation projectCreate` | buat project kosong di workspace, **sekali per akun** |
| 3 | `mutation serviceCreate(repo)` | buat service dari repo GitHub (default `NetroIndonesia/buff-relay`), **N kali per akun** |
| 4 | `stageEnvironmentChanges` + `environmentPatchCommitStaged` | stage + commit perubahan environment |
| 5 | `variableCollectionUpsert` | set `AUTH_USER` / `AUTH_PASS` untuk relay (password beda tiap service) |
| 6 | patch `networking.tcpProxies:{"8080":{}}` + commit | buat TCP proxy untuk port app |
| 7 | poll `tcpProxies` | tunggu sampai `domain` + `proxyPort` muncul |

Hasil akhir: `host:proxyPort` (mis. `acela.proxy.rlwy.net:23117`) plus kredensial
auth relay, ditulis ke `results/relay_endpoints.json` (satu baris per relay).

### Fail-soft per service

Loop service tidak membatalkan seluruh akun. Kalau service ke-3 gagal (mis. TCP
proxy tidak muncul), service 1-2 tetap dikembalikan dengan `ok=True`; yang gagal
`ok=False` + `errors`, lalu lanjut ke service berikutnya.

### Auth

Pakai **cookie hasil farm** (`rw.session`, `rw.session.sig`, `rw.authenticated`,
`rw.authenticated.sig`), **bukan** token API. Endpoint:
`https://backboard.railway.com/graphql/internal`, dengan header
`Origin: https://railway.com` dan `Referer: https://railway.com/`. Cookie dibaca
dari `results/batch_*/accounts.json` (atau folder results yang di-set via
`--accounts-file`).

### Rate limit

**1 `projectCreate` per 30 detik per akun.** Melanggar batas ini memunculkan
error `creating projects too quickly`. Rate limit ini **hanya** untuk pembuatan
project, **tidak** berlaku untuk `serviceCreate` di project yang sudah ada.

Karena satu akun sekarang hanya membuat **satu** project, batas 30s itu dibayar
**sekali per akun**, gap `--gap` (default 35s) diterapkan **antar akun**, bukan
antar service. Jangan turunkan gap di bawah 30 detik.

### Bukti end-to-end

Relay `acela.proxy.rlwy.net:23117`:

```bash
curl -x http://user:pass@acela.proxy.rlwy.net:23117 https://api.ipify.org
# → 152.55.178.122   (IP lokal 111.94.74.38)

curl -x http://acela.proxy.rlwy.net:23117 https://api.ipify.org
# → 407 (tanpa auth ditolak)
```

Jadi egress relay benar-benar keluar dari IP Railway, dan endpoint memerlukan
auth.

### Pakai

```powershell
# via hub (default 5 relay/akun)
python -m jobs run railway-relay -- -n 1 -y

# paksa 1 relay/akun (kompatibel dengan perilaku lama)
python -m jobs run railway-relay -- -n 1 -y --per-account 1

# solo (dari folder farm)
..\..\.venv\Scripts\python.exe deploy_relay.py -n 1 -y --per-account 5

# cek rencana tanpa memanggil API Railway
..\..\.venv\Scripts\python.exe deploy_relay.py --dry-run -n 1 --per-account 5 -y
```

`--proxy-out <file>` menambahkan **setiap** endpoint relay sebagai URL proxy
telanjang (`http://user:pass@host:port`), satu baris per relay, supaya langsung
bisa jadi `proxy.txt` farm lain:

```powershell
..\..\.venv\Scripts\python.exe deploy_relay.py -n 1 -y --per-account 5 --proxy-out ..\outlook\proxy.txt
```

> Catatan: `deploy/endpoints.txt` di repo buff-relay **TIDAK** dipakai di jalur
> ini. File itu untuk `deploy.sh` berbasis token; jalur cookie di sini menulis
> `results/relay_endpoints.json` (dan opsional `--proxy-out`).
