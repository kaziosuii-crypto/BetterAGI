# BetterGI 智能体适配器设计

> 状态：已实施（首版） · 2026-10-26 · 关联：[外部访问服务](external-access.md)、[GameRuntime](game-runtime.md)、[实时触发器](realtime-trigger.md)

## 1. 目标与非目标

### 1.1 目标

智能体适配器（Agent Adapter）让本机的 AI 智能体（如 dsh）能够以“看画面、做操作、记进度”的方式游玩《原神》：

- 通过 BetterGI 已有的截图链路观察当前游戏窗口；
- 通过模板匹配与 PaddleOCR 获得 UI 状态和文本；
- 通过 BetterGI 已有的模拟输入层发送键鼠操作；
- 把截图、动作、观察、备忘和检查点保存在本机会话目录，支持跨请求、跨进程重启续接；
- 复用外部访问服务的 loopback 监听、Bearer 鉴权和统一端口，不新增第二套网络入口。

适配器只做“视觉输入 + 模拟操作”，是现有自动游玩能力的可编排版本，不改变 BetterGI 与游戏交互的方式。

### 1.2 非目标

以下能力明确不在适配器范围内，也不会通过任何隐藏参数开放：

- 读写游戏进程内存、扫描特征码、修改游戏数据；
- 抓包、改包、重放协议或注入网络流量；
- DLL 注入、API Hook、驱动、调试器附加；
- 执行 Shell、启动任意进程、调用系统命令；
- 加载或执行 C# / JavaScript 插件、脚本文件；
- 切换账号、登出登录、修改账号绑定或代练托管；
- 捕获游戏窗口以外的桌面内容，或操作其他应用窗口；
- 绕过游戏的反作弊、风控或服务条款限制。

`GET /api/v1/agent/status` 中的 `memoryAccessAllowed` 恒为 `false`，用于向调用方明确这一边界。

## 2. 安全边界

适配器的所有能力都必须满足下面的硬性约束：

| 维度 | 允许 | 禁止 |
|---|---|---|
| 画面 | 通过 `GameRuntimeService` 当前运行环境截取游戏窗口 | 桌面全屏捕获、其他窗口捕获、录屏上传 |
| 识别 | 现有 Bv 模板匹配、PaddleOCR | 读取游戏内存、解析游戏封包、调用游戏内部接口 |
| 输入 | `InputHub` 的 `SendInput` / `PostMessage` 通道 | 注入、Hook、驱动级输入、直接改写游戏状态 |
| 按键 | `AgentKeyMap` 白名单内的游戏常用键 | Win、PrintScreen、F4、F11、Apps 等可能影响系统或 BetterGI 的键，以及 Alt+Tab 等危险组合 |
| 文件 | `User/AgentSessions` 下的会话文件 | 任意路径读写、删除程序文件、执行脚本 |
| 进程 | 读取当前游戏运行环境信息 | 创建进程、Shell、服务、计划任务 |
| 账号 | 使用当前已登录账号 | 切换、登出、登录、代填账号密码 |

限制由能力服务、安全策略和输入控制器共同保证，HTTP / MCP 适配层不重复实现，也不能绕过：

- 每个动作在序列开始前统一经过 `AgentSafetyPolicy` 校验，任何一项不合法都不会产生部分输入；
- 输入控制器只调用 `InputHub` 的前台或后台通道；
- 坐标在动作执行前统一换算，越界坐标直接拒绝；
- 会话存储只接受符合格式的会话标识与帧标识，并校验最终路径位于会话目录内；
- 用户停止开关会取消当前序列并释放已按下的键鼠，恢复前拒绝新的输入；停止状态在请求间保持，重启 BetterGI 后复位。

## 3. 架构分层

```text
HTTP / WebSocket / MCP（外部访问子 WebApplication）
        │
        ├─ BgiAgentHttpEndpoints      /api/v1/agent/*
        └─ BgiAgentMcpTools           bgi_agent_*
                    │
                    ▼
        IBgiAgentCapabilityService / BgiAgentCapabilityService
        校验、串行化、会话事件、观察组装、运行环境启停
                    │
        ┌───────────┼────────────┬──────────────┬─────────────┐
        ▼           ▼            ▼              ▼             ▼
 IAgentScreenSource IAgentVision IAgentInput   IAgentSession  IGameRuntime
 GameWindowScreen  BgiAgent     Simulated     AgentSession   GameRuntime
 Source            Vision       Input         Store          Accessor
        │           │            │              │             │
        ▼           ▼            ▼              ▼             ▼
 现有截图器       Bv / OCR     InputHub       文件系统     GameRuntimeService
                                                             TaskTriggerDispatcher
```

| 层 | 职责 |
|---|---|
| 传输适配层 | 参数绑定、结果映射、结构化错误；不实现业务规则 |
| 能力服务 | 开关判断、参数与序列校验、并发互斥、观察组装、会话事件、停止开关 |
| 能力原语 | 截图、识别、输入、会话存储、运行环境访问，各自单一职责 |
| 既有运行时 | 复用 BetterGI 的截图、触发器、输入后端、识别与配置服务 |

依赖方向始终由外向内，能力原语不感知 HTTP / MCP，也不反向调用传输层。

### 3.1 并发与互斥

- 观察与动作序列共用 `_sequenceLock` 串行化，并发 observe 不会重叠；两个智能体请求也不会同时操作同一画面。
- 动作序列以非阻塞方式占用 `TaskControl.TaskSemaphore`；BetterGI 正在执行独立任务时立即返回 `BETTERGI_TASK_RUNNING`，避免和用户任务争抢输入。
- 单次动作序列最长 120 秒（`maxSequenceDurationMs`）。超时、BetterGI 全局停止热键/停止按钮、运行环境解绑都会取消剩余动作。
- 截图和识别通过 `TaskTriggerDispatcher.PauseForInference()` 短暂暂停实时触发器自身的识图，避免同一帧被重复分析、降低资源争用。
- 观察、会话、内存和检查点接口不占任务信号量，可以在用户任务运行期间读取状态，但不能发送输入。
- 停止或取消时，无论序列成功、失败还是取消，输入控制器都会释放适配器自己按下且尚未抬起的键鼠；前台与后台通道分别记录、分别释放，避免残留 Ctrl/Alt 按下状态。

## 4. 配置与生命周期

配置位于 `AllConfig.ExternalAccessConfig`，由现有配置服务保存到 `User/config.json`。

| 配置项 | 默认值 | 生效时机 | 说明 |
|---|---:|---|---|
| `AgentControlEnabled` | `false` | 重启后决定路由/工具；调用时实时检查 | 是否向本机智能体开放截图与模拟键鼠；单独开启也会启动实时 WebSocket 监听器 |
| `HttpApiEnabled` | `false` | 重启 BetterGI 后 | 是否映射 `/api/v1/agent/*` HTTP 端点（含 `/live/*`） |
| `McpEnabled` | `false` | 重启 BetterGI 后 | 是否注册 `bgi_agent_*` 与 `bgi_live_*` MCP 工具 |
| `Port` | `30648` | 重启 BetterGI 后 | HTTP、WebSocket、MCP 共用端口 |
| `AccessToken` | 空字符串 | 立即生效 | HTTP API、MCP 与实时 WebSocket 共用的 Bearer 令牌 |

启用规则：

