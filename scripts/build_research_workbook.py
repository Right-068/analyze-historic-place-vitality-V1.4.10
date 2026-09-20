#!/usr/bin/env python3
"""Build the seven-sheet traceability workbook for a historic-place study."""

from __future__ import annotations

import argparse
import csv
import strict_json as json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from spreadsheet_safety import safe_hyperlink

from dimension_framework import DIMENSION_WEIGHTS, RESULT_LAYER_NAME, WEIGHT_SCHEME_NAME
from formal_scoring import (
    annotate_rows,
    build_formal_scoring_chain,
    load_protocol,
    scoring_direction,
    scoring_value,
)
from excel_localization import display_value
from retrieval_controls import executed_query_metrics, executed_search_rows, research_target_summary
from runtime_guard import verify_scoring_input_gate

try:
    import xlsxwriter
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "xlsxwriter is required to build the XLSX deliverable. Install it in the current code runtime."
    ) from exc

from artifact_provenance import (
    assert_frozen_artifact_path,
    register_protected_artifact,
    verify_artifact_writer,
    verify_preflight,
    verify_truth_freeze,
)
from runtime_guard import authorize_runtime_write


SHEET_NAMES = [
    "样本明细",
    "来源页面",
    "主题编码",
    "七维评分",
    "检索日志",
    "数据限制说明",
    "机器可读汇总",
]

SOURCE_COLUMNS = [
    ("内部来源编号", "source_id", 14),
    ("地点名称", "place_name", 20),
    ("实体层级", "entity_level", 14),
    ("平台或网站", "platform", 20),
    ("域名", "domain", 24),
    ("来源类型", "source_category", 20),
    ("页面标题", "page_title", 42),
    ("发布时间", "published_at", 16),
    ("检索日期", "retrieved_at", 16),
    ("页面URL", "url", 48),
    ("检索记录编号", "query_id", 14),
    ("实际检索词", "query_text", 36),
    ("结果排序", "result_rank", 10),
    ("读取状态", "access_status", 16),
    ("内容层级", "content_layer", 18),
    ("选择机制", "selection_mechanism", 20),
    ("是否相关", "is_relevant", 12),
    ("是否用户来源", "is_user_source", 14),
    ("是否疑似推广", "suspected_promotion", 14),
    ("推广判断依据", "promotion_basis", 34),
    ("是否纳入量化评分", "used_for_scoring", 16),
    ("评分范围", "score_scope", 18),
    ("去重组", "dedup_group", 18),
    ("原始来源编号", "original_source_id", 16),
    ("证据定位", "evidence_locator", 34),
    ("备注", "notes", 34),
    ("任务运行编号", "task_run_id", 24),
]

SAMPLE_HEADERS = [
    "内部样本编号",
    "所属来源页面编号",
    "页面标题",
    "发布时间/评论时间",
    "作者类型",
    "内容类型",
    "内容长度类别",
    "是否明确到访/体验",
    "地点相关性",
    "是否疑似推广",
    "疑似推广判断依据",
    "是否有效样本",
    "有效性判断说明",
    "情感倾向",
    "摘要原文",
    "必要摘录/匿名化摘要",
    "规范化评价主题",
    "主题维度标签",
    "主要维度",
    "次要维度",
    "证据类型",
    "时间属性",
    "来源链接",
    "来源类型",
    "内容层级",
    "是否纳入量化评分",
    "情感分值",
    "互动点赞数",
    "页面显示互动/评论总数",
    "时间字段是否完整",
    "原生评分值",
    "原生量表下界",
    "原生量表上界",
    "原生评分标准化值",
    "语义量化方法",
    "语义语言",
    "语义分析单位",
    "语义分句数",
    "词表命中状态",
    "词表命中轨迹",
    "编码解析状态",
    "编码解析候选",
    "编码解析轨迹",
    "开放编码编号",
    "维度判定依据",
    "维度判定置信度",
    "维度规则命中",
    "极性判定依据",
    "否定命中",
    "程度命中",
    "转折命中",
    "弱化命中",
    "反讽或修辞风险",
    "语义连续分",
    "语义整数分",
    "语义置信度",
    "证据可靠性",
    "平台内聚合权重",
    "语义复核状态",
    "正式评分候选资格",
    "正式评分排除原因",
    "正式评分去重键",
    "平台样本状态",
    "是否实际进入平台评分",
    "正式计分方向",
    "语义规则轨迹",
    "编码者编号",
    "裁决状态",
    "备注",
    "任务运行编号",
]

_SAMPLE_WIDE_COLUMNS = {
    "页面标题": 34,
    "疑似推广判断依据": 34,
    "有效性判断说明": 34,
    "摘要原文": 56,
    "必要摘录/匿名化摘要": 48,
    "来源链接": 48,
    "语义分析单位": 52,
    "词表命中轨迹": 42,
    "编码解析候选": 42,
    "编码解析轨迹": 42,
    "维度规则命中": 42,
    "正式评分排除原因": 44,
    "语义规则轨迹": 58,
    "备注": 34,
    "任务运行编号": 24,
}
SAMPLE_WIDTHS = [_SAMPLE_WIDE_COLUMNS.get(header, 16) for header in SAMPLE_HEADERS]

THEME_HEADERS = [
    "主题编码",
    "规范化评价主题",
    "归属维度",
    "方向",
    "定义/适用含义",
    "全部有效提及数",
    "量化样本提及数",
    "正面",
    "中性",
    "负面",
    "混合",
    "有效内容占比",
    "代表性匿名摘录",
]

SCORE_HEADERS = [
    "序号",
    "维度",
    "初始研究权重",
    "评分证据状态",
    "实际权重",
    "检索有效证据数",
    "主题覆盖证据数",
    "评分候选证据数",
    "实际计分证据数",
    "计分正面数",
    "计分中性数",
    "计分负面数",
    "有效计分平台数",
    "维度候选平台数",
    "维度可计分平台数",
    "本次纳入平台数",
    "平台最低样本规则状态",
    "跨平台等权倾向值(-5~+5)",
    "跨平台等权维度得分(0~100)",
    "加权得分",
    "置信度",
    "判断依据",
    "提及比例",
    "提及等级",
    "维度（图表）",
    "得分（图表）",
    "图表数据状态",
    "备注",
    "最低有效证据门槛",
    "独立来源页数",
    "最低来源页门槛",
    "来源类型数",
    "最低来源类型门槛",
    "定向深检轮数",
    "最低置信度要求",
    "缺口或穷尽说明",
    "优先置信度目标",
    "中置信度终止审计",
    "中置信度终止依据",
]

SEARCH_HEADERS = [
    "任务运行编号",
    "检索记录编号",
    "迭代轮次",
    "缺口目标",
    "关联维度目标",
    "迭代模式",
    "本轮前维度置信度",
    "检索日期",
    "检索工具或入口",
    "实际检索词",
    "目标来源类型",
    "返回结果数量",
    "实际打开页面数",
    "相关页面数",
    "重复页面数",
    "本次新增实际计分证据",
    "累计实际计分证据",
    "是否触发登录",
    "是否触发访问限制",
    "检索状态",
    "下一动作",
    "备注",
    "记录类型",
    "计划编号",
    "执行编号",
    "查询编号",
    "执行开始时间",
    "执行结束时间",
    "规范查询意图",
    "重试序号",
    "重试原因",
    "原执行编号",
    "重试间隔策略",
    "目标平台",
    "检索路径",
    "正负路径",
    "目标主体",
    "目标时间情境",
    "查询语言",
]

