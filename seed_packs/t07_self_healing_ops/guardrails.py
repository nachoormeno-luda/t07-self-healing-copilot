"""Blast-radius guardrails for Table 7, enforced in code as an ADK `before_tool_callback`.

The prompt tells Gemini what the policy is; this module makes it impossible to break:
rollbacks only to the release's `previous_stable_version` for the incident's own service,
replica scaling only inside the runbook corridor, and every refusal recorded in the
mock state so the audit trail survives whatever the model says next. Policy values come
from BigQuery rows via `correlate_incident` / `lookup_slo_runbook`, never from log text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from seed_packs.t07_self_healing_ops.mcp_server import record_guardrail_event
from seed_packs.t07_self_healing_ops.tools import correlate_incident, lookup_slo_runbook

logger = logging.getLogger(__name__)

GUARDED_TOOLS = frozenset({"execute_canary_rollback", "scale_service_replicas"})

_NEXT_STEP = (
    "Call annotate_incident_timeline with new_status='ESCALATED' quoting this reason, "
    "then report it to the operator. Do not retry with different arguments."
)


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def check_rollback(args: dict[str, Any]) -> str | None:
    """Return a rejection reason for `execute_canary_rollback` args, or None when allowed."""
    incident_id = str(args.get("incident_id") or "").strip()
    data = json.loads(correlate_incident(incident_id))
    if data.get("status") != "SUCCESS":
        return (
            f"unknown incident '{incident_id}' ({data.get('status')}): "
            "rollbacks require an incident that correlates to a release"
        )
    incident = data["incident"]
    policy = data["policy"]
    if _norm(incident.get("status")) != "open":
        # Citing a closed incident would make its release's previous version a rollback target.
        return (
            f"incident {incident_id} is {incident.get('status')}, not OPEN: only an open incident "
            "can drive a rollback"
        )
    requested_service = str(args.get("service_name") or "").strip()
    if _norm(requested_service) != _norm(incident.get("service_name")):
        return (
            f"service mismatch: {incident_id} belongs to {incident.get('service_name')}, "
            f"not {requested_service or '(empty)'}"
        )
    if incident.get("injection_suspected"):
        return (
            f"incident {incident_id} carries a suspected prompt injection in its log text; "
            "automated rollback refused, escalate for human review"
        )
    if not policy.get("rollback_safe"):
        return f"release {incident.get('active_release_id')} is not marked rollback_safe"
    allowed = policy.get("allowed_rollback_target")
    requested_version = str(args.get("target_stable_version") or "").strip()
    if not allowed or _norm(requested_version) != _norm(allowed):
        return (
            f"target version {requested_version or '(empty)'} is not allowlisted: the only permitted "
            f"rollback target for {incident_id} (release {incident.get('active_release_id')}) "
            f"is its previous stable version {allowed}"
        )
    return None


def check_scale(args: dict[str, Any]) -> str | None:
    """Return a rejection reason for `scale_service_replicas` args, or None when allowed."""
    service = str(args.get("service_name") or "").strip()
    data = json.loads(lookup_slo_runbook(service, "all"))
    runbooks = data.get("runbooks") or []
    if data.get("status") != "SUCCESS" or not runbooks:
        return f"no SLO runbook defines a replica corridor for service '{service}'"
    # Strictest corridor across the service's runbooks.
    lo = max(int(float(b["min_safe_replicas"])) for b in runbooks)
    hi = min(int(float(b["max_safe_replicas"])) for b in runbooks)
    try:
        target = int(float(args.get("target_replicas")))
    except (TypeError, ValueError):
        return f"target_replicas={args.get('target_replicas')!r} is not a number"
    if target < lo or target > hi:
        return f"target_replicas={target} is outside the runbook corridor [{lo}, {hi}] for {service}"
    # ADK hands this same dict to the tool: pass the normalised int on so "12.0" cannot crash int().
    args["target_replicas"] = target
    return None


def _json_safe(args: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
        for k, v in (args or {}).items()
    }


def before_tool_callback(tool: Any, args: dict[str, Any], tool_context: Any) -> dict[str, Any] | None:
    """ADK before_tool_callback: return None to allow the call, or a dict that replaces the tool result.

    Fails closed: any internal error while evaluating policy rejects the action.
    """
    name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
    if name not in GUARDED_TOOLS:
        return None
    try:
        reason = check_rollback(args) if name == "execute_canary_rollback" else check_scale(args)
    except Exception as exc:
        reason = f"guardrail error: {type(exc).__name__}: {exc}"
    if reason is None:
        return None
    try:
        event_id = record_guardrail_event(name, _json_safe(args), reason)["event_id"]
    except Exception as exc:  # the refusal stands even if the audit write fails
        event_id = f"GUARD-UNRECORDED ({type(exc).__name__})"
    # Structured line for Cloud Logging (Agent Runtime / Cloud Run): the audit trail the jury can see.
    logger.warning(
        "GUARDRAIL_REJECTED %s",
        json.dumps({"event_id": event_id, "tool": name, "reason": reason, "args": _json_safe(args)}),
    )
    return {
        "status": "REJECTED_BY_GUARDRAIL",
        "tool": name,
        "reason": reason,
        "guardrail_event_id": event_id,
        "next_step": _NEXT_STEP,
    }
