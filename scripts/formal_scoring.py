#!/usr/bin/env python3
"""Shared formal-scoring eligibility and immutable protocol contract.

This module deliberately stops at record eligibility, deduplication and
platform-by-dimension sample status.  Deliverable validation may reuse the
record-level rules, but must independently aggregate and recompute scores.
"""

from __future__ import annotations

import hashlib
import strict_json as json
import math
import re
from input_safety import finite_number, native_numbers
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from dimension_framework import DIMENSION_NAMES, DIMENSION_WEIGHTS
from runtime_guard import machine_formal_dedup_sha256, normalized_url_sha256
from source_identity import (
    annotate_page_entities,
    normalize_platform_id,
    normalized_url_hash_is_compatible,
    page_entity_id_for_source,
    trusted_independent_source_ids,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "assets" / "evaluation-protocol.json"
DEFAULT_CODEBOOK = ROOT / "assets" / "semantic-quantification-codebook.json"

TRUE_VALUES = {"true", "1", "yes", "y", "是"}
FALSE_VALUES = {"false", "0", "no", "n", "否"}
ACCEPTED_REVIEW_STATUSES = {"auto_eligible", "human_confirmed"}
SCORABLE_USER_UNITS = {"page_body", "user_post", "user_review", "comment", "reply"}
SCORABLE_ACCESS = {"full", "partial"}
FORMAL_SCORING_FIELDS = [
    "formal_dedup_sha256",
    "formal_scoring_eligible",
    "formal_scoring_exclusion_reasons",
    "formal_scoring_dedup_key",
    "platform_sample_status",
    "included_in_platform_score",
    "scoring_direction",
]


def parse_bool(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def parse_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        return None
    try:
        number = finite_number(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def split_topic_tags(value: object) -> list[str]:
    return [
        item
        for item in (part.strip() for part in re.split(r"[;；|]", str(value or "")))
        if item in DIMENSION_NAMES
    ]


def load_protocol(
    protocol_path: Path = DEFAULT_PROTOCOL,
    codebook_path: Path = DEFAULT_CODEBOOK,
) -> dict[str, object]:
    """Load and fail closed when any protected protocol component drifts."""
    protocol = json.loads(protocol_path.read_text(encoding="utf-8-sig"))
    if not isinstance(protocol, dict):
        raise ValueError("evaluation protocol root must be an object")
    codebook = json.loads(codebook_path.read_text(encoding="utf-8-sig"))
    if not isinstance(codebook, dict):
        raise ValueError("semantic codebook root must be an object")
    release = codebook.get("release_protocol")
    if not isinstance(release, dict):
        raise ValueError("semantic codebook requires release_protocol")

    expected_sha = str(protocol.get("semantic_codebook_sha256", ""))
    actual_sha = hashlib.sha256(codebook_path.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or actual_sha != expected_sha:
        raise ValueError("semantic codebook SHA-256 does not match the locked evaluation protocol")
    for field in ("evaluation_protocol_version", "semantic_codebook_version", "released_at"):
        if str(protocol.get(field, "")) != str(release.get(field, "")):
            raise ValueError(f"evaluation protocol and codebook disagree on {field}")
    calibration = protocol.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("evaluation protocol requires release calibration metadata")
    if str(calibration.get("manifest_sha256", "")) != str(release.get("manifest_sha256", "")):
        raise ValueError("evaluation protocol and codebook disagree on calibration manifest SHA-256")

    policy = protocol.get("task_execution_policy")
    if not isinstance(policy, dict) or policy.get("task_level_online_rule_refresh") is not False or policy.get("task_level_rule_mutation") is not False:
        raise ValueError("evaluation protocol must prohibit task-level rule refresh and mutation")
    weights = protocol.get("dimension_weights")
    if not isinstance(weights, dict) or list(weights) != list(DIMENSION_NAMES):
        raise ValueError("evaluation protocol dimension names or order drifted")
    for name, expected in DIMENSION_WEIGHTS.items():
        actual = weights.get(name)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isclose(float(actual), expected, abs_tol=1e-12):
            raise ValueError(f"evaluation protocol weight drifted for {name}")
    codebook_dimensions = [
        str(item.get("name", ""))
        for item in codebook.get("dimensions", [])
        if isinstance(item, dict)
    ]
    if codebook_dimensions != list(DIMENSION_NAMES):
        raise ValueError("semantic codebook dimension names or order drifted")
    for field in ("score_scale", "thresholds"):
        if protocol.get(field) != codebook.get(field):
            raise ValueError(f"evaluation protocol and semantic codebook disagree on {field}")

    formal = protocol.get("formal_scoring")
    if not isinstance(formal, dict):
        raise ValueError("evaluation protocol requires formal_scoring")
    if formal != release.get("formal_scoring"):
        raise ValueError("evaluation protocol formal_scoring differs from the codebook release lock")
    if protocol.get("immutable_during_task") != release.get("locked_task_fields"):
        raise ValueError("evaluation protocol immutable fields differ from the codebook release lock")
    if formal.get("dimension_assignment_field") != "primary_dimension":
        raise ValueError("formal scoring dimension assignment must be primary_dimension")
    if formal.get("topic_coverage_field") != "dimension_tags":
        raise ValueError("formal scoring topic coverage field must be dimension_tags")
    thresholds = formal.get("dimension_confidence_thresholds")
    if not isinstance(thresholds, dict) or list(thresholds) != ["中", "中高", "高"]:
        raise ValueError("formal scoring confidence thresholds are missing or out of order")
    minimum_units = protocol.get("thresholds", {}).get("minimum_dimension_units") if isinstance(protocol.get("thresholds"), dict) else None
    if isinstance(minimum_units, bool) or not isinstance(minimum_units, int) or minimum_units < 1:
        raise ValueError("minimum_dimension_units must be a positive integer")
    if formal.get("platform_dimension", {}).get("minimum_dimension_units") != minimum_units:
        raise ValueError("platform minimum sample rule drifted from the fixed protocol")
    return protocol


def protocol_thresholds(protocol: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    thresholds = protocol.get("thresholds")
    formal = protocol.get("formal_scoring")
    if not isinstance(thresholds, dict) or not isinstance(formal, dict):
        raise ValueError("evaluation protocol thresholds/formal_scoring are invalid")
    return thresholds, formal


def scoring_value(row: Mapping[str, object]) -> float | None:
    if str(row.get("semantic_method", "")) == "source_native_numeric":
        return parse_float(row.get("native_rating_normalized"))
    return parse_float(row.get("sentiment_score"))


def scoring_direction(value: float | None, neutral_band: float) -> str:
    if value is None:
        return "not_scored"
    if value >= neutral_band:
        return "positive"
    if value <= -neutral_band:
        return "negative"
    return "neutral"


def dedup_key(
    row: Mapping[str, object],
    source: Mapping[str, object] | None = None,
) -> str:
    return machine_formal_dedup_sha256(row, source)


def trusted_human_review(row: Mapping[str, object], source: Mapping[str, object] | None = None) -> bool:
    from review_trust import semantic_proof_valid
    if not semantic_proof_valid(row):
        return False
    from temporal_fields import review_time
    try:
        review_time(row.get("review_decision_time", ""), not_before=[
            row[field] for field in ("retrieved_at", "captured_at", "recorded_at") if row.get(field)
        ] + [source[field] for field in ("retrieved_at", "captured_at") if source and source.get(field)])
    except (ValueError, TypeError):
        return False
    return (
        str(row.get("semantic_review_status", "")) == "human_confirmed"
        and str(row.get("adjudication_status", "")) == "human_confirmed"
        and str(row.get("review_origin", "")) == "trusted_human_input"
        and bool(str(row.get("reviewer_id", "")).strip())
        and bool(str(row.get("review_record_id", "")).strip())
        and bool(str(row.get("review_decision_time", "")).strip())
        and bool(str(row.get("review_provenance_sha256", "")).strip())
        and parse_bool(row.get("review_provenance_valid")) is True
    )


def record_eligibility(
    row: Mapping[str, object],
    protocol: Mapping[str, object],
    source: Mapping[str, object] | None = None,
    *,
    require_source_contract: bool = False,
) -> dict[str, object]:
    """Return the only formal record-eligibility decision used by the Skill."""
    thresholds, formal = protocol_thresholds(protocol)
    reasons: list[str] = []
    if row.get('collision_status') == 'verified_independent_occurrence':
        from review_trust import collision_evidence_proof_valid
        if not collision_evidence_proof_valid(row):
            reasons.append('collision_review_provenance_invalid')
    dimension = str(row.get("primary_dimension", "")).strip()
    if parse_bool(row.get("used_for_scoring")) is not True:
        reasons.append("not_marked_for_scoring")
    if parse_bool(row.get("is_valid")) is not True:
        reasons.append("invalid_evidence")
    if parse_bool(row.get("is_direct_place_evidence")) is not True:
        reasons.append("not_direct_place_evidence")
    if str(row.get("score_scope", "")) != "direct":
        reasons.append("score_scope_not_direct")
    if str(row.get("entity_level", "")) == "area_aggregate":
        reasons.append("aggregate_area_evidence")
    method = str(row.get("semantic_method", "")).strip()
    unit_type = str(row.get("unit_type", "")).strip()
    from content_blocks import BLOCK_TYPES, evidence_content_errors
    content_view = dict(row)
    explicit_block_binding = any(str(row.get(field, "")).strip() for field in (
        "source_block_id", "source_block_type", "source_block_is_user_generated",
        "locator_sha256",
    ))
    # Pre-block-ledger records remain interpretable through the released source
    # contract.  This does not invent a locator: it supplies only the semantic
    # type/origin facts that the source ledger already attests, while any
    # explicit block binding continues to be validated exactly as stored.
    if source is not None and not explicit_block_binding:
        source_layer = str(source.get("content_layer", "")).strip()
        if not str(content_view.get("content_layer", "")).strip():
            content_view["content_layer"] = source_layer
        if source_layer in BLOCK_TYPES:
            content_view["source_block_type"] = source_layer
            content_view["source_block_is_user_generated"] = source.get("is_user_source", "")
    content_errors = evidence_content_errors(
        content_view,
        require_locator=(
            require_source_contract
            and unit_type != "source_native_numeric"
            and explicit_block_binding
        ),
        formal_scoring=True,
    )
    if content_errors:
        reasons.append("content_qualification_conflict")
    allowed_units = set(formal.get("eligible_unit_types", SCORABLE_USER_UNITS))
    # A verified native rating is an observational numeric unit rather than a
    # text unit.  Its score conversion remains the locked protocol conversion.
    if method == "source_native_numeric":
        allowed_units.add("source_native_numeric")
    if unit_type not in allowed_units:
        reasons.append("unit_type_not_scorable")
    if dimension not in DIMENSION_NAMES:
        reasons.append("invalid_primary_dimension")
    evidence_id = str(row.get("evidence_id", "")).strip()
    if not evidence_id:
        reasons.append("missing_evidence_id")
    group = dedup_key(row, source)
    if not group:
        reasons.append("missing_dedup_key")

    prohibited_methods = set(formal.get("prohibited_semantic_methods", ["coding_parse_fallback", "not_applicable", ""]))
    if method in prohibited_methods:
        reasons.append("semantic_method_not_formally_scorable")
    reliability = parse_float(row.get("evidence_reliability"))
    minimum_reliability = float(thresholds["minimum_evidence_reliability"])
    if reliability is None or reliability < minimum_reliability:
        reasons.append("evidence_reliability_below_threshold")

    score = scoring_value(row)
    if score is None or score < float(protocol["score_scale"]["minimum"]) or score > float(protocol["score_scale"]["maximum"]):
        reasons.append("missing_or_invalid_scoring_value")
    if method == "source_native_numeric":
        try:
            native_numbers(row)
        except ValueError:
            reasons.append("invalid_native_numeric_scale")
        for field in ("native_rating_value", "native_rating_scale_min", "native_rating_scale_max", "native_rating_normalized"):
            if parse_float(row.get(field)) is None:
                reasons.append("incomplete_native_numeric_scale")
                break
        weight = parse_float(row.get("aggregation_weight"))
        if weight is None or weight <= 0:
            reasons.append("missing_aggregation_weight")
    elif method not in prohibited_methods:
        review_status = str(row.get("semantic_review_status", ""))
        if review_status not in ACCEPTED_REVIEW_STATUSES:
            reasons.append("semantic_review_not_confirmed")
        elif review_status == "human_confirmed" and not trusted_human_review(row, source):
            reasons.append("review_provenance_invalid")
        confidence = parse_float(row.get("semantic_confidence"))
        aspect = parse_float(row.get("aspect_confidence"))
        weight = parse_float(row.get("aggregation_weight"))
        if confidence is None or confidence < float(thresholds["semantic_auto_confidence"]):
            reasons.append("semantic_confidence_below_threshold")
        if aspect is None or aspect < float(thresholds["dimension_auto_confidence"]):
            reasons.append("dimension_confidence_below_threshold")
        if weight is None or weight <= 0:
            reasons.append("missing_aggregation_weight")
        elif confidence is not None and reliability is not None and not math.isclose(weight, confidence * reliability, abs_tol=0.02):
            reasons.append("aggregation_weight_mismatch")
        final_score = parse_float(row.get("semantic_score_final"))
        if final_score is None or score is None or not math.isclose(final_score, score, abs_tol=1e-9):
            reasons.append("semantic_final_score_mismatch")
        if parse_bool(row.get("figurative_risk")) is True and review_status != "human_confirmed":
            reasons.append("figurative_risk_not_human_confirmed")
        if not str(row.get("semantic_unit_text", "")).strip():
            reasons.append("missing_semantic_unit_text")
        if not str(row.get("semantic_rule_trace", "")).strip():
            reasons.append("missing_semantic_rule_trace")
    if method != "source_native_numeric":
        if not str(row.get("original_summary_text", "")).strip():
            reasons.append("missing_original_summary_text")
        if not str(row.get("excerpt_or_summary", "")).strip():
            reasons.append("missing_excerpt_or_summary")

    if require_source_contract:
        if source is None:
            reasons.append("missing_linked_source")
        else:
            from source_identity import source_qualification_errors, linked_evidence_errors
            reasons.extend(source_qualification_errors(source))
            reasons.extend(linked_evidence_errors(row, source))
            from source_identity import identity_url
            expected_url_hash = normalized_url_sha256(identity_url(source))
            if not expected_url_hash or source.get("normalized_url_sha256", "") != expected_url_hash:
                reasons.append("source_normalized_url_hash_invalid")
            if parse_bool(source.get("used_for_scoring")) is not True:
                reasons.append("source_not_marked_for_scoring")
            if parse_bool(source.get("is_relevant")) is not True:
                reasons.append("source_not_relevant")
            if parse_bool(source.get("is_user_source")) is not True:
                reasons.append("source_not_user_evidence")
            if parse_bool(source.get("suspected_promotion")) is not False:
                reasons.append("source_promotional_or_unknown")
            source_access = str(source.get("access_status", ""))
            native_metadata = (
                method == "source_native_numeric"
                and source_access == "metadata_only"
                and str(source.get("content_layer", "")) == "rating_only"
            )
            if source_access not in SCORABLE_ACCESS and not native_metadata:
                reasons.append("source_not_readable")
            if str(source.get("score_scope", "")) != "direct":
                reasons.append("source_scope_not_direct")
            if str(source.get("entity_level", "")) == "area_aggregate":
                reasons.append("source_is_area_aggregate")

    declared_platform = normalize_platform_id(row.get("platform", ""))
    authoritative_platform = declared_platform
    if source is not None:
        source_declared_platform = normalize_platform_id(source.get("platform", ""))
        from source_identity import platform_identity
        authoritative_platform = platform_identity(source.get("url", ""), source.get("final_url", ""))["platform_id"]
        # The evidence cache must agree with the source-ledger declaration.
        # The URL-derived platform_id is then authoritative for aggregation.
        # Keeping these two checks distinct allows a released alias such as
        # ``Ctrip``/``携程`` to normalize to one ID without asking a candidate
        # evidence row to predict hostname resolution internals.
        comparison_platform = source_declared_platform or authoritative_platform
        if declared_platform and declared_platform != comparison_platform:
            raise ValueError("evidence platform differs from authoritative source ledger")
        if require_source_contract and not authoritative_platform:
            reasons.append("source_platform_missing")
    neutral_band = float(protocol["score_scale"]["neutral_band"])
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "primary_dimension": dimension,
        "evidence_id": evidence_id,
        "dedup_key": group,
        "platform": authoritative_platform or "未标注平台",
        "score": score,
        "aggregation_weight": parse_float(row.get("aggregation_weight")),
        "direction": scoring_direction(score, neutral_band),
    }


def _research_unit(row: Mapping[str, object]) -> bool:
    return (
        parse_bool(row.get("is_valid")) is True
        and parse_bool(row.get("is_direct_place_evidence")) is True
        and str(row.get("score_scope", "")) == "direct"
        and str(row.get("primary_dimension", "")) in DIMENSION_NAMES
    )


def build_formal_scoring_chain(
    rows: list[dict[str, str]],
    sources: list[dict[str, str]] | None = None,
    protocol: Mapping[str, object] | None = None,
    *,
    require_source_contract: bool | None = None,
) -> dict[str, object]:
    """Build formal candidates and platform-scored units from bottom records."""
    protocol = dict(protocol or load_protocol())
    thresholds, formal = protocol_thresholds(protocol)
    minimum_units = int(thresholds["minimum_dimension_units"])
    normalized_sources, _page_identity_audit = annotate_page_entities(
        sources or [],
        trusted_independent_source_ids=trusted_independent_source_ids(rows),
    )
    source_lookup = {
        row.get("source_id", ""): row
        for row in normalized_sources
        if row.get("source_id", "")
    }
    if require_source_contract is None:
        require_source_contract = bool(sources) and any("used_for_scoring" in row for row in sources or [])

    decisions: list[dict[str, object]] = []
    seen_dimension_groups: set[tuple[str, str]] = set()
    eligible_rows: list[dict[str, str]] = []
    normalized_rows: list[dict[str, str]] = []
    decision_by_evidence_id: dict[str, dict[str, object]] = {}
    for row in rows:
        source = source_lookup.get(row.get("source_id", ""))
        decision = record_eligibility(
            row,
            protocol,
            source,
            require_source_contract=require_source_contract,
        )
        normalized = dict(row)
        normalized["platform"] = str(decision["platform"])
        normalized["formal_dedup_sha256"] = str(decision["dedup_key"])
        normalized_rows.append(normalized)
        dimension_group = (str(decision["primary_dimension"]), str(decision["dedup_key"]))
        if decision["eligible"] and dimension_group in seen_dimension_groups:
            decision["eligible"] = False
            decision["reasons"] = [*decision["reasons"], "duplicate_within_primary_dimension"]
        elif decision["eligible"]:
            seen_dimension_groups.add(dimension_group)
            eligible_rows.append(normalized)
        decisions.append(decision)
        evidence_id = str(decision["evidence_id"])
        if evidence_id:
            decision_by_evidence_id[evidence_id] = decision

    grouped: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in eligible_rows:
        grouped[(row.get("platform", "未标注平台") or "未标注平台", row["primary_dimension"])].append(row)
    scorable_groups = {key for key, items in grouped.items() if len(items) >= minimum_units}
    scored_rows = [row for key, items in grouped.items() if key in scorable_groups for row in items]
    scored_ids = {row.get("evidence_id", "") for row in scored_rows}

    for decision in decisions:
        if not decision["eligible"]:
            decision["platform_sample_status"] = "not_eligible"
            decision["included_in_platform_score"] = False
            continue
        key = (str(decision["platform"]), str(decision["primary_dimension"]))
        included = key in scorable_groups and str(decision["evidence_id"]) in scored_ids
        decision["platform_sample_status"] = "scored" if included else "insufficient_platform_samples"
        decision["included_in_platform_score"] = included

    retrieved_by_dimension: dict[str, list[dict[str, str]]] = {name: [] for name in DIMENSION_NAMES}
    topic_by_dimension: dict[str, list[dict[str, str]]] = {name: [] for name in DIMENSION_NAMES}
    retrieved_seen: set[tuple[str, str]] = set()
    topic_seen: set[tuple[str, str]] = set()
    for row in normalized_rows:
        if not _research_unit(row):
            continue
        primary = row["primary_dimension"]
        group = dedup_key(row, source_lookup.get(row.get("source_id", "")))
        if group and (primary, group) not in retrieved_seen:
            retrieved_seen.add((primary, group))
            retrieved_by_dimension[primary].append(row)
        for tag in split_topic_tags(row.get("dimension_tags")):
            if group and (tag, group) not in topic_seen:
                topic_seen.add((tag, group))
                topic_by_dimension[tag].append(row)

    eligible_by_dimension: dict[str, list[dict[str, str]]] = {name: [] for name in DIMENSION_NAMES}
    scored_by_dimension: dict[str, list[dict[str, str]]] = {name: [] for name in DIMENSION_NAMES}
    for row in eligible_rows:
        eligible_by_dimension[row["primary_dimension"]].append(row)
    for row in scored_rows:
        scored_by_dimension[row["primary_dimension"]].append(row)

    dimension_summary: dict[str, dict[str, object]] = {}
    confidence_thresholds = formal["dimension_confidence_thresholds"]
    assert isinstance(confidence_thresholds, dict)
    for dimension in DIMENSION_NAMES:
        eligible_items = eligible_by_dimension[dimension]
        scored_items = scored_by_dimension[dimension]
        scored_page_ids = {
            page_entity_id_for_source(source_lookup[row.get("source_id", "")])
            for row in scored_items
            if row.get("source_id", "") in source_lookup
        }
        scored_page_ids.discard("")
        scored_source_ids = {
            row.get("source_id", "") for row in scored_items if row.get("source_id", "")
        }
        categories = {
            source_lookup[source_id].get("source_category", "")
            for source_id in scored_source_ids
            if source_id in source_lookup and source_lookup[source_id].get("source_category", "")
        }
        scorable_platforms = sorted({row.get("platform", "未标注平台") or "未标注平台" for row in scored_items})
        candidate_platforms = sorted({row.get("platform", "未标注平台") or "未标注平台" for row in eligible_items})
        directions = Counter(
            scoring_direction(scoring_value(row), float(protocol["score_scale"]["neutral_band"]))
            for row in scored_items
        )
        confidence = "数据不足"
        for label in ("中", "中高", "高"):
            rule = confidence_thresholds[label]
            if (
                len(scored_items) >= int(rule["scored_evidence_units"])
                and len(scored_page_ids) >= int(rule["distinct_source_pages"])
                and len(categories) >= int(rule["source_categories"])
                and len(scorable_platforms) >= int(rule["scorable_platforms"])
            ):
                confidence = label
        dimension_summary[dimension] = {
            "retrieved_evidence_units": len(retrieved_by_dimension[dimension]),
            "topic_coverage_evidence_units": len(topic_by_dimension[dimension]),
            "eligible_evidence_units": len(eligible_items),
            "scored_evidence_units": len(scored_items),
            "scoring_positive_units": directions["positive"],
            "scoring_neutral_units": directions["neutral"],
            "scoring_negative_units": directions["negative"],
            "candidate_platforms": candidate_platforms,
            "dimension_candidate_platform_count": len(candidate_platforms),
            "scorable_platforms": scorable_platforms,
            "dimension_scorable_platform_count": len(scorable_platforms),
            "distinct_source_pages": len(scored_page_ids),
            "source_categories": sorted(categories),
            "source_category_count": len(categories),
            "scoring_confidence": confidence,
        }

    platform_names = sorted({str(decision["platform"]) for decision in decisions if decision["eligible"]})
    platforms: list[dict[str, object]] = []
    for platform in platform_names:
        platform_eligible = [row for row in eligible_rows if (row.get("platform", "未标注平台") or "未标注平台") == platform]
        platform_scored = [row for row in scored_rows if (row.get("platform", "未标注平台") or "未标注平台") == platform]
        dimensions: dict[str, object] = {}
        for dimension in DIMENSION_NAMES:
            candidates = [row for row in platform_eligible if row["primary_dimension"] == dimension]
            selected = [row for row in platform_scored if row["primary_dimension"] == dimension]
            directions = Counter(
                scoring_direction(scoring_value(row), float(protocol["score_scale"]["neutral_band"]))
                for row in selected
            )
            weighted_total = sum(float(scoring_value(row)) * float(parse_float(row.get("aggregation_weight")) or 0) for row in selected)
            weight_total = sum(float(parse_float(row.get("aggregation_weight")) or 0) for row in selected)
            tendency = None
            if selected and weight_total > 0:
                raw = weighted_total / weight_total
                tendency = math.floor(raw + 0.5) if raw >= 0 else math.ceil(raw - 0.5)
            status = "scored" if selected else "insufficient_platform_samples"
            dimensions[dimension] = {
                "eligible_evidence_units": len(candidates),
                "scored_evidence_units": len(selected),
                "scoring_positive_units": directions["positive"],
                "scoring_neutral_units": directions["neutral"],
                "scoring_negative_units": directions["negative"],
                "minimum_dimension_units": minimum_units,
                "minimum_sample_met": bool(selected),
                "platform_sample_status": status,
                "tendency": tendency,
            }
        platforms.append(
            {
                "platform": platform,
                "eligible_samples": len(platform_eligible),
                "effective_samples": len(platform_scored),
                "sample_count_reliability": "exact",
                "dimensions": dimensions,
            }
        )

    return {
        "schema_version": str(formal.get("schema_version", "formal-scoring-chain-1")),
        "evaluation_protocol_version": protocol.get("evaluation_protocol_version"),
        "semantic_codebook_version": protocol.get("semantic_codebook_version"),
        "minimum_dimension_units": minimum_units,
        "record_decisions": decisions,
        "eligible_rows": eligible_rows,
        "scored_rows": scored_rows,
        "platforms": platforms,
        "dimensions": dimension_summary,
    }


def annotate_rows(rows: list[dict[str, str]], chain: Mapping[str, object]) -> list[dict[str, str]]:
    decisions = chain.get("record_decisions")
    if not isinstance(decisions, list) or len(decisions) != len(rows):
        raise ValueError("formal scoring decisions do not align with evidence rows")
    output: list[dict[str, str]] = []
    for row, raw_decision in zip(rows, decisions):
        if not isinstance(raw_decision, dict):
            raise ValueError("formal scoring decision must be an object")
        updated = dict(row)
        updated.update(
            {
                "platform": str(raw_decision["platform"]),
                "formal_dedup_sha256": str(raw_decision["dedup_key"]),
                "formal_scoring_eligible": "true" if raw_decision["eligible"] else "false",
                "formal_scoring_exclusion_reasons": ";".join(str(item) for item in raw_decision["reasons"]),
                "formal_scoring_dedup_key": str(raw_decision["dedup_key"]),
                "platform_sample_status": str(raw_decision["platform_sample_status"]),
                "included_in_platform_score": "true" if raw_decision["included_in_platform_score"] else "false",
                "scoring_direction": str(raw_decision["direction"]) if raw_decision["included_in_platform_score"] else "not_scored",
            }
        )
        output.append(updated)
    return output
