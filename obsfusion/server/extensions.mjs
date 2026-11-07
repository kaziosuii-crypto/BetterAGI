import fsp from 'node:fs/promises';
import path from 'node:path';
import { URL } from 'node:url';
import { deepMerge, ensureDir, readBody, readJson, sendJson, writeJsonAtomic } from './util.mjs';

export class ExtensionHost {
  constructor(opts) {
    this.root = opts.root;
    this.extensionsDir = opts.extensionsDir || path.join(opts.root, 'extensions');
    this.stateFile = path.join(opts.root, 'data', 'extensions.json');
    this.instances = new Map();
    this.persisted = {};
    this.capabilities = new Map();
    this.broadcastFn = opts.broadcast || function () {};
    this.log = opts.log || console.log;
    this.current = { sceneId: null, project: null };
  }

  async init() {
    await ensureDir(path.join(this.root, 'data'));
    this.persisted = await readJson(this.stateFile, {});
    var entries = [];
    try { entries = await fsp.readdir(this.extensionsDir, { withFileTypes: true }); } catch (_) {}
    for (var i = 0; i < entries.length; i++) {
      if (!entries[i].isDirectory()) continue;
      try { await this.loadOne(entries[i].name); }
      catch (err) { this.log('[ext] load failed ' + entries[i].name + ': ' + err.message); }
    }
  }

  async loadOne(id) {
    var dir = path.join(this.extensionsDir, id);
    var manifest = await readJson(path.join(dir, 'manifest.json'), null);
    if (!manifest || !manifest.id) throw new Error('missing manifest');
    var pref = this.persisted[manifest.id] || {};
    var config = deepMerge(manifest.defaultConfig || {}, pref.config || {});
    var instance = {
      id: manifest.id,
      dir: dir,
      manifest: manifest,
      config: config,
      enabled: pref.enabled !== false,
      routes: [],
      listeners: new Map(),
      api: null,
      error: null
    };
    var self = this;
    var serverEntry = manifest.server ? path.join(dir, manifest.server) : null;
    if (serverEntry) {
      try {
        var url = new URL('file://' + serverEntry.replace(/\\/g, '/'));
        url.searchParams.set('v', String(Date.now()));
        var mod = await import(url.href);
        instance.module = mod.default || mod;
      } catch (err) {
        instance.error = err.message;
        this.log('[ext] import failed ' + manifest.id + ': ' + err.message);
      }
    }
    var api = this.makeApi(instance);
    instance.api = api;
    if (instance.module && typeof instance.module.setup === 'function') {
      try { await instance.module.setup(api); }
      catch (err) {
        instance.error = err.message;
        this.log('[ext] setup failed ' + manifest.id + ': ' + err.message);
      }
    }
    this.instances.set(manifest.id, instance);
    return instance;
  }

  makeApi(instance) {
    var self = this;
    var api = {
      get id() { return instance.id; },
      get config() { return instance.config; },
      get enabled() { return instance.enabled; },
      get project() { return self.current.project; },
      get sceneId() { return self.current.sceneId; },
      get dataDir() { return path.join(self.root, 'data', 'extensions', instance.id); },
      log: function (level, message) {
        var line = '[ext:' + instance.id + '] ' + message;
        if (level === 'error') console.error(line); else if (level === 'warn') console.warn(line); else console.log(line);
      },
      route: function (method, routePath, handler) {
        instance.routes.push({ method: String(method).toUpperCase(), path: routePath, handler: handler });
      },
      on: function (event, handler) {
        if (!instance.listeners.has(event)) instance.listeners.set(event, []);
        instance.listeners.get(event).push(handler);
      },
      broadcast: function (type, payload, sceneId) {
        self.broadcastFn(sceneId || self.current.sceneId, { type: 'event', name: type, payload: payload, now: Date.now() });
      },
      capability: function (name, fn) {
        if (!self.capabilities.has(name)) self.capabilities.set(name, []);
        self.capabilities.get(name).push({ id: instance.id, fn: fn });
      },
      getState: function () { return instance.state || (instance.state = {}); },
      setState: function (patch) { instance.state = Object.assign(instance.state || {}, patch); }
    };
    return api;
  }

