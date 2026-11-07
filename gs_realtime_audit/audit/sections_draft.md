# GameScaling 实时推理审计 · 主报告草稿（供 lead 合并）

> 本文件为子代理草稿，只写事实与可核实引用，不含最终结论性数字以外的臆测。所有数值均来自本地文件；引用路径为相对 `E:\BetterAGI` 的路径。凡未在本项目实测者均标注「需实测」。合并时请保留「需实测」标记。
> 数据来源：`gs_realtime_audit/audit/environment.json`、`gs_realtime_audit/audit/official_code/` 下各文件、`gs_realtime_audit/report/summary.md` 与 `gs_realtime_audit/report/summary.json`、`gs_realtime_audit/research/` 下两份调研文档。本子代理未联网、未连 GPU、未跑推理、未修改 benchmark/、report/、prototypes/、tests/ 下任何文件。

## 1. 环境信息

来源：`gs_realtime_audit/audit/environment.json`（`captured_utc = 2026-11-05T04:48:00Z`；`source = gs_ssh.py exec env_probe.py + ckptlog.sh + mtp.sh (read-only, no inference)`）。

### 1.1 server（完整表格）

| 字段 | 值 |
| --- | --- |
| ssh | `ssh -p 28390 root@i-1.gpushare.com (via 127.0.0.1:7897 HTTP CONNECT tunnel)` |
| model_endpoint_local | `http://127.0.0.1:8000/v1`（vLLM OpenAI API） |
| model_endpoint_external | `http://i-2.gpushare.com:47960/`（proxy to 8000） |
| hostname | `I2c248deb7900b01f91` |
| cpu | `INTEL(R) XEON(R) GOLD 6530, 128 vCPU (2 sockets x 32 cores x 2 threads)` |
| ram | 503 GiB total, 449 GiB available |
| gpu | NVIDIA GeForce RTX 5090, compute_cap 12.0（sm_120, consumer Blackwell GB202） |
| vram | 32607 MiB total，26464 MiB used by vLLM EngineCore (pid 11557)，26454 MiB compute |
| driver | **580.76.05** |
| nvidia_smi_power_draw_idle_W | 13.14 |
| power_limit_W | 575 |
| clocks_idle_MHz | sm 180 / memory 405 |
| temperature_C | 27 |
| cuda | no nvcc on host；**PyTorch 2.14.0+cu130**（CUDA 13.0） |
| python | vllm-env Python 3.11，torch 2.14.0+cu130，**vLLM 0.33.0**（2026-11-01 release） |
| gpu_util_idle_pct | 0 |
| gpu_mem_util_idle_pct | 0 |

**必写口径**：**RTX 5090 32GB**（32607 MiB，sm_120 消费级 Blackwell GB202）、**driver 580.76.05**、**PyTorch 2.14.0+cu130**、**vLLM 0.33.0**（Python 3.11）。

启动命令（`environment.json` 的 `serve_command` 原文）：

```text
/root/vllm-env/bin/vllm serve /hy-tmp/gs/model/GameScaling-9B-S3 --served-model-name gamescaling --port 8000 --tensor-parallel-size 1 --dtype bfloat16 --max-model-len 32768 --limit-mm-per-prompt '{"image": 8}' --gpu-memory-utilization 0.85 --trust-remote-code
```

启动日志：`/hy-tmp/gs/logs/vllm_gs.log`。由命令直接可读：**TP=1**、`--max-model-len 32768`、`--gpu-memory-utilization 0.85`、`--dtype bfloat16`、`--limit-mm-per-prompt '{"image": 8}'`（与 `gs_realtime_audit/audit/official_code/serve_vllm.sh:12-20` 一致，后者注释写 "8 images = 5 history frames + the current one, with headroom"）。

### 1.2 checkpoint（完整表格）

| 字段 | 值 |
| --- | --- |
| path | **`/hy-tmp/gs/model/GameScaling-9B-S3`** |
| config_json | `/hy-tmp/gs/model/GameScaling-9B-S3/config.json` |
| architectures | **`Qwen3_5ForConditionalGeneration`** |
| model_type | qwen3_5 |
| dtype | **bfloat16** |
| weights | **单文件 `model.safetensors`，18,819,722,392 bytes（约 18.8 GB，BF16）** |
| weight_files | model.safetensors, config.json, generation_config.json, chat_template.jinja, preprocessor_config.json, processor_config.json, tokenizer.json, tokenizer_config.json |
| text_config.hidden_size | 4096 |
| text_config.num_hidden_layers | 32 |
| text_config.num_attention_heads | 16 |
| text_config.num_key_value_heads | 4 |
| text_config.head_dim | 256 |
| text_config.vocab_size | 248320 |
| text_config.intermediate_size | 12288 |
| text_config.max_position_embeddings | 262144 |
| text_config.full_attention_interval | **4** |
| text_config.layer_types_32 | **linear_attention x3 + full_attention x1 repeated 8 times（24 GDN + 8 full）** |
| text_config.linear_conv_kernel_dim | 4 |
| text_config.linear_num_key_heads | 16 |
| text_config.linear_num_value_heads | 32 |
| text_config.linear_key_head_dim | 128 |
| text_config.linear_value_head_dim | 128 |
| text_config.mamba_ssm_dtype | float32 |
| text_config.mtp_num_hidden_layers | 1（仅为声明，见 1.2.1 证伪） |
| text_config.mtp_use_dedicated_embeddings | false |
| vision_config.depth | 27 |
| vision_config.hidden_size | 1152 |
| vision_config.out_hidden_size | 4096 |
| vision_config.num_heads | 16 |
| vision_config.patch_size | 16 |
| vision_config.spatial_merge_size | 2 |
| vision_config.temporal_patch_size | 2 |
| vision_config.num_position_embeddings | 2304 |

**必写口径**：checkpoint `/hy-tmp/gs/model/GameScaling-9B-S3`（`Qwen3_5ForConditionalGeneration`，BF16 单文件 18.8GB，**32 层混合注意力 24 GDN + 8 full attention**，`full_attention_interval=4`）。

#### 1.2.1 MTP 权重证伪（重点，环境探针实测）

- `config.json` 中 `text_config.mtp_num_hidden_layers=1` **只是声明**。`environment.json` 的 `checkpoint.note` 原文记录首次扫描失败：`"MTP weight-key scan FAILED on system python (ModuleNotFoundError: No module named 'safetensors'); mtp_num_hidden_layers=1 in config.json is a declaration, NOT yet verified against actual tensor keys. Re-scan with /root/vllm-env/bin/python."`
- 随后用 **`/root/vllm-env/bin/python + safetensors.safe_open`** 重扫 `model.safetensors`（`checkpoint.mtp_weight_verification`）：

