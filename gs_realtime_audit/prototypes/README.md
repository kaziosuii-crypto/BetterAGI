# GameScaling 实时推理审计 · 流式客户端原型

`streaming_client.py` 是一个**独立分支原型**：与官方同步客户端 `audit/official_code/policy.py::GameScalingPolicy` 并列存在，不覆盖、不修改、不 import 官方代码（仅在校验时用 importlib 按文件路径临时加载）。

> ⚠️ 本文件及说明只用于**离线审计原型**。自检（`--self-test`）使用 mock SSE 流，不连 GPU 服务器、不发推理请求、不跑 SSH、不发真实游戏输入、不部署生产。真实 `act()` 调用才会按 `base_url` 发起连接——默认离线流程不会触发。

## 1. 依赖

- Python 3.10+（本机实测 3.15.0）
- `requests`（持久 `requests.Session`，Keep-Alive）、`Pillow`

## 2. 如何运行

### 2.1 离线自检（推荐；零网络请求）

```
cd E:/BetterAGI/gs_realtime_audit/prototypes
python streaming_client.py --self-test          # 45 项 mock-SSE 断言，打印逐项结果
python streaming_client.py --self-test --quiet  # 只通过退出码判断（0=全部通过）
python streaming_client.py --parity             # 打印官方请求体一致性报告(JSON)
```

`--self-test` 覆盖：官方 PolicyConfig 默认值逐字段比对、`build_request` 请求体**完全相等**、无 `chat_template_kwargs`、prefill、finalize 补回 ACTION_END、`finish_reason=length` 不补回、mock 流逐块回调与 g1~g6 到达时间、JSONL trace、官方 `parse_action` 比对、usage 缺失、HTTP 500、中途断连、reasoning=False 路径。

语法自检（等价写法）：

```
python -c "import ast; ast.parse(open('streaming_client.py',encoding='utf-8').read()); print('AST_PARSE_OK')"
```

### 2.2 本机直连（唯一能真正逐块到达的链路）

```python
from PIL import Image
from streaming_client import StreamingGameScalingClient, StreamingConfig

# System Prompt 原样传入：原型不重建 prompt、不改 Output Format、不改模型
SYSTEM_PROMPT = open("../audit/official_code/system_prompt_template.txt",
                     encoding="utf-8").read()

cfg = StreamingConfig(
    base_url="http://127.0.0.1:8000/v1",   # 直连 vLLM，切勿走 frame_relay.py
    jsonl_path="trace.jsonl",               # 每次 act 追加可回溯 JSONL
)
client = StreamingGameScalingClient(
    SYSTEM_PROMPT, cfg,
    on_chunk=lambda i: print(i["delta"], end="", flush=True),
    on_mouse_header=lambda i: print("\n[mouse]", i["mouse_text"], i["t_rel_ms"], "ms"),
    on_group=lambda i: print("\n[g%d]" % i["group_index"], i["group_text"], i["t_rel_ms"], "ms"),
    on_done=lambda i: print("\n[done] TTFT=", i["metrics"]["ttft_ms"]),
)

frame = Image.new("RGB", (1280, 720))       # 真实运行时换成游戏画面
decision = client.act(frame)                 # 同步阻塞到完整响应，但过程中逐块回调
print(decision.metrics)                      # TTFT / Action TTFT / 各组间隔 ...
print(decision.verify)                       # 与官方 parse_action 的比对结果

# 生成器版本（同样逐块回调，额外 yield StreamEvent）
for ev in client.act_stream(frame, run_id="my-run-001"):
    if ev.type == "done":
        final = ev.decision
client.close()
```

`act()` 由 `act_stream()` 消费实现，因此两个 API 的回调时序完全一致；`act()` 返回最后一个 `done` 事件里的 `StreamingDecision`（即使出错也返回对象，`error` 字段说明原因，不抛异常中断审计）。

### 2.3 单独做官方一致性核对（不发网络）

