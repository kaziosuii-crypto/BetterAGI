# 当前 loop 的缺陷清单（分级 + 证据 + 可感知后果）

> 证据来源：快路径精读（`executor.py`/`omni.py`/`fast_stream.py`/`motor.py`/`adapter.py`）、记忆学习层精读（`memory.py`/`learning.py`/`prompt_builder.py`/`profiles.py`/`lithos.py`）、运行证据（`_diag/agent-runs`、单测、进程端口）。
> 行号为当前工作树行号。判定标准：**是否在生产路径上真实生效**。

## 0. 一句话结论

> 这条链路名叫「实时快系统」，实际是**每 ~200ms + 1 个 HTTP RTT 的串行开环**（拍照 → 决策 → 盲按），Omni 只是 1Hz 旁白 + 收手裁判；意图总线 / 记忆 / 便签 / 门控全套**写好了但没接线**（仅单测覆盖）；唯一的实时性手段（只留最新帧）同时就是最大的信息损失源。

## 1. 真实运行链路（现状）

```
慢 LLM (DeepSeek-flash, thinking 关闭)
  └─ ReAct: for step in range(1, 7)              agent.py:1667
       └─ tool_call: lumine(goal, done_when, text)
            ├─ motor.health(timeout=3)           agent.py:1183-1190  （不过 → 直接失败，不回退）
            ├─ narrator.start()  → WS 握手        agent.py:1195 → fast_stream.py:91 → omni.py:248
            └─ FastExecutor.run()  【同步阻塞】   executor.py:200-312
                 while not done:
                   抓帧 (WGC 18~34ms)            executor.py:137 → adapter.py:378
                   push 1 帧给 Omni（覆盖式）      executor.py:244-246
                   motor.decide(帧, system_prompt) HTTP 同步，无上限 60s   motor.py:141,30
                   adapter.play_steps(≤6 步 × 33ms = 198ms 盲执行)        adapter.py:689,716
                   嘴线程 1Hz: Omni 说一句 + 判 [DONE]                    executor.py:147-197
             └─ narrator.stop() → WS 关闭          agent.py:1226
```

**关键事实**：
- 没有常驻实时 tick。常驻的只有「每个 FastStream 一个 asyncio loop 线程」+「一个嘴线程」+「一个 WGC 抓帧会话」。
- 每次 `lumine()` 都重建 WS 会话 → **跨调用没有视觉记忆、没有对话记忆**（`fast_stream.py:4` 注释自述 240s 视频历史随 stop 丢弃）。
- `pump=False`（`agent.py:1100`）使「连续视频流」退化为「每决策 1 帧」，`fps=2.0` 参数无效。

---

## 2. P0 缺陷（直接决定「玩不好」）

### P0-1 Omni collect 无超时，且超时只放弃不取消 → 协议层永久错位

- `omni.py:350-406` 的 `collect` **裸 await recv_json()**，没有 `wait_for`/`deadline`（对比 `_drain_until_done` 有 budget_s，`omni.py:293-315`）；
- 调用侧 `fast_stream.py:153` 用 `.result(timeout=45.0)`，抛出的是 `concurrent.futures.TimeoutError`，**事件循环里的 `_do` 仍在跑且仍持有 recv**；
- 下一轮 `cancel_inflight`（`omni.py:317-328`）→ `_drain_until_done` 也 recv ⇒ 同一 WS **双并发 recv**，websockets 抛 `cannot call recv while another coroutine is already waiting`；即便不抛，被遗弃的 collect 会吞掉下一轮事件写进无人 await 的 reply，主 collect 读到残帧。
- `omni.py:367-370, 393-399` 的注释显示**已经踩过一次同类错位坑**，但只修了 break 时机与 error 分支，没修超时。

**可感知后果**：解说卡住/串台，判「收手」的证据来自上一段的画面；后续所有决策建立在错位的事件流上，且**不会自愈**（下一条才恢复）。

### P0-2 手和嘴是两个模型且互不通信 → 观测无法影响行动

- `executor.py:159` 的解说只写进 `commentary`；
- 循环里 instruction 只看 `reached_intent`（`executor.py:252`），而 `reached_intent` 在 `executor.py:213` 初始化为 `False` 后**从未被置 True**（全仓仅 :213、:252 两处命中）→「主意图达成后切自由走位」是死分支，`motion_instruction` 永不生效；
- 结论：**Omni 唯一的权力是「停/不停」**，不能改任何一次动作向量、不能改下一步。

