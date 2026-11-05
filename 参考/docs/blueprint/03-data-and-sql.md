# 03 BTS 数据接入与 SQL 执行

## 1. 来源与固定版本

使用 [Reporting Carrier On-Time Performance 官方下载页](https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ)。默认 Year=2026、Period=January，按需选择字段下载 CSV 或官方预打包文件。页面若需要交互或下载失败，提供手动下载和本地导入流程，不依赖不稳定的抓取脚本。

不要同时导入 Marketing Carrier 和 Reporting Carrier 两张表。不要把官方月度新闻稿中的汇总数当成当前 CSV 的标准行数。首次成功导入后生成清单并人工核对该表口径，后续复现依赖清单中的哈希。

原始 CSV 不直接提交 Git；README 给出原始网址、获取日期、选择条件与校验方法。仓库提供明确标记的少量合成 fixture，以便无网络测试。离线 fixture 测试不需要付费模型。

## 2. 字段选择与标准化

至少选择下表字段，保留原始列名到内部列名的映射。字段描述来自 [BTS 字段字典](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ)，内部表名、类型和分层是项目设计。

| BTS 字段组 | 内部列示例 | 用途 |
| --- | --- | --- |
| FlightDate / Reporting_Airline / DOT_ID_Reporting_Airline | flight_date / carrier_code / carrier_id | 运行日期和承运人身份 |
| Flight_Number_Reporting_Airline / Tail_Number | flight_number / tail_number | 航班标识和辅助诊断；不能单独用航班号作主键 |
| OriginAirportID / DestAirportID / Origin / Dest | origin_airport_id / dest_airport_id / origin_code / dest_code | 出发、到达机场关联 |
| CRSDepTime / CRSArrTime / DepTime / ArrTime | scheduled_dep_local / scheduled_arr_local / actual_dep_local / actual_arr_local | 保留地方时 HHMM 原义 |
| DepDelay / ArrDelay / ArrDelayMinutes / ArrDel15 | departure_delay / arrival_delay / arrival_delay_nonnegative / arrival_delayed_15 | 偏差、非负分钟数与阈值指标 |
| Cancelled / Diverted / CancellationCode | cancelled / diverted / cancellation_code | 航班状态与取消分类 |
| Distance / AirTime / CRSElapsedTime / ActualElapsedTime | distance_miles / air_time / scheduled_elapsed / actual_elapsed | 距离与时长 |
| CarrierDelay / WeatherDelay / NASDelay / SecurityDelay / LateAircraftDelay | 五类 delay_* 列 | 上报的原因分钟数 |

如下载文件不含某列，导入器列出缺失项并拒绝需要该列的功能，不用全零填充。BTS HHMM 可能涉及跨日、2400 等情况；本项目主要用官方延误/时长字段，避免直接对两个 HHMM 相减计算分钟数。

## 3. 数据库组织

```text
原始 CSV
  → 列映射与严格类型检查
  → raw_flights（只在离线导入连接可写）
  → v_flights（标准化业务视图，对 Agent 开放）
  → dim_carriers / dim_airports（官方代码表，唯一键核验后开放）
```

当前月份无法获取代码表时，可以先显示代码；不能由 LLM 猜测公司名称。数据接口返回可用维度列表。机场维表同时关联 origin/dest 时分别起别名，不重复扩张事实行。

主键 `record_id` 可使用 `source_file_hash + source_row_number` 的哈希。这是来源定位标识，不声称它是官方自然主键。保留源行号；重复内容先报告，不自动删除，除非有明确去重策略和审计记录。

导入流程：

1. 验证日期范围、列、数字转换和取消/备降标记。
2. 先建临时目录，写入新 DuckDB 文件；不覆盖当前正在使用的快照。
3. 生成来源清单，记录空值、异常值、日期越界、重复候选。
4. 维表按键检查唯一性，验证 JOIN 前后事实行数。
5. 完成并关闭写连接后，将临时目录原子转为 snapshot 目录。
6. 在 SQLite 事务中登记 DataSnapshot；用户显式设为当前快照。

