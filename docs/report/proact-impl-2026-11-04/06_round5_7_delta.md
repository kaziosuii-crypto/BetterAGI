# 06 · 第 5–7 轮增量：卡住检测、冻结泵、软预算与宽限、离线验收

- 日期：2026-11-04 晚
- 承接：`README.md`（主报告）、`03_round2_delta.md`、`04_round3_delta.md`、`05_round4_delta.md`
- 全量回归：**608 passed**（49 个模块 / 63 个测试文件）
- 一句话：这一轮修的都不是「加功能」，而是**真机一跑就暴露的四处断裂**——模型答得上却被客户端掐死、冻结泵在真链路上根本不贴图、慢系统思考期没有底片、卡关只能靠模型自述。

## 1 卡住检测（第 6 轮，`agent/stuck.py`）

**原来怎样**：设计 01 §7 的「卡关」只有一条触发路径——快系统自己说「我卡住了」。模型不自知时（对着墙连按 40 秒 W），慢系统永远等不到交棒，整场直播就卡在那面墙上。

**现在怎样**：新增独立检测器，两条判据（`window=12`、`min_change=1.5`、`min_repeats=4`）：

| 判据 | 阈值 | 说明 |
| --- | --- | --- |
| 画面长时间几乎不变 | 连续 12 帧 8x8 灰度签名平均绝对差 < 1.5 | 「对着墙站着」 |
| 同一套动作重复 + 画面基本静止 | 同一动作串重复 ≥4 次，且静止帧 ≥ window/3 | 「一直按同一套键但没进展」 |

- **解不了帧（半截 JPEG / 非 JPEG）只记 `unknown`，绝不判卡住**——宁可不判，不许误判把正常游玩打断。
- 接线：`executor.run()` 每步之后 `observe(最近一帧, 动作串)`；判卡住 → `ended='stuck'`、时间轴写一条 `stuck` 事件（带 `static_streak` / `repeat` / `why`）、`operator_stuck` 事件、收手；`orchestrator.RESULT_MAP['stuck']='blocked'` 且进 `BLOCKED_ENDINGS`（连续卡关才升级到慢系统开思考）；`streamer` 把原因写进见闻（「这一轮被判卡住：画面连续 12 帧几乎没变」）。
- 测试：`tests/test_stuck.py` 9 例 + `test_executor_stuck.py` 6 例 + `test_orchestrator.py` 1 例 + `test_streamer.py` 2 例。

## 2 冻结泵：首轮没有底片（第 7 轮）

**原来怎样**：`FreezePump` 要「最后一张真实画面」才能喂帧，而它只在 `adapter.grab()` 的旁路里被 `set_frame`。第一轮的慢系统思考发生在**第一次抓帧之前**，泵里没有底片 → `available()` 为假 → 那 1.8 秒录播里没有任何帧，剪辑时时间轴对不上。

**现在怎样**：`Streamer._prime_pump()` 在进 `plan()` 前检查：泵没帧就从 `orchestrator.executor.adapter.grab()` 抓一张真画面塞进去；没有采集口就 `warn('冻结泵没有底片，也没有采集口：第一次思考期画面会停住')`（说清楚，不假装正常）。

证据：彩排 v2 第一轮 `freeze` 从 `{}` 变成 `frames=15 / painted=4`，两条 freeze 事件 `ms=1858 / 1586`、`restored=true`。

## 3 冻结泵贴不上字的真缺陷（第 7 轮，必须单独说）

`freeze_pump._paint()` 只认 `painter.draw`，而真贴图件 `freeze_overlay.FreezePainter` 的方法叫 `paint`；实例又不可调用，于是静默原样透传：**`painted` 计数在涨、画面一个字都没贴上**。单测用的是假件 `Paint.draw`，所以一直是绿的。

修法：依次尝试 `paint` → `draw` → 可调用对象；新增真件回归 `test_real_freeze_painter_actually_changes_the_frame`。

修后真机成帧：1280x720 源帧 173,525B → 贴图帧 **147,308B**，平均像素差 **15.782**，落盘 `E:/BetterAGI/_diag/rehearse_v2_painted.jpg`（人眼可见半透明黑条 + 白字「大肥鱼CPU烧了」）。

## 4 「慢」与「坏」分开：软预算 + 宽限 + 超时清历史重试（第 7 轮）

**原来怎样**：`pacing.step_budget_ms()` 给的 740ms（旧隧道实测 p95）被 `motor.decide` 当硬线，`executor` 一遇 `motor_timeout` 就结束整轮。真端点第一次彩排：两轮都是 0 步。

**现在怎样**（三处，逐层降级）：

