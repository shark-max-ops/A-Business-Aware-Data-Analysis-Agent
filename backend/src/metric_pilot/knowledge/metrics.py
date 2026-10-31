"""指标修订、审核、适用期与冲突解析。"""

from sqlalchemy import select

from metric_pilot.data.common import DataError, new_id, utc_now
from metric_pilot.knowledge import models as m


class MetricService:
    """
    作用：保存待审核定义并在固定快照中解析
    输入：知识仓储
    输出：指标服务
    """

    def __init__(self, store):
        """
        作用：固定仓储依赖
        输入：store - 仓储
        输出：无
        """
        self.store = store

    def save(self, request, approved=False, parent_revision_id=None):
        """
        作用：追加指标修订并约束正式审核入口
        输入：定义、版本、内部审核标记和父修订
        输出：新修订
        """
        definition = request.definition.model_dump(mode="json")
        if not approved and definition["review_state"] != "draft":
            raise DataError("METRIC_APPROVAL_REQUIRED")
        if definition["template_key"] is not None:
            raise DataError("METRIC_TEMPLATE_NOT_IMPLEMENTED")
        with self.store.write() as connection:
            current = (
                connection.execute(select(m.metrics).where(m.metrics.c.key == definition["key"]))
                .mappings()
                .first()
            )
            version = current["version"] if current else 0
            if request.expected_version != version or definition["version"] != version + 1:
                raise DataError("VERSION_CONFLICT", {"current_version": version})
            for revision_id in definition["source_revision_ids"]:
                source = self.store.get(connection, m.revisions, revision_id)
                metadata = source["metadata"]
                if (
                    metadata.get("effective_from")
                    and definition["effective_from"] < metadata["effective_from"]
                ):
                    raise DataError("METRIC_SOURCE_DATE_MISMATCH")
                if metadata.get("effective_to") and (
                    not definition["effective_to"]
                    or definition["effective_to"] > metadata["effective_to"]
                ):
                    raise DataError("METRIC_SOURCE_DATE_MISMATCH")
                if (
                    definition["authority"] == "official_mapping"
                    and metadata["source_kind"] != "official_reference"
                ):
                    raise DataError("METRIC_AUTHORITY_MISMATCH")
            # 首版只审核项目定义；官方指标映射需要另有经过验证的完整公式来源。
            if definition["authority"] == "official_mapping":
                raise DataError("OFFICIAL_FORMULA_REVIEW_UNSUPPORTED")
            ident = new_id("metricrev")
            values = {"version": version + 1, "current_revision_id": ident}
            if approved:
                values["approved_revision_id"] = ident
            if current:
                connection.execute(
                    m.metrics.update().where(m.metrics.c.key == definition["key"]).values(**values)
                )
            else:
                connection.execute(m.metrics.insert().values(key=definition["key"], **values))
            connection.execute(
                m.metric_revisions.insert().values(
                    id=ident,
                    metric_key=definition["key"],
                    version=version + 1,
                    definition=definition,
                    created_at=utc_now(),
                )
            )
            return {"id": ident, "definition": definition, "parent_revision_id": parent_revision_id}

    def approve(self, key, request):
        """
        作用：从当前草稿创建新的已审核修订，不改旧草稿
        输入：指标键与版本操作
        输出：已审核修订
        """
        from metric_pilot.knowledge.contracts import MetricWrite

        with self.store.engine.connect() as connection:
            metric = self.store.get(connection, m.metrics, key, "key")
            revision = self.store.get(connection, m.metric_revisions, request.revision_id)
            if (
                revision["metric_key"] != key
                or metric["current_revision_id"] != request.revision_id
                or revision["definition"]["review_state"] != "draft"
            ):
                raise DataError("METRIC_CURRENT_DRAFT_REQUIRED")
        definition = {
            **revision["definition"],
            "version": request.expected_version + 1,
            "review_state": "approved",
        }
        return self.save(
            MetricWrite(definition=definition, expected_version=request.expected_version),
            True,
            request.revision_id,
        )

    def resolve(self, snapshot_id, key, as_of):
        """
        作用：仅返回固定知识快照内已审核且适用的指标，冲突不拼接
        输入：快照、指标键与数据日期
        输出：唯一匹配或澄清错误
        """
        with self.store.engine.connect() as connection:
            snapshot = self.store.get(connection, m.snapshots, snapshot_id)["payload"]
            matches = []
            for ident in snapshot["metric_revision_ids"]:
                row = self.store.get(connection, m.metric_revisions, ident)
                d = row["definition"]
                if (
                    d["key"] == key
                    and d["review_state"] == "approved"
                    and d["effective_from"] <= as_of
                    and (not d["effective_to"] or as_of < d["effective_to"])
                ):
                    matches.append(row)
            if len(matches) != 1:
                raise DataError("METRIC_CONFLICT" if matches else "METRIC_NOT_APPLICABLE")
            return matches[0]
