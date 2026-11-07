# 当前架构审计（CURRENT_ARCHITECTURE）

审计日期：2026-10-27  
审计范围：`BetterGI/integrations/lumine`、`integrations/session`、BetterGI 会话存储、`Docs/`、`lore/` 及相关测试。  
方法：逐文件阅读（不是只看 README/使用手册），并交叉核对测试与运行日志；本文件只描述现状，不包含改造方案。

> Phase 0.5 已修复的断点：memory 响应形状、guidance 发送确认/重试、live
> 会话重启、guidance 竞态、OpenViking 覆盖、session id 对齐、lore 示例
> 剧透。本文其余部分保留审计时的状态；修复内容与剩余项见
> [PHASE_0_5_FIXES.md](PHASE_0_5_FIXES.md)。
>
> Phase 1 已新增 `BetterGI/integrations/player/` shadow 玩家骨架（只读、
> 不发送动作），旧导演/录制链路未改；详见
> [PHASE_1_PLAYER_SKELETON.md](PHASE_1_PLAYER_SKELETON.md)。因此下文
> 「没有 player_profile / resume_state」的描述只代表 Phase 1 之前的状态。

---

## 0. 结论先行

**当前系统不是「一个玩家」在玩原神，而是两个独立决策主体加一个执行器：**

1. **DeepSeek 导演**（`live_director.py`）看画面，决定 `thought`、`guidance`、
   `commentary`，并把少量文本写进记忆；它**不产生任何游戏动作**。
2. **Lumine 视觉模型**（`lumine_protocol.py` / `autodl_server.py`）看另一份
   画面和导演给出的 `guidance`，自己决定 200ms 动作块；它**没有记忆、没有
   剧情目标、没有角色关系，也不向导演汇报自己的意图**。
3. **BetterGI** 执行 Lumine 的动作，把画面、解说、动作状态广播出去；
   **录制器**只是被动记录。

因此：

- `commentary` 来自 DeepSeek，`action` 来自 Lumine；
- 导演只看到 `action_state.keys`，看不到 Lumine 的动作块、鼠标位移和
  `thought`（`live_director.py:1140-1160` 的 `_handle_message` 没有 `action`
  分支）；
- Lumine 的系统提示直接写「You are an expert Genshin Impact player …
  Decide the best action」（`lumine_protocol.py:114`），它拥有局部甚至
  叙事级的自主决策；
- 默认 persona 甚至把对方称为「AI 队友」，明确把导演定位成旁观解说
  （`live_director.py:362-378`）；
- 导演 system prompt 原文是「正和观众一起观看一位 AI 玩家操作游戏」
  （`live_director.py:489`）。

**这是本轮重构要修复的核心架构缺陷：THERE ARE TWO PLAYERS。**

---

## 1. 组件与职责

| 组件 | 入口 | 实际职责 | 是否持有认知状态 |
|---|---|---|---|
| BetterGI Live Hub | `LiveSessionService.cs` / `LiveWebSocketHandler.cs` | 截帧、广播、执行动作、广播 `action_state`、回显 `action` | 只持有执行状态（按键、队列） |
| 导演 | `live_director.py` | 看帧、生成思考/指导/解说、写记忆、调用白名单工具 | 有 `memory` / 最近解说 / 短期事件，但只影响文本 |
| 操作桥 | `lumine_bridge.py` | 把帧与 guidance 转发给 AutoDL；把动作转发回 BetterGI | 只有最新 guidance、队列、统计 |
| Lumine 服务 | `autodl_server.py` + `lumine_protocol.py` | 对当前帧推理 200ms 动作块 | 无跨请求记忆；vLLM KV 仅请求内 |
| 录制器 | `record_session.py` | 把 Hub 消息写成 `session.jsonl` + 视频 | 只有当前会话的写入状态 |
| 渲染器 | `render_session.py` | 飞鱼 TTS、时间对齐、字幕、混流 | 无运行时认知状态 |
| 记忆桥 | `memory_bridge.py` | 导演侧 OpenViking 检索/写入 | 只有连接与写入节流 |
| OpenViking | 本地服务 1933 | 文本资源/记忆的向量检索 | 持久化文本，但没有结构化认知 |

---

## 2. 实际数据流

```text
BetterGI 截帧（10–15fps，JPEG）
        │  WebSocket /ws/v1/agent/live
        ├──────────────────────────────► 导演 live_director.py
        │                                   │  prompt: 系统人格 + 当前帧
        │                                   │          + guidance + action_state
        │                                   │          + memory + short_term
        │                                   │          + OpenViking recall
        │                                   │
        │                                   ├─ thought/commentary ──► Hub 广播
        │                                   └─ guidance ────────────► Hub 广播
        │
        └──────────────────────────────► 操作桥 lumine_bridge.py
                                            │ 帧（5Hz）+ guidance
                                            ▼
                                      AutoDL Lumine 7B
                                            │ 只看当前帧 + guidance
                                            │ 自决 200ms 动作块
                                            ▼
                                     操作桥 → Hub actions
                                            │
                                            ▼
                                   BetterGI 执行模拟键鼠
                                            │
                                            └─ action_state（keys/queue）
                                                + action 回显（block/thought）
```

