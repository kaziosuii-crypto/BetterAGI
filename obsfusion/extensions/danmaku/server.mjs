// 弹幕位置映射扩展（服务端）
// 能力链 danmaku.route：输入标准化消息，输出 { zone, mode, lane, x, y, duration, color, size }

var ZONES = ['top', 'bottom', 'top-left', 'top-right', 'bottom-left', 'bottom-right', 'center', 'mid-left', 'mid-right', 'custom'];

function clamp(n, min, max, fallback) {
  var v = Number(n);
  if (!Number.isFinite(v)) v = fallback;
  return Math.min(max, Math.max(min, v));
}

export function normalizeConfig(input) {
  input = input || {};
  var modes = ['scroll', 'fixed', 'reverse'];
  return {
    lanes: Math.round(clamp(input.lanes, 1, 12, 6)),
    area: ['top', 'bottom', 'full'].indexOf(input.area) >= 0 ? input.area : 'top',
    mode: modes.indexOf(input.mode) >= 0 ? input.mode : 'scroll',
    speed: clamp(input.speed, 2, 30, 8),
    fontSize: Math.round(clamp(input.fontSize, 18, 72, 34)),
    opacity: clamp(input.opacity, 0.2, 1, 0.92),
    maxOnScreen: Math.round(clamp(input.maxOnScreen, 4, 120, 36)),
    avoidSubtitles: input.avoidSubtitles !== false,
    defaultTarget: input.defaultTarget && typeof input.defaultTarget === 'object' ? input.defaultTarget : {},
    routes: Array.isArray(input.routes) ? input.routes.filter(function (r) { return r && r.when && r.target; }) : []
  };
}

export function normalizeMessage(raw) {
  raw = raw || {};
  var text = String(raw.text || '').replace(/\r?\n/g, ' ').trim().slice(0, 200);
  if (!text) {
    var err = new Error('danmaku text is required');
    err.code = 'INVALID';
    throw err;
  }
  return {
    id: String(raw.id || ('dm-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6))),
    text: text,
    user: String(raw.user || '').slice(0, 40),
    color: /^#[0-9a-fA-F]{3,8}$/.test(String(raw.color || '')) ? String(raw.color) : '',
    mode: ['scroll', 'fixed', 'reverse'].indexOf(raw.mode) >= 0 ? raw.mode : '',
    size: Number(raw.size) || 0,
    level: Number(raw.level) || 0,
    badge: String(raw.badge || '').slice(0, 24),
    at: Date.now(),
    raw: raw
  };
}

function testField(value, expected) {
  if (Array.isArray(expected)) return expected.indexOf(value) >= 0;
  return String(value) === String(expected);
}

export function matchRule(when, message) {
  when = when || {};
  if (when.user !== undefined && !testField(message.user, when.user)) return false;
  if (when.mode !== undefined && !testField(message.mode, when.mode)) return false;
  if (when.badge !== undefined && !testField(message.badge, when.badge)) return false;
  if (when.contains !== undefined && String(message.text).indexOf(String(when.contains)) < 0) return false;
  if (when.regex !== undefined) {
    var re;
    try { re = new RegExp(String(when.regex)); } catch (_) { return false; }
    if (!re.test(message.text)) return false;
  }
  if (when.levelMin !== undefined && message.level < Number(when.levelMin)) return false;
  if (when.levelMax !== undefined && message.level > Number(when.levelMax)) return false;
  return true;
}

function zoneXY(zone, lane, cfg, avoidSubtitles) {
  var x = 0.5;
  var y = 0.12;
  var laneGap = 0.062;
  var topBase = 0.09 + lane * laneGap;
  // 底部带向上堆叠：越往后的轨道越靠上，避免压住底部字幕（字幕约占 0.72–0.92）
  var bottomBase = (avoidSubtitles ? 0.62 : 0.78) - lane * 0.055;
  if (zone === 'top' || zone === 'top-left' || zone === 'top-right') {
    y = topBase;
    // x 是锚点：-right 的文本向右展开，起点要留出边距
    x = zone === 'top-left' ? 0.04 : zone === 'top-right' ? 0.88 : 0.5;
  } else if (zone === 'bottom' || zone === 'bottom-left' || zone === 'bottom-right') {
    y = Math.max(0.4, Math.min(bottomBase, 0.9));
    x = zone === 'bottom-left' ? 0.04 : zone === 'bottom-right' ? 0.88 : 0.5;
  } else if (zone === 'center') {
    x = 0.5; y = 0.5;
  } else if (zone === 'mid-left') {
    x = 0.04; y = 0.5;
  } else if (zone === 'mid-right') {
    x = 0.88; y = 0.5;
  } else {
    var area = cfg.area === 'bottom' ? bottomBase : topBase;
    y = Math.min(area, 0.9);
    x = 0.5;
  }
  return { x: Math.round(x * 1000) / 1000, y: Math.round(y * 1000) / 1000 };
}

export function mapDanmaku(message, config, lane) {
  var cfg = normalizeConfig(config);
  var target = cfg.defaultTarget || {};
  var matchedRule = null;
  for (var i = 0; i < cfg.routes.length; i++) {
    if (matchRule(cfg.routes[i].when, message)) {
      target = Object.assign({}, cfg.defaultTarget || {}, cfg.routes[i].target || {});
      matchedRule = cfg.routes[i].name || ('rule-' + (i + 1));
      break;
    }
  }
  var zone = ZONES.indexOf(target.zone) >= 0 ? target.zone : cfg.area;
  var mode = ['scroll', 'fixed', 'reverse'].indexOf(target.mode) >= 0 ? target.mode : cfg.mode;
  var duration = clamp(target.duration, 1, 60, mode === 'scroll' ? cfg.speed : 6);
  var pos = target.zone === 'custom' ? {
    x: clamp(target.x, 0, 1, 0.5),
    y: clamp(target.y, 0, 1, 0.5)
  } : zoneXY(zone, lane, cfg, cfg.avoidSubtitles);
  return {
    id: message.id,
    text: message.text,
    user: message.user,
    color: target.color || message.color || '#F4F1EA',
    size: target.size || message.size || cfg.fontSize,
    zone: zone,
    mode: mode,
    lane: lane,
    lanes: cfg.lanes,
    x: pos.x,
    y: pos.y,
    duration: duration,
    speed: cfg.speed,
    opacity: cfg.opacity,
    avoidSubtitles: cfg.avoidSubtitles,
    matchedRule: matchedRule
  };
}

export default {
  setup: function (api) {
    function nextLane() {
      var state = api.getState();
      var cfg = normalizeConfig(api.config);
      state.lane = (Number(state.lane) || 0) % cfg.lanes;
      var lane = state.lane;
      state.lane = (lane + 1) % cfg.lanes;
      return lane;
    }

    api.capability('danmaku.route', function (ctx, message) {
      var lane = nextLane();
      return mapDanmaku(message, api.config, lane);
    });

    api.route('POST', '/send', function (ctx) {
      var message = normalizeMessage(ctx.body || {});
      var mapped = mapDanmaku(message, api.config, nextLane());
      api.broadcast('danmaku.add', mapped, ctx.sceneId);
      return { ok: true, message: mapped };
    });

    api.log('info', 'danmaku mapper ready');
  }
};