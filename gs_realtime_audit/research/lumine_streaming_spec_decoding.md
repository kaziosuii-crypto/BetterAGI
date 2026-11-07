# GameScaling 实时推理审计：Lumine 流式动作执行与语法感知投机解码调研报告

- 调研日期：2026-11-05
- 调研角色：GameScaling 实时推理审计项目调研子代理（受 lead 委派）
- 核实方式：web_search / web_fetch + Node 全局 fetch。本环境 web_fetch 对 github.com、raw.githubusercontent.com 报「URL hostname "github.com" resolves to a non-public IP address」，HTML 单次截断 100000 字符；故 GitHub 仓库文件走 cdn.jsdelivr.net/gh 与 data.jsdelivr.com，PR/Issue/Commit 正文走 .diff / .patch / HTML stripped。SemanticDiff 返回 429、api.github.com 返回 403，均已在来源表中标注。
- 诚实性声明：下文每个技术结论都附可核实 URL；凡只核实到标题/索引、未取到正文或数值的，明确标注「仅核实标题/出处」；抓取失败（404/429/502/403）也如实标注。本报告不访问 GPU 服务器、不跑任何推理，只产出这一个文件。

---

## 0. 先纠正三个容易混淆的前提（重要）

1. 论文 ≠ 仓库。「Streaming Action Execution」「无 draft 模型投机解码」出自 ByteDance Seed 的 Lumine 论文（arXiv:2511.08892）；任务点名的 zlc1004/Lumine 是第三方尝试复现（README 原文："Attempted implementation of Lumine-Agent by arXiv:2511.08892."）。核查其全量文件树后确认：serve 脚本只是普通 vLLM OpenAI server，没有 speculative_config、没有 guided/grammar、没有分号分块解析；Runner 反而是「同步串行 + latest-frame-only 丢帧」。直接 clone 该仓库得不到论文里的机制。
2. 任务中「MTP 需要 draft 权重」这一前提部分不准确。vLLM 0.33 文档明确 MTP「Unlike draft-model-based methods, you do not need to provide a separate draft model」，但「MTP only works for model families that support MTP in vLLM」。分两类：原生 MTP（多 token 预测头在目标模型权重内，如 MiMo-7B-Base）不需要额外 draft 模型；Gemma 4 assistant checkpoint 路径才需要额外 assistant 权重。详见 §3.2。
3. 25.3× 不是「流式 + 投机解码」单独带来的。论文 Figure 7 原文：「The overall optimization yielding a 25.3× speedup compared with the baseline」，且 Figure 7 标注含 "Infra-Opt"（剩余基础设施优化）。它是通信（JPEG+Base64、持久 TCP）、prefill（历史 KV 复用 + StreamingLLM + attention sink）、decoding（紧凑动作空间 + 无 draft 投机解码）、底层（TP=4×H20、W8A8 SmoothQuant、GEMM 调优、custom one-shot all-reduce、单个 CUDA graph 融合 forward+rejection sampling、图像预处理上 GPU）端到端叠加的结果。移植到 RTX 5090 单卡不能直接套用这个倍数。

---

## 1. 技术原理一：Lumine 的 Streaming Action Execution（流式动作执行）

### 1.1 项目与模型身份（已核实）
- 论文：Lumine: An Open Recipe for Building Generalist Agents in 3D Open Worlds，ByteDance Seed，2025，arXiv:2511.08892。
- 骨干：Qwen2-VL-7B-Base；原始像素 5 Hz（每 200 ms 一帧，对齐人类 200–250 ms 反应时），用 action chunking 自回归产出 30 Hz 文本键鼠动作。
- 上下文：动态保留最近最多 20 步作为短期记忆，推理步骤作为长期记忆（只留最新一条 reasoning，新 reasoning 出现时清上下文）。
- Hybrid Thinking：只在关键转折点产出 <|thought_start|>...<|thought_end|> 内心独白；连续 100 步未思考则强制思考。平均每段 reasoning 37.4±11.7 tokens。

### 1.2 动作格式与「动作组」划分（与 GameScaling 几乎同构）
动作被 <|action_start|> / <|action_end|> 包住，内部是分号分隔序列：

~~~
<|action_start|> ΔX ΔY ΔZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6 <|action_end|>
~~~

- ΔX ΔY：鼠标相对位移整数（−1000..1000），ΔZ：滚轮步数（−5..5）；整体在 200 ms 内平滑执行。
- 6 个按键组 K1..K6，每组对应一个 33 ms 可执行片段，6×33 ms ≈ 200 ms，即 30 Hz。每组 0–4 个键；未在组内列出的键自动释放；连续组中出现同一键则保持 keydown（不重复发按下）。样例：92 0 0 ; Shift W ; Shift W ; F W ; F。
- 分隔符即边界：空格（ΔX、ΔY 之后）、分号 ; （ΔZ、K1..K5 之后，每出现一个分号 = 一个完整 chunk）、<|action_end|>（K6 之后）。
- 与 GameScaling 对照：任务设定输出 <think></think><|action_start|>X Y Z ; g1..g6<|action_end|>，6 组每组 33 ms —— 与 Lumine 的 chunk 结构、分号语义、33 ms 组时长一一对应，只是把 <|thought_*|> 换成 <think></think>。格式同构是本项目可移植性的最大有利条件。

### 1.3 「动作块未完全生成就开始执行」的机制（任务重点 1）
论文 §6.2 Real-Time Optimization 原文（已核实）：

> "We employ a streaming output mechanism for timely action execution. Lumine autoregressively generates an entire action sequence composed of six continuous chunks, each corresponding to a 33 ms executable segment of keyboard and mouse operations. Once a complete chunk is produced, signaled by a terminating semicolon, it can be executed immediately without waiting for the full sequence to finish. This substantially relaxes the strict timing constraint: as long as each action chunk is generated within 33 ms, the remaining 200 ms interval can be fully utilized for processing stages."

机制拆解：
- 生成端维护一个增量解析器：每累积到一个终止分号就判定一个 chunk 完成，立即把这一组（33 ms 的键鼠）派发给执行器；不等 6 组全解码完、不等 <|action_end|>、不等 HTTP 响应整体结束。
- 时序收益：把「整段动作必须在 200 ms 内全部解出来」的硬约束，放松成「每个 chunk 各自在 33 ms 内解出来」，其余 200 ms 窗口可以全部用于 vision encoder / LLM prefill / 网络等重 stages。第一组动作的延迟只取决于第一组的解码，而非整段。

