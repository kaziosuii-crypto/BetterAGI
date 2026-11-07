# PersistentPlayer：设计与实现总览（真人评审稿）

> 版本：2026-10-28  
> 状态：设计评审稿，非验收通过声明。默认 shadow；真实游戏、AutoDL、飞鱼 TTS 与盲测尚未执行。  
> 读者：第一次接触该项目的资深工程师、架构评审者、安全/合规评审者。  
> 证据基线：代码审计（16 个区域）、现有测试、2026-10-28 本地 Python 回归结果。  
> 约束：全程只使用计算机图像识别与模拟输入，不读写游戏内存、不抓包、不注入。

---

## 目录

- 1. 文档目的与评审方法
- 2. 执行摘要与结论
- 3. 背景、问题与设计目标
- 4. 设计原则与安全红线
- 5. 总体架构与职责边界
- 6. 运行时生命周期与数据流
- 7. 玩家身份、档案与持久化
- 8. 记忆系统与上下文构造
- 9. 认知状态：信念、关系、情绪、线索
- 10. 对话感知与 Speech Policy
- 11. 意图协议与 MotorController
- 12. BetterGI C# Live Hub 与感知/执行接口
- 13. 会话录制、事件格式与视频
- 14. 后期渲染、TTS、音频与 VTuber
- 15. 安全、隐私与合规边界
- 16. 测试、连续性与验收
- 17. 部署、配置与运维
- 18. 设计取舍、替代方案与风险
- 19. 已知限制与未完成项
- 20. 评审指南与问题清单
- 附录 A. 文件地图
- 附录 B. 协议与数据 schema
- 附录 C. 命令与配置
- 附录 D. 术语表

---

## 1. 文档目的与评审方法

### 1.1 这份文档要回答什么

这不是使用手册，也不是重构路线图的宣传稿。它面向第一次接触该项目的资深工程师与评审者，回答三个判断性问题：**设计是否成立**（唯一玩家、观察与执行分离、知识边界是否自洽）；**实现是否可信**（代码路径、测试断言、协议契约、失败路径是否对得上）；**边界是否清楚**（哪些已进入生产链路、哪些只是 shadow 旁路、哪些根本没有实机证据）。

评审的产出不是“架构好不好看”，而是两个具体决定：其一，是否允许进入下一阶段（真实游戏 30–60 分钟、跨 3 个 session、盲测）；其二，签字前必须补齐哪些证据、修正哪些设计缺口。因此本文会反复区分“代码存在”“测试存在”“测试通过”“实机通过”四种不同强度的事实。

### 1.2 证据来源与审计范围

本轮输入分为三类：

1. **代码**：`E:\BetterAGI\BetterGI\integrations\player`、`integrations\lumine`、`integrations\session`，以及 `BetterGenshinImpact/Service/ExternalAccess/Agent/Live` 下的 C# Live Hub。
2. **设计文档**：`E:\BetterAGI\Docs\refactor` 下的 `README.md`、`CURRENT_ARCHITECTURE.md`、`GAP_ANALYSIS.md`、`TARGET_ARCHITECTURE.md`、`MIGRATION_PLAN.md`、`PHASE_0.5`–`PHASE_9`、`TEST_PLAN.md`。
3. **只读审计结果**：`Docs/PERSISTENT_PLAYER_AUDIT.json`。其 `result.audits` 有 10 个主审计区，`result.extra` 有 6 个补充审计区，覆盖生命周期与 shadow 边界、记忆与上下文、认知状态、intent/Motor、会话记录、后期流水线、只读边界、测试结论、C# Live Hub 集成等。补充区还覆盖了初版区域清单遗漏的导演循环、OpenViking 记忆桥、LLM 客户端与玩家档案。

审计代理本身是只读的：没有启动 BetterGI、原神或 AutoDL，也没有重跑 pytest。主编在 2026-10-28 使用项目 venv 实际执行了 Python 回归，结果为 **393 passed**（player 189 + session 26 + lumine 178，另有 28 subtests）；审计代理最初的 collect-only 也是 393。这个数字只代表本地/假服务端测试，不代表实机验证。

### 1.3 四种状态的定义

| 状态 | 判定标准 | 本文中的含义 |
|---|---|---|
| 【已实现且有测试】 | 代码路径完整；存在针对该行为的单元测试或假服务端测试，断言覆盖关键不变量 | 可以评审实现细节；主编已在 2026-10-28 实际执行 393 passed，但仍不等于实机通过 |
| 【已实现但未实机验证】 | 代码与本地测试存在，但依赖真实 BetterGI、游戏、LLM、AutoDL、TTS、声卡或长跑 | 设计可信度较高，运行可信度未知 |
| 【部分实现/未闭环】 | 关键代码和测试存在，但缺少反馈消费、生产接线，或存在会改变行为的已知时序缺陷 | 只能按“半成品”评审，不能按能力验收 |
| 【目标设计/未实现】 | 只有文档、接口、数据模型，或只存在于单测中的状态机；生产路径没有接线或没有调用者 | 不计入完成度，不能作为验收依据 |

另有第五类事实不单列为状态，但在各节标为“已知缺陷/文档不一致”，例如 recap 绕过证据门、对话锁 4 秒与 OCR 30 秒的采样错配、intent 反馈回路未接线。它们不是“未实现”，而是“已实现但设计上有洞”，对评审结论的影响往往比缺功能更大。

### 1.4 评审方法

本轮采用五步法，而不是只看 README：

- **先看生产链路，再看 shadow 旁路**。判断新模块是否真的接管了决策，还是只在旁边记录。`integrations/player` 默认是后者：生产链路仍是 DeepSeek 导演 + Lumine 自主操作 + BetterGI 执行。
- **对每个“已实现”追三条链**：谁调用（caller）、失败怎么办（failure path）、怎么回滚（switch）。只有三条都成立，才把它当作可运维的能力。
- **对每个“有测试”追断言**：测试用的是真实 DTO 还是 fake 自证。`fake_live_hub.py` 是按当前代码理解构造的，能证明内部契约，不能证明对端协议；`test_continuity.py:3-5` 自述是“缩小版回归（假数据，不联网、不启动游戏）”。
- **对每个安全结论追执行点**：静态 AST 白名单不是运行时沙箱；文档里的“不发送”必须落到具体代码路径、开关与测试。
- **文档冲突以代码为准**，并记录冲突。项目文档存在历史数字与已失效开关，例如 `MIGRATION_PLAN.md:405-425` 列出的 `DIALOGUE_LOCK`、`SPEECH_POLICY` 在代码中没有读取点；`README.md:1` 的“Phase 3–9 已落地”与 `player_agent.py:3` 的“Phase 1 骨架”自相矛盾。

### 1.5 替代方法及未采用原因

一个自然的替代方案是：把全量 pytest / dotnet test 绿灯当作评审依据。主编确实执行了 Python 回归（393 passed），但没有把它当作唯一依据。当前环境未安装原神，AutoDL 已停止，真实链路本来就跑不起来；项目历史上还出现过 fake 掩盖真实 DTO 的问题（`GAP_ANALYSIS.md` 的 G31/G39）。绿灯只能证明测试内部一致，不能证明外部协议正确。因此本轮以“代码路径 + 测试断言 + 契约对照”为主，并把“未执行”“未实机”显式写入结论。代价是：对时序、并发、真实 OCR/模型行为的判断只能到“设计上是否成立”，不能到“运行时是否稳定”。

### 1.6 阅读路径与本轮边界

第 2 节给出五分钟结论；第 3 节解释项目原来是什么、为什么要重构；第 4 节给出设计原则、安全红线、失败模式与回滚开关；后续章节分别展开记忆、认知状态、intent/Motor、录制与渲染、C# Hub 集成。本文不覆盖 C# 234 项测试的复现，也不对飞鱼 TTS 音质、真实声卡、推流表现做判断；这些在本轮都没有运行证据。

## 2. 执行摘要与结论

### 2.1 一句话结论

**设计方向成立，生产接管没有发生。** 把“导演 + Lumine 两个决策者”收回到一个 `PersistentPlayerAgent`，让 `MotorController`（Lumine）只把 intent 翻译成 200ms 动作，这是解决“两个玩家”的正确抽象；但截至本轮审计，生产链路仍是旧的 DeepSeek 导演 + Lumine 自主操作 + BetterGI 执行，`integrations/player` 是并行的 shadow 主体。代码完成的是“可运行的 shadow 骨架 + 较完整的本地测试”，不是“已接管的玩家”。

### 2.2 五分钟判断表

| 能力 | 状态 | 主要证据 | 评审判断 |
|---|---|---|---|
| shadow 观察、tick、resume、close | 【已实现且有测试】 | `player_agent.py:175-239,523-725`；`test_player_agent.py:170-975` | 骨架完整，单进程假 LLM 可回归 |
| 不向游戏发送动作/指导/解说 | 【已实现且有测试】 | `player_agent.py:10-11`；`test_run_shadow.py:162-203,218-264` | 静态 AST + 假 Hub 证明“接收循环不发”，不是运行时沙箱 |
| profile / resume / 原子写 / 版本与损坏恢复 | 【已实现且有测试】 | `player_profile.py:325-462`；`player_state.py:168-273`；`player_io.py:73-155` | 本地数据纪律较好；无目录级单实例锁 |
| v2 ContextBuilder 分块预算与 legacy 回退 | 【已实现且有测试】 | `player_context_builder.py:27-92,256-427` | 确定性、可预算，required 块不截断 |
| working / episode / semantic + OCR 证据门 | 【已实现且有测试】 | `player_knowledge.py:102-146`；`player_semantic_memory.py:215-278` | 核心可信；recap 是绕过证据门的例外 |
| belief / relationship / emotion / thread | 【部分实现/未闭环】 | `player_beliefs.py:60-191` 等四类 store | store 级测试充分；生产接线不完整，无跨进程锁 |
| intent 协议与 Bridge 发布 | 【部分实现/未闭环】 | `player_intent.py:123-285` | 机械链路成立；反馈闭环未接，默认 Motor prompt 不是 motor |
| dialogue lock | 【部分实现/未闭环】 | `player_dialogue.py:39-78` | 模块存在，但 4 秒 TTL 与 30 秒 OCR 错配 |
| speech policy | 【部分实现/未闭环】 | `player_speech_policy.py:24-81` | 只写 shadow JSONL，不进 TTS/录制 |
| Active 模式 / 同源解说进成片 | 【目标设计/未实现】 | `player_agent.py:695-716` | 没有执行器或 speech 接线 |
| 四层认知循环（fast/reactive/deep） | 【目标设计/未实现】 | `player_loop.py:47-64` | 只有单一 deliberative tick |
| 真实游戏 / AutoDL / 飞鱼 TTS / 盲测 | 【已实现但未实机验证】 | 环境限制；`PHASE_9_CONTINUITY.md:67-72` | 没有运行证据，不能宣称通过 |

### 2.3 哪些可信

第一，**shadow 边界在 player 进程内是可信的**。`PersistentPlayerAgent` 不持有 BetterGI HTTP 客户端，不暴露发送动作/指导/解说的接口；shadow 记录的 `sent=false`、`executed=false` 是记录器保留键，调用方无法伪造（`shadow_recorder.py:133-167`）。第二，**数据纪律可信**：schema_version、player_id 归属校验、未来版本拒绝覆盖、损坏文件 `.corrupt-*` 备份、临时文件 + fsync + `os.replace` 原子写，都有对应测试。第三，**OCR 证据门的设计可信**：事实必须绑定当帧 `frame_sha256`、session/frame、180 秒窗口与逐字引文，检索时 `KnowledgeGate.permits` 二次校验；`MemoryConsolidator` 明确不写 semantic（`facts_created=0`），正常路径只有证据门能写事实。第四，**上下文与失败降级可信**：同一输入生成同一 prompt，required 块不截断，超总预算时跳过模型并写 `shadow_context_error`；LLM、解析、记忆、记录器失败只记录并继续。

### 2.4 哪些只是骨架

- **唯一玩家没有接管生产**：生产链路仍是导演 + Lumine 双主体，`GAP_ANALYSIS.md` 的 G1/G2/G3 仍成立；PlayerAgent 默认并行观察。
- **intent 闭环没有闭合**：`IntentTracker` 有 `sent/running/done/failed/expired/needs_replan`，但 agent 只调用 `publish/cancel`，从不 `fetch_feedback/apply_feedback/expire`；Bridge 在 AutoDL 未连接时也返回 accepted；C# 动作回显不带 `intent_id`；默认 `prompt_mode=pretrain`，intent 对 Motor 没有约束力。
- **同源解说没有进成片**：SpeechPolicy 的决定只写 shadow JSONL，没有进入 `session_format`/`render_session`，正式 commentary 仍由旧导演产生。
- **多尺度循环没有拆**：只有 12 秒一次的 deliberative tick；没有 fast/reactive/deep，也没有事件触发的 reactive 认知。
- **对话感知只是锁**：任意 OCR 文本都会触发 4 秒锁（`player_dialogue.py:56` 的 `if in_talk or texts`），默认 OCR 30 秒一次；短对话可能完全落在两次观察之间，HUD 文字又可能误锁。
- **外部召回与线索重激活未接线**：OpenViking 在 player 侧被显式禁用，`memory_bridge` 恒为 None；`ThreadStore.activate()` 只在测试中被调用，生产检索命中不会重开 dormant 线程。

### 2.5 哪些没有验证

真实 BetterGI Live 的帧率、断线、半开连接与长跑稳定性没有验证；真实 `/api/v1/agent/observe` 的 OCR 质量、UI 分类、时序没有验证；真实 LLM 的 JSON 遵从率、视觉输入、超时没有验证；真实 Bridge 的 intent 幂等、deadline、hold 行为没有验证；AutoDL/Lumine 的 motor 模式与游戏实机没有验证；飞鱼 TTS 与 30–60 分钟盲测没有执行。当前环境未安装原神，AutoDL 已停止，393 项只在本地/假服务端执行通过，A–E 是单进程缩小版回归，不是“重开进程/跨 3 个 session”的实况验收。

### 2.6 签字前必须回答的五个问题

1. **recap 是否经过证据门？** `player_agent.py:201-216` 在 close 时直接用 `semantic_store.list()` 生成 recap，`recap_prompt_text` 把 fact 写成“屏幕上看过”，下一场注入 prompt；被篡改或来源未验证的事实仍可能跨场固化。要么强制 `KnowledgeGate.permits`，要么在 recap 中逐条标注“未验证”。
2. **对话锁的采样率是否够？** 4 秒 TTL、30 秒 OCR、12 秒 tick，三者不匹配；对话期间 hold 可能过期，锁过期后普通 intent 会替换 hold。
3. **intent 总线是否改变了“shadow”的语义？** 设置 `--intent-url` 后，PlayerAgent 的 goal 会经 Bridge → AutoDL → Lumine 产生真实动作，但记录仍是 `mode=shadow`、`sent=false`；`SHADOW_RULES` 还告诉模型“动作不会被发送”。这是间接控制路径，必须在文档和运行模式上重新命名与隔离。
4. **意图失败后谁重规划？** 现在没有 `done/blocked` 反馈到 PlayerAgent，`needs_replan` 无人消费；Motor 可能无限试，或者忽略 intent。
5. **并发与恢复的边界在哪里？** StateStore、belief/relationship/emotion/thread 没有 `file_lock`；`file_lock` 超时 5 秒会强制删除锁文件接管；同一目录两个 shadow 进程没有检测。强杀/断电时可能没有 `shadow_end`，JSONL 尾部可能半行。

### 2.7 结论分级

- **设计评审：有条件通过。** 方向正确，但必须先修正 recap 证据门、对话锁时序、intent 模式语义、反馈闭环与并发假设。
- **实现评审：shadow 范围可接受。** 单进程、假服务、假 LLM 的路径有较完整回归；Active 执行、同源解说、多尺度循环不具备。
- **实机验收：未进行，不能签字。** 需要至少 3 个 session 的连续录像、跨场引用、对话锁、无剧透、回滚演练与盲测记录。

## 3. 背景、问题与设计目标

### 3.1 项目原来是什么

重构前的系统不是“一个玩家”，而是 **两个独立决策主体 + 一个执行器 + 一个录像机**：

```text
                 ┌──────────────────────────┐
  JPEG 10–15fps  │ DeepSeek 导演             │ thought / guidance / commentary
  ──────────────►│ live_director.py          │──────────────► BetterGI Live Hub
                 └──────────────────────────┘                      │
                 ┌──────────────────────────┐                      │ guidance
  JPEG 5fps      │ Lumine 7B                │ 200ms action chunks  ▼
  + 一行guidance │ autodl_server.py         │──────────────► BetterGI 执行模拟输入
                 └──────────────────────────┘                      │
                                                                   ▼
                                                    action_state / action echo
```

导演看画面，产出 `thought`、`guidance`、`commentary`，并把少量文本写进记忆，但它**不产生任何动作**；Lumine 看另一份画面和一行 guidance，自己决定 200ms 动作块，但它**没有记忆、没有剧情目标、没有角色关系，也不向导演汇报意图**。`CURRENT_ARCHITECTURE.md:21-45` 把这称为“THERE ARE TWO PLAYERS”。

代码里有几处很直白的证据：导演默认人格把对方称为“AI 队友”，并说“喜欢和观众一起分析 AI 队友的操作”（`integrations/lumine/live_director.py:362-378`）；导演 system prompt 写“正和观众一起观看一位 AI 玩家操作游戏”（`live_director.py:489`）；Lumine 的 instruct 提示词写“You are an expert Genshin Impact player … Decide the best action”（`integrations/lumine/lumine_protocol.py:134-135`）；导演的消息分发没有 `action` 分支，只消费 `action_state.keys`（`live_director.py:1140-1160`）。

### 3.2 双认知主体造成的具体后果

- **解说与动作不同源。** commentary 来自 DeepSeek，action 来自 Lumine；导演不知道 Lumine 为什么转向、为什么战斗，只能看到按键集合。观众听到的“她想干什么”和画面里“她正在干什么”可能不是同一个判断。
- **目标没有单一所有者。** 导演只给一行 guidance，Lumine 可以执行也可以不执行，双方都不知道对方的完整意图；`CURRENT_ARCHITECTURE.md:139-146` 明确指出“没有真正的目标对象”。
- **没有可恢复的认知状态。** 导演只有 `memory.json`、最近解说、`_short_events`、`_episode_log` 和 OpenViking 文本；Lumine 没有跨请求记忆。重启后丢帧、丢短期事件、丢意图；没有 player_profile、resume_state、belief/relationship/emotion/thread。
- **session.jsonl 不是认知状态。** 它是录像事件流，能重放“发生了什么”，不能回答“当时我怎么想、我学到了什么、我为什么改变主意”。
- **世界真相与玩家已知知识没有分离。** lore 直接导入 OpenViking，玩家可能检索到尚未经历的剧情；`GAP_ANALYSIS.md` 的 G20/G37 把这列为 P0 剧透风险。

### 3.3 为什么这会毁掉“同一个玩家”的叙事

VTuber 录制的验收问题不是“这一帧动作像不像人”，而是“连续十期看下来，观众会不会觉得这是同一个 AI 玩家”。双主体系统在三个地方必然露馅：第一，人格漂移——导演的人格是静态主播描述，Lumine 没有长期偏好，二者的“性格”无法一致；第二，记忆断裂——上一场的情节只以文本摘要形式偶然被召回，无法形成“我记得我当时猜错了”的连续认知；第三，责任不可归因——一个动作失败时，无法判断是导演指导错了、Lumine 没执行，还是执行器拒绝了。`TARGET_ARCHITECTURE.md:11-35` 的第一原则“THERE MUST BE ONE PLAYER”就是针对这三点。

### 3.4 重构目标

| 目标 | 含义 | 代码落点 |
|---|---|---|
| 唯一玩家 | 一个 `PersistentPlayerAgent` 同时拥有 perception、working/episodic/semantic memory、belief/emotion/relationship/thread、goal/intention、planning、commentary 与长期身份 | `integrations/player/` |
| Motor 降级 | Lumine 只把 intent 转成 200ms 动作，不决定任务、路线、对话选项、角色偏好或长期目标 | `lumine_protocol.py:93` 的 `MOTOR_SYSTEM_PROMPT` |
| shadow 先行 | 先只读观察、验证记忆/人格/连续性，再考虑接管动作；旧录制/导演链路保持不变 | `run_shadow.py`；`run_episode.py` 的 `--player-shadow-config` |
| 跨场连续 | profile 固定人格，resume 保存上次想法/位置/任务，episode 记录经历，recap 在下一场注入 | `player_state.py`、`player_recap.py` |
| 无剧透硬边界 | 只允许 `player_knowledge`；玩家事实必须来自当帧 OCR 的逐字引文与证据哈希 | `player_knowledge.py:102-146` |
| 上下文可控 | 分块预算、required 块不截断、超限丢弃可选块、legacy 可回退 | `player_context_builder.py:27-92` |
| 意图/动作分离 | PlayerAgent 产生 goal/motor_mode，Motor 只翻译成动作 | `player_intent.py:63-101` |
| 可回滚 | 默认不启动 shadow/OCR/intent；环境变量与 CLI 开关保留旧链路 | `run_shadow.py:305-325,367-397` |

### 3.5 非目标

本轮明确不优先做：Live2D 精细动作、更多 TTS 音色、炫酷字幕/直播推流、复杂 UI、大规模后期特效（`MIGRATION_PLAN.md:444-454`）。也不以“推倒重来”为目标：BetterGI 的截图/输入/Live Hub、`session.jsonl`、OpenViking、CosyVoice 都要保留，先 shadow 双跑再灰度（`MIGRATION_PLAN.md:5-9,13-32`）。更不代表“测试数量等于完成”：393 只是回归门槛，最高验收问题仍是“不给架构信息，只看连续十期，观众会不会觉得是同一个玩家”（`Docs/refactor/README.md:48-53`）。

### 3.6 硬约束

项目只使用计算机图像识别与模拟输入，不读写游戏内存、不抓包、不注入、不做违规自动化。`PersistentPlayerAgent` 的代码边界正是这条约束的体现：它只读 BetterGI Live WebSocket，可选调用本机 `POST /api/v1/agent/observe` 取截图与 OCR，可选向本机 Bridge 发 intent，除此之外只调用 LLM；没有游戏写端点。默认 shadow 模式下 `proposed_actions` 永远 `executed=false`。这条约束也解释了为什么“精确状态”不能靠内存读取，只能靠画面、OCR、证据门和保守降级。

### 3.7 当前完成度

`Docs/refactor/README.md:4` 的定位是准确的：Phase 0–9 的代码骨架与模块级验证已完成，真实游戏/AutoDL/TTS 端到端与盲测尚未执行，shadow 默认不发送动作。但要补一句：**生产链路没有切换**。`extra` 总览审计的结论是“生产链路仍是 DeepSeek 导演 + Lumine 独立操作 + BetterGI 执行/录制，双认知主体问题未消除；`integrations/player` 是并行的 shadow 单主体实现”。因此当前状态应表述为：单主体骨架已落地并可本地运行，接管动作与同源解说尚未发生，实机验收为零。

## 4. 设计原则与安全红线

### 4.1 六条设计原则

**第一，唯一决策者。** 认知、记忆、目标、解说必须归同一个主体；Motor 只能执行，不得自行选择任务、路线、对话选项或角色偏好。`TARGET_ARCHITECTURE.md:83-106` 把这条写成职责表，`lumine_protocol.py:93` 的 motor 提示词也明确“You do NOT choose goals, quests, routes, dialogue options”。为什么：双主体不仅导致解说与动作不同源，还让失败无法归因，连续性无法验收。

**第二，观察与执行分离，shadow 默认。** `PersistentPlayerAgent` 不持有游戏写客户端；所有提案只记录 `executed=false`。为什么：先验证记忆/人格/连续性，再承担动作风险；同时保留旧录制链路可回滚。替代方案是“先 Active 再补 shadow”，未采用，因为一旦接管动作，任何记忆/人格错误都会直接变成游戏行为，无法区分是认知问题还是执行问题。

**第三，知识 fail-closed。** 只有 `visibility=player_knowledge` 的 episode/fact 能进入检索与 prompt；fact 必须绑定当帧 OCR 证据，引文必须是证据文本的逐字子串，且 session/frame/frame_sha256/180 秒窗口全部匹配（`player_knowledge.py:102-146`）。为什么：这是“不剧透”唯一可执行的证明方式；没有证据的模型自述只能算候选，不能入库。

**第四，上下文确定且有预算。** 同一输入生成同一 prompt；identity/perception/output 为 required，绝不截断；超总预算按优先级丢可选块，required 仍超限则本轮不调用模型并写 `shadow_context_error`（`player_context_builder.py:27-92,256-427`）。为什么：不可预算的 prompt 会在长局中悄悄丢掉关键观察，或者把输出 JSON schema 截断，导致解析失败。

**第五，会话隔离。** `live.started`、`live.stopped`、帧序号回退、hello sessionId 变化、WS 重连都清空旧帧/旧指导/旧解说/旧动作/旧对话状态，并递增 `session_generation`；LLM await 后检查 generation，丢弃换场期间的在途提案（`player_agent.py:440-481,523-547,578,644,670`）。为什么：旧场污染新场是最难事后发现的错误，必须在入口处硬清。

**第六，失败隔离与可审计。** LLM、解析、检索、记忆写入、shadow 写盘、意图发布、OCR 观察的失败都只记录或降级，不终止主循环；每条 shadow 记录固定 `mode=shadow`、`sent=false`、`executed=false`、`seq/run_id/session_id`（`shadow_recorder.py:133-167`）。为什么：shadow 的价值在于“即使模型/服务坏了，也能留下可复盘的证据”，而不是把异常吞掉。

### 4.2 关键替代方案与未采用原因

| 替代方案 | 看起来可行的理由 | 未采用原因 |
|---|---|---|
| 给 Lumine 加记忆/目标，让它直接成为玩家 | 改动小，Lumine 已有视觉与动作能力 | Lumine 是 7B motor 模型，无长上下文；会与导演继续形成双主体；动作模型承担叙事决策后无法审计“为什么” |
| 单模型端到端：帧输入，直接输出动作与解说 | 架构最简单，没有协议转换 | 200ms 动作需要高频低延迟，认知需要 3–15 秒与长上下文；同一模型无法同时满足；成本与失败隔离都差 |
| 先 Active 接管，再补 shadow 与回滚 | 见效快，能尽早验证动作 | 没有基线就无法判断错误来自认知、记忆还是 Motor；一旦接管，旧录制链路可能中断，违背“不破坏现有资产” |
| 读游戏内存/抓包获取精确位置与任务状态 | 状态最准，能消除 OCR 误差 | 违反项目硬约束与合规边界；且会让“只靠画面”的设计假设失效 |
| 把世界库/攻略直接放进 prompt | 能显著提升“正确率” | 直接剧透；玩家已知知识必须来自游玩/画面；世界库只能做内部校验 |
| 用 OpenViking 作为玩家主记忆 | 已有向量检索与长期存储 | 无法逐条证明召回内容属于玩家知识；player 侧已显式禁用外部召回（`player_retrieval.py:204-226`） |
| 多进程并行 tick 提升吞吐 | 单 tick 里 LLM 可能 60 秒，串行会拖慢 | 没有目录级锁；state/belief/relationship/emotion/thread 无 `file_lock`；并行会丢更新与交错写 |

### 4.3 安全红线

1. **player 不发送游戏动作/指导/解说。** 允许的出站只有：BetterGI Live WS（只收）、可选本机 `POST /api/v1/agent/observe`、可选本机 intent `/intent`、LLM HTTPS。执行证据是 `test_run_shadow.py:162-203` 的 AST 白名单与 `:218-264` 的假 Hub `RECEIVED=[]`。缺口：静态扫描只按属性名，不覆盖 `.request("POST")`、`getattr` 动态调用、session/lumine 模块；Live WS 地址没有 loopback 限制，token 可能发往配置指定的任意主机。
2. **不把世界真相注入玩家 prompt。** episode/fact 在 store、retriever、ContextBuilder 三层 fail-closed；profile 拒绝 `world_only/ground_truth/spoiler` 键。缺口：recap 直接消费 `semantic_store.list()`，绕过 `permits`；guidance/commentary/profile 自由文本仍被视为受信输入，没有内容级剧透过滤。
3. **不直接执行模型输出。** `parse_proposal` 宽容提取 JSON，但随后限长、枚举白名单、数值 clamp、动作最多 6 条且 `executed=false`；解析失败只写 `shadow_parse_error`。为什么：模型输出必须视为不可信输入，不能因为“它说了”就执行。
4. **不伪造审计记录。** `shadow_recorder` 的 `mode/executed/sent/type/seq/...` 是保留键，调用方同名字段被忽略；重复 close 只写一次 `shadow_end`。缺口：`_seq` 自增与 close 竞争不在锁内，当前单写者未触发。
5. **不落盘密钥。** token 只从 CLI/`--token-file`/`BETTERGI_AGENT_TOKEN` 读取，JSON 配置中的 token 被忽略；`build_ws_url` 移除旧 token 再附加；日志用 `redact_url`，记录递归脱敏，profile 拒绝 secret 键。缺口：guidance/commentary 原文与截图会发往可配置的 LLM 端点；`run_shadow` 通用异常用 `LOGGER.exception`，未必走 `_safe_error`；仓库内 `integrations/lumine/bridge_config.json` 含 `bettergiToken`/`lumineToken` 字面量（不在此复述值），应按已泄露处理并轮换。
6. **不静默降级或覆盖新版本。** schema_version 高于当前程序时拒绝启动；player_id 不匹配拒绝启动；损坏文件先备份 `.corrupt-*` 再重建；原子写保证完整旧版或完整新版。缺口：episode list 对损坏文件只跳过不备份；belief/relationship/emotion/thread 的 `_recover` 可能吞掉备份失败。
7. **不无界上下文。** 分块预算与丢弃顺序是硬逻辑；required 超总预算直接跳过模型。缺口：单条记忆可超过 `max_chars`（首条例外），`max_msg_size=0` 且 JPEG 无大小上限，LLM 成本只受 tick 间隔约束。
8. **不跨场污染。** 五类会话边界都触发 reset；在途提案按 generation 丢弃。缺口：reset 不取消已发布到 Bridge 的 intent/hold；`live.started` 在当前 C# 服务端不 Broadcast，agent 的该分支实际是死分支。
9. **不把测试数量当完成标准。** 393 项只在本地/假服务端执行通过；A–E 是单进程缩小版；真实游戏/AutoDL/TTS/盲测未执行。这条红线既是工程纪律，也是文档诚信要求。

### 4.4 失败模式与恢复

| 失败 | 当前行为 | 恢复 | 残留风险 |
|---|---|---|---|
| WS 断线/半开 | 记录脱敏错误，`reset_observation`，按 `reconnect_delay` 重连（`run_shadow.py:242-279`） | 自动重连 | 半开连接靠 heartbeat=30s；重连补发旧帧按采集时间拒绝，但 timestamp 缺失会回退 received_ms |
| LLM 超时/解析失败 | 写 `shadow_llm_error`/`shadow_parse_error`，本轮跳过 | 下一 tick 继续 | 60 秒 await 期间 now 变旧；帧龄不重检 |
| 上下文超预算 | 写 `shadow_context_error`，不调用模型（`player_agent.py:649-652`） | 下一轮重建 | 连续超预算会长期沉默 |
| 记忆写入失败 | 写 `shadow_memory_error`，tick 继续 | 下一轮重试 | 可能“记忆已变但 shadow_tick 未写”，审计链断裂 |
| 记录器失败 | `write_errors` 计数，不中断 | 继续运行 | 关闭后写入被捕获；JSONL 可能缺记录 |
| OCR 失败 | 回退 Live 帧 | 下一 tick 再试 | 对话锁/证据门本轮不更新 |
| intent 发布失败 | `published=false`/`publish_failed`，tick 继续 | 下一 tick 重发 | Bridge 可能已执行但玩家不知道；无 cancel on close |
| state 损坏 | 备份 `.corrupt-*`，返回默认状态 | 从空 resume 继续 | 丢失跨场记忆；备份失败则启动失败 |
| schema 过新 | 拒绝启动，返回 2 | 人工升级程序 | 不会覆盖新数据，但会停机 |
| 强杀/terminate | Windows 不执行 Python finally | 下次启动重新加载 | 可能没有 `shadow_end`，JSONL 尾部半行 |
| 磁盘满/权限失败 | 多数只记录，state 保存返回 False | 下次再试 | 无磁盘配额与轮转；episodes/JSONL 只增不减 |
| 同目录双进程 | 无检测 | 无 | state/四类状态丢更新；working/semantic 的 5 秒锁可被强制接管 |

### 4.5 并发、重连与损坏恢复

**进程内并发**靠“单事件循环 + 单 ticker + 串行 tick”避免：`DeliberativeLoop.run` 顺序调用 `agent.tick`，单轮异常被捕获后继续（`player_loop.py:47-64`）。tick 在 await 前 deepcopy snapshot，await 后检查 `session_generation` 与 `_closed`，因此换场期间的在途提案会被丢弃。这个设计的代价是：`observe()` 在 `live.started/live.stopped/帧回退/hello 变化` 时会同步调用 `consolidator.reset_session → working_store.save`（file_lock + fsync），在接收协程里阻塞事件循环；检索会全量扫 episode/fact 文件，consolidation 同步写多个 store，也会延迟 WS 消息处理。

**跨进程并发**没有统一答案。`StateStore.save` 只做原子写，不加锁；belief/relationship/emotion/thread 也没有锁；只有 working 与 semantic 使用 `file_lock`。`file_lock` 超时 5 秒后会直接 `unlink` 锁文件并继续，不检查 PID/心跳，因此“接管失效锁”可能破坏仍在写入的进程。部署假设是“同一 state/memory/shadow 目录只允许一个 shadow 进程”，但代码没有目录锁，也没有单实例检测。

**重连**由 `_run_forever` 处理：断线记录错误、`reset_observation`、等待 `reconnect_delay`；`live.stopped` 或 stop file 则退出。重连前清空旧观察是正确方向，但 `hello` 只在旧、新 sessionId 都非空且不同时才重置，`live.started` 又依赖服务端 Broadcast（当前 C# 只写时间线事件，不广播），所以真实的换场识别主要靠 hello、帧序号回退和 live.stopped。

**损坏恢复**分三档：state/working/semantic 备份后重建或拒绝覆盖；episode 对损坏文件只跳过并告警；belief/relationship/emotion/thread 的 `_recover` 备份失败被吞掉，随后 save 可能覆盖原损坏文件。强度不一致，是评审应要求统一的部分。

### 4.6 回滚开关

| 开关 | 作用 | 证据 |
|---|---|---|
| `PLAYER_AGENT_ENABLED=0` / `--disabled` | 不回创建目录，直接返回 0 | `run_shadow.py:305-323` |
| `PLAYER_AGENT_SHADOW=0` | 拒绝启动并返回 2（防止误把 shadow 当 Active） | 同上 |
| `CONTEXT_BUILDER=legacy` / `--context-builder legacy` | 回退 Phase 1 拼接，不注入召回记忆 | `player_context.py:113-169` |
| `--no-llm` | 只写 `shadow_observation` 心跳，不调模型 | `player_agent.py:549-560` |
| 不传 `--observe-ocr` | 不请求截图，不产生 observe 服务端事件 | `run_shadow.py:367-376` |
| 不传 `--intent-url` | 不发布 intent，player 完全只读 | `run_shadow.py:388-397` |
| `MOTOR_PROTOCOL=v1` | 关闭 Bridge 的 v2 intent 总线 | `lumine_bridge.py:697-714` |
| 删除 `resume_state.json` | 回到空 resume | `player_state.py:168-227` |
| `run_episode --no-agents` | 清空导演/Lumine/shadow 配置 | `run_episode.py:252-295` |
| `--no-game-audio` / 旧渲染路径 | 关闭游戏原音与 ducking | `record_session.py`/`render_session.py` |

需要特别指出：`MIGRATION_PLAN.md:405-418` 与 `PHASE_6_DIALOGUE.md:81-83`、`PHASE_7_SPEECH.md:86-88` 宣称的 `DIALOGUE_LOCK=off`、`SPEECH_POLICY=legacy`、`MEMORY_BACKEND`、`RESUME_ENABLED` 在代码中没有读取点。评审不能把文档里的开关当作可回滚证据；实际可用的只有上表这些。

### 4.7 评审者应带走的三句话

第一，“唯一玩家”是正确的问题定义，但当前只完成了 shadow 旁路的单主体，生产链路仍是双主体。第二，工程纪律（原子写、schema、证据门、预算、脱敏）比功能数量更可信，但并发与恢复仍按单进程假设运行，不能直接上生产。第三，任何“已实现”都要追问三个问题：谁调用、失败怎么办、怎么回滚；任何“有测试”都要追问：测试的是真实协议，还是 fake 自证。

## 5. 总体架构与职责边界

### 5.1 定位：三个平面，而不是一条“大流水线”

这个项目最容易误读的地方，是把 PersistentPlayerAgent（玩家主体）当成整条系统的主循环。实际相反：它叠加在原有的“导演 + Bridge + MotorController（Lumine 动作执行器） + 录制/渲染”链路旁，是一个只读的 shadow 认知层，产出独立的 shadow 记录。代码自述与文档标题也存在冲突：`player_agent.py:3` 写“Phase 1 骨架”，`player_loop.py:3` 写“Phase 1 的最小 deliberative 循环”，而 `integrations/player/README.md:1` 写“Phase 3–9 已落地”。以代码为准：当前是**单一 deliberative tick + v2 分块 ContextBuilder**，不是多尺度认知循环。

按职责可以切成三个平面。理解这张图，后面所有“为什么某个组件不做什么”都能对上号。

