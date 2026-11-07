/* ObsFusion 控制台：时间线编辑 + 传输控制 + 扩展配置 + OBS 推送
   纯 ES2022，零依赖；用户文本一律走 textContent，不拼 innerHTML。 */

const $ = function (id) { return document.getElementById(id); };
const $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

const VOICE_FALLBACK = ['Microsoft Huihui Desktop', 'Microsoft Yaoyao Desktop', 'Microsoft Zira Desktop'];
const ANCHOR_LABEL = { 'bottom-center': '底部居中', 'bottom-left': '底部居左', center: '画面中央', 'top-center': '顶部居中', 'top-left': '左上', 'top-right': '右上' };

const state = {
  projectId: null, project: null, timeline: null, revision: 0,
  projects: [], extensions: [], voices: [],
  transport: { status: 'idle', time: 0, at: Date.now() },
  selectedCueId: null, dirty: false, saving: false, saveTimer: null,
  tab: 'cue', activeExtensionId: null, safeArea: false,
  obs: null, obsInfo: null, health: null, viewers: 0,
  ws: null, wsTimer: null, drag: null, duration: 8
};

/* ── 基础工具 ─────────────────────────────────────────── */
function fmt(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s.toFixed(1);
}
function fmtClock(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec - m * 60);
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}
function stamp() {
  const d = new Date();
  const pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function round(n, digits) {
  const f = Math.pow(10, digits || 0);
  return Math.round((Number(n) || 0) * f) / f;
}
function clamp(n, min, max) { return Math.min(max, Math.max(min, n)); }

async function api(path, opts) {
  opts = opts || {};
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!res.ok) {
    const detail = data && (data.error || data.message) ? (data.error || data.message) : ('HTTP ' + res.status);
    throw new Error(detail);
  }
  return data;
}

function toast(message, kind) {
  const stack = $('toastStack');
  if (!stack) return;
  const node = document.createElement('div');
  node.className = 'toast';
  node.dataset.kind = kind || 'info';
  node.textContent = message;
  stack.appendChild(node);
  while (stack.children.length > 4) stack.removeChild(stack.firstChild);
  setTimeout(function () {
    node.classList.add('is-out');
    setTimeout(function () { node.remove(); }, 300);
  }, 2800);
}

function pushLog(level, message) {
  const stream = $('logStream');
  if (!stream) return;
  const empty = stream.querySelector('.log-empty');
  if (empty) empty.remove();
  const row = document.createElement('div');
  row.className = 'log-line';
  row.dataset.level = level || 'info';
  const time = document.createElement('time');
  time.textContent = stamp();
  const text = document.createElement('p');
  text.textContent = message;
  row.appendChild(time);
  row.appendChild(text);
  stream.appendChild(row);
  while (stream.children.length > 220) stream.removeChild(stream.firstChild);
  $('logCount').textContent = String(stream.children.length);
  stream.scrollTop = stream.scrollHeight;
}

function setPill(id, status, value) {
  const pill = $(id);
  if (!pill) return;
  pill.dataset.state = status;
  const em = pill.querySelector('em');
  if (em && value !== undefined) em.textContent = value;
}

/* ── 传输时钟 ─────────────────────────────────────────── */
function transportTime() {
  const t = state.transport;
  let value = t.time;
  if (t.status === 'playing') value += (Date.now() - t.at) / 1000;
  const dur = state.duration || 8;
  if (t.status === 'playing' && dur > 0) {
    const looped = value % (dur + 0.4);
    value = looped > dur ? 0 : looped;
  }
  return Math.max(0, value);
}

function syncTransport(remote) {
  if (!remote) return;
  state.transport = { status: remote.status || 'idle', time: Number(remote.time) || 0, at: Date.now() };
  document.body.dataset.playing = String(state.transport.status === 'playing');
  const badge = $('transportStatus');
  if (badge) {
    badge.dataset.state = state.transport.status;
    badge.textContent = state.transport.status === 'playing' ? '播放中' : (state.transport.status === 'paused' ? '暂停' : '待机');
  }
}

async function sendTransport(action, time) {
  if (!state.projectId) return;
  const body = { action: action };
  if (typeof time === 'number') body.time = time;
  try {
    const res = await api('/api/projects/' + encodeURIComponent(state.projectId) + '/transport', { method: 'POST', body: body });
    syncTransport(res.state);
    paintTransport();
  } catch (err) {
    toast('传输控制失败：' + err.message, 'bad');
  }
}

function paintTransport() {
  let now = transportTime();
  // 到达片尾自动收尾（服务端不掌握时长，由控制台统一判定），否则时钟会一直越过 duration
  if (state.transport.status === 'playing' && state.duration && now > state.duration + 0.05 && !state.stopping) {
    state.stopping = true;
    sendTransport('stop').finally(function () { setTimeout(function () { state.stopping = false; }, 400); });
    now = state.duration;
  }
  const tc = $('tcNow');
  if (tc) tc.textContent = fmt(now);
  const total = $('tcTotal');
  if (total) total.textContent = fmt(state.duration);
  const ratio = state.duration ? clamp(now / state.duration, 0, 1) : 0;
  const head = $('tlPlayhead');
  const prog = $('tlProgress');
  if (head) head.style.left = (ratio * 100).toFixed(3) + '%';
  if (prog) prog.style.width = (ratio * 100).toFixed(3) + '%';
  const active = activeCueAt(now);
  markActiveBlocks(active ? active.id : null);
  syncPreview(now);
}

function activeCueAt(t) {
  if (!state.timeline || !state.timeline.cues) return null;
  const cues = state.timeline.cues;
  for (let i = 0; i < cues.length; i++) {
    const end = cues[i].displayEnd || cues[i].end;
    if (t >= cues[i].start && t <= end) return cues[i];
  }
  return null;
}

function markActiveBlocks(id) {
  $$('.cue-block').forEach(function (node) {
    node.classList.toggle('is-live', node.dataset.cueId === id);
  });
}

/* ── 数据加载 ─────────────────────────────────────────── */
async function loadHealth() {
  try {
    const res = await api('/api/health');
    state.health = res;
    state.obs = res.obs || null;
    setPill('pillServer', 'ok', res.version || 'ok');
    state.viewers = res.viewers || 0;
    setPill('pillViewers', state.viewers > 0 ? 'ok' : 'idle', String(state.viewers));
    paintObs(res.obs);
  } catch (err) {
    setPill('pillServer', 'bad', '离线');
    setPill('pillObs', 'bad', '未知');
  }
}

async function loadProjects() {
  const res = await api('/api/projects');
  state.projects = res.projects || [];
  renderScenes();
  return state.projects;
}

