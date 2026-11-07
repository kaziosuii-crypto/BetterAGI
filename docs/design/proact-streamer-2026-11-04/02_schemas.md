# 02 数据契约与 Schema

> 版本 v1.0 · 2026-11-04 · 上游：01_architecture.md（结构/时序）· 下游：03_code_change_plan.md（改造落点）
> 本文件是实现的**唯一字段口径**。凡是本文件没定义的字段，实现方不得自行发明——先改本文件。

## 0 通用约定

| 约定 | 规则 | 理由 |
|---|---|---|
| 时间 | 全部用 `t_ms`：相对**本场直播开始**的单调毫秒（`t0`）。落盘另存 `t0_epoch_ms` 供外部对齐 | 系统时间会被 NTP 调，录播对不上，且用户要「时间轴跟 mp4 对得上」 |
| 标识 | `session_id = <YYYYMMDD>-<HHMM>-<seq>`；`intent_id` = ULID（字典序即时间序）；`save_id` 由人工指定 | intent 要在日志里排序、跨进程可读 |
| 编码 | 落盘一律 JSONL（一行一 JSON）+ UTF-8 无 BOM；文件名 ASCII | 追加写不重写全文件，断电只丢最后一行 |
| 命名 | 字段 `snake_case`；类型名 `PascalCase`；事件类型 `domain.action` | 与现有 `agent/` 代码风格一致 |
| 版本 | 每个落盘对象带 `schema_version: "1.0"`；JSONL 文件头第一条是 `{"kind":"header","schema_version":"1.0"}` | 允许边做边加字段（用户明确「先定最小字段集边做边加」） |
| 前向兼容 | **未知字段必须原样保留**（读→改→写不得丢）；读到未知 `kind` 只记 warning 不崩 | 两边并行开发时不会互相炸 |
| 大对象 | schema 里只放**相对路径引用**，不放 base64/图片二进制 | 时间轴要能直接用编辑器打开看 |
| 空值 | 未知用 `null`，不用空字符串；数组不用 `null`，用 `[]` | 下游少一处分支 |

### 0.1 目录布局

```
run/<session_id>/
  meta.json                  # session_id, game, save_id, t0_epoch_ms, build, persona_version
  timeline.jsonl             # §6 时间轴事件（唯一真源）
  session_index.json         # §12 意图索引 / 分片索引
  frames/                    # 5-10Hz 采样帧（环形保留最近 300 张 + 每意图首帧永久）
  danmaku.jsonl              # §10 原始弹幕落盘（与 HTTP 服务解耦）
  segments/seg_000.mp4 ...   # 10 分钟分片
  incidents/<ts>_<intent_id>/ # §11 事故留证
  handoffs/<intent_id>.md     # §3 见闻总结原文（给人看）
sticky/<game>/<save_id>.md   # §7 便利贴
```

---

## 1 数据流总览

```
用户/排期 → orchestrator
  → [慢] SlowPlan(task_md) ──dispatch_task──┐
                                            ▼
                              IntentPacket (§2) ── 喂 Omni realtime 会话
                                            │
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                      ▼
              GameScaling 手            realtime 嘴            timeline 事件 (§6)
              200ms 键位块              speech/l2d               录播分片索引
                     │                      │                      │
                     └──────────┬───────────┘                      │
                                ▼                                  │
                    HandoffReport (§3) ◄──── request_handoff ──────┘
                                │
                                ▼
              [慢] 读见闻+记忆+弹幕 → write_memory/sticky → 下一轮 SlowPlan
```

每条竖直链路都是一个**契约边界**：左边只认 JSON，右边不得直读对方内存。当前实现的问题正是边界被跳过（`agent/tui.py:852-855` 弹幕直接塞进 user 消息、`agent/motor.py:215-222` 解析失败时用 `label_action` 造 6 组假动作），所以 §2–§10 的每个 schema 都配了「禁止事项」。

---

## 2 IntentPacket：快系统意图包

### 2.1 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `intent_id` | string(ULID) | ✓ | 全链路唯一键，键位事件/见闻/记忆都挂它 |
| `session_id` | string | ✓ | |
| `goal` | string | ✓ | **自然语言目标描述**，给 realtime 看。用户明确：意图字段只选这一个 |
| `sticky` | object | ✓ | `{key_binding, game_rules}` **全量**，见 §7。用户明确：意图包带便利贴全量 |
| `first_frame` | string | ✓ | 相对路径，意图开始那一帧 |
| `constraints` | object |  | `{max_idle_ms:5000, no_story_spoiler:true, silence_budget_ms:5000}`，来自全局约束 |
| `speech_hints` | object |  | `{persona_ref:"persona.md", mood:null, banned:[]}`；口癖与行为准则写进 system prompt，不逐意图传 |
| `started_ms` | int | ✓ | 派发给 realtime 的时刻 |

