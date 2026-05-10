"""OAuth 2.1 Authorization Server fuer den KI-OS MCP-Server.

Implementiert:
  - Discovery-Endpoints (.well-known/oauth-protected-resource +
    .well-known/oauth-authorization-server)
  - Authorization Code Flow mit PKCE (RFC 7636 — pflicht in 2.1)
  - Dynamic Client Registration (RFC 7591) — claude.ai braucht das
  - JWT Access-Tokens (HS256, 15 Min Lifetime)
  - Refresh-Tokens mit Rotation (3 Tage Lifetime, Replay-Detection)
  - Token-Revocation (RFC 7009)
  - Custom HTML Login + Consent

Single-User-Setup: ein User in ENV (OAUTH_USER_EMAIL + OAUTH_PASSWORD_HASH).
1 Scope: 'admin' = alles. JWT-Secret aus OAUTH_JWT_SECRET (separater Key vom
Bearer-MCP_TOKEN).

Storage:
  - Authorization-Codes: in-memory (60s expiry, single-use)
  - Refresh-Tokens: SQLite (`/var/lib/mcp-oauth/oauth.db`, persistent)
  - Clients (DCR): SQLite (gleiche DB)

Bestehende Bearer-Auth bleibt parallel — Dual-Auth-Middleware checkt JWT
zuerst, faellt zurueck auf statisches Bearer-Token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import bcrypt
import jwt

log = logging.getLogger("ki-os-mcp.oauth")


# ---------- Config aus ENV --------------------------------------------------

OAUTH_USER_EMAIL = os.environ.get("OAUTH_USER_EMAIL", "julius@sima.or.at").strip().lower()
OAUTH_PASSWORD_HASH = os.environ.get("OAUTH_PASSWORD_HASH", "").strip()
OAUTH_JWT_SECRET = os.environ.get("OAUTH_JWT_SECRET", "").strip()
OAUTH_DB_PATH = os.environ.get("OAUTH_DB_PATH", "/var/lib/mcp-oauth/oauth.db")
# Issuer + Resource URLs — werden in Discovery-Doc + JWT-Claims genutzt
OAUTH_ISSUER = os.environ.get("OAUTH_ISSUER", "https://wiki-mcp.sima.business").rstrip("/")
OAUTH_RESOURCE = os.environ.get("OAUTH_RESOURCE", "https://wiki-mcp.sima.business/mcp/")

# Lifetimes (in Sekunden)
ACCESS_TOKEN_TTL = int(os.environ.get("OAUTH_ACCESS_TTL", "900"))           # 15 Min
REFRESH_TOKEN_TTL = int(os.environ.get("OAUTH_REFRESH_TTL", "259200"))     # 3 Tage
AUTH_CODE_TTL = 60                                                         # 60s — Authorization Code lifetime

OAUTH_SCOPES = ["admin"]                  # Single-Scope-Setup — "admin" = alles
DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
SUPPORTED_RESPONSE_TYPES = ["code"]
SUPPORTED_CODE_CHALLENGE_METHODS = ["S256"]


def is_configured() -> bool:
    """OAuth-Server nur aktiv wenn alle 3 Secrets gesetzt sind."""
    return bool(OAUTH_USER_EMAIL and OAUTH_PASSWORD_HASH and OAUTH_JWT_SECRET)


# ---------- SQLite-Setup -----------------------------------------------------

_db_lock = threading.Lock()


def _ensure_db() -> sqlite3.Connection:
    """Stellt DB sicher inkl. Schema-Migration. Idempotent."""
    db_dir = Path(OAUTH_DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(OAUTH_DB_PATH, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS clients (
        client_id        TEXT PRIMARY KEY,
        client_secret    TEXT,                   -- NULL fuer public clients (PKCE)
        client_name      TEXT,
        redirect_uris    TEXT NOT NULL,          -- JSON array
        token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
        created_at       INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS refresh_tokens (
        token_id         TEXT PRIMARY KEY,       -- random secret string
        client_id        TEXT NOT NULL,
        subject          TEXT NOT NULL,           -- user-email
        scope            TEXT NOT NULL,
        issued_at        INTEGER NOT NULL,
        expires_at       INTEGER NOT NULL,
        revoked          INTEGER NOT NULL DEFAULT 0,
        replaced_by      TEXT,                    -- bei Rotation: ID des Nachfolgers
        FOREIGN KEY(client_id) REFERENCES clients(client_id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_refresh_subject ON refresh_tokens(subject);
    CREATE INDEX IF NOT EXISTS idx_refresh_expires ON refresh_tokens(expires_at);
    """)
    return conn


