# TODO: claude-video 外部接入扩展

> MVP 路线图。配套阅读 `docs/MCP_SERVER_PRD.md`(v1 stdio server 已实现)。
> 所有改动都围绕一个原则:**保持 `watch.run()` 的结构化返回 + session registry 作为单一事实源**,新增能力都以薄包装的形式叠加,不动核心 pipeline。

---

## 当前基线 (v1)

- 单个工具 `watch`,stdio transport,`FastMCP` 包装
- 资源方案 `watch-frame://<sid>/frames/{file}` 与 `watch-frame://<sid>/masks/{file}`
- 进程内 session registry,生命周期 = stdio 客户端生命周期
- `RunResult` 已经结构化返回 frames / masks / transcript_segments / work_dir
- v1 PRD 明确把 HTTP transport / 多 tool / 流式进度 划入 out-of-scope

---

## Phase 1 — 兼容轻量 MCP 客户端 (MVP,目标 ~1 天)

**目标**:让任何遵循 MCP 2024-11-05 的 stdio 客户端都能用,不止 openclaw。当前 `FastMCP` 已经是 MCP 官方 SDK,理论上 stdio 协议层是通的;这一阶段做的是**实测 + 依赖收敛 + 文档**。

### 1.1 协议层冒烟测试

- [ ] 写 `tests/test_mcp_stdio_smoke.py`:用 `mcp.client.session.stdio` 启 server,跑 initialize → tools/list → tools/call(`watch` 对一个本地 5 秒合成视频)→ resources/read 拉第一帧 JPEG
- [ ] 断言:返回的 JPEG bytes 与本地 `work_dir/frames/frame_0001.jpg` 完全一致
- [ ] 断言:session_id 在二次调用之间不冲突(连发两次 watch,sid 不同,registry 各自独立)

### 1.2 依赖收敛

- [ ] 现有 `mcp_server.py:35` 依赖 `mcp` Python 包 —— `requirements.txt`(或 `pyproject.toml`)显式声明 `mcp>=1.0`
- [ ] `skills/watch/scripts/setup.py` 在预检时检测 `import mcp` 是否成功;失败时给出 `pip install mcp` 的明确指引
- [ ] README 增加"轻量客户端"小节,列出**不需要 openclaw** 的启动方式(MCP Inspector / 任意 stdio 客户端 / 直接 python 启动)

### 1.3 输入校验硬化

- [ ] 路径穿越防御当前只在 `read_frame` / `read_mask`(`mcp_server.py:202-208`)做了;补上 `watch` tool 参数层:`source` 是 URL 时走 `is_url()`,本地路径必须 `Path.resolve().is_file()`,`out_dir` 同样校验不能逃出 `Path.home()/.cache/watch-mcp/` 之外(除非显式 `--allow-arbitrary-out`)
- [ ] 错误返回统一走 `ToolError`(已经实现,见 `mcp_server.py:127`),不要再让 `SystemExit` 漏出

### 1.4 兼容性矩阵

- [x] 写 `docs/MCP_CLIENT_COMPAT.md`,列出测试过的客户端:openclaw / Claude Desktop / MCP Inspector / 自写 stdio 客户端,每个记协议版本、最小复现命令 → 已落地 (§2 矩阵 + §5 三步验证 probe)

**Phase 1 完成标志**:任何 200 行内的 stdio 客户端都能跑通 `watch` + 拉帧,且 `pytest -q` 全绿。

---

## Phase 2 — 拆分调用 + 外部按 ID 拉帧 + 进度推送 (MVP,目标 ~3 天)

用户场景:
- A. **外部 web 服务按视频 ID 拉帧** —— 给视频一个稳定 ID(URL hash 或 caller 指定),后续任意时刻通过该 ID 取产物
- B. **拆分调用逐步观察** —— `start_watch` / `get_status` / `get_results` 三个 tool,长任务不再阻塞单次 call
- C. **WebSocket 推送进度** —— download / frames / transcribe / segment 每一步都向订阅者发事件

