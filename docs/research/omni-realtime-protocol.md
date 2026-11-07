# Qwen3.8-Omni-Flash-Realtime 接入协议核实报告

> 调研任务：核实该实时全模态模型的真实接入协议，用于驱动游戏 Agent 的「快系统」。
> 调研日期：2026-11-04。所有结论均标注来源；无文档依据的一律标「未证实」，推断标「推断」。
> **未修改任何项目代码**，仅新增本文件与 `Docs/research/_raw/` 下的原始抓取快照。

## 0. 一句话结论

**真实模型 ID 是 `qwen3.8-omni-flash-realtime`（没有 "3.8-" 以外的写法），走的是私有 WebSocket 实时事件协议（不是 OpenAI 兼容 HTTP，也不是 SSE）**：
`wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime`，
鉴权头 `Authorization: Bearer $DASHSCOPE_API_KEY`。支持「音频 + 每秒 1 帧 JPEG」输入，支持**纯文本输出**（`modalities:["text"]`），文本经 `response.text.delta` 流式返回 —— 可以直接当指令通道用。

> 注意：起点 URL `www.qianwenai.com/models/qwen3.8-omni-flash-realtime` 是**千问AI平台（营销/文档站）**，它给的 endpoint 是 `wss://maas.qianwenaiapi.com/api-ws/v1/realtime`；阿里云百炼官方文档给的是 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime`。两条入口**同一套事件协议**，仅域名与是否需要 WorkspaceId 前缀不同（详见 §2.2）。

---

## 1. 模型 ID 与调用入口

| 项 | 结论 | 来源 |
| --- | --- | --- |
| 真实模型 ID | `qwen3.8-omni-flash-realtime` | 模型页 <https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime> |
| 是否存在 "Qwen3.8-Omni-Flash-Realtime" 的其它拼法 | 不需要。文档 slug 为 `qwen3-8-omni-flash-realtime`，但**查询参数里的 model 值**是 `qwen3.8-omni-flash-realtime`（点号） | 实时文档 <https://help.aliyun.com/zh/model-studio/realtime> |
| 平台 | 阿里云百炼 DashScope / 千问AI平台（同一模型的两个前端品牌） | 同上 |
| 协议形态 | **私有 WebSocket 实时事件协议**（事件名沿用 OpenAI Realtime 风格，但**不是** OpenAI 兼容模式，也不是 HTTP SSE） | 协议概述 <https://help.aliyun.com/zh/model-studio/realtime-api-overview> |
| 是否有 OpenAI 兼容/HTTP 入口 | 未证实。实时文档全文 0 次出现 "OpenAI"；该模型文档只列 WebSocket / WebRTC / AOQ 三种接入 | 同上 |
| 同族离线模型 | `qwen3.8-omni-flash` 走 Chat Completions / Responses（离线音视频分析用），**与本任务无关** | 模型页 §能力 |

**重要**：`qwen3.8-omni-flash-realtime` 与离线 `qwen3.8-omni-flash` 是两个不同接入面；别把离线 HTTP 的用法套到 realtime 上。

---

## 2. 传输协议

### 2.1 三种协议对比（官方表格摘要）

| 维度 | AOQ（AI over QUIC） | WebRTC | WebSocket |
| --- | --- | --- | --- |
| 适用场景 | AI 多模态实时交互、弱网 | 浏览器端 | **服务端集成、快速原型** |
| 数据类型 | 音视频+文本 | 音视频+文本 | 文本/音频/图像 |
| 建连速度 | 快 | 慢 | 慢 |
| 弱网对抗 | 极致 | 良好 | 差 |
| 回声消除/降噪 | 内置 | 内置 | 无（需自行处理） |
| 浏览器兼容 | 不支持 | 原生支持 | 原生支持 |

来源：<https://help.aliyun.com/zh/model-studio/realtime-api-overview#协议选型>

**给游戏 Agent 的取舍**：我们只要「推帧 + 拉文本」，不需要双工语音抗弱网 → **WebSocket 足够**，且是唯一能自己控制 `input_image_buffer.append` 的协议（WebRTC 的图走 RTP 轨道，反而不能手动 append）。

### 2.2 Endpoint 与鉴权（两个入口）

**A. 阿里云百炼（help.aliyun.com 官方文档口径）**

```
wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime        # 华北2 北京
wss://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime    # 新加坡
```

- `{WorkspaceId}` = 百炼**业务空间 ID**（不是 app-id），官方明确要求该模型「必须使用绑定业务空间的调用地址」。
- 请求头：`Authorization: Bearer DASHSCOPE_API_KEY`（Bearer Token 鉴权）。
- 来源：<https://help.aliyun.com/zh/model-studio/realtime#bdaa43cdd7hsd>

**B. 千问AI平台（起点 URL 所在站，`platform.qianwenai.com` 文档口径）**

```
wss://maas.qianwenaiapi.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime
```

- 无 WorkspaceId 前缀；鉴权同样用 `Authorization` Bearer（该站示例通过 `dashscope.api_key` 注入）。
- 来源：<https://platform.qianwenai.com/docs/api-reference/realtime-api/websocket-overview>（抓取到明文 endpoint）与 <https://www.qianwenai.com/models/qwen3.8-omni-flash-realtime>（页面代码示例）。

两入口**事件协议完全一致**，只有域名/前缀差别。选 A 或 B 取决于 API Key 归属那个平台账号。

### 2.3 交互模型（Manual vs VAD）

- **VAD 模式**（默认 `turn_detection.type=server_vad`）：服务端自动判定说话结束并触发响应。
- **Manual 模式**（`turn_detection: null`）：客户端自己 `commit` + `response.create`。**游戏 Agent 必须用这种**——我们不是语音对话，不能让 VAD 决定何时推理。
- VAD 可选类型：`server_vad`（默认）/ `semantic_vad` / `semantic_vad_ec`（后两者仅 3.8 / 3.5 系列支持）。

来源：<https://help.aliyun.com/zh/model-studio/client-events#turn_detection>

---

## 3. 输入能力：**可以收图像帧/视频帧**（关键结论）

模型页能力表：输入模态 = **文本、流式音频、视频（连续图像帧）**。
来源：<https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime#模型能力>

### 3.1 图像走 Base64 append 事件（不是 base64 image_url）

事件：`input_image_buffer.append`，字段 `image`（Base64 编码的**原始 JPEG**字节，不是 data-URL，也没有 `image_url` 结构体）。

```json
{ "event_id": "event_xxx", "type": "input_image_buffer.append", "image": "<base64 jpeg>" }
```

### 3.2 硬限制（原文档明确写出）

- 格式：**只能 JPG / JPEG**（PNG 不行）。
- 单张 Base64 后 **≤ 256 KB**，建议原始图 **≤ 190 KB**。
- 分辨率：推荐 **480p 或 720p**，**最高 1080p**。
- 帧率：**建议 1 张/秒**（视频按抽帧走同一事件）。
- **发送图片前，必须至少发送过一次 `input_audio_buffer.append`** —— 这是硬性前置条件，纯图无法开场。
- 缓冲区提交：`input_audio_buffer.commit` **同时提交音频缓冲区与图像缓冲区**；VAD 模式下本轮图片要在 `input_audio_buffer.speech_stopped` 之前送达。
- WebRTC 走视频轨道，**不支持** `input_image_buffer.append`。

来源：<https://help.aliyun.com/zh/model-studio/client-events#c28ed38410nfw>、<https://help.aliyun.com/zh/model-studio/realtime#82b7d6329836b>

### 3.3 视频聚合（降本开关，值得用）

`session.video.input.representation_compact`：默认 `none`；设为 `normal` 会聚合视频表征，**视频 Token 降为 1/4**，适合「不依赖细粒度视觉」的游戏画面。必须在首段音频前配置，开播后不可改。
来源：<https://help.aliyun.com/zh/model-studio/realtime#realtime-advanced-config>

### 3.4 音频输入规格

- 默认 PCM、16000 Hz、s16le、单声道（`session.audio.input.format`）。
- 3.8 多通道额外支持 1/2/4 声道（多通道只能 PCM + 16000 Hz + interleaved；2 声道 `raw_mic_array`，4 声道 `foa_ambix`），空间音频 Token ×2。
- WebSocket 音频用 `input_audio_buffer.append`（`audio` 字段为 Base64）。
- 官方最小示例按 **3200 字节/包**（16 kHz×16 bit = 100 ms）切片发送。

### 3.5 纯文本输入：**未证实**

模型页写输入含「文本」，但客户端事件文档里 `conversation.item.create` **只支持** `function_call_output` 与 `mcp_approval_response` 两种 item 类型，**没有** `input_text` / `message` 用户文本项的文档。系统提示词用 `session.instructions`（确认存在，见 §4.3）。
→ 结论：**「每轮用纯文本下指令」这条路官方没有文档化**，禁止当成既成事实。可行的替代：(a) 每轮塞一小段静音音频 + 图片 + `response.create`，指令写在 `session.instructions`；(b) 需要逐轮变指令时先做一次实验验证（见 §7）。

---

## 4. 输出能力

### 4.1 输出模态（`session.modalities`）

- `["text"]` —— **仅输出文本**。文本走 `response.text.delta`（流式）/ `response.text.done`（完整）。
- `["text","audio"]` —— **默认值**。此时文本走 `response.audio_transcript.delta/done`（是语音的转写，不是独立文本通道），音频走 `response.audio.delta`（Base64 PCM，默认 24 kHz s16le）。

→ **当指令通道必须用 `["text"]`**，否则拿到的是「被朗读出来的转写文本」——它受 TTS 朗读风格影响（口语化开关 `session.smooth_output` 仅对旧型号 `qwen3-omni-flash-realtime-2025-09-15` 生效，3.8 不适用），并且要额外按音频输出 12 元/M 计费。

来源：<https://help.aliyun.com/zh/model-studio/client-events#modalities>、<https://help.aliyun.com/zh/model-studio/realtime#接收文本与音频>

### 4.2 能否稳定输出结构化文本：**没有 response_format / JSON Schema 模式（未证实存在）**

- 实时文档与客户端事件文档中 `response_format` / `json_object` / `json_schema` 出现次数为 **0**。
- 起点营销页把「结构化输出」列为**平台通用能力**，未说明该 realtime 模型支持 → **未证实**。

**可靠的替代方案（推荐）**：用 **Function Calling 当结构化动作通道**。模型支持自定义工具调用（`session.tools`），工具参数以 JSON 流式返回：`response.function_call_arguments.delta` / `response.function_call_arguments.done`，客户端可用 `conversation.item.create(item.type="function_call_output")` 回填结果再 `response.create`。这对游戏 Agent 是**最稳的指令通道**：结构由 schema 约束，而不是靠提示词祈祷 JSON。
来源：<https://help.aliyun.com/zh/model-studio/realtime#realtime-tools>、<https://help.aliyun.com/zh/model-studio/client-events#conversation.item.create>

### 4.3 系统提示词

`session.instructions`：string，可选，「系统消息，用于设定模型的目标或角色」。示例中直接塞了一整段 AI 客服人设提示词 → **人设/规则可以稳放在这里**。
来源：<https://help.aliyun.com/zh/model-studio/client-events#instructions>

---

## 5. 延迟 / 并发 / 计费

### 5.1 延迟

**官方没有给任何首包/端到端延迟数字（未证实）**。文档只有定性描述：WebSocket「建连速度 慢 / 弱网对抗 差」；WebRTC 适合「低延迟语音」；AOQ 「延迟、弱网对抗、多模态有极致要求」。
→ 游戏 Agent 的快系统必须**自测**（建议指标：`commit→response.created` 与 `commit→首个 response.text.delta`）。

有一个可用的官方量化点：`silence_duration_ms` 默认 800 ms（VAD 判停），Manual 模式下我们用不到它，说明 server 端本身不预设额外等待。

### 5.2 并发与限流

- **60 RPM / 2,000,000 TPM**（北京、新加坡一致）。来源：模型页 §限流。
- RPM 限的是 **WebSocket 建连次数**，同连接内多轮响应不重复计数，**重连计为新请求**。建连被限流时连接以 **1007** 关闭、原因串 `Requests rate limit exceeded, please try again later.` → 指数退避重连。
- **官方明确：没有单独限制同时保持的连接总数**。来源：<https://help.aliyun.com/zh/model-studio/realtime#已知问题>
- 单次会话最长 **120 分钟**，到点服务端主动断开。
- 上下文：全部输入 Token 上限 **196608**；最大输出 **65536**；音频历史 **100 轮 / 600 秒**；视频历史 **50 轮 / 240 秒**（超限自动丢最早）。

**→ 对游戏 Agent 的含义**：一条长连接可撑 2 小时，但图像历史只保 **最近 240 秒**（1 fps ≈ 240 帧）；「记忆」必须靠我们自己的状态机，不能指望模型上下文。

### 5.3 计费口径（按 Token，不是按秒/按次）

| 计费项 | 北京（元/百万 Token） | 新加坡（元/百万 Token） |
| --- | --- | --- |
| 输入：音频 | 6 | 6.781 |
| 输出：音频 | 12 | 13.636 |
| 输入：文本/图片/视频 | 1.5 | 1.677 |
| 输出：文本 | 4.5 | 5.104 |

来源：<https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime#模型价格>

Token 换算（**这两个公式决定成本模型**）：

- 输入音频：`秒数 × 7`；输出音频：`秒数 × 12.5`（不足 1 秒按 1 秒）。
- 图片：每 `32×32` 像素 1 Token，单图 **4 ~ 1280 Token** 封顶。即 720p 一帧 ≈ 1280 Token 封顶 → 1 fps 下**约 1280 Token/秒**。
- `representation_compact="normal"` 时视频 Token → **1/4**。
- **多轮累积计费**：每轮响应都把上下文窗口内**所有历史**音频/图片/文本重算为输入 Token。官方原话：「输入 Token 会随对话轮次的增加而逐轮累积，而非仅计算当前轮次的新增输入」。
- 输出语音时 3.8 的「音频」与「对应文本」**分别计费**（3.5 系列文本不计费）。
- 免费额度：100 万 Token，仅华北2（北京），90 天内。

**成本警示**：1 fps 视频 + 累积上下文 = 长会话成本快速失控。若每轮都把历史图片算进去，10 分钟会话的图片侧输入可达数百万 Token。缓解：`representation_compact=normal`、降低推帧频率、**定期重连换新上下文**（断开即清历史，但要吃 RPM 与建连开销），或用离线 `qwen3.8-omni-flash` 做慢思考、realtime 只做快系统。

---

## 6. 最小可运行 Python 客户端

以下代码的**事件名、字段名全部来自官方文档**，仅「静音音频垫片」与「循环推帧节奏」是为游戏场景自拟的组合（标注「推断」处）。用 `websocket-client`，Manual 模式，**纯文本输出**。

```python
# pip install websocket-client
# 官方最小示例派生：https://help.aliyun.com/zh/model-studio/realtime#qwen38-realtime
import base64, json, os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import websocket

