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

当前实现还冻结 query_purpose（legacy / exploratory / reviewed_metric）、exploration_scope 和 unavailable_metric_keys。legacy 仅兼容历史及未显式声明用途的查询，不表示已审核。exploratory 必须有统计范围、计算/分母与缺失处理声明，不绑定正式指标修订；reviewed_metric 表示实际绑定经过审核和日期校验的修订，不等于 SQL 语义自动证明。报告的指标集合必须等于引用产物实际绑定的并集；不能把探索产物通过报告补填修订升级成正式指标。服务端按产物追加探索限制，人工修订说明也不能移除该身份；旧冻结报告不回填新字段或改写哈希。

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

### 5.C 已实现的外部证据边界

`QueryArtifact` 新增可空 `external_snapshot_id` 与默认空 `external_evidence_ids`。`snapshot_id` 仍指运行主数据，不改成天气快照。服务端从 `external_run_bindings` 查本轮授权的天气版本，通过独立 schema/数据库执行安全 SQL；只有成功产物可供 ValueRef。天气归类为未审核探索，不绑定项目指标；缺失、同刻冲突和界限值分别处理，降水/积雪不开放累计列。

`external_evidence` 固定真实原件、哈希、请求、时间与定位；公告候选不能成为正文依据。报告准备时验证证据和外部快照，再将实际引用的来源和快照冻结到报告；`ReportDTO` / `EvidenceDTO.external_snapshots` 返回所用外部版本。旧报告缺少这些字段仍兼容读取，不回填或改写哈希。数字校验不自动证明天气与异常的因果关系。

