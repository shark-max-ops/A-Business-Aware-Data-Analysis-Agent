"""阶段 4：真实执行产物、报告物化、拒绝边界与不可变修订。"""

import asyncio
import copy
import threading
import time

import pytest
import test_agent
from fastapi.testclient import TestClient
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.main import create_app
from metric_pilot.reports import models as rm
from metric_pilot.reports.api import markdown
from metric_pilot.reports.contracts import ReportDraft, ReportRevisionWrite
from metric_pilot.reports.service import ReportService
from sqlalchemy import func, select
from test_agent import FakeLLM, calls, execute, final_from_query, new_run

agent_workspace = test_agent.agent_workspace

SQL = """SELECT flight_date AS day, COUNT(*) AS record_count,
CAST(100.0 * SUM(CASE WHEN cancelled THEN 1 ELSE 0 END) / COUNT(*) AS DECIMAL(8,2)) AS cancel_pct
FROM v_flights GROUP BY flight_date ORDER BY flight_date"""


def report_candidate(messages):
    """
    作用：从真实工具返回值组成测试报告，不预填数值答案
    输入：真实工具消息
    输出：报告最终候选
    """
    artifact_id = final_from_query(messages)["artifact_ids"][0]
    ref = {"artifact_id": artifact_id, "row": 0, "column": "record_count"}
    return {
        "kind": "report",
        "conversation_title": "每日航班统计",
        "report": {
            "schema_version": "1",
            "title": "每日航班记录",
            "scope": "按航班日期统计合成测试数据",
            "metric_revision_ids": [],
            "limitations": ["合成数据仅供测试"],
            "blocks": [
                {
                    "type": "kpi",
                    "id": "count",
                    "title": "首日记录数",
                    "value_ref": ref,
                    "format": "integer",
                },
                {
                    "type": "kpi",
                    "id": "pct",
                    "title": "首日取消比例（探索）",
                    "value_ref": {**ref, "column": "cancel_pct"},
                    "format": "percent_2",
                },
                {
                    "type": "text",
                    "id": "note",
                    "text_template": "首日共有 {{count}} 条记录。",
                    "values": {"count": ref},
                    "evidence_ids": [artifact_id],
                },
                {
                    "type": "table",
                    "id": "table",
                    "title": "逐日明细",
                    "artifact_id": artifact_id,
                    "columns": ["day", "record_count", "cancel_pct"],
                    "column_labels": {
                        "day": "日期",
                        "record_count": "记录数",
                        "cancel_pct": "取消比例",
                    },
                },
                {
                    "type": "chart",
                    "id": "chart",
                    "title": "逐日记录趋势",
                    "artifact_id": artifact_id,
                    "kind": "line",
                    "x": "day",
                    "y": ["record_count"],
                    "x_label": "日期",
                    "series_labels": {"record_count": "航班记录"},
                    "format": "integer",
                },
            ],
        },
    }


def service(workspace):
    """
    作用：绑定隔离查询与报告服务
    输入：工作区
    输出：报告服务
    """
    config, store, _, _ = workspace
    return ReportService(
        QueryService(Catalog(store.engine, config.storage_dir), QueryExecutor(10, 1000))
    )


def completed(workspace):
    """
    作用：执行真实查询并由 Runtime 原子提交报告
    输入：隔离工作区
    输出：运行和冻结报告
    """
    run, _ = execute(workspace, [calls(("query_sql", {"sql": SQL})), report_candidate])
    assert run["status"] == "completed", run
    assert run["result"]["kind"] == "report"
    return run, service(workspace).read(run["result"]["report_id"], run["result"]["revision_id"])


