# Knoventra · 知域

面向企业内网的 Agentic RAG 知识助手，参考 [Ragent](https://github.com/nageoffer/ragent) 的工程设计，计划使用 **Python + FastAPI + LangChain + LangGraph** 重建后端，复用 React / TypeScript 前端，提供制度问答、部门知识检索与业务流程指引。

> 当前状态：已完成 Java 上游源码导入与 Python 版开发方案 v2.0。仓库尚无 Python 后端实现；下述架构和业务能力均为待开发内容。

## 开发方案

完整设计见 [Knoventra Python 版二次开发方案](docs/knoventra-development-plan.md)，包含迁移边界、FastAPI 分层、LangGraph 状态与恢复、RAG、记忆、ACL、数据迁移、API / SSE 兼容、Worker、P0–P5 实施与验收。

首版重点：

- 将员工手册、IT 指引和公共公告组织为部门频道与知识库。
- 让知识检索、引用预览、历史上下文和工具调用遵守同一权限边界。
- 通过知识检索与模拟事务工具，演示“查制度—理解要求—查询进度—确认操作”的流程。
- 使用自编的虚构企业资料与合成数据，提供可以独立部署的演示环境。

## 上游基线

- 源项目：[nageoffer/ragent](https://github.com/nageoffer/ragent)。
- 导入分支：`main`。
- 基线提交：[`c3b6157730ac1f66a312bcd286c47d3ee6aaac42`](https://github.com/nageoffer/ragent/commit/c3b6157730ac1f66a312bcd286c47d3ee6aaac42)，提交日期为 2026-10-25。
- 本次导入保留上游 Git 提交历史，许可证为 [Apache-2.0](LICENSE)。
- 原项目介绍与启动资料保存在 [README.upstream.md](README.upstream.md)。

当前代码保留上游 Maven 模块、Java 包名、数据库脚本和配置，仅作为来源与功能参考。上游为 Java 17 / Spring Boot 4.1.0 / AgentScope 2.0.2；Python 目标后端将独立实现运行时、认证、任务、检索和记忆，Java Agent 状态不直接迁入 LangGraph。

Python 目标存储为 PostgreSQL / pgvector、独立 Checkpoint 数据库、Redis 和 S3 兼容存储，后台任务采用 Celery，关键词检索阶段加入 Elasticsearch。具体版本与可运行性由 P0 实测和锁定，不承诺现有 Java 配置可直接用于 Python。

## 本地开发

Python 后端目录、锁文件、Alembic 迁移、Compose 与演示种子均待创建，当前还没有 Python 启动命令。

如需运行 Java 参考版本，可查看 [上游启动说明](https://nageoffer.com/ragent/local-dev/) 与 [上游数据库说明](resources/database/README.md)。Python 新环境将使用独立迁移与数据库，不混跑上游 SQL 或复用企业数据。

仅向本地测试库或个人演示环境导入数据。公开演示使用虚构人物、自编制度和个人资源，不依赖任何企业账户、内部接口或真实员工资料。

## 来源与修改说明

当前仓库导入的 Agent 引擎、RAG、知识入库、会话和后台来自 Ragent；Python 版将以其设计和可观察行为为参考。本项目保留上游源码历史、许可证和原作者 / 贡献者署名。

最近一次更新将开发方案切换为 Python / LangChain / LangGraph 路线并同步本页，未修改业务实现。详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
