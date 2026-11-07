# 迁移计划（MIGRATION_PLAN）

原则：

1. 先审计，后实现；本文件不等于已实现。
2. **不破坏现有录像/操作链路**：BetterGI 截图/输入、`session.jsonl`、
   OpenViking、CosyVoice、现有测试必须继续可用。
3. 每一步都可灰度/回滚；先 shadow 双跑，再切换。
4. 以「同一个玩家」为验收，不以测试数量为验收。

---

## 阶段总览

| Phase | 目标 | 是否改动作链路 | 主要风险 |
|---|---|---|---|
| 0 | 审计与文档 | 否 | 无 |
| 1 | Persistent Player 骨架 + profile + 状态文件 | 否（shadow） | schema 设计过度 |
| 2 | ContextBuilder + 结构化 memory | 否 | token/成本 |
| 3 | Intent 协议 + MotorController 降级 | **是** | 操作质量/兼容 |
| 4 | Episodic + consolidation + resume | 否 | 记忆漂移 |
| 5 | Belief / Relationship / Emotion / Thread | 部分（影响 speech/动作） | 状态难校准 |
| 6 | Dialogue ingestion + dialogue_lock | 是 | OCR/锁定误判 |
| 7 | Speech policy / commentary 同源 | 是（只影响 speech） | 过度沉默 |
| 8 | 视频/音频/TTS prosody | 否 | 音轨同步 |
| 9 | 连续性测试与实况验收 | 否 | 长跑成本 |

依赖：Phase 1 → 2 → 4 → 5 → 6/7；Phase 3 可与 2 并行；Phase 8 可后置。

> 2026-10-28 状态：Phase 0.5–9 的代码与模块级测试已完成，
> 默认仍为 shadow，不改变现有录像/操作链路。真实游戏、AutoDL 模型、
> 飞鱼 TTS 与盲测尚未执行；各阶段细节见 `PHASE_*.md`。

---

## Phase 0：审计（当前）

产出：

- `CURRENT_ARCHITECTURE.md`
- `GAP_ANALYSIS.md`
- `TARGET_ARCHITECTURE.md`
- `MEMORY_ARCHITECTURE.md`
- `AGENT_LOOP.md`
- `MIGRATION_PLAN.md`
- `TEST_PLAN.md`

验收：

- 审计结论明确标记「双认知主体」为 P0；
- 列出当前状态持久化矩阵；
- 不修改运行代码。

---

## Phase 0.5：先修当前断点（不新增认知功能）

**状态：第一批 P0 已完成（2026-10-27），见 [PHASE_0_5_FIXES.md](PHASE_0_5_FIXES.md)。**

已完成：memory 响应形状、guidance 发送确认/重试、live 会话重启/动作序号、
guidance 竞态、OpenViking 按 run 写入/延迟重连、session id 对齐、lore 示例
去剧透。尚未完成：action ack/motor_feedback、dialogue/UI 门控、显式
persona/player_profile、session append/resume、frame/audio 事件、world/player
硬隔离。它们继续留在 Phase 1+。

必须修：

1. **memory API 形状**：`GET /memory` 真实 DTO 是
   `{sessionId, updatedAtUtc, values}`，导演按 `{"memory":...}` 读取；
   统一读取 `values`，补真实 DTO fixture（G31）。
2. **guidance 发送确认**：`_send` 失败/`_ws is None` 时不得把
   `_last_sent_guidance` 标成已发；桥回 `guidance_received` 或至少记录
   发送结果（G32）。
3. **guidance 来源/新鲜度**：区分导演/外部 guidance；帧龄超过阈值不生成
   新指导；处理 `live.stopped`（G33）。
4. **OpenViking 覆盖**：同 session 重启不得 `replace` 掉上一场摘要；改为
   每场唯一 URI 或 append（G35）。
5. **session 身份/续写**：统一 `player_id/session_id`，meta 记录
   parent/previous；writer 支持 append/resume（G36）。
