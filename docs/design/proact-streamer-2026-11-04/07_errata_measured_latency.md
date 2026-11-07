# 07 · 勘误：200ms 是游戏时间，不是墙钟预算

- 日期：2026-11-04（实施第一轮之后补测）
- 影响文档：`01_architecture.md` §6/§7、`02_schemas.md` §4 的 deadline 表、`03_code_change_plan.md` §C.3/C.4、`04_acceptance.md` §四 的「手单步 decide」指标
- 结论等级：**推翻原假设**（不是调参）

## 1 原来写的是什么

设计和第一版实现都把 `motor` 的单步预算当成 **200ms 墙钟**：

- `02_schemas.md` §4 等待点表：`motor_step 300ms`；
- `03_code_change_plan.md` §C.4：`deadline_ms=200`，超时算 `slow_steps`；
- 实现里 `agent/motor.py` `MOTOR_STEP_BUDGET_MS=200`、`agent/executor.py` `run(deadline_ms=200)`、`agent/orchestrator.py` `execute(call.setdefault('deadline_ms', 200))`。

`Docs/GameScaling-9B-S3_API使用说明.md` §8 其实写了「200ms 的决策窗口是**游戏时间**语义」，但这句话在实现时被读成了墙钟。

## 2 实测（5090，vLLM，GameScaling-9B-S3）

5 次/档，训练骨架提示词 + 官方示例帧，10/10 parsed：

| 档位 | p50 | p95 | min | max |
| --- | --- | --- | --- | --- |
| 开思考 | 1155ms | 1484ms | 708ms | 1484ms |
| 关思考 | 696ms | 735ms | 673ms | 735ms |

脚本：`E:/BetterAGI/_diag/gs_5090_latency.py`（隧道 `gs_5090_probe.py`）。一次输出覆盖 6×33ms=198ms 游戏输入，所以：

| 档位 | 墙钟 | 按键 | 空闲 | 占空比 | 等效慢动作 |
| --- | --- | --- | --- | --- | --- |
| 关思考 | 740ms | 198ms | 542ms | 27% | 3.7× |
| 开思考 | 1490ms | 198ms | 1292ms | 13% | 7.5× |

## 3 勘误后口径（以此为准）

- **`chunk_ms = 200`**：一次决策输出覆盖的游戏时间窗口（6 步 × 33ms；真正按键 198ms）。
- **`wall_ms`**：这一次决策允许花的墙钟。默认取实测 p95：关思考 **740ms**、开思考 **1490ms**；`BETTERAGI_MOTOR_STEP_MS` 可覆盖。
- `02_schemas.md` §4 的 `motor_step` 一行按上式理解（300ms 这个数作废）；`04_acceptance.md` §四 的「手单步 decide」指标应改为「p95 ≤ 实测 p95 × 1.15」。
- 实现落点：`agent/pacing.py`（`step_budget_ms / duty_cycle / pacing / describe`）；`Orchestrator(motor_budget_ms=...)` 与 `run_stream.run()` 的返回体都带这笔账。

## 4 为什么这不是「调个参数」

按 200ms 判超时，对着真服务是**每一步都超时**——`motor_timeout → failed → 交棒`，一条动作都放不出去，直播会变成「一直道歉、一直交棒」。这不是性能问题，是语义错误。

## 5 剩下要人拍板的（三条路，代码不替用户选）

1. **接受慢动作**（3.7×–7.5×）：行为最连贯，先用这条跑通。
2. **降决策频率**：每 1.5s 决策一次，一次输出更长的动作串（要模型侧配合，训练分布是 6 组）。
3. **并行预取**：一次请求在播时请求下一次（VRAM 26/32GB，未压测；要求动作「晚一步才失效」）。

## 6 复测

```
python E:/BetterAGI/_diag/gs_5090_probe.py     # 起隧道 + 健康
python E:/BetterAGI/_diag/gs_5090_latency.py   # 5 次/档延迟
python E:/BetterAGI/_diag/pacing_smoke.py      # 只算账，不连服务
```
## 7 瓶颈拆解：算力还是网络（2026-11-04 追加实测）

问：单步 700ms/1490ms 里，算力占多少、网络占多少？结论：**取决于是 1 帧还是 6 帧请求**——1 帧时算力（解码）是主项但绝对值很小；生产真实形态（6 帧）时网络上传是主项。

### 7.1 三块时间与实测（5090 上 vLLM，官方 Doom 示例帧，每组 3 次）

