"""报告读取、历史、证据、修订与安全导出接口。"""

import json
import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from metric_pilot.data.api import get_catalog
from metric_pilot.data.contracts import Page
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.reports.contracts import (
    EvidenceDTO,
    ReportDTO,
    ReportRevisionWrite,
    ReportSummary,
)
from metric_pilot.reports.service import ReportService

router = APIRouter(prefix="/api/v1/reports")


def service_for(request):
    """
    作用：绑定同一工作区的报告服务
    输入：请求
    输出：报告服务
    """
    catalog = get_catalog(request)
    settings = request.app.state.settings
    return ReportService(
        QueryService(catalog, QueryExecutor(settings.sql_timeout_seconds, settings.sql_max_rows))
    )


@router.get("", response_model=Page[ReportSummary])
def listing(request: Request, cursor: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    """
    作用：展示真实报告列表
    输入：分页游标
    输出：列表
    """
    return service_for(request).listing(cursor, limit)


@router.get("/{report_id}/revisions", response_model=Page[ReportSummary])
def history(report_id: str, request: Request, cursor: int = Query(0, ge=0)):
    """
    作用：查看保留的不可变修订历史
    输入：报告与分页游标
    输出：历史
    """
    return service_for(request).listing(cursor, 20, report_id)


@router.get("/{report_id}/revisions/{revision_id}", response_model=ReportDTO)
def detail(report_id: str, revision_id: str, request: Request):
    """
    作用：读取指定固定报告
    输入：报告及修订
    输出：详情
    """
    return service_for(request).read(report_id, revision_id)


@router.post("/{report_id}/revisions", response_model=ReportDTO, status_code=201)
def revise(report_id: str, body: ReportRevisionWrite, request: Request):
    """
    作用：人工修改标题、解释或布局并保留旧版
    输入：固定证据上的草稿与加载时版本
    输出：新修订
    """
    return service_for(request).revise(report_id, body)


@router.get("/{report_id}/revisions/{revision_id}/evidence", response_model=EvidenceDTO)
def evidence(
    report_id: str,
    revision_id: str,
    request: Request,
    block_id: str,
    row: int = Query(0, ge=0),
    column: str | None = None,
    value_key: str | None = None,
):
    """
    作用：只读当前报告内指定值的来源
    输入：固定修订、块和单元格选择
    输出：依据抽屉数据
    """
    return service_for(request).evidence(report_id, revision_id, block_id, row, column, value_key)


def escape_markdown(value):
    """
    作用：避免模型文字在导出时变成 HTML、图片或外部链接
    输入：普通文字
    输出：转义 Markdown
    """
    value = str(value).replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", value)


def markdown(report, query_evidence=None):
    """
    作用：导出服务端物化值并附固定证据索引，不执行模板
    输入：固定报告 DTO
    输出：UTF-8 Markdown
    """
    lines = [
        f"# {escape_markdown(report.title)}",
        "",
        escape_markdown(report.scope),
        "",
        (
            f"调查日期：{report.date_range.start} 至 "
            f"{report.date_range.end_exclusive}（不含结束日）"
            if report.date_range
            else f"数据月份：{report.data_snapshot.period}"
        )
        + f" · 版本 {report.version}",
        "",
    ]
    refs = {}
    for block in report.blocks:
        if block.title:
            lines.extend([f"## {escape_markdown(block.title)}", ""])
        if block.type == "kpi":
            value = block.value
            lines.append(escape_markdown(value.display))
            refs[str(value.value_ref.model_dump())] = value.value_ref
        elif block.type == "text":
            lines.append("".join(escape_markdown(s.text) for s in block.segments))
            for segment in block.segments:
                if segment.value:
                    refs[str(segment.value.value_ref.model_dump())] = segment.value.value_ref
        else:
            lines.append("| " + " | ".join(escape_markdown(c.label) for c in block.columns) + " |")
            lines.append("| " + " | ".join("---" for _ in block.columns) + " |")
            for row in block.rows:
                lines.append("| " + " | ".join(escape_markdown(v.display) for v in row) + " |")
            lines.append(f"固定查询产物：{block.artifact_id}")
            if block.type == "chart":
                lines.append("图表以对应的冻结数据表导出。")
        lines.append("")
    lines.extend(["## 限制与说明", ""])
    lines.extend("- " + escape_markdown(value) for value in report.limitations)
    lines.extend(["", "## 数字与来源索引", ""])
    for ref in refs.values():
        lines.append(
            f"- {escape_markdown(ref.artifact_id)}：第 {ref.row + 1} 行，"
            f"列 {escape_markdown(ref.column)}"
        )
    for source in report.sources:
        lines.append(
            f"- {escape_markdown(source.title)}：{escape_markdown(source.id)}"
            f"（{source.source_kind}）"
        )
    for ident, item in (query_evidence or {}).items():
        artifact = item["artifact"]
        lines.extend(
            [
                "",
                "### 查询依据",
                "",
                f"产物：{escape_markdown(ident)}",
                f"结果摘要：{artifact['result_hash']}",
                f"数据版本：{artifact['snapshot_id']}",
                "",
            ]
        )
        lines.extend("    " + line for line in artifact["sql"].splitlines())
        lines.extend(["", "绑定参数：", ""])
        lines.extend(
            "    " + line
            for line in json.dumps(
                artifact["parameters"], ensure_ascii=False, indent=2
            ).splitlines()
        )
    lines.extend(["", f"报告修订：{report.revision_id}", f"内容摘要：{report.content_hash}"])
    return "\n".join(lines) + "\n"


@router.get("/{report_id}/revisions/{revision_id}/export")
def export(
    report_id: str,
    revision_id: str,
    request: Request,
    format: str = Query("markdown", pattern="^(markdown|json)$"),
):
    """
    作用：导出当前固定修订，不重查数据或重新请求模型
    输入：报告、修订及格式
    输出：下载响应
    """
    service = service_for(request)
    report = service.read(report_id, revision_id)
    frozen, _ = service.frozen(report_id, revision_id)
    # 导出附上固定查询元数据与原始行，离开页面后也能核对 SQL 和取值位置。
    content = (
        markdown(report, frozen["artifacts"])
        if format == "markdown"
        else json.dumps(
            {
                **report.model_dump(mode="json"),
                "query_evidence": frozen["artifacts"],
                "knowledge_snapshot": frozen["knowledge_snapshot"],
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
    suffix = "md" if format == "markdown" else "json"
    return Response(
        content=content,
        media_type="text/markdown" if suffix == "md" else "application/json",
        headers={"Content-Disposition": f'attachment; filename="report-{report.version}.{suffix}"'},
    )
