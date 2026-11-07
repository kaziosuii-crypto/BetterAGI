// 章节导航扩展（服务端）
// 职责：把 project.cues 里的「结构化文本」派生为章节区间，供叠加层 / 控制台 / 其它扩展复用。
//   能力链：chapters.build(ctx, seed) → { chapters:[{ index, title, start, end, cueIds }], total }
//   路由：  GET /chapters（挂载后为 /x/chapters/chapters）→ 同构输出
// 纯 Node ESM，零依赖。
import fsp from 'node:fs/promises';
import path from 'node:path';

var AUTO_TITLE_LENGTH = 12;   // 自动分章时章节名取该组第一条文本的前 12 字
var MAX_TITLE_LENGTH = 60;    // 章节名最大长度（防止极端长文本撑坏叠加层）

function clamp(value, min, max, fallback) {
  var n = Number(value);
  if (!Number.isFinite(n)) n = fallback;
  return Math.min(max, Math.max(min, n));
}

function round(value) {
  return Math.round(Number(value) * 1000) / 1000;
}

// 统一空白：换行折成空格、连续空白压成一个、去掉首尾，并按上限截断
function tidy(value, max) {
  var text = String(value === undefined || value === null ? '' : value);
  text = text.replace(/\r\n?/g, '\n').replace(/\s+/g, ' ').trim();
  return text.slice(0, max || 400);
}

