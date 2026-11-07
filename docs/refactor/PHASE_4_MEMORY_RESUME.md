# Phase 4：Episodic / Consolidation / Resume

状态：已实现（假模型、假时钟的单元/接线测试）。  
日期：2026-10-28

## 目标

- 每一段值得记住的经历落成结构化 episode；
- 按节奏 consolidation，而不是把整场 transcript 塞进上下文；
- 进程重启后，下一场首轮能自然延续上一场；
- 所有记忆都带可见性，world_only 永不进入玩家上下文。

## 实现

### Episodic memory

`integrations/player/player_episodic_memory.py`

- 必填字段：`what_happened / what_i_knew_before / what_i_thought / what_i_felt /
  characters / unresolved_questions / importance / source_session / visibility`。
- `event_id` 去重；原子写盘；损坏文件保留备份并拒绝静默重建。
- 默认只读取 `player_knowledge` 可见性。

### Consolidation

`integrations/player/player_memory_consolidator.py`

- 每轮提案先归一化，再由重要性阈值决定是否生成 episode（默认阈值 0.5）。
- 同一轮里可同时更新：
  - 结构化记忆（working / episodic / semantic 候选）；
  - 信念、关系、情绪、叙事线索（Phase 5）；
  - 会话状态（位置、目标、最近想法）。
- 模型给出的 `learned_facts` 只是候选：只有通过证据门（Phase 2）的
  OCR 原文 + 证据哈希 + 时间/会话校验，才会写入语义记忆。
- consolidation 结果写入 shadow 记录，失败不改变已有状态。

### Recap / Resume

`integrations/player/player_recap.py`

- `build_recap()`：从 state、working、最近若干 episode、已知事实生成
  「上一场回顾」，是确定性的（不调用模型）。
- `RecapStore`：schema 1、原子写入、损坏备份、未来版本拒绝加载。
- 默认路径 `state/session_recap.json`，下一场启动时注入 ContextBuilder 的
  `recap` 必选块。

`integrations/player/player_state.py` / `player_working_memory.py`

- `resume_state` 保存位置、最近意图、内心想法、未完成事项等；
- working memory 按 session 重置，但保留跨场可用的摘要；
- `player_agent.close()` 统一写回 recap、working、state。

### 上下文注入

`integrations/player/player_context_builder.py`

- v2 上下文按块构建：identity / perception / working / intention / resume /
  recap / episodic / semantic / beliefs / relationships / emotion / threads /
  dialogue / commentary / output。
- 必选块不截断；超总预算时整轮放弃，不生成「缺记忆的 prompt」。
- `CONTEXT_BUILDER=legacy` 可回滚到旧上下文。

## 验证

- `test_player_memory_stores.py`：episode schema、去重、损坏恢复。
- `test_player_context_builder.py`：块预算、必选块、resume/recap 注入。
- `test_player_agent.py`：consolidation、close 写回、跨进程 resume。
- `test_continuity.py::test_a_*`：跨 session 的 episode → recap → 下一场 prompt。
- `test_run_episode.py`：会话结束后的落盘链路。

```bash
python -m pytest integrations/player/tests -q
```

## 已知边界

- episode 目前不是「每 30–120 秒强制生成」，而是由提案重要性触发；
  低频思考时可能整场没有 episode，只有 recap。
- recap 是确定性摘要，不做模型润色；信息密度受最近 episode 数限制。
- resume 与实际游戏位置不一致时，依赖下一轮观察纠正，没有主动校验任务状态。

## 回滚

- 停止注入 recap/resume：`CONTEXT_BUILDER=legacy`；
- 保留旧 `memory.json` 不动，新状态目录可整体删除重建。
