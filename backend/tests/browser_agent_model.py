"""仅浏览器隔离服务使用的测试模型，不进入正式配置。"""

import asyncio
import json

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
        super().__init__(
            [
                calls(("search_knowledge", {"query": "ArrDelay"}), ("list_tables", {})),
                calls(("describe_table", {"table_name": "v_flights"})),
                calls(("query_sql", {"sql": "SELECT wrong_column FROM v_flights"})),
                calls(("query_sql", {"sql": "SELECT COUNT(*) AS record_count FROM v_flights"})),
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
        await asyncio.sleep(30 if "等待取消" in question else 0.6)
        return await super().next_turn(messages, tools)