- 智能体控制与实时 WebSocket 是独立入口。HTTP 端点要求 `HttpApiEnabled && AgentControlEnabled`，MCP 工具要求 `McpEnabled && AgentControlEnabled`；实时 WebSocket `/ws/v1/agent/live` 只要求 `AgentControlEnabled`，并会单独启动外部访问宿主。
- 只打开智能体控制、不打开 HTTP 或 MCP 时，外部访问宿主仍会启动并映射 `/ws/v1/agent/live`；此时没有普通 observe/act 的 HTTP 端点，也没有 `bgi_agent_*` / `bgi_live_*` MCP 工具，因此也没有启动实时会话的入口（启动直播仍需 HTTP 或 MCP 之一）。
- 如果 HTTP、WebSocket 日志、MCP 与智能体控制全部关闭，不创建监听器。
- 服务启动时读取一次开关并决定路由和工具是否注册；运行期间能力服务每次调用都会重新读取 `AgentControlEnabled`。
  - 启动后关闭开关但未重启：已映射的 HTTP/MCP 路由与实时 WebSocket 仍然存在；实时 WebSocket 仍接受连接，但实时 `start`/`guidance`/`commentary`/`actions` 等能力返回 `403 AGENT_CONTROL_DISABLED`，状态、停止/恢复与实时 `frame` 仍可用；关闭开关的瞬间会停止当前序列与实时会话并释放按键。
  - 启动后打开开关但未重启：HTTP 端点仍是 `404`，MCP 列表中没有智能体工具；实时 WebSocket 端点同样要到重启后才会映射。
- 设置页提供“停止智能体输入”和“恢复智能体输入”按钮，直接调用 `IBgiAgentCapabilityService.Stop()` / `Resume()`，不需要令牌。`Stop()` 会取消当前序列、释放已按下的键鼠并写入 `agent.stopped` 事件；停止在后续请求之间持续生效，只有调用 resume（HTTP/MCP/设置页）才恢复。停止状态不跨 BetterGI 重启保留。
- 启用 HTTP API、MCP 或智能体控制且令牌为空时，服务生成 256-bit 随机令牌，转换为无填充 Base64Url 并立即保存。重新生成后旧令牌立即失效。
- 令牌保存在 `User/config.json` 中，设置页只显示脱敏值并提供复制、重新生成操作。

## 5. HTTP API

### 5.1 通用约定

- 基地址：`http://127.0.0.1:<Port>`，默认 `http://127.0.0.1:30648`。
- 所有 `/api/v1/agent/*` 请求都必须携带 `Authorization: Bearer <token>`，缺失或错误返回 `401`。
- 请求与响应 JSON 使用 `System.Text.Json`，属性名为 camelCase。
- 启动时未启用 HTTP API 或智能体控制时，路由不映射，返回 `404`；启动后关闭智能体控制则路由仍在，返回 `403`（见第 4 节）。
- 请求级错误响应统一为：

```json
{
  "code": "RUNTIME_UNAVAILABLE",
  "message": "游戏运行环境尚未启动或已经停止。请先在 BetterGI 首页启动截图器，或调用 /api/v1/agent/runtime/start。",
  "details": "GameRuntimeService.Current 为空。"
}
```

| 错误码 | HTTP 状态 | 含义 |
|---|---:|---|
| `AGENT_CONTROL_DISABLED` | 403 | 智能体控制开关已关闭；状态、停止、恢复接口仍可用 |
| `AGENT_STOPPED` | 409 | 用户按下了停止开关，需要先恢复 |
| `RUNTIME_UNAVAILABLE` | 409 | 截图器未启动、正在停止或游戏已退出 |
| `BETTERGI_TASK_RUNNING` | 409 | BetterGI 正在执行其他任务，拒绝并发输入或停止运行环境 |
| `INVALID_REQUEST` | 400 | 参数、动作类型、动作序列或会话数据不合法 |
| `COORDINATE_OUT_OF_PRIMARY_SCREEN` | 400 | 目标坐标不在主显示器内 |
| `DANGEROUS_SHORTCUT_BLOCKED` | 400\* | 组合键会切换窗口或打开系统界面，已拒绝 |
| `GAME_NOT_FOREGROUND` | 409\* | 前台通道无法把游戏窗口带到前台，已拒绝发送输入 |
| `CAPTURE_FAILED` | 503\* | 截图失败 |
| `INVALID_GAME_RESOLUTION` | 409\* | 游戏画面分辨率不受支持 |
| `TEXT_NOT_SUPPORTED` | 501\* | 当前输入后端不支持文本输入 |
| `TEXT_NOT_FOUND` | 404\* | `clickAtText` 未找到目标文本 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `FRAME_NOT_FOUND` | 404 | 截图不存在或已被清理 |
| `SESSION_STORE_ERROR` | 500 | 会话目录读写失败（会话/内存/检查点接口） |
| `IMAGE_ENCODE_FAILED` | 500 | 图像编码失败 |
| `INTERNAL_ERROR` | 500 | 未预期异常 |

\* 标星错误在 `/actions` 中属于执行阶段失败：整个请求仍返回 HTTP 200，`ok=false`，错误码与详情分别位于失败步骤的 `code`、`details`。直接调用 `observe` 等接口触发时才使用表中状态。动作序列被取消的步骤使用 `AGENT_STOPPED`（用户停止）或 `CANCELLED`。所有 `click`/`doubleClick`/`rightClick`/`moveMouse`/`drag` 动作（含 drag 终点）会在产生任何输入前统一预检，坐标错误直接返回 `400`，不会执行到一半；`clickAtText` 的落点在执行时由 OCR 结果决定，只能在该步骤内校验。

请求被调用方取消或超时时返回 `408`，不写响应体。请求体 JSON 绑定失败（例如字段类型错误、JSON 语法错误）由 ASP.NET Core 返回框架级 `ProblemDetails`，而不是上面的 `{code,message,details}` 结构；调用方应同时处理这两种错误格式。

### 5.2 端点总表

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v1/agent/status` | 能力、运行环境、停止开关与固定限制 |
| `POST` | `/api/v1/agent/observe` | 观察一帧，可选 UI 状态、OCR 与 Base64 图像 |
| `GET` | `/api/v1/agent/screenshot` | 以二进制返回一帧截图 |
| `POST` | `/api/v1/agent/actions` | 按顺序执行一组动作，默认结束后观察一帧 |
| `GET` | `/api/v1/agent/frames/{sessionId}/{frameId}` | 下载已保存截图，支持 Range |
| `POST` | `/api/v1/agent/runtime/start` | 启动游戏运行环境与实时触发器 |
| `POST` | `/api/v1/agent/runtime/stop` | 停止运行环境并释放输入 |
| `POST` | `/api/v1/agent/stop` | 请求停止当前智能体操作 |
| `POST` | `/api/v1/agent/resume` | 恢复被停止的智能体操作 |
| `POST` | `/api/v1/agent/sessions` | 确保会话存在 |
| `GET` | `/api/v1/agent/sessions` | 列出全部会话 |
| `GET` | `/api/v1/agent/sessions/{sessionId}` | 获取会话摘要 |
| `GET` | `/api/v1/agent/sessions/{sessionId}/resume` | 获取恢复会话所需信息 |
| `GET` | `/api/v1/agent/sessions/{sessionId}/events` | 按序号增量读取事件 |
| `GET` | `/api/v1/agent/sessions/{sessionId}/memory` | 读取会话内存 |
| `POST` | `/api/v1/agent/sessions/{sessionId}/memory` | 合并写入会话内存 |
| `DELETE` | `/api/v1/agent/sessions/{sessionId}/memory/{key}` | 删除一个内存键 |
| `POST` | `/api/v1/agent/sessions/{sessionId}/checkpoints` | 创建检查点 |
| `GET` | `/api/v1/agent/sessions/{sessionId}/checkpoints` | 列出检查点 |

`sessionId` 省略或为空时使用默认会话 `default`。会话标识只允许字母、数字、下划线和短横线，长度为 1～64，首字符不能是符号。

会话、内存、检查点、已保存截图、观察/动作以及运行环境启停接口在每次调用时重新检查 `AgentControlEnabled`；关闭开关后这些接口返回 `403`，`GET /status`、`POST /stop`、`POST /resume` 仍可用。

`SESSION_STORE_ERROR` 由会话、内存、检查点接口的存储包装层返回。`observe`、`actions` 中直接发生的存储失败（建会话、写截图等）不经过该包装层，仍映射为 `INTERNAL_ERROR`（动作步骤中同样是 `INTERNAL_ERROR`）；需要区分时请先调用会话接口确认目录可写。

### 5.3 状态

```http
GET /api/v1/agent/status HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
{
  "enabled": true,
  "runtimeRunning": true,
  "stopRequested": false,
  "capabilities": [
    "status", "observe", "execute", "sessions", "memory", "checkpoints",
    "frames", "screenshot", "click", "doubleClick", "rightClick", "moveMouse",
    "moveMouseBy", "drag", "scroll", "key", "text", "wait", "activateWindow",
    "clickAtText", "runtime.start", "runtime.stop", "stop", "resume"
  ],
  "limits": {
    "maxActionsPerRequest": 64,
    "maxWaitMs": 30000,
    "maxSequenceDurationMs": 120000,
    "maxTextLength": 200,
    "minActionIntervalMs": 40,
    "maxFramesPerSession": 300,
    "maxImageWidth": 1920
  },
  "runtime": {
    "kind": "Win32Window",
    "processId": 12345,
    "windowHandle": 132456,
    "isAlive": true,
    "isForeground": true,
    "isMinimized": false,
    "requiresForeground": true,
    "captureArea": { "x": 100, "y": 80, "width": 1600, "height": 900, "dpiScale": 1.0 }
  },
  "sessionRoot": "C:\\BetterGI\\User\\AgentSessions",
  "memoryAccessAllowed": false,
  "apiVersion": "v1"
}
```

`runtime` 在截图器未启动时为 `null`。`requiresForeground` 表示前台输入是否依赖游戏窗口位于前台：本地 Win32 窗口为 `true`，云原神网页版为 `false`。

### 5.4 观察

```http
POST /api/v1/agent/observe HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
Content-Type: application/json

