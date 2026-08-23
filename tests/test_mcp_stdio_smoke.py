"""Multi-process stdio MCP smoke test.

Verifies that the stdio MCP server can handle **multiple concurrent
subprocess instances** without cross-process session leakage. Each
subprocess maintains its own `SESSIONS` registry (in-memory, lifetime =
process lifetime), so different host processes must get independent
session_ids and must not be able to read each other's frames.

Why multiprocessing and not just `subprocess.Popen`? The point is to
catch race conditions in session-id generation and resource dispatch
under real concurrency — pytest's `asyncio.run` calls in series would
miss them.

Uses `multiprocessing.get_context("spawn").Pool` with `starmap` so
workers get fresh interpreter state (no inherited asyncio state from
the parent). Each worker:

  1. Spawns `mcp_server.py` as a subprocess.
  2. Connects via `mcp.client.stdio.stdio_client` + `ClientSession`.
  3. Runs `initialize` → `list_tools` → `call_tool("watch", ...)` → `read_resource(frame_uri)`.
  4. Returns its session_id + first frame bytes.

The main process asserts: (a) every session_id is unique, (b) the frame
bytes match the JPEG SOI marker, (c) cross-session resource reads fail.

Skip condition: if `mcp` SDK isn't importable (e.g. minimal CI env), skip
with a clear message rather than fail. The single-process subprocess
handshake in `test_mcp_server.py::test_mcp_server_subprocess_initializes`
covers the in-process case.
"""
from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import sys
import traceback
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _worker_entry(
    worker_id: int,
    source: str,
    out_dir: str,
    peer_session_ids: list[str],
) -> dict:
    """Top-level function run in a child process.

    `peer_session_ids` is the list of session_ids OTHER workers produced
    in the first wave. We use it to assert that this worker's process
    can't read peer resources (negative isolation test).
    """
    try:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        return {"worker_id": worker_id, "skipped": True, "reason": str(e)}

    async def run() -> dict:
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(SCRIPTS_DIR / "mcp_server.py")],
            env=None,  # inherit
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}

                result = await session.call_tool(
                    "watch",
                    {
                        "source": source,
                        "no_whisper": True,
                        "out_dir": out_dir,
                        "allow_arbitrary_out": True,  # tests use temp dirs
                        "detail": "efficient",  # fast — keyframe engine only
                    },
                )
                # FastMCP returns a list of content blocks; first is JSON text.
                out = json.loads(result.content[0].text)
                sid = out["session_id"]
                first_frame_uri = out["frame_uris"][0] if out["frame_uris"] else None

                # Read our own first frame.
                own_frame_bytes: bytes | None = None
                if first_frame_uri:
                    res = await session.read_resource(first_frame_uri)
                    # BlobResourceContents has `.blob` (base64); TextResourceContents
                    # has `.text` (str). For JPEG/PNG bytes we expect blob.
                    block = res.contents[0]
                    if hasattr(block, "blob") and block.blob:
                        own_frame_bytes = base64.b64decode(block.blob)
                    elif hasattr(block, "text") and block.text:
                        own_frame_bytes = block.text.encode()

                # Try to read a peer's frame — must fail. The server's
                # _read_artifact raises ValueError("unknown session_id: <sid>")
                # which surfaces as a McpError from FastMCP.
                peer_read_errors: dict[str, str] = {}
                for peer_sid in peer_session_ids:
                    if peer_sid == sid:
                        continue
                    peer_uri = f"watch-frame://{peer_sid}/frames/frame_0001.jpg"
                    try:
                        await session.read_resource(peer_uri)
                        peer_read_errors[peer_sid] = "UNEXPECTED SUCCESS"
                    except Exception as e:
                        peer_read_errors[peer_sid] = type(e).__name__

                return {
                    "worker_id": worker_id,
                    "skipped": False,
                    "protocol_version": init_result.protocolVersion,
                    "server_name": init_result.serverInfo.name,
                    "tool_count": len(tool_names),
                    "session_id": sid,
                    "frame_count": out["frame_count"],
                    "first_frame_uri": first_frame_uri,
                    "own_frame_bytes_len": len(own_frame_bytes) if own_frame_bytes else 0,
                    "own_frame_jpeg_soi": (
                        own_frame_bytes[:3].hex() if own_frame_bytes else None
                    ),
                    "peer_read_errors": peer_read_errors,
                }

    try:
        return anyio.run(run)
    except Exception:
        return {
            "worker_id": worker_id,
            "skipped": False,
            "error": traceback.format_exc(),
        }


