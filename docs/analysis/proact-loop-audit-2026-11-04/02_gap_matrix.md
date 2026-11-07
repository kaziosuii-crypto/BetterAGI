# ProAct 设计要素 vs 我们的实现：逐项对照矩阵

> 判定口径：**「有」= 生产路径真实生效**；**「假有」= 代码存在但无调用点/被旁路**；**「无」= 不存在**。
> 证据行号以当前工作树为准（E:\BetterAGI）。三项独立精读（快路径 / 记忆学习 / 运行证据）交叉核对。

## 0. 总览打分

| 维度 | ProAct | 我们 | 判定 |
| --- | --- | --- | --- |
| 快慢分层 | 两个独立系统，并行 | 慢 LLM 串行调工具，工具内部再起快系统 | **假分层**（快系统是工具的从属） |
| 慢系统节奏 | 连续 streaming cycles | 无 tick，只在工具调用期间存在 | **无** |
| 慢系统时延上界 | 有界 memory bank 锁死 | 无界对话窗口 + 6 步上限，无时延预算 | **无** |
| 记忆 | 三层、每层一句话、session 级 | JSONL/Graphiti 双后端，但 agent 从不调用 | **假有** |
| 意图注入 | ControlNet / prompt / locomotion 三通道 | `intent.py`+`arbiter.py` 完整实现，生产零引用 | **假有（死代码）** |
| 克制门控 | 五维打分 + τ=4 + 通道级 5 分 | 无；提示词反而要求「每轮至少推进一步」 | **反向** |
| 去重 | per-behavior action-history check | 无；且 gamescaling history 生产路径恒空 | **无** |
| 动作闭环 | 每 chunk 内持续观测 | 200ms 盲按键，动作窗口内零观测 | **开环** |
| 中断 | 支持被打断（VAD） | 无外部中断原语，只能等预算耗尽 | **无** |
| 失败恢复 | 计划可重规划 | 任一错误直接终止整段 | **终止式** |

---

## 1. 逐项对照（含证据）

### 1.1 Behavioral System（快系统）

| 要素 | ProAct 做法 | 我们的实现 | 证据 | 差距 |
| --- | --- | --- | --- | --- |
| 常驻性 | 全程在线 | 每次 `lumine()` 调用新建 WS、结束即断 | `agent.py:1195` connect / `agent.py:1226` close；`fast_stream.py:91-99, 183-203` | 跨调用无视觉/对话上下文，240s 视频历史随 stop 丢弃 |
| 连续视频流 | 持续多模态输入 | `pump=False`，实际每决策 1 帧 | `agent.py:1100`；`fast_stream.py:101-114` 泵未启动 | `fps=2.0` 参数完全失效 |
| 多帧上下文 | 3 帧均匀采样 | `extra_frames` 能力存在但生产从不使用 | `omni.py:344-345` | 每轮 1 帧，无场景动态 |
| 输出契约 | 结构化意图 | 自由文本 + `[DONE]` + 理由 ≥4 字 | `executor.py:31-41, 56-78`；`MIN_DONE_REASON_CHARS=4` `executor.py:53` | 无 JSON 契约，靠字符串切分 |
| 打断 | VAD + 可被打断 | `turn_detection=None`（manual），无打断语义 | `omni.py:113` | 只能整轮取消 |

### 1.2 Cognitive System（慢系统）

| 要素 | ProAct 做法 | 我们的实现 | 证据 | 差距 |
| --- | --- | --- | --- | --- |
| 驱动 | 连续 cycle | 单线程 ReAct，`for step in range(1, steps+1)`，默认 6 步 | `agent.py:1667` | 工具调用结束即静默，无「无事可做时也在想」 |
| 上下文压缩 | 有界 memory bank，每层一句话 | 消息窗口 `KEEP_TEXT_MESSAGES=30` / `KEEP_IMAGES=3` 硬截断 | `tui.py:50-51` | 截断≠压缩：无摘要、无事件抽取 |
| 非对称窗口 | 编码器长、规划器短 | 无区分，全部塞进同一 messages | — | 慢系统时延随历史线性增长 |
| 记忆输入 | memory bank 注入 | `build_system_prompt(memory_rules, memory_bindings)` 参数存在，生产只传 profile | `prompt_builder.py:266-267,289-293`；`agent.py:1204,1309` | 记忆从不进提示词 |
| 记忆写入 | 编码器增量写 | 唯一真实写入在 TUI：`tui.py:1001-1014` | `memory_wire.py:18` 的 `WRITEBACK_TOOLS` 含 `act`，但 live 工具面无 `act` | 写回通道名存实亡 |
| 计划复用 | 上轮 plan 作为输入 | 无 plan 对象，只有 messages 历史 | — | 无「上轮计划」概念，无法自检偏离 |

### 1.3 门控与克制（差距最大的一块）

