# README · ProAct 式 AI 主播：实现级设计（2026-11-04）

> 一句话：把「Omni realtime 当嘴和眼睛 + GameScaling 2B 当手 + DeepSeek flash 当偶尔醒来的脑子 + Graphiti 当记性」落成一套**能直接开工改代码**的设计，而不是又一份方向性 PPT。

## 这套文档是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 实现级契约：字段、时序、状态机、file:line 级改造点、验收剧本 | 架构畅想、技术选型对比、给领导看的汇报 |
| 基于**用户 55+ 轮拍板**的约束集合（见 00_decisions.md） | 我自己替用户做的产品决策 |
| 对现有 `E:/BetterAGI/agent/` 编排层的改造清单 | 从零重写、新框架、新仓库 |

硬前提（用户口径）：**不改现有代码结构重写**，直接改造 `agent/` 编排层；慢系统模型用 deepseek flash；子代理只用 DeepSeek 供应商。

---

## 目录与阅读路径

| 文件 | 内容 | 什么时候读 |
|---|---|---|
| `01_architecture.md` | 分层、角色职责、主时序 mermaid、状态机、互斥与「画面冻结」、交棒协议、旁路子代理、记忆读写、时间轴与录播、弹幕链路、故障熔断、与 ProAct 论文的对应、7 条不变量 | **先读这个**，建立整体图 |
| `02_schemas.md` | 唯一字段口径：意图包、见闻总结模板、慢系统输出、mailbox 事件+deadline、时间轴、便利贴、Graphiti 节点/边、13 个工具签名、弹幕 API、事故留证 | 写代码前必读；改字段先改这里 |
| `03_code_change_plan.md` | 模块地图（A）、12 行差异表（B）、14 组改造清单带 file:line 与 P0/P1/P2（C）、8 个风险点含分步修法（D）、4 周落地顺序 | 开工时当工单看 |
| `00_decisions.md` | 决策日志：每条决策 + 用户原话 + 影响面；含被推翻/待定项 | 想改设计时先查这里有没有拍过板 |
| `04_acceptance.md` | 三级门槛（能跑/能播/像人）、不变量断言 A1-A9 的位置、13 个演练剧本（S1-S13）、19 项必打点指标、30 分钟人肉自查、回滚止损顺序 | 上线前逐条过；改完 P0 后当回归清单 |

三条推荐路径：

```text
新人 20 分钟：README → 01 §1/§2/§4 → 02 §2/§3 → 03 §0
实现者：      03（工单）→ 02（契约）→ 01 §14（不变量）→ 04（怎么算做完）
评审/自己看： 00（为什么这么定）→ 03 §D（风险）→ 04 §三（演练）
```

---

## 架构一页图

```text
          ┌──────────── 慢系统 DeepSeek flash（≤2s，关 thinking）─────────────┐
          │ 读：见闻总结 + 记忆Top8 + 弹幕 + 最近30轮意图  →  出：一段 markdown 任务 │
          │ 工具：sticky 读写 / 记忆读写 / 弹幕 / 上一场 / dispatch / hook / plan     │
          └───────────────┬──────────────────────────────────────────────────────┘
                          │ dispatch_task（意图包 = 任务 + 便利贴全量 + 首帧）
                          ▼
   ┌──────────── 快系统（每意图新建会话，结束即断）────────────┐
   │  Omni realtime：看画面 5-10Hz / 解说 / 判结束 / 4 个受限工具 │
   │  GameScaling 2B-S3：200ms = 6×33ms 键位 + 鼠标 XY + 滚轮     │
   └───────────────┬───────────────────────────────────────────┘
                   │ request_handoff(done|failed|blocked)
                   ▼
   见闻总结（一句话结论 + 我做了什么/看到/听到/状态/残留）→ 落账 → 下一轮

   并行副产物：时间轴 JSONL（speech/l2d/viewer，本场单调 ms）+ 10 分钟录播分片
   长期记忆：Graphiti（Episode/Fact/Mechanic/Place/Bit/Viewer/Plan/SelfState）
   程序性记忆：便利贴 sticky/<game>/<save_id>.md，key_binding 1K + game_rules 1K
```

