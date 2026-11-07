# BetterGI × Lumine × VTuber 实时链路协议

版本：`lumine-live-v1`（2026-10-26）

本协议连接四个角色：

```text
┌──────────────┐   画面 / 状态 / 解说    ┌──────────────────┐
│    dsh LLM   │ ◀────────────────────── │ BetterGI Live Hub │
│ 导演 / 解说   │ ──────────────────────▶ │ 采集 + 模拟输入    │
└──────────────┘   指导 / 解说 / 记忆     └────────┬─────────┘
                                                   │ 画面 / 指导
                                                   ▼
                                         ┌──────────────────┐
                                         │  Lumine Bridge   │  本机 Python
                                         └────────┬─────────┘
                                                  │ WS：画面 + 指导
                                                  ▼
                                         ┌──────────────────┐
                                         │ AutoDL Lumine    │  vLLM + 7B
                                         │ Player Server    │  输出动作块
                                         └──────────────────┘
```

- **BetterGI Live Hub**：C# 进程内，仅监听 loopback，Bearer 鉴权。负责按 5～10 fps 采集游戏窗口、广播 JPEG 帧、接收结构化动作块并执行、保存指导/解说时间线。
- **Lumine Bridge**：本机 Python。从 BetterGI 接收画面和指导，按 5 fps 转发给 AutoDL；把 AutoDL 返回的动作块转回 BetterGI。
- **AutoDL Player Server**：跑在 AutoDL GPU 上。加载 `koboshchan/Lumine-Agent-Pretrain-VL-7B`，用 vLLM 推理，解析动作文本并返回结构化动作块。
- **dsh LLM**：导演与解说。通过 BetterGI 的 HTTP/MCP/WS 获取画面，写指导与多维度解说，调用自己的工具和 BetterGI 会话记忆。也可以用仓库里的参考 `live_director.py` 代替。

## 1. 动作格式（Lumine）

模型输出（与官方 `zlc1004/Lumine` 一致）：

```text
<|action_start|>dX dY dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>
<|action_start|><use_absolute> X Y dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>
```

- 每块覆盖 200ms，包含 **6 个 33ms 键块**；每个键块最多 4 个键。
- `dX/dY` 是相对鼠标位移（整数，训练量化单位为 5px / 4px），范围约 ±1000；`dZ` 是滚轮刻度，范围 [-5, 5]。
- `<use_absolute> X Y` 是 1280×720 输出帧中的绝对光标坐标，范围 [0,1279] / [0,719]；用于菜单/UI，不表示相对位移。
- 按键 token：`A`～`Z`、`zero`～`nine`、`One`～`Twelve`（F1～F12）、`Esc`、`Tab`、`Caps`、`Shift`、`Ctrl`、`Alt`、`Space`、`LB`、`RB`、`MB`、`XB1`、`XB2`。
- 可选思考：`<|thought_start|>...<|thought_end|>`，位于动作块之前或之后；解析后作为 `thought` 返回。
- 动作块可以只有 6 个键块，也可以少于 6 个；缺失的键块视为空。

## 2. BetterGI Live Hub

### 2.1 HTTP

基地址 `http://127.0.0.1:30648`，所有请求带 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/agent/live/start` | 启动实时会话 |
| POST | `/api/v1/agent/live/stop` | 停止会话并释放按键 |
| GET | `/api/v1/agent/live/status` | 状态、帧序号、客户端数、队列/饥饿统计 |
| GET | `/api/v1/agent/live/frame` | 最新一帧 JPEG（二进制） |
| POST | `/api/v1/agent/live/guidance` | 设置当前指导文本 |
| POST | `/api/v1/agent/live/commentary` | 追加一条解说事件 |
| GET | `/api/v1/agent/live/timeline` | 读取指导/解说/动作状态时间线 |
| POST | `/api/v1/agent/live/actions` | 直接注入结构化动作块（桥接/调试） |

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

状态响应：

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

指导请求：

```json
{ "text": "先观察左侧，再冲刺到七天神像。", "source": "dsh" }
```

解说请求：

```json
{
  "kind": "reaction",
  "text": "哇，这个丘丘人突然从草丛里冒出来！",
  "emotion": "surprised",
  "frameSeq": 1234,
  "meta": { "scene": "combat" }
}
```

`kind` 建议：`reaction`（即时反应）、`plan`（计划/路线）、`emotion`（情绪）、`lore`（世界观）、`chat`（和观众互动）、`result`（操作结果复盘）。

### 2.2 WebSocket

`ws://127.0.0.1:30648/ws/v1/agent/live?access_token=<token>`

也支持 `Authorization: Bearer <token>` 请求头。浏览器/OBS 来源使用查询参数。

服务端 → 客户端：

