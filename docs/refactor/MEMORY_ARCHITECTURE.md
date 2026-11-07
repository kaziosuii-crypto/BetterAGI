# 记忆架构（MEMORY_ARCHITECTURE）

> 2026-10-28 实现状态：七层中的 Working / Episodic / Semantic / Belief /
> Relationship / Emotion / Thread 与 consolidation、recap、检索均已落地；
> 旧的 OpenViking 共享设定仍不直接进入玩家上下文（无法证明玩家可见）。
> 细节见 `PHASE_2`、`PHASE_4`、`PHASE_5`。

记忆的目标不是「把更多文本塞进 RAG」，而是让同一个玩家在几十/几百小时后
仍然拥有连续的**经历、信念、关系、情绪与未完成的事**。

---

## 1. 七层记忆

```text
Working Memory      当前几十秒～几分钟
Episodic Memory     我经历过什么（结构化事件）
Semantic Memory     玩家已知的设定/事实（PLAYER_KNOWLEDGE）
Belief Store        我相信/怀疑/证伪了什么
Relationship Store  我对每个角色的态度与印象
Emotion State       当前情绪向量与衰减
Narrative Threads   未解决的线索/伏笔/理论
Autobiographical    我的旅程（章节/地区/角色/主题）
```

另设 **WORLD_DATABASE**（世界真相/攻略），只供系统内部校验，不直接进入
玩家 recall。

---

## 2. Working Memory

保存当前几十秒到几分钟：

```json
{
  "location": "",
  "characters": [],
  "dialogue": [],
  "recent_events": [],
  "current_activity": "",
  "why": "",
  "open_questions": [],
  "emotion": {},
  "short_plan": [],
  "last_motor_feedback": {}
}
```

特点：

- 快速变化，不长期保存；
- 每轮由 Perception + 最近事件更新；
- 是 ContextBuilder 的必选块；
- 不写入长期记忆，除非被 consolidation 选中。

---

## 3. Episodic Memory

每个重要事件一条，**必须保存 AI 当时的认知**，不能只存剧情事实。

```json
{
  "event_id": "ep-...",
  "time": "2026-10-27T20:12:33+08:00",
  "game_time_range": [123456, 128900],
  "location": "须弥城",
  "characters": ["Amber", "Paimon"],
  "what_happened": "安柏带我们见了琴，提到了深渊教团。",
  "what_i_knew_before": "只知道蒙德有骑士团。",
  "what_i_learned": "琴是代理团长；深渊教团在附近活动。",
  "what_i_thought": "安柏似乎很信任琴；深渊这个词之前凯亚也提过。",
  "what_i_felt": {"curiosity": 0.72, "tension": 0.31},
  "my_reaction": "决定先跟安柏走，但记住要问凯亚。",
  "choices_i_made": ["跟随安柏", "没有立刻追问深渊"],
  "unresolved_questions": ["凯亚和深渊教团是什么关系？"],
  "importance": 0.64,
  "emotional_intensity": 0.58,
  "related_memories": ["ep-..."],
  "source_session": "s-...",
  "visibility": "player_knowledge"
}
```

规则：

- `what_i_thought` / `what_i_felt` / `what_i_knew_before` 是必填；
- `visibility` 默认 `player_knowledge`；
- 未经历的事件不得写入玩家 episodic；
- 重要度由事件类型 + 情绪 + 角色 + 线索 + 玩家关注综合评分。

---

## 4. Semantic Memory（PLAYER_KNOWLEDGE）

只包含玩家**已经通过游玩解锁**的设定：

```json
{
  "fact_id": "k-...",
  "proposition": "须弥是草神的国度",
  "source": "ep-... / dialogue / ui",
  "learned_at": "...",
  "confidence": 0.9,
  "spoiler_risk": "low",
  "tags": ["region", "archon"]
}
```

**知识解锁**：

```json
{
  "unlock_id": "u-...",
  "proposition": "琴是代理团长",
  "unlocked_by": "dialogue:...",
  "t": 123456
}
```

`WORLD_DATABASE` 的条目默认 `visibility="world_only"`；只有通过：
- 对话/字幕；
- UI/任务文本；
- 玩家明确看到/听到的画面；
- 玩家自己的推理（belief，不是 fact）

才能进入 PLAYER_KNOWLEDGE。禁止直接把世界库全文导入 recall。

---

## 5. Belief Store

```json
{
  "belief_id": "b-...",
  "proposition": "凯亚在隐瞒某件事",
  "status": "hypothesis",
  "confidence": 0.42,
  "evidence": ["他避开了问题", "提到深渊时停顿"],
  "first_formed_at": "...",
  "last_updated_at": "...",
  "contradictions": [],
  "related_characters": ["Kaeya"],
  "related_threads": ["th-..."]
}
```

`status` 枚举：

