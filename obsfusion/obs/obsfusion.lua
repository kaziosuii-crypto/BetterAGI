-- =============================================================================
-- ObsFusion for OBS —— 浏览器源叠加层集成脚本（OBS 30+ / Lua 5.1）
--
-- 作用：
--   1) 往「当前场景」里添加或刷新 ObsFusion 浏览器源
--      URL 形如 http://127.0.0.1:8787/overlay/demo?obs=1
--   2) 打开 ObsFusion 控制台
--   3) 重载叠加层（改写 URL 让浏览器源重新加载页面）
--
-- 用法：
--   1. 先启动服务：node bin/obsfusion.mjs serve
--   2. OBS 菜单「工具 / Tools」->「脚本 / Scripts」->「+」添加本文件
--   3. 在脚本属性里确认端口与项目 id
--   4. 菜单「工具」->「ObsFusion: 添加/刷新浏览器源」
-- =============================================================================

obs = obslua

local SCRIPT_NAME = "ObsFusion"
local SCRIPT_VERSION = "1.0.0"
local PREFIX = "[ObsFusion] "

-- 运行时配置，由 script_properties / script_update / script_load 维护
local conf = {
  port = 8787,
  project = "demo",
  source_name = "ObsFusion Overlay",
  width = 1920,
  height = 1080,
  reroute_audio = true,
  fps = 60,
}

-- 菜单只注册一次，避免 script_load / script_update 重复调用导致重复菜单项
local menus_registered = false

-- ---------------------------------------------------------------- 基础工具

-- 统一日志：优先使用 OBS 注入的 script_log，退化到 obs.script_log，再退化到 print
local function log(level, msg)
  local fn = rawget(_G, "script_log") or (obs and obs.script_log)
  local text = PREFIX .. tostring(msg)
  if type(fn) == "function" then
    fn(level, text)
  else
    print(text)
  end
end

local function log_info(msg)
  log(obs.LOG_INFO, msg)
end

local function log_warn(msg)
  log(obs.LOG_WARNING, msg)
end

local function log_error(msg)
  log(obs.LOG_ERROR, msg)
end

-- 整数取值并夹到合法区间
local function clamp_int(value, min_v, max_v, fallback)
  local n = tonumber(value)
  if n == nil then
    return fallback
  end
  n = math.floor(n)
  if n < min_v then
    return min_v
  end
  if n > max_v then
    return max_v
  end
  return n
end

-- 去掉首尾空白
local function trim(text)
  local s = tostring(text or "")
  return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

-- 最小百分号转义：只保留 URL 安全字符，中文按 UTF-8 字节逐个转义
local function url_escape(text)
  return (tostring(text or ""):gsub("[^%w%-%._~]", function(c)
    return string.format("%%%02X", string.byte(c))
  end))
end

-- 叠加层地址；extra 形如 "_r=1234"，自动接在 ?obs=1 之后
local function overlay_url(extra)
  local base = string.format(
    "http://127.0.0.1:%d/overlay/%s?obs=1",
    conf.port,
    url_escape(conf.project)
  )
  if extra ~= nil and extra ~= "" then
    return base .. "&" .. extra
  end
  return base
end

local function control_url()
  return string.format("http://127.0.0.1:%d/control", conf.port)
end

-- 浏览器源的 settings
local function build_browser_settings(url)
  local data = obs.obs_data_create()

  obs.obs_data_set_string(data, "url", url)
  obs.obs_data_set_bool(data, "reroute_audio", conf.reroute_audio)
  obs.obs_data_set_int(data, "width", conf.width)
  obs.obs_data_set_int(data, "height", conf.height)
  -- 注意：OBS 只有在 fps_custom 为 true 时才使用 fps 字段，否则跟随视频帧率
  obs.obs_data_set_int(data, "fps", conf.fps)
  obs.obs_data_set_bool(data, "fps_custom", true)
  -- 不隐藏时不关机 / 不因切回前台而重启
  obs.obs_data_set_bool(data, "shutdown_when_not_visible", false)
  obs.obs_data_set_bool(data, "shutdown", false)
  obs.obs_data_set_bool(data, "restart_when_active", false)

  return data
