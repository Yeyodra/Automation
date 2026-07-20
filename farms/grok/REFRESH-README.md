# Grok Token Refresh System

Sistem otomatis buat refresh access token akun grok-cli di 9router sebelum expired.

---

## Apa ini?

Akun grok-cli punya `access_token` yang expire tiap **6 jam**. Kalau token mati dan 9router belum tau, semua request ke 9router bakal return `401` sampai token di-refresh manual. Itu nyebelin.

Sistem ini solve masalah itu dengan cara refresh token **sebelum** expired, jadi 9router tetap jalan mulus tanpa downtime. Prosesnya otomatis via Windows Task Scheduler, jalan tiap 5 jam.

Yang terjadi di balik layar:
1. Script baca semua akun grok-cli dari SQLite DB milik 9router
2. Filter akun yang tokennya expired atau mau expired dalam 10 menit ke depan
3. Hit endpoint `https://auth.x.ai/oauth2/token` pakai `grant_type=refresh_token`
4. Kalau berhasil, token baru langsung ditulis balik ke DB
5. Kalau gagal (refresh_token juga expired), akun di-mark `unavailable`

---

## File-file yang Ada

```
grok-farm/
├── refresh-grok.js       # Core logic: baca DB, refresh token, update DB
├── refresh-grok.ps1      # Wrapper PowerShell: jalanin JS, logging, rotate log
├── setup-scheduler.ps1   # Setup Windows Task Scheduler (jalankan sekali)
└── logs/
    └── refresh.log       # Log output (auto-dibuat pas pertama kali jalan)
```

**`refresh-grok.js`**
Node.js script inti. Buka SQLite DB 9router, filter akun yang perlu di-refresh, hit token endpoint dalam batch 10 akun dengan delay 500ms antar request, tulis token baru ke DB. Exit code 0 kalau semua OK, 1 kalau ada yang gagal.

**`refresh-grok.ps1`**
PowerShell wrapper. Jalanin `refresh-grok.js` via node, stream output ke console sekaligus append ke `logs/refresh.log`. Rotate log otomatis kalau ukurannya lebih dari 1MB (keep 500 baris terakhir).

**`setup-scheduler.ps1`**
Setup satu kali. Daftarin Windows Task Scheduler task bernama `GrokTokenRefresh` yang jalan tiap 5 jam. `StartWhenAvailable=true` artinya kalau PC mati pas seharusnya jalan, task bakal catch-up waktu PC nyala lagi.

---

## Quick Start

Setup dari nol, belum pernah pasang sama sekali.

**Prasyarat:**
- Node.js sudah terinstall (`node --version` harus jalan)
- 9router sudah pernah jalan minimal sekali (biar DB-nya ada)
- PowerShell 5.1+ (bawaan Windows 10/11)

**Step 1: Verifikasi DB 9router ada**

```powershell
Test-Path "$env:APPDATA\9router\db\data.sqlite"
# Harus return True
```

Kalau False, jalanin 9router dulu sampai DB terbentuk.

**Step 2: Verifikasi node module better-sqlite3 ada**

```powershell
Test-Path "$env:APPDATA\9router\runtime\node_modules\better-sqlite3"
# Harus return True
```

**Step 3: Test manual refresh dulu**

```powershell
cd C:\Users\Nazril\Downloads\Compress\grok-farm\grok-farm
node refresh-grok.js
```

Lihat output. Kalau ada baris `[OK]` atau `Summary — Refreshed: X` berarti jalan normal.

**Step 4: Setup scheduler (sekali aja)**

```powershell
cd C:\Users\Nazril\Downloads\Compress\grok-farm\grok-farm
.\setup-scheduler.ps1
```

Selesai. Task `GrokTokenRefresh` sekarang jalan otomatis tiap 5 jam.

---

## Cara Pakai Manual

Kalau mau trigger refresh sekarang tanpa nunggu scheduler:

**Opsi A: Langsung via Node (output ke console, tidak ke log)**

```powershell
cd C:\Users\Nazril\Downloads\Compress\grok-farm\grok-farm
node refresh-grok.js
```

**Opsi B: Via PowerShell wrapper (output ke console DAN ke log)**

```powershell
cd C:\Users\Nazril\Downloads\Compress\grok-farm\grok-farm
.\refresh-grok.ps1
```

Output wrapper lebih informatif karena ada header `GrokTokenRefresh — START` dan timestamp dari PowerShell layer.

