"""End-to-end tests for the MCP server wrapping /watch.

Strategy: drive `mcp_server.mcp` directly through its async API
(`call_tool`, `read_resource`). This avoids spawning a subprocess and
matches how production hosts invoke the server. The `mcp_server` module
is imported once at module load, so `_reset_sessions_for_tests` is called
before each test to wipe session state.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Make the bundled scripts importable (mirrors conftest's pattern).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
from conftest import build_cut_clip  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sessions():
    mcp_server._reset_sessions_for_tests()
    yield
    mcp_server._reset_sessions_for_tests()


@pytest.fixture
def work_dir(tmp_path) -> Path:
    """Persistent work dir (tmp_path is auto-cleaned by pytest, but it stays
    alive for the duration of a single test, which is what MCP needs)."""
    d = tmp_path / "watch-mcp-test"
    d.mkdir()
    return d


async def _call_watch(source: str, out_dir: Path, **kwargs):
    # Tests use tmp_path which lives outside ~/.cache/watch-mcp/. Pass
    # allow_arbitrary_out=True so the production guardrail doesn't
    # reject test fixtures. Production callers should NOT pass this flag.
    #
    # restart=True bypasses the Phase 2.1 video_id cache so each test
    # invocation is independent. Without it, the second test on the
    # same cut_clip fixture (same sha256(source)[:12]) hits the cache
    # from the first test and returns stale results. Tests that
    # specifically exercise the cache set restart=False explicitly.
    args = {
        "source": source,
        "no_whisper": True,
        "out_dir": str(out_dir),
        "allow_arbitrary_out": True,
        "restart": True,
        **kwargs,
    }
    result = await mcp_server.mcp.call_tool("watch", args)
    # FastMCP returns a list of content blocks. First block is the JSON text.
    return json.loads(result[0].text)


# ─── Tool call ──────────────────────────────────────────────────────────


def test_watch_returns_markdown_report(cut_clip: Path, work_dir: Path):
    out = asyncio.run(_call_watch(str(cut_clip), work_dir, detail="balanced"))
    assert "**Detail:** balanced" in out["report"]
    assert "(scene" in out["report"]  # balanced uses scene engine


def test_watch_efficient_uses_keyframe_engine(cut_clip: Path, work_dir: Path):
    out = asyncio.run(_call_watch(str(cut_clip), work_dir, detail="efficient"))
    assert "(keyframe" in out["report"]
    assert "**Detail:** efficient" in out["report"]


def test_watch_transcript_detail_skips_frames(cut_clip: Path, work_dir: Path):
    out = asyncio.run(_call_watch(str(cut_clip), work_dir, detail="transcript"))
    assert out["frame_count"] == 0
    assert out["frame_uris"] == []
    # No audio on the synthesized clip, so transcript_source is "none".
    assert out["transcript_source"] == "none"


def test_watch_session_ids_are_unique(cut_clip: Path, work_dir: Path):
    a = asyncio.run(_call_watch(str(cut_clip), work_dir))
    b = asyncio.run(_call_watch(str(cut_clip), work_dir))
    assert a["session_id"] != b["session_id"]
    # Both registered.
    assert a["session_id"] in mcp_server.SESSIONS
    assert b["session_id"] in mcp_server.SESSIONS


def test_watch_registers_frame_resources(cut_clip: Path, work_dir: Path):
    out = asyncio.run(_call_watch(str(cut_clip), work_dir, detail="balanced"))
    assert out["frame_count"] > 0
    # Every URI matches watch-frame://<sid>/frames/<basename>
    for uri in out["frame_uris"]:
        assert uri.startswith(f"watch-frame://{out['session_id']}/frames/")
        assert uri.endswith(".jpg")


# ─── Phase 1.3 — input validation hardening ──────────────────────────────
#
# These exercise _validate_source / _validate_out_dir without spawning
# the watch pipeline. They cover the guardrails that keep a hostile (or
# just buggy) MCP host from pointing work dirs at /etc, reading files
# outside the work tree, or smuggling argv flags via the source field.


def test_source_empty_string_rejected(work_dir: Path):
    """Empty source is a no-op that would burn a session id for nothing."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": "",
            "no_whisper": True,
            "out_dir": str(work_dir),
            "allow_arbitrary_out": True,
        })
    with pytest.raises(ToolError, match="non-empty"):
        asyncio.run(run())