async function loadExtensions() {
  const res = await api('/api/extensions');
  state.extensions = res.extensions || [];
  renderExtList();
  if (!state.activeExtensionId && state.extensions.length) state.activeExtensionId = state.extensions[0].id;
  renderExtPicker();
  renderExtConfig();
}

async function loadVoices() {
  try {
    const res = await api('/api/voices');
    state.voices = (res.voices || []).filter(function (v) { return v && v.name && v.enabled !== false; });
  } catch (_) {
    state.voices = [];
  }
  if (!state.voices.length) state.voices = VOICE_FALLBACK.slice();
  renderVoiceOptions();
}

async function openProject(id, opts) {
  opts = opts || {};
  if (!id) return;
  state.projectId = id;
  state.selectedCueId = null;
  state.dirty = false;
  setPill('pillServer', 'busy', '载入');
  try {
    const loaded = await api('/api/projects/' + encodeURIComponent(id));
    state.project = loaded.project;
    state.revision = loaded.revision || 0;
    await loadTimeline();
    connectSocket();
    paintStage(true);
    paintSceneHeader();
    renderScenes();
    selectCue(null);
    pushLog('info', '已打开场景 ' + id + ' · rev ' + state.revision);
  } catch (err) {
    toast('打开场景失败：' + err.message, 'bad');
    setPill('pillServer', 'bad', '错误');
  }
}

async function loadTimeline() {
  if (!state.projectId) return;
  const tl = await api('/api/projects/' + encodeURIComponent(state.projectId) + '/timeline');
  state.timeline = tl;
  state.revision = tl.revision || state.revision;
  state.duration = Math.max(1, Number(tl.duration) || 8);
  if (tl.extensionsState && tl.extensionsState.length) {
    state.extensions = tl.extensionsState;
    renderExtList();
  }
  renderTimeline();
  renderCueList();
  paintTransport();
  $('cueCount').textContent = String((tl.cues || []).length);
  $('durTotal').textContent = fmt(state.duration);
  const meta = $('sceneMeta');
  if (meta) meta.textContent = (tl.cues || []).length + ' 条 / ' + fmt(state.duration);
}

/* ── 场景列表 ─────────────────────────────────────────── */
function renderScenes() {
  const list = $('sceneList');
  const menu = $('sceneMenu');
  list.innerHTML = '';
  menu.innerHTML = '';
  if (!state.projects.length) {
    const empty = document.createElement('div');
    empty.className = 'log-empty';
    empty.textContent = '还没有场景，点右上角新建。';
    list.appendChild(empty);
    return;
  }
  state.projects.forEach(function (item) {
    const row = document.createElement('button');
    row.className = 'scene-row' + (item.id === state.projectId ? ' is-active' : '');
    row.type = 'button';
    const name = document.createElement('b');
    name.textContent = item.title || item.id;
    const meta = document.createElement('span');
    meta.textContent = item.id + ' · ' + (item.cues || 0) + ' 条';
    const dur = document.createElement('em');
    dur.textContent = fmt(item.duration || 0);
    row.appendChild(name); row.appendChild(meta); row.appendChild(dur);
    row.addEventListener('click', function () { openProject(item.id); });
    list.appendChild(row);

    const opt = document.createElement('button');
    opt.className = 'scene-item' + (item.id === state.projectId ? ' is-active' : '');
    opt.type = 'button';
    const ob = document.createElement('b');
    ob.textContent = item.title || item.id;
    const os = document.createElement('span');
    os.textContent = item.id + ' · ' + fmt(item.duration || 0);
    opt.appendChild(ob); opt.appendChild(os);
    opt.addEventListener('click', function () {
      closeSceneMenu();
      openProject(item.id);
    });
    menu.appendChild(opt);
  });
}

function paintSceneHeader() {
  const p = state.project;
  $('sceneName').textContent = p ? (p.title || p.id) : '未选择';
  $('sceneMeta').textContent = p ? (p.id + ' · rev ' + state.revision) : '—';
  const obsScene = $('obsScene');
  if (obsScene && !obsScene.value && state.projectId) obsScene.value = 'ObsFusion - ' + state.projectId;
}

function openSceneMenu() {
  $('sceneMenu').hidden = false;
  $('sceneBtn').setAttribute('aria-expanded', 'true');
}
function closeSceneMenu() {
  $('sceneMenu').hidden = true;
  $('sceneBtn').setAttribute('aria-expanded', 'false');
}

/* ── 扩展列表 ─────────────────────────────────────────── */
const EXT_ICON = { danmaku: 'D', chapters: 'C' };
function renderExtList() {
  const list = $('extList');
  if (!list) return;
  list.innerHTML = '';
  if (!state.extensions.length) {
    const empty = document.createElement('div');
    empty.className = 'log-empty';
    empty.textContent = 'extensions/ 目录下没有可加载的扩展。';
    list.appendChild(empty);
    return;
  }
  state.extensions.forEach(function (ext) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ext-row' + (ext.enabled === false ? ' is-off' : '') + (ext.id === state.activeExtensionId && state.tab === 'ext' ? ' is-active' : '');
    const ico = document.createElement('span');
    ico.className = 'ext-ico';
    ico.textContent = (EXT_ICON[ext.id] || String(ext.id || '?'))[0].toUpperCase();
    ico.title = ext.id;
    const body = document.createElement('span');
    const name = document.createElement('b');
    name.className = 'ext-name';
    name.textContent = ext.name || ext.id;
    const desc = document.createElement('span');
    desc.className = 'ext-desc';
    desc.textContent = ext.error ? ('错误：' + ext.error) : (ext.description || ext.id);
    body.appendChild(name); body.appendChild(desc);
    const ver = document.createElement('span');
    ver.className = 'ext-ver';
    ver.textContent = 'v' + (ext.version || '0.0.0');
    row.appendChild(ico); row.appendChild(body); row.appendChild(ver);
    row.addEventListener('click', function () {
      state.activeExtensionId = ext.id;
      switchTab('ext');
      renderExtList(); renderExtPicker(); renderExtConfig();
    });
    list.appendChild(row);
  });
}

/* ── 舞台 ─────────────────────────────────────────────── */
function paintStage(hard) {
  const frame = $('stageIframe');
  const wrap = $('stageFrame');
  if (!frame || !state.projectId) return;
  const src = '/overlay/' + encodeURIComponent(state.projectId) + '?preview=1' + (state.safeArea ? '&debug=1' : '');
  if (hard || frame.dataset.src !== src) {
    frame.dataset.src = src;
    wrap.classList.remove('is-ready');
    frame.src = src;
    setTimeout(function () { wrap.classList.add('is-ready'); }, 900);
  }
}

