# 落地路线：从「开环盲按」到「双系统 + 拟人」

> 排序原则：**先修「让当前架构能工作」的硬 bug，再接「让架构成立」的线，最后做「让观感像人」的增强**。
> 每一步都给出：改哪里、改什么、怎么验收。行号是当前工作树行号。

---

## Phase 0（1–2 天）：止血，不改架构

这 6 件都是小改动、独立可验证、立刻能感知到效果。

| # | 事项 | 改动点 | 验收 |
| --- | --- | --- | --- |
| 0.1 | **修 Omni collect 超时 + 取消** | `omni.py:350-406` 的 `collect` 加 `asyncio.wait_for(deadline)`；`fast_stream.py:153` 的 `.result(timeout=45)` 改为在事件循环内取消（`run_coroutine_threadsafe` 的 future `.cancel()` 不够，需在协程内捕获 `CancelledError` 并释放 recv） | 手动注入 45s 无响应，下一轮不再出现双并发 recv；日志无 `cannot call recv` |
| 0.2 | **motor 超时 < 总预算** | `motor.py:30` `DEFAULT_TIMEOUT=60.0` → 取 min(剩余预算, 8s) | 一次卡顿不会吃掉 3 个预算周期 |
| 0.3 | **修 `reached_intent` 永假** | `executor.py:213` 起的循环里，当 `done and not error` 时置 True；`:252` 的 instruction 分支才真正可用 | 单测：给定「已达成」的 mock 裁判，instruction 从「主目标」切到「自由走位」 |
| 0.4 | **修 gamescaling history 写回** | `executor.py:254-257` 决策后补 `self.motor.record(...)`（参考 `agent.py:1333` 旧路径用法） | `motor.history_len` 在生产路径 >0；重复动作率下降 |
| 0.5 | **去掉「每轮必须推进」+ 加 wait 工具** | `agent.py:201-202` 硬规则 1 改写；`tool_specs()` 增 `wait(reason, seconds)`；`executor` 允许 `no_action` 在不该动时正常结束而非记为失败 | 在对话框/加载画面场景，跳过类按键比例 → 0 |
| 0.6 | **接记忆三处断点** | ① `agent.py:1204,1309` 给 `build_system_prompt` 传 `memory_rules/memory_bindings`；② `agent.py:1562-1564` `learn_rule` 传 `memory`；③ `agent.py:1612-1622` `learn_key` 改走 `learning.learn_key` 统一路径 | 学一条规则 → `profiles/<game>/memory/<group>.jsonl` 出现 episode → 下一轮系统提示词包含它 |

> 0.6 是「玩十小时还是新手」的唯一解，且改动量最小。建议**先做 0.6 和 0.4**，因为它们直接对应两个最刺眼的症状（学了就忘、原地抽搐）。

---

## Phase 1（3–5 天）：让「双系统」名副其实

当前快系统是「工具的从属线程」，每次调用重建 WS。要变成 ProAct 式的**常驻快系统 + 稀疏慢注入**。

| # | 事项 | 说明 | 验收 |
| --- | --- | --- | --- |
| 1.1 | **快系统常驻化** | `FastStream` 由「每次 `lumine()` 新建」改为「进程内单例，按 game 复用」；保持 WGC 会话 + WS 长连接 | 连续 3 次 `lumine()` 只有 1 次握手；跨调用能看到上一段的帧 |
| 1.2 | **开连续视频流** | `agent.py:1100` `pump=True`，并在 `fast_stream` 里实现「多帧环形缓冲」（保留最近 5 帧 + 帧差摘要），替代现在「只留最新 1 帧」 | 每次决策能看到 3–5 帧；`fps` 参数生效 |
| 1.3 | **意图总线上游接线** | `OmniConfig.tools` 传入 `INTENT_TOOL_SPECS`（否则模型根本不知道 `set_intent` 存在，见 `omni.py:54,117-119`）；`fast_stream` 解析 function_call → `IntentArbiter.submit` → `IntentBus.accept` | 日志出现 `intent` 事件；`IntentBus.current()` 不再是恒 NO_OP |
| 1.4 | **意图总线下游接线** | `executor.run(instruction)` 从 `IntentBus.current()` 取目标；`intent.type==NO_OP` 时允许「只观察不动作」 | 意图变化能改变动作指令（可回放验证） |
| 1.5 | **慢系统 tick** | 新增一个低频线程（3–5s）：抓一帧 + 更新结构化「局面摘要」（position / quest_stage / last_actions[5] / failed_attempts[3] / current_subgoal），即使无工具调用也跑 | 空闲时日志仍有 tick 事件；摘要大小恒定（不随会话增长） |
| 1.6 | **结构化输出契约** | 慢系统输出从纯文本改为 JSON（`{intent, expected_effect, check_when}`），带 schema 校验失败回退 | 解析失败率 <1% |

---

## Phase 2（5–8 天）：拟人化核心（感知/反应/克制）

