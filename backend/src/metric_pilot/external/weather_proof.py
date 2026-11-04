"""从成功抓取原件复算 5.A 派生结果，避免把编辑过的 JSON 直接升级为证据。"""

from datetime import datetime, timedelta

from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.external.registry import AWC_ROOT, NCEI_ROOT
from metric_pilot.external.weather import (
    WeatherRequest,
    local_window,
    parse_awc,
    parse_ghcnh,
    summarize,
)


def comparable(observations):
    """
    作用：比较原始定位及数值/质量字段，不因说明文字追加而改写历史结果。
    输入：观测集合。
    输出：规范化关键字段。
    """
    keys = {
        "raw",
        "value",
        "unit",
        "qualifier",
        "accepted",
        "quality_code",
        "report_type",
        "source_code",
        "source_station",
        "measurement_code",
    }
    return [
        dict(
            observed_at=row["observed_at"],
            fetch_id=row["fetch_id"],
            locator=row["locator"],
            fields={
                field: {key: value for key, value in cell.items() if key in keys}
                for field, cell in row["fields"].items()
            },
        )
        for row in observations
    ]


async def verify_weather(weather, read_fetch):
    """
    作用：复核采集范围、全部请求和原件，再验证冻结值来自实际解析结果。
    输入：保存的天气、受限原件读取函数。
    输出：请求与已核对原件字节列表；不发网络请求。
    """
    if weather.get("schema_version") != "weather_v1":
        raise DataError("EXTERNAL_DERIVATION_MISMATCH")
    fields = list(weather["coverage"])
    request = WeatherRequest(
        airport=weather["mapping"]["airport"],
        start=weather["local_start"],
        end=weather["local_end"],
        fields=fields,
    )
    captured = datetime.fromisoformat(weather["created_at"])
    if captured.tzinfo is None:
        raise DataError("EXTERNAL_DERIVATION_MISMATCH")
    station, _, _, left, right = local_window(request, captured)
    if (
        left.isoformat() != weather["utc_start"]
        or right.isoformat() != weather["utc_end_exclusive"]
        or station.detail() != weather["mapping"]
    ):
        raise DataError("EXTERNAL_DERIVATION_MISMATCH")
    rows, originals = [], []
    fetches = iter(weather["fetches"])
    if weather["source"] == "awc_metar":
        cursor = left
        while cursor < right:
            end = min(cursor + timedelta(hours=12), right)
            record = next(fetches, None)
            params = dict(
                ids=station.icao, format="json", hours=12, date=end.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            raw = read_fetch(record, AWC_ROOT + "/metar", params)
            rows.extend(parse_awc(raw, record["id"], station, cursor, end, fields))
            originals.append((record, raw))
            cursor = end
    elif weather["source"] == "ncei_ghcnh":
        for year in range(left.year, (right - timedelta(microseconds=1)).year + 1):
            record = next(fetches, None)
            url = (
                NCEI_ROOT + f"/hourly/access/by-year/{year}/psv/GHCNh_{station.station}_{year}.psv"
            )
            raw = read_fetch(record, url, {})
            if record["http_status"] != 204:
                rows.extend(await parse_ghcnh(raw, record["id"], station, left, right, fields))
            originals.append((record, raw))
    else:
        raise DataError("EXTERNAL_SOURCE_INVALID")
    if next(fetches, None) is not None:
        raise DataError("EXTERNAL_DERIVATION_MISMATCH")
    rows, coverage, duplicate = summarize(rows, fields, left, right)
    if (
        canonical_bytes(comparable(rows)) != canonical_bytes(comparable(weather["observations"]))
        or len(rows) != weather["observation_count"]
        or duplicate != weather["duplicate_rows"]
        or weather["status"] != ("observations_available" if rows else "no_observations")
    ):
        raise DataError("EXTERNAL_DERIVATION_MISMATCH")
    for field, old in weather["coverage"].items():
        for key in (
            "raw_present",
            "accepted_observation_times",
            "conflict_times",
            "expected_utc_hours",
            "hours_with_observation",
            "hours_without_observation",
            "exact_min",
            "exact_max",
            "unit",
        ):
            if old[key] != coverage[field][key]:
                raise DataError("EXTERNAL_DERIVATION_MISMATCH")
    return originals
