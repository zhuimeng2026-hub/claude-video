# MCP Client Compatibility Matrix

**Status**: Draft (v0.3.0)
**Audience**: Operators registering the `claude-video` MCP server in third-party MCP hosts — i.e. the **client side** of the protocol. (For the server contract itself, see [`docs/MCP_SERVER_PRD.md`](MCP_SERVER_PRD.md).)
**Scope**: Which MCP-speaking hosts can drive `skills/watch/scripts/mcp_server.py` today, the JSON shape each host expects, the SDK version range that has been verified, and the protocol/SDK gotchas every client-side integration has to handle. Companion document to the PRD; the two are kept 1:1 — no duplication.

---

## 1. SDK version policy

The server pins **`mcp>=1.20,<2.0`** in two places:

| File | Field | Value |
|---|---|---|
| `requirements.txt` | dependency pin | `mcp>=1.20,<2.0` |
| `skills/watch/scripts/setup.py` | `OPTIONAL_PYTHON_PACKAGES` | `["mcp>=1.20,<2.0"]` |

**Why this exact range.** `tests/test_mcp_stdio_smoke.py` validates against **`mcp==1.29.0` + pydantic `2.10`**. Pre-`1.20` versions predate the bare-`bytes` crash in `@mcp.resource` (which the server works around via `Annotated[bytes, Field(...)]` — see `MCP_SERVER_PRD.md` §6.1 Pitfall A). The `<2.0` upper bound reserves the right to pin before any hypothetical major release changes the resource-decorator contract.

**Client SDK rule of thumb.** Prefer the same pinned range. Mixing client SDK `≥2.0` with this server is unsupported until validated in the smoke test below.

To check your client SDK is in range:

```bash
python3 -c "import importlib.metadata as m; print(m.version('mcp'))"
```

---

## 2. Verified compatibility matrix

Legend:
- ✅ **verified** — this repo's test suite exercises it. Reproducible on a clean checkout.
- 🟡 **claimed** — works per the host's vendor docs and the JSON shape above; not exercised by an automated test. Footnote where to test.
- ⚪ **out of scope** — explicitly not supported in v1 (see §6).

### 2.1 Hosted hosts (UI / IDE wrappers around an MCP server process)

| Client | Status | Platform | Config shape | Verification path |
|---|---|---|---|---|
| openclaw (`mcpServers`) | 🟡 claimed | macOS, Linux | `MCP_SERVER_PRD.md` §3.3 | Manual: `tools/list` returns `watch`. |
| openclaw (`mcporter.json` variant) | 🟡 claimed | macOS, Linux | `MCP_SERVER_PRD.md` §3.4 | Same. |
| Claude Desktop | 🟡 claimed | macOS, Windows | `MCP_SERVER_PRD.md` §3.3 | Same. |
| Continue (VS Code / JetBrains extension) | 🟡 claimed | All | `MCP_SERVER_PRD.md` §3.3 | Same. |
| Zed | 🟡 claimed | macOS, Linux | `MCP_SERVER_PRD.md` §3.3 | Same. |

The four hosted-host rows all use the same `mcpServers` JSON schema — only the sidecar location and reload mechanics differ per host, which is outside the server's responsibility. To validate any of them against this server, run the §5 minimal-connect probe (does `tools/list` return `watch` and does a `tools/call` with a tiny local clip return `frame_uris`?).

### 2.2 Lightweight / scripted clients

| Client | Status | SDK / package | Verified by |
|---|---|---|---|
| `@modelcontextprotocol/inspector` | ✅ verified | npm (no install) | Manual + smoke test fixture below. |
| Python stdio client (`mcp.client.stdio.stdio_client`) | ✅ verified | `mcp>=1.20,<2.0` (matches server) | `tests/test_mcp_stdio_smoke.py::test_concurrent_subprocesses_isolated` (N=4 spawn-pool workers, full handshake). |
| Node.js stdio client (`@modelcontextprotocol/sdk/client/stdio`) | ✅ verified | `@modelcontextprotocol/sdk` | Same suite (run against this repo to exercise). |
| **Custom HTTP / SSE client** | ⚪ out of scope | n/a | Server is stdio-only in v1 — see §6. |

The protocol layer (stdio, JSON-RPC 2.0, MCP `2024-11-05` handshake, `tools/list` / `tools/call` / `resources/list` / `resources/read`) is what `tests/test_mcp_stdio_smoke.py` locks down. If a non-listed client passes that test against `mcp_server.py`, treat it as verified.

---

## 3. JSON config shapes

The server is invoked identically from every client (`python3 /absolute/path/to/skills/watch/scripts/mcp_server.py`); only the registration file format differs.

### 3.1 Standard `mcpServers` shape

Used by Claude Desktop, Continue, Zed, and openclaw's default config. See `MCP_SERVER_PRD.md` §3.3 for the full snippet. Key fields:

- `command`: `python3` (or `python` on Windows — the `python3` binary on Windows is the Microsoft Store stub).
- `args[0]`: absolute path to `mcp_server.py` inside the **installed** skill folder, not the repo working tree.
- `env`: leave empty; the server reads API keys from `~/.config/watch/.env` itself.

### 3.2 openclaw `mcporter.json` variant

Only differs in that the server entry sits at the top level (no `mcpServers` wrapper). See `MCP_SERVER_PRD.md` §3.4. If a host you target reads this file, prefer it over the more common shape — both are equivalent.

### 3.3 Caveats that apply to every config shape

- **Absolute paths only.** Relativize `args[0]` and `out_dir` (the optional per-call override) consistently — mixing them produces confusing behaviour on some hosts.
- **No env passthrough.** Don't pass `GROQ_API_KEY`, `OPENAI_API_KEY`, `REPLICATE_API_TOKEN` through this config. The server sources them from `~/.config/watch/.env` (mode `0600`) per `skills/watch/scripts/config.py`. Hosts that write keys to `/tmp` during debugging will leak them.
- **One server, one process.** If the host expects to coordinate multiple `claude-video` server entries (e.g. for different skill versions), they will run as independent stdio processes — each with its own in-memory session registry. Multi-process coordination goes through the BFF (Phase 2.7), not the server.

---

## 4. SDK gotchas that hit the **client side**

These three are lifted from `MCP_SERVER_PRD.md` §6.1 and rewritten in client-side terms. The PRD tells you **how the server handles them**; this section tells you **what code you must write on the client**.

### 4.1 `BlobResourceContents` payload is base64, not raw bytes

When you `resources/read` a `watch-frame://.../frames/frame_NNNN.jpg` URI, the server returns one content block of type `BlobResourceContents`. **That object's payload is in `block.blob` and is base64-encoded.** Newer `mcp` SDK versions do **not** expose a `.content` shortcut — you must branch on the type:

```python
block = res.contents[0]
if hasattr(block, "blob") and block.blob:
    raw_bytes = base64.b64decode(block.blob)          # frames / masks
elif hasattr(block, "text") and block.text:
    raw_bytes = block.text.encode()                   # text resources (no current use)
else:
    raise RuntimeError(f"unknown block shape: {type(block).__name__}")
```

Symptom if you ignore this: `AttributeError: 'BlobResourceContents' object has no attribute 'content'`.

### 4.2 Bare `bytes` cannot be a `@mcp.resource` return type under pydantic 2.10+

This is **mostly a server-side concern**, but if you write your own mock MCP server to test your client without driving claude-video, you will hit it. The error is `PydanticUserError: A non-annotated attribute was detected`. Fix on the server side (mock or real):

```python
from typing import Annotated
from pydantic import Field

@mcp.resource(...)
def read_frame(uri: str) -> Annotated[bytes, Field(description="JPEG bytes")]:
    ...
```

Verified-OK combinations: **`mcp==1.29.0` + `pydantic 2.10`** (what CI runs).

### 4.3 `importlib.util.spec_from_file_location` breaks pydantic resolution

If you write dev tools that load the server via `spec_from_file_location("mcp_server_probe", path)`, annotations like `Annotated[bytes, Field(...)]` will raise `NameError: name 'Annotated' is not defined` even though `mcp_server.py` imports cleanly under normal `import mcp_server`. **Use a real `import`** after `sys.path.insert(0, SCRIPT_DIR)`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("/absolute/path/to/skills/watch/scripts")))
import mcp_server  # registers in sys.modules — required for pydantic
```

`setup.py::_check_mcp_server_compat()` already does this correctly; mirror its pattern.

---

## 5. How to verify a new client (the standard probe)

Before claiming any client works, run the **three-step protocol probe**:

### 5.1 Preflight (host-independent)

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --json
```

Expect `mcp_available: true` **and** `mcp_server_compat: true`. If `mcp_server_compat: false`, the SDK at the client's runtime is out of range — fix that before debugging the client.

### 5.2 Subprocess handshake (host-independent)

```bash
pytest tests/test_mcp_stdio_smoke.py -q
```

This is the truth: 4–8 fresh server subprocesses, each running a full `initialize` → `list_tools` → `call_tool("watch")` → `read_resource` cycle, with cross-process session-ID isolation asserted. If this passes, the server side is fine.

### 5.3 In-host handshake (host-dependent)

In the candidate client, after registering the server:

1. **`tools/list`** — expect exactly `watch` (and, once Phase 2.6 lands, `recompose`). Schema must match `MCP_SERVER_PRD.md` §2.2 (Python) — the JSON-Schema view the host sees is auto-generated by FastMCP from the type hints.
2. **`tools/call("watch", {"source": "<small local mp4>", "detail": "efficient", "no_whisper": true})`** — expect `session_id`, `frame_uris`, `frame_count`, `report`. Don't ship Whisper / segmentation in this probe (network-dependent).
3. **`resources/read`** one `watch-frame://<sid>/frames/frame_0001.jpg` URI — expect raw JPEG bytes after decoding `block.blob`. If the client UI shows `<bytes 0x...>` correctly, you're done.

