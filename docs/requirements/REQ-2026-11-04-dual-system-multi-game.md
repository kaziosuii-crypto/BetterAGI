# BetterAGI 双系统多游戏 Agent · 需求文档 v1.0

> 版本：v1.0（2026-11-04）｜状态：待确认｜范围：把单游戏单循环 agent 改造为“双系统 + 长期记忆 + 多游戏 profile”架构
> 上一版参考：ProAct（arXiv:2602.14048，北大，SIGGRAPH Asia 2026）双系统思想（慢认知 + 快行为 + 克制），仅借鉴架构思想，不引入其人形机器人动作生成部分。

## 0. 背景

当前实现（E:\\BetterAGI\\agent\\agent.py）是**单游戏（原神）、单循环、固定步数**的 ReAct：LLM 每步调一次工具，`lumine` 被当作“一段动作”调用，`pad` 允许直控虚拟手柄。问题：

- 没有真正的快慢分层：慢 LLM 每步驱动一切，实时性靠 lumine 单点撑；
- `pad`（虚拟手柄）实际无法把操作送进游戏，属于死代码与误导性工具；
- 没有长期记忆，换游戏/重启即从零开始，键位、规则、地点、教训全部丢失；
- 只服务一个游戏，无法按游戏分别管理。

本需求把这四点一次性改造到位。

## 1. 目标

1. **双系统**：慢系统（DeepSeek）负责抉择、记忆、规则学习；快系统（Qwen3.8-Omni-Flash-Realtime 流式）负责实时理解与下达指令；动作仍由 GameScaling 策略模型转成键鼠。
2. **长期记忆**：Graphiti 作为唯一长期记忆库，**每个游戏一个 group_id**，按游戏严格隔离。
3. **短期记忆**：LLM 可自写“提示纸条”（scratchpad），设字符上限，防止无限写入。
4. **提示词协作**：快系统启动时由慢系统从记忆注入 {GAME_NAME}/{KEY_BINDINGS}/{GAME_RULES} 构造 GameScaling 系统提示词；快系统学到新键位/规则后回传，慢系统更新记忆并重启快系统。
5. **多游戏 profile**：每个游戏一套独立 profile（配置、记忆作用域、纸条、日志），游戏名严格对齐。
6. **移除 pad**：完全删除虚拟手柄相关代码、工具定义、测试与文档引用。

## 2. 术语

| 术语 | 含义 |
| --- | --- |
| 慢系统 Cognitive | DeepSeek 慢循环：抉择意图、维护记忆、学习规则、审批反馈 |
| 快系统 Behavioral | Qwen Omni 流式循环：读画面/对话，实时产出指令（给 GameScaling） |
| 动作层 | GameScaling 策略：接收指令 + 画面 → 输出 200ms 键鼠序列 |
| profile | 一个游戏的全套运行配置与数据目录 |
| group_id | Graphiti 记忆分区键，与游戏一一对应 |
| 便签 scratchpad | LLM 可读写的短期记忆文本，有字符上限 |
| 键位表 keymap | 游戏内按键 → 效果 的映射（记忆内容之一） |
| 规则库 rules | 游戏规则/机制/目标（记忆内容之一） |

## 3. 功能需求

### FR-1 删除 pad（P0）
- 删除 `agent/pad.py`、`TOOL_SPECS` 中的 pad 工具、`BetterGITools.pad()` 与 dispatch 分支、`--allow-stub-motor` 相关提示、tests 中 pad 用例；
- 全仓库 grep 确认无引用（除归档说明）；
- 文档/提示词中删除“两套腿”的第二条。

### FR-2 慢系统改为 DeepSeek 官方接口（P0）
- `agent/lithos.py` provider 默认改为 `deepseek`：base_url `https://api.deepseek.com/v1`，模型 `deepseek-flash`；
- 三档（base/fast/ultra）保留字段但映射到同一模型，仅超时不同；urgency 仍影响超时与重试策略；
- 保留 thinking 关闭/回传 reasoning_content 的既有约束（已验证）；
- 慢系统允许慢（1–5s），不做激进超时压缩。

### FR-3 快系统接入 Qwen3.8-Omni-Flash-Realtime（P0）
- 模型页面：https://www.qianwenai.com/models/qwen3.8-omni-flash-realtime；
- 以流式方式接入：持续输入画面（+可选语音/文本），持续输出指令文本流；
- 快系统**不直接产生键鼠**，只产生给 GameScaling 的指令（目标/朝向/动作意图/时长），由动作层落成按键；
- 延迟预算：指令到动作层生效 ≤ 300ms（不含动作层自身推理时间）；接口降级时保持上一条指令或进入安全空闲。

### FR-4 长期记忆 = Graphiti，每游戏一个 group_id（P0）
- 启动参数 `--game <game_id>`；`group_id = game_id`，禁止跨游戏读写；
- 写入内容：键位表、游戏规则、地点/NPC、剧情要点、失败教训、有效策略；
- 读取时机：启动快系统前构造系统提示词；慢系统每次抉择前检索相关记忆；
- 记忆内容需带来源（人工/快系统学习/慢系统推断）与置信度；低置信度不可直接进提示词。

### FR-5 短期记忆 = LLM 自写便签（P0）
- 提供工具 `write_note(text)` / `read_notes()` / `clear_notes()`（或等价接口）；
- 字符上限（默认 4000，可配），超限拒绝并返回剩余容量提示；
- 便签按 profile 落盘，重启保留；
- 便签内容与长期记忆的边界：便签是“临时工作台”，长期事实必须经 Graphiti 审核写入。

