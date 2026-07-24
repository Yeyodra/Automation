# Env & Dependencies — Global vs Local

Hub **Automation** pakai model **satu pusat config + satu venv**, farm di bawah `farms/` hanya kode + data runtime.

```
Automation/                          ← HUB (global)
├── .env                             secrets & defaults bersama
├── .env.example
├── .venv/                           Python packages bersama
├── requirements.txt
├── core/env.py                      map shared → GROK_* / ENTER_*
├── core/warp.py                     WARP IP rotate (global)
├── jobs/                            runner (inject env + hub python)
└── farms/
    └── grok/                        ← LOCAL (kode farm + results)
        ├── farm.py
        ├── .env                     opsional, isi gap saja
        ├── results/ screenshots/
        └── requirements.txt         referensi; jangan pip di sini
```

---

## 1. Env — global vs local

### 1.1 Global (sumber utama)

| File | Peran |
|------|--------|
| `Automation/.env` | **Wajib diisi.** Secrets + default run. Gitignored. |
| `Automation/.env.example` | Template aman (tanpa secret). |

Isi shared keys **tanpa prefix** supaya bisa dipakai banyak farm:

| Shared key | Contoh | Dipakai untuk |
|------------|--------|----------------|
| `IMAP_USER` / `IMAP_PASS` | Gmail app password | OTP semua farm |
| `IMAP_HOST` / `IMAP_PORT` | `imap.gmail.com` / `993` | IMAP |
| `EMAIL_MODE` | `domain` \| `plus_trick` \| `gptmail` \| `exzork` | Mode email |
| `EMAIL_DOMAIN` | catch-all tanpa `@` | domain mode (IMAP); also default for exzork |
| `GMAIL_BASE` | base plus-trick | plus_trick (IMAP) |
| `GPTMAIL_API` | `https://mail.chatgpt.org.uk` | gptmail mode (no IMAP) |
| `GPTMAIL_DOMAIN` | (empty=auto) | pin domain; empty = pool + block/rotate |
| `EXZORK_API` | `https://mailer.exzork.me` | exzork mode (no IMAP) |
| `EXZORK_API_KEY` | `tm_...` | claim key (shown once) |
| `EXZORK_DOMAIN` | apex without `@` | defaults to `EMAIL_DOMAIN` |
| `EXZORK_WILDCARD` | `true` | `local@random.apex` (needs `*.apex` MX + claim) |
| `GPTMAIL_PREFIX` | optional | local-part prefix |
| `ACCOUNT_PASSWORD` | password akun farm | signup |
| `HEADLESS` | `true` / `false` | browser |
| `MAX_ACCOUNTS` / `CONCURRENT` / `SPAWN_DELAY` | run defaults | batch |
| `PROXY_*` | optional | proxy pool |
| `WARP_*` | optional | hub rotate IP |
| `OTP_TIMEOUT` / `ACCOUNT_TIMEOUT` / `UI` / `VERBOSE` | optional | farm timeouts / UI |

Override per-job (opsional, di **hub** `.env` juga):

```env
GROK_PASSWORD=...          # hanya grok, menang atas ACCOUNT_PASSWORD
GROK_EMAIL_DOMAIN=...
GROK_EMAIL_MODE=exzork     # or gptmail|domain; IMAP not required when exzork/gptmail
# EXZORK_API_KEY=tm_...
# EXZORK_DOMAIN=wowojomok.my.id
ENTER_GIFT_CODE=...        # nanti saat enter di-bundle
ENTER_EMAIL_MODE=gptmail
```

### 1.2 Local (opsional)

| File | Peran |
|------|--------|
| `farms/grok/.env` | **Hanya gap.** Tidak wajib. `load_dotenv(override=False)`. |
| `farms/grok/.env.example` | Dokumentasi env asli farm (prefix `GROK_*`). |

Jangan taruh secret “utama” di farm-local. Prefer hub.

### 1.3 Urutan load (saat `python -m jobs run …`)

