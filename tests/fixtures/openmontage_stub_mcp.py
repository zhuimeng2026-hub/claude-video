#!/usr/bin/env python3
"""Minimal OpenMontage_Voicebox MCP server stub for end-to-end testing.

Phase 3.4 — used by tests/test_recompose_real_om.py. Implements
just enough MCP surface for the claude-video `recompose` flow:

  - initialize / list_tools / call_tool JSON-RPC over stdio
  - one tool: `claude_video.compose(inputs: dict) -> dict`
  - echoes inputs back (plus a synthetic project_id) so the test
    can verify the inputs package that arrived at the OpenMontage
    side

This stub is NOT a real OpenMontage MCP server — it's the
"testing seam" Phase 3.4 introduces because the real
OpenMontage_Voicebox/mcp_server.py has the same bare-list
Pydantic crash we hit on claude-video (Phase 1.5 pitfall A in
docs/MCP_SERVER_PRD.md §6.1). Once OpenMontage owner fixes that,
the real binary replaces this stub — the recompose test only
needs OPENMONTAGE_BIN pointed at the right script.

The stub lives in tests/ (not skills/) because it's test-only.
Phase 3.4 production path uses the real OpenMontage MCP once it's
working in this environment; the test path uses this stub until then.
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("openmontage-stub")


@mcp.tool(
    name="claude_video.compose",
    description=(
        "OpenMontage stub: receives inputs from claude-video's "
        "recompose tool, echoes them back with a synthetic "
        "project_id. Production OpenMontage runs the actual "
        "pipeline."
    ),
)
async def claude_video_compose(inputs: dict) -> dict:
    """Test seam: round-trip inputs back so the test can assert
    on what the OpenMontage side received."""
    project_id = inputs.get("project_id") or "stub-project-abc"
    return {
        "project_id": project_id,
        "status": "submitted",
        "render_url": f"https://stub.openmontage.local/{project_id}/renders/final.mp4",
        "echoed_inputs": inputs,
    }


if __name__ == "__main__":
    mcp.run()
