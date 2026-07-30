# Agent Guide: Add a New Automation Provider

**Audience:** coding agents / future-you adding a farm (Enter, Cursor, …) into this hub.  
**Goal:** reuse **global** systems (env, venv, WARP every-N, progress HUD). Do **not** reimplement them inside the farm.

Read also: [ENV-AND-DEPS.md](./ENV-AND-DEPS.md).

---

## 0. Mental model (mandatory)

```
Automation/                          HUB (global)
├── .env                             shared secrets
├── .venv/                           shared Python + camoufox[geoip]
├── core/
│   ├── env.py                       map IMAP_* → PREFIX_*
│   ├── warp.py                      rotate / connect / IP
│   ├── warp_policy.py               everyN 1:1 with -c + helpers
│   ├── progress.py                  parse OK/fail from log lines
│   ├── jobctl.py                    stop_all() process tree
│   └── ninerouter.py                batch push credentials to remote 9router VPS
├── jobs/
│   ├── registry.py                  ← register new job here
│   └── runner.py                    env inject + WARP_EVERY_N → farm
├── app.py                           HUD (everyN live-sync to -c)
├── scripts/9router_mark_active.py   bulk testStatus untested→active
└── farms/<id>/                      provider code only
    ├── farm.py                      entry (CLI -n -c -y) + warp hook
    ├── .env                         optional gaps only
    └── results/                     batch outputs
```

| Responsibility | Hub | Farm |
|----------------|-----|------|
| IMAP / password / headless defaults | ✅ `.env` | optional override |
| pip packages / Camoufox geoip | ✅ `.venv` | **no** private venv |
| WARP everyN | inject env; force everyN==c | hook after each **OK**; drain then rotate |
| Progress bar 5/10 ok/fail | ✅ parse stdout | print log contract |
| Stop running job | `jobctl.stop_all` / HUD S | — |
| Signup / OTP / product API | ❌ | ✅ |

**Rule:** if it is useful for 2+ providers → put in `core/` or hub `.env`. Farm stays product-specific.

---

## 1. Checklist (do in order)

### Step 1 — Bundle source under `farms/<id>/`

```text
farms/<id>/
  farm.py              # required entry
  requirements.txt     # docs only; deps go to hub requirements.txt if new
  .env.example         # PREFIX_* keys for humans
  README.md            # optional product notes
  results/ screenshots/ logs/   # empty runtime dirs
```

**Copy:** source scripts, examples, docs.  
**Do not copy:** `.venv`, real `.env`, old `results/*`, secrets, `__pycache__`.

Id naming: short slug, lowercase: `enter`, `grok`, `cursor`.

### Step 2 — Hub deps (only if new packages)

Edit root `requirements.txt`, then:

```powershell
.\.venv\Scripts\pip install -r requirements.txt
# if browser stack:
.\.venv\Scripts\camoufox.exe fetch
```

Reuse existing pins when possible (`camoufox`, `playwright`, `python-dotenv`).

### Step 3 — Register job

Edit `jobs/registry.py`:

```python
_ENTER = _job_cwd("enter", "AUTOMATION_ENTER_FARM")

JOBS["enter"] = JobDef(
    id="enter",
    name="enter-farm",
    cwd=_ENTER,
    entry="farm.py",
    description="Enter/Converge farmer — farms/enter",
    env_prefix="ENTER_",   # MUST end with _
)
JOBS["enter-farm"] = JobDef(  # optional alias
    id="enter",
    name="enter-farm",
    cwd=_ENTER,
    entry="farm.py",
    description="Alias of enter",
    env_prefix="ENTER_",
)
```

Override path (optional): env `AUTOMATION_<ID>_FARM` → absolute cwd.

### Step 4 — Env mapping

Hub `.env` uses **shared keys** (no prefix). Runner maps them via `core/env.py` `_SHARED_MAP`:

| Hub shared | → `ENTER_*` example |
|------------|---------------------|
| `IMAP_USER` | `ENTER_IMAP_USER` |
| `ACCOUNT_PASSWORD` | `ENTER_PASSWORD` |
| `EMAIL_DOMAIN` | `ENTER_EMAIL_DOMAIN` |
| `HEADLESS` | `ENTER_HEADLESS` |
| … | see `_SHARED_MAP` |

