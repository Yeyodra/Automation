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
