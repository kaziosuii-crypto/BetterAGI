// WS 协议测试：用 Node 内置全局 WebSocket 连真实服务
import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestServer } from "./_helper.mjs";

let server = null;
let base = "";
let wsUrl = "";

before(async function () {
  server = await startTestServer();
  base = server.base;
  wsUrl = "ws://127.0.0.1:" + server.port + "/ws?scene=demo&role=control";
});

after(async function () {
  if (server) {
    await server.ctx.close();
    server = null;
  }
});

// 每个连接一个收件队列，按谓词匹配（避免依赖消息顺序）
function connect(url) {
  const socket = new WebSocket(url);
  const inbox = [];
  const waiters = [];
  const received = [];

  function deliver(message) {
    received.push(message);
    for (let i = 0; i < waiters.length; i += 1) {
      if (waiters[i].predicate(message)) {
        const waiter = waiters.splice(i, 1)[0];
        clearTimeout(waiter.timer);
        waiter.resolve(message);
        return;
      }
    }
    inbox.push(message);
  }

  socket.addEventListener("message", function (event) {
    let parsed = null;
    try {
      parsed = JSON.parse(String(event.data));
    } catch (err) {
      parsed = { type: "unparseable", raw: String(event.data) };
    }
    deliver(parsed);
  });

  const opened = new Promise(function (resolve, reject) {
    socket.addEventListener("open", function () { resolve(); });
    socket.addEventListener("error", function (event) {
      reject(new Error("WS 连接失败: " + (event && event.message ? event.message : "unknown")));
    });
  });

  function waitForMessage(predicate, timeoutMs) {
    for (let i = 0; i < inbox.length; i += 1) {
      if (predicate(inbox[i])) {
        return Promise.resolve(inbox.splice(i, 1)[0]);
      }
    }
    return new Promise(function (resolve, reject) {
      const waiter = { predicate: predicate, resolve: resolve, timer: null };
      waiter.timer = setTimeout(function () {
        const index = waiters.indexOf(waiter);
        if (index >= 0) waiters.splice(index, 1);
        reject(new Error("等待 WS 消息超时（" + (timeoutMs || 5000) + "ms），已收到: " + JSON.stringify(received.map(function (m) { return m.type; }))));
      }, timeoutMs || 5000);
      waiters.push(waiter);
    });
  }

  function send(payload) {
    socket.send(typeof payload === "string" ? payload : JSON.stringify(payload));
  }

  return {
    socket: socket,
    opened: opened,
    send: send,
    waitForMessage: waitForMessage,
    received: received,
    close: function () { try { socket.close(); } catch (err) { /* 忽略 */ } }
  };
}

test("WS: 连接后收到 hello，timeline 含 demo 的 6 条 cue", { timeout: 10000 }, async function () {
  const client = connect(wsUrl);
  try {
    await client.opened;
    const hello = await client.waitForMessage(function (m) { return m.type === "hello"; });
    assert.equal(hello.role, "control");
    assert.equal(hello.sceneId, "demo");
    assert.ok(hello.timeline, "hello 应带 timeline");
    assert.equal(hello.timeline.cues.length, 6);
    assert.ok(hello.timeline.duration > 0);
    assert.equal(hello.timeline.project.id, "demo");
    assert.ok(hello.state, "hello 应带 state");
    assert.equal(typeof hello.now, "number");
  } finally {
    client.close();
  }
});

test("WS: 发 transport seek 5 后收到 state.time 约 5", { timeout: 10000 }, async function () {
  const client = connect(wsUrl);
  try {
    await client.opened;
    await client.waitForMessage(function (m) { return m.type === "hello"; });
    client.send({ type: "transport", action: "seek", time: 5 });
    const state = await client.waitForMessage(function (m) {
      return m.type === "state" && m.state && Math.abs(m.state.time - 5) < 0.6;
    });
    assert.equal(state.state.status, "paused");
    assert.equal(state.sceneId, "demo");
  } finally {
    client.close();
  }
});

test("WS: ping 收到 pong", { timeout: 10000 }, async function () {
  const client = connect(wsUrl);
  try {
    await client.opened;
    await client.waitForMessage(function (m) { return m.type === "hello"; });
    client.send({ type: "ping" });
    const pong = await client.waitForMessage(function (m) { return m.type === "pong"; });
    assert.equal(typeof pong.now, "number");
  } finally {
    client.close();
  }
});
