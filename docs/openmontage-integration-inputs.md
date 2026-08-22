# OpenMontage × claude-video: claude-video 侧需要的输入清单

**作者**: OpenMontage 集成 reviewer
**日期**: 2026-08-23
**Status**: ✅ All 8 checklist items resolved as of commit `7a226fc` on `main` (2026-08-23). OM owner can start `tools/external/claude_video.py`.

**关联文档**:
- `OpenMontage_Voicebox/docs/claude-video-integration.md` — 总集成规格
- `OpenMontage_Voicebox/docs/claude-video-whitelist-audit.md` — 白名单独立审核结果(已出)
- `/opt/claude-video/docs/todo.md` — claude-video 团队自己的路线图(已存在,本文不重复,只补 OM 侧的契约)
- `/opt/claude-video/docs/MCP_SERVER_PRD.md` — 当前 MCP server v1 的接口 + §2.6 `recompose` tool 目标契约
- `/opt/claude-video/docs/BFF_API_CONTRACT.md` — Phase 2.7 BFF 端点契约(8 endpoints,SSE)
- `/opt/claude-video/docs/OAUTH_TRUST_MODEL.md` — Phase 2.8 OAuth 信任模型(BFF 单一防线)
- `/opt/claude-video/docs/OPENMONTAGE_NAME_MAP.md` — pipeline + style 名称映射
- `/opt/claude-video/tests/fixtures/sample_runresult.json` — 规范 RunResult 示例
- `/opt/claude-video/tests/fixtures/error_envelope_*.json` — 错误 envelope 示例

---

## 总览

OpenMontage 侧可以独立完成的:
- ✅ 6 个 pipeline 全部 GPU-free 验证完成(见 `claude-video-whitelist-audit.md`)
- ✅ `projects/users/<user_openid>/` 路径已对齐 `web-multiuser-auth.md`
- ✅ Adapter 风格模板 `tools/_comfyui/`、`tools/_kling/` 已确认可参考
- ✅ `tests/integration/` 已有 voicebox 同款集成测试骨架

OpenMontage 侧**做不了**、必须 claude-video 团队提供的:

1. **F1** — `podcast-reproduce` → `podcast-repurpose` 拼写修正(`todo.md` §2.6.2 里就拼错了)
2. **`RunResult` 返回结构补充字段** —— 当前 PRD 的 `watch` 返回没有 `transcript_segments` / `vtt_path` / `video_path`,adapter 拿不到这些必需字段
3. **`recompose` tool 完整签名** + 与 OM §4.4 错误码对齐
4. **Phase 2.7 BFF endpoint 契约** —— 至少 PIN 路径 + 各端点 JSON shape
5. **Phase 2.8 OAuth 信任模型** —— 谁来防止横向越权
6. **名称映射表** —— `pipeline` / `style` 在两仓之间的固定值对照
7. **测试夹具** —— adapter 单测用得上的固定 `RunResult` + 可 import 的 Python 模块入口

每节下面的 **Concrete deliverable** 列具体产出物,**Where to put it** 列文件路径。

---

## 0. Critical:todo.md 必须先修的拼写

### F1: `podcast-reproduce` → `podcast-repurpose`

`todo.md` §2.6.2 当前这行:

> `pipeline` 接受 `["clip-factory", "documentary-montage", "podcast-reproduce", "localization-dub", "hybrid"]` 中任一值

OpenMontage 侧实际 pipeline 文件是 `pipeline_defs/podcast-repurpose.yaml`,OM adapter 严格按这个名字查白名单。**任何 typo 拼写都会拿到** `ToolError: pipeline not in whitelist`。

**Concrete deliverable**:直接编辑 `todo.md` §2.6.2,把 `podcast-reproduce` 改成 `podcast-repurpose`。

