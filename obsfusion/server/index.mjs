// ObsFusion 服务入口：HTTP + WebSocket + 静态资源 + 扩展挂载
import http from 'node:http';
import fsStream from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { VERSION, deepMerge, ensureDir, mimeFor, readBody, readJson, sendJson, sendText } from './util.mjs';
import { attachWebSocket } from './ws.mjs';
import { ProjectStore } from './store.mjs';
import { ExtensionHost } from './extensions.mjs';
import { generateProjectTTS, listVoices } from './tts.mjs';
import { ObsClient } from './obs.mjs';
import { compileTimeline } from './timeline.mjs';

export const ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
export const DEFAULT_PORT = 8787;

function safeId(value) {
  var id = String(value || '').replace(/[^a-zA-Z0-9_-]/g, '');
  if (!id) throw new Error('invalid project id');
  return id;
}

export async function createApp(opts) {
  opts = opts || {};
  var store = new ProjectStore(ROOT);
  await store.init();
  var clients = new Set();
  var states = new Map();
  var events = [];
  var startedAt = Date.now();
  var obs = new ObsClient({ url: opts.obsUrl || 'ws://127.0.0.1:4455', password: opts.obsPassword || '' });

  function sceneState(sceneId) {
    var st = states.get(sceneId);
    if (!st) { st = { status: 'idle', time: 0, anchor: Date.now() }; states.set(sceneId, st); }
    var time = st.time;
    if (st.status === 'playing') time = st.time + (Date.now() - st.anchor) / 1000;
    return { status: st.status, time: Math.round(time * 1000) / 1000, updatedAt: Date.now(), revision: store.revision(sceneId) };
  }

  function setSceneState(sceneId, patch) {
    var st = states.get(sceneId) || { status: 'idle', time: 0, anchor: Date.now() };
    if (patch.status) st.status = patch.status;
    if (typeof patch.time === 'number' && Number.isFinite(patch.time)) st.time = Math.max(0, patch.time);
    st.anchor = Date.now();
    states.set(sceneId, st);
    return sceneState(sceneId);
  }

  function broadcast(sceneId, message) {
    var payload = JSON.stringify(message);
    clients.forEach(function (conn) {
      if (!conn.alive) return;
      if (sceneId && conn.meta.sceneId && conn.meta.sceneId !== sceneId) return;
      conn.sendText(payload);
    });
  }

  function pushLog(level, message) {
    var entry = { type: 'log', level: level, message: String(message), at: Date.now() };
    events.push(entry);
    if (events.length > 80) events.shift();
    broadcast(null, entry);
  }

  var extensions = new ExtensionHost({
    root: ROOT,
    broadcast: function (sceneId, message) { broadcast(sceneId, message); },
    log: function (line) { pushLog('info', line); }
  });
  await extensions.init();

  var loadedProjects = Object.create(null);

  async function loadTimeline(id) {
    var project = await store.load(id);
    var manifest = await store.ttsManifest(id);
    var hostList = extensions.listEnabled();
    var hostMap = new Map(hostList.map(function (ext) { return [ext.id, ext]; }));
    var extList = [];
    (project.extensions || []).forEach(function (entry) {
      var host = hostMap.get(entry.id);
      if (!host || entry.enabled === false) return;
      extList.push({
        id: entry.id,
        enabled: true,
        config: Object.assign({}, host.config, entry.config || {}),
        client: host.client,
        version: host.version
      });
    });
    var timeline = compileTimeline(project, {
      ttsManifest: manifest,
      revision: store.revision(id),
      extensions: extList
    });
    extensions.enter(id, project);
    // 首次加载某个工程时通知扩展；同一工程重复拉时间线（播放中刷新）不重复广播
    if (!loadedProjects[id]) {
      loadedProjects[id] = true;
      extensions.emit('project.load', { sceneId: id, project: project, timeline: timeline }).catch(function (err) {
        pushLog('warn', '[project.load] ' + (err && err.message));
      });
    }
    return { project: project, timeline: timeline, manifest: manifest };
  }

  return { store: store, extensions: extensions, obs: obs, clients: clients, states: states, broadcast: broadcast, sceneState: sceneState, setSceneState: setSceneState, pushLog: pushLog, loadTimeline: loadTimeline, startedAt: startedAt, events: events };
}

