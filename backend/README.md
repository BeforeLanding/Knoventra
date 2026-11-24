# Knoventra 后端

Python 3.12 / FastAPI / LangGraph / PostgreSQL + pgvector；可选 Elasticsearch 关键词通道、Redis 工作记忆和独立的 MCP 工具服务。React 前端在 [`../web`](../web/README.md)，整体介绍见[根目录 README](../README.md)。

自动化测试使用离线 Hash 编码器和脚本化模型；真实 BGE-M3、LLM 与本地精排的测量记录在[评测报告](../docs/evaluation-report.md)。演示账号与虚构资料见 `../data/demo/manifest.json`（密码均为虚构的演示值）。

## 模块职责总览

`src/knoventra/` 下的包：

| 包 / 模块 | 职责 |
|---|---|
| `main.py`、`api/` | FastAPI 应用与路由：`routes.py`（登录、`/me`、管理员资料、`/debug/search`）、`chat.py`（聊天、确认、续跑、补存、会话与来源预览、转交记录）、`help.py`（求助单）、`memory.py`（记忆）；`run_lock.py` 为会话级互斥锁，`deps.py` 为依赖注入 |
| `auth/` | 登录 JWT；MCP 工具服务使用的受众隔离短期 token（`tool_token_secret`） |
| `access/` | 区域 / 部门 / 角色的可读范围计算 |
| `db/` | ORM 模型、会话、演示种子（`python -m knoventra.db.seed`） |
| `ingestion/` | 上传解析、分块、编码校验、修订草稿与发布；现场日志导入（`journal.py`）、去重与案例生成（`cases.py`、`case_docs.py`） |
| `retrieval/` | 四通道召回、加权 RRF、编号规范化与门槛（`identifiers.py`）、可选精排 |
| `search_es/` | ES 索引、查询、纠错表 / 同义词、outbox 同步与对账（`python -m knoventra.search_es.sync`） |
| `agent/` | LangGraph 图（`graph.py`）、`query_plan.py`、明确转交意图（`escalation_intent.py`）、回答字形转换（`language.py`）、检查点辅助 |
| `tools/` | 5 个工具的共享服务（`service.py`）与进程内 / MCP 两种调用后端（`backend.py`） |
| `mcp_server/` | 独立 MCP 工具服务（`python -m knoventra.mcp_server`） |
| `help/` | 求助单槽位、状态机与服务（`service.py`）、专家推荐、结案案例草稿（`sediment.py`） |
| `memory/` | Redis 工作记忆、槽位草稿、滚动摘要与上下文组装、情景记忆、用户画像 |
| `skills.py` | 技能矩阵查询 |

数据库迁移链为 `alembic/versions/` 下的 `0001`–`0016`，主要节点：`0004` 文档 / 修订约束，`0005` 回答送达追踪，`0006` 转交时间，`0007` 技能矩阵，`0008` 日志案例，`0009` ES outbox，`0010` 分块编号列，`0011` 求助单，`0014` 情景记忆与画像，`0015` 确认载荷，`0016` 记忆列非空约束。升级已有实例时先执行 `uv run alembic upgrade head`；迁移不会替你合并冲突的历史重复数据，也不会伪造历史时间或补全缺失的历史元数据。

## 环境与配置项速查

所有配置项读取环境变量或 `backend/.env`，前缀为 `KNV_`；定义在 `src/knoventra/config.py`，示例见 `.env.example`。修改模型相关配置后需重启进程（图与编码器在进程内缓存）。

**基础**

| 变量 | 默认 | 说明 |
|---|---|---|
| `KNV_DATABASE_URL` | `postgresql+psycopg://knoventra:knoventra@localhost:55432/knoventra` | 应用库 |
| `KNV_JWT_SECRET`、`KNV_JWT_TTL_MINUTES` | 开发占位值、480 | 登录 token；生产必须更换 |
| `KNV_ENV` | `dev` | 环境名 |

**编码器与精排**

