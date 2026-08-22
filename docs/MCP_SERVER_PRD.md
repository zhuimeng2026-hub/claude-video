# PRD: MCP Server for claude-video

**Status**: Draft (planning complete, implementation pending)
**Audience**: LLMs / AI agents integrating with claude-video via MCP
**Owner**: bradautomates/claude-video
**Spec target**: Model Context Protocol (MCP) 2024-11-05 transport

---

## 1. Overview

### 1.1 What this is

`claude-video` ships a single slash command — `/watch <url-or-path> [question]` — that downloads a video, extracts frames, pulls captions (or falls back to Whisper), and hands the result to the calling agent. Today it is reachable only through Agent Skills hosts that read `SKILL.md` (Claude Code, Codex, Cursor, GitHub Copilot, Gemini CLI, and ~50 others).

This PRD adds a **Model Context Protocol (MCP) server** that exposes the same `/watch` pipeline to MCP-speaking agents (openclaw, Claude Desktop, Zed, Continue, custom stdio hosts). One tool, one resource scheme, stdio transport.

### 1.2 Why

MCP hosts cannot read `SKILL.md` and shell out to its scripts — they speak JSON-RPC over stdio. Today a claude-video user inside openclaw has to copy frames off disk and paste them back manually. The MCP server lets the agent call `/watch` like any other tool, and `read_resource` frame bytes directly — no filesystem hand-off.

### 1.3 Out of scope (this PRD)

- HTTP / SSE transports (stdio only for v1).
- Multiple tools (v1 exposes one tool, `watch`).
- Streaming progress notifications (long tasks block the call; v2 enhancement).
- A separate Remotion / segmentation tool (these remain CLI-only). Segmentation runs via `segment.py` (still active). The Remotion converters (`watch_to_remotion.py`, `watch_to_remotion_smart.py`) are **currently disabled** — see `docs/todo.md` §2.6 — and are not available as a follow-up chain step.

---

## 2. External Call Interface

### 2.1 Transport

| Field | Value |
|---|---|
| Transport | **stdio** (JSON-RPC 2.0, one JSON object per line on stdin/stdout) |
| Protocol version | `2024-11-05` (negotiate via `initialize`) |
| Server name | `claude-video` |
| Server version | matches `skills/watch/SKILL.md` frontmatter `version` |
| Invocation | `python3 ${SKILL_DIR}/scripts/mcp_server.py` |
| Working dir | inherited from caller (no chdir) |

The server is **stateful across calls within one process**: it remembers session → work-dir mappings so frame resources stay readable after `tools/call` returns.

### 2.2 Tools

Exactly one tool, `watch`. Signature (JSON Schema view follows Python signature):

```python
watch(
    source: str,                                                    # required: URL or local file path
    detail: "transcript" | "efficient" | "balanced" | "token-burner" | None = None,
    start: str | None = None,                                       # "SS" | "MM:SS" | "HH:MM:SS"
    end: str | None = None,
    timestamps: str | None = None,                                  # "T1,T2,..." (mixed time formats OK)
    max_frames: int | None = None,
    resolution: int = 512,                                          # frame width in px (max 1998px tall)
    fps: float | None = None,                                       # auto-fps otherwise; clamped to 2 fps
    whisper: "groq" | "openai" | "local" | None = None,
    no_whisper: bool = False,
    no_dedup: bool = False,                                         # off by default — dedup drops near-identical frames
    segment: bool = False,                                          # SAM 2 via Replicate (needs REPLICATE_API_TOKEN)
    segment_points: str | None = None,                              # "x,y x,y ..."
    segment_labels: str | None = None,                              # "1 0 ..." (1=fg, 0=bg)
    out_dir: str | None = None,                                     # override work dir; default ~/.cache/watch-mcp/<sid>/
) -> dict
```

The tool returns a JSON object:

```json
{
  "report": "<full markdown report — same content as the /watch CLI prints>",
  "session_id": "01a2b3c4d5e6",
  "work_dir": "/home/user/.cache/watch-mcp/01a2b3c4d5e6",
  "frame_uris": [
    "watch-frame://01a2b3c4d5e6/frame_0001.jpg",
    "watch-frame://01a2b3c4d5e6/frame_0002.jpg"
  ],
  "frame_count": 14,
  "transcript_source": "captions" | "whisper (groq)" | "whisper (openai)" | "whisper (local)" | "none"
}
```

