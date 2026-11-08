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
- **拒答**：证据闸门 + 模型按【无依据】或【無依據】声明拒答，识别后清空来源卡并标记拒答状态。
- **转交专家**：`interrupt` 人工确认；确认仅驱动当前等待的操作，不同用户轮次独立，旧确认无法恢复新转交；重复提交、刷新、进程重启后恢复只产生一条记录。
- **会话并发保护**：聊天与确认共用 PostgreSQL 会话锁，同一会话的并发请求返回 409，避免多标签页或多个 API worker 同时推进检查点；流结束、断连或取消后清理锁。
- **失败请求恢复**：模型或工具失败后可显式继续未完成检查点，无需重发用户问题；重试绑定当前检查点，保留已存确认决定及转交幂等身份，不绕过待确认操作。
- **完成回答补存**：新请求的检查点已完成但最终消息未保存时，可直接恢复原答案、来源和拒答状态，不再次调用模型；数据库约束避免重复保存。
- **接口**：登录、SSE 聊天、确认、会话回放、来源预览、转交记录、管理员上传与发布、开发环境检索调试。
- **前端**：登录（演示账号切换）、聊天（流式、来源卡与预览、确认卡、拒答态、会话回放）、转交记录（本人列表、分页、刷新）、管理页（上传、修订、发布）。
- **评测**：60 例评测集 + 四组各 28 例的验证 / 回归集、可复现的评测运行器；val5 对照直接度量精排后的候选，目标片段另有原文依据标注。结果见 [评测报告](docs/evaluation-report.md)。

## 未完成与已知问题

- 评测规模很小：真实 LLM 报告使用 12 份虚构资料、21 个生效块；当前种子另增一份两页 PDF，共 13 份资料、23 个生效块，并已复跑一次 val5 的本地检索对照。评测集为 60 + 112 例，结果只说明对应语料与用例，不代表通用效果；Recall@5 在小语料下饱和，不能区分配置。
- 新的真实 LLM dev 20 例已保存当轮来源快照，并完成 10 个可答例的 Codex 逐句审核；**真人审核数仍为 0，不能把 agent 审核率当成人工支持率**。旧报告缺少快照时不能补造历史依据。审核流程见[说明](backend/eval/REVIEW.md)，单次结果见[评测报告](docs/evaluation-report.md)第 15 节。
- 编码校验覆盖显式 ASCII 编码和有限的带标签两位分写语法；无标签数字、跨句关联、省略型号、范围 / 多选表达不自动推断，编码存在也不证明资料支持所问内容。新 dev 运行发现模型把“日常”误解为“每日”而拒答；运行后已补充提示词约束，尚无新提示词的真实效果测量。
- Rerank 各配置只做单次小语料对照，独立阈值校准尚未做；`base` 与 `v2-m3` 属于不同日期 / 策略的测量，不能直接比较其净效果。OCR、数据驻留、长期记忆、异步入库、MCP 服务化的原因与扩展方向见[开发方案](docs/knoventra-development-plan.md)第 12 节。
- 端到端只用单个 LLM（deepseek-flash），各报告均为单次运行，没有估计波动。

## 快速开始

需要 Docker、[uv](https://docs.astral.sh/uv/)、Node 20+。真实检索需要 GPU 或较强 CPU 加载 BGE-M3（约 2.2GB，首次运行从 HuggingFace 下载）。

```bash
docker compose -f deploy/compose.yaml up -d
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

测试（使用独立的 `knoventra_test` 库和离线哈希编码器，不需要模型和 LLM）：

```bash
cd backend && uv run pytest
```

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
- [演示与会话恢复验证](docs/demo-guide.md)
- [本地浏览器流程录像（脚本模型）](docs/demo/README.md)
- [自动化验收与 CI](docs/ci.md)
- [后端说明](backend/README.md) · [前端说明](web/README.md)

## 上游基线与来源

- 源项目：[nageoffer/ragent](https://github.com/nageoffer/ragent)，基线提交 [`c3b6157`](https://github.com/nageoffer/ragent/commit/c3b6157730ac1f66a312bcd286c47d3ee6aaac42)，许可证 [Apache-2.0](LICENSE)。原项目介绍保存在 [README.upstream.md](README.upstream.md)。
- 仓库保留了上游的 Java 源码与提交历史（Maven 模块、`frontend/`、数据库脚本等），仅作设计参考；**Python 后端（`backend/`）与前端（`web/`）为独立编写，未复制上游代码**。
- 详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
