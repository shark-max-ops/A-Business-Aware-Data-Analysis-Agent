"""独立天气查询：当地日期、两种官方观测、原始质量及字段覆盖。"""

import asyncio
import csv
import io
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from metric_pilot.data.common import DataError, canonical_bytes, new_id, utc_now
from metric_pilot.external.http import SourceHTTP
from metric_pilot.external.registry import (
    AWC_ROOT,
    NCEI_ROOT,
    SOURCE_POLICIES,
    STATIONS,
    distance_km,
    resolve_station,
)

FIELDS = {
    "temperature": ("temp", "°C", 1),
    "wind_speed": ("wspd", "m/s", 0.5144444444444445),
    "visibility": ("visib", "km", 1.609344),
    "precipitation": ("precip", "mm", 25.4),
    "snow_depth": ("snow", "mm", 25.4),
}


class WeatherRequest(BaseModel):
    """
    作用：限定独立天气查询输入，不开放 URL、站点、权限或存储路径。
    输入：机场、当地日期或 yesterday、字段。
    输出：校验后的查询条件。
    """

    model_config = ConfigDict(extra="forbid")
    airport: str = Field(pattern=r"^[A-Z]{3,4}$")
    start: str = Field(pattern=r"^(yesterday|\d{4}-\d{2}-\d{2})$")
    end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    fields: list[str] = Field(
        default_factory=lambda: ["temperature", "wind_speed", "visibility"],
        min_length=1,
        max_length=5,
    )


