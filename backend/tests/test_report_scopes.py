"""阶段7.C：合成范围反例及真实受控查询的修正路径。"""

import copy

import pytest
import test_agent
from metric_pilot.data.common import DataError
from metric_pilot.reports.api import markdown
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.materialize import materialize, render_blocks
from test_agent import calls, execute, final_from_query
from test_reports import service

agent_workspace = test_agent.agent_workspace

MAIN = {"start": "2026-01-01", "end_exclusive": "2026-03-01"}
SUB = {"start": "2026-01-01", "end_exclusive": "2026-02-01"}


def sample():
    """
    作用：生成明确合成的跨月分组反例
    输入：无
    输出：草稿与冻结行
    """
    draft = {
        "schema_version": "1",
        "title": "合成",
        "scope": "说明",
        "blocks": [
            {
                "type": "table",
                "id": "groups",
                "title": "仅OH",
                "artifact_id": "qa_test",
                "columns": ["carrier", "day", "count"],
                "display_scope": {
                    "dimensions": ["carrier", "day"],
                    "population": {"carrier": ["OH"]},
                },
            }
        ],
    }
    artifacts = {
        "qa_test": {
            "artifact": {
                "ordered": True,
                "metric_revision_ids": [],
                "columns": [
                    {"name": "carrier", "type": "VARCHAR"},
                    {"name": "day", "type": "DATE"},
                    {"name": "count", "type": "BIGINT"},
                ],
            },
            "rows": [
                ["OH", {"type": "date", "value": "2026-01-01"}, 10],
                ["OO", {"type": "date", "value": "2026-02-01"}, 20],
            ],
        }
    }
    return draft, artifacts


def test_population_rejects_title_filter_and_keeps_input():
    """
    作用：拒绝以指定人群声明包装全人群结果且不修改冻结证据
    输入：合成混合航司结果
    输出：无
    """
    draft, artifacts = sample()
    before = copy.deepcopy(artifacts)
    with pytest.raises(DataError, match="REPORT_SCOPE_POPULATION"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {}, MAIN)
    assert artifacts == before
    draft["blocks"][0]["display_scope"]["population"]["carrier"].append("OO")
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {}, MAIN)
    assert len(blocks[0]["rows"]) == 2
    assert blocks[0]["rows"][1][0]["value_ref"]["row"] == 1


@pytest.mark.parametrize(
    "change,error",
    [
        ("dimension", "REPORT_SCOPE_DIMENSIONS"),
        ("duplicate", "REPORT_SCOPE_DIMENSIONS"),
        ("missing_column", "REPORT_SCOPE_POPULATION"),
        ("empty_population", "REPORT_SCOPE_POPULATION"),
        ("date_pair", "REPORT_SCOPE_DATE_COLUMN"),
        ("date_rows", "REPORT_SCOPE_DATE_ROWS"),
        ("outside", "REPORT_SCOPE_DATE_RANGE"),
        ("no_main", "REPORT_SCOPE_MAIN_RANGE_REQUIRED"),
    ],
)
def test_invalid_scope(change, error):
    """
    作用：拒绝隐藏维度、无效人群、越界或无日期支持的子范围
    输入：反例类型与错误码
    输出：无
    """
    draft, artifacts = sample()
    scope = draft["blocks"][0]["display_scope"]
    scope["population"] = {}
    main = MAIN
    if change == "dimension":
        scope["dimensions"] = ["missing"]
    elif change == "duplicate":
        scope["dimensions"] = ["carrier", "carrier"]
    elif change == "missing_column":
        scope["population"] = {"missing": ["OH"]}
    elif change == "empty_population":
        scope["population"] = {"carrier": []}
    else:
        scope["date_range"], scope["date_column"] = SUB, "day"
        if change == "date_pair":
            scope.pop("date_column")
        elif change == "outside":
            scope["date_range"] = {"start": "2025-01-01", "end_exclusive": "2026-02-01"}
        elif change == "no_main":
            main = None
    with pytest.raises(DataError, match=error):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {}, main)


@pytest.mark.parametrize(
    "value", [None, "2026-01", "20260101", {"type": "datetime", "value": "2026-01-01"}]
)
def test_subrange_requires_exact_daily_dates(value):
    """
    作用：不将缺失、月份或非规范日期当作日范围证据
    输入：合成无效日期
    输出：无
    """
    draft, artifacts = sample()
    artifacts["qa_test"]["rows"] = [["OH", value, 10]]
    draft["blocks"][0]["display_scope"].update(date_range=SUB, date_column="day")
    with pytest.raises(DataError, match="REPORT_SCOPE_DATE_ROWS"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {}, MAIN)


