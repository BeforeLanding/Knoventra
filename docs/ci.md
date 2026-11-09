# 自动化验收

工作流：[`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。在 `main`、`feat/**`、`fix/**` 推送、PR 和手动触发时运行。两个独立 Linux job：`Backend and migrations`、`Web tests and build`。同一分支有新运行时取消旧运行；仓库 token 只有读取权限，checkout 不保留凭证。

## 后端

- Ubuntu 24.04、Python 3.12、uv 0.12.13。
- PostgreSQL 16 + pgvector 服务，仅使用 job 内的演示账号和测试数据库。
- `uv sync --locked --group dev`，不启用可选 `ml` 依赖，不下载 BGE 模型。
- `KNV_ENCODER=hash`、`KNV_RERANK=off`，LLM 配置显式为空、HuggingFace 离线。测试的模型使用脚本替身。
- `uv run --no-sync python dev/check_migrations.py`，然后 `uv run --no-sync pytest -q --tb=short`。

迁移检查脚本从 `KNV_TEST_ADMIN_URL` 连接测试管理库，创建随机命名的 `knoventra_migrations_<uuid>` 库。依次执行：空库升至 head、检查 ORM 结构差异、降至 base、再次升级和检查、导入种子两次并核对主要表行数。最后删除该临时库。脚本不使用应用库执行迁移，不接受任意目标库名；测试管理账号需要创建 / 删除测试库权限。

pytest 继续使用独立 `knoventra_test` 库，每次测试会话重建该库。不要把 `KNV_TEST_ADMIN_URL` 指向不允许创建测试库的生产实例。模型替身避免评测费用；依赖安装本身仍需要访问包仓库。

## 前端

- Ubuntu 24.04、Node 24，使用 `web/package-lock.json`。
- `npm ci` → `npm test` → `npm run build` → `npm run lint`。
- 交互检查使用 jsdom，包括聊天恢复、确认隔离、SSE、转交分页和取消请求，不代表真实浏览器录屏或模型质量评测。
- lint 当前无错误或警告；App 身份加载与 Login 路由处理已消除原有两项警告。下方历史验收记录保留当时结果；错误会使 job 失败。

## 本地复现

从仓库根目录启动演示数据库：

```bash
docker compose -f deploy/compose.yaml up -d --wait
cd backend
uv sync --locked --group dev
uv run --no-sync python dev/check_migrations.py
uv run --no-sync pytest -q --tb=short
```

前端从 `web/` 执行：

```bash
npm ci
npm test
npm run build
npm run lint
```

本轮还在干净的 Linux Python 3.12 / Node 24 容器中执行相同的安装与检查命令，源码副本排除被忽略的 `.env`、现有虚拟环境及 node_modules。静态工作流检查使用 actionlint。容器使用本机独立测试数据库；GitHub 实际 job 使用自己的 PostgreSQL service。

2026-11-07 本地 Linux 验证：后端 213 项、前端 22 项通过，生产构建、迁移链、ORM 差异、种子重放和 actionlint 通过；lint 保留两项既有警告。发现并修复了过期确认测试依赖列表顺序和共享 Session 缓存的不稳定读取，没有放宽有效期 / 归属断言。

2026-11-08 当前分支在 Windows 本地隔离 PostgreSQL 库验证：后端 546 项、前端 145 项通过；空库迁移 / 降级 / 重升、ORM 差异和含 PDF 的种子重放通过；前端生产构建与 lint 通过。此轮没有调用 LLM。该验证不等于新增改动已在 Linux CI 或 GitHub Actions 运行。

2026-11-09 首次远端运行在初始化阶段失败：checkout 与 setup-node 的固定 SHA 被写入了错误的 Action 仓库。通过官方发布标签 API 核对后修复，提交 `9e938b1e` 的[远端运行](https://github.com/BeforeLanding/Knoventra/actions/runs/42579143319)两个 job 均通过：后端 587 项（34.60 秒）、迁移全链 / ORM 差异 / 种子重放通过；前端 145 项、生产构建和零警告 lint 通过。此运行早于后续回答字形改动，不把它当作新改动的远端验证。

随后提交 `60e7277f` 的[远端运行](https://github.com/BeforeLanding/Knoventra/actions/runs/42581321382)也通过：包含终稿字形处理的后端600项（37.65秒），以及迁移验证、前端145项、构建和零警告lint。后续六账号矩阵与明确转交监督路径仍须随各自提交单独验证，不以此运行替代。

该工作流覆盖独立 Python 后端和 `web/`，保留的上游 Java 模块不在此 CI 范围内；Actions 通过不自动设置分支保护。

## 固定工具版本

checkout 使用 v7.0.1 的 `3d3c42e5aac5ba805825da76410c181273ba90b1`，setup-node 使用 v7.1.0 的 `949feb2413d6458794dcd2491c4babbbce0c15c1`；两个 SHA 不能互换。二者官方 action.yml 均使用 Node 24，已核对当前所用输入。setup-uv 使用 v10.3.3 的 `68559e99cc807c1eb7e9238fd43a15bba460eec2`，uv CLI 单独固定为 0.12.13。更新时既核对 SHA 归属与配置，也检查实际远端运行；静态语法检查无法证明固定提交属于正确仓库。

参考：[checkout](https://github.com/actions/checkout)、[setup-node](https://github.com/actions/setup-node)、[setup-uv](https://github.com/astral-sh/setup-uv)、[actionlint 安装说明](https://github.com/rhysd/actionlint/blob/main/docs/install.md)。
