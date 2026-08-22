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

- [ ] 写 `docs/MCP_CLIENT_COMPAT.md`,列出测试过的客户端:openclaw / Claude Desktop / MCP Inspector / 自写 stdio 客户端,每个记协议版本、最小复现命令

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
- [ ] 提供 `list_sessions` / `delete_session` 两个新 MCP tool,让外部可以清理磁盘

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

### 2.3 进度推送 — 两种传输都给

**stdio MCP 客户端**(Claude Desktop 之类):用 MCP `notifications/progress` 标准协议

- [ ] `start_watch` 调用方拿到 `progressToken`,后台线程在每个阶段结束发 `notifications/progress` with `{progressToken, progress, message}`
- [ ] 进度事件 schema:`{stage, progress, message, ts}`

**Web / 非 MCP 客户端**:WebSocket 端点

- [ ] `mcp_server.py` 增加 `mcp.run_streamable_http_async()` 入口,但**默认还是 stdio**(保持 Phase 1 兼容)
- [ ] 新增 `skills/watch/scripts/ws_progress.py`:独立进程,监听 `ws://localhost:<port>/progress/<video_id>`
- [ ] 进度事件由 session_store 同步推送:用 `asyncio.Queue` 桥接后台 watch 线程的写、ws handler 的读
- [ ] 鉴权:MVP 阶段用 `WATCH_WS_TOKEN` env var(从 `~/.config/watch/.env` 读),客户端连接时在 query string 或 header 携带

### 2.4 外部 web 服务场景的具体接口

按 A 场景(外部 web 服务按视频 ID 拉帧),有两种实现路径,**MVP 选路径 2**:

**路径 1(不做)**:纯 HTTP/REST web service,与 MCP 平行。重复造轮子。

**路径 2(MVP 选这个)**:外部 web 服务作为 MCP 客户端,通过 stdio 调 `start_watch` / `get_status` / `read_frame`。**用户感觉像 REST,但底层是 MCP**。

- [ ] 在 `docs/MCP_CLIENT_COMPAT.md` 加一节:"Node.js web 服务接入示例",用官方 `@modelcontextprotocol/sdk` 的 `StdioClientTransport`,10 行代码示例
- [ ] 加一节"Python web 服务示例",用 `mcp.client.session.stdio`
- [ ] 明确告诉用户:stdio 一个进程跑一个 web 服务,扩展时再换 HTTP transport

### 2.5 取消与超时

- [ ] 后台线程用 `threading.Event` 实现取消,每个阶段入口检查
- [ ] `start_watch` 接受 `timeout_seconds: int | None`,超时自动 cancel 并写 `error: "timeout"`
- [ ] 资源清理:取消时**不删** work_dir,留给 caller 决定;`delete_session` 才真删

### 2.6 重组管线路由到 OpenMontage_Voicebox(MVP,目标 ~2 天)

**硬约束:v2+ 禁止任何代码路径直接 shell 调 `npx remotion render` / `ffmpeg` 渲染产物。** 所有"基于 /watch 产物再生成视频"的请求必须走 `/opt/OpenMontage_Voicebox/` 的 MCP server 或 tool registry,理由见 `docs/todo.md` 末尾的对比表。

#### 2.6.1 旧脚本退役 + 守卫

> **现状 (2026-08-23)**: 两个 Remotion 脚本已经**重命名为 `.py_tmp`**(`watch_to_remotion.py_tmp`、`watch_to_remotion_smart.py_tmp`),作为**临时**禁用手段 — Python 不会 import 它们,SKILL.md / watch.py 也没有引用,所以暂时不会被误调。**下面四件事全部完成后,再把后缀 `_tmp` 去掉、重新启用**:


- [ ] `skills/watch/scripts/watch_to_remotion.py`:删除 `subprocess.run(["npx", "remotion", ...])` 路径(`watch_to_remotion.py:101` 附近的 `ffprobe` 调用可以保留——它只是探测时长,不渲染);脚本改为 **adapter**:把 watch 产物(VTT + frames + video)打包成 OpenMontage 可消费的输入清单,调用 OpenMontage 的 MCP tool `execute_tool(tool_name="video_compose", inputs={...})` 提交
- [ ] `watch_to_remotion_smart.py`:同样的 adapter 化改造;但**保留**它的 LLM-driven 选择能力——作为 OpenMontage 提交前的"预分析"(选 pipeline、调 style、挑场景),分析结果作为 inputs 透传
- [ ] 加 `OPENMONTAGE_REQUIRED=1` env guard:当 `OPENMONTAGE_PATH` 未设置或 OpenMontage MCP 不可达时,两个脚本必须 `SystemExit("recomposition requires OpenMontage_Voicebox at /opt/OpenMontage_Voicebox; set OPENMONTAGE_PATH")`,**不允许 fallback 到本地 npx**
- [ ] `tests/test_remotion_guard.py`:断言上述脚本在任何 env 下都不会 spawn `npx remotion` 子进程(monkeypatch subprocess.run)

#### 2.6.2 MCP 暴露 `recompose` tool

- [ ] `mcp_server.py` 注册新 tool `recompose(video_id, pipeline="clip-factory", style="clean-professional", ...)`,内部通过 stdio 调 OpenMontage MCP server 的 `execute_tool` 提交项目
- [ ] `recompose` 返回 `{project_id, status: "submitted", render_url?}`,渲染产物由 OpenMontage 的 Backlot (`python -m backlot open <project-id>`) 跟踪
- [ ] 进度通过 Phase 2.3 的 WebSocket 通道推送,事件 schema 复用 stage 字段(`stage: "submit" | "compose" | "render" | "done" | "error"`)

#### 2.6.3 OpenMontage 侧的对接约定

- [ ] 在 `OpenMontage_Voicebox/tools/video/`(或新建 `tools/external/claude_video.py`)加一个 adapter tool,接受 `{frames_dir, vtt_path, video_path, style, pipeline}`;把 watch 产物符号链接/拷贝到 `projects/<project-id>/assets/`,触发对应 pipeline
- [ ] 这部分改动落在 OpenMontage 仓库,不在本仓库 scope——但要在 `docs/todo.md` 留 cross-repo 链接,等那边有 owner 后跟踪

**Phase 2 完成标志**:
- web 服务可以在 30 行代码内启动 watch、订阅进度、按 video_id 拉帧
- 调用 `recompose` 后,产物出现在 `OpenMontage_Voicebox/projects/<id>/renders/final.mp4`,并能在 Backlot 里查看
- `pytest -q` 覆盖拆分 tool + 重组 + cancel + timeout + 同 video_id 增量 segment + remotion guard
- `docs/MCP_CLIENT_COMPAT.md` 增加 node + python 完整示例
- `grep -r "npx remotion" skills/` 在 main 分支返回 0 行(除了注释里说明"已禁用")

---

## 风险与权衡

| 风险 | 缓解 |
|---|---|
| 后台线程异常导致 session 永远卡在 `running` | `start_watch` 注册 watchdog:90s 无 stage 切换自动写 `error: "watchdog"` 并清理线程 |
| sessions.json 并发写损坏 | atomic write(temp + os.replace)+ fcntl flock |
| WebSocket 端口被占 | env var `WATCH_WS_PORT=8765`(默认),启动失败时打印明确错误而非 traceback |
| stdio + WebSocket 同时跑会双份开销 | Phase 2.3 默认只起 stdio;`--transport http` 时才起 WS |
| `watch.run()` 当前会阻塞到结束,后台化需要重构成 yield stage | 不重构 —— 包一层 `threading.Thread(target=watch_mod.run, args=(ns,))` 即可,阶段间通过 polling sessions.json 暴露状态 |

---

## 不做(明确 out-of-scope)

- 多用户隔离 / 权限模型(MVP 单机单用户)
- 跨机器 session 共享(留作 cloud 版)
- 自动清理过期 session(留给 cron / 外部清理)
- 把 `download` / `frames` / `transcribe` 各自独立暴露为 MCP tool(粒度过细,Phase 2 的拆分粒度以"stage"为单位)
- **直接调本地 Remotion / FFmpeg 做最终渲染**(v2+ 一律走 OpenMontage_Voicebox)

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
