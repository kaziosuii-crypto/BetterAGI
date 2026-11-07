# 02 · 原始证据（可复核）

全部命令在本机 `E:/BetterAGI` 执行，PowerShell 代码页会把中文显示成乱码，所以下面贴的是把输出以 UTF-8 重写后的文本 / 或用 `PYTHONIOENCODING=utf-8` 采集的结果。

## E1 全量测试

```
PS> cd E:/BetterAGI/agent
PS> uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
........................................................................ [ 49%]
........................................................................ [ 66%]
........................................................................ [ 82%]
........................................................................ [ 99%]
....                                                                     [100%]
436 passed in 12.53s
```

改造前同一条命令：`203 passed in 3.50s`（17 个测试文件）→ 现在 42 个测试文件。

## E2 真实慢系统彩排（`_diag/rehearse_round.py`）

真 `SlowPlanner`（DeepSeek flash，`budget_ms=6000`）+ 真 `Orchestrator` / `Streamer` / `Timeline` / `SessionIndex` / `SegmentWriter`（真 ffmpeg）+ PIL 生成的 320×180 真 JPEG；手模型与解说员是假件。

```json
{
  "ok": true,
  "stage": "handoff",
  "result": "reached",
  "ended": "goal_reached",
  "goal": "先摸清当前画面状态：确认角色所处位置、周围可交互对象与界面提示。若处于可自由行动状态，先做一次小范围移动或观察，建立对场景的基本认知。",
  "conclusion": "这一段从篝火往东走到村口，跟红衣NPC说完了话。",
  "elapsed_s": 2.28,
  "actions": 33,
  "memory_episodes": 1,
  "record": { "ok": true, "frames": 33,
    "segments": ["seg_000.mp4","seg_001.mp4","seg_002.mp4","seg_003.mp4","seg_004.mp4"] },
  "timeline_kinds": ["freeze","freeze","intent_start","game_action" ×32,"intent_end","handoff"],
  "warnings": [],
  "error": null
}
```

读法：

- `goal` 是真 DeepSeek flash 写的三段 markdown 的「这一轮做什么」段，说明慢系统 → 三段解析 → 执行器这条链是真通的。
- `conclusion` 来自假解说员的固定句，**不是真画面理解**，不能当「看懂游戏」的证据。
- 时间轴 `freeze ×2` 是规划期进/出冻结窗口，`intent_start/end` 成对，`game_action` 每个都带同一个 `intent_id`。

## E3 录播分片（`_diag/smoke_record.py`，真 ffmpeg 9.0.2）

30 帧 320×180、`fps=10`、`segment_ms=1000`：

```json
{
  "ok": true, "code": 0, "frames": 30, "headers": 2,
  "segments": [ {"file":"seg_000.mp4","start_s":0.0,"end_s":2.2},
                {"file":"seg_001.mp4","start_s":2.2,"end_s":3.2} ],
  "index": [ {"file":"seg_000.mp4","start_ms":0,"end_ms":2200,"frames":22,"frame_no":0},
             {"file":"seg_001.mp4","start_ms":2200,"end_ms":3200,"frames":10,"frame_no":22} ],
  "sizes": { "seg_000.mp4": 3112, "seg_001.mp4": 1831 }
}
```

注意 `frames` 是**按 ffmpeg 切点推算**（2.2s×10、1.0s×10），与写入的 30 帧差 2 帧——末段未收尾的正常误差；索引里以切点为准，剪辑对齐用 `session_index.json`。

## E4 真慢系统（`_diag/smoke_slow.py`）

```
describe: {"available": true, "base_url": "https://api.deepseek.com/v1",
           "models": {"base":"deepseek-flash","fast":"deepseek-flash","ultra":"deepseek-flash"}}
{"ms": 1733,
 "goal": "继续沿东边的土路往右走，遇到绿色史莱姆就正面迎上去接触它，不要试图绕路，看接触后会发生什么。",
 "success": "画面切换到战斗界面，或史莱姆消失、道路畅通可以继续向右走。",
 "avoid": "不要反复绕路卡在同一个位置；不要主动攻击或做多余操作，先观察接触后的反应。",
 "warnings": [], "needs_rewrite": false}
```

三次实测分别 1521 ms / 1733 ms —— 「慢系统 ≤2s」这件事在本机是**刚好卡线**。

## E5 真 profile dry-run（预检真的会红）

```
PS> cd E:/BetterAGI/agent
PS> uv run --with pillow --with httpx --with pyyaml python run_stream.py --dry-run --profile undertale
{
  "session_id": "run-20261104-190139",
  "root": "E:\BetterAGI\profiles\undertale\runs\run-20261104-190139",
  "prompt_source": "empty",
  "warnings": ["便利贴还没写（或两块都空）：这一轮用旧的全量提示词"],
  "preflight": {
    "ok": false,
    "checks": [
      {"name":"弹幕服务","check_id":"danmaku","ok":false,
       "detail":"弹幕服务没回应：ConnectTimeout: timed out","elapsed_ms":898},
      {"name":"便利贴","check_id":"sticky","ok":true,
       "detail":"key_binding 0 字 / game_rules 0 字"},
      {"name":"磁盘","check_id":"disk","ok":true,"detail":"剩 26654MB"},
      {"name":"手模型","check_id":"gamescaling","ok":false,
       "detail":"ConnectError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。",
       "hint":"起手模型服务（2B-S3 checkpoint 单卡），再验 /health","elapsed_ms":2022}
    ],
    "failures": ["弹幕服务","手模型"]
  }
}
```

