"""显式工具注册；快照、日期、权限和证据归属均由服务端决定。"""

import asyncio
import json
import time
from dataclasses import replace
from datetime import date, timedelta

from pydantic import ValidationError
from sqlalchemy import select

from metric_pilot.agent import contracts as a
from metric_pilot.agent.context import compact_schema
from metric_pilot.agent.external_tools import NAMES as EXTERNAL_NAMES
from metric_pilot.agent.external_tools import ExternalTools
from metric_pilot.agent.findings import canonical_finding
from metric_pilot.agent.investigations import (
    InvestigationStore,
    authorized_runs,
    read_evidence_artifact,
)
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import ToolResult
from metric_pilot.data.models import ArtifactRecord
from metric_pilot.data.ranges import run_range, snapshot_range
from metric_pilot.external.advisories import AdvisorySearch
from metric_pilot.knowledge import models as km
from metric_pilot.knowledge.contracts import SearchRequest
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.service import ReportService

SPECS = {
    "query_historical_weather": (
        a.WeatherArgs,
        "查 ATL/ORD/JFK 当地日天气，支持 yesterday；返回固定版本和schema，数字须查询取值。",
    ),
    "search_official_advisories": (
        AdvisorySearch,
        "按设施和UTC签发日查FAA候选（最多三天）；候选须读正文，不是证据。",
    ),
    "read_external_source": (
        a.ExternalSourceArgs,
        "读本轮候选或同调查授权xe_固定来源；offset/limit翻页，返回可引用evidence_id。",
    ),
    "search_knowledge": (
        a.SearchArgs,
        "在固定知识快照中关键词检索原文与已发布 Wiki 导航；返回逐字摘录和来源",
    ),
    "read_wiki_page": (a.WikiArgs, "读取固定知识快照内已发布的 Wiki 及引用，不读取草稿"),
    "resolve_metric": (
        a.MetricArgs,
        "获取覆盖整个固定日期范围的同一已审核指标；空档、冲突或换版报错",
    ),
    "list_tables": (a.EmptyArgs, "列出当前固定数据快照允许查询的业务表"),
    "describe_table": (a.TableArgs, "查看允许表的字段、类型、语义、关联键和少量样本"),
    "query_sql": (
        a.SQLArgs,
        "受限只读SQL。exploratory须给范围、计算、缺失说明，不绑指标；"
        "reviewed_metric须已解析修订且不传exploration_scope。"
        "航班表由服务端限制到固定date_range，参数不能扩大范围。"
        "天气填external_snapshot_id，不跨库连接。",
    ),
    "read_artifact": (
        a.ArtifactArgs,
        "读取授权成功产物；小型聚合可用limit=100一次读完。"
        "避免反复小分页；大结果需要进一步SQL聚合。",
    ),
}


