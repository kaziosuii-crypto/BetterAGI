import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { WebSocketServer } from 'ws';
import { KIND_META, FAMILIES } from '../events/kinds.js';
import { normalizeFilter } from '../core/hub.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = path.join(HERE, '..', 'panel', 'index.html');

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'access-control-allow-origin': '*',
    'cache-control': 'no-store',
  });
  res.end(body);
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > (limit || 1_000_000)) {
        reject(new Error('payload too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export function createRelayServer(hub, bridge, options) {
  const opts = options || {};
  const config = hub.config;
  const log = hub.log;
  const wss = new WebSocketServer({ noServer: true, clientTracking: false });

  function authorized(req, url) {
    if (!config.ingestToken) return true;
    const header = req.headers['x-relay-token'];
    return header === config.ingestToken || url.searchParams.get('token') === config.ingestToken;
  }

  const server = http.createServer(async (req, res) => {
    let url;
    try {
      url = new URL(req.url, 'http://' + (req.headers.host || 'localhost'));
    } catch (_) {
      return sendJson(res, 400, { error: 'bad url' });
    }
    const pathname = url.pathname;
    const method = req.method || 'GET';

    if (method === 'OPTIONS') {
      res.writeHead(204, {
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET,POST,OPTIONS',
        'access-control-allow-headers': 'content-type,x-relay-token',
      });
      return res.end();
    }

    try {
      if (pathname === '/health') {
        return sendJson(res, 200, {
          ok: true,
          service: 'laplace-relay',
          version: config.version,
          uptime_s: Math.round(process.uptime()),
          bridge: bridge ? bridge.status() : null,
          subscribers: hub.countSubscribers(),
        });
      }

      if (pathname === '/api/state') {
        return sendJson(res, 200, hub.state());
      }

      if (pathname === '/api/kinds') {
        return sendJson(res, 200, { kinds: KIND_META, families: FAMILIES });
      }

      if (pathname === '/api/events') {
        const events = hub.recent({
          limit: url.searchParams.get('limit'),
          kind: url.searchParams.get('kind'),
          family: url.searchParams.get('family'),
          reaction: url.searchParams.get('reaction') === '1',
          sinceSeq: url.searchParams.get('since'),
        });
        return sendJson(res, 200, { count: events.length, events });
      }

      if (pathname === '/api/acks') {
        return sendJson(res, 200, { acks: hub.acks.slice(-100) });
      }

      if (pathname === '/api/ingest' && method === 'POST') {
        if (!authorized(req, url)) return sendJson(res, 401, { error: 'unauthorized' });
        const text = await readBody(req);
        let payload;
        try {
          payload = JSON.parse(text);
        } catch (_) {
          return sendJson(res, 400, { error: 'invalid json' });
        }
        const list = Array.isArray(payload) ? payload : [payload];
        const accepted = [];
        for (const raw of list) {
          const envelope = hub.ingest(raw, { transport: 'http-ingest' });
          if (envelope) accepted.push(envelope.seq);
        }
        return sendJson(res, 202, { accepted: accepted.length, seqs: accepted });
      }

      if (pathname === '/api/ack' && method === 'POST') {
        const text = await readBody(req);
        let payload;
        try {
          payload = JSON.parse(text);
        } catch (_) {
          return sendJson(res, 400, { error: 'invalid json' });
        }
        const record = hub.recordAck(payload, null);
        return sendJson(res, record ? 202 : 400, record || { error: 'event_id required' });
      }

      if (pathname === '/events' || pathname === '/api/stream') {
        return streamSse(req, res, url);
      }

      if (pathname === '/' || pathname === '/panel' || pathname === '/panel/') {
        const html = await readFile(PANEL, 'utf8');
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
        return res.end(html);
      }

      return sendJson(res, 404, { error: 'not found', path: pathname });
    } catch (error) {
      log('error', 'http ' + pathname + ': ' + (error.stack || error.message));
      if (!res.headersSent) return sendJson(res, 500, { error: 'internal error' });
      return res.end();
    }
  });

  function streamSse(req, res, url) {
    const filter = {
      kinds: url.searchParams.get('kinds'),
      families: url.searchParams.get('families'),
      minWeight: url.searchParams.get('min_weight'),
      reactionsOnly: url.searchParams.get('reaction') === '1',
    };
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'access-control-allow-origin': '*',
      'x-accel-buffering': 'no',
    });
    res.write(': connected ' + new Date().toISOString() + '\n\n');
    const sub = hub.addSubscriber({ kind: 'sse', res }, filter);
    const keepAlive = setInterval(() => {
      try { res.write(': ping\n\n'); } catch (_) {}
    }, 15000);
    if (keepAlive.unref) keepAlive.unref();
    sub.close = () => {
      clearInterval(keepAlive);
      try { res.end(); } catch (_) {}
    };
    req.on('close', () => {
      clearInterval(keepAlive);
      hub.removeSubscriber(sub);
    });
  }

  server.on('upgrade', (req, socket, head) => {
    let url;
    try {
      url = new URL(req.url, 'http://' + (req.headers.host || 'localhost'));
    } catch (_) {
      return socket.destroy();
    }
    const pathname = url.pathname;

    if (pathname === '/ws/stream' || pathname === '/stream') {
      wss.handleUpgrade(req, socket, head, (ws) => {
        const filter = {
          kinds: url.searchParams.get('kinds'),
          families: url.searchParams.get('families'),
          minWeight: url.searchParams.get('min_weight'),
          reactionsOnly: url.searchParams.get('reaction') === '1',
        };
        const sub = hub.addSubscriber({ kind: 'ws', socket: ws }, filter, { remote: req.socket.remoteAddress });
        sub.close = () => {
          try { ws.close(1001, 'relay closing'); } catch (_) {}
        };
        ws.send(JSON.stringify({
          t: 'welcome',
          subscriber_id: sub.id,
          session_id: hub.session.id,
          server_time: Date.now(),
          filter: { kinds: filter.kinds, families: filter.families, min_weight: filter.minWeight, reactions_only: filter.reactionsOnly },
          ack_format: '{"t":"ack","event_id":"<id>","action":"...","note":"..."}',
        }));
        ws.on('message', (buffer) => {
          let msg;
          try {
            msg = JSON.parse(buffer.toString());
          } catch (_) {
            return;
          }
          if (!msg || typeof msg !== 'object') return;
          if (msg.t === 'ack' || msg.type === 'ack') {
            const record = hub.recordAck(msg, sub);
            if (record) ws.send(JSON.stringify({ t: 'ack-ok', event_id: record.event_id }));
            return;
          }
          if (msg.t === 'ping') {
            ws.send(JSON.stringify({ t: 'pong', server_time: Date.now() }));
            return;
          }
          if (msg.t === 'filter') {
            sub.filter = normalizeFilter(msg.filter || msg);
            ws.send(JSON.stringify({ t: 'filter-ok' }));
          }
        });
        ws.on('close', () => hub.removeSubscriber(sub));
        ws.on('error', () => hub.removeSubscriber(sub));
      });
      return;
    }

    if (pathname === '/bridge' || pathname === '/ws/bridge') {
      const protocols = String(req.headers['sec-websocket-protocol'] || '')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
      const wantsServer = protocols.some((item) => item.indexOf('role-server') !== -1);
      wss.handleUpgrade(req, socket, head, (ws) => {
        const clientId = 'relay-' + Math.random().toString(36).slice(2, 8);
        if (protocols.length) ws.protocol = protocols[0];
        ws.send(JSON.stringify({
          type: 'established',
          clientId,
          isServer: wantsServer,
          message: 'Connected to laplace-relay ingestor ' + config.version,
        }));
        ws.on('message', (buffer) => {
          const text = buffer.toString();
          let payload;
          try {
            payload = JSON.parse(text);
          } catch (_) {
            return;
          }
          if (!payload || typeof payload !== 'object') return;
          if (payload.type === 'ping') {
            try {
              ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now(), respondingTo: payload.timestamp }));
            } catch (_) {}
            return;
          }
          if (payload.type === 'established') return;
          hub.ingest(payload, { transport: 'inbound-bridge' });
        });
        ws.on('close', () => {});
        ws.on('error', () => {});
      });
      return;
    }

    socket.destroy();
  });

  return { server, wss };
}
