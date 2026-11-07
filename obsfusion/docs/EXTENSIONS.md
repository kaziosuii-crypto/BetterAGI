# 扩展开发指南

ObsFusion 里**每一个画面元件都是扩展**：字幕、弹幕、水印、角标都走同一套接口。本篇写给"想写第三个扩展"的你，行为与源码一致（`server/extensions.mjs`、`web/shared/runtime.js`）。架构与全部 HTTP/WS 契约见 [`docs/CONTRACT.md`](./CONTRACT.md)。

## 1. 扩展是什么

一个扩展是 `extensions/<id>/` 下的三个文件：

| 文件 | 必需 | 运行环境 | 职责 |
| --- | --- | --- | --- |
| `manifest.json` | 是 | 服务端读取 | 身份、入口、默认配置、控制台表单 schema |
| `server.mjs` | 否 | Node 22，无 DOM | 能力链、事件、HTTP 路由、广播——**做决策** |
| `client.js` | 否 | OBS 浏览器源，无 fs/Node | 图层、渲染、动效——**做画面** |

边界一句话：**服务端决定"出现在哪、长什么样"，浏览器端决定"怎么画出来"**；两边不共享内存，只通过 REST 与 WebSocket 通信。只写 client 能在画面上加东西，只写 server 能接外部推送，但 manifest 必须存在。

加载链路：

1. `serve` 启动时扫描 `extensions/*`（只认目录），读 `manifest.json`；**没有 `id` 的整条跳过**并打印 `[ext] load failed`。有 `manifest.server` 就 `import()`（带 `?v=<时间戳>` 破缓存），导出 `setup` 是函数则 `await setup(api)`；import/setup 抛错只写进该扩展的 `error` 字段，不影响别人。
2. 打开 `/overlay/<project>` 时前端拉 `GET /api/projects/<id>/timeline`，其中 `extensions` 只含 `enabled && client` 的项，运行时逐个注入脚本，脚本顶部调 `ObsFusion.registerExtension`。
3. 配置优先级：`manifest.defaultConfig` ←（深合并）`data/extensions.json` 中持久化的 `config`；`enabled` 缺省 `true`。

## 2. manifest.json

