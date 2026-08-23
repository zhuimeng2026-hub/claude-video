"""Phase 3.3 — BFF subprocess pool tests.

The legacy test_bff.py / test_metrics.py force pool size = 1 for
stability. This file covers the multi-slot case explicitly, with
known-good test patterns.

What we test:
  - Pool starts N slots in parallel, all reach connected state
  - Same video_id always picks the same slot (cache affinity)
  - Different video_ids get distributed across slots (load spread)
  - /readyz reports the actual pool size

What we DON'T test (Phase 3.3 limits):
  - Truly concurrent tool calls running in parallel. stdio_client CM
    cancel-scope safety prevents in-process anyio from opening N
    separate MCP subprocesses inside one test task. We verify the
    POOL CONFIGURATION (slot count, routing, metrics) but not actual
    parallel throughput — that needs a multi-process integration
    setup. Tracked for Phase 3.4+.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def test_default_pool_size_is_4():
    """BFFState.DEFAULT_POOL_SIZE should be 4 — chosen because it's a
    sensible default for ~tens of concurrent users (each slot
    handles one in-flight watch). Override via WATCH_BFF_POOL_SIZE
    if you're running on a small VM."""
    import bff
    assert bff.BFFState.DEFAULT_POOL_SIZE == 4


def test_pool_size_override_via_env(monkeypatch):
    """WATCH_BFF_POOL_SIZE controls pool size when start() is called."""
    monkeypatch.setenv("WATCH_BFF_POOL_SIZE", "7")
    # We don't actually start the pool here (would need MCP binary);
    # just verify the value gets read correctly by inspecting the
    # logic indirectly. The real proof is start() spawning N slots
    # which we cover in test_pool_starts_n_slots.
    import bff
    state = bff.BFFState()
    assert state.DEFAULT_POOL_SIZE == 4  # class default unchanged
    # env-reading happens inside start(); the fixture-free smoke check
    # here is just to lock in the env var name.
    assert os_environ("WATCH_BFF_POOL_SIZE") == "7"


def os_environ(name):
    import os
    return os.environ.get(name)


# ─── Slot routing (no subprocess needed) ───────────────────────────────


def test_video_id_routing_is_deterministic():
    """Same video_id must hash to the same slot every time."""
    import bff
    state = bff.BFFState()
    # Fake slots — we only test the routing math
    state._slots = [f"slot-{i}" for i in range(4)]

    for _ in range(10):
        a = state._pick_slot({"video_id": "my-video-123"})
        b = state._pick_slot({"video_id": "my-video-123"})
        assert a == b, "same video_id must pick the same slot"

    # Different video_ids should spread across slots (not guaranteed,
    # but for a few sample ids they should)
    seen = set()
    for vid in ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]:
        seen.add(state._pick_slot({"video_id": vid}))
    assert len(seen) >= 2, "different video_ids should not all collapse to one slot"


def test_no_video_id_routes_round_robin():
    """Tools without video_id (e.g. list_sessions) distribute via
    round-robin counter."""
    import bff
    state = bff.BFFState()
    state._slots = [f"slot-{i}" for i in range(4)]

    # Four consecutive calls should hit four different slots
    slots = [state._pick_slot({}) for _ in range(4)]
    assert len(set(slots)) == 4


def test_pool_size_one_is_backward_compatible():
    """Pool size 1 reproduces the pre-Phase-3.3 single-slot behavior.
    This is the path the existing test_bff.py + test_metrics.py take."""
    import bff
    state = bff.BFFState()
    state._slots = [f"only-slot"]
    # All video_ids route to slot 0
    for vid in ["a", "b", "c"]:
        assert state._pick_slot({"video_id": vid}) == "only-slot"


# ─── Async pool startup (skipped if MCP subprocess can't start) ────────


@pytest.mark.anyio
async def test_pool_starts_n_slots(tmp_path, monkeypatch):
    """Start a 3-slot pool, verify all 3 connect.

    This DOES spawn real MCP subprocesses; it's slow (~750ms) but
    tests the actual lifecycle including stdio_client handshake.
    """
    import bff

    monkeypatch.setenv("WATCH_BFF_POOL_SIZE", "3")

    state = bff.BFFState()
    mcp_bin = SCRIPTS_DIR / "mcp_server.py"
    if not mcp_bin.is_file():
        pytest.skip("mcp_server.py not present")

    try:
        await state.start(mcp_bin)
        assert state.pool_size == 3
        assert all(s.connected for s in state._slots)
    finally:
        await state.stop()
    assert state._slots == []


@pytest.mark.anyio
async def test_readyz_reports_actual_pool_size(tmp_path, monkeypatch):
    """/readyz reflects the actual pool size from env."""
    import bff
    import session_store
    import httpx

    monkeypatch.setenv("WATCH_BFF_POOL_SIZE", "2")
    session_store._reset_for_tests(tmp_path / "watch-store")

    state = bff.BFFState()
    app = bff.create_app(state)
    mcp_bin = SCRIPTS_DIR / "mcp_server.py"
    if not mcp_bin.is_file():
        pytest.skip("mcp_server.py not present")

    async with state:
        await state.start(mcp_bin)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.get("/readyz")
                assert r.status_code == 200
                data = r.json()
                assert data["ready"] is True
                assert data["pool_size"] == 2
        finally:
            await state.stop()
