# MetricPilot

业务口径驱动的数据分析 Agent，主演示为 BTS Reporting Carrier 航班运行数据。

当前仅实施阶段 0：Python 包、FastAPI 健康检查、SQLite/Alembic、React 三页框架、配置和错误契约。数据导入、安全 SQL、知识处理、模型调用与报告生成将在后续阶段实现。实际验收见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)，设计路线见 [蓝图 07](参考/docs/blueprint/07-build-and-evaluate.md)。

## 环境

Python 3.12、uv、Node.js 22+、npm。Python 版本固定为 3.12，依赖由 `uv.lock` 和 `frontend/package-lock.json` 固定。虚拟环境位于根目录 `.venv`，无需手动激活。依赖安装需要网络，模型密钥不是阶段 0 启动的前提。

## Windows PowerShell

从项目根目录执行。默认缓存不可写时，先设置当前终端的项目缓存：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache/uv'
uv sync --locked
Copy-Item .env.example .env
uv run alembic upgrade head
uv run uvicorn metric_pilot.main:app --host 127.0.0.1 --port 8000
```

`.env` 已存在时保留现有配置，不重复复制。uv 可自动获取 Python 3.12，已有解释器时可给 `uv sync` 传 `--python`；不把个人绝对路径写入项目配置。另开 PowerShell 终端：

```powershell
Set-Location frontend
npm.cmd ci --cache ../.cache/npm
npm.cmd run dev
```

PowerShell 可能阻止 `npm.ps1`，使用 `npm.cmd` 即可，无需修改系统执行策略。打开 http://127.0.0.1:5173 。Vite 固定该端口，代理 `/api` 到 8000，端口冲突会明确失败。前后端均仅监听回环地址。

## Linux / macOS

```bash
export UV_CACHE_DIR="$PWD/.cache/uv"
uv sync --locked
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn metric_pilot.main:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
cd frontend
npm ci --cache ../.cache/npm
npm run dev
```

Linux / macOS 命令尚需在相应系统实测，本次 Windows 验收不代表跨平台已通过。

## 配置与状态

配置见 `.env.example`。`APP_HOST` / `APP_PORT` 为预留配置，当前 Uvicorn 监听地址由命令行指定。相对 `STORAGE_DIR` 以命令工作目录为基准，迁移与启动均在根目录执行。

- 健康检查：`GET /api/v1/health`；接口文档：`/docs`。
- 未迁移返回 HTTP 503 / `migration_required`；迁移完成且 FTS5 可用返回 HTTP 200。
- 健康检查实际检测 SQLite 与 FTS5，不发起模型请求。
- 模型缺失或仍为占位值：`not_configured`；完整配置：`configured_unverified`，不代表连通性已验证。
- `worker` 与 `analysis` 为 `not_implemented`，阶段 0 不启动占位 worker。
- 普通错误：`{error: {code, message, details, request_id}}`。健康检查的 503 保留健康 DTO，供页面诊断。

不提交 `.env`、`.venv`、`storage/`、真实原始数据或本机路径。目录尚未初始化 Git，已建立 `.gitignore`。合成数据见 [fixtures/README.md](fixtures/README.md)。

## 检查与契约同步

```powershell
uv run pytest
uv run ruff check .
uv run alembic check
uv run python scripts/export_openapi.py
Set-Location frontend
npm.cmd run generate:api
npm.cmd test
npm.cmd run build
```

DTO 变更后重新导出 OpenAPI 和生成前端类型，一并提交 `frontend/openapi.json` 和 `frontend/src/api/schema.d.ts`。前端另有运行时校验，错误代理响应不会被当成成功。

浏览器验收需要已迁移且未配置模型的后端在 8000 运行：

```powershell
Set-Location frontend
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '../.cache/ms-playwright'
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

Linux 对应使用 `npx playwright install chromium` 和 `npm run test:e2e`，还需提供浏览器系统依赖。

## 目录与后续入口

- `backend/src/metric_pilot/`：配置、契约、数据库和 API。
- `backend/migrations/`：版本化迁移，服务不自动建表。
- `frontend/`：React + TypeScript + Vite + Ant Design 三页框架。
- `scripts/export_openapi.py`：契约导出。
- `fixtures/`：明确标记的合成数据。
- `参考/docs/blueprint/`：设计契约与 DB-GPT 参考快照。

下一阶段按 03 / 07 实现导入、固定快照、SQL AST 防护和子进程执行器。RunContext、ToolResult、QueryArtifact、MetricDefinition、ReportDraft、SSE 在对应阶段按蓝图落地，当前尚未实现。
