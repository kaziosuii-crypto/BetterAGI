# 实时 VTuber 链路设计（BetterGI × Lumine × dsh）

> 状态：首版实施 · 2026-10-26 · 协议：[PROTOCOL.md](../../integrations/lumine/PROTOCOL.md) · 接入指南：[integrations/lumine/README.md](../../integrations/lumine/README.md) · 相关：[智能体适配器](agent-adapter.md)

## 1. 目标与边界

本链路把 BetterGI 已有的截图与模拟输入能力，接到一个低层玩家模型（Lumine）和一个高层导演模型（dsh）上，形成可持续运行的《原神》实况 VTuber：

- BetterGI 负责**看画面、发按键、做安全兜底**；
- Lumine 负责**每 200ms 的低层操作**；
- dsh 负责**路线指导、实况解说、人格与记忆**；
- 叠加层与离线渲染器负责**把解说变成观众看到的画面和声音**。

非目标与现有智能体适配器一致：不读写游戏内存、不抓包、不注入、不执行任意脚本、不切换账号、不捕获游戏窗口以外的桌面。实时链路只是把同样的截图与模拟键鼠以更低延迟组织起来。

## 2. 总体架构

```text
                         ┌──────────────────────────────────────────┐
                         │              dsh（导演 / 解说）            │
                         │  HTTP / MCP / WS：画面、指导、解说、记忆    │
                         └───────────────┬──────────────────────────┘
                                         │ 双向 HTTP / MCP / WS
                    ┌────────────────────▼─────────────────────┐
                    │        BetterGI Live Hub（C# 进程内）       │
                    │  截图 5～10fps · JPEG · 动作执行 · 时间线    │
                    │  loopback + Bearer · 停止开关 · 按键白名单   │
                    └───────┬───────────────────────┬──────────┘
                            │ 双向 WS：画面/指导/解说/动作 │ WS 画面/解说/动作状态
                            │                       ▼
                            │              ┌──────────────────────┐
                            │              │  VTuber 叠加层（OBS） │
                            │              │  index.html 浏览器源  │
                            │              └──────────────────────┘
                            │
                    ┌───────▼─────────────────────┐
                    │   Lumine Bridge（本机 Python）│
                    │ 5Hz 抽帧 · 动作校验 · 重连    │
                    │ 可选 ffmpeg 录制             │
                    └───────┬─────────────────────┘
                            │ WS：画面 + 指导 / 动作块
                    ┌───────▼─────────────────────┐
                    │ AutoDL Lumine Player Server  │
                    │ vLLM + Lumine-Agent-VL-7B    │
                    │ 每帧输出 200ms 动作块         │
                    └─────────────────────────────┘

    离线：timeline JSON ──▶ render_vtuber.py ──▶ ASS 字幕 / 头像 / TTS ──▶ MP4
```

BetterGI 侧的数据面和控制面只经过本机 loopback；Bridge 到 AutoDL 的网络只传输 JPEG 画面、指导文本和动作块，不传输游戏内存、封包或任意脚本。

## 3. 角色与职责

| 角色 | 运行位置 | 职责 | 不做什么 |
|---|---|---|---|
| **dsh 导演 / 解说** | 本机或云端 LLM 运行时 | 读取最新画面与动作状态，写一行可执行指导；按六类维度生成解说；维护会话记忆和检查点；决定何时开始/停止直播 | 不直接读游戏内存、不抓包、不执行任意工具；Lumine 在线时不应同时抢占动作控制 |
| **Lumine 低层玩家** | AutoDL GPU | 接收 1280×720 JPEG 和指导文本，输出 200ms 动作块（6 个 33ms 键块）；在菜单/UI 场景输出绝对坐标 | 不做长期规划、不写解说、不执行脚本 |
| **BetterGI Live Hub** | BetterGI 进程内 | 按 fps 截取游戏窗口、编码 JPEG、广播帧；接收并执行动作块；维护指导/解说时间线；鉴权、停止开关、按键白名单、失去前台释放 | 不读内存、不抓包、不注入；不替模型做语义决策 |
| **Lumine Bridge** | 本机 Python | 连接 BetterGI 与 AutoDL；5Hz 抽最新帧；把 AutoDL 动作转成 BetterGI 动作；协议字段白名单校验；断线重连；可选录制 | 不解析/执行模型任意文本；不启动任意 shell（ffmpeg 录制除外） |
| **叠加层 / 渲染器** | OBS / 离线 | 浏览器叠加层显示指导、解说、按键、队列与饥饿；离线渲染器把时间线做成 ASS 字幕、头像和 TTS 视频 | 不参与控制回路，不修改游戏 |

## 4. 数据流与时序

### 4.1 一帧画面到一次操作

