# 01 · C.1–C.14 改造清单逐条状态

对照 `E:/BetterAGI/Docs/design/proact-streamer-2026-11-04/03_code_change_plan.md` §C。状态口径：

- ✅ **done**：代码已落、有测试或真实输出佐证。
- 🟡 **partial**：主路径可用，但和设计写法有出入（写明差在哪）。
- ⬜ **not done**：没动，写在「卡在哪」。

> 第二轮增量（10 项，含**真机手模型延迟实测**）见 `03_round2_delta.md`；第三轮（事件消费端 / 冻结泵 / 意图归属 / TUI 预算提示）见 `04_round3_delta.md`；下表已按第三轮结果更新。

## 总览

| 组 | 文件 | 状态 | 一句话 |
| --- | --- | --- | --- |
| C.1 | `agent/omni.py` | ✅ | 超时/判死/重连 + 大帧 4 档降质重编都上了（`response_id` 白名单改成「判死连接」） |
| C.2 | `agent/fast_session.py`【新】 | ✅ | 一意图一会话 + function_call 三事件 + 重连一次再降级 |
| C.3 | `agent/motor.py` | 🟡 | 严格解析抛错、reset、health 双探；deadline 默认改按实测 p95（`pacing`）；history 仍在 client 上 |
| C.4 | `agent/gamescaling_request.py` | ✅ | 三槽位内容由 `prompt_builder` 生成后进单 system；参数本来就已提 |
| C.5 | `agent/gamescaling_action.py` | 🟡 | `<parse_failed>` 不污染 history；lenient 未打标 |
| C.6 | `agent/executor.py` | 🟡 | `IntentContext` 双向记账已接（`context=`）+ 事件被 `EventSink` 真消费；「第 k 步那帧」未做 |
| C.7 | `agent/intent.py` | ✅ | owner(fast/slow)/intent_id/handoff_state/session_id + 交棒互斥 + `clock_ms` 统一时钟 |
| C.8 | `agent/lithos.py` | ✅ | `stream_complete(deadline_s=2.0)` 截断 + `aborted` 标记；thinking 提参；`choose_tier(live=True)` 固定 fast |
| C.9 | `agent/tui.py` | 🟡 | `/danmaku` `/stats`、超预算收手提示（`slow_delta_handler`）已做；TUI 仍是 legacy（不走 `SlowPlanner`），2s 画面占位与记忆 query 未做 |
| C.10 | `agent/agent.py` | ✅ | 11 个工具 + dispatch 路由 + 编排层抽出 |
| C.11 | `agent/memory.py` + `memory_wire.py` | ✅ | 五层/原子/预算召回全落；session 隔离只在 local-json 层 |
| C.12 | `agent/sticky.py`【新】+ `profiles.py` | ✅ | 1K+1K、硬顶 2048、事务写、词表校验 |
| C.13 | `agent/adapter.py` | ✅ | 抓帧 tap（录播用）+ `play_steps` 的 plan_t/actual_t/lag_ms/吞键全上 |
| C.14 | 新增模块 | ✅ | 设计点名的 5 个全在，另加 11 个 |

## C.1 `agent/omni.py`（P0）

| 设计条目 | 状态 | 落点 / 差在哪 |
| --- | --- | --- |
| `collect` 加 wait_for（单帧 4s、整轮 12s），超时抛 `OmniTimeout` | ✅ | `OmniConfig.first_token_timeout_s=12.0` / `step_timeout_s=4.0`；`tests/test_omni_deadline.py` |
| `ask` 拆 open/ask_once/close | 🟡 | 没拆三个方法；生命周期交给 `fast_session.IntentFastSession`（`start/close_intent`），`omni` 侧给 `reconnect()` |
| 只接受本次 `response_id` 的事件，其余 stray 丢弃 | 🟡 | 改成更狠的做法：超时即 `_quarantine()` 判死 + 关连接，所以旧连接的迟到包读不到（`SENTINEL_OLD` 用例）；没有 per-id 白名单 |
| `_drain_until_done` 预算耗尽标脏、下次重连 | ✅ | `broken / broken_reason` + `_ensure_usable()` |
| `cancel_inflight` 校验 `response.done` 真到 | 🟡 | 尽力 `response.cancel` + `close`，没做 done 校验 |
| `last_response_id / closed / close()` 供 fast_session 管生命周期 | 🟡 | 有 `broken` / `reconnect()` / `close()`；没有 `last_response_id` |
| JPEG >256KB 降质重编 | ✅ | `encode_jpeg_frame_ex`（`omni.py:243-248`）：4 档质量 × 4 档缩放，仍超限才抛错并带所有档位；`tests/test_omni_jpeg.py` 8 |

