# ProAct 直播编排 · 实施报告

- 日期：2026-11-04
- 依据：`E:/BetterAGI/Docs/design/proact-streamer-2026-11-04/`（README / 00_decisions / 01_architecture / 02_schemas / 03_code_change_plan / 04_acceptance）
- 改造对象：`E:/BetterAGI/agent/`（47 个模块，新增 21 / 修改 15 / 未动 11；测试文件 17 → 58）
- 测试命令：`cd E:/BetterAGI/agent; uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q` → **568 passed in 42.90s**（第一轮末 436 / 第二轮末 511 / 第三轮末 550；改造前基线 203）
- 回滚点：`E:/BetterAGI/_diag/backup_agent_pre_impl_20261104-182829/`（改造前 49 个文件的完整副本；仓库根没有 `.git`，只能靠这个）

---

## 0 一句话结论

三条 P0 全部落地，并被「离线闭环 + 真实慢系统彩排」两种方式验证：**一个意图内能走完 看画面 → 按键 → 说话 → 交棒 → 慢系统规划 → 落账 → 下一轮**，录播分片与时间轴同源、每轮有交棒文件与记忆条目、手模型解析失败会熔断留证。

第二轮（本文 §2.6 起，细节见 `03_round2_delta.md`）把「还差什么」从 7 项压到 3 项，并真连 5090 上的 `GameScaling-9B-S3` 测出延迟——**顺带推翻了一条设计假设：200ms 是游戏时间，不是墙钟预算**。

但它**还不能直接开播**：真游戏窗口 + 真手模型服务在同一进程里跑通一轮还没做过；冻结贴图的「逐帧交给渲染层」没接线；TUI 侧慢系统仍是直调。详见 §4。

---

## 1 三条 P0 验收

### P0-1 交棒真工具（意图完成 → 慢系统接手，全链路有落盘）

**改造前的问题**（审计证据）：`agent/agent.py` 里 9 个慢系统工具**一个都没实现**（`:1624 dispatch` 只认 8 个旧工具）；执行器只认解说文本里的 `[DONE]` 标记、交棒理由无处落；交棒后没有任何「上一轮做到哪」的结构化记录。

**改法**：

| 环节 | 落点 | 做了什么 |
| --- | --- | --- |
| 报告的 schema | `agent/handoff.py`（新，202 行） | `HandoffReport` 校验：`conclusion` 8–60 字、`observations` 120–2000 字、`result ∈ {reached,failed,blocked}`，越界抛 `HandoffError` 并报实际字数；缺失小节只 warning 不编内容 |
| 落盘 | `agent/handoff.py` `write_report` | 原子写 `run/<sid>/handoffs/<intent_id>.json` + `session.append` |
| 工具面 | `agent/agent.py:367` `SLOW_TOOL_SPECS` + `:1963-2436` | 11 个工具的 JSON Schema + `request_handoff / report_blocked / dispatch_task / read_sticky / write_sticky / search_memory / write_memory / read_danmaku / get_last_episode / set_hook / set_plan` 的实现与 `dispatch` 路由；拼错仍 `raise ValueError` |
| 快系统侧触发 | `agent/fast_session.py` `_absorb` | Omni 的 function_calls 映射成 `handoff / blocked / reference` 三类事件；`request_handoff` 时把 `[DONE] <理由>` 补进文本（执行器与 realtime 只认这个标记）；新会话清标记 |
| 编排层合账 | `agent/orchestrator.py` `handoff()` | `HandoffReport` 落盘 + timeline `handoff` 事件 + mailbox `intent.end` + 写记忆（见 P0-3）；未写成功时把原因塞进 `warnings`，不静默 |
| 结论质量 | `agent/streamer.py` `build_conclusion` | 快系统自己说要交棒的那句话（8–60 字）优先当结论，其次执行器总结，最后才是自动兜底句 |

