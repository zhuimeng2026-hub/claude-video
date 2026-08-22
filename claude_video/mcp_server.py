"""Shim for the bundled `mcp_server.py` so external projects can do
`from claude_video.mcp_server import ...` after adding the repo root
to sys.path.

The real implementation lives in `skills/watch/scripts/mcp_server.py`
(the self-contained skill folder that ships via `npx skills add`).
We re-export everything from it here so external callers don't need
to know the skill's internal layout. See
`docs/openmontage-integration-inputs.md` §6 for the rationale.

Implementation note: a plain symlink would have been simpler, but
`Path(__file__).parent.resolve()` inside the real scripts does not
follow symlinks (only `Path(__file__).resolve()` does), so the
scripts' `SCRIPT_DIR` calculation would land in `claude_video/`
instead of `skills/watch/scripts/`, breaking sibling imports like
`from config import ...`. A stub module that adds the real scripts
dir to `sys.path` then re-imports sidesteps that.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REAL_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
if str(_REAL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REAL_SCRIPTS))

# Re-export everything from the real module. `import *` is fine here —
# the shim's job is to be a transparent alias.
from mcp_server import *  # noqa: F401,F403,E402
from mcp_server import (  # noqa: F401 — explicit re-exports for type checkers
    SESSIONS,
    mcp,
    read_frame,
    read_mask,
    watch,
)