```python
from streaming_client import StreamingGameScalingClient
print(StreamingGameScalingClient("SYS").check_request_parity())
```

## 3. 配置参数

### 3.1 与官方 `PolicyConfig` **完全同名、同默认值**的字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:8000/v1` | 服务根地址（可配；直连才流式） |
| `model` | `gamescaling` | 模型名，不改动 |
| `api_key` | `EMPTY` | `Authorization: Bearer ...` |
| `history_len` | `5` | 保留的过去决策轮数 |
| `history_think` | `0` | 最近几轮保留 `<think>`；`-1`=全部 |
| `image_size` | `None` | 非 None 时先 resize |
| `image_format` | `jpeg` | `jpeg` / `png` |
| `jpeg_quality` | `90` | JPEG 质量 |
| `resample` | `bilinear` | `bilinear`/`lanczos`/`bicubic` |
| `reasoning` | `True` | `True`→prefill `<think>`；`False`→`<think>\n\n</think>` |
| `temperature` | `1.0` | 采样温度 |
| `max_tokens` | `2048` | 最大生成 token 数 |
| `stop` | `[<|action_end|>]` | 停止串（服务端会把它从文本中移除） |
| `lenient_parse` | `False` | 宽松解析开关 |
| `timeout` | `120.0` | 请求超时（秒） |

### 3.2 流式/审计专用字段（**不会**进入官方请求体子集）

| 字段 | 默认 | 说明 |
|---|---|---|
| `jsonl_path` | `None` | 追加写入 JSONL；`None`=只留在内存 `decision.trace` |
| `sse_chunk_size` | `1` | `iter_lines(chunk_size=...)`，客户端侧最小缓冲 |
| `verify_official` | `True` | 每次 `act` 自动跑 `verify_against_official()` |
| `include_usage` | `False` | 置 True 才发 `stream_options={"include_usage":True}`（**会偏离官方体**，见限制） |
| `official_code_dir` | `None` | 覆盖官方代码目录（默认 `../audit/official_code`） |
| `extra_headers` | `{}` | 附加 HTTP 头 |

构造函数还支持 `session=`（注入 mock 或自定义持久 Session）、`on_chunk / on_mouse_header / on_group / on_done` 四个实例级回调；`act()`/`act_stream()` 可用同名参数按次覆盖。

## 4. 与官方同步客户端的差异表

| 维度 | 官方 `GameScalingPolicy`（同步） | 本原型 `StreamingGameScalingClient`（流式） |
|---|---|---|
| 传输 | `session.post(..., stream=False)`，一次拿到完整 JSON | 持久 `Session` + `stream=True` + SSE 逐块 `iter_lines` |
| 请求体 | `build_request()` 8 字段 | **同 8 字段逐值相等** + 唯一传输字段 `stream=True` |
| `chat_template_kwargs` | 不发 | 不发（断言保证） |
| 响应读取 | `r.json()['choices'][0]['message']['content']` | 累加 `choices[0].delta.content`，到 `[DONE]` 结束 |
| 时间戳 | 仅 `latency_ms` | 每 chunk `time.perf_counter()`：`t_http_send/t_headers/t_first_token/t_last_token/t_done` + 每个 chunk `t_arrive` |
| 派生指标 | 无 | TTFT / Action TTFT / Full Generation Latency / End-to-End Latency / g1~g6 到达 / 分隔组间隔 |
| 解析 | 结束后一次 `parse_action(finalize(text))` | **累积文本增量解析**（`on_mouse_header`/`on_group`/`on_done`）+ 完整文本兜底 |
| 结果校验 | 无 | `verify_against_official()` 用 importlib 载入官方 `action.py` 比对 |
| 可回溯输出 | 无 | `config`/每 chunk/`usage`/`metrics` 全量写入 JSONL |
| finalize | 官方 `finalize()` | 逐语句复刻：补 prefill、裸 body 补 ACTION_START、`finish_reason=stop` 时补回 ACTION_END |
| 历史 | `deque[Turn]`，`history_len` 裁剪 | 同规则（list + 裁剪），`history_text` 语义一致 |
| 错误 | 捕获后返回 `Decision(error=...)` | 分类捕获 timeout / HTTP 4xx·5xx / 断连 / 解码；返回带 `error` 的 `StreamingDecision`，trace 记 `error` 事件 |
| 定位 | 生产同步客户端（**本原型不改动它**） | `prototypes/` 下的审计原型，独立文件 |

