# 阶段 5.C：让外部资料成为可核对的报告依据

这一小步把“查到了天气和公告”接到“报告能引用真实原文和实际数字”。仍不需要模型，也不用启动前后端。Agent 自动选择外部工具与页面展示留到 5.D。

## 一次操作怎么走

```text
5.A 保存的天气原件 → 复算范围、数值及质量 → 固定天气查询快照
                                              ↓
                                 安全 SQL → 成功查询产物 → ValueRef
                                                                ↓
5.B 实际读到的公告正文 → 核对清单、身份与原件 → 固定原文引用 → 报告
```

报告的主数据版本仍是本次运行的航班快照。天气额外固定自己的版本，两个版本分别检查。天气表与航班表不在这一步自动拼接；同机场、同一天也不能直接证明航班异常原因。

## 1. 把采集原件固定为正式证据

[evidence.py](../../backend/src/metric_pilot/external/evidence.py) 的 `ExternalEvidenceService` 负责登记、授权、原件回读及报告来源。输入已有天气 ID 或固定公告正文 ID，不接受任意路径、URL 或调用者给出的观测数字。ID、版本、主数据与知识边界由服务端填写。

[weather_proof.py](../../backend/src/metric_pilot/external/weather_proof.py) 从成功且未截断的原件重新解析，核对请求窗口、机场/站点、观测数值、质量与覆盖。修改 JSON 中的温度不能变成证据；缺原件、失败采集或哈希不一致会拒绝。公告先回查真实清单，再解析原正文，候选标题不能代替正文。

[models.py](../../backend/src/metric_pilot/external/models.py) 和 [0007 迁移](../../backend/migrations/versions/0007_external_evidence.py) 保存证据、天气快照及运行绑定。一个运行的同一来源条件固定一个版本，不能原地替换。新运行可以取得新版本，旧证据仍保留。

## 2. 天气数字也通过安全 SQL

[weather_snapshot.py](../../backend/src/metric_pilot/external/weather_snapshot.py) 建立 `v_weather_observations`：每个实际观测时刻一行，有 UTC 时间、机场当地日期、站点、温度、风速、能见度、质量状态和原件定位。

缺测不补零，同刻冲突置空。能见度“高于某值”是界限，不作为精确能见度参与平均；下界另存。降水与积雪原件保留，暂不开放累计数值列。`COUNT(*)` 是观测时刻数，`COUNT(temperature_c)` 才是可用精确温度的时刻数，二者不可混称。

[data/query.py](../../backend/src/metric_pilot/data/query.py) 继续使用原有 AST 校验、允许表和函数、只读子进程、超时与取消；执行成功才产生 `QueryArtifact`。新增外部版本字段，不改变主快照字段。天气产物固定为未审核探索，不能绑定已审核项目指标为它背书。

## 3. 报告取值和来源怎样绑定

[reports/service.py](../../backend/src/metric_pilot/reports/service.py) 在准备报告时校验成功产物与外部版本，将固定原文、来源定位和天气快照一起保存。[materialize.py](../../backend/src/metric_pilot/reports/materialize.py) 仍只从冻结查询单元格读取 `ValueRef` 的数字，并附加外部来源不能单独证明因果的限制。

阶段 4 的跨运行规则保留：同调查、同会话、同主数据/知识版本、已完成旧运行才可复用。新 SQL 查询旧天气快照前还要显式绑定；本轮选了新版本，不能混入旧版产物。失败旧运行的资料不能跨运行成为依据。旧报告读取自己的冻结内容，重新获取来源不会改写它。

## 4. 具体怎么试

首次需要已经采集好的天气和公告正文 ID。项目根目录 PowerShell 可运行：

```powershell
.venv\Scripts\python.exe scripts/verify_external_evidence.py --weather-id "你的天气编号" --body-ref "你的固定正文编号" --snapshot-id "你的航班主快照编号"
```

脚本只在 `storage/validation/` 新建隔离工作区，从本机已保存原件核验，不调用模型、不重新联网、不执行 BTS SQL，也不修改主数据库、指标或旧报告。要求示例天气与公告机场及日期标签一致，但仍明确区分当地天气日和 UTC 签发日。终端显示温度时刻数、精确温度范围、最大精确风速及公告编号，而非整段 JSON。

脚本真实执行天气 SQL、生成报告并逐值核对，同时检查固定公告的正文和原文行位置。结果保存到忽略的 `storage/validation/stage5c-external-evidence.json`；对应隔离目录内有 UTF-8 `试用说明.txt`。实际样本与运行记录见 [实施状态](../../IMPLEMENTATION_STATUS.md)，不要把文档中的样本数当成预填答案。

## 5. 验证和当前限制

[test_external_evidence.py](../../backend/tests/test_external_evidence.py) 使用明确合成的网络原件，实际执行 SQLite、DuckDB 和报告服务。覆盖原件/数值篡改、伪造 ID、候选不能当正文、错版本、跨调查、失败来源、截断结果、取消、质量/界限值及保留旧查询的迁移；这些隔离测试与真实来源验收分别记录。

当前仍沿用 5.A 的三站与年份映射、5.B 的 UTC 签发日与设施匹配及来源使用限制。服务层支持外部证据，不代表 Agent 已会自动查天气、选公告、保存外部调查发现或生成整套原因调查；这些是下一步 5.D 的工作。没有运行新的模型或浏览器验收，也不代表跨平台、全来源或整体准确率已验证。
