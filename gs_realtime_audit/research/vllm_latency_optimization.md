# vLLM 0.33 单请求实时延迟：服务端配置与内核现状（RTX 5090 / Blackwell）联网核实报告

> GameScaling 实时推理审计 · 调研子代理产出
> 核实日期：2026-11-05；口径：vLLM `v0.33.0`（2026-11-01 发布），目标硬件 `RTX 5090`。
> 方法：全部结论经联网核实（`web_search` / `web_fetch` / `pwsh Invoke-WebRequest`），主源为 vLLM `v0.33.0` tag 的官方文档与源码（`https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/...`），并给出可核实链接。**未访问 GPU 服务器、未跑推理、未凭记忆下结论。**

---

## 0. 摘要与两个必须先对齐的口径

### 0.1 ⚠️ 硬件校正：RTX 5090 是 sm_120，不是 sm_100（影响整份审计）

任务书写的「RTX 5090 (Blackwell sm_100)」混淆了两代编号。联网核实：

- **RTX 5090 = 消费级 Blackwell GB202，compute capability = SM 12.0（sm_120）**；**sm_100 是数据中心 GB100 / B200 / GB200**。二者在 vLLM 源码里是**两条不同的分支**，kernel 可用性不同。
- `vllm/platforms/cuda.py` 的 `support_deep_gemm()` 分别判断 `is_device_capability(90)`（Hopper）、`is_device_capability_family(100)`（数据中心 Blackwell）、`is_device_capability_family(120)`（消费 Blackwell）：
  `return (cls.is_device_capability(90) or cls.is_device_capability_family(100) or cls.is_device_capability_family(120))`
  该文件另有 SM10 分支注释 `# SM100f defaults to FlashInfer for TRTLLM causal attention`，以及独立的 SM12 分支 `if device_capability.major == 12: return [TRITON_MLA, FLASHINFER_MLA_SPARSE_SM120]`。
- `vllm/config/compilation.py` 对 `max_cudagraph_capture_size` 的原文：*"capped at 512 by default, or 1024 on data center Blackwell GPUs"* —— **5090 只拿 512 的默认上限，1024 只给数据中心卡**。
- **最关键的实测风险**：PR #43814（仍 OPEN）原文 *"Fixes two bugs that silently disabled the CUTLASS FP8 grouped GEMM path for all SM_120/SM_121 hardware (RTX 5090/5080/5070, DGX Spark GB10), causing every MoE expert dispatch to fall back to the Triton backend."* 即 **5090 上 FP8 MoE 的 CUTLASS grouped GEMM 路径曾被静默禁用、全量回退 Triton**。

> **结论：不能把 B200/sm_100 的 FP8 kernel 与延迟数据直接套到 5090/sm_120。** 所有 FP8 性能结论必须先看启动日志 `Selected <kernel> for <module>` 确认实际命中的 GEMM 后端。此校正已单独提前通报 lead。

