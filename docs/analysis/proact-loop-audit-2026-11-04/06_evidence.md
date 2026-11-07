# 运行证据与实测数字（2026-11-04 17:05–17:15 采样）

> 采样方式：只读取证。数据源 `E:\BetterAGI\_diag\agent-runs\*.jsonl`（真实会话）、`_diag\agent-runs-test`（合成，已排除）、`agent/tests`（203 用例）、进程与端口探测。
> 为什么要有这份文档：本项目的缺陷不是「理论上可能有」，而是**已经在真实日志里发生了**。下面每条数字都能复算。

---

## 1. 真实会话实录（`_diag/agent-runs`，49 个真实 jsonl）

每行 JSON：`{ts, kind: start|assistant|tool|end}`；assistant 带 `turn/tier/model/latencyMs/text/toolCalls[]`，tool 带 `turn/tool/ok/text`。

最近 5 次真实会话：

| 会话（结束时刻） | 轮 | 工具调用 | 墙钟 | avgLat / max | 工具分布 | 失败 |
| --- | --- | --- | --- | --- | --- | --- |
| run-…-141528 14:16:04 | 6 | 6 | 29.9s | 1305 / 1577ms | look×2 finish_intent×3 press×1 | **finish_intent 3/3 全失败** |
| run-…-141157 14:12:26 | 6 | 2 | 21.1s | 1127 / 1281 | look×2 | 0 |
| run-…-135714 13:57:46 | 9 | 5 | 30.9s | 981 / 1592 | screen×4 look×1 | 0 |
| run-…-135233 13:53:08 | 7 | 4 | 34.8s | 3453 / 7581 | screen/look/act | screen 1 失败 |
| run-…-104701 11:09:03 | **491** | **204** | **1322.2s（22 分钟）** | p50 1112 / p90 1557 / **p99 2252** / max 2772 | 见下 | **164** |

**正常一次 TUI 会话只跑 6–9 步、半分钟就停**（去掉 104701 后，最近 4 次平均 7 轮 / 29.2s / avgLat 1.7s）。这本身就是问题：真人不会玩 30 秒就退出。

### 1.1 最长真实会话 run-104701（22 分钟，唯一「跑久了」的样本）

- tier 全 `fast`、model 全 `deepseek-flash`，491 轮，latency 累计 576,920ms；
- 工具：`look` ok 11 / **fail 152**、`act` ok 25 / fail 8、`learn_key` ok 4、`motor` ok 0 / **fail 4**；
- **同一签名 `look|false|AdapterError: window not found for profile undertale` 重复 152 次**；`look` 连续失败 106 次（turn14 → turn327）；
- 成条纹重复：`look`×3（t3）、`act`×3（t6）、`learn_key`×4（t10）、`act`×12（t467）；
- **290/491 轮（59%）`toolCalls` 为空** —— 纯发话不动手；150 轮 text 为空；
- **重复文本**：75×「窗口一直没回来，UNDERTALE起不来，我这边只能看、开不了游戏。先停在这，等」（turns 101,104,107,110…）；72×「还是没窗口…」；
- 首次 window-not-found 在 turn13 / +67s，其后 157 次贯穿到 turn491 / +1322s → **后 22 分钟里 97% 的时间在空转**。

> 这条会话完美演示了 P0-4/P1-4 的后果：**没有失败归因、没有放弃机制、没有「换个办法」的能力**，模型会以 1.1s/次的节奏把同一句道歉说 75 遍。它也说明「LLM 不笨」——它在第 10 步就自己补出了缺失的键位映射（见下）。

### 1.2 两个关键事件序列

**序列 A｜开局成功路径（turn 1–12）——模型其实很会玩**

1. `look` → 「我先看看现在游戏停在哪个画面」→ 成功，641×480；
2. `act` 命名界面按确认 → `pressed confirm->z`；
3. `look` 看开场动画 → `act` confirm → 到标题 UNDERTALE；
4. 想打名字 LUMI → **失败**：`AdapterError: key 'l' is not in profile undertale keymap (known: back, cancel, confirm, down, left, menu, quit, right, up)`；
5. **`learn_key` ×4 自己学会 l/u/m/i** → `{"action":"added","key":"l","effect":"physical:l","confidence":0.9}`；
6. 第 10 步就补出了缺失键位 —— **这是全场唯一一次真正的「学习成功」**；turn13 起窗口消失。

**序列 B｜卡死路径（turn 479–491）**

反复试 X（删字）/Z（确认）/回车/menu 全无效，名字被越打越长（`Ljjjj`），最后窗口消失仍在 motor/look 间循环；连续 5 轮纯发话「窗口一直没回来…先停在这」。

**真实错误原文**（可直接 grep）：

