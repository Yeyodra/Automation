"""Global batch progress tracker — shared by all farms via log lines.

Any automation (grok, enter, …) that prints lines in the formats below
can be tracked by the hub HUD without farm-specific code.

## Log contract (stdout, one event per line)

Timestamped step (preferred — matches grok-farm GROK_UI=log):

    [HH:MM:SS] [<id>] <step>  <message>  optionally <email@domain>

Bare step (detail lines):

    [<id>] <message>

Terminal outcomes (step name, case-insensitive):

    OK / success / done / refresh-ok / reauth-ok  → count ok
    DEL / deleted           → count deleted (e.g. Access denied purge)
    fail / failed / error   → count fail
    start / START / …       → worker running

Bare lines (no timestamp) also count when first token is OK/DEL/FAIL/START:

    [3] OK user@x.com — ok exp=…
    [3] DEL user@x.com — Access denied -> DELETED
    [3] START user@x.com status=unavailable

Examples:

    [11:03:15] [2] OK              Account farmed  <user@domain.com>
    [11:03:17] [4] start           Starting farm #4  <a@b.com>
    [11:03:14] [3] wait_otp        Waiting for OTP  <c@d.com>
    [3] OTP fill round 1/3

Hub also understands:

    [hub] done exit=0 …

Usage from Python (optional, farms can just print):

    from core.progress import BatchProgress, format_step, format_ok, format_fail
    print(format_step(1, "start", "begin", email="a@b.com"), flush=True)
    print(format_ok(1, "a@b.com"), flush=True)

    # HUD side:
    prog = BatchProgress(target=10)
    prog.ingest(line)
    print(prog.render())
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

# ── line patterns ────────────────────────────────────────────────────────────

RE_TS_STEP = re.compile(
    r"^\[(?P<ts>\d{1,2}:\d{2}:\d{2})\]\s+\[(?P<id>\d+)\]\s+(?P<step>\S+)\s+(?P<rest>.*)$"
)
RE_BARE_STEP = re.compile(r"^\[(?P<id>\d+)\]\s+(?P<rest>.+)$")
RE_EMAIL = re.compile(r"<([^>]+@[^>]+)>")

_OK_STEPS = frozenset(
    {"ok", "success", "done", "saved", "refresh-ok", "reauth-ok", "refresh_ok", "reauth_ok"}
)
_FAIL_STEPS = frozenset({"fail", "failed", "error", "err", "timeout"})
_DEL_STEPS = frozenset({"del", "deleted", "delete", "purged"})
_START_STEPS = frozenset({"start", "starting", "begin"})
RE_TARGETS = re.compile(
    r"(?i)targets(?:\s*\([^)]*\))?\s*:\s*(?P<n>\d+)"
)
RE_DONE = re.compile(
    r"(?i)\bDONE\b.*\bok=(?P<ok>\d+).*?(?:deleted=(?P<del>\d+).*?)?fail=(?P<fail>\d+)"
)


def _extract_email(text: str) -> str:
    m = RE_EMAIL.search(text)
    return m.group(1) if m else ""


def _short_email(email: str, n: int = 28) -> str:
    if not email or len(email) <= n:
        return email or "-"
    return email[:12] + "…" + email[-12:]


# ── format helpers (for farms / tests) ───────────────────────────────────────


def format_step(
    attempt: int,
    step: str,
    message: str = "",
    email: str = "",
    *,
    with_ts: bool = True,
) -> str:
    """One progress line in hub contract format."""
    msg = message.strip()
    if email and f"<{email}>" not in msg:
        msg = f"{msg}  <{email}>".strip()
    body = f"[{attempt}] {step:<16} {msg}".rstrip()
    if not with_ts:
        return body
    ts = datetime.now().strftime("%H:%M:%S")
    return f"[{ts}] {body}"


def format_ok(attempt: int, email: str = "", message: str = "ok") -> str:
    return format_step(attempt, "OK", message, email)


def format_fail(attempt: int, message: str = "failed", email: str = "") -> str:
    return format_step(attempt, "fail", message, email)


def format_start(attempt: int, email: str = "", message: str = "start") -> str:
    return format_step(attempt, "start", message, email)


# ── tracker ──────────────────────────────────────────────────────────────────


@dataclass
class WorkerState:
    step: str = "?"
    email: str = ""
    status: str = "run"  # run | ok | fail | del


@dataclass
class BatchProgress:
    """Pure progress state — no UI deps. Safe for any job."""

    target: int = 0
    ok: int = 0
    fail: int = 0
    deleted: int = 0
    t0: float = field(default_factory=time.time)
    # attempt_id -> WorkerState  (NOT named workers — avoids Textual clash if mixed in)
    tracks: dict[int, WorkerState] = field(default_factory=dict)

    def reset(self, target: int = 0) -> None:
        self.target = max(0, int(target))
        self.ok = 0
        self.fail = 0
        self.deleted = 0
        self.t0 = time.time()
        self.tracks.clear()

    @property
    def done(self) -> int:
        return self.ok + self.fail + self.deleted

    @property
    def running(self) -> int:
        return sum(1 for w in self.tracks.values() if w.status == "run")

    def ingest(self, line: str) -> bool:
        """Parse one log line. Returns True if state changed."""
        raw = (line or "").strip()
        if not raw:
            return False

        # "Targets (revoked/expired only): 4475" — set bar total when unknown
        mt = RE_TARGETS.search(raw)
        if mt:
            n = int(mt.group("n"))
            if n > 0 and (self.target <= 0 or n > self.target):
                self.target = n
                return True

        # Final summary: DONE ok=… deleted=… fail=…
        md = RE_DONE.search(raw)
        if md:
            try:
                self.ok = int(md.group("ok") or self.ok)
                self.fail = int(md.group("fail") or self.fail)
                if md.group("del") is not None:
                    self.deleted = int(md.group("del"))
            except (TypeError, ValueError):
                pass
            return True

        m = RE_TS_STEP.match(raw)
        if m:
            return self._apply_step(
                int(m.group("id")),
                m.group("step"),
                m.group("rest"),
            )

        m2 = RE_BARE_STEP.match(raw)
        if m2:
            aid = int(m2.group("id"))
            rest = m2.group("rest").strip()
            # Bare outcome: "[3] OK email — msg" / "[3] DEL …" / "[3] FAIL …" / "[3] START …"
            first = (rest.split(None, 1)[0] if rest else "").lower()
            if first in _OK_STEPS or first in _FAIL_STEPS or first in _DEL_STEPS or first in _START_STEPS:
                rest_body = rest[len(first) :].lstrip(" —-\t")
                return self._apply_step(aid, first, rest_body)
            w = self.tracks.setdefault(aid, WorkerState())
            if w.status == "run":
                hint = rest.split(":")[0].strip()[:20]
                if hint:
                    w.step = hint
                email = _extract_email(rest)
                if not email:
                    # reauth bare lines put email as first token
                    tok = rest.split(None, 1)[0] if rest else ""
                    if "@" in tok:
                        email = tok.strip("<>")
                if email:
                    w.email = email
            return True

        return False

    def _apply_step(self, aid: int, step: str, rest: str) -> bool:
        step_l = step.lower()
        email = _extract_email(rest)
        if not email:
            tok = (rest or "").split(None, 1)[0] if rest else ""
            if "@" in tok:
                email = tok.strip("<>")
        w = self.tracks.setdefault(aid, WorkerState())
        if email:
            w.email = email
        w.step = step_l

        if step_l in _OK_STEPS or "account farmed" in rest.lower():
            if w.status != "ok":
                if w.status == "fail":
                    self.fail = max(0, self.fail - 1)
                elif w.status == "del":
                    self.deleted = max(0, self.deleted - 1)
                w.status = "ok"
                self.ok += 1
            return True

        if step_l in _DEL_STEPS:
            if w.status != "del":
                if w.status == "ok":
                    self.ok = max(0, self.ok - 1)
                elif w.status == "fail":
                    self.fail = max(0, self.fail - 1)
                w.status = "del"
                self.deleted += 1
            return True

        if step_l in _FAIL_STEPS or rest.lower().startswith("fail"):
            if w.status not in ("ok", "fail", "del"):
                w.status = "fail"
                self.fail += 1
            return True

        if step_l in _START_STEPS or w.status not in ("ok", "fail", "del"):
            if w.status not in ("ok", "fail", "del"):
                w.status = "run"
        return True

    def render(self, *, bar_width: int = 20, max_active: int = 4) -> str:
        """Multi-line progress block for HUD / CLI."""
        target = self.target
        ok, fail, deleted, running = self.ok, self.fail, self.deleted, self.running
        done = self.done

        if target <= 0 and done == 0 and running == 0:
            return "Progress: idle"

        tot = target if target > 0 else max(done, 1)
        pct = int(100 * done / tot) if tot else 0
        filled = min(bar_width, int(bar_width * done / tot)) if tot else 0
        bar = "█" * filled + "░" * (bar_width - filled)
        elapsed = int(time.time() - self.t0) if self.t0 else 0
        mm, ss = divmod(max(0, elapsed), 60)

        counts = f"ok={ok}  fail={fail}"
        if deleted:
            counts = f"ok={ok}  del={deleted}  fail={fail}"
        lines = [
            f" {bar}  {done}/{tot} ({pct}%)   "
            f"{counts}  run={running}  {mm:02d}:{ss:02d}"
        ]
        active = [
            (aid, w)
            for aid, w in sorted(self.tracks.items())
            if w.status == "run"
        ][:max_active]
        for aid, w in active:
            lines.append(
                f"  #{aid}  {w.step[:16]:16}  {_short_email(w.email)}"
            )
        if not active and target > 0 and done < target:
            lines.append("  (waiting workers…)")
        return "\n".join(lines)

    def status_line(self) -> str:
        """One-line status bar text."""
        if self.target <= 0 and self.done == 0:
            return "idle"
        tot = self.target or max(self.done, 1)
        base = f"{self.done}/{tot}  ok={self.ok}"
        if self.deleted:
            base += f" del={self.deleted}"
        return f"{base} fail={self.fail} run={self.running}"



def make_log_sink(
    progress: BatchProgress,
    *,
    on_change: Callable[[BatchProgress], None] | None = None,
    forward: Callable[[str], None] | None = None,
) -> Callable[[str], None]:
    """Return a log(msg) callback that updates progress + optional forward.

    Example for runner/HUD:

        prog = BatchProgress(target=10)
        log = make_log_sink(prog, on_change=lambda p: ui.set(p.render()), forward=print)
        run_job("grok", args, log=log)
    """

    def _log(msg: str) -> None:
        text = msg.rstrip("\n")
        if forward is not None:
            forward(text)
        if progress.ingest(text) and on_change is not None:
            on_change(progress)

    return _log