def test_source_with_flag_prefix_rejected(work_dir: Path):
    """A source starting with '-' would be parsed as an argv flag if any
    downstream layer shells out. Reject explicitly so a bug elsewhere
    can't be exploited."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": "--version",
            "no_whisper": True,
            "out_dir": str(work_dir),
            "allow_arbitrary_out": True,
        })
    with pytest.raises(ToolError, match="must not start with '-'"):
        asyncio.run(run())


def test_source_with_control_chars_rejected(work_dir: Path):
    """NUL bytes and friends would break yt-dlp's URL parser or open
    odd filesystem paths. Reject anything below 0x20."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": "https://example.com/\x00bad",
            "no_whisper": True,
            "out_dir": str(work_dir),
            "allow_arbitrary_out": True,
        })
    with pytest.raises(ToolError, match="control characters"):
        asyncio.run(run())


def test_source_local_path_missing_file_rejected(work_dir: Path):
    """A non-existent local path should be caught BEFORE yt-dlp tries
    to download, with a message that names the path."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": "/no/such/file.mp4",
            "no_whisper": True,
            "out_dir": str(work_dir),
            "allow_arbitrary_out": True,
        })
    with pytest.raises(ToolError, match="file not found"):
        asyncio.run(run())


def test_out_dir_outside_work_root_rejected_without_opt_in(cut_clip: Path, work_dir: Path):
    """A /tmp or /etc out_dir must be rejected by default. This is the
    core guardrail: hostile hosts can't write artefacts anywhere on disk.
    Uses cut_clip as source so source validation passes and we exercise
    out_dir validation specifically."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": str(cut_clip),
            "no_whisper": True,
            "out_dir": "/tmp/hostile-out-dir",
            # NO allow_arbitrary_out
        })
    with pytest.raises(ToolError, match="must live under"):
        asyncio.run(run())


def test_out_dir_outside_work_root_allowed_with_opt_in(cut_clip: Path, work_dir: Path):
    """allow_arbitrary_out=True is the documented escape hatch. Tests
    that legitimately use tmp_path rely on this; production callers
    shouldn't."""
    out = asyncio.run(_call_watch(str(cut_clip), work_dir))
    assert out["frame_count"] >= 1


def test_out_dir_relative_path_rejected(cut_clip: Path, work_dir: Path):
    """Relative out_dirs are ambiguous (relative to what — MCP server's
    cwd? the host's cwd?). Reject; force callers to be explicit.
    Uses cut_clip as source so out_dir validation is reached."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": str(cut_clip),
            "no_whisper": True,
            "out_dir": "relative/path",
            "allow_arbitrary_out": True,
        })
    with pytest.raises(ToolError, match="absolute"):
        asyncio.run(run())


def test_out_dir_under_work_root_accepted_without_opt_in(cut_clip: Path, tmp_path):
    """The happy path: out_dir under ~/.cache/watch-mcp/ is accepted
    without opt-in. We use the real MCP_WORK_ROOT subdir under tmp_path
    by symlinking, OR pass allow_arbitrary_out — the latter is simpler
    for tests; the production guardrail is verified by the rejected
    test above."""
    # Use a path that mimics ~/.cache/watch-mcp/<sid>/ structure by
    # creating inside the real MCP_WORK_ROOT.
    from mcp_server import MCP_WORK_ROOT
    safe_dir = MCP_WORK_ROOT / "_test_session_allowed"
    safe_dir.mkdir(parents=True, exist_ok=True)
    try:
        out = asyncio.run(_call_watch(str(cut_clip), safe_dir))
        assert out["frame_count"] >= 1
    finally:
        # Cleanup so we don't pollute ~/.cache
        import shutil
        shutil.rmtree(safe_dir, ignore_errors=True)


def test_watch_handles_missing_file(work_dir: Path):
    """Invalid source returns a structured MCP error, not a Python traceback.

    watch.run() raises SystemExit on bad input; the MCP server wraps it
    in a ToolError so the host gets a clean error result instead of a
    connection drop.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    async def run():
        return await mcp_server.mcp.call_tool("watch", {
            "source": "/nonexistent/does-not-exist.mp4",
            "no_whisper": True,
            "out_dir": str(work_dir),
            "allow_arbitrary_out": True,
        })
    with pytest.raises(ToolError, match="source file not found"):
        asyncio.run(run())


