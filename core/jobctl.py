"""Global job process control — stop running automations.

Hub may only run one farm subprocess at a time typically, but the registry
supports multiple. stop_all() terminates process trees (Windows: taskkill /T).

Usage:
    from core.jobctl import register, stop_all, is_running, active_summary

    # runner after Popen:
    register(proc, job_id="grok", cmd=cmd)
    ...
    unregister(proc)

    # HUD / CLI:
    stop_all(log=print)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class JobHandle:
    proc: subprocess.Popen
    job_id: str
    cmd: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    @property
    def pid(self) -> int | None:
        return self.proc.pid

    @property
    def running(self) -> bool:
        return self.proc.poll() is None


_lock = threading.Lock()
_active: list[JobHandle] = []
# Cooperative stop flag — runner checks between stdout lines
_stop_requested = threading.Event()


def request_stop() -> None:
    """Soft signal: runner should terminate after current line."""
    _stop_requested.set()


def clear_stop() -> None:
    _stop_requested.clear()


def stop_requested() -> bool:
    return _stop_requested.is_set()


def register(
    proc: subprocess.Popen,
    *,
    job_id: str = "?",
    cmd: list[str] | None = None,
) -> JobHandle:
    h = JobHandle(proc=proc, job_id=job_id, cmd=list(cmd or []))
    with _lock:
        # drop dead handles
        _active[:] = [x for x in _active if x.running]
        _active.append(h)
    clear_stop()
    return h


def unregister(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with _lock:
        _active[:] = [x for x in _active if x.proc is not proc and x.running]


def active_handles() -> list[JobHandle]:
    with _lock:
        _active[:] = [x for x in _active if x.running]
        return list(_active)


def is_running() -> bool:
    return bool(active_handles())


def active_summary() -> str:
    hs = active_handles()
    if not hs:
        return "idle"
    parts = []
    for h in hs:
        age = int(time.time() - h.started)
        parts.append(f"{h.job_id} pid={h.pid} {age}s")
    return ", ".join(parts)


def _kill_tree(pid: int, log: Callable[[str], None]) -> None:
    """Kill process and children (Camoufox browsers, etc.)."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            log(f"[jobctl] taskkill pid={pid} rc={r.returncode} {out[:120]}")
        except Exception as e:
            log(f"[jobctl] taskkill error: {e}")
            try:
                os.kill(pid, 9)
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
            time.sleep(0.5)
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            try:
                os.kill(pid, 9)
            except Exception:
                pass


def stop_all(
    *,
    grace_s: float = 3.0,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Stop every registered farm process. Returns status rows."""
    log = log or (lambda m: print(m, flush=True))
    request_stop()
    handles = active_handles()
    if not handles:
        log("[jobctl] nothing running")
        clear_stop()
        return []

    results: list[dict] = []
    for h in handles:
        pid = h.pid
        log(f"[jobctl] stopping {h.job_id} pid={pid}…")
        try:
            h.proc.terminate()
        except Exception:
            pass
        deadline = time.time() + grace_s
        while time.time() < deadline and h.proc.poll() is None:
            time.sleep(0.15)
        if h.proc.poll() is None and pid:
            log(f"[jobctl] force kill tree pid={pid}")
            _kill_tree(pid, log)
            try:
                h.proc.wait(timeout=5)
            except Exception:
                pass
        code = h.proc.poll()
        results.append(
            {
                "job_id": h.job_id,
                "pid": pid,
                "exit": code,
                "stopped": code is not None,
            }
        )
        log(f"[jobctl] stopped {h.job_id} exit={code}")

    with _lock:
        _active.clear()
    clear_stop()
    return results


def stop_job(job_id: str, **kwargs) -> list[dict]:
    """Stop only handles matching job_id."""
    log = kwargs.get("log") or (lambda m: print(m, flush=True))
    request_stop()
    targets = [h for h in active_handles() if h.job_id == job_id]
    if not targets:
        log(f"[jobctl] no active job {job_id!r}")
        clear_stop()
        return []
    # temporarily only those in list — stop_all style on subset
    results = []
    grace = float(kwargs.get("grace_s", 3.0))
    for h in targets:
        pid = h.pid
        log(f"[jobctl] stopping {h.job_id} pid={pid}…")
        try:
            h.proc.terminate()
        except Exception:
            pass
        deadline = time.time() + grace
        while time.time() < deadline and h.proc.poll() is None:
            time.sleep(0.15)
        if h.proc.poll() is None and pid:
            _kill_tree(pid, log)
            try:
                h.proc.wait(timeout=5)
            except Exception:
                pass
        results.append(
            {
                "job_id": h.job_id,
                "pid": h.pid,
                "exit": h.proc.poll(),
                "stopped": h.proc.poll() is not None,
            }
        )
        unregister(h.proc)
    clear_stop()
    return results
