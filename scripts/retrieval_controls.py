#!/usr/bin/env python3
"""Bounded retrieval targets, shared-query accounting, and gap priorities."""

from __future__ import annotations

import hashlib
import strict_json as json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from dimension_framework import DIMENSION_NAMES
from formal_states import (
    RETRIEVAL_ALL_DIMENSIONS_FORMALLY_TERMINAL,
    RETRIEVAL_CONTROL_INVALID,
    RETRIEVAL_DIMENSION_BUDGET_REACHED,
    RETRIEVAL_DIMENSION_EXHAUSTED_WITH_SHORTFALL,
    RETRIEVAL_EVIDENCE_SHORTFALL,
    RETRIEVAL_QUERY_BUDGET_REACHED,
    RETRIEVAL_ROUND_BUDGET_REACHED,
    RETRIEVAL_ROUND_SEQUENCE_INCOMPLETE,
    RETRIEVAL_ROUTE_EXHAUSTED_PENDING_AUDIT,
    RETRIEVAL_SOURCE_BLOCKED_PENDING_AUDIT,
    RETRIEVAL_TARGET_MET,
    RETRIEVAL_UNRECOVERABLE_ERROR,
    formal_dimension_lifecycle,
)
from runtime_guard import canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "assets" / "retrieval-control.json"
CONFIDENCE_ORDER = {"数据不足": 0, "低": 1, "中": 2, "中高": 3, "高": 4}
TARGET_CONFIDENCES = ("中", "中高", "高")
EXECUTED_STATUSES = {"completed", "partial", "blocked", "no_results", "failed"}
NON_EXECUTED_STATUSES = {"not_run", "planned", "queued", "cancelled", "skipped"}
EXECUTION_RECORD_TYPES = {"execution", "executed_query"}
PLAN_RECORD_TYPES = {"plan", "planned_query"}
LEGAL_DEEP_ITERATION_MODES = {"evidence_gap_fill", "medium_enhancement"}
CONTROLLED_RETRY_REASONS = {
    "transient_network_error",
    "timeout",
    "temporary_rate_limit",
    "server_error",
}
CONTROLLED_RETRY_INTERVAL_POLICIES = {
    "host_managed_backoff",
    "bounded_exponential_backoff",
    "manual_resume_after_transient_failure",
}
CURRENT_EXECUTION_SCHEMA_VERSION = "search-execution-2"
LEGACY_EXECUTION_SCHEMA_VERSION = "legacy-search-log-1"
CURRENT_EXECUTION_REQUIRED_FIELDS = (
    "task_run_id",
    "record_type",
    "plan_id",
    "execution_id",
    "query_id",
    "query",
    "started_at",
    "finished_at",
)
DEFAULT_BUDGET_FIELDS = {
    "baseline_query_budget": 120,
    "baseline_independent_intent_budget": 120,
    "baseline_attempt_budget": 144,
    "baseline_retry_budget": 24,
    "global_independent_intent_budget": 300,
    "global_attempt_budget": 360,
    "global_retry_budget": 60,
    "total_retry_budget": 84,
    "total_query_safety_limit": 504,
    "per_round_independent_intent_budget": 60,
    "per_round_attempt_budget": 72,
    "per_round_retry_budget": 12,
    "per_dimension_independent_intent_budget": 48,
    "per_dimension_attempt_budget": 62,
    "per_dimension_retry_budget": 14,
    "per_round_per_dimension_independent_intent_budget": 10,
    "per_round_per_dimension_attempt_budget": 13,
    "per_round_per_dimension_retry_budget": 3,
    "minimum_targeted_queries_per_round": 8,
    "minimum_medium_total_targeted_rounds": 5,
    "minimum_medium_enhancement_rounds": 3,
    "minimum_medium_enhancement_queries_per_round": 10,
    "maximum_dimensions_per_shared_query": 3,
    "maximum_controlled_retries_per_intent": 1,
}


def budget_values(config: Mapping[str, object]) -> dict[str, int]:
    raw = config.get("budgets")
    if not isinstance(raw, Mapping):
        raise ValueError("retrieval control requires budgets")
    merged = dict(raw)
    required = {
        "global_query_budget",
        "per_round_query_budget",
        "per_dimension_query_budget",
        "maximum_iteration_rounds",
        "consecutive_low_gain_rounds",
        "repeated_source_failure_limit",
        *DEFAULT_BUDGET_FIELDS,
    }
    if not required.issubset(merged):
        raise ValueError("retrieval control budget fields are incomplete")
    values: dict[str, int] = {}
    for key in required:
        value = merged[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2**53 - 1:
            raise ValueError("retrieval control budgets must be positive integers")
        values[key] = value
    return values


def validate_retrieval_config(payload: Mapping[str, object]) -> dict[str, object]:
    budgets = budget_values(payload)
    target = str(payload.get("default_target_confidence", ""))
    if target not in TARGET_CONFIDENCES:
        raise ValueError("default_target_confidence is invalid")
    total_rounds = budgets["minimum_medium_total_targeted_rounds"]
    enhancement_rounds = budgets["minimum_medium_enhancement_rounds"]
    if enhancement_rounds > total_rounds:
        raise ValueError("enhancement rounds cannot exceed total targeted rounds")
    if budgets["maximum_iteration_rounds"] < total_rounds:
        raise ValueError("maximum iteration rounds cannot satisfy the medium completion audit")
    alias_pairs = (
        ("baseline_query_budget", "baseline_independent_intent_budget"),
        ("global_query_budget", "global_independent_intent_budget"),
        ("per_round_query_budget", "per_round_independent_intent_budget"),
        ("per_dimension_query_budget", "per_dimension_independent_intent_budget"),
    )
    for legacy_name, explicit_name in alias_pairs:
        if budgets[legacy_name] != budgets[explicit_name]:
            raise ValueError(
                f"{legacy_name} must equal the explicit independent-intent budget {explicit_name}"
            )
    per_dimension_minimum = (
        enhancement_rounds * budgets["minimum_medium_enhancement_queries_per_round"]
        + (total_rounds - enhancement_rounds) * budgets["minimum_targeted_queries_per_round"]
    )
    if budgets["per_dimension_independent_intent_budget"] < per_dimension_minimum:
        raise ValueError(
            "per-dimension query budget cannot reserve every mandatory targeted and enhancement round"
        )
    clusters = math.ceil(len(DIMENSION_NAMES) / budgets["maximum_dimensions_per_shared_query"])
    # A cluster may contain dimensions at different points in the released
    # round contract.  Reserve one regular and one enhancement group per
    # cluster so every deterministic transition combination remains feasible.
    per_cluster_worst_case = (
        budgets["minimum_targeted_queries_per_round"]
        + budgets["minimum_medium_enhancement_queries_per_round"]
    )
    per_round_minimum = clusters * per_cluster_worst_case
    if budgets["per_round_independent_intent_budget"] < per_round_minimum:
        raise ValueError("per-round query budget cannot serve all dimension clusters deterministically")
    global_worst_case = total_rounds * per_round_minimum
    if budgets["global_independent_intent_budget"] < global_worst_case:
        raise ValueError(
            "global query budget cannot satisfy every mixed-mode round under the sharing limit"
        )
    attempt_reservations = (
        ("baseline_attempt_budget", "baseline_independent_intent_budget", "baseline_retry_budget"),
        ("global_attempt_budget", "global_independent_intent_budget", "global_retry_budget"),
        ("per_round_attempt_budget", "per_round_independent_intent_budget", "per_round_retry_budget"),
        ("per_dimension_attempt_budget", "per_dimension_independent_intent_budget", "per_dimension_retry_budget"),
        (
            "per_round_per_dimension_attempt_budget",
            "per_round_per_dimension_independent_intent_budget",
            "per_round_per_dimension_retry_budget",
        ),
    )
    for attempt_name, intent_name, retry_name in attempt_reservations:
        if budgets[attempt_name] < budgets[intent_name] + budgets[retry_name]:
            raise ValueError(
                f"{attempt_name} must reserve both {intent_name} and {retry_name}"
            )
    if budgets["per_round_per_dimension_independent_intent_budget"] < max(
        budgets["minimum_targeted_queries_per_round"],
        budgets["minimum_medium_enhancement_queries_per_round"],
    ):
        raise ValueError("per-round per-dimension independent-intent budget is infeasible")
    if budgets["total_retry_budget"] < (
        budgets["baseline_retry_budget"] + budgets["global_retry_budget"]
    ):
        raise ValueError("total retry budget must reserve baseline and deep retry budgets")
    if budgets["total_query_safety_limit"] < (
        budgets["baseline_attempt_budget"] + budgets["global_attempt_budget"]
    ):
        raise ValueError(
            "total query safety limit must reserve the complete baseline and deep-search attempt budgets"
        )
    execution_contract = payload.get("execution_log_contract")
    if not isinstance(execution_contract, Mapping):
        raise ValueError("retrieval control requires execution_log_contract")
    if execution_contract.get("current_schema_version") != CURRENT_EXECUTION_SCHEMA_VERSION:
        raise ValueError("current execution log schema is not the released schema")
    if execution_contract.get("legacy_schema_version") != LEGACY_EXECUTION_SCHEMA_VERSION:
        raise ValueError("legacy execution log schema is not the released schema")
    try:
        datetime.fromisoformat(str(execution_contract.get("legacy_state_cutoff_utc", "")))
    except ValueError as exc:
        raise ValueError("legacy execution schema cutoff must be an ISO timestamp") from exc
    research_targets = payload.get("research_targets")
    if not isinstance(research_targets, Mapping):
        raise ValueError("retrieval control requires research_targets")
    for field in ("minimum_deduplicated_relevant_pages", "legacy_scored_evidence_floor"):
        value = research_targets.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"retrieval control research target is invalid: {field}")
    normalized = dict(payload)
    normalized["budgets"] = budgets
    return normalized


