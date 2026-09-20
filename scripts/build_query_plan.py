#!/usr/bin/env python3
"""Build a reproducible Chinese web-search query matrix for one historic place."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import strict_json as json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from dimension_evidence import DIMENSION_NAMES
from formal_states import AUDIT_NEEDS_ITERATION, AUDIT_TERMINAL_STATUSES
from process_lock import ProcessFileLock
from retrieval_controls import (
    budget_values,
    current_round_dimension_quota,
    deep_search_budget_feasibility,
    dimension_round_schedule,
    executed_query_metrics,
    execution_schema_context_from_state,
    load_retrieval_config,
    normalized_query_intent,
    research_target_summary,
    validate_query_budget_compliance,
    validate_query_dimension_targets,
    validate_round_metrics,
)
from runtime_guard import canonical_sha256, records_sha256, sha256_file


BASE_MODIFIERS = [
    ("", "neutral", "对象发现与消歧"),
    ("评价", "neutral", "普通评价"),
    ("评论", "neutral", "普通评论"),
    ("真实体验", "neutral", "实际到访体验"),
    ("值得去吗", "positive", "推荐意愿"),
    ("推荐", "positive", "正面与推荐"),
    ("避雷", "negative", "负面检索"),
    ("缺点", "negative", "负面问题"),
    ("历史故事 文化导览", "neutral", "历史文化感知"),
    ("有遗产但看不懂", "negative", "历史文化感知负向"),
    ("地方特色 本土文化", "neutral", "在地文化特征"),
    ("老字号 地方语言 饮食习俗", "neutral", "在地文化特征"),
    ("千街一面 连锁化 网红化", "negative", "在地文化特征负向"),
    ("原住民 街坊 日常生活", "neutral", "生活文化延续"),
    ("传统业态 老店 持续经营", "neutral", "生活文化延续"),
    ("居民流失 生活空间旅游化", "negative", "生活文化延续负向"),
    ("非遗 技艺 参与 体验", "neutral", "文化实践体验"),
    ("节庆 民俗 居民参与", "neutral", "文化实践体验"),
    ("文化活动 表演化 形式化", "negative", "文化实践体验负向"),
    ("历史氛围 地方感 场所感", "neutral", "场所氛围体验"),
    ("烟火气 市井气息 夜间氛围", "neutral", "场所氛围体验"),
    ("广告 噪声 破坏文化氛围", "negative", "场所氛围体验负向"),
    ("保护 修缮 修旧如旧", "neutral", "保护活化感知"),
    ("适应性利用 老建筑继续使用", "neutral", "保护活化感知"),
    ("过度翻新 仿古化 空心化", "negative", "保护活化感知负向"),
    ("拥挤 承载 旅游过载", "negative", "承载治理体验"),
    ("客流分流 秩序 安全 环境治理", "neutral", "承载治理体验"),
    ("游客居民冲突 噪声 垃圾", "negative", "承载治理体验负向"),
    ("居民 商户 游客 传承人", "neutral", "主体差异"),
    ("工作日 周末 节假日 白天 夜间", "neutral", "时间与场景差异"),
    ("{local_term}", "neutral", "地方文化关键词"),
    ("改造前后", "neutral", "时间变化"),
]

BUILDING_MODIFIERS = [
    ("", "neutral", "代表节点"),
    ("评价", "neutral", "节点评价"),
    ("真实体验", "neutral", "节点体验"),
    ("值得去吗", "positive", "节点推荐"),
    ("避雷", "negative", "节点负面"),
    ("历史", "neutral", "节点历史"),
    ("建筑", "neutral", "节点建筑"),
    ("文化", "neutral", "节点文化"),
    ("保护", "neutral", "节点保护"),
    ("缺点", "negative", "节点问题"),
]

SOURCE_TARGETS = [
    ("政府 官网", "official", "官方身份、范围与开放信息"),
    ("政府 开放数据", "government_open_data", "政府公开数据"),
    ("文物 保护名录", "heritage_registry", "遗产或保护名录"),
    ("博物馆 场馆", "museum_venue", "场馆与文化机构"),
    ("新闻 报道", "news", "新闻事件与社会关注"),
    ("规划 建筑 保护", "professional", "规划、建筑与保护专业资料"),
    ("论文 研究", "academic", "学术研究"),
    ("百科 历史", "encyclopedic", "百科与交叉核验"),
    ("游客 评价", "user_review", "公开用户评价，不限定大众点评或小红书"),
    ("地图 评价", "map_review", "地图平台公开体验"),
    ("游记 攻略", "travel_ugc", "旅游平台与旅行社区"),
    ("微博 抖音 哔哩哔哩 知乎", "social_content", "公开社交和内容平台"),
    ("博客 公众号 游记", "blog_travel", "博客与长篇体验"),
    ("论坛 讨论", "local_forum", "地方论坛和公共讨论"),
    ("居民 社区 生活", "community_content", "居民和社区生活"),
    ("地址 开放时间 电话", "business_directory", "目录与基础信息交叉核验"),
]

DEFAULT_DEEP_GAPS = [
    *DIMENSION_NAMES,
    "真实正面评价",
    "真实负面评价",
    "本地居民 外地游客差异",
]

DEEP_ROUND_TEMPLATES = {
    1: [
        ("{gap} 当前 真实体验", "neutral", "user_review"),
        ("{gap} 正面 推荐 优点", "positive", "map_review"),
        ("{gap} 负面 缺点 问题", "negative", "travel_ugc"),
        ("{gap} 本地居民 街坊", "neutral", "community_content"),
        ("{gap} 商户 老字号 经营者", "neutral", "local_forum"),
        ("{gap} 游客 参与体验", "neutral", "social_content"),
        ("{gap} 传承人 文化实践者", "neutral", "blog_travel"),
        ("{gap} 管理者 治理回应", "neutral", "news"),
        ("{gap} 工作日 白天", "neutral", "map_review"),
        ("{gap} 周末 夜间", "neutral", "social_content"),
        ("{gap} 节假日 高峰", "negative", "user_review"),
        ("{gap} 专业人士 调研", "neutral", "professional"),
    ],
    2: [
        ("{gap} 近期 正面 认可", "positive", "user_review"),
        ("{gap} 近期 负面 避雷", "negative", "map_review"),
        ("{gap} 历史回忆 以前 过去", "neutral", "local_forum"),
        ("{gap} 改造前后 更新前后", "neutral", "blog_travel"),
        ("{gap} 长期变化 多年变化", "neutral", "travel_ugc"),
        ("{gap} 本地居民 长期体验", "neutral", "community_content"),
        ("{gap} 商户 经营变化", "neutral", "news"),
        ("{gap} 游客 节庆活动期间", "neutral", "social_content"),
        ("{gap} 传承人 技艺实践", "neutral", "museum_venue"),
        ("{gap} 工作日 周末 对比", "neutral", "map_review"),
        ("{gap} 白天 夜间 对比", "neutral", "user_review"),
        ("{gap} 专家 学者 研究", "neutral", "academic"),
    ],
    3: [
        ("{gap} 公众 正面 喜欢 值得", "positive", "user_review"),
        ("{gap} 公众 负面 投诉 风险", "negative", "news"),
        ("{gap} 本地居民 游客 差异", "neutral", "community_content"),
        ("{gap} 商户 管理者 差异", "neutral", "local_forum"),
        ("{gap} 传承人 文化实践者 观点", "neutral", "museum_venue"),
        ("{gap} 专业人士 事实核验", "neutral", "professional"),
        ("{gap} 当前状态 近期", "neutral", "social_content"),
        ("{gap} 改造前后比较", "neutral", "blog_travel"),
        ("{gap} 长期变化 长期问题", "negative", "travel_ugc"),
        ("{gap} 工作日 节假日", "neutral", "map_review"),
        ("{gap} 白天 夜间", "neutral", "user_review"),
        ("{gap} 节庆活动期间 高峰", "neutral", "social_content"),
    ],
    4: [
        ("{gap} 本地居民 真实看法 日常使用", "neutral", "community_content"),
        ("{gap} 商户 老店 经营变化", "neutral", "local_forum"),
        ("{gap} 游客 正面 认可 值得", "positive", "user_review"),
        ("{gap} 游客 负面 投诉 风险", "negative", "map_review"),
        ("{gap} 传承人 文化实践者 持续活动", "neutral", "museum_venue"),
        ("{gap} 管理者 治理 回应", "neutral", "news"),
        ("{gap} 专业人士 真实性 评估", "neutral", "academic"),
        ("{gap} 当前状态 工作日 白天", "neutral", "business_directory"),
        ("{gap} 周末 夜间 公众体验", "neutral", "social_content"),
        ("{gap} 节假日 节庆活动期间", "neutral", "travel_ugc"),
        ("{gap} 历史回忆 以前 过去", "neutral", "blog_travel"),
        ("{gap} 改造前后比较 长期变化", "neutral", "professional"),
    ],
    5: [
        ("{gap} 本地居民 正面 喜欢 认可", "positive", "community_content"),
        ("{gap} 本地居民 负面 问题 冲突", "negative", "local_forum"),
        ("{gap} 商户 经营者 长期变化", "neutral", "news"),
        ("{gap} 游客 近期 真实体验", "neutral", "user_review"),
        ("{gap} 传承人 非遗传承 观点", "neutral", "museum_venue"),
        ("{gap} 管理者 管理部门 治理成效", "neutral", "official"),
        ("{gap} 专业人士 专家 学者 复核", "neutral", "academic"),
        ("{gap} 其他公众 市民 网友 讨论", "neutral", "social_content"),
        ("{gap} 工作日 周末 对比", "neutral", "map_review"),
        ("{gap} 白天 夜间 对比", "neutral", "travel_ugc"),
        ("{gap} 改造前后 更新前后", "neutral", "blog_travel"),
        ("{gap} 节假日 节庆 高峰 缺点", "negative", "user_review"),
    ],
}

DIMENSION_QUERY_TERMS = {
    DIMENSION_NAMES[0]: "历史 老建筑 历史故事 文化遗迹",
    DIMENSION_NAMES[1]: "地方特色 老字号 本地记忆 传统习俗",
    DIMENSION_NAMES[2]: "居民日常 传统业态 社区生活 持续经营",
    DIMENSION_NAMES[3]: "展览 节庆 演出 非遗 参与体验",
    DIMENSION_NAMES[4]: "游览氛围 夜间体验 拥挤 商业化",
    DIMENSION_NAMES[5]: "修缮 保护 更新 活化利用",
    DIMENSION_NAMES[6]: "交通 秩序 设施 服务 管理",
}
DIMENSION_CLUSTERS = (
    (DIMENSION_NAMES[0], DIMENSION_NAMES[1]),
    (DIMENSION_NAMES[2], DIMENSION_NAMES[3]),
    (DIMENSION_NAMES[4], DIMENSION_NAMES[5], DIMENSION_NAMES[6]),
)

ITERATION_OUTPUT_FIELDS = [
    "query_id", "iteration_round", "gap_target", "query_dimension_targets",
    "iteration_mode", "dimension_confidence_before_round", "base_name",
    "modifier", "query", "exact_query", "polarity", "purpose", "origin",
    "source_category_target", "target_platform_id", "target_dimension",
    "platform_dimension_gap", "expected_marginal_gain",
    "normalized_query_intent", "query_target_basis",
]
PLAN_BINDING_SCHEMA = "query-plan-binding-1"

# Query scope is planning metadata, never formal evidence attribution. Explicit
# bindings keep broad discovery routes useful without an empty target loophole.
DISCOVERY_PURPOSE_TARGETS = {
    "对象发现与消歧": (0, 5), "普通评价": (4, 6), "普通评论": (4, 6),
    "实际到访体验": (3, 4, 6), "推荐意愿": (3, 4), "正面与推荐": (3, 4),
    "负面检索": (4, 5, 6), "负面问题": (4, 5, 6),
    "主体差异": (1, 2, 3), "时间与场景差异": (2, 3, 4),
    "地方文化关键词": (0, 1), "时间变化": (2, 5),
    "代表节点": (0, 5), "节点评价": (0, 4), "节点体验": (3, 4),
    "节点推荐": (3, 4), "节点负面": (4, 6), "节点历史": (0,),
    "节点建筑": (0, 5), "节点文化": (0, 1), "节点保护": (5,), "节点问题": (5, 6),
    "官方身份、范围与开放信息": (0, 5, 6), "政府公开数据": (2, 5, 6),
    "遗产或保护名录": (0, 5), "场馆与文化机构": (0, 3),
    "新闻事件与社会关注": (2, 5, 6), "规划、建筑与保护专业资料": (0, 5),
    "学术研究": (0, 2, 5), "百科与交叉核验": (0, 1),
    "公开用户评价，不限定大众点评或小红书": (3, 4, 6),
    "地图平台公开体验": (4, 6), "旅游平台与旅行社区": (0, 3, 4),
    "公开社交和内容平台": (1, 3, 4), "博客与长篇体验": (0, 1, 4),
    "地方论坛和公共讨论": (2, 6), "居民和社区生活": (1, 2, 6),
    "目录与基础信息交叉核验": (5, 6),
}


def unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = " ".join(value.split()).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            output.append(cleaned)
    return output


def build_rows(
    place: str,
    aliases: list[str],
    buildings: list[str],
    local_terms: list[str],
) -> list[dict[str, str]]:
    retrieval_config = load_retrieval_config()
    names = unique_nonempty([place, *aliases])
    local_terms = unique_nonempty(local_terms) or ["地方文化"]
    rows: list[dict[str, str]] = []
    seen_queries: set[str] = set()

    def add(
        base: str,
        modifier: str,
        polarity: str,
        purpose: str,
        origin: str,
        source_category_target: str = "mixed",
    ) -> None:
        query = f"{base} {modifier}".strip()
        query = " ".join(query.split())
        if query in seen_queries:
            return
        seen_queries.add(query)
        targets = [name for name in DIMENSION_NAMES if name in purpose]
        if not targets:
            if purpose not in DISCOVERY_PURPOSE_TARGETS:
                raise ValueError("unbound_discovery_query_purpose")
            targets = [DIMENSION_NAMES[i] for i in DISCOVERY_PURPOSE_TARGETS[purpose]]
        row = {
            "place": place,
            "query_id": f"Q{len(rows) + 1:03d}",
                "base_name": base,
                "modifier": modifier,
                "query": query,
                "exact_query": f'"{base}" {modifier}'.strip(),
                "polarity": polarity,
                "purpose": purpose,
                "origin": origin,
                "source_category_target": source_category_target,
                "query_dimension_targets": json.dumps(targets, ensure_ascii=False, separators=(",", ":")),
            }
        validation = validate_query_dimension_targets(row, config=retrieval_config)
        if validation["status"] != "valid":
            raise ValueError(
                "query plan produced an invalid dimension target declaration: "
                + ",".join(validation["error_codes"])
            )
        rows.append(row)

    for name in names:
        for modifier, polarity, purpose in BASE_MODIFIERS:
            if modifier == "{local_term}":
                for term in local_terms:
                    add(name, term, polarity, purpose, "place_or_alias")
            else:
                add(name, modifier, polarity, purpose, "place_or_alias")

    for building in unique_nonempty(buildings):
        base = f"{place} {building}"
        for modifier, polarity, purpose in BUILDING_MODIFIERS:
            add(base, modifier, polarity, purpose, "representative_building")

    for modifier, category, purpose in SOURCE_TARGETS:
        add(place, modifier, "neutral", purpose, "source_diversity", category)

    budget = int(retrieval_config["budgets"]["baseline_query_budget"])
    if len(rows) <= budget:
        return rows

    # Baseline discovery is bounded independently from deep retrieval.  Keep
    # all source-taxonomy routes plus deterministic coverage of every declared
    # alias/building before filling the remaining capacity round-robin.
    mandatory: list[int] = []
    mandatory_seen: set[int] = set()

    def require(index: int) -> None:
        if index not in mandatory_seen:
            mandatory_seen.add(index)
            mandatory.append(index)

    for index, row in enumerate(rows):
        if row["origin"] == "source_diversity":
            require(index)
    represented_groups: set[tuple[str, str]] = set()
    represented_polarities: set[tuple[str, str, str]] = set()
    for index, row in enumerate(rows):
        group = (row["origin"], row["base_name"])
        if group not in represented_groups:
            represented_groups.add(group)
            require(index)
        polarity = (row["origin"], row["base_name"], row["polarity"])
        if row["polarity"] in {"positive", "negative"} and polarity not in represented_polarities:
            represented_polarities.add(polarity)
            require(index)
    if len(mandatory) > budget:
        raise ValueError(
            "baseline_query_budget_infeasible: declared aliases/buildings and required "
            "source/polarity coverage exceed the released baseline capacity"
        )

    selected = list(mandatory)
    selected_set = set(selected)
    groups: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(rows):
        if index in selected_set:
            continue
        groups.setdefault((row["origin"], row["base_name"]), []).append(index)
    group_order = list(groups)
    while len(selected) < budget and group_order:
        next_order: list[tuple[str, str]] = []
        for group in group_order:
            items = groups[group]
            if items and len(selected) < budget:
                index = items.pop(0)
                selected.append(index)
                selected_set.add(index)
            if items:
                next_order.append(group)
        group_order = next_order
    bounded = [rows[index] for index in sorted(selected[:budget])]
    for number, row in enumerate(bounded, start=1):
        row["query_id"] = f"Q{number:03d}"
    return bounded


def build_iteration_rows(
    place: str,
    aliases: list[str],
    buildings: list[str],
    gaps: list[str],
    iteration_round: int,
    enhancement_dimensions: set[str] | None = None,
    retrieval_config: dict[str, object] | None = None,
    platform_gap_matrix: list[dict[str, object]] | None = None,
    prior_query_metrics: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    if iteration_round < 1:
        raise ValueError("iteration_round must be at least 1")
    names = unique_nonempty([place, *aliases])
    controls = retrieval_config or load_retrieval_config()
    budgets = budget_values(controls)
    if iteration_round > budgets["maximum_iteration_rounds"]:
        return []
    gap_terms = unique_nonempty(gaps)
    templates = DEEP_ROUND_TEMPLATES.get(iteration_round, DEEP_ROUND_TEMPLATES[3])
    enhancement_dimensions = enhancement_dimensions or set()
    prior_metrics = prior_query_metrics or {}
    dimension_gaps = [item for item in gap_terms if item in DIMENSION_NAMES]
    # Coverage alone is a diagnostic, not permission to invent a deficient
    # dimension. The caller retains it until a formal audit supplies targets.
    if not dimension_gaps:
        return []
    if dimension_gaps and prior_query_metrics is not None:
        actionable = [d for d in dimension_gaps if int(
            prior_metrics.get('deep_unique_query_intents_by_dimension', {}).get(d, 0) or 0
        ) < budgets['per_dimension_independent_intent_budget']]
        feasibility = deep_search_budget_feasibility(
            dimensions=actionable, iteration_round=iteration_round,
            metrics=prior_metrics, config=controls,
        )
        if not feasibility['planning_feasible']:
            raise ValueError('query_planning_infeasible: ' + json.dumps(
                feasibility['errors'], ensure_ascii=False, sort_keys=True))
    global_remaining = max(
        0,
        budgets["global_independent_intent_budget"]
        - int(prior_metrics.get("deep_unique_query_intent_count", 0) or 0),
    )
    round_budget = min(budgets["per_round_query_budget"], global_remaining)
    dimension_budget = budgets["per_dimension_query_budget"]
    prior_by_dimension = prior_metrics.get("deep_unique_query_intents_by_dimension", {})
    if not isinstance(prior_by_dimension, dict):
        prior_by_dimension = {}
    rows: list[dict[str, str]] = []
    seen_queries: set[str] = set()
    historical_intents = {
        str(value) for value in prior_metrics.get("query_intents", [])
    } if isinstance(prior_metrics.get("query_intents", []), list) else set()
    planned_by_dimension: dict[str, int] = {name: 0 for name in DIMENSION_NAMES}

    schedules = {
        dimension: dimension_round_schedule(
            dimension=dimension,
            iteration_round=iteration_round,
            metrics=prior_metrics,
            config=controls,
            confidence="中" if dimension in enhancement_dimensions else "",
        )
        for dimension in DIMENSION_NAMES
    }

    def mode_for(dimension: str) -> str:
        return str(schedules[dimension]["iteration_mode"])

    quotas = {
        dimension: int(
            current_round_dimension_quota(
                dimension=dimension,
                iteration_round=iteration_round,
                iteration_mode=mode_for(dimension),
                metrics=prior_metrics,
                config=controls,
            )["quota"]
        )
        for dimension in DIMENSION_NAMES
    }

    def available_targets(values: list[str]) -> list[str]:
        return [
            dimension
            for dimension in values
            if dimension in DIMENSION_NAMES
            and int(prior_by_dimension.get(dimension, 0) or 0) < dimension_budget
            and planned_by_dimension[dimension] < quotas[dimension]
        ]

    def public_terms(values: list[str]) -> str:
        return " ".join(DIMENSION_QUERY_TERMS[item] for item in values)

    def add(
        base: str,
        modifier: str,
        gap: str,
        origin: str,
        polarity: str,
        source_category_target: str,
        query_targets: list[str] | None = None,
        iteration_mode: str | None = None,
        target_platform_id: str = "",
        platform_dimension_gap: int | str = "",
        expected_marginal_gain: float | str = "",
    ) -> None:
        query = " ".join(f"{base} {modifier}".split())
        targets = available_targets(
            list(dict.fromkeys(query_targets or ([gap] if gap in DIMENSION_NAMES else [])))
        )
        if not targets:
            return
        candidate = {
            "place": place,
            "query": query,
            "query_dimension_targets": json.dumps(targets, ensure_ascii=False, separators=(",", ":")),
            "source_category_target": source_category_target,
            "polarity": polarity,
            "target_platform_id": target_platform_id,
        }
        target_validation = validate_query_dimension_targets(candidate, config=controls)
        if target_validation["status"] != "valid":
            raise ValueError(
                "iteration query plan produced an invalid dimension target declaration: "
                + ",".join(target_validation["error_codes"])
            )
        intent = normalized_query_intent(candidate)
        if intent in seen_queries or intent in historical_intents or len(rows) >= round_budget:
            return
        seen_queries.add(intent)
        row = {
                "place": place,
                "query_id": f"I{iteration_round}Q{len(rows) + 1:03d}",
                "iteration_round": str(iteration_round),
                "gap_target": gap,
                "query_dimension_targets": json.dumps(targets, ensure_ascii=False, separators=(",", ":")),
                "iteration_mode": iteration_mode or (
                    "medium_enhancement"
                    if targets and all(mode_for(item) == "medium_enhancement" for item in targets)
                    else "evidence_gap_fill"
                ),
                "dimension_confidence_before_round": (
                    "中"
                    if targets and all(mode_for(item) == "medium_enhancement" for item in targets)
                    else "待深检"
                ),
                "base_name": base,
                "modifier": modifier,
                "query": query,
                "exact_query": f'"{base}" {modifier}',
                "polarity": polarity,
                "purpose": (
                    "分析维度证据缺口的定向深度检索"
                    if gap in DIMENSION_NAMES
                    else "实际计分证据缺口的深度迭代检索"
                ),
                "origin": origin,
                "source_category_target": source_category_target,
                "target_platform_id": target_platform_id,
                "target_dimension": targets[0] if len(targets) == 1 else "",
                "platform_dimension_gap": str(platform_dimension_gap),
                "expected_marginal_gain": str(expected_marginal_gain),
                "query_target_basis": json.dumps(
                    {dimension: DIMENSION_QUERY_TERMS[dimension] for dimension in targets},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        row["normalized_query_intent"] = normalized_query_intent(row)
        rows.append(row)
        for dimension in targets:
            planned_by_dimension[dimension] += 1

    dimension_gaps = [item for item in gap_terms if item in DIMENSION_NAMES]
    other_gaps = [item for item in gap_terms if item not in DIMENSION_NAMES]
    target_partitions: list[tuple[list[str], str]] = []
    evidence_targets = [item for item in dimension_gaps if mode_for(item) == "evidence_gap_fill"]
    enhancement_targets = [item for item in dimension_gaps if mode_for(item) == "medium_enhancement"]
    for cluster in DIMENSION_CLUSTERS:
        evidence_cluster = [item for item in cluster if item in evidence_targets]
        enhancement_cluster = [item for item in cluster if item in enhancement_targets]
        if evidence_cluster:
            target_partitions.append((evidence_cluster, "evidence_gap_fill"))
        if enhancement_cluster:
            target_partitions.append((enhancement_cluster, "medium_enhancement"))

    for name in names:
        for targets, mode in target_partitions:
            for template_index, (template, polarity, source_category) in enumerate(templates):
                active = available_targets(targets)
                if not active:
                    break
                combined_gap = ";".join(active)
                add(
                    name,
                    " ".join(filter(None, [
                        template.format(gap=public_terms(active)),
                        " ".join(other_gaps) if template_index == 0 else "",
                    ])),
                    combined_gap,
                    "shared_dimension_gap_search",
                    polarity,
                    source_category,
                    active,
                    mode,
                )
    for building in unique_nonempty(buildings):
        for targets, mode in target_partitions:
            active = available_targets(targets)
            if not active:
                continue
            combined_gap = ";".join(active)
            add(
                f"{place} {building}",
                f"{public_terms(active)} 真实体验",
                combined_gap,
                "deep_node_search",
                "neutral",
                "travel_ugc",
                active,
                mode,
            )

    # Shared dimension queries are emitted first.  Remaining budget is then
    # assigned to the highest-yield platform × dimension cells, favouring a
    # cell with two eligible units over a new singleton cell.  This does not
    # invent evidence; it only makes the intended gap explicit in the plan.
    for cell in platform_gap_matrix or []:
        if not isinstance(cell, dict) or cell.get("status") != "gap":
            continue
        dimension = str(cell.get("dimension", ""))
        platform_id = str(cell.get("platform_id", "")).strip()
        if dimension not in dimension_gaps or not platform_id:
            continue
        gap_value = int(cell.get("gap_to_minimum", 0) or 0)
        if gap_value <= 0:
            continue
        add(
            place,
            f"{platform_id} {DIMENSION_QUERY_TERMS[dimension]} 真实体验 评价",
            dimension,
            "platform_dimension_residual_search",
            "neutral",
            "user_review",
            [dimension],
            mode_for(dimension),
            target_platform_id=platform_id,
            platform_dimension_gap=gap_value,
            expected_marginal_gain=float(cell.get("expected_marginal_gain", 0.0) or 0.0),
        )
    for dimension in dimension_gaps:
        # A dimension whose released deep-search budget is already exhausted
        # has a zero current-round quota.  It stays absent from the plan while
        # other actionable dimensions continue; its budget boundary is not
        # allowed to block their query generation.
        expected = int(quotas[dimension])
        if planned_by_dimension[dimension] != expected:
            raise RuntimeError(
                "query_plan_dimension_quota_not_met: "
                f"{dimension} planned={planned_by_dimension[dimension]} required={expected}"
            )
    return rows


def targets_from_audit(path: Path) -> tuple[list[str], set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("--audit root must be a JSON object")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("--audit lacks summary")
    gaps = summary.get("dimension_evidence_gap_targets", [])
    enhancement = summary.get("dimension_evidence_enhancement_targets", [])
    combined = summary.get("dimension_iteration_targets")
    if not isinstance(gaps, list) or not isinstance(enhancement, list):
        raise ValueError("--audit has invalid dimension target lists")
    if combined is None:
        combined = [*gaps, *enhancement]
    if not isinstance(combined, list):
        raise ValueError("--audit has invalid dimension_iteration_targets")
    unknown = [str(value) for value in combined if str(value) not in DIMENSION_NAMES]
    if unknown:
        raise ValueError("--audit contains unknown dimension gap targets: " + ", ".join(unknown))
    unknown_enhancement = [str(value) for value in enhancement if str(value) not in DIMENSION_NAMES]
    if unknown_enhancement:
        raise ValueError(
            "--audit contains unknown dimension enhancement targets: "
            + ", ".join(unknown_enhancement)
        )
    return unique_nonempty([str(value) for value in combined]), {str(value) for value in enhancement}


def retrieval_context_from_audit(
    path: Path,
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        raise ValueError("--audit lacks summary")
    matrix = summary.get("platform_dimension_gap_matrix", [])
    if not isinstance(matrix, list) or any(not isinstance(item, dict) for item in matrix):
        raise ValueError("--audit has an invalid platform-dimension gap matrix")
    termination = summary.get("retrieval_termination")
    metrics = termination.get("metrics", {}) if isinstance(termination, dict) else {}
    if not isinstance(metrics, dict):
        raise ValueError("--audit has invalid retrieval query metrics")
    return [dict(item) for item in matrix], dict(metrics), dict(payload)


def read_existing_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ValueError("existing query plan JSON is invalid")
        return [{str(key): str(value) for key, value in item.items()} for item in payload]
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [{str(key): str(value or "") for key, value in row.items()} for row in csv.DictReader(handle)]
    raise ValueError("existing query plan must be CSV or JSON")


def plan_binding_path(output: Path) -> Path:
    return output.with_name(output.name + ".binding.json")


def plan_transaction_path(output: Path) -> Path:
    return output.with_name(output.name + ".transaction.json")


def file_sha256_or_empty(path: Path | None) -> str:
    return sha256_file(path) if path is not None and path.is_file() else ""


def build_plan_binding_context(
    *,
    task_run_id: str,
    place: str,
    aliases: list[str],
    buildings: list[str],
    local_terms: list[str],
    iteration_round: int,
    gaps: list[str],
    enhancement_dimensions: set[str],
    controls: Mapping[str, object],
    audit_path: Path | None,
    state_path: Path | None,
    history_path: Path | None,
    audit_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not task_run_id.strip():
        raise ValueError("task_run_id is required for a persisted query plan")
    identity = {
        "place": " ".join(place.split()).casefold(),
        "aliases": sorted({value.casefold() for value in unique_nonempty(aliases)}),
        "buildings": sorted({value.casefold() for value in unique_nonempty(buildings)}),
        "local_terms": sorted({value.casefold() for value in unique_nonempty(local_terms)}),
    }
    audit_hash = file_sha256_or_empty(audit_path)
    state_hash = file_sha256_or_empty(state_path)
    history_hash = file_sha256_or_empty(history_path)
    snapshot = {}
    if iteration_round > 0:
        if audit_path is None or history_path is None:
            raise ValueError('deep_plan_requires_bound_audit_and_history')
        import base64
        from execution_facts import read_rows
        audit_bytes = audit_path.read_bytes()
        history_bytes = history_path.read_bytes()
        payload = json.loads(audit_bytes.decode('utf-8-sig'))
        actual_binding = validate_iteration_audit_binding(audit_payload=payload,
            task_run_id=task_run_id, controls=controls, history_rows=read_rows(history_path))
        if state_path is not None:
            current = execution_schema_context_from_state(
                json.loads(state_path.read_text(encoding='utf-8-sig')), state_path=state_path)
            provenance = payload['summary']['dimension_evidence']['audit_provenance']
            prior_context = provenance.get('execution_schema_context') or {}
            if prior_context.get('amendments_sha256', '') != current.get('amendments_sha256', ''):
                raise ValueError('deep_plan_prior_audit_amendments_changed')
            from execution_facts import validate_facts
            current_facts = validate_facts(read_rows(history_path), config=controls, schema_context=current)
            if current_facts['invalid_records'] or any(current_facts[key] != provenance.get(key) for key in
                    ('valid_execution_set_sha256', 'invalid_execution_set_sha256')):
                raise ValueError('deep_plan_prior_audit_execution_view_changed')
        if audit_binding and dict(audit_binding) != actual_binding:
            raise ValueError('deep_plan_audit_binding_mismatch')
        audit_binding = actual_binding
        snapshot = {**actual_binding, 'iteration_round': iteration_round,
            'audit_bytes_base64': base64.b64encode(audit_bytes).decode('ascii'),
            'history_bytes_base64': base64.b64encode(history_bytes).decode('ascii'),
            'history_format': history_path.suffix,
            'plan_generation_time': (json.loads(state_path.read_text(encoding='utf-8-sig')).get('updated_at')
                or json.loads(state_path.read_text(encoding='utf-8-sig')).get('created_at')) if state_path else
                max((str(r.get('finished_at', '')) for r in read_rows(history_path)), default=''),
        }
    parameters = {
        "task_run_id": task_run_id.strip(),
        "place": identity["place"],
        "place_identity_sha256": canonical_sha256(identity),
        "alias_set_sha256": canonical_sha256(identity["aliases"]),
        "building_set_sha256": canonical_sha256(identity["buildings"]),
        "local_term_set_sha256": canonical_sha256(identity["local_terms"]),
        "iteration_round": int(iteration_round),
        "gap_targets": unique_nonempty(gaps),
        "enhancement_targets": sorted(enhancement_dimensions),
        "audit_sha256": audit_hash,
        "state_sha256": state_hash,
        "history_sha256": history_hash,
        "audit_machine_binding": dict(audit_binding or {}),
        "retrieval_control_sha256": canonical_sha256(controls),
        "generator_sha256": sha256_file(Path(__file__).resolve()),
        "audit_snapshot": snapshot,
        "temporal_binding": __import__('temporal_fields').plan_state_binding(state_path),
    }
    return {
        "schema_version": PLAN_BINDING_SCHEMA,
        **parameters,
        "parameter_binding_sha256": canonical_sha256(parameters),
    }


def validate_existing_plan_binding(
    output: Path,
    expected: Mapping[str, object],
) -> dict[str, object]:
    binding_file = plan_binding_path(output)
    if not output.is_file():
        return {"valid": False, "reason": "query_plan_missing"}
    if not binding_file.is_file():
        return {"valid": False, "reason": "query_plan_binding_missing"}
    try:
        payload = json.loads(binding_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"valid": False, "reason": "query_plan_binding_invalid_json"}
    if not isinstance(payload, dict):
        return {"valid": False, "reason": "query_plan_binding_invalid_root"}
    for key, value in expected.items():
        if payload.get(key) != value:
            return {"valid": False, "reason": f"query_plan_binding_mismatch:{key}"}
    if payload.get("query_plan_sha256") != sha256_file(output):
        return {"valid": False, "reason": "query_plan_hash_mismatch"}
    try:
        rows = read_existing_rows(output)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {"valid": False, "reason": "query_plan_content_invalid"}
    if int(payload.get("query_count", -1)) != len(rows):
        return {"valid": False, "reason": "query_plan_count_mismatch"}
    return {"valid": True, "reason": "query_plan_binding_match", "rows": rows}


def validate_iteration_audit_binding(
    *,
    audit_payload: Mapping[str, object],
    task_run_id: str,
    controls: Mapping[str, object],
    history_rows: list[dict[str, str]] | None,
) -> dict[str, object]:
    """Fail closed when an iteration audit is stale or belongs to another run."""

    summary = audit_payload.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("audit_binding_invalid: evidence audit lacks summary")
    dimension_audit = summary.get("dimension_evidence")
    if not isinstance(dimension_audit, Mapping):
        raise ValueError("audit_binding_invalid: evidence audit lacks dimension results")
    provenance = dimension_audit.get("audit_provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("audit_binding_invalid: dimension audit lacks machine provenance")
    if str(provenance.get("task_run_id", "")) != task_run_id:
        raise ValueError("audit_binding_mismatch: task_run_id")
    for field in (
        "place_identity_sha256",
        "audit_transaction_id",
        "evidence_records_sha256",
        "source_records_sha256",
        "retrieval_control_sha256",
    ):
        if not str(provenance.get(field, "")).strip():
            raise ValueError(f"audit_binding_invalid: {field} is missing")
    if str(provenance.get("retrieval_control_sha256", "")) != canonical_sha256(
        controls
    ):
        raise ValueError("audit_binding_mismatch: retrieval control")
    target_confidence = str(dimension_audit.get("target_confidence", ""))
    if target_confidence not in {"中", "中高", "高"}:
        raise ValueError("audit_binding_invalid: target confidence")
    if str(provenance.get("target_confidence", "")) != target_confidence:
        raise ValueError("audit_binding_mismatch: target confidence")
    if str(summary.get("task_run_id", "")) not in {"", task_run_id}:
        raise ValueError("audit_binding_mismatch: summary task_run_id")
    expected_search_hash = str(provenance.get("search_records_sha256", ""))
    if not expected_search_hash:
        raise ValueError("audit_binding_invalid: search ledger hash is missing")
    from execution_facts import set_hash as execution_set_hash
    if history_rows is not None and execution_set_hash(history_rows) != expected_search_hash:
        raise ValueError("audit_binding_mismatch: executed search history")
    termination = summary.get("retrieval_termination")
    if isinstance(termination, Mapping) and termination:
        if termination.get("budgets") != budget_values(controls):
            raise ValueError("audit_binding_mismatch: retrieval control budgets")
    dimension_states = {
        name: {
            key: dimension_audit.get("dimensions", {}).get(name, {}).get(key)
            for key in (
                "status",
                "confidence",
                "target_reached",
                "exhaustion_supported",
                "independent_exhaustion_audit_passed",
                "target_confidence",
            )
        }
        for name in DIMENSION_NAMES
        if isinstance(dimension_audit.get("dimensions"), Mapping)
        and isinstance(dimension_audit.get("dimensions", {}).get(name), Mapping)
    }
    return {
        "task_run_id": task_run_id,
        "place_identity_sha256": str(provenance["place_identity_sha256"]),
        "audit_transaction_id": str(provenance["audit_transaction_id"]),
        "search_records_sha256": expected_search_hash,
        "evidence_records_sha256": str(provenance.get("evidence_records_sha256", "")),
        "source_records_sha256": str(provenance.get("source_records_sha256", "")),
        "dimension_states_sha256": canonical_sha256(dimension_states),
        "dimension_states": dimension_states,
        "retrieval_metrics_sha256": canonical_sha256(
            termination.get("metrics", {}) if isinstance(termination, Mapping) else {}
        ),
        "target_confidence": target_confidence,
    }


def validate_plan_audit_snapshot(binding):
    """Re-read the immutable audit bytes embedded in the committed binding."""
    import base64
    import hashlib
    import io
    snap = binding.get('audit_snapshot')
    if not isinstance(snap, dict) or snap.get('iteration_round') != binding['iteration_round']:
        raise ValueError('deep_plan_audit_snapshot_invalid')
    audit_bytes = base64.b64decode(snap['audit_bytes_base64'], validate=True)
    history_bytes = base64.b64decode(snap['history_bytes_base64'], validate=True)
    if (hashlib.sha256(audit_bytes).hexdigest() != binding['audit_sha256'] or
            hashlib.sha256(history_bytes).hexdigest() != binding['history_sha256']):
        raise ValueError('deep_plan_audit_snapshot_hash_mismatch')
    history = (json.loads(history_bytes.decode('utf-8-sig')) if snap['history_format'] == '.json'
        else list(csv.DictReader(io.StringIO(history_bytes.decode('utf-8-sig')))))
    actual = validate_iteration_audit_binding(audit_payload=json.loads(audit_bytes.decode('utf-8-sig')),
        task_run_id=binding['task_run_id'], controls=load_retrieval_config(), history_rows=history)
    if actual != binding['audit_machine_binding'] or any(snap.get(k) != v for k, v in actual.items()):
        raise ValueError('deep_plan_prior_confidence_binding_invalid')
    from temporal_fields import timestamp
    timestamp(snap['plan_generation_time'])


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _serialized_rows(rows: list[dict[str, str]], output: Path) -> bytes:
    if output.suffix.lower() == ".json":
        return (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if output.suffix.lower() != ".csv":
        raise ValueError("--output must end in .csv or .json")
    import io

    text = io.StringIO(newline="")
    writer = csv.DictWriter(
        text,
        fieldnames=list(rows[0].keys()) if rows else ITERATION_OUTPUT_FIELDS,
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + text.getvalue().encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_plan_transaction(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("plan_transaction_invalid: journal is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "query-plan-transaction-1":
        raise ValueError("plan_transaction_invalid: journal schema is invalid")
    return payload


def _transaction_bytes(payload: Mapping[str, object], field: str) -> bytes:
    try:
        content = base64.b64decode(str(payload[field]), validate=True)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"plan_transaction_invalid: {field} is invalid") from exc
    expected = str(payload.get(field.replace("_base64", "_sha256"), ""))
    if _sha256_bytes(content) != expected:
        raise ValueError(f"plan_transaction_invalid: {field} hash mismatch")
    return content


def _recover_plan_transaction(
    output: Path,
    binding_context: Mapping[str, object],
    *,
    fault_at: str = "",
) -> bool:
    journal_path = plan_transaction_path(output)
    journal = _load_plan_transaction(journal_path)
    if journal is None:
        return False
    binding_file = plan_binding_path(output)
    if str(journal.get("output_path", "")) not in {output.name, str(output.resolve())}:
        raise ValueError("plan_transaction_conflict: journal output path differs")
    if str(journal.get("binding_path", "")) not in {binding_file.name, str(binding_file.resolve())}:
        raise ValueError("plan_transaction_conflict: journal binding path differs")
    if str(journal.get("binding_context_sha256", "")) != canonical_sha256(
        dict(binding_context)
    ):
        raise ValueError("plan_transaction_conflict: journal belongs to another plan context")
    for field in ("task_run_id", "place_identity_sha256", "iteration_round"):
        if journal.get(field) != binding_context.get(field):
            raise ValueError(f"plan_transaction_conflict: {field} differs")
    plan_content = _transaction_bytes(journal, "plan_base64")
    binding_content = _transaction_bytes(journal, "binding_base64")
    for path, expected_hash, old_exists, old_hash in (
        (
            output,
            str(journal["plan_sha256"]),
            bool(journal.get("old_plan_exists")),
            str(journal.get("old_plan_sha256", "")),
        ),
        (
            binding_file,
            str(journal["binding_sha256"]),
            bool(journal.get("old_binding_exists")),
            str(journal.get("old_binding_sha256", "")),
        ),
    ):
        if not path.exists():
            if old_exists and str(journal.get("stage", "")) == "prepared":
                raise ValueError("plan_transaction_conflict: pre-existing target disappeared")
            continue
        current_hash = sha256_file(path)
        allowed = {expected_hash}
        if old_exists and old_hash:
            allowed.add(old_hash)
        if current_hash not in allowed:
            raise ValueError("plan_transaction_conflict: target changed outside the transaction")
    _atomic_write_bytes(output, plan_content)
    if fault_at == "after_recovery_plan_replace":
        raise RuntimeError("injected query-plan recovery fault: after_recovery_plan_replace")
    _atomic_write_bytes(binding_file, binding_content)
    if fault_at == "after_recovery_binding_replace":
        raise RuntimeError("injected query-plan recovery fault: after_recovery_binding_replace")
    if sha256_file(output) != str(journal["plan_sha256"]):
        raise ValueError("plan_transaction_recovery_failed: plan hash mismatch")
    if sha256_file(binding_file) != str(journal["binding_sha256"]):
        raise ValueError("plan_transaction_recovery_failed: binding hash mismatch")
    for name in ("plan_stage_path", "binding_stage_path"):
        raw = str(journal.get(name, ""))
        if raw:
            from run_paths import safe_run_relative_path
            legacy = Path(raw)
            if legacy.is_absolute() and legacy.parent.resolve() != output.parent.resolve():
                raise ValueError("plan_transaction_stage_path_invalid")
            stage = legacy if legacy.is_absolute() else safe_run_relative_path(raw, output.parent)
            if stage.parent.resolve() != output.parent.resolve() or not stage.name.startswith(output.name + ".") or not stage.name.endswith(".stage"):
                raise ValueError("plan_transaction_stage_path_invalid")
            if stage.exists():
                stage.unlink()
    if fault_at == "after_recovery_stage_cleanup":
        raise RuntimeError("injected query-plan recovery fault: after_recovery_stage_cleanup")
    journal_path.unlink()
    return True


def write_or_reuse_bound_plan(
    rows: list[dict[str, str]],
    output: Path,
    binding_context: Mapping[str, object],
    *,
    fault_at: str = "",
    run_root: Path | None = None,
) -> tuple[list[dict[str, str]], bool, str]:
    """Persist plan and binding as one recoverable, lock-protected transaction."""

    from execution_facts import finalize_plan_rows, register_committed_plan, precheck_plan_identities

    rows = finalize_plan_rows(rows, binding_context)
    from execution_coordinator import lock, assert_no_pending
    with lock(run_root or output.parent), ProcessFileLock(output), ProcessFileLock((run_root or output.parent) / '.query-plans/registry.json'):
        assert_no_pending(run_root or output.parent)
        recovered = _recover_plan_transaction(
            output,
            binding_context,
            fault_at=fault_at,
        )
        existing = validate_existing_plan_binding(output, binding_context)
        if existing["valid"]:
            register_committed_plan(output, run_root=run_root or output.parent, _registry_locked=True)
            reason = "query_plan_transaction_recovered" if recovered else str(existing["reason"])
            return list(existing["rows"]), True, reason
        mismatch_reason = str(existing["reason"])
        binding_file = plan_binding_path(output)
        if output.is_file() != binding_file.is_file():
            raise ValueError(
                "plan_binding_mismatch: plan and binding must either both exist or both be absent"
            )
        if output.exists():
            if not binding_file.is_file():
                raise ValueError("plan_binding_mismatch: existing plan has no binding sidecar")
            try:
                prior_binding = json.loads(binding_file.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("plan_binding_mismatch: existing binding is unreadable") from exc
            if not isinstance(prior_binding, Mapping):
                raise ValueError("plan_binding_mismatch: existing binding root is invalid")
            for identity_field in (
                "task_run_id",
                "place_identity_sha256",
                "iteration_round",
            ):
                if prior_binding.get(identity_field) != binding_context.get(identity_field):
                    raise ValueError(
                        f"plan_binding_mismatch: output path belongs to another {identity_field}"
                    )
            if mismatch_reason in {"query_plan_hash_mismatch", "query_plan_count_mismatch"}:
                raise ValueError(f"plan_binding_mismatch: {mismatch_reason}")
        precheck_plan_identities(rows, run_root=run_root or output.parent)
        plan_content = _serialized_rows(rows, output)
        binding = {
            **dict(binding_context),
            "query_plan_file": output.name,
            "query_plan_sha256": _sha256_bytes(plan_content),
            "query_count": len(rows),
            "generated_at": __import__('temporal_fields').utc_now(),
        }
        binding_content = (
            json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        transaction_id = canonical_sha256(
            {
                "binding_context": dict(binding_context),
                "plan_sha256": _sha256_bytes(plan_content),
                "binding_sha256": _sha256_bytes(binding_content),
            }
        )
        plan_stage = output.with_name(output.name + f".{transaction_id[:16]}.plan.stage")
        binding_stage = output.with_name(output.name + f".{transaction_id[:16]}.binding.stage")
        journal = {
            "schema_version": "query-plan-transaction-1",
            "transaction_id": transaction_id,
            "stage": "prepared",
            "task_run_id": binding_context.get("task_run_id"),
            "place_identity_sha256": binding_context.get("place_identity_sha256"),
            "iteration_round": binding_context.get("iteration_round"),
            "binding_context_sha256": canonical_sha256(dict(binding_context)),
            "output_path": output.name,
            "binding_path": binding_file.name,
            "plan_stage_path": plan_stage.name,
            "binding_stage_path": binding_stage.name,
            "old_plan_exists": output.is_file(),
            "old_plan_sha256": sha256_file(output) if output.is_file() else "",
            "old_binding_exists": binding_file.is_file(),
            "old_binding_sha256": sha256_file(binding_file) if binding_file.is_file() else "",
            "plan_base64": base64.b64encode(plan_content).decode("ascii"),
            "plan_sha256": _sha256_bytes(plan_content),
            "binding_base64": base64.b64encode(binding_content).decode("ascii"),
            "binding_sha256": _sha256_bytes(binding_content),
        }

        def persist_journal(stage: str) -> None:
            journal["stage"] = stage
            _atomic_write_bytes(
                plan_transaction_path(output),
                (json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )

        def inject(boundary: str) -> None:
            if fault_at == boundary:
                raise RuntimeError(f"injected query-plan transaction fault: {boundary}")

        persist_journal("prepared")
        inject("after_journal_prepare")
        _atomic_write_bytes(plan_stage, plan_content)
        persist_journal("plan_staged")
        inject("after_plan_stage")
        _atomic_write_bytes(binding_stage, binding_content)
        persist_journal("binding_staged")
        inject("after_binding_stage")
        os.replace(plan_stage, output)
        persist_journal("plan_replaced")
        inject("after_plan_replace")
        os.replace(binding_stage, binding_file)
        persist_journal("binding_replaced")
        inject("after_binding_replace")
        if sha256_file(output) != journal["plan_sha256"]:
            raise ValueError("plan_transaction_commit_failed: plan hash mismatch")
        if sha256_file(binding_file) != journal["binding_sha256"]:
            raise ValueError("plan_transaction_commit_failed: binding hash mismatch")
        persist_journal("committed")
        inject("after_commit_mark")
        plan_transaction_path(output).unlink()
        register_committed_plan(output, run_root=run_root or output.parent, _registry_locked=True)
        return rows, False, mismatch_reason


def plan_gate(
    *,
    iteration_round: int,
    audit_payload: dict[str, object] | None,
    state_payload: dict[str, object] | None,
    prior_metrics: dict[str, object],
    controls: dict[str, object],
) -> dict[str, object]:
    budgets = budget_values(controls)
    if iteration_round > budgets["maximum_iteration_rounds"]:
        return {"allowed": False, "reason": "maximum_iteration_rounds_reached"}
    if state_payload:
        phase = str(state_payload.get("phase", ""))
        artifacts = state_payload.get("artifacts", {})
        freeze_exists = (
            isinstance(artifacts, dict) and bool(artifacts.get("truth_freeze"))
        )
        if (
            phase in {"SCORE", "REPORT", "REPORT_BUILD", "VALIDATE", "DELIVER", "DONE"}
            or state_payload.get("truth_frozen") is True
            or freeze_exists
            or state_payload.get("status") == "complete"
        ):
            return {"allowed": False, "reason": "truth_frozen_or_delivery_started"}
    budget_compliance = validate_query_budget_compliance(
        prior_metrics,
        config=controls,
    )
    if budget_compliance["status"] != "valid":
        return {
            "allowed": False,
            "reason": "prior_query_budget_invalid",
            "budget_error_codes": list(budget_compliance["error_codes"]),
        }
    if audit_payload:
        status = str(audit_payload.get("status", ""))
        summary = audit_payload.get("summary", {})
        termination = summary.get("retrieval_termination", {}) if isinstance(summary, dict) else {}
        if status in AUDIT_TERMINAL_STATUSES or (
            isinstance(termination, dict) and termination.get("terminal") is True
        ):
            return {"allowed": False, "reason": "formal_retrieval_terminal"}
        if status and status != AUDIT_NEEDS_ITERATION:
            return {"allowed": False, "reason": "formal_audit_not_ready_for_iteration"}
    rounds = {
        int(value) for value in prior_metrics.get("queries_by_round", {})
        if str(value).isdigit() and int(value) > 0
    } if isinstance(prior_metrics.get("queries_by_round", {}), dict) else set()
    if iteration_round in rounds:
        return {"allowed": False, "reason": "iteration_round_already_executed"}
    required_dimensions: list[str] = []
    if audit_payload:
        summary = audit_payload.get("summary", {})
        termination = summary.get("retrieval_termination", {}) if isinstance(summary, dict) else {}
        raw_remaining = termination.get("remaining_dimensions", []) if isinstance(termination, dict) else []
        if isinstance(raw_remaining, list):
            required_dimensions = [str(value) for value in raw_remaining if str(value) in DIMENSION_NAMES]
    round_validation = validate_round_metrics(
        prior_metrics,
        config=controls,
        required_dimensions=required_dimensions,
        expected_round_count=max(0, iteration_round - 1),
    )
    if iteration_round > 1 and round_validation["round_budget_reached"] is not True:
        return {"allowed": False, "reason": "previous_iteration_round_incomplete"}
    if (
        int(prior_metrics.get("deep_executed_query_count", 0) or 0)
        >= budgets["global_attempt_budget"]
        or int(prior_metrics.get("deep_unique_query_intent_count", 0) or 0)
        >= budgets["global_independent_intent_budget"]
    ):
        return {"allowed": False, "reason": "global_query_budget_reached"}
    return {"allowed": True, "reason": "planning_allowed"}


def write_rows(rows: list[dict[str, str]], output: Path | None) -> None:
    if output is None:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    _atomic_write_bytes(output, _serialized_rows(rows, output))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--place", required=True, help="Unambiguous place name")
    parser.add_argument("--alias", action="append", default=[], help="Repeatable alias")
    parser.add_argument(
        "--building", action="append", default=[], help="Repeatable representative building"
    )
    parser.add_argument(
        "--local-term", action="append", default=[], help="Repeatable local-culture term"
    )
    parser.add_argument(
        "--iteration-round",
        type=int,
        default=0,
        help="0 builds the baseline plan; 1 or greater builds a deep-search iteration",
    )
    parser.add_argument(
        "--gap",
        action="append",
        default=[],
        help="Repeatable evidence gap for a deep-search iteration",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        help="Evidence-audit JSON; pending dimension gaps are loaded automatically",
    )
    parser.add_argument("--output", type=Path, help="CSV or JSON destination")
    parser.add_argument(
        "--task-run-id",
        default="",
        help="Required immutable run identifier when --output persists a plan",
    )
    parser.add_argument("--state", type=Path, help="Current run-state JSON used for code-level planning gates")
    parser.add_argument("--history", type=Path, help="Executed search-log CSV used for cross-round intent exclusion")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.iteration_round < 0:
        raise SystemExit("--iteration-round cannot be negative")
    if args.output and not args.task_run_id.strip():
        raise SystemExit("--task-run-id is required when --output persists a query plan")
    if args.output and not args.state:
        raise SystemExit("--state is required when --output persists a query plan")
    controls = load_retrieval_config()
    plan_reused = False
    binding_reason = ""
    gate: dict[str, object] = {"allowed": True, "reason": "baseline_planning_allowed"}
    gaps: list[str] = []
    enhancement_dimensions: set[str] = set()
    audit_payload: dict[str, object] | None = None
    prior_query_metrics: dict[str, object] = {}
    history_rows: list[dict[str, str]] | None = None
    audit_binding: dict[str, object] = {}
    planned_counts: dict[str, int] = {}
    schema_context = None
    if args.state:
        schema_state = json.loads(args.state.read_text(encoding='utf-8-sig'))
        schema_context = execution_schema_context_from_state(schema_state, state_path=args.state)
        if args.task_run_id and schema_context['task_run_id'] != args.task_run_id:
            raise SystemExit('plan_binding_mismatch: state task_run_id')
        from execution_facts import text as identity_text
        if schema_context['place'] != identity_text(args.place).casefold():
            raise SystemExit('plan_binding_mismatch: state place identity')
    if args.iteration_round:
        audit_gaps: list[str] = []
        if args.audit:
            audit_gaps, enhancement_dimensions = targets_from_audit(args.audit)
            platform_gap_matrix, prior_query_metrics, audit_payload = retrieval_context_from_audit(args.audit)
        else:
            platform_gap_matrix, prior_query_metrics = [], {}
        if args.history:
            with args.history.open("r", encoding="utf-8-sig", newline="") as handle:
                history_rows = list(csv.DictReader(handle))
            prior_query_metrics = executed_query_metrics(history_rows, schema_context=schema_context)
        state_payload: dict[str, object] | None = None
        if args.state:
            loaded_state = json.loads(args.state.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded_state, dict):
                raise SystemExit("--state must contain a JSON object")
            state_payload = loaded_state
            if args.task_run_id and str(state_payload.get("task_run_id", "")) != args.task_run_id:
                raise RuntimeError("plan_binding_mismatch: state task_run_id")
            if " ".join(str(state_payload.get("place", "")).split()).casefold() != " ".join(
                args.place.split()
            ).casefold():
                raise RuntimeError("plan_binding_mismatch: state place identity")
        if audit_payload is not None:
            if not args.task_run_id:
                raise RuntimeError("audit_binding_invalid: --task-run-id is required")
            audit_binding = validate_iteration_audit_binding(
                audit_payload=audit_payload,
                task_run_id=args.task_run_id,
                controls=controls,
                history_rows=history_rows,
            )
            if state_payload is not None and str(
                state_payload.get("target_confidence", "")
            ) != str(audit_binding.get("target_confidence", "")):
                raise RuntimeError("audit_binding_mismatch: state target confidence")
        gate = plan_gate(
            iteration_round=args.iteration_round,
            audit_payload=audit_payload,
            state_payload=state_payload,
            prior_metrics=prior_query_metrics,
            controls=controls,
        )
        gaps = unique_nonempty(
            [
                *args.gap,
                *audit_gaps,
            ]
        )
        if not any(gap in DIMENSION_NAMES for gap in gaps):
            rows = []
            gate = {"allowed": False, "reason": "no_pending_dimension_targets",
                    "collection_coverage_gaps": [gap for gap in gaps if gap not in DIMENSION_NAMES]}
        elif gate["allowed"]:
            target_dimensions = [item for item in gaps if item in DIMENSION_NAMES]
            feasibility = deep_search_budget_feasibility(
                dimensions=target_dimensions,
                iteration_round=args.iteration_round,
                metrics=prior_query_metrics,
                config=controls,
                dimension_confidences={
                    dimension: "中" if dimension in enhancement_dimensions else ""
                    for dimension in target_dimensions
                },
            )
            if feasibility["status"] != "feasible":
                raise RuntimeError(
                    "budget_configuration_infeasible: "
                    + json.dumps(feasibility, ensure_ascii=False, sort_keys=True)
                )
            rows = build_iteration_rows(
                args.place,
                args.alias,
                args.building,
                gaps,
                args.iteration_round,
                enhancement_dimensions,
                retrieval_config=controls,
                platform_gap_matrix=platform_gap_matrix,
                prior_query_metrics=prior_query_metrics,
            )
        else:
            rows = []
        target_dimensions = [item for item in gaps if item in DIMENSION_NAMES]
        planned_counts = {
            dimension: sum(
                dimension in json.loads(row.get("query_dimension_targets", "[]"))
                for row in rows
            )
            for dimension in target_dimensions
        }
    else:
        rows = build_rows(args.place, args.alias, args.building, args.local_term)
        if len(rows) < 45:
            raise RuntimeError("query plan unexpectedly contains fewer than 45 queries")
    if args.output:
        binding_context = build_plan_binding_context(
            task_run_id=args.task_run_id,
            place=args.place,
            aliases=args.alias,
            buildings=args.building,
            local_terms=args.local_term,
            iteration_round=args.iteration_round,
            gaps=gaps,
            enhancement_dimensions=enhancement_dimensions,
            controls=controls,
            audit_path=args.audit,
            state_path=args.state,
            history_path=args.history,
            audit_binding=audit_binding,
        )
        if not args.iteration_round or bool(gate["allowed"]):
            # The committed query identifier includes its immutable plan namespace.
            namespace = str(binding_context["parameter_binding_sha256"])[:16]
            rows = [{**row, "query_id": str(row["query_id"]) + "-" + namespace} for row in rows]
            rows, plan_reused, binding_reason = write_or_reuse_bound_plan(
                rows, args.output, binding_context,
                run_root=args.state.parent if args.state else args.output.parent,
            )
            if plan_reused:
                gate = {"allowed": False, "reason": "existing_bound_plan_reused"}
        else:
            binding_reason = "plan_not_persisted_because_gate_closed"
    else:
        write_rows(rows, None)
    if args.output:
        summary = {
            "place": args.place,
            "queries": len(rows),
            "positive_queries": sum(r["polarity"] == "positive" for r in rows),
            "negative_queries": sum(r["polarity"] == "negative" for r in rows),
            "iteration_round": args.iteration_round,
            "stop_reason": (
                str(gate["reason"])
                if args.iteration_round and not gate["allowed"]
                else "existing_bound_plan_reused"
                if plan_reused
                else "plan_generated"
            ),
            "structured_terminal": bool(args.iteration_round and not rows),
            "restricted_delivery_allowed": bool(
                args.iteration_round
                and not rows
                and gate["reason"] == "formal_retrieval_terminal"
            ),
            "plan_reused": plan_reused,
            "binding_status": binding_reason,
            "plan_binding": str(plan_binding_path(args.output).resolve()),
            "planned_queries_by_dimension": planned_counts if args.iteration_round else {},
            "research_targets": research_target_summary(config=controls)
            if args.iteration_round else research_target_summary(),
            "output": str(args.output.resolve()),
        }
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