```json
{"type":"hello","sessionId":"vtuber-20261026","fps":10,"frameWidth":1280,"frameHeight":720,"actionIntervalMs":33,"chunksPerAction":6}
{"type":"frame","seq":1234,"ts":"2026-10-26T14:00:00.123Z","jpeg":"<base64>","width":1280,"height":720,"capture":{"x":0,"y":0,"width":1920,"height":1080},"runtime":{"isForeground":true,"isMinimized":false},"uiCategory":"Main","guidance":"沿着道路前进"}
{"type":"guidance","seq":12,"text":"沿着道路前进","source":"dsh","ts":"..."}
{"type":"commentary","seq":45,"kind":"reaction","text":"...","emotion":"excited","frameSeq":1234,"ts":"..."}
{"type":"action_state","seq":88,"keys":["W"],"queueDepth":2,"starved":false,"applied":120,"dropped":2}
{"type":"pong","ts":"..."}
{"type":"error","code":"...","message":"..."}
```

客户端 → 服务端：

```json
{"type":"guidance","text":"..."}
{"type":"commentary","kind":"reaction","text":"...","emotion":"excited","frameSeq":1234}
{"type":"actions","seq":88,"chunks":[{"dx":5,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W","Shift"]},{"dx":0,"dy":0,"scroll":0,"keys":["W","Shift"]},{"dx":0,"dy":0,"scroll":0,"keys":[]},{"dx":0,"dy":0,"scroll":0,"keys":[]}],"absolute":{"x":640,"y":360},"thought":"..."}
{"type":"stop_actions"}
{"type":"ping"}
```

> 移动/滚轮前缀只放在第 1 个 33ms 键块；后续键块的 `dx/dy/scroll` 必须为 0。BetterGI 会拒绝后续键块的非零移动，避免 6 倍视角位移。

### 2.3 执行语义

- 收到 `actions` 后，BetterGI 将 6 个键块按 33ms 间隔排队执行；保持按键状态，按差分发送 `KeyDown/KeyUp` 与鼠标按下/抬起。
- 相对鼠标位移通过 `MoveMouseBy(dx,dy)` 发送；`absolute` 通过 1280×720 → 当前游戏画面归一化坐标 → `MoveMouseTo`。
- 滚轮 `scroll` 以刻度发送。
- 游戏失去前台、最小化、用户按下停止开关、运行环境停止时，立即释放所有按键并清空队列。
- 队列超过约 660ms 动作量（20 个 33ms 键块）时丢弃最旧键块；超过 250ms 无新动作时释放按键，避免一直向前跑。
- 危险键（Win、F4、F11、PrintScreen 等）不会被注入。

## 3. AutoDL Player Server

WebSocket：`ws://<autodl-host>:<port>/ws/player?token=<token>`

客户端（本机 Bridge）→ 服务端：

```json
{"type":"hello","sessionId":"vtuber-20261026","fps":5,"protocol":"lumine-live-v1"}
{"type":"frame","seq":1234,"tsMs":1761500000123,"jpeg":"<base64>","guidance":"沿着道路前进"}
{"type":"guidance","text":"..."}
{"type":"reset"}
{"type":"ping"}
```

服务端 → 客户端：

```json
{"type":"hello","model":"koboshchan/Lumine-Agent-Pretrain-VL-7B","fps":5}
{"type":"action","forFrameSeq":1234,"action":"<|action_start|>5 0 0 ; W ; W ; W ; W ; W ; W<|action_end|>","chunks":[{"dx":5,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W"]},{"dx":0,"dy":0,"scroll":0,"keys":["W"]}],"absolute":null,"thought":null,"latencyMs":180,"raw":"..."}
{"type":"status","queue":1,"inflight":true,"dropped":3,"lastLatencyMs":180}
{"type":"error","code":"...","message":"..."}
```

- 服务端只保留最新一帧；推理忙时丢弃旧帧。
- 推理失败/输出无动作块时返回 `error` 或空动作块，Bridge 保持安全停顿。
- `LUMINE_MOCK=1` 时返回脚本动作，便于无 GPU 测试。

## 4. 解说时间线

BetterGI 的 `timeline` 返回统一事件：

```json
{
  "seq": 45,
  "ts": "2026-10-26T14:00:02.345Z",
  "type": "commentary",
  "kind": "reaction",
  "text": "哇，这个丘丘人突然从草丛里冒出来！",
  "emotion": "surprised",
  "frameSeq": 1234,
  "meta": { "scene": "combat" }
}
```

`type` 为 `guidance` / `commentary` / `action_state` / `live.started` / `live.stopped`。该时间线是 VTuber 渲染的输入；视频时间轴以 `live.started` 的 `ts` 为 0。

## 5. 安全边界

- 画面只截取游戏窗口；不读取游戏内存、不抓包、不注入。
- Live Hub 只监听 loopback，Bearer/查询令牌校验；令牌等同游戏控制权。
- 动作仅通过既有 `InputHub` 的 SendInput/PostMessage；危险键与系统组合键被拒绝。
- Bridge/AutoDL 只传输画面和动作，不接收/执行任意脚本。AutoDL 的推理服务应只对可信 IP 开放，建议 SSH 隧道或 TLS + 强令牌。