```json
{
  "id": "watermark", "name": "水印层 Watermark", "version": "1.0.0",
  "description": "右上角常驻水印，文字取自当前工程标题。",
  "client": "client.js", "server": "server.mjs",
  "defaultConfig": { "text": "ObsFusion", "opacity": 0.55 },
  "configSchema": { "fields": [] }
}
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 必填 | 唯一标识，也是 `/x/<id>/*` 的路径段与配置持久化 key |
| `name` | string | `id` | 控制台显示名 |
| `version` | string | `"0.0.0"` | 仅展示，不做兼容检查 |
| `description` | string | `""` | 控制台副标题 |
| `client` | string | 无 | 浏览器脚本相对路径；缺失则不进入叠加层 |
| `server` | string | 无 | 服务端模块相对路径；缺失则无 api/路由/事件 |
| `defaultConfig` | object | `{}` | 默认配置，与持久化配置深合并 |
| `configSchema` | object | `null` | `{ fields: [...] }`，控制台据此生成表单 |

每个 field 的键：`key`（必需）、`label`、`type`、`options`、`min`、`max`、`step`。

```json
[
  { "key": "speed", "label": "滚动时长（秒）", "type": "number", "min": 2, "max": 30, "step": 0.5 },
  { "key": "area", "label": "默认区域", "type": "select", "options": ["top", "bottom", "full"] },
  { "key": "avoidSubtitles", "label": "避开字幕区", "type": "boolean" },
  { "key": "greeting", "label": "欢迎语", "type": "text" }
]
```

控制台按 type 渲染：`boolean` → 开关按钮（已开启/已关闭）；`select` → 下拉框，选项来自 `options`；`number` → 数字框，带 `min`/`max`，`step` 缺省 1，非法输入回落 0；**其它（含 `text`、省略 type）→ 单行文本框**。点「应用配置」发 `PUT /api/extensions/<id>`（body `{ config, enabled }`）；服务端**不会重跑 setup**，要响应变化就导出 `onConfigChange`：

```js
export default {
  setup(api) { /* ... */ },
  onConfigChange(config, api) { api.log('info', 'config -> ' + JSON.stringify(config)); }
};
```

## 3. 服务端 api 参考

模块形状 `export default { setup(api) { ... }, onConfigChange?(config, api) { ... } }`；`setup` 在 import 后 await 调用，只有一次。

| 成员 | 签名 | 说明 | 一行用法 |
| --- | --- | --- | --- |
| id / enabled | `api.id`、`api.enabled` | 扩展 id、是否启用 | ``api.log('info', api.id + ' enabled=' + api.enabled)`` |
| config | `api.config` | 当前配置（已深合并） | ``var lanes = api.config.lanes ?? 6;`` |
| project / sceneId | `api.project`、`api.sceneId` | 最近加载的工程与其 id，可能为 `null` | ``api.project && api.project.title`` |
| dataDir | `api.dataDir` | `data/extensions/<id>`，**不会自动创建** | ``await fs.mkdir(api.dataDir, { recursive: true })`` |
| log | `api.log(level, message)` | `error`/`warn`/其它，前缀 `[ext:<id>]` | ``api.log('warn', 'no config yet')`` |
| route | `api.route(method, path, handler)` | 挂到 `/x/<id><path>` | ``api.route('POST', '/send', fn)`` |
| on | `api.on(event, handler)` | 订阅服务端事件，handler 收 `(payload, api)` | ``api.on('project.save', onSave)`` |
| broadcast | `api.broadcast(type, payload, sceneId?)` | 向叠加层推 WS `event` 帧 | ``api.broadcast('notify', { text: 'hi' }, api.sceneId)`` |
| capability | `api.capability(name, fn)` | 加入一条能力链 | ``api.capability('danmaku.route', fn)`` |
| getState / setState | `api.getState()`、`api.setState(patch)` | 本扩展私有状态，`setState` 为浅合并 | ``api.setState({ n: (api.getState().n ?? 0) + 1 })`` |

route 的真实语义：

- 挂载：`api.route('POST', '/send', fn)` → `POST http://127.0.0.1:8787/x/<id>/send`。
- **精确匹配**：`path` 与请求子路径逐字符比较，没有通配、没有 `:param`，query 不参与；method 自动转大写。
- 只有 `POST`/`PUT`/`PATCH` 才解析 JSON body，失败回 400。
- handler 收到 ctx `{ sceneId, project, config, query, body, req, res }`，另附 `(req, res, body)` 位置参数；返回值 `!== undefined` 且未发头 → `200 JSON`，抛错 → `500 {"error":"..."}`，没命中 → `404 {"error":"route not found","path":"..."}`。要自己写响应就用 `ctx.res`，写完别再返回。
- ⚠️ `/x/*` 上 `ctx.project` **恒为 null**（`server/index.mjs` 只传 `{ sceneId, project: null }`），`sceneId` 来自 `?scene=<id>`；要工程数据自己 `fetch('/api/projects/' + ctx.sceneId)`。

```js
api.route('POST', '/ping', function (ctx) {
  var who = ctx.query.who || 'anon';   // query 已解析为对象
  ctx.body;                            // 已 JSON.parse
  return { ok: true, who: who };       // 返回值 → 200 application/json
});
```

## 4. 事件表

服务端（`api.on`）：

| 事件 | 触发时机 | payload |
| --- | --- | --- |
| `project.save` | `PUT /api/projects/:id` 保存成功后 | `{ sceneId, project, timeline }` |
| `state` | `POST /api/projects/:id/transport` 之后（**仅 HTTP**，WS transport 不触发） | `{ sceneId, state: { status, time } }` |
| `danmaku.raw` | `POST /api/projects/:id/danmaku` 映射并广播之后 | `{ sceneId, raw, mapped }` |
| `overlay.event` | 叠加层发来 WS `{ type:'event', name, payload }` | `{ sceneId, name, payload }` |

