import crypto from 'node:crypto';

var GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
var MAX_FRAME = 8 * 1024 * 1024;

function encodeFrame(opcode, payload) {
  var data = Buffer.isBuffer(payload) ? payload : Buffer.from(String(payload || ''), 'utf8');
  var length = data.length;
  var header;
  if (length < 126) {
    header = Buffer.alloc(2);
    header[1] = length;
  } else if (length < 65536) {
    header = Buffer.alloc(4);
    header[1] = 126;
    header.writeUInt16BE(length, 2);
  } else {
    header = Buffer.alloc(10);
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(length), 2);
  }
  header[0] = 0x80 | opcode;
  return Buffer.concat([header, data]);
}

export class WSConnection {
  constructor(socket, req) {
    this.socket = socket;
    this.req = req;
    this.url = new URL(req.url, 'http://127.0.0.1');
    this.alive = true;
    this.buffer = Buffer.alloc(0);
    this.fragments = [];
    this.fragmentOpcode = 0;
    this.onMessage = null;
    this.onClose = null;
    this.meta = {};
    var self = this;
    socket.on('data', function (chunk) { self._feed(chunk); });
    socket.on('close', function () { self._finish(); });
    socket.on('error', function () { self._finish(); });
    socket.setNoDelay(true);
  }

  _finish() {
    if (!this.alive) return;
    this.alive = false;
    if (typeof this.onClose === 'function') {
      try { this.onClose(); } catch (err) { console.error('[ws:close]', err); }
    }
  }

  sendJson(obj) {
    if (!this.alive) return false;
    try {
      this.socket.write(encodeFrame(0x1, JSON.stringify(obj)));
      return true;
    } catch (err) {
      this._finish();
      return false;
    }
  }

  sendText(text) {
    if (!this.alive) return false;
    try {
      this.socket.write(encodeFrame(0x1, text));
      return true;
    } catch (err) {
      this._finish();
      return false;
    }
  }

  close(code, reason) {
    if (!this.alive) return;
    var payload = Buffer.alloc(2 + Buffer.byteLength(reason || ''));
    payload.writeUInt16BE(code || 1000, 0);
    payload.write(reason || '', 2);
    try { this.socket.write(encodeFrame(0x8, payload)); } catch (_) {}
    this.socket.end();
    this._finish();
  }

  _feed(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length >= 2) {
      var b0 = this.buffer[0];
      var b1 = this.buffer[1];
      var fin = (b0 & 0x80) !== 0;
      var opcode = b0 & 0x0f;
      var masked = (b1 & 0x80) !== 0;
      var length = b1 & 0x7f;
      var offset = 2;
      if (length === 126) {
        if (this.buffer.length < 4) return;
        length = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (this.buffer.length < 10) return;
        var big = this.buffer.readBigUInt64BE(2);
        if (big > BigInt(MAX_FRAME)) { this.close(1009, 'too large'); return; }
        length = Number(big);
        offset = 10;
      }
      if (length > MAX_FRAME) { this.close(1009, 'too large'); return; }
      var maskKey = null;
      if (masked) {
        if (this.buffer.length < offset + 4) return;
        maskKey = this.buffer.subarray(offset, offset + 4);
        offset += 4;
      }
      if (this.buffer.length < offset + length) return;
      var payload = Buffer.from(this.buffer.subarray(offset, offset + length));
      if (maskKey) {
        for (var i = 0; i < payload.length; i++) payload[i] ^= maskKey[i % 4];
      }
      this.buffer = this.buffer.subarray(offset + length);

      if (opcode === 0x8) { this.close(1000, ''); return; }
      if (opcode === 0x9) { this.socket.write(encodeFrame(0xA, payload)); continue; }
      if (opcode === 0xA) { continue; }
      if (opcode === 0x1 || opcode === 0x2) {
        if (fin) {
          this._deliver(opcode, payload);
        } else {
          this.fragmentOpcode = opcode;
          this.fragments = [payload];
        }
        continue;
      }
      if (opcode === 0x0) {
        this.fragments.push(payload);
        if (fin) {
          var merged = Buffer.concat(this.fragments);
          var op = this.fragmentOpcode || 0x1;
          this.fragments = [];
          this.fragmentOpcode = 0;
          this._deliver(op, merged);
        }
      }
    }
  }

  _deliver(opcode, payload) {
    if (opcode === 0x2) return;
    var text = payload.toString('utf8');
    if (typeof this.onMessage !== 'function') return;
    try { this.onMessage(text); } catch (err) { console.error('[ws:message]', err); }
  }
}

export function attachWebSocket(httpServer, onConnection) {
  httpServer.on('upgrade', function (req, socket) {
    var key = req.headers['sec-websocket-key'];
    if (!key || String(req.headers.upgrade || '').toLowerCase() !== 'websocket') {
      socket.write('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');
      socket.destroy();
      return;
    }
    var accept = crypto.createHash('sha1').update(key + GUID).digest('base64');
    socket.write([
      'HTTP/1.1 101 Switching Protocols',
      'Upgrade: websocket',
      'Connection: Upgrade',
      'Sec-WebSocket-Accept: ' + accept,
      '\r\n'
    ].join('\r\n'));
    var conn = new WSConnection(socket, req);
    onConnection(conn);
  });
}
