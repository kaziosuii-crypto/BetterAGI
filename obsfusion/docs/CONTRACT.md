# ObsFusion 架构与接口契约（v1）

> 本文件是实现基线。前端、服务端、扩展、OBS 集成都必须遵守这里的路径与数据结构。

## 1. 目录职责

| 路径 | 职责 |
| --- | --- |
| `bin/obsfusion.mjs` | CLI：serve / demo / tts / doctor / obs:install |
| `server/` | 零依赖 Node ESM 服务：HTTP、WebSocket、项目存储、时间线编译、TTS、扩展注册、OBS 控制 |
| `web/overlay/` | 给 OBS 浏览器源使用的实例（透明背景，1920x1080） |
| `web/control/` | 控制台（浏览器 / OBS 自定义 docks） |
| `extensions/<id>/` | 扩展包：manifest.json + server.mjs + client.js |
| `projects/<id>/project.json` | 结构化文案工程 |
| `projects/<id>/media/` | 视频/背景 BGM 等素材 |
| `projects/<id>/assets/tts/` | 自动生成的 TTS wav + manifest.json |
| `obs/` | OBS Lua 脚本、场景集合模板 |
| `tools/` | 安装与演示素材生成脚本 |
| `test/` | node:test 测试 |

## 2. 运行方式

服务默认监听 127.0.0.1:8787，所有页面与 API 同源。

- 叠加层（OBS 浏览器源）：`/overlay/<projectId>`
- 控制台：`/control`（可选 `?dock=1` 紧凑模式）
- 健康检查：`GET /api/health`
- WebSocket：`/ws?scene=<projectId>&role=overlay|control`

## 3. project.json

    {
      "version": 1,
      "id": "demo",
      "title": "ObsFusion Demo",
      "canvas": { "width": 1920, "height": 1080 },
      "video": {
        "src": "media/demo.mp4",
        "loop": true,
        "muted": true,
        "fit": "cover",
        "opacity": 1
      },
      "audio": { "ttsGain": 1.0, "bgm": null },
      "theme": { "accent": "#E8B05A", "subtitleSize": 64 },
      "sync": { "mode": "audio", "tailPadding": 0.25 },
      "cues": [
        {
          "id": "c1",
          "start": 1.0,
          "end": 4.0,
          "text": "当文字得到时间，它就不再只是文字。",
          "speaker": "解说",
          "anchor": "bottom-center",
          "style": "subtitle",
          "karaoke": true,
          "tts": { "enabled": true, "voice": "Microsoft Huihui Desktop", "rate": 0, "volume": 100 }
        }
      ],
      "extensions": [
        { "id": "danmaku", "enabled": true, "config": { } }
      ],
      "meta": { "createdAt": "...", "updatedAt": "..." }
    }

字段说明：

- `sync.mode`：`audio` 时字幕展示到 TTS 实际播完（`displayEnd = max(end, start + audioDuration + tailPadding)`）；`strict` 时严格按 `end`。
- `anchor`：`bottom-center` | `bottom-left` | `center` | `top-center` | `top-left` | `top-right`（可加 `x`/`y` 归一化偏移）。
- `style`：`subtitle` | `title` | `caption` | `quote`。
- `video.src` 为空表示无视频，仅文字层。
- 视频与音频 URI 一律相对 `projects/<id>/`，服务端负责转成可访问 URL。

## 4. 编译后的时间线（GET /api/projects/:id/timeline）

    {
      "project": { ...原样 project（去掉服务端字段）... },
      "revision": 3,
      "duration": 24.5,
      "cues": [
        {
          "id": "c1", "start": 1, "end": 4, "displayEnd": 5.2,
          "text": "...", "speaker": "解说", "anchor": "bottom-center",
          "style": "subtitle", "karaoke": true,
          "audio": { "url": "/api/projects/demo/assets/tts/c1.wav", "duration": 3.2, "voice": "..." }
        }
      ],
      "video": { "url": "/api/projects/demo/media/demo.mp4", "loop": true, "muted": true, "fit": "cover", "opacity": 1 },
      "extensions": [
        { "id": "danmaku", "enabled": true, "config": { }, "client": "/extensions/danmaku/client.js", "version": "1.0.0" }
      ],
      "now": 171234.5
    }

