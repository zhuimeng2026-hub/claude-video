# Changelog

All notable changes to `/watch` are documented here.

## [0.4.0] — 2026-08-23

This is a substantial expansion of `/watch`: the single-call MCP tool
is now part of an 8-tool surface backed by a persistent session
registry, a background pipeline runner, an HTTP+SSE BFF for browser
clients, and a WeChat service-account OAuth flow. The on-disk
footprint grew from one entry-point script to seven, all documented
under `docs/MCP_CLIENT_COMPAT.md` and `docs/MCP_SERVER_PRD.md`.

### Added

#### MCP server (`skills/watch/scripts/mcp_server.py`) — 1 → 8 tools

The single sync `watch` tool is preserved as a convenience entry-point
and joined by 7 new tools that enable background execution, status
polling, and recomposition:

  - `watch` (kept from v0.3.0, now with `video_id`, `restart`,
    `user_openid`, `user_unionid`, `auth_source`, `allow_arbitrary_out`)
    — sync single-call convenience wrapper
  - `start_watch` (Phase 2.2) — spawns the pipeline in a background
    thread, returns immediately with `{video_id, status: "running",
    stage: "download"}`; supports `timeout_seconds` for auto-cancel
  - `get_status` (Phase 2.2) — `{video_id, status, stage, progress,
    error}` from the persistent session registry
  - `get_results` (Phase 2.2) — full pipeline result on terminal state
  - `cancel_watch` (Phase 2.2) — cooperative cancel via `threading.Event`
    at next stage boundary
  - `list_sessions` (Phase 2.1) — openid-scoped session listing
  - `delete_session` (Phase 2.1) — record removal (does not touch work_dir)
  - `recompose` (Phase 2.6) — submits session artifacts to
    OpenMontage_Voicebox via stdio MCP; GPU-free pipeline whitelist
    enforced (`clip-factory` / `documentary-montage` /
    `podcast-repurpose` / `localization-dub` / `hybrid` / `screen-demo`)

#### Persistent session registry (`session_store.py`, Phase 2.1)

  - Disk-backed JSON store at `~/.cache/watch-mcp/sessions.json`
    (atomic temp+os.replace write, mode 0600, `fcntl.flock` inter-process
    mutex, WAL-equivalent for atomic single-file writes)
  - `SessionRecord` dataclass carries `video_id`, `work_dir`, frames,
    masks, transcript, `user_openid` / `user_unionid` / `auth_source`
    placeholders (Phase 2.8 wires the OAuth verification), `stage`,
    `progress`, `error`
  - `video_id` stable across calls: derived as `sha256(source)[:12]` by
    default, caller-overridable; same `video_id` reuses cached result
    unless `restart=True` or a new flag demands a re-run
  - Cross-user isolation: `list_for_user(openid)` and
    `delete_session` honor the `user_openid` boundary (orphan records
    with no openid are visible to anyone until Phase 2.8 OAuth flow
    lands; tagged records require matching openid)
  - 8 concurrency tests including N=8 parallel writers hammering the
    same file with no lost updates

#### Background pipeline runner (`pipeline_runner.py`, Phase 2.2 + 2.5)

  - `threading.Thread` per running job; in-memory registry indexed by
    `video_id`
  - `cancel_event` checked between stages (ffmpeg / yt-dlp calls
    finish their current invocation; cancel takes effect at the next
    stage boundary — no half-written pipelines)
  - `threading.Timer` watchdog for `timeout_seconds`; timeout and
    user-cancel produce distinguishable error strings
    (`"timeout after Xs"` vs `"cancelled by user"`) so the caller can
    react differently
  - `progress_hook(video_id, stage, progress, message)` callback
    invoked from the background thread on every transition — used by
    the BFF to forward stdio MCP `notifications/progress`

#### Browser BFF (`bff.py`, Phase 2.7)

  - FastAPI app on `WATCH_BFF_PORT=8910` (default) that proxies HTTP
    + SSE to the MCP server's stdio subprocess. 10 REST/SSE endpoints
    under `/api/*` plus 4 OAuth routes under `/auth/*`.
  - `BFFState` holds one stdio MCP subprocess + `ClientSession` +
    `asyncio.Lock`; the lock serializes tool calls because JSON-RPC
    over stdio requires ordered request/response
  - SSE endpoint `GET /api/watch/{video_id}/events` polls
    `get_status` every 0.5s, emits `progress` + `final` records
  - CORS default: `http://localhost:*` + `tauri://`; override via
    `WATCH_BFF_CORS_ORIGINS`
  - `WATCH_BFF_AUTH_TOKEN` Bearer-token fallback for inter-service
    auth (replaced by OAuth when configured)

