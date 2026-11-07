// 章节导航扩展（浏览器端）——把结构化文案派生的章节渲染成叠加层导航条
// 通过 ObsFusion 扩展 API 注册；所有节点由 document.createElement 构建，外部文本一律 textContent（防 XSS）。
// 渲染策略：只在「可见窗口 / 章节集合 / 配置」变化时重建列表，tick 仅更新高亮与进度（不每帧重排整棵树）。
(function () {
  if (!window.ObsFusion) return;

  var STYLE_ID = 'of-chapters-style';
  var SAFE_INSET = 96;            // 叠加层安全区：inset 5%，1920x1080 下即 96px
  var CANVAS_HEIGHT = 1080;
  var SUBTITLE_RESERVE = 300;     // 底部字幕区预留高度（字幕 bottom:8%、字号约 62px，含两行与说话人）
  var DEFAULTS = {
    position: 'left', width: 340, fontSize: 22, opacity: 0.9,
    maxItems: 6, autoSplit: 6, showProgress: true, accent: '#E8B05A', offsetY: 300
  };

  // 样式一次性注入；只用 class 与自定义属性，主题色/字号/透明度全部走变量
  var CSS = [
    '.of-chapters{position:absolute;left:96px;top:300px;width:340px;pointer-events:none;box-sizing:border-box;',
    'font-family:var(--of-sans);color:#e9ecf2;--ch-accent:#e8b05a;--ch-opacity:.9;padding:16px 18px 14px;border-radius:14px;overflow:hidden;',
    'background:linear-gradient(135deg,rgba(8,10,16,.82),rgba(10,12,18,.46) 58%,rgba(8,10,16,.6));',
    'border:1px solid rgba(232,176,90,.16);box-shadow:0 26px 64px rgba(0,0,0,.46),inset 0 1px 0 rgba(255,255,255,.06);',
    '-webkit-backdrop-filter:blur(16px) saturate(125%);backdrop-filter:blur(16px) saturate(125%);',
    'opacity:0;transform:translateY(2px);transition:opacity .5s var(--of-ease),transform .6s var(--of-ease-out);}',
    '.of-chapters.is-ready{opacity:var(--ch-opacity,.9);transform:translateY(0);}',
    '.of-chapters.is-right{left:auto;right:96px;}',
    '.of-chapters.is-empty{display:none;}',
    '.of-chapters::before{content:"";position:absolute;left:0;top:16px;bottom:16px;width:2px;border-radius:2px;',
    'background:linear-gradient(180deg,transparent,var(--ch-accent) 24%,rgba(232,176,90,.22) 76%,transparent);opacity:.8;}',
    '.of-chapters.is-right::before{left:auto;right:0;}',
    '.of-ch-head{display:flex;align-items:center;gap:9px;padding:0 2px 11px;margin-bottom:8px;border-bottom:1px solid rgba(255,255,255,.07);}',
    '.of-ch-dot{width:6px;height:6px;border-radius:50%;background:var(--ch-accent);box-shadow:0 0 10px var(--ch-accent);flex:none;}',
    '.of-ch-kicker{font-family:var(--of-mono);font-size:.56em;letter-spacing:.32em;text-transform:uppercase;color:rgba(233,236,242,.46);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
    '.of-ch-count{margin-left:auto;font-family:var(--of-mono);font-size:.6em;letter-spacing:.12em;color:rgba(232,176,90,.88);font-variant-numeric:tabular-nums;flex:none;}',
    '.of-ch-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px;}',
    '.of-ch-row{position:relative;display:flex;align-items:flex-start;gap:.5em;padding:.42em .5em;border-radius:8px;overflow:hidden;',
    'opacity:.5;transition:opacity .4s var(--of-ease),background-color .4s var(--of-ease);}',
    '.of-ch-row.is-done{opacity:.34;}',
    '.of-ch-row.is-on{opacity:1;background:rgba(232,176,90,.075);}',
    '.of-ch-halo{position:absolute;inset:0;border-radius:8px;opacity:0;pointer-events:none;z-index:0;',
    'background:radial-gradient(120% 150% at 8% 50%,rgba(232,176,90,.26),rgba(232,176,90,0) 62%);}',
    '.of-chapters.is-right .of-ch-halo{background:radial-gradient(120% 150% at 92% 50%,rgba(232,176,90,.26),rgba(232,176,90,0) 62%);}',
    '.of-ch-index{position:relative;z-index:1;flex:none;width:2em;font-family:var(--of-mono);font-size:.66em;line-height:2.1;',
    'letter-spacing:.06em;color:rgba(233,236,242,.32);font-variant-numeric:tabular-nums;transition:color .35s var(--of-ease);}',
    '.of-ch-row.is-on .of-ch-index{color:var(--ch-accent);text-shadow:0 0 14px rgba(232,176,90,.65);}',
    '.of-ch-body{position:relative;z-index:1;flex:1;min-width:0;}',
    '.of-ch-title{font-size:.84em;line-height:1.55;font-weight:500;color:rgba(233,236,242,.76);',
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:color .35s var(--of-ease);}',
    '.of-ch-row.is-on .of-ch-title{color:#fff;font-weight:600;}',
    '.of-ch-row.is-done .of-ch-title{color:rgba(233,236,242,.4);}',
    '.of-ch-track{margin-top:.4em;height:1.5px;border-radius:2px;background:rgba(255,255,255,.08);overflow:hidden;}',
    '.of-ch-fill{display:block;height:100%;width:100%;transform:scaleX(0);transform-origin:left center;',
    'background:linear-gradient(90deg,rgba(232,176,90,.32),var(--ch-accent));box-shadow:0 0 10px rgba(232,176,90,.55);}',
    '.of-chapters.no-progress .of-ch-track{display:none;}',
    '.of-ch-row.is-done .of-ch-fill{opacity:.4;}',
    '.of-ch-more{font-family:var(--of-mono);font-size:.54em;letter-spacing:.2em;color:rgba(233,236,242,.24);text-align:center;padding:.34em 0 .18em;}',
    '@media (prefers-reduced-motion: reduce){',
    '.of-chapters,.of-ch-row,.of-ch-title,.of-ch-fill{transition:none !important;animation:none !important;}',
    '.of-chapters{transform:none;}}'
  ].join('');

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var tag = document.createElement('style');
    tag.id = STYLE_ID;
    tag.textContent = CSS;
    document.head.appendChild(tag);
  }

  // 尊重 prefers-reduced-motion：该媒体查询下不发起任何 WAAPI 动画
  function prefersReducedMotion() {
    try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
    catch (_) { return false; }
  }

  function clamp(value, min, max, fallback) {
    var n = Number(value);
    if (!Number.isFinite(n)) n = Number.isFinite(fallback) ? fallback : min;
    return Math.min(max, Math.max(min, n));
  }

  function pad2(value) {
    var n = Math.max(0, Math.round(Number(value) || 0));
    return n < 10 ? '0' + n : String(n);
  }

  // 与服务端 normalizeConfig 对齐：客户端只做展示相关的收敛，不改动派生规则
  function normalizeConfig(raw) {
    raw = raw || {};
    var accent = String(raw.accent === undefined || raw.accent === null ? '' : raw.accent).trim();
    if (!/^#[0-9a-fA-F]{3,8}$/.test(accent)) accent = DEFAULTS.accent;
    return {
      position: raw.position === 'right' ? 'right' : 'left',
      width: Math.round(clamp(raw.width, 200, 640, DEFAULTS.width)),
      fontSize: Math.round(clamp(raw.fontSize, 14, 48, DEFAULTS.fontSize)),
      opacity: clamp(raw.opacity, 0.2, 1, DEFAULTS.opacity),
      maxItems: Math.round(clamp(raw.maxItems, 1, 12, DEFAULTS.maxItems)),
      autoSplit: Math.round(clamp(raw.autoSplit, 1, 30, DEFAULTS.autoSplit)),
      showProgress: raw.showProgress !== false,
      accent: accent,
      offsetY: Math.round(clamp(raw.offsetY, 120, 560, DEFAULTS.offsetY))
    };
  }

  // 浏览器端 ctx 没有 config，扩展配置挂在时间线的 extensions 列表里
  function readConfig(ctx) {
    var list = (ctx.timeline && ctx.timeline.extensions) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === 'chapters' && list[i].config) return list[i].config;
    }
    return {};
  }

  function tidyText(value) {
    return String(value === undefined || value === null ? '' : value)
      .replace(/\r\n?/g, '\n').replace(/\s+/g, ' ').trim();
  }

  function localChapterTitle(cue) {
    var text = tidyText(cue && cue.text).replace(/^[\s#]+/, '').replace(/[\s#]+$/, '');
    return text.slice(0, 60) || '未命名章节';
  }

  // 路由不可用时的本地兜底：规则与 server.mjs 的 buildChapters 完全一致
  function buildFromCues(cues, autoSplit) {
    var list = [];
    for (var i = 0; i < (cues || []).length; i++) {
      var cue = cues[i] || {};
      var start = Number(cue.start);
      if (!Number.isFinite(start) || start < 0) start = 0;
      var end = Number(cue.end);
      if (!Number.isFinite(end) || end < start) end = start + 0.1;
      list.push({
        id: String(cue.id || ('c' + (i + 1))),
        start: start, end: end,
        text: tidyText(cue.text),
        speaker: tidyText(cue.speaker)
      });
    }
    if (!list.length) return [];
    list.sort(function (a, b) { return a.start - b.start; });

    var marks = [];
    for (var m = 0; m < list.length; m++) {
      if (list[m].speaker === '章节' || /^#/.test(list[m].text.replace(/^\s+/, ''))) marks.push(m);
    }

    var groups = [];
    if (marks.length) {
      // 与服务端同一套区间语义：第一章从 0 起，之后每章从下一个标记处起算，末章到末尾
      var bounds = [0];
      for (var b = 1; b < marks.length; b++) bounds.push(marks[b]);
      bounds.push(list.length);
      for (var k = 0; k < marks.length; k++) {
        groups.push({ title: localChapterTitle(list[marks[k]]), cues: list.slice(bounds[k], bounds[k + 1]) });
      }
    } else {
      var size = Math.round(clamp(autoSplit, 1, 30, DEFAULTS.autoSplit));
      for (var s = 0; s < list.length; s += size) {
        var slice = list.slice(s, s + size);
        var head = '';
        for (var t = 0; t < slice.length; t++) {
          if (slice[t].text) { head = slice[t].text; break; }
        }
        groups.push({ title: head ? head.slice(0, 12) : ('第 ' + (groups.length + 1) + ' 章'), cues: slice });
      }
    }

    return groups.map(function (group, gi) {
      var start = group.cues[0].start;
      var end = group.cues[0].end;
      var ids = [];
      for (var c = 0; c < group.cues.length; c++) {
        if (group.cues[c].end > end) end = group.cues[c].end;
        ids.push(group.cues[c].id);
      }
      return { index: gi + 1, title: group.title, start: start, end: end, cueIds: ids };
    });
  }

  function setup(ctx) {
    try {
      injectStyle();

      // 叠加层刷新 / 控制台重载会再次 setup，先拆掉上一次的实例，避免重复监听与重复面板
      var previous = window.__ofChapters;
      if (previous && typeof previous.dispose === 'function') {
        try { previous.dispose(); } catch (_) {}
      }

      var layer = ctx.layers.create({ id: 'chapters', className: 'of-chapters-layer', z: 58 });
      var panel = document.createElement('div');
      panel.className = 'of-chapters is-left is-empty';
      panel.setAttribute('role', 'navigation');
      panel.setAttribute('aria-label', '章节导航');

      var head = document.createElement('div');
      head.className = 'of-ch-head';
      var dot = document.createElement('span');
      dot.className = 'of-ch-dot';
      var kicker = document.createElement('span');
      kicker.className = 'of-ch-kicker';
      kicker.textContent = '章节 / CHAPTERS';
      var counter = document.createElement('span');
      counter.className = 'of-ch-count';
      counter.textContent = '-- / --';
      head.appendChild(dot);
      head.appendChild(kicker);
      head.appendChild(counter);

      var list = document.createElement('ol');
      list.className = 'of-ch-list';

      panel.appendChild(head);
      panel.appendChild(list);
      layer.appendChild(panel);

      var config = normalizeConfig(readConfig(ctx));
      var chapters = [];
      var rows = [];
      var activeIndex = 0;
      var renderedKey = '';       // 「起始行:可见条数」快照，变化才重建列表
      var viewCap = config.maxItems;
      var lastProgress = -1;
      var unsubscribers = [];
      var disposed = false;

      function isRight() { return config.position === 'right'; }

      function on(name, fn) {
        var off = ctx.on(name, fn);
        if (typeof off === 'function') unsubscribers.push(off);
      }

      function applyConfig() {
        panel.classList.toggle('is-right', isRight());
        panel.classList.toggle('is-left', !isRight());
        panel.classList.toggle('no-progress', !config.showProgress);
        panel.style.width = config.width + 'px';
        panel.style.top = config.offsetY + 'px';
        panel.style.fontSize = config.fontSize + 'px';
        panel.style.setProperty('--ch-opacity', String(config.opacity));
        panel.style.setProperty('--ch-accent', config.accent);
        // 纵向只能用「安全区上沿 → 字幕区上沿」之间的空间：既不出安全区，也不压底部字幕
        var safeBottom = CANVAS_HEIGHT - SAFE_INSET;                 // 984
        var subtitleTop = CANVAS_HEIGHT - SUBTITLE_RESERVE;          // 780，字幕区（bottom 8% + 62px 字号）的上沿
        var maxHeight = Math.max(140, Math.min(safeBottom, subtitleTop) - config.offsetY);
        panel.style.maxHeight = maxHeight + 'px';
        // 可见条数再按实际高度收敛：宁可少显示几章，也不让列表被裁得半行
        var rowHeight = Math.round(config.fontSize * 2.8) + 4;
        var fit = Math.max(1, Math.floor((maxHeight - 44) / rowHeight));
        viewCap = Math.min(config.maxItems, fit);
      }

      function makeRow(chapter, order) {
        var li = document.createElement('li');
        li.className = 'of-ch-row';
        li.dataset.index = String(chapter.index);

        var halo = document.createElement('span');
        halo.className = 'of-ch-halo';

        var index = document.createElement('span');
        index.className = 'of-ch-index';
        index.textContent = pad2(chapter.index);

        var body = document.createElement('div');
        body.className = 'of-ch-body';

        var title = document.createElement('div');
        title.className = 'of-ch-title';
        title.textContent = chapter.title;   // 外部文本一律 textContent
        title.title = chapter.title;

        var track = document.createElement('div');
        track.className = 'of-ch-track';
        var fill = document.createElement('i');
        fill.className = 'of-ch-fill';
        track.appendChild(fill);

        body.appendChild(title);
        body.appendChild(track);
        li.appendChild(halo);
        li.appendChild(index);
        li.appendChild(body);

        li._order = order;          // 用数组下标做高亮判定，服务端 index 只用于展示
        li._fill = fill;
        li._title = title;
        return li;
      }

      function moreRow(label) {
        var li = document.createElement('li');
        li.className = 'of-ch-more';
        li.textContent = label;
        return li;
      }

      function visibleFrom() {
        var total = chapters.length;
        var max = viewCap;
        if (total <= max) return 0;
        return Math.min(Math.max(activeIndex - Math.floor(max / 2), 0), total - max);
      }

      function rebuild(from) {
        var max = viewCap;
        var end = Math.min(chapters.length, from + max);
        list.textContent = '';
        rows = [];
        if (from > 0) list.appendChild(moreRow('前面 ' + from + ' 章'));
        for (var i = from; i < end; i++) {
          var li = makeRow(chapters[i], i);
          list.appendChild(li);
          rows.push(li);
        }
        var rest = chapters.length - end;
        if (rest > 0) list.appendChild(moreRow('后面 ' + rest + ' 章'));
        if (!prefersReducedMotion()) {
          for (var r = 0; r < rows.length; r++) {
            try {
              rows[r].animate(
                [{ transform: 'translateY(5px)' }, { transform: 'translateY(0)' }],
                { duration: 320, delay: Math.min(r, 6) * 24, easing: 'cubic-bezier(.16,1,.3,1)' }
              );
            } catch (_) {}
          }
        }
      }

      // 只有可见窗口变化才重建 DOM；否则仅改 class
      function renderWindow() {
        if (!chapters.length) {
          list.textContent = '';
          rows = [];
          renderedKey = '';
          panel.classList.add('is-empty');
          return;
        }
        panel.classList.remove('is-empty');
        var from = visibleFrom();
        var key = from + ':' + viewCap;
        if (key !== renderedKey) {
          renderedKey = key;
          rebuild(from);
        }
        paint();
      }

      function paint() {
        for (var i = 0; i < rows.length; i++) {
          var li = rows[i];
          var isOn = li._order === activeIndex;
          var isDone = li._order < activeIndex;
          li.classList.toggle('is-on', isOn);
          li.classList.toggle('is-done', isDone);
          if (isOn) li.setAttribute('aria-current', 'true');
          else li.removeAttribute('aria-current');
          if (li._fill) li._fill.style.transform = 'scaleX(' + (isDone ? 1 : 0) + ')';
        }
        var current = chapters[activeIndex];
        counter.textContent = pad2(current ? current.index : activeIndex + 1) + ' / ' + pad2(chapters.length);
        lastProgress = -1;
      }

      function indexAt(time) {
        var total = chapters.length;
        if (!total) return 0;
        var i = Math.min(Math.max(activeIndex, 0), total - 1);
        while (i > 0 && time < chapters[i].start) i -= 1;
        while (i < total - 1 && time >= chapters[i + 1].start) i += 1;
        return i;
      }

      function updateProgress(time) {
        if (!config.showProgress || !chapters.length) return;
        var chapter = chapters[activeIndex];
        if (!chapter) return;
        var span = Math.max(0.001, chapter.end - chapter.start);
        var progress = clamp((time - chapter.start) / span, 0, 1);
        if (Math.abs(progress - lastProgress) < 0.004) return;
        lastProgress = progress;
        for (var i = 0; i < rows.length; i++) {
          if (rows[i]._order !== activeIndex) continue;
          if (rows[i]._fill) rows[i]._fill.style.transform = 'scaleX(' + progress.toFixed(4) + ')';
          return;
        }
      }

      // 进入新章节：面板轻微滑入 + 当前条高亮扩散（reduced-motion 下跳过）
      function animateEnter() {
        if (prefersReducedMotion()) return;
        try {
          var shift = isRight() ? 10 : -10;
          panel.animate(
            [{ transform: 'translateX(' + shift + 'px)' }, { transform: 'translateX(0)' }],
            { duration: 420, easing: 'cubic-bezier(.16,1,.3,1)' }
          );
          for (var i = 0; i < rows.length; i++) {
            if (rows[i]._order !== activeIndex) continue;
            var halo = rows[i].querySelector('.of-ch-halo');
            if (halo) {
              halo.animate(
                [{ opacity: 0 }, { opacity: 1, offset: 0.26 }, { opacity: 0.34 }],
                { duration: 780, easing: 'cubic-bezier(.22,.61,.36,1)' }
              );
            }
            if (rows[i]._title) {
              rows[i]._title.animate(
                [{ opacity: 0, transform: 'translateX(' + (isRight() ? 8 : -8) + 'px)' }, { opacity: 1, transform: 'translateX(0)' }],
                { duration: 460, easing: 'cubic-bezier(.16,1,.3,1)' }
              );
            }
            break;
          }
        } catch (_) {}
      }

      function applyChapters(list2) {
        var source = Array.isArray(list2) ? list2 : [];
        chapters = [];
        for (var i = 0; i < source.length; i++) {
          var item = source[i];
          if (!item || typeof item !== 'object') continue;
          chapters.push({
            index: Number(item.index) || (chapters.length + 1),
            title: String(item.title === undefined || item.title === null ? '未命名章节' : item.title),
            start: Number(item.start) || 0,
            end: Number(item.end) || 0,
            cueIds: Array.isArray(item.cueIds) ? item.cueIds.slice() : []
          });
        }
        var state = ctx.getState() || {};
        var time = Number(state.time) || 0;
        activeIndex = 0;
        activeIndex = chapters.length ? indexAt(time) : 0;
        renderedKey = '';
        renderWindow();
        panel.classList.add('is-ready');
        updateProgress(time);
        ctx.emit('chapters.ready', { total: chapters.length });
      }

      function projectId() {
        var project = ctx.project || (ctx.timeline && ctx.timeline.project);
        if (project && project.id) return String(project.id);
        var matched = location.pathname.match(/^\/overlay\/([^/]+)/);
        return (matched && matched[1]) || 'demo';
      }

      function loadChapters() {
        var url = '/x/chapters/chapters?scene=' + encodeURIComponent(projectId());
        fetch(url, { cache: 'no-store' })
          .then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
          })
          .then(function (data) {
            if (disposed) return;
            applyChapters(data && data.chapters);
          })
          .catch(function (err) {
            if (disposed) return;
            // 服务端能力不可用时用时间线 cue 本地派生，保证叠加层不空场
            applyChapters(buildFromCues((ctx.timeline && ctx.timeline.cues) || [], config.autoSplit));
            console.warn('[of:chapters] 服务端章节不可用，已改用本地派生:', err && err.message);
          });
      }

      applyConfig();
      loadChapters();

      on('tick', function (state) {
        if (disposed || !chapters.length) return;
        var time = Number(state && state.time);
        if (!Number.isFinite(time)) return;
        var next = indexAt(time);
        if (next !== activeIndex) {
          activeIndex = next;
          renderWindow();
          animateEnter();
        }
        updateProgress(time);
      });

      on('timeline.ready', function () {
        config = normalizeConfig(readConfig(ctx));
        applyConfig();
        loadChapters();
      });

      on('project.save', function () {
        loadChapters();
      });

      // 服务端 onConfigChange 广播过来的归一化配置
      on('chapters.config', function (next) {
        config = normalizeConfig(next);
        viewCap = config.maxItems;
        applyConfig();
        renderedKey = '';
        renderWindow();
        loadChapters();
      });

      function dispose() {
        disposed = true;
        for (var i = 0; i < unsubscribers.length; i++) {
          try { unsubscribers[i](); } catch (_) {}
        }
        unsubscribers = [];
        if (panel.parentNode) panel.parentNode.removeChild(panel);
        if (window.__ofChapters === handle) window.__ofChapters = null;
      }

      var handle = { dispose: dispose };
      window.__ofChapters = handle;

      ctx.emit('extension.ready', { id: 'chapters' });
    } catch (err) {
      // 扩展自身失败不影响叠加层主流程
      console.error('[of:chapters] setup failed', err);
    }
  }

  ObsFusion.registerExtension({ id: 'chapters', setup: setup });
})();
