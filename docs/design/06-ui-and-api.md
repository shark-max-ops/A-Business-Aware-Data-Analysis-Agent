# 06 页面、API 与事件协议

## 1. 统一工作区

界面采用暖色纸面、左侧导航和连续内容画布。分析、知识及报告保留已有 URL 以支持书签与旧报告，但共享同一应用框架，不再把分析页拆成多块常驻面板。页面默认中文，技术记录按需展开。

### 左侧导航与会话

侧栏提供分析、知识管理、报告入口。分析会话使用固定高度的独立滚动列表，长标题单行省略，完整标题可通过悬停、键盘和可访问名称读取；会话多时提供本地标题筛选。会话数量不能撑长整页，也不能将底部输入框挤出屏幕。手机上改为可打开、可关闭的侧栏，不把导航内容挤入正文。

### 分析工作区 `/analysis`

```text
┌──────────────┬─────────────────────────────────────────────┐
│ 工作区导航   │ 数据源入口                                  │
│ 新会话       │ 数据与调查范围                              │
│ 搜索会话     │                                             │
│ 会话列表     │ 问题、简短执行进度、报告与发现              │
│ 独立滚动     │ 正文独立滚动，技术记录按需展开              │
│              │                                             │
│ 服务状态     │ 持续可见的输入区 / 显式停止 / 发送          │
└──────────────┴─────────────────────────────────────────────┘
```

空态以问题输入为中心，示例只填入问题，不自动执行、不预填 SQL。当前快照与调查范围清楚可见，新调查/分支继续显式固定版本；跨月尚未实现时不展示可用的跨月选择。执行中显示真实事件，完成后摘要收起，失败/澄清/取消/中断分别呈现。断开页面不取消运行，显式停止仍终止实际请求与查询进程。完整输入、SQL、错误及证据保留于按需详情。

### 知识管理 `/knowledge`

仅保留“文档管理”和“Wiki”两个主入口，移除常驻的固定版本知识检索面板、“指标定义”独立页签和“处理任务”页签。删除 UI 入口不删除后台检索、快照、任务、指标审核或历史证据服务。

**文档管理：** 统一列出参考资料与计算规则，支持名称筛选、分类和原文阅读。新建文档按需打开编辑抽屉，可直接撰写 Markdown 或上传 UTF-8 Markdown/TXT，现有大小、来源及版本校验继续由后端执行。任务状态就近显示在文档卡片，失败可重新整理，不单独暴露一页任务 JSON。

**规则说明：** 计算口径作为文档目录中的规则说明呈现，说明统计范围、算法、分子分母、缺失处理、适用期与来源。后端仍保存结构化 MetricDefinition 和不可变修订，编辑只产生草稿，人工独立审核后才可供新调查使用；不能改成不受校验的自由说明或自动批准。原文历史与规则修订按需打开，旧引用仍读取固定原件。

**Wiki：** 关系图用圆形节点表达实际文档、引用主题、Wiki 和规则之间的关系；鼠标悬停或键盘聚焦时放大，减少动态效果偏好下禁用缩放动效。点击后在地图下方展示选中文档、关联节点及原文/规则阅读入口，长标题在详情完整呈现。缩放、滚动和键盘操作不改变存储内容；连线是引用关系，不是因果推断。草稿编辑默认按需展开，保存后回到阅读视图，发布和回滚保持既有确认及版本冲突保护。

### 报告 `/reports` 与历史修订

报告列表及详情位于统一工作区，分析回答内仍可预览并打开完整报告。关键数字、表格、柱状图和折线图使用服务端冻结值；点击卡片、单元格或数据点读取已有依据。修订追加新版本，保留 Markdown/JSON 导出，旧 URL 不替换成当前报告。图表采用项目现有受限 HTML/SVG，不执行模型 HTML/JS、formatter 或任意图表插件。

### 契约与界面验证

本轮不更改 API DTO、数据库或数据/知识版本，UI 沿用现有后端契约；不重新生成或批准真实 Wiki、指标，不触发外部模型索引。后台任务仍持久化和真实反馈错误，知识快照继续由调查创建流程固定。

验证重点为：大量会话时侧栏独立滚动与输入区可见；创建/阅读文档、选择引用、Wiki 发布/回滚、规则草稿/审核及日期拒绝；圆形节点悬停/键盘/点击导航；发现和报告取证、取消、重放、分支与版本；桌面和手机页面无横向溢出，截图在动画稳定后实际查看。隔离浏览器模型不冒充新的真实模型验收。



## 2. HTTP 契约

API 前缀 `/api/v1`。普通 JSON 响应用 HTTP 状态码表达成功/失败；列表用 `{items, next_cursor}`。错误统一为 `{error: {code, message, details, request_id}}`，不再套多层 success/data/result。

