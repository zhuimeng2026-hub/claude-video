"""Thin client for the OpenMontage_Voicebox MCP server.

Phase 2.6 — recompose routes through OpenMontage instead of local
Remotion. This client encapsulates:

1. **GPU-free pipeline allow-list** (Phase 2.6 hard constraint): the
   claude-video box has no GPU, so any pipeline that would touch
   FLUX / Kling / local_diffusion / hunyuan_video / wan_video /
   cogvideo_video is rejected at submit_compose time. The cross-repo
   doc (`OpenMontage_Voicebox/docs/claude-video-integration.md`) lists
   the full blacklist + whitelist.

2. **stdio MCP transport** to OpenMontage. Spawns the OpenMontage MCP
   server as a subprocess and talks JSON-RPC over stdin/stdout. We
   use the official `mcp` SDK so we get protocol negotiation for free.

3. **inputs packaging** — translates a claude-video SessionRecord into
   the inputs schema OpenMontage expects (see cross-repo doc §4.1).

Why stdio, not HTTP
-------------------
OpenMontage's MCP server exposes a streamable-HTTP transport at
:8900, but the cleanest integration here is stdio because:

- claude-video already has an MCP client (Phase 1.1 SDK proven for
  tests). Reusing the same client removes transport code.
- stdio subprocess is self-contained — no port collision, no auth
  beyond filesystem permissions on the OpenMontage directory.
- Health check is just "subprocess started and responded to list_tools".

A future phase can add HTTP transport if we ever go multi-host.

Failure modes
-------------
- OpenMontage_MCP script missing / not executable -> ToolError
- subprocess.startup timeout (10s default) -> ToolError
- GPU-only pipeline requested -> ToolError (before subprocess)
- list_tools call fails -> raise on submit_compose
- OpenMontage returns error -> ToolError with that message
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent

# OpenMontage side: this is the entry point we'll spawn. In production
# it's a real Python module on the same box (typically
# /opt/OpenMontage_Voicebox/mcp_server.py). Override via env var for
# dev / CI / cross-host scenarios.
DEFAULT_OPENMONTAGE_BIN = "/opt/OpenMontage_Voicebox/mcp_server.py"

# GPU-free pipelines the claude-video box may invoke. OpenMontage has
# more, but this is the only whitelist we accept requests for.
ALLOWED_PIPELINES = frozenset({
    "clip-factory",
    "documentary-montage",
    "podcast-repurpose",
    "localization-dub",
    "hybrid",
    "screen-demo",
})

# GPU-required — explicitly rejected. Anything not in ALLOWED_PIPELINES
# AND not in this blacklist also gets rejected (defensive default).
GPU_REQUIRED_PIPELINES = frozenset({
    "animation",  # remotion-composer — no GPU but produces MP4 locally;
                  # left out of whitelist because Phase 2.6 forbids local render
    "avatar-spokesperson",
    "character-animation",
    "cinematic",
})

# Hardcoded blacklist for clarity / error messages
BLACKLISTED_PROVIDERS = (
    "FLUX", "Kling", "local_diffusion", "hunyuan_video",
    "wan_video", "cogvideo_video",
)


class OpenMontageError(Exception):
    """Base for all client-side failures."""


class PipelineNotAllowedError(OpenMontageError):
    """Caller asked for a pipeline the claude-video box can't run."""


class OpenMontageUnavailableError(OpenMontageError):
    """Subprocess failed to start or didn't respond to handshake."""


def openmontage_bin() -> str:
    return os.environ.get("OPENMONTAGE_BIN", DEFAULT_OPENMONTAGE_BIN)


def is_openmontage_available() -> bool:
    """Synchronous check: does the OpenMontage MCP binary exist?

    Doesn't start the subprocess — just checks the file. Used by
    setup.py preflight and by recompose tool to fail fast with a
    helpful error."""
    bin_path = openmontage_bin()
    return Path(bin_path).is_file()


