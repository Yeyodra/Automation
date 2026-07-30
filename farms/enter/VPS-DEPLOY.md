# Enter Farm — VPS Deployment Guide

## Overview

```
VPS (Ubuntu 24.04, 16GB RAM recommended)
  ├─ Docker
  │   └─ multi-warp (10 containers)
  │       ├─ warp-n1  → socks5://127.0.0.1:40001
  │       ├─ warp-n2  → socks5://127.0.0.1:40002
  │       └─ ...n10   → socks5://127.0.0.1:40010
  │
  └─ tmux "enter"
      └─ xvfb-run → python -m jobs run enter --warp-every-n 0 -- -n 2000 -c 3
          └─ farm.py (Camoufox headless, proxy round-robin via multi-warp)
```

Winrate: **67-77%** (vs 1% tanpa multi-warp/proxy).

---

## Requirements

- Ubuntu 22.04+ (x86_64)
- 16GB RAM (minimum 8GB for c=3 + 10 WARP containers)
- SSH root access
- Internet unrestricted (UDP/TCP outbound for WireGuard)

---

## Fresh VPS Deploy (Step by Step)

### 1. SSH ke VPS

```bash
ssh root@<IP_VPS>
```

### 2. Create user `auto` (farm runs as this user)

```bash
adduser --disabled-password --gecos "" auto
usermod -aG sudo auto
echo "auto ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
```

### 3. Install dependencies

```bash
apt update && apt install -y python3 python3-venv python3-pip git xvfb tmux curl \
  libgtk-3-0 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxcomposite1 \
  libxdamage1 libxrandr2 libatk1.0-0 libatk-bridge2.0-0 libpango-1.0-0 \
  libcairo2 libgdk-pixbuf-2.0-0 libxcursor1 libxi6 libxtst6 \
  libdrm2 libgbm1 libnss3 libnspr4 libxss1 fonts-liberation
```

### 4. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

### 5. Deploy multi-warp (10 WARP exits)

```bash
git clone https://github.com/Micolaabdi/multi-warp.git /opt/multi-warp
cd /opt/multi-warp
chmod +x scripts/*.sh
COUNT=10 ./scripts/up.sh
```

Verify:
```bash
# Should show 10 containers "healthy"
docker ps

# Test exit IPs
for port in 40001 40002 40003; do
  echo -n "port $port -> "
  curl -s --max-time 8 --proxy socks5h://127.0.0.1:$port https://api.ipify.org
  echo
done
```

### 6. Clone/setup Automation

```bash
su - auto
git clone https://github.com/Yeyodra/Automation.git ~/Automation
cd ~/Automation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/camoufox fetch
```

### 7. Configure .env

```bash
cp .env.example .env
nano .env
```

Key settings:
```env
ENTER_IMAP_USER=akuncursorke1@gmail.com
ENTER_IMAP_PASS=<app_password>
ENTER_EMAIL_MODE=gptmail
ENTER_HEADLESS=true
ENTER_GIFT_CODE=USYWTT9QR4
# Gap/delay — jangan set, biar pake default dari code (30s)
```

### 8. Write proxies.txt (multi-warp SOCKS5 pool)

```bash
cat > ~/Automation/farms/enter/proxies.txt << 'EOF'
socks5://127.0.0.1:40001
socks5://127.0.0.1:40002
socks5://127.0.0.1:40003
socks5://127.0.0.1:40004
socks5://127.0.0.1:40005
socks5://127.0.0.1:40006
socks5://127.0.0.1:40007
socks5://127.0.0.1:40008
socks5://127.0.0.1:40009
socks5://127.0.0.1:40010
EOF
```

### 9. Start farm

```bash
cd ~/Automation
tmux new-session -d -s enter \
  'xvfb-run -a -s "-screen 0 1920x1080x24" \
  .venv/bin/python -m jobs run enter --warp-every-n 0 -- -n 2000 -c 3 -y \
  2>&1 | tee -a farms/enter/logs/vps_run.log'
```

### 10. Verify running

```bash
tmux ls                          # should show "enter" session
pgrep -af farm.py               # should show process
tail -f farms/enter/logs/vps_run.log   # live log
```

---

## Operations

### Check status (from Termux/SSH)

```bash
ssh root@<IP_VPS> 'echo "OK: $(grep -c "] OK" /home/auto/Automation/farms/enter/logs/vps_run.log 2>/dev/null)"; echo "FAIL: $(grep -c "] FAIL" /home/auto/Automation/farms/enter/logs/vps_run.log 2>/dev/null)"; tail -3 /home/auto/Automation/farms/enter/logs/vps_run.log'
```

### Stop farm

```bash
ssh root@<IP_VPS> 'sudo -u auto tmux kill-session -t enter; pkill -f farm.py'
```

### Restart farm

```bash
ssh root@<IP_VPS> 'sudo -u auto bash -c "cd /home/auto/Automation && tmux new-session -d -s enter \"xvfb-run -a -s \\\"-screen 0 1920x1080x24\\\" .venv/bin/python -m jobs run enter --warp-every-n 0 -- -n 2000 -c 3 -y 2>&1 | tee -a farms/enter/logs/vps_run.log\""'
```

### Change gift/referral code

```bash
ssh root@<IP_VPS> "sed -i '/^ENTER_GIFT_CODE/d' /home/auto/Automation/.env; echo 'ENTER_GIFT_CODE=NEWCODE' >> /home/auto/Automation/.env"
# Then restart farm
```

### Auto-push ke 9router VPS (default: ON)

