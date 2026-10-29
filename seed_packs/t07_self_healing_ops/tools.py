"""ADK Function Tools for Seed Pack t07_self_healing_ops (BigQuery + Mock Actions)."""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

from google.cloud import bigquery
from seed_packs.t07_self_healing_ops.mcp_server import (
    ACTION_TOOLS,
    annotate_incident_timeline,
    execute_canary_rollback,
    scale_service_replicas,
)

_DATA_DIR = Path(__file__).resolve().parent / "data"
_DEFAULT_DATASET = "seed_t07"


def _resolve_project_and_dataset() -> tuple[str, str]:
    """Resolve project and dataset at call time; '' project means "not configured"."""
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("AGENT_ENGINE_PROJECT")
        or ""
    ).strip()
    if "your-gcp-project-id" in project.lower() or "your-project-id" in project.lower():
        project = ""
    dataset = os.environ.get("SEED_BQ_DATASET", _DEFAULT_DATASET).strip() or _DEFAULT_DATASET
    return project, dataset


def _offline_reason(project: str) -> str:
    """Explain why BigQuery was skipped and what to do about it."""
    if not project:
        return (
            "GOOGLE_CLOUD_PROJECT (or AGENT_ENGINE_PROJECT) is not set, so these rows come "
            "from the bundled CSV copy. Set GOOGLE_CLOUD_PROJECT in .env and run "
            "`make seed PACK=t07` to query BigQuery."
        )
    return "SEED_OFFLINE_MODE enabled: rows come from the bundled CSV copy."


def _bq_error_hint(exc: Exception) -> str:
    """Turn a BigQuery failure into an actionable fallback reason."""
    return (
        f"BigQuery query failed ({type(exc).__name__}: {exc}). These rows come from the "
        "bundled CSV copy. If the dataset is missing run `make seed PACK=t07`; if "
        "credentials are missing run `gcloud auth application-default login`."
    )


