# Graphiti 部署与 Python API 核实报告

> 核实日期：2026-11-04 ｜ 用途：游戏 agent 长期记忆（每游戏一个 `group_id`）｜ 状态：纯调研，未改任何项目代码
> 核实方式：PyPI 元数据直读、GitHub 仓库 main 分支源码（解包至 `.research/graphiti/graphiti-main`）、官方文档（help.getzep.com 的 `.md` 版本）、在本机 venv（Python 3.12）真实 `pip install graphiti-core==0.31.2` 后用 `inspect.signature` 读签名并实际构造客户端。
> **本机无 Docker**，因此「起库 → 写 episode → 检索」的全链路**未在本机跑通**，未验证点集中在 §8。

## 结论速览

| # | 问题 | 结论 |
|---|---|---|
| 1 | 版本/包/后端 | `graphiti-core==0.31.2`（PyPI 最新，2026-10-20 上传），Python >=3.10,<4；后端 Neo4j 5.26 / FalkorDB 1.1.2 / Neptune；**Kuzu 已废弃** |
| 2 | 最小部署 | `docker run` Neo4j 5.26-community + `Graphiti(uri, user, password)` + `build_indices_and_constraints()`；镜像自带 JVM |
| 3 | group_id | 是节点/边/社区上的**属性字段**（不是独立库），非法字符（`:`、空格）会被拒；`search(group_ids=[...])` 过滤，**传 `None`/`[]`/`['']` 等于全库检索** |
| 4 | DeepSeek | 用 `OpenAIGenericClient` + `structured_output_mode="json_object"`；**必须自备 cross_encoder**，否则默认 `OpenAIRerankerClient` 因缺 `OPENAI_API_KEY` 直接报错 |
| 5 | 自定义类型 | `entity_types` / `edge_types` / `edge_type_map` 按**每次 add_episode 传入**，Pydantic 模型即可定义 `KeyBinding`、`GameRule` |

---

## 1. 版本、包名、安装、后端

### 1.1 包与版本

- **包名**：`graphiti-core`（PyPI）。来源：https://pypi.org/project/graphiti-core/
- **当前稳定版**：**0.31.2**，上传时间 2026-10-20T17:25:54Z（PyPI JSON API 直读）；仓库 `pyproject.toml` 版本号同为 0.31.2。
- **Python 要求**：`requires_python = ">=3.10,<4"`（PyPI）。
- 安装：

```bash
pip install graphiti-core            # 默认（Neo4j 后端）
pip install graphiti-core[falkordb]  # FalkorDB 后端
pip install httpx                    # 当前必装，原因见 §6 表第 1 行
```

- **基础依赖**（直读 PyPI `requires_dist`）：`pydantic>=2.11.5`、`neo4j>=5.26.0`、`openai>=1.91.0`、`tenacity>=9.0.0`、`numpy>=1.0.0`、`python-dotenv>=1.0.1`、`posthog>=3.0.0`、`certifi`。注意 **`httpx` 未被声明**，这是 0.31.2 的已知打包 bug。
- 可选 extras：`falkordb`、`falkordblite`、`kuzu`、`neo4j-opensearch`、`neptune`、`anthropic`、`groq`、`google-genai`、`voyageai`、`sentence-transformers`、`gliner2`、`tracing`。

### 1.2 图数据库后端与版本要求

| 后端 | 版本要求 | 状态 | 来源 |
|---|---|---|---|
| **Neo4j** | 5.26+（文档命令用 `neo4j:5.26-community`，仓库 compose 用 `neo4j:5.26.2`） | 推荐，能力最全（含 BM25 全文索引） | README:165-166；https://help.getzep.com/graphiti/configuration/neo4j-configuration |
| **FalkorDB** | 1.1.2+ | 支持，需 `[falkordb]` extra | README:165-166；https://help.getzep.com/graphiti/configuration/falkordb-configuration |
| **Kuzu** | 0.11.2（驱动随包发布） | **已废弃**：上游不再维护，导入即 `DeprecationWarning`，未来版本移除 | README:214-217 |
| Neptune | + OpenSearch Serverless（FTS） | 支持（AWS） | README:165-166 |

驱动类构造签名（本机 `inspect.signature` 实测，0.31.2）：

