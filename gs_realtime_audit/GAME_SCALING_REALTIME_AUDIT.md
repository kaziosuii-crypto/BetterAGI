# GameScaling 实时推理性能审计与流式执行改造报告

生成时间：2026-11-05（本机 Asia/Shanghai）。审计对象：RTX 5090 32GB 上当前部署的 GameScaling checkpoint，出口 http://i-2.gpushare.com:47960/。
执行口径（任务书原则）：①第一阶段不改模型权重 ②不擅自修改训练过的 Output Format ③不直接替换生产部署 ④所有性能优化必须有对照实验 ⑤先建可复现 Benchmark 再决定改造 ⑥不允许把 API 吞吐提升当成单请求实时延迟下降 ⑦不允许把流式输出等同于解决了过期观测问题。最终：不以理论加速倍数作为结论，只以实际单请求端到端测量和真实游戏控制结果作为结论。
本报告所有数值均标注来源与口径。凡「本项目实测」给出样本数与 P50/P95；凡「未实测 / 受限」明确写出原因，不以估计冒充实测；凡「理论推算」明确标注。全部原始数据可追溯到 benchmark/ 与 network/ 下的 JSONL。

## 0. 执行摘要与结论速览

- 当前部署（vLLM 0.33.0 / BF16 / TP=1 / RTX 5090）单次决策端到端 P50 约 499ms，其中 Prefill ≈63ms、Decode ≈351ms、首 Token(TTFT) ≈187ms、第一组键盘动作可用(ActionTTFT) ≈392ms。**Decode 是最大单项耗时，其次 Prefill；网络在服务器本机直连可忽略，但远程公网出口叠加约 3.1s 额外延迟**。
- 官方同步客户端等完整 JSON 才 parse，六组动作虽在 499ms 时同时可用，但没有任何一组能提前执行。流式直连 8000 时六组确实逐组到达（chunk 间隔 P50 10.6ms、生成跨度 342ms），**具备做增量执行的数据基础**。
- 但当前生产链路（公网 47960 → 8080 → 8010 frame_relay → 8000）中 frame_relay 用 Response(content=resp.content) 整体缓冲 SSE，实测远程所有 SSE 块在约 0.5–1.1ms 内一次性到达、首块≈完整响应。**因此「流式输出」在当前部署上并不等于「能提前拿到动作」**；要落地流式执行必须先改这一层转发。
- 最大、最稳的单请求收益来自 **prefix caching**：关闭后 Prefill 63→498ms、E2E +432ms；且当历史帧每轮变化时命中率从 6336→576、Prefill 63.9→276.7ms、E2E 491.8→715.3ms（实证任务书「六张历史图不能假设都能缓存」的警告）。
- 无思考(reasoning=false) 稳定省约 28ms 且让后续组 P95 全部 <33ms；stream_interval=1 是 batch1 唯一让五段组间隔 P95 全部 <33ms 的配置。降分辨率/JPEG q70 收益很小(<25ms)，PNG 是灾难(+494ms)，max_tokens 128~2048 几乎无影响（模型只出约 34 tokens）。
- **不建议改协议、不建议重训模型**。B/C/D 均为探索性方案且需微调，理论最多把 g1 提前 75ms，仍 >200ms 决策窗口。MTP 权重实测不存在、CUDA Graph 已最优、FP8/量化需换权重（禁做）、guided regex 已实测负面。
- 三个最值得投入工程时间的优化：① 修复 frame_relay 的 SSE 缓冲（让流式可达，零模型改动）② 保证/提升历史 KV 命中（稳定 prefix caching）③ 开启 reasoning=false 与 stream_interval=1（省 ~28ms 且组间隔达标）。

## 1. 环境信息

来源：audit/environment.json（captured_utc 2026-11-05T04:48:00Z，只读探针，无推理）。

| 项 | 值 |
| --- | --- |
| GPU | NVIDIA GeForce RTX 5090，32607 MiB（32GB），sm_120 消费级 Blackwell GB202 |
| Driver / CUDA / PyTorch | 580.76.05 / CUDA 13.0 / PyTorch 2.14.0+cu130 |
| CPU / RAM | INTEL XEON GOLD 6530，128 vCPU / 503 GiB |
| vLLM | 0.33.0（Python 3.11） |
| checkpoint | /hy-tmp/gs/model/GameScaling-9B-S3，Qwen3_5ForConditionalGeneration，BF16 单文件 18.8GB |
| 模型结构 | 32 层混合注意力（24 GDN + 8 full attention），full_attention_interval=4，hidden 4096，vocab 248320 |
| 启动参数 | TP=1，dtype bfloat16，max-model-len 32768，gpu-memory-utilization 0.85，limit-mm-per-prompt '{"image": 8}' |
| 运行态（启动日志） | cudagraph_mode=FULL_AND_PIECEWISE（51 PIECEWISE + 35 FULL 全部捕获成功，非 enforce_eager）；enable_prefix_caching=true；enable_chunked_prefill=true；attention_backend=FLASH_ATTN；vit=FLASH_ATTN；gdn prefill=FlashInfer；mamba_cache_mode=align |
| KV Cache | 6.48 GiB / 182706 tokens |
| 投机解码 / 量化 | speculative_config=None，quantization=None，reasoning_parser 空 |
| 其它 GPU 进程 | 仅 vLLM EngineCore（frame_relay uvicorn 为 CPU-only，127.0.0.1:8010） |

MTP 权重证伪（重点）：config.json 的 text_config.mtp_num_hidden_layers=1 只是声明。用 /root/vllm-env/bin/python + safetensors.safe_open 实扫 model.safetensors：total tensors=760，MTP/draft/nextn/eh_proj/enorm/hnorm 相关=0，layer indices 0..31（max 31），layers.32 为空，top-level model=759 + lm_head=1。**该 checkpoint 没有可用 MTP 头**，任何 MTP 投机解码需要额外权重（不存在），不能假设 MTP 存在。

启动日志其它事实：torch.compile AOT 命中缓存 1.32s / init engine 65.84s；multi-modal warmup 19.469s；Triton kernel 在推理中 JIT 编译 store_cache_checkpoints_kernel 造成延迟尖峰（warmup 未覆盖该 shape，出现频率/幅度【需实测】）；serving.py:291 要求 system-first 顺序（官方客户端 system prompt 在首位，满足 prefix cache 条件）。

工具链：本机 bundled python 无 matplotlib（有 pandas/numpy/PIL），服务端 python 有 requests/PIL/numpy、无 matplotlib/pandas。全部图表用 PIL 自绘或纯统计，不 pip install。

## 2. 当前架构分析

依据 audit/official_code/policy.py、action.py、prompt.py、system_prompt_template.txt、serve_vllm.sh、_server_scripts.txt 真实源码。

协议（已训练 Output Format）：<think></think> 换行 <|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>。X/Y=整个 200ms 窗口鼠标相对位移，Z=滚轮，g1~g6=六组键盘动作每组约 33ms、每组最多 4 键、连续组相同键=持续按住、空组=释放所有键。CHUNK_STEPS=6。

阶段 0 客户端核对（逐条，policy.py 行号）：

| 核对项 | 结论 | 依据 |
| --- | --- | --- |
| requests.Session / Keep-Alive | 是，持久连接 | :174 建 Session，:208/:185 复用 |
| 同步 POST、等待完整 JSON | 是，post 无 stream 参数 | :208-213 随后 payload=r.json() |
| 等完整 JSON 才解析 | 是，finalize + parse_action 都在 r.json() 之后 | :222-229 |
| 逐 Token / 逐动作组回调 | 无 | act() 无 stream/回调，只对最终 text 一次 parse |
| 可取消请求 | 无 | 仅整体 timeout=120.0，无中途取消 |
| KV Cache 复用 | 客户端无显式控制，依赖服务端 prefix caching + system leading | :192；environment.json |
| 每次是否重复编码全部历史图 | 否，history 存已编码 data URL 字符串，每轮只编码当前帧一次；但请求体仍带全部 H+1 张图 | :195/:199/:203/:225；实测 n_images=6、image_url_bytes=253107 |
| 历史动作是预测还是实际执行 | 是模型上一轮输出（action.history_text），非执行器实际结果，无任何回写 | :225；action.py |

PolicyConfig 默认：history_len=5、history_think=0、reasoning=True、temperature=1.0、max_tokens=2048、image_format=jpeg、jpeg_quality=90、image_size=None、resample=bilinear、stop=[<|action_end|>]、lenient_parse=False、timeout=120.0。history_len=5 → 最多 6 张截图，实测与之一致。serve_vllm.sh 注释「8 images = 5 history + current + headroom」。

注意：history 存的是模型预测而非真实执行结果，上下文与真实世界状态可能漂移（闭环质量影响【需实测】）。

## 3. 同步基准（阶段 1）

工具：benchmark_runner.py，服务器本机单请求并发、requests.Session Keep-Alive、顺序执行、每配置 10 次预热 + 100 次正式测量，原始记录 benchmark/*.jsonl。

指标定义：TTFT=T7−T0（首 Token）；Action TTFT=T11_g1−T0（第一组键盘动作可用）；Full Generation Latency=T17−T7；End-to-End=T17−T0。每项给 Mean/Median/P50/P90/P95/P99/Min/Max/StdDev（见 report/summary.json）。T4 服务端收到 / T5 Prefill 开始 / T6 Prefill 完成为 client 不可直接观测，用 vLLM Prometheus delta 相对 T3 近似（server_approx_T5/T6/T7，report 标注近似）。

batch1 主表（P50 / P95，ms；n=100）：

| 配置 | E2E P50 | E2E P95 | TTFT P50 | ActionTTFT P50 | Prefill P50 | Decode P50 |
| --- | --- | --- | --- | --- | --- | --- |
| baseline_sync（同步） | 499.6 | 549.0 | 不可测 | 499.6（整响应） | 63.2 | 350.5 |
| baseline_stream（流式） | 499.0 | 567.6 | 186.8 | 392.0 | 63.1 | 350.6 |
| sync_nothink | 471.2 | 533.6 | 不可测 | 471.2 | 62.6 | 329.3 |
| stream_nothink | 471.3 | 512.9 | 180.8 | 364.3 | 62.8 | 329.3 |
| stream_stream_interval_1 | 492.6 | 545.5 | 181.7 | 387.2 | 63.1 | 350.6 |
| stream_si2 | 493.4 | 535.4 | 183.4 | 396.1 | 62.9 | 350.5 |
| stream_prefix_off | 931.6 | 991.7 | 618.6 | 822.8 | 498.0 | 352.0 |
| sync_prefix_off | 928.0 | 979.8 | 不可测 | 928.0 | — | — |
| stream_prefix_on_saltfix | 493.3 | 545.1 | 182.5 | 386.7 | 62.9 | 350.7 |

Token/缓存（baseline_stream，100 次一致）：prompt_tokens=6484、generation_tokens≈34、thinking Tokens=0（reasoning=True 但 prefill 后模型直接闭合 think，实测 think_chars=0）、prefix_hits=6336、mm_hits=6、prefill_kv_computed_tokens=148、output TPS≈96(34 tokens / 350ms)。请求失败率 0，动作解析失败率 0，finish_reason=stop 100/100，g6 由 finish_reason 确认 100/100，no-op 动作 1 次。

结论：同步与流式 E2E 几乎相同（都 499ms），差别在**可用性时间**——同步必须等 499ms 才能动，流式 392ms 就能拿到 g1、499ms 拿齐六组。这 107ms 是流式的全部理论提前量（generation_tail=T17−T_g1）。

## 4. TTFT 与 Action TTFT（阶段 1/2）

baseline_stream 关键时间线（P50，ms，相对 T0）：T7 首 Token 186.8、T8 think 结束≈、T9 <|action_start|> 316.7、T10 鼠标头 X Y Z 齐 357.6、T10b 头部分号 367.7、T11 g1 392.0、T16 g6 498.6、T17 请求完成 499.0。

- TTFT 186.8ms、Action TTFT 392.0ms，两者相差约 205ms，即 <|action_start|> 之前有约 130ms 的「无用」Token/头部解码 + 约 75ms 的头部到 g1。
- 第一组动作可用 392ms 已显著超过 prompt.py:28 要求的 200ms 决策窗口（实测事实）。
- generation_tail（T17−T_g1）=107.3ms；n_content_chunks=29，chunk 间隔 P95 10.9ms。
- TTFT 分布图见 report/charts/ttft_distribution.png，Action TTFT 分布见 action_ttft_distribution.png。

## 5. g1~g6 分组延迟（阶段 2，最重要）

baseline_stream 分段计时（P50 / P95，ms）：

| 段 | P50 | P95 | <33ms? |
| --- | --- | --- | --- |
| action_start→header | 42.6 | 42.9 | — |
| header→g1 | 21.4 | 32.1 | ✓ |
| g1→g2 | 21.3 | 32.2 | ✓ |
| g2→g3 | 21.3 | 42.6 | ✗ |
| g3→g4 | 32.0 | 37.4 | ✗ |
| g4→g5 | 10.7 | 21.4 | ✓ |
| g5→g6 | 21.6 | 32.3 | ✓ |
| g6→complete | 0.42 | 0.48 | — |

任务书阶段 2 七问逐答：
① 第一组(g1)多少 ms：P50 392.0ms（Action TTFT）。
② 后续组平均多久一组：P50 约 10.7~32.0ms（段间不等），总体六组生成跨度约 107ms。
③ 后续组 P95 是否 <33ms：baseline **否**，g2→g3(42.6)、g3→g4(37.4) 超标。
④ 是否有某组明显更慢：是，g2→g3 与 g3→g4 两段 P95 明显更高（约 37~43ms），g4→g5 最快(10.7ms)。
⑤ 第一组前是否有大量无用 Token：是，think_chars=0 但 <|action_start|> 出现在 316.7ms，TTFT→action_start 约 130ms 用于头部/标签解码。
⑥ 模型是否在生成思考：实测否（think_chars=0，reasoning=True 下模型直接从闭合 think 继续），故「无思考」并非去掉了真实思考，而是省掉 prefill 差异。
⑦ 是否有输出缓冲导致 SSE 到达≠Token 生成：服务器本机直连无（chunk 间隔 10.6ms 与 decode 一致）；但经 8010/8080/47960 有整体缓冲（见 §7），远程到达时间≠生成时间。

对照：stream_nothink 五段组间隔 P95 全部 <33ms（32.6/32.1/32.4/21.3/32.3）；stream_stream_interval_1 也全部 ≤32.4ms，是 batch1 唯一满足「后续组 P95 全 <33ms」的两个配置之一。stream_si2 最差（前几段 P95 42.7ms）。

reasoning 对照（阶段 2 要求，特别检查 reasoning=False 是否真避免思考）：reasoning=False 让 E2E 471.3 vs 499.0（−28ms）、ActionTTFT 364.3 vs 392.0（−28ms）、Decode 329.3 vs 350.6、Think Token 数两者都≈0。**结论：更快主要来自 prefill 为空 think 块与更短 decode，不是「避免了思考」——因为 reasoning=True 下模型本就没生成可见思考内容。不能仅因更快就认为 reasoning=False 更好**；需结合动作质量（本报告未做质量对照，标注【需实测】）。六组时间线图见 report/charts/six_group_timeline.png，各配置组间隔见 group_intervals_*.png。

## 6. Prefill / Decode 分解（阶段 1/3）

baseline_stream：Prefill 63.1ms（约 13%）、Decode 350.6ms（约 70%）、Queue/其余≈85ms。占比图见 report_batch2/charts/prefill_decode_ratio.png 与 report/charts/prefill_decode_ratio.png。

- Prefill 对 prefix caching 极敏感：关掉（cache_salt=unique）后 Prefill 63.1→498.0ms（+435ms），TTFT 186.8→618.6ms，E2E 499.0→931.6ms（+432.6ms），而 Decode 基本不变（350.6→352.0）。**证明收益来自 Prefill 缓存，不是 Decode 变快**。
- 历史帧变化的影响（任务书警告，实证）：静态帧+相同历史 → prefix_hits 6336/mm_hits 6；当 5 轮历史各用不同帧（history_variants=true）→ prefix_hits 576、mm_hits 1、Prefill 63.9→276.7ms、E2E 491.8→715.3ms（smoke 极端到 597ms Prefill）。**真实部署历史图每轮变化，六张不可能全命中，静态基准高估了缓存收益**。

## 7. 网络延迟分解（阶段 6，此前网络曾占总决策时间大部分）

真实链路（读源码纠错，文档注释不可信）：公网 47960 → 0.0.0.0:8080(fwd8080.py 字节透明 TCP，源码 TARGET=(127.0.0.1,8010)，其 print 文案误写 8000) → 127.0.0.1:8010(frame_relay uvicorn，pid 12242) → 127.0.0.1:8000(vLLM)。恒源云自定义服务必须监听 0.0.0.0:8080。

frame_relay.py /v1/chat/completions（_server_scripts.txt:252-292）：resp = await _client.post(UPSTREAM + /v1/chat/completions, ...) 后 return Response(content=resp.content, ...)。**resp.content 整体缓冲，SSE chunk 不会逐步到达**。

四链路同体对照（network/summary.md，同一份官方 body：13 messages、6 张 JPEG q90、body 1,523,761 bytes）：

| 链路 | n | E2E P50 | 首块 P50 | chunk 跨度 P50 | 跨度 P95 | chunks | 块间隔 P50 | ActionStart P50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| direct 127.0.0.1:8000 | 100 | 482.9 | 139.6 | 342.2 | 397.2 | 37 | 10.6 | 300.9 |
| relay8010 | 30 | 510.4 | 509.3 | 1.1 | 1.2 | 37 | 0.0 | 509.8 |
| fwd8080 | 30 | 507.6 | 506.6 | 1.1 | 1.4 | 37 | 0.0 | 507.1 |
| public47960（Windows 公网） | 30 | 3626.3 | 3625.8 | 0.5 | 1.0 | 37 | 0.0 | 3625.9 |

结论：
- 本机直连 8000 时 SSE 真逐步到达（跨度 342ms、间隔 10.6ms）；一旦经过 frame_relay(8010)/fwd8080/公网，所有 37 个块在约 0.5–1.1ms 内一次性到达、首块≈E2E → **流式响应被整体缓冲，远程无法逐步收到动作**。这正是阶段 6 要排查的「本机流式正常而远程不能逐步收到 → 代理/传输层」情形，根因定位到 frame_relay 的 resp.content。
- 本机 relay/fwd 相比 direct 仅多约 25ms（缓冲+转发），但公网 47960 叠加约 3.1s（1.5MB body 上传经代理 + DNS fake-ip 198.18.1.46 + Keep-Alive 仅 4s）。**当前生产部署的主要延迟来自公网链路与整体缓冲，不是 vLLM 本身**。
- 网络优化：Keep-Alive/持久 Session 官方已有；需修的是 SSE 缓冲与图片上传体积（见 §8 JPEG/分辨率）。SSE 是否被代理缓冲：是（已证）。图见 network/charts/network_chunk_span.png。

## 8. 图像历史与分辨率影响（阶段 3，batch2 21 配置 × 30 次筛选）

report_batch2/summary.md（P50，ms；除 guided 3 次外各 n=30，err=0、parse_fail=0）：

| 配置 | E2E P50 | ActionTTFT P50 | Prefill P50 | 备注 |
| --- | --- | --- | --- | --- |
| b2_baseline_ref | 491.8 | 387.4 | 63.9 | 基线 |
| b2_hist0 | 514.0 | 385.4 | 60.2 | P95 1285.6 抖动大 |
| b2_hist1 | 446.5 | 349.4 | 61.2 | E2E 最优 |
| b2_hist2 | 466.3 | 366.6 | 61.7 | |
| b2_hist3 | 480.5 | 375.3 | 61.1 | |
| b2_res1280x720 | 489.5 | 386.4 | 62.5 | 与 Native 同一档（源图 1280x720），重复控制组 |
| b2_res960x540 | 480.6 | 373.9 | 62.0 | |
| b2_res640x360 | 468.8 | 359.7 | 61.0 | 最快分辨率档 |
| b2_jpeg80 | 480.9 | 371.1 | 63.0 | |
| b2_jpeg70 | 475.9 | 367.7 | 62.6 | |
| b2_png | 986.1 | 879.6 | 63.9 | 体积 4.55×，灾难 |
| b2_maxtok128/256/512 | 490.6/496.9/493.6 | ~384/393/388 | ~63 | 与 2048 几乎无差（模型只出 34 tokens） |
| b2_prefix_on_fixed | 493.7 | 386.8 | 63.1 | |
| b2_prefix_off | 930.3 | 821.2 | 496.6 | Prefill +433ms |
| b2_histvariant_on | 715.3 | 608.4 | 276.7 | 历史帧变化，缓存失效 |
| b2_histvariant_prefix_off | 957.4 | 850.1 | 525.3 | 双重失效 |
| b2_si1 | 489.0 | 382.2 | 62.6 | |
| b2_stream_nothink | 470.5 | 363.2 | 64.1 | |
| b2_guided | 2869.1 | — | 63.0 | parse_fail 3/3，跑满 tokens，负面 |

结论：history_len=1 最优 E2E 446.5ms（比基线省 45ms）但 hist0 反更慢且抖动大；降分辨率到 640x360 省约 23ms；JPEG q70 省约 16ms；PNG +494ms；max_tokens 基本无关。**注意：降分辨率是否损害小型 UI/血量/敌人/技能图标的可读性未做视觉质量对照【需实测】，不能仅以文件大小决定最佳编码**。图见 report_batch2/charts/。

## 9. Streaming 实验（阶段 4）

原型 prototypes/streaming_client.py（离线完成，--self-test 45/45 全 mock SSE PASS、--parity 全绿、官方目录未改动）：
- build_official_body 与官方 build_request 完全相等（8 键 model/messages/temperature/max_tokens/skip_special_tokens=False/stop/continue_final_message=True/add_generation_prompt=False）；PolicyConfig 16 项默认值零差异；prefill 一致（reasoning=True 为 <think> / False 为 <think>空闭合）；无 chat_template_kwargs。
- 流式请求体相对官方体**唯一新增 stream=True**（include_usage 默认关，开启再加 stream_options 属显式偏离）；System Prompt / Output Format / model 全不动；保留 skip_special_tokens/continue_final_message/add_generation_prompt 与现有 stop。
- 增量回调 on_mouse_header / on_group / on_done，finalize 补回 ACTION_END，错误分类，usage 缺失处理，JSONL 指标（TTFT/Action TTFT/g1~g6 间隔）。
- 已知限制（阶段 6 关键）：必须直连 127.0.0.1:8000；frame_relay 的 Response(content=resp.content) 会整体缓冲，chunk 不逐步到达（见 §7）；自检是 mock，未对真实 vLLM 端到端跑；perf_counter 跨进程不可比。

增量解析器与执行器原型及单测：见 §10/§11（若对应文件缺失，按「未完成」如实标注，见 §15 交付清单状态）。

## 10. 增量解析器测试（阶段 4）

要求：缓冲区处理特殊 Token/数字/按键名/分号被拆到不同 SSE 块、空组、think 内容、流式中断；状态 WAIT_THINK_END / WAIT_ACTION_START / READ_MOUSE_HEADER / READ_GROUP_1..6 / DONE / ERROR；think 未结束不得执行、鼠标头未完整不得提交、组未完整不得提交、收到分隔符才确认组完整、第六组需 action_end 或 finish_reason 确认、不允许执行未完成 Token、结果必须与现有 parse_action 一致、保留分隔符数量与空组语义。
人工构造 SSE 用例（1 Token/块、组拆多块、action_start/action_end 拆开、连续按键、空组、非法按键、>4 键、<6/>6 组、think 中出现类动作文本、reasoning=False、g1/g3 后中断、g6 后正常结束、finish_reason=stop/length、断网、超时）每个比较 Streaming Parser Result vs 现有 parse_action Result，完整响应必须一致。
状态：prototypes/streaming_client.py 自带轻量增量 buffer 并完成 45 项 mock SSE 自检与 parity 校验；**独立的 incremental_action_parser.py 与其人工 SSE 单测未交付（子代理未落盘），如实标注未完成**，见交付清单（§16）。本审计未对真实 vLLM 流式输出逐块跑单测（受 frame_relay 缓冲限制），标注为离线验证。

## 11. Executor 测试（阶段 5）

要求：独立执行器不把未验证流式动作发到正式游戏；收到完整组立即入队每组目标 ~33ms；按键状态（连续组相同键保持按住、变化释放旧键、空组释放全部、跨块状态、不许卡死）；鼠标 A 一次性发完整位移 vs B 平均分配到六个 33ms 片（比较镜头平滑度/总位移误差/输入响应/控制精度，滚轮只触发一次）；中断模拟 g1/g2/g3/g5 后断开（停止未确认动作、释放按键、不猜测剩余、标记不完整、记录已执行）；动作队列记录生成/提交/开始/结束/等待时间与 Action Underrun Rate；调度 A 完整响应才执行 / B 每组完整立即入队 / C 固定 33ms 片不提前 / D 迟到延长上一动作 vs 安全释放，先模拟器比较。
状态：受「不替换生产 / 无真实游戏环境 / 安全」约束，仅做模拟器/影子模式验证，未在真实游戏执行（见 §14）。prototypes/streaming_executor.py（42,248 bytes，虚拟时钟纯离线模拟器）+ tests/test_streaming_executor.py（27,314 bytes）已落盘，`python tests/test_streaming_executor.py` → **Ran 55 tests, 55 PASS, 0 FAIL, 0 ERROR（0.021s）**，覆盖 A/B/C/D 调度、鼠标 A 一次/B 六段、滚轮只触发一次、g1/g2/g3/g5 中断、跨块键盘状态机、>4 键裁剪、非法键丢弃、finish_reason 结束、Action Underrun Rate、纯模块性断言（不 import time/requests/subprocess/socket/threading）。

模拟器 A/B/C/D 实测（同一合成轨迹 compare_strategies()）：

| 策略 | Underrun Rate | 首次有效输入 | 总动作时长 | 卡键 |
| --- | --- | --- | --- | --- |
| A 同步基线 | 0.0 (0/6) | 215.0ms | 200.0ms | 0 |
| B 每组立即入队 | 0.0 (0/6) | 10.0ms | 208.3ms | 3 |
| C 固定 33ms 片 | 0.5 (3/6) | 10.0ms | 208.3ms | 1 |
| D_extend | 0.5 (3/6) | 10.0ms | 208.3ms | 1 |
| D_release | 0.5 (3/6) | 10.0ms | 208.3ms | 0 |

限制（如实）：纯离线模拟器不碰真实输入/时间/网络；B 的 underrun 恒 0 是策略相对定义，须与首次有效输入合看；D 两子策略与 C 共用调度、仅卡键风险不同；默认轨迹为手写合成时间戳而非真实 vLLM 采集，要出真实结论需用 benchmark/ 的真实组到达时间喂 run_simulation()。

## 12. 解码优化实验（阶段 7）

在 Streaming 原型之后每次只改一个变量。本部署硬约束导致多数项无法做 A/B，均如实说明：
- CUDA Graph：已是 FULL_AND_PIECEWISE（启动日志 51 PIECEWISE + 35 FULL 全捕获），是低延迟最优默认；--enforce-eager 只能启动期设置，切换需重启生产（违反原则③）。**无对照实验**，报告说明「需重启生产才能切换」。
- 无法并行起第二份 vLLM 实例：显存 26.4GB/32GB 已占用，9B BF16 + KV 放不下两份 → 作为约束记录。
- FP8 / 量化：需换权重重载，违反「不擅自替换模型/不替换生产」→ 不做，仅给文献性预期（量化不必然降单请求延迟）。
- MTP：checkpoint 实测无 MTP 张量（见 §1）→ 明确不适用；不假设原始 Qwen3.5 的 MTP 存在。
- N-gram / Suffix / Grammar-aware / 固定格式 Token 预测：利用协议确定性分隔符 ; 与 <|action_start|>/<|action_end|> 理论可行（Lumine 论文 forced-token + 拒绝采样），但当前 speculative_config=None、vLLM 0.33.0 无现成 grammar-aware 实现，issue #62130 指出 spec-decode × structured-outputs 在此版本不组合；均需额外验证开销/接受率/分布一致性【需实测】。
- guided regex（structured_outputs）：**实测负面**——与模型 <think>…<|action_start|> 格式不兼容，跑满 max_tokens（batch1 preflight 23s/2048 tokens/parsed=False；batch2 限流后 3/3 parse_fail、decode 2719ms、E2E 2869ms）。作为受控失败结论保留，不得再无限制启用。
- 唯一可做的 per-request 手段：stream_interval（si1/si2）与 cache_salt（prefix on/off）已在 batch1/batch2 有对照（见 §3/§5/§6）。
- Kernel 与 Prefill：Qwen3.5 混合注意力 24 GDN + 8 full attention、FlashInfer GDN prefill、FLASH_ATTN、RMSNorm/LM Head 等内核均为启动期选择，Prefill 与 Decode 已在 §6 分解；不把 Prefill 加速当 Decode 加速。

## 13. 输出协议评估（阶段 8，仅在前述实验完成后决定）

详细逐条论证见 audit/sections_draft.md §8。A 原始 X Y Z ; g1..g6（已训练、兼容、分号天然可流式解析）；B 组优先 g1..g6 ; X Y Z；C 鼠标六段分块；D 动态 2/6/12 长度。

- 基线（baseline_stream P50）：T9_action_start 316.7 / T10_mouse_header 357.6 / T11_g1 392.0 / T16_g6 498.6 ms。
- 「组优先」把 g1 提前的理论上界 = T11_g1 − T9_action_start = 75.3ms（按标记边际 p50 相减）；按配对 p50 头部段约 64.0ms；保守下界约 21.4ms。**全部为理论推算、不是实测**。即便按 75.3ms 上界，g1 仍约 316.7ms，仍 >200ms 窗口。
- B 破坏训练过的 Output Format（prompt.py 明令逐字复制字符串、action.py 解析器要求 parts[0] 为三个数字），需重做 Prompt + 全量 SFT + 质量对照；C 需全新逐 33ms 鼠标微位移训练标签（现有训练数据只有 200ms 聚合位移）；D 破坏固定 6×33ms 契约。三者成本高、质量风险大。
- **结论：当前不建议改协议、不建议重训模型**。优先做完零模型改动项（修 SSE 缓冲、稳 prefix caching、流式增量解析、reasoning=false、si1）；若未来确要改 B/C/D，必须独立微调 + 质量对照（动作质量/解析成功率/闭环成功率/overshoot），用 p50/p99 而非均值。B/C/D 均为探索性方案，独立实验分支验证，不得直接替换正式模型。

## 14. 端到端真实游戏测试（阶段 9）

任务书要求 6 方案（同步 / 流式 / 流式+无思考 / 流式+最优历史 / 流式+最优分辨率 / 流式+最优组合）同场景同初始状态比较首次有效输入时间、动作成功率、方向误差、镜头抖动、按键卡死、受击次数、任务完成率、平均耗时，至少 30 次不同场景质量测试，先影子执行再真实操作。

**本次审计未完成真实游戏端到端测试，如实标注原因**：① 无安全隔离的真实游戏环境与受控场景夹具，② 原则③不直接替换生产部署，③ §7 已证实当前生产链路 SSE 被整体缓冲、流式动作无法提前到达，真实流式执行前置条件尚不满足，④ Unsafe 调度策略不得直接在真实游戏测。因此仅完成到「模拟器 / 影子模式」的解析器与调度器离线验证（§10/§11、streaming_client.py 45 项自测）。**不以模型输出正确评价执行效果，不谎称完成 30 场景质量测试**。下一步需在隔离环境 + 修复 frame_relay 后补做。

## 15. 最终推荐方案（含 14 个必答决策问题）

**当前最优配置（零模型改动、可直接用于服务器本机直连）**：reasoning=false + stream_interval=1 + history_len=1（或保 5 视质量）+ JPEG q90 原生分辨率 + 保持 prefix caching 开启 + 直连 127.0.0.1:8000 的持久 Session。组合预期 E2E 约 446~471ms、ActionTTFT 约 349~364ms、后续组 P95 全 <33ms。

**优化前后实测**：基线 sync E2E 499.6ms / 六组同时可用；最优流式 E2E 约 446~471ms、g1 提前到约 349~364ms（比同步 499ms 提前约 135~150ms 可用）；若再保证历史命中，Prefill 维持 ~63ms 而非 498ms（关缓存 +432ms 的反例）。

**每项收益与代价**：修 SSE 缓冲（收益：让流式真正可达，零模型代价，但需改 frame_relay 代码）；prefix caching（收益最大 +432ms，代价：依赖稳定 system prompt 与历史，真实帧变化会打折）；reasoning=false（−28ms，代价：需质量对照）；si1（组间隔达标，E2E 几乎不变）；降分辨率/q70（<25ms，代价：UI 可读性风险未验证）；PNG/guided（负收益，禁用）。

任务书 14 个必答决策问题：
1. 延迟主要来自网络/图像编码/Prefill/Decode？—— 服务器本机：Decode(351ms)>Prefill(63ms)>首 Token 前开销(约130ms)>图像编码(每轮仅当前帧一次)。经公网 47960：网络+整体缓冲占主导（额外约 3.1s）。
2. 第一组动作实际 P50/P95？—— P50 392.0ms；P95 见 report/summary.json baseline_stream.action_ttft_ms（本摘要未逐值抄录，原始数据可查）。
3. 后续组能否稳定 <33ms？—— baseline 否（g2→g3 42.6、g3→g4 37.4 P95 超标）；reasoning=false 或 si1 可全部达标。
4. Streaming 是否真正减少有效动作等待时间？—— 服务器本机直连是（g1 392ms 比整响应 499ms 早 107ms）；当前生产公网链路否（被整体缓冲 + 3.1s 延迟）。
5. 减少多少毫秒？—— 本机直连约 107ms（generation_tail）；叠加 reasoning=false 约 135ms；不是吞吐提升。
6. 是否需要修改输出协议？—— 否，当前不建议；理论上界仅 75ms 且 g1 仍超 200ms。
7. 是否需要重新训练模型？—— 否；B/C/D 探索性方案需微调但当前不划算。
8. 5090 32GB 上哪种推理配置最优？—— 现役 BF16/TP=1/FULL_AND_PIECEWISE/prefix caching 已接近最优；per-request 加 reasoning=false + si1；量化/第二实例不可行（显存/权重约束）。
9. 当前是否值得 MTP 或其他投机解码？—— 不值得：无 MTP 权重、vLLM 0.33 grammar-aware 不成熟、guided 实测负面；grammar-aware forced-token 仅文献可行【需实测】。
10. 最值得投入工程时间的三个优化？—— ① 修 frame_relay SSE 整体缓冲 ② 稳定/提升历史 KV 命中 ③ reasoning=false + si1 + 流式增量解析落地。

**仍存在的瓶颈**：① frame_relay 整体缓冲使生产链路无法流式 ② 公网 1.5MB 图片 body 上传 + 代理约 3.1s ③ Decode 351ms 是本机硬下限 ④ 历史帧变化使 prefix caching 收益打折 ⑤ 第一组 392ms 已超 200ms 窗口 ⑥ 真实游戏闭环质量未验证。

**下一阶段建议**：修复 frame_relay 改真·SSE 透传并复测远程逐块到达；在隔离游戏环境用影子模式验证增量解析器/执行器调度 A/B/C/D 与中断恢复，再补 30 场景质量测试；如需进一步降 Decode，评估换推理栈后的 grammar-aware forced-token 投机（含接受率/分布一致性对照）；所有改动保持独立分支，不部署生产。

## 16. 交付物清单与状态

- GAME_SCALING_REALTIME_AUDIT.md（本文件，15 节 + 摘要）
- 原始数据：benchmark/*.jsonl（batch1，9 配置×100）、benchmark_batch2/*.jsonl（batch2，21 配置×30）、network/*.jsonl（4 链路 190 次）；全部可追溯到原始请求。
- 统计与图表：report/summary.json|md + report/charts/*.png（10 张）、report_batch2/summary.json|md + charts/*.png（9 张）、network/summary.json|md + charts/network_chunk_span.png。
- 可运行原型（**第二部分交付状态已更新**）：
  - prototypes/incremental_action_parser.py ✅（含 `starts_in_reasoning` 修复，tests/test_incremental_parser.py **39 tests 全 PASS**）；
  - prototypes/streaming_executor.py ✅ + tests/test_streaming_executor.py **55 tests 全 PASS**；
  - prototypes/action_execution_receipt.py ✅ + tests/test_action_receipt.py **14 tests 全 PASS**；
  - prototypes/gamescaling_stream.py（StreamingPolicy 包装层，不改官方 policy.py）✅ + tests/test_streaming_policy.py **8 tests 全 PASS**（服务器 venv 实跑）；
  - prototypes/realtime_loop.py（三开关集成）✅ + tests/test_realtime_loop.py **10 tests 全 PASS**；
  - relay/frame_relay_stream.py（真流式透传，替代 `Response(content=resp.content)`）✅；
  - prototypes/streaming_client.py（+README.md，45/45 mock SSE 自测 + parity 全绿）✅；
  - benchmark_runner.py（服务器版，history_variants v2）✅。
  - **单测合计 126 项，全部 PASS**。原同步客户端未覆盖、未改动。
- 环境/源码/研究：audit/environment.json、audit/official_code/*（官方只读副本）、audit/sections_draft.md、research/lumine_streaming_spec_decoding.md、research/vllm_latency_optimization.md；工具：analyze.py、network/network_probe.py、network_analyze.py、network/body.json。
- 独立分支：`gamescaling-streaming-audit`（第一部分审计）与 `feature/streaming-realtime-control`（第二部分改造）；**未经批准不得部署生产**。
- 第二部分新增原始数据：network/loop_A.jsonl、network/loop_B_A.jsonl、network/loop_D_B.jsonl（各 40 轮）、network/strategy_on_real_arrivals.json（40 chunk × 5 策略）、network/stage5_partial_history.json（部分执行真实模型验证）、network/stage1_summary.json|md（六链路 SSE 对照）、network/stage7_C_stats.json（隧道 108 次）、network/stage7_abcd_summary.json（A/B/D 对照）。
- 第二部分单测合计 126 项（39+55+14+8+10）全 PASS。
- 未实测/受限项已在各节标注【需实测】，并在 audit/sections_draft.md 列出 14 条需实测清单；本报告不以理论推算冒充实测。
---

# 第二部分：实时流式推理与闭环执行改造（AutoDL RTX 4090 + GameScaling-2B-S3）

> 本节对应任务书《GameScaling 实时流式推理与闭环执行改造》7 个阶段的**实测结果**。
> 硬件：AutoDL RTX 4090 24GB；模型 THUSI-Lab/GameScaling-2B-S3（5.44GB，ModelScope）；
> 推理：vLLM 0.33.0 + torch 2.14.0+cu130，`--dtype bfloat16 --max-model-len 32768`，
> 端口 127.0.0.1:6006，KV cache 13.56 GiB。9B 因 24GB 显存 OOM 无法服务（见 §17）。
> 用户明确未开放公网端口，全部测量经 SSH 隧道完成。

## 17. 9B 无法在该卡上服务的原因（记录为约束，非遗漏）

- 9B bf16 权重 18.8GB + 视觉编码器缓存 + profile activation，在 24564 MiB 的 4090 上必 OOM：
  `torch.OutOfMemoryError: Tried to allocate 512.00 MiB. GPU 0 has a total capacity of 23.52 GiB of which 95.75 MiB is free`
- 已尝试 `--max-model-len 16384`、`--limit-mm-per-prompt image:6`、`--gpu-memory-utilization 0.85`、
  `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`，仍 OOM。
- 用户授权降级到 2B（"9b起不来就换成2b吧"）后全部改造在 2B 上落地。
- 结论：**该硬件上只有 2B 可服务**；若需 9B 必须换 ≥48GB 显存机器。这不是实现缺陷。

## 18. 阶段1：Relay 流式透传（已验收）

### 18.1 根因

旧 `frame_relay.py` 的两行：

```python
resp = await _client.post(UPSTREAM + "/v1/chat/completions", content=...)
return Response(content=resp.content, status_code=resp.status_code, media_type=...)
```

`Response(content=resp.content)` 会等整个 body 读完才返回，SSE 的增量性在 Relay 层被彻底抹平。

### 18.2 新实现 `relay/frame_relay_stream.py`

- 上游 `httpx.AsyncClient.build_request(...)` + `send(req, stream=True)`，下游 `StreamingResponse(_pipe(), ...)`；
- `_pipe()` 用 `async for chunk in upstream.aiter_raw()` 原样 yield，`finally: await upstream.aclose()`
  （客户端断开即 `CancelledError/GeneratorExit` → 取消上游，不残留生成）；
- 响应头 `Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no`；
- 帧差分逻辑（`delta://cur` / `frame://-N`）完全保留，只改传输；
- 非流式请求保持原行为，`/v1/models` 仍代理。

### 18.3 四链路实测（P50 ms，相对请求发出）

| 链路 | n | 首内容 | action_start | g1 | g6 | finish | finish−g1 |
|---|---|---|---|---|---|---|---|
| A1 vLLM 本机 6006 | 30 | 72.5 | 141.5 | 164.0 | 220.6 | 230.3 | 61.4 |
| A2 旧 relay(整体缓冲) 8011 | 30 | 222.7 | 223.1 | 223.2 | 223.6 | 223.7 | **0.3** |
| A3 新 relay(流式) 8010 | 30 | 73.5 | 137.5 | 157.2 | 209.3 | 221.3 | **56.9** |
| B1 隧道→vLLM 直连 | 30 | 340.6 | 432.8 | 461.8 | 511.9 | 519.9 | 53.1 |
| B2 隧道→旧 relay | 30 | 682.4 | 682.5 | 682.5 | 682.5 | 682.6 | **0.1** |
| B3 隧道→新 relay | 30 | 392.1 | 515.0 | 539.0 | 575.0 | 587.4 | **50.7** |

**判定**：旧 relay 把六组和 finish 压到同一毫秒（finish−g1 = 0.1~0.3ms），增量信息全丢；
新 relay 的 chunk 间隔与本机直连完全一致（均 4.71ms），Relay 自身开销仅 **+0.95ms**。
Windows 客户端经隧道经新 relay 时，**g1 比 finish 早 50.7ms（P50）到达**，即"模型还没生成完就收到第一组动作"成立。
隧道额外 +318.6ms 属 RTT/带宽，不是缓冲。

## 19. 阶段2：增量解析器（39 项单测全绿）

`prototypes/incremental_action_parser.py`，状态机
`WAIT_THINK_END / WAIT_ACTION_START / READ_MOUSE_HEADER / READ_GROUP_1..6 / DONE / ERROR`。

### 19.1 真实部署暴露并修复的缺陷（重要）

首轮 parity 只有 **16/20**。4 个失败 trial 的报错是：

```
bad mouse header: '100 to 87 while the enemy is directly in front of the player,
so continuing to stand still and trade damage is too risky. ... resume LB fire ...
point-blank range.
</think>
<|action_start|>24 -1 0'
```

根因：prefill 是 `<think>` 时，vLLM 服务端会消费它，首个 delta 直接是**推理散文**（以 "100 to 87 ..." 开头）。
原守卫在 `</think>` 未到达且 `<think>` 不在已累积文本中时，落入 "No reasoning at all" 分支把
`_reasoning_end` 置 0，于是散文被当成动作体解析。修复：构造参数
`IncrementalActionParser(starts_in_reasoning=True)`（由请求体的 prefill 推断），
该模式下未见到 `</think>` 一律不解析；`finish()` 时仍未闭合则判 truncated。

### 19.2 真实 vLLM SSE 结构（实测，非 mock）

- 特殊 token 确实被流式下发且**被拆块**：`'\n\n</t'`、`'hink><'`、`'|'`、…、`'|>'`；
- chunk 间隔 4.7~5.1ms，真增量；
- `stop=["<|action_end|>"]` 会把 `<|action_end|>` 截掉，assembled 文本里**没有**它，
  结束只能靠 `finish_reason='stop'` → 因此 g6 必须由 finish_reason/action_end 确认；
- 修复后 parity **20/20 identical to official parse_action**，groups/trial = 6。

## 20. 阶段3：`StreamingPolicy.act_stream()`（真实 vLLM 五模式全过）

`prototypes/gamescaling_stream.py` 包装官方 policy，不改官方文件。
请求体相对官方 `build_request` **只新增 `stream=True` + `stream_options={"include_usage": true}`**
（System Prompt / Chat Template / 图像 / 历史 / 采样参数 / stop 全部不变）。

| 模式 | n | 结果 |
|---|---|---|
| parity | 20 | **20/20 与官方 parse_action 完全一致** |
| nthink | 10+10 | reasoning=True think_chars mean 99.9；False 恒 0（确实无思考）；ActionTTFT P50 165.4 vs 150.6 |
| history | 10 | prompt_tokens 1834→6508 单调增长，history 长度单调 |
| cancel | 10 | 10/10 `cancelled=True kind=cancelled confirmed=0`，socket 释放 |
| rollback | 10 | 10/10 parsed=True（`streaming_enabled=False` 走官方同步，P50 211.6ms） |

修复的真实缺陷：HTTP 错误分类（`requests.HTTPError` 是 `RequestException` 子类，
raise 会被记成 network，且 `FakeResponse` 无 `.text` 时抛 AttributeError）→ 改为就地分类。

## 21. 阶段4/5：执行器 + 执行回执 + History 接线

- `prototypes/streaming_executor.py`：55 项单测；`prototypes/action_execution_receipt.py`：14 项单测；
- 集成模块 `prototypes/realtime_loop.py`：10 项单测，覆盖**三开关矩阵**：
  - 全关 = 官方客户端（阻塞请求 + 预测进 history）；
  - 仅 streaming = 组增量到达但不调度；
  - 仅 executor = 调度发生、history 仍是预测；
  - 仅 receipt = **什么都没有执行**，receipt 标 `executor_disabled`，下一轮被告知 NOOP
    （不把没执行的组伪装成已执行）；
  - 全开 = 执行组进 history，部分执行保持部分。
- 时间锚定：executor 的 slot 网格以 **g1 为 0 点**提交（否则 160ms 的推理会让全部组"迟到"）；
  绝对时间只进 receipt。
- `action_execution_receipt.py` 修掉 3 处 dataclass 尾逗号 bug（`x: str = "",` 会把默认值变成 tuple）。

### 21.1 部分执行 → History（真实模型验证）

3/6 组执行后中断，写入下一轮的历史文本：

```
<think>
partially executed (network)
</think>

<|action_start|>-143 -19 0 ; LB ; A ; LB ;  ;  ; <|action_end|>
```

恰好 6 个 `;` 槽位、3 组有键、3 组留空、中断原因走推理通道。真实 2B 模型后续 8 次请求
**8/8 parsed=True, finish=stop**（latency P50 ≈ 256ms），即最小兼容表示被真实模型接受，
且不需要补齐成六组、不改变训练分布。

## 22. 阶段6：闭环 + Observation Age（40 轮真实推理）

`Observation Age at First Action = first_action_timestamp − observation_timestamp`

| 模式 | n | Observation Age P50 | P95 | P99 | 执行组数 | underrun |
|---|---|---|---|---|---|---|
| A 官方同步（全开关关） | 40 | **360.6 ms** | 573.6 | 717.7 | 0（未接线执行器） | – |
| B/D 流式+执行器+回执 | 40 | **277.8 ms** | 287.5 | 313.2 | 240/240 | 0 |

- A 的 P50 就是"完整响应延迟"，因为官方客户端必须等整个响应才能执行第一组；
- 流式把 P50 压低 **82.8ms**、P95 压低 **286.1ms**（A 的 P95 被长思考 trial 拉高）；
- **A 的 40/40 轮 Observation Age > 200ms**（决策窗口本身），即每一轮都在用上一帧之前的画面做决策；
  流式后降到 37/40。这个事实按任务书要求显式暴露，没有被 Streaming 掩盖。
- 执行器影子模式（4090 上无游戏/无输入设备）：240/240 组被执行、underrun 0、
  无卡键（跨块按键状态机由单测覆盖）。

## 23. 阶段7：A/B/C/D 对照 + 调度策略

### 23.1 推理侧（同一 40 轮闭环）

| 指标 | A 官方同步 | B 流式+执行+回执(mouse A) | D 同 B(mouse B) |
|---|---|---|---|
| Observation Age P50 | 360.6 | 298.8 | 277.8 |
| Observation Age P95 | 573.6 | 309.2 | 287.5 |
| Action TTFT P50 | = 360.6 | 298.8 | 277.8 |
| E2E P50 | 360.6 | 336.5 | 321.2 |
| E2E P95 | 573.6 | 347.4 | 332.7 |
| 执行组 / underrun | – | 240 / 0 | 240 / 0 |

### 23.2 调度策略（用实测组间隔 21ms 喂模拟器，40 chunk）

| 策略 | underrun | overlap | 首次有效输入 P50 | 总动作时长 P50 | 卡键 |
|---|---|---|---|---|---|
| A 等完整响应 | 0 | 0 | 143.3 ms | 200.0 ms | 0 |
| B 到达即起 | 0 | **200** | **21.0 ms** | 138.3 ms | 0 |
| C 固定 33ms 片 | 0 | 0 | 33.3 ms | 200.0 ms | 0 |
| D_extend | 0 | 0 | 33.3 ms | 200.0 ms | 0 |
| D_release | 0 | 0 | 33.3 ms | 200.0 ms | 0 |

- **B 会产生 200 次 overlap**（21ms 间隔 < 33.333ms 片长，组与组重叠）→ 违反"不许不同动作块交叉执行"，
  因此默认不用 B 作为生产策略；
- C/D 零 overlap、零 underrun、零卡键，首次有效输入 33.3ms；
- **结论：默认 C（固定 33ms 时间片）**；D 仅在需要"迟到时延长上一动作"语义时按配置启用。
- 这与早期合成轨迹上的 A/B/C/D 表一致（那次 B 的 underrun=0 属策略相对定义）。

### 23.3 C 链路（Windows 经隧道，108 次成功样本）

t_first_byte P50 589.2 / P95 1128.0；t_g1 P50 714.6 / P95 1460.2；t_finish P50 774.2；
**finish−g1 P50 58.2ms，100% 的样本 g1 早于 finish**（73.1% 早于 50ms 以上）。
即公网/隧道客户端同样在生成结束前拿到第一组。

## 24. 开关与回退（任务书第十节）

```python
loop = RealtimeLoop(policy, frame_source,
                    streaming_enabled=False,          # 默认 False
                    streaming_executor_enabled=False,  # 默认 False
                    execution_receipt_enabled=False)   # 默认 False
```

- 三个开关互相独立，可单独开启/回退；全关即官方客户端行为；
- `StreamingPolicy.streaming_enabled=False` 时 `act()` 直接委托官方同步路径（实测 rollback P50 211.6ms，10/10 parsed）；
- 官方 `gamescaling/policy.py`、`action.py`、`prompt.py`、输出协议、System Prompt **均未修改**；
  改造以包装层 + 新文件方式落地（`prototypes/`、`relay/`）。
- 独立 Git 分支 `feature/streaming-realtime-control`，未合入生产。

## 25. 仍未完成 / 需要说明的项

1. **真实游戏 30 场景端到端未做**：本环境无隔离游戏环境与输入设备，4090 上只能影子执行
   （Executor 记录"本来会发什么"，不实际发输入）。任务书允许的影子模式已验证数据正确，
   真机受控测试需要在有游戏的机器上按同样开关重放 `network/loop_*.jsonl` 的回放。
2. **C 链路样本有隧道损耗**：100 次顺序探测因 SSH transport 中途死亡损失约 36%
   （`ConnectionAbortedError [WinError 10053]` ×26、`ConnectionResetError [WinError 10054]` ×10），
   分三批补跑后累计 108 次成功。这是隧道可靠性问题，不是模型/relay 问题。
3. `test_streaming_policy.py` 依赖真实 `gamescaling` 包，本地缺 `keymaps.py` 会全 skip，
   已在服务器 venv 实跑 8/8 通过。
4. FP8/量化、MTP 投机解码：MTP 权重经 safetensors 实测**不存在**（760 张量、MTP/draft 匹配 0）；
   FP8 需换权重重载，违反"不擅自替换模型"→ 均未做，仅记录约束。
5. CUDA Graph：启动日志确认 `cudagraph_mode=FULL_AND_PIECEWISE`（51 PIECEWISE + 35 FULL 捕获成功），
   已属低延迟最优；`--enforce-eager` 需重启生产才能对照，故无 A/B。

## 26. 最终推荐配置（2B / 4090，实测）

| 项 | 推荐值 | 依据 |
|---|---|---|
| 传输 | 新流式 relay（`frame_relay_stream.py`） | Relay 开销 +0.95ms，finish−g1 从 0.3ms 变 56.9ms |
| 客户端 | `act_stream()` + `IncrementalActionParser` | parity 20/20，ActionTTFT P50 298.8 vs 360.6 |
| 执行调度 | 策略 C（固定 33ms 片） | 零 overlap / 零 underrun / 零卡键 |
| 鼠标 | mode A（一次性整位移）或 B（六段平滑）按游戏手感选 | 实测两者 Observation Age 差 21ms，均 240/240 执行 |
| 历史 | `execution_receipt_enabled=True` | 下一轮看到真实执行；部分执行 8/8 可解析 |
| 推理参数 | `history_len=5, reasoning=True, temperature=1.0, max_tokens=256` | 与官方一致；256 足够（实测 31~34 tokens） |

**优化前后（同一 40 轮闭环）**：Observation Age P50 360.6 → 277.8ms（−82.8ms，−23%），
P95 573.6 → 287.5ms（−286.1ms，−50%）。

**剩余瓶颈**：① 首 token 前约 72ms 的 prefill/排队；② 隧道 RTT +318.6ms（C 链路）；
③ 2B 模型本身在长思考 trial 上 ActionTTFT 会到 ~500ms。

**下一阶段建议**：① 在带输入设备的机器上做受控真机 30 场景；② 修 `frame_relay` 上游
httpx 的 `trust_env` 与超时分级；③ 若需 9B 换 ≥48GB 卡；④ 隧道加 keepalive+重连窗口以消除 C 链路损耗。
