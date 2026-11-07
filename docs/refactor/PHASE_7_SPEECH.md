# Phase 7：Speech Policy 与同源 Commentary

状态：策略与记录格式已实现；把玩家自己的解说送入实况链路仍属 Active 模式，未接线。  
日期：2026-10-28

## 目标

- 解说只来自玩家当前认知（意图、记忆、情绪、对话锁）；
- 普通移动默认沉默，不按画面独立补台词；
- 对话期间不覆盖 NPC；
- 情绪低落时不会因为一个宝箱瞬间变成狂喜；
- 每条 speech 带语速/音量/停顿等 prosody，供后期 TTS 对齐。

## 实现

### 决策

`integrations/player/player_speech_policy.py`

输入：模型提案中的 `speech`、对话锁、沉默倾向、情绪视图、当前时间。

规则：

- 空文本 / 模型明确沉默 → 不说；
- 对话锁 → `dialogue_lock`；
- 普通内容最小间隔 6 秒；高强度或强烈情绪可越过冷却；
- `silence_bias=high` 时，低强度内容默认不说；
- 强度 ≥ 0.75 → `fast`，≤ 0.3 → `slow`，否则 `normal`；
- 难过 ≥ 0.5 时，`excited/happy` 且强度 < 0.95 会被压回正常语速，
  并标记 `mood_damped`；
- 输出：`speak/text/emotion/intensity/pace/volume/pause_before_ms/
  interruptible/mood_damped`。

`player_agent.tick()` 在模型返回后调用策略，决定写入 shadow 的
`speech_policy`；即使不说也记录原因，便于回看「为什么沉默」。

### 记录格式

`integrations/session/session_format.py`

`emit_speech()` 新增并校验：

- `intensity`（0–1）
- `pace`（slow/normal/fast）
- `volume`（soft/normal/loud）
- `pause_before_ms`
- `interruptible`
- `thought_source`（如 `dialogue` / `memory` / `reaction`）

未知值会被归一化或拒绝；旧会话文件仍可读取。

### 渲染

`integrations/session/render_session.py`

- 读取 prosody，生成对应 TTS 请求参数；
- 在语音前插入停顿；
- 按 volume 调整增益；
- report 中记录实际使用的 prosody。

### 同源原则

- 解说文本来自同一轮提案，与 `intention`、动作提示、情绪一致；
- 不再让独立导演模型仅凭画面写 commentary 作为最终解说；
- 对话锁同时作用于 speech 与 intent。

## 验证

- `test_player_speech.py`：冷却、沉默倾向、对话锁、情绪余韵、prosody 输出。
- `test_session_format.py`：prosody 字段 roundtrip。
- `test_render_session.py`：停顿、音量、语速映射与游戏原音混音。
- `test_player_agent.py`：策略结果写入 shadow、不发送动作。

```bash
python -m pytest integrations/player/tests/test_player_speech.py integrations/session/tests -q
```

## 已知边界

- 当前实况录制链路仍消费 BetterGI 的 `commentary`（旧导演）；
  PlayerAgent 的 speech 决定只写入 shadow 记录。要真正替换生产者，
  需要 Active 模式把决定送回 Bridge/C#，这一步尚未实现。
- 冷却按进程内时间计算，重启后重置；跨场沉默倾向只由 profile 决定。
- `emotion` 标签由模型给出，策略只做一致性/余韵约束，不重新分类。

## 回滚

`SPEECH_POLICY=legacy` 或关闭策略注入，继续使用旧 director commentary。