SEARCH_KEYS = [
    "task_run_id",
    "search_id",
    "iteration_round",
    "gap_target",
    "query_dimension_targets",
    "iteration_mode",
    "dimension_confidence_before_round",
    "retrieved_at",
    "search_tool",
    "query",
    "source_category_target",
    "returned_results",
    "opened_pages",
    "relevant_pages",
    "duplicate_pages",
    "new_scored_evidence_units",
    "cumulative_scored_evidence_units",
    "login_triggered",
    "restriction_triggered",
    "status",
    "next_action",
    "notes",
    "record_type",
    "plan_id",
    "execution_id",
    "query_id",
    "started_at",
    "finished_at",
    "normalized_query_intent",
    "retry_number",
    "retry_reason",
    "original_execution_id",
    "retry_interval_policy",
    "target_platform_id",
    "retrieval_route",
    "polarity",
    "target_subject",
    "target_time_range",
    "language",
]

LIMITATION_HEADERS = ["限制主题", "具体说明", "处理方式", "对解释的影响"]
LIMITATION_KEYS = ["limitation_topic", "details", "handling", "interpretive_impact"]

MACHINE_HEADERS = [
    "project_place",
    "retrieval_date",
    "search_query_rows",
    "source_rows",
    "deduplicated_relevant_pages",
    "minimum_page_target",
    "minimum_page_met",
    "readable_source_pages",
    "snippet_or_metadata_pages",
    "inaccessible_pages",
    "represented_source_categories",
    "attempted_source_categories",
    "user_source_pages",
    "evidence_rows",
    "valid_evidence_rows",
    "scoring_records",
    "positive_count",
    "positive_rate",
    "neutral_count",
    "neutral_rate",
    "negative_count",
    "negative_rate",
    "mixed_count",
    "mixed_rate",
    "score_history_culture",
    "score_local_cultural_character",
    "score_living_cultural_continuity",
    "score_cultural_practice_experience",
    "score_place_atmosphere_experience",
    "score_conservation_adaptive_reuse",
    "score_carrying_governance_experience",
    "result_layer_name",
    "platform_equal_score",
    "sample_weighted_score",
    "recommended_final_score",
    "confidence",
    "time_range",
    "scope_rule",
    "default_platform_rule",
    "complete_comment_claim",
    "privacy_rule",
    "task_run_id",
    "minimum_effective_sample_target",
    "minimum_effective_sample_met",
    "effective_sample_shortfall",
    "deep_iteration_rounds",
    "iteration_outcome",
    "native_numeric_rows",
    "semantic_rule_rows",
    "semantic_coding_parse_rows",
    "semantic_assisted_rows",
    "semantic_manual_rows",
    "semantic_auto_eligible_rows",
    "semantic_human_confirmed_rows",
    "semantic_review_required_rows",
    "semantic_low_confidence_rows",
    "semantic_figurative_risk_rows",
    "semantic_lexicon_zero_hit_rows",
    "semantic_coding_candidate_rows",
    "semantic_coding_unresolved_rows",
    "semantic_mean_confidence",
    "semantic_mean_evidence_reliability",
    "minimum_dimension_confidence",
    "dimensions_high_confidence",
    "dimensions_medium_high_confidence",
    "dimensions_medium_confidence",
    "dimensions_exhausted_low_confidence",
    "dimensions_needing_iteration",
    "dimension_enhancement_targets",
    "dimension_gap_targets",
    "dimension_evidence_status_json",
    "formal_scoring_schema_version",
    "eligible_evidence_units",
    "scored_evidence_units",
    "scored_positive_units",
    "scored_neutral_units",
    "scored_negative_units",
    "dimension_scoring_summary_json",
    "planned_or_nonexecuted_search_rows",
    "unique_executed_query_intents",
    "legacy_scored_evidence_floor",
    "target_confidence",
    "per_dimension_scored_evidence_target",
    "theoretical_minimum_scored_evidence",
    "retrieval_termination_status",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def read_json(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def truthy(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "y", "是"}


def unique_key(row: dict[str, str]) -> str:
    return row.get("dedup_group") or row.get("url") or row.get("source_id", "")


def safe_number(value: object) -> int | float | None:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        from input_safety import finite_number
        number = finite_number(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def add_formats(workbook: "xlsxwriter.Workbook") -> dict[str, object]:
    return {
        "header": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#17324D",
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "body": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "valign": "top",
                "text_wrap": True,
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "center": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "integer": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "num_format": "0",
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "decimal": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "num_format": "0.0",
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "decimal2": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "num_format": "0.00",
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "percent": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "num_format": "0.0%",
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "link": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "font_color": "#0563C1",
                "underline": True,
                "valign": "top",
                "text_wrap": True,
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
        "note": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "font_color": "#666666",
                "bg_color": "#F2F2F2",
                "valign": "top",
                "text_wrap": True,
                "border": 1,
                "border_color": "#D9E2F3",
            }
        ),
    }


def prepare_sheet(worksheet: object) -> None:
    worksheet.hide_gridlines(2)
    worksheet.set_row(0, 34)
    worksheet.set_landscape()
    worksheet.fit_to_pages(1, 0)
    worksheet.set_margins(0.35, 0.35, 0.5, 0.5)


def write_cell(worksheet: object, row: int, column: int, value: object, formats: dict[str, object], hyperlink: bool = False) -> None:
    number = safe_number(value)
    if hyperlink and isinstance(value, str) and safe_hyperlink(value):
        worksheet.write_url(row, column, value, formats["link"], string=value)
    elif number is not None and not isinstance(value, str):
        worksheet.write_number(row, column, number, formats["body"])
    else:
        worksheet.write_string(row, column, "" if value is None else str(value), formats["body"])


def add_table(
    worksheet: object,
    headers: list[str],
    rows: list[list[object]],
    widths: list[float],
    name: str,
    formats: dict[str, object],
    hyperlink_columns: set[int] | None = None,
) -> None:
    prepare_sheet(worksheet)
    hyperlink_columns = hyperlink_columns or set()
    for column, width in enumerate(widths):
        worksheet.set_column(column, column, width)
    for column, header in enumerate(headers):
        worksheet.write(0, column, header, formats["header"])
    displayed_rows = rows if rows else [[""] * len(headers)]
    for row_index, values in enumerate(displayed_rows, start=1):
        worksheet.set_row(row_index, 25.5)
        for column, value in enumerate(values):
            write_cell(
                worksheet,
                row_index,
                column,
                display_value(headers[column], value),
                formats,
                hyperlink=column in hyperlink_columns,
            )
    worksheet.add_table(
        0,
        0,
        len(displayed_rows),
        len(headers) - 1,
        {
            "name": name,
            "style": "Table Style Medium 2",
            "columns": [{"header": header, "header_format": formats["header"]} for header in headers],
        },
    )


def build_sample_rows(evidence: list[dict[str, str]], sources: list[dict[str, str]]) -> list[list[object]]:
    source_lookup = {row.get("source_id", ""): row for row in sources}
    rows: list[list[object]] = []
    for item in evidence:
        source = source_lookup.get(item.get("source_id", ""), {})
        rows.append(
            [
                item.get("evidence_id", ""),
                item.get("source_id", ""),
                source.get("page_title", ""),
                item.get("published_at", ""),
                item.get("user_group", ""),
                item.get("unit_type", ""),
                item.get("content_length_category", ""),
                item.get("explicit_visit", ""),
                item.get("place_relevance", ""),
                source.get("suspected_promotion", ""),
                source.get("promotion_basis", ""),
                item.get("is_valid", ""),
                item.get("validity_reason", ""),
                item.get("sentiment", ""),
                item.get("original_summary_text", ""),
                item.get("excerpt_or_summary", ""),
                item.get("normalized_theme", ""),
                item.get("dimension_tags", ""),
                item.get("primary_dimension", ""),
                item.get("secondary_dimension", ""),
                item.get("evidence_type", ""),
                item.get("time_context", ""),
                source.get("url", ""),
                source.get("source_category", ""),
                item.get("unit_type", ""),
                item.get("used_for_scoring", ""),
                item.get("sentiment_score", ""),
                item.get("interaction_count", ""),
                item.get("displayed_total_count", ""),
                item.get("time_complete", ""),
                item.get("native_rating_value", ""),
                item.get("native_rating_scale_min", ""),
                item.get("native_rating_scale_max", ""),
                item.get("native_rating_normalized", ""),
                item.get("semantic_method", ""),
                item.get("semantic_language", ""),
                item.get("semantic_unit_text", ""),
                item.get("semantic_clause_count", ""),
                item.get("lexicon_match_status", ""),
                item.get("lexicon_match_trace", ""),
                item.get("coding_parse_status", ""),
                item.get("coding_parse_candidates", ""),
                item.get("coding_parse_trace", ""),
                item.get("open_code_id", ""),
                item.get("aspect_assignment_basis", ""),
                item.get("aspect_confidence", ""),
                item.get("dimension_rule_hits", ""),
                item.get("polarity_basis", ""),
                item.get("negation_hits", ""),
                item.get("degree_hits", ""),
                item.get("contrast_hits", ""),
                item.get("hedge_hits", ""),
                item.get("figurative_risk", ""),
                item.get("semantic_score_raw", ""),
                item.get("semantic_score_final", ""),
                item.get("semantic_confidence", ""),
                item.get("evidence_reliability", ""),
                item.get("aggregation_weight", ""),
                item.get("semantic_review_status", ""),
                item.get("formal_scoring_eligible", ""),
                item.get("formal_scoring_exclusion_reasons", ""),
                item.get("formal_scoring_dedup_key", ""),
                item.get("platform_sample_status", ""),
                item.get("included_in_platform_score", ""),
                item.get("scoring_direction", ""),
                item.get("semantic_rule_trace", ""),
                item.get("coder_id", ""),
                item.get("adjudication_status", ""),
                item.get("notes", ""),
                item.get("task_run_id", ""),
            ]
        )
    return rows


def build_theme_records(evidence: list[dict[str, str]]) -> list[dict[str, object]]:
    grouped: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for item in evidence:
        if not truthy(item.get("is_valid", "")):
            continue
        theme = item.get("normalized_theme") or item.get("primary_dimension") or "未归类"
        grouped[theme].append(item)
    records: list[dict[str, object]] = []
    for index, (theme, items) in enumerate(sorted(grouped.items()), start=1):
        sentiments = Counter(item.get("sentiment", "na") for item in items)
        scored = sum(truthy(item.get("used_for_scoring", "")) for item in items)
        direction = max(("positive", "neutral", "negative", "mixed"), key=lambda key: sentiments[key])
        dimension = next((item.get("primary_dimension", "") for item in items if item.get("primary_dimension")), "")
        excerpt = next((item.get("excerpt_or_summary", "") for item in items if item.get("excerpt_or_summary")), "")
        records.append(
            {
                "code": f"T{index:03d}",
                "theme": theme,
                "dimension": dimension,
                "direction": direction,
                "definition": "依据当前有效证据归纳，须结合代表性摘录解释。",
                "all": len(items),
                "scored": scored,
                "positive": sentiments["positive"],
                "neutral": sentiments["neutral"],
                "negative": sentiments["negative"],
                "mixed": sentiments["mixed"],
                "excerpt": excerpt,
            }
        )
    return records


def score_cross_dimensions(scores: dict[str, object]) -> dict[str, float | None]:
    cross = scores.get("cross_platform") if isinstance(scores.get("cross_platform"), dict) else {}
    dimensions = cross.get("dimensions") if isinstance(cross, dict) and isinstance(cross.get("dimensions"), dict) else {}
    result: dict[str, float | None] = {}
    for name in DIMENSION_WEIGHTS:
        item = dimensions.get(name) if isinstance(dimensions, dict) else None
        value = item.get("platform_equal_score") if isinstance(item, dict) else None
        result[name] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return result


def write_theme_sheet(workbook: object, formats: dict[str, object], evidence: list[dict[str, str]]) -> None:
    worksheet = workbook.add_worksheet("主题编码")
    records = build_theme_records(evidence)
    rows = [
        [
            item["code"], item["theme"], item["dimension"], item["direction"], item["definition"],
            item["all"], item["scored"], item["positive"], item["neutral"], item["negative"],
            item["mixed"], "", item["excerpt"],
        ]
        for item in records
    ]
    add_table(
        worksheet,
        THEME_HEADERS,
        rows,
        [12, 26, 24, 12, 42, 14, 14, 10, 10, 10, 10, 16, 48],
        "ThemesTable",
        formats,
    )
    denominator = max(1, sum(1 for item in evidence if truthy(item.get("is_valid", ""))))
    for row_index, item in enumerate(records, start=1):
        excel_row = row_index + 1
        formulas = [
            (5, f'=COUNTIF(DetailTable[规范化评价主题],B{excel_row})', item["all"], formats["integer"]),
            (6, f'=COUNTIFS(DetailTable[规范化评价主题],B{excel_row},DetailTable[是否纳入量化评分],"是")', item["scored"], formats["integer"]),
            (7, f'=COUNTIFS(DetailTable[规范化评价主题],B{excel_row},DetailTable[情感倾向],"正向")', item["positive"], formats["integer"]),
            (8, f'=COUNTIFS(DetailTable[规范化评价主题],B{excel_row},DetailTable[情感倾向],"中性")', item["neutral"], formats["integer"]),
            (9, f'=COUNTIFS(DetailTable[规范化评价主题],B{excel_row},DetailTable[情感倾向],"负向")', item["negative"], formats["integer"]),
            (10, f'=COUNTIFS(DetailTable[规范化评价主题],B{excel_row},DetailTable[情感倾向],"混合")', item["mixed"], formats["integer"]),
            (11, f'=IFERROR(F{excel_row}/COUNTA(DetailTable[内部样本编号]),0)', item["all"] / denominator, formats["percent"]),
        ]
        for column, formula, cached, cell_format in formulas:
            worksheet.write_formula(row_index, column, formula, cell_format, cached)


def write_score_sheet(
    workbook: object,
    formats: dict[str, object],
    evidence: list[dict[str, str]],
    sources: list[dict[str, str]],
    search_log: list[dict[str, str]],
    scores: dict[str, object],
) -> None:
    worksheet = workbook.add_worksheet("七维评分")
    prepare_sheet(worksheet)
    widths = [
        8, 26, 12, 22, 12, 16, 16, 18, 18, 14, 14, 14, 16, 16, 16, 16, 24, 16,
        18, 14, 14, 48, 16, 12, 26, 14, 18, 30, 18, 16, 18, 14, 18, 16, 16, 52,
        18, 22, 38,
    ]
    if len(widths) != len(SCORE_HEADERS):
        raise ValueError("seven-dimension score sheet width/header contract drifted")
    for column, width in enumerate(widths):
        worksheet.set_column(column, column, width)
        worksheet.write(0, column, SCORE_HEADERS[column], formats["header"])
    cross = scores.get("cross_platform") if isinstance(scores.get("cross_platform"), dict) else {}
    score_dimensions = cross.get("dimensions") if isinstance(cross.get("dimensions"), dict) else {}
    total_valid = max(1, sum(1 for item in evidence if truthy(item.get("is_valid", ""))))
    rows: list[list[object]] = []
    for index, (name, weight) in enumerate(DIMENSION_WEIGHTS.items(), start=1):
        score_item = score_dimensions.get(name) if isinstance(score_dimensions, dict) else None
        score_item = score_item if isinstance(score_item, dict) else {}
        details = score_item.get("evidence_audit") if isinstance(score_item.get("evidence_audit"), dict) else {}
        status = str(details.get("status", "not_scored"))
        score = safe_number(score_item.get("platform_equal_score"))
        actual_weight = weight if score is not None else None
        tendency = (score / 10 - 5) if score is not None else None
        retrieved_count = int(details.get("retrieved_evidence_units", 0))
        topic_count = int(details.get("topic_coverage_evidence_units", 0))
        eligible_count = int(score_item.get("eligible_evidence_units", details.get("eligible_evidence_units", 0)))
        scored_count = int(score_item.get("scored_evidence_units", details.get("scored_evidence_units", 0)))
        positive_count = int(score_item.get("scoring_positive_units", details.get("scoring_positive_units", 0)))
        neutral_count = int(score_item.get("scoring_neutral_units", details.get("scoring_neutral_units", 0)))
        negative_count = int(score_item.get("scoring_negative_units", details.get("scoring_negative_units", 0)))
        if positive_count + neutral_count + negative_count != scored_count:
            raise ValueError(f"score object direction counts do not sum to scored evidence: {name}")
        valid_platforms = int(score_item.get("valid_scoring_platforms", 0))
        candidate_platforms = int(score_item.get("dimension_candidate_platform_count", 0))
        scorable_platforms = int(score_item.get("dimension_scorable_platform_count", 0))
        run_platforms = int(score_item.get("run_included_platform_count", 0))
        if valid_platforms != scorable_platforms:
            raise ValueError(f"score object scorable platform count mismatch: {name}")
        platform_rule_status = str(score_item.get("platform_minimum_rule_status", "not_scored"))
        confidence = str(score_item.get("scoring_confidence", details.get("scoring_confidence", details.get("confidence", "数据不足"))))
        mention_ratio = topic_count / total_valid
        mention_level = "A" if mention_ratio >= 0.30 else "B" if mention_ratio >= 0.20 else "C" if mention_ratio >= 0.10 else "D" if mention_ratio >= 0.05 else "E" if topic_count else "U"
        score_status = (
            "优先目标达标" if status == "sufficient" else
            "明确中置信度目标达标" if status == "sufficient_at_requested_target" else
            "中置信度审计终止" if status == "sufficient_at_medium_after_audit" else
            "受控终止后保留合格结果" if status == "retrieval_terminated_with_shortfall" else
            "穷尽后仅作定性说明" if status == "exhausted_with_shortfall" else
            "必须继续本维检索或复核" if status == "needs_iteration" else
            "尚未生成正式评分"
        )
        rows.append(
            [
                index,
                name,
                weight,
                score_status,
                actual_weight,
                retrieved_count,
                topic_count,
                eligible_count,
                scored_count,
                positive_count,
                neutral_count,
                negative_count,
                valid_platforms,
                candidate_platforms,
                scorable_platforms,
                run_platforms,
                platform_rule_status,
                tendency,
                score,
                score * actual_weight if score is not None and actual_weight is not None else None,
                confidence,
                (
                    "正式评分只按主维度归属；候选记录经平台最低样本门槛后形成实际计分集合，正中负数量与得分来自同一集合。"
                    if status == "sufficient"
                    else "正式计分证据达到中置信度门槛且通过更强独立终止审计；继续披露候选与实际计分差额。"
                    if status == "sufficient_at_medium_after_audit"
                    else "检索已按机器证明的受控条件终止；本维达到正式最低门槛，保留确定性分数并同步披露目标缺口。"
                    if status == "retrieval_terminated_with_shortfall" and score is not None
                    else "检索已按机器证明的受控条件终止；本维未达到正式最低门槛，仅保留定性解释。"
                    if status == "retrieval_terminated_with_shortfall"
                    else "本次明确选择中置信度目标且本维达到对应正式门槛。"
                    if status == "sufficient_at_requested_target"
                    else "合法公开路径穷尽后正式计分证据仍不足，只保留定性解释，不输出正式数值。"
                    if status == "exhausted_with_shortfall"
                    else "正式计分能力不足，必须继续本维定向检索、语义复核或平台样本补足。"
                ),
                mention_ratio,
                mention_level,
                name,
                score,
                "正式得分" if score is not None else "数据不足；雷达图保留空点",
                "",
                int(details.get("minimum_evidence_units", 25)),
                int(details.get("distinct_source_pages", 0)),
                int(details.get("minimum_source_pages", 12)),
                int(details.get("source_category_count", 0)),
                int(details.get("minimum_source_categories", 3)),
                len(details.get("targeted_deep_rounds", [])),
                "中",
                (
                    "；".join(
                        str(value)
                        for value in (
                            details.get("enhancement_gaps", [])
                            if confidence == "中" else details.get("gaps", [])
                        )
                    )
                    or str(details.get("exhaustion_basis", ""))
                    or "已达到中高或高置信度优先目标"
                ),
                "中高/高",
                (
                    "通过" if details.get("medium_completion_audit_passed") else
                    "不适用（已达中高/高）" if confidence in {"中高", "高"} else
                    "未通过"
                ),
                str(details.get("medium_completion_basis", "")) or "不适用",
            ]
        )
    displayed_rows = rows
    for row_index, values in enumerate(displayed_rows, start=1):
        worksheet.set_row(row_index, 25.5)
        for column, value in enumerate(values):
            if column in {2, 4, 22} and value is not None:
                worksheet.write_number(row_index, column, float(value), formats["percent"])
            elif column == 17 and value is not None:
                worksheet.write_number(row_index, column, float(value), formats["decimal2"])
            elif column in {18, 19, 25} and value is not None:
                worksheet.write_number(row_index, column, float(value), formats["decimal"])
            elif column in {0, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 28, 29, 30, 31, 32, 33}:
                worksheet.write_number(row_index, column, int(value), formats["integer"])
            else:
                worksheet.write(
                    row_index,
                    column,
                    "" if value is None else display_value(SCORE_HEADERS[column], value),
                    formats["body"],
                )
        excel_row = row_index + 1
        score = values[18]
        weighted = values[19]
        if values[17] is not None:
            worksheet.write_formula(row_index, 18, f'=IF(R{excel_row}="","",(R{excel_row}+5)*10)', formats["decimal"], score)
        if values[4] is not None and score is not None:
            worksheet.write_formula(row_index, 19, f'=IF(OR(E{excel_row}="",S{excel_row}=""),"",E{excel_row}*S{excel_row})', formats["decimal"], weighted)
    worksheet.autofilter(0, 0, len(rows), len(SCORE_HEADERS) - 1)
    worksheet.conditional_format(1, 18, len(rows), 18, {"type": "3_color_scale"})
    chart = workbook.add_chart({"type": "radar", "subtype": "with_markers"})
    chart.add_series(
        {
            "name": "七维得分",
            "categories": "='七维评分'!$Y$2:$Y$8",
            "values": "='七维评分'!$Z$2:$Z$8",
            "line": {"color": "#17324D", "width": 2.0},
            "marker": {"type": "circle", "size": 5, "border": {"color": "#17324D"}, "fill": {"color": "#5B9BD5"}},
        }
    )
    chart.set_title({"name": "历史文化活态传承七维评分"})
    chart.set_legend({"none": True})
    chart.show_blanks_as("gap")
    chart.set_size({"width": 560, "height": 360})
    worksheet.insert_chart("AO2", chart)


def machine_values(
    place: str,
    retrieval_date: str,
    sources: list[dict[str, str]],
    evidence: list[dict[str, str]],
    search_log: list[dict[str, str]],
    scores: dict[str, object],
    execution_schema_context: dict[str, object] | None = None,
) -> tuple[list[object], dict[str, object]]:
    execution_context = execution_schema_context or scores.get('dimension_evidence_audit', {}).get('audit_provenance', {}).get('execution_schema_context')
    executed_log = [dict(row) for row in executed_search_rows(search_log, schema_context=execution_context)]
    relevant = [row for row in sources if truthy(row.get("is_relevant", ""))]
    groups = {unique_key(row) for row in relevant if unique_key(row)}
    readable = [row for row in relevant if row.get("access_status") in {"full", "partial"}]
    readable_groups = {unique_key(row) for row in readable if unique_key(row)}
    limited = sum(row.get("access_status") in {"snippet_only", "metadata_only"} for row in relevant)
    inaccessible = sum(row.get("access_status") == "inaccessible" for row in relevant)
    represented = {row.get("source_category", "") for row in relevant if row.get("source_category")}
    attempted = {
        row.get("source_category_target", "")
        for row in executed_log
        if row.get("source_category_target") not in {"", "mixed"}
    }
    user_pages = {
        unique_key(row)
        for row in readable
        if truthy(row.get("is_user_source", "")) and not truthy(row.get("suspected_promotion", ""))
    }
    valid_evidence = [row for row in evidence if truthy(row.get("is_valid", ""))]
    eligible = [row for row in valid_evidence if truthy(row.get("formal_scoring_eligible", ""))]
    scored = [row for row in valid_evidence if truthy(row.get("included_in_platform_score", ""))]
    protocol = load_protocol()
    default_targets = research_target_summary(protocol=protocol)
    legacy_floor = int(default_targets["legacy_scored_evidence_floor"])
    neutral_band = float(protocol["score_scale"]["neutral_band"])
    sentiment = Counter(scoring_direction(scoring_value(row), neutral_band) for row in scored)
    scored_count = len(scored)
    from temporal_fields import publication_year
    years = sorted({publication_year(row.get("published_at", "")) for row in sources} - {""})
    time_range = "—".join((years[0], years[-1])) if len(years) > 1 else years[0] if years else "数据不足"
    dimension_scores = score_cross_dimensions(scores)
    cross = scores.get("cross_platform") if isinstance(scores.get("cross_platform"), dict) else {}
    recommendation = scores.get("recommendation") if isinstance(scores.get("recommendation"), dict) else {}
    equal_score = cross.get("platform_equal_score") if isinstance(cross, dict) else None
    weighted_score = cross.get("sample_weighted_score") if isinstance(cross, dict) else None
    final_score = recommendation.get("final_score") if isinstance(recommendation, dict) else None
    confidence = "高" if scored_count >= legacy_floor and len(user_pages) >= 30 else "中" if scored_count >= 30 else "低" if scored_count else "数据不足"
    semantic_methods = Counter(row.get("semantic_method", "") for row in evidence if row.get("semantic_method", ""))
    semantic_reviews = Counter(row.get("semantic_review_status", "") for row in evidence if row.get("semantic_review_status", ""))
    lexicon_matches = Counter(row.get("lexicon_match_status", "") for row in evidence if row.get("lexicon_match_status", ""))
    coding_statuses = Counter(row.get("coding_parse_status", "") for row in evidence if row.get("coding_parse_status", ""))
    semantic_confidences = [
        float(row["semantic_confidence"])
        for row in evidence
        if row.get("semantic_confidence", "")
        and re.fullmatch(r"(?:0(?:\.\d+)?|1(?:\.0+)?)", row["semantic_confidence"])
    ]
    evidence_reliabilities = [
        float(row["evidence_reliability"])
        for row in evidence
        if row.get("evidence_reliability", "")
        and re.fullmatch(r"(?:0(?:\.\d+)?|1(?:\.0+)?)", row["evidence_reliability"])
    ]
    semantic_low_confidence = sum(value < 0.72 for value in semantic_confidences)
    semantic_figurative = sum(truthy(row.get("figurative_risk", "")) for row in evidence)
    dimension_evidence = scores.get("dimension_evidence_audit") if isinstance(scores.get("dimension_evidence_audit"), dict) else {}
    dimension_details = dimension_evidence.get("dimensions") if isinstance(dimension_evidence.get("dimensions"), dict) else {
        name: {
            "status": "needs_iteration",
            "confidence": "待深检",
            "scored_evidence_units": 0,
        }
        for name in DIMENSION_WEIGHTS
    }
    dimension_confidences = Counter(str(item["confidence"]) for item in dimension_details.values())
    needs_iteration_dimensions = [
        name for name, item in dimension_details.items()
        if isinstance(item, dict) and item.get("status") == "needs_iteration"
    ]
    enhancement_target_dimensions = [
        name for name in needs_iteration_dimensions
        if dimension_details[name].get("confidence") == "中"
    ]
    task_run_ids = {
        row.get("task_run_id", "")
        for row in [*executed_log, *sources, *evidence]
        if row.get("task_run_id", "")
    }
    task_run_id = next(iter(task_run_ids), "") if len(task_run_ids) == 1 else "数据不一致"
    deep_round_numbers = {
        int(row.get("iteration_round", "0"))
        for row in executed_log
        if row.get("iteration_round", "").isdigit() and int(row.get("iteration_round", "0")) > 0
    }
    new_samples_by_round: defaultdict[int, int] = defaultdict(int)
    statuses_by_round: defaultdict[int, set[str]] = defaultdict(set)
    query_count_by_round: defaultdict[int, int] = defaultdict(int)
    for row in executed_log:
        if not row.get("iteration_round", "").isdigit():
            continue
        round_number = int(row.get("iteration_round", "0"))
        if round_number > 0:
            query_count_by_round[round_number] += 1
        parsed_new = safe_number(row.get("new_scored_evidence_units", ""))
        if isinstance(parsed_new, (int, float)):
            new_samples_by_round[round_number] += int(parsed_new)
        if row.get("status", ""):
            statuses_by_round[round_number].add(row["status"])
    deepest_round = max(deep_round_numbers, default=0)
    deepest_actions = {
        row.get("next_action", "")
        for row in executed_log
        if row.get("iteration_round", "").isdigit()
        and int(row.get("iteration_round", "0")) == deepest_round
    }
    target_confidence = str(dimension_evidence.get("target_confidence", "中高"))
    targets = research_target_summary(target_confidence=target_confidence, protocol=protocol)
    page_target = int(targets["minimum_deduplicated_relevant_pages"])
    effective_target = int(targets["theoretical_minimum_scored_evidence"])
    target_met = scored_count >= effective_target
    latest_two_rounds = sorted(deep_round_numbers)[-2:]
    low_yield_exhaustion = (
        len(latest_two_rounds) == 2
        and all(new_samples_by_round[round_number] < 5 for round_number in latest_two_rounds)
    )
    blocked_or_empty_exhaustion = bool(deepest_round) and bool(statuses_by_round[deepest_round]) and all(
        status in {"blocked", "no_results"}
        for status in statuses_by_round[deepest_round]
    )
    deep_query_depth_met = bool(deep_round_numbers) and all(
        query_count_by_round[round_number] >= 8 for round_number in deep_round_numbers
    )
    exhaustion_supported = (
        (low_yield_exhaustion or blocked_or_empty_exhaustion)
        and deep_query_depth_met
        and len(attempted) >= 10
    )
    retrieval_state = (
        dimension_evidence.get("retrieval_termination", {})
        if isinstance(dimension_evidence.get("retrieval_termination"), dict)
        else {}
    )
    if target_met:
        iteration_outcome = "target_met"
    elif retrieval_state.get("terminal") is True:
        iteration_outcome = "retrieval_terminated_with_shortfall"
    elif len(deep_round_numbers) >= 3 and "exhausted" in deepest_actions and exhaustion_supported:
        iteration_outcome = "exhausted_with_shortfall"
    else:
        iteration_outcome = "continue_deep_search"
    values: list[object] = [
        place,
        retrieval_date,
        len(executed_log),
        len(sources),
        len(groups),
        page_target,
        "是" if len(groups) >= page_target else "否",
        len(readable_groups),
        limited,
        inaccessible,
        len(represented),
        len(attempted),
        len(user_pages),
        len(evidence),
        len(valid_evidence),
        scored_count,
        sentiment["positive"],
        sentiment["positive"] / scored_count if scored_count else None,
        sentiment["neutral"],
        sentiment["neutral"] / scored_count if scored_count else None,
        sentiment["negative"],
        sentiment["negative"] / scored_count if scored_count else None,
        sentiment["mixed"],
        sentiment["mixed"] / scored_count if scored_count else None,
        *[dimension_scores[name] for name in DIMENSION_WEIGHTS],
        RESULT_LAYER_NAME,
        equal_score,
        weighted_score,
        final_score,
        confidence,
        time_range,
        "当前公开网络中可检索、可读取样本；页面数不代表互联网总体。",
        "大众点评和小红书默认剔除：未登录访问通常受限，且另有独立获取路径；本结果用于后续与两类独立结果综合，避免重复样本、重复计数和证据层混淆。",
        "除经核验的官方完整导出外，不声称获得全量评论。",
        "不保存密码、验证码、Cookie、Token 或无关个人标识。",
        task_run_id,
        effective_target,
        "是" if target_met else "否",
        max(0, effective_target - scored_count),
        len(deep_round_numbers),
        iteration_outcome,
        semantic_methods["source_native_numeric"],
        semantic_methods["rule_codebook"],
        semantic_methods["coding_parse_fallback"],
        semantic_methods["assisted_semantic"],
        semantic_methods["manual_code"],
        semantic_reviews["auto_eligible"],
        semantic_reviews["human_confirmed"],
        semantic_reviews["review_required"],
        semantic_low_confidence,
        semantic_figurative,
        lexicon_matches["zero_hit"],
        coding_statuses["candidate_generated"],
        coding_statuses["started_unresolved"],
        sum(semantic_confidences) / len(semantic_confidences) if semantic_confidences else None,
        sum(evidence_reliabilities) / len(evidence_reliabilities) if evidence_reliabilities else None,
        "中",
        dimension_confidences["高"],
        dimension_confidences["中高"],
        dimension_confidences["中"],
        dimension_confidences["低"] + dimension_confidences["数据不足"],
        len(needs_iteration_dimensions),
        "；".join(enhancement_target_dimensions),
        "；".join(needs_iteration_dimensions),
        json.dumps(dimension_evidence, ensure_ascii=False, separators=(",", ":")),
        scores.get("formal_scoring_schema_version", protocol["formal_scoring"]["schema_version"]),
        len(eligible),
        len(scored),
        sentiment["positive"],
        sentiment["neutral"],
        sentiment["negative"],
        json.dumps(
            scores.get("cross_platform", {}).get("dimensions", {})
            if isinstance(scores.get("cross_platform"), dict) else {},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        len(search_log) - len(executed_log),
        int(executed_query_metrics(search_log, schema_context=execution_context)["unique_query_intent_count"]),
        int(targets["legacy_scored_evidence_floor"]),
        target_confidence,
        int(targets["per_dimension_scored_evidence_target"]),
        effective_target,
        str(retrieval_state.get("termination_status", "")),
    ]
    metrics = {
        "deduplicated_relevant_pages": len(groups),
        "readable_source_pages": len(readable_groups),
        "source_categories": len(represented),
        "search_rows": len(executed_log),
        "planned_or_nonexecuted_search_rows": len(search_log) - len(executed_log),
        "evidence_rows": len(evidence),
        "scoring_records": scored_count,
        "minimum_effective_sample_target": effective_target,
        "minimum_effective_sample_met": target_met,
        "deep_iteration_rounds": len(deep_round_numbers),
        "deep_iteration_query_depth_met": deep_query_depth_met,
        "exhaustion_evidence_met": exhaustion_supported,
        "iteration_outcome": iteration_outcome,
        "native_numeric_rows": semantic_methods["source_native_numeric"],
        "semantic_rule_rows": semantic_methods["rule_codebook"],
        "semantic_coding_parse_rows": semantic_methods["coding_parse_fallback"],
        "semantic_assisted_rows": semantic_methods["assisted_semantic"],
        "semantic_manual_rows": semantic_methods["manual_code"],
        "semantic_auto_eligible_rows": semantic_reviews["auto_eligible"],
        "semantic_human_confirmed_rows": semantic_reviews["human_confirmed"],
        "semantic_review_required_rows": semantic_reviews["review_required"],
        "semantic_low_confidence_rows": semantic_low_confidence,
        "semantic_figurative_risk_rows": semantic_figurative,
        "semantic_lexicon_zero_hit_rows": lexicon_matches["zero_hit"],
        "semantic_coding_candidate_rows": coding_statuses["candidate_generated"],
        "semantic_coding_unresolved_rows": coding_statuses["started_unresolved"],
        "minimum_dimension_confidence": "中",
        "dimension_confidence_counts": dict(dimension_confidences),
        "dimensions_needing_iteration": len(needs_iteration_dimensions),
        "dimension_gap_targets": list(needs_iteration_dimensions),
        "dimension_enhancement_targets": list(enhancement_target_dimensions),
        "dimension_evidence": dimension_evidence,
        "task_run_id": task_run_id,
    }
    return values, metrics


def write_machine_sheet(
    workbook: object,
    formats: dict[str, object],
    values: list[object],
) -> None:
    worksheet = workbook.add_worksheet("机器可读汇总")
    widths = [15] * len(MACHINE_HEADERS)
    widths[0] = 18
    widths[1] = 16
    for column in range(38, min(43, len(widths))):
        widths[column] = 42
    for column, width in zip(range(len(widths) - 6, len(widths)), [24, 18, 18, 18, 18, 28]):
        widths[column] = width
    add_table(
        worksheet,
        MACHINE_HEADERS,
        [values],
        widths,
        "MachineSummaryTable",
        formats,
    )
    worksheet.set_row(1, 84)
    cached = values
    formulas = {
        2: '=COUNTA(SearchLogTable[检索记录编号])',
        3: '=COUNTA(SourcePagesTable[内部来源编号])',
        5: '=150',
        6: '=IF(E2>=F2,"是","否")',
        13: '=COUNTA(DetailTable[内部样本编号])',
        14: '=COUNTIF(DetailTable[是否有效样本],"是")',
        15: '=COUNTIF(DetailTable[是否实际进入平台评分],"是")',
        16: '=COUNTIFS(DetailTable[是否实际进入平台评分],"是",DetailTable[正式计分方向],"正向")',
        17: '=IFERROR(Q2/P2,"")',
        18: '=COUNTIFS(DetailTable[是否实际进入平台评分],"是",DetailTable[正式计分方向],"中性")',
        19: '=IFERROR(S2/P2,"")',
        20: '=COUNTIFS(DetailTable[是否实际进入平台评分],"是",DetailTable[正式计分方向],"负向")',
        21: '=IFERROR(U2/P2,"")',
        22: '=0',
        23: '=IFERROR(W2/P2,"")',
    }
    machine_index = {name: index for index, name in enumerate(MACHINE_HEADERS)}
    formulas.update(
        {
            machine_index["native_numeric_rows"]: '=COUNTIF(DetailTable[语义量化方法],"来源原生数值")',
            machine_index["semantic_rule_rows"]: '=COUNTIF(DetailTable[语义量化方法],"代码簿规则")',
            machine_index["semantic_coding_parse_rows"]: '=COUNTIF(DetailTable[语义量化方法],"编码解析候选")',
            machine_index["semantic_assisted_rows"]: '=COUNTIF(DetailTable[语义量化方法],"辅助语义编码")',
            machine_index["semantic_manual_rows"]: '=COUNTIF(DetailTable[语义量化方法],"人工编码")',
            machine_index["semantic_auto_eligible_rows"]: '=COUNTIF(DetailTable[语义复核状态],"自动符合")',
            machine_index["semantic_human_confirmed_rows"]: '=COUNTIF(DetailTable[语义复核状态],"人工确认")',
            machine_index["semantic_review_required_rows"]: '=COUNTIF(DetailTable[语义复核状态],"需要复核")',
            machine_index["semantic_low_confidence_rows"]: '=COUNTIF(DetailTable[语义置信度],"<0.72")',
            machine_index["semantic_figurative_risk_rows"]: '=COUNTIF(DetailTable[反讽或修辞风险],"是")',
            machine_index["semantic_lexicon_zero_hit_rows"]: '=COUNTIF(DetailTable[词表命中状态],"未命中")',
            machine_index["semantic_coding_candidate_rows"]: '=COUNTIF(DetailTable[编码解析状态],"已生成候选")',
            machine_index["semantic_coding_unresolved_rows"]: '=COUNTIF(DetailTable[编码解析状态],"已启动但未解决")',
            machine_index["semantic_mean_confidence"]: '=IFERROR(AVERAGE(DetailTable[语义置信度]),"")',
            machine_index["semantic_mean_evidence_reliability"]: '=IFERROR(AVERAGE(DetailTable[证据可靠性]),"")',
        }
    )
    formulas.update(
        {
            machine_index["eligible_evidence_units"]: '=COUNTIF(DetailTable[正式评分候选资格],"是")',
            machine_index["scored_evidence_units"]: '=COUNTIF(DetailTable[是否实际进入平台评分],"是")',
            machine_index["scored_positive_units"]: '=COUNTIFS(DetailTable[是否实际进入平台评分],"是",DetailTable[正式计分方向],"正向")',
            machine_index["scored_neutral_units"]: '=COUNTIFS(DetailTable[是否实际进入平台评分],"是",DetailTable[正式计分方向],"中性")',
            machine_index["scored_negative_units"]: '=COUNTIFS(DetailTable[是否实际进入平台评分],"是",DetailTable[正式计分方向],"负向")',
        }
    )
    for column, formula in formulas.items():
        cell_format = formats["percent"] if column in {17, 19, 21, 23} else formats["body"]
        worksheet.write_formula(1, column, formula, cell_format, cached[column])


def build_workbook(
    sources_path: Path,
    evidence_path: Path,
    search_log_path: Path,
    output_path: Path,
    place: str,
    retrieval_date: str,
    limitations_path: Path | None = None,
    scores_path: Path | None = None,
    execution_schema_context: dict[str, object] | None = None,
) -> dict[str, object]:
    if scores_path is None:
        raise ValueError("formal workbook generation requires the composite-score truth JSON")
    sources = read_csv(sources_path)
    evidence = read_csv(evidence_path)
    search_log = read_csv(search_log_path)
    limitations = read_csv(limitations_path) if limitations_path else []
    scores = read_json(scores_path)
    from input_safety import assert_public_text
    assert_public_text([sources, evidence, search_log, limitations, place])
    protocol = load_protocol()
    formal_chain = build_formal_scoring_chain(
        evidence,
        sources,
        protocol=protocol,
        require_source_contract=bool(sources),
    )
    evidence = annotate_rows(evidence, formal_chain)
    if scores:
        if scores.get("status") != "valid":
            raise ValueError("formal workbook generation requires a valid composite-score truth JSON")
        scoring_input = scores.get("formal_scoring_input")
        if not isinstance(scoring_input, dict):
            raise ValueError("composite-score truth lacks its formal scoring gate input")
        verify_scoring_input_gate(scoring_input)
        if scores.get("formal_scoring_schema_version") != protocol["formal_scoring"]["schema_version"]:
            raise ValueError("scores formal_scoring_schema_version does not match the fixed protocol")
        if scores.get("evaluation_protocol_version") != protocol["evaluation_protocol_version"]:
            raise ValueError("scores evaluation_protocol_version does not match the fixed protocol")
        if scores.get("dimension_weight_scheme") != WEIGHT_SCHEME_NAME:
            raise ValueError(f"scores must declare dimension_weight_scheme={WEIGHT_SCHEME_NAME}")
        score_weights = scores.get("dimension_weights")
        if not isinstance(score_weights, dict) or set(score_weights) != set(DIMENSION_WEIGHTS):
            raise ValueError("scores dimension_weights must contain exactly the seven canonical dimensions")
        for dimension, expected in DIMENSION_WEIGHTS.items():
            actual = score_weights.get(dimension)
            if (
                isinstance(actual, bool)
                or not isinstance(actual, (int, float))
                or not math.isclose(float(actual), expected, abs_tol=1e-12)
            ):
                raise ValueError(
                    f"scores dimension weight for {dimension} must equal {expected:.0%}"
                )
        cross = scores.get("cross_platform")
        cross_dimensions = cross.get("dimensions") if isinstance(cross, dict) else None
        if not isinstance(cross_dimensions, dict):
            raise ValueError("scores must contain cross_platform dimensions")
        for dimension in DIMENSION_WEIGHTS:
            item = cross_dimensions.get(dimension)
            if not isinstance(item, dict):
                raise ValueError(f"scores missing formal dimension result: {dimension}")
            expected = formal_chain["dimensions"][dimension]
            for field in (
                "eligible_evidence_units",
                "scored_evidence_units",
                "scoring_positive_units",
                "scoring_neutral_units",
                "scoring_negative_units",
            ):
                if int(item.get(field, -1)) != int(expected[field]):
                    raise ValueError(f"scores and evidence ledger disagree on {dimension}/{field}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(output_path, {"strings_to_formulas": False, "strings_to_urls": False})
    workbook.set_properties(
        {
            "title": f"{place}网络检索与量化编码",
            "subject": "历史文化活力公开网络样本分析",
            "author": "研究工作流",
            "comments": "页面、证据、主题、评分、检索日志、限制与机器汇总。",
        }
    )
    workbook.set_calc_mode("auto")
    formats = add_formats(workbook)
    sample_rows = build_sample_rows(evidence, sources)
    worksheet = workbook.add_worksheet("样本明细")
    add_table(
        worksheet,
        SAMPLE_HEADERS,
        sample_rows,
        SAMPLE_WIDTHS,
        "DetailTable",
        formats,
        hyperlink_columns={21},
    )
    worksheet.conditional_format(1, SAMPLE_HEADERS.index("情感分值"), max(1, len(sample_rows)), SAMPLE_HEADERS.index("情感分值"), {"type": "3_color_scale", "min_color": "#F8696B", "mid_color": "#FFEB84", "max_color": "#63BE7B"})
    for semantic_header in ("语义置信度", "证据可靠性", "平台内聚合权重"):
        column = SAMPLE_HEADERS.index(semantic_header)
        worksheet.conditional_format(1, column, max(1, len(sample_rows)), column, {"type": "3_color_scale", "min_color": "#F8696B", "mid_color": "#FFEB84", "max_color": "#63BE7B"})
    source_rows = [[row.get(key, "") for _, key, _ in SOURCE_COLUMNS] for row in sources]
    worksheet = workbook.add_worksheet("来源页面")
    add_table(
        worksheet,
        [header for header, _, _ in SOURCE_COLUMNS],
        source_rows,
        [width for _, _, width in SOURCE_COLUMNS],
        "SourcePagesTable",
        formats,
        hyperlink_columns={9},
    )
    write_theme_sheet(workbook, formats, evidence)
    write_score_sheet(workbook, formats, evidence, sources, search_log, scores)
    worksheet = workbook.add_worksheet("检索日志")
    add_table(
        worksheet,
        SEARCH_HEADERS,
        [
            [display_value(SEARCH_HEADERS[index], row.get(key, "")) for index, key in enumerate(SEARCH_KEYS)]
            for row in search_log
        ],
        [24, 14, 12, 30, 20, 18, 16, 22, 42, 22, 14, 16, 14, 14, 18, 20, 14, 18, 16, 18, 36,
         14, 18, 18, 22, 22, 36, 12, 22, 18, 22, 20, 22, 14, 18, 18, 12],
        "SearchLogTable",
        formats,
    )
    worksheet = workbook.add_worksheet("数据限制说明")
    if not limitations:
        limitations = [
            {
                "limitation_topic": "公开网络样本边界",
                "details": "公开可检索样本不等同于全部居民和游客意见。",
                "handling": "报告页面数、读取状态、选择机制与未覆盖来源。",
                "interpretive_impact": "结论仅适用于本次检索范围。",
            },
            {
                "limitation_topic": "默认来源处理",
                "details": "大众点评和小红书在未登录状态下通常触发访问限制，且研究另有独立获取路径。",
                "handling": "本工作流默认剔除两者，扩展其他公开来源，并在后续把本结果与两类独立获取结果综合。",
                "interpretive_impact": "避免重复样本、重复计数和证据层混淆；不因两平台缺失而虚构内容或降低合规标准。",
            },
        ]
    add_table(
        worksheet,
        LIMITATION_HEADERS,
        [[row.get(key, "") for key in LIMITATION_KEYS] for row in limitations],
        [22, 66, 48, 48],
        "LimitationsTable",
        formats,
    )
    values, metrics = machine_values(place, retrieval_date, sources, evidence, search_log, scores, execution_schema_context)
    write_machine_sheet(workbook, formats, values)
    workbook.close()
    return {
        "status": "created",
        "output": str(output_path.resolve()),
        "sheets": SHEET_NAMES,
        **metrics,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--search-log", required=True, type=Path)
    parser.add_argument("--limitations", type=Path)
    parser.add_argument("--scores", required=True, type=Path)
    parser.add_argument("--place", required=True)
    parser.add_argument("--retrieval-date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--writer-ledger", required=True, type=Path)
    parser.add_argument("--truth-freeze", required=True, type=Path)
    parser.add_argument("--preflight", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="build_research_workbook.py",
            output_role="research_workbook",
            expected_phase="REPORT_BUILD",
            output_path=args.output,
        )
        freeze = verify_truth_freeze(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            manifest_path=args.truth_freeze,
            require_scoring=True,
        )
        for role, path in (
            ("source_ledger", args.sources),
            ("formal_evidence", args.evidence),
            ("search_log", args.search_log),
            ("scoring_output", args.scores),
        ):
            assert_frozen_artifact_path(freeze, role=role, path=path)
        if args.limitations is not None:
            verify_artifact_writer(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_role="limitations",
                output_path=args.limitations,
            )
        verify_preflight(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            preflight_path=args.preflight,
        )
        from execution_facts import context_from_state
        from runtime_guard import load_runtime_state
        from retrieval_controls import load_retrieval_config
        execution_context = context_from_state(load_runtime_state(args.state),
            config=load_retrieval_config(), state_path=args.state)
        result = build_workbook(
            args.sources,
            args.evidence,
            args.search_log,
            args.output,
            args.place.strip(),
            args.retrieval_date.strip(),
            args.limitations,
            args.scores,
            execution_context,
        )
        inputs = {
            "sources": args.sources,
            "evidence": args.evidence,
            "search_log": args.search_log,
            "scores": args.scores,
            "truth_freeze": args.truth_freeze,
            "preflight": args.preflight,
        }
        if args.limitations:
            inputs["limitations"] = args.limitations
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="build_research_workbook.py",
            output_role="research_workbook",
            output_path=args.output,
            input_paths=inputs,
            expected_phase="REPORT_BUILD",
        )
    except (OSError, ValueError, json.JSONDecodeError, xlsxwriter.exceptions.XlsxWriterException) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