### 2.1 稳定 video_id + 持久化 session registry

- [ ] 新增 `skills/watch/scripts/session_store.py`:把 `SESSIONS` dict 从内存搬到 `~/.cache/watch-mcp/sessions.json`(atomic write,模式 `0600`)
- [ ] `video_id` 参数加进 `watch` tool:`video_id: str | None = None`(不传则用 URL hash 的 12 字符)
- [ ] 同 `video_id` 二次调用:默认**复用**已有 work_dir(只重跑请求的阶段,如 `--segment` 增量);可显式 `--restart` 强制重跑
- [ ] **微信用户占位字段(预留,本期不接 OAuth 流)**:每条 session 记录带 `user_openid: str | None`、`user_unionid: str | None`、`auth_source: Literal["wechat_mp","wechat_op","none"]`;MCP tool 接受可选 `user_openid` 参数,先**只存不验**,等 2.7 接入正式 OAuth 流时再校验
- [ ] 提供 `list_sessions` / `delete_session` 两个新 MCP tool,让外部可以清理磁盘;这两个 tool **必须**校验调用方传入的 `user_openid` 与 session 记录的 `user_openid` 匹配,默认拒绝跨用户访问(同 2.7 的隔离模型)

### 2.2 拆分 pipeline 为多个 MCP tool

保持 `watch` 单调用作为便利入口,新增细粒度 tool:

| 新 tool | 输入 | 输出 | 行为 |
|---|---|---|---|
| `start_watch` | `video_id, source, ...` | `{video_id, status: "running", stage: "download"}` | 启动后台线程跑 `watch.run()`,立刻返回 |
| `get_status` | `video_id` | `{video_id, stage, progress: 0-100, eta_seconds?, error?}` | 查 session state |
| `get_results` | `video_id` | 同 `watch` 返回结构 | 任务完成后取结果 |
| `cancel_watch` | `video_id` | `{cancelled: bool}` | 设置取消标志,后台线程在下一个阶段检查 |
| `read_frame` | `video_id, filename` | JPEG bytes | 已存在的 resource,**改成按 video_id** |
| `read_mask` | `video_id, filename` | PNG bytes | 同上 |

- [ ] `session_store.py` 增加 `stage` 字段(`"download" | "frames" | "transcribe" | "segment" | "done" | "error" | "cancelled"`)和 `progress: float`
- [ ] 后台线程在每个关键点更新 stage/progress;`get_status` 直接读 store,无锁(`json` 读写互斥即可)

### 2.3 进度推送 — stdio notifications(浏览器走 Phase 2.7 SSE)

**stdio MCP 客户端**(Claude Desktop / WorkBuddy / 任意 stdio):用 MCP `notifications/progress` 标准协议

- [ ] `start_watch` 调用方拿到 `progressToken`,后台线程在每个阶段结束发 `notifications/progress` with `{progressToken, progress, message}`
- [ ] 进度事件 schema:`{stage, progress, message, ts}`

**浏览器客户端**:**不**走裸 WebSocket(MCP 长连接 + CORS 不适合浏览器,详见 OpenMontage `openmontage-integration.md:53-58` 的论证)。走 Phase 2.7 BFF 的 SSE 端点。

### 2.4 外部 web 服务场景的具体接口

按 A 场景(外部 web 服务按视频 ID 拉帧),有两种实现路径,**MVP 选路径 2**:

**路径 1(不做)**:纯 HTTP/REST web service,与 MCP 平行。重复造轮子。

**路径 2(MVP 选这个)**:Phase 2.7 BFF 把 REST/SSE 翻译成 stdio MCP。**用户感觉像 REST,底层是 MCP。**

