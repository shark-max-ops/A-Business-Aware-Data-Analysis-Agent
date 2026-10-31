# A Business-Aware Data Analysis Agent

一个业务口径驱动的数据分析助手。你用自然语言提问，系统查阅业务资料和指标定义，探索数据结构，执行受限只读 SQL，再生成能追溯数字依据的分析报告。目前使用 BTS Reporting Carrier 航班运行数据作为主演示场景。

## 项目要解决什么问题

例如用户问：“这个月哪家航空公司的延误率最高？为什么？”

系统的目标流程是：

**理解问题 → 查指标定义和资料 → 查看数据结构 → 执行安全 SQL → 生成报告 → 点击数字查看依据。**

项目同时关注“数能否查出来”“计算口径是否明确”和“结论依据能否核对”。解释必须受现有字段和来源支持，不能将相关关系或已报告原因分钟构成直接当成因果证明。

## 第二阶段的作用

仅有航班记录，还不足以决定“延误率”怎么算：延误多少分钟算延误？取消和备降是否进入分母？缺失到达信息如何处理？采用的是官方定义还是项目自定义规则？

阶段 2 建立可查阅、可审核、可版本化的业务知识层，为阶段 3 的 Agent 提供依据。

| 内容 | 作用 |
| --- | --- |
| 原始资料 | 保存字段含义、官方说明、来源和适用期，保留原文版本与位置 |
| Wiki | 把资料整理成方便阅读和搜索的解释，引用回原始资料 |
| 结构化指标定义 | 明确公式、分子、分母、过滤条件、空值处理、来源和版本，经审核后供分析使用 |

Wiki 用于理解和导航；已审核的指标定义固定计算口径。生成的 Wiki 不能自动成为正式规则。规则更新创建新版本，后续运行绑定固定知识版本，已冻结的依据不应被新规则覆盖。

阶段 2 完成后，系统具备可检索的资料和计算规则；阶段 3 已接入模型驱动的工具选择、SQL 生成与修复。阶段 2 的详细契约见 [蓝图 04](参考/docs/blueprint/04-knowledge-and-metrics.md)。

## 各阶段如何衔接

| 阶段 | 主要作用 | 当前状态 |
| --- | --- | --- |
| 0：工程基础 | 环境、数据库迁移、接口契约和三个页面框架 | Windows 本机验收完成 |
| 1：数据与执行 | 真实 CSV 导入、固定快照、质量报告、安全 SQL 和查询证据 | Windows 真实数据与合成数据验收完成 |
| 2：知识与规则 | 文档版本、检索、Wiki 与指标审核管理 | 功能已实现，Windows 验收见实施状态 |
| 3：分析 Agent | 模型根据问题查规则、选工具、生成和修正 SQL，展示真实执行进度 | 已实现；Windows 验收见实施状态 |
| 4：调查与可追溯报告 | 根据证据持续调查；报告提供中文图表、数字取证、历史修订与导出 | 报告已完成 Windows 与真实模型验收；持续调查为新增待实现目标 |
| 5：外部来源与调查工具 | 接入历史观测、资料检索与受限原文读取，固定外部证据 | 尚未实施 |
| 6：用户导入 | 确认字段、质量与业务材料，创建快照并完成调查 | 尚未实施 |
| 7：评估与交付 | 人工核对调查结果、对照评估、失败分析与可复现演示 | 尚未实施 |

当前可以导入和查询真实数据，在页面查看快照、字段、质量报告，并管理版本化知识、Wiki 和指标。自然语言分析已接入真实模型与安全工具，可生成固定数字与依据的中文报告。实际验收见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)，实施路线见 [蓝图 07](参考/docs/blueprint/07-build-and-evaluate.md)。

后续将围绕用户的业务目标开展持续调查：根据查询结果决定下一步，检查可能的反证和限制，并支持用户围绕发现继续追问；再扩展到用户导入自己的数据和业务资料。这些调查与通用导入能力目前还没有实现。

阶段 2 的七份材料、来源清单及八个指标草稿见 [知识材料入口](knowledge_seed/README.md)。本机已经导入真实文档修订和八个指标草稿；指标仍待用户业务审核，没有自动批准。官方原件与整理后的第三方材料存放在本机 `data/`，未提交 Git。代码表已核对覆盖当前数据的全部 ID，但没有改写既有航班快照。

Python 模块路径为 `metric_pilot`，下面的安装与启动命令按这个路径执行。

## 各阶段代码讲解

[阶段代码导读](docs/walkthroughs/README.md) 按阶段 0—4 分别说明阶段目标、执行流程、关键代码文件、具体例子和验证边界。阶段 5—7 尚未实施，目录中明确标注；设计取舍仍保存在 `docs/decisions/`，实际验收结果见 `IMPLEMENTATION_STATUS.md`。

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

