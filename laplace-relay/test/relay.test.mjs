import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeEvent } from '../src/events/normalize.js';
import { createMockSource } from '../src/bridge/mock.js';
import { Hub, normalizeFilter, matches } from '../src/core/hub.js';
import { createFakeBridge } from './fake-bridge.mjs';
import { createRelayServer } from '../src/server/http.js';

const baseRaw = () => ({ id: 'r1', origin: 555, uid: 7, username: '观众', timestamp: Date.now() });

test('message becomes chat envelope with actor metadata', () => {
  const env = normalizeEvent({ ...baseRaw(), type: 'message', message: '你好', guardType: 3, userType: 1, userLvl: 30, medal: { name: '牌子', level: 12 } }, {});
  assert.equal(env.kind, 'message');
  assert.equal(env.family, 'chat');
  assert.equal(env.text, '你好');
  assert.equal(env.actor.guard_name, '舰长');
  assert.equal(env.actor.role, 'moderator');
  assert.equal(env.actor.badge.level, 12);
  assert.equal(env.id, 'message:r1');
  assert.equal(env.v, 1);
});

test('gift carries real CNY amount and asks for a reaction', () => {
  const env = normalizeEvent({ ...baseRaw(), type: 'gift', giftName: '干杯', giftAmount: 3, price: 150000, priceNormalized: 150, coinType: 'gold' }, {});
  assert.equal(env.kind, 'gift');
  assert.equal(env.family, 'money');
  assert.equal(env.amount.cny, 150);
  assert.equal(env.interaction.needs_reaction, true);
  assert.ok(env.interaction.suggested.includes('thank_gift'));
  assert.equal(env.data.count, 3);
});

test('silver-coin gift is converted to an approximate CNY value', () => {
  const env = normalizeEvent({ ...baseRaw(), type: 'gift', giftName: '辣条', giftAmount: 10, price: 2000, coinType: 'silver' }, {});
  assert.equal(env.amount.currency, 'silver');
  assert.equal(env.amount.approx, true);
  assert.equal(env.amount.cny, 2);
});

test('superchat always requests a reaction and keeps translation', () => {
  const env = normalizeEvent({ ...baseRaw(), type: 'superchat', message: '点首歌', price: 50000, priceNormalized: 50, scId: 99, messageTrans: 'sing a song', transMark: 1 }, {});
  assert.equal(env.kind, 'superchat');
  assert.equal(env.interaction.weight, 100);
  assert.equal(env.data.translated, 'sing a song');
  assert.equal(env.data.sc_id, 99);
});

test('unknown event type degrades to kind=unknown without throwing', () => {
  const env = normalizeEvent({ ...baseRaw(), type: 'future-thing', message: '?' }, {});
  assert.equal(env.kind, 'unknown');
  assert.equal(env.family, 'unknown');
  assert.ok(env.data.keys.includes('type'));
});

test('room filter drops other rooms', () => {
  const raw = { ...baseRaw(), type: 'message', message: 'x' };
  assert.equal(normalizeEvent(raw, { roomId: 999 }), null);
  assert.ok(normalizeEvent(raw, { roomId: 555 }));
});

test('timestamp normalisation handles ns and seconds', () => {
  const ns = normalizeEvent({ ...baseRaw(), type: 'like-click', timestamp: 1700000000123456789 }, {});
  assert.equal(ns.ts, 1700000000123);
  const sec = normalizeEvent({ ...baseRaw(), type: 'like-click', timestamp: 1700000000 }, {});
  assert.equal(sec.ts, 1700000000000);
});

test('mock source is deterministic for a given seed', () => {
  const a = createMockSource({ seed: 42, roomId: 1 });
  const b = createMockSource({ seed: 42, roomId: 1 });
  const strip = (e) => ({ ...e, id: null, idStr: null, timestamp: null, timestampNormalized: null, stableKey: null });
  for (let i = 0; i < 50; i += 1) assert.deepEqual(strip(a.next()), strip(b.next()));
});

test('hub assigns monotonic seq and stats by family', () => {
  const hub = new Hub({ config: { ringSize: 10, webhookUrls: [], dataDir: null, includeRaw: false, roomId: 0, webhookMode: 'reaction', webhookMinWeight: 0 } });
  hub.ingest({ ...baseRaw(), type: 'message', message: 'a' }, {});
  hub.ingest({ ...baseRaw(), id: 'r2', type: 'gift', giftName: 'g', giftAmount: 1, priceNormalized: 10, price: 10000, coinType: 'gold' }, {});
  assert.equal(hub.session.total, 2);
  assert.equal(hub.session.by_family.chat, 1);
  assert.equal(hub.session.by_family.money, 1);
  assert.equal(hub.session.amount_cny, 10);
  const list = hub.recent({});
  assert.deepEqual(list.map((e) => e.seq), [1, 2]);
});

test('ring buffer keeps only the newest entries', () => {
  const hub = new Hub({ config: { ringSize: 3, webhookUrls: [], dataDir: null, includeRaw: false, roomId: 0, webhookMode: 'reaction', webhookMinWeight: 0 } });
  for (let i = 0; i < 10; i += 1) hub.ingest({ ...baseRaw(), id: 'x' + i, type: 'message', message: 'm' + i }, {});
  assert.equal(hub.ring.length, 3);
  assert.equal(hub.recent({})[0].data.user_level, 0);
  assert.equal(hub.recent({ limit: 1 })[0].seq, 10);
});

