# 阶段 3：模型工具调用、运行控制与事件流

> 本文说明阶段 3 的贡献与当时边界。阶段 4 现已扩展报告生成与数字取证，见[阶段 4 讲解](stage-4-reports.md)；下文的查询结果契约是阶段 3 基线与历史兼容路径。

更新日期：2026-10-31（Asia/Shanghai）。[返回导读目录](README.md)。

## 1. 阶段目标

阶段 3 把前两个阶段已有的知识管理和安全查询能力，接成一个由模型驱动、服务端控制的分析流程。

模型根据用户问题决定下一步：查资料、解析指标、看表结构、提出 SQL，或说明缺少条件。服务端固定权限、版本、预算和产物归属，并展示实际执行过程。代码没有用问题关键词命中预置 SQL 来冒充 Agent。

本阶段交付查询产物和澄清提示。完整报告、ValueRef 物化、KPI/图表和数字点击取证仍属于尚未实施的阶段 4。

## 2. 执行流程

```text
分析页提交问题
  → API 固定本次数据、知识和预算
  → 后台领取运行
  → 模型选择工具
  → 服务端校验并执行工具
  → 将实际结果或错误反馈给模型
  → 继续查询、修正，或结束并请求补充条件
```

这是一个可以多次往返的循环。模型一轮可以选择多个工具；工具返回后，模型再决定下一步。需要什么工具由问题决定，例如数据范围核对可以查看结构后直接查询，业务指标则需要明确指标口径与来源。

## 3. 代码文件讲解

### 3.1 `api.py` 与 `contracts.py`：接收问题并固定分析依据

[agent/api.py](../../backend/src/metric_pilot/agent/api.py) 的 `create_run()` 检查数据快照、创建固定知识快照、校验已选指标的审核与整月适用性，再把模型名称和运行/SQL 预算保存到新运行中，返回 HTTP 202 和运行信息。

Run 是一次分析任务，一个会话可以有多个 Run。需要澄清时，本次 Run 正常结束；用户补充后创建新 Run。允许带入同会话、同数据快照的已完成上文，但旧产物不能直接成为新运行的数字依据。

[agent/contracts.py](../../backend/src/metric_pilot/agent/contracts.py) 定义创建请求、运行状态、工具参数和事件格式。最终候选只有两种：

| 类型 | 含义 |
| --- | --- |
| `query_result` | 提交本次成功查询的产物 ID |
| `clarification` | 说明缺少的条件并请求用户补充 |

它们不等于阶段 4 的 ReportDraft；当前不会发出 `report.ready` 或保存正式报告修订。

### 3.2 `provider.py`：让模型选择工具

[agent/provider.py](../../backend/src/metric_pilot/agent/provider.py) 的 `AgentProvider.next_turn()` 把问题、上下文和工具签名发送到已配置模型；`parse()` 把原生 `tool_calls` 转成内部 `ToolCall`。

例如模型可以提出以下工具与参数。这只是格式示意，不代表运行时固定要调用它：

```json
{
  "name": "describe_table",
  "arguments": {"table_name": "v_flights"}
}
```

适配器对 429 和短暂 5xx 做有限传输重试，并限制响应大小、校验协议。非标准 NaN/Infinity 会被拒绝或转成可反馈的无效参数，不让非法 JSON 进入事件流。

适配器丢弃 `reasoning_content` 和工具思维元字段，也不保存工具回合附带的模型正文；只提取工具调用、最终候选和白名单数值用量，不保存内部思维链。

### 3.3 `runtime.py`：组织模型与工具的受控循环

[agent/runtime.py](../../backend/src/metric_pilot/agent/runtime.py) 的 `AgentRuntime.run()` 是核心协调代码：

1. 检查取消和剩余时间，整理上下文。
2. 请求模型，判断它返回工具调用还是最终候选。
3. 工具调用先过批次预算与调用 ID 检查，再串行执行。
4. 为每个调用保存对应的 `tool` 回复，包括错误和取消，再交给模型处理。
5. 最终结果检查结构与本次产物归属，合格后保存终态。

默认最多 **16 轮模型交互、24 次工具调用、180 秒运行时间**。轮数和工具次数分别计量；超出调用预算的整批工具不执行，避免只执行半批留下未配对消息。

字段或参数错误可以反馈修正。相同工具加相同参数连续失败两次停止；查询超时与最终候选各最多两次修复。传输重试与模型主动修正分别控制，不让错误进入无限循环。空结果属于正常查询结果。

`wait_model()` 在等待实际异步请求时检查取消与 deadline，结束时回收请求任务；SQL 工具把取消信号继续传到真实查询进程。

同文件的 `AgentWorker` 串行领取持久化运行，当前分析并发为 1。[main.py](../../backend/src/metric_pilot/main.py) 在应用生命周期中启动和关闭它，知识 worker 仍独立工作。

