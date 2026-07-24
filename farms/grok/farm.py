#!/usr/bin/env python3
"""
Standalone Grok / xAI account farmer (CLI-only).

Flow per account:
  1. Generate email (catch-all / plus_trick IMAP, gptmail API, OR exzork API)
  2. Camoufox browser → accounts.x.ai/sign-up
  3. Email → OTP (IMAP or GPTMail API) → Confirm
  4. Name + password + Turnstile → Complete sign up
  5. Login if needed → Device OAuth (Grok CLI / grok2api-style) → tokens
  6. Append result to JSON + TXT (no poolprox DB)

Config: copy .env.example → .env then edit.
Run:    ./run.sh   or   python farm.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import imaplib
import json
import os
import random
import re
import secrets
import string
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email import message_from_bytes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, unquote

# Load .env from script directory.
# setdefault only — hub runner injects GROK_* from global Automation/.env first.
_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed. Run: ./install.sh", flush=True)
    sys.exit(1)

# ── Config from env ──────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()

def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")

IMAP_USER = _env("GROK_IMAP_USER")
IMAP_PASS = _env("GROK_IMAP_PASS").replace(" ", "")
IMAP_HOST = _env("GROK_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(_env("GROK_IMAP_PORT", "993") or "993")
EMAIL_DOMAIN = _env("GROK_EMAIL_DOMAIN").lstrip("@")
EMAIL_MODE = _env("GROK_EMAIL_MODE", "domain").lower()
# domain | plus_trick | gptmail | exzork (mailer.exzork.me REST — no IMAP)
if EMAIL_MODE not in ("plus_trick", "domain", "gptmail", "exzork"):
    EMAIL_MODE = "domain"
GMAIL_BASE = _env("GROK_GMAIL_BASE").lower() or IMAP_USER.lower()
# GPTMail (optional temp-mail provider; OTP via API)
GPTMAIL_API = _env("GROK_GPTMAIL_API", "https://mail.chatgpt.org.uk").rstrip("/")
GPTMAIL_DOMAIN = _env("GROK_GPTMAIL_DOMAIN").lstrip("@").lower()  # optional pin
GPTMAIL_PREFIX = _env("GROK_GPTMAIL_PREFIX").lower()  # optional local prefix
# Exzork temp-mail (custom domain + optional wildcard subdomain rotate)
EXZORK_API = _env("GROK_EXZORK_API", "https://mailer.exzork.me").rstrip("/")
EXZORK_API_KEY = _env("GROK_EXZORK_API_KEY")
_exzork_dom_raw = _env("GROK_EXZORK_DOMAIN") or EMAIL_DOMAIN
EXZORK_DOMAIN = _exzork_dom_raw.lstrip("@").lstrip("*.").lower()
EXZORK_WILDCARD = _env_bool("GROK_EXZORK_WILDCARD", True)
ACCOUNT_PASSWORD = _env("GROK_PASSWORD", "$Priyo000")
MAX_ACCOUNTS = int(_env("GROK_MAX_ACCOUNTS", "5") or "5")
CONCURRENT = int(_env("GROK_CONCURRENT", "1") or "1")
HEADLESS = _env_bool("GROK_HEADLESS", False)  # headed recommended for Turnstile
SPAWN_DELAY = float(_env("GROK_SPAWN_DELAY", "2") or "2")

# Hub global WARP every-N (enter-farm style). 0 = off.
# Injected by Automation runner as WARP_EVERY_N and/or GROK_WARP_EVERY_N.
WARP_EVERY_N = max(
    0,
    int(_env("GROK_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0") or 0,
)
# Seconds to wait after rotate before spawning new browsers (network settle).
WARP_SETTLE_S = max(3.0, float(_env("WARP_SETTLE_AFTER") or _env("GROK_WARP_SETTLE") or "8") or 8.0)
_success_since_warp = 0
_warp_rotate_lock = threading.Lock()
# In-flight account workers (for drain-before-rotate under concurrent > 1)
_in_flight = 0
_in_flight_lock: asyncio.Lock | None = None
_can_start: asyncio.Event | None = None
_warp_drain_owner: int | None = None  # only one drain/rotate at a time


def _ensure_async_gates() -> tuple[asyncio.Lock, asyncio.Event]:
    """Lazy-init asyncio primitives (need running loop)."""
    global _in_flight_lock, _can_start
    if _in_flight_lock is None:
        _in_flight_lock = asyncio.Lock()
    if _can_start is None:
        _can_start = asyncio.Event()
        _can_start.set()
    return _in_flight_lock, _can_start


# Fail-fast timeouts (stuck / unclear page states should free the worker slot)
OTP_TIMEOUT_S = max(30, int(_env("GROK_OTP_TIMEOUT", "120") or "120"))
# whole account hard deadline (signup+login+oauth)
ACCOUNT_TIMEOUT_S = max(120, int(_env("GROK_ACCOUNT_TIMEOUT", "480") or "480"))  # 8 min
# complete_signup turnstile+submit: max wall time before hard fail
# 120s default: concurrent load + CF often needs 2-3 solve/submit cycles
COMPLETE_SIGNUP_TIMEOUT_S = max(30, int(_env("GROK_COMPLETE_TIMEOUT", "120") or "120"))
CONFIRM_EMAIL_TIMEOUT_S = max(15, int(_env("GROK_CONFIRM_TIMEOUT", "45") or "45"))
# Max browsers solving Turnstile at once (same IP → CF "Verification failed" if all hammer)
# Default 1: direct IP + concurrent Camoufox often fails CF when >1 solve in parallel
TURNSTILE_PARALLEL = max(1, int(_env("GROK_TURNSTILE_PARALLEL", "1") or "1"))

def _effective_warp_every_n() -> int:
    """Wave mode: if everyN enabled, always 1:1 with live CONCURRENT (CLI may override env)."""
    if WARP_EVERY_N <= 0:
        return 0
    return max(1, int(CONCURRENT))

# Results root: each run creates results/batch_<id>/ (unless legacy single-file paths set)
RESULTS_ROOT = Path(_env("GROK_RESULTS_DIR", str(_ROOT / "results")))
USED_EMAILS_FILE = Path(_env("GROK_USED_EMAILS_FILE", str(RESULTS_ROOT / "used_emails.txt")))
# Domains rejected by xAI (gptmail) — permanent across runs
BLOCKED_DOMAINS_FILE = Path(
    _env("GROK_BLOCKED_DOMAINS_FILE", str(RESULTS_ROOT / "gptmail_blocked_domains.txt"))
)
# Optional legacy override: if any of these set, write to those fixed paths (no per-batch folder)
_LEGACY_JSON = _env("GROK_RESULTS_JSON")
_LEGACY_TXT = _env("GROK_RESULTS_TXT")
_LEGACY_FAILED = _env("GROK_FAILED_JSON")
EMAIL_LOCAL_LEN = max(10, min(32, int(_env("GROK_EMAIL_LOCAL_LEN", "16") or "16")))

# Set in init_batch() at run start
BATCH_ID = ""
BATCH_DIR: Path = RESULTS_ROOT
RESULTS_JSON: Path = RESULTS_ROOT / "accounts.json"
RESULTS_TXT: Path = RESULTS_ROOT / "accounts.txt"
FAILED_JSON: Path = RESULTS_ROOT / "failed.json"
SCREENSHOT_DIR = _env("GROK_SCREENSHOT_DIR", str(_ROOT / "screenshots"))
Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

# Optional vision CAPTCHA (interactive Turnstile puzzles) via OpenAI-compatible API
CAPTCHA_PROXY_URL = _env("GROK_CAPTCHA_PROXY_URL", "")
CAPTCHA_API_KEY = _env("GROK_CAPTCHA_API_KEY", "")
CAPTCHA_MODEL = _env("GROK_CAPTCHA_MODEL", "gpt-4o")

SIGNUP_URL = "https://accounts.x.ai/sign-up"
SIGNIN_URL = "https://accounts.x.ai/sign-in"

# Grok CLI OIDC (same as official Grok CLI / poolprox3)
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_AUTHORIZE = "https://auth.x.ai/oauth2/authorize"
XAI_TOKEN = "https://auth.x.ai/oauth2/token"
XAI_DEVICE_CODE = "https://auth.x.ai/oauth2/device/code"
XAI_DEVICE_VERIFY = "https://auth.x.ai/oauth2/device/verify"
XAI_DEVICE_APPROVE = "https://auth.x.ai/oauth2/device/approve"
XAI_REDIRECT_URI = "http://127.0.0.1:56121/callback"
# Device OAuth scope (grok2api default). PKCE consent currently returns Access denied.
XAI_SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access"
)
XAI_SCOPE_LEGACY = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)
GROK_FREE_TOKEN_LIMIT = 1_000_000

FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn", "Avery",
    "Parker", "Sage", "River", "Skyler", "Dakota", "Reese", "Finley", "Rowan",
    "Charlie", "Emerson", "Hayden", "Jamie", "Blake", "Drew", "Eden", "Kai",
    "Noah", "Liam", "Emma", "Olivia", "Mia", "Lucas", "Mason", "Sophia",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore",
    "Jackson", "Martin", "Lee", "Thompson", "White", "Harris", "Clark", "Lewis",
    "Walker", "Hall", "Allen", "Young", "King", "Wright", "Scott", "Green",
]

# ── Proxy pool ───────────────────────────────────────────────────────────────
# Sources (merged, de-duped by URL):
#   1) GROK_PROXY_FILE  — path to list file (default: ./proxies.txt if exists)
#   2) GROK_PROXY_POOL  — comma-separated URLs (optional #id suffix)
#   3) BATCHER_PROXY_URL — single proxy fallback
#
# Line formats in proxy file (blank / #comment ignored):
#   http://user:pass@host:port
#   socks5://user:pass@host:port
#   host:port
#   host:port:user:pass
#   user:pass@host:port
#   scheme://host:port#optional_id

def _normalize_proxy_url(raw: str) -> str | None:
    """Turn free-form proxy string into a URL Camoufox/Playwright accepts."""
    s = (raw or "").strip()
    if not s:
        return None
    # strip surrounding quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if not s:
        return None

    # Already has scheme
    if "://" in s:
        return s

    parts = s.split(":")
    # host:port:user:pass  (reseller format; pass may contain ':' or '@')
    # Detect before user:pass@host so passwords with @ still work.
    if len(parts) >= 4 and parts[1].isdigit() and "@" not in parts[0]:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        if host and user:
            return f"http://{user}:{password}@{host}:{port}"

    # user:pass@host:port
    if "@" in s:
        return f"http://{s}"

    # host:port
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"
    # bare host — reject (need port)
    return None


def _parse_proxy_entry(item: str) -> tuple[str, str] | None:
    """Parse one proxy entry → (url, optional_id) or None."""
    item = (item or "").strip()
    if not item or item.startswith("#"):
        return None
    # inline comment: url  # note  (but keep user:pass#weird if scheme present carefully)
    # Prefer optional id after last unquoted ' #' or trailing #id without space when URL has scheme
    pid = ""
    if " #" in item:
        item, _, comment = item.partition(" #")
        item = item.strip()
        pid = comment.strip()
    elif item.count("#") == 1 and "://" in item:
        # http://host:port#myid
        url_part, _, maybe_id = item.partition("#")
        item, pid = url_part.strip(), maybe_id.strip()
    url = _normalize_proxy_url(item)
    if not url:
        return None
    return (url, pid)


def _load_proxy_file(path: Path) -> list[tuple[str, str]]:
    """Load proxies — one per line (standard)."""
    if not path.is_file():
        return []
    out: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[proxy] WARN: cannot read {path}: {e}", flush=True)
        return []
    for lineno, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parsed = _parse_proxy_entry(raw)
        if parsed:
            out.append(parsed)
        else:
            print(f"[proxy] WARN: skip bad line {path.name}:{lineno}: {raw[:60]}", flush=True)
    return out


def _load_proxy_pool() -> tuple[list[tuple[str, str]], str]:
    """Return (pool, source_description)."""
    pool: list[tuple[str, str]] = []
    sources: list[str] = []

    # 1) File list
    file_env = _env("GROK_PROXY_FILE")
    if file_env:
        pfile = Path(file_env).expanduser()
        if not pfile.is_absolute():
            pfile = (_ROOT / pfile).resolve()
    else:
        pfile = (_ROOT / "proxies.txt").resolve()
    if pfile.is_file():
        loaded = _load_proxy_file(pfile)
        if loaded:
            pool.extend(loaded)
            sources.append(f"file:{pfile} ({len(loaded)})")
        elif file_env:
            print(f"[proxy] WARN: GROK_PROXY_FILE={pfile} empty or unreadable", flush=True)
    elif file_env:
        print(f"[proxy] WARN: GROK_PROXY_FILE not found: {pfile}", flush=True)

    # 2) Inline env pool
    raw = os.environ.get("GROK_PROXY_POOL", "").strip()
    if raw:
        n0 = len(pool)
        for item in raw.split(","):
            parsed = _parse_proxy_entry(item.strip())
            if parsed:
                pool.append(parsed)
        if len(pool) > n0:
            sources.append(f"GROK_PROXY_POOL (+{len(pool) - n0})")

    # 3) Single fallback
    if not pool and os.environ.get("BATCHER_PROXY_URL", "").strip():
        parsed = _parse_proxy_entry(os.environ["BATCHER_PROXY_URL"].strip())
        if parsed:
            pool.append(parsed)
            sources.append("BATCHER_PROXY_URL")

    # de-dupe by URL keep first id
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for url, pid in pool:
        if url in seen:
            continue
        seen.add(url)
        uniq.append((url, pid))

    if uniq and _env_bool("GROK_PROXY_SHUFFLE", False):
        random.shuffle(uniq)
        sources.append("shuffled")

    desc = ", ".join(sources) if sources else "direct (no proxy file/env)"
    return uniq, desc


PROXY_POOL, PROXY_SOURCE = _load_proxy_pool()
_proxy_idx = 0
_proxy_lock = asyncio.Lock()
# Cap concurrent Turnstile solves — shared IP gets CF "Verification failed" under hammer
_turnstile_sem: asyncio.Semaphore | None = None


def _get_turnstile_sem() -> asyncio.Semaphore:
    global _turnstile_sem
    if _turnstile_sem is None:
        _turnstile_sem = asyncio.Semaphore(TURNSTILE_PARALLEL)
    return _turnstile_sem


async def next_proxy():
    global _proxy_idx
    if not PROXY_POOL:
        return (None, "")
    async with _proxy_lock:
        url, pid = PROXY_POOL[_proxy_idx % len(PROXY_POOL)]
        _proxy_idx += 1
        return (url, pid)


def _parse_proxy(url: str) -> dict:
    if "://" not in url:
        url = f"http://{url}"
    u = urlparse(url)
    scheme = (u.scheme or "http").lower()
    server = f"{scheme}://{u.hostname}"
    if u.port:
        server += f":{u.port}"
    out: dict[str, Any] = {"server": server}
    if u.username:
        out["username"] = unquote(u.username)
    if u.password:
        out["password"] = unquote(u.password)
    return out


# ── Logging / HUD ────────────────────────────────────────────────────────────
_attempt_proxy: dict[int, str] = {}

# hud = progress panel (default on TTY); log = classic line spam
_UI_ENV = _env("GROK_UI", "").lower()
if _UI_ENV in ("hud", "tui", "progress"):
    UI_MODE = "hud"
elif _UI_ENV in ("log", "verbose", "full"):
    UI_MODE = "log"
else:
    UI_MODE = "hud" if sys.stdout.isatty() else "log"
VERBOSE = _env_bool("GROK_VERBOSE", False)  # force detail lines even under HUD


def _short_email(email: str, width: int = 28) -> str:
    e = (email or "").strip()
    if len(e) <= width:
        return e
    if "@" in e:
        local, _, dom = e.partition("@")
        keep = max(4, width - len(dom) - 4)
        return f"{local[:keep]}…@{dom}"[:width]
    return e[: width - 1] + "…"


def _bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "─" * width
    filled = int(width * min(done, total) / total)
    return "█" * filled + "░" * (width - filled)


class FarmHUD:
    """Compact terminal progress panel (not a full TUI app)."""

    def __init__(self) -> None:
        self.enabled = UI_MODE == "hud"
        self.total = 0
        self.ok = 0
        self.fail = 0
        self.batch_id = ""
        self.batch_dir = ""
        self.started = time.time()
        self._workers: dict[int, dict[str, Any]] = {}
        self._recent: list[str] = []
        self._slock = __import__("threading").Lock()
        self._drawn_lines = 0
        self._started_draw = False
        self._log_fp = None
        self._real_stdout = sys.stdout
        self._tick_task: asyncio.Task | None = None

    def open_log(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(path, "a", encoding="utf-8")
            self._log_fp.write(f"\n===== farm start {datetime.now(timezone.utc).isoformat()} =====\n")
            self._log_fp.flush()
        except Exception:
            self._log_fp = None

    def close_log(self) -> None:
        if self._log_fp:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def log_line(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {line}"
        if self._log_fp:
            try:
                self._log_fp.write(full + "\n")
                self._log_fp.flush()
            except Exception:
                pass
        if not self.enabled or VERBOSE:
            try:
                self._real_stdout.write(full + "\n")
                self._real_stdout.flush()
            except Exception:
                pass

    def start(self, total: int, batch_id: str = "", batch_dir: str = "") -> None:
        self.total = total
        self.ok = 0
        self.fail = 0
        self.batch_id = batch_id
        self.batch_dir = batch_dir
        self.started = time.time()
        self._workers.clear()
        self._recent.clear()
        self._drawn_lines = 0
        self._started_draw = False
        if self.enabled:
            try:
                # hide cursor
                self._real_stdout.write("\033[?25l")
                self._real_stdout.flush()
            except Exception:
                pass
        self.render(force=True)

    def stop(self) -> None:
        if self.enabled:
            try:
                self._real_stdout.write("\033[?25h")  # show cursor
                self._real_stdout.write("\n")
                self._real_stdout.flush()
            except Exception:
                pass

    def set_progress(self, attempt: int, step: str, message: str = "", email: str = "") -> None:
        with self._slock:
            now = time.time()
            w = self._workers.get(attempt)
            if not w:
                w = {
                    "attempt": attempt,
                    "email": email,
                    "step": step,
                    "message": message,
                    "t0": now,       # worker start
                    "step_t0": now,  # current step start
                }
            else:
                if step and step != w.get("step"):
                    w["step_t0"] = now  # reset age when step changes
                if email:
                    w["email"] = email
                w["step"] = step
                w["message"] = message
            w["updated"] = now
            self._workers[attempt] = w
        self.log_line(f"[{attempt}] {step:16} {message}" + (f"  <{email}>" if email else ""))
        self.render()

    def mark_ok(self, attempt: int, email: str, message: str = "ok") -> None:
        with self._slock:
            self.ok += 1
            self._workers.pop(attempt, None)
            self._recent.append(f"✓ #{attempt} {_short_email(email, 32)}")
            self._recent = self._recent[-5:]
        self.log_line(f"[{attempt}] OK               {message}  <{email}>")
        self.render(force=True)

    def mark_fail(self, attempt: int, message: str, error: str = "") -> None:
        with self._slock:
            self.fail += 1
            email = ""
            w = self._workers.pop(attempt, None)
            if w:
                email = w.get("email") or ""
            msg = (error or message or "fail")[:60]
            self._recent.append(f"✗ #{attempt} {msg}")
            self._recent = self._recent[-5:]
        self.log_line(f"[{attempt}] FAIL             {message}" + (f" ({error})" if error else ""))
        self.render(force=True)

    def _build_lines(self) -> list[str]:
        elapsed = int(time.time() - self.started)
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        et = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        done = self.ok + self.fail
        running = len(self._workers)
        pct = int(100 * done / self.total) if self.total else 0
        bar = _bar(done, self.total, 22)
        width = 62
        lines: list[str] = []
        title = f" Grok Farm  ·  batch {self.batch_id or '-'} "
        lines.append("╭" + title.center(width, "─")[:width] + "╮")
        lines.append(f"│ {bar}  {done:>3}/{self.total:<3}  {pct:>3}%".ljust(width + 1) + "│")
        lines.append(
            f"│ ok={self.ok}  fail={self.fail}  run={running}  elapsed {et}".ljust(width + 1) + "│"
        )
        lines.append("│" + "─" * width + "│")
        # active workers (sorted)
        workers = sorted(self._workers.values(), key=lambda x: x["attempt"])
        if not workers:
            lines.append("│  (idle — waiting for workers…)".ljust(width + 1) + "│")
        else:
            for w in workers[:8]:
                # show time ON CURRENT STEP (not whole worker) so stuck steps are obvious
                age = int(time.time() - w.get("step_t0", w.get("t0", time.time())))
                total = int(time.time() - w.get("t0", time.time()))
                em = _short_email(w.get("email") or "…", 22)
                step = (w.get("step") or "")[:12]
                line = f"│  #{w['attempt']:<3} {em:<22} {step:<12} {age:>3}s/{total}s"
                lines.append(line.ljust(width + 1) + "│")
            if len(workers) > 8:
                lines.append(f"│  … +{len(workers) - 8} more".ljust(width + 1) + "│")
        lines.append("│" + "─" * width + "│")
        if self._recent:
            for r in self._recent[-3:]:
                lines.append(f"│  {r}"[: width + 1].ljust(width + 1) + "│")
        else:
            lines.append("│  recent: —".ljust(width + 1) + "│")
        if self.batch_dir:
            bd = self.batch_dir
            if len(bd) > width - 6:
                bd = "…" + bd[-(width - 7) :]
            lines.append(f"│  out: {bd}".ljust(width + 1) + "│")
        lines.append("╰" + "─" * width + "╯")
        lines.append("  detail → batch farm.log  ·  GROK_UI=log for full spam")
        return lines

    def render(self, force: bool = False) -> None:
        if not self.enabled:
            return
        with self._slock:
            lines = self._build_lines()
            out = self._real_stdout
            try:
                if self._started_draw and self._drawn_lines > 0:
                    # move cursor up and redraw
                    out.write(f"\033[{self._drawn_lines}A")
                for line in lines:
                    out.write("\033[2K" + line + "\n")
                out.flush()
                self._drawn_lines = len(lines)
                self._started_draw = True
            except Exception:
                pass

    async def ticker(self) -> None:
        """Refresh elapsed clock while workers run."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if self.ok + self.fail >= self.total and not self._workers:
                    break
                self.render()
        except asyncio.CancelledError:
            return


