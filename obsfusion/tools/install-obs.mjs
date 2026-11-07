#!/usr/bin/env node
// ============================================================================
// ObsFusion -> OBS 安装器（零依赖，Node ESM）
//
// 默认行为：只读检测本机 OBS 环境 + 把 obs/obsfusion.lua 复制到
//           %APPDATA%\obs-studio\scripts\obsfusion.lua（已有文件先备份）
// 可选参数：
//   --scene            额外安装场景集合模板到
//                      %APPDATA%\obs-studio\basic\scenes\ObsFusion.json
//   --port <n>         安装时把叠加层 URL 改写到指定端口（默认 8787）
//   --project <id>     安装时把叠加层 URL 改写到指定项目 id（默认 demo）
//   --dry-run          只检测，不写任何文件
//   --help             显示帮助
//
// 用法：node tools/install-obs.mjs [--scene] [--port 8787] [--project demo]
// ============================================================================

import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const LUA_SRC = join(ROOT, "obs", "obsfusion.lua");
const SCENE_SRC = join(ROOT, "obs", "scene-collection.template.json");

const IS_WINDOWS = process.platform === "win32";
const DEFAULT_PORT = 8787;
const DEFAULT_PROJECT = "demo";

// ------------------------------------------------------------------ 输出工具

function line(text) {
  process.stdout.write((text === undefined ? "" : String(text)) + "\n");
}

function title(text) {
  const tail = text.length < 62 ? "=".repeat(62 - text.length) : "";
  line("");
  line("== " + text + " " + tail);
}

function ok(text) {
  line("  [OK]   " + text);
}

function warn(text) {
  line("  [注意] " + text);
}

function fail(text) {
  line("  [失败] " + text);
}

// ------------------------------------------------------------------ 参数解析

function parseArgs(argv) {
  const opts = { scene: false, dryRun: false, help: false, port: DEFAULT_PORT, project: DEFAULT_PROJECT };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--scene") {
      opts.scene = true;
    } else if (arg === "--dry-run") {
      opts.dryRun = true;
    } else if (arg === "--help" || arg === "-h") {
      opts.help = true;
    } else if (arg === "--port") {
      const raw = argv[i + 1] === undefined ? "" : argv[i + 1];
      const value = Number.parseInt(raw, 10);
      if (!Number.isFinite(value) || value <= 0 || value > 65535) {
        throw new Error("--port 需要一个 1..65535 的整数，收到：" + (raw || "(空)"));
      }
      opts.port = value;
      i += 1;
    } else if (arg === "--project") {
      const value = String(argv[i + 1] === undefined ? "" : argv[i + 1]).trim();
      if (value === "") {
        throw new Error("--project 需要一个项目 id，例如 demo");
      }
      opts.project = value;
      i += 1;
    } else {
      throw new Error("未知参数：" + arg + "（用 --help 查看用法）");
    }
  }

  return opts;
}

function printHelp() {
  line("ObsFusion -> OBS 安装器");
  line("");
  line("  node tools/install-obs.mjs                      只检测并把 Lua 脚本装进 OBS");
  line("  node tools/install-obs.mjs --scene              同时安装场景集合模板");
  line("  node tools/install-obs.mjs --scene --port 8787 --project demo");
  line("  node tools/install-obs.mjs --dry-run            只检测，不写文件");
  line("");
  line("已有同名文件会先备份为 <文件名>.bak-<时间戳>，不会被静默覆盖。");
}

// ------------------------------------------------------------------ 环境检测

function obsDataDir() {
  if (IS_WINDOWS) {
    const appData = process.env.APPDATA;
    if (!appData) {
      return null;
    }
    return join(appData, "obs-studio");
  }
  const home = process.env.HOME;
  return home ? join(home, ".config", "obs-studio") : null;
}

function regQuery(key, value) {
  if (!IS_WINDOWS) {
    return null;
  }
  try {
    const res = spawnSync("reg", ["query", key, "/v", value], { encoding: "utf8", timeout: 5000 });
    if (res.status !== 0 || !res.stdout) {
      return null;
    }
    const match = res.stdout.match(/REG_SZ\s+(.+?)\s*$/m);
    return match ? match[1].trim() : null;
  } catch {
    return null;
  }
}