- `HarnessError: SetForegroundWindow failed: winerr=5`
- `HarnessError: unknown key: 'backspace'`
- `AdapterError: key 'backspace' is not in profile undertale keymap`
- `AdapterError: window not found for profile undertale (match={'title':'UNDERTALE','exe':'UNDERTALE.exe','class':'YYGameMakerYY'}, running=False)`
- `MotorError_: gamescaling HTTP 502:`

---

## 2. 测试：203 全绿，但对真实故障零覆盖

- `E:\BetterAGI\agent` 与仓库根**都没有** `pyproject.toml` / `uv.lock` / `requirements.txt` / `pytest.ini` / `conftest.py`，**没有 .venv**；PATH 上 python 是 3.15.0 且 **pytest 未安装**（`ModuleNotFoundError`）。
- 唯一可用解释器：`E:\BetterAGI\Amaidesu\.venv\Scripts\python.exe`（py3.12.14 / pytest 9.0.2）。
- 实测命令（cwd=E:\BetterAGI\agent，`PYTHONDONTWRITEBYTECODE=1`）：
  `& 'E:\BetterAGI\Amaidesu\.venv\Scripts\python.exe' -m pytest tests -q -p no:cacheprovider`
  → **203 passed in 4.06s, 0 failed, exit 0**（collect-only 也 203，无 import error）。
- 无需联网/密钥：tests 里 api_key 全为假值（`sk-test`/`k`），endpoint 指向 `http://127.0.0.1:9`。
- 历史痕迹：`agent/.pytest_cache/v/cache/lastfailed`（15:20:46 写入）曾有 3 条红 —— `test_play_steps_skips_forbidden_key`、`test_direct_channel_gets_keyboard_persona`、`test_operator_traces_each_step_on_the_fast_lane`，现已全绿。

> **这是本项目最危险的一条**：203 个测试全绿，同时真实日志里有 164 次工具失败、59% 轮次无动作、同一错误重复 152 次。**测试测的是模块契约，不是「一个玩家能不能动」**。缺的是端到端冒烟测试（起游戏 → 跑 60 秒 → 断言「有进展」）。

---

## 3. 进程与端口：两条腿今天都没起来

**活着**：`gs_tunnel.py` ×2（PID 54252+21880，`18041 8041`，15:42 起；PID 65348+50536，14:35 起）、`node 75828` billion-context:18787、`node 71776` obsfusion:8787、`node 41228` relay.test.mjs、GPT-SoVITS python 2436/41284（9880/9881）、browser-bridge 19234、relay 1914/1915、DSH 9740–9742。

**监听实测**：只剩 **18040、18041**（gs_tunnel）。HTTP 探活 `18040/18041 /health` → **连接被重置**（TCP 在听、应用层不通）—— 这正是日志里 `gamescaling HTTP 502` 的实况。

**缺失**：8791 TUI 调试口、18311 桥接、18010 云端隧道/player ws、**30648 Live Hub**、**1933 OpenViking 记忆服务** 全部未监听。
（注：最近一次 TUI 实际用 **8796** 而非默认 8791 —— `_diag/tui.log` 首行「调试接口 http://127.0.0.1:8796」；`agent/tui.py:1066` argparse 默认值仍是 8791。）

**代码常量对齐**：`agent/agent.py:43` `DEFAULT_BRIDGE="http://127.0.0.1:18311"`；`agent/motor.py:33` `DEFAULT_ENDPOINT=…"http://127.0.0.1:18041"`；`agent/motor.py:100` 注释「云机 2B 服务在 8041，经 SSH 隧道转到本机 18041」；`agent/agent.py:1189` hint「手(gamescaling)没接上：先把 18040 的策略服务/隧道拉起来再操作」；`_diag/t_ab_models.py:17` 直接把 18040 标为 `9B(dead)`。

> 结论：需求文档里写的「两条腿」（Live Hub 30648 + OpenViking 1933）**今天两条都没起**，唯一在跑的是 gamescaling 隧道，且应用层 502。

---

## 4. TUI 到底跑的是哪条循环（`agent/tui.py` 1128 行）

