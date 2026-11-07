# 实况录制与离线成片

这套工具把一整场「AI 操作 + 思考 + 解说」的实况保存下来，不做直播推流。
录制结束后得到两样东西：

* `gameplay.mp4`：整场游戏画面；
* `session.jsonl`：一份纯文本、逐行 JSON 的会话记录，包含智能体的思考、
  解说、模拟输入、VTuber 模型参数与时间轴。

语音不在录制时生成。后期渲染时再合成（飞鱼 CosyVoice3、Windows SAPI 或
SVC 变声），
按 `session.jsonl` 里的时间与预估时长对齐，因此录音设备、网络延迟都不会
影响口型与字幕。

## 目录结构

```text
sessions/<会话id>/
    session.jsonl     # 唯一权威记录（可直接用编辑器或 jq 读）
    gameplay.mp4      # 实况画面
    subtitles.ass     # 后期渲染生成
    audio/            # 后期配音，base-*.wav / 000001.wav ...
    final.mp4         # 后期成片
    render.json       # 每条配音的实际时长、拉伸比与警告
```

## 会话文件

`session.jsonl` 第一行是元信息，之后每行一个事件。所有事件共用毫秒时间轴
`t`（从录制起点开始）：

```json
{"type":"session","version":1,"session_id":"20261027-153000-ab12cd","created_at":"2026-10-27T07:30:00.000Z","t0_unix_ms":1761540600000,"source":{"kind":"bettergi-live","capture":"frames"},"video":{"path":"gameplay.mp4","fps":15,"width":1280,"height":720},"avatar":{"defaults":{"ParamMouthOpenY":0.0}}}
{"t":820,"type":"thought","text":"前面有丘丘人，先绕开。","frame":41}
{"t":1500,"type":"speech","text":"哇，这边居然藏了个宝箱！","kind":"reaction","emotion":"surprised","dur":2100,"avatar":{"expression":"surprised","params":{"ParamMouthForm":0.15},"mouth_open":{"step_ms":50,"values":[0.0,0.32,0.71,0.55]}}}
{"t":1600,"type":"action","keys":["W","Shift"],"mouse":{"dx":12,"dy":-4},"scroll":0,"source":"block"}
{"t":1700,"type":"avatar","op":"pose","params":{"ParamAngleX":1.8,"ParamBreath":0.74}}
{"t":3040,"type":"avatar","op":"blink","dur":120}
{"t":9000,"type":"end","events":42,"duration_ms":9123,"counts":{"frames":137,"speeches":5,"thoughts":7,"actions":88}}
```

| 事件 | 含义 |
|---|---|
| `thought` | 智能体的内部思考，不朗读 |
| `speech` | 要朗读的解说，含情绪、预估时长 `dur` 与口型包络 |
| `action` | 模拟输入快照（按键、鼠标位移、滚轮、可选模型思考） |
| `avatar` | VTuber 操作：`pose` 姿态、`blink` 眨眼；`params` 是 VTube Studio 参数 ID |
| `guidance` | 智能体发给操作模型的指导文本 |
| `video` | 首个画面到达的时间偏移 |
| `audio` | 后期写回的配音条目（可选） |
| `error` | 录制期间的错误 |
| `end` | 会话结束、总时长与计数 |

口型包络按 50ms 采样，对应 `ParamMouthOpenY`。它只由文本与 `dur` 推导，
所以即使录制时没有声音，后期也能把配音对齐到这张口型表。表情名
（`happy`、`sad` 等）是通用名，驱动具体模型时按
`meta.avatar.emotion_expressions` 映射到该模型的表情文件即可。

## 录制

前提：BetterGI 已启用智能体控制与 HTTP/WebSocket，令牌可从设置页复制到
`E:\BetterAGI\tools\bettergi_token.txt`。

```bash
# 1) 自动启动实时会话并录 30 分钟；Ctrl+C 也会正常收尾
python record_session.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
    --out sessions --duration 1800 --start --stop-on-exit

# 2) 画面更流畅时可直接录游戏窗口（30fps）
python record_session.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
    --capture window --window-title 原神 --start --stop-on-exit

# 3) 只记事件、不录画面（调试用）
python record_session.py --token-file E:/BetterAGI/tools/bettergi_token.txt --capture none

# 4) 同时录游戏原音（设备名以本机为准）
python record_session.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
    --capture window --window-title 原神 \
    --audio-device "立体声混音 (Realtek(R) Audio)"
```

录制器只消费 BetterGI 公开的实时数据（画面、指导、解说、动作状态），
不读写游戏内存、不抓包、不执行游戏脚本。画面默认用实时画面的 JPEG 帧
按固定帧率合成，因此视频时长与事件时间轴严格一致；`window` 模式用
ffmpeg 直录窗口，画质更顺滑。`--audio-device` 用 ffmpeg dshow 录游戏原音，
写入 `audio/game_audio.m4a`；启动失败只警告，不影响画面/事件录制。

