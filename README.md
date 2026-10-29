# MetricPilot

业务口径驱动的数据分析 Agent，主演示为 BTS Reporting Carrier 航班运行数据。

当前已实施阶段 0—1：可运行工程、Reporting Carrier CSV 导入、固定数据快照、受限只读 SQL、成功查询产物，以及页面中的数据源/快照/表结构查看。知识处理、模型调用和报告生成将在后续阶段实现。实际验收见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)，设计路线见 [蓝图 07](参考/docs/blueprint/07-build-and-evaluate.md)。

## 环境

Python 3.12、uv、Node.js 22+、npm。Python 版本固定为 3.12，依赖由 `uv.lock` 和 `frontend/package-lock.json` 固定。虚拟环境位于根目录 `.venv`，无需手动激活。依赖安装需要网络，模型密钥不是阶段 0 启动的前提。

## Windows PowerShell

从项目根目录执行。默认缓存不可写时，先设置当前终端的项目缓存：

```powershell
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache/uv'
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
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

重新执行 `npm ci` 前，请先停止正在运行的前端开发服务器；Windows 可能锁定正在使用的依赖文件。安装完成后再启动前端。

## Linux / macOS

```bash
export UV_CACHE_DIR="$PWD/.cache/uv"
uv sync --locked
test -f .env || cp .env.example .env
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
- `worker` 与 `analysis` 为 `not_implemented`；当前没有模型分析任务，数据导入与手动 SQL 命令可独立使用。
- 普通错误：`{error: {code, message, details, request_id}}`。健康检查的 503 保留健康 DTO，供页面诊断。

不提交 `.env`、`.venv`、`storage/`、真实原始数据或本机路径。项目的 `.gitignore` 包含这些本地文件；合成数据见 [fixtures/README.md](fixtures/README.md)。

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

浏览器验收需要已迁移且未配置模型的后端在 8000 运行，并已导入下面的合成 fixture（包含两个维表）：

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

下一阶段按 04 / 07 实现知识、Wiki 和指标。RunContext、ToolResult、QueryArtifact 已用于查询服务；MetricDefinition、ReportDraft、Agent Runtime 和 SSE 尚未实现。


## 阶段 1：运行合成演示

先完成 `uv sync --locked` 和 `uv run alembic upgrade head`。从根目录运行 PowerShell：

```powershell
$demo = uv run python -m metric_pilot.data.ingest --input ./fixtures/bts_reporting_synthetic.csv --period 2026-01 --synthetic --carriers ./fixtures/carriers_synthetic.csv --airports ./fixtures/airports_synthetic.csv --activate | ConvertFrom-Json
$demo.id
uv run python -m metric_pilot.data.query --snapshot-id $demo.id --sql 'SELECT carrier_id, COUNT(*) AS scheduled, SUM(cancelled) AS cancelled FROM v_flights GROUP BY carrier_id ORDER BY carrier_id'
```

Linux / macOS 使用相同的 Python 模块参数；从导入输出读取实际 `id`，再作为 `--snapshot-id` 传入，不复制示例 ID。每次导入都会生成新快照；只有显式 `--activate` 才设置默认版本，也可在页面数据源抽屉中选择“设为默认快照”。

打开分析页的“查看数据源”，可看到明确的合成数据标记、真实行数、月份、原文件哈希、质量报告、来源清单及 `v_flights` / 可选维表的列与样本。选择预览不会自动改变默认快照。

CLI JSON 使用 ASCII 转义保存非 ASCII 字符，避免 Windows PowerShell 按本地代码页解码时损坏 JSON；`ConvertFrom-Json` 后中文仍是原文。网页/API 与快照文件保持 UTF-8。

查询输出采用 ToolResult，成功结果包含服务端生成的 `run_id`、`execution_id` 和 QueryArtifact。它是手动 SQL 验证路径，不是自然语言 Agent。其规则引用列表为空，不能将“查询执行成功”称为“业务口径已验证”。

## 导入真实 BTS 数据

官方入口：[Reporting Carrier 下载与字段选择](https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ)、[字段字典](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ)。选择 Reporting Carrier、2026 年 1 月，按蓝图 03 选择全部基线字段。不要使用或混入 Marketing Carrier 数据；程序会拒绝已知 Marketing 字段。

网页交互或下载失败时，手动下载 CSV 后放入忽略的根目录 `data/`；ZIP 需先解压。当前已接入用户从官方页面下载的 `data/T_ONTIME_REPORTING.csv`，实测为 2026-01 的 544,003 条记录，具体验收结果见 IMPLEMENTATION_STATUS.md。

```powershell
uv run python -m metric_pilot.data.ingest --input ./data/T_ONTIME_REPORTING.csv --period 2026-01 --activate
```