1. BetterGI 截图循环按 `fps`（默认 10，范围 2～15）截取当前游戏窗口，编码为 JPEG（默认质量 70、最大宽度 1280），递增 `frameSeq`。
2. 同一帧通过 `/ws/v1/agent/live` 广播给所有客户端，并附带当前 `guidance`、`capture` 区域和 `runtime` 状态。
3. Bridge 收到帧后只保留最新一帧；按 5Hz（默认）抽帧，把最新帧和 `guidance` 发给 AutoDL。旧帧被覆盖并计入 `frameDrops`。
4. AutoDL Player Server 每个连接只保留一个待推理帧；推理忙时新帧覆盖旧的 `pending`，旧帧计入 `dropped`。同一连接同一时刻最多一个推理请求。
5. vLLM 返回动作文本后，服务端解析成 1～6 个键块（`chunks`）、可选绝对坐标和 `thought`，通过 `action` 消息返回。
6. Bridge 只转发协议允许的 `seq`、`chunks`、`absolute`、`thought`；无法解析的动作不转发，保持安全停顿。
7. BetterGI 把每个键块按 33ms 排定到期时间，按“目标按键集合”做差量 `KeyDown/KeyUp`，鼠标用 `MoveMouseBy`，绝对坐标按 1280×720 输出帧归一化到当前游戏画面。
8. 动作状态通过 `action_state` 广播；dsh 与叠加层据此显示按键、队列深度和饥饿状态。
9. dsh 在独立节奏上读取最新帧，写新的 `guidance` 或 `commentary`；Bridge 立即把指导转发给 AutoDL，后续帧也会携带最新指导。
10. 指导、解说、启动/停止事件进入时间线，并可写入会话事件文件；叠加层实时显示，离线渲染器稍后消费时间线。

### 4.2 帧率与动作节奏

| 环节 | 默认值 | 说明 |
|---|---:|---|
| BetterGI 截帧/广播 | 10 fps（2～15） | 每帧附带 `action_state`；JPEG 默认 1280 宽、质量 70 |
| Bridge 转发 AutoDL | 5 fps | `FramePump` 只保留最新帧，按 200ms 间隔取样 |
| Lumine 动作块 | 200ms | 6 个 33ms 键块；每个键块最多 4 个键 |
| BetterGI 执行粒度 | 33ms | 后台 5ms 轮询到期键块；按键差分 |
| 动作队列上限 | 20 个排定键块（约 660ms） | 超出丢弃最旧键块；队列不是延迟缓冲 |
| 空闲释放 | 250ms | 队列为空且仍有按键时释放全部按键，`starved=true` |
| 导演轮询 | 4s（默认） | 指导变化、按键开始/停止可提前触发；最小间隔 2s |

### 4.3 延迟预算

下表是**设计预算**，不是 AutoDL 实机测量值。实际值受网络、GPU、vLLM 版本和画面复杂度影响。

| 阶段 | 估算 | 说明 |
|---|---:|---|
| 截帧 + JPEG 编码 | 10～60ms | 1080p 缩到 1280 宽、质量 70 |
| BetterGI → Bridge（本机 WS） | 1～10ms | base64 体积较大，但仍是 loopback |
| Bridge 5Hz 取样等待 | 0～200ms | 平均约 100ms；帧率越低等待越长 |
| 本机 → AutoDL 网络 | 20～100ms | 取决于地区和是否走 SSH 隧道 |
| vLLM 7B 推理 | 100～400ms（估算） | 128 max tokens、单帧、24GB 级 GPU；未实测 |
| AutoDL → Bridge → BetterGI | 20～100ms | 动作块 JSON 很小 |
| 动作块执行 | 0～200ms | 首个键块立即到期，后续每 33ms |
| **端到端（画面到游戏响应）** | **约 0.4～1.2s** | 目标控制在 1s 内；超过 1.5s 时操作会明显滞后 |

降低延迟优先调低推理时间（分辨率、max tokens、量化、GPU），而不是增大动作队列。队列越长，模型看到的是越旧的画面，动作越容易错位。

### 4.4 队列、饥饿与安全停顿

- **只保留最新**：Bridge 与 AutoDL 都采用“最新帧覆盖旧帧”，不排队追赶。推理慢时表现为丢帧和动作频率下降，而不是延迟无限增长。
- **动作队列**：BetterGI 最多保留 20 个已排定键块；超过后丢弃最旧键块并增加 `dropped` 计数。`LiveActionAckDto.accepted` 只表示入队成功，不表示没有被丢弃。
- **空闲释放**：超过 250ms 没有新动作、队列为空且仍有按键按下时，执行器释放全部按键并把 `starved` 置为 true。这样模型停顿或网络断开时角色不会一直向前跑。
- **失去前台/最小化**：本地 Win32 窗口在 `requiresForeground=true` 且失去前台时，执行器立即释放按键并暂停消费队列；队列保留，回到前台后到期的键块可能集中执行，因此切换窗口前应主动 `stop_actions` 或停止直播。
- **停止开关**：用户按下“停止智能体输入”、关闭智能体控制、停止运行环境或调用 `/live/stop` 时，立即释放按键并清空队列。关闭智能体控制会异步停止实时会话。
- **无动作可执行**：模型没有输出合法动作块时，Bridge 不发送任何 `actions`，BetterGI 在 250ms 后释放按键；`starved` 变为 true。
- **迟到动作**：BetterGI 不比较 `forFrameSeq` 与当前帧，Bridge 也不按帧龄丢弃动作；推理很慢时，迟到的 200ms 动作仍会执行。应通过降低分辨率/推理负载来控制延迟，不要假设服务端会自动丢弃旧动作。

## 5. 协议摘要

完整字段、示例与动作格式见 [PROTOCOL.md](../../integrations/lumine/PROTOCOL.md)。本节只列实现中实际使用的入口。

### 5.1 BetterGI Live Hub HTTP