The `report` field is byte-identical to what `python3 ${SKILL_DIR}/scripts/watch.py <source>` writes to stdout, so any existing parsing / summarization logic that consumes the CLI output also works on the tool return.

### 2.3 Resources

After every successful `watch` call, the server exposes each extracted frame as a **read-only resource**:

| Field | Value |
|---|---|
| URI scheme | `watch-frame://` |
| URI format | `watch-frame://<session_id>/<filename>` |
| `<session_id>` | 12-hex-char id from the tool return |
| `<filename>` | `frame_NNNN.jpg` (frames) or `mask_NNNN.png` (segmentation masks) |
| MIME | `image/jpeg` (frames) / `image/png` (masks) |
| Body | raw image bytes |

**Listing** — call `resources/list`. The server returns all currently-known URIs (sessions active in this process). Sessions older than the server's lifetime are not visible.

**Reading** — call `resources/read` with one URI. The server validates that the resolved path stays under the session's `work_dir` (path-traversal defence) and returns the raw bytes.

### 2.4 Lifecycle

| Phase | What happens |
|---|---|
| Server start | Empty `SESSIONS` registry. First `watch` call creates one. |
| `tools/call` `watch` | Pipeline runs (download → extract → transcribe → optional segment). Result is captured into `SESSIONS[session_id] = {work_dir, frame_paths, mask_paths}`. Tool returns the dict above. |
| `resources/list` | Server enumerates `SESSIONS` → returns all `watch-frame://` URIs across all live sessions. |
| `resources/read` | Server resolves URI to disk path under a known session, returns bytes. Unknown session → error. Path-traversal in filename → error. |
| Server shutdown | Work dirs are **NOT** auto-deleted (stdio has no disconnect signal). Clients should call the tool fresh each turn; the work dir is a hint, not a guarantee. |

Work dir location: `~/.cache/watch-mcp/<session_id>/` (Linux/macOS default). Override per-call via `out_dir`. The server logs the work dir at call time (stderr → MCP host may surface in its own UI).

### 2.5 Errors

The server returns MCP error results (not Python tracebacks) for:

| Condition | Returned error |
|---|---|
| `source` URL/path invalid (yt-dlp / ffprobe failure) | Tool result with `isError: true`, `content: [{type: "text", text: "<stderr log>"}]`. Work dir may be partial. |
| `--segment` requested without `REPLICATE_API_TOKEN` | Tool result with non-fatal warning in stderr; segmentation step skipped, other output unaffected. |
| Whisper key missing and captions absent | Report markdown contains explicit "no transcript" notice; tool still returns frames + URIs. |
| Unknown `watch-frame://` session_id | `resources/read` returns `Unknown resource` error. |
| Path traversal in filename component | `resources/read` returns `Invalid URI` error. (Defence-in-depth: resolver forces `Path.is_relative_to(work_dir)`.) |
| `mcp` Python package not installed | Server fails at startup with `ModuleNotFoundError`. Host should pre-install via `pip install --user mcp>=1.0` or rely on `setup.py` auto-install. |

### 2.6 The `recompose` tool (Phase 2.6.2 — not yet implemented)

> **Status**: This section describes the **target contract** that the
> `recompose` MCP tool must implement, as agreed with the OpenMontage
> integration. The tool does not exist in `mcp_server.py` yet; the
> implementation lands as part of `docs/todo.md` §2.6.2. Until then,
> callers needing recomposition should reach the OM adapter through
> the BFF (Phase 2.7, `docs/BFF_API_CONTRACT.md`) once it ships.

The `recompose` tool takes the artifacts produced by an earlier
`watch` call and submits them to OpenMontage's
`tools/external/claude_video.py` adapter for composition + render. It
is the **only** path from `/watch` output to a rendered video in v2+
— direct `npx remotion render` and `ffmpeg` render invocations are
banned by `docs/todo.md` §2.6.

#### 2.6.1 Signature

```python
recompose(
    video_id: str,                                                                  # required; must exist in session_store
    pipeline: "clip-factory" | "documentary-montage" | "podcast-repurpose" |
              "localization-dub" | "hybrid" | "screen-demo",                         # required; whitelist enforced
    style: str = "clean-professional",                                                # optional; OM playbook name
    user_openid: str | None = None,                                                   # optional; BFF overrides with cookie-bound value
    extra: dict = {},                                                                 # optional; transparent passthrough to OM ClaudeVideoInputs.extra
) -> dict
```

