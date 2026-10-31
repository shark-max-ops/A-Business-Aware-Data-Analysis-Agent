# 阶段 1：导入、快照、受限 SQL 与查询产物

更新日期：2026-10-31（Asia/Shanghai）。[返回导读目录](README.md)。

## 1. 阶段目标

阶段 1 建立可信的数据执行基础：把真实 BTS Reporting Carrier CSV 导入固定快照，让查询在安全边界内执行，并把成功结果保存成可核对的产物。

本阶段已经可以手动执行 SQL，但当时还没有模型选择工具的 Agent 循环。阶段 3 是在这套执行基础上接入模型，没有另外放开一条不受约束的查询通道。

## 2. 执行流程

导入与查询是两条分开的路径：

```text
导入：CSV
  → 保存原始字节并计算哈希
  → 校验字段、类型、日期与状态
  → 建立 raw_flights 和公开业务视图
  → 写 manifest、quality、schema
  → 发布新快照并登记目录

查询：固定 RunContext + SQL + 参数
  → 查询目录取得服务端快照
  → SQL AST 检查
  → 子进程打开受限只读连接
  → 执行并限制结果量
  → 成功保存 QueryArtifact；失败只保存执行记录
```

快照可以理解成“这次分析要使用的一份固定数据版本”。切换默认快照只是修改默认选择，不覆盖已经存在的数据版本或查询结果。

## 3. 代码文件讲解

### 3.1 `ingest.py`：把来源文件变成固定数据

[ingest.py](../../backend/src/metric_pilot/data/ingest.py) 的 `ingest()` 是导入服务，`main()` 提供本地命令入口。

`copy_hashed()` 保存输入字节并计算哈希；`normalize_header()` 和 `parse_value()` 负责字段映射及类型解析。程序支持实际 BTS 导出中的字段别名，同时拒绝同义列冲突和已知 Marketing 字段，避免把 Reporting 与 Marketing Carrier 混用。

导入保留提前到达的负偏差，不把取消航班缺失的到达信息填成零。源文件里的有限负时长保留原值，在质量报告中标记；它们与“提前到达”是不同问题。

原始记录进入 `raw_flights`，对分析开放的 `v_flights` 按导入月份暴露。可选维表由 `import_dimension()` 校验唯一键与关联数量，缺维表时显示代码，不让模型猜公司或机场名称。

### 3.2 `catalog.py`：由服务端决定数据位置

[catalog.py](../../backend/src/metric_pilot/data/catalog.py) 的 `Catalog` 管理来源、快照、公开结构和默认快照。

`database_path()` 根据已登记的快照 ID 定位受约束的数据文件；`schema()` 返回该快照允许查询的表与列；`activate()` 显式设置默认版本。页面和后续 Agent 传递的是登记 ID，不是任意磁盘路径。

[data/models.py](../../backend/src/metric_pilot/data/models.py) 保存来源、数据快照、查询执行与产物记录；[0002_data.py](../../backend/migrations/versions/0002_data.py) 建立对应数据库结构。

### 3.3 `guard.py`：先检查整棵 SQL 语法树

[guard.py](../../backend/src/metric_pilot/data/guard.py) 的 `validate_sql()` 使用 sqlglot 按 DuckDB 方言解析完整 SQL。

它检查单条查询、写入与管理操作、表引用、作用域、函数及参数，拒绝外部文件读取和不在允许集中的访问。检查不会因为 SQL 文本以 `SELECT` 开头就直接放行。

这个文件还构造外层结果行数限制，保持内部聚合口径；超过保存预算的结果必须带截断标记。行数上限与执行超时是两件事，`LIMIT` 不能替代超时。

### 3.4 `executor.py`：把查询放进可终止的子进程

[executor.py](../../backend/src/metric_pilot/data/executor.py) 中，`open_analysis_connection()` 打开只读连接并关闭外部访问、自动扩展安装和加载；`query_child()` 在独立进程里执行查询。

`QueryExecutor.execute()` 监督这个进程，检查查询超时、运行总 deadline 和显式取消，必要时终止并回收子进程。使用子进程是为了能结束实际查询，而不只是让一个等待线程先返回“超时”。

### 3.5 `query.py`：连接检查、执行与证据保存

[query.py](../../backend/src/metric_pilot/data/query.py) 的 `QueryService.execute()` 把上述模块组合起来：登记执行尝试、检查 SQL、调用执行器，成功后写冻结结果并提交产物记录。

`read_artifact()` 根据运行归属读取已有结果，校验成功状态和内容哈希，再提供有限分页；它不会重新查询当前数据库来填旧结果。

[data/contracts.py](../../backend/src/metric_pilot/data/contracts.py) 定义三个关键对象：

| 对象 | 含义 |
| --- | --- |
| `RunContext` | 服务端绑定的运行、数据/知识快照与截止时间 |
| `ToolResult` | 工具是否成功、摘要、错误码与产物 ID |
| `QueryArtifact` | 成功查询的 SQL、参数、版本、列类型、结果哈希与截断信息 |

[serialization.py](../../backend/src/metric_pilot/data/serialization.py) 的 `encode_cell()` 保留 null、日期、Decimal 和大整数的类型与精度。当前 `query.py` 还有阶段 3 增加的指标/知识修订绑定，这部分不是阶段 1 当时已经完成的业务口径验证。

### 3.6 API 与数据源抽屉：让用户查看实际数据范围

[data/api.py](../../backend/src/metric_pilot/data/api.py) 提供来源、快照、默认激活、schema 和产物读取接口。

[DataSourceDrawer.tsx](../../frontend/src/components/DataSourceDrawer.tsx) 通过 [api/data.ts](../../frontend/src/api/data.ts) 展示真实行数、月份、字段、样本和质量报告，帮助用户先确认“正在查哪份数据”。

## 4. 具体例子

假设需要查看各承运人的计划记录数。阶段 1 可以通过手动 CLI 提交聚合 SQL，由服务端创建执行上下文，经过 guard 和子进程执行后返回真实产物。

如果把查询换成读取外部 CSV，guard 会拒绝；如果查询确实开始后被取消，执行器会终止实际子进程；两种情况都不能生成成功证据。

既有真实验收记录中的固定快照包含 **544,003 条** 2026 年 1 月 Reporting Carrier 记录，保留并标记了 **4 条负计划时长**。这些是历史实际文件检查结果，不是程序预填的正确答案，本文没有重新扫描原始数据。

## 5. 验证与限制

[test_data.py](../../backend/tests/test_data.py) 覆盖真实 DuckDB 执行、SQL 防护、类型、截断、哈希、跨运行访问，以及查询开始后的超时和取消回收。[verify_bts_snapshot.py](../../scripts/verify_bts_snapshot.py) 独立读取原 CSV，用标准库和 Decimal 计算基线，再对照正式查询产物，减少用同一导入逻辑自证正确的风险。

历史验收结果与环境限制见 [实施记录](../../IMPLEMENTATION_STATUS.md)；本文创建时没有重新导入数据、执行查询或跑完整回归。

查询成功只能证明这一条 SQL 在受限环境执行成功。它不自动证明分母正确，也不等于已经生成可追溯报告。截断明细不能冒充完整统计，Reporting 与 Marketing 口径不能混用。