end

-- ---------------------------------------------------------------- 业务动作

-- 添加 / 刷新浏览器源到当前场景
local function action_add_or_refresh()
  local scene_source = obs.obs_frontend_get_current_scene()
  if scene_source == nil then
    log_error("当前没有可用场景（obs_frontend_get_current_scene 返回 nil），请先切到一个场景再试。")
    return
  end

  -- obs_scene_from_source 返回的是「借用」引用，不能 release；
  -- 需要释放的只有上面 obs_frontend_get_current_scene 拿到的 scene_source。
  local scene = obs.obs_scene_from_source(scene_source)
  if scene == nil then
    log_error("当前场景不是标准场景（obs_scene_from_source 返回 nil），无法添加浏览器源。")
    obs.obs_source_release(scene_source)
    return
  end

  local name = conf.source_name
  local url = overlay_url(nil)
  local data = build_browser_settings(url)

  local source = obs.obs_get_source_by_name(name)
  if source ~= nil then
    -- 已存在同名源：更新 URL 等设置（浏览器源会自行刷新）
    obs.obs_source_update(source, data)

    -- 同名源可能存在但不在当前场景里，补挂一次
    if obs.obs_scene_find_source(scene, name) == nil then
      obs.obs_scene_add(scene, source)
      log_info(string.format("已把已有源「%s」加入当前场景：%s", name, url))
    else
      log_info(string.format("已刷新浏览器源「%s」：%s", name, url))
    end

    obs.obs_source_release(source)
  else
    source = obs.obs_source_create("browser_source", name, data, nil)
    if source == nil then
      log_error(string.format("创建浏览器源「%s」失败，请确认 OBS 安装包含 obs-browser 插件。", name))
    else
      obs.obs_scene_add(scene, source)
      obs.obs_source_release(source)
      log_info(string.format("已创建浏览器源「%s」（%dx%d @%dfps）：%s",
        name, conf.width, conf.height, conf.fps, url))
    end
  end

  obs.obs_data_release(data)
  obs.obs_source_release(scene_source)
end

-- 重载叠加层：给 URL 追加时间戳参数，触发浏览器源重新加载
local function action_reload_overlay()
  local name = conf.source_name
  local source = obs.obs_get_source_by_name(name)
  if source == nil then
    log_warn(string.format("没有找到名为「%s」的源，请先执行「添加/刷新浏览器源」。", name))
    return
  end

  local source_id = obs.obs_source_get_id(source)
  if source_id ~= "browser_source" then
    log_warn(string.format("源「%s」不是浏览器源（id=%s），已跳过重载。", name, tostring(source_id)))
    obs.obs_source_release(source)
    return
  end

  local url = overlay_url("_r=" .. tostring(os.time()))
  local data = obs.obs_data_create()
  obs.obs_data_set_string(data, "url", url)
  obs.obs_source_update(source, data)
  obs.obs_data_release(data)
  obs.obs_source_release(source)

  log_info(string.format("已重载叠加层「%s」。", name))
end

-- 打开控制台（仅做桌面 shell 调用，失败不抛出）
local function action_open_console()
  local url = control_url()
  local is_windows = package.config:sub(1, 1) == "\\"
  local cmd
  if is_windows then
    cmd = string.format('start "" "%s"', url)
  elseif package.config:sub(1, 1) == "/" and os.getenv("HOME") ~= nil and os.getenv("DISPLAY") == nil then
    -- macOS 一般没有 DISPLAY 环境变量
    cmd = string.format('open "%s"', url)
  else
    cmd = string.format('xdg-open "%s"', url)
  end

  local ok, err = pcall(os.execute, cmd)
  if ok then
    log_info("已尝试打开控制台：" .. url)
  else
    log_warn(string.format("打开控制台失败：%s（可手动访问 %s）", tostring(err), url))
  end
end