| 扫描项 | 结果 |
| --- | --- |
| total tensors | **760** |
| MTP / draft / nextn / eh_proj / enorm / hnorm 相关张量 | **0** |
| layer indices | **0..31**（32 层，max 31） |
| `layers.32` 键 | **空（`[]`）** |
| top-level prefixes | **model = 759 + lm_head = 1** |

- `conclusion` 原文：`"config.json text_config.mtp_num_hidden_layers=1 is ONLY a declaration; NO actual MTP/draft/nextn tensors exist in model.safetensors. The deployed checkpoint has NO usable MTP head — confirms task's warning not to assume MTP exists. Any MTP speculative decoding would require separate weights (not present) and cannot be enabled on this checkpoint without retraining/replacing weights."`
- **结论**：该 checkpoint **没有可用 MTP 头**；任何 MTP 投机解码都需要额外权重（不存在），在本 checkpoint 上不可启用。

### 1.3 runtime_config_from_startup_log（完整表格）

| 字段 | 值 |
| --- | --- |
| speculative_config | **None（no spec decode / MTP / ngram / suffix / eagle enabled）** |
| quantization | **None（no FP8/W8A8；dtype bfloat16）** |
| enforce_eager | false |
| enable_prefix_caching | **true** |
| enable_chunked_prefill | **true** |
| cudagraph_mode | **FULL_AND_PIECEWISE** |
| max_cudagraph_capture_size | 512 |
| cudagraph_capture_sizes | 1,2,4,8,16,...,512（**51 PIECEWISE + 35 FULL captured at startup**） |
| cudagraph_mm_encoder | false |
| compile_mm_encoder | false |
| attention_backend | **FLASH_ATTN**（out of FLASH_ATTN/FLASHINFER/TRITON_ATTN/FLEX_ATTENTION） |
| vit_attention_backend | **FLASH_ATTN**（cuda.py:567, mm_encoder_attention.py:372） |
| gdn_prefill_kernel | **FlashInfer** GDN prefill kernel（qwen_gdn_linear_attn.py:300） |
| mamba_cache_mode | **align**（config.py:766，prefix caching enabled for Qwen3_5ForConditionalGeneration） |
| kv_cache_layout | LBNHC（utils.py:381） |
| kv_cache_size_tokens | **182706** |
| kv_cache_memory_GiB | **6.48** |
| max_concurrency_32768_tokens | 5.58 |
| gpu_memory_utilization_configured | **0.85** |
| gpu_memory_utilization_effective_without_graph_profiling | 0.836 |
| weights_plus_activation_GiB | 20.17 |
| cudagraph_pool_GiB | 0.19 |
| histogram_buckets | custom_histogram_buckets=None（default min bucket 0.3s — sub-300ms SLO NOT measurable from histogram buckets；use _sum/_count deltas instead） |
| per_request_spec_decode_metrics | none |
| structured_outputs_backend | auto |
| reasoning_parser | **空（enable_in_reasoning=False）** |
| kernel_config.rms_norm | native |
| kernel_config.fused_add_rms_norm | native |
| kernel_config.gelu_and_mul_sparse | triton, native |
| kernel_config.moe_backend | auto |
| kernel_config.linear_backend | auto |
| kernel_config.enable_flashinfer_autotune | true |

**必写口径**：TP=1、`max_model_len=32768`、`gpu_memory_utilization=0.85`、`enable_prefix_caching=true`、`enable_chunked_prefill=true`、`cudagraph_mode=FULL_AND_PIECEWISE`（启动日志 **51 PIECEWISE + 35 FULL 全部捕获成功，不是 enforce_eager**）、`attention_backend=FLASH_ATTN`、`vit_attention_backend=FLASH_ATTN`、`gdn_prefill_kernel=FlashInfer`、`mamba_cache_mode=align`、**kv_cache 6.48GiB / 182706 tokens**、`speculative_config=None`、`quantization=None`、reasoning_parser 空。

### 1.4 startup_log_findings（逐条要点，含原文关键串）

1. `"Mamba cache mode 'align' for Qwen3_5ForConditionalGeneration by default when prefix caching enabled"` —— 开启 prefix caching 时 Qwen3_5ForConditionalGeneration 默认 align。
2. `"torch.compile AOT loaded from cache in 1.32s; init engine 65.84s"`。
3. `"Capturing CUDA graphs (PIECEWISE) 51/51 and (FULL) 35/35 succeeded => FULL CUDA graph IS active, not --enforce-eager"` —— **51 PIECEWISE + 35 FULL 全部捕获成功，不是 enforce_eager**。
4. `"FlashInfer precompiled kernels missing warning (flashinfer-cubin/jit-cache not installed), managed autotune cache attached"`。
5. `"Triton kernel JIT compilation DURING inference at 11:07:25: store_cache_checkpoints_kernel — causes latency spike, warmup does not cover this shape"` —— **Triton JIT 在推理中编译 `store_cache_checkpoints_kernel` 造成延迟尖峰**（warmup 未覆盖该 shape）。【需实测】该尖峰的出现频率与幅度。
6. `"serving.py:291 chat template requires system-first ordering; inline system messages would miss prefix cache (official client uses leading system prompt => OK)"` —— System Prompt 必须 leading。
7. `"Multi-modal warmup 19.469s; encoder cache budget 16384 tokens profiled with 1 image of max feature size"` —— **mm warmup 19.469s**。

### 1.5 other_gpu_processes 与其它环境字段

- **other_gpu_processes**：`"none (only vLLM EngineCore pid 11557 on GPU; frame_relay uvicorn pid 12242 is CPU-only on 127.0.0.1:8010)"` —— **其它 GPU 进程只有 vLLM EngineCore（frame_relay uvicorn 是 CPU-only 在 127.0.0.1:8010）**。
- **chat_template_notes**：Qwen3_5 的 `chat_template.jinja` 会给无 think 标签的 assistant turn 注入 `<think>\n\n</think>\n\n`；`add_generation_prompt` 让推理由模型填 thinking 内容；System message 必须 leading；与官方 `policy.py` 的 prefill / `continue_final_message` 设计一致。
- **official_client_defaults**：`environment.json` 内的副本（history_len=5, history_think=0, image_size=null, image_format=jpeg, jpeg_quality=90, resample=bilinear, reasoning=true, temperature=1, max_tokens=2048, stop=[<|action_end|>], lenient_parse=false, timeout=120, base_url/model/api_key）；逐项精确默认值见本草稿 §2.2（源码 `policy.py:52-76`，其中 temperature/timeout 为 `1.0` / `120.0`）。
- **preflight_findings**（原文要点）：7 configs 全部 HTTP 200；guided regex 反例 parsed=False/truncated=True/2048 tokens/23s；**stream 逐步到达 ~10.6ms/token**；unique cache_salt 给出干净的 prefix-off 对照；reasoning=True 输出无可见 think（`<think>` 被 prefill、模型从空的闭合 think 继续）。
- **chart_tooling**：本机 bundled python 无 matplotlib（pandas 3.0.1 / numpy 2.3.5 / PIL 12.3.0）；服务端 python 有 requests 2.32.3、PIL、numpy 2.2.5，无 matplotlib/pandas/aiohttp；计划用 PIL 或纯 SVG/HTML 出图，不 pip install matplotlib。

