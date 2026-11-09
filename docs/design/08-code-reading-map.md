# 08 代码阅读与设计核查路线

本路线直接定位当前项目源码，用于理解一次调查怎样经过模型、工具、数据、知识与报告服务。各阶段的详细执行流程见[代码讲解目录](../walkthroughs/README.md)，实际验收范围见[实施记录](../../IMPLEMENTATION_STATUS.md)。

## 1. 按执行流程阅读

| 顺序 | 代码入口 | 阅读重点 |
| --- | --- | --- |
| 1 | [应用入口](../../backend/src/metric_pilot/main.py)、[配置](../../backend/src/metric_pilot/settings.py) | 服务生命周期、配置、后台 worker 与本地部署边界 |
| 2 | [运行 API](../../backend/src/metric_pilot/agent/api.py)、[运行存储](../../backend/src/metric_pilot/agent/store.py) | 创建运行时固定快照、预算和 ID，状态转换、取消与 SSE 重放 |
| 3 | [运行循环](../../backend/src/metric_pilot/agent/runtime.py)、[模型适配](../../backend/src/metric_pilot/agent/provider.py) | 原生工具调用、消息配对、失败修正与收尾 |
| 4 | [工具注册](../../backend/src/metric_pilot/agent/tools.py)、[外部工具](../../backend/src/metric_pilot/agent/external_tools.py) | 工具签名、参数校验、执行权限和共享预算 |
| 5 | [上下文](../../backend/src/metric_pilot/agent/context.py)、[生命周期观测](../../backend/src/metric_pilot/agent/middleware.py) | 固定目标、完整工具配对裁剪、审计与事件 |
| 6 | [调查管理](../../backend/src/metric_pilot/agent/investigations.py) | 发现、限制、累计额度及跨运行证据授权 |
| 7 | [数据导入](../../backend/src/metric_pilot/data/ingest.py)、[查询服务](../../backend/src/metric_pilot/data/query.py)、[SQL 校验](../../backend/src/metric_pilot/data/guard.py)、[执行器](../../backend/src/metric_pilot/data/executor.py) | 不可变数据快照、AST 允许集、只读连接与查询子进程 |
| 7a | [6.A 原件核验](../../backend/src/metric_pilot/data/audit.py)、[独立复核](../../scripts/verify_bts_audit.py) | 只读原 CSV，登记哈希、字段、日期、质量与用户来源声明，不创建或激活快照 |
| 7b | [6.B 多月快照](../../backend/src/metric_pilot/data/multimonth.py)、[独立验收](../../scripts/verify_multimonth.py) | 复用单月规范化，校验6.A哈希，逐月来源与范围、实际取消及原子发布；范围调查见6.C，页面见6.D |
| 7c | [6.C 日期范围](../../backend/src/metric_pilot/data/ranges.py)、[范围测试](../../backend/tests/test_date_ranges.py)、[真实核验](../../scripts/verify_date_ranges.py) | 左闭右开范围贯穿调查、只读子进程、指标和报告；证据复用须范围相同，日期选择页面见6.D |
| 7e | [6.E验收器](../../scripts/verify_multimonth_investigation.py)、[反例测试](../../backend/tests/test_multimonth_verification.py)、[真实页面](../../frontend/tests/multimonth-live.spec.ts) | 独立CSV日/承运人基线、正式口径与探索、旧证据及范围核对；语义人工审查，失败记录不覆盖 |
| 7d | [6.D 日期表单](../../frontend/src/components/DateRangeFields.tsx)、[覆盖与质量](../../frontend/src/components/SnapshotSummary.tsx)、[页面范围检查](../../frontend/src/api/data.ts) | 新调查显式选择，续查只读继承，新调查/分支草稿隔离；默认版本只影响后续初始选择 |
| 8 | [资料管理](../../backend/src/metric_pilot/knowledge/documents.py)、[检索](../../backend/src/metric_pilot/knowledge/retrieval.py)、[Wiki](../../backend/src/metric_pilot/knowledge/wiki.py)、[指标](../../backend/src/metric_pilot/knowledge/metrics.py) | 原文修订、来源定位、草稿发布与指标审核 |
| 9 | [外部 HTTP](../../backend/src/metric_pilot/external/http.py)、[证据冻结](../../backend/src/metric_pilot/external/evidence.py) | 允许来源、真实请求取消、原件哈希与外部版本 |
| 10 | [报告服务](../../backend/src/metric_pilot/reports/service.py)、[取值](../../backend/src/metric_pilot/reports/values.py)、[物化](../../backend/src/metric_pilot/reports/materialize.py) | 从成功冻结产物取数、校验引用与保存不可变报告 |
| 11 | [分析页面](../../frontend/src/components/AnalysisPage.tsx)、[逐轮结果](../../frontend/src/components/RunTurn.tsx)、[执行记录](../../frontend/src/components/ExecutionTrace.tsx) | 调查与发送目标、连续轮次、活动 SSE、历史记录按需读取及显式取消 |
| 12 | [逐轮调查内容](../../frontend/src/components/RunDetails.tsx)、[调查汇总与发现](../../frontend/src/components/InvestigationPanel.tsx)、[发现展示转换](../../frontend/src/components/investigationPresentation.ts) | 顶部汇总与逐轮内容并存、跨轮修正归组、实际引用来源筛选与来源运行归属 |
| 13 | [事件日志读取](../../frontend/src/api/eventLog.ts)、[历史与冻结内容读取](../../frontend/src/api/runHistory.ts) | 响应契约与分类错误、部分分页重试、读取并发及固定资源缓存 |
| 14 | [数据抽屉](../../frontend/src/components/DataSourceDrawer.tsx)、[证据抽屉](../../frontend/src/components/EvidenceDrawer.tsx) | 工作区数据管理与逐轮固定快照只读模式、不可变依据的按需读取 |