## 全链路时序图（覆盖所有环节）

一张图从「开播前自检」走到「事故止损」，9 个阶段全在里面。看不清就复制下面这段 mermaid 源码到 mermaid.live 放大看——图本身就是可编辑的源文件，改流程时先改图再改代码。

阶段编号与 `01_architecture.md` §5 状态机的对应：**0** = `IDLE → PREFLIGHT_OK`；**1–3** = `PLANNING`（1 是首轮开播仪式，3 是 `dispatch_task` 那一刻）；**4–6** = `EXECUTING`（5、6 是执行期内的支路与卡关，不是独立状态）；**7** = `HANDOFF`；**8** = `SIGNING_OFF`；**9** = `ABORTED`（任意状态都可跳入）。

参与者图例：`DM` 弹幕服务 / `O` 编排层（全链路唯一写者）/ `S` 慢系统 DeepSeek flash / `M` Graphiti 记忆 / `K` 便利贴 sticky / `R` Omni realtime（嘴+眼）/ `H` GameScaling 2B（手）/ `P` 旁路子代理 / `C` 采集·推流·录制 / `V` TTS 与渲染 / `T` 时间轴 JSONL / `X` 熔断与事故目录。

```mermaid
sequenceDiagram
    autonumber
    participant DM as 弹幕
    participant O as 编排层
    participant S as 慢系统
    participant M as 记忆
    participant K as 便利贴
    participant R as Omni
    participant H as 手模型
    participant P as 子代理
    participant C as 采集
    participant V as TTS
    participant T as 时间轴
    participant X as 熔断

    rect rgb(238,244,255)
    Note over O,X: 阶段 0 开播前 pre-flight（六项全绿才往下走）
    O->>C: 游戏窗口在 截屏取得到
    O->>R: Omni 握手 额度够
    O->>H: vLLM 探活 /v1/models
    O->>DM: GET /danmaku?since=0
    O->>M: 连通 并读上一场 episode
    O->>K: sticky 合法 ≤2048
    O->>T: 建 run 目录 定单调 t0
    end
    rect rgb(240,250,240)
    Note over O,X: 阶段 1 开播仪式（固定动作）
    O->>S: 上一场钩子 待议弹幕
    O->>R: 开场话术 上次回顾
    R-->>O: speech
    O->>V: 出声 压住音乐
    O->>DM: 读上舰打赏 逐条谢
    end
    rect rgb(255,250,235)
    Note over O,X: 阶段 2 规划 PLANNING（慢系统独占 冻结 ≤2s）
    DM-->>O: 弹幕增量
    O->>M: search_memory top8 过滤 game 与 save_id
    M-->>O: 只给已获得的 防剧透
    O->>S: 见闻总结 记忆 弹幕 近30轮 约束
    opt 复习或学到了
    S->>K: read_sticky 或 write_sticky
    end
    Note over O,V: 900ms 无 token 贴「大肥鱼CPU烧了」 2s 降级
    Note over R: 此刻活跃 realtime 会话必须为 0
    S-->>O: dispatch_task
    end
    rect rgb(244,240,255)
    Note over O,X: 阶段 3 派发（一次 reset 一个新会话）
    O->>O: 组装意图包 任务 便利贴全量 首帧
    O->>H: policy.reset
    O->>R: 新会话 只给 4 个受限工具
    O->>T: intent_start
    Note over R: 冷启 1-3s 用台词撑场
    end
    rect rgb(240,250,250)
    Note over O,X: 阶段 4 执行 EXECUTING（手嘴并行 细见图 2）
    par 手 每 200ms
        O->>H: 当前帧 前五帧 动作历史
        H-->>O: 六组键位 鼠标千分比 滚轮
        O->>C: 33ms 节拍发键 下一步边界才抢占
        O->>T: game_action
    and 嘴 5-10Hz
        O->>R: 帧 最近动作 执行结果
        R-->>O: speech 或 function call
        O->>V: 排队 说话压音乐
        O->>T: speech est_ms
    end
    end
    rect rgb(255,245,240)
    Note over O,X: 阶段 5 意图内支路（都不打断执行）
    opt 要参考
    R->>O: ask_slow_reference
    O->>P: 起子代理 看不到画面
    P-->>O: 建议
    O->>R: 作为 tool result 注入
    end
    opt 记一眼
    R->>O: note_observation
    O->>T: 落事件
    end
    opt 观众插话
    O->>R: 弹幕原文 不改任务
    end
    end
    rect rgb(255,240,240)
    Note over O,X: 阶段 6 卡关与开思考
    loop 意图内每轮
        R->>O: report_blocked
        O->>O: stuck_count +1
    end
    alt stuck_count 到 N
        O->>S: 本轮允许 thinking ≤10s
        O->>V: 贴图 放发呆台词
    end
    end
    rect rgb(240,240,250)
    Note over O,X: 阶段 7 交棒 HANDOFF（必有见闻总结）
    R->>O: request_handoff
    O->>R: cancel 再关会话
    O->>T: handoff intent_end
    O->>S: HandoffReport 结论 加 见闻
    S->>M: write_memory episode 加 事实
    S->>O: dispatch_task 下一轮
    end
    rect rgb(238,248,240)
    Note over O,X: 阶段 8 下播 SIGNING_OFF（四件事）
    O->>R: 下播话术 下次预告
    O->>S: 写 episode 录播索引 钩子
    S->>M: Plan 节点
    O->>C: 封最后一片 写 sidecar start_ms
    O->>O: session_index 校验同源
    end
    rect rgb(255,235,235)
    Note over O,X: 阶段 9 事故 ABORTED（五步硬序）
    O->>C: 一 冻结键位 全释放
    O->>T: 二 落账 末条是 incident
    O->>C: 三 停推流 分片封口
    O->>X: 四 写 incidents 五件套
    O->>O: 五 os exit 2
    end
```

