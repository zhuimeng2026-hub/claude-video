#!/usr/bin/env python3
"""Standalone SSE progress daemon for /watch.

Phase 2.3 SSE path. Serves Server-Sent Events for browser clients that
can't speak stdio MCP directly. The Phase 2.7 BFF will proxy browser
traffic to this daemon, but it can also be exposed directly to LAN
clients (Node.js dashboards, Python notebooks, etc.).

Architecture
------------

  Browser / Web client          Phase 2.7 BFF              THIS daemon
        |                          |                          |
        |---GET /api/watch/{vid}/events--->|                  |
        |                          |--proxy-->| GET /progress/{vid} |
        |                          |          | (poll session_store) |
        |<--SSE data: {json}----------|<--------|<--data: {json}-----|
        |                          |          |

Single FastAPI app, one asyncio task per SSE connection. Reads from
session_store (same JSON file the MCP server writes), so this process
doesn't share memory with the MCP server. They communicate purely
through the file system — Phase 2.x can swap to a real queue if we
ever go multi-host.

Endpoints
---------

GET /healthz
    Liveness probe. Returns {"status": "ok", "session_root": "..."}.

GET /progress/{video_id}?token=...
    SSE stream. Emits events as the SessionRecord for video_id
    updates. Closes when status reaches a terminal state (done /
    error / cancelled) OR after ?timeout=N seconds (default 600).

GET /progress/{video_id}  (missing token)
    401 with {"error": "missing_token"}.

GET /progress/{video_id}  (server not configured)
    503 with {"error": "watch_sse_token not configured"}.

Auth
----

WATCH_SSE_TOKEN env var. If unset, the endpoint refuses all clients
(503) — better than a silently-open relay. Phase 2.8 OAuth replaces
this with session-cookie auth; the env var is the placeholder that
lets local development work today.

Run
---

    WATCH_SSE_TOKEN=secret WATCH_SSE_PORT=8911 \
        python3 skills/watch/scripts/sse_progress.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import session_store  # noqa: E402

log = logging.getLogger("sse_progress")


def _default_port() -> int:
    return int(os.environ.get("WATCH_SSE_PORT", "8911"))


def _default_token() -> str | None:
    return os.environ.get("WATCH_SSE_TOKEN") or None


def create_app() -> FastAPI:
    app = FastAPI(title="watch-sse-progress", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "session_root": str(session_store.STORE_ROOT),
            "auth_configured": _default_token() is not None,
        })

    @app.get("/progress/{video_id}")
    async def progress(
        video_id: str,
        request: Request,
        token: str | None = Query(default=None),
        timeout: float = Query(default=600.0, ge=1.0, le=3600.0),
    ) -> EventSourceResponse:
        expected_token = _default_token()
        if expected_token is None:
            # Refuse all clients when not configured (matches Phase 2.7
            # web-multiuser-auth convention).
            raise HTTPException(status_code=503, detail={
                "error": "watch_sse_token_not_configured",
                "message": "server WATCH_SSE_TOKEN env var is unset; "
                           "set it to enable progress streaming",
            })
        # Accept token via query string (EventSource API can't set headers)
        # or via Authorization: Bearer header (for non-browser clients).
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        presented = token or bearer
        if presented != expected_token:
            raise HTTPException(status_code=401, detail={"error": "invalid_token"})

        async def event_gen() -> AsyncIterator[dict]:
            last_stage: str | None = None
            last_progress: float | None = None
            last_status: str | None = None
            deadline = asyncio.get_event_loop().time() + timeout
            poll_interval = 0.5

            while True:
                if asyncio.get_event_loop().time() >= deadline:
                    # Timeout: send a final "timeout" event then close
                    yield {
                        "event": "timeout",
                        "data": json.dumps({
                            "video_id": video_id,
                            "reason": "timeout",
                        }),
                    }
                    return

                if await request.is_disconnected():
                    log.debug("SSE client disconnected video_id=%s", video_id)
                    return

                record = session_store.get(video_id)
                if record is None:
                    # No record yet (job hasn't even started in session_store).
                    # Emit a synthetic "pending" event so the client knows
                    # the connection is live and we are waiting.
                    if last_status is not None:
                        # already sent pending once — skip until something changes
                        await asyncio.sleep(poll_interval)
                        continue
                    last_status = "pending"
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "video_id": video_id,
                            "stage": None,
                            "progress": None,
                            "status": "pending",
                            "ts": asyncio.get_event_loop().time(),
                        }),
                    }
                    await asyncio.sleep(poll_interval)
                    continue

                # Record exists. Emit if any of the fields changed.
                changed = (
                    record.stage != last_stage
                    or record.progress != last_progress
                    or record.status != last_status
                )
                if changed:
                    last_stage = record.stage
                    last_progress = record.progress
                    last_status = record.status
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "video_id": record.video_id,
                            "stage": record.stage,
                            "progress": record.progress,
                            "status": record.status,
                            "error": record.error,
                            "ts": record.updated_at,
                        }),
                    }

                # Terminal state: emit one final event tagged 'final'
                # so the client can close cleanly, then close the stream.
                if record.status in ("done", "error", "cancelled"):
                    yield {
                        "event": "final",
                        "data": json.dumps({
                            "video_id": record.video_id,
                            "status": record.status,
                            "stage": record.stage,
                        }),
                    }
                    return

                await asyncio.sleep(poll_interval)

        return EventSourceResponse(event_gen())

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="watch SSE progress daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_default_port())
    parser.add_argument(
        "--reload", action="store_true",
        help="reload on code changes (dev only; production should disable)",
    )
    args = parser.parse_args()

    if _default_token() is None:
        log.warning(
            "WATCH_SSE_TOKEN is unset — the /progress endpoint will "
            "return 503 to all clients until it's configured."
        )
    else:
        log.info("auth token configured (clients must send ?token=...)")

    log.info("session_store root: %s", session_store.STORE_ROOT)
    log.info("starting uvicorn on %s:%d", args.host, args.port)
    uvicorn.run(
        "sse_progress:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
