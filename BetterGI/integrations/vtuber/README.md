# BetterGI 虚拟主播叠加层与离线渲染

> 不直播、只录整场实况并后期成片时，用 `../session/`：录制器把画面、
> 思考、解说、动作与 VTuber 参数写进一份 `session.jsonl`，渲染器再补齐
> 配音与字幕。本目录的叠加层适合仍然想看实时画面的人。

本目录把 BetterGI 实时会话中的**解说（commentary）**、**指导（guidance）**与**动作状态（action_state）**做成适合直播的虚拟主播画面，并提供一条离线渲染流水线，把同一份时间线烧录进录屏视频。

- **实时叠加层**：`overlay/index.html`，单文件 HTML/CSS/JS，供 OBS 浏览器源使用，背景透明，设计尺寸 1280×720。
- **离线渲染器**：`render_vtuber.py`，读取 BetterGI 时间线，生成 ASS 字幕，调用本机 ffmpeg 输出带字幕 / 头像 / 可选 TTS 的 MP4。
- **协议依据**：`../lumine/PROTOCOL.md` 的 `lumine-live-v1`；本目录不涉及游戏内存、抓包或注入，只消费公开的 Live Hub 接口。

```text
integrations/vtuber/
├── overlay/
│   ├── index.html            # OBS 浏览器源（透明背景、无外部依赖）
│   └── config.example.json   # 配置示例（改写为 config.js 使用）
├── render_vtuber.py          # 离线渲染 CLI（Python 标准库 + ffmpeg）
├── tests/
│   └── test_render_timeline.py
└── README.md
```

---

## 1. 准备工作：启动 BetterGI 实时会话

叠加层只负责显示，**不会自动启动或停止实时会话**。先在 BetterGI 中启用外部访问并取得访问令牌，然后启动会话：

```powershell
# 令牌等同游戏控制权，请只保存在本机。
$env:BGITOKEN = "你的实时会话令牌"

curl.exe -X POST http://127.0.0.1:30648/api/v1/agent/live/start `
  -H "Authorization: Bearer $env:BGITOKEN" `
  -H "Content-Type: application/json" `
  -d '{"sessionId":"vtuber-20261026","fps":10,"quality":70,"maxWidth":1280}'
```

随后可以由导演 / 解说程序写入指导与解说：

```powershell
curl.exe -X POST http://127.0.0.1:30648/api/v1/agent/live/guidance `
  -H "Authorization: Bearer $env:BGITOKEN" `
  -H "Content-Type: application/json" `
  -d '{"text":"先观察左侧，再冲刺到七天神像。","source":"dsh"}'

curl.exe -X POST http://127.0.0.1:30648/api/v1/agent/live/commentary `
  -H "Authorization: Bearer $env:BGITOKEN" `
  -H "Content-Type: application/json" `
  -d '{"kind":"reaction","text":"哇，这个丘丘人突然从草丛里冒出来！","emotion":"surprised","frameSeq":1234}'
```

`kind` 建议使用 `reaction` / `plan` / `emotion` / `lore` / `chat` / `result`，叠加层会为每种类型使用不同强调色。

---

## 2. OBS 场景配置

1. 在 OBS 中新建「浏览器」来源，勾选「本地文件」或直接填写 `file:///` 地址。
2. 分辨率设为 **1280×720**，帧率建议 30，与游戏画面来源保持一致。
3. 背景保持透明；`index.html` 已设置透明背景，不需要额外 CSS。
4. 建议关闭「不可见时关闭源 / 场景切换时关闭」，避免切场景后频繁重连。
5. 不要勾选「通过 OBS 控制音频」——叠加层不播放任何音频。

地址示例（把令牌换成真实值）：

```text
file:///E:/BetterAGI/BetterGI/integrations/vtuber/overlay/index.html?token=你的令牌
```

> 如果 OBS 的「本地文件」选择器不接受查询参数，可以改用下面的 `config.js`，让 URL 保持不带令牌。

### 2.1 令牌的三种来源

叠加层按以下优先级解析令牌，**令牌不会写入控制台日志或画面**：

1. URL 查询参数 `token`（也兼容 `access_token`）；
2. `localStorage` 中的 `vtuber_token`；
3. 同目录 `config.js` 里的 `window.VTUBER_CONFIG.token`。

