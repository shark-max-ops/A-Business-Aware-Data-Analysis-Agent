"""受限 SQL 应用服务与手动 CLI；尚不是 Agent Runtime。"""

import argparse
import hashlib
import json
import re
import signal
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, canonical_bytes, new_id, utc_now
from metric_pilot.data.contracts import (
    ExplorationScope,
    QueryArtifact,
    RunContext,
    SchemaDTO,
    ToolResult,
)
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.guard import FUNCTIONS, GUARD_VERSION, validate_sql
from metric_pilot.data.models import ArtifactRecord, QueryExecution
from metric_pilot.data.ranges import resolve_range, run_range, scope_sql
from metric_pilot.database import create_metadata_engine
from metric_pilot.settings import Settings


class QueryService:
    """
    作用：组合权限目录、SQL guard、执行器与成功产物仓储
    输入：目录与执行策略
    输出：查询应用服务
    """

    def __init__(self, catalog: Catalog, executor: QueryExecutor):
        """
        作用：固定依赖，不读取用户指定数据库路径
        输入：目录与执行器
        输出：无
        """
        self.catalog = catalog
        self.executor = executor

    def execute(
        self,
        ctx: RunContext,
        sql: str,
        parameters: dict,
        cancel=None,
        on_started=None,
        *,
        metric_revision_ids=(),
        knowledge_revision_ids=(),
        query_purpose="legacy",
        exploration_scope=None,
        unavailable_metric_keys=(),
        external_snapshot_id=None,
    ) -> ToolResult:
        """
        作用：审计每次尝试，仅为成功执行产生不可变产物
        输入：可信执行上下文、SQL、标量参数与取消通知
        输出：统一工具结果
        """
        execution_id, began = new_id("exec"), time.monotonic()
        error_details = {}
        self.catalog.snapshot(ctx.snapshot_id)
        # 未通过参数校验时只保存空参数，避免非 JSON 值破坏失败审计。
        with Session(self.catalog.engine) as session, session.begin():
            session.add(
                QueryExecution(
                    id=execution_id,
                    run_id=ctx.run_id,
                    snapshot_id=ctx.snapshot_id,
                    status="running",
                    error_code=None,
                    created_at=utc_now(),
                    duration_ms=0,
                    sql=sql if isinstance(sql, str) else "",
                    parameters={},
                )
            )
        try:
            snapshot = self.catalog.snapshot(ctx.snapshot_id)
            selected_range = resolve_range(self.catalog.engine, snapshot.id, ctx.date_range)
            # 正式运行从持久化边界复核；离线验收的独立运行ID仍可使用显式上下文。
            from metric_pilot.agent.models import runs

            with self.catalog.engine.connect() as c:
                recorded = c.execute(select(runs.c.payload).where(runs.c.id == ctx.run_id)).first()
            if recorded and (
                recorded[0]["snapshot_id"] != ctx.snapshot_id
                or run_range(self.catalog.engine, recorded[0]) != selected_range
            ):
                raise DataError("RUN_DATE_RANGE_MISMATCH")
            external = None
            external_evidence_ids = []
            if external_snapshot_id is not None:
                from metric_pilot.external.evidence import ExternalEvidenceService

                external = ExternalEvidenceService(self.catalog.engine, self.catalog.root)
                target, database_path = external.query_target(ctx, external_snapshot_id)
                if metric_revision_ids:
                    raise DataError("EXTERNAL_METRIC_BINDING_NOT_ALLOWED")
                schema = SchemaDTO.model_validate(target["schema"])
                external_evidence_ids = [target["evidence_id"]]
                query_purpose = "exploratory"
                exploration_scope = ExplorationScope(
                    population=target["manifest"]["scope"]
                    + "；"
                    + target["manifest"]["airport"]
                    + "；"
                    + target["manifest"]["utc_start"]
                    + " 至 "
                    + target["manifest"]["utc_end_exclusive"],
                    calculation="对固定天气观测执行所附 SQL；非已审核项目指标，不与主表混合连接",
                    missing_values=target["manifest"]["quality"] + "；降水/积雪不开放累计列",
                )
            else:
                schema = self.catalog.schema(snapshot.source_id, snapshot.id)
                database_path = self.catalog.database_path(ctx.snapshot_id)
            guarded = validate_sql(
                sql, parameters, {table.name for table in schema.tables}, self.executor.max_rows
            )
            if external_snapshot_id is None and "v_flights" in guarded.tables:
                guarded = replace(guarded, scope_sql=scope_sql(selected_range))
            with Session(self.catalog.engine) as session, session.begin():
                execution = session.get(QueryExecution, execution_id)
                execution.parameters = parameters
                execution.external_snapshot_id = external_snapshot_id
            result = self.executor.execute(
                database_path,
                guarded,
                parameters,
                ctx.deadline_monotonic,
                cancel,
                on_started,
            )
            if cancel is not None and cancel.is_set():
                raise DataError("QUERY_CANCELLED")
            result_bytes = canonical_bytes({"columns": result["columns"], "rows": result["rows"]})
            result_hash = hashlib.sha256(result_bytes).hexdigest()
            folder = self.catalog.root / "artifacts"
            if not folder.resolve().is_relative_to(self.catalog.root):
                raise DataError("ARTIFACT_FILE_UNAVAILABLE")
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{result_hash}.json"
            if not target.resolve().is_relative_to(self.catalog.root):
                raise DataError("ARTIFACT_FILE_UNAVAILABLE")
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != result_hash:
                    raise DataError("ARTIFACT_HASH_MISMATCH")
            else:
                with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(result_bytes)
                temporary.replace(target)
            artifact = QueryArtifact(
                id=new_id("qa"),
                run_id=ctx.run_id,
                execution_id=execution_id,
                snapshot_id=ctx.snapshot_id,
                sql=sql,
                parameters=parameters,
                guard_version=GUARD_VERSION,
                normalized_sql=guarded.normalized_sql,
                executed_sql=guarded.limited_sql,
                parameter_keys=guarded.parameter_keys,
                tables=guarded.tables,
                functions=guarded.functions,
                columns=result["columns"],
                returned_rows=len(result["rows"]),
                truncated=result["truncated"],
                result_hash=result_hash,
                duration_ms=int((time.monotonic() - began) * 1000),
                created_at=utc_now(),
                ordered=guarded.ordered,
                metric_revision_ids=list(metric_revision_ids),
                knowledge_revision_ids=list(knowledge_revision_ids),
                query_purpose=query_purpose,
                exploration_scope=exploration_scope,
                unavailable_metric_keys=list(unavailable_metric_keys),
                external_snapshot_id=external_snapshot_id,
                external_evidence_ids=external_evidence_ids,
                date_range=selected_range,
                scope_sql=guarded.scope_sql,
            )
            if cancel is not None and cancel.is_set():
                raise DataError("QUERY_CANCELLED")
            if time.monotonic() >= ctx.deadline_monotonic:
                raise DataError("QUERY_TIMEOUT")
            if external:
                external.query_target(ctx, external_snapshot_id)
            with Session(self.catalog.engine) as session, session.begin():
                execution = session.get(QueryExecution, execution_id)
                execution.status = "succeeded"
                execution.duration_ms = artifact.duration_ms
                session.add(
                    ArtifactRecord(
                        id=artifact.id,
                        execution_id=execution_id,
                        run_id=ctx.run_id,
                        snapshot_id=ctx.snapshot_id,
                        payload=artifact.model_dump(),
                    )
                )
            return ToolResult(
                ok=True,
                summary=f"查询完成，保存 {artifact.returned_rows} 行"
                + ("（结果已截断）" if artifact.truncated else ""),
                artifact_ids=[artifact.id],
                data={"artifact": artifact.model_dump(), "preview": result["rows"][:20]},
            )
        except DataError as exc:
            code = exc.code
            if code == "FUNCTION_NOT_ALLOWED":
                # 仅反馈模型自己提交的函数名与服务端允许列表，不返回连接或异常正文。
                error_details = {
                    "function": str(exc.details.get("function", ""))[:80],
                    "allowed_functions": sorted(FUNCTIONS),
                    "instruction": "该函数未开放，不要原样重试。可选统计无法执行时，"
                    "保留已有成功核查，在报告说明该项限制；不要因此丢弃已有证据。",
                }
        except Exception:
            code = "QUERY_INTERNAL_ERROR"
        with Session(self.catalog.engine) as session, session.begin():
            execution = session.get(QueryExecution, execution_id)
            execution.status = "cancelled" if code == "QUERY_CANCELLED" else "failed"
            execution.error_code = code
            execution.duration_ms = int((time.monotonic() - began) * 1000)
        return ToolResult(
            ok=False,
            summary="查询未成功，未生成证据产物",
            error_code=code,
            data={
                "execution_id": execution_id,
                **({"error_details": error_details} if error_details else {}),
            },
        )

    def read_artifact(self, run_id: str, artifact_id: str, offset=0, limit=20) -> dict:
        """
        作用：仅分页读取指定运行的成功冻结结果并验证内容哈希
        输入：运行、产物标识与分页范围
        输出：产物元数据及有限结果
        """
        if offset < 0 or not 1 <= limit <= 100:
            raise DataError("INVALID_PAGE")
        with Session(self.catalog.engine) as session:
            record = session.get(ArtifactRecord, artifact_id)
            if record is None or record.run_id != run_id:
                raise DataError("ARTIFACT_NOT_FOUND")
            execution = session.get(QueryExecution, record.execution_id)
            if execution.status != "succeeded" or execution.run_id != run_id:
                raise DataError("ARTIFACT_NOT_FOUND")
            artifact = QueryArtifact.model_validate(record.payload)
            if (
                artifact.run_id != run_id
                or artifact.id != artifact_id
                or artifact.snapshot_id != execution.snapshot_id
                or artifact.execution_id != execution.id
                or artifact.external_snapshot_id != execution.external_snapshot_id
            ):
                raise DataError("ARTIFACT_NOT_FOUND")
        if (
            artifact.external_snapshot_id
            or artifact.external_evidence_ids
            or "v_weather_observations" in artifact.tables
        ):
            from metric_pilot.external.evidence import ExternalEvidenceService

            external = ExternalEvidenceService(self.catalog.engine, self.catalog.root)
            external.validate_artifact(external.run(run_id), artifact.model_dump())
        if not re.fullmatch(r"[0-9a-f]{64}", artifact.result_hash):
            raise DataError("ARTIFACT_HASH_MISMATCH")
        folder = (self.catalog.root / "artifacts").resolve()
        if not folder.is_relative_to(self.catalog.root):
            raise DataError("ARTIFACT_FILE_UNAVAILABLE")
        path = (folder / f"{artifact.result_hash}.json").resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise DataError("ARTIFACT_FILE_UNAVAILABLE")
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != artifact.result_hash:
            raise DataError("ARTIFACT_HASH_MISMATCH")
        result = json.loads(encoded)
        return {
            "artifact": artifact.model_dump(),
            "rows": result["rows"][offset : offset + limit],
            "offset": offset,
            "next_offset": offset + limit if offset + limit < artifact.returned_rows else None,
        }


def main():
    """
    作用：使用真实执行服务手动验证 SQL，不伪装自然语言 Agent
    输入：命令行中的快照 ID、SQL 与 JSON 标量参数
    输出：真实工具结果与执行退出码
    """
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="在已登记快照上执行受限只读 SQL")
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--sql", required=True)
    parser.add_argument("--params", default="{}")
    args = parser.parse_args()
    settings = Settings()
    engine = create_metadata_engine(settings)
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        try:
            parameters = json.loads(args.params)
        except ValueError:
            raise DataError("INVALID_PARAMETERS") from None
        context = RunContext(
            new_id("run"),
            args.snapshot_id,
            "knowledge_empty_v1",
            time.monotonic() + settings.run_timeout_seconds,
        )
        service = QueryService(
            Catalog(engine, settings.storage_dir),
            QueryExecutor(settings.sql_timeout_seconds, settings.sql_max_rows),
        )
        result = service.execute(context, args.sql, parameters, cancel)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True, allow_nan=False))
        if not result.ok:
            raise SystemExit(1)
    except DataError as exc:
        print(json.dumps({"error": {"code": exc.code}}, ensure_ascii=True))
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
