import fsp from 'node:fs/promises';
import path from 'node:path';
import { ensureDir, nowIso, readJson, writeJsonAtomic } from './util.mjs';
import { validateProject, compileTimeline } from './timeline.mjs';

export class ProjectStore {
  constructor(root) {
    this.root = root;
    this.projectsDir = path.join(root, 'projects');
    this.revisions = new Map();
  }

  async init() {
    await ensureDir(this.projectsDir);
  }

  projectDir(id) {
    var safe = String(id || '').replace(/[^a-zA-Z0-9_-]/g, '');
    if (!safe) throw new Error('invalid project id');
    return path.join(this.projectsDir, safe);
  }

  projectFile(id) {
    return path.join(this.projectDir(id), 'project.json');
  }

  revision(id) {
    return this.revisions.get(id) || 1;
  }

  bump(id) {
    var next = this.revision(id) + 1;
    this.revisions.set(id, next);
    return next;
  }

  async list() {
    var entries = [];
    var dirs = [];
    try { dirs = await fsp.readdir(this.projectsDir, { withFileTypes: true }); } catch (_) { return entries; }
    for (var i = 0; i < dirs.length; i++) {
      var dirent = dirs[i];
      if (!dirent.isDirectory()) continue;
      var file = path.join(this.projectsDir, dirent.name, 'project.json');
      var raw = await readJson(file, null);
      if (!raw) continue;
      var project = validateProject(raw, dirent.name);
      var last = 0;
      project.cues.forEach(function (cue) {
        var end = Math.max(cue.end, cue.audio ? cue.start + cue.audio.duration : cue.end);
        last = Math.max(last, end);
      });
      entries.push({
        id: project.id,
        title: project.title,
        cueCount: project.cues.length,
        cues: project.cues.length,
        duration: last,
        updatedAt: project.meta.updatedAt,
        revision: this.revision(project.id)
      });
    }
    entries.sort(function (a, b) { return String(a.title).localeCompare(String(b.title), 'zh-Hans-CN'); });
    return entries;
  }

  async load(id) {
    var file = this.projectFile(id);
    var raw;
    try {
      raw = JSON.parse(await fsp.readFile(file, 'utf8'));
    } catch (err) {
      if (err && err.code === 'ENOENT') {
        var missing = new Error('project not found: ' + id);
        missing.code = 'ENOENT';
        throw missing;
      }
      throw err;
    }
    return validateProject(raw, id);
  }

  async save(id, input) {
    var project = validateProject(input, id);
    project.id = String(id);
    var existing = null;
    try { existing = await this.load(id); } catch (_) {}
    project.meta.createdAt = project.meta.createdAt || (existing && existing.meta.createdAt) || nowIso();
    project.meta.updatedAt = nowIso();
    await ensureDir(this.projectDir(id));
    await writeJsonAtomic(this.projectFile(id), project);
    this.bump(id);
    return project;
  }

  async ttsManifest(id) {
    var file = path.join(this.projectDir(id), 'assets', 'tts', 'manifest.json');
    var manifest = await readJson(file, { version: 1, cues: {} });
    if (!manifest.cues) manifest.cues = {};
    return manifest;
  }

  async compile(id) {
    var project = await this.load(id);
    var manifest = await this.ttsManifest(id);
    var timeline = compileTimeline(project, {
      ttsManifest: manifest,
      revision: this.revision(id),
      extensions: []
    });
    return { project: project, timeline: timeline, manifest: manifest };
  }

  async ensure(id) {
    try { await fsp.access(this.projectFile(id)); return true; } catch (_) { return false; }
  }
}