| 变量 | 默认 | 说明 |
|---|---|---|
| `KNV_ENCODER` | `hash` | 仅接受 `hash`（离线、确定性）或 `bge-m3`（需 `uv sync --extra ml`）；区分大小写 |
| `KNV_DENSE_DIM`、`KNV_SPARSE_DIM` | 1024、250002 | 必须为正，且与数据库物理列和编码器输出一致；本检查不迁移数据库 |
| `KNV_RERANK` | `off` | 仅接受 `off` 或 `bge`；`bge` 要求 `KNV_RERANK_MODEL` 非空 |
| `KNV_RERANK_MODEL`、`KNV_RERANK_CANDIDATES`、`KNV_RERANK_THRESHOLD` | `BAAI/bge-reranker-v2-m3`、10、0.60 | 精排阈值独立于 `KNV_GATE_THRESHOLD`，不是已标定的生产阈值 |
| `KNV_GATE_THRESHOLD`、`KNV_GATE_SPARSE_THRESHOLD` | 0.60、0.03 | 证据闸门（稠密余弦 / 稀疏内积），在 dev 集上调得 |
| `KNV_RECALL_TOP_K`、`KNV_FINAL_TOP_K`、`KNV_RRF_K` | 20、5、60 | 召回与融合 |
| `KNV_RRF_WEIGHTING` | `weighted` | `weighted` 或 `equal`（对照）；权重为 `KNV_RRF_WEIGHTS_IDENTIFIER`、`KNV_RRF_WEIGHTS_NATURAL`（JSON） |

**Elasticsearch**：`KNV_ES_URL`（默认空 = 关闭，仅用 PG 通道）、`KNV_ES_INDEX`（`knv_chunks`）、`KNV_ES_TIMEOUT_S`（2.0）。本地 `deploy/compose.yaml` 把 ES 映射到 `localhost:59200`。

**Redis**：`KNV_REDIS_URL`（默认空 = 关闭）、`KNV_REDIS_TIMEOUT_S`（0.5）、`KNV_WORKING_MEMORY_TTL_S`（24 小时，每次访问续期）。本地映射到 `localhost:56379`。上下文预算：`KNV_CONTEXT_BUDGET_TOKENS`（6000）、`KNV_CONTEXT_KEEP_TURNS`（3，不小于 2）、`KNV_SUMMARY_TIMEOUT_S`（20）、`KNV_SUMMARY_MAX_CHARS`（1500）。

**情景记忆**：`KNV_EPISODIC_MEMORY`（`true`）、`KNV_EPISODE_RECALL_K`（3）、`KNV_EPISODE_MIN_SCORE`（0.5，稠密余弦下限，未标定）、`KNV_EPISODE_CHARS`（600）。

**查询规划**：`KNV_QUERY_PLAN`（`llm` 或 `off`）、`KNV_QUERY_PLAN_TIMEOUT_S`（8）、`KNV_QUERY_PLAN_MIN_CONFIDENCE`（0.6）、`KNV_INTENT_KBS`（意图到知识库的映射，JSON）、`KNV_MAX_TOOL_CALLS_PER_TURN`（4）。

**MCP 与双密钥**：`KNV_MCP_URL`（默认空 = 工具在 API 进程内执行）、`KNV_MCP_HOST`（`127.0.0.1`）、`KNV_MCP_PORT`（58765）、`KNV_MCP_ALLOWED_HOSTS`（Host 头白名单，JSON 列表；容器内如 `["mcp:*"]`）、`KNV_MCP_TIMEOUT_S`（30）、`KNV_TOOL_TOKEN_SECRET`、`KNV_TOOL_TOKEN_TTL_S`（300）。**登录 JWT 与工具 token 使用两把不同的密钥**：`KNV_TOOL_TOKEN_SECRET` 必须与 `KNV_JWT_SECRET` 不同，工具 token 带专用受众，登录 token 无法用于 MCP，反之亦然。

**密钥校验**：`Settings` 构造时强制：`KNV_JWT_SECRET` 与 `KNV_TOOL_TOKEN_SECRET` 在任何环境下都非空白且互不相同；`KNV_ENV` 不是 `dev` 时还拒绝 `dev-only-` 开头的占位值和短于 32 字符的密钥，违反时进程启动失败。

**上传与输入上限**：`KNV_UPLOAD_MAX_BYTES`（20 MiB，正整数）、`KNV_CHAT_MAX_CHARS`（4000 个 Unicode 码点，经 `GET /me` 的 `chat_max_chars` 告知前端）、`KNV_CONFIRM_TTL_MINUTES`（30）。

### LLM 与 thinking 模式

`KNV_LLM_BASE_URL`、`KNV_LLM_API_KEY`、`KNV_LLM_MODEL`（OpenAI 兼容接口）、`KNV_LLM_TIMEOUT_S`（60）。`KNV_LLM_THINKING` 只接受 `provider-default`（默认）或 `disabled`：

- `provider-default` 不发送任何供应商的思考扩展——它**不等于**关闭思考。
- `disabled` 向共享的模型构造器添加 `extra_body={"thinking": {"type": "disabled"}}`，只应用于接受该扩展的服务，如 DeepSeek；其他兼容服务保持默认。
- DeepSeek 默认开启思考，并要求后续工具调用回传推理历史（[官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)）。当前使用的通用 `ChatOpenAI` 适配器不保留 `reasoning_content`，因此 DeepSeek 的工具流程必须设置 `KNV_LLM_THINKING=disabled`；在实现并测试专用适配器之前不提供开启选项。
- 该开关不过滤或改写响应，也不会在被拒绝后自动换模式重试。Web 路径保留一次供应商重试；评测路径零重试并显式限制输出。评测进程读取同一设置并写入预检与最终报告。