## 2. 当前架构分析

依据 `gs_realtime_audit/audit/official_code/policy.py`、`action.py`、`prompt.py`、`system_prompt_template.txt`、`serve_vllm.sh`、`_all.txt`、`_server_scripts.txt` 的真实代码。

### 2.1 阶段 0 客户端核对项（逐条）

| 核对项 | 结论 | 代码依据 | 引用 |
| --- | --- | --- | --- |
| requests.Session / Keep-Alive | **是，持久连接** | `GameScalingPolicy.__init__` 创建 `self._session = requests.Session()`；`act()` 复用它 POST、`health_check()` 复用它 GET | `policy.py:174, 185, 208` |
| 同步 POST + 等待完整 JSON | **是** | `act()` 中 `self._session.post(..., json=body, timeout=self.cfg.timeout, headers=...)` **无 `stream` 参数**，随后 `payload = r.json()`；同步阻塞至完整响应体 | `policy.py:208-213` |
| 等完整 JSON 才解析 | **是** | `finalize(text, finish, self.cfg)` 与 `parse_action(text, lenient=...)` 都在 `r.json()` 之后；`Decision` 全部完成后才返回 | `policy.py:222-229` |
| 逐 Token / 逐动作组回调 | **无** | `act()` 无 `stream`、无回调参数，只对最终 `text` 做一次 `parse_action`。（`gs_realtime_audit/prototypes/streaming_client.py` 是独立原型，提供 `on_mouse_header`/`on_group`/`on_done`，不属于官方 `policy.py`；此处只读引用） | `policy.py:202-229` |
| 可取消请求 | **无** | `act()` 无取消/中断接口；仅 `timeout=120.0` 整体超时，非中途取消 | `policy.py:75, 209` |
| KV Cache 复用 | **客户端自身无显式控制**；依赖服务端 `enable_prefix_caching=true`（`environment.json` runtime_config），且 System Prompt 必须 leading（`serving.py:291`）。官方 `messages()` 把 `{"role":"system", ...}` 放在 `out` 首位，满足 leading 条件 | `policy.py:192`、`environment.json` startup_log_findings 第 6 条 | `policy.py:192`；`audit/environment.json` |
| 每次是否重复编码全部历史图片 | **否** —— history 里存的是上一轮已编码的 data URL 字符串 `Turn.image_url`，只把字符串重复放进 messages；每轮只编码当前帧一次（`url = encode_image(image, self.cfg)` 只在 `act()` 调用一次）。**但请求体仍携带全部 H+1 张图的 base64**：history_len=5 → 6 张、约 253KB | `policy.py:195, 199, 203, 225`；实测 `n_images=6`、`image_url_bytes=253107` | `policy.py:195-225`；`report/summary.json:658-679` |
| 历史动作是预测还是实际执行 | **是模型自己上一轮的输出（预测），不是执行器实际执行结果**；`act()` 把 `parse_action` 得到的 `action.history_text` 存入 `Turn(url, reasoning, action.history_text)`；`Action.history_text` 在 `parsed=True` 时返回 `"{ACTION_START}{self.body}{ACTION_END}"`，未解析时返回显式 `NOOP_ACTION_TEXT`；**没有任何执行器回写** | `policy.py:225`、`action.py:37, 95-105` | `policy.py:225`；`action.py:37, 95-105` |

补充事实：

- **history_len=5 → 最多 6 张截图**（5 历史 + 1 当前）：`messages()` 对 `self._history` 每个 Turn 追加一个 user_turn（含图），再追加当前帧 user_turn（`policy.py:193-200`）；`act()` 末尾 `while len(self._history) > self.cfg.history_len: popleft()`（`policy.py:226-227`）把历史上限锁为 5。实测 `n_images=6`、`image_url_bytes=253107`（`gs_realtime_audit/report/summary.json:658-679`，baseline_stream，100 trials 的 min/max/p50 全部相同），与之一致。`serve_vllm.sh:10-11` 注释 "8 images = 5 history frames + the current one, with headroom" 对应 `--limit-mm-per-prompt '{"image": 8}'`。
- 历史动作与上下文的可能漂移：因 `history` 存的是模型预测而非实际执行结果，上下文与真实世界状态可能不一致（事实陈述，闭环影响【需实测】）。

### 2.2 PolicyConfig 默认值表（`policy.py:52-76`，15 个字段 + 1 个派生属性，共 16 行）

| # | 字段 | 默认值 | 行号 |
| --- | --- | --- | --- |
| 1 | `base_url` | `"http://127.0.0.1:8000/v1"` | 54 |
| 2 | `model` | `"gamescaling"` | 55 |
| 3 | `api_key` | `"EMPTY"` | 56 |
| 4 | `history_len` | 5 | 58 |
| 5 | `history_think` | 0 | 60 |
| 6 | `image_size` | `None` | 62 |
| 7 | `image_format` | `"jpeg"` | 63 |
| 8 | `jpeg_quality` | 90 | 64 |
| 9 | `resample` | `"bilinear"` | 65 |
| 10 | `reasoning` | `True` | 68 |
| 11 | `temperature` | `1.0` | 69 |
| 12 | `max_tokens` | 2048 | 70 |
| 13 | `stop` | `["<|action_end|>"]`（`field(default_factory=lambda: [ACTION_END])`，`ACTION_END = "<|action_end|>"`） | 71 |
| 14 | `lenient_parse` | `False` | 74 |
| 15 | `timeout` | `120.0` | 75 |
| 16 | `prefill`（派生 `@property`，非字段） | `THINK_PREFILL = THINK_OPEN = "<think>"`（reasoning=True）或 `NO_THINK_PREFILL = "<think>\n\n</think>"`（reasoning=False） | 77-79 |

注：`environment.json` 的 `official_client_defaults` 副本把 `temperature`/`timeout` 记为整数 `1`/`120`，源码为浮点 `1.0`/`120.0`；`image_size` 副本为 `null` 即 `None`。

