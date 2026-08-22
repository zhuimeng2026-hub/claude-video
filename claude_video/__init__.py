"""claude-video shim package.

Lives at the repo root so external projects (notably OpenMontage's
`tools/external/claude_video.py` adapter and its integration tests) can
do `from claude_video.mcp_server import ...` after adding the repo root
to sys.path. See `docs/openmontage-integration-inputs.md` §6 for the
rationale — the project's primary distribution form is a `.skill`
bundle (not pip), so we expose a thin importable surface rather than
adding a full pyproject.

The actual code lives in `skills/watch/scripts/` (the self-contained
skill folder that ships via `npx skills add`). The modules here are
symlinks; importing through them produces the same module objects
because Python resolves symlinks during import.

What is NOT here (yet, intentionally):
  - A `run_watch` top-level function. `mcp_server.py` currently exposes
    `watch` (the @mcp.tool) and `read_frame` / `read_mask` resources;
    a `run_watch(source, **kwargs) -> RunResult` helper will land as
    part of Phase 2.1 (see `docs/todo.md` §2.1). Until then, callers
    needing a structured RunResult should invoke the `watch` tool
    inside an MCP session and read `result["report"]` + the
    `frame_uris` list.
  - A `RunResult` pydantic model. Currently the result is a plain dict
    returned by the `watch` tool. A typed model lands alongside
    `run_watch` in Phase 2.1; the JSON shape is documented in
    `docs/MCP_SERVER_PRD.md` §2.6 (proposed) and the canonical example
    lives in `tests/fixtures/sample_runresult.json`.
"""
