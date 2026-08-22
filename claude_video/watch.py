"""Shim for the bundled `watch.py` so external projects can do
`from claude_video.watch import run, RunResult`. Same pattern as
`claude_video.mcp_server` — the real implementation lives in
`skills/watch/scripts/watch.py`. See
`docs/openmontage-integration-inputs.md` §6 for the rationale.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REAL_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
if str(_REAL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REAL_SCRIPTS))

from watch import *  # noqa: F401,F403,E402
from watch import main, run  # noqa: F401 — explicit re-exports for type checkers
