# 业务口径驱动的数据分析 Agent：个人项目实施蓝图

编写日期：2026-10-27。项目暂名 **MetricPilot**，主演示数据为美国 BTS 航班运行数据。名称可以修改，接口和模块名以本文档为准。

这是一套供开发者和编码 AI 在当前工作区实施的设计文档，并不是已经完成的应用。文中的目录、API、配置、代码均为拟定契约；没有实际运行的能力不得写成已实现。

## 1. 项目解决什么问题

用户用自然语言分析数据库时，困难不只是 SQL 语法，还包括：业务指标定义不清、统计范围不同、查询失败无法恢复，以及报告中的数字难以核对。

本项目完成一条完整路径：

```text
用户问题
  → 检索业务规则与数据字典
  → 查阅有来源的 Wiki / 指标定义
  → 探索数据库结构
  → 执行经过检查的只读 SQL
  → 必要时根据错误修正
  → 生成绑定查询结果与规则版本的报告
  → 用户点击数字查看依据
```

对外定位是通用的数据分析 Agent，BTS 是主演示场景，不把程序写成只能回答固定航班问题的模板系统。

## 2. 完成版范围

| 能力 | 完成标准 |
| --- | --- |
| 单 Agent 执行框架 | 原生工具调用、统一工具契约、运行状态、步数和时间预算、错误反馈、SSE 事件、可回放运行记录 |
| 业务知识与 Wiki | 文档版本、分块、混合检索、来源引用、Wiki 草稿生成、人工编辑和修订历史 |
| 指标口径 | 明确指标公式、分母、过滤范围、生效期、来源和版本；模糊问题先解释默认口径或请求补充 |
| 数据查询 | 一个 DuckDB 数据源、表结构工具、SQL AST 检查、只读连接、超时和结果大小限制 |
| 报告证据 | KPI、表格、基础图表、解释段落；数字由服务端从查询产物取值；报告修订不可变 |
| 质量评估 | 标准问题、人工核对的 SQL 与答案、基线对比、失败分类、实际运行成本和耗时 |
| 可交付性 | 本地启动、数据导入命令、配置示例、自动化检查、小型演示数据和操作说明 |

范围之外：多 Agent 编排、平台级插件市场、任意代码执行、自动联网代理、多个模型部署服务、多租户、通用 Dashboard 编辑器、分布式任务系统。后续需求必须说明它如何改善上述完整路径。

## 3. 主演示场景

默认尝试导入 **2026 年 1 月 Reporting Carrier On-Time Performance** 一个完整月份。下载成功后记录实际行数，不预先宣称恰有多少条。该表与 Marketing Carrier 统计口径不同，不混合导入、不拼接成两倍数据。若下载受限，可以手动下载后本地导入，不用合成数据冒充官方数据。

演示问题：

1. “比较各航空公司的平均到达时间偏差与平均非负延误，解释为什么结果不同。”
2. “按项目定义计算有效到达航班的延误率，同时列出取消率与备降率。”
3. “只看计划航班数不少于 100 的航线，找出取消率最高的十条航线。”100 是显式演示阈值，不是官方标准。
4. “按已上报的延误原因分钟数，比较航空公司、天气、NAS 等原因构成。”结论必须说明这是报告口径，不能当作独立因果推断。
5. “根据这些数据计算各航空公司的利润。”缺少收入和成本数据，应说明无法计算，并指出可提供的运行指标。

## 4. 文档阅读顺序

| 文件 | 内容 |
| --- | --- |
| [01-architecture.md](01-architecture.md) | 技术选型、分层、目录、对象关系、配置和启动约定 |
| [02-agent-runtime.md](02-agent-runtime.md) | DB-GPT 设计映射、Agent 循环、工具、上下文、中间件和错误恢复 |
| [03-data-and-sql.md](03-data-and-sql.md) | BTS 来源、下载导入、表结构、连接配置、SQL 防护与示例 |
| [04-knowledge-and-metrics.md](04-knowledge-and-metrics.md) | 文档、检索、Wiki、指标规则、版本与引用 |
| [05-evidence-and-reports.md](05-evidence-and-reports.md) | 查询产物、数字引用、报告 Schema、验证与修订 |
| [06-ui-and-api.md](06-ui-and-api.md) | 三个页面、API、事件流、状态与交互 |
| [07-build-and-evaluate.md](07-build-and-evaluate.md) | 实施顺序、每阶段验收、评估集和交付清单 |
| [08-dbgpt-reading-map.md](08-dbgpt-reading-map.md) | 包内 DB-GPT 源码快照阅读路线和参考边界 |
| [references/dbgpt/README.md](references/dbgpt/README.md) | 12 份完整源码快照、来源清单与许可证，可离线查阅 |
| [AGENTS.template.md](AGENTS.template.md) | AI 开发约束的参考模板；实际约束见工作区根目录 AGENTS.md |

## 5. 在当前工作区开始

当前工作区包含根目录 `AGENTS.md` 和 `参考/` 资料目录；本蓝图位于 `参考/docs/blueprint/`，快照位于其 `references/dbgpt/` 子目录。根目录的 [AGENTS.md](../../../AGENTS.md) 适用于整个工作区；[AGENTS.template.md](AGENTS.template.md) 仅供参考。

实施前先检查工作区已有文件，并参考 [实施顺序](07-build-and-evaluate.md)。应用目录、启动命令和 `IMPLEMENTATION_STATUS.md` 均需在实施时创建；阶段完成后记录实际实现和验证结果。外部 API Key 从环境变量读取，不提交真实数据大文件或密钥。

## 6. 实施取舍

保留 DB-GPT 的 Agent / Action / Resource 分离、上下文预算、工具执行记录、Wiki 来源与修订、报告执行产物的思路；用小而明确的 Python 协议和组合对象代替庞大的继承层级、组件容器和兼容层。

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
