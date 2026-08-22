# claude-video BFF (Phase 2.7) — HTTP/SSE API contract

> **PIN WARNING**: Each endpoint below is part of a cross-repo contract.
> The body shape, response shape, and HTTP status codes are pinned; any
> change here must also land in `OpenMontage_Voicebox/docs/
> claude-video-integration.md` (their integration test will hit these
> endpoints via `curl`). See `docs/openmontage-integration-inputs.md`
> §3 for the originating request.

The BFF is a FastAPI app (`skills/watch/scripts/bff.py`, Phase 2.7)
that wraps the stdio MCP server so browsers can talk to `/watch`
without holding a JSON-RPC stdio handle. The MCP server remains the
single source of truth — the BFF is a thin translator.

## Conventions

- **Port**: `8910` default, override via `WATCH_BFF_PORT`.
- **CORS**: `["http://localhost:*", "tauri://"]` default, override via `WATCH_BFF_CORS_ORIGINS` (JSON list).
- **Auth**: every `/api/*` route except `/health` requires the `WATCH_SESSION` cookie. See `docs/OAUTH_TRUST_MODEL.md`.
- **Error envelope**: `{error: "<machine_code>", message: "<human readable>"}` with an HTTP status that mirrors `code`. Error codes mirror `docs/MCP_SERVER_PRD.md` §2.6 — same names, same triggering conditions.
- **SSE** (`/events`): one event per line, `data: {json}\n\n` framing, events keyed by `video_id` in the URL path.
- **stdio MCP child**: the BFF spawns one `mcp_server.py` subprocess at startup, persists the connection, and serializes all tool calls through an `asyncio.Lock` (JSON-RPC over stdio is stateful — concurrent writes would corrupt the stream).

## Endpoints

### `POST /api/watch/start`

Start a `watch` run for a given source.

```bash
curl -X POST http://localhost:8910/api/watch/start \
  -H 'Content-Type: application/json' \
  -b 'WATCH_SESSION=<cookie>' \
  -d '{"source": "https://example.com/video.mp4", "video_id": "optional-12hex", "user_openid": null}'
```

| Field | Required | Notes |
|---|---|---|
| `source` | yes | URL or local path. Validated server-side (`is_url` for URLs, `Path.resolve().is_file()` for local paths). |
| `video_id` | no | 12-hex. If omitted, the BFF computes `hashlib.sha1(source).hexdigest()[:12]`. |
| `user_openid` | no | BFF silently overrides with the cookie-bound value. Accepted in the body for parity with the MCP tool signature. |

**Success** (200): `{video_id, status: "running", stage: "download"}`

**Errors**:

| Status | Body | When |
|---|---|---|
| 400 | `{error: "invalid_source", message: "..."}` | `source` failed validation |
| 401 | `{error: "not_authenticated", message: "..."}` | Missing/expired cookie |
| 409 | `{error: "video_id_in_use", message: "..."}` | Existing session with same `video_id` belongs to a different `user_openid` |

### `GET /api/watch/{video_id}/status`

```bash
curl http://localhost:8910/api/watch/b9f3c1a27e58/status -b 'WATCH_SESSION=...'
```

**Success** (200): `{video_id, stage, progress: 0-100, eta_seconds?: number, error?: string}`

Where `stage` is one of `"download" | "frames" | "transcribe" | "segment" | "done" | "error" | "cancelled"`.

**Errors**: 401 (`not_authenticated`), 403 (`forbidden` — caller not owner of `video_id`), 404 (`video_id_unknown`).

### `GET /api/watch/{video_id}/frame/{filename}`

```bash
curl -OJ http://localhost:8910/api/watch/b9f3c1a27e58/frame/frame_0001.jpg -b 'WATCH_SESSION=...'
```

**Success** (200): `image/jpeg` bytes. Filename must match `frame_NNNN.jpg` (path traversal rejected by MCP layer).

**Errors**: 401, 403, 404 (`frame_not_found`).

### `GET /api/watch/{video_id}/mask/{filename}`

Same shape as `/frame/{filename}` but `image/png` and filenames match `mask_NNNN.png`. 404 is `mask_not_found`.

### `GET /api/watch/{video_id}/events` — **SSE**

```bash
curl -N http://localhost:8910/api/watch/b9f3c1a27e58/events -b 'WATCH_SESSION=...'
```

Server-Sent Events stream. One event per stage transition. Framing:

```
data: {"stage":"download","progress":0,"message":"starting","ts":"2026-08-23T07:00:00Z"}

data: {"stage":"frames","progress":30,"message":"extracted 24/80","ts":"2026-08-23T07:00:05Z"}

...
data: {"stage":"done","progress":100,"message":"complete","ts":"2026-08-23T07:00:42Z"}
```

**Why SSE not WebSocket** (rationale for OM, see
`docs/openmontage-integration-inputs.md` §3 + OpenMontage's
`openmontage-integration.md:53-58`): MCP long connections + CORS
don't fit browsers. SSE gives one-way push with auto-reconnect via
the `EventSource` API, no upgrade dance, and works through standard
HTTP middleware.

**Errors**: 401, 403, 404. The stream closes after `stage=done` or `stage=error`.

### `POST /api/watch/{video_id}/cancel`

```bash
curl -X POST http://localhost:8910/api/watch/b9f3c1a27e58/cancel -b 'WATCH_SESSION=...'
```

**Success** (200): `{cancelled: true}` — sets the cancellation `threading.Event`, the background watch checks at each stage boundary.

**Errors**: 401, 403, 404 (`video_id_unknown`).

### `POST /api/recompose`

Thin wrapper around the `recompose` MCP tool.

```bash
curl -X POST http://localhost:8910/api/recompose \
  -H 'Content-Type: application/json' \
  -b 'WATCH_SESSION=...' \
  -d '{"video_id": "b9f3c1a27e58", "pipeline": "clip-factory", "style": "clean-professional", "user_openid": null}'
```

| Field | Required | Notes |
|---|---|---|
| `video_id` | yes | Must exist in `session_store`. |
| `pipeline` | yes | Whitelist enforced — see `docs/OPENMONTAGE_NAME_MAP.md`. |
| `style` | no | Default `clean-professional`. |
| `user_openid` | no | BFF overrides with cookie-bound value. |
| `extra` | no | Dict; OM whitelist on keys. |

**Success** (200): `{project_id, status: "submitted", render_url?, backlot_url?, ...inputs...}` — see `docs/MCP_SERVER_PRD.md` §2.6 for the full schema.

**Errors**: 401, 403, 404 (`video_id_unknown`), 422 (`pipeline_not_in_whitelist` / `gpu_required` / `assets_copy_failed` / `pipeline_stage_failed`). The `code` field of the MCP `ToolError` becomes the BFF's `error` field — names match exactly.

### `GET /health`

```bash
curl http://localhost:8910/health
```

**Success** (200): `{status: "ok"}`. No auth required. Used by OM-side integration smoke test to confirm BFF is reachable before exercising real endpoints.

## Cross-repo contract — what OM tests

OpenMontage's `tests/integration/test_claude_video_adapter.py` will:

1. Spawn `python3 bff.py` (or import it as a FastAPI app and use `httpx.AsyncClient(transport=ASGITransport(app=app))` — TBD with their owner).
2. Hit `/health` first as a precondition check.
3. Issue a real `POST /api/watch/start` against a 10s test clip.
4. Subscribe to `/events` and assert the stage progression (`download` → `frames` → `transcribe` → `done`).
5. `GET /api/watch/{id}/frame/frame_0001.jpg` and assert the JPEG SOI marker matches.
6. `POST /api/recompose` with a known whitelist `pipeline`.
7. Assert the response shape matches `docs/MCP_SERVER_PRD.md` §2.6.

Auth will be skipped in their CI (a `WATCH_BFF_SKIP_AUTH=1` env var that the BFF honors for test environments only — proposal to be confirmed before they wire it up).

## What this contract does NOT cover

- The BFF's own internal scheduler / watchdog — those are BFF-internal and don't appear in the API.
- OpenMontage's `/backlot/<project_id>` URL — they own that surface; we just expose `render_url` / `backlot_url` from their adapter's response.
- Browser-specific retry / reconnection behavior — that's the caller's job; the BFF just emits clean SSE events.

## Related docs

- `docs/todo.md` §2.7 — BFF implementation checklist
- `docs/MCP_SERVER_PRD.md` §2.6 — `recompose` MCP tool spec (the BFF's `/api/recompose` is a thin wrapper around it)
- `docs/OAUTH_TRUST_MODEL.md` — auth surface that wraps every `/api/*` route
- `docs/OPENMONTAGE_NAME_MAP.md` — the `pipeline` whitelist enforced at `/api/recompose`
- `docs/openmontage-integration-inputs.md` §3 — origin of this contract