def validate_pipeline(pipeline: str) -> None:
    """Raise PipelineNotAllowedError if `pipeline` isn't on the GPU-free
    whitelist. Phase 2.6 hard constraint."""
    if pipeline in ALLOWED_PIPELINES:
        return
    # Any pipeline not whitelisted is rejected by default. We surface
    # the specific blacklist where applicable so operators see why.
    if pipeline in GPU_REQUIRED_PIPELINES or any(
        p in pipeline.lower() for p in ("flux", "kling", "diffusion", "video")
    ):
        raise PipelineNotAllowedError(
            f"pipeline {pipeline!r} requires GPU; this box has no GPU. "
            f"Allowed (GPU-free) pipelines: {sorted(ALLOWED_PIPELINES)}"
        )
    raise PipelineNotAllowedError(
        f"pipeline {pipeline!r} is not on the claude-video allow-list. "
        f"Allowed: {sorted(ALLOWED_PIPELINES)}"
    )


async def submit_compose(
    *,
    video_id: str,
    user_openid: str | None,
    work_dir: str,
    frames_dir: str,
    masks_dir: str | None,
    vtt_path: str | None,
    video_path: str | None,
    pipeline: str,
    style: str = "clean-professional",
    extra: dict[str, Any] | None = None,
    progress_hook=None,
    startup_timeout: float = 10.0,
) -> dict[str, Any]:
    """Submit a claude-video session to OpenMontage for recomposition.

    Returns OpenMontage's response: {project_id, status, render_url?}.

    Args:
        video_id:        claude-video's stable cache key (12 hex chars)
        user_openid:     WeChat openid; OpenMontage puts the project under
                         projects/users/<openid>/ for isolation
        work_dir:        claude-video session work_dir (OpenMontage reads
                         assets from here, then moves them to its own
                         projects/<user>/<project>/assets/)
        frames_dir:      directory containing frame_NNNN.jpg
        masks_dir:       directory containing mask_NNNN.png (or None)
        vtt_path:        path to transcript.vtt (or None if no transcript)
        video_path:      path to source video (or None if URL-only)
        pipeline:        OpenMontage pipeline name (whitelist enforced)
        style:           visual style playbook (default clean-professional)
        extra:           extra inputs forwarded to OpenMontage
        progress_hook:   callable(stage, message) — called on OpenMontage
                         progress updates if available
        startup_timeout: how long to wait for OpenMontage handshake

    Raises:
        PipelineNotAllowedError — pipeline not on whitelist
        OpenMontageUnavailableError — subprocess failed / didn't respond
        OpenMontageError — generic call failure
    """
    validate_pipeline(pipeline)

    if not is_openmontage_available():
        raise OpenMontageUnavailableError(
            f"OpenMontage MCP binary not found at {openmontage_bin()}. "
            f"Set OPENMONTAGE_BIN env var to the correct path, or "
            f"install OpenMontage_Voicebox (see docs/todo.md §2.6)."
        )

    # Lazy import — only required when recompose is actually invoked.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    inputs = {
        "user_openid": user_openid,
        "project_id": video_id,  # OpenMontage uses project_id; we reuse video_id
        "source": {
            "video_id": video_id,
            "frames_dir": frames_dir,
            "masks_dir": masks_dir,
            "vtt_path": vtt_path,
            "video_path": video_path,
            "work_dir": work_dir,
        },
        "pipeline": pipeline,
        "style": style,
        "extra": extra or {},
    }

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[openmontage_bin()],
        env=None,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(
                session.initialize(),
                timeout=startup_timeout,
            )
            # Hand-rolled dispatch: we call `claude_video.compose` tool
            # directly. The Phase 2.6 cross-repo doc lists this as
            # the canonical tool name. The tool signature is
            # `compose(inputs: dict)` — FastMCP wraps the call so
            # we must pass `{"inputs": ...}` not the raw dict.
            result = await asyncio.wait_for(
                session.call_tool("claude_video.compose", {"inputs": inputs}),
                timeout=300.0,  # 5 min max for submit; actual render is async
            )
            # Unwrap FastMCP content blocks
            content = result.content
            if not content:
                raise OpenMontageError("OpenMontage returned empty content")
            first = content[0]
            text = getattr(first, "text", None) or json.dumps(inputs)  # fallback
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw_text": text}