def test_report_real_sql_values_events_and_evidence(agent_workspace):
    """
    作用：验证真实 DuckDB 到报告和取证的完整路径
    输入：隔离数据
    输出：无
    """
    run, report = completed(agent_workspace)
    value = report.blocks[0].value
    assert value.raw == report.blocks[2].segments[1].value.raw
    assert value.display == str(value.raw)
    evidence = service(agent_workspace).evidence(report.report_id, report.revision_id, "count")
    assert evidence.value == value
    assert evidence.artifact.sql == SQL
    assert evidence.data_snapshot.id == run["snapshot_id"]
    events = agent_workspace[1].events_after(run["id"], 0)
    types = [e["type"] for e in events]
    assert types[-2:] == ["report.ready", "run.completed"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert report.validation_level == "references_validated"
    assert report.blocks[4].columns[1].label == "记录数"
    assert "业务" in report.limitations[-1]


@pytest.mark.parametrize(
    "case,code",
    [
        ("row", "REPORT_ROW_OUT_OF_RANGE"),
        ("column", "REPORT_COLUMN_NOT_FOUND"),
        ("foreign", "ARTIFACT_NOT_FOUND"),
        ("duplicate", "REPORT_DUPLICATE_BLOCK"),
        ("scale", "REPORT_PERCENT_SCALE"),
        ("type", "REPORT_VALUE_TYPE"),
        ("metric", "REPORT_METRIC_BINDING"),
        ("template", "REPORT_TEMPLATE_INVALID"),
        ("unused", "REPORT_TEMPLATE_KEYS"),
        ("evidence", "REPORT_EVIDENCE_NOT_ALLOWED"),
        ("column_label", "REPORT_COLUMN_LABEL"),
        ("line_order", "REPORT_LINE_REQUIRES_ORDER"),
    ],
)
def test_report_rejects_invalid_claims(agent_workspace, case, code):
    """
    作用：拒绝无效引用、类型、布局与模板
    输入：隔离产物和单项恶意变更
    输出：无
    """
    run, report = completed(agent_workspace)
    draft = report.draft.model_dump()
    if case == "row":
        draft["blocks"][0]["value_ref"]["row"] = 100000
    elif case == "column":
        draft["blocks"][0]["value_ref"]["column"] = "not_a_column"
    elif case == "foreign":
        other, other_report = completed(agent_workspace)
        assert other["id"] != run["id"]
        draft["blocks"][0]["value_ref"]["artifact_id"] = other_report.blocks[
            0
        ].value.value_ref.artifact_id
    elif case == "duplicate":
        draft["blocks"][1]["id"] = draft["blocks"][0]["id"]
    elif case == "scale":
        draft["blocks"][0]["format"] = "percent_2"
    elif case == "type":
        draft["blocks"][0]["value_ref"]["column"] = "day"
    elif case == "metric":
        draft["metric_revision_ids"] = ["metricrev_forged"]
    elif case == "template":
        draft["blocks"][2]["text_template"] = "{{danger()}}"
    elif case == "unused":
        draft["blocks"][2]["text_template"] = "没有占位符"
    elif case == "evidence":
        draft["blocks"][2]["evidence_ids"] = ["https://invalid.example/secret"]
    elif case == "column_label":
        draft["blocks"][3]["column_labels"]["missing"] = "虚构列"
    elif case == "line_order":
        # 修改冻结对象仅模拟未排序查询；正式 prepare 会从执行记录读取该标记。
        from metric_pilot.reports.materialize import materialize

        frozen, _ = service(agent_workspace).frozen(report.report_id, report.revision_id)
        frozen = copy.deepcopy(frozen)
        for item in frozen["artifacts"].values():
            item["artifact"]["ordered"] = False
        with pytest.raises(DataError, match=code):
            materialize(draft, frozen)
        return
    with pytest.raises(DataError, match=code):
        service(agent_workspace).prepare(run, ReportDraft.model_validate(draft))


def test_report_revision_keeps_old_content_without_result_files(agent_workspace):
    """
    作用：验证修订与旧报告不依赖当前数据、知识或原产物文件
    输入：已完成报告
    输出：无
    """
    run, report = completed(agent_workspace)
    svc = service(agent_workspace)
    original = report.model_dump()
    # 模拟原产物不再可用；旧报告必须仍从自己的冻结副本读取。
    for path in (agent_workspace[0].storage_dir / "artifacts").glob("*.json"):
        path.unlink()
    draft = report.draft.model_copy(update={"title": "修改后的中文标题"})
    newer = svc.revise(report.report_id, ReportRevisionWrite(expected_version=1, report=draft))
    assert newer.version == 2
    assert newer.blocks == report.blocks
    assert svc.read(report.report_id, report.revision_id).model_dump() == original
    assert (
        svc.evidence(report.report_id, report.revision_id, "count").value == report.blocks[0].value
    )
    with pytest.raises(DataError, match="REPORT_VERSION_CONFLICT"):
        svc.revise(report.report_id, ReportRevisionWrite(expected_version=1, report=draft))
    assert len(svc.listing(report_id=report.report_id)["items"]) == 2
    assert agent_workspace[1].read(run["id"])["result"]["revision_id"] == report.revision_id


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_report_commit_checks_stop_inside_transaction(agent_workspace, stop):
    """
    作用：验证报告准备后取消或超时不会留下成功报告
    输入：停止类型
    输出：无
    """
    config, store, _, _ = agent_workspace
    run = new_run(agent_workspace)
    llm = FakeLLM([calls(("query_sql", {"sql": SQL})), report_candidate])
    original_finish = store.finish

    def finish(*args, **kwargs):
        """
        作用：在提交边界注入真实停止条件
        输入：Runtime 提交参数
        输出：无
        """
        if kwargs.get("prepared_report") is not None:
            if stop == "cancel":
                store.cancel(run["id"])
            else:
                kwargs["deadline"] = time.monotonic() - 1
        return original_finish(*args, **kwargs)

    store.finish = finish
    asyncio.run(AgentRuntime(store, config, llm).run(run, threading.Event()))
    assert store.read(run["id"])["status"] == ("cancelled" if stop == "cancel" else "failed")
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(rm.reports)).scalar_one() == 0
    assert "report.ready" not in [e["type"] for e in store.events_after(run["id"], 0)]