#### WeChat service-account OAuth (`users_store.py` + `wechat_oauth.py`,
Phase 2.8)

  - `users.sqlite3` schema: `users` / `sessions` / `oauth_states` with
    WAL mode. SQLite is the right choice here vs. JSON for relational
    state (sessions have FK to users, oauth_states are consumed).
  - OAuth CSRF state stored as SHA-256 hash (plaintext state never
    persisted — DB read doesn't leak usable tokens)
  - One-time-use `consume_oauth_state()` runs lookup + delete in a
    single transaction so concurrent consume() calls can't both win
  - Session sliding window: each authenticated request extends
    `expires_at` by 7 days; idle sessions GC'd by
    `cleanup_expired_sessions()`
  - `require_user` middleware reads `WATCH_SESSION` cookie → look up
    openid → return `{"user_openid", "user_unionid"}` for downstream
    tool calls. WeChat mode is strict — when configured, no cookie
    means 401 (no anonymous fallback, mirroring OpenMontage
    `web-multiuser-auth.md` hard constraint)
  - Routes: `GET /auth/wechat/login` → 302 to WeChat authorize URL;
    `GET /auth/wechat/callback` → exchange code → set cookie → 302 to
    redirect_after; `POST /auth/logout`; `GET /auth/me`

#### OpenMontage recomposition (`openmontage_client.py`, Phase 2.6)

  - Thin stdio MCP client to `OpenMontage_Voicebox/mcp_server.py`
    (default path, override with `OPENMONTAGE_BIN` env var). Spawns
    subprocess on demand; submits `{video_id, user_openid,
    frames_dir, masks_dir, vtt_path, video_path, pipeline, style}`
    inputs to the OpenMontage-side `claude_video.compose` tool.
  - GPU-free pipeline whitelist enforced **twice**: Pydantic
    `Literal[...]` on the tool signature (function-body never runs
    for disallowed values), plus runtime keyword detection
    (flux/kling/diffusion/video) in case the Literal is widened
    later
  - Cross-repo integration spec at
    `OpenMontage_Voicebox/docs/claude-video-integration.md` (425 lines,
    written by us) covers the OpenMontage-side code changes needed:
    `tools/external/claude_video.py` BaseTool, asset copy protocol,
    `projects/users/<openid>/` user isolation reuse

#### Standalone SSE daemon (`sse_progress.py`, Phase 2.3)

  - Separate process at `WATCH_SSE_PORT=8911` exposing
    `GET /progress/{video_id}`. Polls `session_store.json` for state
    changes, emits `data: {json}\n\n` SSE records. Used directly by
    LAN clients or proxied through the BFF.

#### Standalone CLI converters (Phase 2.6.1, **disabled** — kept as `.py_tmp`)

  - `watch_to_remotion.py` and `watch_to_remotion_smart.py` remain
    on disk as `watch_to_remotion.py_tmp` and
    `watch_to_remotion_smart.py_tmp` respectively (the v0.3.0
    "disable by rename" pattern from commit `1cac74b`, restored
    after a brief same-day re-enable in `73c92da` and immediate
    revert in `895520b`). Python's import system ignores them, so
    nothing in the `/watch` MCP server's main flow can call them
    accidentally. **They are not usable as CLI tools in v0.4.0** —
    only the re-enable path (`mv *.py_tmp *.py`) would make them
    importable, and that's gated on the four §2.6.1 conditions
    (adapter refactor / `OPENMONTAGE_REQUIRED` env guard /
    `test_remotion_guard.py` / no-FastMCP-coupling) landing first.
    See `docs/MCP_SERVER_PRD.md` §1.3 for the rationale and
    `docs/todo.md` §2.6.1 for the four open items.

### Changed

  - **MCP tool surface grew from 1 to 8** (see Added). Existing
    clients calling `watch` continue to work; new code should prefer
    `start_watch` + `get_status` + `get_results` for long-running
    videos. `watch` is kept as a sync convenience.
  - `SessionRecord` schema gains `video_id` (persistent cache key),
    `user_openid` / `user_unionid` / `auth_source` (Phase 2.8 OAuth
    placeholders, stored but not enforced until OAuth flow lands),
    `stage` / `progress` / `error` (Phase 2.2 pipeline status).
    Sessions written before 0.4.0 still load (`from_dict` tolerates
    missing fields with defaults).
  - `watch-frame://<session_id>/...` URI scheme preserved. The
    `session_id` is now 12-char random per call (was already), but
    the `video_id` is the new persistent cache key. `read_frame` /
    `read_mask` still keyed by session_id because that's what the
    in-memory `SESSIONS` dict registers — `get_results` returns a
    fresh `session_id` per call so the URI scheme stays useful.
  - All `@mcp.tool` now use `structured_output=False` to work around
    the mcp>=1.20 / pydantic 2.10 `Annotated[bytes, Field(...)]`
    output-model crash. See `docs/MCP_SERVER_PRD.md` §6.1 Pitfall A.
  - Path traversal defence widened: `out_dir` parameter on `watch`
    now requires the resolved path to live under
    `~/.cache/watch-mcp/` by default; tests / ops can opt out with
    `allow_arbitrary_out=True`. The Phase 1.3 path-traversal
    defences on `read_frame` / `read_mask` filenames remain.
  - `setup.py --json` output adds `mcp_server_compat` /
    `mcp_server_error` fields (Phase 1.2) and `mcp_available`,
    `whisper_backend`, etc. status visibility.

### Removed

  - Nothing in v0.4.0 removes user-facing functionality. The two
    Remotion scripts that the v0.3.0 saga disabled are **still
    disabled in v0.4.0** — kept on disk as `.py_tmp` (see Added
    above), not importable, not part of any CLI surface. The
    `mv *.py_tmp *.py` re-enable path is gated on the four §2.6.1
    conditions; until they land, "recomposition" means
    `recompose` → OpenMontage only.

### Security

  - Phase 1.3 — `_validate_source` rejects empty / control-char /
    `-`-prefixed sources (defends against argv injection and weird
    edge cases); `_validate_out_dir` rejects relative paths and
    paths outside `~/.cache/watch-mcp/` unless explicitly allowed.
  - Phase 2.8 — OAuth state stored as SHA-256, never plaintext;
    `consume_oauth_state` is atomic so concurrent calls can't both
    succeed; `WATCH_SESSION` cookie is `HttpOnly + SameSite=Lax`
    with `Secure` flag conditional on `WATCH_BFF_COOKIE_SECURE=true`.
  - Phase 2.6 — GPU-only pipeline rejection prevents callers from
    even attempting to trigger expensive remote providers (FLUX /
    Kling / etc.) from the no-GPU box.

### Test counts

  - 191 passed, 1 pre-existing failure (`test_config.py::test_get_config_keys`
    was already failing on clean main before Phase 2.x — config
    schema mismatch unrelated to this release)
  - 13 new test files added in this release:
    `tests/test_session_store.py`, `tests/test_video_id_reuse.py`,
    `tests/test_split_tools.py`, `tests/test_pipeline_runner.py` (inline
    via split-tools), `tests/test_progress_push.py`,
    `tests/test_timeout_cancel.py`, `tests/test_recompose.py`,
    `tests/test_remotion_guard.py`, `tests/test_bff.py`,
    `tests/test_oauth_phase28a.py`, `tests/test_oauth_phase28b.py`
  - Smoke test (`tests/test_mcp_stdio_smoke.py`) extended from 1 to
    4 tests; still passes against any MCP 2024-11-05 stdio client

### Documentation

  - `docs/MCP_SERVER_PRD.md` — substantial update: §2.6 documents the
    new `recompose` tool, §6.1 catalogs the three SDK pitfalls we
    hit during Phase 1 (bare-bytes crash, importlib synthetic name,
    BlobResourceContents base64), §1.3 captures the v0.3.0 Remotion
    saga's final state and the v0.4.0 re-enable
  - `docs/MCP_CLIENT_COMPAT.md` — new §10 "Web service + browser
    integration patterns" with three drop-in examples (Node.js stdio
    wrap, Python stdio wrap, browser via BFF), an architecture
    comparison table, and a migration path from stdio wrap to BFF
  - `docs/todo.md` — extensive. Phase 1 (1.1–1.5) and Phase 2
    (2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8) all marked ✅
  - `OpenMontage_Voicebox/docs/claude-video-integration.md` (425
    lines, cross-repo) — owned by OpenMontage team to implement;
    issue list at the bottom tracks the 7 code-change items needed on
    their side

