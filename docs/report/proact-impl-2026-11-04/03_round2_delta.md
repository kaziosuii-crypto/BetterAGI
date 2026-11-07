# 03 · 第二轮增量（2026-11-04 晚）

第一轮报告 §4 列了 7 项「还不能开播」的缺口。这一轮把其中 5 项做掉了（2/3/5/6/7），
把 1（真画面+手模型）往前推了一大步——**真的连上 5090 上的 GameScaling-9B-S3 测了延迟**，
结果推翻了原来那条最关键的假设：**200ms 不是墙钟预算**。

另外补了第一轮没列但一定会咬人的三处：手模型健康探针、预检从 4 项补到 8 项、节奏换算模块。

| 序号 | 项 | 第一轮状态 | 现在 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | 手模型真机延迟 | 未测 | **实测（10/10 parsed）** | `_diag/gs_5090_latency.py`，§1 |
| 2 | 冻结贴图「大肥鱼CPU烧了」 | 只有常量 | 真能渲染 JPEG | `agent/freeze_overlay.py` + 成图 |
| 3 | TUI 慢系统 2s 截止 | 直调 lithos | `stream_complete(deadline_s=2.0)` 可截断 | `agent/lithos.py:426-487` |
| 4 | `intent.py` owner | 未改 | **仍未改**（本轮没碰） | — |
| 5 | 手嘴 commentary 通道 | 只有 stop/done | `IntentContext` 双向记账 | `agent/intent_context.py` |
| 6 | `play_steps` plan/actual 时序 | 0 命中 | 每步 plan_t/actual_t/lag_ms/吞键 | `agent/adapter.py:693-792` |
| 7 | Omni 大帧 | >256KB 抛错 | 4 档降质重编 | `agent/omni.py:243-248` |
| 8 | 手模型健康探针 | 只探 `/health` | `/v1/models` + 就绪双探 | `agent/motor.py:196` |
| 9 | 预检项 | 4 项 | 8 项 | `agent/preflight.py` + `streamer.py:156` |
| 10 | 节奏换算 | 无 | `agent/pacing.py` | 占空比/慢动作倍数可算 |

---

## 1 真机手模型延迟实测（本轮最重要的结论）

### 怎么测的

- 服务：5090 云机，vLLM，`GameScaling-9B-S3`（`/v1/models` 返回 `id=gamescaling`、`root=/hy-tmp/gs/model/GameScaling-9B-S3`、`max_model_len=32768`，检查时 VRAM 26/32GB）。
- 链路：`E:/BetterAGI/_diag/gs_5090_probe.py`（paramiko + 本地端口转发 18041）→ 健康与一次冒烟；`E:/BetterAGI/_diag/gs_5090_latency.py`（端口 18042）→ 5 次/档的延迟采样。
- 请求：严格照 `Docs/GameScaling-9B-S3_API使用说明.md`——`system`（训练骨架，含 **Output Format** 段）→ `user`（文本逐字 `current game screen` + 官方示例帧）→ `assistant` 预填 `<think>`；`temperature 1.0`、`max_tokens 2048`、`skip_special_tokens false`、`stop ['<|action_end|>']`、`continue_final_message true`、`add_generation_prompt false`。
- 判定：按文档四步解析，`finish_reason == 'stop'` 且含 `<|action_end|>` 才算 parsed。

### 数字（毫秒，墙钟）

| 档位 | p50 | p95 | min | max | parsed |
| --- | --- | --- | --- | --- | --- |
| 开思考（`<think>` 预填，模型真的 reasoning） | **1155** | **1484** | 708 | 1484 | 5/5 |
| 关思考（`</think>` 预填为空） | **696** | **735** | 673 | 735 | 5/5 |

一次真实输出（开思考档）：

```
<|action_start|>71 -4 0 ; A LB S ; S ; S ; LB S ; LB S ; LB S<|action_end|>
```

即：鼠标 X=71‰ Y=-4‰、滚轮 0，六组键位依次是 `A LB S` / `S` / `S` / `LB S` / `LB S` / `LB S`，「A」是长按，「LB S」是组合键。

### 这条结论推翻了什么

**200ms 是「游戏时间」，不是墙钟预算。** 一次决策输出覆盖 6 × 33ms = 198ms 的游戏输入，但拿到这次输出要 0.7–1.5s。

