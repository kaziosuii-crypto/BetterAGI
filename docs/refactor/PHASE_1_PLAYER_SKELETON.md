# Phase 1：PersistentPlayerAgent 骨架（shadow）

状态：**已实现（2026-10-27）**，只读影子模式；旧导演/录制/操作链路保持不变。  
代码：`BetterGI/integrations/player/`  
运行数据：`E:\BetterAGI\player\`（可用 `PLAYER_HOME` 覆盖）

---

## 1. 结论

Phase 1 交付了一个可启动、可持久化、可测试的玩家主体骨架：

- 启动时加载玩家档案（profile）与上次 resume；
- 只读连接 BetterGI Live，观察帧、指导、解说、动作状态与会话边界；
- 按 deliberative 间隔生成「如果是我会怎么想/怎么做/想说什么」；
- 写入独立 shadow JSONL，并原子更新 resume；
- **没有任何发送动作/指导/解说的接口**，不持有 BetterGI HTTP 客户端。

旧 `live_director.py` 未修改；`run_episode.py` 只新增了两个可选参数：
`--player-shadow-config`（为空则不启动）与 `--player-shadow-script`
（默认指向 `integrations/player/run_shadow.py`）。录制结束时 `run_episode`
会给 shadow 最多 8 秒优雅退出窗口，让 `agent.close()` 写完 resume 与
`shadow_end`，超时才强制结束。

验收对应：

| 验收项 | 结果 |
|---|---|
| 启动加载 profile + resume（若存在） | 通过；假 Hub 重启后日志 `resume_loaded=True` |
| 旧 `live_director.py` 独立运行 | 未改动，169 项 Lumine 测试全绿 |
| shadow 只记录、不发送动作 | 无发送接口；记录均 `executed=false`；smoke 中只有 WS 接收 |
| 现有测试不回归 | 297 passed（169 + 20 + 108，Phase 1 完成时） |

---

## 2. 本轮钉死的歧义

| 问题 | 决定 | 理由 |
|---|---|---|
| 代码根目录 `agent/` | `integrations/player/` | 与 `lumine/`、`session/` 同级；沿用扁平导入 + tests 插入 `sys.path` 的仓库惯例 |
| 数据根目录 | `E:\BetterAGI\player\` | 遵循 TARGET_ARCHITECTURE §4；`PLAYER_HOME` 可覆盖 |
| profile 8 个文件 vs 单文件 | 单文件 `profile/profile.json`（含 8 个逻辑段） | Phase 1 只读、版本统一、避免多文件部分写入/漂移；各段与目标文档一一对应 |
| player_profile vs player/profile | 目录用 `player/profile/` | 与目标目录图一致 |
| state 文件范围 | 只实现 `resume_state.json` | emotion/belief/relationship/thread 属于 Phase 5，提前造空壳会过度设计 |
| resume 字段命名 | 采用 MEMORY_ARCHITECTURE §12 全名 | `active_story_threads` / `recent_important_memories` / `recent_character_relationships` / `things_i_want_to_check_next` |
| shadow 记录写到哪 | 独立 `state/shadow/<session>.jsonl` | 避免与录制器单写者/覆盖写冲突，也避免扩展 `EVENT_TYPES` |
| shadow 能否写 speech/guidance | 可生成 speech 文本，但只记录；禁止任何外发 | 验收只要求记录假设；不形成第二个执行主体 |
| 版本缺失/过新 | profile 缺版本报错；state 缺版本按 0 迁移；过新一律拒绝且不覆盖 | 档案是强约束输入，状态需要可恢复 |
| 损坏文件 | 备份为 `.corrupt-<时间戳>` 后重建空状态 | 不静默覆盖，保留人工恢复可能 |
| 新旧入口关系 | 独立进程；旧导演不变 | 录制器已是第二个 WS 客户端，协议支持多客户端广播；不需要改导演 |
| 开关默认值 | `enabled` 默认 true，但只有显式运行 `run_shadow.py` 或给 `run_episode` 传 `--player-shadow-config` 才生效；`PLAYER_AGENT_ENABLED=0` 可回滚；`PLAYER_AGENT_SHADOW=0` 拒绝启动 | 独立入口本身即显式开启，不启动时旧链路是默认；避免为了可运行性再增加一个必填开关 |
| 「director 作为 commentary-only fallback」 | 解释为「保留其运行能力，角色定位留到 Phase 3/7」 | Phase 1 不允许改动作链路，无法同时把它改成纯解说 |

---

## 3. 模块与职责

```text
integrations/player/
    player_io.py          同目录临时文件 + fsync + os.replace；损坏备份
    player_profile.py     加载/校验/默认值/稳定摘要/默认档案播种
    player_state.py       resume schema、版本检查、玩家归属、损坏恢复、原子保存
    player_proposal.py    宽容 JSON 提取 + 字段裁剪/限长/动作枚举
    player_context.py     Phase 1 最小提示词（Phase 2 替换为 ContextBuilder）
    player_agent.py       PersistentPlayerAgent：observe / tick / save_state / close
    player_loop.py        单线程 deliberative 循环（fast/reactive/deep 留待后续）
    shadow_recorder.py    追加式 JSONL、run_id、令牌脱敏
    player_config.py      CLI > 环境变量 > JSON > 默认；路径解析
    run_shadow.py         只读 WS 入口；断线重连；live.stopped 正常退出
    profiles/lumi.json    与小光 Lumi 人格一致的默认档案
