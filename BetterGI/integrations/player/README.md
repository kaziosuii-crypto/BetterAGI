# PersistentPlayerAgent（观察模式，主动接管未完成）

这是「唯一玩家」重构的实现：持续存在的 shadow 玩家主体，带分块预算的
ContextBuilder、结构化记忆、意图协议、对话锁与 speech policy。

## 这个阶段做什么

- 启动时加载玩家档案（profile）、上次 resume、working memory 与上一场 recap；
- 只读连接 BetterGI Live，观察帧、指导、解说、动作状态与会话边界；
- 每轮检索相关 episode/fact，按块预算构造 prompt；
- 生成「如果是我会怎么想/怎么做/想说什么」，写入 shadow JSONL；
- 维护 working/episode/semantic 与 belief/relationship/emotion/thread 状态；
- 意图与反馈模块已部分实现，但播放器入口的主动模式当前关闭；
- 默认 `observer` 不发送动作、指导或解说；启用屏幕观察会向 BetterGI
  发观察请求并写观察事件，不是零副作用。

旧 `integrations/lumine/live_director.py`、录制器、Lumine 桥、渲染链路
保持原样；不传 `--player-shadow-config` 时不启动本模块。

## 目录

```text
integrations/player/
    player_profile.py            档案加载/校验/稳定摘要/默认档案播种
    player_state.py              resume 状态、版本检查、损坏恢复、原子写
    player_io.py                 原子文件读写
    player_context.py            Phase 1 legacy prompt（仍可回退）
    player_context_builder.py    Phase 2 分块预算 ContextBuilder
    player_working_memory.py     working memory（state/working.json）
    player_episodic_memory.py    episodic memory（memory/episodes/*.json）
    player_semantic_memory.py    PLAYER_KNOWLEDGE facts（facts.json）
    player_knowledge.py          证据门禁：屏幕 OCR 逐字引文才可解锁
    player_vision.py             可选的 BetterGI 观察客户端（截图+OCR）
    player_retrieval.py          本地多因子检索 + 可选外部后端
    player_memory_consolidator.py 一轮认知 → working/episode/fact/四类状态
    player_recap.py              跨场回顾（确定性摘要）与持久化
    player_intent.py             意图状态机、Motor 总线发布/反馈
    player_beliefs.py            信念（可推翻、保留历史）
    player_relationships.py      关系（六维、单步限幅）
    player_emotion.py            情绪（九维、20 分钟半衰期）
    player_threads.py            叙事线索（open/dormant/resolved/abandoned）
    player_dialogue.py           OCR 对话感知与 dialogue lock
    player_speech_policy.py      沉默/冷却/情绪余韵/prosody 决策
    player_legacy_import.py      旧导演记忆 → 不可召回的隔离区
    player_proposal.py           宽容解析模型提案并限长
    player_agent.py              PersistentPlayerAgent（shadow）
    player_loop.py               最小 deliberative 循环
    shadow_recorder.py           独立 shadow JSONL + 脱敏
    player_config.py             CLI/环境变量/JSON 配置合并
    run_shadow.py                只读观察入口
    profiles/lumi.json           默认玩家档案模板
    player_config.example.json
    tests/
```

运行时数据默认在 `E:\BetterAGI\player\`：

```text
player/
    profile/profile.json        首次运行从模板创建，可手工编辑
    state/resume_state.json     每 30 秒 / 关闭时原子保存
    state/working.json          当前短期状态
    state/session_recap.json    上一场回顾（下一场注入）
    state/belief_state.json     信念
    state/relationships.json    关系
    state/emotion_state.json    情绪
    state/narrative_threads.json 叙事线索
    state/shadow/<session>.jsonl
    memory/episodes/<ep-id>.json
    memory/semantic/facts.json
    memory/evidence/<sha256>.json   屏幕 OCR 证据（可校验）
    memory/quarantine/<sha256>.json 旧导演记忆隔离副本（不参与召回）
```

## 运行

```bash
export BETTERGI_AGENT_TOKEN="<token>"
export OPENAI_API_KEY="<deepseek key>"   # 或 LLM_API_KEY
export LLM_PROVIDER=openai
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-flash
export LLM_MAX_TOKENS=4096
export OPENAI_REASONING_EFFORT=low
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/run_shadow.py \
  --config integrations/player/player_config.example.json