| 要素 | ProAct | 我们 | 证据 |
| --- | --- | --- | --- |
| 五维打分 | 有，1–5 | **无** | — |
| 阈值触发 | MAX ≥ 4 | **无**；`IntentBus.min_confidence=0.5` 写好了但未接线 | `intent.py:1-11` 仅被 `tests/test_arbiter.py` 引用 |
| 通道级克制 | Dialogue 仅 5 分 | **无**；提示词要求「每轮至少推进一步」 | `agent.py:201-202` 硬规则 1 |
| 行为去重 | action-history check | **无**；`Motor.history` 生产从不 `.record(` | `motor.py:110-128`；唯一 `.record(` 在 `agent.py:1333`（旧路径） |
| 默认静默 | 全 <4 时行为置空 | **反向**：`lumine` 必须产出动作，连续 3 次空动作即 `no_action` 退出 | `executor.py:283-287` |

> 这一行是整份对照里最刺眼的：**ProAct 的核心是「默认不动、有理由才动」，我们的提示词与执行器结构是「必须动、不动算失败」**。这不是参数问题，是设计哲学相反。

### 1.4 动作层

| 要素 | ProAct | 我们 | 证据 |
| --- | --- | --- | --- |
| 动作生成时延保证 | chunk 生成时间 < 播放时长 | 串行 RTT：抓帧 18–34ms + decide（0.34–0.61s 实测，无上限 60s）+ 198ms 盲执行 | `adapter.py:334,378`；`motor.py:30`；`fast_system.py:9`；`adapter.py:689,716` |
| 动作空间 | 连续关节角 + 速度 | 键鼠离散序列，每步 33ms，最多取前 6 步 | `adapter.py:689,707-716` |
| 语义注入 | ControlNet 分支，无意图时零初始化回落 | `instruction` 只写日志，行为由 `system_prompt` 决定 | `motor.py:152-158` |
| 观测→动作反馈 | 平滑闭环 | 动作执行完才抓下一帧，窗口内无观测 | `executor.py:235-268` |

### 1.5 失败处理

| 要素 | ProAct | 我们 | 证据 |
| --- | --- | --- | --- |
| 单次错误 | 可重规划 | 抓帧连错 3 次 / motor 异常 / 空动作 3 次 → 直接终止 | `executor.py:239-241, 258-261, 283-287` |
| 重试 | — | motor 仅对连接层与 5xx 重试 1 次；Omni `ask` 零重试 | `motor.py:167-190`；`fast_stream.py:130-156` |
| 超时 | 有时延预算 | `DEFAULT_TIMEOUT=60.0` **大于**整段预算 `budget_s=20.0` | `motor.py:30`；`executor.py:200` |
| 归因回灌 | 记忆里记「做过什么」 | 无；连自己上一轮按了什么都不会写回 | — |

---

## 2. 三个「假有」问题（比缺失更危险）

1. **意图总线死代码**：`intent.py` 120 行 + `arbiter.py` 172 行 + `fast_system.py` 249 行，设计文档完整（默认 NO_OP、dwell、urgency 抢占），**生产零调用点**。更糟的是 `OmniConfig.tools` 默认空（`omni.py:54`），`session.update` 只在 tools 非空时下发（`omni.py:117-119`），构造处只给 api_key+model（`fast_stream.py:213-214`）→ **模型从未被告知存在 `set_intent` 工具**。即使现在接线，也拿不到 function_call。这会让「我们有 ProAct 式克制」变成一句自我安慰。
2. **记忆层假有**：`memory.py` 484 行、`GraphitiMemory` 真实现（独立线程跑事件循环，避 `NoneType.send` 坑），但 `agent.py` 全文 grep `memory|Episode|recall` **零命中**。需求文档 FR-4 写的「Graphiti 唯一长期记忆库」在运行时是 `local-json`，因为两个 profile 的 `config.json` 都没有 `memory` 段（`memory.py:504` 默认 `local-json`；`profiles/undertale/config.json`、`profiles/genshin-impact/config.json`）。
3. **便签假有**：`profiles.write_note` 实现完整（4000 上限、2000 分块、原子写、超限抛错不截断），但**零生产调用者**，需求 FR-5 要求的 `write_note/read_notes/clear_notes` 工具从未注册 → 短期记忆实际只剩 30 条消息窗口。

---

## 3. 结论：我们缺的不是「双系统」，是三件具体的事

1. **快系统常驻化**：从「工具的从属线程」变成「一直在跑、永远持有最新几帧的进程内服务」；
2. **慢系统 tick 化 + 有界状态**：固定节拍 + 结构化的「局面摘要」（位置/任务/未完成交互/刚试过什么/失败过什么），而不是把 30 条消息塞进上下文；
3. **门控层从零建起**：五维（游戏版）打分 + 阈值 + 两级去重，把「默认不动」变成系统的默认状态。
