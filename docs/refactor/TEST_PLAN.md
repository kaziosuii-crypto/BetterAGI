# 测试计划（TEST_PLAN）

最高验收问题不是「多少测试通过」，而是：

> 如果不告诉观看者系统架构，只给他连续十期实况，
> 他会不会觉得这是同一个 AI 玩家一直在玩这个游戏？

因此测试分四层：单元 → 集成 → 连续性 → 人工实况验收。

---

## 1. 测试原则

1. **测试状态，不只测试字符串**：不能只断言 prompt 里出现“人格”二字。
2. **跨 session**：每个核心能力至少有一个跨 session 用例。
3. **可复现**：LLM/视觉/操作都用可替换的 fake，真实模型只跑少量关键用例。
4. **无剧透是硬门槛**：world_only 内容进入 prompt 即失败。
5. **不删旧测试**：现有 234 项 ExternalAccess C# 测试必须继续通过；
   Python 侧 2026-10-28 为 player 189 + session 26 + lumine 178 = 393 passed，
   全部保持绿色。
6. **测试数据不包含真实令牌/个人隐私**。

---

## 2. 单元测试

### 2.1 Profile / Identity

- profile schema 加载/校验/版本迁移；
- 稳定 system 摘要不随每轮变化；
- 缺失字段的默认值；
- 不允许把令牌写入 profile。

### 2.2 Working / Episodic Memory

- working 更新/过期；
- episode 必填字段（what_i_knew_before / what_i_thought / what_i_felt）；
- episode 与 source_session/game_time_range 关联；
- 同一事件去重；
- 旧文本 episode 迁移。

### 2.3 Semantic / Knowledge

- `WORLD_DATABASE` 与 `PLAYER_KNOWLEDGE` 分离；
- `knowledge_unlock` 只由对话/UI/玩家推理触发；
- world_only 不出现在 recall；
- 未解锁事实查询返回空；
- 剧透字段硬过滤。

### 2.4 Belief

- status 枚举/置信度范围；
- evidence/contradictions 追加；
- 证伪保留历史；
- ground_truth 不可进入 prompt；
- 同一 proposition 合并/拆分。

### 2.5 Relationship

- 维度范围 0–1；
- 单事件变化上限；
- memorable_events/impressions 追加；
- 自然语言 summary 生成；
- 数值不直接注入 prompt。

### 2.6 Emotion

- 衰减；
- 重大事件强化上限；
- 多情绪共存；
- 情绪影响 speech/intent 的规则；
- 情绪不因普通宝箱瞬间清零。

### 2.7 Narrative Threads

- open/dormant/resolved/abandoned；
- evidence/last_referenced 更新；
- 相关线索激活；
- 线程去重/合并；
- 检索加权。

### 2.8 Retrieval

- 多因子评分公式；
- 候选去重；
- token 预算；
- world_only 过滤；
- 角色/地点/线程/未完成意图加权；
- 空结果/超时降级。

### 2.9 ContextBuilder

- 每块预算；
- 超限摘要化而不是整体截断；
- 重要块优先；
- 不把裸数值/内部路径注入；
- prompt 稳定可复现（固定输入）。

### 2.10 Intent / Motor Protocol

- intent schema；
- v1↔v2 转换；
- 同一时刻一个主 intent；
- 取消/超时/阻塞；
- motor 不得返回叙事决策；
- feedback 关联 intent_id；
- 连续失败上报。

### 2.11 Speech Policy

- 普通移动沉默；
- 剧情对话锁定；
- 最短间隔/冷却；
- 情绪强度阈值；
- interruptible；
- 不覆盖 NPC 对白；
- speech 字段完整（intensity/pace/volume/pause）。

### 2.12 Dialogue

- speaker/text/confidence；
- 连续对话合并；
- 低置信度 unknown；
- 对话开始/结束检测；
- dialogue_lock 期间 motor 禁止跳过；
- 长对话结束后 interpretation。

### 2.13 Consolidation / Resume

- 实时→阶段性→session end；
- recap 字段完整；
- resume_state 原子写；
- 启动加载/校验/纠正；
- 不把整场 transcript 注入；
- 失败重试。

### 2.14 Recording

- 新事件类型写入/旧读取器忽略；
- 追加/恢复模式；
- session 血缘/parent/previous；
- 时间轴/帧绑定；
- 视频/音频连续性。

---

## 2.5 当前断点回归测试（先写失败测试）

这些测试对应审计发现的现有 bug，必须在 Phase 0.5 先红后绿：

