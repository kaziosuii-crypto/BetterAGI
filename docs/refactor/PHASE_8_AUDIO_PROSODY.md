# Phase 8：视频/音频/TTS Prosody

状态：录制游戏原音、混音、ducking、prosody 映射已实现；飞鱼 TTS 仍为后期接线。  
日期：2026-10-28

## 目标

- 录制时保存游戏原音（BGM/环境/NPC 语音）；
- 渲染时把 AI 语音与游戏原音混在一起；
- AI 说话时自动压低游戏原音，但重要 NPC 对话不被完全盖住；
- speech 的情绪/强度映射为语速、音量、停顿；
- 保留当时说话的含义，不重写台词。

## 实现

### 录制

`integrations/session/record_session.py`

- 新增 `AudioSink`：用 ffmpeg `dshow` 录制指定音频设备
  （如「立体声混音」或 VB-Audio 虚拟声卡）。
- CLI：`--audio-device "设备名"`；留空不录游戏原音。
- 输出 `audio/game_audio.m4a`（AAC 192k、48kHz 立体声）。
- 启动失败只警告，不影响事件与画面录制。
- 会话文件写入 `audio` 事件与 `counts.game_audio/game_audio_ms`。
- 结束用 `q` 优雅停止 ffmpeg；超时则 terminate/kill。

### 混合

`integrations/session/render_session.py`

- 自动查找 `audio/game_audio.{m4a,wav,mp3,flac,ogg}`，
  或显式 `--game-audio`；`--no-game-audio` 关闭。
- 语音轨按事件时间 `adelay` 对齐，应用 `volume`（soft 0.8 / normal 1.0 /
  loud 1.15）。
- 有语音时：
  - `sidechaincompress` 以语音为侧链压低游戏原音
    （threshold 0.05、ratio 8、attack 20ms、release 300ms）；
  - 再与语音 `amix`，总时长裁剪到会话长度。
- 无语音时保留游戏原音单轨。
- report 记录 `game_audio` 与每条 speech 的 prosody。

### Prosody

- `session_format.emit_speech()` 接受并保存
  `intensity/pace/volume/pause_before_ms/interruptible/thought_source`。
- 渲染读取：
  - `pause_before_ms` 在语音前插入静音（上限 2000ms）；
  - `pace` 参与 TTS/atempo 目标时长；飞鱼批量请求里每句带独立
    `speed`（slow 0.92、fast 1.08），`feiyu_tts.py` 按句覆盖全局语速；
  - `volume` 映射为增益。
- 仍以「事件 dur + atempo 对齐」保证口型同步。

## 验证

- `test_session_format.py::test_speech_prosody_fields_roundtrip`。
- `test_render_session.py`：prosody 读取、停顿、音量、游戏原音检测与 ducking 滤镜。
- `test_record_session.py`（如存在）覆盖音频设备缺省、启动失败降级。
- 手工验证需要真实声卡设备；本机尚未安装游戏，未做端到端听感验收。

```bash
python -m pytest integrations/session/tests -q
```

## 已知边界

- Windows `dshow` 设备名必须与系统一致；不同机器默认设备不同，
  因此不设默认值，必须显式传入。
- 仅支持系统/虚拟声卡的「回环」录音；没有实现 WASAPI loopback 的专用路径。
- 游戏原音从录制开始计时；若声卡启动比画面晚约 1 秒，混音可能有轻微偏移，
  当前未做自动对齐。
- 飞鱼的音色风格（cute/gentle）仍是整批统一，不能逐句切换；
  情绪强度目前只影响语速/停顿/音量，不影响音色。
- 飞鱼 TTS 的真实音质与听感尚未在本机验收（模型环境未启动）。

## 回滚

渲染加 `--no-game-audio` 即回到纯 AI 语音；录制不传 `--audio-device`
即不产生游戏原音文件。
