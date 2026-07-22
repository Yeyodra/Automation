"""Known automation jobs — bundled under farms/; use hub venv + global env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_HUB = Path(__file__).resolve().parent.parent
_FARMS = _HUB / "farms"


def _job_cwd(name: str, env_key: str) -> Path:
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        return Path(raw)
    return _FARMS / name


@dataclass(frozen=True)
class JobDef:
    id: str
    name: str
    cwd: Path
    entry: str
    description: str = ""
    env_prefix: str = ""  # GROK_ / ENTER_ — for hub env mapping
    # False = never auto-connect / inject / mid-batch WARP (e.g. outlook captcha flaky on CF)
    warp_enabled: bool = True

    def python(self) -> Path:
        """Hub .venv first (global deps), then farm venv, then sys."""
        for rel in (
            _HUB / ".venv" / "Scripts" / "python.exe",
            _HUB / ".venv" / "bin" / "python",
            self.cwd / ".venv" / "Scripts" / "python.exe",
            self.cwd / ".venv" / "bin" / "python",
        ):
            if rel.is_file():
                return rel
        return Path(os.environ.get("PYTHON", "") or __import__("sys").executable)

    def resolve(self) -> tuple[Path, Path]:
        if not self.cwd.is_dir():
            raise FileNotFoundError(f"job cwd missing: {self.cwd}")
        entry = self.cwd / self.entry
        if not entry.is_file():
            raise FileNotFoundError(f"job entry missing: {entry}")
        return self.python(), entry


_GROK = _job_cwd("grok", "AUTOMATION_GROK_FARM")
_OUTLOOK = _job_cwd("outlook", "AUTOMATION_OUTLOOK_FARM")
_ENTER = _job_cwd("enter", "AUTOMATION_ENTER_FARM")
_GETUNIKEY = _job_cwd("getunikey", "AUTOMATION_GETUNIKEY_FARM")

JOBS: dict[str, JobDef] = {
    "grok": JobDef(
        id="grok",
        name="grok-farm",
        cwd=_GROK,
        entry="farm.py",
        description="xAI/Grok CLI farmer — farms/grok (hub env + hub venv)",
        env_prefix="GROK_",
    ),
    "grok-farm": JobDef(
        id="grok",
        name="grok-farm",
        cwd=_GROK,
        entry="farm.py",
        description="Alias of grok",
        env_prefix="GROK_",
    ),
    "outlook": JobDef(
        id="outlook",
        name="outlook-farm",
        cwd=_OUTLOOK,
        entry="farm.py",
        description="Outlook MSA signup (IMAP + PX hold) — WARP disabled",
        env_prefix="OUTLOOK_",
        warp_enabled=False,
    ),
    "outlook-farm": JobDef(
        id="outlook",
        name="outlook-farm",
        cwd=_OUTLOOK,
        entry="farm.py",
        description="Alias of outlook — WARP disabled",
        env_prefix="OUTLOOK_",
        warp_enabled=False,
    ),
    "enter": JobDef(
        id="enter",
        name="enter-farm",
        cwd=_ENTER,
        entry="farm.py",
        description="Enter/Converge farmer — farms/enter (gift→Auth0→OTP→ek_)",
        env_prefix="ENTER_",
    ),
    "enter-farm": JobDef(
        id="enter",
        name="enter-farm",
        cwd=_ENTER,
        entry="farm.py",
        description="Alias of enter",
        env_prefix="ENTER_",
    ),
    "getunikey": JobDef(
        id="getunikey",
        name="getunikey-farm",
        cwd=_GETUNIKEY,
        entry="farm.py",
        description="GetUniKey farmer — farms/getunikey (scaffold; stub OK)",
        env_prefix="GETUNIKEY_",
    ),
    "getunikey-farm": JobDef(
        id="getunikey",
        name="getunikey-farm",
        cwd=_GETUNIKEY,
        entry="farm.py",
        description="Alias of getunikey",
        env_prefix="GETUNIKEY_",
    ),
}


def get_job(job_id: str) -> JobDef:
    key = job_id.strip().lower()
    if key not in JOBS:
        known = ", ".join(sorted({j.id for j in JOBS.values()}))
        raise KeyError(f"unknown job {job_id!r}; known: {known}")
    return JOBS[key]


def list_jobs() -> list[JobDef]:
    seen: dict[str, JobDef] = {}
    for j in JOBS.values():
        seen.setdefault(j.id, j)
    return list(seen.values())
