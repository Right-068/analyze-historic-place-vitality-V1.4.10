#!/usr/bin/env python3
"""Shared formal evidence-audit states and terminal semantics."""

from __future__ import annotations

from typing import Mapping


AUDIT_INVALID = "invalid"
AUDIT_NEEDS_ITERATION = "needs_iteration"
AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL = "retrieval_terminated_with_shortfall"

AUDIT_COMPLETE_STATUSES = {
    "valid",
    "valid_with_sample_shortfall",
    "valid_with_dimension_shortfall",
    "valid_with_sample_and_dimension_shortfall",
}
AUDIT_RESTRICTED_DELIVERY_STATUSES = {
    AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
}
AUDIT_TERMINAL_STATUSES = AUDIT_COMPLETE_STATUSES | AUDIT_RESTRICTED_DELIVERY_STATUSES
AUDIT_KNOWN_STATUSES = AUDIT_TERMINAL_STATUSES | {
    AUDIT_INVALID,
    AUDIT_NEEDS_ITERATION,
}

DIMENSION_NUMERIC_STATUSES = {
    "sufficient",
    "sufficient_at_requested_target",
    "sufficient_at_medium_after_audit",
}
DIMENSION_NONNUMERIC_TERMINAL_STATUSES = {
    "exhausted_with_shortfall",
}
DIMENSION_RESTRICTED_DELIVERY_STATUSES = {
    AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
}
DIMENSION_FINAL_STATUSES = (
    DIMENSION_NUMERIC_STATUSES
    | DIMENSION_NONNUMERIC_TERMINAL_STATUSES
    | DIMENSION_RESTRICTED_DELIVERY_STATUSES
)

DIMENSION_LIFECYCLE_ACTIONABLE = "actionable"
DIMENSION_LIFECYCLE_TARGET_REACHED = "requested_target_reached"
DIMENSION_LIFECYCLE_MEDIUM_AUDITED = "medium_audited_numeric_terminal"
DIMENSION_LIFECYCLE_EXHAUSTED = "verified_exhausted_shortfall"
DIMENSION_LIFECYCLE_RESTRICTED = "retrieval_restricted_terminal"
DIMENSION_LIFECYCLE_INVALID = "invalid"

RETRIEVAL_TARGET_MET = "target_met"
RETRIEVAL_ALL_DIMENSIONS_FORMALLY_TERMINAL = "all_dimensions_formally_terminal"
RETRIEVAL_DIMENSION_EXHAUSTED_WITH_SHORTFALL = "dimension_exhausted_with_shortfall"
RETRIEVAL_CONTROL_INVALID = "retrieval_control_invalid"
RETRIEVAL_ROUND_SEQUENCE_INCOMPLETE = "round_sequence_incomplete"
RETRIEVAL_QUERY_BUDGET_REACHED = "query_budget_reached_without_dimension_exhaustion"
RETRIEVAL_ROUND_BUDGET_REACHED = "round_budget_reached_without_dimension_exhaustion"
RETRIEVAL_DIMENSION_BUDGET_REACHED = "dimension_query_budgets_reached_without_exhaustion"
RETRIEVAL_SOURCE_BLOCKED_PENDING_AUDIT = "source_blocked_pending_dimension_audit"
RETRIEVAL_ROUTE_EXHAUSTED_PENDING_AUDIT = "route_exhausted_pending_dimension_audit"
RETRIEVAL_EVIDENCE_SHORTFALL = "evidence_shortfall"
RETRIEVAL_UNRECOVERABLE_ERROR = "unrecoverable_error"

RETRIEVAL_NUMERIC_TERMINAL_STATUSES = {
    RETRIEVAL_TARGET_MET,
    RETRIEVAL_ALL_DIMENSIONS_FORMALLY_TERMINAL,
}
RETRIEVAL_RESTRICTED_TERMINAL_STATUSES = {
    RETRIEVAL_DIMENSION_EXHAUSTED_WITH_SHORTFALL,
}


def formal_dimension_lifecycle(
    payload: Mapping[str, object],
    *,
    requested_target_met: bool,
    medium_terminal_verified: bool = False,
    exhaustion_verified: bool = False,
    restricted_terminal_verified: bool = False,
) -> dict[str, object]:
    """Classify one dimension with the released, mutually exclusive semantics.

    Callers must supply machine-derived facts.  In particular, a status string
    alone never proves the audited-medium or exhausted terminal.  Keeping this
    classification here prevents the planner, retrieval terminator, state
    synchronizer and final audit from inventing slightly different terminal
    sets.
    """

    status = str(payload.get("status", "")).strip()
    if requested_target_met and status in {
        "sufficient",
        "sufficient_at_requested_target",
    }:
        lifecycle = DIMENSION_LIFECYCLE_TARGET_REACHED
        terminal, numeric, restricted = True, True, False
    elif status == "sufficient_at_medium_after_audit" and medium_terminal_verified:
        lifecycle = DIMENSION_LIFECYCLE_MEDIUM_AUDITED
        terminal, numeric, restricted = True, True, False
    elif status == "exhausted_with_shortfall" and exhaustion_verified:
        lifecycle = DIMENSION_LIFECYCLE_EXHAUSTED
        terminal, numeric, restricted = True, False, True
    elif (
        status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
        and restricted_terminal_verified
    ):
        lifecycle = DIMENSION_LIFECYCLE_RESTRICTED
        terminal = True
        numeric = payload.get("numeric_score_permitted") is True
        restricted = True
    elif status not in DIMENSION_FINAL_STATUSES | {"needs_iteration", ""}:
        lifecycle = DIMENSION_LIFECYCLE_INVALID
        terminal, numeric, restricted = False, False, False
    else:
        lifecycle = DIMENSION_LIFECYCLE_ACTIONABLE
        terminal, numeric, restricted = False, False, False
    return {
        "status": status,
        "lifecycle": lifecycle,
        "terminal": terminal,
        "numeric_score_permitted": numeric,
        "restricted_delivery": restricted,
        "actionable": lifecycle == DIMENSION_LIFECYCLE_ACTIONABLE,
        "requested_target_met": bool(requested_target_met),
        "medium_terminal_verified": bool(medium_terminal_verified),
        "exhaustion_verified": bool(exhaustion_verified),
        "restricted_terminal_verified": bool(restricted_terminal_verified),
    }


def audit_status(payload: Mapping[str, object]) -> str:
    return str(payload.get("status", "")).strip()


def is_terminal_audit(payload: Mapping[str, object]) -> bool:
    return audit_status(payload) in AUDIT_TERMINAL_STATUSES


def is_restricted_delivery_audit(payload: Mapping[str, object]) -> bool:
    return audit_status(payload) in AUDIT_RESTRICTED_DELIVERY_STATUSES


def audit_status_zh(status: str) -> str:
    return {
        "valid": "正式证据门槛与逐维门禁均已满足",
        "valid_with_sample_shortfall": "检索已完成独立穷尽审计，但实际计分证据总量仍不足",
        "valid_with_dimension_shortfall": "检索已完成独立穷尽审计，但部分维度正式证据仍不足",
        "valid_with_sample_and_dimension_shortfall": "检索已完成独立穷尽审计，但总量与部分维度仍存在正式证据缺口",
        AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL: "全部不足维度已通过机器绑定的独立穷尽审计，仍有正式证据缺口，仅允许受限交付",
        AUDIT_NEEDS_ITERATION: "正式证据仍需继续检索",
        AUDIT_INVALID: "正式证据审计无效",
    }.get(status, "未知正式证据审计状态")