{
  "sessionId": "daily",
  "includeUiState": true,
  "includeOcr": true,
  "ocrRoi": { "x": 0, "y": 0, "width": 1600, "height": 900 },
  "includeImageBase64": false,
  "maxWidth": 1280,
  "format": "png",
  "quality": 85,
  "saveFrame": true,
  "name": "main-ui"
}
```

```json
{
  "sessionId": "daily",
  "frameId": "f20261026214530123_0001",
  "capturedAtUtc": "2026-10-26T21:45:30.123+00:00",
  "eventSeq": 42,
  "runtime": {
    "kind": "Win32Window",
    "processId": 12345,
    "windowHandle": 132456,
    "isAlive": true,
    "isForeground": true,
    "isMinimized": false,
    "requiresForeground": true,
    "captureArea": { "x": 100, "y": 80, "width": 1600, "height": 900, "dpiScale": 1.0 }
  },
  "captureArea": { "x": 100, "y": 80, "width": 1600, "height": 900, "dpiScale": 1.0 },
  "image": {
    "mimeType": "image/png",
    "width": 1280,
    "height": 720,
    "originalWidth": 1600,
    "originalHeight": 900,
    "url": "/api/v1/agent/frames/daily/f20261026214530123_0001",
    "name": "main-ui"
  },
  "vision": {
    "uiCategory": "Main",
    "isInMainUi": true,
    "isInDomain": false,
    "isInAnyClosableUi": false,
    "isInGameReadyUi": true,
    "isInPartyViewUi": false,
    "isInBigMapUi": false,
    "isInTalkUi": false,
    "ocr": [
      {
        "text": "派蒙",
        "x": 120,
        "y": 640,
        "width": 96,
        "height": 32,
        "normalizedX": 0.075,
        "normalizedY": 0.7111,
        "normalizedWidth": 0.06,
        "normalizedHeight": 0.0356
      }
    ],
    "elapsedMs": 320
  },
  "imageBase64": null
}
```

字段说明：

- `uiCategory` 取值为 `Unknown`、`Main`、`Talk` 或 `BigMap`。
- `ocrRoi` 使用原始截图像素坐标；省略时识别整帧。全屏 OCR 开销明显，建议只框选需要读取的区域。
- OCR 行的 `x`、`y`、`width`、`height` 位于识别帧的像素空间；画面大于 1080P 时识别帧会被等比缩小。点击时请使用同一行的 `normalizedX` / `normalizedY`，不要直接把 `x` / `y` 当作 `capture` 坐标。
- `saveFrame` 默认为 `true`。保存时 `image.url` 可用于下载；不保存时 `frameId` 仍存在，但以 `unsaved-` 开头且没有文件。
- `maxWidth` 默认 `1280`，最大 `1920`；`0` 表示保持原尺寸。缩小只影响返回图像，不影响归一化坐标。
- `format` 为 `png` 或 `jpg`，`quality` 只在 JPEG 时生效，范围 1～100。
- `name` 最长 64 字符；超过时返回 `400 INVALID_REQUEST`。

### 5.5 二进制截图

```http
GET /api/v1/agent/screenshot?sessionId=daily&maxWidth=1280&format=jpg&quality=85&ocr=false&ui=true&save=true&name=check HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

成功时返回 `200`，`Content-Type` 为 `image/png` 或 `image/jpeg`，并附带响应头：

| 响应头 | 含义 |
|---|---|
| `X-Bgi-Frame-Id` | 帧标识 |
| `X-Bgi-Session` | 会话标识 |
| `X-Bgi-Ui-Category` | UI 类别；请求 `ui=true` 或 `ocr=true`（即返回了 `vision`）时存在 |

该端点不返回 JSON，也不在图像中携带 Base64，适合需要直接取得图片字节的客户端。

### 5.6 执行动作

```http
POST /api/v1/agent/actions HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
Content-Type: application/json

{
  "sessionId": "daily",
  "channel": "foreground",
  "actions": [
    { "type": "key", "key": "W", "durationMs": 800 },
    { "type": "wait", "waitMs": 300 },
    { "type": "click", "x": 0.5, "y": 0.5, "coordinateSpace": "normalized", "button": "left" },
    { "type": "observe", "name": "after-move" }
  ],
  "stopOnError": true,
  "captureAfter": true,
  "includeUiState": true,
  "includeOcr": false,
  "includeImageBase64": true,
  "maxWidth": 1280,
  "format": "png",
  "quality": 85
}
```

```json
{
  "ok": true,
  "sessionId": "daily",
  "stopRequested": false,
  "steps": [
    {
      "index": 0,
      "type": "key",
      "ok": true,
      "message": null,
      "code": null,
      "details": null,
      "startedAtUtc": "2026-10-26T21:46:00.001+00:00",
      "durationMs": 820,
      "frameId": null,
      "point": null,
      "observation": null
    },
    {
      "index": 2,
      "type": "click",
      "ok": true,
      "message": null,
      "code": null,
      "details": null,
      "startedAtUtc": "2026-10-26T21:46:01.140+00:00",
      "durationMs": 75,
      "frameId": null,
      "point": {
        "normalizedX": 0.5,
        "normalizedY": 0.5,
        "captureX": 800.0,
        "captureY": 450.0,
        "desktopX": 900.0,
        "desktopY": 530.0
      },
      "observation": null
    }
  ],
  "observation": {
    "sessionId": "daily",
    "frameId": "f20261026214601150_0006",
    "capturedAtUtc": "2026-10-26T21:46:01.150+00:00",
    "eventSeq": 47,
    "runtime": { "kind": "Win32Window", "processId": 12345, "windowHandle": 132456, "isAlive": true, "isForeground": true, "isMinimized": false, "requiresForeground": true, "captureArea": { "x": 100, "y": 80, "width": 1600, "height": 900, "dpiScale": 1.0 } },
    "captureArea": { "x": 100, "y": 80, "width": 1600, "height": 900, "dpiScale": 1.0 },
    "image": { "mimeType": "image/png", "width": 1280, "height": 720, "originalWidth": 1600, "originalHeight": 900, "url": "/api/v1/agent/frames/daily/f20261026214601150_0006", "name": null },
    "vision": null,
    "imageBase64": "iVBORw0KGgoAAAANSUhEUg..."
  },
  "error": null
}
```

