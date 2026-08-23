"""Phase 2.1 integration tests: video_id reuse + cross-user isolation.

These exercise the `watch` tool's caching path end-to-end through the MCP
layer, plus the new `list_sessions` / `delete_session` tools.

Strategy:
- Tests use a per-test tmp_path as session_store root so they don't
  pollute ~/.cache/watch-mcp/ and don't collide with each other.
- session_store._reset_for_tests() points the module at that root.
- `restart=False` (the production default) is what makes caching kick in.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import session_store  # noqa: E402
import mcp_server  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_session_store(tmp_path):
    """Redirect session_store to a per-test temp dir.

    Also clears the in-memory per-call SESSIONS dict so each test starts
    fresh on both layers (disk-backed registry + ephemeral call registry).
    """
    session_store._reset_for_tests(tmp_path / "watch-store")
    mcp_server._reset_sessions_for_tests()
    yield
    session_store._reset_for_tests(tmp_path / "watch-store")  # cleanup after


async def _watch(source, *, allow_arbitrary_out=True, restart=False, **kwargs):
    args = {
        "source": source,
        "no_whisper": True,
        "allow_arbitrary_out": allow_arbitrary_out,
        "restart": restart,
        **kwargs,
    }
    result = await mcp_server.mcp.call_tool("watch", args)
    # structured_output=False returns a bare list of content blocks; the
    # JSON-serialized dict is in the first TextContent's `.text`.
    return json.loads(result[0].text)


async def _tool(name, args):
    """Generic helper for list_sessions / delete_session."""
    result = await mcp_server.mcp.call_tool(name, args)
    return json.loads(result[0].text)


# ─── video_id reuse ────────────────────────────────────────────────────────


def test_second_call_same_source_reuses_cache(cut_clip: Path):
    """Same source twice → second call returns reused=True with the
    same frame paths, without re-running the pipeline. Note that the
    URI scheme uses per-call session_id, so the full URIs differ; we
    compare the underlying frame basenames which are the persistent
    part of the cache."""
    out1 = asyncio.run(_watch(str(cut_clip)))
    out2 = asyncio.run(_watch(str(cut_clip)))

    assert out1["reused"] is False
    assert out2["reused"] is True
    assert out1["video_id"] == out2["video_id"]

    def basenames(uris):
        return [Path(u).name for u in uris]
    assert basenames(out1["frame_uris"]) == basenames(out2["frame_uris"])
    # session_id is per-call (not cached), so it differs
    assert out1["session_id"] != out2["session_id"]


def test_explicit_video_id_overrides_source_hash(cut_clip: Path):
    """Caller can pin a video_id; same explicit id + same source = cache hit."""
    out1 = asyncio.run(_watch(str(cut_clip), video_id="my-fixed-id"))
    out2 = asyncio.run(_watch(str(cut_clip), video_id="my-fixed-id"))
    assert out1["video_id"] == "my-fixed-id"
    assert out2["video_id"] == "my-fixed-id"
    assert out2["reused"] is True


def test_different_video_ids_dont_collide(cut_clip: Path, tmp_path):
    """Same source but different explicit video_ids = two separate records."""
    out1 = asyncio.run(_watch(str(cut_clip), video_id="vid-A"))
    out2 = asyncio.run(_watch(str(cut_clip), video_id="vid-B"))
    assert out1["video_id"] == "vid-A"
    assert out2["video_id"] == "vid-B"
    assert out1["reused"] is False
    assert out2["reused"] is False
    assert out1["session_id"] != out2["session_id"]


def test_restart_bypasses_cache(cut_clip: Path):
    """restart=True forces a fresh pipeline run even when cache would hit."""
    out1 = asyncio.run(_watch(str(cut_clip)))
    out2 = asyncio.run(_watch(str(cut_clip), restart=True))
    assert out1["reused"] is False
    assert out2["reused"] is False
    # Frame URIs should still be similar (same content), but session_id
    # is fresh because the second call re-ran.
    assert out1["session_id"] != out2["session_id"]


def test_cache_persists_across_module_reload(cut_clip: Path, tmp_path):
    """Simulate server restart: clear in-memory SESSIONS, keep disk
    session_store. Second call should still hit cache."""
    out1 = asyncio.run(_watch(str(cut_clip)))
    # Simulate server restart: clear per-call SESSIONS but keep session_store
    mcp_server._reset_sessions_for_tests()
    out2 = asyncio.run(_watch(str(cut_clip)))
    assert out2["reused"] is True
    assert out1["video_id"] == out2["video_id"]


# ─── WeChat placeholder fields ─────────────────────────────────────────────


def test_user_openid_is_stored(cut_clip: Path):
    """user_openid is stored in the persistent record (Phase 2.1 placeholder
    until Phase 2.8 wires real OAuth verification)."""
    out = asyncio.run(_watch(str(cut_clip), user_openid="alice-openid-123",
                              user_unionid="alice-unionid-456",
                              auth_source="wechat_mp"))
    vid = out["video_id"]
    record = session_store.get(vid)
    assert record is not None
    assert record.user_openid == "alice-openid-123"
    assert record.user_unionid == "alice-unionid-456"
    assert record.auth_source == "wechat_mp"


def test_user_openid_none_by_default(cut_clip: Path):
    """When caller doesn't pass user_openid, the record's field is None."""
    out = asyncio.run(_watch(str(cut_clip)))
    record = session_store.get(out["video_id"])
    assert record.user_openid is None
    assert record.auth_source == "none"


# ─── list_sessions ─────────────────────────────────────────────────────────


def _list(user_openid=None):
    return asyncio.run(_tool("list_sessions", {"user_openid": user_openid}))


def test_list_sessions_anon_sees_only_orphans(cut_clip: Path):
    """Caller without openid sees only records with no openid tag."""
    asyncio.run(_watch(str(cut_clip), user_openid="alice"))
    asyncio.run(_watch(str(cut_clip), video_id="orphan-vid"))  # no openid

    out = _list()  # no openid
    sids = {s["video_id"] for s in out["sessions"]}
    # Orphan (no openid) is visible. Alice's tagged one is not.
    assert "orphan-vid" in sids
    # Alice's record should NOT be in the anon view
    alice_vid = session_store.list_for_user(None)
    assert all(r.user_openid is None for r in alice_vid)


def test_list_sessions_with_openid_sees_own_plus_orphans(cut_clip: Path):
    """Alice's view: her records + orphans, NOT Bob's."""
    asyncio.run(_watch(str(cut_clip), video_id="alice-vid", user_openid="alice"))
    asyncio.run(_watch(str(cut_clip), video_id="bob-vid", user_openid="bob"))
    asyncio.run(_watch(str(cut_clip), video_id="orphan-1"))  # no openid

    out = _list(user_openid="alice")
    sids = {s["video_id"] for s in out["sessions"]}
    assert "alice-vid" in sids
    assert "orphan-1" in sids
    assert "bob-vid" not in sids


# ─── delete_session ────────────────────────────────────────────────────────


def _delete(video_id, user_openid=None):
    return asyncio.run(_tool(
        "delete_session", {"video_id": video_id, "user_openid": user_openid}
    ))


def test_delete_session_orphan_by_anyone(cut_clip: Path):
    """Records without openid can be deleted by anyone (Phase 2.1 placeholder).
    Phase 2.8 OAuth will tighten this — orphan records will be invisible
    to non-authenticated callers."""
    asyncio.run(_watch(str(cut_clip), video_id="orphan-1"))
    result = _delete("orphan-1", user_openid="alice")
    assert result["deleted"] is True
    assert session_store.get("orphan-1") is None


def test_delete_session_cross_user_rejected(cut_clip: Path):
    """Alice cannot delete Bob's record."""
    asyncio.run(_watch(str(cut_clip), video_id="bob-vid", user_openid="bob"))
    with pytest.raises(Exception) as exc_info:
        _delete("bob-vid", user_openid="alice")
    assert "forbidden" in str(exc_info.value).lower()


def test_delete_session_owner_can_delete(cut_clip: Path):
    asyncio.run(_watch(str(cut_clip), video_id="alice-vid", user_openid="alice"))
    result = _delete("alice-vid", user_openid="alice")
    assert result["deleted"] is True


def test_delete_session_not_found(cut_clip: Path):
    """Deleting a nonexistent video_id returns deleted=False, reason=not_found."""
    result = _delete("nope-no-such-vid")
    assert result["deleted"] is False
    assert result["reason"] == "not_found"