/* 预览 iframe 的唯一时钟来源：控制器每帧下发 {t, status}，视频与字幕因此不会各跑各的 */
function syncPreview(t) {
  const frame = $('stageIframe');
  if (!frame || !frame.contentWindow) return;
  try {
    frame.contentWindow.postMessage({ __obsfusion: 'sync', t: t, status: (state.transport && state.transport.status) || 'idle' }, location.origin);
  } catch (_) {}
}

/* ── 时间线渲染 ───────────────────────────────────────── */
function niceStep(dur) {
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  for (let i = 0; i < steps.length; i++) {
    if (dur / steps[i] <= 12) return steps[i];
  }
  return 600;
}

function renderTimeline() {
  const ruler = $('tlRuler');
  const layer = $('tlCues');
  if (!ruler || !layer) return;
  ruler.innerHTML = '';
  layer.innerHTML = '';
  const dur = state.duration || 8;
  const step = niceStep(dur);
  const minor = step / 2;
  for (let t = 0; t <= dur + 0.0001; t += minor) {
    const isMajor = Math.abs(t / step - Math.round(t / step)) < 0.001;
    const tick = document.createElement('i');
    tick.className = 'tl-tick' + (isMajor ? ' is-major' : '');
    tick.style.left = (clamp(t / dur, 0, 1) * 100).toFixed(4) + '%';
    if (isMajor) {
      const label = document.createElement('span');
      label.textContent = fmtClock(t);
      tick.appendChild(label);
    }
    ruler.appendChild(tick);
  }

  const cues = (state.timeline && state.timeline.cues) || [];
  if (!cues.length) {
    const empty = document.createElement('div');
    empty.className = 'cue-empty-track';
    empty.textContent = '时间线上还没有字幕，从右侧「新增字幕」开始';
    layer.appendChild(empty);
    return;
  }

  cues.forEach(function (cue) {
    const block = document.createElement('div');
    block.className = 'cue-block';
    block.dataset.cueId = cue.id;
    block.tabIndex = 0;
    block.setAttribute('role', 'button');
    block.setAttribute('aria-label', '字幕 ' + cue.text);
    const left = clamp(cue.start / dur, 0, 1) * 100;
    const span = Math.max(0.35, (Math.max(cue.end, cue.start + 0.2) - cue.start) / dur * 100);
    block.style.left = left.toFixed(4) + '%';
    block.style.width = 'calc(' + span.toFixed(4) + '% - 2px)';
    if (cue.id === state.selectedCueId) block.classList.add('is-active');

    const gripL = document.createElement('i');
    gripL.className = 'grip l';
    const gripR = document.createElement('i');
    gripR.className = 'grip r';
    const label = document.createElement('b');
    label.textContent = cue.text || '(空)';
    const meta = document.createElement('span');
    meta.textContent = fmt(cue.start) + ' → ' + fmt(cue.displayEnd || cue.end);

    block.appendChild(gripL);
    block.appendChild(gripR);
    block.appendChild(label);
    block.appendChild(meta);
    attachBlockDrag(block, cue);
    block.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        selectCue(cue.id, { seek: true });
      }
    });
    layer.appendChild(block);
  });
}

function cueModel(id) {
  if (!state.project || !state.project.cues) return null;
  for (let i = 0; i < state.project.cues.length; i++) {
    if (state.project.cues[i].id === id) return state.project.cues[i];
  }
  return null;
}

function attachBlockDrag(block, cue) {
  block.addEventListener('pointerdown', function (ev) {
    if (ev.button !== 0) return;
    const grip = ev.target.closest ? ev.target.closest('.grip') : null;
    const mode = grip ? (grip.classList.contains('l') ? 'start' : 'end') : 'move';
    startDrag(ev, cue, mode, block);
  });
  block.addEventListener('click', function () {
    if (state.dragMoved) { state.dragMoved = false; return; }
    selectCue(cue.id, { seek: true });
  });
}

function startDrag(ev, cue, mode, block) {
  const track = $('tlTrack');
  if (!track) return;
  const rect = track.getBoundingClientRect();
  const dur = state.duration || 8;
  const startX = ev.clientX;
  const grabRatio = clamp((startX - rect.left) / rect.width, 0, 1);
  const grabTime = round(grabRatio * dur / 0.05) * 0.05;
  const model = cueModel(cue.id) || cue;
  const base = { start: Number(model.start) || 0, end: Number(model.end) || 0 };
  const length = Math.max(0.2, base.end - base.start);
  let moved = false;
  state.dragMoved = false;
  block.classList.add('is-dragging');
  block.setPointerCapture && block.setPointerCapture(ev.pointerId);
  ev.preventDefault();

  function toTime(clientX) {
    const ratio = clamp((clientX - rect.left) / rect.width, 0, 1);
    return round(ratio * dur / 0.05) * 0.05;
  }

  function onMove(moveEv) {
    if (Math.abs(moveEv.clientX - startX) > 2) moved = true;
    const t = toTime(moveEv.clientX);
    let nextStart = base.start;
    let nextEnd = base.end;
    if (mode === 'move') {
      const delta = t - grabTime;
      nextStart = clamp(round((base.start + delta) / 0.05) * 0.05, 0, Math.max(0, dur - length));
      nextEnd = nextStart + length;
    } else if (mode === 'start') {
      nextStart = clamp(t, 0, base.end - 0.2);
    } else {
      nextEnd = clamp(t, base.start + 0.2, dur);
    }
    const leftPct = clamp(nextStart / dur, 0, 1) * 100;
    const widthPct = Math.max(0.35, (nextEnd - nextStart) / dur * 100);
    block.style.left = leftPct.toFixed(4) + '%';
    block.style.width = 'calc(' + widthPct.toFixed(4) + '% - 2px)';
    const meta = block.querySelector('span');
    if (meta) meta.textContent = fmt(nextStart) + ' → ' + fmt(nextEnd);
    state.drag = { start: nextStart, end: nextEnd };
  }

  function onUp() {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    block.classList.remove('is-dragging');
    if (!moved || !state.drag) { state.dragMoved = false; return; }
    const next = state.drag;
    state.drag = null;
    state.dragMoved = true;
    if (model) {
      model.start = round(next.start, 2);
      model.end = round(Math.max(next.end, next.start + 0.2), 2);
    }
    markDirty();
    renderTimeline();
    renderCueList();
    if (state.selectedCueId === cue.id) fillCueForm();
  }

  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}