## 5. 传输控制

`POST /api/projects/:id/transport`，body：

    { "action": "play" | "pause" | "seek" | "stop", "time": 12.5 }

场景状态：`{ status: "idle"|"playing"|"paused", time: 12.5, updatedAt: 1712345678000, revision: 3 }`。

## 6. WebSocket 协议

服务端 → 客户端：

- `{ type:"hello", role, sceneId, timeline, state, now }`
- `{ type:"state", sceneId, state, now }`
- `{ type:"event", sceneId, name, payload, now }`
- `{ type:"reload", sceneId, revision }`
- `{ type:"log", level, message, at }`

客户端 → 服务端：

- `{ type:"event", name, payload }`（叠加层可上报 `cue.enter` 等，用于联动/调试）
- `{ type:"ping" }` → `{ type:"pong", now }`

## 7. 扩展（服务端）

扩展目录 `extensions/<id>/`：

- `manifest.json`：id/name/version/description/client/server/configSchema/defaultConfig
- `server.mjs`：`export default { setup(api) { ... }, onConfigChange(config, api) {} }`
- `client.js`：浏览器端（叠加层加载）

service api：

- `api.id` / `api.config` / `api.project` / `api.sceneId` / `api.dataDir`
- `api.route(method, path, handler)` → 挂载在 `/x/<id><path>`；handler(ctx, req, res, body)
- `api.on(event, fn)`：`server.start` | `project.load` | `project.save` | `state` | `danmaku.raw` | `overlay.event`
- `api.broadcast(type, payload)`：向该场景所有 WS 客户端发事件
- `api.capability(name, fn)`：注册能力链，已有 `danmaku.route`
- `api.setState(obj)` / `api.getState()` / `api.log(level, msg)`

客户端扩展运行时：

    ObsFusion.registerExtension({
      id: 'danmaku',
      setup(ctx) {
        var layer = ctx.layers.create({ id: 'danmaku', z: 40 });
        ctx.on('danmaku.add', function (msg) { /* 在 layer 中渲染 */ });
        ctx.on('tick', function (s) { /* s.time */ });
        ctx.on('cue.enter', function (s) { /* s.cue */ });
      }
    });

ctx：`root` / `project` / `timeline` / `layers.create` / `layers.zone(name)` / `on(name, fn)` / `emit` / `getState()` / `theme` / `utils`。

## 8. 内置扩展：danmaku

`POST /api/projects/:id/danmaku`，body：

    { "text": "来了来了", "user": "观众A", "color": "#ffffff", "mode": "scroll", "size": 34, "level": 6 }

映射配置：

    {
      "lanes": 6,
      "area": "top",
      "speed": 8,
      "routes": [
        { "when": { "user": "主播" }, "target": { "zone": "top-right", "mode": "fixed", "duration": 5 } },
        { "when": { "regex": "^SC" }, "target": { "zone": "bottom-left", "mode": "fixed", "duration": 6 } }
      ],
      "defaultTarget": { "zone": "top", "mode": "scroll", "duration": 8 }
    }

能力链 `danmaku.route` 的输出：

    { zone, mode, lane, x, y, duration, color, size, matchedRule }

服务端广播事件 `danmaku.add`，客户端扩展负责动画与避让字幕。

## 9. 设计基调（控制台与叠加层）

- 深色广播台质感：墨黑底 + 琥珀金强调色 + 冷银文字，玻璃面板、细网格、克制的辉光。
- 叠加层必须透明背景，所有 UI 元素都在 1920x1080 安全区内，不遮挡字幕与人物主体。
- 中文优先字体：Microsoft YaHei UI / 思源黑体；标题可衬线。
- 所有动效尊重 `prefers-reduced-motion`。