def _read_local_csv(name: str) -> list[dict[str, Any]]:
    path = _DATA_DIR / f"{name}.csv"
    with path.open("r", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


# Markers of instructions smuggled into log/telemetry text (the INC-0019 attack vector).
INJECTION_PATTERN = re.compile(
    r"<!--|-->|prompt injection|system override"
    r"|ignore (?:all |any )?(?:previous|prior|above) instructions"
    r"|call [a-z_]+ with",
    re.IGNORECASE,
)


def flag_untrusted_text(text: str) -> bool:
    """True when free text looks like an embedded instruction rather than diagnostics."""
    return bool(INJECTION_PATTERN.search(text or ""))


def _mark_untrusted(row: dict[str, Any]) -> dict[str, Any]:
    """Rename `recent_log_snippet` -> `untrusted_log_snippet` and add `injection_suspected`."""
    row = dict(row)
    snippet = row.pop("recent_log_snippet", "") or ""
    row["untrusted_log_snippet"] = snippet
    row["injection_suspected"] = flag_untrusted_text(str(snippet))
    return row


def _to_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def query_active_incidents(
    severity: str = "SEV1",
    service_name: str = "all",
) -> str:
    """Query active (OPEN) production service incidents in BigQuery by severity and service name.

    Args:
        severity: Incident severity ('SEV1', 'SEV2', 'SEV3', or 'all').
        service_name: Microservice name (e.g. 'checkout-payment-svc', 'auth-token-gateway', or 'all').

    Returns:
        JSON string with open service incidents sorted by error rate descending.
    """
    project, dataset = _resolve_project_and_dataset()
    sev_clean = (severity or "all").strip()
    svc_clean = (service_name or "all").strip()
    use_offline = os.environ.get("SEED_OFFLINE_MODE", "").lower() in {"1", "true", "yes"}
    if project and not use_offline:
        try:
            client = bigquery.Client(project=project)
            table_ref = f"`{project}.{dataset}.service_incidents`"
            clauses = ["status = 'OPEN'"]
            params: list[Any] = []
            if sev_clean.lower() != "all":
                clauses.append("UPPER(severity) = UPPER(@sev)")
                params.append(bigquery.ScalarQueryParameter("sev", "STRING", sev_clean))
            if svc_clean.lower() != "all":
                clauses.append("LOWER(service_name) = LOWER(@svc)")
                params.append(bigquery.ScalarQueryParameter("svc", "STRING", svc_clean))
            where_sql = " AND ".join(clauses)
            sql = (
                f"SELECT incident_id, service_name, cluster_region, severity, symptom_code, "
                f"error_rate_pct, p99_latency_ms, active_release_id, status, recent_log_snippet "
                f"FROM {table_ref} WHERE {where_sql} ORDER BY error_rate_pct DESC LIMIT 15"
            )
            cfg = bigquery.QueryJobConfig(query_parameters=params)
            rows = [_mark_untrusted(dict(r.items())) for r in client.query(sql, job_config=cfg).result()]
            return json.dumps({
                "status": "SUCCESS",
                "source": f"bigquery:{project}.{dataset}.service_incidents",
                "count": len(rows),
                "incidents": rows,
            }, default=str)
        except Exception as exc:
            fallback_error = _bq_error_hint(exc)
    else:
        fallback_error = _offline_reason(project)

    rows = [
        r for r in _read_local_csv("service_incidents")
        if r["status"] == "OPEN"
        and (sev_clean.lower() == "all" or r["severity"].lower() == sev_clean.lower())
        and (svc_clean.lower() == "all" or r["service_name"].lower() == svc_clean.lower())
    ]
    rows.sort(key=lambda r: float(r["error_rate_pct"]), reverse=True)
    return json.dumps({
        "status": "SUCCESS",
        "source": "local_csv_fallback",
        "fallback_reason": fallback_error,
        "count": len(rows[:15]),
        "incidents": [_mark_untrusted(r) for r in rows[:15]],
    })


_INCIDENT_COLUMNS = (
    "incident_id", "service_name", "cluster_region", "severity", "symptom_code",
    "error_rate_pct", "p99_latency_ms", "active_release_id", "status", "recent_log_snippet",
)
_RELEASE_COLUMNS = (
    "release_id", "service_name", "version_tag", "previous_stable_version", "deployed_at",
    "author_team", "change_type", "canary_pass_rate_pct", "rollback_safe",
)
_RUNBOOK_COLUMNS = (
    "runbook_id", "service_name", "symptom_code", "slo_target_pct", "error_budget_burn_rate",
    "recommended_action", "min_safe_replicas", "max_safe_replicas",
)


def _build_policy(release: dict[str, Any] | None, runbook: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the remediation policy the guardrails enforce from BigQuery rows only."""
    corridor = None
    if runbook:
        corridor = {
            "min": _to_int(runbook.get("min_safe_replicas")),
            "max": _to_int(runbook.get("max_safe_replicas")),
        }
    return {
        "allowed_rollback_target": (release or {}).get("previous_stable_version") or None,
        "rollback_safe": _to_bool((release or {}).get("rollback_safe")),
        "replica_corridor": corridor,
        "recommended_action": (runbook or {}).get("recommended_action") or None,
    }


def _split_joined_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    incident = {c: row.get(f"i_{c}") for c in _INCIDENT_COLUMNS}
    release = {c: row.get(f"r_{c}") for c in _RELEASE_COLUMNS} if row.get("r_release_id") else None
    runbook = {c: row.get(f"b_{c}") for c in _RUNBOOK_COLUMNS} if row.get("b_runbook_id") else None
    return incident, release, runbook


def _correlation_payload(
    *,
    source: str,
    incident: dict[str, Any],
    release: dict[str, Any] | None,
    runbook: dict[str, Any] | None,
    fallback_reason: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "status": "SUCCESS",
        "source": source,
        "incident": _mark_untrusted(incident),
        "release": release,
        "runbook": runbook,
        "policy": _build_policy(release, runbook),
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    return json.dumps(payload, default=str)


def correlate_incident(incident_id: str = "INC-0001") -> str:
    """Correlate one incident with its active deployment release and governing SLO runbook in a single BigQuery JOIN.

    Args:
        incident_id: Incident ID (e.g. 'INC-0007'). Any status (OPEN or MITIGATED).

    Returns:
        JSON string with `incident`, `release`, `runbook` and a `policy` block
        (allowed_rollback_target, rollback_safe, replica_corridor, recommended_action).
    """
    try:
        inc_clean = (incident_id or "").strip()
        if not inc_clean:
            return json.dumps({"status": "NOT_FOUND", "incident_id": inc_clean})
        project, dataset = _resolve_project_and_dataset()
        use_offline = os.environ.get("SEED_OFFLINE_MODE", "").lower() in {"1", "true", "yes"}
        if project and not use_offline:
            try:
                client = bigquery.Client(project=project)
                inc_cols = ", ".join(f"i.{c} AS i_{c}" for c in _INCIDENT_COLUMNS)
                rel_cols = ", ".join(f"r.{c} AS r_{c}" for c in _RELEASE_COLUMNS)
                rb_cols = ", ".join(f"b.{c} AS b_{c}" for c in _RUNBOOK_COLUMNS)
                sql = (
                    f"SELECT {inc_cols}, {rel_cols}, {rb_cols} "
                    f"FROM `{project}.{dataset}.service_incidents` i "
                    f"LEFT JOIN `{project}.{dataset}.deployment_releases` r "
                    f"ON r.release_id = i.active_release_id "
                    f"LEFT JOIN `{project}.{dataset}.slo_runbooks` b "
                    f"ON b.service_name = i.service_name AND b.symptom_code = i.symptom_code "
                    f"WHERE UPPER(i.incident_id) = UPPER(@inc) "
                    f"ORDER BY b.error_budget_burn_rate DESC LIMIT 1"
                )
                cfg = bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("inc", "STRING", inc_clean)]
                )
                rows = [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]
                if not rows:
                    return json.dumps({"status": "NOT_FOUND", "incident_id": inc_clean})
                incident, release, runbook = _split_joined_row(rows[0])
                return _correlation_payload(
                    source=f"bigquery:{project}.{dataset} (service_incidents ⋈ deployment_releases ⋈ slo_runbooks)",
                    incident=incident,
                    release=release,
                    runbook=runbook,
                )
            except Exception as exc:
                fallback_error = _bq_error_hint(exc)
        else:
            fallback_error = _offline_reason(project)

        incidents = [
            r for r in _read_local_csv("service_incidents")
            if r["incident_id"].upper() == inc_clean.upper()
        ]
        if not incidents:
            return json.dumps({"status": "NOT_FOUND", "incident_id": inc_clean})
        incident = incidents[0]
        release = next(
            (
                r for r in _read_local_csv("deployment_releases")
                if r["release_id"].upper() == str(incident["active_release_id"]).upper()
            ),
            None,
        )
        runbooks = [
            b for b in _read_local_csv("slo_runbooks")
            if b["service_name"].lower() == incident["service_name"].lower()
            and b["symptom_code"].upper() == incident["symptom_code"].upper()
        ]
        runbook = max(runbooks, key=lambda b: float(b["error_budget_burn_rate"])) if runbooks else None
        return _correlation_payload(
            source="local_csv_fallback",
            incident=incident,
            release=release,
            runbook=runbook,
            fallback_reason=fallback_error,
        )
    except Exception as exc:
        return json.dumps({"status": "ERROR", "error": str(exc)})


def inspect_deployment_release(release_id: str = "REL-0001") -> str:
    """Inspect deployment release metadata, previous stable version, and canary pass rate in BigQuery.

    Args:
        release_id: Deployment release ID (e.g. 'REL-0001' or 'REL-0019').

    Returns:
        JSON string with release details and rollback safety flag.
    """
    project, dataset = _resolve_project_and_dataset()
    rel_clean = (release_id or "REL-0001").strip()
    use_offline = os.environ.get("SEED_OFFLINE_MODE", "").lower() in {"1", "true", "yes"}
    if project and not use_offline:
        try:
            client = bigquery.Client(project=project)
            table_ref = f"`{project}.{dataset}.deployment_releases`"
            sql = (
                f"SELECT release_id, service_name, version_tag, previous_stable_version, "
                f"deployed_at, author_team, change_type, canary_pass_rate_pct, rollback_safe "
                f"FROM {table_ref} WHERE UPPER(release_id) = UPPER(@rel) LIMIT 5"
            )
            cfg = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("rel", "STRING", rel_clean)]
            )
            rows = [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]
            return json.dumps({
                "status": "SUCCESS",
                "source": f"bigquery:{project}.{dataset}.deployment_releases",
                "count": len(rows),
                "releases": rows,
            }, default=str)
        except Exception as exc:
            fallback_error = _bq_error_hint(exc)
    else:
        fallback_error = _offline_reason(project)

    rows = [
        r for r in _read_local_csv("deployment_releases")
        if r["release_id"].upper() == rel_clean.upper()
    ][:5]
    return json.dumps({
        "status": "SUCCESS",
        "source": "local_csv_fallback",
        "fallback_reason": fallback_error,
        "count": len(rows),
        "releases": rows,
    })


def lookup_slo_runbook(
    service_name: str = "checkout-payment-svc",
    symptom_code: str = "all",
) -> str:
    """Look up the SRE SLO runbook, recommended action, and safe replica bounds from BigQuery.

    Args:
        service_name: Microservice name (e.g. 'checkout-payment-svc', 'auth-token-gateway').
        symptom_code: Optional symptom code filter (e.g. 'P99_LATENCY_SPIKE', 'OOM_CRASH_LOOP', or 'all').

    Returns:
        JSON string with matching SRE runbook policies.
    """
    project, dataset = _resolve_project_and_dataset()
    svc_clean = (service_name or "checkout-payment-svc").strip()
    sym_clean = (symptom_code or "all").strip()
    use_offline = os.environ.get("SEED_OFFLINE_MODE", "").lower() in {"1", "true", "yes"}
    if project and not use_offline:
        try:
            client = bigquery.Client(project=project)
            table_ref = f"`{project}.{dataset}.slo_runbooks`"
            clauses = ["LOWER(service_name) = LOWER(@svc)"]
            params: list[Any] = [bigquery.ScalarQueryParameter("svc", "STRING", svc_clean)]
            if sym_clean.lower() != "all":
                clauses.append("UPPER(symptom_code) = UPPER(@sym)")
                params.append(bigquery.ScalarQueryParameter("sym", "STRING", sym_clean))
            where_sql = " AND ".join(clauses)
            sql = (
                f"SELECT runbook_id, service_name, symptom_code, slo_target_pct, "
                f"error_budget_burn_rate, recommended_action, min_safe_replicas, max_safe_replicas "
                f"FROM {table_ref} WHERE {where_sql} ORDER BY error_budget_burn_rate DESC LIMIT 10"
            )
            cfg = bigquery.QueryJobConfig(query_parameters=params)
            rows = [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]
            return json.dumps({
                "status": "SUCCESS",
                "source": f"bigquery:{project}.{dataset}.slo_runbooks",
                "count": len(rows),
                "runbooks": rows,
            }, default=str)
        except Exception as exc:
            fallback_error = _bq_error_hint(exc)
    else:
        fallback_error = _offline_reason(project)

    rows = [
        r for r in _read_local_csv("slo_runbooks")
        if r["service_name"].lower() == svc_clean.lower()
        and (sym_clean.lower() == "all" or r["symptom_code"].lower() == sym_clean.lower())
    ][:10]
    return json.dumps({
        "status": "SUCCESS",
        "source": "local_csv_fallback",
        "fallback_reason": fallback_error,
        "count": len(rows),
        "runbooks": rows,
    })


TOOLS = [
    query_active_incidents,
    correlate_incident,
    inspect_deployment_release,
    lookup_slo_runbook,
]

__all__ = [
    "TOOLS",
    "ACTION_TOOLS",
    "query_active_incidents",
    "correlate_incident",
    "flag_untrusted_text",
    "inspect_deployment_release",
    "lookup_slo_runbook",
    "execute_canary_rollback",
    "scale_service_replicas",
    "annotate_incident_timeline",
]
