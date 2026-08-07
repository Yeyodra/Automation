"""9router VPS push — reusable by any farm.

Usage:
    from core.ninerouter import NinerouterPusher

    pusher = NinerouterPusher(provider="enter-converge", every_n=3)
    # After each successful account:
    pusher.queue(credential_dict)
    # On exit:
    pusher.flush()

Env (hub .env):
    NINEROUTER_VPS_HOST    — SSH host (required)
    NINEROUTER_VPS_USER    — SSH user (default: ubuntu)
    NINEROUTER_VPS_PW      — SSH password for legacy mode (no default; prefer key-command mode)
    NINEROUTER_VPS_DB      — remote DB path (default: /home/ubuntu/.9router/db/data.sqlite)
    NINEROUTER_VPS_SQLITE  — remote better-sqlite3 path
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

# Lazy paramiko import (only on push)
_paramiko = None


def _get_paramiko():
    global _paramiko
    if _paramiko is None:
        import paramiko
        _paramiko = paramiko
    return _paramiko


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


# Defaults
_DEFAULT_HOST = ""
_DEFAULT_USER = "ubuntu"
_DEFAULT_PW = ""
_DEFAULT_DB = "/home/ubuntu/.9router/db/data.sqlite"
_DEFAULT_SQLITE = "/home/ubuntu/scripts/grok-refresh/node_modules/better-sqlite3"

# Node upsert script template (uploaded once per session)
_UPSERT_JS = '''
const Database = require("{sqlite_path}");
const crypto = require("crypto");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", d => input += d);
process.stdin.on("end", () => {{
    const {{credentials, provider}} = JSON.parse(input);
    const db = new Database("{db_path}");

    const check = db.prepare("SELECT id FROM providerConnections WHERE email = ? AND provider = ?");
    const insert = db.prepare(`INSERT INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
        VALUES (@id, @provider, @authType, @name, @email, @priority, @isActive, @data, @createdAt, @updatedAt)`);
    const update = db.prepare(`UPDATE providerConnections SET data = @data, updatedAt = @updatedAt WHERE id = @id`);

    let inserted = 0, updated = 0;

    const tx = db.transaction(() => {{
        for (const cred of credentials) {{
            const existing = check.get(cred.email, provider);
            if (existing) {{
                update.run({{id: existing.id, data: cred.data, updatedAt: cred.updatedAt}});
                updated++;
            }} else {{
                cred.id = provider.replace(/[^a-z]/g, "") + "-" + crypto.randomBytes(8).toString("hex");
                insert.run(cred);
                inserted++;
            }}
        }}
    }});
    tx();

    db.close();
    console.log(JSON.stringify({{inserted, updated, total: credentials.length}}));
}});
'''


class NinerouterPusher:
    """Batch-push credentials to remote 9router VPS every N successes."""

    def __init__(
        self,
        provider: str,
        every_n: int = 5,
        host: str = "",
        user: str = "",
        pw: str = "",
        db_path: str = "",
        sqlite_path: str = "",
    ):
        self.provider = provider
        self.every_n = max(1, every_n)
        self.host = host or _env("NINEROUTER_VPS_HOST", _DEFAULT_HOST)
        self.user = user or _env("NINEROUTER_VPS_USER", _DEFAULT_USER)
        self.pw = pw or _env("NINEROUTER_VPS_PW", _DEFAULT_PW)
        self.db_path = db_path or _env("NINEROUTER_VPS_DB", _DEFAULT_DB)
        self.sqlite_path = sqlite_path or _env("NINEROUTER_VPS_SQLITE", _DEFAULT_SQLITE)
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._pushed = 0
        self._failed = 0
        self._script_uploaded = False
        self.command = _env("NINEROUTER_VPS_COMMAND")
        self.key = _env("NINEROUTER_VPS_KEY")

    def queue(self, credential: dict) -> None:
        """Add credential to queue. Auto-pushes when queue reaches every_n."""
        batch = None
        with self._lock:
            self._queue.append(credential)
            if len(self._queue) >= self.every_n:
                batch = list(self._queue)
                self._queue.clear()
        if batch is None:
            return
        self._push(batch)

    def flush(self) -> None:
        """Push remaining queued credentials."""
        with self._lock:
            if not self._queue:
                return
            batch = list(self._queue)
            self._queue.clear()
        self._push(batch)

    @property
    def stats(self) -> dict:
        return {"pushed": self._pushed, "failed": self._failed, "queued": len(self._queue)}

    def _push(self, batch: list[dict]) -> bool:
        """SSH push batch to remote router. Returns True on success."""
        if self.command:
            return self._push_command(batch)
        paramiko = _get_paramiko()
        payload = json.dumps({"credentials": batch, "provider": self.provider})
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(self.host, username=self.user, password=self.pw, timeout=15)

            # Upload upsert script once per connection
            if not self._script_uploaded:
                js = _UPSERT_JS.format(sqlite_path=self.sqlite_path, db_path=self.db_path)
                sftp = c.open_sftp()
                with sftp.open('/tmp/9r_upsert.js', 'w') as f:
                    f.write(js)
                sftp.close()
                self._script_uploaded = True

            stdin, stdout, stderr = c.exec_command('node /tmp/9r_upsert.js', timeout=30)
            stdin.write(payload)
            stdin.channel.shutdown_write()
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            c.close()

            if out:
                result = json.loads(out)
                self._pushed += result.get("inserted", 0) + result.get("updated", 0)
                _log(f"[9ROUTER] pushed {result.get('total',0)}: "
                     f"+{result.get('inserted',0)} ~{result.get('updated',0)}")
                return True
            else:
                self._failed += len(batch)
                _log(f"[9ROUTER] push failed: {err[:150]}")
                return False
        except Exception as e:
            self._failed += len(batch)
            _log(f"[9ROUTER] push error: {e}")
            # Re-queue on failure
            with self._lock:
                self._queue.extend(batch)
            return False

    def _push_command(self, batch: list[dict]) -> bool:
        """Push JSON over key-only SSH to a native remote importer."""
        payload = json.dumps({"credentials": batch, "provider": self.provider})
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if self.key:
            command += ["-i", self.key]
        command += [f"{self.user}@{self.host}", self.command]
        try:
            result = subprocess.run(command, input=payload, text=True, capture_output=True, timeout=150)
            if result.returncode:
                raise RuntimeError(result.stderr.strip()[:300] or f"exit {result.returncode}")
            out = json.loads(result.stdout)
            if not out.get("ok"):
                raise RuntimeError(str(out.get("error") or out)[:300])
            pushed = int(out.get("accounts", 0))
            skipped = int(out.get("skipped", 0))
            if pushed != len(batch) or skipped or out.get("errors"):
                raise RuntimeError(f"incomplete native import: {out}"[:300])
            self._pushed += pushed
            _log(f"[NVROUTER] pushed {pushed} account(s)")
            return True
        except Exception as e:
            self._failed += len(batch)
            _log(f"[NVROUTER] push error: {e}")
            with self._lock:
                self._queue.extend(batch)
            return False


def _log(msg: str):
    print(msg, flush=True)


def make_credential(
    provider: str,
    email: str,
    data: dict,
    auth_type: str = "apikey",
    priority: int = 1,
) -> dict:
    """Build a credential dict ready for NinerouterPusher.queue()."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": "",  # generated on remote
        "provider": provider,
        "authType": auth_type,
        "name": email,
        "email": email,
        "priority": priority,
        "isActive": 1,
        "data": json.dumps(data) if isinstance(data, dict) else data,
        "createdAt": now,
        "updatedAt": now,
    }
