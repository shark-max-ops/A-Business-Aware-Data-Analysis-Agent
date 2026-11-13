# 阶段 2 材料准备包

这是阶段 2 的材料入口，文档服务、检索、Wiki 与指标审核接口现已实现。原始资料与项目规则分开登记，本机八个初始项目指标仍保持 **draft，待业务审核**。清单本身不是运行时知识快照；实际修订由导入服务生成。

## 七份首批材料

| 材料 | 类型 | 实际文件 |
| --- | --- | --- |
| 选用字段字典 | 官方原文摘录，附项目映射 | [01-bts-selected-fields.md](../data/knowledge_prepared/01-bts-selected-fields.md) |
| 2026 填报指引相关章节 | 官方 PDF 原文摘录，附实际页码 | [02-bts-directive-2026-excerpts.md](../data/knowledge_prepared/02-bts-directive-2026-excerpts.md) |
| 航空公司 ID 对照说明 | 官方字段定义、代码表及项目检查 | [03-bts-carrier-lookup.md](../data/knowledge_prepared/03-bts-carrier-lookup.md) |
| 机场 ID 对照说明 | 官方字段定义、代码表及项目检查 | [04-bts-airport-lookup.md](../data/knowledge_prepared/04-bts-airport-lookup.md) |
| 项目指标口径 v1 | 项目规则草案 | [05-metric-policy-v1.md](project/05-metric-policy-v1.md) |
| 数据范围与限制 | 项目范围和实际检查说明 | [06-data-scope-and-limitations.md](project/06-data-scope-and-limitations.md) |
| 分析使用说明 | 区分已有能力和后续目标 | [07-analysis-guide.md](project/07-analysis-guide.md) |

前四份是项目整理的来源材料，并非 BTS 新发布文件。官方英文摘录、项目中文提示分别标记；代码表计数是项目计算，不能因为文档 source_kind 为 official_reference 就把每句提示当作官方规则。
后续分块须保留段落的来源角色：中文项目提示不能作为“官方原文”返回或支持官方指标声明；若导入器暂不能区分，应先将这些提示拆为 project_policy 文档。官方引用只绑定实际摘录及其原文定位。
原始资料和整理后的第三方材料保存在 Git 忽略的 `/data/` 中，本仓库不预设第三方资料再分发许可。克隆仓库后需依清单重新获取原件；不能只凭 manifest 声称已有原文。

## 清单和候选文件

- [manifest.json](manifest.json)：相对路径、SHA-256、来源、适用期、处理方式、原文定位及校验摘要。
- [metrics.draft.json](metrics.draft.json)：与蓝图 MetricDefinition 对齐的八个草稿；source_revision_ids 留空，另用材料 ID 指明未来绑定来源。材料 ID 不是数据库修订 ID。
- [validation.json](../data/knowledge_prepared/validation.json)：真实文件检查结果。13 个航司、342 个机场 ID 全部匹配；尚未执行新的 DuckDB 导入。
- [source_locators.json](../data/knowledge_prepared/source_locators.json)：字段单元格/PDF 页码与摘录哈希。
- `data/knowledge_prepared/dim_carriers.csv` 与 `dim_airports.csv`：按现有导入器列名规范化的 UTF-8 代码表候选。当前快照不变；如需接入，后续创建新快照。

## 规则草案重点

取消率/备降率以全部计划记录为分母；延误率和两个到达均值共用“未取消、未备降且所需到达字段完整”的 E 集合。提前到达保留负偏差，非负均值仍包含 0；空分母返回 null。
原因构成只累计已报告分钟并披露缺失，不能把空白当 0，也不能直接作因果推断。原因缺失需区分全体记录与有效延误记录，不能将不要求上报的空白统称为质量错误。
航线取消率演示排名要求至少 100 个计划记录，100 是项目选择；原始异常不自动删改。首版规则只在 2026 年 1 月数据范围内准备，后续扩大适用期必须复核。

## 导入与运行入口

从项目根执行 `uv run alembic upgrade head`，再执行 `uv run python -m metric_pilot.knowledge.seed`；需要显式构建向量时附加 `--embed`，或在知识页操作。指标不会自动批准，第三方本地文件缺失或哈希不符会拒绝导入。
当前操作和限制见 [分析使用说明 v4](project/07-analysis-guide.md)，验收结果见根目录 IMPLEMENTATION_STATUS.md。模型调用使用本机 .env，不输出密钥；本次已验证真实模型路径，准备期的 validation.json 仍只代表当时的材料检查。