原有浏览器回归需要已迁移的后端在 8000 运行，并已导入下面的合成 fixture（包含两个维表）。模型标签根据真实配置检查，不会触发模型请求：

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

RunContext、ToolResult、QueryArtifact 已用于查询服务，MetricDefinition 用于知识管理。Agent Runtime 与 SSE 已在阶段 3 接入；阶段 4 已实现 ReportDraft、ValueRef 取值、固定报告与数字取证。


## 自然语言分析与报告（阶段 3—4）

先运行 `uv run alembic upgrade head`，重启后端，再打开 http://127.0.0.1:5173/analysis 。已有 `.env` 保留，不复制覆盖；模型需要支持原生工具调用。健康检查仅检查本地配置，不发付费请求。

会话列表固定单行：长名默认省略，鼠标悬停或键盘聚焦时横向滚动；减少动态效果偏好下不滚动。新分析在原模型最终回复中顺带生成短标题，失败或缺失时保留完整问题作为名称，原问题正文始终保留。

1. 选择已登记的数据快照，输入问题，例如“查看本月数据的日期范围和记录数”。模型按问题需要查资料、看结构，再提出并执行受限 SQL。
2. 问题下方显示真实执行摘要，完成后自动收起；点击箭头展开简短步骤，失败尝试会保留。点击“执行详情”按需打开右侧抽屉，查看 SQL、参数、用时、错误、来源与固定版本；原始事件默认折叠。查询结果来自冻结产物。新分析默认生成中文报告预览，点击“打开完整报告”阅读；历史查询结果仍可分页查看。
3. 涉及项目指标时，只使用固定知识快照中已审核且适用于整月的定义。当前八项实际指标仍待人工审核；分析 Agent 没有审核或发布工具。未审核时返回需要补充的条件。
4. 刷新页面或断网后会从服务端恢复运行与事件；关闭页面不会停止分析。“停止分析”通过显式接口取消实际模型请求和查询子进程。
5. 需要补充条件时本次运行正常结束，补充回复会创建新运行。同会话、同数据快照的已完成上文可作为背景；旧查询产物不能直接成为新运行的数字依据。

运行默认最多 16 轮、24 次工具调用和 180 秒，总限制与上下文/输出预算在创建时固定；SQL 的时间和行数限制也随运行固定。状态包括排队、运行、完成、失败、取消和服务中断。重启不冒称从模型调用中间恢复。

阶段 4 的模型提交报告布局与 ValueRef，服务端从本次成功查询冻结结果取值，校验后原子保存报告并完成运行；clarification 表示需要补充条件，历史 query_result 仍兼容。执行与引用有效不自动证明自由 SQL 的业务语义。当前 Agent 使用固定快照关键词检索；知识页继续支持既有混合检索。

在“报告”页查看历史报告，点击关键数字、表格单元格或图表数据点打开依据抽屉。正文包含关键数字、表格、柱状图/折线图、解释与限制；技术记录默认收起，图表使用系统中文字体。图表仅将服务端冻结数值映射成坐标，前端不重新计算指标。报告读取不查询当前数据；更新资料、换数据快照或修订报告不会覆盖旧版。

“修改说明”可调整标题、范围与限制，保存为新版本；历史版本可切换，旧版保留。支持 Markdown/JSON 导出，暂不提供 PDF。ReportDraft API 还支持在同一冻结证据集合上修订布局和说明，拒绝并发覆盖；不能借修订添加另一运行的产物。

百分比约定为 SQL 返回 0—100 的 *_pct 列，格式化只添加百分号，不再次乘百。null 显示“无可用值”；截断结果不能成为报告依据，须重新查询；折线图要求横轴唯一、升序且查询有 ORDER BY。单份报告最多 8 个产物、20 个块；单张图最多 60 行与 3 个系列。当前验证级别为“引用取值有效”，没有实现可执行指标模板的语义证明。

阶段 4 真实模型验收：运行 uv run python scripts/verify_reports.py，会请求本机已配置的文本模型，默认检查范围报告、逐日折线图、利润缺数据和未审核指标四例。

它直接读取原始 CSV 计算独立基线，核对报告值与冻结查询产物。结果保存于忽略的 storage/validation/stage4-reports.json，失败尝试另有时间戳记录；只复核已有四例可用 --reuse-record，不会再次请求文本模型。具体已测范围与限制见实施记录。

阶段 3 历史验收脚本保留如下（按阶段 3 的查询结果契约设计，不作为阶段 4 报告验收入口）：


```powershell
uv run python scripts/verify_agent.py
```

