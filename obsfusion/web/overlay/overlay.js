// ObsFusion 叠加层：时间线播放器 + 字幕引擎 + 扩展装载
(function () {
  var OF = window.ObsFusion;
  var stage = document.getElementById('stage');
  var media = document.getElementById('media');
  var sub = document.getElementById('sub');
  var subLine = document.getElementById('subLine');
  var subSpeaker = document.getElementById('subSpeaker');
  var progress = document.getElementById('progress');
  var ticker = document.getElementById('ticker');
  var stateLabel = document.getElementById('stateLabel');
  var bugName = document.getElementById('bugName');
  var bugKicker = document.getElementById('bugKicker');

  var projectId = (location.pathname.match(/^\/overlay\/([^/]+)/) || [])[1] || 'demo';
  var params = new URLSearchParams(location.search);
  // ?preview=1：由控制台通过 postMessage 驱动时间，自己不再跑时钟（否则同屏两套时钟会漂）
  var previewMode = params.get('preview') === '1';
  var timeline = null;
  var project = null;
  var duration = 8;
  var clock = { status: 'idle', base: 0, startedAt: 0, time: 0 };
  var cueIndex = -1;
  var activeEls = new Map();
  var audioPool = new Map();
  var videoEl = null;
  var lastTtsKey = '';
  var rafId = 0;

  function now() { return performance.now(); }

  function currentTime() {
    if (clock.status === 'playing') return clock.base + (now() - clock.startedAt) / 1000;
    return clock.base;
  }

  var stageScale = 1;
  function fitStage() {
    var scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    stageScale = scale || 1;
    stage.style.setProperty('--of-scale', String(stageScale));
    if (window.ObsFusion) window.ObsFusion.scale = function () { return stageScale; };
  }

  function setTicker(text) { ticker.textContent = text; }

  function setStatus(status) {
    stateLabel.innerHTML = status === 'playing' ? 'ON AIR <b>●</b>' : (status === 'paused' ? 'PAUSED<b> ❚❚</b>' : 'IDLE');
    stage.classList.toggle('is-idle', status === 'idle' || !timeline);
  }

  // 注意：水平居中不能写在 transform 里——入场动画也占用 transform，会互相覆盖导致字幕左右跳。
  // 这里统一用"左边缘 + margin-left:-50% 的 width:max-content"实现居中。
  // 注意：水平居中不能写在 transform 里（入场动画也用 transform，会互相覆盖导致字幕左右跳），
  // 也不能用 margin-left:-50%（百分比按父容器宽度算）。这里用 left:50% + JS 写入
  // margin-left:-(自身宽度/2) 做真正居中，宽度变化（换句）时重新写一次。
  var ANCHORS = {
    'bottom-center': { left: '50%', bottom: '8%', centerX: true },
    'bottom-left': { left: '6%', bottom: '8%' },
    'center': { left: '50%', top: '50%', centerX: true, centerY: true },
    'top-center': { left: '50%', top: '10%', centerX: true },
    'top-left': { left: '6%', top: '10%' },
    'top-right': { right: '6%', top: '10%' }
  };

  function buildVideo(v) {
    media.innerHTML = '';
    videoEl = null;
    if (!v || !v.url) return;
    if (/\.(png|jpe?g|webp|gif)$/i.test(v.url)) {
      var img = document.createElement('img');
      img.src = v.url;
      media.appendChild(img);
      return;
    }
    var video = document.createElement('video');
    video.src = v.url;
    video.loop = v.loop !== false;
    video.muted = v.muted !== false;
    video.playsInline = true;
    video.preload = 'auto';
    video.style.opacity = v.opacity === undefined ? 1 : String(v.opacity);
    if (v.fit === 'contain') media.dataset.fit = 'contain';
    media.appendChild(video);
    videoEl = video;
  }

  function applyTheme(theme) {
    theme = theme || {};
    stage.style.setProperty('--accent', theme.accent || '#e8b05a');
    var size = Number(theme.subtitleSize) || 62;
    sub.style.fontSize = size + 'px';
  }

  function prepareCueEl(cue) {
    var el = document.createElement('div');
    el.className = 'of-sub';
    el.dataset.style = cue.style || 'subtitle';
    el.dataset.cueId = cue.id;
    var anchor = ANCHORS[cue.anchor] || ANCHORS['bottom-center'];
    el._centerX = !!anchor.centerX;
    el._centerY = !!anchor.centerY;
    // 锚点用 right/bottom 定位时必须清掉 left/top，否则浏览器沿用上一个 cue 的 left/top 造成错位叠加
    el.style.left = 'auto';
    el.style.top = 'auto';
    el.style.right = 'auto';
    el.style.bottom = 'auto';
    el.style.marginLeft = '0';
    el.style.marginTop = '0';
    el.style.width = 'auto';
    el.style.maxWidth = '72%';
    Object.keys(anchor).forEach(function (key) { el.style[key] = anchor[key]; });
    if (typeof cue.x === 'number') el.style.left = (cue.x * 100) + '%';
    if (typeof cue.y === 'number') { el.style.top = (cue.y * 100) + '%'; el.style.bottom = 'auto'; el.style.transform = cue.transform || 'translate(-50%,-50%)'; }
    var size = Number(cue.size) || Number((project && project.theme && project.theme.subtitleSize)) || 62;
    el.style.fontSize = size + 'px';
    if (cue.speaker) {
      var sp = document.createElement('div');
      sp.className = 'speaker';
      sp.textContent = cue.speaker;
      el.appendChild(sp);
    }
    var panel = document.createElement('div');
    panel.className = 'panel';
    var line = document.createElement('div');
    line.className = 'line';
    line.textContent = cue.text;
    panel.appendChild(line);
    // 卡拉OK 用 background-clip:text 的单层方案：不再复制一层文字（复制层在换行/字距上
    // 很难和底层逐像素对齐，会出现重影），进度由 --p 控制。
    if (cue.karaoke) {
      line.classList.add('is-karaoke');
      line.style.setProperty('--p', '0%');
    }
    el.appendChild(panel);
    return el;
  }

  // 按自身宽度把居中元素推到正中（left:50% 定位，宽度随文案变化）
  function recenter(el) {
    if (!el || !el._centerX) return;
    // 必须用布局尺寸：舞台被 transform:scale() 缩放时，getBoundingClientRect 返回的是视觉尺寸，
    // 拿它写 margin 会在小窗口预览里把字幕推离中心。
    var w = el.offsetWidth;
    var h = el.offsetHeight;
    if (!w) return;
    el.style.marginLeft = (-w / 2) + 'px';
    if (el._centerY) el.style.marginTop = (-h / 2) + 'px';
  }

  function audioFor(cue) {
    if (!cue.audio || !cue.audio.url) return null;
    var key = cue.id + '|' + cue.audio.url;
    if (!audioPool.has(key)) {
      var audio = new Audio();
      audio.src = cue.audio.url;
      audio.preload = 'auto';
      audio.dataset.key = key;
      audioPool.set(key, audio);
    }
    return audioPool.get(key);
  }

  function stopAudio(exceptKey) {
    audioPool.forEach(function (audio) {
      if (audio.dataset.key === exceptKey) return;
      if (!audio.paused) { audio.pause(); }
    });
  }

  function enterCue(cue) {
    var el = activeEls.get(cue.id);
    if (!el) {
      el = prepareCueEl(cue);
      el._dead = false;
      (document.getElementById('stage')).insertBefore(el, document.querySelector('.of-safearea'));
      activeEls.set(cue.id, el);
    }
    recenter(el);
    requestAnimationFrame(function () { el.classList.add('is-on'); });
    if (typeof document.fonts !== 'undefined' && document.fonts.ready) {
      document.fonts.ready.then(function () { if (el.dataset.cueId === cue.id) recenter(el); });
    }
    if (cue.audio && cue.audio.url) {
      var key = cue.id + '|' + cue.audio.url;
      if (key !== lastTtsKey) {
        lastTtsKey = key;
        stopAudio(key);
        var audio = audioFor(cue);
        var offset = Math.max(0, currentTime() - cue.start);
        try {
          audio.currentTime = Math.min(offset, Math.max(0, (cue.audio.duration || offset) - 0.05));
          var gain = Number((project && project.audio && project.audio.ttsGain)) || 1;
          audio.volume = Math.min(1, Math.max(0, gain));
          var p = audio.play();
          if (p && p.catch) p.catch(function () {});
        } catch (_) {}
      }
    }
    OF.emit('cue.enter', { cue: cue, time: currentTime() });
  }

  function leaveCue(cue) {
    var el = activeEls.get(cue.id);
    if (el) {
      el.classList.add('is-leaving');
      el.classList.remove('is-on');
      // 退场瞬间就把 marginLeft 归零，防止残留的居中位移把新一条推到旁边
      el._centerX = false;
      // 不用定时器清理：快速 seek/重入时旧定时器可能删掉同 id 的新元素。
      // is-leaving 让节点立即退出流布局（绝对定位也会占 hit-test 之外的排版高度），
      // 避免同一锚点位置的两条字幕叠在一起。
      el._dead = true;
      setTimeout(function () { if (el._dead && el.parentNode) el.parentNode.removeChild(el); }, 460);
      activeEls.delete(cue.id);
    }
    OF.emit('cue.exit', { cue: cue, time: currentTime() });
  }

  function syncCues(t) {
    var cues = (timeline && timeline.cues) || [];
    var wanted = cues.filter(function (cue) { return t >= cue.start && t < (cue.displayEnd || cue.end); });
    var wantedIds = new Set(wanted.map(function (cue) { return cue.id; }));
    activeEls.forEach(function (el, id) {
      if (wantedIds.has(id)) return;
      var cue = cues.filter(function (item) { return item.id === id; })[0];
      if (cue) leaveCue(cue);
    });
    wanted.forEach(function (cue) {
      if (!activeEls.has(cue.id)) enterCue(cue);
      var el = activeEls.get(cue.id);
      if (cue.karaoke && el) {
        var dur = Math.max(0.2, cue.displayEnd - cue.start);
        var pct = Math.max(0, Math.min(100, ((t - cue.start) / dur) * 100));
        el.style.setProperty('--p', pct.toFixed(2) + '%');
        var lineEl = el.querySelector('.line');
        if (lineEl) lineEl.style.setProperty('--p', pct.toFixed(2) + '%');
      }
    });
    if (!wanted.length && sub.classList.contains('is-on')) sub.classList.remove('is-on');
    return wanted;
  }

  function tick() {
    rafId = requestAnimationFrame(tick);
    var t = currentTime();
    OF.setState({ time: t, status: clock.status });
    OF.emit('tick', { time: t, status: clock.status });
    var active = syncCues(t);
    progress.style.width = Math.min(100, (t / duration) * 100) + '%';
    if (active.length) setTicker('cue ' + active[0].id + '  ·  ' + t.toFixed(2) + 's / ' + duration.toFixed(1) + 's');
    else setTicker('timeline  ·  ' + t.toFixed(2) + 's / ' + duration.toFixed(1) + 's');
    if (t > duration + 0.4 && clock.status === 'playing') {
      clock.status = 'idle'; clock.base = 0;
      setStatus('idle');
    }
  }

  function applyState(state) {
    if (!state) return;
    var wasPlaying = clock.status === 'playing';
    clock.status = state.status;
    clock.base = Number(state.time) || 0;
    clock.startedAt = now();
    setStatus(state.status);
    if (videoEl) {
      if (state.status === 'playing') {
        try {
          if (Math.abs((videoEl.currentTime || 0) - clock.base % (videoEl.duration || 30)) > 0.4) videoEl.currentTime = clock.base % (videoEl.duration || 30);
          videoEl.play().catch(function () {});
        } catch (_) {}
      } else {
        try { videoEl.pause(); } catch (_) {}
        if (state.status === 'idle') { try { videoEl.currentTime = 0; } catch (_) {} }
      }
    }
    if (state.status !== 'playing') {
      stopAudio(null);
      lastTtsKey = '';
    } else if (!wasPlaying) {
      lastTtsKey = '';
    }
  }

  function applyTimeline(payload) {
    if (!payload) return;
    timeline = payload;
    project = payload.project || project;
    var cues = payload.cues || [];
    duration = Math.max(8, Number(payload.duration) || (cues.length ? cues[cues.length - 1].displayEnd || cues[cues.length - 1].end : 8));
    applyTheme(project && project.theme);
    buildVideo(payload.video);
    var t = currentTime();
    clock.base = 0; clock.startedAt = now();
    activeEls.forEach(function (el) { el._dead = true; if (el.parentNode) el.parentNode.removeChild(el); });
    activeEls.clear();
    // 清掉可能残留的字幕节点（历史上重入定时器可能留下孤儿）
    document.querySelectorAll('.of-sub').forEach(function (el) { if (el.parentNode && !el.classList.contains('is-on')) el.parentNode.removeChild(el); });
    cueIndex = -1;
    applyState({ status: clock.status, time: t });
  }

  function loadTimeline() {
    return fetch('/api/projects/' + encodeURIComponent(projectId) + '/timeline', { cache: 'no-store' })
      .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
      .then(function (payload) {
        applyTimeline(payload);
        var p = payload.project || {};
        bugName.textContent = (p.title || projectId).toUpperCase();
        bugKicker.textContent = 'ObsFusion · ' + projectId;
        OF.setState({ timeline: payload, project: p, revision: payload.revision, theme: p.theme || {} });
        OF.emit('timeline.ready', payload);
        var extList = (payload.extensions || []).filter(function (ext) { return ext.enabled && ext.client; });
        OF.loadScripts(extList.map(function (ext) { return ext.client; }), function () {
          OF.emit('extensions.ready', extList);
          var st = document.querySelector('.of-state');
          if (st) st.title = 'extensions: ' + extList.map(function (ext) { return ext.id; }).join(', ');
        });
        return payload;
      })
      .catch(function (err) {
        setTicker('load failed · ' + err.message);
        console.error('[of] timeline load failed', err);
      });
  }

  function connect() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var ws = new WebSocket(proto + '//' + location.host + '/ws?scene=' + encodeURIComponent(projectId) + '&role=overlay');
    var retryTimer = null;
    function scheduleRetry() {
      if (retryTimer) return;
      retryTimer = setTimeout(function () { retryTimer = null; connect(); }, 2000);
    }
    ws.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (_) { return; }
      if (msg.type === 'hello') {
        if (msg.timeline) applyTimeline(msg.timeline);
        applyState(msg.state);
      } else if (msg.type === 'state') {
        applyState(msg.state);
      } else if (msg.type === 'reload') {
        loadTimeline();
      } else if (msg.type === 'event') {
        OF.emit(msg.name, msg.payload);
      } else if (msg.type === 'log') {
        console.log('[of:server]', msg.level, msg.message);
      }
    };
    ws.onclose = scheduleRetry;
    ws.onerror = function () { try { ws.close(); } catch (_) {} };
  }

  // 控制台预览：由父页面下发 {t, status}，叠加层只负责按这个时刻渲染
  window.addEventListener('message', function (event) {
    if (event.origin !== location.origin) return;
    if (!previewMode) return;
    var data = event.data || {};
    if (data.__obsfusion !== 'sync') return;
    var t = Number(data.t);
    if (!isFinite(t)) return;
    clock.status = data.status === 'playing' ? 'playing' : (data.status === 'paused' ? 'paused' : 'idle');
    clock.base = t;
    clock.startedAt = now();
  });

  window.addEventListener('resize', fitStage);
  fitStage();
  setStatus('idle');
  applyTheme({});
  loadTimeline().then(function () { connect(); });
  rafId = requestAnimationFrame(tick);

  if (params.get('debug') === '1') stage.classList.add('debug');
  OF.on('reload', function () { loadTimeline(); });
})();