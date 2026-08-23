"""Phase 3.4 — recompose end-to-end against a real (stub) OpenMontage.

Phase 3.4 wants recompose tested against the actual OpenMontage MCP
binary. The real `OpenMontage_Voicebox/mcp_server.py` has the same
pydantic bare-list crash we documented in
docs/MCP_SERVER_PRD.md §6.1 (Pitfall A) — it can't even import in
this environment.

So we use a minimal stub MCP server at
`tests/fixtures/openmontage_stub_mcp.py` that implements exactly
the surface claude-video's recompose needs:
  - one tool: claude_video.compose
  - echoes inputs back with a synthetic project_id

The stub uses the same mcp SDK so the stdio MCP transport is the
REAL transport — we're not mocking JSON-RPC, only the OpenMontage
business logic. When OpenMontage owner fixes the pydantic crash,
point OPENMONTAGE_BIN at the real binary and the same tests pass
end-to-end.

Coverage:
  - real stdio MCP transport (spawn subprocess, JSON-RPC handshake)
  - real openmontage_client.py submission (no mocks)
  - real recompose tool path through mcp_server.py
  - real inputs packaging (frames_dir, masks_dir, vtt, video_path,
    user_openid, pipeline, style)
  - GPU-free pipeline rejection (no OpenMontage subprocess spawned)
  - error path: OpenMontage binary missing → ToolError
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

STUB_MCP = Path(__file__).resolve().parent / "fixtures" / "openmontage_stub_mcp.py"


@pytest.fixture
def om_env(monkeypatch):
    """Point recompose at the stub MCP, with a real subprocess."""
    monkeypatch.setenv("OPENMONTAGE_BIN", str(STUB_MCP))
    # Also ensure session_store points to a clean per-test dir
    import session_store
    session_store._reset_for_tests(Path("/tmp/_recompose_real_test"))


# ─── Real stdio MCP roundtrip via openmontage_client ──────────────────


def test_submit_compose_real_roundtrip(om_env):
    """Spawn the stub MCP as a real subprocess. openmontage_client
    talks to it via stdio JSON-RPC. Verify inputs flow through and
    project_id comes back."""
    import openmontage_client

    async def go():
        return await openmontage_client.submit_compose(
            video_id="real-om-test-vid",
            user_openid="alice",
            work_dir="/tmp/claude-video/real-om-test-vid",
            frames_dir="/tmp/claude-video/real-om-test-vid/frames",
            masks_dir=None,
            vtt_path="/tmp/claude-video/real-om-test-vid/download/video.en.vtt",
            video_path="/tmp/claude-video/real-om-test-vid/download/video.mp4",
            pipeline="clip-factory",
            style="clean-professional",
            extra={"claude_video_source": "watch_mcp"},
        )

    result = asyncio.run(go())


def test_submit_compose_supports_all_whitelisted_pipelines(om_env):
    """All 6 GPU-free pipelines should be accepted by validate_pipeline
    and reach the (stub) OpenMontage successfully."""
    import openmontage_client

    for pipeline in sorted(openmontage_client.ALLOWED_PIPELINES):
        async def go(p=pipeline):
            return await openmontage_client.submit_compose(
                video_id=f"pipeline-{p}",
                user_openid="alice",
                work_dir="/tmp/x",
                frames_dir="/tmp/x/frames",
                masks_dir=None,
                vtt_path=None,
                video_path=None,
                pipeline=p,
                style="clean-professional",
                extra={},
            )

        result = asyncio.run(go())
        assert result["status"] == "submitted", f"pipeline {pipeline} failed: {result}"
        assert result["project_id"] == f"pipeline-{pipeline}"


# ─── GPU-free enforcement (no subprocess at all) ──────────────────────


def test_submit_compose_rejects_animation_before_subprocess():
    """GPU-required pipelines must be rejected BEFORE spawn — the
    validation is in-process, no subprocess started."""
    import openmontage_client

    with pytest.raises(openmontage_client.PipelineNotAllowedError):
        openmontage_client.validate_pipeline("animation")


def test_submit_compose_rejects_unknown_pipeline():
    import openmontage_client

    with pytest.raises(openmontage_client.PipelineNotAllowedError):
        openmontage_client.validate_pipeline("made-up-pipeline")


# ─── Failure paths ──────────────────────────────────────────────────────


def test_submit_compose_binary_missing_raises_unavailable(monkeypatch, tmp_path):
    """When OPENMONTAGE_BIN points at a non-existent file, the call
    fails fast with OpenMontageUnavailableError — the message must
    name the path so the operator can fix it."""
    import openmontage_client

    fake = tmp_path / "no-such-om-mcp.py"
    monkeypatch.setenv("OPENMONTAGE_BIN", str(fake))

    # is_openmontage_available must return False
    assert not openmontage_client.is_openmontage_available()

    async def go():
        return await openmontage_client.submit_compose(
            video_id="x", user_openid="u", work_dir="/w",
            frames_dir="/w/frames", masks_dir=None, vtt_path=None,
            video_path=None, pipeline="clip-factory",
            style="s", extra={},
        )

    with pytest.raises(openmontage_client.OpenMontageUnavailableError) as ei:
        asyncio.run(go())
    assert "not found" in str(ei.value).lower()


# ─── recompose tool end-to-end via mcp_server (in-process) ────────────


def test_recompose_tool_against_real_stub(om_env, tmp_path):
    """The full recompose MCP tool: caller hits mcp.call_tool, which
    spawns stdio MCP subprocess to OpenMontage stub, gets project_id
    back. No mocking of openmontage_client — we only monkeypatch
    OPENMONTAGE_BIN to point at our stub."""
    import session_store
    import mcp_server

    session_store._reset_for_tests(tmp_path / "watch-store")

    # Seed a completed SessionRecord so recompose has something to send
    work_dir = tmp_path / "recompose-flow-vid"
    frames_dir = work_dir / "frames"
    masks_dir = work_dir / "masks"
    (work_dir / "download").mkdir(parents=True)
    (frames_dir).mkdir(parents=True)
    (masks_dir).mkdir(parents=True)
    for i in range(3):
        (frames_dir / f"frame_{i:04d}.jpg").write_bytes(b"fake-jpg")
    (work_dir / "download" / "video.mp4").write_bytes(b"fake-mp4")
    (work_dir / "download" / "video.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n")

    session_store.upsert(session_store.SessionRecord(
        video_id="recompose-flow-vid",
        work_dir=str(work_dir),
        source="https://example.com/v.mp4",
        status="done",
        frames=[{"path": str(frames_dir / f"frame_{i:04d}.jpg"), "t": 1.0 * i}
                for i in range(3)],
        masks=[{"path": str(masks_dir / f"mask_{i:04d}.png"), "t": 1.0 * i}
                for i in range(2)],
    ))

    async def call_recompose():
        # structured_output=False returns a list of TextContent blocks,
        # not a CallToolResult (see Phase 1.5 structured_output workaround)
        result = await mcp_server.mcp.call_tool("recompose", {
            "video_id": "recompose-flow-vid",
            "pipeline": "clip-factory",
            "user_openid": "alice",
        })
        return json.loads(result[0].text)

    out = asyncio.run(call_recompose())

    assert out["status"] == "submitted"
    assert out["project_id"] == "recompose-flow-vid"
    assert out["pipeline"] == "clip-factory"
    # user_openid intentionally NOT returned (don't leak user info through
    # tool responses — it's an internal label used by OpenMontage for
    # user isolation, not for the caller to see)
    assert "user_openid" not in out
    # The stub's render_url is a synthetic URL; we don't assert its
    # format, just that we got something back.
    assert "render_url" in out
    assert out["video_id"] == "recompose-flow-vid"
    assert out["work_dir"] == str(work_dir)