**证据**：`tests/test_handoff.py`、`tests/test_fast_session.py`（12）、`tests/test_memory_loop.py`（7）、`tests/test_streamer.py`（15）、`tests/test_integration_streamer.py`（3，断言两轮=两个 Omni 会话、`handoffs/` 两份合法 JSON、`session_index.json` 两行 `reached`）。

### P0-2 Omni 超时与会话化（跨意图串台）

**改造前的问题**（这是审计里最硬的一条）：`agent/omni.py:350 collect` 的循环体只有 `recv_json()`（`:359`）**没有 wait_for**；唯一的外层超时在 `agent/fast_system.py:133` 的 `future.result(timeout=60)`，超时后线程仍堵在读上、连接不关；`_drain_until_done(:293)` 与 `cancel_inflight(:317)` 预算用完只 `return`。后果是上一意图的解说/工具调用被下一意图读走——如果交棒是 function call 触发的，就是「上一意图的交棒在下一意图生效」。

**改法**：

- `agent/omni.py`：新增 `OmniTimeout(OmniError_)`（带 `waited_s / budget_s / event / phase`）；`OmniConfig.first_token_timeout_s=12.0`、`step_timeout_s=4.0`；`collect()` 每个包都用 `asyncio.wait_for` 包住；超时即 `_quarantine()` **判死连接**（`_inflight=False`、尽力 `response.cancel` + `close`、`broken=True`），迟到事件投递不到任何地方；新增 `reconnect()` / `transport_factory` / `ask(max_total_s=...)`。
- `agent/fast_system.py`：`_run` 超时先 `future.cancel()` 再 `_drop_client()`（关连接并置 `_client=None`），把 `RuntimeWarning: coroutine ... was never awaited` 也一并修掉（`close()` 现在先排 `_shutdown()` 再 `loop.stop`）。
- `agent/fast_session.py`（新，189 行）：`IntentFastSession` = 一个意图一个 Omni 会话；`ask()` 失败 → `close_intent()+start()`（`reconnects+=1`）重试一次 → 再失败 `degraded=True` 返回预置台词（撑场但不编画面内容）。

**证据**：`tests/test_omni_deadline.py`（8，含「旧连接的迟到事件不能被下一轮读到」的 `SENTINEL_OLD` 哨兵用例）、`tests/test_fast_session.py`（12）、`tests/test_fast_system_timeout.py`（4）。彩排里会话开合计数与 `session_opens` 均为 1/一轮。

### P0-3 记忆闭环 + 便利贴生效

**改造前的问题**：记忆**只写不读**（`search` 在编排路径上从未被调用）；便利贴有存储、`profiles.py:236/251` 有上限，但**零调用点**，手模型吃的还是 profile 里的一段死文本。

**改法**：

- 读：`agent/orchestrator.py` `plan()` 开头先 `self.recall(report_summary)`（`memory_wire.recall_for` + `format_recall`，预算 `RECALL_BUDGET_CHARS=800`），再进冻结窗口调慢系统——检索是 IO，**不占慢系统 2s 预算**。
- 写：`orchestrator.remember()` 在 `handoff()` 末尾落一条 `Episode`（`layer` 五层 + `atoms` 原子事实 + `meta{intent_id,result,game,save_id,session_id}`），`Text` 合并结论与见闻、截 2000 字。
- 便利贴：`agent/sticky.py`（新，429 行）两块固定配额（`key_binding` 1K + `game_rules` 1K，合计硬顶 2048）、整体重写不淘汰、`tmp → fsync → rename → .bak` 事务、74 token 词表校验；`agent/profiles.py` 的原子写搬过去复用。
- 真进 prompt：`agent/motor_wire.py`（新，98 行）`build_motor_system()` 读 sticky → `prompt_builder.assemble_motor_system` 拼三槽位（GAME_NAME / GAME_RULES / KEY_BINDINGS）→ 手模型；sticky 两块都空才回退旧提示词，回退原因进 `warnings`。`agent/agent.py:1490` 起的 operator 路径已接线，`lumine` 返回体带 `prompt_source / prompt_chars / sticky_path`。