def test_report_api_export_and_forged_evidence(agent_workspace):
    """
    作用：验证公开契约、中文导出和取证选择范围
    输入：真实报告与隔离应用
    输出：无
    """
    _, report = completed(agent_workspace)
    base = f"/api/v1/reports/{report.report_id}/revisions/{report.revision_id}"
    with TestClient(create_app(agent_workspace[0])) as client:
        assert client.get("/api/v1/reports").json()["items"][0]["title"] == report.title
        assert client.get(base).status_code == 200
        assert (
            client.get(
                base + "/evidence", params={"block_id": "chart", "row": 0, "column": "record_count"}
            ).json()["value"]["raw"]
            == report.blocks[0].value.raw
        )
        assert (
            client.get(
                base + "/evidence", params={"block_id": "chart", "column": "not_a_column"}
            ).status_code
            == 404
        )
        md = client.get(base + "/export?format=markdown")
        assert md.status_code == 200 and "每日航班记录" in md.text
        assert report.revision_id in md.text
        assert SQL in md.text.replace("    ", "")
        exported = client.get(base + "/export?format=json").json()
        assert exported["content_hash"] == report.content_hash
        artifact_id = report.blocks[0].value.value_ref.artifact_id
        assert exported["query_evidence"][artifact_id]["artifact"]["sql"] == SQL
        assert exported["query_evidence"][artifact_id]["rows"][0][1] == report.blocks[0].value.raw
    assert "业务" in markdown(report)


@pytest.mark.parametrize(
    "raw,fmt,column,expected",
    [
        (None, "integer", "count", "无可用值"),
        (True, None, "cancelled", "是"),
        (False, None, "cancelled", "否"),
        (1e100, "decimal_2", "amount", format(10**100, ",") + ".00"),
        (32, "percent_2", "rate_pct", "32.00%"),
        (0, "percent_2", "rate_pct", "0.00%"),
        (100, "percent_2", "rate_pct", "100.00%"),
        (
            {"type": "decimal", "value": "12345678901234567890.125"},
            "decimal_2",
            "amount",
            "12,345,678,901,234,567,890.13",
        ),
        (
            {"type": "integer", "value": "9007199254740993"},
            "integer",
            "count",
            "9,007,199,254,740,993",
        ),
        (-10, "minutes_2", "arrival_delay", "-10.00 分钟"),
    ],
)
def test_report_value_formats_are_exact(raw, fmt, column, expected):
    """
    作用：核对百分比、空值、大整数与 Decimal，不进行指标推算
    输入：带类型的真实值格式样例
    输出：无
    """
    from metric_pilot.reports.values import display_value

    assert display_value(raw, fmt, column) == expected


def test_report_truncation_cannot_create_success_card(agent_workspace):
    """
    作用：拒绝用截断结果发布报告，保留失败修复记录
    输入：仅允许保存一行的真实查询
    输出：无
    """
    run, llm = execute(
        agent_workspace, [calls(("query_sql", {"sql": SQL})), report_candidate], sql_max_rows=1
    )
    assert run["status"] == "failed"
    assert any("REPORT_TRUNCATED_RESULT" in str(messages) for messages in llm.messages)
    with agent_workspace[1].engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(rm.reports)).scalar_one() == 0