```json
{
  "schema_version": "1.0",
  "intent_id": "01JCV8T2Q7X4M0K9R3F6YH2NBD",
  "session_id": "20261104-1930-01",
  "goal": "走到村口那个穿红衣服的 NPC 面前，跟他对话拿到钥匙；对话一开就算这轮成了，别在村里多转",
  "sticky": {
    "key_binding": "WASD 移动；E 交互/对话；Tab 打开物品栏；Esc 关闭界面",
    "game_rules": "村口 NPC 头顶有红色感叹号；对话需靠近到 2 格内按 E；夜里怪会主动打人"
  },
  "constraints": { "max_idle_ms": 5000, "no_story_spoiler": true, "silence_budget_ms": 5000 },
  "first_frame": "frames/01JCV8T2_first.png",
  "started_ms": 184320
}
```

### 2.2 为什么只有这么几个字段

- 用户明确「意图字段只选目标描述」：多字段会诱导 realtime 做结构化推理，而 realtime 的职责是**看着画面执行 + 解说**，规划在慢系统。
- `sticky` 必须全量而不是增量：GameScaling 是**无状态模型**（每调用吃 system prompt），它的 system prompt 由 `{GAME_NAME}/{GAME_RULES}/{KEY_BINDINGS}` 三槽拼成（现有 `agent/prompt_builder.py:365-370` 已实现三槽，只是 `agent/gamescaling_request.py:104-105` 还在拼单 system 串）。所以每次拼 prompt 都要完整便利贴，增量合并会漂移。
- `first_frame` 进意图包的理由：慢系统「不看画面」，但它需要一帧作为**对账锚点**（见闻总结描述的是哪一屏）。
- **不设「完成条件」字段**（`landscape` / `done_if` 之类一律不设）：用户拍板「完成判定只由 realtime 看画面」，程序侧不做判定、也不留判据开关。要表达的边界写进 `goal` 那一句自然语言里（如「对话一开就算这轮成了」），realtime 说 done 就 done。

### 2.3 禁止事项

| 禁止 | 因为 |
|---|---|
| 往意图包塞执行步骤（1.走 2.按 E 3.对话） | 违反「慢系统只出任务 markdown」，且会让 realtime 退化成脚本播放器 |
| 塞历史对话 | realtime 会话每意图新建，上下文只有本意图；历史靠见闻总结与记忆 |
| 塞弹幕 | 快系统不看弹幕（避免被带节奏导致手抖）；弹幕只走慢系统 |

---

## 3 HandoffReport：交棒载荷（见闻总结）

> 01_architecture.md §7 指向本节。

### 3.1 用户口径

> 「你说的这些都叫见闻总结」——所以载荷**不拆结构化字段**，统一一段 markdown 见闻总结 + 一句话结论。

代价与补偿：慢系统不需要学 schema（好事），但统计要靠后处理（坏事）→ 补一个**可选侧车** `stats`（P2，可缺省，缺了不许崩）。

### 3.2 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `intent_id` / `session_id` | string | ✓ | |
| `result` | enum | ✓ | `done` \| `failed` \| `blocked` \| `aborted` |
| `conclusion` | string | ✓ | **一句话结论**（≤60 字），慢系统第一眼看的 |
| `observations` | string(markdown) | ✓ | **见闻总结**：操作途中所看到听到的全部关键信息（详见 §3.3 模板） |
| `media.frames` | string[] |  | 关键帧相对路径，≤4 张（新场景/可疑点/失败现场） |
| `media.clip` | object |  | `{segment:"seg_000.mp4", from_ms, to_ms}`，交棒前 8s 片段 |
| `timing` | object | ✓ | `{started_ms, ended_ms, cycles, slowest_step_ms}` |
| `stats` | object |  | 侧车：`{actions, no_ops, repeated_errors, speech_chars, idle_ms}`（P2） |
| `last_frame` | string | ✓ | 交棒时刻画面 |

