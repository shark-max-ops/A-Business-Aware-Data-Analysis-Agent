# A Business-Aware Data Analysis Agent

一个围绕业务目标持续调查数据的分析助手。你用自然语言提出问题，系统查阅业务资料、探索数据、执行受限只读 SQL，并根据实际结果继续核查，生成能追溯数字依据的报告。目前以 BTS Reporting Carrier 航班运行数据为演示场景，在本机以单用户方式运行。

参与开发：[项目目录与文件关系](项目目录说明.md) · [开发说明](开发说明.md) · [修改与验证日志](IMPLEMENTATION_STATUS.md)。

例如：

> 本月航班运行中，有哪些问题值得优先核查？继续检查异常日期的天气和运行公告，并说明目前证据能支持什么、还有什么无法确定。

系统的工作流程是：

**提出目标 → 查资料与数据 → 根据结果继续核查 → 必要时查外部来源 → 整理发现和限制 → 生成可追溯报告 → 继续追问。**

试用时若报告标记“部分完成”，表示已保留成功核查结果，仍有解释或候选发现未通过校验。同一批证据的重复分条记录会提前收尾；调查额度耗尽时需显式新建分支，不能通过失败或重启重置。数字可追溯不表示自由文字中的排名或因果判断正确，仍需复核。

新报告把服务端核对的排名／同列关系与自由解释分开展示：前者基于完整冻结结果计算，并可查看并列、缺失和取值依据；后者默认折叠在“未核实解释”，不能当成已核对结论。模型不能直接填写名次或验证状态。该核对只覆盖此查询结果集，不自动证明SQL业务分母或因果。

## 可以做什么

- **自然语言分析**：查看字段、日期范围、缺失和分布，按航空公司、机场、航线或日期查询与比较。模型选择工具，后端校验并执行查询。
- **持续调查**：保存目标、范围、发现和未解决问题；在同一调查中继续追问，并按归属、成功状态和版本校验复用旧证据。
- **业务知识管理**：在“文档管理”中直接撰写或导入 Markdown/TXT，阅读参考资料与计算规则；在 Wiki 圆形关系图中探索引用，点击节点后从图下打开原文。规则和 Wiki 仍需人工审核。
- **外部核查**：查询已登记机场的天气观测，检索 FAA ATCSCC 公告并读取正文，保存来源原件和固定版本。
- **可追溯报告**：展示关键数字、表格、基础图表、解释与限制；点击数字或图表数据点查看查询和来源依据，支持报告历史版本及 Markdown/JSON 导出。
- **执行过程查看**：展示当前行动、工具结果和失败信息，按需展开 SQL 与技术记录；支持显式取消，刷新或关闭页面不会自动取消后台运行。

界面使用统一侧栏与工作区：分析会话在左侧独立滚动，同一调查的各轮问题、结果和发现连续展示，输入框持续可见。顶部“调查信息”“调查发现”保留汇总，每轮也保留自己的发现与下一步建议。顶部“工作区数据源”可查看快照、字段、样本和质量，并显式选择默认数据版本；每轮的固定数据入口只读查看该轮快照。知识管理收拢为“文档管理”和 Wiki，历史报告从侧栏进入。

## 使用边界

当前航班数据入口是 BTS Reporting Carrier 月度 CSV，不支持把任意业务表上传后直接分析。Reporting Carrier 与 Marketing Carrier 不能混用。

正式项目指标须经过人工审核；初始指标包导入后仍为草稿。指标不可用时，可以明确声明范围、算法和缺失处理，开展独立数据探索，但不能把探索结果当作已审核指标。

天气仅开放已登记的 ATL、ORD、JFK 及已核对的 2026 年范围；公告按 ATCSCC UTC 签发日和设施查阅。来源受网络、覆盖、预算和使用许可限制，失败或缺资料会如实展示。天气同现、公告预计影响和已上报原因均不能自动证明因果关系。

报告数字从成功查询的冻结结果中取出，数据与知识更新不会改变旧报告。引用有效不等于模型解释一定正确，比较范围、分母和结论仍需人工复核。已验证所列 Windows 工程案例和部分真实数据、模型路径，尚不能据此声称整体准确率。

## 环境准备

- Python 3.12 和 uv。
- Node.js 22+ 和 npm。
- 支持原生工具调用的 Chat Completions 兼容文本模型服务，用于自然语言分析；生成 Wiki 还需要 JSON 输出能力。
- 可选 embedding 服务，用于知识向量索引和混合检索。

Python 和前端依赖分别由 `uv.lock`、`frontend/package-lock.json` 固定。首次安装需要网络；虚拟环境、依赖目录和缓存由安装命令生成。

## 启动

以下命令从项目根目录执行。Windows PowerShell 使用 `npm.cmd`，避免系统执行策略拦截 `npm.ps1`。

### 1. 安装后端依赖并配置模型

```powershell
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache/uv'
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，填写自己的模型配置：

```dotenv
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=replace-me
LLM_MODEL=your-tool-capable-model