```python
Neo4jDriver.__init__(self, uri: str, user: str | None, password: str | None, database: str = 'neo4j')
FalkorDriver.__init__(self, host='localhost', port=6379, username=None, password=None,
                     falkor_db: FalkorDB | None = None, database='default_db')
KuzuDriver.__init__(self, db: str = ':memory:', max_concurrent_queries: int = 1)  # 废弃
```

> 未装 `[falkordb]` 时导入 `FalkorDriver` 抛：`ImportError: falkordb is required for FalkorDriver. Install it with: pip install graphiti-core[falkordb]`（实测）。

---

## 2. 最小本地部署（Docker + Neo4j）

### 2.1 起 Neo4j

官方文档（`neo4j-configuration`）的单容器命令：

```bash
docker run --name neo4j-community -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/your_password \
  -e NEO4J_PLUGINS='["apoc"]' \
  neo4j:5.26-community
```

仓库自带 `docker-compose.yml`（可作持久化部署模板）：镜像 `neo4j:5.26.2`，端口 `127.0.0.1:7474` 与 `127.0.0.1:${NEO4J_PORT:-7687}`，数据卷 `neo4j_data:/data`，默认凭据 `${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:-password}`。来源：https://github.com/getzep/graphiti/blob/main/docker-compose.yml

要点：
- 官方镜像**自带 JVM**，宿主机不需要装 Java。
- 若用 Neo4j AuraDB（云），URI 形如 `neo4j+s://xxxx.databases.neo4j.io`，APOC 已内置。
- 7474 是 Browser/HTTP，**7687 才是 Bolt**（客户端连这个）。

### 2.2 客户端初始化（真实类名/参数）

```python
from graphiti_core import Graphiti

graphiti = Graphiti("bolt://localhost:7687", "neo4j", "your_password",
                    store_raw_episode_content=True)
await graphiti.build_indices_and_constraints()   # 官方要求：首次使用前必须先建索引/约束
# ...
await graphiti.close()
```

`Graphiti.__init__` 完整签名（实测）：

```python
Graphiti.__init__(self, uri=None, user=None, password=None, llm_client=None, embedder=None,
                  cross_encoder=None, store_raw_episode_content: bool = True, graph_driver=None,
                  max_coroutines: int | None = None, tracer=None, trace_span_prefix='graphiti',
                  prompt_library=None, llm_runtime=None)
```

> 不需要自定义 LLM/embedder 时用前三个参数即可（走默认 OpenAI 客户端）；要接 DeepSeek 见 §4。
> 多实例部署时用 `max_coroutines` 控制并发（默认信号量 20，见 §9 第 7 条）。

## 3. `group_id` 的确切语义

### 3.1 结论

`group_id` 是**写在每个节点/边/社区/episode 上的属性字段**，用来在**同一个图库内**做逻辑分区；Neo4j 后端下**不是**独立 database。源码注释即 `group_id: str = Field(description='partition of the graph')`（`graphiti_core/nodes.py:96`）。

官方文档：https://help.getzep.com/graphiti/core-concepts/graph-namespacing

### 3.2 校验规则（关键坑）

`validate_group_id` 只允许 `^[a-zA-Z0-9_-]+$`，否则抛 `GroupIdValidationError`（`graphiti_core/helpers.py:145-166`）。也就是说 **`:`、空格、中文、`.`、`/` 都会被拒**——游戏 ID 若形如 `steam:730` 必须先规范化为 `steam_730`。空字符串允许。

### 3.3 默认值

`add_episode(group_id=None)` 时取 `get_default_group_id(provider)`：Neo4j/Kuzu 是 `''`，FalkorDB 是 `'_'`（`helpers.py:77-85`）。**坑**：`''` 恰好等于「不过滤」，所以忘传 group_id 会导致跨游戏混淆。

### 3.4 隔离是怎么实现的

`_resolve_request_scope` 按每次调用解析驱动：
- 若传入的 `group_id != driver._database`，则 `driver.clone(database=group_id)`；
- **FalkorDB**：clone 真正切换图名 → 每个 group 一个独立图；
- **Neo4j**：clone 是浅拷贝，隔离靠 Cypher 过滤：`WHERE n.group_id IN $group_ids`（`nodes.py:660/704`，episode 查询 `episodes.py:443`）。

### 3.5 写入：`add_episode` 真实签名