```json
{
  "schema_version": "1.0",
  "intent_id": "01JCV8T2Q7X4M0K9R3F6YH2NBD",
  "session_id": "20261104-1930-01",
  "result": "done",
  "conclusion": "拿到钥匙了，NPC 说晚上八点在塔楼见",
  "observations": "（markdown，见 §3.3 模板）",
  "media": { "frames": ["frames/01JCV8T2_key.png"], "clip": {"segment":"seg_000.mp4","from_ms":181000,"to_ms":189000} },
  "timing": { "started_ms": 184320, "ended_ms": 203980, "cycles": 92, "slowest_step_ms": 640 },
  "last_frame": "frames/01JCV8T2_last.png"
}
```

### 3.3 见闻总结模板（写进 realtime 的 system prompt，强制顺序）

```markdown
## 结论
（一句话，写清成没成、卡在哪）
## 我做了什么
（按时间顺序的关键动作，不要逐步流水账，挑转折——第一次尝试/换方法/失败重试）
## 看到了什么
（画面：新场景长什么样、地图/门/物品位置、UI 上出现的提示文字、数值变化）
## 听到了什么
（NPC 台词原文要点、音效暗示如开门声/脚步声/警告音）
## 我现在在哪 / 状态如何
（位置、血量/资源/道具、是否被通缉/是否在战斗中）
## 还残留什么
（没做完的、正在倒计时的、锁着的、需要回来的）
## 值得记的
（新学的操作/按键、NPC 名字、地名——给慢系统写记忆用）
```

硬约束（实现时必须校验长度与必填段）：

| 约束 | 值 | 违反时 |
|---|---|---|
| `conclusion` 长度 | 8–60 字 | 截断 + warning |
| `observations` 长度 | 120–2000 字 | <120 记 `sparse_handoff` 指标（说明 realtime 在敷衍），>2000 截断 |
| 必含段 | `## 结论`、`## 看到了什么`、`## 我现在在哪` | 缺段不阻断，但计入 `handoff_quality` 指标 |
| 不许出现 | markdown 表格、代码块、超链接 | 去除（慢系统读的是散文） |

失败关闭口径：`result=aborted` 时 `observations` 可以是空串，但 `conclusion` 必须写清「为什么中止」，且必须带 `incidents/` 路径。

### 3.4 交付方式

1. 写 `run/<session_id>/handoffs/<intent_id>.md`（完整 markdown，人可读）。
2. 通过 mailbox 发 `intent.end` 事件（§5），载荷是 §3.2 JSON，`observations` 内联全文。
3. 慢系统 prompt 里以「上一轮见闻」区块注入，**不落进对话历史**（防上下文累积）。

---

## 4 SlowPlan：慢系统输出

### 4.1 形态

慢系统只输出**一段稍规整的 markdown**，程序按模板把它和便利贴拼成 GameScaling 的 system prompt。慢系统不写 JSON、不写函数调用参数（工具调用读 `02 §9`，那是另一条通道）。

```markdown
## 这一轮做什么
（一句到三句，可执行的自然语言目标）
## 就算成了
（画面出现什么就该交棒）
## 别做
（可选：避免剧透/避免耗资源/避免某些行为）
```

### 4.2 程序侧拼装（唯一拼法）

```text
system = TEMPLATE.render(
  GAME_NAME   = meta.game,
  GAME_RULES  = sticky.game_rules,      # ≤1024 字符
  KEY_BINDINGS= sticky.key_binding,     # ≤1024 字符
  TASK        = slowplan 的『这一轮做什么』,
  DONE_HINT   = slowplan 的『就算成了』,
  SPEAK_POLICY= persona 行为准则（口癖/静默上限/失败反应）
)
```

约束：

- `Task` 文本 ≤400 字（超过说明慢系统在写剧本，退回重写）。
- 模板里三槽的顺序与分隔符**必须**与官方 GameScaling README 一致（`{GAME_NAME}/{GAME_RULES}/{KEY_BINDINGS}`），改顺序会让 2B-S3 掉点。
- 不允许把便利贴内容再塞进 task 文本（重复会挤掉历史帧预算）。

---

## 5 Mailbox：进程内事件总线

现有实现的串台根因是**没有信封、没有超时**（`agent/omni.py:350` 的 `while` 内 `agent/omni.py:359` `recv_json()` 无 `wait_for`）。所以统一信封 + 每类队列上限是硬要求。

### 5.1 信封

```json
{ "v":1, "ts_ms":184320, "type":"intent.dispatch", "id":"01JCV8...", "trace":{"intent_id":"...","session_id":"..."}, "payload":{...} }
```

### 5.2 事件表

