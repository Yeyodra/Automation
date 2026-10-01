#!/usr/bin/env python3
"""Gmail dot-trick mailbox for the Enter v3 farm.

Gmail ignores dots in the local part, so akuncursorke1@gmail.com and
a.k.u.n.c.u.r.s.o.r.k.e.1@gmail.com reach the same inbox. This adapter mints a
random dotted variant per account and reads the OTP from that single Gmail
inbox over IMAP, matching the exact dotted address so concurrent variants do
not steal each other's code.
"""
from __future__ import annotations

import imaplib
import json
import os
import random
import re
import secrets
import threading
import time
from email import message_from_bytes
from pathlib import Path

BASE = (os.environ.get("ENTER_GMAIL_BASE") or os.environ.get("ENTER_IMAP_USER") or "").strip().lower()
IMAP_USER = (os.environ.get("ENTER_IMAP_USER") or BASE).strip()
IMAP_PASS = (os.environ.get("ENTER_IMAP_PASS") or "").replace(" ", "")
IMAP_HOST = (os.environ.get("ENTER_IMAP_HOST") or "imap.gmail.com").strip()
IMAP_PORT = int((os.environ.get("ENTER_IMAP_PORT") or "993").strip() or 993)
USED_FILE = Path(
    os.environ.get("ENTER_DOTTRICK_USED_FILE")
    or str(Path(__file__).resolve().parent / "results" / "dottrick_used.txt")
)
DOT_DOMAIN = (os.environ.get("ENTER_DOTTRICK_DOMAIN") or "").strip().lower()
DOT_MODE = (os.environ.get("ENTER_DOTTRICK_MODE") or "dot").strip().lower()

_used: set[str] = set()
_used_lock = threading.Lock()
_claimed_codes: set[str] = set()
_claimed_lock = threading.Lock()

_CODE_TAG_RE = re.compile(r"<code[^>]*>\s*(\d{6})\s*</code>", re.I)
_ANCHORED_RE = re.compile(r"(?:code|otp|verification|verify)[^0-9]{0,40}?(\d{6})(?!\d)", re.I)
_STANDALONE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_HEXISH = re.compile(r"[0-9a-fA-F]{7,}")


class MailboxError(RuntimeError):
    pass


def _split_base() -> tuple[str, str]:
    if not BASE or "@" not in BASE:
        raise MailboxError("ENTER_GMAIL_BASE (or ENTER_IMAP_USER) must be a full Gmail address")
    user, _, domain = BASE.partition("@")
    user = user.split("+", 1)[0]
    return user, (DOT_DOMAIN or domain)


def _load_used() -> None:
    global _used
    if _used:
        return
    with _used_lock:
        if _used:
            return
        if USED_FILE.is_file():
            for line in USED_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                e = line.strip().lower()
                if e and not e.startswith("#"):
                    _used.add(e)


def _persist(addr: str) -> None:
    USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USED_FILE.open("a", encoding="utf-8") as f:
        f.write(addr + "\n")


def _dotted_variant(user: str) -> str:
    if len(user) < 2:
        raise MailboxError("Gmail local part too short for dot trick")
    gaps = len(user) - 1
    for _ in range(64):
        mask = random.getrandbits(gaps)
        if mask == 0:
            mask = 1 << random.randrange(gaps)
        out = []
        for i, ch in enumerate(user):
            out.append(ch)
            if i < gaps and (mask >> i) & 1:
                out.append(".")
        cand = "".join(out)
        if "." in cand:
            return cand
    raise MailboxError("could not build dotted variant")


def create_address() -> str:
    """Mint a unique Gmail variant (dot trick or plus tag) and reserve it."""
    user, domain = _split_base()
    _load_used()
    for _ in range(500):
        if DOT_MODE == "plus":
            local = f"{user}+{secrets.token_hex(5)}"
        else:
            local = _dotted_variant(user)
        addr = f"{local}@{domain}"
        with _used_lock:
            if addr in _used:
                continue
            _used.add(addr)
            _persist(addr)
            return addr
    raise MailboxError("exhausted Gmail variants (all reserved)")