```python
Graphiti.add_episode(self, name: str, episode_body: str, source_description: str,
    reference_time: datetime, source: EpisodeType = EpisodeType.message,
    group_id: str | None = None, uuid: str | None = None, update_communities: bool = False,
    entity_types: dict[str, type[BaseModel]] | None = None,
    excluded_entity_types: list[str] | None = None,
    previous_episode_uuids: list[str] | None = None,
    edge_types: dict[str, type[BaseModel]] | None = None,
    edge_type_map: dict[tuple[str, str], list[str]] | None = None,
    custom_extraction_instructions: str | None = None,
    saga: str | SagaNode | None = None, saga_previous_episode_uuid: str | None = None
) -> AddEpisodeResults
```

`EpisodeType` 取值：`message` / `json` / `text` / `fact_triple`。
docstring 明确：**episode 必须顺序写入**，且推荐放进后台队列（每次 add_episode 都会跑 LLM 抽取，秒级到十几秒）。

实体去重是 **group 内**进行的（`get_by_names(..., group_ids)`），所以两个游戏里都有「Alice」不会互相合并。

### 3.6 检索：`search` / `search_` 真实签名

```python
Graphiti.search(self, query: str, center_node_uuid: str | None = None,
    group_ids: list[str] | None = None, num_results=10,
    search_filter: SearchFilters | None = None, driver=None) -> list[EntityEdge]
# 内部固定用 EDGE_HYBRID_SEARCH_RRF（不吃 cross-encoder）

Graphiti.search_(self, query: str, config: SearchConfig = COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
    group_ids: list[str] | None = None, center_node_uuid=None, bfs_origin_node_uuids=None,
    search_filter=None, driver=None, query_vector: list[float] | None = None) -> SearchResults
# 装饰器 @handle_multiple_group_ids
```

**最重要的坑**（`graphiti_core/search/search.py:155-156`）：

```python
group_ids = group_ids if group_ids and group_ids != [''] else None
```

即 `None` / `[]` / `['']` 都表示**全库检索（无过滤）**。单游戏检索必须显式 `group_ids=["game_123"]`。

`SearchFilters` 可用字段：`node_labels`、`edge_types`、`valid_at`、`invalid_at`、`created_at`、`expired_at`、`edge_uuids`、`edge_source_node_uuids`、`edge_target_node_uuids`、`property_filters`。

按 group 清理：`delete_by_group_id` / `get_by_group_ids`（`EpisodicNode` / `EntityNode` / `CommunityNode` 上都有）。⚠️ 删除是硬删除，玩家重置存档时注意调用成本。

---

## 4. LLM 与 embedding 接 DeepSeek（OpenAI 兼容端点）

### 4.1 用哪个 client

- **必须用 `OpenAIGenericClient`**，不要用 `OpenAIClient`。后者打 `/v1/responses`，只适合真 OpenAI；前者打标准 `/v1/chat/completions`（README:571-577）。
- `OpenAIGenericClient` 的 docstring 里**明确点名 DeepSeek**：支持 OpenAI / vLLM / llama.cpp / Ollama / **DeepSeek** / Together；并注明「不支持 `json_schema` 的提供方（**例如 DeepSeek**）请设 `structured_output_mode='json_object'`，此时 schema 会注入 prompt」。来源：`graphiti_core/llm_client/openai_generic_client.py:47-81`

签名（实测）：

```python
LLMConfig(api_key=None, model=None, base_url=None, temperature: float = 1,
          max_tokens: int = 16384, small_model: str | None = None)

OpenAIGenericClient(config: LLMConfig | None = None, cache: bool = False, client: Any = None,
          max_tokens: int = 16384,
          structured_output_mode: Literal['json_schema', 'json_object'] = 'json_schema')

OpenAIEmbedderConfig(embedding_dim=1024, embedding_model=..., api_key=..., base_url=...)
OpenAIEmbedder(config: OpenAIEmbedderConfig, client=None)
```

`small_model` 用于轻量步骤（去重/摘要等），不填则回落到 `model`。`embedding_dim` 默认取环境变量 `EMBEDDING_DIM` 或 1024，**必须与实际 embedding 模型维度一致**，否则向量检索全线报错。

### 4.2 必须自备 cross_encoder（否则必崩）

默认 `Graphiti(...)` 会构造 `cross_encoder=OpenAIRerankerClient()`，它内部是 `AsyncOpenAI`，**没有 `OPENAI_API_KEY` 就直接抛**：

```
OpenAIError: Missing credentials. Please pass an api_key, workload_identity, admin_api_key,
or set the OPENAI_API_KEY or OPENAI_ADMIN_KEY environment variable.
```