def research_target_summary(
    *,
    target_confidence: str | None = None,
    config: Mapping[str, object] | None = None,
    protocol: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return one machine-derived startup statement for pages, evidence, and budgets."""

    controls = dict(config or load_retrieval_config())
    target = target_confidence or str(controls["default_target_confidence"])
    if protocol is None:
        from formal_scoring import load_protocol

        protocol = load_protocol()
    threshold = confidence_thresholds(protocol).get(target)
    if not isinstance(threshold, Mapping):
        raise ValueError("requested confidence threshold is missing")
    research_targets = controls["research_targets"]
    budgets = budget_values(controls)
    per_dimension = int(threshold["scored_evidence_units"])
    return {
        "target_confidence": target,
        "per_dimension_scored_evidence_target": per_dimension,
        "per_dimension_source_page_target": int(threshold["distinct_source_pages"]),
        "per_dimension_source_category_target": int(threshold["source_categories"]),
        "theoretical_minimum_scored_evidence": len(DIMENSION_NAMES) * per_dimension,
        "minimum_deduplicated_relevant_pages": int(
            research_targets["minimum_deduplicated_relevant_pages"]
        ),
        "legacy_scored_evidence_floor": int(research_targets["legacy_scored_evidence_floor"]),
        "legacy_floor_is_complete_target": False,
        "query_budgets": budgets,
        "legal_terminal_paths": [
            "target_met",
            "dimension_exhausted_with_shortfall",
            "unrecoverable_error",
        ],
        "budget_flags_are_terminal": False,
        "restricted_delivery_requires_verified_dimension_exhaustion": True,
        "noncompensatory_dimension_gates": True,
    }


def load_retrieval_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("retrieval control root must be an object")
    return validate_retrieval_config(payload)


def confidence_thresholds(protocol: Mapping[str, object]) -> Mapping[str, object]:
    formal = protocol.get("formal_scoring")
    if not isinstance(formal, Mapping):
        raise ValueError("formal_scoring is missing")
    thresholds = formal.get("dimension_confidence_thresholds")
    if not isinstance(thresholds, Mapping):
        raise ValueError("dimension confidence thresholds are missing")
    return thresholds


def theoretical_minimum_evidence(protocol: Mapping[str, object], target_confidence: str) -> int:
    if target_confidence not in TARGET_CONFIDENCES:
        raise ValueError("target confidence must be 中, 中高, or 高")
    rule = confidence_thresholds(protocol)[target_confidence]
    if not isinstance(rule, Mapping):
        raise ValueError("target confidence threshold is invalid")
    return len(DIMENSION_NAMES) * int(rule["scored_evidence_units"])


def dimension_target_status(
    dimension_results: Mapping[str, Mapping[str, object]],
    *,
    target_confidence: str,
    protocol: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Summarize a seven-dimension target without replacing per-dimension gates.

    The total is descriptive only.  Reaching 175, 280, or 420 observations can
    never compensate for a dimension that remains below the requested
    confidence.  This keeps both the theoretical total and the seven separate
    gates visible to the manager and the final audit.
    """

    if target_confidence not in TARGET_CONFIDENCES:
        raise ValueError("target confidence must be 中, 中高, or 高")
    if protocol is None:
        # Local import avoids coupling identity/query helpers to the scoring
        # module at import time while retaining the released protocol as the
        # only source of dimension thresholds.
        from formal_scoring import load_protocol

        protocol = load_protocol()
    threshold = confidence_thresholds(protocol).get(target_confidence)
    if not isinstance(threshold, Mapping):
        raise ValueError("requested confidence threshold is missing")
    required_per_dimension = int(threshold["scored_evidence_units"])
    scored_by_dimension: dict[str, int] = {}
    remaining: list[str] = []
    lifecycle_by_dimension: dict[str, dict[str, object]] = {}
    for name in DIMENSION_NAMES:
        result = dimension_results.get(name, {})
        count = int(result.get("scored_evidence_units", 0) or 0)
        scored_by_dimension[name] = count
        confidence = str(result.get("confidence", "数据不足"))
        requested_target_met = not (
            count < required_per_dimension
            or CONFIDENCE_ORDER.get(confidence, 0) < CONFIDENCE_ORDER[target_confidence]
        )
        lifecycle_by_dimension[name] = formal_dimension_lifecycle(
            result,
            requested_target_met=requested_target_met,
        )
        if not requested_target_met:
            remaining.append(name)
    total = sum(scored_by_dimension.values())
    theoretical_total = required_per_dimension * len(DIMENSION_NAMES)
    return {
        "target_confidence": target_confidence,
        "required_scored_evidence_per_dimension": required_per_dimension,
        "theoretical_total_scored_evidence": theoretical_total,
        "actual_total_scored_evidence": total,
        "scored_evidence_by_dimension": scored_by_dimension,
        "remaining_dimensions": remaining,
        "target_met": not remaining,
        "dimension_lifecycle": lifecycle_by_dimension,
    }


def validate_query_dimension_targets(
    row: Mapping[str, object],
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate one query's declared target list without repairing it.

    The declaration is the only source of dimension attribution for query
    accounting.  Invalid declarations are never truncated, inferred from a
    gap label, or partially counted.
    """

    controls = dict(config or load_retrieval_config())
    maximum = budget_values(controls)["maximum_dimensions_per_shared_query"]
    raw_value = row.get("query_dimension_targets", "")
    errors: list[dict[str, object]] = []
    parsed: object
    if isinstance(raw_value, list):
        parsed = raw_value
    else:
        raw = str(raw_value).strip()
        if not raw:
            parsed = None
            errors.append(
                {
                    "code": "query_dimension_targets_missing",
                    "message": "query_dimension_targets is required for an executed query",
                }
            )
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
                errors.append(
                    {
                        "code": "query_dimension_targets_invalid_json",
                        "message": "query_dimension_targets must be a valid JSON array",
                    }
                )
    values: list[str] = []
    if parsed is None and not errors:
        errors.append({"code": "query_dimension_targets_not_array",
                       "message": "query_dimension_targets must be a non-empty JSON array"})
    if parsed is not None:
        if not isinstance(parsed, list):
            errors.append(
                {
                    "code": "query_dimension_targets_not_array",
                    "message": "query_dimension_targets must be a JSON array",
                }
            )
        else:
            values = [str(item) for item in parsed]
            if not values:
                errors.append({"code": "query_dimension_targets_empty",
                               "message": "one executable query must target at least one dimension"})
            unknown = [value for value in values if value not in DIMENSION_NAMES]
            if unknown:
                errors.append(
                    {
                        "code": "query_dimension_target_unknown",
                        "message": "query_dimension_targets contains a non-canonical dimension",
                        "values": unknown,
                    }
                )
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                errors.append(
                    {
                        "code": "query_dimension_target_duplicate",
                        "message": "query_dimension_targets contains duplicate dimensions",
                        "values": duplicates,
                    }
                )
            if len(values) > maximum:
                errors.append(
                    {
                        "code": "query_dimension_target_limit_exceeded",
                        "message": f"one query may target at most {maximum} dimensions",
                        "actual": len(values),
                        "maximum": maximum,
                    }
                )
    return {
        "status": "valid" if not errors else "invalid",
        "targets": values if not errors else [],
        "errors": errors,
        "error_codes": sorted({str(item["code"]) for item in errors}),
        "maximum_dimensions_per_shared_query": maximum,
    }


def parse_dimension_targets(row: Mapping[str, object]) -> list[str]:
    """Return targets only when the complete declaration is valid."""

    result = validate_query_dimension_targets(row)
    return list(result["targets"]) if result["status"] == "valid" else []


def normalized_query_intent(row: Mapping[str, object]) -> str:
    query = unicodedata.normalize("NFKC", str(row.get("query", ""))).casefold()
    query = re.sub(r"[^\w\u4e00-\u9fff]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    ignored = {"有关", "相关", "查询", "搜索", "资料"}
    # The hard identity retains token order.  A sorted bag of words may be
    # useful as a review hint, but it is not safe for automatic suppression:
    # negation, causal direction, subject and object all depend on order.
    tokens = [token for token in query.split() if token and token not in ignored]
    targets = sorted(parse_dimension_targets(row))
    platform = str(row.get("target_platform_id", "")).strip().casefold()
    category = str(row.get("source_category_target", "")).strip().casefold()
    polarity = str(row.get("polarity", "")).strip().casefold()
    return json.dumps(
        {
            "tokens": tokens,
            "place": " ".join(unicodedata.normalize("NFKC", str(row.get("place") or row.get("place_name") or row.get("place_identity") or "")).split()).casefold(),
            "targets": targets,
            "platform": platform,
            "category": category,
            "polarity": polarity,
            "time_range": str(row.get("target_time_range") or row.get("time_context") or "").strip().casefold(),
            "language": str(row.get("language") or row.get("query_language") or "zh").strip().casefold(),
            "iteration_mode": str(row.get("iteration_mode") or "").strip().casefold(),
            "subject": str(row.get("target_subject") or row.get("subject_scope") or "").strip().casefold(),
            "retrieval_entry": str(row.get("retrieval_entry") or "").strip().casefold(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_query_intent_signature(row: Mapping[str, object]) -> dict[str, object]:
    """Recompute the query signature and reject a conflicting stored value.

    The persisted field is an audit checksum, never an authority.  A missing
    value is compatible with legacy rows and can be filled by a controlled
    writer; a conflicting value is fail-closed and contributes no metrics.
    """

    derived = normalized_query_intent(row)
    stored = str(row.get("normalized_query_intent") or "").strip()
    valid = not stored or stored == derived
    return {
        "status": "valid" if valid else "invalid",
        "derived": derived,
        "stored": stored,
        "error": None
        if valid
        else {
            "code": "normalized_query_intent_mismatch",
            "message": "stored normalized_query_intent disagrees with the machine-derived signature",
        },
    }


def execution_schema_context_from_state(
    state: Mapping[str, object],
    *,
    config: Mapping[str, object] | None = None,
    state_path: Path | None = None,
    run_root: Path | None = None,
) -> dict[str, object]:
    from execution_facts import context_from_state

    return context_from_state(
        state, config=config or load_retrieval_config(),
        state_path=state_path, run_root=run_root,
    )


def validate_execution_records(
    search_rows: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object] | None = None,
    schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    from execution_facts import validate_facts

    return validate_facts(
        search_rows, config=config or load_retrieval_config(),
        schema_context=schema_context,
    )


def is_executed_query(row: Mapping[str, object], *,
                      schema_context: Mapping[str, object] | None = None,
                      execution_rows: Iterable[Mapping[str, object]] | None = None) -> bool:
    facts = validate_execution_records([row] if execution_rows is None else execution_rows,
                                       schema_context=schema_context)
    return str(row.get("execution_id", "")) in facts["valid_execution_ids"]


def executed_search_rows(
    search_rows: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object] | None = None,
    schema_context: Mapping[str, object] | None = None,
) -> list[Mapping[str, object]]:
    return list(
        validate_execution_records(
            list(search_rows), config=config, schema_context=schema_context
        )["valid_rows"]
    )


def executed_query_metrics(
    search_rows: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object] | None = None,
    schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    controls = dict(config or load_retrieval_config())
    budgets = budget_values(controls)
    seen_intents: set[str] = set()
    new_intents_by_round: Counter[int] = Counter()
    coverage_intents_by_round: Counter[int] = Counter()
    budget_intents_by_dimension_round: defaultdict[str, defaultdict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    actual_attempts_by_round: Counter[int] = Counter()
    actual_attempts_by_dimension: Counter[str] = Counter()
    actual_attempts_by_dimension_round: defaultdict[str, Counter[int]] = defaultdict(Counter)
    first_attempts_by_round: Counter[int] = Counter()
    retries_by_round: Counter[int] = Counter()
    first_attempts_by_dimension: Counter[str] = Counter()
    retries_by_dimension: Counter[str] = Counter()
    retries_by_dimension_round: defaultdict[str, Counter[int]] = defaultdict(Counter)
    status_by_round: defaultdict[int, list[str]] = defaultdict(list)
    gain_by_round: Counter[int] = Counter()
    modes_by_round: defaultdict[int, set[str]] = defaultdict(set)
    modes_by_dimension_round: defaultdict[str, defaultdict[int, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    baseline_attempt_count = 0
    deep_attempt_count = 0
    baseline_intents: set[str] = set()
    deep_intents: set[str] = set()
    baseline_attempts_by_dimension: Counter[str] = Counter()
    deep_attempts_by_dimension: Counter[str] = Counter()
    round_number_errors: list[dict[str, object]] = []
    targeted_rounds: defaultdict[str, set[int]] = defaultdict(set)
    enhancement_rounds: defaultdict[str, set[int]] = defaultdict(set)
    enhancement_queries: defaultdict[str, Counter[int]] = defaultdict(Counter)
    unique_intents_by_dimension: defaultdict[str, set[str]] = defaultdict(set)
    baseline_unique_intents_by_dimension: defaultdict[str, set[str]] = defaultdict(set)
    deep_unique_intents_by_dimension: defaultdict[str, set[str]] = defaultdict(set)
    unique_intents_by_dimension_round: defaultdict[str, defaultdict[int, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    rows = list(search_rows)
    validation = validate_execution_records(
        rows, config=controls, schema_context=schema_context
    )
    first_ids = set(str(value) for value in validation["first_attempt_ids"])
    retry_ids = set(str(value) for value in validation["controlled_retry_ids"])

    for row in validation["attempt_rows"]:
        raw_round = str(row.get("iteration_round", "0") or "0").strip()
        try:
            round_number = int(raw_round)
        except ValueError:
            continue
        actual_attempts_by_round[round_number] += 1
        if round_number == 0:
            baseline_attempt_count += 1
        elif round_number > 0:
            deep_attempt_count += 1
        target_validation = validate_query_dimension_targets(row, config=controls)
        if target_validation["status"] == "valid":
            for dimension in target_validation["targets"]:
                actual_attempts_by_dimension[dimension] += 1
                actual_attempts_by_dimension_round[dimension][round_number] += 1
                if round_number == 0:
                    baseline_attempts_by_dimension[dimension] += 1
                elif round_number > 0:
                    deep_attempts_by_dimension[dimension] += 1

    for row in validation["valid_rows"]:
        execution_id = str(row.get("execution_id", ""))
        round_number = int(row["_validated_round"])
        intent = str(row["_validated_intent"])
        targets = list(row["_validated_targets"])
        is_new_intent = intent not in seen_intents
        if is_new_intent:
            seen_intents.add(intent)
            new_intents_by_round[round_number] += 1
            if round_number == 0:
                baseline_intents.add(intent)
            else:
                deep_intents.add(intent)
        if row.get('_new_coverage_intent'):
            coverage_intents_by_round[round_number] += 1
        if execution_id in first_ids:
            first_attempts_by_round[round_number] += 1
        if execution_id in retry_ids:
            retries_by_round[round_number] += 1
        status_by_round[round_number].append(str(row.get("status", "")))
        mode = str(row.get("iteration_mode", "")).strip()
        modes_by_round[round_number].add(mode)
        try:
            gain_by_round[round_number] += int(str(row.get("new_scored_evidence_units", "0") or "0"))
        except ValueError:
            pass
        for dimension in targets:
            if execution_id in first_ids:
                first_attempts_by_dimension[dimension] += 1
            if execution_id in retry_ids:
                retries_by_dimension[dimension] += 1
                retries_by_dimension_round[dimension][round_number] += 1
            dimension_new_intent = intent not in unique_intents_by_dimension[dimension]
            if dimension_new_intent:
                unique_intents_by_dimension[dimension].add(intent)
                budget_intents_by_dimension_round[dimension][round_number].add(intent)
                if row.get('_new_coverage_intent'):
                    unique_intents_by_dimension_round[dimension][round_number].add(intent)
                if round_number == 0:
                    baseline_unique_intents_by_dimension[dimension].add(intent)
                elif round_number > 0:
                    deep_unique_intents_by_dimension[dimension].add(intent)
            if round_number > 0:
                targeted_rounds[dimension].add(round_number)
                modes_by_dimension_round[dimension][round_number].add(mode)
            if mode == "medium_enhancement" and round_number > 0:
                enhancement_rounds[dimension].add(round_number)
    for dimension in DIMENSION_NAMES:
        for number in enhancement_rounds[dimension]:
            enhancement_queries[dimension][number] = len(
                unique_intents_by_dimension_round[dimension][number]
            )
    invalid_query_records = list(validation["invalid_records"])
    query_error_codes = sorted(
        {
            str(code)
            for item in invalid_query_records
            for code in item.get("error_codes", [])
        }
    )
    return {
        "actual_execution_attempt_count": len(validation["attempt_rows"]),
        "valid_execution_attempt_count": len(validation["valid_rows"]),
        "first_attempt_count": len(first_ids),
        "controlled_retry_count": len(retry_ids),
        "baseline_controlled_retry_count": int(retries_by_round[0]),
        "deep_controlled_retry_count": sum(
            count for number, count in retries_by_round.items() if number > 0
        ),
        "executed_query_count": len(validation["attempt_rows"]),
        "unique_query_intent_count": len(seen_intents),
        "effective_coverage_intent_count": sum(coverage_intents_by_round.values()),
        "baseline_executed_query_count": baseline_attempt_count,
        "deep_executed_query_count": deep_attempt_count,
        "baseline_unique_query_intent_count": len(baseline_intents),
        "deep_unique_query_intent_count": len(deep_intents),
        "query_intents": sorted(seen_intents),
        "query_ids": sorted({str(row["query_id"]) for row in validation["valid_rows"]}),
        "execution_ids": list(validation["valid_execution_ids"]),
        "independent_intent_ids": sorted(canonical_sha256(value) for value in seen_intents),
        "duplicate_execution_ids": sorted(
            {
                str(item.get("execution_id", ""))
                for item in invalid_query_records
                if "duplicate_execution_id" in item.get("error_codes", [])
            }
        ),
        "invalid_query_records": invalid_query_records,
        "valid_execution_set_sha256": validation["valid_execution_set_sha256"],
        "invalid_execution_set_sha256": validation["invalid_execution_set_sha256"],
        "execution_schema_version": validation["schema_version"],
        "execution_schema_context": validation["schema_context"],
        "legacy_migration": validation["migration"],
        "query_error_codes": query_error_codes,
        "round_number_errors": round_number_errors,
        "queries_by_round": {
            str(key): int(coverage_intents_by_round[key])
            for key in sorted(actual_attempts_by_round)
        },
        "new_unique_query_intents_by_round": {
            str(key): int(new_intents_by_round[key])
            for key in sorted(actual_attempts_by_round)
        },
        "actual_attempts_by_round": {
            str(key): value for key, value in sorted(actual_attempts_by_round.items())
        },
        "first_attempts_by_round": {
            str(key): value for key, value in sorted(first_attempts_by_round.items())
        },
        "controlled_retries_by_round": {
            str(key): value for key, value in sorted(retries_by_round.items())
        },
        "queries_by_dimension": {
            name: actual_attempts_by_dimension[name] for name in DIMENSION_NAMES
        },
        "actual_attempts_by_dimension": {
            name: actual_attempts_by_dimension[name] for name in DIMENSION_NAMES
        },
        "actual_attempts_by_dimension_round": {
            name: {
                str(number): count
                for number, count in sorted(actual_attempts_by_dimension_round[name].items())
            }
            for name in DIMENSION_NAMES
        },
        "first_attempts_by_dimension": {
            name: first_attempts_by_dimension[name] for name in DIMENSION_NAMES
        },
        "controlled_retries_by_dimension": {
            name: retries_by_dimension[name] for name in DIMENSION_NAMES
        },
        "deep_controlled_retries_by_dimension": {
            name: sum(
                count
                for number, count in retries_by_dimension_round[name].items()
                if number > 0
            )
            for name in DIMENSION_NAMES
        },
        "controlled_retries_by_dimension_round": {
            name: {
                str(number): count
                for number, count in sorted(retries_by_dimension_round[name].items())
            }
            for name in DIMENSION_NAMES
        },
        "baseline_queries_by_dimension": {
            name: baseline_attempts_by_dimension[name] for name in DIMENSION_NAMES
        },
        "deep_queries_by_dimension": {
            name: deep_attempts_by_dimension[name] for name in DIMENSION_NAMES
        },
        "unique_query_intents_by_dimension": {
            name: len(unique_intents_by_dimension[name]) for name in DIMENSION_NAMES
        },
        "baseline_unique_query_intents_by_dimension": {
            name: len(baseline_unique_intents_by_dimension[name]) for name in DIMENSION_NAMES
        },
        "deep_unique_query_intents_by_dimension": {
            name: len(deep_unique_intents_by_dimension[name]) for name in DIMENSION_NAMES
        },
        "unique_query_intents_by_dimension_round": {
            name: {
                str(number): len(intents)
                for number, intents in sorted(unique_intents_by_dimension_round[name].items())
            }
            for name in DIMENSION_NAMES
        },
        "new_unique_query_intents_by_dimension_round": {
            name: {
                str(number): len(unique_intents_by_dimension_round[name][number])
                for number in sorted(actual_attempts_by_dimension_round[name])
            }
            for name in DIMENSION_NAMES
        },
        "budget_independent_intents_by_dimension_round": {
            name: {str(number): len(intents) for number, intents in sorted(budget_intents_by_dimension_round[name].items())}
            for name in DIMENSION_NAMES
        },
        "targeted_rounds_by_dimension": {
            name: sorted(targeted_rounds[name]) for name in DIMENSION_NAMES
        },
        "enhancement_rounds_by_dimension": {
            name: sorted(enhancement_rounds[name]) for name in DIMENSION_NAMES
        },
        "enhancement_queries_by_dimension_round": {
            name: {str(number): count for number, count in sorted(enhancement_queries[name].items())}
            for name in DIMENSION_NAMES
        },
        "statuses_by_round": {str(key): values for key, values in sorted(status_by_round.items())},
        "gain_by_round": {str(key): value for key, value in sorted(gain_by_round.items())},
        "iteration_modes_by_round": {
            str(key): sorted(values) for key, values in sorted(modes_by_round.items())
        },
        "iteration_modes_by_dimension_round": {
            name: {
                str(number): sorted(values)
                for number, values in sorted(modes_by_dimension_round[name].items())
            }
            for name in DIMENSION_NAMES
        },
    }


def validate_query_budget_compliance(
    metrics: Mapping[str, object],
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Audit coverage, attempts, and retries as separate hard limits."""

    controls = dict(config or load_retrieval_config())
    budgets = budget_values(controls)
    errors: list[dict[str, object]] = []

    def over(code: str, actual: int, maximum: int, **context: object) -> None:
        if actual > maximum:
            errors.append(
                {
                    "code": code,
                    "actual": actual,
                    "maximum": maximum,
                    **context,
                }
            )

    over(
        "baseline_independent_intent_budget_exceeded",
        int(metrics.get("baseline_unique_query_intent_count", 0) or 0),
        budgets["baseline_independent_intent_budget"],
    )
    over(
        "baseline_query_budget_exceeded",
        int(metrics.get("baseline_executed_query_count", 0) or 0),
        budgets["baseline_attempt_budget"],
    )
    over(
        "baseline_retry_budget_exceeded",
        int(metrics.get("baseline_controlled_retry_count", 0) or 0),
        budgets["baseline_retry_budget"],
    )
    over(
        "global_independent_intent_budget_exceeded",
        int(metrics.get("deep_unique_query_intent_count", 0) or 0),
        budgets["global_independent_intent_budget"],
    )
    over(
        "global_query_budget_exceeded",
        int(metrics.get("deep_executed_query_count", 0) or 0),
        budgets["global_attempt_budget"],
    )
    over(
        "global_retry_budget_exceeded",
        int(metrics.get("deep_controlled_retry_count", 0) or 0),
        budgets["global_retry_budget"],
    )
    over(
        "total_query_safety_limit_exceeded",
        int(metrics.get("actual_execution_attempt_count", 0) or 0),
        budgets["total_query_safety_limit"],
    )
    over(
        "total_retry_budget_exceeded",
        int(metrics.get("controlled_retry_count", 0) or 0),
        budgets["total_retry_budget"],
    )

    unique_rounds = metrics.get("new_unique_query_intents_by_round", {})
    attempt_rounds = metrics.get("actual_attempts_by_round", {})
    retry_rounds = metrics.get("controlled_retries_by_round", {})
    round_keys = {
        str(key)
        for mapping in (unique_rounds, attempt_rounds, retry_rounds)
        if isinstance(mapping, Mapping)
        for key in mapping
        if str(key).isdigit() and int(str(key)) > 0
    }
    for key in sorted(round_keys, key=int):
        over(
            "round_independent_intent_budget_exceeded",
            int(unique_rounds.get(key, 0) if isinstance(unique_rounds, Mapping) else 0),
            budgets["per_round_independent_intent_budget"],
            iteration_round=int(key),
        )
        over(
            "round_query_budget_exceeded",
            int(attempt_rounds.get(key, 0) if isinstance(attempt_rounds, Mapping) else 0),
            budgets["per_round_attempt_budget"],
            iteration_round=int(key),
        )
        over(
            "round_retry_budget_exceeded",
            int(retry_rounds.get(key, 0) if isinstance(retry_rounds, Mapping) else 0),
            budgets["per_round_retry_budget"],
            iteration_round=int(key),
        )

    unique_by_dimension = metrics.get("deep_unique_query_intents_by_dimension", {})
    attempts_by_dimension = metrics.get("deep_queries_by_dimension", {})
    retries_by_dimension = metrics.get("deep_controlled_retries_by_dimension", {})
    unique_by_dimension_round = metrics.get(
        "budget_independent_intents_by_dimension_round", metrics.get("new_unique_query_intents_by_dimension_round", {})
    )
    attempts_by_dimension_round = metrics.get("actual_attempts_by_dimension_round", {})
    retries_by_dimension_round = metrics.get("controlled_retries_by_dimension_round", {})
    for dimension in DIMENSION_NAMES:
        over(
            "dimension_independent_intent_budget_exceeded",
            int(unique_by_dimension.get(dimension, 0) if isinstance(unique_by_dimension, Mapping) else 0),
            budgets["per_dimension_independent_intent_budget"],
            dimension=dimension,
        )
        over(
            "dimension_query_budget_exceeded",
            int(attempts_by_dimension.get(dimension, 0) if isinstance(attempts_by_dimension, Mapping) else 0),
            budgets["per_dimension_attempt_budget"],
            dimension=dimension,
        )
        over(
            "dimension_retry_budget_exceeded",
            int(retries_by_dimension.get(dimension, 0) if isinstance(retries_by_dimension, Mapping) else 0),
            budgets["per_dimension_retry_budget"],
            dimension=dimension,
        )
        dim_unique = (
            unique_by_dimension_round.get(dimension, {})
            if isinstance(unique_by_dimension_round, Mapping)
            and isinstance(unique_by_dimension_round.get(dimension), Mapping)
            else {}
        )
        dim_attempts = (
            attempts_by_dimension_round.get(dimension, {})
            if isinstance(attempts_by_dimension_round, Mapping)
            and isinstance(attempts_by_dimension_round.get(dimension), Mapping)
            else {}
        )
        dim_retries = (
            retries_by_dimension_round.get(dimension, {})
            if isinstance(retries_by_dimension_round, Mapping)
            and isinstance(retries_by_dimension_round.get(dimension), Mapping)
            else {}
        )
        dim_round_keys = {
            str(key)
            for mapping in (dim_unique, dim_attempts, dim_retries)
            for key in mapping
            if str(key).isdigit() and int(str(key)) > 0
        }
        for key in sorted(dim_round_keys, key=int):
            over(
                "dimension_round_independent_intent_budget_exceeded",
                int(dim_unique.get(key, 0)),
                budgets["per_round_per_dimension_independent_intent_budget"],
                dimension=dimension,
                iteration_round=int(key),
            )
            over(
                "dimension_round_query_budget_exceeded",
                int(dim_attempts.get(key, 0)),
                budgets["per_round_per_dimension_attempt_budget"],
                dimension=dimension,
                iteration_round=int(key),
            )
            over(
                "dimension_round_retry_budget_exceeded",
                int(dim_retries.get(key, 0)),
                budgets["per_round_per_dimension_retry_budget"],
                dimension=dimension,
                iteration_round=int(key),
            )
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "error_codes": sorted({str(item["code"]) for item in errors}),
        "budgets": budgets,
    }


def validate_round_metrics(
    metrics: Mapping[str, object],
    *,
    config: Mapping[str, object] | None = None,
    required_dimensions: Iterable[str] | None = None,
    expected_round_count: int | None = None,
) -> dict[str, object]:
    """Apply the released round-completion contract to machine metrics."""

    controls = dict(config or load_retrieval_config())
    budgets = budget_values(controls)
    expected = budgets["maximum_iteration_rounds"] if expected_round_count is None else int(expected_round_count)
    if expected < 0 or expected > budgets["maximum_iteration_rounds"]:
        raise ValueError("expected_round_count is outside the released round budget")
    required = [name for name in dict.fromkeys(required_dimensions or []) if name in DIMENSION_NAMES]
    raw_counts = metrics.get("new_unique_query_intents_by_round", metrics.get("queries_by_round", {}))
    query_counts = {
        int(str(key)): int(value)
        for key, value in raw_counts.items()
        if str(key).lstrip("-").isdigit() and int(str(key)) > 0
    } if isinstance(raw_counts, Mapping) else {}
    raw_attempt_counts = metrics.get("actual_attempts_by_round", {})
    attempt_counts = {
        int(str(key)): int(value)
        for key, value in raw_attempt_counts.items()
        if str(key).lstrip("-").isdigit() and int(str(key)) > 0
    } if isinstance(raw_attempt_counts, Mapping) else {}
    round_numbers = sorted(set(query_counts) | set(attempt_counts))
    deepest = max(round_numbers, default=0)
    invalid_observed_rounds = [
        int(str(item.get("declared_iteration_round")))
        for item in metrics.get("invalid_query_records", [])
        if isinstance(item, Mapping) and str(item.get("declared_iteration_round", "")).isdigit()
    ]
    # Diagnostic gaps do not credit an invalid attempt as completed coverage.
    deepest = max(deepest, min(max(invalid_observed_rounds, default=0), expected))
    internal_missing = [number for number in range(1, deepest + 1) if number not in query_counts]
    future_missing = [number for number in range(deepest + 1, expected + 1)]
    errors: list[dict[str, object]] = []
    budget_audit = validate_query_budget_compliance(metrics, config=controls)
    errors.extend(dict(item) for item in budget_audit["errors"])
    invalid_query_records = metrics.get("invalid_query_records", [])
    if isinstance(invalid_query_records, list) and invalid_query_records:
        for item in invalid_query_records:
            if isinstance(item, Mapping):
                for error in item.get("errors", []):
                    if isinstance(error, Mapping):
                        errors.append(dict(error))
        if not any(item.get("code") for item in errors):
            errors.append(
                {
                    "code": "invalid_executed_query_record",
                    "message": "one or more executed query records violate the released contract",
                }
            )
    if internal_missing:
        errors.append(
            {
                "code": "missing_rounds",
                "message": "deep-search rounds do not form a continuous sequence from round 1",
                "rounds": internal_missing,
            }
        )
    duplicate_execution_ids = sorted(
        str(value) for value in metrics.get("duplicate_execution_ids", [])
    ) if isinstance(metrics.get("duplicate_execution_ids", []), list) else []
    if duplicate_execution_ids:
        errors.append(
            {
                "code": "duplicate_round_execution_conflict",
                "message": "one execution identifier appears more than once in the round sequence",
                "execution_ids": duplicate_execution_ids,
            }
        )

    round_details: dict[str, dict[str, object]] = {}
    completed_rounds: list[int] = []
    modes_by_round = metrics.get("iteration_modes_by_round", {})
    modes_by_dimension_round = metrics.get("iteration_modes_by_dimension_round", {})
    coverage_by_dimension = metrics.get("unique_query_intents_by_dimension_round", {})
    enhancement_by_dimension_round = metrics.get("enhancement_queries_by_dimension_round", {})
    completed_dimension_rounds: defaultdict[str, set[int]] = defaultdict(set)
    completed_enhancement_rounds: defaultdict[str, set[int]] = defaultdict(set)
    for number in round_numbers:
        raw_modes = modes_by_round.get(str(number), []) if isinstance(modes_by_round, Mapping) else []
        modes = {str(value).strip() for value in raw_modes} if isinstance(raw_modes, list) else set()
        minimum = budgets["minimum_targeted_queries_per_round"]
        count = query_counts.get(number, 0)
        attempt_count = attempt_counts.get(number, 0)
        covered = {
            dimension
            for dimension in DIMENSION_NAMES
            if isinstance(coverage_by_dimension, Mapping)
            and isinstance(coverage_by_dimension.get(dimension), Mapping)
            and int(coverage_by_dimension[dimension].get(str(number), 0) or 0) > 0
        }
        missing_targets = [dimension for dimension in required if dimension not in covered]
        round_errors: list[str] = []
        if count < minimum:
            round_errors.append("round_query_minimum_not_met")
            errors.append(
                {
                    "code": "round_query_minimum_not_met",
                    "message": f"round {number} has {count} executed queries; minimum is {minimum}",
                    "round": number,
                }
            )
        if count > budgets["per_round_independent_intent_budget"]:
            round_errors.append("round_independent_intent_budget_exceeded")
            errors.append(
                {
                    "code": "round_independent_intent_budget_exceeded",
                    "message": f"round {number} exceeds the released per-round independent-intent budget",
                    "round": number,
                }
            )
        if missing_targets:
            round_errors.append("round_target_coverage_incomplete")
            errors.append(
                {
                    "code": "round_target_coverage_incomplete",
                    "message": f"round {number} does not cover every required target dimension",
                    "round": number,
                    "missing_dimensions": missing_targets,
                }
            )
        dimension_details: dict[str, dict[str, object]] = {}
        for dimension in required:
            raw_dimension_modes = (
                modes_by_dimension_round.get(dimension, {}).get(str(number), [])
                if isinstance(modes_by_dimension_round, Mapping)
                and isinstance(modes_by_dimension_round.get(dimension), Mapping)
                else []
            )
            dimension_modes = (
                {str(value).strip() for value in raw_dimension_modes}
                if isinstance(raw_dimension_modes, list)
                else set()
            )
            mode = next(iter(dimension_modes)) if len(dimension_modes) == 1 else ""
            dimension_errors: list[str] = []
            if not dimension_modes or "" in dimension_modes:
                dimension_errors.append("dimension_round_mode_missing")
            elif len(dimension_modes) > 1:
                dimension_errors.append("dimension_round_mode_conflict")
            elif mode not in LEGAL_DEEP_ITERATION_MODES:
                dimension_errors.append("dimension_round_mode_invalid")
            dimension_minimum = (
                budgets["minimum_medium_enhancement_queries_per_round"]
                if mode == "medium_enhancement"
                else budgets["minimum_targeted_queries_per_round"]
            )
            coverage_actual = (
                int(coverage_by_dimension.get(dimension, {}).get(str(number), 0) or 0)
                if isinstance(coverage_by_dimension, Mapping)
                and isinstance(coverage_by_dimension.get(dimension), Mapping)
                else 0
            )
            enhancement_actual = (
                int(enhancement_by_dimension_round.get(dimension, {}).get(str(number), 0) or 0)
                if isinstance(enhancement_by_dimension_round, Mapping)
                and isinstance(enhancement_by_dimension_round.get(dimension), Mapping)
                else 0
            )
            actual = enhancement_actual if mode == "medium_enhancement" else coverage_actual
            if mode == "medium_enhancement" and enhancement_actual != coverage_actual:
                dimension_errors.append("dimension_enhancement_query_count_inconsistent")
            if actual < dimension_minimum:
                dimension_errors.append("dimension_round_query_minimum_not_met")
            complete = not dimension_errors
            if complete:
                completed_dimension_rounds[dimension].add(number)
                if mode == "medium_enhancement":
                    completed_enhancement_rounds[dimension].add(number)
            dimension_details[dimension] = {
                "actual_independent_query_count": actual,
                "coverage_independent_query_count": coverage_actual,
                "enhancement_independent_query_count": enhancement_actual,
                "minimum_independent_query_count": dimension_minimum,
                "iteration_mode": mode,
                "declared_modes": sorted(dimension_modes),
                "complete": complete,
                "error_codes": dimension_errors,
                "incomplete_reasons": list(dimension_errors),
            }
            for code in dimension_errors:
                errors.append(
                    {
                        "code": code,
                        "message": (
                            f"round {number} is incomplete for one required dimension: {code}"
                        ),
                        "round": number,
                        "dimension": dimension,
                        "actual_independent_query_count": actual,
                        "minimum_independent_query_count": dimension_minimum,
                        "iteration_mode": mode,
                    }
                )
                if code not in round_errors:
                    round_errors.append(code)
        if not round_errors and not any(missing < number for missing in internal_missing):
            completed_rounds.append(number)
        round_details[str(number)] = {
            "new_independent_query_intent_count": count,
            "actual_execution_attempt_count": attempt_count,
            "executed_query_count": attempt_count,
            "minimum_independent_query_intents": minimum,
            "minimum_executed_queries": minimum,
            "within_independent_intent_budget": count <= budgets["per_round_independent_intent_budget"],
            "within_attempt_budget": attempt_count <= budgets["per_round_attempt_budget"],
            "within_round_budget": (
                count <= budgets["per_round_independent_intent_budget"]
                and attempt_count <= budgets["per_round_attempt_budget"]
            ),
            "covered_dimensions": sorted(covered),
            "missing_required_dimensions": missing_targets,
            "dimension_details": dimension_details,
            "complete": not round_errors,
            "error_codes": round_errors,
        }

    dimension_completion: dict[str, dict[str, object]] = {}
    full_medium_contract = expected >= budgets["minimum_medium_total_targeted_rounds"]
    for dimension in required:
        completed_targeted = sorted(completed_dimension_rounds[dimension])
        completed_enhancement = sorted(completed_enhancement_rounds[dimension])
        dimension_errors: list[str] = []
        if full_medium_contract and deepest >= expected:
            if len(completed_targeted) < budgets["minimum_medium_total_targeted_rounds"]:
                dimension_errors.append("dimension_targeted_rounds_not_met")
            if len(completed_enhancement) < budgets["minimum_medium_enhancement_rounds"]:
                dimension_errors.append("dimension_enhancement_rounds_not_met")
        for code in dimension_errors:
            errors.append(
                {
                    "code": code,
                    "message": f"one required dimension has not completed the released round contract: {code}",
                    "dimension": dimension,
                    "completed_targeted_rounds": completed_targeted,
                    "completed_enhancement_rounds": completed_enhancement,
                }
            )
        dimension_completion[dimension] = {
            "completed_targeted_rounds": completed_targeted,
            "completed_enhancement_rounds": completed_enhancement,
            "minimum_targeted_rounds": budgets["minimum_medium_total_targeted_rounds"],
            "minimum_enhancement_rounds": budgets["minimum_medium_enhancement_rounds"],
            "complete": not dimension_errors and (
                not full_medium_contract or deepest < expected or len(completed_targeted) >= expected
            ),
            "error_codes": dimension_errors,
        }

    error_codes = sorted({str(item["code"]) for item in errors})
    if errors:
        status = "invalid"
    elif deepest < expected:
        status = "incomplete"
    else:
        status = "valid"
    return {
        "status": status,
        "error_codes": error_codes,
        "errors": errors,
        "round_numbers": round_numbers,
        "deepest_round": deepest,
        "expected_round_count": expected,
        "missing_rounds": internal_missing,
        "not_yet_executed_rounds": future_missing,
        "completed_rounds": completed_rounds,
        "round_details": round_details,
        "dimension_completion": dimension_completion,
        "required_dimensions": required,
        "sequence_continuous": not internal_missing,
        "round_budget_reached": status == "valid" and deepest == expected,
        "budget_compliance": budget_audit,
    }


def validate_round_completion(
    search_rows: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object] | None = None,
    required_dimensions: Iterable[str] | None = None,
    expected_round_count: int | None = None,
    schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate deep-search rounds as a continuous, substantive sequence.

    The wrapper deliberately derives its input from the same executed-query
    metrics used by planning, state recovery, evidence audit, and delivery
    validation.  No caller may substitute a maximum round number for this
    completion result.
    """

    return validate_round_metrics(
        executed_query_metrics(
            list(search_rows), config=config, schema_context=schema_context
        ),
        config=config,
        required_dimensions=required_dimensions,
        expected_round_count=expected_round_count,
    )


def validate_controlled_retries(
    search_rows: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object] | None = None,
    schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate every repeated executed intent as a bounded, linked retry."""

    controls = dict(config or load_retrieval_config())
    maximum = budget_values(controls)["maximum_controlled_retries_per_intent"]
    validation = validate_execution_records(
        list(search_rows), config=controls, schema_context=schema_context
    )
    retry_codes = {
        "first_attempt_has_retry_metadata",
        "controlled_retry_reason_invalid",
        "controlled_retry_original_mismatch",
        "controlled_retry_interval_invalid",
        "controlled_retry_sequence_invalid",
    }
    errors = [
        f"{item.get('execution_id', '')}: {code}"
        for item in validation["invalid_records"]
        for code in item.get("error_codes", [])
        if code in retry_codes
    ]
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "executed_attempt_count": len(validation["attempt_rows"]),
        "valid_execution_attempt_count": len(validation["valid_rows"]),
        "unique_query_intent_count": len(validation["first_attempt_ids"]),
        "controlled_retry_count": len(validation["controlled_retry_ids"]),
        "maximum_retries_per_intent": maximum,
    }