| type | 方向 | 载荷 | 队列 | 丢弃策略 |
|---|---|---|---|---|
| `intent.dispatch` | 慢→编排 | IntentPacket §2 | 1 | 不丢（覆盖前必须已取消旧会话） |
| `intent.end` | 编排→慢 | HandoffReport §3 | 8 | 不丢 |
| `action.block` | 手→编排 | `{intent_id, t_ms, keys[6], mouse:[x,y], scroll}` | 64 | 不丢（丢键位=动作错乱） |
| `speech.text` | realtime→编排 | `{text, emotion, target_l2d}` | 256 | 不丢 |
| `tts.audio` | TTS→编排 | `{speech_id, dur_ms, path}` | 64 | 丢最旧 |
| `frame.sample` | 采集→编排 | `{path, t_ms}` | 8 | **只保留最新**（5-10Hz，慢了就丢） |
| `danmaku.batch` | 弹幕服务→编排 | `{items[]}` | 8 | 合并 |
| `tool.call` / `tool.result` | 双向 | §9 | 32 / 32 | 不丢（要配对） |
| `state.change` | 编排 | `{from,to,reason}` | 32 | 不丢 |
| `timeline.event` | 任意→时间轴 | §6 | 4096 | 不丢（丢=录播错位） |
| `incident` | 编排 | §11 | 4 | 不丢 |

### 5.3 每个等待点必须带 deadline

| 等待点 | 上限 | 超时动作 |
|---|---|---|
| Omni 首 token | 30000ms（含 1-3s 冷启） | 重连一次；再超 → `request_handoff(blocked)` |
| Omni 单步 token | 5000ms | 取消本次流，记 `omni_step_timeout`，不重发 |
| GameScaling 单步 | **墙钟按实测 p95：关思考 740ms / 开思考 1490ms**（原写 300ms 已作废，见 `07_errata_measured_latency.md`；200ms 是游戏时间） | 丢弃本步，**不造动作**（禁止 `label_action` 那种假动作），连续 3 次 → 熔断 |
| TTS 首帧 | 2500ms | 跳过音频，说话记为「无声」并继续 |
| 慢系统整轮 | 2000ms | 贴「大肥鱼CPU烧了」→ 降级为「保持当前意图重试一次」 |
| 工具调用 | 800ms（`search_memory` 2000ms） | 返回 `{"error":"timeout"}` 给模型，不阻塞主循环 |

---

## 6 时间轴 timeline.jsonl

用户口径：**只要 speech / l2d / viewer 三类事件** + 一条一直跟录、时间轴对得上的分片 mp4。

### 6.1 事件基形

```json
{"schema_version":"1.0","t_ms":184320,"type":"speech","id":"01JCV8...","intent_id":"...","payload":{...}}
```

### 6.2 三类事件

| type | 载荷 | 约束 |
|---|---|---|
| `speech` | `{text, voice, est_ms, dur_ms, tts_path, interrupted_by, ducking:{game_gain_db}}` | `est_ms` 先写（字数/5.5 字每秒），`dur_ms` 音频回来后**回填同一条**（不改 `t_ms`，改 `payload`，追加 `revision`） |
| `l2d` | `{expr, motion, params, dur_ms, source:"slow\|fast\|auto"}` | 慢系统可选择性改；快系统只读当前表情 |
| `viewer` | `{danmaku_id, uid, nick, kind:"danmaku\|gift\|superchat\|follow\|guard", text, responded:bool, responded_by_intent}` | `responded` 由 realtime 实际说话命中判定（简单规则：5s 内 speech 文本含观众昵称或弹幕关键词） |

### 6.2b 控制事件（工程必需，同一文件、同一时钟）

用户口径的「三类事件」指**播出侧内容**（speech / l2d / viewer，观众能感知的东西）。但离线断言、事故复盘、性能打点要能只看一个文件就还原「谁在什么时候做了什么」，所以同一份 `timeline.jsonl` 里还要写**控制事件**——它们不进剪辑、不进叙事，只给 `04_acceptance.md` §二 / §七B 的断言器消费。

| type | payload | 谁写 | 04 的哪条断言吃它 |
|---|---|---|---|
| `intent_start` | `{task_md, save_id, goal_short}` | 编排层（dispatch_task 之后） | A6 配对、A1 会话数、意图边界唯一真源 |
| `intent_end` | `{reason: done/failed/blocked/aborted, conclusion}` | 编排层 | A6 配对（start 数 == end 数） |
| `game_action` | `{step, keys, mouse, planned_ms, actual_ms, decide_ms}` | 手模型执行循环 | A3 归属、L1-5 抖动、L1-7 空转率 |
| `handoff` | `{reason, conclusion, seen_summary_ref}` | request_handoff 工具 | A6、卡关率、重复交棒 |
| `freeze` | `{reason: slow_think/incident/rest, ms, restored}` | 编排层 | S3（900ms 贴图 与 2s 降级） |
| `incident` | `{kind, dir}` | 熔断链路 | S1、T1-T7；必须是该 session 最后一条 |
| `net_reconnect` | `{attempt, subsystem: omni/tts/danmaku}` | 重连器 | S2 |

