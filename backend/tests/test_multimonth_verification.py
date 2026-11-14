"""6.E验收器反例：引用一致不等于正确分母，缺月和未审核指标不得通过。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def verifier(monkeypatch):
    """作用：加载独立验收脚本；输入：路径替身；输出：验收模块。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import verify_multimonth_investigation

    return verify_multimonth_investigation


@pytest.fixture
def sample():
    """作用：构造含取消/备降/缺失的明确合成反例；输入：无；输出：基线与报告。"""
    reference = {
        "daily_carrier": {
            "2026-01-31|ZZ": [2, 1, 1, 2, 1, 0],
            "2026-02-01|ZZ": [1, 0, 0, 0, 0, 0],
            "2026-02-01|YY": [3, 1, 0, 1, 0, 1],
        }
    }
    selected = {"start": "2026-01-01", "end_exclusive": "2026-03-01"}
    names = ["month", "record_count", "cancelled_count", "cancellation_rate"]
    rows = [["2026-01", 2, 1, 50.0], ["2026-02", 4, 1, 25.0]]
    block = {
        "type": "table",
        "id": "month",
        "rows": [
            [
                {"raw": value, "value_ref": {"artifact_id": "artifact", "row": i, "column": name}}
                for name, value in zip(names, row, strict=True)
            ]
            for i, row in enumerate(rows)
        ],
    }
    case = {
        "run": {"id": "new", "status": "completed", "seq": 0, "date_range": selected},
        "findings": [{}],
        "events": [],
        "report": {
            "date_range": selected,
            "blocks": [block],
            "draft": {"metric_revision_ids": ["approved"]},
        },
        "artifacts": {
            "artifact": {
                "artifact": {
                    "run_id": "new",
                    "date_range": selected,
                    "truncated": False,
                    "columns": [{"name": n} for n in names],
                    "query_purpose": "reviewed_metric",
                    "metric_revision_ids": ["approved"],
                },
                "rows": rows,
            }
        },
    }
    return reference, case


def test_independent_groups_include_cancelled_diverted_and_missing(verifier, sample):
    """作用：核对分母包含取消/备降/缺失，窄范围按原日期筛选；输入：合成数据；输出：断言。"""
    reference, case = sample
    assert verifier.check_case(None, case, reference)["frozen_value_refs"] == 8
    narrow = verifier.groups(
        reference, {"start": "2026-02-01", "end_exclusive": "2026-02-02"}, ["carrier_code"]
    )
    assert narrow[("YY",)]["record_count"] == 3
    assert narrow[("YY",)]["missing_scheduled_elapsed"] == 1
    assert narrow[("ZZ",)]["cancellation_rate"] == 0


def test_coherently_wrong_frozen_rate_is_rejected(verifier, sample):
    """作用：相同错误同时进入冻结值与引用仍须被独立分母识别；输入：合成报告；输出：拒绝。"""
    reference, case = copy.deepcopy(sample)
    case["artifacts"]["artifact"]["rows"][0][3] = 100.0
    case["report"]["blocks"][0]["rows"][0][3]["raw"] = 100.0
    with pytest.raises(RuntimeError, match="INDEPENDENT_VALUE_MISMATCH"):
        verifier.check_case(None, case, reference)


def test_omitted_month_is_not_treated_as_complete(verifier, sample):
    """作用：正确的单行结果不能冒充完整跨月核查；输入：删月报告；输出：拒绝。"""
    reference, case = copy.deepcopy(sample)
    case["artifacts"]["artifact"]["rows"].pop()
    case["report"]["blocks"][0]["rows"].pop()
    with pytest.raises(RuntimeError, match="INCOMPLETE_DATE_GROUPS"):
        verifier.check_case(None, case, reference)


def test_exploration_cannot_masquerade_as_reviewed_metric(verifier, sample):
    """作用：探索产物即使数值正确也不能冒充正式指标；输入：错误用途；输出：拒绝。"""
    reference, case = copy.deepcopy(sample)
    case["artifacts"]["artifact"]["artifact"]["query_purpose"] = "exploratory"
    with pytest.raises(RuntimeError, match="FORMAL_METRIC_BINDING_REQUIRED"):
        verifier.check_case(None, case, reference)


def test_old_evidence_and_range_are_checked(verifier, sample):
    """作用：追问须真引用旧产物且范围一致；输入：合成新旧产物；输出：正反例断言。"""
    reference, case = copy.deepcopy(sample)
    with pytest.raises(RuntimeError, match="OLD_EVIDENCE_NOT_REFERENCED"):
        verifier.check_case(None, case, reference, True)
    case["artifacts"]["artifact"]["artifact"]["run_id"] = "old"
    assert verifier.check_case(None, case, reference, True)["old_value_refs"] == 8
    case["artifacts"]["artifact"]["artifact"]["date_range"] = {
        "start": "2026-02-01",
        "end_exclusive": "2026-03-01",
    }
    with pytest.raises(RuntimeError, match="ARTIFACT_RANGE_OR_TRUNCATION"):
        verifier.check_case(None, case, reference, True)


def test_rounding_and_numeric_month_are_explicit(verifier, sample):
    """作用：只按声明的最终ROUND验算，月份数字须能唯一还原年份；输入：合成数据；输出：断言。"""
    reference, case = copy.deepcopy(sample)
    for index, row in enumerate(case["artifacts"]["artifact"]["rows"]):
        row[0] = index + 1
        case["report"]["blocks"][0]["rows"][index][0]["raw"] = index + 1
    assert verifier.check_case(None, case, reference)["passed"]
    assert (
        verifier.output_precision(
            "SELECT ROUND(SUM(cancelled)*100.0/COUNT(*),2) AS rate FROM v_flights", "rate"
        )
        == 2
    )
    assert (
        verifier.output_precision(
            "SELECT 100*ROUND(SUM(cancelled)*1.0/COUNT(*),2) AS rate FROM v_flights", "rate"
        )
        is None
    )


