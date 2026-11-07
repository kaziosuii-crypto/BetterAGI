(function(){
// ObsFusion 浏览器运行时：扩展注册、图层、事件总线、时间参数
(function (global) {
  var registry = [];
  var listeners = new Map();
  var canvas = { width: 1920, height: 1080 };
  var state = { time: 0, status: 'idle', revision: 0, project: null, timeline: null, theme: {} };
  var layers = new Map();

  function on(name, fn) {
    if (!listeners.has(name)) listeners.set(name, []);
    listeners.get(name).push(fn);
    return function () {
      var list = listeners.get(name) || [];
      var i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    };
  }

  function emit(name, payload) {
    var list = listeners.get(name) || [];
    for (var i = 0; i < list.length; i++) {
      try { list[i](payload); } catch (err) { console.error('[of] listener ' + name, err); }
    }
  }

  function createLayer(opts) {
    opts = opts || {};
    if (opts.id && layers.has(opts.id)) return layers.get(opts.id);
    var root = document.getElementById('stage') || document.body;
    var layer = document.createElement('div');
    layer.className = 'of-layer ' + (opts.className || '');
    layer.dataset.layer = opts.id || ('layer-' + layers.size);
    layer.style.zIndex = String(opts.z || 50);
    layer.style.pointerEvents = opts.pointerEvents ? 'auto' : 'none';
    root.appendChild(layer);
    if (opts.id) layers.set(opts.id, layer);
    return layer;
  }

  function zone(name) {
    var map = {
      'top': { x: 0.5, y: 0.14 }, 'bottom': { x: 0.5, y: 0.86 },
      'top-left': { x: 0.06, y: 0.14 }, 'top-right': { x: 0.94, y: 0.14 },
      'bottom-left': { x: 0.06, y: 0.86 }, 'bottom-right': { x: 0.94, y: 0.86 },
      'center': { x: 0.5, y: 0.5 }, 'mid-left': { x: 0.06, y: 0.5 }, 'mid-right': { x: 0.94, y: 0.5 }
    };
    return map[name] || map.center;
  }

  var utils = {
    clamp: function (n, min, max) { return Math.min(max, Math.max(min, n)); },
    lerp: function (a, b, t) { return a + (b - a) * t; },
    easeOut: function (t) { return 1 - Math.pow(1 - t, 3); },
    cssVar: function (name, fallback) {
      var value = getComputedStyle(document.documentElement).getPropertyValue(name);
      return (value || '').trim() || fallback;
    },
    formatTime: function (seconds) {
      var s = Math.max(0, seconds || 0);
      var m = Math.floor(s / 60);
      var rest = (s - m * 60);
      return m + ':' + (rest < 10 ? '0' : '') + rest.toFixed(1);
    },
    pick: function (list, index) { return list[index % list.length]; }
  };

  function makeCtx(extension) {
    return {
      get id() { return extension.id; },
      get root() { return document.getElementById('stage') || document.body; },
      get project() { return state.project; },
      get timeline() { return state.timeline; },
      get theme() { return state.theme || {}; },
      get canvas() { return canvas; },
      confirm: function () {
        if (global.confirm) return global.confirm.apply(global, arguments);
        return true;
      },
      layers: { create: createLayer, zone: zone, get: function (id) { return layers.get(id) || null; } },
      on: on, emit: emit, utils: utils,
      getState: function () { return { time: state.time, status: state.status, revision: state.revision }; }
    };
  }

  function registerExtension(def) {
    if (!def || !def.id || typeof def.setup !== 'function') {
      console.warn('[of] invalid extension definition', def);
      return;
    }
    registry.push(def);
    try { def.setup(makeCtx(def)); }
    catch (err) { console.error('[of] extension setup failed: ' + def.id, err); }
  }

  function loadScripts(urls, done) {
    var pending = urls.slice();
    if (!pending.length) { done(); return; }
    pending.forEach(function (src) {
      var tag = document.createElement('script');
      tag.src = src + (src.indexOf('?') >= 0 ? '&' : '?') + 'v=' + Date.now();
      tag.async = false;
      tag.onload = tag.onerror = function () {
        pending = pending.filter(function (item) { return item !== src; });
        if (!pending.length) done();
      };
      document.head.appendChild(tag);
    });
  }

  global.ObsFusion = {
    version: '0.1.0',
    registerExtension: registerExtension,
    extensions: registry,
    on: on,
    emit: emit,
    get state() { return state; },
    setState: function (patch) { Object.assign(state, patch); },
    setCanvas: function (w, h) { canvas.width = w; canvas.height = h; },
    layers: { create: createLayer, zone: zone },
    loadScripts: loadScripts,
    utils: utils
  };
})(window);

})();
