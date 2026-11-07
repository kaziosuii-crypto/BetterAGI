# 认知循环（AGENT_LOOP）

> 2026-10-28 实现状态：本文描述目标循环；当前 shadow 实现是单一
> deliberative tick（由 Live 消息驱动，带最小间隔），尚未拆分
> Fast/Reactive/Deliberative/Background 四层。对话锁、意图状态机、
> speech policy 已按本文语义落地，详见 `PHASE_3`–`PHASE_7`。

目标：让 **一个 PersistentPlayerAgent** 以多个时间尺度持续运行；Lumine 只
负责把意图转成动作。循环必须允许沉默、允许慢思考、允许被事件打断。

---

## 1. 四层循环

```text
┌──────────────────────────────────────────────────────────────┐
│ Fast Perception Loop        2–10 Hz                          │
│   帧差分 / UI 变化 / 字幕 / 角色出现 / 战斗反馈 / 交互提示     │
│   不调用大模型；只产出 PerceptionEvent                        │
└──────────────────────────┬───────────────────────────────────┘
                           │ 事件
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ Reactive Cognition          0.5–2 s 或事件触发               │
│   NPC 出现 / 对话开始 / 战斗开始 / 死亡 / 宝箱 / 任务更新     │
│   规则 + 小模型；决定“是否打断当前意图/是否开口”              │
└──────────────────────────┬───────────────────────────────────┘
                           │ 需要重新判断
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ Deliberative Cognition      3–15 s 或事件触发                │
│   DeepSeek flash：现在干什么、下一步、剧情理解、猜测、        │
│   角色判断、是否改计划；产出 thought/intent/speech/memory patch│
└──────────────────────────┬───────────────────────────────────┘
                           │ 重大事件/任务完成/长对话结束/session end
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ Deep Reflection             低频                             │
│   强文本模型：episode/belief/relationship/emotion/thread 更新 │
│   session recap / autobiography / resume_state               │
└──────────────────────────────────────────────────────────────┘
```

**不要用一个固定 4 秒循环承担全部认知。**

---

## 2. 一次 Deliberative Tick 的顺序

```text
1. 取最新 PerceptionBundle
   - 当前帧
   - 最近对话（speaker/text/confidence）
   - UI/任务/交互提示
   - 最近 action feedback
2. 更新 Working Memory
   - location / characters / current activity / why / questions
3. 检查中断条件
   - 危险/对话开始/战斗/死亡/任务更新 → 可能提前触发
4. ContextBuilder 组上下文（带 token 预算）
   - Identity / Perception / Working / Intention
   - Episodic recall / Characters / Threads / Beliefs / Emotion
   - Recent dialogue / Known lore
5. DeepSeek 推理
   - thought
   - goal/intention（可空）
   - speech（可空）
   - belief/relationship/emotion patch
   - memory_patch（scene/plot/objective/...）
6. 应用状态更新
   - belief/relationship/emotion/thread
   - working/resume
7. 发出 MotorIntent（如有）
8. 发出 SpeechEvent（如有）
9. 记录事件到 session.jsonl
```

---

## 3. 动作意图生命周期

```text
CREATED ──► SENT ──► RUNNING ──► DONE
   │          │          │
   │          │          ├──► BLOCKED ──► PlayerAgent 重新规划
   │          │          └──► FAILED  ──► PlayerAgent 重新规划/放弃
   │          └──► CANCELLED（新意图/对话/危险）
   └──► EXPIRED（超时）
```

规则：

- 同一时刻只允许一个**主意图**，可有低优先级子动作；
- 新意图必须显式取消旧意图；
- MotorController 返回 `progress/done/blocked/failed` + observation；
- 连续失败 N 次（建议 3）必须上报，不允许 MotorController 无限试；
- 对话开始/战斗开始/死亡等 `interrupt_conditions` 触发时，意图暂停或取消。

---

## 4. MotorController 交互协议

### 4.1 PlayerAgent → Motor

```json
{
  "type": "intent",
  "intent_id": "i-20261027-0001",
  "goal": "跟随安柏前往目标地点",
  "reason": "她刚刚让我跟她走，我想继续主线",
  "constraints": ["不要离开任务区域", "剧情触发点停下"],
  "motor_mode": "navigate",
  "target": {"kind": "npc", "name": "Amber"},
  "interrupt_conditions": [
    "dialogue_started",
    "combat_started",
    "important_visual_event",
    "navigation_failed"
  ],
  "deadline_ms": 15000
}
```

### 4.2 Motor → PlayerAgent

```json
{
  "type": "motor_feedback",
  "intent_id": "i-20261027-0001",
  "status": "blocked",
  "reason": "path_blocked",
  "observation": "角色卡在岩石前，镜头无法转向",
  "t": 123456
}
```

MotorController **不得**返回「我决定先去开宝箱」这类叙事决策；它只能说
「当前 intent 无法完成/完成/进行中」。

### 4.3 传输保证

- intent/guidance 带 `version`/`t`；旧帧内联 guidance 不得覆盖新 guidance；
- Motor 必须确认 `accepted` 或 `rejected`；发送成功不等于执行成功；
- BetterGI 的拒绝/错误必须转成 MotorFeedback 回到 PlayerAgent；
- 帧带真实宽高与 `captured_at`；超过 `max_frame_age_ms` 的动作丢弃；
- 无效动作立即 `stop_actions`，不依赖 250ms idle release；
- live 会话重启必须重置 seq/pump/guidance，不能把旧场景指导带进新场景。

---