**Provider-specific** keys (gift code, client id, …): put in hub `.env` with full prefix:

```env
ENTER_GIFT_CODE=XXXX
ENTER_EMAIL_MODE=gptmail
```

Or farm-local `farms/enter/.env` with `load_dotenv(override=False)` so hub wins.

If farm still does `load_dotenv(..., override=True)` → **change to `override=False`** or hub inject is wiped.

### Step 5 — Farm CLI contract (required)

`farm.py` must accept (argparse or equivalent):

| Flag | Meaning |
|------|---------|
| `-n` / `--count` | accounts this run |
| `-c` / `--concurrent` | parallel workers |
| `-y` / `--yes` | non-interactive |

Hub runs **one** process with full `-n` (one batch folder).  
`--warp-every-n` only injects env; farm must rotate after N OKs.

Prefer:

```text
python farm.py -n 10 -c 1 -y
```

Stdout line-oriented (not a full-screen TUI that steals the terminal).  
If farm has HUD mode, force log mode when env set:

```text
GROK_UI=log / ENTER_UI=log   # runner already sets these for known prefixes
```

For a new prefix, either:

- teach runner to set `YOUR_UI=log`, or  
- default farm to line logs when not a TTY / when `PYTHONUNBUFFERED=1`.

### Step 6 — Progress log contract (for HUD success bar)

Print **one event per line** on stdout (see `core/progress.py`):

```text
[HH:MM:SS] [<attempt_id>] <step>  message  <email@domain>
```

| Step (case-insensitive) | Effect on HUD |
|-------------------------|---------------|
| `start` | worker running |
| `OK` / `success` / `done` | **ok += 1** |
| `fail` / `failed` / `error` | **fail += 1** |
| other (`wait_otp`, …) | update step label |

Helpers (optional, farm may import hub if on path — prefer plain print for subprocess purity):

```python
from core.progress import format_start, format_ok, format_fail
print(format_start(1, "user@domain.com", "Starting"), flush=True)
print(format_ok(1, "user@domain.com", "Account farmed"), flush=True)
print(format_fail(1, "OTP timeout", "user@domain.com"), flush=True)
```

**Success account** = one `OK` line per completed account. That is what feeds global progress `5/10 ok=5`.

### Step 7 — WARP (do not reimplement)

**Wave 1:1:** one farm process, one batch folder.  
`everyN` is **0 (off)** or **equal to `-c`** (hub auto-fixes; HUD live-syncs field).

Hub injects `WARP_EVERY_N` + concurrent. Farm on each **OK** (not FAIL):

1. Counter += 1  
2. If counter == everyN: block new starts → drain peers → `core.warp.rotate` → settle → resume  
3. See `farms/grok/farm.py` `_maybe_warp_after_success`

```powershell
# c=3 → everyN forced to 3 even if you pass 2
python -m jobs run enter --warp-every-n 3 -- -n 20 -c 3 -y
# log: success 3/3 → drain then rotate… → settle → resume
```

| You need | Call / flag |
|----------|-------------|
| Auto IP per wave | everyN = c (e.g. 3/3 or 8/8) |
| No auto IP | everyN = **0** |
| Pre-run rotate | CLI `--warp-rotate` / HUD **Rotate** (not a form checkbox) |
| Manual mid-run | HUD **Rotate** (better when farm idle) |
| Stop farm | HUD **Stop [S]** / `python -m jobs stop` |
| New farm hook | copy grok `_maybe_warp_after_success` pattern |

**Do not:** multi-spawn farm for rotate; set everyN << c (hub will force everyN=c).

**Stuck worker:** holds a slot; during drain can delay rotate until done or drain timeout (~180s).

### Step 7b — 9router inject (if applicable)

On success insert with `"testStatus": "active"` (not `untested`) so pool is usable without Test One-by-One.  
Bulk: `python scripts/9router_mark_active.py --provider grok-cli`

### Step 8 — Verify

```powershell
cd C:\Users\Nazril\Documents\Projek\Github\Automation

python -m jobs list
python -m jobs run enter --dry-run --warp-every-n 2 -- -n 9 -c 3 -y
# expect: WARP policy everyN 2 → 3; plan everyN=3

python -m jobs run enter -- -n 1 -c 1 -y
# expect: OK line, results under farms/enter/results/
```