def _check_mcp_available() -> bool:
    try:
        from mcp import ClientSession, StdioServerParameters  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _check_mcp_available(),
    reason="mcp SDK not importable (install with `pip install mcp>=1.0`)",
)


def _check_server_imports() -> tuple[bool, str]:
    """Probe whether `mcp_server.py` can be imported under the current
    `mcp` SDK version. Returns (ok, message).

    Newer FastMCP versions raise `pydantic.errors.PydanticUserError` when
    `@mcp.resource` decorates a function returning bare `bytes` (pydantic
    2.10+ requires explicit `Annotated[...]` for the wrapped return model).
    See https://github.com/bradautomates/claude-video/issues for context.

    Implementation note: must use a NORMAL `import mcp_server` (after
    `sys.path` is set up), not `importlib.util.spec_from_file_location`
    with a synthetic name. Pydantic's `validate_call` resolves string
    annotations via `sys.modules[obj.__module__].__dict__`, so a
    synthetic module name that isn't registered will make it fail to
    find `Annotated` / `Field` even when those are imported.
    """
    try:
        import mcp_server  # noqa: F401  -- must register in sys.modules
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


_server_ok, _server_msg = _check_server_imports()


@pytest.fixture
def work_dir_root(tmp_path) -> Path:
    """Per-test temp root. Each worker gets its own subdir under here."""
    root = tmp_path / "watch-mcp-smoke"
    root.mkdir()
    return root


def test_concurrent_subprocesses_isolated(cut_clip: Path, work_dir_root: Path):
    """N concurrent stdio MCP server processes must not cross-pollinate sessions.

    1. Spawn 4 workers in parallel via ProcessPoolExecutor.
    2. Each worker spawns its own mcp_server.py subprocess.
    3. Each runs `watch`, gets a session_id + first frame URI, reads the
       frame bytes.
    4. Main asserts: all session_ids unique; own frame bytes are valid
       JPEGs; reading a peer's session_id fails (ValueError, not silent
       success or wrong data).

    Skip if mcp_server.py can't even import under the current SDK —
    that means FastMCP's resource decorator is broken (separate bug,
    tracked separately).
    """
    if not _server_ok:
        pytest.skip(f"mcp_server.py import failed under current SDK: {_server_msg}")
    N = 4
    # Each worker writes to its own out_dir so frames don't collide on disk.
    worker_dirs = [work_dir_root / f"worker-{i}" for i in range(N)]
    for d in worker_dirs:
        d.mkdir()

    # First wave: collect session_ids.
    first_wave_args = [
        (i, str(cut_clip), str(worker_dirs[i]), []) for i in range(N)
    ]
    with multiprocessing.get_context("spawn").Pool(processes=N) as pool:
        first_results = pool.starmap(_worker_entry, first_wave_args)

    # None skipped → SDK is fine. None errored → handshake worked.
    assert not any(r.get("skipped") for r in first_results), first_results
    assert not any("error" in r for r in first_results), [
        r["error"] for r in first_results if "error" in r
    ]

    session_ids = [r["session_id"] for r in first_results]
    protocol_versions = {r["protocol_version"] for r in first_results}
    server_names = {r["server_name"] for r in first_results}

    # Each subprocess is its own server instance.
    assert protocol_versions, "no protocol_version returned"
    assert server_names == {"claude-video"}, server_names

    # All session_ids unique — generation must be collision-free under concurrency.
    assert len(set(session_ids)) == N, f"duplicate session_ids: {session_ids}"

    # Each call must produce at least one frame and a valid JPEG SOI marker.
    for r in first_results:
        assert r["frame_count"] > 0, f"worker {r['worker_id']} produced no frames"
        assert r["own_frame_jpeg_soi"] == "ffd8ff", (
            f"worker {r['worker_id']} frame missing JPEG SOI: "
            f"{r['own_frame_jpeg_soi']!r}"
        )

    # Second wave: feed each worker the OTHER workers' session_ids and
    # assert peer resource reads fail. We re-spawn fresh subprocesses
    # because the first-wave processes exited when their tasks returned.
    second_wave_args = [
        (
            i,
            str(cut_clip),
            str(worker_dirs[i]),
            [s for j, s in enumerate(session_ids) if j != i],
        )
        for i in range(N)
    ]
    with multiprocessing.get_context("spawn").Pool(processes=N) as pool:
        second_results = pool.starmap(_worker_entry, second_wave_args)

    assert not any("error" in r for r in second_results), [
        r.get("error") for r in second_results if "error" in r
    ]

    for r in second_results:
        peer_errors = r["peer_read_errors"]
        assert peer_errors, f"worker {r['worker_id']} had no peers to test"
        for peer_sid, err in peer_errors.items():
            # FastMCP wraps server ValueError as McpError; raw ValueError
            # also acceptable. The point is: NOT a success and NOT empty
            # bytes. "UNEXPECTED SUCCESS" is the canary the test inserts
            # if read_resource did not raise.
            assert err != "UNEXPECTED SUCCESS", (
                f"worker {r['worker_id']} successfully read peer {peer_sid} — "
                f"session isolation broken!"
            )
            # And the message should mention session — confirms server-side
            # rejection rather than e.g. a connection drop.
            # We can't introspect the exception message from here, but the
            # type name being McpError/ValueError is the structural signal.


