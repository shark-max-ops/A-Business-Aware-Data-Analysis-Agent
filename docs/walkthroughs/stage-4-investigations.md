# 阶段 4：持续调查怎样工作

## 1. 这一阶段解决什么问题

一次调查可以包含多次分析运行。系统保存要调查的目标、范围和固定版本，把查到的观察、待核查线索、反向证据和限制留存下来，用户可以针对这些发现继续追问。已有报告数字取证仍使用原来的安全路径。

## 2. 一次调查经过哪些模块

```text
用户提出目标与范围 → 服务端创建固定调查
  → 提交本轮问题并分配累计额度 → 原有 Agent 循环执行实际工具
  → 根据返回结果继续核查 → 保存发现与固定依据 → 生成本轮报告
  → 用户继续追问 → 同一调查下的新运行核查与更新发现
```

这表示能力如何连接，不要求每个问题采用固定查询步骤。没有明显发现、资料不足、反证或失败都需要真实展示。

## 3. 代码文件分别负责什么

| 文件 | 用途 |
| --- | --- |
| [investigations.py](../../backend/src/metric_pilot/agent/investigations.py) | 保存调查、累计额度、不可变发现与修正历史；校验跨运行产物权限 |
| [contracts.py](../../backend/src/metric_pilot/agent/contracts.py) | 调查创建、运行关联、发现记录、分页读取和页面 DTO |
| [models.py](../../backend/src/metric_pilot/agent/models.py) 与 [0006_investigations.py](../../backend/migrations/versions/0006_investigations.py) | 调查、关联运行与发现历史三张表 |
| [api.py](../../backend/src/metric_pilot/agent/api.py) | 创建、恢复调查和读取发现依据；续查时沿用固定知识快照 |
| [store.py](../../backend/src/metric_pilot/agent/store.py) | 在创建运行的同一事务中核对版本、并发状态及总额度 |
| [context.py](../../backend/src/metric_pilot/agent/context.py) | 给模型调查目标、范围、短摘录及核查要求，完整工具消息按组裁剪 |
| [tools.py](../../backend/src/metric_pilot/agent/tools.py) | 增加 read_investigation、record_finding；实际引用与数字交给报告服务校验 |
| [runtime.py](../../backend/src/metric_pilot/agent/runtime.py) | 继续使用模型驱动循环；调查报告前要求本轮有核查记录，错误可以有限修正 |
| [reports/service.py](../../backend/src/metric_pilot/reports/service.py) 与 [sources.py](../../backend/src/metric_pilot/reports/sources.py) | 只从获授权运行读取产物及访问来源，冻结本轮报告 |
| [InvestigationPanel.tsx](../../frontend/src/components/InvestigationPanel.tsx) | 目标、范围、发现分类、修正历史、固定依据及后续核查按钮 |
| [AnalysisPage.tsx](../../frontend/src/components/AnalysisPage.tsx) | 创建调查、切换与分支、继续提问、运行进度及报告预览 |

## 4. 具体例子

用户问“本月记录在不同日期的分布有什么值得核查的地方？”模型可以查看结构，查询整体与逐日情况，再按真实结果选择进一步比较。观察数字通过 ValueRef 指向成功结果，服务端取值后保存发现。

用户继续问“这些差异能说明什么？”新运行能读到原调查的发现和固定证据，并补充检查。若原解释证据不足，可以保存反证并指向原发现，页面把原记录收进历史。系统不根据单月记录数差异自动得出天气原因、运行质量或利润结论。

## 5. 验证与限制

[test_investigations.py](../../backend/tests/test_investigations.py) 在隔离数据上执行真实工具、数据库和报告服务。[verify_investigations.py](../../scripts/verify_investigations.py) 使用本机真实模型配置，真实调用的授权、结果和未测范围以 [实施记录](../../IMPLEMENTATION_STATUS.md) 为准。

2026-11-01 已完成 Windows 验收及真实模型四例：首轮调查、根据旧发现继续核查、直接复用旧 ValueRef 的范围复核报告、显式分支中的利润缺数据澄清。原 CSV 独立核对总数和按日期对齐的逐日计数；总数可出现在固定发现或报告中，不限定布局。`--resume-record` 会重新检查既有成功运行，只对缺少的案例请求模型，早期失败历史保留。

