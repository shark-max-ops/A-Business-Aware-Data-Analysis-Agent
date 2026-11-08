# 03 BTS 数据接入与 SQL 执行

## 1. 来源与固定版本

使用 [Reporting Carrier On-Time Performance 官方下载页](https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ)。已有一月基线；阶段 6.A 核验本地 2026 年一至四月文件，逐月选择相同 29 字段。页面若需要交互或下载失败，提供手动下载和本地导入流程，不依赖不稳定的抓取脚本。

不要同时导入 Marketing Carrier 和 Reporting Carrier 两张表。不要把官方月度新闻稿中的汇总数当成当前 CSV 的标准行数。首次成功导入后生成清单并人工核对该表口径，后续复现依赖清单中的哈希。

原始 CSV 不直接提交 Git；README 给出原始网址、获取日期、选择条件与校验方法。仓库提供明确标记的少量合成 fixture，以便无网络测试。离线 fixture 测试不需要付费模型。

<a id="bts-download"></a>

### 下载和存放月度文件

1. 打开上方 Reporting Carrier 官方下载页，确认表名为 **Reporting Carrier On-Time Performance**，不要选择 Marketing Carrier。
2. `Filter Year` 选 `2026`，`Filter Period` 分别选需要的月份（本轮为一至四月），地理范围保持全部；逐月下载，不混合两种承运人口径。
3. 按下面 29 个字段勾选后点击 Download。下载 ZIP 后解压 CSV；表头可能导出为 FL_DATE、OP_UNIQUE_CARRIER、ARR_DELAY_NEW 等，已有导入器处理别名，不需手动改表头。
4. 文件统一放入下列路径。一月已经归位；用户提供的新月份文件需另行核验，不创建空占位文件：

```text
data/bts/
  2026-01.csv   已有一月原始文件
  2026-02.csv   用户下载的二月原件，放入后核验
  2026-03.csv   用户下载的三月原件，放入后核验
  2026-04.csv   用户下载的四月原件，放入后核验
```

| 分组 | 勾选字段 |
| --- | --- |
| 日期 | `FlightDate` |
| 航空公司与航班 | `Reporting_Airline`、`DOT_ID_Reporting_Airline`、`Flight_Number_Reporting_Airline`、`Tail_Number` |
| 机场 | `OriginAirportID`、`DestAirportID`、`Origin`、`Dest` |
| 计划与实际时间 | `CRSDepTime`、`CRSArrTime`、`DepTime`、`ArrTime` |
| 延误 | `DepDelay`、`ArrDelay`、`ArrDelayMinutes`、`ArrDel15` |
| 取消与备降 | `Cancelled`、`Diverted`、`CancellationCode` |
| 航距与时长 | `Distance`、`AirTime`、`CRSElapsedTime`、`ActualElapsedTime` |
| 已上报原因 | `CarrierDelay`、`WeatherDelay`、`NASDelay`、`SecurityDelay`、`LateAircraftDelay` |

一月文件从 `data/T_ONTIME_REPORTING.csv` 移至 `data/bts/2026-01.csv`，字节和 SHA-256 不变；历史清单中的旧路径保留为当时位置。已有 `storage/datasets/` 冻结副本、旧调查与报告不移动、不重新导入。运行脚本和当前操作说明使用新路径。月度原始 CSV 继续由根目录 `.gitignore` 的 `/data/` 排除，不提交 Git。

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

## 多月 BTS 数据准备（阶段 6.A 已实现）

本轮不建设通用 CSV/XLSX 上传或任意业务表语义映射。6.A 已提供 [离线核验模块](../../backend/src/metric_pilot/data/audit.py) 与 [合成测试](../../backend/tests/test_bts_audit.py)，直接读取月度 CSV，不连接应用数据库、不调用模型或网络、不登记快照。6.B 已提供下述多月数据快照；调查范围、跨期口径与页面仍待 [6.C—6.E](07-build-and-evaluate.md)，现有单月导入继续可用。

