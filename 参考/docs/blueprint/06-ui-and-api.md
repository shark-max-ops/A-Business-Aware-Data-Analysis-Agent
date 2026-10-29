# 06 页面、API 与事件协议

## 1. 页面范围

只有三个主页面。数据连接管理放在分析页的数据源抽屉，不再做大型配置中心。界面默认中文，保留英文原始字段名以便核对。

### 页面 A：知识管理 `/knowledge`

```text
┌───────────────────────────────────────────────────────────────┐
│ MetricPilot   知识管理 / 分析 / 报告          模型连接状态      │
├─────────────┬─────────────────────────────────────────────────┤
│ 原始文档    │ Wiki / 文档 / 指标（页内 tabs）                  │
│ Wiki 目录   │ 到达延误率                      草稿/已发布 v2  │
│ 指标目录    │ 定义、适用数据、分母、过滤、正文与来源          │
│ 搜索        │ [编辑] [版本历史] [生成更新草稿]                │
│ 上传        │ 右侧来源抽屉：摘录 → 原始文档固定版本          │
└─────────────┴─────────────────────────────────────────────────┘
```

文档上传后显示 queued/parsing/indexing/ready/failed。生成 Wiki 是显式后台任务，显示本次输入文档版本、进度、成本（若有）、失败重试按钮。草稿发布前必须显示来源和 diff；“生成”不等于“已审核”。

指标编辑使用结构化表单：名称、公式描述、范围、分母、null 策略、生效期、来源。避免让用户在产品里编辑 SQL guard 配置等实现细节。

### 页面 B：分析对话 `/analysis`

```text
┌───────────────────────────────────────────────────────────────┐
│ 数据：BTS Reporting 2026-01 ▾    知识版本：已固定    新会话   │
├──────────────┬────────────────────────────┬───────────────────┤
│ 会话列表     │ 用户问题                   │ 执行进度          │
│              │                            │ ✓ 查阅指标        │
│              │ 分析完成摘要               │ ✓ 查看表结构      │
│              │ [打开报告]                 │ ✓ 查询，返回 15 行│
│              │                            │ ✓ 生成报告        │
├──────────────┴────────────────────────────┴───────────────────┤
│ 输入问题……                              [发送 / 停止]        │
└───────────────────────────────────────────────────────────────┘
```

右侧展示简短行动说明和真实工具结果，不展示模型内部思维链。工具项可展开 SQL、参数、结果预览与错误。取消后显示已取消；失败显示失败阶段和“重新发起”，不伪装成成功回答。

数据源抽屉：文件导入、快照列表、实际行数、月份、质量摘要、可查询表、设为当前快照。完整 raw CSV 通过本地导入命令最省事，网页可先只显示已登记快照。未配置模型、无数据、无已审核规则各有明确空状态。

会话只在创建 run 时绑定快照；运行中禁止修改。用户在会话内切换数据时创建新的分析上下文，不能让上一轮数字默认套到新数据上。

### 页面 C：报告详情 `/reports/:reportId/revisions/:revisionId`

顶部显示问题、业务范围、指标版本、数据月份和生成时间。正文依次为 KPI、图表、明细表、解释、限制。右侧 EvidenceDrawer 随点击具体数字打开。

展示代码示例：

```tsx
type CellRef = { artifact_id: string; row: number; column: string };
type KPIProps = {
  title: string;
  displayValue: string; // 服务端物化并格式化后的值
  valueRef: CellRef;
  onInspect: (ref: CellRef) => void;
};

export function KPI({ title, displayValue, valueRef, onInspect }: KPIProps) {
  return (
    <section aria-label={title}>
      <h3>{title}</h3>
      <button type="button" onClick={() => onInspect(valueRef)}>
        {displayValue}
      </button>
    </section>
  );
}
```

图表适配器只把 chart.kind、x、y 和冻结数据转换成受限 ECharts 配置。Markdown 默认禁用原始 HTML，链接限定安全协议。前端不计算指标，不重新拼接 SQL，不接受模型生成的 HTML 页面。

## 2. HTTP 契约

API 前缀 `/api/v1`。普通 JSON 响应用 HTTP 状态码表达成功/失败；列表用 `{items, next_cursor}`。错误统一为 `{error: {code, message, details, request_id}}`，不再套多层 success/data/result。

| 方法和路径 | 用途 |
| --- | --- |
| GET `/health` | 元数据库、worker 状态；不产生付费模型请求 |
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
