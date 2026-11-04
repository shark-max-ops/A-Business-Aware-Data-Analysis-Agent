"""仅隔离浏览器服务加载：明确合成的来源响应，不进入正式配置。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import date

from metric_pilot.agent.external_tools import ExternalTools
from metric_pilot.external.http import SourceHTTP
from test_agent_external import fixed_weather
from test_external_advisories import body, entry, listing
from test_external_evidence import weather_capture
from test_external_weather import Pool


def install(storage):
    """
    作用：在独立浏览器进程安装合成来源，实际使用正式工具及证据服务。
    输入：隔离存储目录。
    输出：无；不访问公网。
    """
    import metric_pilot.agent.external_tools as module

    capture = asyncio.run(weather_capture(storage))
    raw = (storage / "external/fetches" / (capture["fetches"][0]["id"] + ".raw")).read_bytes()
    item = entry(facility="ATL/ZTL")
    item["day"] = date(2026, 11, 2)
    notice = (
        body(
            item, "CTL ELEMENT: ATL\nFLT INCL: CARRIER DAL ONLY\nCOMMENTS: SYNTHETIC BROWSER TEST."
        )
        .replace(b"26/01/31 23:40", b"26/11/02 23:40")
        .replace(b"312345-010200", b"022345-030200")
    )

    class BrowserPool(Pool):
        """
        作用：按正式固定来源路径返回不同合成页。
        输入：隔离请求。
        输出：可观察的正式流接口。
        """

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            """作用：路由合成页面；输入：固定来源请求；输出：合成响应流。"""
            self.chunks = [
                raw
                if "/metar?" in url
                else listing([item], item["day"])
                if "/adv_list?" in url
                else notice
            ]
            async with super().stream(method, url, **kwargs) as response:
                yield response

    original = ExternalTools.client

    def client(self):
        """作用：替换隔离连接池；输入：适配器；输出：正式 HTTP 客户端。"""
        if self.http is None:
            self.http = SourceHTTP(storage, pool_factory=BrowserPool)
        return self.http

    ExternalTools.client = client
    module.query_historical_weather = fixed_weather
    return original
