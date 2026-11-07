// ProjectStore 读写测试：所有写入都使用唯一 id，结束时删除，不碰 projects/demo
import test from "node:test";
import assert from "node:assert/strict";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ProjectStore } from "../server/store.mjs";
import { uniqueId } from "./_helper.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("ProjectStore: init 创建 projects 目录，list() 含 demo", async () => {
  const store = new ProjectStore(ROOT);
  await store.init();
  const stat = await fsp.stat(store.projectsDir);
  assert.ok(stat.isDirectory(), "projects 目录应存在");

  const list = await store.list();
  const demo = list.find(function (entry) { return entry.id === "demo"; });
  assert.ok(demo, "list() 应包含 demo");
  assert.equal(demo.cueCount, 6);
  assert.ok(demo.duration > 0);
  assert.equal(typeof demo.title, "string");
  assert.equal(demo.revision, 1);
  assert.ok(demo.updatedAt, "demo 应有 meta.updatedAt");
});

test("ProjectStore: save / load 往返（中文 + 换行），revision 递增，落盘是合法 JSON", async () => {
  const store = new ProjectStore(ROOT);
  await store.init();
  const id = uniqueId("t-store");
  try {
    assert.equal(store.revision(id), 1, "未保存前 revision 应为 1");

    const input = {
      id: id,
      title: "中文标题 · 往返测试",
      audio: {},
      cues: [
        { id: "c1", start: 0.5, end: 2.5, text: "第一句：中文标点，破折号——以及省略号…", speaker: "解说" },
        { id: "c2", start: 3, end: 4, text: "第二句 line1\nline2" }
      ]
    };

    const saved = await store.save(id, input);
    assert.equal(saved.id, id);
    assert.equal(saved.title, "中文标题 · 往返测试");
    assert.ok(saved.meta.createdAt, "save 后应有 createdAt");
    assert.ok(saved.meta.updatedAt, "save 后应有 updatedAt");
    assert.equal(store.revision(id), 2, "save 后 revision 应递增到 2");

    const loaded = await store.load(id);
    assert.equal(loaded.id, id);
    assert.equal(loaded.title, "中文标题 · 往返测试");
    assert.equal(loaded.cues.length, 2);
    assert.equal(loaded.cues[0].text, "第一句：中文标点，破折号——以及省略号…");
    assert.equal(loaded.cues[0].speaker, "解说");
    assert.equal(loaded.cues[1].text, "第二句 line1\nline2");
    assert.deepEqual(loaded.cues.map(function (c) { return c.start; }), [0.5, 3]);

    // 落盘文件必须是合法 JSON（能被 JSON.parse 且字段完整）
    const raw = await fsp.readFile(store.projectFile(id), "utf8");
    const parsed = JSON.parse(raw);
    assert.equal(parsed.id, id);
    assert.equal(parsed.version, 1);
    assert.equal(parsed.cues.length, 2);
    assert.ok(raw.endsWith("\n"), "写盘文件应以换行结尾");
    assert.ok(raw.indexOf("中文标题") >= 0, "中文应原样落盘（非 \\u 转义）");

    // 第二次 save：revision 继续递增，createdAt 保持
    const saved2 = await store.save(id, Object.assign({}, input, { title: "改过的标题" }));
    assert.equal(store.revision(id), 3);
    assert.equal(saved2.meta.createdAt, saved.meta.createdAt, "createdAt 不应被覆盖");
    assert.equal(saved2.title, "改过的标题");

    // list() 能看到
    const list = await store.list();
    const entry = list.find(function (item) { return item.id === id; });
    assert.ok(entry, "list() 应包含新建项目");
    assert.equal(entry.cueCount, 2);
    assert.equal(entry.title, "改过的标题");
    assert.equal(entry.revision, 3);

    // compile() 走同一条校验链
    const compiled = await store.compile(id);
    assert.equal(compiled.project.id, id);
    assert.equal(compiled.timeline.cues.length, 2);
    assert.equal(compiled.timeline.duration, 8);
    assert.deepEqual(compiled.manifest, { version: 1, cues: {} });
  } finally {
    await fsp.rm(store.projectDir(id), { recursive: true, force: true });
  }
});

test("ProjectStore: 不存在的项目 load 抛 ENOENT，ensure 为 false，ttsManifest 给默认", async () => {
  const store = new ProjectStore(ROOT);
  await store.init();
  const id = uniqueId("t-missing");
  await assert.rejects(function () { return store.load(id); }, function (err) {
    assert.equal(err.code, "ENOENT");
    assert.match(err.message, /project not found/);
    return true;
  });
  assert.equal(await store.ensure(id), false);
  assert.deepEqual(await store.ttsManifest(id), { version: 1, cues: {} });
});

test("ProjectStore: 读取 demo 的 TTS manifest 含 6 条且不写盘", async () => {
  const store = new ProjectStore(ROOT);
  await store.init();
  const manifest = await store.ttsManifest("demo");
  assert.equal(manifest.version, 1);
  const ids = Object.keys(manifest.cues);
  assert.equal(ids.length, 6);
  for (const cueId of ids) {
    assert.equal(typeof manifest.cues[cueId].file, "string");
    assert.ok(manifest.cues[cueId].duration > 0);
  }
  // 只读校验收紧：demo 目录不应被测试改动
  const project = await store.load("demo");
  assert.equal(project.title, "ObsFusion · 文字与画面缝合演示");
  assert.equal(project.cues.length, 6);
});
