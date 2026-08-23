"""Concurrent atomic-write tests for session_store.

Uses multiprocessing.Pool(starmap) to spawn N=8 workers that hammer
the same sessions.json with interleaved upserts. Verifies:

1. Final file is parseable JSON (atomic write never produces torn writes)
2. Total record count matches expected (no lost updates)
3. Each video_id maps to a single, internally consistent record
"""
from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _worker_upsert(worker_id, store_root, batch_size):
    """Run in a child process. Three positional args (not a tuple) so
    `multiprocessing.Pool.starmap` can unpack (wid, root, batch) tuples
    from the iterable directly."""
    import session_store
    session_store._reset_for_tests(Path(store_root))

    for i in range(batch_size):
        vid = f"{worker_id:02d}{i:04d}{'a' * 6}"
        rec = session_store.SessionRecord(
            video_id=vid,
            work_dir=f"/tmp/worker-{worker_id}/rec-{i}",
            source=f"https://example.com/{vid}.mp4",
            status="done",
            frames=[{"path": f"/frames/{vid}.jpg", "t": 0.0}],
            user_openid=f"user-{worker_id}",
        )
        session_store.upsert(rec)

    all_records = session_store.load_all()
    for i in range(batch_size):
        vid = f"{worker_id:02d}{i:04d}{'a' * 6}"
        assert vid in all_records, f"worker {worker_id} lost record {vid}"
        assert all_records[vid].user_openid == f"user-{worker_id}"
        assert all_records[vid].work_dir == f"/tmp/worker-{worker_id}/rec-{i}"

    return worker_id, batch_size


def test_concurrent_upserts_no_lost_writes(tmp_path):
    """N=8 workers, each writing 25 unique records concurrently.
    Final file should have 200 records, none torn, none lost."""
    N = 8
    BATCH = 25
    store_root = tmp_path / "sessions"

    args = [(i, str(store_root), BATCH) for i in range(N)]
    with multiprocessing.get_context("spawn").Pool(processes=N) as pool:
        results = pool.starmap(_worker_upsert, args)

    import session_store
    session_store._reset_for_tests(store_root)
    all_records = session_store.load_all()

    assert len(all_records) == N * BATCH, (
        f"expected {N * BATCH} records, got {len(all_records)} - "
        f"concurrent writes lost data"
    )

    expected_total_frames = N * BATCH
    actual_total_frames = sum(len(r.frames) for r in all_records.values())
    assert actual_total_frames == expected_total_frames

    raw = (store_root / "sessions.json").read_text()
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert len(parsed) == N * BATCH


def test_upsert_is_idempotent(tmp_path):
    import session_store
    session_store._reset_for_tests(tmp_path)

    vid = "abc123def456"
    for i in range(5):
        rec = session_store.SessionRecord(
            video_id=vid,
            work_dir=f"/tmp/v{i}",
            source="https://example.com/same.mp4",
            frames=[],
        )
        session_store.upsert(rec)

    records = session_store.load_all()
    assert len(records) == 1
    assert records[vid].work_dir == "/tmp/v4"


def test_delete_returns_false_when_missing(tmp_path):
    import session_store
    session_store._reset_for_tests(tmp_path)
    assert session_store.delete("does-not-exist") is False


def test_delete_removes_record(tmp_path):
    import session_store
    session_store._reset_for_tests(tmp_path)
    rec = session_store.SessionRecord(
        video_id="vid1", work_dir="/tmp/x", source="x", frames=[]
    )
    session_store.upsert(rec)
    assert session_store.delete("vid1") is True
    assert session_store.get("vid1") is None


def test_list_for_user_openid_isolation(tmp_path):
    """list_for_user must only return records owned by the openid OR
    orphan records (Phase 2.1 placeholder; Phase 2.8 OAuth tightens)."""
    import session_store
    session_store._reset_for_tests(tmp_path)

    session_store.upsert(session_store.SessionRecord(
        video_id="owned_a", work_dir="/x", source="x",
        frames=[], user_openid="alice",
    ))
    session_store.upsert(session_store.SessionRecord(
        video_id="owned_b", work_dir="/y", source="y",
        frames=[], user_openid="bob",
    ))
    session_store.upsert(session_store.SessionRecord(
        video_id="orphan_c", work_dir="/z", source="z",
        frames=[], user_openid=None,
    ))

    alice_view = session_store.list_for_user("alice")
    assert {r.video_id for r in alice_view} == {"owned_a", "orphan_c"}

    bob_view = session_store.list_for_user("bob")
    assert {r.video_id for r in bob_view} == {"owned_b", "orphan_c"}

    anon_view = session_store.list_for_user(None)
    assert {r.video_id for r in anon_view} == {"orphan_c"}


def test_corrupt_file_treated_as_empty(tmp_path):
    import session_store
    store_root = tmp_path
    session_store._reset_for_tests(store_root)
    (store_root / "sessions.json").write_text("this is not JSON {", encoding="utf-8")
    assert session_store.load_all() == {}


def test_load_empty_when_no_file(tmp_path):
    import session_store
    session_store._reset_for_tests(tmp_path)
    assert session_store.load_all() == {}