**证据**：`tests/test_memory_layers.py`（17）、`tests/test_memory_loop.py`（7，断言召回文本真进了慢系统 messages）、`tests/test_sticky.py`、`tests/test_motor_wire.py`（8）、`tests/test_memory_wire.py`。彩排结果 `memory_episodes=1`。

---

## 2 真机证据（不是「测试绿了」就算）

### 2.1 全量回归

```
cd E:/BetterAGI/agent
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
568 passed in 42.90s
```

改造前 203 passed（17 个测试文件）→ 第一轮末 436（42 文件）→ 第二轮末 511（52 文件）→ 第三轮末 550（56 文件）→ 现在 568 passed（58 个测试文件）。

耗时从 14.7s 涨到 42.9s 不是退化：`tests/test_incident.py` 里两条新用例是**故意**去触发「平台 / torch 探针卡住」的，超时预算 0.2s + 5s 是它们在验证「卡住也能按时写完留证」。

### 2.2 真实慢系统彩排（手与画面是假件，其余全真）

脚本 `E:/BetterAGI/_diag/rehearse_round.py`：真 `SlowPlanner`（DeepSeek flash，`budget_ms=6000`）+ 真 `Orchestrator` / `Streamer` / `Timeline` / `SessionIndex` / `SegmentWriter`（真 ffmpeg）+ PIL 生成的 320×180 真 JPEG。

```
ok: true            stage: handoff      result: reached     ended: goal_reached
elapsed_s: 2.28     actions: 33         memory_episodes: 1
record: {ok: true, frames: 33, segments: [seg_000..seg_004]}
warnings: []        error: null
goal: 先摸清当前画面状态：确认角色所处位置、周围可交互对象与界面提示。若处于可自由行动状态，先做一次小范围移动或观察，建立对场景的基本认知。
timeline_kinds: freeze, freeze, intent_start, game_action ×32, intent_end, handoff
```

注意：**goal 是真模型写的**（三段 markdown 被正确解析）；`conclusion` 来自假解说员的固定句，不是真画面——这一条不能当「看懂游戏」的证据。

### 2.3 录播分片（真 ffmpeg，不是 mock）

`E:/BetterAGI/_diag/smoke_record.py`：30 帧 320×180、fps=10、`segment_ms=1000` → `ok:true, code:0, frames:30`，切出 `seg_000.mp4 0.0→2.2s` / `seg_001.mp4 2.2→3.2s`，`session_index.json` 回填 `start_ms 0 / frame_no 0` 与 `start_ms 2200 / frame_no 22`。

掉帧时不会错位：分片时间戳取 ffmpeg 的真实切点（`segments.csv`），不是自己数帧乘间隔。

### 2.4 真 profile dry-run（预检真的会红）

```
uv run python run_stream.py --dry-run --profile undertale
prompt_source: "empty"    warnings: ["便利贴两块都空，回退旧提示词"]
preflight: 弹幕 red(ConnectTimeout 18080) / 便利贴 green(0 字) / 磁盘 green(余 26658MB) / 手模型 red(18041 拒绝连接)
```

预检的「红」是设计要的：`check_motor` 原来把 `{available: false}` 判成通过（`health()` 不返回 `ok` 字段），已修并加回归测试。

### 2.5 真慢系统单次耗时

`_diag/smoke_slow.py`：一次 `plan()` 实测 **1521 ms**，三段解析正确、`needs_rewrite=false`。这是「慢系统 2s 预算」这件事目前唯一的实测数（见 §4）。

### 2.6 真机手模型延迟（本轮最重要的结论：200ms 是游戏时间）

