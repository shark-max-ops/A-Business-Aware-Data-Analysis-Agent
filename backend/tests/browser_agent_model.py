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
        if "报告验收" in question and self.index == 0:
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
        if "模拟服务失败" in question:
            raise DataError("MODEL_REQUEST_FAILED")
        await asyncio.sleep(30 if "等待取消" in question else 0.6)
        turn = await super().next_turn(messages, tools)
        if turn.get("candidate") and "保留长标题" not in question:
            turn["candidate"]["conversation_title"] = "航班数据范围"
        return turn