@pytest.mark.parametrize("empty", [False, True])
def test_subrange_and_legacy_are_compatible(empty):
    """
    作用：保留空结果、子范围及无新字段的旧草稿兼容
    输入：是否为空
    输出：无
    """
    draft, artifacts = sample()
    artifacts["qa_test"]["rows"] = [] if empty else artifacts["qa_test"]["rows"][:1]
    draft["blocks"][0]["display_scope"].update(date_range=SUB, date_column="day")
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {}, MAIN)
    assert blocks[0]["display_scope"]["date_range"] == SUB
    draft["blocks"][0].pop("display_scope")
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert blocks[0]["display_scope"] is None


def test_main_range_cannot_be_overridden():
    """
    作用：拒绝模型把子范围声明为报告主范围
    输入：合成固定上下文
    输出：无
    """
    draft, artifacts = sample()
    draft["date_range"] = SUB
    with pytest.raises(DataError, match="REPORT_SCOPE_MAIN_RANGE$"):
        materialize(draft, {"artifacts": artifacts, "date_range": MAIN})


def scoped_candidate(messages):
    """
    作用：以真实工具产物创建待校验的日范围报告
    输入：隔离模型消息
    输出：候选字典
    """
    ident = final_from_query(messages)["artifact_ids"][0]
    return {
        "kind": "report",
        "report": {
            "schema_version": "1",
            "title": "合成单日",
            "scope": "合成原始记录探索",
            "blocks": [
                {
                    "type": "table",
                    "id": "days",
                    "title": "单日",
                    "artifact_id": ident,
                    "columns": ["day", "count"],
                    "display_scope": {
                        "dimensions": ["day"],
                        "population": {"day": ["2026-01-01"]},
                        "date_range": {"start": "2026-01-01", "end_exclusive": "2026-01-02"},
                        "date_column": "day",
                    },
                }
            ],
        },
    }


@pytest.mark.parametrize("repair", [True, False])
def test_runtime_scope_rejection_and_bounded_repair(agent_workspace, repair):
    """
    作用：验证真实DuckDB拒绝后重新筛选及两次修正上限
    输入：隔离合成数据与是否修正
    输出：无
    """
    sql = "SELECT CAST(flight_date AS VARCHAR) AS day, COUNT(*) AS count FROM v_flights"
    steps = [
        calls(("query_sql", {"sql": sql + " GROUP BY flight_date ORDER BY flight_date"})),
        scoped_candidate,
    ]
    if repair:
        steps.extend(
            [
                calls(
                    (
                        "query_sql",
                        {
                            "sql": sql
                            + " WHERE flight_date='2026-01-01'"
                            + " GROUP BY flight_date ORDER BY flight_date"
                        },
                    )
                ),
                scoped_candidate,
            ]
        )
    run, llm = execute(agent_workspace, steps)
    assert any("REPORT_SCOPE_POPULATION" in str(messages) for messages in llm.messages)
    if repair:
        assert run["status"] == "completed", run
        report = service(agent_workspace).read(
            run["result"]["report_id"], run["result"]["revision_id"]
        )
        assert len(report.blocks[0].rows) == 1
        assert "日期子范围：2026-01-01 至 2026-01-02" in markdown(report)
        assert (
            report.date_range.model_dump() != report.blocks[0].display_scope.date_range.model_dump()
        )
    else:
        assert run["error_code"] == "FINAL_CANDIDATE_INVALID"


def test_chart_dimensions_must_be_visible_categories():
    """
    作用：防止图表声明独立月份维度却只画航司分类
    输入：合成多维图表
    输出：无
    """
    draft, artifacts = sample()
    draft["blocks"] = [
        {
            "type": "chart",
            "id": "chart",
            "title": "分组",
            "kind": "bar",
            "artifact_id": "qa_test",
            "x": "carrier",
            "y": ["count"],
            "display_scope": {"dimensions": ["carrier", "day"]},
        }
    ]
    with pytest.raises(DataError, match="REPORT_SCOPE_DIMENSIONS"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {}, MAIN)
    draft["blocks"][0]["display_scope"]["dimensions"] = ["carrier"]
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {}, MAIN)
    assert len(blocks[0]["rows"]) == 2
