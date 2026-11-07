# laplace-relay

本地 **LAPLACE Chat 事件中继服务**：把 B 站直播里的弹幕、礼物、醒目留言、上舰、天选/红包、进场/关注、房间数据、直播状态、房管操作等
**全部 33 类事件**接收下来，统一成一套结构化 envelope，再通过 **WebSocket / SSE / Webhook / HTTP 拉取** 实时推送给其它服务。

> 目标：让「Amaidesu 之类的 AI 主播/场控/字幕/TTS/弹幕玩法」只需要订阅一个本地端口，就能拿到干净、带语义的结构化事件。

---

## 1. 架构

\`\`\`
LAPLACE Chat (浏览器面板, https://laplace.live/chat)
        │  以 server 角色连接
        ▼
LAPLACE Event Bridge (leb-server, 默认 ws://127.0.0.1:9696)
        │  以 client 角色连接 (role-client, 可带 token)
        ▼
┌────────────────── laplace-relay ──────────────────┐
│ bridge/client.js  ── 原始事件(camelCase)          │
│        │                                          │
│ events/normalize.js ── 统一 envelope + 语义字段    │
│        │                                          │
│ core/hub.js  ── seq / 统计 / 环缓冲 / JSONL 落盘   │
│        │            ── ack 回执 / webhook 重试     │
│        ▼                                          │
│ WS /ws/stream   SSE /events   Webhook   HTTP /api  │
└───────────────────────────────────────────────────┘
        │
        ▼
其它服务（AI 主播、TTS、OBS 叠加层、场控面板…）
\`\`\`

### 数据流要点

| 环节 | 说明 |
| --- | --- |
| 接入 | 一个服务同时支持 **拉**（连 leb-server）和 **推**（别人把事件打进 \`/bridge\`），也支持 **mock** 自测 |
| 归一化 | 33 类事件 → 8 个语义族（chat/money/audience/room/lifecycle/moderation/system/unknown），带中文文案、金额、演员信息 |
| 语义增强 | 每条事件算出 \`interaction.weight\`（0-100）与 \`needs_reaction\`，主播端可直接「按优先级处理」 |
| 分发 | 支持过滤（kind/family/最小权重/只看需互动）的 WS、SSE、Webhook 三通道 |
| 可追溯 | 单调递增 \`seq\`、环形缓冲回放、按天 JSONL 落盘 |
| 回执 | 下游可用 \`ack\` 回报「已回应」，服务端统计待办与延迟 |

---

## 2. 快速开始

\`\`\`bash
cd E:\BetterAGI\laplace-relay
npm install            # 仅一个依赖：ws

# A. 没有直播间/没开 LAPLACE 时，用 mock 事件源看完整链路
npm run mock           # http://127.0.0.1:8787/panel

# B. 真实接入
#   1) 启动 leb-server（见官方 packages/server，或直接用 laplace.live/chat/event-bridge）
#   2) 浏览器打开 https://laplace.live/chat ，进到 /dashboard/<房间号> 并保持常开
#   3) 启动中继
node src/index.js --source bridge --bridge-url ws://127.0.0.1:9696 --room 123456
\`\`\`

打开 **http://127.0.0.1:8787/panel** 可以看到实时事件流、事件分布、待互动队列，并可手工 ack。

---

## 3. 命令行 / 环境变量

所有参数都有 \`--flag\` 和环境变量两种写法（\`LAPLACE_RELAY_<UPPER_SNAKE>\`）。

| 参数 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| \`--host\` | \`LAPLACE_RELAY_HOST\` | \`127.0.0.1\` | 监听地址 |
| \`--port\` | \`LAPLACE_RELAY_PORT\` | \`8787\` | 监听端口 |
| \`--source\` | \`LAPLACE_RELAY_SOURCE\` | \`bridge\` | \`bridge\` / \`mock\` / \`both\` |
| \`--bridge-url\` | \`LAPLACE_RELAY_BRIDGE_URL\` | \`ws://127.0.0.1:9696\` | leb-server 地址 |
| \`--bridge-token\` | \`LAPLACE_RELAY_BRIDGE_TOKEN\` | 空 | leb-server \`--auth\` 的 token |
| \`--room\` | \`LAPLACE_RELAY_ROOM\` | \`0\` | 只保留该房间事件（0=不过滤） |
| \`--include-raw\` | \`LAPLACE_RELAY_INCLUDE_RAW\` | 关 | envelope 附带原始 \`raw\` 对象 |
| \`--ring-size\` | \`LAPLACE_RELAY_RING_SIZE\` | \`500\` | 环形缓冲条数 |
| \`--data-dir\` | \`LAPLACE_RELAY_DATA_DIR\` | \`data\` | JSONL 落盘目录（空字符串=不落盘） |
| \`--webhook\` | \`LAPLACE_RELAY_WEBHOOK\` | 空 | 可重复，多个 webhook URL |
| \`--webhook-secret\` | \`LAPLACE_RELAY_WEBHOOK_SECRET\` | 空 | 配置后附带 \`X-Relay-Signature: sha256=...\` |
| \`--webhook-mode\` | \`LAPLACE_RELAY_WEBHOOK_MODE\` | \`reaction\` | \`reaction\`=只推需互动事件，\`all\`=全推 |
| \`--webhook-min-weight\` | \`LAPLACE_RELAY_WEBHOOK_MIN_WEIGHT\` | \`0\` | 只推权重 ≥ N 的事件 |
| \`--ingest-token\` | \`LAPLACE_RELAY_INGEST_TOKEN\` | 空 | 保护 \`POST /api/ingest\` |
| \`--mock-interval\` | \`LAPLACE_RELAY_MOCK_INTERVAL\` | \`700\` | mock 平均间隔 ms |
| \`--mock-seed\` | \`LAPLACE_RELAY_MOCK_SEED\` | \`20261104\` | mock 随机种子（可复现） |
| \`--log-level\` | \`LAPLACE_RELAY_LOG_LEVEL\` | \`info\` | debug/info/warn/error |

---

## 4. 事件信封（envelope）

所有通道推送的都是**同一个对象**：

\`\`\`jsonc
{
  "v": 1,
  "id": "gift:1793000000001",        // 稳定 ID，可直接用于去重/ack
  "seq": 128,                        // 本会话单调序号
  "kind": "gift",                    // 33 类之一，见下表
  "family": "money",                 // chat|money|audience|room|lifecycle|moderation|system|unknown
  "ts": 1793000000123,               // 毫秒时间戳（源站纳秒/秒自动归一）
  "room": { "id": 123456, "platform": "bilibili" },

  "actor": {                         // 事件主体（房间级事件为 uid=0）
    "uid": 12001,
    "name": "夜航星",
    "avatar": "https://...",
    "role": "viewer",                // viewer | moderator | anchor
    "is_anchor": false,
    "is_moderator": false,
    "guard_level": 3,                // 0 无 1 总督 2 提督 3 舰长
    "guard_name": "舰长",
    "wealth_level": 18,
    "badge": { "name": "星见团", "level": 21 }
  },

  "text": "夜航星 送出 舰长 x1",      // 已拼好的中文文案，可直接 TTS/上屏
  "amount": {                        // 只有涉及钱的 kind 才有
    "value": 198000,                 // 原始值（金瓜子/银瓜子）
    "currency": "gold",
    "cny": 198,                      // 真实人民币（LAPLACE 的 priceNormalized）
    "approx": false                  // 银瓜子按 1000:1 估算时为 true
  },
  "duration_ms": null,               // SC / 上舰 的展示时长

  "interaction": {                   // ★ 主播互动优先级
    "needs_reaction": true,
    "weight": 85,                    // 0-100
    "suggested": ["thank_gift", "call_out_amount"]
  },

  "data": { /* 按 kind 保留各事件特有字段，见下表 */ },

  "source": {
    "transport": "laplace-bridge",   // laplace-bridge | inbound-bridge | mock | http-ingest
    "raw_type": "gift",
    "raw_id": "1793000000001",
    "bridge_source": "server-1"
  },

  "raw": { /* 仅 --include-raw 时存在 */ }
}
\`\`\`

### 互动权重（\`interaction.weight\`）

| 来源 | 权重 | suggested |
| --- | --- | --- |
| 醒目留言 SC | 100 | \`read_superchat\`, \`thank_superchat\` |
| 直播间警告/切断 | 95 | \`check_stream_health\` |
| 上舰/续费（toast） | 88-95 | \`welcome_membership\` |
| 礼物 | 55 + min(30, ¥×1.5)，银瓜子 22 | \`thank_gift\`（≥¥50 追加 \`call_out_amount\`） |
| MVP | 80 | \`thank_gift\` |
| 红包 / 天选开始 | 72 | \`promote_red_envelope\` / \`promote_lottery\` |
| 开播 / 下播 | 70 / 55 | \`open_stream\` / \`close_stream\` |
| 关注 / 特别关注 / 分享 | 38 / 42 / 30 | \`ack_follow\` / \`ack_share\` |
| 公告 notice | 45 | \`announce\` |
| 禁言 / 房管变更 | 25-35 | \`notify_moderation\` / \`ack_mod_change\` |
| 普通弹幕（无牌子） | 0 | — |
| 舰长/提督/总督弹幕 | 12 | \`greet_guard\` |
| 点赞、在线人数、进场 | 0 | — |

\`needs_reaction = weight >= 30\`。

### kind 一览

| family | kind | 上游 type |
| --- | --- | --- |
| chat | \`message\` | message |
| chat | \`effect_message\` | effect-message |
| money | \`gift\` | gift |
| money | \`superchat\` | superchat |
| money | \`membership\` | toast（上舰/续费） |
| money | \`mvp\` | mvp |
| money | \`red_envelope_start\` | red-envelope-start |
| money | \`red_envelope_result\` | red-envelope-result |
| money | \`lottery_start\` | lottery-start |
| money | \`lottery_result\` | lottery-result |
| audience | \`interaction\` | interaction（进场/关注/分享…） |
| audience | \`entry_effect\` | entry-effect |
| audience | \`like\` / \`likes_update\` | like-click / likes-update |
| audience | \`online_update\` / \`online_rank\` | online-update / online-rank-update |
| audience | \`watched_update\` | watched-update |
| room | \`room_meta\` / \`room_title\` | room-meta-update / room-name-update |
| lifecycle | \`live_start\` / \`live_end\` / \`live_warning\` / \`live_cutoff\` | live-* |
| moderation | \`recall\` / \`sc_delete\` / \`user_block\` / \`mute_on\` / \`mute_off\` / \`mod_assign\` / \`mod_revoke\` / \`mod_list\` | message-recall / superchat-delete / user-block / room-mute-* / mod-* |
| system | \`notice\` / \`system\` | notice / system |
| unknown | \`unknown\` | 任何未来新类型（不丢字段名） |

各 kind 的 \`data\` 字段与官方 \`@laplace.live/event-types\` 一一对应（例如 \`gift.data = {gift_id, gift_name, count, coin_type, icon, biz_source, receiver, blind_gift}\`，
\`superchat.data = {sc_id, sc_name, duration_s, message_color, translated, translated_on, audited…}\`）。完整定义见 \`src/events/normalize.js\`。

---

## 5. 对外接口

### 5.1 WebSocket 实时流（推荐）

\`\`\`
ws://127.0.0.1:8787/ws/stream?kinds=gift,superchat&families=money&min_weight=50&reaction=1
\`\`\`

| 查询参数 | 说明 |
| --- | --- |
| \`kinds\` | 逗号分隔的 kind 白名单 |
| \`families\` | 逗号分隔的 family 白名单 |
| \`min_weight\` | 只要权重 ≥ N |
| \`reaction=1\` | 只要 \`needs_reaction\` |

服务端首帧：

\`\`\`json
{ "t": "welcome", "subscriber_id": "ab12cd34", "session_id": "...", "server_time": 1793,
  "filter": { "kinds": null, "families": null, "min_weight": "50", "reactions_only": true } }
\`\`\`

随后每条事件一帧：\`{ "t": "event", "data": <envelope> }\`

客户端可发：

| 消息 | 作用 |
| --- | --- |
| \`{"t":"ack","event_id":"gift:xxx","action":"thanked","note":"已口头感谢"}\` | 回报「已回应」 |
| \`{"t":"filter","filter":{"families":"money,chat","min_weight":40}}\` | 运行时改过滤 |
| \`{"t":"ping"}\` | 心跳，回 \`{"t":"pong"}\` |

服务端回 \`{"t":"ack-ok","event_id":...}\`。

### 5.2 SSE

\`\`\`
GET /events?families=money&min_weight=50
\`\`\`

每条事件一个 \`event: <kind>\` + \`id: <seq>\` + \`data: <envelope>\`，15s 一次 \`: ping\` 保活。浏览器 \`EventSource\` 可直接用。

### 5.3 Webhook

\`--webhook https://your-service/hook\`，POST JSON：

\`\`\`
POST /hook
content-type: application/json
x-relay-event: gift
x-relay-seq: 128
x-relay-signature: sha256=<hmac-sha256(secret, body)>   ← 配置 --webhook-secret 时

{ "event": { ...envelope... } }
\`\`\`

失败自动指数退避重试 3 次；默认只推 \`needs_reaction\` 的事件（\`--webhook-mode all\` 全推）。

### 5.4 REST

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | \`/health\` | 健康检查（含 bridge 连接状态、订阅者数量） |
| GET | \`/api/state\` | 会话统计、订阅者、环缓冲、hook 队列、待互动数 |
| GET | \`/api/events?limit=100&kind=gift&family=money&reaction=1&since=<seq>\` | 环缓冲回放 |
| GET | \`/api/acks\` | 最近 ack 回执 |
| GET | \`/api/kinds\` | kind → family / 中文标签 / 权重表 |
| POST | \`/api/ingest\` | 手工注入原始 LAPLACE 事件（数组或对象），需 \`--ingest-token\` 时校验 \`X-Relay-Token\` |
| POST | \`/api/ack\` | HTTP 回执（等价 WS ack） |
| GET | \`/panel\` | 可视化观测面板 |

### 5.5 反向接入（别人推给中继）

\`ws://127.0.0.1:8787/bridge\` —— 复刻 leb-server 握手：连上先收 \`{"type":"established"}\`，之后发原始 LAPLACE 事件即可进入同一流水线。
用于「LAPLACE 控制台直连中继」，或自建采集器把事件打进来。

---

## 6. 下游接入示例

### Node（ws）

\`\`\`js
import WebSocket from 'ws';
const ws = new WebSocket('ws://127.0.0.1:8787/ws/stream?reaction=1');
ws.on('open', () => console.log('connected'));
ws.on('message', (buf) => {
  const msg = JSON.parse(buf);
  if (msg.t !== 'event') return;
  const e = msg.data;
  if (e.kind === 'superchat') speak('感谢 ' + e.actor.name + ' 的 ' + e.amount.cny + ' 元醒目留言：' + e.text);
  if (e.kind === 'gift' && e.amount.cny >= 10) speak('感谢 ' + e.actor.name + ' 的 ' + e.data.gift_name);
  ws.send(JSON.stringify({ t: 'ack', event_id: e.id, action: 'spoke' }));
});
\`\`\`

### 浏览器（SSE）

\`\`\`js
const es = new EventSource('http://127.0.0.1:8787/events?families=money');
es.onmessage = (ev) => render(JSON.parse(ev.data));
\`\`\`

### Python（websockets / requests）

\`\`\`python
import json, websockets, asyncio

async def main():
    async with websockets.connect("ws://127.0.0.1:8787/ws/stream?reaction=1") as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg["t"] != "event":
                continue
            e = msg["data"]
            print(e["kind"], e["actor"]["name"], e["text"], e["interaction"]["weight"])
            await ws.send(json.dumps({"t": "ack", "event_id": e["id"], "action": "handled"}))

asyncio.run(main())
\`\`\`

---

## 7. 观测面板

\`http://127.0.0.1:8787/panel\` —— 实时事件流（按族着色、礼物/SC 高亮、需互动标记 + 一键回应）、
事件分布柱状、会话统计（事件数/收益/待互动/已回应/速率）。截图见 \`docs/panel.png\`。

---

## 8. 测试

\`\`\`bash
npm test          # node --test，15 条用例
\`\`\`

覆盖：33 类归一化、金额换算（金/银瓜子）、时间戳归一（ns/s/ms）、未知类型降级、房间过滤、
mock 可复现性、seq/统计/环缓冲、过滤器匹配、ack 记账、**端到端（假 leb-server → 中继 → 订阅者 → ack）**、
反向 \`/bridge\` 接入。

---

## 9. 目录结构

\`\`\`
laplace-relay/
├─ src/
│  ├─ index.js              # 入口：装配 source → hub → http
│  ├─ config.js             # CLI + 环境变量
│  ├─ bridge/
│  │  ├─ client.js          # 连 leb-server（client 角色，重连/心跳/ping-pong）
│  │  └─ mock.js            # 可复现的模拟事件源
│  ├─ events/
│  │  ├─ kinds.js           # 33 kind → family/label/权重
│  │  └─ normalize.js       # 原始事件 → envelope
│  ├─ core/hub.js           # 分发中枢：seq/统计/环缓冲/落盘/过滤/ack/webhook
│  ├─ server/http.js        # HTTP + WS(/ws/stream, /bridge) + SSE + 面板
│  └─ panel/index.html      # 观测面板
├─ scripts/demo-client.mjs  # 参考消费者（打印 + 自动 ack）
└─ test/                    # node:test 用例 + 假 leb-server
\`\`\`

---

## 10. 已知限制 / 后续方向

- **LAPLACE 控制台必须常开**：事件来自浏览器里的 \`laplace.live/chat\` 面板（\`/dashboard/<房间号>\`），关掉就没有事件源。
  中继只负责「转接 + 结构化 + 分发」。
- 金额口径：金瓜子按 \`priceNormalized\` 取真实人民币；银瓜子是 \`value/1000\` 的估算（\`amount.approx=true\`）。
- 事件是**纯广播**、无历史补播；断线期间的缺口只能靠 \`/api/events\` 的环缓冲与 JSONL 日志补偿。
- 后续可扩展：多房间并行（当前单房间过滤）、事件持久化到 SQLite、按下游订阅做背压与限流、
  礼物连击合并（连击窗口内只保留一条）、给 TTS/字幕单独做「口播友好」视图。