  list() {
    var out = [];
    var self = this;
    this.instances.forEach(function (instance) {
      out.push({
        id: instance.id,
        name: instance.manifest.name || instance.id,
        version: instance.manifest.version || '0.0.0',
        description: instance.manifest.description || '',
        enabled: instance.enabled,
        config: instance.config,
        configSchema: instance.manifest.configSchema || null,
        client: instance.manifest.client ? ('/extensions/' + instance.id + '/' + instance.manifest.client) : null,
        error: instance.error
      });
    });
    return out;
  }

  listEnabled() {
    return this.list().filter(function (ext) { return ext.enabled; });
  }

  get(id) {
    return this.instances.get(id) || null;
  }

  async update(id, patch) {
    var instance = this.instances.get(id);
    if (!instance) {
      var err = new Error('extension not found: ' + id);
      err.code = 'ENOENT';
      throw err;
    }
    if (patch && typeof patch.enabled === 'boolean') instance.enabled = patch.enabled;
    if (patch && patch.config && typeof patch.config === 'object') {
      instance.config = deepMerge(instance.config, patch.config);
    }
    this.persisted[id] = { enabled: instance.enabled, config: instance.config };
    await writeJsonAtomic(this.stateFile, this.persisted);
    if (instance.module && typeof instance.module.onConfigChange === 'function') {
      try { await instance.module.onConfigChange(instance.config, instance.api); }
      catch (err) { instance.error = err.message; }
    }
    return this.list().find(function (ext) { return ext.id === id; });
  }

  enter(sceneId, project) {
    this.current = { sceneId: sceneId, project: project };
  }

  async emit(event, payload) {
    var errors = [];
    var instances = Array.from(this.instances.values());
    for (var i = 0; i < instances.length; i++) {
      var instance = instances[i];
      if (!instance.enabled) continue;
      var handlers = instance.listeners.get(event) || [];
      for (var j = 0; j < handlers.length; j++) {
        try { await handlers[j](payload, instance.api); }
        catch (err) { errors.push(instance.id + ': ' + err.message); }
      }
    }
    return errors;
  }

  async runCapability(name, ctx, seed) {
    var chain = this.capabilities.get(name) || [];
    var value = seed;
    for (var i = 0; i < chain.length; i++) {
      var instance = this.instances.get(chain[i].id);
      if (!instance || !instance.enabled) continue;
      try { value = await chain[i].fn(ctx, value); }
      catch (err) { this.log('[ext] capability ' + name + ' failed in ' + chain[i].id + ': ' + err.message); }
    }
    return value;
  }

  async handleRoute(req, res, url, context) {
    var parts = url.pathname.split('/').filter(Boolean);
    if (parts.length < 3 || parts[0] !== 'x') return false;
    var id = decodeURIComponent(parts[1]);
    var instance = this.instances.get(id);
    if (!instance) { sendJson(res, 404, { error: 'extension not found: ' + id }); return true; }
    var sub = '/' + parts.slice(2).join('/');
    var body = null;
    if (req.method === 'POST' || req.method === 'PUT' || req.method === 'PATCH') {
      try { body = await readBody(req); } catch (err) { sendJson(res, 400, { error: err.message }); return true; }
    }
    for (var i = 0; i < instance.routes.length; i++) {
      var route = instance.routes[i];
      if (route.method !== req.method) continue;
      if (route.path !== sub) continue;
      try {
        var result = await route.handler({
          sceneId: context.sceneId, project: context.project, config: instance.config,
          query: Object.fromEntries(url.searchParams.entries()), body: body, req: req, res: res
        }, req, res, body);
        if (result !== undefined && !res.headersSent) sendJson(res, 200, result);
      } catch (err) {
        if (!res.headersSent) sendJson(res, 500, { error: err.message });
      }
      return true;
    }
    sendJson(res, 404, { error: 'route not found', path: sub });
    return true;
  }

  async reload(id) {
    var instance = this.instances.get(id);
    if (!instance) return null;
    this.instances.delete(id);
    this.capabilities.forEach(function (list) {
      for (var i = list.length - 1; i >= 0; i--) if (list[i].id === id) list.splice(i, 1);
    });
    return this.loadOne(id);
  }
}