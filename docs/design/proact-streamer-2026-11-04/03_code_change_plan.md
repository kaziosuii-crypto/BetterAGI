# ProAct 双系统直播 · 代码地图与改造清单

> 分析对象：`E:/BetterAGI/agent/` 全部 26 个 .py（约 400KB）。只读分析，未修改任何源码。
> 目标架构：用户已拍板的 ProAct 式双系统（快=阿里云 Omni realtime + GameScaling 2B-S3 手模型；慢=DeepSeek flash；快慢互斥、request_handoff 交棒；Graphiti 五层记忆；便利贴；单调毫秒时间轴 + 10 分钟分片录播；弹幕 HTTP；手解析失败=程序错误熔断）。
> 记法：`文件:行号` 均为实测行号。**【复用】**=可直接用，**【改】**=需改动，**【新】**=仓库中没有、需要新建。

## 0. 先给结论（五条）

1. **快系统的积木是齐的，缺的只有"会话生命周期"。** Omni 的 WS 客户端、手模型 client、动作解析器都存在（`agent/omni.py:233`、`agent/motor.py:90`、`agent/gamescaling_action.py:219`），但 Omni 是一条 WS 永久复用（`agent/fast_stream.py:49`、`agent/fast_system.py:99`），没有 per-intent 会话、没有 response_id 归属校验、collect 里没有 recv 超时（`agent/omni.py:350`）。这就是目标架构第 1 条落地的全部工作量。
2. **慢系统有两套闭环、两套工具面。** `agent/agent.py:1667` 的 Lithos 非流式闭环与 `agent/tui.py:718` 的流式闭环并存，工具表也分裂（`agent/agent.py:89` vs `agent/agent.py:148`）。而目标要的 9 个工具（read_sticky/write_sticky/search_memory/write_memory/read_danmaku/get_last_episode/dispatch_task/set_hook/set_plan）在 `agent/agent.py:89` 和 `:148` 里**一个都没有**。
3. **手嘴并行已经写好了，但两者只共享 stop/done 两个 Event**（`agent/executor.py:147` 与 `:200`），没有结构化通道；`decide_ms` 只记录不校验。这是"全开环"的直接证据，也是目标第 3 条（判断执行是否该结束）最需要补的地方。
4. **便利贴/时间轴/录播/弹幕四件事在 agent/ 内几乎为零。** 便签只有存储层、没有调用方（`agent/profiles.py:236`、`:251`）；弹幕现在是被塞成 user 消息直接进 LLM（`agent/tui.py:852-855`）；ffmpeg / mp4 / VideoWriter 在 agent/ 下 grep 0 命中，OBS 与 ffmpeg 的先例都在别的目录（`Amaidesu/src/modules/studio/obs/obs_provider.py:9`、`BetterGI/integrations/vtuber/render_vtuber.py:10`）。
5. **三槽位提示词其实已经实现好了。** `agent/prompt_builder.py:365-370` 就在做 `{GAME_NAME}/{GAME_RULES}/{KEY_BINDINGS}` 替换；手模型侧只需要把现成的 `PromptBundle.text` 塞进 `gamescaling_request.build_request`，再补"history 跨调用保留 + 意图开始 policy.reset()"的调用纪律（`agent/motor.py:117` 的 reset 与 `:124` 的 record 已有，只是没人按意图边界调）。

---

## A. 模块地图

### A.1 快系统 · Omni realtime

**agent/omni.py（411 行，WS 车道，当前主线）**
- `OmniConfig`(omni.py:43)：api_key/workspace_id/model/modalities(default "text")/instructions/tools/tool_choice。
- `resolve_key`(omni.py:57)：env → `fast_system.resolve_dashscope_key`(`agent/fast_system.py:220`) → keys/dashscope.key。
- `resolve_url`(omni.py:72)：公共入口，**强制拼 `model=` 查询串**（omni.py:93-96；漏了实测报 1009 missing model）。
- `session_update`(omni.py:104)：一帧 session.update（instructions/modalities/tools 都从这里进）。
- `WsTransport`(omni.py:157)：connect/send/recv/close，JSON 帧 + 二进制帧。
- `OmniReply`(omni.py:196)：text/function_calls/latency_ms/raw_events/truncated + `first_call()`。
- `encode_jpeg_frame`(omni.py:213)：校验 FFD8FF 与 `MAX_IMAGE_BYTES`=256KB → **超限直接报错，不会降质重编**。
- `OmniClient`(omni.py:233)：`_audio_seeded` 与 `_inflight` 两个标志，**同一实例跨 ask 复用**。
- `ask`(omni.py:330)：顺序写死 = cancel_inflight → 重垫静音 PCM(640B=20ms) → send_frame → extra_frames → commit → create_response → collect。
- `collect`(omni.py:350，max_events=512)：循环 `recv_json()` 直到 response.done；错误分支先 `_drain_until_done(3.0)` 再抛（omni.py:391-399）。
- `_drain_until_done`(omni.py:293，budget 8.0) / `cancel_inflight`(omni.py:317，budget 8.0)：**预算用完只 return，不抛、不关连接**。
- **关键缺口**：`collect` 的循环里 `transport.recv_json()`（omni.py:359）**没有任何 wait_for 超时**；唯一的超时在 `fast_system._run` 的 `future.result(timeout=60)`(`agent/fast_system.py:133`)。

**agent/webrtc_fast.py（411 行，WebRTC 车道，可作低延迟备选）**
- 常量：DEFAULT_ENDPOINT(webrtc_fast.py:21)、DEFAULT_MODEL=`qwen3.8-omni-flash-realtime`(22)、SYSTEM_INSTRUCTIONS(24)。
- `_silent_audio_track`(webrtc_fast.py:31)：**audio track 是硬要求**，不给就 400 remote_sdp_failed。
- `LatestFrameVideoTrack`(webrtc_fast.py:63)：push_rgb(80)/has_frame(94)/recv(98) —— 只留最新帧的视频轨，天然适合"意图内持续看画面"。
- `WebRTCFastSystem`(webrtc_fast.py:121)：start(157)/_run_loop(175)/_connect(191，SDP POST `/api/v1/webrtc/realtime?model=…`，等固定名 `txt` DataChannel)/_handle_event(260)/_send_session_update(303，modalities=["text","audio"]、turn_detection=server_vad threshold .5 silence 800ms、max_tokens 1024)/push_frame(328)/ask(338，等 `_resp_done`，超时返回 {ok:False,error:"response timeout"})/_shutdown(389)/stop(399)。
- **全仓唯一自带超时的 Omni 实现就是这里**（webrtc_fast.py:338）；但没有 response.cancel、没有 per-intent 会话，且 server_vad(303) 是唯一轮次驱动（manual 不支持）→ 上一 response 未完就投下一帧必串台。