def current_round_dimension_quota(
    *,
    dimension: str,
    iteration_round: int,
    iteration_mode: str,
    metrics: Mapping[str, object],
    config: Mapping[str, object],
) -> dict[str, int | str | bool]:
    """Allocate current queries while reserving every still-mandatory round."""

    budgets = budget_values(config)
    # The per-dimension contract is a deep-search budget.  Baseline discovery
    # is separately bounded and can never consume capacity reserved for rounds.
    prior_by_dimension = metrics.get("deep_unique_query_intents_by_dimension", {})
    targeted = metrics.get("targeted_rounds_by_dimension", {})
    enhancements = metrics.get("enhancement_rounds_by_dimension", {})
    used = int(prior_by_dimension.get(dimension, 0) if isinstance(prior_by_dimension, Mapping) else 0)
    prior_targeted = set(targeted.get(dimension, []) if isinstance(targeted, Mapping) else [])
    prior_enhancement = set(enhancements.get(dimension, []) if isinstance(enhancements, Mapping) else [])
    current_is_enhancement = iteration_mode == "medium_enhancement"
    targeted_after = len(prior_targeted | {iteration_round})
    enhancement_after = len(prior_enhancement | ({iteration_round} if current_is_enhancement else set()))
    future_targeted = max(0, budgets["minimum_medium_total_targeted_rounds"] - targeted_after)
    future_enhancement = max(0, budgets["minimum_medium_enhancement_rounds"] - enhancement_after)
    future_enhancement = min(future_targeted, future_enhancement)
    future_regular = max(0, future_targeted - future_enhancement)
    reserved = (
        future_enhancement * budgets["minimum_medium_enhancement_queries_per_round"]
        + future_regular * budgets["minimum_targeted_queries_per_round"]
    )
    available_now = max(
        0,
        budgets["per_dimension_independent_intent_budget"] - used - reserved,
    )
    desired = (
        budgets["minimum_medium_enhancement_queries_per_round"]
        if current_is_enhancement
        else budgets["minimum_targeted_queries_per_round"]
    )
    quota = min(desired, available_now)
    return {
        "dimension": dimension,
        "iteration_round": iteration_round,
        "iteration_mode": iteration_mode,
        "used": used,
        "reserved_for_future_rounds": reserved,
        "available_now": available_now,
        "quota": quota,
        "budget_reached": used >= budgets["per_dimension_independent_intent_budget"],
    }