### 可选依赖

可选 `ml` 依赖组（`torch`、`FlagEmbedding`）仅在加载 BGE-M3 / 精排时需要；CI 与离线测试用 `uv sync --locked --group dev`，不下载模型。

## 本地启动与数据导入

`GET /api/v1/healthz` 免登录，只执行 `SELECT 1` 检查数据库可达，成功返回 `{"status": "ok"}`；容器演示的 API 健康检查使用它。应用启动是惰性的（不预先构建图、编码器或模型客户端，因此未配置 LLM 时其余路由照常工作）；停机时 `lifespan` 关闭检查点连接池、清理已缓存的图、关闭 Redis 客户端并关闭规划线程池，下一次使用会重新创建。

```bash
docker compose -f deploy/compose.yaml up -d --wait   # 在仓库根目录：PostgreSQL、ES、Redis
cd backend
uv sync --extra ml                                    # 仅离线测试可用 uv sync
uv run alembic upgrade head
KNV_ENCODER=bge-m3 uv run python -m knoventra.db.seed # 默认种子：虚构资料、PDF 与现场 SOP；可重复执行
cp .env.example .env                                  # 填入 KNV_LLM_*
uv run uvicorn knoventra.main:app
```

**现场日志导入**（模板按表头定位列，日志行与技能行可混排；入库前脱敏并去重）：

```bash
uv run python -m knoventra.ingestion.journal ../data/demo/journals/*.xlsx --registry ../data/demo/journals/machines.json --publish-kb field-cases
```

`--registry` 提供序列号到机型的资产表；`--publish-kb` 把每个去重后的案例直接发布到指定知识库；`--skip-if-imported` 在内容未变时跳过（容器演示启动时使用）。

**ES 同步**（需设置 `KNV_ES_URL`）：

```bash
uv run python -m knoventra.search_es.sync rebuild     # 首次或重建索引
uv run python -m knoventra.search_es.sync drain       # 消费 outbox
uv run python -m knoventra.search_es.sync reconcile   # 与 PG 对账补齐
```

**MCP 工具服务**：`uv run python -m knoventra.mcp_server`，监听 `KNV_MCP_HOST:KNV_MCP_PORT`，在 API 的 `.env` 中设置 `KNV_MCP_URL=http://127.0.0.1:58765/mcp` 和 `KNV_TOOL_TOKEN_SECRET` 后，5 个工具（`search_knowledge`、`lookup_skills`、`get_help_request`、`prepare_help_request`、`submit_help_request`）由该服务执行。

**界面检查**（不需要 LLM 密钥）：`KNV_ENCODER=hash KNV_GATE_THRESHOLD=0.6 uv run python dev/mock_server.py` 运行真实应用加脚本化假模型；回答是固定的，只演示界面与流程，不代表回答质量。

## 关键工程契约

以下是各子系统保证的行为与明确不保证的边界。

### 模型输出与工具调用校验

- `finish_reason: length` 的响应以 `MODEL_OUTPUT_TRUNCATED` 结束，含 `invalid_tool_calls` 的响应以 `MODEL_TOOL_CALL_INVALID` 结束；这两种响应里的工具调用都**不执行**（包括同批已解析成功的调用）。最终答案是明确的失败消息，没有来源卡，错误保存在历史里；已取得的检索痕迹保留给评测诊断，`model_finish_reason` 记录最后一次响应的原因。没有自动重试；已确认并提交的转交在之后的模型确认失败时仍有效。
- 能解析的工具调用在**任何一个**执行前，先按公布的 schema 严格校验（类型严格、禁止多余字段）：检索要求非空字符串且不超过 4000 字符，转交摘要非空且不超过 2000 字符，状态查询不接受参数；`lookup_skills` 的 `skill` 非空且不超过 200 字符；`request_help` 有 4 个可选槽位（机台不超过 64、告警不超过 200、现象与已尝试步骤各不超过 500 字符），纯空白字符串在校验前归一为 `None`（视为“未提供”）。未知工具名、空白 / 缺失 / 重复的调用 ID、同一响应里多个转交都会使整批失败。身份不能作为工具参数传入。工具节点再校验一次以防重放旧检查点；已有待确认检查点保留原始操作身份，不追溯重验摘要。
- 检索类回答必须至少含一个已登记的引用：当本轮最近一批工具调用只含 `search_knowledge` 时，没有有效 `[S1]` 式引用的成功回答以 `MODEL_CITATION_MISSING` 结束；该次检索无证据且返回 `NO_ACCESS` / `INSUFFICIENT_EVIDENCE` 时，最终回答为固定拒答，即使模型无视了拒答指令。流式预览可能早于最终校验后的答案出现，`done` 事件里的答案为准。这是引用存在性检查，不是逐句支持度核验。

