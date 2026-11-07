// validateProject / compileTimeline 单元测试（纯函数，不落盘、不启服务）
import test from "node:test";
import assert from "node:assert/strict";
import { validateProject, compileTimeline } from "../server/timeline.mjs";

// validateProject 曾经在缺 audio 字段时崩溃（已修复），除少数针对性用例外，
// 统一带上 audio: {} 作为合法输入底座。
function base(extra) {
  return Object.assign({ audio: {} }, extra || {});
}

function project(extra) {
  return validateProject(base(extra), "demo");
}

// ---------------------------------------------------------------- validateProject

test("validateProject: 输出结构完整且有稳定默认值", () => {
  const p = validateProject(base(), "fallback-id");
  assert.equal(p.version, 1);
  assert.equal(p.id, "fallback-id");
  assert.equal(p.title, "Untitled Scene");
  assert.deepEqual(p.cues, []);
  assert.deepEqual(p.extensions, []);
  assert.deepEqual(p.video, { src: "", loop: true, muted: true, fit: "cover", opacity: 1 });
  assert.deepEqual(p.audio, { ttsGain: 1, bgm: null });
  assert.deepEqual(p.sync, { mode: "audio", tailPadding: 0.25 });
  assert.equal(p.theme.accent, "#E8B05A");
  assert.equal(p.theme.subtitleFont, "");
  assert.equal(p.theme.showSpeaker, true);
  assert.deepEqual(p.meta, { createdAt: null, updatedAt: null, description: "" });
});

test("validateProject: 显式 canvas 保留，超范围 clamp 到边界", () => {
  assert.deepEqual(project({ canvas: { width: 1280, height: 720 } }).canvas, { width: 1280, height: 720 });
  assert.deepEqual(project({ canvas: { width: 1, height: 1 } }).canvas, { width: 320, height: 240 });
  assert.deepEqual(project({ canvas: { width: 99999, height: 99999 } }).canvas, { width: 7680, height: 4320 });
  // 字符串数字也要能识别
  assert.deepEqual(project({ canvas: { width: "1920", height: "1080" } }).canvas, { width: 1920, height: 1080 });
});

test("validateProject: 数值字段超范围 clamp", () => {
  const p = project({
    theme: { subtitleSize: 999, accent: "#123456" },
    sync: { tailPadding: 99, mode: "nope" },
    video: { opacity: 5, fit: "nope" },
    audio: { ttsGain: 9, bgm: null },
    cues: [{ id: "c1", start: 0, end: 1, text: "x", tts: { rate: 99, volume: 999 } }]
  });
  assert.equal(p.theme.subtitleSize, 160);
  assert.equal(p.theme.accent, "#123456");
  assert.equal(p.sync.tailPadding, 3);
  assert.equal(p.sync.mode, "audio", "非法 sync.mode 应回退 audio");
  assert.equal(p.video.opacity, 1);
  assert.equal(p.video.fit, "cover", "非法 fit 应回退 cover");
  assert.equal(p.audio.ttsGain, 2);
  assert.equal(p.cues[0].tts.rate, 10);
  assert.equal(p.cues[0].tts.volume, 100);

  const low = project({ theme: { subtitleSize: 1 }, sync: { tailPadding: -5 }, video: { opacity: -3 }, audio: { ttsGain: -1 } });
  assert.equal(low.theme.subtitleSize, 24);
  assert.equal(low.sync.tailPadding, 0);
  assert.equal(low.video.opacity, 0);
  assert.equal(low.audio.ttsGain, 0);
});