> **✅ Done** — commit `644d7b0 fix(todo): correct podcast-reproduce typo to podcast-repurpose, add screen-demo` on `main`. Also added the missing `screen-demo` value (it's a real OM pipeline referenced elsewhere in our docs but was missing from the whitelist). Final whitelist: `["clip-factory", "documentary-montage", "podcast-repurpose", "localization-dub", "hybrid", "screen-demo"]`.

---

## 1. RunResult 返回结构 → 对齐 OM `ClaudeVideoInputs.source`

OM 侧 adapter (`tools/external/claude_video.py`) 期望 `source` 字段长这样:

```python
{
    "video_id": str,                       # 新增(Phase 2.1)—— 默认 URL hash 12 字符
    "frames_dir": "<abs path>",
    "masks_dir":  "<abs path>" | None,
    "vtt_path":   "<abs path>" | None,
    "video_path": "<abs path>" | None,
    "duration_seconds": float,
    "transcript_segments": [
        {"start": float, "end": float, "text": str}, ...
    ],
}
```

**当前 `MCP_SERVER_PRD.md` §2.2 只返回了**:
- `report` (markdown 字符串)
- `session_id` (12-hex)
- `work_dir`
- `frame_uris` (`watch-frame://<sid>/frame_NNNN.jpg`)
- `frame_count`
- `transcript_source`

**直接缺**: `video_id`、`frames_dir`(只有 URI 没有目录路径)、`masks_dir`、`vtt_path`、`video_path`、`duration_seconds`、`transcript_segments`(只有 markdown 散文,没结构化)。

这些字段有的能 derive(从 `work_dir` 推导标准路径),有的是真没有(VTT 路径、transcript 结构化列表、视频源文件)。

### Concrete deliverables

1. **`recompose` tool 的返回增加以下字段**(不动老的 `watch` 工具):
   ```json
   {
     "video_id": "...",
     "frames_dir": "/abs/path/frames/",
     "masks_dir":  "/abs/path/masks/" | null,
     "vtt_path":   "/abs/path/video.en.vtt" | null,
     "video_path": "/abs/path/source.mp4" | null,
     "duration_seconds": 137.42,
     "transcript_segments": [
       {"start": 0.0, "end": 2.84, "text": "..."},
       ...
     ],
     "project_id": "...",
     "status": "submitted",
     "render_url": "...",
     "backlot_url": "..."
   }
   ```

2. **PIN 路径与文件命名约定**:
   - `frames_dir` = `work_dir/frames/`?
   - frame 命名 = `frame_NNNN.jpg`?
   - mask 命名 = `mask_NNNN.png`?
   - VTT 命名 = `<video-slug>.<lang>.vtt` 还是固定 `transcript.vtt`?
   - 当前 PRD 已经写 `frame_NNNN.jpg` 和 `mask_NNNN.png`,**确认即可**。

3. **`masks_dir` 始终存在**(即使没 segmentation 也是 `null`),让 adapter 一行就能决定要不要处理 masks。

### Where to put it
- 更新 `MCP_SERVER_PRD.md`,新增 §2.6 "The `recompose` tool":完整 JSON schema + 一份带 sample data 的 example。

> **✅ Done** — `docs/MCP_SERVER_PRD.md` §2.6 in commit `7a226fc`. Includes:
> - Full signature (`recompose(video_id, pipeline, style="clean-professional", user_openid=None, extra={})`)
> - Complete return shape with `video_id` / `frames_dir` / `masks_dir` / `vtt_path` / `video_path` / `duration_seconds` / `transcript_segments` + OM's `project_id` / `status` / `render_url` / `backlot_url`
> - Field derivation table showing how each value is reconstructed from `session_store` + watch artifacts (no change to `watch` tool's return — its `report` / `session_id` / `work_dir` / `frame_uris` / `frame_count` / `transcript_source` shape is preserved per OM's "不动老的 `watch` 工具" constraint)
> - Sample referenced from `tests/fixtures/sample_runresult.json`

---

## 2. `recompose` tool —— 完整签名 + 错误码

`todo.md` §2.6.2 当前描述:
```
recompose(video_id, pipeline, style="clean-professional", ...)
```
**不完整**: `user_openid` / `extra` 透传 / 错误码 envelope 都没声明。

### Concrete deliverables

把 `todo.md` §2.6.2 的"输入参数"块改写成下面这种完整 Python-style 签名:

```python
recompose(
    video_id: str,                                                           # required; 必须在 session_store 中已存在
    pipeline: Literal["clip-factory", "documentary-montage",
                      "podcast-repurpose", "localization-dub",
                      "hybrid", "screen-demo"],                              # required; 已修 typo
    style: str = "clean-professional",                                       # optional; OM playbook 名
    user_openid: str | None = None,                                          # optional; BFF 调用时必传;OM 落盘路径依赖它
    extra: dict = {},                                                        # optional; 透传给 OM ClaudeVideoInputs.extra
) -> dict
```

返回示例:
```json
{
  "status": "submitted",
  "project_id": "abc123def456",
  "render_url": "https://.../renders/final.mp4",
  "backlot_url": "http://localhost:8900/backlot/abc123def456"
}
```

错误 envelope(参考 OM `claude-video-integration.md` §4.4,**两边的错误名必须 1:1 对齐**):

| claude-video ToolError `code` | 触发条件 |
|---|---|
| `pipeline_not_in_whitelist` | `pipeline` 不在白名单内 |
| `video_id_unknown` | `video_id` 不在 `session_store.py`(可能已 expire) |
| `user_not_found` | `user_openid` 为空且需要 |
| `assets_copy_failed` | 拷贝 artifacts 到 OM project dir 失败 |
| `pipeline_stage_failed` | OM pipeline 任何 stage 失败 |
| `gpu_required` | 选的 pipeline 在无 GPU 环境不可跑(OM-7) |

### Where to put it
- `MCP_SERVER_PRD.md` §2.6 完整替换 `todo.md` §2.6.2 的描述。
- 或新开 `docs/RECOMPOSE_TOOL_SPEC.md`,在 todo.md §2.6.2 加引用。

> **✅ Done** — Both surfaces documented in commit `7a226fc`:
> - **`MCP_SERVER_PRD.md` §2.6.3** has the 6-code error envelope (`pipeline_not_in_whitelist` / `video_id_unknown` / `user_not_found` / `assets_copy_failed` / `pipeline_stage_failed` / `gpu_required`), with HTTP status equivalents for the BFF layer (see `docs/BFF_API_CONTRACT.md`). Names are 1:1 with OM §4.4 per the cross-repo contract.
> - Three envelope fixtures committed at `tests/fixtures/error_envelope_*.json` so OM CI can pin against them.
> - **`docs/OPENMONTAGE_NAME_MAP.md`** pins the whitelist itself (the doc's 6 values), so adding/removing a value here is a single place to coordinate.

---

## 3. Phase 2.7 BFF —— :8910 endpoint 契约

`todo.md` §2.7 已经规划了 FastAPI BFF 的 endpoint。OM adapter 不直接调 BFF(MCP ↔ MCP,BFF 给浏览器用),但 OM 集成测试会用 `curl` 走 BFF 端到端通一遍。所以 endpoint 必须 PIN。

### Concrete deliverables

新增 `docs/BFF_API_CONTRACT.md`,至少包含:

| 端点 | Method | Body | Success Response | Errors |
|---|---|---|---|---|
| `/api/watch/start` | POST | `{source, video_id?, user_openid?}` | `{video_id, status: "running", stage: "download"}` | 400 `{error: "invalid_source"}` |
| `/api/watch/{video_id}/status` | GET | — | `{video_id, stage, progress: 0-100, eta_seconds?, error?}` | 404 `{error: "video_id_unknown"}` |
| `/api/watch/{video_id}/frame/{fn}` | GET | — | bytes(image/jpeg) | 404 |
| `/api/watch/{video_id}/mask/{fn}` | GET | — | bytes(image/png) | 404 |
| `/api/watch/{video_id}/events` | GET | — | SSE `{stage, progress, message, ts}` | — |
| `/api/watch/{video_id}/cancel` | POST | — | `{cancelled: bool}` | 404 |
| `/api/recompose` | POST | `{video_id, pipeline, style?, user_openid?}` | 同 §2 | 401 / 4xx 见 §2 错误码表 |
| `/health` | GET | — | `{status: "ok"}` | — |

每个 endpoint 给一段 curl 示例(成功 + 错误各一条)。

**Port & CORS**(确认 todo.md §2.7 的默认值):
- `:8910` default,env override `WATCH_BFF_PORT`
- CORS env `WATCH_BFF_CORS_ORIGINS` 默认 `["http://localhost:*", "tauri://"]`
- OpenMontage 的 `.env` 也读同一套 `WECHAT_MP_APP_ID` / `WECHAT_MP_APP_SECRET` / `WECHAT_MP_REDIRECT_URI`(按 OM `doc-wechat-open-platform-oauth.md` 命名);**确认 BFF 读相同的 env 名字,不要换名**。

### Where to put it
- 新建 `docs/BFF_API_CONTRACT.md`。
- `todo.md` §2.7 末尾加一行"see BFF_API_CONTRACT.md"。

> **✅ Done** — `docs/BFF_API_CONTRACT.md` in commit `7a226fc`. All 8 endpoints (POST /api/watch/start, GET /api/watch/{id}/status, /frame/{fn}, /mask/{fn}, /events SSE, /cancel, POST /api/recompose, GET /health) with body/response/error tables and curl examples. Pins :8910 default, CORS default (`["http://localhost:*", "tauri://"]`), SSE framing, and the WECHAT_MP_* env-var compatibility with OM. Cross-repo contract section at the bottom describes what OM's integration test will hit.

---

## 4. Phase 2.8 OAuth —— 信任模型必须明文

`todo.md` §2.8 已经确认 BFF 自己跑 OAuth,cookie 名 `WATCH_SESSION`。**OM 完全不验证这个 cookie**,信任模型是:

```
Browser ─cookie WATCH_SESSION─> BFF (验 cookie → 查 user_openid)
                                       │
                                       └─stdio MCP (user_openid 透传进 tool args)
                                                  │
                                                  └─stdio MCP (OM 收到 string,落到 projects/users/<user_openid>/)
```

**横向越权防御全部在 BFF 那一边**:谁能进 `WATCH_SESSION`,谁就能伪造 `user_openid`。OM 不验。

### Concrete deliverables

新建 `docs/OAUTH_TRUST_MODEL.md`,包含:

1. **明确写出**:"OpenMontage 把 `user_openid` 当不可信输入处理。它落到 `projects/users/<user_openid>/` 完全基于这个字符串。防止 A 用户在 B 用户的目录写文件这一保证,完全依赖 BFF 正确地校验 `WATCH_SESSION` cookie 然后只透传对应的 `user_openid`。"
2. **`openid` 唯一性模型决定**:如果同一个人在不同 service-account / open-platform 之间被分配到不同 openid,OM 当前是按 raw string 分目录的,可能产生重复 user。文档说明:
   - claude-video 用 `unionid` 还是 `openid` 作为 user key?
   - 命名空间走 `openid@provider` 还是纯 `openid`?
   - 不一致时是不是该 namespace?
3. **env var 兼容性**:`WECHAT_MP_APP_ID` / `WECHAT_MP_APP_SECRET` / `WECHAT_MP_REDIRECT_URI` 与 OM 同名。如果有重命名需求,在这里 PIN,不要 ship。

### Where to put it
- 新建 `docs/OAUTH_TRUST_MODEL.md`(短文,~50 行)。
- `todo.md` §2.8 末尾加引用。

> **✅ Done** — `docs/OAUTH_TRUST_MODEL.md` in commit `7a226fc` (slightly longer than ~50 lines because the BFF/MCP/OM matrix warranted a per-layer table). Pins:
> 1. "OM treats `user_openid` as untrusted" — explicit.
> 2. `openid` alone as the MVP user-key uniqueness decision (with rationale + escape hatch to `openid@provider` if needed).
> 3. WECHAT_MP_APP_ID / WECHAT_MP_APP_SECRET / WECHAT_MP_REDIRECT_URI env-var compatibility, plus WATCH_BFF_PUBLIC_URL / WATCH_BFF_COOKIE_SECURE.
> 4. Per-layer check matrix: BFF (cookie), MCP `recompose` tool (video_id ↔ user_openid), OM (nothing).

---

## 5. 名称映射表

`recompose` 工具的 `pipeline` / `style` 参数必须在两仓之间有固定映射。

### pipeline 映射

| claude-video 端接受值(`todo.md` §2.6.2,已用 F1 修正) | OpenMontage 白名单(`claude-video-integration.md` §5) |
|---|---|
| `clip-factory` | `clip-factory` |
| `documentary-montage` | `documentary-montage` |
| `podcast-repurpose` | `podcast-repurpose` |
| `localization-dub` | `localization-dub` |
| `hybrid` | `hybrid` |
| `screen-demo` | `screen-demo` |

如果 claude-video 想引入 OM 没有的概念(`highlight-clip`、`viral-cut` 等),在这一节**先**定义映射,OM 白名单相应扩展;**不要**让 claude-video 端直接传一个 OM 不认的字符串然后期待 OM 兜底。

### style → OM playbook 映射

| claude-video `style` 值 | OM playbook(`styles/*.yaml` 列表) |
|---|---|
| `clean-professional` | `clean-professional` |
| `flat-motion-graphics` | `flat-motion-graphics` |
| `minimalist-diagram` | `minimalist-diagram` |
| `premium-minimalist` | `premium-minimalist` |
| `ink-sketch` | `ink-sketch` |
| `anime-ghibli` | `anime-ghibli` |
| 任何其它值 | `extra={"playbook_override": "<that value>}` 透传;**OM owner 必须在 adapter 里实现明确处理逻辑后**才安全 |

### Concrete deliverable

新建 `docs/OPENMONTAGE_NAME_MAP.md`,把两张表 PIN 死。

### Where to put it
- 新建 `docs/OPENMONTAGE_NAME_MAP.md`。
- `todo.md` §2.6.2 末尾加引用。

> **✅ Done** — `docs/OPENMONTAGE_NAME_MAP.md` in commit `7a226fc`. Two pinned tables:
> - **pipeline** (6 values) with per-value OM `pipeline_defs/*.yaml` source and GPU-free column.
> - **style → OM playbook** (6 fixed values + one escape hatch row for unknown playbooks via `extra={"playbook_override": ...}`, marked "only safe after OM owner implements explicit handling").
>
> Doc also names the three enforcement points (todo.md §2.6.2, the `pipeline_not_in_whitelist` error fixture, and the `recompose` tool's whitelist check) and adds a process for adding new pipeline values that keeps both repos in lockstep.

---

## 6. 测试夹具 + Python 模块入口

OM 侧集成测试样例(`claude-video-integration.md` §6)期望 `from claude_video.mcp_server import run_watch`。但 claude-video 当前发布形式是 `.skill` bundle + git clone,**不是 pip 装的包**。

### Concrete deliverables

1. **让 import 能跑通**(二选一,OM 侧接受任一):
   - **方案 A**:给仓库加个 `pyproject.toml`,至少 `pip install -e .` 后 `import claude_video.mcp_server` 通。**最干净**。
   - **方案 B**:在仓库根放一个 `claude_video/__init__.py` 空壳 + symlink `skills/watch/scripts/mcp_server.py` → `claude_video/mcp_server.py`。OM 测试的 `conftest.py` 加 `sys.path.insert(0, "<repo root>")`。

2. **固定输入的 sample `RunResult`** —— 大约 10 秒 public domain mp4,带一份 `transcript_segments` 列表,加 video_id 锚定,落 `tests/fixtures/sample_runresult.json`。这样 OM CI 不用每次跑都拉网络。

3. **样例错误 envelope**:`pipeline_not_in_whitelist` / `video_id_unknown` / `assets_copy_failed` 各一份落盘(`tests/fixtures/`)。

### Where to put it
- `tests/fixtures/sample_runresult.json`(新)
- `tests/fixtures/claude_video_shim.py`(仅方案 B)
- `pyproject.toml`(仅方案 A)

> **✅ Done** — Chose **方案 B** (shim package over `pyproject.toml`) for these reasons:
> 1. The repo's primary distribution is the `.skill` bundle + `npx skills add` — adding `pyproject.toml` would imply pip-install support that doesn't exist today and would mislead external projects about the install model.
> 2. `pyproject.toml` would also drag in dependency-management decisions (build backend, version source of truth, lockfile) that the project deliberately defers to `~/.config/watch/.env`.
> 3. The shim is small, ships in-tree, and works with both `pip`-style and `git clone`-style consumers.
>
> Implementation:
> - `claude_video/__init__.py` — empty package marker with rationale in the docstring.
> - `claude_video/mcp_server.py`, `claude_video/watch.py` — stub modules that add `skills/watch/scripts/` to `sys.path` and re-export from the real module. (Plain symlinks were tried first; `Path(__file__).parent.resolve()` doesn't follow symlinks, which broke sibling imports inside `watch.py`. Stub modules sidestep that.)
> - `tests/conftest.py` — also adds the repo root to `sys.path` so pytest sees `claude_video`.
> - `tests/fixtures/sample_runresult.json` — canonical 10s Big Buck Bunny example, matching the §2.6 schema.
> - 3 error envelope fixtures at `tests/fixtures/error_envelope_*.json`.
> - `tests/test_claude_video_shim.py` — 8 tests pinning shim re-exports, fixture field shape, segment ordering, and the pipeline-whitelist invariant (no drift between `todo.md` §2.6.2 and the error envelope). All 8 pass; the rest of the suite (95 tests total) collects cleanly.

---

## 7. Open Questions(不阻塞白名单审核,但落地前要回答)

1. **持久化窗口**:`recompose` 返回后 `work_dir` 还活多久?OM adapter 同步拷贝 artifacts 到 `projects/users/<user_openid>/<project_id>/assets/`,大约 5–30 分钟。`work_dir` GC 策略会不会清掉?
2. **并发上限**:BFF 对多用户能不能发并行 `recompose`?MCP stdio 单 session 是 JSON-RPC 串行,要不要每个请求一个独立子进程?
3. **`audio_path` / `subtitle_path`**:`RunResult` 没列,但 `clip-factory` / `podcast-repurpose` / `screen-demo` 都要外挂字幕和原始音频轨,需要在 schema 讨论里加或显式排除。
4. **长视频处理**:`>1h` webinar 进 `clip-factory` 是常态。OM adapter 那边的 `asyncio.wait_for` 超时是否要按视频时长放缩?frame 数随时长线性增长。

> **Status (2026-08-23)**: §7 questions are deferred to the Phase 2.6 implementation phase, not the contract phase. Concrete intent for each:
>
> 1. **持久化窗口** — `session_store.py` (Phase 2.1) will pin a default TTL (proposed: 24h) on `work_dir`. `recompose` reads from the same store and extends the TTL on each access. `delete_session` is the explicit cleanup path. The BFF never auto-cleans during a request lifecycle.
> 2. **并发上限** — MVP architecture: one BFF process → one stdio MCP child → `asyncio.Lock` serializing tool calls. Multi-user concurrency lives at the *BFF process* level (deploy N BFFs behind a load balancer, each with its own MCP child), not at the *request* level. Documented in `docs/BFF_API_CONTRACT.md` "stdio MCP child" section.
> 3. **`audio_path` / `subtitle_path`** — These are real needs for `clip-factory` / `podcast-repurpose`. Plan: add to `RunResult` schema as optional fields (`audio_path: str | None`, `subtitle_path: str | None`) in a Phase 2.6 follow-up. OM adapter treats them as optional now and gracefully degrades when absent. Document the add in `docs/MCP_SERVER_PRD.md` §2.6 once the field lands.
> 4. **长视频处理** — `recompose` will accept `timeout_seconds: int | None`. OM's `asyncio.wait_for` is sized by the caller (claude-video's MCP layer) based on `duration_seconds * factor`, where `factor` is a tunable default (proposed: 30s per minute of source video, clamped 5min ≤ t ≤ 2h). This lives in `recompose`'s implementation, not in the §2.6 contract — the timeout parameter is just an optional int.

---

## 8. 集成启动 checklist

等以下全部落进 `/opt/claude-video/docs/`,OM 侧就开始写 `tools/external/claude_video.py`:

- [x] **F1** typo fix in `todo.md` §2.6.2 — commit `644d7b0` on `main`. Also added `screen-demo`.
- [x] **§1** RunResult/recompose return shape finalized + documented in MCP_SERVER_PRD.md §2.6 — commit `7a226fc`.
- [x] **§2** recompose tool signature + error code table aligned with OM §4.4 — commit `7a226fc`. 6 codes pinned, 3 envelope fixtures committed.
- [x] **§3** BFF endpoint contract documented in `docs/BFF_API_CONTRACT.md` — commit `7a226fc`. 8 endpoints.
- [x] **§4** OAuth trust model documented in `docs/OAUTH_TRUST_MODEL.md` — commit `7a226fc`. Per-layer check matrix.
- [x] **§5** name-mapping table pinned in `docs/OPENMONTAGE_NAME_MAP.md` — commit `7a226fc`. 6 pipelines, 6 playbooks.
- [x] **§6** test fixtures + importable Python entry point — commit `7a226fc`. `claude_video/` shim package, `tests/fixtures/sample_runresult.json`, 3 envelope fixtures, 8 new pytest tests (all passing).
- [x] **§7** answers (or explicit defer) to open questions — status block added; 4 questions get concrete intent, all deferred to Phase 2.6 implementation rather than blocking the contract.

> **All 8 items resolved as of commit `7a226fc` on `main` (ahead of `origin/main` by 2 commits).**
>
> OM owner can start `tools/external/claude_video.py` against:
> - Contract: [`docs/MCP_SERVER_PRD.md` §2.6](MCP_SERVER_PRD.md)
> - Whitelist: [`docs/OPENMONTAGE_NAME_MAP.md`](OPENMONTAGE_NAME_MAP.md)
> - Auth surface: [`docs/OAUTH_TRUST_MODEL.md`](OAUTH_TRUST_MODEL.md)
> - BFF endpoints (for their integration test): [`docs/BFF_API_CONTRACT.md`](BFF_API_CONTRACT.md)
> - Canonical RunResult: [`tests/fixtures/sample_runresult.json`](../tests/fixtures/sample_runresult.json)
> - Error envelopes: [`tests/fixtures/error_envelope_*.json`](../tests/fixtures/)
> - Importable surface: `from claude_video.mcp_server import watch, read_frame, read_mask` (after `sys.path.insert(0, "<claude-video repo root>")`)

一旦 OM 拿到上面 8 项的链接 + 1 份确认 commit hash,就正式合并 OM 侧的 `tools/external/claude_video.py` + 注册到 `tool_registry.py` + `tests/integration/test_claude_video_adapter.py`。