- [x] 在 `docs/MCP_CLIENT_COMPAT.md` 加一节:"Node.js web 服务接入示例",用官方 `@modelcontextprotocol/sdk` 的 `StdioClientTransport`,10 行代码示例 → 已落地 (§7.3)
- [ ] 加一节"Python web 服务示例",用 `mcp.client.session.stdio`
- [ ] 加一节"浏览器接入示例":`fetch('/api/watch/start', ...)` + `new EventSource('/api/watch/{video_id}/events')`,展示 SSE 自动重连
- [ ] 明确告诉用户:stdio 一个进程跑一个 web 服务,BFF 跑在另一进程;扩展时 BFF 可换成 `mcp.run_streamable_http_async()` 端点但默认不开

### 2.5 取消与超时

- [ ] 后台线程用 `threading.Event` 实现取消,每个阶段入口检查
- [ ] `start_watch` 接受 `timeout_seconds: int | None`,超时自动 cancel 并写 `error: "timeout"`
- [ ] 资源清理:取消时**不删** work_dir,留给 caller 决定;`delete_session` 才真删

### 2.6 重组管线路由到 OpenMontage_Voicebox(MVP,目标 ~2 天)

**硬约束:v2+ 禁止任何代码路径直接 shell 调 `npx remotion render` / `ffmpeg` 渲染产物。** 所有"基于 /watch 产物再生成视频"的请求必须走 `/opt/OpenMontage_Voicebox/` 的 MCP server 或 tool registry,理由见 `docs/todo.md` 末尾的对比表。

#### 2.6.1 旧脚本退役 + 守卫

> **Status (2026-08-23, final)**: **Disabled again, restoring original Phase A park state.** Briefly re-enabled in commit `73c92da` (mv *.py_tmp → *.py) by user override, then reverted on the same day. The four §2.6.1 conditions remain open — they are regression-prevention work, not re-enable gates. To re-enable in the future: complete the four items below, then `mv *.py_tmp *.py`.

> **Status (2026-08-23, superseded)** ~~Re-enabled by user override.~~ Briefly re-enabled in `73c92da` then reverted. The `.py_tmp` files were accidentally deleted in commit `0538676` (Phase 2.x side-effect), which closed the documented re-enable path. On 2026-08-23 the user first restored the files and renamed them back to `.py` so the canonical filenames work, then later the same day reversed course and renamed back to `.py_tmp`. The 4 conditions below remain open — they are now **regression-prevention work** rather than re-enable gates.


- [ ] `skills/watch/scripts/watch_to_remotion.py`:删除 `subprocess.run(["npx", "remotion", ...])` 路径(`watch_to_remotion.py:101` 附近的 `ffprobe` 调用可以保留——它只是探测时长,不渲染);脚本改为 **adapter**:把 watch 产物(VTT + frames + video)打包成 OpenMontage 可消费的输入清单,调用 OpenMontage 的 MCP tool `execute_tool(tool_name="video_compose", inputs={...})` 提交
- [ ] `watch_to_remotion_smart.py`:同样的 adapter 化改造;但**保留**它的 LLM-driven 选择能力——作为 OpenMontage 提交前的"预分析"(选 pipeline、调 style、挑场景),分析结果作为 inputs 透传
- [ ] 加 `OPENMONTAGE_REQUIRED=1` env guard:当 `OPENMONTAGE_PATH` 未设置或 OpenMontage MCP 不可达时,两个脚本必须 `SystemExit("recomposition requires OpenMontage_Voicebox at /opt/OpenMontage_Voicebox; set OPENMONTAGE_PATH")`,**不允许 fallback 到本地 npx**
- [ ] `tests/test_remotion_guard.py`:断言上述脚本在任何 env 下都不会 spawn `npx remotion` 子进程(monkeypatch subprocess.run)

#### 2.6.2 MCP 暴露 `recompose` tool

