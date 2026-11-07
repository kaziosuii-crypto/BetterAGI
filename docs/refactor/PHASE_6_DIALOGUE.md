# Phase 6：Dialogue Ingestion 与 Dialogue Lock

状态：已实现（OCR 观察 + 对话锁 + 意图/解说门控）。  
日期：2026-10-28

## 目标

- 从 BetterGI 的 observe 视觉结果识别「正在对话」；
- 对话期间不发布探索意图、不解说、不跳过；
- 只使用屏幕上真实出现的文字，不推断说话人、不补全台词；
- OCR 不可靠时保守暂停，而不是猜。

## 实现

### 视觉来源

`integrations/player/player_vision.py` 的 `ScreenObserver`

- 只允许回环地址；
- 调用 `POST /api/v1/agent/observe`，要求同一截图内返回 OCR；
- 拒绝超过 15 秒的过期截图；
- 返回 `frame_id / frame_sha256 / ui_category / is_in_talk_ui / ocr`；
- 该接口会写 BetterGI 观察事件，不是零副作用；默认低频（30 秒）调用。

### 对话跟踪

`integrations/player/player_dialogue.py`

- 判定条件：`is_in_talk_ui == true` 或 `ui_category == "Talk"`，或 OCR 行非空；
- `LOCK_HOLD_MS = 4000`：最后一次看到对话后至少保持 4 秒，避免切换台词时
  短暂丢失 UI 导致误判结束；
- 只保留最近 8 行；不合并、不改写 NPC 原话；
- 不推断说话人，不猜测选项；
- `context_lines()` 明确标注「屏幕对话（OCR，可能不完整）」。

### 门控

`integrations/player/player_agent.py`

- 每轮 tick 若拿到新的视觉结果，先更新 DialogueTracker；
- 对话开始：
  - 取消旧 intent；
  - 发布显式 hold intent：
    「剧情对话中：只观察，不跳过对话、不打开菜单或地图」，
    `motor_mode=wait`；
  - 这样 Motor 不会因为 intent 为空而回落到自由操作。
- 对话持续期间：
  - SpeechPolicy 返回 `dialogue_lock`，不解说；
  - `_publish_intent()` 复用 hold，不发布新的探索目标；
  - ContextBuilder 注入最近 3 行 OCR 对话。
- 对话结束：取消 hold，恢复正常意图/解说。

### 记录

- shadow 记录写入 `dialogue_started` / `dialogue_ended` 事件；
- 对话文本只作为「玩家看到的内容」，不写成世界真相；
- 模型若在提案里引用对话，仍需通过 Phase 2 的证据门才能成为已知事实。

## 验证

- `test_player_dialogue.py`：开始/保持/结束、reset、锁期间发布门控。
- `test_player_agent.py`：Talk UI 观察触发 hold 意图、取消旧意图、记录事件。
- `test_player_speech.py`：对话锁阻止解说。
- `test_continuity.py::test_e_*`：对话/世界知识不泄漏。

```bash
python -m pytest integrations/player/tests/test_player_dialogue.py integrations/player/tests/test_player_speech.py -q
```

## 已知边界

- OCR 没有置信度字段；当前把「Talk UI + 任意文字」当作对话，可能把
  对话框外的 HUD 文本误判为对话。
- 只观察低频截图（默认 30 秒）；短对话可能在两次 observe 之间结束，
  此时既不会上锁也不会记录完整台词。
- 尚未把一段对话整理成 conversation event 写入 episodic memory；
  目前只保留最近 OCR 行与 shadow 事件。
- C# 动作执行器没有独立的「对话 UI 禁止点击」硬门；安全依赖
  bridge 的 intent 与 Motor prompt，属于软约束。

## 回滚

`DIALOGUE_LOCK=off` 或关闭 `--observe-ocr` 即可停用对话锁；
OCR 失败时系统保持无锁状态，但不会编造对话内容。
