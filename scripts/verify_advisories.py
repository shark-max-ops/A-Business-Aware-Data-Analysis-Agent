"""5.B 真实公开清单、正文、固定分页及实际 HTTPS 取消验收，不调用模型。"""

import argparse
import asyncio
import sys
from pathlib import Path

import httpcore
from metric_pilot.data.common import canonical_bytes, utc_now
from metric_pilot.external.advisories import (
    AdvisorySearch,
    AdvisoryStore,
    read_external_source,
    search_official_advisories,
)
from metric_pilot.external.advisory_cli import readable_body, readable_search
from metric_pilot.external.http import PublicNetworkBackend, SourceHTTP
from metric_pilot.external.registry import FAA_ROOT
from metric_pilot.settings import Settings
from verify_weather import TrackedStream


class CancelStream(TrackedStream):
    """
    作用：仅验收时在真实 HTTPS 响应到达后取消当前请求，观察传输回收。
    输入：真实网络流、跟踪集合与触发状态。
    输出：实际网络流，不伪造响应。
    """

    def __init__(self, stream, streams, state):
        """
        作用：保存真实连接与取消状态。
        输入：流、集合和状态。
        输出：无。
        """
        super().__init__(stream, streams)
        self.state = state

    async def read(self, max_bytes, timeout=None):
        """
        作用：在真实响应字节到达后取消，不等待小文件全部读取完成。
        输入：字节上限与期限。
        输出：字节或实际取消信号。
        """
        data = await super().read(max_bytes, timeout)
        if data and not self.state["triggered"]:
            self.state.update(triggered=True, received_bytes=len(data))
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return data

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        """
        作用：保留正式 TLS 身份验证并跟踪 TLS 流。
        输入：上下文、原主机名与期限。
        输出：实际 TLS 流。
        """
        stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        self.closed = True
        return CancelStream(stream, self.streams, self.state)


class CancelBackend(PublicNetworkBackend):
    """
    作用：使用正式公网解析与连接，仅为真实取消验收增加观测。
    输入：解析模式和跟踪状态。
    输出：真实连接后端。
    """

    def __init__(self, mode, streams, state):
        """
        作用：初始化正式网络保护与跟踪状态。
        输入：模式、集合和状态。
        输出：无。
        """
        super().__init__(mode)
        self.streams, self.state = streams, state

    async def connect_tcp(self, *args, **kwargs):
        """
        作用：包装实际公网连接，不替换 DNS 或响应内容。
        输入：连接参数。
        输出：跟踪流。
        """
        stream = await super().connect_tcp(*args, **kwargs)
        return CancelStream(stream, self.streams, self.state)


async def verify_real_cancel(storage, mode):
    """
    作用：向 FAA 发真实请求，在收到响应字节后取消并核对网络流已关闭。
    输入：目录与解析模式。
    输出：独立验证结果；不是超时或长期稳定性测试。
    """
    streams, pools = [], []
    state = dict(triggered=False, received_bytes=0)

    def factory():
        """
        作用：创建启用正式保护的观察连接池。
        输入：无。
        输出：连接池。
        """
        pool = httpcore.AsyncConnectionPool(
            network_backend=CancelBackend(mode, streams, state),
            retries=0,
            max_keepalive_connections=0,
        )
        pools.append(pool)
        return pool

    client = SourceHTTP(storage, max_requests=1, timeout=30, pool_factory=factory)
    task = asyncio.create_task(
        client.get(
            FAA_ROOT + "/adv_list",
            dict(date="2026-01-25", whichAdvisories="ATCSCC", advisoryCategory="All"),
        )
    )
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True
    record = dict(
        test="real_faa_https_cancel",
        cancelled=cancelled,
        task_done=task.done(),
        received_response_bytes=state["received_bytes"],
        streams_closed=bool(streams) and all(stream.closed for stream in streams),
        pools_empty=all(not pool.connections for pool in pools),
        fetches=client.records,
    )
    record["passed"] = (
        cancelled
        and record["streams_closed"]
        and record["pools_empty"]
        and state["received_bytes"] > 0
    )
    return record


