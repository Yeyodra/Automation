#!/usr/bin/env python3
"""Run one serial Enter lane with domain rotation and strict block attribution."""

import argparse
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
FARM = ROOT / "farms/enter"
FARM_RESULTS_SENTINEL = FARM / "results/gptmail_blocked_domains.txt"


def resolve_blocked_file(env: Mapping[str, str]) -> Path:
    return Path(env.get("ENTER_BLOCKED_DOMAINS_FILE") or FARM_RESULTS_SENTINEL)


def secure_runtime_paths(directory: Path, state_file: Path, log_file: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for path in (state_file, log_file):
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)


def validate_lane(lane: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", lane):
        raise ValueError("lane must contain only letters, numbers, underscore, or hyphen")
    return lane


def normalize_domains(domains: list[str]) -> list[str]:
    result = []
    for value in domains:
        domain = value.strip().lower()
        if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain) and domain not in result:
            result.append(domain)
    return result


def parse_blocked_domains(text: str) -> set[str]:
    return set(normalize_domains([line.split()[0] for line in text.splitlines() if line.split()]))


def select_domain(queue: list[str], cooldowns: dict[str, float], blocked: set[str], now: float) -> str | None:
    for index, domain in enumerate(queue):
        if domain not in blocked and cooldowns.get(domain, 0) <= now:
            queue.append(queue.pop(index))
            return domain
    return None


def terminate_process_group(child: subprocess.Popen) -> str:
    os.killpg(child.pid, signal.SIGTERM)
    try:
        output, _ = child.communicate(timeout=30)
    except (subprocess.TimeoutExpired, TimeoutError):
        output = ""
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.killpg(child.pid, signal.SIGKILL)
        if child.poll() is None:
            tail, _ = child.communicate()
            output += tail
    return output


def classify_outcome(output: str) -> str:
    low = output.lower()
    if re.search(r"\]\s+ok(?:\s|$)", output, re.I):
        return "ok"
    if any(x in low for x in (
        "domain_not_allowed", "domain is not allowed",
        "not allowed to sign up", "email provider is not allowed",
    )):
        return "domain_not_allowed"
    if "access_denied" in low:
        return "access_denied"
    if "otp timeout" in low:
        return "otp_timeout"
    if "risk-aware gateway login not observed" in low:
        return "gateway_missing"
    return "other"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", required=True)
    parser.add_argument("--gift", required=True)
    parser.add_argument("--inviter", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--domains", required=True, help="File containing one Emailqu domain per line")
    parser.add_argument("--preferred", action="append", default=[])
    parser.add_argument("--gap", type=int, default=60)
    parser.add_argument("--cooldown", type=int, default=900)
    args = parser.parse_args()

    blocked_file = resolve_blocked_file(os.environ)
    blocked_file.parent.mkdir(parents=True, exist_ok=True)
    lane = validate_lane(args.lane)
    catalog = Path(args.domains).read_text().splitlines()
    queue = normalize_domains(args.preferred + catalog)
    if not queue:
        raise SystemExit("no valid domains")
    cooldowns: dict[str, float] = {}
    runtime_dir = FARM / "results/lanes"
    state_file = runtime_dir / f"lane-{lane}-state.json"
    log_file = runtime_dir / f"production-lane-{lane}.log"
    secure_runtime_paths(runtime_dir, state_file, log_file)

    while True:
        now = time.time()
        blocked = parse_blocked_domains(blocked_file.read_text()) if blocked_file.exists() else set()
        domain = select_domain(queue, cooldowns, blocked, now)
        if domain is None:
            time.sleep(max(30, int(min(cooldowns.values(), default=now + 60) - now)))
            continue
        env = os.environ.copy()
        env.update({
            "ENTER_EMAIL_MODE": "emailqu", "ENTER_EMAILQU_DOMAIN": domain,
            "ENTER_EMAILQU_PREFIX": args.prefix, "ENTER_GIFT_CODE": args.gift,
            "ENTER_INVITER": args.inviter, "ENTER_INVITEE_REWARD": "100",
            "ENTER_PASSWORD": "A!" + os.urandom(12).hex(),
            "ENTER_PROXY_FILE": "/nonexistent", "ENTER_PROXY_POOL": args.proxy,
            "ENTER_9ROUTER_VPS_EVERY_N": "1", "ENTER_GPTMAIL_DOMAIN_RETRIES": "4",
        })
        command = [str(ROOT / ".venv/bin/python"), "-m", "jobs", "run", "enter", "--warp-every-n", "0", "--", "-n", "1", "-c", "1", "-y", "--headless", "--spawn-delay", "0", "--account-gap", "0"]
        child = subprocess.Popen(
            command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            output, _ = child.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            output = terminate_process_group(child) + "\nsupervisor child timeout\n"
        with log_file.open("a") as handle:
            handle.write(output)
        category = classify_outcome(output)
        if category == "domain_not_allowed":
            if domain not in blocked:
                with blocked_file.open("a") as handle:
                    handle.write(domain + "\n")
        elif category != "ok":
            cooldowns[domain] = time.time() + args.cooldown
        state = {
            "lane": lane, "domain": domain, "category": category,
            "at": datetime.now(timezone.utc).isoformat(),
            "cooldown_domains": sum(value > time.time() for value in cooldowns.values()),
        }
        state_file.write_text(json.dumps(state) + "\n")
        print(json.dumps(state), flush=True)
        time.sleep(args.gap)


if __name__ == "__main__":
    main()
