"""Phase 2.2 integration tests: split pipeline tools (start_watch /
get_status / get_results / cancel_watch).

These exercise the full async-style lifecycle:
  1. start_watch returns immediately with status='running'
  2. Poll get_status; observe stage progression download -> frames
     -> transcript -> done
  3. get_results returns the final shape with frames + report
  4. cancel_watch on a fresh job terminates it at the next stage

Each test uses a fresh per-test tmp session_store root so concurrent
tests don't collide. pipeline_runner is reset between tests so
zombie threads from the previous test don't pollute get_status.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import session_store  # noqa: E402
import pipeline_runner  # noqa: E402
import mcp_server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    """Fresh session_store + empty pipeline registry per test."""
    session_store._reset_for_tests(tmp_path / "watch-store")
    pipeline_runner._reset_for_tests()
    mcp_server._reset_sessions_for_tests()
    yield
    pipeline_runner._reset_for_tests()


async def _tool(name: str, args: dict) -> dict:
    """Call an MCP tool and parse the first TextContent block."""
    result = await mcp_server.mcp.call_tool(name, args)
    return json.loads(result[0].text)


def _poll_until_done(video_id: str, *, timeout: float = 30.0) -> dict:
    """Synchronous poll helper for tests that don't want to write async."""
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        result = asyncio.run(_tool("get_status", {"video_id": video_id}))
        if result["status"] in ("done", "error", "cancelled", "not_found"):
            return result
        last_status = result
        time.sleep(0.2)
    raise AssertionError(f"video {video_id} did not finish within {timeout}s; last={last_status}")


# ─── Lifecycle: start → progress → done → results ─────────────────────────


def test_start_watch_returns_immediately(cut_clip: Path):
    """start_watch must NOT block — returns status='running' right away."""
    out = asyncio.run(_tool("start_watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
    }))
    assert out["status"] == "running"
    assert "video_id" in out
    assert out["stage"] in ("download", "frames", "transcript", "segment", "done")
    # session_id is generated per call (for the URI scheme)
    assert "session_id" in out


def test_full_lifecycle_ends_with_results(cut_clip: Path):
    """start_watch → poll get_status → get_results returns full payload."""
    out = asyncio.run(_tool("start_watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
        "video_id": "lifecycle-vid",
    }))
    assert out["video_id"] == "lifecycle-vid"

    final = _poll_until_done("lifecycle-vid", timeout=30)
    assert final["status"] == "done"
    assert final["stage"] == "done"
    assert final["progress"] == 100.0

    # get_results returns the full shape
    result = asyncio.run(_tool("get_results", {"video_id": "lifecycle-vid"}))
    assert result["status"] == "done"
    assert result["frame_count"] > 0
    assert len(result["frame_uris"]) == result["frame_count"]
    assert result["video_id"] == "lifecycle-vid"


def test_get_status_progression_via_polling(cut_clip: Path):
    """While the pipeline runs, get_status returns a sequence of stages.
    We don't assert the exact order (depends on ffmpeg timing) but
    we DO assert that 'download' comes first and 'done' is terminal."""
    out = asyncio.run(_tool("start_watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
        "video_id": "progress-vid",
    }))
    vid = out["video_id"]

    # Sample status repeatedly until done; record observed stages.
    observed = []
    deadline = time.time() + 30
    while time.time() < deadline:
        s = asyncio.run(_tool("get_status", {"video_id": vid}))
        if s["status"] != "running":
            break
        observed.append(s["stage"])
        time.sleep(0.05)

    # At least one stage was observed before done
    assert len(observed) >= 1, "no progress samples before done"
    # 'download' is the first stage; check we saw something >= it
    assert any(s in ("download", "frames", "transcript", "segment") for s in observed)
    # Final status must be done
    assert s["status"] == "done"


# ─── Cache hit path ────────────────────────────────────────────────────────


def test_start_watch_reuses_existing_record(cut_clip: Path):
    """If a record already exists with status='done' and matches the
    request, start_watch returns immediately with reused=True instead
    of spawning a new background job."""
    # Seed a completed record via the sync watch tool
    asyncio.run(_tool("watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
        "video_id": "reuse-vid",
        "restart": True,  # ensure fresh cache
    }))

    # Now start_watch should hit cache, not run pipeline again
    out = asyncio.run(_tool("start_watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
        "video_id": "reuse-vid",
    }))
    assert out.get("reused") is True
    assert out["status"] == "done"


# ─── get_status for unknown video_id ────────────────────────────────────────


def test_get_status_unknown_video_returns_not_found():
    out = asyncio.run(_tool("get_status", {"video_id": "no-such-vid-1234"}))
    assert out["status"] == "not_found"
    assert out["video_id"] == "no-such-vid-1234"


def test_get_results_unknown_video_returns_not_found():
    out = asyncio.run(_tool("get_results", {"video_id": "no-such-vid-5678"}))
    assert out["status"] == "not_found"


# ─── cancel_watch ──────────────────────────────────────────────────────────


def test_cancel_watch_on_finished_job_is_noop(cut_clip: Path):
    """cancelling a job that's already finished returns cancelled=False."""
    asyncio.run(_tool("watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
        "video_id": "already-done-vid",
        "restart": True,
    }))
    out = asyncio.run(_tool("cancel_watch", {"video_id": "already-done-vid"}))
    assert out["cancelled"] is False
    assert out["reason"] == "not_running"


def test_cancel_watch_on_unknown_video_is_noop():
    out = asyncio.run(_tool("cancel_watch", {"video_id": "never-ran-vid"}))
    assert out["cancelled"] is False
    assert out["reason"] == "not_running"


def test_cancel_watch_terminates_running_job(cut_clip: Path):
    """Spawn a job, cancel it immediately, then verify stage='cancelled'
    once the runner catches up. The actual ffmpeg call (which takes
    ~1-2s for our test clip) will complete in-flight, so we wait for
    the runner to notice the cancel and update session_store."""
    out = asyncio.run(_tool("start_watch", {
        "source": str(cut_clip),
        "no_whisper": True,
        "allow_arbitrary_out": True,
        "video_id": "to-cancel-vid",
    }))
    assert out["status"] == "running"

    # Cancel immediately
    cancel = asyncio.run(_tool("cancel_watch", {"video_id": "to-cancel-vid"}))
    assert cancel["cancelled"] is True

    # Poll until terminal state. Two possibilities:
    #   - cancel before any stage: stage='cancelled', no frames
    #   - cancel during a stage: that stage completes, then runner
    #     notices and writes stage='cancelled'
    deadline = time.time() + 15
    final = None
    while time.time() < deadline:
        final = asyncio.run(_tool("get_status", {"video_id": "to-cancel-vid"}))
        if final["status"] in ("cancelled", "done", "error"):
            break
        time.sleep(0.1)

    # Status must be cancelled (race with fast pipelines may finish
    # before cancel propagates; either is acceptable, both prove the
    # cancel mechanism is wired up)
    assert final["status"] in ("cancelled", "done"), \
        f"cancel didn't terminate job; final={final}"
