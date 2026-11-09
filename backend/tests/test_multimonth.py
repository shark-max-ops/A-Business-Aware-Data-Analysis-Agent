"""6.B：明确合成的多月数据、实际DuckDB、迁移及原子发布验收。"""

import json
import threading
import time
from pathlib import Path

import duckdb
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from metric_pilot.data.audit import audit_months, checksum
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.ingest import ingest
from metric_pilot.data.multimonth import ImportControl, ingest_months
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.main import create_app
from sqlalchemy import text
from test_bts_audit import synthetic_row, write_month
from test_foundation import migrate, settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """
    作用：建立隔离应用存储与合成月度文件，绝不使用真实原件
    输入：pytest临时目录和环境替身
    输出：配置、输入目录和目录服务
    """
    config = settings(tmp_path / "store")
    migrate(monkeypatch, config.storage_dir)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    write_month(inputs / "2026-01.csv", [synthetic_row("2026-01-31")])
    write_month(inputs / "2026-02.csv", [synthetic_row("2026-02-01")])
    engine = create_metadata_engine(config)
    yield config, inputs, Catalog(engine, config.storage_dir)
    engine.dispose()


def assert_no_publication(config, catalog):
    """
    作用：核对失败后没有登记或遗留本次临时/正式快照
    输入：隔离配置和目录服务
    输出：无
    """
    assert not catalog.sources()
    folder = config.storage_dir / "datasets"
    assert not folder.exists() or not list(folder.iterdir())


def test_multimonth_lineage_query_and_legacy_immutability(workspace):
    """
    作用：验证来源行号不冲突、异常和重复保留、安全查询及旧产物不变
    输入：隔离工作区
    输出：无
    """
    config, inputs, catalog = workspace
    old = ingest(config, inputs / "2026-01.csv", "2026-01", synthetic=True)
    catalog.activate(old.source_id, old.id)
    old_folder = config.storage_dir / "datasets" / old.id
    before = {p.name: checksum(p) for p in old_folder.iterdir()}
    query = QueryService(catalog, QueryExecutor(max_rows=100, timeout_seconds=5))
    result = query.execute(
        RunContext("old", old.id, "knowledge", time.monotonic() + 10),
        "SELECT count(*) AS n FROM v_flights",
        {},
    )
    assert result.ok
    artifact = query.read_artifact("old", result.artifact_ids[0])
    # 同一来源内完全重复的记录也保留，并在质量中计数。
    january = synthetic_row("2026-01-31", CRSElapsedTime="-16")
    write_month(inputs / "2026-01.csv", [january, january])
    write_month(inputs / "2026-02.csv", [synthetic_row("2026-02-01", CRSElapsedTime="")])
    snapshot = ingest_months(config, inputs, ["2026-02", "2026-01"], synthetic=True)
    assert snapshot.period is None
    assert snapshot.periods == ["2026-01", "2026-02"]
    assert snapshot.coverage_start == "2026-01-01"
    assert snapshot.coverage_end_exclusive == "2026-03-01"
    assert snapshot.row_count == 3
    assert snapshot.quality_report["duplicate_candidates"] == 1
    assert snapshot.quality_report["negative_duration_counts"]["scheduled_elapsed"] == 2
    assert snapshot.quality_report["null_counts"]["scheduled_elapsed"] == 1
    with duckdb.connect(str(catalog.database_path(snapshot.id)), read_only=True) as connection:
        assert connection.execute(
            "SELECT count(DISTINCT record_id), count(DISTINCT source_file_id) FROM v_flights"
        ).fetchone() == (3, 2)
        rows = connection.execute(
            "SELECT source_period,source_row_number,scheduled_elapsed FROM v_flights ORDER BY 1,2"
        ).fetchall()
        assert rows == [("2026-01", 2, -16), ("2026-01", 3, -16), ("2026-02", 2, None)]
    sources = snapshot.manifest["sources"]
    for source in sources:
        original = config.storage_dir / "datasets" / snapshot.id / source["file"]
        assert checksum(original) == source["sha256"]
        assert original.read_bytes() == (inputs / source["original_name"]).read_bytes()
    ctx = RunContext("multi", snapshot.id, "knowledge", time.monotonic() + 10)
    result = query.execute(
        ctx,
        "SELECT source_period,count(*) AS n FROM v_flights "
        "GROUP BY source_period ORDER BY source_period",
        {},
    )
    assert result.ok
    assert query.read_artifact("multi", result.artifact_ids[0])["rows"] == [
        ["2026-01", 2],
        ["2026-02", 1],
    ]
    assert query.read_artifact("old", artifact["artifact"]["id"]) == artifact
    assert catalog.sources()[0].current_snapshot_id == old.id
    assert before == {p.name: checksum(p) for p in old_folder.iterdir()}
    assert catalog.snapshot(old.id).periods == ["2026-01"]
    assert catalog.activate(snapshot.source_id, snapshot.id).current_snapshot_id == snapshot.id
    assert before == {p.name: checksum(p) for p in old_folder.iterdir()}
    assert query.read_artifact("old", artifact["artifact"]["id"]) == artifact


