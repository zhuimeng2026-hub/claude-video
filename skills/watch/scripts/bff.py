#!/usr/bin/env python3
"""FastAPI BFF (Backend-for-Frontend) for browser clients.

Phase 2.7 — replaces the original WebSocket plan (Phase 2.3 todo.md)
because MCP long-connection + CORS don't fit browsers. Same pattern
OpenMontage uses in their Go BFF (`OpenMontage_Voicebox/frameflow/bff/`):
REST + SSE translate JSON-RPC-over-stdio into something the browser
can consume via fetch + EventSource.

Architecture
------------

  Browser                     THIS BFF                       mcp_server.py (subprocess)
     |                            |                                    |
     |--POST /api/watch/start------>|---call_tool("start_watch")------->|
     |<--202 Accepted {video_id}----|<--{status: running}--------------|
     |                            |                                    |
     |--GET /api/watch/{vid}/events>|                                    |
     |   (EventSource, SSE)       |--poll get_status every 0.5s------>|
     |<--data: {progress}----------|<--{stage, progress}--------------|
     |   data: {progress}         |                                    |
     |   data: {final}            |                                    |
     |   (stream closes)          |                                    |
     |                            |                                    |
     |--GET /api/watch/{vid}/frame/frame_0001.jpg-->                    |
     |<--image/jpeg bytes----------|<--read_frame resource-------------|

Why not WebSocket: SSE has automatic reconnect on the EventSource API,
works through proxies/firewalls, and is one-way (server→client) which
is exactly what progress streaming needs.

stdio MCP session persistence
----------------------------
JSON-RPC over stdio is **stateful**: each call gets a response, but
ordering matters and the connection must stay open. So:

  - BFF startup: `lifespan` context spawns `python3 mcp_server.py`,
    opens one `ClientSession`, stores both in `BFFState`.
  - Each request: acquires `BFFState.lock` (async lock), runs the
    tool call, releases. Lock is essential — without it, two
    concurrent requests would interleave their stdin writes and
    mangle the JSON-RPC stream.
  - BFF shutdown: closes session, terminates subprocess.

CORS
----
Default: `http://localhost:*` (dev) + `tauri://` (Tauri WebView).
Override via `WATCH_BFF_CORS_ORIGINS` (comma-separated).

Auth (Phase 2.7 placeholder)
----------------------------
Phase 2.8 OAuth flow will replace this. For now:
  - `Depends(require_user)` reads `Authorization: Bearer <token>` header.
  - If `WATCH_BFF_AUTH_TOKEN` env is set, tokens must match.
  - If env is unset, all requests pass (dev mode).
  - 401 returns `{"error": "not_authenticated"}` so frontend knows
    to redirect to `/auth/wechat` (Phase 2.8).

Run
---
    WATCH_BFF_PORT=8910 python3 skills/watch/scripts/bff.py

Or in-process for tests:
    async with BFFState() as state:
        await state.start()
        ...

Phase 2.8 — WeChat service-account OAuth
----------------------------------------
If WECHAT_MP_* env vars are set, `/auth/wechat/*` routes are mounted
and `require_user` reads the WATCH_SESSION cookie (sqlite-backed via
users_store). If unset, /auth routes return 503 and require_user
falls back to the env-token scheme (or dev-bypass).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import users_store  # noqa: E402
import wechat_oauth  # noqa: E402

log = logging.getLogger("bff")


def _sse_format(event: str, data: dict) -> str:
    """Format one SSE record as a string. Starlette's StreamingResponse
    encodes strings to bytes via .encode(self.charset) — yielding a
    dict crashes with AttributeError. So we pre-format here."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ─── BFFState — the persistent stdio MCP connection ───────────────────────