-- 菜单注册：不同 OBS 版本对第三参数（hotkey）的处理略有差异，逐个签名退让
local function add_tools_menu_item(label, callback)
  local ok = pcall(obs.obs_frontend_add_tools_menu_item, label, callback)
  if ok then
    return true
  end
  ok = pcall(obs.obs_frontend_add_tools_menu_item, label, callback, nil)
  if ok then
    return true
  end
  ok = pcall(obs.obs_frontend_add_tools_menu_item, label, callback, "")
  if ok then
    return true
  end
  log_warn("注册菜单项失败：" .. label)
  return false
end

local function register_menus()
  if menus_registered then
    return
  end
  menus_registered = true
  add_tools_menu_item("ObsFusion: 添加/刷新浏览器源", action_add_or_refresh)
  add_tools_menu_item("ObsFusion: 打开控制台", action_open_console)
  add_tools_menu_item("ObsFusion: 重载叠加层", action_reload_overlay)
end

-- 把 OBS 传入的 settings 读进 conf
local function apply_settings(settings)
  if settings == nil then
    return
  end

  conf.port = clamp_int(obs.obs_data_get_int(settings, "port"), 1, 65535, 8787)

  local project = trim(obs.obs_data_get_string(settings, "project"))
  if project == "" then
    project = "demo"
  end
  conf.project = project

  local source_name = trim(obs.obs_data_get_string(settings, "source_name"))
  if source_name == "" then
    source_name = "ObsFusion Overlay"
  end
  conf.source_name = source_name

  conf.width = clamp_int(obs.obs_data_get_int(settings, "width"), 64, 7680, 1920)
  conf.height = clamp_int(obs.obs_data_get_int(settings, "height"), 64, 4320, 1080)
  conf.reroute_audio = obs.obs_data_get_bool(settings, "reroute_audio")
end

-- ---------------------------------------------------------------- OBS 回调

-- 脚本属性面板
function script_properties()
  local props = obs.obs_properties_create()

  obs.obs_properties_add_int(props, "port", "服务端口", 1, 65535, 1)
  obs.obs_properties_add_text(props, "project", "场景项目 ID", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_text(props, "source_name", "浏览器源名称", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_int(props, "width", "画布宽度", 64, 7680, 1)
  obs.obs_properties_add_int(props, "height", "画布高度", 64, 4320, 1)
  obs.obs_properties_add_bool(props, "reroute_audio", "把音频路由到浏览器源（reroute_audio）")
  obs.obs_properties_add_text(
    props,
    "obs_fusion_hint",
    "添加完源之后，在「工具」菜单里使用 ObsFusion 的三个菜单项；修改端口/项目 id 后请重新执行一次「添加/刷新浏览器源」。",
    obs.OBS_TEXT_INFO
  )

  return props
end

-- 属性默认值
function script_defaults(settings)
  obs.obs_data_set_default_int(settings, "port", 8787)
  obs.obs_data_set_default_string(settings, "project", "demo")
  obs.obs_data_set_default_string(settings, "source_name", "ObsFusion Overlay")
  obs.obs_data_set_default_int(settings, "width", 1920)
  obs.obs_data_set_default_int(settings, "height", 1080)
  obs.obs_data_set_default_bool(settings, "reroute_audio", true)
end

-- 脚本描述（显示在脚本列表里）
function script_description()
  return string.format([[
ObsFusion v%s —— 把 ObsFusion 的 HTML 叠加层（字幕 / 标题 / 弹幕）接入 OBS。

· 服务地址：http://127.0.0.1:<端口>
· 叠加层：/overlay/<项目 id>?obs=1
· 控制台：/control

安装后使用「工具」菜单里的三个 ObsFusion 菜单项：
「添加/刷新浏览器源」「打开控制台」「重载叠加层」。
]], SCRIPT_VERSION)
end

-- 属性变更
function script_update(settings)
  apply_settings(settings)
end

-- 脚本加载
function script_load(settings)
  apply_settings(settings)
  register_menus()
  log_info(string.format("脚本已加载 v%s（端口 %d，项目 %s，源 %s）。",
    SCRIPT_VERSION, conf.port, conf.project, conf.source_name))
end

-- 脚本卸载
function script_unload()
  log_info("脚本已卸载。")
end
