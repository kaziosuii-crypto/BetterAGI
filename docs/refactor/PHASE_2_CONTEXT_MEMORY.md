# Phase 2：ContextBuilder 与结构化记忆

## 当前状态

已为独立的 shadow 玩家实现上下文构造和本地结构化记忆骨架。旧导演、Lumine 和 `session.jsonl` 格式未被替换；shadow 仍只观察，不发送游戏操作。**Phase 2 的完整验收尚未完成**：自动验证的知识解锁、旧导演记忆双写以及真实游戏连续性均未实现或验证。

## 实现

- `integrations/player/player_context_builder.py`：按身份、观察、工作记忆、意图、恢复状态、情景记忆、语义事实、公开解说与输出格式分块；预算单位为字符，并非模型 tokenizer。可选块超限时逐条裁剪或丢弃；必需内容仍超总预算时跳过本次模型调用。
- `CONTEXT_BUILDER=legacy` 或 `--context-builder legacy` 使用旧的 shadow 提示词构造。这个开关只回退提示词，不停止结构化记忆写入。
- `player_working_memory.py`：工作记忆位于 `PLAYER_HOME/state/working.json`；会话变化时清理当前观察，超过三分钟的会话级内容在下一次模型调用前清理。
- `player_episodic_memory.py`：将 shadow 模型描述的事件、地点、角色、当时的思考和情绪写入 `PLAYER_HOME/memory/episodes/`。这是未验证的模型记录，并非已证实的游戏历史或执行回执。未获取到游戏时间轴时 `game_time_range` 为 `null`。
- `player_semantic_memory.py`：保存带来源的事实。当前只有通过证据门禁的屏幕 OCR 引文会写入；模型给出的 `learned_facts` 只是候选，不会自动成为已知事实。
- `player_knowledge.py`：证据门禁。观察截图与 OCR 结果绑定同一帧；事实必须是该帧 OCR 文本中逐字出现的引文，并携带可校验的证据 ID。证据文件被篡改、过期或帧不匹配时一律拒绝。
- `player_vision.py`：可选的 BetterGI 观察客户端。只请求 `/api/v1/agent/observe`，禁止跟随重定向，且只允许本机回环地址；使用观察返回的同一张截图作为模型输入，不与 Live 帧混用。BetterGI 的 OCR 不提供置信度，因此记录为“未校对”，不冒充高置信事实。
- `player_retrieval.py`：本地中英词法匹配与多因子排序，返回 ID、来源、时间和分数；诊断元数据记录在 shadow JSONL。缺失或非 `player_knowledge` 的条目不会参与检索。
- `player_memory_consolidator.py`：同时更新 shadow 的旧 resume 与新的 working/episode；两类文件分别写入，不是跨文件原子事务。
- `player_legacy_import.py`：把旧导演 memory.json 复制到独立隔离区（`visibility=quarantined`、`verification=unverified`），不导入玩家知识，也不参与召回。旧链路保持原样。

## 安全边界和限制

- 现有 OpenViking 目录可能混入导演摘要和世界资料，外部召回仍然禁用。
- 玩家已知事实只来自本轮截图的 OCR 逐字引文，且标注为未校对；模型推理、模型自述的场景和角色都会在上下文里标明是影子推测。
- 旧导演的公开 guidance、手工编辑的 profile 和手工导入的事实仍可能带入未经历内容，当前没有内容级剧透过滤。
- 观察接口会在 BetterGI 侧创建/追加会话事件与观察记录；它不发游戏输入，但不是零副作用。观察会话使用固定 ID，避免每场新建会话耗尽 BetterGI 的会话配额。
- 结构化存储会对已知密钥格式和敏感字段做脱敏，但模式匹配不能保证识别所有密钥。不要把令牌写入记忆文件。
- 文件写入采用单写者假设；跨进程写同一记忆路径由文件锁串行化。单个文件原子替换，不提供多文件事务。
- 未实现意图执行、动作回执、NPC 对话锁、belief/relationship/thread 存储、完整 session recap 或游戏音频。既有导演与 Lumine 仍是分离的决策链，因此尚未实现唯一玩家目标。

## 验证

Python 全量回归 **350 passed、28 subtests passed**（169 Lumine + 20 session +
161 player），Ruff 检查通过；另有 14 条既存的 `aiohttp` `NotAppKeyWarning`。
新增覆盖：证据门禁（错帧、错会话、篡改、过期、非逐字引文一律拒绝）、旧事实
无法被召回、观察与截图同源、观察端点限制、并发写事实不丢条目、停止信号优雅
退出、会话切换丢弃异步提案、过期工作记忆不复用、旧记忆隔离导入幂等。

未运行 BetterGI 主程序；未验证真实原神、AutoDL 或真实 OCR 识别率。C# 测试
本轮未运行。