（本机实测复现；同源上游 issue：https://github.com/getzep/graphiti/issues/1441 ）

而且 `OpenAIRerankerClient` 的实现是 `chat.completions` + `logit_bias={'6432':1,'7983':1}` + `logprobs=True`，模型默认 `gpt-4.1-mini`（`cross_encoder/openai_reranker_client.py:62-119`）——**DeepSeek 上不可用**。

DeepSeek-only 方案：传一个 noop `CrossEncoderClient` 子类，并把检索换成 RRF 配方（`EDGE_HYBRID_SEARCH_RRF` / `COMBINED_HYBRID_SEARCH_RRF` / `NODE_HYBRID_SEARCH_RRF`）。注意 `search_()` 的**默认** `COMBINED_HYBRID_SEARCH_CROSS_ENCODER` 会用到 cross-encoder，必须显式换掉；而 `search()` 内部就是 `EDGE_HYBRID_SEARCH_RRF`，天然安全。

抽象基类签名（`cross_encoder/client.py:20-40`）：

```python
class CrossEncoderClient(ABC):
    @abstractmethod
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]: ...
```

### 4.3 可直接复制的初始化（本机已实测能构造成功）

```python
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.client import CrossEncoderClient

class NoopReranker(CrossEncoderClient):
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 1.0 / (i + 1)) for i, p in enumerate(passages)]

llm = OpenAIGenericClient(
    config=LLMConfig(api_key=DEEPSEEK_KEY, model="deepseek-chat", small_model="deepseek-chat",
                     base_url="https://api.deepseek.com/v1"),
    structured_output_mode="json_object",   # DeepSeek 必须
)
embedder = OpenAIEmbedder(
    config=OpenAIEmbedderConfig(embedding_model="<你的兼容 embedding 模型名>",
                                embedding_dim=1024, api_key=EMB_KEY, base_url=EMB_BASE_URL)
)
graphiti = Graphiti("bolt://localhost:7687", "neo4j", "password",
                    llm_client=llm, embedder=embedder, cross_encoder=NoopReranker())
```

> 关于 DeepSeek 官方是否提供 `/v1/embeddings`：**未证实**（本次未实测该端点）。graphiti 侧只要求「OpenAI 兼容的 `/v1/embeddings`」，任何兼容服务（本地 TEI / Ollama / 硅基流动 / 智谱 / OpenAI）都可作 `base_url`，但**维度必须与 `embedding_dim` 匹配**。
> 官方示例中 `cross_encoder=OpenAIRerankerClient(client=llm, config=llm_config)` 的写法只适合真 OpenAI 端点。

---

## 5. 自定义实体 / 边类型（KeyBinding / GameRule）

### 5.1 传参方式

`entity_types`、`edge_types`、`edge_type_map` 都是 **`add_episode()` 的按次入参**，没有全局注册表。

```python
entity_types: dict[str, type[BaseModel]]              # 实体类型名 -> Pydantic 模型
edge_types:   dict[str, type[BaseModel]]              # 边类型名   -> Pydantic 模型
edge_type_map: dict[tuple[str, str], list[str]]       # (源实体类型, 目标实体类型) -> 允许的边类型
```

- `('Entity', 'Entity')` 是通配，表示「任意实体之间都可用这些边类型」。
- 未在 `edge_type_map` 里声明的组合：沿用 LLM 抽出的谓词名做边名，**不带自定义 schema**。
- 只给 `edge_types` 不给 `edge_type_map` 时，默认 `{('Entity','Entity'): list(edge_types.keys())}`（`graphiti.py:1463-1468`）。

官方文档：https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types

### 5.2 KeyBinding / GameRule 可以直接定义

```python
from pydantic import BaseModel, Field

class KeyBinding(BaseModel):
    """玩家的一条按键绑定及其变更历史。"""
    action: str | None = Field(None, description="动作名，如 jump / interact")
    key: str | None = Field(None, description="绑定按键，如 Space")
    changed_reason: str | None = Field(None, description="玩家修改该绑定的原因")

class GameRule(BaseModel):
    """玩家陈述或游戏内设定的规则约束。"""
    rule: str | None = Field(None, description="规则内容，一句话表述")
    scope: str | None = Field(None, description="适用范围：本局 / 全局 / 某章节")
    strictness: str | None = Field(None, description="玩家对该规则的坚持程度")

ENTITY_TYPES = {"KeyBinding": KeyBinding, "GameRule": GameRule}
```

