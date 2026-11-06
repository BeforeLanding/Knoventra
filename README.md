# Knoventra · 知域

权限感知的企业知识问答助手：员工向自己有权看到的公司资料提问，得到能定位到文档版本和页码的回答；找不到可靠依据时拒答，并可在确认后转交专家。

参考 [Ragent](https://github.com/nageoffer/ragent) 的工程设计，以 **Python + FastAPI + LangGraph + PostgreSQL / pgvector** 重新实现后端，前端为新建的 Vite + React 精简页面。演示使用完全虚构的企业与自编资料，不含任何企业代码、文档、账号或配置。

## 已完成

- **权限感知检索**：区域 / 部门 / 角色授权；权限过滤在召回 SQL 内，权限为空或查询失败时不检索。
- **混合检索**：BGE-M3 稠密 + 稀疏（`sparsevec`）、RRF 融合、OpenCC 简繁归一化（仅用于稀疏侧）；可选 Rerank 已用本地 `bge-reranker-base` 做检索对照，默认关闭，计划中的 `v2-m3` 尚未实测。
- **精确编码校验**：显式故障码 / 型号必须出现在已授权候选的正文或章节中；支持大小写、全角与常见横线归一化，模型改写查询也不能丢掉本轮用户的编码约束。
- **版本管理**：新修订草稿 → 事务内发布，只检索生效版本；同文档并发上传分配独立版本号，并发发布串行化，数据库保证至多一个生效修订；空文档或编码结果缺失时拒绝入库，发布失败时旧版继续生效。
- **Agent**：LangGraph 图，`search_knowledge`、`get_escalation_status`、需确认的 `escalate_to_expert`；每轮最多 4 次工具调用。
- **可核验的引用**：证据注册表 + `cite_check`，伪造的引用标记不渲染；来源预览每次重新鉴权。
- **回答审核流程**：新 Agent 评测保存当轮完整证据快照，可离线逐句标注并验证原文引句；人工与模型审核分别统计，未审核内容不计为支持。
- **拒答诊断**：保存逐次检索结果与模型拒答标记，离线区分编码 / 分数闸门、模型拒答和运行错误，筛出命中目标原文仍拒答的复核候选；不自动认定语义上的过度拒答。
- **拒答**：证据闸门 + 模型按【无依据】或【無依據】声明拒答，识别后清空来源卡并标记拒答状态。
- **转交专家**：`interrupt` 人工确认；重复提交、刷新、进程重启后恢复只产生一条记录。
- **会话并发保护**：聊天与确认共用 PostgreSQL 会话锁，同一会话的并发请求返回 409，避免多标签页或多个 API worker 同时推进检查点；流结束、断连或取消后清理锁。
- **接口**：登录、SSE 聊天、确认、会话回放、来源预览、转交记录、管理员上传与发布、开发环境检索调试。
- **前端**：登录（演示账号切换）、聊天（流式、来源卡与预览、确认卡、拒答态、会话回放）、管理页（上传、修订、发布）。
- **评测**：60 例评测集 + 四组各 28 例的验证 / 回归集、可复现的评测运行器；val5 对照直接度量精排后的候选，目标片段另有原文依据标注。结果见 [评测报告](docs/evaluation-report.md)。

## 未完成与已知问题

- 评测规模很小（12 份虚构资料，最新运行有 21 个生效块；评测集 60 + 112 例），结果只说明这些用例，不代表通用效果；Recall@5 在此规模下饱和，不能区分配置。
- 历史引用正确率主要检查来源是否指向期望文档、页码、修订，**仍没有真实 LLM 回答的人工支持度抽查结果**。本轮已完成[快照与审核工具](backend/eval/REVIEW.md)，旧报告缺少快照时不能补造历史依据。
- 编码校验只覆盖含连字符的显式 ASCII 编码（如 `E17-42`、`SEAL-9X`）；“E17 设备代码 43”等分开写法、编码存在但资料不支持的问题仍依赖模型拒答。此前端到端验证中模型对 4/16 个可答问题过度拒答，本轮没有改提示词或重跑 LLM。
- 本地 `bge-reranker-base` 对照仅改善 1 例排序，不足以支持默认启用；`bge-reranker-v2-m3` 实测、精排阈值校准尚未做。OCR、数据驻留、长期记忆、异步入库、MCP 服务化的原因与扩展方向见[开发方案](docs/knoventra-development-plan.md)第 12 节。
- 只用单个模型（deepseek-flash）、单次运行，没有估计波动。

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

演示账号与资料见 `data/demo/manifest.json`（密码为虚构的演示值）。不想配置 LLM 时，可用 `backend/dev/mock_server.py` 做界面检查，其回答是固定的，只用于演示流程。

测试（使用独立的 `knoventra_test` 库和离线哈希编码器，不需要模型和 LLM）：

```bash
cd backend && uv run pytest
```

评测：

```bash
cd backend
KNV_ENCODER=bge-m3 uv run python eval/run_eval.py --split dev                 # 检索层，不调用 LLM
KNV_ENCODER=bge-m3 uv run python eval/run_eval.py --split dev --mode agent    # 端到端，调用 LLM 并产生费用
```

## 文档

- [开发方案 v3.0](docs/knoventra-development-plan.md)
- [关键架构决策](docs/DECISIONS.md)
- [评测报告](docs/evaluation-report.md)
- [回答支持度审核流程](backend/eval/REVIEW.md)
- [演示与会话恢复验证](docs/demo-guide.md)
- [后端说明](backend/README.md) · [前端说明](web/README.md)

## 上游基线与来源

- 源项目：[nageoffer/ragent](https://github.com/nageoffer/ragent)，基线提交 [`c3b6157`](https://github.com/nageoffer/ragent/commit/c3b6157730ac1f66a312bcd286c47d3ee6aaac42)，许可证 [Apache-2.0](LICENSE)。原项目介绍保存在 [README.upstream.md](README.upstream.md)。
- 仓库保留了上游的 Java 源码与提交历史（Maven 模块、`frontend/`、数据库脚本等），仅作设计参考；**Python 后端（`backend/`）与前端（`web/`）为独立编写，未复制上游代码**。
- 详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