```text
ground_truth   系统内部真相（不进入玩家 context）
known_fact     玩家已确认
belief         玩家相信但未证实
hypothesis     猜测
suspicion      怀疑
unknown        不知道
disproven      已被证伪
```

关键：

- `ground_truth` 只供调试/验证，**永远不能进入 PlayerAgent prompt**；
- 一个 belief 可以长期存在，置信度逐步变化；
- 证伪时保留 `disproven` + 证据，不要删除；这是「我之前猜错了」的来源；
- 允许矛盾，矛盾本身是有价值的认知历史。

---

## 6. Relationship Store

```json
{
  "character": "Amber",
  "familiarity": 0.63,
  "trust": 0.77,
  "affection": 0.71,
  "suspicion": 0.08,
  "respect": 0.54,
  "concern": 0.31,
  "memorable_events": ["ep-..."],
  "impressions": ["认真但有点莽撞", "第一次见面就帮我"],
  "unresolved_questions": [],
  "relationship_summary": "你逐渐喜欢安柏；她认真又有点莽撞，第一次见面就帮过你。"
}
```

规则：

- 数值只用于内部排序/衰减，不直接显示给 LLM；
- 注入 prompt 的是自然语言 `relationship_summary` + 最近 1–2 个事件；
- 每次事件的变化有上限（例如 ±0.08），禁止一次剧情从 0.2 跳到 0.9；
- 负面事件可以同时提高 suspicion/降低 trust，保留矛盾。

---

## 7. Emotion State

```json
{
  "joy": 0.20,
  "sadness": 0.63,
  "fear": 0.11,
  "anger": 0.05,
  "curiosity": 0.72,
  "confusion": 0.44,
  "excitement": 0.31,
  "nostalgia": 0.08,
  "tension": 0.37,
  "updated_at": "...",
  "decay_per_minute": 0.04
}
```

来源：

- 剧情/对话；
- 角色关系；
- 战斗/失败/重复失败；
- 发现/宝箱/任务完成；
- 伏笔/线索；
- 环境/音乐；
- 长期期待。

规则：

- 每轮按时间衰减；
- 重大事件强化（有上限）；
- 影响语言、动作、注意力、下一轮思考；
- 重大悲伤后 30 秒出现宝箱，不应瞬间兴奋；应保留 sadness 的残余；
- prompt 中只给自然语言摘要（「你现在有些低落，还没完全缓过来」），
  不直接列 0.63。

---

## 8. Narrative Threads

```json
{
  "thread_id": "th-...",
  "thread": "深渊教团到底在计划什么",
  "status": "open",
  "importance": 0.82,
  "first_seen": "...",
  "last_referenced": "...",
  "related_characters": ["Kaeya", "Amber"],
  "evidence": ["ep-...", "dialogue:..."],
  "current_theory": "凯亚可能知道更多，但不愿说。",
  "unresolved_questions": ["深渊教团在蒙德做了什么？"]
}
```

规则：

- 每次出现相关线索，`last_referenced` 更新并激活；
- 检索时给激活线程更高权重；
- 线程可以 `open / dormant / resolved / abandoned`；
- 旧线索重新出现时，PlayerAgent 应能说「这是不是之前提过的东西？」。

---

## 9. Autobiographical Memory

定期生成 `My Journey So Far`：

```text
我最初来到提瓦特是为了寻找……
第一次进入蒙德时……
我逐渐开始信任……
我曾经怀疑……
后来才发现……
目前最让我在意的是……
```

按 chapter / region / character / theme 分节；来源必须引用 episode，
不允许编造未发生的事。它是「我的旅程」，不是剧情百科。

---

## 10. 检索：多因子而不是 top-k

候选生成：

1. 当前场景/对话/意图的语义检索（OpenViking）；
2. 当前角色/地点/任务过滤；
3. 激活的 narrative threads；
4. 未完成 intentions；
5. 最近高情绪事件；
6. 高 importance 的旧 episode。

评分建议（可实验调整）：

```text
score =
  semantic        * 0.30
+ importance      * 0.20
+ emotional       * 0.15
+ character       * 0.10
+ narrative_thread* 0.15
+ recency         * 0.10
```

补充约束：

- 同一事件/角色去重；
- 每条记忆带来源/时间/置信度；
- 先取 top-20 候选，再按 token budget 选 4–8 条；
- 不把几十条无关记忆塞进上下文；
- 对 `ground_truth` / `world_only` 做硬过滤。

---

## 11. Consolidation

### 实时（每轮/每几秒）

```text
raw event → event log → working memory
```

### 阶段性（每 30–120 秒或事件触发）

```text
important event extraction
→ episodic memory
→ belief update
→ relationship update
→ emotion update
→ narrative thread update
```

### Session 结束