关键事实：

- **导演和 Lumine 看的是同一份 JPEG，但不是同一份上下文**。导演有文本
  记忆和人格，Lumine 只有一帧 + 一行 guidance。
- **Lumine 的 `thought` 会随动作回显到 Hub，但导演不消费 `action` 消息**；
  导演只知道按键集合。
- **Lumine 不知道导演的解说**；导演不知道 Lumine 的动作理由。
- **guidance 是单向的**：导演 → Hub → 桥 → Lumine；Lumine 没有回复
  guidance 的通道，只有动作。
- **录制器记录的是两者的输出**，不是任何一方的内部认知。

---

## 3. 逐项回答审计问题

### 3.1 哪个模型看到什么输入？

**导演（DeepSeek `deepseek-flash`）**

- 最新 JPEG（`_latest_frame`，来自 Hub `frame` 消息）；
- 帧序号、当前 guidance、`action_state`（keys/queue/starved/applied）；
- 最近 8 条解说（`_recent_commentary`）；
- `memory` 字典（从 BetterGI `memory.json` 载入，及 `memory_patch` 更新）；
- 短期记忆文本：`scene` / `objective` / `plot` + 最近 8 条 `_short_events`
  （`live_director.py:1196-1225`）；
- 长期记忆：OpenViking 检索结果（最多 4 条，1800 字，
  `live_director.py:1334-1355`）。

**Lumine（7B）**

- 当前 JPEG（1280×720，桥以 5Hz 转发）；
- 一行 `guidance`（可空）；
- 固定系统提示与动作格式（`lumine_protocol.py:114-145`）；
- **没有**历史帧、对话、记忆、角色、任务、情绪或长期目标。

### 3.2 哪个模型拥有记忆？

- **导演**拥有不完整的文本记忆：BetterGI `memory.json`、最近解说、
  `_short_events`、`_episode_log`、OpenViking；
- **Lumine 没有任何持久记忆**；
- **桥**只保存 `latest_guidance` 与队列；
- **录制器**保存事件文件，但不把状态回灌给任何模型。

### 3.3 哪个模型决定目标？

没有真正的「目标」对象。实际是：

- 导演产出**一行 guidance**（`build_prompt` 要求「给下游玩家模型的一行
  可执行指导」，`live_director.py:510`）；
- Lumine 在 system prompt 中被要求「Decide the best action」，并在 guidance
  与画面冲突时自行判断（`lumine_protocol.py:132-134`）；
- 所以**叙事目标没有单一所有者**：导演给建议，Lumine 可执行也可不执行，
  双方都不知道对方的完整意图。

### 3.4 哪个模型决定动作？

**Lumine**。导演没有任何动作 API；它只能发 `guidance`。桥把 Lumine 的
`action` 转成 BetterGI `actions`（`lumine_bridge.py:1095-1107`、
`251-302`）。BetterGI 只执行。

### 3.5 guidance 如何流动？

1. 导演在 `_process_response` 中把 `guidance` 通过 WS 发给 Hub
   （`live_director.py:1450-1470`）；
2. Hub `SetGuidanceAsync` 广播 `guidance`（`LiveSessionService.cs`）；
3. 桥在 `_handle_bettergi_message` 的 `guidance` 分支收到，
   更新 `latest_guidance` 并 `_send_lumine({"type":"guidance"})`
   （`lumine_bridge.py:953-960`）；
4. 桥构帧消息时也会把 `latest_guidance` 作为 `guidance` 字段附在帧上
   （`lumine_bridge.py:304-330`）；
5. Lumine 的 `infer` 用 `build_messages(guidance, mode)` 把它放进 user
   消息（`autodl_server.py:517-527`）。

没有反向通道：Lumine 不确认、不解释、不拒绝 guidance。

### 3.6 Lumine 实际收到了什么？

- 5Hz 的当前 JPEG；
- 最近一次 guidance（来自帧内联或独立 guidance 消息）；
- 固定 system prompt（「expert Genshin Impact player」 + 动作格式）；
- 没有历史、没有对话、没有记忆、没有角色关系。

### 3.7 OpenViking recall 在哪一步发生？

在导演每轮 `_tick` 里、构建 prompt 之前：

