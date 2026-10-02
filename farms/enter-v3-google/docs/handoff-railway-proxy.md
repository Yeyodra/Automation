# Handoff — enter-v3-google × Railway proxy (2026-10-02)

Catatan perjalanan: dari "farm ini jalan pakai WARP" sampai "farm ini jalan pakai
relay Railway sebagai egress". Isinya kronologi trial-error + semua patch yang
dipasang, plus bukti sukses pertama.

Baca ini kalau: farm ini tiba-tiba `access_denied`, macet di consent, atau
`gateway session` 404/204.

---

## 0. Ringkasan hasil

**Sukses pertama lewat proxy Railway** (run15, 2026-10-02):

```
[18:32:54] [1] authenticated gateway session established
[18:33:08] [1] CREDITS  bonus landed (total=200.0)
[18:33:08] [SAVE] auroramarinaefrdvm8927@8edu.org ws=10000530246 key=ek_9cd5b114d...
[18:33:09] [CARTETHYIA] injected -> provider_accounts
[18:33:09] [1] OK  ok
```

| Item | Nilai |
|---|---|
| Akun | `auroramarinaefrdvm8927@8edu.org` |
| Egress IP | `152.55.176.43` (relay `tokaido`, Railway) |
| API key | `ek_9cd5b114d...<REDACTED>` |
| Workspace | `10000530246` |
| Referral | `SY2V0NYTVG` → `credits_total=200.0`, `invitee_bonus_landed=true` |
| Durasi | 108s |

Total patch: **6 bug** — semuanya di `farms/enter-v3-google/farm.py`.

---

## 1. Kenapa pindah ke Railway

`proxy.txt` sebelumnya menunjuk 5 node multi-warp (`socks5://127.0.0.1:40001..40005`).
Semua **mati** (tidak ada yang listen), sehingga farm keluar dari IP lokal dan kena
rate-limit Auth0 setelah beberapa akun.

Railway dipakai sebagai ganti: 1 akun Railway → 1 project → 5 service `buff-relay`
→ 5 TCP proxy → **5 egress IP berbeda**. Deploy-nya lewat `farms/railway/deploy_relay.py`
(job `railway-relay`), lalu `--proxy-out` menulis endpoint langsung ke `proxy.txt` farm ini.

Format endpoint:
```
http://relay:PASSWORD@<host>.proxy.rlwy.net:<port>
```

Backup pool WARP lama disimpan di `proxy.txt.bak-warp`.

---

## 2. Kronologi trial-error (6 bug)

Setiap run berhenti di titik berbeda. Urutan di bawah = urutan kemunculan.

### Bug 1 — `_probe_exit_ip` memaksa SOCKS5, relay Railway dianggap mati

**Gejala.** Di awal batch:
```
[PROXY] h:relay:...@hopper.proxy.rlwy.net:19415 -> unreachable
[PROXY] pool=5 reachable=0 unique_exit_ips=0
[1] egress ip probe failed (proxy may be down)
```
Padahal `curl -x <proxy sama> https://api.ipify.org` mengembalikan IP.

**Akar.** `_probe_exit_ip()` hardcode `socks.socksocket()` + `set_proxy(socks.SOCKS5, ...)`.
Relay Railway bicara **HTTP CONNECT** — bukan SOCKS5.

**Fix.** Fungsi di-dispatch berdasarkan skema URL:
- `socks5/socks5h/socks4` → PySocks (username/password diteruskan)
- `http/https` → socket mentah + `CONNECT host:443` + header `Proxy-Authorization: Basic`

**Hasil.** `pool=5 reachable=5 unique_exit_ips=5`.

Catatan: ini **hanya memperbaiki pelaporan**. Browser sudah benar sejak awal
(`_parse_proxy` mempertahankan skema `http://`), jadi farm tetap jalan walau probe salah.

### Bug 2 — landing Cloudflare tidak ditunggu (IP datacenter butuh ~15-25s)

**Gejala.** Setelah `LAND g_landing`, farm langsung `extract_fpjs` lalu:
```
RuntimeError: FPJS unavailable - aborting (random ids => access_denied)
```
Hanya ~16 detik dari landing ke `browser closed`. Google OAuth belum pernah dimulai.

**Akar.** `goto_with_retry` berhenti di `wait_until="commit"` (respons pertama), lalu
farm hanya `sleep(2.5)`. Dengan egress IP datacenter (Railway), Cloudflare
"Just a moment..." menahan halaman ~15-25 detik, jadi app JS belum jalan dan
`fpjs.converge.ai` tidak pernah di-load.

Diukur langsung: IP lokal lolos **instan** — relay Railway butuh **~15-25s**.