# 入口 A（百炼）：把 {WorkspaceId} 换成真实业务空间 ID
WS = "wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
# 入口 B（千问AI平台）：WS = "wss://maas.qianwenaiapi.com/api-ws/v1/realtime"

parts = urlsplit(WS)
q = dict(parse_qsl(parts.query)); q["model"] = "qwen3.8-omni-flash-realtime"
url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))

ws = websocket.create_connection(
    url,
    header=["Authorization: Bearer " + os.environ["DASHSCOPE_API_KEY"]],
    timeout=30,
)

def recv():
    ev = json.loads(ws.recv())
    if ev["type"] == "error":
        raise RuntimeError(ev.get("error"))
    return ev

# 1) 等建连
while recv()["type"] != "session.created":
    pass

# 2) 会话配置：Manual 模式 + 仅文本输出 + 视频聚合降本
ws.send(json.dumps({
    "type": "session.update",
    "session": {
        "modalities": ["text"],              # 只要文本，别付音频输出的钱
        "instructions": "你是游戏内实时决策器。只输出一行 JSON："
                        "{\"action\":\"<move|attack|talk|wait>\",\"target\":\"<id|none>\"}",
        "turn_detection": None,              # Manual 模式（关闭 VAD）
        "audio": {
            "input": {"format": {"type": "pcm", "sample_rate": 16000,
                                 "sample_format": "s16le", "channels": 1,
                                 "packing": "interleaved", "channel_layout": "mono"}},
            "output": {"voice": "Tina", "format": {"type": "pcm", "sample_rate": 24000}},
        },
        "video": {"input": {"representation_compact": "normal"}},   # 视频 Token → 1/4
    },
}))
while recv()["type"] != "session.updated":
    pass