The `pipeline` whitelist is the exact set OpenMontage accepts; the
mapping lives in [`docs/OPENMONTAGE_NAME_MAP.md`](OPENMONTAGE_NAME_MAP.md).
Any value outside the whitelist returns
`pipeline_not_in_whitelist` (see §2.6.3).

#### 2.6.2 Return shape

`recompose` returns the same set of fields the OM adapter consumes
(`ClaudeVideoInputs.source` in their integration spec), **plus** the
project metadata that OM returns after submission:

```json
{
  "video_id": "b9f3c1a27e58",
  "frames_dir": "/home/user/.cache/watch-mcp/b9f3c1a27e58/frames/",
  "masks_dir":  "/home/user/.cache/watch-mcp/b9f3c1a27e58/masks/",
  "vtt_path":   "/home/user/.cache/watch-mcp/b9f3c1a27e58/transcript.en.vtt",
  "video_path": "/home/user/.cache/watch-mcp/b9f3c1a27e58/source.mp4",
  "duration_seconds": 137.42,
  "transcript_segments": [
    {"start": 0.00, "end": 2.84, "text": "Big Buck Bunny is a short animated film..."},
    {"start": 2.84, "end": 5.92, "text": "..."}
  ],

  "project_id": "abc123def456",
  "status": "submitted",
  "render_url": "https://example.com/renders/abc123def456/final.mp4",
  "backlot_url": "http://localhost:8900/backlot/abc123def456"
}
```

**Field derivation** (from session_store + watch artifacts):

| Field | Source |
|---|---|
| `video_id` | Input argument (echoed back). |
| `frames_dir` | `session_store[video_id]["work_dir"] + "/frames"` — guaranteed to exist if the `watch` run succeeded. |
| `masks_dir` | Same prefix + `"/masks"` — `None` if `--segment` was not used (the OM adapter treats `None` as "no masks to copy"). |
| `vtt_path` | Same prefix + `/transcript.<lang>.vtt` — derived from the language of the caption track used; `None` if no captions and no Whisper run. |
| `video_path` | Same prefix + `/source.mp4` — the file yt-dlp downloaded; `None` if the source was already a local path that the caller moved. |
| `duration_seconds` | `ffprobe -show_entries format=duration -of csv=p=0 <video_path>` — probed lazily at recompose time, cached on first read. |
| `transcript_segments` | Parsed from `vtt_path` via the existing `transcribe.parse_vtt()` helper — same data structure `watch.py` already uses internally. |

A canonical example of this shape lives at
[`tests/fixtures/sample_runresult.json`](../tests/fixtures/sample_runresult.json);
tests in both repos pin against that fixture rather than re-deriving
the schema from MCP tool calls.

The `watch` tool's return shape is **NOT** changed by this section —
it keeps its current `report` / `session_id` / `work_dir` / `frame_uris` /
`frame_count` / `transcript_source` fields. `recompose` is a separate
tool that re-derives the structured inputs from session storage.

#### 2.6.3 Error envelope (ToolError codes)

When `recompose` fails, the tool raises an MCP `ToolError` whose
`message` field carries a stable, machine-readable code in square
brackets. These codes MUST stay 1:1 with OpenMontage's
`claude-video-integration.md` §4.4; both sides' error tables are
generated from the same source-of-truth list.

| `code` (in `[brackets]` after the message) | HTTP status (BFF) | Trigger |
|---|---|---|
| `pipeline_not_in_whitelist` | 422 | `pipeline` is not in the whitelist from `docs/OPENMONTAGE_NAME_MAP.md`. Error message lists the allowed values. |
| `video_id_unknown` | 404 | `video_id` is not in `session_store` — either never created by `watch`, expired by cleanup, or belongs to a different BFF instance. |
| `user_not_found` | 403 | `user_openid` was required (BFF passed an empty value, or session is anonymous in a deploy that disallows it). |
| `assets_copy_failed` | 422 | `cp -r <work_dir>/{frames,masks,*.vtt,source.mp4} <projects/users/<user_openid>/<project_id>/assets/>` returned non-zero (disk full, permission denied, missing source file). Distinct from `pipeline_stage_failed` (which fires later, during OM-side composition/render). |
| `pipeline_stage_failed` | 422 | OM reported a failure in any of `submit`, `compose`, `render`. The OM error message is preserved in the `message` field after the `[pipeline_stage_failed]` prefix. |
| `gpu_required` | 422 | `pipeline` would need a GPU provider (`FLUX`, `Kling`, `local_diffusion`, `hunyuan_video`, `wan_video`, `cogvideo_video`) on this no-GPU host. This is a hard ban — see `docs/todo.md` §2.6 "GPU-free 约束". |