改造前 `executor.run(deadline_ms=200)` + `orchestrator.execute(call.setdefault('deadline_ms', 200))` 的字面含义是「每一步必须在 200ms 内返回」——对着真服务，这意味着**每一步都 MotorTimeout**，然后把每一轮都判成 `motor_timeout → failed`，直播一条动作都放不出去。

实测占空比：

| 档位 | 墙钟 | 真正按键 | 空闲 | 占空比 | 等效慢动作 |
| --- | --- | --- | --- | --- | --- |
| 关思考 | 740ms | 198ms | 542ms | 27% | 3.7× |
| 开思考 | 1490ms | 198ms | 1292ms | 13% | 7.5× |

### 现在已经改成了什么

- 新增 `agent/pacing.py`：`GAME_CHUNK_MS=200`（窗口）/ `KEYED_MS=198`（真按键）/ `MEASURED_THINKING_P95_MS=1490` / `MEASURED_NO_THINKING_P95_MS=740`；`step_budget_ms(thinking)` 默认给实测 p95，`BETTERAGI_MOTOR_STEP_MS` 可覆盖；`duty_cycle()` / `pacing()` / `describe()` 把占空比与慢动作倍数额算出来。
- `Orchestrator(..., motor_budget_ms=None)`：`execute()` 不再硬写 200，未显式给值就走 `pacing.step_budget_ms()`。
- `run_stream.run()` 的返回体带上 `motor_budget_ms` 与 `pacing`（两档的墙钟/空闲/占空比/慢动作倍数 + 一句人话），让「这一场跑成几倍慢动作」变成可查的数字，而不是感觉。

### 还没解决的三选一（要人拍板，不是代码问题）

1. **接受慢动作**：3.7×–7.5× 慢放，观众看得出来，但行为最连贯（推荐先用这条跑通）。
2. **降决策频率**：不追每 200ms 决策，改成每 1.5s 决策、一次输出更长的动作串（要模型侧配合，训练分布是 6 组）。
3. **并行预取动作队列**：一次请求在播的同时请求下一次（实测 VRAM 26/32GB，并发可行但没压测；代价是动作要能「晚一步才失效」）。

本轮代码只做了「把账算清楚 + 让预算可配」，没有替用户选其中一条。

### 另一个实测坑（记下来免得再踩）

合成帧 + 自造 system prompt → 模型只回散文（`<think></think> probe`、甚至回越南语），`motor` 直接 `MotorParseError`。根因是提示词缺训练骨架的 **Output Format** 段，不是模型坏了。所以 `motor_wire` 拼的三槽位必须保留骨架原文，只替换 `{GAME_NAME}` / Game Rules / Common Keys 三段。

---

## 2 逐项细节

### 2.1 冻结贴图（`agent/freeze_overlay.py`，新，只依赖 Pillow）

- `DEFAULT_WARN_TEXT='大肥鱼CPU烧了'`（:21）；`find_cjk_font()` 按 `msyh.ttc → msyhbd.ttc → simhei.ttf` 找，全缺显式抛 `FreezeOverlayError`（:41，绝不画空图）。
- `render_overlay(jpeg, text=..., opacity=180, font_size=None)`（:89-140）：中央半透明黑条 + 白字描边 → JPEG q85，**尺寸不变**；字号缺省取画面高 12%，过宽自动收字号。
- `FreezePainter(text, tap=None)`（:143-188）：绘制失败只记 `errors/last_error` 并原样返回入参字节（贴图不能拖垮直播）；`stats()` 只返回 `{painted, errors, last_error}`。
- 编排层：`orchestrator.set_freeze_painter()`（:139-148）+ `_warn_freeze_overlay()`（:150-161），`plan()` 冻结窗口结束（:212/:224）实测超过 `FREEZE_WARN_MS=900` 就往 `orchestrator.warnings` 塞「本段冻结 1500ms（超过 900ms），请上层贴图：大肥鱼CPU烧了」。
- 装配：`run_stream.assemble(..., freeze_text='')`（:43-47，:114-121，:153），非空才建 painter；返回 dict 的 `freeze` 键。
- 实测 1280×736：`23073B → 40330B`，尺寸不变，`tap` 调用 1 次，`{'painted':1,'errors':0,'last_error':''}`；成图 `C:/Users/Tsuba/AppData/Local/Temp/freeze_demo.jpg`（肉眼确认白字压黑条居中）。
- 剩：渲染/推流层按 `freeze` 事件逐帧调 `parts['freeze'].paint(jpeg)` 的接线不在本轮（本轮做到「装配 + 告警」）。