6. **lore 示例去剧透**：替换 `genshin_basics.md` 的后期剧情事实；导入
   默认 `world_only`，玩家知识单独导入（G37）。
7. **persona 显式化**：创建 `persona.json`，config 显式引用；缺失时明确
   报错或使用带版本的默认人格（G34）。
8. **测试**：上述每一项先写失败测试；修复后旧 162/15/234 不回归。
9. **live 会话重启**：桥处理 `live.started/live.stopped`，重置
   seq/pump/guidance；BetterGI 会话边界重置客户端 `LastActionSeq`
   （G41）。
10. **guidance 竞态**：给 guidance 加版本/时间戳；旧帧内联 guidance 不得
    覆盖新 guidance；桥单写者（G42）。
11. **动作执行反馈**：桥把 BetterGI 的 `error`/拒绝转成 motor_feedback；
    无效动作立即 `stop_actions`（G43）。
12. **UI/对话门控**：至少保留 `uiCategory`/runtime 元数据；没有检测时
    保守暂停；为 Phase 6 的 dialogue_lock 留接口（G44/G48）。
13. **帧龄/坐标**：帧携带真实宽高与时间；过期动作丢弃；绝对坐标按源帧
    映射（G46/G47）。
14. **动作确定性**：Motor 默认低温度/固定 seed；可配置（G45）。

验收：

- 真实 C# memory DTO 形状的集成测试通过；
- guidance 发送失败可见且不误标；
- 重启导演后 `scene/plot/objective` 能读回；
- 同 session 重启不覆盖旧 OpenViking 摘要；
- 示例 lore 不含后期剧透；
- `pytest`/`dotnet test` 全绿。

回滚：每项独立开关/小提交；不改变现有消息格式的字段语义。

---

## Phase 1：Persistent Player 骨架

**状态：已实现（2026-10-27），shadow 只读，不接管动作。**
详见 [PHASE_1_PLAYER_SKELETON.md](PHASE_1_PLAYER_SKELETON.md)。

实际落地与本文原计划的对应：

- 代码放在 `BetterGI/integrations/player/`（不是抽象的 `agent/`）；
  `player_agent.py` / `player_context.py` / `player_loop.py` 均在包内；
