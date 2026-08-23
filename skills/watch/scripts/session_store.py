"""Persistent session registry for the MCP server.

Replaces the in-memory `mcp_server.SESSIONS` dict (Phase 1) with a
disk-backed JSON store at `~/.cache/watch-mcp/sessions.json`. Adds:

- **Persistence**: server restarts don't lose session_ids or work_dirs.
- **`video_id`**: a stable, caller-chosen key (or sha256(source)[:12] if
  absent). Two `watch` calls with the same `video_id` can reuse the
  cached result instead of re-running the pipeline.
- **WeChat placeholders**: `user_openid` / `user_unionid` / `auth_source`
  fields reserved now so Phase 2.8 (OAuth) only adds verification, not
  schema migration. Until then they're stored but not enforced.
- **Cross-user isolation** (Phase 2.1 prep for Phase 2.8): `list_for_user`
  and `delete_session` respect the `user_openid` boundary.

Concurrency model: the file is rewritten under `fcntl.flock(LOCK_EX)` so
concurrent writers serialize, and atomic via `temp + os.replace`. This
is the same pattern Phase 1.3 todo.md §"风险" recommended. The cost is
worst-case one extra fsync per write, which is acceptable for the
write-once-per-pipeline volume we have.

Why not sqlite? Three reasons:
1. JSON is human-readable for debugging — `cat sessions.json` answers
   "what sessions exist right now?" without a sqlite client.
2. The dataset is small (tens to low hundreds of entries per user per
   year; one JSON file < 100 KB even at 1000 entries).
3. Zero new dependencies. `sqlite3` is stdlib but adds boilerplate
   (connection management, schema migrations) for marginal benefit at
   this scale. If sessions.json grows past ~1 MB or we need indexed
   queries, swap to sqlite — the SessionRecord dataclass is the only
   surface callers see.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Literal

log = logging.getLogger(__name__)

# Module-level config so tests can override without monkeypatching.
def _default_root() -> Path:
    return Path.home() / ".cache" / "watch-mcp"

STORE_ROOT: Path = _default_root()
STORE_FILE: Path = STORE_ROOT / "sessions.json"
STORE_DIR_MODE = 0o700
STORE_FILE_MODE = 0o600

AuthSource = Literal["wechat_mp", "wechat_op", "none"]
SessionStatus = Literal["running", "done", "error", "cancelled"]


def hash_source_to_video_id(source: str) -> str:
    """Default video_id derivation: first 12 hex chars of sha256(source).

    Deterministic and stable for the same input URL/path, which is what
    callers want for "second call to the same URL reuses the session".
    URLs with trivial query-string changes (UTM tags etc.) collide —
    acceptable for v1; Phase 2.x can add URL canonicalization if needed.
    """
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


@dataclass
class SessionRecord:
    """One watch pipeline run, persisted across server restarts.

    `frames` and `masks` are stored as list[dict] matching the shape
    that `watch.run()` returns in `RunResult.frames` / `.masks`. Keeping
    them in the registry lets `read_frame` / `read_mask` work after a
    server restart, and lets Phase 2.6 `recompose` find them by
    video_id without re-running watch.
    """
    video_id: str
    work_dir: str
    source: str
    status: SessionStatus = "done"
    frames: list[dict] = field(default_factory=list)
    masks: list[dict] = field(default_factory=list)
    transcript_source: str | None = None
    transcript_text: str | None = None
    user_openid: str | None = None
    user_unionid: str | None = None
    auth_source: AuthSource = "none"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Reserved for Phase 2.2 — when the pipeline is split into
    # start_watch / get_status, the running record needs to track
    # which stage is in flight. Left here so the schema is stable
    # across Phase 2.1 and Phase 2.2.
    stage: str | None = None
    progress: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRecord":
        # Tolerate unknown keys (forward compat) and missing keys (defaults).
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ─── File locking ──────────────────────────────────────────────────────────


@contextlib.contextmanager
def _file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Cross-process file lock via fcntl.flock on a sidecar .lock file.

    Used to serialize concurrent writers of sessions.json. Holding the
    sidecar lock means holding the data file's logical lock — flock is
    advisory on Linux/macOS, which is fine for our single-process MCP
    server (Phase 2.7 BFF spawns one server per request, all sharing
    the same sessions.json).

    On platforms without flock (Windows) we silently skip the lock —
    Phase 2.x will revisit if Windows becomes a real target.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, STORE_FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        except (AttributeError, OSError):
            # No flock on this platform. Best-effort: yield without lock.
            # Concurrent writers may corrupt the JSON; the atomic
            # os.replace below mitigates the worst case (lost write,
            # never partial JSON).
            yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (AttributeError, OSError):
            pass
        os.close(fd)


# ─── Atomic write helpers ───────────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically: tmp file → fsync → os.replace.

    `os.replace` is atomic on POSIX (rename(2) is atomic within a
    filesystem), so readers see either the old or the new file
    contents, never half-written bytes. fsync before replace ensures
    the data is on disk, not just in the page cache.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # In the same dir so os.replace is a same-filesystem rename (atomic).
    fd, tmp_path_str = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path_str, path)
        try:
            os.chmod(path, STORE_FILE_MODE)
        except OSError:
            pass
    except Exception:
        # Clean up the tmp file if replace didn't happen.
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


# ─── Public API ────────────────────────────────────────────────────────────


def load_all() -> dict[str, SessionRecord]:
    """Load every session from disk.

    Returns an empty dict if the file doesn't exist (first run, or after
    a clean uninstall). Malformed JSON is logged and treated as empty —
    better to start fresh than refuse to serve requests because a
    corrupt entry exists.
    """
    if not STORE_FILE.exists():
        return {}
    try:
        with _file_lock(STORE_FILE, exclusive=False):
            data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log.warning("sessions.json root is not a dict; treating as empty")
            return {}
        out: dict[str, SessionRecord] = {}
        for vid, raw in data.items():
            if not isinstance(raw, dict):
                log.warning("skipping non-dict session entry for %s", vid)
                continue
            try:
                out[vid] = SessionRecord.from_dict(raw)
            except TypeError as e:
                log.warning("skipping malformed session %s: %s", vid, e)
        return out
    except json.JSONDecodeError as e:
        log.error("sessions.json is malformed: %s; treating as empty", e)
        return {}


def _dump_all(records: dict[str, SessionRecord]) -> None:
    payload = {vid: r.to_dict() for vid, r in records.items()}
    _atomic_write_json(STORE_FILE, payload)


def get(video_id: str) -> SessionRecord | None:
    """Return one session by id, or None if not found."""
    return load_all().get(video_id)


def upsert(record: SessionRecord) -> None:
    """Insert or update one session record.

    Concurrent writers: serialised by flock. Reads that happen
    concurrently with this write either see the old or new file
    contents (atomic rename), never partial JSON.
    """
    record.updated_at = time.time()
    with _file_lock(STORE_FILE, exclusive=True):
        all_records = _load_locked_unlocked()
        all_records[record.video_id] = record
        _dump_all(all_records)


def delete(video_id: str) -> bool:
    """Remove a session record. Returns True if it existed.

    Does NOT remove the work_dir on disk — caller decides. (Phase 2.1
    todo: cleanup is the caller's job; Phase 2.5 may add a
    `delete_session` MCP tool that also rmdirs if a flag is set.)
    """
    with _file_lock(STORE_FILE, exclusive=True):
        all_records = _load_locked_unlocked()
        if video_id not in all_records:
            return False
        del all_records[video_id]
        _dump_all(all_records)
        return True


def list_for_user(user_openid: str | None) -> list[SessionRecord]:
    """Return sessions visible to the given WeChat openid.

    Access rules (Phase 2.1 placeholder, Phase 2.8 will verify OAuth):
    - caller has openid X: sees records where `user_openid` is X OR
      `user_openid is None` (orphaned records from pre-OAuth calls,
      shown to everyone until Phase 2.8 fills them in).
    - caller has no openid (caller=None): sees ONLY records with
      `user_openid is None`. This is the "anonymous" view.

    Records are returned newest-updated first so the typical UI shows
    recent activity at the top.
    """
    all_records = load_all()
    visible: list[SessionRecord] = []
    for r in all_records.values():
        if user_openid is None:
            if r.user_openid is None:
                visible.append(r)
        else:
            if r.user_openid is None or r.user_openid == user_openid:
                visible.append(r)
    visible.sort(key=lambda r: r.updated_at, reverse=True)
    return visible


# ─── Internal helpers (caller holds lock) ──────────────────────────────────


def _load_locked_unlocked() -> dict[str, SessionRecord]:
    """Load all records assuming the caller already holds the exclusive
    flock. Skips the lock acquisition (would deadlock). Tolerates
    missing / corrupt file by returning empty."""
    if not STORE_FILE.exists():
        return {}
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {
            vid: SessionRecord.from_dict(raw)
            for vid, raw in data.items()
            if isinstance(raw, dict)
        }
    except (json.JSONDecodeError, TypeError):
        return {}


# ─── Test hooks ────────────────────────────────────────────────────────────


def _reset_for_tests(root: Path) -> None:
    """Point the module at a fresh temp dir. Test-only."""
    global STORE_ROOT, STORE_FILE
    STORE_ROOT = root
    STORE_FILE = root / "sessions.json"
    root.mkdir(parents=True, exist_ok=True)
