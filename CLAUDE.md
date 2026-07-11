# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the **claude-video** project — an Agent Skills package that provides the `/watch` slash command, giving AI agents the ability to analyze videos. It installs across Claude Code, Codex, Cursor, GitHub Copilot, and 50+ other Agent Skills hosts. The product is the skill itself (`/watch <url-or-path> [question]`), not a standalone CLI.

## Development Commands

```bash
# Run the test suite (requires ffmpeg; uses synthesized clips, no network)
.venv/bin/pytest -q
# or: python3 -m pytest -q

# Run a single test file
.venv/bin/pytest tests/test_frames.py -q

# Build the claude.ai upload bundle (archives skills/watch/ as root)
bash skills/watch/scripts/build-skill.sh   # → dist/watch.skill

# Sync local edits into installed Claude Code plugin cache (no publish needed)
./dev-sync.sh                  # --dry-run to preview without writing
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

Test files cover: config, dedup, download, fixtures, frames, setup, timestamps, watch (e2e routing), whisper.

## Install Surfaces

| Surface | Install |
|---------|---------|
| Claude Code | `/plugin marketplace add bradautomates/claude-video` then `/plugin install watch@claude-video` |
| Codex / Cursor / Copilot / +50 | `npx skills add bradautomates/claude-video -g` |
| claude.ai (web) | upload `dist/watch.skill` (built by `skills/watch/scripts/build-skill.sh`) |
