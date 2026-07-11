#!/usr/bin/env python3
"""Shared /watch configuration helpers."""
from __future__ import annotations

import os
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "watch"
CONFIG_FILE = CONFIG_DIR / ".env"

DEFAULT_DETAIL = "balanced"
DEFAULT_WHISPER_BACKEND = "local"  # local|groq|openai — local uses faster-whisper
DEFAULT_REPLICATE_MODEL = (
    "meta/sam-2-video"
    ":8493227172126379a30e46d88442159788e60f14b7e5a09125f0a32323c67539"
)

DETAILS = {"transcript", "efficient", "balanced", "token-burner"}
WHISPER_BACKENDS = {"local", "groq", "openai"}


def read_env_file(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = CONFIG_FILE
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        else:
            # Strip an inline comment (a '#' preceded by whitespace) from an
            # unquoted value. Without this, `WATCH_DETAIL=balanced  # note`
            # parses as "balanced  # note", fails validation, and silently
            # falls back to the default. Keeps '#' inside quotes / API keys.
            for i, ch in enumerate(value):
                if ch == "#" and i > 0 and value[i - 1] in " \t":
                    value = value[:i].rstrip()
                    break
        values[key.strip()] = value
    return values


def get_config() -> dict[str, object]:
    file_values = read_env_file()

    detail = (
        os.environ.get("WATCH_DETAIL")
        or file_values.get("WATCH_DETAIL")
        or DEFAULT_DETAIL
    )
    if detail not in DETAILS:
        detail = DEFAULT_DETAIL

    whisper_backend = (
        os.environ.get("WHISPER_BACKEND")
        or file_values.get("WHISPER_BACKEND")
        or DEFAULT_WHISPER_BACKEND
    )
    if whisper_backend not in WHISPER_BACKENDS:
        whisper_backend = DEFAULT_WHISPER_BACKEND

    return {
        "detail": detail,
        "whisper_backend": whisper_backend,
        "config_file": str(CONFIG_FILE),
    }


def frame_cap(detail: str) -> int | None:
    if detail == "efficient":
        return 50
    if detail == "balanced":
        return 100
    if detail == "token-burner":
        return None
    if detail == "transcript":
        return None
    return 100


def get_replicate_token() -> str | None:
    """Return the Replicate API token from env or ~/.config/watch/.env."""
    import os as _os

    def _from_env(name: str) -> str | None:
        value = _os.environ.get(name)
        return value.strip() if value else None

    def _from_dotenv(path: Path, name: str) -> str | None:
        if not path.exists():
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                return value or None
        except OSError:
            return None
        return None

    dotenv_paths = [CONFIG_FILE, Path.cwd() / ".env"]
    value = _from_env("REPLICATE_API_TOKEN")
    if value:
        return value
    for p in dotenv_paths:
        value = _from_dotenv(p, "REPLICATE_API_TOKEN")
        if value:
            return value
    return None
