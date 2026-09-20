#!/usr/bin/env python3
"""Independently audit evidence and search termination for every analysis dimension."""

from __future__ import annotations

import copy
import strict_json as json
import re
from collections import Counter, defaultdict
from typing import Iterable, Mapping

from dimension_framework import DIMENSION_NAMES, RESULT_LAYER_NAME
from formal_scoring import (
    build_formal_scoring_chain,
    load_protocol,
    scoring_direction,
    scoring_value,
)
from runtime_guard import assert_single_task_run, canonical_sha256, records_sha256
from execution_facts import set_hash as execution_set_hash
from formal_states import (
    AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
    formal_dimension_lifecycle,
)
from retrieval_controls import (
    CONFIDENCE_ORDER,
    dimension_exhaustion_signature,
    dimension_medium_terminal_signature,
    executed_query_metrics,
    executed_search_rows,
    load_retrieval_config,
    parse_dimension_targets,
    retrieval_termination,
    validate_query_intent_signature,
)
from source_identity import (
    annotate_page_entities,
    page_entity_id_for_source,
    trusted_independent_source_ids,
)


# “中”是正式评价最低门槛，不是检索任务的正常终止目标。
# 这些正式评分阈值只从发布版 evaluation-protocol.json 读取。
_PROTOCOL = load_protocol()
_CONFIDENCE_THRESHOLDS = _PROTOCOL["formal_scoring"]["dimension_confidence_thresholds"]
MIN_DIMENSION_EVIDENCE_UNITS = int(_CONFIDENCE_THRESHOLDS["中"]["scored_evidence_units"])
MIN_DIMENSION_SOURCE_PAGES = int(_CONFIDENCE_THRESHOLDS["中"]["distinct_source_pages"])
MIN_DIMENSION_SOURCE_CATEGORIES = int(_CONFIDENCE_THRESHOLDS["中"]["source_categories"])
MEDIUM_HIGH_EVIDENCE_UNITS = int(_CONFIDENCE_THRESHOLDS["中高"]["scored_evidence_units"])
MEDIUM_HIGH_SOURCE_PAGES = int(_CONFIDENCE_THRESHOLDS["中高"]["distinct_source_pages"])
MEDIUM_HIGH_SOURCE_CATEGORIES = int(_CONFIDENCE_THRESHOLDS["中高"]["source_categories"])
HIGH_EVIDENCE_UNITS = int(_CONFIDENCE_THRESHOLDS["高"]["scored_evidence_units"])
HIGH_SOURCE_PAGES = int(_CONFIDENCE_THRESHOLDS["高"]["distinct_source_pages"])
HIGH_SOURCE_CATEGORIES = int(_CONFIDENCE_THRESHOLDS["高"]["source_categories"])

# 低于“中”时的严格穷尽审计。
MIN_DIMENSION_DEEP_ROUNDS = 4
MIN_DIMENSION_QUERIES_PER_ROUND = 8
DIMENSION_LOW_YIELD_LIMIT = 2
MIN_DIMENSION_TARGET_SOURCE_CATEGORIES = 5
MIN_DIMENSION_TRACED_SOURCE_PAGES = 12
MIN_DIMENSION_TRACED_DOMAINS = 3
MIN_BLOCKED_ROUTE_SOURCE_PAGES = 5
MIN_BLOCKED_ROUTE_DOMAINS = 2

# 达到“中”但希望在此结束检索时，使用更强的独立终止审计。
MIN_MEDIUM_TOTAL_TARGETED_ROUNDS = 5
MIN_MEDIUM_ENHANCEMENT_ROUNDS = 3
MIN_MEDIUM_ENHANCEMENT_QUERIES_PER_ROUND = 10
MIN_MEDIUM_TARGET_SOURCE_CATEGORIES = 6
MEDIUM_LOW_YIELD_LIMIT = 2
MIN_MEDIUM_TRACED_SOURCE_PAGES = 20
MIN_MEDIUM_TRACED_DOMAINS = 5
MIN_MEDIUM_TRACED_SOURCE_CATEGORIES = 4
MIN_MEDIUM_BLOCKED_ROUTE_SOURCE_PAGES = 10
MIN_MEDIUM_BLOCKED_ROUTE_DOMAINS = 4
MIN_MEDIUM_SUBJECT_GROUPS = 3
MIN_MEDIUM_TIME_CONTEXTS = 3

FINAL_DIMENSION_STATUSES = {
    "sufficient",
    "sufficient_at_requested_target",
    "sufficient_at_medium_after_audit",
    "exhausted_with_shortfall",
    AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
}
TRUE_VALUES = {"true", "1", "yes", "y", "是"}
MEDIUM_ENHANCEMENT_MODE = "medium_enhancement"

SUBJECT_QUERY_PATTERNS = {
    "本地居民": ("本地居民", "居民", "街坊", "原住民", "本地人"),
    "商户": ("商户", "店主", "经营者", "老字号"),
    "游客": ("游客", "访客", "外地游客"),
    "传承人或文化实践者": ("传承人", "文化实践者", "非遗传承", "艺人"),
    "管理者": ("管理者", "管理部门", "管理人员", "治理"),
    "专业人士": ("专业人士", "专家", "学者", "规划师", "建筑师"),
    "其他公众": ("公众", "市民", "网友"),
}
TIME_QUERY_PATTERNS = {
    "当前状态": ("当前", "现在", "近期", "近年"),
    "历史回忆": ("历史回忆", "以前", "过去", "老照片"),
    "改造前后比较": ("改造前后", "更新前后", "修缮前后"),
    "长期变化": ("长期变化", "多年变化", "持续变化", "长期问题"),
    "工作日": ("工作日",),
    "周末": ("周末",),
    "节假日": ("节假日", "假期", "高峰"),
    "节庆活动期间": ("节庆", "节日活动", "活动期间"),
    "白天": ("白天", "日间"),
    "夜间": ("夜间", "晚上", "夜游"),
}
POSITIVE_QUERY_TERMS = ("正面", "推荐", "优点", "认可", "喜欢", "值得")
NEGATIVE_QUERY_TERMS = ("负面", "缺点", "问题", "避雷", "投诉", "风险", "冲突")


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def parse_round(value: object) -> int | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+", text):
        return None
    return int(text)


