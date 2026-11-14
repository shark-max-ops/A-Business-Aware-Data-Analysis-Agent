"""声明式关系反例：排名由完整冻结结果计算，模型文字不自动取得结论身份。"""

import copy

import pytest
import test_agent
from metric_pilot.data.common import DataError
from metric_pilot.reports.claims import POLICY, render_relation
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.materialize import materialize
from pydantic import ValidationError
from test_agent import calls, final_from_query
from test_investigations import finding_call, investigation, run_steps
from test_reports import SQL, service

agent_workspace = test_agent.agent_workspace


def frozen(rows=None):
    """
    作用：提供明确合成、非真实业务证据的完整结果边界
    输入：可选行值
    输出：最小冻结上下文
    """
    rows = (
        rows
        if rows is not None
        else [
            ["Jan", "OH", 11.6],
            ["Jan", "YX", 8.18],
            ["Mar", "NK", 10.38],
            ["Jan", "AA", 8.93],
            ["Apr", "AS", 0.43],
            ["Apr", "B6", 0.10],
        ]
    )
    return {
        "verification_policy": POLICY,
        "date_range": {"start": "2026-01-01", "end_exclusive": "2026-05-01"},
        "artifacts": {
            "qa_test": {
                "artifact": {
                    "columns": [
                        {"name": "month", "type": "VARCHAR"},
                        {"name": "carrier_code", "type": "VARCHAR"},
                        {"name": "rate_pct", "type": "DOUBLE"},
                    ],
                    "result_hash": "synthetic_hash",
                    "returned_rows": len(rows),
                    "truncated": False,
                    "metric_revision_ids": [],
                    "ordered": True,
                },
                "rows": rows,
            }
        },
        "metrics": {},
        "sources": {},
    }


def ranking(**changes):
    """作用：声明排序请求；输入：字段覆盖；输出：无模型名次或标签的请求。"""
    return {
        "type": "ranking",
        "id": "rank",
        "artifact_id": "qa_test",
        "dimensions": ["month", "carrier_code"],
        "measure": "rate_pct",
        "direction": "descending",
        "top_k": 3,
        **changes,
    }


def test_wrong_third_place_and_wrong_minimum_are_computed_from_all_rows():
    """
    作用：重现YX冒充第三、AS冒充最低的真实错误模式
    输入：明确合成且故意未按取消比例排序的完整行
    输出：无
    """
    data = frozen()
    top = render_relation(ranking(), data["artifacts"])
    assert [entry["row"] for entry in top["proof"]["ranked_rows"]] == [0, 2, 3]
    low = render_relation(ranking(direction="ascending", top_k=1), data["artifacts"])
    assert low["proof"]["ranked_rows"] == [{"row": 5, "rank": 1}]
    assert top["verification"] == low["verification"] == "checked_relation"
    assert len(top["proof"]["proof_hash"]) == 64


def test_ties_nulls_and_empty_results_do_not_invent_order_or_zero():
    """
    作用：完整保留临界名次并列，不把空值或空集合补零
    输入：合成并列、缺失和空结果
    输出：无
    """
    data = frozen([["Jan", "A", 5], ["Jan", "B", 3], ["Jan", "C", 3], ["Jan", "D", None]])
    result = render_relation(ranking(top_k=2), data["artifacts"])
    assert result["proof"]["ranked_rows"] == [
        {"row": 0, "rank": 1},
        {"row": 1, "rank": 2},
        {"row": 2, "rank": 2},
    ]
    assert result["proof"]["null_rows"] == 1
    empty = render_relation(ranking(), frozen([])["artifacts"])
    assert not empty["proof"]["ranked_rows"]


@pytest.mark.parametrize(
    "rows,relation",
    [
        ([["Jan", "A", 3], ["Feb", "A", 2]], "higher"),
        ([["Jan", "A", 2], ["Feb", "A", 3]], "lower"),
        ([["Jan", "A", 2], ["Feb", "A", 2]], "equal"),
        ([["Jan", "A", None], ["Feb", "A", 2]], "unavailable"),
    ],
)
def test_comparison_cannot_accept_model_supplied_increase_or_decrease(rows, relation):
    """
    作用：方向只由两条真实记录决定，缺测不作升降判断
    输入：合成同列数值
    输出：无
    """
    block = {
        "type": "comparison",
        "id": "compare",
        "artifact_id": "qa_test",
        "dimensions": ["month", "carrier_code"],
        "measure": "rate_pct",
        "left_row": 0,
        "right_row": 1,
    }
    result = render_relation(block, frozen(rows)["artifacts"])
    assert result["proof"]["relation"] == relation
    assert [s["value"]["value_ref"]["row"] for s in result["segments"] if s.get("value")][-1] == 1


