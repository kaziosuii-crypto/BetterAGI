#!/usr/bin/env node
/**
 * Reference consumer: subscribes to the relay over WebSocket, prints every
 * event, and acknowledges the ones that ask for a host reaction.
 *
 *   node scripts/demo-client.mjs --url ws://127.0.0.1:8787/ws/stream --seconds 15
 */
import WebSocket from 'ws';

const args = process.argv.slice(2);
function arg(name, fallback) {
  const idx = args.indexOf('--' + name);
  return idx === -1 ? fallback : args[idx + 1];
}

const url = arg('url', 'ws://127.0.0.1:8787/ws/stream?min_weight=0');
const seconds = Number(arg('seconds', 12));
const quiet = args.includes('--quiet');

const started = Date.now();
const counts = {};
let total = 0;
let reactions = 0;

const ws = new WebSocket(url);

ws.on('open', () => {
  console.log('[demo] connected ' + url);
});

ws.on('message', (buffer) => {
  const msg = JSON.parse(buffer.toString());
  if (msg.t === 'welcome') {
    console.log('[demo] welcome subscriber=' + msg.subscriber_id + ' filter=' + JSON.stringify(msg.filter));
    return;
  }
  if (msg.t === 'ack-ok') return;
  if (msg.t !== 'event') return;
  const event = msg.data;
  total += 1;
  counts[event.kind] = (counts[event.kind] || 0) + 1;

  if (!quiet) {
    const money = event.amount && event.amount.cny ? ' ¥' + event.amount.cny : '';
    const flag = event.interaction.needs_reaction ? '★' : ' ';
    console.log(
      '[demo] ' + flag + ' #' + event.seq + ' ' + event.kind.padEnd(16) +
      ' ' + (event.actor.name || '-').padEnd(12) +
      ' ' + String(event.text || '').slice(0, 42) + money,
    );
  }

  if (event.interaction.needs_reaction) {
    reactions += 1;
    setTimeout(() => {
      ws.send(JSON.stringify({ t: 'ack', event_id: event.id, action: 'demo_ack', note: 'seen by demo client' }));
    }, 150);
  }
});

ws.on('close', () => console.log('[demo] closed'));
ws.on('error', (error) => console.error('[demo] error ' + error.message));

setTimeout(() => {
  const elapsed = Math.round((Date.now() - started) / 1000);
  console.log('[demo] done in ' + elapsed + 's: ' + total + ' events, ' + reactions + ' reaction-worthy');
  console.log('[demo] by kind: ' + JSON.stringify(counts));
  ws.close();
  process.exit(0);
}, seconds * 1000);