规则：

1. 控制事件与播出事件**共用 `t_ms` 与 `id` 基形**，只是 `type` 不同；不要为控制事件另开文件——两个文件两个时钟就是历史 bug 模式。
2. `game_action` 的 `jitter_ms` 不落盘（消费方按 `actual_ms - planned_ms` 算），避免双写不一致。
3. `incident` 之后**不允许**再有 `game_action`：断言器 A8 直接检查「最后一个 incident 之后是否还有任何动作事件」。
4. 剪辑/发布侧默认过滤掉全部控制事件；若要把时间轴交给外部剪辑项目，先导出 `timeline_viewer.jsonl`（只留 speech / l2d / viewer）。

### 6.3 时钟与录制规则（硬约束）

| 规则 | 实现 | 违反后果 |
|---|---|---|
| 唯一时钟源 | 进程启动时取一次 `perf_counter` 基准 + `t0`，所有 `t_ms` 由它算 | 多处取时钟 → 分片与事件漂移 |
| 单调 | 写入前断言 `t_ms >= last_t_ms`（允许相等） | 倒退即 inc 指标 `timeline_regression` 并修正为 `last` |
| 10 分钟分片 | ffmpeg segment 模式，`-segment_time 600` | 分片命名不可依赖 ffmpeg 自增（用 `seg_%03d.mp4`） |
| 首帧锚点 | 每片第 1 帧写 `{"kind":"segment_header","file":"seg_003.mp4","start_ms":1800000,"frame_no":54120}` 到 `session_index.json` | 丢失则该片**不可用于剪辑**（标记 `orphan:true`） |
| 不二次取时钟 | 索引、事件、分片头全部来自同一变量 | 二次取时钟是历史 bug 模式 |

### 6.4 session_index.json

```json
{
  "schema_version": "1.0",
  "session_id": "20261104-1930-01",
  "game": "eldenring", "save_id": "run3",
  "t0_epoch_ms": 1793000000000,
  "intents": [ {"intent_id":"01JCV8...","goal_short":"去村口找红衣NPC","started_ms":184320,"ended_ms":203980,"result":"done"} ],
  "segments": [ {"file":"seg_000.mp4","start_ms":0,"end_ms":600000,"frames":15000,"orphan":false} ]
}
```

---

## 7 便利贴 sticky/<game>/<save_id>.md

### 7.1 形态（两块固定配额，严格 1024 + 1024 字符）

```markdown
<!-- sticky v1 | game: eldenring | save: run3 | updated_ms: 203980 | by: slow -->
## key_binding
- WASD -> 移动
- Mouse X Y -> 转动视角 / 瞄准（X>0 右转，Y>0 下看）
- E -> 交互 / 拾取
- Shift -> 翻滚（消耗耐力）
- Q -> 锁定目标
- R -> 切换道具；数字键 1-4 -> 使用道具栏

## game_rules
- 篝火处可休息、补状态、复活；休息会把已击杀的普通敌人刷新。
- 死亡后掉落的经验（卢恩）留在死亡点，可以回去捡一次；再死就没了。
- 打不过的敌人先绕开，等级别回来。
```

### 7.2 规则表

| 规则 | 值 | 校验点 |
|---|---|---|
| 总长 | ≤2048 字符（按 Unicode 码点计） | 写入前 |
| 每块 | ≤1024 字符 | 写入前 |
| 键名 | 必须在官方 `KEY_VOCAB`（74 token：字母大写 / zero..nine / One..Twelve / LB RB MB / Up Down Left Right / Shift Ctrl Alt Tab Caps Esc Space Enter Back Delete Insert Home End Pause Equal Minus Period Slash Quote）内 | 写入前 + 拼 prompt 前 |
| 写者 | **只有慢系统**（`write_sticky`），快系统无此工具 | 工具注册表 |
| 读者 | 只有程序拼 prompt 时读；realtime 不读（用户明确「快系统根本用不上」） | — |
| 淘汰 | **无淘汰机制**：按存档一个文件，慢系统整体重写 | — |
| 写时 | 慢系统**每次启动**都可调，不只下播（用户：「为了模拟人类学会游戏操作」） | — |

