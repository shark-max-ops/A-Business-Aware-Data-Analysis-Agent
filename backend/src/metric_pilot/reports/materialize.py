"""报告检查与取值；不执行模型代码，不重新计算业务指标。"""

import copy
import hashlib

from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.reports.claims import POLICY, label, render_relation
from metric_pilot.reports.contracts import RenderedBlock, ReportDraft
from metric_pilot.reports.scope import validate_display_scope
from metric_pilot.reports.values import cell, numeric, text_segments


def artifact_ids(draft, *, require_query=True):
    """
    作用：提取报告实际使用的查询产物
    输入：已通过结构校验的草稿
    输出：唯一产物 ID 集合
    """
    result = set()
    for block in draft.blocks:
        if block.type == "kpi":
            result.add(block.value_ref.artifact_id)
        elif block.type in {"table", "chart", "ranking", "comparison"}:
            result.add(block.artifact_id)
        else:
            result.update(ref.artifact_id for ref in block.values.values())
            result.update(ident for ident in block.evidence_ids if ident.startswith("qa_"))
    if (require_query and not result) or len(result) > 8:
        raise DataError("REPORT_QUERY_REQUIRED")
    return result


def render_blocks(draft, artifacts, sources, main_range=None, policy=None, observation_texts=None):
    """
    作用：校验布局和引用并物化所有显示值
    输入：草稿、本次冻结结果与固定来源
    输出：可显示报告块与补充限制
    """
    ids, blocks, warnings = set(), [], []
    for model in draft.blocks:
        b = model.model_dump()
        if b["id"] in ids:
            raise DataError("REPORT_DUPLICATE_BLOCK")
        ids.add(b["id"])
        if b["type"] in {"ranking", "comparison"}:
            blocks.append(RenderedBlock.model_validate(render_relation(b, artifacts)).model_dump())
            continue
        out = {"type": b["type"], "id": b["id"], "title": b.get("title", "")}
        if policy == POLICY:
            out["verification"] = "result_values"
        if b["type"] == "kpi":
            out["value"] = cell(artifacts, b["value_ref"], b["format"], True)
            if policy == POLICY:
                out["title"] = label(b["value_ref"]["column"]) + "（所选记录）"
        elif b["type"] == "text":
            if not set(b["evidence_ids"]) <= set(artifacts) | set(sources):
                raise DataError("REPORT_EVIDENCE_NOT_ALLOWED")
            out.update(
                segments=text_segments(b, artifacts),
                evidence_ids=b["evidence_ids"],
                row_bindings=b["row_bindings"],
            )
            if policy == POLICY:
                out["verification"] = (
                    "result_values" if b == (observation_texts or {}).get(b["id"]) else "unverified"
                )
        else:
            record = artifacts[b["artifact_id"]]
            validate_display_scope(b, record, main_range)
            out["display_scope"] = b["display_scope"]
            ordered = record["artifact"]["ordered"]
            names = b["columns"] if b["type"] == "table" else [b["x"], *b["y"]]
            if len(set(names)) != len(names):
                raise DataError("REPORT_DUPLICATE_COLUMN")
            labels = (
                b.get("column_labels", {})
                if b["type"] == "table"
                else {b["x"]: b["x_label"], **b["series_labels"]}
            )
            if not set(labels) <= set(names) or any(
                not isinstance(label, str) or not 1 <= len(label) <= 120
                for label in labels.values()
            ):
                raise DataError("REPORT_COLUMN_LABEL")
            if policy == POLICY:
                labels = {name: label(name) for name in names}
                out["title"] = "完整查询结果" if b["type"] == "table" else "查询结果图"
            available = {column["name"] for column in record["artifact"]["columns"]}
            if not set(names) <= available:
                raise DataError("REPORT_COLUMN_NOT_FOUND")
            rows = []
            if b["type"] == "chart":
                if len(record["rows"]) > 60:
                    raise DataError("REPORT_CHART_TOO_MANY_ROWS")
                if b["kind"] == "line" and not ordered:
                    raise DataError("REPORT_LINE_REQUIRES_ORDER")
            for row in range(len(record["rows"])):
                values = []
                for index, name in enumerate(names):
                    is_y = b["type"] == "chart" and index > 0
                    value = cell(
                        artifacts,
                        {"artifact_id": b["artifact_id"], "row": row, "column": name},
                        b["format"] if is_y else None,
                        is_y,
                    )
                    if is_y and value["raw"] is not None and abs(numeric(value["raw"])) > 10**15:
                        raise DataError("REPORT_CHART_VALUE_TOO_LARGE")
                    values.append(value)
                rows.append(values)
            if b["type"] == "chart":
                # 每个横轴值只能对应一行，避免跨月或分组结果被画成同一分类。
                axis = [row[0]["raw"] for row in rows]
                if len({canonical_bytes(key) for key in axis}) != len(axis):
                    raise DataError("REPORT_CHART_AXIS_AMBIGUOUS")
            if b["type"] == "chart" and b["kind"] == "line":
                # 折线只连接唯一、按升序排列的真实横轴，不能把任意行序画成趋势。
                keys = [row[0]["raw"] for row in rows]
                keys = [key.get("value") if isinstance(key, dict) else key for key in keys]
                if any(key is None or isinstance(key, bool) for key in keys):
                    raise DataError("REPORT_LINE_AXIS_INVALID")
                try:
                    if len(set(keys)) != len(keys) or keys != sorted(keys):
                        raise DataError("REPORT_LINE_AXIS_INVALID")
                except TypeError:
                    raise DataError("REPORT_LINE_AXIS_INVALID") from None
            if not ordered and len(rows) > 1:
                warnings.append("部分表格或柱状图按冻结结果顺序展示，不代表业务排名。")
            out.update(
                artifact_id=b["artifact_id"],
                columns=[{"name": name, "label": labels.get(name, name)} for name in names],
                rows=rows,
                ordered=ordered,
            )
            if b["type"] == "chart":
                out["kind"] = b["kind"]
        blocks.append(RenderedBlock.model_validate(out).model_dump())
    return blocks, warnings


