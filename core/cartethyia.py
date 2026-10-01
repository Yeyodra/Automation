#!/usr/bin/env python3
"""
Cartethyia Postgres injector for the Enter farms.

Writes each farmed account straight into Cartethyia's `provider_accounts` table
(provider id `enterconverge`) in addition to the text/JSON artifacts, so a fresh
`ek_` key is routable without a manual import step.

Credential storage must match Cartethyia exactly, or the row is unusable:

  * `credential_ciphertext` is AES-256-GCM, layout `iv(12) || authTag(16) || ct`,
    keyed by `CARTETHYIA_ENCRYPTION_KEY` (base64 or 64-char hex). Verified
    byte-compatible against a live row by decrypting it and re-checking the
    stored `credential_fingerprint`.
  * `credential_fingerprint` is `HMAC-SHA256(key, secret)` hex — the same key as
    the cipher, matching `hashSecret()` in `src/security/crypto.ts`.
  * `auth_state.workspaceId` carries the numeric workspace id; the adapter reads
    it there and otherwise resolves it from `GET /workspaces`.
  * `tenant_id` is nullable: null means the account is shared pool-wide, which
    is what a farmed key is.

The injector is fail-soft by design: a missing driver, unreachable database, or
absent encryption key logs a warning and returns False, so the farm still
records its own results when Cartethyia is not available.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from pathlib import Path

PROVIDER_ID = "enterconverge"
TABLE = "provider_accounts"

_ENV_KEYS = ("CARTETHYIA_ENCRYPTION_KEY", "CARTETHYIA_DATABASE_URL", "DATABASE_URL")
_DEFAULT_ENV_FILES = (
    Path(r"C:\Users\Novella\Documents\Github\Cartethyia\.env"),
)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _config() -> tuple[str, str] | None:
    """Returns (encryption_key_raw, database_url) or None when unconfigured.

    An explicit `CARTETHYIA_ENV_FILE` replaces the built-in search path rather
    than adding to it: a caller pointing at a specific file expects that file's
    absence to mean "unconfigured", not a silent fallback to this machine's
    Cartethyia checkout.
    """
    extra = os.environ.get("CARTETHYIA_ENV_FILE", "").strip()
    paths = [Path(extra)] if extra else list(_DEFAULT_ENV_FILES)

    file_env: dict[str, str] = {}
    for path in paths:
        file_env.update(_read_env_file(path))

    def pick(name: str) -> str:
        return (os.environ.get(name) or file_env.get(name) or "").strip()

    key = pick("CARTETHYIA_ENCRYPTION_KEY")
    url = pick("CARTETHYIA_DATABASE_URL") or pick("DATABASE_URL")
    if not key or not url:
        return None
    return key, url


def _decode_key(raw: str) -> bytes:
    candidate = raw.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
        key = bytes.fromhex(candidate)
    else:
        key = base64.b64decode(candidate)
    if len(key) != 32:
        raise ValueError(f"encryption key must decode to 32 bytes (got {len(key)})")
    return key


def _encrypt(key: bytes, plaintext: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, auth_tag = sealed[:-16], sealed[-16:]
    return iv + auth_tag + ciphertext


def _fingerprint(key: bytes, secret: str) -> str:
    return hmac.new(key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


def inject_account(
    *,
    api_key: str,
    workspace_id: str,
    label: str = "",
    log=None,
) -> bool:
    """Upsert one enterconverge account. Returns True when the row is written."""
    def warn(msg: str) -> None:
        if log:
            log(msg)

    if not api_key or not api_key.startswith("ek_"):
        warn(f"cartethyia: skip (not an ek_ key: {api_key[:12]!r})")
        return False

    cfg = _config()
    if cfg is None:
        warn("cartethyia: skip (CARTETHYIA_ENCRYPTION_KEY / DATABASE_URL not set)")
        return False
    key_raw, db_url = cfg

    try:
        key = _decode_key(key_raw)
    except Exception as exc:
        warn(f"cartethyia: bad encryption key: {type(exc).__name__}: {exc}")
        return False

    try:
        import psycopg
        from psycopg.types.json import Json
    except Exception:
        warn("cartethyia: skip (psycopg not installed)")
        return False

    ciphertext = _encrypt(key, api_key)
    fingerprint = _fingerprint(key, api_key)
    auth_state = {"workspaceId": str(workspace_id)} if workspace_id else {}
    name = label or f"{PROVIDER_ID} account"

    # The unique index keys on (provider_id, coalesce(tenant_id,'0'::uuid),
    # credential_fingerprint), so a conflicting fingerprint is the same
    # credential: refresh its label/workspace instead of inserting a twin.
    sql = f"""
        INSERT INTO {TABLE}
            (provider_id, tenant_id, label, credential_ciphertext,
             credential_fingerprint, credential_kind, auth_state, status)
        VALUES (%s, NULL, %s, %s, %s, 'api_key', %s, 'active')
        ON CONFLICT (provider_id,
                     (coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)),
                     credential_fingerprint)
        DO UPDATE SET
            label = EXCLUDED.label,
            credential_ciphertext = EXCLUDED.credential_ciphertext,
            auth_state = EXCLUDED.auth_state,
            status = 'active',
            consecutive_failures = 0,
            last_error = NULL,
            last_error_at = NULL,
            cooldown_until = NULL
        RETURNING id
    """
    params = (PROVIDER_ID, name, ciphertext, fingerprint, Json(auth_state))

    try:
        with psycopg.connect(db_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM providers WHERE id = %s", (PROVIDER_ID,)
                )
                if cur.fetchone() is None:
                    warn(f"cartethyia: skip (provider row {PROVIDER_ID!r} missing)")
                    return False
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
    except Exception as exc:
        warn(f"cartethyia: inject failed: {type(exc).__name__}: {exc}")
        return False

    row_id = row[0] if row else "?"
    warn(f"cartethyia: injected {label or api_key[:12]} -> provider_accounts {row_id}")
    return True