/* ── 传输按钮 / 时间线点击 ─────────────────────────────── */
function bindTransport() {
  $('btnPlay').addEventListener('click', function () {
    if (state.transport.status === 'playing') sendTransport('pause');
    else sendTransport('play', state.transport.status === 'idle' ? 0 : transportTime());
  });
  $('btnStop').addEventListener('click', function () { sendTransport('stop'); });
  $('tlTrack').addEventListener('pointerdown', function (ev) {
    if (ev.target.closest('.cue-block')) return;
    const rect = ev.currentTarget.getBoundingClientRect();
    const ratio = clamp((ev.clientX - rect.left) / rect.width, 0, 1);
    const time = round(ratio * (state.duration || 8), 2);
    sendTransport('seek', time);
  });
}

/* ── 字幕选择与编辑 ───────────────────────────────────── */
function selectCue(id, opts) {
  opts = opts || {};
  state.selectedCueId = id;
  $$('.cue-block').forEach(function (node) {
    node.classList.toggle('is-active', node.dataset.cueId === id);
  });
  $$('.cue-item').forEach(function (node) {
    node.classList.toggle('is-active', node.dataset.cueId === id);
  });
  renderCueForm();
  if (opts.seek && id) {
    const cue = cueModel(id);
    if (cue) sendTransport('seek', Number(cue.start) || 0);
  }
  if (id) {
    const item = $$('.cue-item').find(function (n) { return n.dataset.cueId === id; });
    if (item) item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    // 右栏同样滚到表单顶部，避免"点了没反应"（表单在列表上方）
    const form = $('cueForm');
    if (form && !form.hidden) form.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function renderCueForm() {
  const form = $('cueForm');
  const empty = $('cueEmpty');
  if (!form || !empty) return;
  const id = state.selectedCueId;
  if (!id || !cueModel(id)) {
    form.hidden = true;
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  form.hidden = false;
  fillCueForm();
  fillCueAudio();
}

function fillCueForm() {
  const cue = cueModel(state.selectedCueId);
  if (!cue) return;
  const text = $('fText');
  if (document.activeElement !== text) text.value = cue.text || '';
  $('fStart').value = String(round(cue.start, 2));
  $('fEnd').value = String(round(cue.end, 2));
  $('fSpeaker').value = cue.speaker || '';
  $('fAnchor').value = cue.anchor || 'bottom-center';
  $('fStyle').value = cue.style || 'subtitle';
  const voice = (cue.tts && cue.tts.voice) || (state.voices[0] && state.voices[0].name) || VOICE_FALLBACK[0];
  ensureVoiceOption(voice);
  $('fVoice').value = voice;
  const kara = $('fKaraoke');
  kara.setAttribute('aria-checked', cue.karaoke === false ? 'false' : 'true');
}

function fillCueAudio() {
  const box = $('cueAudio');
  const audio = $('cueAudioEl');
  const dur = $('cueAudioDur');
  const tlCue = ((state.timeline && state.timeline.cues) || []).find(function (c) { return c.id === state.selectedCueId; });
  if (!box || !audio) return;
  if (tlCue && tlCue.audio && tlCue.audio.url) {
    box.hidden = false;
    audio.src = tlCue.audio.url + '?r=' + state.revision;
    dur.textContent = (tlCue.audio.duration ? tlCue.audio.duration.toFixed(2) + 's' : '—') + ' · ' + (tlCue.audio.voice || '');
  } else {
    box.hidden = true;
    audio.removeAttribute('src');
  }
}

function renderVoiceOptions() {
  const select = $('fVoice');
  if (!select) return;
  select.innerHTML = '';
  const names = [];
  state.voices.forEach(function (voice) {
    if (!voice || !voice.name || names.indexOf(voice.name) >= 0) return;
    names.push(voice.name);
    const option = document.createElement('option');
    option.value = voice.name;
    option.textContent = voice.name + (voice.culture ? ' · ' + voice.culture : '');
    select.appendChild(option);
  });
}

function ensureVoiceOption(name) {
  const select = $('fVoice');
  if (!select || !name) return;
  const exists = Array.prototype.some.call(select.options, function (o) { return o.value === name; });
  if (!exists) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
}

function bindCueForm() {
  const onEdit = function () {
    const cue = cueModel(state.selectedCueId);
    if (!cue) return;
    cue.text = $('fText').value;
    cue.start = Math.max(0, Number($('fStart').value) || 0);
    cue.end = Math.max(cue.start + 0.2, Number($('fEnd').value) || (cue.start + 3));
    cue.speaker = $('fSpeaker').value;
    cue.anchor = $('fAnchor').value;
    cue.style = $('fStyle').value;
    cue.tts = cue.tts || { enabled: true, rate: 0, volume: 100 };
    cue.tts.voice = $('fVoice').value;
    cue.tts.enabled = true;
    cue.karaoke = $('fKaraoke').getAttribute('aria-checked') === 'true';
    markDirty();
    renderTimeline();
    renderCueList();
  };
  ['fText', 'fStart', 'fEnd', 'fSpeaker', 'fAnchor', 'fStyle', 'fVoice'].forEach(function (id) {
    const node = $(id);
    node.addEventListener('input', onEdit);
    node.addEventListener('change', onEdit);
  });
  $('fKaraoke').addEventListener('click', function () {
    const next = this.getAttribute('aria-checked') !== 'true';
    this.setAttribute('aria-checked', next ? 'true' : 'false');
    onEdit();
  });
  $('btnCueTts').addEventListener('click', function () { generateTts([state.selectedCueId], true); });
  $('btnAddCue').addEventListener('click', addCue);
  $('btnDupCue').addEventListener('click', duplicateCue);
  $('btnDelCue').addEventListener('click', deleteCue);
  $('btnTts').addEventListener('click', function () { generateTts(null, false); });
}

function nextCueId() {
  const used = {};
  ((state.project && state.project.cues) || []).forEach(function (cue) { used[cue.id] = true; });
  let i = 1;
  while (used['c' + i]) i++;
  return 'c' + i;
}

function addCue() {
  if (!state.project) return;
  const cues = state.project.cues || (state.project.cues = []);
  let start = 0;
  if (cues.length) {
    const last = cues[cues.length - 1];
    start = round((Number(last.end) || 0) + 0.3, 2);
  }
  const cue = {
    id: nextCueId(),
    start: start,
    end: round(start + 3, 2),
    text: '新字幕',
    speaker: '解说',
    anchor: 'bottom-center',
    style: 'subtitle',
    karaoke: true,
    tts: { enabled: true, voice: ($('fVoice') && $('fVoice').value) || VOICE_FALLBACK[0], rate: 0, volume: 100 }
  };
  cues.push(cue);
  cues.sort(function (a, b) { return a.start - b.start; });
  markDirty();
  state.selectedCueId = cue.id;
  renderCueList();
  renderCueForm();
  renderTimeline();
  pushLog('info', '新增字幕 ' + cue.id);
  save(false);
}

function duplicateCue() {
  const src = cueModel(state.selectedCueId);
  if (!src) return;
  const cues = state.project.cues;
  const copy = JSON.parse(JSON.stringify(src));
  copy.id = nextCueId();
  const offset = Math.max(0.3, (Number(src.end) || 0) - (Number(src.start) || 0));
  copy.start = round((Number(src.end) || 0) + 0.2, 2);
  copy.end = round(copy.start + offset, 2);
  cues.push(copy);
  cues.sort(function (a, b) { return a.start - b.start; });
  state.selectedCueId = copy.id;
  markDirty();
  renderCueList(); renderCueForm(); renderTimeline();
  save(false);
}

function deleteCue() {
  const id = state.selectedCueId;
  if (!id || !state.project) return;
  const cues = state.project.cues || [];
  const index = cues.findIndex(function (cue) { return cue.id === id; });
  if (index < 0) return;
  cues.splice(index, 1);
  state.selectedCueId = cues.length ? cues[Math.max(0, index - 1)].id : null;
  markDirty();
  renderCueList(); renderCueForm(); renderTimeline();
  pushLog('warn', '删除字幕 ' + id);
  save(false);
}

function renderCueList() {
  const list = $('cueList');
  if (!list) return;
  list.innerHTML = '';
  const cues = (state.timeline && state.timeline.cues) || [];
  $('cueListHint').textContent = cues.length ? '共 ' + cues.length + ' 条' : '按时间排序';
  cues.forEach(function (cue) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'cue-item' + (cue.id === state.selectedCueId ? ' is-active' : '');
    item.dataset.cueId = cue.id;
    const time = document.createElement('time');
    time.textContent = fmt(cue.start);
    const body = document.createElement('div');
    const text = document.createElement('p');
    text.textContent = cue.text || '(空)';
    const meta = document.createElement('em');
    meta.textContent = (cue.speaker || '解说') + ' · ' + (ANCHOR_LABEL[cue.anchor] || cue.anchor || '') + (cue.audio ? ' · ' + cue.audio.duration.toFixed(1) + 's' : ' · 无语音');
    body.appendChild(text);
    body.appendChild(meta);
    item.appendChild(time);
    item.appendChild(body);
    item.addEventListener('click', function () { selectCue(cue.id, { seek: true }); });
    list.appendChild(item);
  });
}

/* ── 保存 ─────────────────────────────────────────────── */
function markDirty() {
  state.dirty = true;
  const btn = $('btnSave');
  if (btn) {
    btn.classList.remove('is-saved');
    btn.querySelector('.btn-label').textContent = '保存 ·';
  }
  if (state.saveTimer) clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(function () { save(false); }, 1100);
}

async function save(manual) {
  if (!state.project || !state.projectId || state.saving) return;
  if (!state.dirty && !manual) return;
  state.saving = true;
  const btn = $('btnSave');
  btn.classList.add('is-busy');
  try {
    const res = await api('/api/projects/' + encodeURIComponent(state.projectId), {
      method: 'PUT',
      body: { project: state.project }
    });
    state.revision = res.revision || state.revision;
    state.dirty = false;
    btn.classList.remove('is-busy');
    btn.classList.add('is-saved');
    btn.querySelector('.btn-label').textContent = '保存';
    $('saveHint').textContent = '已保存';
    setTimeout(function () { btn.classList.remove('is-saved'); }, 1500);
    await loadTimeline();
    paintSceneHeader();
    if (manual) toast('工程已保存 · rev ' + state.revision, 'ok');
    pushLog('ok', '保存 rev ' + state.revision + ' · ' + ((state.project.cues || []).length) + ' 条字幕');
  } catch (err) {
    btn.classList.remove('is-busy');
    toast('保存失败：' + err.message, 'bad');
    pushLog('error', '保存失败 ' + err.message);
  } finally {
    state.saving = false;
  }
}

/* ── TTS ──────────────────────────────────────────────── */
async function generateTts(cueIds, force) {
  if (!state.projectId) return;
  const button = cueIds ? $('btnCueTts') : $('btnTts');
  const label = button ? button.textContent : '';
  if (button) { button.classList.add('is-busy'); button.textContent = '生成中…'; }
  pushLog('info', (cueIds ? '生成 ' + cueIds.length + ' 条语音' : '生成全部语音') + '（SAPI 逐条合成，稍等）');
  try {
    const res = await api('/api/projects/' + encodeURIComponent(state.projectId) + '/tts', {
      method: 'POST',
      body: { cueIds: cueIds || undefined, force: !!force }
    });
    await loadTimeline();
    fillCueAudio();
    const ok = (res.generated || []).length;
    const skipped = (res.skipped || []).length;
    const failed = (res.errors || []).length;
    toast('语音生成完成：' + ok + ' 条新增，' + skipped + ' 条跳过' + (failed ? '，' + failed + ' 条失败' : ''), failed ? 'warn' : 'ok');
    pushLog(failed ? 'warn' : 'ok', 'TTS 完成 · 新增 ' + ok + ' / 跳过 ' + skipped + ' / 失败 ' + failed);
  } catch (err) {
    toast('语音生成失败：' + err.message, 'bad');
    pushLog('error', 'TTS 失败 ' + err.message);
  } finally {
    if (button) { button.classList.remove('is-busy'); button.textContent = label; }
  }
}

/* ── 弹幕测试 ─────────────────────────────────────────── */
const DM_BURST = ['来了来了', '这段字幕的节奏不错', '前面的镜头好稳', '主播再说一遍', '音画同步吗', '这个转场有点意思', '声音再大一点', 'SC：求置顶这段', '秒懂，谢谢讲解', '已三连', '弹幕测试 07', '收工'];

function bindDanmaku() {
  $('dmPreset').addEventListener('change', function () {
    if (this.value === 'host') { $('dmUser').value = '主播'; }
    else if (this.value === 'sc') { $('dmUser').value = 'SC'; $('dmText').value = 'SC：这段文案请置顶'; }
    else { $('dmUser').value = '观众A'; }
  });
  $('btnDmSend').addEventListener('click', function () { sendDanmaku(); });
  $('dmText').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') sendDanmaku();
  });
  $('btnDmBurst').addEventListener('click', async function () {
    if (!state.projectId) return;
    for (let i = 0; i < DM_BURST.length; i++) {
      const user = i % 5 === 3 ? '主播' : (i % 7 === 5 ? 'SC' : '观众' + (i + 1));
      await postDanmaku({ text: DM_BURST[i], user: user }).catch(function () {});
      await new Promise(function (r) { setTimeout(r, 240); });
    }
    toast('已发射 12 条弹幕', 'ok');
  });
}