两个细节值得记：

1. `check_id` 与清单标签分开（原来检查函数自带的内部名会把「弹幕服务」覆盖成 `danmaku`）。
2. `check_motor` 原来把 `{"available": false}` 判成通过——因为 `health()` 不返回 `ok` 字段；已修并加回归测试（`tests/test_preflight.py`）。

## E6 事故留证形状（`tests/test_integration_loop.py` 里断言的真实目录）

```
run/incidents/<ts>_<intent_id>/
  README.md            一句话结论
  parsed.json          手模型的原始输出 / 解析结果
  env.json             python/platform/BETTERAGI_* 环境
  proc.json            线程数 + 内存 MB（拿不到就 null，不编）
  timeline_slice.jsonl 事故前后 10s 的事件（与主时间轴同 t_ms）
  frames/              冻结帧；缺帧写 frames/MISSING.txt
```

保留策略 `prune(keep_count=20, max_bytes=2GB)`，`keep` 标记的不删。

## E7 时间轴形状（一次成功回合）

```json
{"v":1,"t_ms":0,"seq":0,"kind":"freeze","ms":0,"restored":false,"reason":"slow_think"}
{"v":1,"t_ms":0,"seq":1,"kind":"intent_start","intent_id":"i1","goal":"先摸清当前画面状态…"}
{"v":1,"t_ms":34,"seq":2,"kind":"game_action","intent_id":"i1","steps":[...],"decide_ms":1.2}
{"v":1,"t_ms":1204,"seq":N,"kind":"intent_end","intent_id":"i1","result":"reached"}
{"v":1,"t_ms":1204,"seq":N+1,"kind":"handoff","intent_id":"i1","result":"reached"}
{"v":1,"t_ms":2281,"seq":N+2,"kind":"freeze","ms":2281,"restored":true}
```

规则（`01_architecture.md §14` 七条不变量 + `02_schemas.md §6.2b`）：`t_ms` 相对本场单调、`intent_start/intent_end` 成对且 id 对齐、`game_action.intent_id` 必须是已开意图、`incident` 之后不许再有 `game_action`、可分片的 `start_ms` 与时间轴同一把尺。

## E8 回滚点

```
E:/BetterAGI/_diag/backup_agent_pre_impl_20261104-182829/   改造前 49 个文件（robocopy /E，排除 __pycache__ 与 .pytest_cache）
E:/BetterAGI/_diag/rehearse_round.py                        彩排脚本（可重跑）
E:/BetterAGI/_diag/smoke_record.py                          录播 smoke
E:/BetterAGI/_diag/smoke_slow.py                            慢系统 smoke
E:/BetterAGI/_diag/dryrun_undertale_utf8.txt                本文件 E5 的原始输出
```
## E9 真机手模型延迟（5090，2026-11-04）

服务：vLLM `GameScaling-9B-S3`，`/v1/models` 返回 `id=gamescaling`、`root=/hy-tmp/gs/model/GameScaling-9B-S3`、`max_model_len=32768`（VRAM 26/32GB）。
请求：训练骨架 system（含 **Output Format**）→ user 文本逐字 `current game screen` + 官方示例帧 → assistant 预填 `<think>`；`temperature 1.0 / max_tokens 2048 / skip_special_tokens false / stop ['<|action_end|>'] / continue_final_message true / add_generation_prompt false`。

```
python E:/BetterAGI/_diag/gs_5090_probe.py     # 起 paramiko 隧道 + 健康
python E:/BetterAGI/_diag/gs_5090_latency.py   # 5 次/档

thinking=on   5/5 parsed  p50 1155ms  p95 1484ms  min 708  max 1484
thinking=off  5/5 parsed  p50  696ms  p95  735ms  min 673  max  735

样例输出：
<|action_start|>71 -4 0 ; A LB S ; S ; S ; LB S ; LB S ; LB S<|action_end|>
```

推算：一次输出覆盖 6×33ms = 198ms 游戏输入 → 关思考占空比 27%（3.7× 慢动作）、开思考 13%（7.5×）。**200ms 是游戏时间，不是墙钟预算**；原 `deadline_ms=200` 会让真服务每一步 MotorTimeout。

## E10 冻结贴图真出图（`_diag` 之外的实测，脚本内联）

```
输入 1280x736 JPEG  23073 B
输出 1280x736 JPEG  40330 B（尺寸不变、q85、中央黑条 + 白字）
tap 调用 1 次   {'painted': 1, 'errors': 0, 'last_error': ''}
成图 C:/Users/Tsuba/AppData/Local/Temp/freeze_demo.jpg（肉眼确认「大肥鱼CPU烧了」）
```