### 2.3 官方输出协议与 6×33ms 语义（`action.py` 顶部注释 + `_build`）

- 协议形态（`action.py:5` 原文）：`<|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>`；训练 Prompt 的 `**Output Format**` 段给出带 `<think></think>` 的两行示例（`prompt.py:35-42`、`system_prompt_template.txt:6-10`）。
- `X Y` 是整段 200ms 的鼠标位移（屏幕千分比，clamp ±1000，X>0 右、Y>0 下）；`Z` 是滚轮 clicks（clamp ±5，Z>0 上）（`action.py:7-9, 34-35`）。
- `g1..g6` 是 **6 个 33ms 子步**，每组列出该子步按住的键/按钮（最多 4 个）；连续组出现同一键视为持续按住；空组释放全部（`action.py:10-12`）。常量：`CHUNK_MS=200.0`、`CHUNK_STEPS=6`、`STEP_MS=200/6≈33.33ms`、`MAX_KEYS_PER_STEP=4`、`MOUSE_LIMIT=1000.0`、`SCROLL_LIMIT=5.0`（`action.py:30-35`）。
- 键词表是训练用的 **74-token 词表**（大小写敏感；`zero..nine` 为数字行键、`One..Twelve` 为 F1..F12；`action.py:39-53`）。`NOOP_ACTION_TEXT = f"{ACTION_START}0 0 0 ; ; ; ; ; ; {ACTION_END}"`（`action.py:37`）。
- `_build()`（`action.py:121-134`）：clamp X/Y/Z；按 `;` 切组，每组 token 只保留 `KEY_VOCAB` 中的前 4 个，其余进 `dropped`；不足 6 组补空组。
- 解析：`parse_action` 严格模式只读最后一个 `</think>` 之后的 tail（`split_reasoning`、`_parse_strict`；`action.py:68-81, 137-157, 196-213`），因为 thinking 模型会「把格式引用给自己」。
- 请求体：`build_request()` 追加 assistant prefill 消息、`continue_final_message=True`、`add_generation_prompt=False`、`skip_special_tokens=False`（`policy.py:129-144`）；`finalize()` 负责补回 prefill 与 `ACTION_END`（`policy.py:147-164`）。
- `prompt.py:11-13` 原文：`"Copy the strings, do not paraphrase them: the checkpoints key their output tags off the exact Output Format section."` —— Output Format 是训练时精确字面量，改动即破坏已训练分布。

### 2.4 frame_relay.py 的整体缓冲（阶段 6 网络实验的关键假设）

`gs_realtime_audit/audit/official_code/_server_scripts.txt:252-292` 的 `/v1/chat/completions` 原文两行（**必须原文引用**）：

```python
resp = await _client.post(UPSTREAM + "/v1/chat/completions",
                          content=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
```

```python
return Response(content=resp.content, status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"))
```

`/v1/models` 同样用 `resp.content`（`_server_scripts.txt:113-121`）：

```python
resp = await _client.get(UPSTREAM + "/v1/models")
...
return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
```

**解释**：`_client` 是 `httpx.AsyncClient`（`_server_scripts.txt:99, 105`），`await _client.post(...)` 会把上游完整响应体读入 `resp.content`；`Response(content=resp.content, ...)` 一次性返回。因此**无论上游 vLLM 是否 SSE，客户端都要等完整响应体，chunk 不会逐步到达** —— 这是**整体缓冲**。`/v1/models` 同样用 `resp.content`。这是阶段 6 网络实验的关键假设。

【需实测】本地 loopback 的 preflight 已见 `"stream逐步到达 ~10.6ms/token"`（`environment.json` preflight_findings），但经外部出口 `http://i-2.gpushare.com:47960/` 的链路是否呈现上述整体缓冲、SSE chunk 的到达分布如何，需阶段 6 网络实验实测；本地 loopback 数据不能代表外网链路。

### 2.5 外部出口与服务端端口链

- **外部出口 `http://i-2.gpushare.com:47960/` 是代理**（`environment.json` server.model_endpoint_external: "proxy to 8000"）；**服务端本机 `127.0.0.1:8000` 是 vLLM**（server.model_endpoint_local: `http://127.0.0.1:8000/v1`）。
- 中转链（`_server_scripts.txt`）：`EXPOSE_8080.md:754` 记 47960 → 实例 8080；`fwd8080.py:582-583` 监听 `0.0.0.0:8080`、`TARGET = ("127.0.0.1", 8010)`；frame_relay uvicorn 是 CPU-only 在 `127.0.0.1:8010`（`environment.json` other_gpu_processes，pid 12242）；`frame_relay.py:83` `UPSTREAM = os.environ.get("FRAME_RELAY_UPSTREAM", "http://127.0.0.1:8000")`。
- 【需实测/需核实】`_server_scripts.txt:752` 的说明文字写 "转发到 127.0.0.1:8000"，与 `fwd8080.py:583` 的 `8010` 不一致；实际端口链需核实。

## 8. 输出协议评估

> **本节仅为探索性评估。B/C/D 均为探索性方案，不得直接替换正式模型。** A 是当前已训练、已兼容的正式协议，仅作对照。
> 基线为 batch1 实测（`gs_realtime_audit/report/summary.json` 的 `baseline_stream`，n=100，p50；`gs_realtime_audit/report/summary.md` 的 baseline_stream 行，仅引用、不改写结论）。

| 基线标记 | 含义 | p50 (ms) |
| --- | --- | --- |
| T9_action_start | `<|action_start|>` 到达 | **316.718** |
| T10_mouse_header_numbers | `X Y Z` 三个数到达 | **357.581** |
| T10b_header_sep | 头部 `;` 到达 | 367.702 |
| T11_g1 | 第一组 g1 完成（`;` 到达） | **392.027** |
| T16_g6 | 第六组 g6 完成 | **498.615** |
| T17_request_complete | 请求完成 | 499.036 |

`report/summary.md` baseline_stream 行：E2E P50 499.0 / E2E P95 567.6 / TTFT P50 186.8 / ActionTTFT P50 392.0 / Prefill P50 63.1 / Decode P50 350.6 / g1→2 P95 32.2。

### 「组优先」对首组延迟的理论推算（**理论推算、不是实测**）

当前 g1 之前必须解码 `<|action_start|>` + `X Y Z` + 头部 `;`。若把六组移到头部之前（B 方案），g1 理论上可提前：