**图 2（局部放大，可选读）**：阶段 4 里手、嘴两条车道的精粒度——200ms 一步、5-10Hz 推帧、各等待点的 deadline、以及「超时不空等」的降级路径。

```mermaid
sequenceDiagram
    autonumber
    participant O as 编排层
    participant R as Omni
    participant H as 手模型
    participant C as 采集
    participant V as TTS
    participant T as 时间轴

    Note over O,T: 图 2 执行期：两条车道互不等待 超预算记 miss 不空等
    O->>H: policy.reset
    O->>R: 新会话 冷启 1-3s 台词撑场
    par 手 200ms 一 chunk
        loop 每 chunk
            O->>H: act 当前帧 前五帧 动作历史
            Note over H: 单步预算 300ms 超时计 miss_rate
            alt parsed
                H-->>O: 6x33ms 键位 加 鼠标 加 滚轮
                O->>C: 逐 33ms 下发
                O->>T: game_action
            else 解析失败或截断
                H-->>O: MotorParseError
                O->>O: 进阶段 9 不造假不重试
            end
        end
    and 嘴 5-10Hz
        loop 每帧或动作完成
            O->>R: 帧 最近三条结果 剩余预算
            Note over R: 首 token 30s 单步 5s 先取消再重连
            R-->>O: speech 或 function call
            alt 可执行指令
                O->>H: 改写下一 chunk 目标 不换任务
            else speech
                O->>V: 排队 按 5.5 字每秒估时
                V-->>O: 真实时长
                O->>T: 回填 dur_ms 顺延
            end
        end
    end
    Note over O,T: 背压：帧只留最新 动作不合并 时间轴先落盘
    Note over O,T: 单调：t_ms 不下降 倒退即 incident 并修正为 last
```

---

## 一页结论：现在这个 loop 的十个硬伤（不修就不能直播）

按严重度排序，全部有 file:line 证据，详见 03 §D：