### Upgrade notes

  - **No data migration needed.** Existing session dirs (named by
    old `session_id`) keep working — the new `video_id` is
    independent. Re-running `watch <same-source>` after upgrade
    creates a fresh record with a new `video_id` (sha256-derived),
    then subsequent calls hit the new cache.
  - **MCP clients calling `watch` are not broken.** The sync tool's
    signature adds 6 optional params (`video_id`, `restart`,
    `user_openid`, `user_unionid`, `auth_source`,
    `allow_arbitrary_out`); all default to the previous behaviour.
  - **MCP clients that want progress updates** must call
    `start_watch` instead of `watch`. The response shape changed
    slightly: cache-hit returns `{video_id, status, stage, progress,
    reused: True}`; cache-miss returns the same plus `session_id`
    and `work_dir`.
  - **The BFF on port 8910 is opt-in.** Don't auto-start it — leave
    stdio MCP clients alone unless browsers need access.
  - **OAuth requires explicit opt-in via env.** No `WECHAT_MP_*`
    vars = no `/auth/*` routes, `require_user` falls back to dev
    bypass (or Bearer token if `WATCH_BFF_AUTH_TOKEN` set). Existing
    Bearer-token setups continue to work; if you set WeChat env,
    Bearer tokens are silently shadowed (WeChat cookie path wins).
  - **OpenMontage not required at install time.** The `recompose`
    tool checks `OPENMONTAGE_BIN` at call time, not at startup.
    Missing binary → ToolError("OpenMontage MCP binary not found").
    Run without `recompose` and the rest of the server works fine.

