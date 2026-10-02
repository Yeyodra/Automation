#!/usr/bin/env python3
"""
Railway relay deployer, turns a farmed Railway *cookie* into a TCP egress proxy.

This does NOT use the `railway` (Google-OAuth) farm and it does NOT use an API
token. It reads the `cookie` string that `farm.py` already saved in
`results/batch_*/accounts.json` and drives Railway's cookie-authenticated
internal GraphQL to build, per account:

    project -> 1..5 services (GitHub repo) -> deploy -> relay vars -> TCP proxy

Each service gets its own TCP proxy and therefore its own Railway egress IP
(verified live: 4 services in one project -> 4 distinct IPs), so one farmed
account yields up to 5 relays. Only `projectCreate` is rate limited (1 / 30s per
account); `serviceCreate` is not, so the wait is paid once per account.

Endpoint shape:  http://<AUTH_USER>:<AUTH_PASS>@<domain>:<proxyPort>

The whole mutation sequence below was verified live against a farmed account
(see README.md, section "Relay egress (deploy via cookie)"). Introspection is
disabled on Railway, so nothing here probes `__schema`.

This module is intentionally standalone (only `httpx` + stdlib) so it can run
with the hub venv without touching `farm.py`. `farm.py` is never modified.

Config: RAILWAY_RELAY_* env keys (see .env.example).
Run:    python -m jobs run railway-relay -- -n 1 -y
        python farms/railway/deploy_relay.py -n 1 -y --proxy-out ../other/proxy.txt
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ── Paths / env bootstrap (hub .env wins; farm .env fills gaps) ───────────────
_ROOT = Path(__file__).resolve().parent          # farms/railway
_HUB = _ROOT.parent.parent                       # Automation/
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    _env_path = _ROOT / ".env"
    if _env_path.is_file():
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


# ── Config ────────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


RESULTS_ROOT = Path(_env("RAILWAY_RESULTS_DIR", str(_ROOT / "results")))
DEFAULT_ENDPOINTS = RESULTS_ROOT / "relay_endpoints.json"

RELAY_REPO = _env("RAILWAY_RELAY_REPO", "NetroIndonesia/buff-relay")
RELAY_BRANCH = _env("RAILWAY_RELAY_BRANCH", "main")
RELAY_PORT = _env_int("RAILWAY_RELAY_PORT", 8080)
RELAY_AUTH_USER = _env("RAILWAY_RELAY_AUTH_USER", "relay")
RELAY_AUTH_PASS = _env("RAILWAY_RELAY_AUTH_PASS", "")
RELAY_ENDPOINTS = Path(_env("RAILWAY_RELAY_ENDPOINTS", str(DEFAULT_ENDPOINTS)))
RELAY_PROXY_OUT = _env("RAILWAY_RELAY_PROXY_OUT", "")
RELAY_ACCOUNT_GAP = max(0.0, _env_float("RAILWAY_RELAY_ACCOUNT_GAP", 35.0))

# Relays (= services = distinct egress IPs) per account. Verified live: 4 services
# in one HOBBY project each got a different Railway egress IP. HARD plan ceiling is
# 5 services/project, so clamp to 1..5.
RELAY_PER_ACCOUNT = min(5, max(1, _env_int("RAILWAY_RELAY_PER_ACCOUNT", 5)))

# Railway rate limit: 1 project per 30s per account. Wait a bit above that when
# the API answers "too quickly".
PROJECT_RATE_WAIT = max(30.0, _env_float("RAILWAY_RELAY_PROJECT_WAIT", 32.0))
PROJECT_RATE_TRIES = max(1, _env_int("RAILWAY_RELAY_PROJECT_TRIES", 5))
GQL_TIMEOUT = max(10.0, _env_float("RAILWAY_RELAY_TIMEOUT", 60.0))
PROXY_POLL_TRIES = max(1, _env_int("RAILWAY_RELAY_PROXY_TRIES", 20))
PROXY_POLL_GAP = max(1.0, _env_float("RAILWAY_RELAY_PROXY_GAP", 5.0))

# ── GraphQL endpoint constants (same host the farm already talks to) ───────────
GQL_URL = "https://backboard.railway.com/graphql/internal"
ORIGIN = "https://railway.com"
REFERER = "https://railway.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# ── GraphQL documents (verbatim, verified live) ───────────────────────────────
Q_ME_WORKSPACES = """
query { me { workspaces { id name plan } } }
""".strip()

M_PROJECT_CREATE = """
mutation projectCreate($input: ProjectCreateInput!) {
  projectCreate(input: $input) { id name environments { edges { node { id name } } } }
}
""".strip()

M_SERVICE_CREATE = """
mutation serviceCreate($input: ServiceCreateInput!) {
  serviceCreate(input: $input) { id name projectId }
}
""".strip()

M_STAGE = """
mutation stageEnvironmentChanges($environmentId: String!, $payload: EnvironmentConfig!, $merge: Boolean) {
  environmentStageChanges(environmentId: $environmentId, input: $payload, merge: $merge) { id }
}
""".strip()

M_COMMIT = """
mutation environmentPatchCommitStaged($environmentId: String!, $message: String, $skipDeploys: Boolean) {
  environmentPatchCommitStaged(environmentId: $environmentId, commitMessage: $message, skipDeploys: $skipDeploys)
}
""".strip()

M_VARS = """
mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) {
  variableCollectionUpsert(input: $input)
}
""".strip()

# NOTE: no $projectId here, an unused variable is a hard GraphQL error.
Q_NETWORKING = """
query networking($environmentId: String!, $serviceId: String!) {
  tcpProxies(environmentId: $environmentId, serviceId: $serviceId) {
    id applicationPort proxyPort domain syncStatus
  }
  serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
    latestDeployment { id status }
  }
}
""".strip()


# ── Logging (hub contract) ────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(attempt: int, step: str, message: str, email: str = "") -> None:
    """Hub contract: [HH:MM:SS] [<n>] <step>  <msg>  <email>"""
    suffix = f"  {email}" if email else ""
    print(f"[{_ts()}] [{attempt}] {step}  {message}{suffix}", flush=True)


def slog(tag: str, message: str) -> None:
    print(f"[{_ts()}] [{tag}] {message}", flush=True)


# ── GraphQL transport ─────────────────────────────────────────────────────────
def _gql(
    cookie: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout: float = GQL_TIMEOUT,
    retries: int = 3,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """POST one GraphQL document with the cookie header.

    Railway answers HTTP 200 even for GraphQL errors, so callers must inspect
    the returned body's `errors`. Returns None when the request never completed.
    """
    say = log or (lambda _m: None)
    try:
        import httpx
    except ImportError:
        say("deploy_relay: httpx not installed in hub venv")
        return None

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "User-Agent": USER_AGENT,
        "Cookie": cookie,
    }
    body: dict[str, Any] = {"query": query}
    if variables is not None:
        body["variables"] = variables

    last_err = ""
    for attempt in range(1, max(1, retries) + 1):
        try:
            resp = httpx.post(
                GQL_URL,
                json=body,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    say(f"gql: {last_err} (no retry)")
                    return None
            else:
                data = resp.json()
                if isinstance(data, dict):
                    return data
                last_err = "non-object JSON"
        except Exception as e:  # network / proxy / JSON
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(min(4.0, 0.8 * attempt))
    say(f"gql: failed after {retries} tries ({last_err})")
    return None


def _gql_data(resp: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return {}


def _error_text(resp: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if isinstance(resp, dict) and isinstance(resp.get("errors"), list):
        for e in resp["errors"]:
            if isinstance(e, dict):
                msg = str(e.get("message") or "").strip()
                if msg:
                    parts.append(msg)
    return " | ".join(parts)[:240]


def _is_rate_limited(text: str) -> bool:
    low = text.lower()
    return "too quickly" in low or "rate limit" in low or "too many" in low


# ── Account loading ───────────────────────────────────────────────────────────
def load_accounts_with_cookie(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read every `results/batch_*/accounts.json` and keep accounts with a cookie.

    `path` may be a results dir (globs `batch_*/accounts.json`), a single
    accounts.json file, or a list of either. Dedup is by email (first wins).
    Returns `[{"email", "cookie", "workspace_id", "plan", "batch"}]`.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    files: list[Path] = []

    roots: list[Path]
    if path is None:
        roots = [RESULTS_ROOT]
    elif isinstance(path, (list, tuple, set)):
        roots = [Path(p) for p in path]
    else:
        roots = [Path(path)]

    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.glob("batch_*/accounts.json")))
            direct = root / "accounts.json"
            if direct.is_file():
                files.append(direct)

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            cookie = str(row.get("cookie") or "").strip()
            if not cookie:
                continue
            email = str(row.get("google_email") or row.get("email") or "").strip()
            key = email.lower() or cookie[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "email": email,
                    "cookie": cookie,
                    "workspace_id": str(row.get("workspace_id") or "").strip(),
                    "plan": str(row.get("plan") or "").strip(),
                    "batch": f.parent.name,
                }
            )
    return out


# ── Single-account deploy ─────────────────────────────────────────────────────
def _resolve_workspace_id(cookie: str, log: Callable[[str], None]) -> str:
    resp = _gql(cookie, Q_ME_WORKSPACES, log=log)
    err = _error_text(resp)
    if err:
        log(f"me: {err}")
        return ""
    data = _gql_data(resp)
    me = data.get("me") if isinstance(data.get("me"), dict) else {}
    workspaces = me.get("workspaces") if isinstance(me.get("workspaces"), list) else []
    for ws in workspaces:
        if isinstance(ws, dict) and ws.get("id"):
            return str(ws["id"])
    return ""


def _create_project(
    cookie: str, workspace_id: str, log: Callable[[str], None]
) -> tuple[str, str, str]:
    """Create a project, retrying through the 1-project/30s rate limit.

    Returns (project_id, environment_id, error).
    """
    variables = {"input": {"workspaceId": workspace_id}}
    last = ""
    for attempt in range(1, PROJECT_RATE_TRIES + 1):
        resp = _gql(cookie, M_PROJECT_CREATE, variables, log=log)
        err = _error_text(resp)
        if resp is not None and not err:
            node = _gql_data(resp).get("projectCreate") or {}
            if isinstance(node, dict) and node.get("id"):
                pid = str(node["id"])
                eid = ""
                envs = node.get("environments") or {}
                edges = envs.get("edges") if isinstance(envs, dict) else None
                if isinstance(edges, list):
                    for edge in edges:
                        n = edge.get("node") if isinstance(edge, dict) else None
                        if isinstance(n, dict) and n.get("id"):
                            eid = str(n["id"])
                            break
                return pid, eid, ""
            last = "projectCreate returned no id"
        else:
            last = err or "projectCreate failed"
        if _is_rate_limited(last) and attempt < PROJECT_RATE_TRIES:
            log(f"project: rate limited, waiting {int(PROJECT_RATE_WAIT)}s")
            time.sleep(PROJECT_RATE_WAIT)
            continue
        break
    return "", "", last


def _create_service(
    cookie: str, project_id: str, repo: str, branch: str, log: Callable[[str], None]
) -> tuple[str, str]:
    variables = {
        "input": {
            "projectId": project_id,
            "source": {"repo": repo},
            "branch": branch,
            "environmentId": None,
        }
    }
    resp = _gql(cookie, M_SERVICE_CREATE, variables, log=log)
    err = _error_text(resp)
    if err:
        return "", err
    node = _gql_data(resp).get("serviceCreate") or {}
    if isinstance(node, dict) and node.get("id"):
        return str(node["id"]), ""
    return "", "serviceCreate returned no id"


def _stage_and_commit(
    cookie: str,
    environment_id: str,
    service_id: str,
    payload: dict[str, Any],
    message: str,
    log: Callable[[str], None],
) -> str:
    """Stage one environment config patch and commit it. Returns error ('' ok)."""
    stage_vars = {
        "environmentId": environment_id,
        "payload": payload,
        "merge": True,
    }
    resp = _gql(cookie, M_STAGE, stage_vars, log=log)
    err = _error_text(resp)
    if err:
        return f"stage: {err}"

    commit_vars = {
        "environmentId": environment_id,
        "message": message,
        "skipDeploys": False,
    }
    resp = _gql(cookie, M_COMMIT, commit_vars, log=log)
    err = _error_text(resp)
    if err:
        return f"commit: {err}"
    return ""


def _set_variables(
    cookie: str,
    project_id: str,
    environment_id: str,
    service_id: str,
    auth_user: str,
    auth_pass: str,
    log: Callable[[str], None],
) -> str:
    variables = {
        "input": {
            "projectId": project_id,
            "environmentId": environment_id,
            "serviceId": service_id,
            "variables": {"AUTH_USER": auth_user, "AUTH_PASS": auth_pass},
        }
    }
    resp = _gql(cookie, M_VARS, variables, log=log)
    err = _error_text(resp)
    if err:
        return err
    if _gql_data(resp).get("variableCollectionUpsert") is not True:
        return "variableCollectionUpsert != true"
    return ""


def _read_proxy(
    cookie: str,
    environment_id: str,
    service_id: str,
    log: Callable[[str], None],
) -> tuple[str, int, str, str]:
    """Poll for the TCP proxy + latest deployment status.

    Returns (domain, proxy_port, deployment_status, error).
    """
    variables = {"environmentId": environment_id, "serviceId": service_id}
    domain, proxy_port, status = "", 0, ""
    for attempt in range(1, PROXY_POLL_TRIES + 1):
        resp = _gql(cookie, Q_NETWORKING, variables, log=log)
        err = _error_text(resp)
        if err:
            return "", 0, status, err
        data = _gql_data(resp)

        inst = data.get("serviceInstance")
        if isinstance(inst, dict):
            dep = inst.get("latestDeployment")
            if isinstance(dep, dict) and dep.get("status"):
                status = str(dep["status"])

        proxies = data.get("tcpProxies")
        if isinstance(proxies, list) and proxies:
            p = proxies[0]
            if isinstance(p, dict) and p.get("domain") and p.get("proxyPort"):
                domain = str(p["domain"])
                try:
                    proxy_port = int(p["proxyPort"])
                except (TypeError, ValueError):
                    proxy_port = 0
                if domain and proxy_port:
                    return domain, proxy_port, status, ""

        if attempt < PROXY_POLL_TRIES:
            time.sleep(PROXY_POLL_GAP)
    return domain, proxy_port, status, "tcp proxy not visible yet"


def _mask_endpoint(user: str, domain: str, port: int) -> str:
    return f"http://{user}:***@{domain}:{port}" if domain and port else ""


def _endpoint_url(user: str, password: str, domain: str, port: int) -> str:
    if not (domain and port):
        return ""
    return f"http://{user}:{password}@{domain}:{port}"


def _blank_result(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "project_id": "",
        "environment_id": "",
        "service_id": "",
        "domain": "",
        "proxy_port": 0,
        "endpoint_url": "",
        "deployment_status": "",
        "ok": False,
        "errors": [],
    }


def deploy_relays_for_account(
    cookie: str,
    *,
    count: int = RELAY_PER_ACCOUNT,
    repo: str = RELAY_REPO,
    branch: str = RELAY_BRANCH,
    auth_user: str = RELAY_AUTH_USER,
    auth_pass: str = RELAY_AUTH_PASS,
    port: int = RELAY_PORT,
    log: Callable[[str], None] | None = None,
    email: str = "",
    attempt: int = 1,
) -> list[dict[str, Any]]:
    """Build ONE project and up to `count` relay services (services = egress IPs).

    Verified live on a HOBBY workspace: 4 services inside one project each got a
    *different* Railway egress IP, and `serviceCreate` is NOT subject to the
    1-project/30s limit (only `projectCreate` is), so the wait is paid once per
    account no matter how many services we add. HARD plan ceiling is 5 services
    per project, so `count` is clamped to 1..5.

    Returns one dict per service, in `index` order (1..N), each shaped like the
    old single-relay result plus `index`. Fail-soft per service: a service that
    fails is returned with `ok=False` + `errors`, and the loop moves on, so a
    partial account (e.g. service 3 dies) still yields `ok=True` rows 1-2.

    Never logs `auth_pass` or the cookie; only the masked endpoint is printed.
    """
    say = log or (lambda _m: None)
    n = min(5, max(1, int(count)))

    def step(name: str, msg: str) -> None:
        _log(attempt, name, msg, email)

    # (0) workspace (shared by every service in this account)
    step("project", "resolving workspace")
    ws_id = _resolve_workspace_id(cookie, say)
    if not ws_id:
        msg = "no workspace id (cookie invalid or expired?)"
        step("fail", "no workspace id")
        rows = [_blank_result(i) for i in range(1, n + 1)]
        for r in rows:
            r["errors"].append(msg)
        return rows

    # (1) ONE project for the whole account
    pid, eid, err = _create_project(cookie, ws_id, say)
    if err or not pid:
        msg = f"project: {err or 'no project id'}"
        step("fail", f"project create failed: {err or 'no id'}")
        rows = [_blank_result(i) for i in range(1, n + 1)]
        for r in rows:
            r["errors"].append(msg)
        return rows
    step("project", f"created {pid} env={eid or '?'} (services=1..{n})")

    rows: list[dict[str, Any]] = []
    for index in range(1, n + 1):
        r = _blank_result(index)
        r["project_id"] = pid
        r["environment_id"] = eid
        rows.append(r)

        # (2) service from the relay repo
        sid, err = _create_service(cookie, pid, repo, branch, say)
        if err or not sid:
            r["errors"].append(f"service: {err or 'no service id'}")
            step("fail", f"[{index}/{n}] service create failed: {err or 'no id'}")
            continue
        r["service_id"] = sid
        step("service", f"[{index}/{n}] created {sid} from {repo}@{branch}")

        # (3) deploy (stage + commit)
        deploy_payload = {
            "services": {sid: {"isCreated": True, "source": {"branch": branch, "repo": repo}}}
        }
        err = _stage_and_commit(cookie, eid, sid, deploy_payload, "deploy relay", say)
        if err:
            r["errors"].append(f"deploy: {err}")
            step("fail", f"[{index}/{n}] deploy failed: {err}")
            continue
        step("deploy", f"[{index}/{n}] staged + committed source")

        # (4) relay credentials. Distinct password per service unless pinned.
        pass_for_service = auth_pass or secrets.token_urlsafe(12)
        err = _set_variables(cookie, pid, eid, sid, auth_user, pass_for_service, say)
        if err:
            r["errors"].append(f"variables: {err}")
            step("fail", f"[{index}/{n}] variables failed: {err}")
            continue
        step("variables", f"[{index}/{n}] set AUTH_USER={auth_user} AUTH_PASS=***")

        # (5) TCP proxy (config patch, keyed by the relay's application port)
        tcp_payload = {"services": {sid: {"networking": {"tcpProxies": {str(port): {}}}}}}
        err = _stage_and_commit(cookie, eid, sid, tcp_payload, "add tcp proxy", say)
        if err:
            r["errors"].append(f"tcp_proxy: {err}")
            step("fail", f"[{index}/{n}] tcp proxy failed: {err}")
            continue
        step("tcp_proxy", f"[{index}/{n}] staged + committed tcpProxies[{port}]")

        # (6) read the endpoint back
        domain, proxy_port, status, err = _read_proxy(cookie, eid, sid, say)
        r["domain"] = domain
        r["proxy_port"] = proxy_port
        r["deployment_status"] = status
        if err or not (domain and proxy_port):
            r["errors"].append(f"tcp_proxy: {err or 'no proxy returned'}")
            step("fail", f"[{index}/{n}] no tcp proxy endpoint ({err or 'empty'})")
            continue

        r["endpoint_url"] = _endpoint_url(auth_user, pass_for_service, domain, proxy_port)
        r["ok"] = True
        step("OK", f"[{index}/{n}] {_mask_endpoint(auth_user, domain, proxy_port)} status={status or '?'}")

    return rows


def deploy_relay_for_account(
    cookie: str,
    *,
    repo: str = RELAY_REPO,
    branch: str = RELAY_BRANCH,
    auth_user: str = RELAY_AUTH_USER,
    auth_pass: str = RELAY_AUTH_PASS,
    port: int = RELAY_PORT,
    log: Callable[[str], None] | None = None,
    email: str = "",
    attempt: int = 1,
) -> dict[str, Any]:
    """Back-compat: one project, one service. Returns the first relay result dict.

    Thin wrapper over `deploy_relays_for_account(count=1)`; the old signature and
    result shape are unchanged so existing callers keep working.
    """
    rows = deploy_relays_for_account(
        cookie,
        count=1,
        repo=repo,
        branch=branch,
        auth_user=auth_user,
        auth_pass=auth_pass,
        port=port,
        log=log,
        email=email,
        attempt=attempt,
    )
    return rows[0] if rows else _blank_result(1)


# ── Batch ─────────────────────────────────────────────────────────────────────
def _write_outputs(
    results: list[dict[str, Any]],
    endpoints_file: Path,
    proxy_out: str | None,
    proxy_rows: list[dict[str, Any]] | None = None,
) -> None:
    """Write the JSON + txt manifest, and optionally append bare proxy URLs.

    `results` is the full flat list (many rows per account) used for the
    JSON/txt manifest. `proxy_rows` is the subset of rows added in this call, so
    `--proxy-out` gets exactly one URL per relay (no duplicates across accounts);
    when omitted it falls back to `results`.
    """
    endpoints_file.parent.mkdir(parents=True, exist_ok=True)
    endpoints_file.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n"
    )

    txt = endpoints_file.with_suffix(".txt")
    lines = [
        "# project_id|service_id|domain:port|proxy_url|deployment_status|created_at"
    ]
    for r in results:
        host = f"{r['domain']}:{r['proxy_port']}" if r.get("domain") else ""
        lines.append(
            "|".join(
                [
                    str(r.get("project_id", "")),
                    str(r.get("service_id", "")),
                    host,
                    str(r.get("endpoint_url", "")),
                    str(r.get("deployment_status", "")),
                    str(r.get("created_at", "")),
                ]
            )
        )
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    if proxy_out:
        p = Path(proxy_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        src = results if proxy_rows is None else proxy_rows
        urls = [r["endpoint_url"] for r in src if r.get("ok") and r.get("endpoint_url")]
        if urls:
            # 0600: the file holds a password. Never commit it.
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(urls) + "\n")
            finally:
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass


def deploy_batch(
    accounts: list[dict[str, Any]],
    *,
    repo: str = RELAY_REPO,
    branch: str = RELAY_BRANCH,
    auth_user: str = RELAY_AUTH_USER,
    auth_pass: str = RELAY_AUTH_PASS,
    port: int = RELAY_PORT,
    per_account: int = RELAY_PER_ACCOUNT,
    gap: float = RELAY_ACCOUNT_GAP,
    endpoints_file: Path = RELAY_ENDPOINTS,
    proxy_out: str | None = RELAY_PROXY_OUT or None,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Deploy relays for every account, `gap` seconds apart (1 project / 30s).

    `per_account` services (= distinct egress IPs) are built inside ONE project
    per account. The `gap` is applied *between accounts* only: the 30s project
    rate limit is paid once per account, and `serviceCreate` has no such limit.
    Returns a flat list of per-relay dicts (N rows per account).
    """
    say = log or (lambda _m: None)
    n = min(5, max(1, int(per_account)))
    results: list[dict[str, Any]] = []

    for i, acct in enumerate(accounts, start=1):
        if i > 1 and gap > 0:
            say(f"waiting {int(gap)}s before next account (project rate limit)")
            time.sleep(gap)

        user_for_account = auth_user or "relay"
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = deploy_relays_for_account(
            acct["cookie"],
            count=n,
            repo=repo,
            branch=branch,
            auth_user=user_for_account,
            auth_pass=auth_pass,  # empty => distinct random password per service
            port=port,
            log=say,
            email=acct.get("email", ""),
            attempt=i,
        )
        for r in rows:
            r["email"] = acct.get("email", "")
            r["workspace_id"] = acct.get("workspace_id", "")
            r["created_at"] = created_at
            results.append(r)

        _write_outputs(results, Path(endpoints_file), proxy_out, proxy_rows=rows)

    ok = sum(1 for r in results if r.get("ok"))
    _log(0, "DONE", f"ok={ok} fail={len(results) - ok} -> {endpoints_file}")
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Railway relay deployer (cookie-based)")
    p.add_argument("-n", "--count", type=int, default=0, help="max accounts (0 = all)")
    p.add_argument("-y", "--yes", action="store_true", help="non-interactive")
    p.add_argument(
        "--per-account",
        type=int,
        default=RELAY_PER_ACCOUNT,
        help="relays/services per account, each a distinct egress IP (1..5, default 5)",
    )
    p.add_argument("--repo", default=RELAY_REPO, help="GitHub repo owner/name")
    p.add_argument("--branch", default=RELAY_BRANCH, help="git branch")
    p.add_argument("--port", type=int, default=RELAY_PORT, help="relay app port (tcp proxy key)")
    p.add_argument("--user", default=RELAY_AUTH_USER, help="relay AUTH_USER")
    p.add_argument("--pass", dest="auth_pass", default=RELAY_AUTH_PASS, help="relay AUTH_PASS")
    p.add_argument("--endpoints-file", default=str(RELAY_ENDPOINTS), help="json output path")
    p.add_argument("--proxy-out", default=RELAY_PROXY_OUT, help="append bare proxy URLs here")
    p.add_argument("--gap", type=float, default=RELAY_ACCOUNT_GAP, help="seconds between accounts")
    p.add_argument("--accounts-file", default="", help="results dir or accounts.json to read")
    p.add_argument("--dry-run", action="store_true", help="print plan, no API calls")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src = args.accounts_file or None
    accounts = load_accounts_with_cookie(src)
    if args.count and args.count > 0:
        accounts = accounts[: args.count]

    slog("START", f"relay deploy: {len(accounts)} account(s) with cookie")
    if not accounts:
        slog("DONE", "no farmed accounts with a cookie (run the railway farm first)")
        return 1

    if args.dry_run:
        per_account = min(5, max(1, args.per_account))
        slog("PLAN", f"repo={args.repo} branch={args.branch} port={args.port}")
        slog("PLAN", f"auth_user={args.user or 'relay'} auth_pass={'***' if args.auth_pass else '<random per service>'}")
        slog("PLAN", f"per_account={per_account} relay(s)/account, 1 project each (max 5)")
        slog("PLAN", f"gap={int(args.gap)}s endpoints={args.endpoints_file}")
        slog("PLAN", f"proxy_out={args.proxy_out or '(none)'}")
        for i, a in enumerate(accounts, start=1):
            _log(
                i,
                "plan",
                f"1 project -> {per_account}x(service->deploy->variables->tcp_proxy:{args.port})",
                a["email"],
            )
        slog("DONE", "dry-run: no API calls made, nothing written")
        return 0

    if not args.yes:
        try:
            ans = input(
                f"Deploy {len(accounts)} account(s) x {min(5, max(1, args.per_account))} relay(s)? [y/N] "
            ).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            slog("DONE", "aborted")
            return 1

    deploy_batch(
        accounts,
        repo=args.repo,
        branch=args.branch,
        auth_user=args.user,
        auth_pass=args.auth_pass,
        port=args.port,
        per_account=args.per_account,
        gap=args.gap,
        endpoints_file=Path(args.endpoints_file),
        proxy_out=args.proxy_out or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
