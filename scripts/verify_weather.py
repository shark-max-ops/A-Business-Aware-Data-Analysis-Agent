"""5.A 真实 HTTPS 验收：公开样本、哈希、实际取消及总期限，不调用模型。"""

import argparse
import asyncio
import hashlib
import json
import re
import time

import httpcore
from metric_pilot.data.common import DataError, canonical_bytes, utc_now
from metric_pilot.external.http import PublicNetworkBackend, SourceHTTP
from metric_pilot.external.registry import NCEI_ROOT
from metric_pilot.external.weather import WeatherRequest, query_historical_weather
from metric_pilot.settings import Settings


class TrackedStream(httpcore.AsyncNetworkStream):
    """
    作用：仅验收时观察真实传输关闭，不替换 DNS、TLS 或网络数据。
    输入：真实网络流及跟踪集合。
    输出：透传网络流。
    """

    def __init__(self, stream, streams):
        """
        作用：保存被观察的真实连接。
        输入：流和跟踪集合。
        输出：无。
        """
        self.stream, self.streams, self.closed = stream, streams, False
        streams.append(self)

    async def read(self, max_bytes, timeout=None):
        """
        作用：原样读取真实网络。
        输入：字节上限与期限。
        输出：字节。
        """
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        """
        作用：原样发送请求。
        输入：字节和期限。
        输出：无。
        """
        await self.stream.write(buffer, timeout)

    async def aclose(self):
        """
        作用：记录并执行实际连接关闭。
        输入：无。
        输出：无。
        """
        await self.stream.aclose()
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        """
        作用：保持正式客户端的主机名与证书校验。
        输入：TLS 上下文、原主机名及期限。
        输出：真实 TLS 流。
        """
        stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        # TLS 接管同一 socket，仅检查新流最终关闭。
        self.closed = True
        return TrackedStream(stream, self.streams)

    def get_extra_info(self, info):
        """
        作用：透传连接信息。
        输入：信息键。
        输出：底层信息。
        """
        return self.stream.get_extra_info(info)


class TrackedBackend(PublicNetworkBackend):
    """
    作用：使用正式公网连接逻辑，仅增加关闭观测。
    输入：解析模式与流集合。
    输出：真实连接后端。
    """

    def __init__(self, mode, streams):
        """
        作用：初始化正式后端及跟踪集合。
        输入：模式和集合。
        输出：无。
        """
        super().__init__(mode)
        self.streams = streams

    async def connect_tcp(self, *args, **kwargs):
        """
        作用：包装实际公网连接。
        输入：连接参数。
        输出：跟踪流。
        """
        return TrackedStream(await super().connect_tcp(*args, **kwargs), self.streams)


async def verify_stop(storage, mode, cancel):
    """
    作用：对真实 NOAA HTTPS 文件读取执行取消或期限终止并核对连接回收。
    输入：目录、解析方式与终止方式。
    输出：真实诊断；未读到响应体不计通过。
    """
    streams, pools = [], []

    def factory():
        """
        作用：创建只增加观测的真实连接池。
        输入：无。
        输出：连接池。
        """
        pool = httpcore.AsyncConnectionPool(
            network_backend=TrackedBackend(mode, streams),
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
        )
        pools.append(pool)
        return pool

    client = SourceHTTP(storage, timeout=45 if cancel else 8, pool_factory=factory)
    url = NCEI_ROOT + "/hourly/access/by-year/2026/psv/GHCNh_USW00013874_2026.psv"
    task = asyncio.create_task(client.get(url, max_bytes=64 * 1024 * 1024))
    started = time.monotonic()
    if cancel:
        while not client.bytes_received and not task.done():
            await asyncio.sleep(0.01)
        task.cancel()
    try:
        await task
        outcome = "unexpected_completion"
    except asyncio.CancelledError:
        outcome = "EXTERNAL_CANCELLED"
    except DataError as error:
        outcome = error.code
    expected = "EXTERNAL_CANCELLED" if cancel else "EXTERNAL_TIMEOUT"
    record = dict(
        test="real_https_cancel" if cancel else "real_https_deadline",
        outcome=outcome,
        response_body_bytes=client.bytes_received,
        task_done=task.done(),
        streams_closed=bool(streams) and all(s.closed for s in streams),
        pools_empty=all(not p.connections for p in pools),
        seconds=time.monotonic() - started,
        fetches=client.records,
    )
    record["passed"] = (
        outcome == expected
        and record["streams_closed"]
        and record["pools_empty"]
        and client.bytes_received > 0
    )
    return record


