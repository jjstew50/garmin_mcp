"""
SQLite-backed user store for API keys and authorized Garmin emails.

The database lives on the Railway volume at GARMIN_DATA_PATH/users.db
(default /app/data/users.db), persisting across restarts and deployments.
"""

import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager


_DB_FILENAME = "users.db"


def _db_path() -> str:
    base = os.environ.get("GARMIN_DATA_PATH", "/app/data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, _DB_FILENAME)


@contextmanager
def _conn(db_path: str | None = None):
    path = db_path or _db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None):
    """Create the users table if it doesn't exist."""
    with _conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                key             TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                email           TEXT NOT NULL UNIQUE,
                training_memory INTEGER NOT NULL DEFAULT 0,
                connected_at    TEXT,
                created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        """)


def add_user(
    key: str,
    name: str,
    email: str,
    training_memory: bool = False,
    db_path: str | None = None,
) -> None:
    """Add a new user. Raises sqlite3.IntegrityError if key or email already exists."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO users (key, name, email, training_memory) VALUES (?, ?, ?, ?)",
            (key, name, email.strip().lower(), int(training_memory)),
        )


def get_user_by_key(key: str, db_path: str | None = None) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str, db_path: str | None = None) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None


def list_users(db_path: str | None = None) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def delete_user(key: str, db_path: str | None = None) -> bool:
    """Delete user by key. Returns True if a row was deleted."""
    with _conn(db_path) as conn:
        cur = conn.execute("DELETE FROM users WHERE key = ?", (key,))
        return cur.rowcount > 0


def update_connected_at(key: str, db_path: str | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE users SET connected_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE key = ?",
            (key,),
        )


def generate_key() -> str:
    """Generate a random 40-character hex API key."""
    return secrets.token_hex(20)


def seed_from_env_users(env_users: list[dict], db_path: str | None = None) -> int:
    """Seed DB from MCP_USERS env var entries. Skips existing keys/emails. Returns insert count."""
    count = 0
    for u in env_users:
        key = u.get("key", "").strip()
        email = u.get("email", "").strip().lower()
        name = u.get("name", email.split("@")[0].capitalize() if email else "Unknown")
        if not key or not email:
            continue
        try:
            add_user(key, name, email, bool(u.get("training_memory", False)), db_path)
            count += 1
        except sqlite3.IntegrityError:
            pass  # already in DB — never overwrite
    return count