export function makeHandler(ctx) {
  var store = ctx.store;
  var clients = ctx.clients;
  var extensions = ctx.extensions;
  var obs = ctx.obs;

  function isMissing(err) {
    return err && (err.code === 'ENOENT' || /not found/i.test(String(err.message)));
  }

  async function serveFile(res, filePath, req) {
    var stat;
    try { stat = await fsp.stat(filePath); } catch (_) { return sendJson(res, 404, { error: 'not found' }); }
    if (!stat.isFile()) return sendJson(res, 404, { error: 'not found' });
    var mime = mimeFor(filePath);
    var range = req.headers.range;
    if (range && /^bytes=/.test(range)) {
      var parts = range.replace(/^bytes=/, '').split('-');
      var start = parseInt(parts[0], 10) || 0;
      var end = parts[1] ? parseInt(parts[1], 10) : stat.size - 1;
      end = Math.min(end, stat.size - 1);
      if (start > end) {
        res.writeHead(416, { 'Content-Range': 'bytes */' + stat.size, 'Accept-Ranges': 'bytes' });
        return res.end();
      }
      res.writeHead(206, {
        'Content-Type': mime,
        'Content-Length': end - start + 1,
        'Content-Range': 'bytes ' + start + '-' + end + '/' + stat.size,
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'public, max-age=3600'
      });
      return fsStream.createReadStream(filePath, { start: start, end: end }).pipe(res);
    }
    res.writeHead(200, {
      'Content-Type': mime,
      'Content-Length': stat.size,
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'no-cache'
    });
    if (req.method === 'HEAD') return res.end();
    fsStream.createReadStream(filePath).pipe(res);
  }


  async function handle(req, res) {
    var url = new URL(req.url, 'http://127.0.0.1');
    var pathname = decodeURIComponent(url.pathname);
    res.setHeader('X-ObsFusion', VERSION);
    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
      });
      return res.end();
    }
    if (pathname.indexOf('/api/') === 0) {
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Cache-Control', 'no-store');
      return handleApi(req, res, url, pathname);
    }
    if (pathname.indexOf('/x/') === 0) {
      return extensions.handleRoute(req, res, url, { sceneId: url.searchParams.get('scene') || null, project: null });
    }
    return handleStatic(req, res, pathname);
  }

  async function handleApi(req, res, url, pathname) {
    var match;
    if (req.method === 'GET' && pathname === '/api/health') {
      return sendJson(res, 200, {
        ok: true, name: 'obsfusion', version: VERSION,
        time: Date.now(), uptimeMs: Date.now() - ctx.startedAt,
        obs: obs.status(),
        projects: (await store.list()).length,
        viewers: Array.from(clients).filter(function (conn) { return conn.alive && conn.meta.role === 'overlay'; }).length,
        clients: clients.size,
        extensions: extensions.list().map(function (ext) { return { id: ext.id, enabled: ext.enabled, error: ext.error }; })
      });
    }

    if (pathname === '/api/voices' && req.method === 'GET') {
      var voices = await listVoices();
      return sendJson(res, 200, { voices: voices, platform: process.platform });
    }

    if (pathname === '/api/projects' && req.method === 'GET') {
      return sendJson(res, 200, { projectRoot: store.projectsDir, projects: await store.list() });
    }
    if (pathname === '/api/projects' && req.method === 'POST') {
      var createBody = await readBody(req);
      var incoming = createBody.project || createBody;
      var id = String(incoming.id || createBody.id || incoming.title || ('project-' + Date.now().toString(36))).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
      if (!/^[a-z0-9_-]+$/.test(id)) id = 'project-' + Date.now().toString(36);
      var saved = await store.save(id, Object.assign({}, incoming, { id: id }));
      await ctx.loadTimeline(id);
      ctx.broadcast(id, { type: 'reload', sceneId: id, revision: store.revision(id) });
      return sendJson(res, 201, { ok: true, project: saved });
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)$/))) {
      var pid = safeId(match[1]);
      if (req.method === 'GET') {
        try {
          return sendJson(res, 200, { project: await store.load(pid), revision: store.revision(pid) });
        } catch (err) {
          return sendJson(res, isMissing(err) ? 404 : 500, { error: String(err.message) });
        }
      }
      if (req.method === 'PUT') {
        var body = await readBody(req);
        var project;
        try { project = await store.save(pid, body.project || body); }
        catch (err) { return sendJson(res, 400, { error: String(err.message) }); }
        var loaded = await ctx.loadTimeline(pid);
        ctx.broadcast(pid, { type: 'reload', sceneId: pid, revision: store.revision(pid) });
        ctx.broadcast(pid, { type: 'event', name: 'project.save', payload: { revision: store.revision(pid) }, now: Date.now() });
        await extensions.emit('project.save', { sceneId: pid, project: project, timeline: loaded.timeline });
        return sendJson(res, 200, { ok: true, project: project, revision: store.revision(pid) });
      }
      if (req.method === 'DELETE') {
        await fsp.rm(store.projectDir(pid), { recursive: true, force: true });
        ctx.states.delete(pid);
        return sendJson(res, 200, { ok: true, deleted: pid });
      }
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)\/timeline$/)) && req.method === 'GET') {
      try {
        var tl = await ctx.loadTimeline(safeId(match[1]));
        return sendJson(res, 200, Object.assign({ revision: store.revision(match[1]), extensionsState: extensions.list() }, tl.timeline));
      } catch (err) {
        return sendJson(res, isMissing(err) ? 404 : 500, { error: String(err.message) });
      }
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)\/transport$/)) && req.method === 'POST') {
      var tid = safeId(match[1]);
      var tbody = await readBody(req);
      var current = ctx.sceneState(tid);
      var action = String(tbody.action || '');
      var next;
      if (action === 'play') next = ctx.setSceneState(tid, { status: 'playing', time: typeof tbody.time === 'number' ? tbody.time : current.time });
      else if (action === 'pause') next = ctx.setSceneState(tid, { status: 'paused', time: current.time });
      else if (action === 'seek') next = ctx.setSceneState(tid, { status: current.status === 'playing' ? 'playing' : 'paused', time: Number(tbody.time) || 0 });
      else if (action === 'stop') next = ctx.setSceneState(tid, { status: 'idle', time: 0 });
      else return sendJson(res, 400, { error: 'unknown action: ' + action });
      ctx.broadcast(tid, { type: 'state', sceneId: tid, state: next, now: Date.now() });
      await extensions.emit('state', { sceneId: tid, state: next });
      return sendJson(res, 200, { ok: true, state: next });
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)\/tts$/)) && req.method === 'POST') {
      var ktid = safeId(match[1]);
      var kbody = await readBody(req);
      var result = await generateProjectTTS(store, ktid, {
        force: !!kbody.force,
        cueIds: Array.isArray(kbody.cueIds) ? kbody.cueIds : undefined,
        onProgress: function (progress) {
          ctx.broadcast(ktid, { type: 'event', name: 'tts.progress', payload: progress, now: Date.now() });
          if (progress.phase === 'cue') ctx.pushLog('info', 'TTS ' + progress.index + '/' + progress.total + ' ' + progress.cueId);
        }
      });
      await ctx.loadTimeline(ktid);
      ctx.broadcast(ktid, { type: 'reload', sceneId: ktid, revision: store.revision(ktid) });
      return sendJson(res, 200, Object.assign({ ok: true }, result));
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)\/danmaku$/)) && req.method === 'POST') {
      var did = safeId(match[1]);
      var dbody = await readBody(req);
      var dproject = await store.load(did);
      var dentry = (dproject.extensions || []).find(function (entry) { return entry.id === 'danmaku'; });
      var dhost = extensions.get('danmaku');
      var dconfig = deepMerge(dhost ? dhost.config : {}, dentry ? (dentry.config || {}) : {});
      var dmapped = await extensions.runCapability('danmaku.route', { sceneId: did, project: dproject, config: dconfig }, dbody);
      if (!dmapped || !dmapped.zone) {
        dmapped = { id: 'dm-' + Date.now().toString(36), text: String(dbody.text || ''), user: String(dbody.user || ''), zone: 'top', mode: 'scroll', lane: 0, lanes: 6, x: 0.5, y: 0.2, duration: 8, color: '#F4F1EA', size: 34, opacity: 0.92 };
      }
      ctx.broadcast(did, { type: 'event', name: 'danmaku.add', payload: dmapped, now: Date.now() });
      await extensions.emit('danmaku.raw', { sceneId: did, raw: dbody, mapped: dmapped });
      return sendJson(res, 200, { ok: true, message: dmapped });
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)\/media\/(.+)$/)) && (req.method === 'GET' || req.method === 'HEAD')) {
      var mid = safeId(match[1]);
      var mediaFile = path.normalize(path.join(store.projectDir(mid), 'media', match[2]));
      if (mediaFile.indexOf(store.projectDir(mid)) !== 0) return sendJson(res, 403, { error: 'forbidden' });
      return serveFile(res, mediaFile, req);
    }

    if ((match = pathname.match(/^\/api\/projects\/([^/]+)\/assets\/(.+)$/)) && (req.method === 'GET' || req.method === 'HEAD')) {
      var aid = safeId(match[1]);
      var assetFile = path.normalize(path.join(store.projectDir(aid), 'assets', match[2]));
      if (assetFile.indexOf(store.projectDir(aid)) !== 0) return sendJson(res, 403, { error: 'forbidden' });
      return serveFile(res, assetFile, req);
    }

    if (pathname === '/api/extensions' && req.method === 'GET') {
      return sendJson(res, 200, { extensions: extensions.list(), dir: extensions.extensionsDir });
    }
    if ((match = pathname.match(/^\/api\/extensions\/([^/]+)$/)) && req.method === 'PUT') {
      var ebody = await readBody(req);
      var updated = await extensions.update(decodeURIComponent(match[1]), ebody);
      return sendJson(res, 200, { ok: true, extension: updated });
    }
    if ((match = pathname.match(/^\/api\/extensions\/([^/]+)\/reload$/)) && req.method === 'POST') {
      var reloaded = await extensions.reload(decodeURIComponent(match[1]));
      if (!reloaded) return sendJson(res, 404, { error: 'extension not found' });
      ctx.broadcast(null, { type: 'reload', sceneId: null, revision: 0 });
      return sendJson(res, 200, { ok: true, extension: extensions.list().find(function (ext) { return ext.id === reloaded.id; }) });
    }

    if (pathname === '/api/obs/status' && req.method === 'GET') {
      return sendJson(res, 200, { obs: obs.status(), settings: { url: obs.url, password: !!obs.password } });
    }
    if (pathname === '/api/obs/connect' && req.method === 'POST') {
      var obody = await readBody(req);
      if (obody.url) obs.url = String(obody.url);
      if (typeof obody.password === 'string') obs.password = obody.password;
      try {
        await obs.connect(6000);
        await obs.getVersion();
        return sendJson(res, 200, { ok: true, obs: obs.status() });
      } catch (err) {
        return sendJson(res, 502, { ok: false, error: err.message, obs: obs.status() });
      }
    }
    if (pathname === '/api/obs/disconnect' && req.method === 'POST') {
      obs.close();
      return sendJson(res, 200, { ok: true, obs: obs.status() });
    }
    if (pathname === '/api/obs/apply' && req.method === 'POST') {
      var abody = await readBody(req);
      var apid = safeId(abody.projectId || abody.sceneId || 'demo');
      await store.load(apid);
      var overlayUrl = 'http://127.0.0.1:' + ctx.port + '/overlay/' + apid;
      try {
        var applied = await obs.applyOverlay({
          sceneName: abody.sceneName || ('ObsFusion - ' + apid),
          sourceName: abody.sourceName || 'ObsFusion Overlay',
          url: overlayUrl,
          width: abody.width || 1920,
          height: abody.height || 1080
        });
        return sendJson(res, 200, { ok: true, applied: applied });
      } catch (err) {
        return sendJson(res, 502, { ok: false, error: err.message, obs: obs.status() });
      }
    }

    return sendJson(res, 404, { error: 'not found', path: pathname });
  }

  async function handleStatic(req, res, pathname) {
    if (req.method !== 'GET' && req.method !== 'HEAD') return sendJson(res, 405, { error: 'method not allowed' });
    function inside(base, rel) {
      var full = path.normalize(path.join(base, rel));
      return full.indexOf(path.normalize(base)) === 0 ? full : null;
    }
    if (pathname === '/' || pathname === '/control' || pathname === '/control/') {
      return serveFile(res, path.join(ROOT, 'web', 'control', 'index.html'), req);
    }
    if (pathname.indexOf('/control/') === 0) {
      var cfile = inside(path.join(ROOT, 'web', 'control'), pathname.slice('/control/'.length));
      return cfile ? serveFile(res, cfile, req) : sendJson(res, 404, { error: 'not found' });
    }
    if (pathname.indexOf('/overlay/') === 0) {
      // /overlay/<file> 优先当静态资源；<file> 无扩展名时才当项目页面 /overlay/<projectId>
      var orel = pathname.slice('/overlay/'.length);
      var ofile = orel ? inside(path.join(ROOT, 'web', 'overlay'), orel) : null;
      if (ofile) {
        try {
          var ost = await fsp.stat(ofile);
          if (ost.isFile()) return serveFile(res, ofile, req);
        } catch (_) {}
      }
      if (/^[^/.]+\/?$/.test(orel)) {
        try { await store.load(safeId(orel.replace(/\/$/, ''))); }
        catch (_) { return sendText(res, 404, 'project not found: ' + orel); }
        return serveFile(res, path.join(ROOT, 'web', 'overlay', 'index.html'), req);
      }
      return sendJson(res, 404, { error: 'not found' });
    }
    if (pathname.indexOf('/shared/') === 0) {
      var sfile = inside(path.join(ROOT, 'web', 'shared'), pathname.slice('/shared/'.length));
      return sfile ? serveFile(res, sfile, req) : sendJson(res, 404, { error: 'not found' });
    }
    var ext = pathname.match(/^\/extensions\/([^/]+)\/(.+)$/);
    if (ext) {
      var efile = inside(path.join(extensions.extensionsDir, decodeURIComponent(ext[1])), decodeURIComponent(ext[2]));
      return efile ? serveFile(res, efile, req) : sendJson(res, 404, { error: 'not found' });
    }
    if (pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
    return sendText(res, 404, '<!doctype html><meta charset="utf-8"><title>ObsFusion</title><body style="background:#0b0d12;color:#f4f1ea;font-family:system-ui;padding:40px"><h1>404</h1><p>没有这个页面。去 <a style="color:#e8b05a" href="/control">控制台</a>。</p>');
  }

  return handle;
}