行为说明：

- 为节省篇幅，上面的 `steps` 只展示了第 0、2 个动作；实际响应会按顺序包含每个已执行动作。
- 动作按数组顺序执行；默认首个失败处停止（`stopOnError=true`），设为 `false` 时继续执行后续动作。
- 所有动作类型和 `click`/`doubleClick`/`rightClick`/`moveMouse`/`drag` 的绝对坐标会在产生任何输入前统一校验；序列或坐标不合法时不会执行任何动作，直接返回 `400`。`clickAtText` 的落点依赖执行时的 OCR 结果，在该步骤内校验。
- 执行阶段才发现的失败（`GAME_NOT_FOREGROUND`、`TEXT_NOT_FOUND`、`TEXT_NOT_SUPPORTED`、`CAPTURE_FAILED`、`DANGEROUS_SHORTCUT_BLOCKED` 等）仍返回 HTTP `200`，整体 `ok=false`，失败步骤的 `code`、`details` 给出稳定错误码与详情；`error` 为导致序列停止的说明，`stopOnError=false` 时可能为空，需同时检查 `steps[].ok`。
- 单次序列最长 120 秒；超时、BetterGI 全局停止或运行环境解绑会取消剩余动作，并写入 `sequence.cancelled` 事件。
- 默认在序列执行结束后观察一帧（`captureAfter=true`，被取消或停止时不执行），该帧同样写入会话事件。
- `observation` 是结束后观察的结果；请求 `includeImageBase64=true` 时其中包含 Base64 图像。
- `stopRequested=true` 表示执行期间或执行后停止开关处于按下状态；当前序列被取消，后续输入会被拒绝，直到显式恢复。
- `step.point` 同时给出归一化、截图和桌面三种坐标，便于调用方核对落点。成功步骤的 `code`、`details` 为 `null`。

### 5.7 已保存截图

```http
GET /api/v1/agent/frames/daily/f20261026214530123_0001 HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

返回图片二进制，并支持 HTTP `Range` 请求，便于分段读取大图。文件不存在或已被清理时返回 `404 FRAME_NOT_FOUND`。

### 5.8 运行环境启停

启动运行环境：

```http
POST /api/v1/agent/runtime/start HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
{ "running": true }
```

停止运行环境：

```http
POST /api/v1/agent/runtime/stop HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

成功返回 `204 No Content`。

`runtime/start` 与首页“启动”按钮复用同一个 `GameRuntimeService.StartAsync`：先绑定当前游戏运行环境，再按配置的 `TriggerInterval` 启动 `TaskTriggerDispatcher`。因此成功启动后，BetterGI 的实时触发器（自动拾取、自动剧情等用户已启用的功能）也会开始工作。启动过程可能查找窗口、激活游戏窗口，或在配置允许时启动游戏；具体行为以“启动”页当前设置为准。

启动异常不会返回 `500`：服务会捕获启动失败并返回 `{ "running": false }`，同时向默认会话写入带 `error` 的 `runtime.started` 事件；客户端应检查 `running` 而不是只看 HTTP 状态。

`runtime/stop` 以非阻塞方式占用 BetterGI 的任务锁；已有用户任务运行时返回 `409 BETTERGI_TASK_RUNNING`。成功时先释放适配器按下的键鼠，再停止截图器与实时触发器，并返回 `204`；运行环境停止会取消依赖它的任务。

### 5.9 停止与恢复

```http
POST /api/v1/agent/stop HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
{ "stopRequested": true }
```

```http
POST /api/v1/agent/resume HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
{ "stopRequested": false }
```

`stop` 会取消正在执行的动作序列并释放已按下的键鼠，同时向默认会话写入 `agent.stopped` 事件；在调用 `resume` 之前，新的输入动作返回 `409 AGENT_STOPPED`。停止状态在后续请求之间持续生效，但不会跨 BetterGI 重启保留；重启后需要重新确认状态。停止开关是面向用户的紧急制动，不会停止截图器和实时触发器；需要停止运行环境时调用 `runtime/stop`。

### 5.10 会话

确保会话存在（已存在时返回现有会话，可用 `name` 更新名称）：

```http
POST /api/v1/agent/sessions HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
Content-Type: application/json

{ "sessionId": "daily", "name": "每日委托" }
```

```json
{
  "id": "daily",
  "name": "每日委托",
  "createdAtUtc": "2026-10-26T21:40:00.000+00:00",
  "updatedAtUtc": "2026-10-26T21:40:00.000+00:00",
  "eventCount": 1,
  "frameCount": 0,
  "lastFrameId": null,
  "lastSeq": 1
}
```

列出会话：

```http
GET /api/v1/agent/sessions HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
[
  {
    "id": "daily",
    "name": "每日委托",
    "createdAtUtc": "2026-10-26T21:40:00.000+00:00",
    "updatedAtUtc": "2026-10-26T21:45:30.123+00:00",
    "eventCount": 42,
    "frameCount": 12,
    "lastFrameId": "f20261026214530123_0001",
    "lastSeq": 42
  }
]
```

获取单个会话：

```http
GET /api/v1/agent/sessions/daily HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

响应与上面的单个会话摘要相同；不存在时返回 `404 SESSION_NOT_FOUND`。

恢复信息：

```http
GET /api/v1/agent/sessions/daily/resume?limit=50 HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
{
  "session": {
    "id": "daily",
    "name": "每日委托",
    "createdAtUtc": "2026-10-26T21:40:00.000+00:00",
    "updatedAtUtc": "2026-10-26T21:45:30.123+00:00",
    "eventCount": 42,
    "frameCount": 12,
    "lastFrameId": "f20261026214530123_0001",
    "lastSeq": 42
  },
  "memory": {
    "sessionId": "daily",
    "updatedAtUtc": "2026-10-26T21:45:00.000+00:00",
    "values": { "objective": "完成每日委托", "step": 2 }
  },
  "recentEvents": [
    {
      "seq": 42,
      "timestampUtc": "2026-10-26T21:45:30.123+00:00",
      "type": "observation",
      "data": { "frameId": "f20261026214530123_0001", "vision": { "uiCategory": "Main" } }
    }
  ],
  "checkpoints": [
    {
      "id": "cp_20261026214400000_0001",
      "name": "before-talk",
      "note": "与凯瑟琳对话前",
      "createdAtUtc": "2026-10-26T21:44:00.000+00:00",
      "memory": { "objective": "完成每日委托", "step": 1 },
      "lastFrameId": "f20261026214350000_0010",
      "lastSeq": 35
    }
  ]
}
```

`limit` 为返回的最近事件数量，范围 1～500，默认 50。检查点按创建时间倒序返回。

事件增量读取：

```http
GET /api/v1/agent/sessions/daily/events?afterSeq=40&limit=100 HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
[
  {
    "seq": 41,
    "timestampUtc": "2026-10-26T21:45:28.000+00:00",
    "type": "action",
    "data": { "index": 0, "type": "key", "ok": true, "message": null, "code": null, "details": null, "frameId": null, "point": null }
  },
  {
    "seq": 42,
    "timestampUtc": "2026-10-26T21:45:30.123+00:00",
    "type": "observation",
    "data": { "frameId": "f20261026214530123_0001", "vision": { "uiCategory": "Main" } }
  }
]
```

`afterSeq` 默认为 `0`，`limit` 会被限制在 1～500。

### 5.11 内存

读取：

```http
GET /api/v1/agent/sessions/daily/memory HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
{
  "sessionId": "daily",
  "updatedAtUtc": "2026-10-26T21:45:00.000+00:00",
  "values": { "objective": "完成每日委托", "step": 2, "lastFrameId": "f20261026214530123_0001" }
}
```

合并写入（值为 `null` 的键会被删除）：

```http
POST /api/v1/agent/sessions/daily/memory HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
Content-Type: application/json