| 方法和路径 | 用途 |
| --- | --- |
| GET `/health` | 元数据库、worker 状态；不产生付费模型请求 |
| POST `/investigations` | 固定目标、范围与数据/知识版本，可用 parent_id 显式建立分支 |
| GET `/investigations?conversation_id=...` | 同会话调查列表 |
| GET `/investigations/{investigation_id}` | 调查、运行关联、累计已分配额度与发现历史 |
| GET `/investigations/{investigation_id}/findings/{finding_id}/evidence` | 该发现已冻结的数值、SQL 和资料依据 |
| GET `/sources` | 数据源及当前快照 |
| GET `/sources/{source_id}/snapshots` | 已导入数据版本、质量摘要 |
| POST `/sources/{source_id}/activate` | 选择默认快照，不改变已有 run |
| GET `/sources/{source_id}/schema?snapshot_id=...` | 当前快照的允许表与列 |
| POST `/documents` | multipart 上传 Markdown/TXT，返回 document_id + job_id |
| GET `/documents/{document_id}/revisions/{revision_id}` | 固定原文修订 |
| POST `/wiki/generate` | 输入文档 revision ID 列表，返回 job_id |
| GET `/jobs/{job_id}` | 获取任务进度与错误 |
| GET `/wiki/pages` | 目录、搜索、草稿/发布状态 |
| GET `/wiki/pages/{page_id}/revisions/{revision_id}` | 固定 Wiki 修订 |
| PUT `/wiki/pages/{page_id}` | content + citations + expected_version，创建新草稿修订 |
| POST `/wiki/pages/{page_id}/publish` | 发布指定草稿，带 expected_version |
| POST `/wiki/pages/{page_id}/revert` | 指定历史 revision，创建新草稿 |
| GET `/metrics` | 指标列表与已审核版本 |
| POST `/metrics` | 创建项目指标草稿 |
| POST `/metrics/{key}/revisions` | 新建指标修订，带 expected_version |
| POST `/metrics/{key}/approve` | 审核固定修订 |
| POST `/conversations` | 新建会话 |
| GET `/conversations` | 会话列表 |
| GET `/conversations/{conversation_id}` | 会话及运行摘要 |
| POST `/runs` | 创建运行，HTTP 202，返回 run_id |
| GET `/runs/{run_id}` | 状态、已绑定快照、报告引用 |
| GET `/runs/{run_id}/events` | SSE，支持 Last-Event-ID 重放 |
| POST `/runs/{run_id}/cancel` | 幂等取消；终态不回退 |
| GET `/runs/{run_id}/artifacts/{artifact_id}` | 当前运行产物元数据与分页预览 |
| GET `/reports` | 报告列表和最新修订 |
| GET `/reports/{report_id}/revisions/{revision_id}` | 冻结报告和证据索引 |
| GET `/reports/{report_id}/revisions/{revision_id}/evidence` | 按 block_id/value key 获取证据详情 |

Wiki/Metric 写入是应用操作，不通过分析 Agent 工具执行。查询产物接口先核对 URL 中 run_id 与产物归属；本地单用户仍不允许任意 ID 越过运行关联。

POST `/runs` 请求例子：

```json
{
  "conversation_id": "conv_example",
  "question": "比较各航空公司的到达时间偏差与非负延误",
  "snapshot_id": "snapshot_example",
  "metric_revision_ids": [],
  "context_run_ids": []
}
```

知识快照由服务端根据当前已发布资料创建；用户可选指标，也可由 Agent 在该快照中 resolve。`context_run_ids` 只允许本会话已完成运行，用于重建上文摘要。跨 run 数据不能直接作为本次报告数字证据，需要在同一数据快照重新查询；跨月份比较应先建立包含所需月份的新数据快照。

## 3. SSE 协议

浏览器先 POST 创建运行，再用 EventSource GET 订阅。避免 EventSource 无法直接 POST 的问题。Vite 同源代理，不依赖前端直连 provider。

统一事件 envelope：

```json
{
  "run_id": "run_example",
  "seq": 12,
  "type": "tool.finished",
  "created_at": "actual_utc_time",
  "payload": {
    "tool_execution_id": "exec_example",
    "tool": "query_sql",
    "status": "succeeded",
    "summary": "查询完成，返回 15 行",
    "artifact_ids": ["qa_example"]
  }
}
```

SSE 帧（payload 的 JSON 放单行）：

```text
id: 12
event: tool.finished
data: {"run_id":"run_example","seq":12,"type":"tool.finished","created_at":"...","payload":{}}

```

事件类型固定为：`run.queued`、`run.started`、`run.progress`、`tool.started`、`tool.finished`、`report.ready`、`run.completed`、`run.failed`、`run.cancelled`、`run.interrupted`。

每个 run 的 seq 严格递增，由数据库事务分配。先提交事件，再通知订阅者。前端按 run_id+seq 去重；重连读取 Last-Event-ID，服务端返回 seq 更大的事件。subscribe 必须无“读历史到注册监听”的丢事件窗口：初版可直接按 seq 轮询 SQLite，再写 SSE；用心跳保持连接，避免每次心跳写数据库。

report.ready 只表示报告已提交，前端继续等待 run.completed 终态。订阅端看到终态后关闭 EventSource。断开 SSE 不等于取消运行；取消只能来自显式 cancel 请求。已完成的运行订阅时仍应重放终态后关闭。

run.completed 的 payload 具有 `result_kind: report | clarification`。report 分支带 report_id 和 revision_id；clarification 分支带 question 与 missing_requirements，不发出 report.ready。GET `/runs/{run_id}` 返回相同结果结构，确保刷新后可恢复。

流式展示先做“工具与进度事件流”。只有适配器实际支持时再做文字 token delta，不把假的打字机效果称为模型流式推理。

## 4. 前端状态原则

用 discriminated union 表达 RunStatus 和报告块；不要用大量互相矛盾的布尔值。API 层统一错误转换，组件只处理明确状态。用户刷新后从服务端恢复会话、运行和报告，不以浏览器内存为唯一来源。

## 5. 演示验收

上传后看到真实处理状态；能编辑并打开历史 Wiki；分析页看到工具开始、结束与错误；取消会停止后端工作；断网恢复不丢事件、不重复卡片；报告数字能打开执行证据；切换当前数据快照后旧报告不变；空值、失败和无依据问题均有准确提示。
