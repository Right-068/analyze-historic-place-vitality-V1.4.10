"""Shared retrieval outcome semantics; never an evaluation or scoring rule."""
from __future__ import annotations

import strict_json as json
from pathlib import Path
from typing import Mapping


STATUS_MATRIX = {
    "completed": {"coverage": True, "sources": True, "actions": {"continue", "exhausted", "not_applicable", "target_met"}},
    "partial": {"coverage": True, "sources": True, "actions": {"continue", "retry", "exhausted"}},
    "blocked": {"coverage": True, "sources": True, "actions": {"continue", "exhausted", "not_applicable"}},
    "no_results": {"coverage": True, "sources": False, "actions": {"continue", "exhausted", "not_applicable", "target_met"}},
    "failed": {"coverage": False, "sources": False, "actions": {"continue", "retry", "exhausted", "not_applicable"}},
}
ITERATION_ACTIONS = set().union(*(rule["actions"] for rule in STATUS_MATRIX.values()))


def route_id(plan: Mapping[str, object]) -> str:
    """One planned intent and access route across attempts, never a tool name or query ID."""
    from runtime_guard import canonical_sha256
    from retrieval_controls import normalized_query_intent
    fields = ('task_run_id', 'place_identity_sha256', 'retrieval_entry',
        'target_platform_id', 'source_category_target', 'iteration_mode', 'gap_target',
        'target_time_range', 'target_subject', 'polarity', 'query_dimension_targets')
    identity = {field: str(plan.get(field, '')) for field in fields}
    identity['normalized_query_intent'] = normalized_query_intent(plan)
    identity['access_policy'] = 'public_only_no_access_barrier_bypass'
    return 'ROUTE-' + canonical_sha256(identity)


def retryable_error(row: Mapping[str, object]) -> str:
    from retrieval_controls import CONTROLLED_RETRY_REASONS
    error = str(row.get("error_type", ""))
    restricted = any(str(row.get(k, "")).lower() in {"true", "1", "yes", "是"}
                     for k in ("login_triggered", "restriction_triggered"))
    if (row.get("status") not in {"failed", "partial"} or restricted
            or row.get("next_action") in {"exhausted", "target_met", "not_applicable"}
            or error not in CONTROLLED_RETRY_REASONS):
        return ""
    return error


def verify_target_basis(row: Mapping[str, object], context: Mapping[str, object]) -> None:
    """A terminal observation references an existing protected machine audit, not a claim."""
    from artifact_provenance import verify_artifact_writer
    from execution_facts import _safe_path
    from runtime_guard import sha256_file
    root = Path(str(context.get("_run_root", "")))
    fields = ("target_audit_file", "target_audit_sha256", "target_audit_state_file", "target_audit_writer_ledger")
    if not context.get("_run_root") or any(not row.get(k) for k in fields):
        raise ValueError("target_met_basis_missing")
    path = _safe_path(root, row["target_audit_file"])
    if sha256_file(path) != row["target_audit_sha256"]:
        raise ValueError("target_met_basis_changed")
    record = verify_artifact_writer(state_path=_safe_path(root, row["target_audit_state_file"]),
        writer_ledger_path=_safe_path(root, row["target_audit_writer_ledger"]),
        output_role="evidence_audit", output_path=path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    summary = payload.get("summary", {})
    termination = summary.get("retrieval_termination") or {}
    if (record.get("task_run_id") != context.get("task_run_id")
            or summary.get("task_run_id") != context.get("task_run_id")
            or payload.get("status") != "valid" or payload.get("errors")
            or summary.get("effective_sample_target_met") is not True
            or termination.get("target_met") is not True
            or termination.get("terminal") is not True):
        raise ValueError("target_met_basis_not_sufficient")


def outcome_errors(row: Mapping[str, object], context: Mapping[str, object]) -> list[str]:
    from execution_facts import integer
    errors = []
    rule = STATUS_MATRIX.get(str(row.get("status")))
    if rule is None:
        return ["execution_status_invalid"]
    action = str(row.get("next_action", ""))
    if action not in rule["actions"]:
        errors.append("execution_status_action_conflict")
    if action == "retry" and not retryable_error(row):
        errors.append("execution_retry_precondition_invalid")
    if action == "target_met":
        try:
            verify_target_basis(row, context)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append("execution_target_basis_invalid:" + str(exc))
    try:
        counts = {k: integer(row.get(k)) for k in ("returned_results", "opened_pages", "relevant_pages", "duplicate_pages")}
    except ValueError:
        return errors  # Strict integer diagnostics are owned by the fact validator.
    if counts["duplicate_pages"] > counts["opened_pages"]:
        errors.append("execution_duplicate_pages_exceed_opened")
    if counts["relevant_pages"] > counts["opened_pages"]:
        errors.append("execution_relevant_pages_exceed_opened")
    if row.get("status") == "no_results" and any(counts.values()):
        errors.append("execution_no_results_has_output")
    if row.get("status") == "failed" and any(counts[k] for k in ("opened_pages", "relevant_pages", "duplicate_pages")):
        errors.append("execution_failed_has_pages")
    if not rule["sources"] and str(row.get("new_scored_evidence_units", "0")) not in {"", "0"}:
        errors.append("execution_without_sources_has_scored_evidence")
    return errors


def source_outcome_errors(row: Mapping[str, object], sources: list[Mapping[str, object]]) -> list[str]:
    from execution_facts import integer
    from source_identity import strict_url_identity
    errors = []
    rule = STATUS_MATRIX.get(str(row.get("status")), {})
    if sources and not rule.get("sources"):
        errors.append("execution_status_cannot_produce_sources")
    # Counts are observations of page opens (including repeated opens). The ledger
    # stores retained discoveries: its distinct URLs are an upper-bound subset.
    # Search result cards are not page opens: following public links may open more
    # pages than returned_results, so no invented opened<=returned rule is used.
    pages = {strict_url_identity(s.get("url", "")) for s in sources}
    relevant = {strict_url_identity(s.get("url", "")) for s in sources
                if str(s.get("is_relevant", "")).lower() in {"true", "1", "yes", "是"}}
    if len(pages) > integer(row.get("opened_pages")):
        errors.append("source_pages_exceed_execution_opened")
    if len(relevant) > integer(row.get("relevant_pages")):
        errors.append("source_relevant_pages_exceed_execution_relevant")
    return errors
