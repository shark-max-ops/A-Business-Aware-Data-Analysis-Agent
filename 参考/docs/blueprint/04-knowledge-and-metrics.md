# 04 业务知识、Wiki 与指标定义

## 1. 三类知识必须区别

| 类型 | 示例 | 用法 |
| --- | --- | --- |
| 官方资料 | BTS 字段字典、当年报告指引、代码表 | 解释字段和报告语义，有 URL、获取时间和适用范围 |
| 项目规则 | 有效到达分母、最小样本阈值、默认比较区间 | 由开发者明确制定并审核，标为 project_policy |
| 生成 Wiki | 概念整理、字段关联、指标解释 | 来源资料的可读导航，不能凭生成结果创造官方政策 |

没有公开的航空公司内部制度就明确没有。系统自己的产品手册说明如何导入、提问和读报告，不伪装成企业内部材料。

## 2. 原始材料包

实际开发时建立 `knowledge_seed/manifest.json`，至少登记：

1. [BTS 字段字典](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ)：只摘录选用字段的说明。
2. [2026 年技术报告指引 #40](https://www.bts.gov/explore-topics-and-geography/modes/aviation/number-40-technical-directive-reporting-time)：使用适合本项目的章节，标明生效期。
3. 实际下载的承运人/机场代码表及字段说明。
4. 自写《项目指标口径 v1》：明确所有自定义分母和过滤规则。
5. 自写《数据范围与限制》：数据表类型、月份、空值、不能计算的利润等。
6. 自写《分析使用说明》：产品功能与操作步骤。

文档输入先支持 UTF-8 Markdown/TXT。官方网页可人工整理为 Markdown，保留 URL 和章节定位；该过程标记为人工整理而非原网页原件。PDF 解析作为增强项，不能假装所有复杂表格已被可靠解析。

来源文档存 revision，不直接覆盖。每份文档记录 `source_kind`、`origin_url`、`retrieved_at`、`effective_from/to`、`content_hash`。摘录定位统一用 source locator：Markdown 标题/段落；后续 PDF 才使用页码。禁止给没有页码的网页编造页码。

## 3. 处理与检索

```text
上传 → 保存不可变 revision → 按标题/段落分块 → FTS5 + embedding
     → 提取 Wiki 候选 → 验证引用 → 草稿 → 人工审核发布
```

- 分块先按章节边界，约 400—800 tokens，必要时 80—120 tokens 重叠；数值是可调初始参数，用评估决定。
- 保存原文坐标和块哈希；不能只保存模型摘要。
- embedding 记录模型 ID、维度和文本哈希；换模型必须重建索引，不能混合向量空间。
- 小规模向量可以保存为 SQLite BLOB，进程内 NumPy 余弦搜索；重启从持久化索引恢复。
- FTS5 处理官方英文术语；中文输入先结合维护的中英指标别名扩展，再检索。中文项目材料需要显式分词或经验证的 tokenizer，不能假设默认 FTS5 能正确分中文词。
- 两路候选用 RRF 合并，原始相关性分数不要直接相加；`k=60` 是初始参数，不是实测最优值。
- 每次检索仅在 run 的知识快照里搜索，并检查数据年份与规则适用期。
- 返回摘录、修订 ID 和 locator。引用来源 ID 必须存在，文本需要实际包含被引用内容。

检索结果示例：

```json
{
  "chunk_id": "chunk_example",
  "document_revision_id": "docrev_example",
  "source_kind": "official_reference",
  "locator": {"section": "Arrival Performance", "paragraph": 3},
  "excerpt": "经实际导入的原文摘录，不在种子数据中伪造",
  "effective_from": "2026-01-01"
}
```

## 4. Wiki 最小生成流程

参考 DB-GPT 的 MAP/DEDUP/REDUCE/FINALIZE 思路，缩成：

1. **提取候选**：逐文档生成概念、指标、字段解释候选，每项附来源 chunk ID。
2. **确定性合并**：先按规范 slug 与别名去重。无法确认同义关系时保留两项供人工审核，避免全量 LLM 两两比较。
3. **合并正文**：按主题组织 Markdown；每个规则段至少绑定一个来源；已有人工编辑页面只生成新草稿与 diff。
4. **发布与链接检查**：审核后创建新 revision，构建目录与 backlinks，重新索引当前发布版本。

一个空间先限制 20—40 页、最多两层目录。目录建议：字段与数据范围、指标定义、运行状态、分析方法。图谱是可选增强，不在三个主页面之外增加大型图谱工作台。

状态：`draft → published → superseded`。Wiki 更新采用 expected_version 乐观锁，过期保存返回 409；回滚创建新版本，不删除历史。生成任务以输入文档 revisions 和当前 Wiki revision 构成幂等键。

引用校验：生成内容引用的 chunk 必须属于本次输入、来源必须存在、摘录 hash 必须匹配。这个检查只能确认引用真实，不能自动证明模型准确解释了语义；发布前人工审核与评估仍然必要。

## 5. 指标定义不是一段自由文本

Wiki 负责解释与导航；计算采用已审核、可版本化的 `MetricDefinition`。分析 Agent 可以提出自定义指标，但不能把它自动保存为正式规则。

字段含义、质量检查与原始分布调查可以独立探索。query_sql 显式使用 purpose=exploratory，并说明 population（范围与排除）、calculation（算法与分母）、missing_values（缺失处理），不得提交正式指标修订。成功产物由服务端冻结为探索身份，报告、发现和依据继续保留“未绑定已审核项目指标”的说明。某个指标解析失败不封锁独立探索或其他已审核指标；未声明用途的旧查询不能仅删引用绕过阻断。用户明确要求正式项目指标时，探索只能交付独立核查部分并说明缺口，不能冒充正式答案。

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    version: int = Field(ge=1)
    name: str
    authority: Literal["official_mapping", "project_policy"]
    review_state: Literal["draft", "approved"]
    unit: Literal["count", "percent", "minutes"]
    grain: str
    numerator_description: str
    denominator_description: str | None
    population_filters: list[str]
    required_columns: list[str]
    null_policy: str
    effective_from: str
    effective_to: str | None = None
    source_revision_ids: list[str]
    template_key: str | None = None
```

这些描述本身不应被 `eval`。经审核的 `template_key` 对应 SQL 构建器是后续设计，当前未实现自动模板语义证明。正式指标查询须绑定已解析、已审核且日期适用的修订；独立探索按上一段的用途与范围契约执行。字符串 contains 判断不是可靠语义验证；当前服务端验证执行、权限与引用，不能声称已证明自由 SQL 的公式、分母和解释完全符合业务含义。

## 6. 首批项目指标

下表是 **项目设计**。官方字段含义与项目统计分母分开展示，不能把整套定义称为 BTS 官方指标。

| metric_key | 项目定义 |
| --- | --- |
| scheduled_flights | 所选 Reporting Carrier 数据范围内的计划航班记录数；包含取消/备降 |
| eligible_arrivals | cancelled=0、diverted=0，arrival_delay 与 arrival_delay_nonnegative 有值，arrival_delayed_15 为 0/1 的记录数 |
| arrival_delay_rate | eligible_arrivals 中 arrival_delayed_15=1 的数量 / eligible_arrivals；零分母为 null |
| cancellation_rate | cancelled=1 的数量 / 所选范围全部计划记录数 |
| diversion_rate | diverted=1 的数量 / 所选范围全部计划记录数；不假设与取消集合绝对互斥 |
| mean_arrival_deviation | eligible_arrivals 中 arrival_delay 的平均值，允许负数 |
| mean_nonnegative_delay | 同一 eligible_arrivals 集合中 arrival_delay_nonnegative 的平均值；包含值为零的航班 |
| reported_delay_cause_share | 五类已报告原因分钟分别求和 / 五类合计；单独报告缺失率，不把缺失默认为“无此原因” |

延误阈值字段语义参考 [官方字段字典](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ)。最后一项只描述已报告分钟数构成，不估算所有航班的真实因果贡献。

时间范围使用 flight_date 左闭右开。排序至少附稳定的第二排序键；“最差航线”必须说明按什么指标和最小样本数。单月数据不回答跨季度下降，也不擅自外推。

## 7. 引用与版本处理

运行开始固定知识快照。`resolve_metric` 只能返回该快照内 approved 且适用期匹配的修订，零个候选返回缺失，多个冲突候选返回澄清。

报告保存 metric revision + Wiki revision + 原始 document/chunk revision 引用。仅引用 Wiki 标题不足以形成来源链。

规则更新后，旧报告保持原值、原文和原来源。要比较新规则，用户创建新 run；不能让旧报告因当前 Wiki 更新而改变解释。

## 调查中如何使用口径与用户材料（待实现）

调查开始时固定知识快照；查找适用的已审核指标及其生效期，保留分子、分母、统计范围、空值与来源。样本量门槛、异常判断标准和比较基线须来自已说明的项目规则、用户选择或明确标注的分析假设，不能冒充官方标准，也不能静默视为已审核指标。缺少适用口径时提出必要澄清；八项实际项目指标目前仍需用户审核。

用户导入的业务资料、官方资料、项目规则和生成 Wiki 分别标识，记录来源及适用期；系统可提出字段含义或规则候选供用户确认，不能自动批准。旧调查和报告始终引用原有修订。

## 8. 验收

更新文档后能找到新版本且旧报告引用仍可打开；模型不能引用不存在的 chunk；未审核 Wiki 不能提升为正式指标；规则生效期不匹配时会提示；中文指标别名可检索到英文资料；冲突规则不自动拼接；重新生成不覆盖人工编辑。
