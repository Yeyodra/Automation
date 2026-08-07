#!/usr/bin/env python3
"""
Enter / Converge account farmer (pattern from grok-farm).

Flow per account (from HAR HTTPToolkit_2026-07-16):
  1. Generate email (catch-all domain OR Gmail plus-trick)
  2. Camoufox -> enter.converge.ai/?gift=CODE (referral landing)
  3. Auth0 Universal Login signup (identifier + captcha/Turnstile)
  4. Email OTP (IMAP, 6-digit) -> password
  5. OAuth PKCE callback -> access/refresh tokens
  6. POST referral/claim + onboarding phase1
  7. POST workspace api-keys -> save batch results

Hub: farms/enter — env ENTER_* (mapped from Automation/.env).
Run:    python -m jobs run enter -- -n 3 -c 1 -y
WARP: hub injects WARP_EVERY_N (1:1 with -c); farm rotates via core.warp.
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
from urllib.parse import parse_qs, quote, urlencode, urlparse

_ROOT = Path(__file__).resolve().parent
_HUB = _ROOT.parent.parent
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

try:
    from dotenv import load_dotenv

    # override=False so shell/CLI env can win over .env defaults
    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed. pip install -r requirements.txt && camoufox fetch", flush=True)
    sys.exit(1)



# ── Config ───────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


# ── Farm HUD (live panel, Windows VT-safe) ───────────────────────────────────
# ENTER_UI=hud|log  (default: hud on TTY). ENTER_VERBOSE=true → detail under panel.
# Default LOG (clean lines). Panel HUD off unless ENTER_UI=hud (often spam on Windows).
_UI_ENV = _env("ENTER_UI", "log").lower()
if _UI_ENV in ("hud", "tui", "progress"):
    UI_MODE = "hud"
else:
    UI_MODE = "log"
VERBOSE = _env_bool("ENTER_VERBOSE", False)

# Fixed panel height so redraw never stacks (critical on Windows without VT).
_HUD_WORKER_SLOTS = 4
_HUD_RECENT_SLOTS = 2
_HUD_WIDTH = 64
_HUD_LINES = 9 + _HUD_WORKER_SLOTS + _HUD_RECENT_SLOTS  # header+stats+sep+workers+sep+recent+out+footer+hint


def _enable_windows_vt() -> bool:
    """Enable ANSI cursor control on Windows 10+ consoles. Return True if OK."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
        if not kernel32.SetConsoleMode(h, new_mode):
            return False
        return True
    except Exception:
        return False


def _short_email(email: str, width: int = 28) -> str:
    e = (email or "").strip()
    if len(e) <= width:
        return e
    if "@" in e:
        local, _, dom = e.partition("@")
        keep = max(3, width - len(dom) - 4)
        return f"{local[:keep]}..@{dom}"[:width]
    return e[: max(1, width - 2)] + ".."


