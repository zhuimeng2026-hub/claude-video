"""Persistent user / session / oauth_state store.

Phase 2.8 — backs the WeChat service-account OAuth flow. Two
"session" concepts coexist in this codebase:

1. **SessionRecord** (session_store.py, Phase 2.1) — per video_id.
   The /watch pipeline run record (frames, masks, work_dir).

2. **AuthSession** (this module) — per browser login. HttpOnly cookie
   value maps to an openid. Used by BFF require_user middleware.

The two are intentionally separate stores:
- session_store.json is human-readable for debugging and may need
  to survive a session-store DB migration without operator pain.
- users.sqlite3 is machine-only (SHA-256 hashes; no cleartext).

Schema (matches docs/todo.md §2.8):

  users         — known openids (record created on first login)
  sessions      — active auth sessions (cookie value → openid)
  oauth_states  — one-time CSRF tokens used during /auth/wechat/login

Storage location: `~/.cache/watch-mcp/users.sqlite3`. Override via
`WATCH_USERS_DB` env var for tests. WAL mode for concurrent reads.

Thread-safety: SQLite connections are NOT shared across threads. The
`get_conn()` helper opens a fresh connection per call. For high-
throughput endpoints (Phase 2.7 BFF) consider a connection pool;
Phase 2.8 MVP keeps it simple.

Lifetimes
---------
sessions: default 7 days (matches OpenMontage convention). Sliding
window — each authenticated request extends expires_at by 1 day.
oauth_states: 10 minutes (state expires before we can use it).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)


def _default_db_path() -> Path:
    return Path(os.environ.get(
        "WATCH_USERS_DB",
        str(Path.home() / ".cache" / "watch-mcp" / "users.sqlite3"),
    ))


# Module-level config (testable via _reset_for_tests)
DB_PATH: Path = _default_db_path()
DEFAULT_SESSION_TTL = 7 * 24 * 3600  # 7 days
DEFAULT_OAUTH_STATE_TTL = 10 * 60  # 10 minutes

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    openid TEXT PRIMARY KEY,
    unionid TEXT,
    nickname TEXT,
    created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    openid TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (openid) REFERENCES users(openid)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    redirect_after TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expires ON oauth_states(expires_at);
"""


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    """Open a fresh connection. Caller closes via context manager.

    Default isolation_level="" means transactions are implicit —
    pysqlite auto-BEGINs on the first DML statement. WAL mode gives
    concurrent readers + one writer. row_factory=sqlite3.Row makes
    results dict-like.
    """
    _ensure_parent(DB_PATH)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Yield a connection inside an explicit transaction (BEGIN/COMMIT).

    pysqlite's default isolation level auto-BEGINs on DML, but the
    explicit BEGIN here groups multi-statement operations (esp. the
    oauth_state consume-then-delete atomicity in consume_oauth_state).
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def init_schema() -> None:
    """Create tables / indexes if missing. Idempotent.

    Uses a plain connection (no transaction wrapper) because
    executescript() implicitly commits, breaking the surrounding
    BEGIN IMMEDIATE.
    """
    _ensure_parent(DB_PATH)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


# ─── Data classes ─────────────────────────────────────────────────────────


@dataclass
class User:
    openid: str
    unionid: Optional[str]
    nickname: Optional[str]
    created_at: int
    last_seen: int


@dataclass
class Session:
    id: str
    openid: str
    created_at: int
    expires_at: int


