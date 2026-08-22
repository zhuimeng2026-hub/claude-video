#!/usr/bin/env python3
"""MCP server wrapping /watch. stdio JSON-RPC transport.

Exposes one tool, `watch`, that mirrors watch.py's CLI surface. Each
extracted frame is also registered as an MCP resource under the
`watch-frame://<session_id>/<filename>` URI scheme so MCP clients can fetch
raw JPEG / PNG bytes without filesystem access.

This is the project's first third-party Python dependency (`mcp`). The
server is excluded from the claude.ai `.skill` bundle via
`skills/watch/.skillignore` because claude.ai's sandboxed skill runtime
does not host stdio MCP servers — but it ships via `npx skills add` for
Codex, Cursor, and openclaw local agents.

Usage (host config):
    openclaw mcp add --transport stdio claude-video \\
        python3 /absolute/path/to/skills/watch/scripts/mcp_server.py

See docs/MCP_SERVER_PRD.md for the full interface contract.
"""
from __future__ import annotations

import argparse
import base64
import sys
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import watch as watch_mod  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

mcp = FastMCP("claude-video")

# In-memory session registry. Lifetime = server process lifetime. Stdio
# transport ties server lifetime to client lifetime, so this is bounded by
# the MCP host's connection. URIs are valid only while the server is alive.
SESSIONS: dict[str, dict] = {}


def _reset_sessions_for_tests() -> None:
    """Clear the session registry. Test-only helper."""
    SESSIONS.clear()


def _make_session_id() -> str:
    """12-hex-char session id — short enough for URIs, long enough for uniqueness."""
    return uuid.uuid4().hex[:12]


@mcp.tool(
    name="watch",
    description=(
        "Watch a video (URL or local path). Downloads with yt-dlp, extracts "
        "auto-scaled frames with ffmpeg, pulls the transcript from captions "
        "(or Whisper fallback), and returns a markdown report with absolute "
        "frame paths. Each frame is also readable as an MCP resource "
        "(watch-frame://<session_id>/<filename>, image/jpeg).\n\n"
        "Use --detail to trade fidelity for speed: 'transcript' skips frames "
        "(downloads video only if no captions), 'efficient' is fast keyframes "
        "(cap 50), 'balanced' is scene-aware (cap 100, default), 'token-burner' "
        "is uncapped.\n\n"
        "Use --start/--end ('SS', 'MM:SS', or 'HH:MM:SS') to focus on a time "
        "range — denser frame budget inside the window. Use --timestamps for "
        "transcript-cue frames at specific moments ('look here', 'as you can see').\n\n"
        "Returns: {report: str (markdown), session_id: str, work_dir: str, "
        "frame_uris: list[str], frame_count: int, transcript_source: str}."
    ),
)
def watch(
    source: str,
    detail: Literal["transcript", "efficient", "balanced", "token-burner"] | None = None,
    start: str | None = None,
    end: str | None = None,
    timestamps: str | None = None,
    max_frames: int | None = None,
    resolution: int = 512,
    fps: float | None = None,
    whisper: Literal["groq", "openai", "local"] | None = None,
    no_whisper: bool = False,
    no_dedup: bool = False,
    segment: bool = False,
    segment_points: str | None = None,
    segment_labels: str | None = None,
    out_dir: str | None = None,
) -> dict:
    # Build the same argparse Namespace watch.main() would build, so we can
    # delegate to watch_mod.run() without going through subprocess + parsing.
    ns = argparse.Namespace(
        source=source,
        detail=detail,
        start=start,
        end=end,
        timestamps=timestamps,
        max_frames=max_frames,
        resolution=resolution,
        fps=fps,
        whisper=whisper,
        no_whisper=no_whisper,
        no_dedup=no_dedup,
        segment=segment,
        segment_points=segment_points,
        segment_labels=segment_labels,
        out_dir=out_dir,
    )

    # Default work dir to ~/.cache/watch-mcp/<session_id>/ when out_dir is unset.
    # Home dir is more durable than /tmp (which is per-boot on macOS) and is
    # user-inspectable for manual cleanup. We do NOT auto-clean — the host may
    # re-read frame URIs after the tool call returns.
    sid = _make_session_id()
    if ns.out_dir is None:
        ns.out_dir = str(Path.home() / ".cache" / "watch-mcp" / sid)

    try:
        result = watch_mod.run(ns)
    except SystemExit as exc:
        # watch.run() raises SystemExit on invalid args / missing files /
        # network failures. Surface as a structured MCP error result instead
        # of letting the exception tear down the server.
        msg = str(exc) or "watch pipeline aborted"
        raise ToolError(f"watch failed: {msg}") from None
    except Exception as exc:
        raise ToolError(f"watch failed: {exc}") from None

    work = Path(ns.out_dir)
    frame_paths = [Path(f["path"]) for f in result.frames]
    mask_paths = [Path(m["path"]) for m in result.masks]
    SESSIONS[sid] = {
        "work_dir": work,
        "frame_paths": frame_paths,
        "mask_paths": mask_paths,
    }

    # URI scheme: watch-frame://<sid>/frames/<file> for video frames,
    # watch-frame://<sid>/masks/<file> for SAM 2 segmentation masks.
    # Subdir is explicit so the resource template can resolve without knowing
    # whether the file lives in frames/ or masks/.
    return {
        "report": result.report,
        "session_id": sid,
        "work_dir": str(work),
        "frame_uris": [f"watch-frame://{sid}/frames/{p.name}" for p in frame_paths],
        "mask_uris": [f"watch-frame://{sid}/masks/{p.name}" for p in mask_paths],
        "frame_count": len(frame_paths),
        "transcript_source": result.transcript_source or "none",
    }