```
1. Environment proses OS (sudah di-set)
2. Automation/.env          → raw keys (setdefault)
3. Prefix map               → IMAP_USER → GROK_IMAP_USER, dll.
4. farms/<job>/.env         → setdefault gap saja
5. farm.py load_dotenv      → override=False (hub tetap menang)
```

Implementasi: `core/env.py` → `build_job_env(prefix, farm_cwd)`.

### 1.4 Mapping shared → prefix

| Shared (hub) | → `GROK_*` | → `ENTER_*` (nanti) |
|--------------|------------|---------------------|
| `IMAP_USER` | `GROK_IMAP_USER` | `ENTER_IMAP_USER` |
| `IMAP_PASS` | `GROK_IMAP_PASS` | `ENTER_IMAP_PASS` |
| `EMAIL_DOMAIN` | `GROK_EMAIL_DOMAIN` | `ENTER_EMAIL_DOMAIN` |
| `ACCOUNT_PASSWORD` | `GROK_PASSWORD` | `ENTER_PASSWORD` |
| `HEADLESS` | `GROK_HEADLESS` | `ENTER_HEADLESS` |
| … | lihat `_SHARED_MAP` di `core/env.py` | sama pola |

### 1.5 Setup env cepat

```powershell
cd C:\Users\Nazril\Documents\Projek\Github\Automation
copy .env.example .env
# edit .env — IMAP, domain, password
```

Cek mapping tanpa run farm:

```powershell
.\.venv\Scripts\python.exe -c "from core.env import build_job_env; from pathlib import Path; e=build_job_env('GROK_', Path('farms/grok')); print(e.get('GROK_IMAP_USER'), e.get('GROK_EMAIL_DOMAIN'))"
```

---

## 2. Dependencies — global vs local

### 2.1 Global (install di sini)

| Lokasi | Isi |
|--------|-----|
| `Automation/requirements.txt` | pin packages untuk **semua** job |
| `Automation/.venv/` | interpreter yang dipakai runner |

Isi saat ini:

```
python-dotenv>=1.0.0
camoufox[geoip]==0.4.11    # + GeoLite2-City.mmdb di site-packages
playwright==1.58.0
textual / rich / typer     # hub TUI/CLI
```

Install / update:

```powershell
cd C:\Users\Nazril\Documents\Projek\Github\Automation
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\camoufox.exe fetch    # browser binary + update assets
```

Runner **selalu prefer** `Automation/.venv/Scripts/python.exe` (lihat `jobs/registry.py`).

### 2.2 Local (jangan pip di sini)

| Lokasi | Isi |
|--------|-----|
| `farms/grok/requirements.txt` | **Dokumentasi** dependency farm asli |
| `farms/grok/.venv/` | **Tidak dipakai** jika hub venv ada |

Jangan `pip install -r farms/grok/requirements.txt` kecuali debugging terpisah. Satu venv hub = reuse.

### 2.3 Camoufox: apa di project vs user cache