基地址 `http://127.0.0.1:30648`，所有请求带 `Authorization: Bearer <token>`。HTTP Live 端点要求 `HttpApiEnabled && AgentControlEnabled`；启动后关闭智能体控制时，`start`、`guidance`、`commentary`、`timeline`、`actions` 会返回 `403 AGENT_CONTROL_DISABLED`，`status`、`stop`、`frame` 仍可调用；重启前路由仍存在。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/agent/live/start` | 启动实时会话；需要截图器已运行；重复调用返回当前状态 |
| `POST` | `/api/v1/agent/live/stop` | 停止会话、释放按键、清空动作队列；返回 `204` |
| `GET` | `/api/v1/agent/live/status` | 运行状态、帧序号、客户端数、按键、队列/饥饿、时间线序号 |
| `GET` | `/api/v1/agent/live/frame` | 最新 JPEG（二进制）；响应头 `X-Bgi-Live-Seq`、`X-Bgi-Live-Timestamp`、`X-Bgi-Capture-*`；无帧返回 `404 LIVE_FRAME_NOT_FOUND` |
| `POST` | `/api/v1/agent/live/guidance` | 设置指导文本（1～500 字符）；body `{text, source}` |
| `POST` | `/api/v1/agent/live/commentary` | 追加解说；body `{kind, text, emotion, frameSeq, meta}` |
| `GET` | `/api/v1/agent/live/timeline?afterSeq=0&limit=200` | 增量读取时间线；`limit` 限制 1～500 |
| `POST` | `/api/v1/agent/live/actions` | 注入一个动作块；body 为 `LiveActionBlockDto`，返回 `LiveActionAckDto`；即使 `accepted=false` 也返回 200 |

启动请求：

```json
{
  "sessionId": "vtuber-20261026",
  "fps": 10,
  "quality": 70,
  "maxWidth": 1280,
  "recordFrames": false,
  "recordFps": 1
}
```

状态响应（camelCase）：

```json
{
  "running": true,
  "sessionId": "vtuber-20261026",
  "fps": 10,
  "frameSeq": 1234,
  "lastFrameAtUtc": "2026-10-26T14:00:00Z",
  "clients": 2,
  "latestGuidance": "沿着道路前进，注意体力。",
  "pressedKeys": ["W"],
  "actionQueueDepth": 3,
  "starved": false,
  "appliedActions": 120,
  "droppedActions": 2,
  "timelineSeq": 45
}
```

### 5.2 BetterGI Live Hub WebSocket

`ws://127.0.0.1:30648/ws/v1/agent/live?access_token=<token>`。端点映射只依赖 `AgentControlEnabled`；只启用智能体控制时外部访问宿主也会启动，并仅提供实时 WebSocket；此时服务仍会生成/复用访问令牌，可在设置页复制。HTTP Live 仍需要 `HttpApiEnabled`，MCP 工具仍需要 `McpEnabled`；因此启动直播会话仍需 HTTP `/live/start` 或 MCP `bgi_live_start` 之一，WS 本身只负责附着和收发。也接受 `Authorization: Bearer` 请求头。浏览器/OBS 无法设置请求头，因此允许查询参数令牌。单条消息最大 4MB；服务端每个客户端的有界队列为 32 条，满时丢弃最旧消息。

服务端 → 客户端：

| `type` | 主要字段 | 说明 |
|---|---|---|
| `hello` | `sessionId`、`fps`、`width`、`height`、`actionIntervalMs`、`chunksPerAction` | 连接建立时发送；`height` 当前可能为空 |
| `frame` | `seq`、`timestampUtc`、`jpeg`、`width`、`height`、`capture`、`runtime`、`guidance` | 每帧广播；`uiCategory` 已预留但当前 Live 采集不填充 |
| `guidance` | `seq`、`timestampUtc`、`text`、`source`、`guidance` | 指导变化时广播 |
| `commentary` | `seq`、`timestampUtc`、`kind`、`text`、`emotion`、`frameSeq`、`meta` | 解说追加时广播；`meta.internal=true` 表示内部思考，只进会话文件 |
| `action` | `seq`、`timestampUtc`、`block`、`text` | 已接受的动作块回显，含 `chunks`、鼠标位移与模型 `thought`；供录制器还原操作 |
| `action_state` | `keys`、`queueDepth`、`starved`、`applied`、`dropped` | 每帧附带；不写入时间线 |
| `pong` | `timestampUtc` | 心跳响应 |
| `error` | `code`、`message` | 协议/执行错误 |
| `live.stopped` | `sessionId` | 会话停止时广播 |

客户端 → 服务端：

| `type` | 主要字段 | 说明 |
|---|---|---|
| `guidance` | `text`、可选 `meta.source` | 设置指导；来源默认 `dsh` |
| `commentary` | `kind`、`text`、`emotion`、`frameSeq`、`meta` | 追加解说 |
| `actions` | `seq`、`chunks`、`absolute`、`thought` | 注入 1～6 个键块；每块最多 4 个键 |
| `stop_actions` | 无 | 清空队列并释放按键 |
| `ping` | 无 | 心跳 |

动作块示例：

```json
{
  "type": "actions",
  "seq": 1234,
  "chunks": [
    {"dx": 5, "dy": 0, "scroll": 0, "keys": ["W"]},
    {"dx": 0, "dy": 0, "scroll": 0, "keys": ["W"]},
    {"dx": 0, "dy": 0, "scroll": 0, "keys": ["W", "Shift"]},
    {"dx": 0, "dy": 0, "scroll": 0, "keys": ["W", "Shift"]},
    {"dx": 0, "dy": 0, "scroll": 0, "keys": []},
    {"dx": 0, "dy": 0, "scroll": 0, "keys": []}
  ],
  "absolute": null,
  "thought": "沿着道路向前"
}
```

### 5.3 BetterGI Live Hub MCP 工具

