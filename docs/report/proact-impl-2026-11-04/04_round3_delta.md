# 04 · 第三轮增量（事件消费端 / 冻结泵 / 意图归属 / TUI 收手）

日期：2026-11-04。基线：第二轮末 511 passed（52 个测试文件）。本文件只记**这一轮真的动了什么**，
每条都给「原来怎样 / 现在怎样 / 证据 / 还剩什么」。

## 0 一句话

第二轮补的是「零件」，这一轮补的是**零件之间的线**：事件有人读了、冻结期有人喂帧了、
意图有明确归属了、慢系统超预算时界面会说了。全量 **550 passed in 14.70s**（56 个测试文件）。

## 1 `agent/event_sink.py`【新】—— mailbox 终于有读者

**原来怎样**：`mailbox.py` 落地后只有写端（`orchestrator.put('state.change'/'intent.end'/'incident')`、
`agent.py` 的 `_publish`），全仓零读者。事件堆到队列上限就被丢——
事故复盘时答不出「那一刻状态机跳了几次、工具调了几次」。

**现在怎样**：`EventSink(mailbox, root, types, capacity, filename)`：

- `drain()` 逐类抽干并追加写 `run/<session_id>/events.jsonl`，行形 `{cursor,type,ts_ms,id,trace,payload}`，cursor 从 1 单调；
- `since(cursor, limit)` 增量回放、`snapshot()` 给 `{cursor,kept,capacity,counts,dropped,path,errors}`；
- 只消费登记类型（默认 `state.change/intent.end/incident/tool.call/tool.result/danmaku.batch`），
  `speech.text` 留在队列给 TTS——**事件总线不是垃圾桶**；
- 写盘失败只记 `errors`，不把直播打挂。

**证据**：`tests/test_event_sink.py` 8 例；`tests/test_streamer.py` 里 4 例证明「round 真的抽干并落盘」
（`events.jsonl` 行数 == 事件数、incident 进 `warnings`、`run()` 汇总 `summary['events']` 与 `sink`）；
`run_stream.assemble()` 已挂 `build_sink(mailbox, root=sess_root)`。

**还剩什么**：`tool.call/tool.result` 目前只有 agent.py 的工具路径在写，新链路（`run_stream`）的工具调用还没有统一发事件。

## 2 `agent/freeze_pump.py`【新】+ 接线 —— 冻结期真的继续出帧

**原来怎样**：慢系统 `plan()` 期间主线程阻塞 2s（卡关 10s），没人抓帧。录播是 `image2pipe` + 固定 fps，
那段时间会被当成「没有时间流过」；而且「大肥鱼CPU烧了」只写进 `orchestrator.warnings`，画面上什么都没有。

**现在怎样**：

- `FreezePump(tap, painter, fps=4, warn_ms=900, refresh_ms=500)`：
  `hold_once(elapsed_ms)` 未过 `warn_ms` 喂原帧，过了就贴图；贴好的帧按 `refresh_ms` 复用（贴图 ~40ms，不是每帧都值得重画）；
- `holding()` 是上下文管理器：`with pump.holding(): <阻塞调用>`，后台线程按 fps 喂帧，退出停泵并结算；
- `FrameTee(*sinks)` 把一次抓帧分发给多个下游（录播 + 泵），单个下游异常只记 `errors`；
- 接线：`streamer.round()` 用 `pump.holding()` 包住 `orchestrator.plan()`，回执里带 `freeze`（时长 / 本轮帧数 / 贴图数 / 错误数，**都是增量不是累计**）；
  `run_stream.assemble()` 把 `adapter.frame_tap` 挂成 `FrameTee(recorder.write, pump.set_frame)`。

**证据**：`tests/test_freeze_pump.py` 9 例（含「150ms 阻塞内真喂了 ≥2 帧」「贴图失败回退原帧」「坏 sink 不拖累好 sink」）；
`tests/test_streamer.py` 新增 5 例（`test_plan_window_is_covered_by_freeze_pump` / 冻结 >900ms 没贴图要告警 / 两轮之间不串账 / plan 抛错也要结算冻结窗口 / 无泵时 `freeze=={}`）；
`tests/test_run_stream.py` 新增 2 例（泵真的收到 `JPEGDATA`、泵和录播共用一次抓帧）。

