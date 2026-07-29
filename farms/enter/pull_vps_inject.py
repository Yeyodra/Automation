#!/usr/bin/env python3
"""Pull accounts from ALL VPS instances and inject into local 9router DB.

Usage: python farms/enter/pull_vps_inject.py
"""
import json
import os
import sqlite3
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

try:
    import paramiko
except ImportError:
    print("pip install paramiko", flush=True)
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
# Add new VPS here: {"host": "IP", "pw": "PASSWORD", "user": "root"}
VPS_LIST = [
    {"host": "104.64.15.110", "pw": "3HKOJACJ1AV342L3", "user": "root"},
    # {"host": "NEW_IP", "pw": r"NEW_PASS", "user": "root"},
]

PROVIDER = "enter-converge"
PRIORITY = 1
DB_PATH = Path(os.environ.get("APPDATA", "")) / "9router" / "db" / "data.sqlite"

# Track already-injected to avoid re-downloading same accounts
_INJECTED_LOG = Path(__file__).parent / "results" / "vps_injected.txt"


def download_accounts() -> list[dict]:
    """Download accounts.json from ALL batches on ALL VPS instances."""
    all_accounts = []

    for vps in VPS_LIST:
        host = vps["host"]
        print(f"\n[*] Connecting to {host}...", flush=True)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(host, username=vps["user"], password=vps["pw"], timeout=15,
                      allow_agent=False, look_for_keys=False)
        except Exception as e:
            print(f"  SKIP {host}: {type(e).__name__}: {e}", flush=True)
            continue

        # Find all batch dirs
        _, o, _ = c.exec_command(
            "ls -d /home/auto/Automation/farms/enter/results/batch_* 2>/dev/null", timeout=10
        )
        batches = [b.strip() for b in o.read().decode().strip().splitlines() if b.strip()]
        if not batches:
            print(f"  No batches on {host}", flush=True)
            c.close()
            continue

        # Download all accounts.json
        sftp = c.open_sftp()
        vps_count = 0
        for batch in batches:
            remote_file = f"{batch}/accounts.json"
            try:
                with sftp.open(remote_file, "r") as f:
                    data = json.loads(f.read().decode())
                if data:
                    all_accounts.extend(data)
                    vps_count += len(data)
            except Exception:
                pass

        sftp.close()
        c.close()
        print(f"  {host}: {vps_count} accounts from {len(batches)} batches", flush=True)

    print(f"\n[*] Total: {len(all_accounts)} accounts from {len(VPS_LIST)} VPS", flush=True)
    return all_accounts


def load_injected() -> set:
    """Load set of already-injected API keys."""
    if not _INJECTED_LOG.is_file():
        return set()
    return set(_INJECTED_LOG.read_text(encoding="utf-8").strip().splitlines())


def save_injected(keys: set):
    """Persist injected keys."""
    _INJECTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    _INJECTED_LOG.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")


def inject_one(cur, account: dict) -> tuple[bool, str]:
    """Inject single account into 9router DB."""
    api_key = (account.get("api_key") or {}).get("key") or ""
    if not api_key:
        return False, "no api_key"

    email = (account.get("email") or "").strip().lower()
    ws = str(account.get("workspace_id") or "")
    tokens = account.get("tokens") or {}
    access_token = tokens.get("access_token") or ""
    refresh_token = tokens.get("refresh_token") or ""
    expires_at = tokens.get("expires_at") or ""

    name = email.split("@")[0][:32] if email else f"farm-{api_key[-8:]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Dedup check
    cur.execute(
        "SELECT id, data FROM providerConnections WHERE provider = ?",
        (PROVIDER,),
    )
    for row_id, data_json in cur.fetchall():
        try:
            d = json.loads(data_json or "{}")
        except Exception:
            d = {}
        if (d.get("apiKey") or "") == api_key:
            return True, f"skip dup id={row_id}"

    # Insert
    row_id = f"enter-farm-{_uuid.uuid4().hex[:16]}"
    data_obj = {
        "displayName": email or name,
        "apiKey": api_key,
        "testStatus": "active",
        "providerSpecificData": {},
        "lastError": None,
        "lastErrorAt": None,
    }
    if access_token:
        data_obj["accessToken"] = access_token
    if refresh_token:
        data_obj["refreshToken"] = refresh_token
    if expires_at:
        data_obj["expiresAt"] = expires_at
    if ws:
        data_obj["providerSpecificData"]["workspaceId"] = ws
    if email:
        data_obj["providerSpecificData"]["email"] = email

    cur.execute(
        "INSERT INTO providerConnections "
        "(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, PROVIDER, "apikey", name, email or None, PRIORITY, 1,
         json.dumps(data_obj, ensure_ascii=False), now, now),
    )
    return True, f"inserted id={row_id} name={name}"


def main():
    # Download from VPS
    accounts = download_accounts()
    if not accounts:
        print("[!] Nothing to inject.", flush=True)
        return

    # Filter already-injected
    injected = load_injected()
    new_accounts = [a for a in accounts if (a.get("api_key") or {}).get("key") not in injected]
    print(f"[*] New accounts to inject: {len(new_accounts)} (skipping {len(accounts) - len(new_accounts)} already done)", flush=True)

    if not new_accounts:
        print("[*] All accounts already injected.", flush=True)
        return

    # Check DB
    if not DB_PATH.is_file():
        print(f"[!] 9router DB not found: {DB_PATH}", flush=True)
        print("[!] Start 9router once to create it.", flush=True)
        return

    # Inject
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    cur = conn.cursor()

    ok_count = 0
    for acc in new_accounts:
        ok, detail = inject_one(cur, acc)
        key = (acc.get("api_key") or {}).get("key") or ""
        email = acc.get("email", "?")
        if ok:
            injected.add(key)
            ok_count += 1
            print(f"  OK  {email} -> {detail}", flush=True)
        else:
            print(f"  ERR {email} -> {detail}", flush=True)

    conn.commit()
    conn.close()
    save_injected(injected)

    print(f"\n[+] Done! Injected {ok_count}/{len(new_accounts)} accounts into 9router.", flush=True)


if __name__ == "__main__":
    main()
