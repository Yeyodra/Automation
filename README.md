# Automation Hub

Pusat runner untuk project farm (Grok, Enter, Outlook, dan provider lain).
**Env + Python deps + WARP every-N + progress = global di hub**; farm hanya logic produk.

## Quick start

```powershell
cd C:\Users\Nazril\Documents\Projek\Github\Automation

# Env
copy .env.example .env   # edit IMAP / domain / password

# Deps (sekali)
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\camoufox.exe fetch

# HUD (default — no long commands)
.\.venv\Scripts\python.exe app.py
#   R = Run · W = WARP · everyN = mid-batch rotate · F = fokus log

# CLI
.\.venv\Scripts\python.exe -m jobs list
.\.venv\Scripts\python.exe -m jobs run grok -- -n 1 -c 1 -y
# Wave WARP: everyN forced = -c (e.g. c=3 → everyN=3)
.\.venv\Scripts\python.exe -m jobs run grok --warp-every-n 3 -- -n 20 -c 3 -y
.\.venv\Scripts\python.exe -m jobs stop
```

HUD: **everyN** live-syncs to **-c** when everyN≠0. Counter = OK only; drain peers then rotate.

## Docs

| Doc | For |
|-----|-----|
| **[docs/ADD-PROVIDER.md](./docs/ADD-PROVIDER.md)** | **Agents:** add new farm / provider + wire global systems |
| **[docs/ENV-AND-DEPS.md](./docs/ENV-AND-DEPS.md)** | Env global vs local, venv, Camoufox, WARP |
| `farms/grok/README.md` | Grok product flow |
| **[farms/enter/OPERATIONS.md](./farms/enter/OPERATIONS.md)** | Enter auth flow, Emailqu domains, isolated lanes, NvRouter, deployment |
| `core/warp.py` | `python -m core.warp status\|rotate` |
| `core/progress.py` | Log contract for OK/fail progress |

## Layout

```
Automation/
├── .env / .env.example
├── .venv / requirements.txt
├── core/          # env, warp, warp_policy, progress  (GLOBAL)
├── jobs/          # registry + runner
├── farms/grok/    # provider
├── farms/enter/   # Enter browser signup + isolated lane supervisor
├── app.py         # HUD
└── docs/
```

## Rules of thumb

1. **Secrets** → hub `.env` (shared keys).  
2. **pip / camoufox** → hub `.venv` only.  
3. **WARP everyN** → **1:1 with `-c`** (auto-fixed). e.g. c=3 everyN=3; `0` = off.  
4. **Account success** → print `[id] OK …` so HUD progress works.  
5. **New provider** → follow `docs/ADD-PROVIDER.md`.  
6. Farm-local `.env` = gaps only (`load_dotenv(override=False)`).
