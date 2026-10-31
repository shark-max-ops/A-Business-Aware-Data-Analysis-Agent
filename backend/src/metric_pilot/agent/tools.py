"""显式工具注册；快照、日期、权限和证据归属均由服务端决定。"""

import calendar
from datetime import date

from pydantic import ValidationError

from metric_pilot.agent import contracts as a
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import ToolResult
from metric_pilot.knowledge import models as km
from metric_pilot.knowledge.contracts import SearchRequest
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore

SPECS = {
    "search_knowledge": (
        a.SearchArgs,
        "在固定知识快照中关键词检索原文与已发布 Wiki 导航；返回逐字摘录和来源",
    ),
    "read_wiki_page": (a.WikiArgs, "读取固定知识快照内已发布的 Wiki 及引用，不读取草稿"),
    "resolve_metric": (a.MetricArgs, "获取适用于整个数据月份的已审核指标；未审核或冲突会报错"),
    "list_tables": (a.EmptyArgs, "列出当前固定数据快照允许查询的业务表"),
    "describe_table": (a.TableArgs, "查看允许表的字段、类型、语义、关联键和少量样本"),
    "query_sql": (a.SQLArgs, "执行受限只读 SQL；指标修订须先 resolve；自由 SQL 不代表口径已验证"),
    "read_artifact": (a.ArtifactArgs, "分页读取本次运行成功查询的冻结产物"),
}


