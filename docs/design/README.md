# 业务口径驱动的数据分析 Agent：个人项目实施蓝图

编写日期：2026-10-27。项目名称为 **A Business-Aware Data Analysis Agent**，主演示数据为美国 BTS 航班运行数据。内部 Python 包名为 `metric_pilot`。

这套文档保留设计与实际契约，实施以根目录 IMPLEMENTATION_STATUS.md 和代码为准。阶段 4 与 5.A—5.D 已完成 Windows 本机开发及所列工程案例验收，阶段 5 可供用户试用；不代表用户已完成 5.D 试用、整体语义准确率或跨平台验证。后续阶段 6 为多月 BTS 数据与跨月调查，阶段 7 为工具使用条件与能力扩展，阶段 8 为整体评估交付，均未实施；通用 CSV 导入移出当前计划。早期自动检查通过但语义错误的报告保留并在验收记录标为拒绝，不改写旧报告。详见实施记录中相关事项的最新日期条目。天气限登记站点/年份，公告限 ATCSCC UTC 签发日和设施，来源许可边界保留。

## 1. 项目解决什么问题

用户带来的是需要调查的业务目标：如何界定问题、选择比较、检查异常是否可靠、继续追问，以及说明仍不知道什么，都需要业务口径和证据支持。

本项目完成一条完整路径：

```text
业务目标 → 确定范围与关键口径 → 制定可调整的调查安排
  → 查询整体情况 → 依据真实结果决定进一步核查
  → 检查样本、分母、缺失与可能的反证
  → 整理发现、依据、限制与下一步 → 用户继续调查或查看报告
```

当前以航班运行分析为落地场景，完善业务知识约束下的持续调查与可追溯报告。执行与证据底座保持可复用，但本轮不追求任意业务表导入；调查由真实结果驱动，不按固定题目映射 SQL。

## 2. 完成版范围

| 能力 | 完成标准 |
| --- | --- |
| 单 Agent 执行框架 | 原生工具调用、统一工具契约、运行状态、步数和时间预算、错误反馈、SSE 事件、可回放运行记录 |
| 业务知识与 Wiki | 文档版本、分块、混合检索、来源引用、Wiki 草稿生成、人工编辑和修订历史 |
| 指标口径 | 明确指标公式、分母、过滤范围、生效期、来源和版本；模糊问题先解释默认口径或请求补充 |
| 数据查询 | 一个 DuckDB 数据源、表结构工具、SQL AST 检查、只读连接、超时和结果大小限制 |
| 持续调查 | 固定目标和范围，根据实际结果深入核查，保留发现、反证、限制和下一步，支持用户继续调查 |
| 报告证据 | KPI、表格、基础图表、解释段落；数字由服务端从查询产物取值；报告修订不可变 |
| 外部核查 | 受控接入历史观测及资料检索，保留来源、时间、匹配条件和固定证据；API 或 MCP 均经过服务端工具权限校验 |
| 多月 BTS 数据 | 月度文件集合、质量与来源清单、固定多月快照、日期范围与跨期比较，保留旧分析 |
| 条件化分析工具 | 明确输入、适用范围、所需数据与预算，扩充分布、分组、质量及证据核查能力 |
| 质量评估 | 标准问题、人工核对的 SQL 与答案、基线对比、失败分类、实际运行成本和耗时 |
| 可交付性 | 本地启动、数据导入命令、配置示例、自动化检查、小型演示数据和操作说明 |

范围之外：通用 CSV/XLSX 业务表导入、任意数据库连接、多 Agent 编排、平台级插件市场、任意代码执行、自动联网代理、多个模型部署服务、多租户、通用 Dashboard 编辑器、分布式任务系统。后续需求必须说明它如何改善上述完整路径。

## 3. 主演示场景

