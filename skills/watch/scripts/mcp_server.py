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
import asyncio
import base64
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import watch as watch_mod  # noqa: E402
from download import is_url  # noqa: E402
from pipeline_runner import runner as pipeline  # noqa: E402
from session_store import (  # noqa: E402
    SessionRecord,
    SessionStatus,
    delete as ss_delete,
    get as ss_get,
    hash_source_to_video_id,
    list_for_user as ss_list_for_user,
    upsert as ss_upsert,
)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp import Context  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

mcp = FastMCP("claude-video")

# The base directory under which all MCP-managed work dirs live. Anything
# outside this tree is rejected from `out_dir` unless the caller explicitly
# opts in with `allow_arbitrary_out=True`. This is the same defence the
# `read_frame` / `read_mask` resources use on filename (Phase 1.3).
MCP_WORK_ROOT = Path.home() / ".cache" / "watch-mcp"

# Per-call session id (12 hex chars, random). Distinct from `video_id`,
# which is the persistent cache key — same video, multiple calls = same
# video_id, different session_ids per call. Kept so the MCP resource
# URI scheme (`watch-frame://<sid>/...`) stays stable for a given call
# even when reuse kicks in for the second.
SESSIONS: dict[str, dict] = {}


def _reset_sessions_for_tests() -> None:
    """Clear the in-memory per-call registry. Test-only helper. Does NOT
    touch the persistent session_store — call `session_store._reset_for_tests`
    separately if a test wants both reset."""
    SESSIONS.clear()


def _validate_source(source: str) -> None:
    """Reject obviously-malformed `source` strings before they reach
    watch_mod.run() (which would SystemExit on bad input).

    A `source` is either an http(s) URL (validated by `download.is_url`)
    or a local filesystem path (must resolve to an existing file).

    Rejection cases:
    - empty string
    - starts with `-` (would be parsed as an argv flag if anyone shells
      out through us — defense in depth)
    - contains NUL byte or other control characters
    - local path that doesn't exist or isn't a regular file
    - local path whose extension isn't a known video type (warning only,
      not rejected — watch.py already prints a similar warning and proceeds)
    """
    if not isinstance(source, str) or not source:
        raise ToolError("source must be a non-empty string")
    if any(ord(c) < 0x20 for c in source):
        raise ToolError("source contains control characters")
    if source.startswith("-"):
        raise ToolError("source must not start with '-' (would be parsed as a flag)")
    if is_url(source):
        return  # URL form, defer to yt-dlp for full validation
    # Local path. Resolve symlinks/relative bits so traversal attempts
    # surface as "file not found" rather than silently targeting elsewhere.
    p = Path(source).expanduser()
    if not p.exists():
        raise ToolError(f"source file not found: {p}")
    if not p.is_file():
        raise ToolError(f"source is not a regular file: {p}")


def _validate_out_dir(out_dir: str | None, *, allow_arbitrary: bool) -> str | None:
    """Validate the caller's `out_dir` choice.

    Default behaviour (allow_arbitrary=False):
      - If out_dir is None, return None (caller lets the server pick
        ~/.cache/watch-mcp/<sid>/).
      - If out_dir is set, the resolved path MUST live under
        ~/.cache/watch-mcp/. This blocks a hostile host from pointing
        work dirs at /etc, ~/.ssh, the user's Documents, etc.

    Opt-in escape hatch (allow_arbitrary=True):
      - Useful for tests that need to write into tmp_path, or for
        operator workflows that intentionally want a project-local dir.
      - We still resolve and create the dir so a typo'd path surfaces
        immediately rather than as a confusing later write error.

    Returns the canonicalized out_dir string (expanded, resolved)."""
    if out_dir is None:
        return None
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise ToolError("out_dir must be a non-empty string when provided")
    if any(ord(c) < 0x20 for c in out_dir):
        raise ToolError("out_dir contains control characters")
    p = Path(out_dir).expanduser()
    if not p.is_absolute():
        raise ToolError(
            f"out_dir must be an absolute path, got {out_dir!r} "
            f"(resolved relative paths are rejected to avoid ambiguity)"
        )
    if not allow_arbitrary:
        try:
            p.resolve().relative_to(MCP_WORK_ROOT.resolve())
        except ValueError:
            raise ToolError(
                f"out_dir must live under {MCP_WORK_ROOT} (got {p}). "
                f"Pass allow_arbitrary_out=True to override — note this "
                f"trusts the caller with arbitrary filesystem writes."
            ) from None
    # Create the dir so a typo'd path fails fast at MCP tool-call time
    # rather than mid-pipeline when watch_mod tries to write a frame.
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ToolError(f"cannot create out_dir {p}: {e}") from None
    return str(p)