def test_source_update_and_metric_rollback_cannot_rewrite_report(agent_workspace):
    """
    作用：验证原文更新及指标回滚不改变旧报告依据
    输入：真实知识访问、查询、隔离指标审核与新修订
    输出：无
    """
    from metric_pilot.knowledge.contracts import MetricWrite, VersionAction
    from metric_pilot.knowledge.metrics import MetricService
    from test_knowledge import add_document

    approved = agent_workspace[3]

    def candidate(messages):
        """
        作用：绑定本次实际解析并执行的测试指标
        输入：工具消息
        输出：测试报告候选
        """
        result = report_candidate(messages)
        result["report"]["metric_revision_ids"] = [approved["id"]]
        return result

    run, _ = execute(
        agent_workspace,
        [
            calls(
                ("search_knowledge", {"query": "ArrDelay"}),
                ("resolve_metric", {"metric_key": "arrival_delay_rate"}),
            ),
            calls(("query_sql", {"sql": SQL, "metric_revision_ids": [approved["id"]]})),
            candidate,
        ],
    )
    assert run["status"] == "completed", run
    svc = service(agent_workspace)
    report = svc.read(run["result"]["report_id"], run["result"]["revision_id"])
    before = svc.evidence(report.report_id, report.revision_id, "count").model_dump()
    assert before["sources"] and before["metrics"][0]["id"] == approved["id"]
    from metric_pilot.knowledge import models as km

    with agent_workspace[1].engine.connect() as connection:
        doc = connection.execute(select(km.documents)).mappings().first()
    add_document(
        agent_workspace[1],
        content="Updated ArrDelay description.",
        document_id=doc["id"],
        expected_version=doc["version"],
    )
    metrics = MetricService(agent_workspace[1])
    definition = copy.deepcopy(approved["definition"])
    definition.update(null_policy="隔离测试的新缺失值说明", review_state="draft", version=3)
    saved = metrics.save(MetricWrite(definition=definition, expected_version=2))
    metrics.approve(
        "arrival_delay_rate", VersionAction(revision_id=saved["id"], expected_version=3)
    )
    assert svc.evidence(report.report_id, report.revision_id, "count").model_dump() == before
    # 回退采用原口径的新修订，仍只发生在隔离数据库。
    restored = {**approved["definition"], "review_state": "draft", "version": 5}
    rollback = metrics.save(MetricWrite(definition=restored, expected_version=4))
    metrics.approve(
        "arrival_delay_rate", VersionAction(revision_id=rollback["id"], expected_version=5)
    )
    assert svc.evidence(report.report_id, report.revision_id, "count").model_dump() == before


def test_model_chart_code_and_extra_fields_are_rejected(agent_workspace):
    """
    作用：报告不能接受模型注入图表执行配置
    输入：已保存草稿中的伪造字段
    输出：无
    """
    from pydantic import ValidationError

    _, report = completed(agent_workspace)
    draft = report.draft.model_dump()
    draft["blocks"][-1]["formatter"] = "function(){alert(1)}"
    with pytest.raises(ValidationError):
        ReportDraft.model_validate(draft)


def test_invalid_report_can_be_repaired_with_real_artifact(agent_workspace):
    """
    作用：验证报告错误反馈可被下一模型回合修复
    输入：真实工具返回与一次无效列引用
    输出：无
    """

    def bad(messages):
        """
        作用：只改变列引用以触发可修复错误
        输入：工具消息
        输出：无效候选
        """
        result = report_candidate(messages)
        result["report"]["blocks"][0]["value_ref"]["column"] = "missing_column"
        return result

    run, llm = execute(agent_workspace, [calls(("query_sql", {"sql": SQL})), bad, report_candidate])
    assert run["status"] == "completed"
    assert run["result"]["kind"] == "report"
    assert "REPORT_COLUMN_NOT_FOUND" in str(llm.messages[-1])


def test_context_retains_unavailable_metrics_when_tool_history_is_trimmed():
    """
    作用：指标不可用事实不能随工具消息裁剪而丢失，避免反复检索耗尽预算
    输入：完整历史组和已失败指标
    输出：无
    """
    from metric_pilot.agent.context import fit_context

    registry = type(
        "Registry",
        (),
        {
            "artifact_ids": set(),
            "resolved": set(),
            "definitions": {},
            "blocked_metrics": {"unreviewed_metric"},
        },
    )()
    group = [
        {"role": "assistant", "tool_calls": [{"id": "one"}]},
        {"role": "tool", "tool_call_id": "one", "content": "x" * 20000},
    ]
    messages = fit_context([{"role": "user", "content": "goal"}], [group], registry, 1500)
    assert "unreviewed_metric" in str(messages)
    assert not any(message["role"] == "tool" for message in messages)


def test_report_percent_is_consistent_in_table_text_and_kpi(agent_workspace):
    """
    作用：三种展示统一百分比，拒绝文字模板重复附加百分号
    输入：真实比例查询与已有报告
    输出：无
    """
    from metric_pilot.reports.materialize import materialize

    _, report = completed(agent_workspace)
    svc = service(agent_workspace)
    frozen, _ = svc.frozen(report.report_id, report.revision_id)
    draft = report.draft.model_dump()
    pct_ref = draft["blocks"][1]["value_ref"]
    draft["blocks"][2].update(text_template="比例为 {{pct}}。", values={"pct": pct_ref})
    result = materialize(draft, frozen)
    assert result["blocks"][1]["value"]["display"] == result["blocks"][2]["segments"][1]["text"]
    assert result["blocks"][1]["value"]["display"] == result["blocks"][3]["rows"][0][2]["display"]
    draft["blocks"][2]["text_template"] = "比例为 {{pct}}%。"
    with pytest.raises(DataError, match="REPORT_DUPLICATE_PERCENT"):
        materialize(draft, frozen)
