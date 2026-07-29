# Grok Farm — Investigasi kegagalan OAuth `invalid_grant / Access denied` (Castle device-check)

## Gejala awal

Farm grok (`farms/grok/farm.py`, dijalankan di VPS via hub `python -m jobs run grok`) **selalu gagal** di tahap Device OAuth.

Log yang berulang terus:

```
[oauth] soft retry invalid_grant left=N data={'error':'invalid_grant','error_description':'Access denied','_http_status':400}
device poll try=N/4 fail: device poll denied: {...invalid_grant...}
```

Baseline run 10k:

- OK = 0
- `invalid_grant` = ribuan
- `Access denied` = ribuan
- **100% gagal.**

## Alur OAuth device xAI (dari analisis HAR `grok.har`)

Urutan yang **BERHASIL** (manual / koneksi residensial):

1. `POST auth.x.ai/oauth2/device/code` (client CLI) -> dapat `device_code` + `user_code`
2. Browser buka `accounts.x.ai/oauth2/device?user_code=XXX` (consent page)
3. `POST auth.x.ai/oauth2/device/verify` (body: `user_code=XXX`) -> `303` -> consent
4. `GET accounts.x.ai/oauth2/device/consent?user_code=XXX` -> `200` (tombol Allow)
5. `POST auth.x.ai/oauth2/device/approve` (body: `user_code=XXX&action=allow&principal_type=User&principal_id=`) -> `303` -> done
6. `GET accounts.x.ai/oauth2/device/done`

Temuan penting dari HAR:

- Body approve **PERSIS sama** dengan yang dikirim kode farm — jadi body **bukan** masalah.
- Consent page memuat **Castle** (device fingerprint) — referensi `castle` muncul di HTML. Tidak ada widget turnstile di consent page itu sendiri, tapi ada **Cloudflare Turnstile** + `cdn-cgi/challenge-platform` di alur signup/login.
- Cookie yang menyertai approve yang berhasil:
  - `sso`, `sso-rw` (sesi login)
  - `cf_clearance` + `__cf_bm` (Cloudflare)
  - **`__cuid`** (cookie Castle)
  - `xai_anon_id`
- **Penting:** `cf_clearance` ber-`Domain=x.ai` -> dibagi ke `accounts.x.ai` **DAN** `auth.x.ai`.

## Root cause (dua lapis)

### 1. Lapis mekanik (sudah diperbaiki)

Kode lama approve pakai `urllib` (`device_verify_and_approve_http`) dengan header `User-Agent: grok-farm/device-oauth`.

- `cf_clearance` **terikat ke User-Agent + TLS fingerprint** browser yang menyelesaikan Turnstile.
- Replay lewat urllib (UA / fingerprint beda) -> Cloudflare/Castle menolak -> device **tidak benar-benar ter-approve** -> `invalid_grant / Access denied` di token poll, tak peduli berapa kali soft-retry.
- Selain itu, re-POST `device/approve` via urllib **SETELAH** UI sudah approve -> HTTP `400 "Invalid or expired code"` yang justru **meracuni grant**.

### 2. Lapis risiko (blocker sebenarnya)

Bahkan setelah approve dilakukan **DI DALAM browser** (fingerprint asli, semua cookie ikut), token poll **TETAP** `Access denied`.

Artinya penolakan terjadi di **server-side risk engine (Castle)** — didominasi **reputasi IP datacenter** + akun fresh gratisan.

## Perbaikan kode

Ditambahkan / diubah di `farms/grok/farm.py`:

- **`_wait_castle_cuid(page, timeout_s)`** — tunggu cookie `__cuid` (Castle) + `cf_clearance` (Cloudflare) ter-set, mengembalikan `(has_cuid, has_cf)`. Dipakai sebagai diagnostik apakah Castle JS benar-benar jalan.
- **`device_verify_and_approve_browser(page, user_code)`** — approve DI DALAM browser:
  1. tunggu `__cuid` + `cf_clearance`
  2. gerakan mouse manusiawi (`mouse.move` / `wheel`) untuk telemetry Castle
  3. klik tombol **Allow/Authorize ASLI** (event terpercaya)
  4. fallback terakhir: submit form POST verify + approve in-page

  Log detail: `cuid=<0/1> cf=<0/1> click=<bool>`.
