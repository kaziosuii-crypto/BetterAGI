import { EventEmitter } from 'node:events';
import { createHmac, randomUUID } from 'node:crypto';
import { appendFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { normalizeEvent } from '../events/normalize.js';
import { KIND_META, familyOf } from '../events/kinds.js';

const MAX_ACKS = 300;
const RATE_WINDOW_MS = 60000;
const RATE_BUCKET_MS = 5000;

/**
 * Central fan-out hub.
 *
 * Everything entering the relay (bridge connection, inbound dashboard socket,
 * mock generator, HTTP ingest) becomes the same normalised envelope, gets a
 * monotonic seq, is written to the ring buffer + JSONL log, and is pushed to
 * every subscriber whose filter matches.
 */
export class Hub extends EventEmitter {
  constructor(options) {
    super();
    const opts = options || {};
    this.config = opts.config;
    this.log = opts.log || (() => {});
    this.session = {
      id: randomUUID(),
      started_at: new Date().toISOString(),
      rooms: new Set(),
      total: 0,
      by_kind: {},
      by_family: {},
      amount_cny: 0,
      reaction_count: 0,
      last_event_at: null,
      seq: 0,
    };
    this.ring = [];
    this.ringSize = this.config.ringSize || 500;
    this.subscribers = new Set();
    this.acks = [];
    this.ackedIds = new Set();
    this.buckets = [];
    this.hooks = [];
    this.hookQueue = [];
    this.hookActive = 0;
    this.logFile = null;
    this.logDay = null;
    this.dropped = 0;
  }

  async start() {
    if (this.config.dataDir) {
      await mkdir(this.config.dataDir, { recursive: true });
    }
    return this;
  }

  stop() {
    for (const sub of this.subscribers) this.closeSubscriber(sub, 'relay shutting down');
    this.subscribers.clear();
  }

  /** Normalise + publish one raw LAPLACE event. Returns the envelope or null. */
  ingest(raw, meta) {
    const info = meta || {};
    let envelope;
    try {
      envelope = normalizeEvent(raw, {
        includeRaw: this.config.includeRaw,
        roomId: this.config.roomId,
      });
    } catch (error) {
      this.log('warn', 'ingest: rejected event: ' + error.message);
      return null;
    }
    if (!envelope) return null;
    envelope.seq = ++this.session.seq;
    envelope.source.transport = info.transport || envelope.source.transport;
    if (info.subscriber) envelope.source.subscriber = info.subscriber;
    this.publish(envelope);
    return envelope;
  }

  publish(envelope) {
    const stats = this.session;
    stats.total += 1;
    stats.by_kind[envelope.kind] = (stats.by_kind[envelope.kind] || 0) + 1;
    stats.by_family[envelope.family] = (stats.by_family[envelope.family] || 0) + 1;
    if (envelope.amount && envelope.amount.cny) stats.amount_cny += envelope.amount.cny;
    stats.last_event_at = new Date().toISOString();
    if (envelope.room.id) stats.rooms.add(envelope.room.id);

    this.ring.push(envelope);
    if (this.ring.length > this.ringSize) this.ring.splice(0, this.ring.length - this.ringSize);

    this.bumpRate();
    this.appendLog(envelope);

    for (const sub of this.subscribers) {
      if (!matches(sub.filter, envelope)) continue;
      this.deliver(sub, envelope);
    }
    this.enqueueHooks(envelope);
    this.emit('event', envelope);
    return envelope;
  }

  // ---------------------------------------------------------------- ring/log

  appendLog(envelope) {
    if (!this.config.dataDir) return;
    const day = new Date(envelope.ts).toISOString().slice(0, 10);
    if (day !== this.logDay) {
      this.logDay = day;
      this.logFile = path.join(this.config.dataDir, 'events-' + day + '.jsonl');
    }
    const line = JSON.stringify(envelope) + '\n';
    appendFile(this.logFile, line).catch((error) => this.log('warn', 'log append failed: ' + error.message));
  }

  recent(query) {
    const q = query || {};
    const limit = Math.max(1, Math.min(Number(q.limit) || 100, this.ringSize));
    const sinceSeq = Number(q.sinceSeq) || 0;
    let list = this.ring;
    if (sinceSeq) list = list.filter((item) => item.seq > sinceSeq);
    if (q.kind) list = list.filter((item) => item.kind === q.kind);
    if (q.family) list = list.filter((item) => item.family === q.family);
    if (q.reaction) list = list.filter((item) => item.interaction.needs_reaction);
    return list.slice(-limit);
  }

  // ------------------------------------------------------------- subscribers

  addSubscriber(transport, filter, meta) {
    const sub = {
      id: randomUUID().slice(0, 8),
      transport,
      filter: normalizeFilter(filter),
      meta: meta || {},
      sent: 0,
      dropped: 0,
      queue: [],
      draining: false,
      connected_at: new Date().toISOString(),
      close: null,
    };
    this.subscribers.add(sub);
    this.emit('subscriber', { action: 'open', id: sub.id, transport: transport.kind });
    return sub;
  }

  removeSubscriber(sub) {
    if (!this.subscribers.has(sub)) return;
    this.subscribers.delete(sub);
    this.emit('subscriber', { action: 'close', id: sub.id, transport: sub.transport.kind });
  }

  closeSubscriber(sub, reason) {
    try {
      if (sub.close) sub.close(reason);
    } catch (_) {}
    this.removeSubscriber(sub);
  }

  deliver(sub, envelope) {
    const transport = sub.transport;
    if (transport.kind === 'ws') {
      const ws = transport.socket;
      if (!ws || ws.readyState !== 1) {
        return this.removeSubscriber(sub);
      }
      if (ws.bufferedAmount > 2 * 1024 * 1024) {
        sub.dropped += 1;
        this.dropped += 1;
        return;
      }
      try {
        ws.send(JSON.stringify({ t: 'event', data: envelope }));
        sub.sent += 1;
      } catch (error) {
        this.log('warn', 'ws send failed: ' + error.message);
        this.removeSubscriber(sub);
      }
      return;
    }
    if (transport.kind === 'sse') {
      const res = transport.res;
      if (!res || res.writableEnded) return this.removeSubscriber(sub);
      try {
        res.write('id: ' + envelope.seq + '\n');
        res.write('event: ' + envelope.kind + '\n');
        res.write('data: ' + JSON.stringify(envelope) + '\n\n');
        sub.sent += 1;
      } catch (error) {
        this.log('warn', 'sse write failed: ' + error.message);
        this.removeSubscriber(sub);
      }
    }
  }

  countSubscribers() {
    const counts = { ws: 0, sse: 0 };
    for (const sub of this.subscribers) counts[sub.transport.kind] = (counts[sub.transport.kind] || 0) + 1;
    return counts;
  }

  // ------------------------------------------------------------------- stats

  bumpRate() {
    const now = Date.now();
    let bucket = this.buckets[this.buckets.length - 1];
    if (!bucket || now - bucket.t >= RATE_BUCKET_MS) {
      bucket = { t: now, n: 0 };
      this.buckets.push(bucket);
      const cutoff = now - RATE_WINDOW_MS;
      this.buckets = this.buckets.filter((item) => item.t >= cutoff);
    }
    bucket.n += 1;
  }

  ratePerMinute() {
    const cutoff = Date.now() - RATE_WINDOW_MS;
    const total = this.buckets.filter((item) => item.t >= cutoff).reduce((sum, item) => sum + item.n, 0);
    return Math.round((total * 60000) / RATE_WINDOW_MS);
  }

  state() {
    return {
      session: {
        id: this.session.id,
        started_at: this.session.started_at,
        rooms: Array.from(this.session.rooms),
        total: this.session.total,
        by_kind: this.session.by_kind,
        by_family: this.session.by_family,
        amount_cny: Math.round(this.session.amount_cny * 100) / 100,
        reaction_count: this.session.reaction_count,
        pending_reactions: this.pendingReactionCount(),
        last_event_at: this.session.last_event_at,
        seq: this.session.seq,
        rate_per_min: this.ratePerMinute(),
        dropped: this.dropped,
      },
      subscribers: this.countSubscribers(),
      ring: { size: this.ring.length, capacity: this.ringSize },
      hooks: { configured: this.config.webhookUrls.length, queued: this.hookQueue.length, pending: this.hookActive },
      acks: this.acks.slice(-20),
      kinds: KIND_META,
      config: {
        rooms: this.config.roomId || 'any',
        source: this.config.source,
        bridge: this.config.bridgeUrl,
        include_raw: this.config.includeRaw,
        webhook_min_weight: this.config.webhookMinWeight,
        verify_webhook: Boolean(this.config.webhookSecret),
      },
    };
  }

  // -------------------------------------------------------------------- acks

  pendingReactionCount() {
    const acked = this.ackedIds;
    return this.ring.filter((item) => item.interaction.needs_reaction && !acked.has(item.id)).length;
  }

  recordAck(ack, sub) {
    if (!ack || !ack.event_id) return null;
    const event = this.ring.find((item) => item.id === ack.event_id);
    this.ackedIds.add(String(ack.event_id));
    const record = {
      event_id: String(ack.event_id),
      seq: event ? event.seq : null,
      kind: event ? event.kind : null,
      action: String(ack.action || 'seen').slice(0, 40),
      note: ack.note ? String(ack.note).slice(0, 500) : null,
      subscriber: sub ? sub.id : null,
      acknowledged_at: new Date().toISOString(),
      latency_ms: event ? Date.now() - event.ts : null,
    };
    this.acks.push(record);
    if (this.acks.length > MAX_ACKS) this.acks.shift();
    this.session.reaction_count += 1;
    this.emit('ack', record);
    return record;
  }

  // ---------------------------------------------------------------- webhooks

  enqueueHooks(envelope) {
    if (!this.config.webhookUrls.length) return;
    const mode = this.config.webhookMode || 'reaction';
    if (mode === 'reaction' && !envelope.interaction.needs_reaction) return;
    if (envelope.interaction.weight < this.config.webhookMinWeight) return;
    for (const url of this.config.webhookUrls) {
      this.hookQueue.push({ url, envelope, attempts: 0, nextAt: 0 });
      if (this.hookQueue.length > 500) this.hookQueue.shift();
    }
    this.pumpHooks();
  }

  pumpHooks() {
    while (this.hookActive < 2 && this.hookQueue.length) {
      const job = this.hookQueue.shift();
      if (job.nextAt > Date.now()) {
        this.hookQueue.push(job);
        return;
      }
      this.hookActive += 1;
      this.sendHook(job)
        .catch(() => {})
        .finally(() => {
          this.hookActive -= 1;
          this.pumpHooks();
        });
    }
  }

  async sendHook(job) {
    const body = JSON.stringify({ event: job.envelope });
    const headers = { 'content-type': 'application/json', 'x-relay-event': job.envelope.kind, 'x-relay-seq': String(job.envelope.seq) };
    if (this.config.webhookSecret) {
      headers['x-relay-signature'] = 'sha256=' + createHmac('sha256', this.config.webhookSecret).update(body).digest('hex');
    }
    try {
      const res = await fetch(job.url, { method: 'POST', headers, body, signal: AbortSignal.timeout(5000) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      this.log('debug', 'webhook ok ' + job.url + ' #' + job.envelope.seq);
    } catch (error) {
      job.attempts += 1;
      if (job.attempts > 3) {
        this.log('warn', 'webhook dropped after retries: ' + job.url + ' (' + error.message + ')');
        return;
      }
      job.nextAt = Date.now() + 500 * Math.pow(2, job.attempts);
      this.hookQueue.push(job);
    }
  }
}

export function normalizeFilter(filter) {
  const f = filter || {};
  const kinds = Array.isArray(f.kinds) ? f.kinds.filter(Boolean) : f.kinds ? String(f.kinds).split(',').filter(Boolean) : [];
  const families = Array.isArray(f.families) ? f.families.filter(Boolean) : f.families ? String(f.families).split(',').filter(Boolean) : [];
  return {
    kinds: kinds.length ? new Set(kinds.map((k) => String(k).trim())) : null,
    families: families.length ? new Set(families.map((k) => String(k).trim())) : null,
    minWeight: Number(f.minWeight || f.min_weight) || 0,
    reactionsOnly: Boolean(f.reactionsOnly || f.reactions_only),
  };
}

export function matches(filter, envelope) {
  if (!filter) return true;
  if (filter.kinds && !filter.kinds.has(envelope.kind)) return false;
  if (filter.families && !filter.families.has(envelope.family)) return false;
  if (filter.minWeight && envelope.interaction.weight < filter.minWeight) return false;
  if (filter.reactionsOnly && !envelope.interaction.needs_reaction) return false;
  return true;
}

export { familyOf };
