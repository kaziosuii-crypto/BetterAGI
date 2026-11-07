# ObsFusion × OBS 接入指南

> 适用：OBS Studio 30 及以上（本机实测 32.2.2）。本机 OBS 装在 D:\SteamLibrary\steamapps\common\OBS Studio。
> 本文只讲「把 ObsFusion 的 HTML 叠加层显示在 OBS 里」，服务端/项目格式见 docs/CONTRACT.md。

---

## 0. 前置条件

1. Node.js 22 或更高版本（node -v 自查）。
2. 先在项目根目录启动 ObsFusion 服务：

        node bin/obsfusion.mjs serve --port 8787

3. 用本机浏览器打开下面两个地址，确认服务正常：

        http://127.0.0.1:8787/control      控制台（编辑字幕/标题）
        http://127.0.0.1:8787/overlay/demo 叠加层（OBS 里显示的就是它）

   叠加层页面背景是透明的，直接用浏览器看会「一片白/透底」——这是正常的，放进 OBS 才看得出效果。

> 【截图位 1】终端里 node bin/obsfusion.mjs serve 启动成功的输出（含监听 127.0.0.1:8787 那一行）。

---

## 1. 方式一（推荐）：OBS Lua 脚本

脚本文件 obs/obsfusion.lua 直接放进 OBS 的脚本目录，之后所有操作都在 OBS 菜单里点。

### 1.1 一键安装

        node tools/install-obs.mjs

安装器会做这些事（全部只读检测 + 一次文件复制）：

- 找 OBS 安装目录（环境变量 OBS_PATH / Program Files / Steam 库），读日志里的 OBS 版本；
- 检测 OBS 是否正在运行；
- 检测 obs-websocket 配置是否存在、是否已启用、端口、是否需要认证（**不会打印密码**）；
- 把 obs/obsfusion.lua 复制到 %APPDATA%\obs-studio\scripts\obsfusion.lua；
  若已有同名文件，先备份成 obsfusion.lua.bak-20251104-170000 这种格式再覆盖；
- 打印后续步骤。

常用参数：

        node tools/install-obs.mjs                 # 只检测 + 安装 Lua
        node tools/install-obs.mjs --scene         # 同时安装场景集合模板（见第 2 节）
        node tools/install-obs.mjs --dry-run       # 只看检测结果，不写任何文件
        node tools/install-obs.mjs --port 8790 --project demo
        node tools/install-obs.mjs --help

> 【截图位 2】install-obs.mjs 运行完的输出（环境检测 + 「Lua 脚本已安装」）。

### 1.2 在 OBS 里加载脚本

1. OBS 菜单「工具 / Tools」→「脚本 / Scripts」；
2. 点「+」，选择：

        %APPDATA%\obs-studio\scripts\obsfusion.lua

   实际路径形如 C:\Users\<你>\AppData\Roaming\obs-studio\scripts\obsfusion.lua；
3. 加载成功后，脚本列表里出现「obsfusion.lua」，下方是脚本描述（ObsFusion v1.0.0 …）。

> 【截图位 3】OBS「脚本」窗口：脚本已加载 + 描述文字。

### 1.3 脚本属性（改端口 / 项目 id 都在这里）

| 属性 | 默认值 | 说明 |
| --- | --- | --- |
| 服务端口 | 8787 | 必须和 ObsFusion 服务的 --port 一致 |
| 场景项目 ID | demo | 对应 projects/<id>/project.json，URL 里变成 /overlay/<id> |
| 浏览器源名称 | ObsFusion Overlay | OBS「来源」里显示的名字，改名后旧源不会自动删 |
| 画布宽度 | 1920 | 浏览器源渲染宽度，建议与画布一致 |
| 画布高度 | 1080 | 同上 |
| reroute_audio | 勾选 | 把叠加层里的音频（cue 音效/视频声）路由进 OBS |

改了端口或项目 id 后，必须再点一次菜单「ObsFusion: 添加/刷新浏览器源」，源里的 URL 才会更新。

> 【截图位 4】脚本属性面板（六个字段 + 底部提示文字）。

### 1.4 添加 / 刷新叠加层

OBS 菜单「工具」→「ObsFusion: 添加/刷新浏览器源」。

- 当前场景里没有同名源：创建 browser_source 并挂到当前场景；
- 已经有同名源：更新它的设置（URL、帧率、音频路由），源会自行刷新；
- 当前场景里找不到但全局存在同名源：把它补挂到当前场景；
- 当前没有场景（或不是普通场景）：只在脚本日志里报错，不会崩。