**Fix.** Fungsi baru `_wait_landing_ready(page, attempt, max_wait=45)`:
poll `page.title()` sampai bukan "Just a moment..."/"Attention Required",
klik `try_click_turnstile` sekali kalau widget managed muncul, lalu lanjut.
Dipanggil di `_goto_login_identifier` sebelum `extract_fpjs`.

**Hasil.** `landing ready (title='Enter Pro: Build Apps, Websites & Agents')` → FPJS berhasil.

### Bug 3 — consent loop tak berujung karena label Vietnam

**Gejala.**
```
[17:21:16] google oauth consent
[17:21:18] google oauth consent
... (puluhan kali, sampai account timeout)
```

**Akar.** Google melokalkan halaman consent berdasarkan region egress IP. Dengan
relay Railway (US/SJC), halaman tampil **bahasa Vietnam**: tombol accept = `Tiếp tục`,
cancel = `Huỷ`. `_CONSENT_SEL` hanya punya label EN/ID (`Continue`, `Allow`,
`Lanjutkan`, `Izinkan`, `Accept`), jadi tidak ada yang cocok.

Lebih buruk: handler lama `return "consent"` **tanpa cek apakah klik berhasil**,
sehingga caller hanya sleep lalu masuk lagi → loop tak berujung.

**Fix (dua lapis).**
1. Fallback **independen label**: pilih tombol afirmatif secara struktural
   buang tombol yang teksnya termasuk daftar cancel (`cancel`, `hủy`, `huỷ`,
   `batal`, `отмена`, `取消`, `ยกเลิก`), lalu ambil tombol **paling bawah lalu
   paling kanan** (posisi tombol accept di layar ini).
2. Kalau tetap tidak ada yang bisa diklik → `return "wait"` (bukan `"consent"`),
   supaya deadline luar yang mengakhiri — bukan spin.

Penting: filter juga **membuang tombol tanpa teks**, karena dropdown bahasa
(teks kosong, posisi di bawah footer) ikut ke-match dan sempat terpilih
menyebabkan klik ke elemen yang salah.

**Hasil.** `google consent: clicked affirmative button (label-independent)`.

### Bug 4 — session 404: polling di host yang salah

**Gejala.**
```
[18:12:14] [1] waiting session (status=404) url=https://auth.converge.ai/login/callback
... berulang sampai habis
[1] FAIL  RuntimeError: gateway session returned HTTP 204
```

**Akar.** Loop tunggu callback keluar prematur karena kondisi
`"/auth/callback" in u or "code=" in u or _on_enter_host(u)`. URL perantara
`auth.converge.ai/login/callback` mengandung **`code=`** — jadi loop berhenti di
**host Auth0**, bukan host app. `fetch('/auth/session')` lalu dijalankan relatif
terhadap origin yang salah → 404 selamanya.

**Fix.** Hanya `_on_enter_host(u)` (host `enter.converge.ai`) yang dianggap selesai.
Setelah itu, poll `/auth/session` sampai status **200** (bukan sekali tembak),
dengan fallback terakhir: kalau belum di app host, navigasi ke `APP_HOST/`.

**Hasil.** `authenticated gateway session established`.

### Bug 5 — akun "already signed up" tidak ditandai used → diulang terus

**Gejala.** Run berikutnya memakai akun yang **sama** berulang kali:
```
[1] FAIL  RuntimeError: gateway session is not a new user
```
Akun tetap tidak masuk `used_google.txt`, jadi selalu terpilih lagi.

**Akar.** `_parse_gateway_session` menolak `user.isNewUser != True`. Error ini
tidak cocok `_is_access_denied` (bukan access_denied) dan tidak menandai apa pun,
sehingga akun diulang tanpa henti.

**Fix.** Di handler error: kalau pesan mengandung `not a new user`, tandai akun
**used** (bukan dead — akun Google-nya sehat, cuma sudah pernah signup Enter):
```python
elif "not a new user" in msg.lower():
    emit_failed(attempt, "already signed up to Enter (retiring account)", email)
    _persist_used_google(email)
```

**Hasil.** Akun pensiun dengan pesan jelas, tidak diulang.

### Bug 6 — consent diklik DUA KALI → state Auth0 sekali-pakai terbakar

**Gejala.** Callback lolos, tapi berakhir di halaman:
```
auth.converge.ai/login/callback?code=4/0...
"Oops! something went wrong"
"...we couldn't find your session. Try logging in again from the application"
```
atau `access_denied at callback`.

Log menunjukkan klik consent **selalu 2×** di semua run:
```
[18:27:13] google consent: clicked affirmative button
[18:27:15] google oauth consent
[18:27:16] google oauth consent      <- klik kedua (state sudah dipakai)
```