MCP 地址 `http://127.0.0.1:30648/mcp`（Streamable HTTP，无状态）。工具要求 `McpEnabled && AgentControlEnabled`。DTO 工具的业务异常以 MCP 协议错误 `CODE: message` 返回；`bgi_live_frame` 无帧时返回 `isError=true` 的工具结果。关闭智能体控制后，`bgi_live_status`、`bgi_live_stop`、`bgi_live_frame` 仍可调用，其余 Live 工具会抛出 `AGENT_CONTROL_DISABLED`。

| Tool | 主要参数 | 返回 |
|---|---|---|
| `bgi_live_start` | `fps=10`、`maxWidth=1280`、`quality=70`、`recordFrames=false`、`sessionId?` | `LiveStatusDto` |
| `bgi_live_stop` | 无 | 无 |
| `bgi_live_status` | 无 | `LiveStatusDto` |
| `bgi_live_frame` | 无 | 文本元数据 + JPEG 图像内容块 |
| `bgi_live_set_guidance` | `text`（1～500 字符） | 时间线事件 |
| `bgi_live_add_commentary` | `kind`、`text`、`emotion?` | 时间线事件 |
| `bgi_live_timeline` | `afterSeq=0`、`limit=200` | 时间线事件数组 |
| `bgi_live_actions` | `chunks`、`absolute?`、`seq?` | `LiveActionAckDto` |

记忆与检查点继续使用 `bgi_agent_memory_set`、`bgi_agent_memory_get`、`bgi_agent_checkpoint_create`、`bgi_agent_session_resume` 等现有工具。

### 5.4 AutoDL Player Server 协议

| 方向 | 消息 | 说明 |
|---|---|---|
| Bridge → 服务端 | `hello` | `sessionId`、`fps=5`、`protocol=lumine-live-v1` |
| Bridge → 服务端 | `frame` | `seq`、`tsMs`、`jpeg`（base64）、`guidance` |
| Bridge → 服务端 | `guidance` | 单独更新指导 |
| Bridge → 服务端 | `reset` | 清空待推理帧、指导与统计 |
| Bridge → 服务端 | `ping` | 心跳 |
| 服务端 → Bridge | `hello` | `model`、`fps`、`protocol`、`mock`、`chunksPerAction`、`actionIntervalMs` |
| 服务端 → Bridge | `action` | `forFrameSeq`、`action`、`chunks`、`absolute`、`thought`、`latencyMs`、`raw` |
| 服务端 → Bridge | `status` | `queue`、`inflight`、`dropped`、`lastLatencyMs` |
| 服务端 → Bridge | `error` | `code`、`message`、可选 `forFrameSeq` |
| 服务端 → Bridge | `pong` | 心跳响应 |

HTTP 辅助端点：`GET /health`（进程/模型/mock）、`GET /status`（会话、队列、丢弃、延迟）。**这两个端点当前不校验令牌**，只应绑定 loopback 或由安全组限制来源。

鉴权：WebSocket `/ws/player` 从查询参数 `token`/`access_token` 或 `Authorization: Bearer` 读取令牌；配置了 `LUMINE_SERVER_TOKEN` 时必须匹配，令牌为空时放行。单帧 base64 上限 2MiB，WS 消息上限 4MiB，指导最长 4000 字符。

### 5.5 字段命名说明

PROTOCOL.md 中的示例使用 `ts`、`frameWidth` 等简写；当前 C# 使用 `System.Text.Json` 的 camelCase，实际字段是 `timestampUtc`、`width`、`height`。叠加层同时兼容 `width` / `frameWidth`；Bridge 的时间戳字段读取 `ts`，缺失时使用接收时间兜底。接入时以实际 JSON 字段为准，必要时两种命名都兼容。

## 6. dsh 导演循环

一次完整的直播循环：

```text
status → live start → observe frames → guidance → player acts
       → frames / commentary return → memory / checkpoint → 下一轮
```

1. **status**：`bgi_agent_status` 确认智能体控制、运行环境和停止开关；`bgi_live_status` 确认是否已有直播。
2. **live start**：如果截图器未运行，先 `bgi_agent_runtime_start` 或在 BetterGI 首页启动；再 `bgi_live_start`（HTTP `POST /live/start`）。
3. **observe frames**：用 `bgi_live_frame` 或订阅 `/ws/v1/agent/live` 读取最新 JPEG；记录 `seq` 与 `uiCategory`（当前可能为空）。
4. **guidance**：根据画面写一行具体、可立即执行的指导，例如“沿海岸线向右，先别下水”，用 `bgi_live_set_guidance` 或 WS `guidance`。Bridge 会把它转发给 Lumine。
5. **player acts**：Lumine 输出 200ms 动作块；BetterGI 执行并广播 `action_state`。dsh 不需要逐块介入。
6. **frames / commentary return**：继续读帧，观察动作结果；用 `bgi_live_add_commentary` 追加 `reaction`、`plan`、`emotion`、`lore`、`chat` 或 `result`。
7. **memory / checkpoint**：用 `bgi_agent_memory_set` 记录路线、目标、最近错误；在进入秘境、对话、领取奖励前用 `bgi_agent_checkpoint_create`。
8. **stop**：结束直播时 `bgi_live_stop`；需要手动接管时先停止 Bridge/Lumine，再考虑 `bgi_live_actions` 或原有 `bgi_agent_act`。

示例工具序列：

```text
bgi_agent_status
bgi_live_status
bgi_agent_runtime_start          # 截图器未启动时
bgi_live_start(fps=10, maxWidth=1280, quality=70, sessionId="vtuber")
bgi_live_frame
bgi_live_set_guidance(text="沿道路前进，注意左侧丘丘人")
bgi_live_frame
bgi_live_add_commentary(kind="reaction", text="左边草丛有动静！", emotion="surprised")
bgi_live_timeline(afterSeq=0, limit=200)
bgi_agent_memory_set(values={"route":"蒙德城北","step":12})
bgi_agent_checkpoint_create(name="before-camp", note="进入营地前")
bgi_live_stop
```