不要丢掉负延误，它表示提前到达；不要把取消航班缺失的到达时间当作零延误。带有效日期却落在所选月份之外的行保留在原始文件与质量报告中，标准视图按导入策略处理并标注数量，不能静默消失。

## 4. 导入清单契约

```json
{
  "source": "BTS Reporting Carrier On-Time Performance",
  "source_url": "https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ",
  "period": "2026-01",
  "downloaded_at": "ACTUAL_UTC_TIME",
  "source_sha256": "COMPUTED_FROM_FILE",
  "schema_version": "bts_reporting_v1",
  "rows_raw": 0,
  "rows_exposed": 0,
  "quality_report_hash": "COMPUTED_FROM_REPORT",
  "importer_commit": "ACTUAL_COMMIT_OR_DIRTY_MARKER"
}
```

这里的 0 是契约占位，不是数据集的实际数量。实际清单必须写真实结果，包含列映射、异常处理策略、代码表哈希和适用规则文档版本。

## 5. 数据连接

前端只显示已登记的数据源和快照，不能让用户把任意文件路径传给模型执行器。

```python
from pathlib import Path
import duckdb

def open_analysis_connection(snapshot_dir: Path):
    # snapshot_dir 必须先由服务端根据 snapshot_id 查询、解析并验证在 storage 根内。
    return duckdb.connect(
        str(snapshot_dir / "analytics.duckdb"),
        read_only=True,
        config={
            "enable_external_access": "false",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "threads": "2",
            "memory_limit": "512MB",
            "max_temp_directory_size": "0B",
        },
    )
```

