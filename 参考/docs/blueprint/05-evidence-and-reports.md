# 05 执行证据与报告

## 1. 证据链

```text
ReportRevision
  → ReportBlock / Claim
    → ValueRef 或 EvidenceRef
      → QueryArtifact
        → ToolExecution + SQL + params + DataSnapshot
        → MetricRevision / WikiRevision / DocumentRevision / Chunk
```

查询执行成功才产生 QueryArtifact。ID、哈希、执行时间和绑定快照由服务端填写，不能信任模型给出的“执行成功”文字。

来源链存在是必要条件，不自动等于推理正确。工程校验能保证引用和取值真实；口径、解释与因果表述还需要受限规则校验、评估和人工复核。

## 2. QueryArtifact 契约

```json
{
  "id": "qa_server_generated",
  "run_id": "run_example",
  "execution_id": "exec_example",
  "snapshot_id": "snapshot_example",
  "sql": "SELECT ...",
  "parameters": {"start_date": "2026-01-01", "end_date": "2026-02-01"},
  "guard_version": "duckdb_allowlist_v1",
  "columns": [{"name": "delay_rate_pct", "type": "DOUBLE"}],
  "returned_rows": 1,
  "truncated": false,
  "result_hash": "computed_sha256",
  "metric_revision_ids": ["metricrev_example"],
  "knowledge_revision_ids": ["docrev_example"],
  "duration_ms": 123,
  "created_at": "actual_utc_time"
}
```

上面所有 ID 和数值为格式示例。完整结果不塞进元数据行，以 result_hash 定位内容寻址文件。执行完成先原子写产物文件，再在事务中登记元数据；孤立文件可以清理，引用到不存在的文件必须报错。

结果规范序列化需要类型标签、列顺序、行顺序、null 和 Decimal 规则。SQL 没有 ORDER BY 时结果顺序未必可复现；图表和按行引用的结果应要求稳定排序，或仅引用已冻结结果的实际顺序并明确它不是业务排名。

## 3. 数字引用与报告块

KPI 和结构化数值由服务端按 ValueRef 取值，模型只提供标签与布局。单元格引用默认零基行号。

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ValueRef(StrictModel):
    artifact_id: str
    row: int = Field(ge=0)
    column: str

class KPIBlock(StrictModel):
    type: Literal["kpi"]
    id: str
    title: str
    value_ref: ValueRef
    format: Literal["integer", "decimal_2", "percent_2", "minutes_2"]

class TableBlock(StrictModel):
    type: Literal["table"]
    id: str
    title: str
    artifact_id: str
    columns: list[str]

class ChartBlock(StrictModel):
    type: Literal["chart"]
    id: str
    title: str
    artifact_id: str
    kind: Literal["bar", "line"]
    x: str
    y: list[str]

class TextBlock(StrictModel):
    type: Literal["text"]
    id: str
    text_template: str
    values: dict[str, ValueRef] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)

Block = Annotated[KPIBlock | TableBlock | ChartBlock | TextBlock,
                  Field(discriminator="type")]

class ReportDraft(StrictModel):
    schema_version: Literal["1"]
    title: str
    scope: str
    metric_revision_ids: list[str]
    blocks: list[Block]
    limitations: list[str]

class ReportCandidate(StrictModel):
    kind: Literal["report"]
    report: ReportDraft

class ClarificationCandidate(StrictModel):
    kind: Literal["clarification"]
    question: str
    missing_requirements: list[str] = Field(min_length=1)

FinalCandidate = Annotated[ReportCandidate | ClarificationCandidate,
                           Field(discriminator="kind")]
```

模型适配器的 `final_candidate` 是该联合类型的 JSON 对象。运行边界可用 `TypeAdapter(FinalCandidate).validate_python(payload)` 解析，并将 ValidationError 转为可修复的结构错误。澄清或资料不足采用 clarification 分支，无数据结论不需要强行生成 KPI 报告；页面显示缺少条件和下一步，而不是空白报告。

TextBlock 模板示例：`"该范围内延误率为 {{rate}}，使用项目有效到达口径。"`，`values.rate` 指向具体单元格。占位符只支持简单键，用受限替换器解析；禁止 Jinja 任意表达式与代码。普通日期、名称中的数字可以作为文字出现；数量性结论应使用引用模板，不能声称靠正则能自动识别所有虚构数字。

百分比约定：SQL 的 `*_pct` 返回 0—100 数值，`percent_2` 只格式化并附 `%`，不再乘以 100。null 显示“无可用值”，不是零。

TextBlock 支持 `evidence_ids` 引用已访问的知识摘录或查询产物。每类 ID 带明确前缀或类型，服务端按类型解析，禁止从任意 URL/文件路径拉取内容。

## 4. 报告校验顺序

1. Pydantic discriminated union 校验，拒绝未知块、重复 block ID 和缺失字段。
2. 所有查询产物必须属于当前 run 的成功执行，且数据快照一致。
3. 指标、文档与 Wiki 引用必须存在于 run 的知识快照；模型不得虚构版本。
4. row 在范围内、column 存在、数值类型匹配；null 与非有限值有明确处理。
5. 图表只接受白名单类型和真实列，y 为数值；禁止模型输出 JavaScript formatter 或任意 ECharts option。
6. 截断结果不可用于声称完整的表、图和汇总；必须重新聚合或明确受限预览，默认拒绝发布为分析图表。
7. 校验所声明指标与执行器记录的 metric_revision_ids 一致；受限模板指标进一步验证模板键、参数和输出列。
8. 服务端物化数字、格式化值、生成来源索引；冻结报告内容与引用清单。
9. 事务保存 ReportRevision 并产生 report.ready 事件。

自由 SQL 的业务语义自动验证能力有限，界面显示 `verified_template` 或 `references_validated`，后者不能标成“业务结论已验证”。模型不能自己设置验证级别。

## 5. 报告修订

同一个逻辑 report_id 可对应多个 immutable revision。新问题默认生成新报告；仅排版或解释修订可在同一 report_id 下创建新 revision，仍需重新校验。

冻结内容包含标题、范围、格式化结果、结果 hash、原始引用、指标定义副本或固定 revision、schema_version。读取旧报告不得重新查当前数据库或当前 Wiki 来填数字。

单用户本地阶段不做公开匿名分享。报告导出先提供 Markdown/JSON；PDF 可后续添加，保持证据索引，但不作为核心路径的前置条件。

## 6. EvidenceDrawer 需要展示什么

- 当前数字的原始值、格式化值、列名和行号。
- SQL、绑定参数、执行耗时、结果摘要、截断状态。
- 数据表/月份、快照 ID、原文件哈希。
- 指标公式、分母、过滤规则、指标版本、验证级别。
- 引用的原文摘录与定位；Wiki 页面是整理层，允许继续查看原始资料。

## 7. 测试重点

伪造 ID、跨 run 引用、越界行、缺失列、百分比二次放大、Decimal 精度、null、截断结果、重复块 ID、模型 JS 注入、修改当前数据后旧报告不变、规则回滚后旧引用仍能打开。至少一个测试真实执行 DuckDB SQL，再生成并读取报告，不只 mock 数据。
