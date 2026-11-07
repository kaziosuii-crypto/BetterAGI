# 差距分析（GAP_ANALYSIS）

对照目标 **Persistent LLM Player Agent**，逐项比较当前系统。  
严重度：P0 = 阻断「一个玩家」；P1 = 阻断连续性/真实性；P2 = 表现层。

> Phase 0.5 已修复其中一部分：G31（memory 形状）、G32（guidance 确认/重试）、
> G33（部分：live 重启/帧龄/guidance 来源）、G35（OpenViking 覆盖）、
> G37（lore 示例剧透）、G41（live 重启 seq/guidance）、G42（guidance 竞态）。
> 其余仍按本表执行；修复记录见 [PHASE_0_5_FIXES.md](PHASE_0_5_FIXES.md)。
>
> Phase 1 部分覆盖：G1（新增独立 shadow 主体骨架，尚未接管动作）、
> G4（显式 profile，但尚未做长期检索）、G12（resume 只保存少量自我状态）、
> G30（schema_version + 原子写 + 损坏备份）。仍是 P0/P1 的项没有被“骨架”
> 掩盖。

| # | 严重度 | 当前系统 | 为什么无法实现 Persistent LLM Player | 应改为什么 | 涉及文件 | 风险 | 迁移方式 |
|---|---|---|---|---|---|---|---|
| G1 | P0 | DeepSeek 导演与 Lumine 各自看画面、各自决策；导演只发一行 guidance，Lumine 自决动作 | 存在两个认知主体；commentary 与 action 不同源；导演不知道 Lumine 的意图/结果 | **唯一 PersistentPlayerAgent** 拥有目标、意图、记忆、情绪；Lumine 降级为 MotorController | `live_director.py`, `lumine_bridge.py`, `lumine_protocol.py`, `autodl_server.py`, `Tests` | 改动面大；现有录制链路依赖当前消息 | 先加 `player_agent` 进程/模块，shadow 模式同时运行；保留旧 director 作为 commentary-only 回退 |
| G2 | P0 | Lumine 的 system prompt 是“expert Genshin player … decide the best action”；guidance 只是可选建议 | Lumine 拥有叙事/局部目标，成为第二玩家 | MotorController prompt 只描述“把给定 intent 转成 200ms 动作”；禁止自行选择任务/路线/对话 | `lumine_protocol.py:114-145`, `autodl_server.py:517-559` | 动作质量可能下降；需要 intent→motor 的清晰映射 | 新增 `motor` prompt 版本；用 `prompt-mode=motor` 灰度；保留旧 prompt 作为 fallback |
| G3 | P0 | 导演只看到 `action_state.keys`；`action` 回显（block/thought/mouse）被导演忽略 | 导演无法判断动作是否执行、为什么执行、是否卡住；解说容易与操作不一致 | 导演消费 `motor_feedback`/`action_result`；动作携带 `intent_id`、`goal`、`reason`、`status` | `live_director.py:1133-1160`, `lumine_bridge.py:1095-1107`, BetterGI `LiveSessionModels` | 消息体变大；需要兼容旧客户端 | 新增 `motor_feedback` 事件；桥把 Lumine 的 `thought/status` 转成反馈；导演订阅 |
| G4 | P0 | 没有 player_profile；persona 只有 name/personality/tone/catchphrases | 人格是静态主播描述，不是持续玩家的经历/偏好/价值观 | `player_profile/`（identity/personality/play_style/preferences/values/speech_style/long_term_interests/self_history），稳定 system 摘要 + 可检索长期人格 | 新建 `player_profile/`；`live_director.py` prompt | 人格漂移；过度工程 | 先写 JSON schema + 只读 profile；首轮注入稳定摘要；长期项走检索 |
| G5 | P0 | OpenViking episode 是每 60 秒一行的文本；无结构化 episode | 无法问“当时我怎么想/我学到了什么/有哪些未解决疑问” | `EpisodicMemory` schema（event_id/time/location/characters/what_happened/what_i_knew_before/what_i_learned/what_i_thought/what_i_felt/my_reaction/choices/unresolved/importance/emotional_intensity/related/source_session/game_time_range） | `memory_bridge.py`, 新建 `memory/` 模块；OpenViking | 写入量/成本上升 | 先双写：旧文本日志 + 新 JSON episode；检索先用 JSON 结构化字段 |
| G6 | P0 | 没有 belief system；OpenViking 可能召回世界真相 | Agent 可能知道尚未经历的剧情；无法保留“猜错/猜中”的认知历史 | `BeliefStore`：proposition/status(ground_truth/known_fact/belief/hypothesis/suspicion/unknown/disproven)/confidence/evidence/first/last/contradictions；严格知识门控 | 新建 `memory/beliefs.py`；`lore/` 拆分；`memory_bridge.py` | 剧透/逻辑不一致；迁移旧记忆需标注未知状态 | 新增 `WORLD_DATABASE` 与 `PLAYER_KNOWLEDGE` 两套 URI；只允许 PlayerKnowledge 进入 recall |
| G7 | P1 | 没有 relationship state | 角色态度无法随事件连续变化；无法形成偏好/怀疑/信任 | `RelationshipStore`：familiarity/trust/affection/suspicion/respect/concern + memorable_events/impressions/unresolved/relationship_summary；自然语言渲染，不暴露数值 | 新建 `memory/relationships.py`；prompt | 数值化导致机械；一次剧情暴涨 | 事件驱动小步更新（每事件上限变化）；只把自然语言摘要注入 |
| G8 | P1 | emotion 只是每条 speech 的标签（calm/excited/...） | 没有持续情绪、衰减、强度；重大剧情后立刻兴奋 | `EmotionState`：joy/sadness/fear/anger/curiosity/confusion/excitement/nostalgia/tension；衰减、事件强化、影响语言/动作/注意力 | 新建 `cognition/emotion.py`；`live_director.py`；`session_format.py` | 情绪状态难校准；可能过度表演 | 先用规则衰减 + LLM 事件打分；输出自然语言情绪摘要，不直接显示数值 |
| G9 | P1 | `plot` 只是一个字符串 | 伏笔/线索无法在几十小时后重新激活 | `NarrativeThreadStore`：thread/status/importance/first_seen/related_characters/evidence/current_theory/last_referenced；出现线索时激活 | 新建 `memory/threads.py`；`memory_bridge.py` | 线索抽取错误；线程爆炸 | LLM 只提议线程，规则去重/合并；检索按 thread 加权 |
| G10 | P1 | 检索是 top-4 语义相似 | 会漏掉重要旧事、角色、伏笔、未完成意图 | 多因子评分：semantic 0.30 + importance 0.20 + emotional 0.15 + character 0.10 + narrative 0.15 + recency 0.10；候选生成 + 重排 | `memory_bridge.py`, 新 `memory/retrieval.py` | 权重难调；延迟上升 | 先离线重排现有 top-20；逐步接 OpenViking 多查询 |
| G11 | P1 | 每 60 秒 append 一行文本；无 consolidation | 记忆不退火、不总结、不更新 belief/relationship/emotion/thread | `MemoryConsolidator`：实时 event log → 阶段性 extraction → episodic/belief/relationship/emotion/thread 更新；session end 生成回顾 | 新建 `memory/consolidator.py`；`live_director.py` | LLM 成本；总结漂移 | 先用规则提取 + 每小时一次 LLM 总结；保留原始 event log 可重放 |
| G12 | P0 | 没有 resume_state | 重启后不能“睡醒继续”；只有偶然的 memory/向量命中 | `resume_state.json`：last_location/current_quest/current_intention/unfinished_actions/active_threads/recent_important/current_emotion/recent_relationships/current_beliefs/things_to_check/last_internal_thought | 新建 `state/resume.py`；`run_episode.py`；`record_session.py` | 状态过期/与游戏实际不符 | 每 30 秒原子写；启动时校验 location/quest；允许人工修正 |
| G13 | P1 | 单一 4 秒 `_tick_loop`；没有 fast/reactive/deep | 危险/对话/战斗反馈无法及时处理；大模型被浪费在普通移动 | 四层循环：fast 2–10Hz 规则/小模型；reactive 0.5–2s 事件；deliberative 3–15s；deep 重大事件/session end | `live_director.py`, 新 `agent/loops.py` | 并发/竞态；成本 | 先拆出 fast perception 与 deliberative；deep 只在大事件触发 |
| G14 | P1 | commentary 由导演独立生成；Lumine 动作不受其控制 | 解说可能是“导演以为发生的事”，不是玩家真实认知 | Speech 来自 PlayerAgent 的统一认知状态；`thought → speech` 同源；speech 事件带 thought_source/emotion/intensity/pace/volume/pause/interruptible | `live_director.py`；`session_format.py`；`render_session.py` | 需要重构 LLM 输出 schema | 新增 `agent_speech` 事件；旧 commentary 作为 fallback |
| G15 | P1 | 提示词只说“可以空数组”；没有 dialogue lock | 普通移动也说话；NPC 对话被覆盖；可能跳过对话 | SpeechPolicy：interesting/emotional/discovery/prediction/confusion/failure/success 才说；dialogue_lock 时禁止动作与覆盖 | `live_director.py`；`lumine_bridge.py`；`lumine_protocol.py` | 可能过度沉默 | 先加规则（对话检测/冷却/最短间隔）；灰度观察 |
| G16 | P0 | 没有 dialogue perception；导演只有 JPEG | Agent 不知道 NPC 说了什么，无法理解剧情 | `DialoguePerception`：speaker/text/timestamp/confidence；BetterGI OCR/字幕/视觉模型；连续对话形成 conversation event | BetterGI vision/OCR；新 `perception/dialogue.py`；`live_director.py` | OCR 错误；字幕延迟 | 先接 BetterGI PaddleOCR/UI category；低置信度标记 unknown；不强行解释 |
| G17 | P0 | 动作协议只有 chunks/absolute/thought；无 intent/owner/goal/reason | 动作无归属、无理由、无完成/阻塞反馈；无法审计 | Intent protocol：intent_id/owner/goal/reason/constraints/motor_mode/target/interrupt_conditions；MotorFeedback：status(done/blocked/failed)/reason/observation | `lumine_protocol.py`；`lumine_bridge.py`；BetterGI `LiveSessionModels` | 协议不兼容；执行器改动 | 新增 v2 协议；桥做 v1↔v2 转换；旧客户端可用 v1 |
| G18 | P1 | 桥只转发动作；没有 blocked 回报给导演 | Lumine 可能无限试；导演不知道卡住 | MotorController 返回 blocked（找不到路/UI异常/卡墙/目标丢失/连续失败），PlayerAgent 重新规划 | `lumine_bridge.py`；`autodl_server.py`；`live_director.py` | 反馈噪声 | 先加 `status` 字段与连续失败计数；超过阈值上报 |
| G19 | P1 | prompt 字符串拼接；只有 memory_limit/recent 限制 | 上下文不可控，重要块可能被截断；token 成本不稳 | `ContextBuilder`：Identity/Perception/Working/Intention/Episodic/Characters/Threads/Beliefs/Emotion/Dialogue/Lore 每块预算 + 动态压缩 | 新 `context/builder.py`；`live_director.py` | 实现复杂；可能丢信息 | 先定义块与预算；保留现有 prompt 作为 fallback |
| G20 | P0 | `lore/` 直接导入 OpenViking，玩家可检索全部 | 剧透：Agent 可能知道未经历的真相 | `WORLD_DATABASE`（离线/OCR/验证）与 `PLAYER_KNOWLEDGE`（只能通过游玩解锁）；`knowledge_unlock` 事件 | `lore/`；`memory_bridge.py`；新 `knowledge/` | 需要标注资料；迁移成本 | 先给 `lore/` 加 `visibility` 元数据；默认 `world_only`；玩家知识单独导入 |
| G21 | P2 | 没有 importance/decay/forgetting | 记忆无限膨胀；旧琐事挤占上下文 | importance 0–1 评分；0.2 以下可遗忘；0.5+ 长期；0.75+ 核心；摘要化 | `memory/consolidator.py`；OpenViking | 误删重要记忆 | 只做软遗忘（降权/摘要），不立即删除 |
| G22 | P2 | 没有 autobiographical memory | 没有“我的旅程”，只有剧情百科 | `My Journey So Far`，按 chapter/region/character/theme；session end 更新 | `memory/autobiography.py` | 文风漂移 | 基于 episode 模板 + LLM 润色；保留来源引用 |
| G23 | P1 | session.jsonl 只有 thought/speech/action/avatar/guidance/frame/... | 无法重放认知/证据/动作归属 | 增加 perception/dialogue/memory_recall/belief_update/emotion_update/relationship_update/narrative_thread_update/intent/motor_feedback/reflection/memory_commit | `session_format.py`；`record_session.py`；各模块 | 事件量大 | 先加新事件类型与可选字段；旧读取器忽略未知类型 |
| G24 | P2 | 录制只有画面；渲染只混 TTS | 实况缺少游戏原音/ducking；TTS 无 prosody | 录制游戏音频；ducking；NPC 对话优先；speech prosody；TTS 只后处理不改内容 | `record_session.py`；`render_session.py`；`cosyvoice/` | 音轨同步/版权 | 先录系统 loopback；渲染加 sidechain ducking；prosody 先存元数据 |
| G25 | P1 | TTS 后处理没有 speech prosody 元数据 | 所有句子一个 cute preset | SpeechEvent：emotion/intensity/pace/volume/pause_before/interruptible/thought_source | `session_format.py`；`render_session.py`；`cosyvoice/feiyu_tts.py` | 飞鱼参数映射 | 先存字段；渲染时映射到 speed/emotion prompt |
| G26 | P1 | 测试只验证模块；没有连续性/剧透/人格验收 | “162 tests passed”不代表同一个玩家 | 新增 Test A–E + 对话锁 + 意图归属 + 30–60 分钟人工验收 | `tests/`；新 `tests/continuity` | 测试运行慢/成本 | 先用 fake LLM/fake motor；真实 DeepSeek 只跑关键用例 |
| G27 | P2 | Amaidesu 有 recap/roster/notebook 记忆，但离线链路未使用 | 重复造轮子；两套记忆互不相通 | 参考其 recap/roster 模式；或把 Amaidesu 仅保留为直播层，PlayerAgent 独立 | `Amaidesu/src/agents/streamer/memory/` | 概念混淆 | 不强行合并；在文档中明确边界 |
| G28 | P1 | Lumine 2.5Hz（0.4s/帧），协议目标 5Hz | 动作间有 200ms 空档；连续操作不自然 | MotorController 可重复/插值/预测；或升级 vLLM/量化；意图层容忍低频 | `autodl_server.py`；`lumine_bridge.py` | 操作质量 | 先加动作填缝/保持；后续优化推理 |
| G29 | P1 | 导演 sessionId 配置为 `vtuber`，录制/实时会话 id 可能不同 | memory.json 与 session.jsonl 不对齐；下一场可能读错记忆 | 统一 `player_id` 与 `session_id`；resume 指针 | `director_config.json`；`record_session.py`；`live_director.py` | 迁移已有数据 | 新增 `player_id`；旧 session 做映射/迁移 |
| G30 | P2 | 没有 player profile 的版本/迁移 | 人格/记忆 schema 升级会破坏旧数据 | schema version + migration；profile 备份 | 新 `state/` | 数据丢失 | 先写版本字段与只读加载；迁移脚本 |
| G31 | P0 | `GET /memory` 形状不匹配：导演期望 `{"memory":{...}}`，C# 返回 `{sessionId,updatedAtUtc,values}`；`self.memory` 变成包装对象 | 重启后 scene/plot/objective 读不到；`_short_term_text`/episode 写入拿到“未知”；测试 fake 掩盖 | 统一 memory DTO：读取 `values`，或后端返回 `memory`；补真实形状测试 | `live_director.py:1085-1105`, `AgentSessionModels.cs:40-50`, `BgiAgentHttpEndpoints.cs:221-226`, `test_director.py:46-52` | 现有测试与真实 API 脱节 | 先修读取形状 + 加真实 DTO fixture；旧数据兼容 |
| G32 | P0 | `_send` 在 `_ws is None` 时静默 return；`_process_response` 仍把 guidance 标记为已发送 | 指导可能从未到 Lumine，但导演以为发了；解说与动作分叉 | 发送返回 bool/ack；失败不更新 last_sent；guidance ACK/重试 | `live_director.py:1449-1452, 1595-1605` | 需要协议 ACK | 先加发送结果检查与日志；桥回 guidance_received |
| G33 | P1 | 外部 guidance 直接覆盖 `_latest_guidance`；无帧新鲜度检查；`live.stopped` 未处理 | 导演可能用别人的指导/旧帧继续解说；停止后仍 tick | guidance 来源优先级；frame age；live.stopped 停循环 | `live_director.py:1129-1159, 1220-1222, 1338` | 行为变化 | 加来源/时间戳与新鲜度阈值；停止事件 |
| G34 | P1 | 无 `persona.json`，只有 example；config 无 persona；默认静默回退 DEFAULT_PERSONA | 不同 cwd/机器人格不同；persona 示例不生效 | 显式 persona 路径 + 版本 + 校验；缺失硬错/明确默认 | `live_director.py:1613-1631, 1685-1691`; `director_config.json` | 启动失败/迁移 | 复制 example 为 persona.json；config 显式引用；加加载测试 |
| G35 | P0 | OpenViking 写入 `mode=replace` 同一个 `live/<session>.md`；首 tick 即写；MemoryBridge 只启动时连一次 | 重启覆盖上一场摘要；OpenViking 晚启动则长期记忆永久关闭 | 每场唯一 URI/追加式 episode；启动重试/健康检查 | `memory_bridge.py:89-115,182-204`; `live_director.py:1017,1480-1519` | 旧摘要丢失 | 新 URI 带时间戳/session；旧文件迁移；重连 |
| G36 | P0 | SessionWriter `"w"` 截断；目录重名新建；meta.session_id 与目录名不一致；无 parent/resume | 无法续录/跨场关联；记忆与录像对不上 | append/resume 模式；统一 player_id/session_id；meta.parent/previous | `session_format.py:294-300`, `record_session.py:703-733` | 数据迁移 | 新 schema v2；旧文件只读；写迁移脚本 |
| G37 | P0 | `lore/genshin_basics.md` 示例含后期剧透；无知识门控 | Agent 可能提前知道真相 | 示例去剧透；world/player 分离；Test E | `lore/genshin_basics.md:8,14-18`; `memory_bridge.py` | 示例本身违规 | 立刻替换示例为无剧透版；导入时加 visibility |
| G38 | P2 | 文档/协议/测试数与代码不一致（dsh vs director、AutoDL 实测 vs 未实测、151 vs 162、PROTOCOL 字段） | 迁移判断失真 | 以代码/运行结果为准，统一文档 | `Docs/`, `integrations/*/README.md` | 低 | Phase 1 文档对齐 |
| G39 | P1 | 测试 fake 的 memory 形状、guidance 转发、action 回显未覆盖，掩盖 G31/G32 | 绿测不代表真实链路 | 真实 DTO fixture；guidance 转发端到端；action 块测试 | `tests/` | 低 | 先补失败测试再修 |
| G40 | P2 | `run_episode` 20 秒无帧也启动智能体；子进程无健康检查；meta.agent 硬编码 | 空转/误导会话元数据 | 首帧门控/健康检查；agent 名称配置 | `run_episode.py:66-91,166-172`; `record_session.py:731` | 低 | 加 `--require-frame`；健康检查 |
| G41 | P0 | 桥忽略 live.started/live.stopped；新 live 会话 seq 从 0 重置，但 BetterGI `LastActionSeq` 不重置；旧 guidance 被带回 | 重启后动作被长期拒绝；新场景执行旧指导；解说与操作完全脱节 | 桥处理 live 事件、重置 seq/pump/guidance；BetterGI 在会话边界重置客户端 seq | `lumine_bridge.py:994,385-397,316-318`; `LiveSessionModels.cs:410-411`; `LiveWebSocketHandler.cs:262-270` | 高 | 先加 live 事件处理与 seq 重置测试；BetterGI 侧修复兼容 |
| G42 | P0 | 旧帧内联 guidance 覆盖新 guidance（桥 frame/guidance 竞态） | Lumine 下一帧仍按旧指导行动 | guidance 带版本/时间戳；帧内联 guidance 只在更新时使用；桥单写者 | `lumine_bridge.py:938-960,316-330`; `autodl_server.py:765-768` | 中 | 加 guidance seq；旧帧丢弃 |
| G43 | P0 | actionsOut 只代表 WS 写入；BetterGI 拒绝/执行失败不回导演；无效 action 不 stop_actions | 导演解说未执行的动作；按键最多继续 250ms | motor_feedback/action_ack 到 PlayerAgent；无效动作立即 stop | `lumine_bridge.py:986-992,1104-1112`; `LiveActionExecutor.cs:380-386` | 中 | 新增 ack/feedback；桥转发 error |
| G44 | P0 | 无 dialogue/menu 门控；bridge 丢 uiCategory/runtime；prompt 教绝对点击 | 剧情/菜单中被点击跳过或选错 | dialogue_lock + UI 类别 + 禁止跳过键 | `lumine_bridge.py:324-330`; `lumine_protocol.py:105-107,123-125`; `LiveSessionService.cs:687-701` | 高 | 先接 UI/对话检测；无检测时保守暂停 |
| G45 | P1 | 默认 pretrain 不要求 follow guidance；temperature 0.7 无 seed | 指导被忽略；动作抖动，解说不一致 | Motor prompt 明确服从 intent；低温度/固定 seed；intent 约束 | `lumine_protocol.py:97-112,132-134`; `autodl_server.py:84-85` | 动作质量下降 | 新增 motor prompt；可配置 temperature/seed |
| G46 | P1 | 绝对坐标固定 1280×720，非 16:9 错位 | 菜单点击偏移 | 传真实宽高/比例；坐标映射按源帧 | `lumine_protocol.py:48-51`; `LiveActionExecutor.cs:448-453`; `lumine_bridge.py:324-330` | 低 | 帧携带宽高；映射修正 |
| G47 | P1 | AutoDL 不检查帧年龄；执行器不看源帧时间 | 迟到点击点到错误选项 | frame age 阈值；过期动作丢弃/降级 | `autodl_server.py:267-273,749-774`; `LiveActionExecutor.cs:165-188` | 中 | 加 `max_frame_age_ms` |
| G48 | P1 | 桥丢弃 width/height/capture/runtime/uiCategory | 无法做 UI/对话/前台门控 | 保留感知元数据到 Motor/Player | `lumine_bridge.py:324-330` | 低 | 扩展 frame schema |

---

## 关键结论

1. **G1/G2/G3/G4/G5/G6/G12/G16/G17/G20 是 P0**，必须最先处理；否则无论加多少
   记忆/情绪模块，仍然不是「一个玩家」。
2. **不要先做 Live2D/TTS 花活**；G14/G15/G24/G25 是表现层，等认知连续后
   再做。
3. **保留现有录制/操作链路**：G1/G17 的迁移必须用 shadow/双写，不得让
   当前可用的录像能力中断。
4. **测试标准要换**：从「模块测试通过」升级为「跨 session 连续性 +
   无剧透 + 人格一致 + 30–60 分钟人工验收」。
