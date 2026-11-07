import { clamp, num, round } from './util.mjs';

var ANCHORS = ['bottom-center', 'bottom-left', 'center', 'top-center', 'top-left', 'top-right'];
var STYLES = ['subtitle', 'title', 'caption', 'quote'];

function cleanText(value) {
  return String(value === undefined || value === null ? '' : value).replace(/\r\n/g, '\n').slice(0, 8000);
}

function cleanCue(raw, index) {
  raw = raw || {};
  var start = round(num(raw.start, 0, 0, 86400), 3);
  // 契约：end 缺失或非法（非正数）时回退为 start + 0.1；合法 end 夹在 [start+0.1, 86400]
  var rawEnd = Number(raw.end);
  var endBase = Number.isFinite(rawEnd) && rawEnd > 0 ? rawEnd : start + 0.1;
  var end = round(Math.min(86400, Math.max(start + 0.1, endBase)), 3);
  var anchor = ANCHORS.indexOf(raw.anchor) >= 0 ? raw.anchor : 'bottom-center';
  var style = STYLES.indexOf(raw.style) >= 0 ? raw.style : 'subtitle';
  var tts = raw.tts || {};
  return {
    id: String(raw.id || ('c' + (index + 1))).replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 64) || ('c' + (index + 1)),
    start: start,
    end: end,
    text: cleanText(raw.text),
    speaker: cleanText(raw.speaker).slice(0, 80),
    anchor: anchor,
    style: style,
    karaoke: raw.karaoke !== false,
    offset: { x: clamp(raw.offset && raw.offset.x || 0, -1, 1), y: clamp(raw.offset && raw.offset.y || 0, -1, 1) },
    tts: {
      enabled: tts.enabled !== false,
      voice: String(tts.voice || 'Microsoft Huihui Desktop').slice(0, 120),
      rate: Math.round(num(tts.rate, 0, -10, 10)),
      volume: Math.round(num(tts.volume, 100, 0, 100))
    }
  };
}

export function validateProject(input, fallbackId) {
  var project = input && typeof input === 'object' ? input : {};
  var id = String(project.id || fallbackId || 'demo').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 48) || 'demo';
  var cues = Array.isArray(project.cues) ? project.cues.map(cleanCue) : [];
  cues.sort(function (a, b) { return a.start - b.start; });
  var seen = Object.create(null);
  cues.forEach(function (cue) {
    var base = cue.id;
    var n = 2;
    while (seen[cue.id]) { cue.id = base + '-' + n; n += 1; }
    seen[cue.id] = true;
  });
  var video = project.video || {};
  var theme = project.theme || {};
  var sync = project.sync || {};
  var extensions = Array.isArray(project.extensions) ? project.extensions.filter(function (e) { return e && e.id; }).map(function (e) {
    return { id: String(e.id), enabled: e.enabled !== false, config: e.config && typeof e.config === 'object' ? e.config : {} };
  }) : [];
  return {
    version: 1,
    id: id,
    title: String(project.title || 'Untitled Scene').slice(0, 160),
    canvas: {
      width: Math.round(num(project.canvas && project.canvas.width, 1920, 320, 7680)),
      height: Math.round(num(project.canvas && project.canvas.height, 1080, 240, 4320))
    },
    video: {
      src: typeof video.src === 'string' ? video.src : '',
      loop: video.loop !== false,
      muted: video.muted !== false,
      fit: ['cover', 'contain', 'fill'].indexOf(video.fit) >= 0 ? video.fit : 'cover',
      opacity: num(video.opacity, 1, 0, 1)
    },
    audio: {
      ttsGain: num(project.audio && project.audio.ttsGain, 1, 0, 2),
      bgm: project.audio && project.audio.bgm ? {
        src: String(project.audio.bgm.src || ''),
        gain: num(project.audio.bgm.gain, 0.18, 0, 1)
      } : null
    },
    theme: {
      accent: String(theme.accent || '#E8B05A'),
      subtitleSize: Math.round(num(theme.subtitleSize, 64, 24, 160)),
      subtitleFont: String(theme.subtitleFont || ''),
      showSpeaker: theme.showSpeaker !== false
    },
    sync: {
      mode: sync.mode === 'strict' ? 'strict' : 'audio',
      tailPadding: num(sync.tailPadding, 0.25, 0, 3)
    },
    cues: cues,
    extensions: extensions,
    meta: {
      createdAt: project.meta && project.meta.createdAt || null,
      updatedAt: project.meta && project.meta.updatedAt || null,
      description: String(project.meta && project.meta.description || '').slice(0, 500)
    }
  };
}

export function compileTimeline(project, opts) {
  opts = opts || {};
  var tts = opts.ttsManifest && opts.ttsManifest.cues ? opts.ttsManifest.cues : {};
  var cues = project.cues.map(function (cue) {
    var entry = tts[cue.id] || null;
    var audio = entry && entry.file ? {
      url: '/api/projects/' + encodeURIComponent(project.id) + '/assets/tts/' + encodeURIComponent(entry.file),
      duration: Number(entry.duration) || 0,
      voice: entry.voice || cue.tts.voice
    } : null;
    var displayEnd = cue.end;
    if (project.sync.mode === 'audio' && audio && audio.duration > 0) {
      displayEnd = Math.max(cue.end, cue.start + audio.duration + project.sync.tailPadding);
    }
    return {
      id: cue.id, start: cue.start, end: cue.end, displayEnd: round(displayEnd, 3),
      text: cue.text, speaker: cue.speaker, anchor: cue.anchor, style: cue.style,
      karaoke: cue.karaoke, offset: cue.offset,
      audio: audio
    };
  });
  var end = 0;
  cues.forEach(function (cue) { end = Math.max(end, cue.displayEnd); });
  var duration = Math.max(round(end + 2, 2), 8);
  return {
    project: project,
    revision: opts.revision || 0,
    duration: duration,
    cues: cues,
    video: project.video.src ? {
      url: '/api/projects/' + encodeURIComponent(project.id) + '/media/' + project.video.src.replace(/^media[\/\\]+/i, '').split('/').map(encodeURIComponent).join('/'),
      loop: project.video.loop,
      muted: project.video.muted,
      fit: project.video.fit,
      opacity: project.video.opacity
    } : null,
    bgm: project.audio.bgm && project.audio.bgm.src ? {
      url: '/api/projects/' + encodeURIComponent(project.id) + '/media/' + project.audio.bgm.src.replace(/^media[\/\\]+/i, '').split('/').map(encodeURIComponent).join('/'),
      gain: project.audio.bgm.gain
    } : null,
    extensions: (opts.extensions || []).map(function (ext) { return { id: ext.id, enabled: ext.enabled, config: ext.config, client: ext.client, version: ext.version }; }),
    now: Date.now()
  };
}