## 2. 关键设计问题

- 模型提出的动作在哪里通过权限、预算和参数检查？运行与快照 ID 是否由服务端决定？
- 失败、超时和取消是否终止真实操作？失败执行是否可能产生可引用的成功产物？
- 上下文裁剪是否保留原始目标、固定范围和完整工具消息配对？审计是否保持完整？
- Wiki 摘要、项目指标与官方原文是否分别标记？更新是否创建新修订并保留旧快照？
- 报告数字是否从冻结结果取值？引用校验是否与业务语义判断明确区分？
- 同调查继续核查时，是否核对旧运行成功状态、会话归属、主快照和外部证据版本？
- 顶部汇总是否与逐轮发现区分？后轮修正后，原轮次的发现和历史建议是否仍可查阅？
- 记录未读、部分读取与读取失败是否真实区分？逐轮数据入口是否锁定原快照，引用材料是否实际绑定且保留来源运行？
- 连续历史展示是否与模型实际收到的上下文区分，定位轮次是否只影响阅读而不改变发送目标？

## 3. 设计取舍

采用单 Agent 和串行分析 worker，将当前复杂度集中在规则、查询与证据的一致性上。模型决定调查方向，服务端控制执行边界；不以增加编排层替代权限与结果校验。

Wiki 用于理解与导航，已审核指标定义固定计算口径；两者分别修订，避免生成摘要改变正式分母。数据、知识和报告均保留版本，使后续更新不会改变旧调查依据。

报告与发现保留事实、候选、限制和建议的区别。冻结产物和有效引用使数字可回查，但模型解释仍需人工核对；相关关系、上报原因和外部事件描述不能自动构成因果证明。

## 4. 检查入口

[后端测试](../../backend/tests)、[前端浏览器测试](../../frontend/tests)和[定向验证脚本](../../scripts)覆盖不同层次。运行命令见[根 README](../../README.md)。测试替身、真实联网、真实模型和人工有限样本复核分别记录，不能互相替代，也不据此宣称整体准确率已完成评估。