| Asset | Di mana | Scope |
|-------|---------|--------|
| pip package `camoufox` | `.venv\Lib\site-packages\camoufox\` | **per project (hub)** |
| GeoIP `GeoLite2-City.mmdb` | sama (dari extra `[geoip]`) | **per hub venv** |
| fonts / territory XML | sama | **per hub venv** |
| Browser `camoufox.exe` + engine | `%LOCALAPPDATA%\camoufox\camoufox\Cache\` | **per user Windows** (shared semua project) |

Cek status:

```powershell
.\.venv\Scripts\camoufox.exe version
.\.venv\Scripts\camoufox.exe path
# harus: Pip package v0.4.11 + Camoufox Up to date
# path: ...\AppData\Local\camoufox\camoufox\Cache
```

Kalau pindah PC: pip install di hub + `camoufox fetch` sekali (download browser ~ratusan MB).

### 2.4 WARP (global mid-batch — wave 1:1 with `-c`)

Bukan pip dependency. Butuh **Cloudflare WARP** + `warp-cli`.

| Layer | Module | Role |
|-------|--------|------|
| Primitif | `core/warp.py` | connect / rotate-keys / disc-conn / public IP |
| Policy | `core/warp_policy.py` | `normalize_every_n` (force everyN==c), helpers |
| Runner | `jobs/runner.py` | inject `WARP_EVERY_N` + `GROK_CONCURRENT` |
| Farm | e.g. `farms/grok` | after each **OK** → counter; hit N → drain peers → rotate → settle |
| Stop | `core/jobctl.py` | `stop_all()` kill process tree |

**Rules (current):**

| Setting | Meaning |
|---------|---------|
| `everyN = 0` | Auto IP off |
| `everyN > 0` | **Forced = `-c`** (1:1). e.g. c=3 everyN=2 → everyN becomes 3 |
| Counter | Hanya akun **OK** (FAIL tidak nambah) |
| Rotate | Setelah N OK: block spawn → wait in-flight peers → rotate → settle ~8s → resume |
| 1 process | Satu batch folder; **bukan** restart farm tiap N akun |

**Recommended presets:**

| Goal | `-c` | everyN |
|------|------|--------|
| Fast, no auto IP | 3 | **0** |
| Parallel + auto IP (wave) | 3 | **3** |
| Safe / slow | 1 | **1** (or 0) |

```powershell
python -m core.warp status
python -m jobs run grok --warp-every-n 3 -- -n 20 -c 3 -y
# hub: everyN 2 → 3 if you passed 2; plan everyN=3
# farm log: success 3/3 → drain then rotate… → settle → resume
python -m jobs stop   # global kill
```

Env: `WARP_EVERY_N` / `GROK_WARP_EVERY_N`, `WARP_SETTLE_AFTER` (default ~8), `WARP_CLI`, …

**9router inject:** farm sets `testStatus: "active"` (not untested). Bulk fix old rows:

```powershell
python scripts/9router_mark_active.py --provider grok-cli
```

---

## 3. Runtime data (local farm)

Tetap di folder farm (bukan hub root):

| Path | Isi |
|------|-----|
| `farms/grok/results/` | batch accounts, used_emails |
| `farms/grok/screenshots/` | fail dumps |
| `farms/grok/logs/` | refresh logs (jika dipakai) |
| `farms/grok/proxies.txt` | opsional, gitignored |

Gitignore hub sudah exclude `.env`, `.venv`, `farms/*/{results,screenshots,logs,.env}`.

---

## 4. Cara jalanin (ringkas)

```powershell
cd C:\Users\Nazril\Documents\Projek\Github\Automation

# 1x setup
# copy .env.example .env  → edit
# .\.venv\Scripts\pip install -r requirements.txt
# .\.venv\Scripts\camoufox.exe fetch

python -m jobs list
python -m jobs run grok --dry-run -- -n 1 -c 1 -y
python -m jobs run grok -- -n 1 -c 1 -y
python -m jobs run grok --warp-rotate -- -n 3 -c 1 -y
```

Args setelah `--` diteruskan ke `farm.py`. Flag hub (`--dry-run`, `--warp-*`) **sebelum** `--`.

---

## 5. Cheat sheet

| Butuh | Global | Local |
|-------|--------|-------|
| Secret IMAP / password | `Automation/.env` | jangan |
| Override domain cuma grok | `GROK_EMAIL_DOMAIN=` di hub `.env` | atau farm `.env` gap |
| pip packages | hub `.venv` + `requirements.txt` | jangan pip di farm |
| Browser Camoufox | `camoufox fetch` (user cache) | — |
| GeoIP mmdb | ikut `camoufox[geoip]` di hub venv | — |
| Results batch | — | `farms/grok/results/` |
| WARP rotate | `core.warp` + WARP app | — |

---

## 6. Nanti: farm baru (mis. enter)

1. Copy source → `farms/enter/`
2. Daftarkan di `jobs/registry.py` (`env_prefix="ENTER_"`)
3. Shared keys di hub `.env` otomatis jadi `ENTER_*`
4. Tambah key khusus enter di hub `.env` (`ENTER_GIFT_CODE`, …)
5. **Tidak** bikin venv baru — reuse hub `.venv` (tambah pin di `requirements.txt` hanya jika package baru)