- 查询 = `guidance + scene + objective + plot + 最近一条解说`
  （`live_director.py:1334-1352`）；
- 目标 URI = `viking://~/resources/genshin` 与
  `viking://~/memories/genshin`（`memory_bridge.py:61-68`）；
- 最多 4 条、1800 字；读取 L2 正文或 abstract（`memory_bridge.py:126-173`）；
- Lumine 完全不使用 OpenViking。

### 3.8 memory write 在什么条件发生？

两处：

1. **`memory_patch`**：每轮 LLM 返回后，`_apply_memory_patch` 合并进
   `self.memory` 并 POST 到 BetterGI 会话记忆
   （`live_director.py:1521-1545`）；写入 `User/AgentSessions/<id>/memory.json`
   （`AgentSessionStore.cs:22, 743-764, 946, 1053`），上限 1MB
   （`AgentLimits.cs:58`）。
2. **OpenViking episode**：`_maybe_write_episode` 每 60 秒（可配置）把
   `scene/objective/plot/最近解说` 追加成一行，写
   `viking://~/memories/genshin/live/<session_id>.md`，最多 60 行，
   `wait=True`（`live_director.py:1480-1519`、`memory_bridge.py:200`）。

没有结构化 episode、belief、relationship、emotion、narrative thread 写入。

### 3.9 一次 session 如何恢复？

**没有真正的恢复机制。**

- 录制器每次启动新建 `sessions/<name>`，`SessionWriter` 写新的
  `session.jsonl`（`record_session.py:294+`）；
- 导演启动时 `_load_initial_memory(ensure=True)` 会读取 BetterGI
  `memory.json`（`live_director.py` 的 `BetterGIClient.memory_get`），
  但只恢复 `memory` 字典；而且当前响应形状不匹配（见 3.14-1），实际读不到
  `scene/plot/objective`；
- BetterGI 本身有 checkpoint/resume 端点（`AgentSessionStore.cs:414-451`
  的 `GetResumeAsync`/`AgentResumeDto`），但导演从不调用；
- 不读取上一场 `session.jsonl`，不读取最后位置/任务/意图/动作；
- 没有 `resume_state.json`；
- 录制器、桥、Lumine 都没有会话恢复协议。

### 3.10 重启程序后哪些状态会丢？

导演进程重启会丢：

- `_latest_frame`、`_frame_seq`；
- `_recent_commentary`（最近 8 条）；
- `_short_events`（最近 40 条）；
- `_episode_log`（本场滚动摘要缓冲）；
- `_last_thought`、`_last_sent_guidance`、`_last_received_guidance`；
- `_action_state`；
- `self.memory` 的进程内副本（重新连接后从 BetterGI `memory.json` 重新读）。

桥重启会丢：

- `latest_guidance`、帧/动作队列、统计、录制队列；
- 与 Lumine 的连接、in-flight 请求。

Lumine 服务重启会丢：

- 所有请求内 KV；没有任何跨请求状态。

### 3.11 重启电脑后哪些状态还存在？

- BetterGI `User/AgentSessions/<id>/memory.json`、`events.jsonl`、`meta.json`、
  `checkpoints/`、`frames/`（`AgentSessionStore.cs:22`）；
- OpenViking 的 `resources/` 与 `memories/`（本地数据目录）；
- 录制目录里的 `session.jsonl`、`gameplay.mp4`、`audio/`；
- `lore/` 源文件与导入结果；
- **不存在** player profile、resume state、belief/relationship/emotion 状态。

### 3.12 下一次游戏启动时上一场经历如何重新进入 context？

只有两条被动路径：

1. 导演连接 BetterGI 后读取 `memory.json`，其中可能有上一次的
   `scene/plot/objective`（如果上一场写过）；但当前 API 形状 bug
   （3.14-1）会让这次读取拿不到顶层字段；
2. 如果本轮查询与旧记忆语义相近，OpenViking 会召回
   `memories/genshin/live/<session>.md` 的文本；同一 session 的
   `replace` 写入又可能已经覆盖上一场（3.14-6）。

没有「上一场结束于哪里、当时想做什么、有哪些未解决疑问」的结构化
resume state；没有自动把上一场摘要注入首轮提示词；没有「昨天我们做到
这里」的稳定开场。是否延续取决于向量检索是否碰巧命中。

### 3.13 commentary 和 action 是否来自同一个认知主体？

**否。**

- commentary 由 DeepSeek 导演根据自己的帧、记忆、guidance 生成；
- action 由 Lumine 根据自己的帧、guidance、固定动作提示生成；
- 导演看不到 Lumine 的动作块/鼠标/thought，只看到按键集合；
- Lumine 看不到导演的 thought/commentary；
- 两者没有共享的 belief、goal、emotion、relationship、narrative thread。