5090 云机 + vLLM + `GameScaling-9B-S3`，训练骨架提示词 + 官方示例帧，**5 次/档，10/10 parsed**（脚本 `_diag/gs_5090_latency.py`，隧道 `_diag/gs_5090_probe.py`）：

| 档位 | p50 | p95 | min | max |
| --- | --- | --- | --- | --- |
| 开思考 | 1155ms | 1484ms | 708ms | 1484ms |
| 关思考 | 696ms | 735ms | 673ms | 735ms |

一次输出覆盖 6×33ms = **198ms 游戏输入**，所以：关思考墙钟 740ms → 占空比 27% → 等效 **3.7× 慢动作**；开思考 1490ms → 13% → **7.5×**。

**这条推翻了原实现**：`executor.run(deadline_ms=200)` 对着真服务是「每一步都 MotorTimeout → failed → 交棒」，一条动作都放不出去。现已改为 `agent/pacing.py` 供预算（默认取实测 p95，`BETTERAGI_MOTOR_STEP_MS` 可覆盖），`Orchestrator(motor_budget_ms=...)` 不再硬写 200，`run_stream.run()` 返回体带 `motor_budget_ms` + `pacing`（两档占空比/慢动作倍数）。设计侧同步出勘误：`Docs/design/proact-streamer-2026-11-04/07_errata_measured_latency.md`。

还剩一个要人拍板的三选一：接受 3.7×–7.5× 慢动作 / 降决策频率（一次出更长动作串）/ 并行预取动作队列。**代码只把账算清楚，不替用户选。**

### 2.7 冻结贴图真出图（「大肥鱼CPU烧了」）

`agent/freeze_overlay.py`（只依赖 Pillow）：1280×736 实测 `23073B → 40330B`，尺寸不变、JPEG q85、`{'painted':1,'errors':0}`；编排层在冻结窗口 >900ms 时把「请上层贴图」写进 `orchestrator.warnings`，`run_stream.assemble(freeze_text=...)` 负责挂载。

成图（肉眼确认白字压黑条居中）：

![冻结贴图实测](C:/Users/Tsuba/AppData/Local/Temp/freeze_demo.jpg)

---

## 3 现在怎么跑

```bash
# 1) 手模型服务（GameScaling 2B-S3，单卡）默认 http://127.0.0.1:18041
# 2) 弹幕服务（可选；不起就预检红一条，不挡开发）默认 http://127.0.0.1:18080
# 3) DashScope key（快系统）与 DeepSeek key（慢系统）
#    BETTERAGI_DANMAKU_URL / BETTERAGI_RUN_ROOT / BETTERAGI_RECORD / BETTERAGI_FFMPEG / BETTERAGI_STICKY_ROOT

cd E:/BetterAGI/agent
uv run --with pillow --with httpx --with pyyaml python run_stream.py --dry-run --profile undertale   # 只装配 + 预检
uv run --with pillow --with httpx --with pyyaml python run_stream.py --profile undertale --rounds 3 --record
```

入口 `agent/run_stream.py`（235 行）：`assemble()` 装全部组件 → `preflight()` → `Streamer.run(rounds)` → `signoff()`（写 episode 记忆、`next_hook.json`、`session_index.json`、下播话术）。

---

## 4 还差什么（按「能不能开播」排序）

第一轮列了 7 项，第二轮做掉 4 项（冻结贴图 / 慢系统截止 / 手嘴通道 / 按键时序 / Omni 大帧）、把第 1 项推进到「已实测但未同进程跑通」。现在只剩 3 项：