@pytest.mark.parametrize(
    "change,code",
    [
        ({"dimensions": ["month"]}, "REPORT_CLAIM_IDENTITY"),
        ({"measure": "wrong"}, "REPORT_CLAIM_COLUMNS"),
        ({"measure": "carrier_code", "dimensions": ["month"]}, "REPORT_VALUE_TYPE"),
    ],
)
def test_incomplete_identity_or_invalid_measure_is_rejected(change, code):
    """作用：拒绝遗漏维度与伪数值列；输入：合成错误声明；输出：无。"""
    with pytest.raises(DataError) as exc:
        render_relation(ranking(**change), frozen()["artifacts"])
    assert exc.value.code == code


def test_truncated_or_incomplete_result_cannot_claim_ranking():
    """作用：不让局部结果冒充完整比较；输入：合成不完整边界；输出：无。"""
    for field, value in [("truncated", True), ("returned_rows", 100)]:
        data = frozen()
        data["artifacts"]["qa_test"]["artifact"][field] = value
        with pytest.raises(DataError, match="REPORT_CLAIM_INCOMPLETE"):
            render_relation(ranking(), data["artifacts"])


def test_model_text_is_quarantined_and_cannot_supply_verification_or_winners():
    """
    作用：引用真实数字的错误文字也不能自动进入已核对结论
    输入：真实引用外形与虚构排名的合成模板
    输出：无
    """
    block = {
        "type": "text",
        "id": "wrong",
        "text_template": "YX是第三高，取消比例{{v}}。",
        "values": {"v": {"artifact_id": "qa_test", "row": 1, "column": "rate_pct"}},
    }
    draft = {"schema_version": "1", "title": "模型标题", "scope": "模型范围", "blocks": [block]}
    result = materialize(draft, frozen())
    assert result["blocks"][0]["verification"] == "unverified"
    assert result["has_unverified_explanations"]
    for invalid in [
        {**block, "verification": "checked_relation"},
        {**ranking(), "winner_rows": [1]},
    ]:
        with pytest.raises(ValidationError):
            ReportDraft.model_validate({**draft, "blocks": [invalid]})


def test_revision_recomputes_proof_and_old_frozen_result_remains_unchanged():
    """
    作用：修改排序声明须重新计算，不沿用旧证明或覆盖原结果
    输入：合成完整冻结报告
    输出：无
    """
    draft = {"schema_version": "1", "title": "排序", "scope": "仅此集合", "blocks": [ranking()]}
    original = materialize(draft, frozen())
    snapshot = copy.deepcopy(original)
    changed = materialize({**draft, "blocks": [ranking(direction="ascending", top_k=1)]}, original)
    assert original == snapshot
    assert changed["blocks"][0]["proof"]["ranked_rows"][0]["row"] == 5
    assert (
        changed["blocks"][0]["proof"]["proof_hash"] != original["blocks"][0]["proof"]["proof_hash"]
    )


def test_real_runtime_freezes_relation_and_evidence_and_cannot_edit_proof(agent_workspace):
    """
    作用：证明真实受控SQL到报告、取证和修订均使用服务端计算结果
    输入：合成数据工作区和正式运行入口
    输出：无
    """

    def candidate(messages):
        """作用：只声明排名；输入：真实工具上下文；输出：不含名次的候选。"""
        ident = final_from_query(messages)["artifact_ids"][0]
        return {
            "kind": "report",
            "report": {
                "schema_version": "1",
                "title": "合成关系核查",
                "scope": "固定合成范围",
                "blocks": [
                    {
                        "type": "ranking",
                        "id": "actual_rank",
                        "artifact_id": ident,
                        "dimensions": ["day"],
                        "measure": "record_count",
                        "top_k": 2,
                    }
                ],
            },
        }

    run, _ = run_steps(
        agent_workspace,
        investigation(agent_workspace),
        [calls(("query_sql", {"sql": SQL})), finding_call, candidate],
    )
    assert run["status"] == "completed", run
    svc = service(agent_workspace)
    result = svc.read(run["result"]["report_id"], run["result"]["revision_id"])
    assert result.blocks[0].verification == "checked_relation"
    assert not result.has_unverified_explanations
    proof = svc.evidence(result.report_id, result.revision_id, "actual_rank").proof
    assert proof == result.blocks[0].proof
    assert proof["result_hash"]
