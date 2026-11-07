# 记忆系统设计

## 目标

主播要同时做到两件事：

1. **短期记住这一场**：刚才在哪、在做什么任务、遇到谁、说过什么，不要每轮
   重新认识世界；
2. **长期理解设定**：地区、角色、元素、任务链、观众约定，跨场次保持一致。

原来的导演只有一份 `memory` 字典（BetterGI 会话记忆）和最近 8 条解说，
没有检索，也没有剧情摘要；OpenViking 补上了长期检索与跨场次沉淀。

## 分层

```text
┌──────────────────────── 导演 LiveDirector ────────────────────────┐
│ 短期记忆（进程内）                                                │
│   scene / objective / plot   ← LLM 每轮 memory_patch 更新         │
│   _short_events（最近 40 条）← 解说/思考/指导/按键变化            │
│   _recent_commentary（最近 8 条）                                 │
│         │ 每轮拼成“短期记忆”段                                     │
│         ▼                                                          │
│ 长期记忆（OpenViking，可选）                                       │
│   recall(query) → 设定/往事片段                                    │
│   remember_session() 每 60 秒写一次本场滚动摘要                     │
└────────────────────────────────────────────────────────────────────┘
          │ prompt：短期 + 长期 + 最近解说 + 关键记忆
          ▼
      DeepSeek deepseek-flash（视觉 + 文本）
```

### 短期记忆

- `memory_patch` 新增约定字段：
  - `scene`：当前地点/任务/威胁（≤80 字）
  - `plot`：最近剧情进展（≤120 字）
  - `objective`：当前目标（≤60 字）
- 导演把这三个字段和最近事件拼成提示词里的“短期记忆”段。
- 这三个字段同时写进 BetterGI 会话记忆，进程重启后还能读回。
- 事件只保留最近 40 条、提示词只取最近 8 条，避免上下文无限增长。

### 长期记忆

OpenViking 是一个本地上下文数据库（AGPLv3），提供：

- `viking://~/resources/genshin`：设定、角色、任务、观众约定等资料；
- `viking://~/memories/genshin`：实况沉淀的剧情摘要；
- 语义检索返回 L0/L1 摘要，必要时读取 L2 正文。

导演每轮：

1. 用 `guidance + scene + objective + plot + 最近解说` 作为查询；
2. `search` 两个子树，取最多 4 条，压成 1800 字以内的“长期记忆”段；
3. 与短期记忆一起放进提示词。

每 60 秒把本场滚动摘要写入
`viking://~/memories/genshin/live/<session_id>.md`，最多保留 60 行。
写入使用 `wait=True`，确保摘要和向量完成后再进入下一轮。

## 为什么选 OpenViking

| 维度 | OpenViking | SuperMemory |
|---|---|---|
| 自托管 | 开源，AGPLv3 | 官方自托管二进制**非开源**，仅 lite 许可 |
| 依赖 | Python 服务 + 本地 bge-small-zh 嵌入 | 官方引擎闭源；社区替代版是 Postgres+pgvector |
| 记忆/RAG/技能 | 统一 `viking://` 文件树 | 偏记忆 API |
| 与 DeepSeek | VLM 用 OpenAI 兼容接口即可 | 云端需要额外 API Key |
| 本项目接入 | 已接入（HTTP SDK） | 未接入，仅保留适配位 |

结论：**用 OpenViking 做长期记忆，SuperMemory 不作为默认组件**。如果以后
需要云端同步或跨设备记忆，可以在 `memory_bridge.py` 增加一个
`SUPERMEMORY_API_KEY` 的适配器，接口保持 `recall/remember_session` 不变。

## 数据与隐私

- OpenViking 只监听 `127.0.0.1:1933`，使用 dev 认证，不接受公网访问。
- 记忆内容是本地 Markdown，可直接打开、编辑、删除。
- 发送给 DeepSeek 的只有当前帧、短期上下文和检索到的片段；不发送令牌。
- OpenViking 采用 AGPLv3；本项目以独立进程/HTTP 方式调用，未修改其源码。
  分发时需遵守 AGPL 的网络使用条款。

## 失败与降级

| 故障 | 行为 |
|---|---|
| OpenViking 未启动/连接失败 | 记 warning，长期记忆为空，短期记忆与导演照常 |
| 检索超时 | 本轮不注入长期记忆，下一轮重试 |
| 写入失败 | 记 debug；下一轮仍会写最新摘要 |
| DeepSeek 不可用 | 导演记录错误，当前轮跳过；短期记忆不丢 |
| 记忆文件过大 | 只保留最近 60 行；检索按 1800 字截断 |

## 配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `OPENVIKING_ENABLED` | 有 URL 时为 1 | 总开关 |
| `OPENVIKING_URL` | `http://127.0.0.1:1933` | 服务地址 |
| `OPENVIKING_LORE_URI` | `viking://~/resources/genshin` | 设定资料 |
| `OPENVIKING_MEMORY_URI` | `viking://~/memories/genshin` | 实况记忆 |
| `OPENVIKING_RECALL_LIMIT` | 4 | 每次检索条数 |
| `OPENVIKING_WRITE_INTERVAL_S` | 60 | 剧情摘要写入间隔 |

资料导入：

```bash
E:\BetterAGI\openviking\.venv\Scripts\python.exe ^
    BetterGI/integrations/lumine/memory_ingest.py ^
    --path BetterGI/lore --to "viking://~/resources/genshin"
```

## 后续可做

- 每场结束调用 OpenViking `commit_session`，让 VLM 自动抽取人物/事件/偏好；
- 对 `plot` 做去重与时间线合并，避免 60 行滚动摘要丢早期关键剧情；
- 把 BetterGI 的记忆键值同步成 OpenViking 的 `profile`/`preferences`；
- 给检索加 rerank（OpenViking 支持，但需要额外的 rerank 模型）。
