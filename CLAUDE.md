# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the **claude-video** project — an Agent Skills package that provides the `/watch` slash command, giving AI agents the ability to analyze videos. It installs across Claude Code, Codex, Cursor, GitHub Copilot, and 50+ other Agent Skills hosts. The product is the skill itself (`/watch <url-or-path> [question]`), not a standalone CLI.

## Development Commands

```bash
# Run the full test suite (ffmpeg required; synthesized clips, no network)
.venv/bin/pytest -q
# or: python3 -m pytest -q

# Run a single test file
.venv/bin/pytest tests/test_frames.py -q
.venv/bin/pytest tests/test_mcp_server.py -q    # newest: MCP server surface

# Run a single test by name (-k matches test function / class)
.venv/bin/pytest -k "test_dedup_drops_near_duplicates" -q
.venv/bin/pytest tests/test_bff.py -q          # BFF route + slot-pool tests
.venv/bin/pytest tests/test_recompose_real_om.py -q   # OpenMontage stdio MCP e2e

# Run the BFF in dev (FastAPI on :8910 by default; spawns mcp_server.py subprocess)
WATCH_BFF_PORT=8910 python3 skills/watch/scripts/bff.py

# Cron cleanup (idempotent, exit-0 always; recommended hourly)
python3 skills/watch/scripts/cleanup.py --quiet

# Build the claude.ai upload bundle (archives skills/watch/ as the bundle root)
bash skills/watch/scripts/build-skill.sh   # → dist/watch.skill

# Sync local edits into installed Claude Code plugin cache (no publish needed)
./dev-sync.sh                  # --dry-run to preview; resolves install path from
                              # ~/.claude/plugins/installed_plugins.json (follows
                              # version bumps). Excludes tests/, docs/, dist/,
                              # and the cache's .in_use/ runtime state.
```

## Architecture

The skill is a self-contained folder: `skills/watch/`. Everything the skill needs lives there — `SKILL.md` (the contract) and `scripts/` (the runtime). This is what lets `npx skills add` copy a working skill as a unit.

**Top-level flow:** `SKILL.md` (slash-command contract read by every Agent Skills host) → `scripts/watch.py` (single-call pipeline entry, stdlib only) orchestrates `download.py` (yt-dlp) → `frames.py` (ffmpeg extraction) → `transcribe.py` (VTT parsing + Whisper) → prints frame paths + transcript to stdout → the agent `Read`s each frame as an image.