@mcp.resource(
    "watch-frame://{session_id}/frames/{filename}",
    name="read_frame",
    mime_type="image/jpeg",
    description=(
        "Read a video frame (JPEG) extracted by the `watch` tool. "
        "{session_id} comes from the tool call's `session_id` field. "
        "Path traversal in {filename} is rejected."
    ),
)
def read_frame(
    session_id: str,
    filename: str,
) -> Annotated[bytes, Field(description="Raw JPEG bytes for the requested frame.")]:
    """Read a video frame (JPEG) by URI.

    The {session_id} is the value returned by the `watch` tool. Hosts
    should not guess session_ids — they come from tool calls only.

    Path-traversal defence: the resolved file path is asserted to be a
    descendant of the session's frames/ subdirectory before bytes are
    returned.

    NOTE: return type is wrapped in `Annotated[bytes, Field(...)]`
    because FastMCP's `@mcp.resource` decorator introspects the function
    signature through pydantic.create_model, and bare `bytes` raises
    `PydanticUserError` under pydantic 2.10+. See Phase 1.5 in
    `docs/todo.md`.
    """
    return _read_artifact(session_id, filename, subdir="frames")


@mcp.resource(
    "watch-frame://{session_id}/masks/{filename}",
    name="read_mask",
    mime_type="image/png",
    description=(
        "Read a SAM 2 segmentation mask (PNG) produced when `watch` is "
        "called with --segment. Same URI scheme and session_id lifecycle "
        "as the frames resource."
    ),
)
def read_mask(
    session_id: str,
    filename: str,
) -> Annotated[bytes, Field(description="Raw PNG bytes for the requested segmentation mask.")]:
    """Read a SAM 2 segmentation mask (PNG) by URI.

    See `read_frame` for the `Annotated[bytes, Field(...)]` rationale
    (Phase 1.5).
    """
    return _read_artifact(session_id, filename, subdir="masks")


def _read_artifact(session_id: str, filename: str, *, subdir: str) -> bytes:
    info = SESSIONS.get(session_id)
    if info is None:
        raise ValueError(f"unknown session_id: {session_id}")
    work: Path = info["work_dir"]
    # Reject filenames that contain path separators or traversal tokens before
    # touching the filesystem. filenames from the registry are bare basenames
    # ("frame_NNNN.jpg", "mask_NNNN.png"), but a hostile URI could include
    # "../". Belt-and-braces.
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise ValueError(f"invalid filename in URI: {filename!r}")
    target = (work / subdir / filename).resolve()
    # Defence-in-depth: target must stay under work/subdir. This catches
    # symlinks that escape, even when basename looks innocuous.
    if not target.is_relative_to((work / subdir).resolve()):
        raise ValueError(f"path traversal blocked: {target}")
    if not target.is_file():
        raise ValueError(f"{subdir.rstrip('s')} not found: {target}")
    return target.read_bytes()


if __name__ == "__main__":
    # Default transport is stdio — the right choice for openclaw local agents
    # and Claude Desktop. Hosts that want SSE / streamable HTTP can call
    # mcp.run_sse_async() / mcp.run_streamable_http_async() instead.
    mcp.run()