def _completed_contract_rounds(
    metrics: Mapping[str, object],
    dimension: str,
    budgets: Mapping[str, int],
) -> tuple[list[int], list[int]]:
    coverage = metrics.get("unique_query_intents_by_dimension_round", {})
    modes = metrics.get("iteration_modes_by_dimension_round", {})
    coverage_map = (
        coverage.get(dimension, {})
        if isinstance(coverage, Mapping) and isinstance(coverage.get(dimension), Mapping)
        else {}
    )
    mode_map = (
        modes.get(dimension, {})
        if isinstance(modes, Mapping) and isinstance(modes.get(dimension), Mapping)
        else {}
    )
    completed: list[int] = []
    enhancements: list[int] = []
    for raw_round, raw_count in coverage_map.items():
        if not str(raw_round).isdigit() or int(str(raw_round)) <= 0:
            continue
        number = int(str(raw_round))
        raw_modes = mode_map.get(str(number), [])
        declared = {str(value) for value in raw_modes} if isinstance(raw_modes, list) else set()
        if len(declared) != 1:
            continue
        mode = next(iter(declared))
        if mode not in LEGAL_DEEP_ITERATION_MODES:
            continue
        minimum = (
            budgets["minimum_medium_enhancement_queries_per_round"]
            if mode == "medium_enhancement"
            else budgets["minimum_targeted_queries_per_round"]
        )
        if int(raw_count or 0) < minimum:
            continue
        completed.append(number)
        if mode == "medium_enhancement":
            enhancements.append(number)
    return sorted(completed), sorted(enhancements)