---

## Task Scheduler

### Setup (kalau belum)

```powershell
cd C:\Users\Nazril\Downloads\Compress\grok-farm\grok-farm
.\setup-scheduler.ps1
```

Script ini bisa dijalanin berulang kali, aman. Kalau task sudah ada, dia hapus dulu terus daftar ulang dengan config terbaru.

### Cek status task

```powershell
Get-ScheduledTask -TaskName 'GrokTokenRefresh' | Select-Object TaskName, State
```

Atau lihat last run time dan result-nya:

```powershell
Get-ScheduledTaskInfo -TaskName 'GrokTokenRefresh' | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult` = `0` artinya sukses. Selain 0 ada masalah.

### Trigger manual via scheduler

```powershell
Start-ScheduledTask -TaskName 'GrokTokenRefresh'
```

Task jalan di background. Cek hasilnya di log:

```powershell
Get-Content -Tail 30 "C:\Users\Nazril\Downloads\Compress\grok-farm\grok-farm\logs\refresh.log"
```

### Hapus task (kalau mau uninstall)

```powershell
Unregister-ScheduledTask -TaskName 'GrokTokenRefresh' -Confirm:$false
```

---

## Cara Baca Log

Log ada di:

```
grok-farm\logs\refresh.log
```

Baca 50 baris terakhir:

```powershell
Get-Content -Tail 50 ".\logs\refresh.log"
```

Atau live follow (mirip `tail -f`):

```powershell
Get-Content -Wait -Tail 20 ".\logs\refresh.log"
```

### Format output

Setiap baris ada timestamp ISO:

```
[2026-07-15T10:30:01.234Z] Total grok-cli accounts: 12 | To refresh: 4 | Skipped (valid): 8
[2026-07-15T10:30:01.456Z]   [TRY]  user@example.com — refreshing token...
[2026-07-15T10:30:02.001Z]   [OK]   user@example.com — refreshed, new expiresAt: 2026-07-15T16:30:02.000Z
[2026-07-15T10:30:02.510Z]   [TRY]  other@example.com — refreshing token...
[2026-07-15T10:30:03.100Z]   [DEAD] other@example.com — refresh_token expired/invalid (HTTP 401)
[2026-07-15T10:30:03.100Z] Summary — Refreshed: 1, Failed: 1, Skipped: 8
```

### Arti tiap status

| Status | Artinya |
|--------|---------|
| `[TRY]` | Lagi coba refresh akun ini |
| `[OK]` | Berhasil, token baru sudah disimpan ke DB |
| `[DEAD]` | refresh_token sudah expired (akun nganggur >30 hari). Akun di-mark `unavailable` |
| `[ERR]` | Gagal karena network/server error, bukan karena token mati. Akun juga di-mark `unavailable` |
| `[SKIP]` | Data akun rusak (JSON invalid di DB), dilewati |

Baris `Summary` di akhir kasih rekap cepat:
- **Refreshed**: berhasil dapat token baru
- **Failed**: gagal, akun sekarang unavailable
- **Skipped**: token masih valid, tidak perlu di-refresh

---

## Flow Diagram

```
Tiap 5 jam (Task Scheduler)
         │
         ▼
  refresh-grok.ps1
         │
         ▼
  refresh-grok.js
         │
         ▼
  Buka SQLite DB 9router
  (providerConnections WHERE provider='grok-cli')
         │
         ▼
  Filter akun yang expired
  atau expiring < 10 menit
         │
    ┌────┴────┐
    │         │
 Ada yang   Semua masih
 perlu      valid
 refresh    │
    │       ▼
    │    [SKIP semua]
    │    Summary: 0 refreshed
    │
    ▼
  Batch per 10 akun
  delay 500ms antar request
         │
         ▼
  POST https://auth.x.ai/oauth2/token
  grant_type=refresh_token
         │
    ┌────┴────────────┐
    │                 │
  HTTP 200          HTTP 401/403
  (token baru)      atau invalid_grant
    │                 │
    ▼                 ▼
  Update DB         Mark akun
  access_token      testStatus='unavailable'
  + expiresAt       catat lastError di DB
  baru              │
    │               ▼
    ▼             [DEAD] di log
  [OK] di log
         │
         ▼
  Summary log
  Refreshed: X, Failed: Y, Skipped: Z
         │
         ▼
  9router baca token fresh dari DB
  Request user tetap jalan normal
```