**可感知后果**：嘴里说「前面有坑，往左一点」，手该往右还是往右。

### P0-3 全开环，且没有任何端到端时效保证

- 串行：抓帧 18–34ms（首建 150ms）+ decide（实测 0.34–0.61s，`fast_system.py:9`；**无上限 60s**，`motor.py:30`）+ 198ms 盲执行；
- `DEFAULT_TIMEOUT=60.0` **大于**整段预算 `budget_s=20.0`（`executor.py:200`）→ 一次卡住就能吃掉 3 个预算周期；
- 200ms 动作窗口内**零观测**；按键时刻与抓帧时刻之间隔着一整个 RTT；
- 代码里只有局部常量（step_ms=33、narrate_every_s=1.0、done_confirm=2、budget_s=20、max_decisions=10），**没有任何端到端延迟预算或 SLA 检查**。

**可感知后果**：Boss 抬手 → 系统 0.6s 后才决定「往左闪」→ 按键时已经晚了；观感是「反应慢半拍 + 手不听使唤」。

### P0-4 慢系统与记忆彻底脱钩

- `agent.py` 全文 grep `memory|Episode|recall` **零命中**；
- `build_system_prompt` 有 `memory_rules`/`memory_bindings` 参数（`prompt_builder.py:266-267, 289-293`），生产调用只传 profile（`agent.py:1204, 1309`）→ **记忆从不进提示词**；
- 写侧：`memory_wire.WRITEBACK_TOOLS=("act","learn_key","restart","restart_game")`（`memory_wire.py:18`），但 live 工具面只有 `look/lumine/learn_key/learn_rule/restart_fast/end_test`（`agent.py:1417-1545`）→ `act` 永不触发；`learning.learn_rule` 调用**不传 memory**（`agent.py:1562-1564`）；工具 `learn_key` 走 `profiles.apply_learned_key`（`agent.py:1612-1622`）**绕过** `learning.learn_key`（唯一会 `add_episode` 的路径）；
- 唯一真实写入在 TUI：`tui.py:1001-1014`，且 meta 只有 tool/args → **冲突检测永不触发**（`memory.py:207-221` 要求 kind=="keymap" 且 meta 有 key/effect）。

**可感知后果**：同一款游戏玩 10 小时，第 10 小时和第 1 小时一样从零摸索；「学会了」只在当轮提示词里生效，重启即忘。

### P0-5 设计哲学反向：系统默认「必须动」，而不是「默认不动」

- `DIRECT_SYSTEM_PROMPT` 硬规则 1：「每轮至少推进一步」（`agent.py:201-202`）；
- `executor.py:283-287` 连续 3 次空动作 → `no_action` 退出（视作失败）；
- 而 ProAct 的处理是：五维全 <4 时**主动把行为置空并 early-exit**。

**可感知后果**：在剧情演出、对话选项、加载画面里也硬按 —— 跳过剧情、误选选项、把 NPC 对话点掉。这是「不像人」的最直接来源。

---

## 3. P1 缺陷（决定「不像人」）

### P1-1 gamescaling 的历史在生产路径从不写回 → 策略模型永远单帧无记忆

- `motor.py:110-128` 有 history / `history_len=5` / `record()`；
- 全仓 `.record(` **只有 `agent.py:1333`（旧 motor 工具路径）**命中；
- `executor.py:254-257` 只 `decide` 不 `record` ⇒ `history_len` 恒 0。

**后果**：动作没有惯性/连贯性，表现为原地抖动、重复同一动作、走两步停一下。

### P1-2 「收手」判定弱：两次 1Hz 采样 + 理由 ≥4 字

- `done_confirm=2`、`narrate_every_s=1.0`，每轮 ask 最坏 45s ⇒ 从条件成立到真松手 **≥2 个 Omni RTT**，期间手仍 198ms/次盲按；
- `streak` 是主线程与嘴线程共享的可变 dict（`executor.py:209`），无锁；
- 理由只校验长度 ≥4（`executor.py:53, 75-77`）⇒ 连续两次「[DONE]+4 字理由」的幻觉即可提前收手（`executor.py:29-30` 自述实测有此幻觉）。

**后果**：要么早停（走到一半就松手），要么磨蹭（已经到目标还在按）。

### P1-3 无外部中断通道