实践要点（官方文档明确要求）：
- 字段**尽量 Optional**，否则 LLM 抽不出值会导致整条失败；
- 每个字段写 `Field(description=...)`，这是抽取质量的主要杠杆；
- 自定义边类型同理，且**类型演进只能加字段**，老节点不会回填；
- 检索时按类型过滤：`SearchFilters(node_labels=["KeyBinding"])` 或 `SearchFilters(edge_types=["PREFERS"])`。

---

## 6. Windows 上的已知坑

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| 1 | **`import graphiti_core` 直接失败**：`ModuleNotFoundError: No module named 'httpx'` | 装完就崩，本机 100% 复现 | `pip install httpx`（实测修好）；上游 issue #1893，修复 PR #1894/#1895 未合 |
| 2 | **Docker Desktop / WSL2** | Neo4j 容器需 WSL2 后端；本机未装 Docker 则整条链路跑不了 | 装 Docker Desktop 或改用 Neo4j Desktop；确认 7687 在监听 |
| 3 | **Neo4j 内存** | 默认堆偏小，建索引/批量写入易 OOM 或极慢 | 给容器分配内存参数（`NEO4J_server_memory_heap_max__size` 等，具体键名按 5.26 版本核对，未实测）；建议容器至少分到 2GB |
| 4 | **Kuzu 在 Windows 上写入崩溃** | `add_episode` 触发 C 扩展 access violation（`kuzu/connection.py:134`），读正常、写崩 | 别选 Kuzu（且已废弃）；上游 issue #1469 未修 |
| 5 | **docker-compose 里 `${PATH}` 插值** | 旧版 compose 文件把宿主 Windows PATH 塞进容器，`redis-server: command not found`、exit 127 | issue #1623（已关）；直接用本文 §2.1 的 `docker run`，别抄旧教程的 compose |
| 6 | **FalkorDB + 反斜杠路径** | episode 正文里的 `C:\path\` 会破坏 RediSearch 查询 | issue #1173；写入前把 `\` 换成 `/` |
| 7 | **FalkorDB 并发** | 单连接并发查询会 `Connection closed by server`（仓库 AGENTS.md 明说） | Windows 本地优先 Neo4j；FalkorDB 需限并发 |
| 8 | `falkordblite` 无 Windows wheel | 只有 macOS / manylinux 轮子 | Windows 上不可用，走 Docker 或远端 |
| 9 | Python 版本 | 3.12 实测可用；3.13 / 3.14 / 3.15 **未证实** | 用 3.11 或 3.12 建 venv |

---

## 7. 最小可跑脚本

> 前置：§2.1 的 Neo4j 容器已启动；`pip install graphiti-core httpx`；环境变量 `DEEPSEEK_API_KEY`、`NEO4J_PASSWORD` 已设。

```python
# graphiti_smoke.py —— 起 Neo4j 后：写 2 条 episode → 按 group_id 检索 → 打印结果
import asyncio, os
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF


class NoopReranker(CrossEncoderClient):
    async def rank(self, query, passages):
        return [(p, 1.0 / (i + 1)) for i, p in enumerate(passages)]


class GameRule(BaseModel):
    rule: str | None = Field(None, description='规则内容')
    scope: str | None = Field(None, description='适用范围')


GAME_A, GAME_B = "game_elden_001", "game_stardew_002"


