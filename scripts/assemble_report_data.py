#!/usr/bin/env python3
"""Lint qualitative narrative and assemble formal report data with machine truth."""

from __future__ import annotations

import argparse
import copy
import hashlib
import strict_json as json
import os
import re
from pathlib import Path
from typing import Mapping

from artifact_provenance import (
    read_writer_ledger,
    register_protected_artifact,
    verify_artifact_writer,
)
from dimension_framework import DIMENSION_NAMES, DIMENSION_WEIGHTS, RESULT_LAYER_NAME
from runtime_guard import authorize_runtime_write, canonical_sha256


REPORT_NARRATIVE_SCHEMA_VERSION = "report-narrative-1"
REPORT_DATA_SCHEMA_VERSION = "report-data-assembled-1"
MAX_NARRATIVE_GENERATION_ROUNDS = 2
MAX_REPORT_PROMOTIONS = 2

FORBIDDEN_NARRATIVE_KEYS = {
    "page_count",
    "source_count",
    "user_source_count",
    "raw_evidence_units",
    "eligible_evidence_units",
    "scored_evidence_units",
    "scoring_positive_units",
    "scoring_neutral_units",
    "scoring_negative_units",
    "weight",
    "dimension_weights",
    "dimension_weight_formula",
    "targeted_deep_rounds",
    "targeted_round_count",
    "deep_iteration_rounds",
    "deep_iteration_round_count",
    "tendency_score",
    "cross_platform_tendency",
    "conversion_score",
    "weighted_score",
    "composite_score",
    "evaluation_protocol_version",
    "semantic_codebook_version",
    "evaluation_protocol_sha256",
    "semantic_codebook_sha256",
    "fixed_thresholds",
    "formal_scoring_method",
    "initial_research_weight",
    "minimum_evidence_units",
    "distinct_source_pages",
    "source_category_count",
    "dimension_candidate_platform_count",
    "dimension_scorable_platform_count",
    "run_included_platform_count",
    "valid_scoring_platforms",
    "formal_scoring_schema_version",
    "result_layer",
}
DIMENSION_TEXT_FIELDS = {
    "positive",
    "negative",
    "differences",
    "fact_perception",
    "mechanism_analysis",
    "counterevidence",
    "uncertainty_boundary",
    "synthesis_judgment",
    "additional_analysis",
    "themes",
}
RESULT_TEXT_FIELDS = {
    "core_strengths",
    "core_risks",
    "bottom_line_issues",
}
QUALITATIVE_TABLE_FIELDS = {
    "7": {"theme", "meaning", "mention_level", "platform", "consistency"},
    "8": {"theme", "problem", "mention_level", "platform", "consistency"},
    "10": {"group", "perception", "issue", "reliability"},
    "11": {"issue", "long_term", "judgment"},
}
PLATFORM_INTERPRETATION_FIELDS = {"label", "positive", "negative", "bias", "narrative"}
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z_])[+-]?\d+(?:\.\d+)?(?:%|‰)?")


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path.name}")
    return payload