```

### 档案（profile）

`schema_version=1`，包含：

- `identity`（name/pronouns/origin/role/self_description）
- `personality`（traits/flaws/quirks/baseline_mood）
- `values`
- `play_style`（pace/risk_tolerance/combat/exploration/puzzle/quest_focus）
- `preferences`（likes/dislikes/fears）
- `speech_style`（language/tone/catchphrases/max_sentence_chars/silence_bias/avoid）
- `long_term_interests`
- `self_history`（summary/milestones）
- `boundaries`（no_spoilers 强制为 true）

`stable_summary()` 不含时间戳、路径、令牌或裸数值；风险值等内部数值只转成
「偏谨慎/均衡/愿意冒险」这类自然语言。相同档案每次生成完全相同的摘要。

### resume（state）

Phase 1 实际写入：

- `last_session_id` / `last_run_id`
- `last_location` / `current_quest` / `current_intention`
- `things_i_want_to_check_next`
- `last_internal_thought`
- `last_frame_seq` / `shadow_ticks`
- `updated_at`

其余字段（active threads、beliefs、relationships、emotion 等）保持空值，
由后续阶段填充。未知字段原样保留，方便只增不删地升级。

---

## 4. 运行契约

启动顺序：

```text
解析配置 → 建目录 → 默认路径缺失时播种档案 → 校验 profile
→ StateStore.load（版本/归属/损坏）→ 构造 agent → 连接 WS
→ 每 interval 秒 tick → live.stopped 或 --duration 到时收尾
→ 保存 resume → 写 shadow_end
```

shadow 记录形如：

```json
{
  "schema_version": 1,
  "type": "shadow_tick",
  "mode": "shadow",
  "executed": false,
  "player_id": "lumi",
  "session_id": "vtuber",
  "run_id": "20261027-222455-ab12",
  "t_unix_ms": 1761575095000,
  "timestamp_utc": "2026-10-27T14:24:55Z",
  "frame_seq": 42,
  "guidance_seen": "沿斜坡往上",
  "proposal": {
    "thought": "...",
    "intention": {"goal": "...", "reason": "...", "horizon": "soon"},
    "speech": {"text": "...", "emotion": "curious", "intensity": 0.6, "should_speak": true},
    "proposed_actions": [{"hint": "walk", "detail": "...", "reason": "...", "executed": false}]
  }
}
```

`--no-llm` 时写 `shadow_observation`（帧序号、帧龄、指导、UI），不调用模型，
用于验证链路与录制环境。

---

## 5. 安全不变量

1. `PersistentPlayerAgent` 没有 `send/send_action/send_guidance/send_commentary/post`
   之类的接口；`run_shadow.py` 只建立 WS 连接并接收。
2. 不导入/不构造 `BetterGIClient`，不调用任何 BetterGI HTTP 写接口。
3. shadow 记录与正式 `session.jsonl` 分离，不改变录制器事件集合。
4. 记录器递归脱敏 `sk-*`、`Bearer *`、`access_token=*`。
5. profile 出现令牌类字段直接拒绝；`no_spoilers=false` 直接拒绝。
6. 提示词只含档案、resume 与当前观察；不接入 lore/世界数据库。
7. 帧龄按 `timestampUtc`（采集时间）计算，超过 `maxFrameAgeMs`（默认 5 秒）
   不思考；重连时服务器补发的旧帧不会因为「刚收到」被当成新帧。
8. `live.started`、帧序号回退（换场）或 WS 重连都会清空旧帧、旧指导、
   旧解说、旧动作状态；换场/重连分别留下诊断记录。
9. 配置中的 `token` 字段被忽略；令牌只来自 CLI/令牌文件/环境变量。
10. 每条 shadow 记录都带 `sent=false`、`executed=false`、`seq`、`run_id`，
    从数据结构上区分「记录」与「发送」。
11. 状态文件中的令牌类字段在加载时丢弃、保存前再次过滤；读取容忍
    Windows 编辑器写入的 UTF-8 BOM。
12. 旧版本状态首次写回前保留 `.v<旧版本>.bak`。
13. 导演的内部思考（`meta.internal` / `kind=thought`）不会进入 shadow
    的「最近解说」；shadow 只观察玩家能听到的公开解说。
14. shadow_tick 记录的 `guidance_seen` 是构造 prompt 时的值，不会在
    LLM await 期间被新 guidance 改写。
15. `live.stopped` / 重连清理观察前，最后帧序号会先写入 resume。

---

## 6. 测试与证据

```bash
E:/BetterAGI/Amaidesu/.venv/Scripts/python.exe -m pytest \
  integrations/lumine/tests integrations/session/tests integrations/player/tests -q
