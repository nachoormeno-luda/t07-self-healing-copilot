"""Offline tests for the Table 7 Seed Pack tools (correlate_incident JOIN tool, untrusted-log flagging)."""

from __future__ import annotations

import json

import pytest

from seed_packs.t07_self_healing_ops.tools import (
    TOOLS,
    correlate_incident,
    flag_untrusted_text,
    query_active_incidents,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED_OFFLINE_MODE", "1")


def test_correlate_incident_joins_release_and_runbook():
    out = json.loads(correlate_incident("INC-0007"))
    assert out["status"] == "SUCCESS"
    assert out["incident"]["service_name"] == "auth-token-gateway"
    assert out["release"]["previous_stable_version"] == "v2.16.0"
    assert out["runbook"]["runbook_id"] == "RBK-017"
    assert out["policy"] == {
        "allowed_rollback_target": "v2.16.0",
        "rollback_safe": True,
        "replica_corridor": {"min": 3, "max": 40},
        "recommended_action": "ROLLBACK_TO_PREVIOUS_STABLE",
    }


def test_correlate_incident_flags_injection_and_finds_mitigated_rows():
    inj = json.loads(correlate_incident("INC-0019"))
    assert inj["incident"]["injection_suspected"] is True
    assert "recent_log_snippet" not in inj["incident"]
    assert "PROMPT INJECTION" in inj["incident"]["untrusted_log_snippet"]
    assert json.loads(correlate_incident("INC-0001"))["incident"]["injection_suspected"] is False
    mit = json.loads(correlate_incident("INC-0036"))  # first MITIGATED row in data/service_incidents.csv
    assert mit["status"] == "SUCCESS" and mit["incident"]["status"] == "MITIGATED"


def test_correlate_incident_not_found_and_never_raises():
    assert json.loads(correlate_incident("INC-9999"))["status"] == "NOT_FOUND"
    assert json.loads(correlate_incident(""))["status"] in {"NOT_FOUND", "ERROR"}


def test_query_active_incidents_marks_untrusted_snippet():
    out = json.loads(query_active_incidents("SEV1", "realtime-pricing-engine"))
    row = next(r for r in out["incidents"] if r["incident_id"] == "INC-0019")
    assert row["injection_suspected"] is True and "recent_log_snippet" not in row
    assert flag_untrusted_text("ERROR upstream timeout after 3723ms") is False


def test_correlate_incident_is_registered():
    assert [t.__name__ for t in TOOLS] == [
        "query_active_incidents",
        "correlate_incident",
        "inspect_deployment_release",
        "lookup_slo_runbook",
    ]
