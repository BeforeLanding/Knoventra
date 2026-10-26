# Knoventra · 知域

面向企业内网的 Agentic RAG 知识助手，基于 [Ragent](https://github.com/nageoffer/ragent) 二次开发，计划围绕制度问答、部门知识检索和业务流程指引，扩展权限感知检索、多语言问答与模拟事务工具。

> 当前状态：已完成上游源码导入与二次开发方案设计。Knoventra 的业务扩展尚未实现；下述产品方向不代表当前已经具备全部功能。

## 开发方案

完整设计见 [Knoventra 二次开发方案](docs/knoventra-development-plan.md)，包含业务边界、源码改造点、Agent 与 RAG 链路、记忆策略、权限模型、数据结构、接口、交互、实施阶段和验收标准。

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

当前代码继续保留上游 Maven 模块、Java 包名、数据库脚本和运行配置。已核对的依赖包括 Java 17、Spring Boot 4.1.0、AgentScope 2.0.2，以及 React 18 / TypeScript / Vite 前端；实际可运行性仍需完成本地构建与依赖验证。

## 本地开发

当前阶段沿用 [上游启动说明](https://nageoffer.com/ragent/local-dev/) 和 [数据库初始化说明](resources/database/README.md)。Knoventra 专用的本地 Compose、演示种子和配置模板列在开发方案 P0 阶段，尚未提供。

仅向本地测试库或个人演示环境导入数据。公开演示使用虚构人物、自编制度和个人资源，不依赖任何企业账户、内部接口或真实员工资料。

## 来源与修改说明

Knoventra 的 Agent 引擎、RAG 管线、知识入库、会话和管理后台等基础能力来自 Ragent；本项目对原作者与贡献者保留署名。

本次修改范围为项目入口文档、上游说明归档和 Knoventra 二次开发方案，未修改业务实现。详细来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
