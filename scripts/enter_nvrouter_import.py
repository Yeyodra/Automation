#!/usr/bin/env python3
"""Import Enter credentials into NvRouter through its loopback vault-backed API."""

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

DB = os.environ.get("NVROUTER_DB", os.path.expanduser("~/.keirouter/keirouter.db"))
BASE = os.environ.get("NVROUTER_BASE_URL", "http://127.0.0.1:20180").rstrip("/")


def dashboard_cookie() -> str:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as database:
        signing = database.execute("SELECT value FROM settings WHERE key='auth.signing_key'").fetchone()
        generation = database.execute("SELECT value FROM settings WHERE key='auth.session_generation'").fetchone()
    if not signing:
        raise RuntimeError("NvRouter dashboard signing key unavailable")
    payload = {
        "sub": "dashboard",
        "exp": int(time.time()) + 300,
        "gen": int(generation[0]) if generation else 1,
        "jti": base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode(),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(base64.b64decode(signing[0]), body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"kr_session={body}.{signature}"


def convert(payload: dict) -> list[dict]:
    rows = []
    for index, credential in enumerate(payload.get("credentials", [])):
        data = credential.get("data", {})
        if isinstance(data, str):
            data = json.loads(data)
        api_key = str(data.get("apiKey") or "")
        email = str(credential.get("email") or data.get("displayName") or "").strip()
        specific = data.get("providerSpecificData") or {}
        if not api_key.startswith("ek_") or not email:
            continue
        rows.append({
            "id": f"enter-farm-{int(time.time())}-{secrets.token_hex(6)}-{index}",
            "provider": "enter-converge",
            "authType": "apiKey",
            "name": email,
            "email": email,
            "priority": int(credential.get("priority") or 1),
            "isActive": True,
            "apiKey": api_key,
            "providerSpecificData": {
                "workspaceId": str(specific.get("workspaceId") or ""),
                "email": email,
            },
        })
    return rows


def main() -> None:
    payload = json.load(sys.stdin)
    rows = convert(payload)
    if not rows:
        print(json.dumps({"ok": True, "accounts": 0, "skipped": len(payload.get("credentials", []))}))
        return
    body = json.dumps(
        {"source": "9router", "config": {"providerConnections": rows}},
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        BASE + "/api/settings/database/import-foreign",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": dashboard_cookie(),
            "User-Agent": "Automation-enter-farm/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"NvRouter API HTTP {error.code}: {error.read(500).decode(errors='replace')}"
        ) from error
    if result.get("accounts") != len(rows) or result.get("errors"):
        raise RuntimeError(f"incomplete NvRouter import: {result}")
    print(json.dumps({
        "ok": True,
        "accounts": result.get("accounts", 0),
        "skipped": result.get("skipped", 0),
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