def local_window(request: WeatherRequest, now: datetime):
    """
    作用：按机场当地日期解析范围，使用半开 UTC 区间正确处理跨日和夏令时。
    输入：请求与带时区的当前时刻。
    输出：映射、当地起止日期、UTC 起止时间。
    """
    names = [s for s in STATIONS if request.airport in (s.airport, s.icao)]
    if not names:
        raise DataError("WEATHER_STATION_NOT_FOUND")
    zone = ZoneInfo(names[0].timezone)
    today = now.astimezone(zone).date()
    try:
        start = (
            today - timedelta(days=1)
            if request.start == "yesterday"
            else (date.fromisoformat(request.start))
        )
        end = date.fromisoformat(request.end) if request.end else start
    except ValueError:
        raise DataError("WEATHER_DATE_INVALID") from None
    if not start <= end < today or (end - start).days >= 3:
        raise DataError("WEATHER_DATE_RANGE", {"max_local_days": 3, "completed_days_only": True})
    station = resolve_station(request.airport, start, end)
    return (
        station,
        start,
        end,
        datetime.combine(start, time.min, zone).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def number(raw):
    """
    作用：只接受有限数，缺失及特殊编码不转换成零。
    输入：原始字段值。
    输出：浮点值或 None。
    """
    try:
        value = float(raw) if not isinstance(raw, bool) else math.nan
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def check_position(station, latitude, longitude):
    """
    作用：拒绝站点迁移或来源位置不匹配，不自动选择附近其他站点。
    输入：已登记映射和本次原始观测坐标。
    输出：无；不匹配抛错。
    """
    lat, lon = number(latitude), number(longitude)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise DataError("WEATHER_STATION_MISMATCH")
    if distance_km(station.station_lat, station.station_lon, lat, lon) > 1:
        raise DataError("WEATHER_STATION_MISMATCH")


def parse_awc(raw, fetch_id, station, start_utc, end_utc, fields):
    """
    作用：解析 AWC 实际观测并保留边界值、原文和无质量码事实。
    输入：原件、抓取标识、映射、UTC 窗口及字段。
    输出：带 JSON 序号定位的观测。
    """
    try:
        items = json.loads(raw) if raw else []
        if not isinstance(items, list) or len(items) >= 400:
            raise DataError("WEATHER_PROVIDER_RESULT_LIMIT")
        result = []
        for index, item in enumerate(items):
            if item["icaoId"] != station.icao:
                raise DataError("WEATHER_STATION_MISMATCH")
            check_position(station, item.get("lat"), item.get("lon"))
            observed = datetime.fromtimestamp(item["obsTime"], UTC)
            if not start_utc <= observed < end_utc:
                continue
            cells = {}
            for field in fields:
                source_key, unit, factor = FIELDS[field]
                value_raw = item.get(source_key)
                qualifier = ""
                # 能见度 10+ 不是精确 10 英里；保留界限，不混入精确极值。
                if (
                    field == "visibility"
                    and isinstance(value_raw, str)
                    and (value_raw.endswith("+"))
                ):
                    qualifier = ">"
                value = number(value_raw.rstrip("+") if qualifier else value_raw)
                if value is not None and field != "temperature" and value < 0:
                    value = None
                cells[field] = dict(
                    raw=value_raw,
                    value=value * factor if value is not None else None,
                    unit=unit,
                    qualifier=qualifier,
                    quality_code=None,
                    quality_policy="AWC 未提供逐字段质量码，不等同于 GHCNh quality=1",
                    accepted=value is not None and item.get("metarType") in ("METAR", "SPECI"),
                    report_type=item.get("metarType"),
                )
            result.append(
                dict(
                    observed_at=observed.isoformat(),
                    fields=cells,
                    fetch_id=fetch_id,
                    locator=f"json[{index}]",
                    raw_report=item.get("rawOb"),
                    report_type=item.get("metarType"),
                    received_at=item.get("receiptTime"),
                )
            )
        return result
    except DataError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise DataError("WEATHER_SOURCE_FORMAT") from None


async def parse_ghcnh(raw, fetch_id, station, start_utc, end_utc, fields):
    """
    作用：解析完整年度 PSV，只选指定时间，保留未通过质量筛选的原值。
    输入：原件、抓取标识、映射、UTC 窗口及字段。
    输出：带原始行号和质量码的观测。
    """
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="|")
        required = {"STATION", "DATE", "LATITUDE", "LONGITUDE"}
        for field in fields:
            required.update(
                {
                    field,
                    field + "_Quality_Code",
                    field + "_Source_Code",
                    field + "_Source_Station_ID",
                    field + "_Report_Type",
                    field + "_Measurement_Code",
                }
            )
        if not required.issubset(reader.fieldnames or []):
            raise DataError("WEATHER_SOURCE_FORMAT")
        result = []
        previous_line = reader.line_num
        for index, row in enumerate(reader, 2):
            line_start, previous_line = previous_line + 1, reader.line_num
            # 本地大文件解析也可被取消，不用不可终止的后台线程继续处理。
            if index % 256 == 0:
                await asyncio.sleep(0)
            if None in row or any(value is None for value in row.values()):
                raise DataError("WEATHER_SOURCE_FORMAT")
            observed = datetime.fromisoformat(row["DATE"])
            if observed.tzinfo is not None:
                raise DataError("WEATHER_SOURCE_FORMAT")
            observed = observed.replace(tzinfo=UTC)
            if not start_utc <= observed < end_utc:
                continue
            if row["STATION"] != station.station:
                raise DataError("WEATHER_STATION_MISMATCH")
            check_position(station, row["LATITUDE"], row["LONGITUDE"])
            cells = {}
            for field in fields:
                quality = row[field + "_Quality_Code"]
                source = row[field + "_Source_Code"]
                source_station = row[field + "_Source_Station_ID"]
                report_type = row[field + "_Report_Type"]
                measurement = row[field + "_Measurement_Code"]
                value = number(row[field])
                if (
                    source == "223"
                    and value is not None
                    and source_station != ("ICAO-" + station.icao)
                ):
                    raise DataError("WEATHER_STATION_MISMATCH")
                if value is not None and field != "temperature" and value < 0:
                    value = None
                cells[field] = dict(
                    raw=row[field],
                    value=value,
                    unit=FIELDS[field][1],
                    quality_code=quality,
                    source_code=source,
                    source_station=source_station,
                    report_type=report_type,
                    measurement_code=measurement,
                    qualifier=">" if measurement == "P" else "",
                    accepted=value is not None
                    and quality == "1"
                    and source == "223"
                    and report_type in ("FM15", "FM16")
                    and (field != "precipitation" or measurement in ("", "2", "4")),
                    quality_policy=(
                        "仅 source223/quality1 的 METAR FM15、SPECI FM16；其他保留不纳入统计"
                    ),
                    measurement_policy=(
                        "降水仅纳入空码、2（微量）、4（累计结束）；其他测量标记保留但不计覆盖"
                        if field == "precipitation"
                        else "保留原始测量码"
                    ),
                )
            result.append(
                dict(
                    observed_at=observed.isoformat(),
                    fields=cells,
                    fetch_id=fetch_id,
                    locator=f"line:{line_start}",
                )
            )
            if len(result) > 20000:
                raise DataError("WEATHER_OBSERVATION_LIMIT")
        return result
    except DataError:
        raise
    except (UnicodeError, ValueError, TypeError, csv.Error, KeyError):
        raise DataError("WEATHER_SOURCE_FORMAT") from None