**agent/fast_system.py（266 行，Omni 薄封装，Non-stream）**
- `FAST_INSTRUCTIONS`(fast_system.py:26)/`INTENT_INSTRUCTIONS`(31，输出 `{"type","target","why"}` JSON)/`DESCRIBE_INSTRUCTIONS`(38)。
- `parse_intent`(fast_system.py:69)：正则抓 `{...}` + INTENT_TYPES 校验，confidence 硬编码 0.6、meta.source="fast"。
- `FastObserver`(fast_system.py:99)：常驻 loop 线程 + 复用一条 WS；`_ensure_loop`(116)/`_run`(133，`.result(timeout=60)`)/connect(137)/close(145)/observe(173，支持 list 多帧 extra)/suggest_intent(209)。
- `build_fast_system`(fast_system.py:255)：由 `BETTERAGI_FAST` 开关控制。

**agent/fast_stream.py（214 行，Omni 薄封装，Stream + 帧泵）**
- `FastStream`(fast_stream.py:49)：pump/interval/`_pending`/frames_sent/last_text/last_latency_ms。
- `_pump_loop`(fast_stream.py:101)：每 interval 秒 seed_audio + send_frame；`push`(116)：只留最新帧、非 JPEG 自动转码、校验体积。
- `ask`(fast_stream.py:130)：timeout=45.0 → cancel_inflight → seed_audio → 可选 send_frame → commit → create_response → collect；`describe_screen`(158)/`suggest_intent`(175)/`stop`(183)。

### A.2 快系统 · 手模型（GameScaling 2B-S3）