- 入口 `main()` `tui.py:1054`；`--turns` 默认 0=不限（:1064）、`--port` 默认 8791（:1066）、`--log-root E:/BetterAGI/_diag/agent-runs`（:1077）、`--log-file _diag/tui.log`（:1078）；:1100 先查 `lithos.LithosConfig.from_env().available()`，缺 key 直接 `return 2`。
- **主循环 = `for turn in itertools.count(1)`（`tui.py:833`），单线程同步、无轮次上限**；:830-831 注释「主播型 agent 不会"玩完"：永远播下去…停的条件只有三个：/quit、外部停止、连续模型失败」；实际停止点 :834 `stop_event`、:840 `/quit`、:928 `is_reasoning_400` break、:935 `llm_failures >= MAX_CONSECUTIVE_LLM_FAILURES`。
- 每轮链路（就是 JSONL 那行的来源）：`interjections.drain()`:839 → `history.trim()`:865 → `probe_legs(tools)`（仅 uses_lumine）:871 → `recall_for(memory,…)`:887 → `history.messages(now_line)`:904 → **`client.stream_complete(messages, urgency, tools=tool_specs, on_delta=…)`:917** → `history.add(Entry(role="assistant"…))`:949 → **`session.append("assistant", turn, tier, model, latencyMs, text, toolCalls)`:958** → 无 toolCalls 就 `continue`:969-972 → `tools.dispatch(name, arguments)`:988 → `session.append("tool", turn, tool, ok, text)`:1000 → 记忆回写 `episode_from_tool`:1003 → `history.add(Entry(role="tool"…))`:1018 → 图片 `pending_images`:1027-1031。
- **慢系统＝这条主循环**，跑在主线程，**同步阻塞在 stream_complete**（一次 20s 的模型调用期间界面全靠 daemon 线程刷）。
- **快系统不在 TUI 常驻**：`tui.py:766-768` 注释「快系统不在这里常驻：它是"手+嘴"，只在 lumine 操作器里启动」；:900-902 再声明「这里不再注入逐帧转述」。
- 快系统真实接线：`tools.dispatch` → `agent/agent.py:873` `lumine()` → :887 `self._operator()` → `agent/agent.py:1133` `_operator`：:1149 `FastExecutor`、:1153 `build_motor`、:1170 `motor=build_motor()`、**:1176 `motor.health(timeout=3)`（失败即 :1186 返回带 hint 的 ToolResult —— 这就是 JSONL 里 motor fail 的来源）**、:1192 `narrator, narrator_why = self._fast_stream()`、:1211 `FastExecutor(ad, motor=…, narrator=…, step_ms=33)`、:1221 `executor.run(…)`。
- `FastExecutor.run`（`agent/executor.py:200`）：:207 `stop=threading.Event()`；**:220 `mind = threading.Thread(target=self._narrate_loop, name="fast-narrator", daemon=True)`**；主循环 :227 `while not done.is_set()` 每圈 `self._frame_url()` → `self.motor.decide(…)`（手），按 `budget_s`/`max_decisions` 退出。
- `FastStream`（`agent/fast_stream.py:49`）自建 asyncio：:74 `asyncio.new_event_loop()`、:82 `threading.Thread(target=run, name="fast-stream-loop", daemon=True)`、:89 `asyncio.run_coroutine_threadsafe`。
- 其他 daemon 线程：Console 状态 `name="status"`（`tui.py:400`）、插话键盘 `name="keys"`（:509）、调试 HTTP `ThreadingHTTPServer` + `name="api"`（:41/:626/:635/:645），token 落 `_diag/tui-endpoint.json`（:795-800）。
- 档位：`lithos.choose_tier(urgency, kind=)`（`agent/lithos.py:205`），模型由 spec + `LITHOS_MODEL_<TIER>` 覆盖（`lithos.py:169-173`）。
- 佐证运行态：`_diag/tui.log` 共 1158 行、**只有 4 次启动横幅**（10:47:09 / 13:57:15 / 14:12:04 / 14:15:34），末行 14:16:04 `press ok`，之后无输出 —— **TUI 最后一次只活了 30 秒**；日志里 168 行含「失败/Error/Traceback」。

---

## 5. 文档 vs 实际：两份 refactor 文档描述的是另一个系统

`Docs/refactor/CURRENT_ARCHITECTURE.md`（701 行 / 34.6KB / 2026-10-27）、`Docs/refactor/GAP_ANALYSIS.md`（78 行 / G1–G48 / 2026-10-27 22:26）。

**最大差异**：文档描述的世界，在**今天在跑的代码里一个字都没有**——
`CURRENT_ARCHITECTURE.md` 全文 grep `tui.py|lithos|executor.py|fast_stream|motor.py|gamescaling` **只命中 1 处**（:111 提到 deepseek-flash 是「导演」）；`GAP_ANALYSIS.md` 命中 **0 处**。
而今天唯一在跑的链路恰恰是 `agent/tui.py` + lithos(deepseek-flash) + gamescaling motor；文档写的是 BetterGI + Lumine 7B 那套**旧双主体**。