## C.2 `agent/fast_session.py`【新】（P0）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| 每意图 new transport + 首帧预热 + 结束 close | ✅ | `IntentFastSession.start()/close_intent()`；`tests/test_fast_session.py` 断言两轮=两会话 |
| 会话持 `intent_id`，function_calls 映射三事件 | 🟡 | 事件映射 ✅（`handoff/blocked/reference` + `[DONE]` 补标记）；**没有 `intent_id` 字段**，靠编排层的外部 id 对账 |
| 断线本意图内重连一次，仍失败上报 handoff | 🟡 | 重连一次 ✅；二次失败走「降级说预设台词」（`degraded=True`），不是上报 handoff |

## C.3 `agent/motor.py`（P0）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| `parsed=False/truncated` 改抛 `MotorParseError` | ✅ | `motor.py:53-81`；旧兜底藏在 `BETTERAGI_MOTOR_LEGACY_FALLBACK=1`；`tests/test_motor_strict.py` 15 |
| `reset` 意图开始调、history 上移一层 | 🟡 | `reset()` 幂等 + `reset_count`、执行器每意图调用 ✅；history 仍在 client 实例上 |
| 加 `deadline_ms`（200ms），超时 `MotorTimeout` | ✅ | `MOTOR_STEP_BUDGET_MS=200`；响应到达后再复核 elapsed（`motor.py:295-301`） |
| `health` 加 vLLM 就绪/尺寸探针 | ✅ | `health(timeout=3.0, probe_ready=True)`（`motor.py:196`）：`/v1/models` 的 id 比对 + `/health` 就绪；探不到记 `ready=None` 不冒充；`tests/test_motor_health.py` 5 |

## C.4 `agent/gamescaling_request.py`（P0/P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| 单 system 改三槽位分段 | ✅ | 内容由 `prompt_builder.assemble_motor_system` 拼（GAME_NAME / GAME_RULES / KEY_BINDINGS 三段），`motor_wire.build_motor_system` 注入；请求格式仍是单条 system 消息（官方协议就是这样） |
| history 每条动作附 pressed/skipped | ✅ | `motor.record(..., pressed=, step=, skipped_reason=)`（`motor.py:190`）→ `history_turn` 行尾注记；执行器每步调 `_record_history`（`executor.py:...`），失败退回两参版本；`tests/test_motor_record_status.py` 5 |
| temperature/max_tokens 提为参数 | ✅ | 本来就有（`gamescaling_request.py:125-126`） |

## C.5 `agent/gamescaling_action.py`（P0）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| 解析失败不写 NOOP，写 `<parse_failed>` | ✅ | `PARSE_FAILED_TEXT`（`gamescaling_action.py:43-49`）；`noop_action()` 标 `parsed=True`，不再冒充失败 |
| lenient 结果单标 `lenient=True` | 🟡 | `parse_action(..., lenient=True)` 存在，但结果对象上没有区分标记 |
| 补出的 action_start/end 记进 raw | ⬜ | 未做 |

