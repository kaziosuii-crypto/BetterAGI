# Persistent Player 重构审计（Phase 0）

审计日期：2026-10-27  
状态：Phase 0–9 的代码骨架与模块级验证已完成；真实游戏/AutoDL/TTS 端到端与盲测尚未执行。Shadow 模式默认不发送任何动作。

## 核心结论

当前系统是「DeepSeek 导演/解说 + Lumine 独立操作 + BetterGI 执行 + 录像机」，
不是「一个持续存在的玩家」。

- 导演只发 guidance/commentary，不产生动作；
- Lumine 只看当前帧+guidance，自己决定 200ms 动作，没有记忆/剧情目标；
- 导演看不到 Lumine 的动作块/thought，只看到按键集合；
- 没有 player_profile、belief、relationship、emotion、narrative thread、
  episodic memory、resume_state；
- session.jsonl 是录像事件流，不是可恢复的认知状态；
- lore 与世界真相没有和玩家已知知识分离，存在剧透。

## 文档

| 文件 | 内容 |
|---|---|
| [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md) | 现状、数据流、13 个审计问题、关键实现缺陷、文档不一致 |
| [GAP_ANALYSIS.md](GAP_ANALYSIS.md) | 逐项差距：现状→为什么→目标→文件→风险→迁移 |
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | 唯一玩家、模块边界、接口、Motor 硬保证 |
| [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md) | 七层记忆、schema、检索评分、consolidation、resume、防剧透 |
| [AGENT_LOOP.md](AGENT_LOOP.md) | 四层循环、意图生命周期、speech policy、dialogue lock、记录钩子 |
| [MIGRATION_PLAN.md](MIGRATION_PLAN.md) | Phase 0.5 修断点 → Phase 1–9，开关/回滚/风险 |
| [TEST_PLAN.md](TEST_PLAN.md) | 单元/集成/连续性 A–E/实况盲测，含当前断点回归 R1–R18 |
| [PHASE_0_5_FIXES.md](PHASE_0_5_FIXES.md) | Phase 0.5 已修复的 P0 断点、测试与剩余项 |
| [PHASE_1_PLAYER_SKELETON.md](PHASE_1_PLAYER_SKELETON.md) | Phase 1 已实现的 shadow 玩家骨架、schema、安全边界与验证 |
| [PHASE_2_CONTEXT_MEMORY.md](PHASE_2_CONTEXT_MEMORY.md) | Phase 2 结构化记忆和上下文构造、验证与未完成边界 |
| [PHASE_3_INTENT_MOTOR.md](PHASE_3_INTENT_MOTOR.md) | Intent 协议、MotorController prompt、意图总线与状态机 |
| [PHASE_4_MEMORY_RESUME.md](PHASE_4_MEMORY_RESUME.md) | Episodic、consolidation、recap/resume 与跨场注入 |
| [PHASE_5_STATES.md](PHASE_5_STATES.md) | 信念、关系、情绪、叙事线索与检索权重 |
| [PHASE_6_DIALOGUE.md](PHASE_6_DIALOGUE.md) | OCR 对话感知、dialogue lock 与 hold 意图 |
| [PHASE_7_SPEECH.md](PHASE_7_SPEECH.md) | Speech Policy、同源解说与 prosody 字段 |
| [PHASE_8_AUDIO_PROSODY.md](PHASE_8_AUDIO_PROSODY.md) | 游戏原音录制、ducking、音量/语速/停顿映射 |
| [PHASE_9_CONTINUITY.md](PHASE_9_CONTINUITY.md) | 连续性 A–E、回归结果与实况验收清单 |

> 2026-10-27：Phase 1 骨架已落地（`BetterGI/integrations/player/`），
> 只读 shadow，不接管动作；旧导演/录制链路保持不变。
>
> 2026-10-28：Phase 3–9 代码落地——intent 总线、四类状态、对话锁、
> speech policy、游戏原音录制/ducking、连续性 A–E。默认仍是 shadow；
> 真实游戏与模型端到端尚未验证。

## 最高验收问题

> 如果不告诉观看者系统架构，只给他连续十期实况，
> 他会不会觉得这是同一个 AI 玩家一直在玩这个游戏？

测试数量（189 player / 26 session / 178 lumine）只是回归门槛，不是完成标准。
