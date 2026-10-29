"""Offline tests for the Table 7 Seed Pack tools (correlate_incident JOIN tool, untrusted-log flagging,
notify_travel_concierge A2A advisory)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from seed_packs.t07_self_healing_ops import mcp_server
from seed_packs.t07_self_healing_ops import tools as t07tools
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


def test_tools_are_registered_in_order():
    assert [t.__name__ for t in TOOLS] == [
        "query_active_incidents",
        "correlate_incident",
        "inspect_deployment_release",
        "lookup_slo_runbook",
        "notify_travel_concierge",
    ]


# ---------------------------------------------------------------------------
# notify_travel_concierge — A2A advisory to Table 8 (Task 8)
# ---------------------------------------------------------------------------


@pytest.fixture
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(mcp_server, "_DEFAULT_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mcp_server, "_FALLBACK_STATE_PATH", tmp_path / "fb.json")
    return tmp_path


def test_notify_sends_advisory_and_returns_reply(monkeypatch: pytest.MonkeyPatch, _state: Path):
    sent: dict[str, str] = {}

    async def fake_send(agent_url, text, *, timeout_s=30.0, httpx_client=None):
        sent["url"], sent["text"] = agent_url, text
        return "ack: rebooking 3 travellers"

    monkeypatch.setattr(t07tools, "send_a2a_text", fake_send)
    monkeypatch.setenv("T08_AGENT_URL", "http://t08.test")
    out = json.loads(
        asyncio.run(t07tools.notify_travel_concierge("INC-0001", "checkout-payment-svc", "payment checkouts timing out"))
    )
    assert out["status"] == "SUCCESS" and out["concierge_reply"].startswith("ack")
    assert out["advisory"]["advisory_id"] == "ADV-4001" and out["advisory"]["advisory_level"] == "DEGRADED"
    assert sent["url"] == "http://t08.test" and "incident_id=INC-0001" in sent["text"]
    state = json.loads((_state / "state.json").read_text())
    assert state["advisories"][0]["advisory_id"] == "ADV-4001"


def test_notify_skips_without_endpoint_and_never_raises(monkeypatch: pytest.MonkeyPatch, _state: Path):
    monkeypatch.delenv("T08_AGENT_URL", raising=False)
    out = json.loads(asyncio.run(t07tools.notify_travel_concierge("INC-0001", "checkout-payment-svc", "x")))
    assert out["status"] == "SKIPPED" and out["advisory"]["advisory_id"] == "ADV-4001"

    async def boom(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(t07tools, "send_a2a_text", boom)
    monkeypatch.setenv("T08_AGENT_URL", "http://t08.test")
    out2 = json.loads(asyncio.run(t07tools.notify_travel_concierge("INC-0001", "checkout-payment-svc", "x")))
    assert out2["status"] == "ERROR" and "refused" in out2["error"]


def test_notify_reports_timeouts_by_type_and_uses_a_realistic_timeout(monkeypatch: pytest.MonkeyPatch, _state: Path):
    seen: dict[str, float] = {}

    async def slow(agent_url, text, *, timeout_s=30.0, httpx_client=None):
        seen["timeout_s"] = timeout_s
        raise asyncio.TimeoutError()  # str() of this exception is empty

    monkeypatch.setattr(t07tools, "send_a2a_text", slow)
    monkeypatch.setenv("T08_AGENT_URL", "http://t08.test")
    out = json.loads(asyncio.run(t07tools.notify_travel_concierge("INC-0001", "checkout-payment-svc", "x")))
    assert out["status"] == "ERROR" and out["error"].startswith("TimeoutError")
    assert seen["timeout_s"] >= 60  # a real agent round trip (Gemini + BigQuery) takes well over 20 s


def test_correlate_incident_requires_incident_id():
    import inspect

    param = inspect.signature(correlate_incident).parameters["incident_id"]
    assert param.default is inspect.Parameter.empty  # a default would let the model silently triage INC-0001


def test_notify_records_outcome_on_the_advisory(monkeypatch: pytest.MonkeyPatch, _state: Path):
    monkeypatch.delenv("T08_AGENT_URL", raising=False)
    asyncio.run(t07tools.notify_travel_concierge("INC-0001", "checkout-payment-svc", "x"))
    assert json.loads((_state / "state.json").read_text())["advisories"][0]["status"] == "SKIPPED"

    async def ok(*_a, **_k):
        return "ack"

    monkeypatch.setattr(t07tools, "send_a2a_text", ok)
    monkeypatch.setenv("T08_AGENT_URL", "http://t08.test")
    asyncio.run(t07tools.notify_travel_concierge("INC-0001", "checkout-payment-svc", "x"))
    assert json.loads((_state / "state.json").read_text())["advisories"][1]["status"] == "SENT"


def test_split_joined_bigquery_row_and_policy_types():
    from seed_packs.t07_self_healing_ops.tools import _build_policy, _split_joined_row

    row = {f"i_{c}": None for c in t07tools._INCIDENT_COLUMNS}
    row.update({"i_incident_id": "INC-0007", "i_service_name": "auth-token-gateway", "i_status": "OPEN"})
    row.update({f"r_{c}": None for c in t07tools._RELEASE_COLUMNS})
    row.update({"r_release_id": "REL-0007", "r_previous_stable_version": "v2.16.0", "r_rollback_safe": True})
    row.update({f"b_{c}": None for c in t07tools._RUNBOOK_COLUMNS})
    row.update({"b_runbook_id": "RBK-017", "b_min_safe_replicas": 3, "b_max_safe_replicas": 40,
                "b_recommended_action": "ROLLBACK_TO_PREVIOUS_STABLE"})
    incident, release, runbook = _split_joined_row(row)
    assert (incident["incident_id"], release["release_id"], runbook["runbook_id"]) == ("INC-0007", "REL-0007", "RBK-017")
    assert _build_policy(release, runbook) == {
        "allowed_rollback_target": "v2.16.0",
        "rollback_safe": True,  # BigQuery BOOL, not the CSV "True" string
        "replica_corridor": {"min": 3, "max": 40},
        "recommended_action": "ROLLBACK_TO_PREVIOUS_STABLE",
    }
    assert _split_joined_row({f"i_{c}": None for c in t07tools._INCIDENT_COLUMNS})[1:] == (None, None)


def test_a2a_client_import_fallback_logs_a_warning(monkeypatch: pytest.MonkeyPatch, caplog):
    import importlib
    import logging
    import sys

    monkeypatch.setitem(sys.modules, "seed_packs.t07_self_healing_ops.a2a_client", None)  # forces ImportError
    with caplog.at_level(logging.WARNING):
        importlib.reload(t07tools)
    try:
        assert t07tools.send_a2a_text is None
        assert any("a2a" in rec.getMessage().lower() for rec in caplog.records), [r.getMessage() for r in caplog.records]
    finally:
        monkeypatch.delitem(sys.modules, "seed_packs.t07_self_healing_ops.a2a_client", raising=False)
        importlib.reload(t07tools)
    assert callable(t07tools.send_a2a_text)
