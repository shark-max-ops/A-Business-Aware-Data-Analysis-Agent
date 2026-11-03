# 阶段 5.A：先把天气查准，再交给 Agent

这一小步让项目能独立查天气。输入机场和当地日期，程序选定已核对的气象站，读取官方观测，说明哪些字段有数据、哪些缺失，并保存原件和出处。当前入口是命令行，不需要启动前后端，也不调用模型。

这还没有接入分析对话或报告。公告检索留给 5.B，外部证据和报告取值留给 5.C，Agent 自动选工具和页面展示留给 5.D。

## 怎么试用

在项目根目录的 PowerShell 中执行：

```powershell
uv sync --locked
uv run python -m metric_pilot.external.cli --list-airports
uv run python -m metric_pilot.external.cli --airport ATL --start yesterday
uv run python -m metric_pilot.external.cli --airport ATL --start 2026-01-25 --fields temperature wind_speed visibility precipitation snow_depth
```

目前明确支持 **ATL、ORD、JFK**，也可输入对应的 KATL、KORD、KJFK。站点映射只核对了 2026 年；单次最多三个已结束的当地日期，可以用 `--start 2026-01-24 --end 2026-01-26`。不接受任意站点或网址，不猜测尚未登记机场。

本机验收时系统 DNS 将 NOAA 解析为 `198.18.*` 代理虚拟地址，客户端会拒绝。遇到 `EXTERNAL_ADDRESS_REJECTED` 时，可由操作者显式选择公共加密 DNS：

```powershell
uv run python -m metric_pilot.external.cli --airport ATL --start yesterday --public-dns
uv run python -m metric_pilot.external.cli --airport ATL --start 2026-01-25 --public-dns
```

