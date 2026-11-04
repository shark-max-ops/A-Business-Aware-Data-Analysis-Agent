"""固定天气查询表；只暴露有明确筛选含义的观测列，不开放降水累计。"""

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from metric_pilot.data.common import canonical_bytes, digest
from metric_pilot.data.executor import CONNECTION_CONFIG

WEATHER_FIELDS = {
    "temperature": "temperature_c",
    "wind_speed": "wind_speed_m_s",
    "visibility": "visibility_km_exact",
}


def weather_rows(weather, evidence_id):
    """
    作用：按实际观测时刻合并字段，未过质量、界限值或同刻冲突不伪装成精确值。
    输入：经原件复核的天气结果、服务端证据标识。
    输出：固定列顺序及观测行。
    """
    grouped = {}
    for observation in weather["observations"]:
        grouped.setdefault(observation["observed_at"], []).append(observation)
    names = ["observed_at_utc", "local_date", "airport", "station", "source_evidence_id"]
    for column in WEATHER_FIELDS.values():
        names.extend([column, column + "_state"])
    names.extend(["visibility_km_lower_bound", "raw_locators"])
    zone = ZoneInfo(weather["mapping"]["timezone"])
    rows = []
    for observed, observations in sorted(grouped.items()):
        timestamp = datetime.fromisoformat(observed)
        row = [
            timestamp.replace(tzinfo=None).isoformat(),
            timestamp.astimezone(zone).date().isoformat(),
            weather["mapping"]["airport"],
            weather["mapping"]["station"],
            evidence_id,
        ]
        lower = None
        for field in WEATHER_FIELDS:
            cells = [item["fields"][field] for item in observations if field in item["fields"]]
            values = {(cell["value"], cell["qualifier"]) for cell in cells if cell["accepted"]}
            value = None
            if len(values) > 1:
                state = "conflict"
            elif values:
                amount, qualifier = next(iter(values))
                state = "bound" if qualifier else "accepted_exact"
                if not qualifier:
                    value = amount
                elif field == "visibility" and qualifier == ">":
                    lower = amount
            else:
                state = "rejected" if any(c["value"] is not None for c in cells) else "missing"
            row.extend([value, state])
        row.extend(
            [
                lower,
                canonical_bytes(
                    [
                        dict(fetch_id=item["fetch_id"], locator=item["locator"])
                        for item in observations
                    ]
                ).decode("utf-8"),
            ]
        )
        rows.append(row)
    return names, rows


async def build_weather_database(folder: Path, snapshot_id, evidence_id, weather, *, check=None):
    """
    作用：用服务端固定 DDL 建只读查询快照，模型不能控制数据库路径或结构。
    输入：受限新目录、服务端 ID 和经原件复核的天气。
    输出：文件哈希、固定 schema 与清单，不登记为主数据源。
    """
    folder.mkdir(parents=True, exist_ok=False)
    names, rows = weather_rows(weather, evidence_id)
    path = folder / "analytics.duckdb"
    with duckdb.connect(str(path), config=CONNECTION_CONFIG) as connection:
        connection.execute("BEGIN TRANSACTION")
        connection.execute("""CREATE TABLE observations (
            observed_at_utc TIMESTAMP, local_date DATE, airport VARCHAR, station VARCHAR,
            source_evidence_id VARCHAR, temperature_c DOUBLE, temperature_c_state VARCHAR,
            wind_speed_m_s DOUBLE, wind_speed_m_s_state VARCHAR,
            visibility_km_exact DOUBLE, visibility_km_exact_state VARCHAR,
            visibility_km_lower_bound DOUBLE, raw_locators VARCHAR)""")
        if rows:
            # 固定类型结构只由服务端生成；一批 JSON 作为一个参数，避免 Python 逐单元格绑定开销。
            types = (
                "TIMESTAMP",
                "DATE",
                "VARCHAR",
                "VARCHAR",
                "VARCHAR",
                "DOUBLE",
                "VARCHAR",
                "DOUBLE",
                "VARCHAR",
                "DOUBLE",
                "VARCHAR",
                "DOUBLE",
                "VARCHAR",
            )
            layout = canonical_bytes([dict(zip(names, types, strict=True))]).decode("utf-8")
            sql = (
                "INSERT INTO observations SELECT "
                + ", ".join("observation." + name for name in names)
                + " FROM UNNEST(from_json(?, ?)) AS batch(observation)"
            )
            for offset in range(0, len(rows), 256):
                await asyncio.sleep(0)
                if check is not None:
                    check()
                batch = canonical_bytes(
                    [dict(zip(names, row, strict=True)) for row in rows[offset : offset + 256]]
                ).decode("utf-8")
                connection.execute(sql, [batch, layout])
        await asyncio.sleep(0)
        if check is not None:
            check()
        connection.execute("CREATE VIEW v_weather_observations AS SELECT * FROM observations")
        columns = [
            dict(name=row[0], type=row[1], description="")
            for row in connection.execute("DESCRIBE v_weather_observations").fetchall()
        ]
        connection.execute("COMMIT")
    schema = dict(
        snapshot_id=snapshot_id,
        tables=[
            dict(
                name="v_weather_observations",
                columns=columns,
                sample=[dict(zip(names, row, strict=True)) for row in rows[:5]],
                join_keys=[],
            )
        ],
    )
    return dict(
        id=snapshot_id,
        evidence_id=evidence_id,
        row_count=len(rows),
        schema=schema,
        schema_hash=digest(schema),
        database_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        manifest=dict(
            source=weather["source"],
            airport=weather["mapping"]["airport"],
            station=weather["mapping"]["station"],
            timezone=weather["mapping"]["timezone"],
            local_start=weather["local_start"],
            local_end=weather["local_end"],
            utc_start=weather["utc_start"],
            utc_end_exclusive=weather["utc_end_exclusive"],
            fields=list(weather["coverage"]),
            coverage=weather["coverage"],
            rules_version="weather_sql_v1",
            scope="每个原始观测时刻一行，空测量保留；COUNT(*) 不等于有效测量数量",
            quality="数值仅采用通过筛选的精确观测，同刻冲突置空；界限另列，缺失不补零",
            limitations=["降水、积雪只保留原始资料，本表不开放累计列", "观测不单独证明航班原因"],
        ),
    )