### 2.2 慢系统截止（`agent/lithos.py`）

- `stream_complete(..., deadline_s: Optional[float] = 2.0)`（:426）：每读到一行算累计耗时，超预算发 `on_delta('abort', 'streaming deadline 2.0s exceeded after 2.14s')` 并 `break`，返回已收到片段 + `LLMReply.aborted=True`（:256-257）。它是**回调式**（`on_delta(kind, text)`，kind ∈ `reasoning|text`），所以中止沿用同一形状，没有新造事件类型。
- `choose_tier(..., live=False)`（:222-223）：`live=True` 时在 forced 之前直接返回 `'fast'`（直播链路只走快档模型）。
- 证据：`tests/test_lithos_deadline.py` 10 例（假 httpx + 假 monotonic）。
- 剩：首行之前彻底卡死仍只能靠 httpx 自己的 timeout（读不到行就没有「行循环」可截）；`thinking` 参数化没做；TUI 侧还没换成 `SlowPlanner`。

### 2.3 手嘴通道（`agent/intent_context.py` 新 + `executor.py` 注入）

- `IntentContext`（178 行，单锁纯内存、对外返回拷贝、空输入不抛）：`record_action` / `last_action` / `record_commentary` / `commentary(limit=8)` / `snapshot()`；`describe_action()` 产出一句中文「上一动作：第 3 步按键 W,A，耗时 41ms」。
- `executor.py:132` 新增 kw-only `context=None`（None 时行为逐字不变）；`:228-233` 嘴的 instructions 末尾追加「上一动作：…」；`:260-265` 嘴说的话写回 `IntentContext`；`:428-443` 解析失败但带部分 `steps` 也记账（`note='解析失败，动作没有执行'`）；`:470-478` 成功步记账（含吞键）；`:524-526` 只在注入时给 result 加 `intent_context` 快照。
- 证据：`tests/test_intent_context.py` 7 + `tests/test_executor_context.py` 6（含「不注入 context 时 instructions 与旧版逐字一致」「result 键集合与改造前一致」两条不回归断言）。
- 剩三条（要 lead/用户决策）：① 嘴的结论还没回灌给手（反向 mailbox）；② 「第 k 步那帧」没做——嘴仍读最新灌的帧，`IntentContext` 不存 `frame_ref`；③ 生产接线没做，`agent.py` / `run_stream.py` 仍 `context=None`（行为不变，只是通道还没通）。

### 2.4 按键时序（`agent/adapter.py` + `gamescaling_request.py`）

- `_clock_ms(clock=None)`（:101）：None → `time.monotonic()`；`timeline.Clock` 这类有 `now_ms()` 的对象直接用；零参可调用按秒×1000。
- `play_steps(..., step_ms=33, clock=None)`（:693）：每步 `plan_t=round(i*step_ms)`、`actual_t=round(_clock_ms(clock)-t0)`（取**该步开始**时刻，与 `plan_t` 同口径）、`lag_ms=actual-plan`；抢前台后再读一次窗口——抢不到 `pressed=False/skipped_reason='no_window_focus'`，键全查不到 `'unknown_key:*'`，部分跳过 `'skipped_keys:*'`，空组是显式 no-op（`pressed=True`）。
- 顶层只**末尾追加** `step_results`，旧 8 个键的名称/语义/顺序不变；`history_turn(..., pressed=..., step=..., skipped_reason=...)`（:143-174）在 `pressed` 给定时行尾加 ` # step3 skipped: no_window_focus` / ` # step2 pressed: ok`，不给时输出与旧版逐字节一致。
- 证据：`tests/test_play_timing.py` 6 + `tests/test_history_status.py` 5（含 5 组输入的逐字节比对）。
- 剩：`step_results` 还没有消费方——执行器的时效校验（`lag_ms > 100` 判积压）与 `motor` 把 `pressed` 传进 history 属其它写域，接口已就绪。

### 2.5 Omni 大帧（`agent/omni.py`）