- [ ] `mcp_server.py` 注册新 tool `recompose(video_id, pipeline, style="clean-professional", ...)`,内部通过 stdio 调 OpenMontage MCP server 的 `execute_tool` 提交项目
  - `pipeline` 接受 `["clip-factory", "documentary-montage", "podcast-repurpose", "localization-dub", "hybrid", "screen-demo"]` 中任一值(MVP 至少支持前两个,其余标 out-of-scope-本机)
  - **GPU-free 约束**:本机无 GPU,recompose tool 必须拒绝任何 GPU-only pipeline(FLUX / Kling / local_diffusion / hunyuan_video / wan_video / cogvideo_video),提交时校验 inputs.pipeline 不在禁止列表,否则 ToolError
- [ ] `recompose` 返回 `{project_id, status: "submitted", render_url?}`,渲染产物由 OpenMontage 的 Backlot (`python -m backlot open <project-id>`) 跟踪
- [ ] 进度通过 Phase 2.7 BFF 的 SSE 通道推送,事件 schema 复用 stage 字段(`stage: "submit" | "compose" | "render" | "done" | "error"`)
- [ ] 调用方传入的 `user_openid` 必须随 inputs 一起透传给 OpenMontage adapter(由那边落到 `projects/users/<user_openid>/`)

#### 2.6.3 OpenMontage 侧的对接约定

- [ ] **本仓库只写文档**:在 `OpenMontage_Voicebox/docs/claude-video-integration.md` 写完整的跨仓集成文档(模板参考同仓 `comfyui-adapter-plan.md` 的结构:背景 / 架构图 / 集成模型 / 数据流 / 配置 / 测试)。文档要列清楚:
  - claude-video 侧 product context(产物结构、video_id 语义、user_openid 透传约定)
  - OpenMontage 侧需要做的代码改动清单(新增 `tools/external/claude_video.py` BaseTool,签名、inputs schema、产物落点)
  - GPU-free pipeline 白名单 + 黑名单
  - 用户隔离约定(与 `web-multiuser-auth.md` 的 `projects/users/<user_id>/` 模型对齐)
  - 微信服务号 OAuth 复用建议
  - 端到端冒烟测试脚本框架
  - 末尾留 issue list 给 OpenMontage owner 接手
- [ ] 这部分**实际代码改动**落在 OpenMontage 仓库,不在本仓库 scope;等那边 owner 实施后,本仓库 recompose tool 的 ToolError 信息要更新成新 adapter tool name

### 2.7 BFF (FastAPI REST + SSE) 给浏览器客户端 (MVP,目标 ~2 天)

**为什么不直接 WebSocket**:MCP 长连接 + CORS 不适合浏览器(参考 OpenMontage `openmontage-integration.md:53-58`)。OpenMontage 自家的做法是 Go BFF(`frameflow/bff/`)把 REST 转 MCP JSON-RPC,我们 Python 侧用 FastAPI 复制同样的模式。

- [ ] 新增 `skills/watch/scripts/bff.py`:FastAPI app,单进程
  - `POST /api/watch/start` body `{source, video_id?, user_openid?}` → 内部 spawn stdio MCP 客户端调 `start_watch`,返回 `{video_id, status: "running"}`
  - `GET  /api/watch/{video_id}/status` → 内部调 `get_status`,透传返回
  - `GET  /api/watch/{video_id}/frame/{filename}` → 内部调 `read_frame`,字节流回
  - `GET  /api/watch/{video_id}/mask/{filename}` → 同上
  - `GET  /api/watch/{video_id}/events` → **SSE**,内部订阅 MCP `notifications/progress` 并转发为 `data: {json}\n\n`
  - `POST /api/watch/{video_id}/cancel` → 调 `cancel_watch`
  - `POST /api/recompose` body `{video_id, pipeline, style?, user_openid?}` → 调 `recompose`