因此存在「解说与操作不一致」「导演说往左、Lumine 往右」「导演描述了一个
它以为发生的动作」的结构性风险。

### 3.14 关键实现缺陷（会让恢复/一致性直接失效）

这些不是“未来能力”，而是当前代码里的具体 bug/断点：

1. **BetterGI memory 读取形状不匹配（最严重）**
   - `live_director.py:1099-1105` 期望 `GET /memory` 返回
     `{"memory": {...}}`；
   - C# `AgentMemoryDto` 实际字段是 `sessionId/updatedAtUtc/values`
     （`AgentSessionModels.cs:40-50`，`BgiAgentHttpEndpoints.cs:221-226`）；
   - 结果 `data.get("memory")` 为 `None`，整个响应被当成 memory，
     `self.memory` 变成 `{"sessionId":..., "updatedAtUtc":..., "values":{...}}`；
   - `_short_term_text`/`_maybe_write_episode` 只读顶层 `scene/objective/plot`
     （`live_director.py:1196-1213, 1493-1495`），重启后读不到；
   - 测试里的 `RecordingClient` 返回自造的 `{"memory": ...}`
     （`test_director.py:46-52`），掩盖了这个 bug。

2. **guidance 发送可能静默丢失**
   - `_send` 在 `_ws is None` 时直接返回（`live_director.py:1602-1605`）；
   - `_process_response` 无论是否真正发出，都会把
     `_last_sent_guidance/_latest_guidance` 设为新值（`:1449-1452`）；
   - 之后导演认为指导已发，Lumine 可能仍按旧指导行动，解说却按新指导在讲。

3. **外部 guidance 会覆盖导演的当前指导**
   - frame 内嵌或服务端 `guidance` 会调用 `_apply_guidance` 并直接更新
     `_latest_guidance`（`live_director.py:1179-1181, 1220-1222`）；
   - 如果 hub/其他客户端设置过 guidance，导演 prompt 里的“当前指导”可能是
     别人的，自己的指导又被去重跳过/冲突。

4. **没有画面新鲜度检查**
   - `_tick` 只判断 `frame is None`（`live_director.py:1338-1340`）；
   - 采集停滞但 WS 仍在时，导演会反复用旧帧生成新 guidance/commentary。

5. **没有处理 `live.stopped`**
   - `_handle_message` 的分支只有 hello/frame/guidance/commentary/
     action_state/error/pong（`live_director.py:1129-1159`）；
   - 会话停止后导演可能继续 tick/发送。

6. **OpenViking 写入会覆盖上一场**
   - `remember_session` 用 `mode="replace"` 写同一个
     `memories/genshin/live/<session_id>.md`（`memory_bridge.py:182-204`）；
   - 默认 `sessionId="vtuber"`（`director_config.json:5`），且
     `_last_memory_write` 初值 0，首次 tick 就会写；
   - 同一 session 重启后第一写会把上一场摘要整个替换掉。

7. **MemoryBridge 只在启动时连一次**
   - `run()` 开头 `start()`（`live_director.py:1017`）；若 OpenViking 尚未
     启动/未装 SDK/健康检查失败，`available=False`，本次进程不再重试
     （`memory_bridge.py:89-115`）。

8. **persona 文件默认路径不匹配**
   - `resolve_options` 默认 `persona_path="persona.json"`
     （`live_director.py:1685-1691`），但仓库只有 `persona.example.json`；
   - `director_config.json` 没有 persona 键；实际运行静默回退
     `DEFAULT_PERSONA`（小光），不是示例里的“小光 Lumi”；
   - 从不同 cwd 启动可能读到不同/不存在的 persona.json，导致跨场人格漂移。

9. **memory_patch 的持久化边界**
   - C# 单键 ≤128 字符、整包 ≤1MB，任一超限会让整个 MergeMemoryAsync 失败
     （`AgentSessionStore.cs:260-282`，`AgentLimits.cs:58,63`）；
   - 导演本地先 `self.memory.update` 再 POST，失败不回滚
     （`live_director.py:1542-1550`）；
   - 键只增不减，没有淘汰；达到 1MB 后所有 patch 都会失败。

10. **session 的恢复语义不存在**
    - `SessionWriter` 以 `"w"` 打开（`session_format.py:299-300`），
      同名目录会新建 `<name>-<epoch>`（`record_session.py:707-709`）；
    - `meta.session_id` 由 writer 独立生成，目录名来自 --name/hello，
      两者通常不一致（`session_format.py:294`，`record_session.py:703-733`）；
    - 没有 parent/previous/resume_cursor。