def check_saved(storage, result):
    """
    作用：逐个回读原件哈希，并核对观测位置可找到原始响应。
    输入：目录与查询结果。
    输出：哈希及定位检查摘要。
    """
    sources, locations = {}, {}
    for fetch in result["fetches"]:
        raw = (storage / "external" / "fetches" / f"{fetch['id']}.raw").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == fetch["sha256"]
        assert fetch["status"] == "succeeded" and not fetch["truncated"]
        sources[fetch["id"]] = raw
        locations[fetch["id"]] = (
            json.loads(raw) if result["source"] == "awc_metar" else raw.decode().splitlines()
        )
    for observation in result["observations"]:
        raw = sources[observation["fetch_id"]]
        locator = observation["locator"]
        if locator.startswith("json["):
            item = locations[observation["fetch_id"]][int(locator[5:-1])]
            assert item["rawOb"] == observation["raw_report"]
        else:
            line = locations[observation["fetch_id"]][int(locator.split(":")[1]) - 1]
            assert observation["observed_at"][:19] in line
    return dict(hashes_checked=len(sources), locators_checked=len(result["observations"]))


async def main():
    """
    作用：分开记录已保存样本核对与真实网络关闭验收。
    输入：命令行选项。
    输出：忽略目录中的中文验收 JSON。
    """
    parser = argparse.ArgumentParser(description="真实独立天气验收，不调用模型")
    parser.add_argument("--public-dns", action="store_true")
    parser.add_argument("--recent-id", help="复核已有近期结果，缺省发起新查询")
    parser.add_argument("--historical-id", help="复核已有历史结果，缺省发起新查询")
    args = parser.parse_args()
    storage = Settings().storage_dir
    mode = "cloudflare" if args.public_dns else "system"
    report = dict(started_at=utc_now(), samples=[], network=[], model_calls=0)
    out = storage / "validation" / "stage5a-weather.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 已保存样本仍逐项回读；不把它们记为本次重新获取。
    for identifier, start in [(args.recent_id, "yesterday"), (args.historical_id, "2026-01-25")]:
        if identifier:
            if not re.fullmatch(r"weather_[a-f0-9]{32}", identifier):
                raise ValueError("invalid weather id")
            result = json.loads(
                (storage / "external" / "weather" / f"{identifier}.json").read_text(
                    encoding="utf-8"
                )
            )
        else:
            result = await query_historical_weather(
                WeatherRequest(
                    airport="ATL",
                    start=start,
                    fields=[
                        "temperature",
                        "wind_speed",
                        "visibility",
                        "precipitation",
                        "snow_depth",
                    ],
                ),
                storage,
                http=SourceHTTP(storage, dns_mode=mode),
            )
        checked = check_saved(storage, result)
        report["samples"].append(
            dict(
                id=result["id"],
                fetched_this_execution=identifier is None,
                local_start=result["local_start"],
                source=result["source"],
                observation_count=result["observation_count"],
                coverage=result["coverage"],
                **checked,
            )
        )
        out.write_bytes(canonical_bytes(report))
    for cancel in (True, False):
        record = await verify_stop(storage, mode, cancel)
        report["network"].append(record)
        out.write_bytes(canonical_bytes(report))
        print(json.dumps(record, ensure_ascii=True), flush=True)
        await asyncio.sleep(2)
    report["passed"] = all(item["passed"] for item in report["network"])
    report["finished_at"] = utc_now()
    out.write_bytes(canonical_bytes(report))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