注意：

- Lumine 在线时，dsh 的职责是指导与解说，不要同时用 `bgi_live_actions` 发动作，否则两套控制会互相覆盖。
- dsh 写指导时应保持短句、可执行，避免长段落；引导文本会原样进入 Lumine 的提示词。
- 解说的“内部思考”和“朗读台词”严格分开：`thought` 只写日志，`commentary[].text` 才会进入时间线并被朗读。
- 直播时间线是排障与离线渲染的共同数据源，重要操作前后应记录检查点。

## 7. 解说模型

### 7.1 六类解说

| `kind` | 用途 | 典型触发 |
|---|---|---|
| `reaction` | 即时反应 | 敌人出现、宝箱开启、突然受击 |
| `plan` | 计划/路线/战斗策略 | 进入新区域、选择路线、准备战斗 |
| `emotion` | 情绪表达 | 紧张、期待、可惜、开心 |
| `lore` | 世界观补充 | 到达地区、遇到角色、看到元素现象；不剧透未到达剧情 |
| `chat` | 与观众互动 | 回应想象中的弹幕、提问、投票 |
| `result` | 操作结果复盘 | 战斗结束、解谜完成、失误之后 |

`kind` 与 `emotion` 都有白名单；未知值在导演侧回退为 `reaction` / `calm`，在 BetterGI 侧返回 `INVALID_REQUEST`。情绪常用值：`calm`、`excited`、`surprised`、`worried`、`happy`；叠加层还显示 `sad`、`angry`、`curious`、`nervous`、`proud`、`thinking`、`amused`、`determined`、`tired`、`neutral`。

### 7.2 内部思考与朗读台词

参考导演 `live_director.py` 要求 LLM 只输出一个 JSON 对象：

```json
{
  "thought": "内部推理，不会朗读",
  "guidance": "一行给下游玩家模型的可执行指导",
  "commentary": [
    {"kind": "reaction", "text": "要朗读的台词", "emotion": "surprised"}
  ],
  "memory_patch": {"键": "值"},
  "tool_calls": [
    {"name": "bgi_agent_memory_set", "arguments": {"values": {"键": "值"}}}
  ]
}
```

- `thought`：内部推理，只写日志，不发送到 BetterGI，也不显示在叠加层。
- `guidance`：给 Lumine 的指导，不是给观众看的台词；变化时才发送。
- `commentary[].text`：真正朗读/显示的台词；默认每轮最多 4 条，每条最多 80 字符。
- `memory_patch`：合并写入会话记忆；值为 `null` 的键会被删除。
- `tool_calls`：只允许 `bgi_agent_memory_set`、`bgi_agent_memory_get`、`bgi_agent_checkpoint_create`、`bgi_live_status`、`bgi_live_set_guidance` 五个白名单工具；其他名称直接拒绝。

导演默认每 4 秒轮询一次；指导变化或按键从空闲变为有输入（或相反）时可以提前触发，但两次轮询至少间隔 2 秒。提示词只带最近 8 条解说和最多 4000 字符的记忆摘要，避免上下文无限增长。

### 7.3 人格

人格文件是 JSON 对象，字段包括：

| 字段 | 说明 |
|---|---|
| `name` | 角色名，显示在叠加层名牌 |
| `personality` | 性格描述 |
| `tone` | 语气 |
| `language` | 语言，默认简体中文 |
| `catchphrases` | 口头禅数组，偶尔自然使用 |
| `content_dimensions` | 六类解说的具体说明，可覆盖默认维度 |

示例见 [persona.example.json](../../integrations/lumine/persona.example.json)。人格只影响解说与指导风格，不改变安全边界和动作白名单。

### 7.4 时间线 schema

`GET /api/v1/agent/live/timeline` 返回 `LiveTimelineEventDto` 数组：

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | int64 | 会话内递增序号 |
| `timestampUtc` | ISO 8601 | 事件时间 |
| `type` | string | `guidance`、`commentary`、`live.started`、`live.stopped` |
| `kind` | string? | 解说类型 |
| `text` | string? | 指导或解说文本 |
| `emotion` | string? | 情绪标签 |
| `frameSeq` | int64? | 关联帧序号 |
| `meta` | object? | 附加元数据 |

实现说明：

- `action_state` 通过 WebSocket 实时广播，但**不写入时间线**；需要离线分析动作节奏时，应在客户端自行记录。
- `live.started` 当前把 `fps`/`maxWidth` 摘要放在 `emotion` 字段（历史实现），消费方不要把它当情绪标签。
- 时间线在内存中最多保留 5000 条；同时尽力追加到会话事件的 `live.*` 记录。视频时间轴以第一条 `live.started` 的 `timestampUtc` 为 0。

### 7.5 VTuber 叠加层与离线渲染

**实时叠加层**：[integrations/vtuber/overlay/index.html](../../integrations/vtuber/overlay/index.html)。

