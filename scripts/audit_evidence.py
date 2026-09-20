#!/usr/bin/env python3
"""Audit page, evidence, and search ledgers for traceability and scoring safety."""

from __future__ import annotations

import argparse
import csv
import strict_json as json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping
from input_safety import query_parameter_names
from input_safety import safe_urlparse as urlparse, parse_public_url, finite_number, native_numbers, privacy_errors

from dimension_evidence import (
    DIMENSION_NAMES,
    MIN_DIMENSION_EVIDENCE_UNITS,
    MIN_DIMENSION_SOURCE_CATEGORIES,
    MIN_DIMENSION_SOURCE_PAGES,
    apply_retrieval_terminal_to_dimension_audit,
    bind_machine_dimension_audit,
    evaluate_dimension_evidence,
)
from dimension_framework import SUBJECT_TAGS, TIME_TAGS
from formal_scoring import (
    build_formal_scoring_chain,
    load_protocol,
    scoring_direction,
    scoring_value,
    trusted_human_review,
)
from artifact_provenance import register_protected_artifact, verify_artifact_writer
from runtime_guard import (
    authorize_runtime_write,
    canonical_sha256,
    canonical_url as runtime_canonical_url,
    machine_formal_dedup_sha256,
    normalized_url_sha256,
)
from retrieval_controls import (
    budget_values,
    execution_schema_context_from_state,
    executed_query_metrics,
    load_retrieval_config,
    platform_gap_recent_success,
    platform_dimension_gap_matrix,
    retrieval_termination,
    theoretical_minimum_evidence,
    validate_controlled_retries,
    validate_execution_records,
    validate_query_budget_compliance,
    validate_query_dimension_targets,
    validate_round_completion,
)
from source_identity import (
    annotate_page_entities,
    normalized_url_hash_is_compatible,
    page_entity_id_for_source,
    trusted_independent_source_ids,
)


_PROTOCOL = load_protocol()
_SCORING_THRESHOLDS = _PROTOCOL["thresholds"]
SEMANTIC_CONFIDENCE_THRESHOLD = float(_SCORING_THRESHOLDS["semantic_auto_confidence"])
EVIDENCE_RELIABILITY_THRESHOLD = float(_SCORING_THRESHOLDS["minimum_evidence_reliability"])
ASPECT_CONFIDENCE_THRESHOLD = float(_SCORING_THRESHOLDS["dimension_auto_confidence"])


SOURCE_FIELDS = {
    "network_observation", "network_observation_sha256", "platform_mapping_sha256", "final_url",
    "research_cutoff",
    "execution_id", "plan_id", "query_definition_sha256", "plan_row_sha256",
    "execution_binding_sha256", "normalized_query_intent", "query_dimension_targets",
    "planned_iteration_round", "iteration_round", "iteration_mode",
    "task_run_id",
    "source_id",
    "place_name",
    "entity_level",
    "platform",
    "domain",
    "source_category",
    "page_title",
    "published_at",
    "retrieved_at",
    "url",
    "normalized_url_sha256",
    "query_id",
    "query_text",
    "result_rank",
    "access_status",
    "content_layer",
    "selection_mechanism",
    "is_relevant",
    "is_user_source",
    "suspected_promotion",
    "promotion_basis",
    "used_for_scoring",
    "score_scope",
    "dedup_group",
    "original_source_id",
    "evidence_locator",
    "notes",
}
from host_receipts import FIELDS as ATTESTATION_FIELDS
from source_identity import IDENTITY_FIELDS
SOURCE_FIELDS.update(ATTESTATION_FIELDS)
SOURCE_FIELDS.update(IDENTITY_FIELDS)

EVIDENCE_FIELDS = {
    "task_run_id",
    "evidence_id",
    "source_id",
    "platform",
    "unit_type",
    "internal_author_id",
    "published_at",
    "time_context",
    "user_group",
    "entity_level",
    "is_direct_place_evidence",
    "explicit_visit",
    "place_relevance",
    "content_length_category",
    "is_valid",
    "validity_reason",
    "normalized_theme",
    "primary_dimension",
    "secondary_dimension",
    "evidence_type",
    "dimension_tags",
    "sentiment",
    "stance_strength",
    "sentiment_score",
    "interaction_count",
    "displayed_total_count",
    "time_complete",
    "used_for_scoring",
    "score_scope",
    "dedup_group",
    "original_summary_text",
    "excerpt_or_summary",
    "native_rating_value",
    "native_rating_scale_min",
    "native_rating_scale_max",
    "native_rating_normalized",
    "semantic_method",
    "semantic_language",
    "semantic_unit_text",
    "semantic_clause_count",
    "lexicon_match_status",
    "lexicon_match_trace",
    "coding_parse_status",
    "coding_parse_candidates",
    "coding_parse_trace",
    "open_code_id",
    "aspect_assignment_basis",
    "aspect_confidence",
    "dimension_rule_hits",
    "polarity_basis",
    "negation_hits",
    "degree_hits",
    "contrast_hits",
    "hedge_hits",
    "figurative_risk",
    "semantic_score_raw",
    "semantic_score_final",
    "semantic_confidence",
    "evidence_reliability",
    "aggregation_weight",
    "semantic_review_status",
    "formal_dedup_sha256",
    "reviewer_id",
    "review_origin",
    "review_record_id",
    "review_decision_time",
    "review_provenance_sha256",
    "review_provenance_valid",
    "review_trust_proof",
    "semantic_rule_trace",
    "coder_id",
    "adjudication_status",
    "notes",
}

from execution_schema import SEARCH_LOG_FIELDS
from temporal_fields import date_errors, publication_year
from source_identity import domain_errors
from execution_facts import SOURCE_BINDING_FIELDS
SOURCE_FIELDS.update(SOURCE_BINDING_FIELDS)

SOURCE_CATEGORIES = {
    "official",
    "government_open_data",
    "heritage_registry",
    "museum_venue",
    "news",
    "professional",
    "academic",
    "encyclopedic",
    "user_review",
    "map_review",
    "travel_ugc",
    "social_content",
    "blog_travel",
    "local_forum",
    "community_content",
    "business_directory",
}
USER_CATEGORIES = {
    "user_review",
    "map_review",
    "travel_ugc",
    "social_content",
    "blog_travel",
    "local_forum",
    "community_content",
}
ACCESS_STATUSES = {"full", "partial", "snippet_only", "metadata_only", "inaccessible"}
SCORABLE_ACCESS = {"full", "partial"}
CONTENT_LAYERS = {
    "official_fact",
    "open_data_record",
    "heritage_record",
    "news_report",
    "academic_claim",
    "page_body",
    "user_post",
    "user_review",
    "comment",
    "reply",
    "search_snippet",
    "rating_only",
}
SCORABLE_UNITS = {"page_body", "user_post", "user_review", "comment", "reply", "source_native_numeric"}
EVIDENCE_UNIT_TYPES = CONTENT_LAYERS | {"source_native_numeric"}
SELECTION_MECHANISMS = {
    "chronological",
    "relevance_ranked",
    "platform_ai_selected",
    "search_result",
    "editorial",
    "official",
    "citation_following",
    "user_provided",
    "unknown",
    "not_applicable",
}
ENTITY_LEVELS = {"poi", "area_direct", "area_aggregate"}
SCORE_SCOPES = {"direct", "aggregate_context", "not_scored"}
SENTIMENTS = {"positive", "neutral", "negative", "mixed", "na"}
SEMANTIC_METHODS = {
    "source_native_numeric",
    "rule_codebook",
    "coding_parse_fallback",
    "assisted_semantic",
    "manual_code",
    "not_applicable",
}
SEMANTIC_REVIEW_STATUSES = {
    "auto_eligible",
    "review_required",
    "human_confirmed",
    "not_applicable",
}
SEMANTIC_ACCEPTED_FOR_SCORING = {"auto_eligible", "human_confirmed"}
SEMANTIC_LANGUAGES = {"zh", "en", "mixed", "unknown"}
UNKNOWN_HORIZONTAL_TAGS = {"", "unknown", "未识别"}