test("validateProject: anchor / style 非法值回退，合法值保留", () => {
  const anchors = ["bottom-center", "bottom-left", "center", "top-center", "top-left", "top-right"];
  const styles = ["subtitle", "title", "caption", "quote"];
  const p = project({ cues: [{ id: "c1", start: 0, end: 1, text: "x", anchor: "middle-ish", style: "huge" }] });
  assert.equal(p.cues[0].anchor, "bottom-center");
  assert.equal(p.cues[0].style, "subtitle");
  for (const anchor of anchors) {
    assert.equal(project({ cues: [{ id: "c", start: 0, end: 1, text: "x", anchor: anchor }] }).cues[0].anchor, anchor);
  }
  for (const style of styles) {
    assert.equal(project({ cues: [{ id: "c", start: 0, end: 1, text: "x", style: style }] }).cues[0].style, style);
  }
});

test("validateProject: cue 按 start 升序，缺 / 非法 end 时 end = start + 0.1", () => {
  const p = project({ cues: [
    { id: "late", start: 10, text: "晚" },
    { id: "early", start: 5, end: 2, text: "早" },
    { id: "mid", start: 7, end: 9, text: "中" }
  ] });
  assert.deepEqual(p.cues.map(function (c) { return c.id; }), ["early", "mid", "late"]);
  assert.deepEqual(p.cues.map(function (c) { return c.start; }), [5, 7, 10]);
  assert.deepEqual(p.cues.map(function (c) { return c.end; }), [5.1, 9, 10.1]);
});

test("validateProject: cue id 去重（按 start 排序后加 -2/-3 后缀）", () => {
  const p = project({ cues: [
    { id: "a", start: 5, end: 6, text: "3" },
    { id: "a", start: 1, end: 2, text: "1" },
    { id: "a", start: 3, end: 4, text: "2" }
  ] });
  assert.deepEqual(p.cues.map(function (c) { return c.id; }), ["a", "a-2", "a-3"]);
  assert.deepEqual(p.cues.map(function (c) { return c.text; }), ["1", "2", "3"]);

  // 缺 id 时按序号补，且与已有 id 冲突时也要去重
  const q = project({ cues: [{ start: 0, end: 1, text: "x" }, { id: "c2", start: 1, end: 2, text: "y" }] });
  assert.equal(q.cues[0].id, "c1");
  assert.equal(q.cues[1].id, "c2");
});

test("validateProject: text / speaker / title 截断与换行归一", () => {
  const p = project({
    title: "T".repeat(300),
    cues: [{ id: "c1", start: 0, end: 1, text: "x".repeat(9000), speaker: "s".repeat(200) }]
  });
  assert.equal(p.title.length, 160);
  assert.equal(p.cues[0].text.length, 8000);
  assert.equal(p.cues[0].speaker.length, 80);

  const q = project({ cues: [{ id: "c1", start: 0, end: 1, text: "第一行\r\n第二行" }] });
  assert.equal(q.cues[0].text, "第一行\n第二行");
  assert.equal(project({ cues: [{ id: "c1", start: 0, end: 1 }] }).cues[0].text, "");
});

test("validateProject: id 清洗，空 id 用 fallback，再退回 demo", () => {
  assert.equal(project({ id: "we ird/../id" }).id, "weirdid");
  assert.equal(validateProject(base(), "fb").id, "fb");
  assert.equal(validateProject(base()).id, "demo");
  assert.equal(project({ id: "x".repeat(80) }).id.length, 48);
});

test("validateProject: tts 默认值（voice / enabled / karaoke）", () => {
  const p = project({ cues: [
    { id: "a", start: 0, end: 1, text: "默认" },
    { id: "b", start: 1, end: 2, text: "关掉", karaoke: false, tts: { enabled: false, voice: "X Voice" } }
  ] });
  assert.equal(p.cues[0].tts.voice, "Microsoft Huihui Desktop");
  assert.equal(p.cues[0].tts.enabled, true);
  assert.equal(p.cues[0].karaoke, true);
  assert.equal(p.cues[1].tts.enabled, false);
  assert.equal(p.cues[1].tts.voice, "X Voice");
  assert.equal(p.cues[1].karaoke, false);
  assert.deepEqual(p.cues[0].offset, { x: 0, y: 0 });
});