- **绝对上界**（按各标记边际 p50 相减）：T11_g1 − T9_action_start = 392.027 − 316.718 = **75.309ms**（假设 g1 组本身零解码时间的理想上界）。
- **按逐请求配对 p50 的头部段**：`action_start_to_header_ms` 42.607ms + `header_to_g1_ms` 21.397ms = **64.004ms**（`report/summary.json:682-703`）。
- **保守下界**：`header_to_g1_ms` 配对 p50 = 21.397ms；按边际 p50 差 T11_g1 − T10b_header_sep = 392.027 − 367.702 = 24.325ms。
- 即便按 75.309ms 的绝对上界，g1 仍约在 316.718ms，**仍高于 `prompt.py:28` 要求的 200ms 决策窗口**。
- 该推算只改变顺序、不改总 token 数，因此 T16_g6 / E2E 预期不变（【需实测】）；收益仅在 g1..g5 的到达时间前移，而鼠标头 `X Y Z` 被推到 g6 之后。
- 以上全部为**理论推算、不是实测**，且只有重训模型改变输出分布后才可能发生；【需实测】需独立微调 + 质量对照实验验证。

### A 原始 `X Y Z ; g1..g6`（正式协议，对照）

- **协议形态**：`<think></think><|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>`（`action.py:5`、`prompt.py:35-42`、`system_prompt_template.txt:8-10`）。
- **动机**：已训练、已兼容官方 `parse_action`；**分号天然可做流式分隔**（Lumine 调研 §1.2 原文 "分隔符即边界……每出现一个分号 = 一个完整 chunk"，`gs_realtime_audit/research/lumine_streaming_spec_decoding.md`），可在零模型改动下做「增量解析 + 分号 dispatch」。
- **对已训练 Output Format 的破坏风险**：无（0）；改它才产生风险。
- **对首组延迟的预期影响**：基线 T11_g1 392.027ms；不改协议即可通过流式增量解析把「g1 可用」提前到其 chunk 到达时刻，但 chunk 到达时间本身由 decode 决定（【需实测】流式执行下 g1..g6 到达时间与 200ms 窗口的关系，用 p50/p99 而非均值）。
- **Token 数影响**：不变（沿用当前已训练格式）。
- **训练数据/微调成本**：0。
- **执行器适配成本**：0（现有 `parse_action` / `_build` 直接可用）。
- **风险**：当前 T11_g1 392.027ms 已超 200ms 窗口（实测事实）；流式执行引入约 200ms stale observation 为【文献结论】（Lumine 调研 §1.5），本项目闭环 overshoot / 导航过冲影响【需实测】。

### B `g1..g6 ; X Y Z`（探索性）

- **协议形态**：六组提前，鼠标头 `X Y Z` 置于 `<|action_end|>` 之前末尾（组优先）。
- **动机**：键盘更早可用；理论上把 g1 提前最多 75.309ms（**理论推算、不是实测**）。
- **对已训练 Output Format 的破坏风险**：**高** —— 改动 `**Output Format**` 的精确字面量（`prompt.py:11-13` 明确 "Copy the strings, do not paraphrase"），改变已训练输出分布；`_parse_strict` 要求 `parts[0]` 是三个数字（`action.py:146-157`），现有解析器会直接 parse fail（baseline_stream 的 `n_parse_fail=0` 是 A 的实测，B 无实测）。
- **对首组延迟的预期影响**：理论最多提前 75.309ms（绝对上界），g1 仍约 316.718ms > 200ms；**理论推算、不是实测**；【需实测】。
- **Token 数影响**：总量基本不变（顺序重排），`;` 位置改变；每组分隔语义不变，decode 总步数预期不变（【需实测】）。
- **训练数据/微调成本**：需重做 Prompt Output Format + 全量 SFT/微调以改变输出顺序分布，并做独立质量对照实验；成本高。
- **执行器适配成本**：需改 `parse_action`（头部在末尾）；流式执行器须缓存六组、等末尾才拿到 `X Y Z`，与「分号即 dispatch」的流式执行冲突，鼠标执行时机被推迟。
- **风险**：输出分布偏移、解析失败率上升、动作质量下降；**探索性方案，不得直接替换正式模型**。

### C 鼠标也六段分块 `dx1 dy1 keys1 ; ...`（探索性）

- **协议形态**：每个 33ms 子步各自带鼠标微位移，6 段各自 `dx dy keys`。
- **动机**：鼠标位移与键盘子步对齐，消除 200ms 聚合位移。
- **对已训练 Output Format 的破坏风险**：**最高** —— 完全改写 Output Format、标签语义与 `Action.mouse` / `Action.steps` 数据结构（`action.py:84-118, 121-134`）。
- **对首组延迟的预期影响**：不仅不能提前，Token 增多会拉长 decode，g1 预期更晚（【需实测】，无本项目数据）。
- **Token 数影响**：**显著增多** —— 由 A 的 1 组 3 个数字 + 6 组按键，变为 6 组 ×（2~3 个数字 + 按键）；decode 变长，总生成时间预期上升（baseline 组间 `g1_to_g2_ms` p50 21.346ms，`report/summary.json:704-713`；【需实测】）。
- **训练数据/微调成本**：**最高** —— 现有训练标签只有 200ms 聚合位移 `X Y Z`，没有逐 33ms 子步的鼠标微位移，需重新采集/生成全部训练标签并重训。
- **执行器适配成本**：需按 6 段分别应用鼠标微位移；现有语义是整块 200ms 平滑执行（`action.py:7-9` 注释）。
- **风险**：逐子步鼠标标签不可得、质量不可控、decode 变长抵消收益；**探索性方案，不得直接替换正式模型**。

### D 动态长度 2/6/12 steps（探索性）

- **协议形态**：每轮动作组数可变（2/6/12），执行器按变长窗口执行。
- **动机**：简单局面少出组以省 decode，复杂局面多出组。
- **对已训练 Output Format 的破坏风险**：**高** —— 破坏固定 6×33ms 契约（`prompt.py:28-32`、`action.py:30-33, 132-133` 的 `CHUNK_STEPS=6` 与补空逻辑）。
- **对首组延迟的预期影响**：长度可变导致 g1 时间分布变化，短序列可能更早、长序列可能更晚（【需实测】，无本项目数据）。
- **Token 数影响**：随长度线性变化（2 组最少 / 12 组最多），E2E 方差增大。
- **训练数据/微调成本**：需支持可变长度的训练数据与 loss masking，重新构造标签并重训。
- **执行器适配成本**：执行器需可变长调度，并把 2/6/12 步映射到 33ms 时序与 200ms 窗口；现有 `parse_action` 固定补/截到 6 组（`action.py:127-133`）。
- **风险**：时序契约破坏、质量与延迟方差增大；**探索性方案，不得直接替换正式模型**。

### 结论倾向

