"""Phase 2.7 BFF integration tests.

Uses `httpx.AsyncClient` + `ASGITransport` to exercise the FastAPI
app **in-process**, no real HTTP server, no real socket. The BFF's
lifespan spawns the actual mcp_server.py subprocess via stdio so
end-to-end coverage is honest (we test the same code path as a real
client).

Each test creates its own BFFState + ASGI app so the stdio MCP
subprocess doesn't leak between tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Per-test BFF state + redirected session_store.

    Phase 3.3 pool: force WATCH_BFF_POOL_SIZE=1 for these tests
    (each test owns one MCP subprocess). Multi-slot pool semantics
    have their own dedicated test in tests/test_subprocess_pool.py —
    spawning N stdio_client CMs in the same anyio task hits a
    cancel-scope safety check that's hard to bypass in unit tests.
    """
    import bff
    import session_store
    monkeypatch.setenv("WATCH_BFF_POOL_SIZE", "1")

    session_store._reset_for_tests(tmp_path / "watch-store")
    state = bff.BFFState()
    # Build a fresh app wired to this state
    app = bff.create_app(state)
    yield state, app
    # Cleanup is handled by lifespan teardown in each test


@asynccontextmanager
async def _with_state(state, app):
    """Run lifespan manually (ASGITransport doesn't always trigger it
    correctly with our custom lifespan; using the wrapper directly
    ensures the mcp subprocess actually starts). Uses state as async
    CM so start/stop happen in the same anyio task — required for
    stdio_client CM cancel-scope safety (Phase 3.3 pool)."""
    async with state:
        await state.start(SCRIPTS_DIR / "mcp_server.py")
        try:
            yield app
        finally:
            await state.stop()


# ─── Health ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_healthz_ok_when_started(isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
        ) as client:
            r = await client.get("/healthz")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"
            assert r.json()["mcp_connected"] is True


# ─── Auth ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_unauthenticated_request_returns_401(monkeypatch, isolated_state):
    """When WATCH_BFF_AUTH_TOKEN is set, requests without matching
    Bearer token get 401 with `not_authenticated` error."""
    monkeypatch.setenv("WATCH_BFF_AUTH_TOKEN", "secret-test-token")
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
        ) as client:
            r = await client.post("/api/watch/start", json={"source": "/nonexistent.mp4"})
            assert r.status_code == 401
            assert r.json()["detail"]["error"] == "not_authenticated"

            r2 = await client.post(
                "/api/watch/start",
                json={"source": "/nonexistent.mp4"},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert r2.status_code == 401


@pytest.mark.anyio
async def test_authenticated_request_passes(monkeypatch, isolated_state):
    monkeypatch.setenv("WATCH_BFF_AUTH_TOKEN", "secret-test-token")
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
            headers={"Authorization": "Bearer secret-test-token"},
        ) as client:
            # 404 source → real ToolError surfaces, but auth passed
            # (we don't care about the 400 response here)
            r = await client.post(
                "/api/watch/start", json={"source": "/nonexistent.mp4"}
            )
            # Got past auth, hit validation. Any non-401 means OK.
            assert r.status_code != 401


# ─── watch lifecycle ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_start_watch_via_rest_returns_running(cut_clip: Path, isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
        ) as client:
            r = await client.post("/api/watch/start", json={
                "source": str(cut_clip),
                "no_whisper": True,
                "allow_arbitrary_out": True,
                "video_id": "bff-start-vid",
            })
            assert r.status_code == 200
            data = r.json()
            assert data["video_id"] == "bff-start-vid"
            assert data["status"] == "running"


@pytest.mark.anyio
async def test_status_polls_to_done(cut_clip: Path, isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
        ) as client:
            await client.post("/api/watch/start", json={
                "source": str(cut_clip),
                "no_whisper": True,
                "allow_arbitrary_out": True,
                "video_id": "bff-done-vid",
            })

            deadline = time.time() + 30
            while time.time() < deadline:
                r = await client.get("/api/watch/bff-done-vid/status")
                assert r.status_code == 200
                if r.json()["status"] == "done":
                    assert r.json()["stage"] == "done"
                    return
                await asyncio.sleep(0.2)
            pytest.fail("video did not reach done within 30s")


# ─── SSE event stream ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sse_emits_stage_progression(cut_clip: Path, isolated_state):
    """GET /api/watch/{vid}/events streams at least download + done."""
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            await client.post("/api/watch/start", json={
                "source": str(cut_clip),
                "no_whisper": True,
                "allow_arbitrary_out": True,
                "video_id": "bff-sse-vid",
            })

            events: list[dict] = []
            async with client.stream(
                "GET", "/api/watch/bff-sse-vid/events"
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[len("data:"):].strip()))
                        except json.JSONDecodeError:
                            pass
                    if any(e.get("status") == "done" for e in events):
                        break

            statuses = [e.get("status") for e in events]
            assert "done" in statuses, f"SSE missing done; got {events}"
            # At least one stage progression observed before done
            assert any(e.get("stage") for e in events), \
                f"no stage events in SSE stream; got {events}"


# ─── frame resource ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_frame_resource_returns_jpeg(cut_clip: Path, isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            # Start + wait for completion
            start_resp = await client.post("/api/watch/start", json={
                "source": str(cut_clip),
                "no_whisper": True,
                "allow_arbitrary_out": True,
                "video_id": "bff-frame-vid",
                "detail": "efficient",
            })
            assert start_resp.status_code == 200
            start_data = start_resp.json()

            # Poll for done
            for _ in range(150):
                s = await client.get("/api/watch/bff-frame-vid/status")
                if s.json()["status"] == "done":
                    break
                await asyncio.sleep(0.2)

            # Use results to get session_id and frame URIs
            results = await client.get("/api/watch/bff-frame-vid/results")
            assert results.status_code == 200
            data = results.json()
            assert data["frame_count"] > 0
            first_uri = data["frame_uris"][0]
            session_id = first_uri.split("://", 1)[1].split("/", 1)[0]
            filename = first_uri.rsplit("/", 1)[1]

            frame_resp = await client.get(
                f"/api/watch/{session_id}/frame/{filename}"
            )
            assert frame_resp.status_code == 200, frame_resp.text
            assert frame_resp.headers["content-type"] == "image/jpeg"
            # JPEG SOI marker
            assert frame_resp.content[:3] == b"\xff\xd8\xff"


# ─── cancel ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cancel_via_rest(cut_clip: Path, isolated_state):
    state, app = isolated_state
    async with _with_state(state, app) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            await client.post("/api/watch/start", json={
                "source": str(cut_clip),
                "no_whisper": True,
                "allow_arbitrary_out": True,
                "video_id": "bff-cancel-vid",
            })
            r = await client.post("/api/watch/bff-cancel-vid/cancel")
            assert r.status_code == 200
            assert r.json()["cancelled"] is True

            # Poll: either done (race) or cancelled
            for _ in range(100):
                s = await client.get("/api/watch/bff-cancel-vid/status")
                if s.json()["status"] in ("cancelled", "done", "error"):
                    return
                await asyncio.sleep(0.1)
            pytest.fail("cancel did not terminate job")