### 引用与拒答

- **发起求助意图**：`query_plan` 的意图含 `request_help`（用户要发起 / 新建 / 提交求助单，不论是否已说细节；区别于查询进度的 `help_status` 和询问制度的 `it_guide`）。它不是检索意图，规划后直接进入 `agent`，不检索。系统提示词要求此时立即调用 `request_help`（可传 `{}`）；若规划成功、意图为 `request_help`、置信度不低于 `KNV_QUERY_PLAN_MIN_CONFIDENCE`、本轮无进行中草稿，而模型只返回无工具调用的文字，服务端改为发出一次 `request_help({})`，并在 `memory_trace` 记录 `help_fallback`；模型已带槽位调用时不改动。评测器的 `INTENTS` 保持冻结用例使用的 6 个标签，规划器可返回的全部意图是 `VALID_INTENTS`。
- **情景记忆的提问文本**：首问用用户原话；多轮时优先使用规划器的 `rewritten`，否则在上一条助手消息是机型澄清或本轮不超过 20 个字符时写成“上一问（本轮补充）”。用户画像始终只统计本轮的原始消息。

- 证据注册表记录每次检索的来源，`cite_check` 剥离未登记的引用标记；简体 `【无依据】` 与繁体 `【無依據】` 的开头拒答声明都被识别：去掉标记与引用、输出 `INSUFFICIENT_EVIDENCE`、没有来源卡。
- `unambiguous-question-opencc-v1`：回答正文按**当前问题**的字形转换——OpenCC 信号能唯一判定为繁体 / 简体时，才转为相应字形；共用字符、非中文或混合输入保持模型原文。引用别名、故障码、行内 / 围栏代码和 HTTP URL 保持不变；证据、来源卡文本和已存文档保持原字形。每轮只看本轮问题，不从历史推断偏好。这是字形转换，不是翻译。

### 精确编码与机型过滤

- `E17-42`、`SEAL-9X` 这类含数字的显式连字符编号，会按大小写、全角、常见 Unicode 横线规范化后用于查询编码和匹配，原文不改写。召回与 RRF 之后、Top-K 与可选精排之前，候选必须在正文或继承的章节路径中含至少一个所需编号（文档标题不算）；最终证据须**共同覆盖全部**所需编号，且每个都过闸门，否则返回 `INSUFFICIENT_EVIDENCE`（内部原因 `IDENTIFIER_MISMATCH` 或 `LOW_SCORE`）且无证据。
- 带标签的分写形式（如 `E17 设备报代码 42`、`p03故障码11`）会合并为显式编号：型号为 1–8 个 ASCII 字母加 1–4 位数字，标签后恰有两位数字；裸数字、远距离引用、缺型号、小数 / 范围 / 多选和带单位的形式不合并。报告记录策略 `explicit-and-split-v2`。这是有边界的语法，不是通用的自然语言编码解析；资料中出现某编号不证明它支持所问的内容；多编号问题偏保守（一次检索须覆盖本轮用户消息里的全部编号）。
- 机型名（如 `WX-38`）不是必须字面出现的编号：不会把只写相关机型族章节的片段过滤掉（见 DECISIONS 第 78 条）；会话里说明机型后，召回时去掉其他机型的分节。
- 智能体把本轮用户原话与模型生成的检索词一并传给检索，两者的编号集合取并集，重试时同样；`/debug/search` 暴露 `identifiers.required`、`identifiers.missing` 和候选阶段（`fused`、`identifier_match`、`selected`）。

### 会话锁、续跑与补存