11. **帧/音频/事件绑定缺失**
    - `frame` 事件类型存在但从未写入（`session_format.py:65`，
      `record_session.py:525-526`）；
    - `audio` 事件从未写入；渲染器只写 `audio/` 与 `render.json`
      （`session_format.py:68`，`render_session.py:827-831`）；
    - `note` 从未调用；
    - `render_session.py` 只读 speech 与 video，thought/action/avatar 不参与成片
      （`render_session.py:804, 841-863`）。

12. **run_episode 的启动语义**
    - 最多等 20 秒首帧；超时后即使一帧没有，只要录制线程活着也会启动
      导演/桥（`run_episode.py:166-172`）；
    - 子进程 stdout/stderr 不捕获、无健康检查、无重启（`:66-91`）；
    - 录制器 meta.agent 硬编码 `director+lumine`，`--no-agents` 也一样
      （`record_session.py:731, 886`）。

13. **测试掩盖了分离架构与形状 bug**
    - 导演测试只断言 thought/guidance/commentary，断言白名单不含动作工具
      （`test_director.py:290-303, 404-580`）；
    - Bridge 测试没有验证独立 guidance 是否真的转发给 Lumine；
    - `RecordingClient` 的 memory 形状是测试自造，不是 C# 实际响应。

14. **桥忽略 live.started/live.stopped，重启后动作会被长期拒绝**
    - BetterGI 新 live 会话的 `FrameSeq` 从 0 重新开始
      （`LiveSessionService.cs:166-176, 609`），但每个客户端的
      `LastActionSeq` 不重置（`LiveSessionModels.cs:410-411`），
      handler 要求严格递增（`LiveWebSocketHandler.cs:262-270`）；
    - `lumine_bridge.py:994` 把 `live.stopped/live.started` 当未知消息忽略，
      FramePump 接受 seq 回退（`:385-397`）；
    - 新会话从 1 开始的 action 会被拒绝，直到 seq 超过旧会话最后值；
    - 新会话帧没有 guidance 时，bridge 回退旧 `latest_guidance`
      （`:316-318, 945-947`），把上一场景指导带进新场景。

15. **guidance 竞态：旧帧内联 guidance 覆盖新 guidance**
    - `build_frame_message` 优先用帧内联 guidance（`:316-318`）；
    - 旧帧先入 FramePump、新 guidance 后到并立即发送，随后旧帧转发时把
      旧 guidance 再发给 AutoDL；AutoDL 每帧内联 guidance 覆盖会话值
      （`autodl_server.py:765-768`）；
    - 结果：导演改了指导，Lumine 下一帧仍按旧指导行动。

16. **Lumine 的默认 prompt 不强制跟随 guidance**
    - pretrain 系统提示只要求“预测下一个 200ms 动作”，没有 follow
      guidance（`lumine_protocol.py:97-112`）；
    - 默认部署 `prompt_mode=pretrain`（`autodl_server.py:86,879`），
      `AUTODL_DEPLOY.md:81-82` 也承认指令跟随有限；
    - instruct 也只是“与画面一致时遵循”（`lumine_protocol.py:132-134`）。

17. **动作没有执行结果反馈**
    - bridge `actions_out += 1` 只代表发送成功（`lumine_bridge.py:1108-1111`）；
    - BetterGI 可能因 seq/范围/安全条件拒绝，error 只回到 bridge
      （`:986-992`），不回导演；
    - 无效 action 只记“安全停顿”，不发送 `stop_actions`
      （`:1104-1107`），按键依赖 250ms idle release
      （`LiveActionExecutor.cs:380-386`）。

18. **动作基于过期帧且无对话/菜单门控**
    - AutoDL 存 `ts_ms` 但不检查帧年龄（`autodl_server.py:267-273,749-774`）；
    - BetterGI 执行器从“收到动作的现在”重排 33ms，不看源帧年龄
      （`LiveActionExecutor.cs:165-188,349-354`）；
    - bridge 丢弃 width/height/capture/runtime/uiCategory
      （`lumine_bridge.py:324-330`）；
    - prompt 教在菜单/UI 用绝对点击（`lumine_protocol.py:105-107,123-125`），
      mock 的 absolute 阶段就是 640,360+LB；没有 dialogue_lock。

19. **动作非确定且分辨率假设固定**
    - `temperature=0.7`、`top_p=0.95`、无 seed（`autodl_server.py:84-85,533-535`）；
    - prompt 固定 1280×720（`lumine_protocol.py:48-51`），绝对坐标按
      x/1280、y/720 映射（`LiveActionExecutor.cs:448-453`），非 16:9 时
      点击可能偏移。

20. **队列替换/饥饿导致走走停停**
    - 新动作块清空旧块（`LiveActionExecutor.cs:157-162`）；2.5Hz 时
      200ms 块之间有空档，250ms 后释放按键（`:380-386`）；
    - 导演看到画面走走停停，可能解说“一直在前进”。