### FR-6 系统提示词构造（P0）
- GameScaling 系统提示词必须包含三段：
  1. `{GAME_NAME}`：严格等于 profile 的规范名（见 FR-8）；
  2. `{KEY_BINDINGS}`：来自记忆键位表，每行 `- key -> effect`；未学到的键位**不得编造**；
  3. `{GAME_RULES}`：来自记忆规则库，含画面元素、目标、计分/胜负条件；
- 未学到的部分显式写“未知，需在运行中学习”，并列入待学清单；
- 提示词由慢系统构造并注入快系统（启动时），运行中不随意改写。

### FR-7 学习回传与重启闭环（P0）
- 快系统发现新键位/新规则/新界面元素 → 结构化事件（JSON）：{type: keymap|rule|ui, entry, evidence, confidence}；
- 慢系统校验（交叉验证/去重/冲突检测）后写入 Graphiti；
- 更新后**重启快系统**，用新记忆重建系统提示词；
- 重启需可观测：记录版本号、变更 diff、生效时间。

### FR-8 多游戏 profile 与游戏名称对齐（P0）
- 每个游戏一个目录：`profiles/<game_id>/`，含 `config.json`、`keymap.json`、`rules.md`、`scratchpad.txt`、`logs/`；
- `game_id` 与显示名/别名映射表维护在 `profiles/registry.json`；
- **游戏名称严格对齐**：写入 Graphiti 的 group_id、提示词 `{GAME_NAME}`、日志、指标全部使用同一个规范名（大小写、空格、中英文一律以 registry 为准，别名只用于输入匹配）；
- 禁止跨 profile 读取记忆；切换游戏必须显式指定 `--game`。

### FR-9 意图总线（P1）
- 慢系统输出 `Intent{type, target, urgency, ttl, why, confidence}`，快系统消费；
- 默认 `NO_OP`（克制）：只有置信度/紧迫度超阈值才覆盖当前意图；
- 意图带 TTL 与抢占规则，动作层每帧读取当前意图。

## 4. 非功能需求

| 类别 | 要求 |
| --- | --- |
| 延迟 | 快系统指令生效 ≤300ms；慢系统 1–5s 可接受 |
| 可靠性 | 快系统断线自动重连；慢系统不可用时快系统保持末条意图或安全空闲 |
| 隔离 | profile 与 group_id 双重隔离，任何跨游戏写入视为缺陷 |
| 可观测 | 事件 JSONL（tick/intent/action/learning/memory write）；指标：指令时延、动作合法率、学习条目数、记忆命中率 |
| 安全 | 单写者输入租约；危险动作策略；便签与记忆注入防护（外部文本不当指令执行） |
| 兼容 | Windows；复用 BetterGI 输入通道与现有桥接（不重写宿主协议） |

## 5. 数据契约（草案）

```
profile = {
  game_id, display_name, aliases[], group_id,
  keymap: [{key, effect, confidence, source}],
  rules: [{section, text, confidence, source}],
  scratchpad_limit: 4000,
  omni: {endpoint, model, voice?},
  motor: {provider: gamescaling|other, endpoint, system_prompt_version}
}
learning_event = {type, entry, evidence, confidence, source, ts}
intent = {type, target, urgency, ttl, why, confidence}
```

## 6. 待确认问题（阻塞项）

1. **Qwen3.8-Omni-Flash-Realtime 的接入方式**：WebSocket？是否支持图像帧输入？输出是否只有语音/文本？若只出语音/文本，它到 GameScaling 的指令通道如何定义？（需要实测接口文档）
2. **Graphiti 部署形态**：本地 Docker 还是远端服务？版本与自定义 schema 能力（episode/rule/keymap 类型）。
3. **游戏名规范表**：原神等游戏的规范英文名/中文名口径由谁定（建议 official English name 为 group_id）。
4. **GameScaling 与 Omni 的分工边界**：Omni 只做“理解+指令”，GameScaling 做“帧级动作”，确认无第二决策者。

## 7. 验收标准

- pad 完全移除：仓库 grep 0 命中，tests 通过；
- 慢系统：全部调用走 DeepSeek 官方 `deepseek-flash`，日志可见；
- 快系统：Qwen Omni 流式接入，能持续输出指令并驱动 GameScaling 产生合法键位；
- Graphiti：读写闭环，两个游戏并行时数据不串；
- 便签：写入超限被拒绝并有提示；重启后保留；
- 提示词：由记忆构造，未学字段标未知；学到的键位/规则回传后重启生效（有 diff 与版本号）；
- 多 profile：`--game A` 与 `--game B` 可分别运行，互不干扰。

## 8. 里程碑

| 里程碑 | 内容 | 验收 |
| --- | --- | --- |
| M1 | 删除 pad；Lithos 切 DeepSeek 官方 flash | grep 0、单测通过、日志模型名正确 |
| M2 | profile 框架 + Graphiti 接入 + 便签 | 两个游戏读写隔离、便签上限生效 |
| M3 | Qwen Omni 快系统接入 + 意图总线 | 快系统持续输出指令、动作层合法动作 |
| M4 | 提示词构造 + 学习回传 + 重启闭环 | 键位/规则从记忆中进入提示词并生效 |
| M5 | 实机联调与验收 | 多游戏切换、指标达标 |

## 9. 风险

- Omni 实时接口若不支持图像输入，需改为“Omni 只做语音/对话、视觉仍由 GameScaling 消费”，架构需相应调整；
- Graphiti 依赖图数据库，部署与运维成本需评估；
- 双系统耦合若意图更新过频，会产生动作抖动，需在意图总线做最小驻留时间（dwell time）。

---

附：上一版（已作废）把我凭想象编的“GoalStack/技能库 tick 循环”称为 ProAct，本轮已更正为按 ProAct 论文（2602.14048）的**双系统 + 克制**思路，结合用户具体要求落地。