## C.6 `agent/executor.py`（P0）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| decide 前后校验 ≤200ms 并入事故 | 🟡 | `decide_ms` 逐步记录、超预算计 `slow_steps`、`error_detail` 带 `deadline_ms`；事故目录由编排层在 FATAL 时统一生成，执行器自己不写 incident |
| 手/嘴 `IntentContext` mailbox | 🟡 | 手↔嘴共享 `IntentContext`（上一动作 + 最近解说）✅；`stop/done` 两个 Event 仍是停机信号（这是对的：停机不能走异步队列）；反向 mailbox 由 `event_sink.EventSink` 消费（`events.jsonl` + `sink.snapshot()`） |
| capture/motor error 接熔断链 | 🟡 | `motor_parse_error` → `aborted`（`FATAL_ENDINGS` 触发 `trip()`）；`capture_error` 只算 `failed`，不熔断 |
| `_summarize` 用 timeline 重放 | ⬜ | 仍再问一次 Omni（`executor.py:448`） |
| 按键时序账（lag/late/dropped）入结果与见闻 | ✅ | `_step_timing` / `_aggregate_step_status` / `_run_timing`；`run()` 返回 `late_steps` / `dropped_steps` / `timing` / `history`；`Streamer.build_observations` 把「按晚了一拍」「没真的生效」写进见闻（`tests/test_executor_history.py` 9 + `test_streamer.py` 2） |
| 每决策写回手模型历史 | ✅ | 改造后全仓只有旧 TUI 路径在 `record()`；现执行器每步写一条（帧 + 动作串 + 状态注记），`history_len` 才真的非空 |

## C.7 `agent/intent.py`（P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| `Intent` 加 `intent_id/handoff_state/owner(fast\|slow)/session_id` | ✅ | `intent.py:24-52`；`INTENT_OWNERS` / `HANDOFF_STATES` 白名单，越界抛 `ValueError` |
| `accept` 加快慢互斥（交棒期间拒绝新的快意图） | ✅ | `IntentBus.begin_handoff()/end_handoff()` + `accept()` 早分支；`NO_OP`（喊停）永远放行 |
| TTL 基准改 `timeline.now_ms` | ✅ | `IntentBus(clock_ms=...)` + `_now()`；`tests/test_intent_owner.py` 7（含「TTL 过了必须回落 NO_OP」） |

说明：新链路（`run_stream.py`）的**执行期**互斥仍由 `orchestrator.ALLOWED` 状态机兜底；`IntentBus` 是给旧入口与外部调用方用的第二道闸，不再作为门控核心。

## C.8 `agent/lithos.py`（P0/P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| `stream_complete` 加 `deadline_s=2.0` | ✅ | `lithos.py:415-438`：每读一行算累计耗时，超预算发 `on_delta('abort', …)` 并返回 `LLMReply.aborted=True`；`tests/test_lithos_deadline.py` 10 |
| 硬编码 thinking 提参 | ✅ | `LithosSlowClient.__call__(messages, thinking=...)` → `client.complete(..., thinking=...)`；`slow_planner.thinking_for(blocked_count, 3)` |
| 外层整体 deadline + 一次重试封顶 | 🟡 | 封顶由 `SlowPlanner` 做（`SlowPlannerTimeout`），lithos 内部仍无预算 |
| `choose_tier` 直播固定 fast | ✅ | `choose_tier(..., live=True)` 在 forced 之前直接返回 `fast`（`lithos.py:222-223`） |

## C.9 `agent/tui.py`（P0/P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| 弹幕不再直接进 history | ✅ | `tui.py:739`「默认只进缓冲」；`read_danmaku` 按需取 |
| `GET /danmaku?since=&limit=` + `GET /stats`，Bearer 鉴权 | ✅ | `tui.py:681-704`；`tests/test_danmaku_api.py` 3（含 401） |
| 慢系统调用换带超时 + 画面占位 | 🟡 | 超时已生效（`deadline_s=2.0`）且超预算会提示「已收手」+ 落 `slow_aborted` 事件（`tui.slow_delta_handler`，`tests/test_tui_deadline.py` 3）；画面占位（`ut_textframe`）仍未接——TUI 不当播出画面用 |
| 记忆注入 query 改上下文 + 弹幕摘要、预算固定 | ⬜ | 仍是旧的关键词拼接 |
| `Runtime.snapshot` 扩成时间轴/事故快照源 | ⬜ | 未做（新链路用 `incident.py` 自己拍） |

