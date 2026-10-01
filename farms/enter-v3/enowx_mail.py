#!/usr/bin/env python3
"""
enowX disposable-mailbox adapter for the Enter v3 farm.

enowX is a local Go service (http://127.0.0.1:1430) that mints addresses on
domains routed through Cloudflare Email Routing and serves the inbox over a
small JSON API. This adapter replaces the legacy mail providers.

Endpoints (verified live):
  GET  /api/mail/domains                              -> {"data":{"domains":[{name,...}]}}
  POST /api/mail/addresses   {"domain": "..."}        -> {"data":{"address":"user@dom"}}
  GET  /api/mail/messages?address=user@dom            -> {"data":{"messages":[...]}}
  GET  /api/mail/watch?address=..&since_id=..&timeout_sec=..  (long-poll)

Message shape: {id, address, from, subject, text_body, html_body, received_at}
Enter's OTP mail: from="noreply@converge.ai", subject="Verify your email",
a 6-digit code inside html_body.
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = (os.environ.get("ENTER_ENOWX_BASE") or "http://127.0.0.1:1430").rstrip("/")
PREFERRED_DOMAIN = (os.environ.get("ENTER_ENOWX_DOMAIN") or "").strip().lstrip("@").lower()
OTP_FROM = (os.environ.get("ENTER_OTP_FROM") or "converge.ai").lower()
OTP_SUBJECT_HINT = (os.environ.get("ENTER_OTP_SUBJECT") or "verify").lower()
# Dot-trick on a catch-all domain: Enter's Auth0 Action normalizes only a fixed
# provider list (gmail/googlemail/icloud/outlook/hotmail/yahoo/yandex/ya.ru), so
# dots on any other domain survive as a distinct address.
DOT_MODE = (os.environ.get("ENTER_ENOWX_DOT") or "0").strip().lower() in ("1", "true", "yes", "on")
DOT_LOCAL_LEN = max(8, min(24, int(os.environ.get("ENTER_ENOWX_DOT_LOCAL_LEN", "12") or "12")))
USED_FILE = Path(
    os.environ.get("ENTER_ENOWX_USED_FILE")
    or str(Path(__file__).resolve().parent / "results" / "enowx_used.txt")
)

_used: set[str] = set()
_used_lock = threading.Lock()


def _load_used() -> None:
    if _used:
        return
    with _used_lock:
        if _used or not USED_FILE.is_file():
            return
        for line in USED_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            e = line.strip().lower()
            if e and not e.startswith("#"):
                _used.add(e)


def _persist(addr: str) -> None:
    USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USED_FILE.open("a", encoding="utf-8") as f:
        f.write(addr + "\n")

_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CODE_TAG_RE = re.compile(r"<code[^>]*>\s*(\d{6})\s*</code>", re.IGNORECASE)
_ANCHORED_RE = re.compile(
    r"(?:code|otp|verification|verify)[^0-9]{0,40}?(\d{6})(?!\d)", re.IGNORECASE
)
_HEXISH = re.compile(r"[0-9a-fA-F]{7,}")


class MailboxError(RuntimeError):
    pass


def _call(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise MailboxError(f"enowx {method} {path} -> {e.code}: {e.read().decode('utf-8','replace')[:200]}") from e
    except urllib.error.URLError as e:
        raise MailboxError(f"enowx {method} {path} unreachable: {e}") from e
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise MailboxError(f"enowx {method} {path} bad JSON: {raw[:200]}") from e


def _data(resp: dict) -> dict:
    d = resp.get("data")
    if not isinstance(d, dict):
        raise MailboxError(f"enowx unexpected envelope: {str(resp)[:200]}")
    return d


def list_domains() -> list[str]:
    d = _data(_call("GET", "/api/mail/domains"))
    doms = d.get("domains") or []
    return [str(x.get("name")).lower() for x in doms if isinstance(x, dict) and x.get("name")]


def pick_domain() -> str:
    if PREFERRED_DOMAIN:
        return PREFERRED_DOMAIN
    doms = list_domains()
    if not doms:
        raise MailboxError("enowx reports no domains")
    return doms[0]


def create_address(domain: str | None = None) -> str:
    dom = (domain or "").strip().lower() or pick_domain()
    if DOT_MODE:
        return _dotted_address(dom)
    d = _data(_call("POST", "/api/mail/addresses", {"domain": dom}))
    addr = str(d.get("address") or "").strip().lower()
    if not addr or "@" not in addr:
        raise MailboxError(f"enowx returned no address for {dom}: {str(d)[:150]}")
    return addr


def _dotted_address(domain: str) -> str:
    """Mint a dotted local part on a catch-all domain (no server-side mint).

    generator.email (enowX's backend) serves any address on a routed domain, so
    the address only has to be chosen locally; the dots keep each account
    distinct for providers that do not normalize this domain.
    """
    _load_used()
    for _ in range(200):
        raw = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(DOT_LOCAL_LEN))
        gaps = len(raw) - 1
        mask = random.getrandbits(gaps) or (1 << random.randrange(gaps))
        out: list[str] = []
        for i, ch in enumerate(raw):
            out.append(ch)
            if i < gaps and (mask >> i) & 1:
                out.append(".")
        local = "".join(out)
        if "." not in local:
            continue
        addr = f"{local}@{domain}"
        with _used_lock:
            if addr in _used:
                continue
            _used.add(addr)
            _persist(addr)
            return addr
    raise MailboxError("could not mint a unique dotted address")


def _messages(address: str) -> list[dict]:
    q = urllib.parse.urlencode({"address": address})
    d = _data(_call("GET", f"/api/mail/messages?{q}"))
    msgs = d.get("messages") or []
    return [m for m in msgs if isinstance(m, dict)]


def _extract_otp(msg: dict) -> str | None:
    html = str(msg.get("html_body") or "")
    text = str(msg.get("text_body") or "")
    # 1) Enter renders the real code inside <code ...>NNNNNN</code>.
    for source in (html, text):
        m = _CODE_TAG_RE.search(source)
        if m:
            return m.group(1)
    # 2) anchor word ("code"/"otp"/"verify") right before a 6-digit run.
    for source in (html, text):
        for m in _ANCHORED_RE.finditer(source):
            return m.group(1)
    # 3) fallback: a standalone 6-digit run that is NOT inside a hex/tracking blob.
    for source in (html, text):
        for m in _OTP_RE.finditer(source):
            code = m.group(1)
            if code == "000000":
                continue
            s = m.start()
            window = source[max(0, s - 24):s + 12]
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
    seen_ids: set = set()
    while time.monotonic() < deadline:
        try:
            msgs = _messages(address)
        except MailboxError:
            msgs = []
        for msg in msgs:
            mid = msg.get("id")
            frm = str(msg.get("from") or "").lower()
            subj = str(msg.get("subject") or "").lower()
            if OTP_FROM and OTP_FROM not in frm:
                continue
            if OTP_SUBJECT_HINT and OTP_SUBJECT_HINT not in subj and "code" not in subj:
                continue
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            code = _extract_otp(msg)
            if code:
                if log:
                    log(f"OTP {code} from {msg.get('from')}")
                return code
        time.sleep(poll)
    raise MailboxError(f"OTP timeout after {timeout}s for {address}")


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if action == "probe":
        print("base:", BASE)
        print("domains:", list_domains()[:8])
        addr = create_address()
        print("minted:", addr)
    elif action == "read" and len(sys.argv) > 2:
        for m in _messages(sys.argv[2]):
            print(m.get("id"), m.get("from"), "|", m.get("subject"), "| otp=", _extract_otp(m))
