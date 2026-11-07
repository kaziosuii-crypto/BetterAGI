# 04 验收、演练与指标（ProAct 双系统直播）

> 版本 v1.0 · 2026-11-04 · 上游：01_architecture.md（不变量 §14、冻结策略 §6、熔断 §12）、03_code_change_plan.md（风险 D.1-D.8、改造 C.1-C.14）
> 定位声明放在最前面，因为它决定了这份文档怎么写。

## 〇 这份文档解决什么问题

**用户口径：「我自己看着不错我就上直播，你别管验收。」**

所以这不是一份给别人签字用的合规文件，也不产出任何「验收通过证书」。它只服务两件事：

1. **实现者自查的工程门槛**——在把直播间推出去之前，你自己要能回答「凭什么说它能播」。
2. **上线前的演练剧本**——把已知会翻车的那十几条故障，在没人看的时候先手动撞一遍，看它会不会自己爬起来。

它要防的具体失败长这样：agent/tests **203 passed / 4.06s / 0 failed**，而真实直播的 49 份运行记录里，最长一场 491 轮中有 **290 轮（59%）toolCalls 为空**、同一签名 `look|false|AdapterError: window not found` 重复 **152 次**、同一句「窗口一直没回来……先停在这」说了 **75 遍**。单测全绿和能直播之间没有因果关系，这份文档就是把这段差距写成可勾的项。

