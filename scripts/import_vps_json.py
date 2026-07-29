#!/usr/bin/env python3
"""Import VPS farm accounts.json into local 9router SQLite.

Workflow:
  VPS farm → batch_*/accounts.json  (JSON only, inject off)
  scp batch folders to PC
  python scripts/import_vps_json.py path\\to\\batch_or_folder

Examples:
  # one batch dir
  python scripts/import_vps_json.py C:\\Users\\Nazril\\Downloads\\vps-grok\\batch_20260722_054911_68b5da

  # single accounts.json
  python scripts/import_vps_json.py .\\accounts.json --provider grok

  # folder containing many batch_* (auto-detect grok/enter per file)
  python scripts/import_vps_json.py C:\\Users\\Nazril\\Downloads\\vps-farm --recursive

  # dry-run
  python scripts/import_vps_json.py .\\batch_xxx --dry-run

  # after inject, bulk untested→active
  python scripts/import_vps_json.py .\\batch_xxx --mark-active

Pull from VPS (PowerShell):
  scp -i $env:USERPROFILE\\Downloads\\prod.pem -r `
    ubuntu@43.156.135.115:/home/ubuntu/Automation/farms/grok/results/batch_* `
    .\\Downloads\\vps-grok\\
  scp -i $env:USERPROFILE\\Downloads\\prod.pem -r `
    ubuntu@43.156.135.115:/home/ubuntu/Automation/farms/enter/results/batch_* `
    .\\Downloads\\vps-enter\\
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def default_db() -> Path:
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        return Path(appdata) / "9router" / "db" / "data.sqlite"
    return Path.home() / ".9router" / "db" / "data.sqlite"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_accounts(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"expected JSON array in {path}")
    return [x for x in raw if isinstance(x, dict)]


def detect_kind(acc: dict[str, Any], forced: str | None) -> str | None:
    if forced and forced != "auto":
        return forced
    tokens = acc.get("tokens")
    if isinstance(tokens, dict) and (tokens.get("access_token") or tokens.get("accessToken")):
        return "grok"
    # enter shapes
    if acc.get("api_key") or acc.get("apiKey") or acc.get("ek_key"):
        return "enter"
    ak = acc.get("api_key")
    if isinstance(ak, dict) and (ak.get("key") or (ak.get("data") or {}).get("key")):
        return "enter"
    enter = acc.get("enter") if isinstance(acc.get("enter"), dict) else {}
    if enter.get("api_key") or enter.get("workspace_id"):
        return "enter"
    # heuristic: has workspace_id + password
    if acc.get("workspace_id") and (acc.get("password") or acc.get("email")):
        return "enter"
    return None


def extract_enter(acc: dict[str, Any]) -> tuple[str, str, str]:
    """Return (api_key, email, workspace_id)."""
    email = (acc.get("email") or "").strip().lower()
    ws = str(
        acc.get("workspace_id")
        or (acc.get("enter") or {}).get("workspace_id")
        or ""
    ).strip()

    key = ""
    # top-level string
    if isinstance(acc.get("api_key"), str) and acc["api_key"].startswith("ek_"):
        key = acc["api_key"].strip()
    elif isinstance(acc.get("apiKey"), str) and acc["apiKey"].startswith("ek_"):
        key = acc["apiKey"].strip()
    # top-level dict (normalized by farm)
    ak = acc.get("api_key")
    if isinstance(ak, dict):
        key = (ak.get("key") or (ak.get("data") or {}).get("key") or key or "").strip()
        if not ws:
            ws = str(ak.get("workspace_id") or "").strip()
    # nested enter.api_key
    enter = acc.get("enter") if isinstance(acc.get("enter"), dict) else {}
    eak = enter.get("api_key") if isinstance(enter.get("api_key"), dict) else {}
    if not key:
        data = eak.get("data") if isinstance(eak.get("data"), dict) else eak
        if isinstance(data, dict):
            key = (data.get("key") or "").strip()
    if not ws:
        ws = str(enter.get("workspace_id") or "").strip()
    return key, email, ws


def extract_grok(acc: dict[str, Any]) -> tuple[dict[str, Any], str]:
    email = (acc.get("email") or "").strip().lower()
    tokens = acc.get("tokens") if isinstance(acc.get("tokens"), dict) else {}
    # also accept flat shape
    if not tokens.get("access_token") and acc.get("access_token"):
        tokens = {
            "access_token": acc.get("access_token"),
            "refresh_token": acc.get("refresh_token"),
            "expires_at": acc.get("expires_at"),
            "expires_in": acc.get("expires_in"),
            "scope": acc.get("scope"),
            "id_token": acc.get("id_token"),
            "auth_mode": acc.get("auth_mode"),
        }
    return tokens, email


def inject_grok(cur: sqlite3.Cursor, acc: dict[str, Any], *, dry: bool) -> str:
    tokens, email = extract_grok(acc)
    if not email or not tokens.get("access_token"):
        return "skip:no-tokens"
    cur.execute(
        "SELECT id FROM providerConnections WHERE email = ? AND provider = 'grok-cli'",
        (email,),
    )
    if cur.fetchone():
        return "skip:dup"
    now = utc_now()
    expires_at = tokens.get("expires_at") or now
    if not tokens.get("expires_at") and tokens.get("expires_in"):
        try:
            exp_in = int(tokens["expires_in"])
            expires_at = (
                datetime.now(timezone.utc).timestamp() + exp_in
            )
            expires_at = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except Exception:
            expires_at = now
    row_id = "grok-farm-" + secrets.token_hex(8)
    data = {
        "displayName": email,
        "accessToken": tokens.get("access_token") or "",
        "refreshToken": tokens.get("refresh_token") or "",
        "expiresAt": expires_at,
        "scope": tokens.get("scope")
        or "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write",
        "testStatus": "active",
        "expiresIn": tokens.get("expires_in") or 21600,
        "providerSpecificData": {
            "authMethod": tokens.get("auth_mode") or "oidc",
            "idToken": tokens.get("id_token"),
            "email": email,
            "userId": None,
            "hasGrokCodeAccess": True,
            "subscriptionTier": None,
        },
        "lastError": None,
        "lastErrorAt": None,
    }
    if dry:
        return "dry:inject"
    cur.execute(
        "INSERT INTO providerConnections "
        "(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            "grok-cli",
            "oauth",
            email,
            email,
            1,
            1,
            json.dumps(data, ensure_ascii=False),
            now,
            now,
        ),
    )
    return f"ok:{row_id}"


def inject_enter(cur: sqlite3.Cursor, acc: dict[str, Any], *, dry: bool) -> str:
    api_key, email, ws = extract_enter(acc)
    if not api_key:
        return "skip:no-apikey"
    # dedup by apiKey in data
    cur.execute(
        "SELECT id, data FROM providerConnections WHERE provider = 'enter-converge'"
    )
    now = utc_now()
    for row_id, data_json in cur.fetchall():
        try:
            d = json.loads(data_json or "{}")
        except Exception:
            d = {}
        if (d.get("apiKey") or "") == api_key:
            psd = d.get("providerSpecificData") or {}
            if ws and not psd.get("workspaceId") and not dry:
                psd["workspaceId"] = ws
                d["providerSpecificData"] = psd
                d["testStatus"] = d.get("testStatus") or "active"
                cur.execute(
                    "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?",
                    (json.dumps(d, ensure_ascii=False), now, row_id),
                )
                return f"updated:{row_id}"
            return f"skip:dup:{row_id}"

    name = (email.split("@")[0][:32] if email else f"farm-{api_key[-8:]}")
    row_id = f"enter-farm-{uuid.uuid4().hex[:16]}"
    data_obj: dict[str, Any] = {
        "displayName": email or name,
        "apiKey": api_key,
        "testStatus": "active",
        "providerSpecificData": {},
        "lastError": None,
        "lastErrorAt": None,
    }
    if ws:
        data_obj["providerSpecificData"]["workspaceId"] = ws
    if email:
        data_obj["providerSpecificData"]["email"] = email
    if dry:
        return "dry:inject"
    cur.execute(
        "INSERT INTO providerConnections "
        "(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            "enter-converge",
            "apikey",
            name,
            email or None,
            1,
            1,
            json.dumps(data_obj, ensure_ascii=False),
            now,
            now,
        ),
    )
    return f"ok:{row_id}"


def iter_account_files(paths: Iterable[Path], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        p = p.expanduser().resolve()
        if not p.exists():
            print(f"WARN: missing {p}", file=sys.stderr)
            continue
        if p.is_file() and p.name.endswith(".json"):
            files.append(p)
            continue
        if p.is_dir():
            if (p / "accounts.json").is_file():
                files.append(p / "accounts.json")
            if recursive or p.name.startswith("batch_"):
                for aj in sorted(p.rglob("accounts.json")):
                    if aj not in files:
                        files.append(aj)
            else:
                for child in sorted(p.iterdir()):
                    if child.is_dir() and child.name.startswith("batch_"):
                        aj = child / "accounts.json"
                        if aj.is_file() and aj not in files:
                            files.append(aj)
    return files


def mark_active(conn: sqlite3.Connection, providers: set[str] | None, dry: bool) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id, provider, data FROM providerConnections")
    n = 0
    now = utc_now()
    for row_id, provider, raw in cur.fetchall():
        if providers and provider not in providers:
            continue
        try:
            data = json.loads(raw or "{}")
        except Exception:
            continue
        st = (data.get("testStatus") or "").lower()
        if st in ("active",):
            continue
        if st and st not in ("untested", "unknown", ""):
            # only promote untested-like; leave unavailable alone
            if st == "unavailable":
                continue
        data["testStatus"] = "active"
        data["lastError"] = None
        data["lastErrorAt"] = None
        if dry:
            n += 1
            continue
        cur.execute(
            "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), now, row_id),
        )
        n += 1
    if not dry:
        conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Import VPS farm JSON → local 9router")
    ap.add_argument(
        "paths",
        nargs="+",
        help="accounts.json path, batch_ dir, or parent folder of batch_*",
    )
    ap.add_argument(
        "--provider",
        choices=["auto", "grok", "enter"],
        default="auto",
        help="force provider (default auto-detect)",
    )
    ap.add_argument(
        "--db",
        default=str(default_db()),
        help="path to 9router data.sqlite",
    )
    ap.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="scan all accounts.json under given dirs",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--mark-active",
        action="store_true",
        help="set testStatus active on injected providers after import",
    )
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file() and not args.dry_run:
        print(
            f"ERROR: 9router DB not found: {db}\n"
            "Start 9router once on this PC so the DB is created.",
            file=sys.stderr,
        )
        return 1

    files = iter_account_files([Path(p) for p in args.paths], args.recursive)
    if not files:
        print("ERROR: no accounts.json found", file=sys.stderr)
        return 1

    print(f"DB: {db}")
    print(f"files: {len(files)}")
    if args.dry_run:
        print("dry-run: no writes")

    conn = sqlite3.connect(str(db) if db.is_file() else ":memory:")
    cur = conn.cursor()

    stats = {
        "grok_ok": 0,
        "grok_skip": 0,
        "enter_ok": 0,
        "enter_skip": 0,
        "unknown": 0,
        "errors": 0,
    }
    touched_providers: set[str] = set()

    for fpath in files:
        try:
            accounts = load_accounts(fpath)
        except Exception as e:
            print(f"FAIL read {fpath}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue
        if not args.quiet:
            print(f"\n== {fpath} ({len(accounts)} rows) ==")
        for acc in accounts:
            kind = detect_kind(acc, args.provider)
            email = (acc.get("email") or "?")[:48]
            try:
                if kind == "grok":
                    r = inject_grok(cur, acc, dry=args.dry_run)
                    if r.startswith("ok") or r.startswith("dry"):
                        stats["grok_ok"] += 1
                        touched_providers.add("grok-cli")
                    else:
                        stats["grok_skip"] += 1
                    if not args.quiet:
                        print(f"  grok  {r:20} {email}")
                elif kind == "enter":
                    r = inject_enter(cur, acc, dry=args.dry_run)
                    if r.startswith("ok") or r.startswith("dry") or r.startswith("updated"):
                        stats["enter_ok"] += 1
                        touched_providers.add("enter-converge")
                    else:
                        stats["enter_skip"] += 1
                    if not args.quiet:
                        print(f"  enter {r:20} {email}")
                else:
                    stats["unknown"] += 1
                    if not args.quiet:
                        print(f"  ?     skip:unknown-shape  {email}")
            except Exception as e:
                stats["errors"] += 1
                print(f"  ERR   {type(e).__name__}: {e}  {email}", file=sys.stderr)

    if not args.dry_run:
        conn.commit()

    if args.mark_active and not args.dry_run:
        n = mark_active(conn, touched_providers or None, dry=False)
        print(f"\nmark-active: {n} row(s)")
    elif args.mark_active and args.dry_run:
        n = mark_active(conn, touched_providers or None, dry=True)
        print(f"\nmark-active dry: would touch ~{n}")

    conn.close()
    print("\n---")
    print(
        f"grok inject/update: {stats['grok_ok']}  skip: {stats['grok_skip']}\n"
        f"enter inject/update: {stats['enter_ok']}  skip: {stats['enter_skip']}\n"
        f"unknown: {stats['unknown']}  errors: {stats['errors']}"
    )
    print("Refresh 9router UI or restart 9router if connections don't show.")
    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