`--downloaded-at` 可填真实获取时间，必须带时区；不填则清单记录 null，不捏造下载时间。真实来源是用户声明，尚未做独立真实性核验。基线字段取自 `backend/src/metric_pilot/data/ingest.py` 的 FIELDS；兼容大小写/下划线，以及字段选择页标签与数据库导出列名：FL_DATE、OP_UNIQUE_CARRIER、OP_CARRIER_AIRLINE_ID、OP_CARRIER_FL_NUM、TAIL_NUM、ARR_DELAY_NEW。日期支持 ISO 和 BTS 导出的 `月/日/年 12:00:00 AM`，不按系统地域猜测月日顺序。同一字段出现多个同义列会拒绝，MKT/Marketing 列也会拒绝。缺列会列出缺失项并拒绝导入，不填零。

可选 `--carriers` 和 `--airports` 接收整理后的 UTF-8 CSV，分别为 `carrier_id,name` 与 `airport_id,name` 两列；须由实际代码表整理并人工核对，不由模型猜测名称。程序冻结整理后文件的哈希、检查唯一键和 JOIN 行数。未提供维表时仅显示源数据代码。合成维表只能配合合成数据演示，不可作为真实公司/机场名称依据。

每个成功快照包含：原始 CSV、只在离线导入时可写的 raw_flights、按月暴露的 v_flights、manifest.json、quality.json、schema.json。越月行留在原始文件/raw_flights 并计入质量报告；重复候选保留。未知空值保留为 null，负到达偏差保留，HHMM 不用于直接相减。有限数值的负时长保留原值，通过 negative_duration_counts 和带源行号的示例报告；它们不是提前到达分钟，使用时长作结论前需要处理这些质量问题。

## 查询与证据边界

- 只接受单条受支持的查询 AST；拒绝写入、写 CTE、SELECT INTO、跨 schema/catalog、系统表、表函数、外部文件/网络读取和未审核函数。完整允许集见 `data/guard.py`，不靠 SELECT 前缀检查。
- 只允许当前快照登记的业务表/视图，不能查询 raw_flights。只读连接禁止外部访问和自动扩展安装/加载；固定线程、内存和临时磁盘预算。不支持安全配置时启动失败。
- 参数使用 `--params` 传入命名键到 JSON 标量的对象，SQL 使用 `$name`。PowerShell 的 SQL 应放在单引号中，避免 `$name` 被当作 PowerShell 变量展开。复杂参数可由调用代码直接传给 QueryService。
- 执行使用 spawn 子进程；SQL_TIMEOUT_SECONDS 与 RunContext 总 deadline 都受墙钟约束。CLI 中 Ctrl+C 发出显式取消，真正终止并回收查询子进程。关闭页面不等于取消查询。
- 默认最多保存 1000 行、预览 20 行，结果文件最多 2 MiB；超过行数/字节预算有 truncated 标记，不能将其当作完整统计。单行过大、非有限结果或不支持的嵌套类型会失败。
- Decimal、日期/时间和超出 JavaScript 安全范围的整数保存类型标签；小整数、浮点和 null 保留原义。结果列顺序、实际冻结行顺序和类型一并保存；没有 ORDER BY 不代表业务排名。
- 只有成功执行才保存 QueryArtifact；失败/拒绝/超时/取消只产生审计记录。结果以 SHA-256 内容寻址，读取校验哈希和运行归属，不重新查询当前数据。
- 可用 `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}?offset=0&limit=20` 读取当前运行的有限分页，limit 不超过 100。当前还没有 Agent Run 创建/状态/SSE 接口。

来源与快照 API：`GET /api/v1/sources`、`GET /api/v1/sources/{source_id}/snapshots`、`POST /api/v1/sources/{source_id}/activate`（body 为 snapshot_id）、`GET /api/v1/sources/{source_id}/schema?snapshot_id=...`。前端类型仍由 OpenAPI 生成。

真实 BTS 行数、模型正确率、费用和跨平台执行结果都以实际验证为准。当前真实数据/模型验证限制与具体检查结果见 IMPLEMENTATION_STATUS.md。


## 真实 CSV 的独立验收

导入后可从输出读取实际快照 ID，再运行：

```powershell
uv run python scripts/verify_bts_snapshot.py --input ./data/T_ONTIME_REPORTING.csv --snapshot-id <实际快照ID>
```

请把占位内容替换为实际 ID，不要连同尖括号输入。该脚本独立用 csv/datetime/Decimal 计算行数、日期、null、负时长和逐承运人的统计，再通过正式 QueryService 执行四组受限 SQL：范围覆盖、计划/取消/备降、项目有效到达指标、已报告原因分钟。整数/null 精确比较，浮点结果按容差比较。

成功验收记录保存到忽略的 `storage/validation/`，包含实际数据哈希、快照、运行及四个成功产物 ID。它是人工验收记录，不是 Agent 生成的业务报告，也不证明来源文件的独立真实性或因果关系。

Windows 发布快照的临时目录继承工作区权限；不再用会保留私有 ACL 的 mkdtemp 目录直接发布。慢查询进度条在分析连接中关闭，保持 CLI JSON 管道稳定。