// 配置归一化：控制台表单可能给出任意值，这里全部收敛到安全区间（与 manifest 的 configSchema 保持一致）
export function normalizeConfig(input) {
  input = input || {};
  var accent = tidy(input.accent, 24);
  if (!/^#[0-9a-fA-F]{3,8}$/.test(accent)) accent = '#E8B05A';
  return {
    position: input.position === 'right' ? 'right' : 'left',
    width: Math.round(clamp(input.width, 200, 640, 340)),
    fontSize: Math.round(clamp(input.fontSize, 14, 48, 22)),
    opacity: round(clamp(input.opacity, 0.2, 1, 0.9)),
    maxItems: Math.round(clamp(input.maxItems, 1, 12, 6)),
    autoSplit: Math.round(clamp(input.autoSplit, 1, 30, 6)),
    showProgress: input.showProgress !== false,
    accent: accent,
    offsetY: Math.round(clamp(input.offsetY, 120, 560, 300))
  };
}

// 把一条 cue 收敛成派生章节只需要的最小字段
function normalizeCue(raw, index) {
  if (!raw || typeof raw !== 'object') return null;
  var start = Number(raw.start);
  if (!Number.isFinite(start) || start < 0) start = 0;
  var end = Number(raw.end);
  if (!Number.isFinite(end) || end < start) end = start + 0.1;
  var id = tidy(raw.id, 64) || ('c' + (index + 1));
  return { id: id, start: round(start), end: round(end), text: tidy(raw.text, 400), speaker: tidy(raw.speaker, 80) };
}

// 章节标记判定：speaker 为「章节」，或文本以 # 开头（允许 # 前有空白）
export function isChapterCue(cue) {
  if (!cue) return false;
  if (tidy(cue.speaker, 80) === '章节') return true;
  return /^#/.test(String(cue.text === undefined || cue.text === null ? '' : cue.text).replace(/^\s+/, ''));
}

// 从标记 cue 里取章节名：去掉开头的 # 与首尾空白
export function chapterTitle(cue) {
  var raw = cue && cue.text;
  // 去掉 Markdown 标题语法的前后 # 与首尾空白（"## 第一幕 ##" → "第一幕"）
  var text = tidy(
    String(raw === undefined || raw === null ? '' : raw).replace(/^[\s#]+/, '').replace(/[\s#]+$/, ''),
    MAX_TITLE_LENGTH
  );
  return text || '未命名章节';
}

// 核心派生：返回稳定结构，不修改入参
export function buildChapters(project, config) {
  var cfg = normalizeConfig(config);
  var raw = project && Array.isArray(project.cues) ? project.cues : [];
  var cues = [];
  for (var i = 0; i < raw.length; i++) {
    var cue = normalizeCue(raw[i], i);
    if (cue) cues.push(cue);
  }
  if (!cues.length) return { chapters: [], total: 0 };
  cues.sort(function (a, b) { return a.start - b.start; });

  var marks = [];
  for (var m = 0; m < cues.length; m++) {
    if (isChapterCue(cues[m])) marks.push(m);
  }

  var groups = [];
  if (marks.length) {
    // 章节区间：[0, marks[1]), [marks[1], marks[2]), …, [marks[n-1], cues.length)
    // 第一章固定从 0 开始（标记之前的引子并入第一章），之后每章从"下一个标记"处起算；
    // 这样每个标记 cue 都落在它自己开启的那一章里，且末尾不截断。
    var bounds = [0];
    for (var b = 1; b < marks.length; b++) bounds.push(marks[b]);
    bounds.push(cues.length);
    for (var k = 0; k < marks.length; k++) {
      groups.push({ title: chapterTitle(cues[marks[k]]), cues: cues.slice(bounds[k], bounds[k + 1]) });
    }
  } else {
    // 兜底：每 autoSplit 条 cue 一章
    var size = cfg.autoSplit;
    for (var s = 0; s < cues.length; s += size) {
      var slice = cues.slice(s, s + size);
      var head = '';
      for (var t = 0; t < slice.length; t++) {
        if (slice[t].text) { head = slice[t].text; break; }
      }
      var title = head ? head.slice(0, AUTO_TITLE_LENGTH) : ('第 ' + (groups.length + 1) + ' 章');
      groups.push({ title: title, cues: slice });
    }
  }

  var chapters = [];
  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    var start = group.cues[0].start;
    var end = group.cues[0].end;
    var cueIds = [];
    for (var c = 0; c < group.cues.length; c++) {
      if (group.cues[c].end > end) end = group.cues[c].end;
      cueIds.push(group.cues[c].id);
    }
    chapters.push({
      index: g + 1,
      title: group.title,
      start: round(start),
      end: round(end),
      cueIds: cueIds
    });
  }
  return { chapters: chapters, total: chapters.length };
}

// 缓存键：项目 id + 更新时间 + cue 数量 + 自动分章步长，任一变化即重算
function cacheKey(project, cfg) {
  var meta = (project && project.meta) || {};
  var count = project && Array.isArray(project.cues) ? project.cues.length : 0;
  return [project && project.id, meta.updatedAt || '', count, cfg.autoSplit].join('|');
}

export default {
  setup: function (api) {
    function currentConfig() {
      return normalizeConfig(api.config);
    }

    function clearCache() {
      var state = api.getState();
      state.chapters = null;
      state.key = '';
    }

    // 解析这次调用该用哪个 project：能力链 seed → 调用方 ctx → 宿主当前场景
    function resolveProject(ctx, seed) {
      if (seed && Array.isArray(seed.cues)) return seed;
      if (ctx && ctx.project && Array.isArray(ctx.project.cues)) return ctx.project;
      if (api.project && Array.isArray(api.project.cues)) return api.project;
      return null;
    }

    // 路由上下文里 project 恒为 null（宿主只传 scene），按需从磁盘补读
    // dataDir = <root>/data/extensions/chapters，上溯三层即仓库根
    async function loadProjectFromDisk(ctx) {
      var id = String((ctx && ctx.query && ctx.query.project) || (ctx && ctx.sceneId) || '');
      if (!/^[a-zA-Z0-9_-]{1,64}$/.test(id)) return null;
      var file = path.join(path.resolve(api.dataDir, '..', '..', '..'), 'projects', id, 'project.json');
      try {
        return JSON.parse(await fsp.readFile(file, 'utf8'));
      } catch (err) {
        api.log('warn', 'chapters: project "' + id + '" 读取失败 - ' + err.message);
        return null;
      }
    }

    function build(project) {
      var cfg = currentConfig();
      var state = api.getState();
      var key = cacheKey(project, cfg);
      if (state.key === key && state.chapters) return state.chapters;
      var result = buildChapters(project, cfg);
      state.key = key;
      state.chapters = result;
      return result;
    }

    // 能力链：任何扩展 / 宿主想拿章节时调 runCapability('chapters.build', ctx, seed)
    api.capability('chapters.build', function (ctx, seed) {
      var project = resolveProject(ctx, seed);
      if (!project) return { chapters: [], total: 0 };
      return build(project);
    });

    // GET /x/chapters/chapters[?scene=<projectId>]
    api.route('GET', '/chapters', async function (ctx) {
      var project = resolveProject(ctx, null);
      if (!project) project = await loadProjectFromDisk(ctx);
      if (!project) return { chapters: [], total: 0 };
      return build(project);
    });

    // 项目装载 / 保存时清缓存，避免拿到过期章节
    api.on('project.load', function () {
      clearCache();
    });
    api.on('project.save', function (payload) {
      clearCache();
      if (payload && payload.project && Array.isArray(payload.project.cues)) build(payload.project);
    });

    api.log('info', 'chapters ready (capability chapters.build, route GET /chapters)');
  },

  // 配置变更：清缓存并把归一化后的配置广播给叠加层，客户端即时生效
  onConfigChange: function (config, api) {
    var cfg = normalizeConfig(config);
    if (api && typeof api.setState === 'function') {
      api.setState({ chapters: null, key: '' });
    }
    if (api && typeof api.broadcast === 'function') {
      api.broadcast('chapters.config', cfg);
    }
  }
};
