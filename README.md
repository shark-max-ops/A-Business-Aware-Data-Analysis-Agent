# A Business-Aware Data Analysis Agent

一个围绕业务目标持续调查数据的分析助手。你用自然语言提出问题，系统查阅业务资料、探索数据、执行受限只读 SQL，并根据实际结果继续核查，生成能追溯数字依据的报告。目前以 BTS Reporting Carrier 航班运行数据为演示场景，在本机以单用户方式运行。

例如：

> 本月航班运行中，有哪些问题值得优先核查？继续检查异常日期的天气和运行公告，并说明目前证据能支持什么、还有什么无法确定。

系统的工作流程是：

**提出目标 → 查资料与数据 → 根据结果继续核查 → 必要时查外部来源 → 整理发现和限制 → 生成可追溯报告 → 继续追问。**

## 可以做什么

- **自然语言分析**：查看字段、日期范围、缺失和分布，按航空公司、机场、航线或日期查询与比较。模型选择工具，后端校验并执行查询。
- **持续调查**：保存目标、范围、发现和未解决问题；在同一调查中继续追问，并按归属、成功状态和版本校验复用旧证据。
- **业务知识管理**：上传 Markdown/TXT 资料，检索原文和来源定位，查看引用关系，生成和编辑 Wiki 草稿；人工审核后发布 Wiki 或启用指标定义。
- **外部核查**：查询已登记机场的天气观测，检索 FAA ATCSCC 公告并读取正文，保存来源原件和固定版本。
- **可追溯报告**：展示关键数字、表格、基础图表、解释与限制；点击数字或图表数据点查看查询和来源依据，支持报告历史版本及 Markdown/JSON 导出。
- **执行过程查看**：展示当前行动、工具结果和失败信息，按需展开 SQL 与技术记录；支持显式取消，刷新或关闭页面不会自动取消后台运行。

页面分为“分析”“知识”和“报告”。分析页的数据源抽屉可以查看已登记快照、字段、样本和质量报告，并显式选择默认数据版本。

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
uv run python -m metric_pilot.data.ingest --input ./fixtures/bts_reporting_synthetic.csv --period 2026-01 --synthetic --carriers ./fixtures/carriers_synthetic.csv --airports ./fixtures/airports_synthetic.csv --activate
```

合成数据只适合演示与测试，不是官方航班数据，不能用于真实业务结论。

分析真实航班数据时，从 [BTS Reporting Carrier 下载页](https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ) 获取 2026 年 1 月 CSV，解压后放入本地 `data/`，例如：

```powershell
uv run python -m metric_pilot.data.ingest --input ./data/bts/2026-01.csv --period 2026-01 --activate
```

月度原件统一放在 `data/bts/YYYY-MM.csv`，已有一月为 `2026-01.csv`；后续下载二月、三月分别保存为 `2026-02.csv`、`2026-03.csv`。下载时的 29 字段勾选清单见 [BTS 下载说明](参考/docs/blueprint/03-data-and-sql.md#bts-download)。当前命令仍只导入单月，多个文件就位不代表已能跨月分析，不要将多月拼成一个文件传给旧入口。字段要求和数据口径见 [数据接入说明](参考/docs/blueprint/03-data-and-sql.md)。缺少基线字段会明确拒绝，不会填零补齐。可选 `--carriers`、`--airports` 需提供经过核对的真实代码表；不要将合成维表用于真实数据。每次成功导入创建新快照，`--activate` 将其显式设为默认版本。

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

1. 打开“分析”，确认当前数据快照；没有数据时先按上文导入。
2. 创建新调查，输入目标，例如“查看本月数据范围与缺失情况，找出值得进一步核查的异常”。合成样本较小，适合先问日期范围、记录数和缺失情况。
3. 查看当前行动与结果，按需打开执行详情或报告依据。涉及正式指标时，先到“知识”核对并审核适用定义。
4. 在同一调查中继续追问。需要切换数据、知识版本或调查范围时，显式新建调查或分支。

原始数据、来源材料、模型密钥和 `storage/` 中的本地数据库与证据不随源码分发；新使用者需要自行准备数据和配置。保留旧报告依据时，应一并保留它引用的数据快照和冻结产物。

进一步阅读：[业务资料与指标](knowledge_seed/README.md)、[设计文档](参考/docs/blueprint/README.md)、[代码讲解](docs/walkthroughs/README.md)、[实现与验证记录](IMPLEMENTATION_STATUS.md)。