| 组 | 帧数 | 上传体 | prompt_tokens | 输出 token | 墙钟 p50 / min–max |
|---|---|---|---|---|---|
| `ink_1f_max1` | 1 | 257 KB | 1843 | 1 | **323 / 303–373 ms** |
| `ink_1f` | 1 | 257 KB | 1843 | 24–30 | **565 / 543–697 ms** |
| `ink_6f_max1` | 6 | 1524 KB | 6483 | 1 | 4229 / 2103–7448 ms |
| `ink_6f` | 6 | 1524 KB | 6483 | 23–24 | 5080 / 1961–7457 ms |
| `bad_model_6f`（404，不做推理） | 6 | 1524 KB | — | — | 4891 / 865–9690 ms（纯上传） |

拆出来的三段：
- **上传**：1 帧 257KB ≈ 0.25–0.30s；6 帧 1.5MB ≈ **0.9–9.7s（中位 ~4.9s）**，抖动一个数量级。
- **预填充**：1 帧 1843 token ≈ 0.07s（323ms 减去上传）；6 帧 6483 token 随之增长，但每帧约 930–1000 token，预填充不是主项。
- **解码**：565−323 = 242ms / 24 token ≈ **99 tok/s**，正好等于 9B bf16（约 18GB）在 5090（约 1.79TB/s）上的单流带宽上限 → 解码已经贴着显存带宽跑，换更大卡、加量化对这一步的收益极小。

### 7.2 网络这一侧的两层

| 项 | 实测 |
|---|---|
| 家庭公网上行（Cloudflare `__up`，1MB） | **1.18 MB/s ≈ 9.4 Mbps** |
| 同一时刻经 paramiko SSH 隧道写 1.5MB 到远端 | 0.11–1.09 MB/s（1.5MB 合 **1.4–13s**） |

即：链路上限本身就只有 ~1.2 MB/s，paramiko 转发又把它压到 0.1–1.1 MB/s 且抖动极大。**所以「6 帧 1.5MB」这个请求形态，在家庭宽带上注定是秒级**——这不是模型慢。

### 7.3 由此得到的优化优先级（按收益/代价排）

1. **别每步重传 5 张历史帧**：1.5MB → 257KB，上传降 83%。代价是与训练分布（当前帧 + 5 历史帧）不一致，需实测动作质量损失。
2. **帧降质/降分辨率**（1280×720 q90 → 640×360 q70，约 30–40KB）：上传再降一个数量级，视觉 token 也降到约 1/4。
3. **换掉 paramiko 隧道**：改用系统 `ssh.exe -N -L`（OpenSSH，吞吐高一个量级），或让客户端/转发跑在 GPU 主机侧，只把动作（几十字节）带回来。
4. **上传与解码流水线化**：发第 N+1 帧时让第 N 步在算，把上传藏到计算背后（与 §5 第 3 条「并行预取」是同一件事）。
5. **不要做的**：换更大卡、上量化——解码只占 0.24s 且已到带宽上限。

复测：`python E:/BetterAGI/_diag/gs_5090_decompose.py 3`（五组对照）、`python E:/BetterAGI/_diag/gs_link_throughput.py`（隧道吞吐）、`python E:/BetterAGI/_diag/uplink_probe.py`（公网上行）。原始值落盘 `_diag/gs_decompose.json`、`_diag/gs_link_throughput.json`、`_diag/uplink.json`。

## 8 追加勘误（第三次实测，暴露端点 + 真彩排）：软预算要配宽限，超时要清历史重试

- 日期：2026-11-04 晚（实施第 7 轮）
- 端点：`http://i-2.gpushare.com:47960`（用户把 5090 的 vLLM 8000 直接暴露出来，`/v1/models` → `gamescaling`、`root=/hy-tmp/gs/model/GameScaling-9B-S3`、`max_model_len=32768`）
- 结论等级：**口径修正 + 实现缺陷**（不是调参）

### 8.1 端点上的真延迟（`_diag/probe_47960_latency.py`，结果 `_diag/probe_47960_latency.txt`）

官方输出骨架提示词 + 真 Doom 帧，`decide(deadline_ms=None)`，解析 8/8：

| 请求形态 | 墙钟 |
| --- | --- |
| 1 帧 / 关思考 | 861 / 904 / 914 ms |
| 6 帧（当前 + 5 历史）/ 关思考 | 1200 / 1206 / **3435** ms |
| 1 帧 / 开思考 | 1233 / 1578 ms |

### 8.2 第一次真彩排：两轮都是 0 步

`_diag/real_motor_rehearsal.py`（真 HTTP + 真 5090）两轮都 `ended=motor_timeout`、`steps=0`、`decide_ms=[883]/[763]`，因为 `pacing.step_budget_ms(thinking=False)` 给的 **740ms 被当成了硬线**（httpx timeout 只剩余预算）。模型其实答得上来，是客户端先掐断。