**MCP / BFF layer (v0.4.0+, used by browser and external clients):** `scripts/mcp_server.py` (stdio MCP, 8 tools + 2 resources) is the single source of truth for tool calls. Browser clients go through `scripts/bff.py` (FastAPI, REST + SSE, default port 8910) which holds a **pool** of stdio MCP subprocesses (default 4 slots, `WATCH_BFF_POOL_SIZE=N`). Same `video_id` always lands on the same slot so the cache-affinity contract is preserved. The MCP server is excluded from the claude.ai `.skill` bundle by `skills/watch/.skillignore` (the sandboxed runtime can't host stdio MCP); the BFF is launchable on a host with Python.

**Persistent state (v0.4.0+):**
- `~/.cache/watch-mcp/sessions.json` (mode 0600) — `session_store.SessionRecord` registry, atomic temp+os.replace writes under `fcntl.flock`. `video_id` is the persistent cache key (sha256(source)[:12] by default; caller-overridable).
- `~/.cache/watch-mcp/users.sqlite3` (WAL mode) — `users_store` backs the WeChat service-account OAuth flow (`users` / `sessions` / `oauth_states` tables).
- OAuth CSRF state stored as SHA-256 hash (plaintext state never persisted). One-time `consume_oauth_state()` runs lookup+delete in one transaction.

**Recompose / OpenMontage (Phase 2.6):** `scripts/openmontage_client.py` is a thin stdio MCP client that spawns `/opt/OpenMontage_Voicebox/mcp_server.py` (override via `OPENMONTAGE_BIN`) and submits `{video_id, user_openid, frames_dir, masks_dir, vtt_path, video_path, pipeline, style}` to OpenMontage's `claude_video.compose` tool. GPU-required pipelines are rejected **before** subprocess spawn (Pydantic `Literal[...]` whitelist + runtime keyword detection). Cross-repo spec at `OpenMontage_Voicebox/docs/claude-video-integration.md`.

**Ops (v0.5.0):** `cleanup.py` is a cron-driven GC that ages out `done` sessions >30 days and `error`/`cancelled` >7 days. `running` sessions are NEVER GC'd. `bff_metrics.py` is a hand-rolled Prometheus 0.0.4 exposition format (no `prometheus_client` dependency) exposed at `GET /metrics`. Counters: `watch_bff_requests_total{tool,status}`; gauges: `watch_bff_mcp_connected`, `watch_bff_active_watches`, `watch_bff_pool_size_used`; histogram: `watch_bff_tool_duration_seconds{tool}`.

**Key scripts:**
- `skills/watch/scripts/watch.py` — single-call pipeline entry; parses args, coordinates download→frames→transcript, prints markdown report
- `skills/watch/scripts/download.py` — yt-dlp wrapper for URLs and local files
- `skills/watch/scripts/frames.py` — ffmpeg frame extraction with auto-fps, scene/keyframe detection, dedup (16×16 grayscale → mean absolute diff against last *kept* frame, threshold 2.0)
- `skills/watch/scripts/transcribe.py` — VTT subtitle parsing and Whisper orchestration
- `skills/watch/scripts/whisper.py` — Groq and OpenAI Whisper API clients (pure stdlib, no deps)
- `skills/watch/scripts/segment.py` — SAM 2 video segmentation via Replicate (`--segment` flag)
- `skills/watch/scripts/mcp_server.py` — stdio MCP server: 8 tools (`watch`, `start_watch`, `get_status`, `get_results`, `cancel_watch`, `list_sessions`, `delete_session`, `recompose`) + 2 resources (`read_frame`, `read_mask`). Each frame registered as `watch-frame://<sid>/frames/<file>` (image/jpeg) and mask as `watch-frame://<sid>/masks/<file>` (image/png). Path-traversal defence via `Path.is_relative_to(work_dir)`. See [`docs/MCP_SERVER_PRD.md`](docs/MCP_SERVER_PRD.md) for the full interface contract and [`docs/MCP_CLIENT_COMPAT.md`](docs/MCP_CLIENT_COMPAT.md) for the verified-host matrix.
- `skills/watch/scripts/bff.py` — FastAPI BFF. 10 `/api/*` routes (`start`, `status`, `cancel`, `results`, `frame/{filename}`, `mask/{filename}`, `events` SSE, `sessions`, `sessions/{video_id}/delete`, `recompose`) + 4 `/auth/*` routes (`wechat/login`, `wechat/callback`, `logout`, `me`) + ops (`/healthz`, `/readyz`, `/metrics`). Pool of N `_Slot` objects, video_id→slot routing preserves cache affinity. See [`docs/BFF_API_CONTRACT.md`](docs/BFF_API_CONTRACT.md).
- `skills/watch/scripts/session_store.py` — disk-backed JSON session registry (`SessionRecord` dataclass, `fcntl.flock` inter-process mutex, atomic temp+os.replace)
- `skills/watch/scripts/pipeline_runner.py` — background `threading.Thread` per running job, in-memory registry indexed by `video_id`, `cancel_event` checked between stage boundaries, `threading.Timer` watchdog for `timeout_seconds`. Distinguishable cancel vs timeout error strings.
- `skills/watch/scripts/openmontage_client.py` — stdio MCP client to OpenMontage_Voicebox; GPU-free pipeline whitelist
- `skills/watch/scripts/users_store.py` + `wechat_oauth.py` — SQLite-backed WeChat service-account OAuth (Phase 2.8); `users.sessions` has 7-day sliding-window expiry, `oauth_states` 10-minute
- `skills/watch/scripts/sse_progress.py` — standalone SSE daemon (`WATCH_SSE_PORT=8911`, polls `session_store.json`); used by LAN clients or proxied through the BFF
- `skills/watch/scripts/bff_metrics.py` — hand-rolled Prometheus 0.0.4 exposition (no `prometheus_client` dep)
- `skills/watch/scripts/cleanup.py` — Phase 3.1 cron entrypoint; `--quiet` mode for systemd timers / k8s CronJob
- `skills/watch/scripts/watch_to_remotion.py` / `watch_to_remotion_smart.py` — `/watch` → Remotion 4.x converters. **Currently disabled**: kept on disk as `.py_tmp` suffix so Python's import system won't pick them up. Re-enable path is `mv *.py_tmp *.py` once §2.6.1 conditions land.
- `skills/watch/scripts/config.py` — shared config from `~/.config/watch/.env`
- `skills/watch/scripts/setup.py` — preflight check and first-run installer

**BFF env knobs:** `WATCH_BFF_PORT` (default 8910), `WATCH_BFF_CORS_ORIGINS` (JSON list), `WATCH_BFF_AUTH_TOKEN` (Bearer-token fallback for inter-service auth), `WATCH_BFF_POOL_SIZE` (default 4), `WECHAT_MP_APP_ID` / `WECHAT_MP_APP_SECRET` / `WECHAT_MP_REDIRECT_URI` / `WECHAT_MP_SCOPE` (enable WeChat OAuth; unset → `/auth/*` returns 503, `require_user` falls back to env-token / dev-bypass).

**Standalone package mirror:** `claude_video/` is a parallel top-level copy of the same source (entry `claude_video/watch.py`, `claude_video/mcp_server.py`) — keep in sync with `skills/watch/scripts/` if you touch the canonical scripts.

**Config location:** `~/.config/watch/.env` (mode `0600`) — stores API keys and `WATCH_DETAIL` default. Never commit real keys.

## Critical Rules

**Path resolution is harness-agnostic.** `SKILL.md` resolves `SKILL_DIR` as the directory containing the SKILL.md the model just Read, then runs `${SKILL_DIR}/scripts/...`. Do NOT use `${CLAUDE_SKILL_DIR}` (Claude-Code-only) — it is unset on Codex/Cursor/agents and breaks every script call there.

**The skill folder is the unit of distribution.** `skills/watch/SKILL.md` and `skills/watch/scripts/` are siblings. Do NOT move them back to the repo root — non-Claude installers will copy `SKILL.md` without its scripts.

**Version must stay in sync** across four files when cutting a release:
- `skills/watch/SKILL.md` frontmatter (`version:`)
- `.claude-plugin/plugin.json` (`version`)
- `.codex-plugin/plugin.json` (`version`)
- `requirements.txt` — `mcp>=1.20,<2.0` SDK pin matches the server (see [`docs/MCP_CLIENT_COMPAT.md`](docs/MCP_CLIENT_COMPAT.md) §1 for why this exact range)

**Releasing:** tag `vX.Y.Z` and push. `.github/workflows/release.yml` builds `dist/watch.skill` and attaches it to the GitHub release.

**The BFF is the trust boundary for OAuth.** OpenMontage treats `user_openid` as untrusted input — it uses the string to compute filesystem paths and nothing else. The guarantee that user A cannot write into user B's directory depends entirely on the BFF resolving `WATCH_SESSION` cookie → openid and forwarding that exact value. The BFF MUST silently override any `user_openid` parameter sent by the browser. The MCP tool layer adds a second line of defense: it checks `video_id.user_openid` matches the caller's `user_openid`. See [`docs/OAUTH_TRUST_MODEL.md`](docs/OAUTH_TRUST_MODEL.md) — full contract.

**stdio MCP is stateful and single-threaded per subprocess.** Concurrent `tools/call` requests against one MCP subprocess MUST be serialized through an `asyncio.Lock`; parallel writes would corrupt the JSON-RPC stream. The v0.5.0 `_Slot` pool sidesteps this by routing each `video_id` to one slot — cache affinity preserves the v0.4.0 contract.

**BFF ops endpoints.** `/healthz` = liveness (any slot connected), `/readyz` = readiness (ALL slots + MCP handshake OK), `/metrics` = Prometheus 0.0.4 exposition. Use `/readyz` for k8s readiness / LB health checks.

**Cursor/Copilot rules.** There are no `.cursorrules`, `.cursor/rules/`, or `.github/copilot-instructions.md` in this repo — nothing to merge.

## Key Documents

- [`docs/MCP_SERVER_PRD.md`](docs/MCP_SERVER_PRD.md) — full MCP interface contract, JSON shapes, gotchas (incl. §6.1 Pitfall A: bare-bytes crash with `mcp<1.20`)
- [`docs/MCP_CLIENT_COMPAT.md`](docs/MCP_CLIENT_COMPAT.md) — verified-host matrix for Claude Desktop / Zed / Continue / openclaw / MCP Inspector / custom stdio clients
- [`docs/BFF_API_CONTRACT.md`](docs/BFF_API_CONTRACT.md) — every REST + SSE endpoint pinned (cross-repo contract with OpenMontage)
- [`docs/OAUTH_TRUST_MODEL.md`](docs/OAUTH_TRUST_MODEL.md) — what BFF vs OpenMontage each verify
- [`docs/todo.md`](docs/todo.md) — phase tracker; Phases 1–3 done through v0.5.0
- [`CHANGELOG.md`](CHANGELOG.md) — per-release notes
- [`AGENTS.md`](AGENTS.md) — generic-agent entry point; links to CLAUDE.md via `@AGENTS.md`

## Testing

Tests are in `tests/` and use pytest. They synthesize test clips with ffmpeg (solid-color segments with hard cuts) — no network calls, no real videos. Key fixtures in `tests/conftest.py`:
- `cut_clip` — 14-color segment clip (one keyframe per cut, exercises keyframe and scene detection)
- `static_clip` — single-color clip (triggers fallback to uniform sampling)

Test files cover: config, dedup, download, fixtures, frames, mcp_server (newest), setup, timestamps, watch (e2e routing), whisper.

## Install Surfaces

See [`README.md`](README.md#install) for full install commands. Quick reference: Claude Code marketplace (`/plugin marketplace add …` then `/plugin install watch@claude-video`), `npx skills add bradautomates/claude-video -g` for Codex / Cursor / Copilot / 50+ Agent Skills hosts, and `dist/watch.skill` upload for claude.ai web.