def test_tight_context_keeps_history_page_cursor_and_full_pairs():
    """作用：紧预算保留可继续读取的历史概览和完整工具配对，不改审计；输入：合成批次；输出：断言。"""
    from metric_pilot.agent.context import compact_group, fit_context, model_message_bytes

    registry = SimpleNamespace(
        artifact_ids=set(), resolved=set(), definitions={}, run={"investigation_id": "inv"}
    )
    findings = [
        {
            "id": f"finding_{i}",
            "run_id": "old",
            "source_run_status": "completed",
            "kind": "fact",
            "check": "核对记录分布",
            "text": "已核实观察。" * 100,
            "artifact_ids": ["qa_old"],
            "value_refs": {},
            "supersedes": None,
        }
        for i in range(5)
    ]
    group = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "history",
                    "type": "function",
                    "function": {
                        "name": "read_investigation",
                        "arguments": '{"offset":7,"limit":5}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "history",
            "content": json.dumps(
                {
                    "ok": True,
                    "data": {
                        "findings": findings,
                        "finding_count": 20,
                        "offset": 7,
                        "next_offset": 12,
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    original = copy.deepcopy(group)
    header = [{"role": "system", "content": "保持固定范围和权限，不展示内部思维链。"}]
    full = fit_context(header, [group], registry, 100000)
    bound = model_message_bytes(full) - 100
    bounded = fit_context(header, [group], registry, bound)
    assert model_message_bytes(bounded) <= bound
    assert group == original
    reply = json.loads(next(m["content"] for m in bounded if m.get("tool_call_id") == "history"))[
        "data"
    ]
    assert len(reply["findings"]) == 1
    assert reply["next_offset"] == 8 and reply["finding_count"] == 20
    assert reply["context_findings_omitted"] == 4
    assert reply["findings"][0]["source_run_status"] == "completed"
    assert "原始回复" in reply["context_note"]
    compact = compact_group(group, registry, brief=True)
    assert compact[0]["tool_calls"][0]["id"] == compact[1]["tool_call_id"]


def test_context_result_excerpt_has_real_cursor_and_is_idempotent():
    """
    作用：多行产物节选可真实续读，反复精简不丢标记，也不把上下文节选伪称SQL截断
    输入：明确合成的20行页和50行固定产物
    输出：原始数据不变、游标与配对保留
    """
    from metric_pilot.agent.context import compact_group

    group = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {"name": "read_artifact", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "read",
            "content": json.dumps(
                {
                    "ok": True,
                    "data": {
                        "artifact": {
                            "id": "qa_synthetic",
                            "run_id": "old",
                            "columns": [{"name": "n", "type": "BIGINT"}],
                            "returned_rows": 50,
                            "truncated": False,
                        },
                        "rows": [[n] for n in range(5, 25)],
                        "offset": 5,
                        "next_offset": 25,
                    },
                }
            ),
        },
    ]
    original = copy.deepcopy(group)
    first = compact_group(group, brief=True)
    second = compact_group(first, brief=True)
    assert first == second and group == original
    data = json.loads(first[1]["content"])["data"]
    assert data["indexed_rows"] == [[5, [5]]] and data["next_offset"] == 6
    assert data["context_rows_omitted"] == 19
    assert data["artifact"]["returned_rows"] == 50 and not data["artifact"]["truncated"]
    assert first[0]["tool_calls"][0]["id"] == first[1]["tool_call_id"]


def test_recoverable_cache_cannot_displace_latest_real_result():
    """
    作用：固定预算下缓存摘要膨胀时，保留真实最新回复、范围、指标及授权ID
    输入：合成可重读缓存和单个实际工具配对
    输出：缓存让位且不修改原始消息或增加预算
    """
    from metric_pilot.agent.context import fit_context, model_message_bytes

    registry = SimpleNamespace(
        artifact_ids={"qa_synthetic"},
        resolved={"metric_synthetic"},
        definitions={"metric_synthetic": {"denominator": "all records"}},
        run={"investigation_id": "inv_synthetic"},
    )
    header = [{"role": "system", "content": "Fixed range 2026-01-01/2026-05-01"}]
    group = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "lookup",
                    "type": "function",
                    "function": {"name": "list_tables", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "lookup",
            "content": json.dumps({"ok": True, "data": {"tables": ["v_flights"]}}),
        },
    ]
    original = copy.deepcopy(group)
    bound = model_message_bytes(fit_context(header, [group], registry, 100000)) + 500
    registry.query_summaries = [{"artifact_id": "qa_synthetic", "columns": ["x" * 2000]}]
    registry.source_summaries = {
        "chunk_synthetic": {
            "chunk_id": "chunk_synthetic",
            "source_kind": "project_policy",
            "excerpt": "y" * 2000,
        }
    }
    messages = fit_context(header, [group], registry, bound)
    assert model_message_bytes(messages) <= bound and group == original
    assert messages[0] == header[0]
    memory = json.loads(messages[1]["content"])
    assert memory["current_artifact_ids"] == ["qa_synthetic"]
    assert memory["resolved_metric_revision_ids"] == ["metric_synthetic"]
    assert memory["resolved_metric_definitions"] == registry.definitions
    assert memory["recent_successful_queries"] == []
    assert memory["cached_source_excerpts"] == []
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"]
    assert json.loads(messages[-1]["content"])["data"]["tables"] == ["v_flights"]