```powershell
uv run python -m metric_pilot.data.audit --input-dir data/bts --periods 2026-01 2026-02 2026-03 2026-04 --output-dir storage/validation/bts-6a/my-audit
uv run python scripts/verify_bts_audit.py --manifest storage/validation/bts-6a/my-audit/manifest.json --input-dir data/bts --output storage/validation/bts-6a/my-audit/independent-check.json
```

核验目录必须新建，拒绝覆盖历史结果或将结果目录设在原件目录内。真实清单及验收结果位于忽略的 `storage/validation/`，不提交真实原件、明细或本机路径。`manifest.json` 包含文件字节、扫描前后 SHA-256、29 字段映射、额外/缺失/冲突列、全量行数、每日覆盖、空白/非法字段、取消/备降、负时长、一致性检查、重复候选及最多五条异常行定位；`summary.md` 提供阅读摘要。坏行或解析失败不能当作已完整核验。

原始全字段重复、规范29字段重复和航班候选键重复分别计数，均为每组首条以外的行数，不自动删行。候选键使用日期、Reporting Carrier DOT ID、航班号、出发/到达机场ID、计划出发时间，不声称它是唯一键。空白不填零；空白计数仅覆盖格式完整且字段可定位的行，缺列和坏行另记。负到达偏差允许，负时长单独保留质量提示；缺少日期仅表示文件与预期日历存在差异，不直接证明官方数据遗漏。

退出码 0 只表示所选月份没有技术阻断，不表示官方来源已认证或质量无异常；2 表示缺文件、缺月、坏行、字段错误、越月/重叠日期、重复文件或扫描不完整等阻断。来源默认待确认，获取时间未知留空；仅在用户已明确确认所选文件来自 Reporting Carrier 且地理范围全部时，追加 `--user-confirmed-reporting-all`，声明绑定本次文件哈希，仍保留 `provenance_verified=false`。独立复核脚本复用既有 `verify_bts_snapshot.profile_csv` 的独立原始列映射及 datetime/Decimal 计算，核对主要统计；该脚本当前针对官方导出别名表头，不验证下载凭据、重复候选或业务结论。

本地一至四月实际核验、用户来源确认、质量异常及未测范围见 [实施日志](../../IMPLEMENTATION_STATUS.md)。6.B 依据该清单重新核对输入哈希，保留异常与来源声明，创建新的不可变多月快照。

### 6.B：不可变多月数据快照

[多月导入模块](../../backend/src/metric_pilot/data/multimonth.py) 调用现有 `ingest.prepare_month` 完成各月原件冻结、别名检查、类型解析与质量计算，在本次临时 DuckDB 中逐月装载；正式数据必须提供6.A清单并重新核对哈希、行数、空值、负时长与重复候选等。月份必须连续且不重复，实际日期必须属于来源月，正式数据须覆盖完整日历；不能仅依赖清单自称通过。合成样本以 `--synthetic` 显式隔离，允许部分日覆盖以测试边界。命令见根 README。

`raw_flights` 和允许查询的 `v_flights` 增加 `source_file_id`、`source_period`；`source_row_number` 保留原始CSV逻辑记录号，`record_id` 仍按原件哈希与记录号生成。冻结原件保存为 `sources/YYYY-MM/source.csv`，清单逐月记录原件哈希、字节、映射、日期、每日分布和用户来源声明，负值、空白、重复候选不删除或改写。当前不附加代码维表，不自动审核业务指标。

`SnapshotDTO.period` 改为可空：单月仍返回原月份，多月返回 `null`；新增 `periods`、`coverage_start`、`coverage_end_exclusive`，结束日不包含在范围内。旧快照响应按原月份补充这些字段，不改写旧清单。多月 `file_hash` 是按有序月份与原件哈希计算的集合摘要，具体每月字节哈希在 `manifest.sources`，不冒充某个CSV的哈希。元数据迁移 `0008_multimonth` 只放宽单月字段可空；迁移时事务性重建引用表、检查外键完整性，存在多月记录时拒绝降级以避免丢数据。