def summarize(observations, fields, start_utc, end_utc):
    """
    作用：按字段统计有观测的 UTC 小时，排除重复及冲突，不冒充完整小时平均。
    输入：原始定位观测、字段与 UTC 窗口。
    输出：去重观测、字段覆盖及重复计数。
    """
    unique = {}
    duplicates = 0
    # 同时刻不同内容保留为冲突，不能凭获取顺序挑选一条作为事实。
    for observation in observations:
        key = canonical_bytes(
            {
                k: v
                for k, v in observation.items()
                if k not in ("fetch_id", "locator", "received_at")
            }
        )
        if key in unique:
            duplicates += 1
        else:
            unique[key] = observation
    rows = sorted(unique.values(), key=lambda row: row["observed_at"])
    expected = int((end_utc - start_utc).total_seconds() / 3600)
    coverage = {}
    for field in fields:
        usable, conflicts = [], 0
        by_time = {}
        for row in rows:
            cell = row["fields"][field]
            if cell["accepted"]:
                by_time.setdefault(row["observed_at"], []).append(cell)
        for observed, cells in by_time.items():
            signatures = {(c["value"], c["qualifier"]) for c in cells}
            if len(signatures) != 1:
                conflicts += 1
                continue
            usable.append((observed, cells[0]))
        hours = {observed[:13] for observed, _ in usable}
        exact = [cell["value"] for _, cell in usable if not cell["qualifier"]]
        coverage[field] = dict(
            unit=FIELDS[field][1],
            raw_present=sum(row["fields"][field]["raw"] not in (None, "") for row in rows),
            numeric_present=sum(row["fields"][field]["value"] is not None for row in rows),
            accepted_observation_times=len(usable),
            conflict_times=conflicts,
            expected_utc_hours=expected,
            hours_with_observation=len(hours),
            hours_without_observation=expected - len(hours),
            coverage_definition="该 UTC 小时至少一条符合筛选的观测；不证明整小时连续覆盖",
            exact_min=(
                min(exact) if exact and field not in ("precipitation", "snow_depth") else None
            ),
            exact_max=(
                max(exact) if exact and field not in ("precipitation", "snow_depth") else None
            ),
            aggregation="仅覆盖，不累计降水/积雪"
            if field in ("precipitation", "snow_depth")
            else "仅精确值极值；特殊报告频次不等，不计算记录加权平均",
        )
    return rows, coverage, duplicates


async def _query_weather(request: WeatherRequest, storage: Path, *, http=None, now=None):
    """
    作用：独立查天气并冻结原件和解析结果，不生成报告证据或调用模型。
    输入：天气请求、服务端存储目录、可选隔离客户端和当前时间。
    输出：天气结果及来源；失败不写成功结果。
    """
    now = now or datetime.now(UTC)
    fields = request.fields
    if len(set(fields)) != len(fields) or any(field not in FIELDS for field in fields):
        raise DataError("WEATHER_FIELD_INVALID")
    station, start, end, start_utc, end_utc = local_window(request, now)
    source = "awc_metar" if start_utc >= now - timedelta(days=29) else "ncei_ghcnh"
    if not SOURCE_POLICIES[source]["enabled"]:
        raise DataError("EXTERNAL_SOURCE_DISABLED")
    client = http or SourceHTTP(storage)
    observations, fetches = [], []
    if source == "awc_metar":
        # 单机场每次最多 12 小时，达到服务条数上限即拒绝，不能当成完整统计。
        cursor = start_utc
        while cursor < end_utc:
            right = min(cursor + timedelta(hours=12), end_utc)
            params = dict(
                ids=station.icao, format="json", hours=12, date=right.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            raw, record = await client.get(AWC_ROOT + "/metar", params)
            observations.extend(parse_awc(raw, record["id"], station, cursor, right, fields))
            fetches.append(record)
            cursor = right
    else:
        # 完整有界读取年度文件；不以遇到下一日期就假定文件始终有序。
        for year in range(start_utc.year, (end_utc - timedelta(microseconds=1)).year + 1):
            url = (
                NCEI_ROOT + f"/hourly/access/by-year/{year}/psv/GHCNh_{station.station}_{year}.psv"
            )
            raw, record = await client.get(url, max_bytes=64 * 1024 * 1024)
            if record["http_status"] == 204:
                fetches.append(record)
                continue
            observations.extend(
                await parse_ghcnh(raw, record["id"], station, start_utc, end_utc, fields)
            )
            fetches.append(record)
    rows, coverage, duplicates = summarize(observations, fields, start_utc, end_utc)
    result = dict(
        id=new_id("weather"),
        schema_version="weather_v1",
        created_at=utc_now(),
        status="observations_available" if rows else "no_observations",
        source=source,
        source_policy=SOURCE_POLICIES[source],
        mapping=station.detail(),
        local_start=start.isoformat(),
        local_end=end.isoformat(),
        utc_start=start_utc.isoformat(),
        utc_end_exclusive=end_utc.isoformat(),
        coverage=coverage,
        duplicate_rows=duplicates,
        observation_count=len(rows),
        observations=rows,
        fetches=fetches,
        limitations=[
            "独立天气查询，尚未接入 Agent 或报告 ValueRef，不是已审核项目指标",
            "缺失不当作零；有观测不等于连续覆盖；不据此推断航班延误原因",
            "近期数据可能补报或修订；新查询另存原件，不改旧结果",
            "降水含不同累计区间和小时内重复报告；此工具不计算总降水",
            "GHCNh 来源与质量筛选不同于 AWC，未静默拼接；特殊报告可能更频繁",
        ],
    )
    root = storage / "external" / "weather"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{result['id']}.json").write_bytes(canonical_bytes(result))
    return result


async def query_historical_weather(request: WeatherRequest, storage: Path, *, http=None, now=None):
    """
    作用：为整个独立工具调用设置总期限，包含读取和可取消的文件解析。
    输入：天气条件、存储目录、服务端客户端和可选验收时钟。
    输出：真实天气结果；总期限到达不保存成功结果。
    """
    try:
        async with asyncio.timeout(120):
            return await _query_weather(request, storage, http=http, now=now)
    except TimeoutError:
        raise DataError("EXTERNAL_TIMEOUT") from None
