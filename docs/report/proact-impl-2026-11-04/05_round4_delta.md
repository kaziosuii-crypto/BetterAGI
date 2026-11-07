# 第四轮增量：手模型历史真的写上、按键时序真的入账

- 日期：2026-11-04
- 本轮范围：`agent/executor.py`（写历史 + 时序账）、`agent/motor.py`（`record()` 收状态）、`agent/streamer.py`（见闻里报时序）、`agent/incident.py`（留证探针加界）、新增 3 个测试文件
- 测试：`cd E:/BetterAGI/agent; uv run ... python -m pytest -q` → **568 passed in 42.90s**（上轮末 550）

---

## 1 为什么先修这一条：`history` 一直是空的

`grep -rn "\.record(" agent/` 在改造后只剩 `agent/agent.py:1644`（旧 TUI/operator 路径）。也就是说：

- 新链路 `orchestrator → executor → motor` **从来没写过一条历史**；
- gamescaling 的输入是「当前帧 + 前 5 帧 + 当时动作」（官方 README §4），`history_len` 恒为 0；
- 后果不是「少一点上下文」，而是模型**永远不知道上一步的键有没有泵进去**：窗口被抢、词表外的键、按键异常，它下一轮照样照同一个打法再来一遍——与审计里「同一错误 152 次」是同一种病。

矩阵 `01_matrix.md` C.4 那一行当时标的是 ⬜「需要 C.13 的时序先有数据」。C.13 的 `step_results` 第三轮已经有了，消费方一直没接。

## 2 改了什么

### 2.1 每次决策写一条历史，带上「这一步到底按没按动」

`agent/executor.py`：

| 位置 | 改动 |
| --- | --- |
| `_step_timing(step_results, step_ms)` | 汇总单次 `play_steps` 的节拍账：`{steps, late_steps, max_lag_ms, mean_lag_ms, step_ms}`；**`lag_ms > step_ms` 才算 late**（晚一整拍），没有 `step_results` 返回 `{}`（绝不伪造 0 延迟） |
| `_aggregate_step_status(step_results)` | 把 6 条一步的按键状态压成一句 `{pressed, step, reason}`；`reason` 取第一个非空（`no_window_focus` / `skipped_keys:*` / `unknown_key:*`） |
| `_run_timing(actions, step_ms)` | 整轮汇总 `{decisions, max_lag_ms, mean_lag_ms, step_ms}` |
| `_record_history(frame_url, decision, status)` | 调 `motor.record(frame_url, decision['history_text'], pressed=…, step=…, skipped_reason=…)`；`TypeError` 时退回两参版本（老 motor 也能用）；任何异常吞成 `{'ok': False, 'reason': …}`，**不中断按键** |
| `run()` | 每步：算 `timing` → 累加 `late_steps` / `dropped_steps` → `entry['timing']` / `entry['step_status']` → 写历史；返回新增 `late_steps` / `dropped_steps` / `timing` / `history` 四个键 |

`agent/motor.py` 的 `record()` 原来只收两个位置参数——执行器传状态会 `TypeError` 退回，状态注记根本到不了模型。改成：

```python
def record(self, frame_url: str, action_text: str, *, pressed: Optional[bool] = None,
           step: Optional[int] = None, skipped_reason: str = "") -> None:
    turn = request_mod.history_turn(frame_url, action_text, pressed=pressed,
                                    step=step, skipped_reason=skipped_reason)
```

历史里长这样（不传 `pressed` 时与旧格式**逐字节一致**，旧格式是训练分布的一部分）：

```
<|action_start|>0 0 0 ; Z ; Z<|action_end|> # step4 skipped: no_window_focus
```

### 2.2 时序账进见闻总结（慢系统才看得见）

`agent/streamer.py::build_observations` 追加两条事实，**只在真有数据时写**：

- 「有 N 步按键晚了一拍以上（最晚 Xms，单拍 33ms）」
- 「有 M 步的按键没真的生效（no_window_focus）」

不写这两条的话，慢系统看到的永远只是「解说词」，它会以为手已经把这个打法执行到位了。

### 2.3 事故留证不再能把崩溃路径卡死