def split_dimensions(row: dict[str, str]) -> list[str]:
    raw = row.get("dimension_tags", "") or row.get("primary_dimension", "")
    values = [part.strip() for part in re.split(r"[;；|]", raw) if part.strip()]
    primary = row.get("primary_dimension", "").strip()
    if primary and primary not in values:
        values.insert(0, primary)
    return [value for value in values if value in DIMENSION_NAMES]


def targets_dimension(row: dict[str, str], dimension: str) -> bool:
    return dimension in parse_dimension_targets(row)


def deduplicate_dimension_rows(
    evidence: Iterable[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    chain = build_formal_scoring_chain(list(evidence), protocol=_PROTOCOL)
    output: dict[str, list[dict[str, str]]] = {name: [] for name in DIMENSION_NAMES}
    for row in chain["scored_rows"]:
        output[row["primary_dimension"]].append(row)
    return output


def query_group_hits(text: str, patterns: dict[str, tuple[str, ...]]) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {
        label
        for label, terms in patterns.items()
        if any(re.sub(r"\s+", "", term) in compact for term in terms)
    }


def evaluate_dimension_evidence(
    evidence: list[dict[str, str]],
    sources: list[dict[str, str]],
    search_rows: list[dict[str, str]],
    *,
    minimum_evidence_units: int = MIN_DIMENSION_EVIDENCE_UNITS,
    minimum_source_pages: int = MIN_DIMENSION_SOURCE_PAGES,
    minimum_source_categories: int = MIN_DIMENSION_SOURCE_CATEGORIES,
    minimum_deep_rounds: int = MIN_DIMENSION_DEEP_ROUNDS,
    minimum_queries_per_round: int = MIN_DIMENSION_QUERIES_PER_ROUND,
    target_confidence: str = "中高",
    execution_schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if target_confidence not in {"中", "中高", "高"}:
        raise ValueError("target_confidence must be 中, 中高, or 高")
    for name, value in (
        ("minimum_evidence_units", minimum_evidence_units),
        ("minimum_source_pages", minimum_source_pages),
        ("minimum_source_categories", minimum_source_categories),
        ("minimum_deep_rounds", minimum_deep_rounds),
        ("minimum_queries_per_round", minimum_queries_per_round),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    locked_values = {
        "minimum_evidence_units": MIN_DIMENSION_EVIDENCE_UNITS,
        "minimum_source_pages": MIN_DIMENSION_SOURCE_PAGES,
        "minimum_source_categories": MIN_DIMENSION_SOURCE_CATEGORIES,
    }
    supplied_values = {
        "minimum_evidence_units": minimum_evidence_units,
        "minimum_source_pages": minimum_source_pages,
        "minimum_source_categories": minimum_source_categories,
    }
    for name, expected in locked_values.items():
        if supplied_values[name] != expected:
            raise ValueError(f"{name} is locked by evaluation-protocol.json at {expected}")

    # Page identity is a derived property of the complete source collection.
    # Rebuild it here so every caller, including an independent validator that
    # reloads the unmodified CSV, evaluates the same globally de-duplicated
    # page entities.
    sources, _page_identity_audit = annotate_page_entities(
        sources,
        trusted_independent_source_ids=trusted_independent_source_ids(evidence),
    )

    source_by_id = {row.get("source_id", ""): row for row in sources if row.get("source_id", "")}
    from execution_facts import validate_source_links
    from retrieval_controls import validate_execution_records
    facts = validate_execution_records(search_rows, schema_context=execution_schema_context)
    source_links = validate_source_links(sources, facts)
    allowed_source_ids = {row['source_id'] for row in source_links['valid_sources']}
    if facts['status'] != 'valid' or source_links['status'] != 'valid' or len(source_links['valid_sources']) != len(sources):
        # Invalid provenance can never support a formal dimension score.
        evidence = [row for row in evidence if row.get('source_id') in allowed_source_ids]
        sources, _page_identity_audit = annotate_page_entities(list(source_links['valid_sources']),
            trusted_independent_source_ids=trusted_independent_source_ids(evidence))
        source_by_id = {row['source_id']: row for row in sources}
    search_by_id = {str(row['execution_id']): row for row in facts['valid_rows']}
    from source_identity import linked_evidence_errors
    evidence = [row for row in evidence if not linked_evidence_errors(row, source_by_id.get(row.get('source_id')))]
    scoring_chain = build_formal_scoring_chain(evidence, sources, protocol=_PROTOCOL)
    dimension_rows: dict[str, list[dict[str, str]]] = {name: [] for name in DIMENSION_NAMES}
    for row in scoring_chain["scored_rows"]:
        dimension_rows[row["primary_dimension"]].append(row)
    scoring_summaries = scoring_chain["dimensions"]
    search_metrics = executed_query_metrics(
        search_rows,
        schema_context=execution_schema_context,
    )
    valid_search_rows = executed_search_rows(
        search_rows,
        schema_context=execution_schema_context,
    )
    results: dict[str, dict[str, object]] = {}

    for dimension in DIMENSION_NAMES:
        rows = dimension_rows[dimension]
        formal_summary = scoring_summaries[dimension]
        source_ids = {
            row.get("source_id", "")
            for row in rows
            if row.get("source_id", "") in source_by_id
        }
        categories = set(formal_summary["source_categories"])
        platforms = set(formal_summary["scorable_platforms"])
        neutral_band = float(_PROTOCOL["score_scale"]["neutral_band"])
        sentiments = Counter(scoring_direction(scoring_value(row), neutral_band) for row in rows)
        user_groups = Counter(row.get("user_group", "unknown") for row in rows)
        time_contexts = Counter(row.get("time_context", "unknown") for row in rows)

        # Planned/not-run rows are not evidence that a search route was
        # attempted.  Use the shared execution classifier everywhere this
        # audit derives depth, breadth, gain, blockage, or exhaustion.
        targeted = [
            row
            for row in valid_search_rows
            if (parse_round(row.get("iteration_round")) or 0) > 0
            and targets_dimension(row, dimension)
        ]
        queries_by_round: defaultdict[int, int] = defaultdict(int)
        statuses_by_round: defaultdict[int, set[str]] = defaultdict(set)
        actions_by_round: defaultdict[int, set[str]] = defaultdict(set)
        targeted_query_ids_by_round: defaultdict[int, set[str]] = defaultdict(set)
        targeted_source_categories: set[str] = set()
        targeted_search_tools: set[str] = set()

        enhancement_queries_by_round: defaultdict[int, int] = defaultdict(int)
        enhancement_statuses_by_round: defaultdict[int, set[str]] = defaultdict(set)
        enhancement_actions_by_round: defaultdict[int, set[str]] = defaultdict(set)
        enhancement_query_ids_by_round: defaultdict[int, set[str]] = defaultdict(set)
        enhancement_target_categories: set[str] = set()
        enhancement_subject_groups: set[str] = set()
        enhancement_time_contexts: set[str] = set()
        enhancement_polarities: set[str] = set()
        counted_intents: set[str] = set()
        counted_enhancement_intents: set[str] = set()

        for row in targeted:
            round_number = parse_round(row.get("iteration_round"))
            if round_number is None or round_number < 1:
                continue
            status = row.get("status", "")
            executed = True
            # The stored signature is only an integrity assertion.  Counting
            # always uses the value derived by the released implementation.
            intent = str(validate_query_intent_signature(row)["derived"])
            unique_intent = bool(row.get('_new_coverage_intent'))
            counted_intents.add(intent)
            if unique_intent:
                queries_by_round[round_number] += 1
            if status:
                statuses_by_round[round_number].add(status)
            action = row.get("next_action", "")
            if action:
                actions_by_round[round_number].add(action)
            query_id = row.get("query_id", "") or row.get("search_id", "")
            if query_id:
                targeted_query_ids_by_round[round_number].add(query_id)
            source_category = row.get("source_category_target", "")
            if unique_intent and source_category and source_category != "mixed":
                targeted_source_categories.add(source_category)
            search_tool = row.get("search_tool", "")
            if search_tool:
                targeted_search_tools.add(search_tool)

            # The query plan owns the round contract.  A confidence snapshot
            # can change between planning and execution and must not erase a
            # formally scheduled enhancement round from the completion audit.
            is_medium_enhancement = row.get("iteration_mode", "") == MEDIUM_ENHANCEMENT_MODE
            if not is_medium_enhancement:
                continue
            unique_enhancement_intent = unique_intent
            counted_enhancement_intents.add(intent)
            if unique_enhancement_intent:
                enhancement_queries_by_round[round_number] += 1
            if status:
                enhancement_statuses_by_round[round_number].add(status)
            if action:
                enhancement_actions_by_round[round_number].add(action)
            if query_id:
                enhancement_query_ids_by_round[round_number].add(query_id)
            if unique_enhancement_intent and source_category and source_category != "mixed":
                enhancement_target_categories.add(source_category)
            if not unique_enhancement_intent:
                continue
            query_text = " ".join(
                str(row.get(key, "")) for key in ("gap_target", "query", "notes")
            )
            enhancement_subject_groups.update(query_group_hits(query_text, SUBJECT_QUERY_PATTERNS))
            enhancement_time_contexts.update(query_group_hits(query_text, TIME_QUERY_PATTERNS))
            compact_query = re.sub(r"\s+", "", query_text)
            if any(term in compact_query for term in POSITIVE_QUERY_TERMS):
                enhancement_polarities.add("positive")
            if any(term in compact_query for term in NEGATIVE_QUERY_TERMS):
                enhancement_polarities.add("negative")

        all_targeted_query_ids = (
            set().union(*targeted_query_ids_by_round.values())
            if targeted_query_ids_by_round else set()
        )
        all_enhancement_query_ids = (
            set().union(*enhancement_query_ids_by_round.values())
            if enhancement_query_ids_by_round else set()
        )

        def traced(query_ids: set[str]) -> list[dict[str, str]]:
            return [
                row for row in sources
                if row.get("query_id", "") in query_ids and row.get('source_id') in allowed_source_ids and truthy(row.get("is_relevant"))
            ]

        traced_sources = traced(all_targeted_query_ids)
        traced_enhancement_sources = traced(all_enhancement_query_ids)

        def trace_metrics(items: list[dict[str, str]]) -> tuple[set[str], set[str], set[str]]:
            return (
                {
                    page_entity_id_for_source(row)
                    for row in items
                    if page_entity_id_for_source(row)
                },
                {__import__('source_identity').trusted_domain(__import__('source_identity').identity_url(row)) for row in items},
                {row.get("source_category", "") for row in items if row.get("source_category", "")},
            )

        traced_source_ids, traced_domains, traced_categories = trace_metrics(traced_sources)
        medium_source_ids, medium_domains, medium_categories = trace_metrics(traced_enhancement_sources)

        new_evidence_by_round: defaultdict[int, int] = defaultdict(int)
        for row in rows:
            source = source_by_id.get(row.get("source_id", ""), {})
            query_id = source.get("query_id", "")
            search = search_by_id.get(str(source.get('execution_id', '')), {})
            round_number = parse_round(search.get("iteration_round"))
            if round_number is None or query_id not in targeted_query_ids_by_round.get(round_number, set()):
                continue
            new_evidence_by_round[round_number] += 1

        targeted_rounds = sorted(queries_by_round)
        query_depth_met = (
            len(targeted_rounds) >= minimum_deep_rounds
            and all(queries_by_round[number] >= minimum_queries_per_round for number in targeted_rounds)
        )
        final_round = max(targeted_rounds, default=0)
        latest_two = targeted_rounds[-2:]
        low_yield = (
            len(latest_two) == 2
            and all(new_evidence_by_round[number] < DIMENSION_LOW_YIELD_LIMIT for number in latest_two)
        )
        blocked_or_empty = bool(final_round) and bool(statuses_by_round.get(final_round)) and all(
            status in {"blocked", "no_results"} for status in statuses_by_round[final_round]
        )
        low_yield_trace_met = (
            low_yield
            and len(traced_source_ids) >= MIN_DIMENSION_TRACED_SOURCE_PAGES
            and len(traced_domains) >= MIN_DIMENSION_TRACED_DOMAINS
            and len(traced_categories) >= 3
        )
        blocked_route_trace_met = (
            blocked_or_empty
            and len(traced_source_ids) >= MIN_BLOCKED_ROUTE_SOURCE_PAGES
            and len(traced_domains) >= MIN_BLOCKED_ROUTE_DOMAINS
        )
        exhaustion_checks = {
            "minimum_four_targeted_rounds": len(targeted_rounds) >= minimum_deep_rounds,
            "minimum_eight_executed_queries_each_round": query_depth_met,
            "minimum_five_target_source_categories": (
                len(targeted_source_categories) >= MIN_DIMENSION_TARGET_SOURCE_CATEGORIES
            ),
            "final_round_exhausted_action": "exhausted" in actions_by_round.get(final_round, set()),
            "cross_ledger_low_yield_route": low_yield_trace_met,
            "cross_ledger_blocked_route": blocked_route_trace_met,
        }
        exhaustion_supported = (
            exhaustion_checks["minimum_four_targeted_rounds"]
            and exhaustion_checks["minimum_eight_executed_queries_each_round"]
            and exhaustion_checks["minimum_five_target_source_categories"]
            and exhaustion_checks["final_round_exhausted_action"]
            and (
                exhaustion_checks["cross_ledger_low_yield_route"]
                or exhaustion_checks["cross_ledger_blocked_route"]
            )
        )

        enhancement_rounds = sorted(enhancement_queries_by_round)
        latest_three_enhancement = enhancement_rounds[-3:]
        medium_low_yield = (
            len(latest_three_enhancement) == 3
            and all(
                new_evidence_by_round[number] < MEDIUM_LOW_YIELD_LIMIT
                for number in latest_three_enhancement
            )
        )
        final_enhancement_round = max(enhancement_rounds, default=0)
        medium_blocked_or_empty = (
            bool(final_enhancement_round)
            and bool(enhancement_statuses_by_round.get(final_enhancement_round))
            and all(
                status in {"blocked", "no_results"}
                for status in enhancement_statuses_by_round[final_enhancement_round]
            )
        )
        medium_low_yield_trace_met = (
            medium_low_yield
            and len(medium_source_ids) >= MIN_MEDIUM_TRACED_SOURCE_PAGES
            and len(medium_domains) >= MIN_MEDIUM_TRACED_DOMAINS
            and len(medium_categories) >= MIN_MEDIUM_TRACED_SOURCE_CATEGORIES
        )
        medium_blocked_route_trace_met = (
            medium_blocked_or_empty
            and len(medium_source_ids) >= MIN_MEDIUM_BLOCKED_ROUTE_SOURCE_PAGES
            and len(medium_domains) >= MIN_MEDIUM_BLOCKED_ROUTE_DOMAINS
        )
        medium_completion_checks = {
            "minimum_five_total_targeted_rounds": (
                len(targeted_rounds) >= MIN_MEDIUM_TOTAL_TARGETED_ROUNDS
            ),
            "minimum_three_explicit_medium_enhancement_rounds": (
                len(enhancement_rounds) >= MIN_MEDIUM_ENHANCEMENT_ROUNDS
            ),
            "minimum_ten_executed_queries_each_enhancement_round": (
                len(enhancement_rounds) >= MIN_MEDIUM_ENHANCEMENT_ROUNDS
                and all(
                    enhancement_queries_by_round[number]
                    >= MIN_MEDIUM_ENHANCEMENT_QUERIES_PER_ROUND
                    for number in enhancement_rounds
                )
            ),
            "minimum_six_target_source_categories": (
                len(enhancement_target_categories) >= MIN_MEDIUM_TARGET_SOURCE_CATEGORIES
            ),
            "positive_and_negative_routes_executed": (
                {"positive", "negative"}.issubset(enhancement_polarities)
            ),
            "minimum_three_subject_groups": (
                len(enhancement_subject_groups) >= MIN_MEDIUM_SUBJECT_GROUPS
            ),
            "minimum_three_time_contexts": (
                len(enhancement_time_contexts) >= MIN_MEDIUM_TIME_CONTEXTS
            ),
            "final_enhancement_round_exhausted_action": (
                "exhausted" in enhancement_actions_by_round.get(final_enhancement_round, set())
            ),
            "cross_ledger_three_round_low_yield_route": medium_low_yield_trace_met,
            "cross_ledger_blocked_route": medium_blocked_route_trace_met,
        }
        medium_completion_audit_passed = all(
            value
            for key, value in medium_completion_checks.items()
            if key not in {"cross_ledger_three_round_low_yield_route", "cross_ledger_blocked_route"}
        ) and (
            medium_completion_checks["cross_ledger_three_round_low_yield_route"]
            or medium_completion_checks["cross_ledger_blocked_route"]
        )

        evidence_count = int(formal_summary["scored_evidence_units"])
        eligible_count = int(formal_summary["eligible_evidence_units"])
        retrieved_count = int(formal_summary["retrieved_evidence_units"])
        topic_coverage_count = int(formal_summary["topic_coverage_evidence_units"])
        source_count = int(formal_summary["distinct_source_pages"])
        category_count = int(formal_summary["source_category_count"])
        gaps: list[str] = []
        if evidence_count < minimum_evidence_units:
            gaps.append(f"实际计分证据缺口{minimum_evidence_units - evidence_count}")
        if source_count < minimum_source_pages:
            gaps.append(f"独立来源页缺口{minimum_source_pages - source_count}")
        if category_count < minimum_source_categories:
            gaps.append(f"来源类型缺口{minimum_source_categories - category_count}")
        enhancement_gaps: list[str] = []
        if evidence_count < MEDIUM_HIGH_EVIDENCE_UNITS:
            enhancement_gaps.append(f"中高目标实际计分证据缺口{MEDIUM_HIGH_EVIDENCE_UNITS - evidence_count}")
        if source_count < MEDIUM_HIGH_SOURCE_PAGES:
            enhancement_gaps.append(f"中高目标来源页缺口{MEDIUM_HIGH_SOURCE_PAGES - source_count}")
        if category_count < MEDIUM_HIGH_SOURCE_CATEGORIES:
            enhancement_gaps.append(f"中高目标来源类型缺口{MEDIUM_HIGH_SOURCE_CATEGORIES - category_count}")

        protocol_confidence = str(formal_summary["scoring_confidence"])
        if protocol_confidence in {"高", "中高", "中"}:
            confidence = protocol_confidence
        elif exhaustion_supported:
            confidence = "低" if evidence_count else "数据不足"
        else:
            confidence = "待深检"

        target_reached = (
            CONFIDENCE_ORDER.get(confidence, 0)
            >= CONFIDENCE_ORDER[target_confidence]
        )
        if target_reached and target_confidence == "中":
            status = "sufficient_at_requested_target"
        elif target_reached:
            status = "sufficient"
        elif confidence == "中" and medium_completion_audit_passed:
            status = "sufficient_at_medium_after_audit"
        elif confidence == "中":
            status = "needs_iteration"
        elif exhaustion_supported:
            status = "exhausted_with_shortfall"
        else:
            status = "needs_iteration"

        dimension_result: dict[str, object] = {
            "status": status,
            "evaluation_permitted": status in FINAL_DIMENSION_STATUSES,
            "numeric_score_permitted": status in {
                "sufficient", "sufficient_at_requested_target", "sufficient_at_medium_after_audit"
            },
            # Compatibility alias: this is always actual formal scored evidence.
            "evidence_units": evidence_count,
            "retrieved_evidence_units": retrieved_count,
            "topic_coverage_evidence_units": topic_coverage_count,
            "eligible_evidence_units": eligible_count,
            "scored_evidence_units": evidence_count,
            "minimum_evidence_units": minimum_evidence_units,
            "distinct_source_pages": source_count,
            "minimum_source_pages": minimum_source_pages,
            "source_categories": sorted(categories),
            "source_category_count": category_count,
            "minimum_source_categories": minimum_source_categories,
            "platforms": sorted(platforms),
            "candidate_platforms": list(formal_summary["candidate_platforms"]),
            "dimension_candidate_platform_count": int(
                formal_summary["dimension_candidate_platform_count"]
            ),
            "scorable_platforms": list(formal_summary["scorable_platforms"]),
            "dimension_scorable_platform_count": int(
                formal_summary["dimension_scorable_platform_count"]
            ),
            "sentiment_counts": dict(sorted(sentiments.items())),
            "scoring_positive_units": int(formal_summary["scoring_positive_units"]),
            "scoring_neutral_units": int(formal_summary["scoring_neutral_units"]),
            "scoring_negative_units": int(formal_summary["scoring_negative_units"]),
            "user_group_counts": dict(sorted(user_groups.items())),
            "time_context_counts": dict(sorted(time_contexts.items())),
            "gaps": gaps,
            "enhancement_gaps": enhancement_gaps,
            "targeted_deep_rounds": targeted_rounds,
            "targeted_query_counts": {
                str(number): queries_by_round[number] for number in targeted_rounds
            },
            "new_evidence_by_targeted_round": {
                str(number): new_evidence_by_round[number] for number in targeted_rounds
            },
            "query_depth_met": query_depth_met,
            "targeted_source_categories": sorted(targeted_source_categories),
            "targeted_search_tools": sorted(targeted_search_tools),
            "traced_targeted_source_pages": len(traced_source_ids),
            "traced_targeted_domains": sorted(traced_domains),
            "traced_targeted_source_categories": sorted(traced_categories),
            "exhaustion_checks": exhaustion_checks,
            "independent_exhaustion_audit_passed": exhaustion_supported,
            "exhaustion_supported": exhaustion_supported,
            "exhaustion_basis": (
                "latest_two_rounds_low_yield" if low_yield_trace_met else
                "final_round_blocked_or_no_results" if blocked_route_trace_met else ""
            ),
            "medium_enhancement_rounds": enhancement_rounds,
            "medium_enhancement_query_counts": {
                str(number): enhancement_queries_by_round[number]
                for number in enhancement_rounds
            },
            "medium_enhancement_subject_groups": sorted(enhancement_subject_groups),
            "medium_enhancement_time_contexts": sorted(enhancement_time_contexts),
            "medium_enhancement_polarities": sorted(enhancement_polarities),
            "medium_enhancement_target_source_categories": sorted(enhancement_target_categories),
            "medium_enhancement_traced_source_pages": len(medium_source_ids),
            "medium_enhancement_traced_domains": sorted(medium_domains),
            "medium_enhancement_traced_source_categories": sorted(medium_categories),
            "medium_completion_checks": medium_completion_checks,
            "medium_completion_audit_passed": medium_completion_audit_passed,
            "medium_completion_basis": (
                "three_enhancement_rounds_low_yield" if medium_low_yield_trace_met else
                "final_enhancement_round_blocked_or_no_results"
                if medium_blocked_route_trace_met else ""
            ),
            "confidence": confidence,
            # Keep research disclosure confidence separate from the released
            # formal-scoring confidence.  A machine-proven exhausted route may
            # support a low-confidence qualitative finding, but it does not
            # turn a below-threshold dimension into a formally scorable one.
            "scoring_confidence": protocol_confidence,
            "target_confidence": target_confidence,
            "target_reached": target_reached,
            "formal_dimension_assignment": "primary_dimension",
            "dimension_tags_role": "theme_coverage_only_not_formal_scoring",
        }
        if status == "exhausted_with_shortfall":
            dimension_result["exhaustion_machine_binding"] = {
                "schema_version": "dimension-exhaustion-binding-1",
                "sha256": dimension_exhaustion_signature(
                    dimension,
                    dimension_result,
                    search_metrics,
                ),
            }
        if status == "sufficient_at_medium_after_audit":
            dimension_result["medium_terminal_machine_binding"] = {
                "schema_version": "dimension-medium-terminal-binding-1",
                "sha256": dimension_medium_terminal_signature(
                    dimension,
                    dimension_result,
                    search_metrics,
                ),
            }
        results[dimension] = dimension_result

    needs_iteration = [
        name for name in DIMENSION_NAMES if results[name]["status"] == "needs_iteration"
    ]
    evidence_gap_targets = [
        name for name in needs_iteration if results[name]["confidence"] == "待深检"
    ]
    enhancement_targets = [
        name for name in needs_iteration
        if results[name]["confidence"] in {"中", "中高"}
    ]
    exhausted_shortfall = [
        name for name in DIMENSION_NAMES
        if results[name]["status"] == "exhausted_with_shortfall"
    ]
    high_or_medium_high = [
        name for name in DIMENSION_NAMES if results[name]["status"] == "sufficient"
    ]
    requested_target = [
        name for name in DIMENSION_NAMES
        if results[name]["status"] == "sufficient_at_requested_target"
    ]
    medium_terminal = [
        name for name in DIMENSION_NAMES
        if results[name]["status"] == "sufficient_at_medium_after_audit"
    ]
    finalized = high_or_medium_high + requested_target + medium_terminal
    return {
        "framework": "7+1",
        "result_layer": RESULT_LAYER_NAME,
        "target_confidence": target_confidence,
        "thresholds": {
            "minimum_evidence_units": minimum_evidence_units,
            "minimum_source_pages": minimum_source_pages,
            "minimum_source_categories": minimum_source_categories,
            "medium_high_evidence_units": MEDIUM_HIGH_EVIDENCE_UNITS,
            "medium_high_source_pages": MEDIUM_HIGH_SOURCE_PAGES,
            "medium_high_source_categories": MEDIUM_HIGH_SOURCE_CATEGORIES,
            "high_evidence_units": HIGH_EVIDENCE_UNITS,
            "high_source_pages": HIGH_SOURCE_PAGES,
            "high_source_categories": HIGH_SOURCE_CATEGORIES,
            "minimum_deep_rounds": minimum_deep_rounds,
            "minimum_queries_per_round": minimum_queries_per_round,
            "low_yield_new_evidence_limit": DIMENSION_LOW_YIELD_LIMIT,
            "minimum_target_source_categories_for_exhaustion": MIN_DIMENSION_TARGET_SOURCE_CATEGORIES,
            "minimum_medium_total_targeted_rounds": MIN_MEDIUM_TOTAL_TARGETED_ROUNDS,
            "minimum_medium_enhancement_rounds": MIN_MEDIUM_ENHANCEMENT_ROUNDS,
            "minimum_medium_enhancement_queries_per_round": MIN_MEDIUM_ENHANCEMENT_QUERIES_PER_ROUND,
            "minimum_medium_target_source_categories": MIN_MEDIUM_TARGET_SOURCE_CATEGORIES,
            "minimum_medium_subject_groups": MIN_MEDIUM_SUBJECT_GROUPS,
            "minimum_medium_time_contexts": MIN_MEDIUM_TIME_CONTEXTS,
        },
        "formal_scoring_schema_version": scoring_chain["schema_version"],
        "dimension_assignment_field": "primary_dimension",
        "topic_coverage_field": "dimension_tags",
        "dimensions": results,
        "sufficient_dimensions": finalized,
        "high_or_medium_high_dimensions": high_or_medium_high,
        "requested_target_dimensions": requested_target,
        "medium_terminal_dimensions": medium_terminal,
        "needs_iteration_dimensions": needs_iteration,
        "evidence_gap_target_dimensions": evidence_gap_targets,
        "exhausted_shortfall_dimensions": exhausted_shortfall,
        "enhancement_target_dimensions": enhancement_targets,
        "dimension_iteration_targets": needs_iteration,
        "all_dimensions_finalized": not needs_iteration,
        "all_dimensions_at_or_above_medium": not evidence_gap_targets and not exhausted_shortfall,
        "all_dimensions_preferred_target_met": all(
            bool(results[name].get("target_reached")) for name in DIMENSION_NAMES
        ),
    }


def extract_dimension_audit(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("dimension_evidence"), dict):
        return summary["dimension_evidence"]
    if isinstance(payload.get("dimension_evidence"), dict):
        return payload["dimension_evidence"]
    if isinstance(payload.get("dimensions"), dict):
        return payload
    raise ValueError("dimension evidence audit is missing")


def apply_retrieval_terminal_to_dimension_audit(
    audit: dict[str, object],
    termination: dict[str, object],
) -> dict[str, object]:
    """Finalize unfinished dimensions after a machine-proven retrieval stop.

    This does not declare evidence sufficient.  It records that the fixed
    budgets/routes no longer permit more retrieval, freezes the exact gaps,
    and only preserves a numeric result where the released minimum formal
    scoring threshold was independently met.
    """

    if termination.get("terminal") is not True or termination.get("target_met") is True:
        return copy.deepcopy(audit)
    if termination.get("restricted_delivery_allowed") is not True:
        return copy.deepcopy(audit)
    output = copy.deepcopy(audit)
    dimensions = output.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("dimension evidence audit is invalid")
    finalized_shortfall: list[str] = []
    per_dimension = termination.get("per_dimension_termination", {})
    remaining_gaps = termination.get("remaining_gaps", {})
    for name in DIMENSION_NAMES:
        item = dimensions.get(name)
        if not isinstance(item, dict) or item.get("status") != "needs_iteration":
            continue
        confidence = str(item.get("confidence", "数据不足"))
        formal_minimum_met = (
            confidence in {"中", "中高", "高"}
            and int(item.get("scored_evidence_units", 0) or 0) >= MIN_DIMENSION_EVIDENCE_UNITS
            and int(item.get("distinct_source_pages", 0) or 0) >= MIN_DIMENSION_SOURCE_PAGES
            and int(item.get("source_category_count", 0) or 0) >= MIN_DIMENSION_SOURCE_CATEGORIES
            and int(item.get("dimension_scorable_platform_count", 0) or 0) >= 1
        )
        item["status"] = AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
        item["evaluation_permitted"] = True
        item["numeric_score_permitted"] = formal_minimum_met
        item["retrieval_termination_status"] = str(termination.get("termination_status", ""))
        item["retrieval_termination_scope"] = (
            per_dimension.get(name, {}) if isinstance(per_dimension, dict) else {}
        )
        item["remaining_scored_evidence_gap"] = int(
            remaining_gaps.get(name, 0) if isinstance(remaining_gaps, dict) else 0
        )
        finalized_shortfall.append(name)
    output["retrieval_termination"] = copy.deepcopy(termination)
    output["retrieval_terminated_shortfall_dimensions"] = finalized_shortfall
    output["needs_iteration_dimensions"] = []
    output["dimension_iteration_targets"] = []
    output["evidence_gap_target_dimensions"] = []
    output["enhancement_target_dimensions"] = []
    output["all_dimensions_finalized"] = True
    output["all_dimensions_preferred_target_met"] = False
    output["all_dimensions_at_or_above_medium"] = all(
        str(dimensions[name].get("confidence", "数据不足")) in {"中", "中高", "高"}
        for name in DIMENSION_NAMES
    )
    return output


def validate_final_dimension_audit(payload: dict[str, object]) -> dict[str, object]:
    audit = extract_dimension_audit(payload)
    dimensions = audit.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSION_NAMES):
        raise ValueError("dimension evidence audit must contain exactly the seven canonical dimensions")
    pending: list[str] = []
    restricted_context = audit.get("retrieval_termination")
    for name in DIMENSION_NAMES:
        item = dimensions.get(name)
        if not isinstance(item, dict):
            raise ValueError(f"dimension evidence audit item is invalid: {name}")
        status = str(item.get("status", ""))
        if status not in FINAL_DIMENSION_STATUSES | {"needs_iteration"}:
            raise ValueError(f"dimension evidence audit status is invalid: {name}/{status}")
        if status == "needs_iteration":
            pending.append(name)
        evidence_units = int(item.get("scored_evidence_units", item.get("evidence_units", 0)))
        source_pages = int(item.get("distinct_source_pages", 0))
        source_categories = int(item.get("source_category_count", 0))
        if status in {
            "sufficient",
            "sufficient_at_requested_target",
            "sufficient_at_medium_after_audit",
        }:
            if evidence_units < MIN_DIMENSION_EVIDENCE_UNITS:
                raise ValueError(f"dimension marked sufficient below 25 evidence units: {name}")
            if source_pages < MIN_DIMENSION_SOURCE_PAGES:
                raise ValueError(f"dimension marked sufficient below source-page threshold: {name}")
            if source_categories < MIN_DIMENSION_SOURCE_CATEGORIES:
                raise ValueError(f"dimension marked sufficient below source-category threshold: {name}")
            if int(item.get("dimension_scorable_platform_count", 0)) < 1:
                raise ValueError(f"dimension marked sufficient without a scorable platform: {name}")
            if item.get("numeric_score_permitted") is not True:
                raise ValueError(f"dimension marked sufficient but numeric scoring is not permitted: {name}")
        if status == "sufficient":
            target = str(item.get("target_confidence", audit.get("target_confidence", "中高")))
            if CONFIDENCE_ORDER.get(str(item.get("confidence", "数据不足")), 0) < CONFIDENCE_ORDER.get(target, 3):
                raise ValueError(f"dimension completion is below requested confidence: {name}")
        if status == "sufficient_at_requested_target":
            if str(item.get("target_confidence", audit.get("target_confidence", ""))) != "中":
                raise ValueError(f"requested-target completion is only valid for an explicit medium target: {name}")
            if str(item.get("confidence", "")) not in {"中", "中高", "高"}:
                raise ValueError(f"requested-target completion lacks medium confidence: {name}")
        if status == "sufficient_at_medium_after_audit":
            if str(item.get("confidence", "")) != "中":
                raise ValueError(f"medium terminal status requires medium confidence: {name}")
            if (
                item.get("medium_completion_audit_passed") is not True
                or not str(item.get("medium_completion_basis", ""))
            ):
                raise ValueError(f"medium-confidence completion lacks strong independent audit: {name}")
            # The complete ledger-bound validation below recomputes the
            # signature with real search metrics.  At this structural layer,
            # require the released binding shape without trusting its value.
            binding = item.get("medium_terminal_machine_binding")
            if not isinstance(binding, dict) or binding.get("schema_version") != "dimension-medium-terminal-binding-1":
                raise ValueError(f"medium-confidence completion lacks a machine binding: {name}")
        if status == "exhausted_with_shortfall":
            if item.get("numeric_score_permitted") is not False:
                raise ValueError(f"exhausted dimension must not permit a formal numeric score: {name}")
            rounds = item.get("targeted_deep_rounds", [])
            if not isinstance(rounds, list) or len(rounds) < MIN_DIMENSION_DEEP_ROUNDS:
                raise ValueError(f"dimension shortfall lacks four targeted deep-search rounds: {name}")
            if (
                item.get("exhaustion_supported") is not True
                or item.get("independent_exhaustion_audit_passed") is not True
                or not str(item.get("exhaustion_basis", ""))
            ):
                raise ValueError(f"dimension shortfall lacks supported exhaustion evidence: {name}")
        if status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL:
            if not str(item.get("retrieval_termination_status", "")):
                raise ValueError(f"retrieval-terminal dimension lacks a stop reason: {name}")
            if item.get("evaluation_permitted") is not True:
                raise ValueError(f"retrieval-terminal dimension must permit restricted disclosure: {name}")
            if item.get("numeric_score_permitted") is True:
                if evidence_units < MIN_DIMENSION_EVIDENCE_UNITS:
                    raise ValueError(f"retrieval-terminal numeric dimension is below 25 units: {name}")
                if source_pages < MIN_DIMENSION_SOURCE_PAGES:
                    raise ValueError(f"retrieval-terminal numeric dimension is below page threshold: {name}")
                if source_categories < MIN_DIMENSION_SOURCE_CATEGORIES:
                    raise ValueError(f"retrieval-terminal numeric dimension is below category threshold: {name}")
                if int(item.get("dimension_scorable_platform_count", 0)) < 1:
                    raise ValueError(f"retrieval-terminal numeric dimension lacks a scorable platform: {name}")
        lifecycle = formal_dimension_lifecycle(
            item,
            requested_target_met=bool(item.get("target_reached")),
            medium_terminal_verified=bool(
                item.get("medium_completion_audit_passed")
                and isinstance(item.get("medium_terminal_machine_binding"), dict)
            ),
            exhaustion_verified=bool(
                item.get("exhaustion_supported")
                and item.get("independent_exhaustion_audit_passed")
            ),
            restricted_terminal_verified=bool(
                status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
                and isinstance(restricted_context, dict)
                and restricted_context.get("terminal") is True
                and restricted_context.get("restricted_delivery_allowed") is True
                and str(item.get("retrieval_termination_status", ""))
                == str(restricted_context.get("termination_status", ""))
            ),
        )
        if status in FINAL_DIMENSION_STATUSES and lifecycle["terminal"] is not True:
            raise ValueError(f"dimension final status lacks its authoritative terminal proof: {name}")
    if pending:
        raise ValueError(
            "dimension search remains unfinished; continue gap or medium-enhancement search: "
            + ", ".join(pending)
        )
    return audit


def bind_machine_dimension_audit(
    audit: dict[str, object],
    *,
    task_run_id: str,
    evidence: list[dict[str, str]],
    sources: list[dict[str, str]],
    search_rows: list[dict[str, str]],
    execution_schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Bind a computed dimension audit to the exact three input ledgers."""
    assert_single_task_run(
        task_run_id,
        *([("证据台账", evidence)] if evidence else []),
        ("来源台账", sources),
        ("检索日志", search_rows),
    )
    target_confidence = str(audit.get("target_confidence", ""))
    if target_confidence not in {"中", "中高", "高"}:
        raise ValueError("dimension evidence audit target confidence is invalid")
    search_metrics = executed_query_metrics(
        search_rows,
        schema_context=execution_schema_context,
    )
    controls = load_retrieval_config()
    bound = copy.deepcopy(audit)
    normalized_place_identity = {
        "source_place_names": sorted(
            {
                " ".join(str(row.get("place_name", "")).split()).casefold()
                for row in sources
                if str(row.get("place_name", "")).strip()
            }
        ),
        "search_place_identities": sorted(
            {
                " ".join(
                    str(row.get("place_identity") or row.get("place") or "").split()
                ).casefold()
                for row in search_rows
                if str(row.get("place_identity") or row.get("place") or "").strip()
            }
        ),
    }
    raw_execution_context = search_metrics["execution_schema_context"]
    bound_execution_context = (
        copy.deepcopy(raw_execution_context)
        if isinstance(raw_execution_context, Mapping)
        and str(raw_execution_context.get("context_binding_sha256", ""))
        else {}
    )
    provenance = {
        "origin": "dimension_evidence.evaluate_dimension_evidence",
        "task_run_id": task_run_id,
        "place_identity_sha256": canonical_sha256(normalized_place_identity),
        "evidence_records_sha256": records_sha256(evidence),
        "source_records_sha256": records_sha256(sources),
        "search_records_sha256": execution_set_hash(search_rows),
        "target_confidence": target_confidence,
        "valid_execution_set_sha256": search_metrics["valid_execution_set_sha256"],
        "invalid_execution_set_sha256": search_metrics["invalid_execution_set_sha256"],
        "execution_schema_version": search_metrics["execution_schema_version"],
        "execution_schema_context": bound_execution_context,
        "retrieval_control_sha256": canonical_sha256(controls),
    }
    provenance["audit_transaction_id"] = canonical_sha256(provenance)
    bound["audit_provenance"] = provenance
    return bound


def validate_machine_dimension_audit(
    payload: dict[str, object],
    *,
    task_run_id: str,
    evidence: list[dict[str, str]],
    sources: list[dict[str, str]],
    search_rows: list[dict[str, str]],
    execution_schema_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Recompute the audit from ledgers; never trust hand-written terminal fields."""
    audit = validate_final_dimension_audit(payload)
    provenance = audit.get("audit_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("dimension evidence audit lacks machine provenance")
    assert_single_task_run(
        task_run_id,
        *([("证据台账", evidence)] if evidence else []),
        ("来源台账", sources),
        ("检索日志", search_rows),
    )
    target_confidence = str(audit.get("target_confidence", ""))
    if target_confidence not in {"中", "中高", "高"}:
        raise ValueError("dimension evidence audit target confidence is invalid")
    stored_execution_context = provenance.get("execution_schema_context")
    if execution_schema_context is not None:
        if dict(execution_schema_context) != dict(stored_execution_context or {}):
            raise ValueError("dimension audit execution context differs from current state")
        stored_execution_context = execution_schema_context
    if stored_execution_context is not None and not isinstance(
        stored_execution_context, Mapping
    ):
        raise ValueError("dimension evidence audit execution schema context is invalid")
    search_metrics = executed_query_metrics(
        search_rows,
        schema_context=stored_execution_context or None,
    )
    controls = load_retrieval_config()
    normalized_place_identity = {
        "source_place_names": sorted(
            {
                " ".join(str(row.get("place_name", "")).split()).casefold()
                for row in sources
                if str(row.get("place_name", "")).strip()
            }
        ),
        "search_place_identities": sorted(
            {
                " ".join(
                    str(row.get("place_identity") or row.get("place") or "").split()
                ).casefold()
                for row in search_rows
                if str(row.get("place_identity") or row.get("place") or "").strip()
            }
        ),
    }
    raw_execution_context = search_metrics["execution_schema_context"]
    bound_execution_context = (
        copy.deepcopy(raw_execution_context)
        if isinstance(raw_execution_context, Mapping)
        and str(raw_execution_context.get("context_binding_sha256", ""))
        else {}
    )
    expected_provenance = {
        "origin": "dimension_evidence.evaluate_dimension_evidence",
        "task_run_id": task_run_id,
        "place_identity_sha256": canonical_sha256(normalized_place_identity),
        "evidence_records_sha256": records_sha256(evidence),
        "source_records_sha256": records_sha256(sources),
        "search_records_sha256": execution_set_hash(search_rows),
        "target_confidence": target_confidence,
        "valid_execution_set_sha256": search_metrics["valid_execution_set_sha256"],
        "invalid_execution_set_sha256": search_metrics["invalid_execution_set_sha256"],
        "execution_schema_version": search_metrics["execution_schema_version"],
        "execution_schema_context": bound_execution_context,
        "retrieval_control_sha256": canonical_sha256(controls),
    }
    expected_provenance["audit_transaction_id"] = canonical_sha256(
        expected_provenance
    )
    if provenance != expected_provenance:
        raise ValueError("dimension evidence audit provenance does not match the current ledgers")
    recomputed = evaluate_dimension_evidence(
        evidence,
        sources,
        search_rows,
        target_confidence=target_confidence,
        execution_schema_context=stored_execution_context or None,
    )
    terminal_context = audit.get("retrieval_termination")
    if isinstance(terminal_context, dict):
        if terminal_context.get("budgets") != controls.get("budgets"):
            raise ValueError("dimension retrieval-terminal context uses stale budgets")
        terminal_metrics = terminal_context.get("metrics")
        terminal_schema_context = (
            terminal_metrics.get("execution_schema_context")
            if isinstance(terminal_metrics, dict)
            else None
        )
        if terminal_schema_context is not None and not isinstance(terminal_schema_context, dict):
            raise ValueError("dimension retrieval-terminal execution schema context is invalid")
        independently_computed = retrieval_termination(
            search_rows,
            recomputed["dimensions"],
            target_confidence=str(recomputed.get("target_confidence", "中高")),
            config=controls,
            protocol=_PROTOCOL,
            schema_context=stored_execution_context,
        )
        if json.dumps(terminal_context, ensure_ascii=False, sort_keys=True) != json.dumps(
            independently_computed, ensure_ascii=False, sort_keys=True
        ):
            raise ValueError("dimension retrieval-terminal context is stale or hand-written")
        recomputed = apply_retrieval_terminal_to_dimension_audit(
            recomputed,
            independently_computed,
        )
    supplied = copy.deepcopy(audit)
    supplied.pop("audit_provenance", None)
    if json.dumps(supplied, ensure_ascii=False, sort_keys=True) != json.dumps(
        recomputed, ensure_ascii=False, sort_keys=True
    ):
        raise ValueError("dimension evidence audit contains hand-written or stale terminal results")
    result = copy.deepcopy(audit)
    result["audit_provenance"]["execution_schema_context"] = stored_execution_context
    return result