{ "step": 3, "lastFrameId": "f20261026214601150_0006", "oldGoal": null }
```

```json
{
  "sessionId": "daily",
  "updatedAtUtc": "2026-10-26T21:46:02.000+00:00",
  "values": {
    "objective": "完成每日委托",
    "step": 3,
    "lastFrameId": "f20261026214601150_0006"
  }
}
```

删除单个键：

```http
DELETE /api/v1/agent/sessions/daily/memory/step HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

响应为删除后的完整内存对象。

单个内存键最长 128 字符；整个会话内存序列化后不能超过 1,000,000 字节，超出时返回 `400 INVALID_REQUEST`。

### 5.12 检查点

创建：

```http
POST /api/v1/agent/sessions/daily/checkpoints HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
Content-Type: application/json

{ "name": "before-talk", "note": "与凯瑟琳对话前" }
```

```json
{
  "id": "cp_20261026214400000_0001",
  "name": "before-talk",
  "note": "与凯瑟琳对话前",
  "createdAtUtc": "2026-10-26T21:44:00.000+00:00",
  "memory": { "objective": "完成每日委托", "step": 1 },
  "lastFrameId": "f20261026214350000_0010",
  "lastSeq": 35
}
```

`name` 省略时使用 `checkpoint`。检查点保存创建时的内存快照、最近帧标识和事件序号。名称最长 80 字符，`note` 最长 512 字符；单个会话最多保留 50 个检查点，超出时返回 `400 INVALID_REQUEST`。

列出：

```http
GET /api/v1/agent/sessions/daily/checkpoints HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

```json
[
  {
    "id": "cp_20261026214400000_0001",
    "name": "before-talk",
    "note": "与凯瑟琳对话前",
    "createdAtUtc": "2026-10-26T21:44:00.000+00:00",
    "memory": { "objective": "完成每日委托", "step": 1 },
    "lastFrameId": "f20261026214350000_0010",
    "lastSeq": 35
  }
]
```

## 6. MCP

### 6.1 连接

- 地址：`http://127.0.0.1:30648/mcp`
- 传输：MCP Streamable HTTP，无状态（`HttpServerSessionMode.Stateless`），不提供旧式 SSE 端点。
- 鉴权：`Authorization: Bearer <token>`。
- 请求头：`Content-Type: application/json`，`Accept: application/json, text/event-stream`。
- 启用条件：`McpEnabled && AgentControlEnabled`；关闭时工具不存在。即使工具已注册，调用时仍会重新检查 `AgentControlEnabled`，关闭后返回 `403 AGENT_CONTROL_DISABLED`（`observe`/`act` 为 `isError=true` 的工具结果，其余工具可能是协议错误）。

```http
POST /mcp HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"dsh","version":"1.0"}}}
```

无状态模式不保存 MCP 会话；每次工具调用都应携带完整参数。标准 MCP 客户端会先发送 `initialize` 和 `notifications/initialized`，之后调用 `tools/list`、`tools/call`。

### 6.2 工具列表

| Tool | 主要参数 | 返回 |
|---|---|---|
| `bgi_agent_status` | 无 | `AgentStatusDto` |
| `bgi_agent_observe` | `sessionId?`、`includeUiState=true`、`includeOcr=false`、`includeImage=true`、`maxWidth=1280` | 文本 JSON 元数据 + 可选 PNG 图像内容块 |
| `bgi_agent_act` | `actions`、`sessionId?`、`channel=foreground`、`captureAfter=true`、`includeUiState=true`、`includeOcr=false`、`includeImage=true`、`maxWidth=1280` | 文本 JSON 动作结果 + 可选 PNG 图像内容块 |
| `bgi_agent_session_create` | `sessionId?`、`name?` | 会话摘要 |
| `bgi_agent_session_list` | 无 | 会话摘要列表 |
| `bgi_agent_session_resume` | `sessionId`、`limit=50` | 会话摘要、内存、最近事件与检查点 |
| `bgi_agent_memory_get` | `sessionId?` | 会话内存 |
| `bgi_agent_memory_set` | `values`、`sessionId?` | 合并后的会话内存 |
| `bgi_agent_checkpoint_create` | `sessionId?`、`name?`、`note?` | 检查点 |
| `bgi_agent_runtime_start` | 无 | 是否成功启动 |
| `bgi_agent_runtime_stop` | 无 | 无 |
| `bgi_agent_stop` | 无 | 无 |
| `bgi_agent_resume` | 无 | 无 |

说明：

- 启用 MCP 后，原有的 `get_bgi_health`、`get_bgi_version` 工具仍可用。
- `bgi_agent_observe` 与 `bgi_agent_act` 把图像放在 MCP 图像内容块中，JSON 文本里不重复 Base64；`includeImage=false` 时只返回文本元数据。
- `bgi_agent_act` 在 `captureAfter=false` 时，会回退使用最后一个 `observe` 步骤的图像；若序列中没有 `observe` 步骤，则没有图像内容块。
- `bgi_agent_observe` 与 `bgi_agent_act` 捕获 `AgentException`，以 `CallToolResult` 返回 `isError=true`，文本格式为 `CODE: message`；调用方应检查工具结果的错误标记，而不是等待 JSON-RPC 错误。
- 其余 DTO 工具（会话、内存、检查点、运行环境启停等）没有包装业务异常，`AGENT_CONTROL_DISABLED`、`BETTERGI_TASK_RUNNING` 等可能直接表现为 MCP 协议错误。
- `bgi_agent_act` 的 `actions` 使用与 HTTP 相同的动作对象结构。
- `bgi_agent_observe` / `bgi_agent_act` 的文本 JSON 使用 camelCase 并省略值为 `null` 的字段；HTTP JSON 会保留 `null` 字段（例如成功步骤的 `code`、`details`）。
- 会话摘要读取、事件读取、内存键删除和检查点列表目前只有 HTTP 端点，没有对应的 MCP 工具；需要时可直接调用 HTTP API。
- 工具失败时调用方应同时处理 MCP 协议错误和 `isError=true` 的工具结果。

## 7. 坐标空间

所有带坐标的动作都支持三种坐标空间，由 `coordinateSpace` 指定：

| 取值 | 含义 | 示例 |
|---|---|---|
| `normalized`（默认） | 相对游戏画面（捕获区域）的比例，`0～1` | `{ "x": 0.5, "y": 0.5 }` 表示画面中心 |
| `capture` | 相对游戏捕获区域的像素坐标 | `{ "x": 800, "y": 450 }` |
| `desktop` | 桌面物理像素坐标 | `{ "x": 900, "y": 530 }` |

换算关系：

```text
captureX  = normalizedX × captureArea.width
captureY  = normalizedY × captureArea.height
desktopX  = captureArea.x + captureX
desktopY  = captureArea.y + captureY
```

关键限制：

- 归一化坐标相对的是**游戏捕获区域**，不是返回图像的像素尺寸。图像被 `maxWidth` 缩小后，归一化坐标仍然不变。
- `capture` 使用捕获区域的物理像素，不是返回图像的像素；不要把缩小后图像的像素直接当作 `capture` 坐标。
- `desktop` 使用 Windows 桌面物理像素；`captureArea` 由当前游戏窗口的实时视口给出。
- **仅绝对坐标动作受主显示器限制。** `click`、`doubleClick`、`rightClick`、`moveMouse`、`drag` 和 `clickAtText` 的落点必须位于主显示器内；前台 `SendInput` 与后台 `PostMessage` 的绝对坐标都按主显示器归一化，目标落在负坐标或主显示器之外时返回 `400 COORDINATE_OUT_OF_PRIMARY_SCREEN`（在 `/actions` 中为失败步骤的 `code`），不会静默点击到错误位置。`key`、`text`、`scroll`、`moveMouseBy` 不使用绝对坐标，不受此检查影响。
- 多显示器、DPI 缩放、窗口移动或缩放都会改变桌面坐标。智能体应优先使用 `normalized`，并在移动窗口或切换分辨率后重新观察。
- 动作执行时会重新读取实时捕获区域；`step.point` 返回实际使用的三种坐标，便于核对。

