# GameScaling-9B-S3 推理接口使用说明

> 服务已部署并验收：权重 sha256 与 ModelScope 官方一致，官方 \`examples/run_example.py --trials 3\` = **3/3 parsed, rc=0**。
> 本文所有请求/响应示例均为本机实测输出，不是文档摘抄。

---

## 1. 服务基本信息

| 项 | 值 |
| --- | --- |
| 模型 id | \`gamescaling\` |
| 协议 | OpenAI 兼容 \`/v1/chat/completions\`（vLLM 0.33.0） |
| 服务端监听 | \`0.0.0.0:8000\`（仅在云主机内网可达，公网 8000 不通） |
| 本机访问入口 | \`http://127.0.0.1:18000/v1\`（SSH 隧道映射，见 §2） |
| 权重 | \`/hy-tmp/gs/model/GameScaling-9B-S3/model.safetensors\`，18,819,722,392 B，sha256 \`18afe037…6d7d50\` |
| 上下文长度 | \`--max-model-len 32768\` |
| 单条消息图片上限 | \`--limit-mm-per-prompt {"image": 8}\` → 一次请求最多 8 张图（历史 5 帧 + 当前帧 = 6，安全） |
| 显存占用 | 约 26 GB / 32 GB（RTX 5090） |
| 鉴权 | 无（\`Authorization\` 可省略或填任意值） |

**决策语义（训练契约，不要改）**：一次调用 = 未来 **200 ms** 的操作，拆成 **6 步 × 33 ms**；鼠标位移是**相对位移**，X/Y 单位是「屏幕宽度的千分之一」（±1000 截断），Z 是滚轮格数（±5）。

---

## 2. 网络接入：必须走 SSH 隧道

云主机安全组只放行 SSH（\`i-1.gpushare.com:28390\`），8000 端口直连会超时（实测 \`DIRECT_FAIL 操作超时\`）。用一条本地端口转发即可：

\`\`\`powershell
cd E:\BetterAGI\_diag
$env:UV_LINK_MODE='copy'
uv run --no-project --with paramiko python -X utf8 -u gs_tunnel8000.py 18000 8000
# forwarding 127.0.0.1:18000 -> i-1.gpushare.com:8000
\`\`\`

验证：

\`\`\`powershell
curl.exe -s http://127.0.0.1:18000/v1/models
# {"object":"list","data":[{"id":"gamescaling",...}]}
\`\`\`

> 隧道是长连接（keepalive 30 s）。它挂了就重跑上面的命令；\`agent/motor.py\` 接回来时也应该指向 \`http://127.0.0.1:18000/v1\`。

---

## 3. 请求格式

### 3.1 messages 结构（角色顺序固定）

\`\`\`
system     ← 游戏系统提示词（纯文本，见 §6）
user       ← "current game screen" + 当前帧（data:image/jpeg;base64,…）
[可选历史] ← 每轮一对：user("current game screen"+帧) / assistant(动作文本)
assistant  ← 预填充 "<think>"，让模型从这里接着写
\`\`\`

用户的文本**必须逐字是** \`current game screen\` —— 这是训练时的固定串。

### 3.2 请求体（关键参数一个都不能少）

\`\`\`json
{
  "model": "gamescaling",
  "messages": [
    {"role": "system", "content": "<系统提示词>"},
    {"role": "user", "content": [
      {"type": "text", "text": "current game screen"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
    ]},
    {"role": "assistant", "content": "<think>"}
  ],
  "temperature": 1.0,
  "max_tokens": 2048,
  "skip_special_tokens": false,
  "stop": ["<|action_end|>"],
  "continue_final_message": true,
  "add_generation_prompt": false
}
\`\`\`

| 参数 | 为什么必须这么写 |
| --- | --- |
| \`continue_final_message: true\` + \`add_generation_prompt: false\` | 让 vLLM 接着最后一条 assistant 的 \`<think>\` 续写，而不是另起一轮。这是 vLLM 扩展字段，标准 OpenAI SDK 需用 \`extra_body\` 传 |
| \`skip_special_tokens: false\` | \`<|action_start|>\` / \`<|action_end|>\` 是特殊 token，默认会被解码器吃掉 |
| \`stop: ["<|action_end|>"]\` | 动作一闭合立刻停，省 token；**代价是返回文本里没有闭合标签**（见 §4） |
| \`temperature: 1.0\` | 官方采样温度；\`0\` 会退化成重复动作 |
| \`max_tokens: 2048\` | 带思考时约 100–120 token，留足余量 |

**两种模式**：\`assistant\` 预填 \`"<think>"\` = 允许思考（推荐）；预填 \`"<think>\n\n</think>"\` = 禁止思考、直接出动作，延迟更低（省 300–900 ms），但复杂局面判断变差。

### 3.3 用 openai 官方 SDK 时

\`\`\`python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:18000/v1", api_key="EMPTY")
resp = client.chat.completions.create(
    model="gamescaling",
    messages=messages,
    temperature=1.0, max_tokens=2048,
    extra_body={
        "skip_special_tokens": False,
        "stop": ["<|action_end|>"],
        "continue_final_message": True,
        "add_generation_prompt": False,
    },
)
text = resp.choices[0].message.content
\`\`\`

---

## 4. 响应格式与解析规则

### 4.1 模型输出

\`\`\`text
<think>（可选，一段自然语言推理）</think>
<|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>
\`\`\`

因为请求里把 \`<|action_end|>\` 设为 stop，**返回的 \`content\` 通常以 \`<|action_end|>\` 缺失的形式结尾**，且不含预填的 \`<think>\`：

\`\`\`text
\n\nThe player is already trading shots ... stay on the target.\n</think><|action_start|>78 6 0 ;   ;   ; D ; D ; D
\`\`\`

### 4.2 解析四步（顺序不能错）

1. **补回预填**：\`content\` 不以 \`<think>\` 开头时，前面拼回 \`"<think>"\`。
2. **只看最后一个 \`</think>\` 之后**：思考文本里常会「引用」格式（\`the format is <|action_start|>X Y Z …\`），整段扫描会执行到引用的假动作。
3. **补闭合标签**：\`finish_reason == "stop"\` 且出现 \`<|action_start|>\` 但后面没有 \`<|action_end|>\` → 末尾补 \`<|action_end|>\`。
4. **正则取值**：\`<\|action_start\|[>}](.*?)<\|action_end\|[>}]\`（\`}\` 是模型偶发的闭括号笔误，要容忍）。

### 4.3 字段含义

| 字段 | 含义 |
| --- | --- |
| \`X Y\` | 200 ms 内鼠标**相对位移**，单位 = 屏幕宽度 / 1000；X>0 右、Y>0 下，各自截断在 ±1000 |
| \`Z\` | 滚轮格数，±5，Z>0 上滚 |
| \`g1…g6\` | 6 个 33 ms 子步；每组最多 4 个键；**空组必须保留分号** |
| 按键语义 | 连续两组都出现同一个键 = 一直按住；某组为空 = 该 33 ms 内全部松开 |

### 4.4 按键词表（74 个，大小写敏感，不可自造）

\`\`\`
字母      A B C … Z（大写）
数字       zero one two … nine      ← 数字键，小写英文单词
功能键     One Two … Twelve         ← F1…F12，首字母大写
鼠标键     LB RB MB                 ← 左/右/中键
方向键     Up Down Left Right
特殊键     Shift Ctrl Alt Tab Caps Esc Space Enter Back Delete Insert Home End Pause
标点键     Equal Minus Period Slash Quote
\`\`\`

> 注意大小写陷阱：\`one\` 是数字键 1，\`One\` 是 F1。

---

## 5. 排队/多轮上下文建议

- 历史保留 **5 轮**（每轮 1 帧 + 1 动作），加当前帧共 6 张图，未触及 8 图上限。
- 历史轮默认**丢弃思考文本**，只回填动作串；训练就是这样渲染的（模板自动补 \`<think>\n\n</think>\n\n\`）。
- 换场景/换关卡时调用 \`reset()\` 清空历史，否则上一局的动作会被当成上下文。

---

## 6. 系统提示词：必须逐字用训练骨架

系统提示词不是随便写的攻略文本，模型靠 \`**Output Format**\` 这一节的**字面标记**触发输出格式。骨架顺序固定：

\`\`\`text
You are a gaming expert, and you are currently playing {游戏名}. …（开场，含 200ms / 6×33ms 契约）
**Game Rules**
{游戏规则}
**Common Keys in {游戏名}**
- {训练词表里的键} -> {作用}
- Mouse X Y -> aim / rotate the camera (X>0 right, Y>0 down)
**Output Format**
**Action only (usual)**
<think></think><|action_start|>X Y Z ; k1 k2 k3 ; k4 k5 ; k6 ; k7 ; k8 ; k9 k10<|action_end|>
**Thinking + action (when necessary)**
<think>reasoning</think><|action_start|>X Y Z ; k1 k2 k3 ; k4 k5 ; k6 ; k7 ; k8 ; k9 k10<|action_end|>
**Explanation**
1. **Mouse Movement**: …
2. **Key Sequence**: …
3. Only output a plain string that conforms to the above format — …
**Key Naming Rules**
- Number keys 0-9: use lowercase English words …
- Function keys F1-F12: use capitalized English words …
- Mouse buttons: use LB, RB, MB …
- Letters: use the uppercase letter …
- Arrow keys: use Up, Down, Left, Right …
- Modifier/special keys: Shift, Ctrl, Alt, Tab, Caps, Esc, Space, Enter, Back, Delete, Insert, Home, End, Pause
\`\`\`

自己写游戏时只替换 **{游戏名}**、**Game Rules**、**Common Keys** 三段；\`**Output Format**\` 之后的部分原样保留。服务端现成的完整样例：\`prompts/example_doom_battle1.txt\`（已取回 \`E:\BetterAGI\_diag\gs_client\example_doom_battle1.txt\`）。

---

## 7. 最小可用客户端（本机实测通过）

\`\`\`python
import base64, io, json, re, urllib.request
from PIL import Image

BASE = "http://127.0.0.1:18000/v1"          # 隧道入口
MODEL = "gamescaling"
ACTION_START, ACTION_END, THINK = "<|action_start|>", "<|action_end|>", "<think>"
TAG_RE = re.compile(r"<\|action_start\|[>}](.*?)<\|action_end\|[>}]", re.DOTALL)

def data_url(path, quality=90):
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def act(system, image_path, think=True):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": "current game screen"},
                {"type": "image_url", "image_url": {"url": data_url(image_path)}},
            ]},
            {"role": "assistant", "content": THINK if think else "<think>\n\n</think>"},
        ],
        "temperature": 1.0, "max_tokens": 2048,
        "skip_special_tokens": False, "stop": [ACTION_END],
        "continue_final_message": True, "add_generation_prompt": False,
    }
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        payload = json.loads(r.read().decode())
    choice = payload["choices"][0]
    text = choice["message"]["content"] or ""
    if not text.lstrip().startswith(THINK):
        text = (THINK if think else "<think>\n\n</think>") + text
    if choice.get("finish_reason") == "stop" and ACTION_START in text \
            and ACTION_END not in text[text.rfind(ACTION_START):]:
        text += ACTION_END
    m = TAG_RE.search(text)
    if not m:
        return {"parsed": False, "raw": text}
    head, *groups = [p.strip() for p in m.group(1).split(";")]
    x, y, z = (float(v) for v in head.split())
    steps = [g.split() if g else [] for g in groups]
    return {"parsed": True, "mouse": [x, y, z], "steps": steps,
            "keys": sorted({k for g in steps for k in g}),
            "fires": any("LB" in g for g in steps)}
\`\`\`

可直接跑的版本：\`E:\BetterAGI\_diag\gs_client\client.py\`（含示例帧 \`doom_battle1.jpg\` 与系统提示词）。

**实测输出**（Doom 示例帧，本机经隧道）：

\`\`\`text
latency_ms 1593.6
finish stop usage {'prompt_tokens': 1834, 'completion_tokens': 95, 'total_tokens': 1929}
parsed {"parsed": true, "mouse": [78.0, 6.0, 0.0], "steps": [[], [], [], ["D"], ["D"], ["D"]], "keys": ["D"], "fires": false}
\`\`\`

---

## 8. 延迟参考（单请求，本机经隧道，含思考）

| 场景 | 延迟 | 备注 |
| --- | --- | --- |
| 官方 3 次试验 | 1351 / 436 / 1293 ms | 第 2 次 32 token 无思考，最快 |
| 本机客户端 | 1593 ms | prompt 1834 token、补 95 token |
| 关闭思考模式 | 预计 −300～900 ms | 预填空 think 块 |

> 200 ms 的决策窗口是**游戏时间**语义，不是墙钟预算；实际按「一次决策的最快出招频率」用它。并发/批量吞吐本机未压测，需要时单独测。

---

## 9. 排错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 输出多语言乱码（连 \`What is 1+1?\` 都不对） | 权重文件损坏（大小对、头能解析也没用） | 比对官方 sha256：\`sha256sum model.safetensors\` 必须 = \`18afe037…6d7d50\` |
| \`parsed=False\`，文本里有动作但没标签 | 忘开 \`skip_special_tokens: false\` | 加上；或让 stop 只写 \`</think>\` |
| 模型「复读」格式字符串 / 空动作 | 用了 \`temperature: 0\` 或历史里塞了脏动作 | 温度 1.0；清理历史 |
| 动作永远全零 | 系统提示词不是训练骨架（\`**Output Format**\` 段被改写） | 逐字复用 §6 骨架 |
| HTTP 400 \`limit-mm-per-prompt\` | 一次塞图超过 8 张 | 历史截到 5 轮 |
| 连接超时 | 隧道断了 | 重跑 \`gs_tunnel8000.py\` |

---

*文档对应服务：vLLM 0.33.0 / torch 2.14.0 / transformers 5.20.2，GameScaling-9B-S3，2026-11-04 部署验收。*