手机发现依据弹窗居中，长正文在弹窗内滚动；浏览器等待开启动画结束后检查上下边界、透明度和关闭按钮位置，再保存截图，避免仅凭“元素存在”误判可读性。测试仍使用隔离数据库与模型；真实模型案例另行保存。

调查累计额度采用完整运行的不可退还预分配，取消和失败不会退回，页面不把额度当实际模型消耗。跨调查、不同版本或失败运行的产物不能复用于后续报告。来源运行失败前保存的发现仍可查阅，但需要重新核查才能复用其数字。

业务规则仍由人工审核，引用有效不等于语义或因果已证明。历史天气与资料检索属于阶段 5，用户导入属于阶段 6；这里没有接入外部来源。

## 原始数据探索为什么不要求先批准项目指标

例如调查“提前、准点和晚到的记录分布”，可以先查官方字段含义，再说明统计人群、算法和缺失处理，直接做原始数据探索。用户不需要为了看分布而批准一个项目延误率。模型即使误查了不可用指标，也可以选择继续做独立探索；真正需要正式项目指标时，仍必须使用已审核且日期适用的版本。

[agent/contracts.py](../../backend/src/metric_pilot/agent/contracts.py) 的 SQLArgs 定义明确查询用途与探索说明；[agent/tools.py](../../backend/src/metric_pilot/agent/tools.py) 校验用途、修订及固定版本，再调用原有安全执行器。探索不能绑定正式指标，删掉指标引用也不会自动成为探索。[data/query.py](../../backend/src/metric_pilot/data/query.py) 只在成功后保存探索身份和声明范围，SQL 防护、实际取消和失败无产物的规则继续生效。

[reports/materialize.py](../../backend/src/metric_pilot/reports/materialize.py) 按产物强制保留探索限制，报告补填修订无法把探索变成正式指标；[ExplorationNote.tsx](../../frontend/src/components/ExplorationNote.tsx) 在执行、发现依据和报告依据中展示中文说明，报告预览也标出探索身份。说明与 SQL 是否业务一致仍需复核，不能声称用途枚举证明了语义。

工具签名复用 [agent/context.py](../../backend/src/metric_pilot/agent/context.py) 的 compact_schema 去除展示标题，保留全部参数约束。固定上下文保留最近查询索引、带完整性标记的小型实际结果及少量已查来源短摘录，减少旧工具组裁剪后的重复读取；完整内容仍在工具审计与冻结产物。空间紧张时只进一步缩短预览和摘录，保留工具配对、范围与证据定位，既定预算不扩容，仍放不下则真实失败。未开放函数会返回具体函数名与允许列表，模型应修正或说明可选统计的限制，不得原样重试或把失败变成证据。

## 为什么要给最终报告留出轮数

一次模型回复算一轮，可能包含多次工具调用。用户第二轮调查曾有 18 次工具调用，但已用完 16 轮交互：前面持续查数据，最后才保存发现，修正一次参数后已无机会生成报告。增加轮数只能推迟同类问题。

[agent/runtime.py](../../backend/src/metric_pilot/agent/runtime.py) 现在分配探索、收尾准备和最终提交的工具范围。默认最后五轮进入收尾，前三轮只允许读已有结果、保存发现，最后两轮不再开放工具，让模型提交报告并在引用有误时修正。较早耗尽工具额度或时间也会触发收尾。若模型仍要求新增 SQL，服务端拒绝执行并保存配对工具回复。报告必须继续通过原有 ValueRef、固定版本、归属及本轮发现校验，失败仍显示未完成；不会自动拼一份报告冒充模型完成。

## 历史很多时怎样继续追问

一项发现可能带多个数字引用，批量读取十项时不能把全部引用反复塞进模型上下文。[agent/context.py](../../backend/src/metric_pilot/agent/context.py) 将多项历史整理为可定位概览，保留来源运行状态、短摘录和产物 ID；模型用 detail_offset 与 limit=1 读取需要核对的单项完整内容。原始工具审计、发现及冻结证据没有删减，单项引用保持原样。读取详情后须核对 ID，追加发现后应重新获取当前概览。