A canonical example envelope per code is in
[`tests/fixtures/`](../tests/fixtures/):

- `error_envelope_pipeline_not_in_whitelist.json`
- `error_envelope_video_id_unknown.json`
- `error_envelope_assets_copy_failed.json`

(The remaining three — `user_not_found`, `pipeline_stage_failed`,
`gpu_required` — are planned but not yet committed as fixtures.)

#### 2.6.4 Authorization contract

`recompose` enforces the second-line checks described in
[`docs/OAUTH_TRUST_MODEL.md`](OAUTH_TRUST_MODEL.md):

1. `video_id` must exist in `session_store` (Phase 2.1 introduces persistent session storage).
2. `session_store[video_id]["user_openid"]` must equal the `user_openid` parameter passed to `recompose`. Mismatch → `user_not_found` (or a tighter `video_id_user_mismatch` code — see `docs/OAUTH_TRUST_MODEL.md` §MCP tool layer).

The BFF is expected to silently override any `user_openid` sent in
HTTP request bodies with the cookie-bound value, so this check is
typically a no-op in normal operation. It exists so that an MCP
caller without BFF protection (e.g. a custom stdio client) cannot
recompose someone else's session.

---

## 3. Configuration & Installation

### 3.1 Prerequisite binaries (host-side, must exist)

- `python3` (or `python` on Windows — see SKILL.md note).
- `ffmpeg` + `ffprobe` on `PATH`.
- `yt-dlp` on `PATH`.

### 3.2 Prerequisite Python packages

- `mcp>=1.0` — the official Anthropic SDK. This is the project's first third-party Python dep (everything else is stdlib + system binaries).

Two install paths:

1. **Automatic** — `${SKILL_DIR}/scripts/setup.py` auto-installs `mcp` via `pip install --user mcp` if absent. Same semantics as the existing brew/apt install of ffmpeg / yt-dlp. The preflight `--json` output gains `mcp_available: bool`.
2. **Manual** — `pip install --user "mcp>=1.0"`.

### 3.3 Host registration (openclaw)

```json
{
  "mcpServers": {
    "claude-video": {
      "command": "python3",
      "args": ["/absolute/path/to/skills/watch/scripts/mcp_server.py"],
      "env": {}
    }
  }
}
```

`SKILL_DIR` is the directory containing the `SKILL.md` the host already loaded — substitute the absolute path the host reported. Path is identical for Claude Desktop, Zed, and Continue (same JSON schema).

### 3.4 Openclaw `mcporter.json` variant

```json
{
  "claude-video": {
    "command": "python3",
    "args": ["/absolute/path/to/skills/watch/scripts/mcp_server.py"]
  }
}
```

### 3.5 Skills distribution

- `npx skills add bradautomates/claude-video -g` → ships `mcp_server.py` alongside `watch.py` in the user's installed skill folder.
- claude.ai web upload (`dist/watch.skill`, built by `scripts/build-skill.sh`) → **does NOT** include `mcp_server.py`. The MCP server is for local stdio agents, not the sandboxed claude.ai runtime. `.skillignore` enforces this.

---

## 4. Use Cases for LLM Callers

### 4.1 Minimal — YouTube link + question

```json
{
  "method": "tools/call",
  "params": {
    "name": "watch",
    "arguments": {
      "source": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "detail": "balanced"
    }
  }
}
```

Returns the markdown report (with title, uploader, duration, frame list, transcript). The LLM caller `Read`s each absolute path in the report's `## Frames` section to see the images, or `resources/read` the `watch-frame://` URIs to fetch bytes directly.

### 4.2 Focused window

User asked about a specific moment:

```json
{
  "method": "tools/call",
  "params": {
    "name": "watch",
    "arguments": {
      "source": "https://youtu.be/<id>",
      "start": "2:15",
      "end": "2:45",
      "fps": 2
    }
  }
}
```

Denser frame budget inside the window; transcript auto-filtered to the same range.

### 4.3 Transcript only — no frames, no download if captioned

```json
{
  "method": "tools/call",
  "params": {
    "name": "watch",
    "arguments": {
      "source": "https://youtu.be/<id>",
      "detail": "transcript"
    }
  }
}
```

Skips video download when captions exist; transcript-only report. Use when the user only needs what was said.

### 4.4 Cue frames + targeted moments

