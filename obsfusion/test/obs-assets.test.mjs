// ObsFusion / OBS 静态资产校验（只读，不启动 OBS、不依赖服务运行）
// 运行：node --test test/
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const TEMPLATE_FILE = join(ROOT, "obs", "scene-collection.template.json");
const LUA_FILE = join(ROOT, "obs", "obsfusion.lua");
const INSTALLER_FILE = join(ROOT, "tools", "install-obs.mjs");

// ---------------------------------------------------------------- Lua 粗检查

const LF = String.fromCharCode(10);
const BACKSLASH = String.fromCharCode(92);

// 从 start（指向 '['）解析 Lua 长括号，返回结束位置；不是长括号返回 -1
function longBracketEnd(source, start) {
  let level = 0;
  let cursor = start + 1;
  while (source[cursor] === "=") {
    level += 1;
    cursor += 1;
  }
  if (source[cursor] !== "[") {
    return -1;
  }
  const closer = "]" + "=".repeat(level) + "]";
  const index = source.indexOf(closer, cursor + 1);
  return index === -1 ? -1 : index + closer.length;
}

// 跳过注释/字符串后做括号配平；同时记录未闭合的字符串
function scanLua(source) {
  const problems = [];
  const stack = [];
  const pairs = { ")": "(", "]": "[", "}": "{" };
  let i = 0;

  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];

    // 注释：--[[ ... ]] 或 -- 行注释
    if (ch === "-" && next === "-") {
      const longEnd = source[i + 2] === "[" ? longBracketEnd(source, i + 2) : -1;
      if (longEnd !== -1) {
        i = longEnd;
        continue;
      }
      const newline = source.indexOf(LF, i);
      i = newline === -1 ? source.length : newline + 1;
      continue;
    }

    // 普通字符串
    if (ch === "\"" || ch === "'") {
      const quote = ch;
      let cursor = i + 1;
      let closed = false;
      while (cursor < source.length) {
        const c = source[cursor];
        if (c === BACKSLASH) {
          cursor += 2;
          continue;
        }
        if (c === LF) {
          break;
        }
        if (c === quote) {
          closed = true;
          cursor += 1;
          break;
        }
        cursor += 1;
      }
      if (!closed) {
        problems.push("字符串未闭合（起始位置 " + i + "）");
      }
      i = cursor;
      continue;
    }

    // 长字符串 [[ ... ]]
    if (ch === "[") {
      const longEnd = longBracketEnd(source, i);
      if (longEnd !== -1) {
        i = longEnd;
        continue;
      }
    }

    if (ch === "(" || ch === "[" || ch === "{") {
      stack.push(ch);
    } else if (ch === ")" || ch === "]" || ch === "}") {
      const want = pairs[ch];
      const got = stack.pop();
      if (got !== want) {
        problems.push("括号不匹配（位置 " + i + "）：遇到 " + ch + "，期望配对的 " + want);
      }
    }

    i += 1;
  }

  if (stack.length > 0) {
    problems.push("还有 " + stack.length + " 个括号没有闭合：" + stack.join(" "));
  }

  return problems;
}

// ---------------------------------------------------------------- 场景集合

test("场景集合模板是合法 JSON 且含 browser_source 与正确 URL", () => {
  assert.ok(existsSync(TEMPLATE_FILE), "缺少文件：" + TEMPLATE_FILE);

  const raw = readFileSync(TEMPLATE_FILE, "utf8");
  let scene = null;
  assert.doesNotThrow(() => {
    scene = JSON.parse(raw);
  }, "scene-collection.template.json 不是合法 JSON");

  assert.equal(typeof scene.name, "string");
  assert.ok(Array.isArray(scene.sources), "sources 必须是数组");

  const browser = scene.sources.find((item) => item && item.id === "browser_source");
  assert.ok(browser, "模板里没有 id 为 browser_source 的源");
  assert.equal(browser.name, "ObsFusion Overlay");
  assert.equal(browser.settings.url, "http://127.0.0.1:8787/overlay/demo?obs=1");
  assert.equal(browser.settings.reroute_audio, true);
  assert.equal(browser.settings.fps, 60);
  assert.equal(browser.settings.fps_custom, true);
  assert.equal(browser.settings.width, 1920);
  assert.equal(browser.settings.height, 1080);
  assert.equal(browser.settings.shutdown_when_not_visible, false);

  // 场景本身：items 必须能按 name 找到对应源
  const sceneSource = scene.sources.find((item) => item && item.id === "scene");
  assert.ok(sceneSource, "模板里没有 id 为 scene 的场景");
  const names = new Set(scene.sources.map((item) => item && item.name));
  assert.ok(Array.isArray(sceneSource.settings.items));
  assert.ok(sceneSource.settings.items.length >= 1);
  for (const item of sceneSource.settings.items) {
    assert.ok(names.has(item.name), "场景 item 引用了不存在的源：" + item.name);
    assert.equal(item.visible, true);
  }
  assert.ok(names.has(browser.name), "浏览器源不在 sources 里");

  assert.equal(scene.current_scene, sceneSource.name);
  assert.ok(Array.isArray(scene.scene_order) && scene.scene_order.length >= 1);
  assert.ok(scene.resolution && scene.resolution.x === 1920 && scene.resolution.y === 1080);
});