## C.10 `agent/agent.py`（P0/P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| 新增工具 schema（9+1） | ✅ | `agent.py:367 SLOW_TOOL_SPECS` 11 条；`:1856` 拼进 `tool_specs()` |
| `dispatch` 加分支、lumine 隔离 | ✅ | `:1963-2436` 11 个实现 + `:2436` 路由；拼错 `raise ValueError` |
| `agent.py run` 与 `tui.py run` 合一抽 Orchestrator | 🟡 | `orchestrator.py` 已抽出、`run_stream.py` 走新链路；TUI 老入口未切 |
| `tool_specs` 动态注入扩展到便签三槽位 | 🟡 | 手模型提示词走 `motor_wire`（operator 路径），不是通过 `tool_specs` 注入 |

## C.11 `agent/memory.py` + `memory_wire.py`（P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| `Episode` 加 `layer` + `atoms` | ✅ | `memory.py:48-129`：`MEMORY_LAYERS`、`LAYER_WEIGHTS`（procedural ×1.5）、`normalize_atom`、`is_spoiler`、`visible_atoms` |
| `search` 的 `group_ids` 改列表、结果带 layer | ✅ | `memory.py:470-585` 支持 `layers/top_k/include_spoiler/group_ids` |
| `add_episode` 同时落 episode + atoms、source=slow | ✅ | `memory.py:323-451`；相似 >0.92 只合并不新增（`salience+0.1`、`supports+1`） |
| `recall_for` query 改上下文文本、预算中等 | ✅ | `memory_wire.py:23 RECALL_BUDGET_CHARS=800`；`orchestrator.recall()` 注入慢系统 |
| session 隔离（工作层不落盘、情景层带 session_id） | 🟡 | local-json 后端只按游戏分区；Graphiti 多作用域支持了，但「工作层不落盘」没有强制 |

## C.12 `agent/sticky.py`【新】+ `profiles.py`（P0）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| 两块各 1024、合计硬顶 2048、整体重写不淘汰 | ✅ | `sticky.py`（429 行）；`tests/test_sticky.py` |
| 原子写 + 超限抛错搬自 `profiles.py` | ✅ | `tmp → fsync → rename → .bak`；`profiles.py:251-279` 保留 |
| `note_limit` 与 sticky 配额解耦 | 🟡 | 两套上限仍在（`profiles` 的 per-profile 4000 与 sticky 2048），没有互斥校验 |

## C.13 `agent/adapter.py`（P1）

| 设计条目 | 状态 | 落点 |
| --- | --- | --- |
| `grab` 加旁路 tap（录播与 Omni 共用一次抓帧） | ✅ | `adapter.py grab` 末尾 `frame_tap`；`run_stream.py` 挂 `FrameTee(recorder.write, pump.set_frame)`——一次抓帧同时喂录播与冻结泵（`_diag` 级证据：`tests/test_run_stream.py::test_pump_and_recorder_share_one_frame_tap`） |
| `play_steps` 回传 plan_t/actual_t/吞键 | ✅ | `_clock_ms`（`:101`）+ `play_steps(step_ms=33, clock=None)`（`:693`）+ 顶层 `step_results`；`tests/test_play_timing.py` 6、`test_history_status.py` 5 |
| `_wgc_grab` 暴露未裁剪原图 | ⬜ | 未做（录制用的是 Omni 同一路裁剪帧） |

## C.14 新增模块（P0/P1）