def _make_session_id() -> str:
    """12-hex-char session id — short enough for URIs, long enough for uniqueness."""
    return uuid.uuid4().hex[:12]


def _cache_hits_request(record: SessionRecord, *, segment: bool) -> bool:
    """Decide whether an existing session record already satisfies the
    caller's request, or whether we must re-run the pipeline.

    MVP rule: cache misses when the caller asks for something the cached
    run didn't produce. The only such flag today is `segment` — other
    flags (detail, fps, timestamps, ...) just change WHICH frames/masks
    we extract, and the cached frames are still valid as long as the
    underlying source hasn't changed.

    A more complete Phase 2.x would diff the full flag set. For v1 this
    keeps the logic tight and testable.
    """
    if record.status != "done":
        return False  # cached run was incomplete; force re-run
    if segment and not record.masks:
        return False  # caller wants masks, cached run didn't produce them
    return True


@mcp.tool(
    name="watch",
    description=(
        "Watch a video (URL or local path). Downloads with yt-dlp, extracts "
        "auto-scaled frames with ffmpeg, pulls the transcript from captions "
        "(or Whisper fallback), and returns a markdown report with absolute "
        "frame paths. Each frame is also readable as an MCP resource "
        "(watch-frame://<session_id>/frames/<filename>, image/jpeg).\n\n"
        "Use --detail to trade fidelity for speed: 'transcript' skips frames "
        "(downloads video only if no captions), 'efficient' is fast keyframes "
        "(cap 50), 'balanced' is scene-aware (cap 100, default), 'token-burner' "
        "is uncapped.\n\n"
        "Use --start/--end ('SS', 'MM:SS', or 'HH:MM:SS') to focus on a time "
        "range — denser frame budget inside the window. Use --timestamps for "
        "transcript-cue frames at specific moments ('look here', 'as you can see').\n\n"
        "**Phase 2.1 reuse**: pass `video_id` to cache the result and reuse "
        "it on subsequent calls. If omitted, video_id is derived as "
        "sha256(source)[:12]. Pass `restart=True` to force re-run even when "
        "cache hits.\n\n"
        "**Phase 2.1 placeholder auth**: pass `user_openid` / `user_unionid` "
        "to tag the session for cross-user isolation. Stored but NOT verified "
        "until Phase 2.8 OAuth flow lands.\n\n"
        "Returns: {report: markdown, session_id: str, work_dir: str, "
        "frame_uris: list[str], frame_count: int, transcript_source: str, "
        "video_id: str, reused: bool}."
    ),
    structured_output=False,  # Phase 1.5/2.1: avoid FastMCP's bare-dict output-model crash on mcp>=1.20
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
    allow_arbitrary_out: bool = False,
    video_id: str | None = None,
    restart: bool = False,
    user_openid: str | None = None,
    user_unionid: str | None = None,
    auth_source: Literal["wechat_mp", "wechat_op", "none"] | None = None,
) -> dict[str, Any]:
    # Validate `source` and `out_dir` BEFORE building the namespace and
    # calling watch_mod.run(). This turns bad-input failures into clean
    # MCP ToolError results rather than bare SystemExit that would tear
    # down the stdio connection. See Phase 1.3 in docs/todo.md.
    _validate_source(source)
    validated_out_dir = _validate_out_dir(out_dir, allow_arbitrary=allow_arbitrary_out)

    # Resolve video_id. Default = sha256(source)[:12]. We compute it
    # BEFORE running the pipeline so cache lookup is stable for the
    # caller's intent, not for whatever random sid we'd assign.
    effective_video_id = video_id or hash_source_to_video_id(source)

    # ─── Cache hit fast path (Phase 2.1 reuse) ────────────────────────
    # If the same video_id was processed before, AND the new request
    # is satisfied by the cached run, AND caller didn't force restart,
    # return the cached record without re-running watch.
    existing = ss_get(effective_video_id) if not restart else None
    if existing is not None and _cache_hits_request(existing, segment=segment):
        sid = _make_session_id()
        SESSIONS[sid] = {
            "work_dir": Path(existing.work_dir),
            "frame_paths": [Path(f["path"]) for f in existing.frames],
            "mask_paths": [Path(m["path"]) for m in existing.masks],
        }
        return {
            "report": existing.transcript_text or "",
            "session_id": sid,
            "work_dir": existing.work_dir,
            "frame_uris": [
                f"watch-frame://{sid}/frames/{Path(f['path']).name}"
                for f in existing.frames
            ],
            "mask_uris": [
                f"watch-frame://{sid}/masks/{Path(m['path']).name}"
                for m in existing.masks
            ],
            "frame_count": len(existing.frames),
            "transcript_source": existing.transcript_source or "none",
            "video_id": existing.video_id,
            "reused": True,
        }

    # ─── Cache miss: run the pipeline ─────────────────────────────────
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
        out_dir=validated_out_dir,
    )

    # Default work dir to ~/.cache/watch-mcp/<video_id>/ when out_dir is unset.
    # We key by video_id (not session_id) so two calls with the same
    # video_id naturally land in the same dir for the reuse path.
    sid = _make_session_id()
    if ns.out_dir is None:
        ns.out_dir = str(MCP_WORK_ROOT / effective_video_id)

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

    # ─── Persist the record (Phase 2.1) ──────────────────────────────
    # We persist AFTER the pipeline succeeds, so a failed run never
    # writes a half-correct record. The record is the cache; it must
    # only ever reflect a completed run.
    record = SessionRecord(
        video_id=effective_video_id,
        work_dir=str(work),
        source=source,
        status="done",
        frames=result.frames,
        masks=result.masks,
        transcript_source=result.transcript_source,
        transcript_text=result.report,
        user_openid=user_openid,
        user_unionid=user_unionid,
        auth_source=auth_source or "none",
    )
    try:
        ss_upsert(record)
    except Exception as exc:
        # Persistence is best-effort — don't fail the watch call because
        # the disk is full or perms changed. Log so the operator sees it.
        import logging
        logging.getLogger(__name__).warning(
            "session_store.upsert failed for video_id=%s: %s", effective_video_id, exc
        )

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
        "video_id": effective_video_id,
        "reused": False,
    }


