# ObsFusion 验收记录

> 生成时间：2026-11-04 · 本机实测（Windows 11 build 10.0.26200，Node 24.19.0，OBS 32 已安装但未运行）

## 1. 环境

| 项 | 实测值 |
| --- | --- |
| Node | 24.19.0 |
| ffmpeg | 9.0.2-full_build-www.gyan.dev |
| SAPI 中文语音 | Microsoft Huihui Desktop |
| SAPI 英文语音 | Microsoft Zira Desktop |
| OBS | D:\SteamLibrary\steamapps\common\OBS Studio（含 obs-browser / obs-websocket） |
| obs-websocket | port=4455，enabled=**false**，auth_required=true（测试时未启用，故 OBS 侧一键建源未联机验证） |

## 2. 自动化测试

```
$ npm test
ℹ tests 47
ℹ pass 47
ℹ fail 0
ℹ todo 0
```

覆盖：时间线编译与默认值（timeline）· 工程读写与并发（store）· HTTP API 与错误路径（server-api，含 404/400）· WebSocket 会话与播放指令（ws）· OBS 资产静态校验（obs-assets，Lua 语法与场景集合字段）。

## 3. 接口实测

| 调用 | 结果 |
| --- | --- |
| GET /api/health | 200，含 `projects/viewers/clients/extensions` |
| GET /api/projects/demo/timeline | 200，6 条 cue，`duration=27.85`，video/bgm URL 为 `/api/projects/demo/media/demo.mp4` |
| GET /api/projects/nope-xyz | **404**（修复前是 500） |
| POST /api/projects（只给 title） | 201，默认 canvas 1920×1080 / subtitleSize 64 / ttsGain 1 |
| POST /api/projects/demo/tts | 生成 5 条 wav（章节标题不需要语音），命中缓存不重复合成 |
| POST /api/projects/demo/danmaku `{text:"SC 测试一下",user:"SC"}` | `SC：测试一下 → bottom-left fixed 8s #E8B05A`，matchedRule「SC 固定」 |
| POST /api/projects/demo/danmaku `{text:"来了来了",user:"主播"}` | → `top-right fixed 6s`，matchedRule「主播置顶」 |
| GET /x/chapters/chapters?scene=demo | 2 章（c1–c4 / c5–c6） |
| GET /overlay/overlay.js | 200 `text/javascript`（修复前 404，导致叠加层全黑） |
| WS /ws?scene=demo&role=overlay | hello → state → reload 全通；`transport seek/play` 生效，`ping` → `pong` |

## 4. 浏览器实测（ego 内置浏览器，视口 1264×805）

截图见 `docs/shots/`：

| 文件 | 说明 |
| --- | --- |
| control-01-overview.png | 控制台三栏总览：场景 / 扩展 / 事件流 + 实况预览 + 时间线 + 检查器 |
| control-02-cue-editor.png | 选中 cue 后的编辑表单（文本、入出点、说话人、锚点、样式、语音、卡拉 OK 开关、音频试听） |
| control-03-danmaku.png | 弹幕面板：发一条 SC，展示规则命中「SC 固定」、区域 `fixed · 第 5 轨`、坐标 `x 0.04 · y 0.87 · 8s` |
| control-04-extensions.png | 扩展面板：按 configSchema 自动生成 8 个控件 + 原始 JSON |
| control-05-obs.png | OBS 面板：连接状态、地址/密码、场景与源名、一键推送 |
| overlay-01-karaoke.png | 叠加层：字幕逐字琥珀色高亮推进（卡拉 OK） |
| overlay-02-danmaku.png | 叠加层：三条弹幕按规则落在右上/固定位，字幕居中 |
| overlay-03-chapters.png | 叠加层：章节导航条 02/02，切换章节时游标推进 |
| control-06-preview.png | 控制台实况预览：位置 3.1s，预览内字幕/播放头与时间码逐帧同步 |

## 5. 本轮修复的缺陷

1. `/api/health` 恒 500：`makeHandler` 引用未定义的 `clients` → 改为从 ctx 取。
2. `GET /api/projects/:id` / `/timeline` 工程不存在返回 500 → 统一映射 404。
3. `validateProject` 用 `clamp(undefined)` 填默认值，导致 canvas 320×240、字幕 24px、TTS 音量 0（静音）、语速 −10 → 新增 `num(value, fallback, min, max)`。
4. cue 缺 `end` 时被算成 `start+3`，与契约「start+0.1」不符 → 按契约回退。
5. `/overlay/overlay.js` 返回 404（静态分支把文件名当工程 id）→ 叠加层全黑；静态资源优先解析。
6. 字幕水平居中写在 `transform` 上，被入场动画覆盖 → 字幕贴左边；改为 `left:50%` + 运行时按实测宽度写 `margin-left`。
7. 同一锚点切换 cue 时旧元素残留 `left/top` 造成错位叠加 → 应用锚点前先清空定位与 margin。
8. 退场字幕虽然淡出但仍占排版位，与下一条叠字 → `.is-leaving` 立即 `visibility:hidden`。
9. 卡拉 OK 高亮层继承不到底层字号（0.32em 侧标继承了小字号）→ 创建时逐项拷贝字体属性，运行时按 `.line` 的实测盒子对齐。
10. 控制台预览舞台尺寸按 `100vh` 估算，最小化窗口时溢出、下方大片空白 → iframe 绝对定位不参与排版，改由 flex 分配高度。
11. `doctor` 必崩（把 `listVoices()` 返回值当对象）、OBS 路径硬编码 → 支持 `OBS_DIR` 与多个候选路径，并新增中文语音检查。
12. `--help` / `--version` 会被当成 `serve` 的选项（直接启动服务）→ 作为独立旗标处理，未知命令打印用法并返回码 1。
13. 扩展拿不到 `project.load` 事件 → 工程首次加载时向扩展广播。
14. 卡拉 OK 高亮层复制了一份文字，长句换行时与底层错位出现重影 → 改为单层 `background-clip: text` 渐变（`--p` 控制进度），并关掉会透过透明字形的 `text-shadow`、改用 `filter: drop-shadow`。
15. 控制台预览的 iframe 自带时钟，与控制器时间码漂移 → 控制器每帧 postMessage 下发 `{t,status}`，叠加层 `?preview=1` 时只按该时刻渲染（实测 00:07.2 与 cue c2 7.20s 一致）。
16. 右锚点弹幕按 `left = x*W − 估算宽` 定位会偏到画面中间 → 改用 `right = (1−x)*W` 贴右边缘向左展开；底部轨道改为向上堆叠，避免压住字幕。

## 6. 已知限制

- **OBS 一键建源在本机未联机验证**：obs-websocket 处于禁用状态。控制台的 OBS 面板、`server/obs.mjs` 的认证与建源逻辑按 obs-websocket 5.x 实现，但需要用户在 OBS 里启用 WebSocket 后实测确认。三条接入里的「浏览器源手填 URL」与「Lua 脚本」路径不依赖 WebSocket。
- TTS 走 Windows SAPI，非 Windows 平台不可用（其余功能可用）。
- 叠加层持久运行在浏览器里；OBS 源需要「当源不可见时保持运行」才不会被暂停。
- 扩展的浏览器侧运行时是原生 ESM，不支持打包器产物（`import` 语法直接用）。