---

## 4. 记忆与人格现状

### 4.1 persona 不是 player profile

`persona.example.json` / `DEFAULT_PERSONA` 只有：

- name、personality（一句话）、tone、language、catchphrases；
- `content_dimensions`（解说的六个维度）。

它没有：

- play_style、preferences、values、long_term_interests；
- self_history；
- 对探索/解谜/收集/战斗/角色的稳定倾向；
- 对失败、悲剧、可爱的反应规则；
- 可检索的长期人格。

persona 每轮原样进入 system prompt；没有「稳定人格 + 可检索长期人格」的
分层。

### 4.2 短期记忆是文本拼接，不是工作记忆

`_short_term_text` 把 `memory.scene/objective/plot` 和最近 8 条事件拼成
一段文本（`live_director.py:1196-1225`）。它没有：

- 当前对话/说话人；
- 当前疑问；
- 当前情绪；
- 当前短期计划；
- 为什么这么做；
- 最近一次动作的结果。

### 4.3 长期记忆是滚动日志，不是情景记忆

OpenViking 中的 episode 是：

```text
# 实况记忆 <session>
[20:12:33] 场景=... 目标=... 剧情=... 最近说=...
[20:13:33] ...
```

没有 event_id、location、characters、what_happened、what_i_knew_before、
what_i_learned、what_i_thought、what_i_felt、my_reaction、choices_i_made、
unresolved_questions、importance、emotional_intensity、related_memories、
source_session、game_time_range 等字段。

### 4.4 没有 belief / relationship / emotion / narrative thread

- belief：没有 proposition/status/confidence/evidence/contradictions；
- relationship：没有 familiarity/trust/affection/suspicion/respect/concern；
- emotion：只有每条 `speech` 的 `emotion` 标签，没有持续状态、强度、衰减；
- narrative thread：只有 `plot` 字符串，没有 open/closed、importance、
  evidence、current_theory、last_referenced。

### 4.5 检索只有 top-k 语义

- 查询是几个字段拼接；
- 目标只有两个子树；
- 最多 4 条、1800 字；
- 没有 recency/importance/emotional/character/location/quest/narrative/
  belief/unfinished-intent 的加权；
- 没有冲突消解、去重、时间线合并。

### 4.6 没有 consolidation / resume / autobiographical memory

- 没有 session-end `MemoryConsolidator`；
- 没有「本期发生什么、谁出现、关系怎么变、哪些猜测被推翻、未解决什么、
  下次想做什么」的结构化总结；
- 没有 `resume_state.json`；
- 没有 `My Journey So Far` 这类自传记忆。

### 4.7 并行的 Amaidesu 记忆没有接入

`Amaidesu/src/agents/streamer/memory/` 已经有一套主播记忆：

- `recap.py`：场次回顾；
- `roster.py`：常客/观众画像；
- `notebook.py`：工作笔记；
- `tools.py`：`memory_recall_session` / `memory_recall_viewer` 工具。

但它只服务于 Amaidesu 的可选直播层，导演、桥、OpenViking、离线 session
都没有使用它。当前工作区实际上有三套互不相通的记忆：

1. 导演 `memory.json` + `_short_events` + `_episode_log`；
2. OpenViking `resources/memories`；
3. Amaidesu `session_recaps` / roster / notebook。

这正是「多套记忆、没有单一玩家」的另一条证据。

---

## 5. 对话、动作与实况质量

### 5.1 没有 dialogue perception

导演只收到 JPEG，没有 OCR/字幕/说话人/对话文本；BetterGI 的
`uiCategory` 在 Live 帧里也没有被填充（设计文档已注明预留）。导演无法
知道「NPC 刚刚说了什么」，只能从画面猜测。

### 5.2 没有 dialogue_lock

Lumine 的动作提示没有「剧情对话期间不要跳过」的硬约束；桥/执行器也没有
对话状态。bridge 直接把帧的 width/height/capture/runtime/uiCategory 丢弃
（`lumine_bridge.py:324-330`），BetterGI 当前也不给 `uiCategory` 赋值
（`LiveSessionService.cs:687-701`）。prompt 还教它在菜单/UI 使用绝对点击
（`lumine_protocol.py:105-107,123-125`），mock 的 absolute 阶段就是
640,360 + LB。Lumine 可能在剧情对话/菜单中继续点击/攻击，导致跳过对话
或选错选项。

### 5.3 动作没有 ownership / intent

动作协议是 `chunks/absolute/thought`（`lumine_protocol.py:317+`、
`lumine_bridge.py:251-302`），没有：

- `intent_id`、`owner`、`goal`、`reason`、`constraints`、
  `motor_mode`、`target`、`interrupt_conditions`；