# ─── Phase 2.1: cross-user session management tools ───────────────────────


def _check_user_access(record: SessionRecord, caller_openid: str | None) -> None:
    """Raise ToolError if `caller_openid` is forbidden from touching `record`.

    Phase 2.1 placeholder rules (Phase 2.8 OAuth will tighten):
    - record.user_openid is None: anyone can touch it (orphan record from
      pre-OAuth calls, OR caller didn't pass openid yet).
    - record.user_openid is set: only callers with matching openid OR
      callers without an openid (read-only fallback — see Phase 2.1c).
      For v1 we allow openid-less callers to DELETE orphans but not to
      read non-orphans. (TODO Phase 2.8: tighten when OAuth is wired.)
    """
    if record.user_openid is None:
        return
    if caller_openid is None:
        # Orphan records are still readable; tagged records require tag.
        # For deletion we treat None-caller as "anonymous" and allow it —
        # matches Phase 2.8 fallback where users can clean up untagged
        # sessions without logging in. If we tightened to "always reject",
        # an OAuth-free cleanup path would need separate tooling.
        return
    if record.user_openid != caller_openid:
        raise ToolError(
            f"forbidden: session belongs to a different user (caller "
            f"openid={caller_openid!r}, record openid={record.user_openid!r})"
        )


@mcp.tool(
    name="list_sessions",
    description=(
        "List sessions visible to the caller. Returns newest-updated first.\n\n"
        "**Phase 2.1**: when `user_openid` is provided, returns sessions "
        "tagged with that openid PLUS sessions with no openid (orphans). "
        "When `user_openid` is omitted, returns ONLY orphan sessions — "
        "the anonymous view.\n\n"
        "Phase 2.8 OAuth will replace this placeholder semantics with "
        "session-cookie-driven auth. Returns: list[{video_id, work_dir, "
        "source, status, frame_count, user_openid, updated_at}]."
    ),
    structured_output=False,
)
def list_sessions(user_openid: str | None = None) -> dict[str, Any]:
    records = ss_list_for_user(user_openid)
    return {
        "sessions": [
            {
                "video_id": r.video_id,
                "work_dir": r.work_dir,
                "source": r.source,
                "status": r.status,
                "frame_count": len(r.frames),
                "mask_count": len(r.masks),
                "transcript_source": r.transcript_source,
                "user_openid": r.user_openid,
                "auth_source": r.auth_source,
                "updated_at": r.updated_at,
            }
            for r in records
        ],
        "count": len(records),
    }