### 4.1 请求体一致性结论（实测）

`check_request_parity()` 用 importlib 加载官方 `policy.py` 并逐值比对，自检通过：

- `build_official_body()` 与官方 `build_request()` **完全相等**（空历史、`system_prompt="SYS"`、同一 `image_url`）；
- 键集合固定为 8 个：`add_generation_prompt, continue_final_message, max_tokens, messages, model, skip_special_tokens, stop, temperature`；
- assistant prefill = `<think>`（reasoning=True）/ `<think>\n\n</think>`（reasoning=False），与官方 `prefill` 一致；
- 不含 `chat_template_kwargs`；System Prompt 原样传入、Output Format 不改、模型不改；
- `build_streaming_body()` 相对官方体**只多 `stream=True`** 一个键，官方 8 字段全部原值保留。

> 为何必须加 `stream=True`：OpenAI 兼容服务（vLLM）由 JSON body 的 `stream` 决定走 SSE 还是一次性 JSON；客户端的 `stream=True`（requests 的读模式）只影响本地是否延迟下载 body，二者不等价。

## 5. SSE 缓冲排查（最重要的实测坑）

审计目标是「每个 chunk 何时到达」。若所有 chunk 的 `t_arrive` 挤在同一毫秒，说明**链路上有整体缓冲，流式失效**——先按此表排查，不要怀疑客户端：

| 链路 | 是否逐步到达 | 原因 |
|---|---|---|
| 直连 `http://127.0.0.1:8000/v1/chat/completions` | ✅ 逐步 | vLLM 原生 SSE，边生成边发 |
| 经 `E:/BetterAGI/svc/frame_relay.py` 转发 | ❌ 整体到达 | **`svc/frame_relay.py:234`：`return Response(content=resp.content, ...)`** 把上游 `resp.content` 当整块内存读完后一次性返回；且中继用 `httpx.AsyncClient` + `content=json.dumps(body)` 非流式 POST。chunk 永远不会逐步到达 |
| 任何 `Response(content=<完整字节>)` 型反代 / 网关 | ❌ 整体到达 | 同上：`content=` 会先物化整个 body |
| 客户端 `iter_lines(chunk_size=N)` | 局部 | 只是本地读缓冲；`sse_chunk_size=1` 已最小化，不能修复服务端整体缓冲 |

排查清单：

1. 先看 `decision.metrics['inter_chunk_intervals_ms']`：若全为 0 或只有一个巨大尾部值 → 服务端整体缓冲。
2. 确认 `base_url` 是 `127.0.0.1:8000` **直连**，没有经 `frame_relay.py` / 任何 `Response(content=resp.content)` 中间层。
3. 确认请求体里 `stream=True` 且服务端返回 `Content-Type: text/event-stream`（非 `application/json`）。
4. 确认 `skip_special_tokens=False` 已生效，否则 `<|action_start|>`/`<|action_end|>` 被服务端剥掉，增量解析定位不到动作头。
5. 代理/网关需关闭 response buffering（如 nginx `proxy_buffering off;`、X-Accel-Buffering: no），并改用 StreamingResponse/逐块转发。
6. 帧协议里的 `frame_delta` / `delta://cur` / `frame://-1` 占位符与本流式原型无关，本原型**不实现 delta**。

## 6. JSONL 字段字典