```

只验证链路、不调用 LLM（不需要密钥）：

```bash
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/run_shadow.py --no-llm
```

和录制/导演一起启动：

```bash
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/session/run_episode.py \
  --token-file E:/BetterAGI/tools/bettergi_token.txt \
  --director-config integrations/lumine/director_config.json \
  --lumine-config integrations/lumine/bridge_config.json \
  --player-shadow-config integrations/player/player_config.example.json
```

`run_episode.py` 新增两个参数：`--player-shadow-config`（为空则不启动）
与 `--player-shadow-script`（默认指向本目录的 `run_shadow.py`）。
录制结束时先给 shadow 最多 8 秒优雅退出，再强制结束。

## 开关与回滚

- 不传 `--player-shadow-config`、不运行 `run_shadow.py`：本阶段完全不生效；
- `PLAYER_AGENT_ENABLED=0`：即使误启动也立即退出；
- `--disabled`：同上的 CLI 形式；
- 删除 `E:\BetterAGI\player\state\resume_state.json`：回到空 resume
  （不影响档案与已有 shadow 记录）。

## 配置项

| JSON 键 | 环境变量 | 默认 |
|---|---|---|
| `enabled` | `PLAYER_AGENT_ENABLED` | `true` |
| `shadow` | `PLAYER_AGENT_SHADOW` | `true`（Phase 1 只支持 shadow，`0` 会拒绝启动） |
| `bettergiWs` | `BETTERGI_LIVE_WS` | `ws://127.0.0.1:30648/ws/v1/agent/live` |
| `sessionId` | `BETTERGI_SESSION_ID` | `vtuber` |
| `interval` | `PLAYER_INTERVAL` | `12` 秒 |
| `reconnectDelay` | `PLAYER_RECONNECT_DELAY` | `3` 秒 |
| `maxFrameAgeMs` | `PLAYER_MAX_FRAME_AGE_MS` | `5000` |
| `stateSaveIntervalMs` | `PLAYER_STATE_SAVE_INTERVAL_MS` | `30000` |
| `contextBuilder` | `CONTEXT_BUILDER` | `v2`（`legacy` 回退 Phase 1 拼接） |
| `profile` | `PLAYER_PROFILE` | `<PLAYER_HOME>/profile/profile.json` |
| `state` | `PLAYER_STATE` | `<PLAYER_HOME>/state/resume_state.json` |
| `shadowDir` | `PLAYER_SHADOW_DIR` | `<PLAYER_HOME>/state/shadow` |
| `memoryDir` | `PLAYER_MEMORY_DIR` | `<PLAYER_HOME>/memory` |
| `episodesDir` | `PLAYER_EPISODES_DIR` | `<memoryDir>/episodes` |
| `semanticPath` | `PLAYER_SEMANTIC_PATH` | `<memoryDir>/semantic/facts.json` |
| `workingPath` | `PLAYER_WORKING_PATH` | `<PLAYER_HOME>/state/working.json` |
| `recapPath` | `PLAYER_RECAP_PATH` | `<PLAYER_HOME>/state/session_recap.json` |
| `beliefPath` | `PLAYER_BELIEF_PATH` | `<PLAYER_HOME>/state/belief_state.json` |
| `relationshipPath` | `PLAYER_RELATIONSHIP_PATH` | `<PLAYER_HOME>/state/relationships.json` |
| `emotionPath` | `PLAYER_EMOTION_PATH` | `<PLAYER_HOME>/state/emotion_state.json` |
| `threadPath` | `PLAYER_THREAD_PATH` | `<PLAYER_HOME>/state/narrative_threads.json` |
| — | `PLAYER_HOME` | `E:\BetterAGI\player` |
| — | `BETTERGI_AGENT_TOKEN` | 空 |
| — | `PLAYER_NO_LLM` | `false` |

令牌只从 CLI、`--token-file` 或环境变量读取；JSON 配置里的 `token`
被忽略，也不会写入 profile/state/shadow 记录。

## ContextBuilder 与记忆

- 默认 `v2`：身份、当前观察、working、当前意图、resume、episodic recall、
  semantic facts、公开解说、输出 schema 各自独立预算；超限逐条裁剪并记录
  `truncated/items/dropped`，不是整段截断。
- `CONTEXT_BUILDER=legacy`（或 `--context-builder legacy`）完全回退 Phase 1
  的 `build_shadow_prompt`。
- 每轮从本地 episode/fact 检索，结果带 `source/time/score`；分数用于
  排序与 shadow 诊断，不写进 prompt。模型生成的 episode 是未验证的影子记录，
  不代表实际执行过的动作。