来源：[PR #43814](https://github.com/vllm-project/vllm/pull/43814)、[v0.33.0 cuda.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/platforms/cuda.py)、[v0.33.0 compilation.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/config/compilation.py)、[SM12.0 参考](https://github.com/cdcseacave/openMVS/issues/1248)。

### 0.2 版本口径

v0.33.0 于 **2026-11-01** 发布，927 commits / 389 contributors，基于 **PyTorch 2.14**，**仅发布 CUDA 13.0 的 wheel 与镜像（CUDA 12.9 已停止发布）**，默认 ROCm 10.0；移除了 Medusa、MLP speculator、Ray compiled-graph executor；`--mamba-cache-mode` 已弃用且无效。5090 属 sm_120，源码构建需 SM12x arch list（PR #43814 验证环境用 `12.0a;12.1a;12.1+PTX` + CUDA 13.0.2）。

来源：[AI/TLDR v0.33.0 发布页](https://ai-tldr.dev/releases/vllm-v0-33-0/)、[newreleases v0.33.0rc3](https://newreleases.io/project/github/vllm-project/vllm/release/v0.33.0rc3)。

### 0.3 与「单请求实时延迟」最相关的六条结论（TL;DR）

1. **APC 只省 prefill，不省 decode**：原文 *"APC only reduces the time of processing the queries (the prefilling phase) and does not reduce the time of generating new tokens (the decoding phase)"*。对「首 token 快、长回答」的实时场景，收益集中在 TTFT。
2. **CUDA Graph 默认已是低延迟最优档**：`FULL_AND_PIECEWISE`（`-O2` 默认），`--enforce-eager` 会**牺牲稳态 decode 性能**，只用于快速启动/测启动开销。
3. **投机解码是「降 ITL」而非「降 TTFT」**：官方定位 *"reduce inter-token latency under medium-to-low QPS, memory-bound workloads"*；单请求收益取决于接受率，不保证。
4. **FP8 量化不必然降低单请求延迟**：官方文档明确收益是「2× 显存 / 1.6× **吞吐**」，在线动态量化 *"latency improvements are limited"*，小图 ViT 量化 *"may see no speedup due to quantization overhead"*；sm_120 还有 kernel 回退风险。
5. **Qwen3.5 混合注意力（GDN + full）会让 CUDA Graph 自动降级、前缀缓存只能在块边界命中**：因为 hybrid mixer 取所有后端 `AttentionCGSupport` 的**最小值**。
6. **命中率没有 gauge**：0.33 暴露的是 `vllm:prefix_cache_queries` / `vllm:prefix_cache_hits` 两个 Counter，命中率要用 PromQL 现算。

---

## 1. Prefix Caching（APC）：开关、VL 前缀复用、命中率观测

### 1.1 开关

`enable_prefix_caching=True`（离线 `LLM(..., enable_prefix_caching=True)`；在线由引擎参数控制）。v1 中 prefix caching 近乎零开销。哈希算法 `--prefix-caching-hash-algo`：自 v0.11 起默认 `sha256`（pickle 序列化，跨 Python/vLLM 版本不保证可复现）；`sha256_cbor`（cbor2，跨语言可复现，**官方推荐用于确定性缓存**）、`xxhash` / `xxhash_cbor`（更快但非密码学安全，有碰撞风险警告）。

来源：[v0.33.0 automatic_prefix_caching.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/automatic_prefix_caching.md)、[v0.33.0 prefix_caching.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/prefix_caching.md)。

### 1.2 哪些前缀可复用（多图多轮 VL 请求）

**块哈希的三个组成**（原文）：`hash(tuple[components])`，components = ① **Parent hash**（父块哈希）② **Block tokens**（块内 token 元组，"to reduce potential hash value collision"）③ **Extra hashes**（*"such as LoRA IDs, multi-modality input hashes ..., and cache salts"*）。且 *"We only cache full blocks."*（只缓存满块）。

**多模态图像如何进哈希**（设计文档示例）：`[IMG]` 经分词后替换为一串占位符 `<P>`，prefill 时再被图像 embedding 替换。为区分「不同图像但相同占位符」，官方把 **前端 image processor 生成的图像哈希** 作为 `Extra hash` 写进覆盖占位符的每一个块：

`Block 0  Parent: None        Tokens: 1,3,...,<p>,...  Extra hash: <image hash>`
`Block 1  Parent: Block0 hash Tokens: <p>,...           Extra hash: <image hash>`

由此可推出（本报告据源码机制的**推断**，非官方原句）多图多轮的可复用规则：

| 场景 | 是否可复用 | 原因 |
| --- | --- | --- |
| 同一会话下一轮，仅在历史末尾追加新文本/新问题 | ✅ 复用此前所有满块 | 父哈希链前半段不变 |
| 两张图字节/内容完全一致、且图像顺序、分辨率、`mm_processor_kwargs` 一致 | ✅ 覆盖这些图的块（含后续块）可复用 | 图像哈希相同 → Extra hash 相同 |
| 换图 / 插入图 / 调整图片顺序 / 改 `max_pixels` 等处理器参数 | ❌ 从第 N 个块起全部失效 | Extra hash 变化 + 父哈希链断裂 |
| 换 LoRA、加 `cache_salt`、无共享前缀 | ❌ | LoRA ID / salt 进哈希 |
| 只换最后一句问题、图与前文不变 | ✅ 复用图与前文 | 仅末尾块重算 |

- `cache_salt`（请求体字段）注入首块哈希做多租户隔离，防时序侧信道（*"This prevents timing-based attacks where an adversary could infer cached content by observing latency differences"*）——**审计时若发现跨请求命中率异常低，先查是否被 salt 或不同 UUID 隔离**。
- 媒体项默认**按内容哈希**；可用 `multi_modal_uuids` 提供稳定 ID 免去重复哈希，命中后甚至可以**不传图像数据**（缺 UUID 或未命中会报错）。原文警告：*"If both multimodal processor caching and prefix caching are disabled, user-provided multi_modal_uuids are ignored."*

### 1.3 VL 请求的三层缓存与「命中了仍慢」的坑

VL 请求要快，需**三层缓存同时命中**（`optimization.md` / `mm_processing.md`）：

1. **Processor Output Caching**（HF processor 输出缓存，自动开）：原文 *"Some HF processors, such as the one for Qwen2-VL, are very slow... we cache the multi-modal outputs of HF processor to avoid processing the same multi-modal input (e.g. image) again"*。
2. **IPC Caching**（API 进程↔EngineCore，自动开，需 1:1 进程对应；TP>1 建议 `mm_processor_cache_type="shm"`）。
3. **KV Prefix Caching**（本文 1.2）。

`mm_processor_cache_gb` 默认 **4 GiB**，设 0 同时关掉 IPC + processor 缓存；单条超大图像超限会「不缓存 + 告警」而非启动失败；API server scale-out 会禁用 IPC 缓存但**不影响** processor 缓存。

**⚠️ 单请求 TTFT 的隐藏线性开销**：Issue #52040 标题原文 *"[Performance]: MM input tensors are re-broadcast to all TP workers on every request, even when the prefix cache fully covers the image tokens (~19 ms per unique image)"*（v0.25.1、TP=4、Gemma-4-31B-IT、15.5MB pixel_values/图）。即**即使 KV 前缀 100% 命中，多图请求的 TTFT 仍会随图片数线性增长（约 19 ms/图）**。修复 PR #52041 *"[Core] Skip broadcasting mm tensor data to workers for prefix-cache-covered items"* 已 Merged（`NewRequestData.from_request` 剥离已覆盖项的数据，沿用 `data=None` 约定；抢占恢复用 `CachedRequestData.resumed_mm_features` 重发）。**审计动作**：确认服务端版本是否含 #52041，并实测 `TTFT ~ 图片数` 曲线，而不是只看命中率。

来源：[Issue #52040](https://github.com/vllm-project/vllm/issues/52040)、[PR #52041](https://app.semanticdiff.com/gh/vllm-project/vllm/pull/52041/overview)、[v0.33.0 optimization.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/configuration/optimization.md)、[v0.33.0 multimodal_inputs.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/multimodal_inputs.md)、[v0.33.0 mm_processing.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/mm_processing.md)。

### 1.4 命中率如何观测（Prometheus 真实名称）

设计文档原文 *"See above - we now expose 'queries' and 'hits' counters rather than a 'hit rate' gauge."* —— **不存在 `vllm:prefix_cache_hit_rate` 这个 gauge**。真实指标：

- `vllm:prefix_cache_queries`（Counter）— *Number of prefix cache queries*（查询的 token/块数）
- `vllm:prefix_cache_hits`（Counter）— *Number of prefix cache hits*

命中率用 PromQL 现算：`rate(vllm:prefix_cache_hits[5m]) / rate(vllm:prefix_cache_queries[5m])`。另外 INFO 日志每 5 秒打印一次 *"The prefix cache hit rate over the most recent 1k kv-cache block queries"*（固定最近 1k 次块查询窗口）。

来源：[v0.33.0 design/metrics.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/metrics.md)、[v0.33.0 usage/metrics.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/usage/metrics.md)。

---

## 2. CUDA Graph：`cudagraph_capture_sizes` / full vs piecewise / `--enforce-eager`

### 2.1 单一旋钮与五种模式

`docs/design/cuda_graphs.md`：*"CUDAGraphMode is the single knob you tune in CompilationConfig.cudagraph_mode"*。CLI：`--compilation-config '{"cudagraph_mode": "FULL_AND_PIECEWISE"}'`（**大写字符串**）。

| 模式 | 含义 | 对单请求 decode 延迟 |
| --- | --- | --- |
| `NONE` | 关闭，调试用 | 每次 kernel launch 开销全付，最慢 |
| `PIECEWISE` | 旧默认，最灵活；attention 等 CG 不兼容算子保持 eager，其余入图；需 piecewise compilation | 消除大部分 launch 开销，但 attention 仍 eager |
| `FULL` | 对非均匀 batch 捕全图，均匀 decode 复用同 batch_size 图；适合小模型/短 prompt | 小模型低延迟好 |
| `FULL_DECODE_ONLY` | 只对均匀 decode 捕全图，prefill/mixed 不入图；省显存，适合 P/D 分离 decode 实例 | decode 快、省内存 |
| `FULL_AND_PIECEWISE` | **默认**；均匀 decode 用全图、其余用 piecewise | 官方原文 *"generally the most performant setting, especially for low latency with small models or MoEs"*，代价是**显存最多、capture 最长** |

默认值：v1 + piecewise compilation → `FULL_AND_PIECEWISE`（pooling 模型仍 `PIECEWISE`）；否则 `NONE`。`compilation.py` 原文亦称其 *"is the most performant mode for most models and is the default"*。

**术语**：*"pure decode (max_query_len=1) or speculative decode (max_query_len = 1+num_spec_tokens)"* 称 **uniform decode**；prefill / mixed prefill-decode 称 **non-uniform**。

### 2.2 `cudagraph_capture_sizes` 与自动降级

- `cudagraph_capture_sizes: list[int] = None` —— 原文 *"None (default): capture sizes are inferred from vllm config. - list[int]: capture sizes are specified as given."*；`max_cudagraph_capture_size` 未指定时按 `[1, 2, 4] + range(8,256,8) + range(256, max+1, 16)` 自动生成，且 *"capped at 512 by default, or 1024 on data center Blackwell GPUs"*（**5090 落 512**）。相关新参数（0.33）：`cudagraph_decoder_replay=True`（YOCO/DeepSeek-V4.1）、`decoder_replay_cudagraph_capture_sizes`、`decoder_replay_trim_threshold=768`。`compile_sizes` 可接受字面量 `"cudagraph_capture_sizes"`。
- **自动降级**：*"Not all CUDA Graph modes are compatible with every attention backend. We automatically 'downgrade' modes to the closest supported mode."* `AttentionCGSupport`：`ALWAYS(3) > UNIFORM_BATCH(2) > UNIFORM_SINGLE_TOKEN_DECODE(1) > NEVER(0)`。后端表：FA v2=`UNIFORM_BATCH`，FA v3=`ALWAYS`，Triton=`ALWAYS`，FlashInfer=`UNIFORM_SINGLE_TOKEN_DECODE`（*"Will be set to UNIFORM_BATCH when using TRTLLM attention on Blackwell"*），FlashMLA/FlashInferMLA/FlashInferMLASparse=`UNIFORM_BATCH`，AITER MLA/CUTLASS MLA=`UNIFORM_SINGLE_TOKEN_DECODE`，**Mamba attention=`UNIFORM_SINGLE_TOKEN_DECODE`**。`CudagraphDispatcher` 优先级 `FULL > PIECEWISE > None`。**混合/Mamba mixer 模型取所有后端能力的最小值** → 见第 5 节。`AttnQuantFusionPass` 与 piecewise 编译不兼容，会自动改用 `FULL`/`FULL_DECODE_ONLY`。

### 2.3 `--enforce-eager` 与优化级别

`optimization.md` 原文：`--enforce-eager` *"Skips both compilation and CUDA-graph capture for the fastest possible startup, at the cost of steady-state decode performance. Useful for development loops and for measuring how much of a boot is compile/capture."* —— **对单请求稳态 decode 延迟是负收益**，审计时若发现开了它，应视为可疑配置。

| 级别 | 原文 | CUDA Graph |
| --- | --- | --- |
| `-O0` | *No optimizations. Fastest startup time, but lowest performance.* | 无 |
| `-O1` | *Simple compilation and fast fusions, and PIECEWISE cudagraphs.* | PIECEWISE |
| `-O2` | **Default** *Additional compilation ranges, additional fusions, FULL_AND_PIECEWISE cudagraphs.* | FULL_AND_PIECEWISE |
| `-O3` | 当前等同 `-O2` | 同 |

加速启动（不影响稳态）：`VLLM_CACHE_ROOT` + `VLLM_FORCE_AOT_LOAD=1`、`--kv-cache-memory`（跳过显存 profiling 与 CG 显存估算）。**Chunked prefill** 在 V1 默认开且优先调度 decode（*"It improves inter-token latency (ITL)"*），较小的 `max_num_batched_tokens`（如 2048）ITL 更好——这也是单请求 ITL 的相关旋钮。

### 2.4 如何确认 CUDA Graph 命中（日志关键字，v0.33 实测源码）

- `vllm/v1/worker/gpu_model_runner.py` 的 tqdm：
  `desc="Capturing CUDA graphs ({}, {})".format("decode" if uniform_decode else "mixed prefill-decode", cudagraph_runtime_mode.name)`
  → 启动日志出现 **`Capturing CUDA graphs (decode, FULL)`** / **`Capturing CUDA graphs (mixed prefill-decode, PIECEWISE)`**；`self.cudagraph_batch_sizes = sorted(self.compilation_config.cudagraph_capture_sizes)`。
- `vllm/compilation/cuda_graph.py`：DEBUG 级 `logger.debug("Capturing a cudagraph on (%s,%s)", ...)`（需 `cudagraph_options.debug_log_enable`）；`CUDAGraphLogging` 输出 `**CUDAGraph Config Settings:** - Mode: {mode} - Capture sizes: {sizes}` 与 `**CUDAGraph Stats:**` 表（列 `Unpadded Tokens / Padded Tokens / Num Paddings / Runtime Mode / Count`）。
- ⚠️ 旧版常见的 `Capturing CUDA graph shapes` / `Capturing cudagraph` **不在 v0.33 runner 中**，用旧关键字 grep 会误判为「未命中」。
- **ViT 编码器另有独立 CUDA Graph**（`docs/design/cuda_graphs_multimodal.md`）：*"Encoder CUDA Graphs are orthogonal to decoder CUDA Graphs — both can be enabled simultaneously."* 动机原文 *"Vision encoder inference incurs CUDA kernel launch overhead on the host side. The overhead is more significant when the batch size is small or image size is small."* 按 token budget 捕获（如 `[2048,4096,8192,13824]`，`EncoderCudaGraphManager` / `BudgetGraphMetadata`，可用 `encoder_cudagraph_token_budgets` 覆盖）。原文 *"Encoder CUDA Graph has currently been tested with --mm-encoder-attn-backend=FLASH_ATTN and --mm-encoder-attn-backend=FLASHINFER on Blackwell GPUs."* 支持表含 `Qwen3VLForConditionalGeneration`、`Qwen3_5ForConditionalGeneration`、`Qwen3_5MoeForConditionalGeneration`。**对多图单请求，encoder CG 往往是 TTFT 的关键。**

来源：[v0.33.0 cuda_graphs.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/cuda_graphs.md)、[v0.33.0 compilation.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/config/compilation.py)、[v0.33.0 gpu_model_runner.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/v1/worker/gpu_model_runner.py)、[v0.33.0 cuda_graph.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/compilation/cuda_graph.py)、[v0.33.0 cuda_graphs_multimodal.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/cuda_graphs_multimodal.md)、[v0.33.0 optimization.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/configuration/optimization.md)。

---

## 3. vLLM 0.33 投机解码：n-gram / suffix / MTP / EAGLE

### 3.1 官方定位（就是冲着单请求 ITL 来的）

`docs/features/speculative_decoding/README.md` 首句原文：*"reduce inter-token latency under medium-to-low QPS (queries per second), memory-bound workloads"*；*"Model-based methods such as EAGLE, MTP, draft models and PARD provide the best latency reduction, while simpler methods such as n-gram and suffix decoding provide modest speedups without increasing workload during peak traffic."*

| 方法 | 低 QPS（延迟导向） | 高 QPS（吞吐导向） |
| --- | --- | --- |
| EAGLE / MTP / Draft model / PARD | High gain | Medium~High gain |
| N-gram / Suffix | Low to medium gain | Medium gain |

### 3.2 是否需要额外权重 / 如何配置（核心对照表）

统一入口 `--speculative-config '{...}'`（Python 为 `LLM(..., speculative_config={...})`）。⚠️ `draft_model.md` 原文：*"Please use --speculative-config to set all configurations related to speculative decoding. The previous method of specifying the model through --speculative-model and adding related parameters such as --num-speculative-tokens separately has been deprecated."*

| 方法 | 需要 draft/MTP 权重？ | 最小配置示例 | 机制 / 适用 |
| --- | --- | --- | --- |
| **n-gram** | ❌ 完全不需要模型 | `{"method":"ngram","num_speculative_tokens":5,"prompt_lookup_max":4}` | 在 **prompt** 内做 n-gram 匹配提议；`prompt_lookup_min/max` 默认 5（省略其一则镜像另一个）；轻量、零额外显存 |
| **suffix** | ❌ 不需要 draft 模型，但需 `pip install arctic-inference` | `{"method":"suffix","num_speculative_tokens":32}` | 匹配 prompt **+ 历史生成**、按频次提议、**动态**推测长度；`num_speculative_tokens` 是**上限**（建议 16/32）；`suffix_decoding_max_tree_depth=24`、`suffix_decoding_max_cached_requests=10000`（0 关全局缓存）、`suffix_decoding_max_spec_factor=1.0`、`suffix_decoding_min_token_prob=0.1`；最适合代码编辑 / agentic 循环 / RL rollouts |
| **MTP** | ⚠️ **需要 checkpoint 自带原生 MTP 头/权重**，但不需单独 draft 模型 | `{"method":"mtp","num_speculative_tokens":1}`（如 `XiaomiMiMo/MiMo-7B-Base`） | 原文 *"Unlike draft-model-based methods, you do not need to provide a separate draft model"* **但** *"MTP only works for model families that support MTP in vLLM"*；Gemma 4 assistant checkpoint 走 `"method":"mtp","model":"...assistant"`；`num_speculative_tokens:1` 是好的起点；可选 `"draft_lm_head_quantization":"fp8"|"nvfp4"` 给 drafter 一份量化 lm_head 副本（248k×2560 头约 0.6/0.35 GiB，*"only the acceptance length can move"*）；`config/speculative.py` 的 `MTPModelTypes` 含 `qwen3_5_mtp`、`qwen3_next_mtp`、`deepseek_mtp`、`gemma4_mtp` 等 |
| **EAGLE / EAGLE3** | ✅ 需要独立 EAGLE/EAGLE3 head 权重 | `{"model":"yuhuili/EAGLE-LLaMA3-Instruct-8B","draft_tensor_parallel_size":1,"num_speculative_tokens":2,"method":"eagle"}` 或 `RedHatAI/Llama-3.1-8B-Instruct-speculator.eagle3` + `"method":"eagle3"` | 预训练权重见 HF collections `RedHatAI/speculator-models` / `yuhuili/models`；需与目标模型匹配 |

**通用键**：`method`（`draft_model/ngram/suffix/mtp/eagle3/dflash`；省略时按配置推断）、`model`（draft 模型 / EAGLE head / 辅助模型；**ngram / ngram_gpu / suffix / mtp 常可省略**）、`num_speculative_tokens`、`draft_tensor_parallel_size`（⚠️ 不是 `tensor_parallel_size`，后者是非法键）、`max_model_len`、`parallel_drafting`（默认 false，仅 EAGLE + draft-model）、`rejection_sample_method`（`standard|synthetic|block|fly`，默认 `standard`）、`draft_sample_method`（`greedy|probabilistic`，默认 greedy）、`use_heterogeneous_vocab`（TLI，仅 `draft_model` + greedy）。`temperature` / `top_p` 是采样参数，不是 `speculative_config` 字段。

### 3.3 对单请求延迟的收益与额外开销

- **收益来源**：一次 verify 步可产出多个 token，压低 **ITL / TPOT**（`request_time_per_output_token_seconds`）；**不降 TTFT**，且 speculative decode 的 `max_query_len = 1 + num_spec_tokens` 仍被算作 uniform decode，可继续用 FULL cudagraph。
- **额外开销（务必写进审计）**：model-based 方法每步多一次 **draft forward + 对 k+1 个 token 的 verify**（高 QPS 时反而增负），外加**额外权重显存 / KV 组织**；n-gram/suffix 只有 CPU 侧模式匹配 + verify，故 *"without increasing workload during peak traffic"*。
- **不保证收益**：接受率低时，验证开销与回退会吃掉收益，甚至变慢；`FLy` 验证是有损的（*"deliberately NOT distribution-preserving"*，`fly_window_size=min(6,k-1)`、`fly_entropy_threshold=0.3`、`fly_entropy_top_k=3`），不能用于要求输出分布一致的场景。0.33 新增 DFlash/DSpark 不再预留 draft slot（官方 GB300 示例 1373→3390 output tok/s，属**吞吐**数据，非单请求延迟）。
- **已知不兼容**：流水线并行（PP）不可与投机解码组合（<=0.15.0 注记）；Draft model 在 <=0.10.0 不支持。

### 3.4 接受率指标（如何量化「单请求是否真的受益」）

**逐请求（0.33 新增，实验性）**：`--per-request-spec-decode-metrics summary|detailed`（默认 `none`，*"Collection is gated at the source"*）。响应 `metrics.speculative_decoding`：

| 字段 | 含义 |
| --- | --- |
| `mean_acceptance_length` | `1 + num_accepted_draft_tokens/num_spec_steps`，范围 1.0~k+1，含 bonus token |
| `draft_acceptance_rate` | `num_accepted_draft_tokens/num_draft_tokens` |
| `acceptance_histogram` | 长度 k+1，索引 j = 恰好接受 j 个 draft token 的步数（不含 bonus） |
| `num_spec_steps` / `num_accepted_draft_tokens` / `num_draft_tokens` / `num_spec_tokens`(=k) | 计数；`detailed` 另加 `per_step_accepted` / `per_step_drafted` |

限制：**仅 n==1 单序列请求**（否则 `null`）；流式时挂在最后一个 usage chunk（需 `stream_options.include_usage:true` 或 `--enable-force-include-usage`）。文档示例值：`mean_acceptance_length=1.2326, draft_acceptance_rate=0.0775, acceptance_histogram=[39,1,0,3], num_spec_steps=43`。

**服务端聚合 Counter**（`acceptance_metrics.md` 对账表）：`vllm:spec_decode_num_drafts_total`、`vllm:spec_decode_num_draft_tokens_total`、`vllm:spec_decode_num_accepted_tokens_total`。设计文档另列 legacy Gauge（标注 *"revisit"*）：`vllm:spec_decode_draft_acceptance_rate`、`vllm:spec_decode_efficiency`、`vllm:spec_decode_num_accepted_tokens`、`vllm:spec_decode_num_draft_tokens`、`vllm:spec_decode_num_emitted_tokens`，并写明 *"We should probably expose acceptance rate as separate accepted and draft counters, like we do for prefix caching hit rate."*

> **审计要点**：`vllm:inter_token_latency_seconds` 与 `vllm:request_time_per_output_token_seconds` 在投机解码下**会分叉**（一次 output 事件可含多 token，见第 6 节），评估单请求延迟请用 **TPOT**（`request_time_per_output_token_seconds`）并配合逐请求接受率。

来源：[v0.33.0 spec decode README](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/README.md)、[n_gram.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/n_gram.md)、[suffix.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/suffix.md)、[mtp.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/mtp.md)、[eagle.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/eagle.md)、[draft_model.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/draft_model.md)、[acceptance_metrics.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/acceptance_metrics.md)、[v0.33.0 speculative.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/config/speculative.py)、[docs.vllm.ai 投机解码](https://docs.vllm.ai/en/latest/features/speculative_decoding/)。

---

## 4. FP8 (W8A8) vs BF16：Blackwell kernel 支持与单请求 TTFT/ITL

### 4.1 支持矩阵与「实际命中哪个 kernel」

- `llm_compressor/fp8.md` 原文：*"Ada Lovelace, Hopper, and Blackwell GPUs are supported for W8A8"*；*"FP8 computation is supported on NVIDIA GPUs with compute capability >= 8.9 ... FP8 models will run on compute capability >= 7.5 (Turing) as weight-only W8A16, utilizing FP8 Marlin"*。即 **<8.9 会退化成 W8A16（只压权重、不压激活）**，此时「W8A8 加速」不成立。
- `Fp8Config.get_min_capability()` 返回 **75**（对应的正是 W8A16/Marlin 兜底）；`get_supported_act_dtypes() = [bfloat16, half]`；源码注释 *"Only support float8_e4m3fn data type due to the limitation of torch._scaled_mm"*；按 `cutlass_fp8_supported()` 决定 per-token 激活缩放。
- **启动时自动选 kernel 并打印**（原文）：*"vLLM picks an FP8 GEMM kernel automatically at load time and logs a Selected <kernel> for <module> line at startup. For block-quantized checkpoints on CUDA it tries, in order: a FlashInfer/DeepGEMM hybrid (Hopper only), DeepGEMM, CUTLASS, Marlin, Triton, Humming, then a PyTorch fallback."* —— **这是审计 FP8 是否真的生效的第一手证据**。卡住可用 `VLLM_USE_DEEP_GEMM=0` 或 `--linear-backend cutlass`；注意 `--linear-backend` 只管量化 linear 层，**MoE 要用 `--moe-backend`**。
- 在线动态量化（`--quantization fp8_per_tensor` / `fp8_per_block` / `mxfp8` / `mxfp4`，见 `online.md`）：`mxfp8` 原文 *"Requires SM 100+ (Blackwell or newer) for w8a8, other GPUs use a w8a16 fallback"*；`fp8_per_tensor` 原文 *"As a result, latency improvements are limited in this mode."*
- FP8 KV cache（`quantized_kvcache.md`）：`kv_cache_dtype="fp8_e4m3"`（CUDA 11.8+/ROCm）或 `"fp8_e5m2"`，`--kv-cache-dtype-skip-layers`；与 FlashAttention-3 组合时 **Q 也被量化**；默认 scale=1.0（未校准时无收益甚至有损）。
- ⚠️ **文档滞后**：`quantization/README.md` 的硬件表只列到 Hopper（无 Blackwell 列），不可据其判断 5090 支持与否。

### 4.2 Blackwell / sm_120 的 kernel 现实（再次强调）

见 0.1：`support_deep_gemm()` 对 family(120) 放行，但 **PR #43814 证明 SM120 的 CUTLASS FP8 grouped GEMM (MoE) 路径曾因两个 bug 全量回退 Triton**：① `vllm/_custom_ops.py` 门槛 `cuda_device_capability >= 110 → False`（`121>=110` 为真，误杀 SM121）应改为 `>=130`；② 缺 `grouped_mm_c3x_sm120.cu`（SM100 版本的 SM120 类比，`KernelPtrArrayTmaWarpSpecializedCooperativeSm120<2>`、tile 128×128×128、cluster 1×1×1、arch `Sm120`），需 `scaled_mm_entry.cu` 增加 `version_num >= 120 && < 130` 分派；还依赖上游 [NVIDIA/cutlass#3280](https://github.com/NVIDIA/cutlass/pull/3280)（CUTLASS 4.5 尚无 `MainloopSm120ArrayTmaWarpSpecialized`）。该 PR 的**单流 MTP 实测**（DGX Spark SM121、`gemma-4-26B-A4B-it-FP8-Dynamic`、3 iters）：短输出 128 tok 76.3→81.9 tok/s（**+7.3%**）、512 tok 89.8→89.8（0%）、1024 tok 87.6→85.5（**−2.4%**，噪声）——**decode 主导的短请求才有可见收益，长请求被接受率噪声淹没**。

### 4.3 量化是否必然降低单请求延迟？——**否**

可核实的反证链：

1. 官方 FP8 收益表述是 *"2x reduction in model memory requirements and up to a 1.6x improvement in **throughput**"* —— **吞吐，不是单请求延迟**。
2. 在线动态量化：*"latency improvements are limited in this mode"*。
3. FP8 ViT attention（`fp8_vit_attn.md`，专用于 `qwen3_vl/qwen3_vl_moe/qwen3_5/qwen3_5_moe`）：*"Performance gains are mostly visible at QHD/4K resolutions or multi-image requests. **Smaller images may see no speedup due to quantization overhead (3 quantization kernel launches + un-padding)**"*；*"Dynamic scaling adds a small per-forward overhead"*；*"Dynamic scaling is not compatible with ViT full CUDA graphs"*（**与 encoder CG 互斥**，开了 FP8 动态缩放可能反而丢掉 CG 收益）；*"FP8 tensor-core speedup is more pronounced on GB300 than GB200"*（**越靠数据中心越明显，5090 更弱**）。生产建议先 `vllm bench mm-processor --model $MODEL --mm-encoder-attn-backend $MM_ATTN` 校准静态 scale。
4. PR #43814 的长序列 −2.4% 数据点 + sm_120 kernel 回退。
5. W8A8 会省显存 → 可放大 KV cache / 减抢占，但**单请求短上下文、compute-bound 的 prefill 常不降反升**（量化/反量化开销）。

> **审计结论**：FP8 对单请求是**条件收益**（大 batch / 长上下文 / 显存瓶颈 / 命中 FP8 tensor-core kernel / 静态 scale / 多图大图 ViT）。必须逐配置实测 `vllm:time_to_first_token_seconds` 与 `vllm:request_time_per_output_token_seconds` 的 P50/P99，**不能假设「量化=更快」**。

来源：[v0.33.0 llm_compressor/fp8.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/llm_compressor/fp8.md)、[online.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/online.md)、[fp8_vit_attn.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/fp8_vit_attn.md)、[quantized_kvcache.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/quantized_kvcache.md)、[fp8.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/model_executor/layers/quantization/fp8.py)、[PR #43814](https://github.com/vllm-project/vllm/pull/43814)。

---

## 5. Qwen3.5 / Qwen3-VL 混合注意力（GDN + full）对 decode kernel 与 prefix caching 的影响

### 5.1 结构（外部架构源 + v0.33 源码）

- [torchtitan qwen3_5 README](https://github.com/pytorch/torchtitan/blob/main/torchtitan/models/qwen3_5/README.md) 原文：*"Qwen3.5: Multimodal Model with Hybrid Attention ... **Hybrid Decoder: 75% GatedDeltaNet (linear attention) + 25% full attention** with output gating and partial RoPE. Vision Encoder: A Vision Transformer (ViT) with 2D RoPE ... Patch Merger ... MRoPE ... MoE variant: Routed experts + shared expert with sigmoid gate."*
- [Sebastian Raschka · Hybrid Attention](https://sebastianraschka.com/llm-architecture-gallery/hybrid-attention/) 原文：*"Qwen3-Next 80B-A3B has 48 layers arranged as 12 repetitions of the 3:1 pattern. Three Gated DeltaNet blocks are followed by one Gated Attention block. This means 36 layers use recurrent linear attention and 12 layers retain softmax attention"*；*"Qwen3.5 keeps the same 3:1 backbone ... the 60-layer Qwen3.5 397B-A17B model repeats the four-layer group 15 times"*；*"The remaining attention layers still require a growing KV cache, but far fewer layers contribute to it"*；GDN 层用固定大小状态（alpha/beta 门 + delta-rule 快权重更新），**状态不随序列增长**。
- v0.33 `vllm/model_executor/models/qwen3_5.py` 逐层分派（原文）：
  `layer_type = config.layer_types[extract_layer_index(prefix)]`
  `if self.layer_type == "linear_attention": self.linear_attn = QwenGatedDeltaNetAttention(..., gqa_interleaved_layout=False, ...)`
  `elif self.layer_type == "full_attention": self.self_attn = Qwen3NextAttention(..., mrope_positions_factor=mrope_positions_factor(vllm_config))`
  `Qwen3_5ProcessingInfo(Qwen3VLProcessingInfo)` 说明 Qwen3.5 复用 Qwen3-VL 的多模态处理链。`supported_models.md` 登记：`Qwen3_5ForConditionalGeneration`(`Qwen/Qwen3.5-9B-Instruct`)、`Qwen3_5MoeForConditionalGeneration`(`Qwen/Qwen3.5-35B-A3B-Instruct`)、`Qwen3VLForConditionalGeneration`（`Qwen/Qwen3-VL-4B-Instruct`）、`Qwen3VLMoeForConditionalGeneration`（`Qwen/Qwen3-VL-30B-A3B-Instruct`）。

### 5.2 对 decode kernel 的影响

- v0.33 `vllm/v1/attention/backends/gdn_attn.py`：`GDNAttentionBackend.get_name() -> "GDN_ATTN"`、`is_ssm() -> True`、`kv_cache_spec: MambaSpec`、`reorder_batch_threshold = 1`、`_cudagraph_support = AttentionCGSupport.UNIFORM_BATCH`；`FLASHINFER_GDN_CHECKPOINT_ALIGNMENT = 64`（*"FlashInfer's GDN prefill emits states every N tokens, N a multiple of its 64-token chunk, so checkpoint offsets must be multiples of 64"*）；`plan_flashinfer_checkpoints(offsets, query_lens)`、`build(common_prefix_len, common_attn_metadata, num_accepted_tokens=None, ...)`、`use_full_cuda_graph = compilation_config.cudagraph_mode.has_full_cudagraphs()`。
- **decode 不再是「纯 attention」**：GDN 层走 SSM/线性注意力状态更新（固定大小 state + 64-token 对齐 checkpoint），只有 25% 层走常规 KV-cache attention。好处是长上下文 KV 显存与 decode 访存更低；代价是**引入了需要额外状态管理、chunk 对齐的 kernel 路径**。
- **CUDA Graph 自动降级**：`cuda_graphs.md` 原文 *"Suppose we have hybrid attention backends (e.g., in mamba mixer models). In that case, we seek the minimum capability of all backends to determine the final capability of the model"*。Mamba/SSM 后端只有 `UNIFORM_SINGLE_TOKEN_DECODE`，FlashInfer 也是 `UNIFORM_SINGLE_TOKEN_DECODE` → **混合模型会被从 `FULL_AND_PIECEWISE` 降到较低能力档**，即「理论上最优」的 CG 模式在 Qwen3.5 上未必拿得到。**审计时必须读启动日志里 `CUDAGraph Config Settings: - Mode:` 的实际值**，而不是假定默认档生效。

### 5.3 对 prefix caching 的影响（重要限制）

`automatic_prefix_caching.md` 的 *Hybrid Mamba models* 段原文：*"With prefix caching enabled, Mamba state is stored only on the Mamba block grid, so a **prefix-cache hit can resume only at a block boundary**. --enable-mamba-shared-prefix-checkpoint also stores a checkpoint at the shared-prefix junction ... Requests whose shared prefix ends inside a block can then reuse it."*

该开关**默认关闭**，且**仅在以下四条全部满足时生效**：① 开启 prefix caching；② Mamba group 上启用 EAGLE/MTP 投机解码；③ `--prefix-match-unit` **小于** Mamba block size；④ 模型不用 multi-module MTP。示例：

`vllm serve <hybrid-model> --prefix-match-unit 64 --enable-mamba-shared-prefix-checkpoint`

`--prefix-match-unit`（前缀缓存键粒度）缺省时 = 各 prefix-cacheable KV group 块大小的**最大公约数**；对混合 Mamba 模型就等于块大小本身 → **没有子块边界、该 flag 无效果**。取值必须整除每个 cacheable group 的块大小，且是稀疏 MLA 等 per-state 压缩比的整数倍，启动时校验并报错指出冲突尺寸；**64 是合理起点**，块大小要从启动日志读。

> **审计含义**：Qwen3.5/Qwen3-VL 的多轮对话前缀命中**只在满块边界生效**，短前缀/块内分叉会 miss；要提升命中率需显式设 `--prefix-match-unit` 并配 MTP/EAGLE + `--enable-mamba-shared-prefix-checkpoint`。这同时把「投机解码」与「前缀缓存」耦合在一起——**单独调任一项都可能看不到收益**。

来源：[v0.33.0 qwen3_5.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/model_executor/models/qwen3_5.py)、[v0.33.0 qwen3_vl.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/model_executor/models/qwen3_vl.py)、[v0.33.0 gdn_attn.py](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/v1/attention/backends/gdn_attn.py)、[v0.33.0 supported_models.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/models/supported_models.md)、[v0.33.0 automatic_prefix_caching.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/automatic_prefix_caching.md)、[v0.33.0 cuda_graphs.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/cuda_graphs.md)、[torchtitan](https://github.com/pytorch/torchtitan/blob/main/torchtitan/models/qwen3_5/README.md)、[Raschka](https://sebastianraschka.com/llm-architecture-gallery/hybrid-attention/)、[executorch qwen3_5_moe](https://github.com/pytorch/executorch/blob/main/examples/models/qwen3_5_moe/model.md)。

---

## 6. vLLM 0.33 Prometheus 指标真实名称

原文：*"In v1, an extensive set of metrics are exposed via a Prometheus-compatible /metrics endpoint using the vllm: prefix"*。样例 `# HELP vllm:num_requests_running Number of requests in model execution batches.` / `# TYPE vllm:num_requests_running gauge`。

### 6.1 单请求延迟相关（审计主用）

| 指标（0.33 真实写法） | 类型 | 含义 |
| --- | --- | --- |
| `vllm:time_to_first_token_seconds` | Histogram | TTFT；可用 `--custom-histogram-buckets` 改桶 |
| `vllm:inter_token_latency_seconds` | Histogram | ITL，**每个流式 output 事件一个样本**（相邻输出的墙钟间隔） |
| `vllm:request_time_per_output_token_seconds` | Histogram | 请求级 TPOT = `(e2e - TTFT)/(output_tokens - 1)`，**输出 ≤1 token 记 0** |
| `vllm:e2e_request_latency_seconds` | Histogram | 端到端 |
| `vllm:request_prefill_time_seconds` | Histogram | prefill 时间 |
| `vllm:request_decode_time_seconds` | Histogram | decode 时间 |
| `vllm:request_queue_time_seconds` | Histogram | 排队时间 |
| `vllm:request_inference_time_seconds` | Histogram | 推理时间 |

⚠️ **ITL ≠ TPOT**，设计文档原文：*"vllm:inter_token_latency_seconds records one sample per streamed output event (the wall-clock gap between successive outputs), whereas vllm:request_time_per_output_token_seconds is recorded once per finished request as (end-to-end latency - TTFT) / (number of output tokens - 1). These two metrics differ when an output bundles multiple tokens (e.g. speculative decoding)"* → **评估单请求用 TPOT；要逐 token 间隔才用 ITL**。

⚠️ **亚 300ms SLO 必须自定义桶**：`usage/metrics.md` 原文 *"Use it, for example, to track sub-300ms latency SLOs with the request-phase histograms, whose smallest default boundary is 0.3s"*，例：`--custom-histogram-buckets '{"request_latency": [0.01,0.05,0.1,0.25,0.5,1.0,5.0,30.0]}'`。family key 映射：`request_latency`(e2e/queue/inference/prefill/decode)、`time_to_first_token`、`inter_token_latency`(ITL+TPOT)、`iteration_tokens`、`request_params_n`、`request_num_preemptions`、`request_tokens`、`kv_cache_residency`（需 `--kv-cache-metrics`）；未知 key 启动即报错，桶值须正、有限、严格递增。指标弃用策略：X.Y 弃用 → X.Y+1 隐藏（`--show-hidden-metrics-for-version=X.Y` 恢复）→ X.Y+2 删除。

### 6.2 缓存 / 并发 / 计数

| 指标 | 类型 | 含义 |
| --- | --- | --- |
| `vllm:prefix_cache_queries` | Counter | 前缀缓存查询数（token/块） |
| `vllm:prefix_cache_hits` | Counter | 前缀缓存命中数 |
| `vllm:num_requests_running` | Gauge | 运行中的请求（另有 `_swapped` / `_waiting`） |
| `vllm:kv_cache_usage_perc` | Gauge | KV cache 占用比例（0–1） |
| `vllm:cache_config_info{...enable_prefix_caching="False"...}` | Gauge | 缓存配置快照（**可直接确认 APC 是否开启**） |
| `vllm:prompt_tokens_total` / `vllm:generation_tokens_total` | Counter | 累计 prompt / 生成 token |
| `vllm:request_success_total{finished_reason=...}` | Counter | 完成数（stop/length/abort） |
| `vllm:admission_rejections_total` | Counter | 被队列限制拒绝的准入 |
| `vllm:request_prompt_tokens` / `vllm:request_generation_tokens` / `vllm:request_max_num_generation_tokens` / `vllm:request_params_n` / `vllm:request_params_max_tokens` / `vllm:request_prefill_kv_computed_tokens` / `vllm:request_num_preemptions` | Histogram | 请求维度分布 |
| `vllm:iteration_tokens_total` | Histogram | 每 engine_step 的 token 数 |
| `vllm:kv_block_lifetime_seconds` / `vllm:kv_block_idle_before_evict_seconds` / `vllm:kv_block_reuse_gap_seconds` | Histogram | KV 块驻留（`--kv-cache-metrics`） |
| `vllm:hisparse_host_block_*_seconds` | Histogram | host 池块驻留 |

**命中率 PromQL**：`rate(vllm:prefix_cache_hits[5m]) / rate(vllm:prefix_cache_queries[5m])`（**没有 `hit_rate` gauge**；INFO 日志每 5s 另打印最近 1k 次块查询的命中率）。

**投机解码聚合**：`vllm:spec_decode_num_drafts_total` / `vllm:spec_decode_num_draft_tokens_total` / `vllm:spec_decode_num_accepted_tokens_total`；legacy `vllm:spec_decode_draft_acceptance_rate` / `vllm:spec_decode_efficiency` / `vllm:spec_decode_num_accepted_tokens` / `vllm:spec_decode_num_draft_tokens` / `vllm:spec_decode_num_emitted_tokens`。

来源：[v0.33.0 design/metrics.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/metrics.md)、[v0.33.0 usage/metrics.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/usage/metrics.md)、[v0.33.0 acceptance_metrics.md](https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/acceptance_metrics.md)。

---

## 7. 给 GameScaling 审计的可操作核查清单（按优先级）

1. **确认 GPU 身份**：`nvidia-smi --query-gpu=compute_cap` → 5090 应为 **12.0**；在报告里把 sm_100 相关结论标为「不适用/需重测」。
2. **确认 vLLM 版本与 CUDA**：0.33.0 仅 CUDA 13.0 wheel；源码构建需 SM12x arch list。
3. **抓启动日志 4 个关键字**：`Selected <kernel> for <module>`（FP8 实际 GEMM）、`Capturing CUDA graphs (decode, FULL)` / `(mixed prefill-decode, PIECEWISE)`、`CUDAGraph Config Settings: - Mode: ... - Capture sizes: ...`（**混合模型是否被降级**）、`enable_prefix_caching="..."`（或用 `vllm:cache_config_info`）。
4. **命中率用两个 Counter 现算**，并同时看 INFO 日志的「最近 1k 次块查询」；若命中率异常低，查 `cache_salt` / `multi_modal_uuids` / `--prefix-caching-hash-algo` 是否跨进程可复现（建议 `sha256_cbor`）。
5. **多图多轮实测 `TTFT ~ 图片数`**：即使 `hits/queries≈1` 也可能有 ~19 ms/图的广播开销（#52040/#52041）；确认版本含 #52041。
6. **单请求延迟一律用 `vllm:request_time_per_output_token_seconds`（TPOT）+ `vllm:time_to_first_token_seconds`（TTFT）**，并配 `--custom-histogram-buckets` 把最小桶压到 0.01s 级别；不要把 `inter_token_latency_seconds` 当 TPOT（投机解码下会分叉）。
7. **逐请求接受率**：开 `--per-request-spec-decode-metrics summary`（n==1、流式需 `include_usage`），用 `mean_acceptance_length` 判断投机解码是否真的降了 ITL；接近 1.0 时应关掉该方法（纯增开销）。
8. **FP8 逐配置 A/B**：BF16 vs `fp8_per_tensor` vs 预量化 W8A8，各测 TTFT/TPOT P50/P99；同时确认是否误开 ViT FP8 动态缩放导致与 encoder CUDA Graph 互斥；**不要预设「量化更快」**。
9. **混合模型要联动调参**：`--prefix-match-unit 64 --enable-mamba-shared-prefix-checkpoint` 需与 EAGLE/MTP 同时开启才生效；单开无效。
10. **排除可疑配置**：`--enforce-eager`（牺牲稳态 decode）、`-O0/-O1`（未用默认 FULL_AND_PIECEWISE）、过大的 `max_num_batched_tokens`（ITL 变差）、CPU 核数不足（需 ≥ 2+N 进程）。

---

## 8. 来源链接汇总

**发布 / 硬件**
- https://ai-tldr.dev/releases/vllm-v0-33-0/
- https://newreleases.io/project/github/vllm-project/vllm/release/v0.33.0rc3
- https://github.com/vllm-project/vllm/pull/43814
- https://github.com/NVIDIA/cutlass/pull/3280
- https://github.com/cdcseacave/openMVS/issues/1248
- https://github.com/saladtechnologies/salad-cloud-docs/blob/5d2c2b7579d963f303985d7556cf3ac1a0098c49/container-engine/tutorials/machine-learning/pytorch-rtx5090.mdx

**Prefix caching / 多模态**
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/automatic_prefix_caching.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/prefix_caching.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/mm_processing.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/multimodal_inputs.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/configuration/optimization.md
- https://github.com/vllm-project/vllm/issues/52040
- https://app.semanticdiff.com/gh/vllm-project/vllm/pull/52041/overview

**CUDA Graph**
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/cuda_graphs.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/cuda_graphs_multimodal.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/config/compilation.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/v1/worker/gpu_model_runner.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/compilation/cuda_graph.py

**投机解码**
- https://docs.vllm.ai/en/latest/features/speculative_decoding/
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/README.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/n_gram.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/suffix.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/mtp.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/eagle.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/draft_model.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/speculative_decoding/acceptance_metrics.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/config/speculative.py

**FP8 / 量化**
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/llm_compressor/fp8.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/online.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/fp8_vit_attn.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/quantized_kvcache.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/features/quantization/README.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/model_executor/layers/quantization/fp8.py

**Qwen3.5 / 混合注意力**
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/model_executor/models/qwen3_5.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/model_executor/models/qwen3_vl.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/vllm/v1/attention/backends/gdn_attn.py
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/models/supported_models.md
- https://github.com/pytorch/torchtitan/blob/main/torchtitan/models/qwen3_5/README.md
- https://sebastianraschka.com/llm-architecture-gallery/hybrid-attention/
- https://github.com/pytorch/executorch/blob/main/examples/models/qwen3_5_moe/model.md

**指标**
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/design/metrics.md
- https://raw.githubusercontent.com/vllm-project/vllm/v0.33.0/docs/usage/metrics.md