在浏览器开发者工具中设置 localStorage 的示例：

```js
localStorage.setItem("vtuber_token", "你的令牌");
```

### 2.2 config.js

`config.example.json` 是字段示例。由于浏览器从 `file://` 读取本地 JSON 会受到限制，请把它改写为同目录的 `config.js`：

```js
// integrations/vtuber/overlay/config.js
window.VTUBER_CONFIG = {
  wsUrl: "ws://127.0.0.1:30648/ws/v1/agent/live",
  token: "你的令牌",
  avatarImage: "avatar.png",
  displayName: "小荧",
  accentColors: {
    reaction: "#ff6b9d",
    plan: "#4fc3f7",
    emotion: "#ffd166",
    lore: "#b388ff",
    chat: "#7cf5c0",
    result: "#ffa94d",
    guidance: "#9bd7ff"
  },
  maxCommentaryLines: 8
};
```

| 字段 | 说明 |
|---|---|
| `wsUrl` | BetterGI Live Hub WebSocket 地址，默认 `ws://127.0.0.1:30648/ws/v1/agent/live` |
| `token` | 访问令牌；`REPLACE_ME` 等占位符会被忽略 |
| `avatarImage` | 头像图片路径或 URL；留空使用内置 SVG 占位形象 |
| `displayName` | 头像下方名牌文字 |
| `accentColors` | 各解说类型的强调色 |
| `maxCommentaryLines` | 右侧「解说动态」保留条数，范围 3～30 |

`avatarImage` 使用相对路径时，相对于 `index.html` 所在目录解析。头像建议使用透明背景 PNG；叠加层会添加轻微上下浮动、说话嘴型与情绪徽章。

### 2.3 叠加层会显示什么

- **头像区**：空闲上下浮动；解说打字期间嘴型动画与眨眼加速；右上角显示情绪徽章。
- **对话气泡**：最新解说逐字显示，多个事件自动排队；不同 `kind` 使用不同强调色，并显示情绪标签。
- **右侧面板**：当前指导、按键、直播/饥饿状态、画面分辨率与帧率、最近解说、连接状态。
- **连接状态**：断线后按指数退避自动重连，并显示重连倒计时；会话停止时给出提示。
- **画面帧**：叠加层不显示游戏画面本身（请在 OBS 中另行添加游戏采集），只使用 `frame` 消息更新状态。

---

## 3. 离线渲染

### 3.1 导出时间线

```powershell
curl.exe -H "Authorization: Bearer $env:BGITOKEN" `
  http://127.0.0.1:30648/api/v1/agent/live/timeline `
  -o timeline.json
```

接口可能返回数组，也可能返回 `{ "events": [...] }` 对象，渲染器都支持。事件类型包括 `live.started` / `live.stopped` / `guidance` / `commentary` / `action_state`。

### 3.2 基本用法

```powershell
python integrations/vtuber/render_vtuber.py `
  --video .\recordings\live.mp4 `
  --timeline .\timeline.json `
  --out .\out.mp4 `
  --avatar .\avatar.png `
  --font "Microsoft YaHei"
```

| 参数 | 说明 |
|---|---|
| `--video` | 输入录屏（建议 1280×720） |
| `--timeline` | 时间线 JSON |
| `--out` | 输出 MP4 |
| `--avatar` | 可选头像 PNG；会叠加在右下角，并带 sin 上下浮动与 0.6 秒淡入 |
| `--start-ts` | 时间轴零点（ISO 8601）。缺省时取最早的 `live.started`，再退化到最早事件 |
| `--font` | ASS 字体名称，默认 `Microsoft YaHei`；需系统已安装 |
| `--include-guidance` | 同时渲染 `guidance` 事件（默认只渲染 commentary） |
| `--tts` | 使用 edge-tts 合成解说语音并混入时间轴 |
| `--tts-voice` | edge-tts 音色，默认 `zh-CN-XiaoxiaoNeural` |
| `--avatar-width` | 头像叠加宽度，默认 280 像素 |
| `--ffmpeg` / `--ffprobe` | 手动指定可执行文件；默认从 PATH 查找 |

渲染规则：