### 1.4 与「同步等待完整响应」的区别与代价（任务重点 1）
论文没有写「synchronous baseline」这个对照实验名，但 §6.2 措辞（"without waiting for the full sequence to finish"、"substantially relaxes the strict timing constraint"）、Figure 7 的 baseline 与 §8 的异步延迟讨论共同给出对比：

| 维度 | 同步等待完整响应 | Lumine 流式执行 |
| --- | --- | --- |
| 执行触发点 | 等 6 组全部解码 + <|action_end|> + 整段响应返回 | 每个分号即派发一组 |
| 首动作延迟 | 被整段 decode 拖累（典型 20-token 动作 × 每 token 3.1 ms ≈ 62 ms decode，叠加 prefill/网络后逼近甚至超过 200 ms 预算） | 只依赖第一组解码（论文 "First action chunk w/o reasoning 113.9 ms"） |
| 时间预算 | 200 ms 内要完成全部生成 | 每 chunk 33 ms 内生成即可，200 ms 留给重 stages |
| 观测新鲜度 | 生成期间环境冻结，观测与执行对齐 | 异步执行引入约 200 ms 观测滞后（stale observation） |
| 代价 | 低控制频率 / 错过时机 | 光标 overshoot、转向过冲；需历史上下文补偿（见 1.5） |

§6.2 延迟分解表（已核实精确值）：

| Stage | Time (ms) | Token | Forward Step |
| --- | --- | --- | --- |
| Network latency | 6 | - | - |
| Preprocessing | 6.8 | - | - |
| Vision encoder | 39 | 1196 | 1 |
| LLM prefill | 52 | 1209 | 1 |
| Decode latency per token | 3.1 | 1 | - |
| First action chunk w/o reasoning | 113.9 | 8.4 | 4.7 |
| First action chunk w/ reasoning | 234.0 | 46.8 | 43.1 |
| Action chunk (average) | 3.1 | 1.8 | 1.02 |
| Action chunk (max) | 12.4 | 5 | 4 |

原文注："Due to speculative decoding, the number of forward steps is typically smaller than the number of generated tokens." 关键结论：带 reasoning 的首块 234.0 ms > 200 ms 预算，这正是 Hybrid Thinking 必须只在少数步触发的原因；也是 GameScaling 必须限制 <think> 触发频率的直接依据。

### 1.5 过期观测 / stale observation 的处理（任务重点 1）
论文正文没有使用 "stale observation" 这个词；实际论述在实时条件下「有历史 / 无历史模型」对比段落（§8 附近，已核实原文）：

> "By contrast, non-history models are significantly affected by the 200ms delay introduced by asynchronous execution. Specifically, when a non-history model attempts to move the mouse to a designated position, the visual input it receives corresponds to the pre-execution state of the previous action. As a result, the model incorrectly believes that the cursor has not yet moved and generates another mouse movement action in the same direction. This leads to overshooting the target and makes precise clicking on GUI elements considerably more difficult. Since models with history have access to previous actions and trajectories in context, they are far less affected by latency and can perform operations much more smoothly and stably. The same phenomenon also occurs in navigation when turning directions."

处理方式的实质：
- 不是重新观测、不是取消/回滚重规划、不是给观测打时间戳丢弃；而是把历史动作与轨迹放进上下文（最多 20 步），让模型自己做「我已经发过这个动作了」的推断，从而抵消 200 ms 反馈滞后。
- 辅助手段：新 reasoning 出现时清空上下文（同时缓解 StreamingLLM 在长程任务上的性能退化，原文已核实）；Hybrid Thinking 控制 thinking 频率以避免 234 ms 超预算。
- 工程侧的「丢过期帧」是另一个概念：zlc1004/Lumine 的 Runner 用 "one latest-only slot"、"no queued ticks" 直接丢弃过期采集帧（不是丢弃观测语义），这恰恰与论文流式执行相反，见 §1.6。

