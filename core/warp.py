"""Cloudflare WARP IP rotate — shared hub primitive (stdlib only).

Extracted from enter-farm; no farm/browser deps. Safe for any job to call.

CLI:
  python -m core.warp status
  python -m core.warp ip
  python -m core.warp connect
  python -m core.warp disconnect
  python -m core.warp rotate
  python -m core.warp rotate --tries 4 --no-keys
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ── defaults (override via env or WarpClient kwargs) ─────────────────────────

_DEFAULT_CLI_WIN = r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"
_IP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def public_ip(timeout: float = 10.0) -> str:
    """Best-effort public IPv4/IPv6 string, or '?'."""
    for url in _IP_URLS:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore").strip()
        except Exception:
            continue
    return "?"


@dataclass(frozen=True)
class RotateResult:
    ok: bool
    before: str
    after: str
    changed: bool
    tries: int
    connected: bool
    detail: str = ""

    def __str__(self) -> str:
        flag = "changed" if self.changed else "same-ip"
        return (
            f"ok={self.ok} {flag} tries={self.tries} "
            f"{self.before} -> {self.after} connected={self.connected}"
            + (f" ({self.detail})" if self.detail else "")
        )


class WarpClient:
    """Thin wrapper around warp-cli: status / connect / disconnect / rotate."""

    def __init__(
        self,
        cli: str | None = None,
        *,
        disconnect_wait: float | None = None,
        connect_wait: float | None = None,
        cooldown_after: float | None = None,
        max_ip_tries: int | None = None,
        use_rotate_keys: bool | None = None,
        min_rotate_interval: float = 15.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.cli = cli or _env("WARP_CLI", _DEFAULT_CLI_WIN)
        self.disconnect_wait = (
            disconnect_wait
            if disconnect_wait is not None
            else _env_float("WARP_DISCONNECT_WAIT", 3.0)
        )
        self.connect_wait = (
            connect_wait
            if connect_wait is not None
            else _env_float("WARP_CONNECT_WAIT", 8.0)
        )
        self.cooldown_after = (
            cooldown_after
            if cooldown_after is not None
            else _env_float("WARP_COOLDOWN_AFTER", 15.0)
        )
        self.max_ip_tries = max(
            1,
            max_ip_tries
            if max_ip_tries is not None
            else _env_int("WARP_MAX_IP_TRIES", 4),
        )
        self.use_rotate_keys = (
            use_rotate_keys
            if use_rotate_keys is not None
            else _env_bool("WARP_USE_ROTATE_KEYS", True)
        )
        self.min_rotate_interval = min_rotate_interval
        self._log = log or (lambda _m: None)
        self._last_rotate = 0.0

    # ── process ──────────────────────────────────────────────────────────────

    def cli_path(self) -> str | None:
        p = Path(self.cli)
        if p.is_file():
            return str(p)
        return shutil.which("warp-cli") or shutil.which("warp-cli.exe")

    def run(self, *args: str, timeout: float = 30.0) -> tuple[int, str]:
        exe = self.cli_path()
        if not exe:
            return 127, "warp-cli not found"
        try:
            r = subprocess.run(
                [exe, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode, out
        except Exception as e:
            return 1, f"{type(e).__name__}: {e}"

    # ── status ───────────────────────────────────────────────────────────────

    def status_text(self) -> str:
        code, out = self.run("status")
        return out or f"(exit {code})"

    def is_connected(self) -> bool:
        st = self.status_text().lower()
        # Prefer explicit status lines; avoid "disconnected" false positives.
        if "status update: connected" in st or "warp is connected" in st:
            return True
        if "status: connected" in st:
            return True
        if "status update: disconnected" in st or "status: disconnected" in st:
            return False
        if "disconnected" in st and "connected" not in st.replace("disconnected", ""):
            return False
        if "connected" in st and "disconnected" not in st:
            return True
        return False

    def wait_connected(self, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_connected():
                return True
            time.sleep(1.0)
        return self.is_connected()

    # ── connect / disconnect ─────────────────────────────────────────────────

    def ensure_connected(self) -> bool:
        if not self.cli_path():
            self._log("WARP: cli missing")
            return False
        if self.is_connected():
            self._log(f"WARP already connected ip={public_ip()}")
            return True
        self._log("WARP ensuring connect…")
        code, out = self.run("connect")
        self._log(f"WARP connect rc={code} {out[:100]}")
        time.sleep(max(3.0, self.connect_wait))
        ok = self.is_connected()
        self._log(f"WARP connected={ok} ip={public_ip()}")
        return ok

    def disconnect(self) -> bool:
        code, out = self.run("disconnect")
        self._log(f"WARP disconnect rc={code} {out[:80]}")
        time.sleep(max(1.0, self.disconnect_wait))
        return not self.is_connected()

    def connect(self) -> bool:
        code, out = self.run("connect")
        self._log(f"WARP connect rc={code} {out[:80]}")
        ok = self.wait_connected(max(8.0, self.connect_wait + 10))
        return ok

    # ── rotate ───────────────────────────────────────────────────────────────

    def rotate_ip(
        self,
        *,
        max_tries: int | None = None,
        rotate_keys: bool | None = None,
        force: bool = False,
    ) -> RotateResult:
        """Refresh WARP egress (best-effort).

        Algorithm (enter-farm proven):
          1) tunnel rotate-keys (optional)
          2) disconnect → wait → connect
          3) retry until public IP changes or max_tries

        Note: same IP after reconnect is normal (WARP anycast). ok=True if
        tunnel is connected even when changed=False.
        """
        if not self.cli_path():
            return RotateResult(
                ok=False,
                before="?",
                after="?",
                changed=False,
                tries=0,
                connected=False,
                detail=f"warp-cli not found ({self.cli!r})",
            )

        now = time.time()
        if not force and (now - self._last_rotate) < self.min_rotate_interval:
            ip = public_ip()
            return RotateResult(
                ok=True,
                before=ip,
                after=ip,
                changed=False,
                tries=0,
                connected=self.is_connected(),
                detail="skipped (recently rotated)",
            )

        tries = max(1, max_tries if max_tries is not None else self.max_ip_tries)
        do_keys = (
            self.use_rotate_keys if rotate_keys is None else bool(rotate_keys)
        )
        before = public_ip()
        self._log(f"WARP: rotate start (ip_before={before})")

        after = before
        connected = False
        changed = False
        used = 0

        for try_i in range(1, tries + 1):
            used = try_i
            self._log(f"WARP rotate try {try_i}/{tries}")

            if do_keys:
                code, out = self.run("tunnel", "rotate-keys")
                self._log(f"WARP rotate-keys rc={code} {out[:80]}")

            code, out = self.run("disconnect")
            self._log(f"WARP disconnect rc={code} {out[:80]}")
            time.sleep(max(1.5, self.disconnect_wait + (try_i - 1) * 1.5))

            code, out = self.run("connect")
            self._log(f"WARP connect rc={code} {out[:80]}")
            connected = self.wait_connected(max(8.0, self.connect_wait + 10))
            time.sleep(max(1.0, self.cooldown_after * 0.5))

            after = public_ip()
            changed = bool(
                before and after and after != "?" and before != after
            )
            self._log(
                f"WARP try {try_i}: connected={connected} ip={after} changed={changed}"
            )
            if changed:
                break
            time.sleep(2.0 + try_i)

        time.sleep(max(0.5, self.cooldown_after * 0.5))
        after = public_ip()
        changed = bool(before and after and after != "?" and before != after)
        self._last_rotate = time.time()

        ok = connected or after != "?"
        detail = ""
        if not changed:
            detail = (
                f"IP unchanged after {used} tries "
                "(normal for WARP anycast — tunnel still refreshed)"
            )
            self._log(f"WARP: {detail}")
        self._log(
            f"WARP: rotate done connected={connected} "
            f"ip_before={before} ip_after={after} changed={changed}"
        )
        return RotateResult(
            ok=ok,
            before=before,
            after=after,
            changed=changed,
            tries=used,
            connected=connected,
            detail=detail,
        )


# ── module-level convenience ─────────────────────────────────────────────────

_default: WarpClient | None = None


def client() -> WarpClient:
    global _default
    if _default is None:
        _default = WarpClient(log=lambda m: print(m, flush=True))
    return _default


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m core.warp",
        description="Cloudflare WARP control (hub global IP rotate)",
    )
    p.add_argument(
        "command",
        choices=("status", "ip", "connect", "disconnect", "rotate"),
        help="Action",
    )
    p.add_argument(
        "--tries",
        type=int,
        default=None,
        help="rotate: max attempts (default WARP_MAX_IP_TRIES / 4)",
    )
    p.add_argument(
        "--no-keys",
        action="store_true",
        help="rotate: skip tunnel rotate-keys",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="rotate: ignore min interval skip",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="less step logging",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    log = (lambda _m: None) if args.quiet else (lambda m: print(m, flush=True))
    w = WarpClient(log=log)

    if args.command == "status":
        print(w.status_text())
        print(f"connected={w.is_connected()} ip={public_ip()}")
        return 0 if w.cli_path() else 127

    if args.command == "ip":
        print(public_ip())
        return 0

    if args.command == "connect":
        ok = w.ensure_connected()
        print(f"connected={ok} ip={public_ip()}")
        return 0 if ok else 1

    if args.command == "disconnect":
        ok = w.disconnect()
        print(f"disconnected={ok} status={w.status_text()[:80]}")
        return 0 if ok else 1

    # rotate
    result = w.rotate_ip(
        max_tries=args.tries,
        rotate_keys=False if args.no_keys else None,
        force=args.force,
    )
    print(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