def invalid_horizontal_tags(value: object, allowed: tuple[str, ...]) -> list[str]:
    text = str(value or "").strip()
    if text in UNKNOWN_HORIZONTAL_TAGS:
        return []
    values = [part.strip() for part in re.split(r"[;；|]", text) if part.strip()]
    return [part for part in values if part not in allowed]
LEXICON_MATCH_STATUSES = {"full_match", "dimension_only", "polarity_only", "zero_hit", "not_applicable"}
CODING_PARSE_STATUSES = {"not_needed", "candidate_generated", "started_unresolved", "not_applicable"}
DIMENSIONS = set(DIMENSION_NAMES)
SEARCH_STATUSES = {
    "completed", "partial", "blocked", "no_results", "failed",
    "not_run", "planned", "queued", "cancelled", "skipped",
}
from execution_semantics import ITERATION_ACTIONS
SENSITIVE_QUERY_KEYS = {"token", "xsec_token", "auth", "authorization", "session", "cookie", "code"}
TRACKING_QUERY_KEYS = {"spm", "from", "source", "ref", "referrer"}
DEFAULT_EXCLUDED_PLATFORMS = {"大众点评", "小红书"}
DEFAULT_EXCLUDED_DOMAINS = {"dianping.com", "xiaohongshu.com"}


