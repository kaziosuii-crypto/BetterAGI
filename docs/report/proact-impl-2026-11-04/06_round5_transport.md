# 传输层优化：四步实测报告（2026-11-04）

> 目标：把「手模型每步到底慢在哪」拆成可复现的三个数，并在**不改默认值**的前提下，
> 给出「历史帧 5 张 vs 2 张」的 A/B 证据。所有数字都在真端点 `http://i-2.gpushare.com:47960`
> （vLLM，model id `gamescaling`，root `/hy-tmp/gs/model/GameScaling-9B-S3`）上测的。

## 0. 一句话结论

| 问题 | 答案 |
|---|---|
| 连接复用有用吗 | 有用，但只在**小请求**上明显：单帧 1782ms → 995ms（省 0.79s）；6 帧大请求被上传压过，无收益 |
| 每步时间怎么分 | `t_encode` **0.5ms** / `t_upload_infer` **828ms** / `t_act` 由按键窗口决定 —— 编码不是瓶颈，上传+推理才是 |
| 5 帧 vs 2 帧 | 生产格式（JPEG 150KB/帧）：**1125ms vs 924ms**，解析成功率都是 **10/10**；PNG 口径差距被放大到 3052ms vs 1566ms |
| 帧格式要不要改 | **不改**。生产链路本来就是 JPEG q85 `subsampling=0`、1280x736，150KB/帧；退化成 idle 的是**降采样**（960 宽），不是 JPEG 本身 |

## 1. 第 1 步：连接改成模块级单例 + 开播前 warmup

**代码**（`agent/motor.py`）

- `TRANSPORT_LIMITS = httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=300.0)`
- `shared_client(timeout=...)`：工厂变了或 `is_closed` 就重建，否则复用同一个连接池
- `GamescalingClient.warmup(attempts=2)` → 连打两次 `GET /v1/models`，返回 `{ok,status,attempts,elapsed_ms,reused}`
- `health()` / `decide()` 全部走 `shared_client`；`close_shared_client()` 供退出时收尾

**测试**：`agent/tests/test_motor_transport.py`，7 例全绿（含 autouse 隔离连接池、`REAL_CLIENT = httpx.Client` 抓真类避免 monkeypatch 递归）。

**实测**（`_diag/transport_step1.py` → `_diag/transport_step1.json`，1280x736 PNG，803,671 B/帧）

| 场景 | 冷连接 | 复用连接 | 变化 |
|---|---|---|---|
| warmup（`/v1/models`） | 231.8ms | 82.1ms | −150ms |
| 1 帧 decide | 1782.4ms | 995.1ms | **−787ms** |
| 6 帧 decide | 3020.2ms | 3174.1ms | ±0（噪声内） |

结论：握手成本约 0.79s，只在小请求里看得见；**6 帧 = 4.8MB 上传把一切都压过去了**。

## 2. 第 2 步：三段计时落到 `run/<sid>/steps.jsonl`

**代码**

- 新模块 `agent/steptrace.py`：`StepTrace(root, session_id)` 每步追加一行 JSONL，字段 `t_encode` / `t_upload_infer` / `t_act` + `intent_id, step, t_capture_ms, payload_bytes, history_len, key_groups, pressed, late_steps, raw_chars`；`summary()` 给每段 n / 中位数 / p95 与缺段警告（某段没测到就记 `None`，不拿别的段冒充）。
- `agent/executor.py`：`_frame_url()` 顺手量 `captured`（截屏完）与 `encoded`（转 data URL 完）两个时间戳；主循环用 `act_started` 包住 `play_steps` 得 `t_act`；`result['steps_summary']` 直接带三段汇总。
- `agent/run_stream.py`：开播装配时建 `trace`，`run()` 的报告里带 `report['steps']`。

**测试**：`tests/test_steptrace.py` 7 例 + `tests/test_executor_trace.py` 4 例，全绿。

**真机数字**（`_diag/trace_probe.py` → `_diag/trace_probe.json`，真执行器 + 真手模型，4 步，生产 JPEG 130KB/帧）

| 段 | 中位数 | p95 | 含义 |
|---|---|---|---|
| `t_encode` | **0.5ms** | 0.6ms | 抓完到 data URL 生成完，可忽略 |
| `t_upload_infer` | **828.0ms** | 876.0ms | 上传 + 排队 + 推理 + 解出动作 |
| `t_act` | **695.3ms** | 695.7ms | 本次用假 adapter，固定 sleep 6×33ms 累计，属假件特性；真机应≈198ms |
| 合计 | 1523.7ms | — | 真机若 t_act≈198ms，则落在 **≈1.03s/步** |

每行 `history_len` 从 1 递增到 4，证明「写回历史」这条链在真执行器里确实生效（第 4 轮补的 `motor.record`）。