async function sendDanmaku() {
  const text = $('dmText').value.trim() || '来了来了';
  const user = $('dmUser').value.trim() || '观众A';
  const payload = { text: text, user: user };
  await postDanmaku(payload);
}

async function postDanmaku(payload) {
  if (!state.projectId) return;
  try {
    const res = await api('/api/projects/' + encodeURIComponent(state.projectId) + '/danmaku', { method: 'POST', body: payload });
    paintDanmaku(res.message);
    pushLog('info', '弹幕 ' + (payload.user || '') + '：' + payload.text + ' → ' + res.message.zone);
    return res.message;
  } catch (err) {
    toast('弹幕发送失败：' + err.message, 'bad');
    pushLog('error', '弹幕失败 ' + err.message);
    return null;
  }
}

function paintDanmaku(msg) {
  if (!msg) return;
  const result = $('dmResult');
  result.dataset.state = 'ok';
  result.querySelector('.dm-result-empty').hidden = true;
  result.querySelector('.dm-result-body').hidden = false;
  $('dmRule').textContent = msg.matchedRule || '默认路由';
  $('dmZone').textContent = (msg.mode || 'scroll') + ' · 第 ' + ((msg.lane || 0) + 1) + ' 轨';
  const x = Number(msg.x);
  const y = Number(msg.y);
  $('dmCoords').textContent = 'x ' + (isFinite(x) ? x.toFixed(2) : '?') + ' · y ' + (isFinite(y) ? y.toFixed(2) : '?') + ' · ' + (msg.duration || 0) + 's';
  const dot = $('dmDot');
  dot.style.left = (clamp(isFinite(x) ? x : 0.5, 0, 1) * 100).toFixed(2) + '%';
  dot.style.top = (clamp(isFinite(y) ? y : 0.5, 0, 1) * 100).toFixed(2) + '%';
  dot.style.background = msg.color || '#e8b05a';
  $('dmZoneLabel').textContent = msg.zone || 'top';
}

