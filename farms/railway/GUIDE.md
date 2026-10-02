# Panduan Railway Farm + Relay Egress

Panduan operasional lengkap: dari nol sampai endpoint proxy siap dipakai farm lain.

Dokumen ini **step-by-step** (lakukan ini, lalu ini). Untuk detail teknis/reverse-engineering (analisis HAR, daftar mutation GraphQL), lihat [`README.md`](./README.md).

---

## 0. Ini apa?

Ada **dua hal** di farm ini, dan keduanya dipakai berurutan:

| Job | File | Fungsinya |
|---|---|---|
| `railway` | `farm.py` | Bikin **akun Railway baru** lewat Google OAuth → dapat **cookie sesi** |
| `railway-relay` | `deploy_relay.py` | Ubah tiap akun jadi **relay proxy** di Railway → dapat **endpoint IP** |

**Kenapa ini ada.** Farm lain (Enter, Outlook, Grok, …) kena rate limit kalau semua akun keluar dari satu IP yang sama. Railway memberi **egress IP yang berbeda untuk setiap service**, jadi tiap akun Railway bisa jadi beberapa endpoint proxy yang bisa dipakai bergantian.

```
┌─────────────────┐     ┌──────────────────────┐     ┌────────────────────┐
│ farm railway    │     │ railway-relay        │     │ farm lain          │
│ (Google OAuth)  │ ──► │ 1 akun → 1 project   │ ──► │ proxy.txt          │
│ → cookie sesi   │     │ → 5 service          │     │ (Enter, Outlook…)  │
│                 │     │ → 5 TCP proxy        │     │                    │
│                 │     │ → 5 IP egress BEDA   │     │                    │
└─────────────────┘     └──────────────────────┘     └────────────────────┘
```

**Hasil terukur (verified):** 1 akun → 1 project → 5 relay → **5 IP unik**, selesai dalam **~41 detik**.

| relay | endpoint | egress IP |
|---|---|---|
| 1 | `maglev.proxy.rlwy.net:37884` | 152.55.177.203 |
| 2 | `iriguchi.proxy.rlwy.net:56047` | 152.55.178.101 |
| 3 | `roundhouse.proxy.rlwy.net:12747` | 152.55.177.199 |
| 4 | `iriguchi.proxy.rlwy.net:35128` | 152.55.177.79 |
| 5 | `trolley.proxy.rlwy.net:18983` | 152.55.177.164 |

(IP lokal mesin saat uji: `111.94.74.38` berbeda dari semuanya.)

> **"Register" = login OAuth pertama kali.** Railway tidak punya form register email/password. Akun Railway dibuat saat Google OAuth pertama berhasil. Tidak ada langkah verifikasi email terpisah.

---

## 1. Prasyarat

| Kebutuhan | Cara cek |
|---|---|
| Hub venv + dependensi | `ls .venv/Scripts/python.exe` |
| Camoufox browser (untuk farm OAuth) | `ls .venv/Scripts/camoufox.exe` |
| Akun Google (format `email\|password`) | siapkan daftarnya |
| Akses repo relay publik | default `NetroIndonesia/buff-relay` (harus publik) |
| (Opsional) proxy pool | supaya farm OAuth tidak keluar dari IP lokal |

Kalau `camoufox.exe` belum ada: `.venv/Scripts/camoufox.exe fetch`

---

## 2. Setup awal (sekali saja)

Semua perintah dijalankan dari root hub:

```bash
cd /c/Users/Novella/Documents/Github/Automation
```

**(a) Siapkan pool akun Google**

```bash
cp farms/railway/google_accounts.txt.example farms/railway/google_accounts.txt
```

Lalu isi `farms/railway/google_accounts.txt`, satu akun per baris:

```
user1@yourdomain.com|YourPassword123
user2@yourdomain.com|AnotherPass456
```

Format yang diterima: `email|password`, `email:password`, atau `email<TAB>password`. Baris `#` diabaikan. File ini **gitignored**.

**(b) (Opsional) Siapkan proxy pool**

```bash
cp farms/railway/proxy.txt.example farms/railway/proxy.txt
```

Isi satu proxy per baris (`socks5://127.0.0.1:40001`). Kalau file ini tidak ada, semua akun keluar dari IP lokal. Satu akun = satu egress sticky (browser OAuth + semua GraphQL post-auth lewat proxy yang sama).

**(c) Cek job terdaftar**