```
┌───────────────────── 观测/认知平面（默认只读） ─────────────────────┐
│  BetterGI Live Hub（BetterGI 进程，C#）                               │
│  LiveSessionService / LiveWebSocketHandler                            │
│      │ WS 广播：hello / frame / guidance / commentary / action_state  │
│      ▼                                                                │
│  run_shadow.py 进程                                                   │
│   ├─ _receive_loop ──▶ PersistentPlayerAgent.observe()                │
│   └─ DeliberativeLoop ──▶ PersistentPlayerAgent.tick()                │
│            │                                                          │
│            ├─ ContextBuilder v2 ◀── MemoryRetriever ◀── 各 Store      │
│            ├─ shadow JSONL（独立证据）                                 │
│            └─ resume / working / episode / semantic / recap /         │
│               belief / relationship / emotion / thread                │
│                                                                       │
│  可选 --intent-url（仅 loopback）                                      │
└───────────────────────────────┬───────────────────────────────────────┘
                                ▼
┌────────────────── 控制平面（可选，默认关闭） ─────────────────────────┐
│ IntentPublisher ──HTTP POST /intent──▶ lumine_bridge.py（Bridge 进程） │
│                                          │ WS /ws/player               │
│                                          ▼                            │
│                                   AutoDL autodl_server.py + vLLM       │
│                                   （MotorController / Lumine，6×33ms）             │
│                                          │ action chunks               │
│                                          ▼                            │
│                                   Bridge 白名单/单调 seq               │
│                                          │ WS actions                  │
│                                          ▼                            │
│  BetterGI Live Hub ──▶ LiveActionExecutor ──▶ 游戏（模拟输入）         │
└───────────────────────────────────────────────────────────────────────┘

┌──────────────── 记录/渲染平面（旁路，权威记录） ─────────────────────┐
│ BetterGI Live Hub ──WS──▶ record_session.py（recorder 线程）          │
│                             ├─ session.jsonl（事件时间轴）             │
│                             ├─ gameplay.mp4（帧合成/窗口直录）         │
│                             └─ audio/game_audio.m4a（可选 dshow）      │
│                        render_session.py（离线）                      │
│                             └─ final.mp4 / subtitles.ass / render.json│
└───────────────────────────────────────────────────────────────────────┘
```

关键点：shadow JSONL 与 `session.jsonl` 是**两套互不消费的记录**。前者由 `integrations/player/shadow_recorder.py` 写，后者由 `integrations/session/record_session.py` 写；渲染器只读后者，不会把 shadow 提案渲染进成片（audit 4、audit 9 均确认）。这是当前的刻意隔离，也是“shadow 证据无法与成片逐帧对齐”的代价。

### 5.2 组件、宿主与硬边界

| 组件 | 宿主/进程 | 输入 → 输出 | 职责与硬边界 | 状态 |
|---|---|---|---|---|
| BetterGI Live Hub | BetterGI 进程（C#） | 游戏屏幕/会话 → WS 帧、指导、解说、动作回显 | 截图广播、动作队列与安全释放；`LiveHttpEndpoints.cs:28` 挂 `/api/v1/agent/live` | 【已实现但未实机验证】真实游戏/真实帧协议未验证 |
| `run_episode.py` | 编排主进程 | CLI/令牌 → recorder 线程 + 3 个子进程 | 先录制、等首帧、再拉起 director/bridge/shadow；令牌走环境变量 | 【已实现且有测试（FakeProcess）】 |
| `record_session.py` | recorder 线程 | Live WS → `session.jsonl` + `gameplay.mp4` + 可选原音 | 被动消费者；不读写内存、不注入 | 【已实现且有测试（frames 假 WS）/已实现但未实机验证（window/audio/重连）】 |
| `render_session.py` | 离线进程 | `session.jsonl` → `final.mp4`、ASS、`render.json` | TTS、时长对齐、混音、叠字幕；正常路径不写回会话 | 【已实现且有测试（SAPI/none）/已实现但未实机验证（飞鱼/SVC/原音）】 |
| `PersistentPlayerAgent` | shadow 进程 | Live 消息 + 本地记忆 → 提案 + `shadow_tick` | 不持有游戏写接口；`proposed_actions` 永远 `executed=false` | 【已实现且有测试（单测）/已实现但未实机验证（真实 Live/LLM）】 |
| `ContextBuilder` v2 | shadow 进程 | 快照/检索结果 → 分块 prompt | 确定性、可预算、超限丢可选块；可回退 legacy | 【已实现且有测试】 |
| 各记忆 Store | shadow 进程 | JSON 文件 ↔ 内存对象 | working/episode/semantic/recap/信念/关系/情绪/线程；版本化、原子写 | 【已实现且有测试】多进程一致性未验证 |
| `IntentPublisher`/`IntentTracker` | shadow 进程 | 提案 → `POST /intent` | 单主意图；只发目标不发按键 | 【部分实现/未闭环】发布有测试；反馈消费属【目标设计/未实现】 |
| `lumine_bridge.py` | Bridge 进程 | BetterGI WS ↔ AutoDL WS ↔ 意图 HTTP | 5Hz 抽帧、动作白名单、单调 seq；不做目标决策 | 【已实现且有测试（mock）/已实现但未实机验证（真实 GPU）】 |
| `autodl_server.py` + vLLM | AutoDL 进程 | JPEG + guidance + intent → action/status/error | 单连接单 worker、generation 丢弃在途；不读游戏内存 | 【已实现且有测试（mock/WS）/已实现但未实机验证（真实 Lumine）】 |

### 5.3 职责边界：哪些事情被结构性地排除了

第一，**玩家主体不能直接操作游戏**。`PersistentPlayerAgent` 不导入 `aiohttp`，模块内没有发送动作、指导、解说的接口；`run_shadow.py:169-194` 的接收循环只有 `observe`，没有 `send_*`。仓库还用 AST 静态测试禁止 `send_json/send_str/send_bytes/send_frame/stop_actions` 以及游戏写端点（`tests/test_run_shadow.py:162-204`）。这条边界由“代码结构 + 静态测试 + 假 Hub 无发送断言”三重保证。

第二，**Bridge 不做目标决策，AutoDL 不直接读游戏内存或进程状态**。Bridge 只把 MotorController 返回的动作块做类型/数量/字段白名单校验，并分配自己的单调 seq（`lumine_bridge.py:1301-1319`）；AutoDL 只接收 JPEG、guidance 和 intent，调用 vLLM，输出动作文本。真正接触游戏的只有 BetterGI 的 `LiveActionExecutor`，它在执行前检查截图新鲜、游戏前台、未停止，并丢弃迟到超过 200ms 的键块、在失去前台时释放全部按键（`LiveActionExecutor.cs:141-190,349-354`）。

第三，**“shadow”不等于“零外发”**。默认配置下玩家只读 Live；但 `--observe-ocr` 会 POST 本机 `/api/v1/agent/observe`（服务端会写观察事件，`player_vision.py:73-77`），`--intent-url` 会 POST 本机 Bridge 的 `/intent`（`player_agent.py:940-941`）。因此正确的表述是：**玩家进程不直接发送游戏输入；但启用可选能力后，系统存在控制面/观察面的副作用**。README 首句“零写接口”与后文“并非零副作用”需要合并阅读（audit 0、audit 14 的 doc_mismatches）。

第四，**录制器是旁观者，渲染器只写派生文件**。`session.jsonl` 是录制侧的权威时间轴，但渲染器的 `--out` 未做保护，理论上可被指向 `session.jsonl` 并被 ffmpeg `-y` 覆盖（`render_session.py:744-745,1283-1364`），这是实现与“绝不修改权威记录”承诺之间的缺口，不能当作已成立的不变量。

第五，**玩家知识只能来自可回溯的屏幕证据**。语义事实的写入路径不是“模型说了什么”，而是 OCR 行 → `EvidenceStore` 保存 `frame_sha256` 与引文 → `KnowledgeGate.unlock_quote` 校验同 session/同帧/180 秒内/逐字子串（provenance/证据门）→ 才写入 `visibility=player_knowledge`（`player_knowledge.py:102-130`）；检索与 ContextBuilder 又各做一次 `visibility` 硬过滤（`player_context_builder.py:286`）。模型的 `learned_facts` 只是候选，不会自动入库。这条设计把“角色扮演中的猜测”和“玩家真的看见过的事实”分开，避免模型把自己编的设定当成世界真相。代价是知识增长很慢，且过期证据在 `permits()` 阶段并不复查 180 秒窗口（audit 9 指出的缺口）。

### 5.4 shadow 与 active：同一条决策链，不同的输出端

| 维度 | shadow（当前唯一可运行模式） | active（目标设计/未实现） |
|---|---|---|
| 启动开关 | `shadow_mode=true`；`PLAYER_AGENT_SHADOW=0` 直接返回 2，不降级为 active（`run_shadow.py:319-323`） | 无启动路径 |
| 内心独白/提案 | 写入 `shadow_tick`，供审计 | 仍应保留记录 |
| speech policy | 只记录“是否开口、语速、音量、停顿”决策；不进入 TTS/渲染（`player_speech_policy.py:5`） | 同一决策交给 TTS/渲染 |
| proposed_actions | 只以 `executed=false` 记录，没有执行器 | 交给执行器/意图总线 |
| 意图总线 | 仅显式 `--intent-url` 时发布 goal；默认不启用 | 预计成为常规输出，并需要反馈闭环 |
| 主要风险 | 误判/误记；若开启 intent，仍可能经 Bridge 间接驱动游戏 | 需要额外安全门、预算与回滚 |

这里必须说清楚一个容易混淆的点：**shadow 模式限制的是“玩家主体直接发动作”，不是“整条系统绝不产生动作”**。一旦显式配置 `--intent-url`，玩家发出的目标会经 Bridge、MotorController 和 BetterGI 执行器变成模拟输入；默认不配置时这条链路才不存在。评审时要把“player 模块无写接口”和“整场运行无输入”分开判断。

从 shadow 走到 active，至少还缺四块，缺一不可：① speech decision 要接到 TTS/渲染并进入 `session.jsonl` 或渲染输入；② `proposed_actions` 要有执行器或完整的 intent 消费方；③ 玩家要消费 `/feedback`，否则 `needs_replan`、失败计数、deadline 都是死状态；④ 需要有比现在更严格的“目标合法性/频率/冲突”安全门，因为 active 下错误不再只是记录，而会变成真实输入。当前 `run_shadow.py:319-323` 对 `shadow_mode=false` 直接返回 2，等于主动拒绝半成品 active，这是正确的保守选择。

### 5.5 为什么这样分层：一个被否定的替代方案

最直接的替代方案是**单进程单体导演**：让一个 LLM 直接看画面、输出按键/鼠标，录制器顺手写盘。它被否定有三个原因。

其一，**时间尺度不匹配**。玩家 tick 默认 12 秒，LLM 超时上限 60 秒（`llm_client.py:39`）；而执行器按 33ms 键块、200ms 迟到阈值工作。让 12 秒级的模型直接决定 200ms 级输入，等于把实时控制交给最慢的环节。

其二，**安全与可审计性**。分层后，LLM 只能产出“目标级 intent”，具体按键由受限的 MotorController 生成，再经 Bridge 白名单和 C# 执行器安全检查；任何一层都可以独立关闭（`motor_protocol=v1`、`intent_port=0`、不配 `--intent-url`）。单体方案一旦模型输出异常，异常会直接落到输入层，缺少中间可拦截点。

其三，**故障隔离**。录制、导演、Bridge、shadow 分属不同进程；shadow 崩溃不应中断录制，Bridge 重连不应重置玩家记忆。`run_episode.py:88-131` 用独立子进程实现这一点。

另一个被明确排除的替代方案是直接读游戏内存/存档或抓包，以取得精确的世界状态与任务进度。它不只是合规问题：一旦允许读取“世界真相”，玩家就不再是“只看屏幕的玩家”，OCR 证据门、知识边界和 shadow 的可信度都会失效。当前设计宁愿接受“玩家可能看错/记错”，也不把 ground truth 注入玩家认知，这是这套架构最重要的取舍。

代价同样明确：跨进程契约更容易漂移。已发现的例子包括：玩家 README 的 `--intent-url .../intent` 与 `IntentPublisher` 只接受根地址冲突（`player_intent.py:212-221`）；服务端只广播 `live.stopped` 而不广播 `live.started`（`LiveSessionService.cs:183,268-270`），导致 agent 的 `live.started` 分支实际是死分支；intent 反馈闭环尚未接线。这些不是“实现细节”，而是分层架构必须持续维护的接口债。

### 5.6 架构结论

分层本身是成立的：观察、决策、执行、记录四条职责有清晰的代码边界，默认配置下玩家主体没有游戏写路径；Memory/ContextBuilder 的预算与证据门也体现了“模型推理不等于事实”的设计意图。但当前是**旁路 shadow 架构**，不是闭环玩家：active 属【目标设计/未实现】，意图反馈未消费，多尺度循环属【目标设计/未实现】，真实游戏/AutoDL/飞鱼均未联合验证。主编在 2026-10-28 实际执行了 Python 回归（393 passed），但没有启动游戏或 AutoDL；“有测试”只代表本地/假服务端测试通过。评审应把“分层设计是否合理”和“闭环是否已经成立”分开评价。

## 6. 运行时生命周期与数据流

### 6.1 进程与线程模型：单机、单实例、多进程 + 文件契约

整套系统不是服务端集群，而是“一台 Windows 机器 + 一个 BetterGI 实例 + 若干 Python 子进程”的组合。并发模型必须先看清宿主，否则容易把文件锁当成跨进程事务。

| 层 | 宿主 | 并发单元 | 关键事实 |
|---|---|---|---|
| 编排 | `run_episode.py` 主进程 | 主线程 + 1 个非守护 recorder 线程 + 3 个子进程 | 录制在进程内线程跑；director/bridge/shadow 用 `sys.executable` 另起进程（`run_episode.py:258-269`） |
| 录制 | recorder 线程 | `FrameSink` 写线程 + ffmpeg 子进程；`AudioSink`/`WindowSink` 各自 ffmpeg 子进程 | 帧合成与窗口直录是两种互斥 sink；音频是可选旁路（`record_session.py:81-137,199-230,256-300`） |
| 玩家 | `run_shadow.py` 进程 | 1 个 asyncio loop；receiver task + ticker task | `asyncio.wait(FIRST_COMPLETED)`，任一结束就取消另一个（`run_shadow.py:208-226`） |
| Bridge | `lumine_bridge.py` 进程 | 1 个 asyncio loop + 4 个 task：BetterGI、AutoDL、forward、stats | 可选 ffmpeg 录制线程；动作 seq 在单 loop 内自增（`lumine_bridge.py:750,794-797`） |
| 推理 | AutoDL `autodl_server.py` | aiohttp app；每 WS 连接一个 `PlayerSession` + 一个 worker task | 同一连接同一时刻最多一个推理请求；新帧覆盖 pending 帧（`autodl_server.py:275-305,405-474`） |
| 离线成片 | `render_session.py` 进程 | 单进程 + ffmpeg/PowerShell/飞鱼子进程 | 不与在线链路共享内存，只读 `session.jsonl` |

由此得到四条并发不变量：① 同一 agent 不会并发 tick，因为 `DeliberativeLoop.run` 是单任务顺序 await（`player_loop.py:47-64`）；② 同一玩家同一时刻只有一个主意图，新意图把旧意图标为 `cancelled`（`player_intent.py:134-145`）；③ 同一 AutoDL 连接只有一个在途推理，`generation` 递增可丢弃旧结果（`autodl_server.py:405-474`）；④ 状态/记忆文件默认按“单写者”设计，`StateStore`、信念/关系/情绪存储没有跨进程锁（audit 0、audit 3）。

### 6.2 启动顺序：先有画面，再有智能体

启动顺序不是随意的，核心约束是“智能体启动时必须有帧可读”。完整路径如下：

1. `run_episode.main` 解析 CLI，读取令牌（`--token`/`--token-file`，否则 `BETTERGI_LIVE_TOKEN`/`BETTERGI_AGENT_TOKEN`），构造 `SessionRecorder`，把 `recorder.run` 放进非守护线程（`run_episode.py:255-262`）。
2. recorder 可选先 `POST /api/v1/agent/live/start`，随后连接 Live WS；首条带 `timestampUtc` 的消息或首帧确定 `t0`，创建会话目录并写 `session.jsonl` 元信息（`record_session.py:548,598-614`）。
3. 主线程最多等 20 秒，直到 `recorder.frame_count > 0` 或录制线程死亡（`run_episode.py:263-269`）。这个等待是“导演/玩家不要对着空画面思考”的保护。
4. 依次启动 director、bridge、shadow 三个子进程；令牌只通过环境变量 `BETTERGI_AGENT_TOKEN` 传递，shadow 额外获得临时 `--stop-file`，可选 `--observe-ocr`（`run_episode.py:88-131`）。
5. shadow 内部：`configure_stdio → argparse → logging → asyncio.run(async_main)`（`run_shadow.py:492-508`）。`resolve_config` 按 CLI > 环境变量 > JSON > 默认值合并；`enabled=false` 返回 0，`shadow_mode=false` 返回 2（`run_shadow.py:305-323`）。
6. `ensure_directories` 创建 profile/state/shadow/memory 等目录；默认 profile 缺失才播种，自定义 profile 缺失直接退出 2（`run_shadow.py:325-333`）。
7. 构造 `ShadowRecorder`，再构造 working/episodic/semantic/recap/belief/relationship/emotion/thread/evidence 存储、`KnowledgeGate`、`MemoryRetriever`、`ContextBuilder`、`MemoryConsolidator`；可选构造 `ScreenObserver`、`IntentPublisher`、LLM 客户端（`run_shadow.py:335-407`）。
8. 构造 `PersistentPlayerAgent`：`StateStore.load()` 读取 resume（版本/归属/损坏检查），加载 working 与上一场 recap（`player_agent.py:136-145`）。
9. `agent.start()` 写 `shadow_start`（重复调用无副作用，`player_agent.py:175-194`）。
10. 建立 WS：`build_ws_url` 清掉旧 token 参数再附加新 token，`heartbeat=30s`、连接超时 15s、`max_msg_size=0`；连接成功后 receiver 与 ticker 同时启动（`run_shadow.py:197-226`）。

异常路径需要单独说明：如果 `ScreenObserver`/`IntentPublisher` 构造抛 `ValueError`，它不在 `run_shadow.py:433-443` 捕获的状态/记忆异常列表里，会冒泡到 `main` 返回 2，但此时 recorder 可能已经创建而没有写 `shadow_end`。启动顺序上的这类“半初始化”比 tick 内失败更难恢复。

### 6.3 一次 tick 的完整链路

tick 是整条认知链路的核心。它不是“取一帧调一次模型”，而是带门禁、快照、预算和代次检查的事务性尝试：

```
tick()
 ├─ 门禁：已 start / min_tick_gap / 有 latest_frame / 帧龄 <= max_frame_age
 ├─ 冻结：last_frame_seq、guidance、deepcopy(snapshot)、session_generation
 ├─ 无 LLM → shadow_observation → maybe_save_state → return
 ├─ working 超过 180s → reset_session
 ├─ 可选 OCR → 换图、清 guidance/commentary/actions、dialogue.observe
 │    ├─ 对话开始 → cancel 旧意图 + 发布 60s wait hold
 │    └─ 对话结束 → 仅当当前目标是 hold 时取消
 ├─ 本地检索（episode + semantic）
 ├─ ContextBuilder v2 → 总预算检查
 │    └─ 超限 → shadow_context_error，不调用模型
 ├─ await llm.complete(system, user, image_b64)
 ├─ 再次检查 generation / closed
 ├─ parse_proposal
 │    └─ 失败 → shadow_parse_error
 ├─ _apply_proposal（只改自我状态）
 ├─ speech_policy.decide（只记录）
 ├─ _publish_intent（可选）
 ├─ consolidator.consolidate（working/episode/四类状态）
 ├─ _unlock_ocr_quotes（证据门）
 ├─ 写 shadow_tick
 └─ _maybe_save_state（按 stateSaveIntervalMs 节流）
```

逐段对应代码：门禁在 `player_agent.py:523-547`；无 LLM 心跳在 `:549-557`；working 过期重置在 `:559-567`；OCR 与对话锁在 `:571-639`；检索/上下文/预算在 `:640-652`；LLM 调用与错误在 `:653-668`；代次复查/解析/应用/发布/固化/记录在 `:670-724`。`ContextBuilder` 的块预算在 `player_context_builder.py:27-42`，优先级丢弃在 `:78-92`，构建与 `visibility` 硬过滤在 `:256-416`。

还有一个数据契约事实：C# 构建帧消息时没有给 `uiCategory` 赋值，玩家侧读到的 `frame.ui_category` 实际总是空；真正的 UI 类别只能来自可选的 `--observe-ocr` 响应（audit 0、audit 9）。这也是为什么对话锁与 OCR 证据门都依赖 observe 路径，而不是 Live 帧本身。

时序上有一个必须点明的偏差：`now` 在 tick 入口只取一次（`player_agent.py:527`），而 LLM 超时上限是 60 秒。因此 speech 冷却、意图 `created_ms`/`deadline`、状态保存节流都基于“tick 开始时间”，不是实际完成时间；`await` 之后也不会重新检查帧龄，只看 `session_generation`。当一次模型调用跨过换场时，代码能丢弃旧提案，但不能发现“帧已经太旧”。

失败分支的设计原则是“单轮失败不终止主循环”，具体行为如下：

| 故障 | 代码行为 | 是否中断 tick |
|---|---|---|
| 没有帧 / 帧龄超限 | 直接 return，不写 tick | 否 |
| 无 LLM（`--no-llm`） | 只写 `shadow_observation` | 否 |
| Context 必需块超总预算 | 写 `shadow_context_error`，跳过模型 | 否 |
| LLM 超时/异常 | 计数 + `shadow_llm_error` | 否 |
| 模型输出无法解析 | 计数 + `shadow_parse_error`（raw_preview 脱敏限长） | 否 |
| 记忆存储写入失败 | consolidator 内逐存储 try/except，记 `*_error` | 否 |
| shadow JSONL 写失败 | `_write_errors += 1`，只告警 | 否 |
| intent 发布失败 | 记录 `publish_failed`，下一 tick 再试 | 否 |
| OCR 失败 | 回退使用 Live 帧 | 否 |
| 会话代次变化 | 丢弃在途提案 | 否（本轮返回） |
| resume 保存失败 | 只告警，返回 False | 否 |

shadow JSONL 的记录类型也不是随意字符串：启动 `shadow_start`、无模型心跳 `shadow_observation`、成功提案 `shadow_tick`、解析/模型/上下文错误分别是 `shadow_parse_error`/`shadow_llm_error`/`shadow_context_error`、会话边界 `shadow_live_started`/`shadow_live_restarted`/`shadow_live_stopped`、退出 `shadow_end`。每条记录的公共信封固定为 `schema_version/type/mode=shadow/executed=false/sent=false/seq/player_id/session_id/run_id/t_unix_ms/run_t_ms/timestamp_utc`，调用方传同名字段会被忽略，无法伪造 `sent/executed`（`shadow_recorder.py:133-157`）。

### 6.4 WS 重连与会话重置：三套重连，三种语义

系统里有三条独立的 WS 链路，它们的重连策略并不相同，不能混为一谈：

| 链路 | 重连策略 | 重连后做什么 | 已知风险 |
|---|---|---|---|
| 玩家 → BetterGI | 固定 `reconnect_delay`（默认 3s，配置 0–120s），`run_shadow.py:242-279` | `reset_observation()` 清空旧帧/指导/动作，再连 | 意图 tracker/已发布 hold 不取消 |
| 录制器 → BetterGI | 指数退避，初始 `max(500, reconnect_ms)`，上限 10s，`record_session.py:489-521` | 继续写同一个 Writer，不重新 start live | 计数是净值；退避成功后不重置 |
| Bridge → BetterGI / AutoDL | 指数退避 1–20s，稳定 10s 后重置，`lumine_bridge.py:999-1069` | 清 pump/guidance，补发 latest guidance/intent | 不向 AutoDL 发 cancel/reset |

玩家侧代码里登记了五个“换场”入口：`hello.sessionId` 变化、帧序号回退、`live.started`、`live.stopped`、WS 重连。它们最终都调用 `_reset_session()`（`player_agent.py:289-327,342-351,440-481`）。重置动作包括：`session_generation += 1`、清空 frame/guidance/commentary/recent_actions/action_state、重置 dialogue 与 speech policy、重置 working 并同步写盘。`session_generation` 是关键：tick 在 OCR 后、检索后、LLM 后三次比对它，确保换场期间返回的旧提案不会写进新场（`player_agent.py:578,644,670`）。

但重置并不完整：`IntentTracker` 不重置，`intent_publisher.cancel()` 不调用；`dialogue.reset()` 只清本地锁，不通知 Bridge 取消已经发布的 60 秒 wait hold。于是换场/重连后可能出现“Bridge 已清空、AutoDL 仍持有旧 intent、玩家 tracker 以为 hold 还在”的三方不一致，直到 hold deadline 到期或被新意图替换。这是当前生命周期里最值得优先修的边界问题之一。

输入边界还有一个缺口：玩家 WS 使用 `max_msg_size=0`，Live 帧的 base64 JPEG 也没有显式大小上限；异常或恶意本机广播可能造成大内存占用（audit 0）。在单机可信环境里这不是首要风险，但不应被当作“协议已加固”。

另有一个契约级问题：C# 服务端确实 Broadcast `live.stopped`（`LiveSessionService.cs:268-270`），但 `live.started` 只写入时间线，没有广播（`:183-184`）。所以 agent 的 `live.started` 分支在真实服务下不会触发；代码注释也承认“服务器不会广播 live.started”（`player_agent.py:343-344`），真实换场主要靠 sessionId 变化和帧序号回退。

### 6.5 关闭顺序、崩溃恢复与回滚开关

正常关闭有两条触发链。第一条是外部信号：`live.stopped`、`--stop-file`、`--duration`、Ctrl+C。`_run_forever` 退出后，`async_main` 的 `finally` 调用 `agent.close()`（`run_shadow.py:476-488`）。`close()` 的顺序固定为：生成并保存 session recap → 保存 working/consolidator → 强制 `save_state(force=True)` → `recorder.close()` 写 `shadow_end`（带 `shadow_ticks/parse_errors/llm_errors/write_errors`）→ 关闭 LLM 客户端 → 打印 stats（`player_agent.py:196-239`）。

第二条是父进程收尾：`run_episode` 默认让 recorder 在结束时 `POST /live/stop`（`run_episode.py:65-68`），BetterGI 广播 `live.stopped`，shadow 自行优雅退出；随后 `finally` 写 stop 文件作为兜底，给 shadow 最多 8 秒，再 terminate 其他子进程，统一等 10 秒后 kill（`run_episode.py:148-174,283-294`）。注意 Windows 的 `terminate()` 是 `TerminateProcess`，不会执行 Python 的 `finally`；因此强杀路径可能没有 `shadow_end`，JSONL 尾部可能是半行。

`close()` 本身也不等待在途 tick。如果从其他线程/任务调用，tick 可能已经写完部分记忆、最后发现 `_closed` 而丢弃结果，形成“磁盘已变、审计未记”的窗口。反过来，tick 内部是先写记忆/证据、最后才写 `shadow_tick`；如果最后一步失败，同样无法从 JSONL 事后重建这次变更。

不同产物的损坏恢复能力差异很大：

| 产物 | 写者 | 一致性手段 | 崩溃/损坏后 |
|---|---|---|---|
| `resume_state.json` | `StateStore` | schema_version、规范化、原子写；无锁 | 损坏备份 `.corrupt-*` 并回空状态；版本过新拒绝覆盖 |
| `working.json` | `WorkingMemoryStore` | 原子写 + `file_lock`（5s 超时强制接管） | 损坏备份；锁可被抢 |
| `facts.json` | `SemanticStore` | 原子写 + `file_lock` | 同上；证据文件单独校验哈希 |
| episode/信念/关系/情绪 | 各 Store | 原子写，无跨进程锁 | 损坏回退/备份；多进程会丢更新 |
| shadow JSONL | `ShadowRecorder` | 追加 + `run_id` + `seq`；无锁 | 同 session 多次运行追加同一文件；`strict=False` 只容忍最后一行半截，中间坏行不可读 |
| `session.jsonl` | `SessionWriter` | `"w"` 打开、逐条 flush，无 fsync/原子替换 | 半行会让 `read_session` 整份失败；无修复工具 |
| `render.json` | `SessionRenderer` | 渲染结束写报告 | 早期失败可能没有报告 |

`ShadowRecorder` 的实现细节还值得单独点名：`_seq` 自增与 `_closed` 检查在锁外，`close()` 与 `write()` 并发时不能保证 `shadow_end` 最后落盘；同一 session 的多个进程各自从 `seq=1` 追加，没有 run 级清单供读者切分（audit 14）。因此“线程安全/单写者”目前是调用约定，不是强制不变量。

录制侧最脆弱：`SessionWriter` 用 `"w"` 覆盖打开（`session_format.py:299`），只 `flush` 不 `fsync`（`:330`），没有原子替换；同名目录冲突时还可能截断旧会话。也就是说，**被称作“唯一权威记录”的 `session.jsonl`，持久性反而弱于玩家的 resume 状态**。评审若要做长时间运行，必须先补这一块的 fsync/截断修复/目录唯一性。

音频/头像链路还有两个独立缺口：`AudioSink` 在 Writer 打开后才启动 ffmpeg，首采样相对 `t0` 的偏移没有记录，而渲染器从 0ms 混入 `game_audio` 且不读 `audio` 事件（audit 4），所以“音画同步”目前不是已建立的不变量；`avatar` 参数由录制器写入但渲染器完全忽略，Live2D 成片属【目标设计/未实现】。

回滚开关是这套设计的保险丝，且大多数是“结构性缺失”而不是运行时分支：

| 开关 | 效果 | 语义 |
|---|---|---|
| 不传 `--player-shadow-config` 或 `--no-agents` | 不启动 shadow | 最干净的回退 |
| `PLAYER_AGENT_ENABLED=0` / `--disabled` | 返回 0，不建目录/档案 | 完全禁用 |
| `PLAYER_AGENT_SHADOW=0` | 返回 2，拒绝启动 | 明确“没有 active 可切” |
| `CONTEXT_BUILDER=legacy` | 回退 Phase 1 prompt | 丢 working/episode/beliefs，但仍运行 |
| `--no-llm` | 只写心跳观察 | 用于链路联调，不产生提案/记忆 |
| 不配 `--intent-url`/`PLAYER_INTENT_URL` | 不发任何 intent | 默认关闭控制面 |
| Bridge `motorProtocol=v1` / `intentPort=0` | 不启动 HTTP 意图总线 | 回到旧 guidance 链路 |
| 删除 `resume_state.json` | 下次启动回默认状态 | 可清空连续性，但不会自动备份 |
| `--capture none` / `--tts none` | 只记事件 / 只出字幕 | 分段排查录制与渲染 |

### 6.6 生命周期结论

当前生命周期可以概括为“**多进程 + 文件契约 + 单写者假设**”。在单机、单实例、正常退出的前提下，它能自洽地完成“观察 → 思考 → 可选意图 → 记录 → 关闭”的闭环；但跨进程没有统一事务，强杀可能留下半行记录，active 模式没有入口，意图反馈也没有回到玩家。它适合作为可审计的 shadow 原型，还不具备生产级的长时运行与故障恢复保证。

最后必须交代验证边界：本节所有“有测试”指仓库中存在对应测试代码，且主编已在 2026-10-28 实际执行（393 passed），但不代表实机通过。本次没有启动 BetterGI 或原神，也未连接 AutoDL；当前环境未安装原神，AutoDL 实例已停止，飞鱼 TTS 与盲测同样没有跑过。因此真实 Live 帧率、真实模型延迟、长时间 soak、断网恢复和实机动作安全性都仍需独立验证。

## 7. 玩家身份、档案与持久化

本章回答两个问题：PersistentPlayerAgent 认为“我是谁”，以及这个“谁”如何跨进程、跨场次保持不变。结论先行：profile 是启动时加载、加载后冻结的只读身份快照；所有会变化的东西都不在 profile 里。状态标记：`【已实现且有测试】` 指代码与单元测试都存在；`【已实现但未实机验证】` 指已接线，但只用假 WebSocket、假 LLM、假 OCR 跑过；`【目标设计/未实现】` 指只存在于设计文档。

### 7.1 为什么把身份做成冻结快照

Game Agent 常见的坏做法是把“人格”塞进每轮可变的状态文件：模型每轮改一点语气，几十轮后人格漂移；一次状态写坏，玩家连“自己是谁”都丢了。这里把数据按变化频率切开：profile 低频、人工维护、只读；resume/working/episode 高频、由程序写。`PlayerProfile` 是 `@dataclass(frozen=True)`（`integrations/player/player_profile.py:201-218`），加载后没有任何运行时写入路径；`close()` 只保存 resume、working、recap 与 shadow_end，不写 profile（`player_agent.py:196-239`）。

```text
profile.json ──load/validate──> PlayerProfile(frozen)
  低频、人工改                     │
                                  ├─ stable_summary() ─> ContextBuilder identity 块
                                  ├─ speech_style.silence_bias ─> SpeechPolicy.decide
                                  └─ player_id ─> StateStore 归属键 / recorder 元数据
state/*.json + memory/* ──每轮/每场──> 可变状态，与身份解耦
```

替代方案 A：把 persona 字段并入 `resume_state.json`。未采用，因为 state 每 30 秒写一次且要参与损坏恢复，把低频身份和高频状态绑在一起，会让一次 state 损坏同时损失人格与进度，也无法独立审计“谁改了人格”。替代方案 B：用数据库存 profile。未采用，因为当前是单机单进程 shadow，单文件可手改、可 diff、可随仓库复制，引入数据库只会增加部署面。

### 7.2 “八段契约”到底有几段

`schema_version=1` 是硬门槛：必须是整数且非 bool，`>1` 与 `<1` 都拒绝，明确不自动迁移（`player_profile.py:342-356`）。`player_id` 必填，去空白、≤64 字符、仅 `[A-Za-z0-9_-]`，同时用作 StateStore 归属校验和 ShadowRecorder 元数据。除 `identity` 外还有 8 个“人格段”，因此文档称“八段契约”；加上 identity，profile 实际有 9 个内容键（`player_profile.py:449-462`）。

| 段 | 关键字段与默认值 | 校验/上限 | 实际消费点 |
|---|---|---|---|
| identity | name 必填；pronouns 默认“她”；origin、role、self_description | 60/20/200/200/400 字符 | name 进 stable_summary；pronouns、role 目前无运行时消费者 |
| personality | traits 必填 1..8；flaws 0..6；quirks 0..6；baseline_mood | 单项 ≤200；baseline_mood ≤80 | stable_summary |
| values | 1..10 条 | 每条 ≤200 | stable_summary，用“；”连接 |
| play_style | pace/combat/exploration/puzzle/quest_focus 各有默认枚举；risk_tolerance 默认 0.5 | 各 ≤40；risk 转 float 并钳制 [0,1]；枚举不做闭集校验 | stable_summary 的自然语言映射 |
| preferences | likes/dislikes/fears 各 0..10 | 每条 ≤200 | stable_summary，用“、”连接 |
| speech_style | tone 必填；language 默认简体中文；catchphrases 0..8；max_sentence_chars 默认 60；silence_bias 仅 low/medium/high；avoid 0..8 | tone ≤200；句长钳制 [10,300] | stable_summary；silence_bias 另进 SpeechPolicy |
| long_term_interests | 0..12 条 | 每条 ≤200 | stable_summary |
| self_history | summary；milestones 0..20 | summary ≤600；每条 ≤300 | stable_summary |
| boundaries | no_spoilers；no_meta_ai_talk；no_game_memory_access | no_spoilers 显式 false 直接拒绝；后两者 `bool()` 强转、缺省 true | stable_summary 的“底线”行 |

默认档案 `profiles/lumi.json` 是“小光 Lumi”：4 条 traits、risk_tolerance=0.45、silence_bias=medium、3 条 values、4 条口头禅（`profiles/lumi.json:1-88`）。首次运行把它原样复制到 `<PLAYER_HOME>/profile/profile.json`，不是写“规范化后的 JSON”（`player_profile.py:503-520`），因此模板里的字段顺序与格式会原样保留，但也意味着模板必须是合法且能通过校验的档案。

安全侧写：`normalize_profile` 先递归扫描敏感键名（token/api_key/password/secret 及 `_token`、`_secret` 等后缀）和世界真相键名（world_only/ground_truth/world_database/spoiler），命中即 `ProfileError`（`player_profile.py:30-56, 88-116, 330-340`）。这是 **键名级** 防护，不是内容级：`self_description` 里手写剧透不会被发现。未知顶层键在返回值中被静默丢弃；可选的 preferences/self_history 段若拼错，不会报错，只会退化为默认值——这是可评审的缺口。`boundaries.no_spoilers` 只拒绝显式 `false`；`0`、`"false"`、`null` 都会被强制成 true（`player_profile.py:440-447`），方向安全但语义不一致。

【已实现且有测试】加载、默认值、拒密钥/拒 world 键名、版本/player_id/路径错误、BOM、播种幂等与坏模板都有 `tests/test_player_profile.py:32-178` 覆盖。【目标设计/未实现】profile schema 迁移、损坏档案备份/重建、热重载、内容级剧透过滤，以及“聚合长度上限”都不存在。

### 7.3 启动、播种与失败语义

启动顺序是刻意固定的：`resolve_config` 先算出 profile 路径和 `profile_is_default`（`player_config.py:256-264`）；`PLAYER_AGENT_ENABLED=0/--disabled` 直接返回 0，`PLAYER_AGENT_SHADOW=0` 返回 2，两者都发生在档案加载之前（`run_shadow.py:316-323`）；然后建目录、只在“默认路径且文件不存在”时播种、再 `load_profile`（`run_shadow.py:325-330`）。自定义路径缺失时 **不播种、不回退默认人格**，留给 `load_profile` 抛 `ProfileError`，进程返回 2。

