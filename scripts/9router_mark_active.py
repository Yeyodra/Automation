#!/usr/bin/env python3
"""Bulk-set 9router connection testStatus untested → active (skip one-by-one test).

Default: provider=grok-cli, only rows with testStatus=untested.

  python scripts/9router_mark_active.py
  python scripts/9router_mark_active.py --provider grok-cli --dry-run
  python scripts/9router_mark_active.py --all-untested
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def default_db() -> Path:
    appdata = os.environ.get("APPDATA") or ""
    return Path(appdata) / "9router" / "db" / "data.sqlite"


def main() -> int:
    ap = argparse.ArgumentParser(description="Mark 9router connections as active")
    ap.add_argument(
        "--db",
        default=str(default_db()),
        help="Path to data.sqlite",
    )
    ap.add_argument(
        "--provider",
        default="grok-cli",
        help="Provider id (default grok-cli). Use * with --all-untested for all providers.",
    )
    ap.add_argument(
        "--all-untested",
        action="store_true",
        help="All providers: any testStatus=untested → active",
    )
    ap.add_argument(
        "--include-unavailable",
        action="store_true",
        help="Also set unavailable → active (dangerous; re-enables known-bad)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"ERROR: DB not found: {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    if args.all_untested:
        cur.execute("SELECT id, provider, email, data FROM providerConnections")
    else:
        cur.execute(
            "SELECT id, provider, email, data FROM providerConnections WHERE provider = ?",
            (args.provider,),
        )

    want = {"untested"}
    if args.include_unavailable:
        want.add("unavailable")

    updated = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for row_id, provider, email, raw in cur.fetchall():
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            skipped += 1
            continue
        st = (data.get("testStatus") or "").lower()
        if st not in want:
            skipped += 1
            continue
        old = data.get("testStatus")
        data["testStatus"] = "active"
        data["lastError"] = None
        data["lastErrorAt"] = None
        new_raw = json.dumps(data, ensure_ascii=False)
        if args.dry_run:
            print(f"DRY {provider} {email}: {old} → active")
        else:
            cur.execute(
                "UPDATE providerConnections SET data = ?, updatedAt = ? WHERE id = ?",
                (new_raw, now, row_id),
            )
        updated += 1

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"{'would update' if args.dry_run else 'updated'}={updated} skipped={skipped} db={db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