- 1280×720、透明背景、无外部 CDN/字体/音频，适合 OBS 浏览器源。
- 连接 Live WebSocket，显示最新指导、解说动态、头像气泡、情绪徽章、当前按键、队列深度和 `starved` 状态。
- 解说按类型着色，打字机效果，气泡队列最多 8 条；解说动态列表默认 8 行，可在配置中调整 3～30。
- 4 秒收不到帧会显示“画面中断”；断线按 1.8 的指数退避重连（上限 15 秒），每 25 秒 ping。
- 令牌优先级：URL 查询参数 `token`/`access_token` > `localStorage.vtuber_token` > 同目录 `config.js` 的 `window.VTUBER_CONFIG`。`config.example.json` 是字段示例，实际叠加层读取的是 `config.js`。

**离线渲染器**：[integrations/vtuber/render_vtuber.py](../../integrations/vtuber/render_vtuber.py)。

- 输入录屏 MP4 + 时间线 JSON，输出带 ASS 字幕的 MP4。
- 可选 `--avatar` 叠加头像，`--tts` 使用 edge-tts 合成语音（默认 `zh-CN-XiaoxiaoNeural`）。
- 每条字幕默认显示 2.5 秒 + 每字 0.12 秒，限制在 2～8 秒；TTS 最多 80 条。
- 时间轴零点默认取最早的 `live.started`；没有启动事件时用 `--start-ts` 指定。

## 8. AutoDL 部署

### 8.1 模型与 vLLM

模型：`koboshchan/Lumine-Agent-Pretrain-VL-7B`。官方 Lumine 仓库提供训练/推理环境参考（`run/install_vllm.sh`、`run/serve.Lumine-Agent-Pretrain-VL-7B.sh`）。

```bash
# AutoDL GPU 实例
cd /root
git clone https://github.com/zlc1004/Lumine.git
cd Lumine
bash run/install_vllm.sh          # 安装 vLLM、transformers、accelerate 等
source .venv/bin/activate

# 国内下载模型
export HF_ENDPOINT=https://hf-mirror.com
hf download koboshchan/Lumine-Agent-Pretrain-VL-7B \
  --local-dir ./models/Lumine-Agent-Pretrain-VL-7B
```

启动 vLLM（OpenAI 兼容接口，只绑 loopback）：

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model ./models/Lumine-Agent-Pretrain-VL-7B \
  --served-model-name lumine-agent-vl-7b \
  --dtype bfloat16 \
  --host 127.0.0.1 \
  --port 8000 \
  --gpu-memory-utilization 0.90
```

首次加载 7B 权重通常需要 1～5 分钟。`--served-model-name` 必须与 `autodl_server.py --model` 一致。

### 8.2 autodl_server.py

把 BetterGI 的 `integrations/lumine/` 上传到 AutoDL（或从仓库拉取），在目录内运行：

```bash
export LUMINE_SERVER_TOKEN='请替换为强随机令牌'
export LUMINE_HOST=127.0.0.1
export LUMINE_PORT=8010
export HF_MIRROR=https://hf-mirror.com
# vLLM 装在官方 Lumine 仓库的虚拟环境时：
export LUMINE_VENV=/root/Lumine/.venv
# 复用 8.1 已下载的模型，避免重复下载：
export LUMINE_MODEL_DIR=/root/Lumine/models/Lumine-Agent-Pretrain-VL-7B
bash serve_lumine.sh
```

`serve_lumine.sh` 会：按需下载模型、启动 vLLM、等待 `/health` 就绪、再启动 `autodl_server.py`。没有 GPU 或只想联调时：

```bash
LUMINE_MOCK=1 LUMINE_SERVER_TOKEN='dev-token' bash serve_lumine.sh
```

手动启动服务端：

```bash
python autodl_server.py \
  --host 127.0.0.1 \
  --port 8010 \
  --vllm-url http://127.0.0.1:8000 \
  --model lumine-agent-vl-7b \
  --token "$LUMINE_SERVER_TOKEN" \
  --prompt-mode pretrain \
  --max-tokens 128 \
  --temperature 0.7 \
  --top-p 0.95
```

常用环境变量：`LUMINE_MOCK=1`、`LUMINE_SERVER_TOKEN`、`LUMINE_MODEL_DIR`、`LUMINE_VENV`、`VLLM_EXTRA_ARGS`、`HF_MIRROR`。完整参数以 `python autodl_server.py --help` 为准。

### 8.3 端口

| 端口 | 进程 | 建议绑定 |
|---:|---|---|
| `8000` | vLLM OpenAI API | `127.0.0.1`，只给同机 `autodl_server.py` 用 |
| `8010` | `autodl_server.py` WS `/ws/player` + `/health`、`/status` | `127.0.0.1` + SSH 隧道；或安全组限制来源 IP |
| `30648` | BetterGI 外部访问 | `127.0.0.1`，绝不端口转发 |
| `8090` | 可选：叠加层静态服务器 | `127.0.0.1`，只给 OBS 本机浏览器源 |

### 8.4 GPU 与量化

- 7B VL 模型 bf16 权重约 15GB，加上视觉编码器、KV cache 和 vLLM 开销，建议 24GB 显存起步（RTX 4090 / A10 / L4 级别）。
- 16GB 显存可尝试 AWQ/GPTQ 4bit 权重，并用 `--quantization awq` 启动 vLLM；需要单独的量化模型目录，不能直接对 bf16 权重加参数。量化会改变输出质量，需要重新验证动作合法性。
- 显存不足时优先降低 `--max-model-len`、`--gpu-memory-utilization`，或加 `--enforce-eager`；这些是部署建议，不是本仓库测试过的组合。
- 老卡不支持 bf16 时可改用 `--dtype float16`，但数值稳定性和显存占用会变化，需要重新验证动作输出。
- 多卡/更大模型不在首版范围内；本链路按单实例 7B 设计。

### 8.5 国内下载

```bash
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/.cache/huggingface
hf download koboshchan/Lumine-Agent-Pretrain-VL-7B \
  --local-dir ./models/Lumine-Agent-Pretrain-VL-7B