## 8. 动作类型与限制

### 8.1 动作

| `type` | 必要参数 | 可选参数 | 说明 |
|---|---|---|---|
| `click` | `x`、`y` | `button`、`clicks`、`coordinateSpace`、`channel` | `clicks` 范围 1～3 |
| `doubleClick` | `x`、`y` | `button`、`coordinateSpace`、`channel` | 固定点击 2 次 |
| `rightClick` | `x`、`y` | `coordinateSpace`、`channel` | 右键单击 |
| `moveMouse` | `x`、`y` | `coordinateSpace`、`channel` | 移动鼠标 |
| `moveMouseBy` | `deltaX` 或 `deltaY` | `channel` | 相对移动，仅前台通道，适合视角控制 |
| `drag` | `x`、`y`、`toX`、`toY` | `durationMs`、`coordinateSpace`、`channel` | 仅前台；持续时间 0～10000 ms |
| `scroll` | `scrollClicks` | `channel` | 仅前台；绝对值 1～10，正值向上 |
| `key` | `key` | `keyAction`、`durationMs`、`channel` | `keyAction` 为 `press` / `down` / `up`；`durationMs` 0～30000 |
| `text` | `text` | `channel` | 仅前台、仅 Win32 输入后端；最多 200 字符，不能含控制字符；中文/IME/Raw Input 下可能失败，优先用按键动作 |
| `wait` | `waitMs` | 无 | 0～30000 ms，不产生输入 |
| `activateWindow` | 无 | 无 | 把游戏窗口带到前台 |
| `screenshot` | 无 | `name` | 截图并保存，不做识别 |
| `observe` | 无 | `name` | 完整观察一帧，返回 UI 状态与可选 OCR |
| `clickAtText` | `text` | `exact`、`button`、`channel` | 先 OCR 查找文本再点击；`text` 最多 100 字符，落点受主显示器检查 |

动作对象还支持：

- `channel`：覆盖请求级默认通道；
- `coordinateSpace`：覆盖默认坐标空间；
- `button`：`left`、`right`、`middle`，默认 `left`；
- `name`：截图或观察事件的名称，最长 64 字符；
- `exact`：`clickAtText` 是否要求文本完全相等，默认包含匹配。

### 8.2 固定限制

| 限制 | 值 | 说明 |
|---|---:|---|
| 单次请求动作数 | 64 | 超过时整组拒绝 |
| 单次序列总时长 | 120000 ms | 超时后取消剩余动作 |
| `waitMs` | 30000 ms | 单个等待动作上限 |
| 文本长度 | 200 字符 | `text` 动作 |
| `clickAtText` 文本 | 100 字符 | |
| 动作间最小间隔 | 40 ms | 输入动作执行后统一等待 |
| 拖拽持续时间 | 10000 ms | 0 表示使用默认 300 ms，实际下限 100 ms |
| 滚轮刻度 | 10 | 绝对值 |
| 截图最大宽度 | 1920 px | 默认 1280；0 表示原尺寸 |
| 单会话截图数 | 300 | 超出后删除最早的截图 |
| 单张截图大小 | 20 MB | 超过时拒绝保存 |
| 单会话事件数 | 20000 | 超出后压缩为最近 10000 条 |
| 会话标识 | 1～64 字符 | 字母、数字、下划线、短横线 |
| 会话名称 | 80 字符 | |
| 观察/截图名称 | 64 字符 | `name` 字段 |
| 会话内存 | 1000000 字节 | 序列化后的上限 |
| 内存键 | 128 字符 | |
| 检查点名称 | 80 字符 | |
| 检查点说明 | 512 字符 | `note` |
| 单会话检查点数 | 50 | 超出后拒绝创建 |
| 会话总数 | 100 | 达到上限后拒绝创建新会话 |
| HTTP 请求体 | 4 MB | Kestrel 上限 |

### 8.3 按键白名单

`key` 接受常见别名（`W`、`A`、`S`、`D`、`E`、`Q`、`Space`、`Shift`、`Ctrl`、`Alt`、`Esc`、`Enter`、`Tab`、方向键、`F1`～`F12` 等），内部转换为 Windows 虚拟键码，并限制在白名单内：

- 字母 `A`～`Z`、数字 `0`～`9`、小键盘 `0`～`9`；
- `F1`～`F12`（不含 `F4`、`F11`）；
- `Space`、`Shift`、`Ctrl`、`Alt`、`Esc`、`Tab`、`Enter`、`Backspace`；
- 方向键、`Home`、`End`、`PageUp`、`PageDown`、`Insert`、`Delete`；
- 常用标点符号键。

Win、PrintScreen、F4、F11、Apps 等可能影响系统、其他应用或 BetterGI 自身的键不在白名单内，传入时返回 `400 INVALID_REQUEST`。`F11` 是 BetterGI 默认的全局启用/禁用热键，因此与 `F4` 一起排除。

按键动作在 `press` / `down` 时会检查危险组合：Alt+Tab、Alt+Space、Alt+Enter、Alt+Esc、Ctrl+Esc 返回 `400 DANGEROUS_SHORTCUT_BLOCKED`（在动作序列中作为失败步骤返回）。`up` 不检查，否则会妨碍释放已经按下的键。

## 9. 前台与后台能力

| 能力 | `foreground`（SendInput） | `background`（PostMessage） |
|---|---|---|
| 需要游戏窗口在前台 | 是；窗口最小化或不在前台时会尝试激活，失败则 `409 GAME_NOT_FOREGROUND` | 否 |
| 点击 / 双击 / 右键 | 支持 | 支持左键、右键 |
| 鼠标中键 | 支持 | 不支持 |
| 绝对移动 | 支持 | 支持 |
| 相对移动（视角） | 支持 | 不支持 |
| 拖拽 | 支持 | 不支持 |
| 滚轮 | 支持 | 不支持 |
| 键盘按下 / 抬起 / 长按 | 支持 | 支持 |
| 文本输入 | 尽力而为（仅 Win32 输入后端） | 不支持 |
| `clickAtText` | 支持 | 支持（最终为点击） |

前台通道更接近真实用户操作，兼容性最好，但会抢占前台，可能干扰用户正在使用的其他窗口。后台通道不抢焦点，但游戏可能忽略 `PostMessage`，且不支持连续视角控制、拖拽和滚轮。智能体应优先使用小批量前台操作，并在每批后观察。

云原神网页版运行环境（`WebPage`）使用 Web SDK 输入后端，`requiresForeground=false`，前台/后台通道差异与本地窗口不同；`text` 动作只支持 Win32 后端，在网页版会返回 `501 TEXT_NOT_SUPPORTED`。

文本输入是尽力而为：仅前台 Win32 后端可用，通过 `KEYEVENTF_UNICODE` 模拟键盘，不经过剪贴板；游戏使用 IME、中文输入法或 Raw Input 时可能忽略或乱序。需要稳定输入时优先使用按键动作或点击界面元素，不要依赖 `text` 输入中文。

## 10. 会话布局与续接

### 10.1 目录结构

会话根目录默认为程序目录下的 `User/AgentSessions`：

```text
User/AgentSessions/
└─ daily/
   ├─ meta.json
   ├─ events.jsonl
   ├─ memory.json
   ├─ frames/
   │  ├─ f20261026214530123_0001.png
   │  └─ f20261026214601150_0006.jpg
   └─ checkpoints/
      └─ cp_20261026214400000_0001.json
```