@pytest.mark.parametrize(
    "periods,error",
    [
        (["2026-01"], "UNIQUE_MULTIPLE_PERIODS_REQUIRED"),
        (["2026-01", "2026-01"], "UNIQUE_MULTIPLE_PERIODS_REQUIRED"),
        (["2026-01", "2026-03"], "MISSING_MONTH"),
        (["2026-01", "2026-13"], "INVALID_PERIOD"),
        (["2026-02", "2026-03"], "MONTH_FILE_UNAVAILABLE"),
    ],
)
def test_invalid_month_collection(workspace, periods, error):
    """
    作用：拒绝不完整或重复月份，不能发布空快照
    输入：隔离工作区、月份和预期错误
    输出：无
    """
    config, inputs, catalog = workspace
    with pytest.raises(DataError, match=error):
        ingest_months(config, inputs, periods, synthetic=True)
    assert_no_publication(config, catalog)


@pytest.mark.parametrize(
    "change,error",
    [
        ({"FlightDate": "2026-01-31"}, "MONTH_DATE_OVERLAP"),
        ({"ArrDelay": "NaN"}, "INVALID_FIELD"),
        ({"Cancelled": ""}, "INVALID_FIELD"),
    ],
)
def test_bad_second_file_rolls_back_entire_snapshot(workspace, change, error):
    """
    作用：首月已入临时库后次月失败，仍不登记或保留半成品
    输入：隔离工作区、错误字段与错误码
    输出：无
    """
    config, inputs, catalog = workspace
    write_month(
        inputs / "2026-02.csv",
        [synthetic_row("2026-02-01", Flight_Number_Reporting_Airline="2", **change)],
    )
    with pytest.raises(DataError, match=error):
        ingest_months(config, inputs, ["2026-01", "2026-02"], synthetic=True)
    assert_no_publication(config, catalog)


def test_duplicate_file_content_rejected(workspace):
    """
    作用：拒绝两个文件名实际装同一原件
    输入：隔离工作区
    输出：无
    """
    config, inputs, catalog = workspace
    (inputs / "2026-02.csv").write_bytes((inputs / "2026-01.csv").read_bytes())
    with pytest.raises(DataError, match="DUPLICATE_SOURCE_FILE"):
        ingest_months(config, inputs, ["2026-01", "2026-02"], synthetic=True)
    assert_no_publication(config, catalog)