```

`serve_lumine.sh` 也支持 `HF_MIRROR`，会写入 `HF_ENDPOINT`。下载失败时先确认 `hf`/`huggingface-cli` 在 PATH、磁盘空间足够、镜像可访问。

### 8.6 SSH 隧道、TLS 与令牌

推荐只把 AutoDL 的 8010 暴露给本机：

```bash
ssh -N \
  -L 8010:127.0.0.1:8010 \
  -p <AutoDL SSH 端口> \
  root@<AutoDL 主机>
```

本机 Bridge 使用 `--lumine-ws ws://127.0.0.1:8010/ws/player`。这样 AutoDL 的播放器端口不需要对公网开放。

如果必须跨公网直连：

- 用 Caddy/Nginx 反向代理到 `wss://`，配置有效 TLS 证书；
- 只允许可信源 IP；
- 设置强随机 `LUMINE_SERVER_TOKEN`，并定期轮换；
- 记住 Bridge 把令牌放在查询参数里，URL 可能进入代理日志、浏览器历史和监控系统；
- `/health`、`/status` 不鉴权，不要把 8010 直接暴露在公网；
- 绝对不要转发 BetterGI 的 `30648`，那个令牌等同于本机游戏控制权。

## 9. 安全边界

| 维度 | 本链路的做法 |
|---|---|
| 网络 | BetterGI 只监听 `localhost`，校验 Host 为 loopback；HTTP/MCP/WS 使用 Bearer；OBS/浏览器 WS 允许查询参数令牌 |
| 画面 | 只截取当前游戏窗口；不捕获桌面或其他窗口；不读游戏内存、不抓包、不注入 |
| 输入 | 只通过既有 `InputHub.Foreground` 的 SendInput/PostMessage；不加载脚本、不启动任意进程 |
| 按键 | 白名单：A-Z、`zero`～`nine`、`One`～`Twelve`（F1～F12，但 F4/F11 拒绝）、`Esc`/`Tab`/`Caps`/`Shift`/`Ctrl`/`Alt`/`Space`/`Enter`、`LB`/`RB`/`MB`/`XB1`/`XB2`；不接受 Win、PrintScreen 等系统键 |
| 危险组合 | `Alt+Tab`、`Alt/Ctrl+Esc`、`Alt+Space`、`Alt+Enter` 被拒绝；按键抬起不做组合检查 |
| 停止开关 | 设置页“停止智能体输入”、关闭智能体控制、`/live/stop`、`stop_actions`、失去前台/最小化、运行环境停止都会释放按键；停止状态跨请求保持，直到显式恢复 |
| 令牌 | BetterGI 令牌等于截屏 + 模拟键鼠控制权；AutoDL 令牌等于推理服务控制权；都不提交、不截图、不贴聊天 |
| 并发 | 实时动作不占用 `TaskControl.TaskSemaphore`；不要与 BetterGI 自动任务同时运行，避免两套输入叠加 |
| 条款 | 第三方软件和模拟操作存在封号风险；只建议在离线/单人内容中谨慎使用，不用于联机、竞技、代练或账号交易 |

BetterGI 的 CORS 允许任意 Origin，这不是访问控制；任何拿到令牌的网页或本机进程都可以调用。不要因为“只监听 loopback”就忽略了同机恶意程序与浏览器页面。

## 10. 失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| Bridge 连不上 AutoDL | 隧道断开、端口未开、令牌错误、AutoDL 实例重启 | 看 Bridge 日志的 `AutoDL ... 1～20 秒后重连`；`curl http://127.0.0.1:8010/health`；检查 SSH 隧道和 `LUMINE_SERVER_TOKEN` |
| 推理很慢 / 动作稀疏 | GPU 不足、max tokens 过大、分辨率过高、vLLM 排队 | 看 `/status` 的 `inflight`、`lastLatencyMs`；降 `max-tokens`、降分辨率、量化、换更大 GPU；不要靠加大动作队列解决 |
| 丢帧 / 动作错位 | 网络丢包/抖动、Bridge 5Hz 取样、AutoDL 忙时覆盖旧帧 | 这是“只保留最新”的设计行为；降低 fps/分辨率、缩短推理、使用有线网络；检查 `dropped` 与 `frameDrops` |
| 游戏失去前台 | Alt-Tab、最小化、其他窗口抢焦点 | 执行器立即释放按键；切回游戏前先 `stop_actions` 或停止直播，避免队列在回到前台时集中执行 |
| 模型输出无效动作 | 模型没按 Lumine 格式输出、max tokens 截断、量化质量下降、模型名不匹配 | AutoDL 返回 `bad_action` / `vllm_bad_response`；Bridge 不转发并保持停顿；降低 temperature、改用 `instruct` 提示、检查 `--model` 与 `--served-model-name` |
| 会话磁盘增长 | 录制、`recordFrames`、事件文件、截图 | Live 缩略帧最多 3000 张；每会话事件最多 20000 条（压缩到 10000）；截图 300 张；检查点 50 个；内存 1MB；会话 100 个；定期清理 `User/AgentSessions` 和录制文件 |
| 直播未运行 | 未调用 start、会话已停止、智能体开关关闭 | `409 LIVE_NOT_RUNNING` 时先 `bgi_live_start`；`403 AGENT_CONTROL_DISABLED` 时打开开关并重启 |
| 叠加层无画面 | token 错误、WS 未连接、直播未开始、帧未产生 | 看叠加层连接状态与 4 秒超时提示；确认 `LIVE_FRAME_NOT_FOUND` 不是 HTTP 404 路由问题；检查 `/status` 的 `clients` 和 `frameSeq` |
| 推理服务启动失败 | 模型未下载、显存不足、vLLM 版本不兼容 | 看 `vllm.log`；`serve_lumine.sh` 会在 300 秒内等待健康检查并输出末尾日志 |