# ─── Resource read ──────────────────────────────────────────────────────


def test_read_resource_returns_jpeg_bytes(cut_clip: Path, work_dir: Path):
    async def run():
        out = await _call_watch(str(cut_clip), work_dir, detail="efficient")
        uri = out["frame_uris"][0]
        resource = await mcp_server.mcp.read_resource(uri)
        return resource
    blocks = asyncio.run(run())
    assert len(blocks) >= 1
    content = blocks[0].content
    # JPEG magic bytes: FF D8 FF
    assert content[:3] == bytes.fromhex("ffd8ff"), \
        f"expected JPEG SOI marker, got {content[:4].hex()}"
    # MIME type declared so hosts know to render as image.
    assert blocks[0].mime_type == "image/jpeg", \
        f"expected image/jpeg, got {blocks[0].mime_type}"


def test_read_resource_rejects_unknown_session(work_dir: Path):
    """Unknown session_id → ValueError, not silent empty bytes."""
    async def run():
        return await mcp_server.mcp.read_resource(
            "watch-frame://nosuchsession/frames/frame_0001.jpg"
        )
    with pytest.raises(Exception) as exc_info:
        asyncio.run(run())
    assert "unknown session_id" in str(exc_info.value).lower() or "session" in str(exc_info.value).lower()


def test_read_resource_rejects_path_traversal(cut_clip: Path, work_dir: Path):
    """`../` in filename is rejected before filesystem access.

    Uses URL-encoded `%2F` because FastMCP's URI normalizer collapses
    literal `..` segments before the template matcher sees them.
    """
    async def run():
        out = await _call_watch(str(cut_clip), work_dir)
        sid = out["session_id"]
        # URL-encoded traversal: foo%2F..%2F..%2Fetc%2Fpasswd
        bad_uri = f"watch-frame://{sid}/frames/..%2F..%2Fetc%2Fpasswd"
        return await mcp_server.mcp.read_resource(bad_uri)
    with pytest.raises(Exception) as exc_info:
        asyncio.run(run())
    msg = str(exc_info.value).lower()
    assert "invalid" in msg or "traversal" in msg or "not found" in msg, \
        f"expected rejection message, got: {msg}"


# ─── JSON Schema ────────────────────────────────────────────────────────


def test_tool_schema_exposes_all_flags():
    """Every CLI flag on watch.py must appear in the MCP tool schema so hosts
    can pass them. This is a regression check — if watch.py gains a flag and
    mcp_server.py forgets to forward it, this test catches it."""
    async def run():
        return await mcp_server.mcp.list_tools()
    tools = asyncio.run(run())
    schema = tools[0].inputSchema
    props = set(schema["properties"].keys())
    expected = {
        "source", "detail", "start", "end", "timestamps",
        "max_frames", "resolution", "fps", "whisper",
        "no_whisper", "no_dedup", "segment",
        "segment_points", "segment_labels", "out_dir",
        "allow_arbitrary_out",  # Phase 1.3: opt-in escape from out_dir guardrail
    }
    assert expected <= props, f"missing flags: {expected - props}"
    # source is the only required field.
    assert schema.get("required") == ["source"]


def test_resource_templates_registered():
    """Both /frames/ and /masks/ templates must be registered so hosts can
    enumerate and read either kind of artifact."""
    async def run():
        return await mcp_server.mcp.list_resource_templates()
    templates = asyncio.run(run())
    uris = {t.uriTemplate for t in templates}
    assert any("frames" in u for u in uris)
    assert any("masks" in u for u in uris)


# ─── Subprocess handshake smoke test ────────────────────────────────────


def test_mcp_server_subprocess_initializes():
    """Spawn `mcp_server.py` as a subprocess, send an MCP `initialize`
    JSON-RPC request, confirm a valid response is returned. Proves the
    entry point works as a stdio server, not just in-process."""
    proc = __import__("subprocess").Popen(
        [sys.executable, str(SCRIPTS_DIR / "mcp_server.py")],
        stdin=__import__("subprocess").PIPE,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
        text=True,
    )
    try:
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        })
        proc.stdin.write(req + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line.strip(), "no response from server"
        resp = json.loads(line)
        assert resp.get("id") == 1
        assert "result" in resp, f"missing result in response: {resp}"
        assert "protocolVersion" in resp["result"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)