### 3.4 `tools.py`：只开放明确的七个工具

[agent/tools.py](../../backend/src/metric_pilot/agent/tools.py) 的 `SPECS` 显式注册工具，`ToolRegistry.invoke()` 先按严格参数模型校验，再分发到已有服务。

| 工具 | 用途 |
| --- | --- |
| `search_knowledge` | 在固定知识快照中关键词检索原文与已发布 Wiki 导航 |
| `read_wiki_page` | 读取快照中的固定发布修订，不读取草稿 |
| `resolve_metric` | 获取已审核且适用于整个月份的指标 |
| `list_tables` | 查看当前数据快照允许查询的表 |
| `describe_table` | 查看字段、类型、语义与少量样本 |
| `query_sql` | 调用既有受限查询服务 |
| `read_artifact` | 分页读取当前运行的成功冻结结果 |

模型不能通过工具参数指定数据库文件、工作区、连接串或另一个知识快照，也没有审核指标、发布 Wiki、写任意文件或执行任意代码的工具。

指标解析缺失或冲突后，查询工具会阻止简单地省略指标引用来绕过该失败。查询中声明的指标必须来自允许的已解析集合，用户已选的指标也不能被悄悄省略。

这些引用检查不等于自动理解所有自由 SQL。系统明确使用 `references_validated` 级别，不声称分母或业务语义已经被全面证明。

### 3.5 复用 `data/query.py`：让模型也遵守同一执行边界

[QueryService.execute()](../../backend/src/metric_pilot/data/query.py) 继续执行阶段 1 的 AST 检查、受限只读连接和查询子进程流程。阶段 3 增加由服务端把指标/知识修订绑定到成功产物。

SQL 取消和超时最终进入 [QueryExecutor.execute()](../../backend/src/metric_pilot/data/executor.py)，终止并回收实际进程。失败记录不能成为成功 QueryArtifact，读取产物也要检查运行归属与哈希。

模型最终提交的是产物 ID，服务端会重新核对 ID 是否指向本次成功执行；不能靠模型写一句“查询成功”就接受结果。

### 3.6 `context.py`：控制上下文长度，保留关键依据

[agent/context.py](../../backend/src/metric_pilot/agent/context.py) 的 `initial_messages()` 组织目标、固定版本和允许的上文；`fit_context()` 保留指标定义与当前产物清单，按完整 assistant/tool 组裁剪历史。

预算使用 UTF-8 字节作保守估计，并预留工具签名与输出空间；仍超限就明确失败。原始执行记录不会随上下文裁剪被覆盖，需要更多结果可以调用 `read_artifact`。

### 3.7 `store.py`、`models.py` 与 `middleware.py`：保存真实运行过程

[agent/models.py](../../backend/src/metric_pilot/agent/models.py) 与 [0004_agent.py](../../backend/migrations/versions/0004_agent.py) 定义并创建会话、运行、事件和工具审计表。Run 本身就是持久化分析任务，没有再增加职责相同的 Job 队列。

[agent/store.py](../../backend/src/metric_pilot/agent/store.py) 的 `RunStore` 用短事务处理创建、领取、结束、取消与重启恢复。状态包括 queued、running、completed、failed、cancelled、interrupted。重启将遗留运行和未结束工具记为中断，不冒称恢复模型调用。

[agent/middleware.py](../../backend/src/metric_pilot/agent/middleware.py) 的 `RuntimeObserver` 在模型请求前、工具执行前后和运行结束时记录进度与审计。安全限制仍由 Runtime 与执行器强制执行，观测代码不能放宽权限。

### 3.8 SSE 与分析页：断线后恢复实际进度

[agent/api.py](../../backend/src/metric_pilot/agent/api.py) 的 `events()` 按数据库事件游标持续读取，支持 `Last-Event-ID` 和 `after`。事件 `seq` 在事务内严格递增，先提交再被订阅读取；心跳不写数据库。

[AnalysisPage.tsx](../../frontend/src/components/AnalysisPage.tsx) 管理会话、调查选择、问题提交、显式停止和按调查保存的页内草稿，顶部提供调查汇总及工作区数据入口。同一调查的运行按时间连续展示，默认渲染最近 20 轮，每次可再显示 20 轮；运行列表接口仍一次返回全部记录，分批的是页面渲染。“定位轮次”只滚动到对应回答，不改变当前调查或发送目标；无调查归属的历史独立运行只读查看。

[RunTurn.tsx](../../frontend/src/components/RunTurn.tsx) 独立维护各轮执行记录、读取错误和结果分页，仅活动运行订阅 SSE；[api/analysis.ts](../../frontend/src/api/analysis.ts) 的 `mergeEvents()` 按运行 ID 和序号去重。历史运行在展开执行记录时通过 [eventLog.ts](../../frontend/src/api/eventLog.ts) 校验并读取现有 `event-log` 接口，[runHistory.ts](../../frontend/src/api/runHistory.ts) 按页合并记录，失败保留已读部分，重试从已读位置继续；未读取、部分读取不显示为完整统计，轮次卸载后停止读取后续页。