| 文件 | 内容 |
|---|---|
| `meta.json` | 会话标识、名称、创建/更新时间、事件数、截图数、最近帧、最后序号 |
| `events.jsonl` | 按行追加的事件，序号在会话内递增 |
| `memory.json` | 调用方可自由读写的键值 JSON 对象 |
| `frames/` | 已保存截图，文件名即帧标识 |
| `checkpoints/` | 检查点 JSON 快照 |

所有元数据、内存和检查点写入都使用临时文件加替换，避免半写状态；事件行追加后立即刷新到磁盘。截图超过 300 张时按文件名（时间顺序）删除最早的；事件超过 20000 条时压缩为最近 10000 条，压缩后序号可能不连续。

### 10.2 事件

事件类型包括：

| 类型 | 触发时机 |
|---|---|
| `session.created` | 会话首次创建 |
| `frame.saved` | 每保存一帧截图（观察、截图动作、自动结束观察） |
| `observation` | 观察一帧（含结束后自动观察） |
| `action` | 单个动作完成，记录结果、`code`、`details`、坐标和帧 |
| `sequence.cancelled` | 动作序列因停止、超时或运行环境解绑而中断 |
| `runtime.started` | 请求启动运行环境（失败时 `data.error` 非空） |
| `runtime.stopped` | 请求停止运行环境 |
| `agent.stopped` | 用户按下停止开关 |

事件类型只有上表 8 种，不存在 `memory.updated`、`checkpoint` 等事件；内存和检查点变更请读取对应接口。`runtime.started`、`runtime.stopped`、`agent.stopped` 写入默认会话 `default`，不会写入调用方会话。

事件只保存摘要，不把完整 OCR 文本和图像写入事件文件，避免事件文件膨胀。

### 10.3 续接流程

推荐的重连顺序：

1. `GET /api/v1/agent/status` 确认能力启用、运行环境就绪；若 `stopRequested=true`，先调用 `/resume`（停止状态不会自行过期）；
2. `GET /api/v1/agent/sessions/{id}/resume?limit=50` 取得会话摘要、内存、最近事件和检查点；
3. 读取 `memory.values` 恢复目标与进度；
4. 用 `lastFrameId` 或重新 `observe` 获取当前画面，与最近事件核对；
5. 需要回到历史状态时，参考检查点中的 `memory`、`lastFrameId`、`lastSeq`，由调用方决定如何重建上下文；适配器不会自动回滚游戏状态；
6. 继续小批量动作，并在每批后写入内存。

会话文件跨进程重启保留；运行环境、游戏窗口和用户任务状态不会随会话恢复。

### 10.4 内存与检查点

- 内存是调用方自己的便签，适配器不解释键名和值，只保证 JSON 对象读写和合并。
- 合并写入时值为 `null` 的键会被删除；也可以调用 `DELETE .../memory/{key}`。
- 检查点是某一时刻的内存、最近帧和事件位置的快照，不会自动执行，也不会回滚游戏。
- 建议在长任务的关键节点创建检查点，例如进入秘境前、对话选择前、领取奖励前。

## 11. 运行时启停的副作用

`POST /api/v1/agent/runtime/start` 不是只启动截图：

- 调用 `GameRuntimeService.StartAsync`，在 UI 线程上查找/绑定游戏窗口；
- 绑定成功后按 `TriggerInterval` 启动 `TaskTriggerDispatcher`，用户已启用的实时触发器开始运行（自动拾取、自动剧情等）；
- 可能激活游戏窗口；在配置允许时可能启动游戏；
- 如果已有运行环境，直接返回当前环境，不会重复启动；
- 启动失败返回 `{ "running": false }`（并写入带 `error` 的 `runtime.started` 事件），不会创建第二个截图链路。找不到游戏窗口且未开启关联启动时，现有启动流程可能弹出错误对话框并等待用户确认，HTTP 调用会相应阻塞。

`POST /api/v1/agent/runtime/stop`：

- 以非阻塞方式占用 `TaskControl.TaskSemaphore`；BetterGI 正在执行用户任务时返回 `409 BETTERGI_TASK_RUNNING`，不会停掉用户任务；
- 先释放适配器自己按下但尚未抬起的键鼠；
- 再停止截图器与实时触发器，并取消依赖该运行环境的任务；
- 不会关闭游戏进程，也不会登出账号。

`POST /api/v1/agent/stop` 只停止智能体输入，不停止运行环境；`POST /api/v1/agent/resume` 只清除停止标记，不会重新启动运行环境。停止状态跨请求保持，直到恢复或 BetterGI 重启。

## 12. 运行注意事项

### 12.1 管理员权限与 UIPI

游戏通常以管理员权限运行。Windows 的 UIPI 会阻止低完整性进程向高完整性窗口发送输入。BetterGI 必须以不低于游戏进程的权限运行，否则：

- 前台 `SendInput` 可能被系统拦截；
- 后台 `PostMessage` 同样可能被 UIPI 拦截；
- 表现为动作返回失败、游戏无响应或部分输入丢失。

如果模拟输入被安全软件拦截，需要把 BetterGI 加入白名单。已知 360 或自定义 WD 规则会拦截部分模拟点击。

### 12.2 焦点与窗口

- 前台通道会激活游戏窗口；运行期间不要同时用鼠标键盘操作游戏，否则会与智能体输入叠加。
- 后台通道不抢焦点，但游戏可能忽略消息；视角控制、拖拽和滚轮必须使用前台。
- 游戏窗口最小化时前台通道会尝试恢复；无法恢复时返回 `GAME_NOT_FOREGROUND`。
- 窗口移动、缩放、切换显示器或分辨率后，桌面坐标会变化；智能体应重新观察并优先使用归一化坐标。

### 12.3 OCR 成本

- 全屏 PaddleOCR 可能耗时数百毫秒到数秒，具体取决于画面复杂度、CPU 和 OCR 区域。
- 尽量使用 `ocrRoi` 只识别目标区域；对话、任务提示等固定区域更适合框选。
- 不要每帧都开启 OCR；先观察 UI 状态和图像，需要读文本时再请求。
- OCR 结果是图像识别的近似值，可能漏字、错字或合并行；不要把 OCR 文本当作绝对可靠的结构化数据。

### 12.4 操作节奏

- BetterGI 约定键鼠操作后需适当延时：游戏响应通常有 30～60 ms 延迟，截图来源也有延迟。
- 适配器在每个输入动作后至少等待 40 ms，点击按下/抬起之间也有额外延时；调用方不要再以更高频率连续发送。
- 一批动作后应观察一帧，确认界面变化后再决定下一步。
- 30 fps 游戏输入过快会丢键；长距离移动、对话选择、传送确认等场景应留出更长的等待。

### 12.5 图像与带宽

- PNG 清晰但体积大；需要频繁传输时使用 `maxWidth` 缩小或 `format=jpg`。
- Base64 编码会让图像体积增加约 33%；HTTP 优先使用 `/screenshot` 或 `frames` 下载二进制。
- MCP 的 `includeImage=false` 可以只取元数据，需要时再单独观察。

### 12.6 云原神与网页版

- 云原神网页版运行环境通过 WebView/Web SDK 工作，`requiresForeground=false`。
- 画面经过视频编码，细节和 OCR 准确率可能低于本地窗口；精确点击和快速连续操作更容易失败。
- 文本输入只在 Win32 输入后端可用，网页版返回 `501 TEXT_NOT_SUPPORTED`；即使本地窗口，IME/中文/Raw Input 也可能导致输入失败，属于尽力而为。
- 网络延迟、排队、断流都会表现为画面不更新或动作无效；智能体应先观察再重试，不要盲目连点。

### 12.7 与用户任务共存