HUD = FarmHUD()


def emit_progress(attempt: int, step: str, message: str, email_addr: str = "", **kwargs):
    email = email_addr or kwargs.get("email") or ""
    HUD.set_progress(attempt, step, message, email)


def emit_success(attempt: int, email_addr: str, message: str):
    HUD.mark_ok(attempt, email_addr, message)


def emit_failed(attempt: int, message: str, error: str = ""):
    HUD.mark_fail(attempt, message, error)


def vlog(msg: str, attempt: int | None = None) -> None:
    """Verbose/debug line — always to farm.log; terminal only if log mode or VERBOSE."""
    prefix = f"[{attempt}] " if attempt is not None else ""
    HUD.log_line(prefix + msg)


# ── Email uniqueness (crypto random + global used list across all batches) ───
_used_emails: set[str] = set()
_emails_lock = asyncio.Lock()

# ── GPTMail (mail.chatgpt.org.uk) — optional EMAIL_MODE=gptmail ─────────────
_gptmail_accounts: dict[str, dict[str, str]] = {}
_gptmail_lock = threading.Lock()
_gptmail_domains_cache: list[str] = []
_gptmail_domains_ts = 0.0
_gptmail_slot_domain: dict[int, str] = {}
_gptmail_domain_rr = 0
_gptmail_blocked_domains: set[str] = set()
_ALPHANUM = string.ascii_lowercase + string.digits


def _crypto_local_part(length: int) -> str:
    """Cryptographically strong local-part: secrets, not random.choices."""
    return "".join(secrets.choice(_ALPHANUM) for _ in range(length))