**怎么读**：以后说「手模型慢」必须拆开说——`t_upload_infer` 大就是网络/排队，`t_act` 大就是按键窗口，`t_encode` 大才是本地编码。单次上传秒数不再当带宽用。

## 3. 第 3 步：10 步 A/B（历史帧 5 张 vs 2 张）

跑法：A/B **交替**（偶数步先 5 后 2，奇数步先 2 后 5），每步 `reset()` 后按指定张数灌历史，保证历史长度就是被测值；`reasoning=False`，`deadline_ms=None`（不让预算截断污染时延）。

### 3.1 按你指定的 PNG 1280x736 口径（`_diag/ab_history.py` → `ab_history.json`）

| 组 | 解析成功 | 中位 | p95 | min–max | 请求体 |
|---|---|---|---|---|---|
| hist5 | 10/10 | **3051.7ms** | 3130.2 | 1023.7–3130.2 | **6.11MB** |
| hist2 | 10/10 | **1566.1ms** | 1605.5 | 1475.4–1605.5 | **3.06MB** |

差 ≈1.49s，与 3MB 上传体积差一一对应。**解析成功率没有差别**：少 3 张历史帧不会让模型不会解析。

### 3.2 生产帧格式 JPEG q85 `subsampling=0` 口径（`_diag/ab_history_jpeg.py` → `ab_history_jpeg.json`）

| 组 | 解析成功 | 中位 | p95 | min–max | 请求体 | 单帧 |
|---|---|---|---|---|---|---|
| hist5 | 10/10 | **1125.0ms** | 1362.0 | 862.5–1362.0 | 1.17MB | 150.4KB |
| hist2 | 10/10 | **923.5ms** | 1005.0 | 783.8–1005.0 | 0.58MB | 150.4KB |

生产口径下差 **201ms（18%）**，而不是 PNG 口径的 1.49s —— **同一组数据，格式一变，结论的份量就差 7 倍**。
这也是为什么第 2 步坚持要三段计时：拿 PNG 的 3s 去推线上，会得出「必须砍历史帧」的错误紧迫感。

> 注：两边都是 10/10，只测了「能不能解析」，**没有测动作质量**。要判断 2 帧够不够，还得看同一段画面下两组的动作分布是否一致（下一轮可做）。

## 4. 第 4 步：帧格式核查（结论：不改）

查了生产代码，**现在的线上格式本来就是 JPEG，不是 PNG**：

- `agent/adapter.py:393` `grab(fmt='JPEG', quality=85, ...)`
- `agent/adapter.py:88` `image.save(buffer, format='JPEG', quality=85, subsampling=0)`（不做色度欠采样）
- 尺寸仍是 1280x736（原尺寸 + 底部黑边补到 736），没有降采样

同一帧四种编码的实测体积（`_diag/frame_format_size.py`）：

| 编码 | 单帧 | 6 帧合计 |
|---|---|---|
| PNG 1280x736（compress_level=1） | 1,289,719 B | 7.38MB |
| **JPEG q85 sub0（生产）** | **151,632 B** | **0.87MB** |
| JPEG q85 sub2 | 130,260 B | 0.75MB |
| JPEG q90 sub0 | 201,695 B | 1.15MB |

所以「改小/改 JPEG 会退化成 idle」这条教训要精确复述：**退化的是降采样（960 宽）**，
不是 JPEG。生产用的 1280x736 JPEG q85 在真执行器上 4/4 解析成功、每组 2–6 个键（不是 idle）——见 `_diag/trace_probe.json`。

**本轮没有改动任何帧格式代码**，也没有动 `history_len` 默认值。

## 5. 等你拍板的三件事

1. **历史帧默认值**：保持 5 张（现默认，动作质量口径最保守）/ 改 2 张（省 201ms，10/10 解析）/ 改 3 张折中。
2. **是否要测动作质量**：同一段画面、5 帧 vs 2 帧各跑 30 步，比键位分布与「是否撞墙原地重复」。
3. **5090 关机时点**：`i-2.gpushare.com:47960` 现在还在计费，测完就可以关；要留着我继续做上面第 2 项。

## 6. 复跑命令

```powershell
cd E:\BetterAGI\_diag
$uv = 'C:/Users/Tsuba/AppData/Local/Microsoft/WinGet/Packages/astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe/uv.exe'
& $uv run --with httpx --with pillow python transport_step1.py   # 第 1 步
& $uv run --with httpx --with pillow python trace_probe.py        # 第 2 步
& $uv run --with httpx --with pillow python ab_history.py         # 第 3 步（PNG 口径）
& $uv run --with httpx --with pillow python ab_history_jpeg.py    # 第 3 步（生产口径）
& $uv run --with pillow python frame_format_size.py               # 第 4 步
```

原始数据：`_diag/transport_step1.json`、`_diag/trace_probe.json`、`_diag/trace_probe_run/steps.jsonl`、`_diag/ab_history.json`、`_diag/ab_history_jpeg.json`。