HUD: Job=`enter`, `-c`=3, everyN becomes 3 live, Run once.

---

## 2. Global APIs cheat sheet

### Env

```python
from core.env import build_job_env, load_hub_env, hub_python
env = build_job_env("ENTER_", farm_cwd)  # dict for subprocess
```

### WARP

```python
from core.warp import WarpClient, public_ip
from core.warp_policy import WarpPolicy, normalize_every_n

w = WarpClient(log=print)
w.ensure_connected()
w.rotate_ip(force=True)

every, note = normalize_every_n(concurrent=3, every_n=2)  # → (3, "…")
policy = WarpPolicy(every_n=every, log=print)
# after each farmed account OK (in-process):
policy.on_success()  # may rotate when counter hits every_n
```

```python
from core.jobctl import stop_all, is_running, active_summary
stop_all(log=print)
```

### Progress

```python
from core.progress import BatchProgress, format_ok, make_log_sink
p = BatchProgress(target=10)
p.ingest("[12:00:00] [1] OK  done  <a@b.com>")
print(p.render(), p.status_line())
```

### Run job (agents / scripts)

```python
from jobs.runner import run_job
run_job(
    "enter",
    ["-n", "10", "-c", "1", "-y"],
    warp_every_n=2,
    warp_rotate=False,
    dry_run=False,
    log=print,
)
```

### Stop running automation (global)

```python
from core.jobctl import stop_all, is_running, active_summary
print(active_summary())   # e.g. grok pid=1234 45s
stop_all(log=print)       # terminate + taskkill /T (Windows process tree)
```

CLI: `python -m jobs stop` · HUD: **Stop [S]** (not the same as Quit).
---

## 3. Anti-patterns (agents must avoid)

| ❌ Don't | ✅ Do |
|----------|--------|
| New `.venv` under `farms/x` | Hub `.venv` only |
| Secrets only in farm `.env` | Hub `.env` shared keys |
| Copy-paste full warp-cli into farm | hub `core.warp` + after-OK hook |
| Multi-spawn farm every N (new batch each time) | one process + inject everyN |
| everyN != c when everyN>0 | hub forces everyN=c (wave 1:1) |
| testStatus untested on inject | `"testStatus": "active"` |
| Full-screen farm TUI as default under hub | Line logs + optional farm HUD when run solo |
| `load_dotenv(override=True)` wiping hub inject | `override=False` |
| Custom progress only inside farm print spam | `OK`/`fail` steps for HUD |
| Hardcode absolute paths in registry | `_job_cwd("id", "AUTOMATION_ID_FARM")` |
| Name App attrs `workers` / `_workers` in Textual | Use `_farm_track` etc. (Textual owns `workers`) |

---

## 4. Minimal “enter” example (skeleton)

```text
1. Copy enter-farm sources → farms/enter/ (no venv/results/secrets)
2. farm.py: load_dotenv(override=False); CLI -n -c -y
3. registry.py: JobDef id=enter, env_prefix=ENTER_
4. Hub .env: shared IMAP_* + ENTER_GIFT_CODE=...
5. python -m jobs run enter --warp-every-n 3 -- -n 9 -c 3 -y
```

---

## 5. Definition of done

- [ ] `python -m jobs list` shows `[ok]` and hub `.venv` python  
- [ ] Dry-run `--warp-every-n 2 -- -c 3` logs everyN **2 → 3**  
- [ ] Farm after-OK warp hook if everyN needed (copy grok)  
- [ ] One real account: `OK` line; HUD progress ok≥1  
- [ ] Results in `farms/<id>/results/batch_*`  
- [ ] `python -m jobs stop` / HUD Stop works  
- [ ] No second venv; no full warp-cli paste in farm  

---

## 6. File index for agents

| Path | When to touch |
|------|----------------|
| `jobs/registry.py` | always (register) |
| `farms/<id>/` | always (code) |
| `requirements.txt` | if new pip dep |
| `core/env.py` `_SHARED_MAP` | if new shared key name |
| `jobs/runner.py` | only if new runner behavior (rare) |
| `app.py` | only if new HUD control (rare; prefer generic form) |
| `docs/ENV-AND-DEPS.md` | env docs |
| **this file** | process for new providers |

When unsure: **extend `core/` + registry**, not fork hub logic into the farm.