def _walk(value: object, path: str = "$") -> list[tuple[str, object]]:
    items = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            items.extend(_walk(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            items.extend(_walk(child, f"{path}[{index}]"))
    return items


def lint_narrative(narrative: Mapping[str, object], task_run_id: str) -> dict[str, object]:
    errors: list[str] = []
    if narrative.get("schema_version") != REPORT_NARRATIVE_SCHEMA_VERSION:
        errors.append("narrative schema_version is invalid")
    if narrative.get("task_run_id") != task_run_id:
        errors.append("narrative task_run_id is invalid")
    allowed_top = {
        "schema_version",
        "task_run_id",
        "generation_round",
        "sections",
        "dimensions",
        "result_interpretation",
        "qualitative_tables",
        "platform_interpretations",
    }
    unknown_top = sorted(set(narrative) - allowed_top)
    if unknown_top:
        errors.append("narrative contains unknown top-level fields: " + ", ".join(unknown_top))
    generation_round = narrative.get("generation_round")
    if isinstance(generation_round, bool) or not isinstance(generation_round, int) or generation_round not in {1, 2}:
        errors.append("generation_round must be 1 or 2")
    for path, value in _walk(dict(narrative)):
        key = path.rsplit(".", 1)[-1]
        if key in FORBIDDEN_NARRATIVE_KEYS:
            errors.append(f"forbidden machine fact field in narrative: {path}")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and key != "generation_round":
            errors.append(f"numeric machine fact is forbidden in narrative: {path}")
        if isinstance(value, str) and key not in {"schema_version", "task_run_id", "label"} and NUMBER_PATTERN.search(value):
            errors.append(f"numeric expression is forbidden in narrative text: {path}")
    sections = narrative.get("sections")
    if not isinstance(sections, dict) or set(sections) != {str(value) for value in range(1, 16)}:
        errors.append("narrative sections must contain exactly section keys 1 through 15")
    elif any(not isinstance(value, list) for value in sections.values()):
        errors.append("each narrative section must be an array")
    dimensions = narrative.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSION_NAMES):
        errors.append("narrative dimensions must contain exactly the seven canonical dimensions")
    else:
        for name, raw in dimensions.items():
            if not isinstance(raw, dict):
                errors.append(f"narrative dimension must be an object: {name}")
                continue
            unknown = sorted(set(raw) - DIMENSION_TEXT_FIELDS)
            if unknown:
                errors.append(f"narrative dimension contains non-qualitative fields: {name}/{', '.join(unknown)}")
    result = narrative.get("result_interpretation")
    if not isinstance(result, dict):
        errors.append("result_interpretation must be an object")
    else:
        unknown = sorted(set(result) - RESULT_TEXT_FIELDS)
        if unknown:
            errors.append("result_interpretation contains non-qualitative fields: " + ", ".join(unknown))
    tables = narrative.get("qualitative_tables")
    if not isinstance(tables, dict) or set(tables) != set(QUALITATIVE_TABLE_FIELDS):
        errors.append("qualitative_tables must contain exactly sections 7, 8, 10, and 11")
    else:
        for section, rows in tables.items():
            if not isinstance(rows, list):
                errors.append(f"qualitative table {section} must be an array")
                continue
            required = QUALITATIVE_TABLE_FIELDS[section]
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict) or set(row) != required:
                    errors.append(f"qualitative table {section} row {index} has an invalid schema")
    platform_rows = narrative.get("platform_interpretations")
    if not isinstance(platform_rows, list) or len(platform_rows) < 3:
        errors.append("platform_interpretations must contain at least three qualitative source groups")
    else:
        for index, row in enumerate(platform_rows, start=1):
            if not isinstance(row, dict) or set(row) != PLATFORM_INTERPRETATION_FIELDS:
                errors.append(f"platform interpretation row {index} has an invalid schema")
    return {"status": "valid" if not errors else "invalid", "errors": errors, "error_count": len(errors)}


def _joined(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "未记录"
    parts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or item.get("label") or item.get("value") or ""))
        else:
            parts.append(str(item))
    return "、".join(part for part in parts if part) or "未记录"