依赖：`pip install websockets`，以及 PATH 中的 `ffmpeg`。

### 一条命令跑一场

`run_episode.py` 会先启动实时会话，等第一帧到达后拉起导演与 AutoDL 操作桥，
录制结束后统一收尾：

```bash
python run_episode.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
    --duration 1800 \
    --director-config ../lumine/director_config.json \
    --lumine-config ../lumine/bridge_config.json

# 只录画面、不跑智能体
python run_episode.py --token-file E:/BetterAGI/tools/bettergi_token.txt --no-agents
```

`--duration 0` 表示手动停止（Ctrl+C 会让录制器先写完会话文件）。
`run_episode.py` 用当前 Python 启动导演与操作桥，所以请用已安装
`websockets`、`aiohttp` 的解释器运行（例如 Amaidesu 的 `.venv`）。

画面默认不含游戏声音：实时 Hub 只传画面，窗口录制也只抓视频。需要保留
游戏原声时，可以在成片阶段用 ffmpeg 把另录的音轨混进来；本流水线的配音
始终来自 `session.jsonl` 的解说时间轴。

### 与智能体配合

录制器本身不产生解说。导演与操作模型照常运行，它们通过 BetterGI 的实时
会话收发数据：

```bash
# 导演：每轮看画面，产出 thought / guidance / commentary，写回同一时间线
python ../lumine/live_director.py --config ../lumine/director_config.json \
    --token <令牌>

# 操作：AutoDL 上的 Lumine 模型，按导演的指导操作游戏
python ../lumine/lumine_bridge.py --config ../lumine/bridge_config.json \
    --token <令牌>
```

导演的内部思考会带 `meta.internal` 写回，录制器把它记成 `thought` 事件；
直播叠加层会忽略它，只有会话文件保留。

用本目录录制时，建议关掉 `lumine_bridge.py` 自带的 `record` 选项：两边
同时用 ffmpeg 编码同一批画面只会多占 CPU，成片以录制器的
`gameplay.mp4` 为准。

## 后期成片

```bash
# 默认：飞鱼 CosyVoice3 cute（检测到飞鱼环境时自动启用）
python render_session.py sessions/20261027-153000-ab12cd

# 显式指定 cute / gentle
python render_session.py sessions/<id> --tts feiyu --feiyu-style cute
python render_session.py sessions/<id> --tts feiyu --feiyu-style gentle

# 没有模型时用 Windows 自带中文语音
python render_session.py sessions/<id> --tts sapi

# SAPI + so-vits-svc 变声（备选）
python render_session.py sessions/<id> --tts sapi \
    --svc-model E:/BetterAGI/svc/models/<模型名>

# 只做字幕和画面，不配音
python render_session.py sessions/<id> --tts none
```

飞鱼 TTS 会一次性加载模型并批量合成所有台词，避免逐句重启；失败时加
`--allow-tts-fallback` 可回退 SAPI。模型与环境见
`E:\BetterAGI\cosyvoice\README.md`。

渲染器会：

1. 按 `speech.t` 把配音放到音轨的正确位置；
2. 用 `atempo` 把每条配音拉伸到 `speech.dur` 附近（0.75–1.5 倍），实现
   后期对齐；超出范围时保留原速，并在 `render.json` 里记一条警告；
3. 生成 `subtitles.ass` 并烧录进画面；
4. 若存在 `audio/game_audio.*`，自动混入游戏原音，并在 AI 说话时用
   sidechain 压低原音；`--game-audio` 可指定别的音轨，`--no-game-audio` 关闭；
5. 输出 `final.mp4`（H.264 + AAC），同时写 `render.json` 记录每条配音的
   实际时长与处理结果。

`session.jsonl` 不会被修改。VTuber 参数已经完整保存在 `avatar` 事件里，
后续如果要做 Live2D 离线渲染，直接读这张参数表即可。

## 配音引擎

| 方式 | 命令 | 说明 |
|---|---|---|
| 飞鱼 CosyVoice3 | `--tts feiyu` | 直接生成角色音色；`cute`/`gentle` 两种声线 |
| Windows SAPI | `--tts sapi` | 系统自带，离线，音质一般 |
| SAPI + SVC | `--tts sapi --svc-model ...` | so-vits-svc 变声，备选 |

飞鱼环境在 `E:\BetterAGI\cosyvoice`（约 7GB 权重，独立 venv）；SVC 环境在
`E:\BetterAGI\svc`（模型自备）。两者都不进入 Amaidesu 或 BetterGI 的依赖。
没有可用模型时渲染器只走 SAPI，功能不受影响。

## 测试

```bash
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest tests -q
```

测试覆盖会话格式往返、录制器对假实时服务的端到端录制、后期渲染的
时间对齐与字幕输出。
