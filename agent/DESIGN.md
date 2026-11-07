# BetterAGI Player Agent 设计说明（参考 DSH 的设计，不复用它的代码）

这份文档回答三件事：**插件是怎么把图片交给模型的**、**为什么要自己写一个 agent**、
以及 **LithosAI 三档模型怎么按紧迫程度切**。

---

## 1. 图片是怎么"回调"给模型的

关键点：模型看到的不是 base64 文本，而是一个真正的图片内容块。链路分四段：

```
BetterGI 截图 (Win32/BitBlt，1920x1080)
  → POST /api/v1/agent/observe  {maxWidth:960, format:"jpg", includeImageBase64:true}
  → 响应里的 imageBase64（JPEG 的 base64）
  → ctx.attachments.saveImage({data, mediaType:"image/jpeg"})   // 落成不可变附件
  → ImageAttachmentRef {attachmentId, bytes, width, height}
  → finalizeContent(exec, result) 返回 [...result.content, {type:"image", attachment:ref}]
  → 模型这一轮就"看见"了这张图
```

几个容易踩的点：

- `finalizeContent` 是**唯一**能把图片塞进结果的钩子；在 `execute` 里返回 base64 字符串
  只会变成几千 token 的文本，模型既看不清也浪费上下文。
- 一次调用最多带 4 张图（`MAX_IMAGES_PER_CALL`），由 `pendingImages: Map<callId, ref[]>`
  在 `finalizeContent` 里取走；不取会一直挂在内存里。
- 图片块是 `{type:'image', attachment: ref}`，不是 OpenAI 的 `image_url`。
  DSH 的附件层负责把它转成模型 API 真正接受的形式。
- 主动推送用的是会话事件：`session.append('user/message', {id, role:'user',
  content:[text, image], source:{kind:'dsh-betteragi'}}, {surfaceOp:'append'})`。
  `source.kind` 必须是"产出者自己的 kind"，写 `'plugin'` 会被格式校验拒绝。

自己写的 agent 里没有 DSH 的附件层，所以走的是标准 OpenAI 多段内容：

```
{"role":"user","content":[
  {"type":"text","text":"这一帧的画面："},
  {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}
]}
```

两条路本质相同：**图片作为内容块进入上下文**，而不是作为字符串。

---

## 2. 为什么还要自己写一个 agent

DSH 的长处是通用（工具、附件、会话、权限），但游戏循环需要的几件事它不管：

| 需求 | DSH 插件能做到吗 | 自己写的内核 |
| --- | --- | --- |
| 连续操作（一按几秒、按帧输出） | 只能一次一个动作，靠模型反复调 | `lumine` 直接把目标交给策略模型，自己持续输出 |
| 禁止手动单键 | 只能靠提示词约束 | 工具表里根本没有按键/点击工具 |
| 紧迫度分档 | 无 | base/fast/ultra 由 urgency 与事件类型决定 |
| 帧预算（带几张图、多大） | 由附件层决定 | 显式参数，按步数衰减 |
| 会话可回放 | 有，但格式绑定 DSH | 自己的 JSONL + frames/ 目录，随便读 |

所以策略是：**DSH 插件保留**（它很适合"人机一起调试"），
同时写一个只做闭环的小内核（`agent.py`），两者共用同一套 BetterGI HTTP 接口。

### 内核结构（模仿 DSH 的三层）

```
Session（事件流）      —— 每一步都落 JSONL，图片单独存文件，事件只引用路径
ToolTable（工具表）    —— scan / look / lumine / finish，四个，且不含任何手动输入
ModelRouter（模型路由）—— LithosAI base/fast/ultra，按 urgency 与 kind 选档
```

Agent 主循环：

```
while step < budget:
    reply = model(messages, tools)
    if reply.tool_calls is empty: 结束
    for call in reply.tool_calls:
        result = tools.dispatch(call)
        messages += tool result（+ 图片内容块，如果这个工具产图）
```

刻意不做的事：不做多 agent 编排、不做长期记忆（下一步单独做）、不做并发工具调用
（游戏是单线程资源，并发只会互相踩）。

---

## 3. LithosAI 三档

三档不是"三个模型名"，而是三种**延迟/质量取舍**：

| 档位 | 用途 | 默认超时 | 触发条件 |
| --- | --- | --- | --- |
| `base` | 反思、总结、写记忆 | 60s | urgency ≤ 0.34，或 `kind` ∈ {reflect, summarize, memory} |
| `fast` | 规划、常规决策 | 30s | urgency 0.35–0.74，或 `kind` ∈ {plan, decide}，以及**未指定时** |
| `ultra` | 实时反应、打断处理 | 15s | urgency ≥ 0.75，或 `kind` ∈ {react, urgent, interrupt} |

接口按 OpenAI 兼容的 `/chat/completions` 实现（`agent/lithos.py`）：

- 环境变量：`LITHOS_API_KEY`、`LITHOS_BASE_URL`、
  `LITHOS_MODEL_BASE/FAST/ULTRA`、`LITHOS_TIMEOUT_BASE/FAST/ULTRA`；
- `choose_tier(urgency, kind=..., forced=...)`：`forced` > `kind` > `urgency`；
- 重试策略：网络错误与 5xx/429 退避重试（默认 2 次），4xx 直接抛——
  参数错了重试只会烧额度；
- 所有错误抛 `LithosError`（带 status/body/code），日志用 `redact()` 抹掉 key。

模型上线后只需要设三个模型 id，不用改代码。

---

## 4. 当前的边界（不要当成已完成）

- `agent.py` 的 `lumine` 工具调用的是本机确定性 Motor；真 Lumine 需要 GPU 实例在线。
- 记忆/关系/情绪还没接进这条新链路（用户明确说"记忆功能以后再说"）。
- 手动输入在插件里默认**劝阻但可用**（`manualInput=discouraged`）：`betteragi_act` 遇到
  `key/click/moveMouse...` 会照做，但结果里带 `manualInputWarning` 要求优先用 lumine；
  只有显式把 `manualInput` 设成 `off` 才硬禁（用户要求：能不用就不用，不能一刀切禁掉）。

## 三档模型：测试用官方 DeepSeek，生产用 lithosAI

- 生产（`LITHOS_PROVIDER=lithos`）：`deepseek-ai/DeepSeek-V4.1-Flash` / `-fast` / `-ultra`，base `https://api.lithos.ai/v1`，key 用 `LITHOS_API_KEY`。
- 测试（默认，`LITHOS_PROVIDER=deepseek`）：官方接口**只认** `deepseek-flash` 与 `deepseek-v4-pro`
  （实测传 `deepseek-ai/DeepSeek-V4.1-Flash` 直接 400），所以三个档位都落到 `deepseek-flash`：
  档位语义（职责与超时）仍在，但**速度差异在测试环境复现不了**，必须等生产 key 才能验。
- key 优先取 `LITHOS_API_KEY` / `DEEPSEEK_API_KEY` 环境变量；都没有时只读
  `C:\Users\Tsuba\.dsh\.credentials.yaml` 的 `refs`（不落盘、不打印）。
- 手动输入策略：`manualInput` 三态 `discouraged`（默认，放行并提示优先用 lumine）/ `off`（显式硬禁）/ `on`（不提示）。