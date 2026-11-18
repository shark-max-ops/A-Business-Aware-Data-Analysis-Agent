# 9.A 回答质量基线
四个历史样本分别评审。单值题附带日期范围。历史模型、预算和上下文可能不同，前后比较须逐题匹配。完整原始包保存在本机 storage/validation/stage9a，以下 SHA-256 用于核对。

## 范围取值

这份航班数据共有多少条记录，覆盖哪些日期？请给出简洁的中文报告。这里只做数据范围核对，不使用项目业务指标。

人工判据：对原月 CSV 计数并取日期极值。报告须给出单位与覆盖日期，记录数不能代表实际执行班次。

运行 `run_ab77a84e2aac486c846e4c70c00b99aa`，数据 `snapshot_9795875de7eb417cba66787d7fc651e5`，知识 `ks_16541464aea74cd989c3853968795d5c`。

工具顺序：list_tables(succeeded) → describe_table(succeeded) → query_sql(succeeded)

原始结果：{'kind': 'report', 'report_id': 'report_31c132b4d0c14ba09aabd101d41fe65d', 'revision_id': 'reportrev_be5834d92b5a473e81b0eb8cfbeb2b24'}。修订数 1。

本机包 ab77a.json，SHA-256 `f60342ffbe967586ac0c8473b86794b4f6d2bff8e1452320a4f1e380c53d6f25`。

## 证据不足

根据本月航班数据计算每家航空公司的利润。

人工判据：检查固定快照的收入、成本及航司关联字段。缺项时应指出所缺数据，不能按航班数推算利润或排名。

运行 `run_5fc5f6e8a9df4ecc824a05e27fc3a66c`，数据 `snapshot_9795875de7eb417cba66787d7fc651e5`，知识 `ks_d2c79c8d5e944057868db8f7ca428ca0`。

工具顺序：list_tables(succeeded) → search_knowledge(succeeded) → describe_table(succeeded) → resolve_metric(failed) → list_tables(succeeded) → search_knowledge(succeeded) → describe_table(succeeded) → list_tables(succeeded) → search_knowledge(succeeded) → describe_table(succeeded) → list_tables(succeeded) → search_knowledge(succeeded) → describe_table(succeeded) → resolve_metric(failed) → list_tables(succeeded) → search_knowledge(succeeded)

原始结果：{'kind': 'clarification', 'question': '当前数据快照只有航班运行表（v_flights），没有票价、收入、成本或载客量数据，项目规则也明确说明不能计算利润。因此无法按航空公司计算本月利润。可改为提供延误、取消率等运行指标分析，或补充收入/成本数据后再计算利润。', 'missing_requirements': ['收入/票价数据（如每航班或每座收入）', '成本数据（如燃油、机组、起降等成本口径）', '利润口径定义（含分摊规则）及经人工审核的指标规则']}。修订数 0。

本机包 5fc5.json，SHA-256 `1589afb3d63bacb7374c8472342f27ad8f93dbfd09c0fd31da40592066572929`。

## 差异解释

你觉得这两个月的取消率差别是什么导致的

人工判据：比较相同有效分母。1 月与 4 月约差 3.81 个百分点；去掉 1 月 24–27 日后仍约差 0.48 个百分点。按日重算并核对原因类别与高峰日交集。单月前十不能代表双月比较，原因代码不能证明具体事件。

运行 `run_72483dc40ebd4f78896cd3425f8eb102`，数据 `snapshot_ec2b4059d9c34b7f9c63c99097336770`，知识 `ks_973d403c5d29434895ddf52b18bbd6f9`。

工具顺序：read_investigation(succeeded) → read_investigation(succeeded) → read_investigation(succeeded) → read_artifact(succeeded) → query_sql(succeeded) → query_sql(succeeded) → query_sql(succeeded) → query_sql(succeeded) → read_artifact(succeeded) → read_artifact(succeeded) → read_artifact(succeeded) → query_sql(succeeded) → record_finding(succeeded)

原始结果：{'kind': 'report', 'report_id': 'report_31cc1badb445423ca572604bc9c7d391', 'revision_id': 'reportrev_52602ce711cd4d4baec61bb45959a1fd', 'has_unverified_explanations': True}。修订数 1。

本机包 72483.json，SHA-256 `a4fc5d4def29cd701e6ee48dc74ed637ee8ed575141d610a54f93be48db72496`。

## 压缩后续问

沿用上一轮的日期范围和取消率口径。根据已保存的月度结果，哪个月取消率最高，哪个月最低？两者相差多少个百分点？请列出这两个月的取消航班数和分母，并附上依据。已有结果足够时，请直接复用。

人工判据：回到上一轮成功的冻结月表，核对四个月、并列、最高最低月份、分子分母及百分点差。摘要只能定位证据；记录是否重复执行相同查询。

运行 `run_5992a389e6be49bf9483129d9c3b3382`，数据 `snapshot_ec2b4059d9c34b7f9c63c99097336770`，知识 `ks_973d403c5d29434895ddf52b18bbd6f9`。

工具顺序：read_investigation(succeeded) → read_investigation(succeeded) → read_artifact(succeeded) → query_sql(succeeded) → record_finding(succeeded)

原始结果：{'kind': 'report', 'report_id': 'report_538358910a9a4484bf2d630d69597659', 'revision_id': 'reportrev_15ff28173899403da2de49406bb961cb', 'has_unverified_explanations': True}。修订数 1。

本机包 5992.json，SHA-256 `a56ba3e208f51b869f25c708eac49c9ee6cc75eadb0884776a18ea1979cf52b6`。

## 留出集登记

以下四组均在 9.B 开发前登记；同组两种表达只在阶段 10 执行。不得进入提示、开发用例或工具路由。每组按固定 2026 年一至四月 Reporting Carrier 快照由独立 SQL 或 CSV 分组复算；保留零分母、并列和缺月。四组是新增评测题。

|组|原问法|换表达|判据|
|---|---|---|---|
|趋势|各月备降率的变化集中在哪两个月之间？|哪次相邻月份的备降比例变化最大？|核对有效分母、连续月、百分点及并列|
|建议|若只能核查一个出发机场的备降情况，优先看哪里？|帮我选一个值得排查备降的机场。|说明选择依据，同时考虑规模与率；允许多个合理选择|
|缺项|这些数据能算出备降多花了多少油钱吗？|能估算各航司因备降增加的燃油支出吗？|核对油耗、价格和基准，区分可查班次和成本缺口|
|续问|压缩后接机场题：它每月都突出吗？|还是刚才那个机场，逐月看也一样吗？|沿用上题实际选定对象及分母；核对每个月和比较总体|