- 动作序列会占用 `TaskControl.TaskSemaphore`；BetterGI 正在执行独立任务时返回 `409 BETTERGI_TASK_RUNNING`。
- 观察与动作序列共用同一把请求锁并串行执行，并发 observe 不会重叠；动作序列执行期间新的 observe 会等待。
- 观察和会话读取不占任务信号量，可以在任务期间读取，但此时画面可能正在被任务操作。
- 不要在用户任务运行时强行注入输入；等待任务结束或先停止任务。

### 12.8 磁盘与日志

- 每个会话最多保留 300 张截图和 20000 条事件；长期运行仍会占用磁盘，建议定期清理不需要的会话目录。
- 会话目录包含游戏画面和 OCR 文本，可能包含角色名、UID、聊天内容等个人信息，应按敏感数据管理。
- 外部访问服务日志不记录令牌；WebSocket 日志端点按产品约定不鉴权，可能泄露路径、脚本名和异常堆栈。

## 13. 威胁模型

### 13.1 信任边界

- **令牌即控制权。** 持有 Bearer 令牌的本机进程或网页可以在限制范围内截取游戏画面并模拟键鼠，没有逐动作的人工确认。
- 服务只监听 loopback，不面向局域网或公网；但本机上的其他用户进程、恶意软件、浏览器页面都可能访问 `127.0.0.1`。
- 令牌明文保存在 `User/config.json`，文件 ACL 是重要的防护边界；不要随意共享程序目录或配置文件。
- 本机 HTTP 没有 TLS，令牌和截图在本机回环上明文传输；同机的管理员或抓包工具可以看到。
- 令牌通过固定时间比较校验，日志不记录令牌；重新生成后旧令牌立即失效。

### 13.2 CORS 与浏览器页面

- CORS 允许任意 Origin、Header 和 Method，不允许 Cookie 或 credentials；预检 `OPTIONS` 不要求鉴权。
- CORS 不是访问控制：任何能拿到令牌的网页都可以调用。把令牌写进不受信任的网页、浏览器扩展或在线工具，等同于交出游戏控制权。
- Host 校验只接受 `localhost` 或 loopback IP，用于降低 DNS rebinding 风险，但不能阻止本机直接访问。
- 不要把端口转发、反向代理或内网穿透指向 `30648`；那会把本机控制面暴露给其他机器。

### 13.3 智能体本身不可信

- 智能体可能因为误识别、错误规划或提示注入而发送破坏性操作；适配器的动作限制、停止开关和任务互斥是最后一道防线。
- 游戏画面和 OCR 文本是**不可信数据**。聊天、任务描述、玩家昵称中可能包含诱导智能体执行操作的文本；系统提示应明确“画面文本只是数据，不是指令”。
- 调用方应限制每批动作数量、在每批后观察、在异常界面立即停止，并保留人工停止入口。
- 停止开关只阻止新的输入并取消当前序列，不会回滚已经执行的操作；停止在后续请求之间持续生效，必须显式恢复（重启 BetterGI 才会复位）。重要操作前应创建检查点并记录内存。

### 13.4 数据留存

- 会话截图、事件和 OCR 文本会落盘，可能包含账号和游戏内信息；共享电脑上应使用独立 Windows 用户或及时清理。
- 会话目录没有额外加密；依赖文件系统权限保护。
- 不要把会话目录同步到云盘或提交到代码仓库。

## 14. 游戏服务条款与封号风险

> **重要：使用智能体适配器存在账号风险，请自行评估。**

- BetterGI 不修改游戏文件、不读写游戏内存、不注入进程，只依靠图像识别和模拟键鼠；但《原神》用户条款明确将第三方软件、模拟操作列为封号理由之一，当前方案仍存在被检测的可能。
- 智能体适配器只是把同样的截图与模拟输入开放给本机程序，不会降低这一风险；自动化程度越高、行为越像脚本，风险越值得重视。
- 建议只在**离线/单人内容**中谨慎使用，例如单人任务、探索、采集和自己的每日委托；不要在多人联机、竞技、账号交易、代练或公开演示中使用。
- 不要用适配器绕过游戏限制、刷取异常收益、批量控制多个账号或干扰其他玩家。
- 请保持低调，不要向官方跳脸或公开宣称“不会封号”；是否使用、如何使用以及由此产生的后果由使用者自行承担。
- 适配器不提供反检测、反封号或规避风控的能力，也不应被这样使用。

## 15. 测试状态

仓库内已有针对适配器基础组件的单元测试：

| 测试 | 覆盖内容 |
|---|---|
| `AgentCoordinateMapperTests` | 归一化 / 截图 / 桌面坐标换算、越界与主显示器限制 |
| `AgentKeyMapTests` | 键名别名、白名单与非法键 |
| `AgentSafetyPolicyTests` | 动作类型、参数、通道与序列校验 |
| `AgentSessionStoreTests` | 会话、事件、内存、检查点、截图保留与路径安全 |
| `AgentSourceBoundaryTests` | 能力来源边界，防止越过截图 / 识别 / 模拟输入范围 |

当前测试的边界：

- 单元测试覆盖纯逻辑和文件存储，不需要启动游戏。
- 未包含真实游戏窗口的自动化端到端测试：BetterGI 需要管理员权限，测试环境无法可靠启动主程序和游戏窗口。
- 本文档中的 HTTP / MCP 示例按实现约定编写，未在真实游戏客户端上逐条执行；首次接入时建议按下面的冒烟顺序手动验证。

建议的手动冒烟顺序：

1. 启用 HTTP API 与智能体控制，重启 BetterGI，复制令牌；
2. `GET /status` 确认 `enabled=true`、`runtime` 状态正确；
3. `POST /runtime/start` 或手动启动截图器，确认 `runtimeRunning=true`；
4. `observe` 不带 OCR，再带 OCR，确认图像、UI 类别和文本；
5. 发送单个 `click` 或 `key`，确认游戏响应且 `step.point` 合理；
6. 测试 `stop` 后输入返回 `AGENT_STOPPED`，`resume` 后恢复；
7. 在 BetterGI 任务运行时测试 `BETTERGI_TASK_RUNNING`；
8. 写入内存、创建检查点、重启 BetterGI，再用 `resume` 确认会话可续接；
9. 把游戏窗口移到副屏或让坐标越界，确认返回 `COORDINATE_OUT_OF_PRIMARY_SCREEN`。

## 16. 实时 VTuber 链路

实时链路复用本适配器的截图、输入、会话记忆与安全边界，在 `/api/v1/agent/live/*`、`/ws/v1/agent/live` 和 `bgi_live_*` MCP 工具上增加低延迟直播能力：BetterGI 按 5～10fps 广播 JPEG、执行 200ms 动作块，Lumine Bridge 以 5Hz 转发给 AutoDL 的 Lumine 玩家模型，dsh 负责指导与解说。完整架构、时序、部署和安全说明见 [实时 VTuber 链路设计](live-vtuber-pipeline.md)，字段与动作格式见 [PROTOCOL.md](../../integrations/lumine/PROTOCOL.md)。

- HTTP：`/api/v1/agent/live/start|stop|status|frame|guidance|commentary|timeline|actions`；
- WebSocket：`/ws/v1/agent/live`，推送 `frame`/`guidance`/`commentary`/`action_state`，接收 `guidance`/`commentary`/`actions`/`stop_actions`；
- MCP：`bgi_live_start`、`bgi_live_stop`、`bgi_live_status`、`bgi_live_frame`、`bgi_live_set_guidance`、`bgi_live_add_commentary`、`bgi_live_timeline`、`bgi_live_actions`。

实时动作仍只通过 `InputHub.Foreground` 和键位白名单执行，不新增内存、抓包或脚本能力；关闭智能体控制、用户停止开关、失去前台或调用 `stop_actions` 都会释放按键。实时动作不占用 `TaskControl.TaskSemaphore`，不要与 BetterGI 自动任务同时运行。