`jsonl_path` 下每次 `act()` 追加多行（每行一个自包含 JSON 对象）；不设路径时同样内容留在 `decision.trace`。所有行共享 `run_id`（uuid4 或调用方传入）。时间均为 `time.perf_counter()` 单调时钟秒；`t_rel_ms` 为相对 `t_http_send` 的毫秒。

### 6.1 顶层事件（`event` 字段）

| event | 何时写 | 主要字段 |
|---|---|---|---|
| `run_start` | 构造 TraceWriter | `config`(配置快照，api_key 非 EMPTY 时脱敏), `t_http_send`, `t_http_send_wall`(UTC ISO) |
| `request` | 发出前 | `url`, `method`, `stream`, `body_keys`, `body_model`, `message_count`, `prefill`, `chat_template_kwargs`(布尔) |
| `response_headers` | 收到响应头 | `t_headers`, `t_rel_ms`, `status_code` |
| `chunk` | 每个 SSE data 事件 | `index`, `t_arrive`, `t_rel_ms`, `delta`, `cumulative_len`, `finish_reason`, `usage`；坏 JSON 时含 `parse_error`,`raw` |
| `mouse_header` | 扫到 `X Y Z ;` | `mouse_text`, `t_arrive`, `t_rel_ms`, `cumulative_text` |
| `group` | 扫到 g1~g6 | `group_index`(1..6), `group_text`, `t_arrive`, `t_rel_ms`, `cumulative_text` |
| `sse_done` | 收到 `data: [DONE]` | `t_arrive`, `t_rel_ms` |
| `metrics` | 收尾 | `metrics`(见 6.2) |
| `done` | 收尾 | `t_done`, `t_rel_ms`, `finish_reason`, `usage`, `usage_missing`, `error`, `truncated`, `text`(finalize 后完整文本), `action`, `verify` |
| `error` | 出错时 | `error`, `t_done`, `t_rel_ms` |
| `trace_io_error` | JSONL 写失败 | `error`（写失败不会中断流） |

### 6.2 `metrics` 派生指标（同时进 `done` 事件与 `decision.metrics`）

| 字段 | 含义 |
|---|---|---|
| `ttft_ms` | Time To First Token：`t_http_send → t_first_token` |
| `action_ttft_ms` | Action TTFT：`t_http_send →` 首次出现 `<|action_start|>` 的 chunk（含 think 时长） |
| `full_generation_latency_ms` | Full Generation Latency：`t_first_token → t_last_token` |
| `end_to_end_latency_ms` | End-to-End Latency：`t_http_send → t_done` |
| `time_to_headers_ms` | `t_http_send → t_headers` |
| `first_token_to_headers_ms` | 响应头后到首 token 的等待 |
| `group_arrivals` | `g1..g6` 各自的 `t_arrive`（perf_counter 秒）；`g1`=第 2 个 `;` 到达，`g6`=ACTION_END 或流结束 |
| `group_intervals_ms` | `mouse_header->g1`, `g1->g2` … `g5->g6` 的分隔组间隔 |
| `group_texts` / `groups_seen` / `mouse_text` | 各组文本、已识别组号、鼠标头文本 |
| `inter_chunk_intervals_ms` | 相邻 SSE chunk 到达间隔数组（诊断整体缓冲最直接的信号） |
| `chunk_count` / `nonempty_delta_count` / `cumulative_chars` | chunk 数、非空 delta 数、累积字符数 |
| `finish_reason` / `done_sentinel_seen` | 结束原因、`[DONE]` 是否出现 |
| `usage` / `usage_missing` / `completion_tokens` | usage 统计（缺失时 `usage_missing=true`，不报错） |
| `truncated` / `length_truncated` | 出错/断连/`finish_reason=length`/无 `[DONE]` 任一为真 |
| `action_source` | `incremental`（增量解析成功）或 `local_strict_fallback`（完整文本兜底） |
| `parser_warnings` / `error` | 增量解析告警、错误信息 |