**Akar.** Handler consent dipanggil lagi pada iterasi berikutnya (URL belum berubah
karena redirect sedang berjalan), jadi tombol diklik dua kali. Auth0 state
**sekali pakai**: klik kedua menghancurkan transaksi yang sama → "couldn't find
your session" / `access_denied`.

**Fix.** Tambah parameter `consent_done` ke `_handle_google_step`; loop menandainya
`True` setelah klik pertama, dan handler langsung `return "wait"` (tanpa klik)
kalau sudah pernah klik.

**Hasil.** `consent_click=1` (dari sebelumnya selalu 2) → run15 **sukses penuh**.

---

## 3. Faktor non-kode: reputasi IP

Selain 6 bug di atas, ada satu faktor yang **bukan bug farm**:

| IP relay | Akun dicoba | Hasil |
|---|---|---|
| `152.55.176.47` (`hopper`) | 4 akun berturut-turut | semua `access_denied` setelah consent |
| `152.55.176.43` (`tokaido`) | 1 akun | **OK** |

Penyebab: farm selalu memulai dari **relay pertama** di `proxy.txt` (`_proxy_idx`
reset tiap proses) — jadi satu IP dipakai berulang untuk banyak akun. Setelah
beberapa penolakan, reputasi IP itu rusak di sisi Enter/Auth0.

**Praktik yang terbukti:** kalau satu relay mulai `access_denied`, **hapus baris
itu** dari `proxy.txt` supaya akun berikutnya pindah IP. Jangan retry akun baru
di IP yang sudah menolak.

Catatan: `access_denied` yang datang **tepat setelah consent** (bukan di Google
sign-in) biasanya sinyal reputasi IP / risk rule Enter — bukan akun bermasalah.

---

## 4. Cara pakai (perintah yang terbukti)

```bash
cd /c/Users/Novella/Documents/Github/Automation

# pool akun: satu baris email|password
# proxy.txt: satu http://relay:PASS@host:port per baris (endpoint Railway)

# jalankan (headed lebih mudah di-debug)
ENTER_GIFT_CODE=SY2V0NYTVG ENTER_INVITER="Prayoga Bandi" ENTER_INVITEE_REWARD=100 \
  .venv/Scripts/python.exe -m jobs run enter-v3-google -- --headed -n 1 -c 1 -y

# headless
ENTER_GIFT_CODE=SY2V0NYTVG ENTER_INVITER="Prayoga Bandi" ENTER_INVITEE_REWARD=100 \
  .venv/Scripts/python.exe -m jobs run enter-v3-google -- -n 1 -c 1 -y
```

Catatan referral: **tidak ada prompt interaktif** di CLI farm ini. Field `Gift`
hanya ada di HUD (`app.py`). Lewat `jobs run`, referral harus lewat env
`ENTER_GIFT_CODE` / `ENTER_INVITER` (kalau kosong, farm memakai default legacy
`farms/enter/farm.py`: `2CL8V7UQ6R` / `Akun Ninja`).

Hasil batch: `results/batch_<stamp>_<hex>/accounts.json` + `credentials.txt`,
plus `results/all_credentials.txt` (append lintas batch) dan `results/used_google.txt`.

---

## 5. Referensi cepat

| Gejala | Bug | Lihat |
|---|---|---|
| `reachable=0` padahal proxy hidup | 1 | §2 Bug 1 |
| `FPJS unavailable` | 2 | §2 Bug 2 |
| `google oauth consent` berulang | 3, 6 | §2 Bug 3 & 6 |
| `session status=404` / `HTTP 204` | 4 | §2 Bug 4 |
| `is not a new user` | 5 | §2 Bug 5 |
| `access_denied` tepat setelah consent | reputasi IP | §3 |
| `deletedaccount` di URL Google | akun Google dihapus | bukan bug farm |

## 6. Catatan untuk yang melanjutkan

- **6 patch ini belum pernah diuji ulang dari nol** (fresh clone, pool baru,
  relay baru). Yang terbukti cuma satu run sukses (run15) plus uji unit per patch.
- `_wait_landing_ready` punya `max_wait=45`; kalau relay makin lambat, angka ini
  yang pertama perlu dinaikkan.
- `access_denied` **tidak** di-retry (by design) dan menandai akun dead.
  Kalau ternyata penyebabnya reputasi IP, akun sehat ikut terbuang — pertimbangkan
  membedakan "Google menolak akun" vs "Auth0 menolak IP" sebelum menandai dead.
- Relay Railway punya biaya: tiap service yang hidup memakan kredit trial akun
  Railway. Relay yang tidak dipakai sebaiknya dihapus.