### 7.3 写入事务（失败必须回滚）

```text
1. 模型给出 full_text（两块都重写，不接受 diff）
2. 解析两块 → 长度/键名/必填校验
3. 写 sticky.tmp → fsync → 校验回读 → rename 覆盖（原子）
4. 旧版本留在 .bak（只留 1 版）
失败 → 删除 tmp，返回 {"error":"invalid","why":"..."} 给模型，让它重写；旧文件不动
```

> 现状警示：`agent/profiles.py:236/251` 已有便签存储但**零调用方**，且上限是 per-profile 4000 字符（不是 1K+1K）。改造时必须改成 §7.1 的形态，否则两个配额会互相挤。

---

## 8 Graphiti 图谱 schema

部署：本地 Docker（Neo4j 或 FalkorDB），只作**长期记忆**；短期记忆只留在上下文里（用户明确）。成本不管，但写入要克制（见 §8.4）。

### 8.1 节点

| 节点 | 关键属性 | 唯一键 | 说明 |
|---|---|---|---|
| `Episode` | `session_id, game, save_id, started_ms, ended_ms, summary, narrative, mood, outcome, salience` | `session_id` | 一场直播一条；`narrative` 供下播总结 |
| `Fact` | `text, subject, predicate, object, confidence, first_seen, salience, source_episode` | `normalize(text)+save_id` | 原子事实（「村口红衣 NPC 叫梅琳娜」） |
| `Mechanic` | `game, name, rule_text, verified, salience` | `game+normalized(name)` | **程序性记忆**，最值钱；`verified` 表示亲手验证过 |
| `Place` | `game, save_id, name, state(locked/cleared/visited), note` | `save_id+normalized(name)` | 地点/谜题/门/Boss |
| `Bit` | `text, kind(meme\|running_gag\|catchphrase), first_used_ms` | `normalize(text)` | 梗库；长期叙事用（吃瘪→立 flag→复仇） |
| `Viewer` | `uid, nick, first_seen, last_seen, tags[], rapport:int, notes` | `uid` | 常客；**不主动提**（对方发言才回应） |
| `Plan` | `text, due_session, status(open/done/dropped), origin_episode` | `uuid` | 跨场钩子/企划 |
| `SelfState` | `persona_ref, mood, energy, arc_stage, updated_ms` | `singleton` | 人设随观众反馈演化 |

### 8.2 边

| 边 | 从 → 到 | 用途 |
|---|---|---|
| `MENTIONS` | Episode → Fact/Mechanic/Place/Bit/Viewer | 溯源：这条记忆哪场来的 |
| `ABOUT_SAVE` | Fact/Place/Plan → (save_id 属性, 非节点) | 检索过滤（用属性，不建节点） |
| `NEXT` | Episode → Episode | 时序链，供「上次讲到哪」 |
| `USED_BIT` | Episode → Bit | 呼应「上次这个梗」 |
| `LED_TO` | Plan → Fact/Place | 企划兑现 |
| `KNOWS` | SelfState → Mechanic（`verified=true`） | 「我会这个操作」 |

### 8.3 检索（每轮注入）

```text
query_text = 最近 3 条意图 goal + 上一轮 HandoffReport.conclusion + 当前 SlowPlan 草稿
query_vec  = embed(query_text)          # 直接用上下文文本，不额外生成 query（用户明确）
filters    = {game: meta.game, save_id: meta.save_id}   # 标签过滤
top_k      = 8                          # Fact/Mechanic 混排，Mechanic 权重 ×1.5
budget     = 600-900 token（注入区块上限，超了按 salience 截断）
输出       = 『已知（记忆）』区块，只含文本，不含 id；不写回对话历史
```

**防剧透双保险**：① prompt 约定「记忆就是全部已知，不许引入记忆外的知识」；② 写入端只写**已获得**的信息（没走到的地方不写剧情、不写攻略）。检索结果里出现 `verified=false` 的 Mechanic 必须标注「（未验证）」。

### 8.4 写入时机与粒度

| 时机 | 写什么 | 粒度 |
|---|---|---|
| 每轮意图结束（落账） | `Episode` 追加摘要 + 从见闻总结抽 0-3 条 `Fact`/`Mechanic` | 两层粒度：episode 一条 + 原子事实若干（用户明确） |
| 慢系统 `write_memory` 工具 | 模型显式决定写的一条 | 一条一写，必须带 `confidence` |
| 读到弹幕 | 更新/新建 `Viewer` | 只写「对方说过的话/雷点/称呼」 |
| 下播 | `Episode.narrative` + `Plan`（下一场钩子）+ `SelfState.mood` | 一条 |

