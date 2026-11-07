# Phase 3：Intent 协议与 MotorController

状态：已实现（单元/集成级），真实 AutoDL 与游戏内动作归属尚未联调。  
日期：2026-10-28

## 目标

把「谁决定目标」和「谁执行动作」拆开：

- PersistentPlayerAgent 是唯一的目标/意图来源；
- Lumine 只把 intent 翻译成 200ms 动作块，不再自行决定剧情目标；
- 每个动作都能追溯到一个 `intent_id`；
- 保留 v1 协议，随时可回滚。

## 实现

### 玩家侧

`integrations/player/player_intent.py`

- `normalize_intent()`：限制字段长度、补齐 `intent_id/status/created_ms/deadline_ms`，
  非法 `motor_mode` 直接拒绝。
- `IntentTracker`：单主意图状态机
  `created → sent → running → blocked/done/failed/cancelled/expired`。
  新意图会取消旧意图；`apply_feedback()` 接收 Motor 的进度/阻塞/完成反馈；
  `expire()` 处理超时。
- `IntentPublisher`：只允许回环地址的 HTTP intent 总线；
  `publish()` / `cancel()`（POST `{"cancel": true}`）/ `fetch_feedback()`。
  发布失败不抛到主循环，只记录。

`integrations/player/player_agent.py`

- `_publish_intent()`：从提案的 `intention` + `proposed_actions` 提示生成
  `motor_mode`（`navigate/combat/interact/ui/wait`），交给总线。
- 相同 goal 且仍在执行的意图复用，不重复创建。
- 对话锁期间不发布叙事意图，改为发布显式 hold 意图（见 Phase 6）。

### Motor 侧

`integrations/lumine/lumine_protocol.py`

- 新增 `motor` prompt 模式与 `MOTOR_SYSTEM_PROMPT`：
  「你只负责把给定 intent 转成键鼠动作；不得自行选择目标、推进剧情或改写意图」。
- `format_intent()` 把目标、理由、模式、截止时间写进 prompt；
  `build_messages(..., intent=)` / `build_motor_messages()`。

`integrations/lumine/autodl_server.py`

- `PlayerSession.intent` 保存并校验 `intent_id + goal`；
- 推理时若有 intent，走 motor prompt；没有则保持旧 v1 行为；
- WebSocket 接受 `{"type": "intent", "intent": {...}}`，回 `intent_ack`；
- 会话 reset / 关闭时清空 intent。

`integrations/lumine/lumine_bridge.py`

- `BridgeConfig.motor_protocol`（默认 `v1`）与 `intent_host/intent_port/intent_token`；
- `motor_protocol=v2` 时启动本机 HTTP 总线：
  - `POST /intent` 设置/取消当前意图；
  - `GET /feedback` 返回当前意图与最近反馈；
- 动作回显、错误、确认都会写入 feedback（带 `intent_id`）；
- 断线重连后自动重发当前 intent；
- 会话 reset 时清空并上报 `blocked/session_reset`。

## 验证

- `integrations/player/tests/test_player_intent.py`：状态机、取消、超时、反馈、回环限制。
- `integrations/player/tests/test_player_agent.py`：意图发布、相同目标复用、对话锁 hold。
- `integrations/lumine/tests/test_lumine_protocol.py`：motor prompt 不包含目标选择权限。
- `integrations/lumine/tests/test_lumine_bridge.py`：总线鉴权、intent 转发、重连重发、cancel。
- `integrations/lumine/tests/test_autodl_server.py`：intent 校验、推理透传、ack、reset 清空。

测试命令：

```bash
python -m pytest integrations/player/tests/test_player_intent.py integrations/lumine/tests -q
```

## 已知边界

- C# 动作回显尚未携带 `intent_id`；当前通过 bridge 的「最近一次 intent」关联，
  多意图并发时精度有限。Phase 3 的 C# 侧改动标记为可选，未实施。
- 真实 Lumine/AutoDL 端到端尚未跑通（模型服务已停止，游戏尚未安装）。
- `motor_mode` 只作为 prompt 约束，不是硬白名单；Motor 仍可能输出越界动作。

## 回滚

设置 `MOTOR_PROTOCOL=v1`（或 bridge 配置 `motor_protocol=v1`）即可完全关闭
intent 总线，恢复旧的 guidance 驱动模式。
