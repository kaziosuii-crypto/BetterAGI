import { EventEmitter } from 'node:events';
import WebSocket from 'ws';

const ROLE_PROTOCOL = 'laplace-event-bridge-role-server';

/**
 * Outbound connection to a LAPLACE Event Bridge (leb-server) as a "client".
 *
 * The bridge only knows two roles: the LAPLACE dashboard connects as
 * "server" (producer) and everything else connects without a subprotocol.
 * We therefore connect as a plain client and receive every broadcast event.
 */
export class BridgeClient extends EventEmitter {
  constructor(options) {
    super();
    const opts = options || {};
    this.url = opts.url;
    this.token = opts.token || '';
    this.reconnect = opts.reconnect !== false;
    this.roomId = Number(opts.roomId) || 0;
    this.log = opts.log || (() => {});
    this.ws = null;
    this.stopped = false;
    this.attempt = 0;
    this.connected = false;
    this.lastMessageAt = 0;
    this.received = 0;
    this.reconnectTimer = null;
    this.pingTimer = null;
    this.heartbeatTimer = null;
  }

  start() {
    this.stopped = false;
    this.connect();
    return this;
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.reconnectTimer);
    clearInterval(this.heartbeatTimer);
    if (this.ws) {
      try { this.ws.close(1000, 'relay shutting down'); } catch (_) {}
      this.ws = null;
    }
    this.connected = false;
  }

  status() {
    return {
      url: this.url,
      connected: this.connected,
      attempts: this.attempt,
      received: this.received,
      last_message_at: this.lastMessageAt || null,
    };
  }

  connect() {
    if (this.stopped) return;
    const protocols = this.token ? ['laplace-event-bridge-role-client', this.token] : undefined;
    let ws;
    try {
      ws = new WebSocket(this.url, protocols, { handshakeTimeout: 8000 });
    } catch (error) {
      this.log('error', 'bridge: failed to create socket: ' + error.message);
      return this.scheduleReconnect();
    }
    this.ws = ws;

    ws.on('open', () => {
      this.log('info', 'bridge: socket open ' + this.url);
      this.emit('open');
    });

    ws.on('message', (buffer) => {
      let payload;
      const text = buffer.toString();
      try {
        payload = JSON.parse(text);
      } catch (_) {
        return this.log('warn', 'bridge: non-JSON message ignored: ' + text.slice(0, 120));
      }
      this.lastMessageAt = Date.now();
      this.handlePayload(payload);
    });

    ws.on('close', (code, reason) => {
      const wasConnected = this.connected;
      this.connected = false;
      this.ws = null;
      this.log(wasConnected ? 'warn' : 'info', 'bridge: closed code=' + code + ' reason=' + reason.toString());
      this.emit('close', code);
      if (!this.stopped) this.scheduleReconnect();
    });

    ws.on('error', (error) => {
      this.log('warn', 'bridge: ' + (error.message || String(error)));
      this.emit('error', error);
    });
  }

  scheduleReconnect() {
    if (!this.reconnect || this.stopped) return;
    this.attempt += 1;
    const base = Math.min(10000, 400 * Math.pow(1.7, Math.min(this.attempt, 8)));
    const delay = Math.round(base + Math.random() * 200);
    this.log('info', 'bridge: reconnect in ' + delay + 'ms (attempt ' + this.attempt + ')');
    this.emit('reconnecting', { attempt: this.attempt, delay });
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  handlePayload(payload) {
    if (!payload || typeof payload !== 'object') return;
    const type = payload.type;

    if (type === 'established') {
      this.connected = true;
      this.attempt = 0;
      this.log('info', 'bridge: established as ' + payload.clientId + ' server=' + Boolean(payload.isServer));
      this.emit('established', payload);
      this.startHeartbeat();
      return;
    }

    if (type === 'ping') {
      this.replyPong(payload);
      return;
    }

    if (type === 'broadcast-success' || type === 'client-message-received' || type === 'error') {
      this.emit('control', payload);
      return;
    }

    if (type === 'unknown-message') {
      this.log('warn', 'bridge: unknown-message: ' + String(payload.text || '').slice(0, 120));
      return;
    }

    this.received += 1;
    this.emit('event', payload);
  }

  replyPong(ping) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    try {
      this.ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now(), respondingTo: ping.timestamp }));
    } catch (_) {}
  }

  startHeartbeat() {
    clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = setInterval(() => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      try { this.ws.ping(); } catch (_) {}
      if (this.lastMessageAt && Date.now() - this.lastMessageAt > 90000) {
        this.log('warn', 'bridge: no traffic for 90s, recycling socket');
        try { this.ws.terminate(); } catch (_) {}
      }
    }, 30000);
    if (this.heartbeatTimer.unref) this.heartbeatTimer.unref();
  }
}

export { ROLE_PROTOCOL };