def _plain(body: str) -> str:
    text = re.sub(r"<style[\s\S]*?</style>", " ", body or "", flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    return re.sub(r"<[^>]+>", " ", text)


def _extract_otp(html: str, text: str) -> str | None:
    for source in (html, text):
        m = _CODE_TAG_RE.search(source)
        if m:
            return m.group(1)
    for source in (html, text):
        m = _ANCHORED_RE.search(source)
        if m:
            return m.group(1)
    for source in (html, text):
        for m in _STANDALONE_RE.finditer(source):
            code = m.group(1)
            if code == "000000":
                continue
            if _HEXISH.search(source[max(0, m.start() - 24):m.start() + 12]):
                continue
            return code
    return None


def _body_of(msg) -> tuple[str, str]:
    html = ""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not text:
                try:
                    text = part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    text = ""
            elif ct == "text/html" and not html:
                try:
                    html = part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    html = ""
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", "replace")
        except Exception:
            body = str(msg.get_payload() or "")
        if "<html" in body.lower():
            html = body
        else:
            text = body
    return html, text


_GMAIL_ALT_DOMAINS = ("gmail.com", "googlemail.com")


def _recipient_matches(msg, target: str) -> bool:
    target = target.lower()
    local, _, domain = target.partition("@")
    alternates = [f"{local}@{d}" for d in _GMAIL_ALT_DOMAINS] if domain in _GMAIL_ALT_DOMAINS else []
    for hdr in ("To", "Delivered-To", "X-Original-To", "X-Forwarded-To", "Cc", "Envelope-To"):
        try:
            value = (msg.get(hdr, "") or "").lower()
        except Exception:
            continue
        if target in value or any(alt in value for alt in alternates):
            return True
    return False


def wait_for_otp(address: str, *, since_ts: float | None = None, timeout: int = 180,
                 poll: float = 4.0, log=None) -> str:
    """Poll the shared Gmail inbox for the OTP addressed to this exact variant."""
    if not IMAP_PASS:
        raise MailboxError("ENTER_IMAP_PASS (Gmail app password) is required")
    log = log or (lambda m: None)
    target = address.lower()
    deadline = time.monotonic() + max(30, timeout)
    seen: set[bytes] = set()
    log(f"[IMAP] waiting OTP -> {address} (timeout={timeout}s)")

    while time.monotonic() < deadline:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(IMAP_USER, IMAP_PASS)
            mail.select("INBOX")
            for criterion in ('(FROM "converge.ai")', '(FROM "auth0.com")', "(SUBJECT \"Verify\")", "ALL"):
                try:
                    status, data = mail.search(None, criterion)
                    ids = data[0].split() if data and data[0] else []
                except Exception:
                    ids = []
                for mid in reversed(ids[-30:]):
                    if mid in seen:
                        continue
                    try:
                        status, raw = mail.fetch(mid, "(RFC822)")
                    except Exception:
                        continue
                    if not raw or not raw[0] or not isinstance(raw[0], tuple):
                        seen.add(mid)
                        continue
                    msg = message_from_bytes(raw[0][1])
                    if not _recipient_matches(msg, target):
                        seen.add(mid)
                        continue
                    html, text = _body_of(msg)
                    code = _extract_otp(html, text)
                    if not code:
                        seen.add(mid)
                        continue
                    with _claimed_lock:
                        if code in _claimed_codes:
                            seen.add(mid)
                            continue
                        _claimed_codes.add(code)
                    try:
                        mail.store(mid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                    log(f"[IMAP] OTP {code} for {address}")
                    return code
        except Exception as e:
            log(f"[IMAP] error: {type(e).__name__}: {e}")
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
        time.sleep(poll)
    raise MailboxError(f"OTP timeout after {timeout}s for {address}")


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if action == "probe":
        print("base:", BASE or "(unset)")
        print("imap:", IMAP_HOST, IMAP_PORT, "user:", IMAP_USER)
        print("minted:", create_address())
    elif action == "read" and len(sys.argv) > 2:
        print("otp:", wait_for_otp(sys.argv[2], timeout=30, log=print))