def read_csv(path: Path) -> tuple[list[dict[str, str]], set[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    return rows, fields


def parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "是"}:
        return True
    if normalized in {"false", "0", "no", "n", "否"}:
        return False
    return None


def parse_nonnegative_int(value: str) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_unit_float(value: str) -> float | None:
    try:
        number = finite_number(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None


def valid_url(value: str) -> bool:
    try:
        parse_public_url(value)
        return True
    except ValueError:
        return False


def sensitive_url_keys(value: str) -> list[str]:
    parsed = urlparse(value)
    return sorted({key for key in query_parameter_names(parsed.query) if key in SENSITIVE_QUERY_KEYS})


def canonical_url(value: str) -> str:
    return runtime_canonical_url(value)


def split_dimensions(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;；|]", value) if item.strip()]


def dedup_key(
    row: dict[str, str],
    id_field: str,
    source: dict[str, str] | None = None,
) -> str:
    if row.get("url") and valid_url(row["url"]):
        return page_entity_id_for_source(row)
    if id_field == "evidence_id":
        return machine_formal_dedup_sha256(row, source)
    return row.get(id_field, "")


def is_default_excluded_source(row: dict[str, str]) -> bool:
    platform = row.get("platform", "").strip()
    domain = row.get("domain", "").strip().lower()
    if platform in DEFAULT_EXCLUDED_PLATFORMS:
        return True
    return any(domain == item or domain.endswith("." + item) for item in DEFAULT_EXCLUDED_DOMAINS)


def audit(
    sources_path: Path,
    evidence_path: Path,
    search_log_path: Path | None,
    minimum_pages: int = 150,
    minimum_effective_samples: int = 100,
    minimum_dimension_evidence: int = MIN_DIMENSION_EVIDENCE_UNITS,
    minimum_dimension_sources: int = MIN_DIMENSION_SOURCE_PAGES,
    minimum_dimension_source_categories: int = MIN_DIMENSION_SOURCE_CATEGORIES,
    allow_incomplete: bool = False,
    target_confidence: str | None = None,
    retrieval_control_path: Path | None = None,
    execution_schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    sources, source_fields = read_csv(sources_path)
    provenance_sources = sources
    evidence, evidence_fields = read_csv(evidence_path)
    sources, page_identity_audit = annotate_page_entities(
        sources,
        trusted_independent_source_ids=trusted_independent_source_ids(evidence),
    )
    search_rows: list[dict[str, str]] = []
    search_fields: set[str] = set()
    if search_log_path is not None:
        search_rows, search_fields = read_csv(search_log_path)
    from execution_facts import event_key, validate_source_links
    sources.sort(key=lambda row: (str(row.get('source_id', '')), canonical_sha256(row)))
    evidence.sort(key=lambda row: (str(row.get('evidence_id', '')), canonical_sha256(row)))
    def audit_row_order(row):
        try:
            return (0, event_key(row))
        except (ValueError, TypeError):
            return (1, str(row.get('execution_id', '')), canonical_sha256(row))
    search_rows.sort(key=audit_row_order)
    retrieval_config = load_retrieval_config(
        retrieval_control_path
        if retrieval_control_path is not None
        else Path(__file__).resolve().parents[1] / "assets" / "retrieval-control.json"
    )

    missing_source_fields = sorted(SOURCE_FIELDS - source_fields)
    missing_evidence_fields = sorted(EVIDENCE_FIELDS - evidence_fields)
    if missing_source_fields:
        errors.append(f"source ledger missing columns: {', '.join(missing_source_fields)}")
    if missing_evidence_fields:
        errors.append(f"evidence ledger missing columns: {', '.join(missing_evidence_fields)}")
    if search_log_path is None:
        errors.append("search log is required so every executed query is traceable")
    else:
        missing_search_fields = sorted(SEARCH_LOG_FIELDS - search_fields)
        if missing_search_fields:
            errors.append(f"search log missing columns: {', '.join(missing_search_fields)}")
    if errors:
        return {"status": "invalid", "errors": errors, "warnings": warnings}

    run_ids_by_ledger: dict[str, set[str]] = {}
    for ledger_name, rows in (
        ("search log", search_rows),
        ("source ledger", sources),
        ("evidence ledger", evidence),
    ):
        run_ids: set[str] = set()
        for index, row in enumerate(rows, start=2):
            run_id = row["task_run_id"].strip()
            if not run_id:
                errors.append(f"{ledger_name} row {index}: task_run_id is required")
            else:
                run_ids.add(run_id)
        if len(run_ids) > 1:
            errors.append(f"{ledger_name} mixes multiple task_run_id values: {sorted(run_ids)}")
        run_ids_by_ledger[ledger_name] = run_ids
    nonempty_run_ids = set().union(*run_ids_by_ledger.values())
    if len(nonempty_run_ids) > 1:
        errors.append(f"ledgers mix different task runs: {sorted(nonempty_run_ids)}")
    task_run_id = next(iter(nonempty_run_ids), "")

    execution_validation = validate_execution_records(
        search_rows,
        config=retrieval_config,
        schema_context=execution_schema_context,
    )
    valid_execution_ids = {
        str(row.get("execution_id", ""))
        for row in execution_validation["valid_rows"]
    }
    source_links = validate_source_links(sources, execution_validation)
    original_evidence = list(evidence)
    from execution_amendments import retracted_sources
    retracted_source_ids = retracted_sources(execution_schema_context, sources)
    for issue in source_links['errors']:
        errors.append('source_execution_chain_invalid: ' + json.dumps(issue, ensure_ascii=False, sort_keys=True))

    search_ids: set[str] = set()
    executed_search_ids: set[str] = set()
    executed_query_ids: set[str] = set()
    attempted_categories: set[str] = set()
    iteration_rounds: set[int] = set()
    actions_by_round: defaultdict[int, set[str]] = defaultdict(set)
    statuses_by_round: defaultdict[int, set[str]] = defaultdict(set)
    new_samples_by_round: defaultdict[int, int] = defaultdict(int)
    query_count_by_round: defaultdict[int, int] = defaultdict(int)
    last_cumulative_samples = 0
    field_audit_rows = execution_validation['valid_rows'] if execution_validation['status'] == 'valid' else search_rows
    for index, row in enumerate(field_audit_rows, start=2):
        label = f"search log row {index}"
        executed = str(row.get('execution_id', '')) in valid_execution_ids
        search_id = row["search_id"]
        if not search_id:
            errors.append(f"{label}: search_id is required")
        elif search_id in search_ids:
            errors.append(f"{label}: duplicate search_id {search_id}")
        else:
            search_ids.add(search_id)
        for field in ("query", "source_category_target", "status"):
            if not row[field]:
                errors.append(f"{label}: {field} is required")
        if executed:
            executed_search_ids.add(search_id)
            executed_query_ids.add(row["query_id"])
            for field in ("retrieved_at", "search_tool"):
                if not row[field]:
                    errors.append(f"{label}: executed query requires {field}")
        category = row["source_category_target"]
        if category not in SOURCE_CATEGORIES and category != "mixed":
            errors.append(f"{label}: invalid source_category_target {category!r}")
        elif category != "mixed" and executed:
            attempted_categories.add(category)
        if row["status"] not in SEARCH_STATUSES:
            errors.append(f"{label}: invalid status {row['status']!r}")
        target_validation = validate_query_dimension_targets(row)
        if target_validation["status"] != "valid":
            for item in target_validation["errors"]:
                errors.append(f"{label}: {item['code']}: {item['message']}")
        iteration_round = parse_nonnegative_int(row["iteration_round"])
        if iteration_round is None:
            errors.append(f"{label}: iteration_round must be a non-negative integer")
        else:
            if executed:
                iteration_rounds.add(iteration_round)
                statuses_by_round[iteration_round].add(row["status"])
            if iteration_round > 0 and not row["gap_target"]:
                errors.append(f"{label}: gap_target is required for deep-search iterations")
            if iteration_round > 0 and executed:
                query_count_by_round[iteration_round] += 1
        if row["next_action"] not in ITERATION_ACTIONS:
            errors.append(f"{label}: invalid next_action {row['next_action']!r}")
        elif iteration_round is not None and executed:
            actions_by_round[iteration_round].add(row["next_action"])
        from execution_schema import basic_errors
        from execution_facts import DerivedExecution
        field_row = ({k: v for k, v in row.items() if not k.startswith('_')}
                     if isinstance(row, DerivedExecution) else row)
        errors.extend(f'{label}: {issue}' for issue in basic_errors(field_row))
        for field in ("returned_results", "opened_pages", "relevant_pages", "duplicate_pages"):
            if (executed or row[field]) and parse_nonnegative_int(row[field]) is None:
                errors.append(f"{label}: {field} must be a non-negative integer")
        for field in ("new_scored_evidence_units", "cumulative_scored_evidence_units"):
            value = parse_nonnegative_int(row[field])
            if (executed or row[field]) and value is None:
                errors.append(f"{label}: {field} must be a non-negative integer")
            elif executed and field == "new_scored_evidence_units" and iteration_round is not None:
                new_samples_by_round[iteration_round] += value
            elif executed and field == "cumulative_scored_evidence_units":
                if value < last_cumulative_samples:
                    errors.append(f"{label}: cumulative_scored_evidence_units cannot decrease")
                last_cumulative_samples = value
        for field in ("login_triggered", "restriction_triggered"):
            if (executed or row[field]) and parse_bool(row[field]) is None:
                errors.append(f"{label}: {field} must be true or false")

    shared_execution_metrics = executed_query_metrics(
        search_rows,
        config=retrieval_config,
        schema_context=execution_schema_context,
    )
    if shared_execution_metrics["duplicate_execution_ids"]:
        errors.append(
            "search log repeats execution identifiers: "
            + ", ".join(shared_execution_metrics["duplicate_execution_ids"])
        )
    for item in shared_execution_metrics.get("invalid_query_records", []):
        errors.append(
            "search log contains an invalid executed query record: "
            + json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    retry_audit = validate_controlled_retries(
        search_rows,
        config=retrieval_config,
        schema_context=execution_schema_context,
    )
    errors.extend(str(item) for item in retry_audit["errors"])
    budget_compliance = validate_query_budget_compliance(
        shared_execution_metrics,
        config=retrieval_config,
    )
    for item in budget_compliance["errors"]:
        errors.append(
            "search execution budget violation: "
            + json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    native_source_ids = {
        row.get("source_id", "")
        for row in evidence
        if row.get("unit_type", "") == "source_native_numeric"
        and all(
            row.get(field, "")
            for field in (
                "native_rating_value",
                "native_rating_scale_min",
                "native_rating_scale_max",
            )
        )
    }
    source_by_id: dict[str, dict[str, str]] = {}
    source_scoring_groups: defaultdict[str, list[str]] = defaultdict(list)
    relevant_sources: list[dict[str, str]] = []

    for index, row in enumerate(sources, start=2):
        label = f"sources row {index}"
        source_id = row["source_id"]
        if not source_id:
            errors.append(f"{label}: source_id is required")
        elif source_id in source_by_id:
            errors.append(f"{label}: duplicate source_id {source_id}")
        else:
            source_by_id[source_id] = row
        for field in ("place_name", "platform", "domain", "page_title", "retrieved_at", "url", "query_id", "query_text"):
            if not row[field]:
                errors.append(f"{label}: {field} is required")
        if row["query_id"] and row["query_id"] not in executed_query_ids:
            errors.append(
                f"{label}: query_id {row['query_id']!r} is absent from the executed search log"
            )
        if row["entity_level"] not in ENTITY_LEVELS:
            errors.append(f"{label}: invalid entity_level {row['entity_level']!r}")
        if row["source_category"] not in SOURCE_CATEGORIES:
            errors.append(f"{label}: invalid source_category {row['source_category']!r}")
        if row["access_status"] not in ACCESS_STATUSES:
            errors.append(f"{label}: invalid access_status {row['access_status']!r}")
        if row["content_layer"] not in CONTENT_LAYERS:
            errors.append(f"{label}: invalid content_layer {row['content_layer']!r}")
        if row["selection_mechanism"] not in SELECTION_MECHANISMS:
            errors.append(f"{label}: invalid selection_mechanism {row['selection_mechanism']!r}")
        if row["score_scope"] not in SCORE_SCOPES:
            errors.append(f"{label}: invalid score_scope {row['score_scope']!r}")
        bool_values = {
            field: parse_bool(row[field])
            for field in ("is_relevant", "is_user_source", "suspected_promotion", "used_for_scoring")
        }
        for field, value in bool_values.items():
            if value is None:
                errors.append(f"{label}: {field} must be true or false")
        if bool_values["is_relevant"] is True:
            relevant_sources.append(row)
        if row["source_category"] in USER_CATEGORIES and bool_values["is_user_source"] is not True:
            warnings.append(f"{label}: user/UGC category is not marked as a user source")
        if row["url"] and not valid_url(row["url"]):
            errors.append(f"{label}: URL must be an absolute http(s) URL")
        from source_identity import identity_url, identity_errors
        expected_url_hash = normalized_url_sha256(identity_url(row))
        if not expected_url_hash or row.get("normalized_url_sha256", "") != expected_url_hash:
            errors.append(f"{label}: normalized_url_sha256 must be machine-derived from URL")
        leaked = sensitive_url_keys(row["url"]) if valid_url(row["url"]) else []
        if leaked:
            errors.append(f"{label}: URL exposes sensitive query keys: {', '.join(leaked)}")
        errors.extend(domain_errors(row, label=label))
        from source_identity import platform_errors
        from public_network import network_errors
        errors.extend(platform_errors(row, label=label))
        errors.extend(network_errors(row, label=label))
        from host_receipts import proof_errors
        errors.extend(proof_errors(row, label=label))
        errors.extend(identity_errors(row, label=label))
        errors.extend(privacy_errors(row, label=label))
        errors.extend(date_errors(row, label=label, capture=True))
        if row["result_rank"] and parse_nonnegative_int(row["result_rank"]) is None:
            warnings.append(f"{label}: result_rank is not a non-negative integer")
        if row["access_status"] in SCORABLE_ACCESS and not row["evidence_locator"]:
            warnings.append(f"{label}: readable source lacks evidence_locator")
        used = bool_values["used_for_scoring"] is True
        if used:
            source_scoring_groups[page_entity_id_for_source(row)].append(source_id or f"row-{index}")
            if bool_values["is_relevant"] is not True:
                errors.append(f"{label}: scored source is not marked relevant")
            if bool_values["is_user_source"] is not True:
                errors.append(f"{label}: scored source is not marked as a user source")
            if bool_values["suspected_promotion"] is not False:
                errors.append(f"{label}: promoted or unknown-promotion source cannot be scored")
            native_metadata = (
                source_id in native_source_ids
                and row["access_status"] == "metadata_only"
                and row["content_layer"] == "rating_only"
            )
            if row["access_status"] not in SCORABLE_ACCESS and not native_metadata:
                errors.append(f"{label}: {row['access_status']} source cannot be scored")
            if row["content_layer"] not in SCORABLE_UNITS and not native_metadata:
                errors.append(f"{label}: {row['content_layer']} content cannot be scored")
            if row["score_scope"] == "not_scored":
                errors.append(f"{label}: scored source has score_scope=not_scored")
            if row["entity_level"] == "area_aggregate" and row["score_scope"] == "direct":
                errors.append(f"{label}: area_aggregate cannot enter the direct place score")
            if row["selection_mechanism"] == "platform_ai_selected":
                warnings.append(f"{label}: scored comments are platform-AI-selected; disclose selection bias")

    for group, ids in source_scoring_groups.items():
        if len(ids) > 1:
            errors.append(f"source dedup group {group!r} has multiple scored rows: {', '.join(ids)}")

    evidence_ids: set[str] = set()
    evidence_scoring_groups: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    scored_evidence: list[dict[str, str]] = []
    semantic_method_counts: Counter[str] = Counter()
    semantic_review_counts: Counter[str] = Counter()
    lexicon_match_counts: Counter[str] = Counter()
    coding_parse_counts: Counter[str] = Counter()
    semantic_low_confidence_count = 0
    semantic_figurative_count = 0
    for index, row in enumerate(evidence, start=2):
        label = f"evidence row {index}"
        errors.extend(date_errors(row, label=label))
        evidence_id = row["evidence_id"]
        if not evidence_id:
            errors.append(f"{label}: evidence_id is required")
        elif evidence_id in evidence_ids:
            errors.append(f"{label}: duplicate evidence_id {evidence_id}")
        else:
            evidence_ids.add(evidence_id)
        source = source_by_id.get(row["source_id"])
        if source is not None:
            from source_identity import platform_errors
            errors.extend(platform_errors(row, label=label, expected_source=source))
            errors.extend(date_errors({**row, 'retrieved_at': source.get('retrieved_at'),
                'research_cutoff': source.get('research_cutoff')}, label=label))
        if not row["source_id"]:
            errors.append(f"{label}: source_id is required")
        elif source is None:
            errors.append(f"{label}: unknown source_id {row['source_id']}")
        if not row["platform"]:
            errors.append(f"{label}: platform is required")
        if row["unit_type"] not in EVIDENCE_UNIT_TYPES:
            errors.append(f"{label}: invalid unit_type {row['unit_type']!r}")
        if row.get("recognition_rule_sha256") or any(
                row.get(field) for field in (
                    "source_block_id", "source_block_type",
                    "source_block_is_user_generated", "locator_sha256")):
            from content_blocks import evidence_content_errors
            for issue in evidence_content_errors(
                    row,
                    require_locator=row.get("unit_type") != "source_native_numeric",
                    formal_scoring=parse_bool(row.get("used_for_scoring")) is True):
                errors.append(f"{label}: {issue}")
        if row["entity_level"] not in ENTITY_LEVELS:
            errors.append(f"{label}: invalid entity_level {row['entity_level']!r}")
        if row["sentiment"] not in SENTIMENTS:
            errors.append(f"{label}: invalid sentiment {row['sentiment']!r}")
        invalid_subjects = invalid_horizontal_tags(row.get("user_group"), SUBJECT_TAGS)
        if invalid_subjects:
            errors.append(
                f"{label}: invalid subject tag(s): {', '.join(invalid_subjects)}"
            )
        invalid_times = invalid_horizontal_tags(row.get("time_context"), TIME_TAGS)
        if invalid_times:
            errors.append(
                f"{label}: invalid time-context tag(s): {', '.join(invalid_times)}"
            )
        if row["score_scope"] not in SCORE_SCOPES:
            errors.append(f"{label}: invalid score_scope {row['score_scope']!r}")
        direct = parse_bool(row["is_direct_place_evidence"])
        valid = parse_bool(row["is_valid"])
        used = parse_bool(row["used_for_scoring"])
        if direct is None:
            errors.append(f"{label}: is_direct_place_evidence must be true or false")
        if valid is None:
            errors.append(f"{label}: is_valid must be true or false")
        if used is None:
            errors.append(f"{label}: used_for_scoring must be true or false")
        dimensions = split_dimensions(row["dimension_tags"])
        invalid_dimensions = sorted(set(dimensions) - DIMENSIONS)
        if invalid_dimensions:
            errors.append(f"{label}: invalid dimension tags: {', '.join(invalid_dimensions)}")
        if row["primary_dimension"] not in DIMENSIONS:
            errors.append(f"{label}: primary_dimension must be one canonical dimension")
        if row["stance_strength"]:
            try:
                strength = int(row["stance_strength"])
                if strength < 1 or strength > 5:
                    raise ValueError
            except ValueError:
                errors.append(f"{label}: stance_strength must be blank or an integer from 1 to 5")
        if row["sentiment_score"]:
            try:
                score = int(row["sentiment_score"])
                if score < -5 or score > 5:
                    raise ValueError
            except ValueError:
                errors.append(f"{label}: sentiment_score must be blank or an integer from -5 to 5")
        semantic_method = row["semantic_method"]
        review_status = row["semantic_review_status"]
        if semantic_method:
            semantic_method_counts[semantic_method] += 1
            if semantic_method not in SEMANTIC_METHODS:
                errors.append(f"{label}: invalid semantic_method {semantic_method!r}")
        if review_status:
            semantic_review_counts[review_status] += 1
            if review_status not in SEMANTIC_REVIEW_STATUSES:
                errors.append(f"{label}: invalid semantic_review_status {review_status!r}")
        semantic_language = row["semantic_language"]
        if semantic_language and semantic_language not in SEMANTIC_LANGUAGES:
            errors.append(f"{label}: invalid semantic_language {semantic_language!r}")
        lexicon_status = row["lexicon_match_status"]
        if lexicon_status:
            lexicon_match_counts[lexicon_status] += 1
            if lexicon_status not in LEXICON_MATCH_STATUSES:
                errors.append(f"{label}: invalid lexicon_match_status {lexicon_status!r}")
        coding_status = row["coding_parse_status"]
        if coding_status:
            coding_parse_counts[coding_status] += 1
            if coding_status not in CODING_PARSE_STATUSES:
                errors.append(f"{label}: invalid coding_parse_status {coding_status!r}")
        numeric_semantic: dict[str, float | None] = {}
        for field in ("aspect_confidence", "semantic_confidence", "evidence_reliability", "aggregation_weight"):
            value = row[field]
            parsed = parse_unit_float(value) if value else None
            numeric_semantic[field] = parsed
            if value and parsed is None:
                errors.append(f"{label}: {field} must be blank or a number from 0 to 1")
        if numeric_semantic["semantic_confidence"] is not None and numeric_semantic["semantic_confidence"] < SEMANTIC_CONFIDENCE_THRESHOLD:
            semantic_low_confidence_count += 1
        figurative = parse_bool(row["figurative_risk"]) if row["figurative_risk"] else None
        if row["figurative_risk"] and figurative is None:
            errors.append(f"{label}: figurative_risk must be blank, true, or false")
        if figurative is True:
            semantic_figurative_count += 1
        if row["semantic_clause_count"] and parse_nonnegative_int(row["semantic_clause_count"]) is None:
            errors.append(f"{label}: semantic_clause_count must be blank or a non-negative integer")
        for field in ("semantic_score_raw", "semantic_score_final", "native_rating_normalized"):
            if not row[field]:
                continue
            try:
                number = finite_number(row[field], field=field)
                if number < -5 or number > 5:
                    raise ValueError
                if field == "semantic_score_final" and not number.is_integer():
                    raise ValueError
            except ValueError:
                errors.append(f"{label}: {field} must be blank or a number from -5 to 5; semantic_score_final must be an integer")
        native_fields = [row["native_rating_value"], row["native_rating_scale_min"], row["native_rating_scale_max"]]
        if any(native_fields):
            if not all(native_fields):
                errors.append(f"{label}: native rating value, minimum, and maximum must be supplied together")
            else:
                try:
                    checked_native = native_numbers(row)
                    native_value, native_minimum, native_maximum = (
                        checked_native[key] for key in ("native_rating_value", "native_rating_scale_min", "native_rating_scale_max"))
                    if native_maximum <= native_minimum or not native_minimum <= native_value <= native_maximum:
                        raise ValueError
                    expected_native = -5 + 10 * (native_value - native_minimum) / (native_maximum - native_minimum)
                    if row["native_rating_normalized"] and not abs(finite_number(row["native_rating_normalized"]) - expected_native) <= 0.01:
                        errors.append(f"{label}: native_rating_normalized does not match the declared source scale")
                except ValueError:
                    errors.append(f"{label}: native rating fields must define a valid numeric scale")
        if row["dimension_rule_hits"]:
            try:
                if not isinstance(json.loads(row["dimension_rule_hits"]), dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                errors.append(f"{label}: dimension_rule_hits must be a JSON object")
        if row["semantic_rule_trace"]:
            try:
                if not isinstance(json.loads(row["semantic_rule_trace"]), list):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                errors.append(f"{label}: semantic_rule_trace must be a JSON array")
        for field, expected_type, label_type in (
            ("lexicon_match_trace", dict, "object"),
            ("coding_parse_candidates", list, "array"),
            ("coding_parse_trace", dict, "object"),
        ):
            if not row[field]:
                continue
            try:
                if not isinstance(json.loads(row[field]), expected_type):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                errors.append(f"{label}: {field} must be a JSON {label_type}")
        if semantic_method == "coding_parse_fallback":
            if lexicon_status not in {"dimension_only", "polarity_only", "zero_hit"}:
                errors.append(f"{label}: coding_parse_fallback requires a partial or zero lexicon match")
            if coding_status not in {"candidate_generated", "started_unresolved"}:
                errors.append(f"{label}: coding_parse_fallback requires an active coding parse status")
            if review_status != "review_required":
                errors.append(f"{label}: coding_parse_fallback must remain review_required")
            if not re.fullmatch(r"OPEN-[0-9a-f]{12}", row["open_code_id"]):
                errors.append(f"{label}: coding_parse_fallback requires a stable OPEN-* code ID")
            if parse_bool(row["used_for_scoring"]) is True:
                errors.append(f"{label}: coding_parse_fallback cannot be scored before assisted or human coding")
        if (
            valid is True
            and row["unit_type"] in SCORABLE_UNITS
            and row["original_summary_text"]
            and not semantic_method
        ):
            warnings.append(f"{label}: valid readable user text has no semantic quantification method")
        if source and source["platform"] != row["platform"]:
            errors.append(f"{label}: platform differs from authoritative linked source")
        expected_formal_dedup = machine_formal_dedup_sha256(row, source)
        if row.get("formal_dedup_sha256", "") != expected_formal_dedup:
            errors.append(f"{label}: formal_dedup_sha256 is not machine-derived from source lineage, primary dimension, and semantic text")
        if review_status == "human_confirmed" and not trusted_human_review(row):
            errors.append(f"{label}: review_provenance_invalid")
        if row.get('collision_status') == 'verified_independent_occurrence':
            from review_trust import collision_evidence_proof_valid
            if not collision_evidence_proof_valid(row):
                errors.append(f"{label}: collision_review_provenance_invalid")
        if used is True:
            scored_evidence.append(row)
            evidence_scoring_groups[(row["primary_dimension"], dedup_key(row, "evidence_id", source))].append(evidence_id or f"row-{index}")
            if valid is not True:
                errors.append(f"{label}: invalid evidence cannot be scored")
            if source is not None:
                if parse_bool(source["used_for_scoring"]) is not True:
                    errors.append(f"{label}: linked source is not marked for scoring")
                if parse_bool(source["is_user_source"]) is not True:
                    errors.append(f"{label}: linked source is not a user source")
                if parse_bool(source["suspected_promotion"]) is not False:
                    errors.append(f"{label}: linked source is promotional or promotion status is unknown")
                native_numeric = row["unit_type"] == "source_native_numeric"
                native_metadata = (
                    native_numeric
                    and source["access_status"] == "metadata_only"
                    and source["content_layer"] == "rating_only"
                )
                if source["access_status"] not in SCORABLE_ACCESS and not native_metadata:
                    errors.append(f"{label}: linked source is not fully or partially readable")
            if row["unit_type"] not in SCORABLE_UNITS:
                errors.append(f"{label}: {row['unit_type']} cannot be a scored user unit")
            if direct is not True or row["score_scope"] != "direct":
                errors.append(f"{label}: main scored evidence must be direct and score_scope=direct")
            if row["entity_level"] == "area_aggregate":
                errors.append(f"{label}: area_aggregate evidence cannot enter the direct score")
            if not dimensions and row["primary_dimension"] not in DIMENSIONS:
                errors.append(f"{label}: scored evidence requires at least one dimension tag")
            if row["sentiment"] == "na":
                errors.append(f"{label}: scored evidence requires a sentiment")
            if row["unit_type"] != "source_native_numeric":
                if not row["excerpt_or_summary"]:
                    errors.append(f"{label}: scored evidence requires an excerpt or summary")
                if not row["original_summary_text"]:
                    errors.append(f"{label}: scored evidence requires original summary text from the current readable page")
            if semantic_method not in SEMANTIC_METHODS - {"not_applicable"}:
                errors.append(f"{label}: scored evidence requires a valid semantic or source-native quantification method")
            if semantic_method == "source_native_numeric":
                if not all(native_fields) or not row["native_rating_normalized"]:
                    errors.append(f"{label}: source-native numeric scoring requires the original value, scale, and normalized value")
            else:
                if review_status not in SEMANTIC_ACCEPTED_FOR_SCORING:
                    errors.append(f"{label}: semantic text cannot be scored until it is auto-eligible or human-confirmed")
                if not row["semantic_unit_text"]:
                    errors.append(f"{label}: semantic scoring requires semantic_unit_text")
                if not row["semantic_rule_trace"]:
                    errors.append(f"{label}: semantic scoring requires a machine-readable rule or adjudication trace")
                if numeric_semantic["semantic_confidence"] is None or numeric_semantic["semantic_confidence"] < SEMANTIC_CONFIDENCE_THRESHOLD:
                    errors.append(f"{label}: scored semantic text requires semantic_confidence >= {SEMANTIC_CONFIDENCE_THRESHOLD:.2f}")
                if numeric_semantic["evidence_reliability"] is None or numeric_semantic["evidence_reliability"] < EVIDENCE_RELIABILITY_THRESHOLD:
                    errors.append(f"{label}: scored semantic text requires evidence_reliability >= {EVIDENCE_RELIABILITY_THRESHOLD:.2f}")
                if numeric_semantic["aspect_confidence"] is None or numeric_semantic["aspect_confidence"] < ASPECT_CONFIDENCE_THRESHOLD:
                    errors.append(f"{label}: scored semantic text requires aspect_confidence >= {ASPECT_CONFIDENCE_THRESHOLD:.2f}")
                if numeric_semantic["aggregation_weight"] is None:
                    errors.append(f"{label}: scored semantic text requires aggregation_weight")
                elif (
                    numeric_semantic["semantic_confidence"] is not None
                    and numeric_semantic["evidence_reliability"] is not None
                    and abs(
                        numeric_semantic["aggregation_weight"]
                        - numeric_semantic["semantic_confidence"] * numeric_semantic["evidence_reliability"]
                    ) > 0.02
                ):
                    errors.append(f"{label}: aggregation_weight must equal semantic_confidence × evidence_reliability")
                if row["semantic_score_final"] and row["sentiment_score"]:
                    try:
                        if int(finite_number(row["semantic_score_final"])) != int(finite_number(row["sentiment_score"])):
                            errors.append(f"{label}: semantic_score_final must match sentiment_score for semantic scoring")
                    except ValueError:
                        errors.append(f"{label}: semantic scores must be finite numbers")
                if figurative is True and review_status != "human_confirmed":
                    errors.append(f"{label}: figurative or sarcasm-risk text requires human confirmation before scoring")
            if row["unit_type"] in {"user_post", "user_review", "comment", "reply"} and not row["internal_author_id"]:
                warnings.append(f"{label}: user unit lacks an anonymized internal_author_id")

    for (dimension, group), ids in evidence_scoring_groups.items():
        if len(ids) > 1:
            errors.append(
                f"evidence dedup group {group!r} has multiple scored rows within primary_dimension {dimension}: {', '.join(ids)}"
            )

    # Keep diagnostics for every submitted row, but rebuild all formal quantities
    # exclusively from outcome-consistent execution/source relationships.
    allowed_source_ids = {row['source_id'] for row in source_links['valid_sources']}
    rejected_source_count = len(sources) - len(source_links['valid_sources'])
    rejected_evidence_count = sum(row.get('source_id') not in allowed_source_ids for row in evidence)
    if rejected_source_count or rejected_evidence_count:
        for row in evidence:
            if row.get('source_id') not in allowed_source_ids and row.get('source_id') not in retracted_source_ids:
                errors.append('evidence_execution_chain_invalid: ' + str(row.get('evidence_id', '')))
        evidence = [row for row in evidence if row.get('source_id') in allowed_source_ids]
        sources, page_identity_audit = annotate_page_entities(list(source_links['valid_sources']),
            trusted_independent_source_ids=trusted_independent_source_ids(evidence))
        source_by_id = {row['source_id']: row for row in sources}
        relevant_sources = [row for row in sources if parse_bool(row.get('is_relevant')) is True]
    from source_identity import linked_evidence_errors
    evidence = [row for row in evidence if not linked_evidence_errors(row, source_by_id.get(row.get('source_id')))]
    formal_chain = build_formal_scoring_chain(
        evidence,
        sources,
        protocol=_PROTOCOL,
        require_source_contract=True,
    )
    for row, decision in zip(evidence, formal_chain["record_decisions"]):
        if parse_bool(row.get("used_for_scoring")) is True and not decision["eligible"]:
            errors.append(
                f"evidence {row.get('evidence_id', '')}: formal scoring eligibility failed: "
                + ", ".join(decision["reasons"])
            )
    scored_evidence = list(formal_chain["scored_rows"])
    eligible_evidence = list(formal_chain["eligible_rows"])

    searched_groups = {
        page_entity_id_for_source(row)
        for row in relevant_sources
        if row.get("source_id") and page_entity_id_for_source(row)
    }
    readable_sources = [
        row
        for row in relevant_sources
        if row["access_status"] in SCORABLE_ACCESS
        or (
            row.get("source_id", "") in native_source_ids
            and row["access_status"] == "metadata_only"
            and row["content_layer"] == "rating_only"
        )
    ]
    readable_groups = {dedup_key(row, "source_id") for row in readable_sources}
    relevant_categories = sorted({row["source_category"] for row in relevant_sources})
    readable_categories = sorted({row["source_category"] for row in readable_sources})
    user_platforms = sorted({
        row["platform"]
        for row in readable_sources
        if row["source_category"] in USER_CATEGORIES
        and parse_bool(row["is_user_source"]) is True
        and parse_bool(row["suspected_promotion"]) is False
    })
    scoring_groups = {
        (row["primary_dimension"], dedup_key(row, "evidence_id", source_by_id.get(row.get("source_id", ""))))
        for row in scored_evidence
    }
    eligible_groups = {
        (row["primary_dimension"], dedup_key(row, "evidence_id", source_by_id.get(row.get("source_id", ""))))
        for row in eligible_evidence
    }
    neutral_band = float(_PROTOCOL["score_scale"]["neutral_band"])
    sentiment_counts = Counter(
        scoring_direction(scoring_value(row), neutral_band) for row in scored_evidence
    )
    dimension_counts: Counter[str] = Counter(row["primary_dimension"] for row in scored_evidence)
    dimension_evidence = evaluate_dimension_evidence(
        evidence,
        sources,
        search_rows,
        minimum_evidence_units=minimum_dimension_evidence,
        minimum_source_pages=minimum_dimension_sources,
        minimum_source_categories=minimum_dimension_source_categories,
        target_confidence=target_confidence or "中高",
        execution_schema_context=execution_schema_context,
    )
    requested_confidence = target_confidence or ""
    effective_sample_target = minimum_effective_samples
    retrieval_state: dict[str, object] | None = None
    if requested_confidence:
        effective_sample_target = max(
            minimum_effective_samples,
            theoretical_minimum_evidence(_PROTOCOL, requested_confidence),
        )
        retrieval_state = retrieval_termination(
            search_rows,
            dimension_evidence["dimensions"],
            target_confidence=requested_confidence,
            config=retrieval_config,
            protocol=_PROTOCOL,
            schema_context=execution_schema_context,
        )
        if retrieval_state.get("terminal") is True and retrieval_state.get("target_met") is False:
            dimension_evidence = apply_retrieval_terminal_to_dimension_audit(
                dimension_evidence,
                retrieval_state,
            )
    bounded_shortfall_terminal = bool(
        retrieval_state
        and retrieval_state.get("restricted_delivery_allowed") is True
    )
    if task_run_id and provenance_sources and not source_links['errors']:
        dimension_evidence = bind_machine_dimension_audit(
            dimension_evidence,
            task_run_id=task_run_id,
            evidence=original_evidence,
            sources=provenance_sources,
            search_rows=search_rows,
            execution_schema_context=execution_schema_context,
        )
    pending_dimensions = list(dimension_evidence["needs_iteration_dimensions"])
    evidence_gap_targets = list(dimension_evidence["evidence_gap_target_dimensions"])
    enhancement_targets = list(dimension_evidence["enhancement_target_dimensions"])
    exhausted_dimension_shortfalls = list(dimension_evidence["exhausted_shortfall_dimensions"])
    for dimension in pending_dimensions:
        details = dimension_evidence["dimensions"][dimension]
        if details["confidence"] == "中":
            warnings.append(
                f"dimension {dimension} has only medium confidence and has not reached the preferred "
                "medium-high/high target or passed the stronger medium-completion audit; continue "
                f"dimension enhancement search ({'; '.join(details['enhancement_gaps'])})"
            )
        else:
            warnings.append(
                f"dimension {dimension} is below the minimum medium-confidence threshold "
                f"({'; '.join(details['gaps'])}); continue dimension-targeted deep search and do "
                "not score or finalize this dimension"
            )
    for dimension in exhausted_dimension_shortfalls:
        details = dimension_evidence["dimensions"][dimension]
        warnings.append(
            f"dimension {dimension} remains below the evidence threshold after supported targeted-search exhaustion; "
            f"retain the dimension as a low-confidence evaluation and disclose: {'; '.join(details['gaps'])}"
        )
    access_counts = Counter(row["access_status"] for row in relevant_sources)
    years = sorted({
        year
        for row in sources
        if (year := publication_year(row["published_at"]))
    })
    excluded_count = sum(is_default_excluded_source(row) for row in relevant_sources)

    if len(searched_groups) < minimum_pages:
        message = f"only {len(searched_groups)} deduplicated relevant searched pages; minimum is {minimum_pages}"
        if allow_incomplete or bounded_shortfall_terminal:
            warnings.append(message)
        else:
            errors.append(message)
    if len(relevant_categories) < 8:
        warnings.append(
            f"only {len(relevant_categories)} source categories represented; expand all applicable public source types"
        )
    if len(attempted_categories) < 10:
        warnings.append(
            f"only {len(attempted_categories)} source categories attempted; search as many applicable types as possible"
        )
    unattempted_categories = sorted(SOURCE_CATEGORIES - attempted_categories)
    if len(user_platforms) < 2:
        warnings.append(f"only {len(user_platforms)} readable user/social platforms; cross-platform perception is limited")
    if not scoring_groups:
        warnings.append("no valid direct user evidence is marked for scoring")
    if sentiment_counts.get("positive", 0) == 0:
        warnings.append("no positive scored user evidence")
    if sentiment_counts.get("negative", 0) == 0:
        warnings.append("no negative scored user evidence")

    scored_sample_count = len(scoring_groups)
    sample_target_met = scored_sample_count >= effective_sample_target
    sample_shortfall = max(0, effective_sample_target - scored_sample_count)
    deep_round_numbers = sorted(
        int(str(round_number))
        for round_number in shared_execution_metrics.get("actual_attempts_by_round", {})
        if str(round_number).isdigit() and int(str(round_number)) > 0
    )
    deepest_round = max(deep_round_numbers, default=0)
    round_completion = (
        retrieval_state.get("round_completion_validation", {})
        if isinstance(retrieval_state, dict)
        else {}
    )
    if not isinstance(round_completion, dict) or not round_completion:
        round_completion = validate_round_completion(
            search_rows,
            config=retrieval_config,
            required_dimensions=pending_dimensions,
            expected_round_count=min(
                deepest_round,
                budget_values(retrieval_config)["maximum_iteration_rounds"],
            ),
            schema_context=execution_schema_context,
        )
    latest_two_rounds = deep_round_numbers[-2:]
    low_yield_exhaustion = (
        len(latest_two_rounds) == 2
        and all(
            int(shared_execution_metrics.get("gain_by_round", {}).get(str(round_number), 0)) < 5
            for round_number in latest_two_rounds
        )
    )
    blocked_or_empty_exhaustion = bool(deepest_round) and bool(statuses_by_round.get(deepest_round)) and all(
        status in {"blocked", "no_results"}
        for status in statuses_by_round[deepest_round]
    )
    deep_query_depth_met = bool(deep_round_numbers) and round_completion.get("status") in {
        "valid",
        "incomplete",
    } and not round_completion.get("error_codes")
    source_breadth_met = len(attempted_categories) >= 10
    exhaustion_evidence = (
        (low_yield_exhaustion or blocked_or_empty_exhaustion)
        and deep_query_depth_met
        and source_breadth_met
    )
    search_exhausted = (
        deepest_round > 0
        and round_completion.get("status") == "valid"
        and "exhausted" in actions_by_round.get(deepest_round, set())
        and exhaustion_evidence
    )
    if search_rows and last_cumulative_samples != scored_sample_count:
        warnings.append(
            "search log cumulative_scored_evidence_units does not match the deduplicated scored evidence count"
        )
    if round_completion.get("status") in {"invalid", "incomplete"}:
        warnings.append(
            "deep-search round sequence is incomplete or internally inconsistent; continue retrieval "
            "before any shortfall terminal: "
            + ", ".join(str(value) for value in round_completion.get("error_codes", []))
        )
    if not sample_target_met:
        warnings.append(
            f"only {scored_sample_count} deduplicated valid scoring samples; target is {effective_sample_target}"
        )
        if len(deep_round_numbers) < 3:
            warnings.append(
                f"effective-sample target requires deep iterative search; only {len(deep_round_numbers)} round(s) recorded"
            )
        elif not search_exhausted:
            warnings.append(
                "deep search lacks a supported exhaustion record: require at least eight executed queries in each deep round, at least ten attempted source categories, a final exhausted action, and either fewer than five new samples in each of the latest two rounds or a fully blocked/no-results final round"
            )

    # A descriptive global low-gain pattern is not an independent delivery
    # terminal.  Restricted shortfall is legal only when the shared retrieval
    # decision has verified every below-target dimension against the formal
    # dimension audit.
    sample_exhausted_shortfall = bool(
        not sample_target_met
        and retrieval_state
        and retrieval_state.get("restricted_delivery_allowed") is True
    )
    if errors:
        result_status = "invalid"
        iteration_outcome = "invalid"
    elif bounded_shortfall_terminal and retrieval_state:
        result_status = "retrieval_terminated_with_shortfall"
        iteration_outcome = str(retrieval_state.get("termination_status"))
    elif pending_dimensions:
        result_status = "needs_iteration"
        iteration_outcome = "continue_dimension_deep_search"
    elif (
        retrieval_state
        and retrieval_state.get("termination_status")
        == "all_dimensions_formally_terminal"
    ):
        result_status = "valid" if sample_target_met else "valid_with_sample_shortfall"
        iteration_outcome = "all_dimensions_formally_terminal"
    elif not sample_target_met and not sample_exhausted_shortfall and not bounded_shortfall_terminal:
        result_status = "needs_iteration"
        iteration_outcome = "continue_deep_search"
    elif sample_target_met and not exhausted_dimension_shortfalls and not pending_dimensions:
        result_status = "valid"
        iteration_outcome = "target_met"
    elif sample_target_met:
        result_status = "valid_with_dimension_shortfall"
        iteration_outcome = "dimension_exhausted_with_shortfall"
    elif not exhausted_dimension_shortfalls and not pending_dimensions:
        result_status = "valid_with_sample_shortfall"
        iteration_outcome = "exhausted_with_shortfall"
    else:
        result_status = "valid_with_sample_and_dimension_shortfall"
        iteration_outcome = (
            "sample_and_dimension_exhausted_with_shortfall"
        )

    summary = {
        "task_run_id": task_run_id,
        "minimum_page_target": minimum_pages,
        "minimum_effective_sample_target": effective_sample_target,
        "legacy_minimum_effective_sample_floor": minimum_effective_samples,
        "target_confidence": requested_confidence or "legacy_floor",
        "source_rows": len(sources),
        "evidence_rows": len(evidence),
        "execution_rejected_source_rows": rejected_source_count,
        "execution_rejected_evidence_rows": rejected_evidence_count,
        "search_log_rows": len(search_rows),
        "executed_search_rows": int(shared_execution_metrics["actual_execution_attempt_count"]),
        "actual_execution_attempts": int(shared_execution_metrics["actual_execution_attempt_count"]),
        "valid_execution_attempts": int(shared_execution_metrics["valid_execution_attempt_count"]),
        "first_execution_attempts": int(shared_execution_metrics["first_attempt_count"]),
        "controlled_retry_attempts": int(shared_execution_metrics["controlled_retry_count"]),
        "unique_query_intents": int(shared_execution_metrics["unique_query_intent_count"]),
        "actual_attempts_by_round": shared_execution_metrics["actual_attempts_by_round"],
        "first_attempts_by_round": shared_execution_metrics["first_attempts_by_round"],
        "controlled_retries_by_round": shared_execution_metrics["controlled_retries_by_round"],
        "actual_attempts_by_dimension": shared_execution_metrics["actual_attempts_by_dimension"],
        "new_unique_query_intents_by_dimension_round": shared_execution_metrics[
            "new_unique_query_intents_by_dimension_round"
        ],
        "invalid_execution_record_count": len(execution_validation["invalid_records"]),
        "valid_execution_set_sha256": shared_execution_metrics["valid_execution_set_sha256"],
        "invalid_execution_set_sha256": shared_execution_metrics["invalid_execution_set_sha256"],
        "execution_log_schema_version": shared_execution_metrics["execution_schema_version"],
        "legacy_execution_migration": shared_execution_metrics["legacy_migration"],
        "query_budget_compliance": budget_compliance,
        "controlled_retry_audit": retry_audit,
        "deduplicated_relevant_searched_pages": len(searched_groups),
        "deduplicated_readable_source_pages": len(readable_groups),
        "access_status_counts": dict(sorted(access_counts.items())),
        "represented_source_categories": relevant_categories,
        "readable_source_categories": readable_categories,
        "attempted_source_categories": sorted(attempted_categories),
        "unattempted_source_categories": unattempted_categories,
        "readable_user_or_social_platforms": user_platforms,
        "default_excluded_source_rows_if_any": excluded_count,
        "eligible_direct_user_units": len(eligible_groups),
        "scored_direct_user_units": len(scoring_groups),
        "effective_sample_target_met": sample_target_met,
        "effective_sample_shortfall": sample_shortfall,
        "deep_iteration_rounds": len(deep_round_numbers),
        "deep_iteration_round_numbers": deep_round_numbers,
        "deep_iteration_new_samples": {
            str(round_number): int(
                shared_execution_metrics.get("gain_by_round", {}).get(str(round_number), 0)
            )
            for round_number in deep_round_numbers
        },
        "deep_iteration_query_counts": {
            str(round_number): int(
                shared_execution_metrics.get("queries_by_round", {}).get(str(round_number), 0)
            )
            for round_number in deep_round_numbers
        },
        "deep_iteration_query_depth_met": deep_query_depth_met,
        "deep_iteration_round_completion": round_completion,
        "exhaustion_source_breadth_met": source_breadth_met,
        "exhaustion_evidence_met": exhaustion_evidence,
        "iteration_outcome": iteration_outcome,
        "retrieval_termination": retrieval_state,
        "platform_dimension_gap_matrix": platform_dimension_gap_matrix(
            formal_chain,
            recent_success=platform_gap_recent_success(search_rows, schema_context=execution_schema_context),
        ),
        "page_identity_audit": page_identity_audit,
        "scored_sentiment_counts": dict(sorted(sentiment_counts.items())),
        "semantic_method_counts": dict(sorted(semantic_method_counts.items())),
        "semantic_review_status_counts": dict(sorted(semantic_review_counts.items())),
        "lexicon_match_status_counts": dict(sorted(lexicon_match_counts.items())),
        "coding_parse_status_counts": dict(sorted(coding_parse_counts.items())),
        "semantic_low_confidence_rows": semantic_low_confidence_count,
        "semantic_figurative_risk_rows": semantic_figurative_count,
        "scored_dimension_counts": {
            dimension: dimension_counts.get(dimension, 0) for dimension in sorted(DIMENSIONS)
        },
        "formal_scoring_schema_version": formal_chain["schema_version"],
        "dimension_evidence": dimension_evidence,
        "dimension_evidence_gap_targets": evidence_gap_targets,
        "dimension_evidence_enhancement_targets": enhancement_targets,
        "dimension_iteration_targets": pending_dimensions,
        "dimensions_sufficient_count": len(dimension_evidence["sufficient_dimensions"]),
        "dimensions_preferred_target_count": len(dimension_evidence["high_or_medium_high_dimensions"]),
        "dimensions_medium_terminal_count": len(dimension_evidence["medium_terminal_dimensions"]),
        "dimensions_exhausted_shortfall_count": len(exhausted_dimension_shortfalls),
        "dimensions_needing_iteration_count": len(pending_dimensions),
        "published_years": years,
    }
    return {
        "status": result_status,
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
    }


def iteration_feedback(result):
    """Transport a sole collection shortfall without changing audit calculations.

    Default audit is unchanged. This output is deliberately needs_iteration and
    therefore cannot authorize truth freeze, scoring or delivery.
    """
    import copy
    result = copy.deepcopy(result)
    summary = result.get("summary", {})
    count = summary.get("deduplicated_relevant_searched_pages")
    minimum = summary.get("minimum_page_target")
    if count is None or minimum is None or count >= minimum:
        return result
    message = f"only {count} deduplicated relevant searched pages; minimum is {minimum}"
    if result.get("errors") != [message]:
        return result
    result["errors"] = []
    result["status"] = "needs_iteration"
    result["collection_blockers"] = [message]
    summary["iteration_outcome"] = "continue_page_collection"
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--search-log", required=True, type=Path)
    parser.add_argument("--minimum-pages", type=int, default=150)
    parser.add_argument("--minimum-effective-samples", type=int, default=100)
    parser.add_argument("--minimum-dimension-evidence", type=int, default=MIN_DIMENSION_EVIDENCE_UNITS)
    parser.add_argument("--minimum-dimension-sources", type=int, default=MIN_DIMENSION_SOURCE_PAGES)
    parser.add_argument(
        "--minimum-dimension-source-categories",
        type=int,
        default=MIN_DIMENSION_SOURCE_CATEGORIES,
    )
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--iteration-feedback", action="store_true",
                        help="Classify the unchanged page-count failure as nonterminal collection feedback; never an acceptance.")
    parser.add_argument("--target-confidence", choices=("中", "中高", "高"))
    parser.add_argument("--retrieval-control", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output is None:
        raise SystemExit("formal evidence audit requires --output")
    if args.minimum_pages < 1:
        raise SystemExit("--minimum-pages must be at least 1")
    if args.minimum_effective_samples < 1:
        raise SystemExit("--minimum-effective-samples must be at least 1")
    if args.minimum_dimension_evidence < 1:
        raise SystemExit("--minimum-dimension-evidence must be at least 1")
    if args.minimum_dimension_sources < 1:
        raise SystemExit("--minimum-dimension-sources must be at least 1")
    if args.minimum_dimension_source_categories < 1:
        raise SystemExit("--minimum-dimension-source-categories must be at least 1")
    state_payload = json.loads(args.state.read_text(encoding="utf-8-sig"))
    if not isinstance(state_payload, dict):
        raise SystemExit("--state must contain one JSON object")
    controls = load_retrieval_config(
        args.retrieval_control
        if args.retrieval_control is not None
        else Path(__file__).resolve().parents[1] / "assets" / "retrieval-control.json"
    )
    state_target_confidence = str(state_payload.get("target_confidence", ""))
    if state_target_confidence not in {"中", "中高", "高"}:
        raise SystemExit("run state lacks a valid target_confidence binding")
    requested_target_confidence = args.target_confidence or state_target_confidence
    if requested_target_confidence != state_target_confidence:
        raise SystemExit("--target-confidence differs from the run-state binding")
    execution_schema_context = execution_schema_context_from_state(
        state_payload,
        config=controls,
        state_path=args.state,
    )
    authorize_runtime_write(
        state_path=args.state,
        writer_script_id="audit_evidence.py",
        output_role="evidence_audit",
        expected_phase="EVIDENCE_AUDIT",
        output_path=args.output,
    )
    for role, path in (
        ("source_ledger", args.sources),
        ("formal_evidence", args.evidence),
        ("search_log", args.search_log),
    ):
        verify_artifact_writer(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            output_role=role,
            output_path=path,
        )
    result = audit(
        args.sources,
        args.evidence,
        args.search_log,
        minimum_pages=args.minimum_pages,
        minimum_effective_samples=args.minimum_effective_samples,
        minimum_dimension_evidence=args.minimum_dimension_evidence,
        minimum_dimension_sources=args.minimum_dimension_sources,
        minimum_dimension_source_categories=args.minimum_dimension_source_categories,
        allow_incomplete=args.allow_incomplete,
        target_confidence=requested_target_confidence,
        retrieval_control_path=args.retrieval_control,
        execution_schema_context=execution_schema_context,
    )
    if args.iteration_feedback:
        result = iteration_feedback(result)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(args.output)
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="audit_evidence.py",
            output_role="evidence_audit",
            output_path=args.output,
            input_paths={
                "sources": args.sources,
                "evidence": args.evidence,
                "search_log": args.search_log,
            },
            expected_phase="EVIDENCE_AUDIT",
        )
        from runtime_dispatch import classify_audit_failure
        invalid=result['status']=='invalid'
        classification,repair_action=classify_audit_failure(result) if invalid else ('','')
        print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
            "continuation": ("SOFT_RETRY" if classification=='repairable' else "HARD_BLOCKER") if invalid else "AUTO_CONTINUE",
            "action": "repair_audit_errors" if classification=='repairable' else "restore_verified_checkpoint" if invalid
                      else "return_to_search" if result["status"]=="needs_iteration" else "freeze_truth",
            "audit_repair_action":repair_action}, ensure_ascii=False))
    else:
        print(payload, end="")
    # Auditing completed normally; research eligibility is carried by its JSON.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