// ---------------------------------------------------------------- Lua 脚本

test("obsfusion.lua 括号与引号配平", () => {
  assert.ok(existsSync(LUA_FILE), "缺少文件：" + LUA_FILE);

  const lua = readFileSync(LUA_FILE, "utf8");
  assert.ok(lua.length > 1000, "脚本内容过短，可能被截断");
  assert.deepEqual(scanLua(lua), []);
});

test("obsfusion.lua 含关键 API 与菜单文案", () => {
  const lua = readFileSync(LUA_FILE, "utf8");

  const required = [
    "obs = obslua",
    "obs.obs_frontend_get_current_scene",
    "obs.obs_scene_from_source",
    "obs.obs_get_source_by_name",
    "obs.obs_source_create",
    "obs.obs_source_update",
    "obs.obs_scene_add",
    "obs.obs_data_create",
    "obs.obs_data_set_string",
    "obs.obs_data_set_bool",
    "obs.obs_data_set_int",
    "obs.obs_data_release",
    "obs.obs_source_release",
    "obs_frontend_add_tools_menu_item",
    "function script_properties",
    "function script_update",
    "function script_description",
    "ObsFusion: 添加/刷新浏览器源",
    "ObsFusion: 打开控制台",
    "ObsFusion: 重载叠加层",
    "browser_source",
    "reroute_audio",
    "restart_when_active",
  ];
  for (const needle of required) {
    assert.ok(lua.includes(needle), "脚本缺少关键内容：" + needle);
  }

  // 叠加层 URL 模板
  assert.ok(lua.includes("http://127.0.0.1:%d/overlay/%s?obs=1"), "URL 模板不对");
  assert.ok(lua.includes("/control"), "缺少控制台 URL");

  // 不允许出现我们没用过的场景释放 API（obs_scene_from_source 返回借用引用）
  assert.ok(!lua.includes("obs_scene_release"), "不应调用不存在的 obs_scene_release");

  // 所有 obs.xxx 调用/常量必须在白名单内，防止写错 API 名
  const known = new Set([
    "LOG_ERROR",
    "LOG_INFO",
    "LOG_WARNING",
    "OBS_TEXT_DEFAULT",
    "OBS_TEXT_INFO",
    "obs_data_create",
    "obs_data_get_bool",
    "obs_data_get_int",
    "obs_data_get_string",
    "obs_data_release",
    "obs_data_set_bool",
    "obs_data_set_default_bool",
    "obs_data_set_default_int",
    "obs_data_set_default_string",
    "obs_data_set_int",
    "obs_data_set_string",
    "obs_frontend_add_tools_menu_item",
    "obs_frontend_get_current_scene",
    "obs_get_source_by_name",
    "obs_properties_add_bool",
    "obs_properties_add_int",
    "obs_properties_add_text",
    "obs_properties_create",
    "obs_scene_add",
    "obs_scene_find_source",
    "obs_scene_from_source",
    "obs_source_create",
    "obs_source_get_id",
    "obs_source_release",
    "obs_source_update",
    "script_log",
  ]);

  const used = new Set();
  const pattern = /obs\.([A-Za-z_][A-Za-z0-9_]*)/g;
  let match = pattern.exec(lua);
  while (match !== null) {
    used.add(match[1]);
    match = pattern.exec(lua);
  }
  assert.ok(used.size > 0, "没有扫描到任何 obs. API 调用");
  for (const name of used) {
    assert.ok(known.has(name), "脚本使用了未验证的 API：obs." + name);
  }
});

// ---------------------------------------------------------------- 安装器

test("install-obs.mjs 能被 node --check 解析", () => {
  assert.ok(existsSync(INSTALLER_FILE), "缺少文件：" + INSTALLER_FILE);

  const result = spawnSync(process.execPath, ["--check", INSTALLER_FILE], { encoding: "utf8" });
  assert.equal(result.status, 0, "node --check 失败：" + (result.stderr || result.stdout));
});

test("install-obs.mjs --help 可直接运行且不写文件", () => {
  const result = spawnSync(process.execPath, [INSTALLER_FILE, "--help"], { encoding: "utf8" });
  assert.equal(result.status, 0, "运行安装器 --help 失败：" + (result.stderr || result.stdout));
  const out = String(result.stdout || "");
  assert.ok(out.includes("install-obs.mjs"), "帮助里应包含用法");
  assert.ok(out.includes("--scene"), "帮助里应包含 --scene");
  assert.ok(out.includes("备份"), "帮助里应说明备份行为");
});