async def main():
    """
    作用：分别保存真实来源正反例、不可变原文分页和实际网络取消结果。
    输入：命令行网络选项。
    输出：忽略目录中的验收 JSON 与终端文本。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="独立公告真实验收，不调用模型")
    parser.add_argument("--public-dns", action="store_true")
    args = parser.parse_args()
    storage = Path(Settings().storage_dir)
    mode = "cloudflare" if args.public_dns else "system"
    http = SourceHTTP(storage, max_requests=10, timeout=120, dns_mode=mode)
    out = storage / "validation" / "stage5b-advisories.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    result = dict(started_at=utc_now(), model_calls=0, checks=[], passed=False)
    out.write_bytes(canonical_bytes(result))

    # 样例编号来自此前人工读取的官方清单，不是生产运行的固定问题路线。
    search = await search_official_advisories(
        AdvisorySearch(airport="DFW", start="2026-01-25"), storage, http=http
    )
    assert all(row["facility"].startswith("DFW/") for row in search["items"])
    result["search"] = search
    result["checks"].append("真实 DFW 清单按设施本地筛选；检索仅候选")
    bodies = []
    for number in (83, 86, 31):
        candidate = next(row for row in search["items"] if row["number"] == number)
        body = await read_external_source(candidate["source_ref"], storage, limit=100, http=http)
        text = "\n".join(row["text"] for row in body["lines"])
        if number == 83:
            assert "CARRIER AAL ONLY" in text and "AAL" in body["carrier_scope"]["codes_mentioned"]
        if number == 86:
            assert "GROUND STOP CANCELLED" in text and candidate["action"] != "cancelled"
        if number == 31:
            assert "GS CNX PERIOD" in text and candidate["action"] == "cancelled"
        store = AdvisoryStore(storage)
        frozen = (store.root / (body["body_ref"] + ".json")).read_bytes()
        calls = http.requests
        second = await read_external_source(body["body_ref"], storage, offset=2, limit=3, http=http)
        assert second["lines"] == body["lines"][2:5] and http.requests == calls
        assert (store.root / (body["body_ref"] + ".json")).read_bytes() == frozen
        bodies.append(body)
        result["bodies"] = bodies
        out.write_bytes(canonical_bytes(result))
    result["checks"].extend(
        [
            "AAL 限制语句保留",
            "不把正文提到的其他管制取消套给当前计划",
            "管制取消单独标记",
            "固定正文按行翻页不联网且文件不变",
        ]
    )
    empty = await search_official_advisories(
        AdvisorySearch(airport="DFW", start="2026-01-25", types=["arrival_delay"]),
        storage,
        http=http,
    )
    assert empty["total"] == 0
    result["empty_search"] = empty
    network = await search_official_advisories(
        AdvisorySearch(airport="DCC", start="2026-01-25"), storage, http=http
    )
    volcano = next(row for row in network["items"] if row["number"] == 90)
    negative = await read_external_source(volcano["source_ref"], storage, limit=100, http=http)
    assert any("ECUADOR" in row["text"] for row in negative["lines"])
    assert not any(row["number"] == 90 for row in search["items"])
    result.update(negative_body=negative, weather_requests=0, completed_at=utc_now())
    result["checks"].extend(["真实日期的指定类型无匹配", "同日厄瓜多尔火山公告未当成 DFW 证据"])
    out.write_bytes(canonical_bytes(result))
    result["cancel"] = await verify_real_cancel(storage, mode)
    result["passed"] = result["cancel"]["passed"]
    result["finished_at"] = utc_now()
    out.write_bytes(canonical_bytes(result))
    print(readable_search(search))
    print(readable_body(bodies[0]))
    print(
        f"真实来源检查：{len(result['checks'])} 项；实际 HTTPS 取消：{result['cancel']['passed']}"
    )
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
