"""Global hub .env — shared secrets + map to job prefixes (GROK_*, ENTER_*, OUTLOOK_*).

Load order for a job subprocess:
  1) process env (already set)
  2) hub .env raw keys
  3) hub shared keys expanded to job prefix (IMAP_USER → GROK_IMAP_USER)
  4) farm-local .env only fills gaps (setdefault)

Shared keys (no prefix) reuse across farms:
  IMAP_USER, IMAP_PASS, IMAP_HOST, IMAP_PORT
  EMAIL_MODE, EMAIL_DOMAIN, GMAIL_BASE
  ACCOUNT_PASSWORD, HEADLESS, MAX_ACCOUNTS, CONCURRENT, SPAWN_DELAY
  PROXY_FILE, PROXY_POOL, PROXY_SHUFFLE
  OTP_TIMEOUT, ACCOUNT_TIMEOUT, UI, VERBOSE
"""

from __future__ import annotations

import os
from pathlib import Path

_HUB = Path(__file__).resolve().parent.parent
HUB_ENV = _HUB / ".env"
HUB_ENV_EXAMPLE = _HUB / ".env.example"

# shared hub key → suffix after job prefix (GROK_ / ENTER_)
_SHARED_MAP: dict[str, str] = {
    "IMAP_USER": "IMAP_USER",
    "IMAP_PASS": "IMAP_PASS",
    "IMAP_HOST": "IMAP_HOST",
    "IMAP_PORT": "IMAP_PORT",
    "EMAIL_MODE": "EMAIL_MODE",
    "EMAIL_DOMAIN": "EMAIL_DOMAIN",
    "GMAIL_BASE": "GMAIL_BASE",
    "GPTMAIL_API": "GPTMAIL_API",
    "GPTMAIL_DOMAIN": "GPTMAIL_DOMAIN",
    "GPTMAIL_PREFIX": "GPTMAIL_PREFIX",
    "ACCOUNT_PASSWORD": "PASSWORD",
    "PASSWORD": "PASSWORD",
    "HEADLESS": "HEADLESS",
    "MAX_ACCOUNTS": "MAX_ACCOUNTS",
    "CONCURRENT": "CONCURRENT",
    "SPAWN_DELAY": "SPAWN_DELAY",
    "PROXY_FILE": "PROXY_FILE",
    "PROXY_POOL": "PROXY_POOL",
    "PROXY_SHUFFLE": "PROXY_SHUFFLE",
    "OTP_TIMEOUT": "OTP_TIMEOUT",
    "ACCOUNT_TIMEOUT": "ACCOUNT_TIMEOUT",
    "UI": "UI",
    "VERBOSE": "VERBOSE",
    "CAPTCHA_PROXY_URL": "CAPTCHA_PROXY_URL",
    "CAPTCHA_API_KEY": "CAPTCHA_API_KEY",
    "CAPTCHA_MODEL": "CAPTCHA_MODEL",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VAL dotenv (no export, no interpolation)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_hub_env(into: dict[str, str] | None = None) -> dict[str, str]:
    """Return env dict: os.environ copy + hub .env (file does not override existing)."""
    env = dict(into) if into is not None else dict(os.environ)
    for k, v in parse_env_file(HUB_ENV).items():
        env.setdefault(k, v)
    return env


def apply_job_prefix(env: dict[str, str], prefix: str) -> dict[str, str]:
    """Expand shared keys → PREFIX+suffix; keep explicit PREFIX* as-is.

    Explicit GROK_* in hub .env wins over shared mapping (setdefault order).
    """
    if not prefix:
        return env
    p = prefix if prefix.endswith("_") else prefix + "_"
    out = dict(env)

    # 1) shared → prefixed (only if target not set)
    for shared, suffix in _SHARED_MAP.items():
        if shared not in out or not str(out[shared]).strip():
            continue
        target = p + suffix
        out.setdefault(target, out[shared])

    # 2) any unprefixed key that already looks like farm-specific stays
    return out


def merge_farm_dotenv(env: dict[str, str], farm_cwd: Path) -> dict[str, str]:
    """Optional farm-local .env fills gaps only."""
    out = dict(env)
    for k, v in parse_env_file(farm_cwd / ".env").items():
        out.setdefault(k, v)
    return out


def build_job_env(prefix: str, farm_cwd: Path | None = None) -> dict[str, str]:
    """Full env for subprocess: hub global + prefix map + farm gaps."""
    env = load_hub_env()
    env = apply_job_prefix(env, prefix)
    if farm_cwd is not None:
        env = merge_farm_dotenv(env, farm_cwd)
    return env


def hub_python() -> Path:
    """Hub shared venv interpreter."""
    win = _HUB / ".venv" / "Scripts" / "python.exe"
    unix = _HUB / ".venv" / "bin" / "python"
    if win.is_file():
        return win
    if unix.is_file():
        return unix
    return Path(os.environ.get("PYTHON", "") or __import__("sys").executable)