| 设计点名 | 实际文件 | 状态 |
| --- | --- | --- |
| `timeline.py` | `agent/timeline.py`（166 行） | ✅ `Clock` + `event()` + `payload` 口子 + `VIEWER_KINDS/CONTROL_KINDS` + `export_viewer()` |
| `recorder.py` | `agent/recorder.py`（205 行） | ✅ `SessionIndex` 索引与原子保存（**不自己取时钟**） |
| 录播分片 | `agent/record.py`（251 行） | ✅ 真 ffmpeg image2pipe + `segments.csv` 回填 sidecar |
| `danmaku.py` | `agent/danmaku_hub.py`（82 行）+ 旧 `danmaku_feed.py` | ✅ 环形 500 + `/danmaku` `/stats` 数据源 |
| `incident.py` | `agent/incident.py`（236 行） | ✅ 事故目录 + 帧 + 进程/环境快照 + `prune(20 个/2GB)` |
| `orchestrator.py` | `agent/orchestrator.py`（433 行） | ✅ 快慢互斥状态机 + 冻结窗口 + FATAL 熔断 |
| （补）冻结泵 | `agent/freeze_pump.py`（210 行） | ✅ 冻结窗口按 fps 喂最后一帧、>900ms 贴图、`FrameTee` 一分多；`tests/test_freeze_pump.py` 9 |
| （补）事件消费端 | `agent/event_sink.py`（150 行） | ✅ mailbox → `events.jsonl`，cursor/`since`/`snapshot`；`tests/test_event_sink.py` 8 |
| （未点名，实际必需） | `mailbox.py` 251、`preflight.py` 183、`slow_planner.py` 277、`streamer.py` 378、`run_stream.py` 235、`handoff.py` 202、`motor_wire.py` 98 | ✅ |
| `pacing.py`（第二轮） | `agent/pacing.py`（76 行） | ✅ 200ms 是游戏时间；墙钟预算按实测 p95 给 |
| `intent_context.py`（第二轮） | `agent/intent_context.py`（178 行） | ✅ 手→嘴、嘴→手 双向记账（生产未接线） |
| `freeze_overlay.py`（第二轮） | `agent/freeze_overlay.py`（188 行） | ✅ 「大肥鱼CPU烧了」真渲染 JPEG（渲染层未接线） |

---

## 变更文件清单（对照 `_diag/backup_agent_pre_impl_20261104-182829/` 逐个哈希比对）

**新建 16 个**（字节）：

`danmaku_feed.py` 4897、`danmaku_hub.py` 3584、`fast_session.py` 8657、`handoff.py` 9315、`incident.py` 9926、`mailbox.py` 9788、`motor_wire.py` 4402、`orchestrator.py` 20276、`preflight.py` 8558、`record.py` 9294、`recorder.py` 8011、`run_stream.py` 9836、`slow_planner.py` 11644、`sticky.py` 17711、`streamer.py` 17439、`timeline.py` 7312。

**修改 13 个**（当前字节）：

`adapter.py` 37195、`agent.py` 136333、`executor.py` 23675、`fast_stream.py` 8714、`fast_system.py` 14965、`gamescaling_action.py` 12322、`memory.py` 41336、`memory_wire.py` 8074、`motor.py` 21429、`omni.py` 31043、`profiles.py` 18126、`prompt_builder.py` 24325、`tui.py` 53632。

**未动 13 个**：`arbiter.py`、`gamescaling_request.py`、`gamescaling_vocab.py`、`graphiti_wiring.py`、`intent.py`、`learning.py`、`lithos.py`、`rule_templates.py`、`say.py`、`screen_crop.py`、`ut_textframe.py`、`webrtc_fast.py`、`wgc_capture.py`。

## 测试文件（42 个，新增 25 个）

新增：`test_danmaku_api.py`、`test_danmaku_feed.py`、`test_danmaku_hub.py`、`test_executor_chunk.py`、`test_fast_session.py`、`test_fast_system_timeout.py`、`test_handoff.py`、`test_incident.py`、`test_integration_loop.py`、`test_integration_streamer.py`、`test_mailbox.py`、`test_memory_layers.py`、`test_memory_loop.py`、`test_motor_strict.py`、`test_motor_wire.py`、`test_omni_deadline.py`、`test_orchestrator.py`、`test_preflight.py`、`test_record.py`、`test_recorder.py`、`test_run_stream.py`、`test_slow_planner.py`、`test_sticky.py`、`test_streamer.py`、`test_timeline.py`。