## 5. Speech Policy

### 5.1 什么情况下说话

- interesting thought（值得分享的观察/推理）；
- emotional reaction（真实情绪强度超过阈值）；
- new discovery（宝箱/地点/角色/线索）；
- prediction（对下一步/身份的猜测）；
- confusion（看不懂/卡住）；
- character reaction（对特定角色的态度）；
- failure / major success；
- 观众互动（可选，不能压过剧情）。

### 5.2 什么情况下沉默

- 普通移动/跑图；
- NPC 对话进行中（dialogue_lock）；
- 刚刚说过且没有新信息；
- 情绪低落/沉重剧情的余韵期；
- 正在专注解谜/战斗；
- 没有值得说的内容。

### 5.3 约束

- 最短发言间隔（建议 2.5–4 秒，剧情除外）；
- 同一话题冷却；
- 不覆盖 NPC 语音/字幕；
- `interruptible` 标记：重要剧情说话不可被普通反应打断；
- speech 带 `emotion/intensity/pace/volume/pause_before`，供 TTS 表现。

---

## 6. Dialogue Lock

触发：

- 字幕出现/对话 UI 出现/说话人识别；
- 对话选项出现；
- 剧情过场开始。

行为：

- PlayerAgent 决定「继续/选择/等待」；
- MotorController 在 lock 期间**禁止**发送会跳过对话的键（Space/点击/F）；
- 需要推进时，由 PlayerAgent 明确发 `motor_mode=dialogue_advance`；
- 对话结束后解除 lock，并生成 conversation event。

没有 dialogue lock，Lumine 可能为了“继续”疯狂点击，导致 Agent 不知道剧情。

---

## 7. 视觉/对话感知

### 7.1 PerceptionBundle

```json
{
  "frame": {"jpeg": "...", "width": 1280, "height": 720, "seq": 1},
  "ui": {"category": "dialogue|combat|menu|exploration", "confidence": 0.8},
  "dialogue": [
    {"speaker": "Amber", "text": "...", "confidence": 0.9, "t": 123456}
  ],
  "interaction_hints": ["F 对话", "宝箱"],
  "danger": {"level": "low|medium|high", "kind": "combat|fall|environment"},
  "action_feedback": {"intent_id": "...", "status": "blocked", "reason": "..."}
}
```

### 7.2 来源

- BetterGI 截图/识别/OCR/UI category；
- 视觉模型（DeepSeek flash / Lumine 的 vision tower 可复用）；
- 游戏字幕（如果能稳定读取）；
- MotorController 的 feedback。

### 7.3 原则

- 低置信度对话标 `unknown`，不要编造；
- 连续对话合并成 conversation event，长对话结束后再做 narrative
  interpretation；
- 不把世界数据库真相当作对话内容。

---

## 8. 记录钩子

每个循环阶段都可写 `session.jsonl`：

```text
perception          每帧/关键事件
dialogue            说话人/文本/置信度
memory_recall       本轮召回了哪些记忆（id/score）
belief_update       proposition/old→new/status/confidence
emotion_update      old→new/原因
relationship_update character/维度变化/原因
narrative_thread_update thread/status/evidence
intent              目标/理由/约束/目标对象
motor_feedback      status/reason/observation
reflection          深思结论
memory_commit       episode/belief/relationship/emotion/thread 落盘
speech              text/emotion/intensity/pace/volume/pause/interruptible
```

视频从 session 开始连续录制；这些事件不要求出现在成片中，但必须保留用于
调试、复盘、记忆重建和剪辑。

---

## 9. 延迟与并发

| 任务 | 预算 | 说明 |
|---|---|---|
| Fast perception | 50–200 ms | 规则/小模型，不能阻塞 |
| Reactive | 0.5–2 s | 事件触发，可打断 deliberative |
| DeepSeek deliberative | 3–15 s | 允许慢；动作由上一个 intent 维持 |
| Motor action | 200 ms/块 | 2.5Hz 实测，需动作填缝/保持 |
| Deep reflection | 10–60 s | 后台，不阻塞游玩 |
| Consolidation | 后台 | session end/每小时 |

并发规则：

- Deliberative 运行中，MotorController 继续执行当前 intent；
- 新 perception 事件可取消/替换 intent；
- Deep reflection 与 consolidation 不应阻塞动作；
- 所有状态写入原子化，避免并发覆盖。

---

## 10. 失败与降级

| 故障 | 行为 |
|---|---|
| DeepSeek 超时/限流 | 保留当前 intent；重试；必要时用规则 fallback |
| Motor blocked | 上报 PlayerAgent；重新规划；连续 3 次失败则停下 |
| 对话感知失败 | 标 unknown；不编造；dialogue_lock 仍按 UI 保守处理 |
| OpenViking 不可用 | 只用 working memory；记录 warning |
| 视频录制中断 | 事件继续写；标记 session 不完整；恢复后新文件 |
| 状态写入失败 | 保留内存副本；下一轮重试；不丢整个 session |
| 世界库/玩家库混淆 | 硬过滤；拒绝注入 world_only |

---

## 11. 完成判据（行为层）

一次 30–60 分钟 session 应至少出现：

- 自然沉默（不是每 4 秒一句）；
- 即时反应；
- 剧情推理/猜测；
- 引用旧经历；
- 角色偏好；
- 情绪延续；
- 主动计划与计划改变；
- 操作失败后的真实反应；
- 对 NPC 对白的回应；
- 不出现数据库式复述/全知视角/剧透/突然失忆/突然换人格。
