# 容器化离线演示

这套入口用于复现虚构资料上的权限、引用、版本和转交流程。后端使用 Hash 编码器和固定回答，回答会标注“演示用固定回答”；不加载 BGE、不调用 LLM，不代表真实问答效果。无需本机安装 Python、uv、Node 或 Java，只需已启动的 Docker 与 Compose v2。首次构建需要访问镜像仓库、PyPI 和 npm。

## 启动

在仓库根目录运行：

```bash
docker compose -f deploy/compose.demo.yaml up --build -d --wait
```

打开 [本机演示页面](http://127.0.0.1:8088)。可在登录页选择演示账号；六个账号均使用虚构密码 `demo-pass-1`，完整身份和授权见 `data/demo/manifest.json`。交互步骤见[演示指南](demo-guide.md)。

默认只发布本机 `127.0.0.1:8088`，数据库和 API 不映射宿主端口。占用时可设置 `KNV_DEMO_PORT`，例如 PowerShell：

```powershell
$env:KNV_DEMO_PORT = '8089'
docker compose -f deploy/compose.demo.yaml up --build -d --wait
```

此后访问对应端口。查看状态和日志：

```bash
docker compose -f deploy/compose.demo.yaml ps
docker compose -f deploy/compose.demo.yaml logs --tail 80 api web
```

## 数据与重启

Compose 项目为 `knoventra-demo`，使用自己的 `demo_pgdata` 数据卷，独立于原 `deploy/compose.yaml` 的数据库；不读取 `backend/.env` 或挂载本机源代码、虚拟环境。基础镜像按实测 digest 固定，Python / npm 依赖使用现有锁文件。

数据库健康后，API 启动时先升级迁移并幂等导入种子；失败则不会启动 HTTP 服务。API 使用非 root 用户，前端由 Nginx 提供生产静态资源，并同源代理 `/api/`。SSE 代理关闭响应缓存 / 缓冲，读取超时为 180 秒；API 容器重建后由 Docker DNS 重新解析地址。

会话、确认、转交记录和管理员发布的修订保存在数据卷中。重启会升级结构和补齐缺少的种子文档，不把已发布的 E17 v2 重置回 v1：

```bash
docker compose -f deploy/compose.demo.yaml restart api
```

停止并移除容器，保留数据以便下次启动：

```bash
docker compose -f deploy/compose.demo.yaml down
```

需要从初始演示资料重新开始时，以下命令会删除这套演示的数据卷（包括会话、确认、转交和上传资料），再重新执行启动命令：

```bash
docker compose -f deploy/compose.demo.yaml down --volumes
```

## 自动验收

开发环境可从 `backend/` 运行：

```bash
uv run --no-sync python dev/check_compose_demo.py
```

验收工具生成随机 Compose 项目及空数据卷、分配临时本机端口，从 Dockerfile 构建。通过 Nginx 检查 HTML / JS / CSS、六账号登录、来源 / 权限 / 简繁 / 拒答、版本发布；在待确认时重启 API，再恢复并确认；之后重建 API 容器并重放批准，核验记录始终只有一条。模型与业务断言复用 `check_demo.py`，不使用当前应用数据库。

正常完成或检查失败都会移除本次随机项目的容器、网络和数据卷；构建镜像及缓存保留供复用。强杀脚本或机器断电可能留下资源，启动日志会打印项目名，便于手动核对清理。验收会修改自己的临时演示资料，因此不支持向现有演示地址直接执行。

CI 新增独立 `Container demo acceptance` job。2026-11-16 本地 Docker Desktop / Linux 容器验收通过，基础镜像及锁定依赖从镜像环境安装，未复用宿主虚拟环境或 node_modules；本轮远端 job 尚未运行。该结果覆盖后端 HTTP 与静态资源交付，没有执行浏览器交互、真实模型语义审核或生产部署验证。
