"""Mid-batch WARP policy (enter-farm / Coverage pattern) — global hub.

Preferred flow (one farm process, one batch folder):
  hub runner injects WARP_EVERY_N into env
  farm after each OK → WarpPolicy.on_success() or farms/grok _maybe_warp_after_success
  → core.warp.rotate_ip() every N successes

plan_chunks() remains a helper only (not used by runner for multi-spawn).

Usage:
    from core.warp_policy import WarpPolicy
    policy = WarpPolicy(every_n=2, log=print)
    # after each farmed account OK:
    policy.on_success()  # may rotate when counter hits every_n
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .warp import RotateResult, WarpClient


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def plan_chunks(total: int, every_n: int) -> list[int]:
    """Split total accounts into chunk sizes of at most every_n.

    plan_chunks(10, 2) -> [2,2,2,2,2]
    plan_chunks(5, 2)  -> [2,2,1]
    plan_chunks(3, 0)  -> [3]
    """
    total = max(0, int(total))
    every_n = int(every_n)
    if total <= 0:
        return []
    if every_n <= 0 or every_n >= total:
        return [total]
    chunks: list[int] = []
    left = total
    while left > 0:
        take = min(every_n, left)
        chunks.append(take)
        left -= take
    return chunks


def default_every_n() -> int:
    """Hub env WARP_EVERY_N (enter-farm: ENTER_WARP_EVERY_N default 2)."""
    return max(0, _env_int("WARP_EVERY_N", 2))


@dataclass
class WarpPolicy:
    """Serialize rotates + optional in-process success counter."""

    client: WarpClient | None = None
    every_n: int = field(default_factory=default_every_n)
    log: Callable[[str], None] = field(default=lambda _m: None)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _success_since: int = 0
    _last_rotate: float = 0.0

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = WarpClient(log=self.log)

    def ensure_connected(self) -> bool:
        assert self.client is not None
        with self._lock:
            return self.client.ensure_connected()

    def rotate(self, reason: str = "", *, force: bool = True) -> RotateResult:
        assert self.client is not None
        with self._lock:
            if reason:
                self.log(f"[warp-policy] rotate ({reason})")
            r = self.client.rotate_ip(force=force)
            self._success_since = 0
            self._last_rotate = time.time()
            self.log(f"[warp-policy] {r}")
            return r

    def on_success(self) -> RotateResult | None:
        """In-process hook after each OK account."""
        if self.every_n <= 0:
            return None
        with self._lock:
            self._success_since += 1
            n = self._success_since
            if n < self.every_n:
                self.log(f"[warp-policy] success {n}/{self.every_n} (no rotate yet)")
                return None
            self.log(f"[warp-policy] success {n}/{self.every_n} -> proactive rotate")
        return self.rotate(f"every {self.every_n} successes")

    def reset_counter(self) -> None:
        with self._lock:
            self._success_since = 0


def rewrite_count_args(args: Sequence[str] | list[str], n: int) -> list[str]:
    """Replace or insert -n/--count in farm argv."""
    out: list[str] = []
    i = 0
    a = list(args)
    replaced = False
    while i < len(a):
        tok = a[i]
        if tok in ("-n", "--count", "--max") and i + 1 < len(a):
            out.extend([tok, str(n)])
            i += 2
            replaced = True
            continue
        if tok.startswith("-n=") or tok.startswith("--count="):
            key = tok.split("=", 1)[0]
            out.append(f"{key}={n}")
            i += 1
            replaced = True
            continue
        out.append(tok)
        i += 1
    if not replaced:
        out = ["-n", str(n), *out]
    return out


def extract_count(args: Sequence[str], default: int = 1) -> int:
    """Read -n / --count from farm args."""
    a = list(args)
    i = 0
    while i < len(a):
        tok = a[i]
        if tok in ("-n", "--count", "--max") and i + 1 < len(a):
            try:
                return max(1, int(a[i + 1]))
            except ValueError:
                return default
        if tok.startswith("-n=") or tok.startswith("--count="):
            try:
                return max(1, int(tok.split("=", 1)[1]))
            except ValueError:
                return default
        i += 1
    return max(1, default)


def extract_concurrent(args: Sequence[str], default: int = 1) -> int:
    """Read -c / --concurrent from farm args."""
    a = list(args)
    i = 0
    while i < len(a):
        tok = a[i]
        if tok in ("-c", "--concurrent") and i + 1 < len(a):
            try:
                return max(1, int(a[i + 1]))
            except ValueError:
                return default
        if tok.startswith("-c=") or tok.startswith("--concurrent="):
            try:
                return max(1, int(tok.split("=", 1)[1]))
            except ValueError:
                return default
        i += 1
    return max(1, default)


def normalize_every_n(concurrent: int, every_n: int) -> tuple[int, str | None]:
    """Best practice: everyN is 0 (off) or equal to concurrent (1:1 wave).

    Returns (normalized_every_n, note_if_changed).
    """
    c = max(1, int(concurrent))
    e = max(0, int(every_n))
    if e == 0:
        return 0, None
    if e != c:
        return c, f"everyN {e} → {c} (1:1 with concurrent -c {c})"
    return e, None
