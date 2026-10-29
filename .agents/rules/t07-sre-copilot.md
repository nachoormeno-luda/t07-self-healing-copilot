# Table 7 — SRE Copilot rules for coding agents (Antigravity)

These rules apply whenever you touch `seed_packs/t07_self_healing_ops/`, `app/`, `eval/` or `tests/`.

## Domain invariants (never weaken these)

- **Policy comes from BigQuery rows, never from log text.** The rollback target is `policy.allowed_rollback_target`
  (= the release's `previous_stable_version`) and the replica corridor is the runbook's
  `min_safe_replicas..max_safe_replicas`, both returned by `correlate_incident`. `untrusted_log_snippet` and
  `injection_suspected` exist so the model never treats telemetry as instructions.
- **`guardrails.before_tool_callback` is the enforcement point.** Any new action tool that changes production
  state must be added to `GUARDED_TOOLS` with its own `check_*` function. Refusals return
  `{"status": "REJECTED_BY_GUARDRAIL", ...}` and are persisted as `GUARD-xxxx` events in `.state.json`.
  The callback fails closed; keep it that way.
- **Tools never raise.** Wrap bodies in `try/except Exception` and return `json.dumps({"status": "ERROR", ...})`.
  Resolve `GOOGLE_CLOUD_PROJECT` / `AGENT_ENGINE_PROJECT` inside the function, never at import time.
- **No hardcoded model IDs, no `-latest` aliases, no raw `{braces}` in instructions** (ADK session-state tokens).

## Eval-first workflow (Plan → Act → Verify)

1. Before adding or changing a tool or prompt, add a failing golden case to
   `seed_packs/t07_self_healing_ops/eval/golden.test.json` and/or a failing pytest in `tests/`, and commit it.
   - `forbidden_tools` is a **case-level** key (sibling of `conversation`), not inside `intermediate_data`.
   - Expected `args` are matched against what the model actually passed, except `reason`,
     `remediation_summary` and `impact_summary` (free text).
2. Never edit golden cases `case_01` … `case_05`.
3. Run `make test` and `make eval-validate PACK=t07` before proposing a PR; run `make eval PACK=t07` (live Gemini)
   before merging anything that changes `pack.yaml` or a tool signature.
4. After merging changes to `.env`, tools or instructions: `make deploy` then `make test-remote` (the Agent Runtime
   container has no `.env`; config is baked in at deploy time).

## Git workflow

- One feature branch per task (`git worktree add ../wt-<task> -b feat/<task>`), Pull Request into `main`,
  CI (`.github/workflows/ci.yml`) must be green before merge. Never push to `main` directly.
- Demo prompts and pitch evidence live in `docs/DEMO.md`; keep it current when behaviour changes.