去重：写入前按唯一键查；相似度 >0.92 则 merge（`salience += 0.1`，更新 `last_seen`），不新增节点。**不遗忘，只加权**（用户明确），`salience` 只影响注入排序。

---

## 9 工具集 function schema

现状：`agent/agent.py:89/148` 定义了工具但 0 实现；`agent/agent.py:1624` 的 dispatch 只认 8 个名字。以下 13 个必须逐一落地，签名即契约。

### 9.1 慢系统（9 个）

```json
{"name":"read_sticky","description":"读取当前存档的便利贴（key_binding 与 game_rules 全量）",
 "parameters":{"type":"object","properties":{"save_id":{"type":"string"}},"required":["save_id"]}}

{"name":"write_sticky","description":"整体重写便利贴，必须同时给出两块内容；超过 1024+1024 字符会被拒绝",
 "parameters":{"type":"object","properties":{"save_id":{"type":"string"},
   "key_binding":{"type":"string","description":"键位说明，≤1024 字符"},
   "game_rules":{"type":"string","description":"游戏规则，≤1024 字符"}},
  "required":["save_id","key_binding","game_rules"]}}

{"name":"search_memory","description":"检索长期记忆，返回 Fact/Mechanic/Place 文本",
 "parameters":{"type":"object","properties":{"query":{"type":"string"},
   "top_k":{"type":"integer","default":8},"save_id":{"type":"string"}},"required":["query"]}}

{"name":"write_memory","description":"写一条原子事实或机制（已获得的信息才写）",
 "parameters":{"type":"object","properties":{"kind":{"type":"string","enum":["fact","mechanic","place","bit","viewer"]},
   "text":{"type":"string"},"confidence":{"type":"number"},"save_id":{"type":"string"}},
  "required":["kind","text"]}}

{"name":"read_danmaku","description":"读取最近未处理的弹幕",
 "parameters":{"type":"object","properties":{"limit":{"type":"integer","default":30},"since_ms":{"type":"integer"}}}}

{"name":"get_last_episode","description":"取上一场直播的叙事摘要与未完成企划",
 "parameters":{"type":"object","properties":{"game":{"type":"string"}}}}

{"name":"dispatch_task","description":"把这一轮的任务交给快系统执行",
 "parameters":{"type":"object","properties":{"task_markdown":{"type":"string"},
   "done_hint":{"type":"string"},"avoid":{"type":"string"}},
  "required":["task_markdown"]}}

{"name":"set_hook","description":"设一个下一场/稍后要兑现的钩子",
 "parameters":{"type":"object","properties":{"text":{"type":"string"},"due_session":{"type":"string"}},"required":["text"]}}

{"name":"set_plan","description":"写/改本场企划（内容企划由它自己定，用户只审批）",
 "parameters":{"type":"object","properties":{"text":{"type":"string"},"status":{"type":"string","enum":["open","done","dropped"]}},"required":["text"]}}
```

### 9.2 快系统（4 个，不含 read_sticky）

```json
{"name":"note_observation","description":"记一条值得带回给慢系统的观察（会进见闻总结）",
 "parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}

{"name":"request_handoff","description":"请求交棒：意图完成/失败/卡住",
 "parameters":{"type":"object","properties":{"result":{"type":"string","enum":["done","failed","blocked"]},
   "conclusion":{"type":"string"},"observations":{"type":"string"}},"required":["result","conclusion"]}}

{"name":"ask_slow_reference","description":"向旁路慢模型要一段执行参考（不打断当前执行）",
 "parameters":{"type":"object","properties":{"question":{"type":"string"},"mode":{"type":"string","enum":["advise","preplan"]}},
  "required":["question"]}}

{"name":"report_blocked","description":"报告卡住（触发交棒，同时计入卡关次数）",
 "parameters":{"type":"object","properties":{"why":{"type":"string"},"tried":{"type":"array","items":{"type":"string"}}},"required":["why"]}}
```

### 9.3 工具的权限与副作用

