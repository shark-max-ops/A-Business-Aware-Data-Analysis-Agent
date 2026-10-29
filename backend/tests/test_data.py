"""阶段 1 真实导入、AST 防护、子进程生命周期与证据测试。"""

import csv
import hashlib
import json
import multiprocessing
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import duckdb
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, new_id
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor, open_analysis_connection
from metric_pilot.data.guard import validate_sql
from metric_pilot.data.ingest import ingest, parse_value
from metric_pilot.data.models import ArtifactRecord, QueryExecution
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.main import create_app
from metric_pilot.settings import Settings
from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/bts_reporting_synthetic.csv"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """
    作用：建立每项测试独立的真实迁移数据库
    输入：临时目录与环境隔离
    输出：配置和目录服务
    """
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    config = Settings(
        _env_file=None, storage_dir=tmp_path, llm_api_key="", llm_base_url="", llm_model=""
    )
    engine = create_metadata_engine(config)
    yield config, Catalog(engine, tmp_path)
    engine.dispose()


@pytest.fixture
def dataset(workspace):
    """
    作用：导入标记为合成的真实 CSV 和代码表
    输入：独立工作区
    输出：配置、目录和固定快照
    """
    config, catalog = workspace
    snapshot = ingest(
        config,
        FIXTURE,
        "2026-01",
        synthetic=True,
        carriers=ROOT / "fixtures/carriers_synthetic.csv",
        airports=ROOT / "fixtures/airports_synthetic.csv",
    )
    return config, catalog, snapshot


def context(snapshot_id, seconds=20):
    """
    作用：模拟可信服务端固定上下文，不从工具参数读取权限
    输入：快照标识与截止时长
    输出：不可变执行上下文
    """
    return RunContext(new_id("run"), snapshot_id, "knowledge_empty_v1", time.monotonic() + seconds)


def mutate_csv(tmp_path, update):
    """
    作用：构造明确的边界输入
    输入：临时目录与行变换函数
    输出：变更后的 CSV 路径
    """
    with FIXTURE.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows, columns = list(reader), reader.fieldnames
    columns, rows = update(columns, rows)
    path = tmp_path / "changed.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_import_manifest_schema_and_snapshots(dataset):
    """
    作用：核对实际行数、质量、哈希、结构与快照切换不覆盖
    输入：真实导入的合成快照
    输出：无
    """
    config, catalog, snapshot = dataset
    assert snapshot.row_count == 10
    assert snapshot.quality_report["rows_raw"] == 11
    assert snapshot.quality_report["out_of_period"] == 1
    assert snapshot.quality_report["null_counts"]["arrival_delay"] == 5
    assert snapshot.file_hash == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    source = catalog.sources()[0]
    assert source.source_kind == "synthetic"
    assert source.current_snapshot_id is None
    schema = catalog.schema(source.id, snapshot.id)
    assert {t.name for t in schema.tables} == {"v_flights", "dim_carriers", "dim_airports"}
    path = catalog.database_path(snapshot.id)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog.activate(source.id, snapshot.id)
    second = ingest(config, FIXTURE, "2026-01", synthetic=True)
    catalog.activate(source.id, second.id)
    assert second.id != snapshot.id
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    assert catalog.snapshot(snapshot.id).file_hash == second.file_hash
    with open_analysis_connection(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_flights").fetchone()[0] == 11
        assert connection.execute("SELECT COUNT(*) FROM v_flights").fetchone()[0] == 10
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v_flights f LEFT JOIN dim_airports o ON "
                "f.origin_airport_id=o.airport_id LEFT JOIN dim_airports d ON "
                "f.dest_airport_id=d.airport_id"
            ).fetchone()[0]
            == 10
        )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("Cancelled", "2", "INVALID_FIELD"),
        ("ArrDelay", "NaN", "INVALID_FIELD"),
        ("ArrDelayMinutes", "-1", "INVALID_FIELD"),
        ("FlightDate", "invalid", "INVALID_FIELD"),
        ("CRSDepTime", "2460", "INVALID_FIELD"),
        ("DOT_ID_Reporting_Airline", "1.5", "INVALID_FIELD"),
    ],
)
def test_invalid_input_is_not_registered(workspace, tmp_path, field, value, code):
    """
    作用：拒绝无效数字、状态和时间，不发布半成品快照
    输入：边界字段和值
    输出：无
    """
    config, catalog = workspace

    def update(columns, rows):
        """
        作用：修改一行字段
        输入：列与数据行
        输出：变更后的列与数据行
        """
        rows[0][field] = value
        return columns, rows

    path = mutate_csv(tmp_path, update)
    with pytest.raises(DataError, match=code):
        ingest(config, path, "2026-01", synthetic=True)
    assert catalog.sources() == []
    assert not list((config.storage_dir / "datasets").glob(".import-*"))


def test_missing_marketing_synthetic_and_duplicate_dimension(workspace, tmp_path):
    """
    作用：防止表口径混淆、缺列和代码表重复键
    输入：工作区和临时目录
    输出：无
    """
    config, catalog = workspace
    with pytest.raises(DataError, match="SYNTHETIC_FLAG_REQUIRED"):
        ingest(config, FIXTURE, "2026-01")
    path = mutate_csv(
        tmp_path,
        lambda cols, rows: (
            [c for c in cols if c != "ArrDelay"],
            [{k: v for k, v in row.items() if k != "ArrDelay"} for row in rows],
        ),
    )
    with pytest.raises(DataError, match="MISSING_COLUMNS"):
        ingest(config, path, "2026-01", synthetic=True)
    path = mutate_csv(
        tmp_path,
        lambda cols, rows: (
            cols + ["Marketing_Airline_Network"],
            [dict(row, Marketing_Airline_Network="ZZ") for row in rows],
        ),
    )
    with pytest.raises(DataError, match="MARKETING_CARRIER_NOT_ALLOWED"):
        ingest(config, path, "2026-01", synthetic=True)
    with pytest.raises(DataError, match="DUPLICATE_DIMENSION_KEY"):
        ingest(
            config,
            FIXTURE,
            "2026-01",
            synthetic=True,
            airports=ROOT / "fixtures/airports_duplicate_synthetic.csv",
        )
    assert catalog.sources() == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "/* SELECT */ DELETE FROM v_flights",
        "SELECT * INTO new_table FROM v_flights",
        "WITH t AS (DELETE FROM v_flights RETURNING *) SELECT * FROM t",
        "COPY v_flights TO 'bad.csv'",
        "ATTACH 'bad.db' AS other",
        "PRAGMA version",
        "SET threads=99",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM read_csv('secret.csv')",
        "SELECT * FROM read_parquet('https://example.com/x')",
        "SELECT read_blob('secret')",
        "SELECT * FROM range(1000000000)",
        "SELECT * FROM generate_series(1,9)",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM raw_flights",
        "SELECT * FROM other.main.v_flights",
        "SELECT current_setting('threads')",
        "WITH v_flights AS (SELECT * FROM raw_flights) SELECT * FROM v_flights",
        "WITH RECURSIVE t AS (SELECT 1 UNION ALL SELECT 1 FROM t) SELECT * FROM t",
        "SELECT * FROM query_table('raw_flights')",
        "SELECT nextval('sequence')",
        "SELECT * FROM v_flights USING SAMPLE 100%",
        "SELECT * FROM v_flights PIVOT(COUNT(*) FOR carrier_id IN (90001))",
    ],
)
def test_sql_attack_rejected(sql):
    """
    作用：验证完整 AST、表、函数和作用域防护
    输入：禁止的 SQL
    输出：无
    """
    with pytest.raises(DataError):
        validate_sql(sql, {}, {"v_flights"}, 1000)


@pytest.mark.parametrize(
    "sql,params",
    [
        ("SELECT $x", {}),
        ("SELECT 1", {"x": 1}),
        ("SELECT ?", {"1": 1}),
        ("SELECT $x", {"x": []}),
        ("SELECT $x", {"x": float("nan")}),
    ],
)
def test_invalid_parameter_binding(sql, params):
    """
    作用：拒绝位置参数、缺失绑定、额外绑定及非标量
    输入：SQL 与参数
    输出：无
    """
    with pytest.raises(DataError):
        validate_sql(sql, params, {"v_flights"}, 1000)


def test_cte_union_allowed_and_denominators(dataset):
    """
    作用：实际执行项目口径，核对负延误、取消、备降和零分母
    输入：真实 DuckDB 合成快照
    输出：无
    """
    _, catalog, snapshot = dataset
    ctx = context(snapshot.id)
    service = QueryService(catalog, QueryExecutor())
    sql = """WITH eligible AS (
        SELECT * FROM v_flights WHERE cancelled=0 AND diverted=0
        AND arrival_delay IS NOT NULL AND arrival_delay_nonnegative IS NOT NULL
        AND arrival_delayed_15 IN (0,1))
        SELECT carrier_id, COUNT(*) AS eligible, AVG(arrival_delay) AS deviation,
        AVG(arrival_delay_nonnegative) AS nonnegative,
        100.0*SUM(CASE WHEN arrival_delayed_15=1 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0) AS rate
        FROM eligible WHERE flight_date >= $start AND flight_date < $end
        GROUP BY carrier_id ORDER BY carrier_id"""
    result = service.execute(ctx, sql, {"start": "2026-01-01", "end": "2026-02-01"})
    assert result.ok, result
    saved = service.read_artifact(ctx.run_id, result.artifact_ids[0])
    zz, zx = saved["rows"]
    assert zz[:2] == [90001, 3]
    assert zz[2:] == pytest.approx([10 / 3, 20 / 3, 100 / 3])
    assert zx[:2] == [90002, 2]
    assert zx[2:] == pytest.approx([14.5, 14.5, 50])
    population = service.execute(
        ctx,
        "SELECT carrier_id, COUNT(*) AS scheduled, SUM(cancelled) AS cancelled, "
        "SUM(diverted) AS diverted FROM v_flights GROUP BY carrier_id ORDER BY "
        "carrier_id",
        {},
    )
    assert population.ok
    assert service.read_artifact(ctx.run_id, population.artifact_ids[0])["rows"] == [
        [90001, 3, 0, 0],
        [90002, 5, 1, 1],
        [90003, 2, 1, 1],
    ]
    zero = service.execute(
        ctx,
        "SELECT 100.0*SUM(arrival_delayed_15)/NULLIF(COUNT(*),0) AS rate FROM "
        "v_flights WHERE carrier_id=90003 AND cancelled=0 AND diverted=0",
        {},
    )
    assert zero.ok
    assert service.read_artifact(ctx.run_id, zero.artifact_ids[0])["rows"] == [[None]]
    union = service.execute(
        ctx,
        "SELECT carrier_id FROM v_flights WHERE carrier_id=90001 UNION SELECT "
        "carrier_id FROM v_flights WHERE carrier_id=90002 ORDER BY carrier_id",
        {},
    )
    assert union.ok


def test_truncation_empty_decimal_and_failed_evidence(dataset):
    """
    作用：检查真实冻结值、截断、空结果、Decimal 及失败不产生证据
    输入：真实快照
    输出：无
    """
    _, catalog, snapshot = dataset
    ctx = context(snapshot.id)
    service = QueryService(catalog, QueryExecutor(max_rows=2))
    result = service.execute(ctx, "SELECT carrier_id FROM v_flights ORDER BY record_id", {})
    assert result.ok and result.data["artifact"]["truncated"]
    assert result.data["artifact"]["returned_rows"] == 2
    assert not service.execute(ctx, "SELECT 1; SELECT 2", {}).ok
    assert not service.execute(ctx, "SELECT missing_column FROM v_flights", {}).ok
    assert not service.execute(ctx, "SELECT 1.0/0.0 AS infinite", {}).ok
    empty = service.execute(ctx, "SELECT carrier_id FROM v_flights WHERE 1=0", {})
    assert empty.ok and empty.data["artifact"]["returned_rows"] == 0
    value = service.execute(
        ctx,
        "SELECT CAST(1.20 AS DECIMAL(18,2)) AS precise, NULL AS absent, "
        "CAST(9007199254740993 AS BIGINT) AS large_integer",
        {},
    )
    assert value.ok
    assert service.read_artifact(ctx.run_id, value.artifact_ids[0])["rows"] == [
        [
            {"type": "decimal", "value": "1.20"},
            None,
            {"type": "integer", "value": "9007199254740993"},
        ]
    ]
    with pytest.raises(DataError, match="ARTIFACT_NOT_FOUND"):
        service.read_artifact("run_another", value.artifact_ids[0])
    with Session(catalog.engine) as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(QueryExecution)
                .where(QueryExecution.status == "failed")
            )
            == 3
        )
    artifact = value.data["artifact"]
    path = catalog.root / "artifacts" / f"{artifact['result_hash']}.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(DataError, match="ARTIFACT_HASH_MISMATCH"):
        service.read_artifact(ctx.run_id, value.artifact_ids[0])


def test_guard_before_executor_and_connection_constraints(dataset):
    """
    作用：证明非法 SQL 不进入执行器，连接本身也禁止外部读取和写入
    输入：真实快照
    输出：无
    """
    _, catalog, snapshot = dataset

    class NeverExecute(QueryExecutor):
        """
        作用：测试中检测非法查询越过防护边界
        输入：无
        输出：无
        """

        def execute(self, *args, **kwargs):
            """
            作用：禁止测试非法 SQL 到达执行器
            输入：执行参数
            输出：失败断言
            """
            raise AssertionError("guard was bypassed")

    result = QueryService(catalog, NeverExecute()).execute(
        context(snapshot.id), "SELECT * FROM raw_flights", {}
    )
    assert result.error_code == "TABLE_NOT_ALLOWED"
    with open_analysis_connection(catalog.database_path(snapshot.id)) as connection:
        with pytest.raises(duckdb.Error):
            connection.execute("CREATE TABLE forbidden(a INT)")
        with pytest.raises(duckdb.Error):
            connection.execute("SELECT * FROM read_csv(?)", [str(FIXTURE)])
        assert (
            connection.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0]
            is False
        )
        assert (
            connection.execute("SELECT current_setting('enable_external_access')").fetchone()[0]
            is False
        )
        assert (
            connection.execute("SELECT current_setting('autoload_known_extensions')").fetchone()[0]
            is False
        )


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_running_query_is_terminated_and_reaped(dataset, mode):
    """
    作用：在真实昂贵查询开始后验证超时或取消会结束底层进程
    输入：真实快照和终止方式
    输出：无
    """
    _, catalog, snapshot = dataset
    ctx = context(snapshot.id)
    cancel, pids, timers = threading.Event(), [], []

    def started(pid):
        """
        作用：记录实际执行 PID 并在取消测试中延迟发出取消
        输入：查询子进程 PID
        输出：无
        """
        pids.append(pid)
        if mode == "cancel":
            timer = threading.Timer(0.15, cancel.set)
            timers.append(timer)
            timer.start()

    tables = ", ".join(f"v_flights t{i}" for i in range(10))
    expression = "+".join(f"COALESCE(t{i}.arrival_delay,0)" for i in range(10))
    service = QueryService(catalog, QueryExecutor(timeout_seconds=5 if mode == "timeout" else 15))
    result = service.execute(
        ctx, f"SELECT SUM({expression}) AS expensive FROM {tables}", {}, cancel, started
    )
    for timer in timers:
        timer.join()
    assert pids, "query must actually reach the execution stage"
    assert result.error_code == ("QUERY_TIMEOUT" if mode == "timeout" else "QUERY_CANCELLED")
    assert not set(pids) & {child.pid for child in multiprocessing.active_children()}
    with Session(catalog.engine) as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 0
        assert session.scalar(select(QueryExecution.status)) == (
            "failed" if mode == "timeout" else "cancelled"
        )


def test_api_real_snapshot_and_cross_source_boundary(dataset):
    """
    作用：核对真实 API 列表、结构、激活、错误与跨来源归属
    输入：真实快照
    输出：无
    """
    config, catalog, snapshot = dataset
    source_id = snapshot.source_id
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/health").json()["stage"] == 1
        assert client.get("/api/v1/sources").json()["items"][0]["source_kind"] == "synthetic"
        assert (
            client.get(f"/api/v1/sources/{source_id}/snapshots").json()["items"][0]["row_count"]
            == 10
        )
        response = client.get(
            f"/api/v1/sources/{source_id}/schema", params={"snapshot_id": snapshot.id}
        )
        assert response.status_code == 200
        assert response.json()["tables"][0]["name"] == "v_flights"
        assert (
            client.post(
                f"/api/v1/sources/{source_id}/activate", json={"snapshot_id": snapshot.id}
            ).status_code
            == 200
        )
        assert client.get(f"/api/v1/sources/{source_id}/schema").status_code == 200
        invalid = client.get("/api/v1/sources/missing/schema")
        assert invalid.status_code == 404 and invalid.json()["error"]["code"] == "SOURCE_NOT_FOUND"
        assert (
            client.post(
                f"/api/v1/sources/{source_id}/activate",
                json={"snapshot_id": snapshot.id, "path": "forbidden"},
            ).status_code
            == 422
        )
        assert str(config.storage_dir) not in json.dumps(response.json())
    with pytest.raises(DataError):
        catalog.database_path("../../outside")


def test_byte_limit(dataset):
    """
    作用：验证字节预算也会标记截断，单行过大不能生成产物
    输入：真实快照
    输出：无
    """
    _, catalog, snapshot = dataset
    service = QueryService(catalog, QueryExecutor(max_bytes=120))
    result = service.execute(context(snapshot.id), "SELECT origin_code FROM v_flights", {})
    assert result.ok and result.data["artifact"]["truncated"]
    result = service.execute(context(snapshot.id), "SELECT '" + "x" * 1000 + "' AS huge", {})
    assert not result.ok and result.error_code == "RESULT_TOO_LARGE"


def test_duplicate_rows_retained_and_metadata_empty_state(workspace, tmp_path):
    """
    作用：检查重复候选保留与真实空来源列表
    输入：独立工作区和临时目录
    输出：无
    """
    config, catalog = workspace
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/sources").json() == {"items": [], "next_cursor": None}
    path = mutate_csv(tmp_path, lambda columns, rows: (columns, rows + [dict(rows[0])]))
    snapshot = ingest(config, path, "2026-01", synthetic=True)
    assert snapshot.row_count == 11
    assert snapshot.quality_report["duplicate_candidates"] == 1
    assert snapshot.quality_report["rows_raw"] == 12


def test_cross_source_and_artifact_api_boundaries(dataset):
    """
    作用：拒绝跨来源激活和跨运行证据读取，检查分页边界
    输入：真实快照
    输出：无
    """
    from metric_pilot.data.models import DataSource

    config, catalog, snapshot = dataset
    other_source = new_id("source")
    with Session(catalog.engine) as session, session.begin():
        session.add(
            DataSource(
                id=other_source,
                name="test only",
                source_kind="official_bts",
                current_snapshot_id=None,
            )
        )
    ctx = context(snapshot.id)
    result = QueryService(catalog, QueryExecutor()).execute(
        ctx, "SELECT COUNT(*) AS flights FROM v_flights", {}
    )
    assert result.ok
    artifact_id = result.artifact_ids[0]
    with TestClient(create_app(config)) as client:
        assert (
            client.post(
                f"/api/v1/sources/{other_source}/activate", json={"snapshot_id": snapshot.id}
            ).status_code
            == 404
        )
        assert client.get(f"/api/v1/runs/{ctx.run_id}/artifacts/{artifact_id}").json()["rows"] == [
            [10]
        ]
        assert client.get(f"/api/v1/runs/run_other/artifacts/{artifact_id}").status_code == 404
        assert (
            client.get(f"/api/v1/runs/{ctx.run_id}/artifacts/{artifact_id}?limit=101").status_code
            == 422
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell encoding regression")
def test_cli_powershell_legacy_decoding(workspace):
    """
    作用：在 CP936 输出解码的 PowerShell 中验证真实 CLI JSON 管道
    输入：隔离迁移工作区
    输出：无
    """
    shell = shutil.which("powershell.exe")
    if not shell:
        pytest.skip("Windows PowerShell is unavailable")
    config, _ = workspace
    python = str(Path(sys.executable)).replace("'", "''")
    fixture = str(FIXTURE).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::GetEncoding(936)
$OutputEncoding = [System.Text.Encoding]::ASCII
$importArgs = @('-m', 'metric_pilot.data.ingest', '--input', '{fixture}',
                '--period', '2026-01', '--synthetic', '--activate')
$demo = & '{python}' @importArgs | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $demo.id -or $demo.row_count -ne 10) {{
    throw 'Import JSON failed'
}}
$published = Join-Path (Join-Path $env:STORAGE_DIR 'datasets') $demo.id
if ((Get-Acl -LiteralPath $published).AreAccessRulesProtected) {{
    throw 'Published snapshot must inherit workspace permissions'
}}
if (-not $demo.manifest.policy.Contains('不填充空值')) {{
    throw 'Unicode was corrupted'
}}
$queryArgs = @('-m', 'metric_pilot.data.query', '--snapshot-id', $demo.id,
               '--sql', 'SELECT COUNT(*) AS scheduled FROM v_flights')
$query = & '{python}' @queryArgs | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $query.ok -or $query.data.preview[0][0] -ne 10) {{
    throw 'Query JSON failed'
}}
$queryArgs[-1] = 'SELECT * FROM raw_flights'
$rejected = & '{python}' @queryArgs | ConvertFrom-Json
if ($LASTEXITCODE -ne 1 -or $rejected.ok) {{ throw 'Failure JSON failed' }}
if ($rejected.error_code -ne 'TABLE_NOT_ALLOWED') {{ throw 'Wrong failure code' }}
Write-Output 'POWERSHELL_PIPELINE_OK'
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"POWERSHELL_PIPELINE_OK" in result.stdout


def test_bts_database_export_headers_and_dates(workspace, tmp_path):
    """
    作用：验证实际 BTS 数据库导出名称和美式午夜日期，使用合成数据复现
    输入：隔离工作区和临时目录
    输出：无
    """
    config, catalog = workspace
    aliases = {
        "FlightDate": "FL_DATE",
        "Reporting_Airline": "OP_UNIQUE_CARRIER",
        "DOT_ID_Reporting_Airline": "OP_CARRIER_AIRLINE_ID",
        "Flight_Number_Reporting_Airline": "OP_CARRIER_FL_NUM",
        "Tail_Number": "TAIL_NUM",
        "ArrDelayMinutes": "ARR_DELAY_NEW",
    }

    def export(columns, rows):
        """
        作用：将合成文件转为实际导出格式
        输入：原字段和数据行
        输出：导出字段及行
        """
        renamed = {c: aliases.get(c, c.upper()) for c in columns}
        converted = []
        for row in rows:
            year, month, day = row["FlightDate"].split("-")
            row["FlightDate"] = f"{int(month)}/{int(day)}/{year} 12:00:00 AM"
            converted.append({renamed[k]: v for k, v in row.items()})
        return list(renamed.values()), converted

    path = mutate_csv(tmp_path, export)
    snapshot = ingest(config, path, "2026-01", synthetic=True)
    assert snapshot.row_count == 10
    assert snapshot.quality_report["out_of_period"] == 1
    assert snapshot.manifest["column_mapping"]["ARR_DELAY_NEW"] == "arrival_delay_nonnegative"
    assert snapshot.manifest["column_mapping"]["OP_UNIQUE_CARRIER"] == "carrier_code"
    with open_analysis_connection(catalog.database_path(snapshot.id)) as connection:
        row = connection.execute(
            "SELECT CAST(flight_date AS VARCHAR), carrier_code, carrier_id, "
            "arrival_delay, arrival_delay_nonnegative FROM v_flights "
            "ORDER BY source_row_number LIMIT 1"
        ).fetchone()
        assert row == ("2026-01-01", "ZZ", 90001, -10.0, 0.0)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1/2/2026 12:00:00 AM", "2026-01-02"),
        ("2/1/2026 12:00:00 AM", "2026-02-01"),
        ("2026-01-02 00:00:00", "2026-01-02"),
    ],
)
def test_explicit_business_date_formats(value, expected):
    """
    作用：禁止依赖系统地域猜测月日顺序
    输入：受支持日期及预期日期
    输出：无
    """
    assert parse_value(value, "flight_date", "DATE") == expected


@pytest.mark.parametrize("value", ["1/2/2026 12:00:00 PM", "1/2/2026 01:00:00 AM"])
def test_non_midnight_export_date_rejected(value):
    """
    作用：日期字段不接受带业务时刻的隐式截断
    输入：非午夜日期时间
    输出：无
    """
    with pytest.raises(ValueError):
        parse_value(value, "flight_date", "DATE")


@pytest.mark.parametrize(
    "column,code",
    [
        ("ARR_DELAY_NEW", "AMBIGUOUS_COLUMNS"),
        ("MKT_UNIQUE_CARRIER", "MARKETING_CARRIER_NOT_ALLOWED"),
    ],
)
def test_export_alias_conflict_and_marketing_rejected(workspace, tmp_path, column, code):
    """
    作用：拒绝同时出现的同义列以及 Marketing 导出列
    输入：工作区、临时文件和冲突列
    输出：无
    """
    config, catalog = workspace
    path = mutate_csv(
        tmp_path,
        lambda columns, rows: (columns + [column], [dict(row, **{column: "0"}) for row in rows]),
    )
    with pytest.raises(DataError, match=code):
        ingest(config, path, "2026-01", synthetic=True)
    assert catalog.sources() == []


@pytest.mark.parametrize(
    "column,internal",
    [
        ("CRSElapsedTime", "scheduled_elapsed"),
        ("ActualElapsedTime", "actual_elapsed"),
        ("AirTime", "air_time"),
    ],
)
def test_negative_duration_retained_and_flagged(workspace, tmp_path, column, internal):
    """
    作用：复现真实源文件负时长，保留原值并形成可定位质量记录
    输入：工作区、临时目录和时长字段
    输出：无
    """
    config, catalog = workspace

    def update(columns, rows):
        """
        作用：为合成首行加入异常时长
        输入：列与数据行
        输出：变更后的列与数据行
        """
        rows[0][column] = "-64.00"
        return columns, rows

    path = mutate_csv(tmp_path, update)
    snapshot = ingest(config, path, "2026-01", synthetic=True)
    assert snapshot.row_count == 10
    assert snapshot.quality_report["rows_raw"] == 11
    assert snapshot.quality_report["negative_duration_counts"][internal] == 1
    assert snapshot.quality_report["negative_duration_examples"][internal] == [
        {"source_row_number": 2, "value": -64.0}
    ]
    with open_analysis_connection(catalog.database_path(snapshot.id)) as connection:
        assert (
            connection.execute(
                f"SELECT {internal} FROM v_flights WHERE source_row_number=2"
            ).fetchone()[0]
            == -64.0
        )