def invalid_arguments(error, summary, tool_name=None):
    """
    作用：只暴露可修复的字段位置和类型，不回传输入值或敏感异常正文
    输入：Pydantic校验错误、固定说明与工具名
    输出：统一工具失败结果
    """
    instructions = {
        "query_sql": "按fields修正。purpose=reviewed_metric必须绑定metric_revision_ids，"
        "且exploration_scope须省略或为null；purpose=exploratory必须提供exploration_scope，"
        "且metric_revision_ids必须为空；standard不能带exploration_scope。"
        "保持用户目标和正式指标绑定，不删除指标或改成探索来绕过审核。",
        "record_finding": "优先使用observations选择真实行，每次最多4行、每行最多6列；"
        "不用statement、values或row_bindings；note只写普通待复核说明。"
        "历史statement输入按constraints修正；values过多须减少叙述组数，"
        "完整比较放表格，不删除必要身份引用来绕过校验。",
    }
    return ToolResult(
        ok=False,
        summary=summary,
        error_code="INVALID_ARGUMENT",
        retryable=True,
        data={
            "fields": [
                {
                    "location": [str(part)[:80] for part in item["loc"][:6]],
                    "type": item["type"],
                    "constraints": {
                        key: value
                        for key, value in item.get("ctx", {}).items()
                        if key in {"max_length", "min_length", "actual_length", "ge", "le"}
                        and type(value) in {int, float}
                    },
                }
                for item in error.errors(include_input=False)[:12]
            ],
            "instruction": instructions.get(
                tool_name, "按fields位置与constraints修正参数，使用该工具公开签名中的字段。"
            ),
        },
    )


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
        self.date_range = run_range(query.catalog.engine, run)
        if ctx.date_range is not None and ctx.date_range != self.date_range:
            raise DataError("RUN_DATE_RANGE_MISMATCH")
        self.ctx = replace(ctx, date_range=self.date_range)
        self.first = date.fromisoformat(self.date_range.start)
        self.last = date.fromisoformat(self.date_range.end_exclusive) - timedelta(days=1)
        if self.date_range != snapshot_range(snapshot):
            self.schema = self.schema.model_copy(
                update={
                    "tables": [
                        table.model_copy(update={"sample": []}) for table in self.schema.tables
                    ]
                }
            )
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
        # 固定快照目录已加载，模型可直接使用列名，不重复调用结构发现工具。
        self.known_columns = {
            table.name: {column.name: column.type for column in table.columns}
            for table in self.schema.tables
        }
        self.query_summaries, self.progress = [], {}
        self.source_summaries = {}
        self.recorded_count = 0
        self.external = ExternalTools(run, self.ctx, query, settings, cancel)
        self.prior_artifacts = self.previous_artifacts() if run.get("investigation_id") else []
        # 旧成功产物已绑定且在当前固定范围仍适用的口径可直接复用，不重复模型检索。
        with self.store.engine.connect() as connection:
            for item in self.prior_artifacts:
                for ident in item.get("metric_revision_ids", []):
                    if ident in self.resolved:
                        continue
                    row = self.store.get(connection, km.metric_revisions, ident)
                    try:
                        actual = self.metric(row["metric_key"])
                    except DataError:
                        continue
                    if actual["id"] == ident:
                        self.resolved.add(ident)
                        self.definitions[ident] = actual["definition"]

    async def invoke_async(self, call):
        """
        作用：外部 HTTP 保持可取消异步任务，已有本地执行器继续在线程中调度。
        输入：实际模型调用。
        输出：统一工具结果。
        """
        if call.name not in EXTERNAL_NAMES:
            return await asyncio.to_thread(self.invoke, call)
        try:
            args = self.specs()[call.name][0].model_validate(call.arguments)
        except ValidationError as error:
            return invalid_arguments(error, "外部工具参数不符合契约，请按签名修正", call.name)
        return await self.external.invoke(call.name, args, call_id=call.id)

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
                a.RecordFindingInput,
                "保存核查发现：fact/candidate/counterevidence/limitation/recommendation；"
                "observations选择成功产物的artifact_id、绝对零基row和columns（含分组身份列）；"
                "最多4行，每行最多6列。服务端生成引用和同行绑定，不填statement/values/row_bindings。"
                "note只写待复核解释或限制，不手写数值；完整比较留在报告表格。"
                "默认保存一条关键发现后提交报告，新核查或修正才追加；supersedes 指向被修正的发现。",
            ),
        }

    def previous_artifacts(self):
        """
        作用：列出最近授权本地查询，避免续查重跑已有结果
        输入：当前固定调查和既有证据权限
        输出：最多六项经归属、成功和哈希核对的产物定位，不含数值
        """
        allowed = authorized_runs(self.store.engine, self.run) - {self.run["id"]}
        if not allowed:
            return []
        with self.store.engine.connect() as connection:
            ids = (
                connection.execute(
                    select(ArtifactRecord.id)
                    .where(
                        ArtifactRecord.run_id.in_(allowed),
                        ArtifactRecord.payload["external_snapshot_id"].as_string().is_(None),
                    )
                    .order_by(ArtifactRecord.payload["created_at"].as_string().desc())
                    .limit(6)
                )
                .scalars()
                .all()
            )
        result = []
        for ident in ids:
            try:
                metadata = read_evidence_artifact(self.query, self.run, ident, 0, 1)["artifact"]
            except DataError as exc:
                result.append({"artifact_id": ident, "unavailable": exc.code})
                continue
            result.append(
                {
                    "artifact_id": ident,
                    "run_id": metadata["run_id"],
                    "columns": [c["name"] for c in metadata["columns"]],
                    "rows": metadata["returned_rows"],
                    "truncated": metadata["truncated"],
                    "query_purpose": metadata["query_purpose"],
                    "metric_revision_ids": metadata["metric_revision_ids"],
                }
            )
        return result

    def metric(self, key):
        """
        作用：验证同一已审核指标修订覆盖整段调查范围
        输入：指标键
        输出：已审核固定修订
        """
        service = MetricService(self.store)
        return service.resolve_range(self.ctx.knowledge_snapshot_id, key, self.date_range)

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
            model = specs[call.name][0]
            # 历史工具输入保留严格校验；当前模型只看到行选择签名。
            if call.name == "record_finding" and "statement" in call.arguments:
                model = a.RecordFinding
            args = model.model_validate(call.arguments)
            selected_rows = isinstance(args, a.RecordFindingInput)
            if selected_rows:
                args = canonical_finding(args, self.query, self.run)
            data = self.execute(call.name, args, observation=selected_rows)
            if isinstance(data, ToolResult):
                return data
            if selected_rows:
                data["report_block"] = args.statement.model_dump(exclude_defaults=True)
            return ToolResult(ok=True, summary=specs[call.name][1], data=data)
        except ValidationError as error:
            return invalid_arguments(error, "参数不符合工具契约，请按签名修正", call.name)
        except DataError as exc:
            blocked = exc.code == "METRIC_REVIEW_OR_CLARIFICATION_REQUIRED"
            return ToolResult(
                ok=False,
                summary=(
                    "指标尚不可用，需要补充已审核且日期适用的口径"
                    if call.name == "resolve_metric" or blocked
                    else "发现文字与数值引用不匹配，请修正占位符。"
                    if exc.code == "REPORT_TEMPLATE_KEYS"
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
                else {
                    "error_details": exc.details,
                    "instruction": "text_template中的{{key}}必须与values的键一致；"
                    "unused_refs须在模板中显示或从values移除；绑定中的身份键必须显示，"
                    "不同组即使月份相同也分别显示自己的月份占位符。"
                    "数量用对应占位符和真实ValueRef，不直接写死数字，不换成无关引用。",
                }
                if exc.code == "REPORT_TEMPLATE_KEYS"
                else {"error_details": exc.details}
                if exc.code in {"REPORT_ROW_BINDING_KEYS", "REPORT_ROW_BINDING_MISMATCH"}
                else {},
                error_code=exc.code,
                retryable=exc.code
                in {
                    "SQL_EXECUTION_FAILED",
                    "QUERY_TIMEOUT",
                    "INVALID_PARAMETERS",
                    "REPORT_TEMPLATE_KEYS",
                    "REPORT_ROW_BINDING_KEYS",
                    "REPORT_ROW_BINDING_MISMATCH",
                    "REPORT_COLUMN_NOT_FOUND",
                    "REPORT_ROW_NOT_FOUND",
                },
            )

    def execute(self, name, args, *, observation=False):
        """
        作用：分发显式工具
        输入：名称、已验证参数及服务端行选择标记
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
                "external_sources": self.external.index(),
                "external_checks": self.external.failed_checks(),
                "available_artifacts": self.previous_artifacts(),
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
            prepared = ReportService(self.query).prepare(
                self.run,
                draft,
                require_query=False,
                observation_text=args.statement if observation and args.statement.values else None,
            )
            frozen = {
                "block": prepared["blocks"][0],
                "artifacts": prepared["artifacts"],
                "sources": prepared["sources"],
                "metrics": prepared["metrics"],
                "data_snapshot": prepared["data_snapshot"],
                "date_range": prepared["date_range"],
                "knowledge_snapshot_id": self.run["knowledge_snapshot_id"],
                "external_snapshots": prepared.get("external_snapshots", {}),
                "observation_texts": prepared.get("observation_texts", {}),
            }
            if self.cancel.is_set():
                raise DataError("RUN_CANCELLED")
            if time.monotonic() >= self.ctx.deadline_monotonic:
                raise DataError("RUN_TIMEOUT")
            finding = InvestigationStore(self.store.engine).record(
                self.run, args, prepared["blocks"][0], frozen
            )
            self.recorded_count += 1
            return {
                **finding,
                "next_step_hint": "已有本轮记录；若所需比较已在成功结果中，"
                "请直接提交表格与简短解释，额外核查列为下一步。",
            }
        if name == "list_tables":
            return {"tables": [t.name for t in self.schema.tables]}
        if name == "describe_table":
            if args.external_snapshot_id:
                snapshot, _ = self.external.service.query_target(
                    self.ctx, args.external_snapshot_id
                )
                for table in snapshot["schema"]["tables"]:
                    if table["name"] == args.table_name:
                        return {"table": table, "external_snapshot_id": snapshot["id"]}
                raise DataError("TABLE_NOT_ALLOWED")
            for table in self.schema.tables:
                if table.name == args.table_name:
                    if self.run.get("investigation_id"):
                        self.known_columns[table.name] = {
                            column.name: column.type for column in table.columns
                        }
                    return {"table": table.model_dump(), "date_range": self.date_range.model_dump()}
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
                    end_exclusive=date.fromisoformat(self.date_range.end_exclusive),
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
            page = read_evidence_artifact(
                self.query, self.run, args.artifact_id, args.offset, args.limit
            )
            # 绝对零基行号与当前页一一对应，避免模型按分组排序猜测行号。
            page["row_indices"] = list(range(args.offset, args.offset + len(page["rows"])))
            if self.run.get("investigation_id"):
                metadata = page["artifact"]
                summary = dict(
                    artifact_id=metadata["id"],
                    run_id=metadata["run_id"],
                    rows=metadata["returned_rows"],
                    truncated=metadata["truncated"],
                    query_purpose=metadata["query_purpose"],
                    external_snapshot_id=metadata.get("external_snapshot_id"),
                )
                cached = dict(
                    columns=[c["name"] for c in metadata["columns"]],
                    rows=page["rows"] if args.offset == 0 else [],
                    complete=False,
                )
                # 只合并从首行连续取得的真实页面；完整缓存不能被后续小分页覆盖。
                previous = next(
                    (
                        item.get("result_preview")
                        for item in self.query_summaries
                        if item["artifact_id"] == metadata["id"]
                    ),
                    None,
                )
                if previous and previous["columns"] == cached["columns"]:
                    prefix = previous["rows"]
                    if args.offset <= len(prefix):
                        cached["rows"] = (
                            prefix[: args.offset]
                            + page["rows"]
                            + prefix[args.offset + len(page["rows"]) :]
                        )
                    else:
                        cached["rows"] = prefix
                cached["complete"] = (
                    not metadata["truncated"] and len(cached["rows"]) == metadata["returned_rows"]
                )
                if len(json.dumps(cached, ensure_ascii=False).encode()) <= 4096:
                    summary["result_preview"] = cached
                elif previous:
                    summary["result_preview"] = previous
                self.query_summaries = [
                    s for s in self.query_summaries if s["artifact_id"] != metadata["id"]
                ]
                self.query_summaries.append(summary)
            return page
        if name == "query_sql":
            if args.external_snapshot_id and (
                args.purpose != "exploratory" or args.metric_revision_ids
            ):
                raise DataError("EXTERNAL_METRIC_BINDING_NOT_ALLOWED")
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
                external_snapshot_id=args.external_snapshot_id,
            )
            self.artifact_ids.update(result.artifact_ids)
            if result.ok:
                result.data["row_indices"] = list(range(len(result.data["preview"])))
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
                    if len(json.dumps(cached, ensure_ascii=False).encode()) <= 4096:
                        summary = self.query_summaries[-1]
                        summary["result_preview"] = cached
                        if args.exploration_scope is not None:
                            summary.pop("sql")
                            summary["population"] = args.exploration_scope.population
                result.data["validation_level"] = "references_validated"
                result.data["limitation"] = "执行与引用有效；自由 SQL 的业务语义未自动证明。"
            return result
        raise DataError("UNKNOWN_TOOL")