这是本轮踩出来的真故障，值得单独记：

现象：`pytest tests/test_incident.py` 在第 1 个用例上**卡住 60 秒以上不退**，进程 CPU 接近 0（不是死循环，是阻塞在系统调用里）；同一时刻 `tasklist`、`cmd.exe` 也超时。

根因：`IncidentBundle.env_snapshot()` 调 `platform.platform()` —— 在 Windows 上它会去跑外部命令（`cmd /c ver` 一类）拿版本串。系统进程枚举一有状况，这条调用就能挂几分钟。而 `env_snapshot` 恰恰是在**崩溃处理路径**上跑的：为了写一份环境快照把整个进程卡住，等于连事故现场一起丢。

改法（`agent/incident.py`）：

| 常量 / 函数 | 作用 |
| --- | --- |
| `SNAPSHOT_TIMEOUT_S = 2.0` / `TORCH_PROBE_TIMEOUT_S = 5.0` | 探针预算 |
| `_bounded(fn, default, timeout_s)` | 在守护线程里跑探针，超时返回默认值；**不抛异常、不阻塞调用方** |
| `_torch_info()` | torch / CUDA 探测独立成函数（导入 torch + 初始化 CUDA 也可能卡几秒） |
| `env_snapshot` | `platform` / `machine` / `cpu_count` / `cwd` / torch 全部走 `_bounded`；超时值写 `unknown` 并进 `slow_probes` 列表 |
| `proc_snapshot` | `memory_mb`（psapi）也走 `_bounded` |

结果：留证照常写完，只是 `env.json` 里 `platform` 是 `unknown`、`slow_probes: ["platform"]` —— 如实标注比卡死强。

## 3 证据

```
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
568 passed in 42.90s
```

新增测试文件（12 例）：

| 文件 | 例数 | 钉住什么 |
| --- | --- | --- |
| `agent/tests/test_executor_history.py` | 9 | 每决策一条历史 / 被吞键标 `pressed=False` / 全落地标 ok / 旧签名回退 / 无 `history_text` 不写 / 按键失败标 `play_failed` / lag 超一拍计 late / 无 `step_results` 不报时序 / 状态原因优先级 |
| `agent/tests/test_motor_record_status.py` | 5 | 不带状态时逐字节一致 / pressed ok / skipped+reason / 只留最近 N 轮 / 执行器 kwargs 真能命中 |
| `tests/test_incident.py` 扩 2 例 | 2 | 平台探针卡 30s 时留证仍在 5s 内写完且标 `slow_probes` / torch 探针失败只标记不致命 |
| `tests/test_streamer.py` 扩 2 例 | 2 | 见闻里出现晚点与吞键 / 没数据时一个字都不编 |

顺手修掉的形状守卫：`tests/test_executor_context.py` 的 `OLD_RESULT_KEYS` 是「结果形状不许变」的守卫。新键是**纯加法**（老键一个不少），所以把它改成 `OLD_RESULT_KEYS | ADDED_KEYS`，并显式列出 `{late_steps, dropped_steps, timing, history}` —— 以后谁再加键，这个测试会立刻红。

## 4 还剩什么

1. **历史里没有「第 k 步那帧」**：现在记的是决策时那一帧 + 动作串。要严格复现「当前帧 + 前 5 帧」的训练形态，得把每步的帧也留下来（会占内存，需要环形缓冲 + 上限）。
2. **时序账没有事故联动**：`late_steps` 超阈值（比如连续 3 个决策步都晚一拍）目前只进见闻，不触发熔断——按用户口径「只有解析失败算程序错误」，这属于观察项，先记账。
3. **真窗口仍是零**：`dropped_steps` 目前只在假件里被构造出来过（`no_window_focus` 分支有单测），真游戏窗口抢焦点的实测还没做。

## 5 复跑

```bash
cd E:/BetterAGI/agent
# 全量
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q
# 只看本轮新增
uv run --with pytest --with pytest-asyncio --with pillow --with httpx --with pyyaml python -m pytest -q \
  tests/test_executor_history.py tests/test_motor_record_status.py tests/test_incident.py tests/test_streamer.py
```
