#!/usr/bin/env node
import { pathToFileURL } from 'node:url';
import { loadConfig } from './config.js';
import { Hub } from './core/hub.js';
import { BridgeClient } from './bridge/client.js';
import { createMockSource } from './bridge/mock.js';
import { createRelayServer } from './server/http.js';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };

export async function main(argv, env) {
  const config = loadConfig(argv, env);
  const threshold = LEVELS[config.logLevel] || LEVELS.info;
  const startedAt = Date.now();

  function log(level, message) {
    if ((LEVELS[level] || LEVELS.info) < threshold) return;
    const stamp = new Date().toISOString().slice(11, 23);
    const line = stamp + ' ' + level.toUpperCase().padEnd(5) + ' ' + message;
    if (level === 'error') process.stderr.write(line + '\n');
    else process.stdout.write(line + '\n');
  }

  const hub = new Hub({ config, log });
  await hub.start();

  let bridge = null;
  if (config.source === 'bridge' || config.source === 'both') {
    bridge = new BridgeClient({ url: config.bridgeUrl, token: config.bridgeToken, roomId: config.roomId, log });
    bridge.on('event', (raw) => hub.ingest(raw, { transport: 'laplace-bridge' }));
    bridge.on('established', (info) => log('info', 'bridge handshake: ' + info.clientId + ' isServer=' + info.isServer));
    bridge.on('reconnecting', (info) => log('info', 'bridge reconnecting in ' + info.delay + 'ms'));
    bridge.on('error', () => {});
    bridge.start();
    log('info', 'bridge source enabled -> ' + config.bridgeUrl);
  }

  let mockTimer = null;
  if (config.source === 'mock' || config.source === 'both') {
    const mock = createMockSource({ roomId: config.roomId || 1001, seed: config.mockSeed });
    const tick = () => {
      const raw = mock.next();
      hub.ingest(raw, { transport: 'mock' });
      const jitter = config.mockIntervalMs * (0.5 + Math.random());
      mockTimer = setTimeout(tick, jitter);
      if (mockTimer.unref) mockTimer.unref();
    };
    mockTimer = setTimeout(tick, 300);
    log('info', 'mock source enabled (room ' + mock.roomId + ', seed ' + mock.seed + ', ~' + config.mockIntervalMs + 'ms)');
  }

  const { server } = createRelayServer(hub, bridge, {});
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(config.port, config.host, () => resolve());
  });

  const url = 'http://' + config.host + ':' + config.port;
  log('info', 'relay listening on ' + url);
  log('info', '  panel   ' + url + '/panel');
  log('info', '  state   ' + url + '/api/state');
  log('info', '  events  ' + url + '/api/events?limit=50');
  log('info', '  ws      ws://' + config.host + ':' + config.port + '/ws/stream');
  log('info', '  sse     ' + url + '/events');
  if (config.webhookUrls.length) log('info', 'webhooks -> ' + config.webhookUrls.join(', '));

  let shuttingDown = false;
  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log('info', 'received ' + signal + ', shutting down');
    if (mockTimer) clearTimeout(mockTimer);
    if (bridge) bridge.stop();
    hub.stop();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 2500).unref();
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  return { server, hub, bridge, config, startedAt };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2), process.env).catch((error) => {
    process.stderr.write('fatal: ' + (error.stack || error.message) + '\n');
    process.exit(1);
  });
}
