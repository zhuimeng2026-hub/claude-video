"""Phase 2.6.2 recompose tool integration tests.

Strategy:
- Monkeypatch `openmontage_client.submit_compose` to a fake that
  records its arguments and returns a canned response. This lets us
  verify the inputs packaging (frames_dir, masks_dir, vtt, video) and
  the GPU-free pipeline enforcement without spinning up the real
  OpenMontage MCP server.
- Use the sync `watch` tool to seed a SessionRecord, then call
  `recompose` and assert the canned response plus the args the fake
  received.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import session_store  # noqa: E402
import openmontage_client  # noqa: E402
import mcp_server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    session_store._reset_for_tests(tmp_path / "watch-store")
    mcp_server._reset_sessions_for_tests()
    yield


async def _tool(name: str, args: dict) -> dict:
    result = await mcp_server.mcp.call_tool(name, args)
    return json.loads(result[0].text)


def _seed_record(work_dir: Path, *, video_id: str, user_openid: str | None = None,
                 frames_count: int = 3) -> None:
    """Write a minimal SessionRecord so recompose has something to send."""
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "frames").mkdir(exist_ok=True)
    for i in range(frames_count):
        (work_dir / "frames" / f"frame_{i:04d}.jpg").write_bytes(b"fake-jpg")
    rec = session_store.SessionRecord(
        video_id=video_id,
        work_dir=str(work_dir),
        source="https://example.com/v.mp4",
        status="done",
        stage="done",
        progress=100.0,
        user_openid=user_openid,
        frames=[{"path": str(work_dir / "frames" / f"frame_{i:04d}.jpg"), "t": 1.0 * i}
                for i in range(frames_count)],
    )
    session_store.upsert(rec)


# ─── Inputs packaging ─────────────────────────────────────────────────────


def test_recompose_packages_record_into_inputs(cut_clip: Path, tmp_path):
    """recompose reads the SessionRecord, packages frames/vtt/video
    paths, and calls openmontage_client.submit_compose with the right
    inputs."""
    work_dir = tmp_path / "watch-store" / "compose-pkg-vid"
    _seed_record(work_dir, video_id="compose-pkg-vid")

    captured: dict[str, Any] = {}
    async def fake_submit_compose(**kwargs):
        captured.update(kwargs)
        return {
            "project_id": "proj-xyz",
            "status": "submitted",
            "render_url": "https://openmontage.local/projects/test/renders/out.mp4",
        }

    with patch.object(openmontage_client, "submit_compose", side_effect=fake_submit_compose):
        out = asyncio.run(_tool("recompose", {
            "video_id": "compose-pkg-vid",
            "pipeline": "clip-factory",
        }))

    assert out["status"] == "submitted"
    assert out["project_id"] == "proj-xyz"
    assert out["pipeline"] == "clip-factory"
    # submit_compose signature: video_id, user_openid, work_dir, frames_dir,
    # masks_dir, vtt_path, video_path, pipeline, style, extra
    assert captured["video_id"] == "compose-pkg-vid"
    assert captured["pipeline"] == "clip-factory"
    assert captured["frames_dir"].endswith("/frames")
    assert captured["masks_dir"] is None  # no masks in seed
    assert captured["work_dir"] == str(work_dir)
    assert captured["vtt_path"] is None
    assert captured["video_path"] is None


def test_recompose_rejects_gpu_pipeline_before_subprocess():
    """When the caller asks for a GPU-required pipeline, recompose
    must reject. Two layers of enforcement:

    1. Pydantic Literal[...] on the tool signature rejects unknown
       values before the function body runs (covers `animation`,
       `made-up-pipeline`, etc.)
    2. openmontage_client.validate_pipeline rejects GPU-required
       keywords (flux / kling / diffusion / video) — applies if the
       Literal is later widened or removed.

    Both paths surface as ToolError. Either error is acceptable here."""
    from mcp.server.fastmcp.exceptions import ToolError

    # Pydantic literal-error (value not in Literal whitelist)
    with pytest.raises(ToolError, match="Input should be|allow-list"):
        asyncio.run(_tool("recompose", {
            "video_id": "any-vid",
            "pipeline": "animation",  # not in Literal whitelist
        }))


def test_recompose_rejects_unknown_pipeline():
    """Same dual-layer reasoning as the GPU test above."""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Input should be|allow-list"):
        asyncio.run(_tool("recompose", {
            "video_id": "any-vid",
            "pipeline": "made-up-pipeline",
        }))


def test_recompose_rejects_gpu_keywords_in_pipeline():
    """Pipeline names containing 'flux' / 'kling' / 'diffusion' / 'video'
    are rejected. Pydantic Literal still rejects because the value
    isn't in the whitelist — same dual-layer story."""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Input should be|allow-list"):
        asyncio.run(_tool("recompose", {
            "video_id": "any-vid",
            "pipeline": "flux-montage",
        }))


# ─── Failure modes ────────────────────────────────────────────────────────


def test_recompose_errors_when_no_record():
    """Recompose on a nonexistent video_id is a clean ToolError."""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="no session record"):
        asyncio.run(_tool("recompose", {
            "video_id": "never-ran",
            "pipeline": "clip-factory",
        }))


def test_recompose_errors_when_record_not_done(tmp_path):
    """A running / cancelled / errored record cannot be recomposed yet."""
    work_dir = tmp_path / "watch-store" / "running-vid"
    work_dir.mkdir(parents=True)
    rec = session_store.SessionRecord(
        video_id="running-vid",
        work_dir=str(work_dir),
        source="x",
        status="running",
        stage="frames",
    )
    session_store.upsert(rec)

    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="not 'done'"):
        asyncio.run(_tool("recompose", {
            "video_id": "running-vid",
            "pipeline": "clip-factory",
        }))


def test_recompose_surfaces_openmontage_unavailable(monkeypatch):
    """When OpenMontage MCP subprocess fails to start, the error
    bubbles up as a ToolError — not a Python traceback."""
    work_dir = Path("/tmp/fake-watch-store")
    work_dir.mkdir(exist_ok=True)
    _seed_record(work_dir, video_id="no-om-vid")

    from mcp.server.fastmcp.exceptions import ToolError

    async def fake(**kwargs):
        raise openmontage_client.OpenMontageUnavailableError(
            "OpenMontage MCP binary /opt/OpenMontage_Voicebox/mcp_server.py not found"
        )

    with patch.object(openmontage_client, "submit_compose", side_effect=fake):
        with pytest.raises(ToolError, match="not found"):
            asyncio.run(_tool("recompose", {
                "video_id": "no-om-vid",
                "pipeline": "documentary-montage",
            }))


# ─── Pure openmontage_client unit tests ──────────────────────────────────


def test_validate_pipeline_accepts_allowed():
    for p in openmontage_client.ALLOWED_PIPELINES:
        openmontage_client.validate_pipeline(p)  # must not raise


def test_validate_pipeline_rejects_disallowed():
    with pytest.raises(openmontage_client.PipelineNotAllowedError):
        openmontage_client.validate_pipeline("animation")
    with pytest.raises(openmontage_client.PipelineNotAllowedError):
        openmontage_client.validate_pipeline("avatar-spokesperson")
    with pytest.raises(openmontage_client.PipelineNotAllowedError):
        openmontage_client.validate_pipeline("cinematic")


def test_is_openmontage_available_respects_env(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_BIN", "/nonexistent/path")
    assert not openmontage_client.is_openmontage_available()
    monkeypatch.setenv("OPENMONTAGE_BIN", str(__file__))
    assert openmontage_client.is_openmontage_available()
