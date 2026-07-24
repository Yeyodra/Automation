# Agent instructions (Automation hub)

When the user asks to add a farm / provider / automation (Enter, Cursor, …):

1. **Read first:** `docs/ADD-PROVIDER.md` (full checklist).  
2. **Also:** `docs/ENV-AND-DEPS.md` for env/venv/WARP.  
3. **Reuse globals** — do not reimplement:
   - `core/env.py` — secrets mapping  
   - `core/warp.py` + `core/warp_policy.py` — IP rotate every-N  
   - `core/progress.py` — success/fail progress from logs  
   - `jobs/runner.py` — subprocess + inject `WARP_EVERY_N` (in-farm rotate after N OK)  
   - `core/jobctl.py` — **stop_all()** global kill farm process tree  
4. **Touch:** `farms/<id>/` + `jobs/registry.py` (+ hub `requirements.txt` if new deps).  
5. **WARP everyN:** **1:1 with `-c`** (hub auto-fixes). `0`=off. Counter = **OK only**. Drain peers → rotate → settle.  
6. **Verify:** `python -m jobs list` and `--dry-run --warp-every-n 2 -- -n 9 -c 3 -y` (expect everyN→3).  
7. **9router:** inject `testStatus: active`; bulk `scripts/9router_mark_active.py`.  
8. **Grok reauth (pool repair):** job id `grok-reauth` → `farms/grok/reauth_device_oauth.py`  
   - HUD: Job=`grok-reauth`, **-n 0 = --all** (full expired/revoked pool), `-c` + everyN 1:1.  
   - CLI: `python -m jobs run grok-reauth --warp-every-n 5 -- --all -c 5 --warp-rotate`  
   - Progress: `[N] OK|DEL|FAIL|START …`; DEL = Access denied → row deleted from 9router.  
   - Not the same as farm create (`grok` / `farm.py`).

Do **not** create per-farm venv, hardcode machine paths, paste full WARP CLI into farm, or restart farm every N accounts.

Stop running farm: HUD **Stop [S]** / `python -m jobs stop` / `from core.jobctl import stop_all`.