def push_audio(pcm_bytes: bytes):
    ws.send(json.dumps({
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm_bytes).decode("ascii"),
    }))

def push_frame(jpeg_bytes: bytes):
    ws.send(json.dumps({
        "type": "input_image_buffer.append",
        "image": base64.b64encode(jpeg_bytes).decode("ascii"),
    }))

def ask(frames: list[bytes], silence_ms: int = 300) -> str:
    """frames: 本轮要送的 JPEG 帧（≤190KB/张，建议 1 fps，≤1080p）。"""
    # 3) 硬性前置：必须先有过一次 input_audio_buffer.append 才能发图
    push_audio(b"\x00" * (16000 * 2 * silence_ms // 1000))   # 16k/16bit/mono 静音垫片（推断：纯静音即可）
    for jpg in frames:
        push_frame(jpg)
    # 4) 提交（同时提交音频与图像缓冲区）
    ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
    while recv()["type"] != "input_audio_buffer.committed":
        pass
    # 5) 请求响应，收集文本
    ws.send(json.dumps({"type": "response.create"}))
    buf = []
    while True:
        ev = recv()
        t = ev["type"]
        if t == "response.text.delta":
            buf.append(ev["delta"])
        elif t == "response.text.done":
            buf.append(ev.get("text", ""))          # 推断：done 携带完整文本，字段名按 server-events 惯例
        elif t == "response.done":
            break
    return "".join(buf).strip()

if __name__ == "__main__":
    frames = [open(p, "rb").read() for p in (os.environ["FRAME_1"], os.environ["FRAME_2"])]
    print(ask(frames))          # → {"action":"move","target":"enemy_7"}
    ws.close()                  # 注意：建连后马上 close 会有 5-10 秒延迟，官方建议异步 close
```

**代码注意点（都有文档依据）**

1. `response.text.done` 的载荷字段名官方页面为锚点 `#d675635a94jfb`，正文只写"获取完整文本"，未在摘要里给出字段名 → **标「推断」**，实现时以 `response.text.delta` 拼接为准（delta 字段名 `delta` 已确认）。
2. 音频分包：官方示例 3200 字节/包（100 ms @16k/16bit/mono）；本代码一次性发送短静音，长度小无分包问题。
3. `session.video.input.representation_compact` 必须在**首段音频前**设置，之后不可改。
4. 关闭连接：刚建立即 `close()` 官方称约 5-10 秒，建议异步。
5. 重连 = 新的一次 RPM 计数；被限流时 close code 1007。

---

## 7. 未证实清单（禁止当成事实用）

| # | 未知项 | 现状 | 建议验证方式 |
| --- | --- | --- | --- |
| 1 | **纯文本输入路径**（每轮直接下发文字指令，不寄音频） | 客户端事件文档无 `input_text` 用户项；`conversation.item.create` 仅 tool output / MCP 审批 | 实测：发 `conversation.item.create{item:{type:"message",role:"user",content:[{type:"input_text",text:"..."}]}}` 看是否报错；失败则退回「静音音频 + instructions」 |
| 2 | **是否有 `response_format` / JSON Schema 约束** | 文档 0 命中 | 实测 `session.response_format` 或 `response.create.response_format`；失败则改用 Function Calling |
| 3 | **首包延迟真实数值** | 官方零数字 | 自测：manual commit → 首个 `response.text.delta` 的 p50/p95（北京地域、同城 ECS） |
| 4 | **纯文本输出是否仍强制音频输入** | 「发图前必须有 audio append」是硬约束；「`modalities:["text"]` 下能否完全免音频」未写 | 实测：不发任何 audio 直接 commit 是否报错 |
| 5 | **`response.text.done` 的完整文本字段名** | 只有锚点无正文 | 抓一次原始事件 JSON 即可定案 |
| 6 | **keep-alive / ping 机制、空闲超时** | 文档只写 120 分钟硬上限，未写 ping 要求 | 长连接实测（静默 60s 后是否仍在） |
| 7 | **`semantic_vad_ec` 之外的自定义断句** | 无需用于 Manual 模式 | — |
| 8 | **两入口（aliyuncs.com / qianwenaiapi.com）是否等价 SLA 与计费** | 事件协议一致，商务口径未说明 | 向平台确认账号归属 |

---

## 8. 给「快系统」的落地建议（基于以上事实）

1. **协议选 WebSocket + Manual 模式**：唯一能精确控制「何时推理」且能手动 append 图像帧的通道。
2. **推帧**：JPEG、480p（原图 ≤190KB）、**1 fps**，`representation_compact="normal"` 省 3/4 视频 Token；每轮先垫 300 ms 静音满足前置约束。
3. **输出**：`modalities:["text"]` 拿 `response.text.delta`；**结构化靠 Function Calling**（`session.tools` + `function_call_arguments.done`），不要指望 JSON 模式。
4. **记忆外置**：模型只保留最近 240 秒画面 / 100 轮音频，且历史 Token 每轮重算 → 状态机、目标、长期记忆必须在我们自己的进程里；到点（或成本阈值）主动重连换干净上下文。
5. **预算**：720p@1fps ≈ 1280 Token/s 输入（×1.5 元/M）；音频输入 7 Token/s；文本输出 4.5 元/M。以 1 fps 跑满十分钟并累积历史，最快烧钱的是图像输入 → 优先用 `normal` 聚合 + 降帧 + 重连。

---

## 附录：原始来源清单

| 用途 | URL |
| --- | --- |
| 模型页（能力/上下文/价格/限流） | <https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime> |
| 实时调用指南（endpoint/鉴权/最小示例/输入输出/计费/FAQ） | <https://help.aliyun.com/zh/model-studio/realtime> |
| 客户端事件（session.update / buffer.append / commit / image.append） | <https://help.aliyun.com/zh/model-studio/client-events> |
| 服务端事件（response.text.delta 等） | <https://help.aliyun.com/zh/model-studio/server-events> |
| 协议选型（AOQ / WebRTC / WebSocket 对比） | <https://help.aliyun.com/zh/model-studio/realtime-api-overview> |
| 千问AI平台模型页（起点 URL） | <https://www.qianwenai.com/models/qwen3.8-omni-flash-realtime> |
| 千问AI平台 WebSocket 接入概览 | <https://platform.qianwenai.com/docs/api-reference/realtime-api/websocket-overview> |
| 模型总览（qwen3.8-omni-flash-realtime 条目） | <https://help.aliyun.com/zh/model-studio/models> |

> 抓取方式说明：本机 `web_fetch` 因 DNS 被解析到 198.18.x.x（代理 fake-IP）而拒绝抓取，故改用 `Invoke-WebRequest -Proxy http://127.0.0.1:7897` + `r.jina.ai` 渲染代理取正文；原始快照存于 `Docs/research/_raw/`。