验收通过真实 HTTP 创建运行，核对真实快照的行数/日期范围、未审核指标处理及利润缺数据提示。`--reuse-record` 只重新读取已有运行、事件和冻结结果核对，不再次调用模型。结果与历史副本保存在忽略的 `storage/validation/stage3-agent*.json`；失败运行也保留在数据库中。它不是问答准确率评估，不自动批准指标或发布 Wiki。

隔离浏览器测试：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache/ms-playwright'
npm.cmd run test:analysis --prefix frontend
```

启动器使用独立数据库、合成数据和位于 `backend/tests/` 的 FakeLLM，仍运行真实工具与查询子进程。正式配置没有测试模型开关。测试端口为 8002 / 5175；Windows 启动命令已验证，Linux/macOS 尚未实测。实现边界见 [阶段 3 决策](docs/decisions/0004-agent-runtime.md)。

## 阶段 2：使用知识管理

先更新依赖与迁移，再启动服务；已有运行中的旧后端需要重启以启动知识 worker。

```powershell
uv sync --locked
uv run alembic upgrade head
uv run python -m metric_pilot.knowledge.seed
```

最后一条从 `knowledge_seed/manifest.json` 校验并导入本机准备的七份材料，保留旧版本并绑定八个指标草稿的真实来源。缺少官方本地文件时会拒绝，不用占位资料冒充原件。重复导入不会覆盖已经编辑的指标。

打开 http://127.0.0.1:5173/knowledge ：

1. 原始文档：上传 UTF-8 Markdown/TXT（最大 500 KB）、选择新建或更新，查看原文/历史/定位片段。
2. 检索：输入“航班早到了，为什么还有负的延误分钟？”，先用关键词模式。官方 ArrDelay 解释优先展示，ArrDelayMinutes 可作对照。结果先显示最相关的五个主题和逐字短摘录，长文、更多结果按需展开；表格直接渲染。检索范围固定，资料更新后点“更新知识快照”。
3. 向量：显式点击“构建/更新向量索引”，任务完成后切换“关键词 + 向量”；缺索引或未配置不会显示假成功。
4. Wiki：默认查看“关系图”，点击资料、引用主题和指标节点，再打开原文或对应指标；“阅读解释”显示正文和编号引用。图包含明确标记的草稿，连线表示实际引用与来源。需要修改时再打开“编辑草稿”；也可新建人工草稿或从所选文档生成草稿。每段正文保留引用标记，核对后发布；回滚创建新草稿。
5. 指标：先阅读公式中的分子/分母、统计范围、缺失处理、适用期和来源，再按需展开字段细节或“编辑规则”。保存仍是草稿，人工审核后才可在新知识快照中解析。

embedding 与文本生成使用 `.env` 中的配置；模型请求只由明确的索引/检索/生成操作触发，健康检查不计费。API 根地址或完整 `/embeddings` 端点均可配置。Wiki 模型需要支持 JSON 输出；`WIKI_REASONING_EFFORT=none` 在本机服务实测通过，不支持该字段的服务可以留空。只保存最终候选和数值 token 用量，不记录内部思维正文。

真实材料检查：

```powershell
uv run python scripts/verify_knowledge.py
# 以下命令会请求真实 embedding；没有匹配的成功 Wiki 草稿时会排队生成。
uv run python scripts/verify_knowledge.py --live-models
# 回归两种“提前到达”问法；只检索，不生成 Wiki 或审核规则。
uv run python scripts/verify_knowledge_reading.py
# 同时检查真实 query embedding 与混合检索，会请求已配置的 embedding 服务。
uv run python scripts/verify_knowledge_reading.py --live-embedding
```

记录分别在忽略的 `storage/validation/stage2-knowledge-local.json`、`stage2-knowledge.json` 和 `stage2-reading-feedback.json`。向量按模型/端点和文本哈希持久化，重复构建复用匹配项；Wiki 验收会复用来源一致的成功模型草稿，并明确记录是否复用。来源校验不自动证明语义正确，没有预填回答准确率或费用。

知识浏览器验收使用隔离数据库，不修改实际项目指标。Windows 可在单独终端从根目录启动 `uv run python scripts/browser_knowledge_server.py`（8001），再运行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache/ms-playwright'
npm.cmd run test:knowledge --prefix frontend
```

测试配置会在 5174 启动专用前端并代理到 8001，不请求远程模型。详细操作见 [分析使用说明](knowledge_seed/project/07-analysis-guide.md)，设计边界见 [阶段 2 决策](docs/decisions/0003-knowledge-and-metrics.md)。Linux/macOS 的浏览器命令及系统依赖仍需对应系统实测。


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
- 可用 `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}?offset=0&limit=20` 读取当前运行的有限分页，limit 不超过 100。Agent Run 创建、状态、显式取消和 SSE 接口已在阶段 3 接入。

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