| 文档声称 | 实测 | 证据 |
| --- | --- | --- |
| §0「THERE ARE TWO PLAYERS / Lumine 拥有叙事级自主决策」 | **已失效**：motor prompt 已改成「你不选目标/任务/路线/对话」 | `lumine_protocol.py:114` `MOTOR_SYSTEM_PROMPT = """You are the MotorController for a persistent player agent. You do NOT choose goals, quests, routes, dialogue options, or what to explore…` |
| §1 引 `lumine_protocol.py:114` 为「…expert Genshin Impact player…Decide the best action」 | 行号还在，内容已被换掉；旧文案挪到 :156 `INSTRUCT_SYSTEM_PROMPT`，默认路径不用 | `lumine/tests/test_autodl_server.py:289` `assertNotIn("expert Genshin Impact player", messages[0]["content"])` |
| G17 P0「动作协议只有 chunks/absolute/thought，无 intent/owner/goal/reason」 | **已实现**：解析校验 `intentId`/`actionGroupId`/`motorGeneration`，并把 Intent ID 注入 prompt | `lumine_protocol.py:98-111`、:193 `"Intent ID: {0}".format(...)` |
| G44 P0「无 dialogue/menu 门控」 | **已实现**，mtime 全晚于文档审计日 | `Live/HostDialogueExitPolicy.cs`(11/2)、`HostDialogueSafetyPolicy.cs`、`HostPlayerDialoguePolicy.cs`(10/29 21:10)、`LiveDialogueModels.cs`、`LiveDialogueCandidateTracker.cs` |
| G16 P0「没有 dialogue perception」 | 有对话模型/候选追踪 | `Live/LiveDialogueModels.cs`(10/29 13:13)、`LiveDialogueCandidateTracker.cs`(10/28 18:21) |
| G45 P1「temperature 0.7 无 seed」 | **仍成立** | `autodl_server.py:112` `temperature: float = 0.7`、:1185 `LUMINE_TEMPERATURE` 默认 0.7、:87 `TRAINING_TEMPERATURE = 0.0`（仅训练） |
| §2「WebSocket /ws/v1/agent/live，5Hz 帧」 | 路径真、**两端都没起**；且导演 tick 是 **4 秒**不是 5Hz | `live_director.py:57` `DEFAULT_WS_URL="ws://127.0.0.1:30648/ws/v1/agent/live"`、:59 `DEFAULT_INTERVAL=4.0`；C# `ExternalAccessHost.cs:395` `app.Map("/ws/v1/agent/live", …)`；实测 30648 未监听 |
| §1 组件表路径（record/render/LiveSessionService 都在 integrations 下） | 实际在 `integrations\session\` 与 `Service\ExternalAccess\Agent\Live\` | `record_session.py`/session 75843B、`render_session.py`/session 62404B、`LiveSessionService.cs` 86729B(11/3 17:15)、`LiveWebSocketHandler.cs` 39656B(10/29) |
| §1 记忆桥 → OpenViking 1933 | 服务未起，记忆链路事实上断开（`MemoryBridge` 只在启动连一次，见 G35） | `memory_bridge.py:44` `url="http://127.0.0.1:1933"`；实测 1933 无监听 |
| 仍成立的引用 | `live_director.py:489` prompt 仍是「你是《原神》实际 VTuber「{name}」，正和观众一起观看一位 AI 玩家操作游戏」；:1143 `_handle_message`、:1454 `await self._send(payload)` 行号仍对 | 文件为 **GBK** 编码，需按 GBK 读 |

**数字校正**：按真实物理行（含空行）重数：`live_director.py` **1917** 行、`lumine_protocol.py` **543**、`memory_bridge.py` **231**（先前用 `Measure-Object -Line` 不计空行得出 1697/453/204，勿引用后者）。`GAP_ANALYSIS.md` 实为 **G1–G48**（非 G43），且 G38 自己承认「文档/协议/测试数与代码不一致（dsh vs director、151 vs 162）」。

> 这条对本分析的意义：**不能拿 refactor 文档当「现状」来评估**，否则会把已修好的问题（对话门控、动作协议）当缺失，把已不存在的系统（Lumine 双主体）当主干。本报告所有「现状」结论都来自代码与运行证据，不来自那两份文档。

---

## 6. 一句话给决策者

**能跑的测试 203 全绿，但真实资产（22 分钟那次会话）是 59% 轮次空转、同一句「窗口没回来」重复 75 遍、152 次同一个 window-not-found；环境上 Live Hub(30648)、OpenViking(1933)、桥(18311)、隧道(18010)、gamescaling 应用层全挂，只剩 18040/18041 在听且 /health 被重置。两份 refactor 文档比代码晚 7 天，且它们描述的 agent 层（tui/lithos/executor/gamescaling）在全文里 grep 不到——「文档声称 vs 实际」不是局部偏差，是描述了两个不同的系统。**