If any of these three fail, open an issue with the (host, host version, mcp SDK version, Python version, OS) tuple — that's the minimum useful report.

---

## 6. Out of scope for v1 (intentionally unsupported)

These are not bugs. They are v1 design choices; clients should not assume otherwise.

| Gap | Status | Workaround |
|---|---|---|
| **HTTP / SSE / streamable-HTTP transport** | ⚪ not implemented | Local stdio agents only. Remote platforms currently need the BFF (Phase 2.7, `docs/BFF_API_CONTRACT.md`). |
| **Streaming progress notifications** (`meta.progressToken`, `notifications/progress`) | ⚪ not wired up | `tools/call` blocks until the pipeline completes. For a 30-min `balanced` call, that's 30–90 s of blocking. Hosts that surface stderr can show partial progress; hosts that don't surface stderr see nothing until the call returns. |
| **Multiple tools** (`recompose`, `cleanup`, etc.) | ⚪ only `watch` ships | The PRD §2.6 `recompose` target contract exists; implementation lands in `docs/todo.md` §2.6.2. Until then, callers needing composition go through the BFF. |
| **Cross-restart session persistence** | ⚪ sessions are in-process | Server restart loses the `SESSIONS` registry. Do not cache `watch-frame://` URIs across restarts — re-call `watch`. |
| **`out_dir` as a relative path** | ⚪ discouraged | Pass absolute paths. Relative paths are resolved against the host's cwd, which depends on how the host launched the server. |
| **Claude.ai sandbox** | ⚪ excluded by design | `mcp_server.py` is in `skills/watch/.skillignore`. The claude.ai web runtime doesn't host stdio MCP servers; ship via `npx skills add` to get the MCP server on local hosts. |

---

## 7. Lightweight clients (ready-to-run)

The server is plain stdio JSON-RPC 2.0 — **any MCP 2024-11-05 client can drive it**. Three drop-in examples. Mirror them when adding a new host's smoke test.

### 7.1 MCP Inspector (npm, no install beyond Node)

```bash
npx @modelcontextprotocol/inspector python3 /absolute/path/to/skills/watch/scripts/mcp_server.py
```

Opens a web UI showing `tools/list`, `resources/list`, and lets you call them interactively. Good for sanity-checking the server works in your environment before wiring it into a host.

### 7.2 Python stdio client (10 lines, no FastMCP)

```python
import asyncio, json
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="python3",
        args=[str(Path("/absolute/path/to/skills/watch/scripts/mcp_server.py"))],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("watch", {
                "source": "/path/to/clip.mp4",
                "detail": "efficient",
                "no_whisper": True,
            })
            print(json.loads(result.content[0].text).keys())

asyncio.run(main())
```

### 7.3 Node.js stdio client

```js
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({
  command: "python3",
  args: ["/absolute/path/to/skills/watch/scripts/mcp_server.py"],
});
const client = new Client({ name: "my-agent", version: "0.1.0" }, { capabilities: {} });
await client.connect(transport);
const { tools } = await client.listTools();
const { content } = await client.callTool({
  name: "watch",
  arguments: { source: "/path/to/clip.mp4", detail: "efficient", no_whisper: true },
});
console.log(JSON.parse(content[0].text).session_id);
```

---

## 8. Reporting new findings (keeping the matrix honest)

- **A new client works** → open a PR adding a row to §2.1 or §2.2. Status flips to ✅ only once `tests/test_mcp_stdio_smoke.py` runs unmodified against it (or runs after a documented change).
- **A new client breaks** → open an issue with the (client, host version, mcp SDK version, OS, Python) tuple and the exact failure point (handshake, `tools/list`, `tools/call`, `resources/read`). Then add a row marked ❌ in §2.1 with the failure summary linked.
- **A new SDK version changes behaviour** → bump §1, run §5's full probe, update §4. If the change is breaking, pin a new upper bound in `requirements.txt` and `setup.py` (these two must stay in lockstep).
- **A protocol contract change** → update both this document and `MCP_SERVER_PRD.md` in the same PR. PRD is the server-side source of truth; this file is the client-side mirror.

---

## 9. Reference

- Server contract: [`docs/MCP_SERVER_PRD.md`](MCP_SERVER_PRD.md)
- Server source: `skills/watch/scripts/mcp_server.py`
- Smoke test: `tests/test_mcp_stdio_smoke.py` (also pulls in `tests/test_mcp_server.py` for the in-process single-client case)
- Setup / compat probe: `skills/watch/scripts/setup.py` → `_check_mcp_server_compat()`, `_ensure_scripts_on_path()`
- SDK pin: `requirements.txt` (`mcp>=1.20,<2.0`)
- MCP spec: https://modelcontextprotocol.io/specification/2024-11-05
- mcp Python SDK: https://github.com/modelcontextprotocol/python-sdk
