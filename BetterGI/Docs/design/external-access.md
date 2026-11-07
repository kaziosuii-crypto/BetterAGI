# BetterGI 外部访问服务设计

## 目标与边界

外部访问服务为本机程序和 BetterGI 内部 WebView2 页面提供统一的 HTTP、WebSocket 与 MCP 入口。默认只公开健康状态、版本信息和实时日志；启用可选的智能体适配器后，还会开放 `/api/v1/agent/*` 与 `bgi_agent_*` 工具。除智能体适配器明确提供的游戏窗口截图、视觉识别、模拟键鼠和会话留存外，不提供其他任务启动、停止等业务控制能力。

服务只属于 Primary 实例，只监听 loopback 地址。HTTP、WebSocket 和 MCP 共用一个端口，默认端口为 `30648`。HTTP、WebSocket、MCP 与智能体控制默认关闭；全部关闭或当前进程不是 Primary 实例时，不创建监听器。只启用智能体控制时，宿主仍会启动并仅提供实时 WebSocket `/ws/v1/agent/live`。端口被占用或宿主启动失败只会更新设置页状态并写入日志，不影响 WPF 主程序继续运行。

## 分层

外部访问实现分为四层：

1. `IBgiExternalCapabilityService` 定义与传输无关的 BetterGI 能力，并返回共享 DTO。
2. HTTP 与 MCP 适配层把各自的请求转换为能力服务调用，不重复实现业务逻辑。
3. `ExternalLogHub`、`ExternalLogSink` 和 `ExternalLogWebSocketHandler` 负责日志采集、回放与实时传输。
4. `ExternalAccessHost` 管理独立 `WebApplication` 的生命周期、loopback 监听、路由与中间件。

## 配置与生命周期

配置位于 `AllConfig.ExternalAccessConfig`，并继续由现有配置服务保存到 `User/config.json`：

| 配置项 | 默认值 | 生效时机 |
|---|---:|---|
| `HttpApiEnabled` | `false` | 重启 BetterGI 后 |
| `WebSocketEnabled` | `false` | 重启 BetterGI 后 |
| `McpEnabled` | `false` | 重启 BetterGI 后 |
| `AgentControlEnabled` | `false` | 重启 BetterGI 后（调用时还会再次检查） |
| `Port` | `30648` | 重启 BetterGI 后 |
| `AccessToken` | 空字符串 | 立即生效 |

HTTP 或 MCP 启用且令牌为空时，服务会生成 256-bit 随机值，转换为无填充的 Base64Url 字符串并立即保存。设置页只显示脱敏值，提供复制和重新生成操作；重新生成后旧令牌立即失效，使用旧令牌的 MCP stdio 桥或客户端需要更新 `BETTERGI_AGENT_TOKEN` 并重启。BetterGI 不向 WebView2 页面自动注入令牌，调用页面必须自行提供。

## HTTP API

HTTP JSON 使用 `System.Text.Json` 和 camelCase 属性名。未启用 HTTP 时不映射下列路由，因此返回 `404`。

| 方法与路径 | 能力 | 返回模型 |
|---|---|---|
| `GET /api/v1/health` | 查询进程健康状态 | `status`、`instanceRole`、`processId`、`startedAtUtc`、`uptimeSeconds` |
| `GET /api/v1/version` | 查询版本 | `product`、`version`、`apiVersion` |

同时启用 `AgentControlEnabled` 时，还会映射 `/api/v1/agent/*` 下的状态、观察、截图、动作序列、会话/内存/检查点、运行环境启停与停止/恢复端点。

示例请求：

```http
GET /api/v1/health HTTP/1.1
Host: 127.0.0.1:30648
Authorization: Bearer <token>
```

## MCP

MCP 使用 `ModelContextProtocol.AspNetCore` 的无状态 Streamable HTTP 传输，挂载在 `/mcp`，不提供旧式 SSE 端点。未启用 MCP 时不映射该路由。

| Tool | 共享能力与返回模型 |
|---|---|
| `get_bgi_health` | 与 `GET /api/v1/health` 相同 |
| `get_bgi_version` | 与 `GET /api/v1/version` 相同 |

启用智能体控制后，同一 `/mcp` 端点还会注册 `bgi_agent_*` 工具，详见[智能体适配器设计](agent-adapter.md#6-mcp)。

## 鉴权、CORS 与本机边界

- HTTP API 与 MCP 要求 `Authorization: Bearer <token>`，缺失或错误时返回 `401`。
- 令牌通过固定时间比较校验，服务日志不得记录令牌。
- CORS 允许任意 Origin、Header 和 Method，不允许 Cookie 或 credentials；预检 `OPTIONS` 不要求鉴权。
- WebSocket 日志按产品约定不鉴权并允许任意 Origin。设置页会明确提示日志可能泄露路径、脚本名、异常堆栈等信息。
- Kestrel 只监听 loopback；额外的 Host 校验只接受 `localhost` 或 loopback IP，降低 DNS rebinding 风险。

## WebSocket 日志协议

连接地址为：

```text
ws://127.0.0.1:30648/ws/v1/logs?minimumLevel=Information
```

`minimumLevel` 可省略，默认值为 `Information`，并接受 Serilog 日志级别名称。值无效时握手请求返回 `400`。

连接建立后先按原顺序回放最近 200 条满足最低级别的日志，再持续推送实时日志。每个客户端拥有容量 512 的独立有界队列；队列满时丢弃最旧消息，不阻塞日志与业务线程，并在下一次发送前推送累计丢弃通知。

日志消息字段如下：

| 字段 | 含义 |
|---|---|
| `sequence` | 进程内递增序号 |
| `timestampUtc` | UTC 时间戳 |
| `level` | 日志级别 |
| `source` | 日志来源 |
| `message` | 已渲染消息 |
| `exception` | 异常文本；没有异常时为空 |
| `instance` | BetterGI 实例标识 |

丢弃通知为独立 JSON 消息，包含 `type: "dropped"` 与 `count`。服务支持多个并发客户端，并在客户端断开或应用退出时取消订阅并释放连接资源。

## 智能体适配器

智能体适配器是在外部访问服务之上新增的一层可选游戏操作能力。它通过 `ExternalAccessConfig.AgentControlEnabled` 单独开关，默认关闭，重启 BetterGI 后生效；HTTP 端点还要求启用 HTTP API，MCP 工具还要求启用 MCP，实时 WebSocket 只依赖智能体控制。适配器只提供游戏窗口截图、现有视觉识别和 `SendInput` / `PostMessage` 模拟键鼠，不读写游戏内存、不抓包、不注入、不执行 Shell 或脚本，也不切换账号。

在 HTTP API 启用时，适配器挂载 `/api/v1/agent/*`，包括状态、观察、二进制截图、动作序列、会话、事件、内存、检查点、截图下载、运行环境启停和停止/恢复；在 MCP 启用时，额外注册 `bgi_agent_*` 工具，与 HTTP 共用 `/mcp`、端口和 Bearer 令牌。动作数量、文本长度、等待时间、120 秒序列上限、坐标空间、按键白名单和主显示器限制由能力层统一执行；click/drag 类绝对坐标在产生输入前统一预检，观察与动作序列串行执行。用户停止开关会取消当前序列并释放输入，停止状态跨请求保持，直到显式恢复。文本输入仅在 Win32 前台后端尽力而为，中文/IME 可能失败。

完整设计、端点与 JSON 示例、MCP 工具、坐标空间、会话布局、运行注意事项、威胁模型和游戏服务条款风险见[智能体适配器设计](agent-adapter.md)。