- 数据放在 `E:\BetterAGI\player\`，默认由 `PLAYER_HOME` 决定；
- profile 先采用单文件 `profile/profile.json`（含文档列出的八个逻辑段），
  暂不拆成八个文件；
- state 先只落地 `resume_state.json`，其余状态文件留给 Phase 4/5；
- shadow 记录写入独立 `state/shadow/<session>.jsonl`，不写 `session.jsonl`；
- 新入口是独立进程 `run_shadow.py`；`run_episode.py` 新增可选
  `--player-shadow-config`，不传时旧链路完全不变。

目标：

- 新建 profile schema 与运行时状态文件；
- 新建 `PersistentPlayerAgent` 入口（先 shadow，不接管动作）；
- 保留 director 作为 commentary-only fallback；
- 定义 `schema_version` 与原子写。

文件（实际）：

```text
integrations/player/player_profile.py
integrations/player/player_state.py
integrations/player/player_agent.py
integrations/player/player_context.py   （Phase 1 最小版，Phase 2 扩展）
integrations/player/player_loop.py
integrations/player/run_shadow.py
integrations/player/profiles/lumi.json
E:\BetterAGI\player\profile\profile.json   （首次运行播种）
E:\BetterAGI\player\state\resume_state.json
E:\BetterAGI\player\state\shadow\<session>.jsonl
```

验收（已完成）：

- 启动时能加载 profile + 上次 resume（如果存在）；
- 旧 `live_director.py` 仍可独立运行；
- shadow 模式下新 agent 只记录「如果是我会怎么想/怎么做」，不发送动作；
- 现有 169/20/234 测试不回归（Phase 1/2 新增 166 项后 Python 合计 350）。

回滚：关闭新入口，恢复旧 director。

---

## Phase 2：ContextBuilder 与结构化记忆

**状态：shadow 骨架已实施，完整验收未完成。** 参见
[PHASE_2_CONTEXT_MEMORY.md](PHASE_2_CONTEXT_MEMORY.md)：本地 working/episode、
语义存储与预算化 ContextBuilder 已接入 shadow；模型事实暂不自动解锁，
OpenViking 玩家召回与旧导演 `memory_patch` 双写尚未实现。

目标：

- 把 prompt 拼接改成 `ContextBuilder`，每块预算；
- 新增 working/episodic/semantic 的基本 schema；
- OpenViking 只作为检索后端，结构化状态本地 JSON/SQLite；
- 先双写：旧 `memory_patch` + 新结构化状态。

文件：

```text
agent/context_builder.py
memory/working.py
memory/episodes.py
memory/semantic.py
memory/retrieval.py
memory/consolidator.py（骨架）
```

验收：

- 同一轮输入可以生成稳定 ContextBundle；
- 结构化 episode 至少覆盖 location/characters/what_happened/thought/felt；
- 旧 `session.jsonl` 仍可读；
- 检索结果带来源/时间/分数。

回滚：`CONTEXT_BUILDER=legacy` 使用旧拼接。

---

## Phase 3：Intent 协议与 MotorController

目标：

- 新增 `intent` / `motor_feedback` 消息；
- PlayerAgent 拥有目标/意图；Lumine 只执行 intent；
- 改 Motor prompt，禁止叙事决策；
- 桥做 v1↔v2 转换；保留旧协议 fallback。

文件：

```text
lumine/lumine_protocol.py
lumine/lumine_bridge.py
lumine/autodl_server.py
BetterGI/.../LiveSessionModels.cs（可选：回显 intent_id）
agent/intent.py
```

验收：

- 每个动作都能追溯到 `intent_id`；
- Motor 不能自行改目标；guidance 只作为 intent 的约束/目标描述；
- blocked 反馈能到达 PlayerAgent；
- v1 客户端仍能工作；
- 录像链路不中断。

回滚：`MOTOR_PROTOCOL=v1`。

风险：Lumine 2.5Hz；intent 太长会降低动作质量；需要做 intent 摘要。

---

## Phase 4：Episodic / Consolidation / Resume

目标：

- 结构化 episode 落盘；
- 每 30–120 秒阶段性 consolidation；
- session end 生成 recap + resume_state；
- 启动时加载 resume + relevant memories。

文件：

```text
memory/episodes.py
memory/consolidator.py
state/resume.py
agent/context_builder.py
session/record_session.py（新事件）
```

验收：

- 重启进程/电脑后，下一场首轮能自然引用上一场；
- 不把整场 transcript 塞进 prompt；
- resume_state 与实际 location/quest 不一致时能纠正；
- Test A/C/D 通过（见 TEST_PLAN）。

回滚：停用 resume 注入，保留旧 memory.json。

---

## Phase 5：Belief / Relationship / Emotion / Thread

目标：

- 实现四个状态存储与更新；
- 检索加 importance/emotion/character/thread 权重；
- 情绪衰减与事件强化；
- 关系小步更新；
- 线索激活。

文件：

```text
memory/beliefs.py
memory/relationships.py
memory/threads.py
cognition/emotion.py
memory/retrieval.py
```

验收：

- Test B（错误猜测被推翻）、Test C（伏笔重激活）、Test D（情绪延续）通过；
- 不出现一次剧情关系暴涨；
- prompt 只看到自然语言摘要，不看到裸数值；
- 无 world_only 泄漏。

回滚：关闭新状态，回到 Phase 4 的 working/episode。

---

## Phase 6：Dialogue Ingestion 与 Dialogue Lock

目标：

- 接入 BetterGI OCR/UI/字幕，形成 conversation event；
- 识别对话开始/结束；
- 对话期间 Motor 不得跳过；
- 长对话结束后做 narrative interpretation。

文件：

```text
perception/dialogue.py
perception/ui.py
lumine/lumine_bridge.py
lumine/lumine_protocol.py
BetterGI vision/OCR（已有服务）
```

验收：

- Agent 能复述 NPC 最近说了什么；
- 对话期间不会连点跳过；
- 低置信度不编造；
- Test E（无剧透）不回归。

回滚：`DIALOGUE_LOCK=off`；OCR 失败时保守暂停。

---

## Phase 7：Speech Policy 与同源 Commentary

目标：

- speech 只来自 PlayerAgent 当前认知；
- 加入沉默/冷却/对话锁/情绪连续；
- speech 事件带 thought_source/emotion/intensity/pace/volume/pause；
- 不再由独立模型根据画面补台词。

文件：

```text
agent/speech_policy.py
agent/player_agent.py
session/session_format.py
render_session.py
```

验收：

- 普通移动自然沉默；
- 剧情对话不被覆盖；
- 情绪低落时不会因宝箱瞬间兴奋；
- commentary 与最近 intent/动作一致；
- 30–60 分钟人工验收通过。

回滚：`SPEECH_POLICY=legacy`（旧 director commentary）。

---

## Phase 8：视频/音频/TTS Prosody

目标：

- 录制游戏原音（loopback/wasapi）；
- 渲染混入 BGM/环境/NPC 语音；
- sidechain ducking；
- speech prosody 映射到飞鱼；
- 保留当时 speech 含义，不重写。

文件：

```text
session/record_session.py
session/render_session.py
cosyvoice/feiyu_tts.py
```

验收：

- 成片有游戏原音 + AI 语音；
- AI 说话时 BGM 降低；
- NPC 重要对话不被覆盖；
- 不同情绪有可感知的语速/音量/停顿差异。

回滚：`--no-game-audio` / 旧渲染路径。

---

## Phase 9：连续性与实况验收

目标：

- 实现 Test A–E 与对话锁/意图归属测试；
- 跑 30–60 分钟真实 session；
- 盲测「是否同一个玩家」。

文件：

```text
tests/continuity/*
tests/acceptance/*
scripts/run_continuity.py
```

验收：

- 跨 3 个 session 的人格/记忆/信念一致；
- 无剧透；
- 人工观看无数据库口吻/全知视角/突然失忆；
- 盲测多数认为「同一个玩家」。

---

## 迁移中的双写与开关

建议环境变量/配置：

```text
PLAYER_AGENT_ENABLED=0|1
PLAYER_AGENT_SHADOW=1
CONTEXT_BUILDER=legacy|v2
MOTOR_PROTOCOL=v1|v2
DIALOGUE_LOCK=off|on
SPEECH_POLICY=legacy|v2
MEMORY_BACKEND=openviking|local
RESUME_ENABLED=0|1
```

规则：

- 默认保持旧链路；新功能逐项开启；
- 每次切换前跑现有测试；
- 出问题一键回旧值；
- 所有新事件向后兼容，旧读取器忽略未知类型。

---

## 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| DeepSeek 延迟/成本 | 认知循环变慢/贵 | flash 用于实时；pro 只做 deep reflection；缓存/批量 |
| Lumine 2.5Hz | 动作不连续 | 动作填缝/保持；intent 层容忍低频；后续优化 vLLM |
| 双写状态不一致 | 记忆错乱 | 单一事实源 event log；结构化状态可重建 |
| 剧透 | 破坏体验 | world/player 分离；硬过滤；Test E |
| 人格漂移 | 不像同一个玩家 | profile + 稳定摘要 + 连续性测试 |
| 记录中断 | 视频缺失 | 视频/事件分离；录制器独立进程；断线重连 |
| 兼容性 | 现有链路坏 | feature flags + shadow + 回滚 |
| AGPL | 分发义务 | OpenViking 独立进程；保留许可证声明 |

---

## 明确暂不做

在认知连续性完成前，不优先投入：

- Live2D 精细动作；
- 更多 TTS 音色；
- 炫酷字幕/直播推流；
- 复杂 UI；
- 大规模后期特效。

它们属于表现层，不能替代「同一个主体/同一个过去」。
