# 系统状态与运行链路

更新日期：2026-10-27

## 这个项目在做什么

一句话：让大模型「玩」原神，并把整场过程录成可后期成片的素材。

不直播、不推流。链路由三段组成：

```text
① 看与操作
   BetterGI 截取游戏窗口、编码帧、模拟键鼠（只做图像识别 + 模拟输入）
        │  本机 WebSocket 30648
        ▼
② 思考与解说
   导演（本机 LLM）看画面 → thought / guidance / commentary
   Lumine（AutoDL 视觉模型）看画面与指导 → 200ms 动作块
        │  同一实时会话
        ▼
③ 记录与成片
   录制器 → gameplay.mp4 + session.jsonl（思考/解说/动作/VTuber 参数）
   渲染器 → 飞鱼 CosyVoice3 配音 + 字幕 + 对齐 → final.mp4
```

会话文件是唯一权威时间轴；语音在后期生成，按 `speech.t` / `speech.dur`
对齐，口型包络也随文本一起存进文件。

## 运行路线与本地实测

为了在没有原神、没有 AutoDL、没有 LLM 密钥的机器上验证协议，新增了
`tools/pipeline_smoke/fake_stack.py`：它同时扮演假 BetterGI Live Hub
（持续推画面、回显解说与动作）和假 OpenAI（按轮次返回导演 JSON）。
以下测试都是实际跑出来的，不是单元测试替身。

| 阶段 | 实际运行 | 结果 |
|---|---|---|
| 1. 录制器单独 | 假 Hub → `record_session.py` 6 秒 | `gameplay.mp4` 1280×720/15fps/5.93s；`session.jsonl` 10 条事件 |
| 2. 导演 + 录制 | 假 Hub + 假 LLM + 导演 12 秒 | 5 条思考、5 条解说、5 条指导全部落盘 |
| 3. 操作桥 + 录制 | 假 Hub + `autodl_server --mock` + 桥 12 秒 | 51 个动作落盘，含按键、鼠标位移、光标、模型 thought |
| 4. 三者并发 | 导演 + 桥 + 录制 20 秒 | 8 解说 / 8 思考 / 8 指导 / 77 动作 / 25 个 VTuber 参数 |
| 5. 飞鱼成片 | 用阶段 4 的会话渲染 | `final.mp4` 23.35s，8 条字幕，AAC 非静音，飞鱼 cute |
| 6. 一条命令 | `run_episode.py` 拉起导演与桥 14 秒 | 6 解说 / 6 思考 / 57 动作；再用飞鱼渲染出 18.15s 成片 |
| 7. 真实 Lumine | AutoDL 4090 + vLLM 0.7.3 + 7B 模型 + 真实游戏截图 | 返回合法动作块；首帧 2.2s，后续 0.39–0.46s/帧 |
| 8. DeepSeek + 记忆 | 假 Hub + 真实 `deepseek-flash` + OpenViking 30 秒 | 11 解说 / 6 思考，scene/plot 写入；下一轮能检索到本场与设定 |

阶段 4/5 的产物在
`E:\BetterAGI\tools\pipeline_smoke\sessions\stage4\`
与 `stage5\`，可以直接打开 `final.mp4` 对照。

真实 AutoDL 的部署细节与兼容补丁见
[integrations/lumine/AUTODL_DEPLOY.md](../../integrations/lumine/AUTODL_DEPLOY.md)
第 8 节和 `E:\BetterAGI\tools\autodl_deploy_notes.md`。

## 测试汇总

| 套件 | 结果 |
|---|---|
| 会话格式 / 录制 / 渲染 | **15 passed** |
| Lumine 导演 / 桥 / 协议 / 服务 | **151 passed** |
| BetterGI ExternalAccess（C#） | **234 passed** |
| BetterGI 编译 | 成功（`dotnet msbuild ... -clp:ErrorsOnly`，退出码 0） |
| 飞鱼 TTS | 实测出声：cute / gentle；24kHz；显存峰值 3.3GB |
| VTS | 进程运行、8001 端口开放、模型已加载；插件授权未点 |

## 就绪度

| 环节 | 状态 | 说明 |
|---|---|---|
| BetterGI 截图/键鼠/Live Hub | 可用，未实机 | 编译与 234 项测试通过；本机没有原神，无法启动真实会话 |
| 会话录制（画面+事件） | 可用 | 假 Hub 全链路通过；窗口直录只验证过桌面 |
| 会话文件格式 | 可用 | 思考/解说/动作/VTuber 参数统一时间轴 |
| 导演（LLM） | 已接 DeepSeek 并实测 | `deepseek-flash` 图像输入、思考、JSON 输出正常；假 Hub 30 秒录制 11 解说/6 思考 |
| 长期记忆 | 已接入 OpenViking | 设定/往期剧情检索；每 60 秒写回本场摘要；未启动时自动降级 |
| Lumine 操作桥 | 已部署并实测 | AutoDL 4090 上 vLLM 0.7.3 + 7B 模型已跑通；真实截图能返回合法动作，约 0.4s/帧（2.5Hz），机器已关机待原神 |
| 飞鱼 TTS | **可用** | 模型 7GB 已就位，默认 cute；批量推理 |
| 后期渲染 | **可用** | 配音对齐、字幕、混流、报告齐全 |
| VTuber 参数 | 已记录，未渲染 | 参数在 `avatar` 事件；Live2D 离线渲染尚未实现 |
| 实机联调 | 未开始 | 缺原神客户端 |

整体判断：

* **代码层面约 90%**：离线录制→成片的主干已经闭合；
* **本地可验证部分 100% 通过**（用假 Hub/mock AutoDL）；
* **AutoDL 真实模型已部署并验证**，推理约 0.4 秒/帧，未达到 5Hz 目标；
* **真实使用还差两件外部条件**：原神客户端、AutoDL 用时开机；DeepSeek
  与 OpenViking 已在本机验证。

## 现在可以怎么用

### 已经能直接用

```bash
# 任意一场已有会话 → 飞鱼 cute 配音成片
python BetterGI/integrations/session/render_session.py sessions/<会话id>