**agent/motor.py（248 行，唯一 HTTP 调用点）**
- 常量：`DEFAULT_TIMEOUT`=60.0(motor.py:30)、`DEFAULT_ENDPOINT`=env `BETTERAGI_GAMESCALING_URL` 或 `http://127.0.0.1:18041`(33)。
- `NATURAL_LABEL_KEYS`(motor.py:41)/`label_action`(52)：把自然语言动作标签映射成 token。
- `GamescalingClient`(motor.py:90)：fields endpoint/model="gamescaling"/api_key/**history**/history_len=5(env `BETTERAGI_GAMESCALING_HISTORY`)/last_*。
- `reset`(motor.py:117)：注释明写对应 README `3 的 `policy.reset()`——**但全仓没有调用点**。`set_history`(121)/`record`(124，只留最近 history_len 条)/`health`(130)。
- `decide`(motor.py:141)：build_request → POST `/v1/chat/completions` → 5xx 与连接错重试一次 → finalize → parse_action → sanitize_steps_report → **parsed=False 时 label_action 兜底成 6 组空动作**。
- `decision_action_text`(motor.py:231)/`build_motor`(motor.py:246)。

**agent/gamescaling_request.py（148 行，请求体拼装）**
- `USER_TEXT`="current game screen"(gamescaling_request.py:35)、`PREFILL_THINK`(44)/`PREFILL_NO_REASONING`(46)、`DEFAULT_API_KEY`="EMPTY"(50)。
- `build_messages`(gamescaling_request.py:95)：history 按 `[{frame_url, action}]` 旧→新展开 + 末尾一条 assistant prefill；**system 只有一个字符串槽（104-105）**，三槽位在这里会被拍平。
- `build_request`(gamescaling_request.py:118)：temperature=1.0、max_tokens=2048、stop=[ACTION_END]、continue_final_message=True、add_generation_prompt=False。
- `history_turn`(gamescaling_request.py:143)。

**agent/gamescaling_action.py（272 行，动作语义与解析）**
- 常量：CHUNK_MS=200.0、CHUNK_STEPS=6、STEP_MS=33.33、MAX_KEYS_PER_STEP=4、MOUSE_LIMIT=1000、SCROLL_LIMIT=5（gamescaling_action.py:36-41）→ 正好对上"200ms = 6×33ms 键位组 + 鼠标千分比 + 滚轮"。
- `split_reasoning`(54)/`Action`(70)：mouse/steps/**parsed/truncated**/body/dropped；`history_text`(81，**解析失败写 NOOP_ACTION_TEXT**(44))/`is_noop`(92)/`pressed`(96)/`to_text`(104)/`as_dict`(109)。
- `_build`(127)：钳制 x,y∈±1000、z∈±5；`_parse_strict`(175)/`_parse_lenient`(199)/`parse_action`(219，默认 lenient=False、keep_unknown=True)/`finalize`(246，补 action_start/action_end)/`noop_action`(268)。

**agent/gamescaling_vocab.py（207 行，74 token 词表）**
- `KEY_VOCAB`(gamescaling_vocab.py:30)、`to_token`(51)、`in_vocab`(69)、`tokens_for_profile`(73)、`sanitize_steps_report`(83)、`sanitize_steps`(105)、`TOKEN_TO_KEYMAP`(147)、`keymap_name`(165)、`is_mouse_button`(180)。
- 链路：token → `keymap_name`(165) → profile keymap → 物理 VK，落地在 `adapter._vk_for_step_key`(`agent/adapter.py:641`)。

**调用点**：`agent/agent.py:1091` _fast_stream（fps=2.0, pump=False）、`:1105` _fast_probe、`:1283` motor（单次 decide+record+play_steps）、`agent/executor.py:200` run（每轮 decide→play_steps）。

### A.3 意图

**agent/intent.py（132 行）**
- `INTENT_TYPES`(intent.py:21) = NO_OP/navigate/explore/combat/interact/wait。
- `Intent`(intent.py:25)：type/target/urgency/ttl=20.0/why/confidence/created_at/meta；expires_at(44)/expired(47)/as_dict(50)；`NO_OP`(54)。
- `IntentBus`(intent.py:57)：min_dwell=1.0/min_confidence=0.5；`_set`(88，以接收时刻为 TTL 基准)/`accept`(96，返回 (bool,reason)，含 NO_OP 免 dwell、置信/紧迫门槛、同意图续期、dwell 1s、urgency≥0.9 可抢断)/`clear`(128)。
- **没有任何会话/交棒/互斥字段** —— 目标第 3、4 条的状态机必须新建。

**agent/arbiter.py（179 行）**
- `INTENT_TOOL_SPECS`(arbiter.py:22)：已把 set_intent 做成 strict 函数调用（enum 意图类型、target/urgency/ttl/confidence）。
- `intent_from_call`(arbiter.py:100)：严格校验、不兜底猜测；`motor_mode_for`(138，navigate/combat/interact 才走腿)。
- `IntentArbiter.submit`(arbiter.py:151) → 置信度门槛(169) → bus.accept(178)。
- **这是 request_handoff / report_blocked 的现成 schema 模板。**

### A.4 手嘴并行编排

**agent/executor.py（331 行，手嘴并行的唯一实现）**
- `NARRATE_INSTRUCTIONS`(executor.py:31)：一轮同时要求"≤25 字画面变化"+"完成条件明确成立时写 `[DONE]` 紧跟『理由：』"；`SUMMARY_INSTRUCTIONS`(43)；`DONE_MARK`(50)；`MIN_DONE_REASON_CHARS`=4(53)；`_split_done`(56) 返回 {text,done,reason,rejected}。
- `FastExecutor`(executor.py:81)：adapter/motor/narrator/motor_prompt/game/motion_instruction/on_event/narrate_every_s/done_confirm/step_ms/clock/require_done_reason。
- `_frame_url`(executor.py:134)：每轮 `adapter.grab()` 现抓现编码；`_narrate_loop`(147)：独立线程，连续 done_confirm 次 `[DONE]` 才 `done.set()`。
- `run`(executor.py:200)：每轮 `_frame_url → narrator.push(raw) → motor.decide(...) → adapter.play_steps(steps, step_ms)`；退出原因 7 种（no_motor/budget/max_decisions/capture_error 连续 3 次/motor_error/no_action 连续 3 次空动作/goal_reached）；actions 条目 {t_ms, keys, mouse, steps, decide_ms, pressed, skipped, raw}；`_summarize`(315) 收尾。
- **手与嘴只共享 stop/done 两个 Event；decide 耗时只记录不校验。**

### A.5 慢系统

**agent/lithos.py（576 行，唯一慢系统 HTTP 客户端）**
- `LITHOS_MODELS`(lithos.py:41 flash/flash-fast/flash-ultra)、`DEEPSEEK_MODELS`(50，三档都映射 `deepseek-flash`)、`PROVIDERS`(53)、`DEFAULT_PROVIDER`="deepseek"(70)。
- `DEFAULT_CREDENTIALS_PATH`=`C:/Users/Tsuba/.dsh/.credentials.yaml`(75)、`_read_credential`(78)。
- `DEFAULT_TIMEOUTS`={base:60, fast:30, ultra:15}(100)；`choose_tier`(205，forced>kind>urgency，<=0.34 base / <=0.74 fast / else ultra)；`TierConfig`(129，temperature .6 / max_tokens 1024)。
- `LLMReply`(236)+as_message(250，强制回传 reasoning_content)；`build_user_content`(267，支持图片 part)。
- `LithosClient`(279)：`complete`(308，可传 thinking 开关 341-344、max_retries=2、429/5xx 重试、4xx 直抛、**只有 httpx timeout(cfg.timeout) 没有整体 deadline**(352))；`stream_complete`(406，**硬编码 thinking disabled**(436)、on_delta(413/479/484)、tool_calls 按 index 累积 486-498)；`react`(517)/`plan`(534)/`reflect`(542)/`probe`(550)。

**agent/agent.py（1812 行，老闭环 + 工具面）**
- `DIRECT_SYSTEM_PROMPT`(agent.py:184)：明说"按键不在你手上"、lumine 是唯一动作通道、硬规则 8 条（200-209）；`SYSTEM_PROMPT`(230)/`base_system_prompt`(226)/`compose_system_prompt`(264)/`resolve_system_prompt`(279，走 profiles+prompt_builder，失败回退静态)。
- `TOOL_SPECS`(agent.py:89)=scan/look/lumine/end_test；`UT_TOOL_SPECS`(148)=look/act/end_test；`END_TEST_SPEC`(72)。
- `ToolResult`(311)/`Session`(319，JSONL 落盘 path 326/append 329/save_image 336)。
- `BetterGITools`(364)：observe(448)/scan(454)/look(506)/ensure_foreground(532)/ensure_live(563)/full_screen(692)。
- `judge_intent`(740)：lithos tier=base、thinking=False、max_tokens=8，只回 stop/continue；`_cancel_intent`(772)；live runner 一组（RUNNER_URL 807 … _runner_stop_intent 862）；`lumine`(873)/`_lumine_bridge`(895)。
- `_fast_stream`(1091)/`_fast_probe`(1105)/`_operator`(1133，FastExecutor 包装)/`motor`(1283)/`known_keys`(1373)/`end_test`(1380)/`tool_specs`(1417)/`learn_rule`(1547)/`restart_fast`(1569)/`learn_key`(1612)。
- `dispatch`(agent.py:1624)：按工具名路由，lumine 分支在 1653-1663；`run`(1667)：ensure_foreground→client.complete→tool_calls→dispatch→图片单独 user 消息；`main`(1778)。

### A.6 记忆

**agent/memory.py（531 行）**
- `Episode`(memory.py:47)：text/kind/group_id/source/confidence/ts/meta —— **全仓只有 episode 这一层粒度**；`EPISODE_KINDS`(28)=keymap/rule/place/story/lesson/strategy；`_SOURCE_RANK`(31)。
- `LocalJsonMemory`(97)：`add_episode`(168，去重合并 supports + last_seen)、`_detect_conflict`(208)、`search`(224，分数 = 2.0*hit_ratio + 1.5*confidence + 1/(1+age_days)，冲突 -0.4)、`neighbours`(246)/`conflicts`(271)/`count`(286)。
- `_LoopRunner`(292)：常驻 loop 线程（neo4j 异步驱动必须绑 loop）；`GraphitiMemory`(322)：`add_episode`(409，source=EpisodeType.text)、`search`(439，**group_ids 固定单值**、结果 kind 一律 "story")、`close`(464)。
- `build_memory`(494)：按 profile.memory.backend 选 local-json/graphiti。

**agent/memory_wire.py（169 行）**
- `WRITEBACK_TOOLS`(memory_wire.py:18)=act/learn_key/restart/restart_game；`query_terms`(23，中文 2 字滑窗)；`recall_for`(55，关键词多路 + 最近补齐)；`format_recall`(91，max_chars=600)；`episode_from_tool`(120)；`writeback`(160)。

**agent/graphiti_wiring.py（127 行）**
- `DEFAULT_MODEL`=`BAAI/bge-small-zh-v1.5`/`DEFAULT_DIM`=512(17-18)、`DEEPSEEK_BASE_URL`(19)、`FastEmbedEmbedder`(45)、`build_llm_client`(86)/`build_embedder`(109)/`build_clients`(122)。

### A.7 便利贴 / 规则 / 三槽位提示词

**agent/profiles.py（便签存储已实现，但全仓无调用方）**
- `KeyBinding`(profiles.py:47)；`Profile.note_limit`(124)；`DEFAULT_NOTE_LIMIT`=4000(33)、`MAX_NOTE_CHUNK`=2000(36)。
- `read_notes`(236)/`note_stats`(241)/`write_note`(251，mode=append|replace，**超限抛 ProfileError 不静默截断**(271-275)，tmp 原子替换(276-279))/`clear_notes`(282)。
- `register_game`(323，note_limit 参数 329/345)/`apply_learned_key`(368)/`resolve_game`(421)/`open_profile`(440)。
- 注：便签是 **per-profile 单文件**，不是目标要求的 per-save 双块。

**agent/prompt_builder.py（444 行，三槽位已在此）**
- `TEMPLATE_PATH`(prompt_builder.py:38) → `agent/templates/gamescaling_system_prompt.txt`；`DEFAULT_REQUIRED_KEYS`(41)；`DEFAULT_MIN_CONFIDENCE`=0.5(47)；`UNKNOWN_MARK`(53)。
- `PromptBundle`(76)：text/game_name/fingerprint/version/learning_targets/used_keys/dropped_low_confidence/templates_used/objective_included/instruction。
- `build_key_bindings`(153)：置信度 + 训练词表过滤，产出"待学清单"(190-193)；`build_game_rules`(197)：固定段落 Screen/Goal/Combat/Interaction/Menus and dialogue/Stamina/Other。
- `build_system_prompt`(260)：**槽位替换在 365-370**（按 GAME_NAME → GAME_RULES → KEY_BINDINGS → LEARNING 顺序 replace）；objective 以 "## Current objective" 追加在规则段末(323-334)、**指纹不含 objective**(377-382)；版本号仅内容变化时 +1 并成对落盘(383-406)。

**agent/rule_templates.py**
- `DEFAULT_RULE_BUDGET`=1400(rule_templates.py:56)、`OBJECTIVE_TITLE`(59)、`RuleTemplate`(69)、`select_templates`(184)、`render_rules`(217)、`assemble`(248)、`TemplateLibrary`(282)。

### A.8 TUI / 主循环 / 本地 HTTP / 观众侧

**agent/tui.py（50KB，唯一的流式闭环 + 本机 HTTP）**
- `Entry`(tui.py:189)/`History`(214，trim 254)/`Console`(297)/`Interjections`(497)/`Runtime`(570，snapshot 595/event 619)/`QuietServer`(626)。
- `Api`(tui.py:635)：**本机 HTTP 服务** —— Bearer token(656-658)、GET /state(674)、GET /events?limit(677)、POST /say(701)、POST /quit(706)、`url`(714)；起在 api_port(791-801)，把 {url,token} 写 `_diag/tui-endpoint.json`(795-800)。**目标里的 /danmaku、/stats 应该长在这一层旁边。**
- `run`(tui.py:718)：Session(732)/open_profile(737)/resolve_system_prompt(738)/BetterGITools(739)/ProfileAdapter.restart(747)/LithosClient(750)/build_memory(759)/Runtime(764)/Interjections(765)/stop_event(770)/dev_mode(775)/tools.trace(777)。
- 主循环(833)：drain 插话 → console.viewer + runtime.event("interject") + history.add（**现在是整段弹幕当 user 消息塞进 LLM**，852-855）→ ensure_foreground(861) → history.trim(865) → 记忆 `recall_for`/`format_recall`(887-893) → `messages=history.messages(now_line)`(904) → `client.stream_complete`(917，on_delta 只渲染 reasoning) → history.add assistant(949) → tool_calls 逐个 dispatch(988) → writeback(1003-1012) → pending_images 单独 user 消息(1027-1031)；`is_reasoning_400`(928-933)；KEEP_TEXT_MESSAGES/KEEP_IMAGES 控制窗口。

**agent/say.py（82 行）**
- `ENDPOINT`=`E:/BetterAGI/_diag/tui-endpoint.json`(say.py:23)、`load_endpoint`(26)/`call`(32，Bearer)/`main`(44) → 打 TUI 的 /say /state /events?limit= /quit。
- **这就是"外部进程经本机 HTTP 喂数据进运行时"的现成范式**，弹幕服务与事件回填可照抄。

### A.9 采集与输入

**agent/adapter.py（36KB）**
- `ProfileAdapter`(adapter.py:125)；三路抓帧 `CAPTURE_MODES`(328)；`_wgc_grab`(330)：WGC 原图 966×760 → screen_crop 裁(357-361) → fit(364) → info{source,width,height,native,frames,captured_at}(365-372)。
- `grab`(378)：默认 wgc，按 wgc→screen→printwindow 降级，写 info[fallback_chain]/[degraded]，**返回 info["data"] 原始字节**。
- `play_steps`(adapter.py:670)：6 步、held 集合实现长按语义、每步 sleep 对齐 step_ms(716)、finally 里 key_up 松键(719-723)、返回 {pressed,skipped_keys,game,steps,step_ms,hovered,input_safe,layout}。
- 其他：post_keys(477)/raw_press(506)/type_text(544)/ensure_input(560，IME 守卫)/press_tune(574)/act(589)/restart(728)/learn_key(738)。

**agent/wgc_capture.py（20KB）**：Windows Graphics Capture 的 ctypes 实现 —— `WgcCapture`(wgc_capture.py:439)/`grab_frame`(485)/`find_window_by_title`(494)。

**agent/screen_crop.py（136 行）**：`crop_content`(screen_crop.py:118)/`is_blank`(133)/`band_top`(90)，只裁标题栏、不按包围盒裁。

**agent/ut_textframe.py（80 行）**：`render_text`(ut_textframe.py:44) —— 文字帧渲染，**"大肥鱼CPU烧了"与故障冻结画面可直接复用**。

**agent/learning.py（12KB）**：`learn_key`(learning.py:122)/`learn_rule`(213)/`verify_restart`(283) —— 键位/规则学习并重建 PromptBundle。

### A.10 关键能力"现在在哪"速查表

| 能力 | 现在的位置 |
|---|---|
| Omni 建连/发帧/收事件 | `agent/omni.py:233` / `:157` / `:350`（WebRTC 版 `agent/webrtc_fast.py:121`） |
| Omni 指令注入 | `agent/omni.py:104`；`agent/webrtc_fast.py:303` |
| Omni 工具调用解析 | `agent/omni.py:196`（OmniReply.first_call） |
| 手模型请求/解析/执行 | `agent/motor.py:141` → `agent/gamescaling_action.py:219` → `agent/adapter.py:670` |
| 手模型系统提示词（三槽位） | `agent/prompt_builder.py:260`（365-370） |
| 意图结构与接受规则 | `agent/intent.py:25` / `:96` |
| 手嘴并行 | `agent/executor.py:81` |
| 慢系统 LLM | `agent/lithos.py:308` / `:406` |
| 慢系统工具面 | `agent/agent.py:89`/`:148`/`:1624`；TUI 侧 `agent/tui.py:988` |
| 记忆读写 | `agent/memory.py:168/224/409/439`；`agent/memory_wire.py:55/160` |
| 便签存储 | `agent/profiles.py:236/251`（**无调用方**） |
| TUI 主循环 | `agent/tui.py:718`（循环 833） |
| 本机 HTTP | `agent/tui.py:635` |
| 截屏 | `agent/adapter.py:378/330` |
| 推流 / 录播 | **本仓库 agent/ 内没有**；先例在 `Amaidesu/src/modules/studio/obs/obs_provider.py:9`、`BetterGI/integrations/vtuber/render_vtuber.py:10` |

---

## B. 差异表

> 列说明：现状=证据；复用=可直接用的现有代码；新增=要写的文件；改造=要改的现有代码。

### B.1 快系统会话管理（每意图新建、意图结束即断，上下文只保留本意图）

| 项 | 内容 |
|---|---|
| 现状 | `OmniClient` 单实例跨 ask 复用（`agent/omni.py:233`）；`FastStream` 一条 WS 永久活着（`agent/fast_stream.py:49`）；"取消"只是 `response.cancel`（`agent/omni.py:317`），不关连接；无 intent_id 概念 |
| 复用 | `WsTransport`(`agent/omni.py:157`)、`session_update`(`:104`)、`encode_jpeg_frame`(`:213`)、`OmniReply`(`:196`) |
| 新增 | `agent/fast_session.py`：`IntentSession.open(intent_id, instructions, tools)` / `.ask(...)` / `.close()`，内部持有独立 `WsTransport` + `response_id`，close 时一定关 socket |
| 改造 | `agent/omni.py:330 ask` 拆成 open/ask/close 三段；`agent/fast_stream.py:101` 帧泵改为"会话内的帧泵"；`agent/executor.py:200` 每轮不再复用同一 client |

### B.2 手模型调用（history 跨调用保留 + 意图开始 policy.reset()）

| 项 | 内容 |
|---|---|
| 现状 | `GamescalingClient.history` 是实例字段（`agent/motor.py:90`/`:124`），`reset`(`:117`) 存在但**零调用点**；没有意图边界概念；`DEFAULT_TIMEOUT`=60s(`:30`) 与 200ms 预算无关 |
| 复用 | `build_request`(`agent/gamescaling_request.py:118`)、`parse_action`(`agent/gamescaling_action.py:219`)、`sanitize_steps_report`(`agent/gamescaling_vocab.py:83`)、`adapter.play_steps`(`agent/adapter.py:670`) |
| 新增 | `agent/hand_policy.py`：持有 history（跨意图保留）+ `begin_intent()`（调 `policy.reset()` 语义 + 清空意图内临时态） |
| 改造 | `agent/motor.py:124 record` 改由 hand_policy 调用；`agent/motor.py:141 decide` 增加 deadline_ms 与"解析失败即抛错" |

### B.3 手模型三槽位 prompt

| 项 | 内容 |
|---|---|
| 现状 | `build_system_prompt` 已经产出完整三槽位文本（`agent/prompt_builder.py:365-370`）；但手模型请求只有单个 system 字符串（`agent/gamescaling_request.py:104-105`） |
| 复用 | `build_system_prompt`(`agent/prompt_builder.py:260`)、`build_key_bindings`(`:153`)、`build_game_rules`(`:197`)、`rule_templates.assemble`(`agent/rule_templates.py:248`) |
| 新增 | 无（结构已在；只在 request 层补显式分段） |
| 改造 | `agent/gamescaling_request.py:95 build_messages` 接受 `system_sections={GAME_NAME, GAME_RULES, KEY_BINDINGS}` 并分三段拼 system；`agent/agent.py:1283 motor` 传 bundle |

### B.4 慢系统规划（markdown 任务 + 关 thinking ≤2s + 卡关 N 次才开）

| 项 | 内容 |
|---|---|
| 现状 | `stream_complete` **硬编码 thinking disabled**（`agent/lithos.py:436`）；超时只有 httpx timeout(`:352`)，无整体 deadline；TUI 调它时无超时（`agent/tui.py:917`） |
| 复用 | `LithosClient.complete/stream_complete`(`agent/lithos.py:308/406`)、`choose_tier`(`:205`)、`is_reasoning_400`(`agent/tui.py:928`) |
| 新增 | `agent/slow_plan.py`：`plan(context, budget_s=2.0)` → {markdown_task, tool_calls, degraded}；`stuck_counter` 达 N 才开 thinking |
| 改造 | `agent/lithos.py:436` thinking 提为参数；`agent/lithos.py:406` 加 deadline 与中止事件；`agent/tui.py:917` 换成带预算的调用 + 超时占位画面 |

### B.5 交棒（request_handoff / report_blocked 必交棒）

| 项 | 内容 |
|---|---|
| 现状 | `intent.IntentBus` 只有接受/过期（`agent/intent.py:96`），无状态机；`arbiter.INTENT_TOOL_SPECS`(`agent/arbiter.py:22`) 是唯一的 strict 工具范例；`executor.run` 的退出原因是 7 个字符串（`agent/executor.py:200`），不是交棒事件 |
| 复用 | `INTENT_TOOL_SPECS`(`agent/arbiter.py:22`)、`intent_from_call`(`:100`)、`IntentBus`(`agent/intent.py:57`) |
| 新增 | `agent/orchestrator.py`：状态机 FAST_RUNNING→HANDOFF_TO_SLOW→SLOW_RUNNING→（FREEZE/INCIDENT），唯一允许改状态的入口；handoff 工具 schema 定义在此 |
| 改造 | `agent/executor.py:200` 退出原因映射成 handoff reason（done/blocked/budget/error）；`agent/omni.py:43 OmniConfig.tools` 注入 request_handoff/report_blocked |

### B.6 受限工具集（9 个 + ask_slow_reference）

| 项 | 内容 |
|---|---|
| 现状 | `TOOL_SPECS`(`agent/agent.py:89`)=scan/look/lumine/end_test；`UT_TOOL_SPECS`(`:148`)=look/act/end_test；`dispatch`(`:1624`) 只认这 8 个名字；目标 9 个工具 **0 实现** |
| 复用 | `ToolResult`(`agent/agent.py:311`)、`Session`(`:319`)、`_post`(`:423`)、`dispatch` 路由骨架(`:1624`)、`tool_specs` 动态注入(`:1417`) |
| 新增 | `agent/slow_tools.py`：9 个工具的实现 + schema；`agent/reference_agent.py`：ask_slow_reference 旁路子代理（无画面权限，只读记忆/便签） |
| 改造 | `agent/agent.py:89/148` spec 列表合并为一份，由 orchestrator 按"快/慢"授权裁剪 |

### B.7 便利贴（per-save 单文件，key_binding 1K + game_rules 1K，硬顶 2K，整体重写、不淘汰）

| 项 | 内容 |
|---|---|
| 现状 | 存储有（`agent/profiles.py:236 read_notes`/`:251 write_note`）、**零调用方**；配额是 per-profile 4000(`:33`)，单文件混合内容 |
| 复用 | `write_note` 的原子写与"超限抛错不截断"(`agent/profiles.py:251-279`)、`read_notes`(`:236`) |
| 新增 | `agent/sticky.py`：`save_id` → 目录下 `key_binding.md`/`game_rules.md` 两文件，各 1024 字符，合计硬顶 2048；`write_sticky(save_id, slots)` 整体重写 |
| 改造 | `agent/profiles.py:124 note_limit` 与 sticky 配额解耦；`agent/agent.py:1624` 加 read_sticky/write_sticky 分支 |

### B.8 Graphiti 五层记忆（上下文文本作 query，预算中等不跨会话累积，写回在慢系统落账）

| 项 | 内容 |
|---|---|
| 现状 | 只有 `Episode` 一层（`agent/memory.py:47`）；检索是关键词打分(`:224`)或 Graphiti search(`:439`，**group_ids 单值**、结果 kind 一律 story)；注入预算 600 字(`agent/memory_wire.py:91`)；写回由工具触发(`:160`)而非慢系统落账 |
| 复用 | `LocalJsonMemory`(`agent/memory.py:97`)、`GraphitiMemory`(`:322`)、`_LoopRunner`(`:292`)、`graphiti_wiring`(`agent/graphiti_wiring.py:122`)、`_detect_conflict`(`agent/memory.py:208`) |
| 新增 | 层标签 {working/episodic/semantic/procedural/social} + `atoms`（原子事实节点）；`session_id` 作用域；`agent/memory_query.py`：用"当前上下文文本"做向量关联（embedder 已有 `FastEmbedEmbedder`） |
| 改造 | `agent/memory.py:409 add_episode` 增加 layer/atoms；`:439 search` group_ids 改列表 + 按层加权；`agent/memory_wire.py:55 recall_for` 改上下文 query |

### B.9 时间轴（本场单调毫秒，speech/l2d/viewer 事件 JSON）

| 项 | 内容 |
|---|---|
| 现状 | 只有零散 `time.monotonic`（`agent/adapter.py:716` 节拍、`agent/agent.py:591/614/628` 前台等待）；executor 的 `t_ms` 相对本段 started（`agent/executor.py:178`/`:272`）；`intent` TTL 用 `time.time`(`agent/intent.py:88`) —— 三套时钟 |
| 复用 | `Runtime.event`(`agent/tui.py:619`)、`Runtime.snapshot`(`:595`)、`Session.append` JSONL(`agent/agent.py:329`) |
| 新增 | `agent/timeline.py`：`Clock`（本场单调 ms）+ `emit(kind=speech\|l2d\|viewer\|action\|handoff, payload)` → append-only JSONL；TTS 先按字数估时长、拿到音频后回填修正 |
| 改造 | `agent/executor.py:178`/`:272` 的 t_ms 换成全局 `clock.now_ms()`；`agent/adapter.py:716` 回传 plan_t/actual_t |

### B.10 录播分片（固定 10 分钟 mp4，每片首帧写 start_ms 与帧号）

| 项 | 内容 |
|---|---|
| 现状 | `agent/` 下 grep ffmpeg / mp4 / VideoWriter = **0 命中**；无推流封装 |
| 复用 | 外部门禁：`BetterGI/integrations/vtuber/render_vtuber.py:10`（build_ffmpeg_command / ASS）、`BetterGI/integrations/session/tests/test_render_session.py:56/60`（emit_video_start(fps,width,height) 的写法）；抓帧 `agent/adapter.py:378` |
| 新增 | `agent/recorder.py`：WGC 帧 → ffmpeg `image2pipe` → 每 600s 切段；段首帧写 sidecar `{start_ms, frame_no, save_id}` |
| 改造 | `agent/adapter.py:378 grab` 增加"旁路 tap"（录制与 Omni 共用一次抓帧，不重复截屏） |

### B.11 弹幕（GET /danmaku?since&limit、GET /stats，只有慢系统读）

| 项 | 内容 |
|---|---|
| 现状 | 无端点、无结构化缓冲；TUI 把插话包成 user 消息直接进 LLM（`agent/tui.py:852-855`）；`agent/agent.py:252-254` 只在系统提示词里讲弹幕 |
| 复用 | `Api`(`agent/tui.py:635`)、Bearer 鉴权(`:656-658`)、`say.py` 客户端范式(`agent/say.py:32`) |
| 新增 | `agent/danmaku.py`：环形缓冲（ts/user/text/type）+ since/limit 查询 + 在线/礼物统计；`/danmaku`、`/stats` 两条路由 |
| 改造 | `agent/tui.py:833` 主循环删掉"弹幕→history.add"直连；弹幕只进 buffer |

### B.12 故障熔断（parsed=False/truncated = 程序错误 → 冻结→落账→停推流→事故报告→退进程）

| 项 | 内容 |
|---|---|
| 现状 | 解析失败被折成 NOOP 继续跑（`agent/motor.py:141` 的 label_action 兜底、`agent/gamescaling_action.py:81 history_text` 写 NOOP_ACTION_TEXT(44)）；executor 只有 motor_error 退出（`agent/executor.py:200`），没有冻结/落账/报告 |
| 复用 | `Action.parsed/truncated` 字段(`agent/gamescaling_action.py:70`)、`Action.as_dict`(`:109`) 可作完整中间态、`ut_textframe.render_text`(`agent/ut_textframe.py:44`) 冻结画面、`Runtime.snapshot`(`agent/tui.py:595`) 进程状态 |
| 新增 | `agent/incident.py`：写 `incidents/<ts>/`（request_body.json / raw_reply.txt / parsed.json / timeline_slice.jsonl / proc.json）+ 冻结帧 + 停推流钩子 |
| 改造 | `agent/motor.py:141` 解析失败 raise `MotorParseError`；`agent/executor.py:200` 捕获后走熔断链；`agent/gamescaling_action.py:81` 不再写 NOOP |

---

## C. 改造清单（按文件，P0=不修就不能直播 / P1=影响体验或可回放性 / P2=稳健性）

### C.1 `agent/omni.py`（P0）

- **P0** `omni.py:350 collect`：给循环里的 `recv_json()`（`:359`）加 wait_for（单帧 4s、整轮 12s），超时抛 `OmniTimeout` 而不是死等。
- **P0** `omni.py:330 ask`：拆成 `open_session()/ask_once()/close_session()`，ask 只服务"一个意图内的一次响应"。
- **P0** `omni.py:359`：只接受 `response_id` == 本次 `create_response` 返回值的事件，其余计 stray 丢弃（**串台的直接补丁**）。
- **P1** `omni.py:293 _drain_until_done`：预算耗尽的残留事件要记录并把会话标脏，下次必须重连。
- **P1** `omni.py:317 cancel_inflight`：取消后校验 response.done 是否真到；没到直接 close 连接。
- **P1** `omni.py:233 OmniClient`：加 `last_response_id`/`closed` 状态与 `close()`，供 fast_session 管生命周期。
- **P2** `omni.py:213 encode_jpeg_frame`：超 256KB 时降质量重编（现在是直接报错）。

### C.2 `agent/fast_session.py`【新】（P0）

- **P0** 每意图 `new WsTransport` + `session_update` + 首帧预热；意图结束 close，绝不跨意图复用。
- **P0** 会话持有 `intent_id`，把 Omni 的 function_calls 映射成 `request_handoff/report_blocked/ask_slow_reference` 三种事件。
- **P1** 断线只在本意图内重连一次，仍失败则上报 handoff。

### C.3 `agent/motor.py`（P0）

- **P0** `motor.py:141 decide`：`parsed=False` / `truncated=True` 从"兜底空动作"改为 `raise MotorParseError`（现在 `:52 label_action` 会造出 6 组假动作）。
- **P0** `motor.py:124 record` / `:117 reset`：history 上移一层（属会话不属 client），意图开始调 reset。
- **P1** `motor.py:141`：加 `deadline_ms`（目标 200ms），超时返回 `MotorTimeout` 由编排层决定"用上一步动作"还是"停"。
- **P1** `motor.py:130 health`：加 vLLM 就绪/模型尺寸探针，启动时校验。

### C.4 `agent/gamescaling_request.py`（P0/P1）

- **P0** `gamescaling_request.py:104-105`：单 system 字符串改为三槽位分段（GAME_NAME/GAME_RULES/KEY_BINDINGS 各一段），入参直接吃 `prompt_builder` 的 text。
- **P1** `gamescaling_request.py:95 build_messages`：history 每条动作附"上一步是否生效(pressed/skipped)"，让手能自纠。
- **P2** `gamescaling_request.py:118 build_request`：temperature/max_tokens 提为参数。

### C.5 `agent/gamescaling_action.py`（P0）

- **P0** `gamescaling_action.py:81 history_text`：解析失败禁止写 NOOP（会污染 history），改写成 `<parse_failed>` 并让上层熔断。
- **P1** `gamescaling_action.py:219 parse_action`：lenient 结果单独标 `lenient=True`，不要和严格解析共用 `parsed`。
- **P2** `gamescaling_action.py:246 finalize`：补出的 action_start/action_end 记进 raw，便于事故报告复现。

### C.6 `agent/executor.py`（P0）

- **P0** `executor.py:200 run`：decide 前记 `now_ms`、decide 后校验 ≤200ms，超预算计数并入 incident（现在只记 `decide_ms`）。
- **P0** `executor.py:147 _narrate_loop` + `:200 run`：加 `IntentContext` mailbox（手每步 → 嘴的 commentary；嘴的结论 → 手的 instruction），替换"只共享 stop/done"。
- **P1** `executor.py:200` 的 capture_error/motor_error 分支接熔断链（冻结→落账→停推流→incident→exit）。
- **P1** `executor.py:315 _summarize`：改用 timeline 事件重放，不再"再问一次 Omni"。

### C.7 `agent/intent.py`（P1）

- **P1** `intent.py:25 Intent`：加 `intent_id/handoff_state/owner(fast|slow)/session_id`。
- **P1** `intent.py:96 accept`：加"快慢互斥"不变量（handoff 期间拒绝新的快意图）。
- **P2** `intent.py:88 _set`：TTL 基准改 `timeline.now_ms`，避免 `time.time` 与单调时钟混用。

### C.8 `agent/lithos.py`（P0/P1）

- **P0** `lithos.py:406 stream_complete`：加 `deadline_s`（默认 2.0），超时 yield 中止事件，不靠 httpx 超时。
- **P1** `lithos.py:436`：硬编码 thinking disabled 提为参数（卡关 N 次才开）。
- **P1** `lithos.py:352`：外层加整体 deadline + 一次重试的预算封顶。
- **P2** `lithos.py:205 choose_tier`：直播固定 fast 档，不因 urgency 升到 ultra。

### C.9 `agent/tui.py`（P0/P1）

- **P0** `tui.py:852-855`：删掉"弹幕直接进 history"，改为慢系统经 `read_danmaku` 按需取。
- **P0** `tui.py:635 Api`：新增 `GET /danmaku?since=&limit=` 与 `GET /stats`，鉴权沿用 Bearer(`:656`)。
- **P1** `tui.py:917 stream_complete`：换成带超时的慢系统调用，超时在画面中间显示占位（复用 `ut_textframe.render_text`）。
- **P1** `tui.py:887-893` 记忆注入：query 从关键词拼接改为"当前上下文文本 + 最近弹幕摘要"，预算固定。
- **P2** `tui.py:595 Runtime.snapshot`：扩成时间轴/事故报告的快照源。

### C.10 `agent/agent.py`（P0/P1）

- **P0** `agent.py:89`/`:148`：新增 10 个工具 schema（9 个 + ask_slow_reference）。
- **P0** `agent.py:1624 dispatch`：加对应分支；lumine 分支(`:1653-1663`)隔离到"只有原神走"。
- **P1** `agent.py:1667 run` 与 `agent/tui.py:718 run` 合一，抽 Orchestrator。
- **P2** `agent.py:1417 tool_specs`：动态注入从"已知键位"扩展到"便签三槽位"。

### C.11 `agent/memory.py` + `agent/memory_wire.py`（P1）

- **P1** `memory.py:47 Episode`：加 `layer`(五层) 与 `atoms`(原子事实)；EPISODE_KINDS(28) 仅作 kind。
- **P1** `memory.py:439 search`：group_ids 改列表（含跨游戏社交层），结果带 layer。
- **P1** `memory.py:409 add_episode`：写回同时落 episode + atoms，source 标 slow。
- **P1** `memory_wire.py:55 recall_for`：query 改为上下文文本，走向量/Graphiti 混合，预算中等（现 600 字）。
- **P2** `memory.py:224 search`：加 session 隔离（工作层不落盘、情景层带 session_id）。

### C.12 `agent/sticky.py`【新】+ `agent/profiles.py`（P0）

- **P0** 新 `sticky.py`：`save_id` → 两块固定配额各 1024 字符、合计硬顶 2048，`write_sticky` 整体重写、不淘汰。
- **P0** `profiles.py:251-279`：原子写 + 超限抛错的实现直接搬。
- **P1** `profiles.py:124 note_limit`：与 sticky 配额解耦，避免两套上限打架。

### C.13 `agent/adapter.py`（P1）

- **P1** `adapter.py:378 grab`：加旁路 tap 回调（录制与 Omni 共用一次抓帧）。
- **P1** `adapter.py:670 play_steps`：回传每步 plan_t/actual_t/是否被吞键，供时间轴与时效校验。
- **P2** `adapter.py:330 _wgc_grab`：暴露未裁剪原图（录制要全画面）。

### C.14 新增模块（P0/P1）

- **P0** `agent/timeline.py`：`Clock` + `emit(kind)` → JSONL；TTS 估时长与回填。
- **P0** `agent/recorder.py`：ffmpeg image2pipe 分片 600s，每片首帧 sidecar `{start_ms, frame_no}`。
- **P0** `agent/danmaku.py`：环形缓冲 + /danmaku /stats 数据源。
- **P0** `agent/incident.py`：事故目录 + 冻结帧 + 停推流 + 进程状态。
- **P1** `agent/orchestrator.py`：快慢互斥状态机，唯一改状态的入口。

---

## D. 风险点（会直接阻碍目标架构的地方）

### D.1【P0】Omni `collect` 无超时，"只放弃不取消"→ 跨意图串台

**证据**
- `agent/omni.py:350 collect` 的循环体只有 `transport.recv_json()`（`agent/omni.py:359`），**没有 wait_for**。
- 唯一的外层超时在 `agent/fast_system.py:133` 的 `future.result(timeout=60)`——超时后线程仍堵在 `recv_json` 里，连接不被关闭。
- `_drain_until_done`(`agent/omni.py:293`，budget 8.0) 与 `cancel_inflight`(`agent/omni.py:317`，budget 8.0) **预算用完只 return**，既不抛也不关连接。
- `OmniClient._inflight`(`agent/omni.py:233`) 是跨 ask 复用的唯一闸门，语义是"同连接串行"而不是"同意图隔离"。
- `agent/fast_stream.py:130 ask` 的 `timeout=45.0` 是 future 层超时，同样不取消底层读。
- WebRTC 车道也不安全：`agent/webrtc_fast.py:338 ask` 虽有超时，但轮次由 `server_vad`(`:303`) 驱动，**上一 response 未完就投下一帧必然串**。

**后果**：上一意图的解说/工具调用会被下一意图读到。若 `request_handoff` 是通过 Omni 的 function call 触发，串台等于"上一意图的 handoff 在下一意图生效"→ 交棒时机错乱、慢系统被无关事件唤醒，直播节奏直接崩。线程堆积后帧延迟无上界。

**修法（按落地顺序）**
1. `collect` 每帧 `wait_for(4s)`、整轮 deadline(12s)；超时抛 `OmniTimeout` 并 **close transport**（脏连接不复用）。
2. 只接受 `response_id` 匹配的事件（`omni.py:359` 处过滤），其余计 stray 丢弃并落日志。
3. 会话化：`fast_session` 每意图 new `WsTransport`，意图结束 close；`_inflight` 语义改为"同意图串行"。
4. 交棒即断：`request_handoff` 触发后立刻 close，慢系统跑完再开新会话。

### D.2【P0】手嘴互不通信（只共享 stop/done 两个 Event）

**证据**
- `agent/executor.py:81 FastExecutor` 的共享状态只有 `stop`/`done` 两个 Event。
- `_narrate_loop`(`agent/executor.py:147`) 是独立线程：只 `narrator.push(raw)` 帧、读 `last_text`；`run`(`:200`) 只把 raw 交给 `motor.decide`。
- `actions` 条目里没有 commentary；`narrator` 不知道手刚按了什么、失败在哪一步。

**后果**：目标要求的"看画面出解说 / 判断执行是否该结束"退化成两个瞎子并行。更具体：嘴每 `narrate_every_s`=1s 取一帧、手每 200ms 一步，**嘴看到的是"当前帧"而不是"第 k 步的帧"**，`[DONE]` 判定必然滞后一个动作；`require_done_reason`(`:53`) 只能防"没理由就结束"，防不了"看错步骤就结束"。

**修法**
1. 建 `IntentContext{intent_id, mailbox}`：手每步写 `{t_ms, keys, pressed, skipped, frame_ref}`，嘴每次读最近 N 条并拼进 prompt。
2. 嘴的结论（继续/DONE/需重规划）以结构化事件回写 mailbox，手在下一次 `decide` 的 instruction 里带上。
3. 时序对齐：嘴的输入用"手第 k 步 + 该步画面"，不要用"最新帧"。

### D.3【P0】全开环无时效校验（200ms 预算没人守）

**证据**
- `agent/executor.py:200` 每轮把 `decide_ms` 写进 actions 但**从不比较**。
- `agent/adapter.py:670 play_steps` 每步 sleep 对齐 step_ms(`:716`)，decide 花了 900ms 时整段已经晚了，没有"追不上就放弃"的分支。
- 时间预算的定义在 `agent/gamescaling_action.py:36-41`（CHUNK_MS=200/6×33.33），但 `agent/motor.py:30 DEFAULT_TIMEOUT`=60.0——两者相差 300 倍，说明目前根本没有"200ms"这条约束。

**后果**：动作与画面脱节，长按/连招直接错；观众看到"人发呆然后乱按"。

**修法**
1. `decide` 加 deadline（**墙钟按实测 p95：关思考 740ms / 开思考 1490ms**，见 `07_errata_measured_latency.md`；原写 200/300ms 是游戏时间口径，已作废），超时用"上一动作的下一步"或纯 NOOP 占位，并统计 miss 率。
2. 每步执行前校验 plan_t/actual_t 偏差，>100ms 丢掉积压步数，不追帧播放。
3. 时间轴记 planned_ms/actual_ms，事后可算 jitter 分布 —— 这就是"时效校验"的落点。

### D.4【P1】解析失败被当成"合法空动作"

**证据**：`agent/motor.py:141 decide` 在 parsed=False 时走 `label_action`(`agent/motor.py:52`) 造 6 组空动作；`agent/gamescaling_action.py:81 history_text` 把失败写成 `NOOP_ACTION_TEXT`(`:44`)；`Action.parsed/truncated`(`agent/gamescaling_action.py:70`) 字段已存在但没人当错误用。

**后果**：目标"parsed=False/truncated → 程序错误熔断"永远不会触发，模型坏了系统继续空转，事故无从复盘。

**修法**：`motor.decide` 抛 `MotorParseError`（带完整 raw_reply）；executor 捕获后走 D.7 链；`history_text` 写 `<parse_failed>` 占位。

### D.5【P1】双慢系统闭环 + 工具面分裂

**证据**：`agent/agent.py:1667 run`（Lithos 非流式）与 `agent/tui.py:718 run`（流式 + 本地 HTTP）并存；工具表分裂成 `agent/agent.py:89` 与 `:148`；两个入口的系统提示词也不同（`agent/agent.py:184` vs `agent/tui.py:738 resolve_system_prompt`）。

**后果**：给慢系统加 read_sticky/read_danmaku 要改两处；行为在不同入口不一致，事故现场难复现。

**修法**：先合 Orchestrator 单入口、工具表只留一份（`agent/agent.py:1417 tool_specs` 作唯一来源）。

### D.6【P1】记忆不是五层、检索不是上下文向量

**证据**：`agent/memory.py:47 Episode` 单粒度；`search`(`:224`) 是关键词打分；`GraphitiMemory.search`(`:439`) `group_ids` 固定单值、结果 kind 一律 "story"；`format_recall`(`agent/memory_wire.py:91`) 只有 600 字；写回由 `writeback`(`:160`) 在工具侧触发，而不是"慢系统落账时"。

**后果**：工作记忆会污染长期记忆（跨会话累积）、程序性/社交层无处安放、检索命中率低。

**修法**：见 C.11。关键一条：**工作层只在内存；情景层按 session_id 隔离；语义/程序性/社交三层才跨会话**。

### D.7【P1】没有故障熔断链与事故报告

**证据**：`agent/executor.py:200` 的 capture_error/motor_error 只是退出；`agent/tui.py:595 Runtime.snapshot` 只是 UI 快照；`agent/ut_textframe.py:44 render_text` 有文字帧能力但没接到错误路径。

**修法**：新增 `agent/incident.py`，按目标顺序：冻结（把"程序错误"文字帧推到画面与推流源）→ 落账（写 memory + timeline）→ 停推流（通知 recorder/OBS）→ 写 `incidents/<ts>/`（request_body/raw_reply/parsed/timeline_slice/proc）→ `os._exit`。

### D.8【P2】其他会咬人的地方

- **帧体积**：`agent/omni.py:213 encode_jpeg_frame` 超 256KB 直接报错；1080p 帧很容易超，需要质量回退。
- **凭证两条路**：Omni 走 `resolve_key`(`agent/omni.py:57`)，慢系统读 `.credentials.yaml`(`agent/lithos.py:75`)，容器/云机部署要对齐。
- **便签上限冲突**：`agent/profiles.py:33 DEFAULT_NOTE_LIMIT`=4000 与目标 2048 冲突，别直接复用。
- **三套时钟**：`agent/executor.py:178/272` 段内相对 t_ms、`agent/intent.py:88` 用 `time.time`、adapter 用 monotonic —— 必须统一到 `timeline.Clock`。
- **stdout 争用**：`agent/tui.py:626 QuietServer` 与 Console 会抢 stdout，录制/事故报告写 stdout 前需接管。

---

## 附：建议落地顺序（最小可跑路径）

1. **第 1 周**：omni 超时 + response_id 过滤（D.1）→ `fast_session` 会话化 → motor 解析失败抛错（D.4）。
2. **第 2 周**：`IntentContext` mailbox（D.2）+ decide deadline（D.3）+ `timeline`。
3. **第 3 周**：`orchestrator` 状态机 + request_handoff/report_blocked 工具 + 慢系统 2s deadline（B.4/B.5）。
4. **第 4 周**：sticky + 9 个工具（B.6/B.7）+ 弹幕 HTTP（B.11）+ 录制分片（B.10）+ incident 熔断（B.12）。