async def main():
    llm = OpenAIGenericClient(
        config=LLMConfig(api_key=os.environ["DEEPSEEK_API_KEY"],
                         model="deepseek-chat", small_model="deepseek-chat",
                         base_url="https://api.deepseek.com/v1"),
        structured_output_mode="json_object",
    )
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            embedding_model=os.environ.get("EMB_MODEL", "text-embedding-3-small"),
            embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1536")),
            api_key=os.environ.get("EMB_API_KEY", os.environ["DEEPSEEK_API_KEY"]),
            base_url=os.environ.get("EMB_BASE_URL", "https://api.openai.com/v1"),
        ))

    g = Graphiti("bolt://localhost:7687", "neo4j", os.environ["NEO4J_PASSWORD"],
                 llm_client=llm, embedder=embedder, cross_encoder=NoopReranker())
    await g.build_indices_and_constraints()

    try:
        # 同一 group_id 的两条 episode，必须串行写入
        r1 = await g.add_episode(
            name="player_binding_change",
            episode_body="玩家把跳跃键从 Space 改成了 V，因为手柄坏了。",
            source=EpisodeType.text,
            source_description="in-game chat",
            reference_time=datetime.now(timezone.utc),
            group_id=GAME_A,
            entity_types={"GameRule": GameRule},
        )
        print("episode1 uuid:", getattr(r1, "episode", None) and r1.episode.uuid or r1)

        await g.add_episode(
            name="boss_rule",
            episode_body="玩家要求：打 Boss 前必须先存档（硬性规则）。",
            source=EpisodeType.text,
            source_description="in-game chat",
            reference_time=datetime.now(timezone.utc),
            group_id=GAME_A,
            entity_types={"GameRule": GameRule},
        )

        # 另一个游戏写一条，用于验证隔离
        await g.add_episode(
            name="other_game",
            episode_body="玩家在这个游戏里只玩钓鱼。",
            source=EpisodeType.text,
            source_description="in-game chat",
            reference_time=datetime.now(timezone.utc),
            group_id=GAME_B,
            entity_types={"GameRule": GameRule},
        )

        # 单 group 检索：search() 默认 RRF，不依赖 cross-encoder
        edges = await g.search("跳跃键改成了什么？", group_ids=[GAME_A], num_results=5)
        for e in edges:
            print(f"[{GAME_A}] {e.name}: {e.fact}")

        # 想看节点+社区：search_ + RRF 配方（不要用默认的 CROSS_ENCODER 配方）
        res = await g.search_("存档规则", config=COMBINED_HYBRID_SEARCH_RRF,
                              group_ids=[GAME_A])
        print("nodes:", [n.name for n in res.nodes])
    finally:
        await g.close()


if __name__ == "__main__":
    asyncio.run(main())
```

**跑之前必须确认**：脚本里的 embedding 模型名与维度是占位值，必须换成真实可用的 OpenAI 兼容 embedding 服务，否则 `add_episode` 会在 embed 阶段报错。

---

## 8. 未证实 / 未能验证清单（不要当结论用）

1. **端到端跑通**：本机无 Docker，`add_episode` → `search` 的真实时延、抽取质量、检索命中率**均未实测**。
2. **DeepSeek 官方 embedding 端点**：未确认 `https://api.deepseek.com/v1/embeddings` 是否存在，需用 curl 验。
3. **DeepSeek 在 `json_object` 模式下的抽取稳定性**：抽取是重 schema 任务，小模型可能漏字段；建议先跑 20 条真实对话统计失败率，未实测。
4. **Neo4j 5.26 容器内存参数键名**：未实测该版本的 `NEO4J_dbms_*` 与 `NEO4J_server_memory_*` 哪个生效。
5. **FalkorDB 在 Windows 上的并发上限**：仅仓库文档描述，未实测。
6. **Python 3.13+ / 3.15 兼容性**：未证实。

---

## 9. 给「每游戏一个 group_id」的落地建议

1. **group_id 规范化**：只允许 `[A-Za-z0-9_-]`，把 `steam:730`、`游戏 名` 统一转成 `steam_730`、`game_xxx`，在应用层做映射表，不要靠 graphiti 抛错兜底。
2. **永远显式传 group_ids**：`search()` 不传 = 全库，这是最容易出的「串档」事故；封装 `search_game(game_id, q)` 强制加参数。
3. **写入串行化**：`add_episode` 会跑多次 LLM，必须排队（asyncio.Queue / 单 worker），不要并发打同一 group。
4. **关闭遥测**：`GRAPHITI_TELEMETRY_ENABLED=false`（默认走 posthog 上报，影响启动与隐私）。
5. **别暴露内置 server**：`mcp_server/` 与 `server/` 是参考实现、**无鉴权**，README:327-341 明确「任何调用方可读改删任意 group_id 的数据」。
6. **成本**：每条 episode 至少 1 次抽取 LLM + 若干 embedding + 去重调用；按每条 1–3k token 估，DeepSeek 便宜但要限流。
7. **并发上限**：默认信号量 20（`helpers.py:46`，环境变量 `SEMAPHORE_LIMIT` 覆盖；README 里写的 10 已过时），多实例部署用 `max_coroutines`。

---

## 10. 来源清单