这个选择值得展开：如果自定义路径写错就静默使用默认人格，评审者会看到“程序正常启动”，但玩家身份已经换了，且所有 resume/state 仍指向原 player_id，产生最危险的一类静默错配。因此宁可启动失败。对应测试 `tests/test_run_shadow.py:84-129` 覆盖 disabled、shadow=0、缺失自定义 profile、默认路径播种。

失败模式：
- 档案损坏或含敏感键：启动返回 2，不进入 shadow。没有“备份坏档案后重建”的路径，因为重建意味着自动生成一套人格，比失败更不可审计。
- 播种时 `atomic_write_text` 失败（权限/磁盘满）：`OSError` 由 `main` 统一返回 2；目标已存在时 `seed_default_profile` 返回 False 且不改字节，绝不覆盖。
- 启动成功后 profile 不再重读；`live.started`、`live.stopped`、WS 重连只清观察/动作/指导，不重读 profile（`player_agent.py:316-325, 440-466`）。热重载被有意放弃：换人格必须重启，保证一场 episode 内 identity 不变。

### 7.4 stable_summary：确定性与预算冲突

`stable_summary()` 是纯函数，顺序固定为 identity → personality → values → play_style → preferences → speech_style → interests → self_history → boundaries；不输出 `updated_at`、路径或风险原始小数，但会输出 `max_sentence_chars` 数字，自由文本中的换行原样保留（`player_profile.py:224-322`）。默认 lumi 的摘要实测为 642 字符/17 行。

这个 642 很关键：identity 块默认预算 600，`ContextBuilder._block` 对 required 块只记录 `over_budget=True`、**绝不截断**（`player_context_builder.py:291-296, 439-454`）。也就是说，默认档案一启动就超出自己的块预算，但不会报错；块预算对 required 只是观测值，不是约束。更大的风险是：profile 的每个字段都有上限，但缺少聚合上限；一个 schema 完全合法的长档案可以把 identity 撑到约 2.4 万字符。identity 被拼进 system（`player_context_builder.py:398`），而 agent 在 `total_chars > 6600` 时整轮不调用 LLM（`player_agent.py:649-652`），于是“档案合法”会变成“每轮都记 shadow_context_error、永不思考”。这是本章最值得主编追问的失败模式：应在加载时做聚合预算校验，或为 identity 提供可审计的截断策略，而不是让运行期静默停摆。

另一个细节：`speech_style.silence_bias` 直接进入 `SpeechPolicy.decide`（`player_agent.py:695-700`），但策略只对 `high` 生效；`low` 与 `medium` 在策略层没有行为差异，只在 stable_summary 的文案里不同（`player_speech_policy.py:30-53`）。如果评审期待“档案里的沉默倾向被完整执行”，这里应标为部分实现。

### 7.5 持久化版图与写入时机

| 文件 | 写入者/时机 | 一致性机制 | 损坏/版本策略 |
|---|---|---|---|
| `profile/profile.json` | 仅首次播种；运行时只读 | `atomic_write_text`，已存在不覆盖 | 坏档案启动失败；无备份重建 |
| `state/resume_state.json` | 启动、每 30 秒、close、live.stopped | 原子替换；保存前剥离敏感键；player_id 归属校验 | 损坏备份后重建空状态；版本过新拒绝；旧版本迁移前留 `.vN.bak`（`player_state.py:103-273`） |
| `state/working.json` | 每次 consolidation、会话 reset | 原子写 + `file_lock`（可能 fail-open） | 损坏备份；未来版本 load 抛错、save 拒绝覆盖（`player_working_memory.py:210-294`） |
| `memory/episodes/*.json` | 满足 episode 阈值时 | 每文件原子写；无跨进程锁 | 坏文件/未来版本 warning 跳过，不备份（`player_episodic_memory.py:223-304`） |
| `memory/semantic/facts.json` | 仅 OCR unlock 路径 | 全程 `file_lock`；坏条目计数后拒绝覆盖 | 损坏备份；未来版本运行期被吞掉（`player_semantic_memory.py:164-305`） |
| `state/session_recap.json` | 仅 close | 原子写，无锁、无字段级校验 | 损坏备份后返回 None；版本过新抛错（`player_recap.py:141-198`） |
| `memory/evidence/<sha256>.json` | 每次 OCR observe | 内容哈希命名；同 id 内容不一致拒绝 | 不可变；无 GC/保留期（`player_knowledge.py:39-92`） |
| `memory/quarantine/<sha256>.json` | 手动 CLI | 按原始字节哈希幂等 | 只复制，不进入召回（`player_legacy_import.py:19-46`） |
| `state/shadow/<session>.jsonl` | 每 tick/事件追加 | 行级 JSON；仅 close 的 shadow_end fsync | 半行无启动修复；多进程无锁（`shadow_recorder.py:159-197`） |

【已实现且有测试】上述存储的 roundtrip、上限、损坏、未来版本、player_id 归属、原子写与锁都有单测；【已实现但未实机验证】真实 Windows 文件共享冲突、磁盘满、断电窗口没有实测。

`shadow_recorder.py:133-157` 还强制了每条记录的公共信封：`schema_version`、`type`、`mode=shadow`、`executed=false`、`sent=false`、`seq`、`run_id`、`player_id`、`session_id`；调用方传入同名字段会被静默丢弃。因此记录本身能区分“记录”与“发送”，这也是 shadow 证据可信的原因之一。但 `read_shadow_records` 目前只有测试调用，没有生产消费者；记录类型也没有枚举注册表或 schema 迁移逻辑，未来离线分析工具只能自行猜字段。

### 7.6 失败模式、并发与回滚

启动期与运行期的语义并不一致：state/working/recap 的未来版本在启动时抛错并由 `run_shadow.py:437-449` 返回 2；episodic 的未来版本在 `list()` 里被跳过，表现为“旧 episode 突然全部不可见”；semantic 的未来版本在 agent 的 `_retrieve_memories`/`_unlock_ocr_quotes` 被捕获，表现为“语义检索为空、事实写不进”，而不是启动失败。README 笼统写成“只降级”，评审时应要求区分。

并发假设是“同一 state/memory 目录只有一个 shadow 进程”，但代码不强制：episodic/recap/evidence 无锁；working/semantic 的 `file_lock` 在无法创建锁文件时退化为无锁，5 秒超时后还会强行接管（`player_io.py:158-200`）。多进程同 session 会重复写 episode、丢 working 更新，或产生两个从 1 开始的 seq。

损坏恢复是分文件的：working/semantic/state/recap 损坏会备份；episodic 损坏只跳过、不备份且 `count()` 仍统计它；`working.save` 读取现有文件时若 JSON 损坏，`ValueError` 未转换成 `WorkingMemoryError`，close 路径可能逃逸（`player_working_memory.py:227-294`）。没有跨文件事务：consolidator 先写 working，再写 episode/状态；episode 失败不会回滚 working。

重连/换场：`reset_observation` 与 `live.stopped` 会清空 working 的 location/characters/dialogue/recent_events/current_activity/why/emotion/last_motor_feedback，只保留 open_questions 与 short_plan（`player_working_memory.py:183-194`）。如果随后 close，recap 拿到的是清空后的 working，`emotional_aftertaste` 与 `current_activity` 可能丢失；现有测试只覆盖正常 close。

回滚开关：`PLAYER_AGENT_ENABLED=0/--disabled` 退出 0；`PLAYER_AGENT_SHADOW=0` 拒绝启动（不是 active 模式）；`CONTEXT_BUILDER=legacy` 只回退 prompt，记忆写入继续；`--no-llm` 只写 shadow_observation；不传 `--observe-ocr` 则不产生新证据/事实；不传 `--intent-url` 则不发意图；删除 `PLAYER_HOME` 或 resume 可重建/清空，但 profile 与 quarantine 不会自动清理。

### 7.7 替代方案与结论

除 7.1 的两个方案外，还可考虑“把 profile 做成每轮从数据库读的可变记录”。未采用的原因是可复现性：同一份 prompt 必须能由同一份文件、同一段代码完全重建；数据库行没有 diff、没有 code review、没有“这次启动用的是哪版人格”的证据。当前设计的代价也很明确：不支持运行中换人格、不支持自动迁移、聚合长度不受控。评审应把这三条作为已知边界，而不是缺陷清单里的意外。

## 8. 记忆系统与上下文构造

shadow 玩家的连续性不是“把整场 transcript 塞回 prompt”，而是四层不同可信度的记忆 + 一个按预算组装的上下文构造器。实现集中在 `integrations/player/player_working_memory.py`、`player_episodic_memory.py`、`player_semantic_memory.py`、`player_recap.py`、`player_retrieval.py`、`player_memory_consolidator.py`、`player_context_builder.py` 与 `player_knowledge.py`。整体状态：【已实现且有测试】数据结构和假数据集成路径；【已实现但未实机验证】真实 BetterGI OCR、真实 LLM 提案质量与长局连续性；【目标设计/未实现】自动遗忘、自传体记忆、OpenViking 玩家召回与旧导演 memory_patch 双写。

### 8.1 分层与“真相等级”

四层不是按时间分，而是按“这句话是谁说的、能不能验证”分：

```text
working  ← 本轮观察 + 模型自述，标签“此前影子推测（未验证）”
episode  ← 模型自述的一段经历，标签“影子模型的未验证记录”
semantic ← 仅来自当帧截图 OCR 的逐字引文，标签“屏幕OCR（未校对）”
recap    ← close 时由上述结构确定性派生的下一场开场摘要
```

这四层的共同硬约束是 `visibility == player_knowledge`：episode/fact 在 normalize 时缺失或为 `world_only/ground_truth/world_database` 直接抛错（`player_episodic_memory.py:115-120`、`player_semantic_memory.py:97-101`），store.list 再跳过，retriever 与 ContextBuilder 再过滤（`player_retrieval.py:181-200`、`player_context_builder.py:283-289`）。系统不保存原始截图、不保存整场 transcript、不读写游戏内存或存档；所有记忆是本地 JSON，单写者假设，无向量索引。

### 8.2 数据结构、上限与写入时机

| 产物 | 关键结构与上限 | 写入时机 | 备注 |
|---|---|---|---|
| `state/working.json` | location/characters(12)/dialogue(20)/recent_events(20)/current_activity/why/open_questions(10)/emotion(12)/short_plan(8)/last_motor_feedback；文本 clean 后截断 | 每轮 consolidation 保存；会话边界 reset | reset 只清会话级字段，保留 open_questions 与 short_plan（`player_working_memory.py:183-194`） |
| `memory/episodes/<event_id>.json` | schema v1；强制 what_happened、what_i_knew_before、what_i_thought、what_i_felt；characters、importance、source_session 等有默认值 | consolidation 满足条件时 | fingerprint=sha1(session+location+what_happened+thought)，默认 600 秒窗口去重；event_id 冲突拒绝覆盖（`player_episodic_memory.py:94-136, 233-251`） |
| `memory/semantic/facts.json` | proposition、source、visibility=player_knowledge 必填；evidence_id/quote/confidence/tags/spoiler_risk 可选；按 proposition.lower() 去重并合并 confidence/source/tags | 仅 `agent._unlock_ocr_quotes` 成功时 | 存储层不强制 evidence；source 合并上限 4；一个坏条目会让后续 add 拒绝写入直到手工修复（`player_semantic_memory.py:83-136, 215-305`） |
| `state/session_recap.json` | last_location/current_quest/what_happened(3)/people(10)/learned(4)/unresolved(8)/emotional_aftertaste/last_thought/next_intentions(4)/episode_count | 仅 close | 取 top3 episode、top8 的 people/unresolved、top4 fact；emotional_aftertaste 与 episode_count 存储但不渲染（`player_recap.py:49-138`） |
| belief/relationship/emotion/thread | 各自 schema；proposal 触发 apply | 每轮 consolidation 末尾 | 每个 store 单独 try/except，失败只写 `*_error`；摘要各取 4–5 条进 ContextBuilder |
| `memory/evidence/<sha256>.json` | session/frame/frame_sha256/observed_at_ms/text/confidence/evidence_id | 每次 OCR observe | 只存 OCR 摘要，不存图片；无 GC（`player_knowledge.py:39-92`） |

一次 tick 的写入顺序是：检索 → 组 prompt → LLM → `parse_proposal` → `_apply_proposal` 改 resume（内存态）→ speech/intent → `consolidate()` 写 working/episode/四类状态 → `_unlock_ocr_quotes` 写 semantic → 写 `shadow_tick` → 节流保存 resume（`player_agent.py:640-724`）。注意 `_apply_proposal` 在 consolidation 之前，所以 consolidation 看到的 state 已包含本轮 thought/intention/scene/quest。

### 8.3 检索：轻量分词与多因子打分

`tokenize` 取 ASCII 2 字符以上词与中文二元组；语义分 `0.7 * query coverage + 0.3 * jaccard`（`player_retrieval.py:65-86`）。最终分数是加权和：

| 因子 | 权重 | episode 取值 | fact 取值 |
|---|---|---|---|
| semantic | 0.30 | 查询与文本 token 覆盖率 | 同左 |
| importance | 0.20 | episode.importance | fact.confidence |
| emotional | 0.15 | emotional_intensity | 无 |
| character | 0.10 | 角色名出现在 query 或 active_characters | 同左，允许 proposition 包含角色名 |
| thread | 0.15 | 与 open thread token 有交集即 1 | 同左 |
| recency | 0.10 | 7 天半衰期 | 同左，用 learned_ms |

episode 与 fact 分别遍历 `store.list()`；语义、角色、线索三项全为 0 的条目不进入候选。之后按 `memory_id`/text 去重，按 score、time_ms、id 倒序，limit 默认 8、max_chars 默认 2400（`player_retrieval.py:159-202, 357-382`）。`limit` 与 `max_chars` 是软限制：第一条即使超 max_chars 也会保留，后续条目放不下就跳过。检索 query 由 guidance、location、current_activity、current_intention、current_quest、last_location、characters、最近 commentary 拼成，截 500 字（`player_retrieval.py:100-132`）。

外部检索默认禁用：`retrieve_async` 只记录“外部记忆来源尚未验证，已跳过外部检索”，没有 OpenViking 适配器（`player_retrieval.py:204-226`）。证据门在检索层的实际代码是 `if self.knowledge_gate is None or not self.knowledge_gate.permits(fact): continue`（`player_retrieval.py:194`），即 **未注入 gate 时直接跳过所有 fact，属于 fail-closed**。这一点与审计摘要中“gate 为 None 会放行”的说法相反，评审应以代码为准。

### 8.4 Consolidation：确定性的结构化写入

`MemoryConsolidator.consolidate()` 先对 proposal/snapshot/state 做 `_sanitize`，再更新 working 并保存；working 保存失败只置 `result.error`，不阻断后续。episode 的生成条件是 thought、what_happened、felt 三者非空，importance ≥ 0.5，且距上次 episode ≥60 秒或 importance ≥0.75（`player_memory_consolidator.py:212-217`）。importance 公式为 `0.25 + 0.20(what_happened) + 0.15(learned) + 0.10(characters) + 0.15*max(felt) + 0.05(unresolved)`，`note.importance` 可显式覆盖（`player_memory_consolidator.py:207-210` 及 `_importance`）。

关键实现事实：`learned` 被硬编码为空串（`player_memory_consolidator.py:184-185`），`result.facts` 恒为 `[]`，`_normalize_learned_facts` 是死代码；semantic fact 只走 OCR 解锁路径。这不是“忘了写”，而是当前阶段的证据门策略：模型自述不能自动晋升为事实。代价是 `tests/test_player_memory_stores.py:442-451` 与 `test_player_agent.py:679-712` 的测试名仍叫 “creates_episode_and_facts”“writes_episode_facts”，断言却明确 `facts_created==0`、semantic 为空，容易误导评审。四类状态 store 各自 try/except；没有跨文件事务，working 已写而 episode/状态失败时不回滚。

### 8.5 ContextBuilder：分块预算与超预算行为

v2 构造器按固定顺序组装 15 个块：identity、perception、working、intention、resume、recap、episodic、semantic、beliefs、relationships、emotion、threads、dialogue、commentary、output（`player_context_builder.py:43-59`）。预算按字符而非 tokenizer，理由是中文约 1 字≈1 token、ASCII 也按 1 计，整体偏保守且不引入模型依赖（`player_context_builder.py:3-7`）。

默认块预算：identity 600、perception 500、working 800、intention 250、resume 420、episodic 1100、semantic 650、beliefs 400、relationships 450、emotion 150、threads 400、dialogue 650、commentary 650、output 1300；recap 不在表里，fallback 为 400（`player_context_builder.py:27-42, 437`）。这些预算相加约 8.7k，已经超过全局 `DEFAULT_MAX_TOTAL_CHARS=6600`，所以真正约束的是总预算，块预算只是第一道裁剪。

required 只有 identity、perception、output；required 块永不截断，只记录 `over_budget`；optional 块用 `_fit_lines` 逐行装入，超限时加“还有 N 条”或省略号（`player_context_builder.py:439-467`）。总预算超限时按 priority 从低到高丢弃非 required 块：episodic 20、semantic 30、beliefs/relationships/threads 35、commentary/emotion 40、working 60、recap 65、resume 70、intention 80；如果 required 仍超 6600，agent 记录 `shadow_context_error` 并整轮不调用 LLM（`player_agent.py:649-652`）。默认 lumi 的 identity 642 > 600，但只是 `over_budget=True`、`truncated=False`；真正危险的是聚合超长档案（见 7.4）。

块标题本身承担“真相标注”：working 叫“此前影子推测（未验证）”，semantic 叫“过往屏幕 OCR 记录（未校对）”，resume 叫“上次影子提案（未验证）”。recap 不是 required，可被总预算丢弃，这与 `PHASE_4_MEMORY_RESUME.md` 称其为必选块不一致。legacy 模式直接调用 `build_shadow_prompt`，只含 stable_summary + 观察 + resume + 输出协议，不注入 working/episodic/semantic/recap，也没有总预算守卫；ContextBuilder 抛异常时 agent 回退 legacy（`player_agent.py:809-836`）。

### 8.6 OCR 证据门与 world_only 隔离

证据链是本节最值得细看的部分：

```text
ScreenObserver.capture() ──POST /api/v1/agent/observe──> 同一帧 bytes + OCR 行
        │
        ▼
EvidenceStore.record_screen_ocr(): text≤500、脱敏不变、frame_sha256
        │ evidence_id = sha256(规范化 payload)
        ▼
模型提案 learned_facts[{evidence_id, quote}]
        │
        ▼
agent._unlock_ocr_quotes(): evidence_id 必须在本轮 visual.ocr 白名单
        │
        ▼
KnowledgeGate.unlock_quote(): session/frame 一致、frame_sha256 一致、
quote 4..200 且为 evidence.text 逐字子串、observed_at 在 180 秒内
        │
        ▼
SemanticStore.add(proposition="屏幕OCR（未校对）："+quote, source=screen_ocr:<id>)
```

`EvidenceStore.get` 会重新计算证据文件哈希，任何篡改返回 None（`player_knowledge.py:77-92`）。`permits()` 在召回时校验 visibility、证据存在、quote 逐字、source 与 proposition 精确匹配（`player_knowledge.py:132-146`），但 **不重新校验过期时间、session、帧号**；也就是说“写入时必须是当帧 180 秒内”成立，“召回时证据已过期就拒绝”不成立，README/PHASE_2 的措辞过强。

world_only 的隔离是四层防御：episode/fact 的 normalize 拒绝非 `player_knowledge`（fail-closed）；store.list 跳过；retriever 再做等值过滤；ContextBuilder 进入前再过滤一次。注意 `_FORBIDDEN_VISIBILITY` 集合（`player_retrieval.py:32`）实际上未被引用，真正生效的是等值判断，行为等价但实现点与文档不同。ContextBuilder 不调用 `knowledge_gate`，只信 retriever 的结果；`SemanticStore.add` 也不强制证据——这两点是“调用方约定”而非存储层不变量，直接调用 builder 或 store 的内部代码可以绕过证据门。当前生产路径由 `run_shadow.py:357-364` 同时注入 gate 与 retriever，暂时没有绕过路径，但评审应把它视为可被未来代码破坏的边界。

### 8.7 旧导演 OpenViking 记忆：为什么隔离、不召回

仓库里存在两套完全不同的记忆：玩家侧本地 JSON，以及 Lumine 导演的 OpenViking 长期记忆桥（`integrations/lumine/memory_bridge.py`、`memory_ingest.py`）。后者通过 `openviking-sdk` 访问默认 `127.0.0.1:1933`，把 lore 放在 `viking://~/resources/genshin`、每场 60 秒滚动摘要放在 `viking://~/memories/genshin/live/`，每 4 秒 tick 检索最多 4 条、1800 字注入导演提示词（`memory_bridge.py:39-72, 140-195`）。它是导演的记忆，不是玩家的记忆。

玩家侧明确不接线：`run_shadow.py:358-364` 在 `PLAYER_OPENVIKING` 被设置时只打印“OpenViking 玩家知识来源尚未隔离；已禁用外部召回，使用本地记忆”，`memory_bridge` 恒为 None；`MemoryRetriever.external` 从未被调用，测试还明确断言不存在 OpenViking 适配器。旧导演的 `memory.json` 只能通过 `player_legacy_import.py` 复制到 `memory/quarantine/<sha256>.json`，记录固定 `visibility=quarantined`、`verification=unverified`，按原始字节哈希幂等，超过 1 MiB 拒绝（`player_legacy_import.py:19-46`）。EpisodicStore/SemanticStore/RecapStore/Retriever 都不读 quarantine 目录，所以它既不进检索，也不进 prompt。

隔离的理由有三条：其一，lore 库本身含 `world_only` 内容，玩家召回会把未经历剧情/世界真相带进 prompt（`player_retrieval.py:3-8` 明确写了这一点）；其二，旧导演记忆是“全知旁白视角”，不是玩家“看到/听到/想过”的经历，语义上不能等价；其三，OpenViking 检索无法逐条证明 `visibility` 与来源，绕过了本地 store 的 fail-closed 校验。替代方案是“把 OpenViking 当作玩家长期后端”，未采用，直到能按 URI 隔离 world/player 且每条命中带 provenance。

这并不表示导演桥本身可信：审计显示运行期 `available` 不会因失败回退、也没有重连；最坏一次 recall 约 52 秒（12 秒 search + 4×10 秒 read）阻塞 4 秒 tick；写入没有超时；`wait=True` 对 memories 命名空间并不保证向量刷新完成；每次启动写 `<session>-<run_id>.md` 且永不清理。这些缺陷也是“不把它提升为玩家记忆后端”的现实理由。

### 8.8 失败模式、并发、重连与回滚

- 检索失败：agent 捕获后返回空结果，本轮继续（`player_agent.py:770-774`）。
- LLM/解析失败：不写 episode/fact，只写 shadow_llm_error / shadow_parse_error（`player_agent.py:653-684`）。
- working 保存失败：只置 `result.error`，episode 与状态仍继续；episode 失败则 working 已写、无回滚。
- semantic 坏条目：`load` 计数 `invalid_items`，之后 `add` 拒绝覆盖原文件，必须手工修复；未来版本在运行期表现为检索为空/写入跳过。
- 并发：episodic/recap/evidence 无锁；working/semantic 的 `file_lock` 可 fail-open，5 秒超时强行接管；没有 process-level 单例。
- 重连/换场：`live.stopped`、hello session 变化、帧序号回退、WS 重连都会 reset working；随后 close 生成的 recap 可能丢失 emotion/current_activity（现有测试未覆盖）。
- 性能：每 tick 全量读 episodes、全量读 facts，且每个 fact 再读 evidence 文件；没有索引/缓存/轮转，长局线性变慢。
- 回滚开关：`CONTEXT_BUILDER=legacy` 只改变 prompt，不停止记忆写入；`--no-llm` 只写 shadow_observation；关闭 `--observe-ocr` 不再产生证据/事实；删除 `memory/` 或 `PLAYER_HOME` 可清空，但没有单条事实删除/过期 API。

【目标设计/未实现】自动遗忘/衰减（GAP_ANALYSIS 的 G21，只做软降权/摘要，不物理删除）、自传体记忆 `My Journey So Far`（G22）、把一段对话整理成 conversation event、证据 GC/保留期、episode schema 迁移、semantic fact 过期/删除、OpenViking 玩家召回与旧导演 memory_patch 双写（`MIGRATION_PLAN.md:161-173` 明确“尚未实现”）。

### 8.9 替代方案与取舍

1. 向量库/embedding 检索：未采用。当前用字符 n-gram + 多因子打分，零依赖、确定性、可手算、可审计；代价是语义召回弱、长局全量扫描。未来若要上向量库，应保持 `RetrievedMemory` 接口与 provenance 字段不变，而不是替换 store。
2. 整场 transcript 摘要：未采用。让 LLM 每场总结会把不可验证的叙述变成“记忆”，且体积/隐私不可控；当前选择结构化 episode + 确定性 recap，模型只提供字段，不提供“总结事实”。
3. LLM 自动抽取 semantic fact：未采用。模型推理与屏幕事实不可区分，因此 semantic 层被刻意收窄为 OCR 逐字引文；代价是知识获取很慢，这也是 conversation event 仍是缺口的原因之一。
4. 复用 OpenViking 作为玩家后端：暂不采用。需要先解决 world_only 隔离、逐条 visibility/provenance、运行期可用性与写回语义；否则会把导演的“全知记忆”泄漏成玩家的“已知事实”。

## 9. 认知状态：信念、关系、情绪、线索

这一层不是知识库，而是“玩家对自己的叙事”。四类状态各自落在独立 JSON 文件里，由模型每轮提案中的 `beliefs` / `relationships` / `emotion` / `threads` 驱动更新，再以自然语言摘要进入 v2 ContextBuilder。它解决的是“同一个玩家跨轮、跨场仍有连续的猜测、态度、情绪和未解线索”，而不是“记住世界真相”。实现状态需要分开看：Store、schema、摘要、预算和单元测试都在；真实 LLM、真实 OCR 与长会话没有验证；关系身份、部分更新和时间戳三个缺陷没有被现有测试覆盖。以下引用的 py 文件除特别注明外均位于 `integrations/player/`，`render_session.py` 位于 `integrations/session/`。

### 9.1 数据流与职责边界

```
Live/OCR ──> tick ──> LLM proposal(JSON)
                         ├─ beliefs[] ─────> BeliefStore ─────┐
                         ├─ relationships[] > RelationshipStore┤
                         ├─ emotion{} ─────> EmotionStore ─────┤─> summary() ─> ContextBuilder extras ─> 下一轮 prompt
                         └─ threads[] ─────> ThreadStore ──────┘
                                              state/*.json（跨会话保留）
```

装配点在 `run_shadow.py:330-432`：四个 Store 以同一组实例注入 MemoryConsolidator 与 PersistentPlayerAgent。tick 的顺序是 OCR → 检索 → ContextBuilder → LLM → parse → `_apply_proposal` → 读取 `emotion.view()` → SpeechPolicy → 发布 intent → `_consolidate_proposal` → 写 shadow_tick（`player_agent.py:523-725`）。因此本轮提案写入的状态，要到下一轮才进入 prompt；本轮 SpeechPolicy 读到的情绪也是更新前的衰减值，而不是本轮提案里的新情绪。这个“一拍延迟”不是 bug，但评审时必须知道：模型看到的是上一轮结束时的自己。

`MemoryConsolidator._apply_state_updates` 固定按 beliefs → relationships → emotion → threads 顺序调用，每个 Store 单独 try/except，失败只写 `belief_error` 等字段，不影响其它 Store（`player_memory_consolidator.py:261-290`）。四个 Store 与会话级状态不同：`_reset_session` 只清 dialogue、SpeechPolicy 冷却和 working，不清四类长期 Store（`player_agent.py:440-463`）。因此“重开一场”不会清空玩家对安柏的信任，也不会清空未解线索。

需要强调一个边界：四类状态只来自模型提案，不像 semantic fact 那样要求 OCR provenance（逐字证据门）。`known_fact` 是模型自封的“我确定”，不是“有证据”。这也是它与 KnowledgeGate 的根本区别。

### 9.2 Schema 与状态机

| 状态 | 文件与主键 | 取值/转移 | 单轮限幅 | prompt 摘要 |
|---|---|---|---|---|
| 信念 | `state/belief_state.json`，`fingerprint=sha1(proposition.lower())[:20]` | `known_fact/belief/hypothesis/suspicion/unknown/disproven`；无转移矩阵 | 每次最多 8 条；confidence 0..1 | 最多 5 条，按状态换措辞 |
| 关系 | `state/relationships.json`，`character_id` 由 ASCII 白名单生成 | 六维连续量：familiarity/trust/affection/suspicion/respect/concern | 已存在角色每维单步 ≤0.15；新角色不限 | 最多 5 个角色，只出自然语言 |
| 情绪 | `state/emotion_state.json`，九维平铺 | joy/sadness/fear/anger/curiosity/confusion/excitement/nostalgia/tension | 每维单次 ≤0.35；半衰期 20 分钟 | 取 ≥0.25 的前 4 个中文词 |
| 线索 | `state/narrative_threads.json`，`fingerprint=sha1(title.lower())[:20]` | `open/dormant/resolved/abandoned` | 每次最多 8 条；importance 0..1 | open/dormant 的标题与理论 |

以 belief 为例，落盘记录是：

```json
{"belief_id": "b-3f2a...", "proposition": "西风骑士团在隐瞒什么",
 "status": "hypothesis", "confidence": 0.4, "evidence": ["安柏欲言又止"],
 "contradictions": [], "history": [], "first_formed_ms": 1730000000000,
 "last_updated_ms": 1730000600000, "fingerprint": "3f2a..."}
```

关系记录把六维平铺在 `character` / `character_id` 之后，另有 `memorable_events[≤12]`、`impressions[≤10]`、`unresolved[≤8]`；但 `impressions` / `unresolved` 在模型路径上永远不会被填充，因为 proposal 解析只传 character、六维和 memorable_events（`player_proposal.py:326-346`）。情绪则是九维平铺在 JSON 顶层，没有嵌套的 `emotions` 对象（`player_emotion.py:42-48`）。

信念的“状态机”其实是名义上的：`normalize_belief` 只校验枚举，`apply` 对已存在记录直接覆盖 `status` 与 `confidence`（`player_beliefs.py:145-160`）。只有转入 `disproven` 时，才把旧状态追加到 `history` 并合并 `contradictions`；反向从 `disproven` 回到 `hypothesis` 不留任何翻转记录。模块 docstring 说“证伪保留历史，不删除旧信念”，字面上成立，语义上弱于“不可逆”。`ground_truth` / `world_only` 在提案侧和 Store 侧都被拒绝（`player_beliefs.py:19-20,66-68`；`player_proposal.py:310-314`），这是硬边界。

关系层有三个必须点名的问题。第一，`_character_id` 用 `[^A-Za-z0-9._-]+` 替换非 ASCII 字符（`player_relationships.py:21,57-58`），任何纯中文角色名都会变成 `unknown`；“安柏”和“琴”会命中同一条记录，第二个角色的更新会改写第一个，摘要里的名字仍是第一个。第二，`normalize_relationship` 把未提供的维度补成 0.0（`player_relationships.py:67-69`），而 `apply` 把输入当绝对目标值；模型只提交 `trust` 时，`affection` 等维度每轮被向下拉最多 0.15。审计记录中的实测是 0.9 → 0.75。第三，新角色首次写入不走 0.15 限幅（`player_relationships.py:163-166`），同一提案内重复出现同一 `character_id` 还会各算一次限幅，单轮最大可变化 0.9。现有测试只覆盖“单角色 trust 限幅”（`tests/test_player_states.py:50-59`），这三条都没有测试。

情绪是连续量而非每轮标签：`view()` 只按 `0.5 ** (elapsed/20min)` 计算衰减、不写盘；`apply()` 先衰减再按 ±0.35 强化并写回（`player_emotion.py:51-53,88-114`）。`apply({'happy':0.9})` 会静默忽略未知维度，却仍更新时间戳并写盘（`player_emotion.py:104-113`），于是“状态文件变了，但九维全是 0”。这不是崩溃，是静默语义漂移。

线索层同样没有转移约束：`normalize_thread` 对缺失 `status` 默认 `open`、缺失 `importance` 默认 0.5（`player_threads.py:53-58`），`apply` 又用这两个默认值覆盖旧记录（`player_threads.py:131-132`）。模型只提一句标题，就可能把已 `resolved` 的线索重新打开并重置重要性。`activate()` 本应让被检索命中的 `dormant` 线索复活，但生产代码没有任何调用点，只有测试调用（`player_threads.py:146-161`；全仓 grep 结果），所以它是死代码，长期伏笔只能靠模型显式重开。

### 9.3 限幅、半衰期与自然语言摘要

限幅的目的不是“精确建模”，而是把不可信的模型输出挡在单轮冲击之外：关系 0.15、情绪 0.35、每次最多 8 条，都是“一次剧情不能把角色拉满、一句话不能把情绪顶满”的工程护栏。但关系限幅只对已存在角色生效，新角色首次写入不受限；线索则完全没有 importance 限幅，新旧记录都直接覆盖。这是两处刻意或默认的例外，也是文档没有讲清的口径差异。

进入 prompt 的摘要刻意不暴露数值。以默认 Lumi 为例，实际块文本形如：

```
【我的信念与猜测】
- 我有点怀疑「西风骑士团在隐瞒什么」。
- 我曾经以为「遗迹守卫是哑巴」，后来发现猜错了。
【我对角色的看法】
- 对安柏：我比较信任 TA；我挺喜欢和 TA 相处。
【当前情绪】
- 开心、好奇
【还惦记的线索】
- 我还惦记着：失踪的商队（我猜：和深渊教团有关）
```

这些字符串由各 Store 的 `summary()` 生成（`player_beliefs.py:180-191`；`player_relationships.py:82-103,207-211`；`player_emotion.py:116-134`；`player_threads.py:180-188`），阈值分别是关系 0.3/0.5/0.6、情绪 0.25、摘要条数 5/5/4/5。块标题与预算在 `player_context_builder.py:27-76`：beliefs 400、relationships 450、emotion 150、threads 400 字符；总预算 6600。总预算超限时按优先级从低到高丢块：episodic 20 → semantic 30 → beliefs/relationships/threads/dialogue 35 → commentary/emotion 40 → working 60 → recap 65 → resume 70 → intention 80；identity/perception/output 是 required，永不丢（`player_context_builder.py:78-92,405-416`）。`tests/test_player_context_builder.py:135-143` 断言 prompt 里不出现 0.82 这类裸分数。

为什么用自然语言而不是把 JSON 状态直接塞进 prompt？替代方案是“状态即结构化 JSON”，实现更简单、信息不丢失；未采用的原因有三点：一是 token 成本与字符预算不可控，模型可能逐字段复述；二是裸数值会制造虚假精度，模型容易把 0.62 当成系统结论；三是自然语言摘要更接近“玩家会怎么想”，也便于人工审查。代价同样明确：confidence、importance、familiarity 都不进 prompt；情绪 0.24 直接消失；线索 importance 0.9 与 0.1 在摘要里没有区别；关系只讲 trust/affection/suspicion/respect/concern，familiarity 永远不被表达。这是“可读性优先于保真度”的取舍，评审应确认它是否可接受。

### 9.4 检索加权：状态实际影响了什么

这里需要纠正一个常见误解：四类状态不是检索文档。`MemoryRetriever` 只遍历 EpisodicStore 与 SemanticStore（`player_retrieval.py:181-201`），belief/relationship/emotion/thread 不会作为候选记忆被打分。检索公式是：

```
episode: 0.30*semantic + 0.20*importance + 0.15*emotional
       + 0.10*character + 0.15*thread + 0.10*recency(7天半衰期)
fact:    0.30*semantic + 0.20*confidence + 0.10*character
       + 0.15*thread + 0.10*recency
```

（`player_retrieval.py:24-31,228-265,292-317`）其中 `active_characters` 来自 working.characters，`active_threads` 只取 `thread_store.list(status="open", limit=6)` 的标题（`player_agent.py:740-749`）。所以线索层确实影响了检索：open 线索的标题参与 `thread` 二元匹配；但 dormant 线索不会，因为检索只看 open。关系层与信念层不进入 query，也不进入打分；它们只通过 prompt 摘要影响模型，不通过检索影响“想起哪段经历”。

`thread` 与 `character` 都是二元命中：一个中文二元组重合就给满 0.15，一个角色名命中就给满 0.10（`player_retrieval.py:333-355`）。这会带来噪声——“任务”“对话”这类通用词就能触发线索加权。因此“认知状态驱动的检索”目前只能算部分成立：能驱动检索的是 open 线索标题和 working 角色名，不是完整的信念/关系/情绪状态。把四类状态整体纳入检索加权，属于【目标设计/未实现】。

另一个常被忽略的边界是 recap：`build_recap` 只取 episode、semantic fact、working 的 emotion/short_plan、unresolved questions 与 resume 字段（`player_recap.py:56-110`），不读四个 Store。四类状态跨场连续靠的是各自 JSON 文件持续存在，而不是 recap 摘要。如果删除 `state/*.json` 但保留 recap，下一场仍有“上次回顾”，但信念、关系、情绪、线索全部归零。

### 9.5 失败模式、并发、损坏恢复与回滚