| # | 硬伤 | 现状证据 | 后果 |
|---|---|---|---|
| 1 | Omni `collect` 的 `while` 里 `recv_json()` **没有超时**，预算用完只 `return` 不取消 | `agent/omni.py:350/359`、`_drain_until_done`(`:293`)/`cancel_inflight`(`:317`) | 上一意图的事件流被下一意图读到 → **跨意图串台**，这是「59% 轮次空转」的根因之一 |
| 2 | 手模型解析失败被当成「合法空动作」继续跑 | `agent/motor.py:215-222` 用 `label_action`(`:52`) 造 6 组假动作 | 模型胡说的状态被当成执行成功，错误会累积 152 次同类 |
| 3 | `policy.reset()` 全仓零调用 | `agent/motor.py:117` 定义，`grep` 无调用点 | 跨意图历史污染，第一帧就带着上一段的动作分布 |
| 4 | 200ms 预算无人守 | `DEFAULT_TIMEOUT`=60s(`agent/motor.py:30`) vs `CHUNK_MS`=200(`agent/gamescaling_action.py:36-41`) | 单步卡 60s，手停下但嘴还在说「我在打」 |
| 5 | 手与嘴只共享 `stop`/`done` 两个 Event | `agent/executor.py:81/147/200` | 无互斥、无时序校验，`decide_ms` 只记不校验 |
| 6 | 9 个工具 0 实现，dispatch 只认 8 个名字 | `agent/agent.py:89/148`、`:1624` | 慢系统没有手脚，只能干说 |
| 7 | 便签有存储、零调用方，且上限是 per-profile 4000 字符 | `agent/profiles.py:236/251` | 程序性记忆不可达，且配额与 1K+1K 不一致 |
| 8 | 弹幕直塞 user 消息 | `agent/tui.py:852-855` | 观众一句话就能改当前任务 → 手抖 |
| 9 | 记忆只写不读、不是五层 | `agent/memory.py:47`（Episode 无 layer/atoms）、`:439`（单值 group_ids） | 「记得这游戏怎么玩」是假的 |
| 10 | 无录播/时间轴产物 | `agent/` 内 `ffmpeg` / `mp4` 零命中 | 只有实时画面，没有可回放、可剪辑、可对账的素材 |

补充一条好消息：三槽位提示词**已经实现**（`agent/prompt_builder.py:365-370`），只需把现成 `PromptBundle.text` 接进 `agent/gamescaling_request.py:104-105`（那里还在拼单条 system 串）。这省掉一整块活儿。

---

## 先做哪三件（第一周的最小闭环）

| 顺序 | 动作 | 完成标志 |
|---|---|---|
| ① | 交棒变成真工具：`request_handoff` / `report_blocked` 真正终止意图并产 HandoffReport | 意图结束必有一份见闻总结落盘，且旧 Omni 会话确认关闭 |
| ② | Omni 超时 + 会话化：每意图一建一毁，`recv_json` 带 deadline，超时**取消**连接 | 连续 100 个意图无串台（时间轴里不存在跨意图的 speech 事件） |
| ③ | 记忆闭环：便利贴 1K+1K 真正拼进手模型 prompt；慢系统能读上一场 | 手模型 system prompt 里能看到三槽；重启后仍能复述上一场结论 |

做完这三件，系统才「可控」；再有 4 周计划（03 §附）才是「像人」。

---

## 与第一轮审计的关系

| 轮次 | 产物 | 结论 |
|---|---|---|
| 第一轮（审计） | `E:/BetterAGI/Docs/analysis/proact-loop-audit-2026-11-04/` | ProAct 内核 = 有界压缩 + 五维打分门控 + 两级去重；我们有三个「假有」（意图总线死代码 / 记忆只写不读 / 便签不可达）；真实会话 59% 轮次空转 |
| 第二轮（本套） | 本目录 | 用 55+ 条用户拍板把「怎么做」钉死：schema、时序、改造点、验收 |

审计文档里的「五维打分 + τ=4 门控」**已被用户否掉**，改为事件驱动（realtime 主观交棒 + 卡关计数）。别拿旧文档当现状。

---

## 关键风险 Top 3