class BFFState:
    """One process holds one stdio MCP subprocess + ClientSession.

    Lifetimes match the FastAPI app via the lifespan context manager.
    Tests use ASGITransport with lifespan="on" so the state is started
    before requests and torn down after.
    """

    def __init__(self) -> None:
        self._stdio_ctx = None
        self._session = None
        self._subprocess = None
        self.lock = asyncio.Lock()

    async def start(self, mcp_server_path: Path) -> None:
        """Spawn the MCP server subprocess and open a session."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not mcp_server_path.is_file():
            raise RuntimeError(
                f"mcp_server.py not found at {mcp_server_path}; "
                f"set WATCH_MCP_BIN env var"
            )

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(mcp_server_path)],
            env=None,  # inherit
        )

        # stdio_client returns an async context manager that yields
        # (read_stream, write_stream). We need to keep it alive for the
        # lifetime of the session. Store the cm on self.
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        log.info("BFF connected to MCP server at %s", mcp_server_path)

    async def stop(self) -> None:
        """Tear down session + subprocess."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("session close failed: %s", exc)
            self._session = None
        if self._stdio_ctx is not None:
            try:
                await self._stdio_ctx.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("stdio ctx close failed: %s", exc)
            self._stdio_ctx = None

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool, serialize via lock.

        FastMCP returns a list of content blocks; first TextContent has
        the JSON-serialized result. We unwrap here so endpoints can
        just return the parsed dict.
        """
        async with self.lock:
            if self._session is None:
                raise RuntimeError("BFFState not started")
            result = await self._session.call_tool(name, arguments)
            content = result.content
            if not content:
                return {}
            text = getattr(content[0], "text", None)
            if text is None:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}


# ─── Auth (Phase 2.7 placeholder, replaced by Phase 2.8 OAuth) ──────────


def _configured_token() -> str | None:
    return os.environ.get("WATCH_BFF_AUTH_TOKEN") or None


async def require_user(
    authorization: Optional[str] = Header(default=None),
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    """Phase 2.7 placeholder + Phase 2.8 OAuth.

    Three modes, picked at startup based on env config:

    1. **WeChat cookie (browser, prod)**: when WECHAT_MP_APP_ID env is
       set, callers MUST send a valid WATCH_SESSION cookie. Missing /
       expired cookie → 401, never dev-bypass.

    2. **Bearer token (dev / inter-service)**: when WATCH_BFF_AUTH_TOKEN
       env is set but WeChat is NOT, callers must send
       `Authorization: Bearer <token>`. Missing → 401.

    3. **Dev bypass**: when NEITHER is configured (used by tests).
    """
    # 1) WeChat cookie path — when configured, this is the only auth
    if wechat_oauth.is_configured() and request is not None:
        sid = request.cookies.get("WATCH_SESSION")
        if not sid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "not_authenticated",
                        "message": "missing WATCH_SESSION cookie; redirect to /auth/wechat/login"},
            )
        sess = users_store.get_session(sid)
        if sess is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid",
                        "message": "WATCH_SESSION expired or unknown; redirect to /auth/wechat/login"},
            )
        # Sliding window — bump expiry on each authenticated request
        users_store.extend_session(sid)
        user = users_store.get_user(sess.openid)
        return {
            "auth": "wechat",
            "user_openid": sess.openid,
            "user_unionid": user.unionid if user else None,
            "session_id": sid,
        }

    # 2) Bearer token path
    expected = _configured_token()
    if expected is not None:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "not_authenticated",
                        "message": "missing or malformed Authorization header"},
            )
        presented = authorization[7:].strip()
        if presented != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_token"},
            )
        return {"auth": "bearer", "user": presented[:8] + "..."}

    # 3) Dev bypass — only when NO auth scheme is configured
    return {"auth": "dev-bypass"}


# ─── Pydantic request/response models ────────────────────────────────────


class StartRequest(BaseModel):
    source: str
    video_id: str | None = None
    detail: str | None = None
    start: str | None = None
    end: str | None = None
    timestamps: str | None = None
    max_frames: int | None = None
    resolution: int | None = 512
    fps: float | None = None
    whisper: str | None = None
    no_whisper: bool = False
    no_dedup: bool = False
    segment: bool = False
    segment_points: str | None = None
    segment_labels: str | None = None
    out_dir: str | None = None
    allow_arbitrary_out: bool = False
    restart: bool = False
    user_openid: str | None = None
    user_unionid: str | None = None
    auth_source: str | None = None
    timeout_seconds: float | None = None


class RecomposeRequest(BaseModel):
    video_id: str
    pipeline: str = "clip-factory"
    style: str = "clean-professional"
    user_openid: str | None = None
    user_unionid: str | None = None


# ─── FastAPI app ──────────────────────────────────────────────────────────


def create_app(state: BFFState | None = None) -> FastAPI:
    """Build the FastAPI app.

    Tests pass `state=BFFState()` to control lifecycle directly; the
    real BFF uses a module-level singleton so uvicorn picks it up via
    the lifespan context.
    """
    if state is None:
        state = _module_state

    mcp_bin_env = os.environ.get("WATCH_MCP_BIN", str(SCRIPT_DIR / "mcp_server.py"))
    mcp_bin = Path(mcp_bin_env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.start(mcp_bin)
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(
        title="claude-video-bff",
        version="0.1.0",
        description="REST + SSE proxy for /watch MCP server (browser clients)",
        lifespan=lifespan,
    )

    # CORS: localhost (dev) + tauri:// (Tauri WebView) by default.
    origins_env = os.environ.get("WATCH_BFF_CORS_ORIGINS")
    if origins_env:
        allow_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
    else:
        allow_origins = [
            "http://localhost:*",
            "http://127.0.0.1:*",
            "tauri://localhost",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "mcp_connected": state._session is not None,
            "auth_configured": _configured_token() is not None,
            "wechat_configured": wechat_oauth.is_configured(),
        }

    # ── /auth/wechat/* (Phase 2.8) ──────────────────────────────────────
    #
    # WeChat service-account OAuth flow. State persisted in users.sqlite3
    # via users_store. Cookie name: WATCH_SESSION (HttpOnly, SameSite=Lax,
    # Secure when WATCH_BFF_COOKIE_SECURE=true).

    COOKIE_NAME = "WATCH_SESSION"
    COOKIE_SECURE = os.environ.get("WATCH_BFF_COOKIE_SECURE", "true").lower() == "true"

    @app.get("/auth/wechat/login")
    async def wechat_login(redirect: str = "/"):
        """Step 1: mint CSRF state, redirect to WeChat authorize URL."""
        if not wechat_oauth.is_configured():
            raise HTTPException(status_code=503, detail={
                "error": "wechat_not_configured",
                "message": "管理员未配置微信登录;WECHAT_MP_APP_ID 等 env 缺失",
            })
        # Validate redirect: only allow same-origin paths (no open redirect)
        if not redirect.startswith("/") or redirect.startswith("//"):
            redirect = "/"
        state = users_store.create_oauth_state(redirect)
        url = wechat_oauth.build_authorize_url(state)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=url, status_code=302)

    @app.get("/auth/wechat/callback")
    async def wechat_callback(
        code: str,
        state: str,
    ):
        """Step 2: WeChat redirects here with code+state. Exchange
        code for token, fetch userinfo, mint session cookie, redirect
        back to the originally-requested page."""
        from fastapi.responses import RedirectResponse
        consumed = users_store.consume_oauth_state(state)
        if consumed is None:
            raise HTTPException(status_code=400, detail={
                "error": "invalid_or_expired_state",
                "message": "OAuth state 已过期或被使用,请重新发起登录",
            })
        try:
            tok = await wechat_oauth.exchange_code_for_token(code)
        except wechat_oauth.WeChatAPIError as exc:
            raise HTTPException(status_code=400, detail={
                "error": "wechat_token_exchange_failed",
                "errmsg": exc.errmsg,
            })
        # Try to fetch userinfo (only available if scope was snsapi_userinfo).
        # If it fails (silent scope), proceed with openid only.
        try:
            info = await wechat_oauth.get_userinfo(tok.access_token, tok.openid)
            nickname = info.get("nickname")
            unionid = info.get("unionid") or tok.unionid
        except wechat_oauth.WeChatAPIError:
            nickname = None
            unionid = tok.unionid

        users_store.upsert_user(tok.openid, unionid=unionid, nickname=nickname)
        sess = users_store.create_session(tok.openid)

        resp = RedirectResponse(url=consumed.redirect_after, status_code=302)
        resp.set_cookie(
            key=COOKIE_NAME,
            value=sess.id,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            max_age=7 * 24 * 3600,
        )
        return resp

    @app.post("/auth/logout")
    async def wechat_logout(request: Request):
        """Invalidate server-side session and clear the cookie."""
        sid = request.cookies.get(COOKIE_NAME)
        if sid:
            users_store.delete_session(sid)
        from fastapi.responses import JSONResponse
        resp = JSONResponse({"logged_out": True})
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/auth/me")
    async def wechat_me(user=Depends(require_user)):
        """Return the current authenticated user (no MCP call needed)."""
        return {
            "auth": user.get("auth"),
            "user_openid": user.get("user_openid"),
            "user_unionid": user.get("user_unionid"),
        }

    # ── /api/watch/* ─────────────────────────────────────────────────────

    @app.post("/api/watch/start")
    async def watch_start(body: StartRequest, _user=Depends(require_user)):
        args = body.model_dump(exclude_none=True)
        try:
            return await state.call_tool("start_watch", args)
        except RuntimeError as exc:
            raise HTTPException(409, detail={"error": str(exc)})

    @app.get("/api/watch/{video_id}/status")
    async def watch_status(video_id: str, _user=Depends(require_user)):
        return await state.call_tool("get_status", {"video_id": video_id})

    @app.post("/api/watch/{video_id}/cancel")
    async def watch_cancel(video_id: str, _user=Depends(require_user)):
        return await state.call_tool("cancel_watch", {"video_id": video_id})

    @app.get("/api/watch/{video_id}/results")
    async def watch_results(video_id: str, _user=Depends(require_user)):
        return await state.call_tool("get_results", {"video_id": video_id})

    @app.get("/api/watch/{video_id}/frame/{filename}")
    async def watch_frame(video_id: str, filename: str, _user=Depends(require_user)):
        """Stream raw JPEG bytes for a frame.

        We have to call the MCP resource (not a tool) to get the bytes.
        Goes through state.session.read_resource.
        """
        async with state.lock:
            if state._session is None:
                raise HTTPException(503, detail={"error": "mcp_disconnected"})
            uri = f"watch-frame://{video_id}/frames/{filename}"
            result = await state._session.read_resource(uri)
        if not result.contents:
            raise HTTPException(404, detail={"error": "frame_not_found"})
        block = result.contents[0]
        if hasattr(block, "blob") and block.blob:
            import base64
            data = base64.b64decode(block.blob)
            return Response(content=data, media_type="image/jpeg")
        if hasattr(block, "text") and block.text:
            return Response(content=block.text.encode(), media_type="text/plain")
        raise HTTPException(500, detail={"error": "unknown_resource_payload"})

    @app.get("/api/watch/{video_id}/mask/{filename}")
    async def watch_mask(video_id: str, filename: str, _user=Depends(require_user)):
        async with state.lock:
            if state._session is None:
                raise HTTPException(503, detail={"error": "mcp_disconnected"})
            uri = f"watch-frame://{video_id}/masks/{filename}"
            result = await state._session.read_resource(uri)
        if not result.contents:
            raise HTTPException(404, detail={"error": "mask_not_found"})
        block = result.contents[0]
        if hasattr(block, "blob") and block.blob:
            import base64
            data = base64.b64decode(block.blob)
            return Response(content=data, media_type="image/png")
        raise HTTPException(500, detail={"error": "unknown_resource_payload"})

    @app.get("/api/watch/{video_id}/events")
    async def watch_events(
        video_id: str,
        request: Request,
        _user=Depends(require_user),
    ) -> StreamingResponse:
        """SSE: poll get_status every 0.5s, emit on change, close on
        terminal state."""
        async def event_gen() -> AsyncIterator[str]:
            last_status = None
            last_stage = None
            last_progress = None
            deadline = asyncio.get_event_loop().time() + 600.0
            poll_interval = 0.5
            while True:
                if await request.is_disconnected():
                    return
                if asyncio.get_event_loop().time() >= deadline:
                    yield _sse_format("timeout",
                                       {"video_id": video_id, "reason": "timeout"})
                    return
                try:
                    state_dict = await state.call_tool(
                        "get_status", {"video_id": video_id}
                    )
                except Exception as exc:
                    yield _sse_format("error", {"error": str(exc)})
                    return

                cur_status = state_dict.get("status")
                cur_stage = state_dict.get("stage")
                cur_progress = state_dict.get("progress")
                changed = (
                    cur_status != last_status
                    or cur_stage != last_stage
                    or cur_progress != last_progress
                )
                if changed or cur_status in ("pending", "running"):
                    last_status = cur_status
                    last_stage = cur_stage
                    last_progress = cur_progress
                    yield _sse_format("progress", {
                        "video_id": video_id,
                        "stage": cur_stage,
                        "progress": cur_progress,
                        "status": cur_status,
                        "error": state_dict.get("error"),
                    })

                if cur_status in ("done", "error", "cancelled", "not_found"):
                    yield _sse_format("final", {
                        "video_id": video_id,
                        "status": cur_status,
                        "stage": cur_stage,
                    })
                    return
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable proxy buffering
            },
        )

    @app.get("/api/sessions")
    async def list_sessions(_user=Depends(require_user),
                            user_openid: str | None = None):
        return await state.call_tool("list_sessions",
                                     {"user_openid": user_openid})

    @app.post("/api/sessions/{video_id}/delete")
    async def delete_session(video_id: str, _user=Depends(require_user),
                              user_openid: str | None = None):
        return await state.call_tool(
            "delete_session", {"video_id": video_id, "user_openid": user_openid}
        )

    # ── /api/recompose (Phase 2.6) ──────────────────────────────────────

    @app.post("/api/recompose")
    async def recompose(body: RecomposeRequest, _user=Depends(require_user)):
        args = body.model_dump(exclude_none=True)
        return await state.call_tool("recompose", args)

    # ── Error handlers ──────────────────────────────────────────────────

    @app.exception_handler(Exception)
    async def _on_unhandled_exc(request: Request, exc: Exception):
        log.exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": str(exc)},
        )

    return app


# Module-level singleton used by `python3 bff.py` (uvicorn lifespan).
# Tests construct their own via `create_app(state=BFFState())`.
_module_state = BFFState()
app = create_app(_module_state)


def main() -> None:
    parser = argparse.ArgumentParser(description="claude-video BFF")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("WATCH_BFF_PORT", "8910")))
    args = parser.parse_args()

    if _configured_token() is None:
        log.warning(
            "WATCH_BFF_AUTH_TOKEN is unset — all /api/* requests will "
            "be accepted without auth. Set this env var before exposing "
            "the BFF to anything other than localhost."
        )

    log.info("starting BFF on %s:%d", args.host, args.port)
    try:
        uvicorn.run(
            "bff:app",
            host=args.host,
            port=args.port,
            log_level="info",
            lifespan="on",
        )
    except OSError as exc:
        # Port already in use, etc.
        log.error("failed to bind %s:%d: %s", args.host, args.port, exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