- 完成/阻塞/失败的结构化反馈。

导演只看到 `action_state.keys`，不知道 Lumine 在做什么、为什么。

### 5.4 没有真正的 speech policy

- 提示词只说「没有值得说的内容可以是空数组」（`live_director.py:511`）；
- 默认 `max_commentary_per_tick=4`（代码），本地配置改为 1；
- 没有「普通移动保持沉默」「NPC 对话期间禁止覆盖」「情绪低落时不要
  立刻兴奋」等规则；
- `speech` 事件没有 intensity/pace/volume/pause/interruptible/thought_source
  （`session_format.py:359-400`）。

### 5.5 视频/音频不完整

- 录制器只录画面（WS 帧或窗口），没有游戏原音；
- 渲染器只混 TTS，没有 BGM/NPC 语音/环境音/ducking；
- 没有对话时钟，只有统一的 `t` 和 speech `dur`；
- TTS 是后处理，但没有 prosody 元数据；当前只按 emotion 选样式。

---

## 6. 状态持久化矩阵

| 状态 | 位置 | 进程重启 | 电脑重启 | 下一次 session |
|---|---|---|---|---|
| persona | 代码/文件 | 保留 | 保留 | 原样注入 |
| memory（scene/plot/objective） | BetterGI `memory.json` | 重连后本应读回，但当前 API 形状不匹配，实际读不到 | 保留 | 不可靠 |
| 最近解说/事件/episode 缓冲 | 导演内存 | **丢失** | **丢失** | 无 |
| OpenViking resources/memories | OpenViking 数据目录 | 保留 | 保留 | 需查询才召回 |
| session.jsonl / 视频 / audio | 录制目录 | 保留 | 保留 | 不自动读取 |
| 桥 guidance/队列 | 桥内存 | **丢失** | **丢失** | 无 |
| Lumine 任何状态 | vLLM 请求内 | **丢失** | **丢失** | 无 |
| player profile / beliefs / relationships / emotion / resume | 不存在 | — | — | — |

---

## 7. 测试现状

- Lumine 导演/桥/协议/记忆：审计时 **162 passed**；Phase 0.5 后 **169 passed**；
  2026-10-28（含 Phase 3 intent）为 **178 passed**（另有 28 subtests）；
- 会话格式/录制/渲染：审计时 **15 passed**；2026-10-28（含 Phase 7/8 prosody 与音频）为 **26 passed**；
- Phase 1/2 shadow 玩家骨架：**161 passed**；2026-10-28（含 Phase 3–9）为 **189 passed**；
- BetterGI ExternalAccess：**234 passed**。

这些测试主要验证：

- 消息协议、字段校验、动作安全、录制格式、渲染输出；
- 导演的 prompt 构造、JSON 提取、guidance/commentary 写回；
- 记忆桥的格式化与 OpenViking 接口形状。

**Phase 0 审计时没有**（Phase 3–9 已补充模块级 A–E 回归；
真实 30–60 分钟实况与盲测仍未执行）：

- 跨 session 记忆/人格连续性测试；
- 错误猜测被推翻的认知历史测试；
- 情绪延续测试；
- 剧透/知识解锁测试；
- 「commentary 与 action 同源」测试；
- 对话锁定/不跳过测试；
- 30–60 分钟实况的人工验收标准。

测试通过数量不代表「同一个玩家在连续游玩」。

### 7.1 文档层面的不一致/过时

审计还发现文档与代码/文档自身有多处冲突：

1. **导演是谁**：`live-vtuber-pipeline.md`/`PROTOCOL.md`/`lumine/README.md`
   把 `dsh` 当作导演；根 README/使用手册/`system-status.md` 把
   `live_director.py` + DeepSeek 当作导演。没有说明哪个是当前生产链路。
2. **AutoDL 是否实测**：`lumine/README.md:533-534` 与
   `live-vtuber-pipeline.md:92,100,579` 说“未实测/设计预算”；
   `AUTODL_DEPLOY.md:85-120`、`system-status.md:44,75`、根 README 说
   已实测约 0.4s/帧。至少一处过时。
3. **测试数**：`system-status.md:60` 写 151；`使用手册.md:85` 写 162。
4. **协议字段**：`PROTOCOL.md:126` 用 `ts/frameWidth`，当前 C# 是
   `timestampUtc/width`；`PROTOCOL.md:199` 把 `action_state` 列为时间线
   类型，`live-vtuber-pipeline.md:371` 说不写入时间线；`PROTOCOL.md:152`
   说动作队列约 20 个 33ms 键块、超 660ms 丢最旧，实际是新块整体替换旧块
   （`LiveActionExecutor.cs:157-162`）；`PROTOCOL.md:126` 的 `uiCategory`
   实际不赋值；`README.md:510` 说事件上限 20000，代码是 5000
   （`LiveSessionService.cs:42`）。