1. **实测未知量**：2B-S3 单步延迟可能 150-400ms 也可能更慢（主成本是 5 张历史帧 prefill）。设计里所有「200ms 流水线」都标注了「先打点再定」——**不要先假定它会快**，也不要为了凑 200ms 提前上双缓冲。
2. **慢系统冻结是故意设计**：思考期画面完全停（≤2s），超了贴「大肥鱼CPU烧了」。这会显得「卡」，但用户明确选了「简单可控」而不是「边想边动」。评审时不要把它当 bug 修。
3. **便利贴配额会互相挤**：1K+1K 是硬顶，模型很容易写超。写入事务必须校验+回滚（02 §7.3），否则一旦写进 3K 文本，手模型的 system prompt 会挤掉历史帧预算，动作质量断崖下跌。

---

## 待实测 / 待定（不许当成已定）

| 项 | 现状 | 谁定 |
|---|---|---|
| GameScaling 单步 p50/p95 | 未测；设计只给了 deadline 归属 | 打点后按实测决定是否双缓冲 |
| Omni 首 token p50/p95（含 1-3s 冷启） | 未测 | 决定「每意图新建会话」是否可接受 |
| 卡关计数阈值 N（开 thinking 的触发） | 未定，建议 3 | 用户/实测 |
| 发呆台词池内容 | 未写 | 人设侧补充 |
| `dispatch_task` 能否不经慢系统直接由提前规划子代理落 | 设计允许，未实现 | 实测后决定 |
| 弹幕服务真实接口是否与 02 §10 完全一致 | 由别项目实现 | 接入时对齐 |

---

## 交付物清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `README.md` | 324 | 本文件（入口，含全链路时序图 ×2） |
| `00_decisions.md` | 379 | 全量决策日志（D01-D124）+ 被推翻项 + 待定项 |
| `01_architecture.md` | 208 | 架构总设计 |
| `02_schemas.md` | 566 | 数据契约与 Schema（含 §6.2b 控制事件） |
| `03_code_change_plan.md` | 511 | 代码地图 + 14 组改造清单 + 8 风险 |
| `04_acceptance.md` | 348 | 三级门槛 + 13 个演练剧本 + 19 项指标 + 止损顺序 |
| `07_errata_measured_latency.md` | 60 | **勘误（必读）**：200ms 是游戏时间不是墙钟；实测 p95 740/1490ms |
| `CHECKSUMS.txt` | — | 全部文件 SHA256 |
| `proact-streamer-2026-11-04.zip` | — | 上述文件打包（保留目录结构） |

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-04 | v1.0 首版：由 55+ 轮用户问答 + 现有 `agent/` 代码审计共同产出 |
| 2026-11-04 | v1.0.1 一致性修订：意图包口径统一为「带便利贴全量、但不注入 realtime 上下文」（02 §2.1 样例补 `sticky`、删除未定义的 `landscape` 字段并说明不设完成条件字段）；04 的 L0-1 改回 pre-flight 六项、L0-5 与 A9 判据改为「进模型文本不得含便利贴/记忆正文」；00 的 D51 与 02 §3.3 七段模板对齐 |
| 2026-11-04 | v1.0.3 勘误：新增 `07_errata_measured_latency.md`。5090 真机实测（GameScaling-9B-S3，5 次/档，10/10 parsed）开思考 p50 1155 / p95 1484ms、关思考 p50 696 / p95 735ms；一次决策覆盖 198ms 游戏输入。**原「motor 单步 200ms 墙钟预算」作废**（02 §4 的 `motor_step 300ms`、03 §C.4 的 `deadline_ms=200` 均按 07 的新口径读）；实现已改为 `agent/pacing.py` 给预算，占空比 27%（关思考）/13%（开思考） |
| 2026-11-04 | v1.0.2 加图：新增「全链路时序图（覆盖所有环节）」——图 1 覆盖 9 个阶段（pre-flight → 开播仪式 → 规划 → 派发 → 执行 → 支路 → 卡关 → 交棒 → 下播 → 事故），12 个参与者、autonumber 编号，阶段号与 01 §5 状态机显式对应；图 2 是阶段 4 的局部放大（手 200ms / 嘴 5-10Hz 双车道、各 deadline、超时降级）。两图均用 mermaid 11.17.2 实际渲染校验通过（无语法错误、无溢出） |