写入路径统一走 `atomic_write_json`（同目录临时文件 + fsync + os.replace），损坏 JSON 会备份为 `.corrupt-<UTC时间戳>` 后返回空状态，`schema_version > 1` 抛各自的 StoreError（BeliefError/RelationshipError 等）（`player_io.py:73-155`；各 Store 的 `load/_recover`）。这保证“文件不会被写坏”，但不保证“更新不丢失”：四个 Store 都是 load-modify-save，**没有 file_lock**；file_lock 只用在工作记忆与 semantic 存储（`player_io.py:158-201`）。README 的安全边界声称“跨进程加锁”，已知限制又说“同一目录只允许一个 shadow 进程，没有锁”（`integrations/player/README.md:219,237`），两处自相矛盾；以代码为准，当前是单写者假设，没有运行时强制。两个进程指向同一 state 目录会互相覆盖。

更隐蔽的是时间语义损坏：`BeliefStore.load()` 对每条记录调用 `normalize_belief(..., now_ms=self._clock())`，无条件把 `last_updated_ms` 设为当前时间（`player_beliefs.py:71,83,119`）；`ThreadStore.load()` 同样重置 `last_referenced_ms`（`player_threads.py:56,71,107`）。于是 `list()` 按新鲜度排序失去意义，`summary(limit)` 取到的“最近 5 条”取决于读盘顺序而不是真实先后。这不是崩溃，而是每次读盘都把历史刷新成“现在”，会让跨场连续性悄悄退化。

版本过新也不会在启动期 fail-fast：四个 Store 构造时不 `load()`，`schema_version > 1` 的错误发生在首个 tick 的 `summary()` 或 `apply()` 内，被 agent/consolidator 的宽泛 except 吞掉，表现为该状态静默停用并每轮告警。`run_shadow.py:433-444` 虽然捕获 BeliefError 等异常，但构造阶段根本不会触发它们（`player_agent.py:790-807`；`player_memory_consolidator.py:266-289`）。`_memory_extras` 用一个 try 包住五类摘要：若 relationships 抛异常，beliefs 已生成的条目仍在字典里，relationships 及其后的 emotion/threads/dialogue 被跳过；审计文字说“任一 Store 异常会让本次所有 extras 一起缺失”，与代码不完全一致，应为“部分缺失”。这一点建议主编在总评中修正。

跨 Store 没有事务：consolidator 先更新并立即保存 working，再依次写四类状态；关系写成功、线索写失败时，两类状态处于不同步版本，但都不影响 shadow_tick 记录。四类 Store 也没有总条数上限，只有单次更新条数和字段长度；每次 apply 全量重写，长期运行文件单调增长。替代方案是单个 `cognitive_state.json` 或 SQLite：前者能提供跨 Store 原子性，后者能提供事务与并发；未采用的原因是可手改、单 Store 损坏不拖累其它 Store、实现依赖最少，代价就是当前看到的无事务、无统一锁。

由于状态文本会原样进入下一轮 prompt，模型可以把任意命题、角色名、线索标题写进去，形成“自我提示注入”的持久化通道。`known_fact` 不需要证据，只有 semantic fact 需要 OCR 证据门。评审应把四类状态视为“可被模型污染的叙事缓存”，而不是“可信知识”。

回滚开关只有整机级别：不传 `--player-shadow-config`、`PLAYER_AGENT_ENABLED=0`、`--disabled`、`PLAYER_AGENT_SHADOW=0` 可关闭整个 shadow；`--no-llm` 只写观察、不写状态；`CONTEXT_BUILDER=legacy` 让四类摘要不进入 prompt，但 Store 仍会在后台写盘；删除对应 state 文件可重置，但没有单独的“关闭信念/关系/情绪/线索”开关。评审若要求精细降级，需要新增配置。

### 9.6 实现状态与评审结论

| 能力 | 状态 |
|---|---|
| 四类 Store 的 schema、单轮限幅、摘要、consolidator 失败隔离 | 【已实现且有测试】`tests/test_player_states.py:35-120` |
| 原子写、损坏备份、版本拒绝 | 【已实现且有测试（player_io）】但四 Store 在 agent 运行期的损坏/过新行为未覆盖 |
| ContextBuilder 分块、预算、总预算丢弃、无裸分数 | 【已实现且有测试】`tests/test_player_context_builder.py:88-194` |
| 四类摘要进入 extras 并影响 prompt | 【已实现但未实机验证】没有测试注入四 Store 后断言 prompt 含五块；agent 测试也不注入这些 Store |
| 关系中文名、部分更新、重复角色、信念时间戳 | 【已实现但未实机验证】且存在可复现缺陷，测试未覆盖 |
| 四类状态在真实 LLM 长会话中的稳定性 | 【已实现但未实机验证】 |
| 状态感知检索、recap 纳入四类状态、ThreadStore.activate 接线 | 【目标设计/未实现】 |
| 单 Store 级回滚/禁用 | 【目标设计/未实现】 |

结论：这一层的结构是清楚的——独立文件、独立失败、自然语言摘要、预算可控、证据门留给 semantic fact，符合“离线、可手改、可审查”的项目取向。但“同一个玩家”的承诺目前被三个高优先级缺陷削弱：纯中文角色名全部塌缩为 `unknown`、关系部分更新持续拉低未提及维度、读盘重置时间戳让新鲜度失效。三者都不是边界情况，而是《原神》中文角色名和正常模型输出下的常见路径。再加上对话锁（见第 10 节）可能误触发，当前实现更适合作为“可观察的认知脚手架”，不宜宣称已经具备稳定的角色关系与情绪连续性。

## 10. 对话感知与 Speech Policy

第 10 节讨论两个相邻但职责不同的机制：DialogueTracker 负责“屏幕上是否正在对话”，SpeechPolicy 负责“这一刻该不该开口、用什么 prosody 开口”。两者都在 shadow 进程内，默认不产生声音；唯一可能影响游戏的是可选的 intent 总线：对话锁期间会取消旧意图并向 MotorController（Lumine 动作执行器，代码中称 Motor）发布 wait hold。因此“shadow 零副作用”只在未启用 `--intent-url` 时成立。

### 10.1 设计目标与替代方案

BetterGI Live Hub 广播的帧没有对话结构，只有 UI 分类和 OCR 行；项目又禁止读内存、抓包、注入。因此 DialogueTracker 的设计目标只能是“用视觉信号做保守候选”：不推断说话人、不解析选项、不覆盖 NPC 原话（`player_dialogue.py:3-8`）。

替代方案 A 是只认 `ui_category == "talk"` / `is_in_talk_ui`。它误报最低，但当 BetterGI 的 UI 分类缺失或延迟时，对话会被漏掉。代码没有采用严格的 A，而是允许 OCR 文本单独触发；这降低了漏报，却把误报面扩大到整个屏幕。替代方案 B 是只在 UI 分类变化时事件触发截图，而不是固定轮询；observe 接口是请求/响应式、且有 5 秒最小间隔，轮询实现最简单，但 30 秒默认值与 4 秒锁并不匹配。于是当前实现同时继承了 A 的宽条件和 B 的时序缺陷：条件写成 `if in_talk or texts`，任何全屏 OCR 文本都能上锁，而锁的保持时间只有 4 秒、OCR 默认 30 秒一次。

### 10.2 判定逻辑与 hold 周期

核心代码只有十几行（`player_dialogue.py:39-78`）：

```python
in_talk = is_in_talk_ui or ui_category.lower() == "talk"
texts = [row.text for row in ocr[:8] if row.text]
if in_talk or texts:
    if not locked: started = True
    locked = True
    lock_until_ms = now + 4000
    append_recent(texts)          # deque(maxlen=8)，仅相邻去重
elif locked and now >= lock_until_ms:
    ended = True; locked = False
```

`recent` 只保留 8 条，`turns` 统计的是追加行数而不是真实轮次，`context_lines()` 只取最后 3 行并加“OCR，可能不完整”标签（`player_dialogue.py:16-17,64-67,93-97`）。状态不落盘；`live.started`、`hello` 会话变化、帧序号回退、`live.stopped`、WS 重连都会 `reset()`（`player_agent.py:440-481`）。只有在 `--observe-ocr` 且 `CONTEXT_BUILDER=v2` 时才会调用 observe（`player_agent.py:571-606`）；不启用 OCR 时对话锁永远不生效。

上锁/解锁在 agent 侧对应两件事（`player_agent.py:607-639`）：`started` 时先 `publisher.cancel()` 取消旧意图，再发布 `DIALOGUE_HOLD_GOAL` 的 wait 意图，`deadline_ms=60000`，目的是防止 Bridge 退回自由操作后点掉对话（`player_agent.py:42,621-623,959-1019`）；`ended` 时取消该 hold。锁期间 `_publish_intent` 不再发布模型目标，只复用/发布 hold（`player_agent.py:888-895`）。`_memory_extras` 只在 `is_locked()` 为真时把最后 3 行 OCR 放进 dialogue 块（`player_agent.py:803-804`）。

### 10.3 4 秒锁与 30 秒 OCR 的时序冲突

这是本节最重要的失败模式。默认参数是：tick 12 秒、OCR 最短间隔 30 秒（`run_shadow.py:124`；`ScreenObserver.interval` 下限 5 秒，`player_vision.py:50`）、锁保持 4 秒（`player_dialogue.py:17`）、hold deadline 60 秒（`player_agent.py:989`）。把 12 秒 tick 与 30 秒限频叠在一起，下一次成功的 OCR 实际落在 t=36，而不是 t=30：

```
t=0    tick，OCR 成功，看到 Talk/文本 -> started，cancel 旧意图，发布 hold(60s)
t=0-4  is_locked=True；speech 被拒；intent 只发 hold
t=4    lock_until 到期，但没人调用 observe
t=12   tick：ScreenObserver 因 30s 限频返回 None -> 不调用 dialogue.observe
       SpeechPolicy / _publish_intent 调 is_locked() -> 静默置 locked=False
       没有 ended 事件 -> hold 不会被取消；随后发布普通模型意图
t=24   tick：仍 <30s，capture 继续返回 None
t=36   tick：capture 成功（36>=30）。若仍有任意 OCR 文本 -> 再次 started、cancel、hold
```

后果有三层。第一，锁并不是“对话期间持续保持”，而是“每次 OCR 采样后的 4 秒”。两次采样之间，SpeechPolicy 会认为可以说话，`_publish_intent` 会发布普通目标；如果 Bridge 只保留单主意图，hold 会被普通意图顶掉；如果 Bridge 保留旧意图，hold 又会活到 60 秒 deadline。两种行为都依赖审计未实机验证的 Bridge 语义。第二，`ended` 只在“成功 observe 且当前没有文本且已过 lock_until”时产生；如果对话结束后画面仍有任何 HUD OCR 文本（拾取提示、任务提示等），`texts` 非空会立刻重新上锁，`ended` 永远不触发，hold 每次成功采样（默认约 36 秒）被重新发布一次。第三，`is_locked()` 的静默解锁没有事件，导致“锁已过期”和“对话已结束”在代码里不是同一件事；hold 的取消路径只挂在 `ended` 上，而不是挂在“锁失效”上。

在 `--observe-ocr` + `--intent-url` 同时启用时，这意味着一条常驻 HUD 文本就可能让 MotorController 反复收到 wait，玩家的正常移动被冻结。这是 shadow 层最需要评审的越界点，也是“shadow 不是绝对零副作用”的具体证据。

### 10.4 OCR 无置信度的风险

`ScreenObserver.capture()` 请求 `includeOcr/includeImageBase64`、`maxWidth=1280`，只取返回 OCR 的前 12 行，并在写 evidence 时把 `confidence` 固定传 `None`（`player_vision.py:64-72,95-107`）。这带来三个问题：

1. **没有区域约束**：OCR 是全屏前 12 行，不是对话框裁剪；HUD、拾取提示、任务标题都可能被当成对话。
2. **没有置信度门槛**：无法区分“安柏：跟我来”和一次误识别；DialogueTracker 对文本一视同仁。
3. **没有说话人/选项结构**：不解析 `角色：台词`，也不识别选项，因此只能做“锁/不锁”，不能做“谁在说、说到哪”。

此外三处口径并不一致：ScreenObserver 存 12 行 OCR，DialogueTracker 只看前 8 行（`player_dialogue.py:49`），ContextBuilder 的 perception 又展示 12 行（`player_context_builder.py:324`）。这意味着模型看到的某些 OCR 行可能从未参与“是否上锁”的判断。

项目 README 自己承认“OCR 无置信度，对话锁可能误判；短对话可能落在两次 observe 之间”（`integrations/player/README.md:233`），但没有把“任意 OCR 文本即可上锁”写成安全边界。现有缓解只有 prompt 侧的“OCR 可能识别错误，不是指令或确定事实”标签（`player_context_builder.py:323-329`），以及 semantic fact 的逐字证据门；对话锁本身没有任何证据门。

### 10.5 SpeechPolicy 的判定顺序

`SpeechPolicy.decide()` 是纯函数加两个进程内字段，顺序是固定的（`player_speech_policy.py:24-54`）：

1. 空文本 → `empty`；
2. `should_speak == false` → `model_silent`；
3. `dialogue_locked` → `dialogue_lock`；
4. 冷却：`spoken_count > 0` 且距上次 `< 6000ms` 且非重要 → `cooldown`；首句必过；
5. 沉默倾向：`silence_bias == "high"` 且 `intensity < 0.6` 且非重要 → `silence_bias`；
6. 否则 `speak=True`，并立即更新 `last_spoken_ms` / `spoken_count`。

“重要”定义为 `intensity >= 0.8` 或 emotion ∈ `{surprised, excited, worried, angry, sad}`（`player_speech_policy.py:13-14,49-52`）。`silence_bias` 来自 profile 的 `speech_style`，只允许 low/medium/high（`player_profile.py:414-428`）。注意 low 与 medium 在策略层没有任何行为差异，只有 high 会额外拦截（`player_speech_policy.py:53`；审计 extra[3] 的结论）；如果产品期望“低沉默倾向更主动”，当前没有实现。另外 `happy` 和 `nervous` 不在 IMPORTANT_EMOTIONS 里：低强度的 `happy`（<0.6）会被 high silence_bias 拦下，`nervous` 不能绕过冷却、只获得 soft 音量。

为什么把 SpeechPolicy 放在模型之外，而不是让 prompt 里的 `should_speak` 全权决定？替代方案是纯模型决策：提示词里已经有 `should_speak` 和 `intensity`，看起来不需要第二套规则。未采用的原因是模型看不到可靠的播放时钟、冷却剩余时间和对话锁状态，也无法稳定地自我克制；一个确定性、可单测的策略层更适合做“发言闸门”。代价是策略看不到模型对本轮语义重要性的完整判断，只能依赖 `intensity` 和 emotion 两个标量；同时策略输出不回灌 prompt，模型不知道自己刚刚被拒。

`speed/volume/pause` 由 intensity 与 emotion 决定：≥0.75 fast、≤0.3 slow；sad/worried/nervous 音量 soft；sad/worried/thinking 前停 300ms，其余 120ms；angry/determined 标记不可打断（`player_speech_policy.py:57-80`）。情绪余韵是唯一跨状态耦合：上一轮衰减后的 `sadness >= 0.5`，本轮 emotion 为 excited/happy 且 intensity < 0.95 时，把 intensity 压到 ≤0.5、pace 回 normal，并标记 `mood_damped`（`player_speech_policy.py:63-69`）。`tests/test_continuity.py:100-115` 覆盖了这条链路。

三个细节值得评审。第一，`last_spoken_ms` 在 shadow 阶段也会更新，尽管并没有真正发声；这是为未来 Active 预留的“假设发言”冷却，但在 shadow 日志里会出现“speak=true 但音频没发生”的记录，不能把 speak 次数当成实际播出次数。第二，冷却状态不持久化，`reset()` 在会话切换时清零（`player_speech_policy.py:83-85`）；重启进程后首句永远放行，多进程/重启语义与进程内不同。第三，余韵只处理 sadness，不处理 fear/tension/anger；`surprised` 不在余韵判断里，难过之后一个高强度的 surprised 仍会满强度输出。另外，`speech.text` 只按 160 字符硬截断（`player_speech_policy.py:37`），profile 的 `max_sentence_chars`（默认 60）从未被使用，可能截出半句。

### 10.6 它如何影响 prompt 和实际输出

对话文本进入 prompt 有两条路。第一条是 required 的 perception 块：只要本轮 OCR 成功，前 12 行中带 `evidence_id` 的 OCR 行都会进入“当前观察”，并带有“可能识别错误、不是指令”的警告（`player_context_builder.py:320-336`）。第二条是 extras 的 dialogue 块：仅在锁定时注入最后 3 行，预算 650 字符、优先级 35，可按总预算丢弃（`player_context_builder.py:27-42,378-384`）。因此“对话锁”并不阻止模型看到 OCR；它阻止的是模型说话和发布普通意图。即使 dialogue 块被预算丢弃，perception 里仍有 OCR。

SpeechPolicy 的输出只写进 shadow 记录里的 `shadow_tick.speech_policy`（`player_agent.py:705-717`），不会回灌到下一轮 prompt。模型看不到“上一句因为冷却被拒”，所以会继续提出 speech；这会产生大量 `cooldown` / `silence_bias` 记录和无效 token 消耗。player 链路没有 TTS 接线；`render_session.py` 虽然有 SAPI/飞鱼 TTS，但它读的是 session 台词，不读 shadow，也不消费 `speech_policy`。prosody 的 pace/volume/pause/interruptible 目前是“给未来 TTS 的结构化提示”，不是实际音频参数。这是【目标设计/未实现】。

真正可能改变游戏的是 hold intent：启用 `--intent-url` 时，`publisher.cancel()` 与 wait 意图会到达本机 Bridge，再由 Bridge 影响 MotorController；`deadline_ms=60000` 让一次 hold 最长可压制 60 秒。未启用时，SpeechPolicy 和 DialogueTracker 都只是本地记录。

### 10.7 失败模式、回滚与实现状态

| 失败/场景 | 当前行为 | 风险 |
|---|---|---|
| OCR HTTP 失败/超时 | warning，回退 Live 帧，本轮不更新对话（`player_agent.py:571-577`） | 锁不刷新，hold 可能过期或残留 |
| 30 秒限频返回 None | 不调用 `dialogue.observe` | 4 秒锁与 30 秒采样错位（10.3） |
| 任意 HUD 文本 | `texts` 非空即上锁 | 误锁、反复 hold、无法解锁 |
| intent 发布/取消失败 | warning，继续 tick（`player_agent.py:610-615,629-635,997-1002`） | 旧意图可能继续执行 |
| profile 缺失/损坏 | 启动返回 2（`run_shadow.py:326-333`；`player_profile.py`） | 无默认人格兜底 |
| player 侧无 TTS 消费 | 只记录 prosody | 无实际语音；`render_session.py` 的 TTS 不读 shadow |

回滚开关：不启用 `--observe-ocr` 就没有 OCR、对话锁和 dialogue 块；不启用 `--intent-url` 就没有 cancel/hold，对游戏零影响；`PLAYER_AGENT_ENABLED=0` / `--disabled` / `PLAYER_AGENT_SHADOW=0` 关闭整个 shadow；`--no-llm` 只观察；`CONTEXT_BUILDER=legacy` 会跳过 OCR（`player_agent.py:571`），因此也跳过对话锁，但四类状态仍可在后台更新。没有单独的“只关 hold、保留对话记录”或“只关对话锁、保留 OCR”的开关。

| 能力 | 状态 |
|---|---|
| DialogueTracker 的 start/hold/end/reset | 【已实现且有测试】`tests/test_player_dialogue.py:23-50` |
| 对话开始时取消意图并发布 wait hold | 【已实现且有测试】`tests/test_player_agent.py:816-857`（假 publisher） |
| 真实 30 秒 OCR 下的连续 hold/解锁 | 【已实现但未实机验证】且存在 10.3 的时序缺陷 |
| SpeechPolicy 冷却/沉默/情绪余韵/prosody | 【已实现且有测试】`tests/test_player_speech.py:34-66`；余韵见 `tests/test_continuity.py:100-115` |
| OCR 置信度、对话区域裁剪、说话人/选项解析 | 【目标设计/未实现】 |
| TTS/渲染消费 prosody、实际发声 | 【目标设计/未实现】 |
| 真实游戏、AutoDL、飞鱼 TTS、盲测 | 【已实现但未实机验证】当前环境未安装原神，AutoDL 已停止；飞鱼 TTS 未接入 player |

结论：DialogueTracker 与 SpeechPolicy 的单元逻辑小而清晰，测试也覆盖了主要分支；但它们的组合在默认参数下不成立——4 秒锁追不上 30 秒限频（默认 tick 下约 36 秒才采到下一次），`ended` 又依赖一次“干净”的采样，导致 hold 可能被普通意图顶掉或反复重发。更严重的是锁条件过宽：任意全屏 OCR 文本都能触发，且在启用 intent 总线时会真实取消 MotorController 意图。评审若只允许一个修改，应优先把锁条件收紧为 `is_in_talk_ui`（或对话框区域 OCR + 置信度），并把 hold 的取消从 `ended` 事件改为“锁失效即取消”；否则“shadow 不干扰游戏”的安全声明在 `--observe-ocr --intent-url` 组合下不成立。

## 11. 意图协议与 MotorController：把“想做什么”与“按什么键”分开

### 11.1 为什么需要一条 intent 总线

PersistentPlayerAgent 的思考周期是 12 秒级（`integrations/player/player_agent.py:523-538`），而游戏输入需要 200ms 级。让同一次 LLM 调用既决定“去哪、做什么”又决定“这一帧按什么键”，在时延和安全性上都不成立。项目因此把控制拆成三段：玩家主体只产出目标级 MotorIntent；Bridge 只做协议搬运、校验与反馈；AutoDL 上的 MotorController（Lumine VLM）把 intent 渲染成 6 个 33ms 动作块，再由 BetterGI 执行。

```
PersistentPlayerAgent (12s tick)
   │ POST /intent（仅 loopback，可选 Bearer）
   ▼
Lumine Bridge ── WS intent ──► AutoDL LumineServer（VLM）
   │                                  │ 6×33ms chunks
   │ ◄──── action / intent_ack ───────┘
   ▼
BetterGI Live Hub（C#）──► InputHub.Foreground
```

这个分层的关键不是“多了一个 JSON”，而是权限被切开：intent schema 里没有 keys 字段（`integrations/player/player_intent.py:63-101`）；具体按键只出现在动作文本里，并由 C# 的按键白名单与危险组合拦截兜底。

替代方案 A 是让玩家 LLM 直接输出动作块。未采用的原因很直接：12 秒的推理结果对 200ms 的动作已经过期，通用对话模型也没有经过动作格式训练；更严重的是，一旦玩家进程被 prompt injection 影响，它就直接拥有按键权。替代方案 B 是让 Motor 自己选目标。也未采用：7B VLM 没有跨 episode 的记忆与任务状态，Motor 的 system prompt 明确禁止它选择目标（`integrations/lumine/lumine_protocol.py:93-100`）。代价是链路更长、需要额外的总线与反馈，而这个反馈在 11.5 会看到并没有完全闭环。

### 11.2 数据模型与状态机

【已实现且有测试】MotorIntent 的规范化在 `integrations/player/player_intent.py:63-101`：

```json
{
  "type": "intent",
  "version": 2,
  "intent_id": "i-20261028-120000-a1b2",
  "goal": "前往七天神像并解锁传送点",
  "reason": "当前体力充足，顺路解锁",
  "constraints": ["不要进入战斗", "优先走大路"],
  "motor_mode": "navigate",
  "target": {"name": "statue"},
  "interrupt_conditions": ["受到攻击", "进入对话"],
  "deadline_ms": 15000,
  "created_ms": 1760000000000
}
```

约束是硬编码的：goal 必填且截到 300 字符；reason ≤400；constraints ≤8 条×200；interrupt_conditions ≤8 条×60；motor_mode 只允许 navigate/interact/combat/explore/ui/wait，非法值静默回退 navigate（`player_intent.py:74-76`）；deadline_ms 夹在 1–60 秒；version 必须是正整数；intent_id 只允许字母数字点下划线和连字符。注意“静默回退”是有意的失败安全选择：宁可让 Motor 走默认导航，也不让一个拼错的模式让整条链停摆；但它也让调用方无法知道自己传错了。

状态机是单写者、单主意图（`player_intent.py:134-191`）：

```
created ──publish accepted──► sent ──progress──► running ──done──► done
   │                            │                  │
   │ new create                 │ blocked/failed   │ blocked/failed ×3
   ▼                            ▼                  ▼
cancelled                 blocked/failed      needs_replan=true
   ▲
   └── cancel() / expire()（生产路径均未调用）
```

create 时若旧意图仍处于 created/sent/running/blocked，会把旧意图标成 `cancelled(replaced_by_new_intent)` 并压入 history（`:134-145`），保证执行侧永远只有一个目标。两个容易忽略的缺陷：create 会把 needs_replan 清零，却不清零 consecutive_failures（`:141-144`），所以一旦累计到 3 次，新意图的第一次失败就会再次触发 replan；history 只增不减，export 时才截最后 20 条（`:193-200`）。

【目标设计/未实现】apply_feedback、expire 只被单测调用。生产代码里没有任何地方调用 `IntentTracker.apply_feedback`，也没有定时器调用 expire；因此 done、needs_replan、expired 在真实运行中不会发生（`player_agent.py:888-1019` 只做 create/mark_sent）。这不是“实现得不够好”，而是反馈闭环根本没有接上。

### 11.3 v1/v2 与 HTTP 意图总线

所谓 v2 不是一条新的 wire 协议。Bridge 仍然上报 `lumine-live-v1`（`integrations/lumine/lumine_bridge.py:69`、`:1220`），v2 只是本地扩展开关 `motor_protocol`，默认值 v1，`intent_port` 默认 0（`:711-714`）。只有 `motor_protocol == "v2"` 且 `intent_port > 0` 时才启动意图 HTTP 服务（`:918-921`）。这是一个明确的回滚设计：不配置就完全回到“guidance + action”的旧链路。

总线只有两个端点（`:935-956`）：

| 端点 | 请求 | 响应 |
|---|---|---|
| POST /intent | 扁平 intent 或 `{"intent":{...}}`；`{"cancel":true}` 清空 | 200 `{accepted:true,intent_id}`；坏 JSON 400；坏意图 400；鉴权失败 401 |
| GET /feedback | 无 | `{protocol,current,feedback[]}`，feedback 最多 20 条 |

鉴权与回环是这条链最值得评审的地方，因为两端并不对称：

- 玩家侧 IntentPublisher 构造时强制 http/https + 127.0.0.1/localhost/::1 + 路径为空或 `/`（`player_intent.py:212-221`），publish 时自动追加 `/intent`（`:242-243`），禁止重定向、5 秒超时、非 200 或响应非 accepted 都返回 False（`:234-253`）。【已实现但未实机验证】这段 URL 校验没有单测；README 里的 `--intent-url http://127.0.0.1:8011/intent` 写法会直接构造失败，正确写法是根地址。
- Bridge 侧只做“可选 Bearer”：`intent_token` 默认为空，`authorized()` 在 token 为空时直接放行（`lumine_bridge.py:925-931`）；`intent_host` 默认 127.0.0.1，但可以配置成 0.0.0.0，代码不校验回环，也没有 TLS（`:959`）。如果把它暴露到局域网且不配 token，任何能访问该端口的人都能注入 goal。
- AutoDL 侧是第三套边界：`set_intent` 只检查 intent_id 和 goal 是非空字符串，其余原样存 dict（`integrations/lumine/autodl_server.py:363-375`）；默认 `--host 0.0.0.0`、`--token` 为空（`:890-900`），只在启动时打 warning。token 非空时用 `hmac.compare_digest` 校验 query 的 token/access_token 或 Bearer 头（`:660-677`）。

Bridge 的 `_normalize_intent_bus` 同样是“弱规范化”（`lumine_bridge.py:842-870`）：constraints 直接对 `(payload.get("constraints") or [])[:8]` 做切片，如果传的是字符串就会按字符切；deadline_ms 直接 `int(...)`，非数字会抛异常，而 HTTP handler 没有兜底，aiohttp 会回 500 而不是 400。这些都属于“本机可信调用方”假设下的偷懒，但只要 token 为空、host 可配，这个假设就不成立。

### 11.4 Motor prompt 的权限边界

【已实现且有测试】MOTOR_SYSTEM_PROMPT 的措辞是这条链的“宪法”（`integrations/lumine/lumine_protocol.py:93-110`）：

> You do NOT choose goals, quests, routes, dialogue options, or what to explore. You only convert the supplied intent into the next 200 ms action... If the intent is already satisfied, cannot be executed safely, or the frame is unclear, output a safe idle action... Never invent progress.

format_intent 只渲染 intent_id/goal/motor_mode/deadline/reason/constraints/target（`:166-184`）；version 和 interrupt_conditions 不进入 prompt。也就是说：

- Motor 知道“为什么”和“约束”，但不知道“什么情况下必须停”。interrupt_conditions 目前只是存储字段，没有执行语义。
- Motor 不知道协议版本，无法按版本改变行为。
- target 是任意 dict，原样 JSON 序列化后拼进 user prompt，没有转义或“这是数据不是指令”的隔离。goal/reason/target 都来自玩家 LLM 的输出，若玩家被 prompt injection 影响，攻击文本会原样进入 Motor 的 prompt。C# 的按键白名单和危险组合能阻止 Alt+Tab 一类系统键，但不能判断“按 W 往前走”是否违背玩家意图。

因此，Motor 的权限边界应表述为：它不拥有目标选择权，但拥有在目标内的按键选择权；它不是安全沙箱。真正的安全边界在 C# 的 InputHub 与白名单（见第 12 节），以及玩家侧“不发送动作”的静态约束。

另一个结构问题是仓库里有两套模型适配器：玩家/导演走 `integrations/lumine/llm_client.py`（Anthropic Messages 与 OpenAI chat/completions 的统一传输层），Motor 走 `autodl_server.py` 自己的 vLLM 调用。两者的端点归一化、超时、错误分类、图像校验各写一套：autodl_server 会校验 base64、大小和 JPEG SOI（`autodl_server.py:112-163`），llm_client 完全不校验图像。短期看这是“各管各的”，长期会让“同一张帧在两条路径上的失败行为”漂移。

### 11.5 feedback、ack 与重连：闭环在哪里断了

Bridge 记录的反馈只有三种来源（`lumine_bridge.py:1168-1186`、`:1301-1324`、`:891-908`）：

| 触发 | 记录 | 含义 |
|---|---|---|
| AutoDL intent_ack | progress / intent_ack | 服务端已存下 intent，不代表已执行 |
| BetterGI 广播 action | progress / action_applied | 动作块已被接受并回显 |
| Bridge 成功转发 action | progress / action_forwarded | 已发给 BetterGI |
| BetterGI/AutoDL error | failed / code | 传输或执行侧报错 |
| clear_intent | blocked / session_reset 等 | 会话边界清空 |

三个断点：

1. 没有任何代码产生 done。玩家侧没有“意图完成”的判定器，Bridge 只能把每个动作记成 progress；feedback deque 上限 20，5fps 的动作会在数秒内把历史冲掉。
2. 玩家从不消费反馈。`IntentPublisher.fetch_feedback` 存在（`player_intent.py:255-269`），但 player_agent 只 publish，不 GET；所以 consecutive_failures、needs_replan、expire 全部是死状态。
3. ack 不等于执行。AutoDL 在非 motor 模式下也会回 intent_ack，但 prompt 里根本不渲染 intent；Bridge 无法从 hello 知道 `promptMode`，于是“已 ack”可能只是静默 no-op。

重连与重发：

- AutoDL 断线重连后，Bridge 会补发 latest_guidance 和 latest_intent（`lumine_bridge.py:1224-1232`），退避 1–20 秒，稳定连接 10 秒后重置（`:1204-1276`）。
- BetterGI 的 live.started/live.stopped 只清空 Bridge 本地 latest_intent（`:1101-1109`），不会向 AutoDL 发 cancel/reset；AutoDL 会继续持有上一场的 intent，直到新 intent 替换或断线。
- `set_intent` 在 `_send_lumine` 返回 False 时仍然记录 progress/intent_sent（`:878-880`），反馈会谎报“已发送”。
- 玩家每个 tick 对同一 goal 复用 intent_id 并重复 publish（`player_agent.py:917-949`）；AutoDL 不做幂等比较，只是覆盖，所以语义上依赖“替换”而不是“去重”。
- 玩家进程退出或 tick 卡住后，没有任何一方按 deadline 终止 intent；deadline_ms 只出现在 prompt 文本里（`lumine_protocol.py:174`）。Motor 会无限期继续上一个目标。

### 11.6 失败模式、并发与回滚

| 失败 | 当前行为 | 风险 |
|---|---|---|
| 玩家 publish 超时/非 200 | 返回 False，写 publish_failed，下 tick 重试（`player_agent.py:939-956`） | 无退避，12 秒一次持续打 |
| Bridge 转发 AutoDL 失败 | 仍记 progress/intent_sent | 玩家以为已送达 |
| AutoDL vLLM 持续失败 | 每帧 error 广播，无熔断 | 5fps 错误风暴 |
| 意图总线坏 payload | constraints 字符串按字符切；deadline 非数字 500 | 本机调用方也可能踩 |
| 会话切换 | Bridge 清本地，不通知 AutoDL | Motor 执行上一场目标 |
| 对话锁 | 先 cancel，再发 60 秒 wait hold（`player_agent.py:959-1019`） | hold 复用分支在会话切换后直接返回 published=True，不真正重发 |
| 玩家崩溃/被杀 | 无 deadline 执行 | Motor 保持最后 intent |

并发方面，玩家侧是单 ticker 串行调用 tick（`integrations/player/player_loop.py:44-56`），IntentTracker 是单写者；Bridge 的 HTTP handler 是并发的，两个 POST /intent 可能交错，最终以最后一次 `self.latest_intent = intent` 为准，没有序号或版本比较。对“同一时刻一个主意图”的不变量来说这够用，但对“哪个意图被 Motor 真正执行”无法追溯。

回滚开关是齐的：`motor_protocol=v1`（默认）、`intent_port=0`（默认）、玩家不配 `--intent-url`/`PLAYER_INTENT_URL`、`--no-llm`、AutoDL `--mock`、删除 resume_state.json。它们能分别退回“无总线”“无意图”“无模型”“无 GPU”四种形态；但这些组合没有端到端回归测试，且当前环境未安装原神、AutoDL 已停止，不能声称真实链路可用。

状态归纳：

- 【已实现且有测试】intent 规范化/限长、单主意图替换、状态迁移、失败阈值、foreign feedback 忽略、动作文本解析、Bridge 意图 roundtrip 与 401、AutoDL mock。
- 【已实现但未实机验证】玩家→Bridge→AutoDL→BetterGI 全链、真实 Motor 的意图遵循度、真实帧尺寸/延迟。
- 【目标设计/未实现】done 判定、反馈消费、deadline 计时器、interrupt_conditions 执行、场景切换时向 Motor 下发 cancel/reset、intent_id 回显。

## 12. BetterGI C# Live Hub 与感知/执行接口

### 12.1 先纠正命名：没有 C# 的 PersistentPlayer

C# 侧不存在名为 PersistentPlayer 的服务。实际结构是两段式：C# 的 `LiveSessionService` + `LiveActionExecutor` + HTTP/WS/MCP 组成 BetterGI Live Hub，是唯一拥有截图与输入能力的部分；Python 的 PersistentPlayerAgent 是只读 shadow 进程，消费同一个 Live WebSocket，写自己的记忆与 shadow 记录。C# 不读游戏内存、不抓包、不注入脚本；Python 不直接操作游戏。Active 模式（让玩家真正发送动作/指导/解说）尚未实现。

```
PersistentPlayerAgent (Python, shadow)
   │  WS 只读（frame/guidance/action/live.stopped）
   │  POST /api/v1/agent/observe（可选 OCR，loopback）
   ▼
BetterGI Live Hub (C#)
   LiveSessionService ──► IAgentScreenSource.Capture（复用 GameRuntime）
   LiveActionExecutor ──► InputHub.Foreground ──► 游戏窗口
   LiveHttpEndpoints / LiveWebSocketHandler / LiveMcpTools
```

| 职责 | C# Live Hub | Python PersistentPlayerAgent |
|---|---|---|
| 截图/编码 | 唯一实现，复用现有 GameRuntime | 只接收 JPEG |
| 键鼠注入 | 唯一实现，经 InputHub + 白名单 | 无接口 |
| 会话/时间线 | live.started/stopped、guidance、commentary | 只读消费 |
| OCR/UI | `/api/v1/agent/observe` | 可选调用，生成证据 |
| 认知/记忆 | 不参与 | 全部在 Python |
| 意图 | 不感知 intent | 可选发给本机 Bridge |

替代方案是让 Python 直接用 Win32 截图和 SendInput。未采用的原因有三点：会与 BetterGI 的 GameRuntime 形成第二条捕获链路，分辨率和时序不一致；会绕开 InputHub 的统一按键白名单、危险组合拦截和停止开关；令牌与安全边界会散落到两个进程。另一个替代方案是把认知也放进 C#，同样未采用：vLLM/记忆/提示词生态在 Python，C# 只做“看画面、发按键”更符合职责分离。

### 12.2 宿主、端点与鉴权

【已实现且有测试】宿主只在 Primary 实例启动，端口必须 1024–65535，任一开关启用即启动子 WebApplication；`ListenLocalhost(port)`，请求体上限 4MB（`BetterGenshinImpact/Service/ExternalAccess/ExternalAccessHost.cs:125-190`、`:246-251`）。所有请求先校验 Host 为 localhost/loopback（`:302-319`），HTTP/MCP/Live WS 需要 Bearer；只有 `/ws/v1/agent` 允许 `access_token` 查询参数，因为浏览器 WebSocket API 不能设置 Authorization 头（`:324-353`）。CORS 是 `AllowAnyOrigin`，它不构成访问控制（`:257-259`）。配置只在启动时读取，运行中改端口/开关要重启。