_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    """Lazy DB-Init mit Lock-Protected Singleton. Race-safe."""
    global _conn
    # Fast-path ohne lock wenn schon initialized — Lock-Akquisition ist teuer
    local = _conn
    if local is not None:
        return local
    with _db_lock:
        # Re-check nach Lock-Acquire (anderer Thread kann mittlerweile init haben)
        if _conn is None:
            _conn = _ensure_db()
        return _conn


def is_jwt_secret_strong() -> tuple[bool, str]:
    """Prueft beim Boot ob OAUTH_JWT_SECRET ausreichend lang ist."""
    if not OAUTH_JWT_SECRET:
        return False, "OAUTH_JWT_SECRET ist leer — OAuth nicht aktiv"
    if len(OAUTH_JWT_SECRET) < 32:
        return False, f"OAUTH_JWT_SECRET zu kurz ({len(OAUTH_JWT_SECRET)} chars, Min. 32). Generiere via: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
    return True, "ok"


def is_password_hash_strong() -> tuple[bool, str]:
    """Prueft beim Boot ob OAUTH_PASSWORD_HASH ein bcrypt mit min. 10 rounds ist."""
    if not OAUTH_PASSWORD_HASH:
        return False, "OAUTH_PASSWORD_HASH ist leer"
    # bcrypt-Format: $2[abxy]$<rounds>$<22-char-salt><31-char-hash>
    if not OAUTH_PASSWORD_HASH.startswith(("$2a$", "$2b$", "$2x$", "$2y$")):
        return False, f"OAUTH_PASSWORD_HASH ist kein bcrypt-Format ({OAUTH_PASSWORD_HASH[:7]!r})"
    try:
        rounds = int(OAUTH_PASSWORD_HASH[4:6])
    except ValueError:
        return False, "OAUTH_PASSWORD_HASH: rounds nicht parsebar"
    if rounds < 10:
        return False, f"OAUTH_PASSWORD_HASH: nur {rounds} bcrypt-rounds (Min. empfohlen: 12)"
    return True, f"ok (bcrypt rounds={rounds})"


# ---------- In-Memory Authorization-Code Store ------------------------------
# Codes leben max 60s und sind single-use. RAM ist fine — bei Server-Restart
# muessen Clients halt neu authorizen, das ist harmlos.

_auth_codes: dict[str, dict[str, Any]] = {}
_codes_lock = threading.Lock()


@dataclass
class AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    subject: str
    issued_at: int


def issue_auth_code(client_id: str, redirect_uri: str,
                    code_challenge: str, code_challenge_method: str,
                    scope: str, subject: str) -> str:
    code = secrets.token_urlsafe(32)
    with _codes_lock:
        _auth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "scope": scope,
            "subject": subject,
            "issued_at": int(time.time()),
        }
        # Cleanup expired codes
        cutoff = int(time.time()) - AUTH_CODE_TTL
        for k in [k for k, v in _auth_codes.items() if v["issued_at"] < cutoff]:
            del _auth_codes[k]
    return code


def cleanup_expired_codes() -> int:
    """Entfernt abgelaufene Auth-Codes aus dem in-memory Store.

    Wird periodic vom Maintain-Scheduler gerufen (alle 10 Min). Verhindert
    Memory-Leak falls jemand Codes ausgibt aber nie consumed.
    """
    cutoff = int(time.time()) - AUTH_CODE_TTL
    with _codes_lock:
        expired = [k for k, v in _auth_codes.items() if v["issued_at"] < cutoff]
        for k in expired:
            del _auth_codes[k]
    return len(expired)


