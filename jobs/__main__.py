"""CLI: python -m jobs list|run|stop …

Hub flags BEFORE job args. Farm args after `--` only.

Examples:
  python -m jobs list
  python -m jobs run grok --warp-every-n 2 -- -n 10 -c 1 -y
  python -m jobs stop
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .registry import get_job, list_jobs
from .runner import run_job


def _split_hub_farm(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jobs",
        description="Automation hub — run external farm jobs",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List registered jobs")
    sub.add_parser("stop", help="Stop all running farm processes (global)")

    r = sub.add_parser(
        "run",
        help="Run a job. Pass farm.py args after --",
    )
    r.add_argument("job", help="Job id: grok")
    r.add_argument(
        "--warp-connect",
        action="store_true",
        help="Ensure WARP connected before job",
    )
    r.add_argument(
        "--warp-rotate",
        action="store_true",
        help="Rotate WARP once before farm start",
    )
    r.add_argument(
        "--warp-every-n",
        type=int,
        default=0,
        metavar="N",
        help="Wave rotate: 0=off, else forced equal to -c (1:1). e.g. -c 3 --warp-every-n 3",
    )
    r.add_argument(
        "--warp-tries",
        type=int,
        default=None,
        help="Max WARP rotate tries per rotate",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan only, do not execute",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    hub_argv, farm_args = _split_hub_farm(raw)
    args = _build_parser().parse_args(hub_argv)

    if args.cmd == "list":
        for j in list_jobs():
            ok = j.cwd.is_dir() and (j.cwd / j.entry).is_file()
            flag = "ok" if ok else "MISSING"
            print(f"{j.id:8}  [{flag}]  {j.name}")
            print(f"         {j.description}")
            print(f"         cwd={j.cwd}")
            print(f"         py={j.python()}")
            print()
        print("farms/<id>/ | stop: python -m jobs stop | WARP: --warp-every-n 2")
        return 0

    if args.cmd == "stop":
        _hub = Path(__file__).resolve().parent.parent
        if str(_hub) not in sys.path:
            sys.path.insert(0, str(_hub))
        from core.jobctl import stop_all, active_summary

        print(f"[jobctl] before: {active_summary()}")
        rows = stop_all(log=print)
        print(f"[jobctl] stopped {len(rows)} process(es)")
        return 0

    try:
        get_job(args.job)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    every = max(0, args.warp_every_n or 0)
    try:
        result = run_job(
            args.job,
            farm_args,
            warp_connect=args.warp_connect or args.warp_rotate or every > 0,
            warp_rotate=args.warp_rotate,
            warp_every_n=every,
            warp_tries=args.warp_tries,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if result.stopped:
        return 130
    return 0 if result.ok else (result.exit_code or 1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
