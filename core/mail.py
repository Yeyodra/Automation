"""Shared email generation + IMAP OTP (domain / plus_trick).

Reused by farms (outlook, later grok). No gptmail here — product farms own that.

Usage:
    from core.mail import ImapConfig, UsedEmailStore, generate_email, read_otp_imap, extract_digit_otp

    cfg = ImapConfig.from_prefix("OUTLOOK_")
    store = UsedEmailStore(results_dir / "used_emails.txt")
    store.load(results_dir)
    email = generate_email(cfg, store, mode="domain")
    code = read_otp_imap(cfg, email, timeout=120, extract=extract_digit_otp)
"""
from __future__ import annotations

import imaplib
import json
import os
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from email import message_from_bytes
from pathlib import Path
from typing import Callable


_ALPHANUM = string.ascii_lowercase + string.digits
_claimed_otps: set[str] = set()
_claimed_lock = threading.Lock()


def crypto_local_part(length: int = 16) -> str:
    n = max(4, min(32, int(length)))
    return "".join(secrets.choice(_ALPHANUM) for _ in range(n))


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


@dataclass(frozen=True)
class ImapConfig:
    user: str
    password: str
    host: str = "imap.gmail.com"
    port: int = 993
    email_domain: str = ""
    gmail_base: str = ""
    local_len: int = 16

    @classmethod
    def from_prefix(cls, prefix: str) -> ImapConfig:
        """Build from PREFIX_IMAP_* + PREFIX_EMAIL_DOMAIN / GMAIL_BASE (hub-mapped)."""
        p = prefix if prefix.endswith("_") else prefix + "_"
        user = _env(f"{p}IMAP_USER")
        password = _env(f"{p}IMAP_PASS").replace(" ", "")
        host = _env(f"{p}IMAP_HOST", "imap.gmail.com") or "imap.gmail.com"
        port_raw = _env(f"{p}IMAP_PORT", "993") or "993"
        try:
            port = int(port_raw)
        except ValueError:
            port = 993
        domain = _env(f"{p}EMAIL_DOMAIN").lstrip("@")
        gmail = (_env(f"{p}GMAIL_BASE") or user).lower()
        try:
            local_len = max(10, min(32, int(_env(f"{p}EMAIL_LOCAL_LEN", "16") or "16")))
        except ValueError:
            local_len = 16
        return cls(
            user=user,
            password=password,
            host=host,
            port=port,
            email_domain=domain,
            gmail_base=gmail,
            local_len=local_len,
        )

    def require_login(self) -> None:
        if not self.user or not self.password:
            raise RuntimeError("IMAP_USER and IMAP_PASS required")

    def require_mode(self, mode: str) -> None:
        self.require_login()
        m = (mode or "domain").lower()
        if m == "domain" and not self.email_domain:
            raise RuntimeError("EMAIL_DOMAIN required for domain mode")
        if m == "plus_trick" and not (self.gmail_base or self.user):
            raise RuntimeError("GMAIL_BASE or IMAP_USER required for plus_trick")
        if m not in ("domain", "plus_trick"):
            raise RuntimeError(f"unsupported email mode {mode!r} (use domain|plus_trick)")

    def ping(self) -> bool:
        """Login + select INBOX. Raises on failure."""
        self.require_login()
        mail = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            mail.login(self.user, self.password)
            mail.select("INBOX")
            return True
        finally:
            try:
                mail.logout()
            except Exception:
                pass


