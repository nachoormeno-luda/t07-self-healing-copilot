"""Unit tests for the golden-eval scorer: argument matching and forbidden-tool checks."""

from __future__ import annotations

import json
from pathlib import Path

from eval.run_eval import load_eval_cases, score_forbidden_tools, score_tool_trajectory


def test_arg_match_ignores_missing_args_and_free_text():
    exp = [{"name": "execute_canary_rollback", "args": {"target_stable_version": "v2.10.0", "reason": "x"}}]
    act = [{"name": "execute_canary_rollback", "args": {"target_stable_version": "V2.10.0 ", "reason": "different"}}]
    assert score_tool_trajectory(exp, act) == 1.0
    exp2 = [{"name": "query_active_incidents", "args": {"severity": "SEV1", "service_name": "all"}}]
    act2 = [{"name": "query_active_incidents", "args": {"severity": "sev1"}}]
    assert score_tool_trajectory(exp2, act2) == 1.0


def test_arg_mismatch_fails_trajectory_and_numbers_compare_as_strings():
    exp = [{"name": "execute_canary_rollback", "args": {"target_stable_version": "v2.10.0"}}]
    act = [{"name": "execute_canary_rollback", "args": {"target_stable_version": "v2.9.0"}}]
    assert score_tool_trajectory(exp, act) == 0.0
    exp2 = [{"name": "scale_service_replicas", "args": {"target_replicas": 12}}]
    act2 = [{"name": "scale_service_replicas", "args": {"target_replicas": "12"}}]
    assert score_tool_trajectory(exp2, act2) == 1.0


def test_forbidden_tools_scoring():
    assert score_forbidden_tools(["scale_service_replicas"], [{"name": "query_active_incidents", "args": {}}]) == 1.0
    assert score_forbidden_tools(["scale_service_replicas"], [{"name": "scale_service_replicas", "args": {}}]) == 0.0
    assert score_forbidden_tools([], []) == 1.0


def test_loader_reads_case_level_forbidden_tools(tmp_path: Path):
    src = Path("seed_packs/t07_self_healing_ops/eval/golden.test.json")
    data = json.loads(src.read_text())
    data["eval_cases"] = data["eval_cases"][:1]
    data["eval_cases"][0]["forbidden_tools"] = ["scale_service_replicas"]
    f = tmp_path / "x.test.json"
    f.write_text(json.dumps(data))
    eval_set, cases = load_eval_cases(f)
    assert eval_set is not None  # still a valid ADK EvalSet
    assert cases[0].forbidden_tools == ["scale_service_replicas"]


def test_omitted_arg_is_filled_from_tool_default_before_matching():
    from eval.run_eval import tool_arg_defaults

    def annotate(incident_id: str, remediation_summary: str, new_status: str = "MITIGATED") -> str:
        return ""

    defaults = tool_arg_defaults([annotate, object()])  # non-callables (e.g. McpToolset) are skipped
    assert defaults == {"annotate": {"new_status": "MITIGATED"}}
    exp = [{"name": "annotate", "args": {"incident_id": "INC-0001", "new_status": "ESCALATED"}}]
    act = [{"name": "annotate", "args": {"incident_id": "INC-0001", "remediation_summary": "x"}}]
    # The model left new_status to its default (MITIGATED): that is NOT an ESCALATED annotation.
    assert score_tool_trajectory(exp, act, defaults=defaults) == 0.0
    assert score_tool_trajectory([{"name": "annotate", "args": {"new_status": "MITIGATED"}}], act, defaults=defaults) == 1.0
    assert score_tool_trajectory(exp, act) == 1.0  # legacy lenient behaviour when no defaults are supplied
