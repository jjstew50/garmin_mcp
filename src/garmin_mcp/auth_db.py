"""
User database with API key generation and Garmin token encryption.

Security model
--------------
* API keys are 64-char hex (256-bit entropy) — shown once at signup, never stored.
* DB stores SHA-256(api_key) for fast O(1) lookup. This is safe because the 256-bit
  entropy makes brute-force infeasible. Argon2 is reserved for passwords, which have
  low entropy.
* Garmin tokens are encrypted with AES-256-GCM using a key derived via HKDF-SHA256
  from the user's API key + a per-user random salt. The server cannot decrypt stored
  tokens without receiving the user's API key in the request.
* If the DB is breached in isolation, all ciphertext is worthless.
* The unavoidable limitation: since the server makes Garmin API calls, the operator
  can intercept plaintext tokens at the moment of decryption by modifying server code.
  This is inherent to any server-side proxy. The design minimises attack surface
  (at-rest protection) without claiming zero-knowledge.
"""

import hashlib
import os
import secrets
import uuid
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ph = PasswordHasher()
_HKDF_INFO = b"garmin-token-enc-v1"
_AUTH_DB_FILENAME = "garmin_auth.db"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    email             TEXT UNIQUE,
    google_sub        TEXT UNIQUE,
    password_hash     TEXT,
    api_key_hash      TEXT NOT NULL UNIQUE,
    api_key_enc_salt  TEXT NOT NULL,
    encrypted_tokens  TEXT,
    token_nonce       TEXT,
    garmin_connected  INTEGER DEFAULT 0,
    created_at        TEXT NOT NULL,
    last_seen_at      TEXT,
    total_calls       INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_api_key_hash ON users(api_key_hash);
CREATE INDEX IF NOT EXISTS idx_users_email        ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_google_sub   ON users(google_sub);

CREATE TABLE IF NOT EXISTS usage_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    tool_name   TEXT,
    called_at   TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id, called_at);
"""


def _db_path() -> str:
    base = os.environ.get("GARMIN_DATA_PATH", "/app/data")
    return os.path.join(base, _AUTH_DB_FILENAME)


async def ensure_db() -> None:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA_DDL)
        await db.commit()


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def _hash_api_key(api_key: str) -> str:
    """SHA-256 hex for DB lookup. Safe: 256-bit key entropy makes brute-force infeasible."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def _derive_enc_key(api_key: str, salt_b64: str) -> bytes:
    """Derive a 32-byte AES key from the user's API key using HKDF-SHA256."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b64decode(salt_b64), info=_HKDF_INFO)
    return hkdf.derive(api_key.encode())


def encrypt_tokens(api_key: str, salt_b64: str, plaintext: str) -> tuple[str, str]:
    """Encrypt garth token dump. Returns (ciphertext_b64, nonce_b64)."""
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_derive_enc_key(api_key, salt_b64)).encrypt(nonce, plaintext.encode(), None)
    return b64encode(ciphertext).decode(), b64encode(nonce).decode()


def decrypt_tokens(api_key: str, salt_b64: str, ciphertext_b64: str, nonce_b64: str) -> str:
    """Decrypt garth token dump. Raises InvalidTag on wrong key or tampered data."""
    plaintext = AESGCM(_derive_enc_key(api_key, salt_b64)).decrypt(
        b64decode(nonce_b64), b64decode(ciphertext_b64), None
    )
    return plaintext.decode()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_user(
    email: Optional[str] = None,
    password: Optional[str] = None,
    google_sub: Optional[str] = None,
) -> tuple[str, str]:
    """Create a new user. Returns (user_id, api_key). The api_key is shown ONCE and never stored."""
    await ensure_db()
    user_id = str(uuid.uuid4())
    api_key = secrets.token_hex(32)  # 64-char hex, 256-bit entropy
    api_key_hash = _hash_api_key(api_key)
    api_key_enc_salt = b64encode(secrets.token_bytes(16)).decode()
    password_hash = _ph.hash(password) if password else None

    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO users
               (id, email, google_sub, password_hash, api_key_hash, api_key_enc_salt, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, email, google_sub, password_hash, api_key_hash, api_key_enc_salt, _now()),
        )
        await db.commit()
    return user_id, api_key


async def authenticate_password(email: str, password: str) -> Optional[dict]:
    """Verify email + password. Returns user row or None."""
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email = ?", (email,)) as cur:
            row = await cur.fetchone()
    if not row or not row["password_hash"]:
        return None
    try:
        _ph.verify(row["password_hash"], password)
        return dict(row)
    except VerifyMismatchError:
        return None


async def get_user_by_google_sub(google_sub: str) -> Optional[dict]:
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_email(email: str) -> Optional[dict]:
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email = ?", (email,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(user_id: str) -> Optional[dict]:
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def lookup_user_by_api_key(api_key: str) -> Optional[dict]:
    """O(1) lookup via SHA-256 hash. Fast because the key has 256-bit entropy."""
    await ensure_db()
    key_hash = _hash_api_key(api_key)
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE api_key_hash = ?", (key_hash,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def email_exists(email: str) -> bool:
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT 1 FROM users WHERE email = ?", (email,)) as cur:
            return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Garmin token storage (encrypted)
# ---------------------------------------------------------------------------

async def store_garmin_tokens(user_id: str, api_key: str, garth_dump: str) -> None:
    """Encrypt garth token dump with the user's API key and store in DB."""
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT api_key_enc_salt FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise ValueError(f"User {user_id} not found")
    ciphertext_b64, nonce_b64 = encrypt_tokens(api_key, row["api_key_enc_salt"], garth_dump)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE users SET encrypted_tokens=?, token_nonce=?, garmin_connected=1 WHERE id=?",
            (ciphertext_b64, nonce_b64, user_id),
        )
        await db.commit()


async def load_garmin_tokens(user_id: str, api_key: str) -> Optional[str]:
    """Decrypt and return garth token dump. Returns None if Garmin not connected."""
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT api_key_enc_salt, encrypted_tokens, token_nonce, garmin_connected FROM users WHERE id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or not row["garmin_connected"] or not row["encrypted_tokens"]:
        return None
    return decrypt_tokens(api_key, row["api_key_enc_salt"], row["encrypted_tokens"], row["token_nonce"])


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

async def record_usage(user_id: str, tool_name: Optional[str] = None) -> None:
    now = _now()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO usage_log (user_id, tool_name, called_at) VALUES (?,?,?)",
            (user_id, tool_name, now),
        )
        await db.execute(
            "UPDATE users SET total_calls = total_calls + 1, last_seen_at = ? WHERE id = ?",
            (now, user_id),
        )
        await db.commit()


async def get_usage_stats(user_id: str) -> dict:
    await ensure_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT total_calls, last_seen_at, created_at, garmin_connected FROM users WHERE id = ?",
            (user_id,),
        ) as cur:
            raw = await cur.fetchone()
            user = dict(raw) if raw else {}
        async with db.execute(
            """SELECT tool_name, COUNT(*) as count FROM usage_log
               WHERE user_id = ? GROUP BY tool_name ORDER BY count DESC LIMIT 10""",
            (user_id,),
        ) as cur:
            top_tools = [dict(r) for r in await cur.fetchall()]
    return {
        "total_calls": user.get("total_calls", 0),
        "last_seen_at": user.get("last_seen_at"),
        "member_since": user.get("created_at"),
        "garmin_connected": bool(user.get("garmin_connected", 0)),
        "top_tools": top_tools,
    }
