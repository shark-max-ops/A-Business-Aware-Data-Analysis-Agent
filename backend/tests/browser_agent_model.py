"""仅浏览器隔离服务使用的测试模型，不进入正式配置。"""

import asyncio
import json

from metric_pilot.data.common import DataError
from test_agent import FakeLLM, calls, final_from_query


class BrowserModel(FakeLLM):
    """
    作用：验证浏览器与真实执行工具
    输入：测试配置
    输出：隔离模型
    """

    def __init__(self, settings):
        """
        作用：固定浏览器测试序列
        输入：隔离配置
        输出：无
        """
        # 合成快照范围聚合同时覆盖多列表格与类型化日期展示。
        range_sql = (
            "SELECT COUNT(*) AS record_count, COUNT(DISTINCT flight_date) AS distinct_days, "
            "MIN(flight_date) AS min_date, MAX(flight_date) AS max_date FROM v_flights"
        )
        super().__init__(
            [
                calls(("search_knowledge", {"query": "ArrDelay"}), ("list_tables", {})),
                calls(("describe_table", {"table_name": "v_flights"})),
                calls(("query_sql", {"sql": "SELECT wrong_column FROM v_flights"})),
                calls(("query_sql", {"sql": range_sql})),
                final_from_query,
            ]
        )

    async def next_turn(self, messages, tools):
        """
        作用：留出刷新及取消窗口
        输入：上下文
        输出：测试回合
        """
        question = json.loads(messages[1]["content"])["question"]
        if "外部调查验收" in question and self.index == 0:
            from test_agent_external import SCOPE, final, statement
            from test_external_evidence import SQL

            state = {}

            def choose_external(messages):
                """作用：根据实际外部版本和候选选择工具；输入：消息；输出：测试调用。"""
                data = [json.loads(m["content"])["data"] for m in messages if m["role"] == "tool"]
                weather = next(d for d in data if d.get("external_snapshot_id"))
                candidates = next(d for d in data if d.get("candidate_only"))
                return calls(
                    ("read_external_source", dict(source_ref=candidates["items"][0]["source_ref"])),
                    (
                        "query_sql",
                        dict(
                            sql=SQL,
                            purpose="exploratory",
                            exploration_scope=SCOPE,
                            external_snapshot_id=weather["external_snapshot_id"],
                        ),
                    ),
                )

            self.steps = [
                calls(
                    ("query_historical_weather", dict(airport="ATL", start="2026-11-02")),
                    ("search_official_advisories", dict(airport="ATL", start="2026-11-02")),
                ),
                choose_external,
                lambda messages: statement(messages, state),
                lambda _: final(state),
            ]
        if "探索边界验收" in question and self.index == 0:
            from test_exploration import exploration
            from test_investigations import finding_call
            from test_reports import SQL, report_candidate

            self.steps = [
                calls(("resolve_metric", {"metric_key": "missing_reviewed_metric"})),
                calls(("query_sql", {"sql": SQL})),
                calls(exploration()),
                finding_call,
                report_candidate,
            ]
        if "审核阻断验收" in question and self.index == 0:
            query = ("query_sql", {"sql": "SELECT COUNT(*) FROM v_flights"})
            self.steps = [
                calls(("resolve_metric", {"metric_key": "missing_reviewed_metric"})),
                calls(query, query),
            ]
        if "调查验收" in question and "外部" not in question and self.index == 0:
            from test_investigations import finding_call
            from test_reports import SQL, report_candidate

            previous = json.loads(messages[1]["content"])["investigation"]["recent_findings"]
            if previous:

                def continue_with_evidence(messages):
                    """
                    作用：从真实分页工具响应取得引用，避免依赖上下文摘要的展示字段
                    输入：实际工具消息
                    输出：读取产物的测试回合
                    """
                    found = json.loads(messages[-1]["content"])["data"]["findings"]
                    old = next(item for item in found if item["id"] == previous[-1]["id"])
                    ident = old["artifact_ids"][0]
                    statement = {
                        "type": "text",
                        "id": "continued",
                        "text_template": "核查首日记录为 {{n}} 条，缺乏证据判断原因。",
                        "values": old["value_refs"],
                        "evidence_ids": [ident],
                    }
                    self.steps.extend(
                        [
                            calls(
                                (
                                    "record_finding",
                                    {
                                        "kind": "counterevidence",
                                        "check": "重读固定结果并核对解释边界",
                                        "statement": statement,
                                        "supersedes": old["id"],
                                        "questions": ["补充其他解释所需资料"],
                                    },
                                )
                            ),
                            {
                                "kind": "report",
                                "report": {
                                    "schema_version": "1",
                                    "title": "调查继续核查",
                                    "scope": "合成数据",
                                    "metric_revision_ids": [],
                                    "blocks": [statement],
                                    "limitations": ["不能证明因果"],
                                },
                            },
                        ]
                    )
                    return calls(("read_artifact", {"artifact_id": ident}))

                self.steps = [calls(("read_investigation", {})), continue_with_evidence]
            else:
                self.steps = [calls(("query_sql", {"sql": SQL})), finding_call, report_candidate]
        if "报告验收" in question and self.index == 0:
            from test_investigations import finding_call
            from test_reports import SQL, report_candidate

            def report_with_bar(messages):
                """
                作用：以实际查询结果验收两种中文图表
                输入：隔离工具消息
                输出：仅测试使用的报告
                """
                result = report_candidate(messages)
                chart = dict(result["report"]["blocks"][-1])
                chart.update(id="bar", kind="bar", title="每日航班记录对比")
                result["report"]["blocks"].append(chart)
                return result

            self.steps = [
                calls(("search_knowledge", {"query": "ArrDelay"}), ("list_tables", {})),
                calls(("describe_table", {"table_name": "v_flights"})),
                calls(("query_sql", {"sql": SQL})),
                finding_call,
                report_with_bar,
            ]
        if "需要补充条件" in question:
            return {
                "calls": [],
                "candidate": {
                    "kind": "clarification",
                    "question": "需要先审核项目指标口径",
                    "missing_requirements": ["已审核的指标定义"],
                },
                "usage": {},
                "attempts": 1,
            }
        if "跨月趋势验收" in question and self.index == 0:
            monthly_state = {}
            # 仅供隔离浏览器测试；实际数字和差值都由受控SQL计算。
            sql = """WITH monthly AS (
                SELECT CAST(DATE_TRUNC('month',flight_date) AS DATE) AS month,
                       COUNT(*) AS n
                FROM v_flights GROUP BY 1
            ) SELECT a.month,a.n,a.n-b.n AS delta,
                     CAST(1.0*(a.n-b.n)/NULLIF(b.n,0) AS DOUBLE) AS relative_delta
              FROM monthly a LEFT JOIN monthly b
                ON a.month=b.month+INTERVAL '1 month'
              ORDER BY a.month"""
            grouped_sql = """SELECT CAST(DATE_TRUNC('month',flight_date) AS DATE) AS month,
                carrier_code,COUNT(*) AS n FROM v_flights GROUP BY 1,2 ORDER BY 1,2"""

            def monthly_report(messages):
                """
                作用：将实际月度结果绑定为冻结表格和趋势图
                输入：测试工具消息
                输出：隔离报告候选，不预填数值
                """
                artifact = monthly_state["artifact_id"]
                grouped_artifact = final_from_query(messages)["artifact_ids"][0]
                return {
                    "kind": "report",
                    "report": {
                        "schema_version": "1",
                        "title": "合成跨月趋势与分组比较",
                        "scope": "固定部分月范围内按月份与航司计数",
                        "metric_revision_ids": [],
                        "limitations": [
                            "明确合成数据；两端为部分月，总量不能解释为完整月环比或季节规律。"
                        ],
                        "blocks": [
                            {
                                "type": "table",
                                "id": "monthly",
                                "title": "月度变化与部分月比较",
                                "artifact_id": artifact,
                                "columns": [
                                    "month",
                                    "n",
                                    "delta",
                                    "relative_delta",
                                ],
                                "column_labels": {
                                    "month": "月份",
                                    "n": "记录数",
                                    "delta": "绝对变化",
                                    "relative_delta": "相对变化（比例）",
                                },
                            },
                            {
                                "type": "table",
                                "id": "grouped",
                                "title": "按航司分组的月度记录",
                                "artifact_id": grouped_artifact,
                                "columns": ["month", "carrier_code", "n"],
                                "column_labels": {
                                    "month": "月份",
                                    "carrier_code": "航司",
                                    "n": "记录数",
                                },
                            },
                            {
                                "type": "chart",
                                "id": "trend",
                                "title": "所选范围月度趋势",
                                "artifact_id": artifact,
                                "kind": "line",
                                "x": "month",
                                "y": ["n"],
                                "x_label": "月份",
                                "series_labels": {"n": "记录数"},
                                "format": "integer",
                            },
                        ],
                    },
                }

            def monthly_finding(messages):
                """
                作用：保存本轮实际查询及部分月限制，遵循报告前置检查
                输入：实际工具消息
                输出：发现工具调用
                """
                artifact = final_from_query(messages)["artifact_ids"][0]
                monthly_state["artifact_id"] = artifact
                return calls(
                    (
                        "record_finding",
                        {
                            "kind": "fact",
                            "check": "核对所选范围的航司月度分布",
                            "statement": {
                                "type": "text",
                                "id": "monthly_observed",
                                "text_template": (
                                    "首个部分月有 {{n}} 条记录；不能据此解释完整月或季节规律。"
                                ),
                                "values": {"n": {"artifact_id": artifact, "row": 0, "column": "n"}},
                                "evidence_ids": [artifact],
                            },
                            "questions": [],
                        },
                    )
                )

            self.steps = [
                calls(("query_sql", {"sql": sql})),
                monthly_finding,
                calls(("query_sql", {"sql": grouped_sql})),
                monthly_report,
            ]
        if "模拟服务失败" in question:
            raise DataError("MODEL_REQUEST_FAILED")
        await asyncio.sleep(30 if "等待取消" in question else 0.6)
        turn = await super().next_turn(messages, tools)
        if turn.get("candidate") and "保留长标题" not in question:
            turn["candidate"]["conversation_title"] = "航班数据范围"
        return turn