handler 返回被忽略；单个 handler 抛错只收集、不影响其它扩展；`enabled=false` 收不到。⚠️ 契约里写过的 `server.start` 与 `project.load` **没有任何 emit 点**：初始化放 `setup()`，要工程就 `GET /api/projects/:id`。

浏览器端（`ctx.on`）：

| 事件 | 触发时机 | payload |
| --- | --- | --- |
| `tick` | 每个 rAF 帧 | `{ time, status }`，status 为 `idle`/`playing`/`paused` |
| `cue.enter` | 字幕进入显示区间 | `{ cue, time }` |
| `cue.exit` | 离开区间（**不是** `cue.leave`） | `{ cue, time }` |
| `timeline.ready` | 时间线拉取成功（每次 reload 都触发） | 完整 timeline |
| `extensions.ready` | 所有 client 脚本 load/error 结束 | `extList` 数组 |
| WS `event` 名 | 服务端 `api.broadcast` 或 danmaku 接口 | 原样 payload（`danmaku.add`、`tts.progress` 等） |

页面收到 WS `reload` 会自己重拉时间线并重注入脚本，**扩展无需监听 reload**。

## 5. 能力链

`ExtensionHost.runCapability(name, ctx, seed)`：取该名字下所有注册（按 `api.capability` 调用顺序）→ `value = seed`，依次 `await fn(ctx, value)`，**上一个的返回值就是下一个的输入** → 某环抛错只记日志并保留上一值，链条继续 → 被禁用或已 reload 的扩展跳过。意义：多个扩展协作处理同一件事，也可中途改写、拦截。

内置只有一条链 `danmaku.route`，唯一入口 `POST /api/projects/:id/danmaku`：

```text
seed   = 请求 body   { text, user, color, mode, size, level }
ctx    = { sceneId, project, config }     // config = 扩展默认配置 深合并 工程内该扩展配置
return = 映射对象     { id, text, user, color, size, zone, mode, lane, lanes,
                       x, y, duration, speed, opacity, avoidSubtitles, matchedRule }
```

- 返回值缺失或没有 `zone` 时服务端兜底为 `zone:'top'`、`mode:'scroll'`、`color:'#F4F1EA'`、`lane:0`、`lanes:6`；
- 之后固定两件事：WS 广播 `danmaku.add`（payload 即映射结果），再 emit `danmaku.raw`；
- 内置 `extensions/danmaku` 忽略传入 value、直接返回自己的映射。**要链式改写就基于传入 value 改字段再返回**（§8 配方①）。

## 6. 浏览器端运行时

```js
ObsFusion.registerExtension({ id: 'watermark', setup: function (ctx) { /* ... */ } });
```

`def` 必须有 `id` 且 `setup` 是函数，否则 `console.warn` 后丢弃；`setup` **立即同步执行**，抛错只 `console.error` 不影响页面。脚本顶部包一层 `if (!window.ObsFusion) return;`。其它全局成员：`version`、`extensions`、`on`/`emit`、`state`、`setState`、`setCanvas`、`layers.create`/`layers.zone`、`loadScripts`、`utils`。

| ctx 成员 | 说明 |
| --- | --- |
| `id` / `root` | 扩展 id；`#stage`（不存在则 `body`） |
| `project` / `timeline` / `theme` | 当前工程、编译后时间线、`project.theme`；前两者可能为 null |
| `canvas` | `{ width, height }`，默认 1920×1080 |
| `confirm(msg)` | 包装 `window.confirm`，无实现时返回 true |
| `layers.create(opts)` / `layers.zone(name)` / `layers.get(id)` | 建图层、归一化坐标、按 id 取图层（没有返回 null） |
| `on(name, fn)` | 订阅，**返回取消订阅函数** |
| `emit(name, payload)` | 发给本页其它监听者 |
| `utils` | clamp/lerp/easeOut/cssVar/formatTime/pick |
| `getState()` | `{ time, status, revision }` |

