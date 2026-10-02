"""Railway internal GraphQL client, cookie-authenticated, HTTP-only.

Railway's dashboard talks to:

    POST https://backboard.railway.com/graphql/internal?q=<operationName>

with a JSON body `{"query": ..., "variables": {...}, "operationName": "..."}` and
NO bearer token: the session is carried entirely by the `rw.*` cookies that the
Google OAuth callback sets (see ../README.md). That is why this module only needs
a cookie header string, not a token.

Queries are lifted verbatim from the capture HAR and stored in `_har_queries.json`
by the extraction snippet documented in README.md, do not hand-edit them,
re-extract instead.

Everything here is fail-soft: callers get `None` on failure and decide whether
that is fatal. A GraphQL hiccup must never throw away an account whose cookies
were already obtained.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

GQL_URL = "https://backboard.railway.com/graphql/internal"
ORIGIN = "https://railway.com"
REFERER = "https://railway.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

_QUERIES_PATH = Path(__file__).resolve().parent / "_har_queries.json"
_QUERIES: dict[str, dict[str, Any]] | None = None


def load_queries(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load the HAR-extracted {operationName: {query, variables}} map (cached)."""
    global _QUERIES
    if _QUERIES is not None and path is None:
        return _QUERIES
    p = Path(path) if path is not None else _QUERIES_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if path is None:
        _QUERIES = data
    return data


def operation_names() -> list[str]:
    return sorted(load_queries().keys())


def _payload(operation: str, variables: dict[str, Any] | None) -> dict[str, Any] | None:
    spec = load_queries().get(operation)
    if not spec or not spec.get("query"):
        return None
    merged: dict[str, Any] = {}
    base = spec.get("variables")
    if isinstance(base, dict):
        merged.update(base)
    if variables:
        merged.update(variables)
    return {
        "query": spec["query"],
        "variables": merged,
        "operationName": operation,
    }


def railway_gql(
    cookie_header: str,
    operation: str,
    variables: dict[str, Any] | None = None,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Run one Railway GraphQL operation with the given cookie header.

    Returns the parsed JSON body (Railway answers HTTP 200 even for GraphQL
    errors, so callers should inspect `errors`), or None when the request never
    completed / was not JSON.
    """
    say = log or (lambda _m: None)
    body = _payload(operation, variables)
    if body is None:
        say(
            f"graphql: no stored query for operation {operation!r} "
            f"(have: {', '.join(operation_names()) or 'none'})"
        )
        return None

    try:
        import httpx
    except ImportError:
        say("graphql: httpx not installed in hub venv")
        return None

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "User-Agent": USER_AGENT,
        "Cookie": cookie_header,
    }

    last_err = ""
    for attempt in range(1, max(1, retries) + 1):
        try:
            kwargs: dict[str, Any] = {
                "params": {"q": operation},
                "json": body,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": True,
            }
            if proxy:
                kwargs["proxy"] = proxy
            resp = httpx.post(GQL_URL, **kwargs)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                # 4xx (except 429) will not fix itself; stop early.
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    say(f"graphql {operation}: {last_err} (no retry)")
                    return None
            else:
                data = resp.json()
                if isinstance(data, dict):
                    return data
                last_err = "non-object JSON"
        except Exception as e:  # network / proxy / JSON errors
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(min(4.0, 0.8 * attempt))
    say(f"graphql {operation}: failed after {retries} tries ({last_err})")
    return None


def gql_data(resp: dict[str, Any] | None) -> dict[str, Any]:
    """`data` sub-object of a GraphQL response (empty dict when missing)."""
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return {}


def gql_errors(resp: dict[str, Any] | None) -> list[dict[str, Any]]:
    if isinstance(resp, dict) and isinstance(resp.get("errors"), list):
        return [e for e in resp["errors"] if isinstance(e, dict)]
    return []


def error_text(resp: dict[str, Any] | None) -> str:
    """Flat, human-readable error string for logs ('' when clean)."""
    parts: list[str] = []
    for e in gql_errors(resp):
        msg = str(e.get("message") or "").strip()
        if msg:
            parts.append(msg)
    return " | ".join(parts)[:240]