- `encode_jpeg_frame_ex(data, *, max_bytes=MAX_IMAGE_BYTES, allow_reencode=True) -> (b64, meta)`（:248）：魔数 `FFD8FF` 校验 → 未超限原样 → `allow_reencode=False` 走旧抛错 → 无 Pillow 抛「没有 Pillow，无法降质重编」→ 按 `REENCODE_QUALITIES=(85,70,55,40)` × `REENCODE_SCALES=(1.0,0.85,0.7,0.55)` 逐档 `encode(optimize=True)` 查体积 → 极限仍超限抛错（带原始字节与全部档位）。
- `meta = {reencoded, bytes, original_bytes, max_bytes, quality, scale, width, height, attempts[]}`；`encode_jpeg_frame` 变薄包装（:347）。
- 证据：`tests/test_omni_jpeg.py` 8 例，含 3840 宽 → 167424B、2.43MB → 203854B、以及 `OmniClient.send_frame` 真路径。

### 2.6 手模型健康探针（`agent/motor.py:196`）

- `health(timeout=3.0, *, probe_ready=True)`：先 `/v1/models` 拿 `available/status/body/model_ids/model_ok`；`probe_ready` 时再打 `/health` 得 `ready/ready_status`；**探不到记 `ready=None`（「没查」≠「没就绪」）**。
- `hint` 分三种：model 名不对 → 检查 `--served-model-name` 与 `BETTERAGI_GAMESCALING_MODEL`；连不上 → 起服务再验 `/health`；否则空。
- 证据：`tests/test_motor_health.py` 5 例。

### 2.7 预检补到 8 项（`agent/preflight.py` + `streamer.py:156`）

| 检查 | 失败/跳过口径 | 落点 |
| --- | --- | --- |
| 游戏窗口 | 无探针 → `ok=True + skipped=True`（**不许伪装成通过**）；探针返回 dict/tuple/bool 都收 | `check_window(probe)` |
| 快系统 | 缺凭证 → fail + hint 设置 `DASHSCOPE_API_KEY`；有凭证打码 `key[:4]…key[-2:]`，并明说「额度只能实连验证」 | `check_omni(key, model)` |
| 记忆 | `memory=None` → fail；`search(query, top_k=1, layers=['episodic'])`，`TypeError` 退回 `search(query, limit=1)`；0 条 → ok「还没有上一场记录」 | `check_memory(memory)` |
| 编码器 | `record.ffmpeg_path()` 失败 → fail + hint `BETTERAGI_FFMPEG` | `check_encoder()` |

加上原有的弹幕/便利贴/磁盘/手模型，共 8 项；证据 `tests/test_preflight_extra.py` 12 例 + `tests/test_run_stream.py` 断言六项齐全。

### 2.8 节奏换算（`agent/pacing.py`，新）

- 常量与函数见 §1；`tests/test_pacing.py` 5 例（含「6×33=198 ≠ 200」这条口径本身的断言、「env 给垃圾不生效」、「占空比与慢动作倍数」）。
- 为什么单独成模块：`chunk_ms`（游戏时间）与 `wall_ms`（墙钟）必须分别命名，否则下一个人还会写 `deadline_ms=200`。

---

## 3 回归

```
cd E:/BetterAGI/agent
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
511 passed in 14.61s
```

第一轮末 436 → 现在 511（+75：本轮 10 项新增 5 个测试文件 75 例，未删任何旧例）。

---

## 4 仍然「不能开播」的三件事（按顺序）

1. **真游戏窗口 + 真手模型服务在本进程里跑通一轮**：本轮的延迟是脚本直连 vLLM 测的，`adapter.grab` 真抓窗口、`play_steps` 真按键、`motor.decide` 真走 18041 这条**同进程链路**还没跑过；`BETTERAGI_MOTOR_LEGACY_FALLBACK` 必须确认是关的。
2. **冻结贴图的渲染接线**：`parts['freeze'].paint(jpeg)` 要在推流/渲染层按 `freeze` 事件调用，否则观众只看到画面停住。
3. **TUI 侧慢系统**：`tui.py` 仍直调 `lithos.stream_complete`（现在有 2s 截止可用，但没接 `SlowPlanner`、没有超时占位）。

## 5 复测命令（谁都能重跑）

```
# 1) 起隧道（把远端 8000 映射到本机 18041/18042，先起 probe 再跑 latency）
python E:/BetterAGI/_diag/gs_5090_probe.py
python E:/BetterAGI/_diag/gs_5090_latency.py
# 2) 只看节奏账（不连任何服务）
python E:/BetterAGI/_diag/pacing_smoke.py
```