### 1.5 三个菜单项

| 菜单项 | 作用 |
| --- | --- |
| ObsFusion: 添加/刷新浏览器源 | 往当前场景加源，或更新已有源的 URL/参数 |
| ObsFusion: 打开控制台 | 用系统浏览器打开 http://127.0.0.1:<端口>/control |
| ObsFusion: 重载叠加层 | 给 URL 追加 _r=<时间戳> 强制浏览器源重新加载页面 |

「重载叠加层」不会重建源，所以 OBS 里的位置、缩放、滤镜都保留，只刷新页面内容。

### 1.6 脚本写进浏览器源的参数

| 键 | 值 | 说明 |
| --- | --- | --- |
| url | http://127.0.0.1:<端口>/overlay/<项目 id>?obs=1 | obs=1 让页面进入 OBS 模式（透明底、不显示调试控件） |
| width / height | 1920 / 1080 | 来自脚本属性 |
| fps | 60 | 只有 fps_custom=true 时 OBS 才用它，脚本会一起置 true |
| reroute_audio | 属性值 | 音频进 OBS 混音器 |
| shutdown | false | 切换场景不销毁页面（避免重载闪烁） |
| restart_when_active | false | 切回前台不重启页面 |
| shutdown_when_not_visible | false | 旧版本字段，OBS 32 已不读，留着兼容老版本 |

> 提醒：OBS 32 里浏览器源的「隐藏时关闭」选项已合并到「关闭源时关闭」。脚本两个键都写 false，行为一致。

---

## 2. 方式二：场景集合导入

适合「新建一个干净的 OBS 配置」或「给同事一份现成模板」。

### 2.1 导入

1. 安装（会自动改名成 ObsFusion.json 并放到 OBS 的场景目录）：

        node tools/install-obs.mjs --scene

2. 或者手动导入：OBS 菜单「场景集合 / Scene Collection」→「导入 / Import」→ 选择 obs/scene-collection.template.json。

模板内容：

- 场景「ObsFusion」，1920×1080；
- 源「ObsFusion Background」：color_source 纯黑背景（**只用于预览**）；
- 源「ObsFusion Overlay」：browser_source，URL http://127.0.0.1:8787/overlay/demo?obs=1，60fps，reroute_audio 开，隐藏时不关闭。

> 【截图位 5】导入后「场景集合」菜单里出现 ObsFusion，且「来源」列表是两条。

### 2.2 两个必须知道的点

1. **切换场景集合会整体切换**：导入的是独立集合，不会覆盖你现在的场景，但切过去以后原来的场景/源要切回来才看得到。
2. **黑色背景只是预览用**：要把叠加层压在游戏/摄像头画面上时，在「来源」里把「ObsFusion Background」点掉眼睛图标（隐藏）或直接删除，否则整块画面被黑底盖住。

URL 里的端口和项目 id 要和实际服务一致；用 --port / --project 安装时安装器会自动改写模板里的 URL。

---

## 3. 方式三：手动添加浏览器源

不用脚本、不用模板，OBS 原生操作：

1. 「来源」面板 → 「+」→「浏览器 / Browser」；
2. 名称填 ObsFusion Overlay；
3. 勾选「本地文件」**不要勾**（走 URL）；
4. URL：

        http://127.0.0.1:8787/overlay/demo?obs=1

5. 宽度 1920、高度 1080；
6. 勾选「自定义帧率」填 60（不勾会跟随画布帧率）；
7. 勾选「通过 OBS 控制音频」（= reroute_audio）；
8. 「关闭源时关闭浏览器」取消勾选；「页面变为可见时刷新」取消勾选；
9. 在「自定义 CSS」里保留透明背景（一般默认就是）：

        body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }

> 【截图位 6】浏览器源属性窗口，红框标出 URL / 宽高 / 自定义帧率 / 通过 OBS 控制音频。

---

## 4. obs-websocket（自动控制 / 自动安装说明）

- OBS 28 起 obs-websocket **已内置**，不需要单独装插件；本机 OBS 32.2.2 自带。
- 默认是**关闭**的，需要手动开启一次：OBS 菜单「工具 / Tools」→「WebSocket 服务器设置 / WebSocket Server Settings」→ 勾选「启用 WebSocket 服务器」，端口保持 4455。
- 密码：OBS 默认「启用认证」并生成随机密码，配置写在

        %APPDATA%\obs-studio\plugin_config\obs-websocket\config.json

  字段：server_enabled / server_port / auth_required / server_password。
