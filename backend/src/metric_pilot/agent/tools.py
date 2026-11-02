"""显式工具注册；快照、日期、权限和证据归属均由服务端决定。"""

import calendar
import json
import time
from datetime import date

from pydantic import ValidationError

from metric_pilot.agent import contracts as a
from metric_pilot.agent.context import compact_schema
from metric_pilot.agent.investigations import InvestigationStore, read_evidence_artifact
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import ToolResult
from metric_pilot.knowledge import models as km
from metric_pilot.knowledge.contracts import SearchRequest
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.service import ReportService

SPECS = {
    "search_knowledge": (
        a.SearchArgs,
        "在固定知识快照中关键词检索原文与已发布 Wiki 导航；返回逐字摘录和来源",
    ),
    "read_wiki_page": (a.WikiArgs, "读取固定知识快照内已发布的 Wiki 及引用，不读取草稿"),
    "resolve_metric": (a.MetricArgs, "获取适用于整个数据月份的已审核指标；未审核或冲突会报错"),
    "list_tables": (a.EmptyArgs, "列出当前固定数据快照允许查询的业务表"),
    "describe_table": (a.TableArgs, "查看允许表的字段、类型、语义、关联键和少量样本"),
    "query_sql": (
        a.SQLArgs,
        "执行受限只读 SQL。数据探索使用 purpose=exploratory，必须给出 exploration_scope 的"
        "population（范围和排除）、calculation（计算及分母）、missing_values（缺失处理）；"
        "不绑定指标且始终标为未审核探索。正式指标使用 purpose=reviewed_metric，修订须先 resolve。",
    ),
    "read_artifact": (a.ArtifactArgs, "读取本次或服务端授权的同调查已完成运行的成功冻结产物"),
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
        self.known_columns, self.query_summaries, self.progress = {}, [], {}
        self.source_summaries = {}
        self.recorded_count = 0

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
                    "parameters": compact_schema(model.model_json_schema()),
                },
            }
            for name, (model, description) in self.specs().items()
        ]

    def specs(self):
        """
        作用：只向真实调查开放发现记录与恢复工具
        输入：无
        输出：工具签名
        """
        if not self.run.get("investigation_id"):
            return SPECS
        return {
            **SPECS,
            "read_investigation": (
                a.ReadInvestigation,
                "查看固定调查和历史；多项历史在模型上下文中只保留概览，"
                "用 detail_offset 与 limit=1 读取单项完整文字和 ValueRef，读取后核对发现 ID",
            ),
            "record_finding": (
                a.RecordFinding,
                "保存核查发现：fact/candidate/counterevidence/limitation/recommendation；"
                "数字用 statement.values 的 ValueRef；supersedes 指向被修正的发现。",
            ),
        }

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
        specs = self.specs()
        if call.name not in specs:
            return ToolResult(ok=False, summary="未知工具", error_code="UNKNOWN_TOOL")
        try:
            args = specs[call.name][0].model_validate(call.arguments)
            data = self.execute(call.name, args)
            if isinstance(data, ToolResult):
                return data
            return ToolResult(ok=True, summary=specs[call.name][1], data=data)
        except ValidationError:
            return ToolResult(
                ok=False,
                summary="参数不符合工具契约，请按签名修正",
                error_code="INVALID_ARGUMENT",
                retryable=True,
            )
        except DataError as exc:
            blocked = exc.code == "METRIC_REVIEW_OR_CLARIFICATION_REQUIRED"
            return ToolResult(
                ok=False,
                summary=(
                    "指标尚不可用，需要补充已审核且日期适用的口径"
                    if call.name == "resolve_metric" or blocked
                    else "工具未成功：" + exc.code
                ),
                data={
                    "requires_clarification": False,
                    "blocked_metric_keys": sorted(self.blocked_metrics),
                    "instruction": "未声明用途的查询已拒绝。若做独立探索，需显式使用"
                    "purpose=exploratory 并填写 exploration_scope，结果不作为正式项目指标。"
                    "确需不可用的正式指标时返回 clarification。",
                }
                if blocked
                else {
                    "metric_key": args.metric_key,
                    "metric_available": False,
                    "exploration_available": True,
                    "instruction": "该正式指标不可用。若问题可由原始数据支持，可用明确范围的"
                    "exploratory 查询继续，结果不得冒充该指标；确需正式指标时再澄清。",
                }
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
        if name == "read_investigation":
            investigation = InvestigationStore(self.store.engine).read(self.run["investigation_id"])
            ordered = list(reversed(investigation["findings"]))
            return {
                **investigation,
                "findings": ordered[args.offset : args.offset + args.limit],
                "finding_count": len(ordered),
                "offset": args.offset,
                "next_offset": args.offset + args.limit
                if args.offset + args.limit < len(ordered)
                else None,
            }
        if name == "record_finding":
            if args.kind in {"fact", "counterevidence"} and not (
                args.statement.values or args.statement.evidence_ids
            ):
                raise DataError("FINDING_EVIDENCE_REQUIRED")
            # 用报告同一取值与来源校验路径生成发现；只允许服务端物化数字。
            draft = ReportDraft(
                schema_version="1",
                title="调查核查记录",
                scope="固定调查范围",
                metric_revision_ids=[],
                blocks=[args.statement],
                limitations=[],
            )
            # 先冻结实际引用，使用产物绑定的指标集合，不能由模型改写审核状态。
            from metric_pilot.reports.materialize import artifact_ids

            metrics = set()
            for ident in artifact_ids(draft, require_query=False):
                metadata = read_evidence_artifact(self.query, self.run, ident, 0, 1)["artifact"]
                metrics.update(metadata["metric_revision_ids"])
            draft.metric_revision_ids = sorted(metrics)
            prepared = ReportService(self.query).prepare(self.run, draft, require_query=False)
            frozen = {
                "block": prepared["blocks"][0],
                "artifacts": prepared["artifacts"],
                "sources": prepared["sources"],
                "metrics": prepared["metrics"],
                "data_snapshot": prepared["data_snapshot"],
                "knowledge_snapshot_id": self.run["knowledge_snapshot_id"],
            }
            if self.cancel.is_set():
                raise DataError("RUN_CANCELLED")
            if time.monotonic() >= self.ctx.deadline_monotonic:
                raise DataError("RUN_TIMEOUT")
            finding = InvestigationStore(self.store.engine).record(
                self.run, args, prepared["blocks"][0], frozen
            )
            self.recorded_count += 1
            return finding
        if name == "list_tables":
            return {"tables": [t.name for t in self.schema.tables]}
        if name == "describe_table":
            for table in self.schema.tables:
                if table.name == args.table_name:
                    if self.run.get("investigation_id"):
                        self.known_columns[table.name] = {
                            column.name: column.type for column in table.columns
                        }
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
            # 保留真实来源短摘录，避免旧工具组裁剪后反复查同一定义。
            for item in items:
                self.source_summaries[item["chunk_id"]] = {
                    "chunk_id": item["chunk_id"],
                    "source_kind": item["source_kind"],
                    "excerpt": item["excerpt"][:140],
                }
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
            return read_evidence_artifact(
                self.query, self.run, args.artifact_id, args.offset, args.limit
            )
        if name == "query_sql":
            exploratory = args.purpose == "exploratory"
            # 只有显式说明范围的探索可独立继续；删掉指标引用不构成用途切换。
            if self.blocked_metrics and args.purpose == "standard":
                raise DataError("METRIC_REVIEW_OR_CLARIFICATION_REQUIRED")
            if not exploratory and not set(self.run["metric_revision_ids"]) <= set(
                args.metric_revision_ids
            ):
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
                query_purpose="exploratory"
                if exploratory
                else "reviewed_metric"
                if args.metric_revision_ids
                else "legacy",
                exploration_scope=args.exploration_scope,
                unavailable_metric_keys=sorted(self.blocked_metrics) if exploratory else (),
            )
            self.artifact_ids.update(result.artifact_ids)
            if result.ok:
                if self.run.get("investigation_id"):
                    metadata = result.data["artifact"]
                    self.query_summaries.append(
                        {
                            "artifact_id": metadata["id"],
                            "sql": args.sql[:240],
                            "column_count": len(metadata["columns"]),
                            "rows": metadata["returned_rows"],
                            "truncated": metadata["truncated"],
                            "query_purpose": metadata["query_purpose"],
                        }
                    )
                    cached = {
                        "columns": [column["name"] for column in metadata["columns"]],
                        "rows": result.data["preview"],
                        "complete": not metadata["truncated"]
                        and len(result.data["preview"]) == metadata["returned_rows"],
                    }
                    # 只缓存小型真实结果；完整性标记防止把部分预览当完整统计。
                    if len(json.dumps(cached, ensure_ascii=False).encode()) <= 1200:
                        summary = self.query_summaries[-1]
                        summary["result_preview"] = cached
                        if args.exploration_scope is not None:
                            summary.pop("sql")
                            summary["population"] = args.exploration_scope.population
                result.data["validation_level"] = "references_validated"
                result.data["limitation"] = "执行与引用有效；自由 SQL 的业务语义未自动证明。"
            return result
        raise DataError("UNKNOWN_TOOL")