### 1.6 仓库核查结论（已核实，含原文）
- 文件树（data.jsdelivr.com/v1/package/gh/zlc1004/Lumine@main）：README.md、Runner/README.md、run/vllm_proxy.py、run/sglang_proxy.py、run/serve.Lumine-Agent-Base-VL-7B.sh、run/install_vllm.sh、run/serve.sh、configs/*、DataInject/*、DataProcessor/*、KeyRecorder/*、quantize.py、train.sh 等。没有任何分号分块解析、streaming action execution、自定义投机解码的实现代码。
- run/serve.Lumine-Agent-Base-VL-7B.sh 关键行：python3 -m vllm.entrypoints.openai.api_server --model ./models/Lumine-Agent-Base-VL-7B --served-model-name lumine-agent-vl-7b --dtype bfloat16 --port 8000 --host 0.0.0.0 --trust-remote-code --max-model-len 16384 --disable-custom-all-reduce --enforce-eager —— 无 speculative_config、无 structured/guided 参数，且 --enforce-eager 与论文的 CUDA graph 相反。另提供 sglang（端口 30000）与 TGI 3.2.1 选项。
- run/vllm_proxy.py：aiohttp 转发，只按 /tokenize 结果裁剪 max_tokens、pop("thinking")、转发 /v1/chat/completions；run/sglang_proxy.py 做字段白名单过滤（含 "stream"）。run/install_vllm.sh 用 uv pip install vllm transformers accelerate ... --upgrade，未 pin 版本。
- Runner/README.md 原文："Capture samples at 5Hz with one latest-only slot; inference and playback are serialized, so slow endpoints reduce decision rate rather than queueing actions."；"Dry run parses and displays output but never calls SendInput."；"F8 works globally and cancels inference, stops execution and releases held inputs."；OBS websocket v5 取 1280x720 JPEG、"one latest-frame slot, no queued ticks and no overlapping screenshot calls"；30 次 Linux loopback mock 平均 14.75 ms / p95 17.33 ms。→ Runner 是同步串行、丢过期帧，不是论文的流式执行。
- 模型 HF repo：koboshchan/Lumine-Agent-Base-VL-7B（由仓库 README 引用，间接来源，未单独抓取）。

---

## 2. 技术原理二：Grammar-aware / 语法约束投机解码

### 2.1 Lumine 的做法：无 draft 模型、利用固定分隔符的确定性（最直接可移植）
论文 §6.2 Decoding 段原文（已核实）：

> "we implement a draft-model-less speculative decoding strategy [leviathan2023fast] to further reduce token decoding, leveraging fixed delimiters across generation stages. Specifically, we observe that ΔX and ΔY end with a space, ΔZ and K1,⋯K5 end with a semicolon, and K6 ends with the <|action_end|> token. We use a simple state variable to track the generator's stage to dynamically select the appropriate delimiter as a draft token. Standard reject sampling is then applied to ensure the final output matches the same distribution."

原理：
- 输出格式是确定性状态机：数值字段后必然是空格/分号，第 6 组后必然是 <|action_end|>。这些位置的下一个 token 无熵（zero-entropy / forced token），因此不需要 draft 模型，用一个 stage 变量即可 100% 正确地「猜」出分隔符，作为 draft token 提交给 verifier。
- 用标准拒绝采样（Leviathan et al. 2023）保证最终输出分布与不用投机时一致 —— 这是它区别于后来「局部掩码」方案的关键（见 2.4）。
- 与 llguidance 的 fast-forward / jump-forward 本质相同。fast_forward.md 原文（已核实）："Fast-forward tokens (also called zero-entropy tokens, forced tokens, fixed tokens, or jump tokens) are tokens that are added to the current sequence in one step due to the grammar constraint... This is similar to speculative decoding, except that the speculation is 100% correct." 例：JSON schema 先强制 {"name":"、模型生成 John"、再强制 ,"age":。
- 对固定格式的确定性利用：只在「分隔符 / 固定片段」位置可猜，数值与按键内容仍由模型自回归生成；但恰好这些分隔符出现频率高（每 chunk 至少一个），省掉的 forward step 很可观 —— 论文实测 8.4 tokens 只需 4.7 forward steps、平均 chunk 1.8 tokens 只需 1.02 forward steps。
- 接受率与开销：确定性位置的接受率理论 = 100%，无 draft 权重、无额外显存、无额外模型 forward；开销仅为一个状态变量 + 拒绝采样 + 一次并行 verify。缺点是可投机位置受格式约束上限（分隔符只占少数 token），且必须与专用 token ID 对齐（见 §2.3 边界风险）。
- 代价/风险：llguidance 文档警告 non-canonical forcing —— 若强推的字节串不是 tokenizer 的 canonical 切分，后续 token 分布会被扭曲（甚至永远产空格）；llguidance 用回查最多 4 个尾部 token 找跨越边界且 grammar 允许的更长 token 来裁剪，maskbench 上 max_tokens>=2 无 non-canonical，但 max_tokens==0 时 97% forced 序列 non-canonical。Lumine 的分隔符是训练时固定 token，属于 canonical，风险低；GameScaling 若自定义格式需专门验证。

### 2.2 vLLM 社区的 Grammar-aware Speculative Decoding（PR #47885）
来源：https://github.com/vllm-project/vllm/pull/47885.diff 与 commit b0781c3 的 .patch（作者 jmamou / Intel，2026-07-07，标题 "feat(spec_decode): Grammar-aware draft token sampling for structured outputs"，599 insertions，4 文件）。

原理（commit message 原文）：当 structured output（JSON schema / regex / grammar）与投机解码同时开启时，在 worker 进程维护一个 shadow xgrammar.GrammarMatcher 指导 draft token 选择；若 argmax token 违反语法，回退到 top-K=32 候选中第一个语法合法 token。改动文件：tests/v1/spec_decode/test_shadow_grammar.py（+185）、vllm/v1/spec_decode/llm_base_proposer.py（+48）、vllm/v1/spec_decode/shadow_grammar.py（+267）、vllm/v1/worker/gpu_model_runner.py（+101）。shadow_grammar.py 用 ShadowGrammarManager / ShadowGrammarState 维护 per-request GrammarMatcher（max_rollback_tokens=num_speculative_tokens），做 fill_next_token_bitmask + apply_token_bitmask_inplace 约束 draft logits、advance_with_accepted_tokens、rollback。

效果（已核实数值，Qwen2.5-Coder-3B + Qwen2.5-0.5B，JSON schema）：CPU Xeon 8580 TPOT 434.2→398.9 ms（−8.9%，t=3.11，p<0.05）；GPU RTX A6000 101.3→92.9 ms（−9.1%）；"Top-K=4 achieves 100% coverage of grammar-critical positions"。定位：消除 verifier 必然因语法拒绝的浪费验证轮，收益约 9% TPOT，不是数量级提升。

### 2.3 vLLM 里该组合仍是「已知缺口 / 在研」（关键结论）
Issue #62130 "[Spec Decode] Compose grammar-constrained sampling with EAGLE speculative decoding"（2026-10-17 开，状态 Open，labels: speculative-decoding / structured-output）原文第一句（已核实）：

> "Speculative decoding and grammar-constrained sampling (structured_outputs, guided decoding) do not compose in vLLM today."

实测（vLLM 0.11.2 + xgrammar + Qwen2.5-7B-Instruct，A100，batch=1，agentic JSON，K=5）：
- unmasked EAGLE3 接受 1.44 draft tokens/propose，但 "0.97× slower than no speculation"（比不投机还慢）；
- 早先 n-gram 实验 84.9% drafted tokens 被 grammar FSM 拒绝（292/344）—— 说明无 logits 的 proposer 与语法约束组合最差；
- 提出的 grammar-masked draft logits（draft loop 每位置 compute_logits→apply bitmask→argmax）实测：no_spec TPOT 11.09±0.01 ms；EAGLE3 unmasked 11.47±0.04 ms（1.443 tok/propose，0.967×）；EAGLE3+grammar mask 9.61±0.03 ms（1.969 tok/propose，+36.5%，1.154×）；"39.1% of draft-position argmaxes change under the mask (1756/4486 positions)"；K=3 1.15× / K=5 1.19× / K=7 1.17×，"best absolute: K=3 grammar, 1.25× vs no_spec"。
- 第二个 enum-heavy tool-call schema：no_spec 5/8 valid、11.02 ms；unmasked 3/8 valid、13.54 ms、1.203 tok/propose；masked 5/8 valid、10.37 ms（1.31×）、1.807（+50.2%）。
- 文中还列出 open issues：#52620（concurrent structured-output + ngram_gpu spec decode 返回 HTTP 500）、#55313（XGrammar desync 吐 trailing spaces）。
- 兼容性矩阵历史：PR #12373.diff 把兼容性矩阵某格由 ✅ 改成 ✗ 并指向 issue #11484 "[Bug]: xgrammar crashes with speculative decoding"（2024-12-25，Closed as not planned，labels: bug / stale / structured-output）。

边界 bug（与 GameScaling 的 <think></think> 直接同类）：PR #43424.diff "[V1][Bugfix] structured_output × spec-decode: pre-commit grammar filter for boundary-step bonus tokens" 注释原文：reasoning_ended 在边界被观测到之前为 False，导致该步 verifier 的 bonus token 没有语法掩码，需在 commit 前截尾（precommit_filter_tokens() / _find_reasoning_end_in_tokens()），否则 FINISHED_ERROR 并要回退 num_computed_tokens / num_output_placeholders。相关：PR #44993（Merged）、#44297（reasoning boundary 的 bitmask 与 grammar advance 裁剪）。中文解析（仅核实为二手概述）：https://www.shaqiu.cn/tracking/engineering:vllm-project:vllm:grammar-speculation-distribution-20261028 称此前过早裁掉非法草稿 token 会造成采样分布偏移，修正后 H100/Qwen3-4B 同步约 2% 吞吐损失、异步高并发 2–3% 提升。

### 2.4 学术界对该组合的两点定性（已核实）
- 分布一致性陷阱：arXiv:2605.07698 "Future Validity is the Missing Statistic: ... Grammar-Faithful Speculative Decoding"（2026）摘要原文：任何带 local mask access + Leviathan rejection + rollback 的投机解码器，采样的是 locally projected distribution μ-proj 而非 grammar-conditional μ-star；在 Dyck 文法 + Qwen3-8B 上 TV gap 可达 0.996。OneStep 使 Dyck TV 降 14%（<1% 吞吐开销），精确 DP 降 97%，有限语言修正把 JSON gap 压到数值精度。→ 对控制任务意味着「语法 + 投机」可能悄悄改变动作分布，必须做闭环 A/B。
- NAACL 2025 CDSL（Constrained Decoding with Speculative Lookaheads，aclanthology 2025.naacl-long.239，摘要已核实）：用 draft model 做 lookahead roll-out 加速约束解码，是「约束解码 + 投机」的另一条学术线。
- 其它索引级来源（仅核实标题/出处或摘要，未逐项核实正文数值）：SpecHub（aclanthology 2024.emnlp-main.1148）；STAND / Accelerated Test-Time Scaling with Model-Free Speculative Sampling（arXiv:2506.04708v3，摘要已核实：model-free stochastic n-gram drafting + Gumbel-Top-K，AIME-2024 / GPQA-Diamond / LiveCodeBench 上比标准自回归降 60–65% 延迟）；ASPIRE（export.arxiv.org/pdf/2609.17943）；CASD: A Contract-Aware Speculative Decoding Method for Constrained Output Generation（2026 IEEE SSE，ieeexplore 11664971 / computer.org 2joOwwfu2Ji，IEEE 页面未取到正文数值，仅核实标题与出处；openreview PDF 提及与 EAGLE-2 / LLaMA3.1-8B-Instruct / UZGentRAG、SQuAD 对比）。

### 2.5 TensorRT-LLM 已实现组合（对照，说明工程上可行但复杂）
NVIDIA + XGrammar 团队博客（已核实全文）：guided decoding = grammar compile → mask gen → grammar advance → apply mask；语法后端在 CPU，"can be overlapped with the GPU computation, achieving near-zero overhead"。两个挑战原文：

> "For the target model, guided decoding should advance the grammar state and generate the mask for every draft token. If some draft tokens are rejected, guided decoding should rollback the grammar state to the last accepted token. For the draft model, without grammar constraints, some draft tokens may violate the grammar and thus be forcefully rejected in the verification step. Clearly, this hurts the acceptance rate."

并明确 "Some drafting algorithms work without logits sampling, which require other ways to apply the grammar constraints"（正是 n-gram / suffix 的痛点）。该博客称已支持 DeepSeek-R1 + guided decoding + overlap scheduler + CUDA graph + ADP + MTP + disaggregated serving 全开。→ 业界已有可行实现，但 vLLM 0.33 尚未提供同等能力。

### 2.6 接受率与开销小结
| 方案 | 确定性利用 | 接受率 | 额外开销 | vLLM 0.33 现成？ |
| --- | --- | --- | --- | --- |
| Lumine 式固定分隔符 draft（=fast-forward/jump-forward） | 分隔符位置 100% 确定 | 这些位置 100% | 状态变量 + 拒绝采样；无 draft 权重 | 无现成；可用 custom_class proposer 或外部 forced-token 实现 |
| 语法掩码 draft model / EAGLE（PR #47885、#62130） | mask 改变 39.1% 位置 argmax | 1.44→1.97 tok/propose（+36.5%）；TPOT 1.154×–1.31× | shadow GrammarMatcher + bitmask + rollback；CPU 计算 | 无现成，PR/issue 在研 |
| n-gram / suffix + grammar | 无法介入（无 logits） | 最差：84.9% draft 被 FSM 拒绝 | 低 | 方法本身有，组合有缺口 |
| 单独 grammar/constrained decoding（不投机） | 全量约束 | 不适用 | CPU bitmask（可与 GPU 重叠至近零） | 有（structured_outputs） |

---

## 3. 技术原理三：vLLM 0.33 的投机解码与 guided/grammar decoding 现状

### 3.1 版本核实
- PyPI：https://pypi.org/pypi/vllm/0.33.0/json → version=0.33.0，requires_python <3.15,>=3.11，依赖 transformers<5.21.0,>=5.16.1、huggingface_hub>=1.31.0、requests>=2.33.0、certifi>=2026.10.19。
- 发布摘要 https://ai-tldr.dev/releases/vllm-v0-33-0/（2026-11-01，927 commits / 389 contributors）：投机解码是改动重点（DFlash/DSpark block-diffusion drafters "no longer reserve draft slots"，DeepSeek-V4.1-Flash+DSpark on GB300 1373→3390 output tok/s）；新增 FLy lossy verification（1.04–1.31× on MI355X/B300）；CUDA 13.0-only wheels、PyTorch 2.14；移除 Medusa 与 MLP speculator、Ray compiled-graph executor；LM Format Enforcer 在 0.33.0 deprecated。GitHub release tag v0.33.0 存在（khluu tagged 01 Nov 09:21，commit 4ba787e），但 HTML changelog 动态加载未取到。
- 版本化文档 https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/ 页脚 "October 23, 2026"，已核实正文。

### 3.2 投机解码支持清单（全部来自 v0.33.0 官方文档，已核实）
方法：EAGLE / LiLiCorr / MTP / Draft Model / PARD / N-Gram / Suffix Decoding / Hidden State Extraction / Custom Proposer (Experimental) / Dynamic Speculative Decoding / Adaptive Verification / FLy Verification (Lossy) / Per-Request Acceptance Metrics。

- 定位原文："model-based methods such as EAGLE, MTP, draft models and PARD provide the best latency reduction, while simpler methods such as n-gram and suffix decoding provide modest speedups without increasing workload during peak traffic."；文档开头："reduce inter-token latency under medium-to-low QPS, memory-bound workloads"。
- N-Gram：method="ngram"，键 prompt_lookup_max / prompt_lookup_min（默认均 5）；Method Selection 表标 "Low to medium gain / Medium gain / Lightweight and easy to enable"。v0.33.0 的 n_gram 子页给出离线例子 --speculative-config '{"method":"ngram","num_speculative_tokens":5,"prompt_lookup_max":4}'（子页已核实）。无需 draft 权重。
- Suffix Decoding：键 suffix_decoding_max_tree_depth（默认 24）、suffix_decoding_max_cached_requests（默认 10000）、suffix_decoding_max_spec_factor（1.0）、suffix_decoding_min_token_prob（0.1）；Method Selection 表标 "No extra draft model; dynamic speculation depth"。v0.33.0 的 /suffix_decoding/ 子页返回 404，但主页面明确列出该方法（如实标注）。
- MTP（https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/mtp/ 已核实）："MTP is a speculative decoding method where the target model includes native multi-token prediction capability. Unlike draft-model-based methods, you do not need to provide a separate draft model."；"MTP only works for model families that support MTP in vLLM"；示例 XiaomiMiMo/MiMo-7B-Base：--speculative-config '{"method":"mtp","num_speculative_tokens":1}'；Gemma 4 assistant checkpoints 需要 method="mtp" + model=<assistant 权重>；"If your model does not support MTP, use another method such as EAGLE or draft model speculation." → 纠正任务前提：MTP 不必然需要单独 draft 权重，但要求模型家族自带 MTP 头（Qwen2-VL-7B 类不在支持列表内）。
- Custom Proposer Backend (Experimental) —— 移植 Lumine 状态机的最直接落点，文档原文：'You can plug in your own custom proposer class for speculative decoding by setting the method to custom_class and providing the full module path to your class. Your custom class must accept a VllmConfig upon instantiation and implement a propose method.' 配置示例：speculative_config.method = "custom_class"、speculative_config.model = "your_module.YourCustomProposerClass"。Method Selection 表标 "Bring your own proposer class (experimental)"。
- 通用键：method、model、num_speculative_tokens、draft_tensor_parallel_size、max_model_len、parallel_drafting（仅 EAGLE/draft-model）、rejection_sample_method（standard/synthetic/block/fly）、draft_sample_method（greedy/probabilistic）。
- Lossless 与不兼容：标准拒绝采样理论 lossless（仅浮点差异）；FLy 故意 lossy 不保分布；vLLM 不保证 logprobs 稳定。"Pipeline parallelism is not composable with speculative decoding as of vllm<=0.15.0"、"Speculative decoding with draft models is not supported in vllm<=0.10.0"。

### 3.3 guided / grammar decoding 现状（v0.33.0，已核实）
- https://docs.vllm.ai/en/v0.33.0/features/structured_outputs/ 原文："vLLM supports the generation of structured outputs using xgrammar or guidance as backends."；后端 auto / xgrammar / guidance（也可 outlines）；请求参数 choice / regex / json / grammar / structural_tag；旧 guided_* 字段在 v0.12.0 移除，改为 structured_outputs。LM Format Enforcer：0.33.0 deprecated（警告可用）、0.34.0 需显式 opt-in、0.35.0 移除。默认 --structured-outputs-config.backend auto。
- 对 GameScaling 最关键的官方警告 "Custom grammars and parser boundary tokens"：reasoning / tool-call parser 按 token ID 识别边界，纯文本 EBNF 字面量可能用普通词表 token 拼出「看起来一样」的解码文本却不是专用 token ID，导致 tool_call / 边界不触发；必须用 XGrammar token-level structural-tag 元素 {"type":"token","token":"</think>"} 而不是 {"type":"const_string","value":"</think>"}，并用 return_token_ids=true 核对边界 ID；文档给出 Qwen3 streaming parser 识别 <think>/</think>/<tool_call>/</tool_call> 的例子。另有 --structured-outputs-config.enable_in_reasoning=True（Qwen3 Coder v0.11.2+）。
- 组合现状（本报告核心判断）：v0.33.0 的 speculative_decoding 文档正文中 "grammar" 出现 0 次、"structured" 只出现在侧栏导航；structured_outputs 文档中 "speculative" 也只出现在侧栏导航。即 0.33.0 官方文档没有 grammar-aware 投机解码章节，也没有 spec-decode × structured-outputs 的组合说明。结合 §2.3 的 open issue / PR，结论是：截至 vLLM 0.33.0，二者组合仍是已知缺口 / 在研，不是可依赖的现成特性。0.33 发布说明里的投机解码改动（DFlash/DSpark、FLy）也不包含 grammar-aware。

---

## 4. 移植到 GameScaling 的可行性判断与风险（任务重点 4）

项目设定：输出 <think></think><|action_start|>X Y Z ; g1..g6<|action_end|>，6 组每组 33 ms，RTX 5090 单卡 / vLLM 0.33。

### 4.1 逐项可行性矩阵
| 技术 | 可行性 | 依据与主要风险 |
| --- | --- | --- |
| 分号分块 + 流式增量执行 | 高 | 格式与 Lumine 同构（§1.2）；但 vLLM OpenAI API 的 stream 只按 delta 推 token，不会替你执行动作，需在客户端/代理层自研「累积到分号即 dispatch 一组 33 ms」的解析器。zlc1004 的 vllm_proxy.py 只是转发，无分块逻辑。 |
| 固定分隔符投机解码（Lumine 式） | 中-高，需自研 | 走 vLLM 0.33 custom_class proposer（Experimental）实现 stage 状态机；或退到外部 llguidance/xgrammar forced-token fast-forward。风险：实验接口、V1/V2 runner 差异、拒绝采样分布一致性、<think>/<|action_end|> 必须是专用 token ID（§3.3）。 |
| n-gram / suffix | 中 | 零额外权重、易启用；但 GameScaling 动作是数值+按键组合，重复度可能低；且无 logits 的 proposer 与 grammar 组合最差（issue #62130：84.9% 被 FSM 拒绝）。 |
| MTP | 低（除非换模型） | Qwen2-VL-7B 类不在 vLLM MTP 支持家族；任务设定未给 MTP 权重；Gemma4 assistant 路径要额外权重。 |
| guided / grammar decoding（单独） | 高 | structured_outputs / xgrammar 现成；但 boundary token 必须用 structural-tag + token 元素 + return_token_ids 校验。 |
| spec-decode × structured-outputs 组合 | 低（当前不可用） | v0.33 官方文档无该组合，#62130/#52620/#55313/#11484 等在研或崩溃/偏移问题。 |
| stale observation 补偿（历史上下文） | 高，但需上下文/训练工程 | 把最近 ~20 步(观测,动作)进上下文；hybrid thinking 只在转折点；不能靠重新观测解决 200 ms 滞后（§1.5）。 |
| 33 ms / 200 ms 硬实时预算 | 需实测 | 6×33 ms = 198 ms ≈ 200 ms；论文带 reasoning 首块 234 ms 已超预算。RTX 5090 单卡 ≠ 论文 4×H20 TP=4 + W8A8 + 定制 kernel/CUDA graph；论文每 token 3.1 ms 不可直接套用。 |

### 4.2 风险清单（按优先级）
1. 论文/仓库混淆：zlc1004/Lumine 是第三方复现，serve 脚本是普通 vLLM + --enforce-eager，Runner 是同步串行 + latest-frame-only 丢帧；把它当作「已实现流式/投机解码」会得出完全错误的审计结论。
2. 组合缺口：vLLM 0.33 无官方 grammar-aware spec decode；同时开 structured_outputs + spec decode 有崩溃（#11484 closed as not planned）、HTTP 500（#52620）、XGrammar desync trailing spaces（#55313）、分布偏移等已知问题。
3. 边界 token 双重陷阱：(a) <think></think> / <|action_start|> / <|action_end|> 必须是专用 token ID，XGrammar 字面量会拼出「看起来一样但 ID 不同」的文本导致边界不触发；(b) reasoning 结束那一步 verifier 的 bonus token 没有语法掩码，会触发 FINISHED_ERROR（PR #43424），需要 pre-commit filter 截尾。
4. 分布一致性：local mask + Leviathan rejection + rollback 采样的是 locally projected 而非 grammar-conditional 分布（arXiv:2605.07698，Dyck TV 可达 0.996；issue #62130 亦明确）。控制任务对动作分布敏感，必须用闭环成功率 A/B 验证，不能只看 TPOT。
5. stale observation / overshoot：流式执行必然带来约 200 ms 观测滞后；无 history 会鼠标 overshoot、导航转向过冲（§1.5 原文）。评估指标应是闭环任务成功率与过冲率，不是单步 token 延迟。
6. 硬件/系统差距：25.3× 是端到端叠加（§0.3），包含 TP=4、W8A8、定制 all-reduce、单个 CUDA graph 融合 forward+rejection sampling、图像预处理上 GPU、持久 TCP。RTX 5090 单卡缺这些时，prefill/decode 分解和 p99 抖动会显著不同。
7. FLy / logprobs：FLy 故意 lossy 破坏分布，控制任务建议保持 rejection_sample_method="standard"；vLLM 不保证 logprobs 稳定，勿依赖 logprobs 做控制判据。
8. 环境门槛：vLLM 0.33 是 CUDA 13.0-only wheels + PyTorch 2.14，RTX 5090 驱动/镜像需匹配；0.33 移除了 Medusa/MLP speculator，旧配置会失效。
9. 33 ms p99：vLLM 调度/批处理/SSE 抖动可能吃掉预算；必须测 p99 而非均值，流式解析+执行放客户端侧以避开网络 RTT（论文网络延迟仍有 6 ms）。

### 4.3 对 GameScaling 的具体建议（渐进路线）
- 阶段 0（零模型改动，先拿到基线）：客户端开启 stream，做「增量解析 + 分号 dispatch」：累积 delta，每遇到 ; 立即把该组（33 ms 键鼠）送执行器，遇到 <|action_end|> 收尾；同时记录同步等待完整响应的 baseline 首动作延迟 p50/p99 作为对照。用 return_token_ids=true 核对 <think> / <|action_start|> / <|action_end|> 的真实 token ID。此阶段不依赖 vLLM 任何投机特性，风险最低。
- 阶段 1（锁格式，不碰组合）：单独启用 structured_outputs 的 grammar/regex（xgrammar 后端），把动作格式锁死；边界一律用 structural-tag 的 {"type":"token","token":"..."} 元素而非 const_string。暂时不要同时开 spec decode，避开 0.33 组合缺口。
- 阶段 2（低成本投机）：先试 method="ngram"（prompt_lookup_max/min）并用 Per-Request Acceptance Metrics 记录接受率与 TPOT；若数值/按键重复度低导致接受率差，再试 suffix。记录是否出现比不投机更慢的情况（issue #62130 的 0.97× 现象）。
- 阶段 3（移植 Lumine 式确定性分隔符）：优先二选一 —— (a) 用 vLLM 0.33 custom_class proposer 实现 stage 状态机：ΔX/ΔY 后 draft 空格、ΔZ/g1..g5 后 draft ;、g6 后 draft <|action_end|>，保持 rejection_sample_method="standard"；(b) 若 custom_class 实验接口不稳，退到 llguidance / xgrammar 的 fast-forward forced tokens，并专门验证 canonical tokenization（避免 non-canonical forcing 扭曲分布）。两种都不要用 FLy；都需验证与「专用 token ID 边界」的对齐。
- 阶段 4（闭环质量）：把最近约 20 步(观测, 动作)进上下文；hybrid thinking 只在关键转折触发（带 reasoning 首块 234 ms 超 200 ms 预算）；评估闭环任务成功率、鼠标 overshoot / 导航过冲率，而非仅 token 延迟。新 thinking 出现时按论文做法清上下文以缓解长程退化。
- 阶段 5（可选，等上游）：待 vLLM 合并 PR #47885 或关闭 issue #62130 后，再评估 grammar-masked EAGLE；MTP 仅在换成原生 MTP 模型家族时才考虑，当前 Qwen2-VL 类不适用。TensorRT-LLM 的 guided+spec 组合可作为「将来若换推理栈」的对照方案。
- 明确不做：不要把 zlc1004/Lumine 的 serve 脚本/Runner 当作已实现流式或投机解码；不要在组合 issue 关闭前同时开 structured_outputs + spec decode；本次审计不访问 GPU 服务器、不跑推理。

### 4.4 一句话结论
流式「分号分块即执行」与「固定分隔符确定性投机解码」在原理上高度可移植（GameScaling 输出格式与 Lumine 几乎同构，且 vLLM 0.33 提供 custom_class proposer 作为落点），但 vLLM 0.33 没有现成的 grammar-aware speculative decoding，spec-decode × structured-outputs 仍是官方已知缺口/在研；建议按「客户端流式解析 → 单独用 structured_outputs → 自研状态机 proposer（或 forced-token fast-forward）→ 历史上下文补偿 stale observation」渐进落地，并对边界 token ID、采样分布一致性、33 ms p99 预算、以及 RTX 5090 单卡与论文 4×H20 系统级优化的差距做专项验证。

---

## 5. 来源 URL 列表（分类；标注核实程度）

### A. Lumine 论文 / 项目 / 仓库（正文与数值已核实，除标注外）
| URL | 说明 |
| --- | --- |
| https://arxiv.org/abs/2511.08892 | 论文摘要页（Lumine, ByteDance Seed, 2025） |
| https://arxiv.org/html/2511.08892v1 | 论文 HTML 全文（§6.2、§8、Table、Figure 7 均取自此，多次抓取，stripped ≈144.7k 字符） |
| https://www.lumine-ai.org/ | 项目官网 |
| https://cdn.jsdelivr.net/gh/zlc1004/Lumine@main/README.md | 第三方复现仓库 README，"Attempted implementation..." 原文 |
| https://data.jsdelivr.com/v1/package/gh/zlc1004/Lumine@main | 仓库全量文件树（核实无相关实现） |
| https://cdn.jsdelivr.net/gh/zlc1004/Lumine@main/Runner/README.md | Runner 同步串行 / latest-only / dry-run / F8 / OBS 原文 |
| https://cdn.jsdelivr.net/gh/zlc1004/Lumine@main/run/serve.Lumine-Agent-Base-VL-7B.sh | vLLM serve 命令（无 spec/guided） |
| https://cdn.jsdelivr.net/gh/zlc1004/Lumine@main/run/vllm_proxy.py | 转发代理（无分块解析） |
| https://cdn.jsdelivr.net/gh/zlc1004/Lumine@main/run/sglang_proxy.py | sglang 代理字段白名单 |
| https://cdn.jsdelivr.net/gh/zlc1004/Lumine@main/run/install_vllm.sh | 未 pin 版本的安装脚本 |
| https://snowwarri0r.github.io/llm-wiki/papers/lumine.html | 中文解析（二手参照） |
| https://qiita.com/pocokhc/items/91ea66dc80043625d175 | 日文解析：prompt 格式、上下文 n=6 等（二手参照） |
| https://cloud.tencent.cn/developer/article/2591929 | 腾讯云周报（二手参照） |
| https://huggingface.co/koboshchan/Lumine-Agent-Base-VL-7B | 模型 HF repo（由仓库 README 引用，间接来源，未单独抓取） |
| http://www.gamelook.com.cn/2025/11/582138 | 抓取返回 502，未取得正文（如实标注） |

### B. vLLM 官方文档 / 版本 / 发布（正文已核实，除标注外）
| URL | 说明 |
| --- | --- |
| https://docs.vllm.ai/en/latest/features/speculative_decoding/ | latest 投机解码总页（方法表、配置键、Lossless、Incompatibility） |
| https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/ | v0.33.0 投机解码总页（正文 "grammar"=0 次；Custom Proposer 原文） |
| https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/n_gram/ | v0.33 n-gram 子页与离线例子 |
| https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/mtp/ | v0.33 MTP 子页（"you do not need to provide a separate draft model"） |
| https://docs.vllm.ai/en/latest/features/speculative_decoding/mtp/ | latest MTP 子页（同上） |
| https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/suffix_decoding/ | 返回 404（主页面仍列出 Suffix Decoding，如实标注） |
| https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/lilicorr/ 、 https://docs.vllm.ai/en/v0.33.0/features/speculative_decoding/acceptance_metrics/ | 确认存在（未逐字细读） |
| https://docs.vllm.ai/en/latest/features/structured_outputs/ | latest 结构化输出（xgrammar/guidance、boundary token 警告） |
| https://docs.vllm.ai/en/v0.33.0/features/structured_outputs/ | v0.33.0 结构化输出（同上；无组合说明） |
| https://docs.vllm.ai/en/latest/api/vllm/config/structured_outputs/ 、 https://docs.vllm.ai/en/v0.11.2/api/vllm/config/speculative/ | 检索发现的配置 API 页（仅索引，未逐项核实） |
| https://docs.vllm.ai/en/v0.14.0/features/spec_decode/ 、 https://docs.vllm.ai/projects/vllm-metal/en/latest/speculative_decoding/ 、 https://docs.vllm.ai/projects/ascend/zh-cn/v0.23.0/user_guide/feature_guide/speculative_decoding.html | 其它版本/移植版文档（仅索引） |
| https://docs.vllm.ai/en/v0.33.0/features/compatibility_matrix/ 、 https://docs.vllm.ai/en/latest/features/compatibility_matrix/ | 返回 404（改用 PR #12373.diff 核实矩阵变更） |
| https://pypi.org/project/vllm/0.33.0/ 、 https://pypi.org/pypi/vllm/0.33.0/json | 版本与依赖元数据（已核实） |
| https://github.com/vllm-project/vllm/releases/tag/v0.33.0 | release tag 存在（khluu, 01 Nov 09:21, 4ba787e）；changelog 动态加载未取到 |
| https://ai-tldr.dev/releases/vllm-v0-33-0/ | 第三方发布摘要（2026-11-01，927 commits、CUDA 13.0-only、FLy、移除项） |

### C. Grammar-aware / 语法约束投机解码（数值已核实，除标注外）
| URL | 说明 |
| --- | --- |
| https://github.com/vllm-project/vllm/pull/47885.diff | PR "Grammar-aware draft token sampling for structured outputs" 全文 diff |
| https://github.com/vllm-project/vllm/commit/b0781c3aaa69ddebcda9cb218a7195797431ec2e.patch | 同 PR commit patch（commit message、599 insertions、数值） |
| https://app.semanticdiff.com/gh/vllm-project/vllm/pull/47885/overview | 返回 429 Ratelimit Exceeded（改用 .diff/.patch） |
| https://github.com/vllm-project/vllm/issues/62130 | "Compose grammar-constrained sampling with EAGLE"（Open，全部实测数值、"do not compose in vLLM today"） |
| https://github.com/vllm-project/vllm/pull/43424.diff | reasoning boundary bonus token 的 pre-commit filter fix |
| https://app.semanticdiff.com/gh/vllm-project/vllm/pull/44993/overview 、 https://app.semanticdiff.com/gh/vllm-project/vllm/pull/44297/overview | reasoning boundary 相关 PR（仅索引/标题；SemanticDiff 曾 429） |
| https://github.com/vllm-project/vllm/pull/12373.diff | 兼容性矩阵 ✅→✗（指向 issue 11484） |
| https://github.com/vllm-project/vllm/issues/11484 | "[Bug]: xgrammar crashes with speculative decoding"（Closed as not planned） |
| https://www.shaqiu.cn/tracking/engineering:vllm-project:vllm:grammar-speculation-distribution-20261028 | 中文二手解析：分布偏移修正（仅二手概述） |
| https://nvidia.github.io/TensorRT-LLM/blogs/tech_blog/blog12_Combining_Guided_Decoding_and_Speculative_Decoding.html | NVIDIA+XGrammar 组合博客全文（两挑战、near-zero overhead） |
| https://nvidia.github.io/TensorRT-LLM/1.4.0rc2/blogs/tech_blog/blog12_Combining_Guided_Decoding_and_Speculative_Decoding.html | 同博客版本化镜像（web_search 返回） |
| https://cdn.jsdelivr.net/gh/guidance-ai/llguidance@7ca3404aaf4ba458e03448619790dd52b71eaf4d/docs/fast_forward.md | llguidance fast_forward 全文（forced tokens、non-canonical forcing、97%） |
| https://github.com/guidance-ai/llguidance/blob/7ca3404aaf4ba458e03448619790dd52b71eaf4d/docs/fast_forward.md 、 https://atomgit.com/gh_mirrors/ll/llguidance/blob/main/docs/fast_forward.md | 同文档其它镜像 |
| https://arxiv.org/abs/2605.07698 | Grammar-Faithful Speculative Decoding（μ-proj vs μ-star、Dyck TV 0.996，摘要已核实） |
| https://arxiv.org/abs/2405.21047 | Grammar-Aligned Decoding（被 issue #62130 引用，仅索引） |
| https://aclanthology.org/2025.naacl-long.239/ | NAACL 2025 CDSL（摘要已核实） |
| https://arxiv.org/abs/2506.04708v2 、 https://arxiv.org/abs/2506.04708 、 https://aclanthology.org/2025.emnlp-main.1558/ | STAND / Model-Free Speculative Sampling（v3 摘要已核实：降 60–65% 延迟） |
| https://aclanthology.org/2024.emnlp-main.1148.pdf | SpecHub（仅索引，未深读） |
| https://export.arxiv.org/pdf/2609.17943 | ASPIRE（仅索引，未深读） |
| https://aclanthology.org/2026.eacl-long.255.pdf | 小模型投机解码实证研究（仅索引，未深读） |
| https://ieeexplore.ieee.org/document/11664971 、 https://www.computer.org/csdl/proceedings-article/sse/2026/119800a290/2joOwwfu2Ji | CASD, 2026 IEEE SSE（仅核实标题/出处；正文数值未取到） |
| https://openreview.net/attachment?id=g3D27bfmrf&name=pdf | CASD 相关 PDF（仅索引） |

---

## 附录：核实方法与局限
- 本环境 web_fetch 对 github.com / raw.githubusercontent.com / mp.weixin.qq.com 返回 "URL hostname "github.com" resolves to a non-public IP address"；HTML 单次截断在 100000 字符。绕过方式：在 run_code 中用 Node 全局 fetch() 取全文；GitHub 仓库文件走 cdn.jsdelivr.net/gh 与 data.jsdelivr.com；GitHub PR/Issue/Commit 正文走 .diff / .patch / HTML stripped。api.github.com 返回 403（rate limit），SemanticDiff 返回 429。这些限制只影响取材方式，不影响已核实结论。
- 未取到正文/数值的条目已在来源表中逐条标注，报告中不把它们当作数值依据。
- 本报告为文献与代码核实，未做任何本地推理、未访问 GPU 服务器；RTX 5090 上的实际延迟、接受率、p99 抖动需后续实测（本次审计范围外）。