⚠️ **ctx 上没有 `config`**：要读自己配置就 `fetch('/api/extensions')` 找到 `id` 对应项。

`ctx.layers.create({ id, z, className, pointerEvents })` 创建 `div.of-layer` 挂到 `#stage`：`id` 相同**直接复用**（幂等）；`z` 缺省 50；`pointerEvents` 只有显式 true 才 `auto`，默认 `none`。`ctx.layers.zone(name)` 的 9 个键（乘画布宽高即像素，未知回落 `center`）：`top` .5/.14、`top-left` .06/.14、`top-right` .94/.14、`mid-left` .06/.5、`center` .5/.5、`mid-right` .94/.5、`bottom` .5/.86、`bottom-left` .06/.86、`bottom-right` .94/.86。

`utils`：`clamp(n,min,max)`、`lerp(a,b,t)`、`easeOut(t)=1-(1-t)^3`、`cssVar(name,fallback)`、`formatTime(sec)` → `m:ss.s`、`pick(list,index)=list[index%len]`。

### 完整最小示例

`extensions/clock-bug/client.js`——左下角显示时间码与播放状态：

```js
(function () {
  if (!window.ObsFusion) return;
  ObsFusion.registerExtension({
    id: 'clock-bug',
    setup: function (ctx) {
      var layer = ctx.layers.create({ id: 'clock-bug', z: 50 });
      layer.style.cssText = 'position:absolute;inset:0;pointer-events:none;';
      var el = document.createElement('div');
      el.style.cssText = 'position:absolute;left:48px;bottom:44px;font:500 22px/1 ui-monospace,monospace;color:#E9ECF2;opacity:.55;';
      layer.appendChild(el);
      ctx.on('tick', function (s) { el.textContent = ctx.utils.formatTime(s.time) + ' · ' + s.status; });
      ctx.on('cue.enter', function (e) { el.dataset.cue = e.cue.id; el.style.opacity = '0.9'; });
      ctx.on('cue.exit', function () { el.style.opacity = '0.55'; });
    }
  });
})();
```

## 7. 调试

- **安全区**：打开 `/overlay/demo?debug=1`，`.of-stage` 加 `debug` 类，`.of-safearea`（z 90，inset 5%）边框由 `display:none` 变为 `block`；底部 ticker 是常显的（z 40），不用 debug 也在。
- **WS 帧**：Chrome 打开同一 URL → DevTools → Network → WS → `/ws?scene=demo&role=overlay`，看 `hello`/`state`/`event` 的大小与内容。
- **服务端日志**：`api.log` 打到运行 `serve` 的终端，前缀 `[ext:<id>]`；`error`/`warn` 分别走 console.error/warn。
- **加载错误**：`GET /api/extensions` 里该扩展的 `error` 字段（import 失败、setup 抛错、onConfigChange 抛错）；路由运行期异常不写入，直接 500。
- **只重载服务端**：`curl -X POST http://127.0.0.1:8787/api/extensions/<id>/reload`；改 `client.js` 则刷新浏览器源即可，改 `server.mjs` 必须重载或重启 serve。
- **手调路由**：`curl -X POST "http://127.0.0.1:8787/x/danmaku/send?scene=demo" -H "Content-Type: application/json" -d '{"text":"hi"}'`。

## 8. 三个照着做的配方

### ① 按关键词染色（改能力链）

`extensions/tint/server.mjs`：拿到上一环的映射结果，只改颜色与呈现；没命中原样放行。注册顺序就是执行顺序——`danmaku` 先注册，`tint` 就拿得到它算好的 `zone/x/y`。

```js
export default {
  setup(api) {
    api.capability('danmaku.route', function (ctx, value) {
      if (!value || !value.text) return value;
      if (/翻车|寄了|哈哈哈/.test(value.text)) {
        return Object.assign({}, value, { color: '#FF6B6B', mode: 'fixed', duration: 6 });
      }
      return value;
    });
    api.log('info', 'tint ready');
  }
};
```

### ② 右上角自绘水印层（client 图层 + config）

`extensions/watermark/client.js`：配置从接口读，图层 z 落在字幕之上、安全区之下。