/* ── 扩展配置 ─────────────────────────────────────────── */
function activeExtension() {
  return state.extensions.find(function (ext) { return ext.id === state.activeExtensionId; }) || null;
}

function renderExtPicker() {
  const wrap = $('extPicker');
  if (!wrap) return;
  wrap.innerHTML = '';
  state.extensions.forEach(function (ext) {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'ext-pill' + (ext.id === state.activeExtensionId ? ' is-active' : '');
    pill.textContent = ext.name || ext.id;
    pill.addEventListener('click', function () {
      state.activeExtensionId = ext.id;
      renderExtPicker(); renderExtConfig(); renderExtList();
    });
    wrap.appendChild(pill);
  });
}

function renderExtConfig() {
  const box = $('extConfig');
  if (!box) return;
  const ext = activeExtension();
  box.innerHTML = '';
  if (!ext) {
    const empty = document.createElement('div');
    empty.className = 'ext-config-empty';
    empty.textContent = '没有可配置的扩展';
    box.appendChild(empty);
    return;
  }
  if (ext.error) {
    const err = document.createElement('div');
    err.className = 'hint-block';
    const b = document.createElement('b');
    b.textContent = '这个扩展加载失败';
    const p = document.createElement('p');
    p.textContent = ext.error;
    err.appendChild(b); err.appendChild(p);
    box.appendChild(err);
  }

  const head = document.createElement('div');
  head.className = 'ext-config-head';
  const name = document.createElement('b');
  name.textContent = ext.name || ext.id;
  const ver = document.createElement('span');
  ver.textContent = 'v' + (ext.version || '0.0.0') + ' · ' + ext.id;
  head.appendChild(name); head.appendChild(ver);
  box.appendChild(head);

  const desc = document.createElement('p');
  desc.className = 'ext-config-desc';
  desc.textContent = ext.description || '';
  box.appendChild(desc);

  const working = JSON.parse(JSON.stringify(ext.config || {}));
  const fields = (ext.configSchema && ext.configSchema.fields) || [];

  fields.forEach(function (field) {
    const label = document.createElement('label');
    label.className = 'field field-wide';
    const title = document.createElement('span');
    title.textContent = field.label || field.key;
    label.appendChild(title);

    if (field.type === 'boolean') {
      const row = document.createElement('div');
      row.className = 'switch-row';
      const text = document.createElement('span');
      text.textContent = working[field.key] ? '已开启' : '已关闭';
      const sw = document.createElement('button');
      sw.type = 'button';
      sw.className = 'switch';
      sw.setAttribute('role', 'switch');
      sw.setAttribute('aria-checked', working[field.key] ? 'true' : 'false');
      const knob = document.createElement('i');
      sw.appendChild(knob);
      sw.addEventListener('click', function () {
        const next = sw.getAttribute('aria-checked') !== 'true';
        sw.setAttribute('aria-checked', next ? 'true' : 'false');
        text.textContent = next ? '已开启' : '已关闭';
        working[field.key] = next;
      });
      row.appendChild(text); row.appendChild(sw);
      label.appendChild(row);
    } else if (field.type === 'select') {
      const select = document.createElement('select');
      (field.options || []).forEach(function (option) {
        const node = document.createElement('option');
        node.value = String(option);
        node.textContent = String(option);
        select.appendChild(node);
      });
      select.value = String(working[field.key]);
      select.addEventListener('change', function () { working[field.key] = select.value; });
      label.appendChild(select);
    } else if (field.type === 'number') {
      const input = document.createElement('input');
      input.type = 'number';
      if (field.min !== undefined) input.min = String(field.min);
      if (field.max !== undefined) input.max = String(field.max);
      input.step = String(field.step || 1);
      input.value = String(working[field.key] === undefined ? '' : working[field.key]);
      input.addEventListener('input', function () {
        const value = Number(input.value);
        working[field.key] = isFinite(value) ? value : 0;
      });
      label.appendChild(input);
    } else {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = working[field.key] === undefined ? '' : String(working[field.key]);
      input.addEventListener('input', function () { working[field.key] = input.value; });
      label.appendChild(input);
    }
    box.appendChild(label);
  });

  const actions = document.createElement('div');
  actions.className = 'ext-actions';
  const applyBtn = document.createElement('button');
  applyBtn.className = 'btn primary tiny';
  applyBtn.textContent = '应用配置';
  applyBtn.addEventListener('click', function () { applyExtConfig(working); });
  const reloadBtn = document.createElement('button');
  reloadBtn.className = 'btn ghost tiny';
  reloadBtn.textContent = '重载扩展';
  reloadBtn.addEventListener('click', function () { reloadExtension(ext.id); });
  actions.appendChild(applyBtn); actions.appendChild(reloadBtn);
  box.appendChild(actions);

  const details = document.createElement('details');
  details.style.marginTop = '16px';
  const summary = document.createElement('summary');
  summary.textContent = '原始 JSON（路由规则等深层字段在这里改）';
  summary.style.cssText = 'font-size:11px;color:var(--of-dim);cursor:pointer;margin-bottom:8px;';
  const area = document.createElement('textarea');
  area.className = 'ext-json';
  area.spellcheck = false;
  area.value = JSON.stringify(ext.config || {}, null, 2);
  const jsonActions = document.createElement('div');
  jsonActions.className = 'ext-actions';
  const applyJson = document.createElement('button');
  applyJson.className = 'btn tiny';
  applyJson.textContent = '按 JSON 应用';
  applyJson.addEventListener('click', function () {
    let parsed;
    try { parsed = JSON.parse(area.value); }
    catch (err) { toast('JSON 解析失败：' + err.message, 'bad'); return; }
    applyExtConfig(parsed);
  });
  jsonActions.appendChild(applyJson);
  details.appendChild(summary);
  details.appendChild(area);
  details.appendChild(jsonActions);
  box.appendChild(details);
}