- 显示时长按文本长度计算：`2.5 秒 + 0.12 秒/字`，限制在 2～8 秒。
- 事件按时间（同时间按 `seq`）排序；如果与上一条重叠，会**顺延**到上一条结束之后，保证不重叠。
- 时间戳早于零点时钳制到 0；晚于视频结尾的事件丢弃；被视频结尾截断后不足 0.5 秒的字幕丢弃。
- ASS 字幕位于画面底部居中，按 `kind` / `emotion` 生成独立样式；`{}`、反斜杠与换行会被正确转义。
- 视频输出 H.264 + `yuv420p`；输入有音轨时优先直接复制，复制失败自动降级为 AAC。
- 没有 `--avatar` 时只烧录字幕；有头像时使用 `filter_complex` 把头像叠在右下角。

### 3.3 TTS（可选）

```powershell
pip install edge-tts
python integrations/vtuber/render_vtuber.py `
  --video .\recordings\live.mp4 --timeline .\timeline.json --out .\out.mp4 `
  --tts --tts-voice zh-CN-XiaoxiaoNeural
```

- `edge-tts` 是**可选依赖**，未安装时只打印提示并继续渲染无语音版本。
- 每条解说会生成一个临时 mp3，按字幕开始时间延迟后与原始音轨混音；临时文件在渲染结束后自动清理。
- 合成需要网络；单条失败不会中断整体渲染。
- 默认最多合成 80 条，避免超长时间线占用过多时间与磁盘。

### 3.4 推荐的录制流程

1. 在 OBS 中添加「游戏采集」与「浏览器」两个来源，画面尺寸统一 1280×720。
2. 先启动 BetterGI 实时会话，确认叠加层显示「已连接 / 直播中」。
3. 用 OBS 正常录制 MP4（30 或 60 fps）。BetterGI 的缩略帧录制只用于调试，不建议作为成片来源。
4. 录制结束后导出时间线，必要时用 `--start-ts` 对齐录制起点，再运行离线渲染做后期字幕 / 头像 / TTS。
5. 如果直播时已经录到叠加层，离线渲染可以只做字幕版本；两种产物都使用同一份时间线。

---

## 4. 测试

单元测试不调用 ffmpeg、不需要 GPU：

```powershell
python -m unittest discover -s integrations/vtuber/tests -v
```

覆盖时间戳解析、`live.started` 零点选择、视频时长钳制、顺序排期、显示时长、ASS 特殊字符转义、样式生成与 ffmpeg 命令构建。

---

## 5. 隐私与安全

- **令牌等同游戏控制权。** URL 查询参数中的 `token` 会出现在 OBS 场景配置、浏览器历史、截图和录屏里；不要把场景集合或包含令牌的截图发给他人。
- 优先使用 `config.js` 保存令牌，并限制该文件的访问权限；建议把 `overlay/config.js` 加入本机 `.gitignore`（本目录不修改仓库级 `.gitignore`）。
- 叠加层默认只连接 `127.0.0.1` 上的 Live Hub，默认不加载任何 CDN、字体或音频；`avatarImage` 若填写远程 URL，会由浏览器直接加载，请自行确认来源可信。令牌不会写入 DOM、`console` 或错误提示。
- 如果令牌泄露，立即在 BetterGI 中轮换令牌并重启实时会话。
- 时间线中的解说文本可能来自模型或第三方，只作为数据显示；叠加层使用 `textContent` 渲染，不执行 HTML。
- 本目录不包含游戏内存读取、网络抓包或注入代码。所有输入均为 BetterGI 对外公开的 Live Hub 接口与用户主动提供的录屏 / 时间线。

---

## 6. 已知限制

- 内置头像是 **SVG 占位形象**；`avatarImage` 支持静态 PNG。若需要 Live2D / VTube Studio，可把本叠加层作为信息层，用 VTube Studio 的透明窗口采集替换头像区域，或在 OBS 中叠加独立的 Live2D 来源。
- 离线渲染的头像同样是**静态图片**（带浮动与淡入），不做口型同步；TTS 与字幕时间轴对齐，但不做音素级口型。
- 需要系统安装 `--font` 指定的中文字体；缺失时 libass 会回退字体，可能出现字形差异。
- 没有 `ffprobe` 时无法获知视频时长与音轨，时间戳不会按结尾裁剪；建议安装完整 ffmpeg 发行版。
- 叠加层的断线重连是无限退避重试；如果 BetterGI 未启动，状态会一直显示为断开。