- OpenViking 外部召回仍然禁用：现有共享目录可能包含导演摘要或世界知识，
  尚不能逐条证明它们对玩家可见。
- 玩家已知事实只能来自**本轮截图 OCR 中的逐字引文**（`--observe-ocr`），
  并标注为“未校对”；模型给出的 `learned_facts` 只是候选，不会自动入库。
- 双写只覆盖 shadow 的 resume 与 working/episode；旧导演的 memory_patch
  未改动，可用 `player_legacy_import.py` 复制到隔离区（不参与召回）。

### 屏幕观察与区域 OCR（可选）

`--observe-ocr` 启用 BetterGI 屏幕观察，但必须额外提供按原始截图校准的
`--ocr-roi X Y WIDTH HEIGHT` 才会请求 OCR；未配置时仅观察 UI，不做全屏 OCR。
`--dialogue-roi LEFT TOP RIGHT BOTTOM`、`--choice-roi LEFT TOP RIGHT BOTTOM`
使用 0–1 的归一化边界，必须按实际画面校准。观察会在 BetterGI 写事件，
默认间隔 30 秒，不能保证捕获短对话；Live 帧当前不提供可靠的对话分类。

### 运行模式与意图安全门

`--mode observer` 是默认模式；此模式拒绝任何 `--intent-url` 或
`PLAYER_INTENT_URL`。`motor_active` 和 `full_active` 在动作归属、对话硬门、
单写者和崩溃恢复等安全门验证之前会明确拒绝启动。Bridge 的意图协议已有部分
实现，但这不等于主动接管已经可用。

### 导入旧导演记忆（可选）

```bash
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe integrations/player/player_legacy_import.py \
  --input "E:/BetterAGI/BetterGI/User/AgentSessions/<id>/memory.json" \
  --quarantine-dir E:/BetterAGI/player/memory/quarantine
```

副本标记为 `visibility=quarantined`、`verification=unverified`，不会被召回；
旧文件不会被改动。

## 安全边界

1. 只读：默认不持有任何 BetterGI HTTP 客户端，也没有动作/指导/解说发送路径；
   `--observe-ocr` 只请求只读观察接口，禁止跟随重定向且只允许本机回环地址；
2. shadow 记录器对所有字符串做令牌脱敏；
3. 档案禁止令牌类字段，`boundaries.no_spoilers=false` 直接拒绝加载；
4. 新的 shadow 记忆不接入 lore/世界数据库；现有导演 guidance、profile 和
   手动导入的本地事实仍是受信输入，尚不能保证它们不含剧透；
5. 帧龄按 `timestampUtc`（采集时间）计算；重连补发的旧帧不会因为「刚收到」
   被当成新帧；
6. `live.started`、帧序号回退、hello 会话变化与 WS 重连都会清空旧画面/
   旧指导/旧解说/旧动作状态；
7. 导演的内部思考（`meta.internal` / `kind=thought`）和动作回显里的内部
   推理都不进入 shadow 的「最近解说」；
8. episode/fact 只接受 `visibility=player_knowledge`，`world_only` /
   `ground_truth` 直接拒绝；检索与 ContextBuilder 再做一次硬过滤；
9. 已知事实必须能回溯到当帧 OCR 证据；证据被改、过期或不匹配时拒绝召回；
10. 记忆文件带 `schema_version`，损坏备份、过新拒绝、写入原子、跨进程加锁；
11. 记忆检索失败、episode/fact 写入失败都只降级并记录，不会中断 shadow。

## 测试

```bash
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest integrations/player/tests -q
```

## 已知限制

- 仍是 shadow：speech 决定只写 shadow，不会进入实况录制或 TTS；
  要替换旧导演 commentary，需要 Active 模式接线（未实现）；
- 真实 AutoDL/Lumine、原神实机、飞鱼 TTS 与 30–60 分钟盲测尚未执行；
- OCR 无置信度，对话锁可能误判；短对话可能落在两次 observe 之间；
- 尚未把一段对话整理成 conversation event 写入 episodic memory；
- 四层认知循环未拆分，当前只有单一 deliberative tick；
- C# 动作回显不带 `intent_id`，动作归属靠 bridge 最近意图近似；
- 同一 state / memory / shadow 目录只允许一个 shadow 进程，没有锁；
- shadow JSONL 与 episodes 只追加、不轮转。