- [ ] **stdio MCP 子进程管理**:BFF 启动时 spawn `python3 mcp_server.py`,持久化一个 stdio 会话,所有 tool call 通过 `mcp.client.session.stdio` 复用同一连接(JSON-RPC over stdio 是有状态的,不能每次新建)。或者用进程内 asyncio queue 串行化请求。
- [ ] **CORS**:默认只允许 `http://localhost:*` + `tauri://`(与 OpenMontage 一致),通过 env `WATCH_BFF_CORS_ORIGINS` 覆盖
- [ ] **鉴权**:所有 `/api/*` 路由挂 `Depends(require_user)`(由 2.8 提供);未登录返回 `401 {error: "not_authenticated"}`,前端跳 `/auth/wechat`
- [ ] **端口**:默认 `WATCH_BFF_PORT=8910`;与 OpenMontage 的 8900 区分;启动失败时明确报端口占用,不打印 traceback
- [ ] 测试 `tests/test_bff.py`:用 `httpx.AsyncClient + ASGITransport` 跑全流程,断言 SSE 事件流含 `stage=download` / `frames` / `transcribe` / `done` 的 progression

### 2.8 微信服务号 OAuth 流接入 session store (MVP,目标 ~1 天)

**复用 OpenMontage 现有方案**:见 `OpenMontage_Voicebox/docs/doc-wechat-open-platform-oauth.md`(实际采用服务号方案)+ `web-multiuser-auth.md`(用户隔离模型)。本仓库单独走一遍,而不是依赖 OpenMontage 的 OAuth,因为我们 MVP 阶段要让本仓库独立可跑。

- [ ] **配置**(从 `~/.config/watch/.env` 读,与现有 config.py 集成):
  ```dotenv
  WECHAT_MP_APP_ID=服务号 AppID
  WECHAT_MP_APP_SECRET=服务号 AppSecret
  WECHAT_MP_REDIRECT_URI=https://your-domain/auth/wechat/callback
  WATCH_BFF_PUBLIC_URL=https://your-domain
  WATCH_BFF_COOKIE_SECURE=true
  ```
  未配置时 `/auth/wechat/login` 返回 **503 配置错误**,不允许 fallback 到任何共享 token(参考 `web-multiuser-auth.md` 的同款硬约束)
- [ ] **三个路由**:
  - `GET  /auth/wechat/login?redirect=<前端回跳路径>` 创建一次性 10 分钟 OAuth state(存 `users.sqlite3` SHA-256 摘要),302 跳微信授权页
  - `GET  /auth/wechat/callback?code=&state=` 校验 state(code 一次性,用后即删),code 换 openid+unionid(用 `/sns/oauth2/access_token`),建 HttpOnly + SameSite=Lax + Secure 的 `WATCH_SESSION` cookie(value = session_id)
  - `POST /auth/logout` 撤销 server-side session
- [ ] **session 存储**:`~/.cache/watch-mcp/users.sqlite3`,schema:
  ```sql
  CREATE TABLE users (openid TEXT PRIMARY KEY, unionid TEXT, created_at, last_seen);
  CREATE TABLE sessions (id TEXT PRIMARY KEY, openid TEXT, expires_at, created_at);
  CREATE TABLE oauth_states (state_hash TEXT PRIMARY KEY, expires_at);  -- 用后即删
  ```
- [ ] **依赖中间件** `require_user(request) -> openid`:读 cookie → 查 sessions 表 → 校验未过期 → 续期;失败抛 401
- [ ] **`video_id ↔ user_openid` 强校验**:所有 MCP tool(`start_watch` / `get_status` / `get_results` / `recompose` / `delete_session`)接受可选 `user_openid` 参数;`session_store.py` 写记录时持久化 `user_openid`;读取时若 caller 传入 `user_openid`,必须等于记录值,否则拒绝(防止横向越权)。**同 video_id 复用 work_dir 必须同 user**(避免 A 用户的 URL 被 B 用户触发复用)

**Phase 2 完成标志**:
- web 服务可以在 30 行代码内启动 watch、订阅进度、按 video_id 拉帧
- 调用 `recompose` 后,产物出现在 `OpenMontage_Voicebox/projects/<id>/renders/final.mp4`,并能在 Backlot 里查看
- `pytest -q` 覆盖拆分 tool + 重组 + cancel + timeout + 同 video_id 增量 segment + remotion guard
- [x] `docs/MCP_CLIENT_COMPAT.md` 增加 node + python 完整示例 → 已落地 (§7.2 + §7.3)
- `grep -r "npx remotion" skills/` 在 main 分支返回 0 行(除了注释里说明"已禁用")