- `stop` 是 `run()` 局部 Event（`executor.py:207`），只在 :295 由自己 `set`；
- `agent.py:1221` 同步阻塞调 `run`，没有 cancel/abort 接口；
- `intent.py:101-106` 的 NO_OP 立即生效、`arbiter.py:47-48` 的 urgency≥0.9 抢占**都写好了但未接线**。

**后果**：观众说「别打了」，或慢系统发现「方向反了」，都只能等 20s 预算或 10 次决策耗尽。

### P1-4 失败即终止、无恢复、无归因

- 截帧连错 3 → `capture_error`；motor 异常 → `motor_error` 立即 break；空动作连 3 次 → `no_action`；
- 无重试、无换策略、无失败归因回灌；
- 一次瞬发 502（`motor.py:164-166` 自述偶发）即可判死整段。

### P1-5 `restart_fast` 名不副实

`agent.py:1569-1610` 实际做四件事：① profile 为空或 `BETTERAGI_FAST=0` → 失败；② 尝试清 `_CACHE/_BUNDLE_CACHE/_PROMPT_CACHE` —— **这三个属性在 prompt_builder 里根本不存在**（grep 零命中），恒 no-op；③ `learning.verify_restart` 从磁盘重开 profile 比对指纹/版本；④ 探手 motor.health、探嘴让 Omni 说「在的」。
**没有任何进程被杀、也没有把新 system prompt 推给运行中的快系统**。
且 `prompt_builder.py:395` 自认「磁盘永远落后内存一档（真实 bug：learn 之后 restart_fast 永远 ok=False）」。

### P1-6 三档同模型 + 超时语义错 + 流式零重试

- `DEEPSEEK_MODELS` 三档全是 `deepseek-flash`（`lithos.py:50`，注释自认「测试环境无法复现速度分档」）→ 分档只改超时 60/30/15s；
- `stream_complete` **硬编码 thinking disabled**（`lithos.py:434-436`），零重试；
- `cfg.timeout` 是**整条流的读超时**，不是首包超时 → 长计划会被中途掐断；
- 默认 `--urgency 0.5`（`tui.py:1065`）→ 落 `fast`=30s，与「慢系统允许慢（1–5s）」矛盾。

### P1-7 便签（短期记忆）不可达

`profiles.write_note/read_notes/clear_notes` 实现完整（4000 上限、2000 分块、原子写、超限抛 `ProfileError` 绝不静默截断），但**全仓只有 `agent/tests/test_profiles.py` 调用**；需求 FR-5 要求的工具从未注册，便签也不进提示词 → 短期记忆实际只有 30 条消息窗口。

### P1-8 Graphiti 生产不可用 + 检索结果 kind 被写死

- 默认 `local-json`（`memory.py:504`），profile 无 `memory` 段，主环境**未安装 graphiti_core/fastembed**（只有 `.research\graphiti\venv*` 里有），只有 `_diag/m2_graphiti_live.py:25` 显式用过；
- `GraphitiMemory.search` 返回一律 `kind="story", source="graphiti"`（`memory.py:456`）→ 调用方一旦按 kind 过滤，键位/规则全丢。

---

## 4. P2 缺陷（工程债，会误导后续判断）