```bash
.venv/Scripts/python.exe -m jobs list
```

Harus muncul `railway` dan `railway-relay`.

---

## 3. STEP 1, Farm akun Railway (dapat cookie)

Job: `railway`

```bash
.venv/Scripts/python.exe -m jobs run railway -- -n 1 -c 1 -y
```

Ganti `-n` sesuai jumlah akun. Untuk beberapa akun paralel: `-n 5 -c 3 -y`.

**Yang terjadi:** browser Camoufox membuka `backboard.railway.com/login/google`, menyelesaikan Google OAuth (email → password → consent), lalu menjalankan onboarding lewat GraphQL (`userTermsUpdate`, `fairUseAgree`).

**Output yang diharapkan:**

```
[13:14:19] [1] navigate  backboard.railway.com/login/google (OAuth entry)  mala1@...
[13:14:21] [1] google_authorize  at accounts.google.com  mala1@...
[13:14:24] [1] google_password  entering password  mala1@...
[13:14:29] [1] OK  ws=0955287e-... plan=HOBBY user=097a7cf9-...  mala1@...
[13:14:29] [0] DONE  ok=1 fail=0
```

**Hasil disimpan di:**

| File | Isi |
|---|---|
| `farms/railway/results/batch_<stamp>_<hex>/accounts.json` | data lengkap per akun (termasuk `cookie`) |
| `farms/railway/results/batch_<stamp>_<hex>/credentials.txt` | baris `\|`-separated |
| `farms/railway/results/all_credentials.txt` | gabungan lintas batch |
| `farms/railway/results/used_google.txt` | email terpakai (di-skip run berikutnya) |
| `farms/railway/results/google_dead.txt` | email ditolak Google |

**Cek cepat:**

```bash
cat farms/railway/results/used_google.txt
```

**Catatan penting:**

- Akun yang sudah ada di `used_google.txt` akan **di-skip** di run berikutnya. Jadi `-n 5` dengan pool 5 akun yang sudah terpakai akan gagal dengan pesan jelas, bukan mengulang akun yang sama.
- `access_denied` = Google menolak akun (akun baru/berisiko). Akun ditandai **dead**, **tidak** di-retry. Ganti akun, jangan retry buta.
- Kalau muncul challenge 2FA Google (`challenge/ipp|totp|az|dp|iap|sk|pk|recaptcha|ootp|selection|webauthn`) → dianggap `fail`, bukan retry.

---

## 4. STEP 2, Deploy relay (dapat endpoint IP)

Job: `railway-relay`

**Syarat:** Step 1 sudah jalan (ada cookie di `results/batch_*/accounts.json`).

```bash
.venv/Scripts/python.exe -m jobs run railway-relay -- -n 1 --per-account 5 -y
```

**Yang terjadi:** untuk tiap akun, modul membuat **1 project** lalu **N service** (buff-relay dari repo GitHub), tiap service dibuka TCP proxy publik dengan auth sendiri.

**Output yang diharapkan:**

```
[16:07:36] [1] project   created d5e3910e-... env=d01d375a-... (services=1..5)
[16:07:47] [1] OK  [1/5] http://relay:***@maglev.proxy.rlwy.net:37884 status=BUILDING
[16:07:54] [1] OK  [2/5] http://relay:***@iriguchi.proxy.rlwy.net:56047 status=QUEUED
[16:08:02] [1] OK  [3/5] http://relay:***@roundhouse.proxy.rlwy.net:12747 status=QUEUED
[16:08:09] [1] OK  [4/5] http://relay:***@iriguchi.proxy.rlwy.net:35128 status=QUEUED
[16:08:17] [1] OK  [5/5] http://relay:***@trolley.proxy.rlwy.net:18983 status=QUEUED
[16:08:17] [0] DONE  ok=5 fail=0
```

**Hasil disimpan di:**

| File | Isi |
|---|---|
| `farms/railway/results/relay_endpoints.json` | array objek per relay (project/service id, domain, port, endpoint_url, status) |
| `farms/railway/results/relay_endpoints.txt` | `# project_id\|service_id\|domain:port\|proxy_url\|deployment_status\|created_at` |

**Tunggu deploy selesai, lalu uji endpoint.** Status di log (`BUILDING`/`QUEUED`) artinya Railway masih build; tunggu **60-90 detik** sebelum relay bisa dipakai:

```bash
curl -x "http://relay:PASSWORD@maglev.proxy.rlwy.net:37884" https://api.ipify.org
# → 152.55.177.203   (IP Railway, beda dari IP lokal)
```

Password relay ada di `relay_endpoints.txt` (field `proxy_url`). Setiap service punya password **sendiri** (random) kalau `RAILWAY_RELAY_AUTH_PASS` tidak di-pin.

**Bukti auth bekerja:**

```bash
curl -x "http://maglev.proxy.rlwy.net:37884" https://api.ipify.org
# → curl: (7) CONNECT tunnel failed, response 407    ← benar, tanpa auth ditolak
```

**Cek semua endpoint sekaligus:**

```bash
cd /c/Users/Novella/Documents/Github/Automation
.venv/Scripts/python.exe - <<'PY'
import json, subprocess, urllib.parse
for r in json.load(open('farms/railway/results/relay_endpoints.json')):
    u = urllib.parse.urlparse(r['endpoint_url'])
    p = f"http://{u.username}:{u.password}@{u.hostname}:{u.port}"
    out = subprocess.run(["curl","-sS","--max-time","30","-x",p,"https://api.ipify.org"],
                         capture_output=True, text=True).stdout.strip()
    print(f"relay{r['index']}  {u.hostname}:{u.port}  ->  {out}")
PY
```

---

## 5. STEP 3, Pakai endpoint di farm lain

Ada dua cara.

**(a) Tulis langsung ke `proxy.txt` farm lain** (paling praktis):

```bash
.venv/Scripts/python.exe -m jobs run railway-relay -- -n 1 --per-account 5 -y --proxy-out farms/enter-v3-google/proxy.txt
```

`--proxy-out` menambahkan tiap endpoint sebagai URL telanjang `http://user:pass@host:port`, satu baris per relay. Farm lain yang sudah punya abstraksi proxy pool (`PROXY_FILE`) langsung bisa memakainya, **tanpa perubahan kode**.

**(b) Ambil manual dari `relay_endpoints.txt`:**

```bash
cut -d'|' -f4 farms/railway/results/relay_endpoints.txt
```

lalu tempel baris yang diinginkan ke `proxy.txt` farm tujuan.

**Catatan:** farm yang memakai proxy ini harus memperlakukan **1 akun = 1 endpoint sticky** (browser + semua request keluar dari egress yang sama), supaya konsisten.

---

## 6. Referensi command

### Farm akun (`railway`)

| Command | Arti |
|---|---|
| `python -m jobs run railway -- -n 1 -c 1 -y` | 1 akun |
| `python -m jobs run railway -- -n 10 -c 3 -y` | 10 akun, 3 paralel |
| `python -m jobs run railway -- --dry-run -- -n 5 -c 1 -y` | cek rencana tanpa jalan |
| `python -m jobs run railway --warp-every-n 3 -- -n 9 -c 3 -y` | WARP rotate tiap 3 OK |

### Relay (`railway-relay`)

| Command | Arti |
|---|---|
| `python -m jobs run railway-relay -- -n 1 -y` | 1 akun → 5 relay (default) |
| `python -m jobs run railway-relay -- -n 1 --per-account 1 -y` | 1 akun → 1 relay |
| `python -m jobs run railway-relay -- -n 3 -y` | 3 akun → sampai 15 relay |
| `python -m jobs run railway-relay -- --dry-run -n 1 -y` | cek rencana tanpa API call |
| `python -m jobs run railway-relay -- -n 1 -y --proxy-out farms/X/proxy.txt` | sekalian tulis proxy pool |
| `python -m jobs run railway-relay -- -n 1 -y --gap 40` | jeda antar akun 40s |

### Solo (tanpa hub, tetap pakai venv hub)

```bash
cd farms/railway
../../.venv/Scripts/python.exe farm.py -n 1 -c 1 -y
../../.venv/Scripts/python.exe deploy_relay.py -n 1 --per-account 5 -y
../../.venv/Scripts/python.exe deploy_relay.py --help
```

### Stop farm yang sedang jalan

```bash
.venv/Scripts/python.exe -m jobs stop
```

---

## 7. Batas, biaya, dan rate limit

| Batas | Nilai | Catatan |
|---|---|---|
| Project per workspace | **2** (HOBBY) | 1 akun = 1 project, jadi sisa 1 project lagi |
| Service per project | **5** | `--per-account` di-clamp ke 1..5 |
| Rate limit bikin project | **1 per 30 detik per akun** | error `creating projects too quickly` |
| Kredit trial | **$5 / 30 hari** | tiap relay yang hidup memakan biaya jalan |
| Relay per akun | **sampai 5** | 5 service × 1 IP masing-masing |

