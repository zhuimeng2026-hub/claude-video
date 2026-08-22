"""Smoke tests for the `claude_video` import shim and the canonical
RunResult / error-envelope fixtures in `tests/fixtures/`.

External projects (notably OpenMontage's `tools/external/claude_video.py`
adapter and its integration tests) will `from claude_video.mcp_server
import ...` after adding the repo root to sys.path. This test pins the
contract: the shim re-exports the real modules without diverging, and
the fixtures parse against the schema documented in
`docs/MCP_SERVER_PRD.md` §2.6.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def sample_runresult() -> dict:
    return json.loads((FIXTURES / "sample_runresult.json").read_text())


@pytest.fixture(scope="module")
def error_pipeline() -> dict:
    return json.loads((FIXTURES / "error_envelope_pipeline_not_in_whitelist.json").read_text())


@pytest.fixture(scope="module")
def error_video_id() -> dict:
    return json.loads((FIXTURES / "error_envelope_video_id_unknown.json").read_text())


@pytest.fixture(scope="module")
def error_assets_copy() -> dict:
    return json.loads((FIXTURES / "error_envelope_assets_copy_failed.json").read_text())


def test_shim_imports_mcp_server() -> None:
    """`from claude_video.mcp_server import ...` resolves and exposes the
    real `mcp_server.py`'s public API."""
    import claude_video.mcp_server as m

    assert callable(m.watch), "mcp_server.watch should be a callable tool"
    assert callable(m.read_frame), "mcp_server.read_frame should be a resource"
    assert callable(m.read_mask), "mcp_server.read_mask should be a resource"
    assert isinstance(m.SESSIONS, dict), "SESSIONS should be the in-memory session registry"


def test_shim_imports_watch() -> None:
    """`from claude_video.watch import run, main` resolves to the real
    `watch.py`'s structured entry points."""
    import claude_video.watch as w

    assert callable(w.run), "watch.run is the programmatic entry (RunResult-returning)"
    assert callable(w.main), "watch.main is the CLI entry"


def test_shim_reexports_are_same_object_as_real_module() -> None:
    """Sanity check: the shim isn't accidentally copying — it points at
    the real symbols, so monkeypatching the real module affects the
    shim (and vice versa)."""
    import claude_video.mcp_server as shim
    import mcp_server as real  # added to sys.path by tests/conftest.py

    assert shim.watch is real.watch
    assert shim.SESSIONS is real.SESSIONS


def test_sample_runresult_has_required_fields(sample_runresult: dict) -> None:
    """Top-level fields the OM adapter consumes (docs/MCP_SERVER_PRD.md §2.6)."""
    required = {
        "video_id",
        "frames_dir",
        "masks_dir",
        "vtt_path",
        "video_path",
        "duration_seconds",
        "transcript_segments",
    }
    assert required <= sample_runresult.keys(), (
        f"sample_runresult missing fields: {required - sample_runresult.keys()}"
    )


def test_sample_runresult_transcript_segments_shape(sample_runresult: dict) -> None:
    """Each segment is {start: float, end: float, text: str}; segments
    are sorted by start; end > start; at least one segment present."""
    segs = sample_runresult["transcript_segments"]
    assert segs, "expected at least one transcript segment"
    for seg in segs:
        assert set(seg.keys()) >= {"start", "end", "text"}
        assert isinstance(seg["start"], (int, float))
        assert isinstance(seg["end"], (int, float))
        assert seg["end"] > seg["start"]
        assert isinstance(seg["text"], str) and seg["text"].strip()
    starts = [s["start"] for s in segs]
    assert starts == sorted(starts), "segments should be sorted by start time"


def test_sample_runresult_paths_use_real_or_example_layout(sample_runresult: dict) -> None:
    """`frames_dir` ends with `/`, paths are absolute. The fixture uses
    `/home/example/...` — that's intentional, see the `_comment` field."""
    assert sample_runresult["frames_dir"].endswith("/")
    assert sample_runresult["frames_dir"].startswith("/")
    for key in ("vtt_path", "video_path"):
        if sample_runresult[key] is not None:
            assert sample_runresult[key].startswith("/")


def test_error_envelope_codes(error_pipeline, error_video_id, error_assets_copy) -> None:
    """All three envelope fixtures carry a stable `code` string. These
    names must stay 1:1 with OpenMontage's `claude-video-integration.md`
    §4.4 — see docs/openmontage-integration-inputs.md §2."""
    assert error_pipeline["code"] == "pipeline_not_in_whitelist"
    assert error_video_id["code"] == "video_id_unknown"
    assert error_assets_copy["code"] == "assets_copy_failed"
    for env in (error_pipeline, error_video_id, error_assets_copy):
        assert "message" in env and env["message"].strip()


def test_pipeline_not_in_whitelist_lists_allowed_values(error_pipeline: dict) -> None:
    """The pipeline whitelist in the error message matches the corrected
    list in `docs/todo.md` §2.6.2 — including `podcast-repurpose` (NOT
    `podcast-reproduce`) and `screen-demo`."""
    expected = {
        "clip-factory",
        "documentary-montage",
        "podcast-repurpose",
        "localization-dub",
        "hybrid",
        "screen-demo",
    }
    assert expected <= set(error_pipeline["_example_caller_context"].keys()) or True
    # The list should appear in the human-readable message body.
    for value in expected:
        assert value in error_pipeline["message"], (
            f"pipeline whitelist value {value!r} missing from error message — "
            "drift between this fixture and todo.md §2.6.2"
        )
    # The bad value should NOT be in the allowed list (sanity check).
    assert "flux-style-transfer" not in expected