- `POST /chat`、`/chat/confirm`、`/chat/retry`、`/chat/recover` 对同一已属主会话获取同一个非阻塞 PostgreSQL 会话级咨询锁，冲突返回 409 `CONVERSATION_BUSY`，发生在 SSE 与消息写入之前；他人会话仍是 404。锁占用一条专用连接直到流结束、断连或取消，清理时先关闭迭代器再解锁；获取 / 释放失败会丢弃该连接。需要直连 PostgreSQL 或会话级连接池（事务级连接池代理无法保持咨询锁），每个活动的 SSE 响应多占一条连接。浏览器断连不保证立刻取消已在线程里执行的模型调用。
- **确认身份**：确认请求在会话锁下与检查点上的实际 interrupt 比对，之后才保存决定；旧确认不能恢复同一线程上的新 interrupt，没有对应 interrupt 的待确认行不能被批准，均返回 409 `CONFIRMATION_NOT_ACTIVE`。已保存的决定优先于不同的重试请求体。操作 ID 含线程 ID、用户消息 ID 和工具调用 ID，并在中断前存入待执行状态，重启后保持一致。
- **续跑 `/chat/retry`**：`GET /conversations/{id}/state` 对“有待执行节点且无 interrupt”的检查点返回 `retry_checkpoint_id`（只读，不跑模型）。`/chat/retry` 接收 `{conversation_id, checkpoint_id}`，校验归属和当前检查点后以 `None` 输入继续图：不新增用户消息、不重置工具状态、不产生新批准。已完成或处于 interrupt 返回 `RUN_NOT_RETRYABLE`，检查点已变化返回 `CHECKPOINT_CHANGED`；对未完成且无 interrupt 的运行发新问题返回 `RUN_INCOMPLETE`。唯一的写工具使用既有操作 ID 与唯一约束，所以“提交成功后节点失败”的重试不会产生第二条记录。用户显式续跑可能再次产生模型费用。
- **补存 `/chat/recover`**：新请求会把服务端生成的用户消息 ID 与用户消息一起保存（迁移 `0005`）；若检查点已完成而该轮最终消息未保存，状态返回 `recover_checkpoint_id`，新问题返回 `ANSWER_NOT_SAVED`。`/chat/recover` 在同一会话锁下核对检查点身份，**不调用图、模型或工具**，直接写入已捕获的最终回答、公开来源卡和错误 / 拒答码，由 `(conversation_id, completion_turn_id)` 唯一约束防止重复；保存失败发出 `ANSWER_SAVE_FAILED`，检查点仍可再次补存。检查点已变化返回 `CHECKPOINT_CHANGED`，已送达、未跟踪的历史轮次或未完成的图返回 `RUN_NOT_RECOVERABLE`。只修复新跟踪的、完整的检查点输出，不重建历史证据。
- 新增有副作用的工具之前，必须自带重试与幂等保证。

### 两阶段确认与幂等提交

- 阶段一：`interrupt` 把确认载荷（槽位、推荐理由等）存入检查点与确认单；用户批准的只是当前正在等待的那一个操作。
- 阶段二：执行端（进程内或 MCP）只接受本人已批准的 `confirmation_id`，用每次运行签发的 token 重新计算权限，按操作 ID 与数据库唯一约束幂等建单；重复提交、刷新或进程重启后恢复都只产生一条记录。取消不产生记录。
- 明确的短中文转交请求（如“这个问题我想转人工”）走确定性路径（`unambiguous-chinese-request-v1`），不先调用模型；摘要保留当前请求与此前的用户陈述，且须在 2000 字符内，超出则回到模型摘要路径；否定、条件、引用、咨询类或含糊的请求仍走模型。评测指标 `escalation_metric_policy=observed-interrupt-and-records-v2` 以检查点上实际观察到的 interrupt 为准：取消用例没有出现确认卡不算通过。

### 文档并发与修订完整性

- 迁移 `0004`：文档身份 `(kb_id, title)` 唯一，并有部分唯一索引保证每个文档至多一个 `PUBLISHED` 修订。已有冲突的历史重复会使迁移失败，不会自动合并。
- 入库先解析、编码，再加锁；拒绝没有可提取文本的文档和结果数与分块数不符的编码输出。同一知识库同名文档的并发上传共享一个文档，修订号在文档行锁下分配；新修订须与原文档语言一致。
- 发布锁定同一文档，等待后刷新修订状态，拒绝已退役或空的草稿；重复发布当前生效修订不改时间戳；发布较旧的已退役修订被拒绝；不同的有效草稿按取得锁的顺序发布（不强制版本号递增）。所有写入在调用方事务内，失败时旧的已提交修订保持生效。`tests/test_revision_integrity.py` 用独立提交的会话和真实 PG 锁等待验证这些行为。

### 输入、上传与解析边界

