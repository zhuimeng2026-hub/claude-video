"""Phase 3.1 — cleanup.py cron entry tests.

Verifies the cleanup logic:
  - Removes expired terminal SessionRecords (done > 30d, error/cancelled > 7d)
  - Keeps recent and in-progress records
  - Calls users_store.cleanup_expired_sessions() for stale sessions/oauth_states
  - Always exits 0 (cron-friendly) even if one store fails
  - --json emits machine-readable stats

We import `cleanup` as a module and call its inner functions
directly — the in-process state (session_store.STORE_ROOT) is set by
the test fixtures via `_reset_for_tests`. The `main()` function
itself is tested separately via a single subprocess invocation to
verify the CLI path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cleanup  # noqa: E402


def _make_record(video_id: str, *, status: str, updated_at: float):
    import session_store
    return session_store.SessionRecord(
        video_id=video_id,
        work_dir=f"/tmp/{video_id}",
        source="https://example.com/x.mp4",
        status=status,
        updated_at=updated_at,
    )


def test_cleanup_removes_old_done_records(tmp_path, monkeypatch):
    import session_store
    import users_store
    import time as time_mod
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    session_store._reset_for_tests(tmp_path / "watch-store")
    users_store._reset_for_tests(tmp_path / "users.sqlite3")

    # Pin time so we can backdate records deterministically.
    # upsert() overwrites record.updated_at with time.time() each call,
    # so we control time at the moment of upsert.
    real_time = time_mod.time
    now = real_time()
    day = 86400

    def fake_time():
        return fake_time.t
    fake_time.t = now

    monkeypatch.setattr(time_mod, "time", fake_time)
    # Also patch session_store's view of time.time — it's bound at
    # import time as the module-level default_factory, but the
    # dataclass calls it lazily so re-patching the time module
    # attribute should work since Python looks it up each call.
    import session_store as ss
    monkeypatch.setattr(ss.time, "time", fake_time)

    fake_time.t = now - 31 * day
    session_store.upsert(_make_record("old-done", status="done",
                                      updated_at=now - 31 * day))
    fake_time.t = now - 5 * day
    session_store.upsert(_make_record("recent-done", status="done",
                                      updated_at=now - 5 * day))
    fake_time.t = now - 8 * day
    session_store.upsert(_make_record("old-error", status="error",
                                      updated_at=now - 8 * day))
    fake_time.t = now - 1 * day
    session_store.upsert(_make_record("recent-cancelled", status="cancelled",
                                      updated_at=now - 1 * day))
    fake_time.t = now - 31 * day
    session_store.upsert(_make_record("old-running", status="running",
                                      updated_at=now - 31 * day))

    # Move clock back to "now" for the cleanup decision
    fake_time.t = now
    stats = cleanup._cleanup_session_store()

    assert stats["deleted"] == 2  # old-done + old-error
    assert stats["remaining"] == 3  # recent-done, recent-cancelled, old-running

    remaining = session_store.load_all()
    assert set(remaining.keys()) == {"recent-done", "recent-cancelled", "old-running"}


def test_cleanup_users_store_deletes_expired_sessions(tmp_path, monkeypatch):
    import users_store
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    users_store._reset_for_tests(tmp_path / "users.sqlite3")

    users_store.upsert_user("alice")
    expired = users_store.create_session("alice", ttl=1)
    fresh = users_store.create_session("alice", ttl=3600)
    time.sleep(1.5)

    stats = cleanup._cleanup_users_store()

    assert stats["sessions_deleted"] == 1
    assert users_store.get_session(expired.id) is None
    assert users_store.get_session(fresh.id) is not None


def test_cleanup_keeps_recent_records_intact(tmp_path, monkeypatch):
    import session_store
    import users_store
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    session_store._reset_for_tests(tmp_path / "watch-store")
    users_store._reset_for_tests(tmp_path / "users.sqlite3")

    session_store.upsert(_make_record("fresh", status="done", updated_at=time.time()))

    stats = cleanup._cleanup_session_store()

    assert stats["deleted"] == 0
    assert stats["remaining"] == 1
    assert "fresh" in session_store.load_all()


def test_main_always_exits_zero(tmp_path, monkeypatch):
    """Even if one store is unreadable, cleanup must exit 0 so
    cron doesn't page ops for a transient SQLite lock."""
    monkeypatch.setenv("WATCH_USERS_DB", "/nonexistent/dir/users.sqlite3")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "cleanup.py"), "--json", "--quiet"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path),
             "WATCH_USERS_DB": "/nonexistent/dir/users.sqlite3"},
        timeout=10,
    )
    assert result.returncode == 0, f"cleanup should exit 0:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert "error" in payload["users_store"]
    assert "session_store" in payload


def test_main_emits_valid_json(tmp_path, monkeypatch):
    """--json mode outputs valid machine-readable stats."""
    monkeypatch.setenv("WATCH_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "cleanup.py"), "--json", "--quiet"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path),
             "WATCH_USERS_DB": str(tmp_path / "users.sqlite3")},
        timeout=10,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "session_store" in payload
    assert "users_store" in payload
    assert "elapsed_seconds" in payload
    assert isinstance(payload["elapsed_seconds"], (int, float))