function readSteamLibraries() {
  const roots = new Set();
  const steamPath = regQuery("HKCU\\Software\\Valve\\Steam", "SteamPath")
    || regQuery("HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam", "InstallPath");
  if (steamPath) {
    roots.add(steamPath.replace(/\//g, "\\"));
  }
  for (const guess of ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary", "E:\\SteamLibrary"]) {
    if (existsSync(guess)) {
      roots.add(guess);
    }
  }

  const libraries = new Set(roots);
  for (const root of roots) {
    const vdf = join(root, "steamapps", "libraryfolders.vdf");
    if (!existsSync(vdf)) {
      continue;
    }
    try {
      const text = readFileSync(vdf, "utf8");
      for (const match of text.matchAll(/"path"\s+"([^"]+)"/g)) {
        libraries.add(match[1]);
      }
    } catch {
      // 读不到就跳过，不影响主流程
    }
  }

  return [...libraries];
}

function findObsInstall() {
  const candidates = [];
  for (const envKey of ["OBS_PATH", "OBS_INSTALL", "OBS_STUDIO_PATH"]) {
    if (process.env[envKey]) {
      candidates.push(process.env[envKey]);
    }
  }
  for (const base of [process.env["ProgramFiles"], process.env["ProgramFiles(x86)"]]) {
    if (base) {
      candidates.push(join(base, "obs-studio"));
    }
  }
  for (const lib of readSteamLibraries()) {
    candidates.push(join(lib, "steamapps", "common", "OBS Studio"));
    if (lib.toLowerCase().endsWith("steamapps")) {
      candidates.push(join(lib, "common", "OBS Studio"));
    }
  }

  for (const candidate of candidates) {
    if (!candidate) {
      continue;
    }
    const exe = join(candidate, "bin", "64bit", "obs64.exe");
    if (existsSync(exe)) {
      return { dir: candidate, exe };
    }
  }

  return null;
}

function newestLogFile(dir) {
  if (!existsSync(dir)) {
    return null;
  }
  let files;
  try {
    files = readdirSync(dir).filter((name) => name.toLowerCase().endsWith(".txt"));
  } catch {
    return null;
  }
  if (files.length === 0) {
    return null;
  }
  files.sort((a, b) => {
    try {
      return statSync(join(dir, b)).mtimeMs - statSync(join(dir, a)).mtimeMs;
    } catch {
      return 0;
    }
  });
  return join(dir, files[0]);
}

function detectObsVersion(dataDir) {
  const logFile = newestLogFile(join(dataDir, "logs"));
  if (!logFile) {
    return null;
  }
  try {
    const head = readFileSync(logFile, "utf8").split(/\r?\n/, 3).join("\n");
    const match = head.match(/OBS\s+(\d+\.\d+\.\d+)/);
    if (match) {
      return { version: match[1], source: logFile };
    }
  } catch {
    // 忽略
  }
  return null;
}

function isObsRunning() {
  if (!IS_WINDOWS) {
    const res = spawnSync("pgrep", ["-f", "obs"], { encoding: "utf8" });
    return res.status === 0;
  }
  try {
    const res = spawnSync("tasklist", ["/FI", "IMAGENAME eq obs64.exe", "/NH"], { encoding: "utf8", timeout: 8000 });
    const out = String(res.stdout === undefined ? "" : res.stdout).toLowerCase();
    return out.includes("obs64.exe");
  } catch {
    return false;
  }
}

function readWebsocketConfig(dataDir) {
  const file = join(dataDir, "plugin_config", "obs-websocket", "config.json");
  if (!existsSync(file)) {
    return { exists: false, file };
  }
  try {
    const raw = JSON.parse(readFileSync(file, "utf8"));
    return {
      exists: true,
      file,
      enabled: Boolean(raw.server_enabled),
      port: raw.server_port === undefined ? 4455 : raw.server_port,
      authRequired: Boolean(raw.auth_required),
      hasPassword: typeof raw.server_password === "string" && raw.server_password.length > 0,
    };
  } catch (error) {
    return { exists: true, file, error: String(error && error.message ? error.message : error) };
  }
}

// ------------------------------------------------------------------ 安装动作

function stamp(date) {
  const d = date || new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return String(d.getFullYear()) + pad(d.getMonth() + 1) + pad(d.getDate())
    + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
}

function copyWithBackup(src, dest) {
  if (!existsSync(src)) {
    throw new Error("源文件不存在：" + src);
  }
  mkdirSync(dirname(dest), { recursive: true });

  let backup = null;
  if (existsSync(dest)) {
    backup = dest + ".bak-" + stamp();
    copyFileSync(dest, backup);
  }
  copyFileSync(src, dest);
  return { dest, backup };
}

function installLuaScript(dataDir, dryRun) {
  const dest = join(dataDir, "scripts", "obsfusion.lua");

  if (dryRun) {
    ok("[试运行] 将复制 " + LUA_SRC + " -> " + dest);
    return { dest, backup: null };
  }

  const result = copyWithBackup(LUA_SRC, dest);
  ok("Lua 脚本已安装：" + result.dest);
  if (result.backup) {
    ok("旧文件已备份：" + result.backup);
  }
  return result;
}

function installSceneCollection(dataDir, opts) {
  const scene = JSON.parse(readFileSync(SCENE_SRC, "utf8"));

  const overlay = Array.isArray(scene.sources)
    ? scene.sources.find((item) => item && item.id === "browser_source")
    : null;
  if (overlay && overlay.settings) {
    overlay.settings.url = "http://127.0.0.1:" + opts.port + "/overlay/"
      + encodeURIComponent(opts.project) + "?obs=1";
  }

  const targetDir = join(dataDir, "basic", "scenes");
  const dest = join(targetDir, "ObsFusion.json");

  if (opts.dryRun) {
    ok("[试运行] 将写入场景集合：" + dest);
    return { dest, backup: null, url: overlay && overlay.settings ? overlay.settings.url : null };
  }

  mkdirSync(targetDir, { recursive: true });
  let backup = null;
  if (existsSync(dest)) {
    backup = dest + ".bak-" + stamp();
    copyFileSync(dest, backup);
  }
  writeFileSync(dest, JSON.stringify(scene, null, 2) + "\n", "utf8");

  ok("场景集合已写入：" + dest);
  if (backup) {
    ok("旧文件已备份：" + backup);
  }
  return { dest, backup, url: overlay && overlay.settings ? overlay.settings.url : null };
}

// ------------------------------------------------------------------ 主流程

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (error) {
    fail(error.message);
    line("");
    printHelp();
    return 1;
  }

  if (opts.help) {
    printHelp();
    return 0;
  }

  line("ObsFusion for OBS 安装器");
  line("项目目录：" + ROOT);
  if (opts.dryRun) {
    warn("试运行模式（--dry-run）：只检测，不写任何文件。");
  }

  const dataDir = obsDataDir();
  if (!dataDir) {
    fail("无法确定 OBS 配置目录（APPDATA 环境变量缺失）。");
    return 1;
  }

  title("1. 环境检测");

  const install = findObsInstall();
  if (install) {
    ok("OBS 安装目录：" + install.dir);
  } else {
    warn("没有自动找到 OBS 安装目录，可设置环境变量 OBS_PATH 指向 OBS Studio 目录。");
  }

  const obsVersion = detectObsVersion(dataDir);
  if (obsVersion) {
    ok("OBS 版本：" + obsVersion.version + "（来自日志 " + obsVersion.source + "）");
  } else {
    warn("没有从 OBS 日志里读到版本号；本脚本面向 OBS 30+，更老的版本未验证。");
  }

  ok("OBS 配置目录：" + dataDir);
  ok("脚本目录：" + join(dataDir, "scripts"));

  if (isObsRunning()) {
    warn("OBS 正在运行：脚本装好后可在 OBS 里重新添加一次脚本，安装器不会去关闭 OBS。");
  } else {
    ok("OBS 当前未运行。");
  }

  const ws = readWebsocketConfig(dataDir);
  if (!ws.exists) {
    warn("没有找到 obs-websocket 配置文件：可能是首次启动 OBS，或 OBS 版本早于 28。");
    warn("OBS 28+ 自带 obs-websocket；启动一次 OBS 后会自动生成配置。");
  } else if (ws.error) {
    warn("obs-websocket 配置存在但解析失败：" + ws.error);
  } else {
    ok("obs-websocket 配置：" + ws.file);
    line("         server_enabled=" + ws.enabled + "，端口=" + ws.port
      + "，认证=" + ws.authRequired + "，密码" + (ws.hasPassword ? "已设置" : "未设置"));
    if (!ws.enabled) {
      warn("obs-websocket 服务器当前是关闭状态：请在 OBS「工具 -> WebSocket 服务器设置」里勾选启用（端口 4455），否则自动控制不可用。");
    }
  }

  title("2. 安装 OBS Lua 脚本");

  let luaResult;
  try {
    luaResult = installLuaScript(dataDir, opts.dryRun);
  } catch (error) {
    fail("安装 Lua 脚本失败：" + error.message);
    return 1;
  }

  let sceneResult = null;
  if (opts.scene) {
    title("3. 安装场景集合模板");
    try {
      sceneResult = installSceneCollection(dataDir, opts);
      if (sceneResult.url) {
        ok("浏览器源 URL：" + sceneResult.url);
      }
    } catch (error) {
      fail("安装场景集合失败：" + error.message);
      return 1;
    }
  }

  title(opts.scene ? "4. 后续步骤" : "3. 后续步骤");
  line("  1) 启动 ObsFusion 服务：node bin/obsfusion.mjs serve");
  line("  2) 打开 OBS -> 菜单「工具 / Tools」->「脚本 / Scripts」->「+」-> 选择：");
  line("       " + luaResult.dest);
  line("  3) 在脚本属性里确认：服务端口（默认 8787）、场景项目 id（默认 demo）、");
  line("     源名称（默认 ObsFusion Overlay）、画布 1920x1080、reroute_audio（建议勾选）。");
  line("  4) 菜单「工具」->「ObsFusion: 添加/刷新浏览器源」，当前场景里会出现浏览器源。");
  line("     浏览器源 URL：http://127.0.0.1:" + opts.port + "/overlay/" + opts.project + "?obs=1");
  line("     控制台：http://127.0.0.1:" + opts.port + "/control");
  if (opts.scene && sceneResult) {
    line("  5) 场景集合：OBS 菜单「场景集合 / Scene Collection」->「ObsFusion」即可切换；");
    line("     或用「场景集合 -> 导入」选择该 JSON。它是独立集合，不会覆盖你现有场景。");
  }
  line("");
  line("  注意：场景集合里的黑色背景源只用于预览；把叠加层压到游戏画面上时，");
  line("        请在「来源」里隐藏或删除「ObsFusion Background」。");

  return 0;
}

process.exitCode = main();
