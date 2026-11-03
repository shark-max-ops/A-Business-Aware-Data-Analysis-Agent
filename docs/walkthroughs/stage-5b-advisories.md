# 阶段 5.B：查官方公告，再读固定原文

这一小步新增两个独立工具：`search_official_advisories()` 查 FAA ATCSCC 公开运行通知的候选清单，`read_external_source()` 读取清单登记的正文。输入设施/机场和 UTC 签发日期，先看有哪些候选，再核对原文的机场、时间与航司限制。它们还没有向 Agent 开放，也不能直接成为报告证据；这两部分分别留给 5.D 和 5.C。

## 怎么试用

在项目根目录 PowerShell 执行，不用启动前后端或模型：

```powershell
.venv\Scripts\python.exe -m metric_pilot.external.advisory_cli search --airport DFW --start 2026-01-25 --public-dns
```

终端默认显示中文候选列表：公告编号、UTC 签发时间、控制设施、项目类型标签、原标题及可复制的“正文编号”。找一条想看的公告，把它的正文编号粘到下面引号内：

```powershell
$sourceRef = "粘贴上一步返回的正文编号"
.venv\Scripts\python.exe -m metric_pilot.external.advisory_cli read $sourceRef --public-dns
```

正文以原始英文显示，带行号，中文说明类型、有效期和范围；不会调用模型翻译或改写。默认每页 40 行，最多 100 行。需要翻页时，复制输出的“固定正文编号”，例如：

```powershell
$bodyRef = "粘贴读取后返回的固定正文编号"
.venv\Scripts\python.exe -m metric_pilot.external.advisory_cli read $bodyRef --offset 40 --limit 40
```

这一页读取本机保存的同一份正文，不重新联网。再次读取候选编号会重新获取并追加一个正文版本；旧正文编号及其文件不会覆盖。候选清单超过一页时，可用 `list "固定清单编号" --offset 20 --limit 20` 回读同一清单。各子命令加 `--json` 可看技术字段。

检索可加 `--end 2026-01-27`（最多三天），或 `--types ground_stop ground_delay`；类型还包括 `arrival_delay`、`operations_plan`、`other`。设施代码精确匹配 FAA 清单的首段，例如 DFW/ZFW 的 DFW；也可明确查询 DCC 全国性通知。它不证明任意三字母都是已核对机场，不扩展 5.A 的天气站点映射。

这里的日期是 **UTC 签发日**，与天气工具的机场当地日期不同。并非“所有与当地当天有效期重叠的公告”查全接口。无匹配也不能推出没有事件；全国性、航路性或前几天签发的通知可能不在筛选结果中。

`--public-dns` 与 5.A 一样用于本机代理虚拟 DNS：操作者显式选择固定 `1.1.1.1` 加密 DNS，仅发送已允许的 FAA 域名，不发送机场、业务内容或密钥给 DNS。默认系统 DNS，拒绝内网/保留地址，不自动绕过来源封锁。

## 1. 查清单：在本地筛选，不相信失效的网站过滤