def materialize(draft, frozen, *, require_query=True, runtime_warnings=()):
    """
    作用：只在固定证据集合上生成可保存报告，不允许换数据或口径
    输入：严格草稿与冻结上下文
    输出：含展示值的报告准备数据
    """
    draft = ReportDraft.model_validate(draft)
    # 模型可声明主范围，但不能覆盖服务端范围；旧草稿省略时沿用原契约。
    if draft.date_range is not None and draft.date_range.model_dump() != frozen.get("date_range"):
        raise DataError(
            "REPORT_SCOPE_MAIN_RANGE",
            {"instruction": "主范围须等于固定date_range；子范围请声明块display_scope。"},
        )
    wanted = artifact_ids(draft, require_query=require_query)
    if not wanted <= set(frozen["artifacts"]):
        raise DataError("REPORT_ARTIFACT_NOT_ALLOWED")
    artifacts = {key: frozen["artifacts"][key] for key in wanted}
    metrics = {
        ident for item in artifacts.values() for ident in item["artifact"]["metric_revision_ids"]
    }
    if set(draft.metric_revision_ids) != metrics or len(draft.metric_revision_ids) != len(metrics):
        raise DataError("REPORT_METRIC_BINDING")
    if not metrics <= set(frozen["metrics"]):
        raise DataError("REPORT_METRIC_NOT_ALLOWED")
    blocks, warnings = render_blocks(
        draft,
        artifacts,
        frozen["sources"],
        frozen.get("date_range"),
        frozen.get("verification_policy"),
        frozen.get("observation_texts"),
    )
    if frozen.get("verification_policy") == POLICY:
        warnings.append(
            "已核对结论仅限完整冻结结果集内的排序或同列数值关系；不认证SQL业务分母、总体覆盖、显著性或因果。"
        )
    warnings.extend(runtime_warnings)
    # 运行收尾产生的真实未完成事项在人工修订时继续保留，模型不能移除。
    warnings.extend(frozen.get("runtime_limitations", []))
    # 探索身份由成功产物决定，模型或人工修改报告说明不能删除这条边界。
    if any(item["artifact"].get("query_purpose") == "exploratory" for item in artifacts.values()):
        warnings.append("包含数据探索结果，未绑定已审核项目指标；范围、计算与缺失处理见固定依据。")
    if any(
        source.get("source_kind") in {"official_advisory", "official_weather_observations"}
        for source in frozen["sources"].values()
    ):
        warnings.append(
            "外部观测与公告是固定来源证据，不单独证明航班异常原因；有效期及航司限制见原文。"
        )
    result = {
        **copy.deepcopy(frozen),
        "draft": draft.model_dump(),
        "title": draft.title,
        "scope": draft.scope,
        "blocks": blocks,
        "has_unverified_explanations": any(b.get("verification") == "unverified" for b in blocks),
        "validation_level": "references_validated",
        "limitations": list(
            dict.fromkeys(
                [
                    *draft.limitations,
                    *warnings,
                    "数字和引用已核对；自由 SQL 的业务口径与解释仍需结合来源复核。",
                ]
            )
        ),
    }
    if len(canonical_bytes(result)) > 12 * 1024 * 1024:
        raise DataError("REPORT_TOO_LARGE")
    return result


def content_hash(payload):
    """
    作用：计算冻结报告规范摘要以检测存储损坏
    输入：不含摘要字段的报告内容
    输出：SHA-256
    """
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