```text
本期发生什么
哪些事情重要
认识了谁
关系怎么变化
产生了什么猜测
哪些猜测被推翻
有什么未解决问题
目前最期待什么
当前情绪是什么
下一次想做什么
```

输出：

- 新 episode / belief / relationship / thread 更新；
- `resume_state.json`；
- 一段 session recap（给下一场首轮注入）；
- `My Journey` 增量。

**不要把整场 transcript 直接喂给下一场。**

---

## 12. Session Resume

```json
{
  "schema_version": 1,
  "player_id": "p-...",
  "last_session_id": "s-...",
  "last_location": "须弥城",
  "current_quest": "第三章·第一幕",
  "current_intention": "先调查深渊教团那条线",
  "unfinished_actions": [],
  "active_story_threads": ["th-..."],
  "recent_important_memories": ["ep-..."],
  "current_emotion": {"sadness": 0.4, "curiosity": 0.6},
  "recent_character_relationships": ["Amber", "Kaeya"],
  "current_beliefs": ["b-..."],
  "things_i_want_to_check_next": ["任务列表里的深渊线索"],
  "last_internal_thought": "今天先到这吧，下次想先看看那个任务。"
}
```

启动时：

```text
Persistent Self
+ Session Resume State
+ Relevant Memories
+ Current Open Threads
+ Current Relationships
+ Current Beliefs
+ Current Emotion
```

生成自然开场，例如「上次做到这里……我本来想先调查深渊那条线」，
禁止数据库口吻（「根据记忆数据库，上一场完成了……」）。

---

## 13. Importance / Decay / Forgetting

```text
0.0–0.2   可遗忘（软删除/降权）
0.2–0.5   普通情节
0.5–0.75  值得长期保存
0.75–1.0  核心人生事件
```

影响因素：

- 剧情重要度；
- 情绪强度；
- 角色关系；
- 反转/伏笔；
- 玩家主动关注；
- 重复出现。

真人不会记得 24 小时前第 38 只史莱姆。允许摘要化/降权，不要立即物理删除；
保留 `source_event` 可回溯。

---

## 14. 存储映射

| 数据 | 存储 | 说明 |
|---|---|---|
| Working memory | 进程内 + 定期 `state/working.json` | 快速变化 |
| Episodic | `memory/episodes/*.json` + OpenViking | 结构化 + 可检索 |
| Semantic (player) | `memory/semantic/*.json` + OpenViking | 只含已解锁 |
| World database | `world/` + 独立 OpenViking URI | 不进入玩家 recall |
| Beliefs | `state/belief_state.json` | 小，原子写 |
| Relationships | `state/relationships.json` | 小，原子写 |
| Emotion | `state/emotion_state.json` | 每轮更新 |
| Threads | `state/narrative_threads.json` | 小 |
| Resume | `state/resume_state.json` | 每 30 秒 |
| Autobiography | `memory/autobiography/*.md` | 章节 |
| Event log | `sessions/<id>/session.jsonl` | 事实源 |
| Profile | `player/profile/*.json` | 只读/低频写 |

OpenViking 是可替换后端；结构化状态不依赖向量库也能恢复。

---

## 15. ContextBuilder 的预算

建议每轮预算（可按模型调整）：

```text
Identity / Profile        300–500 tokens
Current Perception        200–400
Working Memory            400–700
Current Intention         100–200
Episodic Recall           600–1000
Characters                200–400
Narrative Threads         200–400
Beliefs                   200–400
Emotion                   100–200
Recent Dialogue           300–600
Known Lore                300–600
Output reserve            800–1200
```

原则：

- 每块独立预算，超限时摘要化，而不是整体截断；
- 重要块（当前意图、对话）优先；
- 记忆条目带来源/时间/置信度；
- 未解锁知识硬过滤。

---

## 16. 迁移要求

1. 现有 OpenViking 文本日志保留为 `legacy_episode`，逐步转成结构化 episode；
2. 现有 `memory.json` 的 `scene/plot/objective` 迁入 resume/working；
3. 旧 `session.jsonl` 保持可读；新事件类型向后兼容；
4. 所有 schema 带 version，迁移幂等；
5. 不删除旧数据，先双写再切换。

### 16.1 迁移前必须修的现有断点

- `GET /memory` 实际 DTO 是 `{sessionId,updatedAtUtc,values}`，当前导演按
  `{"memory":...}` 读取，导致重启后 `scene/plot/objective` 读不到
  （见 `CURRENT_ARCHITECTURE.md` 3.14-1）；
- 同 session 的 OpenViking 文件用 `replace` 覆盖，重启首 tick 会冲掉上一场
  摘要（3.14-6）；
- `_short_events/_episode_log/_recent_commentary` 只在内存，必须先持久化
  working/resume，才能谈跨场恢复。

不先修这些，新的 episodic/belief 结构会继承坏状态。