- **当前不建议改协议或重训模型**（依据任务书原则 1/2/3；本子代理未拿到任务书原文，原则原文表述需 lead 按任务书补齐）。
- 先把**无可训练改动**的优化做完：客户端流式增量解析 + 分号 dispatch（Lumine 调研 §4.3 阶段 0）、prefix caching 核对、网络链路（§2.4 整体缓冲）与 vLLM 配置核查；并以 batch1 的 p50/p95 基线作对照。
- 若未来确要改协议，必须有**独立的微调 + 质量对照实验**（动作质量、解析成功率、闭环任务成功率、overshoot 率），用 p50/p99 而非均值评估。
- 【需实测】B/C/D 的 Token 数、decode 时长、g1/g6 延迟、解析成功率与闭环动作质量。
- 本节所有「组优先」提前量均为**理论推算、不是实测**。

## 附录. Lumine / vLLM 优化研究综述

本附录基于 `gs_realtime_audit/research/lumine_streaming_spec_decoding.md` 与 `gs_realtime_audit/research/vllm_latency_optimization.md`。每条结论标注【文献结论】或【本项目实测】；两份调研文件内的 URL 为其原始出处，本子代理未联网、未复核其外部来源、未连 GPU、未跑推理。

### A.1 Lumine 的 Streaming Action Execution 是什么

- 【文献结论】Lumine（ByteDance Seed，arXiv:2511.08892，骨干 Qwen2-VL-7B-Base，5Hz 帧 / 30Hz 动作）用 `<|action_start|>` / `<|action_end|>` 包住 `ΔX ΔY ΔZ ; K1 ; ... ; K6`，6 组各 33ms；"分隔符即边界：每出现一个分号 = 一个完整 chunk"（`lumine_streaming_spec_decoding.md` §1.2）。与本项目 `<think></think><|action_start|>X Y Z ; g1..g6<|action_end|>` 的 chunk 结构、分号语义、33ms 组时长一一对应（§1.2 原文 "格式同构是本项目可移植性的最大有利条件"）。
- 【文献结论】论文 §6.2 原文（该报告 §1.3 已核实引用）：`"Once a complete chunk is produced, signaled by a terminating semicolon, it can be executed immediately without waiting for the full sequence to finish."` 即增量解析器每遇终止分号即派发一组，不等 6 组全解码、不等 `<|action_end|>`、不等 HTTP 响应整体结束；把 200ms 硬约束放松为每 chunk 33ms（§1.3-1.4）。论文延迟分解表（§1.4，已核实精确值）：decode 每 token 3.1ms、"First action chunk w/o reasoning 113.9ms"、带 reasoning "234.0ms"（已超 200ms 预算）、平均 chunk 1.8 tokens / 1.02 forward steps。
- 【文献结论】代价：异步执行引入约 200ms stale observation，无 history 模型会鼠标 overshoot、导航转向过冲；论文靠把最近最多 20 步（观测, 动作）放进上下文补偿，而非重新观测或取消重规划（§1.5 原文引用）。
- 【文献结论】25.3× 是 Figure 7 `"The overall optimization yielding a 25.3× speedup compared with the baseline"` 的端到端叠加（通信 JPEG+Base64/持久 TCP、prefill 历史 KV 复用 + StreamingLLM + attention sink、紧凑动作空间 + 无 draft 投机解码、TP=4×H20/W8A8 SmoothQuant/定制 all-reduce/单个 CUDA graph 融合 forward+rejection sampling/图像预处理上 GPU），**不是「流式 + 投机解码」单独带来的**；RTX 5090 单卡不可直接套用（§0.3）。
- 【文献结论】论文 ≠ 仓库：`zlc1004/Lumine` 是第三方复现（README: "Attempted implementation of Lumine-Agent by arXiv:2511.08892."），其 serve 脚本是普通 vLLM + `--enforce-eager`、无 `speculative_config`、无 guided/grammar、无分号分块解析；Runner 是 `"one latest-only slot"` 同步串行丢帧，与论文流式执行相反（§0.1、§1.6）。

### A.2 Grammar-aware / 语法约束投机解码是什么

- 【文献结论】Lumine 的 "draft-model-less speculative decoding"：输出格式是确定性状态机 —— `ΔX`/`ΔY` 后必为空格，`ΔZ` 与 `K1..K5` 后必为分号，`K6` 后必为 `<|action_end|>`；这些位置下一个 token 无熵（zero-entropy / forced token），用一个 stage 状态变量即可 100% 正确「猜」出分隔符作为 draft token，再用标准拒绝采样（Leviathan et al. 2023）保证输出分布与不用投机时一致（§2.1，论文 §6.2 原文）。本质等同 llguidance 的 fast-forward / jump-forward（`"the speculation is 100% correct"`）。论文实测 8.4 tokens 只需 4.7 forward steps、平均 chunk 1.8 tokens 只需 1.02 forward steps（§1.4、§2.1）。
- 【文献结论】vLLM 社区版本：PR #47885 "Grammar-aware draft token sampling for structured outputs"（2026-07-07，jmamou/Intel）在 worker 维护 shadow `xgrammar.GrammarMatcher` 约束 draft logits；CPU Xeon 8580 TPOT 434.2→398.9ms（−8.9%）、RTX A6000 101.3→92.9ms（−9.1%）；`"Top-K=4 achieves 100% coverage of grammar-critical positions"`（§2.2）。收益约 9% TPOT，不是数量级提升。
- 【文献结论】**组合缺口（关键结论）**：Issue #62130 原文 `"Speculative decoding and grammar-constrained sampling (structured_outputs, guided decoding) do not compose in vLLM today."`（状态 Open）；v0.33.0 的 `speculative_decoding` 正文 "grammar" 出现 0 次、`structured_outputs` 文档的 "speculative" 只在侧栏导航；0.33 发布说明的投机解码改动（DFlash/DSpark、FLy）不含 grammar-aware（§2.3、§3.3）。已知问题：#52620（HTTP 500）、#55313（XGrammar desync trailing spaces）、#11484（xgrammar crash，closed as not planned）；PR #43424 指出 reasoning 结束边界那一步 verifier 的 bonus token 没有语法掩码会触发 FINISHED_ERROR（§2.3）。
- 【文献结论】分布一致性陷阱：arXiv:2605.07698 指出 local mask + Leviathan rejection + rollback 采样的是 locally projected 分布而非 grammar-conditional，Dyck 文法 + Qwen3-8B 上 TV gap 可达 0.996；控制任务必须做闭环 A/B，不能只看 TPOT（§2.4、§4.2）。

### A.3 与本项目协议的可移植 / 不可移植之处

**可移植**