逐月入库后核对实际行数、日期、空值、取消/备降和负时长，整体核对总量和记录ID唯一性；关闭连接、写完清单后才重命名完整目录并原子登记。失败、取消、超时清理本次UUID目录，不修改默认指针；已经提交的完整快照不会因调用方退出被删。导入预算独立于查询预算，默认600秒且可显式配置；文件处理定期检查取消/截止时间，DuckDB语句运行时由监测线程调用 `interrupt()` 终止实际执行。CLI不提供自动激活选项。

6.C尚未接通前，服务端默认切换、创建调查/运行和工具初始化拒绝多月快照，返回 `MULTIMONTH_INVESTIGATION_NOT_READY`。前端可只读预览来源、质量和结构，但不能设为默认或选择为调查数据。受控SQL服务仍可通过显式快照ID作数据层核对，不能据此宣称模型跨月调查已实现。OpenAPI与前端类型由生成命令同步。

独立验收入口为 [verify_multimonth.py](../../scripts/verify_multimonth.py)，参数为 `--storage-dir`、`--snapshot-id`、`--input-dir`、`--output`。它直接扫描原始CSV并以独立映射和Decimal基线对照实际受控SQL产物，检查冻结原件哈希及来源定位；会向指定验收存储写查询审计/产物，不调用模型、天气或公告。输出文件须尚不存在，真实验收存储与原件不提交Git。

计划的多月快照须逐月记录原始文件、月份、哈希、实际日期覆盖和质量；合并后保留来源文件与原行定位，校验缺月、重复文件、重叠和同名字段冲突。原始重复候选与异常不自动删除，缺失不补零。数据契约显式表示范围，不将旧单月 `period` 拼成范围字符串；运行、调查、工具、报告和 UI 同步更新，并继续校验版本与成功产物。

指标和业务资料需要覆盖所选范围，现有一月草稿不能自动扩期或批准。新快照不覆盖旧快照，切换范围须新建调查或分支。实际用户文件到达后再执行真实导入与独立 CSV 基线核对；多月命令须在实现和验证后才进入 README。

阶段 5.C 已增加独立天气查询快照 `v_weather_observations`，不注册为 BTS 主数据源，不跨库拼接。主快照检查保留，天气版本由服务端额外绑定并校验；复用同一 AST、只读子进程、结果冻结和真实取消。天气表数值采用通过筛选的精确温度、风速和能见度；界限、缺失、冲突及原件定位单列保留，降水/积雪暂不开放累计列。跨月调查也不自动扩大站点/年份、单次日期范围或联网额度，须从实际线索缩小核查范围。详见 [05 外部证据边界](05-evidence-and-reports.md#5c-已实现的外部证据边界)。

## 9. 必测情况

多条语句；注释伪装；WITH 内写入；SELECT INTO；外部 CSV/Parquet 读取；网络 URI；扩展加载；超大范围生成；未允许函数；CTE 别名；含空值的聚合；取消/备降/提前到达；结果截断；超时与取消后无残留查询进程。

---

<a id="decision-0002"></a>

## 实施设计取舍 0002：阶段 1：数据快照与受限查询

本节保留实施时的阶段边界及后续补充；当前完成状态以根目录 IMPLEMENTATION_STATUS.md 中相关事项的最新日期记录为准。

依据蓝图 01—08，重点采用 03 的导入和 SQL 边界、05 的 QueryArtifact、06 的数据源 API。查询服务将权限检查、实际执行与成功产物登记分开处理。

#### 设计取舍

- 查询检查与执行、产物与展示分别负责。sqlglot AST 使用默认拒绝的白名单、作用域解析及函数/表允许集，结合 DuckDB 受限只读连接和 spawn 子进程超时，不能仅凭 SELECT 前缀判断安全。
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
