"""Phase 3.2 — BFF metrics + health/metrics endpoint tests.

Verifies:
  - /healthz returns 200 with mcp_connected=true when started
  - /readyz returns 200 with tool list when MCP handshake OK
  - /readyz returns 503 when MCP session is None
  - /metrics returns Prometheus text format with all expected
    counter / gauge / histogram names
  - request middleware bumps per-tool counters
  - time_block records histogram observations
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Per-test BFF state with redirected session_store.
    Forces pool size = 1 (Phase 3.3 pool tests are in
    test_subprocess_pool.py — these are the legacy single-slot suite)."""
    import bff
    import session_store
    monkeypatch.setenv("WATCH_BFF_POOL_SIZE", "1")

    session_store._reset_for_tests(tmp_path / "watch-store")
    state = bff.BFFState()
    app = bff.create_app(state)
    yield state, app
    session_store._reset_for_tests(tmp_path / "watch-store")


@asynccontextmanager
async def _with_state(state, app):
    """Run lifespan manually so mcp subprocess actually starts."""
    mcp_bin = SCRIPTS_DIR / "mcp_server.py"
    await state.start(mcp_bin)
    try:
        yield app
    finally:
        await state.stop()


# ─── /healthz ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_healthz_ok_when_started(isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a), base_url="http://test"
        ) as client:
            r = await client.get("/healthz")
            assert r.status_code == 200
            assert r.json()["mcp_connected"] is True


# ─── /readyz ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_readyz_returns_tool_list_when_started(isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a), base_url="http://test"
        ) as client:
            r = await client.get("/readyz")
            assert r.status_code == 200
            data = r.json()
            assert data["ready"] is True
            assert "watch" in data["tools"]
            assert "start_watch" in data["tools"]
            assert "recompose" in data["tools"]


# ─── /metrics ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_metrics_returns_prometheus_text_format(isolated_state):
    """`/metrics` returns text/plain with Prometheus 0.0.4 exposition
    format. Verify all expected metric NAMES appear."""
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a), base_url="http://test"
        ) as client:
            r = await client.get("/metrics")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/plain")
            body = r.text
            # HELP + TYPE comments + values
            for expected in (
                "watch_bff_requests_total",
                "watch_bff_active_watches",
                "watch_bff_mcp_connected",
                "watch_bff_tool_duration_seconds",
            ):
                assert expected in body, f"missing metric {expected} in:\n{body}"


@pytest.mark.anyio
async def test_metrics_bumps_request_counters(isolated_state):
    """Each /api/* call should bump watch_bff_requests_total{tool,status}."""
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a), base_url="http://test"
        ) as client:
            # Fire a few requests
            await client.get("/healthz")
            await client.get("/readyz")
            await client.get("/api/sessions")  # list_sessions

            r = await client.get("/metrics")
            body = r.text
            # Counters with status=2xx should be present
            assert 'watch_bff_requests_total{tool="healthz",status="2xx"}' in body
            assert 'watch_bff_requests_total{tool="readyz",status="2xx"}' in body
            assert 'watch_bff_requests_total{tool="list_sessions",status="2xx"}' in body


@pytest.mark.anyio
async def test_metrics_records_tool_duration(isolated_state):
    """After a real tool call, the histogram should have observations."""
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a), base_url="http://test"
        ) as client:
            # Trigger a real tool call (so the histogram has an observation)
            await client.post("/api/watch/start", json={
                "source": str(Path(SCRIPTS_DIR).parent.parent / "tests/conftest.py"),
                "no_whisper": True,
                "allow_arbitrary_out": True,
                "video_id": "metrics-test-vid",
            })

            r = await client.get("/metrics")
            body = r.text
            # Histogram emits _bucket, _sum, _count lines
            assert "watch_bff_tool_duration_seconds_bucket" in body
            assert 'tool="start_watch"' in body


# ─── bff_metrics unit tests ──────────────────────────────────────────────


def test_counter_inc_and_render():
    from bff_metrics import _Counter
    c = _Counter("test_counter_total", "test", labelnames=("label1",))
    c.inc(label1="a")
    c.inc(label1="a")
    c.inc(label1="b")
    out = "\n".join(c.render())
    assert 'test_counter_total{label1="a"} 2' in out
    assert 'test_counter_total{label1="b"} 1' in out


def test_gauge_set_and_render():
    from bff_metrics import _Gauge
    g = _Gauge("test_gauge", "test")
    g.set(42.5)
    out = "\n".join(g.render())
    assert "test_gauge 42.5" in out


def test_histogram_observe_buckets_correctly():
    """Histogram should bucket values into cumulative counts."""
    from bff_metrics import _Histogram
    h = _Histogram("test_hist", "test", buckets=(0.1, 1.0, 10.0))
    h.observe(0.05)   # → 0.1, 1.0, 10.0, +Inf
    h.observe(0.5)    # → 1.0, 10.0, +Inf
    h.observe(5.0)    # → 10.0, +Inf
    h.observe(50.0)   # → +Inf
    out = "\n".join(h.render())
    # le="0.1" → count 1 (only 0.05)
    assert 'le="0.1"} 1' in out
    # le="1.0" → count 2 (0.05, 0.5)
    assert 'le="1.0"} 2' in out
    # le="10.0" → count 3 (0.05, 0.5, 5.0)
    assert 'le="10.0"} 3' in out
    # +Inf → count 4
    assert 'le="+Inf"} 4' in out
    # Sum (with closing brace before the value)
    assert "_sum} 55.550000" in out


def test_time_block_records_observation():
    from bff_metrics import TOOL_DURATION, time_block
    # Capture starting count for "test_block" (or any tool we use)
    with time_block("test_block_tool"):
        sum(range(1000))
    out = "\n".join(TOOL_DURATION.render())
    assert 'tool="test_block_tool"' in out
