#!/usr/bin/env python3
"""
fluffy self-hosted mailbox adapter for the Enter v3 farm.

fluffy is a Cloudflare Worker + D1 temp-mail service (fluffyhowl/temp-mail) that
receives mail through Cloudflare Email Routing, so the receiving domain's MX is
route1/2/3.mx.cloudflare.net instead of a disposable-mail host.

Endpoints (verified live):
  GET  /api/domains                        -> {"domains":["dom.tld",...]}
  POST /api/inboxes        {}              -> {"email":..,"meta":{..},"inboxToken":..}
  GET  /api/messages?address=user@dom      -> {"email":..,"inbox":{..},"messages":[{id,from,subject,preview,receivedAt}]}
  GET  /api/messages/{id}                  -> {id,from:{name,address},subject,body,bodyType,htmlAvailable,receivedAt}

Message list carries only a `preview`; the full 6-digit code is in the detail
`body`. Enter's OTP mail: from "noreply@converge.ai", subject "Verify your email".
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = (os.environ.get("ENTER_FLUFFY_API") or "https://temp-mail.nzryeyo.workers.dev").rstrip("/")
API_KEY = (os.environ.get("ENTER_FLUFFY_API_KEY") or "").strip()
PREFERRED_DOMAIN = (os.environ.get("ENTER_FLUFFY_DOMAIN") or "").strip().lstrip("@").lower()
OTP_FROM = (os.environ.get("ENTER_OTP_FROM") or "converge.ai").lower()
OTP_SUBJECT_HINT = (os.environ.get("ENTER_OTP_SUBJECT") or "verify").lower()
# Cloudflare Bot Fight Mode answers non-browser signatures with error 1010.
USER_AGENT = (
    os.environ.get("ENTER_FLUFFY_UA")
    or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CODE_TAG_RE = re.compile(r"<code[^>]*>\s*(\d{6})\s*</code>", re.IGNORECASE)
_ANCHORED_RE = re.compile(
    r"(?:code|otp|verification|verify)[^0-9]{0,40}?(\d{6})(?!\d)", re.IGNORECASE
)
_HEXISH = re.compile(r"[0-9a-fA-F]{7,}")


class MailboxError(RuntimeError):
    pass


def _call(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": BASE,
        "Referer": BASE + "/",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        if e.code == 403 and "1010" in detail:
            raise MailboxError(
                f"fluffy blocked by Cloudflare Bot Fight Mode (1010) at {BASE}"
            ) from e
        raise MailboxError(f"fluffy {method} {path} -> {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise MailboxError(f"fluffy {method} {path} unreachable: {e}") from e
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise MailboxError(f"fluffy {method} {path} bad JSON: {raw[:200]}") from e


def list_domains() -> list[str]:
    resp = _call("GET", "/api/domains")
    doms = resp.get("domains") or []
    return [str(d).strip().lower() for d in doms if isinstance(d, str) and d]


def pick_domain() -> str:
    if PREFERRED_DOMAIN:
        return PREFERRED_DOMAIN
    doms = list_domains()
    if not doms:
        raise MailboxError("fluffy reports no domains")
    return doms[0]


def create_address(domain: str | None = None) -> str:
    dom = (domain or "").strip().lower() or PREFERRED_DOMAIN
    body: dict[str, str] = {"domain": dom} if dom else {}
    resp = _call("POST", "/api/inboxes", body)
    addr = str(resp.get("email") or "").strip().lower()
    if not addr or "@" not in addr:
        raise MailboxError(f"fluffy returned no address: {str(resp)[:150]}")
    return addr


def _messages(address: str) -> list[dict]:
    q = urllib.parse.urlencode({"address": address})
    resp = _call("GET", f"/api/messages?{q}")
    msgs = resp.get("messages") or []
    return [m for m in msgs if isinstance(m, dict)]


def _detail(message_id: str) -> dict:
    resp = _call("GET", f"/api/messages/{urllib.parse.quote(message_id, safe='')}")
    return resp if isinstance(resp, dict) else {}


def _received_ts(msg: dict) -> float | None:
    raw = str(msg.get("receivedAt") or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _from_address(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("address") or value.get("name") or "").lower()
    return str(value or "").lower()


def _extract_otp(subject: str, body: str) -> str | None:
    for source in (body, subject):
        m = _CODE_TAG_RE.search(source)
        if m:
            return m.group(1)
    for source in (body, subject):
        m = _ANCHORED_RE.search(source)
        if m:
            return m.group(1)
    for source in (body, subject):
        for m in _OTP_RE.finditer(source):
            code = m.group(1)
            if code == "000000":
                continue
            window = source[max(0, m.start() - 24):m.start() + 12]
            if _HEXISH.search(window):
                continue
            return code
    return None


def wait_for_otp(
    address: str,
    *,
    since_ts: float | None = None,
    timeout: int = 180,
    poll: int = 3,
    log=None,
) -> str:
    """Return the 6-digit OTP for Enter, or raise MailboxError on timeout."""
    deadline = time.monotonic() + max(30, timeout)
    seen_ids: set[str] = set()
    while time.monotonic() < deadline:
        try:
            msgs = _messages(address)
        except MailboxError as e:
            if log:
                log(f"fluffy list error: {e}")
            msgs = []
        for msg in msgs:
            mid = str(msg.get("id") or "")
            frm = _from_address(msg.get("from") or msg.get("from_address"))
            subj = str(msg.get("subject") or "")
            if OTP_FROM and OTP_FROM not in frm:
                continue
            if OTP_SUBJECT_HINT and OTP_SUBJECT_HINT not in subj.lower() and "code" not in subj.lower():
                continue
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            received = _received_ts(msg)
            if received and since_ts and received < since_ts - 5:
                continue
            body = str(msg.get("preview") or msg.get("body") or msg.get("text_body") or "")
            code = _extract_otp(subj, body)
            if not code and mid:
                try:
                    det = _detail(mid)
                    subj = str(det.get("subject") or subj)
                    body = str(
                        det.get("body") or det.get("text_body") or det.get("preview") or body
                    )
                    if not body and det.get("html_body"):
                        body = re.sub(r"<[^>]+>", " ", str(det.get("html_body")))
                    frm = _from_address(det.get("from")) or frm
                    code = _extract_otp(subj, body)
                except MailboxError as e_det:
                    if log:
                        log(f"fluffy detail error: {e_det}")
            if code:
                if log:
                    log(f"OTP {code} from {frm or '?'} t+{int(time.monotonic() - (deadline - max(30, timeout)))}s")
                return code
        time.sleep(poll)
    raise MailboxError(f"OTP timeout after {timeout}s for {address}")


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if action == "probe":
        print("base:", BASE)
        print("domains:", list_domains()[:8])
        print("minted:", create_address())
    elif action == "read" and len(sys.argv) > 2:
        for m in _messages(sys.argv[2]):
            print(m.get("id"), m.get("from"), "|", m.get("subject"), "| otp=", _extract_otp(str(m.get("subject") or ""), str(m.get("preview") or "")))