**官方文档**（`help.getzep.com/graphiti/...`，URL 加 `.md` 可直接取 markdown）
- Quick Start：https://help.getzep.com/graphiti/getting-started/quick-start
- Graph Namespacing（group_id）：https://help.getzep.com/graphiti/core-concepts/graph-namespacing
- Custom Entity & Edge Types：https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types
- Searching：https://help.getzep.com/graphiti/working-with-data/searching
- Neo4j 配置：https://help.getzep.com/graphiti/configuration/neo4j-configuration
- LLM 配置：https://help.getzep.com/graphiti/configuration/llm-configuration
- FalkorDB 配置：https://help.getzep.com/graphiti/configuration/falkordb-configuration

**代码 / 包**
- 仓库：https://github.com/getzep/graphiti
- docker-compose：https://github.com/getzep/graphiti/blob/main/docker-compose.yml
- PyPI：https://pypi.org/project/graphiti-core/

**关键 issue**
- httpx 缺失（0.31.2 装完即崩）：https://github.com/getzep/graphiti/issues/1893
- Kuzu Windows 崩溃：https://github.com/getzep/graphiti/issues/1469
- compose `${PATH}` 插值：https://github.com/getzep/graphiti/issues/1623
- FalkorDB 反斜杠：https://github.com/getzep/graphiti/issues/1173
- 非 OpenAI 提供方被 reranker 卡死：https://github.com/getzep/graphiti/issues/1441

**本机实测记录**
- venv：`E:/BetterAGI/.research/graphiti/venvtest`（Python 3.12，graphiti-core 0.31.2 + httpx）
- 签名/构造脚本：该目录下 `introspect.py`、`inittest.py`
- 源码快照：`E:/BetterAGI/.research/graphiti/graphiti-main`（main 分支 tarball）


## 本机部署结论�?026-11-04 实测，已跑通）

**环境**：WSL2 Ubuntu + Docker 29.1.3 + Neo4j 5.26 容器；Windows �?Python 直连 bolt://localhost:7687�?
启动脚本：scripts/start_graphiti.ps1�?

| 组件 | 实际选择 | 说明 |
| --- | --- | --- |
| 图库 | Neo4j 5.26 容器 graphiti-neo4j | 7687(Bolt) �?Windows 可达�?474(HTTP) �?WSL NAT 下未转发，不影响 Graphiti |
| LLM | DeepSeek Chat（OpenAI 兼容端点�?| agent/graphiti_wiring.py �?build_llm_client，密钥取环境变量�?~/.dsh/.credentials.yaml �?refs |
| Embedding | 进程�?fastembed BAAI/bge-small-zh-v1.5�?12 维） | 本机 CPU、离线缓存；不用 OpenAI embeddings（DeepSeek 无该端点�?|
| Reranker | 关闭，走 RRF | Graphiti 默认 OpenAIRerankerClient 依赖 logit_bias，DeepSeek 不兼�?|

**踩过的三个真坑（都已修在代码里）**

1. **客户端必须和调用在同一条事件循环上**。neo4j 异步驱动把连接绑死建立时�?loop，跨 asyncio.run 复用会报 AttributeError: NoneType has no attribute send。方案：memory._LoopRunner 常驻 loop 线程，客户端构建与所有调用都提交到它上面（agent/memory.py）�?
2. **LLM/embedding 客户端也要在 loop 内实例化**，否�?httpx 异步客户端跨 loop �?bound to a different event loop。方案：build_memory �?llm_factory / embedder_factory，在 _async_build 里落地�?
3. **新版本不再隐式建索引**：不�?build_indices_and_constraints() 时检索报 There is no such fulltext schema index: edge_name_and_fact。已在建客户端后幂等执行�?

**验收输出**（_diag/m2_graphiti_live.py，真�?Neo4j + 真实 DeepSeek�?

```
profile: genshin-impact group: genshin-impact
backend: graphiti group: genshin-impact
write_seconds: 13.8
hits: 5
 - 元素视野可以通过�?V 键开
 - 元素视野可在蒙德城七天神像附近使�?
 - 元素视野可在蒙德城七天神像附近通过 V 键开
cross_group_leak: 0
OK
```

写入 2 �?episode 耗时 13.8s（含 DeepSeek 抽取），检�?5 条命中，�?group 零泄漏�?

**运维提醒**：WSL 发行版空闲会自动停机，容器随之退出。长期运行需保活（常�?wsl 会话 / 计划任务），或用上面的脚本拉起�
