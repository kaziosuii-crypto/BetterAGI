# Phase 0.5 修复记录

日期：2026-10-27  
范围：只修审计列出的 P0 断点，不引入 PersistentPlayerAgent 新架构。
运行链路（BetterGI → 桥 → Lumine → 录制）保持原协议，未改动 C#。

---

## 已修复

### 1. BetterGI memory 响应形状

问题：导演期望 `{"memory": {...}}`，C# 实际返回
`{sessionId, updatedAtUtc, values}`，重启后 `scene/plot/objective` 读不到。

修复：

- 新增 `extract_memory_values(data)`，同时支持
  `values` / `memory` / 纯键值对象三种形状；
- `_load_initial_memory` 使用它；
- `bgi_agent_memory_get` 的 key 查询和整体返回使用它；
- 测试 fake `RecordingClient` 改为真实 C# DTO 形状。

文件：`live_director.py`、`tests/test_director.py`。  
测试：`MemoryShapeTests`（真实形状、legacy `memory`、纯键值、加载恢复）。

### 2. guidance 发送确认与重试

问题：`_send` 在 `_ws is None` 时静默 return，调用方仍把
`_last_sent_guidance/_latest_guidance` 标成已发。

修复：

- `_send` 返回 `bool`；无连接/发送异常返回 `False` 并记 warning；
- 发送失败时写入 `_pending_guidance`，不更新已发状态；
- 下一轮即使 LLM 没给新 guidance，也会重试待发文本；
- 发送成功才清空 pending 并更新 `last_sent/latest`。

文件：`live_director.py`、`tests/test_director.py`。  
测试：`GuidanceSendTests`（无 WS 不误标、下一轮重试成功）。

### 3. live 会话重启与动作序号

问题：桥忽略 `live.started/live.stopped`；新 live 会话帧序号归零，但
BetterGI 的 `LastActionSeq` 不重置，动作会被长期拒绝；旧 guidance 还会
被带进新场景。

修复：

- 桥处理 `live.started/live.stopped`：清空 `FramePump`、`latest_guidance`
  和时间戳；
- 动作序号改用桥自己的单调 `_action_seq`，不再使用可能归零的
  `forFrameSeq`；
- BetterGI 重连时同时清空 guidance 时间戳。

文件：`lumine_bridge.py`、`tests/test_lumine_bridge.py`。  
测试：`BridgeStateTests`（live.stopped 清状态、frame 序号 5→1 时动作序号
仍为 1→2）。

### 4. guidance 竞态（旧帧覆盖新指导）

问题：桥构帧时优先使用帧内联 guidance；旧帧被转发时可能把旧指导再次发给
Lumine，覆盖新指导。

修复：

- guidance 带 `latest_guidance_at_ms`；
- 帧内联 guidance 只有在其时间戳不早于当前 guidance 时才更新缓存；
- `build_frame_message` 优先使用桥缓存的 `latest_guidance`，帧内联仅作
  回退；同时接受 `timestampUtc` 与 `ts`。

文件：`lumine_bridge.py`、`tests/test_lumine_bridge.py`。  
测试：`FrameMessageTests.test_latest_guidance_wins_over_stale_inline`。

### 5. OpenViking 覆盖上一场

问题：`remember_session` 以 `mode="replace"` 写固定的
`live/<session_id>.md`；默认 session `vtuber`，重启首 tick 会覆盖上一场
摘要。MemoryBridge 也只在启动时连一次。

修复：

- 每次 `MemoryBridge` 实例生成独立 `run_id`，写入
  `live/<session>-<run_id>.md`，不再覆盖旧文件；
- `recall`/`write_memory` 调用 `_ensure_started()`，OpenViking 晚启动时
  最多每 30 秒重试一次；
- `start()` 失败时清理 client，避免半初始化。

文件：`memory_bridge.py`、`tests/test_memory_bridge.py`。  
测试：`WriteTests`（两个实例 URI 不同、仍为 replace）。

### 6. session 标识对齐

问题：`meta.session_id` 由 writer 独立生成，目录名来自 --name/hello，
两者通常不一致，跨场无法关联。

修复：录制器创建 `SessionWriter` 时显式传入
`session_id=self.session_dir.name`。

文件：`record_session.py`、`tests/test_record_session.py`。  
测试：录制端到端断言 `meta.session_id == session_dir.name`。

### 7. lore 示例去剧透并重新导入

问题：`lore/genshin_basics.md` 含坎瑞亚/天理、稻妻解除锁国、枫丹/纳塔/
至冬等后期事实，且已被导入 OpenViking。

修复：

- 示例只保留开局可知道的蒙德/璃月与通用机制；
- 移除后期地区/真相；
- 已重新执行 `memory_ingest.py` 覆盖旧资源。

文件：`lore/genshin_basics.md`、OpenViking `resources/genshin`。

---

## 验证

```bash
# Lumine / memory
cd /e/BetterAGI/BetterGI/integrations/lumine
/e/BetterAGI/Amaidesu/.venv/Scripts/ruff check .
/e/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest tests -q
# 169 passed, 28 subtests

# session
cd /e/BetterAGI/BetterGI/integrations/session
/e/BetterAGI/Amaidesu/.venv/Scripts/ruff check .
/e/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest tests -q
# 15 passed
```

没有真实原神/BetterGI 运行环境，因此未做实机联调；上述测试覆盖了修复的
协议/形状/状态逻辑。

---

## 仍未做（后续 Phase）

这些是 Phase 1+ 的架构工作，未在本阶段改动：

- `action` 执行结果的 `motor_feedback`/ack 到导演（G43）；
- dialogue_lock / UI 类别 / 菜单门控（G44）；
- 显式 `persona.json` 与 player_profile（G34/G4）；
- session append/resume、parent/previous（G36/G12）；
- `frame`/`audio`/`note` 事件的实际写入（G23/G24）；
- WORLD_DATABASE / PLAYER_KNOWLEDGE 的硬隔离（G20/G37）；
- episodic/belief/relationship/emotion/narrative thread/consolidation（G5–G13）；
- 多时间尺度循环与 ContextBuilder（G13/G19）；
- 30–60 分钟连续性与盲测（TEST_PLAN A–E/人工验收）。

### 2026-10-28 更新

- **已完成**：motor feedback/ack（bridge intent feedback）、dialogue_lock 与 hold 意图、
  player_profile、resume_state/recap、episodic/belief/relationship/emotion/thread、
  consolidation、ContextBuilder、`audio` 事件写入、world_only/ground_truth 过滤、
  A–E 模块级回归。
- **部分完成**：UI 类别门控只覆盖对话；`frame`/`note` 事件仍不逐帧写入；
  session append/parent 仍未实现。
- **未完成**：真实 30–60 分钟实况、盲测、AutoDL/飞鱼 TTS 端到端。