# 需要向量索引和混合检索时填写
EMBEDDING_BASE_URL=https://your-provider.example/v1
EMBEDDING_API_KEY=replace-me
EMBEDDING_MODEL=your-embedding-model
```

以上地址、密钥和模型名都是占位值。完整配置见 [.env.example](.env.example)。服务不支持 `reasoning_effort` 时，将 `AGENT_REASONING_EFFORT`、`WIKI_REASONING_EFFORT` 留空。保留已有 `.env`，不要用示例覆盖真实配置，也不要提交密钥。

未配置模型仍可启动页面和查看本地数据，但不能完成自然语言分析。健康检查只核对本地依赖与配置，不会发起模型请求，也不代表远程服务已经连通。

### 2. 初始化数据库并准备数据

```powershell
uv run alembic upgrade head
```

如果已经有本地运行数据，可以保留现有 `storage/` 并跳过重复导入。全新目录可先导入仓库附带的合成数据，查看页面、字段和基本查询流程：

```powershell
uv run python -m metric_pilot.data.ingest --input ./fixtures/synthetic_flight_records.csv --period 2026-01 --synthetic --carriers ./fixtures/synthetic_airline_lookup.csv --airports ./fixtures/synthetic_airport_lookup.csv --activate
```

合成数据只适合演示与测试，不是官方航班数据，不能用于真实业务结论。

分析真实航班数据时，从 [BTS Reporting Carrier 下载页](https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ) 获取 2026 年 1 月 CSV，解压后放入本地 `data/`，例如：

```powershell
uv run python -m metric_pilot.data.ingest --input ./data/bts/2026-01.csv --period 2026-01 --activate
```

月度原件统一放在 `data/bts/YYYY-MM.csv`，已有一月为 `2026-01.csv`；后续下载二月、三月分别保存为 `2026-02.csv`、`2026-03.csv`。下载时的 29 字段勾选清单见 [BTS 下载说明](docs/design/03-data-and-sql.md#bts-download)。上方命令只导入单月；多月使用下方独立入口，多个文件就位不代表已登记快照，不要将多月拼成一个文件传给旧入口。字段要求和数据口径见 [数据接入说明](docs/design/03-data-and-sql.md)。缺少基线字段会明确拒绝，不会填零补齐。可选 `--carriers`、`--airports` 需提供经过核对的真实代码表；不要将合成维表用于真实数据。每次成功导入创建新快照，`--activate` 将其显式设为默认版本。

阶段 6.A 已提供一至四月原件的离线核验入口。它只生成来源与质量清单，不导入或激活数据；输出目录必须尚不存在，复跑时换新目录：

```powershell
uv run python -m metric_pilot.data.audit --input-dir data/bts --periods 2026-01 2026-02 2026-03 2026-04 --output-dir storage/validation/bts-6a/my-audit
```

结果为 `manifest.json` 和 `summary.md`；退出码 0 表示本次技术检查无阻断，2 表示输入/结构/日期等有阻断，不代表来源已认证或质量无异常。下载时间未知就留空；只有用户明确确认这些文件的表口径与地理选项时，才追加 `--user-confirmed-reporting-all`，登记用户声明而非独立认证。详细检查、独立复核命令及 6.B 入口见 [数据准备说明](docs/design/03-data-and-sql.md#多月-bts-数据准备阶段-6a-已实现)。

阶段 6.B 已提供不可变多月快照导入，建议在专用终端使用隔离存储（该终端后续命令均使用此存储）：

```powershell
$env:STORAGE_DIR = Join-Path (Get-Location) 'storage/dev-multimonth'
uv run alembic upgrade head
uv run python -m metric_pilot.data.multimonth --input-dir data/bts --periods 2026-01 2026-02 2026-03 2026-04 --audit-manifest storage/validation/bts-6a/20261108-confirmed/manifest.json --timeout-seconds 600
```

清单路径请替换为本机实际核验结果。命令要求月份连续、原件哈希与清单一致；全部成功后登记新快照，不设为默认。Ctrl+C 会取消实际导入。6.C/6.D 已接通日期范围的后端调查、页面选择与报告；6.E已完成有限真实月度/分组续查/缩小范围验收，复杂语义失败仍保留。6.B结构需要迁移至`0008_multimonth`，已处于该版本的工作区无需额外6.C/6.D迁移；范围字段存入既有JSON，不改写旧记录。

新调查的API可提交 `date_range: {start, end_exclusive}`，开始日包含、结束日不包含；多月快照必须明确选择，续查继承原范围，改变范围需新建调查或分支。正式指标须覆盖整个范围且已人工审核，一月定义不会自动扩期。详细契约和验收入口见 [固定日期范围与跨期口径](docs/design/03-data-and-sql.md#6c固定日期范围与跨期口径)。

页面试用：在分析页选择已登记的单月或多月快照，核对并选择“开始日期（含）”“结束日期（不含）”，再发送问题或通过“新调查”固定目标与范围。例如一至三月选择 `2026-01-01` 至 `2026-04-01`。创建后日期固定；续查沿用原范围，修改时用“新调查”或“新建分支”。缺月、越界或空范围不能提交；取消创建弹窗不会改变当前调查。数据源抽屉展示逐月实际记录日期、全版本质量、来源清单和原件哈希；预览不会切换默认，点击“设为默认快照”只影响后续新调查的初始选择。旧调查和旧报告不重算。真实多月验收数据仍在隔离存储，页面只显示当前后端存储已登记的快照；启动后端须使用与导入相同的 `STORAGE_DIR`，不要期待主工作区自动出现隔离版本。

6.E已按所列有限案例完成第六阶段：真实月度总览、OH航司续查及一月按天分支有独立CSV核对；旧复杂分组拒绝案例保留；阶段7已完成所列复杂分组真实复核与引用防护，结果仍需人工判断，下一步阶段8。仅本机6.E隔离库批准一至四月项目取消率，主库旧草稿与默认版本不变。按记录重启6.E服务后可访问 **http://127.0.0.1:5176**；固定报告、拒绝案例、上下文48000的实测配置及重启命令见本机[验收记录](storage/validation/bts-6e/acceptance-20261109/ACCEPTANCE.md)。该忽略目录不随Git分发，新环境需按前述流程准备自己的数据和审核，不能依赖本机记录。

需要完整的初始业务资料时，先按 [知识材料说明](knowledge_seed/README.md) 准备本地原件及整理材料，再运行：

```powershell
uv run python -m metric_pilot.knowledge.seed
```

该命令校验材料路径和哈希，缺文件或哈希不符会失败，不会生成占位原文。导入不会自动批准指标或发布 Wiki；向量索引可在知识页按需构建。也可以先启动页面，再上传自己的 Markdown/TXT 业务说明。

### 3. 启动后端

保持在项目根目录，运行：

```powershell
uv run uvicorn metric_pilot.main:app --host 127.0.0.1 --port 8000
```

### 4. 启动前端

另开终端，从项目根目录进入前端：

```powershell
Set-Location frontend
npm.cmd ci --cache ../.cache/npm
npm.cmd run dev
```

打开 **http://127.0.0.1:5173**。前端将 `/api` 请求代理到本机 8000 端口；前后端均仅监听回环地址。接口文档位于 http://127.0.0.1:8000/docs，健康检查位于 http://127.0.0.1:8000/api/v1/health。

后续启动只需分别运行后端和前端启动命令。更新依赖或清理依赖目录后重新安装；更新代码后执行数据库迁移。Windows 下重新运行 `npm ci` 前，应先停止正在使用依赖的前端服务。

### Linux / macOS

命令尚未在这两个系统实测。安装、迁移和准备数据的模块及参数相同，环境准备可改为：

```bash
export UV_CACHE_DIR="$PWD/.cache/uv"
uv sync --locked
test -f .env || cp .env.example .env
# 编辑 .env 后执行迁移，并按上文准备数据
uv run alembic upgrade head
uv run uvicorn metric_pilot.main:app --host 127.0.0.1 --port 8000
```

另开终端执行：

```bash
cd frontend
npm ci --cache ../.cache/npm
npm run dev
```

## 开始使用

1. 打开“分析”，从“工作区数据源”确认默认数据快照；没有数据时先按上文导入。
2. 创建新调查，输入目标，例如“查看本月数据范围与缺失情况，找出值得进一步核查的异常”。合成样本较小，适合先问日期范围、记录数和缺失情况。
3. 查看当前行动、结果与“本轮发现”；按需展开“本轮信息”“本轮数据与依据”，打开执行详情或报告依据。逐轮固定数据只读查看，发现被后轮修正后仍保留在原轮次，并可定位后续修正。
4. 通过顶部“调查信息”“调查发现”查看当前调查的范围、累计分配额度和发现汇总。涉及正式指标时，先到“知识”核对并审核适用定义。
5. 在同一调查中继续追问。点击建议问题只填入输入框，已有不同草稿时确认替换，仍需点击发送。需要切换数据、知识版本或调查范围时，显式新建调查或分支。

较早轮次可分批显示，“定位轮次”只滚动到对应回答，不改变发送目标。草稿按调查保存在页面内存中，刷新后不保留；页面连续展示历史不代表模型自动获得完整历史回答，续查仍受既有上下文和证据规则限制。

原始数据、来源材料、模型密钥和 `storage/` 中的本地数据库与证据不随源码分发；新使用者需要自行准备数据和配置。保留旧报告依据时，应一并保留它引用的数据快照和冻结产物。

进一步阅读：[业务资料与指标](knowledge_seed/README.md)、[设计文档](docs/design/README.md)、[代码讲解](docs/walkthroughs/README.md)、[实现与验证记录](IMPLEMENTATION_STATUS.md)。

阶段7的分组身份、范围与真实验收路线见[代码讲解](docs/walkthroughs/stage-7-explanations.md)；本机完整成功/拒绝、配置与复现命令见[阶段7验收记录](storage/validation/stage7/acceptance-20261114/ACCEPTANCE.md)。该目录被Git忽略，模型仍可能有自由文字语义错误，不代表整体准确率。