def _machine_section_tables(
    section_number: int,
    truth: Mapping[str, object],
    narrative: Mapping[str, object],
) -> list[list[list[object]]]:
    groups = truth.get("source_group_summary", [])
    group_rows = [item for item in groups if isinstance(item, dict)] if isinstance(groups, list) else []
    dimensions = [item for item in truth.get("dimensions", []) if isinstance(item, dict)] if isinstance(truth.get("dimensions"), list) else []
    narrative_dimensions = narrative.get("dimensions", {})
    if section_number == 1:
        return [[
            ["研究对象", truth.get("place_name", "")],
            ["检索截止时间", truth.get("retrieval_date", "")],
            ["研究任务编号", truth.get("task_run_id", "")],
            ["评价框架", "七个评价维度与一个综合结果层"],
            ["评价协议", truth.get("evaluation_protocol_version", "")],
            ["语义代码簿", truth.get("semantic_codebook_version", "")],
            ["正式证据边界", "仅纳入可追溯、可定位并通过准入审计的公开网络证据"],
            ["事实生成路径", "源内容快照、候选准入、正式派生、整体冻结与只读验收"],
        ]]
    if section_number == 2:
        summary = [
            ["去重相关网页", truth["page_count"]],
            ["正式来源数", truth["source_count"]],
            ["用户来源数", truth["user_source_count"]],
            ["原始证据单元", truth["raw_evidence_units"]],
            ["评分候选证据", truth["eligible_evidence_units"]],
            ["实际计分证据", truth["scored_evidence_units"]],
            ["正向计分证据", truth["scoring_positive_units"]],
            ["中性计分证据", truth["scoring_neutral_units"]],
            ["负向计分证据", truth["scoring_negative_units"]],
            ["定向深检轮数", truth["deep_iteration_round_count"]],
            ["观察到的平台数", len(truth.get("observed_platforms", []))],
        ]
        access = [
            [item.get("label", ""), _joined(item.get("access_statuses")), "按实际访问状态纳入或排除"]
            for item in group_rows[: max(5, len(group_rows))]
        ]
        return [summary, access]
    if section_number == 3:
        rows = [
            [
                item.get("label", ""),
                item.get("source_count", 0),
                _joined(item.get("content_types")),
                _joined(item.get("time_range")),
                "是" if int(item.get("used_for_scoring_source_count", 0)) > 0 else "否",
                "用于补充来源类型、主题或正式评分证据",
                "受公开可见内容与样本结构限制",
            ]
            for item in group_rows[: max(8, len(group_rows))]
        ]
        return [rows]
    if section_number == 4:
        return [[
            [
                "综合分",
                "数据不足" if truth.get("composite_score") is None else truth.get("composite_score"),
                "由七维固定初始研究权重计算",
            ],
            ["实际计分证据", truth["scored_evidence_units"], "来自正式计分集合"],
            ["评分候选证据", truth["eligible_evidence_units"], "通过单条正式准入规则"],
            ["正向计分证据", truth["scoring_positive_units"], "与正式计分集合一致"],
            ["中性计分证据", truth["scoring_neutral_units"], "与正式计分集合一致"],
            ["负向计分证据", truth["scoring_negative_units"], "与正式计分集合一致"],
            ["评价置信度", truth.get("result_layer", {}).get("evaluation_confidence", ""), "取七维正式评分置信度的最低层级"],
            ["证据充分度", truth.get("result_layer", {}).get("evidence_sufficiency", ""), "由正式证据审计状态生成"],
        ]]
    if section_number == 6:
        rows = []
        for item in dimensions:
            name = str(item.get("name", ""))
            narrative_item = narrative_dimensions.get(name, {}) if isinstance(narrative_dimensions, dict) else {}
            positive = narrative_item.get("positive", []) if isinstance(narrative_item, dict) else []
            negative = narrative_item.get("negative", []) if isinstance(narrative_item, dict) else []
            rows.append([
                name,
                f"{float(item.get('weight', 0)) * 100:.0f}%",
                _joined(positive),
                _joined(negative),
                item.get("mention_level", ""),
                "数据不足" if item.get("cross_platform_tendency") is None else item.get("cross_platform_tendency"),
                "数据不足" if item.get("conversion_score") is None else item.get("conversion_score"),
                item.get("scoring_confidence", ""),
                _joined([source.get("label", "") for source in item.get("representative_sources", []) if isinstance(source, dict)]),
            ])
        return [rows]
    qualitative = narrative.get("qualitative_tables", {})
    table_rows = qualitative.get(str(section_number), []) if isinstance(qualitative, dict) else []
    if section_number == 7:
        return [[
            [index, row["theme"], row["meaning"], row["mention_level"], "按定性证据判断", row["platform"], row["consistency"]]
            for index, row in enumerate(table_rows, start=1)
            if isinstance(row, dict)
        ]]
    if section_number == 8:
        return [[
            [index, row["theme"], row["problem"], row["mention_level"], "按定性证据判断", row["platform"], row["consistency"]]
            for index, row in enumerate(table_rows, start=1)
            if isinstance(row, dict)
        ]]
    if section_number == 9:
        source_counts = {str(item.get("label", "")): int(item.get("source_count", 0)) for item in group_rows}
        interpretations = narrative.get("platform_interpretations", [])
        return [[
            [
                row["label"], row["positive"], row["negative"], "不单独计算",
                "存在正式来源" if source_counts.get(str(row["label"]), 0) else "未形成独立正式来源组",
                row["bias"],
            ]
            for row in interpretations
            if isinstance(row, dict)
        ]]
    if section_number == 10:
        return [[[row["group"], row["perception"], row["issue"], row["reliability"]] for row in table_rows if isinstance(row, dict)]]
    if section_number == 11:
        return [[[row["issue"], row["long_term"], row["judgment"]] for row in table_rows if isinstance(row, dict)]]
    return []