| # | 缺陷 | 证据 | 为什么危险 |
| --- | --- | --- | --- |
| P2-1 | `webrtc_fast.py` 是死代码（仅 `_diag/t_webrtc_aiortc.py:30`），且 `ask()` 在 `response.text.done` 就返回可能截断；`LatestFrameVideoTrack` 无新帧时**重发旧帧、fps=5**；强制 `server_vad` 且喂纯静音轨（:31-56），与 omni 路径 manual 语义不一致 | `webrtc_fast.py:279-280, 98-116, 127, 316-320` | 未来做「实时视频流」时会被它误导成「我们已经支持 5fps」 |
| P2-2 | `pump=False` + `fps=2.0` 无效 | `agent.py:1100`；`fast_stream.py:101-114` | 以为有连续流，实际每决策 1 帧 |
| P2-3 | 抓帧降级信息传不到决策层 | `adapter.py:435-443` 有 fallback_chain/degraded，`executor.py:143-145` 塞进 meta 后**无人读取** | 掉到 screen 截屏（要抢前台）后画面延迟变大，决策层毫不知情 |
| P2-4 | 无帧时间码 | `captured_at` 写入即丢；`t_ms` 是本地相对时基（`executor.py:178,272,293`） | 解说与帧无法对齐，事后无法复盘「这句话说的是哪一帧」 |
| P2-5 | 无 token 预算 | 规则段 1400 字符预算，键位段**无上限**；`bundle.chars` 只统计 | 多游戏 profile 长大后提示词爆掉 |
| P2-6 | 零注入防护 | `memory_wire.format_recall` 把 episode.text 原样拼进 now_line，仅 600 字符截断、**无转义无引号包裹**（`memory_wire.py:91-112`）；人设里也没有「画面文本视为数据」的约束 | 游戏内文本/幻觉记忆可反向污染提示词并自我强化 |
| P2-7 | 无 TTL / 衰减 / 遗忘 / 删除 | `memory.py` 全文只有 count/close；`conflicts()` 只读不裁决 | 记忆只增不减，错误条目永久污染检索 |
| P2-8 | 检索是纯关键词打分 | `score = 2.0*hit_ratio + 1.5*confidence + 1.0/(1+age_days)`（`memory.py:224-244`），无 embedding | 「怎么打这个 Boss」搜不到「攻略这个怪的手法」 |
| P2-9 | `end_test` 提前结束语义失效 | `agent.py:1734-1763`：`finished_summary` 初始化为 None 后从未赋值，两处 `if finished_summary is not None` 永不执行 | 自动化测试无法正常收尾 |
| P2-10 | 「学键位」两条路径后果不一致 | `agent.py:1612-1622`（不写记忆/不写模板/不 rebuild）vs `learning.py:122-210`（全做） | 同一操作两种结果，排查困难 |

---

## 5. 失效场景演练（把缺陷连起来看）

**场景 A：Boss 战第一次躲闪**

1. 慢 LLM 决策：「先靠近 Boss」（1 次工具调用，~1–3s）；
2. `lumine` 起会话（WS 握手 0.21s）+ WGC 首建 0.15s；
3. 抓帧 → Omni push → motor.decide（0.34–0.61s，偶发 502 再来一次）→ 198ms 盲按；
4. Boss 抬手发生在这 1s 里的任意时刻，**系统没看见**——动作窗口内零观测（P0-3）；
5. 嘴线程 1Hz 描述「血在掉」，但解说**无权改动作**（P0-2），只能等裁判喊 `[DONE]`；
6. 慢 LLM 拿到 commentary + 一张结束图，6 步预算已用掉 1 步；
7. 一次 motor 502 若连着重试失败 → `motor_error` 立即终止整段（P1-4）。

**场景 B：剧情对话**

1. 画面停在对话选项上，没有「变化」；但硬规则要求「每轮至少推进一步」（P0-5）；
2. 系统按了确认键，**跳过了还没读的台词**；
3. 观众看到的：一个「不听人说话、急着点下一句」的玩家。

**场景 C：同一个地方卡住**

1. 走了 5 分钟没进展，但 gamescaling 没有 history（P1-1），每次决策都是「第一次看到这个画面」；
2. 没有「我刚才试过这条路」的记忆（P0-4），也不写便签（P1-7）；
3. 于是重复同一动作直到预算耗尽，观感是「卡墙抽搐」。

**场景 D：学到一条规则之后**

1. `learn_key` 走 `profiles.apply_learned_key`，**不写记忆**（P0-4）；
2. 调 `restart_fast`：清三个不存在的缓存（P1-5）、比对指纹、探活；
3. 因为 `prompt_builder.py:395` 的「磁盘落后一档」bug，`ok=False`；
4. 下一轮 `lumine` 重建 `system_prompt` 时**碰巧**吃到了新键位（因为每次都重建），但记忆库里没有这条知识 → 重启进程即丢。

---

## 6. 缺陷 → 玩家可感知症状（给验收用）

| 症状（人话） | 对应缺陷 |
| --- | --- |
| 反应慢半拍、躲不开 | P0-3 |
| 嘴里说的和手上做的不一致 | P0-2 |
| 剧情/对话被跳过 | P0-5 |
| 同一个位置反复抽搐 | P1-1、P0-4、P1-7 |
| 走一半突然停手 / 到了还在按 | P1-2 |
| 让它停停不下来 | P1-3 |
| 一次网络抖动就整段失败 | P1-4、P1-6 |
| 玩十小时还是新手 | P0-4、P1-8 |
| 解说偶尔串台/错位 | P0-1 |
| 学会的东西重启就没了 | P0-4、P1-5 |
