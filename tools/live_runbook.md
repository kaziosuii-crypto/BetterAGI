# BetterAGI 直播链路启动与验收手册（2026-11-03 21:10 更新）

## 0. 现状快照

- **Lumine 云端在跑**：`i-2.gpushare.com:41846`，容器内 `screen -S lumine` 存活，`curl 127.0.0.1:8010/health`
  → `{"ok": true, "model": "lumine-agent-vl-7b", "mock": false}`；GPU 显存 36936 MiB、利用率 0%（空转计费中）。
- **本机链路未启动**：tunnel / lumine_bridge / BetterGI 宿主 / TUI 当前都没有进程。
- **本轮已修**：thinking 模式 `reasoning_content` 400（详见第 4 节），42 项单测通过，真实接口 8 轮自检全 200。

## 1. 启动顺序（四步，缺一不可）

### 1.1 游戏与宿主

1. 启动原神，进到主界面（保证前台、1920x1080）。
2. 启动 BetterGI 宿主（管理员）：
   `E:\BetterAGI\BetterGI\BetterGenshinImpact\bin\Debug\net10.0-windows10.0.22621.0\BetterGI.exe`
   宿主内置 Live Hub：`ws://127.0.0.1:30648/ws/v1/agent/live`。

### 1.2 SSH 隧道（本地 18010 → 云端 8010）

```powershell
$env:AUTODL_SSH_PASSWORD = (Get-Content E:\BetterAGI\_diag\.autodl_pw -Raw).Trim()
Start-Process pythonw -ArgumentList 'E:\BetterAGI\_diag\tunnel.py --host i-2.gpushare.com --port 41846 --fingerprint SHA256:ctccQQbbMeavq7D0b3QYWVnVANA3HMkOCsKM9QGN3eA --local 18010 --remote 8010'
```

自检：`curl http://127.0.0.1:18010/health` 返回 `"ok": true`。

### 1.3 桥接（BetterGI ↔ Lumine）

```powershell
Start-Process pythonw -WorkingDirectory 'E:\BetterAGI\BetterGI\integrations\lumine' -ArgumentList 'lumine_bridge.py --config bridge_config.json --motor-protocol v2 --intent-port 18311 --intent-token ' + (Get-Content E:\BetterAGI\tools\intent_token.txt -Raw).Trim()
```

自检：`curl http://127.0.0.1:18311/feedback` → `motorConnectionActive: true`、
`motorCapability.declarations.protocol = lumine-live-v2`。

### 1.4 TUI（唯一的玩家主体）

```powershell
Start-Process 'E:\BetterAGI\agent\launch_tui.cmd'
```

- 默认无限轮（`--turns 0`），没有 finish 工具，不会自己宣布"玩完"。
- 底部状态栏就是输入框：直接打字 = 观众弹幕，回车提交，`/quit` 退出，`/time` 看时长。
- 调试接口 `http://127.0.0.1:8791`（Bearer token 在 `E:\BetterAGI\_diag\tui-endpoint.json`），
  `GET /state`、`GET /events`、`POST /say`、`POST /quit`。

## 2. 验收信号

| 项 | 通过标准 | 证据位置 |
| --- | --- | --- |
| 模型调用 | 连续 8 轮无 HTTP 400 | `_diag/tui.log`；离线自检 `_diag/verify-8rounds.json` |
| 桥接 | `motorConnectionActive: true` | `GET :18311/feedback` |
| Lumine | `appliedActions` 递增、`pressedKeys` 非空 | `_diag/bridge2.err.log` 的 5 秒统计 |
| 画面 | 动作前后帧不同 | `_diag/e2e-before.jpg` / `e2e-after.jpg` |

## 3. 关机（省钱优先）

```bash
screen -S lumine -X quit
pkill -f 'autodl_serve[r]'
pkill -f 'vllm.entrypoints.openai.api_serve[r]'
```

确认 `nvidia-smi` 显存回落后再关实例。**没在推理就立刻关**，不要空转。

## 4. 本轮修复：thinking 模式 `reasoning_content` 400

### 4.1 根因（已实证，不是猜）

DeepSeek thinking 模式硬约束：**assistant 消息只要带 `tool_calls`，就必须回传 `reasoning_content`，
哪怕是空串 `""`；整个字段缺省就 400**。纯文本 assistant 省略该字段没问题。

实测矩阵（`api.deepseek.com/v1`、`deepseek-flash`、脚本 `_diag/repro_400.py` + `repro_400b.py`）：

| 组 | 形状 | 结果 |
| --- | --- | --- |
| A | 首轮调用 | 200，reasoning 37 字符 |
| B | 纯文本 assistant 省略 reasoning_content | 200 |
| C | 纯文本 assistant 带空串 | 200 |
| D1 | **带 tool_calls、省略 reasoning_content** | **400**（与 TUI 日志报错逐字一致） |
| D2 | 带 tool_calls、reasoning 非空 | 200 |
| D3 | 带 tool_calls、reasoning_content="" | 200 |
| D4 | 带 tool_calls、content + reasoning | 200 |

TUI 的触发路径：某一轮模型没吐思考却返回了 tool_calls（15:23:24 那轮日志只有"说"没有"想"），
历史里这条消息没带 reasoning_content → 下一轮起永久 400；旧代码失败后每 3 秒无脑重试，
15:23–20:44 共烧了 1185 次 400。

### 4.2 改动

- `agent/tui.py`
  - `History.messages()`：带 `tool_calls` 的 assistant 一律写 `reasoning_content`（`item.reasoning or ""`）。
  - 新增 `MAX_CONSECUTIVE_LLM_FAILURES = 12` 与 `is_reasoning_400()`：reasoning 400 直接停并报清楚，
    其它错误连续 12 次也停——不再无限重试烧额度；真错误（`failed`）不会被"跑到轮数上限"洗白。
- `agent/lithos.py`
  - `LLMReply.as_message()`：同样在带 `tool_calls` 时始终带 `reasoning_content`（空串兜底）。
- 测试：`agent/tests/test_tui.py` + `agent/tests/test_lithos.py` 新增 7 条回归用例。

### 4.3 验证

- `pytest agent/tests -q` → **42 passed**；`ruff check agent` → 0 error。
- `_diag/verify_8rounds.py`：用 TUI 自己的 `History` 拼请求，预置"带 tool_calls 且 reasoning 为空"的毒历史，
  再真打 8 轮 → **8/8 HTTP 200**（修复前第 1 轮就 400）。结果落盘 `_diag/verify-8rounds.json`。
