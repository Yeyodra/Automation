#!/usr/bin/env python3
"""Run one serial Enter lane with domain rotation and strict block attribution."""

import argparse
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
FARM = ROOT / "farms/enter"
FARM_RESULTS_SENTINEL = FARM / "results/gptmail_blocked_domains.txt"
TERMINAL_CATEGORIES = {
    "ok", "mailbox_create_failed", "mailbox_read_failed", "otp_timeout",
    "landing_failed", "invite_dialog_failed", "fingerprint_failed",
    "risk_session_failed", "gateway_missing", "turnstile_failed",
    "identifier_domain_blocked", "identifier_other_rejection",
    "password_stalled", "access_denied", "callback_failed",
    "session_invalid", "referral_claim_failed_nonfatal", "workspace_missing",
    "onboarding_failed", "api_key_failed", "nvrouter_push_failed",
    "account_timeout", "other",
}
SECRET_FIELDS = {"email", "password", "otp", "token", "api_key", "cookie", "code", "state"}


def resolve_blocked_file(env: Mapping[str, str]) -> Path:
    return Path(env.get("ENTER_BLOCKED_DOMAINS_FILE") or FARM_RESULTS_SENTINEL)


def secure_runtime_paths(directory: Path, state_file: Path, log_file: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for path in (state_file, log_file):
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)


def write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.write("\n")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def load_scheduler_state(data) -> tuple[int, dict, dict[str, float], dict[str, float]]:
    if not isinstance(data, dict):
        return 0, {}, {}, {}
    attempt = data.get("attempt", 0)
    domains = data.get("domains", {})
    last_used = data.get("last_used", {})
    cooldowns = data.get("cooldowns", {})
    if (
        not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0
        or not isinstance(domains, dict)
        or not isinstance(last_used, dict)
        or not isinstance(cooldowns, dict)
    ):
        return 0, {}, {}, {}
    try:
        def timestamps(values: dict) -> dict[str, float]:
            result = {}
            for key, value in values.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                number = float(value)
                if not math.isfinite(number) or abs(number) > 10_000_000_000:
                    raise ValueError
                result[str(key)] = number
            return result
        last_used = timestamps(last_used)
        cooldowns = timestamps(cooldowns)
        for domain, row in domains.items():
            if not isinstance(domain, str) or not isinstance(row, dict):
                raise ValueError
            if not isinstance(row.get("ok", 0), int) or not isinstance(row.get("ambiguous", 0), int):
                raise ValueError
            recent = row.get("recent", [])
            if not isinstance(recent, list) or any(x not in (0, 1) or isinstance(x, bool) for x in recent):
                raise ValueError
    except (OverflowError, TypeError, ValueError):
        return 0, {}, {}, {}
    return attempt, domains, last_used, cooldowns


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


def select_contextual_domain(queue: list[str], cooldowns: dict[str, float],
                             blocked: set[str], now: float, stats: dict,
                             attempt: int, last_used: dict[str, float],
                             min_reuse: int, explore_every: int) -> str | None:
    available = [
        domain for domain in queue
        if domain not in blocked
        and cooldowns.get(domain, 0) <= now
        and last_used.get(domain, 0) + min_reuse <= now
    ]
    if not available:
        return None
    if explore_every > 0 and attempt % explore_every == 0:
        selected = min(
            available,
            key=lambda domain: (
                int(stats.get(domain, {}).get("ok", 0))
                + int(stats.get(domain, {}).get("ambiguous", 0)),
                last_used.get(domain, 0),
            ),
        )
    else:
        def score(domain: str) -> tuple[float, float]:
            row = stats.get(domain, {})
            recent = row.get("recent") or []
            recent_wr = sum(recent) / len(recent) if recent else 0.5
            total = int(row.get("ok", 0)) + int(row.get("ambiguous", 0))
            smoothed = (int(row.get("ok", 0)) + 1) / (total + 2)
            return (0.7 * recent_wr + 0.3 * smoothed, -last_used.get(domain, 0))
        selected = max(available, key=score)
    queue.append(queue.pop(queue.index(selected)))
    return selected


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