test('filter matching supports kinds, families and reaction-only', () => {
  const hub = new Hub({ config: { ringSize: 10, webhookUrls: [], dataDir: null, includeRaw: false, roomId: 0, webhookMode: 'reaction', webhookMinWeight: 0 } });
  const chat = hub.ingest({ ...baseRaw(), type: 'message', message: 'hi' }, {});
  const gift = hub.ingest({ ...baseRaw(), id: 'g', type: 'gift', giftName: 'g', giftAmount: 1, price: 1000, priceNormalized: 1, coinType: 'gold' }, {});
  assert.equal(matches(normalizeFilter({ families: 'chat' }), chat), true);
  assert.equal(matches(normalizeFilter({ families: 'chat' }), gift), false);
  assert.equal(matches(normalizeFilter({ kinds: 'gift' }), gift), true);
  assert.equal(matches(normalizeFilter({ reactions_only: true }), chat), false);
  assert.equal(matches(normalizeFilter({ reactions_only: true }), gift), true);
  assert.equal(matches(normalizeFilter({ min_weight: 90 }), gift), false);
});

test('ack marks a reaction as handled', () => {
  const hub = new Hub({ config: { ringSize: 10, webhookUrls: [], dataDir: null, includeRaw: false, roomId: 0, webhookMode: 'reaction', webhookMinWeight: 0 } });
  const env = hub.ingest({ ...baseRaw(), type: 'superchat', message: 'sc', price: 50000, priceNormalized: 50 }, {});
  assert.equal(hub.pendingReactionCount(), 1);
  const rec = hub.recordAck({ event_id: env.id, action: 'thank' }, null);
  assert.equal(rec.action, 'thank');
  assert.equal(hub.pendingReactionCount(), 0);
  assert.equal(hub.session.reaction_count, 1);
});

test('end-to-end: bridge -> hub -> subscriber -> ack', async () => {
  const fake = await createFakeBridge();
  const config = {
    version: 'test', host: '127.0.0.1', port: 0, bridgeUrl: fake.url, source: 'bridge', roomId: 0,
    includeRaw: false, dataDir: null, webhookUrls: [], webhookSecret: '', ringSize: 50,
    webhookMode: 'reaction', webhookMinWeight: 0, ingestToken: '', logLevel: 'warn',
  };
  const hub = new Hub({ config, log: () => {} });
  const { server } = createRelayServer(hub, null, {});
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;

  const { BridgeClient } = await import('../src/bridge/client.js');
  const client = new BridgeClient({ url: fake.url, reconnect: false });
  client.on('event', (raw) => hub.ingest(raw, { transport: 'laplace-bridge' }));
  client.on('established', () => {
    fake.broadcast({ ...baseRaw(), type: 'gift', giftName: '舰长', giftAmount: 1, price: 198000, priceNormalized: 198, coinType: 'gold' });
  });
  client.start();

  const { default: WebSocket } = await import('ws');
  const ws = new WebSocket('ws://127.0.0.1:' + port + '/ws/stream');
  const received = [];
  await new Promise((resolve, reject) => {
    ws.on('open', resolve);
    ws.on('error', reject);
  });

  ws.on('message', (buf) => {
    const msg = JSON.parse(buf.toString());
    if (msg.t === 'event') received.push(msg.data);

  });

  const deadline = Date.now() + 5000;
  while (!received.length && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
  assert.ok(received.length >= 1, 'subscriber received the relayed event');
  const ev = received[0];
  assert.equal(ev.kind, 'gift');
  assert.equal(ev.amount.cny, 198);
  assert.equal(ev.interaction.needs_reaction, true);
  assert.equal(ev.source.transport, 'laplace-bridge');

  ws.send(JSON.stringify({ t: 'ack', event_id: ev.id, action: 'thank' }));
  const ackDeadline = Date.now() + 3000;
  while (hub.session.reaction_count === 0 && Date.now() < ackDeadline) await new Promise((r) => setTimeout(r, 25));
  assert.equal(hub.session.reaction_count, 1);
  assert.equal(hub.pendingReactionCount(), 0);

  const state = await (await fetch('http://127.0.0.1:' + port + '/api/state')).json();
  assert.equal(state.session.total, 1);
  assert.equal(state.session.amount_cny, 198);

  ws.close();
  client.stop();
  hub.stop();
  await new Promise((resolve) => server.close(resolve));
  await fake.close();
});

test('inbound bridge socket accepts dashboard-style events', async () => {
  const config = { version: 'test', ingestToken: '', dataDir: null, ringSize: 20, webhookUrls: [], roomId: 0, includeRaw: false, webhookMode: 'reaction', webhookMinWeight: 0 };
  const hub = new Hub({ config, log: () => {} });
  const { server } = createRelayServer(hub, null, {});
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const { default: WebSocket } = await import('ws');
  const ws = new WebSocket('ws://127.0.0.1:' + port + '/bridge');
  const established = [];
  await new Promise((resolve, reject) => {
    ws.on('message', (buf) => {
      const msg = JSON.parse(buf.toString());
      if (msg.type === 'established') { established.push(msg); resolve(); }
      if (msg.type === 'ping') ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now(), respondingTo: msg.timestamp }));
    });
    ws.on('error', reject);
  });
  assert.equal(established[0].isServer, false);
  ws.send(JSON.stringify({ ...baseRaw(), type: 'message', message: '来自控制台' }));
  const deadline = Date.now() + 3000;
  while (hub.session.total === 0 && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
  assert.equal(hub.recent({})[0].text, '来自控制台');
  assert.equal(hub.recent({})[0].source.transport, 'inbound-bridge');
  ws.close();
  hub.stop();
  await new Promise((resolve) => server.close(resolve));
});
