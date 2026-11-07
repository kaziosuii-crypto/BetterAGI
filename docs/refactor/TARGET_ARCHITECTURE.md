# 目标架构（TARGET_ARCHITECTURE）

目标不是「漂亮的框架」，而是一个 **PersistentPlayerAgent**：从第一次进入
提瓦特开始，以同一个认知主体持续游玩；Lumine、BetterGI、OCR、寻路、录制器
都是它使用的工具。本文只定义目标结构，不写实现代码。

---

## 1. 第一原则

**THERE MUST BE ONE PLAYER.**

只能有一个主体同时拥有：

```text
perception
working memory
episodic memory
semantic memory
beliefs
emotions
goals / intentions
planning
decisions
commentary
relationships
long-term identity
```

任何新增模块都要回答：

> 这个模块是在帮助同一个 PersistentPlayerAgent 玩游戏，
> 还是又创造了一个独立做决定的 AI？

如果是后者，重新设计。

---

## 2. 目标组件图

```text
                    ┌─────────────────────────────────────────────┐
                    │        PersistentPlayerAgent                 │
                    │                                             │
  ┌───────────────► │  Perception  ──► Working Memory              │
  │                 │       │             │                      │
  │                 │       ▼             ▼                      │
  │                 │  Beliefs        Episodic Memory             │
  │                 │  Relationships  Semantic Memory             │
  │                 │  Emotion State  Narrative Threads          │
  │                 │       │             │                      │
  │                 │       ▼             ▼                      │
  │                 │  Goal / Intention Manager                  │
  │                 │       │                                    │
  │                 │       ▼                                    │
  │                 │  Planner (deliberative)                    │
  │                 │       │                                    │
  │                 │       ├──► Commentary / Speech Policy      │
  │                 │       └──► Motor Intent                   │
  │                 └───────────────┬─────────────────────────────┘
  │                                 │ intent + constraints
  │                                 ▼
  │                      MotorController (Lumine)
  │                                 │ 200ms action chunks
  │                                 ▼
  │                      BetterGI Live Hub / 模拟输入
  │                                 │ frames, action_state, action echo
  └─────────────────────────────────┘

  旁路：
    Session Recorder  ◄── Hub 帧/事件 ──►  session.jsonl + video
    MemoryConsolidator ◄── event log / session end
    ContextBuilder     ◄── 各状态 → Prompt
```

**只有一个认知主体**。MotorController 是运动系统；BetterGI 是手；录制器是
摄像机；OpenViking 是记忆库；DeepSeek 是 cognition 模型。

---

## 3. 职责边界

| 层 | 拥有 | 不拥有 |
|---|---|---|
| PersistentPlayerAgent | 身份、记忆、信念、情绪、关系、目标、计划、解说、动作意图 | 低层按键/鼠标轨迹 |
| MotorController（Lumine） | 把 intent 转成 200ms 动作；局部避障/镜头/战斗操作 | 叙事目标、任务解释、角色偏好、剧情选择、长期意图 |
| BetterGI | 截图、识别、模拟输入、执行动作、广播状态 | 任何认知决策 |
| OpenViking | 可检索的文本资源/记忆 | 决策、情绪、关系状态机 |
| Session Recorder | 连续视频 + 事件日志 | 决定录制什么内容 |
| Renderer / TTS | 后期配音、字幕、混流 | 改写当时 speech 的含义 |

### MotorController 的边界（硬约束）

允许：

- 避障、镜头微调、战斗中的局部连招、路径修正；
- 在 intent 的 `constraints` 内选择具体键鼠；
- 返回 `done / blocked / failed / progress`。

禁止：

- 自行决定去哪个任务/地点/角色；
- 自行选择对话选项或跳过剧情；
- 拥有长期偏好/关系/情绪；
- 修改 PlayerAgent 的目标。

---

## 4. 玩家身份与持久化目录

目标目录（建议，不要求一次到位）：

```text
E:\BetterAGI\player\
    profile\
        identity.json
        personality.json
        play_style.json
        preferences.json
        values.json
        speech_style.json
        long_term_interests.json
        self_history.json
    state\
        resume_state.json
        emotion_state.json
        belief_state.json
        relationships.json
        narrative_threads.json
        goals.json
    memory\
        episodes\
        semantic\
        autobiography\
        imports\
    sessions\
        <session_id>\
            session.jsonl
            gameplay.mp4
            ...
```

要求：

- 每个文件带 `schema_version`、`updated_at`；
- 写入原子（临时文件 + replace）；
- 允许只读加载与迁移；
- 敏感信息（令牌）不进入 player 目录。

---

## 5. 核心接口（概念）

### 5.1 PerceptionEvent

```json
{
  "event_id": "p-...",
  "t": 123456,
  "kind": "frame|dialogue|ui|combat|interaction|action_feedback",
  "source": "bettergi|capture|ocr|motor",
  "payload": {},
  "confidence": 0.0
}
```

### 5.2 MotorIntent

