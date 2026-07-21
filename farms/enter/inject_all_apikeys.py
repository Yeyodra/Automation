#!/usr/bin/env python3
"""
Import all farmed ek_ keys from results/all_apikeys.txt into 9router SQLite.

Same approach as grok-farm: write %APPDATA%\\9router\\db\\data.sqlite directly
(no HTTP, no auth). Safe to re-run — duplicates are skipped / workspace patched.

Usage:
  python inject_all_apikeys.py
  python inject_all_apikeys.py --file results/all_apikeys.txt
  python inject_all_apikeys.py --dry-run
  python inject_all_apikeys.py --limit 50
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_keys(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith("ek_"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            # allow ek_|email|ws pipe format too
            parts = line.split("|")
        if len(parts) < 3:
            continue
        rows.append(
            {
                "key": parts[0].strip(),
                "email": parts[1].strip() if len(parts) > 1 else "",
                "ws": parts[2].strip() if len(parts) > 2 else "",
                "batch": parts[3].strip() if len(parts) > 3 else "",
            }
        )
    # dedup by key (keep last)
    by_key: dict[str, dict[str, str]] = {}
    for r in rows:
        by_key[r["key"]] = r
    return list(by_key.values())


def main() -> None:
    ap = argparse.ArgumentParser(description="Import all_apikeys.txt → 9router Enter Converge pool")
    ap.add_argument(
        "--file",
        "-f",
        default=str(_ROOT / "results" / "all_apikeys.txt"),
        help="Path to all_apikeys.txt (ek_\\temail\\tworkspace_id\\tbatch)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Parse only, no DB write")
    ap.add_argument("--limit", type=int, default=0, help="Max keys to inject (0=all)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Inject even if ENTER_9ROUTER_INJECT=false in .env",
    )
    args = ap.parse_args()

    _load_dotenv()
    # bulk import always intends to inject
    if args.force or True:
        os.environ["ENTER_9ROUTER_INJECT"] = "true"

    sys.path.insert(0, str(_ROOT))
    import farm  # after env set

    # re-bind inject flag if farm already read False before we set env
    farm.NINEROUTER_INJECT = True

    keys = parse_keys(Path(args.file))
    if args.limit and args.limit > 0:
        keys = keys[: args.limit]

    print(f"file: {args.file}")
    print(f"keys: {len(keys)}")
    print(f"db:   {farm.NINEROUTER_DB}")
    print(f"db exists: {Path(farm.NINEROUTER_DB).is_file()}")
    if args.dry_run:
        for i, r in enumerate(keys[:10], 1):
            print(f"  [{i}] {r['email'] or '-'}  ws={r['ws']}  {r['key'][:16]}...")
        if len(keys) > 10:
            print(f"  ... +{len(keys) - 10} more")
        print("dry-run: no writes")
        return

    if not Path(farm.NINEROUTER_DB).is_file():
        raise SystemExit(
            f"9router DB missing: {farm.NINEROUTER_DB}\n"
            "Start 9router once so the DB is created, then re-run."
        )

    ok = skip = fail = 0
    for i, r in enumerate(keys, 1):
        success, detail = farm.inject_to_9router(
            r["key"], r["ws"], email=r["email"], name=""
        )
        low = detail.lower()
        if success and "skip" in low:
            skip += 1
            mark = "SKIP"
        elif success:
            ok += 1
            mark = "OK  "
        else:
            fail += 1
            mark = "FAIL"
        if mark != "SKIP" or i <= 5 or i % 50 == 0:
            print(f"[{i}/{len(keys)}] {mark} {r['email'] or r['key'][:12]}  {detail}")

    print("---")
    print(f"inserted/updated: {ok}")
    print(f"skipped (dup):    {skip}")
    print(f"failed:           {fail}")
    print("Refresh Enter Converge connections in 9router UI (or restart 9router).")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
