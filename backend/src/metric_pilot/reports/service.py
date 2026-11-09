"""报告服务：冻结来源、不可变保存、取证与显式修订。"""

from sqlalchemy import select

from metric_pilot.agent import models as agent_models
from metric_pilot.agent.investigations import read_evidence_artifact
from metric_pilot.data.common import DataError, digest, new_id, utc_now
from metric_pilot.data.ranges import run_range
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports import models as m
from metric_pilot.reports.contracts import EvidenceDTO, ReportDraft, ReportDTO, ReportSummary
from metric_pilot.reports.materialize import artifact_ids, content_hash, materialize
from metric_pilot.reports.sources import freeze_sources


def weather_scope_key(arguments):
    """
    作用：比较本轮天气失败与后续成功是否为同一请求，字段顺序不影响范围身份。
    输入：服务端保存的工具参数。
    输出：固定比较摘要，不作为业务指标。
    """
    args = dict(arguments)
    if isinstance(args.get("fields"), list) and all(
        isinstance(field, str) for field in args["fields"]
    ):
        args["fields"] = sorted(args["fields"])
    return digest(args)


class ReportService(KnowledgeStore):
    """
    作用：组合报告验证、保存与固定证据读取
    输入：查询服务与同一元数据库
    输出：报告应用服务
    """

    def __init__(self, query):
        """
        作用：绑定已有安全查询读取服务
        输入：QueryService
        输出：无
        """
        super().__init__(query.catalog.engine)
        self.query = query

    def prepare(self, run, draft, *, require_query=True):
        """
        作用：读取本次成功产物并冻结所有报告依据
        输入：运行与模型草稿
        输出：尚未提交的报告准备数据
        """
        draft = ReportDraft.model_validate(draft)
        with self.engine.connect() as connection:
            checks = (
                connection.execute(
                    select(agent_models.executions).where(
                        agent_models.executions.c.run_id == run["id"],
                        agent_models.executions.c.name == "query_historical_weather",
                    )
                )
                .mappings()
                .all()
            )
        successful = {
            weather_scope_key(row["arguments"]) for row in checks if row["status"] == "succeeded"
        }
        unresolved = any(
            row["status"] == "failed" and weather_scope_key(row["arguments"]) not in successful
            for row in checks
        )
        artifacts = {}
        for ident in artifact_ids(draft, require_query=require_query):
            offset, rows = 0, []
            while True:
                page = read_evidence_artifact(self.query, run, ident, offset, 100)
                artifact = page["artifact"]
                if artifact["snapshot_id"] != run["snapshot_id"]:
                    raise DataError("REPORT_SNAPSHOT_MISMATCH")
                if artifact["truncated"]:
                    raise DataError("REPORT_TRUNCATED_RESULT")
                if len({c["name"] for c in artifact["columns"]}) != len(artifact["columns"]):
                    raise DataError("REPORT_AMBIGUOUS_COLUMNS")
                rows.extend(page["rows"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            if len(rows) != artifact["returned_rows"]:
                raise DataError("REPORT_RESULT_INCOMPLETE")
            artifacts[ident] = {"artifact": artifact, "rows": rows}
        snapshot = self.query.catalog.snapshot(run["snapshot_id"]).model_dump()
        sources, metrics, knowledge = freeze_sources(self, run, artifacts)
        external_ids = {
            ident
            for block in draft.blocks
            if block.type == "text"
            for ident in block.evidence_ids
            if ident.startswith("xe_")
        }
        external_snapshots = {}
        for item in artifacts.values():
            external_ids.update(item["artifact"].get("external_evidence_ids", []))
        if external_ids:
            from metric_pilot.external.evidence import ExternalEvidenceService

            external = ExternalEvidenceService(self.engine, self.query.catalog.root)
            sources.update(external.collect(run, external_ids))
            for item in artifacts.values():
                if item["artifact"].get("external_snapshot_id"):
                    metadata = external.validate_artifact(external.run(run["id"]), item["artifact"])
                    external_snapshots[metadata["id"]] = metadata
        return materialize(
            draft,
            {
                "run_id": run["id"],
                "question": run["question"],
                "data_snapshot": snapshot,
                "date_range": run_range(self.engine, run).model_dump(),
                "knowledge_snapshot_id": run["knowledge_snapshot_id"],
                "knowledge_snapshot": knowledge,
                "sources": sources,
                "metrics": metrics,
                "artifacts": artifacts,
                "external_snapshots": external_snapshots,
            },
            require_query=require_query,
            runtime_warnings=[
                "本轮仍有天气核查未成功完成，相关请求不能视为覆盖检查通过；未查询日期不表示来源无资料。"
            ]
            if unresolved
            else [],
        )

    @staticmethod
    def persist(connection, prepared, report_id=None, version=1):
        """
        作用：在调用者事务内追加不可变报告修订
        输入：短事务与服务端准备数据
        输出：报告结果引用
        """
        report_id = report_id or new_id("report")
        revision_id, created = new_id("reportrev"), utc_now()
        payload = {
            **prepared,
            "report_id": report_id,
            "revision_id": revision_id,
            "version": version,
            "created_at": created,
        }
        if version == 1:
            connection.execute(
                m.reports.insert().values(
                    id=report_id,
                    run_id=prepared["run_id"],
                    latest_revision_id=revision_id,
                    version=version,
                    created_at=created,
                )
            )
        else:
            connection.execute(
                m.reports.update()
                .where(m.reports.c.id == report_id)
                .values(latest_revision_id=revision_id, version=version)
            )
        connection.execute(
            m.revisions.insert().values(
                id=revision_id,
                report_id=report_id,
                version=version,
                payload=payload,
                content_hash=content_hash(payload),
                created_at=created,
            )
        )
        return {"kind": "report", "report_id": report_id, "revision_id": revision_id}

    def frozen(self, report_id, revision_id):
        """
        作用：读取指定固定修订并校验归属与摘要
        输入：报告 ID 和修订 ID
        输出：冻结内容与摘要
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(m.revisions).where(
                        m.revisions.c.id == revision_id, m.revisions.c.report_id == report_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DataError("REPORT_NOT_FOUND")
        if content_hash(row["payload"]) != row["content_hash"]:
            raise DataError("REPORT_HASH_MISMATCH")
        return row["payload"], row["content_hash"]

    def read(self, report_id, revision_id):
        """
        作用：只从冻结报告生成公开正文，不查当前数据或知识
        输入：报告和修订 ID
        输出：报告详情
        """
        payload, digest = self.frozen(report_id, revision_id)
        result = {key: payload[key] for key in ReportDTO.model_fields if key in payload}
        result.update(
            content_hash=digest,
            sources=list(payload["sources"].values()),
            metrics=list(payload["metrics"].values()),
            external_snapshots=list(payload.get("external_snapshots", {}).values()),
        )
        return ReportDTO.model_validate(result)

    def listing(self, offset=0, limit=20, report_id=None):
        """
        作用：分页读取报告列表或某报告的历史修订
        输入：偏移、上限及可选报告
        输出：统一列表
        """
        statement = select(m.revisions)
        if report_id:
            statement = statement.where(m.revisions.c.report_id == report_id)
        else:
            statement = statement.join(
                m.reports, m.reports.c.latest_revision_id == m.revisions.c.id
            )
        statement = statement.order_by(m.revisions.c.created_at.desc(), m.revisions.c.id.desc())
        with self.engine.connect() as connection:
            if report_id:
                self.report_row(connection, report_id)
            rows = connection.execute(statement.offset(offset).limit(limit + 1)).mappings().all()
        items = []
        for row in rows[:limit]:
            p, digest = row["payload"], row["content_hash"]
            if content_hash(p) != digest:
                raise DataError("REPORT_HASH_MISMATCH")
            items.append(
                ReportSummary(
                    id=p["report_id"],
                    revision_id=p["revision_id"],
                    version=p["version"],
                    title=p["title"],
                    question=p["question"],
                    scope=p["scope"],
                    period=p["data_snapshot"]["period"],
                    date_range=p.get("date_range"),
                    source_kind=p["data_snapshot"]["manifest"].get("source_kind", "unknown"),
                    created_at=p["created_at"],
                )
            )
        return {"items": items, "next_cursor": str(offset + limit) if len(rows) > limit else None}

    def report_row(self, connection, report_id):
        """
        作用：读取报告指针，使用报告专属的不存在错误
        输入：连接与报告 ID
        输出：报告记录
        """
        row = (
            connection.execute(select(m.reports).where(m.reports.c.id == report_id))
            .mappings()
            .first()
        )
        if row is None:
            raise DataError("REPORT_NOT_FOUND")
        return dict(row)

    def revise(self, report_id, request):
        """
        作用：显式在已有冻结证据上创建修订，保留加载时版本保护
        输入：报告与带 expected_version 的草稿
        输出：新修订
        """
        with self.engine.connect() as connection:
            row = self.report_row(connection, report_id)
        old, _ = self.frozen(report_id, row["latest_revision_id"])
        prepared = materialize(request.report, old)
        with self.write() as connection:
            current = self.report_row(connection, report_id)
            if current["version"] != request.expected_version:
                raise DataError("REPORT_VERSION_CONFLICT")
            result = self.persist(connection, prepared, report_id, current["version"] + 1)
        return self.read(result["report_id"], result["revision_id"])

    def evidence(self, report_id, revision_id, block_id, row=0, column=None, value_key=None):
        """
        作用：按报告中已存在的块与单元格读取冻结证据，拒绝任意产物 ID
        输入：固定修订、块和受限选择
        输出：对应值及固定来源
        """
        p, _ = self.frozen(report_id, revision_id)
        block = next((b for b in p["blocks"] if b["id"] == block_id), None)
        if block is None:
            raise DataError("REPORT_BLOCK_NOT_FOUND")
        value, ident = None, block.get("artifact_id")
        if block["type"] == "kpi":
            value = block["value"]
        elif block["type"] == "text" and value_key:
            segment = next((s for s in block["segments"] if s.get("key") == value_key), None)
            if segment is None:
                raise DataError("REPORT_VALUE_NOT_FOUND")
            value = segment["value"]
        elif block["type"] in {"table", "chart"} and column is not None:
            index = next((i for i, c in enumerate(block["columns"]) if c["name"] == column), None)
            if index is None or not 0 <= row < len(block["rows"]):
                raise DataError("REPORT_VALUE_NOT_FOUND")
            value = block["rows"][row][index]
        if value:
            ident = value["value_ref"]["artifact_id"]
        artifact = p["artifacts"][ident]["artifact"] if ident else None
        used_artifacts = {ident} if ident else set()
        source_ids = set(block.get("evidence_ids", []))
        if block["type"] == "text" and value_key is None:
            used_artifacts.update(
                s["value"]["value_ref"]["artifact_id"] for s in block["segments"] if s.get("value")
            )
            used_artifacts.update(source_ids & set(p["artifacts"]))
        metric_ids = set()
        for artifact_id in used_artifacts:
            metadata = p["artifacts"][artifact_id]["artifact"]
            source_ids.update(metadata["knowledge_revision_ids"])
            source_ids.update(metadata.get("external_evidence_ids", []))
            metric_ids.update(metadata["metric_revision_ids"])
        for metric_id in metric_ids:
            source_ids.update(p["metrics"][metric_id]["definition"]["source_revision_ids"])
        # 只展示所选块的实际绑定来源，Wiki 可继续展开到固定原文。
        while True:
            expanded = set(source_ids)
            for source in p["sources"].values():
                if source["id"] in source_ids or source["revision_id"] in source_ids:
                    expanded.add(source["id"])
                    expanded.add(source["revision_id"])
                    expanded.update(source["locator"].get("citation_chunk_ids", []))
            if expanded == source_ids:
                break
            source_ids = expanded
        return EvidenceDTO(
            block_id=block_id,
            value=value,
            artifact=artifact,
            data_snapshot=p["data_snapshot"],
            date_range=p.get("date_range"),
            knowledge_snapshot_id=p["knowledge_snapshot_id"],
            sources=[source for key, source in p["sources"].items() if key in source_ids],
            metrics=[metric for key, metric in p["metrics"].items() if key in metric_ids],
            external_snapshots=[
                snapshot
                for key, snapshot in p.get("external_snapshots", {}).items()
                if any(
                    p["artifacts"][ident]["artifact"].get("external_snapshot_id") == key
                    for ident in used_artifacts
                )
            ],
        )
