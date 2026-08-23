# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the **claude-video** project — an Agent Skills package that provides the `/watch` slash command, giving AI agents the ability to analyze videos. It installs across Claude Code, Codex, Cursor, GitHub Copilot, and 50+ other Agent Skills hosts. The product is the skill itself (`/watch <url-or-path> [question]`), not a standalone CLI.

## Development Commands

```bash
# Run the test suite (requires ffmpeg; uses synthesized clips, no network)
.venv/bin/pytest -q
# or: python3 -m pytest -q

# Run a single test file (also useful: test_mcp_server.py — newest module)
.venv/bin/pytest tests/test_frames.py -q

# Run a single test by name (-k matches test function / class name)
.venv/bin/pytest -k "test_dedup_drops_near_duplicates" -q

# Build the claude.ai upload bundle (archives skills/watch/ as root)
bash skills/watch/scripts/build-skill.sh   # → dist/watch.skill

# Sync local edits into installed Claude Code plugin cache (no publish needed)
./dev-sync.sh                  # --dry-run to preview; resolves install path from
                              # ~/.claude/plugins/installed_plugins.json (follows
                              # version bumps). Excludes tests/, docs/, dist/, and
                              # the cache's .in_use/ runtime state. No --delete.
```

## Architecture

The skill is a self-contained folder: `skills/watch/`. Everything the skill needs lives there — `SKILL.md` (the contract) and `scripts/` (the runtime). This is what lets `npx skills add` copy a working skill as a unit.

**Execution flow:** `SKILL.md` → `scripts/watch.py` (entry point) → orchestrates `download.py` (yt-dlp) → `frames.py` (ffmpeg extraction) → `transcribe.py` (VTT parsing + Whisper) → prints frame paths + transcript to stdout → the agent `Read`s each frame as an image.

**Key scripts:**
- `skills/watch/scripts/watch.py` — entry point; parses args, coordinates the pipeline, prints the markdown report
- `skills/watch/scripts/download.py` — yt-dlp wrapper for URLs and local files
- `skills/watch/scripts/frames.py` — ffmpeg frame extraction with auto-fps, scene/keyframe detection, and dedup
- `skills/watch/scripts/transcribe.py` — VTT subtitle parsing and Whisper orchestration
- `skills/watch/scripts/whisper.py` — Groq and OpenAI Whisper API clients (pure stdlib, no deps)
- `skills/watch/scripts/segment.py` — SAM 2 video segmentation via Replicate (`--segment` flag); uploads video, polls predictions, downloads mask frames
- `skills/watch/scripts/mcp_server.py` — stdio MCP server wrapping the same CLI flags as `watch.py`; exposes the `watch` tool and registers each extracted frame as a `watch-frame://<sid>/frames/<file>` resource (image/jpeg) and mask as `watch-frame://<sid>/masks/<file>` (image/png). Path-traversal defence via `Path.is_relative_to(work_dir)`. **Excluded from the claude.ai `.skill` bundle** by `skills/watch/.skillignore` (sandboxed runtime can't host stdio MCP). Full interface contract in [`docs/MCP_SERVER_PRD.md`](docs/MCP_SERVER_PRD.md).
- `skills/watch/scripts/watch_to_remotion.py` / `watch_to_remotion_smart.py` — `/watch` → Remotion 4.x converters (deterministic + LLM-driven variants). **Currently disabled**: the files are kept on disk with a `.py_tmp` suffix so Python's import system won't pick them up. Do not import or call them. (Briefly re-enabled in commit `73c92da`, reverted to disabled on 2026-08-23 — the `mv *.py_tmp *.py` rename remains the re-enable path once the §2.6.1 adapter refactor lands.)
- `skills/watch/scripts/config.py` — shared config from `~/.config/watch/.env`
- `skills/watch/scripts/setup.py` — preflight check and first-run installer

**Config location:** `~/.config/watch/.env` (mode `0600`) — stores API keys and `WATCH_DETAIL` default. Never commit real keys.

## Critical Rules

**Path resolution is harness-agnostic.** `SKILL.md` resolves `SKILL_DIR` as the directory containing the SKILL.md the model just Read, then runs `${SKILL_DIR}/scripts/...`. Do NOT use `${CLAUDE_SKILL_DIR}` (Claude-Code-only) — it is unset on Codex/Cursor/agents and breaks every script call there.

**The skill folder is the unit of distribution.** `skills/watch/SKILL.md` and `skills/watch/scripts/` are siblings. Do NOT move them back to the repo root — non-Claude installers will copy `SKILL.md` without its scripts.

**Version must stay in sync** across three files when cutting a release:
- `skills/watch/SKILL.md` frontmatter (`version:`)
- `.claude-plugin/plugin.json` (`version`)
- `.codex-plugin/plugin.json` (`version`)

**Releasing:** tag `vX.Y.Z` and push. `.github/workflows/release.yml` builds `dist/watch.skill` and attaches it to the GitHub release.

## Testing

Tests are in `tests/` and use pytest. They synthesize test clips with ffmpeg (solid-color segments with hard cuts) — no network calls, no real videos. Key fixtures in `tests/conftest.py`:
- `cut_clip` — 14-color segment clip (one keyframe per cut, exercises keyframe and scene detection)
- `static_clip` — single-color clip (triggers fallback to uniform sampling)

Test files cover: config, dedup, download, fixtures, frames, mcp_server (newest), setup, timestamps, watch (e2e routing), whisper.

## Install Surfaces

See [`README.md`](README.md#install) for full install commands. Quick reference: Claude Code marketplace (`/plugin marketplace add …` then `/plugin install watch@claude-video`), `npx skills add bradautomates/claude-video -g` for Codex / Cursor / Copilot / 50+ Agent Skills hosts, and `dist/watch.skill` upload for claude.ai web.