| # | 项 | 现状 | 落点 | 为什么现在不能算完 |
| --- | --- | --- | --- | --- |
| 1 | **真窗口 + 真手模型同进程跑一轮** | 延迟是脚本直连 vLLM 测的；`adapter.grab` 真抓窗口 / `play_steps` 真按键 / `motor.decide` 走 18041 这条同进程链路没跑过 | `agent/adapter.py` + 起 18041 | 抓帧耗时、按键吞键率、端到端一轮真实节奏全部未知；`BETTERAGI_MOTOR_LEGACY_FALLBACK` 必须确认是关的 |
| 2 | **冻结贴图的渲染接线** | `FreezePainter` 能出图、编排层会告警，但没有任何生产代码按 `freeze` 事件逐帧调 `paint()` | 推流/渲染层 + `streamer` | 观众看到的仍是静止画面，冻结没有视觉反馈；`freeze_text` 非空时运行期需要 Pillow |
| 3 | **TUI 侧慢系统** | `tui.py` 仍直调 `lithos.stream_complete`；2s 截止与 `choose_tier(live=True)` 已经在 `lithos` 里可用，但没换成 `SlowPlanner`、没有超时占位 | `agent/tui.py` | 只有 `run_stream.py` 这条链路受预算保护，TUI 手动跑仍可能卡满 |

第三轮把其中两项接上了：`intent.py` 的 owner/handoff_state 已引入并加互斥；反向 mailbox 已有消费者（`event_sink.EventSink` → `events.jsonl`）。**仍未接**：`adapter.play_steps` 的 `step_results` 没有消费方（执行器时效校验）、TUI 仍是 legacy 不走 `SlowPlanner`。细节见 `04_round3_delta.md`。

---

## 5 风险

- **节奏账（已量化，不再靠感觉）**：墙钟 p95 关思考 740ms / 开思考 1490ms，占空比 27% / 13%，等效 3.7× / 7.5× 慢动作。`run_stream.run()` 的返回体里带 `pacing`，开播前先看这一行再决定走哪条路（接受慢放 / 降频 / 并行预取）。
- **慢系统 2s 预算偏紧**：单次实测 1521ms，含网络抖动会破 2s；彩排故意放到 6s。`lithos.stream_complete(deadline_s=2.0)` 已能截断并标 `aborted=True`，但「超时 → 发呆台词 → 下一轮补规划」这条降级路径没在真机上演练。
- **提示词骨架不能省**：实测合成帧 + 自造 system prompt 会让模型只回散文（`MotorParseError`），因为缺训练骨架的 **Output Format** 段；`motor_wire` 拼三槽位时必须保留骨架原文。
- **没有版本库**：回滚只有 `_diag/backup_agent_pre_impl_20261104-182829/`；建议 `git init`（或每次改造前再备份）。
- **并行改造的遗留耦合**：45 个模块里 19 个新建、15 个被改，接口靠 `tests/test_integration_loop.py` 与 `tests/test_integration_streamer.py` 两条离线闭环兜住；集成测试只覆盖假件，真窗口仍是零。

---

## 6 交付物

| 文件 | 内容 |
| --- | --- |
| `README.md`（本文） | 结论、三条 P0 验收、真机证据（含 5090 延迟实测）、怎么跑、还差什么 |
| `01_matrix.md` | C.1–C.14 逐条状态 + 第二轮新增模块；每条带证据与落点 |
| `02_evidence.md` | 原始命令与输出摘录（可复核） |
| `03_round2_delta.md` | **第二轮增量 10 项**：每项「原来怎样 / 现在怎样 / 证据 / 还剩什么」+ 延迟实测全表 + 复测命令 |
| `04_round3_delta.md` | **第三轮增量 5 项**：事件消费端 / 冻结泵接线 / 意图归属与互斥 / TUI 预算收手 / 550 passed 证据 |
| `05_round4_delta.md` | **第四轮增量 3 项**：手模型历史真的写上（含被吞键注记）/ 按键时序真的入账并进见闻 / 事故留证探针加界（真故障复盘） |
| `CHECKSUMS.txt` | 五份文档的 SHA256 |

配套设计文档在 `E:/BetterAGI/Docs/design/proact-streamer-2026-11-04/`（本轮新增第 7 份 `07_errata_measured_latency.md` 并回改了 02/03/04 里过期的 200/300ms 口径）。
