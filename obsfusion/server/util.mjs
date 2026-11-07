import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

export const VERSION = '0.1.0';

export function nowIso() {
  return new Date().toISOString();
}

export function clamp(n, min, max) {
  var v = Number(n);
  if (!Number.isFinite(v)) v = min;
  return Math.min(max, Math.max(min, v));
}

// 带默认值的数字校验：缺失/非数字时回退 default，再夹到 [min,max]
export function num(value, fallback, min, max) {
  var v = Number(value);
  if (value === undefined || value === null || value === '' || !Number.isFinite(v)) v = Number(fallback);
  if (!Number.isFinite(v)) v = 0;
  return Math.min(max, Math.max(min, v));
}

export function round(n, digits) {
  var p = Math.pow(10, digits || 2);
  return Math.round(Number(n) * p) / p;
}

export function slug(input, fallback) {
  var s = String(input || '').trim().toLowerCase();
  s = s.replace(/[^a-z0-9\u4e00-\u9fff_-]+/g, '-').replace(/^-+|-+$/g, '');
  if (!s) s = fallback || 'scene';
  return s.slice(0, 64);
}

export function sha1(input) {
  return crypto.createHash('sha1').update(String(input)).digest('hex');
}

export async function ensureDir(dir) {
  await fsp.mkdir(dir, { recursive: true });
}

export async function readJson(file, fallback) {
  try {
    var raw = await fsp.readFile(file, 'utf8');
    return JSON.parse(raw);
  } catch (err) {
    if (err && err.code !== 'ENOENT') console.error('[json]', file, err.message);
    return fallback;
  }
}

export async function writeJsonAtomic(file, data) {
  var dir = path.dirname(file);
  await ensureDir(dir);
  var tmp = file + '.tmp-' + process.pid + '-' + Date.now();
  await fsp.writeFile(tmp, JSON.stringify(data, null, 2) + '\n', 'utf8');
  await fsp.rename(tmp, file);
}

export function safeJoin(base, rel) {
  var target = path.resolve(base, '.' + path.sep + String(rel || '').replace(/^[/\\]+/, ''));
  var root = path.resolve(base);
  if (target !== root && !target.startsWith(root + path.sep)) {
    throw new Error('path escapes root: ' + rel);
  }
  return target;
}

export function isFile(file) {
  try { return fs.statSync(file).isFile(); } catch (_) { return false; }
}

export function mimeFor(file) {
  var ext = path.extname(file).toLowerCase();
  var map = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
    '.woff': 'font/woff',
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.m4a': 'audio/mp4',
    '.txt': 'text/plain; charset=utf-8'
  };
  return map[ext] || 'application/octet-stream';
}

export function readBody(req, limit) {
  limit = limit || 4 * 1024 * 1024;
  return new Promise(function (resolve, reject) {
    var chunks = [];
    var size = 0;
    req.on('data', function (chunk) {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', function () {
      var raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve(null);
      try { resolve(JSON.parse(raw)); } catch (err) { reject(new Error('invalid JSON body')); }
    });
    req.on('error', reject);
  });
}

export function sendJson(res, status, data) {
  var body = JSON.stringify(data);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body)
  });
  res.end(body);
}

export function sendText(res, status, text) {
  res.writeHead(status, { 'content-type': 'text/plain; charset=utf-8' });
  res.end(text);
}

export function deepMerge(base, patch) {
  if (Array.isArray(base) || Array.isArray(patch)) return patch === undefined ? base : patch;
  if (typeof base !== 'object' || base === null) return patch === undefined ? base : patch;
  var out = Object.assign({}, base);
  if (!patch || typeof patch !== 'object') return out;
  Object.keys(patch).forEach(function (key) {
    out[key] = deepMerge(base[key], patch[key]);
  });
  return out;
}

export function timestampSlug() {
  var d = new Date();
  function p(n) { return String(n).padStart(2, '0'); }
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

export function shortId(prefix) {
  return (prefix || 'id') + '-' + crypto.randomBytes(3).toString('hex');
}

export function logger(scope) {
  var tag = '[' + scope + ']';
  return {
    info: function () { console.log.apply(console, [tag].concat(Array.prototype.slice.call(arguments))); },
    warn: function () { console.warn.apply(console, [tag].concat(Array.prototype.slice.call(arguments))); },
    error: function () { console.error.apply(console, [tag].concat(Array.prototype.slice.call(arguments))); }
  };
}
