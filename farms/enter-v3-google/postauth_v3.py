#!/usr/bin/env python3
"""
Enter/Converge post-auth v3 — pure-HTTP steps after the browser has produced a JWT.

Reversed from live HAR captures (2026-09-20):
  - suksesreferal.har            : full success with referral -> ek_ api key
  - newversion.har               : full success without referral
  - domainnotallow.har           : password-step 400 "email domain is not allowed to sign up"

Observed v3 sequence (all confirmed 200 against api.enter.pro):
  POST /code/api/v1/referral/claim?code=<gift>          (body null / {} ; nonfatal)
  GET  /code/api/v1/users/info
  GET  /code/api/v1/workspaces
  GET  /code/api/v1/onboarding/config                    (accepts flow_version v2 OR v3)
  POST /code/api/v1/onboarding/complete                  ({"role","team_size","build_intent"})
  POST /code/api/v1/workspaces/{id}/api-keys             ({"name","scope","reveal_policy"})
  GET  /code/api/v1/referral/rewards

Auth contract (verified live): Authorization: Bearer <jwt> + Origin/Referer.
X-Enter-GA-Context is NOT required (verified: 200 without it).

This module is standalone: it only needs an access token and env. It does not
import the legacy farm, so it can be unit-tested in isolation.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

APP_HOST = (os.environ.get("ENTER_APP_HOST") or "https://enter.converge.ai").rstrip("/")
API_HOST = (os.environ.get("ENTER_API_HOST") or "https://api.enter.pro").rstrip("/")
UA = (
    os.environ.get("ENTER_HTTP_UA")
    or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# HAR-proven defaults (suksesreferal.har [193] used exactly this shape).
ONBOARDING_ROLE = os.environ.get("ENTER_ONBOARDING_ROLE", "founder")
ONBOARDING_TEAM_SIZE = os.environ.get("ENTER_ONBOARDING_TEAM_SIZE", "just_me")
BUILD_INTENT = os.environ.get("ENTER_BUILD_INTENT", "other")

API_KEY_NAME = os.environ.get("ENTER_API_KEY_NAME", "farm")
API_KEY_SCOPE = os.environ.get("ENTER_API_KEY_SCOPE", "all")
API_KEY_REVEAL = os.environ.get("ENTER_API_KEY_REVEAL", "create_only")

# A fresh account starts with 100 bonus credits; a landed invitee referral makes
# it 200 (referral/rewards reports invitee_reward=100 + inviter_reward=100).
BASE_SIGNUP_CREDITS = float(os.environ.get("ENTER_BASE_SIGNUP_CREDITS", "100") or "100")
REFERRAL_BONUS_TOTAL = float(os.environ.get("ENTER_REFERRAL_BONUS_TOTAL", "200") or "200")


class PostAuthError(RuntimeError):
    """Terminal post-auth failure. Message is stable, safe to classify upstream."""


def _api(
    method: str,
    path: str,
    token: str,
    *,
    body: Any = None,
    proxy: str | None = None,
    timeout: int = 45,
    retries: int = 3,
) -> dict:
    """One API call. Raises PostAuthError on HTTP/parse failure."""
    url = f"{API_HOST}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Origin": APP_HOST,
        "Referer": f"{APP_HOST}/",
        "User-Agent": UA,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    opener = (
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        if proxy
        else urllib.request.build_opener()
    )
    last: str = ""
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "ignore")[:400]
            raise PostAuthError(f"API {method} {path} -> {e.code}: {err}") from e
        except urllib.error.URLError as e:
            last = f"{type(e).__name__}: {e}"
            if attempt + 1 >= retries:
                raise PostAuthError(f"API {method} {path} network: {last}") from e
            time.sleep(2 ** attempt)
    raise PostAuthError(f"API {method} {path} failed: {last}")


def _data(response: dict, label: str):
    if not isinstance(response, dict) or response.get("code") != 0:
        code = response.get("code") if isinstance(response, dict) else "?"
        msg = response.get("message") if isinstance(response, dict) else response
        raise PostAuthError(f"{label} failed (code={code} message={msg})")
    return response.get("data")


# ── Individual steps ─────────────────────────────────────────────────────────

def claim_referral(token: str, gift_code: str, *, proxy: str | None = None) -> dict:
    """POST referral/claim?code=<gift>. HAR: body null, 200 data:null."""
    query = urllib.parse.urlencode({"code": gift_code}) if gift_code else ""
    path = "/code/api/v1/referral/claim" + (f"?{query}" if query else "")
    # Body: send_json with empty dict — server accepts null ({}) per HAR.
    return _api("POST", path, token, body={}, proxy=proxy)


def get_user_info(token: str, *, proxy: str | None = None) -> dict:
    return _api("GET", "/code/api/v1/users/info", token, proxy=proxy)


def get_workspaces(token: str, *, proxy: str | None = None) -> dict:
    return _api("GET", "/code/api/v1/workspaces", token, proxy=proxy)


def get_onboarding_config(token: str, *, proxy: str | None = None) -> dict:
    return _api("GET", "/code/api/v1/onboarding/config", token, proxy=proxy)


def complete_onboarding(
    token: str,
    *,
    role: str,
    team_size: str,
    build_intent: str,
    proxy: str | None = None,
) -> dict:
    """HAR-proven 3-field payload. Extra fields are not sent."""
    return _api(
        "POST",
        "/code/api/v1/onboarding/complete",
        token,
        body={"role": role, "team_size": team_size, "build_intent": build_intent},
        proxy=proxy,
    )


def create_api_key(token: str, workspace_id: str, *, proxy: str | None = None) -> dict:
    body = {"name": API_KEY_NAME, "scope": API_KEY_SCOPE, "reveal_policy": API_KEY_REVEAL}
    return _api(
        "POST",
        f"/code/api/v1/workspaces/{workspace_id}/api-keys",
        token,
        body=body,
        proxy=proxy,
    )


def get_referral_rewards(token: str, *, proxy: str | None = None) -> dict:
    return _api("GET", "/code/api/v1/referral/rewards", token, proxy=proxy)


def get_workspace_credits(
    token: str, workspace_id: str, *, proxy: str | None = None
) -> dict:
    """GET workspaces/{id}/credits — the ONLY trustworthy referral-bonus signal.

    POST /referral/claim answers 200 "claim referral code successfully" even when
    the server silently withholds the invitee bonus (proven in
    HTTPToolkit_2026-08-03.har: claim 200, credits_balance.total stayed 100).
    So bonus landing must be verified here, not from the claim response.
    """
    return _api(
        "GET", f"/code/api/v1/workspaces/{workspace_id}/credits", token, proxy=proxy
    )


# ── Orchestration ────────────────────────────────────────────────────────────

def post_auth_setup(
    access_token: str,
    gift_code: str = "",
    *,
    proxy: str | None = None,
) -> dict:
    """Run the full v3 post-auth and return a result dict.

    Returns:
      {
        "workspace_id": str,
        "api_key": {id, key, name, scope, reveal_policy},   # raw data from create
        "referral_claim": dict | None,
        "referral_claim_error": str | None,
        "user_info": dict,
        "onboarding": dict | None,
        "onboarding_skipped": bool,
        "flow_version": str,
        "rewards": dict | None,
        "referral_code": str,
        "user_id": str,
        "credits_total": float | None,
        "invitee_bonus_landed": bool | None,
      }

    Raises PostAuthError on a terminal failure (workspace missing, api key
    missing, email not verified). Referral claim is non-fatal by design.
    """
    if not access_token or not str(access_token).strip():
        raise PostAuthError("post-auth called without access token")

    out: dict[str, Any] = {}

    # 1. referral claim — nonfatal (official client continues on failure)
    if gift_code:
        try:
            claim = claim_referral(access_token, gift_code, proxy=proxy)
            _data(claim, "referral claim")
            out["referral_claim"] = claim
        except PostAuthError as e:
            out["referral_claim"] = None
            out["referral_claim_error"] = str(e)
    else:
        out["referral_claim"] = None
        out["referral_claim_error"] = None

    # 2. user info — must be verified, merge candidate must not be pending
    info = get_user_info(access_token, proxy=proxy)
    udata = _data(info, "users info")
    if not isinstance(udata, dict) or udata.get("must_verify_email") is not False:
        raise PostAuthError("user email is not verified")
    merge_action = udata.get("merge_action") or "no_action"
    merge_candidate = udata.get("merge_candidate_id")
    merge_block = udata.get("merge_block_reason")
    if merge_action == "candidate_pending" and merge_candidate and not merge_block:
        raise PostAuthError("user merge candidate requires resolution")
    out["user_info"] = info

    # This account's OWN referral code — the seed for the next chain link.
    user_obj = udata.get("user") if isinstance(udata.get("user"), dict) else {}
    out["referral_code"] = str(user_obj.get("referral_code") or "").strip()
    out["user_id"] = str(user_obj.get("user_id") or "").strip()
    out["user_name"] = str(user_obj.get("name") or "").strip()

    # 3. workspace — fail closed if none
    ws_resp = get_workspaces(access_token, proxy=proxy)
    ws_data = _data(ws_resp, "workspaces")
    items = ws_data.get("workspaces") if isinstance(ws_data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise PostAuthError("workspace list is empty")
    workspace_id = str(items[0].get("id") or items[0].get("workspace_id") or "").strip()
    if not workspace_id:
        raise PostAuthError("workspace id is empty")
    out["workspaces"] = ws_resp
    out["workspace_id"] = workspace_id

    # 4. onboarding config — accept v2 or v3 (live = v3)
    cfg_resp = get_onboarding_config(access_token, proxy=proxy)
    cfg = _data(cfg_resp, "onboarding config")
    if not isinstance(cfg, dict):
        raise PostAuthError("onboarding config is invalid")
    flow_version = str(cfg.get("flow_version") or "")
    if flow_version not in ("v2", "v3"):
        raise PostAuthError(f"unsupported onboarding flow_version={flow_version!r}")
    out["onboarding_config"] = cfg_resp
    out["flow_version"] = flow_version

    # 5. onboarding complete — only when not already completed
    if cfg.get("completed"):
        out["onboarding"] = None
        out["onboarding_skipped"] = True
    else:
        ob = complete_onboarding(
            access_token,
            role=ONBOARDING_ROLE,
            team_size=ONBOARDING_TEAM_SIZE,
            build_intent=BUILD_INTENT,
            proxy=proxy,
        )
        ob_data = _data(ob, "onboarding completion")
        if not isinstance(ob_data, dict) or ob_data.get("completed") is not True:
            # accept either explicit success flag or a plain completed=true
            if ob_data.get("success") is False:
                raise PostAuthError("onboarding did not complete")
            raise PostAuthError("onboarding did not complete")
        out["onboarding"] = ob
        out["onboarding_skipped"] = False

    # 6. api key — required for a successful farm result
    created = create_api_key(access_token, workspace_id, proxy=proxy)
    cdata = _data(created, "api key creation")
    if not isinstance(cdata, dict):
        raise PostAuthError("api key response is invalid")
    key = str(cdata.get("key") or "").strip()
    key_id = str(cdata.get("id") or "").strip()
    if not key or not key_id:
        raise PostAuthError("api key response missing key or id")
    out["api_key"] = created

    # 7. verify the referral bonus ACTUALLY landed (claim lies; credits don't)
    out["credits_total"] = None
    out["invitee_bonus_landed"] = None
    try:
        cred = get_workspace_credits(access_token, workspace_id, proxy=proxy)
        cbal = _data(cred, "workspace credits")
        if isinstance(cbal, dict):
            total = (cbal.get("credits_balance") or {}).get("total")
            if total is None:
                total = cbal.get("credits")
            if total is not None:
                out["credits_total"] = float(total)
                out["invitee_bonus_landed"] = float(total) >= REFERRAL_BONUS_TOTAL
        out["credits"] = cred
    except PostAuthError as e:
        out["credits_error"] = str(e)

    # 8. rewards — best effort
    try:
        out["rewards"] = get_referral_rewards(access_token, proxy=proxy)
    except PostAuthError:
        out["rewards"] = None

    return out


if __name__ == "__main__":  # tiny self-check: import + shape only (no network)
    import argparse

    ap = argparse.ArgumentParser(description="Enter post-auth v3 (self-check)")
    ap.add_argument("--token", help="access token to run a real post-auth (mutating!)")
    ap.add_argument("--gift", default="", help="referral gift code")
    ap.add_argument("--proxy", default="")
    args = ap.parse_args()

    if not args.token:
        print("module loads OK; defaults:",
              {"role": ONBOARDING_ROLE, "team_size": ONBOARDING_TEAM_SIZE,
               "build_intent": BUILD_INTENT, "api_host": API_HOST})
        raise SystemExit(0)

    result = post_auth_setup(args.token, args.gift, proxy=args.proxy or None)
    print(json.dumps(
        {"workspace_id": result["workspace_id"],
         "flow_version": result["flow_version"],
         "api_key_id": (result.get("api_key", {}).get("data") or {}).get("id"),
         "has_key": bool((result.get("api_key", {}).get("data") or {}).get("key")),
         "claim_ok": result.get("referral_claim") is not None,
         "claim_error": result.get("referral_claim_error"),
         "onboarding_skipped": result.get("onboarding_skipped")},
        indent=2))