**判定原则（贯穿全文）：只认落盘证据。**
timeline/*.jsonl 里的事件、incidents/<ts>_<intent_id>/ 下的文件、sticky/<save_id>.md 的字节数、录播分片 sidecar 的 start_ms、进程心跳文件。不认「我看了一下还行」，也不认「单测过了」。

**术语与 01/03 严格对齐**：意图 = intent（intent_id）；快系统 = Omni realtime（嘴）+ GameScaling（手）；慢系统 = PLANNING 阶段；交棒 = handoff（HandoffReport）；便利贴 = sticky（sticky/<save_id>.md，2K 硬顶）；时间轴 = timeline（本场单调毫秒 t）。

---

## 一 三级门槛

三级不是三种验收标准，是三道闸门：**L0 不过不许联调，L1 不过不许开播，L2 不过只是「能播但不像人」**——L2 由你自己看着定，不设签字。

### L0 能跑（冒烟，单人 10 分钟，单意图）

| # | 检查项 | 断言证据 | 对应实现 |
|---|---|---|---|
| L0-1 | pre-flight 六项全绿 | 一次 pre-flight 输出 JSON，六项全为 pass：① 游戏窗口句柄可枚举（前台窗口标题匹配）② vLLM /v1/models 200 且 Omni 握手成功（手嘴两条链路连通 + 额度够）③ GET /danmaku?since=0&limit=1 200 ④ Graphiti 可写 + 上一场 episode 摘要可读 ⑤ sticky/<save_id>.md 合法（两块各 ≤1024 字符，可解析） ⑥ 录制目录可写且剩余空间 ≥ 本场预估 | 03 C.14 orchestrator |
| L0-2 | 一次意图完整走完 | timeline 存在 intent_start → 若干 game_action → handoff 三条事件，intent_id 一致 | 01 §4 主时序 |
| L0-3 | 手解析零失败 | 连续 10 个 chunk 全部 Action.parsed=True、truncated=False；Action.dropped 为空 | agent/gamescaling_action.py:70 |
| L0-4 | 三槽位真的进了手模型 | 抓一次请求体：system 里同时出现 GAME_NAME/GAME_RULES/KEY_BINDINGS 三段，且保留 **Output Format** 原文 | agent/prompt_builder.py:365-370、03 C.4 |
| L0-5 | 便利贴/记忆正文不进快系统上下文 | 抓 Omni 建会话的**请求体**（system + messages 全量文本）grep key_binding / game_rules 的实际内容：命中数必须为 0。注意：IntentPacket 本身**允许**携带 sticky（编排层要用它拼手模型三槽位），禁止的是把它塞进 realtime 的对话上下文 | 01 §3 职责分离、02 §2.2、00 D12 |
| L0-6 | 便利贴读写各一次 | read_sticky 返回非空、write_sticky 返回 ok 且文件 mtime 与字节数变化 | agent/sticky.py（新） |
| L0-7 | 时间轴事件各有样本 | intent_start / game_action / speech / handoff / state 各 ≥1 条，字段齐全 | agent/timeline.py（新） |
| L0-8 | 慢系统 2s deadline 生效 | 故意让慢系统吐 3s：slow_abort 事件在 ≤2100ms 出现 | agent/lithos.py:406、03 C.8 |

**L0 判定**：8 项全勾才算过。任何一项靠「读代码觉得对」不算过——必须有一次实际输出贴在记录里。

### L1 能播（30 分钟无人值守）

| # | 检查项 | 目标 | 抓取方式 |
|---|---|---|---|
| L1-1 | 零熔断 | incident_count == 0 | incidents/ 目录为空 |
| L1-2 | 零串台 | stray_event_count == 0 | 每意图 omni.stray 计数（03 D.1 第 2 步） |
| L1-3 | 零会话泄漏 | 任意 1s 采样 active_realtime_sessions ≤1，意图结束后 ==0 | orchestrator 采样日志 |
| L1-4 | 零长静默 | 非剧情段 speech 间隔 >5000ms 的次数 == 0 | timeline 计算，剧情段需 speech.mode=story 打标豁免 |
| L1-5 | 零空转 | 无动作且无解说的时长占比 <10% | timeline 区间扫描（历史基线 59% 空轮） |
| L1-6 | 每意图必有交棒 | handoff_count == intent_count，含 aborted | 01 §14 不变量 6 |
| L1-7 | 时间轴单调 | 断言器 0 violation | 见 §2 A4 |
| L1-8 | 录制可用 | ≥3 个 10 分钟分片，每个 sidecar 有 start_ms/frame_no，且 start_ms 与 timeline 同源 | agent/recorder.py（新） |
| L1-9 | 资源稳定 | 进程 RSS 增长 <10%，线程数增长 <20，无 fd 泄漏（每意图不涨） | 30 分钟首尾各采一次 |
| L1-10 | 不重复自己 | 同一句台词 ≤2 次/30 分钟；同一错误签名 ≤3 次/30 分钟 | timeline speech.text 去重计数 |

**L1 判定**：L1-1/2/3/4/6 为硬项，任一不过即不许开播；其余允许 1 项 warn，但必须在记录里写明。

### L2 像人（主观 + 客观，不签字）

客观（能算）：

| # | 指标 | 目标区间 | 说明 |
|---|---|---|---|
| L2-1 | 意图时长分布 | 主体落在 20-120s；<5s 占比 <10%；>300s 占比 <5% | 太短是碎步没规划，太长是陷在原地 |
| L2-2 | 解说-动作时差 | p95 < 1500ms | 「说到就看到」，超过就是配音 |
| L2-3 | 失败反应多样性 | 同类失败连续 3 次，回应文本相似度 <0.6 | 复读「啊，我又死了」就是破功 |
| L2-4 | 卡关行为链完整 | 先有抱怨/自言自语 ≥1 句，再交棒；不允许无声冻结 | 01 §6 发呆圆场 |
| L2-5 | 仪式完整 | 开播话术 1 次、下播话术 1 次、读上舰感谢 ≥1 次 | 用户明确要的四个仪式 |
| L2-6 | 点名回应率 | realtime 不答（设计如此），慢系统下一轮必须提及该观众 ≥1 次 | 冲突时「执行优先、事后议」 |

主观（你自己 + 2 个不看代码的人，看 15 分钟）：

- 问一句「这是 AI 还是人？」——≥2 人答「像人 / 半信半疑」算过；都答「肯定是 AI」= L2 不过。
- 问「哪一刻最不像人」——把答案原话记下来，这条比任何指标值钱。

---

## 二 不变量断言（01 §14 七条 + 两条新增）

| 编号 | 不变量 | 在哪断言 | 触发时的处理 | 违反时的日志字段 |
|---|---|---|---|---|
| A1 | len(active_realtime_sessions) ≤ 1 | orchestrator 建会话入口 + 每 1s 采样 | 拒绝新建；若已存在则强制 close() 旧会话并记违规，不得静默复用 | orchestrator.sessions, invariant=A1 |
| A2 | 慢系统运行时 active_realtime_sessions == 0 | dispatch_task 前 + PLANNING 期间采样 | 阻塞进入 PLANNING（等会话关闭）；超 500ms 则强关并记违规 | orchestrator.state, invariant=A2 |
| A3 | 每条键位事件落在某个 intent_id 区间 | timeline 写入 game_action 时校验 open intent | 丢弃该事件 + 计数（不许落一条孤立键位） | timeline.orphan_key |
| A4 | 时间轴事件单调不减 | Clock.emit 内比较 last_t | 拒绝写入，落 clock_violation，用 max(last_t, t) 继续（不崩） | timeline.clock_violation, delta_ms |
| A5 | 便利贴总长 ≤2048 字符（各 1024） | sticky.write 原子写之前（temp+rename） | 抛错回给慢系统，文件保持原样；不回滚半截写 | sticky.reject, chars, limit |
| A6 | 每个意图必然产生 HandoffReport | 意图关闭钩子（含异常路径/finally） | 超时 2s 未收到则自动生成 aborted 报告 | handoff.missing, intent_id |
| A7 | 分片 start_ms 与时间轴同源 | recorder 只从 timeline.Clock 取一次，写完读回校验 | 不一致 → 记 incident 但不中断直播（可回放性降级） | recording.mismatch, sidecar_ms, timeline_ms |
| **A8（新增）** | 每意图 stray_event_count == 0 | omni.collect 只接受 response_id 匹配事件（agent/omni.py:359） | 丢弃 + 计数；>0 告警，>10 记 incident | omni.stray, dropped_ids |
| **A9（新增）** | 便利贴/记忆正文不得进入快系统的**提示文本** | ① IntentPacket 构造处：sticky 只允许出现在 packet.sticky 字段，其余字段值里出现 key_binding/game_rules 正文即 fail fast；② Omni 请求体发出前：system+messages 序列化后 grep 便利贴正文，命中即拒发 | 构造/发送即 fail fast（拒绝发会话，不许降级放行） | packet.forbidden_field、omni.prompt_leak |

落点提醒：A1/A2 属于 orchestrator（03 C.14 P1），A3/A4 属于 timeline（03 C.14 P0），A5 属于 sticky（03 C.12 P0），A6 属于 orchestrator 关闭钩子，A7 属于 recorder。**A3 与 A6 是最容易「看起来写了其实没生效」的两条**，断言必须放在写入路径上，不能放在事后统计里。

---

## 三 演练剧本（13 条）

每条都在「没人看的时候」跑，跑完把证据留在 _diag/rehearsal/<日期>/。**失败即回滚**一列写的是「这个剧本不过时，你退回去的那个状态」。

### S1 手模型输出解析失败（parsed=False / 截断 / 连续 no-op）

| | |
|---|---|
| 注入 | 把 BETTERAGI_GAMESCALING_URL 指向 stub：A) 返回 <\|action_start\|>garbage<\|action_end\|>；B) 截断（无 action_end）；C) 连续 3 次显式 no-op |
| 预期 | decide 抛 MotorParseError（**不许**走 label_action 造 6 组假动作，agent/motor.py:52/:141）→ 冻结键位（全释放）→ 时间轴写 incident → 画面贴「程序出错了」文字帧（agent/ut_textframe.py:44）→ 停推流 → 写 incidents/<ts>_<intent_id>/ → 退进程 |
| 判定通过 | 1) incidents 目录 5 件套齐全：request_body.json / raw_reply.txt / parsed.json / timeline_slice.jsonl / proc.json；2) raw_reply.txt 非空且含原文；3) timeline 最后一条是 incident；4) 冻结到退进程 ≤3000ms；5) 之后**没有任何** game_action 事件（不许继续空转）；6) history_text 写的是 <parse_failed> 而不是 no-op（agent/gamescaling_action.py:81） |
| 失败回滚 | 回到 label_action 兜底（当前行为）。若走到这步，说明整条熔断链未实现，L1-1 直接判不过 |

### S2 Omni 断线重连（预置台词撑场）

| | |
|---|---|
| 注入 | 意图执行中 close WS（stub 服务主动 RST，或本机防火墙 drop 该连接 10s） |
| 预期 | 本意图内最多重连 1 次（03 C.2 P1）；重连期间从预置台词池吐字撑场；手继续跑不停；重连成功后不读旧 response |
| 判定通过 | 1) timeline 有 1 条 net_reconnect；2) 断线窗口内 speech 覆盖率 >0，静默 ≤3000ms；3) 本意图 session_start 计数仍为 1（不许偷偷开第二个）；4) stray_event_count == 0；5) 重连失败时走 request_handoff(blocked) 而不是卡死 |
| 失败回滚 | 关掉自动重连，改为「断线即交棒」（降级但可控） |

### S3 慢系统超预算（900ms 贴图 / 2s 降级）

| | |
|---|---|
| 注入 | 慢系统 stub：A) 1200ms 才吐第一个 token；B) 3000ms 才结束 |
| 预期 | A：900ms 时画面中央贴「大肥鱼CPU烧了」；B：deadline_s=2.0 中止该轮（agent/lithos.py:406），进入降级——用上一条 plan/钩子兜底交棒，不产出半截 markdown 任务 |
| 判定通过 | 1) freeze_overlay 的 t 落在 [900,1000]ms；2) slow_abort 在 ≤2100ms；3) 画面恢复 ≤500ms；4) dispatch_task 未被调用（没有半截任务下发）；5) 慢系统超时后快系统会话已关闭（A2 成立） |
| 失败回滚 | deadline 恢复 60s（agent/lithos.py:352 现状）+ 关闭 thinking 分支；此时只能全快系统常驻，放弃重规划 |

### S4 超时串台（旧会话未取消就开新会话——历史根因）

| | |
|---|---|
| 注入 | stub 让 collect 永远等不到 response.done；第 12s 由脚本强制 request_handoff 并开新意图 |
| 预期 | 单帧 4s / 整轮 12s 超时抛 OmniTimeout 且 **close transport**（agent/omni.py:350/:359、:293/:317 不再只 return）；旧 response_id 事件一律计 stray 丢弃 |
| 判定通过 | 1) omni.stray ≥1 且有对应丢弃日志；2) 新意图首帧 t > 旧会话 close 的 t；3) 哨兵词检查：旧意图 speech 里埋 SENTINEL_OLD，新意图 timeline 的 speech 文本中该词出现次数 == 0；4) 新意图 30s 内正常出 5 个 game_action（没被拖死） |
| 失败回滚 | 禁用 _inflight 复用与双开会话，退回「意图完成才允许下一次规划」——等于放弃打断能力，但不会串台 |

### S5 卡关连续 N 次 → thinking → 发呆 → 交棒

| | |
|---|---|
| 注入 | 把目标物移到墙后（或 mock 画面逐帧完全相同），让 realtime 连续 3 次 report_blocked |
| 预期 | 第 3 次进入 PLANNING_THINKING，画面挂贴图 5-10s，播发呆台词池；thinking 完仍无解 → 交棒 |
| 判定通过 | 1) state 事件链 EXECUTING→HANDOFF→PLANNING→PLANNING_THINKING；2) stuck_count 到 3 才切（不许 1 次就开）；3) 发呆台词 ≥1 条 speech；4) 总冻结 ≤10000ms；5) 结束时 handoff.reason ∈ {blocked, failed}；6) 交棒载荷里 seen_summary 说明了「卡在哪」 |
| 失败回滚 | thinking 保持常关（用户默认口径），卡关判定退化为「20s 无进展」 |

### S6 弹幕突袭（点单与当前任务冲突）

| | |
|---|---|
| 注入 | 执行中向 /danmaku 源注入一条「主播去打那个 boss」 |
| 预期 | 快系统不读弹幕（IntentPacket 里没有弹幕）；当前意图不被打断；弹幕进环形缓冲，下一次 PLANNING 时 read_danmaku 才读到；下一轮任务 markdown 体现「观众提到 X，但先把当前做完」 |
| 判定通过 | 1) 注入到本意图结束之间 timeline **无** viewer 事件（说明没被读）；2) buffer 计数 +1；3) 下一意图 task_md 含该关键词或显式 defer 记录；4) 当前意图时长不超过基线 120%（没被拉长） |
| 失败回滚 | read_danmaku 一律返回空（关掉弹幕读取），直播照跑 |

### S7 便利贴超 2K / 非法内容

| | |
|---|---|
| 注入 | write_sticky 传 3KB；另一次传 key_binding 里塞脚本标签；再一次传空 game_rules |
| 预期 | 拒绝写入并回错给慢系统；文件字节与 mtime 均不变（原子写，agent/profiles.py:251 的可搬实现）；不合规字段同样拒绝 |
| 判定通过 | 1) 前后 sha256 相同；2) sticky.reject 日志带 chars 与 limit；3) 慢系统收到错误后重试一次能成功（整体重写语义）；4) 文件始终 ≤2048 字符 |
| 失败回滚 | 上限放宽到 4096 并记录偏离（agent/profiles.py:33 现状），但必须在 01 里同步改口径 |

### S8 时间轴时钟倒退 / 分片 start_ms 与时间轴不一致

| | |
|---|---|
| 注入 | monkeypatch Clock.now_ms 返回上一值；另一次手工把某片 sidecar 的 start_ms 改早 5s |
| 预期 | emit 检测 t < last_t → 拒绝写 + clock_violation；对齐校验失败 → recording.mismatch 并把该片从索引里降级，不拿它做对齐 |
| 判定通过 | 1) 断言器发现恰好 1 处 violation；2) 主循环不崩、继续跑 ≥60s；3) 后续分片正常；4) 降级后的索引里不含坏片 |
| 失败回滚 | 关闭录制只留时间轴，复盘用 OBS 录屏替代（此时 L1-8 作废，需在记录里写明） |

### S9 录播分片缺失 / 首帧未写 start_ms

| | |
|---|---|
| 注入 | 录制开始后删除第 1 片；或让 sidecar 写入失败（占用文件句柄） |
| 预期 | 每片封口时校验 sidecar 存在且 start_ms 与 timeline 同源（不变量 A7）；缺失记 incident 但**不中断直播** |
| 判定通过 | 1) 校验器报 1 处缺失；2) 直播继续 ≥5 分钟；3) 后续分片 sidecar 正常；4) 索引文件明确标注 missing_segment |
| 失败回滚 | 停止分片，改单文件录制（回放体验下降但不断流） |

### S10 进程整体卡死 >60s

| | |
|---|---|
| 注入 | 给主线程注入 90s 阻塞（或外部 SIGSTOP 90s） |
| 预期 | 独立 watchdog 60s 无心跳 → 切「休息一下」画面 → 先发全键位释放 → 落账 → 重启进程（新 session_id，本场单调 t 继续） |
| 判定通过 | 1) 释放事件早于冻结（不卡键）；2) 屏幕出现休息画面；3) 重启后 timeline 连续且 session_id 变了；4) 重启后首个意图 ≤30s 内启动；5) 事故目录记录了卡死时刻 |
| 失败回滚 | 只保留「人工 kill + 重开」，不自动重启（开播时你要在旁边） |

### S11 下播流程（四件事全做）

| | |
|---|---|
| 注入 | 手动触发 SIGNING_OFF（到点） |
| 预期 | 下播话术 ≥1 条 → 本场 episode 记忆写入（layer=episode，带 session_id）→ 分片索引与时间轴落盘 → 为下一场写钩子（set_hook）→ 退出（或待机） |
| 判定通过 | 四件事各 1 条证据：signoff speech 事件、memory 写入日志（agent/memory.py:409）、session_index.json、hook 文件；重开一场能读到上次钩子与「上次播到哪」 |
| 失败回滚 | 只写 episode，跳过钩子（下一场人工开场）；不影响下一场可播 |

### S12 三槽位/数据边界回归（便利贴不许漏给快系统）

| | |
|---|---|
| 注入 | 便利贴写入一条测试规则 TEST_RULE_42；跑一个意图 |
| 预期 | 发往手模型的 system 里能 grep 到 TEST_RULE_42；发往 Omni 的 session_update / 帧里 grep 不到 |
| 判定通过 | 1) 手侧请求体命中；2) Omni 侧零命中（含 tools 描述）；3) 便利贴本身未被改写（mtime 不变） |
| 失败回滚 | 在 IntentPacket 构造处加白名单（A9）并重测；不能靠 prompt 自律 |

### S13 会话生命周期长跑（每意图一建一毁 ×20）

| | |
|---|---|
| 注入 | 脚本连续触发 20 个短意图（每个 3-5s） |
| 预期 | 20 次 session_start / 20 次 session_close；无 fd/线程/RSS 增长；每次冷启 1-3s（01 §4 已接受） |
| 判定通过 | 1) start==close==20；2) 结束时 active_realtime_sessions == 0；3) fd 数差 ≤5，线程数差 ≤5；4) 冷启耗时分位数记录（这就是 01 §6「先测再定」的输入） |
| 失败回滚 | 退到「一条 WS 常驻 + 每次意图 cancel_inflight 清场」，代价是上下文不绝对干净 |

---

## 四 指标与打点

打点统一进 timeline（agent/timeline.py），一行一个事件，字段最小集见附录 A。所有分位数按**本场**统计，跨场只存汇总，不累计进上下文（01 §9）。

| 指标 | 字段 | 目标区间 | 为什么这个指标能抓住「不像人」 |
|---|---|---|---|
| Omni 首 token | speech.first_token_ms | 先测再定；>2000ms 的轮次 <5% | 真人接话 200-600ms。>2s 的沉默不是思考，是掉线；分布长尾比均值更能暴露卡顿 |
| Omni 单轮 | speech.turn_ms | p50 记录、p95 <4000ms | 一轮说 8 秒还没说完，画面早变了，解说必然和画面对不上 |
| 手单步决定 | game_action.decide_ms | 实测 p95 = 740ms（关思考）/ 1490ms（开思考）；阈值 = 实测 p95 × 1.15（见 `07_errata_measured_latency.md`，原「>300ms 占比<2%」已作废） | 超过这档 p95 就不是「手在动」而是「手在想」；尾巴直接对应错按 |
| 超预算 miss | tool.miss_rate | <2% | 预算没人守（03 D.3）时这个数会在 20% 以上而不自知 |
| 步抖动 | game_action.jitter_ms（actual-plan） | p95 <50ms，>100ms 丢积压 | 抖动大 = 动作和画面脱节，观众看到「乱按」 |
| 意图时长 | intent.dur_s | 主体 20-120s | 太短是碎步（没有规划），太长是原地打转——两个极端都不像人 |
| 每意图轮次 | intent.turns | 记录，与时长相关性 >0.5 | 时长涨而轮次不涨 = 空转（历史 59% 空轮的形态） |
| 空转率 | idle_ratio | <10%（L1）、<5%（L2） | 「没动作也没说话」的时长占比，是「像不像人在玩」最直接的单一指标 |
| 静默间隔 | speech.gap_ms | >5000ms 次数 == 0（剧情段豁免） | 用户口径：绝不静默超 5 秒 |
| 卡关交棒率 | handoff.reason=stuck 占比 | <20% | 太高说明意图给得不清楚，太低说明它在死磕 |
| 同目标重复交棒 | handoff.repeat_same_target | ≤2 次 | 第 3 次交棒还没换思路 = 慢系统也在复读 |
| 同一错误签名 | tool.sig_count | ≤3 次/30min | 历史 152 次同一签名（look\|false\|AdapterError），这条直接对着旧病 |
| 同一句台词 | speech.text_dup | ≤2 次/30min | 历史 75 遍同一句告别——复读是最大的破功点 |
| 便利贴体积 | sticky.chars | ≤2048，>1800 预警 | 超限会挤掉 key_binding，手模型直接不会按 |
| 时间轴写延迟 | timeline.lag_ms | p95 <50ms | 延迟大 = 复盘对不上帧，事后无法判断「是不是真像人」 |
| TTS 估时误差 | speech.est_err_ms | p95 <400ms 或 <20% | 估算偏大就静默，偏小就抢话，两种都不像人 |
| 熔断次数 | incident_count | ==0（L1） | 一次熔断 = 一次直播事故，没有「轻微」这一档 |
| 串台计数 | omni.stray | ==0 | 串台的表现是「上一句的 handoff 在这一句生效」，节奏直接崩 |
| 会话泄漏 | session_leak | ==0 | 泄漏的典型症状是「越播越卡、越卡越乱」 |

**先测再定的三项（不要现在拍数字）**：Omni 首 token、手单步 decide、冷启耗时。方法：S13 + 一次 30 分钟空跑，把 p50/p95 写回本表，再定 L2 阈值。01 §6 已经明确「不预先假定慢，也不假定快」。

---

## 五 上线前 30 分钟「像人」自查（人肉观察表）

坐在旁边看，不看代码、不看日志，只记时间点。发现即记缺陷，**缺陷不是「改进建议」，是「这条不过 L2」**。

| 观察项 | 缺陷阈值（达到即记） | 记录方式 |
|---|---|---|
| 节奏 | 连续 30s 只有一个动作且无解说；或 30s 内 20+ 次无意义按键 | 记起止时刻 |
| 停顿 | 非剧情段静默 >5s 一次 | 记时长；如果是「想事情」请它说话 |
| 失败反应 | 同一句道歉/自嘲 ≥3 次/10 分钟 | 抄原句 |
| 重复口头禅 | 同一句 ≥3 次/10 分钟；或同一句式占比 >50% | 抄原句 3 条 |
| 无意义操作 | 同一处来回走 >20s；反复开关菜单 >3 次；对着墙按攻击 >5 次 | 记场景 |
| 解说对不上画面 | 说到的事 3s 内画面没出现，≥5 次/10 分钟 | 记「说了什么 / 画面实际是什么」 |
| 解说迟到 | 画面事件发生 3s 后才被提到，≥3 次/10 分钟 | 记差值 |
| 卡关表现 | 卡住 >15s 仍无任何台词 | 记时长；应先有抱怨再交棒 |
| 弹幕 | 观众点名 3 次无任何回应（下一轮也算） | 记观众名与时刻 |
| 仪式 | 开播/下播话术缺失或明显模板味（同一句念稿） | 抄文本 |
| 称呼 | 出现「粉毛妹」「那个男的」这类外貌标签而不是角色名 | 抄原句（用户明确讨厌） |
| 一句话总评 | —— | 「像人 / 半像 / 明显是 AI」三选一 + 一句理由 |

**用法**：30 分钟结束后，把这张表当 diff 看——哪几行有记录，就去 timeline 里对那一刻的 speech/game_action，把「感觉」变成可复现的字段值。这一步是整个文档里最容易被跳过的一步，也是最有效的一步。

---

## 六 回滚与止损

### 必须停播的触发（任一命中）

| # | 触发 | 判据 |
|---|---|---|
| T1 | 手解析失败/截断 | MotorParseError 抛出（S1） |
| T2 | 进程无心跳 | >60s 无 heartbeat |
| T3 | 画面冻结 | 实际冻结 >10s（不含 thinking 的 5-10s 授权窗口） |
| T4 | 键位卡住 | 检测到按键按下未释放 >2s |
| T5 | 推流中断 | 观众侧黑屏 >30s |
| T6 | 慢系统连续超预算 | 连续 3 次 slow_abort 且兜底任务也失败 |
| T7 | 错误风暴 | 同一错误签名 >20 次/分钟 |

### 停播动作顺序（硬序，不许换）

1. **冻结键位**：先发全释放（所有按下的键抬起来），再停止发键。顺序反了就会「角色一直往前走」。
2. **落账**：写本场 episode + timeline 收尾事件（signoff/aborted），确保时间轴最后一条是终止事件。
3. **停推流**：关闭 recorder/OBS，让当前分片正常封口并写 sidecar。
4. **写事故目录**：incidents/<ts>_<intent_id>/ 五件套（S1 判据）。
5. **退进程**：os._exit(2)。**最后做**——先退进程会丢时间轴尾部，事故就不可复现了。

### 回滚级别

| 级别 | 动作 | 适用 |
|---|---|---|
| 代码级 | 按 03 的 C.x 分文件 revert（每个改造点单独提交，就是为了能这样退） | 单个改造引入回归 |
| 运行级 | 四个 feature flag 单独关：enable_danmaku_read / enable_thinking / enable_recording / enable_auto_reconnect | 某一子系统不稳但主体可播 |
| 产品级 | 降级「只看不玩」：保留解说与弹幕互动，停掉手模型 | 手模型/推理服务整体不可用 |

**最后一条纪律**：单测全绿不代表能播；本文档的所有「过」都必须有一次落盘证据（timeline / incidents / sidecar / sticky 文件）。没有证据的勾，一律算没过。

---

## 七 附录

### A 时间轴最小事件集（字段先小后大）

字段名以 `02_schemas.md` §6 为准：本表的 `t` 就是落盘的 `t_ms`，其余字段进 `payload`；控制事件的完整定义见 02 §6.2b（本表只列断言要用的最小集）。

| 事件 | 必填字段 | 备注 |
|---|---|---|
| intent_start | t, intent_id, task_md, save_id | 意图边界唯一真源 |
| intent_end | t, intent_id, reason | 与 handoff 成对 |
| game_action | t, intent_id, step, keys, mouse, planned_ms, actual_ms, decide_ms | jitter_ms = actual-planned 由消费方算，避免双写 |
| speech | t, intent_id, text, est_ms, mode(talk/story/signoff), first_token_ms | mode=story 才豁免静默阈值 |
| viewer | t, reader(slow), count, action(read/reply/skip) | 只有慢系统会写 |
| handoff | t, intent_id, reason(done/failed/blocked/aborted), conclusion, seen_summary_ref | 见闻总结存独立文件，事件里只放引用 |
| freeze | t, intent_id, reason(slow_think/incident/rest), ms | 贴图与恢复各一条 |
| incident | t, intent_id, kind, dir | 必须是该 session 的最后一条 |
| net_reconnect | t, intent_id, attempt | S2 判据 |

### B 断言脚本（跑在每次演练之后，输入只有 timeline 目录）

必须能离线跑、不连任何服务，输出「断言名 / 通过与否 / 反例行号」：

1. A1/A2 会话不变量（读 orchestrator 采样日志或 state 事件）。
2. A3 键位归属：每条 game_action 前必须有 open 的同 intent_id 的 intent_start。
3. A4 单调：t 序列不允许下降。
4. A6 配对：intent_start 数 == handoff 数（含 aborted）。
5. A7 同源：每片 sidecar start_ms 与 timeline 中该片首帧事件 t 差 ≤1 帧。
6. 空转率与最长静默（L1-4/L1-5）。
7. 重复检测：speech.text 去重计数、tool.sig 去重计数（L1-10）。
8. 熔断完备：每个 incident 事件后不允许再有 game_action。

### C 与 03 落地顺序的挂载

| 03 的周次 | 完成后应该过哪些 | 证据 |
|---|---|---|
| 第 1 周（omni 超时 + response_id + fast_session + motor 抛错） | L0-2/L0-3、S1、S4、S13 | incidents/、stray 日志 |
| 第 2 周（mailbox + decide deadline + timeline） | L0-7、L1-5/L1-7、S8 | timeline 断言器输出 |
| 第 3 周（orchestrator + handoff 工具 + 2s deadline） | L0-8、L1-1/L1-3/L1-6、S3/S5/S10 | state 事件、slow_abort |
| 第 4 周（sticky + 9 工具 + 弹幕 + 录制 + incident） | L0-4/L0-5/L0-6、L1-8/L1-10、S6/S7/S9/S11/S12 | sticky 文件、sidecar、buffer |

**一句话收尾**：这张表里的每一条，都是为了让你在开播那一刻不用赌——赌「应该没事」。赌输了观众会看见，而观众记不住你 203 个通过的测试。