| 端点 | 作用 | 关键限制 |
|---|---|---|
| POST `/api/v1/agent/live/start` | 启动 Live 会话 | fps 2–15、quality 30–95、maxWidth 320–1920、recordFps 1–10 |
| POST `/live/stop` | 停止并释放按键 | 幂等 |
| GET `/live/status` | 运行状态 | 关闭 AgentControlEnabled 后仍可用 |
| GET `/live/frame` | 最新 JPEG | 404 LIVE_FRAME_NOT_FOUND |
| POST `/live/guidance`、`/commentary` | 指导/解说 | 文本 1–500，kind 白名单 |
| GET `/live/timeline` | 增量时间线 | limit 夹在 1–500 |
| POST `/live/actions` | 注入动作块 | 1–6 chunk，每块 ≤4 键 |
| WS `/ws/v1/agent/live` | 帧/指导/动作双向 | 单控制者、seq 严格递增 |
| POST `/api/v1/agent/observe` | 截图 + UI/OCR | 与动作序列共用 `_sequenceLock` |
| MCP `/mcp` | 8 个 `bgi_live_*` 工具 | 仅 mcpEnabled && agentEnabled |

错误映射是稳定的：`AgentException` 转 code/status，`OperationCanceledException` 转 408，其他转 500 `INTERNAL_ERROR`，Details 带异常消息（`LiveHttpEndpoints.cs:135-169`）。但 `/live/actions` 即使 `accepted=false` 也返回 200，这一点与“HTTP 状态即结果”的直觉相反，调用方必须读 body 的 `accepted`。

### 12.3 帧循环与 observe/OCR

【已实现且有测试】Live 帧循环按 `1000/fps` 间隔截图，缩放到 maxWidth 后 JPEG 编码，`FrameSeq` 递增，生成 `LiveFrameSnapshot` 并广播（`LiveSessionService.cs:594-684`）。安全条件是 `IsFrameSafe`：state 引用一致、runtime 存活且未停止、窗口存活/前台/非最小化、最近截图在 `max(500, 3000/fps)` ms 内（`:571-591`）。截图失败时 `LastFrameTicks=0`、`StopActions`、广播 `LIVE_CAPTURE_FAILED`、等 500ms 再试（`:647-667`）。Live 会话整个生命周期以非阻塞方式持有 `TaskControl.TaskSemaphore`（`:161-164`），直到采集循环真正结束才释放；这意味着直播期间 BetterGI 的其他独立任务会被拒绝，文档中“实时动作不占用任务锁”的说法与代码不符。

【已实现但未实机验证】`observe/OCR` 是另一条入口：`ObserveAsync` 用 `_sequenceLock` 串行化观察与动作序列（`BgiAgentCapabilityService.cs:198-212`），截图和视觉识别在 `TaskTriggerDispatcher.PauseForInference()` 租约内执行（`:230-245`），返回 `imageBase64`、`vision`、`ocr` 和 `eventSeq`。Live 帧不填充 `uiCategory`，所以 Python 想要 UI/OCR 必须单独调用 observe；这条路径会在 BetterGI 侧写 observation 事件，不是零副作用。

并发风险是明确的：Live 帧循环直接调用 `IAgentScreenSource.Capture`，既不暂停 `TaskTriggerDispatcher`，也不与 observe 的 `_sequenceLock` 互斥（`GameWindowScreenSource.cs:19-98`、`LiveSessionService.cs:606`）。如果实时触发器、Live 广播和 OCR 同时抓同一个窗口，截图后端是否允许并发取决于 `IGameCapture` 实现，代码层没有仲裁。替代方案是为 Live 建立独立捕获源，未采用是为了避免第二条截图链、分辨率漂移和额外 GPU 拷贝；代价就是把并发正确性留给了底层实现。

Python 侧 `ScreenObserver` 只接受 loopback、同端口、协议匹配，禁止 URL 凭据/查询/重定向；默认 30s 限频、15s 超时、maxWidth 1280，响应校验 sessionId/frameId/时间新鲜度，并对最多 12 行 OCR 调 `EvidenceStore.record_screen_ocr` 生成 sha256 证据（`integrations/player/player_vision.py:23-120`）。【已实现且有测试】但只在假 observe 服务上验证过。

### 12.4 动作队列与执行器

【已实现且有测试】动作块的契约在 `LiveSessionService.EnqueueActionsAsync`（`LiveSessionService.cs:382-454`）：1–6 个 chunk，每块 ≤4 键，dx/dy ∈ [-1000,1000]，scroll ∈ [-5,5]，只有第 1 块可带位移/滚轮。WS 入口额外做单控制者检查：已有其他客户端控制时返回 `ACTION_REJECTED`；带 seq 的动作块对同一连接必须严格递增，防止重放或乱序（`LiveWebSocketHandler.cs:246-296`）。HTTP `/live/actions` 没有 owner 与 seq 检查，任何持有 Bearer 的调用方都能直接入队。

执行器的真实语义与文档差别很大（`LiveActionExecutor.cs:141-191`）：

- `Enqueue` 先检查安全，然后 `Interlocked.Increment(ref _epoch)` 并清空整个队列；没有文档宣称的“20 个 chunk 上限、超出丢最旧”。一次动作块最多排 6 个 chunk + 1 个 absolute，新块到达即作废旧块。
- 后台循环约 5ms 轮询，只消费到期 chunk；旧 epoch、stop、延迟超过 200ms 的 chunk 直接丢弃（`:330-387`）。
- 每个 chunk 注入前重新 `IsSafe`；队列空且仍有按键超过 250ms 才全部释放（`:380-386`）。
- 按键集合做差分；未知 token 被 `TryMap` 静默忽略；Alt+Tab、Alt/Ctrl+Esc、Alt+Space、Alt+Enter 整批拒绝；F4/F11 不在白名单（`LiveKeyTokenMap.cs:17-33`、`:54-113`、`LiveActionExecutor.cs:457-534`）。
- `StopActions` 先置 stop、递增 epoch、等待 `_executionGate`、清队、释放全部按键，返回后不会再产生新按键（`:205-224`）。

【目标设计/未实现】intent_id 没有回显。C# 的 `LiveActionBlockDto`、`LiveServerMessage.action`、`LiveActionAckDto` 都没有 intent 字段（在 `BetterGenshinImpact/Service/ExternalAccess` 下 grep `intent_id|intentId` 无命中）；AutoDL 的 action 消息也只有 `forFrameSeq/action/chunks/absolute/thought/latencyMs/raw`。因此 Bridge 收到 action 时无法知道它属于哪个 intent，只能用 `latest_intent` 近似记 progress。这是第 11 节反馈闭环断裂在 C#/协议层的直接原因。

失败模式：

| 场景 | 行为 | 后果 |
|---|---|---|
| 失去前台/最小化/游戏退出 | 循环内 `StopActions` 清队并释放按键 | 不会在回到前台后补播 |
| 截图失败 | 清帧龄、StopActions、广播 error、500ms 后重试 | 错误会持续广播 |
| 慢客户端 | 每客户端 32 条 `DropOldest` 有界 Channel | 丢帧但不断连接 |
| 控制端断开 | 若它是 action owner，先 StopActions 再 Detach | 所有权释放，但 `stop_actions` 本身不释放所有权 |
| 同一 session 重启 + recordFrames | FrameSeq 从 1 开始 | 覆盖上一轮 live/frames 缩略图 |
| 绝对坐标越界 | 入队时不校验，执行循环抛异常并 StopActions | 同块剩余动作一起丢 |

### 12.5 限额、持久化与恢复

【已实现且有测试】`AgentSessionStore` 用 `_gate` 串行化文件操作，meta/memory/checkpoint 用同目录临时文件 + `File.Move` 原子替换；sessionId 必须匹配 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`，frameId 另有正则与目录前缀双重校验防路径穿越（`AgentSessionStore.cs:30-32`、`:94-198`、`:970-984`）。限制是 100 个会话、每会话 300 帧、20k 事件、1MB memory、50 个检查点、单帧 20MB（`AgentLimits.cs:55-108`）；事件超过 20000 条压缩到最近 10000 条。

损坏恢复的边界要说清楚：events.jsonl 先写 meta.LastSeq 再追加事件，崩溃只产生序号空洞，不重复使用序号；但 meta 损坏/丢失后 `EnsureSession` 会从 seq=1 重建 meta，旧 events.jsonl 仍在，可能产生重复序号（`AgentSessionStore.cs:550-588`、`:607-642`）。损坏事件行跳过，损坏 meta 的会话在 `ListSessions` 中跳过。时间线 `_timeline`/`_timelineSeq` 是进程级字段（`LiveSessionService.cs:87-90`），Start/Stop 不清空也不重置，新会话 `afterSeq=0` 会读到上一场的 guidance/commentary。

鉴权与限额方面：令牌等同本机截图 + 键鼠控制权，HTTP/MCP 必须 Bearer，Live WS 额外允许查询参数令牌（`ExternalAccessHost.cs:324-353`）；但 Live 客户端数量没有上限，每个慢客户端 32 条 base64 帧队列可能占数 MB，也没有 per-client 指标或踢出策略。

回滚开关：关闭 `AgentControlEnabled` 会 `StopActions` 并异步 `StopAsync`，运行中的 start/guidance/commentary/actions 走 403，status/stop/frame 仍可用（`LiveSessionService.cs:884-913`）；不启用 `HttpApiEnabled`/`McpEnabled`/`WebSocketEnabled` 则对应路由不映射；Python 侧 `PLAYER_AGENT_ENABLED=0`、`--disabled`、不传 `--player-shadow-config`、`--no-agents` 都可完全绕开。

### 12.6 尚未打通的接口与状态矩阵

当前 Python 与 C# 之间还有几处“看起来通了、实际没通”：

1. C# 从不广播 `live.started`。它只把 live.started 写入时间线（`LiveSessionService.cs:183-184`），停止时才广播 `live.stopped`（`:270`）。Python 的 live.started 分支（`player_agent.py:314-316`）是死路径，真实换场依赖 hello sessionId 变化、帧序号回退和 live.stopped。
2. hello 字段名不一致。Bridge 读 `frameWidth/frameHeight`（`lumine_bridge.py:1094-1095`），C# 发 `width/height`，只影响日志，不影响控制。
3. intent_id 不回显（见 12.4），动作无法归属到意图。
4. Windows 下 `run_episode` 用 terminate 结束 Bridge，Python 的 SIGTERM handler 不会执行，`stop_actions` 可能不发送；安全兜底只能依赖录制器先停 live 会话或 BetterGI 的 250ms 饥饿释放。
5. Active 模式未实现：Python 没有任何发送 actions/guidance/commentary 的代码路径，无法替换旧导演的实况解说/TTS。

| 能力 | 状态 |
|---|---|
| C# Live 生命周期、参数校验、帧循环、停止释放、动作执行、按键白名单 | 【已实现且有测试】`LiveSessionServiceTests`/`LiveKeyTokenMapTests` 存在；本次未执行 dotnet test |
| HTTP 端点绑定/错误映射、MCP 工具、真实 WS 收发、慢客户端、多客户端并发 | 【已实现但未实机验证】部分无直接测试 |
| 真实游戏 SendInput、33ms 节奏、OBS、AutoDL/vLLM、端到端直播 | 【已实现但未实机验证】当前环境未安装原神、AutoDL 已停止 |
| observe/OCR 的 C# 视觉链路（Bv/PaddleOCR） | 【已实现但未实机验证】无单元测试 |
| Python shadow 只读观察、记忆、证据门、意图发布 | 【已实现且有测试】假 Live Hub/假 observe 上通过；真实链路未验证 |
| Active 模式、intent_id 回显、live.started 广播、会话级时间线 | 【目标设计/未实现】 |

一句话总结第 12 节：C# Live Hub 的“看”和“按”已经是一个边界清楚、可停止、可审计的实现；Python 的“想”也守住了只读 shadow 的边界。两者之间真正缺的不是更多类，而是三个协议字段和一条消费者链路：intent_id 回显、live.started 广播、以及有人读取 /feedback 并据此 replan。没有这三样，当前系统仍是“开环的意图 + 闭环的按键”，而不是一个可声称成立的持久玩家。

## 13. 会话录制、事件格式与视频

> 状态标记：【已实现且有测试】表示仓库内有对应自动化测试，且主编已执行 393 passed；【已实现但未实机验证】表示代码路径存在，但缺少真实游戏、真实设备或真实服务的证据；【部分实现/未闭环】表示关键链路存在但反馈/接线不完整；【目标设计/未实现】表示只有类型、接口或文档承诺。

正式录制链路位于 `integrations/session`。PersistentPlayerAgent（玩家主体）当前处于 shadow 阶段，只写自己的 shadow 记录（`integrations/player/shadow_recorder.py`），不写本节讨论的 `session.jsonl`，也不调用渲染器；两条时间线目前没有交叉引用。因此本节结论只对“导演 + MotorController（Lumine 动作执行器）+ BetterGI Live Hub”的正式录制成立。

### 13.1 一个会话目录与一份权威记录

一场录制对应 `sessions/<session_id>/`。`session.jsonl` 是唯一权威记录，`gameplay.mp4` 是可选画面，`audio/game_audio.m4a` 是可选游戏原音；`final.mp4`、`subtitles.ass`、`render.json` 由后期写入。设计目标写得很清楚：一行一个 JSON、UTF-8 无 BOM、人类和 `jq` 都能直接读，删除视频后事件仍可独立解释（`integrations/session/session_format.py:3-18`）。

首行是元信息，之后每行一个事件。元信息至少包含：

```json
{"type":"session","version":1,"session_id":"20261027-171602-07d1bc",
 "created_at":"2026-10-27T09:16:02.000Z","t0_unix_ms":1761556562000,
 "source":{"kind":"bettergi-live","ws":"ws://127.0.0.1:30648/ws/v1/agent/live","capture":"frames"},
 "video":{"path":"gameplay.mp4","fps":15,"width":1280,"height":720},
 "agent":{"kind":"director+lumine","name":"director"}}
```

事件类型是封闭集合，共 12 类（`session_format.py:55-72`）。但“类型存在”不等于“生产者存在”，这是评审时最容易混淆的地方：

| type | 语义 | 当前生产者 | 状态 |
|---|---|---|---|
| `session` | 元信息，仅首行 | SessionWriter | 已实现且有测试 |
| `thought` | 内部思考，不朗读 | `_on_commentary` 且 `meta.internal` | 已实现且有测试 |
| `speech` | 要朗读的解说，含 `dur` 与口型包络 | `_on_commentary` | 已实现且有测试 |
| `action` | 模拟输入快照 | `action_state` / `action` 消息 | 已实现且有测试 |
| `avatar` | pose / blink | `_housekeeping` 每秒补姿态、随机眨眼 | 已实现且有测试 |
| `guidance` | 给操作模型的指导 | `_on_guidance` | 已实现且有测试 |
| `video` | 画面开始偏移 | 首个有效帧或窗口启动 | 已实现；window 未实机验证 |
| `audio` | 游戏原音采集标记 | `_start_audio` 成功后写一次 | 已实现；dshow 未实机验证 |
| `error` | 服务端错误 | 仅 `error` 消息 | 已实现 |
| `end` | 收尾统计 | `SessionWriter.close` | 已实现且有测试 |
| `frame` | 文档称“关键截图” | 没有生产者 | 目标设计/未实现 |
| `note` | 人工/工具备注 | 只有 `emit_note` 辅助，无调用方 | 目标设计/未实现 |

`audio` 事件的语义在文档和代码之间是冲突的。`integrations/session/session_format.py:32` 与 `integrations/session/README.md:50` 把它写成“后期写回的配音条目”，但代码中只有 `record_session.py:807-815` 写它，内容是 `path/device/source=dshow` 的游戏原音采集标记；渲染器从不写 `session.jsonl`，也不读这个事件。评审应以代码为准。

为什么用 JSONL 而不是 SQLite 或二进制容器？JSONL 的恢复边界最差但可读性最好：任意一行可以单独解析，坏行能定位到行号，评审者不需要工具就能核对时间轴。代价是没有索引、没有事务、没有文件锁。当前代码接受了这个代价：`SessionWriter` 以 `"w"` 打开文件（`session_format.py:299`），每行 `write+flush`，但没有 `fsync`、原子改名、备份或锁（`session_format.py:325-330`）。

### 13.2 时间轴：t0、t 与视频偏移

所有事件的 `t` 是相对 `t0` 的非负整数毫秒。`t0` 由第一条带 `timestampUtc` 的消息或第一帧设置；`hello` 只保存，不触发 Writer（`record_session.py:574-578`、`604-610`）。如果连接后 5 秒没有任何消息，`_tick` 会用 `_connected_at` 兜底建 Writer，而 `_connected_at` 是 `SessionRecorder.__init__` 的构造时刻，不是连接成功时刻（`record_session.py:383`、`545-549`）。因此“连接/启动等待”可能被计入 `t=0` 之前，设计文档所说“t0 是首帧/首事件的 UTC 时间”并不总成立。

事件 `t` 优先取服务端 `timestampUtc`，缺失时回退本地当前时间（`record_session.py:597-602`、`613`）。画面却走另一条时钟：`FrameSink` 用本地 `monotonic` 按 `1/fps` 节拍写帧（`record_session.py:146-167`）。两者叠加时，事件轴与画面轴不是同一时钟；再加上 `t0` 的 5 秒兜底，长录制可能出现漂移。`video` 事件记录画面相对会话起点的偏移，渲染器用最早一条 `video.t` 做片头 `tpad`（`render_session.py:886-890`、`1307-1321`），这是目前唯一的显式补偿。

`t` 不要求严格递增，`validate` 也明确允许时钟回退（`session_format.py:525-530`）。文件顺序只代表写入时机，对齐一律看 `t`。这对重连、并发消息是合理的，但也意味着“读到的事件顺序”不能直接当播放顺序。

### 13.3 两种画面采集：帧合成与窗口直录

```text
BetterGI Live Hub --WS frame(JPEG)--> FrameSink --image2pipe--> ffmpeg libx264 --> gameplay.mp4
BetterGI Live Hub --events----------> SessionWriter ---------------------------> session.jsonl
桌面窗口 ----------------------------> WindowSink --gdigrab-------------------> gameplay.mp4
声卡/虚拟设备 -----------------------> AudioSink --dshow---------------------> audio/game_audio.m4a
```

| 维度 | `--capture frames`（默认） | `--capture window` |
|---|---|---|
| 输入 | Hub 推来的 JPEG 帧 | ffmpeg `gdigrab title=<窗口标题>` |
| 帧率 | 按 `--fps` 用最新帧重复补齐 | ffmpeg 按 `--fps` 抓屏 |
| 优点 | 与智能体看到的画面同源；不依赖前台窗口；可跨桌面 | 画质与流畅度更接近真实窗口 |
| 缺点 | Hub 掉帧时重复旧帧；编码阻塞会压缩时长 | 依赖窗口标题/桌面会话；最小化、遮挡、DPI 都可能失败 |
| 验证 | 假 WS 端到端测试 | 未实机验证；文档也只承认验证过桌面 |

帧合成的关键缺陷在 `_run`：写 stdin 阻塞导致落后时，代码把 `next_tick` 直接重置为当前时间，不补帧（`record_session.py:162-167`）。持续过载时 `frames_written/fps` 会短于墙钟，视频内容被压缩，而事件 `t` 仍按真实时间走，于是“视频与事件严格同步”不成立。README 的“严格一致”（`integrations/session/README.md:84`）是过度承诺。

窗口直录还有两个元数据问题：启动时 `WindowSink` 先 `sleep 1.0` 再轮询进程，只能发现“启动即失败”（`record_session.py:303-312`）；写 `video` 事件时硬编码 `1920x1080`（`record_session.py:476-485`），而 Writer 元信息用 `hello.width/height` 或默认 `1280x720`（`record_session.py:784-789`）。如果 `hello` 没给尺寸，元信息与事件会互相矛盾。渲染器实际用 `ffprobe` 探测真实视频，因此画布通常以探测结果为准，但数据契约本身不干净。

为什么默认帧合成而不是窗口直录？因为帧合成消费的是 BetterGI Live Hub 已经公开的同一批 JPEG，和导演/操作模型看到的画面一致，且不要求游戏窗口在前台；窗口直录更漂亮，但依赖 Windows 桌面会话、窗口标题和 DPI，评审环境无法稳定复现。代价是 Hub 的帧率和编码延迟直接决定成片质量，且当前没有“落后多少帧就丢弃/计数”的可观测指标。

### 13.4 AudioSink：游戏原音的时间对齐缺口

`AudioSink` 用 ffmpeg `dshow` 录 `audio/game_audio.m4a`，48kHz 立体声、AAC 192k（`record_session.py:209-235`）。启动流程是：Writer 打开后 `_start_audio` 启动 ffmpeg，`start()` 内部固定 `sleep 1.0` 再检查进程是否已退出；成功后写一条 `audio` 事件，`t` 取 `last_event_t`（通常是 0），内容是路径、设备名和 `source=dshow`（`record_session.py:795-816`）。

这里有一个明确的同步缺口：`audio` 事件没有记录首采样相对 `t0` 的真实偏移，而 `start()` 至少包含 1 秒等待和设备初始化时间；渲染器又完全不读 `audio` 事件，只按文件名发现 `audio/game_audio.*`，并从 0ms 开始 `atrim` 混入（`render_session.py:849-863`、`1230-1237`）。因此游戏原音可能整体提前或滞后至少一秒，且没有任何补偿字段。这是当前会话/渲染链路最值得优先修的时间轴问题。

另一个工程边界是 `run_episode.py` 的 `build_recorder_args` 只转发 `url/http-base/out/capture/fps/window-title/reconnect-ms/name/duration/start/stop-on-exit`，没有 `--audio-device`（`run_episode.py:42-71`）。也就是说，一条命令跑一场的典型路径根本录不到游戏原音；只有直接调用 `record_session.py` 并显式传设备名才可能。stage4/5 的 `render.json` 中 `game_audio` 均为 `null`，与这一点一致。

替代方案是把原音和画面在录制时直接混进同一个 MP4，或改用 WASAPI loopback 并记录首采样时间戳。前者会让“删除视频后事件仍可解释”变差，也会把音频编码和画面编码耦合在一个进程里；后者需要额外依赖和设备枚举。当前选择“分开录、后期混”在架构上更干净，但没有把偏移写进契约，导致干净的分层变成了不可验证的同步。

### 13.5 故障模式、重连与收尾

| 故障 | 当前行为 | 后果 |
|---|---|---|
| WS 断开 | 指数退避重连，初始 `max(500, reconnect_ms)`，上限 10s；继续写同一 Writer（`record_session.py:488-521`） | 事件可能重复（commentary 重放），`reconnects` 只是结束时的连续失败净值，不是累计次数 |
| 重连成功 | `reconnects=max(0,reconnects-1)`，但 `backoff_ms` 不重置 | 反复断线会长期停在较大间隔 |
| FrameSink 写失败 | 记 `sink.error` 并退出线程，不写 `error` 事件 | 画面缺失，事件继续 |
| FrameSink 启动失败 | 每个新帧都重试并打印一次警告（`record_session.py:623-629`） | 日志可能刷屏，仍不计数、不写事件 |
| WindowSink 失败 | 本次不录画面，事件继续（`record_session.py:469-474`） | 只剩 `session.jsonl` |
| AudioSink 失败 | 只警告，不写 `audio` 事件 | 后期只能靠显式 `--game-audio` |
| 单条消息异常 | `counts.errors += 1` 并打印，不写 `error` 事件（`record_session.py:539-543`） | 错误不可在会话内审计 |
| 坏 base64 | `validate=True` 解码失败后静默丢弃（`record_session.py:641-644`） | 丢帧无计数 |
| Writer 创建失败 | `_ensure_writer` 先置 `t0/session_dir` 再 `_open_writer`；异常后下一轮因 `t0` 非空不再重试（`record_session.py:604-610`） | 整场静默无会话 |
| 目录重名 | 先加 `-<epoch秒>` 后缀；若仍存在，`mkdir(exist_ok=True)` 复用并因 Writer `"w"` 截断旧文件（`record_session.py:760-769`、`session_format.py:299`） | 旧会话被覆盖 |
| 进程强杀/断电 | 每行 flush 但无 fsync；可能留半行 | `read_session` 对任意坏行直接抛 `ValueError`，整份不可读（`session_format.py:566-569`）；没有截断修复工具 |

收尾顺序是：停 FrameSink（最多等线程 10s、进程 15s），计算 `duration=max(last_event_t,last_frame_t,sink.duration_ms)`，停 AudioSink 并把音频墙钟时长只放进 `counts.game_audio_ms`，最后 `writer.close(duration_ms, counts)` 写 `end`（`record_session.py:885-917`）。音频时长不参与会话总时长，所以“画面/事件比音频长”不会被自动纠正。

`record_session.main` 与 `run_episode.main` 恒返回 0（`record_session.py:989-994`）。没有生成会话、录制线程异常死亡、join 超时，自动化都拿不到非零退出码，只能靠产物和 `validate` 判断。这是可观测性上的高风险点。

### 13.6 边界、并发与回滚开关

会话文件被设计成“半可信输入”：读取阶段只做 JSON、首行、版本检查；`validate` 才检查 `t/type/speech/end`，而渲染器把 `validate` 失败降级为 warning（`render_session.py:807-811`）。`video.path` 只做 `Path` 拼接和 `is_file`，绝对路径或 `../` 可以指向会话目录外（`session_format.py:504-511`）；这是本地文件读取面，不是网络面。

并发假设是“同一会话只有一个 Writer、一个渲染器”。代码没有文件锁：第二个 Writer 会以 `"w"` 截断先写者；两个渲染器会互相覆盖 `base-*.wav`、`mix.m4a`、`render.json`、`final.mp4`。录制器内部只有一条 WS 循环和一个 FrameSink 线程，`_latest` 用锁保护，但 Writer 本身不是线程安全的。

回滚/降级开关包括：`--capture none` 只记事件、`--audio-device` 留空不录音、`--no-agents` 不拉起智能体、`--no-start` 不自动启动实时会话、`--keep-live` 结束时不停止实时会话、`--duration` 限时。没有“运行中禁止写 session.jsonl”的开关，也没有“写失败转备用文件”的开关。

安全边界方面，录制器只连本机 `127.0.0.1:30648` 的 WS/HTTP，显式把回环地址加入 `no_proxy`（`record_session.py:62-78`）；令牌放在 WS URL query 和 HTTP Bearer 中，`source.ws` 经 `_redact` 去掉 query 后落盘（`record_session.py:818-821`）。但 `--token` 会进命令行/进程列表，文档自己也提示不建议这样用。

### 13.7 成熟度结论

| 能力 | 状态 | 证据 |
|---|---|---|
| Writer/Reader/validate、口型、prosody 往返 | 【已实现且有测试】 | `test_session_format.py`，本次已随 session 套件执行 |
| 帧合成录制、事件翻译、收尾 | 【已实现且有测试】 | 假 WS 端到端 `test_record_session.py` |
| `run_episode` 进程接线 | 【已实现且有测试】 | FakeProcess 6 个测试 |
| 窗口直录、dshow 原音、重连、HTTP 启停、真实 BetterGI 协议 | 【已实现但未实机验证】 | 无真实设备/服务证据 |
| `frame`/`note` 生产者、渲染器写回 `audio`、严格音画同步、强杀修复 | 【目标设计/未实现】 | 只有类型/文档或缺失 |

一句话总结：事件格式和帧合成链路已经具备可评审的工程形态，但“唯一权威记录”目前只保证“正常情况下可读”，不保证“异常情况下可恢复”；“统一时间轴”在事件层成立，在画面与原音层仍是近似。

## 14. 后期渲染、TTS、音频与 VTuber

> 状态标记同第 13 节：有测试不等于本次环境已执行通过。本节审计对象是 `integrations/session/render_session.py` 及其上游 `record_session.py`、飞鱼 worker 和 stage4/5 产物。

先厘清一个容易误解的边界：审计材料把这个子系统称为“PersistentPlayer 会话后期流水线”，但 PersistentPlayerAgent 的 shadow 记录并不写 `session.jsonl`，也不调用 `render_session.py`。真正被渲染的是“导演 + MotorController（Lumine 动作执行器）+ BetterGI Live Hub”产出的正式会话。因此第 14 节的 TTS、字幕、混音、头像叠加，目前都与 shadow 玩家的解说策略没有端到端连接。

### 14.1 输入、输出与主流程

```text
session.jsonl ──read_session/validate(仅警告)──┐
gameplay.mp4 ──ffprobe（缺失→黑底）───────────┤
audio/game_audio.*（可选）────────────────────┤
                                              ▼
飞鱼批量 TTS 或 SAPI/SVC → 逐句 atempo 对齐 → 排程
                                              ▼
ASS 字幕 → ffmpeg 混音（可选 sidechain ducking）→ 叠字幕/头像
                                              ▼