```json
{
  "intent_id": "i-...",
  "owner": "PersistentPlayerAgent",
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

### 5.3 MotorFeedback

```json
{
  "intent_id": "i-...",
  "status": "done|progress|blocked|failed",
  "reason": "path_blocked|target_lost|ui_anomaly|combat|timeout",
  "observation": "角色卡在岩石前，镜头无法转向",
  "t": 123456
}
```

### 5.4 AgentSpeech

```json
{
  "t": 123456,
  "text": "等等，他刚刚是不是说了深渊？",
  "thought_source": "这个 NPC 刚刚提到了深渊……我记得之前凯亚那里也提到过。",
  "emotion": {"curiosity": 0.74, "tension": 0.31},
  "intensity": 0.71,
  "pace": 1.08,
  "volume": 0.92,
  "pause_before": 0.18,
  "interruptible": true,
  "dialogue_lock": false
}
```

### 5.5 Belief

```json
{
  "proposition": "凯亚在隐瞒某件事",
  "status": "hypothesis",
  "confidence": 0.42,
  "evidence": ["他避开了问题", "他提到深渊时停顿"],
  "first_formed_at": "...",
  "last_updated_at": "...",
  "contradictions": []
}
```

### 5.6 Motor Bus 的硬保证

- 每个 intent 有唯一 `intent_id` 和版本；
- Motor 必须回 `accepted/rejected/progress/done/blocked/failed`；
- 拒绝/无效动作立即停止当前输入，不得继续按 250ms 惯性执行；
- 帧携带真实宽高、时间、UI 类别；超过 `max_frame_age_ms` 的动作不执行；
- `dialogue_lock` 期间禁止跳过键/绝对点击；
- Motor 不返回叙事目标/路线/角色偏好；
- 同一时刻只有一个主 intent；新 intent 显式取消旧 intent；
- 所有动作可追溯到 intent + 源帧。

---

## 6. 两种模型的分工

| 模型 | 用途 | 输入 | 输出 |
|---|---|---|---|
| DeepSeek `deepseek-flash` | 视觉理解 + 实时 cognition | 当前帧 + ContextBundle | thought / intent / speech / belief/relationship/emotion patch |
| DeepSeek `deepseek-v4-pro`（或同级文本模型） | deep reflection / consolidation | 文本 episode/对话/状态 | 回顾、信念更新、自传章节 |
| Lumine 7B | MotorController | 当前帧 + intent/constraints | 200ms 动作 + status |
| 规则/小模型 | fast perception | 帧差分、UI 变化、字幕 | 事件、危险、对话开始/结束 |

原则：**只有一个模型负责「决定」**；MotorController 只负责「执行」。

---

## 7. 多时间尺度

| 层 | 频率/触发 | 负责 | 是否调 LLM |
|---|---|---|---|
| Fast perception | 2–10 Hz | 危险、UI、角色出现、战斗反馈、字幕、交互提示 | 否（规则/小模型） |
| Reactive cognition | 0.5–2 s，事件触发 | NPC 出现、剧情字幕变化、战斗开始、死亡、宝箱、任务更新 | 小模型/规则，必要时轻量 LLM |
| Deliberative cognition | 3–15 s | 现在干什么、下一步、剧情理解、猜测、角色判断、计划变更 | DeepSeek flash |
| Deep reflection | 重大剧情/任务完成/长对话结束/session end | 信念/关系/情绪/线索/自传更新 | 强文本模型 |

详见 [AGENT_LOOP.md](AGENT_LOOP.md)。

---

## 8. 记忆与知识

- 短期/工作记忆：当前场景、人物、对话、意图、疑问、情绪、短期计划；
- 情景记忆：结构化 episode（不是文本 append）；
- 语义记忆：玩家已知设定（PLAYER_KNOWLEDGE）；
- 信念：明确状态/置信度/证据；
- 关系：角色维度 + 事件；
- 情绪：连续状态 + 衰减；
- 叙事线索：open/closed + 证据 + 理论；
- 自传：My Journey So Far；
- 世界数据库：WORLD_DATABASE 仅用于内部校验/OCR，不进入玩家 recall。

详见 [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md)。

---

## 9. 录制与成片

保留现有 event sourcing 与视频链路，扩展事件：

```text
perception
dialogue
memory_recall
belief_update
emotion_update
relationship_update
narrative_thread_update
intent
motor_feedback
reflection
memory_commit
```

视频从 session 开始连续录制；LLM 思考不暂停。  
统一时钟：video / event / speech / action / dialogue 都是 monotonic `t`。  
TTS 可以后处理（CosyVoice），但 speech 文本/情绪/时间来自当时认知，不允许
后期让另一个 LLM 重写反应。

---

## 10. 设计红线

1. **不得有两个决策者**。任何「小模型自己决定去哪/做什么」都违反第一原则。
2. **不得让 WORLD_DATABASE 直接进入玩家 context**。
3. **不得用固定 4 秒循环承担所有认知**。
4. **不得在后期重写 speech 含义**。
5. **不得为了架构漂亮破坏现有录制/操作链路**。
6. **不得把测试通过数量当成完成标准**。

---

## 11. 与现有资产的关系

| 现有资产 | 目标中的位置 |
|---|---|
| BetterGI 截图/输入/Live Hub | 保留为 Perception + Actuator |
| `session.jsonl` / 录制器 | 保留为 Event Log + 视频 |
| OpenViking / `memory_bridge.py` | 保留为可替换 Memory Backend |
| DeepSeek 导演 | 升级为 PersistentPlayerAgent 的 cognition 部分 |
| Lumine / 桥 / 协议 | 降级为 MotorController 与 Motor Bus |
| CosyVoice / 渲染器 | 保留为后期 Voice/Render |
| Amaidesu | 可选直播层；不进入 PlayerAgent 核心 |

目标不是推倒重来，而是**把决策权收回到一个主体**。