| # | 事项 | 说明 | 验收 |
| --- | --- | --- | --- |
| 2.1 | **帧差变化检测** | numpy 相邻帧差分 → 「哪些区域在动、幅度」；结果既给慢系统做摘要，也给反射层做触发 | 单帧处理 <5ms；能识别「血条下降」「红光闪烁」 |
| 2.2 | **反射层** | 独立小线程：帧差 + 规则表 → 直接发固定按键（后撤/闪避），不经过任何 LLM | 突发事件响应 P50 <250ms |
| 2.3 | **五维门控（游戏版）** | 按 `01_proact_framework.md` §6 定义实现打分；任一分 ≥4 才允许干预，=5 且非演出态才允许打断 | 剧情/加载场景干预率 → 0；战斗场景干预率上升 |
| 2.4 | **action-history 去重** | 最近 N 次同类动作直接否决（含话术去重） | 5s 内同类动作重复率 <10% |
| 2.5 | **dialogue lock / 演出检测** | UI 解析（P-2）识别对话框/过场/加载 → 锁定"跳过类"按键 | 剧情跳过率 <2% |
| 2.6 | **预测-验证回路** | 动作发出时记 `expected_effect`，下一帧校验；不符立即中断并在 commentary 里标注 | 动作失效率可统计；中断延迟 <1 帧 |

---

## Phase 3（长期）：记忆四层 + 人格状态

| # | 事项 | 说明 |
| --- | --- | --- |
| 3.1 | 工作记忆结构化（ProAct 式三层每层一句） | 替代 30 条消息硬截断（`tui.py:50-51`） |
| 3.2 | 情景记忆接 Graphiti（或保持 local-json 但**先确保被调用**） | 需要主环境装 `graphiti_core/fastembed`，或明确宣布「本地 JSONL 为生产后端」并删掉误导性文档 |
| 3.3 | 便签三工具注册 | `profiles.py` 实现已完整，只差注册 + 进提示词 |
| 3.4 | 程序性记忆（动作原语固化） | 高频序列不再过 LLM |
| 3.5 | 情绪状态机 + 偏好表 | 状态驱动表演，避免"念稿式拟人" |
| 3.6 | 遗忘/衰减/冲突裁决 | 记忆可淘汰，错误条目不永久污染 |

---

## 依赖图

```
Phase0(止血) ──┬─> Phase1.1 常驻快系统 ──> Phase1.2 连续流 ──> Phase2.1 帧差 ──> Phase2.2 反射层
               ├─> Phase1.3/1.4 意图总线 ──> Phase2.3 五维门控 ──> Phase2.4 去重
               ├─> Phase1.5 慢 tick ──> Phase3.1 工作记忆结构化
               └─> Phase0.6 记忆闭环 ──> Phase3.2/3.3/3.6
Phase2.5 演出检测 <── Phase2.1 帧差 + P-2 UI 解析
```

---

## 与需求文档 M1–M5 的关系

| REQ 里程碑 | 本路线对应 | 补充说明 |
| --- | --- | --- |
| M1 删 pad + Lithos 切 flash | 已完成大部分（`lithos.py:41-50` 三档都是 deepseek-flash） | 但「三档同模型」需在文档里明确，或把 base 档换真慢思考模型 |
| M2 profile + Graphiti + 便签 | Phase 0.6 + Phase 3.2/3.3 | **建议把「Graphiti 为唯一长期记忆库」降级为可选后端**，因为主环境未装依赖，硬推会卡住整个里程碑 |
| M3 Omni 快系统 + 意图总线 | Phase 1 全部 | REQ 的「指令生效 ≤300ms」在 Phase 2.2 反射层之后才可能达成 |
| M4 提示词构造 + 学习回传 + 重启 | Phase 0.6 + 修 `restart_fast` + `prompt_builder.py:395` 的磁盘落后 bug | `restart_fast` 目前不清任何真实缓存、不重启任何进程 |
| M5 实机联调 | Phase 2 完成后 | 建议以本文 §11 的六个指标作为验收口径 |

---

## 需要立刻拍板的三个决策

1. **Graphiti 还是 local-json？** 主环境没装 `graphiti_core/fastembed`，而需求写「Graphiti 唯一长期记忆库」。要么装依赖（要考虑 neo4j 运维），要么改文档承认 JSONL 为生产后端。**这个不定，M2 无法验收。**
2. **快系统模型？** 现在 Omni 是「1Hz 旁白 + 收手裁判」，而 REQ FR-3 期望它「持续输出指令」。要么把 Omni 提升为真正的指令源（需要连续视频流 + 结构化输出），要么承认它只是旁白，指令权交回反射层+慢系统。**这决定 Phase 1 的工作量。**
3. **「慢系统允许慢」到底多慢？** 现在默认 urgency 0.5 → 落 fast 档 30s 读超时，且 `stream_complete` 硬编码关 thinking。如果真想要「想清楚再做」，需要开 thinking + 用真慢档模型（`base` 目前也是 deepseek-flash）。
