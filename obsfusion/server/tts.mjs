import { spawn } from 'node:child_process';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { ensureDir, nowIso, readJson, sha1, writeJsonAtomic } from './util.mjs';

var PS = 'powershell.exe';
var SCRIPT = path.join(import.meta.dirname, 'tts-sapi.ps1');
var VOICES_SCRIPT = path.join(import.meta.dirname, 'voices.ps1');
var voiceCache = { at: 0, voices: null };

function runPowerShell(script, args, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var child = spawn(PS, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script].concat(args), {
      windowsHide: true
    });
    var stdout = '';
    var stderr = '';
    var timer = setTimeout(function () {
      try { child.kill(); } catch (_) {}
      reject(new Error('TTS timeout after ' + timeoutMs + 'ms'));
    }, timeoutMs || 60000);
    child.stdout.on('data', function (d) { stdout += d.toString(); });
    child.stderr.on('data', function (d) { stderr += d.toString(); });
    child.on('error', function (err) { clearTimeout(timer); reject(err); });
    child.on('close', function (code) {
      clearTimeout(timer);
      if (code === 0) resolve({ stdout: stdout, stderr: stderr });
      else reject(new Error('powershell exited ' + code + ': ' + (stderr || stdout).slice(0, 800)));
    });
  });
}

export async function listVoices(force) {
  if (!force && voiceCache.voices && (Date.now() - voiceCache.at) < 60000) return voiceCache.voices;
  try {
    var out = await runPowerShell(VOICES_SCRIPT, [], 20000);
    var raw = String(out.stdout || '').trim();
    var parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) parsed = [parsed];
    voiceCache = { at: Date.now(), voices: parsed };
    return parsed;
  } catch (err) {
    console.warn('[tts] listVoices failed:', err.message);
    return [
      { name: 'Microsoft Huihui Desktop', culture: 'zh-CN', gender: 'Female', enabled: true },
      { name: 'Microsoft Zira Desktop', culture: 'en-US', gender: 'Female', enabled: true }
    ];
  }
}

export async function probeDuration(file) {
  try {
    var out = await new Promise(function (resolve, reject) {
      var child = spawn('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file], { windowsHide: true });
      var text = '';
      var errText = '';
      var timer = setTimeout(function () { try { child.kill(); } catch (_) {} reject(new Error('ffprobe timeout')); }, 15000);
      child.stdout.on('data', function (d) { text += d.toString(); });
      child.stderr.on('data', function (d) { errText += d.toString(); });
      child.on('error', function (err) { clearTimeout(timer); reject(err); });
      child.on('close', function (code) {
        clearTimeout(timer);
        if (code === 0) resolve(text.trim());
        else reject(new Error('ffprobe: ' + errText.slice(0, 300)));
      });
    });
    var value = parseFloat(out);
    if (Number.isFinite(value) && value > 0) return value;
  } catch (_) {}
  return probeWavFallback(file);
}

function probeWavFallback(file) {
  try {
    var buf = fs.readFileSync(file);
    if (buf.toString('ascii', 0, 4) !== 'RIFF') return 0;
    var pos = 12;
    var byteRate = 0;
    while (pos + 8 <= buf.length) {
      var id = buf.toString('ascii', pos, pos + 4);
      var size = buf.readUInt32LE(pos + 4);
      if (id === 'fmt ') {
        byteRate = buf.readUInt32LE(pos + 8 + 8);
      }
      if (id === 'data' && byteRate > 0) return size / byteRate;
      pos += 8 + size + (size % 2);
    }
  } catch (_) {}
  return 0;
}

function ttsHash(text, voice, rate, volume) {
  return sha1([text, voice, rate, volume].join('|'));
}

export async function synthesizeCue(projectDir, cue, opts) {
  opts = opts || {};
  var ttsDir = path.join(projectDir, 'assets', 'tts');
  await ensureDir(ttsDir);
  var outFile = path.join(ttsDir, cue.id + '.wav');
  var textFile = path.join(ttsDir, '.cue-' + cue.id + '-' + process.pid + '.txt');
  var text = String(cue.text || '').trim();
  if (!text) throw new Error('cue has no text: ' + cue.id);
  await fsp.writeFile(textFile, text, 'utf8');
  try {
    await runPowerShell(SCRIPT, [
      '-TextFile', textFile,
      '-OutFile', outFile,
      '-Voice', cue.tts.voice,
      '-Rate', String(cue.tts.rate),
      '-Volume', String(cue.tts.volume)
    ], opts.timeoutMs || 90000);
  } finally {
    try { await fsp.unlink(textFile); } catch (_) {}
  }
  var stat = await fsp.stat(outFile);
  if (!stat.size) throw new Error('TTS produced empty file: ' + outFile);
  var duration = await probeDuration(outFile);
  return {
    file: path.basename(outFile),
    duration: Math.round(duration * 1000) / 1000,
    hash: ttsHash(text, cue.tts.voice, cue.tts.rate, cue.tts.volume),
    voice: cue.tts.voice,
    at: nowIso(),
    bytes: stat.size
  };
}

export async function generateProjectTTS(store, projectId, opts) {
  opts = opts || {};
  var project = await store.load(projectId);
  var projectDir = store.projectDir(projectId);
  var manifestFile = path.join(projectDir, 'assets', 'tts', 'manifest.json');
  var manifest = await readJson(manifestFile, { version: 1, cues: {} });
  if (!manifest.cues) manifest.cues = {};
  var only = opts.cueIds && opts.cueIds.length ? new Set(opts.cueIds) : null;
  var generated = [];
  var skipped = [];
  var errors = [];
  for (var i = 0; i < project.cues.length; i++) {
    var cue = project.cues[i];
    if (only && !only.has(cue.id)) continue;
    if (!cue.tts.enabled || !String(cue.text || '').trim()) { skipped.push({ id: cue.id, reason: 'disabled' }); continue; }
    var hash = ttsHash(cue.text, cue.tts.voice, cue.tts.rate, cue.tts.volume);
    var prev = manifest.cues[cue.id];
    var file = path.join(projectDir, 'assets', 'tts', cue.id + '.wav');
    if (!opts.force && prev && prev.hash === hash && fs.existsSync(file)) {
      skipped.push({ id: cue.id, reason: 'up-to-date' });
      continue;
    }
    try {
      if (typeof opts.onProgress === 'function') opts.onProgress({ id: cue.id, index: i, total: project.cues.length, phase: 'start' });
      var entry = await synthesizeCue(projectDir, cue, opts);
      manifest.cues[cue.id] = entry;
      manifest.updatedAt = nowIso();
      await writeJsonAtomic(manifestFile, manifest);
      generated.push({ id: cue.id, duration: entry.duration, bytes: entry.bytes });
      if (typeof opts.onProgress === 'function') opts.onProgress({ id: cue.id, index: i, total: project.cues.length, phase: 'done', duration: entry.duration });
    } catch (err) {
      errors.push({ id: cue.id, error: err.message });
      if (typeof opts.onProgress === 'function') opts.onProgress({ id: cue.id, index: i, total: project.cues.length, phase: 'error', error: err.message });
    }
  }
  return { manifest: manifest, generated: generated, skipped: skipped, errors: errors };
}
