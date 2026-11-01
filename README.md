# Knoventra · 知域

权限感知的企业知识问答助手：员工向自己有权看到的公司资料提问，得到能定位到文档版本和页码的回答；找不到可靠依据时明确拒答，并可在确认后转交专家。

参考 [Ragent](https://github.com/nageoffer/ragent) 的工程设计，计划以 **Python + FastAPI + LangGraph + PostgreSQL / pgvector** 重新实现后端。

> 当前状态：已完成 Java 上游源码导入与 MVP 开发方案 v3.0。仓库尚无 Python 实现；下述能力均为待开发内容。

## 开发方案

完整设计见 [Knoventra 开发方案 v3.0](docs/knoventra-development-plan.md)：三周 MVP，涵盖演示故事、架构、检索子图、数据模型、API、评测与里程碑。

MVP 重点：

- **权限在检索时生效**：按区域与部门授权，未授权内容不进入候选、精排、模型上下文或来源预览。
- **可核验的引用**：回答引用到文档修订与页码；新版本发布后旧版本不再被检索；伪造的引用标识不渲染为来源。
- **混合检索与简繁体**：BGE-M3 稠密 + 稀疏双通道、RRF 融合、Rerank、OpenCC 归一化，覆盖故障码等精确词与繁简交叉提问。
- **拒答与转交**：证据不足时拒答；转交专家经 LangGraph `interrupt` 人工确认，幂等写入。
- **可复现评测**：60 例评测集（20 调参 / 40 留出），三组消融对照，越权泄露与重复写入为 0 作为硬性验收。

演示使用完全虚构的跨区域企业与自编资料，不包含任何企业代码、文档、账号或配置。

## 上游基线

- 源项目：[nageoffer/ragent](https://github.com/nageoffer/ragent)。
- 导入分支：`main`。
- 基线提交：[`c3b6157730ac1f66a312bcd286c47d3ee6aaac42`](https://github.com/nageoffer/ragent/commit/c3b6157730ac1f66a312bcd286c47d3ee6aaac42)，提交日期为 2026-10-25。
- 本次导入保留上游 Git 提交历史，许可证为 [Apache-2.0](LICENSE)。
- 原项目介绍与启动资料保存在 [README.upstream.md](README.upstream.md)。

当前代码保留上游 Maven 模块、Java 包名、数据库脚本和配置，仅作为设计参考。上游为 Java 17 / Spring Boot 4.1.0 / AgentScope 2.0.2；Python 实现独立编写运行时、认证、检索与 Agent 图，不依赖 Java 服务。

## 本地开发

Python 后端、前端、Compose 与演示数据均待创建，当前还没有 Python 启动命令。

如需运行 Java 参考版本，可查看 [上游启动说明](https://nageoffer.com/ragent/local-dev/) 与 [上游数据库说明](resources/database/README.md)。

## 来源与修改说明

当前仓库导入的 Agent 引擎、RAG、知识入库、会话和后台来自 Ragent；Python 实现以其设计为参考，移植的代码会保留归属。本项目保留上游源码历史、许可证和原作者 / 贡献者署名。详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
