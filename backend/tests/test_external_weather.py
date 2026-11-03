"""5.A 隔离合成观测和受控传输测试；不以样例替代真实来源验收。"""

import asyncio
import csv
import hashlib
import io
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import httpcore
import httpx
import pytest
from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.external.http import PublicNetworkBackend, SourceHTTP
from metric_pilot.external.registry import STATIONS, resolve_station, validate_url
from metric_pilot.external.weather import (
    FIELDS,
    WeatherRequest,
    local_window,
    parse_awc,
    parse_ghcnh,
    query_historical_weather,
    summarize,
)
from pydantic import ValidationError

NOW = datetime(2026, 11, 3, 2, tzinfo=UTC)
URL = "https://aviationweather.gov/api/data/metar"


class Pool:
    """
    作用：模拟可观察关闭的响应，不请求公网。
    输入：字节块、状态、响应头或阻塞信号。
    输出：受控连接池。
    """

    def __init__(self, chunks=(), status=200, headers=(), block=None):
        """
        作用：保存隔离传输条件。
        输入：响应条件。
        输出：无。
        """
        self.chunks, self.status, self.headers = chunks, status, headers
        self.block = block
        self.closed = False
        self.opened = 0

    async def __aenter__(self):
        """
        作用：进入隔离池。
        输入：无。
        输出：自身。
        """
        return self

    async def __aexit__(self, *args):
        """
        作用：记录池关闭。
        输入：退出原因。
        输出：无。
        """
        self.closed = True

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        """
        作用：提供与实际 httpcore 一致的流接口。
        输入：请求。
        输出：异步响应。
        """
        self.opened += 1

        async def content():
            """
            作用：输出片段或等待取消。
            输入：无。
            输出：响应字节。
            """
            for chunk in self.chunks:
                yield chunk
            if self.block:
                self.block.set()
                await asyncio.Event().wait()

        response = httpcore.Response(self.status, headers=self.headers, content=content())
        try:
            yield response
        finally:
            await response.aclose()


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_local_dates_dst(day, hours):
    """
    作用：核对 Windows IANA 时区与夏令时真实长度。
    输入：转换日期和预期小时。
    输出：无。
    """
    _, _, _, start, end = local_window(WeatherRequest(airport="ATL", start=day), NOW)
    assert (end - start).total_seconds() == hours * 3600
    _, start_day, _, start, end = local_window(
        WeatherRequest(airport="ATL", start="yesterday"), NOW
    )
    # UTC 11 月 3 日凌晨，在机场仍为 11 月 2 日；昨天应为 11 月 1 日。
    assert start_day == date(2026, 11, 1)
    assert start.hour == 4 and end.hour == 5


def test_mapping_and_input_boundaries():
    """
    作用：拒绝无站点、多个适用候选、过期映射、未来及过长范围。
    输入：无。
    输出：无。
    """
    for airport, mappings, code in [
        ("XXX", STATIONS, "WEATHER_STATION_NOT_FOUND"),
        ("ATL", (STATIONS[0], STATIONS[0]), "WEATHER_STATION_AMBIGUOUS"),
    ]:
        with pytest.raises(DataError, match=code):
            resolve_station(airport, date(2026, 1, 1), date(2026, 1, 1), mappings)
    with pytest.raises(DataError, match="WEATHER_MAPPING_DATE"):
        resolve_station("ATL", date(2025, 1, 1), date(2025, 1, 1))
    for start, end in [("2026-11-03", None), ("2026-02-30", None), ("2026-01-01", "2026-01-04")]:
        with pytest.raises(DataError):
            local_window(WeatherRequest(airport="ATL", start=start, end=end), NOW)
    with pytest.raises(ValidationError):
        WeatherRequest(airport="ATL", start="yesterday", url="http://localhost")