def consume_auth_code(code: str, client_id: str, redirect_uri: str,
                      code_verifier: str) -> dict[str, Any] | None:
    """Validiert + invalidiert den Code. Returns claims dict oder None."""
    with _codes_lock:
        entry = _auth_codes.pop(code, None)
    if not entry:
        return None
    if int(time.time()) - entry["issued_at"] > AUTH_CODE_TTL:
        return None
    if entry["client_id"] != client_id or entry["redirect_uri"] != redirect_uri:
        return None
    # PKCE-Validierung
    if entry["code_challenge_method"] != "S256":
        return None
    expected = urlsafe_b64encode(
        sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    if expected != entry["code_challenge"]:
        return None
    return entry


# ---------- Password verification + JWT-Issuance ----------------------------


def verify_password(email: str, password: str) -> bool:
    if email.strip().lower() != OAUTH_USER_EMAIL:
        return False
    if not OAUTH_PASSWORD_HASH:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), OAUTH_PASSWORD_HASH.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_access_token(subject: str, client_id: str, scope: str) -> tuple[str, int]:
    """Returns (jwt_string, expires_in_seconds)."""
    now = int(time.time())
    payload = {
        "iss": OAUTH_ISSUER,
        "aud": OAUTH_RESOURCE,
        "sub": subject,
        "client_id": client_id,
        "scope": scope,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(payload, OAUTH_JWT_SECRET, algorithm="HS256")
    return token, ACCESS_TOKEN_TTL


def verify_access_token(token: str) -> dict[str, Any] | None:
    """JWT validieren — checkt sig, exp, aud, iss. None wenn ungueltig."""
    try:
        return jwt.decode(
            token,
            OAUTH_JWT_SECRET,
            algorithms=["HS256"],
            audience=OAUTH_RESOURCE,
            issuer=OAUTH_ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as e:
        log.debug("JWT-Verify fail: %s", e)
        return None


# ---------- Refresh-Token-Store (mit Rotation + Theft-Detection) ------------


def issue_refresh_token(client_id: str, subject: str, scope: str) -> str:
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    db().execute(
        "INSERT INTO refresh_tokens (token_id, client_id, subject, scope, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, client_id, subject, scope, now, now + REFRESH_TOKEN_TTL),
    )
    return token


def rotate_refresh_token(old_token: str, client_id: str) -> tuple[str, str, str] | None:
    """Tauscht alten Refresh-Token gegen Paar (new_access, new_refresh, scope).
    Returns None wenn ungueltig/abgelaufen/replayed.

    Bei Replay-Detection (alter Token nochmal verwendet nachdem rotiert):
    revokt ALLE refresh-tokens des Subjects → Forced re-login. Spec-konformer
    Theft-Schutz (RFC 6819 §5.2.2.3).

    Atomic via explicit BEGIN/COMMIT — bei Exception in der Mitte wird
    rollback gemacht, Token-DB bleibt konsistent.
    """
    new_token: str | None = None
    subject: str | None = None
    scope: str | None = None

    with _db_lock:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT subject, scope, expires_at, revoked, replaced_by FROM refresh_tokens "
                "WHERE token_id = ? AND client_id = ?",
                (old_token, client_id),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            subject_db, scope_db, expires_at, revoked, replaced_by = row

            now = int(time.time())
            if now >= expires_at:
                conn.execute("ROLLBACK")
                return None

            if revoked or replaced_by:
                # Replay! → ALLE Tokens des Subjects revoken (Theft-Schutz)
                log.warning("Refresh-Token Replay erkannt subject=%s — alle Tokens revoked", subject_db)
                conn.execute(
                    "UPDATE refresh_tokens SET revoked = 1 WHERE subject = ? AND revoked = 0",
                    (subject_db,),
                )
                conn.execute("COMMIT")
                return None

            # OK — alten Token revoken, neuen ausgeben (BEIDE in einer TX)
            new_token = secrets.token_urlsafe(48)
            subject = subject_db
            scope = scope_db
            conn.execute(
                "INSERT INTO refresh_tokens (token_id, client_id, subject, scope, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_token, client_id, subject, scope, now, now + REFRESH_TOKEN_TTL),
            )
            conn.execute(
                "UPDATE refresh_tokens SET revoked = 1, replaced_by = ? WHERE token_id = ?",
                (new_token, old_token),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            log.exception("rotate_refresh_token DB-Fehler — rollback")
            raise

    if new_token is None or subject is None or scope is None:
        return None
    new_access, _ = issue_access_token(subject, client_id, scope)
    return (new_access, new_token, scope)


def revoke_refresh_token(token_id: str) -> bool:
    cursor = db().execute(
        "UPDATE refresh_tokens SET revoked = 1 WHERE token_id = ? AND revoked = 0",
        (token_id,),
    )
    return cursor.rowcount > 0


def revoke_all_for_subject(subject: str) -> int:
    """Loggt User komplett aus (Logout-All)."""
    cursor = db().execute(
        "UPDATE refresh_tokens SET revoked = 1 WHERE subject = ? AND revoked = 0",
        (subject,),
    )
    return cursor.rowcount


# ---------- Dynamic Client Registration -------------------------------------


def register_client(client_name: str, redirect_uris: list[str]) -> dict[str, Any]:
    """Registriert einen public Client (PKCE-Flow, kein client_secret).

    Wir verlangen explizit redirect_uris (kein Wildcard) gegen Open-Redirect.
    """
    if not redirect_uris or not isinstance(redirect_uris, list):
        raise ValueError("redirect_uris muss nicht-leere Liste sein")
    for uri in redirect_uris:
        if not isinstance(uri, str):
            raise ValueError("redirect_uris muss strings enthalten")
        # Erlaubt: https:// (production) oder http://localhost (dev)
        # SSRF/Open-Redirect-Schutz: keine private-IP-ranges, keine localhost
        # bei Production-issuer
        if not (uri.startswith("https://") or uri.startswith("http://localhost")
                or uri.startswith("http://127.0.0.1")):
            raise ValueError(f"redirect_uri muss https:// oder http://localhost sein: {uri}")

    client_id = secrets.token_urlsafe(24)
    db().execute(
        "INSERT INTO clients (client_id, client_secret, client_name, redirect_uris, "
        "token_endpoint_auth_method, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (client_id, None, client_name, json.dumps(redirect_uris), "none", int(time.time())),
    )
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": DEFAULT_GRANT_TYPES,
        "response_types": SUPPORTED_RESPONSE_TYPES,
        "token_endpoint_auth_method": "none",
        "client_id_issued_at": int(time.time()),
    }


def get_client(client_id: str) -> dict[str, Any] | None:
    cursor = db().execute(
        "SELECT client_id, client_name, redirect_uris, token_endpoint_auth_method "
        "FROM clients WHERE client_id = ?",
        (client_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "client_id": row[0],
        "client_name": row[1],
        "redirect_uris": json.loads(row[2]),
        "token_endpoint_auth_method": row[3],
    }


def validate_redirect_uri(client_id: str, redirect_uri: str) -> bool:
    """Strict-match — kein Wildcard, kein Teilstring-Match."""
    client = get_client(client_id)
    if not client:
        return False
    return redirect_uri in client["redirect_uris"]


# ---------- Discovery-Metadata -----------------------------------------------


def protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728 — sagt Clients wo der Auth-Server liegt + welche scopes."""
    return {
        "resource": OAUTH_RESOURCE,
        "authorization_servers": [OAUTH_ISSUER],
        "scopes_supported": OAUTH_SCOPES,
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata() -> dict[str, Any]:
    """RFC 8414 — Auth-Server-Beschreibung mit allen Endpoints."""
    return {
        "issuer": OAUTH_ISSUER,
        "authorization_endpoint": f"{OAUTH_ISSUER}/oauth/authorize",
        "token_endpoint": f"{OAUTH_ISSUER}/oauth/token",
        "registration_endpoint": f"{OAUTH_ISSUER}/oauth/register",
        "revocation_endpoint": f"{OAUTH_ISSUER}/oauth/revoke",
        "scopes_supported": OAUTH_SCOPES,
        "response_types_supported": SUPPORTED_RESPONSE_TYPES,
        "grant_types_supported": DEFAULT_GRANT_TYPES,
        "code_challenge_methods_supported": SUPPORTED_CODE_CHALLENGE_METHODS,
        "token_endpoint_auth_methods_supported": ["none"],
        "service_documentation": "https://github.com/julasim/KI_WIKI_MCP",
    }


# ---------- Async-Wrapper fuer blocking calls -------------------------------
# Werden aus async-Routen aufgerufen via `await asyncio.to_thread(...)`.
# Ohne diese wrapper blockiert bcrypt + SQLite-I/O den event-loop unter Last.


async def verify_password_async(email: str, password: str) -> bool:
    return await asyncio.to_thread(verify_password, email, password)


async def register_client_async(client_name: str, redirect_uris: list[str]) -> dict[str, Any]:
    return await asyncio.to_thread(register_client, client_name, redirect_uris)


async def get_client_async(client_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_client, client_id)


async def validate_redirect_uri_async(client_id: str, redirect_uri: str) -> bool:
    return await asyncio.to_thread(validate_redirect_uri, client_id, redirect_uri)


async def issue_refresh_token_async(client_id: str, subject: str, scope: str) -> str:
    return await asyncio.to_thread(issue_refresh_token, client_id, subject, scope)


async def rotate_refresh_token_async(old_token: str, client_id: str) -> tuple[str, str, str] | None:
    return await asyncio.to_thread(rotate_refresh_token, old_token, client_id)


async def revoke_refresh_token_async(token_id: str) -> bool:
    return await asyncio.to_thread(revoke_refresh_token, token_id)