这是连接初始化示例。实现时必须用锁定的 DuckDB 版本实际测试配置支持及拒绝行为；不支持的设置应导致启动诊断，不无声忽略。导入连接独立运行，允许写新快照；分析连接从不复用导入连接。参考 [DuckDB 配置文档](https://duckdb.org/docs/stable/configuration/overview) 和 [Python 连接文档](https://duckdb.org/docs/stable/clients/python/dbapi)。

## 6. SQL 防护边界

只读连接不意味着没有副作用：读取外部文件、网络访问、扩展加载和资源消耗都需要约束。

校验顺序：

1. 用 sqlglot DuckDB 方言解析完整 SQL，必须恰好一条 SELECT/UNION 等查询表达式。
2. 拒绝写操作、DDL、COPY、ATTACH、DETACH、PRAGMA、SET、INSTALL、LOAD、SELECT INTO 等节点，含子查询和 CTE 内部。
3. 分析作用域，区分 CTE 名和物理表名；只允许登记过的业务视图和维表。拒绝跨 catalog/schema 访问与未允许的系统目录。
4. 拒绝表函数、外部读取函数和未审核函数。采用有限函数允许集：聚合、CASE、常用日期/字符串/数值操作。遇到新函数先加测试再开放。
5. 参数只允许命名键到标量值的绑定，不通过字符串拼接。
6. 独立子进程打开受限只读连接，执行并限制结果量；父进程用墙钟 deadline 终止超时进程。
7. 成功结果序列化为产物，返回摘要；失败只保存执行记录。

不要把以下代码当作完整 SQL guard：

```python
# 仅是接口和最外层检查示意，scope/function/AST 细节必须另行实现与测试。
from sqlglot import parse, exp

def parse_single_query(sql: str) -> exp.Query:
    roots = [node for node in parse(sql, read="duckdb") if node is not None]
    if len(roots) != 1 or not isinstance(roots[0], exp.Query):
        raise ValueError("ONLY_ONE_QUERY_ALLOWED")
    return roots[0]
```

完整 guard 应产出：原始 SQL、标准化 SQL、读表清单、函数清单、参数键、校验版本。异常不得原样暴露本机文件路径或连接配置。

`asyncio.wait_for(asyncio.to_thread(query))` 不能可靠终止底层查询；本项目要求子进程级超时。Windows 使用 spawn，入口函数定义在模块顶层，并保证主入口保护和取消后子进程回收。

## 7. 结果限制与完整性

- 默认最多保存 1000 行，预览最多 20 行，序列化总大小另设 2MB 上限。
- 使用 AST 在最外层限制为上限+1 行或等效方式；不得修改内部聚合口径。超过上限时记录 `truncated=true`。
- 被截断的结果可以预览，不能作为完整图表数据或总量结论来源；Agent 应用 SQL 再聚合或显式缩小范围。
- 导出完整大结果暂不实现。`LIMIT 1000` 不是执行时间限制。
- 记录数值类型和 null；Decimal 使用规范十进制字符串和类型标签保存，避免 JSON 浮点改变精度。非有限数值不能进入报告。

## 8. 一个项目指标的 SQL 示例

以下是 **项目自定义的有效到达口径**，不是直接复刻官方新闻稿的准点率。定义见 04：排除取消和备降、到达偏差与阈值标记均有效。时间左闭右开。

```sql
SELECT carrier_id,
       COUNT(*) AS eligible_arrivals,
       SUM(CASE WHEN arrival_delayed_15 = 1 THEN 1 ELSE 0 END) AS delayed_arrivals,
       100.0 * SUM(CASE WHEN arrival_delayed_15 = 1 THEN 1 ELSE 0 END)
           / NULLIF(COUNT(*), 0) AS delay_rate_pct,
       AVG(arrival_delay) AS mean_arrival_deviation_min,
       AVG(arrival_delay_nonnegative) AS mean_nonnegative_delay_min
FROM v_flights
WHERE flight_date >= $start_date AND flight_date < $end_date
  AND cancelled = 0 AND diverted = 0
  AND arrival_delay IS NOT NULL
  AND arrival_delay_nonnegative IS NOT NULL
  AND arrival_delayed_15 IN (0, 1)
GROUP BY carrier_id
ORDER BY delay_rate_pct DESC, carrier_id;
```

上例函数应纳入允许集。执行参数类型与 DuckDB 实测绑定一致。取消率另查全体计划记录，不复用已排除取消的分母。

## 用户导入与调查数据准备（阶段 6，待实现）

阶段 5.C 已增加独立天气查询快照 `v_weather_observations`，不注册为 BTS 主数据源，不跨库拼接。主快照检查保留，天气版本由服务端额外绑定并校验；复用同一 AST、只读子进程、结果冻结和真实取消。天气表数值采用通过筛选的精确温度、风速和能见度；界限、缺失、冲突及原件定位单列保留，降水/积雪暂不开放累计列。详见 [05 外部证据边界](05-evidence-and-reports.md#5c-已实现的外部证据边界)。

在现有 BTS 专用导入之外，支持用户提供自己的表格数据与业务材料。明确受支持格式、大小、编码、字段与类型，展示真实解析和质量报告，包括缺失、重复、异常值及日期或单位歧义；未知含义由用户确认，不以模型猜测当作业务事实。来源、原文件哈希、转换策略和确认记录进入清单；导入失败不生成可分析的成功快照，新快照不覆盖旧版本。

发布为可查询快照前，明确可用表与字段并复用 AST 校验、允许表与函数、受限只读连接、子进程取消与结果限制。用户材料进入版本化知识与审核流程，生成内容不能自动成为正式规则。文件内容属于不可信输入；上传到外部模型服务之前应明确实际发送范围并遵守已有授权和配置。

## 9. 必测情况

多条语句；注释伪装；WITH 内写入；SELECT INTO；外部 CSV/Parquet 读取；网络 URI；扩展加载；超大范围生成；未允许函数；CTE 别名；含空值的聚合；取消/备降/提前到达；结果截断；超时与取消后无残留查询进程。

---

<a id="decision-0002"></a>

## 实施设计取舍 0002：阶段 1：数据快照与受限查询

本节保留实施时的阶段边界及后续补充；当前完成状态以根目录 IMPLEMENTATION_STATUS.md 末尾为准。

依据蓝图 01—08，重点采用 03 的导入和 SQL 边界、05 的 QueryArtifact、06 的数据源 API。参考源码仅阅读包内 `参考/docs/blueprint/references/dbgpt/10-sql-read-only.py.txt`，未复制源码或安装 DB-GPT。

#### 设计取舍

- 参考 DB-GPT 将查询检查与执行、产物与展示分开。该快照使用完整语句检查；本项目额外使用 sqlglot AST 默认拒绝白名单、作用域解析、函数/表允许集，结合 DuckDB 受限只读连接和 spawn 子进程超时，不能仅凭 SELECT 前缀判断安全。
- 一个业务分析引擎 DuckDB，SQLite 仅保存目录和执行审计。导入连接可写，只写新临时目录；分析连接只读且禁止外部访问、自动扩展安装/加载，内存与临时磁盘预算固定。锁定版本必须实际支持全部设置。
- 原始输入先复制并计算哈希，后续解析读取复制件。发布前保留 CSV、DuckDB、manifest、quality 和 schema，关闭写连接后原子重命名，最后在事务中登记。登记失败可能留下不被引用的文件，但不能发布不完整快照。
- 合成和用户声明的官方 BTS 来源分开登记。出现 Marketing 字段就拒绝混用。未知或缺失基线字段不默认为零；当前版本要求完整基线字段，列不全则明确拒绝整个导入。
- 日期越月记录留在 raw_flights 和源 CSV，v_flights 按所选月过滤，质量报告显示数量。重复候选不删除。机场/承运人表必须唯一键，逐条验证 JOIN 前后事实数量。
- RunContext、ToolResult、QueryArtifact 沿用蓝图契约，以组合服务实现。阶段 1 只有手动 CLI 查询作用域和 QueryExecution 审计，run_id 由服务端生成；并未提前实现 Agent Run 状态机、工具选择或 SSE。knowledge_empty_v1 表示尚无知识快照，证据中的规则引用列表为空，不声称口径已自动验证。
- 外层 AST 限制最多预算+1 行，不改写内部聚合。达到行/字节上限明确标记 truncated。Decimal 与超过 JavaScript 安全整数范围的值用类型标签和十进制字符串冻结，null 不转为零，非有限数值拒绝。
- 先写内容寻址产物，再事务提交成功执行与 QueryArtifact。失败、超时、取消均只保留执行审计，不生成成功证据。产物读取验证 run 关联、成功状态、内容哈希，且不重新执行 SQL。
- 不提供模型可控制的路径、任意插件、脚本或配置设置。当前网页只查看快照和结构、显式激活默认版本；SQL 仅通过手动 CLI 和内部服务执行，后续阶段再接入真实工具调用。

#### 验证方向

实际 DuckDB 导入与聚合核对项目有效到达分母、取消/备降与零分母；拒绝多语句、写 CTE、外部读取、系统目录和新函数；验证 CTE 别名与 UNION 可用；验证超时/取消发生在实际执行阶段且进程被回收；验证截断、空结果、Decimal、大整数、内容哈希与跨 run 拒绝；快照切换不覆盖旧文件。

实际检查命令与结果以根目录 IMPLEMENTATION_STATUS.md 为准。真实数据尚未取得时不把 fixture 行数或结果写成 BTS 官方规模。


#### 真实 CSV 接入后的修正

用户下载的 T_ONTIME_REPORTING 导出使用 OP_UNIQUE_CARRIER、OP_CARRIER_AIRLINE_ID、OP_CARRIER_FL_NUM、TAIL_NUM、ARR_DELAY_NEW 等名称，以及美式午夜日期。明确增加与页面字段标签的一对一映射，拒绝同义列冲突并补充 MKT 字段拒绝；未引入多数据源兼容层。

源文件中实际发现 4 条负计划时长。将有限数值的时长范围异常与结构/类型错误分开：保留原值和记录，在质量报告中提供计数及源行定位。负非负延误字段、非有限数字和无效标记仍被拒绝，不为通过导入而填零或删除数据。

Windows 的 mkdtemp 私有 ACL 随重命名保留，导致跨执行账户访问发布目录失败。现在用工作区根下的 UUID 临时目录并继承父目录权限，回归检查发布目录不阻断继承。本次验收只修复新生成真实快照的继承，没有改写原 CSV 或旧快照数据。

独立验收脚本使用标准库和 Decimal 直接计算原 CSV 基线，与正式受限 SQL 的冻结产物逐项比较，避免把同一导入器产生的答案直接当作 gold。慢查询进度使用连接级 SET 关闭，不放入 DuckDB 全局连接配置。