export async function startServer(opts) {
  opts = opts || {};
  var ctx = await createApp(opts);
  ctx.port = opts.port || DEFAULT_PORT;
  var handler = makeHandler(ctx);
  var server = http.createServer(function (req, res) {
    Promise.resolve(handler(req, res)).catch(function (err) {
      console.error('[http]', err);
      if (!res.headersSent) sendJson(res, 500, { error: err.message });
      else { try { res.end(); } catch (_) {} }
    });
  });
  attachWebSocket(server, function (conn) { setupConnection(ctx, conn); });
  await new Promise(function (resolve, reject) {
    server.once('error', reject);
    server.listen(ctx.port, opts.host || '127.0.0.1', function () { resolve(); });
  });
  ctx.server = server;
  ctx.url = 'http://127.0.0.1:' + ctx.port;
  ctx.close = function () {
    ctx.clients.forEach(function (conn) { try { conn.close(); } catch (_) {} });
    obsSafeClose(ctx.obs);
    return new Promise(function (resolve) { server.close(function () { resolve(); }); });
  };
  return ctx;
}

function obsSafeClose(obs) {
  try { obs.close(); } catch (_) {}
}

function setupConnection(ctx, conn) {
  conn.meta = {
    role: conn.url.searchParams.get('role') || 'overlay',
    sceneId: conn.url.searchParams.get('scene') || null,
    joinedAt: Date.now()
  };
  ctx.clients.add(conn);

  async function hello() {
    var sceneId = conn.meta.sceneId;
    if (!sceneId) {
      return conn.sendJson({ type: 'hello', role: conn.meta.role, sceneId: null, timeline: null, state: null, now: Date.now() });
    }
    try {
      var loaded = await ctx.loadTimeline(sceneId);
      return conn.sendJson({
        type: 'hello', role: conn.meta.role, sceneId: sceneId,
        timeline: loaded.timeline, state: ctx.sceneState(sceneId), now: Date.now()
      });
    } catch (err) {
      return conn.sendJson({ type: 'error', error: err.message, sceneId: sceneId });
    }
  }

  hello();

  conn.onMessage = async function (text) {
    var msg;
    try { msg = JSON.parse(text); } catch (_) { return; }
    if (!msg || !msg.type) return;
    if (msg.type === 'ping') return conn.sendJson({ type: 'pong', now: Date.now() });
    if (msg.type === 'subscribe') {
      conn.meta.sceneId = String(msg.scene || '');
      if (msg.role) conn.meta.role = String(msg.role);
      return hello();
    }
    if (msg.type === 'event') {
      await ctx.extensions.emit('overlay.event', { sceneId: conn.meta.sceneId, name: msg.name, payload: msg.payload });
      return;
    }
    if (msg.type === 'transport') {
      var sceneId = conn.meta.sceneId;
      if (!sceneId) return;
      var action = String(msg.action || '');
      var current = ctx.sceneState(sceneId);
      var next;
      if (action === 'play') next = ctx.setSceneState(sceneId, { status: 'playing', time: typeof msg.time === 'number' ? msg.time : current.time });
      else if (action === 'pause') next = ctx.setSceneState(sceneId, { status: 'paused', time: current.time });
      else if (action === 'seek') next = ctx.setSceneState(sceneId, { status: current.status === 'playing' ? 'playing' : 'paused', time: Number(msg.time) || 0 });
      else if (action === 'stop') next = ctx.setSceneState(sceneId, { status: 'idle', time: 0 });
      if (next) ctx.broadcast(sceneId, { type: 'state', sceneId: sceneId, state: next, now: Date.now() });
      return;
    }
  };

  conn.onClose = function () { ctx.clients.delete(conn); };
}