**还剩什么**：冻结帧目前只喂给录播；播出侧的推流合成（设计 01 §10）还没有把泵接进去——那条链路整体未开工。

## 3 `agent/intent.py` —— 意图归属与快慢互斥（03 §C.7 P1/P2）

**原来怎样**：`intent.py` 完全没动（`grep handoff_state|owner` 0 命中）。互斥只靠 `Orchestrator.ALLOWED`，
而 `IntentBus` 是旧的 TTL/挂起逻辑，语义含糊：谁拥有意图、交棒期谁能改主意，代码里没有答案。

**现在怎样**：

- `Intent` 增 `intent_id / owner(fast|slow) / handoff_state(idle|pending|running) / session_id`，越界抛 `ValueError`；
- `IntentBus.begin_handoff()/end_handoff()`；`accept()` 早分支：交棒中拒收新的**快**意图（`NO_OP` 永远放行——喊停是安全通道）；
- `IntentBus(clock_ms=...)` + `_now()`：TTL 用 `timeline.Clock.now_ms`，不再和 `time.time()` 混用；
- 归属规则写死在代码注释里：**慢系统只在规划期持有意图，执行期一律归快系统**。

**证据**：`tests/test_intent_owner.py` 7 例（含「TTL 500ms 没到就还在、过了必须回落 NO_OP」「history 记 handoff_begin/end」）。

**还剩什么**：新链路（`run_stream`）执行期的互斥仍由 `orchestrator.ALLOWED` 兜底；`IntentBus` 是第二道闸，
两条闸没有合并成一套（合并会碰编排层状态机，风险大于收益，暂不做）。

## 4 `agent/tui.py` —— 慢系统超预算要「说出来」，并标注 legacy

**原来怎样**：TUI 直调 `lithos.stream_complete`；`deadline_s` 默认 2.0 已经在生效，但中止事件 `on_delta('abort', …)`
被回调里 `if kind == 'reasoning'` 静默丢掉——模型被截断，界面上看不出来，复盘也查不到。

**现在怎样**：

- 抽出 `tui.slow_delta_handler(console, aborted)`：`reasoning` 照常滚动，`abort` 打一行「慢系统超出预算，已收手：…」并置 `aborted['hit']`；
- 循环里若 `reply.aborted` 或 `aborted['hit']`，落 `slow_aborted` 事件 + 提示「本轮回复是预算内已收到的部分（不是完整规划）」；
- 文件头明确写清 **TUI 是 legacy 入口**（不走 `slow_planner.SlowPlanner`），真正开播走 `run_stream.py`。

**证据**：`tests/test_tui_deadline.py` 3 例；`tests/test_tui.py` 14 例仍绿。

**还剩什么**：TUI 没有换 `SlowPlanner`（也不打算换：它的交互模型是「人看着聊天」，不是直播总装）；
`ut_textframe` 的 2s 画面占位仍未接。

## 5 数字与复跑

| 项 | 第二轮末 | 第三轮末 |
| --- | --- | --- |
| 全量测试 | 511 passed / 52 文件 | **550 passed / 56 文件** |
| agent/ 模块 | 45 | 47（+`event_sink.py`、`freeze_pump.py`） |
| 03 §C 组状态 | C.7 ⬜ | C.7 ✅、C.8 ✅、C.9 🟡（legacy 但会说话） |

复跑：

```bash
cd E:/BetterAGI/agent
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
```

只跑这一轮的：

```bash
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q \
  tests/test_event_sink.py tests/test_freeze_pump.py tests/test_intent_owner.py \
  tests/test_tui_deadline.py tests/test_streamer.py tests/test_run_stream.py
```

实跑（离线彩排，不连 5090、不开游戏窗口）：

```bash
uv run python _diag/rehearse_round.py      # 真慢系统 + 真编排/总装/录播，手与画面是假件
```