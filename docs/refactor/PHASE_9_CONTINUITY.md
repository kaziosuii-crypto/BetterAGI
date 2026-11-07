# Phase 9：连续性与实况验收

状态：A–E 连续性回归已实现并通过；真实 30–60 分钟实况与盲测未执行。  
日期：2026-10-28

## 目标

验证「同一个玩家」而不是「同一个程序」：

- 跨 3 个 session 的人格、记忆、信念一致；
- 不出现数据库口吻、全知视角、突然失忆；
- 无剧透；
- 盲测多数人认为是同一个玩家。

## 已实现的回归（A–E）

`integrations/player/tests/test_continuity.py`

| 编号 | 场景 | 断言 |
|---|---|---|
| A | 跨 session 记忆 | episode → recap → 下一场 prompt 能引用「安柏帮我们解了围」 |
| B | 错误猜测被推翻 | `disproven` 保留原有 suspicion 历史，摘要承认「猜错了」 |
| C | 休眠线索重激活 | `dormant → activate → open`，重开进程后仍存在 |
| D | 情绪余韵 | 难过 0.95 后 30 秒，兴奋解说被压回正常语速并标记 `mood_damped` |
| E | 无剧透 | `world_only` 事实不进入检索结果与 ContextBuilder prompt |

这些测试使用假时钟、假存储路径和真实 JSON 落盘，重开 store 再读，
验证的是状态持久化而不是字段存在。

## 附加连续性测试

- `test_player_agent.py`：跨 agent 实例的 resume/recap、session 重置、
  对话锁 hold、意图发布。
- `test_player_recap.py`：recap 构建、损坏恢复、版本拒绝。
- `test_player_states.py`：四类状态的 delta 上限、衰减、历史保留。
- `test_player_knowledge.py`：OCR 证据门、world_only 拒绝。
- `test_player_observation_e2e.py`：观察 → 证据 → 上下文注入链路。

## 运行

```bash
python -m pytest integrations/player/tests -q
python -m pytest integrations/session/tests integrations/lumine/tests -q
```

当前结果（2026-10-28）：

- player：189 passed
- session：26 passed
- lumine：178 passed（另有 28 subtests）
- 合计：393 passed

## 实况验收清单（尚未执行）

1. 安装原神，启动 BetterGI，开启实时会话。
2. 录制 30–60 分钟，至少跨 3 个 session（中途重启进程）。
3. 检查：
   - 下一场首轮是否自然引用上一场；
   - 是否出现「根据记录」「数据库显示」等口吻；
   - 是否提前知道未看过的剧情；
   - 对话期间是否停止操作与解说；
   - 普通移动是否保持沉默；
   - 情绪低落时是否仍会因小事件瞬间兴奋。
4. 盲测：给不了解架构的观看者连续片段，询问是否同一玩家，
   并要求举例说明跨期记忆/情绪。

## 已知阻塞

- 本机尚未安装原神；AutoDL 模型服务已停止。
- 因此真实帧、真实 OCR、真实 Lumine 动作与真实 TTS 的端到端验收
  无法在本次完成；上述 393 项测试只证明模块与接线正确。
- 盲测需要人工观看，不能由自动化测试替代。

## 通过标准

- 所有 P0 单元/集成测试通过；
- Test A–E 通过；
- 无剧透测试通过；
- 真实 session 中无「失忆/全知/数据库口吻」；
- 盲测多数认为是同一个玩家。