两处修正（都落地了）：

1. **软预算 + 宽限**（`agent/motor.py`）：`decide(..., grace_ms=None)` → `hard_ms = deadline_ms + grace`；`DEFAULT_GRACE_MS = 3000`（`BETTERAGI_MOTOR_GRACE_MS` 可覆盖，非法值一律退回默认，绝不变成 0）。超软预算**不算失败**，只记 `late_ms` / `over_budget`。硬线是 p95 的 3 倍以上——实测尾延迟到过 2282ms，也到过 **3788ms**，任何「p95 当硬线」都会被公网上传抖动误杀。
2. **单次超时等价重试**（`agent/executor.py`）：第一次 `motor_timeout` 不结束意图，`timeouts += 1` 后 `continue`；连续两次才收手。重试**先清手历史**（`motor.reset()`）——超时的主因是「5 张历史帧 = 1.5MB」在 3.7Mbps 上传下光上传就 ~3.2s，清掉后只带当前帧（约 250KB）。

### 8.3 彩排 v2 证据（`_diag/rehearse_v2.py`，结果 `_diag/rehearse_v2.json`）

真手模型（暴露端点，关思考）+ 真慢系统（DeepSeek `slow_planner`，预算 6000ms）+ 真冻结泵/贴图 + 真录播（ffmpeg 分片）+ 真卡住检测（window=6），走 `Streamer.round()` 两轮：

| 项 | 值 |
| --- | --- |
| 两轮结果 | 都是 `reached` / `goal_reached` |
| 手模型解析 | 2/2（`parsed=true`），每步 6 组键位 |
| 决策墙钟 | 1141.8ms / 901.6ms（预算 740ms，`slow_steps=1` 记账，不判死） |
| 冻结窗口 | 1858ms / 1586ms（两条 freeze 事件成对，`restored=true`） |
| 冻结泵 | `frames=15`、`painted=4`、`errors=0` |
| 贴图成帧 | 1280x720，173525B → **147308B**，与源帧平均像素差 **15.782**，`_diag/rehearse_v2_painted.jpg` |
| 交棒报告 | 2 份，conclusion 20 字 / observations 134、163 字 |
| 录播 | 1 分片 `seg_000.mp4`，520,355B |

离线验收（本轮新增 `agent/verify.py`）跑这个运行目录：**4 项通过 / 0 不通过 / 5 跳过**（A2 动作归属、A3 单调、A4 交棒报告、A5 分片锚点全过；A1/A6/A7/A8/A9 因为这次没落会话事件、没便利贴、没事故而诚实跳过，不伪装通过）。

### 8.4 顺带查出的真缺陷：冻结泵根本调不动真贴图件

`agent/freeze_pump.py` 的 `_paint()` 只认 `painter.draw`，而真贴图件 `agent/freeze_overlay.FreezePainter` 的方法叫 **`paint`**；实例又不可调用，于是 `out = jpeg` 原样透传——**`painted` 计数在涨、画面一个字都没贴上**，单测用的是假件 `Paint.draw` 所以全绿。

修法：`_paint()` 依次尝试 `paint` → `draw` → 可调用对象；新增回归用例 `test_real_freeze_painter_actually_changes_the_frame`（真 `FreezePainter` + 真 JPEG，断言 `frames[0] != jpeg`）。这是「假件把接口缺陷盖住」的第二个实例（第一个是 `FakeNarrator` 缺 `ask`，真链路是 `IntentFastSession.ask()`）。

### 8.5 复测命令

```powershell
cd E:/BetterAGI/_diag
$env:PYTHONIOENCODING='utf-8'
uv run --with pillow --with httpx --with pyyaml python probe_47960_latency.py
uv run --with pillow --with httpx --with pyyaml python rehearse_v2.py
```
## 9 MP4 式帧间差分：实现了，收益只有个位数百分比（2026-11-05 实测）

用户要求「每次发图片参考 mp4 编码，只发送变化部分」。实现已完成（`agent/frame_delta.py` + `svc/frame_relay.py`），实测收益小于按「字节数 × 上行速率」的推算，原因见 §9.5。

### 9.1 为什么不是「外接矩形」而是「块」

`E:/BetterAGI/_diag/measure_delta_mp4.py` 在 28 帧 Undertale 序列上量外接矩形 patch 方案：

| 方案 | 平均体积 | 与整帧之比 |
| --- | --- | --- |
| 每帧整帧 webp q65 | 10,867B | 1.00 |
| 变化外接矩形 patch | 7,140B | 0.66 |

`inspect_delta.py` 的 16px 分块图显示：Undertale 一屏只有 2–3% 像素在变，但这些像素**分散在整片文字区**，外接矩形一框就框住大半屏幕。改成 16px 块 + 变化块拼 atlas 后降到 6,195B（0.57）；块取 8 或 32 差别不大（7,061B / 6,134B）。

顺带否掉了无损路线（`measure_codecs.py`）：webp 无损 56,627B、png 94,431B、png8 34,606B，因为源帧是 JPEG q85 带噪声，无损把噪声全保留；q40 也只有 9,562B。残差图（P 帧经典做法）6,725B 且还原误差 2.8/像素，不如块 atlas（`measure_residual.py`）。

### 9.2 协议（客户端 ↔ GPU 机上的中继）

- 客户端 `agent/g2p.py` 在请求体顶层加 `frame_delta: {sid, seq, refs:{cur}}`；messages 里当前帧写占位符 `delta://cur`，历史帧写 `frame://-1`、`frame://-2`（**零字节**，这是参考帧的关键）；
- 中继 `svc/frame_relay.py`（FastAPI，跑在 127.0.0.1:8010）按 sid 保存最近 12 帧（PIL 图像常驻内存），把占位符替换成还原后的整帧再转发给 127.0.0.1:8000；公网链路 = 网关 47960 → 实例 8080（`/root/fwd8080.py` 裸 TCP 管道，TARGET 已改成 8010）→ 中继 → vLLM；
- `seq` 是同步保险：keyframe 发 0 让中继清空重来，之后必须逐帧 +1，不等就 409 `need_full`，客户端重置 chain 后发一次整帧。**没有这个校验时中继会拿别的帧当基准拼出「根本不是当时画面」的图且不报错**，静默错误比崩溃危险；
- 中继连不上或 5xx 时，`g2p._fallback_direct()` 自动关掉差分、退回整帧重发（实测死端口 → 3063ms 内恢复并正常出动作）；中继对普通整帧请求完全透传，所以「退回整帧」不需要第二条网络路径。

### 9.3 编码格式（服务器实测，640x480 单帧）

| 格式 | 编码耗时 | 体积 | 解码耗时 |
| --- | --- | --- | --- |
| PNG | 23.4ms | 96,025B | 4.9ms |
| JPEG q85 | 1.6ms | 33,980B | 1.7ms |
| WEBP q65 | 11.7ms | 12,416B | 3.6ms |

中继→vLLM 是 loopback，体积不要钱，所以发 vLLM 用 **JPEG q85**（每帧省 22ms 编码）；存链条本身用 PIL 图像，拼帧零编解码。

### 9.4 端到端实测（18 组配对，同一帧交替发）

| 指标 | 整帧 | 差分 |
| --- | --- | --- |
| 请求体 | 35,063B | 15,662B（−55%） |
| p50 | 332ms | 320ms |
| 配对差 | — | mean −30ms，14/18 次更快 |

画面质量（28 帧重放，`check_delta_quality.py`）：还原帧与原始帧平均像素差 **1.18**，再叠一次 JPEG q85 也只 1.21，肉眼不可辨且不随帧数漂移（块是绝对像素，参考帧用发送方自己的原图而不是还原结果——否则静止画面会被自身量化噪声判成变化，每次都退回整帧）。

### 9.5 为什么只有 ~9%

按 §7 的 2.9ms/KB 推算，省 19.4KB 该省 56ms，实测只有 30ms。原因是**上传本来就不是主体**：一次请求 ≈ 网关固定开销 80–100ms + 模型推理 240–380ms + 上传 20–30%，省掉的那点字节只能吃掉上传那一份。

同时也否掉了「差分一定更快」的直觉：第一版中继（每帧 PNG 存 + 每次重编 WEBP + PIL 跑在事件循环里）量出来差分 599ms vs 整帧 498ms，**差分更慢**；把中继改成「PIL 图像常驻内存 + `asyncio.to_thread` 卸载 CPU + 直出 JPEG」后 resolve 从 74ms 降到 5.7–11ms，才翻正。教训：小样本（6 次）+ 网络抖动会把结论测反，必须配对设计。

### 9.6 复测命令

```powershell
cd E:/BetterAGI/_diag
$env:PYTHONIOENCODING='utf-8'
E:/BetterAGI/.venv/Scripts/python.exe measure_tile_delta.py
E:/BetterAGI/.venv/Scripts/python.exe bench_paired.py
E:/BetterAGI/.venv/Scripts/python.exe check_delta_quality.py
E:/BetterAGI/.venv/Scripts/python.exe check_failover2.py
```

中继面板：`GET http://i-2.gpushare.com:47960/frame/stats`（含 `ms_avg_resolve` / `ms_avg_upstream`）。