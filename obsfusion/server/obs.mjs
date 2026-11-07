// obs-websocket v5 客户端（零依赖，使用 Node 内置 WebSocket / crypto）
import crypto from 'node:crypto';

export const DEFAULT_OBS_URL = 'ws://127.0.0.1:4455';
export const INPUT_KIND_BROWSER = 'browser_source';

function sha256b64(text) {
  return crypto.createHash('sha256').update(text, 'utf8').digest('base64');
}

export class ObsClient {
  constructor(opts) {
    opts = opts || {};
    this.url = opts.url || DEFAULT_OBS_URL;
    this.password = opts.password || '';
    this.name = opts.name || 'ObsFusion';
    this.ws = null;
    this.identified = false;
    this.seq = 0;
    this.pending = new Map();
    this.handlers = new Map();
    this.obsVersion = null;
    this.websocketVersion = null;
    this.lastError = null;
  }

  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(fn);
    return this;
  }

  emit(type, data) {
    var list = this.handlers.get(type) || [];
    for (var i = 0; i < list.length; i++) {
      try { list[i](data); } catch (_) {}
    }
  }

  status() {
    return {
      connected: this.identified && this.ws && this.ws.readyState === WebSocket.OPEN,
      connecting: !!this.ws && !this.identified,
      url: this.url,
      obsVersion: this.obsVersion,
      websocketVersion: this.websocketVersion,
      error: this.lastError
    };
  }

  connect(timeoutMs) {
    var self = this;
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.identified) {
      return Promise.resolve(this.status());
    }
    this.close();
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        self.lastError = 'connect timeout';
        try { self.ws && self.ws.close(); } catch (_) {}
        reject(new Error('OBS WebSocket connect timeout'));
      }, timeoutMs || 5000);
      var ws;
      try {
        ws = new WebSocket(self.url, 'obswebsocket.json');
      } catch (err) {
        clearTimeout(timer);
        reject(err);
        return;
      }
      self.ws = ws;
      ws.addEventListener('message', function (event) {
        var msg;
        try { msg = JSON.parse(String(event.data)); } catch (_) { return; }
        if (msg.op === 0) {
          var auth = msg.d && msg.d.authentication;
          var identify = { op: 1, d: { rpcVersion: 1, eventSubscriptions: 33 } };
          if (auth) {
            if (!self.password) {
              self.lastError = 'OBS requires a password';
              try { ws.close(); } catch (_) {}
              return;
            }
            identify.d.authentication = sha256b64(sha256b64(self.password + auth.salt) + auth.challenge);
          }
          ws.send(JSON.stringify(identify));
        } else if (msg.op === 2) {
          self.identified = true;
          self.lastError = null;
          clearTimeout(timer);
          if (!settled) { settled = true; resolve(self.status()); }
          self.emit('identified', self.status());
        } else if (msg.op === 5) {
          self.emit(msg.d.eventType, msg.d.eventData);
        } else if (msg.op === 7) {
          var pending = self.pending.get(msg.d.requestId);
          if (!pending) return;
          self.pending.delete(msg.d.requestId);
          if (msg.d.requestStatus && msg.d.requestStatus.result) {
            pending.resolve(msg.d.responseData || {});
          } else {
            var err = new Error((msg.d.requestStatus && msg.d.requestStatus.comment) || 'OBS request failed');
            err.code = msg.d.requestStatus && msg.d.requestStatus.code;
            pending.reject(err);
          }
        } else if (msg.op === 9) {
          self.lastError = (msg.d && msg.d.error) || 'OBS error';
          self.emit('error', self.lastError);
        }
      });
      ws.addEventListener('error', function () {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          self.lastError = 'WebSocket error: is OBS running and the WebSocket server enabled?';
          reject(new Error(self.lastError));
        }
      });
      ws.addEventListener('close', function () {
        self.identified = false;
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          self.lastError = 'OBS closed the connection (check the WebSocket server password)';
          reject(new Error(self.lastError));
        }
        self.emit('closed', self.status());
      });
    });
  }

  request(requestType, requestData) {
    var self = this;
    if (!this.identified) return Promise.reject(new Error('OBS is not connected'));
    var id = 'of-' + (++this.seq).toString(36) + '-' + Math.random().toString(36).slice(2, 5);
    this.ws.send(JSON.stringify({ op: 6, d: { requestType: requestType, requestId: id, requestData: requestData || {} } }));
    return new Promise(function (resolve, reject) {
      self.pending.set(id, { resolve: resolve, reject: reject });
      setTimeout(function () {
        if (self.pending.has(id)) {
          self.pending.delete(id);
          reject(new Error('OBS request timeout: ' + requestType));
        }
      }, 8000);
    });
  }

  close() {
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
    this.identified = false;
    this.pending.forEach(function (p) { p.reject(new Error('closed')); });
    this.pending.clear();
  }
  // ---- 高层能力 ----

  async getVersion() {
    var r = await this.request('GetVersion');
    this.obsVersion = r.obsVersion;
    this.websocketVersion = r.obsWebSocketVersion;
    return r;
  }

  async getSceneList() {
    var r = await this.request('GetSceneList');
    return r.scenes || [];
  }

  async getCurrentProgramScene() {
    var r = await this.request('GetCurrentProgramScene');
    return r.currentProgramSceneName;
  }

  async createScene(sceneName) {
    try { await this.request('CreateScene', { sceneName: sceneName }); }
    catch (err) { if (!/exists/i.test(err.message)) throw err; }
    return sceneName;
  }

  async ensureBrowserSource(inputName, settings) {
    try {
      await this.request('GetInputSettings', { inputName: inputName });
      await this.request('SetInputSettings', { inputName: inputName, inputSettings: settings, overlay: true });
    } catch (err) {
      var sceneName = await this.getCurrentProgramScene();
      await this.request('CreateInput', {
        sceneName: sceneName,
        inputName: inputName,
        inputKind: INPUT_KIND_BROWSER,
        inputSettings: settings,
        sceneItemEnabled: true
      });
    }
    return inputName;
  }

  async findSceneItem(sceneName, sourceName) {
    var items = await this.request('GetSceneItemList', { sceneName: sceneName });
    var list = items.sceneItems || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].sourceName === sourceName) return list[i].sceneItemId;
    }
    return null;
  }

  async ensureSceneItem(sceneName, sourceName) {
    var existing = await this.findSceneItem(sceneName, sourceName);
    if (existing !== null) return existing;
    var created = await this.request('CreateSceneItem', {
      sceneName: sceneName,
      sourceName: sourceName,
      sceneItemEnabled: true
    });
    return created.sceneItemId;
  }

  async setBrowserUrl(inputName, url, extra) {
    var settings = Object.assign({
      url: url,
      width: 1920,
      height: 1080,
      fps_custom: true,
      fps: 60,
      shutdown: false,
      reroute_audio: false,
      css: ''
    }, extra || {});
    await this.request('SetInputSettings', { inputName: inputName, inputSettings: settings, overlay: true });
    try { await this.request('PressInputPropertiesButton', { inputName: inputName, propertyName: 'refreshnocache' }); } catch (_) {}
    return settings;
  }

  async setSceneItemTransform(sceneName, sceneItemId, transform) {
    return this.request('SetSceneItemTransform', { sceneName: sceneName, sceneItemId: sceneItemId, sceneItemTransform: transform });
  }

  async applyOverlay(opts) {
    var sceneName = opts.sceneName || 'ObsFusion';
    var sourceName = opts.sourceName || 'ObsFusion Overlay';
    var url = opts.url;
    if (!url) throw new Error('applyOverlay requires url');
    await this.createScene(sceneName);
    var settings = await this.setBrowserUrl(sourceName, url, opts.settings);
    await this.ensureBrowserSource(sourceName, settings);
    var itemId = await this.ensureSceneItem(sceneName, sourceName);
    await this.setSceneItemTransform(sceneName, itemId, {
      positionX: 0, positionY: 0, alignment: 5,
      boundsAlignment: 5, boundsType: 'OBS_BOUNDS_SCALE_INNER',
      boundsWidth: opts.width || 1920, boundsHeight: opts.height || 1080
    });
    return { sceneName: sceneName, sourceName: sourceName, sceneItemId: itemId, url: url };
  }
}