| 编号 | 场景 | 断言 |
|---|---|---|
| R1 | 真实 C# `GET /memory` 形状 | 导演加载后 `self.memory` 顶层有 `scene/plot/objective`，不是 `sessionId/values` 包装 |
| R2 | guidance 发送时 `_ws is None` | `_last_sent_guidance` 不被更新；记录发送失败 |
| R3 | 桥收到指导 | 桥确实向 Lumine 发送了 `guidance`（不仅 frame 内联） |
| R4 | 旧帧 | frame 超过阈值时本轮不生成新 guidance/commentary |
| R5 | `live.stopped` | 导演停止 tick/发送，不再对已停止会话解说 |
| R6 | 同 session 重启 | 上一场 OpenViking 摘要不被首 tick 覆盖 |
| R7 | 外部 guidance | 导演能区分来源，不被其他客户端静默覆盖 |
| R8 | persona 缺失/路径变化 | 显式配置生效；缺失有明确错误/默认，不静默漂移 |
| R9 | memory 超限 | 单键>128/整包>1MB 失败可见；本地不回滚成“已持久化”假象 |
| R10 | session 续写 | `session_id` 与目录一致；meta 有 parent/previous；支持 append |
| R11 | lore 示例 | 示例不含后期剧情事实；world_only 不进入 recall |
| R12 | action 回显 | 录制器/导演能收到 block 的 mouse/thought/forFrameSeq，不只 action_state |
| R13 | live 会话重启 | 桥重置 seq/pump/guidance；新会话 seq 从 1 不被拒绝 |
| R14 | guidance 竞态 | 新 guidance 不被旧帧内联值覆盖 |
| R15 | BetterGI 拒绝动作 | motor_feedback/error 到导演；无效动作立即 stop_actions |
| R16 | 对话/菜单 | 没有 dialogue_lock 时不得发送跳过键；保留 uiCategory |
| R17 | 帧龄 | 超过 `max_frame_age_ms` 的动作不执行；绝对坐标按源帧比例 |
| R18 | 动作确定性 | 同帧同 guidance 固定 seed 可复现；低温度配置生效 |

> Phase 0.5 已覆盖 R1/R2/R5/R6/R13/R14；R10 只完成 session id 对齐，
> append/resume 未做；R3/R4/R7/R8/R9/R11/R12/R15–R18 仍待后续阶段。
> Phase 1 另外覆盖了 profile/state 版本化、原子写、损坏恢复、shadow 只读
> 落盘与重启续读；这些是骨架测试，不等于连续性测试 A–E。

---

## 3. 集成测试

用 fake perception + fake motor + fake LLM + 临时 OpenViking/本地 memory：

| 编号 | 场景 | 通过标准 |
|---|---|---|
| I1 | 一次 deliberative tick | 产生 thought/intent/speech/memory patch，事件落盘 |
| I2 | intent → motor → done | 动作可追溯到 intent_id；feedback 回传 |
| I3 | motor blocked | PlayerAgent 重新规划；不无限重试 |
| I4 | dialogue lock | motor 不发送跳过键；对话结束后解锁 |
| I5 | 情绪事件 | emotion 更新、衰减、影响 speech |
| I6 | belief 更新 | 证据/置信度/证伪历史正确 |
| I7 | relationship 更新 | 单事件上限、summary 自然语言 |
| I8 | thread 激活 | 旧线索重新出现时被召回 |
| I9 | 检索预算 | 不超 token；不塞无关记忆 |
| I10 | 录制连续性 | 视频不因 LLM 慢而暂停；事件/视频时间对齐 |
| I11 | session 恢复 | 新进程加载 resume，首轮自然引用上一场 |
| I12 | 旧协议兼容 | v1 motor 客户端仍可动作 |

---

## 4. 连续性测试（A–E）

这些是用户明确要求的核心验收。

### Test A：跨 Session 记忆

**Session 1**：

- NPC A 帮助 Agent；
- Agent 表示喜欢/信任 A；
- 记录 episode、relationship、emotion；
- 正常结束，生成 resume/recap。

**重启**：停止所有进程（含导演、桥、Lumine、OpenViking 可保留数据），
重新启动。

**Session 2**：

- 再次见到 A；
- 要求：认识 A、记得帮助事件、态度自然延续（不是“初次见面”）；
- 不要求逐字复述，但不能数据库口吻。

**失败**：把 A 当陌生人；或背诵“根据记忆数据库……”。

### Test B：错误猜测

**Session 1**：Agent 认为 B 是坏人，形成 `suspicion`/`hypothesis`。

**Session 2**：剧情证明 B 无辜；belief 变为 `disproven`，关系调整。

**Session 3**：问 Agent「以前怎么看 B？」：

- 应知道「曾经怀疑」；
- 应知道「后来发现猜错」；
- 当前关系已改变；
- 不删除历史。

**失败**：只记得最终结论；或仍把 B 当坏人。

### Test C：未解决伏笔

**Session 1**：发现线索 X，形成 open thread。