已接入 **2026 年 1 月 Reporting Carrier On-Time Performance**。下一步先增加 2026 年 2 月、3 月，月度原始文件集中在 `data/bts/YYYY-MM.csv`；用户提供的新月份文件需先核验，跨月能力仍待阶段 6 实施。下载与 29 字段清单见 [蓝图 03](03-data-and-sql.md#bts-download)。每月及整体规模由实际文件计算，不预填。该表与 Marketing Carrier 统计口径不同，不混合导入、不拼接成两倍数据。若下载受限，可以手动下载后本地导入，不用合成数据冒充官方数据。

演示问题：

1. “比较各航空公司的平均到达时间偏差与平均非负延误，解释为什么结果不同。”
2. “按项目定义计算有效到达航班的延误率，同时列出取消率与备降率。”
3. “只看计划航班数不少于 100 的航线，找出取消率最高的十条航线。”100 是显式演示阈值，不是官方标准。
4. “按已上报的延误原因分钟数，比较航空公司、天气、NAS 等原因构成。”结论必须说明这是报告口径，不能当作独立因果推断。
5. “根据这些数据计算各航空公司的利润。”缺少收入和成本数据，应说明无法计算，并指出可提供的运行指标。

新增调查目标：“本月航班运行中，哪些问题值得优先核查？”系统应从整体情况出发，根据真实结果检查时间、机场或 Reporting Carrier 差异，视证据需要核查样本、分母、缺失与反证；数据不足或没有显著发现时如实说明。现有单月数据不能支持跨月趋势，也不能单独证明天气等原因。

## 4. 文档阅读顺序

| 文件 | 内容 |
| --- | --- |
| [01-architecture.md](01-architecture.md) | 技术选型、分层、目录、对象关系、配置和启动约定 |
| [02-agent-runtime.md](02-agent-runtime.md) | Agent 循环、职责分工、工具、上下文、中间件和错误恢复 |
| [03-data-and-sql.md](03-data-and-sql.md) | BTS 来源、下载导入、表结构、连接配置、SQL 防护与示例 |
| [04-knowledge-and-metrics.md](04-knowledge-and-metrics.md) | 文档、检索、Wiki、指标规则、版本与引用 |
| [05-evidence-and-reports.md](05-evidence-and-reports.md) | 查询产物、数字引用、报告 Schema、验证与修订 |
| [06-ui-and-api.md](06-ui-and-api.md) | 统一工作区、文档与 Wiki、API、事件流、状态与交互 |
| [07-build-and-evaluate.md](07-build-and-evaluate.md) | 实施顺序、每阶段验收、评估集和交付清单 |
| [08-code-reading-map.md](08-code-reading-map.md) | 本项目代码阅读路线、模块职责和设计核查问题 |

## 5. 在当前工作区开始

本蓝图位于 `docs/design/`，对应实现位于根目录的 `backend/`、`frontend/` 和 `scripts/`。根目录的 [AGENTS.md](../../AGENTS.md) 适用于整个工作区。

实施前先检查工作区已有文件，并参考 [实施顺序](07-build-and-evaluate.md)。已有应用目录和启动命令以根目录 README 为准；阶段完成后在 `IMPLEMENTATION_STATUS.md` 记录实际实现和验证结果。外部 API Key 从环境变量读取，不提交真实数据大文件或密钥。

## 6. 实施取舍

模型负责选择下一步，运行框架负责预算与生命周期，工具负责受控执行，知识服务固定规则版本，报告服务绑定冻结证据。模块通过明确的 Python 协议和组合对象连接，避免引入当前单用户、单分析引擎场景不需要的组件容器和兼容层。

尤其不要复刻模型隐式思维链。界面展示简短行动说明、工具输入、结果摘要、错误和证据即可。

单用户本地应用是明确部署边界。默认监听回环地址，提供一个本地工作区。公网部署、身份认证和组织隔离需要另行设计，不能声称已有企业级权限能力。

## 7. 官方来源

- [BTS 数据下载与字段选择](https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ)
- [BTS 字段字典](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ)
- [2026 年技术报告指引 #40](https://www.bts.gov/explore-topics-and-geography/modes/aviation/number-40-technical-directive-reporting-time)，页面注明 2026-01-01 生效。历史数据必须匹配适用版本。
- [DuckDB Python API](https://duckdb.org/docs/stable/clients/python/dbapi)
- [DuckDB 配置](https://duckdb.org/docs/stable/configuration/overview)
- [FastAPI 文档](https://fastapi.tiangolo.com/)

官方资料支持字段语义与报告规则；本项目的指标默认值、交互、架构和 API 是项目自己的设计。公开统计规范不是某航空公司的内部制度。来源、许可和下载版本应记录在数据清单中，不笼统声明所有第三方资料均可按本项目许可证再发布。

<a id="implementation-decisions"></a>

## 8. 已实施阶段的设计取舍

设计目标与实施取舍统一维护在本蓝图。以下各节保留实施时的背景、取舍理由和后续调整；历史阶段的“尚未实现”描述不代表当前状态。实际验收及当前限制以根目录 IMPLEMENTATION_STATUS.md 为准，代码执行路径见 docs/walkthroughs/。

| 实施记录 | 合并位置 |
| --- | --- |
| 0001：阶段 0：工程边界与职责划分 | [01-architecture.md](01-architecture.md#decision-0001) |
| 0002：阶段 1：数据快照与受限查询 | [03-data-and-sql.md](03-data-and-sql.md#decision-0002) |
| 0003：固定知识修订、引用与审核边界 | [04-knowledge-and-metrics.md](04-knowledge-and-metrics.md#decision-0003) |
| 0004：单 Agent、持久运行与阶段 3 查询结果 | [02-agent-runtime.md](02-agent-runtime.md#decision-0004) |
| 0005：服务端取值、固定报告与可追溯展示 | [05-evidence-and-reports.md](05-evidence-and-reports.md#decision-0005) |
| 0006：持续调查、运行额度与证据归属 | [02-agent-runtime.md](02-agent-runtime.md#decision-0006) |
| 0007：数据探索与正式项目指标分开授权 | [04-knowledge-and-metrics.md](04-knowledge-and-metrics.md#decision-0007) |
| 0008：主数据边界与外部证据版本分开校验 | [05-evidence-and-reports.md](05-evidence-and-reports.md#decision-0008) |
| 0009：在单 Agent 调查中组合受控外部工具 | [02-agent-runtime.md](02-agent-runtime.md#decision-0009) |