async function applyExtConfig(config) {
  const ext = activeExtension();
  if (!ext) return;
  try {
    await api('/api/extensions/' + encodeURIComponent(ext.id), { method: 'PUT', body: { config: config, enabled: ext.enabled !== false } });
    await loadExtensions();
    await loadTimeline();
    toast('扩展 ' + (ext.name || ext.id) + ' 配置已生效', 'ok');
    pushLog('ok', '扩展配置更新 ' + ext.id);
  } catch (err) {
    toast('配置失败：' + err.message, 'bad');
    pushLog('error', '扩展配置失败 ' + err.message);
  }
}

async function reloadExtension(id) {
  try {
    await api('/api/extensions/' + encodeURIComponent(id) + '/reload', { method: 'POST' });
    await loadExtensions();
    paintStage(true);
    toast('已重载扩展 ' + id, 'ok');
  } catch (err) {
    toast('重载失败：' + err.message, 'bad');
  }
}

/* ── OBS ──────────────────────────────────────────────── */
function paintObs(obs) {
  const dot = $('obsDot');
  const label = $('obsState');
  const meta = $('obsMeta');
  const body = $('obsBody');
  if (!dot || !label) return;
  const connected = !!(obs && obs.connected);
  const connecting = !!(obs && obs.connecting);
  dot.dataset.state = connected ? 'ok' : (connecting ? 'warn' : 'bad');
  label.textContent = connected ? '已连接' : (connecting ? '连接中…' : (obs && obs.error ? '连接失败' : '未连接'));
  if (meta) meta.textContent = (obs && obs.url) || 'ws://127.0.0.1:4455';
  if (body) {
    body.textContent = connected
      ? 'OBS ' + (obs.obsVersion || '?') + ' · websocket ' + (obs.websocketVersion || '?')
      : (obs && obs.error ? String(obs.error) : 'obs-websocket 5.x · OBS 28+ 内置。在 OBS「工具 → WebSocket 服务器设置」里启用。');
  }
  setPill('pillObs', connected ? 'ok' : (connecting ? 'busy' : 'warn'), connected ? '已连接' : (obs && obs.error ? '失败' : '未连接'));
}

function bindObs() {
  $('btnObsConnect').addEventListener('click', async function () {
    const btn = this;
    btn.classList.add('is-busy');
    try {
      const res = await api('/api/obs/connect', {
        method: 'POST',
        body: { url: $('obsUrl').value.trim(), password: $('obsPassword').value }
      });
      paintObs(res.obs);
      toast('已连接 OBS ' + (res.obs.obsVersion || ''), 'ok');
      pushLog('ok', 'OBS 已连接 ' + (res.obs.obsVersion || ''));
    } catch (err) {
      toast('连接 OBS 失败：' + err.message, 'bad');
      pushLog('error', 'OBS 连接失败 ' + err.message);
      await loadHealth();
    } finally {
      btn.classList.remove('is-busy');
    }
  });
  $('btnObsDisconnect').addEventListener('click', async function () {
    try {
      const res = await api('/api/obs/disconnect', { method: 'POST' });
      paintObs(res.obs);
      pushLog('warn', 'OBS 已断开');
    } catch (err) { toast(err.message, 'bad'); }
  });
  $('btnObsApply').addEventListener('click', async function () {
    const btn = this;
    btn.classList.add('is-busy');
    const log = $('obsApplyLog');
    log.hidden = false;
    log.textContent = '正在创建场景与浏览器源…';
    try {
      const res = await api('/api/obs/apply', {
        method: 'POST',
        body: {
          projectId: state.projectId,
          sceneName: $('obsScene').value.trim() || ('ObsFusion - ' + state.projectId),
          sourceName: $('obsSource').value.trim() || 'ObsFusion Overlay',
          width: 1920,
          height: 1080
        }
      });
      const applied = res.applied || {};
      log.textContent = '已推送：场景 ' + (applied.sceneName || '') + ' / 源 ' + (applied.sourceName || '') + '\nURL ' + (applied.url || '') + '\n可以切到 OBS 看画面了。';
      toast('已推送到 OBS 场景', 'ok');
      pushLog('ok', 'OBS 推送 ' + (applied.sceneName || ''));
    } catch (err) {
      log.textContent = '推送失败：' + err.message;
      toast('推送失败：' + err.message, 'bad');
      pushLog('error', 'OBS 推送失败 ' + err.message);
    } finally {
      btn.classList.remove('is-busy');
    }
  });
  $('btnObsDocs').addEventListener('click', function () {
    window.open('https://obsproject.com/kb/websocket-server', '_blank', 'noopener');
    toast('本地安装文档：docs/OBS_SETUP.md', 'info');
  });
}

/* ── 页签 ─────────────────────────────────────────────── */
const TAB_ORDER = ['cue', 'danmaku', 'ext', 'obs'];

function switchTab(name) {
  state.tab = name;
  $$('.tab').forEach(function (tab) { tab.classList.toggle('is-active', tab.dataset.tab === name); });
  $$('.tab-pane').forEach(function (pane) { pane.classList.toggle('is-active', pane.dataset.pane === name); });
  const index = Math.max(0, TAB_ORDER.indexOf(name));
  const indicator = $('tabIndicator');
  if (indicator) indicator.style.transform = 'translateX(' + (index * 100) + '%)';
  renderExtList();
}

