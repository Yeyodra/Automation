"""Run registered jobs as subprocesses (hub venv + global env inject).

WARP mid-batch (enter-farm pattern):
  Inject WARP_EVERY_N into farm env → farm rotates in-process after N OKs.
  One farm process for full -n (one batch folder) — same as Coverage.

Stop: core.jobctl.stop_all() / HUD Stop / python -m jobs stop
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .registry import get_job


@dataclass(frozen=True)
class RunResult:
    job_id: str
    exit_code: int
    duration_s: float
    cwd: str
    cmd: list[str]
    warp: str = ""
    chunks: int = 1
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.stopped


def _peek_env(env: dict[str, str], k: str) -> str:
    v = env.get(k, "")
    if not v:
        return "-"
    if "PASS" in k or "PASSWORD" in k or "TOKEN" in k or "KEY" in k:
        return "***"
    return v


def run_job(
    job_id: str,
    args: Sequence[str] = (),
    *,
    warp_connect: bool = False,
    warp_rotate: bool = False,
    warp_every_n: int | None = None,
    warp_tries: int | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> RunResult:
    """
    Launch one process: <hub-venv-python> farm.py [args...]

    warp_every_n: inject into env; farm rotates after every N OK accounts.
    env_overrides: force env keys for this run (e.g. GROK_EMAIL_MODE from HUD).
    Cooperative stop via core.jobctl.request_stop / stop_all.
    """
    use_print = log is None
    log = log or (lambda m: print(m, flush=True))
    _hub = Path(__file__).resolve().parent.parent
    if str(_hub) not in sys.path:
        sys.path.insert(0, str(_hub))

    from core.env import build_job_env
    from core import jobctl
    from core.warp_policy import extract_concurrent, extract_count, normalize_every_n

    job = get_job(job_id)
    py, entry = job.resolve()
    farm_args = list(args)
    env = build_job_env(job.env_prefix, job.cwd)
    prefix = job.env_prefix or ""

    # HUD / caller overrides win over .env mapping
    if env_overrides:
        for k, v in env_overrides.items():
            if k and v is not None and str(v).strip() != "":
                env[str(k)] = str(v)

    total_n = extract_count(farm_args, default=1)
    conc = extract_concurrent(farm_args, default=1)

    # Per-job WARP kill-switch (outlook: captcha flaky on Cloudflare exits)
    if not getattr(job, "warp_enabled", True):
        if warp_connect or warp_rotate or (warp_every_n is not None and int(warp_every_n) > 0):
            log(f"[hub] WARP disabled for job={job.id} (skip connect/rotate/everyN)")
        warp_connect = False
        warp_rotate = False
        warp_every_n = 0
        every_raw = 0
        every, every_note = 0, ""
        # hard-off inject so farm cannot mid-batch rotate from env leftovers
        env["WARP_EVERY_N"] = "0"
        if prefix:
            env[f"{prefix}WARP_EVERY_N"] = "0"
            env[f"{prefix}CONCURRENT"] = str(conc)
    else:
        every_raw = 0 if warp_every_n is None else max(0, int(warp_every_n))
        every, every_note = normalize_every_n(conc, every_raw)

        if every > 0:
            env["WARP_EVERY_N"] = str(every)
            if prefix:
                env[f"{prefix}WARP_EVERY_N"] = str(every)
                env[f"{prefix}CONCURRENT"] = str(conc)
            env["GROK_WARP_EVERY_N"] = str(every)
            env["GROK_CONCURRENT"] = str(conc)

    cmd = [str(py), str(entry), *farm_args]

    log(f"[hub] job={job.id} name={job.name}")
    log(f"[hub] cwd={job.cwd}")
    log(f"[hub] py={py}")
    log(f"[hub] cmd={' '.join(cmd)}")
    log(
        f"[hub] env: {prefix}IMAP_USER={_peek_env(env, prefix + 'IMAP_USER')} "
        f"DOMAIN={_peek_env(env, prefix + 'EMAIL_DOMAIN')} "
        f"MODE={_peek_env(env, prefix + 'EMAIL_MODE')} "
        f"HEADLESS={_peek_env(env, prefix + 'HEADLESS')}"
    )
    if every_note:
        log(f"[hub] WARP policy: {every_note}")
    log(
        f"[hub] plan: single process  n={total_n}  c={conc}  "
        f"everyN={every or 'off'} (1:1 with c when on)  "
        f"pre_rotate={warp_rotate} connect={warp_connect or warp_rotate or every > 0}"
    )

    warp_notes: list[str] = []
    t0 = time.time()

    if dry_run:
        log("[hub] dry-run — not executing")
        if every > 0:
            log(
                f"[hub] dry: wave mode c={conc} everyN={every} — "
                f"after each {every} OK: drain peers → rotate → settle → resume"
            )
        return RunResult(
            job_id=job.id,
            exit_code=0,
            duration_s=0.0,
            cwd=str(job.cwd),
            cmd=cmd,
            warp="dry-run",
            chunks=1,
        )

    if warp_connect or warp_rotate or every > 0:
        from core.warp import WarpClient

        w = WarpClient(log=log)
        if warp_tries is not None:
            w.max_ip_tries = max(1, warp_tries)
        ok = w.ensure_connected()
        warp_notes.append(f"connect={'ok' if ok else 'fail'}")
        if not ok:
            log("[hub] WARN: WARP not connected")
        if warp_rotate:
            r = w.rotate_ip(force=True)
            warp_notes.append(f"pre-run {r}")
            log(f"[hub] WARP pre-run {r}")

    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["GROK_UI"] = "log"
    env["GROK_VERBOSE"] = env.get("GROK_VERBOSE") or "true"
    env["ENTER_UI"] = "log"
    env["OUTLOOK_UI"] = "log"
    env["OUTLOOK_VERBOSE"] = env.get("OUTLOOK_VERBOSE") or "true"
    env["GETUNIKEY_UI"] = "log"
    env["GETUNIKEY_VERBOSE"] = env.get("GETUNIKEY_VERBOSE") or "true"

    log("[hub] starting farm process (one batch)…")
    log("[hub] stop: HUD Stop / S key / python -m jobs stop")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(job.cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        log(f"[hub] spawn failed: {e}")
        return RunResult(
            job_id=job.id,
            exit_code=127,
            duration_s=time.time() - t0,
            cwd=str(job.cwd),
            cmd=cmd,
            warp="; ".join(warp_notes),
            chunks=1,
        )

    jobctl.register(proc, job_id=job.id, cmd=cmd)
    stopped = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if jobctl.stop_requested():
                log("[hub] stop requested — killing farm process tree…")
                stopped = True
                jobctl.stop_all(log=log, grace_s=2.0)
                break
            text = line.rstrip("\n\r")
            if text:
                log(text)
            if not use_print:
                try:
                    sys.stdout.write(line if line.endswith("\n") else line + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass
        if not stopped:
            # drain: process may still be alive if stdout closed early
            code = proc.wait()
        else:
            code = proc.poll()
            if code is None:
                code = 130
    except KeyboardInterrupt:
        log("[hub] interrupt — stopping…")
        stopped = True
        jobctl.stop_all(log=log, grace_s=2.0)
        code = 130
    finally:
        jobctl.unregister(proc)
        jobctl.clear_stop()

    dur = time.time() - t0
    if stopped:
        log(f"[hub] STOPPED exit={code} duration={dur:.1f}s")
    else:
        log(f"[hub] done exit={code} duration={dur:.1f}s")
        if code == 0:
            log(f"[hub] results → {job.cwd / 'results'}")

    return RunResult(
        job_id=job.id,
        exit_code=code if code is not None else 130,
        duration_s=dur,
        cwd=str(job.cwd),
        cmd=cmd,
        warp="; ".join(warp_notes),
        chunks=1,
        stopped=stopped,
    )
