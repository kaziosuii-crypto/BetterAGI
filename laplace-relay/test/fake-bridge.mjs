import http from 'node:http';
import { WebSocketServer } from 'ws';

/**
 * Minimal stand-in for the Go leb-server: accepts ws connections, greets them
 * with an "established" frame, and broadcasts whatever the test asks it to.
 */
export async function createFakeBridge() {
  const server = http.createServer((req, res) => res.end('fake bridge'));
  const wss = new WebSocketServer({ server });
  const sockets = new Set();
  wss.on('connection', (ws) => {
    sockets.add(ws);
    ws.on('close', () => sockets.delete(ws));
    ws.on('message', (buf) => {
      let msg;
      try { msg = JSON.parse(buf.toString()); } catch (_) { return; }
      if (msg.type === 'pong') return;
      broadcast({ ...msg, source: 'server-1' });
    });
    ws.send(JSON.stringify({ type: 'established', clientId: 'client-1', isServer: false, message: 'fake bridge' }));
  });
  function broadcast(payload) {
    const text = JSON.stringify(payload);
    for (const ws of sockets) {
      if (ws.readyState === 1) ws.send(text);
    }
  }
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  return {
    url: 'ws://127.0.0.1:' + port,
    port,
    broadcast,
    close: () => new Promise((resolve) => {
      for (const ws of sockets) ws.terminate();
      wss.close();
      server.close(resolve);
    }),
  };
}