报告和查询结果在轮次进入视野附近时加载；各轮查询分页与失败重试独立。`runHistory.ts` 为报告、查询及固定依据读取提供共享的三路并发队列，只对固定报告修订与冻结依据缓存成功响应，失败后允许重试，不缓存创建或取消请求。

[ExecutionTrace.tsx](../../frontend/src/components/ExecutionTrace.tsx) 把执行过程放在本次问题下方：运行时显示真实当前操作，完成后自动收起；点击箭头展开短步骤，点击“执行详情”或某一步再打开右侧抽屉，集中查看 SQL、参数、来源、错误和版本。页面不再常驻显示执行侧栏。

执行记录读取区分网络、HTTP、响应解析和契约错误，部分分页失败也会保留已读记录；错误与重试在执行详情内可见，读取失败不会改写运行终态。调查记录读取失败不能当成零发现。用户原先报告的“获取失败”尚未在原浏览器复现、根因未确认，新增诊断与重试入口不等于已修复该历史故障，后续应结合实际错误核对请求、代理与服务状态。

[executionPresentation.ts](../../frontend/src/components/executionPresentation.ts) 按真实事件序号和执行 ID 配对步骤，保留失败与后续成功尝试，避免把所有状态都显示为绿色完成。用时只从实际开始/结束事件计算，缺失时不编造。抽屉关闭和步骤折叠不会发出取消请求。

[ConversationItem.tsx](../../frontend/src/components/ConversationItem.tsx) 将左侧会话名固定为单行，溢出时显示省略号；悬停或键盘聚焦后按实际文字宽度滚动，离开即复位。开启减少动态效果时不播放动画。

[titles.py](../../backend/src/metric_pilot/agent/titles.py) 校验模型附带的可选短标题，runtime 在严格验证分析结果前先将其拆出。store 只允许最新运行更新会话名，标题不进入查询证据或分析结果；无效或缺失时继续使用原问题。完整问题不会被标题替换。已有长名可通过 [summarize_conversation_titles.py](../../scripts/summarize_conversation_titles.py) 显式补全，默认预览，`--apply` 会把原始提问发送到已配置文本模型。

日期、Decimal 和大整数以原值文本显示，null 显示缺失；前端不重新计算指标。

刷新或断线后可以恢复状态与事件。关闭页面只停止订阅，不自动取消运行；“停止分析”才调用幂等取消接口。

## 4. 具体例子

用户问“这个月的数据有多少条记录，覆盖哪些日期？”时，既有真实模型记录中，模型自主选择了可用表、字段结构和 SQL 查询，从冻结结果取得记录数与日期范围。

用户问“这个月哪家航空公司的延误率最高？”时，当前实际指标尚未审核，系统返回需要补充/审核的条件，没有按草稿产生排名。利润问题因缺收入和成本数据而说明无法计算。

这是模型按问题选择不同工具与结束方式。正常运行不依赖测试 FakeLLM，也没有预置这三道题到 SQL 的映射。

## 5. 验证与限制

[test_agent.py](../../backend/tests/test_agent.py) 用仅测试可用的 FakeLLM 验证框架，但实际执行 SQLite、知识服务和 DuckDB 查询，包括消息配对、预算、修复、空结果、重放和取消后子进程回收。

[verify_agent.py](../../scripts/verify_agent.py) 通过真实 HTTP 驱动已配置模型；`--reuse-record` 只读取已有实际运行、事件和冻结结果重新核对，不冒充再次调用模型。隔离浏览器入口 [browser_analysis_server.py](../../scripts/browser_analysis_server.py) 使用独立数据库和位于测试目录的模型，正式配置没有 FakeLLM 开关。

已有 Windows 验收记录为后端 110 项、前端 9 项、浏览器合计 6 项通过，Agent 专项 18 项复验通过；真实模型三个案例另行记录。这些数字来自已有 [实施记录](../../IMPLEMENTATION_STATUS.md)，本文整理时没有重新运行测试或请求模型。

当前 Agent 使用关键词检索，知识页保留真实混合检索。八项实际项目指标仍待人工审核，Wiki 未发布。自由 SQL 的业务语义没有被全面自动证明，完整报告和 ValueRef 物化尚未实施；Linux/macOS 与整体问答准确率也没有据这些 Windows 案例得到验证。


2026-10-31 的执行进度展示修订另有前端单元与隔离浏览器检查，最新结果见实施记录；上面的 110/9/6 为阶段 3 原验收记录，不能当成本次全部重新运行的结果。