class ToolRegistry:
    """
    作用：组合已有知识和查询服务
    输入：固定运行及服务
    输出：受限工具集
    """

    def __init__(self, run, ctx, query, settings, cancel):
        """
        作用：绑定执行范围
        输入：运行、上下文、查询服务和取消
        输出：无
        """
        self.run, self.ctx, self.query, self.cancel = run, ctx, query, cancel
        self.store = KnowledgeStore(query.catalog.engine)
        self.retriever = Retriever(self.store, KnowledgeProvider(settings))
        snapshot = query.catalog.snapshot(ctx.snapshot_id)
        self.schema = query.catalog.schema(snapshot.source_id, snapshot.id)
        year, month = map(int, snapshot.period.split("-"))
        self.first = date(year, month, 1)
        self.last = date(year, month, calendar.monthrange(year, month)[1])
        self.resolved = set(run["metric_revision_ids"])
        self.definitions = {}
        with self.store.engine.connect() as c:
            for ident in self.resolved:
                row = self.store.get(c, km.metric_revisions, ident)
                if self.metric(row["metric_key"])["id"] != ident:
                    raise DataError("METRIC_NOT_APPLICABLE")
                self.definitions[ident] = row["definition"]
        self.blocked_metrics = set()
        self.artifact_ids = set()
        self.knowledge_ids = set()

    def schemas(self):
        """
        作用：公开严格工具签名
        输入：无
        输出：模型原生工具列表
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in SPECS.items()
        ]

    def metric(self, key):
        """
        作用：验证指标覆盖整个月份
        输入：指标键
        输出：已审核固定修订
        """
        service = MetricService(self.store)
        start = service.resolve(self.ctx.knowledge_snapshot_id, key, self.first.isoformat())
        end = service.resolve(self.ctx.knowledge_snapshot_id, key, self.last.isoformat())
        if start["id"] != end["id"]:
            raise DataError("METRIC_PERIOD_CONFLICT")
        return start

    def invoke(self, call):
        """
        作用：校验参数并把预期错误反馈模型
        输入：调用
        输出：统一工具结果
        """
        if call.name not in SPECS:
            return ToolResult(ok=False, summary="未知工具", error_code="UNKNOWN_TOOL")
        try:
            args = SPECS[call.name][0].model_validate(call.arguments)
            data = self.execute(call.name, args)
            if isinstance(data, ToolResult):
                return data
            return ToolResult(ok=True, summary=SPECS[call.name][1], data=data)
        except ValidationError:
            return ToolResult(
                ok=False,
                summary="参数不符合工具契约，请按签名修正",
                error_code="INVALID_ARGUMENT",
                retryable=True,
            )
        except DataError as exc:
            return ToolResult(
                ok=False,
                summary=(
                    "指标尚不可用，需要补充已审核且日期适用的口径"
                    if call.name == "resolve_metric"
                    else "工具未成功：" + exc.code
                ),
                data={"requires_clarification": True, "metric_key": args.metric_key}
                if call.name == "resolve_metric"
                else {},
                error_code=exc.code,
                retryable=exc.code
                in {"SQL_EXECUTION_FAILED", "QUERY_TIMEOUT", "INVALID_PARAMETERS"},
            )

    def execute(self, name, args):
        """
        作用：分发显式工具
        输入：名称及已验证参数
        输出：实际服务结果
        """
        if name == "list_tables":
            return {"tables": [t.name for t in self.schema.tables]}
        if name == "describe_table":
            for table in self.schema.tables:
                if table.name == args.table_name:
                    return {"table": table.model_dump()}
            raise DataError("TABLE_NOT_ALLOWED")
        if name == "resolve_metric":
            try:
                row = self.metric(args.metric_key)
            except DataError:
                self.blocked_metrics.add(args.metric_key)
                raise
            self.blocked_metrics.discard(args.metric_key)
            self.resolved.add(row["id"])
            self.definitions[row["id"]] = row["definition"]
            return row
        if name == "search_knowledge":
            found = self.retriever.search(
                SearchRequest(
                    snapshot_id=self.ctx.knowledge_snapshot_id,
                    query=args.query,
                    mode="text",
                    as_of=self.first,
                )
            )
            items = []
            for hit in found["items"][: args.top_k]:
                piece = hit["chunk"]
                with self.store.engine.connect() as c:
                    metadata = self.store.get(c, km.revisions, piece["revision_id"])["metadata"]
                self.knowledge_ids.add(piece["revision_id"])
                items.append(
                    {
                        "chunk_id": piece["id"],
                        "revision_id": piece["revision_id"],
                        "excerpt": hit["excerpt"],
                        "locator": piece["locator"],
                        "source_kind": piece["source_kind"],
                        "effective_from": metadata.get("effective_from"),
                        "effective_to": metadata.get("effective_to"),
                        "text_hash": piece["text_hash"],
                    }
                )
            return {
                "items": items,
                "wiki_items": [
                    {
                        "page_id": row["page_id"],
                        "revision_id": row["id"],
                        "title": row["payload"]["title"],
                    }
                    for row in found.get("wiki_items", [])
                ],
                "mode": "text",
                "warnings": found["warnings"],
            }
        if name == "read_wiki_page":
            with self.store.engine.connect() as c:
                snapshot = self.store.get(c, km.snapshots, self.ctx.knowledge_snapshot_id)[
                    "payload"
                ]
                for ident in snapshot["wiki_revision_ids"]:
                    row = self.store.get(c, km.wiki_revisions, ident)
                    if row["page_id"] == args.page_id:
                        self.knowledge_ids.add(ident)
                        return row
            raise DataError("WIKI_NOT_IN_SNAPSHOT")
        if name == "read_artifact":
            return self.query.read_artifact(
                self.ctx.run_id, args.artifact_id, args.offset, args.limit
            )
        if name == "query_sql":
            if self.blocked_metrics:
                raise DataError("METRIC_REVIEW_OR_CLARIFICATION_REQUIRED")
            if not set(self.run["metric_revision_ids"]) <= set(args.metric_revision_ids):
                raise DataError("SELECTED_METRIC_BINDING_REQUIRED")
            if not set(args.metric_revision_ids) <= self.resolved:
                raise DataError("METRIC_RESOLVE_REQUIRED")
            sources = set(self.knowledge_ids)
            with self.store.engine.connect() as c:
                for ident in args.metric_revision_ids:
                    row = self.store.get(c, km.metric_revisions, ident)
                    if self.metric(row["metric_key"])["id"] != ident:
                        raise DataError("METRIC_NOT_APPLICABLE")
                    sources.update(row["definition"]["source_revision_ids"])
            result = self.query.execute(
                self.ctx,
                args.sql,
                args.params,
                self.cancel,
                metric_revision_ids=args.metric_revision_ids,
                knowledge_revision_ids=sorted(sources),
            )
            self.artifact_ids.update(result.artifact_ids)
            if result.ok:
                result.data["validation_level"] = "references_validated"
                result.data["limitation"] = "执行与引用有效；自由 SQL 的业务语义未自动证明。"
            return result
        raise DataError("UNKNOWN_TOOL")