@pytest.mark.parametrize(
    "url",
    [
        "http://aviationweather.gov/api/data/metar",
        "https://127.0.0.1/api/data/metar",
        "https://aviationweather.gov.evil.example/api/data/metar",
        "https://aviationweather.gov:443/api/data/metar",
        "https://u:p@aviationweather.gov/api/data/metar",
        "https://aviationweather.gov/api/data/metar#fragment",
        "https://www.ncei.noaa.gov/oa/../secret",
        "file:///tmp/data",
    ],
)
def test_unsafe_urls(url):
    """
    作用：拒绝来源、端口、凭据、协议及路径绕过。
    输入：危险 URL。
    输出：无。
    """
    with pytest.raises(DataError, match="EXTERNAL_ADDRESS_REJECTED"):
        validate_url(url)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "::1", "169.254.169.254", "198.18.0.10", "::ffff:127.0.0.1"],
)
def test_dns_refuses_private_and_proxy_addresses(monkeypatch, address):
    """
    作用：校验实际连接前拒绝非公网 DNS，包括本机代理虚拟地址。
    输入：隔离 DNS 返回。
    输出：无。
    """

    async def run():
        """
        作用：注入解析结果，不建立任何网络连接。
        输入：无。
        输出：无。
        """

        async def lookup(*args, **kwargs):
            """
            作用：返回合成地址。
            输入：查询参数。
            输出：DNS 结果。
            """
            return [(None, None, None, None, (address, 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        with pytest.raises(DataError, match="EXTERNAL_ADDRESS_REJECTED"):
            await PublicNetworkBackend().connect_tcp("aviationweather.gov", 443)

    asyncio.run(run())


def awc_sample(observed, **values):
    """
    作用：生成明确合成的 AWC 格式数据。
    输入：时刻及覆盖字段。
    输出：测试行。
    """
    return dict(
        icaoId="KATL",
        obsTime=observed,
        metarType="METAR",
        lat=33.62972,
        lon=-84.44223,
        temp=0,
        wspd=10,
        visib="10+",
        **values,
    )


def test_awc_units_missing_bounds_duplicates_and_conflicts():
    """
    作用：验证单位、下限能见度、缺测、重复及同一时刻冲突处理。
    输入：无。
    输出：无。
    """
    _, _, _, start, end = local_window(WeatherRequest(airport="ATL", start="yesterday"), NOW)
    row = awc_sample(int(start.timestamp()))
    raw = canonical_bytes([row, row])
    observations = parse_awc(raw, "synthetic_fetch", STATIONS[0], start, end, list(FIELDS))
    rows, coverage, duplicates = summarize(observations, list(FIELDS), start, end)
    assert duplicates == 1 and len(rows) == 1
    assert coverage["temperature"]["exact_min"] == 0
    assert coverage["visibility"]["exact_min"] is None
    assert coverage["wind_speed"]["exact_max"] == pytest.approx(5.1444444444)
    assert coverage["precipitation"]["hours_without_observation"] == 25
    assert rows[0]["fields"]["precipitation"]["value"] is None
    conflict = dict(row, temp=20)
    observations += parse_awc(
        canonical_bytes([conflict]), "f2", STATIONS[0], start, end, list(FIELDS)
    )
    _, coverage, _ = summarize(observations, list(FIELDS), start, end)
    assert coverage["temperature"]["conflict_times"] == 1
    assert coverage["temperature"]["hours_with_observation"] == 0
    with pytest.raises(DataError, match="WEATHER_PROVIDER_RESULT_LIMIT"):
        parse_awc(canonical_bytes([row] * 400), "f", STATIONS[0], start, end, list(FIELDS))


def ghcnh_sample(quality="1", source="223", icao="ICAO-KATL", measurement=""):
    """
    作用：构造合成 PSV，避免将真实气象数据写入 fixture。
    输入：质量、来源及标识。
    输出：带表头的 UTF-8 PSV。
    """
    row = dict(
        STATION="USW00013874", DATE="2026-01-25T05:00:00", LATITUDE="33.6297", LONGITUDE="-84.4422"
    )
    for field in FIELDS:
        row.update(
            {
                field: "2.3",
                field + "_Quality_Code": quality,
                field + "_Source_Code": source,
                field + "_Source_Station_ID": icao,
                field + "_Report_Type": "FM15",
                field + "_Measurement_Code": measurement,
            }
        )
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(row), delimiter="|")
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode()


def test_ghcnh_quality_and_station():
    """
    作用：核对来源专用质量码、历史位置与原始数值不重复缩放。
    输入：无。
    输出：无。
    """

    async def run():
        """
        作用：解析合成历史字段与拒绝坏格式。
        输入：无。
        输出：无。
        """
        _, _, _, start, end = local_window(WeatherRequest(airport="ATL", start="2026-01-25"), NOW)
        for quality, source, accepted in [
            ("1", "223", True),
            ("0", "223", False),
            ("1", "313", False),
        ]:
            rows = await parse_ghcnh(
                ghcnh_sample(quality, source), "f", STATIONS[0], start, end, list(FIELDS)
            )
            assert rows[0]["fields"]["temperature"]["value"] == 2.3
            assert rows[0]["fields"]["temperature"]["accepted"] is accepted
            assert rows[0]["locator"] == "line:2"
            _, coverage, _ = summarize(rows, list(FIELDS), start, end)
            assert coverage["precipitation"]["exact_max"] is None
        for measurement, accepted in [
            ("1", False),
            ("E", False),
            ("I", False),
            ("J", False),
            ("2", True),
            ("4", True),
        ]:
            rows = await parse_ghcnh(
                ghcnh_sample(measurement=measurement), "f", STATIONS[0], start, end, list(FIELDS)
            )
            assert rows[0]["fields"]["precipitation"]["accepted"] is accepted
            assert rows[0]["fields"]["precipitation"]["measurement_code"] == measurement
        with pytest.raises(DataError, match="WEATHER_STATION_MISMATCH"):
            await parse_ghcnh(
                ghcnh_sample(icao="ICAO-KORD"), "f", STATIONS[0], start, end, list(FIELDS)
            )
        with pytest.raises(DataError, match="WEATHER_SOURCE_FORMAT"):
            await parse_ghcnh(b"<html>Error</html>", "f", STATIONS[0], start, end, list(FIELDS))

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,headers,chunks,code",
    [
        (302, (), (), "EXTERNAL_REDIRECT_REJECTED"),
        (401, (), (), "EXTERNAL_HTTP_ERROR"),
        (403, (), (), "EXTERNAL_HTTP_ERROR"),
        (404, (), (), "EXTERNAL_HTTP_ERROR"),
        (429, (), (), "EXTERNAL_HTTP_ERROR"),
        (500, (), (), "EXTERNAL_HTTP_ERROR"),
        (200, ((b"content-encoding", b"gzip"),), (), "EXTERNAL_ENCODING_REJECTED"),
        (200, (), (b"1234", b"5678"), "EXTERNAL_RESPONSE_LIMIT"),
    ],
)
def test_http_failure_closes_and_retains_partial(tmp_path, status, headers, chunks, code):
    """
    作用：错误页、重定向、压缩和超限不生成成功证据，且不重试。
    输入：隔离响应。
    输出：无。
    """

    async def run():
        """
        作用：执行并检查失败清单及原件哈希。
        输入：无。
        输出：无。
        """
        pool = Pool(chunks, status, headers)
        client = SourceHTTP(tmp_path, pool_factory=lambda: pool)
        with pytest.raises(DataError, match=code):
            await client.get(URL, max_bytes=5)
        assert pool.closed and pool.opened == 1
        record = client.records[0]
        assert record["status"] == "failed" and record["truncated"]
        raw = (client.root / f"{record['id']}.raw").read_bytes()
        assert len(raw) <= 5 and hashlib.sha256(raw).hexdigest() == record["sha256"]

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [True, False])
def test_request_cancel_and_deadline_close_stream(tmp_path, cancel):
    """
    作用：验证取消或总期限终止等待并关闭实际客户端上下文。
    输入：终止方式。
    输出：无。
    """

    async def run():
        """
        作用：阻塞隔离流，再取消或耗尽期限。
        输入：无。
        输出：无。
        """
        opened = asyncio.Event()
        pool = Pool((b"partial",), block=opened)
        client = SourceHTTP(tmp_path, timeout=2 if cancel else 0.03, pool_factory=lambda: pool)
        task = asyncio.create_task(client.get(URL))
        await opened.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(DataError, match="EXTERNAL_TIMEOUT"):
                await task
        assert pool.closed and task.done()
        assert client.records[0]["error_code"] == (
            "EXTERNAL_CANCELLED" if cancel else "EXTERNAL_TIMEOUT"
        )

    asyncio.run(run())