## 11. 调参

| 参数 | 默认 | 建议 | 影响 |
|---|---:|---|---|
| 截帧 `fps` | 10 | 5～10；卡顿时 5～8 | 越高越流畅，但 CPU/带宽/推理压力更大 |
| JPEG `quality` | 70 | 55～75 | 越低越省带宽，但画面细节和 UI 文本变差 |
| `maxWidth` | 1280 | 1280；带宽紧张时 960 | Lumine 按 1280×720 训练；不要为了“更清晰”盲目调大 |
| Bridge `--fps` | 5 | 5 | 与 200ms 动作块匹配；更高会增加丢帧和推理压力 |
| 动作队列 | 20 个键块（约 660ms） | 保持默认，不要加大 | 加大只增加旧动作延迟，不提高吞吐 |
| 空闲释放 | 250ms | 保持默认 | 过短会频繁松键，过长会在模型停顿时继续移动 |
| `temperature` | 0.7 | 0.2～0.5（稳定操作）；0.7～0.9（多样性） | 越低越确定，越高越容易输出格式错误 |
| `max-tokens` | 128 | 64～128 | 动作块很短；过大只增加延迟 |
| `top-p` | 0.95 | 0.9～0.95 | 与 temperature 一起控制采样 |
| `prompt-mode` | `pretrain` | 模型是预训练版时用 `pretrain`；需要更强指导跟随时可试 `instruct` | 改变系统提示的详细程度 |
| 导演 `interval` | 4s | 3～6s | 越短解说越密，LLM 成本越高 |
| 每轮解说数 | 4 | 1～4 | 过多会刷屏，叠加层也会排队 |
| 单条解说长度 | 80 字符 | 20～60 字符 | 适合字幕和 TTS |
| 记忆上限 | 4000 字符 | 2000～6000 | 提示词成本与连续性之间的折中 |

补充：

- 绝对坐标始终按 1280×720 输出帧解释，再映射到当前游戏画面。游戏窗口应保持 16:9；推荐窗口化或无边窗口 1280×720 或更高 16:9 分辨率。
- 动作模型看到的是 Bridge 抽样的最新帧，**不是** dsh 正在看的那一帧；两者可能有几百毫秒偏差。指导应描述方向/意图，不要依赖单帧像素级细节。
- 推理慢时，降低分辨率/量化通常比降低 fps 更有效；fps 降得太多会让动作块之间缺少反馈。
- dsh 的导演间隔与 Bridge 的 5Hz 相互独立；不要让 dsh 以高于 2 秒的频率反复改指导，否则 Lumine 会在指令之间来回摆动。

## 12. 测试状态

| 项目 | 状态 | 说明 |
|---|---|---|
| C# 编译 | 已纳入项目编译目标 | Live 服务、执行器、HTTP 端点、MCP 工具、WebSocket 处理器均在 `BetterGenshinImpact/Service/ExternalAccess/Agent/Live`；按项目约定使用 `dotnet msbuild BetterGenshinImpact.sln -restore -p:Configuration=Debug -nologo -tl:off -clp:ErrorsOnly` 验证 |
| C# 单元测试 | 已覆盖 | `Test/BetterGenshinImpact.UnitTest/ServiceTests/ExternalAccess/LiveSessionServiceTests.cs` 覆盖启动参数校验、JPEG 帧采集/停止、指导与解说时间线、动作校验与执行、重复启动/停止、客户端 hello/detach；`LiveKeyTokenMapTests.cs` 覆盖字母/数字/功能键/鼠标键映射、F4/F11 拒绝、大小写与未知 token。测试使用假截图源、假输入和临时目录，不启动游戏 |
| Python 单元测试 | 已覆盖 | `python -m unittest discover -s integrations/lumine/tests -v`；覆盖动作协议解析、Bridge 抽帧/动作转换/重连辅助、AutoDL mock 服务器、导演 JSON 提取/提示词/工具白名单、时间线渲染 |
| Python mock 服务器 | 可用 | `LUMINE_MOCK=1` 或 `autodl_server.py --mock` 返回按帧序号确定的动作，不需要 GPU，可用于端到端联调 |
| 实际 AutoDL GPU | 未在本环境验证 | 未在真实 AutoDL 实例上测量 vLLM 7B 的吞吐、延迟、显存占用和输出质量；本文的延迟数字是设计预算 |
| 真实游戏输入 | 未在本环境验证 | 未启动游戏主程序做端到端点击/移动验证；首次接入应按 README 的冒烟顺序人工观察 |
| OBS / 离线渲染 | 部分覆盖 | `render_vtuber.py` 有 `test_render_timeline.py` 覆盖时间线/字幕逻辑；叠加层是静态 HTML，未在 OBS 实际推流和真实录屏上验收 |

结论：C# 与 Python 的纯逻辑/协议层已有测试和 mock 联调路径（本轮文档编写未重新运行 C# 构建/测试，也未启动游戏或 AutoDL GPU）；**GPU 推理质量、真实游戏延迟和 OBS 效果仍需在目标机器上实测**。不要根据本文的估算值承诺固定帧率或延迟。