- ObsFusion 的自动安装/自动控制会读取这个配置文件拿端口和密码，**不会把密码打印到日志或控制台**；
  安装器只会告诉你「密码已设置 / 未设置」，便于排查。
- 检查是否真的起来了：OBS 菜单「帮助 → 日志 → 查看当前日志」，搜 WebSocket server started。

> 【截图位 7】OBS「WebSocket 服务器设置」窗口，勾选启用 + 端口 4455。

---

## 5. 常见问题

### 5.1 黑屏 / 一片空白 / 显示 ObsFusion 连不上

按下表从上往下排：

1. 服务是否在跑：浏览器打开 http://127.0.0.1:8787/api/health，应返回 JSON；
2. 端口是否一致：脚本属性里的端口、服务 --port、URL 里的端口三处必须相同；
3. 项目 id 是否拼错：/overlay/demo 里的 demo 要对应 projects/demo/project.json；
4. 在 OBS 里右键该源 →「刷新页面」（或在「工具」菜单点「ObsFusion: 重载叠加层」）；
5. 还是黑：OBS 菜单「工具 → 脚本」看脚本日志，或「帮助 → 日志 → 查看当前日志」搜 ObsFusion。

### 5.2 没有声音

- 脚本属性里 reroute_audio 要勾选，改完重新点「添加/刷新浏览器源」；
- 浏览器源创建时若没开 reroute_audio，之后改属性不会自动加分轨，删掉源重加最省事；
- OBS「混音器」里看目标源有没有音量条；Windows 音量合成器里 OBS 是否被静音；
- 浏览器对自动播放有限制：ObsFusion 的 ?obs=1 页面首帧会尝试静音播放音频，用户交互后才出声。若 cue 音效没声，在控制台点一次播放按钮或勾选「允许自动播放」。

### 5.3 透明背景变成黑底 / 白底

- 浏览器源「自定义 CSS」里的 body 背景必须是 rgba(0,0,0,0)（脚本和模板都已设置）；
- 场景里不要放不透明底图；用了模板的「ObsFusion Background」时必须隐藏它；
- 源顺序：「ObsFusion Overlay」要在背景源之上（OBS 里越靠上越后面绘制）；
- 叠加层页面本身有底色的话，改 projects/<id>/project.json 的 theme，别在 OBS 里硬压。

### 5.4 端口被占用（8787）

        netstat -ano | findstr :8787
        taskkill /PID <上面查到的 PID> /F

或者换端口（两处都要改）：

        node bin/obsfusion.mjs serve --port 8790
        # 然后在 OBS 脚本属性里把「服务端口」改成 8790，再点「添加/刷新浏览器源」

### 5.5 字体 / 中文显示成方框

- 叠加层走系统字体栈，缺字时给 Windows 装思源黑体 / 微软雅黑即可；
- 字号由项目文件里的 theme.subtitleSize（默认 64）控制，不是 OBS 的字体设置；
- OBS 里缩放源会让文字模糊：优先改画布/字号，不要靠放大源。

### 5.6 画面卡顿 / 掉帧

- 浏览器源帧率设为 60 足够；4K 画布或复杂动效时降到 30；
- 「关闭源时关闭浏览器」勾上会省内存，但切场景会重新加载页面（有闪烁），按需取舍。

### 5.7 脚本加载报错 attempt to call a nil value (field 'obs_frontend_add_tools_menu_item')

- OBS 版本过旧（< 30）或前端 API 未暴露，升级 OBS；
- 本脚本对菜单注册做了 pcall 兼容，真报错说明 OBS 环境异常，先重启 OBS 再试。

---

## 6. 文件与命令速查

| 文件 | 作用 |
| --- | --- |
| obs/obsfusion.lua | OBS Lua 脚本：添加/刷新浏览器源、打开控制台、重载叠加层 |
| obs/scene-collection.template.json | 可导入 OBS 的场景集合模板（1920×1080 + 黑色背景） |
| tools/install-obs.mjs | 零依赖安装器：复制脚本、可选装场景集合、环境检测 |
| docs/OBS_SETUP.md | 本文 |
| test/obs-assets.test.mjs | 静态校验：模板 JSON、Lua 结构、安装器语法 |

    node bin/obsfusion.mjs serve --port 8787     # 启动服务
    node tools/install-obs.mjs                   # 装 Lua 脚本
    node tools/install-obs.mjs --scene           # 装 Lua + 场景集合
    node tools/install-obs.mjs --dry-run         # 只检测
    node --test                                  # 跑测试（Node 24 下不要带 test/ 参数）