[advisories.py](../../backend/src/metric_pilot/external/advisories.py) 的 `AdvisorySearch` 限定三字母设施、UTC 日期和允许类型；单次最多三天、不查未来，不接受任意网址。`search_official_advisories()` 只请求 [FAA 历史查询入口](https://www.fly.faa.gov/adv/advAdvisoryForm) 对应的 `/adv/adv_list`，固定条件为 ATCSCC、All，随后在后端核对设施和类型。

此前实际测试表明网站类别过滤可能返回全部清单；现在不依赖它。`advisory_parser.py` 的 `parse_list()` 校验清单日期、NUMBER/CONTROL ELEMENT/DATE/BRIEF TITLE/SEND TIME 列、编号及链接日期。结构改变、身份不符、越界链接或有数据却找不到预期正文链接均报错，不能当成合法空结果。HTTP 200 也不能代替内容检查。

搜索结果分配新的固定清单 ID；每个候选的 `source_ref` 由服务端从清单 ID 和条目索引生成。没有匹配的候选不凭空登记，搜索标题也不是已经读取的正文。

## 2. 读正文：编号来自真实清单

`read_external_source()` 接受两种服务端标识：候选编号用于首次获取；固定正文编号用于本机回读。`AdvisoryStore.candidate()` 先核对清单、请求条件、原件哈希及原列表条目，才允许生成 `/adv/adv_otherdis` 的日期/编号参数。模型或用户不能直接提供任意 URL，也不能只猜官方公告编号跳过登记。

[advisory_parser.py](../../backend/src/metric_pilot/external/advisory_parser.py) 使用标准库 HTMLParser，不执行 JavaScript，也不跟随正文链接。它提取标题、MESSAGE 的 PRE 原文、EFFECTIVE TIME 和 SIGNATURE，删除页面导航，保留原文换行，并核对清单与正文的编号、日期、标题、设施和签发时间。正文格式或身份错误时直接失败，不拿候选摘要补成成功正文。

返回的正文行号对应提取后的 MESSAGE；同时记录原 HTML 中 PRE 起始行、抓取 ID 和原件，能从固定原文定位回原始响应。相邻月份的有效期可以保守解析，覆盖跨月、跨年、闰日；未知格式保留原值，不猜时间。FAA 运行时间按 UTC 解读，依据 [AIM 4-2-12](https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap4_section_2.html)；有效期、签发时间和原文中具体程序影响时段分别保留，不能互相替代。

## 3. 说明适用范围，不急着解释航班原因

类型和动作是项目的保守标签，不冒充官方解释。程序可标记地面停止、地面延误计划、到达延误通知、运行安排及其他通知；动作区分拟议、更新、延长、管制取消和未明确状态。原标题始终保留，普通签发通知不自动等于正在生效。

真实样本说明了这些区别：DFW 2026-01-25 的 #083 写有 `CARRIER AAL ONLY`，不能推广为全部航司；#086 是地面延误计划，正文提到另一项 ground stop 被取消，不能把本计划误标为取消；#031 的 GS CNX 是管制取消，不是航班取消。同日 #090 是厄瓜多尔火山通告，没有因为“同一天官方发布”就当作 DFW 异常依据。

工具保留 FLT INCL、COMMENTS 等完整原文，返回限制性语句摘录和出现的航司代码；摘录有明确限量标记，完整内容仍可按行读取。这不是对所有航司适用范围的自动语义证明。预计延误、上报条件和 BTS 实际统计分开，来源描述不单独证明因果。

## 4. 来源条件、网络安全与固定文件

[registry.py](../../backend/src/metric_pilot/external/registry.py) 单独登记 FAA 来源和使用边界。[FAA AIM 对 OIS 的说明](https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap5_section_1.html)及公开查询表支持公众查阅运行通知；[当前免责声明](https://www.faa.gov/web_policies/disclaimer)要求授权、正当使用。本项目的接入判断限定为单用户本机低频只读、保留来源的资料查阅与引用，不作公开镜像、批量爬取、图片/加拿大公告/第三方材料再分发的授权推定。

没有找到该 HTML 入口明确的数字配额、稳定 API 承诺或通用自动抓取许可证；这些未知仍记录，不能写成已全部解决。旧隐私页、robots 跳转和 HTTP 200 均不当作抓取许可证。遇到 403/429、登录或脚本挑战会停止，不绕过，不自动重试。公开部署或批量再分发不在本次验收范围。

[http.py](../../backend/src/metric_pilot/external/http.py) 复用 5.A 的受控连接，只新增 `www.fly.faa.gov` 的两个精确 HTTPS 路径：清单和正文。限制每次工具 120 秒、响应 2 MB、请求间隔至少两秒；搜索最多三次清单，正文最多一次请求。校验全部 DNS 答案后直连选定公网 IP，保留 TLS 主机名校验；拒绝环境代理、内网、全部跳转和压缩响应。FAA 网页被读取不意味着 JavaScript、图片或外链会被执行/下载。

抓取字节及请求/哈希沿用 `storage/external/fetches/`；固定清单和正文保存在 `storage/external/advisories/`，服务端 UUID 追加、不覆盖。回读校验内容及原件哈希、成功状态和清单归属，拒绝篡改、截断、伪造 ID 和危险路径。已冻结的项目标签不随分类器更新重算。5.C 将在这些原件之上接入正式外部证据、运行/调查授权及报告验证；此处不声称已有报告引用权限。

## 验证与当前限制

[test_external_advisories.py](../../backend/tests/test_external_advisories.py) 使用明确合成页面和隔离传输，实际执行解析、抓取清单保存、原件哈希、来源登记及分页；验证错误页、链接/身份/范围错误、结构变化、无匹配、拟议/更新/取消、航司限制、恶意原文、篡改、取消与期限、正文失败不退回摘要、重新获取后旧正文不变。没有使用模型或真实页面作为 Git fixture。

[verify_advisories.py](../../scripts/verify_advisories.py) 单独走真实服务路径，取得 DFW 清单、#083/#086/#031 正文、指定类型无匹配以及 DCC #090 的不相关反例，核对固定分页不联网。还在真实 FAA HTTPS 响应字节到达后取消，核对实际流关闭和连接池清空；没有伪造网络响应。记录在忽略的 `storage/validation/stage5b-advisories.json`，原件和固定正文均可回读。

当前仅完成 Windows 独立接口及终端验收，不代表长期可用性、全部机场/公告类型、全量历史、跨平台、模型调查质量或来源语义准确率已验证。没有前端、Agent、报告改动，也没有新模型调用、付费数据服务、Wiki 发布或指标审批。下一入口是 5.C 的外部证据冻结与 ValueRef 接入。