**Poin penting soal rate limit:** batas 30 detik itu hanya untuk `projectCreate`, **tidak** berlaku untuk `serviceCreate`. Karena sekarang satu akun hanya membuat **satu** project, batas itu dibayar **sekali per akun**, bukan per relay. Itu sebabnya 5 relay selesai ~41 detik, bukan 5 × 30 detik.

Karena itu `--gap` (default **35s**) diterapkan **antar akun**. **Jangan turunkan di bawah 30 detik.**

**Biaya:** setiap relay = 1 service yang jalan 24/7. Relay yang tidak dipakai sebaiknya dihapus, karena kredit trial terkuras. Cara hapus: hapus project-nya (semua service di dalamnya ikut terhapus).

---

## 8. Troubleshooting

| Gejala | Penyebab | Solusi |
|---|---|---|
| `All N accounts already used` | semua email ada di `used_google.txt` | tambah akun baru ke pool |
| `no workspace id (cookie invalid or expired?)` | cookie sudah kadaluarsa | jalankan `railway` (farm) lagi untuk akun itu |
| `project create failed: creating projects too quickly` | kena rate limit 30s | tunggu, atau naikkan `--gap` |
| `access_denied` saat farm | Google menolak akun | akun ditandai dead; ganti akun |
| Relay `407` padahal password benar | belum selesai build | tunggu 60-90s lagi |
| Relay `407` setelah build selesai | password salah / terbalik | ambil ulang dari `relay_endpoints.txt` |
| `no tcp proxy endpoint` | TCP proxy belum muncul | cek ulang beberapa saat; fail-soft, service lain tetap jalan |
| `Cannot query field "networking"` | salah nama root field | gunakan `tcpProxies(environmentId:, serviceId:)` (sudah benar di modul) |
| Farm gagal di step `google_password` | akun butuh verifikasi tambahan | pakai akun yang sudah "warm" |
| `camoufox` tidak ditemukan | browser belum di-fetch | `.venv/Scripts/camoufox.exe fetch` |

**Fail-soft:** kalau satu service gagal, service lain dalam akun yang sama tetap tersimpan (`ok=true`). Cek `errors` di `relay_endpoints.json` untuk yang gagal.

---

## 9. Verifikasi cepat (sanity check)

```bash
cd /c/Users/Novella/Documents/Github/Automation

# 1. job terdaftar?
.venv/Scripts/python.exe -m jobs list

# 2. syntax kedua modul
.venv/Scripts/python.exe -c "import ast;ast.parse(open('farms/railway/farm.py',encoding='utf-8').read());print('farm OK')"
.venv/Scripts/python.exe -c "import ast;ast.parse(open('farms/railway/deploy_relay.py',encoding='utf-8').read());print('relay OK')"

# 3. rencana tanpa API call
.venv/Scripts/python.exe farms/railway/deploy_relay.py --dry-run -n 1 --per-account 5 -y

# 4. hasil yang sudah ada
cat farms/railway/results/used_google.txt
cat farms/railway/results/relay_endpoints.txt
```

---

## 10. Alur lengkap dari nol (ringkas)

```bash
cd /c/Users/Novella/Documents/Github/Automation

# 1) siapkan pool akun
cp farms/railway/google_accounts.txt.example farms/railway/google_accounts.txt
nano farms/railway/google_accounts.txt          # isi email|password

# 2) farm akun Railway (dapat cookie)
.venv/Scripts/python.exe -m jobs run railway -- -n 1 -c 1 -y

# 3) deploy relay (dapat 5 endpoint IP)
.venv/Scripts/python.exe -m jobs run railway-relay -- -n 1 --per-account 5 -y

# 4) tunggu build, lalu uji
sleep 90
cut -d'|' -f4 farms/railway/results/relay_endpoints.txt | while read p; do
  echo -n "$p -> "; curl -sS --max-time 30 -x "$p" https://api.ipify.org; echo
done

# 5) pakai di farm lain
.venv/Scripts/python.exe -m jobs run railway-relay -- -n 1 --per-account 5 -y \
  --proxy-out farms/enter-v3-google/proxy.txt
```

Selesai. Sekarang farm lain punya pool proxy dengan IP egress Railway yang berbeda-beda.