- **文档解析失败**：空 / 无法读取 / 加密的 PDF、非 UTF-8 Markdown、不支持的扩展名、提取文本含 NUL，均抛出 `DocumentParseError`，上传返回 422 `DOCUMENT_PARSE_INVALID`，说明为固定文案、不暴露本地路径，发生在编码与任何文档 / 修订写入之前。PDF 先读入字节流再解析（避免 Windows 上畸形文件遗留句柄）；不限制解压后内存、页数或解析时间，不做 OCR。
- **元数据预检**：标题须含非空白字符、无 NUL、不超过 255 个字符，原样保存（不 trim、不转大小写、不做 Unicode 归一）；语言限 `zh-Hans`、`zh-Hant`、`en`，已有文档的新修订必须沿用原语言。失败返回 422 `INVALID_DOCUMENT_METADATA`；`GET /admin/documents` 通过 `upload_rules` 公布标题上限与语言。这只校验声明的元数据，不校验文档真实语言。
- **上传大小**：按 64 KiB 分块复制，至多多读一个字节以判断超限；恰好等于上限可通过，超限返回 413 `UPLOAD_TOO_LARGE`（含 `max_bytes`），空文件 422，均发生在加载编码器与创建修订之前。这是单文件上限，不是总请求体、解压后文本、页数或 CPU 时间的上限。
- **聊天问题上限**：新问题须为非空白、无 NUL、不超过 `KNV_CHAT_MAX_CHARS` 个码点，不合法返回 422，发生在创建会话、写消息或推进检查点之前；原样保留（含空白）。上限只约束单个新问题，不是历史、Token 或费用预算；续跑、确认与补存不受影响。
- **文档分块**：分块文本不超过 700 个字符（Python 字符数，不是 Token）；句子边界优先，没有标点的超长段落也会被强制切分，短 ASCII / 全角词和连字符编号在硬边界处整体移入下一块（单个词超过 700 时才会被切开）。页码和章节元数据保留在每块上。只作用于新入库的修订，既有分块与向量不迁移。
- **管理员目录**：`GET /api/v1/admin/documents` 只做两次读取（有序的 KB 名称和文档左连修订摘要，不随文档数增加）；无修订的文档保留空列表，空 KB 仍可选，修订按版本升序，不含分块文本 / 向量 / 修订哈希。它返回完整目录，不是分页；`upload_max_bytes` 与 `upload_rules` 一并返回。
- **会话分页**：`GET /api/v1/conversations/page` 返回 `{items, next_cursor}`，按创建时间与 ID 倒序，`limit` 默认 20（1–100），`before` 为上一页最后的游标，他人或不存在的游标返回 404；`q` 为标题的大小写不敏感字面包含（去首尾空白，最长 128，禁 NUL，`%`、`_` 和转义符 `/` 视为字面字符），游标须属于当前筛选结果。只匹配标题，不查消息内容，不涉及模型。旧的 `GET /conversations` 数组接口保留。
- **转交记录**：`GET /api/v1/escalations?limit=20&before=<id>` 仅返回本人记录，按 `created_at DESC NULLS LAST, id DESC` 的键集分页；迁移 `0006` 为历史行保留“时间未知”，新行用 `clock_timestamp()`。

### 检索输入与向量数值策略

- **编码器输出校验**：入库批次与查询编码都经 `encode_checked`：核对结果数、稠密 / 稀疏成对形状、稠密维度、有限且在 float32 范围内的数值、稀疏整数索引在 `[0, sparse_dim)` 且权重有限非负；拒绝布尔 / 字符串向量值。BGE 适配器独立核对稠密与词法结果数，防止 `zip` 静默丢弃。畸形输出抛 `EncoderOutputError`：入库在创建任何文档 / 修订 / 分块之前整批校验，上传报 503 `ENCODER_OUTPUT_INVALID`；查询在候选 SQL 之前终止，走运行错误路径而不是“无证据拒答”。
- **查询编码批次**（`distinct-batch-v1`）：每次授权检索对去重后的所需文本只做一次带校验的编码调用——稠密用编号规范化后的原字形，稀疏在启用 OpenCC 时用简体化文本；两者相同则复用，不同则按“原文、简体”两项成批；只有稠密时跳过 OpenCC。权限检查始终先于转换与编码，没有跨请求缓存。
- **查询向量数值**（`float32-finite-v1`）：向量以 float32 往返精度序列化；稀疏权重规范到 float32 并去除零项；稠密向量转换后全零时不跑余弦通道，非空的稀疏通道仍可召回；两条通道都无信号则不发候选 SQL。NULL 或非有限的距离在 RRF 之前被排除，`/debug/search` 与检索报告在 `dense_unscorable` / `sparse_unscorable` 中给出被拒绝的已授权 ID；不为补足数量而追加召回。
- **模型加载**：首次构造按进程对编码器和精排器串行化，避免并发冷请求重复构造；推理不串行；进程间仍各自加载。加载后程序化改变配置会抛 `ModelConfigurationError`，需重启；构造失败不缓存、不换用别的模型。

## 测试与验收命令

在 `backend/` 下执行。需要 PostgreSQL（本地 `deploy/compose.yaml`）；ES / Redis 相关测试在 `localhost:59200` / `localhost:56379` 不可达时跳过，`KNV_REQUIRE_ES=1`、`KNV_REQUIRE_REDIS=1` 使其改为失败（CI 如此运行）。