---

## [0.3.0] — 2026-08-19

### Added
- **`scripts/watch_to_remotion.py`** — deterministic `/watch` → Remotion converter. Parses the work directory's VTT cues, copies extracted frames (and original video when available), and scaffolds a runnable Remotion 4.x project (`Root.tsx`, `Composition.tsx`, `Subtitles.tsx`) that renders either an `OffthreadVideo` with burned-in subtitles or a frame slideshow. `public/cues.json` is the single source of truth — edit it to retime subtitles or swap modes without touching code. Honors `/watch`'s frame-dedup gaps by exporting actual sorted frame filenames in `framePaths`.
- **`scripts/watch_to_remotion_smart.py`** — LLM-driven variant. Same input, but an LLM (OpenAI / Groq / LiteLLM, stdlib HTTP, no `pip install` needed) reads the transcript + frame count + duration and produces a structured JSON spec describing scenes, highlights, intro/outro copy, and subtitle styling. The resulting `Composition.tsx` is video-tailored rather than a fixed template. `--dry-run` prints the prompt and scaffolds a project from a `DEFAULT_SPEC` so the pipeline is testable without API credentials.

> **Status (2026-08-23, final)**: **Disabled again.** Briefly re-enabled in commit `73c92da` (mv *.py_tmp → *.py) then reverted on the same day. The scripts remain on disk as `.py_tmp`; the four §2.6.1 conditions remain open as regression-prevention work. The original disabled-block below remains the accurate current description.
>
> **Status (2026-08-23, updated, superseded)** ~~Re-enabled.~~ The `.py_tmp` files added in v0.3.0 were accidentally deleted in commit `0538676` (Phase 2.x side-effect). On 2026-08-23 the user first restored them and renamed back to `.py`, then later the same day reverted to `.py_tmp`. The §2.6.1 adapter refactor / env guard / guard test remain open as **regression-prevention work** — see `docs/todo.md` §2.6.1 for the four pending items. The original 2026-08-23 status block (disabled with `.py_tmp` park) is preserved below for the historical record.

> **Status (post-0.3.0, 2026-08-23, superseded)** ~~both scripts above were **temporarily disabled** in commit `1cac74b` — the `.py` files were renamed to `.py_tmp` (i.e. `watch_to_remotion.py_tmp`, `watch_to_remotion_smart.py_tmp`) so Python's import system would ignore them. They were kept on disk for re-enable once the adapter refactor in `docs/todo.md` §2.6 was complete. Nothing in `SKILL.md` or `watch.py` invoked them while disabled.~~ **Superseded** by the updated status above — the files were accidentally deleted in commit `0538676`, briefly restored as `.py` in `73c92da`, then re-disabled as `.py_tmp` on 2026-08-23.

## [0.2.0] — 2026-06-29

### Added
- **`--detail` dial** with four modes — `transcript` (captions only, no frames), `efficient` (fast keyframe pass, cap 50), `balanced` (scene-aware, cap 100, default), and `token-burner` (scene-aware, uncapped). Set the default with `WATCH_DETAIL` in `~/.config/watch/.env`.
- **Frame deduplication** (default on; `--no-dedup` to disable). Before the budget cap, a pass downscales each frame to a 16×16 grayscale thumbnail and drops frames whose mean per-pixel difference from the last *kept* frame is within threshold — so the budget goes to distinct content instead of held slides and static recordings. The **Frames** report line shows how many near-duplicates were dropped.
- **Whisper auto-chunking.** Audio over the 25 MB upload cap is split into evenly sized chunks, transcribed per chunk, with segment timestamps shifted back into source time. Partial failures are tolerated — transcription only fails if *every* chunk fails, so length alone no longer breaks it.
- **`--timestamps T1,T2,…`** — grab a frame at each absolute timestamp; reserved against the cap, and the only frames produced under `--detail transcript`.
- **`--no-whisper`** — disable transcription entirely (frames only).
- pytest suite covering config, dedup, download, fixtures, frames, setup, timestamps, watch, and whisper (no network; ffmpeg-synthesized clips).

### Changed
- **Restructured into a self-contained `skills/watch/` package** so `SKILL.md` and its `scripts/` runtime are siblings in one folder. This fixes installs on Codex, Cursor, Copilot, and other Agent Skills hosts: `npx skills add` now copies the skill as a working unit instead of grabbing the root `SKILL.md` without its scripts.
- **Harness-agnostic path resolution** — `SKILL.md` resolves `$SKILL_DIR` from where it was Read instead of the Claude-Code-only `${CLAUDE_SKILL_DIR}`, so script calls work on every host.
- `/watch` is now derived from `SKILL.md` frontmatter; the separate `commands/watch.md` wrapper was dropped to avoid a duplicate slash command.
- `balanced` now full-decodes to detect every scene cut across the whole video. The previous early-exit was faster but kept only the first cuts and dropped the tail of long videos.
- `token-burner` is exempt from the long-video "sparse scan" warning, since it keeps every scene-change frame.
- `--max-frames` is now an override on top of each mode's default cap, rather than a fixed default of 80.

### Fixed
- Non-Claude installs (`npx skills add`) were dead on arrival — the installer copied `SKILL.md` without the `scripts/` it shells out to. The self-contained package layout resolves this.

### Removed
- `V2_PLAN.md` and `V2_CONCERNS.md` planning docs.

## [0.1.3] — 2026-05-09

### Fixed
- Windows: `video.info.json` is read as UTF-8 (#4). Previously `Path.read_text()` defaulted to cp1252 on Windows and crashed on yt-dlp's UTF-8 output, silently dropping Title/Uploader from the report. Same fix applied to `.env` reads/writes in `whisper.py` and `setup.py`.
- `download.py` now logs info.json parse failures to stderr instead of swallowing them.

### Security
- Hardened subprocess argv against option injection (#2): inserted `--` before the URL in the yt-dlp argv, and tightened `is_url` to reject `-`-prefixed sources and require a non-empty netloc. Resolved video/audio paths to absolute via `Path.resolve()` before passing to `ffmpeg`/`ffprobe`, so a relative path starting with `-` can't be misinterpreted as a flag.

## [0.1.2] — 2026-04-24

### Fixed
- Windows console crash: removed the emoji from the long-video warning in `watch.py`; cp1252 consoles couldn't encode it.
- `setup.py` now prints `winget` / `pip` install commands on Windows instead of "unsupported platform" — matches what the README already promised.

### Changed
- `SKILL.md` notes that on Windows the scripts must be invoked with `python`, not `python3` (the latter is the Microsoft Store stub on Windows).

## [0.1.1] — 2026-04-24

### Fixed
- Added `commands/watch.md` shim so `/watch` is callable when installed as a Claude Code plugin. Without it, the plugin loaded but the skill wasn't exposed as a slash command.
- `scripts/build-skill.sh` now strips `commands/` from the claude.ai `.skill` bundle alongside `hooks/` and `.claude-plugin/`.

## [0.1.0] — 2026-04-24

Initial marketplace release.

### Added
- `/watch <url-or-path> [question]` slash command.
- yt-dlp download with native caption extraction (manual + auto-subs).
- ffmpeg frame extraction with auto-scaled fps (≤2 fps, ≤100 frames, duration-aware budget).
- `--start` / `--end` focused mode with denser frame budget and transcript range filtering.
- Whisper fallback (Groq preferred, OpenAI secondary) for videos without captions.
- `setup.py` preflight: silent `--check`, structured `--json`, and installer that auto-runs `brew install` on macOS.
- Session-start hook that prints a one-line status on first run / partial config.
- `.skill` bundle packaging for claude.ai upload via `scripts/build-skill.sh`.