@dataclass
class UsedEmailStore:
    path: Path
    emails: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def load(self, results_root: Path | None = None) -> int:
        """Load used_emails.txt + optional batch accounts.json under results_root."""
        with self._lock:
            self.emails.clear()
            if self.path.is_file():
                for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                    e = line.strip().lower()
                    if e and not e.startswith("#"):
                        self.emails.add(e)
            if results_root and results_root.is_dir():
                self.emails |= _emails_from_accounts_json(results_root / "accounts.json")
                for batch in results_root.glob("batch_*"):
                    if batch.is_dir():
                        self.emails |= _emails_from_accounts_json(batch / "accounts.json")
            return len(self.emails)

    def reserve(self, email: str) -> bool:
        """Claim email if unused. Persists to path. Returns False if already used."""
        key = email.lower().strip()
        if not key:
            return False
        with self._lock:
            if key in self.emails:
                return False
            self.emails.add(key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(key + "\n")
            return True

    def __contains__(self, email: str) -> bool:
        return email.lower().strip() in self.emails

    def __len__(self) -> int:
        return len(self.emails)


def _emails_from_accounts_json(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    e = (row.get("email") or "").lower().strip()
                    if e:
                        out.add(e)
    except Exception:
        pass
    return out


def generate_email(
    cfg: ImapConfig,
    store: UsedEmailStore,
    *,
    mode: str = "domain",
    max_tries: int = 200,
) -> str:
    """Unique catch-all or plus-trick address; reserves in store immediately."""
    mode = (mode or "domain").lower()
    cfg.require_mode(mode)
    for _ in range(max_tries):
        if mode == "domain":
            name = crypto_local_part(cfg.local_len)
            addr = f"{name}@{cfg.email_domain.lstrip('@')}"
        else:
            base = cfg.gmail_base or cfg.user
            user, _, domain = base.partition("@")
            user = user.split("+", 1)[0]
            tag_len = max(10, min(20, cfg.local_len))
            tag = crypto_local_part(tag_len)
            addr = f"{user}+{tag}@{domain}"
        if store.reserve(addr):
            return addr
    raise RuntimeError("Could not generate unique email after max tries")


# ── OTP extractors ───────────────────────────────────────────────────────────

ExtractFn = Callable[[str, str], str | None]


def extract_digit_otp(subject: str, body: str, *, digits: int = 6) -> str | None:
    """Generic N-digit OTP (Microsoft / many providers). Prefer subject then body."""
    pat = re.compile(rf"\b(\d{{{digits}}})\b")
    for text in (subject or "", _plain_body(body)):
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def extract_digit_otp_flex(subject: str, body: str) -> str | None:
    """4–8 digit OTP; try 6 first (most common)."""
    for n in (6, 7, 8, 4, 5):
        code = extract_digit_otp(subject, body, digits=n)
        if code:
            return code
    return None


def extract_microsoft_otp(subject: str, body: str) -> str | None:
    """Microsoft account verify mail: 'security code: 023211' / Verify your email address."""
    plain = _plain_body(body)
    subj = subject or ""
    # Prefer explicit "security code:" (Gmail + Cloudflare forward body)
    for text in (plain, subj):
        m = re.search(
            r"security\s*code\s*[:\s]+(\d{4,8})\b",
            text,
            re.I,
        )
        if m:
            return m.group(1)
        m = re.search(
            r"(?:verification|verify|one[- ]?time|otp)\s*(?:code)?\s*[:\s]+(\d{4,8})\b",
            text,
            re.I,
        )
        if m:
            return m.group(1)
    # Subject is often just "Verify your email address" — code only in body
    if re.search(r"verify\s+your\s+email|microsoft\s+account", subj, re.I) or re.search(
        r"microsoft\s+account|verify\s+your\s+email|security\s+code",
        plain,
        re.I,
    ):
        return extract_digit_otp_flex(subj, plain)
    return extract_digit_otp_flex(subj, plain)


def extract_xai_otp(subject: str, body: str) -> str | None:
    """xAI style XXX-XXX (same rules as farms/grok)."""
    subj_re = re.compile(r"^\s*([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI\s+confirmation", re.I)
    code_re = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", re.I)
    m = subj_re.search(subject or "")
    if m and _plausible_xai(m.group(1)):
        return m.group(1).upper()
    for m in code_re.finditer(subject or ""):
        if _plausible_xai(m.group(1)):
            return m.group(1).upper()
    plain = _plain_body(body)
    for m in code_re.finditer(plain):
        if _plausible_xai(m.group(1)):
            return m.group(1).upper()
    m = re.search(r"\b(\d{6})\b", plain)
    return m.group(1) if m else None


def _plausible_xai(code: str) -> bool:
    code = (code or "").upper().strip()
    if not re.fullmatch(r"[A-Z0-9]{3}-[A-Z0-9]{3}", code):
        return False
    left, right = code.split("-", 1)
    if re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right):
        return False
    if re.fullmatch(r"\d+", left) and re.fullmatch(r"\d+", right):
        return False
    if code in {"PER-100", "RGB-255", "PX-16", "EM-16", "REM-16", "MS-300", "MS-200"}:
        return False
    return True


def _plain_body(body: str) -> str:
    plain = body or ""
    plain = re.sub(r"<style[\s\S]*?</style>", " ", plain, flags=re.I)
    plain = re.sub(r"<script[\s\S]*?</script>", " ", plain, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    return plain


def _msg_body(msg) -> str:
    if msg.is_multipart():
        body = ""
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    body = ""
                if body:
                    return body
            if ct == "text/html" and not body:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    body = ""
        return body
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:
        return str(msg.get_payload() or "")


def _header_blob(msg) -> str:
    """All recipient-ish headers (Cloudflare→Gmail often drops alias from To:)."""
    keys = (
        "To",
        "Delivered-To",
        "X-Original-To",
        "X-Forwarded-To",
        "X-Forwarded-For",
        "Cc",
        "Envelope-To",
        "Apparently-To",
        "Received",
        "Return-Path",
        "From",
        "Sender",
        "Subject",
    )
    parts: list[str] = []
    for k in keys:
        try:
            raw = msg.get_all(k) if k == "Received" else [msg.get(k, "")]
        except Exception:
            raw = [msg.get(k, "")]
        for v in raw or []:
            if v:
                parts.append(str(v))
    return " ".join(parts).lower()


def _recipient_hit(msg, body: str, target_email: str) -> bool:
    """Match CF/Gmail 'Recipients' = full local@domain (anti-swap between aliases).

    Priority: To / Delivered-To / X-Original-To / X-Forwarded-To / Cc, then body.
    Do NOT match local-part alone — concurrent workers would steal each other's OTP.
    """
    target_lower = target_email.lower().strip()
    if not target_lower or "@" not in target_lower:
        return False
    # Envelope recipients (what CF dashboard shows as Recipients)
    recip = " ".join(
        filter(
            None,
            [
                msg.get("To", ""),
                msg.get("Delivered-To", ""),
                msg.get("X-Original-To", ""),
                msg.get("X-Forwarded-To", ""),
                msg.get("Cc", ""),
                msg.get("Envelope-To", ""),
                msg.get("Apparently-To", ""),
            ],
        )
    ).lower()
    if target_lower in recip:
        return True
    # Body may quote the address
    if target_lower in (body or "").lower():
        return True
    # Received chain sometimes embeds RCPT TO:<alias@domain>
    blob = _header_blob(msg)
    if target_lower in blob:
        return True
    return False


def _ms_sender(msg) -> bool:
    blob = " ".join(
        filter(
            None,
            [
                msg.get("From", ""),
                msg.get("Sender", ""),
                msg.get("Reply-To", ""),
            ],
        )
    ).lower()
    return any(
        x in blob
        for x in (
            "accountprotection.microsoft.com",
            "microsoft.com",
            "account.microsoft.com",
            "outlook.com",
            "account-security-noreply",
        )
    )


def read_otp_imap(
    cfg: ImapConfig,
    target_email: str,
    *,
    timeout: int = 180,
    since_ts: float | None = None,
    extract: ExtractFn = extract_digit_otp_flex,
    from_filter: str = "",
    subject_hint: str = "",
    poll_s: float = 4.0,
    log: Callable[[str], None] | None = None,
    # When True: Microsoft verify mail may match by sender+code even if CF stripped To:
    microsoft_mode: bool = False,
) -> str | None:
    """Poll IMAP for OTP to target_email. Claim codes so concurrent workers don't share.

    from_filter: optional IMAP FROM substring (e.g. 'microsoft.com', 'x.ai').
    subject_hint: also SEARCH SUBJECT (plus always recent ALL fallback).
    microsoft_mode: accept MS verify mails matched by local-part or MS sender + code.
    """
    _log = log or (lambda m: print(m, flush=True))
    cfg.require_login()
    start = time.time()
    since_ts = since_ts or (start - 30)
    target_lower = target_email.lower()
    target_local = target_lower.split("@")[0]
    seen_uids: set[bytes] = set()
    _log(f"[IMAP] Waiting OTP -> {target_email} (timeout={timeout}s)")

    # Prefer narrow FROM first (MS OTP). Avoid SUBJECT "Verify your email" alone —
    # that hits hundreds of non-MS mails and slows Gmail fetch.
    from_candidates: list[str] = []
    if from_filter:
        from_candidates.append(from_filter)
    if microsoft_mode:
        for f in (
            "accountprotection.microsoft.com",
            "account-security-noreply",
        ):
            if f not in from_candidates:
                from_candidates.append(f)
    subj_candidates: list[str] = []
    if subject_hint and subject_hint.lower() not in ("code", "otp"):
        # "code" alone is too broad; "Verify your email address" is OK as secondary
        subj_candidates.append(subject_hint)

    while time.time() - start < timeout:
        try:
            mail = imaplib.IMAP4_SSL(cfg.host, cfg.port)
            mail.login(cfg.user, cfg.password)
            mail.select("INBOX")
            id_set: list[bytes] = []
            seen_ids: set[bytes] = set()

            def _add_search(criterion: str, tail: int = 25) -> None:
                try:
                    status, messages = mail.search(None, criterion)
                    ids = messages[0].split() if messages and messages[0] else []
                    for mid in ids[-tail:]:
                        if mid not in seen_ids:
                            seen_ids.add(mid)
                            id_set.append(mid)
                except Exception:
                    pass

            for f in from_candidates:
                _add_search(f'(FROM "{f}")', tail=30)
            for s in subj_candidates:
                _add_search(f'(SUBJECT "{s}")', tail=20)
            # recent inbox only if nothing from MS yet
            if not id_set:
                _add_search("ALL", tail=30)

            # newest first
            for mid in reversed(id_set):
                if mid in seen_uids:
                    continue
                # header-only first: filter by Recipients (To:) before full body fetch
                status, data = mail.fetch(mid, "(BODY.PEEK[HEADER])")
                if not data or not data[0]:
                    seen_uids.add(mid)
                    continue
                try:
                    hdr_msg = message_from_bytes(data[0][1])
                except Exception:
                    seen_uids.add(mid)
                    continue
                if not _recipient_hit(hdr_msg, "", target_email):
                    # full-address not in To yet — still fetch body if MS sender
                    # (some paths only put alias in body)
                    if not (microsoft_mode and _ms_sender(hdr_msg)):
                        seen_uids.add(mid)
                        continue

                status, data = mail.fetch(mid, "(RFC822)")
                if not data or not data[0]:
                    continue
                msg = message_from_bytes(data[0][1])
                subject = msg.get("Subject", "") or ""
                body = _msg_body(msg)

                if not _recipient_hit(msg, body, target_email):
                    seen_uids.add(mid)
                    continue

                code = extract(subject, body)
                if code:
                    with _claimed_lock:
                        if code in _claimed_otps:
                            seen_uids.add(mid)
                            continue
                        _claimed_otps.add(code)
                    to_show = (msg.get("To") or "")[:60]
                    _log(
                        f"[IMAP] OTP {code} for {target_email} "
                        f"to={to_show!r} subj={subject[:50]!r}"
                    )
                    try:
                        mail.store(mid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                    mail.logout()
                    return code
                seen_uids.add(mid)
            mail.logout()
        except Exception as e:
            _log(f"[IMAP] Error: {e}")
        time.sleep(poll_s)

    _log("[IMAP] Timeout waiting for OTP")
    return None


def _self_check() -> None:
    assert re.fullmatch(r"[a-z0-9]{16}", crypto_local_part(16))
    assert extract_digit_otp("Your code is 123456", "") == "123456"
    assert extract_xai_otp("K35-1QR xAI confirmation code", "") == "K35-1QR"
    assert extract_xai_otp("PER-100 style", "color:PER-100") is None
    ms_body = (
        "To finish setting up your Microsoft account, we just need to make sure "
        "this email address is yours.\n"
        "To verify your email address use this security code: 023211\n"
    )
    assert extract_microsoft_otp("Verify your email address", ms_body) == "023211"
    store = UsedEmailStore(Path(os.environ.get("TEMP", ".") ) / "_mail_test_used.txt")
    store.emails.clear()
    cfg = ImapConfig(
        user="u@g.com",
        password="x",
        email_domain="test.example",
        gmail_base="u@g.com",
    )
    # reserve without real IMAP
    a = f"{crypto_local_part(12)}@test.example"
    assert store.reserve(a)
    assert not store.reserve(a)
    print("core.mail self-check ok", flush=True)


if __name__ == "__main__":
    _self_check()
