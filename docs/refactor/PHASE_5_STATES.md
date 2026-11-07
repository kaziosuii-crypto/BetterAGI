# Phase 5：Belief / Relationship / Emotion / Thread

状态：已实现（假数据单元测试）。  
日期：2026-10-28

## 目标

让「同一个玩家」拥有可累积、可修正、不会一步登天的内部状态：

- 信念可以被证据推翻，并保留推翻历史；
- 关系只能小步变化；
- 情绪连续、会衰减，但重大事件余韵更长；
- 剧情线索可以休眠、重新激活、解决或放弃。

## 实现

### 信念

`integrations/player/player_beliefs.py`

- 状态：`known_fact / belief / hypothesis / suspicion / unknown / disproven`。
- 每条记录包含命题、置信度、证据、反证、来源、首次/最近时间。
- `disproven` 不删除记录：保留原始判断、推翻证据与历史，供上下文写成
  「我曾以为……后来发现……」。
- `ground_truth` 一类的世界真相状态在归一化时直接拒绝。

### 关系

`integrations/player/player_relationships.py`

- 六个维度：`familiarity / trust / affection / suspicion / respect / concern`。
- 单次更新每个维度最多变化 `0.15`，避免一次剧情让关系暴涨。
- 记录 memorable events；`summary()` 只输出自然语言，例如
  「对安柏：信任、亲近；她帮过我们」。
- 裸数值不进入 prompt。

### 情绪

`integrations/player/player_emotion.py`

- 九个维度：`joy / sadness / fear / anger / curiosity / confusion /
  excitement / nostalgia / tension`。
- 20 分钟半衰期；每次事件强化前先按时间衰减。
- 单次每个维度最多变化 `0.35`，防止瞬间极端化。
- `summary()` 只在超过阈值时描述，例如「有些难过，还没缓过来」。

### 叙事线索

`integrations/player/player_threads.py`

- 状态：`open / dormant / resolved / abandoned`。
- 线索带标题、摘要、相关角色、最近推进时间。
- `activate()` 重新打开休眠线索；检索时按线索匹配加权。

### 检索

`integrations/player/player_retrieval.py`

默认权重：语义 `0.30`、重要性 `0.20`、情绪 `0.15`、角色 `0.10`、
线索 `0.15`、新近度 `0.10`。

- 记忆与事实分别打分后合并排序；
- `world_only / ground_truth / world_database` 在入口直接过滤；
- 分数只用于选择，不写进 prompt。

## 验证

- `test_player_states.py`：状态机、delta 上限、衰减、自然语言摘要、损坏恢复。
- `test_continuity.py`：
  - B：错误猜测被推翻且保留历史；
  - C：休眠线索重新激活；
  - D：难过余韵压低即时兴奋；
  - E：world_only 永不进入 prompt。
- `test_player_memory_stores.py` / `test_player_agent.py`：consolidation 写入四类状态。

```bash
python -m pytest integrations/player/tests/test_player_states.py integrations/player/tests/test_continuity.py -q
```

## 已知边界

- 状态更新仍由模型提案驱动；若模型长期不输出 beliefs/relationships/emotion，
  状态会保持平稳而不是自动推断。
- 情绪维度目前没有「恢复事件」的专门语义，只靠衰减与反方向事件。
- 关系/线索的检索加权是小规模启发式，未做真实长局调参。

## 回滚

关闭四类状态注入即可回到 Phase 4 的 working/episode；状态文件保留，
重新开启后仍可继续使用。