def test_official_inputs_require_matching_audit(workspace):
    """
    作用：验证正式来源必须提供清单，原件变化不能复用旧审计
    输入：隔离工作区和明确合成的全月样本
    输出：无
    """
    from metric_pilot.data.audit import month_days

    config, inputs, catalog = workspace
    with pytest.raises(DataError, match="AUDIT_MANIFEST_REQUIRED"):
        ingest_months(config, inputs, ["2026-01", "2026-02"])
    for period in ("2026-01", "2026-02"):
        write_month(inputs / (period + ".csv"), [synthetic_row(day) for day in month_days(period)])
    report = audit_months(inputs, ["2026-01", "2026-02"], user_confirmed_reporting_all=True)
    audit = inputs.parent / "audit.json"
    audit.write_text(json.dumps(report), encoding="utf-8")
    # 此测试只是合成输入走正式入口，不代表真实BTS来源验收。
    snapshot = ingest_months(config, inputs, ["2026-01", "2026-02"], audit_manifest=audit)
    assert snapshot.row_count == 59
    assert snapshot.manifest["sources"][0]["provenance"]["acquired_at"] is None
    assert (
        config.storage_dir / "datasets" / snapshot.id / "audit.json"
    ).read_bytes() == audit.read_bytes()
    with (inputs / "2026-02.csv").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(DataError, match="SOURCE_HASH_MISMATCH"):
        ingest_months(config, inputs, ["2026-01", "2026-02"], audit_manifest=audit)
    assert len(catalog.snapshots(snapshot.source_id)) == 1
    assert not list((config.storage_dir / "datasets").glob(".import-*"))


@pytest.mark.parametrize("point", ["before", "during", "after_rename", "register"])
def test_cancellation_or_publication_failure_cleans_up(workspace, monkeypatch, point):
    """
    作用：核对各发布边界取消/失败不遗留可用快照
    输入：隔离工作区、替身和取消位置
    输出：无
    """
    import metric_pilot.data.multimonth as module

    config, inputs, catalog = workspace
    cancel = threading.Event()
    if point == "before":
        cancel.set()
    elif point == "during":
        original = module.prepare_month

        def prepare(*args, **kwargs):
            """
            作用：在冻结原件阶段结束后触发取消
            输入：真实准备参数
            输出：真实准备结果
            """
            result = original(*args, **kwargs)
            cancel.set()
            return result

        monkeypatch.setattr(module, "prepare_month", prepare)
    elif point == "after_rename":
        original = Path.rename

        def rename(path, target):
            """
            作用：在完整文件目录发布后、元数据提交前触发取消
            输入：路径和目标
            输出：真实重命名结果
            """
            result = original(path, target)
            cancel.set()
            return result

        monkeypatch.setattr(Path, "rename", rename)
    else:

        def fail(*args, **kwargs):
            """
            作用：模拟原子登记失败
            输入：登记参数
            输出：无，抛出错误
            """
            raise DataError("TEST_REGISTER_FAILED")

        monkeypatch.setattr(Catalog, "register", fail)
    with pytest.raises(DataError, match="IMPORT_CANCELLED|TEST_REGISTER_FAILED"):
        ingest_months(config, inputs, ["2026-01", "2026-02"], synthetic=True, cancel=cancel)
    assert_no_publication(config, catalog)


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_actual_duckdb_execution_interrupted(mode):
    """
    作用：用实际长查询确认取消和超时中断连接，而不是只停止等待
    输入：取消方式
    输出：无，短时间退出后同一连接仍可查询
    """
    cancel = threading.Event()
    control = ImportControl(cancel, 0.15 if mode == "timeout" else 10)
    timer = threading.Timer(0.15, cancel.set) if mode == "cancel" else None
    with duckdb.connect(":memory:") as connection:
        if timer:
            timer.start()
        began = time.monotonic()
        with pytest.raises(DataError, match="IMPORT_CANCELLED|IMPORT_TIMEOUT"):
            with control.watch(connection):
                connection.execute("SELECT sum(sin(i)) FROM range(10000000000) t(i)").fetchone()
        assert time.monotonic() - began < 5
        assert connection.execute("SELECT 1").fetchone() == (1,)
        if timer:
            timer.join()


def test_cross_year_range(workspace):
    """
    作用：核对跨年月份和结束日不落回旧年份
    输入：隔离工作区
    输出：无
    """
    config, inputs, _ = workspace
    write_month(inputs / "2025-12.csv", [synthetic_row("2025-12-31")])
    snapshot = ingest_months(config, inputs, ["2025-12", "2026-01"], synthetic=True)
    assert snapshot.coverage_start == "2025-12-01"
    assert snapshot.coverage_end_exclusive == "2026-02-01"


