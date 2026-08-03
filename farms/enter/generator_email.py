from __future__ import annotations

import html
import random
import re
import secrets
import string
import threading
import time

from curl_cffi import requests

BASE = "https://generator.email"
_sessions: dict[str, requests.Session] = {}
_lock = threading.Lock()


def extract_api_token(page: str) -> str:
    match = re.search(r'<meta\s+name="api-token"\s+content="([^"]+)"', page, re.I)
    if not match:
        raise RuntimeError("generator.email page has no api-token")
    return match.group(1)


def extract_otp(page: str) -> str | None:
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text)
    match = re.search(
        r"(?:verification|verify|one[-\s]?time|passcode|security|your)\s+(?:code\s+)?(?:is\s*)?:?\s*(\d{6})",
        text,
        re.I,
    )
    return match.group(1) if match else None


def _new_session() -> requests.Session:
    session = requests.Session(impersonate="chrome136")
    session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
    return session


def _domains(session: requests.Session) -> list[str]:
    page = session.get(f"{BASE}/inbox9/", timeout=30)
    page.raise_for_status()
    session.headers.update({
        "X-API-Token": extract_api_token(page.text),
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/inbox9/",
        "Accept": "*/*",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    response = session.get(f"{BASE}/api/domains.php", timeout=30)
    response.raise_for_status()
    for key in ("X-API-Token", "X-Requested-With", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"):
        session.headers.pop(key, None)
    domains = [item.get("ascii", "") for item in response.json()]
    return [domain for domain in domains if domain and "." in domain]


def create_inbox() -> str:
    session = _new_session()
    domains = _domains(session)
    if not domains:
        raise RuntimeError("generator.email returned no domains")
    local = "ent" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    domain = random.choice(domains)
    address = f"{local}@{domain}"
    session.cookies.set("inbox_n", "9", domain="generator.email", path="/")
    session.cookies.set("inbox_ctx", f"{domain}/{local}/", domain="generator.email", path="/")
    response = session.get(f"{BASE}/inbox9/", timeout=30)
    response.raise_for_status()
    rendered = re.search(r'id="email_ch_text"[^>]*>([^<]+)', response.text)
    if not rendered or rendered.group(1).strip().lower() != address:
        raise RuntimeError("generator.email did not select requested inbox")
    with _lock:
        _sessions[address] = session
    return address


def poll_otp(address: str, timeout: int = 180, since_ts: float | None = None) -> str | None:
    # generator.email inboxes are created immediately before signup; since_ts is
    # accepted for compatibility with Enter's other mailbox providers.
    with _lock:
        session = _sessions.get(address)
    if session is None:
        raise RuntimeError(f"no generator.email session for {address}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = session.get(f"{BASE}/inbox9/", timeout=30)
        response.raise_for_status()
        if code := extract_otp(response.text):
            return code
        time.sleep(3)
    return None