| 工具 | 副作用 | 幂等 | 超时 |
|---|---|---|---|
| `read_sticky` / `search_memory` / `read_danmaku` / `get_last_episode` | 只读 | ✓ | 800ms / 2000ms |
| `write_sticky` | 覆盖文件（事务 + 回滚） | 否（整体替换即幂等） | 800ms |
| `write_memory` | 写图谱 | 否（去重合并） | 2000ms |
| `dispatch_task` | 开意图、清空 realtime 会话 | 否 | 100ms（只投递） |
| `set_hook` / `set_plan` | 写一条 Plan | ✓ | 800ms |
| `note_observation` | 仅内存累积 | ✓ | 10ms |
| `request_handoff` / `report_blocked` | 终止意图 | 否（第二次忽略） | 10ms |
| `ask_slow_reference` | 起子进程调用（独立配额） | ✓ | 丢弃不阻塞 |

---

## 10 弹幕服务 API（已拍板，最简方案）

| 接口 | 返回 | 说明 |
|---|---|---|
| `GET /danmaku?since=<ms>&limit=50` | JSONL，一行一条 | `since` 用 `t_ms` 还是 epoch？**用 epoch ms**（外部服务不知道本场 t0），编排层收到后转 `t_ms` |
| `GET /stats` | `{"online":int,"likes":int,"guard":int,"updated_ms":int}` | 只做展示与节奏参考，不进 prompt 主路径 |

```json
{"id":"dm_88213","ts_ms":1793000123456,"uid":10086,"nick":"小夜的车轮","text":"左边有宝箱","kind":"danmaku","gift":null}
```

规则：

1. 编排层 `DanmakuBuffer` 环形 500 条；断线时 `since` 从最后一条继续，缺口 >60s 记 `danmaku_gap`。
2. **只有慢系统读**（`read_danmaku`），且只在 PLANNING 时读。快系统不看弹幕。
3. 与当前任务冲突（点单）→ **执行优先**，记入 `Plan`/`Bit` 桶，等意图结束再议（用户明确）。
4. 每条弹幕落盘 `danmaku.jsonl`，并作为 `viewer` 事件进时间轴（`§6.2`）。

---

## 11 事故留证 incident bundle

触发：手模型解析失败 / 连续 3 次 no-op / 连续 3 次单步超时 / 不变量违反 / 整体卡死 >60s。用户口径：**手解析失败=程序错误**，留证后直接断流下播。

```
incidents/<ts>_<intent_id>/
  README.md          # 自动生成：时间线摘要、触发点、结论占位
  request.json       # 发给模型的完整请求（含 system prompt 原文，便利贴内容已脱敏可留）
  raw_reply.txt      # 原始返回字节（不裁剪）
  parsed.json        # 解析中间态：tokens、解析器输出了什么、为什么判失败
  frames/            # 失败前后各 3 帧
  timeline_slice.jsonl # ±10s 的时间轴切片
  proc.json          # 进程状态：线程数、内存、active_sessions、队列深度
  env.json           # build、模型版本、GPU、驱动、checkpoint 路径
```

保留策略：最近 20 个或 2GB，先删最旧（但被人工标记 `keep` 的不删）。

---

## 12 与 03_code_change_plan.md 的对应

| 本文件 | 落点（03 的改造组） |
|---|---|
| §2 IntentPacket | C-3 意图+arbiter（`agent/executor.py`、`agent/agent.py`） |
| §3 HandoffReport | C-3 + C-5 慢系统两套闭环 |
| §4 SlowPlan | C-5 + C-7 三槽位（`agent/prompt_builder.py:365-370`、`agent/gamescaling_request.py:104-105`） |
| §5 Mailbox/deadline | C-1 omni 超时（`agent/omni.py:350/359`）+ C-2 会话化 |
| §6 时间轴 | C-11 采集与时间轴（新增） |
| §7 便利贴 | C-6 便签（`agent/profiles.py:236/251`） |
| §8 Graphiti | C-6 记忆（`agent/memory.py:47/439`） |
| §9 工具 | C-8 工具集（`agent/agent.py:89/148/1624`） |
| §10 弹幕 | C-9 弹幕（`agent/tui.py:852-855`） |
| §11 事故 | C-10 熔断 |

---

## 13 版本与演进规则

| 变更类型 | 处理 |
|---|---|
| 加可选字段 | 直接加，`schema_version` 次版本号 +1 |
| 改字段含义/删除 | 主版本号 +1，且必须写迁移脚本 + 兼容读一个月 |
| 加事件 type | 允许，读取方必须忽略未知 type |
| 时间轴字段 | **只加不改**（录播是对外产物，改字段等于历史素材不可复用） |

用户口径提醒：「先定最小字段集边做边加」——所以本文件刻意保留了 `stats`/`constraints` 这类可缺省位；但**已定义的字段语义不许悄悄改**，否则便利贴/时间轴/记忆三处会同时错。