同调查已完成旧运行复用仍须符合主数据、知识和会话边界，并核对外部版本；本轮一旦选定同条件新版本，不混用旧版产物。查询旧天气版本前须显式绑定，报告读取旧成功产物沿用原授权。失败/取消/中断运行不能跨运行引用。当前为服务层验收，Agent、发现和页面接入留到 5.D；细节见 [决策 0008](05-evidence-and-reports.md#decision-0008)。

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
2. 查询产物须来自当前 run 的成功执行，或服务端授权的同调查、同会话、相同数据及知识快照、状态 completed 的旧 run；再校验成功执行、归属和文件哈希。普通运行不获得跨运行权限。
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

## 调查发现与报告组织（阶段 4 已实现）

围绕用户目标呈现范围和口径、主要发现、支持与削弱发现的证据、限制与下一步建议。发现状态区分待核查、证据支持、证据不足、被反证削弱或否定；保留实际检查，不把候选发现直接包装成结论。比较时说明人群、分母、样本量、缺失和时间范围；数据不支持因果解释时只描述观察与建议。无显著发现也是有效结果。

数量性结论与图表仍由服务端通过 ValueRef 从成功的冻结结果取值，引用校验不代表语义或因果已经证明。跨 Run 复用已经按上述授权校验接入调查；未通过校验的产物仍须在本次重新查询，不能靠模型给出旧 ID 放行。每轮调查报告独立冻结，发现修正也只追加历史；失败或取消运行前保存的发现可查看，但其产物不直接授权用于后续报告。调查记录与报告草稿都不能自行批准业务规则。

## 7. 测试重点

伪造 ID、跨 run 引用、越界行、缺失列、百分比二次放大、Decimal 精度、null、截断结果、重复块 ID、模型 JS 注入、修改当前数据后旧报告不变、规则回滚后旧引用仍能打开。至少一个测试真实执行 DuckDB SQL，再生成并读取报告，不只 mock 数据。

---

### 后续多月证据契约（阶段 6—7，待实施）

新主快照纳入多个 BTS 月份时，查询产物和报告须明确实际日期范围、所含月份、缺口与比较人群，并能回查原始文件及行定位。跨月 ValueRef 仍只从成功冻结结果取值，不能在前端临时合并旧单月报告或把分组首行当作总体。正式指标覆盖整个比较范围，缺失月份、零分母、分位算法、样本与构成限制随证据保留；不自动延长旧指标适用期。换范围创建新调查或分支，既有单月产物、旧运行与旧报告不重算。外部快照继续单独授权，跨月目标不扩大来源条件或联网预算。

<a id="decision-0005"></a>

## 实施设计取舍 0005：服务端取值、固定报告与可追溯展示

本节保留实施时的阶段边界及后续补充；当前完成状态以根目录 IMPLEMENTATION_STATUS.md 中相关事项的最新日期记录为准。

日期：2026-10-31。范围：蓝图 05/06/07 的阶段 4。

复用阶段 3 的单 Agent 循环与阶段 1 成功查询产物。模型提交 ReportDraft，只声明布局、标签、固定证据与 ValueRef；服务端读取真实值、验证归属和格式后生成展示数据。report 运行结果保存服务器生成的 report_id/revision_id；历史 query_result 与 clarification 继续兼容。模型候选和持久运行结果分别定义，避免未经验证的草稿成为可读报告。

报告只接受本次成功、同快照、未截断产物。指标声明必须等于所用产物绑定指标的并集，并再次核对已审核与整月日期适用性。不执行模板键或自由公式；当前一律为 references_validated，不能声明 verified_template。对自然语言文字的所有数量、口径和因果语义不声称已经自动证明。

报告冻结数据行、规范类型、服务端显示值、查询和知识元数据、指标与原文副本，并计算内容摘要。SQLite 存储报告快照，现有内容寻址查询文件继续保留；旧报告和导出读取自己的副本，不重查当前数据库或 Wiki。已有表/引用不足以证明业务解释正确，页面保留限制说明。

初次提交报告、report.ready 与 run.completed 位于同一短事务，锁等待后再次检查取消及 deadline；取消、超时、验证失败或重启中断不会留下成功报告。修订只追加记录，使用 expected_version 检查并发；同报告只能使用既有冻结证据，不能混入另一运行或新查询。

图表首版采用原生 HTML/SVG，保留蓝图的 bar/line 白名单，缩小 ECharts 的预定实现范围，避免引入模型自定义 option/formatter 或额外图表依赖。系统中文字体、响应式折线、键盘可点击点、空值留空；只在视觉坐标中转换有限数值，业务值和格式来自服务端。折线要求唯一升序横轴与 ORDER BY，图表最多 60 行/3 个系列。Markdown 图表导出为冻结数据表，JSON 保留引用和冻结原始行；两种导出附查询元数据、SQL、参数与哈希，离开页面仍可核对。PDF 留待后续。

前端沿用对话式执行摘要与按需抽屉，不恢复常驻右侧进度栏。报告预览连接详情、列表、历史和编辑说明；正文突出中文数据阅读，SQL/版本/哈希默认折叠。公开 DTO 由 OpenAPI 重新生成，SSE report.ready 纳入既有 seq/重放。

真实验收发现指标不可用信息在大工具消息裁剪后丢失，模型反复检索直到预算耗尽。将 blocked_metric_keys 与审核/澄清要求放入固定上下文，并为指标失败提供明确 ToolResult 信息；不硬编码问题路由或放宽 SQL 限制。修复后真实未审核指标案例转为澄清。

实际检查、失败历史与限制见实施记录；没有批准实际指标或发布 Wiki。阶段 5 的整体评估与准确率基准尚未实施。

---

<a id="decision-0008"></a>

## 实施设计取舍 0008：主数据边界与外部证据版本分开校验

本节保留实施时的阶段边界及后续补充；当前完成状态以根目录 IMPLEMENTATION_STATUS.md 中相关事项的最新日期记录为准。

日期：2026-11-03。阶段 5.C 将已经留存的天气观测和 FAA 正文接入证据、查询与报告服务，尚未开放 Agent 自动调用。

保留 `QueryArtifact.snapshot_id` 作为运行的主数据快照，不将天气注册成可切换的 BTS 数据源，也不放宽报告既有主快照检查。天气 SQL 增加 `external_snapshot_id`、`external_evidence_ids`，由服务端从实际运行绑定取出只读 DuckDB 文件和允许 schema，复用 AST、防护连接、子进程执行、超时/取消及成功产物规则。数据库迁移使用 SQLite 原地新增可空引用列，避免重建有旧产物引用的父表。

`external_evidence` 保存数据库锚定的不可变 payload 与摘要，包含请求、原件哈希、获取时间、原始定位及解析版本信息。原件另存到内容寻址目录；天气冻结前从原件复算关键观测、质量、覆盖和范围，不把可编辑的采集 JSON 直接认作真实数值。公告回查清单及正文原件，再提取正文、有效期和限制；搜索候选不能当正文证据。

同运行、同来源条件只选一个固定版本，重复登记同一采集幂等，不能覆盖。新运行可以选择新版本。跨运行只允许阶段 4 已授权的同调查、同会话、同主数据及知识快照、已完成旧运行；失败/取消/中断来源不能复用。旧天气产物可以在授权后供报告引用；执行新的天气 SQL 前，还须显式绑定该外部版本。新运行一旦选定同条件新版本，不能混用旧版本产物。

查询表按原始观测时刻归并，不插值、不把缺失补零。同刻冲突置空；界限能见度不进入精确统计，仅保留独立下界列。温度与风速只有通过筛选的精确值进入数值列，状态和原件定位一并保留。降水、积雪保留原始资料，但暂不开放累计数值列，避免把不等时段观测相加。

天气数字必须经成功 SQL 产物再由 `ValueRef` 取值；公告按固定正文来源引用。报告冻结实际用到的外部快照、来源和定位，读取旧报告不联网、不读取新版本、不重算旧值。外部来源会强制附加用途限制，不能把天气同现或公告描述自动当作航班异常原因。既有旧报告不回填字段或改写哈希。

本步只完成服务层。三个工具的模型签名、运行预算调度、SSE、调查发现和页面展示属于 5.D；不因独立报告样本成功宣称完整外部调查完成。