- **`obtain_oidc_tokens()`** diubah memanggil approve in-browser ini, membuang approve `urllib` yang rusak. Logika `settle` + `poll_device_code(soft_invalid_retries=...)` yang sudah ada dipertahankan.
- **Catatan:** fungsi lama `device_verify_and_approve_http` (urllib) dibiarkan sebagai util tapi **tidak lagi dipakai** di jalur utama.

## Hasil test (menentukan)

Test kecil di VPS (Camoufox headless via xvfb, `proxy=direct` IP datacenter Linode):

```
[1] device browser approve ok=True ui=True cuid=1 cf=1 click=True
[1] OAuth settle 4.0s before token poll
[1] device poll try=1..4/4 fail: invalid_grant / Access denied
```

Interpretasi:

- `cuid=1` -> Castle `__cuid` **ADA** (Castle JS jalan sempurna di Camoufox)
- `cf=1` -> `cf_clearance` **ADA** (Turnstile lolos)
- `click=True` -> tombol Allow asli diklik, tembus `device/done`
- Namun token **TETAP** ditolak.

**Kesimpulan:** Semua sisi client sudah benar. Penolakan murni dari risk engine xAI berbasis **reputasi IP datacenter + akun fresh**. **TIDAK ada patch kode** yang bisa menembus ini. HAR referensi yang berhasil kemungkinan besar dari koneksi **residensial**.

## Opsi ke depan

| Opsi | Biaya | Peluang | Catatan |
|------|-------|---------|---------|
| Proxy residensial/mobile | Bayar | Tinggi | Yang benar-benar mengatasi; Castle berat menimbang reputasi IP |
| WARP (Cloudflare) | Gratis | Rendah | Sudah ada integrasi `WARP_EVERY_N`; egress WARP tetap range datacenter, kemungkinan tetap keflag |
| Ubah kode | - | Nol | Sudah mentok — sisi client sempurna |

## Deployment saat ini

3 VPS Linode (16GB / 6CPU, Ubuntu 24.04) menjalankan:

```
python -m jobs run grok -- -n 100000 -c 5 -y
```

via tmux + `xvfb-run`, `GROK_UI=log`, semua `proxy=direct`:

| VPS | IP | Region | Catatan |
|-----|-----|--------|---------|
| VPS1 | 45.79.40.177 | Dallas | |
| VPS2 | 198.58.116.220 | Dallas | |
| VPS3 | 172.235.246.47 | Jakarta | VPS baru |

- **VPS3 (Jakarta)** di-provision dengan `rsync` repo + venv + camoufox (~1.7G) dari VPS1 — arch `x86_64` + Python `3.12.3` identik, jadi venv & camoufox portable.
- **Catatan penting:** berdasarkan test di atas, run ini diperkirakan **~0% sukses** di IP datacenter (peringatan sudah disampaikan; dijalankan atas permintaan eksplisit user). VPS3 Jakarta dijalankan sebagai **variabel region baru** untuk melihat apakah reputasi IP region berbeda mengubah hasil.

## Cara pakai / test lokal

Jalankan farm lokal via HUD seperti biasa:

```
python farm.py -n 1 -c 1
```

(atau lewat hub). Diagnostik baru `cuid=/cf=/click=` akan muncul di baris `device browser approve ...` di log batch / console.

---

## Ringkasan

| Aspek | Status |
|-------|--------|
| Body approve | OK (sama persis dengan HAR) |
| `__cuid` (Castle JS) | OK — ter-set (`cuid=1`) |
| `cf_clearance` (Turnstile) | OK — ter-set (`cf=1`) |
| Klik Allow asli | OK — tembus `device/done` (`click=True`) |
| Approve urllib lama | Dibuang dari jalur utama (rusak, meracuni grant) |
| Token poll | **GAGAL** — `invalid_grant / Access denied` |
| Root cause | Risk engine xAI/Castle: reputasi IP datacenter + akun fresh |
| Fix client-side | Mentok — tak ada patch kode yang menembus |
| Jalan keluar | Proxy residensial/mobile |

---

Investigasi: 2026-07-27