## 7. 增量解析与回调

- `IncrementalActionParser` 维护累积文本 `self.text`，每个 chunk 后重扫一次：仅在 `</think>` 之后定位动作体（避免 thinking 里「引用格式」的文本被误解析）；`X Y Z ;` 触发 `on_mouse_header`，第 `k+1` 个 `;` 触发 `on_group(g{k})`，ACTION_END 或流结束触发 `g6`，收尾触发 `on_done`。
- 因为 `stop=[<|action_end|>]` 会把结束标签从解码文本中移除，**正常情况流里没有 ACTION_END**；`g6` 以流结束为准，`finalize_text()` 再按官方规则补回。
- 这是**轻量自带解析器**（本模块独立可运行），不 import 另一子代理负责的完整增量解析器；`verify_against_official()` 才是与官方 `parse_action` 对齐的权威比对。
- 回调抛异常被吞掉并记入 `parser.warnings`，不会中断流。

## 8. 错误处理对照

| 情况 | 行为 |
|---|---|---|
| 网络断开 / ConnectionError | `error='network_error: ...'`，保留已收部分文本，trace 写 `error` 事件 |
| 超时 | `error='timeout: ...'` |
| HTTP 4xx / 5xx | 抛 `requests.HTTPError` 并记为 `error='http_error: HTTP <code>: <body[:500]>'` |
| `finish_reason=length` | `truncated=true`；**不**补 ACTION_END（与官方 finalize 一致） |
| stop 串移除 ACTION_END | `finalize_text()` 仅在 `finish_reason=='stop'` 时补回 `<|action_end|>` |
| usage 缺失 | `usage_missing=true`、`completion_tokens=None`，不报错 |
| SSE data 坏 JSON | 记 `chunk.parse_error` + `raw`，跳过该块继续 |

## 9. 已知限制

1. **必须直连本机 `127.0.0.1:8000`**：经 `frame_relay.py`（`Response(content=resp.content)`）或任何整体缓冲反代时，所有 chunk 一次性到达，TTFT/组间隔失真——这是链路限制，不是客户端 bug。
2. `stream=True` 是相对官方请求体的**唯一新增字段**（SSE 必需）；`include_usage=True` 会再加 `stream_options`，属显式偏离官方体，默认关闭。
3. usage 依赖服务端在流式里回传；OpenAI 兼容实现通常要 `stream_options.include_usage`，所以默认 `usage` 可能为 `None`（`usage_missing=true`），属预期。
4. 轻量增量解析器只覆盖严格格式；畸形/宽松恢复以完整文本 `local_strict_parse(lenient=...)` 兜底，最终以 `verify_against_official()` 的官方 `parse_action` 为准。
5. `verify_against_official()` 直接按文件路径加载官方 `action.py`（官方无相对导入）；`check_request_parity()` 加载 `policy.py` 时需为缺失的 `keymaps.py` 注入 stub 模块——纯校验用，运行时不依赖官方包。
6. `lenient_parse` 的宽松分支、`<think>` 未闭合等边缘形状未做与官方逐一对齐的穷举测试。
7. `time.perf_counter()` 是进程内单调时钟，**跨进程/跨机不可比**；JSONL 只另存一个 UTC 墙钟 `t_http_send_wall` 作参考。
8. 本原型不发真实游戏输入、不做动作执行、不做生产部署；`health_check()` 存在但默认不自动调用（避免隐式网络请求）。
9. 自检 45 项全部基于 mock SSE，未验证真实 vLLM 0.17.0 的逐块行为；上线/正式测量前需在本机直连复测一次。
10. `history` 用 list 实现（官方用 deque），语义一致但在极高轮数下裁剪开销不同；审计场景可忽略。

---

文件：`prototypes/streaming_client.py`（实现）、`prototypes/README.md`（本说明）。写入范围仅限这两个文件；官方代码只读。