function bindTabs() {
  $$('.tab').forEach(function (tab) {
    tab.addEventListener('click', function () { switchTab(tab.dataset.tab); });
  });
  switchTab('cue');
}

/* ── WebSocket ────────────────────────────────────────── */
function connectSocket() {
  if (!state.projectId) return;
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch (_) {} }
  if (state.wsTimer) { clearTimeout(state.wsTimer); state.wsTimer = null; }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + '/ws?scene=' + encodeURIComponent(state.projectId) + '&role=control');
  state.ws = ws;
  ws.addEventListener('open', function () {
    setPill('pillServer', 'ok', (state.health && state.health.version) || 'ok');
    pushLog('ok', '控制台已连上场景通道');
  });
  ws.addEventListener('message', function (ev) {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    handleSocket(msg);
  });
  ws.addEventListener('close', function () {
    if (state.ws !== ws) return;
    setPill('pillServer', 'warn', '重连中');
    state.wsTimer = setTimeout(connectSocket, 1800);
  });
  ws.addEventListener('error', function () {
    setPill('pillServer', 'warn', '通道异常');
  });
}

let reloadGuard = false;

function handleSocket(msg) {
  if (!msg || !msg.type) return;
  if (msg.type === 'hello') {
    syncTransport(msg.state);
    if (msg.timeline && msg.timeline.revision > state.revision + 0) {
      // 服务端有更新版本，拉一次完整时间线
      if (!reloadGuard) {
        reloadGuard = true;
        loadTimeline().then(function () { reloadGuard = false; }).catch(function () { reloadGuard = false; });
      }
    }
    return;
  }
  if (msg.type === 'state') { syncTransport(msg.state); return; }
  if (msg.type === 'event') {
    if (msg.name === 'danmaku.add' && msg.payload) {
      pushLog('info', '叠加层收到弹幕 ' + (msg.payload.zone || '') + ' · ' + (msg.payload.text || ''));
    } else if (msg.name === 'tts.progress' && msg.payload) {
      pushLog('info', 'TTS ' + (msg.payload.index + 1) + '/' + msg.payload.total + ' · ' + msg.payload.id);
    }
    return;
  }
  if (msg.type === 'reload') {
    if (msg.revision && msg.revision !== state.revision && !state.saving) {
      loadTimeline().then(function () { fillCueAudio(); paintStage(false); }).catch(function () {});
    }
    return;
  }
  if (msg.type === 'log') { pushLog(msg.level, msg.message); return; }
  if (msg.type === 'error') { pushLog('error', String(msg.error)); }
}

/* ── 快捷键 ───────────────────────────────────────────── */
function isTyping() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

function bindKeys() {
  document.addEventListener('keydown', function (ev) {
    const meta = ev.ctrlKey || ev.metaKey;
    if (meta && (ev.key === 's' || ev.key === 'S')) {
      ev.preventDefault();
      save(true);
      return;
    }
    if (isTyping()) return;
    if (ev.key === ' ') {
      ev.preventDefault();
      if (state.transport.status === 'playing') sendTransport('pause');
      else sendTransport('play', transportTime());
    } else if (ev.key === 'ArrowRight') {
      ev.preventDefault();
      sendTransport('seek', clamp(transportTime() + (ev.shiftKey ? 5 : 1), 0, state.duration));
    } else if (ev.key === 'ArrowLeft') {
      ev.preventDefault();
      sendTransport('seek', clamp(transportTime() - (ev.shiftKey ? 5 : 1), 0, state.duration));
    } else if (ev.key === 'Home') {
      sendTransport('seek', 0);
    } else if (ev.key === 'Escape') {
      closeSceneMenu();
    }
  });
}

/* ── 场景菜单 / 新建 ───────────────────────────────────── */
function bindSceneMenu() {
  $('sceneBtn').addEventListener('click', function (ev) {
    ev.stopPropagation();
    if ($('sceneMenu').hidden) openSceneMenu();
    else closeSceneMenu();
  });
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest('#sceneSwitch')) closeSceneMenu();
  });
  $('btnNewScene').addEventListener('click', async function () {
    const id = 'scene-' + Date.now().toString(36);
    const project = {
      id: id,
      title: '新场景',
      canvas: { width: 1920, height: 1080 },
      video: { src: '', loop: true, muted: true, fit: 'cover', opacity: 1 },
      audio: { ttsGain: 1, bgm: null },
      theme: { accent: '#E8B05A', subtitleSize: 64 },
      sync: { mode: 'audio', tailPadding: 0.25 },
      cues: [],
      extensions: [{ id: 'danmaku', enabled: true, config: {} }]
    };
    try {
      const res = await api('/api/projects', { method: 'POST', body: { project: project } });
      await loadProjects();
      await openProject(res.project.id);
      toast('已创建场景 ' + res.project.id, 'ok');
    } catch (err) {
      toast('创建场景失败：' + err.message, 'bad');
    }
  });
  $('btnReloadExt').addEventListener('click', async function () {
    if (state.activeExtensionId) await reloadExtension(state.activeExtensionId);
    else { await loadExtensions(); toast('扩展列表已刷新', 'ok'); }
  });
  $('btnOpenOverlay').addEventListener('click', function () {
    if (state.projectId) window.open('/overlay/' + encodeURIComponent(state.projectId), '_blank', 'noopener');
  });
  $('btnSave').addEventListener('click', function () { save(true); });
  $('btnReloadStage').addEventListener('click', function () { paintStage(true); });
  $('btnSafeArea').addEventListener('click', function () {
    state.safeArea = !state.safeArea;
    this.classList.toggle('is-on', state.safeArea);
    paintStage(true);
  });
}

/* ── 启动 ─────────────────────────────────────────────── */
async function init() {
  bindTabs();
  bindTransport();
  bindCueForm();
  bindDanmaku();
  bindObs();
  bindKeys();
  bindSceneMenu();

  $('logStream').innerHTML = '<div class="log-empty">等待事件…</div>';

  await loadHealth().catch(function () {});
  await loadVoices().catch(function () {});
  try {
    await loadProjects();
  } catch (err) {
    toast('无法读取场景列表：' + err.message, 'bad');
    return;
  }
  const wanted = new URLSearchParams(location.search).get('project');
  const first = wanted || (state.projects[0] && state.projects[0].id);
  if (first) await openProject(first);
  await loadExtensions().catch(function () {});
  switchTab('cue');
  setInterval(paintTransport, 90);
  setInterval(loadHealth, 5000);
  pushLog('ok', '控制台就绪 · ' + (state.projects.length) + ' 个场景');
}

init().catch(function (err) {
  console.error(err);
  toast('控制台初始化失败：' + err.message, 'bad');
});
