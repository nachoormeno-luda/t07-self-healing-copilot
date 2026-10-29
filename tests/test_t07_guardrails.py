"""Offline tests for the Table 7 blast-radius guardrails (ADK before_tool_callback)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seed_packs.t07_self_healing_ops import guardrails, mcp_server
from seed_packs.t07_self_healing_ops.guardrails import before_tool_callback


class _Tool:
    """Stand-in for an ADK BaseTool / McpTool: the callback only reads `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture(autouse=True)
def _offline_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("SEED_OFFLINE_MODE", "1")
    monkeypatch.setattr(mcp_server, "_DEFAULT_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mcp_server, "_FALLBACK_STATE_PATH", tmp_path / "fallback.json")
    return tmp_path


def _rb(**kw):
    base = {
        "incident_id": "INC-0007",
        "service_name": "auth-token-gateway",
        "target_stable_version": "v2.16.0",
        "reason": "canary 72%",
    }
    return {**base, **kw}


def test_rollback_to_previous_stable_is_allowed():
    assert before_tool_callback(_Tool("execute_canary_rollback"), _rb(), None) is None


def test_rollback_to_unlisted_version_is_rejected_and_audited(_offline_state: Path):
    res = before_tool_callback(
        _Tool("execute_canary_rollback"),
        _rb(incident_id="INC-0001", service_name="checkout-payment-svc", target_stable_version="v2.9.0"),
        None,
    )
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and "v2.10.0" in res["reason"]
    assert res["guardrail_event_id"].startswith("GUARD-")
    state = json.loads((_offline_state / "state.json").read_text())
    assert state["guardrail_events"][0]["tool"] == "execute_canary_rollback"


def test_rollback_service_mismatch_is_rejected():
    res = before_tool_callback(
        _Tool("execute_canary_rollback"),
        _rb(incident_id="INC-0019", service_name="auth-token-gateway", target_stable_version="v2.16.0"),
        None,
    )
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and "realtime-pricing-engine" in res["reason"]


def test_rollback_unknown_incident_is_rejected():
    res = before_tool_callback(_Tool("execute_canary_rollback"), _rb(incident_id="INC-9999"), None)
    assert res["status"] == "REJECTED_BY_GUARDRAIL"


@pytest.mark.parametrize("replicas", [12, "12", 12.0, 3, 40])
def test_scale_inside_corridor_is_allowed(replicas):
    args = {"service_name": "auth-token-gateway", "cluster_region": "europe-west1", "target_replicas": replicas}
    assert before_tool_callback(_Tool("scale_service_replicas"), args, None) is None


@pytest.mark.parametrize(
    "service,replicas",
    [("auth-token-gateway", 0), ("auth-token-gateway", 41), ("auth-token-gateway", "abc"), ("ghost-svc", 12)],
)
def test_scale_outside_policy_is_rejected(service, replicas):
    args = {"service_name": service, "cluster_region": "europe-west1", "target_replicas": replicas}
    res = before_tool_callback(_Tool("scale_service_replicas"), args, None)
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and res["tool"] == "scale_service_replicas"


def test_non_guarded_tool_passes_through():
    assert before_tool_callback(_Tool("query_active_incidents"), {"severity": "SEV1"}, None) is None


def test_guardrail_fails_closed_on_internal_error(monkeypatch: pytest.MonkeyPatch):
    def boom(*_a, **_k):
        raise RuntimeError("bq exploded")

    monkeypatch.setattr(guardrails, "correlate_incident", boom)
    res = before_tool_callback(_Tool("execute_canary_rollback"), _rb(), None)
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and "guardrail error" in res["reason"]


def test_loader_and_agent_attach_guardrail_callback():
    from app.agent import build_agent
    from app.loader import load_seed_pack

    pack = load_seed_pack("t07", enable_mcp_stdio=False)
    assert pack.before_tool_callback is guardrails.before_tool_callback
    agent, _ = build_agent("t07", enable_mcp_stdio=False)
    assert agent.before_tool_callback is not None
    assert load_seed_pack("t00_example", enable_mcp_stdio=False).before_tool_callback is None


# ---------------------------------------------------------------------------
# Review fix pass: cross-incident / injection-flagged rollbacks, arg normalisation, audit logging
# ---------------------------------------------------------------------------


def test_rollback_citing_a_closed_incident_is_rejected():
    from seed_packs.t07_self_healing_ops.tools import correlate_incident

    allowed = json.loads(correlate_incident("INC-0036"))["policy"]["allowed_rollback_target"]  # MITIGATED incident
    res = before_tool_callback(
        _Tool("execute_canary_rollback"),
        {"incident_id": "INC-0036", "service_name": "checkout-payment-svc", "target_stable_version": allowed, "reason": "x"},
        None,
    )
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and "MITIGATED" in res["reason"]


def test_rollback_on_injection_flagged_incident_is_rejected():
    from seed_packs.t07_self_healing_ops.tools import correlate_incident

    allowed = json.loads(correlate_incident("INC-0019"))["policy"]["allowed_rollback_target"]
    res = before_tool_callback(
        _Tool("execute_canary_rollback"),
        {"incident_id": "INC-0019", "service_name": "realtime-pricing-engine", "target_stable_version": allowed, "reason": "x"},
        None,
    )
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and "injection" in res["reason"].lower()


def test_scale_normalises_target_replicas_in_place():
    args = {"service_name": "auth-token-gateway", "cluster_region": "europe-west1", "target_replicas": "12.0"}
    assert before_tool_callback(_Tool("scale_service_replicas"), args, None) is None
    assert args["target_replicas"] == 12 and isinstance(args["target_replicas"], int)


def test_guardrail_rejection_is_logged_for_cloud_logging(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        res = before_tool_callback(
            _Tool("scale_service_replicas"),
            {"service_name": "auth-token-gateway", "cluster_region": "europe-west1", "target_replicas": 0},
            None,
        )
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("GUARDRAIL_REJECTED" in m and res["guardrail_event_id"] in m for m in messages), messages


# ---------------------------------------------------------------------------
# Minor fix pass: corridor over all runbooks, informative fail-closed reason
# ---------------------------------------------------------------------------


def test_scale_corridor_uses_all_runbooks_not_the_lookup_limit(monkeypatch: pytest.MonkeyPatch):
    from seed_packs.t07_self_healing_ops import tools as t07tools

    calls: list[str] = []
    real = t07tools.service_replica_corridor

    def spy(service: str):
        calls.append(service)
        return real(service)

    monkeypatch.setattr(guardrails, "service_replica_corridor", spy)
    args = {"service_name": "auth-token-gateway", "cluster_region": "europe-west1", "target_replicas": 12}
    assert before_tool_callback(_Tool("scale_service_replicas"), args, None) is None
    assert calls == ["auth-token-gateway"]
    assert real("auth-token-gateway") == {"min": 3, "max": 40}
    assert real("ghost-svc") is None


def test_fail_closed_reason_includes_upstream_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        guardrails, "correlate_incident", lambda _id: json.dumps({"status": "ERROR", "error": "bq exploded"})
    )
    res = before_tool_callback(_Tool("execute_canary_rollback"), _rb(), None)
    assert res["status"] == "REJECTED_BY_GUARDRAIL" and "bq exploded" in res["reason"]