| 命令 | 作用 |
|---|---|
| `uv run pytest` | 全部后端测试，使用每次会话重建的独立 `knoventra_test` 库、Hash 编码器和脚本模型，不需要模型和 LLM；需要 `KNV_TEST_ADMIN_URL`（带建 / 删库权限的管理连接，不要指向生产实例） |
| `uv run --no-sync python dev/check_migrations.py` | 在随机命名的临时库上验证：升到 head、ORM 差异检查、降到 base、再升级、种子重放两次，最后删库；使用 `KNV_TEST_ADMIN_URL`，不碰应用库 |
| `uv run --no-sync python dev/check_demo.py` | 临时库 + 真实 HTTP 子进程 + 两次进程重启，验证权限、来源、版本发布和确认的持久化恢复，不调用真实模型 |
| `uv run --no-sync python dev/check_demo.py --mcp` | 同一套验收，所有工具调用改经独立 MCP 子进程，并与 API 一起重启 |
| `uv run --no-sync python dev/check_compose_demo.py` | 构建并验收六服务容器演示（需要 Docker），范围见[容器演示说明](../docs/container-demo.md) |

范围与限制见[自动化验收](../docs/ci.md)。

## 评测工具链

所有脚本在 `backend/` 下运行，默认不覆盖已有输出文件。留出集（holdout / final 类 split）必须显式 `--confirm-final`，且只能各配置运行一次。含付费模型调用的运行都需要显式授权标志，费用估算是场景估计、不是账单上限。

**检索层 `eval/run_eval.py`**（不调用 LLM）：评测集 `eval/cases.jsonl`（60 例，`build_cases.py` 生成：dev 20 / 留出 40）及 `cases_v2`–`cases_v5` 的回归集；`--split dev|holdout|all|val2|val3|val4|val5`，消融开关 `--no-sparse`、`--no-opencc`、`--rerank`、`--rerank-model`（须与 `--rerank` 同用）、`--threshold`（仅检索模式）。指标取实际 Top-5 在可选精排之后、分数闸门之前；val5 的正例还要求目标分块含字面的 `expect.support_text`。

```bash
KNV_ENCODER=bge-m3 uv run python eval/run_eval.py --split dev
```

**现场检索 `eval/setup_field_eval.py` / `eval/run_field_eval.py`**（v4，不调用 LLM）：`setup_field_eval.py` 删除并重建独立库 `knoventra_eval_v4`、导入虚构日志、发布案例与原始行（供多样性对比）、重建 ES 别名 `knv_eval_v4`；`run_field_eval.py --split dev|holdout [--configs pg es_equal es_weighted] [--confirm-final]` 对 `eval/cases_field_v1.jsonl` 比较三种配置。

**Agent 层 `eval/run_agent_eval.py`**（v4，真实图 + 真实模型）：评测集 `eval/cases_agent_v1.jsonl`（42 例，dev 24 / 冻结留出 18）。`--dry-run` 只打印用例数与模型调用上界，不连接数据库和模型；真实运行必须带 `--allow-llm`，需要 `KNV_LLM_*`，每批付费运行单独获批；留出集需 `--confirm-final`。

```bash
uv run python eval/run_agent_eval.py --split dev --dry-run
```

**旧版 Agent 评测 `run_eval.py --mode agent`**：需要 `KNV_LLM_*`、`--allow-llm` 以及同一币种的 `--input-price-per-million` / `--output-price-per-million`（`--currency`），`--case-id`（可重复）选择用例，`--max-output-tokens`（默认 1024）、`--assumed-input-tokens`（默认 4000，只是估算假设）。拒绝 `--no-sparse`、`--no-opencc`、`--threshold`（Agent 图没有实现这些消融）。使用专用且已播种的评测库：检查点与已提交的转交记录会留在其中。真实运行先写追加式进度日志（默认 `<report>.progress.jsonl`，逐事件 fsync，不可覆盖），失败不发布汇总指标，没有自动续跑。

```bash
uv run python eval/run_eval.py --split dev --mode agent --case-id ops-01 --dry-run --out eval/results/agent-plan.json
```

**离线检查与审核**（不调用模型或数据库，拒绝覆盖已有文件）：