# 只验证配音
E:\BetterAGI\cosyvoice\.venv\Scripts\python.exe E:\BetterAGI\cosyvoice\feiyu_tts.py ^
    --text "你好呀" --out sample.wav
```

### 装上原神后可以完整用

1. 安装原神，确认窗口标题（默认 `原神`）。
2. 启动 BetterGI（管理员权限）。`User/config.json` 已预置：
   `agentControlEnabled/httpApiEnabled/mcpEnabled/webSocketEnabled = true`，
   端口 30648，令牌与 `tools/bettergi_token.txt` 一致。
3. 录一场小样：

```bash
cd BetterGI/integrations/session
python run_episode.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
    --duration 300 --capture window --window-title 原神
```

4. 查看与成片：

```bash
python show_session.py sessions/<会话id> --thoughts --actions
python render_session.py sessions/<会话id>
```

### 全自动 AI 游玩还需要

* **LLM 密钥**：设置 `OPENAI_API_KEY` + `OPENAI_BASE_URL` 或
  `ANTHROPIC_API_KEY`，否则导演只能用 `--no-llm`（只记日志）。
* **AutoDL**：模型与服务已部署并验证，机器当前关机。用时在 AutoDL
  控制台开机，执行
  `screen -dmS lumine bash /root/autodl-tmp/lumine/start_lumine.sh`，
  再按 `AUTODL_DEPLOY.md` 建 SSH 隧道并填 `bridge_config.json`。
* **VTS 授权**（可选）：如果要做直播层表情/口型，在 VTube Studio 弹窗点
  允许；离线成片不依赖它。

## 已知限制

1. 录制的视频默认没有游戏原声；需要时另行采集并混入。
2. 飞鱼文本前端未启用，阿拉伯数字读法可能不自然；台词尽量写中文数字。
3. 同屏解说密度过高时，渲染器会把后一句顺延，成片可能比录像长；导演
   间隔建议 ≥4 秒、每轮一条解说。
4. Live2D 只记录参数，不做离线渲染；需要额外做 VTS/浏览器渲染器。
5. 单场只验证过 20 秒级；30 分钟长场次的磁盘、队列与漂移尚未压测。
6. AutoDL 真实推理约 0.4 秒/帧（2.5Hz），低于 5Hz 目标；动作之间会有
   约 200ms 空档。先保证模型与格式正确，速度优化另开任务。

## 复现本地联调

```bash
# 1) 假 Hub + 假 LLM
python tools/pipeline_smoke/fake_stack.py --port 30648 --llm-port 18080 \
    --frame tools/pipeline_smoke/frame.jpg --fps 10

# 2) mock AutoDL
python BetterGI/integrations/lumine/autodl_server.py --mock --port 8010

# 3) 一条命令录制（另开终端）
cd BetterGI/integrations/session
LLM_PROVIDER=openai OPENAI_API_KEY=fake \
OPENAI_BASE_URL=http://127.0.0.1:18080/v1 OPENAI_MODEL=fake-director \
python run_episode.py --no-start --token smoke --duration 14 \
    --director-config E:/BetterAGI/tools/pipeline_smoke/director_smoke.json \
    --lumine-config E:/BetterAGI/tools/pipeline_smoke/bridge_smoke.json

# 4) 飞鱼成片
python render_session.py E:/BetterAGI/tools/pipeline_smoke/sessions/stage5
```

这套假服务只用于协议联调，不会产生真实游戏输入。