```js
(function () {
  if (!window.ObsFusion) return;
  ObsFusion.registerExtension({
    id: 'watermark',
    setup: function (ctx) {
      var layer = ctx.layers.create({ id: 'watermark', z: 45 });
      layer.style.cssText = 'position:absolute;inset:0;pointer-events:none;';
      var box = document.createElement('div');
      box.style.cssText = 'position:absolute;right:56px;top:44px;font:600 26px/1 "Microsoft YaHei UI",sans-serif;color:#E8B05A;opacity:.55;letter-spacing:.12em;';
      layer.appendChild(box);
      function paint(text, opacity) {
        box.textContent = String(text || 'ObsFusion').slice(0, 40);
        box.style.opacity = opacity == null ? '.55' : String(opacity);
      }
      paint((ctx.project && ctx.project.title) || 'ObsFusion');
      fetch('/api/extensions').then(function (r) { return r.json(); }).then(function (data) {
        var me = (data.extensions || []).filter(function (e) { return e.id === 'watermark'; })[0];
        if (me && me.config) paint(me.config.text, me.config.opacity);
      }).catch(function () {});
      ctx.on('timeline.ready', function () { paint((ctx.project && ctx.project.title) || 'ObsFusion'); });
    }
  });
})();
```

### ③ 新增 `/x/<id>/notify` 路由供外部程序推送

`extensions/notify/server.mjs`：HTTP 进、WS 出；前端用 `ctx.on('notify', fn)` 接，或用已有的弹幕层显示 `danmaku.add`。

```js
export default {
  setup(api) {
    api.route('POST', '/notify', function (ctx) {
      var text = String((ctx.body && ctx.body.text) || '').slice(0, 80).trim();
      if (!text) return { ok: false, error: 'text is required' };
      api.broadcast('notify', { text: text, level: String((ctx.body && ctx.body.level) || 'info') }, ctx.sceneId);
      api.log('info', 'notify -> ' + text);
      return { ok: true, text: text, sceneId: ctx.sceneId };
    });
  }
};
```

外部调用要带 `?scene=<工程 id>`，否则广播给"当前场景"（通常是 null）：

```powershell
curl.exe -X POST "http://127.0.0.1:8787/x/notify/notify?scene=demo" -H "Content-Type: application/json" -d '{"text":"下半场 5 分钟后开始","level":"warn"}'
```

## 9. 常见坑

1. **`client.js` 每次加载都重新执行。** 页面 reload、时间线刷新都会重注入脚本（带 `?v=`），`setup` 会再跑一遍。必须幂等：style 用固定 `id` 判重、图层靠 `layers.create` 的 id 复用、事件只用 `ctx.on`（返回取消函数）而不是往 `window` 挂监听。
2. **别用 `innerHTML` 拼外部文本。** 弹幕/推送都是外部输入，一律 `textContent` 或 `createElement`+`appendChild`。
3. **别在 `tick` 里做重活。** 它每帧都跑，DOM 读写要合并，循环里别反复 `getBoundingClientRect`。
4. **图层 z 值**：真实代码是媒体 1、遮罩 5、字幕 30、bug/ticker 40、进度 45、弹幕扩展层 60、安全区 90；契约文档写的"媒体 10 / 弹幕 40 / 字幕 60"与实现不符。建议区间：水印角标 40-55，弹幕 60，调试层 85-90。
5. **容忍 `project` 为空。** `/x/*` 上恒为 null，叠加层 `hello` 之前也没有工程；先判空再取字段。`dataDir` 也不会自动创建，写文件前自己 `mkdir(recursive: true)`。
6. **路由精确匹配**，不支持 `/item/:id`，需要 id 就用 `?id=`。
7. **配置变了浏览器端不知道。** `PUT /api/extensions/<id>` 只更新服务端并调 `onConfigChange`；要画面跟着变，自己在里面 `api.broadcast('config.changed', config)` 再由 client 监听。

MIT · 零第三方运行时依赖 · 接口以仓库源码为准
