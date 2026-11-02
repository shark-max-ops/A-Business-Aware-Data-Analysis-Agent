"""按本次运行的真实访问记录冻结知识来源，不从任意 URL 拉取内容。"""

import calendar
from datetime import date

from sqlalchemy import select

from metric_pilot.agent import models as am
from metric_pilot.agent.investigations import authorized_runs
from metric_pilot.data.common import DataError
from metric_pilot.knowledge import models as km
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.reports.contracts import FrozenSource


def freeze_sources(store, run, artifacts, period):
    """
    作用：收集实际访问与查询绑定的来源和已审核指标
    输入：仓储、本次运行、冻结产物和月份
    输出：固定来源字典、指标字典及知识快照副本
    """
    sources, definitions, requested = {}, {}, set()
    # 仅冻结产物实际绑定的指标；运行中选过或解析过不代表探索采用了该口径。
    metric_ids = set()
    allowed = authorized_runs(store.engine, run)
    for item in artifacts.values():
        requested.update(item["artifact"]["knowledge_revision_ids"])
        metric_ids.update(item["artifact"]["metric_revision_ids"])
    with store.engine.connect() as connection:
        snapshot = store.get(connection, km.snapshots, run["knowledge_snapshot_id"])["payload"]
        executions = (
            connection.execute(
                select(am.executions).where(
                    am.executions.c.run_id.in_(allowed), am.executions.c.status == "succeeded"
                )
            )
            .mappings()
            .all()
        )
        for execution in executions:
            data = execution["result"].get("data", {})
            if execution["name"] == "search_knowledge":
                for item in data.get("items", []):
                    requested.add(item["chunk_id"])
                    requested.add(item["revision_id"])
            elif execution["name"] == "read_wiki_page":
                requested.add(data["id"])
        # 指标必须与成功产物所绑定版本一致，并覆盖数据月份。
        year, month = map(int, period.split("-"))
        for ident in metric_ids:
            if ident not in snapshot["metric_revision_ids"]:
                raise DataError("REPORT_METRIC_NOT_ALLOWED")
            row = store.get(connection, km.metric_revisions, ident)
            for day in (1, calendar.monthrange(year, month)[1]):
                found = MetricService(store).resolve(
                    run["knowledge_snapshot_id"],
                    row["metric_key"],
                    date(year, month, day).isoformat(),
                )
                if found["id"] != ident:
                    raise DataError("REPORT_METRIC_NOT_APPLICABLE")
            definitions[ident] = row
            requested.update(row["definition"]["source_revision_ids"])
        # 递归冻结已发布 Wiki 引用的原文，使旧报告仍可查看固定原文。
        while requested - set(sources):
            ident = sorted(requested - set(sources))[0]
            row = (
                connection.execute(select(km.chunks).where(km.chunks.c.id == ident))
                .mappings()
                .first()
            )
            if row:
                parent = store.get(connection, km.revisions, row["revision_id"])
                if row["revision_id"] not in snapshot["document_revision_ids"]:
                    raise DataError("REPORT_SOURCE_NOT_ALLOWED")
                doc = store.get(connection, km.documents, parent["document_id"])
                item = FrozenSource(
                    id=ident,
                    revision_id=row["revision_id"],
                    title=doc["title"],
                    source_kind=row["source_kind"],
                    content=row["text"],
                    locator=row["locator"],
                )
                requested.add(row["revision_id"])
            elif ident in snapshot["document_revision_ids"]:
                row = store.get(connection, km.revisions, ident)
                doc = store.get(connection, km.documents, row["document_id"])
                item = FrozenSource(
                    id=ident,
                    revision_id=ident,
                    title=doc["title"],
                    source_kind=(
                        "mixed_document"
                        if row["metadata"].get("separate_project_annotations")
                        else row["metadata"]["source_kind"]
                    ),
                    content=row["content"],
                    locator={"content_hash": row["content_hash"], "metadata": row["metadata"]},
                )
            elif ident in snapshot["wiki_revision_ids"]:
                row = store.get(connection, km.wiki_revisions, ident)
                payload = row["payload"]
                item = FrozenSource(
                    id=ident,
                    revision_id=ident,
                    title=payload["title"],
                    source_kind="wiki_generated",
                    content=payload["content"],
                    locator={"citation_chunk_ids": [c["chunk_id"] for c in payload["citations"]]},
                )
                requested.update(c["chunk_id"] for c in payload["citations"])
            else:
                raise DataError("REPORT_SOURCE_NOT_ALLOWED")
            sources[ident] = item.model_dump()
    return sources, definitions, snapshot
