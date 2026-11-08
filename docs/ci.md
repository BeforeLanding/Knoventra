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

工作流须随代码推送后才会在 GitHub 上运行；本地容器通过不等于远端 Actions 已通过，也不自动设置分支保护。该工作流覆盖独立 Python 后端和 `web/`，保留的上游 Java 模块不在此 CI 范围内。

## 固定工具版本

checkout 和 setup-node 使用经核实的 v7 提交 SHA；setup-uv 使用 v10.3.3 提交 SHA，uv CLI 单独固定为 0.12.13。更新 Action 时一起核对官方配置和兼容条件，再跑静态与 Linux 验证。

参考：[checkout](https://github.com/actions/checkout)、[setup-node](https://github.com/actions/setup-node)、[setup-uv](https://github.com/astral-sh/setup-uv)、[actionlint 安装说明](https://github.com/rhysd/actionlint/blob/main/docs/install.md)。