def _bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    filled = int(width * min(done, total) / total)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def _clip(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").replace("\r", "")
    return s if len(s) <= n else s[: max(0, n - 2)] + ".."


class FarmHUD:
    """Fixed-height live panel. Falls back to single status line if VT fails."""

    def __init__(self) -> None:
        # Prefer panel whenever user asked for hud (or default on TTY).
        # On Windows, try VT; if VT fails we still try panel once and
        # auto-disable only if redraw throws (see render).
        # Panel only when user explicitly set ENTER_UI=hud AND VT works.
        want = UI_MODE == "hud"
        self._vt = _enable_windows_vt() if want else False
        self.enabled = bool(want and self._vt and sys.stdout.isatty())
        self.total = 0
        self.ok = 0
        self.fail = 0
        self.batch_id = ""
        self.batch_dir = ""
        self.gift = ""
        self.started = time.time()
        self._workers: dict[int, dict[str, Any]] = {}
        self._recent: list[str] = []
        self._slock = threading.Lock()
        self._drawn = False
        self._log_fp = None
        self._real_stdout = sys.stdout
        self._emails: dict[int, str] = {}
        self._status = "starting..."

    def open_log(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(path, "a", encoding="utf-8")
            self._log_fp.write(
                f"\n===== farm start {datetime.now(timezone.utc).isoformat()} =====\n"
            )
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

    def log_line(self, line: str, *, quiet: bool = False) -> None:
        """Write farm.log always. Console only if not quiet (and not under live HUD)."""
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {line}"
        if self._log_fp:
            try:
                self._log_fp.write(full + "\n")
                self._log_fp.flush()
            except Exception:
                pass
        # Live HUD panel: keep terminal clean unless VERBOSE
        if self.enabled and not VERBOSE:
            return
        if quiet and not VERBOSE:
            return
        try:
            self._real_stdout.write(full + "\n")
            self._real_stdout.flush()
        except Exception:
            pass

    def start(self, total: int, batch_id: str = "", batch_dir: str = "", gift: str = "") -> None:
        self.total = total
        self.ok = 0
        self.fail = 0
        self.batch_id = batch_id
        self.batch_dir = batch_dir
        self.gift = gift
        self.started = time.time()
        self._workers.clear()
        self._recent.clear()
        self._emails.clear()
        self._drawn = False
        self._status = "running"
        if self.enabled:
            try:
                self._real_stdout.write("\033[?25l")  # hide cursor
                self._real_stdout.flush()
            except Exception:
                pass
        self.render(force=True)

    def stop(self) -> None:
        self._status = "done"
        self.render(force=True)
        if self.enabled:
            try:
                self._real_stdout.write("\033[?25h")  # show cursor
                self._real_stdout.write("\n")
                self._real_stdout.flush()
            except Exception:
                pass

    def set_progress(self, attempt: int, step: str, message: str = "", email: str = "") -> None:
        # attempt<=0 is system/WARP bootstrap — farm.log only, not console noise
        if attempt is None or int(attempt) <= 0:
            self.log_line(f"[sys] {step} {message}", quiet=True)
            return
        with self._slock:
            now = time.time()
            if email:
                self._emails[attempt] = email
            em = email or self._emails.get(attempt, "")
            w = self._workers.get(attempt)
            if not w:
                w = {
                    "attempt": attempt,
                    "email": em,
                    "step": step,
                    "message": message,
                    "t0": now,
                    "step_t0": now,
                }
            else:
                if step and step != w.get("step"):
                    w["step_t0"] = now
                if em:
                    w["email"] = em
                w["step"] = step
                w["message"] = message
            w["updated"] = now
            self._workers[attempt] = w
        # Full detail always in farm.log; console = short step line
        self.log_line(
            f"[{attempt}] {step:12} {message}" + (f"  <{em}>" if em else ""),
            quiet=True,
        )
        short_em = _short_email(em, 28) if em else ""
        console = f"[{attempt}] {step:<10}"
        if short_em:
            console += f"  {short_em}"
        if message and step not in message:
            # tiny hint without dumping URLs
            hint = message
            if "http" in hint:
                hint = step
            elif len(hint) > 40:
                hint = hint[:38] + ".."
            if hint and hint.lower() != step.lower():
                console += f"  {hint}"
        # print short line directly (not via quiet log_line)
        if not self.enabled or VERBOSE:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                self._real_stdout.write(f"[{ts}] {console}\n")
                self._real_stdout.flush()
            except Exception:
                pass
        self.render()

    def mark_ok(self, attempt: int, email: str, message: str = "ok") -> None:
        with self._slock:
            self.ok += 1
            self._workers.pop(attempt, None)
            if email:
                self._emails[attempt] = email
            self._recent.append(f"OK  #{attempt} {_short_email(email, 28)}")
            self._recent = self._recent[-_HUD_RECENT_SLOTS:]
        self.log_line(f"[{attempt}] OK  {_short_email(email, 36)}  {message}")
        self.render(force=True)

    def mark_fail(self, attempt: int, message: str, error: str = "") -> None:
        with self._slock:
            self.fail += 1
            email = ""
            w = self._workers.pop(attempt, None)
            if w:
                email = w.get("email") or ""
            msg = _clip(error or message or "fail", 40)
            self._recent.append(f"FAIL #{attempt} {msg}")
            self._recent = self._recent[-_HUD_RECENT_SLOTS:]
        err = _clip(error or message or "fail", 50)
        em = _short_email(email, 28) if email else ""
        self.log_line(f"[{attempt}] FAIL  {em}  {err}".rstrip())
        self.render(force=True)

    def _row(self, inner: str) -> str:
        # fixed width content area
        w = _HUD_WIDTH
        body = _clip(inner, w)
        return "|" + body.ljust(w) + "|"

    def _build_lines(self) -> list[str]:
        w = _HUD_WIDTH
        elapsed = int(time.time() - self.started)
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        et = f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        done = self.ok + self.fail
        running = len(self._workers)
        pct = int(100 * done / self.total) if self.total else 0
        bar = _bar(done, self.total, 18)
        bid = _clip(self.batch_id or "-", 28)

        lines: list[str] = []
        lines.append("+" + f" ENTER FARM  {bid} ".center(w, "-")[:w] + "+")
        lines.append(self._row(f" {bar}  {done}/{self.total}  {pct}%"))
        lines.append(
            self._row(
                f" ok={self.ok}  fail={self.fail}  run={running}  time {et}"
                + (f"  gift={_clip(self.gift, 12)}" if self.gift else "")
            )
        )
        lines.append("|" + ("-" * w) + "|")

        # workers: always exactly N slots
        workers = sorted(self._workers.values(), key=lambda x: x["attempt"])
        for i in range(_HUD_WORKER_SLOTS):
            if i < len(workers):
                wr = workers[i]
                age = int(time.time() - wr.get("step_t0", wr.get("t0", time.time())))
                total_t = int(time.time() - wr.get("t0", time.time()))
                em = _short_email(wr.get("email") or "-", 20)
                step = _clip(wr.get("step") or "-", 10)
                lines.append(
                    self._row(
                        f" #{wr['attempt']:<3} {em:<20} {step:<10} {age:>3}s  (tot {total_t}s)"
                    )
                )
            else:
                lines.append(self._row(" ·"))

        lines.append("|" + ("-" * w) + "|")

        # recent: always fixed slots
        recent = list(self._recent[-_HUD_RECENT_SLOTS:])
        while len(recent) < _HUD_RECENT_SLOTS:
            recent.insert(0, "")
        for r in recent:
            lines.append(self._row(" " + (r if r else "·")))

        # out path
        bd = self.batch_dir or "-"
        if len(bd) > w - 6:
            bd = "..." + bd[-(w - 9) :]
        lines.append(self._row(f" out {bd}"))
        lines.append("+" + ("-" * w) + "+")
        lines.append(_clip("  log: farm.log   ENTER_UI=log for lines   ENTER_VERBOSE=1", w + 2))

        # hard guarantee fixed count
        while len(lines) < _HUD_LINES:
            lines.append("")
        return lines[:_HUD_LINES]

    def render(self, force: bool = False) -> None:
        # Panel disabled → no console redraw at all (detail already in log_line / farm.log)
        if not self.enabled:
            return
        with self._slock:
            lines = self._build_lines()
            out = self._real_stdout
            try:
                if self._drawn:
                    out.write(f"\033[{_HUD_LINES}A")
                for line in lines:
                    out.write("\033[2K" + line + "\n")
                out.flush()
                self._drawn = True
            except Exception:
                self.enabled = False

    async def ticker(self) -> None:
        if not self.enabled:
            return
        try:
            while True:
                await asyncio.sleep(1.0)
                if self.ok + self.fail >= self.total and not self._workers:
                    break
                self.render()
        except asyncio.CancelledError:
            return


HUD = FarmHUD()


def _infer_step(msg: str) -> str:
    """Map alog text → short step label for HUD.

    Do NOT tag intermediate retries / Auth0 risk blocks as FAIL.
    Terminal failures use emit_failed → mark_fail (explicit FAIL step).
    """
    low = f" {msg.lower()} "
    rules = (
        ("START", ("start ",)),
        ("RATE", ("rate limit", "cooldown", "rate-limit", "global cooldown")),
        # Auth0 risk_control_blocked is recovery path, not terminal fail
        ("RISK", ("risk_control", "risk block", "risk/", "access_denied", "error_description")),
        ("WARP", ("warp",)),
        ("EMAIL", ("email", "gptmail", "tempmail")),
        ("OTP", ("otp", "imap", "verification code")),
        ("PROXY", ("proxy",)),
        ("BROWSER", ("browser", "screenshot", "camoufox")),
        ("LAND", ("landing",)),
        ("AUTH", ("authorize", "auth0", "signup", "login", "sign in", "session", "oauth", "pkce", "token", "snarf", "recovery")),
        ("CAPTCHA", ("captcha", "turnstile")),
        ("CLAIM", ("referral", "claim", "gift")),
        ("ONBOARD", ("onboard", "workspace", "phase1")),
        ("KEY", ("api key", "api-key", "apikey")),
        ("SAVE", ("save", "credentials")),
        # Terminal-ish phrases only (not mid-flow TimeoutError / access_denied)
        ("FAIL", ("failed after", "account timeout", "otp timeout", "could not ", " expected ")),
    )
    for name, keys in rules:
        if any(k in low for k in keys):
            return name
    return "FLOW"


def alog(attempt: int, msg: str, level: str | None = None) -> None:
    msg = str(msg).strip()
    step = _infer_step(msg)
    email = ""
    m = re.search(r"[\w.+\-]+@[\w.\-]+\.\w+", msg)
    if m:
        email = m.group(0)
    HUD.set_progress(int(attempt or 0), step, msg[:120], email)


def slog(tag: str, msg: str, *, quiet: bool = False) -> None:
    # Quiet system tags by default (full detail still in farm.log)
    if tag.upper() in ("BATCH", "CFG", "GPTMAIL", "WARN", "SYS") and not quiet:
        # still show WARN/DONE-ish; BATCH/CFG/GPTMAIL → farm.log only unless VERBOSE
        if tag.upper() in ("BATCH", "CFG", "GPTMAIL") and not VERBOSE:
            HUD.log_line(f"[{tag}] {msg}", quiet=True)
            return
    HUD.log_line(f"[{tag}] {msg}", quiet=quiet)


def emit_progress(attempt: int, step: str, message: str, email_addr: str = "", **kwargs):
    email = email_addr or kwargs.get("email") or ""
    HUD.set_progress(int(attempt or 0), step, message, email)


def emit_success(attempt: int, email_addr: str, message: str = "ok"):
    HUD.mark_ok(int(attempt or 0), email_addr, message)


def emit_failed(attempt: int, message: str, error: str = ""):
    HUD.mark_fail(int(attempt or 0), message, error)


def vlog(msg: str, attempt: int | None = None) -> None:
    prefix = f"[{attempt}] " if attempt is not None else ""
    HUD.log_line(prefix + msg)


IMAP_USER = _env("ENTER_IMAP_USER")
IMAP_PASS = _env("ENTER_IMAP_PASS").replace(" ", "")
IMAP_HOST = _env("ENTER_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(_env("ENTER_IMAP_PORT", "993") or "993")
EMAIL_DOMAIN = _env("ENTER_EMAIL_DOMAIN").lstrip("@")
# Default gptmail: Auth0 often blocks catch-all domains; IMAP not required.
EMAIL_MODE = _env("ENTER_EMAIL_MODE", "gptmail").lower()
# domain | plus_trick | tempmail | gptmail | generator | exzork | emailqu | rotate
if EMAIL_MODE not in ("plus_trick", "domain", "tempmail", "gptmail", "generator", "exzork", "emailqu", "rotate"):
    EMAIL_MODE = "gptmail"
GMAIL_BASE = _env("ENTER_GMAIL_BASE").lower() or IMAP_USER.lower()
TEMPMAIL_API = _env("ENTER_TEMPMAIL_API", "https://api.mail.tm").rstrip("/")
TEMPMAIL_PROVIDER = _env("ENTER_TEMPMAIL_PROVIDER", "mail.tm").lower()
TEMPMAIL_ROTATION = tuple(
    x.strip().lower()
    for x in _env(
        "ENTER_TEMPMAIL_ROTATION",
        "generator,emailqu,exzork,mail.tm,tempmail.io,guerrillamail",
    ).split(",")
    if x.strip()
)
TEMPMAIL_IO_API = _env("ENTER_TEMPMAIL_IO_API", "https://api.internal.temp-mail.io/api/v3").rstrip("/")
GUERRILLA_API = _env("ENTER_GUERRILLA_API", "https://api.guerrillamail.com/ajax.php")
# GPTMail (https://mail.chatgpt.org.uk) — claim inbox via POST /api/inbox-token
GPTMAIL_API = _env("ENTER_GPTMAIL_API", "https://mail.chatgpt.org.uk").rstrip("/")
GPTMAIL_DOMAIN = _env("ENTER_GPTMAIL_DOMAIN").lstrip("@").lower()  # optional pin
GPTMAIL_PREFIX = _env("ENTER_GPTMAIL_PREFIX").lower()  # optional fixed local prefix
EXZORK_API = _env("ENTER_EXZORK_API", _env("EXZORK_API", "https://mailer.exzork.me")).rstrip("/")
EXZORK_API_KEY = _env("ENTER_EXZORK_API_KEY", _env("EXZORK_API_KEY"))
EXZORK_DOMAIN = _env("ENTER_EXZORK_DOMAIN", _env("EXZORK_DOMAIN")).lstrip("@").lstrip("*.").lower()
EXZORK_WILDCARD = _env_bool("ENTER_EXZORK_WILDCARD", True)
EMAILQU_API = _env("ENTER_EMAILQU_API", "https://emailqu.com").rstrip("/")
ACCOUNT_PASSWORD = _env("ENTER_PASSWORD", "@EnterPass1")
MAX_ACCOUNTS = int(_env("ENTER_MAX_ACCOUNTS", "1") or "1")
CONCURRENT = int(_env("ENTER_CONCURRENT", "1") or "1")
HEADLESS = _env_bool("ENTER_HEADLESS", False)
# Safe defaults for direct-IP farming (override via .env / CLI)
SPAWN_DELAY = float(_env("ENTER_SPAWN_DELAY", "30") or "30")
# After each account finishes (ok/fail), pause before freeing the worker slot
ACCOUNT_GAP = float(_env("ENTER_ACCOUNT_GAP", "30") or "30")
# Global pause when Auth0 shows "Too many signup attempts"
RATE_LIMIT_COOLDOWN = float(_env("ENTER_RATE_LIMIT_COOLDOWN", "300") or "300")
# Hub global WARP every-N (injected as WARP_EVERY_N / ENTER_WARP_EVERY_N). 0 = off.
# When >0, hub forces everyN == concurrent (-c). Rotate via core.warp (not local warp-cli).
WARP_EVERY_N = max(
    0,
    int(_env("ENTER_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0") or 0,
)
WARP_SETTLE_S = max(
    3.0,
    float(_env("WARP_SETTLE_AFTER") or _env("ENTER_WARP_SETTLE") or "8") or 8.0,
)
# Reactive rotate on Auth0 rate-limit / nav hang (still hub core.warp)
WARP_ON_RATE_LIMIT = _env_bool("ENTER_WARP_ON_RATE_LIMIT", True)
WARP_SKIP_LONG_COOLDOWN = _env_bool("ENTER_WARP_SKIP_LONG_COOLDOWN", True)
WARP_COOLDOWN_AFTER = float(_env("ENTER_WARP_COOLDOWN_AFTER", "15") or "15")
VPNX_API = _env("ENTER_VPNX_API").rstrip("/")
VPNX_TOKEN = _env("ENTER_VPNX_TOKEN")
VPNX_COUNTRY = (_env("ENTER_VPNX_COUNTRY", "JP") or "JP").upper()
VPNX_SETTLE_S = max(3.0, float(_env("ENTER_VPNX_SETTLE", "15") or "15"))
VPNX_EVERY_N = max(0, int(_env("ENTER_VPNX_EVERY_N", "0") or "0"))
# Legacy alias: ENTER_WARP_ROTATE=true still enables reactive rotates
if _env_bool("ENTER_WARP_ROTATE", False):
    WARP_ON_RATE_LIMIT = True
_success_since_warp = 0
_warp_rotate_lock = threading.Lock()
_in_flight = 0
_in_flight_lock: asyncio.Lock | None = None
_can_start: asyncio.Event | None = None
_warp_drain_owner: int | None = None

GIFT_CODE = _env("ENTER_GIFT_CODE", "2CL8V7UQ6R")
INVITER = _env("ENTER_INVITER", "Akun Ninja")
INVITEE_REWARD = _env("ENTER_INVITEE_REWARD", "100")

API_KEY_NAME = _env("ENTER_API_KEY_NAME", "farm")
API_KEY_SCOPE = _env("ENTER_API_KEY_SCOPE", "all")
API_KEY_REVEAL = _env("ENTER_API_KEY_REVEAL", "create_only")
BUILD_INTENT = _env("ENTER_BUILD_INTENT", "other")
RELEASE_FORM = _env("ENTER_RELEASE_FORM", "desktop")
ONBOARDING_ROLE = _env("ENTER_ONBOARDING_ROLE", "founder")
ONBOARDING_INDUSTRY = _env("ENTER_ONBOARDING_INDUSTRY", "manufacturing")
ONBOARDING_TEAM_SIZE = _env("ENTER_ONBOARDING_TEAM_SIZE", "6-20")
ONBOARDING_AGENCY_INTEREST = _env("ENTER_ONBOARDING_AGENCY_INTEREST", "in_house")

# Optional: auto-inject farmed ek_ into 9router SQLite DB (same approach as grok-farm)
# No HTTP auth — writes %APPDATA%\9router\db\data.sqlite directly.
# ENTER_9ROUTER_INJECT=true
NINEROUTER_INJECT = _env_bool("ENTER_9ROUTER_INJECT", True)
NINEROUTER_PROVIDER = _env("ENTER_9ROUTER_PROVIDER", "enter-converge")
NINEROUTER_PRIORITY = max(1, int(_env("ENTER_9ROUTER_PRIORITY", "1") or "1"))
_NINEROUTER_DB_DEFAULT = str(
    Path(os.environ.get("APPDATA", "")) / "9router" / "db" / "data.sqlite"
)
NINEROUTER_DB = _env("ENTER_9ROUTER_DB", _NINEROUTER_DB_DEFAULT)
# VPS push: auto-push to remote 9router every N OK (0=off)
NINEROUTER_VPS_EVERY_N = max(0, int(_env("ENTER_9ROUTER_VPS_EVERY_N", "3") or "3"))
_vps_pusher = None  # initialized in main() if every_n > 0

# Auth0/catch-all can be slow; grok uses 120–180 default — we use 180
OTP_TIMEOUT_S = max(60, int(_env("ENTER_OTP_TIMEOUT", "180") or "180"))
ACCOUNT_TIMEOUT_S = max(120, int(_env("ENTER_ACCOUNT_TIMEOUT", "600") or "600"))
TURNSTILE_PARALLEL = max(1, int(_env("ENTER_TURNSTILE_PARALLEL", "1") or "1"))
# Landing/authorize navigation (enter.converge.ai often hangs under CF/WARP heat)
GOTO_TIMEOUT_MS = max(15000, int(_env("ENTER_GOTO_TIMEOUT_MS", "45000") or "45000"))
GOTO_RETRIES = max(1, int(_env("ENTER_GOTO_RETRIES", "3") or "3"))
GOTO_RETRY_DELAY = max(0.5, float(_env("ENTER_GOTO_RETRY_DELAY", "3") or "3"))
GOTO_WARP_ON_FAIL = _env_bool("ENTER_GOTO_WARP_ON_FAIL", True)

CAPTCHA_PROXY_URL = _env("ENTER_CAPTCHA_PROXY_URL", "")
CAPTCHA_API_KEY = _env("ENTER_CAPTCHA_API_KEY", "")
CAPTCHA_MODEL = _env("ENTER_CAPTCHA_MODEL", "gpt-4o")

RESULTS_ROOT = Path(_env("ENTER_RESULTS_DIR", str(_ROOT / "results")))
USED_EMAILS_FILE = Path(_env("ENTER_USED_EMAILS_FILE", str(RESULTS_ROOT / "used_emails.txt")))
# Domains rejected by Auth0 (gptmail) — permanent across runs
BLOCKED_DOMAINS_FILE = Path(
    _env("ENTER_BLOCKED_DOMAINS_FILE", str(RESULTS_ROOT / "gptmail_blocked_domains.txt"))
)
EMAIL_LOCAL_LEN = max(10, min(32, int(_env("ENTER_EMAIL_LOCAL_LEN", "16") or "16")))
# Max domain retries per account when Auth0 says domain not allowed
GPTMAIL_DOMAIN_RETRIES = max(1, int(_env("ENTER_GPTMAIL_DOMAIN_RETRIES", "8") or "8"))
SCREENSHOT_DIR = Path(_env("ENTER_SCREENSHOT_DIR", str(_ROOT / "screenshots")))

AUTH_HOST = _env("ENTER_AUTH_HOST", "https://auth.converge.ai").rstrip("/")
APP_HOST = _env("ENTER_APP_HOST", "https://enter.converge.ai").rstrip("/")
API_HOST = _env("ENTER_API_HOST", "https://api.enter.pro").rstrip("/")
CLIENT_ID = _env("ENTER_CLIENT_ID", "anCisSaaIA36fTZ2DUMiTMro3bYuptrf")
AUDIENCE = _env("ENTER_AUDIENCE", "https://api.enter.pro")
SCOPE = _env("ENTER_SCOPE", "openid profile email offline_access")
REDIRECT_URI = _env("ENTER_REDIRECT_URI", APP_HOST)
TOKEN_URL = f"{AUTH_HOST}/oauth/token"
AUTHORIZE_URL = f"{AUTH_HOST}/authorize"

BATCH_ID = ""
BATCH_DIR: Path = RESULTS_ROOT
RESULTS_JSON: Path = RESULTS_ROOT / "accounts.json"
RESULTS_TXT: Path = RESULTS_ROOT / "accounts.txt"
FAILED_JSON: Path = RESULTS_ROOT / "failed.json"
# Per-batch + global credential dumps (only successful accounts with api key)
CREDS_TXT: Path = RESULTS_ROOT / "credentials.txt"
CREDS_KEYS_TXT: Path = RESULTS_ROOT / "apikeys.txt"
# Global append-only (all batches, never wiped)
GLOBAL_CREDS_TXT = Path(_env("ENTER_GLOBAL_CREDS", str(RESULTS_ROOT / "all_credentials.txt")))
GLOBAL_KEYS_TXT = Path(_env("ENTER_GLOBAL_KEYS", str(RESULTS_ROOT / "all_apikeys.txt")))
_results_lock = asyncio.Lock()
_emails_lock = asyncio.Lock()
_used_emails: set[str] = set()
_proxy_lock = asyncio.Lock()
_proxy_pool: list[tuple[str, str]] = []
_proxy_idx = 0
_turnstile_sem: asyncio.Semaphore | None = None
_claimed_otps_sync: set[str] = set()
_claimed_otps_lock = threading.Lock()
_rate_limit_until = 0.0
_rate_limit_lock = asyncio.Lock()
# tempmail (mail.tm): address -> {password, token}
_tempmail_accounts: dict[str, dict[str, str]] = {}
_tempmail_lock = threading.Lock()
_rotating_mail_accounts: dict[str, tuple[str, str]] = {}
_rotating_mail_lock = threading.Lock()
_rotating_mail_idx = 0
# gptmail (mail.chatgpt.org.uk): address -> {token, sid, expires_at}
_gptmail_accounts: dict[str, dict[str, str]] = {}
_gptmail_lock = threading.Lock()
_gptmail_session_cookies: dict[str, str] = {}  # cookie jar for verified gptmail session
_gptmail_session_lock = threading.Lock()
_gptmail_domains_cache: list[str] = []
_gptmail_domains_ts = 0.0
# sticky domain per worker slot: slot -> domain; blocklist when Auth0 rejects domain
_gptmail_slot_domain: dict[int, str] = {}
_gptmail_blocked_domains: set[str] = set()
_gptmail_domain_rr = 0  # round-robin for fresh domain picks
# Per-domain consecutive fail counter: domain -> fail_count (resets on success)
_domain_fail_count: dict[str, int] = {}
_domain_fail_lock = threading.Lock()
DOMAIN_AUTO_BLOCK_THRESHOLD = 5  # auto-blacklist after N consecutive fails



def _ensure_async_gates() -> tuple[asyncio.Lock, asyncio.Event]:
    """Lazy-init asyncio primitives (need running loop)."""
    global _in_flight_lock, _can_start
    if _in_flight_lock is None:
        _in_flight_lock = asyncio.Lock()
    if _can_start is None:
        _can_start = asyncio.Event()
        _can_start.set()
    return _in_flight_lock, _can_start


def _effective_warp_every_n() -> int:
    """Wave mode: if everyN enabled, always 1:1 with live CONCURRENT."""
    if WARP_EVERY_N <= 0:
        return 0
    return max(1, int(CONCURRENT))


def _vpnx_wave_due(completed: int, every_n: int) -> bool:
    return every_n > 0 and completed > 0 and completed % every_n == 0


def _vpnx_rotate_sync() -> dict:
    query = urlencode({"country": VPNX_COUNTRY})
    req = urllib.request.Request(
        f"{VPNX_API}/rotate?{query}",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {VPNX_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


async def _vpnx_rotate_wave(attempt: int) -> None:
    result = await asyncio.to_thread(_vpnx_rotate_sync)
    if result.get("status") != "connected":
        raise RuntimeError(f"VPNX rotate failed: {result}")
    alog(attempt, f"VPNX rotate: server={result.get('server')} country={result.get('country')}")
    alog(attempt, f"VPNX settle {VPNX_SETTLE_S:.0f}s…")
    await asyncio.sleep(VPNX_SETTLE_S)


def _hub_root() -> Path | None:
    try:
        p = Path(__file__).resolve().parent
        if p.parent.name == "farms":
            return p.parent.parent
    except Exception:
        pass
    return _HUB if "_HUB" in globals() else None


def _warp_rotate_sync(attempt: int = 0) -> bool:
    """Call hub core.warp (preferred). No local warp-cli."""
    hub = _hub_root()
    if hub is None:
        alog(attempt, "WARP: hub root not found — skip")
        return False
    hub_s = str(hub)
    if hub_s not in sys.path:
        sys.path.insert(0, hub_s)
    try:
        from core.warp import WarpClient  # type: ignore

        def _log(m: str) -> None:
            alog(attempt, m)

        w = WarpClient(log=_log)
        if not w.ensure_connected():
            _log("WARP: not connected")
        r = w.rotate_ip(force=True)
        _log(f"WARP rotate: {r}")
        return bool(getattr(r, "ok", False))
    except Exception as e:
        alog(attempt, f"WARP error: {type(e).__name__}: {e}")
        return False


def warp_rotate_ip(attempt: int = 0) -> bool:
    """Hub rotate (sync). Used by rate-limit / goto recovery."""
    return _warp_rotate_sync(attempt)


async def warp_rotate_ip_async(attempt: int = 0) -> bool:
    """Async wrapper; one rotate at a time via thread lock."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _warp_rotate_sync(attempt))


async def _maybe_warp_after_success(attempt: int) -> None:
    """Proactive rotate every WARP_EVERY_N successes (1:1 with concurrent).

    Wave mode: block new starts, drain peers, rotate once, settle, resume.
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
            alog(
                attempt,
                f"WARP every_n: success {n}/{every} (wave c={CONCURRENT})",
            )
            return
        if _warp_drain_owner is not None:
            alog(
                attempt,
                f"WARP every_n: success {n}/{every} "
                f"(drain owned by #{_warp_drain_owner})",
            )
            return
        _warp_drain_owner = attempt
        _success_since_warp = 0
        should_rotate = True
        alog(
            attempt,
            f"WARP every_n: wave complete {every}/{every} → drain then rotate…",
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
                alog(
                    attempt,
                    f"WARP every_n: drain timeout (in_flight={n_if}) — rotate anyway",
                )
                break
            now = time.time()
            if now - last_log >= 5.0:
                alog(attempt, f"WARP every_n: waiting peers (in_flight={n_if})…")
                last_log = now
            await asyncio.sleep(0.5)

        alog(attempt, "WARP every_n: drain ok → rotate IP…")
        await warp_rotate_ip_async(attempt)
        alog(attempt, f"WARP every_n: settle {WARP_SETTLE_S:.0f}s…")
        await asyncio.sleep(WARP_SETTLE_S)
    finally:
        with _warp_rotate_lock:
            _warp_drain_owner = None
        can_start.set()
        alog(attempt, "WARP every_n: resume workers")




async def _wait_rate_limit_window(attempt: int) -> None:
    """Block workers while global Auth0 signup cooldown is active."""
    global _rate_limit_until
    while True:
        now = time.time()
        wait = _rate_limit_until - now
        if wait <= 0:
            return
        alog(attempt, f"rate-limit cooldown {wait:.0f}s left...")
        await asyncio.sleep(min(wait, 15.0))


async def _trip_rate_limit(attempt: int, reason: str) -> None:
    global _rate_limit_until
    alog(attempt, f"RATE LIMIT: {reason}")

    # Prefer hub WARP reconnect over long idle cooldown
    rotated = False
    if WARP_ON_RATE_LIMIT:
        try:
            rotated = await warp_rotate_ip_async(attempt)
        except Exception as e:
            alog(attempt, f"WARP rotate error: {e}")

    cooldown = RATE_LIMIT_COOLDOWN
    if rotated and WARP_SKIP_LONG_COOLDOWN:
        # Short settle only — IP should be fresh
        cooldown = min(cooldown, max(10.0, WARP_COOLDOWN_AFTER + 5.0))
        alog(attempt, f"WARP rotated - short cooldown {cooldown:.0f}s (set ENTER_WARP_SKIP_LONG_COOLDOWN=false for full {RATE_LIMIT_COOLDOWN}s)")

    async with _rate_limit_lock:
        until = time.time() + cooldown
        if until > _rate_limit_until:
            _rate_limit_until = until
        left = max(0.0, _rate_limit_until - time.time())
        alog(attempt, f"global cooldown {left:.0f}s (ENTER_RATE_LIMIT_COOLDOWN={RATE_LIMIT_COOLDOWN}, warp={rotated})")


async def _page_error_text(page) -> str:
    """Body + alert/error nodes (shared by rate-limit + domain-block detectors)."""
    try:
        txt = await page.evaluate(
            """() => {
                const body = (document.body && (document.body.innerText || document.body.textContent) || '');
                const bits = [body];
                for (const sel of ['[role="alert"]', '[class*="error"]', '[class*="Error"]',
                                   '[data-error]', '.ulp-error-info', '#error-element-password',
                                   '#prompt-alert', '.error-message', '[class*="Banner"]']) {
                  document.querySelectorAll(sel).forEach(el => bits.push(el.innerText || ''));
                }
                return bits.join('\\n').slice(0, 6000);
            }"""
        )
    except Exception:
        txt = ""
    return txt or ""


async def page_has_domain_block(page) -> str | None:
    """Detect Auth0 banner: 'This email domain is not allowed to sign up'."""
    t = (await _page_error_text(page)).lower()
    needles = (
        "email domain is not allowed",
        "domain is not allowed to sign up",
        "domain is not allowed",
        "email domain not allowed",
        "not allowed to sign up",
        "disposable email",
        "email provider is not allowed",
        "use a different email domain",
    )
    for n in needles:
        if n in t:
            return n
    # domain + not allowed / blocked
    if "domain" in t and ("not allowed" in t or "not permitted" in t or "blocked" in t):
        if "email" in t or "sign up" in t or "signup" in t:
            return "domain + not allowed"
    return None


async def page_has_rate_limit(page) -> str | None:
    """Detect Auth0 / Enter 'Too many signup attempts' (and similar) banners.

    Exact banner from production screenshots:
      "Too many signup attempts. Please try again later"
    """
    t = (await _page_error_text(page)).lower()

    # Strong matches first (exact product copy)
    strong = (
        "too many signup attempts",
        "too many sign-up attempts",
        "too many sign up attempts",
        "too many login attempts",
        "too many attempts. please try again",
        "we have detected unusual activity",
        "blocked due to suspicious",
        "rate limit exceeded",
        "too many requests",
    )
    for n in strong:
        if n in t:
            return n

    # Combined signals: "too many" + attempt/signup + later/wait
    if "too many" in t and ("attempt" in t or "signup" in t or "sign-up" in t or "sign up" in t):
        return "too many + attempt/signup"

    if "try again later" in t and ("too many" in t or "attempt" in t or "signup" in t):
        return "try again later (rate)"

    u = (page.url or "").lower()
    if "too_many" in u or "rate_limit" in u:
        return "url:" + u[:80]
    return None


async def raise_if_rate_limited(page, attempt: int, where: str) -> None:
    """If rate-limit banner visible: screenshot, trip global cooldown, raise fast."""
    reason = await page_has_rate_limit(page)
    if not reason:
        return
    await screenshot(page, attempt, f"rate_limit_{where}")
    await _trip_rate_limit(attempt, f"{where}: {reason}")
    raise RuntimeError(f"Too many signup attempts ({where}): {reason}")


# ── Risk session (bypass Auth0 risk_control_blocked) ─────────────────────────
def _get_risk_session_id() -> str | None:
    """Get risk_session_id from Enter API. Accepts random FPJS data."""
    vid = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    eid = f"{int(time.time() * 1000)}.{''.join(secrets.choice(string.ascii_letters) for _ in range(6))}"
    data = json.dumps({"fp_event_id": eid, "visitor_id": vid, "platform": "web"}).encode()
    req = urllib.request.Request(
        f"{API_HOST}/code/api/v1/auth/risk-session",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Origin": APP_HOST,
            "Referer": f"{APP_HOST}/",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())["data"]["risk_session_id"]
    except Exception:
        return None


# ── BYCF Turnstile solver (pure HTTP, no browser) ───────────────────────────
BYCF_URL = _env("ENTER_BYCF_URL", "https://shannz.zone.id/api")
BYCF_SECRET = _env("ENTER_BYCF_SECRET", "shannz-secret-key-123")
AUTH0_TURNSTILE_SITEKEY = "0x4AAAAAACwSuI5jPtwnNwc5"
# Browser gateway is the only supported auth mode; legacy HTTP helpers are inert.
AUTH_MODE = _env("ENTER_AUTH_MODE", "browser").lower()


def _solve_turnstile_bycf(url: str, sitekey: str) -> str:
    """Solve Cloudflare Turnstile via bycf remote service. Returns token string."""
    body = json.dumps({"url": url, "siteKey": sitekey}).encode()
    req = urllib.request.Request(
        f"{BYCF_URL}/solve-turnstile-min",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-bycf-version": "1.0.5",
            "x-bycf-secret": BYCF_SECRET,
        },
    )
    resp = urllib.request.urlopen(req, timeout=60)
    j = json.loads(resp.read())
    if not j.get("success") or not j.get("data"):
        raise RuntimeError(f"bycf turnstile failed: {j.get('error', j)}")
    return j["data"]


def _http_auth0_post_form(url: str, form_data: dict, cookies: str = "") -> tuple:
    """POST form to Auth0 (no-follow redirect). Returns (status, location, body)."""
    import http.client
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://converge-ai.us.auth0.com",
    }
    if cookies:
        headers["Cookie"] = cookies
    parsed = urlparse(url)
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=30)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    conn.request("POST", path, body=urlencode(form_data).encode(), headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    loc = dict(resp.getheaders()).get("Location", dict(resp.getheaders()).get("location", ""))
    conn.close()
    return resp.status, loc, body


def _http_auth0_get(url: str, cookies: str = "") -> tuple:
    """GET from Auth0 (no-follow). Returns (status, location, body)."""
    import http.client
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    if cookies:
        headers["Cookie"] = cookies
    parsed = urlparse(url)
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=30)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    hdrs = dict(resp.getheaders())
    loc = hdrs.get("Location", hdrs.get("location", ""))
    # Parse set-cookie
    raw_cookies = [v for k, v in resp.getheaders() if k.lower() == "set-cookie"]
    conn.close()
    return resp.status, loc, body, raw_cookies


def do_signup_http(email_addr: str, password: str, attempt: int, otp_func) -> dict:
    """Pure HTTP Auth0 signup. otp_func(email, timeout) returns OTP code string.

    Returns same dict as exchange_code_for_tokens (access_token, refresh_token, etc).
    """
    alog(attempt, "http-auth: start")

    # 1. Solve Turnstile
    alog(attempt, "http-auth: solving turnstile (bycf)...")
    ts_token = _solve_turnstile_bycf(
        "https://converge-ai.us.auth0.com/u/signup/identifier",
        AUTH0_TURNSTILE_SITEKEY,
    )
    alog(attempt, f"http-auth: turnstile ok (len={len(ts_token)})")

    # 2. risk_session_id
    rs_id = _get_risk_session_id()
    alog(attempt, f"http-auth: risk_session={'ok' if rs_id else 'fail'}")

    # 3. /authorize -> get state + cookies
    import hashlib as _hl
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        _hl.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    our_state = secrets.token_urlsafe(16)

    auth_params = urlencode({
        "client_id": CLIENT_ID, "scope": SCOPE, "audience": AUDIENCE,
        "redirect_uri": REDIRECT_URI, "response_type": "code", "response_mode": "query",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": our_state,
        "auth0Client": "eyJuYW1lIjoiYXV0aDAtcmVhY3QiLCJ2ZXJzaW9uIjoiMi4xMC4wIn0=",
        "risk_session_id": rs_id or "", "screen_hint": "signup",
    })
    s3, loc3, _, raw_ck = _http_auth0_get(f"{AUTHORIZE_URL}?{auth_params}")
    cookie_str = "; ".join(c.split(";")[0] for c in raw_ck if "=" in c.split(";")[0])
    alog(attempt, f"http-auth: authorize {s3} -> {loc3[:50]}")

    if not loc3:
        raise RuntimeError(f"http-auth: no redirect from /authorize ({s3})")

    # Follow to signup page
    signup_url = f"https://auth.converge.ai{loc3}" if loc3.startswith("/") else loc3
    _, _, body_su, _ = _http_auth0_get(signup_url, cookie_str)
    m = re.search(r'name="state"\s+value="([^"]+)"', body_su)
    auth0_state = m.group(1) if m else ""
    if not auth0_state:
        raise RuntimeError("http-auth: no state on signup page")

    # 4. POST identifier
    alog(attempt, "http-auth: POST identifier...")
    form = {
        "state": auth0_state, "email": email_addr, "captcha": ts_token,
        "js-available": "true", "webauthn-available": "true",
        "is-brave": "false", "webauthn-platform-available": "true", "action": "default",
    }
    s4, loc4, body4 = _http_auth0_post_form(
        f"https://converge-ai.us.auth0.com/u/signup/identifier?state={auth0_state}",
        form, cookie_str,
    )
    alog(attempt, f"http-auth: identifier {s4} -> {loc4[:60]}")

    if "challenge" not in loc4 and s4 != 302:
        err = re.search(r'data-error-code="([^"]+)"', body4)
        err_code = err.group(1) if err else "unknown"
        raise RuntimeError(f"http-auth: identifier rejected ({err_code})")

    # 5. Get email challenge page
    ch_url = f"https://converge-ai.us.auth0.com{loc4}" if loc4.startswith("/") else loc4
    _, _, body_ch, _ = _http_auth0_get(ch_url, cookie_str)
    ch_m = re.search(r'name="state"\s+value="([^"]+)"', body_ch)
    ch_state = ch_m.group(1) if ch_m else auth0_state

    # 6. Wait for OTP via callback
    alog(attempt, "http-auth: waiting for OTP...")
    otp = otp_func(email_addr, OTP_TIMEOUT_S)
    if not otp:
        raise RuntimeError("http-auth: OTP timeout")
    alog(attempt, f"http-auth: OTP={otp}")

    # 7. Submit OTP
    s7, loc7, body7 = _http_auth0_post_form(
        f"https://converge-ai.us.auth0.com/u/email-identifier/challenge?state={ch_state}",
        {"state": ch_state, "code": otp, "action": "default"},
        cookie_str,
    )
    alog(attempt, f"http-auth: OTP submit {s7} -> {loc7[:60]}")
    if "risk_control_blocked" in body7 or "access_denied" in body7:
        raise RuntimeError("http-auth: risk_control_blocked at OTP step")
    if "password" not in loc7 and 'name="password"' not in body7:
        err = re.search(r'data-error-code="([^"]+)"', body7)
        raise RuntimeError(f"http-auth: OTP rejected ({err.group(1) if err else 'no password step'})")

    # 8. Submit password
    pass_state = ch_state
    if loc7 and "password" in loc7:
        pu = f"https://converge-ai.us.auth0.com{loc7}" if loc7.startswith("/") else loc7
        _, _, pb, _ = _http_auth0_get(pu, cookie_str)
        pm = re.search(r'name="state"\s+value="([^"]+)"', pb)
        if pm:
            pass_state = pm.group(1)

    alog(attempt, "http-auth: POST password...")
    s8, loc8, body8 = _http_auth0_post_form(
        f"https://converge-ai.us.auth0.com/u/signup/password?state={pass_state}",
        {"state": pass_state, "password": password, "action": "default"},
        cookie_str,
    )
    alog(attempt, f"http-auth: password {s8} -> {loc8[:80]}")
    if "risk_control_blocked" in body8 or "access_denied" in body8:
        raise RuntimeError("http-auth: risk_control_blocked at password step")

    # 9. Extract OAuth code
    code = ""
    if "code=" in loc8:
        code = parse_qs(urlparse(loc8).query).get("code", [""])[0]
    elif loc8:
        fu = f"https://converge-ai.us.auth0.com{loc8}" if loc8.startswith("/") else loc8
        _, locf, _, _ = _http_auth0_get(fu, cookie_str)
        if "code=" in locf:
            code = parse_qs(urlparse(locf).query).get("code", [""])[0]

    if not code:
        raise RuntimeError(f"http-auth: no OAuth code in final redirect")

    # 10. Exchange code for tokens
    alog(attempt, "http-auth: exchanging code...")
    tokens = exchange_code_for_tokens(code, verifier)
    alog(attempt, f"http-auth: SUCCESS (expires_in={tokens.get('expires_in')})")
    return tokens


# ── Proxy helpers (grok-farm compatible) ─────────────────────────────────────
def _normalize_proxy_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    # host:port:user:pass
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    if "@" in raw:
        return f"http://{raw}"
    return f"http://{raw}"


def _parse_proxy_entry(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    opt_id = ""
    if "#" in line and "://" in line.split("#", 1)[0]:
        line, opt_id = line.rsplit("#", 1)
        line, opt_id = line.strip(), opt_id.strip()
    url = _normalize_proxy_url(line)
    if not url:
        return None
    return url, opt_id or ""


def _load_proxy_file(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        ent = _parse_proxy_entry(line)
        if ent:
            out.append(ent)
    return out


def _load_proxy_pool() -> list[tuple[str, str]]:
    pool: list[tuple[str, str]] = []
    file_path = _env("ENTER_PROXY_FILE")
    if file_path:
        pool.extend(_load_proxy_file(Path(file_path)))
    else:
        default = _ROOT / "proxies.txt"
        if default.is_file():
            pool.extend(_load_proxy_file(default))
    extra = _env("ENTER_PROXY_POOL")
    if extra:
        for part in extra.split(","):
            ent = _parse_proxy_entry(part.strip())
            if ent:
                pool.append(ent)
    if _env_bool("ENTER_PROXY_SHUFFLE", False):
        random.shuffle(pool)
    return pool


def _parse_proxy(proxy_url: str) -> dict[str, str]:
    u = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    conf: dict[str, str] = {
        "server": f"{u.scheme}://{u.hostname}:{u.port or (1080 if u.scheme.startswith('socks') else 8080)}"
    }
    if u.username:
        conf["username"] = u.username
    if u.password:
        conf["password"] = u.password
    return conf


async def next_proxy() -> tuple[str | None, str]:
    global _proxy_idx
    async with _proxy_lock:
        if not _proxy_pool:
            return None, ""
        url, pid = _proxy_pool[_proxy_idx % len(_proxy_pool)]
        _proxy_idx += 1
        return url, pid


def _get_turnstile_sem() -> asyncio.Semaphore:
    global _turnstile_sem
    if _turnstile_sem is None:
        _turnstile_sem = asyncio.Semaphore(TURNSTILE_PARALLEL)
    return _turnstile_sem


# ── Email / batch ────────────────────────────────────────────────────────────
def _crypto_local_part(n: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _load_used_emails() -> None:
    _used_emails.clear()
    if USED_EMAILS_FILE.is_file():
        for line in USED_EMAILS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            e = line.strip().lower()
            if e and "@" in e:
                _used_emails.add(e)
    if RESULTS_ROOT.is_dir():
        for p in RESULTS_ROOT.glob("batch_*/accounts.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for row in data if isinstance(data, list) else []:
                    em = (row.get("email") or "").lower()
                    if em:
                        _used_emails.add(em)
            except Exception:
                pass


def _persist_used_email(email: str) -> None:
    USED_EMAILS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USED_EMAILS_FILE.open("a", encoding="utf-8") as f:
        f.write(email.lower() + "\n")


def init_batch(max_accounts: int, concurrent: int) -> str:
    global BATCH_ID, BATCH_DIR, RESULTS_JSON, RESULTS_TXT, FAILED_JSON, CREDS_TXT, CREDS_KEYS_TXT
    _load_used_emails()
    global _proxy_pool
    _proxy_pool = _load_proxy_pool()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BATCH_ID = _env("ENTER_BATCH_ID") or f"batch_{stamp}_{secrets.token_hex(3)}"
    BATCH_DIR = RESULTS_ROOT / BATCH_ID
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON = BATCH_DIR / "accounts.json"
    RESULTS_TXT = BATCH_DIR / "accounts.txt"
    FAILED_JSON = BATCH_DIR / "failed.json"
    CREDS_TXT = BATCH_DIR / "credentials.txt"
    CREDS_KEYS_TXT = BATCH_DIR / "apikeys.txt"
    for p, empty in (
        (RESULTS_JSON, "[]"),
        (FAILED_JSON, "[]"),
        (RESULTS_TXT, ""),
        (CREDS_TXT, ""),
        (CREDS_KEYS_TXT, ""),
    ):
        if not p.exists():
            p.write_text(empty + ("\n" if empty else ""), encoding="utf-8")
    # global credential files (header once)
    GLOBAL_CREDS_TXT.parent.mkdir(parents=True, exist_ok=True)
    if not GLOBAL_CREDS_TXT.exists():
        GLOBAL_CREDS_TXT.write_text(
            "# email|password|api_key|workspace_id|key_id|key_name|created_at|batch_id\n",
            encoding="utf-8",
        )
    if not GLOBAL_KEYS_TXT.exists():
        GLOBAL_KEYS_TXT.write_text(
            "# one api key per line (successful farms only)\n",
            encoding="utf-8",
        )
    meta = {
        "batch_id": BATCH_ID,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "product": "enter.converge.ai",
        "gift_code": GIFT_CODE,
        "email_mode": EMAIL_MODE,
        "email_domain": (
            EMAIL_DOMAIN
            if EMAIL_MODE == "domain"
            else (GPTMAIL_DOMAIN or GPTMAIL_API if EMAIL_MODE == "gptmail" else None)
        ),
        "max_accounts": max_accounts,
        "concurrent": concurrent,
        "proxies": len(_proxy_pool),
    }
    (BATCH_DIR / "batch_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    HUD.open_log(BATCH_DIR / "farm.log")
    slog("BATCH", f"id={BATCH_ID}")
    slog("BATCH", f"dir={BATCH_DIR}")
    slog("BATCH", f"proxies={len(_proxy_pool)} gift={GIFT_CODE} target={max_accounts} concurrent={concurrent}")
    return BATCH_ID


def _gptmail_solve_turnstile() -> bool:
    """Visit gptmail via Camoufox, click Random to trigger Turnstile solve, extract session cookies.
    Call once at farm startup. Returns True if session verified."""
    import asyncio as _aio

    async def _solve():
        from camoufox.async_api import AsyncCamoufox

        proxy_server = None
        if _proxy_pool:
            proxy_server = {"server": _proxy_pool[0][0].replace("socks5://", "socks5://")}
        kwargs = {"headless": True, "geoip": True}
        if proxy_server:
            kwargs["proxy"] = proxy_server
        async with AsyncCamoufox(**kwargs) as browser:
            page = await browser.new_page()
            await page.goto("https://mail.chatgpt.org.uk/", wait_until="networkidle", timeout=30000)
            await _aio.sleep(3)
            # Click Random — triggers inbox-token → 428 → app solves Turnstile → verifies
            btn = page.locator("button:has-text('Random')")
            if await btn.count() > 0:
                await btn.click()
                # Wait for URL change (app redirects to /email@domain after success)
                for _ in range(30):
                    await _aio.sleep(1)
                    if "@" in page.url:
                        break
            # Extract cookies
            cookies = await page.context.cookies()
            cookie_dict = {}
            for ck in cookies:
                if "chatgpt.org.uk" in ck.get("domain", ""):
                    cookie_dict[ck["name"]] = ck["value"]
            with _gptmail_session_lock:
                _gptmail_session_cookies.update(cookie_dict)
            return "@" in page.url  # success if page has inbox email in URL

    try:
        loop = _aio.new_event_loop()
        ok = loop.run_until_complete(_solve())
        loop.close()
        if ok:
            print("[GPTMAIL] Turnstile session verified via browser", flush=True)
        else:
            print("[GPTMAIL] Turnstile solve: page did not redirect (may still work)", flush=True)
        return ok
    except Exception as e:
        print(f"[GPTMAIL] Turnstile solve failed: {e}", flush=True)
        return False


def _http_json(url: str, data: dict | None = None, headers: dict | None = None, method: str | None = None) -> dict | list:
    """JSON HTTP helper — uses verified gptmail session cookies + proxy."""
    import requests as _req

    _BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    )
    h = {
        "Accept": "application/json",
        "User-Agent": _BROWSER_UA,
    }
    if headers:
        h.update(headers)
    proxies = None
    cookies = None
    if "chatgpt.org.uk" in url:
        # Use proxy
        if _proxy_pool:
            p = _proxy_pool[_proxy_idx % len(_proxy_pool)][0].replace("socks5://", "socks5h://")
            proxies = {"http": p, "https": p}
        # Attach session cookies from Turnstile verification
        with _gptmail_session_lock:
            if _gptmail_session_cookies:
                cookies = dict(_gptmail_session_cookies)
    m = method or ("POST" if data is not None else "GET")
    resp = _req.request(m, url, json=data if data is not None else None,
                        headers=h, proxies=proxies, cookies=cookies, timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def _tempmail_pick_domain() -> str:
    """GET /domains from mail.tm — returns first active public domain."""
    data = _http_json(f"{TEMPMAIL_API}/domains")
    members = data if isinstance(data, list) else (data.get("hydra:member") or data.get("member") or [])
    if not members:
        raise RuntimeError("tempmail: no domains from API")
    for d in members:
        if isinstance(d, dict) and d.get("isActive", True) and not d.get("isPrivate", False):
            dom = d.get("domain") or ""
            if dom:
                return dom
    # fallback first
    d0 = members[0]
    dom = d0.get("domain") if isinstance(d0, dict) else str(d0)
    if not dom:
        raise RuntimeError(f"tempmail: bad domains payload {str(data)[:200]}")
    return dom


def create_tempmail_account() -> str:
    """Create mail.tm inbox; store password+token for OTP polling. Returns address."""
    domain = _tempmail_pick_domain()
    for _ in range(8):
        local = "ent" + _crypto_local_part(12)
        addr = f"{local}@{domain}".lower()
        pw = "Tmp_" + secrets.token_hex(6)
        try:
            _http_json(
                f"{TEMPMAIL_API}/accounts",
                {"address": addr, "password": pw},
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            # address taken / rate limit — retry new local
            if e.code in (409, 422, 429):
                time.sleep(0.4)
                continue
            raise RuntimeError(f"tempmail create account HTTP {e.code}: {body[:200]}") from e
        try:
            tok = _http_json(
                f"{TEMPMAIL_API}/token",
                {"address": addr, "password": pw},
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"tempmail token HTTP {e.code}: {body[:200]}") from e
        token = (tok or {}).get("token") if isinstance(tok, dict) else None
        if not token:
            raise RuntimeError(f"tempmail: no token in {tok}")
        with _tempmail_lock:
            _tempmail_accounts[addr] = {"password": pw, "token": token}
        print(f"[TEMPMAIL] created {addr} domain={domain}", flush=True)
        return addr
    raise RuntimeError("tempmail: could not create account after retries")


def read_otp_from_tempmail_sync(target_email: str, timeout: int = 180, since_ts: float | None = None) -> str | None:
    """Poll mail.tm messages for Auth0/Converge 6-digit code."""
    addr = target_email.lower()
    with _tempmail_lock:
        cred = dict(_tempmail_accounts.get(addr) or {})
    if not cred.get("token"):
        print(f"[TEMPMAIL] no session for {addr}", flush=True)
        return None
    token = cred["token"]
    print(f"[TEMPMAIL] Waiting OTP -> {addr} (timeout={timeout}s)...", flush=True)
    start = time.time()
    since_ts = since_ts or (start - 30)
    seen_ids: set[str] = set()
    polls = 0
    while time.time() - start < timeout:
        polls += 1
        elapsed = int(time.time() - start)
        try:
            # refresh token occasionally if 401
            msgs = _http_json(
                f"{TEMPMAIL_API}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            # mail.tm may return list or hydra collection
            if isinstance(msgs, list):
                items = msgs
            elif isinstance(msgs, dict):
                items = msgs.get("hydra:member") or msgs.get("member") or msgs.get("items") or []
            else:
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                # fetch full message for body
                full = item
                if mid:
                    try:
                        full = _http_json(
                            f"{TEMPMAIL_API}/messages/{mid}",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    except Exception:
                        full = item
                subject = str(full.get("subject") or item.get("subject") or "")
                # text / html / intro
                text = (
                    full.get("text")
                    or full.get("textBody")
                    or full.get("intro")
                    or ""
                )
                html = full.get("html") or full.get("htmlBody") or ""
                if isinstance(html, list):
                    html = "\n".join(str(x) for x in html)
                body = str(text) + "\n" + _strip_html(str(html))
                fr = ""
                frm = full.get("from") or item.get("from") or {}
                if isinstance(frm, dict):
                    fr = str(frm.get("address") or frm.get("name") or "")
                else:
                    fr = str(frm)
                # optional created date filter
                created = full.get("createdAt") or item.get("createdAt") or ""
                # extract OTP
                code = _extract_otp(subject, body)
                if not code:
                    m = re.search(r"your code is\s*:?\s*(\d{6})", body, re.I)
                    code = m.group(1) if m else None
                if not code:
                    continue
                # prefer auth0/converge-ish
                if not _is_auth0ish(subject, fr, body) and "verify" not in subject.lower():
                    # still accept 6-digit if subject verify-ish missing but code present
                    if "code" not in body.lower() and "verify" not in body.lower():
                        continue
                with _claimed_otps_lock:
                    if code in _claimed_otps_sync:
                        continue
                    _claimed_otps_sync.add(code)
                print(
                    f"[TEMPMAIL] OTP found: {code} for {addr} "
                    f"(subj={subject[:60]!r} from={fr[:40]!r} t+{elapsed}s)",
                    flush=True,
                )
                return code
            if polls == 1 or polls % 4 == 0:
                print(
                    f"[TEMPMAIL] still waiting… {elapsed}s/{timeout}s msgs={len(items)}",
                    flush=True,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[TEMPMAIL] HTTP {e.code}: {body[:120]}", flush=True)
            if e.code == 401:
                # re-token
                try:
                    tok = _http_json(
                        f"{TEMPMAIL_API}/token",
                        {"address": addr, "password": cred.get("password") or ""},
                    )
                    token = (tok or {}).get("token") or token
                    with _tempmail_lock:
                        if addr in _tempmail_accounts:
                            _tempmail_accounts[addr]["token"] = token
                except Exception as e2:
                    print(f"[TEMPMAIL] retoken fail: {e2}", flush=True)
        except Exception as e:
            print(f"[TEMPMAIL] poll error: {e}", flush=True)
        time.sleep(3)
    print(f"[TEMPMAIL] Timeout after {timeout}s for {addr}", flush=True)
    return None


# ── GPTMail (mail.chatgpt.org.uk) — HAR HTTPToolkit_2026-07-17 ───────────────
def _gptmail_headers(token: str = "") -> dict[str, str]:
    h = {
        "Accept": "application/json",
        "Origin": GPTMAIL_API,
        "Referer": f"{GPTMAIL_API}/",
    }
    if token:
        h["x-inbox-token"] = token
        # JWT payload is first segment (base64url): {"sid":"...","email":"...","exp":...}
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
        # only public+active when fields present
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
    """Load permanent Auth0-rejected domains from disk into memory."""
    global _gptmail_blocked_domains
    if not BLOCKED_DOMAINS_FILE.is_file():
        return
    try:
        loaded: set[str] = set()
        for line in BLOCKED_DOMAINS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            dom = line.split("#", 1)[0].strip().lower().lstrip("@")
            if dom and "." in dom and "@" not in dom:
                loaded.add(dom)
        if loaded:
            with _gptmail_lock:
                _gptmail_blocked_domains |= loaded
            print(
                f"[GPTMAIL] loaded {len(loaded)} blocked domain(s) from {BLOCKED_DOMAINS_FILE.name}",
                flush=True,
            )
    except Exception as e:
        print(f"[GPTMAIL] could not read blocked list: {e}", flush=True)


def _persist_blocked_domain(domain: str, reason: str = "") -> None:
    """Append domain to permanent blocklist (never re-enter pool)."""
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom:
        return
    try:
        BLOCKED_DOMAINS_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    raise RuntimeError(
        f"gptmail: no usable domains left "
        f"(pool={len(pool)} blocked={len(blocked)} file={BLOCKED_DOMAINS_FILE.name})"
    )


def _gptmail_pick_domain(worker_slot: int | None = None) -> str:
    """Sticky domain per worker slot; only changes when domain is blocked.

    worker_slot = concurrent slot index (0..c-1). Concurrent itself is NEVER capped —
    user sets -c freely; we just pin one domain per active slot.
    """
    if GPTMAIL_DOMAIN:
        return GPTMAIL_DOMAIN
    pool = _gptmail_pool()
    if worker_slot is None:
        return secrets.choice(pool)

    with _gptmail_lock:
        global _gptmail_domain_rr
        cur = _gptmail_slot_domain.get(worker_slot)
        if cur and cur not in _gptmail_blocked_domains and cur in pool:
            return cur
        # assign next free domain not already sticky on another slot
        used = {d for s, d in _gptmail_slot_domain.items() if s != worker_slot}
        candidates = [d for d in pool if d not in used] or list(pool)
        # round-robin for stable spread across slots
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
    """Mark domain blocked (Auth0 'not allowed to sign up'); next claim rotates."""
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


def _domain_fail_track(email_addr: str, reason: str, worker_slot: int | None = None) -> None:
    """Track consecutive fails per domain; auto-blacklist after threshold."""
    dom = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    if not dom:
        return
    with _domain_fail_lock:
        _domain_fail_count[dom] = _domain_fail_count.get(dom, 0) + 1
        count = _domain_fail_count[dom]
    if count >= DOMAIN_AUTO_BLOCK_THRESHOLD:
        gptmail_block_domain(dom, reason=f"auto: {count} consecutive fails", worker_slot=worker_slot)


def _domain_fail_reset(email_addr: str) -> None:
    """Reset fail counter on success."""
    dom = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    if not dom:
        return
    with _domain_fail_lock:
        _domain_fail_count.pop(dom, None)


def _gptmail_make_local() -> str:
    """Custom prefix is client-side only (no dedicated API) — random local-part."""
    if GPTMAIL_PREFIX:
        # fixed prefix + short random suffix so addresses stay unique
        suffix = _crypto_local_part(max(4, min(10, EMAIL_LOCAL_LEN // 2)))
        base = re.sub(r"[^a-z0-9._-]", "", GPTMAIL_PREFIX)[:24] or "ent"
        return f"{base}{suffix}"
    # HAR-style: letter-ish name + digits (pwebster317)
    letters = "".join(secrets.choice(string.ascii_lowercase) for _ in range(secrets.randbelow(5) + 6))
    digits = "".join(secrets.choice(string.digits) for _ in range(secrets.randbelow(3) + 2))
    return f"{letters}{digits}"


def create_gptmail_inbox(worker_slot: int | None = None) -> str:
    """Claim GPTMail inbox on sticky domain for worker_slot; new prefix each call."""
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
        except Exception as e:
            import requests as _req
            if isinstance(e, _req.HTTPError) and e.response is not None:
                code = e.response.status_code
                body = e.response.text[:160]
                last_err = f"HTTP {code}: {body}"
                if code in (409, 422, 429, 400, 428):
                    time.sleep(0.3 + attempt * 0.1)
                    continue
                raise RuntimeError(f"gptmail inbox-token {last_err}") from e
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
    """Poll GPTMail GET /api/emails for Auth0 6-digit code (content field often has OTP)."""
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
                # re-claim same address if token rejected
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
                # timestamp filter (unix sec)
                ts = item.get("timestamp") or 0
                try:
                    ts_f = float(ts)
                    if ts_f > 1e12:  # ms
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
                # list content often already has "Your code is: 140765"
                code = _extract_otp(subject, body)
                if not code:
                    m = re.search(r"your code is\s*:?\s*(\d{6})", body, re.I)
                    code = m.group(1) if m else None
                if not code and mid:
                    # fetch detail
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
                            code = _extract_otp(subject, body)
                            if not code:
                                m = re.search(r"your code is\s*:?\s*(\d{6})", body, re.I)
                                code = m.group(1) if m else None
                    except Exception as e_det:
                        if polls % 4 == 0:
                            print(f"[GPTMAIL] detail fail: {e_det}", flush=True)
                if not code:
                    continue
                if not _is_auth0ish(subject, fr, body) and "verify" not in subject.lower():
                    if "code" not in body.lower() and "verify" not in body.lower():
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
            if polls == 1 or polls % 4 == 0:
                print(
                    f"[GPTMAIL] still waiting… {elapsed}s/{timeout}s msgs={len(items)}",
                    flush=True,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[GPTMAIL] HTTP {e.code}: {body[:120]}", flush=True)
            if e.code in (401, 403):
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
                except Exception as e2:
                    print(f"[GPTMAIL] re-claim fail: {e2}", flush=True)
        except Exception as e:
            print(f"[GPTMAIL] poll error: {e}", flush=True)
        time.sleep(2.5)
    print(f"[GPTMAIL] Timeout after {timeout}s for {addr}", flush=True)
    return None


# ── Exzork helpers (EMAIL_MODE=exzork — mailer.exzork.me) ────────────────────

_exzork_lock = threading.Lock()
_exzork_known: set[str] = set()


def _exzork_headers() -> dict[str, str]:
    if not EXZORK_API_KEY:
        raise RuntimeError("ENTER_EXZORK_API_KEY required for exzork mode")
    return {
        "Accept": "application/json",
        "User-Agent": "enter-farm/exzork",
        "X-API-Key": EXZORK_API_KEY,
    }


def _exzork_host() -> str:
    """Apex or random subdomain host for mailbox create."""
    base = (EXZORK_DOMAIN or "").strip().lower().lstrip("@").lstrip("*.")
    if not base:
        raise RuntimeError("ENTER_EXZORK_DOMAIN required for exzork mode")
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
    """Poll Exzork for Enter/Auth0 six-digit OTP."""
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
                code = _extract_otp(subject, body)
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
                            code = _extract_otp(subject, body)
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



_emailqu_domains: list[str] = []
_emailqu_domains_lock = threading.Lock()


def _emailqu_get(path: str, etag: str = "") -> tuple[int, dict, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(f"{EMAILQU_API}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.load(response), response.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, {}, e.headers.get("ETag", etag)
        raise


def _emailqu_apex_domains() -> list[str]:
    with _emailqu_domains_lock:
        if _emailqu_domains:
            return list(_emailqu_domains)
        _, data, _ = _emailqu_get("/api/domains/random")
        domains = [
            str(item.get("domain") or "").lower()
            for item in data.get("domains", [])
            if isinstance(item, dict)
            and not item.get("is_subdomain")
            and not item.get("is_hidden")
            and item.get("domain")
        ]
        if not domains:
            raise RuntimeError("emailqu: no public apex domains")
        _emailqu_domains.extend(dict.fromkeys(domains))
        return list(_emailqu_domains)


def create_emailqu_inbox() -> str:
    _, data, _ = _emailqu_get("/api/random-username")
    username = re.sub(r"[^a-z0-9]", "", str(data.get("username") or "").lower())
    if not username:
        raise RuntimeError("emailqu: random username missing")
    domain = random.choice(_emailqu_apex_domains())
    _, verified, _ = _emailqu_get(f"/api/domain/verify/{quote(domain, safe='')}")
    if not verified.get("verified"):
        raise RuntimeError(f"emailqu: domain not verified: {domain}")
    address = f"{username}@{domain}"
    print(f"[EMAILQU] claimed {address}", flush=True)
    return address


def read_otp_from_emailqu_sync(
    target_email: str, timeout: int = 180, since_ts: float | None = None
) -> str | None:
    address = target_email.lower().strip()
    path = f"/api/public/emails/{quote(address, safe='')}?limit=20"
    start = time.time()
    since_ts = since_ts or (start - 30)
    etag = ""
    seen: set[str] = set()
    print(f"[EMAILQU] Waiting OTP -> {address} (timeout={timeout}s)...", flush=True)
    while time.time() - start < timeout:
        try:
            status, data, new_etag = _emailqu_get(path, etag)
            etag = new_etag or etag
            if status == 304:
                time.sleep(3)
                continue
            for item in data.get("emails", []):
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                received = item.get("received_at")
                if received:
                    try:
                        ts = datetime.fromisoformat(str(received).replace("Z", "+00:00")).timestamp()
                        if ts < since_ts - 5:
                            continue
                    except Exception:
                        pass
                subject = str(item.get("subject") or "")
                body = str(item.get("body_text") or "") + "\n" + _strip_html(str(item.get("body_html") or ""))
                code = _extract_otp(subject, body)
                if code:
                    print(f"[EMAILQU] OTP found for {address}", flush=True)
                    return code
        except Exception as e:
            print(f"[EMAILQU] poll error: {type(e).__name__}: {e}", flush=True)
        time.sleep(3)
    return None


def _stdlib_json(url: str, *, data: dict | None = None, headers: dict | None = None) -> dict | list:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {}), **(headers or {})},
        method="POST" if body else "GET",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def _create_tempmail_io() -> tuple[str, str]:
    data = _stdlib_json(
        f"{TEMPMAIL_IO_API}/email/new",
        data={"min_name_length": 12, "max_name_length": 16},
        headers={"User-Agent": "enter-farm/1.0"},
    )
    return str(data["email"]).lower(), str(data["token"])


def _poll_tempmail_io(address: str, token: str, timeout: int, since_ts: float | None) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = _stdlib_json(
                f"{TEMPMAIL_IO_API}/email/{quote(address, safe='@')}/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            for item in rows if isinstance(rows, list) else []:
                code = _extract_otp(
                    str(item.get("subject") or ""),
                    str(item.get("body_text") or "") + "\n" + str(item.get("body_html") or ""),
                )
                if code:
                    return code
        except Exception as e:
            print(f"[ROTATE] tempmail.io poll: {type(e).__name__}: {e}", flush=True)
        time.sleep(3)
    return None


def _create_guerrillamail() -> tuple[str, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{GUERRILLA_API}?f=get_email_address&ip=127.0.0.1&agent=enter-farm", timeout=20) as response:
        first = json.load(response)
    sid = str(first["sid_token"])
    local = _crypto_local_part(12)
    with opener.open(
        f"{GUERRILLA_API}?f=set_email_user&email_user={quote(local)}&sid_token={quote(sid)}",
        timeout=20,
    ) as response:
        data = json.load(response)
    return str(data.get("email_addr") or first["email_addr"]).lower(), sid


def _poll_guerrillamail(address: str, token: str, timeout: int, since_ts: float | None) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(
                f"{GUERRILLA_API}?f=check_email&seq=0&sid_token={quote(token)}", timeout=20
            ) as response:
                data = json.load(response)
            for item in data.get("list") or []:
                code = _extract_otp(
                    str(item.get("mail_subject") or ""), str(item.get("mail_excerpt") or "")
                )
                if code:
                    return code
        except Exception as e:
            print(f"[ROTATE] guerrillamail poll: {type(e).__name__}: {e}", flush=True)
        time.sleep(3)
    return None


def _rotation_candidates() -> list[str]:
    supported = {"generator", "emailqu", "exzork", "mail.tm", "tempmail.io", "guerrillamail"}
    return [p for p in TEMPMAIL_ROTATION if p in supported and (p != "exzork" or (EXZORK_API_KEY and EXZORK_DOMAIN))]


def create_rotating_inbox() -> str:
    global _rotating_mail_idx
    providers = _rotation_candidates()
    if not providers:
        raise RuntimeError("rotate: no configured providers")
    with _rotating_mail_lock:
        start = _rotating_mail_idx % len(providers)
        _rotating_mail_idx += 1
    errors: list[str] = []
    for offset in range(len(providers)):
        provider = providers[(start + offset) % len(providers)]
        try:
            if provider == "generator":
                from generator_email import create_inbox
                address, token = create_inbox(), ""
            elif provider == "emailqu":
                address, token = create_emailqu_inbox(), ""
            elif provider == "exzork":
                address, token = create_exzork_inbox(), ""
            elif provider == "mail.tm":
                address, token = create_tempmail_account(), ""
            elif provider == "tempmail.io":
                address, token = _create_tempmail_io()
            else:
                address, token = _create_guerrillamail()
            domain = address.rsplit("@", 1)[-1].lower()
            if domain in _gptmail_blocked_domains:
                raise RuntimeError(f"blocked domain {domain}")
            with _rotating_mail_lock:
                _rotating_mail_accounts[address.lower()] = (provider, token)
            print(f"[ROTATE] provider={provider} domain={domain}", flush=True)
            return address
        except Exception as e:
            errors.append(f"{provider}:{type(e).__name__}")
            print(f"[ROTATE] {provider} unavailable: {type(e).__name__}: {e}", flush=True)
    raise RuntimeError("rotate: all providers failed (" + ", ".join(errors) + ")")


def read_otp_from_rotating_sync(
    target_email: str, timeout: int = 180, since_ts: float | None = None
) -> str | None:
    with _rotating_mail_lock:
        provider, token = _rotating_mail_accounts.get(target_email.lower(), ("", ""))
    polls = {
        "emailqu": read_otp_from_emailqu_sync,
        "exzork": read_otp_from_exzork_sync,
        "mail.tm": read_otp_from_tempmail_sync,
    }
    if provider == "generator":
        from generator_email import poll_otp
        return poll_otp(target_email, timeout, since_ts)
    if provider in polls:
        return polls[provider](target_email, timeout, since_ts)
    if provider == "tempmail.io":
        return _poll_tempmail_io(target_email, token, timeout, since_ts)
    if provider == "guerrillamail":
        return _poll_guerrillamail(target_email, token, timeout, since_ts)
    raise RuntimeError(f"rotate: provider session missing for {target_email.rsplit('@', 1)[-1]}")


async def generate_email(worker_slot: int | None = None) -> str:
    """worker_slot: concurrent slot (0..c-1) for gptmail sticky domain. Not a cap."""
    async with _emails_lock:
        for _ in range(200):
            if EMAIL_MODE == "rotate":
                addr = await asyncio.get_event_loop().run_in_executor(None, create_rotating_inbox)
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
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
            if EMAIL_MODE == "emailqu":
                addr = await asyncio.get_event_loop().run_in_executor(None, create_emailqu_inbox)
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
            if EMAIL_MODE == "exzork":
                addr = await asyncio.get_event_loop().run_in_executor(None, create_exzork_inbox)
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
            if EMAIL_MODE == "generator":
                from generator_email import create_inbox
                addr = await asyncio.get_event_loop().run_in_executor(None, create_inbox)
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
            if EMAIL_MODE == "tempmail":
                # create unique mail.tm inbox (API); no custom domain
                loop = asyncio.get_event_loop()
                addr = await loop.run_in_executor(None, create_tempmail_account)
                key = addr.lower()
                if key not in _used_emails:
                    _used_emails.add(key)
                    _persist_used_email(key)
                    return addr
                continue
            if EMAIL_MODE == "domain":
                if not EMAIL_DOMAIN:
                    raise RuntimeError("ENTER_EMAIL_DOMAIN required for domain mode")
                addr = f"{_crypto_local_part(EMAIL_LOCAL_LEN)}@{EMAIL_DOMAIN}"
            else:
                base = GMAIL_BASE or IMAP_USER
                if not base or "@" not in base:
                    raise RuntimeError("ENTER_GMAIL_BASE / ENTER_IMAP_USER required for plus_trick")
                user, _, domain = base.partition("@")
                user = user.split("+", 1)[0]
                addr = f"{user}+{_crypto_local_part(max(10, min(20, EMAIL_LOCAL_LEN)))}@{domain}"
            key = addr.lower()
            if key not in _used_emails:
                _used_emails.add(key)
                _persist_used_email(key)
                return addr
    raise RuntimeError("Could not generate unique email after 200 attempts")


# ── IMAP OTP (Auth0 / Enter = 6-digit) — patterned after grok-farm ───────────
_OTP6_RE = re.compile(r"\b(\d{6})\b")
# Auth0 / Converge subjects vary; keep broad but prefer verification context
_OTP_CONTEXT_RE = re.compile(
    r"(?:verification|verify|one[-\s]?time|passcode|security code|your code|"
    r"enter the code|confirmation code|login code|otp)[^\d]{0,80}(\d{6})",
    re.I,
)
_claimed_msg_ids_sync: set[str] = set()


def _email_body(msg) -> str:
    """Prefer text/plain, fallback html (grok-style)."""
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            try:
                raw = part.get_payload(decode=True)
                if not raw:
                    continue
                text = raw.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html:
                html = text
    else:
        try:
            raw = msg.get_payload(decode=True)
            text = (raw or b"").decode(msg.get_content_charset() or "utf-8", "replace")
            if (msg.get_content_type() or "").startswith("text/html"):
                html = text
            else:
                plain = text
        except Exception:
            plain = str(msg.get_payload() or "")
    return plain or html


def _strip_html(s: str) -> str:
    s = re.sub(r"<style[\s\S]*?</style>", " ", s or "", flags=re.I)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_otp(subject: str, body: str) -> str | None:
    plain = _strip_html(body)
    blob = f"{subject or ''}\n{plain}"
    m = _OTP_CONTEXT_RE.search(blob)
    if m:
        return m.group(1)
    # Subject-only 6 digits (Auth0 often: "123456 is your verification code")
    m = _OTP6_RE.search(subject or "")
    if m:
        return m.group(1)
    # Body: avoid matching years/ids — require nearby verify words OR isolated line
    for m in re.finditer(r"(?m)^\s*(\d{6})\s*$", plain):
        return m.group(1)
    m = _OTP6_RE.search(plain)
    return m.group(1) if m else None


def _msg_date_ts(msg) -> float | None:
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            from datetime import timezone as _tz

            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()
    except Exception:
        return None


def _recipient_blob(msg) -> str:
    return " ".join(
        filter(
            None,
            [
                msg.get("To", ""),
                msg.get("Delivered-To", ""),
                msg.get("X-Original-To", ""),
                msg.get("X-Forwarded-To", ""),
                msg.get("Cc", ""),
                msg.get("Envelope-To", ""),
            ],
        )
    ).lower()


def _is_auth0ish(subject: str, from_addr: str, body: str) -> bool:
    blob = f"{subject} {from_addr} {body[:500]}".lower()
    keys = (
        "auth0",
        "converge",
        "enter",
        "verification",
        "verify your",
        "one-time",
        "passcode",
        "security code",
        "confirm",
        "login code",
    )
    return any(k in blob for k in keys)


def read_otp_from_imap_sync(
    target_email: str, timeout: int = 180, since_ts: float | None = None
) -> str | None:
    """Poll Gmail IMAP for Auth0/Converge 6-digit code.

    Real mail shape (verified):
      From: noreply@converge.ai
      Subject: Verify your email
      Body: "Your code is: 334697"
      To: alias@catch-all  (also lands in IMAP_USER inbox)

    IMPORTANT: never SEARCH ALL on large Gmail inboxes — it hangs.
    """
    print(f"[IMAP] Waiting for Enter/Auth0 OTP -> {target_email} (timeout={timeout}s)...", flush=True)
    start = time.time()
    since_ts = since_ts or (start - 60)
    target_lower = target_email.lower()
    target_local = target_lower.split("@")[0]
    seen_uids: set[bytes] = set()
    polls = 0

    # IMAP SINCE date (UTC day) — broad enough for clock skew
    since_date = time.strftime("%d-%b-%Y", time.gmtime(since_ts - 86400))

    while time.time() - start < timeout:
        polls += 1
        elapsed = int(time.time() - start)
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
            mail.login(IMAP_USER, IMAP_PASS)
            mail.select("INBOX")

            # FULL AUDIT (44/44 timeout mails): mail WAS in Gmail with code.
            # Farm failed because it scanned hundreds of FROM converge mails and
            # often never fetched the one TO this alias within 180s.
            # Fix: search TO "exact@alias" FIRST (hits=1), then narrow TEXT fallbacks.
            id_set: list[bytes] = []
            seen_id: set[bytes] = set()
            queries = (
                # Proven: exact recipient (audit found every timeout this way)
                f'(TO "{target_lower}")',
                f'(TEXT "{target_lower}")',
                # Narrow: converge + local-part only (not whole inbox FROM converge)
                f'(FROM "converge.ai" TEXT "{target_local}")',
                f'(FROM "noreply@converge.ai" TEXT "{target_local}")',
                f'(SUBJECT "Verify your email" TEXT "{target_local}")',
                # Last resort: recent converge (bounded) — only if nothing above
            )
            for query in queries:
                try:
                    status, messages = mail.search(None, query)
                except Exception as e:
                    if polls == 1:
                        print(f"[IMAP] search skip {query!r}: {e}", flush=True)
                    continue
                if status != "OK" or not messages or not messages[0]:
                    continue
                for mid in messages[0].split():
                    if mid not in seen_id:
                        seen_id.add(mid)
                        id_set.append(mid)
                # Exact TO hit is enough — don't pile hundreds of other mails
                if id_set and query.startswith('(TO "'):
                    break

            # Only if still empty: tiny recent converge window (not full history)
            if not id_set:
                try:
                    status, messages = mail.search(
                        None, f'(FROM "converge.ai" SINCE {since_date})'
                    )
                    if status == "OK" and messages and messages[0]:
                        # only newest 25 — never 280+
                        for mid in messages[0].split()[-25:]:
                            if mid not in seen_id:
                                seen_id.add(mid)
                                id_set.append(mid)
                except Exception:
                    pass

            # Newest first — and only a small set now
            for mid in reversed(id_set[-30:]):
                if mid in seen_uids:
                    continue
                try:
                    if mid.startswith(b"UID:"):
                        uid = mid[4:]
                        status, data = mail.uid("fetch", uid, "(RFC822)")
                    else:
                        status, data = mail.fetch(mid, "(RFC822)")
                except Exception:
                    seen_uids.add(mid)
                    continue
                if not data or not data[0]:
                    seen_uids.add(mid)
                    continue
                # data[0] can be tuple (meta, bytes) or weird partials
                raw = None
                for part in data:
                    if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                        raw = part[1]
                        break
                if not raw:
                    seen_uids.add(mid)
                    continue
                try:
                    msg = message_from_bytes(raw)
                except Exception:
                    seen_uids.add(mid)
                    continue

                subject = msg.get("Subject", "") or ""
                from_addr = (msg.get("From", "") or "").lower()
                to_addr = _recipient_blob(msg)
                body = _email_body(msg)
                body_l = body.lower()

                dts = _msg_date_ts(msg)
                if dts is not None and dts < (since_ts - 120):
                    seen_uids.add(mid)
                    continue

                header_hit = target_lower in to_addr or (
                    len(target_local) >= 6 and target_local in to_addr
                )
                body_hit = target_lower in body_l or (
                    len(target_local) >= 8 and target_local in body_l
                )
                from_hit = "converge.ai" in from_addr or "auth0" in from_addr
                # Converge mails always address To: alias@domain — require header or body
                if not header_hit and not body_hit:
                    seen_uids.add(mid)
                    continue
                if not from_hit and not _is_auth0ish(subject, from_addr, body):
                    seen_uids.add(mid)
                    continue

                code = _extract_otp(subject, body)
                # explicit "Your code is: NNNNNN" (Auth0/Converge template)
                if not code:
                    m = re.search(r"your code is\s*:?\s*(\d{6})", body_l, re.I)
                    if m:
                        code = m.group(1)
                if not code:
                    seen_uids.add(mid)
                    continue

                mid_key = mid.decode("utf-8", "ignore") if isinstance(mid, bytes) else str(mid)
                with _claimed_otps_lock:
                    if code in _claimed_otps_sync or mid_key in _claimed_msg_ids_sync:
                        seen_uids.add(mid)
                        continue
                    _claimed_otps_sync.add(code)
                    _claimed_msg_ids_sync.add(mid_key)

                print(
                    f"[IMAP] OTP found: {code} for {target_email} "
                    f"(subj={subject[:60]!r} from={from_addr[:40]!r} t+{elapsed}s)",
                    flush=True,
                )
                try:
                    if mid.startswith(b"UID:"):
                        mail.uid("store", mid[4:], "+FLAGS", "\\Seen")
                    else:
                        mail.store(mid, "+FLAGS", "\\Seen")
                except Exception:
                    pass
                try:
                    mail.logout()
                except Exception:
                    pass
                return code

            try:
                mail.logout()
            except Exception:
                pass
            if polls == 1 or polls % 3 == 0:
                # Count how many candidates actually match target domain (helps catch-all misconfig)
                domain = target_lower.split("@")[-1] if "@" in target_lower else ""
                print(
                    f"[IMAP] still waiting… {elapsed}s/{timeout}s candidates={len(id_set)} "
                    f"target={target_local}@{domain}",
                    flush=True,
                )
                if polls == 1 and domain:
                    print(
                        f"[IMAP] tip: if catch-all for {domain} does not forward into "
                        f"{IMAP_USER}, OTP will never appear here (routing UI != IMAP inbox).",
                        flush=True,
                    )
        except Exception as e:
            print(f"[IMAP] poll error: {e}", flush=True)
            try:
                if mail is not None:
                    mail.logout()
            except Exception:
                pass
        time.sleep(3)
    domain = target_lower.split("@")[-1] if "@" in target_lower else ""
    print(f"[IMAP] Timeout after {timeout}s for {target_email}", flush=True)
    print(
        f"[IMAP] DIAG: no matching mail in {IMAP_USER} for domain={domain}. "
        f"Routing dashboard may show delivery elsewhere. "
        f"Fix: forward catch-all @{domain} -> {IMAP_USER} (same as novela.biz.id setup), "
        f"or set ENTER_EMAIL_DOMAIN to a domain that already lands in this IMAP.",
        flush=True,
    )
    return None


async def wait_otp_imap_keepalive(
    page, email_addr: str, timeout_s: int, since_ts: float, attempt: int
) -> str | None:
    """Poll IMAP in a thread; keep browser awake (from grok-farm)."""
    loop = asyncio.get_event_loop()
    fut = loop.run_in_executor(
        None,
        lambda: read_otp_from_imap_sync(email_addr, timeout_s, since_ts),
    )
    tick = 0
    while not fut.done():
        tick += 1
        try:
            await page.evaluate("() => document.title")
            if tick % 4 == 0:
                # soft nudge so React OTP inputs don't go stale
                try:
                    loc = page.locator(
                        'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                    ).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=1000)
                except Exception:
                    pass
        except Exception as e:
            if tick % 5 == 0:
                alog(attempt, f"OTP keep-alive warn: {e}")
        try:
            await asyncio.wait({fut}, timeout=3.5)
        except Exception:
            await asyncio.sleep(3.5)
    return fut.result()


async def wait_otp_imap(email_addr: str, since_ts: float | None = None, page=None, attempt: int = 0) -> str:
    """Wait for OTP via IMAP, mail.tm, GPTMail, or generator.email."""
    since = since_ts if since_ts is not None else (time.time() - 45)
    loop = asyncio.get_event_loop()

    if EMAIL_MODE in ("tempmail", "gptmail", "generator", "exzork", "emailqu", "rotate"):
        if EMAIL_MODE == "rotate":
            poll_fn, label = read_otp_from_rotating_sync, "ROTATE"
        elif EMAIL_MODE == "gptmail":
            poll_fn, label = read_otp_from_gptmail_sync, "GPTMAIL"
        elif EMAIL_MODE == "generator":
            from generator_email import poll_otp
            poll_fn, label = poll_otp, "GENERATOR"
        elif EMAIL_MODE == "exzork":
            poll_fn, label = read_otp_from_exzork_sync, "EXZORK"
        elif EMAIL_MODE == "emailqu":
            poll_fn, label = read_otp_from_emailqu_sync, "EMAILQU"
        else:
            poll_fn, label = read_otp_from_tempmail_sync, "TEMPMAIL"
        fut = loop.run_in_executor(
            None,
            lambda: poll_fn(email_addr, OTP_TIMEOUT_S, since),
        )
        tick = 0
        while not fut.done():
            tick += 1
            if page is not None:
                try:
                    await page.evaluate("() => document.title")
                except Exception:
                    pass
            try:
                await asyncio.wait({fut}, timeout=3.5)
            except Exception:
                await asyncio.sleep(3.5)
        code = fut.result()
        if not code:
            raise RuntimeError(f"{label} OTP timeout after {OTP_TIMEOUT_S}s for {email_addr}")
        return code

    if page is not None:
        code = await wait_otp_imap_keepalive(page, email_addr, OTP_TIMEOUT_S, since, attempt)
    else:
        code = await loop.run_in_executor(
            None,
            lambda: read_otp_from_imap_sync(email_addr, OTP_TIMEOUT_S, since),
        )
    if not code:
        raise RuntimeError(f"OTP timeout after {OTP_TIMEOUT_S}s for {email_addr}")
    return code


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


# ── OIDC / API ───────────────────────────────────────────────────────────────
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
    if "enter.converge.ai" not in host and "converge.ai" not in host:
        # still allow if query has code=
        if "code=" not in url:
            return None
    params = parse_qs(parsed.query)
    vals = params.get("code")
    return vals[0] if vals else None


def exchange_code_for_tokens(code: str, verifier: str) -> dict:
    form = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": APP_HOST,
            "Auth0-Client": base64.b64encode(
                json.dumps({"name": "auth0-react", "version": "2.10.0"}).encode()
            ).decode(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if not access:
        raise RuntimeError(f"token response missing access_token: {list(data.keys())}")
    expires_in = int(data.get("expires_in") or 86400)
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in
    expires_at_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
    email = ""
    id_token = data.get("id_token") or ""
    if id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            email = payload.get("email") or ""
        except Exception:
            pass
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "expires_at": expires_at_iso,
        "expires_in": expires_in,
        "token_type": data.get("token_type") or "Bearer",
        "scope": data.get("scope") or SCOPE,
        "email_from_id": email,
    }


def _api_json(
    method: str,
    path: str,
    access_token: str,
    body: dict | None = None,
    query: dict | None = None,
    *,
    send_json: bool = False,
) -> dict:
    url = f"{API_HOST}{path}"
    if query:
        url += ("&" if "?" in url else "?") + urlencode(query)
    data = None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Origin": APP_HOST,
        "Referer": f"{APP_HOST}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    }
    if body is not None or send_json:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"API {method} {path} -> {e.code}: {err_body[:400]}") from e


def _require_api_data(response: dict, label: str):
    if not isinstance(response, dict) or response.get("code") != 0:
        raise RuntimeError(f"{label} failed")
    return response.get("data")


def enter_post_auth_setup(access_token: str, gift_code: str) -> dict:
    """Validate the captured post-auth v2 contract and create one API key."""
    out: dict[str, Any] = {}

    claim = _api_json(
        "POST", "/code/api/v1/referral/claim", access_token,
        body=None, query={"code": gift_code}, send_json=True,
    )
    _require_api_data(claim, "referral claim")
    out["referral_claim"] = claim

    user_info = _api_json("GET", "/code/api/v1/users/info", access_token)
    user_data = _require_api_data(user_info, "users info")
    if not isinstance(user_data, dict) or user_data.get("must_verify_email") is not False:
        raise RuntimeError("user email is not verified")
    if user_data.get("merge_action"):
        raise RuntimeError("user merge action is unresolved")
    out["user_info"] = user_info

    workspaces = _api_json("GET", "/code/api/v1/workspaces", access_token)
    workspace_data = _require_api_data(workspaces, "workspaces")
    items = workspace_data.get("workspaces") if isinstance(workspace_data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise RuntimeError("workspace list is empty")
    workspace_id = str(items[0].get("id") or items[0].get("workspace_id") or "").strip()
    if not workspace_id:
        raise RuntimeError("workspace id is empty")
    out["workspaces"] = workspaces
    out["workspace_id"] = workspace_id

    config = _api_json("GET", "/code/api/v1/onboarding/config", access_token)
    config_data = _require_api_data(config, "onboarding config")
    if not isinstance(config_data, dict) or config_data.get("flow_version") != "v2":
        raise RuntimeError("unsupported onboarding flow")
    out["onboarding_config"] = config
    if not config_data.get("completed"):
        onboarding = _api_json(
            "POST",
            "/code/api/v1/onboarding/complete",
            access_token,
            body={
                "role": ONBOARDING_ROLE,
                "industry": ONBOARDING_INDUSTRY,
                "team_size": ONBOARDING_TEAM_SIZE,
                "build_intent": BUILD_INTENT,
                "agency_service_interest": ONBOARDING_AGENCY_INTEREST,
            },
        )
        onboarding_data = _require_api_data(onboarding, "onboarding completion")
        if not isinstance(onboarding_data, dict) or onboarding_data.get("success") is not True or onboarding_data.get("completed") is not True:
            raise RuntimeError("onboarding did not complete")
        out["onboarding"] = onboarding

    key_body = {
        "name": API_KEY_NAME,
        "scope": API_KEY_SCOPE,
        "reveal_policy": API_KEY_REVEAL,
    }
    created = _api_json(
        "POST",
        f"/code/api/v1/workspaces/{workspace_id}/api-keys",
        access_token,
        body=key_body,
    )
    created_data = _require_api_data(created, "api key creation")
    if not isinstance(created_data, dict):
        raise RuntimeError("api key response is invalid")
    key = str(created_data.get("key") or "").strip()
    key_id = str(created_data.get("id") or "").strip()
    if not key or not key_id:
        raise RuntimeError("api key response missing key or id")
    out["api_key"] = created
    try:
        rewards = _api_json("GET", "/code/api/v1/referral/rewards", access_token)
        out["rewards"] = rewards
    except Exception as e:
        out["rewards_error"] = str(e)
    return out


# ── Browser ──────────────────────────────────────────────────────────────────
def _is_nav_timeout(err: BaseException) -> bool:
    s = f"{type(err).__name__}: {err}".lower()
    return (
        "timeout" in s
        and (
            "goto" in s
            or "navigat" in s
            or "page.goto" in s
            or "net::" in s
            or "err_connection" in s
            or "err_timed_out" in s
            or "ns_error" in s
        )
    ) or "err_connection" in s or "err_timed_out" in s


async def goto_with_retry(
    page,
    url: str,
    attempt: int,
    *,
    label: str = "goto",
    timeout_ms: int | None = None,
    retries: int | None = None,
    warp_on_fail: bool | None = None,
) -> None:
    """page.goto with retries; optional WARP rotate after nav timeout (CF hang)."""
    timeout_ms = timeout_ms if timeout_ms is not None else GOTO_TIMEOUT_MS
    retries = retries if retries is not None else GOTO_RETRIES
    warp_on_fail = GOTO_WARP_ON_FAIL if warp_on_fail is None else warp_on_fail
    last: BaseException | None = None
    warped = False

    for try_i in range(1, retries + 1):
        # commit = first response (faster under CF); then domcontentloaded
        wait_until = "commit" if try_i == 1 else "domcontentloaded"
        try:
            if try_i == 1:
                alog(attempt, f"{label}")
            else:
                alog(attempt, f"{label} retry {try_i}/{retries}")
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            # ensure we actually left about:blank / got a host
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=min(20000, timeout_ms))
            except Exception:
                pass
            u = (page.url or "").lower()
            if u.startswith("about:") or u in ("", "about:blank"):
                raise RuntimeError(f"{label}: still on blank after goto ({page.url!r})")
            if try_i > 1:
                alog(attempt, f"{label} ok (try {try_i})")
            return
        except Exception as e:
            last = e
            # "miss" not "fail" — avoid HUD FAIL label; retries continue
            alog(attempt, f"{label} miss {type(e).__name__}")
            navish = _is_nav_timeout(e) or "blank" in str(e).lower()
            if not navish and try_i >= retries:
                break
            # soft recover: stop loading, brief pause
            try:
                await page.evaluate("() => { try { window.stop(); } catch (e) {} }")
            except Exception:
                pass
            if warp_on_fail and GOTO_WARP_ON_FAIL and navish and not warped:
                alog(attempt, f"{label} WARP rotate")
                try:
                    await warp_rotate_ip_async(attempt)
                    warped = True
                except Exception as we:
                    alog(attempt, f"{label} WARP err {we}")
            delay = GOTO_RETRY_DELAY * try_i + random.uniform(0, 1.5)
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"{label} failed after {retries} tries: {type(last).__name__}: {last}"
    ) from last


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
    page.set_default_timeout(max(60000, GOTO_TIMEOUT_MS + 15000))
    return manager, browser, page


async def screenshot(page, attempt: int, tag: str):
    try:
        path = SCREENSHOT_DIR / f"enter_farm_{attempt}_{tag}.png"
        await page.screenshot(path=str(path), full_page=True)
        alog(attempt, f"screenshot: {path}")
    except Exception as e:
        alog(attempt, f"screenshot fail: {e}")


async def fill_input(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            await loc.click(timeout=3000)
            await loc.fill("")
            await loc.type(value, delay=random.randint(25, 55))
            return True
        except Exception:
            continue
    try:
        return bool(
            await page.evaluate(
                """({selectors, value}) => {
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
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
        )
    except Exception:
        return False


async def click_text_button(page, keywords: list[str]) -> str | None:
    for kw in keywords:
        try:
            loc = page.get_by_role("button", name=re.compile(kw, re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                txt = (await loc.first.inner_text()).strip()
                await loc.first.click()
                return txt
        except Exception:
            pass
        try:
            loc = page.locator(f"button:has-text('{kw}'), a:has-text('{kw}'), [role=button]:has-text('{kw}')").first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                return kw
        except Exception:
            continue
    return None


async def turnstile_token_len(page) -> int:
    try:
        return int(
            await page.evaluate(
                """() => {
                    const names = ['cf-turnstile-response', 'captcha', 'g-recaptcha-response'];
                    for (const n of names) {
                        const el = document.querySelector('[name="' + n + '"], textarea[name="' + n + '"]');
                        if (el && el.value && el.value.length > 20) return el.value.length;
                    }
                    const inputs = document.querySelectorAll('input, textarea');
                    for (const i of inputs) {
                        const nm = (i.name || '') + (i.id || '');
                        if (/turnstile|captcha/i.test(nm) && i.value && i.value.length > 20) return i.value.length;
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
                        alog(attempt, f"Turnstile: clicked host text ({sel})")
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
                alog(attempt, f"Turnstile: clicked container {sel}")
                return True
            except Exception:
                continue

        # 3) Inside CF frames - checkbox selectors
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
                    alog(attempt, f"Turnstile: clicked frame {sel}")
                    return True
                except Exception:
                    continue
    except Exception as e:
        alog(attempt, f"Turnstile click error: {e}")
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
                    const complete = btns.find(b => /complete\s+sign\s*up/i.test((b.innerText||'').trim()));
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
        alog(attempt, f"Turnstile: clicked slot above Complete ({x:.0f},{y:.0f})")
        return True
    except Exception as e:
        alog(attempt, f"Turnstile slot click warn: {e}")
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
    Hard: remove dead CF iframes (last resort - can leave blank box if React won't remount).
    """
    mode = "hard" if hard else "soft"
    alog(attempt, f"Turnstile: remount ({mode})")

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
        alog(attempt, f"Turnstile remount JS warn: {e}")

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
    """Camoufox auto-pass -> managed checkbox click -> vision for interactive puzzles.

    require_token=True: used on Complete sign-up - blank widget means NOT ready,
    never treat absence of iframe as success.
    use_global_limit=True: acquire TURNSTILE_PARALLEL semaphore (concurrent farm).
    allow_remount=False: click-only (old complete_signup style - no soft/hard remount).
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
            alog(attempt, f"Turnstile: token present (len={tok})")
            return True

        # CF hard-fail widget - must remount, clicking forever does nothing
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
            alog(attempt, f"Turnstile: Verification failed (remounts exhausted)")
            return False

        visible = await turnstile_visible(page)
        mounted = await _turnstile_mount_present(page)
        if not visible and not mounted:
            if require_token:
                # Wait longer before aggressive clicking - blank mount often still loading
                if clicks == 0:
                    await asyncio.sleep(2.0)
                if clicks < 6:
                    await _click_turnstile_slot_above_complete(page, attempt)
                    await try_click_turnstile(page, attempt)
                    clicks += 1
                # blank for a long time -> soft then hard remount (login path)
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
            # Widget still loading (blank grey box) - wait first, then poke
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
                alog(attempt, f"Turnstile: solved after click")
                return True
            continue

        # Still blocked - vision for interactive image challenge
        try:
            img = await page.screenshot(full_page=True)
            b64 = base64.b64encode(img).decode("ascii")
            resp = _call_vision_model(b64, _VISION_TURNSTILE_PROMPT)
            if resp:
                alog(attempt, f"Turnstile vision: {resp[:120]}")
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
            alog(attempt, f"Turnstile vision fail: {e}")

        await asyncio.sleep(1.2)

    tok = await turnstile_token_len(page)
    if tok > 20:
        return True
    alog(attempt, f"Turnstile: timeout after {max_wait}s (token_len={tok})")
    return False




async def wait_url(page, pred, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = page.url
        if pred(url):
            return url
        await asyncio.sleep(0.35)
    raise RuntimeError(f"URL wait timeout; last={page.url}")


def _is_enter_url(url: str, path: str) -> bool:
    try:
        parsed = urlparse(url)
        app = urlparse(APP_HOST)
        return parsed.scheme == app.scheme and parsed.netloc == app.netloc and parsed.path == path
    except Exception:
        return False


def _is_enter_login_url(url: str) -> bool:
    return _is_enter_url(url, "/auth/login")


def _is_enter_callback_url(url: str) -> bool:
    return _is_enter_url(url, "/auth/callback")


def _is_gateway_callback_status(status: int) -> bool:
    return 300 <= status < 400


def _is_enter_app_url(url: str) -> bool:
    return _is_enter_url(url, "/")


def _parse_gateway_session(status: int, body: str) -> dict:
    if status != 200:
        raise RuntimeError(f"gateway session returned HTTP {status}")
    try:
        data = json.loads(body)
    except (TypeError, json.JSONDecodeError) as e:
        raise RuntimeError("gateway session returned malformed JSON") from e
    if not isinstance(data, dict):
        raise RuntimeError("gateway session JSON must be an object")
    user = data.get("user")
    access = data.get("accessToken")
    expires_at = data.get("expiresAt")
    if not isinstance(user, dict) or not user:
        raise RuntimeError("gateway session missing user")
    if user.get("isNewUser") is not True:
        raise RuntimeError("gateway session is not a new user")
    if not isinstance(access, str) or not access.strip():
        raise RuntimeError("gateway session missing accessToken")
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise RuntimeError("gateway session missing expiresAt")
    return {"access_token": access, "expires_at": expires_at, "user": user}


async def _fetch_gateway_session(page) -> dict:
    response = await page.evaluate(
        """async () => {
            const response = await fetch('/auth/session?include=access_token', {
                credentials: 'include',
                headers: {'Accept': 'application/json'},
            });
            return {status: response.status, body: await response.text()};
        }"""
    )
    return _parse_gateway_session(
        int(response.get("status") or 0), response.get("body") or ""
    )


async def _click_official_login_action(page, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for text in ("Reject All", "Accept All", "Got It"):
            try:
                consent = page.get_by_role("button", name=re.compile(f"^{re.escape(text)}$", re.I))
                if await consent.count() and await consent.first.is_visible():
                    await consent.first.click(timeout=1500)
            except Exception:
                pass
        try:
            locator = page.get_by_role("button", name=re.compile(r"^Get Free Credits$", re.I))
            for index in range(await locator.count()):
                button = locator.nth(index)
                if await button.is_visible():
                    await button.click(timeout=3000)
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def do_signup_and_oauth(page, email_addr: str, password: str, attempt: int) -> dict:
    """Browser: referral landing -> Enter gateway -> Auth0 signup -> gateway session."""
    login_seen = asyncio.Event()
    callback_seen = asyncio.Event()

    async def on_response(resp):
        url = resp.url or ""
        if _is_enter_login_url(url):
            login_seen.set()
        elif _is_enter_callback_url(url) and _is_gateway_callback_status(resp.status):
            await resp.finished()
            callback_seen.set()

    def on_nav(frame):
        if frame != page.main_frame:
            return
        if _is_enter_login_url(frame.url):
            login_seen.set()

    page.on("response", on_response)
    page.on("framenavigated", on_nav)

    q = {"gift": GIFT_CODE, "inviteeReward": INVITEE_REWARD}
    if INVITER:
        q["inviter"] = INVITER
    land = f"{APP_HOST}/?{urlencode(q)}"
    alog(attempt, "landing")
    await goto_with_retry(page, land, attempt, label="landing")
    await asyncio.sleep(3.0)

    # The landing action owns FPJS risk preflight and gateway PKCE. If the app
    # fails open without navigating, use only its same-origin gateway fallback.
    started = await _click_official_login_action(page)
    if started:
        alog(attempt, "official login action triggered")
    try:
        await asyncio.wait_for(login_seen.wait(), timeout=13)
    except asyncio.TimeoutError:
        await goto_with_retry(
            page,
            f"{APP_HOST}/auth/login?return_to=%2F",
            attempt,
            label="gateway_login",
        )
    await asyncio.sleep(1.5)

    # Prefer signup over login
    for _ in range(3):
        url = page.url
        if "/u/signup" in url:
            break
        clicked = await click_text_button(
            page,
            [
                "Sign up",
                "Sign Up",
                "Create account",
                "Create Account",
                "Register",
            ],
        )
        if clicked:
            alog(attempt, f"clicked: {clicked}")
            await asyncio.sleep(1.2)
            continue
        # direct signup path if still on login
        if "/u/login" in url:
            try:
                signup_link = page.locator("a[href*='signup'], a:has-text('Sign up'), a:has-text('Sign Up')").first
                if await signup_link.count() > 0:
                    await signup_link.click()
                    await asyncio.sleep(1.2)
            except Exception:
                pass
        break

    # If still login identifier, try navigate signup with same state from URL
    if "/u/login" in page.url and "state=" in page.url:
        try:
            st = parse_qs(urlparse(page.url).query).get("state", [""])[0]
            if st:
                await goto_with_retry(
                    page,
                    f"{AUTH_HOST}/u/signup/identifier?state={st}",
                    attempt,
                    label="signup_identifier",
                    timeout_ms=min(60000, GOTO_TIMEOUT_MS + 15000),
                    retries=max(2, GOTO_RETRIES - 1),
                    warp_on_fail=False,
                )
                await asyncio.sleep(1.0)
        except Exception:
            pass

    await screenshot(page, attempt, "signup_identifier")

    # Email
    ok = await fill_input(
        page,
        [
            'input[name="email"]',
            'input[type="email"]',
            'input[inputmode="email"]',
            'input[id*="email" i]',
            'input[autocomplete="email"]',
        ],
        email_addr,
    )
    if not ok:
        await screenshot(page, attempt, "email_fail")
        raise RuntimeError("could not fill email")
    alog(attempt, f"email filled")

    await handle_turnstile(page, attempt, max_wait=60, require_token=True, use_global_limit=True)
    await asyncio.sleep(0.5)

    otp_since = time.time()
    btn = await click_text_button(page, ["Continue", "Next", "Submit", "Sign up", "Sign Up"])
    if not btn:
        # form submit fallback
        try:
            await page.locator('button[type="submit"]').first.click(timeout=5000)
        except Exception as e:
            await screenshot(page, attempt, "continue_fail")
            raise RuntimeError(f"continue after email failed: {e}") from e
    alog(attempt, f"submitted email ({btn or 'submit'})")
    await asyncio.sleep(2.0)
    await screenshot(page, attempt, "after_email")

    # Domain block (sticky gptmail must rotate) — check before generic rate-limit
    dom_block = await page_has_domain_block(page)
    if dom_block:
        await screenshot(page, attempt, "domain_blocked")
        raise RuntimeError(f"domain_not_allowed: {dom_block}")

    # Auth0 rate limit often appears right after email Continue
    await raise_if_rate_limited(page, attempt, "after_email")

    # Email challenge / OTP
    async def on_challenge() -> bool:
        u = page.url
        return "challenge" in u or "password" in u or "code" in u or callback_seen.is_set()

    try:
        await wait_url(page, lambda u: "challenge" in u or "password" in u or _is_enter_callback_url(u), 60)
    except Exception:
        pass

    dom_block = await page_has_domain_block(page)
    if dom_block:
        await screenshot(page, attempt, "domain_blocked_challenge")
        raise RuntimeError(f"domain_not_allowed: {dom_block}")

    await raise_if_rate_limited(page, attempt, "challenge")

    if "password" not in page.url and not callback_seen.is_set():
        # need OTP
        alog(attempt, f"waiting OTP (timeout={OTP_TIMEOUT_S}s)...")
        code = await wait_otp_imap(
            email_addr, since_ts=otp_since - 20, page=page, attempt=attempt
        )
        filled = await fill_input(
            page,
            [
                'input[name="code"]',
                'input[name="email-verification-code"]',
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
                'input[type="tel"]',
            ],
            code,
        )
        if not filled:
            # multi-box
            try:
                boxes = page.locator('input[maxlength="1"]')
                n = await boxes.count()
                if n >= 4:
                    for i, ch in enumerate(code[:n]):
                        await boxes.nth(i).fill(ch)
                    filled = True
            except Exception:
                pass
        if not filled:
            await screenshot(page, attempt, "otp_fill_fail")
            raise RuntimeError("could not fill OTP")
        await click_text_button(page, ["Continue", "Next", "Submit", "Verify"])
        try:
            await page.locator('button[type="submit"]').first.click(timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    # Password page
    if not callback_seen.is_set():
        try:
            await wait_url(page, lambda u: "password" in u or _is_enter_callback_url(u), 45)
        except Exception:
            await screenshot(page, attempt, "no_password")
            raise RuntimeError(f"expected password page, got {page.url}")

    if "password" in page.url:
        ok = await fill_input(
            page,
            [
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="new-password"]',
            ],
            password,
        )
        if not ok:
            await screenshot(page, attempt, "password_fail")
            raise RuntimeError("could not fill password")
        # confirm password if present
        await fill_input(
            page,
            [
                'input[name="re-enter-password"]',
                'input[name="confirmPassword"]',
                'input[autocomplete="new-password"]',
            ],
            password,
        )
        # Auth0 password step usually has NO Turnstile (HAR #120).
        # require_token=True + slot-above-Complete wrongly targets Continue forever.
        await handle_turnstile(
            page,
            attempt,
            max_wait=8,
            require_token=False,
            password=password,
            use_global_limit=True,
            allow_remount=False,
        )
        clicked = await click_text_button(
            page, ["Continue", "Sign up", "Sign Up", "Create", "Submit", "Next"]
        )
        if not clicked:
            try:
                await page.locator('button[type="submit"]').first.click(timeout=5000)
                clicked = "submit"
            except Exception:
                pass
        if not clicked:
            try:
                loc = page.get_by_role("button", name=re.compile(r"^continue$", re.I)).first
                if await loc.count() > 0:
                    await loc.click(timeout=5000)
                    clicked = "Continue"
            except Exception:
                pass
        if not clicked:
            await screenshot(page, attempt, "password_continue_fail")
            raise RuntimeError("password filled but Continue not clicked")
        alog(attempt, f"password submitted ({clicked})")
        await asyncio.sleep(2.0)
        # Banner often appears on the same password page after Continue
        dom_block = await page_has_domain_block(page)
        if dom_block:
            raise RuntimeError(f"domain_not_allowed: {dom_block}")
        await raise_if_rate_limited(page, attempt, "after_password")

    try:
        await asyncio.wait_for(callback_seen.wait(), timeout=60)
    except asyncio.TimeoutError as e:
        await raise_if_rate_limited(page, attempt, "wait_callback")
        raise RuntimeError("Enter auth callback not reached") from e

    try:
        await wait_url(page, _is_enter_app_url, 8)
    except Exception:
        # The callback response already established HttpOnly app-session
        # cookies. Some headless runs do not complete its final document
        # redirect; return to the same-origin app before reading the session.
        await goto_with_retry(page, f"{APP_HOST}/", attempt, label="callback_return")

    tokens = await _fetch_gateway_session(page)
    tokens.update(
        {
            "refresh_token": "",
            "id_token": "",
            "token_type": "Bearer",
            "scope": SCOPE,
            "email_from_id": "",
        }
    )
    alog(attempt, "authenticated gateway session established")
    return tokens


def _normalize_token_response(data: dict) -> dict:
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if not access:
        raise RuntimeError(f"token response missing access_token: {list(data.keys())}")
    expires_in = int(data.get("expires_in") or 86400)
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in
    expires_at_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
    email = ""
    id_token = data.get("id_token") or ""
    if id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            email = payload.get("email") or ""
        except Exception:
            pass
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "expires_at": expires_at_iso,
        "expires_in": expires_in,
        "token_type": data.get("token_type") or "Bearer",
        "scope": data.get("scope") or SCOPE,
        "email_from_id": email,
    }


async def _signin_snarf_tokens(page, attempt: int, token_bag: dict, email_addr: str = "", password: str = "") -> dict | None:
    """Click Sign in like a human; wait for SPA POST /oauth/token; return tokens."""
    token_bag["tokens"] = None
    # If already rate-limited, Sign in is not on page — fail fast (caller trips cooldown)
    await raise_if_rate_limited(page, attempt, "signin_snarf_entry")
    await _dismiss_app_modals(page)

    # Also snarf Bearer tokens from outgoing API requests (Auth0 SPA SDK v2+ stores in-memory)
    bearer_bag: dict[str, str] = {}

    def _on_request(req):
        try:
            auth = req.headers.get("authorization") or ""
            if auth.startswith("Bearer eyJ") and not bearer_bag.get("access_token"):
                bearer_bag["access_token"] = auth[7:]
        except Exception:
            pass

    page.on("request", _on_request)

    signed = await click_text_button(
        page, ["Sign in", "Sign In", "Log in", "Log In", "Get Free Credits"]
    )
    alog(attempt, f"recovery UI clicked: {signed or '(none)'}")
    await asyncio.sleep(1.0)
    await _dismiss_app_modals(page)
    # After failed click, page may still show rate-limit banner
    if not signed:
        await raise_if_rate_limited(page, attempt, "signin_button_missing")

    # Wait for SPA to finish OAuth (network snarf) OR land on app without error
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if token_bag["tokens"]:
            page.remove_listener("request", _on_request)
            return _normalize_token_response(token_bag["tokens"])
        # Check bearer from outgoing requests
        if bearer_bag.get("access_token"):
            alog(attempt, "tokens snarfed from outgoing API request Bearer header")
            page.remove_listener("request", _on_request)
            return _normalize_token_response({
                "access_token": bearer_bag["access_token"],
                "refresh_token": "",
                "expires_in": 86400,
                "token_type": "Bearer",
                "scope": SCOPE,
            })
        u = page.url
        # Auth0 login form after "Sign in" click — fill credentials to complete OAuth
        if email_addr and password and not bearer_bag.get("_login_filled") and (
            "/u/login" in u or ("/u/signup" in u and "identifier" not in u)
        ):
            alog(attempt, "recovery: Auth0 login form detected, filling credentials")
            await fill_input(page, ['input[name="username"]', 'input[name="email"]', 'input[type="email"]'], email_addr)
            await asyncio.sleep(0.3)
            await click_text_button(page, ["Continue", "Next", "Submit"])
            await asyncio.sleep(1.5)
            await fill_input(page, ['input[name="password"]', 'input[type="password"]'], password)
            await asyncio.sleep(0.3)
            await click_text_button(page, ["Continue", "Log in", "Sign in", "Submit"])
            bearer_bag["_login_filled"] = True
            alog(attempt, "recovery: login form submitted")
            await asyncio.sleep(3.0)
            continue
        # SPA sometimes stores tokens without us seeing /oauth/token if cached session
        if (
            "enter.converge.ai" in u
            and "error=" not in u
            and "risk_control" not in u
            and "auth.converge" not in u
            and "code=" not in u
        ):
            # give SPA a bit more time to hit token endpoint or make API calls
            await asyncio.sleep(2.0)
            if token_bag["tokens"]:
                page.remove_listener("request", _on_request)
                return _normalize_token_response(token_bag["tokens"])
            if bearer_bag.get("access_token"):
                alog(attempt, "tokens snarfed from outgoing API request Bearer header")
                page.remove_listener("request", _on_request)
                return _normalize_token_response({
                    "access_token": bearer_bag["access_token"],
                    "refresh_token": "",
                    "expires_in": 86400,
                    "token_type": "Bearer",
                    "scope": SCOPE,
                })
            # try reading access_token from localStorage / sessionStorage
            tok = await _tokens_from_storage(page)
            if tok:
                alog(attempt, f"tokens from storage")
                page.remove_listener("request", _on_request)
                return tok
            # Trigger SPA API call to flush Bearer from memory
            await page.evaluate("fetch('/api/user/me',{credentials:'include'}).catch(()=>{})")
            await asyncio.sleep(1.0)
            if bearer_bag.get("access_token"):
                alog(attempt, "tokens snarfed after triggered /api/user/me")
                page.remove_listener("request", _on_request)
                return _normalize_token_response({
                    "access_token": bearer_bag["access_token"],
                    "refresh_token": "",
                    "expires_in": 86400,
                    "token_type": "Bearer",
                    "scope": SCOPE,
                })
        await asyncio.sleep(0.4)

    page.remove_listener("request", _on_request)
    if token_bag["tokens"]:
        return _normalize_token_response(token_bag["tokens"])
    if bearer_bag.get("access_token"):
        return _normalize_token_response({
            "access_token": bearer_bag["access_token"],
            "refresh_token": "",
            "expires_in": 86400,
            "token_type": "Bearer",
            "scope": SCOPE,
        })
    tok = await _tokens_from_storage(page)
    if tok:
        return tok
    await screenshot(page, attempt, "snarf_timeout")
    return None


async def _tokens_from_storage(page) -> dict | None:
    """Best-effort pull SPA tokens from web storage (Auth0 SPA SDK patterns)."""
    try:
        raw = await page.evaluate(
            """() => {
                const out = {};
                for (const store of [localStorage, sessionStorage]) {
                  for (let i = 0; i < store.length; i++) {
                    const k = store.key(i);
                    const v = store.getItem(k);
                    if (!v) continue;
                    if (/access_token|id_token|refresh_token|@@auth0|auth0spa/i.test(k+v)
                        || (v.includes('access_token') && v.includes('eyJ'))) {
                      out[k] = v;
                    }
                  }
                }
                return out;
            }"""
        )
    except Exception:
        return None
    if not raw:
        return None
    access = refresh = id_token = ""
    expires_in = 86400
    for _k, v in raw.items():
        try:
            data = json.loads(v) if isinstance(v, str) and v.startswith("{") else None
        except Exception:
            data = None
        if isinstance(data, dict):
            # nested body
            body = data.get("body") if isinstance(data.get("body"), dict) else data
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    body = data
            if isinstance(body, dict):
                access = access or body.get("access_token") or body.get("accessToken") or ""
                refresh = refresh or body.get("refresh_token") or body.get("refreshToken") or ""
                id_token = id_token or body.get("id_token") or body.get("idToken") or ""
                if body.get("expires_in"):
                    expires_in = int(body["expires_in"])
            # auth0 cache shape: { body: { access_token... } } already handled
            if not access:
                for vv in data.values():
                    if isinstance(vv, dict) and vv.get("access_token"):
                        access = vv.get("access_token") or access
                        refresh = vv.get("refresh_token") or refresh
                        id_token = vv.get("id_token") or id_token
        elif isinstance(v, str) and v.startswith("eyJ") and not access:
            access = v
    if not access:
        return None
    return _normalize_token_response(
        {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "expires_in": expires_in,
            "token_type": "Bearer",
            "scope": SCOPE,
        }
    )


async def _dismiss_app_modals(page) -> None:
    """Cookie / Free Credits popups on enter.converge.ai marketing page."""
    for sel in (
        'button:has-text("Accept")',
        'button:has-text("Accept all")',
        'button:has-text("Got it")',
        'button:has-text("OK")',
        '[aria-label="Close"]',
        '[aria-label="close"]',
        'button[aria-label*="close" i]',
        # modal X near Free Credits
        'div[role="dialog"] button',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                txt = ""
                try:
                    txt = (await loc.inner_text()).strip().lower()
                except Exception:
                    pass
                # don't click primary CTAs by accident
                if txt in ("get free credits", "sign in", "claim discount"):
                    continue
                await loc.click(timeout=1500)
                await asyncio.sleep(0.25)
        except Exception:
            pass


async def _start_authorize(page, challenge: str, email_addr: str, attempt: int, *, prompt: str | None = "login") -> None:
    """Navigate to Auth0 /authorize with OUR PKCE (must match token exchange verifier)."""
    state = secrets.token_urlsafe(24)
    rs_id = _get_risk_session_id()
    params = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "audience": AUDIENCE,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "login_hint": email_addr,
        "auth0Client": "eyJuYW1lIjoiYXV0aDAtcmVhY3QiLCJ2ZXJzaW9uIjoiMi4xMC4wIn0=",
    }
    if prompt:
        params["prompt"] = prompt
    if rs_id:
        params["risk_session_id"] = rs_id
    auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    alog(attempt, f"authorize prompt={prompt or 'none'}...")
    await goto_with_retry(
        page,
        auth_url,
        attempt,
        label=f"authorize_prompt_{prompt or 'none'}",
        warp_on_fail=False,
    )
    await asyncio.sleep(1.2)


async def _wait_app_session(page, attempt: int, timeout: float = 45.0) -> bool:
    """True when we land on enter.converge.ai app without risk/error (dashboard/session)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        u = page.url
        if "auth.converge.ai" in u:
            await asyncio.sleep(0.4)
            continue
        if "enter.converge.ai" in u:
            if "risk_control_blocked" in u or "error=access_denied" in u:
                return False
            # SPA may use / or /code or app routes with cookies set
            if "error=" not in u:
                alog(attempt, f"app session url={u[:120]}")
                return True
        await asyncio.sleep(0.35)
    return "enter.converge.ai" in page.url and "error=" not in page.url


async def _silent_authorize_code(
    page,
    challenge: str,
    email_addr: str,
    attempt: int,
    timeout: float = 45.0,
) -> str | None:
    """After Auth0 session exists: authorize with OUR PKCE (no login form)."""
    # Drop any SPA-captured code — wrong PKCE verifier.
    await _start_authorize(page, challenge, email_addr, attempt, prompt=None)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = extract_code_from_url(page.url)
        if code:
            alog(attempt, f"silent authorize code ok")
            return code
        # SSO bounce sometimes lands on app without code in URL — retry once with prompt=none
        if "enter.converge.ai" in page.url and "code=" not in page.url and "auth.converge" not in page.url:
            if time.monotonic() + 15 < deadline:
                alog(attempt, f"silent authorize retry prompt=none")
                await _start_authorize(page, challenge, email_addr, attempt, prompt="none")
        if "risk_control_blocked" in page.url:
            return None
        # login form appeared = no session
        if "/u/login" in page.url or "/u/signup" in page.url:
            return None
        await asyncio.sleep(0.35)
    return extract_code_from_url(page.url)


async def _login_and_capture_code(
    page,
    email_addr: str,
    password: str,
    attempt: int,
    verifier: str,
    challenge: str,
    auth_code: dict,
) -> str | None:
    """Risk-blocked after signup.

    Manual path (user): Sign in -> dashboard (session already created by signup).
    Automation must NOT force prompt=login (that re-opens login form).

    Flow:
      1) Click Sign in (or Get Free Credits)
      2) Wait for dashboard / app session
      3) Silent /authorize with OUR PKCE -> code matching verifier
      4) Only if no session: fall back to password login, then silent authorize again

    Never exchange SPA-generated codes (wrong code_verifier -> HTTP 403).
    """
    # Invalidate SPA codes captured by framenavigated during Sign-in click
    auth_code["code"] = None

    await _dismiss_app_modals(page)

    signed = await click_text_button(
        page, ["Sign in", "Sign In", "Log in", "Log In", "Get Free Credits"]
    )
    if signed:
        alog(attempt, f"recovery UI clicked: {signed}")
    await asyncio.sleep(1.5)
    await _dismiss_app_modals(page)

    # Wait for dashboard like manual Sign in (do NOT authorize yet)
    session_ok = await _wait_app_session(page, attempt, timeout=40)
    # Clear any SPA ?code= that may have been set on URL / listener
    spa_code = extract_code_from_url(page.url)
    if spa_code:
        alog(attempt, f"ignoring SPA oauth code (wrong PKCE)")
    auth_code["code"] = None

    if session_ok:
        alog(attempt, f"session after Sign in - silent authorize with our PKCE")
        code = await _silent_authorize_code(page, challenge, email_addr, attempt)
        if code:
            return code
        await screenshot(page, attempt, "silent_auth_fail")

    # Fallback: only if still on Auth0 login form (no session)
    if "/u/login" not in page.url and "/u/signup" not in page.url and "auth.converge" not in page.url:
        # try Sign in once more then silent authorize
        await _dismiss_app_modals(page)
        await click_text_button(page, ["Sign in", "Sign In"])
        await asyncio.sleep(1.2)
        if await _wait_app_session(page, attempt, timeout=20):
            auth_code["code"] = None
            code = await _silent_authorize_code(page, challenge, email_addr, attempt)
            if code:
                return code

    if "auth.converge.ai" not in page.url:
        await _start_authorize(page, challenge, email_addr, attempt, prompt="login")

    if "signup" in page.url:
        try:
            login_link = page.locator(
                "a:has-text('Log in'), a:has-text('Log In'), a:has-text('Sign in')"
            ).first
            if await login_link.count() > 0:
                await login_link.click()
                await asyncio.sleep(1.0)
        except Exception:
            pass

    await screenshot(page, attempt, "recovery_login_fallback")
    alog(attempt, f"fallback password login (no SSO session)")

    await fill_input(
        page,
        [
            'input[name="username"]',
            'input[name="email"]',
            'input[type="email"]',
            'input[inputmode="email"]',
        ],
        email_addr,
    )
    await handle_turnstile(
        page, attempt, max_wait=20, require_token=False, use_global_limit=True, allow_remount=False
    )
    await click_text_button(page, ["Continue", "Next", "Log in", "Sign in"])
    try:
        await page.locator('button[type="submit"]').first.click(timeout=3000)
    except Exception:
        pass
    await asyncio.sleep(1.2)

    if "password" in page.url or await page.locator('input[type="password"]').count() > 0:
        ok = await fill_input(
            page,
            [
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]',
            ],
            password,
        )
        if not ok:
            await screenshot(page, attempt, "recovery_password_fail")
            raise RuntimeError("recovery: could not fill login password")
        await handle_turnstile(
            page,
            attempt,
            max_wait=8,
            require_token=False,
            password=password,
            use_global_limit=True,
            allow_remount=False,
        )
        await click_text_button(page, ["Continue", "Log in", "Sign in", "Submit"])
        try:
            await page.locator('button[type="submit"]').first.click(timeout=3000)
        except Exception:
            pass
        alog(attempt, f"recovery password submitted")
        await asyncio.sleep(1.5)

    # After password login, SPA may redirect with ITS code — ignore, silent authorize ours
    auth_code["code"] = None
    if await _wait_app_session(page, attempt, timeout=30) or "enter.converge.ai" in page.url:
        code = await _silent_authorize_code(page, challenge, email_addr, attempt)
        if code:
            return code

    await screenshot(page, attempt, "recovery_no_code")
    return None


# ── Persist ──────────────────────────────────────────────────────────────────
def _extract_api_key_fields(result: dict) -> dict[str, str]:
    """Normalize api key fields from result shapes used by farmer."""
    # preferred: top-level flattened by _do_register_body
    top = result.get("api_key") if isinstance(result.get("api_key"), dict) else {}
    # fallback: raw API response under enter.api_key.data
    enter_ak = ((result.get("enter") or {}).get("api_key") or {})
    data = enter_ak.get("data") if isinstance(enter_ak.get("data"), dict) else {}
    if not data and isinstance(enter_ak, dict) and enter_ak.get("key"):
        data = enter_ak
    key = (top.get("key") or data.get("key") or "").strip()
    kid = (top.get("id") or data.get("id") or "").strip()
    name = (top.get("name") or data.get("name") or API_KEY_NAME or "").strip()
    scope = (top.get("scope") or data.get("scope") or API_KEY_SCOPE or "").strip()
    ws = str(
        result.get("workspace_id")
        or (result.get("enter") or {}).get("workspace_id")
        or ""
    ).strip()
    return {
        "key": key,
        "id": kid,
        "name": name,
        "scope": scope,
        "workspace_id": ws,
        "email": str(result.get("email") or "").strip(),
        "password": str(result.get("password") or "").strip(),
        "created_at": str(result.get("created_at") or "").strip(),
    }


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")


def inject_to_9router(api_key: str, workspace_id: str, email: str = "", name: str = "",
                      access_token: str = "", refresh_token: str = "", expires_at: str = "") -> tuple[bool, str]:
    """Inject farmed ek_ + JWT into 9router SQLite DB (grok-farm style — no HTTP/auth).

    Writes providerConnections row for enter-converge.
    Stores both apiKey (ek_) for /chat/completions AND accessToken (JWT) for project-chat models.
    Returns (ok, detail). Skips when disabled / DB missing / duplicate key.
    """
    import sqlite3
    import uuid as _uuid

    if not NINEROUTER_INJECT:
        return False, "inject disabled"
    if not api_key:
        return False, "no api key"

    db_path = Path(NINEROUTER_DB)
    if not db_path.is_file():
        return False, f"DB not found: {db_path} (start 9router once to create it)"

    email_l = (email or "").strip().lower()
    base = (name or "").strip()
    if not base and email_l:
        base = email_l.split("@")[0][:32]
    if not base:
        base = f"farm-{api_key[-8:]}"
    conn_name = base
    ws = str(workspace_id or "").strip()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        cur = conn.cursor()

        # Dedup: same provider + same apiKey already in data JSON
        cur.execute(
            "SELECT id, data FROM providerConnections WHERE provider = ?",
            (NINEROUTER_PROVIDER,),
        )
        for row_id, data_json in cur.fetchall():
            try:
                d = json.loads(data_json or "{}")
            except Exception:
                d = {}
            if (d.get("apiKey") or "") == api_key:
                # refresh workspaceId if missing + update JWT tokens
                psd = d.get("providerSpecificData") or {}
                updated = False
                if ws and not psd.get("workspaceId"):
                    psd["workspaceId"] = ws
                    d["providerSpecificData"] = psd
                    updated = True
                # Always refresh JWT if provided (tokens expire 24h)
                if access_token:
                    d["accessToken"] = access_token
                    updated = True
                if refresh_token:
                    d["refreshToken"] = refresh_token
                    updated = True
                if expires_at:
                    d["expiresAt"] = expires_at
                    updated = True
                if updated:
                    d["testStatus"] = d.get("testStatus") or "active"
                    cur.execute(
                        "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?",
                        (json.dumps(d, ensure_ascii=False), now, row_id),
                    )
                    conn.commit()
                    conn.close()
                    return True, f"updated existing id={row_id} name={conn_name}"
                conn.close()
                return True, f"skip duplicate key id={row_id}"

        row_id = f"enter-farm-{_uuid.uuid4().hex[:16]}"
        data_obj: dict[str, Any] = {
            "displayName": email_l or conn_name,
            "apiKey": api_key,
            "testStatus": "active",
            "providerSpecificData": {},
            "lastError": None,
            "lastErrorAt": None,
        }
        # JWT for project-chat models (opus 4.8, sonnet 5, gemini etc)
        if access_token:
            data_obj["accessToken"] = access_token
        if refresh_token:
            data_obj["refreshToken"] = refresh_token
        if expires_at:
            data_obj["expiresAt"] = expires_at
        if ws:
            data_obj["providerSpecificData"]["workspaceId"] = ws
        if email_l:
            data_obj["providerSpecificData"]["email"] = email_l

        cur.execute(
            "INSERT INTO providerConnections "
            "(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                NINEROUTER_PROVIDER,
                "apikey",
                conn_name,
                email_l or None,
                NINEROUTER_PRIORITY,
                1,
                json.dumps(data_obj, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return True, f"inserted id={row_id} name={conn_name} ws={ws or '-'}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def save_result_to_file(result: dict) -> None:
    """Save successful account JSON + dedicated credential/apikey txt files."""
    async with _results_lock:
        rows = []
        if RESULTS_JSON.is_file():
            try:
                rows = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(result)
        RESULTS_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

        fields = _extract_api_key_fields(result)
        email = fields["email"]
        password = fields["password"]
        api_key = fields["key"]
        key_id = fields["id"]
        ws = fields["workspace_id"]
        key_name = fields["name"]
        created = fields["created_at"] or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # legacy tab-separated batch accounts.txt
        _append_line(
            RESULTS_TXT,
            f"{email}\t{password}\t{api_key}\t{key_id}\t{ws}",
        )

        # human-friendly credentials (batch)
        # email|password|api_key|workspace_id|key_id|key_name|created_at
        cred_line = "|".join(
            [
                email,
                password,
                api_key,
                ws,
                key_id,
                key_name,
                created,
            ]
        )
        _append_line(CREDS_TXT, cred_line)

        # api keys only (batch) — one ek_… per line
        if api_key:
            _append_line(CREDS_KEYS_TXT, api_key)

        # global append-only (all successful farms across runs)
        global_cred = "|".join(
            [
                email,
                password,
                api_key,
                ws,
                key_id,
                key_name,
                created,
                BATCH_ID,
            ]
        )
        _append_line(GLOBAL_CREDS_TXT, global_cred)
        if api_key:
            _append_line(GLOBAL_KEYS_TXT, f"{api_key}\t{email}\t{ws}\t{BATCH_ID}")

        # optional: email:password for simple import tools
        simple = BATCH_DIR / "email_password.txt"
        _append_line(simple, f"{email}:{password}")

        # full single-line dump for copy-paste
        # email ---- password ---- api_key
        pretty = BATCH_DIR / "credentials_pretty.txt"
        _append_line(
            pretty,
            f"{email} | {password} | {api_key} | ws={ws} | key_id={key_id}",
        )

        with (BATCH_DIR / "farm.log").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "ok": True,
                        "email": email,
                        "workspace_id": ws,
                        "api_key_id": key_id,
                        "has_api_key": bool(api_key),
                    }
                )
                + "\n"
            )
        slog("SAVE", f"credentials -> {CREDS_TXT.name}, apikeys -> {CREDS_KEYS_TXT.name}, global -> {GLOBAL_CREDS_TXT.name}")

        # Auto-inject into 9router Enter Converge pool (optional)
        if api_key and NINEROUTER_INJECT:
            # Pass JWT tokens for project-chat models (opus 4.8, sonnet 5, gemini)
            tokens = result.get("tokens") or {}
            ok_inj, detail = inject_to_9router(
                api_key, ws, email=email, name="",
                access_token=tokens.get("access_token", ""),
                refresh_token=tokens.get("refresh_token", ""),
                expires_at=tokens.get("expires_at", ""),
            )
            if ok_inj:
                slog("9ROUTER", f"injected {email} ws={ws} -> {detail}")
            else:
                slog("9ROUTER", f"inject failed {email}: {detail}")

        # Auto-push to remote 9router VPS (batched every N)
        if api_key and _vps_pusher is not None:
            tokens = result.get("tokens") or {}
            data_obj = {
                "displayName": email,
                "apiKey": api_key,
                "testStatus": "active",
                "providerSpecificData": {"workspaceId": ws, "email": email},
                "lastError": None,
                "lastErrorAt": None,
            }
            if tokens.get("access_token"):
                data_obj["accessToken"] = tokens["access_token"]
            if tokens.get("refresh_token"):
                data_obj["refreshToken"] = tokens["refresh_token"]
            if tokens.get("expires_at"):
                data_obj["expiresAt"] = tokens["expires_at"]
            from core.ninerouter import make_credential
            cred = make_credential(NINEROUTER_PROVIDER, email, data_obj)
            _vps_pusher.queue(cred)


async def save_failed_to_file(attempt: int, email: str, err: str) -> None:
    async with _results_lock:
        rows = []
        if FAILED_JSON.is_file():
            try:
                rows = json.loads(FAILED_JSON.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(
            {
                "attempt": attempt,
                "email": email,
                "error": err,
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        FAILED_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        with (BATCH_DIR / "farm.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "ok": False, "email": email, "error": err}) + "\n")


# ── Register one ─────────────────────────────────────────────────────────────
async def _do_register_body(attempt: int, email_addr: str, password: str, proxy_url: str | None, proxy_id: str) -> dict:
    manager = None
    try:
        if AUTH_MODE != "browser":
            raise RuntimeError("ENTER_AUTH_MODE must be browser")

        manager, browser, page = await launch_browser(proxy_url)
        _plog = "direct"
        if proxy_url:
            try:
                _u = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
                _plog = f"{_u.scheme}://{_u.hostname}:{_u.port or ''}"
            except Exception:
                _plog = proxy_url[:40]
        alog(attempt, f"browser proxy={_plog}")

        tokens = await do_signup_and_oauth(page, email_addr, password, attempt)
        enter_meta = enter_post_auth_setup(tokens["access_token"], GIFT_CODE)
        api_data = (enter_meta.get("api_key") or {}).get("data") or {}
        alog(attempt, "API key created")
        return {
            "email": email_addr,
            "password": password,
            "gift_code": GIFT_CODE,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempt": attempt,
            "proxy": proxy_url or "direct",
            "workspace_id": enter_meta.get("workspace_id"),
            "tokens": {
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": tokens.get("expires_at"),
                "expires_in": tokens.get("expires_in"),
                "scope": tokens.get("scope"),
            },
            "api_key": {
                "id": api_data.get("id"),
                "name": api_data.get("name"),
                "key": api_data.get("key"),
                "scope": api_data.get("scope"),
                "reveal_policy": api_data.get("reveal_policy"),
            },
            "enter": enter_meta,
        }
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


async def register_one_account(
    attempt: int, slot_q: asyncio.Queue
) -> dict | None:
    """Take a free worker slot from slot_q (size = concurrent). No hard max on -c."""
    global _in_flight
    worker_slot = await slot_q.get()
    email_addr = ""
    nav_fail = False
    if_lock, can_start = _ensure_async_gates()
    await can_start.wait()
    async with if_lock:
        _in_flight += 1
    try:
        await _wait_rate_limit_window(attempt)
        password = ACCOUNT_PASSWORD
        proxy_url, proxy_id = await next_proxy()
        domain_tries = GPTMAIL_DOMAIN_RETRIES if EMAIL_MODE in ("gptmail", "generator", "exzork", "emailqu", "rotate") else 1
        last_msg = ""
        for dom_try in range(1, domain_tries + 1):
            email_addr = await generate_email(worker_slot=worker_slot)
            emit_progress(
                attempt,
                "START",
                f"slot={worker_slot} domain_try={dom_try}/{domain_tries}",
                email_addr,
            )
            try:
                result = await asyncio.wait_for(
                    _do_register_body(attempt, email_addr, password, proxy_url, proxy_id),
                    timeout=ACCOUNT_TIMEOUT_S,
                )
                await save_result_to_file(result)
                _domain_fail_reset(email_addr)
                emit_success(attempt, email_addr, "ok")
                await _maybe_warp_after_success(attempt)
                return result
            except asyncio.TimeoutError:
                msg = f"account timeout after {ACCOUNT_TIMEOUT_S}s"
                emit_failed(attempt, msg)
                await save_failed_to_file(attempt, email_addr, msg)
                return None
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                last_msg = msg
                low = msg.lower()
                nav_fail = (
                    "landing failed" in low
                    or "authorize failed" in low
                    or ("goto" in low and "timeout" in low)
                    or "nav timeout" in low
                    or "still on blank" in low
                )
                # Track per-domain fails (auto-blacklist after threshold)
                if not nav_fail:
                    _domain_fail_track(email_addr, msg[:120], worker_slot=worker_slot)
                is_domain_block = EMAIL_MODE in ("gptmail", "generator", "exzork", "emailqu", "rotate") and (
                    "domain_not_allowed" in low
                    or "domain is not allowed" in low
                    or "email domain is not allowed" in low
                    or "not allowed to sign up" in low
                    or "email provider is not allowed" in low
                    or ("domain" in low and "not allowed" in low)
                )
                if is_domain_block:
                    dom = email_addr.split("@")[-1] if "@" in email_addr else ""
                    gptmail_block_domain(dom, reason=msg[:120], worker_slot=worker_slot)
                    if dom_try < domain_tries:
                        alog(
                            attempt,
                            f"domain blocked → retry {dom_try + 1}/{domain_tries} with a new mailbox",
                        )
                        continue
                    emit_failed(attempt, f"domain blocked after {domain_tries} tries: {msg[:160]}")
                    await save_failed_to_file(attempt, email_addr, msg)
                    return None
                if (
                    "too many" in low
                    or "rate limit" in low
                    or "try again later" in low
                    or "signup attempts" in low
                ):
                    await _trip_rate_limit(attempt, msg[:160])
                emit_failed(attempt, msg)
                await save_failed_to_file(attempt, email_addr, msg)
                return None
        emit_failed(attempt, last_msg or "domain retries exhausted")
        await save_failed_to_file(attempt, email_addr, last_msg or "domain retries exhausted")
        return None
    finally:
        # Stagger workers; short gap after pure landing/nav fail (don't burn 45s)
        if ACCOUNT_GAP > 0:
            if nav_fail:
                gap = 6.0 + random.uniform(0, 4.0)
            else:
                gap = ACCOUNT_GAP + random.uniform(0, min(8.0, ACCOUNT_GAP * 0.3))
            alog(attempt, f"account gap {gap:.0f}s before releasing slot")
            await asyncio.sleep(gap)
        # return lane so next account on this slot keeps sticky domain
        async with if_lock:
            _in_flight = max(0, _in_flight - 1)
        slot_q.put_nowait(worker_slot)


# ── CLI ──────────────────────────────────────────────────────────────────────
def _prompt_int(label: str, default: int) -> int:
    try:
        raw = input(f"  {label} [{default}]: ").strip()
        return int(raw) if raw else default
    except Exception:
        return default


def _prompt_yes_no(label: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    raw = input(f"  {label} [{d}]: ").strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes", "1")


async def main() -> None:
    import argparse

    global HEADLESS, SPAWN_DELAY, ACCOUNT_GAP, MAX_ACCOUNTS, CONCURRENT

    ap = argparse.ArgumentParser(description="Enter/Converge farmer (grok-farm style)")
    ap.add_argument("-n", "--count", type=int, default=None)
    ap.add_argument("-c", "--concurrent", type=int, default=None)
    ap.add_argument("-y", "--yes", action="store_true")
    ap.add_argument("--headless", action="store_true", help="Force headless browser")
    ap.add_argument("--headed", action="store_true", help="Force headed browser")
    ap.add_argument("--spawn-delay", type=float, default=None, help="Seconds between spawning workers")
    ap.add_argument("--account-gap", type=float, default=None, help="Seconds after each account before freeing slot")
    args = ap.parse_args()

    if args.headless:
        HEADLESS = True
    if args.headed:
        HEADLESS = False
    if args.spawn_delay is not None:
        SPAWN_DELAY = max(0.0, args.spawn_delay)
    if args.account_gap is not None:
        ACCOUNT_GAP = max(0.0, args.account_gap)

    if EMAIL_MODE == "gptmail":
        print(
            f"[CFG] email_mode=gptmail api={GPTMAIL_API} "
            f"domain_pin={GPTMAIL_DOMAIN or '-'} prefix={GPTMAIL_PREFIX or '-'}",
            flush=True,
        )
        try:
            await asyncio.get_event_loop().run_in_executor(None, _gptmail_load_domains)
            await asyncio.get_event_loop().run_in_executor(None, _load_blocked_domains)
        except Exception as e:
            print(f"ERROR: gptmail domains unreachable: {e}", flush=True)
            sys.exit(1)
    elif EMAIL_MODE == "rotate":
        providers = _rotation_candidates()
        if not providers:
            print("ERROR: ENTER_TEMPMAIL_ROTATION has no usable providers", flush=True)
            sys.exit(1)
        print(f"[CFG] email_mode=rotate providers={','.join(providers)}", flush=True)
        _load_blocked_domains()
    elif EMAIL_MODE == "generator":
        print("[CFG] email_mode=generator api=https://generator.email", flush=True)
    elif EMAIL_MODE == "tempmail":
        slog("CFG", f"email_mode=tempmail provider={TEMPMAIL_PROVIDER} api={TEMPMAIL_API}")
    else:
        if not IMAP_USER or not IMAP_PASS:
            print(
                "ERROR: set ENTER_IMAP_USER + ENTER_IMAP_PASS in .env "
                "(or ENTER_EMAIL_MODE=gptmail|tempmail)",
                flush=True,
            )
            sys.exit(1)
        if EMAIL_MODE == "domain" and not EMAIL_DOMAIN:
            print("ERROR: set ENTER_EMAIL_DOMAIN for domain mode", flush=True)
            sys.exit(1)
    # WARP connect/pre-rotate: hub runner (python -m jobs run --warp-*)
    slog(
        "CFG",
        f"WARP every_n={_effective_warp_every_n() or 'off'} "
        f"(raw env={WARP_EVERY_N}) rate_limit_rotate={WARP_ON_RATE_LIMIT}",
    )

    if not GIFT_CODE:
        print("ERROR: set ENTER_GIFT_CODE (referral gift)", flush=True)
        sys.exit(1)

    n = args.count if args.count is not None else MAX_ACCOUNTS
    c = args.concurrent if args.concurrent is not None else CONCURRENT
    if not args.yes and sys.stdin.isatty():
        n = _prompt_int("Berapa akun yang mau di-farm?", n)
        c = _prompt_int("Concurrency (browser paralel)?", c)
        if not _prompt_yes_no(f"Mulai farm {n} akun × concurrent {c}?", True):
            print("Cancelled.", flush=True)
            return

    n = max(1, n)
    c = max(1, min(c, n))
    CONCURRENT = c
    MAX_ACCOUNTS = n
    init_batch(n, c)
    # Init VPS pusher (remote 9router)
    global _vps_pusher
    if NINEROUTER_VPS_EVERY_N > 0:
        from core.ninerouter import NinerouterPusher
        _vps_pusher = NinerouterPusher(provider=NINEROUTER_PROVIDER, every_n=NINEROUTER_VPS_EVERY_N)
        slog("9ROUTER", f"VPS push enabled: every_n={NINEROUTER_VPS_EVERY_N} host={_vps_pusher.host}")
    HUD.start(n, BATCH_ID, str(BATCH_DIR), gift=GIFT_CODE)
    _dom_label = EMAIL_DOMAIN or GMAIL_BASE
    if EMAIL_MODE == "gptmail":
        _dom_label = GPTMAIL_DOMAIN or "auto"
    elif EMAIL_MODE == "rotate":
        _dom_label = "rotate/" + ",".join(_rotation_candidates())
    elif EMAIL_MODE == "generator":
        _dom_label = "generator.email/auto"
    elif EMAIL_MODE == "tempmail":
        _dom_label = TEMPMAIL_API
    slog(
        "CFG",
        f"mode={EMAIL_MODE} domain={_dom_label} "
        f"headless={HEADLESS} gift={GIFT_CODE} proxies={len(_proxy_pool)} "
        f"spawn_delay={SPAWN_DELAY}s account_gap={ACCOUNT_GAP}s "
        f"rate_cooldown={RATE_LIMIT_COOLDOWN}s concurrent={c} ui={UI_MODE}",
    )
    if not _proxy_pool:
        slog("WARN", "no proxies loaded - concurrent signup from one IP often hits rate limits. Put proxies in proxies.txt")

    # Solve Turnstile for gptmail session before workers start
    if EMAIL_MODE == "gptmail":
        slog("GPTMAIL", "solving Turnstile for API session...")
        await asyncio.get_event_loop().run_in_executor(None, _gptmail_solve_turnstile)

    # Free worker lanes (0..c-1). User -c is the only limit — no hard max.
    slot_q: asyncio.Queue = asyncio.Queue()
    for s in range(c):
        slot_q.put_nowait(s)
    if EMAIL_MODE == "gptmail":
        print(
            f"[CFG] gptmail sticky domains: {c} worker slots "
            f"(prefix changes each account; domain rotates only on block)",
            flush=True,
        )

    tasks = []
    results = []
    tick = asyncio.create_task(HUD.ticker())
    try:
        wave_size = VPNX_EVERY_N if VPNX_API and VPNX_EVERY_N > 0 else n
        for wave_start in range(1, n + 1, wave_size):
            wave_end = min(n, wave_start + wave_size - 1)
            tasks = []
            for i in range(wave_start, wave_end + 1):
                tasks.append(asyncio.create_task(register_one_account(i, slot_q)))
                if SPAWN_DELAY > 0 and i < wave_end:
                    jitter = random.uniform(0, min(5.0, SPAWN_DELAY * 0.25))
                    await asyncio.sleep(SPAWN_DELAY + jitter)
            results.extend(await asyncio.gather(*tasks))
            if VPNX_API and wave_end < n:
                slog("VPNX", f"wave {wave_start}-{wave_end} complete → rotate")
                try:
                    await _vpnx_rotate_wave(wave_end)
                except Exception as e:
                    slog("VPNX", f"rotate error: {type(e).__name__}: {e}")
    finally:
        tick.cancel()
        try:
            await tick
        except asyncio.CancelledError:
            pass
        HUD.stop()
        HUD.close_log()
    ok = sum(1 for r in results if r)
    # Flush remaining VPS push queue
    if _vps_pusher is not None:
        _vps_pusher.flush()
        s = _vps_pusher.stats
        slog("9ROUTER", f"VPS push final: pushed={s['pushed']} failed={s['failed']}")
    slog("DONE", f"ok={ok}/{n} batch={BATCH_DIR}")
    if not HUD.enabled:
        print(f"[DONE] ok={ok}/{n} batch={BATCH_DIR}", flush=True)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(main())