def _fill_sections(template: list[object], narrative: Mapping[str, object], truth: Mapping[str, object]) -> list[object]:
    narrative_sections = narrative.get("sections")
    provided = narrative_sections if isinstance(narrative_sections, dict) else {}
    result = copy.deepcopy(template)
    for section in result:
        if not isinstance(section, dict):
            continue
        number = int(section.get("number", 0))
        supplied = provided.get(str(number), []) if isinstance(provided, dict) else []
        if not isinstance(supplied, list):
            raise ValueError(f"narrative section {number} must be an array")
        text_blocks = [item for item in supplied if isinstance(item, dict) and item.get("type") == "paragraph"]
        text_index = 0
        machine_tables = _machine_section_tables(number, truth, narrative)
        table_index = 0
        platform_rows = narrative.get("platform_interpretations", [])
        platform_index = 0
        for block in section.get("blocks", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "paragraph" and text_index < len(text_blocks):
                supplied_block = text_blocks[text_index]
                text_index += 1
                block["text"] = str(supplied_block.get("text", ""))
                if block.get("type") == "paragraph":
                    block["links"] = []
            if block.get("type") == "subheading3" and isinstance(platform_rows, list) and platform_index < len(platform_rows):
                row = platform_rows[platform_index]
                if isinstance(row, dict):
                    block["text"] = str(row.get("label", ""))
                platform_index += 1
            if block.get("type") == "table" and table_index < len(machine_tables):
                block["rows"] = machine_tables[table_index]
                table_index += 1
            if block.get("type") == "judgments":
                interpretation = narrative.get("result_interpretation", {})
                result_truth = truth.get("result_layer", {})
                if not isinstance(interpretation, dict) or not isinstance(result_truth, dict):
                    raise ValueError("result interpretation or machine result layer is invalid")
                values = {
                    "综合分": truth.get("composite_score") if truth.get("composite_score") is not None else "数据不足",
                    "核心优势": _joined(interpretation.get("core_strengths", [])),
                    "核心风险": _joined(interpretation.get("core_risks", [])),
                    "底线性问题": _joined(interpretation.get("bottom_line_issues", [])),
                    "证据充分度": result_truth.get("evidence_sufficiency", ""),
                    "评价置信度": result_truth.get("evaluation_confidence", ""),
                }
                items = block.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item["value"] = values.get(str(item.get("label", "")), "")
        if text_index != len(text_blocks):
            raise ValueError(f"narrative section {number} has more text blocks than the fixed schema")
    return result


def assemble_report_data(
    *,
    report_truth: Mapping[str, object],
    narrative: Mapping[str, object],
    template: Mapping[str, object],
) -> dict[str, object]:
    if report_truth.get("status") != "valid":
        raise ValueError("report truth is not valid")
    task_run_id = str(report_truth.get("task_run_id", ""))
    lint = lint_narrative(narrative, task_run_id)
    if lint["status"] != "valid":
        raise ValueError("; ".join(str(item) for item in lint["errors"]))
    source_groups = report_truth.get("source_group_summary", [])
    allowed_group_labels = {
        str(item.get("label", ""))
        for item in source_groups
        if isinstance(item, dict) and item.get("label")
    } if isinstance(source_groups, list) else set()
    platform_rows = narrative.get("platform_interpretations", [])
    labels = [str(item.get("label", "")) for item in platform_rows if isinstance(item, dict)]
    if len(labels) != len(set(labels)) or any(label not in allowed_group_labels for label in labels):
        raise ValueError("platform interpretation labels must be unique machine-truth source groups")
    data = copy.deepcopy(dict(template))
    data["schema_version"] = REPORT_DATA_SCHEMA_VERSION
    data["title"] = str(report_truth.get("place_name", "")) + "历史文化活力分析报告"
    data["retrieval_date"] = str(report_truth.get("retrieval_date", ""))
    data["dimension_weight_scheme"] = report_truth["dimension_weight_scheme"]
    data["dimension_weights"] = copy.deepcopy(report_truth["dimension_weights"])
    disclosure = report_truth.get("method_disclosure", {})
    data["research_scope"] = [
        {"text": str(value), "links": []}
        for value in disclosure.values()
    ] if isinstance(disclosure, dict) else []
    minimum_effective_sample_target = report_truth.get("minimum_effective_sample_target")
    if not isinstance(minimum_effective_sample_target, int) or minimum_effective_sample_target < 100:
        raise ValueError("report truth minimum effective sample target is invalid")
    data["research_run"] = {
        "task_run_id": task_run_id,
        "minimum_effective_samples": minimum_effective_sample_target,
        "legacy_minimum_effective_sample_floor": report_truth.get(
            "legacy_minimum_effective_sample_floor", 100
        ),
        "target_confidence": report_truth.get("target_confidence", ""),
        "actual_effective_samples": report_truth["scored_evidence_units"],
        "eligible_evidence_units": report_truth["eligible_evidence_units"],
        "scored_evidence_units": report_truth["scored_evidence_units"],
        "formal_scoring_schema_version": report_truth.get("formal_scoring_method", {}).get("schema_version", ""),
        "deep_iteration_rounds": report_truth["deep_iteration_round_count"],
        "targeted_deep_rounds": copy.deepcopy(report_truth["deep_iteration_rounds"]),
        "sample_target_status": report_truth.get("limitations", {}).get("sample_target_status", ""),
        "semantic_quantification": {
            **copy.deepcopy(report_truth.get("semantic_quantification", {})),
            "evaluation_protocol_version": report_truth["evaluation_protocol_version"],
            "semantic_codebook_version": report_truth["semantic_codebook_version"],
            "semantic_codebook_sha256": report_truth["semantic_codebook_sha256"],
            "task_rule_refresh_performed": False,
            "task_rule_mutation_performed": False,
            "semantic_rules_workbook": "非量化文本量化评价规则.xlsx",
            "agreement_summary": "按发布版复核与一致性规则执行",
        },
    }
    sections = data.get("sections")
    if not isinstance(sections, list):
        raise ValueError("report template sections are invalid")
    data["sections"] = _fill_sections(sections, narrative, report_truth)
    truth_dimensions = report_truth.get("dimensions")
    template_dimensions = data.get("dimension_analyses")
    narrative_dimensions = narrative.get("dimensions")
    if not isinstance(truth_dimensions, list) or not isinstance(template_dimensions, list) or not isinstance(narrative_dimensions, dict):
        raise ValueError("dimension report schema is invalid")
    template_by_name = {str(item.get("name", "")): item for item in template_dimensions if isinstance(item, dict)}
    assembled_dimensions: list[dict[str, object]] = []
    for truth_item in truth_dimensions:
        if not isinstance(truth_item, dict):
            raise ValueError("report truth dimension must be an object")
        name = str(truth_item["name"])
        item = copy.deepcopy(template_by_name[name])
        narrative_item = narrative_dimensions[name]
        assert isinstance(narrative_item, dict)
        for field in DIMENSION_TEXT_FIELDS:
            item[field] = copy.deepcopy(narrative_item.get(field, []))
        item.update(
            {
                "number": truth_item["number"],
                "name": name,
                "initial_research_weight": truth_item["weight"],
                "tendency_score": truth_item["cross_platform_tendency"],
                "conversion_score": truth_item["conversion_score"],
                "weighted_score": truth_item["weighted_score"],
                "evidence_strength": truth_item["scoring_confidence"],
                "retrieved_evidence_units": truth_item["retrieved_evidence_units"],
                "topic_coverage_evidence_units": truth_item["topic_coverage_evidence_units"],
                "eligible_evidence_units": truth_item["eligible_evidence_units"],
                "scored_evidence_units": truth_item["scored_evidence_units"],
                "evidence_units": truth_item["scored_evidence_units"],
                "scoring_positive_units": truth_item["scoring_positive_units"],
                "scoring_neutral_units": truth_item["scoring_neutral_units"],
                "scoring_negative_units": truth_item["scoring_negative_units"],
                "valid_scoring_platforms": truth_item["dimension_scorable_platform_count"],
                "dimension_candidate_platform_count": truth_item["dimension_candidate_platform_count"],
                "dimension_scorable_platform_count": truth_item["dimension_scorable_platform_count"],
                "run_included_platform_count": truth_item["run_included_platform_count"],
                "platform_minimum_rule_status": truth_item["platform_minimum_rule_status"],
                "distinct_source_pages": truth_item["distinct_source_pages"],
                "source_category_count": truth_item["source_category_count"],
                "dimension_confidence": truth_item["scoring_confidence"],
                "evidence_status": truth_item["evidence_status"],
                "numeric_score_permitted": truth_item["numeric_score_permitted"],
                "target_confidence": truth_item["target_confidence"],
                "retrieval_termination_status": truth_item["retrieval_termination_status"],
                "retrieval_termination_scope": copy.deepcopy(
                    truth_item["retrieval_termination_scope"]
                ),
                "remaining_scored_evidence_gap": truth_item[
                    "remaining_scored_evidence_gap"
                ],
                "targeted_deep_rounds": copy.deepcopy(truth_item["targeted_deep_rounds"]),
                "targeted_round_count": truth_item["targeted_round_count"],
                "mention_level": truth_item["mention_level"],
                "mention_range": truth_item["mention_range"],
                "mention_basis": truth_item["mention_basis"],
                "representative_sources": copy.deepcopy(truth_item["representative_sources"]),
                "medium_enhancement_rounds": copy.deepcopy(truth_item["medium_enhancement_rounds"]),
                "medium_completion_audit_passed": truth_item["medium_completion_audit_passed"],
                "medium_completion_basis": truth_item["medium_completion_basis"],
                "independent_exhaustion_audit_passed": truth_item["independent_exhaustion_audit_passed"],
                "exhaustion_basis": truth_item["exhaustion_basis"],
            }
        )
        assembled_dimensions.append(item)
    data["dimension_analyses"] = assembled_dimensions
    interpretation = narrative["result_interpretation"]
    assert isinstance(interpretation, dict)
    data["result_layer"] = {
        "name": RESULT_LAYER_NAME,
        "composite_score": report_truth.get("composite_score"),
        "core_strengths": copy.deepcopy(interpretation.get("core_strengths", [])),
        "core_risks": copy.deepcopy(interpretation.get("core_risks", [])),
        "bottom_line_issues": copy.deepcopy(interpretation.get("bottom_line_issues", [])),
        "evidence_sufficiency": str(report_truth.get("result_layer", {}).get("evidence_sufficiency", "")),
        "evaluation_confidence": str(report_truth.get("result_layer", {}).get("evaluation_confidence", "")),
        "bottom_line_override_checked": True,
    }
    scored_total = int(report_truth["scored_evidence_units"])
    direction_shares = [
        (
            round(float(report_truth[field]) / scored_total * 100, 1)
            if scored_total
            else 0.0
        )
        for field in (
            "scoring_positive_units",
            "scoring_neutral_units",
            "scoring_negative_units",
        )
    ]
    dimension_scores = [
        item.get("conversion_score") if isinstance(item, dict) else None
        for item in report_truth.get("dimensions", [])
    ]
    composite = report_truth.get("composite_score")
    sensitivity = report_truth.get("sample_weighted_sensitivity_score")
    result_truth = report_truth.get("result_layer", {})
    result_truth = result_truth if isinstance(result_truth, dict) else {}
    data["machine_readable_line"] = "｜".join(
        [
            str(report_truth.get("place_name", "")),
            str(report_truth["source_count"]),
            str(scored_total),
            *(f"{value:.1f}%" for value in direction_shares),
            *("数据不足" if value is None else f"{float(value):.1f}" for value in dimension_scores),
            "数据不足" if composite is None else f"{float(composite):.1f}",
            str(result_truth.get("evidence_sufficiency", "")),
            "数据不足" if composite is None else f"{float(composite):.1f}",
            "数据不足" if sensitivity is None else f"{float(sensitivity):.1f}",
            "数据不足" if composite is None else f"{float(composite):.1f}",
            str(result_truth.get("evaluation_confidence", "")),
        ]
    )
    data["report_truth_sha256"] = report_truth["report_truth_sha256"]
    data["report_narrative_sha256"] = canonical_sha256(dict(narrative))
    data["report_data_sha256"] = canonical_sha256(data)
    return data


def _unique_role_hashes(ledger: Path, task_run_id: str, role: str) -> set[str]:
    return {
        str(item.get("output_sha256", ""))
        for item in read_writer_ledger(ledger, task_run_id=task_run_id)
        if item.get("output_role") == role
    }


def _serialized_sha256(payload: Mapping[str, object]) -> str:
    text = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    parser.add_argument("--report-truth", type=Path, required=True)
    parser.add_argument("--narrative-input", type=Path, required=True)
    parser.add_argument("--narrative-output", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        authorization = authorize_runtime_write(
            state_path=args.state,
            writer_script_id="assemble_report_data.py",
            output_role="report_data",
            expected_phase="REPORT_BUILD",
            output_path=args.output,
        )
        task_run_id = str(authorization["task_run_id"])
        verify_artifact_writer(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            output_role="report_truth",
            output_path=args.report_truth,
        )
        truth = _read_json(args.report_truth)
        narrative = _read_json(args.narrative_input)
        template = _read_json(args.template)
        lint = lint_narrative(narrative, task_run_id)
        if lint["status"] != "valid":
            raise ValueError("; ".join(str(item) for item in lint["errors"]))
        narrative_hashes = _unique_role_hashes(args.writer_ledger, task_run_id, "report_narrative")
        report_hashes = _unique_role_hashes(args.writer_ledger, task_run_id, "report_data")
        candidate_narrative_hash = _serialized_sha256(narrative)
        if candidate_narrative_hash not in narrative_hashes and len(narrative_hashes) >= MAX_NARRATIVE_GENERATION_ROUNDS:
            raise ValueError("report_narrative_nonconvergent: narrative generation rounds exceeded")
        result = assemble_report_data(report_truth=truth, narrative=narrative, template=template)
        from build_docx_report import validate_report_data

        validate_report_data(result)
        candidate_report_hash = _serialized_sha256(result)
        if candidate_report_hash not in report_hashes and len(report_hashes) >= MAX_REPORT_PROMOTIONS:
            raise ValueError("report_narrative_nonconvergent: report promotions exceeded")
        args.narrative_output.parent.mkdir(parents=True, exist_ok=True)
        normalized_narrative = json.dumps(narrative, ensure_ascii=False, indent=2) + "\n"
        narrative_tmp = args.narrative_output.with_name(args.narrative_output.name + ".tmp")
        narrative_tmp.write_text(normalized_narrative, encoding="utf-8")
        os.replace(narrative_tmp, args.narrative_output)
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="assemble_report_data.py",
            output_role="report_narrative",
            output_path=args.narrative_output,
            input_paths={"narrative_input": args.narrative_input, "report_truth": args.report_truth},
            expected_phase="REPORT_BUILD",
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="assemble_report_data.py",
            output_role="report_data",
            output_path=args.output,
            input_paths={
                "report_truth": args.report_truth,
                "report_narrative": args.narrative_output,
                "report_template": args.template,
            },
            expected_phase="REPORT_BUILD",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "valid", "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