该选项仅向固定公网 `https://1.1.1.1/dns-query` 发送 NOAA 域名，使用 [Cloudflare 公布的 JSON 查询格式](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。不发送机场、业务问题、航班数据或密钥给 DNS 服务。默认仍用系统 DNS，不自动切换，也不放行虚拟或内网地址。若 uv 的本机全局缓存无访问权限，可以给上述 uv 命令加 `--cache-dir .cache/uv`，或直接用已同步的 `.venv\Scripts\python.exe`。

终端默认显示中文摘要和观测表：机场当地日期与时区、记录数、温度/风速等观测极值及覆盖、每条观测的当地时间与数值。明细时间带 UTC 偏移，夏令时回拨的重复钟点可区分。默认显示前 20 条，加 `--rows 100` 可增加显示条数（最多 20000）；所有结果始终保存，不因终端预览截断。显示值最多保留两位小数，保存精度不变；`>` 表示界限值，`*` 表示未通过筛选或同刻冲突，不纳入摘要，缺测不显示为零。无结果、失败和取消用中文说明。

需要技术记录时加 `--json`，输出完整 JSON，包含逐条观测；`--list-airports` 默认也显示中文表格。完整观测保存在 `storage/external/weather/<weather_id>.json`；对应 `fetches` 条目指向 `storage/external/fetches/<fetch_id>.raw` 和同名清单。近期观测按 `json[index]` 定位，历史观测按原文件 `line:n` 定位。文件为 UTF-8；Windows 管道读取时设置 `$OutputEncoding` 和 `[Console]::OutputEncoding` 为 UTF-8，避免中文被替换。按 Ctrl+C 会取消请求，已读取部分保留为失败原件，不生成成功天气结果。

## 1. 先确认查的是哪座机场、哪一天

[registry.py](../../backend/src/metric_pilot/external/registry.py) 保存服务端来源登记与有限站点映射。IATA、ICAO 和机场坐标来自实际 AWC 机场接口，其中机场信息标注 `source=FAA`；气象站坐标来自站点接口，GHCNh 编号通过 NCEI 站点清单和历史观测的 `Source_Station_ID` 交叉核对。机场参考点与气象站有距离，不当成同一个坐标。

| 机场 | 观测标识 | GHCNh 站点 | 时区 | 站点距机场参考点 |
| --- | --- | --- | --- | --- |
| ATL | KATL | USW00013874 | America/New_York | 约 1.537 km |
| ORD | KORD | USW00094846 | America/Chicago | 约 2.690 km |
| JFK | KJFK | USW00094789 | America/New_York | 约 1.248 km |

映射依据是 2026-11-03 实际取得的 [AWC 机场资料](https://aviationweather.gov/api/data/airport?ids=KATL,KORD,KJFK&format=json)、[AWC 气象站资料](https://aviationweather.gov/api/data/stationinfo?ids=KATL,KORD,KJFK&format=json) 和 [GHCNh 清单](https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/doc/ghcnh-station-list.txt)。原件与哈希在忽略的 `storage/validation/stage5a-discovery/`；三站的 2026 年 1 月历史样本已有读取记录。本项目没有完整迁站史，所以只开放已核对年份，并逐条核对返回坐标和历史来源标识；偏离登记站点超过 1 km 即拒绝，不自动换站。

[weather.py](../../backend/src/metric_pilot/external/weather.py) 的 `local_window()` 按机场当地日期解释 yesterday，再转换为左闭右开的 UTC 窗口。`tzdata` 已加入项目依赖和锁文件，Windows 可以读取 IANA 时区。夏令时切换日可能是 23 或 25 小时，不能固定按 UTC 零点截取 24 小时。

## 2. 根据日期取得真实观测

`query_historical_weather()` 近期使用 AWC METAR，较早日期使用 NCEI GHCNh。来源在结果中明确标识；一次查询只用一种来源，不静默拼接，不用预报补历史缺测。

| 路径 | 读取方式 | 范围与使用条件 |
| --- | --- | --- |
| AWC `/api/data/metar` | 单机场、每次最多 12 小时，按实际观测时间过滤 | [官方 API](https://aviationweather.gov/data/api/) 说明最近 30 天、100 次/分钟、多数接口上限 400 条；项目留一天边界余量，达到返回上限即拒绝完整性声明 |
| NCEI GHCNh 年度 PSV | 完整读取已登记站点的年度文件，再选指定窗口 | [元数据](https://www.ncei.noaa.gov/metadata/geoportal/rest/metadata/item/gov.noaa.ncdc:C01688/html) 说明每日更新及引用信息，通常电子下载免费；不承诺昨日已补齐，也不使用可能收费的认证/定制服务 |

AWC 公开 API 明确用于机器访问；[NWS 使用声明](https://www.weather.gov/disclaimer) 允许依法免费使用公共信息，但不能冒充自己或修改后冒充官方产品，也不能暗示 NOAA 背书。GHCNh 按元数据保留 Menne et al. (2023)、DOI、站点/日期/访问时间，并遵守 [NCEI 公共信息声明](https://data.ngdc.noaa.gov/ngdcinfo/privacy.html) 的署名及第三方例外。不是对全部第三方材料作统一许可承诺。

本地预算比 AWC 公布额度保守：每次查询最多 8 次天气请求、间隔至少 2 秒、整个网络预算 120 秒、不自动重试。NCEI 具体入口未查到数字配额，不解释为无限制；每份最多 64 MiB，跨 UTC 年最多两份。年度文件可能较大，因此历史查询比近期查询慢。并行启动多个 CLI 不构成跨进程全局限流；当前按单用户串行使用，接入后台时须保持统一调度。

## 3. 把缺失、质量和观测频率说明白

`parse_awc()` 将风速 knots 转为 m/s，能见度 statute miles 转为 km，降水及积雪 inches 转为 mm；温度保留 °C。`10+` 这类能见度是下界，不能当成精确值参与极值。接口没有逐字段质量码，输出明确写为未知，不冒充 GHCNh 的 quality=1。

`parse_ghcnh()` 保留原值、质量码、测量码、报告类型和来源编号。当前统计仅采用 source223、quality1、FM15/FM16 的 METAR/SPECI，其他来源或质量仍保留在观测里，但不纳入统计。PSV 已是小数单位，不再除以十。

`summarize()` 去除完全相同的重复观测，同一时刻的冲突值排除并计数。覆盖指“这一 UTC 小时至少有一条符合筛选的观测”，不证明连续完整。特殊报告更频繁，不能按记录数计算一个未经说明的全天平均值。

降水原值和累计信息保留，**不相加、不计算全天总降水**；积雪缺失不当成零。历史降水覆盖进一步仅接受空测量码、2（微量）、4（累计结束），其他标记保留但不计可用覆盖；这仍不是完整小时累计量认证。工具只给逐字段覆盖及温度、风速、精确能见度的观测极值，不由天气与航班异常同时出现推出因果。[GHCNh 格式说明](https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/doc/ghcnh_DOCUMENTATION.pdf) 是历史字段、质量及降水规则依据。

## 4. 请求受控，原件可回查

[http.py](../../backend/src/metric_pilot/external/http.py) 只开放两个登记域名的指定 HTTPS 路径。连接前核对全部 DNS 结果，拒绝本机、内网及保留地址，再直接连接选定公网 IP，保留原始主机名的 TLS 证书校验，避免二次解析绕过。所有跳转拒绝，环境代理不生效；不接受压缩响应，限制保存字节，不执行原文里的任何脚本或指令。

每次读取分配新的抓取 ID，保存请求、获取/结束时间、状态、哈希和原始字节。来源错误、超时、取消、超限与截断不写成成功结果，也不成为报告证据。新查询另建文件，旧结果不重算。正式外部证据授权、报告校验和 ValueRef 在 5.C 完成。

## 验证与当前限制

[test_external_weather.py](../../backend/tests/test_external_weather.py) 使用合成观测和隔离传输检查日期、站点、地址、质量、重复/冲突、缺测、条数/字节上限、状态码、取消和期限。

[verify_weather.py](../../scripts/verify_weather.py) 独立检查真实近期/历史结果的原件哈希和定位，并观察真实 NOAA HTTPS 流在取消、超时后是否关闭及连接池是否清空。它不替换实际网络响应、不调用模型；验收记录在忽略的 `storage/validation/stage5a-weather.json`。当前只验证 Windows 和所列样本，不承诺所有机场、所有日期或全部字段完整。