test("validateProject: extensions 过滤非法项并规范化", () => {
  const p = project({ extensions: [
    { id: "danmaku", config: { lanes: 4 } },
    null,
    { enabled: true },
    { id: "other", enabled: false }
  ] });
  assert.equal(p.extensions.length, 2);
  assert.deepEqual(p.extensions[0], { id: "danmaku", enabled: true, config: { lanes: 4 } });
  assert.deepEqual(p.extensions[1], { id: "other", enabled: false, config: {} });
});

// ---------------------------------------------------------------- compileTimeline

function makeProject(extra) {
  return project(extra);
}

test("compileTimeline: sync.mode=audio 时 displayEnd 取尾音后", () => {
  const p = makeProject({ id: "demo", sync: { mode: "audio", tailPadding: 0.5 }, cues: [
    { id: "c1", start: 1, end: 2, text: "短字幕长语音" },
    { id: "c2", start: 10, end: 12, text: "语音短字幕长" }
  ] });
  const tl = compileTimeline(p, {
    ttsManifest: { cues: {
      c1: { file: "c1.wav", duration: 3.3, voice: "V1" },
      c2: { file: "c2.wav", duration: 0.4, voice: "V2" }
    } },
    revision: 7
  });
  assert.equal(tl.cues[0].displayEnd, 4.8); // max(2, 1 + 3.3 + 0.5)
  assert.equal(tl.cues[1].displayEnd, 12); // max(12, 10 + 0.4 + 0.5)
  assert.equal(tl.cues[0].audio.url, "/api/projects/demo/assets/tts/c1.wav");
  assert.equal(tl.cues[0].audio.duration, 3.3);
  assert.equal(tl.cues[0].audio.voice, "V1");
  assert.equal(tl.revision, 7);
  assert.equal(tl.project.id, "demo");
  assert.equal(typeof tl.now, "number");
});

test("compileTimeline: sync.mode=strict 时不延长", () => {
  const p = makeProject({ sync: { mode: "strict", tailPadding: 0.5 }, cues: [{ id: "c1", start: 1, end: 2, text: "x" }] });
  const tl = compileTimeline(p, { ttsManifest: { cues: { c1: { file: "c1.wav", duration: 9 } } } });
  assert.equal(tl.cues[0].displayEnd, 2);
  assert.equal(tl.cues[0].audio.duration, 9);
});

test("compileTimeline: duration = max(displayEnd) + 2 且不小于 8", () => {
  const long = makeProject({ cues: [
    { id: "c1", start: 0, end: 2, text: "a" },
    { id: "c2", start: 20, end: 21, text: "b" }
  ] });
  assert.equal(compileTimeline(long, {}).duration, 23);
  assert.equal(compileTimeline(makeProject({ cues: [] }), {}).duration, 8);
  assert.equal(compileTimeline(makeProject({ cues: [{ id: "c1", start: 0, end: 3, text: "a" }] }), {}).duration, 8);

  const audio = makeProject({ sync: { mode: "audio", tailPadding: 0.25 }, cues: [{ id: "c1", start: 0, end: 1, text: "a" }] });
  const tl = compileTimeline(audio, { ttsManifest: { cues: { c1: { file: "c1.wav", duration: 10 } } } });
  assert.equal(tl.cues[0].displayEnd, 10.25);
  assert.equal(tl.duration, 12.25);
});

test("compileTimeline: 无 video / 无 bgm 时为 null", () => {
  const tl = compileTimeline(makeProject({ cues: [] }), {});
  assert.equal(tl.video, null);
  assert.equal(tl.bgm, null);
});