```json
{
  "method": "tools/call",
  "params": {
    "name": "watch",
    "arguments": {
      "source": "https://youtu.be/<id>",
      "timestamps": "4:32,7:10,9:55",
      "detail": "balanced"
    }
  }
}
```

Cue frames at transcript-flagged "look here" moments are reserved against the cap and reported with `reason=transcript-cue`.

### 4.5 Local file with Whisper fallback

```json
{
  "method": "tools/call",
  "params": {
    "name": "watch",
    "arguments": {
      "source": "/Users/me/Movies/screen-recording.mp4",
      "whisper": "groq"
    }
  }
}
```

Local file with no captions → falls back to `whisper-large-v3` via Groq (needs `GROQ_API_KEY` in `~/.config/watch/.env`).

### 4.6 Fetching a frame as image bytes

After a `watch` call returns `frame_uris: ["watch-frame://01a2b3c4d5e6/frame_0001.jpg"]`:

```json
{
  "method": "resources/read",
  "params": { "uri": "watch-frame://01a2b3c4d5e6/frame_0001.jpg" }
}
```

Returns JPEG bytes — drop straight into a multimodal context.

---

## 5. Call Patterns for LLM Clients

### 5.1 Recommended flow

1. **Discover**: `tools/list` → confirm `watch` is available with the expected schema.
2. **Plan**: choose `detail` based on duration guess; pick `--start`/`--end` if user named a moment; pick `whisper` only if captions likely absent.
3. **Call**: `tools/call` with `watch` and the planned arguments.
4. **Read result**:
   - Parse `report` markdown for title, duration, frame count, transcript text.
   - Either `Read` each absolute path in `## Frames` OR `resources/read` each `watch-frame://` URI. URIs are valid for the lifetime of the server process.
5. **Synthesize**: answer the user's question grounded in what the frames show and what the transcript says.

### 5.2 Anti-patterns

- **Don't** call `watch` more than once on the same video in one session. Re-running re-downloads and re-extracts; cache the result in conversation context.
- **Don't** request `token-burner` on >10-min videos unless the user explicitly asks for full coverage. Image-token cost grows linearly.
- **Don't** assume `whisper` will succeed. If `~/.config/watch/.env` has no key, captions must exist or `transcript_source` will be `"none"` — surface this to the user and offer `--no-whisper` re-run if appropriate.
- **Don't** pass relative paths in `out_dir`. The server resolves them; pass absolute paths to avoid ambiguity.

### 5.3 Long-task handling

`tools/call` blocks until the pipeline finishes. For a 30-min video with `balanced` detail, expect 30-90 s including download. The server prints progress to stderr; some MCP hosts surface stderr in the agent's run log. There is no per-stage progress notification in v1 — if a host needs streaming progress, the call will return only when complete.

---

## 6. Constraints & Trade-offs

| Constraint | Impact |
|---|---|
| `mcp` is the first third-party Python dep | Disk + install step on every host. Mitigated by `setup.py` auto-install. |
| Stdio transport only | v1. SSE/HTTP deferred. Local agents only. |
| Work dirs not auto-cleaned | User must delete `~/.cache/watch-mcp/` manually. Documented in tool output footer. |
| One tool | Composition happens client-side. If you want segmented output, call `watch` first then shell out to `segment.py` directly (it remains a CLI script). The Remotion converters are disabled — see `docs/todo.md` §2.6 for the re-enable plan. |
| Session registry is in-process | Server restart loses session IDs. URIs dangling after restart are surfaced as `Unknown resource` errors — clients should re-call `watch` rather than cache URIs across restarts. |
| Resource reads return raw image bytes | MIME type is `image/jpeg` / `image/png`. Multiplexing or conversion not supported. |

---

## 7. Reference

- MCP spec: https://modelcontextprotocol.io/specification/2024-11-05
- mcp Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Project SKILL.md: `skills/watch/SKILL.md` (same parameter semantics, full prose docs)
- Watch CLI flag reference: this doc + `skills/watch/SKILL.md` (canonical)
- Test fixtures: `tests/conftest.py` (`cut_clip`, `static_clip` — ffmpeg-synthesized, no network)

---

## 8. Open Questions (for follow-up)

1. Should `tools/call` return streaming progress via `meta.progressToken`? (MCP supports it; needs host testing.)
2. Should there be a `cleanup` tool that deletes a session's work dir? (Avoids manual cleanup; needs confirmation flow.)
3. Should segmentation / Remotion-conversion be exposed as their own MCP tools in v2?
4. SSE / HTTP transport for remote agents?

These are deliberately out of scope for v1.