5. **会话事件清单**：使用手册漏 `audio`，`session/README.md` 漏 `note`；
   实际两者都在 `session_format.py:57-72`。
6. **架构入口**：`Docs/architecture/README.md` 仍写“暂无”；
   `Docs/design/README.md` 没有索引新增的 agent/live/memory/session 文档。
7. **“本机 LLM”措辞**：`system-status.md:17` 写“本机 LLM”，实际是
   DeepSeek 云端 API。
8. **persona**：只有 `persona.example.json`，没有 `persona.json`；
   `director_config.json` 没有 persona 键，默认静默回退 `DEFAULT_PERSONA`。
9. **lore 示例本身含剧透**：`lore/genshin_basics.md:8,14,15-18` 包含
   坎瑞亚/天理、稻妻解除锁国、枫丹/纳塔/至冬等后期事实；当前 recall
   没有任何进度过滤。
10. **“可持续运行”**：`live-vtuber-pipeline.md:7` 是目标表述；
    `system-status.md:141` 明确只验证过 20 秒级，30 分钟未压测。

这些不一致会直接影响迁移判断，必须在 Phase 1 前统一。

---

## 8. 现有可保留资产

改造时不能破坏：

1. **BetterGI 的图像识别 + 模拟输入 + Live Hub**（`Service/ExternalAccess/Agent`）；
2. **会话录制**（`record_session.py` + `session.jsonl` 事件源）；
3. **会话文件格式的 event sourcing 思路**（`session_format.py`）；
4. **OpenViking 作为可替换 memory backend**（`memory_bridge.py` 的
   `recall/remember_session` 接口）；
5. **CosyVoice 离线渲染**（`render_session.py` + `cosyvoice/`）；
6. **现有有效测试**（162/15/234）。

---

## 9. 核心缺陷清单（按严重度）

1. **双认知主体**：DeepSeek 导演与 Lumine 各自决策，没有唯一玩家。
2. **动作与解说不同源**：没有共享 belief/goal/emotion，存在解说不一致。
3. **Lumine 没有记忆/目标**：它只看当前帧与一行 guidance，无法承担叙事。
4. **没有 Persistent Self / player_profile**：人格是静态主播描述。
5. **没有 episodic/belief/relationship/emotion/narrative 结构**。
6. **没有 session resume**：重启后无法自然「睡醒继续」。
7. **没有 dialogue ingestion / dialogue_lock**：Agent 不知道剧情对白，
   可能跳过对话。
8. **没有 intent/ownership/motor feedback 协议**：动作无理由、无归属、
   无阻塞回报。
9. **检索只有 top-k 向量**：没有重要性/情绪/角色/线索加权。
10. **没有 consolidation/autobiographical memory/forgetting**。
11. **没有知识解锁/剧透隔离**：`lore/` 直接可被玩家检索。
12. **没有 speech policy/情绪连续/游戏原音/ducking/prosody**。
13. **单一 4 秒循环**：没有 fast/reactive/deliberative/deep 多时间尺度。
14. **测试只覆盖模块行为**：没有连续性/人格/剧透验收。

---

## 10. 审计证据索引

| 事实 | 文件:行 |
|---|---|
| 导演 system prompt 与 memory_patch 约定 | `live_director.py:456-556` |
| 导演每轮 tick / recall | `live_director.py:1319-1408` |
| 导演只处理 action_state，不处理 action | `live_director.py:1133-1160` |
| 导演短期事件/场景卡 | `live_director.py:1183-1225` |
| memory_patch 写入 | `live_director.py:1521-1545` |
| OpenViking recall/写入 | `memory_bridge.py:126-205` |
| Lumine「expert player」提示 | `lumine_protocol.py:114-145` |
| 桥转发 guidance | `lumine_bridge.py:953-960` |
| 桥把 Lumine 动作转给 BetterGI | `lumine_bridge.py:251-302, 1095-1107` |
| Lumine 服务只传当前帧+guidance | `autodl_server.py:517-559` |
| 会话事件类型 | `session_format.py:57-81` |
| speech 字段 | `session_format.py:359-400` |
| 录制器 commentary/action | `record_session.py:589-700` |
| 会话存储 layout/1MB | `AgentSessionStore.cs:22-30`, `AgentLimits.cs:58,108` |
| persona「AI 队友」 | `live_director.py:362-378`, `persona.example.json` |
| 渲染只混 TTS | `render_session.py:1135-1203` |

---

**一句话总结：当前系统是一个「DeepSeek 解说员 + Lumine 独立操作员 + 录像机」，
不是一个持续存在的玩家。**
