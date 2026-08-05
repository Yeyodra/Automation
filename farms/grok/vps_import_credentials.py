#!/usr/bin/env python3
"""Import Grok reauth credentials through NvRouter's localhost admin API."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB = os.environ.get("NVROUTER_DB", "/home/ubuntu/.keirouter/keirouter.db")
BASE = os.environ.get("NVROUTER_URL", "http://127.0.0.1:20180")


def session_cookie():
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        signing = db.execute("SELECT value FROM settings WHERE key='auth.signing_key'").fetchone()
        generation = db.execute("SELECT value FROM settings WHERE key='auth.session_generation'").fetchone()
    if not signing:
        raise RuntimeError("NvRouter dashboard signing key unavailable")
    payload = {
        "sub": "dashboard",
        "exp": int(time.time()) + 300,
        "gen": int(generation[0]) if generation else 1,
        "jti": base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode(),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(base64.b64decode(signing[0]), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"kr_session={body}.{sig}"


def request(path, data=None, method=None):
    method = method or ("GET" if data is None else "POST")
    req = urllib.request.Request(
        BASE + path,
        data=None if data is None else json.dumps(data, separators=(",", ":")).encode(),
        method=method,
        headers={"Content-Type": "application/json", "Cookie": session_cookie(), "User-Agent": "Automation-grok-reauth/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"NvRouter API HTTP {error.code}: {error.read(500).decode(errors='replace')}") from error


def main():
    if sys.argv[1:] == ["--self-test"]:
        status, result = request("/api/accounts")
        print(json.dumps({"ok": status == 200, "status": status, "accounts": len(result.get("accounts", []))}))
        return

    payload = json.load(sys.stdin)
    credentials = payload.get("credentials", [])
    connections = []
    for index, item in enumerate(credentials):
        email = str(item.get("email") or "").strip()
        access = item.get("accessToken") or item.get("access_token") or ""
        refresh = item.get("refreshToken") or item.get("refresh_token") or ""
        if not email or not access or not refresh:
            continue
        connections.append({
            "id": f"grok-reauth-{int(time.time())}-{index}",
            "provider": "grok-cli",
            "authType": "oauth",
            "name": email,
            "email": email,
            "isActive": True,
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": item.get("expiresAt") or item.get("expires_at") or "",
            "providerSpecificData": {
                "authMethod": item.get("authMethod") or item.get("auth_mode") or "device_oauth",
                "email": email,
                **({"idToken": item.get("idToken") or item.get("id_token")} if item.get("idToken") or item.get("id_token") else {}),
            },
        })
    if not connections:
        print(json.dumps({"ok": True, "accounts": 0, "skipped": len(credentials)}))
        return
    old_by_email = {}
    _, current = request("/api/accounts")
    for account in current.get("accounts", []):
        if account.get("provider") == "grok-cli":
            old_by_email.setdefault(str(account.get("label") or "").lower(), []).append(account.get("id"))

    status, result = request("/api/settings/database/import-foreign", {"source": "9router", "config": {"providerConnections": connections}})
    errors = result.get("errors") or []
    if status != 200 or result.get("accounts") != len(connections) or errors:
        raise RuntimeError(f"incomplete NvRouter import: {result}")
    removed = 0
    for connection in connections:
        for account_id in old_by_email.get(connection["email"].lower(), []):
            if account_id:
                request(f"/api/accounts/{account_id}", method="DELETE")
                removed += 1
    print(json.dumps({"ok": True, "accounts": result.get("accounts", 0), "replaced": removed, "skipped": result.get("skipped", 0)}))


try:
    main()
except Exception as error:
    print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
    raise SystemExit(1)
