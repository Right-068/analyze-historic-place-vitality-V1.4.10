#!/usr/bin/env python3
"""Compile all reportable machine facts from frozen formal artifacts.

The output is the only source for counts, scores, weights, thresholds, rounds,
protocol identity, and the fixed 7+1 result-layer definition used by reports.
It contains no model-authored interpretation.
"""

from __future__ import annotations

import argparse
import csv
import strict_json as json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping

from artifact_provenance import (
    register_protected_artifact,
    verify_artifact_writer,
    verify_truth_freeze,
)
from dimension_framework import (
    DIMENSION_NAMES,
    DIMENSION_WEIGHT_FORMULA,
    DIMENSION_WEIGHTS,
    RESULT_LAYER_NAME,
    WEIGHT_SCHEME_NAME,
)
from formal_states import (
    AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
    audit_status_zh,
)
from runtime_guard import authorize_runtime_write, canonical_sha256, sha256_file
from quantify_text_semantics import mention_level


REPORT_TRUTH_SCHEMA_VERSION = "report-truth-1"

ACCESS_STATUS_ZH = {
    "full": "完整读取",
    "partial": "部分读取",
    "snippet_only": "仅检索摘要",
    "metadata_only": "仅元数据",
    "inaccessible": "不可访问",
    "blocked": "访问受阻",
    "login_required": "需要登录",
    "captcha": "验证码阻断",
    "no_results": "无结果",
}
CONTENT_LAYER_ZH = {
    "official_fact": "官方事实",
    "open_data_record": "开放数据记录",
    "heritage_record": "遗产登记记录",
    "news_report": "新闻报道",
    "academic_claim": "学术论述",
    "page_body": "页面正文",
    "user_post": "用户帖子",
    "user_review": "用户评价",
    "comment": "评论",
    "reply": "回复",
    "search_snippet": "检索摘要",
    "rating_only": "原生数值评价",
}
SOURCE_CATEGORY_ZH = {
    "official": "官方机构",
    "government_open_data": "政府开放数据",
    "heritage_registry": "遗产名录",
    "museum_venue": "博物馆与文化场馆",
    "news": "新闻媒体",
    "professional": "专业资料",
    "academic": "学术资料",
    "encyclopedic": "百科资料",
    "user_review": "用户评价",
    "map_review": "地图评价",
    "travel_ugc": "旅游用户内容",
    "social_content": "社交公开内容",
    "blog_travel": "博客与游记",
    "local_forum": "本地论坛",
    "community_content": "社区内容",
    "business_directory": "商业名录",
}


def _platform_display(value: str) -> str:
    label = str(value or "").strip()
    if label and all(ord(character) < 128 for character in label):
        return f"{label}（平台标识）"
    return label


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path.name}")
    return payload


def _truth(value: object) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "是"}