| 层 | 改动 | 落点 |
| --- | --- | --- |
| 手模型 | `hard_ms = deadline_ms + grace`，`DEFAULT_GRACE_MS=3000`（`BETTERAGI_MOTOR_GRACE_MS`）；超软预算只记 `late_ms` / `over_budget`，不判失败 | `agent/motor.py` |
| 执行器 | 第一次 `motor_timeout` 等价重试一次（`timeouts<1` → `continue`），连续两次才收手 | `agent/executor.py` |
| 重试策略 | 重试前 `motor.reset()` 清掉手历史，只带当前帧 —— 超时主因是 5 张历史帧 1.5MB 上传（3.7Mbps 下 ~3.2s），不是模型算不过来 | `agent/executor.py` |

为什么要给到 3 倍：真机尾延迟在这台暴露端点上见过 2282ms（硬线 2240 差 22ms 误杀）与 **3788ms**（硬线 3740 差 48ms 误杀）。「p95 当硬线」在公网链路上一定出事，慢只记账、坏才判死。

## 5 离线验收器（第 7 轮，`agent/verify.py`）

把 `04_acceptance.md` §二 的 A1–A9 不变量变成**只读运行目录的检查器**：`verify_run(root, session_id=None) -> {ok, checks, counts, warnings}` + `format_report()` + `python -m verify`。

对彩排 v2 运行目录的实跑结果：**4 通过 / 0 不通过 / 5 跳过**。

| 项 | 结果 |
| --- | --- |
| A2 每个动作都落在某条意图的生死区间内 | PASS：2 条 `game_action` 全在 2 个意图区间内 |
| A3 时间轴单调 | PASS：12 行，`t_ms` 不倒退、`seq` 严格递增 |
| A4 每条意图都有合规交棒报告 | PASS：2 份，conclusion 20 字、observations 134/163 字 |
| A5 分片与时间轴同源且实体文件齐全 | PASS：`seg_000.mp4` start_ms=0 有锚点 |
| A1 活跃 realtime 会话 ≤1 | SKIP：时间轴还没有会话事件打点（**不能说通过**） |
| A6/A7/A8/A9 | SKIP：这次运行没有便利贴、没有事故 |

> 诚实的跳过比假的通过重要：A1 目前无法验证，需要快系统在 `fast_session` 里补 `omni.session_open/close` 打点（已进待办）。

## 6 真机彩排 v2（这一轮的验收证据）

`_diag/rehearse_v2.py`：真手模型（暴露端点，关思考）+ 真慢系统（DeepSeek `slow_planner`，预算 6000ms）+ 真冻结泵/贴图 + 真录播（ffmpeg 分片）+ 真卡住检测（window=6），走 `Streamer.round()` 两轮。

| 项 | 值 |
| --- | --- |
| 两轮结果 | `reached` / `goal_reached` |
| 手模型解析 | 2/2，每步 6 组键位 |
| 决策墙钟 | 1141.8ms / 901.6ms（预算 740ms，`slow_steps=1` 记账不判死） |
| 冻结窗口 | 1858ms / 1586ms，两条 freeze 事件成对、`restored=true` |
| 冻结泵 | `frames=15`、`painted=4`、`errors=0` |
| 贴图成帧 | 173,525B → 147,308B，像素差 15.782 |
| 交棒报告 | 2 份合规 |
| 录播 | 1 分片，520,355B |
| 时间轴事件 | `freeze` / `game_action` / `handoff` / `intent_start` / `intent_end` |

## 7 现在还剩什么

| # | 项 | 为什么还没算完 |
| --- | --- | --- |
| 1 | 真游戏窗口 + 真按键同进程跑一轮 | 彩排的画面是官方示例帧平移出来的，`adapter.grab` 抓真窗口 / `play_steps` 真按键没跑过；抓帧耗时与吞键率仍未知 |
| 2 | 快系统（Omni realtime）真连 | 缺 DashScope key / workspace / 配额，从未真连过一次；A1 不变量因此无法验证 |
| 3 | 历史帧上传成本 | 每步重传 5 张历史帧 = 1.5MB，是当前超时的主因；改不改（降分辨率 / 只传 1-2 帧 / 服务器侧缓存）需要拍板，涉及「像不像人」的口径 |
| 4 | TUI 侧慢系统接 `SlowPlanner` | 正在由子代理接线（`agent/tui.py`），完成后 `run_stream` 与 TUI 才共用同一条 2s 预算 |

## 8 复跑

```powershell
cd E:/BetterAGI/agent
$env:PYTHONIOENCODING='utf-8'
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q

cd E:/BetterAGI/_diag
uv run --with pillow --with httpx --with pyyaml python rehearse_v2.py
cd E:/BetterAGI/agent
uv run --with pillow --with httpx --with pyyaml python -m verify E:/BetterAGI/_diag/rehearse_v2_run --session-id rehearse-v2
```