def read_terminal_status(path: Path, *, expected_attempt: int | None = None,
                         expected_domain: str | None = None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {
        "version", "attempt", "ok", "category", "domain", "stage",
        "api_key_created", "nvrouter_pushed", "at",
    }:
        raise ValueError("invalid terminal artifact schema")
    if (
        data["version"] != 1
        or not isinstance(data["attempt"], int) or isinstance(data["attempt"], bool)
        or not isinstance(data["ok"], bool)
        or data["category"] not in TERMINAL_CATEGORIES
        or not isinstance(data["domain"], str)
        or not isinstance(data["stage"], str)
        or not isinstance(data["api_key_created"], bool)
        or not isinstance(data["nvrouter_pushed"], bool)
        or not isinstance(data["at"], str)
        or (data["category"] == "ok" and not (
            data["ok"] and data["api_key_created"] and data["nvrouter_pushed"]
        ))
        or (data["category"] != "ok" and data["ok"])
        or (data["category"] != "ok" and data["nvrouter_pushed"])
        or (expected_attempt is not None and data["attempt"] != expected_attempt)
        or (expected_domain is not None and data["domain"] != expected_domain)
    ):
        raise ValueError("invalid terminal artifact")
    return data


def read_terminal_outcome(path: Path, output: str, *, expected_attempt: int | None = None,
                          expected_domain: str | None = None) -> str:
    try:
        data = read_terminal_status(
            path, expected_attempt=expected_attempt, expected_domain=expected_domain
        )
        category = data["category"]
        return "domain_not_allowed" if category == "identifier_domain_blocked" else category
    except (OSError, ValueError, json.JSONDecodeError):
        fallback = classify_outcome(output)
        return "nvrouter_push_failed" if fallback == "ok" else fallback


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
    parser.add_argument("--min-reuse", type=int, default=120)
    parser.add_argument("--explore-every", type=int, default=5)
    args = parser.parse_args()

    blocked_file = resolve_blocked_file(os.environ)
    blocked_file.parent.mkdir(parents=True, exist_ok=True)
    lane = validate_lane(args.lane)
    catalog = Path(args.domains).read_text().splitlines()
    queue = normalize_domains(args.preferred + catalog)
    if not queue:
        raise SystemExit("no valid domains")
    runtime_dir = FARM / "results/lanes"
    state_file = runtime_dir / f"lane-{lane}-state.json"
    log_file = runtime_dir / f"production-lane-{lane}.log"
    secure_runtime_paths(runtime_dir, state_file, log_file)
    stats_file = runtime_dir / f"lane-{lane}-domain-stats.json"
    stats_file.touch(mode=0o600, exist_ok=True)
    stats_file.chmod(0o600)
    try:
        persisted = json.loads(stats_file.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        persisted = {}
    attempt_number, stats, last_used, cooldowns = load_scheduler_state(persisted)

    while True:
        now = time.time()
        blocked = parse_blocked_domains(blocked_file.read_text()) if blocked_file.exists() else set()
        attempt_number += 1
        domain = select_contextual_domain(
            queue, cooldowns, blocked, now, stats, attempt_number, last_used,
            max(0, args.min_reuse), max(0, args.explore_every),
        )
        if domain is None:
            attempt_number -= 1
            wakeups = [value for value in cooldowns.values() if value > now]
            wakeups += [value + max(0, args.min_reuse) for value in last_used.values() if value + max(0, args.min_reuse) > now]
            time.sleep(max(30, int(min(wakeups, default=now + 60) - now)))
            continue
        last_used[domain] = now
        terminal_file = runtime_dir / f"terminal-{lane}-{time.time_ns()}.json"
        env = os.environ.copy()
        env.update({
            "ENTER_EMAIL_MODE": "emailqu", "ENTER_EMAILQU_DOMAIN": domain,
            "ENTER_EMAILQU_PREFIX": args.prefix, "ENTER_GIFT_CODE": args.gift,
            "ENTER_INVITER": args.inviter, "ENTER_INVITEE_REWARD": "100",
            "ENTER_PASSWORD": "A!" + os.urandom(12).hex(),
            "ENTER_PROXY_FILE": "/nonexistent", "ENTER_PROXY_POOL": args.proxy,
            "ENTER_9ROUTER_VPS_EVERY_N": "1", "ENTER_GPTMAIL_DOMAIN_RETRIES": "4",
            "ENTER_TERMINAL_STATUS_FILE": str(terminal_file),
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
        try:
            terminal = read_terminal_status(terminal_file, expected_attempt=1, expected_domain=domain)
        except (OSError, ValueError, json.JSONDecodeError):
            terminal = {}
        category = read_terminal_outcome(
            terminal_file, output, expected_attempt=1, expected_domain=domain
        )
        terminal_file.unlink(missing_ok=True)
        if category == "domain_not_allowed":
            if domain not in blocked:
                with blocked_file.open("a") as handle:
                    handle.write(domain + "\n")
        elif category != "ok":
            cooldowns[domain] = time.time() + args.cooldown
        row = stats.setdefault(domain, {"ok": 0, "ambiguous": 0, "recent": []})
        if category == "ok":
            row["ok"] = int(row.get("ok", 0)) + 1
            outcome = 1
        else:
            row["ambiguous"] = int(row.get("ambiguous", 0)) + 1
            outcome = 0
        row["recent"] = (list(row.get("recent") or []) + [outcome])[-5:]
        write_private_json(stats_file, {
            "version": 1, "attempt": attempt_number, "domains": stats,
            "last_used": last_used, "cooldowns": cooldowns,
        })
        state = {
            "lane": lane, "domain": domain, "category": category,
            "at": datetime.now(timezone.utc).isoformat(),
            "cooldown_domains": sum(value > time.time() for value in cooldowns.values()),
            "api_key_created": bool(terminal.get("api_key_created")),
            "nvrouter_pushed": bool(terminal.get("nvrouter_pushed")),
        }
        write_private_json(state_file, state)
        print(json.dumps(state), flush=True)
        time.sleep(args.gap)


if __name__ == "__main__":
    main()
