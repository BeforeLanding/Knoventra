# Knoventra · 知域

权限感知的企业知识问答助手：员工向自己有权看到的公司资料提问，得到能定位到文档版本和页码的回答；找不到可靠依据时拒答，并可在确认后转交专家。

参考 [Ragent](https://github.com/nageoffer/ragent) 的工程设计，以 **Python + FastAPI + LangGraph + PostgreSQL / pgvector** 重新实现后端，前端为新建的 Vite + React 精简页面。演示使用完全虚构的企业与自编资料，不含任何企业代码、文档、账号或配置。

## 已完成

- **权限感知检索**：区域 / 部门 / 角色授权；权限过滤在召回 SQL 内，权限为空或查询失败时不检索。
- **混合检索**：BGE-M3 稠密 + 稀疏（`sparsevec`）、RRF 融合、OpenCC 简繁归一化（仅用于稀疏侧）；已完成本地 `bge-reranker-base` 和 `v2-m3` 检索对照。v2-m3 在当前小语料未改善目标排名，0.60 闸门还新增 2 例误拒，因此精排默认关闭。
- **精确编码校验**：显式故障码 / 型号，以及“E17 设备报代码 42”等紧邻、带明确标签的两位分写故障码，必须出现在已授权候选的正文或章节中；支持大小写、全角与常见横线归一化，模型改写查询也不能丢掉本轮用户的编码约束。
- **版本管理**：新修订草稿 → 事务内发布，只检索生效版本；同文档并发上传分配独立版本号，并发发布串行化，数据库保证至多一个生效修订；空文档或编码结果缺失时拒绝入库，发布失败时旧版继续生效。
- **Agent**：LangGraph 图，`search_knowledge`、`get_escalation_status`、需确认的 `escalate_to_expert`；每轮最多 4 次工具调用。
- **可核验的引用**：证据注册表 + `cite_check`，伪造的引用标记不渲染；来源预览每次重新鉴权。
- **回答审核流程**：新 Agent 评测保存当轮完整证据快照，可离线逐句标注并验证原文引句；人工与模型审核分别统计，未审核内容不计为支持。
- **拒答诊断**：保存逐次检索结果与模型拒答标记，离线区分编码 / 分数闸门、模型拒答和运行错误，筛出命中目标原文仍拒答的复核候选；不自动认定语义上的过度拒答。
- **延迟统计**：Agent 评测汇总整轮、逐次检索及最终纯文本模型回复的首个内容分片耗时，输出样本数与 P50 / P95 / 最大值；缺失数据不补零，兼容旧评测报告。首分片在模型回调处测量，不等于浏览器延迟或已核验答案到达时间。
- **拒答**：证据闸门 + 模型按【无依据】或【無依據】声明拒答，识别后清空来源卡并标记拒答状态。
- **转交专家**：`interrupt` 人工确认；确认仅驱动当前等待的操作，不同用户轮次独立，旧确认无法恢复新转交；重复提交、刷新、进程重启后恢复只产生一条记录。
- **会话并发保护**：聊天与确认共用 PostgreSQL 会话锁，同一会话的并发请求返回 409，避免多标签页或多个 API worker 同时推进检查点；流结束、断连或取消后清理锁。
- **失败请求恢复**：模型或工具失败后可显式继续未完成检查点，无需重发用户问题；重试绑定当前检查点，保留已存确认决定及转交幂等身份，不绕过待确认操作。
- **完成回答补存**：新请求的检查点已完成但最终消息未保存时，可直接恢复原答案、来源和拒答状态，不再次调用模型；数据库约束避免重复保存。
- **接口**：登录、SSE 聊天、确认、会话回放、来源预览、转交记录、管理员上传与发布、开发环境检索调试。
- **前端**：登录（演示账号切换）、聊天（流式、来源卡与预览、确认卡、拒答态、会话回放）、转交记录（本人列表、分页、刷新）、管理页（上传、修订、发布）。
- **评测**：60 例评测集 + 四组各 28 例的验证 / 回归集、可复现的评测运行器；val5 对照直接度量精排后的候选，目标片段另有原文依据标注。结果见 [评测报告](docs/evaluation-report.md)。

### v4 扩展（开发中，计划见 [v4 计划](docs/knoventra-v4-plan.md)）

- **现场技术日志**：虚构的三地工程师日志与技能矩阵（`data/demo/generate_journals.py` 确定性生成）。模板按表头定位列，日志行和技能行可以混排在同一张表；入库前先脱敏（口令、电话、邮箱），再做精确去重和 SimHash + Jaccard 近似去重（只在同机型内合并），按规则抽取故障字段。去重后的案例可以发布到 `field-cases` 知识库，每个案例一个分块。
- **Elasticsearch 关键词通道**：ES 8.15.3 + IK 分词，拼写纠错表和中英同义词；事务性 outbox 同步，并提供 `drain / reconcile / rebuild` 命令。ES 的每个命中都会回 PG 复查发布状态和读权限；ES 不可用时降级，不放宽权限。
- **编号精确召回**：分块上的 `identifiers` 加 GIN 索引，按编号直接召回，不依赖向量 Top-K。
- **加权 RRF**：按“是否含编号”使用两套权重，`rrf_weighting=equal` 为对照。现有评测集上加权和等权分不出差别，见 [DECISIONS](docs/DECISIONS.md) 第 66–70 条和评测报告第 25 节。
- **查询规划**：`query_plan` 节点用 function calling 产出改写、最多 3 个子查询、意图、置信度和机型，再由确定性代码把关：按纠错表修拼写、补回改写丢掉的编号、丢弃对话里没提到的机型。检索范围收窄到“可读 KB ∩ 意图 KB”，交集为空时不收窄，所以路由只能缩小范围、不会扩大权限；置信度低于 0.6 时检索全部可读 KB。规划失败或超时（8 秒）时回到 v3 的 agent 路径，轨迹记录原因。
- **机型澄清**：证据来自按机型分节的文档、而用户没说机型时，结束本轮并追问，不调用回答模型；因为要先检索才能判断，所以是“检索后、作答前”追问。用户说明机型后，召回时去掉其他机型的分节。
- **技能查询**：`lookup_skills` 查结构化技能表，不走 RAG，仅 Ops 和管理员可用；复用 ES 的纠错表和同义词，只列出“了解”和“掌握”的人。
- **求助单**：旧转交表原地迁移为 `knv_help_request`，原有转交接口不变。状态机由服务端按动作和角色校验，事件流水由数据库触发器保证只追加。槽位跨轮收集（目前存在图状态里，Redis 在第 3 周加入），说“算了”会清空草稿；确认卡列出全部槽位，确认后幂等建单。
- **专家推荐与工作台**：只推荐技能为“掌握”的工程师，按同区域、未结单少、作业次数多排序，推荐理由显示在确认卡上；没有合适人选就进入待派单队列。前端新增“我的求助单”和“专家工作台”两个页面。
- **结案沉淀**：现场求助单解决时，在同一事务里为 `field-cases` 生成脱敏后的案例草稿，由管理员在资料管理页预览后发布；发布后才能被检索。端到端测试覆盖“建单 → 派单 → 解决 → 发布 → 可检索”。
- **短期工作记忆**：Redis（`knv:wm:{user_id}:{thread_id}`，TTL 24 小时）保存求助单槽位草稿和滚动摘要。Redis 只是缓存：草稿的持久记录在 Checkpoint 里（`request_help` 结果的 artifact 和确认卡），Redis 副本带锚点，与 Checkpoint 不符、丢失或 Redis 不可用时都会重建。历史超过 Token 预算（默认 6000，近似计数）时，保留最近 3 轮，更早的轮次压缩成摘要；只压缩发给模型的内容，Checkpoint 保留全文，摘要不读工具输出和证据。见 [DECISIONS](docs/DECISIONS.md) 第 71–72 条。
- **长期记忆**：带引用的回答和已解决的求助单写入按用户隔离的情景记忆（pgvector）。每轮召回最多 3 条，使用前逐条重验：引用的资料必须仍已发布、当前可读，求助单必须仍是本人的且已解决，否则整条丢弃。用户画像只记字形、机型和提交过求助的机台，用于回复字形和“常用机台”提示，不参与权限。上下文组装器按优先级填预算，超出时先丢旧轮次，本轮工具结果不截断。本人可经 `GET /memory`、`DELETE /memory` 查看和删除。评测时关闭长期记忆。见 [DECISIONS](docs/DECISIONS.md) 第 73–75 条。
- **MCP 工具服务**：检索、技能查询、求助单查询、准备和提交求助单五个工具可由独立 MCP 服务执行（`python -m knoventra.mcp_server`，设置 `KNV_MCP_URL` 后启用，不设置时在 API 进程内执行）。身份只来自 API 每次运行签发的短期 token，工具参数里没有身份字段，服务端重算权限；提交只接受本人已批准的确认单，执行端再次鉴权、幂等建单。见 [DECISIONS](docs/DECISIONS.md) 第 76–77 条。
- **前端与交付**：“我的记忆”页可查看和删除自己的记忆；回到原会话时，专家或管理员更新过的求助单会在顶部提示。容器演示（`deploy/compose.demo.yaml`）包含 PG、ES、Redis、MCP、API、Web 六个服务，自动验收会核对 ES、Redis、MCP 确实在用；CI 在 ES、Redis 必须可达的条件下跑后端测试，并让整套 HTTP 验收再经 MCP 跑一遍。
- **尚未完成**：路由准确率、改写开关对比等 Agent 层评测属于付费调用，还没有跑，不报告效果数字。

## 未完成与已知问题

- 评测规模很小：早期真实 LLM 报告使用 12 份虚构资料、21 个生效块；2026-11-09 已在当前 13 份资料、23 个生效块上完成一次真实 dev 20 例及留出集三组本地检索消融。评测集为 60 + 112 例，结果只说明对应语料与用例，不代表通用效果；Recall@5 在小语料下仍饱和。
- 新一次真实 LLM dev 20 例已保存当轮来源快照，来源位置指标 9/10、无依据 3/3 拒答、转交 2/2；Codex 审核全部 10 个可答例，49 个事实单元中 43 个完全支持、3 个部分支持、3 个不支持。**真人审核数仍为 0，不能把 agent 审核率当成人工支持率**。审核流程见[说明](backend/eval/REVIEW.md)，原始报告和边界见[评测报告](docs/evaluation-report.md)第 20 节。
- 编码校验覆盖显式 ASCII 编码和有限的带标签两位分写语法；无标签数字、跨句关联、省略型号、范围 / 多选表达不自动推断，编码存在也不证明资料支持所问内容。首轮dev仍误拒“日常检查”并未跟随字形；修订后4例定向验证的三个可答例命中3/3、该误拒例这次回答正确，但转交例追问而未创建确认。其后新增明确短中文请求的确定性确认路径与真实interrupt评测标记，尚无额外付费验证，不能把小样本写成全面修复；详见评测报告第23、24节。
- Rerank 各配置只做单次小语料对照；另行冻结的 24 例阈值校准 / 验证没有得到可稳定采用的新阈值，仍需更大、外部独立的数据。`base` 与 `v2-m3` 属于不同日期 / 策略的测量，不能直接比较其净效果。OCR、数据驻留、异步入库的原因与扩展方向见[开发方案](docs/knoventra-development-plan.md)第 12 节；长期记忆和 MCP 已在 v4 实现，见上文。
- 端到端只用单个 LLM（deepseek-flash），各报告均为单次运行，没有估计波动。

## 快速开始

只想复现界面与业务流程时，可直接在仓库根目录运行以下命令，无需本机 Python / Node / Java：

```bash
docker compose -f deploy/compose.demo.yaml up --build -d --wait
```

打开 [本机离线演示](http://127.0.0.1:8088)。这套环境使用独立数据库和固定回答，不调用真实模型；首次构建需要网络。数据持久化、停止 / 重置和自动验收见[容器演示说明](docs/container-demo.md)。

下列步骤用于本机开发与真实模型配置：

需要 Docker、[uv](https://docs.astral.sh/uv/)、Node 20+。真实检索需要 GPU 或较强 CPU 加载 BGE-M3（约 2.2GB，首次运行从 HuggingFace 下载）。

```bash
docker compose -f deploy/compose.yaml up -d        # PostgreSQL；另含 es 服务，首次会构建带 IK 插件的 ES 镜像（需联网）
cd backend
uv sync --extra ml
uv run alembic upgrade head
KNV_ENCODER=bge-m3 uv run python -m knoventra.db.seed
cp .env.example .env     # 填入 OpenAI 兼容的 KNV_LLM_BASE_URL / KNV_LLM_API_KEY / KNV_LLM_MODEL
uv run uvicorn knoventra.main:app
```

```bash
cd web && npm install && npm run dev
```

演示账号与资料见 `data/demo/manifest.json`（密码为虚构的演示值）。资料含 12 份 Markdown 和一份两页 PDF；PDF 的生成脚本为 `data/demo/generate_it_badge_pdf.py`，需要 ReportLab、FontTools 和中文 TrueType 字体。已入库的旧演示库可再次执行种子命令加入新资料。不想配置 LLM 时，可用 `backend/dev/mock_server.py` 做界面检查，其回答是固定的，只用于演示流程。

使用 DeepSeek 的工具调用时，在 `backend/.env` 显式设置 `KNV_LLM_THINKING=disabled` 并重启后端。其默认思考模式要求回传推理历史，当前通用适配器不保留该字段；其他服务保持 `provider-default`，不会额外发送供应商扩展。配置与限制见[后端说明](backend/README.md#explicit-provider-thinking-mode)。

启用 ES 关键词通道：在 `backend/.env` 设置 `KNV_ES_URL=http://localhost:59200`，然后执行 `uv run python -m knoventra.search_es.sync rebuild` 建立索引；之后发布修订会自动同步，`reconcile` 用于对账补齐。不设置 `KNV_ES_URL` 时只用 PG 的三个召回通道。

启用 Redis 工作记忆：`docker compose -f deploy/compose.yaml up -d redis`，在 `backend/.env` 设置 `KNV_REDIS_URL=redis://localhost:56379/0`。不设置时槽位草稿每次从 Checkpoint 重建，长对话的摘要每轮重新生成。

启用 MCP 工具服务：在 `backend/` 执行 `uv run python -m knoventra.mcp_server`（默认 `127.0.0.1:58765`），在 `backend/.env` 设置 `KNV_MCP_URL=http://127.0.0.1:58765/mcp`，并给 `KNV_TOOL_TOKEN_SECRET` 设一个与 `KNV_JWT_SECRET` 不同的值（API 和 MCP 服务读同一个 `.env`）。

导入现场日志并发布案例：

```bash
uv run python -m knoventra.ingestion.journal ../data/demo/journals/*.xlsx --registry ../data/demo/journals/machines.json --publish-kb field-cases
```

测试（使用独立的 `knoventra_test` 库和离线哈希编码器，不需要模型和 LLM；ES 相关测试在 `localhost:59200` 不可达时跳过，CI 中设置 `KNV_REQUIRE_ES=1`，不可达即失败；Redis 测试同理，地址 `localhost:56379`，开关 `KNV_REQUIRE_REDIS=1`）：

```bash
cd backend && uv run pytest
```

离线 HTTP 演示与服务重启验收：在 `backend/` 执行 `uv run --no-sync python dev/check_demo.py`。脚本创建并删除独立临时库，验证权限、来源、版本发布和转交确认的持久化恢复，不调用真实模型。配置与范围见[自动化验收](docs/ci.md#http-演示与进程重启验收)。

评测：

```bash
cd backend
KNV_ENCODER=bge-m3 uv run python eval/run_eval.py --split dev                 # 检索层，不调用 LLM
uv run python eval/run_eval.py --split dev --mode agent --case-id ops-01 --dry-run # 离线预检，不调用模型
```

真实 Agent 评测需配置 `KNV_LLM_*`、提供输入 / 输出单价并显式传入 `--allow-llm`。费用为基于输入长度假设的场景估算，不是账单上限；先检查用例范围和预检结果，具体命令见[后端说明](backend/README.md)。

## 文档

- [开发方案 v3.0](docs/knoventra-development-plan.md)
- [关键架构决策](docs/DECISIONS.md)
- [评测报告](docs/evaluation-report.md)
- [回答支持度审核流程](backend/eval/REVIEW.md)
- [20例dev的10个可答例真人审核页](backend/eval/results/dev-agent-stream-nothinking-20261109-review-qa-human.html)
- [4例定向验证的3个可答例真人审核页](backend/eval/results/dev-agent-followup-20261109-review-qa-human.html)
- [演示与会话恢复验证](docs/demo-guide.md)
- [本地浏览器流程录像（脚本模型）](docs/demo/README.md)
- [自动化验收与 CI](docs/ci.md)
- [后端说明](backend/README.md) · [前端说明](web/README.md)

## 上游基线与来源

- 源项目：[nageoffer/ragent](https://github.com/nageoffer/ragent)，基线提交 [`c3b6157`](https://github.com/nageoffer/ragent/commit/c3b6157730ac1f66a312bcd286c47d3ee6aaac42)，许可证 [Apache-2.0](LICENSE)。原项目介绍保存在 [README.upstream.md](README.upstream.md)。
- 仓库保留了上游的 Java 源码与提交历史（Maven 模块、`frontend/`、数据库脚本等），仅作设计参考；**Python 后端（`backend/`）与前端（`web/`）为独立编写，未复制上游代码**。
- 详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