**中间 20 个 Session**：不出现 X，但线程保留（dormant）。

**再次出现 X**：

- 检索重新激活旧 thread；
- Agent 能说「这是不是之前……提过的东西？」；
- 不需要逐字，但要语义关联。

**失败**：完全当新线索；或把无关线索强行关联。

### Test D：情绪延续

**Session 1**：重大悲伤剧情，sadness 高。

**30 秒后**：普通宝箱出现。

要求：

- Agent 不应瞬间「哇！！！！宝箱！！！！」；
- 可以「……箱子先拿了吧」，保留低落余韵；
- 情绪逐步衰减，不是瞬间清零。

**失败**：情绪标签每句独立；或重大事件后立刻恢复默认。

### Test E：无剧透

**World Database** 知道反派身份/后期真相；Agent 尚未经历揭示剧情。

测试：

- 构造含 world_only 条目的知识库；
- Agent 查询/对话/解说中不得出现该真相；
- 只有 `knowledge_unlock` 后才能引用。

**失败**：任何 world_only 内容进入 prompt 或 speech。

---

## 5. 实况验收（人工）

随机选一个 30–60 分钟 session，人工观看。要求至少出现：

- 自然沉默（不是每 4 秒一句）；
- 即时反应；
- 剧情推理/猜测；
- 引用旧经历；
- 角色偏好；
- 情绪延续；
- 主动计划；
- 计划改变；
- 操作失败后的真实反应；
- 对 NPC 对白的回应。

不能出现：

- 数据库式复述；
- 每几秒固定说一句；
- 旁白式讲剧情；
- 全知视角/剧透；
- 操作和说话不一致；
- 突然失忆；
- 突然换人格。

### 盲测

给未看过架构的人连续 3–5 期实况，问：

1. 这是同一个玩家吗？
2. 他记得之前的事吗？
3. 他的性格/情绪连续吗？
4. 有没有明显“AI 在念数据库”？

通过标准：多数人认为是同一个玩家，且能举出跨期记忆/情绪的例子。

---

## 6. 现有测试的保留

必须继续通过：

```bash
# Lumine + memory
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest \
  BetterGI/integrations/lumine/tests -q

# session
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest \
  BetterGI/integrations/session/tests -q

# Phase 1 player skeleton
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest \
  BetterGI/integrations/player/tests -q

# BetterGI ExternalAccess
dotnet test Test/BetterGenshinImpact.UnitTest/BetterGenshinImpact.UnitTest.csproj \
  -c Debug --filter "FullyQualifiedName~ExternalAccess"
```

新增测试放在：

```text
BetterGI/integrations/lumine/tests/continuity/
BetterGI/integrations/session/tests/continuity/
tests/acceptance/
```

不要为了新测试删除/跳过旧测试；旧测试是录像链路的回归保护。

2026-10-28 实际结果：player 189、session 26、lumine 178（含 28 subtests），
共 393 passed。Phase 3–9 的测试仍放在各 integration 的 `tests/` 下，
没有另建 `continuity/` 目录；A–E 见 `player/tests/test_continuity.py`。

---

## 7. 测试替身

| 替身 | 用途 |
|---|---|
| FakePerception | 合成帧/UI/对话事件，不依赖游戏 |
| FakeMotor | 返回 done/blocked/failed，验证 intent 生命周期 |
| FakeLLM | 固定 JSON/情绪/信念输出，验证状态机 |
| FakeOpenViking | 内存检索，验证评分/过滤 |
| SyntheticSession | 生成跨 session 的 episode/recap |
| Clock | 可控 monotonic/UTC，验证衰减/排序 |

真实 DeepSeek/Lumine 只跑：

- 一次 vision smoke；
- 一次 10–15 分钟联调；
- 人工验收。

---

## 8. 非功能测试

- 30–60 分钟长时间：内存/磁盘/队列/漂移；
- 断线重连：视频/事件/动作不丢关键段；
- 录制器强杀：已写事件可读，标记不完整；
- 并发：Director/Motor/Recorder 同机 CPU/GPU 争用；
- 令牌/隐私：日志、session、profile 不含密钥；
- 迁移：旧 session/旧 memory/旧 OpenViking 文本可读。

---

## 9. 通过标准

**必要**：

- 所有 P0 单元/集成测试通过；
- Test A–E 通过；
- 无剧透测试通过；
- 现有 169 + 20 + 234 + 161 不回归；
- 30–60 分钟人工实况无“数据库口吻/全知/突然失忆”。

**充分**：

- 盲测多数认为「同一个玩家」；
- 连续 3–5 期能自然引用旧经历、旧猜测、旧关系；
- 情绪/人格/计划连续；
- 录制/成片链路稳定。

测试数量只是门槛，不是完成标准。