test("compileTimeline: video.src 去掉 media/ 前缀，不会出现 /media/media/", () => {
  const tl = compileTimeline(makeProject({ video: { src: "media/demo.mp4" }, cues: [] }), {});
  assert.equal(tl.video.url, "/api/projects/demo/media/demo.mp4");
  assert.ok(tl.video.url.indexOf("/media/media/") === -1, "URL 不应重复 media 段：" + tl.video.url);
  assert.equal(tl.video.loop, true);
  assert.equal(tl.video.muted, true);
  assert.equal(tl.video.fit, "cover");
  assert.equal(tl.video.opacity, 1);

  const nested = compileTimeline(makeProject({ video: { src: "media\\sub/clip x.mp4" }, cues: [] }), {});
  assert.equal(nested.video.url, "/api/projects/demo/media/sub/clip%20x.mp4");
  assert.ok(nested.video.url.indexOf("/media/media/") === -1);

  const bare = compileTimeline(makeProject({ video: { src: "demo.mp4" }, cues: [] }), {});
  assert.equal(bare.video.url, "/api/projects/demo/media/demo.mp4");
});

test("compileTimeline: bgm 有 src 时给出 url 与 gain", () => {
  const p = makeProject({ audio: { ttsGain: 1, bgm: { src: "media/bgm.mp3", gain: 0.4 } }, cues: [] });
  const tl = compileTimeline(p, {});
  assert.deepEqual(tl.bgm, { url: "/api/projects/demo/media/bgm.mp3", gain: 0.4 });

  const empty = compileTimeline(makeProject({ audio: { bgm: { src: "" } }, cues: [] }), {});
  assert.equal(empty.bgm, null);
});

test("compileTimeline: TTS 缺 manifest / 缺该 cue 时 audio 为 null", () => {
  const p = makeProject({ cues: [{ id: "c1", start: 0, end: 2, text: "无语音" }] });
  assert.equal(compileTimeline(p, {}).cues[0].audio, null);
  assert.equal(compileTimeline(p, { ttsManifest: {} }).cues[0].audio, null);
  assert.equal(compileTimeline(p, { ttsManifest: { cues: {} } }).cues[0].audio, null);
  assert.equal(compileTimeline(p, { ttsManifest: { cues: { c1: { file: "" } } } }).cues[0].audio, null);
});

test("compileTimeline: extensions 映射 client / version / config", () => {
  const tl = compileTimeline(makeProject({ cues: [] }), {
    extensions: [{ id: "danmaku", enabled: true, config: { lanes: 6 }, client: "/extensions/danmaku/client.js", version: "1.0.0" }]
  });
  assert.deepEqual(tl.extensions, [{ id: "danmaku", enabled: true, config: { lanes: 6 }, client: "/extensions/danmaku/client.js", version: "1.0.0" }]);
});

test("compileTimeline: 缺省 revision 为 0", () => {
  assert.equal(compileTimeline(makeProject({ cues: [] }), {}).revision, 0);
});

// ---------------------------------------------------------------- 回归测试（这些曾经是 bug，2026-11 已修复并转正）

test("validateProject: 完全空 / 非对象输入应回退默认值而非抛错", () => {
  assert.doesNotThrow(function () { validateProject({}); });
  assert.doesNotThrow(function () { validateProject(null, "fb"); });
  assert.doesNotThrow(function () { validateProject(undefined); });
  assert.doesNotThrow(function () { validateProject("nope"); });
  const p = validateProject({});
  assert.equal(p.title, "Untitled Scene");
  assert.equal(p.audio.ttsGain, 1);
  assert.equal(validateProject(null, "fb").id, "fb");
});

test("validateProject: 缺省 canvas / subtitleSize 应回退契约默认 1920x1080 与 64", () => {
  const p = validateProject({ audio: {} });
  assert.deepEqual(p.canvas, { width: 1920, height: 1080 });
  assert.equal(p.theme.subtitleSize, 64);
});

test("validateProject: cue 缺省 tts.rate / volume 应为 0 / 100", () => {
  const p = validateProject({ audio: {}, cues: [{ id: "c1", start: 0, end: 1, text: "x" }] });
  assert.equal(p.cues[0].tts.rate, 0);
  assert.equal(p.cues[0].tts.volume, 100);
});