final.mp4 + subtitles.ass + audio/* + render.json
```

主流程顺序固定在 `render_session.py:798-845`：校验、探测视频、飞鱼批量、逐句 TTS/SVC/atempo、排程、算总时长、写字幕、混音、合成画面、最后写报告。输入只读；派生文件失败或重跑不应改写 `session.jsonl`。这个契约在正常路径成立，但有一个 `--out` 例外，见 14.7。

`validate` 失败只记 warning 并继续（`render_session.py:807-811`），所以缺 `end`、未知事件类型、部分坏字段不会阻止渲染；但 `t/dur` 是字符串等类型错误可能在后面的 `int()` 处抛出未捕获异常，用户看到 traceback 而不是友好错误。

### 14.2 TTS 选择：飞鱼、SAPI、SVC 与逐句语速

`--tts auto` 的检测只看三个路径：飞鱼解释器、`feiyu_tts.py`、基础模型目录下的 `cosyvoice3.yaml`（`render_session.py:764-779`）。三者齐全就选飞鱼，否则 SAPI。它不检查 cute 权重、参考音频或显存；这些缺失会在模型加载阶段才失败。显式 `--tts sapi`、`--tts feiyu`、`--tts none` 分别对应系统语音、飞鱼和纯字幕。SVC 只允许与 SAPI 组合，飞鱼本身已生成角色音色（`render_session.py:781-789`）。

飞鱼采用“一次加载、批量合成”，`_prepare_feiyu_batch` 把所有台词写进 `audio/feiyu-request.json`，启动一次 worker，返回 `{序号: wav}`（`render_session.py:894-984`）。这是一个明确的替代方案选择：逐句启动 worker 实现简单、单句失败隔离好，但 4GB 级模型每句重载一次不可接受；批量只加载一次，代价是部分失败处理更复杂。worker 内部逐句 `try/except`，单句失败写 `error` 不拖垮整批（`E:/BetterAGI/cosyvoice/feiyu_tts.py:260-308`）。

“飞鱼逐句语速”不是每句独立模型参数，而是 `request.texts[].speed = feiyu_speed × pace_factor`：`slow` 乘 0.92，`fast` 乘 1.08，`normal` 乘 1.0（`render_session.py:912-927`、`622-626`）。worker 再把单句速度夹到 0.5–1.5（`feiyu_tts.py:277-280`）。这只能改变语速，不能改变停顿、重音或情绪；真正的 prosody 端到端没有接通：`live_director.normalize_commentary` 只保留 `type/kind/text/emotion/frameSeq`（`integrations/lumine/live_director.py:572-608`），`record_session._on_commentary` 也不转发 `pace/volume/pause/intensity/interruptible`（`record_session.py:647-681`）。PersistentPlayerAgent 的 `SpeechPolicy` 只在 shadow 阶段记录决定（`integrations/player/player_speech_policy.py:5`、`70-80`），不进入 `session.jsonl`。因此生产会话里这些字段通常不存在，逐句语速目前主要靠手工会话或测试触发。

SAPI 路径把台词写入 UTF-8 临时文件，再用 UTF-16LE Base64 的 PowerShell `-EncodedCommand` 调用 `System.Speech`，语音名和路径按单引号字面量转义（`render_session.py:386-446`）。默认语音固定为 `Microsoft Huihui Desktop`（`render_session.py:73`），没有“列出系统语音并回退”的逻辑；换机器或精简系统可能直接失败。SAPI 与 `--tts none` 有真实 ffmpeg/SAPI 集成测试，飞鱼只有假 worker 协议测试；真实飞鱼模型、真实 SVC 没有自动化验收。

部分句失败时的行为需要说清楚：批量整体失败且没有 `--allow-tts-fallback` 时直接 `RenderError`；有该开关则整体回退 SAPI。批量成功但个别句缺失时，只有同一开关才会逐句回退 SAPI，否则报错（`render_session.py:1028-1045`）。即使逐句回退了 SAPI，`effective_tts` 仍是 `feiyu`，报告仍写 `tts=feiyu`、`voice=null`，无法从 `render.json` 看出哪几句实际用了 SAPI。这是一个报告语义缺口。

### 14.3 atempo 对齐与排程级联

每条 `speech` 有录制期预估的 `dur`。渲染器用 `ffprobe` 测实际音频时长 `actual_ms`，计算 `ratio = actual_ms / dur`；只有 `0.75 ≤ ratio ≤ 1.5` 且偏离 1 超过 0.005 时才用 `atempo` 拉伸，否则保留原速并写警告（`render_session.py:1078-1097`、`543-569`）。这是一个刻意的质量取舍：超出范围说明文本与语速差异过大，强行拉伸会明显失真；替代方案是强制拉伸到 `dur`，或改写台词/重新合成，但前者损害听感，后者需要人工介入，所以当前选择“保留原速 + 报告警告”。

问题在于“保留原速”会破坏口型、音频、字幕三者共用工时轴的承诺。stage4 的首句给出实测：`dur=3005ms`，飞鱼实际 `1920ms`，`ratio=0.6389`，`tempo_applied=false`（`E:/BetterAGI/tools/pipeline_smoke/sessions/stage4/render.json`）。此时音频和字幕按 1920ms 走，而口型包络仍按 `dur=3005ms` 生成；未来 Live2D 回放会在 1920ms 后继续张嘴约 1 秒。设计文档所说“声音、字幕、口型共用一条时间轴”只在 `atempo` 生效时成立。

排程公式是 `start=max(t, cursor)+pause_before_ms`，`cursor=start+final_ms+120ms`；顺延超过 50ms 才写 warning（`render_session.py:1107-1127`）。这保证相邻配音不重叠，但密集解说会级联后移。stage4 的第 3–8 句分别顺延 849/1619/1635/2451/3202/3234ms，最后一句结束时超出会话总时长 3218ms，渲染器用片尾静止画面补足。字幕和声音仍互相同步，但已经远离原始事件时间；`interruptible` 只被记录，不执行抢占或丢弃。

总时长取 `max(session.duration_ms, 最后一句结束, 1000ms)`；视频用 `tpad` 补头/补尾再 `trim` 到同一总时长（`render_session.py:1129-1155`、`1305-1328`）。这避免切掉半句台词，但会让“成片时长等于会话时长”的预期失效。

### 14.4 ASS 字幕

`build_ass` 生成完整 ASS：`PlayResX/Y` 取视频探测到的画布（缺失则 1280×720），默认样式字号 34；按 `kind` 生成主色、按 `emotion` 生成描边色，每条 `speech` 一个 `Dialogue`，带 `\fad(160,220)`，文本转义反斜杠、花括号和换行（`render_session.py:629-707`、`593-601`）。文件以 UTF-8 BOM 写出（`render_session.py:1176-1178`）。

字幕时间不是 `t + dur`，而是 `start_ms` 到 `start_ms + max(400ms, final_ms)`（`render_session.py:1163-1168`）。所以 atempo 生效时字幕跟随实际音频，atempo 不生效时字幕比口型短；这与 14.3 是同一个根因。字幕只做单行 `Dialogue`，没有按字数主动换行，长文本依赖 libass 的 `WrapStyle: 0`；字体缺失时没有存在性检查，只能依赖系统回退。

为什么用 ASS 而不是 SRT？ASS 支持颜色、描边、淡入淡出和按 `kind/emotion` 分样式，且可以直接烧录进画面，交付物只有一个 `final.mp4`；SRT 更通用但样式能力弱，还需要播放器或额外烧录步骤。代价是 ASS 的转义、字体和换行都更脆弱，而现有测试只断言字幕文本出现和 Dialogue 数量，没有验证样式、转义、换行或时间轴边界。

### 14.5 游戏原音、混音与 sidechain ducking

游戏原音的发现优先级是：`--no-game-audio` 关闭；显式 `--game-audio` 优先；否则自动扫描 `audio/game_audio.{m4a,wav,mp3,flac,ogg}`（`render_session.py:849-863`）。显式路径不存在时静默返回 `None`，不会报错；这与“用户明确指定了文件”的预期不符。更关键的是，渲染器完全不读 `audio` 事件，也不读 `record_session` 记录的设备/偏移，只从 0ms 开始 `atrim` 混入（`render_session.py:1230-1237`）。第 13 节说的 AudioSink 首采样偏移，在这里没有任何补偿。

混音链是：`anullsrc` 48kHz 立体声底；每条配音 `aresample + aformat + volume + adelay(start_ms)`；`amix normalize=0`；如果有游戏原音且存在语音，把语音 `asplit` 成 `[sp][sc]`，用 `[game][sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[ducked]` 压低原音，再与语音 `amix`（`render_session.py:1183-1252`）。没有 `alimiter` 或 `loudnorm`；`volume=loud` 时增益 1.15（`render_session.py:1010-1014`），语音叠加游戏原音可能削波。sidechain 的阈值/比例/攻击/释放是固定常量，没有按素材响度校准。

替代方案是固定音量包络或把原音整体降一个固定 dB。固定降增益实现更简单、可预测，但会一直压住游戏音；sidechain 只在语音出现时压低，听感更自然，代价是参数未实机调过，且当前没有任何混音测试。stage4/5 的 `render.json` 都是 `game_audio=null`，真实 dshow 原音、真实 sidechain 输出都没有样本。

### 14.6 VTuber 叠加：静态 PNG，不是 Live2D

`--avatar-image` 只做静态 PNG 叠加：缩放到宽 400，放右下角，留 24px 边距（`render_session.py:1300-1336`）。渲染器完全不读 `avatar` 事件、`speech.avatar.mouth_open`、`pose` 或 `blink`。也就是说，录制器辛苦生成的 VTube Studio 参数目前是 write-only：会话文件里有完整数据，成片里没有任何消费者。

为什么不做 Live2D 离线渲染？替代方案是接 VTube Studio 或内嵌 Live2D Cubism SDK，按 `t` 驱动参数并逐帧渲染；它能把口型、眨眼、姿态真正变成画面，但需要额外运行时、模型文件、授权和确定性控制，还会把“离线可复现”变成“依赖某个桌面应用”。当前选择静态头像，保证渲染只依赖 ffmpeg 和图片，代价是“口型同步”只存在于数据契约里。文档也承认 Live2D 离线渲染未实现（`Docs/design/system-status.md:78`）。这是典型的【目标设计/未实现】。

### 14.7 报告、失败模式、并发与回滚

`render.json` 字段很全：会话标识与输出路径、`duration_ms`、实际/请求的 TTS 引擎、SAPI 的 `voice/rate`、`feiyu{style,speed,seed,load_seconds}`、`svc{model,applied,command}`、`subtitles{path,burned,font,lines}`、`audio_mix`、`game_audio`、`video{source,offset_ms,width,height,fps}`、`speeches[]` 与 `warnings[]`；每条 speech 记录 `t/start_ms/shift_ms/dur_ms/base_audio/svc_audio/final_audio/actual_ms/final_ms/tempo_ratio/tempo_applied/svc_applied/volume/pause_before_ms/pace/interruptible/warnings`（`render_session.py:1387-1463`）。但报告只在 `_render_video` 成功且 `final.mp4` 非空后才写（`render_session.py:838-842`）；失败时没有新报告，如果目录里留着上次成功的 `render.json`，会与本次失败不匹配。

| 失败 | 当前行为 | 可观测性 |
|---|---|---|
| 缺 `gameplay.mp4` 或无视频流 | 黑底，继续 | warning + `video.source=black` |
| 缺 `end` | `validate` 警告，时长用最后事件兜底 | warning |
| 飞鱼整体失败 | 默认 `RenderError`；有 `--allow-tts-fallback` 才回退 SAPI | stderr；无报告 |
| 飞鱼单句失败 | 有开关则逐句回退 SAPI，否则报错 | warning；报告仍标 `tts=feiyu` |
| atempo 超界 | 保留原速，`final_ms=actual` | warning；口型仍按 `dur` |
| SVC 失败 | 有开关回退 SAPI 基础音频 | warning |
| 游戏原音损坏 | ffmpeg 混音返回非零，整体失败 | `RenderError`；无报告 |
| 坏会话/坏 JSON | `read_session` 抛错，构造函数转 `RenderError` | 友好错误；无报告 |
| 字段类型异常 | `int()`/`float()` 抛未捕获异常 | traceback |

并发与原子性：`final.mp4`、`render.json`、`mix.m4a` 都是直接覆盖，没有临时文件+rename，也没有文件锁；两个渲染进程对同一会话并发会互相破坏。`--out` 未做保护：如果指向 `session.jsonl`，`ffmpeg -y` 会覆盖权威记录（`render_session.py:744-745`、`1283`）；相对路径还可以 `../` 逃出会话目录。这和“绝不修改 `session.jsonl`”的承诺冲突，是高风险的使用边界。

回滚/降级开关：`--tts none` 只出字幕和画面；`--no-game-audio` 不混原音；`--no-subtitles` 生成但不烧录；`--allow-tts-fallback` 允许飞鱼/SVC 回退；`--out` 另存成片。重跑会覆盖中间文件和成片，没有 dry-run，也没有“保留上一版成片”的开关。

### 14.8 成熟度结论与未验证清单

| 能力 | 状态 |
|---|---|
| SAPI 合成、`--tts none`、ASS 基本输出、排程/pause、game_audio 自动发现、pace 映射 | 【已实现且有测试】 |
| 假飞鱼批量协议、auto→SAPI 回退 | 【已实现且有测试】 |
| 真实飞鱼模型成片 | 【已实现但未实机验证】（stage4/5 有产物，不是自动化验收） |
| 真实 SVC 推理 | 【已实现但未实机验证】（只测命令构造） |
| 真实 dshow 游戏原音、sidechain ducking、头像 overlay | 【已实现但未实机验证】 |
| Live2D/VTube Studio 参数驱动、prosody 端到端、失败时写报告、音画严格同步 | 【目标设计/未实现】 |

当前环境没有安装原神，AutoDL 已停止，真实飞鱼/SVC 也没有自动测试；因此不能把 stage4/5 的 `final.mp4` 当作“真实游戏 + 真实 TTS + 真实混音已通过”的证据。它们能证明的是：在假 Hub 会话和现有模型环境下，渲染器确实能产出成片和报告。

## 15. 安全、隐私与合规边界

### 15.1 先定义“shadow”，否则安全承诺不可检验

PersistentPlayerAgent（玩家主体）的默认承诺是“只读认知层”：它不持有 BetterGI 动作/指导/解说写客户端，输出只写独立 shadow 记录。但这条承诺只在两个可选开关都不开时严格成立：

| 运行形态 | 额外出站 | 状态 | 边界结论 |
|---|---|---|---|
| 纯 shadow（无 OCR、无 intent） | BetterGI Live WS 读 + LLM HTTPS | 【已实现且有测试】 | 不产生游戏输入；但截图、OCR 文本、记忆会离开本机 |
| `--observe-ocr` | loopback `POST /api/v1/agent/observe` | 【已实现且有测试】假服务端；真实 BetterGI 未验证 | 只读观察但非零副作用：BetterGI 侧会写观察事件 |
| `--intent-url` | loopback `POST /intent` → Bridge → AutoDL → MotorController（Lumine 动作执行器）→ BetterGI | 【已实现但未实机验证】 | 可间接改变游戏；记录仍写 `mode=shadow`、`sent=false` |
| Active 解说/TTS/正式录制接线 | 无 | 【目标设计/未实现】 | 当前不存在 |

因此，`sent=false` 是“PlayerAgent 没有直接发送”的记录，不是“这一场没有发生动作”的证明。启用 intent 总线后，游戏动作由 Bridge/AutoDL/MotorController 产生，PlayerAgent 只是没有亲自按按键；这是评审必须接受的语义收缩。

合规上，本流水线的技术范围是屏幕截图/OCR + 模拟键鼠，不读写游戏内存、不抓包、不注入、不执行游戏脚本；C# 执行器在注入前检查前台、最小化、帧新鲜和停止开关（`LiveActionExecutor.cs:390-394`）。这不等于游戏服务条款允许，也不等于第三方模型/语音服务条款允许；上线前需要项目所有者单独确认账号风险、截图与语音的数据处理、VTuber 形象/声音授权。本文不构成合规结论。

### 15.2 信任边界

```text
BetterGI Live WS（不可信消息）
        │ frame/guidance/commentary/action
        ▼
PersistentPlayerAgent ──► LLM HTTPS（外部；截图+记忆+prompt）
        │                         │
        │                         └─ 返回任意文本（不可信）
        ├─► state/memory/evidence（半可信本地文件）
        ├─► shadow JSONL（本地证据）
        └─► 可选 loopback observe / intent
                    │
                    ▼
        Bridge（可配非 loopback、token 可空）
                    │
                    ▼
        AutoDL server（默认 0.0.0.0、token 可空）──► vLLM
                    │
                    ▼
        BetterGI C# 执行器 ──► 游戏
```

信任分级在代码里是存在的：Live 消息、LLM 输出、OCR 文本都被当作不可信；profile/state/memory 被当作“半可信、可手工编辑”；Bridge 意图只做最小 id/goal 校验。问题在于信任分级没有形成统一的出站能力对象，靠分散校验和静态测试维持。

### 15.3 威胁模型与现有缓解

| 编号 | 威胁 | 现有缓解（证据） | 残余风险 |
|---|---|---|---|
| T1 | 令牌/密钥泄漏到仓库、日志、记录、prompt | token 只从 CLI/`--token-file`/环境变量读取，JSON 中的 token 被忽略（`player_config.py:344-348`）；`build_ws_url` 清掉旧 token（`player_config.py:30-56`）；profile 拒绝 secret 键（`player_profile.py:330-340`）；state 保存剥离（`player_state.py:191-198,232`）；shadow 记录递归脱敏（`shadow_recorder.py:27-68`）；异常日志 `_safe_error`（`run_shadow.py:158-166`） | `integrations/lumine/bridge_config.json:3,5` 仍有 `bettergiToken`/`lumineToken` 字面量；`run_shadow.py:433-454` 通用异常仍可能原样打印；belief/relationship/emotion/thread 加载与 session recorder 异常没有同等脱敏 |
| T2 | 越权动作：shadow 间接控制游戏 | PlayerAgent 没有 send 方法，结构断言 `test_player_agent.py:219`；静态 AST `test_run_shadow.py:162-203`；intent/observe 强制 loopback（`player_intent.py:212-221`、`player_vision.py:41-45`）；C# 执行器安全门（`LiveActionExecutor.cs:141-171,390-394`） | `--intent-url` 打开后 Bridge→AutoDL→MotorController 可产生真实输入；Bridge 的 `intent_host` 可配非 loopback（`lumine_bridge.py:1556`），`intent_token` 为空时直接放行（`lumine_bridge.py:925-927`）；AutoDL 默认 `0.0.0.0`（`autodl_server.py:76,890`），token 空时放行（`autodl_server.py:671-675`）；C# 动作回显无 `intent_id` |
| T3 | 剧透进入 prompt | profile 拒绝 world/ground_truth 键且 `no_spoilers=false` 直接拒绝（`player_profile.py:336-340,441-442`）；fact 只接受 `player_knowledge`（`player_semantic_memory.py:97-101`）；retrieval/ContextBuilder 再过滤；旧导演记忆导入隔离区 | `agent.close` 用未过滤的 `semantic_store.list()` 生成 recap 并注入下一场（`player_agent.py:201-213`；`player_recap.py:126-131`）；guidance/profile 自由文本、手工导入事实是受信输入；`SemanticStore.add` 缺 visibility 时自动补 `player_knowledge`，source 只要求非空（`player_semantic_memory.py:220-226`） |
| T4 | 恶意/伪造 WS 消息与模型输出 | Live 消息按 type/长度/字段分派，未知消息忽略；C# 单条消息 4MB（`LiveWebSocketHandler.cs:27`）；LLM 输出宽容解析、限长/枚举/钳制；提案永不执行；OCR 必须逐字引文 | player `ws_connect(max_msg_size=0)`（`run_shadow.py:204-205`），jpeg 只检查非空（`player_agent.py:338-339`）；没有完整 schema；恶意本地 WS 可注入 guidance/commentary 文本，模型输出可污染 episode/recap；没有对模型输出做内容级剧透/指令过滤 |
| T5 | 路径/文件/损坏：路径穿越、半截 JSON、串玩家、并发覆盖 | `safe_session_name` 限制 session 文件名（`shadow_recorder.py:81-87`）；evidence 文件名用 sha256（`player_knowledge.py:67-68`）；`atomic_write_json` 同目录 temp+fsync+replace（`player_io.py:73-129`）；schema/player_id 校验（`player_state.py:203-217`）；损坏备份 | 只有 working/semantic 用 `file_lock`；state/belief/relationship/emotion/thread 没有跨进程锁；`file_lock` 超时会删除锁强行接管（`player_io.py:159-200`）；没有目录 fsync；多进程同目录会丢更新 |
| T6 | 回环绕过/重定向 | observe/intent 只接受 loopback 根地址且 `allow_redirects=False`（`player_vision.py:23-33,74`；`player_intent.py:242-247`）；OCR 与 Live 要求同主机同端口 | BetterGI Live WS URL 不受 loopback 限制（`player_config.py:336-343` + `build_ws_url`），配置被篡改时 token 可发往远端；LLM base URL 任意可配，没有域名 allowlist；Bridge/AutoDL 绑定默认不安全 |
| T7 | 隐私：截图、OCR、记忆、日志 | observe `saveFrame=false`（`player_vision.py:69`）；evidence 只存哈希与 OCR 文本，不存原图（`player_knowledge.py:33-35`）；记录脱敏 | 每 tick 可能把整张截图、profile、记忆、guidance 发往可配置 LLM 端点；本地记忆/evidence 明文落盘；JSONL/evidence 只增不轮转；无加密、无保留期限 |
| T8 | 资源/成本 | 分块预算、required 超限不调用模型；interval 钳制 1–600s；state save 30s 节流；`--duration` | 没有 token/费用硬上限；每次 tick 都可发整图；`max_msg_size=0`；JSONL/episodes 无轮转；检索每次全量扫描 |
| T9 | 审计证据被误读 | shadow 记录固定 `executed=false/sent=false`（`shadow_recorder.py:138-151`）；close 幂等（`shadow_recorder.py:124-131`）；session generation 丢弃在途结果 | `sent=false` 不能证明游戏无动作；C# 无 `intent_id`；记录没有签名；intent 未在 close 时 cancel，旧目标可能跨场保留 |

### 15.4 关键设计理由与未采用的替代方案

这里采用“多层防御”而不是“单一开关”：配置层不读 token、存储层剥离、记录层脱敏、日志层兜底、静态测试防回归。原因是任何单点都可能被绕过，而失败代价不对称——一次误发送就可能改变游戏状态，一次 token 泄漏就可能被远端利用。

替代方案 A：把出站能力收敛为一个 `GameEgress` 能力对象/独立代理进程，所有 observe/intent/LLM 调用都必须经过它，代理强制 loopback、非空 token、请求签名和域名 allowlist。未采用的原因是它要改 `player_intent.py`、`player_vision.py`、`run_episode.py`、Bridge 和 AutoDL 的启动契约，超出当前 Phase 9 的迁移范围；现有静态 AST 只是回归护栏，不是安全边界。建议作为 Active 模式的前置改造。

替代方案 B：对 LLM 端点做域名/回环 allowlist。未采用的原因是项目需要 DeepSeek、OpenAI 兼容网关和本地 vLLM 多种端点，硬编码 allowlist 会削弱可运维性；残余风险是配置被篡改时整帧与记忆可被发往任意地址。折中方案是把“端点变更”做成显式高风险开关并记录审计事件，而不是静默默认。

### 15.5 隐私与合规数据面

| 数据 | 产生/存放 | 是否出站 | 风险与缺口 |
|---|---|---|---|
| 游戏截图 JPEG | Live WS 内存；`record_session` 另写 `gameplay.mp4` | 每 tick 可发往 LLM | 可能含 UID、聊天、好友列表；无裁剪/打码；provider 保留策略未知 |
| OCR 文本/evidence | `memory/evidence/<sha256>.json` | 进入 prompt/记忆 | 明文；可能含账号信息；证据不保存原图，无法事后复核画面 |
| profile/记忆/episode/recap | `PLAYER_HOME` 下 JSON/JSONL | 进入 prompt | 明文；无加密；保留期无限；recap 会把模型自述写成“发生过” |
| 日志 | stdout/文件 | 可能进入日志采集 | 令牌脱敏不完整；guidance/commentary 原文可能被打印 |
| 语音/形象 | 渲染链路（非 player） | TTS/直播 | 声音克隆、VTuber 形象授权没有技术强制；需人工确认 |

合规建议不是“加一个开关”能解决的：至少需要明确截图是否允许离开本机、是否允许第三方模型处理、是否在直播中披露 AI 生成内容、是否获得声音/形象授权。当前代码没有数据保留/删除 API，也没有“仅本地模型”的硬约束；`OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` 可以指向任何地址（`llm_client.py:245-256`）。

### 15.6 失败模式、并发/恢复与回滚

- WS 重连：`reset_observation()` 清空旧帧/指导/动作，`_session_generation` 丢弃在途结果（`player_agent.py:440-442,578,644,670`）；但重连补发的旧帧若缺 `timestampUtc`，`_frame_age_ms` 会回退 `received_ms`（`player_agent.py:485-490`），旧帧可能被当新帧。
- 意图生命周期：`agent.close` 不发布 cancel；Bridge 的 `clear_intent` 不向 AutoDL 发 reset；AutoDL `session.intent` 可跨 live 会话保留。启用 v2 后，崩溃/重启可能留下旧目标继续驱动 MotorController。
- 强杀/断电：单文件原子写能避免半截 JSON，但没有目录 fsync；`shadow_end` 可能缺失；resume 最多丢 30 秒；锁文件残留会在 5 秒后被强行接管。
- 多进程：同一 `PLAYER_HOME` 只允许一个 shadow 写者；state/belief/relationship/emotion/thread 没有锁，两个进程会 lost update。
- 损坏恢复：state/working/semantic 有备份；episodic 损坏可能只跳过告警；belief/relationship/emotion/thread 的备份失败可能被吞掉，随后 save 覆盖原文件（审计记录）。

回滚开关（均已在代码中读取）：

| 开关 | 效果 | 备注 |
|---|---|---|
| 不传 `--player-shadow-config` / 不运行 `run_shadow.py` | 完全不启动 | 最干净的回滚 |
| `PLAYER_AGENT_ENABLED=0` 或 `--disabled` | 返回 0，不创建 profile | `run_shadow.py:316-318` |
| `PLAYER_AGENT_SHADOW=0` | 返回 2，拒绝启动 | Phase 1 只支持 shadow（`run_shadow.py:319-323`） |
| `CONTEXT_BUILDER=legacy` / `--context-builder legacy` | 回退 Phase 1 prompt | `player_config.py:324-334` |
| 不传 `--observe-ocr` / `--player-observe-ocr` | 不请求截图/OCR | 但 LLM 仍可用 Live 帧 |
| 不传 `--intent-url` / 不设 `PLAYER_INTENT_URL` | 不发布意图 | 关闭间接控制 |
| 删除/改名 `state/resume_state.json` | 回到空 resume | 不影响 profile/记忆 |
| `run_episode --no-agents` | 清空 director/lumine/shadow | `run_episode.py:177-183` |
| Bridge `MOTOR_PROTOCOL=v1` / `intentPort=0` | 关闭 v2 意图总线 | 需与 shadow 侧一致 |

文档中的 `DIALOGUE_LOCK=off`、`SPEECH_POLICY=legacy`、`MEMORY_BACKEND`、`RESUME_ENABLED` 在当前代码中不存在读取点，不能作为回滚依据；不要把它们写进操作手册。

残余风险排序：1) intent 总线使 shadow 成为间接控制路径且无 `intent_id`；2) Bridge/AutoDL 绑定与 token 不对称、`bridge_config.json` 字面量令牌；3) recap 绕过证据门禁与模型自述固化；4) 整帧/记忆发往任意 LLM 端点；5) 多进程/磁盘/长跑无轮转导致的丢更新与磁盘增长。

## 16. 测试、连续性与验收

### 16.1 本次复核的测试口径

本次在项目 venv 中重新 collect：393 tests collected = player 189 + session 26 + lumine 178。随后实际运行 Python 测试：player 189 passed（9.21s）；session+lumine 204 passed、28 subtests passed（14.06s，14 条 aiohttp 警告）；合计 393 passed。没有运行 `dotnet test`，C# 文档声称的 234 项没有本次执行证据。

这些测试全部是本地或假服务端测试，不连接 BetterGI、原神、AutoDL、飞鱼 TTS 或 VTS。因此“393 passed”只证明模块状态机、序列化、假协议接线，不证明真实协议、真实画面或真实动作。审计静态计数为 player 168 个 `def test_`、session 25 个，参数化后 collected 为 189/26；参数化会放大数量，报告必须同时给出 passed/collected，不能把数量当覆盖。

| 测试层 | 代表文件 | 数量 | 证明什么 | 不能证明什么 |
|---|---|---:|---|---|
| 单元：配置/档案/状态/存储 | `test_player_config_loop.py`、`test_player_profile.py`、`test_player_state.py`、`test_player_memory_stores.py` | 19+29+23+27 | 优先级、路径、令牌来源、schema、损坏备份、原子写、去重、并发 fact add | 真实文件系统故障、多进程、真实 BetterGI 数据 |
| 单元：解析/意图/对话/语音 | `test_player_proposal.py`、`test_player_intent.py`、`test_player_dialogue.py`、`test_player_speech.py` | 7+9+3+3 | 宽容解析、限长、单主意图、4 秒锁、冷却/情绪余韵 | 真实模型输出分布、真实对话节奏 |
| Agent 接线 | `test_player_agent.py` | 34 | shadow tick 不发送、resume、重连清场、旧帧、LLM/parse/recorder 失败降级、OCR 解锁、hold 意图、close 幂等 | 真实 LLM 视觉、真实 WS 协议、真实长跑 |
| 入口/静态边界 | `test_run_shadow.py` | 10 | disabled、默认 profile、token 脱敏、假 Hub 端到端、静态无发送、stop-file | 真实 BetterGI Hub、真实子进程 |
| 观察链路 | `test_player_observation_e2e.py` | 1 | 假 observe HTTP → 同帧 image+OCR → FakeLLM 引用 evidence → KnowledgeGate 解锁 → shadow_tick/end | 真实 OCR、真实 `observe` DTO |
| 连续性 A–E | `test_continuity.py` | 5 | episode→recap→prompt、信念证伪、线索重激活、情绪余韵、world_only 拒绝 | 真实 3 session、真实 PlayerAgent、真实进程重启 |
| 会话/录制/渲染 | session 26 项 | 26 | session 格式、假 Live 录制、ffmpeg、run_episode 收尾、SAPI/飞鱼批量/字幕排程 | 真实游戏画面、真实声卡、飞鱼听感 |
| Lumine/Bridge/AutoDL | lumine 178 项 | 178 | 动作解析、字段白名单、动作 seq、重连、AutoDL 帧校验/intent/reset | 真实 AutoDL vLLM、真实 BetterGI 执行 |
| C# ExternalAccess | 文档声称 234 项 | 未运行 | 代码可读的 seq/owner/队列/安全门 | 本次没有任何执行证据 |

### 16.2 测试金字塔与本次实际位置

```text
真实游戏 + BetterGI + AutoDL + TTS + 盲测     未执行
3 个真实 session、跨进程重启                  未执行
30–60 分钟连续实况                            未执行
────────────────────────────────────────────
假服务端接线 / A–E 模块回归                  393 passed（本次）
单元：存储/配置/解析/去重                     …
```

A–E 是当前“连续性”的主要证据，但它位于金字塔底部偏上：有状态机与 JSON 落盘，没有真实 agent tick。审计明确把 PHASE_9 文档中的“跨 session/重开进程”降级为“单进程、假数据、缩小版回归”；这个降级是必要的事实修正。

### 16.3 A–E 连续性到底验证了什么

`test_continuity.py:51-133` 的五个测试：

| 用例 | 实际链路 | 断言 | 缩小点 |
|---|---|---|---|
| A | `EpisodicStore.add` → `build_recap` → `RecapStore` 保存/重载 → `MemoryRetriever` → `ContextBuilder` | prompt 含“安柏帮我们解了围”和“上一场回顾” | 不启动 PlayerAgent，不调用 LLM，不跨真实 session |
| B | `BeliefStore` 写入 suspicion → disproven → 重开文件 | 保留 `history`，summary 含“猜错了” | 只测信念存储，不测模型如何产生证伪 |
| C | `ThreadStore` open → dormant → 手工 `activate` → 新 store 读取 | 重新变为 open | 生产代码没有调用 `ThreadStore.activate()`，休眠线索不会因检索命中自动重开 |
| D | `EmotionStore` 连续 sadness → 30 秒后 `SpeechPolicy.decide` | `mood_damped=True`，pace 不是 fast | 不测真实剧情与语音听感 |
| E | 手写 `facts.json` 的 `world_only` fact → `SemanticStore` → retrieval → builder | store.list 为空，prompt 不含该文本 | 该 fact 在 normalize 阶段已被丢弃，未验证“合法 world_only 在检索层被硬过滤”的完整路径 |

结论：A–E 证明“状态机与持久化没有在换场时丢东西”，不证明“同一个玩家在真实三场里连续”。尤其 C 的“重开进程”实际只是新建 store 实例；B/C 没有 PlayerAgent 参与。

### 16.4 假替身与静态无发送测试

实际存在的假替身包括：`FakeLLM`/`GatedLLM`/`CountingStore`/`Clock`（agent）、`FakeWS`/`FakeMessage`/`RecordingAgent`（run_shadow）、`fake_live_hub`（aiohttp WS 假服务端）、`FakeProcess`/`GracefulProcess`（run_episode）、`FakeResponse`/`FakeSession`/`FakeAiohttp`（dialogue）、`FakeLiveServer`（record_session）、`RecordingClient`（director）、`FakeClient`（memory_bridge）。`fake_live_hub` 不是 mock 对象，而是按当前 Live 消息形状构造的 WS 服务端，并有 `test_fake_hub_matches_real_payload_contract`（`test_run_shadow.py:149`）与假 Hub 端到端（`test_run_shadow.py:218`）。但 TEST_PLAN 中列出的 `FakePerception`/`FakeMotor`/`FakeOpenViking`/`SyntheticSession` 并未以这些名字实现。

`test_player_modules_have_no_outbound_game_calls`（`test_run_shadow.py:162-203`）的实际范围是：

- `package.glob("player_*.py")` 加 `run_shadow.py`、`shadow_recorder.py`；
- 按属性名黑名单检查 `send_json`/`send_str`/`send_bytes`/`send_frame`/`stop_actions`；
- 非 allowlist 模块禁止 `post`/`put`/`patch`/`delete`；
- `player_intent.py`/`player_vision.py` 是 allowlist，只额外断言不含 `/api/v1/agent/live/`、`/api/v1/agent/runtime/`、`stop_actions`。

盲区很明确：`.request("POST")`、通用 `send`、`getattr` 动态调用、`session`/`lumine` 模块都不在范围内；黑名单按属性名，改名即可绕过。`test_player_agent.py:219` 的 `hasattr(agent, "send"/"send_action"/...)` 是结构性补充，但只针对 agent 实例。静态测试是回归护栏，不是安全边界；替代方案是运行时能力对象或统一出站代理，未采用原因见 15.4。

### 16.5 未覆盖/未执行清单

- 真实 BetterGI Live/observe 响应形状、真实原神画面、真实 OCR 质量与时序、真实 DeepSeek/OpenAI 视觉调用、真实 AutoDL motor 模式、真实飞鱼 TTS/VTS、C# ExternalAccess 234 项。
- 30–60 分钟连续实况；3 个真实 session 的重启/迁移；盲测“是否同一个玩家”。
- 崩溃/强杀/断电/磁盘满/目录权限/多进程同时写；state/episode/belief/relationship/emotion/thread 的 corrupt/future-version；`file_lock` 超时接管。
- IntentPublisher 对假 Bridge 的 publish/fetch_feedback 真实 HTTP；对话结束 hold 取消；OCR 失败回退 Live 帧；`--observe-ocr` 全链路进程测试；required+extras 组合。
- shadow JSONL/episodes 轮转与内存增长；真实 LLM 客户端超时/限流分类。
- C# LastActionSeq 在 live 重启后的行为；C# 测试未运行。

`.pytest_cache/v/cache/lastfailed` 里的旧失败名在当前代码中已不存在，不能作为“现在仍失败”的证据；反过来，也不能把缓存当作通过证据。

### 16.6 验收门：393 是门槛，不是验收

| 门 | 内容 | 当前状态 |
|---|---|---|
| G0 | Python 393 项通过（含无 skip/fail） | 本次已通过；仅本地/假服务端 |
| G1 | C# ExternalAccess 234 项通过 | 未执行 |
| G2 | 真实 3 个 session 重启连续性 | 未执行 |
| G3 | 30–60 分钟长跑 | 未执行 |
| G4 | 盲测“同一个玩家” | 未执行 |
| G5 | 故障注入：强杀、磁盘满、权限拒绝、双 shadow | 部分：单测覆盖 save/recorder/LLM/parse 失败，未覆盖强杀/磁盘/多进程 |
| G6 | 安全回归：非 loopback、空 token、令牌泄漏、intent 根路径 | 部分：单测覆盖脱敏/证据/loopback/静态无发送；未做真实 Bridge/AutoDL 鉴权联调 |

建议的通过标准：

- G2：第 1 场 episode 能在第 3 场 prompt 中被召回；第 2 场不出现第 1 场未清理的 guidance/action；每场都有 `shadow_end`；`world_only` 不出现。
- G3：30–60 分钟内没有超过 `maxFrameAgeMs` 的帧被送入 LLM；没有未处理异常；recap/resume/working 正常保存；JSONL/episode 增长在可接受范围。
- G4：成对样本盲评，评审者不能稳定区分“同一玩家”与“不同人格”；盲测协议、样本量和判定标准需先写死，不能事后解释。
- G5：强杀后不出现半截 JSON；重启后损坏文件有 `.corrupt-*` 备份；双进程不产生静默 lost update。
- G6：Bridge/AutoDL 在非 loopback 或空 token 时拒绝启动/拒绝请求；日志和 shadow 记录不含令牌；intent URL 带 `/intent` 时明确报错。

### 16.7 测试策略的失败模式与替代方案

当前策略是“模块级单测 + 假服务端 + 缩小版连续性”，不是“真实 E2E 优先”。未采用 E2E 优先的原因很实际：本机未安装原神、AutoDL 已停止，真实链路无法稳定执行；如果只写 E2E，开发会被环境阻塞。代价是假替身可能漂移：`fake_live_hub` 是按当前代码理解构造的，真实 BetterGI 的 hello/action 回显/OCR 时序可能不同；Phase 0.5 已出现过“fake 形状掩盖真实 bug”的记录。

失败模式：

- 假服务端与真实 DTO 漂移，393 全绿但真实协议失败；缓解是抓真实 DTO fixture、做契约测试，目前未做。
- A–E 通过但 agent tick 接线错误；缓解是增加一个完整假 agent tick，并用真实 3 session 验收。
- 静态 AST 通过但 `session.post`、`getattr` 或 session/lumine 模块绕过；缓解是运行时能力对象。
- 测试数量被参数化放大，误把 collected 当覆盖；报告必须同时给出 passed/skipped/failed 与用例清单。

测试失败时的回滚顺序：先关闭 shadow（不传配置），再回退 `CONTEXT_BUILDER=legacy`，再关闭 intent/OCR；录制链路保持原样，避免为了修 shadow 影响 session 产物。

## 17. 部署、配置与运维

### 17.1 拓扑与当前状态

```text
BetterGI（30648 Live WS + HTTP observe）
  ├─ run_episode.py（录制线程）── sessions/<id>/session.jsonl、gameplay.mp4
  ├─ live_director.py（可选）
  ├─ lumine_bridge.py（可选）── AutoDL 8010 ── vLLM 8000
  └─ run_shadow.py（可选）── LLM HTTPS
```

| 项目 | 当前状态 | 证据/影响 |
|---|---|---|
| 原神客户端 | 未安装 | 无法做真实画面/OCR/动作验收 |
| AutoDL/vLLM | 已停止 | 8010/8000 无服务，MotorController 真实链路不可用 |
| BetterGI Live 30648 | 审计时未监听 | 无法真实连接 Live/observe |
| OpenViking 1933 | 审计时未监听 | 当前也被显式禁用 |
| 默认 `PLAYER_HOME` | 目录存在但无运行文件 | 生产 shadow 尚未在默认目录真正启动过 |
| Python 测试 | 393 passed（本次） | 仅本地/假服务端 |
| C# 测试 | 未运行 | 文档 234 项无本次证据 |

### 17.2 前置条件

| 依赖 | 要求 | 说明 |
|---|---|---|
| Windows | Windows 10/11 | BetterGI 是 WPF 桌面程序，按项目约定需管理员权限；player 本身不需要管理员 |
| BetterGI | 已启动 Live/ExternalAccess，30648 可访问 | 需要有效访问令牌；Live 未启动时 shadow 会重连 |
| Python | 项目 venv（如 `E:/BetterAGI/Amaidesu/.venv`） | `run_episode` 用 `sys.executable` 启动子进程，venv 必须同时装好 player/session/lumine 依赖 |
| aiohttp | 必需 | 缺失时 `run_shadow` 返回 2（`run_shadow.py:306-308`） |
| LLM | OpenAI 兼容或 Anthropic | `--no-llm` 可不配密钥，但只记录观察心跳 |
| ffmpeg/ffprobe | 录制/渲染需要 | session 测试在缺失时会 skip |
| AutoDL/vLLM | 可选 | 仅在需要真实 MotorController 时 |
| TTS/VTS | 可选 | 飞鱼、SAPI、VTS 均未在本次环境验证 |

### 17.3 启动命令

```bash
# 0) 只验证链路，不调用 LLM（不需要模型密钥）
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/run_shadow.py \
  --config integrations/player/player_config.example.json --no-llm

# 1) 纯 shadow + LLM
export BETTERGI_AGENT_TOKEN="<token>"
export LLM_PROVIDER=openai
export OPENAI_API_KEY="<key>"
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-flash
export LLM_MAX_TOKENS=4096
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/run_shadow.py \
  --config integrations/player/player_config.example.json

# 2) 增加低频 OCR（非零副作用）
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/run_shadow.py \
  --config integrations/player/player_config.example.json \
  --observe-ocr --observe-http http://127.0.0.1:30648

# 3) 增加 intent 总线：必须是根地址，不能带 /intent
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/run_shadow.py \
  --config integrations/player/player_config.example.json \
  --intent-url http://127.0.0.1:30700 \
  --intent-token "$BRIDGE_INTENT_TOKEN"

# 4) 录制 + 导演 + Bridge + shadow
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/session/run_episode.py \
  --token-file E:/BetterAGI/tools/bettergi_token.txt \
  --director-config integrations/lumine/director_config.json \
  --lumine-config integrations/lumine/bridge_config.json \
  --player-shadow-config integrations/player/player_config.example.json \
  --player-observe-ocr --duration 1800

# 5) 只录制
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/session/run_episode.py \
  --token-file E:/BetterAGI/tools/bettergi_token.txt --no-agents

# 6) 测试
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest -q integrations/player/tests
```

注意：`integrations/player/README.md:182` 的 `--intent-url http://127.0.0.1:8011/intent` 会被 `IntentPublisher` 拒绝，因为它要求 path 为空或 `/`（`player_intent.py:220-221`），publish 自己拼 `/intent`（`player_intent.py:242-247`）。另外 `run_episode.py` 没有 `--intent-url`/`--intent-token` 透传（`run_episode.py:116-130`），当前无法一条命令同时启用意图总线；要么单独运行 shadow，要么先补参数。

### 17.4 配置优先级与配置表

优先级是 CLI > 环境变量 > JSON 配置 > 默认值（`player_config.py:3,243`）。相对路径大多按配置文件所在目录解析；`PLAYER_HOME` 单独解析，CLI 相对路径也会受配置目录影响（`player_config.py:243-264,158-164`）。这是常见踩坑点：不要把“当前目录”当成所有相对路径的基准。

| JSON 键 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `PLAYER_AGENT_ENABLED` | `true` | `0`/`false` 返回 0 |
| `shadow` | `PLAYER_AGENT_SHADOW` | `true` | `0` 返回 2；Phase 1 只支持 shadow |
| `bettergiWs` | `BETTERGI_LIVE_WS` | `ws://127.0.0.1:30648/ws/v1/agent/live` | 不强制 loopback |
| `sessionId` | `BETTERGI_SESSION_ID` | `vtuber` | shadow 文件名会做安全清洗 |
| `interval` | `PLAYER_INTERVAL` | `12` 秒 | 1–600 钳制 |
| `reconnectDelay` | `PLAYER_RECONNECT_DELAY` | `3` 秒 | 0–120 钳制 |
| `maxFrameAgeMs` | `PLAYER_MAX_FRAME_AGE_MS` | `5000` | 0–60000 钳制 |
| `stateSaveIntervalMs` | `PLAYER_STATE_SAVE_INTERVAL_MS` | `30000` | 1000–600000 钳制 |
| `contextBuilder` | `CONTEXT_BUILDER` | `v2` | `legacy` 回退；非法值静默回 v2 |
| `profile` | `PLAYER_PROFILE` | `<PLAYER_HOME>/profile/profile.json` | 默认路径缺失才自动播种 |
| `state` | `PLAYER_STATE` | `<PLAYER_HOME>/state/resume_state.json` | 删除即空 resume |
| `shadowDir` | `PLAYER_SHADOW_DIR` | `<PLAYER_HOME>/state/shadow` | JSONL 目录 |
| `memoryDir` | `PLAYER_MEMORY_DIR` | `<PLAYER_HOME>/memory` | episodes/evidence 根 |
| `episodesDir` | `PLAYER_EPISODES_DIR` | `<memoryDir>/episodes` | |
| `semanticPath` | `PLAYER_SEMANTIC_PATH` | `<memoryDir>/semantic/facts.json` | |
| `workingPath` | `PLAYER_WORKING_PATH` | `<PLAYER_HOME>/state/working.json` | |
| `recapPath` | `PLAYER_RECAP_PATH` | `<PLAYER_HOME>/state/session_recap.json` | |
| `beliefPath` | `PLAYER_BELIEF_PATH` | `<PLAYER_HOME>/state/belief_state.json` | |
| `relationshipPath` | `PLAYER_RELATIONSHIP_PATH` | `<PLAYER_HOME>/state/relationships.json` | |
| `emotionPath` | `PLAYER_EMOTION_PATH` | `<PLAYER_HOME>/state/emotion_state.json` | |
| `threadPath` | `PLAYER_THREAD_PATH` | `<PLAYER_HOME>/state/narrative_threads.json` | |
| — | `PLAYER_HOME` | `E:/BetterAGI/player` | `default_player_home()` |
| — | `BETTERGI_AGENT_TOKEN` | 空 | 令牌只从 CLI/文件/环境变量读取（`player_config.py:344-348`） |
| — | `PLAYER_NO_LLM` | `false` | 只记录观察 |
| — | `PLAYER_INTENT_URL` | 空 | 也可用 `--intent-url` |
| — | `BRIDGE_INTENT_TOKEN` | 空 | 也可用 `--intent-token` |

LLM 变量来自 `llm_client.py:15-17,245-256`：`LLM_PROVIDER`（anthropic/openai）、`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`ANTHROPIC_BASE_URL`、`OPENAI_API_KEY`/`OPENAI_MODEL`/`OPENAI_BASE_URL`、`LLM_API_KEY` 兜底、`LLM_MAX_TOKENS`。不要把这些值写进 JSON 配置或仓库。

`PLAYER_OPENVIKING` 即使设为 1，也只打印“已禁用外部召回”的警告（`run_shadow.py:358-364`），不会启用 OpenViking；不要把它当成可用开关。

### 17.5 目录布局与保留

```text
PLAYER_HOME/
  profile/profile.json                 首次运行播种，可手工编辑
  state/resume_state.json              30 秒/关闭时原子保存
  state/working.json                   当前短期状态
  state/session_recap.json             上一场 recap，下一场注入
  state/belief_state.json
  state/relationships.json
  state/emotion_state.json
  state/narrative_threads.json
  state/shadow/<session>.jsonl         每次记录追加，关闭 fsync
  memory/episodes/<ep-id>.json
  memory/semantic/facts.json
  memory/evidence/<sha256>.json        只存 OCR 文本+帧哈希，不存原图
  memory/quarantine/<sha256>.json      旧导演记忆隔离副本
```

当前默认 `E:/BetterAGI/player` 只有空目录，没有 profile/state/shadow 文件；这说明“默认目录下的生产 shadow”尚未真正跑过，现有 `_player_smoke_*` 只是假 Hub 冒烟产物。所有 JSONL/episode/evidence 只增不轮转，长期运行需要外部归档；`state/shadow` 与 `episodes` 没有数量上限。

### 17.6 生命周期、重连、收尾、并发与损坏

- 启动顺序：`run_episode` 先启动录制线程，最多等 20 秒首帧（`run_episode.py:263-269`）；只要录制线程还活着，就启动 director/bridge/shadow。token 通过环境变量 `BETTERGI_AGENT_TOKEN` 传给子进程，避免出现在命令行（`run_episode.py:92-94`）。
- 收尾：先写 shadow stop 文件，给它 8 秒优雅退出；再 terminate 其他子进程，10 秒后 kill（`run_episode.py:148-174`）。Windows 的 `terminate()` 不执行 `finally`，所以 stop 文件是必要的。
- shadow 重连：WS 断开后按 `reconnectDelay` 重连并 `reset_observation()`；收到 `live.stopped` 正常结束；`close()` 生成 recap、保存 working/resume、写 `shadow_end`（`player_agent.py:196-235`）。`close()` 失败只告警，不阻止退出。
- 退出码：配置错误、缺 aiohttp、profile 非法、state/memory 错误、LLM 初始化失败通常返回 2；`enabled=false` 返回 0；`shadow=false` 返回 2（`run_shadow.py:306-333,399-407,433-454`）。
- 并发：同一 `PLAYER_HOME` 只允许一个 shadow 写者；working/semantic 有 `file_lock`，state/belief/relationship/emotion/thread 没有。`file_lock` 超时删除锁并强行接管（`player_io.py:159-200`）；多开或手工编辑会丢更新。
- 损坏：state 写入原子（`player_io.py:73-129`），损坏文件备份为 `.corrupt-*`；schema 高于程序时拒绝读取/覆盖（`player_state.py:203-217`）。episodic 损坏可能只跳过告警；belief/relationship/emotion/thread 的备份失败可能被吞掉；C# `AgentSessionStore` 的 `memory.json` 损坏没有同等备份。没有目录 fsync，强杀/断电的持久性依赖文件系统。

### 17.7 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 启动即返回 2，日志“需要 aiohttp” | venv 未装 aiohttp | 用运行 `run_episode` 的同一个 venv 安装 |
| 返回 2，日志“玩家档案错误” | 自定义 profile 路径不存在；schema/secret/world 键非法 | 默认路径才会自动播种；自定义路径必须显式创建 |
| 返回 2，日志“玩家状态/记忆错误” | state/episode/semantic schema 过新、player_id 不匹配、JSON 损坏 | 保留 `.corrupt-*`/`.vN.bak`；不要直接删除；用新版本程序读取 |
| 返回 2，“LLM 初始化失败” | 缺 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`、provider 不合法 | 配好变量，或先用 `--no-llm` |
| 进程立刻退出 0 | `PLAYER_AGENT_ENABLED=0` 或 `--disabled` | 检查环境变量/参数 |
| 一直重连、没有 shadow_tick | BetterGI Live 未启动、token 错、30648 不通 | 先确认 Live/ExternalAgent 已开启；检查 token 文件 |
| 每轮都跳过“帧太旧” | `timestampUtc` 缺失/不可解析、机器时钟偏差 | 看 `shadow` 日志与帧字段；不要依赖 `received_ms` 回退 |
| 没有 JSONL | `PLAYER_HOME`/`shadowDir` 指错、sessionId 被清洗、目录无权限 | 看 `config.describe()` 日志；检查目标目录 |
| intent 启动报 ValueError | URL 带了 `/intent`，或不是 loopback 根地址 | 改成 `http://127.0.0.1:<port>`；不要带路径 |
| intent 发布失败/401 | Bridge 未开 v2、`intentPort=0`、token 不匹配 | 检查 `motor_protocol`/`intentPort`/`intentToken` |
| 日志出现令牌 | `bridge_config.json` 字面量、异常原样打印、`--token` 进命令行 | 立即轮换；改用 token 文件/环境变量；清理日志 |
| 降级程序后状态报“版本过新” | state/memory schema 高于当前程序 | 用新版本读取；保留备份；不要删原文件 |
| 磁盘持续增长 | JSONL/episodes/evidence 无轮转 | 停机归档；目前没有内置清理 API |

### 17.8 回滚矩阵

| 层级 | 操作 | 效果 | 验证 |
|---|---|---|---|
| 入口 | 不传 `--player-shadow-config`，或不运行 `run_shadow.py` | 完全不启动 shadow | `run_episode` 只启动 director/bridge |
| 环境 | `PLAYER_AGENT_ENABLED=0` | 返回 0，不创建 profile | 进程立即退出、无新目录 |
| CLI | `--disabled` | 同上 | |
| 模式 | `PLAYER_AGENT_SHADOW=0` | 返回 2，拒绝启动 | 这是拒绝，不是降级；Phase 1 没有 Active |
| 上下文 | `CONTEXT_BUILDER=legacy` / `--context-builder legacy` | 回退 Phase 1 prompt | 日志 `context=legacy` |
| 观察 | 去掉 `--observe-ocr` / `--player-observe-ocr` | 不请求截图/OCR | 不再有 observe 事件 |
| 意图 | 去掉 `--intent-url` / `PLAYER_INTENT_URL`；Bridge `MOTOR_PROTOCOL=v1` 或 `intentPort=0` | 关闭间接控制 | shadow 记录不再有 intent |
| resume | 删除/改名 `state/resume_state.json` | 回到空 resume | 保留 profile/记忆 |
| 全部智能体 | `run_episode --no-agents` | 清空 director/lumine/shadow | 只录制 |
| 升级 | 保留旧二进制与 `.vN.bak` | 新 schema 不可被旧程序覆盖 | 旧程序会拒绝读取，不会写坏 |

再次强调：`DIALOGUE_LOCK=off`、`SPEECH_POLICY=legacy`、`MEMORY_BACKEND`、`RESUME_ENABLED` 在代码中没有读取点，不能作为回滚开关；`PLAYER_AGENT_SHADOW=0` 是拒绝启动，不是“切回旧模式”。

### 17.9 部署加固清单

1. Bridge 的 `intentHost`、AutoDL 的 `--host` 强制 `127.0.0.1`，通过 SSH 隧道访问 8010；不要默认 `0.0.0.0`。
2. `intentToken`/AutoDL token 必须非空；空 token 在当前实现中等于放行（`lumine_bridge.py:925-927`、`autodl_server.py:671-675`）。
3. `integrations/lumine/bridge_config.json` 中的字面量令牌移出仓库并轮换；`.gitignore:67` 虽有忽略规则，但当前仓库不是 git 仓库，忽略规则不能消除已有文件暴露。
4. BetterGI token 用 `--token-file`/环境变量，不要用 `--token`（会进进程列表）；日志采集要过滤。
5. `PLAYER_HOME` 放在受控 ACL 目录；记忆/evidence 是明文，按敏感数据管理。
6. LLM 端点显式选择；如果截图不允许出本机，使用本地模型或关闭 LLM（`--no-llm`）。
7. 同一 `PLAYER_HOME` 只运行一个 shadow；升级前备份 `state/`、`memory/`。
8. 监控 `shadow_llm_error`、`shadow_parse_error`、`shadow_memory_error`、`write_errors`、`shadow_context_error`、`shadow_live_restarted` 与 JSONL 大小；目前没有内置轮转。
9. 在真实 G2–G6 验收前保持 shadow 默认关闭；不要用“393 passed”替代实机验收。

### 17.10 为什么这样部署，以及未采用的替代方案

当前选择“多进程 + stop 文件”而不是单进程一体化：录制器在主进程，director/bridge/shadow 是子进程；shadow 崩溃不会直接杀死录制，收尾时先写 stop 文件给它保存 recap/resume/shadow_end 的机会。未采用单进程的原因正是 Windows 上 `terminate()` 不执行 `finally`（`run_episode.py:154-155`），单进程一旦被强杀，收尾逻辑同样不可靠，而且一个组件的异常更容易拖垮录制。

未采用 Docker/容器化：BetterGI 是 Windows WPF 桌面程序，需要管理员权限和真实桌面/游戏窗口；AutoDL/vLLM 又在远端 GPU。容器化不能解决窗口捕获、输入注入和 GPU 分离，反而增加部署复杂度。

未采用的另一个方案是让 `run_episode` 直接透传 `--intent-url`：当前没有实现（`run_episode.py:116-130`），所以启用 intent 需要单独运行 shadow 或手工改脚本。这是运维上的明确缺口，建议补参数并在日志中把“intent 模式”标成高风险。

### 17.11 真实环境阻塞与结论

| 阻塞 | 现状 | 对验收的影响 |
|---|---|---|
| 原神未安装 | 无法产生真实画面/OCR/动作 | 不能证明视觉输入、对话锁、MotorController 真实行为 |
| AutoDL 已停止 | 8010/8000 无服务 | 不能证明 AutoDL 推理、动作协议、重连 |
| 30648/1933 未监听 | BetterGI/OpenViking 未起 | 不能证明 Live/observe 真实 DTO |
| 默认 `PLAYER_HOME` 为空 | 只有空目录 | 生产 shadow 未真正跑过 |
| 飞鱼 TTS/VTS 未验证 | 渲染链路未联合 | 不能声称语音/形象通过 |
| 30–60 分钟/盲测未执行 | 无录像/盲评记录 | 不能声称“同一个玩家”成立 |
| C# 234 项未运行 | 只有代码阅读 | 不能声称执行器回归通过 |

因此，当前可交付的结论是：Python 侧 393 项测试通过，shadow 的默认只读路径、假服务端接线、记忆/证据门禁和回滚开关有代码与测试支撑；真实游戏、AutoDL、飞鱼 TTS、VTS 与盲测均未通过。部署时应按“默认关闭、显式开启、逐层验收”的方式推进，而不是把 393 当作上线许可。

## 18. 设计取舍、替代方案与风险

本节的判断标准不是“模块是否齐全”，而是“在现有证据下，这个选择是否比其他可行动方案更安全、更可验证”。状态标记沿用全文：【已实现且有测试】指仓库内存在单元测试或假服务端测试；【已实现但未实机验证】指代码路径存在，但没有连接真实 BetterGI、原神、AutoDL 或真实模型；【目标设计/未实现】指只有文档、类型或接口。

### 18.1 取舍总表

| 决策 | 实际选择 | 被否掉的替代方案 | 主要原因 | 代价与残留风险 | 状态 |
|---|---|---|---|---|---|
| 运行模式 | 默认 shadow，只记录提案 | 直接 active 接管动作 | 游戏动作不可逆，感知/意图链路尚无闭环证据 | shadow 不能证明动作正确；`--intent-url` 又使 shadow 有条件可写 | shadow【已实现但未实机验证】；active【未实现】 |
| 玩家主体 | 新建 PersistentPlayerAgent，与旧导演并行 | 把 live_director 改造成唯一玩家 | 保住录制、TTS、解说和回滚路径 | 生产链仍有两个认知主体，context 会混入旧导演文本 | 并行【已实现但未实机验证】；接管【未实现】 |
| 长期记忆 | 本地结构化记忆 + 证据门 | 直接用 OpenViking 向量召回 | 剧透隔离、逐字证据、离线、确定性 | 无语义泛化，检索 O(n)，跨场只靠 recap | 本地【已实现且有测试】；外部召回【未实现】 |
| 对话保护 | 发布 wait/hold intent | stop_actions、取消意图、无意图 | 最小破坏且可撤销，避免 Bridge 回落自由操作 | 4 秒 TTL、30 秒 OCR、无 ACK，保证很弱 | 【已实现但未实机验证】 |
| 数值状态 | 代码限幅 | 只靠 prompt 约束或让模型自由写分数 | 防止一次剧情把关系/情绪拉满 | 首次出现的角色不受 0.15 限幅；可能欠反应 | 【已实现且有测试】 |
| 跨场回顾 | 确定性 build_recap | 每场结束再调一次 LLM 总结 | 便宜、可复现、离线、不递归自总结 | 把未验证 episode 写成“发生过”，且绕过证据门 | 【已实现且有测试】，存在设计缺口 |
| 认知循环 | 单一固定间隔 deliberative tick | fast/reactive/deliberative/deep 四层 | LLM 延迟 60 秒、成本高、无高频感知管线 | 默认 12 秒才想一次，对话可能整段漏掉 | 单 tick【已实现】；四层【未实现】 |
| 上下文 | v2 分块预算 + legacy 回退 | 全量拼接所有记忆 | token 可控、同输入同 prompt、可丢低优先级块 | 必需块超预算时整轮不调模型；recap 非必需会被丢 | 【已实现且有测试】 |
| 动作通道 | player→Bridge intent→Motor | player 直接调 C# 输入接口 | 单一写者、复用 C# 安全策略、边界清晰 | 多一跳、无精确归属、Bridge 可能假 accepted | 协议【已实现】；闭环【未实现】 |
| 旧链路 | 保留旧导演与 OpenViking | 一次性切换到新玩家 | 没有实机/盲测证据，录制链路不能冒险 | 双记忆、双人格、旧摘要可能带剧透 | 保留；迁移未完成 |

### 18.2 为什么默认 shadow，而不是直接 active

最直接的理由是**不可逆性**。原神里的对话选项、树脂消耗、任务推进和地图传送没有事务回滚；一次错误识别就可能导致跳剧情或浪费资源。shadow 把“如果是我会怎么想、怎么做”写成 `shadow_tick`，`proposed_actions[].executed` 恒为 `false`（`integrations/player/player_proposal.py:195-202`），记录器再固定写 `mode="shadow"`、`executed=false`、`sent=false`（`integrations/player/shadow_recorder.py:134-152`）。这样评审至少能对比“玩家提案”和“Lumine 实际动作”，而不必先承担动作风险。

第二个理由是**依赖尚未闭环**。PlayerAgent 不消费 Motor 反馈，C# 动作回显不带 `intent_id`，Bridge 在 AutoDL WS 未连接时也会返回 accepted（`integrations/lumine/lumine_bridge.py:872-881`），而 AutoDL 默认 `prompt_mode=pretrain`，intent 只有在 `motor` 模式下才进入提示词（`integrations/lumine/autodl_server.py:86`、`:557`）。如果此时直接 active，失败会被同时归因于感知、规划和执行三个层，无法定位。shadow 先固定“感知+记忆+提案”这一层，是合理的分层验证。

但必须指出一个**边界裂缝**：只要同时设置 `--intent-url` 且 Bridge 使用 `motor_protocol=v2`，PlayerAgent 的 intent 就会经 Bridge 驱动 Lumine 产生真实动作；而 `SHADOW_RULES` 仍告诉模型“你的动作不会被发送到游戏”（`integrations/player/player_context.py:18-23`）。这不是纯文档问题，而是运行语义与提示词语义不一致。评审应把“默认 shadow”理解为“未启用意图总线时只读”，不能理解为“进程天然只读”。

替代方案是“离线回放”：把 session.jsonl 的帧喂给 agent，完全不接 Live。它更安全，但测不到 WS 重连、心跳、帧龄和 tick 时序；因此团队选择“在线 shadow、默认不执行”，这是一个折中，不是免费的安全证明。

### 18.3 为什么并行新主体，而不是改造旧导演

旧导演 `live_director.py` 已经在生产链路里承担 guidance、commentary、memory_patch 和工具调用，录制器、TTS、字幕、VTuber 叠加都消费它的输出。把它原地改造成“唯一玩家”，等于在缺少实机证据时同时替换认知、记忆、动作和解说四条链路，失败后没有可回退的对照。并行的好处是：旧链路继续产出节目，新主体在旁路积累 shadow 记录，出问题只需不传 `--player-shadow-config`。

代价也很真实：目标架构写着“THERE MUST BE ONE PLAYER”（`E:/BetterAGI/Docs/refactor/TARGET_ARCHITECTURE.md:11`），但生产上仍是“导演+Lumine”两个主体。PlayerAgent 的 context 还会读取旧导演的 guidance 和公开 commentary，这在 active 后可能把另一主体的叙事、甚至剧透带进唯一玩家。这个“双主体债”没有被代码消除，只是被 shadow 隔离了一部分。

替代方案是“导演退化为纯感知适配器”，只保留取帧和写事件，把决策全部交给 PlayerAgent。未采用的原因是导演有自己的 persona、OpenViking 记忆和工具白名单，且是 dsh 的可替换参考实现；一次性拆除会破坏现有录制与参考实现，也无法保证新主体能产出同等质量的解说。

### 18.4 为什么本地结构化记忆，不直接用 OpenViking 召回

OpenViking 是“可信文本管道”：它检索回来的 lore、旧场摘要和正文会被直接拼进提示词，没有逐条 provenance，也没有 `player_knowledge` 可见性字段。玩家侧的核心约束是“只知道屏幕上逐字看到的东西”，所以 `KnowledgeGate` 要求 fact 带 `evidence_id`、`evidence_quote`、`source=screen_ocr:<id>`，且引文必须出现在证据原文中、证据不超过 180 秒（`integrations/player/player_knowledge.py:102-146`）。这套门禁无法套在任意向量命中上，因为 OpenViking 返回的是自然语言片段，不是带帧哈希的 OCR 证据。

第二是**剧透隔离**。旧导演共享的 `memories/genshin/live` 里已经有须弥、草神等旧摘要；如果玩家直接召回，模型可能“记得”自己还没玩到的剧情。第三是**延迟与可用性**：本地检索虽然 O(n)，但可预算；OpenViking 单轮最坏约 52 秒（12 秒 search + 4×10 秒 read），还会在服务崩溃后保持 `available=False` 而不重连（`integrations/lumine/memory_bridge.py:108-120`），不适合 12 秒 tick 的玩家。因此 `run_shadow.py:358-364` 对 `PLAYER_OPENVIKING=1` 只打警告并继续禁用外部召回。

替代方案是“OpenViking 命名空间 + visibility 标签 + 内容过滤”。未采用是因为当前服务端不提供 fact 级 provenance，旧数据也未重新导入隔离命名空间，且没有内容级过滤；先上召回等于把剧透风险引入唯一玩家。代价是玩家没有跨场语义泛化，只能靠 episode/recap/关键词检索；随着 episode 增长，`list()` 全扫会变成 I/O 瓶颈，需要索引或轮转，这是后续工作。

### 18.5 为什么对话锁发 hold intent，而不是停手或取消

`_publish_dialogue_hold` 的注释把取舍写得很清楚：只取消不发布会让 Bridge 回落到旧的自由操作模式，可能点掉对话；一条 `wait` 意图既能阻止连点，也能把“为什么不动”写进反馈链路（`integrations/player/player_agent.py:959-964`）。相比 `stop_actions`，hold 不改按键、不打断移动，破坏性最小；相比完全不发意图，hold 明确占用“当前主意图”，避免 Motor 自行发挥。

但实现有一个硬矛盾：锁 TTL 只有 4 秒（`integrations/player/player_dialogue.py:17`），OCR 观察默认 30 秒一次（`run_shadow.py:124`），而 tick 默认 12 秒。短对话可能整段落在两次 observe 之间；HUD、任务追踪、物品名等任意 OCR 文本又会因为 `if in_talk or texts` 触发锁（`player_dialogue.py:56`）。锁过期后 `is_locked()` 静默变 false，agent 不一定补发取消；C# 侧也没有对话/菜单硬门控。因此 hold 是“尽力而为”，不是“保证不跳过剧情”。这个取舍可以理解，但不能按 README 的“不会让 Motor 回落”来宣传。

替代方案是 C# 侧识别 Talk UI 并硬暂停输入。它更可靠，但需要改 C# UI 分类和输入策略，当前没有做；Python 侧的 4 秒锁只是临时缓冲。

### 18.6 为什么限幅，以及为什么 recap 是确定性的

关系六维单次最多变化 0.15（`integrations/player/player_relationships.py:20`），情绪九维单次最多 0.35、半衰期 20 分钟（`integrations/player/player_emotion.py:26-27`），语音最小间隔 6 秒（`integrations/player/player_speech_policy.py:13`）。这些是**代码级不变量**，不是 prompt 建议。LLM 倾向于输出饱和分数，一次戏剧性事件就可能把 trust/affection 写到 1.0；限幅让关系轨迹可解释，也让“数值只在内部使用、prompt 只给自然语言摘要”的边界成立。替代方案是只在 prompt 里写“不要一次拉满”，未采用是因为模型输出不可保证。

限幅的代价是：新角色第一次出现时直接采用模型值，不走 0.15 限幅（`player_relationships.py:163-166`），所以仍可能一次拉满；已有角色则可能对真正重大的剧情反应不足。限幅只约束幅度，不验证语义，不能替代“这件事是否值得 +0.15”的判断。

recap 选择确定性 `build_recap`：只从结构化 episode/fact/working 生成，不把整场 transcript 塞进下一场，也不做新的叙事判断（`integrations/player/player_recap.py:3-7`）。替代方案是 session-end LLM 总结，未采用是因为多一次模型调用、不可复现、可能把旧幻觉再总结一遍。代价是 recap 会把 episode 的 `what_happened` 写成“发生过”、把 fact 写成“屏幕上看过”（`player_recap.py:126-137`），却不经过 `KnowledgeGate`；模型自生成的 episode 因此可能被下一场当成事实。这是“便宜连续性”换来的污染风险，评审应要求给 recap 条目加 provenance/“未验证”标签，或只允许通过门禁的 fact 进入 recap。

### 18.7 为什么是单 tick 和分块预算

单 tick 的直接原因是 LLM 调用最长 60 秒、成本高，且当前没有 2–10Hz 的快速感知层。`player_loop.py:3-7` 明确说只做“按固定间隔调用 agent.tick”，不实现 fast/reactive/deep；单线程顺序执行也天然避免同一 agent 并发 tick。替代的四层循环未采用，是因为需要高频帧处理、独立状态锁和更复杂的调度，Phase 1 没有这些基础设施。代价是默认 12 秒才思考一次，对话锁和 OCR 的 30 秒节奏更放大了盲区。

ContextBuilder 用 6600 字总预算、每块独立预算、required 块不丢、低优先级块先丢（`player_context_builder.py:27-42`、`:92`）。替代方案是全量拼接，未采用是因为 token 会随 episode 增长失控。代价是：required 块合计超预算时整轮不调用模型（`player_agent.py:649-652`）；recap 的 priority=65 且非 required（`player_context_builder.py:362-366`），在紧张时会被静默丢弃，跨场连续性并不保证。

### 18.8 回滚边界：哪些开关是真的

| 开关 | 实际行为 | 证据 |
|---|---|---|
| `PLAYER_AGENT_ENABLED=0` / `enabled=false` | 不启动 shadow，返回 0 | `run_shadow.py:316-318`；`player_config.py:404-411` |
| `PLAYER_AGENT_SHADOW=0` | 返回 2；“Phase 1 只支持 shadow” | `run_shadow.py:319-323` |
| `CONTEXT_BUILDER=legacy` | 回到 Phase 1 拼接 prompt | `player_config.py:324-334`；`player_agent.py:129-133` |
| Bridge `motor_protocol=v1` | 不启动 v2 意图总线 | `lumine_bridge.py:920-921` |
| 不传 `--intent-url` / `PLAYER_INTENT_URL` | 不创建 IntentPublisher，真正只读 | `run_shadow.py:388-397` |
| 不传 `--observe-ocr` | 不请求 OCR，对话锁永不触发 | `run_shadow.py:366-376`；`player_agent.py:571` |
| `--no-llm` | 只记录观察，不调模型 | `run_shadow.py:398-407` |
| `run_episode --player-shadow-config ""` | 不启动 shadow 子进程 | `run_episode.py:116-130` |
| 导演 `--no-llm` / `--dry-run` | 不调用模型/不发送 | `live_director.py:1824-1833` |
| `PLAYER_OPENVIKING=1` | 只打警告，不启用外部召回 | `run_shadow.py:358-364` |
| `DIALOGUE_LOCK` / `SPEECH_POLICY` / `MEMORY_BACKEND` / `RESUME_ENABLED` | **代码中没有读取，文档声称可回滚但不成立** | 审计 `doc_mismatches`；全仓无读取点 |

这张表说明：回滚能力存在，但文档承诺的四个开关是“纸面开关”。评审时只能以实际读取的环境变量为准。

### 18.9 风险登记（按严重度）

| 级别 | 风险 | 触发条件 | 影响 | 主要证据 |
|---|---|---|---|---|
| P0 | shadow 语义与 intent 总线冲突 | `--intent-url` + `motor_protocol=v2` | 模型被告知“不会发送”，实际可能驱动游戏 | `run_shadow.py:388-397`；`player_context.py:18-23` |
| P0 | 意图无反馈、无过期、无 ACK | 任意 intent 发布 | Bridge 假 accepted、重复 intent、失败不重规划 | `player_agent.py:888-1019`；`player_intent.py:255` |
| P0 | 对话保护不可证明 | HUD 误判/短对话/锁过期 | 可能跳过剧情，也可能长时间误锁 | `player_dialogue.py:17,56` |
| P0 | recap 绕过证据门 | 关闭 agent 生成 recap | 未验证内容进入下一场 prompt，形成自强化 | `player_agent.py:201-214`；`player_recap.py:126-137` |
| P0 | 多进程写无锁/强制接管 | 误开两个 shadow 进程 | 丢更新、交错写、锁文件被删 | `player_io.py:158-200`；审计 gaps |
| P0 | 记忆先写、审计后写 | tick 任意一步失败 | 记忆已推进但 JSONL 无记录，无法重建 | `player_agent.py:686-717` |
| P0 | Bridge/AutoDL 鉴权默认弱 | 空 token / 非 loopback 配置 | 本机或远端可发布意图/动作 | `lumine_bridge.py:925-931`；`autodl_server.py:76-80` |
| P1 | 事件循环阻塞 | 重置会话、检索、consolidate、fsync | WS 消息延迟，帧龄和 tick 失真 | 审计 `player_agent.py:440-463`；`player_retrieval.py:181-202` |
| P1 | 输入无上限/WS 非 loopback | 恶意或异常本地服务 | 内存膨胀、token 发往远端 | `run_shadow.py:205`；`player_config.py:336-348` |
| P1 | 旧导演与 OpenViking 未适配 | 新会话/服务崩溃 | 旧 guidance、旧摘要、52 秒阻塞 | 审计 `live_director`/`memory_bridge` |

## 19. 已知限制与未完成项

### 19.1 判定口径

本节只列“会影响评审结论”的限制，不列纯风格问题。状态分四类：A【已实现且有测试】、B【已实现但未实机验证】、C【部分实现/未闭环】、D【目标设计/未实现】。测试只证明纯逻辑和假替身，不证明真实 BetterGI/原神/AutoDL/飞鱼 TTS。主编已在 2026-10-28 执行 393 passed；下面的“有测试”仍只代表本地/假服务端证据，不代表实机通过。

### 19.2 P0：会改变安全或正确性结论的未完成项

| # | 问题 | 状态 | 说明与证据 |
|---|---|---|---|
| 1 | Active 模式不存在 | C | `PersistentPlayerAgent.mode="shadow"`（`player_agent.py:68`）；speech 只写 shadow，不接 TTS/渲染；proposed_actions 永远 executed=false。 |
| 2 | 意图反馈闭环未接 | C | agent 只 publish/cancel，不 `fetch_feedback`/`apply_feedback`/`expire`；`IntentTracker.needs_replan` 无人读；Bridge 的 feedback 只供 HTTP 查询（`player_intent.py:154-284`；`player_agent.py:888-1019`）。 |
| 3 | 动作归属不精确 | C | C# action 回显不含 intent_id（`LiveSessionService.cs:436-443`）；Bridge 用 `latest_intent` 近似（`lumine_bridge.py:1168-1172`）。 |
| 4 | 对话锁不能保证不跳过剧情 | B/C | 任意 OCR 文本触发 4 秒锁（`player_dialogue.py:56`）；OCR 30 秒一次；C# 无硬门控；锁过期静默解锁。 |
| 5 | recap 污染跨场记忆 | B | close 直接用 `semantic_store.list()` 生成 recap，不过 KnowledgeGate；`what_happened` 写成“发生过”，fact 写成“屏幕上看过”（`player_agent.py:201-214`；`player_recap.py:126-137`）。 |
| 6 | 多进程一致性 | B | State/Belief/Relationship/Emotion/Thread 的 load→改→save 无 file_lock；working/semantic 的 file_lock 5 秒超时会 unlink 接管；ShadowRecorder 的 seq 自增不在锁内。 |
| 7 | 写盘顺序不可重建 | B | tick 先 `_apply_proposal`、publish、consolidate、unlock evidence，最后才 `_record("shadow_tick")`（`player_agent.py:686-717`）；中途失败会“记忆已变、审计缺失”。 |
| 8 | 网络与鉴权默认 | B | Bridge `intent_token` 为空即不鉴权（`lumine_bridge.py:925-931`）；AutoDL 默认 `0.0.0.0`、空 token 放行（`autodl_server.py:76-80`）；`serve_lumine.sh:26,59-60` 默认 `0.0.0.0` 且只警告。 |
| 9 | WS 与帧输入边界 | B | `max_msg_size=0`（`run_shadow.py:205`）；JPEG 无大小上限；WS URL 没有 loopback 强制，token 会放到 query 发往任意配置主机。 |
| 10 | 会话重置不取消意图 | B | `_reset_session` 清 dialogue/speech/working，但不调用 `intent_publisher.cancel`（`player_agent.py:440-463`）；live.stopped、重连后 Bridge 可能仍持有旧 hold/意图直到 deadline。 |

### 19.3 P1：影响质量、可运维性或评审可信度

| 问题 | 状态 | 说明 |
|---|---|---|
| 关系首次出现不限幅 | B | 新角色直接采用模型值（`player_relationships.py:163-166`），可一次拉满 trust/affection；已有角色才受 0.15 限幅。 |
| 叙事线索不会自动重激活 | B | `ThreadStore.activate()` 只在测试中调用，生产检索命中不会重开 dormant 线程；审计 G9。 |
| resume 与四类状态不同步 | B | `resume_state` 只写 location/quest/intention 等子集，`current_emotion`/`current_beliefs`/`recent_relationships`/`active_threads` 未从 store 同步（`player_state.py:46-68`；审计 G12）。 |
| OpenViking 运行期不可恢复 | B | `available` 只在 start/close 变化，运行失败只记 debug，不重连；写回无超时、无本地 spool、旧 run 文件不清理；recall 最坏约 52 秒（`memory_bridge.py:108-120`）。 |
| LLM 客户端可靠性不足 | B | 无重试/退避/熔断；`except aiohttp.ClientError` 在 `asyncio.TimeoutError` 之前（`llm_client.py:374-378`），连接超时被报成“无法连接”；默认 `max_tokens=1024` 可能截断 JSON；默认模型 `claude-sonnet-5-5` 未验证（`llm_client.py:34`）。 |
| 旧导演无帧龄/无 live 生命周期 | B | `_tick` 只判断 frame is None；不处理 live.started/live.stopped；guidance 发送只有本地 send 成功，不是服务端 ACK（审计 idx10）。 |
| 事件循环阻塞 | B | 会话重置时同步 `working_store.save`（file_lock+fsync）；`retrieve_async` 实际同步扫 episode/fact；consolidate/save_state 同步写，可能延迟 WS 接收。 |
| 检索与存储只增不减 | B | 每轮 list 全部 episode、load 全部 fact；episode 去重全扫；shadow JSONL/episodes/evidence 不轮转，长局 O(n) I/O 与磁盘增长。 |
| 可观测性不足 | B | 无 recall/write 成功计数、耗时、最后成功时间；运行期 OpenViking 失败是 debug；玩家只有 tick/llm_errors 计数。 |
| 文档与代码漂移 | B | README 标题写 Phase 3–9，代码自称 Phase 1；README:219 说“跨进程加锁”但多数 store 无锁；README:188 的 wait hold 只在开启 OCR 时成立；`PHASE_6/7` 的回滚开关不存在；`TEST_PLAN` 的 fake 类不存在。 |

### 19.4 P2：清理项与死代码

- `_last_message_ms`、`_guidance_history` 只写不读；hello 的 fps/width/height 只存不用（审计 gaps）。
- `run_shadow.memory_bridge` 恒为 `None`，OpenViking 分支保留但永不生效，容易被误读为“可配置”。
- `run_shadow` 在 `recorder` 构造失败的异常路径可能引用未绑定变量；通用 `LOGGER.exception` 未走 `_safe_error` 的令牌脱敏（`run_shadow.py:433-454`）。
- `IntentPublisher.fetch_feedback`、`IntentTracker.expire/needs_replan` 无调用者；`normalize_intent` 会重置 `created_ms`，外部传入的时间不保留。
- `player_recap` 的 field 规范化不足；`RecapStore.load` 不校验；`SemanticStore.add` 在缺 visibility 时自动补 `player_knowledge`，API 层可绕过 OCR 门。
- `integrations/lumine/bridge_config.json:3,5` 含 `bettergiToken`/`lumineToken` 字面量，与 README 的“不要提交令牌”冲突；应轮换并改为环境变量/示例文件，本文不复述具体值。

### 19.5 失败模式、并发、重连与损坏恢复

**失败模式。** 单轮异常被 tick 捕获并记录 `shadow_llm_error`/`shadow_parse_error`，主循环继续（`player_agent.py:653-684`）；`player_loop.py:47-64` 也保证单轮异常不终止整场。这是正确的隔离方向，但代价是“持续失败也只留下警告”：LLM 不可用会每 12 秒重试一次，OpenViking 不可用会每轮超时，磁盘满会让 `_record` 进入 write_errors 而不是停机上报告。

**重连。** `_run_forever` 捕获 `aiohttp.ClientError/OSError/TimeoutError`，等待 `reconnect_delay`（默认 3 秒）后重连，并调用 `agent.reset_observation()` 丢弃旧帧/旧指导（`run_shadow.py:242-275`）。但重连不会向 Bridge 取消已发布 intent，也不会让 AutoDL reset `PlayerSession.intent`；旧 hold 可能继续有效到 deadline。`live.stopped` 会让 shadow 退出，但 Bridge 只在收到 live 事件时清本地 intent（`lumine_bridge.py:1101-1108`），两边生命周期不是同一事务。

**并发。** 代码假设“同一 state/memory/shadow 目录只有一个 shadow 进程”，但没有目录锁。`working/semantic` 用 `file_lock`，其他 store 只用原子替换；`file_lock` 超时后会删除锁文件强行接管（`player_io.py:158-200`），可能破坏正在写入的进程。`ShadowRecorder._seq += 1` 在 `_build_record` 中、锁外执行（`shadow_recorder.py:133-145`），close 与并发 write 的顺序没有完全证明。正确部署必须靠外部单实例约束，而不是代码。

**损坏恢复。** State/Working/Semantic 对版本过新会拒绝覆盖，并有备份；Episodic 的 `list/get` 对损坏/未来版本只跳过告警，不自动备份；Belief/Relationship/Emotion/Thread 的 `_recover` 备份失败被吞掉，随后 save 可能覆盖原损坏文件（审计 idx7）。这意味着“损坏备份”不是统一保证，评审应按 store 分别确认。

**关闭边界。** `close()` 不等待在途 tick；如果从其他任务调用，tick 可能已完成部分内存/磁盘更新后被 `_closed` 丢弃。Windows 的 terminate/kill 不执行 finally，可能没有 `shadow_end`，JSONL 尾部可能半行；`read_shadow_records(strict=False)` 只能容忍最后一行无换行。

### 19.6 为什么现在不能签字

- 真实原神未安装，AutoDL 已停止，飞鱼 TTS 未验收，30–60 分钟实况和盲测未执行；这些是项目自己文档承认的边界，不是本文的推测。
- A–E 连续性测试是单进程、假数据、缩小版，且 A–E 都不启动 PlayerAgent 全链路；C 的“重开进程”只是新建 ThreadStore 读同一文件（`test_continuity.py:3-5,88-98`）。
- 测试数字有口径差异：主编 2026-10-28 实际运行 Python 测试为 393 passed（player 189 + session 26 + lumine 178，另有 28 subtests）；静态 AST 只数到 `def test_` 168/26/178，参数化会放大数量，不能用静态计数反推已执行。C# `Test/` 下 `[Fact]/[Theory]` 静态合计 609（ExternalAccess 96），与文档常引用的 234 不是同一口径，需要主编统一；本次没有运行 `dotnet test`。
- 文档称 `test_player_agent.py` 覆盖“跨进程 resume”，实际是同进程新建 store 读同一路径（审计 doc_mismatch）；这类措辞会把“机制可行”误读成“进程级恢复已验证”。

## 20. 评审指南与问题清单

### 20.1 建议阅读顺序

不要从 README 的标题开始，否则会把“Phase 3–9 已落地”当成事实。建议按“边界→主循环→记忆→执行→证据”的顺序读：

| 时间 | 读什么 | 目的 |
|---|---|---|
| 0–30 分钟 | `integrations/player/player_agent.py` 的类头与 `tick`（65-68、523-724）、`shadow_recorder.py`（133-168）、`run_shadow.py`（305-489） | 先确认 shadow 到底写什么、什么时候写、哪些开关真的生效 |
| 30–60 分钟 | `player_context.py:18-37`、`player_context_builder.py:27-42,340-395`、`player_proposal.py:180-215` | 确认模型看到什么、输出 schema、哪些字段被丢弃/限长 |
| 60–100 分钟 | `player_state.py:46-68`、`player_episodic_memory.py:110-200`、`player_semantic_memory.py:83-136`、`player_knowledge.py:95-146`、`player_recap.py:49-138` | 确认“玩家知道什么”的证据链和跨场污染面 |
| 100–140 分钟 | `player_intent.py:63-284`、`player_dialogue.py:17-78`、`player_speech_policy.py:13-84`、`lumine_bridge.py:872-980,1095-1186` | 确认 intent 生命周期、对话锁、反馈与动作归属 |
| 140–180 分钟 | `LiveSessionService.cs:183-184,463-485,687-715`、`LiveSessionModels.cs:249-333`、`record_session.py:946-976`、`render_session.py:764-838,1238-1246` | 确认 Live 契约、录制/TTS/混音的真实边界 |
| 任意时间 | `Docs/PERSISTENT_PLAYER_AUDIT.json` 的 `audits` + `extra` | 对照审计的 gaps/doc_mismatches，不要只看 summary |

### 20.2 可执行验收命令

以下命令只验证代码与假服务，不启动原神、不启动主程序、不连接 AutoDL。先安装依赖（仓库没有统一的 player requirements；`aiohttp` 是运行与假 Hub 所需，`pytest` 是测试所需）：

```bash
python -m pip install -U pytest aiohttp
python -m pytest integrations/player/tests -q
python -m pytest integrations/session/tests -q
python -m pytest integrations/lumine/tests -q
python -m pytest integrations/player/tests --collect-only -q
```

假 Hub 端到端（两个终端）：

```bash
# 终端 A：只广播、不执行任何操作
python integrations/player/tests/fake_live_hub.py

# 终端 B：不调 LLM，跑 10 秒，数据写到临时目录
python integrations/player/run_shadow.py \
  --ws-url ws://127.0.0.1:30999/ws/v1/agent/live \
  --home E:/BetterAGI/tmp/player-smoke \
  --no-llm --duration 10 --verbose
```

跑完后检查 `E:/BetterAGI/tmp/player-smoke/state/shadow/*.jsonl`：每条记录应有 `mode="shadow"`、`executed=false`、`sent=false`、`seq` 单调递增；`shadow_end` 应存在。若进程被强杀，尾部可能半行，这是已知边界。

C# 编译验证（只编译，不启动主程序；遵循 `AGENTS.md`）：

```bash
dotnet msbuild BetterGenshinImpact.sln -restore -p:Configuration=Debug -nologo -tl:off -clp:ErrorsOnly
```

离线渲染（不需要飞鱼，验证字幕/混音/合成链路）：

```bash
python integrations/session/show_session.py <会话目录>
python integrations/session/render_session.py <会话目录> --tts none --no-subtitles
```

注意：`--tts feiyu` 需要 `E:/BetterAGI/cosyvoice` 独立环境，当前环境未验收；不要把它写成“已通过”。

### 20.3 声明—证据对照

| 文档声明 | 代码证据 | 评审结论 |
|---|---|---|
| “shadow 不发送动作/指导/解说” | `player_agent.py:68`；`player_proposal.py:195-202`；`run_shadow.py:388-397` | 仅在未启用 `--intent-url` 时成立；启用后经 Bridge 可产生动作 |
| “跨进程加锁” | `player_working_memory.py:260`；`player_semantic_memory.py:226`；其他 store 无锁 | 只对两个 store 成立，不能概括全部记忆 |
| “live.started 会重置” | `LiveSessionService.cs:183-184` 只写时间线，不 Broadcast | agent 的 live.started 分支是死分支；真实重置靠 hello/帧回退/live.stopped |
| “意图可追溯到一个 intent_id” | C# action 回显无 intent_id（`LiveSessionService.cs:436-443`） | 只能近似归属，不能精确追溯 |
| “对话锁期间不会回落自由操作” | 4 秒 TTL（`player_dialogue.py:17`）；30 秒 OCR | 只覆盖很短窗口，且依赖 publish 成功与 Bridge 行为 |
| “recap 是必选块” | `player_context_builder.py:362-366` priority=65, required=False | 可被预算丢弃，不是必选 |
| “每 30 秒保存 resume” | `player_agent.py` 的 `_maybe_save_state` 只在 tick/关闭触发 | 没有帧或没有 tick 时不会按 30 秒落盘 |

### 20.4 必须追问的问题

1. **安全边界**：生产默认到底是“只读 shadow”还是“intent 总线可写”？如果启用 v2，谁负责把 `SHADOW_RULES` 改成“动作可能被发送”，并提供一键断开 Bridge 的演练？
2. **唯一玩家**：active 后 commentary 与 action 是否同源？当前 player prompt 注入旧导演 guidance/commentary，如何证明观众听到的不是“第三个声音”？
3. **意图闭环**：Bridge 在 AutoDL 未连接时返回 accepted，谁负责把“已送达”与“已执行”分开？`failed` 三次后谁重规划？`deadline_ms` 由谁执行？
4. **对话**：任何 OCR 文本都会锁 4 秒，HUD/任务追踪是否会把玩家长期锁死？短对话落在两次 30 秒 observe 之间时，谁保证不跳过？会话结束/重连谁取消 hold？
5. **证据门**：recap、resume、belief/thread 都是模型生成内容，为什么它们不需要 `KnowledgeGate`？如果模型把幻觉写进 recap，下一场如何发现并清除？
6. **并发**：部署如何保证单进程？如果误开两个 shadow，哪个文件会丢更新、能否检测？`file_lock` 5 秒强制接管是否应改成 PID/心跳校验？
7. **崩溃恢复**：tick 先写记忆后写 shadow_tick，崩溃后如何从事后 JSONL 重建这次变更？是否需要 outbox/事务/先写意图日志？
8. **OpenViking**：运行期崩溃为什么不重连、不把 available 置回 False？52 秒召回阻塞 4 秒导演 tick 是否可接受？`wait=True` 在当前服务端不保证下一轮可检索，文档为何仍这样写？
9. **模型接入**：默认模型 `claude-sonnet-5-5` 是否真实可用？`max_tokens=1024` 是否足够输出完整 JSON？超时/错误分类顺序、无重试/熔断由谁负责？
10. **安全默认**：Bridge intent_host 可配非 loopback、空 token 不鉴权；AutoDL 默认 0.0.0.0；`bridge_config.json` 含字面量令牌。生产部署如何强制 loopback、轮换令牌、避免仓库泄密？
11. **验收**：A–E 是假数据模块测试，能否给出 3 个真实 session、跨场引用、对话锁、无剧透、回滚演练的最小证据？30–60 分钟盲测谁执行、评分标准是什么？
12. **文档口径**：README 标题、PHASE 文档、TEST_PLAN 的 fake 类、`DIALOGUE_LOCK`/`SPEECH_POLICY` 开关都与代码不一致；评审以哪一份为基线？谁负责在签字前统一？

### 20.5 签字前检查清单

- [ ] 默认配置下 `run_shadow.py --no-llm` 能连假 Hub，JSONL 每条 `executed=false/sent=false`，`shadow_end` 存在。
- [ ] 不传 `--observe-ocr` 时 `DialogueTracker` 从不 observe；传了之后能用非 Talk UI 文本复现误锁，并记录影响。
- [ ] 用假 Bridge 验证：AutoDL 未连接时 `/intent` 仍 accepted；重复 `intent_id` 的行为；`/feedback` 能读到什么、读不到什么。
- [ ] 用两个进程同时写同一目录，验证 lost update/锁接管；如果无法保证单进程，必须在部署文档写死。
- [ ] 人为破坏 state/episode/belief 文件，确认哪些 store 备份、哪些静默跳过、哪些会覆盖。
- [ ] 检查 `bridge_config.json` 是否含真实令牌；若有，轮换并改为环境变量。
- [ ] 对照 `E:/BetterAGI/Docs/refactor/GAP_ANALYSIS.md` 的 G1–G48，以及 `Docs/PERSISTENT_PLAYER_AUDIT.json` 中 16 条审计的 277 条 gap 记录，确认 P0 项没有被漏掉。

### 20.6 停止条件

出现以下任一情况，评审应暂停而不是“先签后补”：默认配置就可能产生游戏动作而文档仍称只读；意图反馈无法区分 accepted/executed；对话锁被证明会长期误锁或完全漏锁；recap 已把未验证内容写入下一场且无清除手段；仓库含真实令牌且未轮换。

## 附录 A. 文件地图

### A.1 顶层结构

```text
E:/BetterAGI/BetterGI/
├─ BetterGenshinImpact/                 C# 主程序、Live Hub、Agent HTTP/WS、输入与识别
├─ integrations/
│  ├─ player/                           PersistentPlayerAgent（shadow）
│  ├─ session/                          录制、会话格式、离线渲染、单场编排
│  ├─ lumine/                           旧导演、AutoDL 服务、Bridge、LLM 客户端、OpenViking
│  ├─ dsh/                              外部导演协议客户端/stdio 桥
│  └─ vtuber/                           VTuber 叠加与渲染
├─ Test/BetterGenshinImpact.UnitTest/   C# 单元测试
├─ Docs/                                设计、使用手册、审计产物
├─ lore/                                去剧透的玩家可见资料
└─ E:/BetterAGI/Docs/refactor/          重构目标/阶段文档（不在 BetterGI 仓库内）
```

### A.2 PersistentPlayerAgent（`integrations/player/`）

| 文件 | 行数 | 职责 | 状态 |
|---|---:|---|---|
| `player_agent.py` | 1160 | 玩家主体：tick、观察、context、proposal、intent、consolidate、recap、close | B |
| `player_loop.py` | 64 | 固定间隔单 tick 循环 | A |
| `run_shadow.py` | 512 | 进程入口：配置、WS 重连、依赖装配、关闭 | B |
| `shadow_recorder.py` | 197 | 独立 shadow JSONL、seq、脱敏、close | A |
| `player_config.py` | 475 | CLI/环境变量/JSON 合并、路径解析 | A |
| `player_profile.py` | 521 | 档案 schema/校验/稳定摘要/默认档案 | A |
| `player_state.py` | 273 | resume 状态、版本检查、原子写 | A |
| `player_working_memory.py` | 294 | working memory | A |
| `player_episodic_memory.py` | 310 | episode 存储、去重、损坏跳过 | A |
| `player_semantic_memory.py` | 305 | PLAYER_KNOWLEDGE fact 存储 | A |
| `player_knowledge.py` | 146 | 证据记录 + OCR 逐字引文门禁 | A |
| `player_vision.py` | 120 | 可选 BetterGI `/agent/observe` 客户端 | B |
| `player_retrieval.py` | 382 | 本地多因子检索、可选 external（被禁用） | A |
| `player_memory_consolidator.py` | 407 | 提案→working/episode/belief/relationship/emotion/thread | A |
| `player_recap.py` | 198 | 确定性跨场 recap | A |
| `player_context.py` | 169 | legacy prompt 与 SHADOW_RULES | A |
| `player_context_builder.py` | 491 | v2 分块预算 ContextBuilder | A |
| `player_proposal.py` | 400 | 模型 JSON 宽容解析、字段限长 | A |
| `player_intent.py` | 284 | intent/feedback 状态机、HTTP 发布 | A/B |
| `player_beliefs.py` | 197 | 信念存储、可推翻历史 | A |
| `player_relationships.py` | 217 | 关系六维、0.15 限幅 | A |
| `player_emotion.py` | 140 | 情绪九维、20 分钟半衰期、0.35 限幅 | A |
| `player_threads.py` | 194 | 叙事线索 open/dormant/resolved | A |
| `player_dialogue.py` | 97 | OCR 对话感知、4 秒锁 | A/B |
| `player_speech_policy.py` | 85 | 沉默/冷却/情绪余韵/prosody | A |
| `player_legacy_import.py` | 63 | 旧导演记忆隔离导入 | A |
| `player_io.py` | 200 | 原子写、file_lock、脱敏、时间解析 | A |
| `profiles/lumi.json` | — | 默认玩家档案模板 | A |
| `player_config.example.json` | 12 | 示例配置 | A |
| `tests/` | 16 个测试文件 | 单测、假 Hub、连续性 A–E | A（假环境） |

### A.3 会话与渲染（`integrations/session/`）

| 文件 | 行数 | 职责 |
|---|---:|---|
| `session_format.py` | 588 | 会话 schema、事件类型、读写校验 |
| `record_session.py` | 998 | 连接 Live、录帧/事件/音频、写 session.jsonl |
| `show_session.py` | 125 | 会话摘要查看 |
| `render_session.py` | 1578 | 离线 TTS（SAPI/飞鱼）、ASS 字幕、原音混音、VTuber 叠加、ffmpeg 合成 |
| `run_episode.py` | 299 | 启动 Live、导演、Bridge、shadow 子进程并录制 |
| `tests/` | 4 个测试文件 | 格式、录制、渲染、编排 |

### A.4 旧导演、Bridge 与模型接入（`integrations/lumine/`）

| 文件 | 行数 | 职责 |
|---|---:|---|
| `live_director.py` | 1917 | 旧导演：取帧、guidance/commentary/memory_patch/工具调用 |
| `lumine_bridge.py` | 1639 | BetterGI Live ↔ AutoDL Lumine 桥、200ms 动作、v2 intent 总线 |
| `lumine_protocol.py` | 487 | Motor 协议、动作块/坐标/键位约束 |
| `autodl_server.py` | 998 | AutoDL 侧 WebSocket 服务、vLLM 推理、动作生成 |
| `llm_client.py` | 396 | Anthropic/OpenAI 兼容传输层 |
| `memory_bridge.py` | 231 | OpenViking recall/remember_session |
| `memory_ingest.py` | 80 | lore 导入 CLI |
| `bridge_config.json` | 11 | 运行配置（含令牌字段，需轮换） |
| `bridge_config.example.json` | 11 | 安全示例（占位符） |
| `director_config.json` | 9 | 旧导演运行配置 |
| `persona.example.json` | 20 | 人格示例 |
| `requirements.txt` | 4 | aiohttp 等 |
| `serve_lumine.sh` | 129 | AutoDL 启动脚本 |
| `run_director_deepseek.sh` | 25 | 导演启动脚本（默认 OpenViking 开启） |
| `tests/` | 5 个测试文件 | 导演、Bridge、协议、AutoDL、OpenViking 假客户端 |

### A.5 C# Live Hub（`BetterGenshinImpact/Service/ExternalAccess/Agent/`）

| 文件 | 行数 | 职责 |
|---|---:|---|
| `Live/LiveSessionService.cs` | 963 | 会话生命周期、帧广播、guidance/commentary、action、hello、live.stopped |
| `Live/LiveWebSocketHandler.cs` | 342 | WS 接入、令牌、消息循环 |
| `Live/LiveHttpEndpoints.cs` | 171 | `/live/start`、`/live/stop`、`/live/status`、guidance/commentary |
| `Live/LiveActionExecutor.cs` | 699 | 33ms 键块、200ms 动作、停止/队列 |
| `Live/LiveSessionModels.cs` | 429 | 请求/响应/服务端消息 DTO（含未赋值的 `UiCategory`） |
| `Live/LiveKeyTokenMap.cs` | 114 | 按键名映射 |
| `Live/LiveMcpTools.cs` | 303 | Live 相关 MCP 工具 |
| `Input/SimulatedInputController.cs` | 597 | 模拟键鼠输入 |
| `Input/AgentCoordinateMapper.cs` | 154 | 坐标映射（默认 1280×720 口径） |
| `Safety/AgentSafetyPolicy.cs` | 328 | 输入/动作安全策略 |
| `Sessions/AgentSessionStore.cs` | 1103 | 会话记忆键值、时间线 |
| `Models/AgentObservationModels.cs` | 202 | OCR/观察 DTO |
| `Vision/BgiAgentVisionService.cs` | 134 | BetterGI 截图/OCR 观察 |

### A.6 文档与数据

| 路径 | 说明 |
|---|---|
| `Docs/PERSISTENT_PLAYER_AUDIT.json` | 本次审计原始结果：`result.audits` 10 条 + `result.extra` 6 条 |
| `E:/BetterAGI/Docs/refactor/TARGET_ARCHITECTURE.md` | 目标架构（唯一玩家） |
| `E:/BetterAGI/Docs/refactor/AGENT_LOOP.md` | 四层循环目标（未实现） |
| `E:/BetterAGI/Docs/refactor/MEMORY_ARCHITECTURE.md` | 记忆分层、剧透隔离 |
| `E:/BetterAGI/Docs/refactor/PHASE_1…PHASE_9` | 阶段实现/已知边界 |
| `E:/BetterAGI/Docs/refactor/TEST_PLAN.md` | 验收目标与测试口径 |
| `Docs/design/live-vtuber-pipeline.md` | 旧导演/解说协议设计 |
| `Docs/design/memory-system.md` | OpenViking 记忆设计（部分已过时） |
| `Docs/使用手册.md` | 部署与使用 |
| `integrations/player/README.md` | 玩家模块说明（含与代码不一致的概括，需按本文校正） |
| `lore/genshin_basics.md` | 去剧透资料 |
| 默认运行数据 `E:/BetterAGI/player/` | profile/state/memory/shadow，见附录 B |

## 附录 B. 协议与数据 schema

### B.1 BetterGI Live WebSocket

服务端 → 客户端（camelCase；`LiveSessionModels.cs:249-333`）：

| type | 关键字段 | 说明 |
|---|---|---|
| `hello` | `sessionId`, `fps`, `width`, `height`, `actionIntervalMs`, `chunksPerAction` | 连接后立即发；`LiveSessionService.cs:463-475` |
| `frame` | `seq`, `timestampUtc`, `jpeg`(base64), `width`, `height`, `capture`, `runtime`, `guidance` | `BuildFrameMessage`；`UiCategory` 字段存在但从未赋值 |
| `guidance` | `text`, `source`, `seq` | 外部指导 |
| `commentary` | `kind`, `text`, `emotion`, `frameSeq`, `meta` | 解说；`meta.internal` 仍会广播 |
| `action` | `seq`, `block`, `text`(thought) | 已接受动作回显；**不含 intent_id** |
| `action_state` | `keys`, `queueDepth`, `starved`, `applied`, `dropped` | 动作执行状态 |
| `live.started` | `sessionId` | 当前 C# 只写时间线，不 Broadcast（`LiveSessionService.cs:183-184`），Python 分支是死分支 |
| `live.stopped` | `sessionId` | 会 Broadcast（`:270`），shadow 收到后退出 |
| `error` | `code`, `message` | 错误 |

客户端 → 服务端主要是 HTTP：`POST /api/v1/agent/live/start`、`/stop`、`/guidance`、`/commentary`、`/action`、`/observe`；Python 侧只使用 `/observe`（可选）和 Bridge 的 `/intent`，不直接调 action。

### B.2 intent v2 与 motor_feedback

`normalize_intent`（`player_intent.py:63-101`）的输出：

```json
{
  "type": "intent",
  "version": 2,
  "intent_id": "i-20261028-120000-ab12",
  "goal": "去七天神像附近找凯瑟琳",
  "reason": "任务提示",
  "constraints": ["不进入战斗"],
  "motor_mode": "navigate",
  "target": {},
  "interrupt_conditions": ["对话开始", "生命值过低"],
  "deadline_ms": 15000,
  "created_ms": 1760000000000
}
```

约束：`goal` 必填且 ≤300 字；`intent_id` 只允许字母/数字/点/下划线/连字符；`motor_mode` 未知时回退 `navigate`（不是拒绝）；`deadline_ms` 被夹在 1000–60000；`target` 只接受 dict；`constraints`/`interrupt_conditions` 各最多 8 条。`normalize_feedback` 的 `status` 只允许 `progress|done|blocked|failed`（`player_intent.py:24-27,104-120`）。

注意：PlayerAgent 实际只传 `goal/reason/motor_mode/deadline_ms`（`player_agent.py:927-935`），`target/constraints/interrupt_conditions` 没有从提案传入；同 goal 复用时还会丢弃新的 `reason/motor_mode`（`:919-925`）。因此 schema 支持不等于运行时使用。

### B.3 shadow JSONL

每条记录由 `ShadowRecorder._build_record` 生成（`shadow_recorder.py:133-157`）：

```json
{
  "schema_version": 1,
  "type": "shadow_tick",
  "mode": "shadow",
  "executed": false,
  "sent": false,
  "seq": 42,
  "player_id": "lumi",
  "session_id": "vtuber",
  "run_id": "20261028-120000-a1b2",
  "t_unix_ms": 1760000000000,
  "run_t_ms": 120000,
  "timestamp_utc": "2026-10-28T12:00:00Z",
  "frame_seq": 123,
  "frame_age_ms": 850,
  "context": {"mode": "v2", "total_chars": 5123, "blocks": []},
  "memory": {"episode": null, "facts_created": 0, "ocr_unlocks": []},
  "intent": {"intent_id": "...", "published": true, "status": "sent"},
  "speech_policy": {"speak": false, "reason": "cooldown"},
  "proposal": {"observation": "...", "thought": "...", "intention": {}},
  "visual_observation": {"ocr": ["..."]}
}
```

记录类型至少包括 `shadow_start`、`shadow_observation`、`shadow_tick`、`shadow_llm_error`、`shadow_parse_error`、`shadow_context_error`、`shadow_observer_reconnected`、`shadow_end`。任何字段都经 `_sanitize`；`executed/sent` 在顶层固定为 false，但嵌套 proposal 的 `executed` 也固定 false（`player_proposal.py:195-202`）。

### B.4 记忆文件

| 文件 | schema_version | 关键字段 | 说明 |
|---|---:|---|---|
| `state/resume_state.json` | 1 | `player_id`, `last_location`, `current_quest`, `current_intention`, `current_emotion`, `recent_character_relationships`, `current_beliefs`, `active_story_threads`, `things_i_want_to_check_next` | `player_state.py:46-68`；后四个字段未与 store 同步 |
| `state/working.json` | 1 | `location`, `characters`, `dialogue`, `recent_events`, `short_plan` | 会话级短期状态 |
| `state/session_recap.json` | 1 | `last_location`, `current_quest`, `what_happened`, `people`, `learned`, `unresolved`, `last_thought`, `next_intentions` | 确定性 recap；`learned` 会被写成“屏幕上看过” |
| `state/belief_state.json` | 1 | `belief_id`, `proposition`, `status`, `confidence`, `evidence`, `contradictions`, `history` | `status=ground_truth` 被拒绝（`player_beliefs.py:60-85`） |
| `state/relationships.json` | 1 | `character_id`, 六维分数, `memorable_events`, `impressions`, `unresolved` | 单次 ±0.15；首次记录不限幅 |
| `state/emotion_state.json` | 1 | 九维分数, `last_updated_ms` | 半衰期 20 分钟；单次 ±0.35 |
| `state/narrative_threads.json` | 1 | `title`, `status`, `importance`, `current_theory`, `related` | `activate()` 生产未调用 |
| `memory/episodes/<id>.json` | 1 | `event_id`, `time_ms`, `location`, `characters`, `what_happened`, `what_i_knew_before`, `what_i_thought`, `what_i_felt`, `my_reaction`, `proposed_intention`, `choices_i_made`, `unresolved_questions` | 必填四字段见 `player_episodic_memory.py:122-136` |
| `memory/semantic/facts.json` | 1 | `fact_id`, `proposition`, `source`, `evidence_id`, `evidence_quote`, `learned_ms`, `confidence`, `spoiler_risk`, `visibility` | `visibility` 必须是 `player_knowledge`（`player_semantic_memory.py:95-101`） |
| `memory/evidence/<sha256>.json` | — | `evidence_id`, `frame_id`, `frame_sha256`, `session_id`, `observed_at_ms`, `text`, `confidence` | 证据原始 OCR；180 秒内有效 |
| `memory/quarantine/*.json` | — | 旧导演记忆副本 | 不参与召回 |

### B.5 证据门（provenance）

一条 fact 要进入 retrieval，必须同时满足（`player_knowledge.py:132-146`）：

1. `visibility == "player_knowledge"`；
2. `evidence_id` 能在 EvidenceStore 找到；
3. `evidence_quote` 是 4–200 字、且逐字出现在证据 `text` 中；
4. `source == "screen_ocr:<evidence_id>"`；
5. `proposition == "屏幕OCR（未校对）：" + quote`。

`unlock_quote` 还要求证据属于当前 `session_id`/`frame_id`、`frame_sha256` 匹配、观察时间在 0–180 秒内（`:102-130`）。这套门禁只覆盖 OCR fact；episode、recap、belief、thread、resume 不受它约束，这是当前最大的“证据边界不完整”。

### B.6 session.jsonl

`session_format.py:55-72` 定义的事件类型：`session`, `thought`, `speech`, `action`, `avatar`, `guidance`, `frame`, `note`, `video`, `audio`, `error`, `end`。每条事件至少 `t`（毫秒）与 `type`；`session` 头带 `version`、`session_id`、`started_at` 等。审计指出的缺口是：perception/dialogue/memory_recall/belief_update/emotion_update/relationship_update/narrative_thread_update/intent/motor_feedback/reflection/memory_commit 等新事件没有写入；`frame`/`note` 也未必实际写。

### B.7 兼容与版本规则

- 所有主要文件带 `schema_version`；读取到更高版本应拒绝降级覆盖（State/Working/Semantic/Profile 均有对应错误）。
- Profile `schema_version<1` 直接报“需要迁移，当前版本不支持自动迁移”（`player_profile.py:351-355`），没有迁移实现。
- `intent.version` 默认 2；未知 `motor_mode` 回退而非拒绝；这与 `PHASE_3_INTENT_MOTOR.md:22` 的“直接拒绝”冲突。
- `hello`/`frame` 的字段名以 C# camelCase 为准；旧导演读 `frameWidth/frameHeight/ts`，实际是 `width/height/timestampUtc`，导致日志/元数据为 None（审计 idx10）。这是客户端未适配，不是协议缺失。

## 附录 C. 命令与配置

### C.1 端点与端口

| 端点 | 默认 | 用途 |
|---|---|---|
| BetterGI HTTP/WS | `127.0.0.1:30648` | `/ws/v1/agent/live`、`/api/v1/agent/observe`、`/live/*` |
| AutoDL Lumine | `0.0.0.0:8010`（脚本默认） | `/ws/player`，WebSocket 动作服务 |
| Bridge intent bus | 默认不启动（`intent_port=0`） | `POST /intent`、`GET /feedback`；v2 才启动 |
| OpenViking | `127.0.0.1:1933` | 导演长期记忆；玩家侧禁用 |
| 假 Live Hub | `127.0.0.1:30999` | 测试用只读广播 |

### C.2 环境变量（实际读取）

| 组 | 变量 | 作用 |
|---|---|---|
| 玩家路径 | `PLAYER_HOME`, `PLAYER_PROFILE`, `PLAYER_STATE`, `PLAYER_SHADOW_DIR`, `PLAYER_MEMORY_DIR`, `PLAYER_EPISODES_DIR`, `PLAYER_SEMANTIC_PATH`, `PLAYER_WORKING_PATH`, `PLAYER_RECAP_PATH`, `PLAYER_BELIEF_PATH`, `PLAYER_RELATIONSHIP_PATH`, `PLAYER_EMOTION_PATH`, `PLAYER_THREAD_PATH` | 覆盖默认目录/文件 |
| 玩家连接 | `BETTERGI_LIVE_WS`, `BETTERGI_AGENT_TOKEN`, `BETTERGI_SESSION_ID`, `PLAYER_INTERVAL`, `PLAYER_RECONNECT_DELAY`, `PLAYER_MAX_FRAME_AGE_MS`, `PLAYER_STATE_SAVE_INTERVAL_MS` | WS/节拍/帧龄/保存间隔 |
| 玩家开关 | `PLAYER_AGENT_ENABLED`, `PLAYER_AGENT_SHADOW`, `PLAYER_NO_LLM`, `CONTEXT_BUILDER`, `PLAYER_OPENVIKING`, `PLAYER_INTENT_URL`, `BRIDGE_INTENT_TOKEN` | 启停、legacy 回退、外部召回（无效）、意图总线 |
| LLM | `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MAX_TOKENS`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL`, `OPENAI_REASONING_EFFORT` | provider 解析、模型、超时外的生成参数 |
| Bridge/AutoDL | `BRIDGE_INTENT_HOST`, `BRIDGE_INTENT_PORT`, `BRIDGE_INTENT_TOKEN`, `LUMINE_HOST`, `LUMINE_PORT`, `LUMINE_VLLM_URL`, `LUMINE_MODEL`, `LUMINE_SERVER_TOKEN`, `LUMINE_MOCK`, `LUMINE_MAX_TOKENS`, `LUMINE_TEMPERATURE`, `LUMINE_TOP_P`, `LUMINE_PROMPT_MODE` | 服务地址、鉴权、推理参数 |
| OpenViking | `OPENVIKING_URL`, `OPENVIKING_ENABLED`, `OPENVIKING_RECALL_LIMIT` | 仅旧导演使用 |

`LLM_MAX_TOKENS` 默认 1024（`llm_client.py`），`deepseek.env.example` 建议 4096；`LLM_API_KEY` 只参与取值、不参与 provider 推断；两把 key 同时存在时 Anthropic 优先。这些都是评审容易踩的配置坑。

### C.3 配置示例

玩家：

```json
{
  "enabled": true,
  "shadow": true,
  "bettergiWs": "ws://127.0.0.1:30648/ws/v1/agent/live",
  "sessionId": "vtuber",
  "interval": 12,
  "reconnectDelay": 3,
  "maxFrameAgeMs": 5000,
  "stateSaveIntervalMs": 30000,
  "contextBuilder": "v2"
}
```

Bridge（示例文件，占位符，不要提交真实令牌）：

```json
{
  "bettergiWs": "ws://127.0.0.1:30648/ws/v1/agent/live",
  "bettergiToken": "REPLACE_WITH_BETTERGI_AGENT_TOKEN",
  "lumineWs": "ws://127.0.0.1:8010/ws/player",
  "lumineToken": "REPLACE_WITH_LUMINE_SERVER_TOKEN",
  "fps": 5,
  "reconnect": true
}
```

### C.4 常用命令

```bash
# 只验证链路，不调用模型
python integrations/player/run_shadow.py --no-llm --duration 30

# 带配置启动 shadow
python integrations/player/run_shadow.py --config integrations/player/player_config.example.json

# 和录制/导演/Bridge 一起启动
python integrations/session/run_episode.py \
  --token-file <token 文件> \
  --director-config integrations/lumine/director_config.json \
  --lumine-config integrations/lumine/bridge_config.json \
  --player-shadow-config integrations/player/player_config.example.json

# 只录制，不启动导演（director-config 为空）
python integrations/session/record_session.py --start --out sessions

# 查看/渲染
python integrations/session/show_session.py sessions/<name>
python integrations/session/render_session.py sessions/<name> --tts sapi
python integrations/session/render_session.py sessions/<name> --tts none --no-subtitles

# 测试
python -m pytest integrations/player/tests -q
python -m pytest integrations/session/tests -q
python -m pytest integrations/lumine/tests -q
```

### C.5 令牌与密钥处理

- 令牌优先用 `--token-file` 或 `BETTERGI_AGENT_TOKEN` 环境变量，不要放在命令行历史；`run_episode.py:92-94` 通过环境变量传给子进程。
- 日志侧有 `redact_url`/`redact_secrets`/`_safe_error`，但不是所有异常路径都走（`run_shadow.py` 通用 `LOGGER.exception` 是例外）。
- `integrations/lumine/bridge_config.json:3,5` 含字面量 token；应先确认是否为真实凭据，轮换后改为 `bridge_config.example.json` + 环境变量。本文不复制具体值。
- AutoDL 空 token 等于无鉴权，默认又绑 `0.0.0.0`；生产至少应设置 `LUMINE_SERVER_TOKEN` 并把 host 改为 loopback 或加防火墙。

## 附录 D. 术语表

| 术语 | 含义 | 代码/文档锚点 |
|---|---|---|
| PersistentPlayerAgent | 持续存在的 shadow 玩家主体；拥有 profile、记忆、信念、情绪、意图与解说决策 | `player_agent.py:65` |
| MotorController | 把 intent 转成 200ms 动作的执行器；当前由 Lumine/AutoDL + Bridge 承担 | `lumine_bridge.py`；`autodl_server.py` |
| BetterGI Live Hub | C# 侧实时会话：广播 `hello/frame/guidance/commentary/action/action_state/live.stopped` | `LiveSessionService.cs` |
| shadow 记录 | 独立于 session.jsonl 的 JSONL，固定 `mode=shadow/executed=false/sent=false` | `shadow_recorder.py:133-157` |
| intent 总线 | PlayerAgent → Bridge 的 HTTP 目标通道；v2 协议，只发目标不发按键 | `player_intent.py`；`lumine_bridge.py:918-969` |
| hold intent | 对话锁期间发布的 `motor_mode=wait` 意图，防止 Motor 自由操作点掉对话 | `player_agent.py:959-1019` |
| episode | 一轮结构化“发生了什么/我之前知道什么/我怎么想/我怎么感受”的记忆单元 | `player_episodic_memory.py:172-200` |
| recap | 一场结束时从 state/working/episode/fact 生成的确定性跨场回顾 | `player_recap.py:49-138` |
| provenance / 证据门 | fact 必须绑定 OCR 证据、逐字引文、帧哈希和来源，才可被召回 | `player_knowledge.py:95-146` |
| working memory | 当前场景/角色/对话/近期事件的短期状态 | `player_working_memory.py` |
| episodic memory | 跨 tick 的事件记忆，按 episode 文件存储 | `player_episodic_memory.py` |
| semantic memory | 玩家已知事实（PLAYER_KNOWLEDGE），带证据字段 | `player_semantic_memory.py` |
| belief | 可推翻的剧情猜测，保留 evidence/contradictions/history | `player_beliefs.py` |
| relationship | 角色关系六维：familiarity/trust/affection/suspicion/respect/concern | `player_relationships.py:19` |
| emotion | 情绪九维，20 分钟半衰期，单次 ±0.35 | `player_emotion.py:20-27` |
| narrative thread | 未解决线索，状态 open/dormant/resolved/abandoned | `player_threads.py` |
| dialogue lock | OCR 疑似对话时的 4 秒软锁；期间 speech 不发声、intent 发 hold | `player_dialogue.py:17`；`player_speech_policy.py:42-43` |
| speech policy | 决定是否开口、语速、音量、停顿、是否可打断 | `player_speech_policy.py:24-81` |
| ContextBuilder | v2 分块预算提示词构造器；可回退 legacy | `player_context_builder.py` |
| legacy prompt | Phase 1 的单段拼接提示词 | `player_context.py` |
| consolidation | 把模型提案写入 working/episode/四类状态的流程；不自动写 semantic | `player_memory_consolidator.py:212-259` |
| active / shadow | active 会执行动作；shadow 只记录。当前 active 未实现 | `player_agent.py:68` |
| Motor prompt mode | AutoDL 的 `pretrain/instruct/motor`；只有 motor 模式把 intent 放进提示词 | `autodl_server.py:86,557` |
| v1 / v2 协议 | v1 是旧 guidance/action 路径；v2 是 intent 总线 | `lumine_bridge.py:920-921` |
| session_generation | agent 内部会话代际；await 后用它丢弃旧会话结果 | `player_agent.py:442,547,578` |
| run_id | 每次 shadow 运行/OpenViking 写入的唯一标识，避免覆盖上一场 | `shadow_recorder.py`；`memory_bridge.py:85-87` |
| OpenViking | 旧导演的长期记忆服务；玩家侧禁用外部召回 | `memory_bridge.py:44` |
| prosody | 语速/音量/停顿等表现参数；只影响表现，不改变语义 | `player_speech_policy.py:70-80`；`render_session.py:1009` |
| sidechain ducking | 用解说音轨压低游戏原音的 ffmpeg 混音 | `render_session.py:1238-1246` |
| 飞鱼 TTS / CosyVoice3 | 离线角色音色合成；需要独立环境，未验收 | `render_session.py:76-80` |
| A–E 连续性 | 模块级假数据回归：episode→recap、belief、thread、emotion+speech、semantic+retrieval | `test_continuity.py` |
| G1–G48 | 重构差距分析条目；任务旧口径只到 G42，实际文档有 48 项 | `GAP_ANALYSIS.md` |
| P0/P1/P2 | 本文风险分级：P0 改变安全/正确性结论，P1 影响质量/可运维性，P2 清理项 | 本文第 18–19 节 |
