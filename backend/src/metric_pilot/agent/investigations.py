"""调查事务、累计额度与服务端证据授权；不执行模型生成的计划代码。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from metric_pilot.agent import models as m
from metric_pilot.data.common import DataError, digest, new_id, utc_now
from metric_pilot.data.models import ArtifactRecord
from metric_pilot.data.ranges import resolve_range, run_range
from metric_pilot.knowledge.store import KnowledgeStore


class InvestigationStore(KnowledgeStore):
    """
    作用：保存调查边界、运行额度与不可变发现
    输入：元数据库引擎
    输出：调查仓储
    """

    def require(self, connection, ident):
        """
        作用：查找真实调查
        输入：连接与调查标识
        输出：数据库记录
        """
        row = (
            connection.execute(select(m.investigations).where(m.investigations.c.id == ident))
            .mappings()
            .first()
        )
        if row is None:
            raise DataError("INVESTIGATION_NOT_FOUND")
        return dict(row)

    def create(self, body, knowledge_id, budget):
        """
        作用：显式固定目标、范围、版本与不可自动重置的总额度
        输入：已验证请求、知识快照与服务端运行预算
        输出：调查
        """
        ident = new_id("inv")
        selected = resolve_range(
            self.engine, body.snapshot_id, body.date_range, require_explicit=True
        )
        payload = {
            "goal": body.goal.strip(),
            "scope": body.scope.strip(),
            "date_range": selected.model_dump(),
            "snapshot_id": body.snapshot_id,
            "knowledge_snapshot_id": knowledge_id,
            "parent_id": body.parent_id,
            "created_at": utc_now(),
            "limits": {
                "runs": 4,
                "max_turns": budget["max_turns"] * 4,
                "max_tool_calls": budget["max_tool_calls"] * 4,
                "timeout_seconds": budget["timeout_seconds"] * 4,
            },
            "allocated": {"runs": 0, "max_turns": 0, "max_tool_calls": 0, "timeout_seconds": 0},
        }
        if not payload["goal"] or not payload["scope"]:
            raise DataError("INVESTIGATION_SCOPE_REQUIRED")
        with self.write() as c:
            if not c.execute(
                select(m.conversations.c.id).where(m.conversations.c.id == body.conversation_id)
            ).first():
                raise DataError("CONVERSATION_NOT_FOUND")
            if body.parent_id:
                parent = self.require(c, body.parent_id)
                if parent["conversation_id"] != body.conversation_id:
                    raise DataError("INVESTIGATION_PARENT_MISMATCH")
            c.execute(
                m.investigations.insert().values(
                    id=ident, conversation_id=body.conversation_id, payload=payload, version=1
                )
            )
        return self.read(ident)

    def attach(self, c, body, knowledge_id, budget, run_id):
        """
        作用：在创建运行的同一写事务中校验版本并分配累计额度
        输入：事务、请求、固定知识、运行预算与服务端运行标识
        输出：无
        """
        row = self.require(c, body.investigation_id)
        p = row["payload"]
        if (
            row["conversation_id"] != body.conversation_id
            or p["snapshot_id"] != body.snapshot_id
            or p["knowledge_snapshot_id"] != knowledge_id
            or run_range(self.engine, p) != body.date_range
        ):
            raise DataError("INVESTIGATION_BOUNDARY_MISMATCH")
        if self.active(c, row["id"]):
            raise DataError("INVESTIGATION_BUSY")
        if row["version"] != body.expected_investigation_version:
            raise DataError("INVESTIGATION_VERSION_CONFLICT")
        # 采用不可退还的运行额度预分配，取消、重启和失败也不能重置总预算。
        allocation = {
            "runs": 1,
            **{key: budget[key] for key in ("max_turns", "max_tool_calls", "timeout_seconds")},
        }
        used = {key: p["allocated"][key] + allocation[key] for key in allocation}
        if any(used[key] > p["limits"][key] for key in used):
            raise DataError("INVESTIGATION_BUDGET_EXCEEDED")
        c.execute(
            m.investigations.update()
            .where(m.investigations.c.id == row["id"])
            .values(payload={**p, "allocated": used}, version=row["version"] + 1)
        )
        c.execute(m.investigation_runs.insert().values(run_id=run_id, investigation_id=row["id"]))

    def active(self, c, ident):
        """
        作用：识别同一调查正在排队或执行的运行
        输入：连接与调查标识
        输出：运行标识或空值
        """
        return c.execute(
            select(m.runs.c.id)
            .join(m.investigation_runs, m.runs.c.id == m.investigation_runs.c.run_id)
            .where(
                m.investigation_runs.c.investigation_id == ident,
                m.runs.c.status.in_(["queued", "running"]),
            )
        ).scalar()

    def read(self, ident):
        """
        作用：恢复调查状态、发现历史与累计额度
        输入：调查标识
        输出：公开调查
        """
        with self.engine.connect() as c:
            row = self.require(c, ident)
            runs = list(
                c.execute(
                    select(m.investigation_runs.c.run_id).where(
                        m.investigation_runs.c.investigation_id == ident
                    )
                ).scalars()
            )
            rows = (
                c.execute(select(m.findings).where(m.findings.c.investigation_id == ident))
                .mappings()
                .all()
            )
            statuses = dict(
                c.execute(select(m.runs.c.id, m.runs.c.status).where(m.runs.c.id.in_(runs))).all()
            )
            entries = [
                {**self.public_finding(r), "source_run_status": statuses[r["run_id"]]} for r in rows
            ]
            active = self.active(c, ident)
        p = row["payload"]
        exhausted = any(p["allocated"][key] >= value for key, value in p["limits"].items())
        return {
            "id": ident,
            "conversation_id": row["conversation_id"],
            **p,
            "version": row["version"],
            "status": "running" if active else "budget_exhausted" if exhausted else "ready",
            "active_run_id": active,
            "run_ids": runs,
            "findings": sorted(entries, key=lambda e: (e["created_at"], e["id"])),
        }

    def public_finding(self, row):
        """
        作用：校验发现哈希并只返回已物化的短文
        输入：固定发现记录
        输出：公开发现
        """
        if digest(row["payload"]) != row["content_hash"]:
            raise DataError("FINDING_HASH_MISMATCH")
        p = row["payload"]
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "source_run_status": "running",
            "artifact_ids": sorted(p["frozen"]["artifacts"]),
            "evidence_ids": p["block"]["evidence_ids"],
            "row_bindings": p["block"].get("row_bindings", []),
            "value_refs": {
                s["key"]: s["value"]["value_ref"] for s in p["block"]["segments"] if s.get("value")
            },
            **{key: p[key] for key in ("kind", "check", "questions", "supersedes", "created_at")},
            "text": "".join(s["text"] for s in p["block"]["segments"]),
        }

    def record(self, run, args, block, frozen):
        """
        作用：验证当前运行归属并追加发现修订，保留被否定线索
        输入：运行、已验证参数、物化块与冻结证据
        输出：公开发现
        """
        payload = {
            **args.model_dump(exclude={"statement"}),
            "block": block,
            "frozen": frozen,
            "created_at": utc_now(),
        }
        row = {
            "id": new_id("finding"),
            "investigation_id": run["investigation_id"],
            "run_id": run["id"],
            "payload": payload,
            "content_hash": digest(payload),
        }
        with self.write() as c:
            investigation = self.require(c, row["investigation_id"])
            current = c.execute(select(m.runs).where(m.runs.c.id == run["id"])).mappings().one()
            if current["status"] != "running" or current["cancel_requested"]:
                raise DataError("RUN_CANCELLED")
            link = c.execute(
                select(m.investigation_runs).where(
                    m.investigation_runs.c.run_id == run["id"],
                    m.investigation_runs.c.investigation_id == row["investigation_id"],
                )
            ).first()
            if not link:
                raise DataError("INVESTIGATION_BOUNDARY_MISMATCH")
            if args.supersedes:
                old = (
                    c.execute(
                        select(m.findings).where(
                            m.findings.c.id == args.supersedes,
                            m.findings.c.investigation_id == row["investigation_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if old is None:
                    raise DataError("FINDING_NOT_FOUND")
                # 每项发现只能有一个直接后继；再次修正应引用最新后继。
                prior = c.execute(
                    select(m.findings.c.id).where(
                        m.findings.c.investigation_id == row["investigation_id"],
                        m.findings.c.payload["supersedes"].as_string() == args.supersedes,
                    )
                ).first()
                if prior:
                    raise DataError("FINDING_VERSION_CONFLICT")
            if (
                len(
                    list(
                        c.execute(
                            select(m.findings.c.id).where(
                                m.findings.c.investigation_id == row["investigation_id"]
                            )
                        ).scalars()
                    )
                )
                >= 80
            ):
                raise DataError("INVESTIGATION_FINDING_LIMIT")
            c.execute(m.findings.insert().values(**row))
            c.execute(
                m.investigations.update()
                .where(m.investigations.c.id == row["investigation_id"])
                .values(version=investigation["version"] + 1)
            )
        return self.public_finding(row)

    def evidence(self, ident, finding_id):
        """
        作用：从固定发现读取证据，不重新执行 SQL 或读取最新知识
        输入：调查与发现标识
        输出：已冻结证据
        """
        with self.engine.connect() as c:
            self.require(c, ident)
            row = (
                c.execute(
                    select(m.findings).where(
                        m.findings.c.id == finding_id, m.findings.c.investigation_id == ident
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise DataError("FINDING_NOT_FOUND")
            self.public_finding(row)
            return row["payload"]["frozen"]


def authorized_runs(engine, run):
    """
    作用：仅授权同一调查、相同版本且已完成的旧运行
    输入：数据库与当前可信运行
    输出：可读运行标识集合
    """
    allowed = {run["id"]}
    ident = run.get("investigation_id")
    if not ident:
        return allowed
    with engine.connect() as c:
        store = InvestigationStore(engine)
        inv = store.require(c, ident)
        p = inv["payload"]
        if (
            inv["conversation_id"] != run["conversation_id"]
            or p["snapshot_id"] != run["snapshot_id"]
            or p["knowledge_snapshot_id"] != run["knowledge_snapshot_id"]
            or run_range(engine, p) != run_range(engine, run)
        ):
            raise DataError("INVESTIGATION_BOUNDARY_MISMATCH")
        linked = list(
            c.execute(
                select(m.investigation_runs.c.run_id).where(
                    m.investigation_runs.c.investigation_id == ident
                )
            ).scalars()
        )
        if run["id"] not in linked:
            raise DataError("INVESTIGATION_BOUNDARY_MISMATCH")
        for row in c.execute(
            select(m.runs).where(m.runs.c.id.in_(linked), m.runs.c.status == "completed")
        ).mappings():
            old = row["payload"]
            if (
                old["snapshot_id"] == run["snapshot_id"]
                and old["knowledge_snapshot_id"] == run["knowledge_snapshot_id"]
                and row["conversation_id"] == run["conversation_id"]
                and run_range(engine, old) == run_range(engine, run)
            ):
                allowed.add(row["id"])
    return allowed


def read_evidence_artifact(query, run, ident, offset=0, limit=20):
    """
    作用：先验证调查权限，再复用成功状态、元数据和文件哈希校验
    输入：安全查询服务、可信运行、产物及分页
    输出：冻结产物页
    """
    with Session(query.catalog.engine) as session:
        record = session.get(ArtifactRecord, ident)
        owner = record.run_id if record else None
    if owner not in authorized_runs(query.catalog.engine, run):
        raise DataError("ARTIFACT_NOT_FOUND")
    page = query.read_artifact(owner, ident, offset, limit)
    if page["artifact"]["snapshot_id"] != run["snapshot_id"]:
        raise DataError("REPORT_SNAPSHOT_MISMATCH")
    selected = run_range(query.catalog.engine, run)
    artifact_range = resolve_range(
        query.catalog.engine, run["snapshot_id"], page["artifact"].get("date_range")
    )
    if artifact_range != selected:
        raise DataError("ARTIFACT_DATE_RANGE_MISMATCH")
    if page["artifact"].get("external_snapshot_id"):
        from metric_pilot.external.evidence import ExternalEvidenceService

        external = ExternalEvidenceService(query.catalog.engine, query.catalog.root)
        actual = external.run(run["id"])
        if (actual["snapshot_id"], actual["knowledge_snapshot_id"]) != (
            run["snapshot_id"],
            run["knowledge_snapshot_id"],
        ):
            raise DataError("EXTERNAL_CONTEXT_MISMATCH")
        external.validate_artifact(actual, page["artifact"])
    return page
