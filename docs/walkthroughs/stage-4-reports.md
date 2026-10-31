# 阶段 4：数字取值、报告校验与固定版本

更新日期：2026-10-31。项目名称：**A Business-Aware Data Analysis Agent**；Python 包名：metric_pilot。

## 1. 阶段目标

阶段 3 让模型根据问题查资料、看结构、执行安全查询。阶段 4 在这条真实路径后接上报告：把成功查询整理成中文关键数字、表格、图表和解释，让用户点击数字核对依据，并保留以后仍能读取的固定版本。

模型决定展示哪些内容，服务端决定数字从哪里读取、引用是否允许、何时保存。模型不能填写报告 ID、物化值、验证等级或可执行图表代码。

## 2. 执行流程

~~~text
模型调用真实查询工具，取得本次成功产物
  → 模型提交报告草稿：标题、范围、块与取值地址
  → 服务端校验产物归属、列、行、类型、口径与截断情况
  → 从冻结结果读取和格式化数字，固定原文与指标版本
  → 同一事务保存报告修订、report.ready 和 run.completed
  → 页面打开报告，点击数字读取这份报告保存的依据
~~~

ValueRef 就是“取值地址”：查询产物 ID、零基行号、原始列名。它不是一段表达式，也不允许模型提交任意文件路径。文字使用简单的 {{key}} 占位符，服务端把 key 对应的真实单元格填进去；不执行模板语言、Python 或 JavaScript。

格式或引用错误可以反馈模型修正，沿用有限修复预算。资料不足仍返回澄清，取消、超时和失败不产生成功报告。历史阶段 3 的 query_result 继续可读，运行 DTO 中 report 只包含已保存报告的 ID 与修订 ID，不包含未经检查的模型草稿。

## 3. 代码文件讲解

### 模型声明和服务端显示数据分开

[reports/contracts.py](../../backend/src/metric_pilot/reports/contracts.py) 定义 ReportDraft、ValueRef、四种块、冻结单元格、报告详情、证据与修订请求。模型只能使用关键数字、表格、柱状图/折线图和纯文字模板，不接受任意图表配置。

表格的原始列名与中文显示名分别保存。百分比只接受返回 0—100 的 *_pct 列；显示添加百分号，不重复乘百。null 显示“无可用值”。Decimal 和超出 JavaScript 安全范围的整数保留类型及精度。

### 检查、取值与固定证据

[reports/values.py](../../backend/src/metric_pilot/reports/values.py) 根据真实行列取值，使用 Decimal 格式化，拒绝非有限数值和不匹配类型。模板只允许简单命名占位符，缺失或多余键均拒绝；*_pct 在卡片、表格和文字中的显示规则一致，重复百分号被拒绝。

[reports/materialize.py](../../backend/src/metric_pilot/reports/materialize.py) 检查重复块、列、显示标签、指标绑定和图表数据，再生成服务器显示值。折线图必须有稳定排序、唯一升序横轴，缺失数值不当作零。单张图最多 60 行与 3 个数值系列；单份报告最多 8 个查询产物、20 个块，冻结内容受大小限制。

[reports/sources.py](../../backend/src/metric_pilot/reports/sources.py) 根据本次实际工具访问、成功查询所绑定来源和指标，固定原文与知识版本。指标必须已审核并覆盖数据月份；Wiki 必须属于本次快照中的已发布版本。官方片段、项目规则、生成 Wiki 分开标识；含项目注释的整份原文单独标识，避免把其中全部文字都当作官方说明。

[reports/service.py](../../backend/src/metric_pilot/reports/service.py) 复用已有 QueryService 读取并核对产物哈希与运行归属，拒绝跨任务、错快照、越界、歧义列与截断结果。报告额外保存冻结行、原始值、显示值、查询元数据、来源和口径副本；读取旧报告或依据不依赖当前数据、Wiki 或原查询结果文件。取证时只返回所选块/值实际绑定的来源。

### 接入既有 Agent，并原子提交

[agent/context.py](../../backend/src/metric_pilot/agent/context.py) 提供报告提示和严格 schema；固定上下文增加“当前不可用指标”清单，避免工具历史裁剪后模型忘记已遇到的审核障碍而反复检索。