@mcp.tool(
    name="delete_session",
    description=(
        "Delete a session record from the registry. **Does NOT remove the "
        "work directory on disk** — caller cleans up files separately.\n\n"
        "**Phase 2.1 access control**: if the record has a `user_openid` and "
        "the caller passes a different `user_openid`, returns forbidden. "
        "Orphan records (no openid) can be deleted by anyone.\n\n"
        "Returns: {deleted: bool, video_id: str}."
    ),
    structured_output=False,
)
def delete_session(video_id: str, user_openid: str | None = None) -> dict[str, Any]:
    record = ss_get(video_id)
    if record is None:
        return {"deleted": False, "video_id": video_id, "reason": "not_found"}
    _check_user_access(record, caller_openid=user_openid)
    deleted = ss_delete(video_id)
    return {"deleted": deleted, "video_id": video_id}


# ─── Phase 2.2 — split pipeline into start/status/results/cancel ──────────
#
# The single `watch` tool above stays as a sync convenience wrapper. The
# 4 tools below are the granular interface for hosts that want
# non-blocking progress reporting (e.g. UI showing "downloading... 30%",
# BFF forwarding SSE events, OpenMontage waiting on a long job).


@mcp.tool(
    name="start_watch",
    description=(
        "Start a /watch pipeline in the background. Returns immediately "
        "with `{video_id, status: 'running', stage: 'download'}`. Poll "
        "`get_status` to follow progress, then call `get_results` once "
        "status is 'done'.\n\n"
        "video_id is derived as sha256(source)[:12] unless you pass one "
        "explicitly. If a record already exists for this video_id AND "
        "the cached run satisfies the request, returns `{reused: True, "
        "status: 'done', ...}` without spawning a new job.\n\n"
        "Args mirror the single-call `watch` tool plus `video_id`, "
        "`restart`, and the WeChat placeholders. Returns: "
        "{video_id, status, stage, progress, session_id?, reused?, "
        "projected_eta_seconds?}."
    ),
    structured_output=False,
)
def start_watch(
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
    allow_arbitrary_out: bool = False,
    video_id: str | None = None,
    restart: bool = False,
    user_openid: str | None = None,
    user_unionid: str | None = None,
    auth_source: Literal["wechat_mp", "wechat_op", "none"] | None = None,
    timeout_seconds: float | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    _validate_source(source)
    validated_out_dir = _validate_out_dir(out_dir, allow_arbitrary=allow_arbitrary_out)
    effective_video_id = video_id or hash_source_to_video_id(source)

    # Cache hit fast path (same logic as `watch`, but no sync pipeline run).
    existing = ss_get(effective_video_id) if not restart else None
    if existing is not None and _cache_hits_request(existing, segment=segment):
        return {
            "video_id": existing.video_id,
            "status": existing.status,
            "stage": existing.stage or "done",
            "progress": existing.progress if existing.progress is not None else 100.0,
            "reused": True,
        }

    # Cache miss: spawn the background thread. We need a tiny record
    # pre-created so get_status works immediately.
    sid = _make_session_id()
    work_dir_str = validated_out_dir or str(MCP_WORK_ROOT / effective_video_id)
    work = Path(work_dir_str)
    work.mkdir(parents=True, exist_ok=True)

    pre_record = SessionRecord(
        video_id=effective_video_id,
        work_dir=work_dir_str,
        source=source,
        status="running",
        stage="download",
        progress=0.0,
        user_openid=user_openid,
        user_unionid=user_unionid,
        auth_source=auth_source or "none",
    )
    try:
        ss_upsert(pre_record)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("session_store.upsert pre-record failed: %s", exc)

    # Build the same Namespace watch.main() would build.
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
        out_dir=work_dir_str,
    )

    def _save_result(vid: str, result) -> None:
        """Persist the final SessionRecord once watch_mod.run returns."""
        rec = ss_get(vid) or SessionRecord(
            video_id=vid,
            work_dir=work_dir_str,
            source=source,
        )
        rec.status = "done"
        rec.stage = "done"
        rec.progress = 100.0
        rec.frames = result.frames
        rec.masks = result.masks
        rec.transcript_source = result.transcript_source
        rec.transcript_text = result.report
        rec.updated_at = time.time()
        try:
            ss_upsert(rec)
        except Exception as exc:  # noqa: BLE001
            log.warning("session_store.upsert final record failed for %s: %s", vid, exc)

    # Build a progress_hook that bridges the background thread back to
    # the MCP client's notifications/progress stream. ctx.report_progress
    # is async and reads request_context.meta.progressToken — the token
    # the client sent with the original tools/call. We capture the main
    # event loop here (start_watch runs in it) and use
    # run_coroutine_threadsafe to schedule the notification.
    progress_hook = None
    if ctx is not None:
        try:
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            main_loop = None
        if main_loop is not None:
            def _progress_hook(vid: str, stage: str, progress, message: str) -> None:
                if ctx is None:
                    return
                # report_progress is a coroutine; schedule it on the main loop.
                total = 100.0
                prog_value = progress if progress is not None else 0.0
                try:
                    asyncio.run_coroutine_threadsafe(
                        ctx.report_progress(prog_value, total=total, message=f"{stage}: {message}"),
                        main_loop,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("report_progress scheduling failed for %s: %s", vid, exc)

            progress_hook = _progress_hook

    try:
        pipeline.start(
            effective_video_id,
            work_dir=work_dir_str,
            run_fn=watch_mod.run,
            run_args=ns,
            session_save_fn=_save_result,
            progress_hook=progress_hook,
            timeout_seconds=timeout_seconds,
        )
    except RuntimeError as exc:
        # Another job is already running for this video_id. Surface as
        # ToolError so the host knows to retry.
        raise ToolError(str(exc)) from None

    return {
        "video_id": effective_video_id,
        "session_id": sid,
        "status": "running",
        "stage": "download",
        "progress": 0.0,
        "reused": False,
        "work_dir": work_dir_str,
    }


@mcp.tool(
    name="get_status",
    description=(
        "Query the current status of a watch job by video_id. Returns "
        "{video_id, status, stage, progress, error?} where:\n"
        "- status: 'running' | 'done' | 'error' | 'cancelled'\n"
        "- stage: 'download' | 'frames' | 'transcript' | 'segment' | 'done'\n"
        "- progress: float 0-100\n"
        "- error: error message if status='error' or 'cancelled'\n\n"
        "Status is read from session_store, so it survives server "
        "restarts (terminal states persist; in-flight jobs are lost on "
        "restart and surface as 'error: server_restart')."
    ),
    structured_output=False,
)
def get_status(video_id: str) -> dict[str, Any]:
    record = ss_get(video_id)
    if record is None:
        return {
            "video_id": video_id,
            "status": "not_found",
            "stage": None,
            "progress": None,
            "error": "no record for this video_id",
        }
    return {
        "video_id": record.video_id,
        "status": record.status,
        "stage": record.stage,
        "progress": record.progress,
        "error": record.error,
    }


@mcp.tool(
    name="get_results",
    description=(
        "Fetch the full results of a completed watch job. Returns the "
        "same shape as the sync `watch` tool: {report, session_id, "
        "work_dir, frame_uris, mask_uris, frame_count, transcript_source, "
        "video_id, reused: False}.\n\n"
        "If the job is still running, returns {status: 'running', "
        "stage, progress} so the caller can decide to poll again. If "
        "the job errored or was cancelled, returns the same shape with "
        "status set accordingly and no frames."
    ),
    structured_output=False,
)
def get_results(video_id: str) -> dict[str, Any]:
    record = ss_get(video_id)
    if record is None:
        return {"status": "not_found", "video_id": video_id}
    if record.status != "done":
        return {
            "status": record.status,
            "stage": record.stage,
            "progress": record.progress,
            "error": record.error,
            "video_id": video_id,
        }
    # Done — return the full result shape. We mint a fresh session_id
    # per call so the caller can use the URI scheme immediately.
    sid = _make_session_id()
    work = Path(record.work_dir)
    frame_paths = [Path(f["path"]) for f in record.frames]
    mask_paths = [Path(m["path"]) for m in record.masks]
    SESSIONS[sid] = {
        "work_dir": work,
        "frame_paths": frame_paths,
        "mask_paths": mask_paths,
    }
    return {
        "status": "done",
        "report": record.transcript_text or "",
        "session_id": sid,
        "work_dir": record.work_dir,
        "frame_uris": [f"watch-frame://{sid}/frames/{p.name}" for p in frame_paths],
        "mask_uris": [f"watch-frame://{sid}/masks/{p.name}" for p in mask_paths],
        "frame_count": len(frame_paths),
        "transcript_source": record.transcript_source or "none",
        "video_id": record.video_id,
        "reused": False,
    }


@mcp.tool(
    name="cancel_watch",
    description=(
        "Cancel a running watch job. The background thread stops at the "
        "next stage boundary (download / frames / transcript / segment); "
        "in-flight ffmpeg / yt-dlp calls are NOT interrupted mid-call. "
        "Returns {cancelled: True} on success, {cancelled: False, "
        "reason: 'not_running'} if no job is active for the video_id."
    ),
    structured_output=False,
)
def cancel_watch(video_id: str) -> dict[str, Any]:
    if not pipeline.cancel(video_id):
        return {"cancelled": False, "video_id": video_id, "reason": "not_running"}
    return {"cancelled": True, "video_id": video_id}


# ─── Phase 2.6 — recompose via OpenMontage_Voicebox ──────────────────────
#
# v2+ hard constraint: NO local Remotion / local final-render ffmpeg.
# recompose forwards the watch session record to OpenMontage's MCP
# server, which has 12 production pipelines + stage orchestration +
# Backlot storyboard. See OpenMontage_Voicebox/docs/claude-video-integration.md
# for the cross-repo contract.


@mcp.tool(
    name="recompose",
    description=(
        "Submit a completed /watch session to OpenMontage_Voicebox for "
        "recomposition (montage, captions, highlight reel, etc.).\n\n"
        "**GPU-free constraint**: this box has no GPU. `recompose` "
        "accepts only OpenMontage pipelines that don't touch GPU "
        "providers (FLUX / Kling / local_diffusion / video diffusion "
        "models are rejected). Default whitelisted pipelines: "
        "`clip-factory`, `documentary-montage`, `podcast-repurpose`, "
        "`localization-dub`, `hybrid`, `screen-demo`.\n\n"
        "Requires the OpenMontage MCP binary at OPENMONTAGE_BIN "
        "(default `/opt/OpenMontage_Voicebox/mcp_server.py`). The "
        "OpenMontage side stores outputs under "
        "`projects/users/<user_openid>/<video_id>/renders/final.mp4`.\n\n"
        "Returns: {project_id, status: 'submitted', pipeline, "
        "render_url?, work_dir}. Raises ToolError on pipeline rejection "
        "or OpenMontage unavailability."
    ),
    structured_output=False,
)
async def recompose(
    video_id: str,
    pipeline: Literal[
        "clip-factory",
        "documentary-montage",
        "podcast-repurpose",
        "localization-dub",
        "hybrid",
        "screen-demo",
    ] = "clip-factory",
    style: str = "clean-professional",
    user_openid: str | None = None,
    user_unionid: str | None = None,
) -> dict[str, Any]:
    record = ss_get(video_id)
    if record is None:
        raise ToolError(
            f"no session record for video_id={video_id!r}. "
            f"Run /watch (or start_watch) first, then recompose."
        )
    if record.status != "done":
        raise ToolError(
            f"video_id={video_id!r} status is {record.status!r}, not 'done'. "
            f"Wait for the pipeline to finish, or call cancel_watch + restart."
        )

    work = Path(record.work_dir)
    frames_dir = work / "frames"
    masks_dir = work / "masks"
    # Find VTT under download/
    vtt_candidates = sorted((work / "download").glob("video*.vtt")) if (work / "download").is_dir() else []
    vtt_path = str(vtt_candidates[0]) if vtt_candidates else None
    # Find source video
    video_candidates = sorted((work / "download").glob("video.*")) if (work / "download").is_dir() else []
    video_path = str(video_candidates[0]) if video_candidates else None

    # Resolve effective user_openid: prefer caller-provided, fall back
    # to the record's stored openid (covers the case where the same
    # operator who created the session now wants to recompose it).
    effective_openid = user_openid or record.user_openid

    # Lazy import to avoid loading mcp SDK on every server start.
    import openmontage_client

    try:
        result = await openmontage_client.submit_compose(
            video_id=video_id,
            user_openid=effective_openid,
            work_dir=record.work_dir,
            frames_dir=str(frames_dir) if frames_dir.is_dir() else "",
            masks_dir=str(masks_dir) if masks_dir.is_dir() else None,
            vtt_path=vtt_path,
            video_path=video_path,
            pipeline=pipeline,
            style=style,
            extra={
                "user_unionid": effective_openid and user_unionid,
                "claude_video_source": "watch_mcp",
            },
        )
    except openmontage_client.PipelineNotAllowedError as exc:
        raise ToolError(str(exc)) from None
    except openmontage_client.OpenMontageUnavailableError as exc:
        raise ToolError(str(exc)) from None
    except openmontage_client.OpenMontageError as exc:
        raise ToolError(f"recompose failed: {exc}") from None

    return {
        "project_id": result.get("project_id", video_id),
        "status": result.get("status", "submitted"),
        "pipeline": pipeline,
        "style": style,
        "video_id": video_id,
        "render_url": result.get("render_url"),
        "work_dir": record.work_dir,
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