## E11 节奏账（不连任何服务）

```
python E:/BetterAGI/_diag/pacing_smoke.py

no_thinking: chunk_ms 200 / keyed_ms 198 / wall_ms 740 / idle 542 / duty 0.268 / slowdown 3.7x
thinking:    chunk_ms 200 / keyed_ms 198 / wall_ms 1490 / idle 1292 / duty 0.133 / slowdown 7.45x
note: 200ms 游戏输入需要 740ms 墙钟（占空比 27%，等效 3.7 倍慢动作）
```

## E12 第二轮回归

```
cd E:/BetterAGI/agent
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
511 passed in 14.61s
```

新增测试文件（本轮 10 项）：`test_omni_jpeg.py` 8、`test_lithos_deadline.py` 10、`test_freeze_overlay.py` 10、`test_play_timing.py` 6、`test_history_status.py` 5、`test_intent_context.py` 7、`test_executor_context.py` 6、`test_motor_health.py` 5、`test_preflight_extra.py` 12、`test_pacing.py` 5。
## E13 第三轮回归（事件 / 冻结泵 / 意图归属 / TUI 收手）

```
cd E:/BetterAGI/agent
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
550 passed in 15.34s      # 56 个测试文件、47 个模块（第二轮末 511 / 52 / 45）

# 只跑这一轮碰到的 7 个文件：
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q \
  tests/test_event_sink.py tests/test_freeze_pump.py tests/test_intent_owner.py \
  tests/test_tui_deadline.py tests/test_streamer.py tests/test_run_stream.py tests/test_orchestrator.py
71 passed in 4.50s
```

新增测试文件：`test_event_sink.py` 8、`test_freeze_pump.py` 9、`test_intent_owner.py` 7、`test_tui_deadline.py` 3；`test_streamer.py` 19（含 5 例冻结窗口）、`test_run_stream.py` 9（含泵与 FrameTee）。

## E14 冻结泵在真回合里的行为（离线，`tests/test_streamer.py` 与 `tests/test_run_stream.py` 的实测断言）

```
plan() 被 pump.holding() 包住 → 阻塞窗口内至少喂 2 帧（fps=4、阻塞 150ms 起）
冻结 1000ms 且 painter 可用 → painted 增长；painter 抛错 → 原帧照发，errors+1
冻结 >900ms 但本轮 painted==0 → warnings 里出现「一帧都没贴图（画面会停在原帧）」
两轮之间不串账：第二轮 freeze.frames 只算第二轮新增
plan() 抛 SlowPlannerTimeout → 冻结窗口照样结算（finally），不会漏记
run_stream 挂 FrameTee：一次 adapter.grab 同时喂 SegmentWriter 与泵，泵确实收到 JPEGDATA
```

## E15 手模型历史与时序账（第四轮）

```
$ uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q \
    tests/test_executor_history.py tests/test_motor_record_status.py tests/test_history_status.py
25 passed in 0.22s
```

钉住的断言（不是「跑通」，是「说得出为什么」）：

- 每决策一条历史：`len(motor.records) == result['action_count']`，且 `frame_url` 是那一帧的 data URL；
- 被吞键：`step_results` 里 `pressed=False, skipped_reason='no_window_focus'` 时，`record()` 收到的 `pressed=False`、`step=2`、`skipped_reason` 原样带上；
- 旧签名回退：motor 只有 `record(frame_url, action_text)` 时仍写成，`history[i].ok == True`；
- 没有 `history_text`（拿不到动作串）时**一条都不写**，`ok=False`（不伪造）；
- 按键异常：`play_steps` 抛错 → 状态强制 `{'pressed': False, 'reason': 'play_failed'}`；
- 时序：`lag_ms` 47 与 106 都 > 33 → 两个决策共 4 个 late 步；没有 `step_results` → `timing == {}`（绝不报 0 延迟）。

## E16 事故留证探针加界（第四轮，真故障复盘）

```
$ uv run --with pytest ... -m pytest -q tests/test_incident.py
6 passed in 12.15s     # 修前：第 1 个用例卡住 60s+ 不退，CPU≈0（阻塞在系统调用）
```

现场：`IncidentBundle.env_snapshot()` 调 `platform.platform()`，在 Windows 上它会去跑外部命令拿版本串；那一阵系统进程枚举异常（同一时刻 `tasklist` / `cmd.exe` 也超时），这条调用能挂几分钟。

修法：`SNAPSHOT_TIMEOUT_S=2.0` / `TORCH_PROBE_TIMEOUT_S=5.0` + `_bounded(fn, default, timeout_s)`（守护线程 + join 超时，不抛不阻塞），`platform` / `machine` / `cpu_count` / `cwd` / `memory_mb` / torch 全部套上界；超时值写 `unknown` 并进 `slow_probes`。

新增两条断言：平台探针 `sleep(30)` 时，`env_snapshot()` 仍在 5s 内写完且 `slow_probes` 含 `platform`；torch 探针返回空时只标记 `slow_probes: ['torch']`，不抛。