[agent/runtime.py](../../backend/src/metric_pilot/agent/runtime.py) 接收报告候选，调用报告服务准备内容；错误以安全代码和字段位置反馈，允许在预算内修正。原来的模型调用、消息配对、受限 SQL 和显式取消继续复用。

[agent/store.py](../../backend/src/metric_pilot/agent/store.py) 在提交事务内再次检查取消和时间限制，保存报告、严格递增事件与运行终态。没有报告先可见而运行随后被取消的半完成窗口。重启时尚未完成的运行仍按原机制标为中断，不伪称恢复模型状态。

[reports/models.py](../../backend/src/metric_pilot/reports/models.py) 与 [0005_reports.py](../../backend/migrations/versions/0005_reports.py) 保存报告指针和只追加的修订。每个修订有规范内容哈希；修订提交需匹配加载时版本，避免静默覆盖他人的新修改。

### 页面、证据与导出

[ReportsPage.tsx](../../frontend/src/components/ReportsPage.tsx) 提供报告列表、详情、会话内预览、历史切换、分页加载更早版本，以及“修改说明”保存新版本。正文居中，数字卡片、图表、表格与限制有独立间距，依据通过抽屉按需查看。

[ReportChart.tsx](../../frontend/src/components/ReportChart.tsx) 使用受控 HTML/SVG 绘制柱状图和折线图，没有增加任意 ECharts 配置入口。中文使用系统字体；折线按容器宽度绘制，手机减少横轴标签。绘图坐标仅用于排版，不重新计算业务指标；数据点标签仍使用服务端显示值，支持键盘取证。

[EvidenceDrawer.tsx](../../frontend/src/components/EvidenceDrawer.tsx) 先展示点击值、原始值和数据范围，同类型片段与完整原文合为一个阅读入口并保留实际行号；口径与原文按需查看；SQL、参数、版本和哈希默认折叠。点击指标来源可以打开对应固定原文。只通过报告块及单元格选择读取依据，客户端不能任意指定别的查询产物。

[reports/api.py](../../backend/src/metric_pilot/reports/api.py) 提供列表、固定修订、历史、取证、修订和 Markdown/JSON 导出。图表在 Markdown 中按对应冻结数据表导出，JSON 保留取值引用与冻结查询原始行；两种导出均附固定 SQL、参数和结果摘要。原文字段导出时转义，模型内容不会成为可执行页面。

前端报告类型来自生成的 [schema.d.ts](../../frontend/src/api/schema.d.ts)，不是独立手写的另一套业务契约。

## 4. 具体例子

用户问“本月记录数与日期范围”时，模型自行查询，再把记录数、日期等作为 ValueRef 放入报告。服务端取值并保存；点击记录数可看到原值、SQL 和固定数据版本。

用户要求“逐日记录数折线图”时，模型查询完整聚合结果并排序。图中的每个点与表格单元格均指向冻结行列，页面显示中文标题与系列名；缺失值会留空，不画成零。

用户要求利润或未审核项目指标排名时，仍应说明缺少收入成本或已审核口径；不能为了凑一份报告绕过既有执行限制。实际指标仍由用户人工审核，测试中的审核在隔离数据库。

## 5. 验证与限制

[test_reports.py](../../backend/tests/test_reports.py) 以真实 SQLite、DuckDB 和隔离 FakeLLM 验证取值、百分比、精度、模板、跨任务、截断、取消提交边界、修复、修订冲突和固定来源。还验证删除原产物文件、更新原文、变更及回退指标后，旧报告仍可读取自己的依据。

[verify_reports.py](../../scripts/verify_reports.py) 经正式 HTTP 调用本机真实模型，以原始 CSV 独立统计为基线，检查范围报告与逐日图表；利润缺数据、未审核指标必须结束为澄清。首次网络失败和指标反复检索耗尽预算的尝试保留在忽略的验收记录中，修复后四例均实际通过。

最新检查数量、截图与真实模型记录位置见 [IMPLEMENTATION_STATUS.md](../../IMPLEMENTATION_STATUS.md)。这些案例不是整体问答准确率或跨平台验证。当前只有 references_validated，不声称已实现可执行指标模板的业务语义证明；自然语言解释与引用是否真正支持结论仍需复核。当前不做 PDF、公开分享、多 Agent 或任意代码图表。