def _round_list(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("targeted_deep_rounds must be an array")
    rounds: list[int] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ValueError("targeted_deep_rounds must contain positive integers")
        rounds.append(raw)
    if rounds != sorted(set(rounds)):
        raise ValueError("targeted_deep_rounds must be sorted and unique")
    return rounds


def compile_truth(
    *,
    task_run_id: str,
    sources: list[dict[str, str]],
    raw_evidence: list[dict[str, str]],
    formal_evidence: list[dict[str, str]],
    search_rows: list[dict[str, str]],
    evidence_audit: Mapping[str, object],
    platform_scores: Mapping[str, object],
    scores: Mapping[str, object],
    protocol: Mapping[str, object],
    codebook: Mapping[str, object],
    freeze: Mapping[str, object],
    input_hashes: Mapping[str, str],
) -> dict[str, object]:
    for name, payload in (
        ("evidence_audit", evidence_audit),
        ("platform_scores", platform_scores),
        ("scores", scores),
    ):
        payload_run_id = payload.get("task_run_id")
        if name == "evidence_audit":
            summary = payload.get("summary")
            payload_run_id = summary.get("task_run_id") if isinstance(summary, dict) else ""
        if payload_run_id != task_run_id:
            raise ValueError(f"{name} belongs to a different task_run_id")
    score_dimensions = scores.get("cross_platform", {})
    if not isinstance(score_dimensions, dict):
        raise ValueError("scores.cross_platform must be an object")
    cross_dimensions = score_dimensions.get("dimensions")
    if not isinstance(cross_dimensions, dict) or list(cross_dimensions) != list(DIMENSION_NAMES):
        raise ValueError("scores must contain the seven canonical dimensions in order")
    audit_summary = evidence_audit.get("summary")
    if not isinstance(audit_summary, dict):
        raise ValueError("evidence audit summary is missing")
    dimension_audit = audit_summary.get("dimension_evidence")
    audit_dimensions = dimension_audit.get("dimensions") if isinstance(dimension_audit, dict) else None
    if not isinstance(audit_dimensions, dict):
        raise ValueError("dimension evidence audit is missing")

    eligible_rows = [row for row in formal_evidence if _truth(row.get("formal_scoring_eligible"))]
    scored_rows = [row for row in formal_evidence if _truth(row.get("included_in_platform_score"))]
    direction_counts = Counter(row.get("scoring_direction", "") for row in scored_rows)
    source_by_id = {row.get("source_id", ""): row for row in sources}
    user_source_count = sum(_truth(row.get("is_user_source")) for row in sources)
    source_types = Counter(row.get("source_category", "") for row in sources if row.get("source_category"))
    platforms = sorted({row.get("platform", "") for row in sources if row.get("platform")})
    sources_by_platform: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    sources_by_category: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in sources:
        if row.get("platform"):
            sources_by_platform[row["platform"]].append(row)
        if row.get("source_category"):
            sources_by_category[row["source_category"]].append(row)

    def source_summary(label: str, rows: list[dict[str, str]], group_type: str) -> dict[str, object]:
        dates = sorted(value for value in (row.get("published_at", "") for row in rows) if value)
        access = sorted({row.get("access_status", "") for row in rows if row.get("access_status")})
        layers = sorted({row.get("content_layer", "") for row in rows if row.get("content_layer")})
        categories = sorted({row.get("source_category", "") for row in rows if row.get("source_category")})
        return {
            "label": _platform_display(label) if group_type == "platform" else label,
            "machine_label": label,
            "group_type": group_type,
            "source_count": len(rows),
            "content_types": [CONTENT_LAYER_ZH.get(value, value) for value in layers],
            "source_categories": [SOURCE_CATEGORY_ZH.get(value, value) for value in categories],
            "time_range": [dates[0], dates[-1]] if dates else [],
            "access_statuses": [ACCESS_STATUS_ZH.get(value, value) for value in access],
            "used_for_scoring_source_count": sum(_truth(row.get("used_for_scoring")) for row in rows),
        }

    source_group_summary = [
        source_summary(platform, rows, "platform")
        for platform, rows in sorted(sources_by_platform.items())
    ]
    source_group_summary.extend(
        source_summary("来源类型：" + category, rows, "source_category")
        for category, rows in sorted(sources_by_category.items())
        if len(source_group_summary) < 8
    )
    aggregate_groups = [
        ("全部正式来源", sources),
        ("用户来源", [row for row in sources if _truth(row.get("is_user_source"))]),
        ("非用户来源", [row for row in sources if not _truth(row.get("is_user_source"))]),
        ("正式评分来源", [row for row in sources if _truth(row.get("used_for_scoring"))]),
        ("背景研究来源", [row for row in sources if not _truth(row.get("used_for_scoring"))]),
        ("完整读取来源", [row for row in sources if row.get("access_status") == "full"]),
        ("部分读取来源", [row for row in sources if row.get("access_status") == "partial"]),
        ("受限或不可读取来源", [row for row in sources if row.get("access_status") not in {"full", "partial"}]),
    ]
    existing_labels = {str(item["label"]) for item in source_group_summary}
    for label, rows in aggregate_groups:
        if len(source_group_summary) >= 8:
            break
        if label not in existing_labels:
            source_group_summary.append(source_summary(label, rows, "aggregate"))
            existing_labels.add(label)
    deep_rounds = sorted(
        {
            int(row["iteration_round"])
            for row in search_rows
            if str(row.get("iteration_round", "")).isdigit() and int(row["iteration_round"]) > 0
        }
    )
    retrieval_dates = sorted(
        str(value).strip()[:10]
        for value in [
            *(row.get("retrieved_at", "") for row in sources),
            *(row.get("retrieved_at", "") for row in search_rows),
        ]
        if str(value).strip()
    )
    semantic_methods = Counter(row.get("semantic_method", "") for row in formal_evidence)
    review_statuses = Counter(row.get("semantic_review_status", "") for row in formal_evidence)
    coding_statuses = Counter(row.get("coding_parse_status", "") for row in formal_evidence)
    lexicon_statuses = Counter(row.get("lexicon_match_status", "") for row in formal_evidence)
    low_confidence_threshold = float(protocol.get("thresholds", {}).get("low_confidence_review", 0.72))
    low_confidence_records = sum(
        1
        for row in formal_evidence
        if row.get("semantic_confidence", "")
        and float(row["semantic_confidence"]) < low_confidence_threshold
    )

    dimension_facts: list[dict[str, object]] = []
    for position, name in enumerate(DIMENSION_NAMES, start=1):
        score_item = cross_dimensions.get(name)
        audit_item = audit_dimensions.get(name)
        if not isinstance(score_item, dict) or not isinstance(audit_item, dict):
            raise ValueError(f"missing machine fact for dimension: {name}")
        rounds = _round_list(audit_item.get("targeted_deep_rounds", []))
        conversion = score_item.get("platform_equal_score")
        if conversion is not None and (isinstance(conversion, bool) or not isinstance(conversion, (int, float))):
            raise ValueError(f"invalid conversion score for dimension: {name}")
        conversion_display = None if conversion is None else round(float(conversion) + 1e-12, 1)
        tendency = None if conversion is None else round(float(conversion) / 10.0 - 5.0, 2)
        weighted = None if conversion is None else round(float(conversion) * DIMENSION_WEIGHTS[name] + 1e-12, 1)
        dimension_rows = [row for row in scored_rows if row.get("primary_dimension") == name]
        if not dimension_rows:
            dimension_rows = [row for row in formal_evidence if row.get("primary_dimension") == name]
        if not dimension_rows:
            dimension_rows = [
                row
                for row in raw_evidence
                if row.get("primary_dimension") == name
                or name in {part.strip() for part in str(row.get("dimension_tags", "")).split("|")}
            ]
        representative_sources: list[dict[str, object]] = []
        seen_urls: set[str] = set()
        for row in dimension_rows:
            source = source_by_id.get(row.get("source_id", ""), {})
            url = str(source.get("url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            representative_sources.append(
                {
                    "source_id": source.get("source_id", ""),
                    "label": source.get("page_title", "") or source.get("domain", "") or "代表性来源",
                    "url": url,
                }
            )
            if len(representative_sources) >= 5:
                break
        mention = mention_level(int(score_item.get("scored_evidence_units", 0)), max(1, len(scored_rows)))
        fact = {
            "number": position,
            "name": name,
            "weight": DIMENSION_WEIGHTS[name],
            "retrieved_evidence_units": int(audit_item.get("retrieved_evidence_units", 0)),
            "topic_coverage_evidence_units": int(audit_item.get("topic_coverage_evidence_units", 0)),
            "eligible_evidence_units": int(score_item.get("eligible_evidence_units", 0)),
            "scored_evidence_units": int(score_item.get("scored_evidence_units", 0)),
            "scoring_positive_units": int(score_item.get("scoring_positive_units", 0)),
            "scoring_neutral_units": int(score_item.get("scoring_neutral_units", 0)),
            "scoring_negative_units": int(score_item.get("scoring_negative_units", 0)),
            "distinct_source_pages": int(audit_item.get("distinct_source_pages", 0)),
            "source_category_count": int(audit_item.get("source_category_count", 0)),
            "dimension_candidate_platform_count": int(
                score_item.get("dimension_candidate_platform_count", 0)
            ),
            "dimension_scorable_platform_count": int(
                score_item.get("dimension_scorable_platform_count", 0)
            ),
            "run_included_platform_count": int(score_item.get("run_included_platform_count", 0)),
            "platform_minimum_rule_status": str(score_item.get("platform_minimum_rule_status", "")),
            "scoring_confidence": str(score_item.get("scoring_confidence", "")),
            "evidence_status": str(audit_item.get("status", "")),
            "numeric_score_permitted": bool(audit_item.get("numeric_score_permitted", False)),
            "target_confidence": str(audit_item.get("target_confidence", "")),
            "retrieval_termination_status": str(
                audit_item.get("retrieval_termination_status", "")
            ),
            "retrieval_termination_scope": audit_item.get(
                "retrieval_termination_scope", {}
            ),
            "remaining_scored_evidence_gap": int(
                audit_item.get("remaining_scored_evidence_gap", 0) or 0
            ),
            "targeted_deep_rounds": rounds,
            "targeted_round_count": len(rounds),
            "cross_platform_tendency": tendency,
            "conversion_score": conversion_display,
            "weighted_score": weighted,
            "mention_level": mention,
            "mention_range": f"按{name}在本次实际计分证据中的占比分级",
            "mention_basis": f"{name}提及率的分母为本次全部实际计分证据，未将主题标签重复计数",
            "representative_sources": representative_sources,
            "medium_enhancement_rounds": audit_item.get("medium_enhancement_rounds", []),
            "medium_completion_audit_passed": bool(audit_item.get("medium_completion_audit_passed", False)),
            "medium_completion_basis": str(audit_item.get("medium_completion_basis", "")),
            "independent_exhaustion_audit_passed": bool(audit_item.get("independent_exhaustion_audit_passed", False)),
            "exhaustion_basis": str(audit_item.get("exhaustion_basis", "")),
        }
        if fact["scoring_positive_units"] + fact["scoring_neutral_units"] + fact["scoring_negative_units"] != fact["scored_evidence_units"]:
            raise ValueError(f"direction counts do not match scored evidence: {name}")
        dimension_facts.append(fact)

    scoring_thresholds = protocol.get("thresholds")
    formal_scoring = protocol.get("formal_scoring")
    if not isinstance(scoring_thresholds, dict) or not isinstance(formal_scoring, dict):
        raise ValueError("evaluation protocol lacks fixed scoring thresholds")
    result_layer = scores.get("result_layer")
    if not isinstance(result_layer, dict) or result_layer.get("name") != RESULT_LAYER_NAME:
        raise ValueError("fixed 7+1 result layer is missing")
    composite = result_layer.get("base_composite_score")
    composite_display = None if composite is None else round(float(composite) + 1e-12, 1)
    confidence_rank = {"高": 3, "中高": 2, "中": 1, "低": 0, "": -1}
    dimension_confidences = [str(item["scoring_confidence"]) for item in dimension_facts]
    evaluation_confidence = min(
        dimension_confidences,
        key=lambda value: confidence_rank.get(value, -1),
    ) if dimension_confidences else ""
    audit_status = str(evidence_audit.get("status", ""))
    evidence_sufficiency = audit_status_zh(audit_status)
    sample_target_status = (
        "target_met"
        if bool(audit_summary.get("effective_sample_target_met"))
        else AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
        if audit_status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
        else "exhausted_with_shortfall"
        if "sample_shortfall" in audit_status
        else "needs_iteration"
    )
    payload: dict[str, object] = {
        "schema_version": REPORT_TRUTH_SCHEMA_VERSION,
        "status": "valid",
        "task_run_id": task_run_id,
        "place_name": str(scores.get("place_name", "")),
        "retrieval_date": retrieval_dates[-1] if retrieval_dates else "",
        "page_count": int(audit_summary.get("deduplicated_relevant_searched_pages", 0)),
        "source_count": len(sources),
        "user_source_count": user_source_count,
        "raw_evidence_units": len(raw_evidence),
        "eligible_evidence_units": len(eligible_rows),
        "scored_evidence_units": len(scored_rows),
        "minimum_effective_sample_target": int(
            audit_summary.get("minimum_effective_sample_target", 100)
        ),
        "legacy_minimum_effective_sample_floor": int(
            audit_summary.get("legacy_minimum_effective_sample_floor", 100)
        ),
        "target_confidence": str(audit_summary.get("target_confidence", "")),
        "scoring_positive_units": direction_counts["positive"],
        "scoring_neutral_units": direction_counts["neutral"],
        "scoring_negative_units": direction_counts["negative"],
        "source_type_counts": dict(sorted(source_types.items())),
        "observed_platforms": platforms,
        "source_group_summary": source_group_summary,
        "deep_iteration_rounds": deep_rounds,
        "deep_iteration_round_count": len(deep_rounds),
        "dimension_weight_scheme": WEIGHT_SCHEME_NAME,
        "dimension_weights": DIMENSION_WEIGHTS,
        "dimension_weight_formula": DIMENSION_WEIGHT_FORMULA,
        "dimensions": dimension_facts,
        "composite_score": composite_display,
        "sample_weighted_sensitivity_score": result_layer.get("sample_weighted_sensitivity_score"),
        "evaluation_protocol_version": protocol.get("evaluation_protocol_version"),
        "semantic_codebook_version": protocol.get("semantic_codebook_version"),
        "evaluation_protocol_sha256": input_hashes.get("evaluation_protocol", ""),
        "semantic_codebook_sha256": input_hashes.get("semantic_codebook", ""),
        "semantic_quantification": {
            "native_numeric_records": semantic_methods["source_native_numeric"],
            "rule_codebook_records": semantic_methods["rule_codebook"],
            "coding_parse_records": semantic_methods["coding_parse_fallback"],
            "lexicon_zero_hit_records": lexicon_statuses["zero_hit"],
            "coding_candidate_records": coding_statuses["candidate_generated"],
            "coding_unresolved_records": coding_statuses["started_unresolved"],
            "assisted_or_manual_records": semantic_methods["assisted_semantic"] + semantic_methods["manual_code"],
            "auto_eligible_records": review_statuses["auto_eligible"],
            "human_confirmed_records": review_statuses["human_confirmed"],
            "review_required_records": review_statuses["review_required"],
            "low_confidence_records": low_confidence_records,
            "figurative_risk_records": sum(_truth(row.get("figurative_risk")) for row in formal_evidence),
            "protocol_released_at": protocol.get("released_at", ""),
            "release_calibration_mode": protocol.get("calibration", {}).get("mode", ""),
            "release_calibration_source_count": int(protocol.get("calibration", {}).get("source_count", 0)),
            "release_calibration_decision_count": int(protocol.get("calibration", {}).get("decision_count", 0)),
        },
        "fixed_thresholds": scoring_thresholds,
        "formal_scoring_method": formal_scoring,
        "method_disclosure": {
            "evidence_scope": "研究仅使用本次任务中公开可读、能够保存来源定位并可由底层台账复核的网络材料。页面摘要、元数据、聚合统计和直接用户体验分层记录；只有满足地点直接性、内容完整性、来源可靠性、语义置信度与复核状态要求的独立证据单元，才有机会进入正式评分。",
            "data_lifecycle": "数据依次经过原始采集、候选暂存、准入前审计、原子提升、只追加规范台账和派生视图。正式来源与证据保留原始快照哈希、文字定位、查询关系、批次信息和记录哈希；任何无法从现行记录反查冻结原文的候选材料均不得提升为规范证据。",
            "dimension_assignment": "正式评分仅使用唯一 primary_dimension（主评价维度）。同一原始文本若包含多个可独立评价的语义方面，应先切分为具有不同证据编号的独立分析单元；不得依靠多标签把同一计分记录重复纳入多个评价维度。dimension_tags（主题标签）仅用于检索覆盖、主题统计、缺口分析和定性解释，不参与正式评分归属，也不能用于满足逐维证据数量门槛。平台、来源类别和内容层级由规范来源台账确定并由证据继承，不由报告写作者重新填写。",
            "scoring_chain": f"评分候选证据与实际计分证据严格区分。单条记录先通过正式准入规则，再接受同维去重和平台与维度最低样本门槛；平台倾向、跨平台维度分、转换分、固定权重与综合结果均由发布版确定性程序计算，报告不得手工改写机器数值。本次有效评分样本数为{len(scored_rows)}个。原生数值与语义推断分栏处理；评价协议版本和语义代码簿版本均由冻结真值提供，任务期不更新正式规则；双语词表及机器可读近义释义用于透明匹配，词表无法完整识别时启动编码解析并进入受控复核。",
            "missing_score": "当任一维度或平台的数据不足以形成正式结果时，保留缺失状态并说明证据缺口，不以零分、中性值或经验估计强行补齐，也不把缺失维度的既定权重重新分配给其他维度。广义研究材料仍可用于边界清楚的定性解释，但不得冒充实际计分样本。",
            "independent_validation": "正式交付前由独立验收程序从底层证据重新筛选并复算计分样本、正中负数量、平台倾向、维度得分、转换分、加权分和综合结果，同时核验报告、工作簿与机器真值的一致性。生成器输出不能作为验证自身的唯一真值。本次运行只读取发布版评价协议、语义量化代码簿、七维顺序、既定权重和证据门槛；运行期不得根据研究对象、检索结果或期望得分修改正式规则，受保护规则发生未授权变化时必须停止评分与交付。",
        },
        "limitations": {
            "audit_status": evidence_audit.get("status"),
            "sample_target_status": sample_target_status,
            "dimension_gap_targets": (
                audit_summary.get("retrieval_termination", {}).get("remaining_dimensions", [])
                if isinstance(audit_summary.get("retrieval_termination"), dict)
                else audit_summary.get("dimension_evidence_gap_targets", [])
            ),
            "remaining_scored_evidence_gaps": (
                audit_summary.get("retrieval_termination", {}).get("remaining_gaps", {})
                if isinstance(audit_summary.get("retrieval_termination"), dict)
                else {}
            ),
            "retrieval_termination_status": (
                audit_summary.get("retrieval_termination", {}).get("termination_status", "")
                if isinstance(audit_summary.get("retrieval_termination"), dict)
                else ""
            ),
            "restricted_delivery": audit_status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
        },
        "result_layer": {
            "name": RESULT_LAYER_NAME,
            "role": "七个评价维度共同形成的结果层，不是第八个并列维度",
            "has_parallel_weight": False,
            "participates_in_weighting_again": False,
            "required_outputs": list(result_layer.get("required_outputs", [])),
            "evidence_sufficiency": evidence_sufficiency,
            "evaluation_confidence": evaluation_confidence,
        },
        "truth_freeze_sha256": freeze.get("freeze_sha256"),
        "input_hashes": dict(input_hashes),
    }
    payload["report_truth_sha256"] = canonical_sha256(payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    parser.add_argument("--truth-freeze", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--raw-evidence", type=Path, required=True)
    parser.add_argument("--formal-evidence", type=Path, required=True)
    parser.add_argument("--search-log", type=Path, required=True)
    parser.add_argument("--dimension-audit", type=Path, required=True)
    parser.add_argument("--platform-scores", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--codebook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        authorization = authorize_runtime_write(
            state_path=args.state,
            writer_script_id="compile_report_truth.py",
            output_role="report_truth",
            expected_phase="SCORE",
            output_path=args.output,
        )
        task_run_id = str(authorization["task_run_id"])
        freeze = verify_truth_freeze(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            manifest_path=args.truth_freeze,
            require_scoring=True,
        )
        inputs = {
            "source_ledger": args.sources,
            "raw_evidence": args.raw_evidence,
            "formal_evidence": args.formal_evidence,
            "search_log": args.search_log,
            "evidence_audit": args.dimension_audit,
            "platform_scores": args.platform_scores,
            "scoring_output": args.scores,
        }
        for role, path in inputs.items():
            verify_artifact_writer(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_role=role,
                output_path=path,
            )
        input_hashes = {role: sha256_file(path) for role, path in inputs.items()}
        input_hashes["evaluation_protocol"] = sha256_file(args.protocol)
        input_hashes["semantic_codebook"] = sha256_file(args.codebook)
        result = compile_truth(
            task_run_id=task_run_id,
            sources=_read_csv(args.sources),
            raw_evidence=_read_csv(args.raw_evidence),
            formal_evidence=_read_csv(args.formal_evidence),
            search_rows=_read_csv(args.search_log),
            evidence_audit=_read_json(args.dimension_audit),
            platform_scores=_read_json(args.platform_scores),
            scores=_read_json(args.scores),
            protocol=_read_json(args.protocol),
            codebook=_read_json(args.codebook),
            freeze=freeze,
            input_hashes=input_hashes,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="compile_report_truth.py",
            output_role="report_truth",
            output_path=args.output,
            input_paths={**inputs, "truth_freeze": args.truth_freeze, "protocol": args.protocol, "codebook": args.codebook},
            expected_phase="SCORE",
        )
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "valid", "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