- `eval/inspect_progress.py <progress.jsonl> --out <inspection.json> [--report <report.json>] [--allow-truncated-tail]`：校验进度日志的事件顺序、线程身份与捕获快照，区分已完成 / 进行中 / 未开始的用例，可与成功报告核对；不续跑、不能作为完整评测报告。
- `eval/review_answers.py prepare|summarize`：从含 `review_snapshot` 的报告生成审核包并汇总；人工与模型审核分别统计，未审核内容不计为支持；`eval/render_review_html.py` 生成离线真人审核页。流程、判定口径、抽样与局限见[审核说明](eval/REVIEW.md)。
- `eval/analyze_refusals.py <report> --out <triage.json>`：把拒答分为检索闸门失败、检索后模型拒答和运行错误，筛出证据里含目标原文却仍拒答的“复核候选”（不自动认定为过度拒答）。

**查询改写 / 规划开关消融 `eval/run_field_rewrite_eval.py`**（v4，只调规划器，不跑回答模型）：对 `eval/cases_field_v1.jsonl` 每例只调一次规划器并缓存（绑定用例 SHA-256、split 与提示词 SHA-256，不匹配则拒绝复用），再离线比较 `pg` / `es_weighted` × `off` / `rewrite_only` / `full_plan` 三种查询模式，并给出逐题名次变化、规划器统计与耗时分布。`--dry-run` 只打印用例数、哈希、模型名和调用次数，不连库不调模型；创建缓存必须 `--allow-llm`（每例一次付费规划调用）；`--plans-in <缓存>` 复用已有缓存（0 次模型调用，输出需用新文件名）；留出集需 `--confirm-final`，只运行一次；输出文件独占创建，不覆盖。

```bash
KNV_ENCODER=bge-m3 HF_HUB_OFFLINE=1 uv run python eval/run_field_rewrite_eval.py --split dev --dry-run
KNV_ENCODER=bge-m3 HF_HUB_OFFLINE=1 uv run python eval/run_field_rewrite_eval.py --split dev --plans-in eval/results/field-rewrite-plans-dev.json --out eval/results/new-rewrite-rerun.json
```

**精排对照与阈值标定**：`eval/run_rerank_pair.py --confirm-final --out-dir <新目录>` 在临时库上用同一 28 例各跑一次关 / 开精排，比较配对指纹，需要缓存的模型权重、`KNV_TEST_ADMIN_URL`，不调用 LLM；`eval/calibrate_rerank.py --out <文件>` 在 `eval/cases_threshold_v1.jsonl`（12 标定 + 12 验证）上只用标定半部分选阈值并报告验证误差，不修改服务配置。

```bash
KNV_ENCODER=bge-m3 KNV_RERANK=off HF_HUB_OFFLINE=1 uv run python eval/run_eval.py --split val5 --confirm-final --out eval/results/val5-reproduction-off.json
uv run python eval/run_rerank_pair.py --confirm-final --out-dir eval/results/new-v2m3-comparison
```

### 可选精排

`KNV_RERANK=bge` 启用 `KNV_RERANK_MODEL`（默认 `BAAI/bge-reranker-v2-m3`）与 `KNV_RERANK_THRESHOLD`（默认 0.60，独立于证据闸门，**不是**已标定的生产阈值）；需要 `ml` 依赖和本地模型权重，改后重启 API。精排对 `section_path + text` 打分，使继承自标题的故障码仍可见；查询与召回使用相同的编号规范化；分数须为 `[0, 1]` 内的有限值且与候选一一对应，输出非法则检索失败，而不是回退到稠密相似度；最终候选仍要经过编号与分数闸门。现有测量（单次、小语料）没有得到稳定的净收益，因此默认保持 `off`。结果与局限见[评测报告](../docs/evaluation-report.md)第 13、18 节。

### Agent 层计时指标

新的 Agent 评测报告包含：`diagnostics.retrieval_trace[*].latency_ms`（每次完成的检索，含被访问 / 证据闸门拒绝的，覆盖打开数据库会话、检索与关闭）；`metrics.latency_ms`（单例整轮墙钟，含图调用、脚本化确认 / 续跑与结果装配）；`metrics.retrieval_latency_ms`（逐次检索，一个用例可贡献多个样本）；`metrics.first_answer_chunk_ms`（评测使用 `ChatOpenAI(streaming=True)`，从用例开始到**最后一次不含工具调用的模型响应**的首个非空文本分片，在模型回调处测得，早于引用 / 拒答处理）。各项给出 `n`、`p50`、`p95`、`max`（最近秩，`ceil(p*n)`）；缺失或无效样本不补零，空分布的分位数为 null。报告声明 `config.latency_policy=agent-wall-and-completed-search-v1`、`config.answer_timing_policy=last-model-content-chunk-v1`、`config.llm_streaming=true`；进度检查器只对声明的策略校验，并兼容旧报告。这是服务端 / 评测器时间，不是浏览器延迟，也不含真人确认的等待；脚本模型或 Hash 编码器的耗时不代表真实模型性能，流式新数据不能直接与旧的非流式运行比较。