- 格式同构：确定性分隔符 `;` 与 `<|action_start|>` / `<|action_end|>` 适合 grammar-constrained / 固定模板 token 预测（§1.2、§2.1、§4.1 可行性矩阵「分号分块 + 流式增量执行：高」）。
- 零模型改动：客户端流式增量解析 + 分号 dispatch（§4.3 阶段 0）；`gs_realtime_audit/prototypes/streaming_client.py` 已有 `on_mouse_header`/`on_group`/`on_done` 增量回调原型（只读引用，不属于官方 `policy.py`）。

**不可移植 / 本部署约束（【本项目实测】，来自 `gs_realtime_audit/audit/environment.json`）**

- **checkpoint 无可用 MTP 头**：见 §1.2.1 的 MTP 权重证伪（760 tensors、MTP 相关 0、layer indices 0..31、`layers.32` 空、top-level model=759 + lm_head=1）。
- `runtime_config_from_startup_log.speculative_config = "None (no spec decode / MTP / ngram / suffix / eagle enabled)"`、`quantization=None`、`per_request_spec_decode_metrics="none"`、`structured_outputs_backend="auto"`、reasoning_parser 空。
- **vLLM 0.33.0**（`environment.json` server.python）。【文献结论】MTP 在 0.33 只对 "model families that support MTP in vLLM" 生效，示例为 XiaomiMiMo/MiMo-7B-Base；`config/speculative.py` 的 `MTPModelTypes` 含 `qwen3_5_mtp` 等，但**需要 checkpoint 自带原生 MTP 头/权重**（`vllm_latency_optimization.md` §3.2、`lumine_streaming_spec_decoding.md` §3.2）。本 checkpoint 无该权重 → **MTP 投机解码不可启用**。
- 边界 token 必须是专用 token ID：structured_outputs 要用 `{"type":"token","token":"</think>"}` 而非 `const_string`，并用 `return_token_ids=true` 核对，否则会拼出「看起来一样但 ID 不同」的文本导致边界不触发（`lumine_streaming_spec_decoding.md` §3.3、§4.2）。
- 硬件/系统差距：本机 RTX 5090 单卡 sm_120、TP=1（`environment.json`），与论文 TP=4×H20 + W8A8 + 定制 kernel 的系统级优化差距大；论文 3.1ms/token、113.9ms 首块等数值**不可直接套用**（§4.1 明确 "33 ms / 200 ms 硬实时预算：需实测"，§4.2 风险 6）。

**Grammar-aware 在本部署的可行路径与代价（【文献结论】+ 本部署事实；落地收益【需实测】）**

1. 【文献结论】vLLM 0.33 的 `custom_class` proposer（Experimental，"Bring your own proposer class"）实现 stage 状态机：ΔX/ΔY 后 draft 空格、ΔZ/g1..g5 后 draft `;`、g6 后 draft `<|action_end|>`，保持 `rejection_sample_method="standard"`（§3.2、§4.3 阶段 3）。代价：实验接口、V1/V2 runner 差异、必须验证与专用 token ID 对齐、分布一致性。
2. 【文献结论】退路：llguidance / xgrammar 的 fast-forward forced tokens，需专门验证 canonical tokenization（non-canonical forcing 会扭曲后续 token 分布，§2.1）。
3. 【文献结论】只单独启用 `structured_outputs`（xgrammar/guidance 后端）的 regex/grammar 锁死格式，**暂时不要同时开 spec decode**，避开 0.33 组合缺口（§4.3 阶段 1）。LM Format Enforcer 在 0.33.0 已 deprecated（§3.3）。
4. 【文献结论】低成本投机先试 `method="ngram"`（零额外权重）并用 Per-Request Acceptance Metrics（0.33 新增、默认 none、仅 n==1）记录 `mean_acceptance_length` 与 TPOT；但 GameScaling 动作是数值+按键组合，重复度可能低，且无 logits 的 proposer 与 grammar 组合最差（issue #62130：84.9% drafted tokens 被 FSM 拒绝、unmasked EAGLE3 "0.97× slower than no speculation"）（§2.3、§3.4、§4.1、§4.3 阶段 2；`vllm_latency_optimization.md` §3.4）。
5. 【文献结论】代价小结（`lumine_streaming_spec_decoding.md` §2.6 表）：固定分隔符 draft 在 vLLM 0.33 **无现成实现**；spec-decode × structured-outputs **当前不可用（低可行性）**；单独 grammar 有现成 `structured_outputs`；所有投机路径都需额外验证开销、接受率与分布一致性。TensorRT-LLM 已实现 guided+spec 组合可作「将来若换推理栈」的对照（§2.5）。【需实测】本部署上任一路径的 TTFT/TPOT、接受率、动作质量与闭环成功率。

### A.4 vLLM 侧 CUDA Graph / chunked prefill / prefix caching / FP8 的已知取舍

**1) CUDA Graph**

- 【文献结论】`FULL_AND_PIECEWISE` 是 v1 + piecewise compilation 的默认（`-O2`），官方原文 `"generally the most performant setting, especially for low latency with small models or MoEs"`，代价是显存最多、capture 最长；`--enforce-eager` `"at the cost of steady-state decode performance"`，审计中应视为可疑配置（`vllm_latency_optimization.md` §2.1、§2.3、§0.3 第 2 条）。`max_cudagraph_capture_size` 默认上限 512，1024 只给数据中心 Blackwell（§2.2、§0.1）。
- 【文献结论】混合 GDN + full 注意力模型取所有后端 `AttentionCGSupport` 的**最小值**，Mamba/SSM 与 FlashInfer 后端只有 `UNIFORM_SINGLE_TOKEN_DECODE` → **可能被自动降级**（§5.2、§0.3 第 5 条）。ViT encoder 另有独立 CUDA Graph，对多图单请求 TTFT 往往是关键（§2.4）。
- 【本项目实测】`environment.json`：`cudagraph_mode=FULL_AND_PIECEWISE`、`enforce_eager=false`、启动日志 "Capturing CUDA graphs (PIECEWISE) 51/51 and (FULL) 35/35 succeeded => FULL CUDA graph IS active, not --enforce-eager"、`cudagraph_mm_encoder=false`、`compile_mm_encoder=false`、`max_cudagraph_capture_size=512`、`cudagraph_pool_GiB=0.19`。即文献预测的「可能降级」在本部署日志中未阻止 FULL 捕获。【需实测/需核对】启动日志 `CUDAGraph Config Settings: - Mode:` 的实际值（该调研 §7 核查清单第 3 条），以确认文献预测与实测是否冲突。

**2) chunked prefill**

- 【文献结论】V1 默认开且优先调度 decode，`"It improves inter-token latency (ITL)"`；较小 `max_num_batched_tokens`（如 2048）ITL 更好（§2.3）。
- 【本项目实测】`enable_chunked_prefill=true`（`environment.json`）。

**3) prefix caching**

- 【文献结论】APC `"only reduces the time of processing the queries (the prefilling phase) and does not reduce the time of generating new tokens (the decoding phase)"`，收益集中在 TTFT（§0.3 第 1 条）。多图多轮 VL 需 Processor Output / IPC / KV 三层缓存同时命中（§1.3）；即使 KV 100% 命中，多图 TTFT 仍可能随图片数线性增长（Issue #52040 约 19ms/图，修复 PR #52041 已 Merged）。命中率**没有 gauge**，须用 `rate(vllm:prefix_cache_hits[5m]) / rate(vllm:prefix_cache_queries[5m])` 现算（§1.4）。Qwen3.5 混合 Mamba 前缀命中**只在满块边界生效**，`--enable-mamba-shared-prefix-checkpoint` 默认关闭且需 prefix caching + EAGLE/MTP + `--prefix-match-unit` 小于块大小同时满足才生效（§5.3）。System Prompt 必须 leading（本草稿 §2.1 第 6 项）。
- 【本项目实测】`environment.json`：`enable_prefix_caching=true`、`mamba_cache_mode=align`、**kv_cache 6.48GiB / 182706 tokens**。【本项目实测，batch1 表引用】`gs_realtime_audit/report/summary.md`：`baseline_stream` Prefill P50 63.1ms / TTFT P50 186.8ms / E2E P50 499.0ms / Decode P50 350.6ms；`stream_prefix_off` Prefill P50 498.0ms / TTFT P50 618.6ms / E2E P50 931.6ms / Decode P50 352.0ms。即 **prefix off 使 prefill 63.1→498.0ms、TTFT 186.8→618.6ms、E2E +432.6ms（499.0→931.6），decode 基本不变 350.6→352.0ms** —— 与文献「APC 只省 prefill、不省 decode」一致。以上为 batch1 表内数字的引用，不改写其结论。
- 【需实测】`TTFT ~ 图片数` 曲线（确认本版本是否含 PR #52041、是否仍有约 19ms/图广播开销）、跨请求命中率与 `cache_salt` 影响。

**4) FP8 / 量化**

- 【文献结论】官方收益 `"2x reduction in model memory requirements and up to a 1.6x improvement in throughput"` —— 是**吞吐**而非单请求延迟；在线动态量化 `"latency improvements are limited in this mode"`；FP8 ViT 小图 `"may see no speedup due to quantization overhead (3 quantization kernel launches + un-padding)"`，动态缩放还与 ViT full CUDA Graph 互斥（§4.1、§4.3）。RTX 5090 是 sm_120 非 sm_100；PR #43814（OPEN）证明 SM120/SM121 的 CUTLASS FP8 grouped GEMM 曾因两个 bug 全量回退 Triton（§0.1、§4.2）。审计结论：**FP8 对单请求是条件收益，不能假设「量化=更快」**，须逐配置实测 TTFT/TPOT P50/P99，并先看启动日志 `Selected <kernel> for <module>` 确认实际命中的 GEMM 后端（§4.3、§7 第 8 条）。
- 【本项目实测】`environment.json`：`quantization=None`（BF16）、`reasoning_parser` 空、`structured_outputs_backend=auto`。本部署**未启用任何量化**；是否引入 FP8 需逐配置 A/B 实测（BF16 vs `fp8_per_tensor` vs 预量化 W8A8），【需实测】。

### A.5 证据分级

- 【文献结论】= 来自 `gs_realtime_audit/research/lumine_streaming_spec_decoding.md` 与 `gs_realtime_audit/research/vllm_latency_optimization.md` 的联网核实结论（其原始 URL 见两文件 §5 / §8 来源列表；本子代理未联网复核）。
- 【本项目实测】= 来自 `gs_realtime_audit/audit/environment.json`（只读环境探针 + 启动日志 + checkpoint 权重扫描）、`gs_realtime_audit/report/summary.md` 与 `gs_realtime_audit/report/summary.json`（batch1，每配置 100 trials）。
- 本子代理未联网、未连 GPU、未跑推理，未修改 `benchmark/`、`report/`、`prototypes/`、`tests/` 下任何文件；只写本草稿文件。

### 本草稿标记的「需实测」项（汇总）

1. 【需实测】外部出口 `http://i-2.gpushare.com:47960/` 链路的整体缓冲与 SSE chunk 到达分布（阶段 6 网络实验）；本地 loopback 的 ~10.6ms/token 不能代表外网链路。
2. 【需实测/需核实】`_server_scripts.txt` 中 8080 转发目标端口标注不一致（`fwd8080.py:583` 为 8010，`EXPOSE_8080.md:752` 写 8000），实际端口链需核实。
3. 【需实测】KV Cache / prefix caching 在本客户端多轮对话中的实际命中率与 `cache_salt` 影响（`vllm:prefix_cache_hits` / `vllm:prefix_cache_queries`）。
4. 【需实测】Triton JIT `store_cache_checkpoints_kernel` 延迟尖峰的出现频率与幅度。
5. 【需实测/需核对】启动日志 `CUDAGraph Config Settings: - Mode:` 的实际值，核对混合 GDN 模型是否被自动降级。
6. 【需实测】`TTFT ~ 图片数` 曲线（确认 vLLM 版本是否含 PR #52041、是否仍有约 19ms/图广播开销）。
7. 【需实测】「组优先」把 g1 提前 21.397–75.309ms 的理论推算（**理论推算、不是实测**），需新协议微调后的实测对照。
8. 【需实测】B/C/D 的 Token 数、decode 时长、g1/g6 延迟、解析成功率与闭环动作质量，需独立微调 + 质量对照实验。
9. 【需实测】33ms p99 预算、stale observation 与鼠标 overshoot / 导航过冲的闭环影响。
10. 【需实测】Grammar-aware / `custom_class` proposer / `structured_outputs` / n-gram 投机解码在本部署（`speculative_config=None`、无 MTP 头、vLLM 0.33.0）的可用路径、接受率、TTFT/TPOT、分布一致性与闭环 A/B。
11. 【需实测】FP8 / 量化在本部署的 TTFT/TPOT P50/P99（BF16 vs `fp8_per_tensor` vs 预量化 W8A8），不能假设量化更快。
12. 【需实测】流式执行下 A 方案的 g1..g6 到达时间与 200ms 窗口的关系（用 p50/p99 而非均值）。
13. 【需实测】历史动作存的是模型预测而非实际执行结果，其造成的上下文/真实状态漂移对闭环质量的影响。
14. 【需实测】B 方案「只改变顺序不改总 token 数 → T16_g6 / E2E 预期不变」的假设。