def _emails_from_accounts_json(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                e = (row.get("email") or "").lower().strip()
                if e:
                    out.add(e)
    except Exception:
        pass
    return out


def _load_used_emails():
    """Load every email ever farmed: used_emails.txt + all batch/legacy results."""
    global _used_emails
    _used_emails = set()

    # Global index (authoritative across batches)
    if USED_EMAILS_FILE.is_file():
        try:
            for line in USED_EMAILS_FILE.read_text(encoding="utf-8").splitlines():
                e = line.strip().lower()
                if e and not e.startswith("#"):
                    _used_emails.add(e)
        except Exception as e:
            print(f"[DEDUP] Could not read {USED_EMAILS_FILE}: {e}", flush=True)

    # Legacy single file at results root
    _used_emails |= _emails_from_accounts_json(RESULTS_ROOT / "accounts.json")

    # Every batch folder
    if RESULTS_ROOT.is_dir():
        for batch in sorted(RESULTS_ROOT.glob("batch_*")):
            if batch.is_dir():
                _used_emails |= _emails_from_accounts_json(batch / "accounts.json")

    # Explicit legacy path override (if different)
    if _LEGACY_JSON:
        _used_emails |= _emails_from_accounts_json(Path(_LEGACY_JSON))

    print(f"[DEDUP] {len(_used_emails)} unique email(s) known across all batches", flush=True)


def _persist_used_email(email: str) -> None:
    """Append to global used_emails.txt so later batches never reuse it."""
    e = email.lower().strip()
    if not e:
        return
    USED_EMAILS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(USED_EMAILS_FILE, "a", encoding="utf-8") as f:
        f.write(e + "\n")


def init_batch(max_accounts: int, concurrent: int) -> str:
    """Create a dedicated results folder for this run. Returns batch_id."""
    global BATCH_ID, BATCH_DIR, RESULTS_JSON, RESULTS_TXT, FAILED_JSON

    # Fixed paths if user forced legacy env vars
    if _LEGACY_JSON or _LEGACY_TXT or _LEGACY_FAILED:
        BATCH_ID = _env("GROK_BATCH_ID") or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        BATCH_DIR = RESULTS_ROOT
        RESULTS_JSON = Path(_LEGACY_JSON) if _LEGACY_JSON else RESULTS_ROOT / "accounts.json"
        RESULTS_TXT = Path(_LEGACY_TXT) if _LEGACY_TXT else RESULTS_ROOT / "accounts.txt"
        FAILED_JSON = Path(_LEGACY_FAILED) if _LEGACY_FAILED else RESULTS_ROOT / "failed.json"
        RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        print(f"[BATCH] legacy single-file mode batch_id={BATCH_ID}", flush=True)
        return BATCH_ID

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = secrets.token_hex(3)
    BATCH_ID = _env("GROK_BATCH_ID") or f"{stamp}_{short}"
    # sanitize
    BATCH_ID = re.sub(r"[^a-zA-Z0-9_.-]", "_", BATCH_ID)[:80]
    BATCH_DIR = RESULTS_ROOT / f"batch_{BATCH_ID}"
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON = BATCH_DIR / "accounts.json"
    RESULTS_TXT = BATCH_DIR / "accounts.txt"
    FAILED_JSON = BATCH_DIR / "failed.json"

    # empty batch files
    RESULTS_JSON.write_text("[]\n", encoding="utf-8")
    RESULTS_TXT.write_text("", encoding="utf-8")
    FAILED_JSON.write_text("[]\n", encoding="utf-8")
    meta = {
        "batch_id": BATCH_ID,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "email_mode": EMAIL_MODE,
        "email_domain": (
            EMAIL_DOMAIN
            if EMAIL_MODE == "domain"
            else (GPTMAIL_DOMAIN or "auto")
            if EMAIL_MODE == "gptmail"
            else (
                f"{'*' if EXZORK_WILDCARD else ''}{EXZORK_DOMAIN}"
                if EMAIL_MODE == "exzork"
                else None
            )
        ),
        "max_accounts": max_accounts,
        "concurrent": concurrent,
        "email_local_len": EMAIL_LOCAL_LEN,
    }
    (BATCH_DIR / "batch_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"[BATCH] id={BATCH_ID}", flush=True)
    print(f"[BATCH] dir={BATCH_DIR}", flush=True)
    return BATCH_ID


async def generate_email(worker_slot: int | None = None) -> str:
    """Unique email; domain/plus_trick local, gptmail/exzork via API claim.

    worker_slot: concurrent slot for gptmail sticky domain only.
    """
    async with _emails_lock:
        for _ in range(200):
            if EMAIL_MODE == "gptmail":
                loop = asyncio.get_event_loop()
                addr = await loop.run_in_executor(
                    None, lambda: create_gptmail_inbox(worker_slot)
                )
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
            if EMAIL_MODE == "exzork":
                loop = asyncio.get_event_loop()
                addr = await loop.run_in_executor(None, create_exzork_inbox)
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
            if EMAIL_MODE == "domain":
                if not EMAIL_DOMAIN:
                    raise RuntimeError("GROK_EMAIL_DOMAIN required for domain mode")
                # secrets-based alnum (not random.choices) + global used set
                name = _crypto_local_part(EMAIL_LOCAL_LEN)
                addr = f"{name}@{EMAIL_DOMAIN.lstrip('@')}"
            else:
                base = GMAIL_BASE or IMAP_USER
                if not base or "@" not in base:
                    raise RuntimeError("GROK_GMAIL_BASE / GROK_IMAP_USER required for plus_trick")
                user, _, domain = base.partition("@")
                user = user.split("+", 1)[0]
                tag_len = max(10, min(20, EMAIL_LOCAL_LEN))
                tag = _crypto_local_part(tag_len)
                addr = f"{user}+{tag}@{domain}"
            key = addr.lower()
            if key not in _used_emails:
                _used_emails.add(key)
                _persist_used_email(key)  # reserve immediately so other processes / future batches skip
                return addr
    raise RuntimeError("Could not generate unique email after 200 attempts")



def random_name() -> tuple[str, str]:
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


# ── IMAP OTP ─────────────────────────────────────────────────────────────────
# xAI confirmation codes look like "K35-1QR" / "W0H-75T" (subject: "{CODE} xAI confirmation code")
_XAI_CODE_RE = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", re.I)
# Subject almost always: "ABC-123 xAI confirmation code"
_XAI_SUBJ_CODE_RE = re.compile(
    r"^\s*([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI\s+confirmation", re.I
)
# Claimed OTPs across concurrent IMAP threads — one code per worker, never share
_claimed_otps_sync: set[str] = set()
_claimed_otps_lock = threading.Lock()


def _is_plausible_xai_otp(code: str) -> bool:
    """Accept real xAI codes; reject CSS noise (PER-100, RGB-255, PX-16).

    xAI codes are XXX-XXX alnum — often mixed (Y34-FHY) but ALSO pure alpha (WGJ-HKA).
    Do NOT require a digit.
    """
    code = (code or "").upper().strip()
    if not re.fullmatch(r"[A-Z0-9]{3}-[A-Z0-9]{3}", code):
        return False
    left, right = code.split("-", 1)
    # CSS-ish: all-alpha left + all-digit right (PER-100, EM-16) — reject
    if re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right):
        return False
    # all-digit both sides unlikely for xAI (and CSS-ish)
    if re.fullmatch(r"\d+", left) and re.fullmatch(r"\d+", right):
        return False
    if code in {"PER-100", "RGB-255", "PX-16", "EM-16", "REM-16", "MS-300", "MS-200"}:
        return False
    return True


def _extract_xai_code(subject: str, body: str) -> str | None:
    # 1) Prefer subject line — authoritative for xAI
    m = _XAI_SUBJ_CODE_RE.search(subject or "")
    if m:
        code = m.group(1).upper()
        if _is_plausible_xai_otp(code):
            return code
    # 2) Any XXX-XXX in subject
    for m in _XAI_CODE_RE.finditer(subject or ""):
        code = m.group(1).upper()
        if _is_plausible_xai_otp(code):
            return code
    # 3) Body plain-text only (strip style/script to avoid CSS PER-100 etc.)
    plain = body or ""
    plain = re.sub(r"<style[\s\S]*?</style>", " ", plain, flags=re.I)
    plain = re.sub(r"<script[\s\S]*?</script>", " ", plain, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    for m in _XAI_CODE_RE.finditer(plain):
        code = m.group(1).upper()
        if _is_plausible_xai_otp(code):
            return code
    # Fallback 6-digit (unlikely for xAI but keep)
    m = re.search(r"\b(\d{6})\b", plain)
    return m.group(1) if m else None


def read_otp_from_imap_sync(target_email: str, timeout: int = 180, since_ts: float | None = None) -> str | None:
    """Poll Gmail IMAP for xAI confirmation code addressed to target_email.

    Codes arrive from noreply@x.ai with subject like "K35-1QR xAI confirmation code".
    Catch-all domains forward into this inbox; match To/Delivered-To/body for the alias.
    Concurrent workers: each OTP code is claimed once (no double-use of same mail).
    """
    print(f"[IMAP] Waiting for xAI OTP to {target_email}...", flush=True)
    start = time.time()
    since_ts = since_ts or (start - 30)
    target_lower = target_email.lower()
    target_local = target_lower.split("@")[0]
    seen_uids: set[bytes] = set()

    while time.time() - start < timeout:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(IMAP_USER, IMAP_PASS)
            mail.select("INBOX")
            status, messages = mail.search(None, '(FROM "x.ai")')
            msg_ids = messages[0].split() if messages and messages[0] else []
            if not msg_ids:
                status, messages = mail.search(None, '(SUBJECT "confirmation code")')
                msg_ids = messages[0].split() if messages and messages[0] else []

            for mid in reversed(msg_ids[-40:]):
                if mid in seen_uids:
                    continue
                status, data = mail.fetch(mid, "(RFC822)")
                if not data or not data[0]:
                    continue
                msg = message_from_bytes(data[0][1])
                subject = msg.get("Subject", "") or ""
                to_addr = " ".join(
                    filter(
                        None,
                        [
                            msg.get("To", ""),
                            msg.get("Delivered-To", ""),
                            msg.get("X-Original-To", ""),
                            msg.get("Cc", ""),
                        ],
                    )
                ).lower()

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == "text/plain":
                            try:
                                body = part.get_payload(decode=True).decode("utf-8", "replace")
                            except Exception:
                                body = ""
                            if body:
                                break
                        if ct == "text/html" and not body:
                            try:
                                body = part.get_payload(decode=True).decode("utf-8", "replace")
                            except Exception:
                                body = ""
                else:
                    try:
                        body = msg.get_payload(decode=True).decode("utf-8", "replace")
                    except Exception:
                        body = str(msg.get_payload() or "")

                # Match recipient (catch-all alias, plus-trick, or body mention)
                # Prefer header match; body match only if subject is clearly xAI confirmation
                header_hit = target_lower in to_addr or target_local in to_addr
                body_l = body.lower()
                body_hit = target_lower in body_l or (
                    len(target_local) >= 8 and target_local in body_l
                )
                subj_is_xai = bool(_XAI_SUBJ_CODE_RE.search(subject) or re.search(
                    r"xAI\s+confirmation", subject or "", re.I
                ))
                if not header_hit and not (body_hit and subj_is_xai):
                    seen_uids.add(mid)
                    continue

                code = _extract_xai_code(subject, body)
                if code:
                    # Concurrent: claim code once so two workers don't take same OTP
                    with _claimed_otps_lock:
                        if code in _claimed_otps_sync:
                            seen_uids.add(mid)
                            continue
                        _claimed_otps_sync.add(code)
                    print(f"[IMAP] Found OTP: {code} for {target_email} (subj={subject[:60]!r})", flush=True)
                    try:
                        mail.store(mid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                    mail.logout()
                    return code
                seen_uids.add(mid)
            mail.logout()
        except Exception as e:
            print(f"[IMAP] Error: {e}", flush=True)
        time.sleep(4)
    print("[IMAP] Timeout waiting for OTP", flush=True)
    return None


# ── Vision CAPTCHA (interactive Turnstile puzzles) ───────────────────────────
def _resolve_captcha_api_key() -> str:
    return CAPTCHA_API_KEY or ""


def _call_vision_model(image_b64: str, prompt: str, timeout: int = 60) -> str | None:
    if not CAPTCHA_PROXY_URL:
        return None
    api_key = _resolve_captcha_api_key()
    if not api_key:
        print("[CAPTCHA] No API key for vision model", flush=True)
        return None
    payload = {
        "model": CAPTCHA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0,
    }
    req = urllib.request.Request(
        CAPTCHA_PROXY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
    except Exception as e:
        print(f"[CAPTCHA] Vision error: {e}", flush=True)
        return None


_VISION_TURNSTILE_PROMPT = """You are looking at a browser screenshot that may show a Cloudflare Turnstile
interactive challenge (image selection puzzle, not a simple checkbox).

If you see a visual challenge (select all images with X, click objects, etc.):
1. Identify the tiles/objects to click
2. Return click coordinates as percentages of the FULL PAGE screenshot:
   CLICK: x1%,y1% | x2%,y2% | ...
   where x and y are 0-100 relative to the full image.

If only a simple "Verify you are human" checkbox is visible:
  return exactly: CHECKBOX

If no captcha/challenge is visible:
  return exactly: NO_CAPTCHA

Do not invent coordinates for form fields."""


def _parse_vision_clicks(text: str) -> list[tuple[float, float]] | None:
    if not text:
        return None
    upper = text.strip().upper()
    if "NO_CAPTCHA" in upper or "CHECKBOX" in upper:
        return None
    clicks = []
    for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%\s*[, ]\s*(\d{1,3}(?:\.\d+)?)\s*%", text):
        x, y = float(m.group(1)), float(m.group(2))
        if 0 <= x <= 100 and 0 <= y <= 100:
            clicks.append((x, y))
    return clicks or None


# ── Browser helpers ──────────────────────────────────────────────────────────
async def launch_browser(proxy_url: str | None):
    kwargs: dict[str, Any] = {
        "headless": HEADLESS,
        "humanize": 0.5,
        "os": random.choice(["windows", "macos", "linux"]),
        "locale": "en-US",
        "geoip": True,
        "block_webrtc": True,
    }
    if proxy_url:
        kwargs["proxy"] = _parse_proxy(proxy_url)
    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(60000)
    return manager, browser, page


async def screenshot(page, attempt: int, tag: str):
    try:
        path = f"{SCREENSHOT_DIR}/grok_farm_{attempt}_{tag}.png"
        await page.screenshot(path=path, full_page=True)
        print(f"[{attempt}] screenshot: {path}", flush=True)
    except Exception as e:
        print(f"[{attempt}] screenshot fail: {e}", flush=True)


async def dismiss_cookie_banner(page) -> None:
    """Cookie modal (OneTrust + xAI custom) blocks Next/Login — accept early."""
    for sel in (
        "#onetrust-accept-btn-handler",
        "#onetrust-reject-all-handler",
        "#accept-recommended-btn-handler",
        'button:has-text("Accept All Cookies")',
        'button:has-text("Accept All")',
        'button:has-text("Reject All")',
    ):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=2000)
                await asyncio.sleep(0.4)
                return
        except Exception:
            continue
    try:
        # Prefer Accept All Cookies over Reject (xAI sometimes needs consent for auth)
        hit = await click_text_button(
            page,
            ["Accept All Cookies", "Accept All", "Accept all cookies", "Reject All", "Allow All"],
        )
        if hit:
            await asyncio.sleep(0.4)
            return
    except Exception:
        pass
    # Role-based fallback (React buttons without stable ids)
    for name in (r"Accept All Cookies", r"Accept All", r"Reject All"):
        try:
            loc = page.get_by_role("button", name=re.compile(name, re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2000)
                await asyncio.sleep(0.4)
                return
        except Exception:
            continue


async def click_text_button(page, keywords: list[str], exclude: list[str] | None = None) -> str | None:
    exclude = exclude or []
    # Prefer Playwright role/name matching (more reliable than raw DOM for React)
    for kw in keywords:
        try:
            loc = page.get_by_role("button", name=re.compile(rf"^{re.escape(kw)}$", re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                txt = (await loc.first.inner_text()).strip()
                if exclude and any(e.lower() in txt.lower() for e in exclude):
                    continue
                await loc.first.click()
                return txt
        except Exception:
            pass
        try:
            loc = page.get_by_role("button", name=re.compile(kw, re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                txt = (await loc.first.inner_text()).strip()
                if exclude and any(e.lower() in txt.lower() for e in exclude):
                    continue
                # Avoid social providers when looking for email actions
                if exclude and any(e.lower() in txt.lower() for e in exclude):
                    continue
                await loc.first.click()
                return txt
        except Exception:
            pass

    exclude_re = re.compile("|".join(re.escape(e) for e in exclude), re.I) if exclude else None
    return await page.evaluate(
        """({keywords, exclude}) => {
            const den = exclude ? new RegExp(exclude, 'i') : null;
            const btns = [...document.querySelectorAll('button, a, [role="button"], input[type="submit"]')];
            // Prefer exact match first
            for (const preferExact of [true, false]) {
              for (const b of btns) {
                const txt = (b.innerText || b.textContent || b.value || '').trim();
                if (!txt) continue;
                const rect = b.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                if (den && den.test(txt)) continue;
                // Skip OneTrust / cookie UI
                if (b.id && b.id.includes('onetrust')) continue;
                if ((b.className || '').toString().includes('onetrust')) continue;
                const low = txt.toLowerCase();
                for (const kw of keywords) {
                    const k = kw.toLowerCase();
                    const hit = preferExact ? (low === k) : (low === k || low.includes(k));
                    if (hit) {
                        b.click();
                        return txt;
                    }
                }
              }
            }
            return null;
        }""",
        {"keywords": keywords, "exclude": exclude_re.pattern if exclude_re else ""},
    )


async def fill_input(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() == 0:
                continue
            if not await el.is_visible():
                continue
            await el.click()
            await el.fill("")
            await el.fill(value)
            # React-friendly events
            await el.evaluate(
                """(el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            return True
        except Exception:
            continue
    # JS fallback
    try:
        ok = await page.evaluate(
            """({selectors, value}) => {
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                return false;
            }""",
            {"selectors": selectors, "value": value},
        )
        return bool(ok)
    except Exception:
        return False


async def turnstile_token_len(page) -> int:
    try:
        return int(
            await page.evaluate(
                """() => {
                    const el = document.querySelector('[name="cf-turnstile-response"], [name="cf-turnstile-response"] input, textarea[name="cf-turnstile-response"]');
                    if (el && el.value) return el.value.length;
                    const inputs = document.querySelectorAll('input[type="hidden"]');
                    for (const i of inputs) {
                        if ((i.name || '').includes('turnstile') && i.value) return i.value.length;
                    }
                    return 0;
                }"""
            )
            or 0
        )
    except Exception:
        return 0


async def turnstile_visible(page) -> bool:
    try:
        if await turnstile_token_len(page) > 20:
            return False  # solved
        # Text label "Verify you are human" / cloudflare widget
        n = await page.locator(
            "text=Verify you are human, iframe[src*='challenges.cloudflare'], iframe[src*='turnstile'], [data-sitekey]"
        ).count()
        if n > 0:
            return True
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or "") or "turnstile" in (f.url or ""):
                return True
    except Exception:
        pass
    return False


async def try_click_turnstile(page, attempt: int) -> bool:
    """Humanized click on Cloudflare Turnstile managed checkbox."""
    try:
        # 1) Click by accessible text on host page (managed widget often projects this)
        for sel in (
            'text=Verify you are human',
            'label:has-text("Verify you are human")',
            '[aria-label*="Verify you are human" i]',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    box = await loc.bounding_box(timeout=2000)
                    if box:
                        x = box["x"] + min(18, box["width"] * 0.15)
                        y = box["y"] + box["height"] / 2
                        await page.mouse.move(x - 40, y - 20, steps=8)
                        await asyncio.sleep(random.uniform(0.15, 0.4))
                        await page.mouse.move(x, y, steps=10)
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        await page.mouse.click(x, y)
                        print(f"[{attempt}] Turnstile: clicked host text ({sel})", flush=True)
                        return True
            except Exception:
                continue

        # 2) Click left side of turnstile container / iframe
        for sel in (
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            "[data-sitekey]",
            'div:has(iframe[src*="challenges.cloudflare"])',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                box = await loc.bounding_box(timeout=2000)
                if not box:
                    continue
                x = box["x"] + min(28, max(12, box["width"] * 0.12))
                y = box["y"] + box["height"] / 2
                await page.mouse.move(x - 50, y - 25, steps=8)
                await asyncio.sleep(random.uniform(0.15, 0.4))
                await page.mouse.move(x, y, steps=12)
                await asyncio.sleep(random.uniform(0.25, 0.6))
                await page.mouse.click(x, y)
                print(f"[{attempt}] Turnstile: clicked container {sel}", flush=True)
                return True
            except Exception:
                continue

        # 3) Inside CF frames — checkbox selectors
        for f in page.frames:
            if "challenges.cloudflare.com" not in (f.url or "") and "turnstile" not in (f.url or ""):
                continue
            for sel in (
                'input[type="checkbox"]',
                "label.cb-lb input",
                'label input[type="checkbox"]',
                '[role="checkbox"]',
                "body",
            ):
                try:
                    loc = f.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    box = await loc.bounding_box(timeout=2000)
                    if not box:
                        continue
                    tx = box["x"] + min(20, box["width"] * 0.2)
                    ty = box["y"] + box["height"] / 2
                    await page.mouse.move(tx, ty, steps=12)
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    await page.mouse.click(tx, ty)
                    print(f"[{attempt}] Turnstile: clicked frame {sel}", flush=True)
                    return True
                except Exception:
                    continue
    except Exception as e:
        print(f"[{attempt}] Turnstile click error: {e}", flush=True)
    return False


async def _turnstile_mount_present(page) -> bool:
    """True if page has a Turnstile mount/placeholder even when iframe not ready yet."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                    if (document.querySelector('[data-sitekey], .cf-turnstile, #cf-turnstile, [name="cf-turnstile-response"]'))
                        return true;
                    const ifr = document.querySelectorAll('iframe');
                    for (const f of ifr) {
                        const s = (f.src || '') + (f.getAttribute('src') || '');
                        if (s.includes('challenges.cloudflare') || s.includes('turnstile')) return true;
                    }
                    // grey empty box under password on complete form is often the mount
                    const t = (document.body && document.body.innerText) || '';
                    if (/Verify you are human/i.test(t)) return true;
                    // Detect blank CF placeholder: wide short box above Complete button
                    const btns = Array.from(document.querySelectorAll('button'));
                    const complete = btns.find(b => /complete\\s+sign\\s*up/i.test((b.innerText||'').trim()));
                    if (complete) {
                        const br = complete.getBoundingClientRect();
                        const nodes = document.querySelectorAll('div, section, span');
                        for (const el of nodes) {
                            const r = el.getBoundingClientRect();
                            if (r.width < 200 || r.width > 420) continue;
                            if (r.height < 40 || r.height > 90) continue;
                            // sits just above Complete button
                            if (r.bottom <= br.top && (br.top - r.bottom) < 40 && r.bottom > br.top - 100) {
                                return true;
                            }
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


async def _click_turnstile_slot_above_complete(page, attempt: int) -> bool:
    """Click the blank Turnstile slot that sits just above 'Complete sign up'."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"complete\s+sign\s*up", re.I)).first
        if await btn.count() == 0:
            return False
        box = await btn.bounding_box(timeout=2000)
        if not box:
            return False
        # Widget is a ~300x65 grey box immediately above the button
        x = box["x"] + min(28, box["width"] * 0.12)
        y = box["y"] - 36
        if y < 8:
            return False
        await page.mouse.move(x - 30, y - 10, steps=6)
        await asyncio.sleep(random.uniform(0.1, 0.25))
        await page.mouse.move(x, y, steps=8)
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await page.mouse.click(x, y)
        print(f"[{attempt}] Turnstile: clicked slot above Complete ({x:.0f},{y:.0f})", flush=True)
        return True
    except Exception as e:
        print(f"[{attempt}] Turnstile slot click warn: {e}", flush=True)
        return False


async def _turnstile_verification_failed(page) -> bool:
    """True when CF shows red 'Verification failed' / Troubleshoot widget."""
    try:
        if await page.locator("text=/Verification failed/i").count() > 0:
            return True
        if await page.locator("text=/Troubleshoot/i").count() > 0:
            # Troubleshoot alone can be false positive; require nearby CF context
            body = (await page.inner_text("body"))[:2500]
            if re.search(r"Verification failed|CLOUDFLARE", body, re.I):
                return True
        return False
    except Exception:
        return False


async def _force_turnstile_remount(
    page, attempt: int, password: str | None = None, *, hard: bool = False
) -> None:
    """Recover blank / 'Verification failed' Turnstile.

    Soft (default): turnstile.reset() + password re-poke (does NOT rip iframes).
    Hard: remove dead CF iframes (last resort — can leave blank box if React won't remount).
    """
    mode = "hard" if hard else "soft"
    print(f"[{attempt}] Turnstile: remount ({mode})", flush=True)

    # Click CF "Troubleshoot" / retry if verification failed
    try:
        if await page.locator("text=/Verification failed/i").count() > 0:
            for sel in (
                'text=Troubleshoot',
                'a:has-text("Troubleshoot")',
                'text=/try again/i',
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    continue
    except Exception:
        pass

    try:
        await page.evaluate(
            """(hard) => {
                try {
                    if (window.turnstile && typeof window.turnstile.reset === 'function') {
                        window.turnstile.reset();
                    }
                } catch (e) {}
                document.querySelectorAll(
                    '[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name*="turnstile"]'
                ).forEach(el => { try { el.value = ''; } catch (e) {} });
                if (hard) {
                    document.querySelectorAll(
                        'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]'
                    ).forEach(f => { try { f.remove(); } catch (e) {} });
                }
            }""",
            hard,
        )
    except Exception as e:
        print(f"[{attempt}] Turnstile remount JS warn: {e}", flush=True)

    # Password re-focus often re-triggers CF mount on complete form
    if password:
        try:
            loc = page.locator('input[type="password"]').first
            if await loc.count() > 0:
                await loc.click(timeout=2000)
                await asyncio.sleep(0.15)
                await loc.fill("")
                await loc.fill(password)
                await loc.evaluate(
                    """(el, v) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }""",
                    password,
                )
        except Exception:
            pass
    # Give CF time to re-fetch challenge (concurrent IP needs breathing room)
    await asyncio.sleep(2.5 if hard else 2.0)


async def _on_complete_signup_form(page) -> bool:
    """True while 'Complete your sign up' profile step is still showing."""
    try:
        if await page.locator("text=Complete your sign up").count() > 0:
            return True
        # Fallback: Complete button + password still present
        has_btn = await page.get_by_role(
            "button", name=re.compile(r"complete\s+sign\s*up", re.I)
        ).count()
        has_pw = await page.locator('input[type="password"]').count()
        return has_btn > 0 and has_pw > 0
    except Exception:
        return False


async def handle_turnstile(
    page,
    attempt: int,
    max_wait: float = 35.0,
    *,
    require_token: bool = False,
    password: str | None = None,
    use_global_limit: bool = False,
    allow_remount: bool = True,
) -> bool:
    """Camoufox auto-pass → managed checkbox click → vision for interactive puzzles.

    require_token=True: used on Complete sign-up — blank widget means NOT ready,
    never treat absence of iframe as success.
    use_global_limit=True: acquire TURNSTILE_PARALLEL semaphore (concurrent farm).
    allow_remount=False: click-only (old complete_signup style — no soft/hard remount).
    """
    if use_global_limit:
        async with _get_turnstile_sem():
            return await _handle_turnstile_inner(
                page,
                attempt,
                max_wait,
                require_token=require_token,
                password=password,
                allow_remount=allow_remount,
            )
    return await _handle_turnstile_inner(
        page,
        attempt,
        max_wait,
        require_token=require_token,
        password=password,
        allow_remount=allow_remount,
    )


async def _handle_turnstile_inner(
    page,
    attempt: int,
    max_wait: float = 35.0,
    *,
    require_token: bool = False,
    password: str | None = None,
    allow_remount: bool = True,
) -> bool:
    deadline = time.monotonic() + max_wait
    clicks = 0
    remounts = 0
    while time.monotonic() < deadline:
        tok = await turnstile_token_len(page)
        if tok > 20:
            print(f"[{attempt}] Turnstile: token present (len={tok})", flush=True)
            return True

        # CF hard-fail widget — must remount, clicking forever does nothing
        if await _turnstile_verification_failed(page):
            if allow_remount and remounts < 4:
                await _force_turnstile_remount(
                    page, attempt, password, hard=(remounts >= 1)
                )
                remounts += 1
                clicks = 0
                continue
            if not allow_remount:
                # old style: keep clicking; don't abort early on CF fail banner
                await try_click_turnstile(page, attempt)
                await _click_turnstile_slot_above_complete(page, attempt)
                clicks += 1
                await asyncio.sleep(2.0)
                continue
            print(f"[{attempt}] Turnstile: Verification failed (remounts exhausted)", flush=True)
            return False

        visible = await turnstile_visible(page)
        mounted = await _turnstile_mount_present(page)
        if not visible and not mounted:
            if require_token:
                # Wait longer before aggressive clicking — blank mount often still loading
                if clicks == 0:
                    await asyncio.sleep(2.0)
                if clicks < 6:
                    await _click_turnstile_slot_above_complete(page, attempt)
                    await try_click_turnstile(page, attempt)
                    clicks += 1
                # blank for a long time → soft then hard remount (login path)
                if (
                    allow_remount
                    and clicks >= 3
                    and remounts < 3
                    and (deadline - time.monotonic()) > 10
                ):
                    await _force_turnstile_remount(
                        page, attempt, password, hard=(remounts >= 1)
                    )
                    remounts += 1
                    clicks = 0
                await asyncio.sleep(1.5)
                continue
            # Other pages may not require it
            return True
        if not visible and mounted:
            # Widget still loading (blank grey box) — wait first, then poke
            if clicks == 0:
                await asyncio.sleep(2.5)  # CF under concurrent IP is slow
            if clicks < 4:
                await _click_turnstile_slot_above_complete(page, attempt)
                await try_click_turnstile(page, attempt)
                clicks += 1
            if (
                allow_remount
                and clicks >= 3
                and remounts < 3
                and (deadline - time.monotonic()) > 10
            ):
                await _force_turnstile_remount(
                    page, attempt, password, hard=(remounts >= 1)
                )
                remounts += 1
                clicks = 0
            await asyncio.sleep(1.5)
            continue

        # Widget still needs interaction
        if clicks < 6:
            await try_click_turnstile(page, attempt)
            await _click_turnstile_slot_above_complete(page, attempt)
            clicks += 1
            await asyncio.sleep(2.5)
            if await turnstile_token_len(page) > 20:
                print(f"[{attempt}] Turnstile: solved after click", flush=True)
                return True
            continue

        # Still blocked — vision for interactive image challenge
        try:
            img = await page.screenshot(full_page=True)
            b64 = base64.b64encode(img).decode("ascii")
            resp = _call_vision_model(b64, _VISION_TURNSTILE_PROMPT)
            if resp:
                print(f"[{attempt}] Turnstile vision: {resp[:120]}", flush=True)
                upper = resp.strip().upper()
                if "NO_CAPTCHA" in upper:
                    # only trust if no mount / token already ok
                    if not await _turnstile_mount_present(page) or await turnstile_token_len(page) > 20:
                        return True
                if "CHECKBOX" in upper:
                    await try_click_turnstile(page, attempt)
                    await asyncio.sleep(2.0)
                    clicks = 0
                    continue
                coords = _parse_vision_clicks(resp)
                if coords:
                    try:
                        size = await page.evaluate(
                            "() => ({w: Math.max(document.documentElement.scrollWidth, window.innerWidth), h: Math.max(document.documentElement.scrollHeight, window.innerHeight)})"
                        )
                        w, h = size["w"], size["h"]
                    except Exception:
                        vp = page.viewport_size or {"width": 1280, "height": 800}
                        w, h = vp["width"], vp["height"]
                    for px, py in coords:
                        await page.mouse.click((px / 100.0) * w, (py / 100.0) * h)
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                    await asyncio.sleep(2.0)
                    continue
        except Exception as e:
            print(f"[{attempt}] Turnstile vision fail: {e}", flush=True)

        await asyncio.sleep(1.2)

    tok = await turnstile_token_len(page)
    if tok > 20:
        return True
    print(f"[{attempt}] Turnstile: timeout after {max_wait}s (token_len={tok})", flush=True)
    return False


# ── OIDC helpers (Device OAuth preferred; PKCE kept as fallback) ─────────────
def generate_pkce_pair() -> tuple[str, str]:
    raw = secrets.token_bytes(96)
    verifier = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def extract_code_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost"):
        return None
    if "/callback" not in (parsed.path or "") and "code=" not in url:
        return None
    params = parse_qs(parsed.query)
    vals = params.get("code")
    return vals[0] if vals else None


def _oauth_post_form(url: str, form: dict[str, str], timeout: float = 30.0) -> dict:
    body = urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"_raw": raw, "error": f"http_{e.code}"}
        data["_http_status"] = e.code
        raise RuntimeError(f"oauth POST {url} failed: {data}") from e


def _tokens_from_oauth_json(data: dict, email_fallback: str = "", auth_mode: str = "device_oauth") -> dict:
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if not access or not refresh:
        raise RuntimeError(f"token response missing tokens: {list(data.keys())}")
    expires_in = int(data.get("expires_in") or 21600)
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in
    expires_at_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
    email = email_fallback
    id_token = data.get("id_token") or ""
    if id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            email = payload.get("email") or email
        except Exception:
            pass
    tokens = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at_iso,
        "expires_in": expires_in,
        "email": email,
        "client_id": XAI_CLIENT_ID,
        "auth_mode": auth_mode,
        "scope": data.get("scope") or XAI_SCOPE,
    }
    if id_token:
        tokens["id_token"] = id_token
    return tokens


def start_device_authorization() -> dict:
    """xAI Device OAuth — same as grok2api / pi / opencode."""
    form = {"client_id": XAI_CLIENT_ID, "scope": XAI_SCOPE}
    # pi client sends referrer; some xAI builds expect a non-empty app tag
    ref = _env("GROK_OAUTH_REFERRER", "grok-cli")
    if ref:
        form["referrer"] = ref
    return _oauth_post_form(XAI_DEVICE_CODE, form)


def _cookie_header_from_playwright(cookies: list[dict]) -> str:
    """Build Cookie header for auth.x.ai + accounts.x.ai."""
    parts: list[str] = []
    for c in cookies or []:
        name = c.get("name") or ""
        val = c.get("value")
        if not name or val is None:
            continue
        dom = (c.get("domain") or "").lower().lstrip(".")
        # device approve needs SSO session cookies
        if dom.endswith("x.ai") or "x.ai" in dom:
            parts.append(f"{name}={val}")
    # de-dupe keep last
    seen: dict[str, str] = {}
    for p in parts:
        k, _, v = p.partition("=")
        seen[k] = v
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def device_verify_and_approve_http(user_code: str, cookie_header: str) -> tuple[bool, str]:
    """POST device/verify + device/approve (grok2api SSO path). Returns (ok, detail)."""
    if not user_code or not cookie_header:
        return False, "missing user_code or cookies"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/json,*/*",
        "User-Agent": "grok-farm/device-oauth",
        "Cookie": cookie_header,
        "Origin": "https://accounts.x.ai",
        "Referer": "https://accounts.x.ai/",
    }
    detail_parts: list[str] = []
    for url, form in (
        (XAI_DEVICE_VERIFY, {"user_code": user_code}),
        (
            XAI_DEVICE_APPROVE,
            {
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
        ),
    ):
        body = urlencode(form).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                final = resp.geturl()
                raw = resp.read(512).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            final = e.geturl() if hasattr(e, "geturl") else url
            raw = e.read(512).decode("utf-8", "replace")
        except Exception as e:
            return False, f"{url} err={e}"
        detail_parts.append(f"{url.split('/')[-1]}:{status}:{final[-60:]}")
        if status >= 400:
            return False, f"{url} HTTP {status} {raw[:120]}"
    return True, " | ".join(detail_parts)


def poll_device_code(
    device_code: str,
    interval_s: float = 5.0,
    timeout_s: float = 180.0,
    *,
    wait_before_first: bool = True,
) -> dict:
    """RFC 8628 device-code poll. Wait one interval before first poll (pi/opencode)."""
    deadline = time.monotonic() + timeout_s
    sleep_s = max(3.0, float(interval_s or 5))
    if wait_before_first:
        time.sleep(sleep_s)
    while time.monotonic() < deadline:
        body = urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": XAI_CLIENT_ID,
                "device_code": device_code,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            XAI_TOKEN,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "grok-farm/device-oauth",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except Exception:
                data = {"error": f"http_{e.code}", "_raw": raw}
        if data.get("access_token"):
            return data
        err = (data.get("error") or "").lower()
        if err == "authorization_pending":
            time.sleep(sleep_s)
            continue
        if err == "slow_down":
            sleep_s = min(30.0, sleep_s + 5.0)
            time.sleep(sleep_s)
            continue
        if err in ("access_denied", "expired_token", "invalid_grant"):
            raise RuntimeError(f"device poll denied: {data}")
        # unknown error — brief backoff then continue until timeout
        print(f"[oauth] device poll unexpected: {data}", flush=True)
        time.sleep(sleep_s)
    raise TimeoutError("device code poll timed out")


def exchange_code_for_tokens(code: str, verifier: str) -> dict:
    data = _oauth_post_form(
        XAI_TOKEN,
        {
            "grant_type": "authorization_code",
            "client_id": XAI_CLIENT_ID,
            "code": code,
            "redirect_uri": XAI_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    return _tokens_from_oauth_json(data, auth_mode="oidc_pkce")


# ── Signup / login UI ────────────────────────────────────────────────────────
async def _read_xai_otp_value(page) -> str:
    """Read current OTP from root input or multi-slot boxes (alnum only)."""
    try:
        raw = await page.evaluate(
            """() => {
                const norm = (s) => (s || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
                const roots = [...document.querySelectorAll(
                    'input[name="code"], input[autocomplete="one-time-code"]'
                )];
                for (const el of roots) {
                    const v = norm(el.value);
                    if (v.length >= 3) return v;
                }
                const slots = [...document.querySelectorAll('input[maxlength="1"]')];
                if (slots.length) return norm(slots.map(s => s.value || '').join(''));
                if (roots[0]) return norm(roots[0].value);
                return '';
            }"""
        )
        return (raw or "").upper()
    except Exception:
        return ""


async def _otp_form_broken(page) -> bool:
    """Zod/React broken state after bad value clear: 'expected string, received undefined'."""
    try:
        n = await page.locator("text=/expected string, received undefined/i").count()
        return n > 0
    except Exception:
        return False


async def _otp_inputs_ready(page) -> bool:
    """True when verify page has a focusable OTP control."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const pick = document.querySelector(
                        'input[name="code"], input[autocomplete="one-time-code"], input[maxlength="1"]'
                    );
                    if (!pick) return false;
                    if (pick.disabled) return false;
                    return true;
                }"""
            )
        )
    except Exception:
        return False


async def _type_alnum(page, text: str, delay_ms: int = 40) -> None:
    """Type alnum only; prefer press_sequentially when available."""
    text = re.sub(r"[^A-Za-z0-9]", "", text or "")
    if not text:
        return
    # Playwright press_sequentially is more reliable than keyboard.type on React OTP
    try:
        focused = page.locator("input:focus").first
        if await focused.count() > 0:
            await focused.press_sequentially(text, delay=delay_ms)
            return
    except Exception:
        pass
    try:
        await page.keyboard.type(text, delay=delay_ms)
    except Exception:
        for ch in text:
            try:
                await page.keyboard.press(ch)
            except Exception:
                await page.keyboard.insert_text(ch)


async def fill_xai_otp_boxes(page, otp_chars: str, attempt: int) -> bool:
    """Fill xAI multi-segment OTP (6 alnum chars, UI shows XXX-XXX).

    Critical (from VPS screenshots):
      - NEVER set input.value = '' via JS — React/Zod ends as undefined
      - NEVER type the visual hyphen — only 6 alnum keystrokes
      - Always verify all 6 chars landed before returning True
      - Retry a few rounds — ~15% flakiness is timing/focus, not permanent fail
    """
    otp_chars = re.sub(r"[^A-Za-z0-9]", "", otp_chars or "").upper()
    if not otp_chars:
        return False
    if len(otp_chars) != 6:
        print(f"[{attempt}] WARN: OTP length {len(otp_chars)} (want 6): {otp_chars}", flush=True)

    # Hard ceiling so fill_otp never idles until account timeout
    fill_deadline = time.monotonic() + 28.0
    max_rounds = 3

    async def _verified() -> bool:
        val = await _read_xai_otp_value(page)
        ok = val == otp_chars
        if ok:
            print(f"[{attempt}] OTP verified value={val!r}", flush=True)
        return ok

    async def _soft_keyboard_clear() -> None:
        # Keyboard-only clear (safe for React). No value=''.
        for key in ("Control+a", "Meta+a"):
            try:
                await page.keyboard.press(key)
            except Exception:
                pass
        for _ in range(8):
            try:
                await page.keyboard.press("Backspace")
            except Exception:
                break

    async def _focus_otp() -> bool:
        # Prefer visible multi-slot first (what user sees), then root controller
        for sel in (
            'input[maxlength="1"]',
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
        ):
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                try:
                    await loc.click(timeout=2000, force=False)
                    return True
                except Exception:
                    try:
                        await loc.click(timeout=1200, force=True)
                        return True
                    except Exception:
                        try:
                            await loc.focus(timeout=1000)
                            return True
                        except Exception:
                            continue
            except Exception:
                continue
        try:
            await page.evaluate(
                """() => {
                    const el = document.querySelector(
                        'input[maxlength="1"], input[name="code"], input[autocomplete="one-time-code"]'
                    );
                    if (el) el.focus();
                }"""
            )
            return True
        except Exception:
            return False

    async def _strategy_keyboard() -> bool:
        if not await _focus_otp():
            return False
        await asyncio.sleep(0.08)
        await _soft_keyboard_clear()
        await _type_alnum(page, otp_chars, delay_ms=35)
        await asyncio.sleep(0.2)
        val = await _read_xai_otp_value(page)
        print(f"[{attempt}] OTP keyboard value={val!r}", flush=True)
        return await _verified()

    async def _strategy_per_slot() -> bool:
        slots = page.locator('input[maxlength="1"]')
        n = await slots.count()
        if n < 1:
            return False
        # Click first, type full sequence (auto-advance)
        try:
            await slots.first.click(timeout=2000)
        except Exception:
            try:
                await slots.first.click(timeout=1000, force=True)
            except Exception:
                return False
        await asyncio.sleep(0.05)
        await _soft_keyboard_clear()
        await _type_alnum(page, otp_chars[:6], delay_ms=40)
        await asyncio.sleep(0.2)
        if await _verified():
            return True
        # Explicit per-slot: click each box, one char (no JS value setter)
        for i, ch in enumerate(otp_chars[: min(6, n)]):
            if time.monotonic() >= fill_deadline:
                break
            try:
                slot = slots.nth(i)
                await slot.click(timeout=1200)
                await page.keyboard.press("Backspace")
                await page.keyboard.type(ch, delay=30)
            except Exception:
                continue
        await asyncio.sleep(0.2)
        val = await _read_xai_otp_value(page)
        print(f"[{attempt}] OTP per-slot n={n} value={val!r}", flush=True)
        return await _verified()

    async def _strategy_paste() -> bool:
        # Paste last — some builds handle paste well; avoid native value='' clear
        if not await _focus_otp():
            return False
        try:
            ok = await page.evaluate(
                """(code) => {
                    const el = document.querySelector(
                        'input[name="code"], input[autocomplete="one-time-code"]'
                    ) || document.querySelector('input[maxlength="1"]');
                    if (!el) return false;
                    el.focus();
                    try {
                        const dt = new DataTransfer();
                        dt.setData('text/plain', code);
                        el.dispatchEvent(new ClipboardEvent('paste', {
                            bubbles: true, cancelable: true, clipboardData: dt
                        }));
                        return true;
                    } catch (e) {
                        return false;
                    }
                }""",
                otp_chars,
            )
            await asyncio.sleep(0.25)
            if ok and await _verified():
                print(f"[{attempt}] OTP filled via paste", flush=True)
                return True
        except Exception as e:
            print(f"[{attempt}] OTP paste warn: {e}", flush=True)
        return False

    # Wait briefly for controls to be ready (page can be stale after long IMAP wait)
    ready_deadline = time.monotonic() + 5.0
    while time.monotonic() < ready_deadline:
        if await _otp_inputs_ready(page):
            break
        await asyncio.sleep(0.3)
    else:
        print(f"[{attempt}] OTP inputs not ready after wait", flush=True)

    for round_i in range(1, max_rounds + 1):
        if time.monotonic() >= fill_deadline:
            break
        print(f"[{attempt}] OTP fill round {round_i}/{max_rounds}", flush=True)
        try:
            await dismiss_cookie_banner(page)
        except Exception:
            pass

        # Order: keyboard → per-slot → paste (JS setter removed — it caused Zod undefined)
        for name, strat in (
            ("keyboard", _strategy_keyboard),
            ("per_slot", _strategy_per_slot),
            ("paste", _strategy_paste),
        ):
            if time.monotonic() >= fill_deadline:
                break
            try:
                if await strat():
                    print(f"[{attempt}] OTP ok via {name} (round {round_i})", flush=True)
                    return True
            except Exception as e:
                print(f"[{attempt}] OTP {name} warn: {e}", flush=True)

        if await _otp_form_broken(page):
            print(f"[{attempt}] OTP form shows Zod broken state — retry soft", flush=True)
        await asyncio.sleep(0.35 * round_i)

    val = await _read_xai_otp_value(page)
    broken = await _otp_form_broken(page)
    print(
        f"[{attempt}] OTP fill FAILED final={val!r} want={otp_chars!r} broken={broken}",
        flush=True,
    )
    return False



# ── GPTMail helpers (EMAIL_MODE=gptmail) ─────────────────────────────────────

def _http_json(url: str, data: dict | None = None, headers: dict | None = None, method: str | None = None) -> dict | list:
    """Minimal JSON HTTP helper (stdlib)."""
    h = {
        "Accept": "application/json",
        "User-Agent": "grok-farm/gptmail",
    }
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
        method = method or "POST"
    req = urllib.request.Request(url, data=body, headers=h, method=method or "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}


def _strip_html(html: str) -> str:
    s = html or ""
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _gptmail_headers(token: str = "") -> dict[str, str]:
    h = {
        "Accept": "application/json",
        "User-Agent": "grok-farm/gptmail",
        "Origin": GPTMAIL_API,
        "Referer": f"{GPTMAIL_API}/",
    }
    if token:
        h["x-inbox-token"] = token
        try:
            payload_b64 = token.split(".")[0]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            sid = payload.get("sid") or ""
            if sid:
                h["Cookie"] = f"gm_sid={sid}"
        except Exception:
            pass
    return h


def _gptmail_load_domains(force: bool = False) -> list[str]:
    """GET /api/domains/public — cache ~10 min."""
    global _gptmail_domains_cache, _gptmail_domains_ts
    now = time.time()
    if not force and _gptmail_domains_cache and (now - _gptmail_domains_ts) < 600:
        return list(_gptmail_domains_cache)
    data = _http_json(f"{GPTMAIL_API}/api/domains/public", headers=_gptmail_headers())
    if not isinstance(data, dict) or not data.get("success", True):
        raise RuntimeError(f"gptmail domains failed: {str(data)[:200]}")
    raw = (data.get("data") or {}).get("domains") or []
    out: list[str] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        name = (d.get("domain_name") or d.get("domain") or "").strip().lower()
        if not name or "@" in name:
            continue
        if d.get("is_active") not in (None, 1, True, "1"):
            continue
        vis = (d.get("visibility") or "public").lower()
        if vis and vis != "public":
            continue
        out.append(name)
    if not out:
        raise RuntimeError(f"gptmail: empty domain list {str(data)[:200]}")
    _gptmail_domains_cache = out
    _gptmail_domains_ts = now
    print(f"[GPTMAIL] domains loaded: {len(out)}", flush=True)
    return list(out)


def _load_blocked_domains() -> None:
    """Load permanent xAI-rejected domains from disk into memory."""
    global _gptmail_blocked_domains
    if not BLOCKED_DOMAINS_FILE.is_file():
        return
    try:
        loaded: set[str] = set()
        for line in BLOCKED_DOMAINS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # allow "domain # reason" comments
            dom = line.split("#", 1)[0].strip().lower().lstrip("@")
            if dom and "." in dom and "@" not in dom:
                loaded.add(dom)
        if loaded:
            with _gptmail_lock:
                _gptmail_blocked_domains |= loaded
            print(f"[GPTMAIL] loaded {len(loaded)} blocked domain(s) from {BLOCKED_DOMAINS_FILE.name}", flush=True)
    except Exception as e:
        print(f"[GPTMAIL] could not read blocked list: {e}", flush=True)


def _persist_blocked_domain(domain: str, reason: str = "") -> None:
    """Append domain to permanent blocklist (never re-enter pool)."""
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom:
        return
    try:
        BLOCKED_DOMAINS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # skip if already on disk
        if BLOCKED_DOMAINS_FILE.is_file():
            existing = BLOCKED_DOMAINS_FILE.read_text(encoding="utf-8", errors="replace").lower()
            if re.search(rf"(?m)^\s*{re.escape(dom)}(\s|#|$)", existing):
                return
        note = (reason or "").replace("\n", " ").strip()[:120]
        with open(BLOCKED_DOMAINS_FILE, "a", encoding="utf-8") as f:
            if note:
                f.write(f"{dom}  # {note}\n")
            else:
                f.write(f"{dom}\n")
    except Exception as e:
        print(f"[GPTMAIL] persist block fail {dom}: {e}", flush=True)


def _gptmail_pool() -> list[str]:
    """Usable public domains (excludes permanently blocked). Never recycle blocked."""
    domains = _gptmail_load_domains()
    pool = [d for d in domains if 4 <= len(d) <= 40 and "." in d]
    if not pool:
        pool = list(domains)
    with _gptmail_lock:
        blocked = set(_gptmail_blocked_domains)
    free = [d for d in pool if d not in blocked]
    if free:
        return free
    # do NOT recycle blocked — xAI already rejected them
    raise RuntimeError(
        f"gptmail: no usable domains left "
        f"(pool={len(pool)} blocked={len(blocked)} file={BLOCKED_DOMAINS_FILE.name})"
    )


def _gptmail_pick_domain(worker_slot: int | None = None) -> str:
    """Sticky domain per worker slot; only changes when domain is blocked."""
    if GPTMAIL_DOMAIN:
        # pinned domain still checked against blocklist
        if GPTMAIL_DOMAIN in _gptmail_blocked_domains:
            raise RuntimeError(
                f"gptmail: pinned domain {GPTMAIL_DOMAIN} is blocked by xAI — "
                f"clear GROK_GPTMAIL_DOMAIN or remove from {BLOCKED_DOMAINS_FILE.name}"
            )
        return GPTMAIL_DOMAIN
    pool = _gptmail_pool()
    if worker_slot is None:
        return secrets.choice(pool)
    with _gptmail_lock:
        global _gptmail_domain_rr
        cur = _gptmail_slot_domain.get(worker_slot)
        if cur and cur not in _gptmail_blocked_domains and cur in pool:
            return cur
        used = {d for s, d in _gptmail_slot_domain.items() if s != worker_slot}
        candidates = [d for d in pool if d not in used] or list(pool)
        idx = _gptmail_domain_rr % len(candidates)
        _gptmail_domain_rr += 1
        chosen = candidates[idx]
        _gptmail_slot_domain[worker_slot] = chosen
        print(
            f"[GPTMAIL] slot={worker_slot} sticky domain={chosen} "
            f"(blocked={len(_gptmail_blocked_domains)})",
            flush=True,
        )
        return chosen


def gptmail_block_domain(domain: str, reason: str = "", worker_slot: int | None = None) -> None:
    """Mark domain blocked permanently (memory + disk); next claim rotates sticky slot."""
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom:
        return
    with _gptmail_lock:
        already = dom in _gptmail_blocked_domains
        _gptmail_blocked_domains.add(dom)
        if worker_slot is not None and _gptmail_slot_domain.get(worker_slot) == dom:
            _gptmail_slot_domain.pop(worker_slot, None)
        else:
            for s, d in list(_gptmail_slot_domain.items()):
                if d == dom:
                    _gptmail_slot_domain.pop(s, None)
    if not already:
        _persist_blocked_domain(dom, reason)
    print(
        f"[GPTMAIL] BLOCK domain={dom} reason={reason[:80]!r} "
        f"total_blocked={len(_gptmail_blocked_domains)} "
        f"(persisted → {BLOCKED_DOMAINS_FILE.name})",
        flush=True,
    )


def _gptmail_make_local() -> str:
    if GPTMAIL_PREFIX:
        suffix = _crypto_local_part(max(4, min(10, EMAIL_LOCAL_LEN // 2)))
        base = re.sub(r"[^a-z0-9._-]", "", GPTMAIL_PREFIX)[:24] or "grk"
        return f"{base}{suffix}"
    letters = "".join(secrets.choice(string.ascii_lowercase) for _ in range(secrets.randbelow(5) + 6))
    digits = "".join(secrets.choice(string.digits) for _ in range(secrets.randbelow(3) + 2))
    return f"{letters}{digits}"


def create_gptmail_inbox(worker_slot: int | None = None) -> str:
    """Claim GPTMail inbox on sticky domain for worker_slot."""
    last_err = ""
    for attempt in range(12):
        domain = _gptmail_pick_domain(worker_slot)
        local = _gptmail_make_local()
        addr = f"{local}@{domain}".lower()
        try:
            data = _http_json(
                f"{GPTMAIL_API}/api/inbox-token",
                {"email": addr},
                headers=_gptmail_headers(),
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            last_err = f"HTTP {e.code}: {body[:160]}"
            if e.code in (409, 422, 429, 400):
                time.sleep(0.3 + attempt * 0.1)
                continue
            raise RuntimeError(f"gptmail inbox-token {last_err}") from e
        except Exception as e:
            last_err = str(e)
            time.sleep(0.4)
            continue
        if not isinstance(data, dict) or not data.get("success", True):
            last_err = str(data)[:200]
            time.sleep(0.3)
            continue
        auth = data.get("auth") or {}
        token = auth.get("token") or ""
        email = (auth.get("email") or (data.get("data") or {}).get("email") or addr).lower()
        if not token:
            last_err = f"no token in {data}"
            continue
        with _gptmail_lock:
            _gptmail_accounts[email] = {
                "token": token,
                "expires_at": str(auth.get("expires_at") or ""),
                "worker_slot": str(worker_slot if worker_slot is not None else ""),
            }
        print(
            f"[GPTMAIL] claimed {email} slot={worker_slot if worker_slot is not None else '-'}",
            flush=True,
        )
        return email
    raise RuntimeError(f"gptmail: could not claim inbox after retries ({last_err})")


def read_otp_from_gptmail_sync(target_email: str, timeout: int = 180, since_ts: float | None = None) -> str | None:
    """Poll GPTMail for xAI XXX-XXX confirmation code."""
    addr = target_email.lower()
    with _gptmail_lock:
        cred = dict(_gptmail_accounts.get(addr) or {})
    token = cred.get("token") or ""
    if not token:
        print(f"[GPTMAIL] no session for {addr}", flush=True)
        return None
    print(f"[GPTMAIL] Waiting OTP -> {addr} (timeout={timeout}s)...", flush=True)
    start = time.time()
    since_ts = since_ts or (start - 30)
    seen_ids: set[str] = set()
    polls = 0
    while time.time() - start < timeout:
        polls += 1
        elapsed = int(time.time() - start)
        try:
            q = urlencode({"email": addr})
            data = _http_json(
                f"{GPTMAIL_API}/api/emails?{q}",
                headers=_gptmail_headers(token),
            )
            if isinstance(data, dict) and data.get("success") is False:
                err = data.get("error") or data
                print(f"[GPTMAIL] list error: {err}", flush=True)
                if "denied" in str(err).lower() or "token" in str(err).lower():
                    try:
                        recl = _http_json(
                            f"{GPTMAIL_API}/api/inbox-token",
                            {"email": addr},
                            headers=_gptmail_headers(),
                        )
                        auth = (recl or {}).get("auth") or {}
                        if auth.get("token"):
                            token = auth["token"]
                            with _gptmail_lock:
                                _gptmail_accounts[addr] = {
                                    "token": token,
                                    "expires_at": str(auth.get("expires_at") or ""),
                                }
                            print(f"[GPTMAIL] re-claimed token for {addr}", flush=True)
                    except Exception as e2:
                        print(f"[GPTMAIL] re-claim fail: {e2}", flush=True)
                time.sleep(2.5)
                continue
            items = []
            if isinstance(data, dict):
                items = (data.get("data") or {}).get("emails") or data.get("emails") or []
            elif isinstance(data, list):
                items = data
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                ts = item.get("timestamp") or 0
                try:
                    ts_f = float(ts)
                    if ts_f > 1e12:
                        ts_f /= 1000.0
                    if ts_f and ts_f < since_ts - 5:
                        continue
                except (TypeError, ValueError):
                    pass
                subject = str(item.get("subject") or "")
                content = str(item.get("content") or item.get("text") or "")
                html = str(item.get("html_content") or item.get("html") or "")
                body = content + "\n" + _strip_html(html)
                fr = str(item.get("from_address") or item.get("from") or "")
                code = _extract_xai_code(subject, body)
                if not code and mid:
                    try:
                        q2 = urlencode({"email": addr, "include_raw": "0"})
                        full = _http_json(
                            f"{GPTMAIL_API}/api/email/{mid}?{q2}",
                            headers=_gptmail_headers(token),
                        )
                        d = (full or {}).get("data") if isinstance(full, dict) else None
                        if not isinstance(d, dict) and isinstance(full, dict):
                            d = full
                        if isinstance(d, dict):
                            subject = str(d.get("subject") or subject)
                            content = str(d.get("content") or d.get("text") or content)
                            html = str(d.get("html_content") or d.get("html") or html)
                            body = content + "\n" + _strip_html(html)
                            fr = str(d.get("from_address") or d.get("from") or fr)
                            code = _extract_xai_code(subject, body)
                    except Exception as e_det:
                        if polls % 4 == 0:
                            print(f"[GPTMAIL] detail fail: {e_det}", flush=True)
                if not code:
                    continue
                blob = f"{subject} {fr} {body}".lower()
                if "xai" not in blob and "grok" not in blob and "confirmation" not in blob:
                    if "code" not in blob and "verify" not in blob:
                        continue
                with _claimed_otps_lock:
                    if code in _claimed_otps_sync:
                        continue
                    _claimed_otps_sync.add(code)
                print(
                    f"[GPTMAIL] OTP found: {code} for {addr} "
                    f"(subj={subject[:60]!r} from={fr[:40]!r} t+{elapsed}s)",
                    flush=True,
                )
                return code
            if polls == 1 or polls % 5 == 0:
                print(
                    f"[GPTMAIL] still waiting… {elapsed}s/{timeout}s msgs={len(items)}",
                    flush=True,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[GPTMAIL] HTTP {e.code}: {body[:120]}", flush=True)
        except Exception as e:
            print(f"[GPTMAIL] poll error: {e}", flush=True)
        time.sleep(3)
    print(f"[GPTMAIL] Timeout after {timeout}s for {addr}", flush=True)
    return None


# ── Exzork helpers (EMAIL_MODE=exzork — mailer.exzork.me) ────────────────────

_exzork_lock = threading.Lock()
_exzork_known: set[str] = set()


def _exzork_headers() -> dict[str, str]:
    if not EXZORK_API_KEY:
        raise RuntimeError("GROK_EXZORK_API_KEY required for exzork mode")
    return {
        "Accept": "application/json",
        "User-Agent": "grok-farm/exzork",
        "X-API-Key": EXZORK_API_KEY,
    }


def _exzork_host() -> str:
    """Apex or random subdomain host for mailbox create."""
    base = (EXZORK_DOMAIN or "").strip().lower().lstrip("@").lstrip("*.")
    if not base:
        raise RuntimeError("GROK_EXZORK_DOMAIN or GROK_EMAIL_DOMAIN required for exzork mode")
    if not EXZORK_WILDCARD:
        return base
    # random subdomain under claimed *.base (anti-block rotate)
    sub = _crypto_local_part(max(6, min(10, EMAIL_LOCAL_LEN // 2)))
    return f"{sub}.{base}"


def create_exzork_inbox() -> str:
    """POST /api/v1/mailboxes — random local on apex or rotating subdomain."""
    last_err = ""
    for attempt in range(8):
        host = _exzork_host()
        try:
            data = _http_json(
                f"{EXZORK_API}/api/v1/mailboxes",
                {"random": True, "domain": host},
                headers=_exzork_headers(),
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            last_err = f"HTTP {e.code}: {body[:160]}"
            if e.code in (400, 409, 422, 429):
                time.sleep(0.3 + attempt * 0.15)
                continue
            raise RuntimeError(f"exzork mailbox {last_err}") from e
        except Exception as e:
            last_err = str(e)
            time.sleep(0.4)
            continue
        mb = {}
        if isinstance(data, dict):
            mb = data.get("mailbox") or {}
            if not mb and data.get("mailboxes"):
                mbs = data["mailboxes"]
                if isinstance(mbs, list) and mbs:
                    mb = mbs[0] if isinstance(mbs[0], dict) else {}
        addr = (mb.get("address") if isinstance(mb, dict) else "") or ""
        addr = str(addr).strip().lower()
        if not addr or "@" not in addr:
            last_err = f"no address in {str(data)[:200]}"
            time.sleep(0.3)
            continue
        with _exzork_lock:
            _exzork_known.add(addr)
        print(f"[EXZORK] claimed {addr}", flush=True)
        return addr
    raise RuntimeError(f"exzork: could not create mailbox after retries ({last_err})")


def _exzork_msg_blob(item: dict) -> tuple[str, str, str]:
    """subject, body_text, from for one Exzork message dict."""
    subject = str(item.get("subject") or "")
    fr = str(
        item.get("from")
        or item.get("from_address")
        or item.get("sender")
        or ""
    )
    text = str(item.get("text") or item.get("body") or item.get("content") or "")
    html = str(item.get("html") or item.get("html_content") or item.get("body_html") or "")
    # nested body object
    body_obj = item.get("body")
    if isinstance(body_obj, dict):
        text = text or str(body_obj.get("text") or "")
        html = html or str(body_obj.get("html") or "")
    body = text + "\n" + _strip_html(html)
    return subject, body, fr


def read_otp_from_exzork_sync(
    target_email: str, timeout: int = 180, since_ts: float | None = None
) -> str | None:
    """Poll Exzork GET /mailboxes/{address}/messages for xAI XXX-XXX code."""
    addr = target_email.lower().strip()
    print(f"[EXZORK] Waiting OTP -> {addr} (timeout={timeout}s)...", flush=True)
    start = time.time()
    since_ts = since_ts or (start - 30)
    seen_ids: set[str] = set()
    polls = 0
    enc = quote(addr, safe="")
    while time.time() - start < timeout:
        polls += 1
        elapsed = int(time.time() - start)
        try:
            data = _http_json(
                f"{EXZORK_API}/api/v1/mailboxes/{enc}/messages",
                headers=_exzork_headers(),
            )
            items: list = []
            if isinstance(data, dict):
                items = data.get("messages") or data.get("items") or []
            elif isinstance(data, list):
                items = data
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                # optional created_at filter
                created = item.get("created_at") or item.get("received_at") or item.get("date")
                if created and since_ts:
                    try:
                        if isinstance(created, (int, float)):
                            ts_f = float(created)
                            if ts_f > 1e12:
                                ts_f /= 1000.0
                        else:
                            # ISO-ish
                            s = str(created).replace("Z", "+00:00")
                            ts_f = datetime.fromisoformat(s).timestamp()
                        if ts_f and ts_f < since_ts - 5:
                            continue
                    except Exception:
                        pass
                subject, body, fr = _exzork_msg_blob(item)
                code = _extract_xai_code(subject, body)
                if not code and mid:
                    try:
                        full = _http_json(
                            f"{EXZORK_API}/api/v1/messages/{mid}",
                            headers=_exzork_headers(),
                        )
                        d = full
                        if isinstance(full, dict):
                            d = full.get("message") or full.get("data") or full
                        if isinstance(d, dict):
                            subject, body, fr = _exzork_msg_blob(d)
                            code = _extract_xai_code(subject, body)
                    except Exception as e_det:
                        if polls % 4 == 0:
                            print(f"[EXZORK] detail fail: {e_det}", flush=True)
                if not code:
                    continue
                blob = f"{subject} {fr} {body}".lower()
                if "xai" not in blob and "grok" not in blob and "confirmation" not in blob:
                    if "code" not in blob and "verify" not in blob:
                        continue
                with _claimed_otps_lock:
                    if code in _claimed_otps_sync:
                        continue
                    _claimed_otps_sync.add(code)
                print(
                    f"[EXZORK] OTP found: {code} for {addr} "
                    f"(subj={subject[:60]!r} from={fr[:40]!r} t+{elapsed}s)",
                    flush=True,
                )
                return code
            if polls == 1 or polls % 5 == 0:
                print(
                    f"[EXZORK] still waiting… {elapsed}s/{timeout}s msgs={len(items)}",
                    flush=True,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[EXZORK] HTTP {e.code}: {body[:120]}", flush=True)
        except Exception as e:
            print(f"[EXZORK] poll error: {e}", flush=True)
        time.sleep(3)
    print(f"[EXZORK] Timeout after {timeout}s for {addr}", flush=True)
    return None


async def _page_error_text(page) -> str:
    """Best-effort page text including red/error nodes (xAI React banners)."""
    try:
        return (
            await page.evaluate(
                """() => {
                  const parts = [];
                  if (document.body) parts.push(document.body.innerText || '');
                  // role=alert / aria-live often hold validation errors
                  for (const sel of [
                    '[role="alert"]',
                    '[aria-live]',
                    '[class*="error" i]',
                    '[class*="Error"]',
                    '[data-testid*="error" i]',
                    'p',
                    'span',
                  ]) {
                    try {
                      for (const el of document.querySelectorAll(sel)) {
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t && t.length < 500) parts.push(t);
                      }
                    } catch (e) {}
                  }
                  return parts.join('\\n');
                }"""
            )
        ) or ""
    except Exception:
        try:
            return (await page.evaluate("() => (document.body && document.body.innerText) || ''")) or ""
        except Exception:
            return ""


def _domain_block_reason_from_text(t: str) -> str | None:
    """Classify page/error text as domain rejection (xAI copy included)."""
    low = (t or "").lower()
    if not low:
        return None
    # xAI exact: "Your email domain X has been rejected. Please use a different email address..."
    if "domain" in low and "rejected" in low:
        return "email domain has been rejected"
    if "has been rejected" in low and ("email" in low or "domain" in low):
        return "has been rejected"
    needles = (
        "use a different email address",
        "use a different email domain",
        "email domain is not allowed",
        "domain is not allowed to sign up",
        "domain is not allowed",
        "email domain not allowed",
        "not allowed to sign up",
        "disposable email",
        "email provider is not allowed",
        "invalid email domain",
        "email address is not allowed",
        "domain has been rejected",
        "domain was rejected",
        "domain is rejected",
        "contact support@x.ai",
    )
    for n in needles:
        if n not in low:
            continue
        if n in ("use a different email address", "contact support@x.ai"):
            if "domain" in low or "rejected" in low:
                return n
            continue
        return n
    if "domain" in low and ("not allowed" in low or "not permitted" in low or "blocked" in low):
        if "email" in low or "sign up" in low or "signup" in low:
            return "domain + not allowed"
    return None


async def page_has_domain_block(page) -> str | None:
    """Detect xAI banner when email domain is rejected."""
    return _domain_block_reason_from_text(await _page_error_text(page))


async def raise_if_domain_blocked(page, attempt: int = 0) -> None:
    """If domain-reject banner present → screenshot + raise domain_not_allowed."""
    dom_block = await page_has_domain_block(page)
    if not dom_block:
        return
    try:
        await screenshot(page, attempt, "domain_blocked")
    except Exception:
        pass
    raise RuntimeError(f"domain_not_allowed: {dom_block}")


async def wait_otp_imap_keepalive(
    page, email_addr: str, timeout_s: int, since_ts: float, attempt: int
) -> str | None:
    """Poll OTP (IMAP or GPTMail) in a thread while keeping the browser page awake.

    Long idle during OTP wait can leave Camoufox/React inputs sticky (~flaky fill).
    """
    loop = asyncio.get_event_loop()
    if EMAIL_MODE == "gptmail":
        poll_fn = lambda: read_otp_from_gptmail_sync(email_addr, timeout_s, since_ts)
    elif EMAIL_MODE == "exzork":
        poll_fn = lambda: read_otp_from_exzork_sync(email_addr, timeout_s, since_ts)
    else:
        poll_fn = lambda: read_otp_from_imap_sync(email_addr, timeout_s, since_ts)
    fut = loop.run_in_executor(None, poll_fn)
    tick = 0
    while not fut.done():
        tick += 1
        try:
            # Light keep-alive: title read + cookie dismiss; no navigation
            await page.evaluate("() => document.title")
            if tick % 3 == 0:
                try:
                    await dismiss_cookie_banner(page)
                except Exception:
                    pass
            # Confirm OTP field still present
            if tick % 4 == 0 and not await _otp_inputs_ready(page):
                print(f"[{attempt}] WARN: OTP inputs missing during OTP wait", flush=True)
        except Exception as e:
            print(f"[{attempt}] page keep-alive warn: {e}", flush=True)
        try:
            await asyncio.wait({fut}, timeout=3.5)
        except Exception:
            await asyncio.sleep(3.5)
    return fut.result()


async def wait_for_selector_any(page, selectors: list[str], timeout_ms: int = 15000) -> str | None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return sel
            except Exception:
                continue
        await asyncio.sleep(0.35)
    return None


async def do_signup(page, email_addr: str, password: str, attempt: int) -> bool:
    emit_progress(attempt, "signup_open", "Opening sign-up page", email_addr)
    try:
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[{attempt}] goto signup failed: {e}", flush=True)
        await page.goto(SIGNUP_URL, wait_until="commit", timeout=45000)
    await asyncio.sleep(1.5)
    await dismiss_cookie_banner(page)
    await handle_turnstile(page, attempt, max_wait=8)

    # Prefer email signup over Google/X/Apple
    emit_progress(attempt, "signup_email_btn", "Selecting Sign up with email", email_addr)
    clicked = await click_text_button(
        page,
        ["Sign up with email", "Sign up with Email"],
        exclude=["Google", "Apple", "Microsoft", " with X"],
    )
    if not clicked:
        # Fallback: exact text via locator
        try:
            await page.get_by_role("button", name=re.compile(r"sign up with email", re.I)).click(timeout=5000)
            clicked = "Sign up with email"
        except Exception:
            print(f"[{attempt}] No email signup button — assuming email form present", flush=True)
    if clicked:
        print(f"[{attempt}] Clicked: {clicked}", flush=True)
    await asyncio.sleep(1.0)
    await dismiss_cookie_banner(page)

    emit_progress(attempt, "fill_email", "Filling registration email", email_addr)
    email_sel = await wait_for_selector_any(
        page,
        ['input[name="email"]', 'input[type="email"]', 'input[autocomplete="email"]'],
        10000,
    )
    if not email_sel:
        await screenshot(page, attempt, "no_email_input")
        raise RuntimeError("Could not find email input on sign-up")
    filled = await fill_input(page, [email_sel], email_addr)
    if not filled:
        await screenshot(page, attempt, "email_fill_fail")
        raise RuntimeError("Failed to fill email")

    await asyncio.sleep(0.4)
    await handle_turnstile(page, attempt, max_wait=8)

    emit_progress(attempt, "submit_email", "Clicking Sign up", email_addr)
    otp_wait_started = time.time()
    # Prefer submit button (exact "Sign up" on this form — not "Sign up with Google")
    try:
        await page.locator('button[type="submit"]').filter(has_text=re.compile(r"^sign up$", re.I)).click(timeout=5000)
    except Exception:
        await click_text_button(page, ["Sign up"], exclude=["Google", "Apple", "email", "X"])
    await asyncio.sleep(1.2)
    # Fail-fast on domain reject (don't burn 20s waiting for OTP)
    await raise_if_domain_blocked(page, attempt)

    # Wait for OTP — poll domain-reject banner every tick (xAI often shows red text, no OTP)
    otp_sels = ['input[name="code"]', 'input[autocomplete="one-time-code"]']
    code_sel = None
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        await raise_if_domain_blocked(page, attempt)
        code_sel = await wait_for_selector_any(page, otp_sels, 1500)
        if code_sel:
            break
    if not code_sel:
        await raise_if_domain_blocked(page, attempt)
        await screenshot(page, attempt, "no_otp_input")
        # maybe turnstile blocked submit
        await handle_turnstile(page, attempt, max_wait=15)
        try:
            await page.locator('button[type="submit"]').filter(has_text=re.compile(r"^sign up$", re.I)).click(timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        await raise_if_domain_blocked(page, attempt)
        code_sel = await wait_for_selector_any(page, otp_sels, 10000)
    if not code_sel:
        await raise_if_domain_blocked(page, attempt)
        await screenshot(page, attempt, "otp_input_missing")
        # last chance: classify body text so log is domain_not_allowed not OTP missing
        body = await _page_error_text(page)
        reason = _domain_block_reason_from_text(body)
        if reason:
            raise RuntimeError(f"domain_not_allowed: {reason}")
        raise RuntimeError("OTP input never appeared after Sign up")

        _otp_via = (
            "GPTMAIL"
            if EMAIL_MODE == "gptmail"
            else "EXZORK"
            if EMAIL_MODE == "exzork"
            else "IMAP"
        )
        emit_progress(
            attempt,
            "wait_otp",
            f"Waiting for xAI confirmation code via {_otp_via}",
            email_addr,
        )
    otp = await wait_otp_imap_keepalive(
        page, email_addr, OTP_TIMEOUT_S, otp_wait_started - 15, attempt
    )
    if not otp:
        await screenshot(page, attempt, "otp_timeout")
        raise RuntimeError(f"OTP timeout after {OTP_TIMEOUT_S}s (xAI confirmation code)")

    emit_progress(attempt, "fill_otp", f"Entering code {otp}", email_addr)
    try:
        await dismiss_cookie_banner(page)
    except Exception:
        pass
    # Brief settle after IMAP — UI can lag behind mail arrival
    await asyncio.sleep(0.4)
    if not await _otp_inputs_ready(page):
        # One short re-wait for React re-render
        await wait_for_selector_any(
            page,
            ['input[name="code"]', 'input[autocomplete="one-time-code"]', 'input[maxlength="1"]'],
            8000,
        )

    # UI is multi-box "XXX-XXX". Never value='' clear / never type hyphen.
    otp_clean = otp.strip().upper()
    otp_chars = re.sub(r"[^A-Z0-9]", "", otp_clean)
    if len(otp_chars) != 6:
        print(f"[{attempt}] WARN: unexpected OTP length {len(otp_chars)}: {otp_clean}", flush=True)

    try:
        otp_filled = await asyncio.wait_for(
            fill_xai_otp_boxes(page, otp_chars, attempt),
            timeout=35.0,
        )
    except asyncio.TimeoutError:
        await screenshot(page, attempt, "otp_fill_timeout")
        raise RuntimeError("fill_otp hung >35s (Playwright stuck on OTP inputs)")
    if not otp_filled:
        await screenshot(page, attempt, "otp_fill_fail")
        if await _otp_form_broken(page):
            raise RuntimeError(
                "OTP form broken (expected string, received undefined) — bad clear/type"
            )
        raise RuntimeError("Failed to enter OTP into page (value not verified)")
    # NOTE: do NOT re-read DOM and hard-fail here. xAI React OTP often drops
    # visible input.value after accept while internal form state keeps the code.
    # That caused mass "otp_lost_before_confirm" after a successful verify.
    await asyncio.sleep(0.3)

    emit_progress(attempt, "confirm_email", "Confirming email", email_addr)
    try:
        await dismiss_cookie_banner(page)
    except Exception:
        pass
    confirm_deadline = time.monotonic() + CONFIRM_EMAIL_TIMEOUT_S
    try:
        await page.get_by_role("button", name=re.compile(r"confirm email", re.I)).click(timeout=5000)
    except Exception:
        await click_text_button(page, ["Confirm email", "Confirm Email", "Confirm", "Verify"])
    await asyncio.sleep(1.5)

    # Fail-fast: stuck / invalid / unclear verify page must not hold the worker
    try:
        while time.monotonic() < confirm_deadline:
            still_verify = await page.locator("text=Verify your email").count()
            if still_verify == 0:
                break
            # Real rejections only (not flaky DOM re-reads)
            err = await page.locator(
                "text=/expected string, received undefined|Invalid input|incorrect|expired|try again/i"
            ).count()
            if err > 0:
                await screenshot(page, attempt, "otp_invalid")
                raise RuntimeError("OTP rejected by form (Invalid input / broken OTP state)")
            # one re-click then short wait
            try:
                await page.get_by_role("button", name=re.compile(r"confirm email", re.I)).click(timeout=2500)
            except Exception:
                pass
            await asyncio.sleep(1.5)
        else:
            # still on verify after timeout — unclear state, abort
            await screenshot(page, attempt, "confirm_stuck")
            raise RuntimeError(
                f"confirm_email stuck >{CONFIRM_EMAIL_TIMEOUT_S}s (still on Verify page)"
            )
    except RuntimeError:
        raise
    except Exception as e:
        # locator flake — if wall time exceeded, still fail
        if time.monotonic() >= confirm_deadline:
            await screenshot(page, attempt, "confirm_error")
            raise RuntimeError(f"confirm_email error after timeout: {e}") from e

    # Profile: first / last / password (probed live)
    first, last = random_name()
    emit_progress(attempt, "profile", f"Filling profile {first} {last}", email_addr)
    await screenshot(page, attempt, "profile_step")

    # Dump inputs for debug if fill fails
    profile_ready = await wait_for_selector_any(
        page,
        [
            'input[name*="first" i]',
            'input[autocomplete="given-name"]',
            'input[name="given_name"]',
            'input[type="password"]',
            'input[name*="name" i]',
        ],
        15000,
    )
    if not profile_ready:
        await screenshot(page, attempt, "no_profile")
        # might already be past profile / logged in — continue to OAuth path
        print(f"[{attempt}] Profile form not found — checking page state", flush=True)
    else:
        # Try common name field patterns (xAI may use firstName/lastName)
        await fill_input(
            page,
            [
                'input[name="firstName"]',
                'input[name="first_name"]',
                'input[name="given_name"]',
                'input[name*="first" i]',
                'input[autocomplete="given-name"]',
                'input[placeholder*="First" i]',
            ],
            first,
        )
        await asyncio.sleep(0.25)
        await fill_input(
            page,
            [
                'input[name="lastName"]',
                'input[name="last_name"]',
                'input[name="family_name"]',
                'input[name*="last" i]',
                'input[autocomplete="family-name"]',
                'input[placeholder*="Last" i]',
            ],
            last,
        )
        await asyncio.sleep(0.25)
        # If only a single "Name" field
        try:
            name_inputs = await page.locator(
                'input[name="name"], input[autocomplete="name"], input[placeholder*="Name" i]'
            ).all()
            if name_inputs and not await page.locator('input[name*="first" i]').count():
                await name_inputs[0].fill(f"{first} {last}")
        except Exception:
            pass

        # Prefer React-safe fill (triggers turnstile mount after password input)
        await _ensure_password_filled(page, password, attempt)
        await asyncio.sleep(1.2)  # allow CF widget to mount after password

        # Complete sign-up — OLD concurrent-friendly flow (restored):
        # wall-clock budget, 25s Turnstile slices, click-only (no serial lock /
        # remount/reload). Matches morning batch that got multi-worker OK.
        emit_progress(attempt, "complete_signup", "Turnstile + Complete sign up", email_addr)
        completed = False
        complete_deadline = time.monotonic() + COMPLETE_SIGNUP_TIMEOUT_S
        round_i = 0
        while time.monotonic() < complete_deadline and not completed:
            round_i += 1
            remaining = max(3.0, complete_deadline - time.monotonic())
            # Re-fill if React re-render wiped fields
            try:
                await fill_input(
                    page,
                    [
                        'input[name="firstName"]',
                        'input[name="first_name"]',
                        'input[name*="first" i]',
                        'input[autocomplete="given-name"]',
                    ],
                    first,
                )
                await fill_input(
                    page,
                    [
                        'input[name="lastName"]',
                        'input[name="last_name"]',
                        'input[name*="last" i]',
                        'input[autocomplete="family-name"]',
                    ],
                    last,
                )
                await _ensure_password_filled(page, password, attempt)
            except Exception:
                pass

            # Complete form ALWAYS needs Turnstile — blank ≠ absent
            # No global serial lock / no remount (old style — parallel OK)
            slice_wait = min(25.0, remaining)
            ok_ts = await handle_turnstile(
                page,
                attempt,
                max_wait=slice_wait,
                require_token=True,
                allow_remount=False,
            )
            tok = await turnstile_token_len(page)
            mounted = await _turnstile_mount_present(page)
            print(
                f"[{attempt}] complete round {round_i}: turnstile_ok={ok_ts} "
                f"token_len={tok} mounted={mounted}",
                flush=True,
            )

            # Do not spam Complete without token (except last ~8s of budget)
            time_left = complete_deadline - time.monotonic()
            if tok <= 20 and time_left > 8:
                print(
                    f"[{attempt}] no Turnstile token yet — wait (left {time_left:.0f}s)",
                    flush=True,
                )
                await asyncio.sleep(2.0)
                continue

            try:
                await page.get_by_role(
                    "button", name=re.compile(r"complete\s+sign\s*up", re.I)
                ).click(timeout=5000)
            except Exception:
                await click_text_button(
                    page,
                    ["Complete sign up", "Complete Sign Up", "Create account", "Continue"],
                )
            await asyncio.sleep(2.5)
            # Success: left the complete form
            try:
                if await page.locator("text=Complete your sign up").count() == 0:
                    completed = True
                    break
                print(f"[{attempt}] still on complete form after click", flush=True)
            except Exception:
                completed = True
                break

        if not completed:
            await screenshot(page, attempt, "complete_stuck")
            raise RuntimeError(
                f"complete_signup stuck >{COMPLETE_SIGNUP_TIMEOUT_S}s "
                "(Turnstile unclear / form not advancing) — abort worker"
            )

    await screenshot(page, attempt, "after_signup")
    return True


async def _password_field_value(page) -> str:
    try:
        return await page.evaluate(
            """() => {
                const el = document.querySelector('input[type="password"], input[name="password"], input[autocomplete="current-password"]');
                return el && typeof el.value === 'string' ? el.value : '';
            }"""
        ) or ""
    except Exception:
        return ""


async def _wait_password_ready(page, attempt: int, max_wait: float = 8.0) -> None:
    """After password fill, wait briefly for Turnstile mount (do not block on eye-icon SVG)."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if await turnstile_token_len(page) > 20:
            return
        if await turnstile_visible(page):
            await asyncio.sleep(0.4)
            return
        if await _turnstile_mount_present(page):
            await asyncio.sleep(0.8)  # give iframe a moment to init
            return
        await asyncio.sleep(0.35)
    # not fatal — handle_turnstile will keep trying


async def _ensure_password_filled(page, password: str, attempt: int) -> bool:
    """Fill password and verify React state kept it (Turnstile re-render can wipe it)."""
    for try_i in range(3):
        if await page.locator('input[type="password"]').count() == 0:
            return True  # password step not shown
        await fill_input(
            page,
            [
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
                'input[autocomplete="new-password"]',
            ],
            password,
        )
        await asyncio.sleep(0.25)
        # Prefer Playwright fill again if value empty
        try:
            loc = page.locator('input[type="password"]').first
            if await loc.count() > 0:
                val = await loc.input_value()
                if not val:
                    await loc.click()
                    await loc.fill(password)
                    await asyncio.sleep(0.2)
                    val = await loc.input_value()
                if val:
                    # blur to commit React state + trigger CF mount
                    try:
                        await loc.evaluate("el => el.blur()")
                    except Exception:
                        pass
                    await _wait_password_ready(page, attempt, max_wait=10.0)
                    return True
        except Exception:
            pass
        val = await _password_field_value(page)
        if val:
            await _wait_password_ready(page, attempt, max_wait=8.0)
            return True
        print(f"[{attempt}] password empty after fill (try {try_i+1})", flush=True)
        await asyncio.sleep(0.4)
    return bool(await _password_field_value(page))


async def recover_page_load_error(page, attempt: int) -> bool:
    """Handle Firefox 'This page couldn't load' (network blip / concurrent stress)."""
    try:
        body = (await page.inner_text("body"))[:500].lower()
    except Exception:
        body = ""
    if "couldn't load" not in body and "could not load" not in body and "page isn’t available" not in body:
        # also check title-ish
        if "reload" not in body or "try again" not in body:
            return False
        if "couldn't" not in body and "could not" not in body and "can't be reached" not in body:
            return False
    print(f"[{attempt}] page load error detected — reloading", flush=True)
    try:
        btn = page.get_by_role("button", name=re.compile(r"reload", re.I))
        if await btn.count() > 0:
            await btn.first.click(timeout=3000)
        else:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2.0)
        return True
    except Exception as e:
        print(f"[{attempt}] reload failed: {e}", flush=True)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)
            return True
        except Exception:
            return False


async def click_login_with_email(page) -> bool:
    """xAI UI uses both 'Sign in with email' and 'Login with email'."""
    clicked = await click_text_button(
        page,
        [
            "Login with email",
            "Log in with email",
            "Sign in with email",
            "Sign in with Email",
            "Continue with email",
        ],
        exclude=["Google", "Apple", "Microsoft", " with X", " with x"],
    )
    if clicked:
        return True
    try:
        await page.get_by_role(
            "button", name=re.compile(r"(log\s*in|sign\s*in)\s+with\s+email", re.I)
        ).click(timeout=4000)
        return True
    except Exception:
        return False


async def drive_email_password_login(page, email_addr: str, password: str, attempt: int) -> bool:
    """Drive accounts.x.ai email login form (Next → password → Turnstile → Login).

    Same email as registration is re-entered (NOT a change-email step). xAI OAuth
    often starts a fresh sign-in even right after signup.

    Order matters: always re-fill password AFTER turnstile, because solving CF
    can remount the form and wipe the password field (turnstile checked + empty pw).
    """
    await dismiss_cookie_banner(page)
    await recover_page_load_error(page, attempt)

    # Provider chooser may still be showing
    if await page.locator("text=/Log( ?in|in) with email|Sign in with email/i").count() > 0:
        if await page.locator('input[type="email"], input[type="password"]').count() == 0:
            await click_login_with_email(page)
            await asyncio.sleep(1.0)

    # Step: email (same farmed address — re-auth, not "ganti email")
    if await page.locator('input[type="email"], input[name="email"]').count() > 0:
        await fill_input(
            page,
            ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]'],
            email_addr,
        )
        await asyncio.sleep(0.2)
        # Next only when password not yet visible — wait out loading spinner
        if await page.locator('input[type="password"]').count() == 0:
            try:
                await page.get_by_role("button", name=re.compile(r"^next$", re.I)).click(timeout=4000)
            except Exception:
                await click_text_button(page, ["Next", "Continue"], exclude=["Google", "Apple"])
            # Wait for password field (not just fixed sleep — Next can hang on network)
            for _ in range(20):
                await recover_page_load_error(page, attempt)
                if await page.locator('input[type="password"]').count() > 0:
                    break
                await asyncio.sleep(0.3)
            await asyncio.sleep(0.2)

    # Step: password first (before turnstile)
    if not await _ensure_password_filled(page, password, attempt):
        print(f"[{attempt}] WARN: could not fill password before turnstile", flush=True)

    for round_i in range(5):
        await recover_page_load_error(page, attempt)

        # Still on provider chooser?
        if await page.locator('input[type="password"]').count() == 0 and await page.locator(
            "text=/Login with email|Log in with email|Sign in with email/i"
        ).count() > 0:
            await click_login_with_email(page)
            await asyncio.sleep(0.5)
            continue

        # 1) Solve / confirm turnstile if present (throttle + remount on fail)
        needs_ts = (
            await turnstile_visible(page)
            or await page.locator("text=Verify you are human").count() > 0
            or await _turnstile_mount_present(page)
            or await _turnstile_verification_failed(page)
        )
        if needs_ts:
            await handle_turnstile(
                page,
                attempt,
                max_wait=22,
                require_token=True,
                password=password,
                use_global_limit=True,
            )

        # 2) ALWAYS re-fill password after turnstile — CF widget often remounts the form
        if not await _ensure_password_filled(page, password, attempt):
            print(f"[{attempt}] password still empty after turnstile (round {round_i+1})", flush=True)
            await asyncio.sleep(0.5)
            continue

        # 3) Click Login only when password non-empty + token ok (or no mount)
        pw_now = await _password_field_value(page)
        tok_now = await turnstile_token_len(page)
        if not pw_now:
            continue
        if tok_now <= 20 and await _turnstile_mount_present(page):
            print(f"[{attempt}] login: waiting for turnstile token (round {round_i+1})", flush=True)
            await asyncio.sleep(1.0)
            continue
        print(
            f"[{attempt}] login submit round {round_i+1} (pw_len={len(pw_now)}, ts={tok_now})",
            flush=True,
        )
        try:
            await page.get_by_role("button", name=re.compile(r"^(login|log in|sign in)$", re.I)).click(timeout=4000)
        except Exception:
            await click_text_button(page, ["Login", "Log in", "Sign in", "Continue"])
        await asyncio.sleep(1.5)

        # left login form?
        try:
            if await page.locator("text=Log in with your email").count() == 0:
                # still might be on consent / redirect
                if await page.locator("text=Verify you are human").count() == 0 or await turnstile_token_len(page) > 20:
                    if await page.locator('input[type="password"]').count() == 0:
                        return True
            if "sign-in" not in (page.url or "") and "login" not in (page.url or "").lower():
                if "accounts.x.ai/sign" not in (page.url or ""):
                    return True
            # Auth error?
            if await page.locator("text=/incorrect|invalid password|wrong password/i").count() > 0:
                print(f"[{attempt}] login rejected (wrong password?)", flush=True)
                await _ensure_password_filled(page, password, attempt)
        except Exception:
            return True
    return False


async def do_email_login(page, email_addr: str, password: str, attempt: int) -> bool:
    """Login with email+password on accounts.x.ai if not already sessioned."""
    emit_progress(attempt, "login", "Email login (post-signup)", email_addr)
    try:
        cur = page.url or ""
    except Exception:
        cur = ""

    # If still on complete signup, try finishing there first
    if await page.locator("text=Complete your sign up").count() > 0:
        print(f"[{attempt}] Still on complete signup — finishing before login", flush=True)
        return True  # caller already tried; OAuth will re-login

    # Session cookies?
    try:
        cookies = await page.context.cookies()
        has_sess = any(
            any(k in (c.get("name") or "").lower() for k in ("session", "auth", "token", "sid"))
            for c in cookies
        )
        if has_sess and "sign-in" not in cur and "sign-up" not in cur:
            print(f"[{attempt}] Session cookies present — skip explicit login", flush=True)
            return True
    except Exception:
        pass

    if "sign-in" not in cur and await page.locator('input[type="password"]').count() == 0:
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.2)

    await dismiss_cookie_banner(page)
    await recover_page_load_error(page, attempt)
    # Prefer email path on provider chooser ("Login with email" on OAuth)
    await click_login_with_email(page)
    await asyncio.sleep(0.4)
    ok = await drive_email_password_login(page, email_addr, password, attempt)
    await screenshot(page, attempt, "after_login")
    return ok


async def obtain_oidc_tokens(page, email_addr: str, password: str, attempt: int) -> dict:
    """Device OAuth (grok2api-style). PKCE consent currently Access denied on xAI."""
    emit_progress(attempt, "oauth", "Starting Grok CLI Device OAuth", email_addr)

    # Ensure session (email login) before device approval page
    try:
        cur = page.url or ""
    except Exception:
        cur = ""
    if "sign-in" in cur or await page.locator('input[type="password"]').count() > 0:
        await click_login_with_email(page)
        await asyncio.sleep(0.3)
        await drive_email_password_login(page, email_addr, password, attempt)
    else:
        try:
            cookies = await page.context.cookies()
            has_sess = any(
                any(k in (c.get("name") or "").lower() for k in ("session", "auth", "token", "sid"))
                for c in cookies
            )
        except Exception:
            has_sess = False
        if not has_sess:
            await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(0.8)
            await dismiss_cookie_banner(page)
            await click_login_with_email(page)
            await drive_email_password_login(page, email_addr, password, attempt)

    last_err: Exception | None = None
    for oauth_try in range(1, 3):  # 1 hard retry on invalid_grant after UI success
        poll_task: asyncio.Task | None = None
        try:
            try:
                dev = await asyncio.to_thread(start_device_authorization)
            except Exception as e:
                raise RuntimeError(f"device/code failed: {e}") from e

            device_code = dev.get("device_code") or ""
            user_code = dev.get("user_code") or ""
            verify = dev.get("verification_uri_complete") or (
                f"{dev.get('verification_uri', 'https://accounts.x.ai/oauth2/device')}"
                f"?user_code={user_code}"
            )
            interval = float(dev.get("interval") or 5)
            if not device_code:
                raise RuntimeError(f"device/code missing device_code: {dev}")

            print(
                f"[{attempt}] device user_code={user_code} try={oauth_try}/2",
                flush=True,
            )

            # Navigate + consent FIRST, then poll (avoids early invalid_grant race)
            await page.goto(verify, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.0)
            await dismiss_cookie_banner(page)
            await recover_page_load_error(page, attempt)
            await handle_turnstile(page, attempt, max_wait=12)

            if await page.locator('input[type="email"], input[type="password"]').count() > 0:
                await click_login_with_email(page)
                await asyncio.sleep(0.3)
                await drive_email_password_login(page, email_addr, password, attempt)
                await asyncio.sleep(0.6)
                await page.goto(verify, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.0)

            approved = False
            for _ in range(12):
                await handle_turnstile(page, attempt, max_wait=6)
                body = ""
                try:
                    body = (await page.inner_text("body"))[:800].lower()
                except Exception:
                    pass
                if any(
                    x in body
                    for x in (
                        "success",
                        "approved",
                        "you can close",
                        "return to",
                        "device authorized",
                        "authorization complete",
                        "already authorized",
                    )
                ):
                    print(f"[{attempt}] device page looks approved", flush=True)
                    approved = True
                    break
                if "failed to generate" in body or "access denied" in body:
                    print(
                        f"[{attempt}] WARN device page text: access denied / failed generate",
                        flush=True,
                    )
                clicked = await click_text_button(
                    page,
                    [
                        "Continue",
                        "Allow",
                        "Authorize",
                        "Approve",
                        "Confirm",
                        "Yes",
                        "Accept",
                        "Next",
                    ],
                    exclude=["Google", "Deny", "Cancel", "Sign out"],
                )
                await asyncio.sleep(1.0 if clicked else 1.2)

            await screenshot(page, attempt, "device_oauth")

            # Explicit HTTP verify+approve (grok2api) — UI "Device Authorized"
            # alone often still yields invalid_grant on token poll.
            try:
                cookies = await page.context.cookies()
            except Exception:
                cookies = []
            cookie_hdr = _cookie_header_from_playwright(cookies)
            if cookie_hdr:
                ok_http, http_detail = await asyncio.to_thread(
                    device_verify_and_approve_http, user_code, cookie_hdr
                )
                print(
                    f"[{attempt}] device HTTP approve ok={ok_http} {http_detail}",
                    flush=True,
                )
                if ok_http:
                    approved = True
            else:
                print(f"[{attempt}] WARN no x.ai cookies for HTTP approve", flush=True)

            if approved:
                await asyncio.sleep(2.0)

            poll_task = asyncio.create_task(
                asyncio.to_thread(
                    poll_device_code,
                    device_code,
                    interval,
                    120.0,
                    wait_before_first=not approved,
                )
            )
            data = await asyncio.wait_for(poll_task, timeout=140.0)
            emit_progress(
                attempt, "token_exchange", "Device code exchanged for tokens", email_addr
            )
            tokens = _tokens_from_oauth_json(
                data, email_fallback=email_addr, auth_mode="device_oauth"
            )
            if not tokens.get("email"):
                tokens["email"] = email_addr
            return tokens
        except Exception as e:
            last_err = e
            if poll_task is not None and not poll_task.done():
                poll_task.cancel()
                try:
                    await poll_task
                except Exception:
                    pass
            low = str(e).lower()
            retryable = "invalid_grant" in low or "access denied" in low or "device poll denied" in low
            print(f"[{attempt}] device oauth try={oauth_try} fail: {e}", flush=True)
            try:
                await screenshot(page, attempt, f"oauth_no_code_t{oauth_try}")
            except Exception:
                pass
            if not retryable or oauth_try >= 2:
                break
            await asyncio.sleep(1.5)
            continue

    raise RuntimeError(f"Device OAuth failed: {last_err}") from last_err


# ── Worker ───────────────────────────────────────────────────────────────────
async def _do_register_body(attempt_num: int, email_addr: str, password: str, proxy_url: str, proxy_id: str) -> dict:
    """Core farm path — may raise. Caller applies account-level timeout."""
    emit_progress(attempt_num, "browser", "Launching Camoufox", email_addr)
    manager = None
    try:
        manager, browser, page = await launch_browser(proxy_url)
        _plog = "direct"
        if proxy_url:
            try:
                _u = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
                _plog = f"{_u.scheme}://{_u.hostname}:{_u.port or ''}"
                if _u.username:
                    _plog = f"{_u.scheme}://{_u.username}:***@{_u.hostname}:{_u.port or ''}"
            except Exception:
                _plog = (proxy_url[:32] + "…") if len(proxy_url) > 32 else proxy_url
        print(
            f"[{attempt_num}] Browser up: {email_addr} proxy={_plog} id={proxy_id or '-'}",
            flush=True,
        )
        await do_signup(page, email_addr, password, attempt_num)
        try:
            await do_email_login(page, email_addr, password, attempt_num)
        except Exception as e:
            print(f"[{attempt_num}] login branch warn: {e}", flush=True)

        tokens = await obtain_oidc_tokens(page, email_addr, password, attempt_num)
        return {
            "email": email_addr,
            "password": password,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempt": attempt_num,
            "proxy": proxy_url or "direct",
            "tokens": tokens,
        }
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


async def _do_register(attempt_num: int) -> dict | None:
    global _in_flight
    if_lock, can_start = _ensure_async_gates()

    # Don't start work while a WARP rotate is draining / in progress
    await can_start.wait()

    async with if_lock:
        _in_flight += 1
    email_addr = ""
    # sticky gptmail domain per concurrent slot
    worker_slot = (max(1, attempt_num) - 1) % max(1, CONCURRENT)
    try:
        email_addr = await generate_email(worker_slot=worker_slot)
        password = ACCOUNT_PASSWORD
        proxy_url, proxy_id = await next_proxy()
        _attempt_proxy[attempt_num] = proxy_id

        emit_progress(attempt_num, "start", f"Starting Grok farm #{attempt_num}", email_addr)
        try:
            result = await asyncio.wait_for(
                _do_register_body(attempt_num, email_addr, password, proxy_url, proxy_id),
                timeout=ACCOUNT_TIMEOUT_S,
            )
            await save_result_to_file(result)
            emit_success(attempt_num, email_addr, "Account farmed (tokens saved)")
            # Mid-batch WARP — drain peers first so rotate doesn't kill mid-flow workers
            if WARP_EVERY_N > 0:
                await _maybe_warp_after_success(attempt_num)
            return result
        except asyncio.TimeoutError:
            msg = f"account timeout after {ACCOUNT_TIMEOUT_S}s (stuck / unclear state)"
            print(f"[{attempt_num}] FAILED: {msg}", flush=True)
            emit_failed(attempt_num, msg, "AccountTimeout")
            try:
                await save_failed_to_file(attempt_num, email_addr, msg)
            except Exception:
                pass
            return None
        except Exception as e:
            print(f"[{attempt_num}] FAILED: {e}", flush=True)
            emit_failed(attempt_num, str(e)[:200], type(e).__name__)
            try:
                await save_failed_to_file(attempt_num, email_addr, str(e)[:400])
            except Exception:
                pass
            # gptmail: domain rejected → permanent block + next account new domain
            if EMAIL_MODE == "gptmail":
                low = str(e).lower()
                if (
                    "domain_not_allowed" in low
                    or "domain is not allowed" in low
                    or "email domain is not allowed" in low
                    or "not allowed to sign up" in low
                    or "disposable email" in low
                    or "has been rejected" in low
                    or "domain has been rejected" in low
                    or "use a different email" in low
                    or "rejected" in low and "domain" in low
                ):
                    dom = email_addr.split("@")[-1] if "@" in email_addr else ""
                    # prefer domain named in xAI error text if present
                    m = re.search(
                        r"email domain\s+([a-z0-9][a-z0-9._-]*\.[a-z0-9.-]+)\s+has been rejected",
                        low,
                    )
                    if m:
                        dom = m.group(1).strip(".")
                    gptmail_block_domain(dom, reason=str(e)[:120], worker_slot=worker_slot)
            return None
    finally:
        async with if_lock:
            _in_flight = max(0, _in_flight - 1)


async def register_one_account(attempt_num: int, semaphore: asyncio.Semaphore) -> dict | None:
    _, can_start = _ensure_async_gates()
    # Wait outside semaphore so we don't hold a slot while rotate drains
    await can_start.wait()
    async with semaphore:
        await can_start.wait()
        return await _do_register(attempt_num)


def _hub_root() -> Path | None:
    """Automation hub root if this farm is under farms/<id>/."""
    # farms/grok/farm.py → parents[0]=grok, [1]=farms, [2]=Automation
    try:
        p = _ROOT.resolve()
        if p.parent.name == "farms":
            return p.parent.parent
    except Exception:
        pass
    return None


def _warp_rotate_sync(attempt: int) -> bool:
    """Call hub core.warp (preferred) or skip if unavailable."""
    hub = _hub_root()
    if hub is None:
        print(f"[{attempt}] WARP every_n: hub root not found — skip", flush=True)
        return False
    hub_s = str(hub)
    if hub_s not in sys.path:
        sys.path.insert(0, hub_s)
    try:
        from core.warp import WarpClient  # type: ignore

        def _log(m: str) -> None:
            print(f"[{attempt}] {m}", flush=True)

        w = WarpClient(log=_log)
        if not w.ensure_connected():
            _log("WARP: not connected")
        r = w.rotate_ip(force=True)
        _log(f"WARP every_n rotate: {r}")
        return bool(r.ok)
    except Exception as e:
        print(f"[{attempt}] WARP every_n error: {type(e).__name__}: {e}", flush=True)
        return False


async def _maybe_warp_after_success(attempt: int) -> None:
    """Proactive rotate every WARP_EVERY_N successes (1:1 with concurrent).

    Wave mode: block new starts, drain peers, rotate once, settle, resume.
    Only one drain owner (avoids double-rotate spam).
    """
    global _success_since_warp, _warp_drain_owner
    every = _effective_warp_every_n()
    if every <= 0:
        return

    should_rotate = False
    with _warp_rotate_lock:
        _success_since_warp += 1
        n = _success_since_warp
        if n < every:
            print(
                f"[{attempt}] WARP every_n: success {n}/{every} (wave c={CONCURRENT})",
                flush=True,
            )
            return
        if _warp_drain_owner is not None:
            print(
                f"[{attempt}] WARP every_n: success {n}/{every} "
                f"(drain owned by #{_warp_drain_owner})",
                flush=True,
            )
            return
        _warp_drain_owner = attempt
        _success_since_warp = 0
        should_rotate = True
        print(
            f"[{attempt}] WARP every_n: wave complete {every}/{every} "
            f"→ drain then rotate…",
            flush=True,
        )

    if not should_rotate:
        return

    if_lock, can_start = _ensure_async_gates()
    can_start.clear()
    last_log = 0.0
    try:
        drain_deadline = time.time() + min(180.0, float(ACCOUNT_TIMEOUT_S))
        while True:
            async with if_lock:
                n_if = _in_flight
            if n_if <= 1:
                break
            if time.time() >= drain_deadline:
                print(
                    f"[{attempt}] WARP every_n: drain timeout "
                    f"(in_flight={n_if}) — rotate anyway",
                    flush=True,
                )
                break
            now = time.time()
            if now - last_log >= 5.0:
                print(
                    f"[{attempt}] WARP every_n: waiting peers "
                    f"(in_flight={n_if})…",
                    flush=True,
                )
                last_log = now
            await asyncio.sleep(0.5)

        print(f"[{attempt}] WARP every_n: drain ok → rotate IP…", flush=True)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _warp_rotate_sync(attempt))
        print(
            f"[{attempt}] WARP every_n: settle {WARP_SETTLE_S:.0f}s…",
            flush=True,
        )
        await asyncio.sleep(WARP_SETTLE_S)
    finally:
        with _warp_rotate_lock:
            _warp_drain_owner = None
        can_start.set()
        print(f"[{attempt}] WARP every_n: resume workers", flush=True)

_results_lock = asyncio.Lock()

# Lazy VPS batch pusher (same stack as reauth: merge-by-email every N OK)
_vps_pusher = None
_vps_pusher_lock = threading.Lock()


def _get_vps_pusher():
    """Create once: GROK_VPS_PUSH=1 (or true) + host/pass from hub .env."""
    global _vps_pusher
    with _vps_pusher_lock:
        if _vps_pusher is not None:
            return _vps_pusher if _vps_pusher is not False else None
        raw = (_env("GROK_VPS_PUSH") or "").strip().lower()
        if raw not in ("1", "true", "yes", "on"):
            _vps_pusher = False
            print(
                "[vps-push] off for farm (set GROK_VPS_PUSH=1 to inject VPS)",
                flush=True,
            )
            return None
        try:
            from vps_push import VpsBatchPusher  # same dir as farm.py

            every = max(1, int(_env("GROK_VPS_PUSH_EVERY") or "10"))
            _vps_pusher = VpsBatchPusher(
                every=every,
                log=lambda m: print(m, flush=True),
                enabled=True,
            )
            return _vps_pusher
        except Exception as e:
            print(f"[vps-push] init failed: {e}", flush=True)
            _vps_pusher = False
            return None


def _flush_vps_pusher() -> None:
    p = _get_vps_pusher()
    if p is None:
        return
    try:
        p.flush_remaining()
        print(
            f"[vps-push] farm summary flushes={p.flushes} pushed={p.pushed} "
            f"pruned_local={getattr(p, 'pruned', 0)} errors={p.errors}",
            flush=True,
        )
    except Exception as e:
        print(f"[vps-push] final flush error: {e}", flush=True)


def _inject_to_9router(result: dict) -> None:
    """Inject a farmed account directly into the 9router SQLite DB."""
    import secrets as _secrets
    DB_PATH = os.path.join(os.environ.get("APPDATA", ""), "9router", "db", "data.sqlite")
    if not os.path.isfile(DB_PATH):
        vlog(f"[9router inject] DB not found at {DB_PATH}, skipping")
        return
    try:
        import sqlite3
        tokens = result.get("tokens") or {}
        email = (result.get("email") or "").lower()
        if not email or not tokens.get("access_token"):
            vlog("[9router inject] missing email or access_token, skipping")
            return

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Check duplicate
        cur.execute("SELECT id FROM providerConnections WHERE email = ? AND provider = 'grok-cli'", (email,))
        if cur.fetchone():
            vlog(f"[9router inject] already exists: {email}")
            conn.close()
            return

        row_id = "grok-farm-" + _secrets.token_hex(8)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        expires_at = tokens.get("expires_at") or now

        data = json.dumps({
            "displayName": email,
            "accessToken": tokens.get("access_token", ""),
            "refreshToken": tokens.get("refresh_token", ""),
            "expiresAt": expires_at,
            "scope": tokens.get("scope", "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write"),
            # "active" = usable in pool without Test Connection One-by-One (UI badge).
            # Real failures later still flip to unavailable via 9router runtime.
            "testStatus": "active",
            "expiresIn": tokens.get("expires_in", 21600),
            "providerSpecificData": {
                "authMethod": tokens.get("auth_mode", "oidc"),
                "idToken": tokens.get("id_token", None),
                "email": email,
                "userId": None,
                "hasGrokCodeAccess": True,
                "subscriptionTier": None,
            },
            "lastError": None,
            "lastErrorAt": None,
        })

        cur.execute(
            "INSERT INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, "grok-cli", "oauth", email, email, 1, 1, data, now, now)
        )
        conn.commit()
        conn.close()
        print(f"[9router inject] ✓ {email} → local DB (id={row_id})", flush=True)
    except Exception as ex:
        vlog(f"[9router inject] ERROR: {ex}")


def _queue_vps_inject(result: dict) -> None:
    """Queue farmed tokens for VPS merge push (every GROK_VPS_PUSH_EVERY OK)."""
    tokens = result.get("tokens") or {}
    email = (result.get("email") or "").strip()
    if not email or not tokens.get("access_token") or not tokens.get("refresh_token"):
        return
    pusher = _get_vps_pusher()
    if pusher is None:
        return
    try:
        pusher.add(email, tokens)
    except Exception as e:
        print(f"[vps-push] queue error for {email}: {e}", flush=True)


async def save_result_to_file(result: dict):
    """Append success to JSON + one-line TXT (email|password|access_token|refresh_token)."""
    async with _results_lock:
        results = []
        if RESULTS_JSON.is_file():
            try:
                results = json.loads(RESULTS_JSON.read_text())
                if not isinstance(results, list):
                    results = []
            except Exception:
                results = []
        results.append(result)
        RESULTS_JSON.write_text(json.dumps(results, indent=2))

        tokens = result.get("tokens") or {}
        line = "|".join([
            str(result.get("email") or ""),
            str(result.get("password") or ""),
            str(tokens.get("access_token") or ""),
            str(tokens.get("refresh_token") or ""),
            str(tokens.get("expires_at") or ""),
        ])
        with open(RESULTS_TXT, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        email = (result.get("email") or "").lower()
        if email:
            _used_emails.add(email)
        vlog(f"saved → {RESULTS_JSON.name} + {RESULTS_TXT.name}")

    # Inject into local 9router DB (non-blocking)
    try:
        _inject_to_9router(result)
    except Exception as ex:
        vlog(f"[9router inject] unhandled: {ex}")

    # Merge-push to VPS 9router (batch every N; prune local after success if enabled)
    try:
        _queue_vps_inject(result)
    except Exception as ex:
        vlog(f"[vps-push] unhandled: {ex}")


async def save_failed_to_file(attempt: int, email: str, error: str):
    async with _results_lock:
        rows = []
        if FAILED_JSON.is_file():
            try:
                rows = json.loads(FAILED_JSON.read_text())
                if not isinstance(rows, list):
                    rows = []
            except Exception:
                rows = []
        rows.append({
            "attempt": attempt,
            "email": email,
            "error": error,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        FAILED_JSON.write_text(json.dumps(rows, indent=2))


def _prompt_int(label: str, default: int, *, min_v: int = 1, max_v: int = 1000) -> int:
    """Ask user for an int; Enter keeps default from .env."""
    while True:
        try:
            raw = input(f"  {label} [{default}]: ").strip()
        except EOFError:
            return default
        if raw == "":
            val = default
        else:
            try:
                val = int(raw)
            except ValueError:
                print(f"    → masukkan angka (min {min_v}, max {max_v})", flush=True)
                continue
        if val < min_v or val > max_v:
            print(f"    → harus antara {min_v}–{max_v}", flush=True)
            continue
        return val


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {label} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if raw == "":
        return default
    return raw in ("y", "yes", "1", "true")


async def main():
    global CONCURRENT
    if EMAIL_MODE in ("domain", "plus_trick"):
        if not IMAP_USER or not IMAP_PASS:
            print("ERROR: set GROK_IMAP_USER and GROK_IMAP_PASS in .env", flush=True)
            sys.exit(1)
    if EMAIL_MODE == "domain" and not EMAIL_DOMAIN:
        print("ERROR: set GROK_EMAIL_DOMAIN for domain mode", flush=True)
        sys.exit(1)
    if EMAIL_MODE == "plus_trick" and not (GMAIL_BASE or IMAP_USER):
        print("ERROR: set GROK_GMAIL_BASE or GROK_IMAP_USER for plus_trick", flush=True)
        sys.exit(1)
    if EMAIL_MODE == "gptmail" and not GPTMAIL_API:
        print("ERROR: set GROK_GPTMAIL_API for gptmail mode", flush=True)
        sys.exit(1)
    if EMAIL_MODE == "exzork":
        if not EXZORK_API_KEY:
            print("ERROR: set GROK_EXZORK_API_KEY for exzork mode", flush=True)
            sys.exit(1)
        if not EXZORK_DOMAIN:
            print("ERROR: set GROK_EXZORK_DOMAIN or GROK_EMAIL_DOMAIN for exzork mode", flush=True)
            sys.exit(1)

    _load_used_emails()
    if EMAIL_MODE == "gptmail":
        _load_blocked_domains()
    known = len(_used_emails)

    print("=" * 60, flush=True)
    print("  Grok / xAI Standalone Farmer", flush=True)
    print("=" * 60, flush=True)
    print(f"  Email mode : {EMAIL_MODE}", flush=True)
    if EMAIL_MODE == "domain":
        print(f"  Domain     : @{EMAIL_DOMAIN}", flush=True)
        print(f"  IMAP       : {IMAP_USER} @ {IMAP_HOST}:{IMAP_PORT}", flush=True)
    elif EMAIL_MODE == "plus_trick":
        print(f"  Gmail base : {GMAIL_BASE or IMAP_USER}", flush=True)
        print(f"  IMAP       : {IMAP_USER} @ {IMAP_HOST}:{IMAP_PORT}", flush=True)
    elif EMAIL_MODE == "exzork":
        print(f"  Exzork     : {EXZORK_API}", flush=True)
        print(
            f"  Domain     : {'*.' if EXZORK_WILDCARD else ''}{EXZORK_DOMAIN} "
            f"(wildcard={'on' if EXZORK_WILDCARD else 'off'})",
            flush=True,
        )
        print(f"  API key    : {EXZORK_API_KEY[:10]}…", flush=True)
    else:
        print(f"  GPTMail    : {GPTMAIL_API}", flush=True)
        print(f"  Pin domain : {GPTMAIL_DOMAIN or '(auto pool)'}", flush=True)
    print(f"  Password   : {'*' * max(0, len(ACCOUNT_PASSWORD) - 2)}{ACCOUNT_PASSWORD[-2:]}", flush=True)
    print(f"  Headless   : {HEADLESS}", flush=True)
    print(
        f"  Proxies    : {len(PROXY_POOL)} ({PROXY_SOURCE})"
        if PROXY_POOL
        else f"  Proxies    : direct ({PROXY_SOURCE})",
        flush=True,
    )
    print(f"  Email len  : {EMAIL_LOCAL_LEN} (crypto secrets)", flush=True)
    print(f"  Known mail : {known} (all batches + used_emails.txt)", flush=True)
    print(f"  Results    : {RESULTS_ROOT}/batch_<id>/  (per run)", flush=True)
    print("-" * 60, flush=True)
    print("  Setting run (Enter = pakai default dari .env)", flush=True)

    # CLI args override: python farm.py --count 10 --concurrent 2 --yes
    arg_count: int | None = None
    arg_conc: int | None = None
    skip_prompt = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--count", "--max") and i + 1 < len(args):
            arg_count = int(args[i + 1])
            i += 2
            continue
        if a in ("-c", "--concurrent") and i + 1 < len(args):
            arg_conc = int(args[i + 1])
            i += 2
            continue
        if a in ("-y", "--yes", "--non-interactive"):
            skip_prompt = True
            i += 1
            continue
        if a in ("-h", "--help"):
            print(
                "Usage: farm.py [-n COUNT] [-c CONCURRENT] [-y]\n"
                "  -n/--count        jumlah akun batch ini (default: tanya / .env)\n"
                "  -c/--concurrent   browser paralel (default: tanya / .env)\n"
                "  -y/--yes          non-interactive, pakai .env / flags saja\n"
                "Each run writes to results/batch_<timestamp>/ (fresh files).\n"
                "Emails stay unique across batches via results/used_emails.txt",
                flush=True,
            )
            sys.exit(0)
        i += 1

    if skip_prompt:
        max_accounts = arg_count if arg_count is not None else MAX_ACCOUNTS
        concurrent = arg_conc if arg_conc is not None else CONCURRENT
    else:
        max_accounts = arg_count if arg_count is not None else _prompt_int(
            "Berapa akun yang mau di-farm (batch ini)?", MAX_ACCOUNTS, min_v=1, max_v=1000
        )
        concurrent = arg_conc if arg_conc is not None else _prompt_int(
            "Concurrency (browser paralel)?", CONCURRENT, min_v=1, max_v=20
        )
        if not _prompt_yes_no(f"Mulai farm {max_accounts} akun × concurrent {concurrent}?", True):
            print("  Dibatalkan.", flush=True)
            sys.exit(0)

    max_accounts = max(1, min(1000, int(max_accounts)))
    concurrent = max(1, min(20, int(concurrent)))
    # Keep module CONCURRENT in sync so WARP everyN 1:1 uses live -c
    CONCURRENT = concurrent

    # Fresh batch folder for this run (results isolated per batch)
    init_batch(max_accounts, concurrent)
    # this batch starts empty — count is "how many this run", not cumulative
    target = max_accounts

    print("-" * 60, flush=True)
    print(f"  Batch      : {BATCH_ID}", flush=True)
    print(f"  Create     : {max_accounts} accounts (concurrent={concurrent})", flush=True)
    print(f"  Out        : {BATCH_DIR}", flush=True)
    print(f"  UI         : {UI_MODE}" + (" + verbose" if VERBOSE else ""), flush=True)
    print("=" * 60, flush=True)

    # Full detail always lands in batch farm.log; HUD keeps terminal clean
    log_path = BATCH_DIR / "farm.log"
    HUD.open_log(log_path)
    HUD.start(max_accounts, batch_id=BATCH_ID, batch_dir=str(BATCH_DIR))

    # Mute noisy print() → farm.log while HUD is on (banner above already shown)
    import builtins
    _orig_print = builtins.print

    def _quiet_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        msg = sep.join(str(a) for a in args)
        if not HUD.enabled or VERBOSE or kwargs.pop("force_console", False):
            _orig_print(*args, **{k: v for k, v in kwargs.items() if k != "force_console"})
        else:
            HUD.log_line(msg)

    if HUD.enabled and not VERBOSE:
        builtins.print = _quiet_print  # type: ignore[assignment]

    semaphore = asyncio.Semaphore(concurrent)
    created = 0
    failed = 0
    next_attempt = 1
    counter_lock = asyncio.Lock()
    start = time.time()
    tick = asyncio.create_task(HUD.ticker())

    async def worker():
        nonlocal created, failed, next_attempt
        while True:
            async with counter_lock:
                if next_attempt > target:
                    return
                num = next_attempt
                next_attempt += 1
            # Stagger browser launch so concurrent workers don't all hit CF/IMAP at once
            if SPAWN_DELAY > 0:
                await asyncio.sleep(SPAWN_DELAY * ((num - 1) % max(1, concurrent)))
            res = await register_one_account(num, semaphore)
            async with counter_lock:
                if res:
                    created += 1
                else:
                    failed += 1
            # HUD already updated via emit_success / emit_failed

    try:
        workers = [asyncio.create_task(worker()) for _ in range(concurrent)]
        await asyncio.gather(*workers)
        # Flush remaining VPS inject queue (< every-N leftovers)
        try:
            await asyncio.to_thread(_flush_vps_pusher)
        except Exception as e:
            print(f"[vps-push] end flush: {e}", flush=True)
    finally:
        tick.cancel()
        try:
            await tick
        except (asyncio.CancelledError, Exception):
            pass
        builtins.print = _orig_print  # type: ignore[assignment]
        HUD.stop()
        HUD.close_log()

    # finalize batch meta
    try:
        meta_path = BATCH_DIR / "batch_meta.json"
        meta = {}
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update({
            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "created": created,
            "failed": failed,
            "elapsed_s": int(time.time() - start),
        })
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

    print("=" * 60, flush=True)
    print(f"  DONE: {created} created, {failed} failed in {int(time.time() - start)}s", flush=True)
    print(f"  Batch: {BATCH_ID}", flush=True)
    print(f"  Dir  : {BATCH_DIR}", flush=True)
    print(f"  JSON : {RESULTS_JSON}", flush=True)
    print(f"  TXT  : {RESULTS_TXT}", flush=True)
    print(f"  Log  : {log_path}", flush=True)
    print(f"  Used : {USED_EMAILS_FILE}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    # Camoufox under /root/.cache is often incomplete → "Couldn't load XPCOM"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print(
            "ERROR: jangan jalankan farm sebagai root/sudo.\n"
            "  Pakai user yang install Camoufox (biasanya priyo), bukan root.\n"
            "  Root's ~/.cache/camoufox is incomplete → XPCOM launch crash.",
            flush=True,
        )
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted", flush=True)
        sys.exit(130)