---

## 风险与权衡

| 风险 | 缓解 |
|---|---|
| 后台线程异常导致 session 永远卡在 `running` | `start_watch` 注册 watchdog:90s 无 stage 切换自动写 `error: "watchdog"` 并清理线程 |
| sessions.json 并发写损坏 | atomic write(temp + os.replace)+ fcntl flock |
| BFF 端口被占 | env var `WATCH_BFF_PORT=8910`(默认),启动失败时打印明确错误而非 traceback |
| BFF 进程崩溃导致 stdio MCP 子进程成孤儿 | BFF 启动时用 `preexec_fn=os.setsid` 把 MCP 子进程挂到新进程组,BFF exit 时 `os.killpg(SIGTERM)` 清理 |
| stdio MCP 子进程并发请求冲突(JSON-RPC 必须串行) | BFF 内用 `asyncio.Lock` 串行化所有 tool call,接受 ~50ms 排队延迟 |
| `watch.run()` 当前会阻塞到结束,后台化需要重构成 yield stage | 不重构 —— 包一层 `threading.Thread(target=watch_mod.run, args=(ns,))` 即可,阶段间通过 polling sessions.json 暴露状态 |
| 微信服务号 OAuth 未配置时静默放过 | `/auth/wechat/login` 路由在缺 env 时返回 503 明确错误,前端可显示"管理员未配置登录";不 fallback 到任何共享 token |
| OpenMontage MCP 不可达 | recompose tool 启动时 health-check,失败返回 ToolError 列出 OPENMONTAGE_PATH 和可达性测试命令;BFF 启动时若要 enable recompose 路由,需先连上 OpenMontage |

---

## 不做(明确 out-of-scope)

- 多用户隔离 / 权限模型(MVP 单机单用户,但 Phase 2.8 起为微信用户预留 openid 占位 + session 关联)
- 跨机器 session 共享(留作 cloud 版)
- 自动清理过期 session(留给 cron / 外部清理)
- 把 `download` / `frames` / `transcribe` 各自独立暴露为 MCP tool(粒度过细,Phase 2 的拆分粒度以"stage"为单位)
- **直接调本地 Remotion / FFmpeg 做最终渲染**(v2+ 一律走 OpenMontage_Voicebox)
- **GPU-required pipeline**:本机无 GPU,recompose tool 拒绝 `FLUX` / `Kling` / `local_diffusion` / `hunyuan_video` / `wan_video` / `cogvideo_video` 等任何需要 CUDA 的 provider;等 OpenMontage 跑在有 GPU 的机器上时再开放

---

## 附:为什么禁止直接调本地 Remotion

| 维度 | 本地 `npx remotion render` | `/opt/OpenMontage_Voicebox/` MCP |
|---|---|---|
| 调用方式 | subprocess 直调 CLI | MCP `execute_tool` / Python registry |
| Pipeline 选择 | 无 | 12 个 YAML pipeline 按内容自动选 |
| Tool 组合 | 仅 Remotion | Remotion + HyperFrames + FLUX + Kling + ElevenLabs + TTS... |
| 阶段编排 | 一次性 | stage director skills + meta skills |
| 审查 / checkpoint | 无 | reviewer / checkpoint-protocol |
| 产物追溯 | 临时目录 | `projects/<id>/` + Backlot storyboard |
| 多 provider 切换 | 不支持 | render_runtime 是锁定的、可审计的 |
| 决策日志 | 无 | append-only `decision_log` |

结论:**Remotion 只是 OpenMontage 工具箱里的一个渲染器。** 直接调它 = 把 24 个 tool 的平台降级成 1 个 CLI 调用。Phase 2.6 的约束保证本仓库不出现这种降级路径。