def dimension_round_schedule(
    *,
    dimension: str,
    iteration_round: int,
    metrics: Mapping[str, object],
    config: Mapping[str, object],
    confidence: str = "",
) -> dict[str, object]:
    """Choose a contract-completable mode instead of reacting to one confidence snapshot."""

    if dimension not in DIMENSION_NAMES:
        raise ValueError("dimension is not canonical")
    budgets = budget_values(config)
    completed, enhancements = _completed_contract_rounds(metrics, dimension, budgets)
    total_required = budgets["minimum_medium_total_targeted_rounds"]
    enhancement_required = budgets["minimum_medium_enhancement_rounds"]
    remaining_slots = max(0, total_required - len(completed))
    enhancement_needed = max(0, enhancement_required - len(enhancements))
    errors: list[str] = []
    if iteration_round < 1 or iteration_round > budgets["maximum_iteration_rounds"]:
        errors.append("round_out_of_bounds")
    if remaining_slots <= 0:
        errors.append("targeted_round_contract_already_complete")
    if enhancement_needed > remaining_slots:
        errors.append("insufficient_round_slots_for_enhancement_contract")
    force_enhancement = enhancement_needed > 0 and enhancement_needed >= remaining_slots
    mode = "medium_enhancement" if force_enhancement else "evidence_gap_fill"
    minimum = (
        budgets["minimum_medium_enhancement_queries_per_round"]
        if mode == "medium_enhancement"
        else budgets["minimum_targeted_queries_per_round"]
    )
    path_state = (
        "enhancement_in_progress"
        if mode == "medium_enhancement"
        else "low_confidence_gap_fill"
        if confidence in {"", "数据不足", "低", "待深检"}
        else "contract_gap_fill"
    )
    return {
        "status": "feasible" if not errors else "infeasible",
        "dimension": dimension,
        "iteration_round": iteration_round,
        "confidence": confidence,
        "iteration_mode": mode,
        "path_state": path_state,
        "minimum_query_count": minimum,
        "completed_targeted_rounds": completed,
        "completed_enhancement_rounds": enhancements,
        "remaining_targeted_round_slots": remaining_slots,
        "remaining_enhancement_rounds": enhancement_needed,
        "errors": errors,
    }


def planning_budget_account(
    *, intent_used: int, intent_limit: int, attempt_used: int, attempt_limit: int,
    retry_used: int, retry_limit: int, required_now: int, required_future: int,
) -> dict[str, object]:
    """Keep coverage, resources, and retry reserve in separate units."""
    from execution_facts import integer

    for value in (intent_used, intent_limit, attempt_used, attempt_limit,
                  retry_used, retry_limit, required_now, required_future):
        integer(value)
    intent_remaining = intent_limit - intent_used
    attempt_remaining = attempt_limit - attempt_used
    retry_remaining = retry_limit - retry_used
    reasons = []
    if intent_remaining < required_now + required_future:
        reasons.append('independent_intent_budget_infeasible')
    if attempt_remaining < required_now + required_future + max(0, retry_remaining):
        reasons.append('actual_attempt_budget_infeasible')
    if retry_remaining < 0:
        reasons.append('retry_budget_infeasible')
    return {
        'independent_intent_used': intent_used,
        'independent_intent_remaining': intent_remaining,
        'actual_attempt_used': attempt_used,
        'actual_attempt_remaining': attempt_remaining,
        'retry_used': retry_used,
        'retry_remaining': retry_remaining,
        'new_intents_required_this_round': required_now,
        'minimum_future_new_intents': required_future,
        'minimum_future_attempts': required_future,
        'planning_feasible': not reasons,
        'infeasible_reason': reasons,
    }


