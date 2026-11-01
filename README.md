# Knoventra · 知域

权限感知的企业知识问答助手：员工向自己有权看到的公司资料提问，得到能定位到文档版本和页码的回答；找不到可靠依据时拒答，并可在确认后转交专家。

参考 [Ragent](https://github.com/nageoffer/ragent) 的工程设计，以 **Python + FastAPI + LangGraph + PostgreSQL / pgvector** 重新实现后端，前端为新建的 Vite + React 精简页面。演示使用完全虚构的企业与自编资料，不含任何企业代码、文档、账号或配置。

## 已完成

- **权限感知检索**：区域 / 部门 / 角色授权；权限过滤在召回 SQL 内，权限为空或查询失败时不检索。
- **混合检索**：BGE-M3 稠密 + 稀疏（`sparsevec`）、RRF 融合、OpenCC 简繁归一化（仅用于稀疏侧）；Rerank 接口已实现但默认关闭，**尚未实测**。
- **版本管理**：新修订草稿 → 事务内发布，只检索生效版本；发布失败时旧版继续生效。
- **Agent**：LangGraph 图，`search_knowledge`、`get_escalation_status`、需确认的 `escalate_to_expert`；每轮最多 4 次工具调用。
- **可核验的引用**：证据注册表 + `cite_check`，伪造的引用标记不渲染；来源预览每次重新鉴权。
- **拒答**：证据闸门 + 模型按【无依据】声明拒答，拒答时不展示来源卡。
- **转交专家**：`interrupt` 人工确认；重复提交、刷新、进程重启后恢复只产生一条记录。
- **接口**：登录、SSE 聊天、确认、会话回放、来源预览、转交记录、管理员上传与发布、开发环境检索调试。
- **前端**：登录（演示账号切换）、聊天（流式、来源卡与预览、确认卡、拒答态、会话回放）、管理页（上传、修订、发布）。
- **评测**：60 例评测集 + 两组验证集、可复现的评测运行器，结果见 [评测报告](docs/evaluation-report.md)。

## 未完成与已知问题

- 评测规模很小（12 份虚构资料约 35 个块，评测集 60 + 56 例），结果只说明这些用例，不代表通用效果；Recall@5 在此规模下饱和，不能区分配置。
- 引用正确率只检查来源是否指向期望文档、页码、修订，**没有做“回答是否被来源支持”的人工抽查**。
- 证据闸门无法拒绝与真实编码相近的编码（如 E17-43 与 E17-42），这类问题依赖模型拒答；在最近的验证集上模型对 4/16 个可答问题过度拒答。
- 未做 Rerank 实测、OCR、数据驻留、长期记忆、异步入库、MCP 服务化（原因与扩展方向见[开发方案](docs/knoventra-development-plan.md)第 12 节）。
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
- [后端说明](backend/README.md) · [前端说明](web/README.md)

## 上游基线与来源

- 源项目：[nageoffer/ragent](https://github.com/nageoffer/ragent)，基线提交 [`c3b6157`](https://github.com/nageoffer/ragent/commit/c3b6157730ac1f66a312bcd286c47d3ee6aaac42)，许可证 [Apache-2.0](LICENSE)。原项目介绍保存在 [README.upstream.md](README.upstream.md)。
- 仓库保留了上游的 Java 源码与提交历史（Maven 模块、`frontend/`、数据库脚本等），仅作设计参考；**Python 后端（`backend/`）与前端（`web/`）为独立编写，未复制上游代码**。
- 详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