def test_migration_preserves_referenced_snapshots(workspace, monkeypatch):
    """
    作用：验证含真实外键引用的旧库迁移及旧产物读取，不重写历史清单
    输入：隔离工作区与环境替身
    输出：无
    """
    config, inputs, catalog = workspace
    old = ingest(config, inputs / "2026-01.csv", "2026-01", synthetic=True)
    query = QueryService(catalog, QueryExecutor(max_rows=100, timeout_seconds=5))
    result = query.execute(
        RunContext("old", old.id, "k", time.monotonic() + 10),
        "SELECT count(*) AS n FROM v_flights",
        {},
    )
    assert result.ok
    before = query.read_artifact("old", result.artifact_ids[0])
    cfg = Config(str(ROOT / "alembic.ini"))
    monkeypatch.setenv("STORAGE_DIR", str(config.storage_dir))
    catalog.engine.dispose()
    command.downgrade(cfg, "0007_external_evidence")
    command.upgrade(cfg, "head")
    assert query.read_artifact("old", result.artifact_ids[0]) == before
    assert catalog.snapshot(old.id).manifest == old.manifest
    with catalog.engine.connect() as c:
        assert not c.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_multimonth_blocks_downgrade_and_investigation(workspace, monkeypatch):
    """
    作用：禁止为了降级丢弃多月数据，API不让多月范围进入旧单月调查
    输入：隔离工作区与环境替身
    输出：无
    """
    config, inputs, catalog = workspace
    snapshot = ingest_months(config, inputs, ["2026-01", "2026-02"], synthetic=True)
    monkeypatch.setenv("STORAGE_DIR", str(config.storage_dir))
    with pytest.raises(RuntimeError, match="MULTIMONTH_SNAPSHOTS_PREVENT_DOWNGRADE"):
        command.downgrade(Config(str(ROOT / "alembic.ini")), "0007_external_evidence")
    with catalog.engine.connect() as c:
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0008_multimonth"
        )
    with TestClient(create_app(config)) as client:
        conv = client.post("/api/v1/conversations").json()
        response = client.post(
            "/api/v1/investigations",
            json={
                "conversation_id": conv["id"],
                "snapshot_id": snapshot.id,
                "goal": "合成目标",
                "scope": "合成范围",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DATE_RANGE_REQUIRED"


def test_forged_coverage_does_not_bypass_actual_check(workspace):
    """
    作用：即使本地清单谎称通过，正式导入仍从实际数据拒绝不完整月份
    输入：隔离工作区和合成数据
    输出：无
    """
    config, inputs, catalog = workspace
    report = audit_months(inputs, ["2026-01", "2026-02"])
    report["technical_checks_passed"] = True
    for month in report["months"]:
        month["technical_checks_passed"] = True
        month["technical_blockers"] = []
    path = inputs.parent / "forged-audit.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(DataError, match="MONTH_DATE_COVERAGE_INCOMPLETE"):
        ingest_months(config, inputs, ["2026-01", "2026-02"], audit_manifest=path)
    assert_no_publication(config, catalog)


def test_independent_verifier_with_synthetic_raw_export(workspace, monkeypatch):
    """
    作用：用人工可算的官方别名格式合成样本验证独立验收脚本及SQL对照
    输入：隔离工作区与脚本导入路径
    输出：无，不冒充真实BTS验收
    """
    import csv

    from metric_pilot.data.ingest import FIELDS

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from verify_bts_snapshot import RAW_COLUMNS
    from verify_multimonth import verify

    config, inputs, _ = workspace
    fields = {name: original for original, name, *_ in FIELDS}
    for period, month in (("2026-01", 1), ("2026-02", 2)):
        row = synthetic_row(f"{month}/1/2026 12:00:00 AM")
        with (inputs / (period + ".csv")).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(RAW_COLUMNS)
            writer.writerow([row[fields[name]] for name in RAW_COLUMNS.values()])
    snapshot = ingest_months(config, inputs, ["2026-01", "2026-02"], synthetic=True)
    result = verify(config, snapshot.id, inputs)
    assert result["passed"]
    assert result["total_rows"] == 2
    assert all(month["comparison_count"] == 37 for month in result["months"])
