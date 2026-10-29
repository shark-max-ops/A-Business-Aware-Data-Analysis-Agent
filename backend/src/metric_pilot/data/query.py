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
from pathlib import Path

from sqlalchemy.orm import Session

from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, canonical_bytes, new_id, utc_now
from metric_pilot.data.contracts import QueryArtifact, RunContext, ToolResult
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.guard import GUARD_VERSION, validate_sql
from metric_pilot.data.models import ArtifactRecord, QueryExecution
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
        self, ctx: RunContext, sql: str, parameters: dict, cancel=None, on_started=None
    ) -> ToolResult:
        """
        作用：审计每次尝试，仅为成功执行产生不可变产物
        输入：可信执行上下文、SQL、标量参数与取消通知
        输出：统一工具结果
        """
        execution_id, began = new_id("exec"), time.monotonic()
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
            schema = self.catalog.schema(snapshot.source_id, snapshot.id)
            guarded = validate_sql(
                sql, parameters, {table.name for table in schema.tables}, self.executor.max_rows
            )
            with Session(self.catalog.engine) as session, session.begin():
                session.get(QueryExecution, execution_id).parameters = parameters
            result = self.executor.execute(
                self.catalog.database_path(ctx.snapshot_id),
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
            )
            if cancel is not None and cancel.is_set():
                raise DataError("QUERY_CANCELLED")
            if time.monotonic() >= ctx.deadline_monotonic:
                raise DataError("QUERY_TIMEOUT")
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
            data={"execution_id": execution_id},
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
            ):
                raise DataError("ARTIFACT_NOT_FOUND")
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
