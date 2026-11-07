# BetterGI 实时解说接入

[BetterGI](https://github.com/babalae/better-genshin-impact) 跑游戏时可以通过实时会话 WebSocket 把画面帧、指导与**解说文本**广播给本地客户端。Amaidesu 的 `StreamerAgent` 可以只读订阅这条流：解说文本已经是成品台词，直接交给主播既有的发言管线播出（语音合成、字幕、`streamer.speech` 业务事件，虚拟形象按情绪反射），**不经过语言模型**——主播不会把游戏解说改写成自己的话。

## 数据链路

```
BetterGI 实时会话（ws://127.0.0.1:30648/ws/v1/agent/live，Bearer 鉴权）
        ↓ 推解说 commentary（画面帧 frame 在此丢弃）
StreamerAgent.BetterGILive（类型白名单 → 判重 → 有界队列）
        ↓ SpeechDispatcher.dispatch
TTS 播放队列 / 字幕 / streamer.speech 业务事件 → VTS 等虚拟形象反射情绪
```

解说与主播自主发言**共用同一个发言管线**：同一条播放队列、同一个语音引擎、同一张嘴。两条来源排队播出，不会出现两路声音叠在一起。

## 前置条件

1. **BetterGI 与 Amaidesu 在同一台机器**——接入只允许本机回环地址（`127.0.0.1` / `localhost` / `::1`），避免把访问令牌发到别处。
2. **BetterGI 侧开启外部访问与智能体实时会话**，并记下它生成的访问令牌（BetterGI 的外部访问设置里可见）。端口默认为 `30648`，以 BetterGI 实际设置为准。
3. **BetterGI 处于实时会话运行中**：解说事件只在会话运行期间产生，会话没开时不会有任何解说。

## 配置步骤

### 1. 设置访问令牌（只放环境变量）

令牌不写进配置文件，也不放进 URL：

```powershell
# 当前 PowerShell 会话生效；写入用户环境变量则用 [Environment]::SetEnvironmentVariable
$env:BETTERGI_LIVE_TOKEN = "<BetterGI 的访问令牌>"
```

### 2. 打开接入

编辑 `config/agents.toml`：

```toml
[agents.streamer.bettergi_live]
enabled = true
url = "ws://127.0.0.1:30648/ws/v1/agent/live"   # 默认值；端口以 BetterGI 为准
queue_max = 20        # 待播解说的有界队列长度，满了丢最旧的一条
reconnect_ms = 2000   # 断线重连间隔（毫秒，100～60000）
```

字段的权威定义在
[`StreamerConfig` 的 `BetterGILiveConfig`](../../src/agents/streamer/config.py)。
其余字段不用手抄，配置加载管线会把缺省键补默认值并写回。

该段默认关闭。首次运行生成配置后它已经出现在 `agents.toml` 里，把 `enabled` 改成 `true` 即可。

### 3. 重启 Amaidesu

接入随 `StreamerAgent` 启停：发言管线就绪后才开始连接，停播时先断开连接再停发言队列。

## 独立运行器（不启动主播 Agent、不调用语言模型）

如果解说台词由外部进程（例如 BetterGI 侧的导演模型）生成，Amaidesu 只需作出声端，可以单独运行它，不启动主播 Agent、不构造 LLM、不起 Dashboard：

```bash
# 在仓库根目录运行（data/vts_token.txt 等相对路径以仓库根为基准）
uv run python -m src.bettergi_live_runner
uv run python -m src.bettergi_live_runner --debug --filter StreamerAgent.BetterGILive
uv run python -m src.bettergi_live_runner --seconds 30   # 跑 30 秒后自动退出，冒烟用
uv run python -m src.bettergi_live_runner --dry          # 只验证配置加载与装配，不连接任何外部服务
```

运行器只装配"出声道"：TTS 引擎、字幕服务、口型分析器、虚拟形象平台 Provider（VTS / Warudo / VRChat / 表情差分）、`SpeechDispatcher` 与 `BetterGILiveBridge`，装配入口与主程序是同一批函数。解说经 `SpeechDispatcher` 播出并驱动虚拟形象表情与口型；主程序里"主播自主发言"是第二条来源，运行器里没有这条来源。

开启条件与行为：

- **凭证仍从环境变量读**：`BETTERGI_LIVE_TOKEN`，缺失时只停解说接收并在日志提示，TTS / 字幕 / 虚拟形象照常启动。
- **配置段的 `enabled` 不影响运行器**：`[agents.streamer.bettergi_live].enabled` 只控制主程序内嵌接入；独立运行器以"进程被启动"为开关，URL、队列上限、重连间隔仍从该段读取。
- **首次连接 VTube Studio** 时 VTS 会弹出授权窗口，需要点击允许；批准的 token 写入 `data/vts_token.txt`，后续启动不再询问。
- **降级**：VTS 连不上时只让虚拟形象降级，语音与字幕照常；TTS 未启用时解说只出字幕与 `streamer.speech` 业务事件。
- **退出**：Ctrl+C；先断 BetterGI 连接，再停发言队列、虚拟形象连接与事件总线。

> 主程序与独立运行器不要同时开着同一份 `bettergi_live` 配置——两边都会连接并播出，出现重复发言。

## 行为边界

- **只收解说**：消息类型白名单只放行 `commentary`；画面帧（base64 JPEG，高频）、指导、动作状态、错误等消息一律丢弃，画面不会进入任何下游，更不会进入语言模型。
- **只读一方向**：连接建立后不向 BetterGI 发送任何应用消息（不回写解说、不执行动作、不发送指导）。
- **不重复播出**：同一会话内按 `（会话号, seq, timestampUtc）` 判重；重连拿到重复消息不会重播。会话号来自连接建立时的 `hello` 消息，会话结束（`live.stopped`）后清空判重表。
- **断线重连**：连接失败或中断后按 `reconnect_ms` 间隔无限重连，只记 warning，不阻塞启动。断线期间的解说不补播。
- **情绪映射**：BetterGI 的情绪标签按项目情绪词表收敛——`calm` → `neutral`，`worried` → `scared`，词表外的值 → `neutral`；`excited`、`surprised` 等合法值原样透传。强度固定 0.5（BetterGI 不提供强度）。
- **敏感词**：解说直接播出，不经过 Replyer 的敏感词净化（净化是语言模型输出的环节）。需要过滤时请在 BetterGI 侧处理。
- **队列上限**：待播解说超过 `queue_max` 时丢弃最旧的一条并记 warning，内存占用有界。

## 验证

1. Amaidesu 日志出现 `BetterGI 实时会话已连接`（`--filter BetterGILive` 只看该接入的日志）。
2. BetterGI 产生一条解说后，Amaidesu 的语音合成播出该句，字幕同步显示，Dashboard 实时事件流出现 `streamer.speech`。
3. 未设置 `BETTERGI_LIVE_TOKEN` 时日志提示 `未设置环境变量 BETTERGI_LIVE_TOKEN，BetterGI 解说接入未启动`——这是预期行为，填入令牌后重启即可。

## 排障

| 现象 | 原因与处置 |
|---|---|
| 日志提示未设置令牌 | 环境变量只在启动 Amaidesu 的进程里生效；确认变量已设置后重启 |
| 反复 `连接中断或失败` | BetterGI 未启动、外部访问/智能体实时会话未开启、端口不一致或令牌错误 |
| 地址不合法的 error 日志 | 地址必须是 `ws://` / `wss://` 的回环地址，且不能带用户名、密码、查询参数——令牌只放环境变量 |
| 连上了但没有解说 | BetterGI 的实时会话没在运行；解说事件只在会话运行期间产生 |
| 运行器日志 `VTS 连接失败: 'authenticated'` | VTS 授权未通过：在 VTS 弹出的授权窗口点「允许」；批准后 token 写入 `data/vts_token.txt`，Provider 会自动重连，无需重启 |
| 运行器启动后没有声音 | 检查 `[tts].enabled` 与 `[tts].provider`：`edge_tts` 需要外网（进程内按需设 `HTTP(S)_PROXY`），GPT-SoVITS 需要本机语音服务在跑 |