Farm otomatis push credentials ke remote 9router VPS setiap **3 OK** (configurable).

```env
# .env di farm VPS
ENTER_9ROUTER_VPS_EVERY_N=3          # push setiap 3 OK (0=off)
NINEROUTER_VPS_HOST=43.156.135.115   # 9router VPS
NINEROUTER_VPS_USER=ubuntu
NINEROUTER_VPS_PW=Bintang_088
```

Tidak perlu pull manual lagi — credentials langsung masuk 9router DB remote.
Module: `core/ninerouter.py` (reusable semua farm).

### Pull results to local (Windows) — legacy/manual

```bash
python farms/enter/pull_vps_inject.py
```

Script ini:
- SSH ke **semua VPS** di `VPS_LIST`
- Download semua accounts.json dari semua batch
- Inject ke local 9router DB (skip duplicates)
- Track injected keys di `results/vps_injected.txt`

#### Tambah VPS baru ke pull script

Edit `farms/enter/pull_vps_inject.py`, tambah entry di `VPS_LIST`:

```python
VPS_LIST = [
    {"host": "172.235.246.47", "pw": r"YogzZDlS^MYNqs%i4Dlin", "user": "root"},
    {"host": "NEW_IP_HERE", "pw": r"NEW_PASSWORD", "user": "root"},  # <-- tambah
]
```

Run `python farms/enter/pull_vps_inject.py` — otomatis pull dari semua VPS (skip yang unreachable).

### Restart multi-warp containers

```bash
ssh root@<IP_VPS> 'cd /opt/multi-warp && docker compose restart'
```

### Check WARP health

```bash
ssh root@<IP_VPS> 'for p in $(seq 40001 40010); do echo -n "port $p -> "; curl -s --max-time 5 --proxy socks5h://127.0.0.1:$p https://api.ipify.org; echo; done'
```

---

## Deploy ke VPS Baru (Quick Copy)

Kalau lo beli VPS baru dan mau clone setup:

### From local (Windows, pake paramiko script):

1. Edit IP + password di script
2. Run deploy script:

```python
# Edit HOST dan PW di file ini:
# C:\Users\Nazril\AppData\Local\Temp\opencode\deploy_multiwarp.py
```

### Manual (SSH langsung):

```bash
# 1 command chain — copy paste ke VPS baru:
apt update && apt install -y python3 python3-venv python3-pip git xvfb tmux curl \
  libgtk-3-0 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxcomposite1 \
  libxdamage1 libxrandr2 libatk1.0-0 libatk-bridge2.0-0 libpango-1.0-0 \
  libcairo2 libgdk-pixbuf-2.0-0 libxcursor1 libxi6 libxtst6 \
  libdrm2 libgbm1 libnss3 libnspr4 libxss1 fonts-liberation && \
curl -fsSL https://get.docker.com | sh && \
git clone https://github.com/Micolaabdi/multi-warp.git /opt/multi-warp && \
chmod +x /opt/multi-warp/scripts/*.sh && \
cd /opt/multi-warp && COUNT=10 ./scripts/up.sh && \
adduser --disabled-password --gecos "" auto 2>/dev/null; \
sudo -u auto bash -c 'cd ~ && git clone https://github.com/Yeyodra/Automation.git && cd Automation && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/camoufox fetch'
```

Lalu setup .env + proxies.txt + start farm (step 7-9 di atas).

**Estimated time:** ~5 menit per VPS baru.

---

## Scaling

| VPS Count | Concurrent Total | Expected OK/hour |
|-----------|-----------------|------------------|
| 1 VPS     | c=3             | ~60-70           |
| 2 VPS     | c=6             | ~120-140         |
| 3 VPS     | c=9             | ~180-200         |

Setiap VPS independent — gak perlu koordinasi antar VPS. Cuma pastiin gift code sama.

---

## Troubleshooting

### Farm stuck "rate-limit cooldown"
- Pastiin `proxies.txt` ada dan isi SOCKS5 multi-warp
- Proxy mode = no global cooldown (patch terbaru)

### Docker containers unhealthy
```bash
cd /opt/multi-warp && docker compose down && docker compose up -d
sleep 30
./scripts/healthcheck.sh -n 10 --trace
```

### "InvalidProxy" di farm log
- Cek container running: `docker ps`
- Cek port reachable: `curl --proxy socks5h://127.0.0.1:40001 https://api.ipify.org`

### VPS mati / SSH unreachable
- VPS murah = bisa mati kapan aja
- Re-deploy ke VPS baru (5 menit)
- Results yang belum di-pull = hilang (pull regularly!)

### Camoufox crash / Xvfb error
```bash
# Kill all and restart clean
pkill -f farm.py; pkill -f Xvfb
# Start ulang (step 9)
```

---

## File Locations (VPS)

```
/opt/multi-warp/                  → multi-warp Docker setup
/opt/multi-warp/docker-compose.yml
/opt/multi-warp/data-n{1..10}/    → WARP identity volumes

/home/auto/Automation/            → farm code
/home/auto/Automation/.env        → config
/home/auto/Automation/farms/enter/farm.py
/home/auto/Automation/farms/enter/proxies.txt
/home/auto/Automation/farms/enter/results/batch_*/accounts.json
/home/auto/Automation/farms/enter/logs/vps_run*.log
```

## Current VPS

| IP | Region | Status | Note |
|----|--------|--------|------|
| 104.64.15.110 | Jakarta, ID (id-cgk) | ACTIVE | g6-standard-6, 16GB, multi-warp + farm (2026-07-29) |