@dataclass
class OAuthState:
    state_hash: str
    redirect_after: str
    created_at: int
    expires_at: int


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        openid=row["openid"],
        unionid=row["unionid"],
        nickname=row["nickname"],
        created_at=row["created_at"],
        last_seen=row["last_seen"],
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        openid=row["openid"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _row_to_oauth_state(row: sqlite3.Row) -> OAuthState:
    return OAuthState(
        state_hash=row["state_hash"],
        redirect_after=row["redirect_after"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


# ─── CRUD: users ─────────────────────────────────────────────────────────


def upsert_user(openid: str, *, unionid: str | None = None,
                nickname: str | None = None) -> User:
    """Insert or update user; bumps last_seen."""
    now = int(time.time())
    with transaction() as conn:
        existing = conn.execute(
            "SELECT openid, unionid, nickname, created_at, last_seen FROM users WHERE openid = ?",
            (openid,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO users (openid, unionid, nickname, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (openid, unionid, nickname, now, now),
            )
        else:
            conn.execute(
                "UPDATE users SET unionid = COALESCE(?, unionid), "
                "nickname = COALESCE(?, nickname), last_seen = ? WHERE openid = ?",
                (unionid, nickname, now, openid),
            )
    return get_user(openid)  # type: ignore[return-value]


def get_user(openid: str) -> Optional[User]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT openid, unionid, nickname, created_at, last_seen FROM users WHERE openid = ?",
            (openid,),
        ).fetchone()
    return _row_to_user(row) if row else None


# ─── CRUD: auth sessions ──────────────────────────────────────────────────


def create_session(openid: str, *, ttl: int = DEFAULT_SESSION_TTL) -> Session:
    """Mint a new session id and store it. The id is the cookie value."""
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + ttl
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (id, openid, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, openid, now, expires),
        )
    return Session(id=sid, openid=openid, created_at=now, expires_at=expires)


def get_session(session_id: str) -> Optional[Session]:
    """Look up by id. Returns None if missing or expired (without
    deleting — cleanup happens via cleanup_expired_sessions)."""
    if not session_id:
        return None
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, openid, created_at, expires_at FROM sessions "
            "WHERE id = ? AND expires_at > ?",
            (session_id, now),
        ).fetchone()
    return _row_to_session(row) if row else None


def extend_session(session_id: str, *, ttl: int = DEFAULT_SESSION_TTL) -> bool:
    """Sliding-window: each authenticated request bumps expires_at by ttl."""
    now = int(time.time())
    new_expires = now + ttl
    with transaction() as conn:
        cur = conn.execute(
            "SELECT expires_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if cur is None:
            return False
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE id = ?",
            (new_expires, session_id),
        )
    return True


def delete_session(session_id: str) -> bool:
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE id = ?", (session_id,)
        )
    return cur.rowcount > 0


def cleanup_expired_sessions() -> int:
    """Periodic GC. Returns count deleted."""
    now = int(time.time())
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (now,)
        )
        cur2 = conn.execute(
            "DELETE FROM oauth_states WHERE expires_at <= ?", (now,)
        )
    log.debug("cleanup: deleted %d sessions, %d oauth_states",
              cur.rowcount, cur2.rowcount)
    return cur.rowcount


# ─── CRUD: oauth_states (CSRF tokens) ─────────────────────────────────────


def _hash_state(state: str) -> str:
    """Store SHA-256(state) only — plaintext state is never persisted
    (defence in depth: DB read doesn't leak usable CSRF tokens)."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_oauth_state(redirect_after: str,
                       *, ttl: int = DEFAULT_OAUTH_STATE_TTL) -> str:
    """Generate a fresh CSRF state and persist it. Returns the
    plaintext state (the only time it's seen — caller passes it
    through WeChat's flow)."""
    state = secrets.token_urlsafe(32)
    now = int(time.time())
    with transaction() as conn:
        conn.execute(
            "INSERT INTO oauth_states (state_hash, redirect_after, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_state(state), redirect_after, now, now + ttl),
        )
    return state


def consume_oauth_state(state: str) -> Optional[OAuthState]:
    """Look up state by its SHA-256 hash and DELETE it (one-time use).
    Returns None if missing, expired, or hash mismatch."""
    state_hash = _hash_state(state)
    now = int(time.time())
    with transaction() as conn:
        row = conn.execute(
            "SELECT state_hash, redirect_after, created_at, expires_at "
            "FROM oauth_states WHERE state_hash = ? AND expires_at > ?",
            (state_hash, now),
        ).fetchone()
        if row is None:
            return None
        # Atomic: verify hash then delete in same transaction so a
        # concurrent consume() can't win the race.
        if row["state_hash"] != state_hash:
            return None
        conn.execute("DELETE FROM oauth_states WHERE state_hash = ?",
                     (state_hash,))
        return _row_to_oauth_state(row)


# ─── Test hooks ──────────────────────────────────────────────────────────


def _reset_for_tests(path: Path) -> None:
    """Point the module at a fresh test DB. Test-only."""
    global DB_PATH
    DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    init_schema()


def _wipe_for_tests() -> None:
    """Delete all rows from all tables. Test-only convenience."""
    with transaction() as conn:
        conn.execute("DELETE FROM oauth_states")
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM users")