# 297 passed, 28 subtests passed
```

> 以上为 Phase 1 完成时的数字；Phase 2 之后 player 测试增至 161 项，
> 全量见 [PHASE_2_CONTEXT_MEMORY.md](PHASE_2_CONTEXT_MEMORY.md)。

新增 113 项（108 项 player + 5 项 run_episode 接线）：

- profile：加载/默认值/拒绝密钥/拒绝关闭剧透禁令/稳定摘要/播种；
- state：缺失、往返、UTF-8、损坏备份、过新拒绝、玩家归属、未知字段保留；
- recorder：追加/结束/幂等/脱敏/路径清理/坏行；
- proposal：围栏 JSON、前后解释、字符串内括号、字段限长、动作枚举；
- context：确定性、resume 注入、不注入路径/图片；
- agent：tick 落盘、无发送接口、重启续读、旧帧跳过、live 重置、
  动作回显 `block.chunks` 解析、guidance 优先级、帧序号回退换场、重连清理、
  LLM/解析失败不崩溃、状态节流与关闭强制保存；
- config/loop：层级优先级、令牌文件、URL 脱敏、间隔钳制、循环停止与异常隔离；
- 启动器：禁用返回 0、`PLAYER_AGENT_SHADOW=0` 拒绝、缺失自定义档案报错、
  默认档案播种、只读接收循环零发送。

假 Live Hub 冒烟（`tests/fake_live_hub.py`，只广播）：

```text
hello → 15 帧 + action_state → live.stopped
--no-llm --interval 1：frames_seen=15；写 shadow_observation；resume last_frame_seq=15
重启后 resume_loaded=True
```

真实 DeepSeek（deepseek-flash，1 次调用）：

```text
thought: 咦？这蓝得也太彻底了……先别乱动，看看左边能不能看出点什么。
intention: 往左边看看，确认四周环境
speech: 等一下——这画面怎么全是蓝的？（curious, 0.6）
proposed_actions: look（executed=false）
remembered: scene=一片蓝色的未知环境
shadow_tick 记录 executed=false / sent=false；记录与 state 中无令牌
```

这次调用也验证了图像真正到达模型（纯色测试图被正确描述）；无效 JPEG
会被模型以 HTTP 400 拒绝，属于预期错误路径。

### 6.1 对抗审查后加固

独立审查（4 个视角 + 逐条验证）后补的修复：

- 导演以 `commentary` 广播的 `meta.internal` 内心思考不再进入 shadow
  的「最近解说」（否则会把导演记忆/剧情带进玩家 prompt）；
- 密钥键名归一化支持 camelCase（`accessToken`/`groundTruth` 等），
  档案拒绝、状态剥离、记录器遮蔽共用同一实现；
- 中文紧邻的 `密钥sk-...已失效`、`令牌Bearer ...` 也能脱敏；连接异常
  日志同时按原值/URL 编码值替换令牌，并去掉 URL userinfo；
- 孤立代理项（`\ud800`）在写入前被替换，原子写失败会清理临时文件，
  不再让一轮 LLM 输出击穿 shadow 记录；
- 断线重连 / hello 会话变化 / 帧序号回退都会清理旧观察，`recent_actions`
  也纳入清理判断；`live.stopped` 先保存最后帧序号再清理；
- `run_episode` 结束录制时先给 shadow 8 秒优雅退出窗口，再强制结束；
  启动失败会清理已启动的兄弟进程；显式 `--ws-url` 会传递给 shadow；
- 空白环境变量不再覆盖配置文件里的 `enabled/shadow=false`；示例配置
  不再写死绝对路径，`PLAYER_HOME` 能真正改变数据目录。

尚未验证：真实 BetterGI + 原神运行时、AutoDL Lumine 同时运行。
AutoDL 实例目前处于停止状态。

---

## 7. 明确未做（后续阶段）

- ContextBuilder 分块预算（Phase 2）；
- 结构化 episodic memory / consolidation / OpenViking 写入（Phase 2/4）；
- intent / motor_feedback / Lumine 降级（Phase 3）；
- belief / relationship / emotion / thread（Phase 5）；
- dialogue perception / dialogue_lock（Phase 6）；
- speech policy 与实际 TTS 接入（Phase 7/8）；
- Phase 0.5 剩余项：动作回执、UI/对话门控、session append/resume、
  frame/audio 事件、world/player 知识硬隔离。

已知运行限制：

- 单写者假设：同一 state 路径 / shadow 目录同时跑两个 shadow 进程会
  互相覆盖或交错，Phase 1 不建锁；
- shadow JSONL 只追加、不轮转，长时间运行需要后续做按日/按场切分；
- 换场检测依赖帧序号回退（服务器不广播 live.started、重启后不重发
  hello）；长时间断线后同序号或更高序号理论上可能漏判；
- 导演 guidance 被标为「外部指导」，但 Phase 1 没有内容级剧透过滤。

Phase 1 只证明「骨架能持续存在、能读回过去、不会越权操作」；
它还不是一个完整的玩家。
