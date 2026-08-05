"""Batch-push reauth credentials directly to the VPS NvRouter SQLite DB.

Default: every 10 OK → SSH + node vps_upsert_credentials.js on VPS.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import threading
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent
UPSERT_SCRIPT_NAME = "vps_import_credentials.py"
DEFAULT_EVERY = 10


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def tokens_to_cred(email: str, tokens: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": email,
        "accessToken": tokens.get("access_token") or tokens.get("accessToken") or "",
        "refreshToken": tokens.get("refresh_token") or tokens.get("refreshToken") or "",
        "expiresAt": tokens.get("expires_at") or tokens.get("expiresAt") or "",
        "expiresIn": tokens.get("expires_in") or tokens.get("expiresIn") or 21600,
        "scope": tokens.get("scope") or "",
        "authMethod": tokens.get("auth_mode") or tokens.get("authMethod") or "device_oauth",
        "idToken": tokens.get("id_token") or tokens.get("idToken"),
    }


def delete_local_grok_emails(local_db: Path, emails: list[str]) -> int:
    """Remove grok-cli rows from local 9router DB by email (after successful VPS push)."""
    if not emails or not local_db.is_file():
        return 0
    # unique lower emails
    uniq = sorted({(e or "").strip().lower() for e in emails if (e or "").strip()})
    if not uniq:
        return 0
    conn = sqlite3.connect(str(local_db), timeout=30)
    try:
        cur = conn.cursor()
        deleted = 0
        for em in uniq:
            cur.execute(
                "DELETE FROM providerConnections WHERE provider = 'grok-cli' "
                "AND lower(email) = ?",
                (em,),
            )
            deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return deleted
    finally:
        conn.close()


class VpsBatchPusher:
    """Thread-safe queue: flush every N credentials via SSH.

    After a successful VPS upsert, optionally prune those emails from local DB
    so future reauth runs skip accounts already live on VPS.
    """

    def __init__(
        self,
        *,
        every: int = DEFAULT_EVERY,
        host: str = "",
        user: str = "",
        password: str = "",
        port: int = 22,
        remote_upsert: str = "",
        log: Callable[[str], None] | None = None,
        enabled: bool = True,
        prune_local: bool | None = None,
        local_db: Path | str | None = None,
    ) -> None:
        self.every = max(1, int(every))
        self.host = host or _env("VPS_HOST") or _env("GROK_VPS_HOST")
        self.user = user or _env("VPS_USER") or _env("GROK_VPS_USER") or "ubuntu"
        self.password = password or _env("VPS_PASS") or _env("GROK_VPS_PASS")
        self.port = port or int(_env("VPS_PORT") or _env("GROK_VPS_PORT") or "22" or "22")
        self.remote_upsert = remote_upsert or _env(
            "GROK_VPS_UPSERT",
            "/home/ubuntu/scripts/grok-refresh/vps_import_credentials.py",
        )
        self.log = log or (lambda m: print(m, flush=True))
        self.enabled = bool(enabled and self.host and self.user)
        # default ON when push enabled (user workflow: PC = queue only)
        self.prune_local = (
            _env_bool("GROK_VPS_PRUNE_LOCAL", True)
            if prune_local is None
            else bool(prune_local)
        )
        self.local_db = Path(
            local_db
            or _env("GROK_LOCAL_DB")
            or (Path(os.environ.get("APPDATA", "")) / "9router" / "db" / "data.sqlite")
        )
        self._q: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.pushed = 0
        self.flushes = 0
        self.errors = 0
        self.pruned = 0

        if not self.enabled:
            self.log(
                "[vps-push] OFF "
                "(set GROK_VPS_HOST + GROK_VPS_PASS, or --vps-push with env)"
            )
        else:
            self.log(
                f"[vps-push] ON host={self.user}@{self.host} every={self.every} "
                f"prune_local={self.prune_local} upsert={self.remote_upsert}"
            )

    def add(self, email: str, tokens: dict[str, Any]) -> None:
        if not self.enabled:
            return
        cred = tokens_to_cred(email, tokens)
        if not cred["accessToken"] or not cred["refreshToken"]:
            self.log(f"[vps-push] skip incomplete tokens for {email}")
            return
        flush_payload: list[dict[str, Any]] | None = None
        with self._lock:
            self._q.append(cred)
            if len(self._q) >= self.every:
                flush_payload = list(self._q)
                self._q.clear()
        if flush_payload:
            self._flush(flush_payload, reason=f"batch/{self.every}")

    def flush_remaining(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            payload = list(self._q)
            self._q.clear()
        if payload:
            self._flush(payload, reason="final")

    def _flush(self, creds: list[dict[str, Any]], reason: str) -> None:
        n = len(creds)
        self.log(f"[vps-push] flush {n} credential(s) ({reason}) → {self.host}")
        try:
            result = self._ssh_upsert(creds)
            self.flushes += 1
            self.pushed += n
            self.log(f"[vps-push] OK {result}")
            # Only prune local AFTER successful VPS upsert
            if self.prune_local:
                emails = [c.get("email") or "" for c in creds]
                try:
                    deleted = delete_local_grok_emails(self.local_db, emails)
                    self.pruned += deleted
                    self.log(
                        f"[vps-push] pruned local DB rows={deleted} "
                        f"(emails={len([e for e in emails if e])})"
                    )
                except Exception as e:
                    self.log(f"[vps-push] WARN local prune failed: {e}")
        except Exception as e:
            self.errors += 1
            self.log(f"[vps-push] FAIL ({n} creds): {e}")
            # Do NOT prune local on failure — keep for retry / reauth
            try:
                out = _ROOT / "results" / f"vps_push_failed_{int(time.time())}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps({"credentials": creds}, indent=2),
                    encoding="utf-8",
                )
                self.log(f"[vps-push] saved failed batch → {out}")
            except Exception as e2:
                self.log(f"[vps-push] also failed to save batch: {e2}")

    def _ssh_upsert(self, creds: list[dict[str, Any]]) -> str:
        try:
            import paramiko
        except ImportError as e:
            raise RuntimeError("paramiko required for VPS push (pip install paramiko)") from e

        payload = json.dumps({"credentials": creds}, ensure_ascii=False)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kw: dict[str, Any] = {
            "hostname": self.host,
            "username": self.user,
            "port": self.port,
            "timeout": 45,
            "allow_agent": False,
            "look_for_keys": bool(_env("GROK_VPS_KEY") or _env("VPS_KEY")),
        }
        key_path = _env("GROK_VPS_KEY") or _env("VPS_KEY")
        if key_path:
            connect_kw["key_filename"] = key_path
        elif self.password:
            connect_kw["password"] = self.password
            connect_kw["look_for_keys"] = False
        else:
            raise RuntimeError("set GROK_VPS_PASS or GROK_VPS_KEY")

        client.connect(**connect_kw)
        try:
            # Ensure remote script exists (best-effort upload if missing)
            sftp = client.open_sftp()
            try:
                sftp.stat(self.remote_upsert)
            except OSError:
                local = _ROOT / UPSERT_SCRIPT_NAME
                if local.is_file():
                    remote_dir = os.path.dirname(self.remote_upsert).replace("\\", "/")
                    try:
                        client.exec_command(f"mkdir -p {remote_dir}", timeout=15)
                    except Exception:
                        pass
                    sftp.put(str(local), self.remote_upsert)
                    self.log(f"[vps-push] uploaded {UPSERT_SCRIPT_NAME} to VPS")
            finally:
                sftp.close()

            cmd = f"/usr/bin/python3 {self.remote_upsert}"
            stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
            stdin.write(payload)
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            code = stdout.channel.recv_exit_status()
            if code != 0:
                raise RuntimeError(f"remote exit {code}: {err or out}")
            return out or "ok"
        finally:
            client.close()


