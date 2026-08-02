"""
Tasklet farm — pure HTTP magic link signup via exzork mailer.

Flow per account:
  1. Create random mailbox (exzork API)
  2. Request magic link (Tasklet API)
  3. Poll exzork for email → extract token
  4. Verify token → get PIN
  5. SignIn with PIN + secret → sessionToken
  6. Create organization + claim daily bonus
  7. Save result

Config: TASKLET_* env keys (hub .env maps shared → TASKLET_*).
Run:    python -m jobs run tasklet -- -n 5 -c 3 -y
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. pip install httpx", flush=True)
    sys.exit(1)


# ── Config ───────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


CONCURRENT = int(_env("TASKLET_CONCURRENT", "1") or "1")
ACCOUNT_TIMEOUT_S = int(_env("TASKLET_ACCOUNT_TIMEOUT", "120") or "120")

# Exzork mailer
EXZORK_API = _env("EXZORK_API", "https://mailer.exzork.me")
EXZORK_KEY = _env("EXZORK_API_KEY", "")
EXZORK_DOMAIN = _env("TASKLET_EXZORK_DOMAIN") or _env("EXZORK_DOMAIN", "")

# Tasklet
TASKLET_API = "https://api.tasklet.ai"

# WARP
WARP_EVERY_N = max(0, int(_env("TASKLET_WARP_EVERY_N") or _env("WARP_EVERY_N") or "0"))
WARP_SETTLE_S = max(3.0, float(_env("WARP_SETTLE_AFTER") or "8"))

# Results
RESULTS_ROOT = Path(_env("TASKLET_RESULTS_DIR", str(_ROOT / "results")))
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)


# ── WARP ─────────────────────────────────────────────────────────────────────
_warp_ok_counter = 0


def _effective_warp_every_n() -> int:
    if WARP_EVERY_N <= 0:
        return 0
    return max(1, CONCURRENT)


async def _maybe_warp_after_success() -> None:
    global _warp_ok_counter
    every = _effective_warp_every_n()
    if every <= 0:
        return
    _warp_ok_counter += 1
    if _warp_ok_counter >= every:
        _warp_ok_counter = 0
        try:
            from core.warp import WarpClient
            w = WarpClient(log=print)
            print("[WARP] rotating IP...", flush=True)
            w.rotate_ip(force=True)
            await asyncio.sleep(WARP_SETTLE_S)
            print("[WARP] settled", flush=True)
        except Exception as e:
            print(f"[WARP] rotate failed: {e}", flush=True)


# ── Exzork Mailer ────────────────────────────────────────────────────────────
def _exzork_headers() -> dict[str, str]:
    return {"X-API-Key": EXZORK_KEY, "Content-Type": "application/json"}


async def create_mailbox(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{EXZORK_API}/api/v1/mailboxes",
        headers=_exzork_headers(),
        json={"random": True, "domain": EXZORK_DOMAIN},
    )
    resp.raise_for_status()
    data = resp.json()
    if "mailbox" in data:
        return data["mailbox"]["address"]
    if "mailboxes" in data and data["mailboxes"]:
        return data["mailboxes"][0]["address"]
    raise ValueError(f"Unexpected mailbox response: {data}")


async def poll_magic_link_email(client: httpx.AsyncClient, address: str, timeout: int = 60) -> str:
    """Poll exzork for tasklet magic link email. Returns token from URL."""
    start = time.time()
    while time.time() - start < timeout:
        resp = await client.get(
            f"{EXZORK_API}/api/v1/mailboxes/{address}/messages?limit=5&offset=0",
            headers=_exzork_headers(),
        )
        if resp.status_code == 200:
            data = resp.json()
            messages = data.get("messages", []) if isinstance(data, dict) else data
            for msg in messages:
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                full = await client.get(
                    f"{EXZORK_API}/api/v1/messages/{msg_id}",
                    headers=_exzork_headers(),
                )
                if full.status_code == 200:
                    body = json.dumps(full.json())
                    match = re.search(
                        r"https://tasklet\.ai[^\s\"'<>]*[?&]token=([A-Za-z0-9_\-]+)", body
                    )
                    if match:
                        return match.group(1)
        await asyncio.sleep(3)
    raise TimeoutError(f"No magic link email for {address} within {timeout}s")


# ── Tasklet API ──────────────────────────────────────────────────────────────
async def tasklet_request_magic_link(client: httpx.AsyncClient, email: str, secret: str) -> bool:
    resp = await client.post(
        f"{TASKLET_API}/api/auth/magic-link/request",
        json={"email": email, "magicLinkSecret": secret},
    )
    return resp.status_code == 200


async def tasklet_verify_magic_link(client: httpx.AsyncClient, token: str) -> str | None:
    resp = await client.post(
        f"{TASKLET_API}/api/auth/magic-link/verify",
        json={"token": token},
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("pin")


async def tasklet_sign_in(client: httpx.AsyncClient, secret: str, pin: str) -> dict | None:
    resp = await client.post(
        f"{TASKLET_API}/api/signIn",
        json={
            "type": "magic_link",
            "magicLinkSecret": secret,
            "pin": pin,
            "attributionHistory": [],
            "allowDuplicate": False,
        },
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("type") != "success":
        return None
    return data


async def tasklet_create_org(client: httpx.AsyncClient, token: str, name: str) -> dict | None:
    resp = await client.post(
        f"{TASKLET_API}/api/organization/create",
        json={"name": f"{name}'s organization"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        return None
    return resp.json()


async def tasklet_claim_daily_bonus(client: httpx.AsyncClient, token: str, org_id: str) -> bool:
    resp = await client.post(
        f"{TASKLET_API}/api/billing/claimDailyBonus",
        json={"organizationId": org_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code == 200 and resp.json().get("claimed", False)


async def tasklet_get_credits(client: httpx.AsyncClient, token: str, org_id: str) -> int:
    resp = await client.post(
        f"{TASKLET_API}/api/billing/creditGrants",
        json={"organizationId": org_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        return 0
    return resp.json().get("totalAvailable", 0)


# ── Core Farm Logic ──────────────────────────────────────────────────────────
async def farm_one_account(idx: int) -> dict | None:
    ts = lambda: datetime.now().strftime("%H:%M:%S")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            email = await create_mailbox(client)
        except Exception as e:
            print(f"[{ts()}] [{idx}] FAIL mailbox: {e}", flush=True)
            return None
        print(f"[{ts()}] [{idx}] start  {email}", flush=True)

        secret = str(uuid.uuid4())
        ok = await tasklet_request_magic_link(client, email, secret)
        if not ok:
            print(f"[{ts()}] [{idx}] FAIL magic link request  {email}", flush=True)
            return None

        try:
            token = await poll_magic_link_email(client, email, timeout=60)
        except TimeoutError:
            print(f"[{ts()}] [{idx}] FAIL email timeout  {email}", flush=True)
            return None
        print(f"[{ts()}] [{idx}] got_token  {email}", flush=True)

        pin = await tasklet_verify_magic_link(client, token)
        if not pin:
            print(f"[{ts()}] [{idx}] FAIL verify  {email}", flush=True)
            return None

        sign_in = await tasklet_sign_in(client, secret, pin)
        if not sign_in:
            print(f"[{ts()}] [{idx}] FAIL signIn  {email}", flush=True)
            return None

        session_token = sign_in["sessionToken"]
        user_id = sign_in["userId"]
        print(f"[{ts()}] [{idx}] signed_in  user={user_id}", flush=True)

        headers = {"Authorization": f"Bearer {session_token}"}
        prof_resp = await client.post(f"{TASKLET_API}/api/profile", json=None, headers=headers)
        if prof_resp.status_code == 200:
            profile = prof_resp.json()
            name = profile.get("name", "User")
            orgs = profile.get("organizations", [])
        else:
            name = "User"
            orgs = []

        if orgs:
            org_id = orgs[0]["organizationId"]
            ws_id = orgs[0].get("workspaces", [{}])[0].get("workspaceId", "")
        else:
            org = await tasklet_create_org(client, session_token, name)
            if not org:
                print(f"[{ts()}] [{idx}] FAIL org create  {email}", flush=True)
                return None
            org_id = org["organizationId"]
            ws_id = org.get("workspaceId", "")
        print(f"[{ts()}] [{idx}] org={org_id}", flush=True)

        bonus = await tasklet_claim_daily_bonus(client, session_token, org_id)
        total_credits = await tasklet_get_credits(client, session_token, org_id)

        result = {
            "email": email,
            "userId": user_id,
            "sessionToken": session_token,
            "organizationId": org_id,
            "workspaceId": ws_id,
            "totalCredits": total_credits,
            "dailyBonusClaimed": bonus,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        print(f"[{ts()}] [{idx}] OK  {email}  credits={total_credits}", flush=True)
        return result


# ── Save Results ─────────────────────────────────────────────────────────────
def save_result(batch_dir: Path, result: dict) -> None:
    accounts_file = batch_dir / "accounts.json"
    data: list[dict] = []
    if accounts_file.is_file():
        try:
            data = json.loads(accounts_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.append(result)
    accounts_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── 9Router DB Inject ────────────────────────────────────────────────────────
def _resolve_9router_db() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "9router" / "db" / "data.sqlite"
    return Path.home() / ".9router" / "db" / "data.sqlite"


def inject_to_9router(result: dict) -> bool:
    try:
        import sqlite3
    except ImportError:
        return False

    db_path = _resolve_9router_db()
    if not db_path or not db_path.is_file():
        return False

    try:
        conn_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        name = result["email"].split("@")[0]
        data_blob = json.dumps({
            "apiKey": result["sessionToken"],
            "testStatus": "active",
            "providerSpecificData": {
                "workspaceId": result.get("workspaceId", ""),
                "organizationId": result.get("organizationId", ""),
                "userId": result.get("userId", ""),
                "totalCredits": result.get("totalCredits", 0),
            },
        })

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider = ?",
            ("tasklet",),
        ).fetchone()
        priority = (row[0] if row else 0) + 1

        db.execute(
            """INSERT INTO providerConnections(id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 data=excluded.data, updatedAt=excluded.updatedAt""",
            (conn_id, "tasklet", "apikey", name, result["email"], priority, 1, data_blob, now, now),
        )
        db.commit()
        db.close()
        return True
    except Exception as e:
        print(f"[9router] inject failed: {e}", flush=True)
        return False


# ── Batch Runner ─────────────────────────────────────────────────────────────
async def run_farm(count: int, concurrent: int) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [tasklet] farming {count} accounts (exzork domain={EXZORK_DOMAIN})", flush=True)

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = RESULTS_ROOT / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    fail_count = 0
    sem = asyncio.Semaphore(concurrent)

    async def worker(idx: int) -> None:
        nonlocal ok_count, fail_count
        async with sem:
            try:
                result = await asyncio.wait_for(
                    farm_one_account(idx),
                    timeout=ACCOUNT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                t = datetime.now().strftime("%H:%M:%S")
                print(f"[{t}] [{idx}] FAIL outer timeout", flush=True)
                result = None
            if result:
                save_result(batch_dir, result)
                if inject_to_9router(result):
                    t = datetime.now().strftime("%H:%M:%S")
                    print(f"[{t}] [{idx}] 9router injected", flush=True)
                ok_count += 1
                await _maybe_warp_after_success()
            else:
                fail_count += 1

    tasks = []
    for i in range(1, count + 1):
        tasks.append(asyncio.create_task(worker(i)))
        if i < count:
            await asyncio.sleep(random.uniform(0.5, 1.5))

    await asyncio.gather(*tasks, return_exceptions=True)

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] [DONE] ok={ok_count} fail={fail_count} batch={batch_id}", flush=True)
    print(f"[{ts}] [DONE] results: {batch_dir}", flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tasklet magic-link farm (pure HTTP)")
    p.add_argument("-n", "--count", type=int, default=int(_env("TASKLET_MAX_ACCOUNTS", "5")))
    p.add_argument("-c", "--concurrent", type=int, default=CONCURRENT)
    p.add_argument("-y", "--yes", action="store_true", help="Non-interactive")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    count = args.count
    concurrent = args.concurrent

    if not EXZORK_KEY:
        print("ERROR: EXZORK_API_KEY not set", flush=True)
        sys.exit(1)
    if not EXZORK_DOMAIN:
        print("ERROR: TASKLET_EXZORK_DOMAIN or EXZORK_DOMAIN not set", flush=True)
        sys.exit(1)

    if not args.yes:
        print(f"  Tasklet farm (magic link): {count} accounts, concurrent={concurrent}")
        print(f"  Domain: {EXZORK_DOMAIN}")
        confirm = input("  Start? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.", flush=True)
            return

    print(f"[tasklet] plan: n={count} c={concurrent}", flush=True)
    asyncio.run(run_farm(count, concurrent))


if __name__ == "__main__":
    main()