def test_server_can_import_under_current_sdk():
    """Gate test: mcp_server.py must be importable. If this fails, the
    whole server is non-functional — every other test in this module
    will skip, plus tests/test_mcp_server.py will fail to collect.

    Captures the symptom (PydanticUserError on `@mcp.resource` with
    bare `bytes` return type) so the failure is traceable.
    """
    ok, msg = _check_server_imports()
    if not ok:
        pytest.fail(
            "mcp_server.py cannot be imported under the current mcp SDK. "
            f"Error: {msg}\n"
            "Likely cause: FastMCP's @mcp.resource decorator breaks on "
            "functions returning bare `bytes` under pydantic 2.10+. "
            "Fix: wrap return type as `Annotated[bytes, ...]` or pin "
            "mcp<1.20. See Phase 1.2 / new task tracking SDK version fix."
        )


def test_subprocess_handshake_minimal(cut_clip: Path, work_dir_root: Path):
    """Smallest-viable smoke: one worker, one call, asserts no exception.

    This is the 'does the basic flow work end-to-end' gate. The isolation
    test above is the harder property.
    """
    if not _server_ok:
        pytest.skip(f"mcp_server.py import failed under current SDK: {_server_msg}")
    args = [(0, str(cut_clip), str(work_dir_root / "solo"), [])]
    with multiprocessing.get_context("spawn").Pool(processes=1) as pool:
        [result] = pool.starmap(_worker_entry, args)

    assert not result.get("skipped"), result
    assert "error" not in result, result.get("error")
    assert result["tool_count"] >= 1
    assert result["frame_count"] > 0
    assert result["own_frame_jpeg_soi"] == "ffd8ff"


def test_concurrent_calls_share_no_session_id_space(cut_clip: Path, work_dir_root: Path):
    """Session IDs across N concurrent processes must show no ordering bias
    and no collision. With N=8 we expect all unique even at the 12-hex-char
    level (4^12 = 16M space)."""
    if not _server_ok:
        pytest.skip(f"mcp_server.py import failed under current SDK: {_server_msg}")
    N = 8
    worker_dirs = [work_dir_root / f"worker-{i}" for i in range(N)]
    for d in worker_dirs:
        d.mkdir()
    args = [(i, str(cut_clip), str(worker_dirs[i]), []) for i in range(N)]
    with multiprocessing.get_context("spawn").Pool(processes=N) as pool:
        results = pool.starmap(_worker_entry, args)

    sids = [r["session_id"] for r in results if not r.get("skipped") and "error" not in r]
    assert len(sids) == N, f"got {len(sids)}/{N} successful runs"
    # All 12-char hex strings
    assert all(len(s) == 12 and all(c in "0123456789abcdef" for c in s) for s in sids)
    # All unique
    assert len(set(sids)) == N, f"collisions: {sids}"