def deep_search_budget_feasibility(
    *,
    dimensions: Iterable[str],
    iteration_round: int,
    metrics: Mapping[str, object],
    config: Mapping[str, object],
    dimension_confidences: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Preflight every remaining round against one shared budget interpretation."""

    budgets = budget_values(config)
    targets = [name for name in dict.fromkeys(dimensions) if name in DIMENSION_NAMES]
    confidences = dimension_confidences or {}
    errors: list[dict[str, object]] = []
    budget_audit = validate_query_budget_compliance(metrics, config=config)
    if metrics.get("query_error_codes"):
        errors.append(
            {
                "code": "invalid_prior_query_metrics",
                "details": list(metrics.get("query_error_codes", [])),
            }
        )
    errors.extend(dict(item) for item in budget_audit["errors"])
    baseline_attempts_used = int(metrics.get("baseline_executed_query_count", 0) or 0)
    deep_attempts_used = int(metrics.get("deep_executed_query_count", 0) or 0)
    baseline_intents_used = int(metrics.get("baseline_unique_query_intent_count", 0) or 0)
    deep_intents_used = int(metrics.get("deep_unique_query_intent_count", 0) or 0)
    deep_retries_used = int(metrics.get("deep_controlled_retry_count", 0) or 0)

    schedules: dict[str, list[dict[str, object]]] = {}
    per_dimension_required: dict[str, int] = {}
    simulated_modes: dict[int, dict[str, str]] = defaultdict(dict)
    simulated_minimums: dict[int, dict[str, int]] = defaultdict(dict)
    for dimension in targets:
        local_metrics = dict(metrics)
        completed, enhanced = _completed_contract_rounds(local_metrics, dimension, budgets)
        completed_count = len(completed)
        enhanced_count = len(enhanced)
        schedule: list[dict[str, object]] = []
        next_round = iteration_round
        while completed_count < budgets["minimum_medium_total_targeted_rounds"]:
            remaining_slots = budgets["minimum_medium_total_targeted_rounds"] - completed_count
            enhancement_needed = max(
                0, budgets["minimum_medium_enhancement_rounds"] - enhanced_count
            )
            if next_round > budgets["maximum_iteration_rounds"]:
                errors.append(
                    {
                        "code": "insufficient_round_slots_for_contract",
                        "dimension": dimension,
                        "next_round": next_round,
                    }
                )
                break
            mode = (
                "medium_enhancement"
                if enhancement_needed > 0 and enhancement_needed >= remaining_slots
                else "evidence_gap_fill"
            )
            minimum = (
                budgets["minimum_medium_enhancement_queries_per_round"]
                if mode == "medium_enhancement"
                else budgets["minimum_targeted_queries_per_round"]
            )
            entry = {
                "round": next_round,
                "iteration_mode": mode,
                "minimum_query_count": minimum,
            }
            schedule.append(entry)
            simulated_modes[next_round][dimension] = mode
            simulated_minimums[next_round][dimension] = minimum
            completed_count += 1
            if mode == "medium_enhancement":
                enhanced_count += 1
            next_round += 1
        schedules[dimension] = schedule
        required = sum(int(item["minimum_query_count"]) for item in schedule)
        per_dimension_required[dimension] = required
        deep_by_dimension = metrics.get("deep_unique_query_intents_by_dimension", {})
        used = int(
            deep_by_dimension.get(dimension, 0)
            if isinstance(deep_by_dimension, Mapping)
            else 0
        )
        if used + required > budgets["per_dimension_independent_intent_budget"]:
            errors.append(
                {
                    "code": "per_dimension_deep_budget_infeasible",
                    "dimension": dimension,
                    "used": used,
                    "required": required,
                    "maximum": budgets["per_dimension_independent_intent_budget"],
                }
            )
        dimension_attempts = metrics.get("deep_queries_by_dimension", {})
        dimension_retries = metrics.get("deep_controlled_retries_by_dimension", {})
        attempts_used = int(
            dimension_attempts.get(dimension, 0)
            if isinstance(dimension_attempts, Mapping)
            else 0
        )
        retries_used = int(
            dimension_retries.get(dimension, 0)
            if isinstance(dimension_retries, Mapping)
            else 0
        )
        remaining_retry_reserve = max(
            0, budgets["per_dimension_retry_budget"] - retries_used
        )
        if (
            attempts_used + required + remaining_retry_reserve
            > budgets["per_dimension_attempt_budget"]
        ):
            errors.append(
                {
                    "code": "per_dimension_attempt_budget_infeasible",
                    "dimension": dimension,
                    "attempts_used": attempts_used,
                    "required_independent_intents": required,
                    "reserved_retries": remaining_retry_reserve,
                    "maximum": budgets["per_dimension_attempt_budget"],
                }
            )

    # Queries can be shared only inside the released clusters and only by
    # dimensions with the same mode.  Mixed modes therefore create extra
    # groups and are budgeted explicitly instead of assumed away.
    global_required = 0
    per_round_required: dict[str, int] = {}
    for number in sorted(simulated_modes):
        required = 0
        for cluster in (
            (DIMENSION_NAMES[0], DIMENSION_NAMES[1]),
            (DIMENSION_NAMES[2], DIMENSION_NAMES[3]),
            (DIMENSION_NAMES[4], DIMENSION_NAMES[5], DIMENSION_NAMES[6]),
        ):
            by_mode: defaultdict[str, list[str]] = defaultdict(list)
            for dimension in cluster:
                mode = simulated_modes[number].get(dimension)
                if mode:
                    by_mode[mode].append(dimension)
            for grouped in by_mode.values():
                required += max(simulated_minimums[number][name] for name in grouped)
        per_round_required[str(number)] = required
        global_required += required
        if required > budgets["per_round_independent_intent_budget"]:
            errors.append(
                {
                    "code": "per_round_deep_budget_infeasible",
                    "round": number,
                    "required": required,
                    "maximum": budgets["per_round_independent_intent_budget"],
                }
            )
        if (
            required + budgets["per_round_retry_budget"]
            > budgets["per_round_attempt_budget"]
        ):
            errors.append(
                {
                    "code": "per_round_attempt_budget_infeasible",
                    "round": number,
                    "required_independent_intents": required,
                    "reserved_retries": budgets["per_round_retry_budget"],
                    "maximum": budgets["per_round_attempt_budget"],
                }
            )
    if deep_intents_used + global_required > budgets["global_independent_intent_budget"]:
        errors.append(
            {
                "code": "global_deep_budget_infeasible",
                "used": deep_intents_used,
                "required": global_required,
                "maximum": budgets["global_independent_intent_budget"],
            }
        )
    remaining_global_retry_reserve = max(
        0, budgets["global_retry_budget"] - deep_retries_used
    )
    if (
        deep_attempts_used + global_required + remaining_global_retry_reserve
        > budgets["global_attempt_budget"]
    ):
        errors.append(
            {
                "code": "global_attempt_budget_infeasible",
                "attempts_used": deep_attempts_used,
                "required_independent_intents": global_required,
                "reserved_retries": remaining_global_retry_reserve,
                "maximum": budgets["global_attempt_budget"],
            }
        )
    if (
        baseline_attempts_used
        + deep_attempts_used
        + global_required
        + remaining_global_retry_reserve
        > budgets["total_query_safety_limit"]
    ):
        errors.append(
            {
                "code": "total_query_safety_limit_infeasible",
                "baseline_used": baseline_attempts_used,
                "deep_used": deep_attempts_used,
                "required": global_required,
                "reserved_retries": remaining_global_retry_reserve,
                "maximum": budgets["total_query_safety_limit"],
            }
        )
    accounts = {}
    for dimension, required in per_dimension_required.items():
        schedule = schedules[dimension]
        now = int(schedule[0]['minimum_query_count']) if schedule else 0
        accounts[dimension] = planning_budget_account(
            intent_used=int(metrics.get('deep_unique_query_intents_by_dimension', {}).get(dimension, 0)),
            intent_limit=budgets['per_dimension_independent_intent_budget'],
            attempt_used=int(metrics.get('deep_queries_by_dimension', {}).get(dimension, 0)),
            attempt_limit=budgets['per_dimension_attempt_budget'],
            retry_used=int(metrics.get('deep_controlled_retries_by_dimension', {}).get(dimension, 0)),
            retry_limit=budgets['per_dimension_retry_budget'], required_now=now, required_future=required-now,
        )
    now = int(per_round_required.get(str(iteration_round), 0))
    global_account = planning_budget_account(
        intent_used=deep_intents_used, intent_limit=budgets['global_independent_intent_budget'],
        attempt_used=deep_attempts_used, attempt_limit=budgets['global_attempt_budget'],
        retry_used=deep_retries_used, retry_limit=budgets['global_retry_budget'],
        required_now=now, required_future=global_required-now,
    )
    return {
        "status": "feasible" if not errors else "infeasible",
        "planning_feasible": not errors,
        "infeasible_reason": sorted({str(e['code']) for e in errors}),
        "budget_accounts": {"global": global_account, "dimensions": accounts},
        "error_code": "" if not errors else "budget_configuration_infeasible",
        "errors": errors,
        "iteration_round": iteration_round,
        "target_dimensions": targets,
        "dimension_schedules": schedules,
        "per_dimension_remaining_query_requirement": per_dimension_required,
        "per_round_remaining_query_requirement": per_round_required,
        "global_remaining_query_requirement": global_required,
        "baseline_query_count": baseline_intents_used,
        "baseline_attempt_count": baseline_attempts_used,
        "deep_query_count": deep_intents_used,
        "deep_attempt_count": deep_attempts_used,
        "remaining_baseline_budget": max(
            0, budgets["baseline_independent_intent_budget"] - baseline_intents_used
        ),
        "remaining_baseline_attempt_budget": max(
            0, budgets["baseline_attempt_budget"] - baseline_attempts_used
        ),
        "remaining_deep_global_budget": max(
            0, budgets["global_independent_intent_budget"] - deep_intents_used
        ),
        "remaining_deep_attempt_budget": max(
            0, budgets["global_attempt_budget"] - deep_attempts_used
        ),
        "remaining_total_safety_capacity": max(
            0,
            budgets["total_query_safety_limit"]
            - baseline_attempts_used
            - deep_attempts_used,
        ),
    }


def _route_key(row: Mapping[str, object], dimension: str) -> tuple[str, ...]:
    from execution_semantics import route_id
    return (
        dimension,
        str(row.get("target_platform_id", "")).strip(),
        str(row.get("source_category_target", "")).strip(),
        route_id(row),
        str(row.get("polarity", "")).strip(),
        str(row.get("target_subject") or row.get("subject_scope") or "").strip(),
        str(row.get("target_time_range") or row.get("time_context") or "").strip(),
    )


def scoped_dimension_termination(
    search_rows: Iterable[Mapping[str, object]],
    remaining_dimensions: Iterable[str],
    *,
    config: Mapping[str, object],
    schema_context: Mapping[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    budgets = budget_values(config)
    all_rows = list(search_rows)
    active_statuses = {"continue", "retry", "pending", "running"}
    valid_executions = list(
        validate_execution_records(all_rows, config=config, schema_context=schema_context)["valid_rows"]
    )
    valid_execution_ids = {
        str(row.get("execution_id", "")) for row in valid_executions
    }
    active_rows = [
        row
        for row in all_rows
        if str(row.get("record_type", "")).strip() not in PLAN_RECORD_TYPES
        and str(row.get("execution_id", "")) not in valid_execution_ids
        and (
            str(row.get("status", "")).strip().casefold() in active_statuses
            or str(row.get("next_action", "")).strip().casefold() in active_statuses
        )
    ]
    # Uncredentialed state rows cannot override validated execution events.
    route_state_rows = [row for row in valid_executions if not row.get('_retracted')]
    output: dict[str, dict[str, object]] = {}
    for dimension in remaining_dimensions:
        rows = [row for row in route_state_rows if dimension in parse_dimension_targets(row)]
        route_rows: defaultdict[tuple[str, ...], list[Mapping[str, object]]] = defaultdict(list)
        for row in rows:
            route_rows[_route_key(row, dimension)].append(row)
        terminal_routes: list[tuple[str, ...]] = []
        blocked_routes: list[tuple[str, ...]] = []
        exhausted_routes: list[tuple[str, ...]] = []
        active_routes: list[tuple[str, ...]] = []
        latest_route_states: list[dict[str, object]] = []
        from execution_facts import event_key
        for route, items in sorted(route_rows.items()):
            items = sorted(items, key=event_key)
            latest = items[-1]
            statuses = [str(item.get("status", "")).strip().casefold() for item in items]
            latest_status = statuses[-1]
            latest_action = str(latest.get("next_action", "")).strip().casefold()
            fresh_evidence = int(str(latest.get("new_scored_evidence_units", "0") or "0")) > 0
            active = (
                latest_status in active_statuses
                or latest_action in active_statuses
                or (fresh_evidence and latest_action != "exhausted")
            )
            explicit = latest_action == "exhausted" and not active
            repeated_block = (
                len(items) >= budgets["repeated_source_failure_limit"]
                and all(
                    status in {"blocked", "no_results", "failed"}
                    for status in statuses[-budgets["repeated_source_failure_limit"] :]
                )
                and not active
            )
            if explicit or repeated_block:
                terminal_routes.append(route)
            else:
                active_routes.append(route)
            if (explicit or repeated_block) and latest_status in {"blocked", "no_results", "failed"}:
                blocked_routes.append(route)
            if explicit and latest_status not in {"blocked", "no_results", "failed"}:
                exhausted_routes.append(route)
            latest_route_states.append(
                {
                    "route": list(route),
                    "status": latest_status,
                    "next_action": latest_action,
                    "fresh_scored_evidence": fresh_evidence,
                    "terminal": explicit or repeated_block,
                }
            )
        categories = {route[2] for route in terminal_routes if route[2] and route[2] != "mixed"}
        supported = bool(route_rows) and len(terminal_routes) == len(route_rows)
        reason = ""
        if supported:
            reason = "source_blocked" if len(blocked_routes) == len(route_rows) else "exhausted"
        output[dimension] = {
            "terminal": supported,
            "termination_reason": reason,
            "attempted_route_count": len(route_rows),
            "terminal_route_count": len(terminal_routes),
            "terminal_source_categories": sorted(categories),
            "blocked_route_count": len(blocked_routes),
            "exhausted_route_count": len(exhausted_routes),
            "active_route_count": len(active_routes),
            "latest_route_states": latest_route_states,
        }
    return output


def verified_dimension_exhaustion(
    dimension: str,
    result: Mapping[str, object],
    metrics: Mapping[str, object],
    *,
    config: Mapping[str, object],
) -> dict[str, object]:
    """Verify a shortfall terminal from machine facts instead of trusting a label.

    The dimension audit remains the authority for the released exhaustion
    criteria.  This adapter checks that every required criterion is present and
    that its round/count claims agree with the independently derived query
    metrics.  A hand-written ``status`` value therefore cannot remove a
    dimension from the actionable retrieval set.
    """

    budgets = budget_values(config)
    checks = result.get("exhaustion_checks")
    reported_counts = result.get("targeted_query_counts")
    actual_by_dimension_round = metrics.get("unique_query_intents_by_dimension_round")
    actual_counts = (
        actual_by_dimension_round.get(dimension, {})
        if isinstance(actual_by_dimension_round, Mapping)
        and isinstance(actual_by_dimension_round.get(dimension), Mapping)
        else {}
    )
    errors: list[str] = []
    binding = result.get("exhaustion_machine_binding")
    expected_signature = dimension_exhaustion_signature(dimension, result, metrics)
    if not isinstance(binding, Mapping):
        errors.append("exhaustion_machine_binding_missing")
    else:
        if binding.get("schema_version") != "dimension-exhaustion-binding-1":
            errors.append("exhaustion_machine_binding_schema_invalid")
        if str(binding.get("sha256", "")) != expected_signature:
            errors.append("exhaustion_machine_binding_mismatch")
    if str(result.get("status", "")) != "exhausted_with_shortfall":
        errors.append("status_not_exhausted_with_shortfall")
    if result.get("numeric_score_permitted") is not False:
        errors.append("numeric_score_not_disabled")
    if result.get("exhaustion_supported") is not True:
        errors.append("exhaustion_not_supported")
    if result.get("independent_exhaustion_audit_passed") is not True:
        errors.append("independent_exhaustion_audit_not_passed")
    if not str(result.get("exhaustion_basis", "")).strip():
        errors.append("exhaustion_basis_missing")
    if not isinstance(checks, Mapping):
        errors.append("exhaustion_checks_missing")
    else:
        mandatory = {
            "minimum_four_targeted_rounds",
            "minimum_eight_executed_queries_each_round",
            "minimum_five_target_source_categories",
            "final_round_exhausted_action",
        }
        if any(checks.get(key) is not True for key in mandatory):
            errors.append("mandatory_exhaustion_checks_not_met")
        if not (
            checks.get("cross_ledger_low_yield_route") is True
            or checks.get("cross_ledger_blocked_route") is True
        ):
            errors.append("cross_ledger_exhaustion_route_missing")
    rounds = result.get("targeted_deep_rounds")
    if not isinstance(rounds, list) or len(rounds) < 4:
        errors.append("targeted_round_trace_incomplete")
        rounds = []
    if not isinstance(reported_counts, Mapping):
        errors.append("targeted_query_counts_missing")
        reported_counts = {}
    for raw_round in rounds:
        if not str(raw_round).isdigit():
            errors.append("targeted_round_invalid")
            continue
        number = int(str(raw_round))
        reported = int(reported_counts.get(str(number), 0) or 0)
        actual = int(actual_counts.get(str(number), 0) or 0)
        if reported != actual:
            errors.append("targeted_query_count_mismatch")
        if actual < budgets["minimum_targeted_queries_per_round"]:
            errors.append("targeted_round_below_minimum")
    return {
        "dimension": dimension,
        "verified": not errors,
        "errors": sorted(set(errors)),
        "reported_rounds": [int(value) for value in rounds if str(value).isdigit()],
    }


def dimension_exhaustion_signature(
    dimension: str,
    result: Mapping[str, object],
    metrics: Mapping[str, object],
) -> str:
    """Return the deterministic binding for a dimension exhaustion decision.

    Only the released evidence audit should emit this value.  It binds the
    decision fields to query facts re-derived from the execution ledger, so a
    copied or hand-written terminal label is not sufficient for delivery.
    """

    actual_by_dimension_round = metrics.get("unique_query_intents_by_dimension_round")
    raw_actual = (
        actual_by_dimension_round.get(dimension, {})
        if isinstance(actual_by_dimension_round, Mapping)
        and isinstance(actual_by_dimension_round.get(dimension), Mapping)
        else {}
    )
    raw_rounds = result.get("targeted_deep_rounds")
    rounds = sorted(
        {
            int(str(value))
            for value in raw_rounds
            if str(value).isdigit()
        }
    ) if isinstance(raw_rounds, list) else []
    raw_reported = result.get("targeted_query_counts")
    reported = {
        str(number): int(raw_reported.get(str(number), 0) or 0)
        for number in rounds
    } if isinstance(raw_reported, Mapping) else {}
    actual = {
        str(number): int(raw_actual.get(str(number), 0) or 0)
        for number in rounds
    }
    raw_checks = result.get("exhaustion_checks")
    checks = {
        str(key): bool(value)
        for key, value in sorted(raw_checks.items(), key=lambda item: str(item[0]))
    } if isinstance(raw_checks, Mapping) else {}
    payload = {
        "schema_version": "dimension-exhaustion-binding-1",
        "dimension": dimension,
        "status": str(result.get("status", "")),
        "numeric_score_permitted": result.get("numeric_score_permitted"),
        "exhaustion_supported": result.get("exhaustion_supported"),
        "independent_exhaustion_audit_passed": result.get(
            "independent_exhaustion_audit_passed"
        ),
        "exhaustion_basis": str(result.get("exhaustion_basis", "")),
        "targeted_deep_rounds": rounds,
        "targeted_query_counts": reported,
        "actual_query_counts": actual,
        "exhaustion_checks": checks,
        "query_integrity_error_codes": sorted(
            str(value) for value in metrics.get("query_error_codes", [])
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dimension_medium_terminal_signature(
    dimension: str,
    result: Mapping[str, object],
    metrics: Mapping[str, object],
) -> str:
    actual_by_dimension_round = metrics.get(
        "new_unique_query_intents_by_dimension_round",
        metrics.get("unique_query_intents_by_dimension_round", {}),
    )
    actual = (
        actual_by_dimension_round.get(dimension, {})
        if isinstance(actual_by_dimension_round, Mapping)
        and isinstance(actual_by_dimension_round.get(dimension), Mapping)
        else {}
    )
    checks = result.get("medium_completion_checks")
    payload = {
        "schema_version": "dimension-medium-terminal-binding-1",
        "dimension": dimension,
        "status": str(result.get("status", "")),
        "confidence": str(result.get("confidence", "")),
        "scoring_confidence": str(result.get("scoring_confidence", "")),
        "target_confidence": str(result.get("target_confidence", "")),
        "numeric_score_permitted": result.get("numeric_score_permitted"),
        "medium_completion_audit_passed": result.get(
            "medium_completion_audit_passed"
        ),
        "medium_completion_basis": str(result.get("medium_completion_basis", "")),
        "medium_completion_checks": {
            str(key): bool(value)
            for key, value in sorted(checks.items(), key=lambda item: str(item[0]))
        }
        if isinstance(checks, Mapping)
        else {},
        "targeted_query_counts": {
            str(key): int(value or 0)
            for key, value in sorted(
                (result.get("targeted_query_counts") or {}).items(),
                key=lambda item: str(item[0]),
            )
        }
        if isinstance(result.get("targeted_query_counts"), Mapping)
        else {},
        "actual_query_counts": {
            str(key): int(value or 0)
            for key, value in sorted(actual.items(), key=lambda item: str(item[0]))
        },
        "valid_execution_set_sha256": str(
            metrics.get("valid_execution_set_sha256", "")
        ),
        "query_integrity_error_codes": sorted(
            str(value) for value in metrics.get("query_error_codes", [])
        ),
    }
    return canonical_sha256(payload)


def verified_medium_numeric_terminal(
    dimension: str,
    result: Mapping[str, object],
    metrics: Mapping[str, object],
) -> dict[str, object]:
    errors: list[str] = []
    binding = result.get("medium_terminal_machine_binding")
    if str(result.get("status", "")) != "sufficient_at_medium_after_audit":
        errors.append("status_not_medium_terminal")
    if str(result.get("confidence", "")) != "中":
        errors.append("confidence_not_medium")
    if result.get("numeric_score_permitted") is not True:
        errors.append("numeric_score_not_permitted")
    if result.get("medium_completion_audit_passed") is not True:
        errors.append("medium_completion_audit_not_passed")
    if not str(result.get("medium_completion_basis", "")).strip():
        errors.append("medium_completion_basis_missing")
    if not isinstance(binding, Mapping):
        errors.append("medium_terminal_machine_binding_missing")
    else:
        if binding.get("schema_version") != "dimension-medium-terminal-binding-1":
            errors.append("medium_terminal_machine_binding_schema_invalid")
        if str(binding.get("sha256", "")) != dimension_medium_terminal_signature(
            dimension, result, metrics
        ):
            errors.append("medium_terminal_machine_binding_mismatch")
    return {
        "dimension": dimension,
        "verified": not errors,
        "errors": sorted(set(errors)),
    }


def retrieval_termination(
    search_rows: list[Mapping[str, object]],
    dimension_results: Mapping[str, Mapping[str, object]],
    *,
    target_confidence: str,
    config: Mapping[str, object] | None = None,
    protocol: Mapping[str, object] | None = None,
    unrecoverable_error: str = "",
    schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    controls = dict(config or load_retrieval_config())
    budgets = budget_values(controls)
    metrics = executed_query_metrics(
        search_rows, config=controls, schema_context=schema_context
    )
    budget_compliance = validate_query_budget_compliance(metrics, config=controls)
    target_status = dimension_target_status(
        dimension_results,
        target_confidence=target_confidence,
        protocol=protocol,
    )
    below_target = list(target_status["remaining_dimensions"])
    target_met = bool(target_status["target_met"])
    rounds = [
        int(key)
        for key in metrics.get("actual_attempts_by_round", {})
        if int(key) > 0
    ]
    exhaustion_verification = {
        name: verified_dimension_exhaustion(
            name,
            dimension_results.get(name, {}),
            metrics,
            config=controls,
        )
        for name in below_target
    }
    medium_terminal_verification = {
        name: verified_medium_numeric_terminal(
            name, dimension_results.get(name, {}), metrics
        )
        for name in below_target
    }
    dimension_lifecycle = {
        name: formal_dimension_lifecycle(
            dimension_results.get(name, {}),
            requested_target_met=name not in below_target,
            medium_terminal_verified=bool(
                medium_terminal_verification.get(name, {}).get("verified")
            ),
            exhaustion_verified=bool(
                exhaustion_verification.get(name, {}).get("verified")
            ),
        )
        for name in DIMENSION_NAMES
    }
    target_met = all(
        dimension_lifecycle[name]["lifecycle"] == "requested_target_reached"
        for name in DIMENSION_NAMES
    )
    verified_exhausted = [
        name
        for name in below_target
        if dimension_lifecycle[name]["lifecycle"] == "verified_exhausted_shortfall"
    ]
    verified_medium_terminal = [
        name
        for name in below_target
        if dimension_lifecycle[name]["lifecycle"] == "medium_audited_numeric_terminal"
    ]
    remaining = [name for name in DIMENSION_NAMES if dimension_lifecycle[name]["actionable"]]
    round_completion = validate_round_completion(
        search_rows,
        config=controls,
        required_dimensions=remaining,
        expected_round_count=budgets["maximum_iteration_rounds"],
        schema_context=schema_context,
    )
    latest = sorted(rounds)[-int(budgets["consecutive_low_gain_rounds"]) :]
    low_gain = (
        len(latest) == int(budgets["consecutive_low_gain_rounds"])
        and all(int(metrics["gain_by_round"].get(str(number), 0)) < 2 for number in latest)
    )
    dimension_routes = scoped_dimension_termination(
        search_rows, remaining, config=controls, schema_context=schema_context)
    per_dimension_budget = {
        name: (
            int(metrics["deep_queries_by_dimension"].get(name, 0))
            >= budgets["per_dimension_attempt_budget"]
            or int(metrics["deep_unique_query_intents_by_dimension"].get(name, 0))
            >= budgets["per_dimension_independent_intent_budget"]
        )
        for name in DIMENSION_NAMES
    }
    target_met_dimensions = [
        name
        for name in DIMENSION_NAMES
        if dimension_lifecycle[name]["lifecycle"] == "requested_target_reached"
    ]
    budget_reached_dimensions = [name for name in remaining if per_dimension_budget[name]]
    route_terminal_dimensions = [name for name in remaining if dimension_routes[name]["terminal"]]
    all_remaining_route_terminal = bool(remaining) and set(route_terminal_dimensions) == set(remaining)
    all_remaining_dimension_budget = bool(remaining) and set(budget_reached_dimensions) == set(remaining)
    # Once deep search has started, a shortfall terminal is not available
    # until the prescribed sequence is both continuous and substantively
    # complete.  This prevents route, global, or dimension budget flags from
    # bypassing unfinished rounds.
    round_sequence_blocks_terminal = bool(rounds) and round_completion["status"] != "valid"
    hard_round_error_codes = {
        "round_number_out_of_bounds",
        "round_query_budget_exceeded",
        "round_independent_intent_budget_exceeded",
        "round_retry_budget_exceeded",
        "dimension_query_budget_exceeded",
        "dimension_independent_intent_budget_exceeded",
        "dimension_retry_budget_exceeded",
        "dimension_round_query_budget_exceeded",
        "dimension_round_independent_intent_budget_exceeded",
        "dimension_round_retry_budget_exceeded",
        "missing_rounds",
        "dimension_round_mode_invalid",
        "dimension_round_mode_conflict",
        "dimension_enhancement_query_count_inconsistent",
    }
    retrieval_control_invalid = (
        bool(metrics.get("query_error_codes"))
        or budget_compliance["status"] != "valid"
        or bool(
        set(round_completion.get("error_codes", [])) & hard_round_error_codes
        )
    )
    deep_query_count = int(metrics["deep_executed_query_count"])
    total_query_count = int(metrics["executed_query_count"])
    all_below_target_formally_terminal = bool(below_target) and not remaining
    all_below_target_verified_exhausted = (
        all_below_target_formally_terminal and bool(verified_exhausted)
    )
    if unrecoverable_error:
        status, terminal = RETRIEVAL_UNRECOVERABLE_ERROR, True
    elif retrieval_control_invalid:
        status, terminal = RETRIEVAL_CONTROL_INVALID, False
    elif target_met:
        status, terminal = RETRIEVAL_TARGET_MET, True
    elif all_below_target_verified_exhausted:
        status, terminal = RETRIEVAL_DIMENSION_EXHAUSTED_WITH_SHORTFALL, True
    elif all_below_target_formally_terminal:
        status, terminal = RETRIEVAL_ALL_DIMENSIONS_FORMALLY_TERMINAL, True
    elif round_sequence_blocks_terminal:
        status, terminal = RETRIEVAL_ROUND_SEQUENCE_INCOMPLETE, False
    elif (
        deep_query_count >= budgets["global_attempt_budget"]
        or int(metrics.get("deep_unique_query_intent_count", 0))
        >= budgets["global_independent_intent_budget"]
    ):
        status, terminal = RETRIEVAL_QUERY_BUDGET_REACHED, False
    elif round_completion["round_budget_reached"] is True:
        status, terminal = RETRIEVAL_ROUND_BUDGET_REACHED, False
    elif all_remaining_dimension_budget:
        status, terminal = RETRIEVAL_DIMENSION_BUDGET_REACHED, False
    elif all_remaining_route_terminal:
        reasons = {str(dimension_routes[name]["termination_reason"]) for name in remaining}
        status, terminal = (
            RETRIEVAL_SOURCE_BLOCKED_PENDING_AUDIT
            if reasons == {"source_blocked"}
            else RETRIEVAL_ROUTE_EXHAUSTED_PENDING_AUDIT
        ), False
    else:
        status, terminal = RETRIEVAL_EVIDENCE_SHORTFALL, False
    dimension_completion = round_completion.get("dimension_completion", {})
    round_budget_dimensions = [
        name
        for name in remaining
        if isinstance(dimension_completion, Mapping)
        and isinstance(dimension_completion.get(name), Mapping)
        and dimension_completion[name].get("complete") is True
        and round_completion.get("deepest_round") == budgets["maximum_iteration_rounds"]
    ]
    return {
        "termination_status": status,
        "terminal": terminal,
        "target_met": target_met,
        "target_confidence": target_confidence,
        "below_target_dimensions": below_target,
        "remaining_dimensions": remaining,
        "remaining_dimension_count": len(remaining),
        "verified_exhausted_dimensions": verified_exhausted,
        "verified_medium_terminal_dimensions": verified_medium_terminal,
        "exhaustion_verification": exhaustion_verification,
        "medium_terminal_verification": medium_terminal_verification,
        "dimension_lifecycle": dimension_lifecycle,
        "invalid_terminal_dimensions": [
            name
            for name in below_target
            if str(dimension_results.get(name, {}).get("status", ""))
            == "exhausted_with_shortfall"
            and name not in verified_exhausted
        ],
        "budget_reached_with_shortfall": (
            deep_query_count >= budgets["global_attempt_budget"]
            or int(metrics.get("deep_unique_query_intent_count", 0))
            >= budgets["global_independent_intent_budget"]
            or bool(round_completion["round_budget_reached"])
            or all_remaining_dimension_budget
        ) and not target_met,
        "low_gain_supported": low_gain,
        "source_blocked_supported": all_remaining_route_terminal
        and all(
            dimension_routes[name]["termination_reason"] == "source_blocked"
            for name in remaining
        ),
        "repeated_blocked_routes": [],
        "per_dimension_termination": dimension_routes,
        "target_met_dimensions": target_met_dimensions,
        "dimension_query_budget_reached": budget_reached_dimensions,
        "dimension_round_budget_reached": round_budget_dimensions,
        "remaining_gaps": {
            name: max(
                0,
                int(target_status["required_scored_evidence_per_dimension"])
                - int(target_status["scored_evidence_by_dimension"].get(name, 0)),
            )
            for name in remaining
        },
        "restricted_delivery_allowed": bool(
            verified_exhausted
            and all_below_target_formally_terminal
            and not retrieval_control_invalid
            and not unrecoverable_error
        ),
        "no_query_reason": status if terminal and not target_met else "",
        "unrecoverable_error": unrecoverable_error,
        "metrics": metrics,
        "budget_compliance": budget_compliance,
        "budget_error_codes": list(budget_compliance["error_codes"]),
        "round_completion_validation": round_completion,
        "target_status": target_status,
        "budgets": budgets,
        "budget_summary": {
            "baseline_independent_intent_budget": budgets["baseline_independent_intent_budget"],
            "baseline_independent_intents_used": int(metrics["baseline_unique_query_intent_count"]),
            "baseline_budget": budgets["baseline_attempt_budget"],
            "baseline_used": int(metrics["baseline_executed_query_count"]),
            "baseline_remaining": max(
                0,
                budgets["baseline_attempt_budget"]
                - int(metrics["baseline_executed_query_count"]),
            ),
            "deep_global_independent_intent_budget": budgets["global_independent_intent_budget"],
            "deep_unique_intents_used": int(metrics["deep_unique_query_intent_count"]),
            "deep_global_budget": budgets["global_attempt_budget"],
            "deep_used": deep_query_count,
            "deep_remaining": max(0, budgets["global_attempt_budget"] - deep_query_count),
            "total_safety_limit": budgets["total_query_safety_limit"],
            "total_used": total_query_count,
            "total_remaining": max(
                0, budgets["total_query_safety_limit"] - total_query_count
            ),
            "per_round_independent_intent_budget": budgets["per_round_independent_intent_budget"],
            "per_round_budget": budgets["per_round_attempt_budget"],
            "per_dimension_independent_intent_budget": budgets["per_dimension_independent_intent_budget"],
            "per_dimension_deep_budget": budgets["per_dimension_attempt_budget"],
            "triggered_budget_type": (
                "deep_global"
                if (
                    deep_query_count >= budgets["global_attempt_budget"]
                    or int(metrics.get("deep_unique_query_intent_count", 0))
                    >= budgets["global_independent_intent_budget"]
                )
                else "rounds"
                if bool(round_completion["round_budget_reached"])
                else "per_dimension"
                if all_remaining_dimension_budget
                else ""
            ),
        },
    }


def platform_dimension_gap_matrix(
    formal_chain: Mapping[str, object],
    *,
    recent_success: Mapping[tuple[str, str], float] | None = None,
) -> list[dict[str, object]]:
    """Prioritize existing platform cells that are closest to scoring."""

    success = recent_success or {}
    minimum = int(formal_chain.get("minimum_dimension_units", 3))
    rows: list[dict[str, object]] = []
    decisions = formal_chain.get("record_decisions", [])
    decision_rows = [item for item in decisions if isinstance(item, Mapping)] if isinstance(decisions, list) else []
    platforms = formal_chain.get("platforms", [])
    if not isinstance(platforms, list):
        platforms = []
    platform_lookup = {
        str(platform.get("platform", "")): platform
        for platform in platforms
        if isinstance(platform, Mapping) and str(platform.get("platform", ""))
    }
    platform_ids = sorted(
        set(platform_lookup)
        | {
            str(decision.get("platform", ""))
            for decision in decision_rows
            if str(decision.get("platform", ""))
        }
    )
    for platform_id in platform_ids:
        platform = platform_lookup.get(platform_id, {})
        dimensions = platform.get("dimensions", {})
        if not isinstance(dimensions, Mapping):
            dimensions = {}
        for dimension in DIMENSION_NAMES:
            item = dimensions.get(dimension, {})
            if not isinstance(item, Mapping):
                item = {}
            eligible = int(item.get("eligible_evidence_units", 0))
            scored = int(item.get("scored_evidence_units", 0))
            cell_decisions = [
                decision
                for decision in decision_rows
                if str(decision.get("platform", "")) == platform_id
                and str(decision.get("primary_dimension", "")) == dimension
            ]
            excluded = Counter(
                str(reason)
                for decision in cell_decisions
                if not bool(decision.get("eligible"))
                for reason in decision.get("reasons", [])
            )
            gap = max(0, minimum - eligible)
            if gap == 0:
                status = "met"
                marginal = 0.0
            else:
                status = "gap"
                marginal = (minimum - gap + 1) / minimum + float(success.get((platform_id, dimension), 0.0))
            rows.append(
                {
                    "platform_id": platform_id,
                    "dimension": dimension,
                    "eligible_evidence_units": eligible,
                    "scored_evidence_units": scored,
                    "candidate_evidence_units": len(cell_decisions),
                    "gap_to_minimum": gap,
                    "minimum_dimension_units": minimum,
                    "status": status,
                    "expected_marginal_gain": round(marginal, 6),
                    "recent_success_rate": float(success.get((platform_id, dimension), 0.0)),
                    "primary_exclusion_reasons": [
                        {"reason": reason, "count": count}
                        for reason, count in sorted(
                            excluded.items(), key=lambda pair: (-pair[1], pair[0])
                        )[:3]
                    ],
                }
            )
    return sorted(
        rows,
        key=lambda item: (
            item["status"] == "met",
            item["gap_to_minimum"],
            -item["expected_marginal_gain"],
            item["platform_id"],
            item["dimension"],
        ),
    )


def platform_gap_recent_success(
    search_rows: Iterable[Mapping[str, object]],
    *, schema_context: Mapping[str, object] | None = None,
) -> dict[tuple[str, str], float]:
    attempts: Counter[tuple[str, str]] = Counter()
    successes: Counter[tuple[str, str]] = Counter()
    for row in executed_search_rows(search_rows, schema_context=schema_context):
        platform_id = str(row.get("target_platform_id", "")).strip()
        dimensions = parse_dimension_targets(row)
        explicit_dimension = str(row.get("target_dimension", "")).strip()
        if explicit_dimension in DIMENSION_NAMES and explicit_dimension not in dimensions:
            dimensions.append(explicit_dimension)
        if not platform_id:
            continue
        try:
            gain = int(str(row.get("new_scored_evidence_units", "0") or "0"))
        except ValueError:
            gain = 0
        for dimension in dimensions:
            key = (platform_id, dimension)
            attempts[key] += 1
            if gain > 0:
                successes[key] += 1
    return {
        key: successes[key] / attempts[key]
        for key in attempts
    }