---

## Troubleshooting

### Semua akun di-refresh tapi 9router masih return 401

9router mungkin cache token di memory, bukan baca ulang dari DB tiap request. Restart 9router setelah refresh paksa:

```powershell
node refresh-grok.js
# tunggu selesai, lalu restart 9router
```

### Banyak akun [DEAD] sekaligus

Ini tanda akun-akun itu nganggur lebih dari 30 hari, refresh_token-nya sudah expired di sisi xAI. Tidak ada yang bisa dilakukan selain farm ulang.

Cek berapa akun yang masih aktif:

```powershell
node -e "
const db = require('$env:APPDATA\\9router\\runtime\\node_modules\\better-sqlite3')('$env:APPDATA\\9router\\db\\data.sqlite');
const rows = db.prepare(\"SELECT data FROM providerConnections WHERE provider='grok-cli'\").all();
const available = rows.filter(r => { try { return JSON.parse(r.data).testStatus !== 'unavailable'; } catch(e) { return false; } });
console.log('Available:', available.length, '/', rows.length);
db.close();
"
```

Kalau available tinggal sedikit, saatnya farm ulang pakai `farm.py` (lihat bagian bawah).

### Error: `Failed to load better-sqlite3`

9router belum pernah diinstall atau path-nya beda. Verifikasi:

```powershell
Test-Path "$env:APPDATA\9router\runtime\node_modules\better-sqlite3"
```

Kalau tidak ada, pastiin 9router sudah terinstall dengan benar.

### Error: `Failed to open SQLite DB`

DB tidak ada atau sedang di-lock oleh proses lain (misalnya 9router lagi nulis ke DB bersamaan).

```powershell
# Cek DB ada
Test-Path "$env:APPDATA\9router\db\data.sqlite"

# Cek ada proses yang lock DB (butuh sysinternals handle.exe atau tutup 9router dulu)
```

Kalau 9router lagi jalan dan DB terlocked, tunggu beberapa detik terus coba lagi. Biasanya tidak jadi masalah karena script pakai better-sqlite3 yang synchronous dan cepat.

### Log tidak terbuat / logs/ folder tidak ada

Folder `logs/` dibuat otomatis pas `refresh-grok.ps1` pertama kali jalan. Kalau jalan langsung via `node refresh-grok.js`, log tidak ditulis (output hanya ke console). Pakai wrapper `.ps1` kalau mau logging.

### Task Scheduler jalan tapi tidak ada efek

Cek `LastTaskResult`:

```powershell
Get-ScheduledTaskInfo -TaskName 'GrokTokenRefresh' | Select-Object LastRunTime, LastTaskResult
```

- `LastTaskResult = 0`: sukses, semua token valid (Skipped semua)
- `LastTaskResult = 1`: ada yang gagal, cek log
- `LastTaskResult = 2147942402`: path script tidak ditemukan, pastiin `refresh-grok.ps1` ada di folder yang sama

---

## Hubungan dengan `farm.py`

Dua tool ini punya peran berbeda:

| | `refresh-grok.js` | `farm.py` |
|-|-------------------|-----------|
| **Fungsi** | Perpanjang umur akun yang ada | Buat akun grok-cli baru dari nol |
| **Kapan dipakai** | Rutin, otomatis tiap 5 jam | Kalau akun sudah habis/dead |
| **Syarat** | Akun sudah ada di DB, refresh_token masih valid | Email domain/catch-all, bisa solve Turnstile |
| **Hasil** | Token diperpanjang 6 jam lagi | Akun baru dengan token fresh masuk ke DB via `inject-to-9router.js` |

**Pakai `refresh` cukup** kalau:
- Akun masih ada dan `[DEAD]` belum banyak
- Pool akun di 9router masih cukup buat handle load

**Waktunya farm ulang** kalau:
- Sebagian besar akun sudah `[DEAD]`
- Akun unavailable lebih banyak dari yang available
- 9router mulai sering return error karena pool tipis

Alur lengkap kalau pool sudah kritis:

```powershell
# 1. Farm akun baru
python farm.py -n 20 -c 2 -y

# 2. Inject ke 9router
node inject-to-9router.js

# 3. Verifikasi token fresh
node refresh-grok.js
```

Setelah inject, scheduler otomatis handle refresh ke depannya. Tidak perlu setup ulang.