def test_independent_query_no_data_and_immutable_outputs(tmp_path):
    """
    作用：空观测真实输出且多次查询不覆盖旧结果；不调用模型或数据库。
    输入：临时目录。
    输出：无。
    """

    async def run():
        """
        作用：执行两个隔离空结果并检查独立文件。
        输入：无。
        输出：无。
        """
        first = None
        for _ in range(2):
            pool = Pool((b"[]",))
            client = SourceHTTP(tmp_path, pool_factory=lambda: pool)
            # 避免用实际等待验证间隔；网络期限仍启用。
            result = await query_historical_weather(
                WeatherRequest(airport="ATL", start="yesterday"), tmp_path, http=client, now=NOW
            )
            assert result["status"] == "no_observations"
            assert result["coverage"]["temperature"]["expected_utc_hours"] == 25
            assert len(result["fetches"]) == 3
            path = tmp_path / "external" / "weather" / f"{result['id']}.json"
            if first:
                assert path != first[0] and first[0].read_bytes() == first[1]
            first = path, path.read_bytes()
            with pytest.raises(DataError, match="EXTERNAL_REQUEST_LIMIT"):
                client.max_requests = client.requests
                await client.get(URL)

    asyncio.run(run())


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({"Status": 0, "Answer": [{"type": 1, "data": "150.171.109.148"}]}, "150.171.109.148"),
        ({"Status": 0, "Answer": [{"type": 1, "data": "10.0.0.1"}]}, None),
        (
            {
                "Status": 0,
                "Answer": [
                    {"type": 1, "data": "150.171.109.148"},
                    {"type": 1, "data": "127.0.0.1"},
                ],
            },
            None,
        ),
        ({"Status": 0, "Answer": None}, None),
        ({"Status": 2}, None),
        ([], None),
    ],
)
def test_explicit_public_dns_pins_only_public_answers(monkeypatch, answer, expected):
    """
    作用：验证固定加密 DNS、异常响应、混合内网答案及直连 IP，防止二次域名解析。
    输入：合成 DNS 响应及预期连接地址。
    输出：无。
    """

    async def run():
        """
        作用：用隔离 DoH 替代公网，但保留正式解析与连接选择逻辑。
        输入：无。
        输出：无。
        """
        targets = []
        original_client = httpx.AsyncClient

        def respond(request):
            """
            作用：核对只向固定 DNS 发送 NOAA 域名。
            输入：请求。
            输出：合成响应。
            """
            assert str(request.url).startswith("https://1.1.1.1/dns-query?")
            assert dict(request.url.params) == {"name": "aviationweather.gov", "type": "A"}
            return httpx.Response(200, json=answer)

        def client(**kwargs):
            """
            作用：确保代理和自动跳转保持关闭。
            输入：客户端配置。
            输出：隔离客户端。
            """
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
            return original_client(transport=httpx.MockTransport(respond), **kwargs)

        async def connect(self, host, port, **kwargs):
            """
            作用：记录拟直连的数字 IP，不建立网络连接。
            输入：连接参数。
            输出：无。
            """
            targets.append(host)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
        backend = PublicNetworkBackend("cloudflare")
        if expected:
            await backend.connect_tcp("aviationweather.gov", 443)
            assert targets == [expected]
        else:
            with pytest.raises(DataError):
                await backend.connect_tcp("aviationweather.gov", 443)
            assert not targets

    asyncio.run(run())
