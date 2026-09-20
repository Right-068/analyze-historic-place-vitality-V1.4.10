#!/usr/bin/env python3
"""Reopen and jointly validate the final DOCX and two XLSX deliverables without Office."""

from __future__ import annotations

import argparse
import csv
import strict_json as json
import re
import zipfile
import math
import os
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from input_safety import query_parameter_names
from input_safety import safe_urlparse as urlparse, finite_number, privacy_errors, normalize_host
from xml.etree import ElementTree as ET
from spreadsheet_safety import audit_formula_cells, audit_external_relationships

from dimension_evidence import (
    HIGH_EVIDENCE_UNITS,
    HIGH_SOURCE_CATEGORIES,
    HIGH_SOURCE_PAGES,
    MEDIUM_HIGH_EVIDENCE_UNITS,
    MEDIUM_HIGH_SOURCE_CATEGORIES,
    MEDIUM_HIGH_SOURCE_PAGES,
    MIN_DIMENSION_EVIDENCE_UNITS,
    MIN_DIMENSION_SOURCE_CATEGORIES,
    MIN_DIMENSION_SOURCE_PAGES,
    evaluate_dimension_evidence,
    validate_machine_dimension_audit,
)
from dimension_framework import (
    DIMENSION_BOUNDARIES,
    DIMENSION_DEFINITIONS,
    DIMENSION_NAMES,
    DIMENSION_WEIGHTS,
    RESULT_LAYER_NAME,
    WEIGHT_SCHEME_NAME,
)
from formal_scoring import (
    dedup_key as formal_dedup_key,
    load_protocol,
    record_eligibility,
    scoring_direction,
    scoring_value,
)
from formal_states import DIMENSION_FINAL_STATUSES
from retrieval_controls import (
    budget_values,
    execution_schema_context_from_state,
    executed_query_metrics,
    load_retrieval_config,
    research_target_summary,
    validate_execution_records,
    validate_query_budget_compliance,
    validate_round_completion,
)
from excel_localization import normalize_matrix_for_validation, display_value
from detailed_run_log import audit_log, assert_state_log_alignment
from artifact_provenance import (
    register_protected_artifact,
    verify_artifact_writer,
    verify_preflight,
    verify_truth_freeze,
)
from validation_repair_plan import build_error_manifest, build_repair_plan, stable_error
from pre_admission_audit import (
    verify_admission_commit,
    verify_canonical_active_views,
    verify_source_grounding,
)
from source_capture import verify_capture_manifest, verify_capture_transaction_chain
from source_identity import normalized_url_sha256
from runtime_guard import (
    authorize_runtime_write,
    canonical_sha256,
    canonical_url,
    load_runtime_state,
    validate_evidence_truth_chain,
    verify_release_manifest,
    verify_scoring_input_gate,
)

_PROTOCOL = load_protocol()
_THRESHOLDS = _PROTOCOL["thresholds"]
SEMANTIC_CONFIDENCE_THRESHOLD = float(_THRESHOLDS["semantic_auto_confidence"])
EVIDENCE_RELIABILITY_THRESHOLD = float(_THRESHOLDS["minimum_evidence_reliability"])
ASPECT_CONFIDENCE_THRESHOLD = float(_THRESHOLDS["dimension_auto_confidence"])


SECTIONS = [
    "地点基本信息",
    "检索过程与数据来源",
    "来源结构分析",
    "总体情感与综合评价",
    "七个维度的详细分析",
    "七维评价汇总表",
    "高频正面评价主题",
    "高频负面评价主题",
    "不同平台评价差异",
    "不同用户群体和场景差异",
    "时间变化分析",
    "历史文化活态传承感知评价",
    "结论",
    "来源清单",
    "机器可读结果",
]

DIMENSIONS = DIMENSION_NAMES

SHEETS = [
    "样本明细",
    "来源页面",
    "主题编码",
    "七维评分",
    "检索日志",
    "数据限制说明",
    "机器可读汇总",
]

RULE_WORKBOOK_SHEETS = [
    "评价协议摘要", "发布校准来源", "发布校准决策", "极性词表", "七维词表", "上下文规则", "编码解析规则"
]
RULE_WORKBOOK_HEADERS = {
    "评价协议摘要": ["地点名称", "任务运行编号", "本次检索日期", "评价协议版本", "语义代码簿版本", "协议发布日期", "发布校准模式", "校准查询数", "可读方法来源数", "独立来源域名数", "校准决策数", "语义代码簿SHA256", "任务期规则变更", "规则应用说明"],
    "发布校准来源": ["来源编号", "页面标题", "发布机构", "来源域名", "来源URL", "检索日期", "读取状态", "方法领域", "发布校准发现"],
    "发布校准决策": ["决策编号", "动作", "规则目标", "决策理由", "依据来源编号", "是否改变锁定代码簿"],
    "极性词表": ["概念编号", "极性", "基础权重", "语言", "词项类型", "词项", "中文释义", "英文释义", "机器读取路径"],
    "七维词表": ["维度编号", "中文维度名", "英文维度名", "初始研究权重", "语言", "词项类型", "词项", "中文释义", "英文释义", "机器读取路径"],
    "上下文规则": ["规则类别", "规则编号", "语言", "词项或参数", "参数值", "作用范围", "机器处理", "规则说明"],
    "编码解析规则": ["顺序", "触发条件", "输入", "处理规则", "输出", "阈值或上限", "评分政策", "复核要求"],
}

REQUIRED_HEADERS = {
    "样本明细": [
        "内部样本编号", "所属来源页面编号", "页面标题", "摘要原文", "来源链接",
        "主题维度标签",
        "是否纳入量化评分", "情感分值", "原生评分值", "原生量表下界",
        "原生量表上界", "原生评分标准化值", "语义量化方法", "语义语言", "语义分析单位",
        "语义分句数", "词表命中状态", "词表命中轨迹", "编码解析状态",
        "编码解析候选", "编码解析轨迹", "开放编码编号",
        "维度判定依据", "维度判定置信度", "维度规则命中",
        "极性判定依据", "否定命中", "程度命中", "转折命中", "弱化命中",
        "反讽或修辞风险", "语义连续分", "语义整数分", "语义置信度",
        "证据可靠性", "平台内聚合权重", "语义复核状态", "语义规则轨迹",
        "正式评分候选资格", "正式评分排除原因", "正式评分去重键", "平台样本状态",
        "是否实际进入平台评分", "正式计分方向",
        "编码者编号", "裁决状态", "任务运行编号",
    ],
    "来源页面": ["内部来源编号", "地点名称", "来源类型", "页面标题", "页面URL", "检索记录编号", "实际检索词", "读取状态", "任务运行编号"],
    "主题编码": ["主题编码", "规范化评价主题", "归属维度", "全部有效提及数"],
    "七维评分": [
        "序号", "维度", "初始研究权重", "评分证据状态", "检索有效证据数",
        "主题覆盖证据数", "评分候选证据数", "实际计分证据数",
        "计分正面数", "计分中性数", "计分负面数", "有效计分平台数",
        "维度候选平台数", "维度可计分平台数", "本次纳入平台数",
        "平台最低样本规则状态", "跨平台等权倾向值(-5~+5)", "跨平台等权维度得分(0~100)",
        "加权得分", "置信度", "最低有效证据门槛", "独立来源页数",
        "最低来源页门槛", "来源类型数", "最低来源类型门槛", "定向深检轮数",
        "最低置信度要求", "缺口或穷尽说明", "优先置信度目标",
        "中置信度终止审计", "中置信度终止依据",
    ],
    "检索日志": ["任务运行编号", "检索记录编号", "迭代轮次", "缺口目标", "迭代模式", "本轮前维度置信度", "检索日期", "实际检索词", "目标来源类型", "本次新增实际计分证据", "累计实际计分证据", "检索状态", "下一动作", "记录类型", "计划编号", "执行编号", "查询编号", "规范查询意图"],
    "数据限制说明": ["限制主题", "具体说明", "处理方式", "对解释的影响"],
    "机器可读汇总": [
        "project_place", "deduplicated_relevant_pages", "minimum_page_target", "minimum_page_met",
        "task_run_id", "minimum_effective_sample_target", "minimum_effective_sample_met",
        "deep_iteration_rounds", "iteration_outcome", "native_numeric_rows", "semantic_rule_rows",
        "semantic_coding_parse_rows",
        "semantic_assisted_rows", "semantic_manual_rows",
        "semantic_auto_eligible_rows", "semantic_human_confirmed_rows",
        "semantic_review_required_rows", "semantic_low_confidence_rows",
        "semantic_figurative_risk_rows", "semantic_lexicon_zero_hit_rows",
        "semantic_coding_candidate_rows", "semantic_coding_unresolved_rows", "semantic_mean_confidence",
        "semantic_mean_evidence_reliability",
        "minimum_dimension_confidence", "dimensions_high_confidence",
        "dimensions_medium_high_confidence", "dimensions_medium_confidence",
        "dimensions_exhausted_low_confidence", "dimensions_needing_iteration",
        "dimension_enhancement_targets", "dimension_gap_targets",
        "dimension_evidence_status_json",
        "formal_scoring_schema_version", "eligible_evidence_units", "scored_evidence_units",
        "scored_positive_units", "scored_neutral_units", "scored_negative_units",
        "dimension_scoring_summary_json",
    ],
}

MIN_DIMENSION_EVIDENCE = MIN_DIMENSION_EVIDENCE_UNITS
MIN_DIMENSION_EXHAUSTION_ROUNDS = 4
MIN_DIMENSION_QUERIES_PER_ROUND = 8
MIN_DIMENSION_EXHAUSTION_TARGET_CATEGORIES = 5
MIN_DIMENSION_LOW_YIELD_SOURCE_PAGES = 12
MIN_DIMENSION_LOW_YIELD_DOMAINS = 3
MIN_DIMENSION_BLOCKED_SOURCE_PAGES = 5
MIN_DIMENSION_BLOCKED_DOMAINS = 2
EXECUTED_SEARCH_STATUSES = {"completed", "partial", "blocked", "no_results"}

MIN_POSITIVE_CJK = 100
MIN_NEGATIVE_CJK = 90
MIN_COMBINED_DIFFERENCE_CJK = 180
MIN_DIFFERENCE_ITEMS = 2
MIN_DIMENSION_CJK = 1000
MIN_SCOPE_CJK = 300
MIN_CONCLUSION_CJK = 1500
MIN_CONCLUSION_PARAGRAPHS = 7
MIN_ANALYTICAL_BODY_CJK = 12500
PREFERRED_ANALYTICAL_BODY_CJK = (13500, 16500)

SECTION_MIN_CJK = {
    "1. 地点基本信息": 800,
    "2. 检索过程与数据来源": 1000,
    "3. 来源结构分析": 350,
    "4. 总体情感与综合评价": 350,
    "5. 七个维度的详细分析": 5000,
    "6. 七维评价汇总表": 300,
    "7. 高频正面评价主题": 250,
    "8. 高频负面评价主题": 280,
    "9. 不同平台评价差异": 750,
    "10. 不同用户群体和场景差异": 500,
    "11. 时间变化分析": 650,
    "12. 历史文化活态传承感知评价": 750,
    "13. 结论": MIN_CONCLUSION_CJK,
}

EXPECTED_HEADING2 = [
    "扩展检索名称和关联词",
    "2.1 实际使用的检索词",
    "2.2 来源规模",
    "2.3 去重规则执行情况",
    "2.4 无法充分访问的平台",
    "跨平台计算",
    *[f"5.{index} {dimension}" for index, dimension in enumerate(DIMENSIONS, start=1)],
    "权重核算",
    "9.1 平台特征",
    "9.2 平台评分表",
    "11.1 改造前后的空间变化",
    "11.2 近年用户感知的变化",
    "11.3 商业化程度变化",
    "11.4 游客数量和拥挤变化",
    "11.5 文化活动变化",
    "11.6 长期问题判断",
]

DOCX_TABLE_HEADERS = [
    ["项目", "基本信息"],
    ["指标", "本次检索结果"],
    ["平台", "访问情况", "处理方式"],
    ["来源平台", "来源数量", "内容类型", "时间范围", "是否用于情感分析", "主要价值", "主要局限"],
    ["指标", "得分", "说明"],
    ["评价维度", "初始研究权重", "正面主题", "负面主题", "提及率等级", "倾向得分", "转换分", "证据充分度", "主要来源平台"],
    ["排名", "正面主题", "具体含义", "提及率等级", "正面强度1—5", "主要平台", "证据一致性"],
    ["排名", "负面主题", "具体问题", "提及率等级", "负面强度1—5", "主要平台", "证据一致性"],
    ["平台", "主要正面主题", "主要负面主题", "七维综合分", "样本充分度", "平台偏向"],
    ["用户群体或场景", "主要感知", "主要问题", "判断可靠性"],
    ["问题", "是否长期存在", "判断"],
    ["序号", "平台或网站", "页面标题", "发布时间", "内容类型", "是否用于评分", "来源链接"],
]

SENSITIVE_QUERY_KEYS = {"token", "xsec_token", "auth", "authorization", "session", "cookie", "code"}
DIMENSION_FIXED_SUBHEADINGS = [
    "主要正面评价",
    "主要负面评价",
]
DIMENSION_LABELS = [
    "维度定义",
    "作用机制",
    "反证与替代解释",
    "不确定性与适用边界",
    "综合判断",
    "初始研究权重",
    "高频主题",
    "提及率等级",
    "跨平台等权倾向值",
    "跨平台等权维度得分",
    "证据充分度",
    "检索有效证据数",
    "主题覆盖证据数",
    "评分候选证据数",
    "实际计分证据数",
    "计分正中负构成",
    "有效计分平台",
    "独立来源页数",
    "来源类型数",
    "维度置信度",
    "最低置信度要求",
    "优先置信度目标",
    "维度证据状态",
    "定向深检轮次",
    "定向深检轮数",
    "独立穷尽审计",
    "中置信度终止审计",
    "中置信度终止依据",
    "编码边界",
    "代表性来源",
]
XLSX_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
ALLOWED_REPORT_FONTS = {"黑体", "宋体", "Times New Roman"}
ENGLISH_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z]{2,}(?:[A-Za-z0-9+./-]*)(?:\s+[A-Za-z][A-Za-z0-9+./-]*){0,5})(?![A-Za-z0-9_])"
)
REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
OFFICE_REL_NS = {"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def read_sources(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def zip_integrity(path: Path, required_parts: set[str]) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"file not found: {path}"]
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                errors.append(f"corrupt ZIP member in {path.name}: {bad}")
            missing = sorted(required_parts - set(archive.namelist()))
            if missing:
                errors.append(f"{path.name} missing package parts: {', '.join(missing)}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"cannot open {path.name}: {exc}")
    return errors


def relationship_targets(archive: zipfile.ZipFile, rels_path: str) -> dict[str, str]:
    root = ET.fromstring(archive.read(rels_path))
    return {
        item.attrib["Id"]: item.attrib["Target"]
        for item in root.findall("r:Relationship", REL_NS)
    }


def resolve_part(base: str, target: str) -> str:
    base_path = PurePosixPath(base).parent
    combined = base_path / target
    parts: list[str] = []
    for part in combined.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in {".", ""}:
            parts.append(part)
    return "/".join(parts)


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    from spreadsheet_safety import decode_spreadsheet_text
    return [decode_spreadsheet_text("".join(node.text or "" for node in item.findall(".//x:t", XLSX_NS))) for item in root.findall("x:si", XLSX_NS)]


def column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        return 0
    value = 0
    for character in letters.group(0):
        value = value * 26 + ord(character) - 64
    return value


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        from spreadsheet_safety import decode_spreadsheet_text
        return decode_spreadsheet_text("".join(node.text or "" for node in cell.findall(".//x:t", XLSX_NS)))
    value = cell.find("x:v", XLSX_NS)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(value.text)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "true" if value.text == "1" else "false"
    return value.text


def sheet_matrix(xml: bytes, shared_strings: list[str]) -> tuple[list[list[str]], int, dict[str, str]]:
    root = ET.fromstring(xml)
    rows: list[list[str]] = []
    formula_count = len(root.findall(".//x:f", XLSX_NS))
    row1 = root.find(".//x:sheetData/x:row[@r='1']", XLSX_NS)
    row1_attributes = dict(row1.attrib) if row1 is not None else {}
    for row in root.findall(".//x:sheetData/x:row", XLSX_NS):
        values: dict[int, str] = {}
        for cell in row.findall("x:c", XLSX_NS):
            index = column_number(cell.attrib.get("r", ""))
            if index:
                values[index] = cell_text(cell, shared_strings)
        if values:
            maximum = max(values)
            rows.append([values.get(index, "") for index in range(1, maximum + 1)])
        else:
            rows.append([])
    return rows, formula_count, row1_attributes


def header_style(archive: zipfile.ZipFile, sheet_xml: bytes) -> dict[str, object]:
    sheet_root = ET.fromstring(sheet_xml)
    a1 = sheet_root.find(".//x:c[@r='A1']", XLSX_NS)
    if a1 is None or "s" not in a1.attrib:
        return {}
    style_index = int(a1.attrib["s"])
    styles = ET.fromstring(archive.read("xl/styles.xml"))
    xfs = styles.findall("x:cellXfs/x:xf", XLSX_NS)
    if style_index >= len(xfs):
        return {}
    xf = xfs[style_index]
    font_id = int(xf.attrib.get("fontId", "0"))
    fill_id = int(xf.attrib.get("fillId", "0"))
    fonts = styles.findall("x:fonts/x:font", XLSX_NS)
    fills = styles.findall("x:fills/x:fill", XLSX_NS)
    font = fonts[font_id] if font_id < len(fonts) else None
    fill = fills[fill_id] if fill_id < len(fills) else None
    name_node = font.find("x:name", XLSX_NS) if font is not None else None
    size_node = font.find("x:sz", XLSX_NS) if font is not None else None
    color_node = font.find("x:color", XLSX_NS) if font is not None else None
    fill_node = fill.find("x:patternFill/x:fgColor", XLSX_NS) if fill is not None else None
    alignment = xf.find("x:alignment", XLSX_NS)
    return {
        "font_name": name_node.attrib.get("val") if name_node is not None else None,
        "font_size": size_node.attrib.get("val") if size_node is not None else None,
        "bold": font.find("x:b", XLSX_NS) is not None if font is not None else False,
        "font_color": color_node.attrib.get("rgb") if color_node is not None else None,
        "fill_color": fill_node.attrib.get("rgb") if fill_node is not None else None,
        "horizontal": alignment.attrib.get("horizontal") if alignment is not None else None,
        "vertical": alignment.attrib.get("vertical") if alignment is not None else None,
        "wrap_text": alignment.attrib.get("wrapText") if alignment is not None else None,
    }


def matrix_records(matrix: list[list[str]]) -> list[dict[str, str]]:
    if not matrix:
        return []
    headers = matrix[0]
    return [
        {
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(headers)
        }
        for row in matrix[1:]
        if any(value for value in row)
    ]


def read_workbook_matrices(path: Path) -> dict[str, list[list[str]]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = relationship_targets(archive, "xl/_rels/workbook.xml.rels")
        shared_strings = load_shared_strings(archive)
        matrices: dict[str, list[list[str]]] = {}
        for sheet in workbook.findall("x:sheets/x:sheet", XLSX_NS):
            name = sheet.attrib.get("name", "")
            relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS['r']}}}id", "")
            target = rels.get(relationship_id, "")
            if not target:
                continue
            part = resolve_part("xl/workbook.xml", target)
            if part in archive.namelist():
                matrices[name] = normalize_matrix_for_validation(
                    sheet_matrix(archive.read(part), shared_strings)[0]
                )
        return matrices


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value + 1e-12, digits)


def _capped_weights(counts: dict[str, int], cap: float) -> dict[str, float] | None:
    if len(counts) < 2:
        return None
    result = {name: 0.0 for name in counts}
    active = list(counts)
    remaining = 1.0
    while active:
        total = sum(counts[name] for name in active)
        tentative = {name: remaining * counts[name] / total for name in active}
        over = [name for name, weight in tentative.items() if weight > cap + 1e-12]
        if not over:
            result.update(tentative)
            break
        for name in over:
            result[name] = cap
            active.remove(name)
            remaining -= cap
    return result


def independent_formal_scoring_audit(
    evidence_path: Path,
    sources_path: Path,
    search_log_path: Path,
    scores_path: Path,
    report_data_path: Path,
    workbook_path: Path,
    execution_schema_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Independently rebuild every score from bottom evidence and compare outputs."""
    errors: list[str] = []
    warnings: list[str] = []
    evidence = read_sources(evidence_path)
    sources = read_sources(sources_path)
    search_rows = read_sources(search_log_path)
    if execution_schema_context is not None:
        from execution_facts import validate_source_links
        facts = validate_execution_records(search_rows, schema_context=execution_schema_context)
        linked = validate_source_links(sources, facts)
        if facts['invalid_records'] or linked['errors']:
            errors.append('independent scoring execution/source identity validation failed')
        # Current eligibility must be reconstructed, never accepted from an
        # old annotation on an execution that was subsequently retracted.
        active_source_ids = {row['source_id'] for row in linked['valid_sources']}
        for row in evidence:
            if row.get('source_id') not in active_source_ids and (
                    row.get('used_for_scoring') != 'false' or row.get('score_scope') != 'not_scored'):
                errors.append('retracted source evidence still advertises scoring scope: '+row.get('evidence_id',''))
    scores = json.loads(scores_path.read_text(encoding="utf-8-sig"))
    report_data = json.loads(report_data_path.read_text(encoding="utf-8-sig"))
    if not isinstance(scores, dict) or not isinstance(report_data, dict):
        raise ValueError("scores and report data roots must be JSON objects")
    protocol = load_protocol()
    formal = protocol["formal_scoring"]
    platform_rule = formal["platform_dimension"]
    aggregation_rule = formal["platform_aggregation"]
    minimum_units = int(platform_rule["minimum_dimension_units"])
    neutral_band = float(protocol["score_scale"]["neutral_band"])
    source_lookup = {row.get("source_id", ""): row for row in sources if row.get("source_id", "")}

    seen_evidence_ids: set[str] = set()
    seen_dimension_groups: set[tuple[str, str]] = set()
    eligible: list[dict[str, str]] = []
    decisions: dict[str, dict[str, object]] = {}
    for row in evidence:
        evidence_id = row.get("evidence_id", "")
        if not evidence_id or evidence_id in seen_evidence_ids:
            errors.append(f"bottom evidence has missing or duplicate evidence_id: {evidence_id!r}")
            continue
        seen_evidence_ids.add(evidence_id)
        decision = record_eligibility(
            row,
            protocol,
            source_lookup.get(row.get("source_id", "")),
            require_source_contract=True,
        )
        key = (str(decision["primary_dimension"]), str(decision["dedup_key"]))
        if decision["eligible"] and key in seen_dimension_groups:
            decision["eligible"] = False
            decision["reasons"] = [*decision["reasons"], "duplicate_within_primary_dimension"]
        elif decision["eligible"]:
            seen_dimension_groups.add(key)
            normalized = dict(row)
            normalized["platform"] = str(decision["platform"])
            normalized["formal_dedup_sha256"] = str(decision["dedup_key"])
            eligible.append(normalized)
        decisions[evidence_id] = decision

    grouped: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in eligible:
        grouped[(row.get("platform", "未标注平台") or "未标注平台", row["primary_dimension"])].append(row)
    scored: list[dict[str, str]] = []
    platform_dimensions: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    platform_names = sorted({platform for platform, _ in grouped})
    for platform in platform_names:
        platform_eligible = [row for row in eligible if (row.get("platform", "未标注平台") or "未标注平台") == platform]
        for dimension in DIMENSIONS:
            candidates = grouped.get((platform, dimension), [])
            selected = candidates if len(candidates) >= minimum_units else []
            scored.extend(selected)
            directions = Counter(scoring_direction(scoring_value(row), neutral_band) for row in selected)
            weighted_total = sum(float(scoring_value(row)) * float(row["aggregation_weight"]) for row in selected)
            weight_total = sum(float(row["aggregation_weight"]) for row in selected)
            tendency = None
            if selected and weight_total > 0:
                raw_tendency = weighted_total / weight_total
                tendency = math.floor(raw_tendency + 0.5) if raw_tendency >= 0 else math.ceil(raw_tendency - 0.5)
            platform_dimensions[platform][dimension] = {
                "eligible_evidence_units": len(candidates),
                "scored_evidence_units": len(selected),
                "scoring_positive_units": directions["positive"],
                "scoring_neutral_units": directions["neutral"],
                "scoring_negative_units": directions["negative"],
                "minimum_dimension_units": minimum_units,
                "minimum_sample_met": bool(selected),
                "platform_sample_status": "scored" if selected else "insufficient_platform_samples",
                "tendency": tendency,
                "conversion": None if tendency is None else (tendency + 5) * 10,
            }

    scored_ids = {row["evidence_id"] for row in scored}
    for row in evidence:
        decision = decisions.get(row.get("evidence_id", ""))
        if not decision:
            continue
        expected_candidate = bool(decision["eligible"])
        expected_scored = row.get("evidence_id", "") in scored_ids
        expected_status = (
            "not_eligible" if not expected_candidate else
            "scored" if expected_scored else "insufficient_platform_samples"
        )
        expected_direction = scoring_direction(scoring_value(row), neutral_band) if expected_scored else "not_scored"
        ledger_comparisons = {
            "formal_scoring_eligible": "true" if expected_candidate else "false",
            "formal_scoring_dedup_key": str(decision["dedup_key"]),
            "platform_sample_status": expected_status,
            "included_in_platform_score": "true" if expected_scored else "false",
            "scoring_direction": expected_direction,
        }
        for field, expected in ledger_comparisons.items():
            if str(row.get(field, "")).strip().lower() != expected.lower():
                errors.append(f"bottom evidence annotation differs from independent decision: {row.get('evidence_id')}/{field}")

    audit_payload = scores.get("dimension_evidence_audit")
    audit_dimensions = audit_payload.get("dimensions") if isinstance(audit_payload, dict) else None
    if not isinstance(audit_dimensions, dict):
        errors.append("scores omit dimension_evidence_audit")
        audit_dimensions = {}
    dimension_recomputed: dict[str, dict[str, object]] = {}
    for dimension in DIMENSIONS:
        retrieved_keys = {
            formal_dedup_key(row, source_lookup.get(row.get("source_id", "")))
            for row in evidence
            if row.get("primary_dimension") == dimension
            and str(row.get("is_valid", "")).strip().lower() in {"true", "1", "yes", "是"}
            and str(row.get("is_direct_place_evidence", "")).strip().lower() in {"true", "1", "yes", "是"}
            and row.get("score_scope") == "direct"
            and formal_dedup_key(row, source_lookup.get(row.get("source_id", "")))
        }
        topic_keys = {
            formal_dedup_key(row, source_lookup.get(row.get("source_id", "")))
            for row in evidence
            if str(row.get("is_valid", "")).strip().lower() in {"true", "1", "yes", "是"}
            and str(row.get("is_direct_place_evidence", "")).strip().lower() in {"true", "1", "yes", "是"}
            and row.get("score_scope") == "direct"
            and dimension in {
                value.strip()
                for value in re.split(r"[;；|]", row.get("dimension_tags", ""))
                if value.strip()
            }
            and formal_dedup_key(row, source_lookup.get(row.get("source_id", "")))
        }
        eligible_rows = [row for row in eligible if row["primary_dimension"] == dimension]
        scored_rows = [row for row in scored if row["primary_dimension"] == dimension]
        source_ids = {row.get("source_id", "") for row in scored_rows if row.get("source_id", "")}
        categories = {
            source_lookup[source_id].get("source_category", "")
            for source_id in source_ids
            if source_id in source_lookup and source_lookup[source_id].get("source_category", "")
        }
        scorable_platforms = sorted({row.get("platform", "未标注平台") or "未标注平台" for row in scored_rows})
        directions = Counter(scoring_direction(scoring_value(row), neutral_band) for row in scored_rows)
        confidence = "数据不足"
        for label in ("中", "中高", "高"):
            rule = formal["dimension_confidence_thresholds"][label]
            if (
                len(scored_rows) >= int(rule["scored_evidence_units"])
                and len(source_ids) >= int(rule["distinct_source_pages"])
                and len(categories) >= int(rule["source_categories"])
                and len(scorable_platforms) >= int(rule["scorable_platforms"])
            ):
                confidence = label
        audit_item = audit_dimensions.get(dimension) if isinstance(audit_dimensions.get(dimension), dict) else {}
        status = str(audit_item.get("status", ""))
        formal_minimum_met = confidence in {"中", "中高", "高"}
        numeric_permitted = status in {
            "sufficient",
            "sufficient_at_requested_target",
            "sufficient_at_medium_after_audit",
        } or (
            status == "retrieval_terminated_with_shortfall" and formal_minimum_met
        )
        if status == "sufficient" and confidence not in {"中高", "高"}:
            errors.append(f"dimension marked sufficient below medium-high formal scoring confidence: {dimension}")
        if status == "sufficient_at_medium_after_audit" and (
            confidence != "中" or audit_item.get("medium_completion_audit_passed") is not True
        ):
            errors.append(f"dimension medium terminal status is unsupported: {dimension}")
        if status == "exhausted_with_shortfall" and audit_item.get("numeric_score_permitted") is not False:
            errors.append(f"exhausted dimension incorrectly permits a numeric score: {dimension}")
        if status == "retrieval_terminated_with_shortfall" and not audit_item.get("retrieval_termination_status"):
            errors.append(f"retrieval-terminal dimension lacks a machine stop reason: {dimension}")
        if status not in DIMENSION_FINAL_STATUSES:
            errors.append(f"final scoring contains unfinished or invalid dimension status: {dimension}/{status}")
        recomputed = {
            "retrieved_evidence_units": len(retrieved_keys),
            "topic_coverage_evidence_units": len(topic_keys),
            "eligible_evidence_units": len(eligible_rows),
            "scored_evidence_units": len(scored_rows),
            "scoring_positive_units": directions["positive"],
            "scoring_neutral_units": directions["neutral"],
            "scoring_negative_units": directions["negative"],
            "distinct_source_pages": len(source_ids),
            "source_category_count": len(categories),
            "dimension_scorable_platform_count": len(scorable_platforms),
            "scoring_confidence": confidence,
            "numeric_score_permitted": numeric_permitted,
        }
        dimension_recomputed[dimension] = recomputed
        for field, expected in recomputed.items():
            if field in {"numeric_score_permitted"}:
                actual = audit_item.get(field)
            else:
                actual = audit_item.get(field, audit_item.get("confidence") if field == "scoring_confidence" else None)
            if actual != expected:
                errors.append(f"scores dimension audit differs from bottom evidence: {dimension}/{field}")

    platform_results_raw = scores.get("platform_results")
    platform_results = {
        item.get("platform", ""): item
        for item in platform_results_raw
        if isinstance(platform_results_raw, list) and isinstance(item, dict)
    } if isinstance(platform_results_raw, list) else {}
    if set(platform_results) != set(platform_names):
        errors.append("scores platform set differs from independently reconstructed eligible platforms")
    platform_effective: dict[str, int] = {}
    platform_partial: dict[str, float | None] = {}
    for platform in platform_names:
        result = platform_results.get(platform, {})
        result_dimensions = result.get("dimensions") if isinstance(result.get("dimensions"), dict) else {}
        effective = sum(int(platform_dimensions[platform][name]["scored_evidence_units"]) for name in DIMENSIONS)
        eligible_count = sum(int(platform_dimensions[platform][name]["eligible_evidence_units"]) for name in DIMENSIONS)
        platform_effective[platform] = effective
        if result.get("effective_samples") != effective or result.get("eligible_samples") != eligible_count:
            errors.append(f"scores platform sample counts differ from bottom evidence: {platform}")
        conversions: dict[str, float] = {}
        for dimension in DIMENSIONS:
            expected = platform_dimensions[platform][dimension]
            actual = result_dimensions.get(dimension) if isinstance(result_dimensions.get(dimension), dict) else {}
            for field in (
                "eligible_evidence_units", "scored_evidence_units", "scoring_positive_units",
                "scoring_neutral_units", "scoring_negative_units", "minimum_dimension_units",
                "minimum_sample_met", "platform_sample_status", "tendency",
            ):
                if actual.get(field) != expected[field]:
                    errors.append(f"scores platform result differs from bottom evidence: {platform}/{dimension}/{field}")
            if dimension_recomputed[dimension]["numeric_score_permitted"] and expected["conversion"] is not None:
                conversions[dimension] = float(expected["conversion"])
        coverage_weight = sum(DIMENSION_WEIGHTS[name] for name in conversions)
        partial = (
            sum(conversions[name] * DIMENSION_WEIGHTS[name] for name in conversions) / coverage_weight
            if conversions else None
        )
        platform_partial[platform] = partial
        actual_partial = result.get("partial_platform_score")
        if partial is None:
            partial_mismatch = actual_partial is not None
        else:
            partial_mismatch = (
                isinstance(actual_partial, bool)
                or not isinstance(actual_partial, (int, float))
                or not math.isclose(float(actual_partial), float(_rounded(partial)), abs_tol=0.011)
            )
        if partial_mismatch:
            errors.append(f"scores platform partial score cannot be independently reproduced: {platform}")

    scored_platforms = [name for name in platform_names if platform_partial[name] is not None]
    minimum_platform_samples = int(aggregation_rule["minimum_platform_effective_samples"])
    normally_included = [name for name in scored_platforms if platform_effective[name] >= minimum_platform_samples]
    included = normally_included or scored_platforms
    cross = scores.get("cross_platform") if isinstance(scores.get("cross_platform"), dict) else {}
    if cross.get("included_platforms") != included:
        errors.append("cross-platform included platform set differs from independent recomputation")
    cross_dimensions = cross.get("dimensions") if isinstance(cross.get("dimensions"), dict) else {}
    recomputed_cross: dict[str, dict[str, object]] = {}
    for dimension in DIMENSIONS:
        available = [
            platform for platform in included
            if dimension_recomputed[dimension]["numeric_score_permitted"]
            and platform_dimensions[platform][dimension]["conversion"] is not None
        ]
        conversions = {platform: float(platform_dimensions[platform][dimension]["conversion"]) for platform in available}
        equal_score = _rounded(sum(conversions.values()) / len(conversions)) if conversions else None
        sample_weights = _capped_weights(
            {platform: platform_effective[platform] for platform in available},
            float(aggregation_rule["sample_weight_cap"]),
        )
        weighted_score = _rounded(sum(sample_weights[name] * conversions[name] for name in available)) if sample_weights else None
        item = cross_dimensions.get(dimension) if isinstance(cross_dimensions.get(dimension), dict) else {}
        for field, expected in {
            "eligible_evidence_units": dimension_recomputed[dimension]["eligible_evidence_units"],
            "scored_evidence_units": dimension_recomputed[dimension]["scored_evidence_units"],
            "scoring_positive_units": dimension_recomputed[dimension]["scoring_positive_units"],
            "scoring_neutral_units": dimension_recomputed[dimension]["scoring_neutral_units"],
            "scoring_negative_units": dimension_recomputed[dimension]["scoring_negative_units"],
            "valid_scoring_platforms": len(available),
            "dimension_candidate_platform_count": sum(
                1
                for platform in platform_names
                if int(platform_dimensions[platform][dimension]["eligible_evidence_units"]) > 0
            ),
            "dimension_scorable_platform_count": len(available),
            "run_included_platform_count": len(included),
            "available_platforms": available,
            "platform_equal_score": equal_score,
            "sample_weighted_score": weighted_score,
        }.items():
            if item.get(field) != expected:
                errors.append(f"cross-platform dimension cannot be independently reproduced: {dimension}/{field}")
        if dimension_recomputed[dimension]["numeric_score_permitted"] and equal_score is None:
            errors.append(f"dimension is scoring-sufficient but formal cross-platform score is empty: {dimension}")
        if not dimension_recomputed[dimension]["numeric_score_permitted"] and equal_score is not None:
            errors.append(f"dimension has formal cross-platform score despite insufficient scored evidence: {dimension}")
        recomputed_cross[dimension] = {
            **dimension_recomputed[dimension],
            "valid_scoring_platforms": len(available),
            "dimension_candidate_platform_count": sum(
                1
                for platform in platform_names
                if int(platform_dimensions[platform][dimension]["eligible_evidence_units"]) > 0
            ),
            "dimension_scorable_platform_count": len(available),
            "run_included_platform_count": len(included),
            "platform_minimum_rule_status": "scored" if available else "insufficient_platform_samples",
            "tendency_score": None if equal_score is None else equal_score / 10 - 5,
            "conversion_score": equal_score,
            "platform_equal_score": equal_score,
            "sample_weighted_score": weighted_score,
        }

    complete = all(recomputed_cross[name]["platform_equal_score"] is not None for name in DIMENSIONS)
    equal_composite = _rounded(sum(float(recomputed_cross[name]["platform_equal_score"]) * DIMENSION_WEIGHTS[name] for name in DIMENSIONS)) if complete else None
    weighted_complete = all(recomputed_cross[name]["sample_weighted_score"] is not None for name in DIMENSIONS)
    weighted_composite = _rounded(sum(float(recomputed_cross[name]["sample_weighted_score"]) * DIMENSION_WEIGHTS[name] for name in DIMENSIONS)) if weighted_complete else None
    if cross.get("platform_equal_score") != equal_composite:
        errors.append("cross-platform composite score cannot be independently reproduced")
    if cross.get("sample_weighted_score") != weighted_composite:
        errors.append("cross-platform sample-weighted score cannot be independently reproduced")

    matrices = read_workbook_matrices(workbook_path)
    workbook_scores = {row.get("维度", ""): row for row in matrix_records(matrices.get("七维评分", []))}
    for dimension in DIMENSIONS:
        row = workbook_scores.get(dimension, {})
        expected = recomputed_cross[dimension]
        comparisons = {
            "检索有效证据数": expected["retrieved_evidence_units"],
            "主题覆盖证据数": expected["topic_coverage_evidence_units"],
            "评分候选证据数": expected["eligible_evidence_units"],
            "实际计分证据数": expected["scored_evidence_units"],
            "计分正面数": expected["scoring_positive_units"],
            "计分中性数": expected["scoring_neutral_units"],
            "计分负面数": expected["scoring_negative_units"],
            "有效计分平台数": expected["valid_scoring_platforms"],
            "维度候选平台数": expected["dimension_candidate_platform_count"],
            "维度可计分平台数": expected["dimension_scorable_platform_count"],
            "本次纳入平台数": expected["run_included_platform_count"],
        }
        for header, value in comparisons.items():
            if integer_cell(row.get(header)) != value:
                errors.append(f"XLSX formal score field was altered or drifted: {dimension}/{header}")
        numeric_comparisons = {
            "跨平台等权倾向值(-5~+5)": expected["tendency_score"],
            "跨平台等权维度得分(0~100)": expected["conversion_score"],
            "加权得分": None if expected["conversion_score"] is None else float(expected["conversion_score"]) * DIMENSION_WEIGHTS[dimension],
        }
        for header, value in numeric_comparisons.items():
            actual = numeric_cell(row.get(header))
            if (actual is None) != (value is None) or (actual is not None and abs(actual - float(value)) > 0.011):
                errors.append(f"XLSX formal score field was altered or drifted: {dimension}/{header}")
        if row.get("平台最低样本规则状态", "") != expected["platform_minimum_rule_status"]:
            errors.append(f"XLSX platform sample status drifted: {dimension}")

    report_dimensions_raw = report_data.get("dimension_analyses")
    report_dimensions = {
        item.get("name", ""): item
        for item in report_dimensions_raw
        if isinstance(report_dimensions_raw, list) and isinstance(item, dict)
    } if isinstance(report_dimensions_raw, list) else {}
    for dimension in DIMENSIONS:
        item = report_dimensions.get(dimension, {})
        expected = recomputed_cross[dimension]
        for field in (
            "retrieved_evidence_units", "topic_coverage_evidence_units",
            "eligible_evidence_units", "scored_evidence_units", "scoring_positive_units",
            "scoring_neutral_units", "scoring_negative_units", "valid_scoring_platforms",
            "dimension_candidate_platform_count", "dimension_scorable_platform_count",
            "run_included_platform_count",
            "platform_minimum_rule_status", "tendency_score", "conversion_score",
        ):
            if item.get(field) != expected[field]:
                errors.append(f"report data formal score field differs from bottom evidence: {dimension}/{field}")
        if item.get("evidence_units") != expected["scored_evidence_units"]:
            errors.append(f"report data evidence_units alias differs from actual scored evidence: {dimension}")
    result_layer = report_data.get("result_layer") if isinstance(report_data.get("result_layer"), dict) else {}
    if result_layer.get("composite_score") != equal_composite:
        errors.append("report data composite score differs from independent formal recomputation")

    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "formal_scoring_schema_version": formal["schema_version"],
            "eligible_evidence_units": len(eligible),
            "scored_evidence_units": len(scored),
            "platform_count": len(platform_names),
            "dimensions": recomputed_cross,
            "platform_equal_score": equal_composite,
            "sample_weighted_score": weighted_composite,
        },
    }


def integer_cell(value: object) -> int:
    try:
        return int(finite_number(value if value not in (None, "") else 0))
    except ValueError:
        return 0


def numeric_cell(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("%"):
            return finite_number(text[:-1]) / 100
        return finite_number(value if isinstance(value, bool) else text)
    except ValueError:
        return None


def workbook_dimension_evidence_audit(
    matrices: dict[str, list[list[str]]],
    active_source_ids: set[str] | None = None,
) -> dict[str, object]:
    """Recompute displayed formal counts from reopened XLSX rows."""
    errors: list[str] = []
    warnings: list[str] = []
    sample_rows = matrix_records(matrices.get("样本明细", []))
    source_rows = matrix_records(matrices.get("来源页面", []))
    score_rows = matrix_records(matrices.get("七维评分", []))
    source_by_id = {
        row.get("内部来源编号", ""): row
        for row in source_rows
        if row.get("内部来源编号", "")
    }
    score_by_dimension = {
        row.get("维度", ""): row for row in score_rows if row.get("维度", "")
    }
    dimension_metrics: dict[str, dict[str, object]] = {}
    for dimension in DIMENSIONS:
        rows = [row for row in sample_rows if row.get("主要维度", "") == dimension]
        retrieved_keys: set[str] = set()
        topic_keys: set[str] = set()
        eligible_keys: set[str] = set()
        scored_keys: set[str] = set()
        scored_sources: set[str] = set()
        scored_categories: set[str] = set()
        scored_platforms: set[str] = set()
        directions: Counter[str] = Counter()
        for row in sample_rows:
            key = row.get("正式评分去重键", "") or row.get("内部样本编号", "")
            source_id = row.get("所属来源页面编号", "")
            source = source_by_id.get(source_id, {})
            primary = row.get("主要维度", "")
            valid_direct = (
                row.get("是否有效样本", "").strip().lower() in {"true", "1", "yes", "是"}
                and source.get("实体层级", "") == "area_direct"
                and source.get("评分范围", "") == "direct"
                and (active_source_ids is None or source_id in active_source_ids)
            )
            if valid_direct and primary == dimension and key:
                retrieved_keys.add(key)
            topic_tags = {
                value.strip()
                for value in re.split(r"[;；|]", row.get("主题维度标签", ""))
                if value.strip()
            }
            if valid_direct and key and dimension in topic_tags:
                topic_keys.add(key)
            if primary != dimension:
                continue
            if row.get("正式评分候选资格", "").strip().lower() in {"true", "1", "yes", "是"} and key:
                eligible_keys.add(key)
            if row.get("是否实际进入平台评分", "").strip().lower() in {"true", "1", "yes", "是"} and key:
                scored_keys.add(key)
                scored_sources.add(source_id)
                if source.get("来源类型", ""):
                    scored_categories.add(source["来源类型"])
                if source.get("平台或网站", ""):
                    scored_platforms.add(source["平台或网站"])
                directions[row.get("正式计分方向", "")] += 1
        score_row = score_by_dimension.get(dimension)
        if not score_row:
            errors.append(f"worksheet 七维评分 omits dimension: {dimension}")
            continue
        expected_weight = DIMENSION_WEIGHTS[dimension]
        actual_weight = numeric_cell(score_row.get("初始研究权重"))
        if actual_weight is None or abs(actual_weight - expected_weight) > 1e-12:
            errors.append(f"dimension {dimension} {WEIGHT_SCHEME_NAME} differs from canonical value")
        comparisons = {
            "检索有效证据数": len(retrieved_keys),
            "主题覆盖证据数": len(topic_keys),
            "评分候选证据数": len(eligible_keys),
            "实际计分证据数": len(scored_keys),
            "计分正面数": directions["positive"],
            "计分中性数": directions["neutral"],
            "计分负面数": directions["negative"],
            "独立来源页数": len(scored_sources),
            "来源类型数": len(scored_categories),
        }
        for header, expected in comparisons.items():
            if integer_cell(score_row.get(header)) != expected:
                errors.append(f"dimension {dimension} {header} differs from reopened sample rows: {expected}")
        if sum(directions[key] for key in ("positive", "neutral", "negative")) != len(scored_keys):
            errors.append(f"dimension {dimension} workbook scoring directions do not sum to actual scored evidence")
        tendency = numeric_cell(score_row.get("跨平台等权倾向值(-5~+5)"))
        conversion = numeric_cell(score_row.get("跨平台等权维度得分(0~100)"))
        weighted = numeric_cell(score_row.get("加权得分"))
        if (tendency is None) != (conversion is None):
            errors.append(f"dimension {dimension} tendency and conversion must both be present or absent")
        if tendency is not None and abs(conversion - (tendency + 5) * 10) > 0.011:
            errors.append(f"dimension {dimension} conversion cannot be recomputed from tendency")
        if conversion is not None and (weighted is None or abs(weighted - conversion * expected_weight) > 0.011):
            errors.append(f"dimension {dimension} weighted score cannot be recomputed")
        confidence = score_row.get("置信度", "")
        status = score_row.get("评分证据状态", "")
        formally_sufficient = confidence in {"中", "中高", "高"} and len(scored_keys) >= MIN_DIMENSION_EVIDENCE
        formal_platform_count = len(scored_platforms) if formally_sufficient else 0
        if formally_sufficient and conversion is None:
            errors.append(f"dimension {dimension} is marked scoring-sufficient but formal score is empty")
        if not formally_sufficient and conversion is not None:
            errors.append(f"dimension {dimension} has a formal score despite insufficient scored evidence")
        if integer_cell(score_row.get("有效计分平台数")) != formal_platform_count:
            errors.append(f"dimension {dimension} valid scoring platform count differs from reopened evidence")
        if integer_cell(score_row.get("维度可计分平台数")) != formal_platform_count:
            errors.append(f"dimension {dimension} scorable platform count differs from reopened evidence")
        if integer_cell(score_row.get("维度候选平台数")) < len(scored_platforms):
            errors.append(f"dimension {dimension} candidate platform count is below scorable platform count")
        dimension_metrics[dimension] = {
            "initial_research_weight": DIMENSION_WEIGHTS[dimension],
            "retrieved_evidence_units": len(retrieved_keys),
            "topic_coverage_evidence_units": len(topic_keys),
            "eligible_evidence_units": len(eligible_keys),
            "scored_evidence_units": len(scored_keys),
            "evidence_units": len(scored_keys),
            "scoring_positive_units": directions["positive"],
            "scoring_neutral_units": directions["neutral"],
            "scoring_negative_units": directions["negative"],
            "valid_scoring_platforms": formal_platform_count,
            "dimension_candidate_platform_count": integer_cell(
                score_row.get("维度候选平台数")
            ),
            "dimension_scorable_platform_count": integer_cell(
                score_row.get("维度可计分平台数")
            ),
            "run_included_platform_count": integer_cell(
                score_row.get("本次纳入平台数")
            ),
            "platform_minimum_rule_status": score_row.get("平台最低样本规则状态", ""),
            "tendency_score": tendency,
            "conversion_score": conversion,
            "source_pages": len(scored_sources),
            "source_categories": len(scored_categories),
            "confidence": confidence,
            "status": status,
            "targeted_round_count": integer_cell(score_row.get("定向深检轮数")),
            "independent_exhaustion_audit_passed": "穷尽" in status,
            "medium_completion_audit_passed": score_row.get("中置信度终止审计", "") == "通过",
            "medium_completion_basis": score_row.get("中置信度终止依据", "") or "不适用",
        }

    unknown_score_dimensions = sorted(set(score_by_dimension) - set(DIMENSIONS))
    if unknown_score_dimensions:
        errors.append(
            "worksheet 七维评分 contains non-canonical dimensions: "
            + ", ".join(unknown_score_dimensions)
        )
    return {
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "framework": "7+1",
            "result_layer": RESULT_LAYER_NAME,
            "minimum_dimension_confidence": "中",
            "preferred_dimension_confidence": "中高/高",
            "dimensions": dimension_metrics,
            "dimensions_at_or_above_medium": sum(item["confidence"] in {"中", "中高", "高"} for item in dimension_metrics.values()),
            "dimensions_preferred_target_met": sum(
                item["confidence"] in {"中高", "高"}
                for item in dimension_metrics.values()
            ),
            "dimensions_medium_terminal_audited": sum(
                item["medium_completion_audit_passed"]
                for item in dimension_metrics.values()
            ),
            "dimensions_independently_exhausted": sum(
                item["independent_exhaustion_audit_passed"] for item in dimension_metrics.values()
            ),
        },
    }


def audit_xlsx(
    path: Path,
    expected_source_ids: set[str],
    minimum_pages: int,
    minimum_effective_samples: int = 100,
    audit_status: str = "",
    retrieval_termination_status: str = "",
    execution_records: list[dict[str, str]] | None = None,
    execution_schema_context: dict[str, object] | None = None,
    source_records: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    errors = zip_integrity(
        path,
        {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"},
    )
    warnings: list[str] = []
    if errors:
        return {"status": "invalid", "errors": errors, "warnings": warnings}
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = relationship_targets(archive, "xl/_rels/workbook.xml.rels")
        shared_strings = load_shared_strings(archive)
        sheet_names: list[str] = []
        sheet_paths: dict[str, str] = {}
        for sheet in workbook.findall("x:sheets/x:sheet", XLSX_NS):
            name = sheet.attrib.get("name", "")
            relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS['r']}}}id", "")
            target = rels.get(relationship_id, "")
            if target:
                sheet_names.append(name)
                sheet_paths[name] = resolve_part("xl/workbook.xml", target)
        if sheet_names != SHEETS:
            errors.append(f"workbook sheets must be exactly {SHEETS}; found {sheet_names}")
        errors.extend(audit_external_relationships(archive))
        formula_count = 0
        matrices: dict[str, list[list[str]]] = {}
        header_styles: dict[str, dict[str, object]] = {}
        for name, part in sheet_paths.items():
            if part not in archive.namelist():
                errors.append(f"missing worksheet part for {name}: {part}")
                continue
            xml = archive.read(part)
            errors.extend(audit_formula_cells(xml, name))
            matrix, formulas, row1_attributes = sheet_matrix(xml, shared_strings)
            matrix = normalize_matrix_for_validation(matrix)
            matrices[name] = matrix
            formula_count += formulas
            if not matrix:
                errors.append(f"worksheet {name} is empty")
                continue
            headers = matrix[0]
            for required in REQUIRED_HEADERS.get(name, []):
                if required not in headers:
                    errors.append(f"worksheet {name} missing header: {required}")
            style = header_style(archive, xml)
            header_styles[name] = style
            if style.get("font_name") != "Carlito" or style.get("font_size") not in {"10", "10.0"}:
                errors.append(f"worksheet {name} header font must be Carlito 10")
            if not style.get("bold") or style.get("font_color") not in {"FFFFFFFF", "FFFFFF"}:
                errors.append(f"worksheet {name} header must use bold white font")
            if style.get("fill_color") not in {"FF17324D", "17324D"}:
                errors.append(f"worksheet {name} header fill must be #17324D")
            if style.get("horizontal") != "center" or style.get("vertical") != "center":
                errors.append(f"worksheet {name} header alignment must be centered")
            if style.get("wrap_text") not in {"1", "true", True}:
                errors.append(f"worksheet {name} header must wrap text")
            if row1_attributes.get("ht") not in {"34", "34.0"}:
                warnings.append(f"worksheet {name} header row height is not 34")
        source_matrix = matrices.get("来源页面", [])
        source_headers = source_matrix[0] if source_matrix else []
        source_rows = source_matrix[1:] if source_matrix else []
        source_ids = {row[0] for row in source_rows if row and row[0]}
        if source_ids != expected_source_ids:
            missing = sorted(expected_source_ids - source_ids)
            extra = sorted(source_ids - expected_source_ids)
            errors.append(f"workbook source IDs differ from ledger; missing={missing[:5]}, extra={extra[:5]}")
        if len(source_ids) < minimum_pages:
            errors.append(f"workbook contains only {len(source_ids)} source pages; minimum is {minimum_pages}")
        sample_matrix = matrices.get("样本明细", [])
        sample_headers = sample_matrix[0] if sample_matrix else []
        sample_rows = sample_matrix[1:] if sample_matrix else []
        scoring_column = sample_headers.index("是否实际进入平台评分") if "是否实际进入平台评分" in sample_headers else -1
        candidate_column = sample_headers.index("正式评分候选资格") if "正式评分候选资格" in sample_headers else -1
        scored_sample_rows = sum(
            scoring_column >= 0
            and len(row) > scoring_column
            and row[scoring_column].strip().lower() in {"true", "1", "yes", "是"}
            for row in sample_rows
        )
        machine_records = matrix_records(matrices.get("机器可读汇总", []))
        machine_record = machine_records[0] if len(machine_records) == 1 else {}
        machine_target = integer_cell(machine_record.get("minimum_effective_sample_target"))
        if minimum_effective_samples > 0 and machine_target != minimum_effective_samples:
            errors.append(
                "workbook machine target differs from the selected formal evidence target"
            )
        machine_terminal_status = machine_record.get("retrieval_termination_status", "")
        expected_terminal_status = (
            retrieval_termination_status
            if audit_status == "retrieval_terminated_with_shortfall"
            else ""
        )
        if machine_terminal_status != expected_terminal_status:
            errors.append(
                "workbook retrieval terminal status differs from the formal evidence audit"
            )
        machine_iteration_outcome = machine_record.get("iteration_outcome", "")
        expected_iteration_outcome = (
            "retrieval_terminated_with_shortfall"
            if audit_status == "retrieval_terminated_with_shortfall"
            else ""
        )
        if expected_iteration_outcome and machine_iteration_outcome != expected_iteration_outcome:
            errors.append(
                "workbook iteration outcome does not preserve the restricted terminal audit state"
            )
        semantic_status_column = sample_headers.index("语义复核状态") if "语义复核状态" in sample_headers else -1
        semantic_method_column = sample_headers.index("语义量化方法") if "语义量化方法" in sample_headers else -1
        semantic_confidence_column = sample_headers.index("语义置信度") if "语义置信度" in sample_headers else -1
        evidence_reliability_column = sample_headers.index("证据可靠性") if "证据可靠性" in sample_headers else -1
        aspect_confidence_column = sample_headers.index("维度判定置信度") if "维度判定置信度" in sample_headers else -1
        aggregation_weight_column = sample_headers.index("平台内聚合权重") if "平台内聚合权重" in sample_headers else -1
        semantic_unit_column = sample_headers.index("语义分析单位") if "语义分析单位" in sample_headers else -1
        semantic_final_column = sample_headers.index("语义整数分") if "语义整数分" in sample_headers else -1
        sentiment_score_column = sample_headers.index("情感分值") if "情感分值" in sample_headers else -1
        semantic_trace_column = sample_headers.index("语义规则轨迹") if "语义规则轨迹" in sample_headers else -1
        semantic_language_column = sample_headers.index("语义语言") if "语义语言" in sample_headers else -1
        lexicon_status_column = sample_headers.index("词表命中状态") if "词表命中状态" in sample_headers else -1
        lexicon_trace_column = sample_headers.index("词表命中轨迹") if "词表命中轨迹" in sample_headers else -1
        coding_status_column = sample_headers.index("编码解析状态") if "编码解析状态" in sample_headers else -1
        coding_candidates_column = sample_headers.index("编码解析候选") if "编码解析候选" in sample_headers else -1
        coding_trace_column = sample_headers.index("编码解析轨迹") if "编码解析轨迹" in sample_headers else -1
        open_code_column = sample_headers.index("开放编码编号") if "开放编码编号" in sample_headers else -1
        figurative_column = sample_headers.index("反讽或修辞风险") if "反讽或修辞风险" in sample_headers else -1
        coder_column = sample_headers.index("编码者编号") if "编码者编号" in sample_headers else -1
        adjudication_column = sample_headers.index("裁决状态") if "裁决状态" in sample_headers else -1
        native_value_column = sample_headers.index("原生评分值") if "原生评分值" in sample_headers else -1
        native_minimum_column = sample_headers.index("原生量表下界") if "原生量表下界" in sample_headers else -1
        native_maximum_column = sample_headers.index("原生量表上界") if "原生量表上界" in sample_headers else -1
        native_normalized_column = sample_headers.index("原生评分标准化值") if "原生评分标准化值" in sample_headers else -1
        semantic_review_required_rows = 0
        semantic_low_confidence_rows = 0
        semantic_figurative_rows = 0
        semantic_lexicon_zero_hit_rows = 0
        semantic_coding_candidate_rows = 0
        semantic_coding_unresolved_rows = 0
        for row_number, row in enumerate(sample_rows, start=2):
            scored = (
                scoring_column >= 0
                and len(row) > scoring_column
                and row[scoring_column].strip().lower() in {"true", "1", "yes", "是"}
            )
            formal_candidate = (
                candidate_column >= 0
                and len(row) > candidate_column
                and row[candidate_column].strip().lower() in {"true", "1", "yes", "是"}
            )
            status = row[semantic_status_column] if semantic_status_column >= 0 and len(row) > semantic_status_column else ""
            method = row[semantic_method_column] if semantic_method_column >= 0 and len(row) > semantic_method_column else ""
            confidence_text = row[semantic_confidence_column] if semantic_confidence_column >= 0 and len(row) > semantic_confidence_column else ""
            reliability_text = row[evidence_reliability_column] if evidence_reliability_column >= 0 and len(row) > evidence_reliability_column else ""
            aspect_text = row[aspect_confidence_column] if aspect_confidence_column >= 0 and len(row) > aspect_confidence_column else ""
            weight_text = row[aggregation_weight_column] if aggregation_weight_column >= 0 and len(row) > aggregation_weight_column else ""
            semantic_unit = row[semantic_unit_column] if semantic_unit_column >= 0 and len(row) > semantic_unit_column else ""
            semantic_final_text = row[semantic_final_column] if semantic_final_column >= 0 and len(row) > semantic_final_column else ""
            sentiment_score_text = row[sentiment_score_column] if sentiment_score_column >= 0 and len(row) > sentiment_score_column else ""
            trace = row[semantic_trace_column] if semantic_trace_column >= 0 and len(row) > semantic_trace_column else ""
            language = row[semantic_language_column] if semantic_language_column >= 0 and len(row) > semantic_language_column else ""
            lexicon_status = row[lexicon_status_column] if lexicon_status_column >= 0 and len(row) > lexicon_status_column else ""
            lexicon_trace = row[lexicon_trace_column] if lexicon_trace_column >= 0 and len(row) > lexicon_trace_column else ""
            coding_status = row[coding_status_column] if coding_status_column >= 0 and len(row) > coding_status_column else ""
            coding_candidates = row[coding_candidates_column] if coding_candidates_column >= 0 and len(row) > coding_candidates_column else ""
            coding_trace = row[coding_trace_column] if coding_trace_column >= 0 and len(row) > coding_trace_column else ""
            open_code = row[open_code_column] if open_code_column >= 0 and len(row) > open_code_column else ""
            figurative = row[figurative_column].strip().lower() if figurative_column >= 0 and len(row) > figurative_column else ""
            coder = row[coder_column] if coder_column >= 0 and len(row) > coder_column else ""
            adjudication = row[adjudication_column] if adjudication_column >= 0 and len(row) > adjudication_column else ""
            if status == "review_required":
                semantic_review_required_rows += 1
            try:
                confidence = finite_number(confidence_text) if confidence_text else None
            except ValueError:
                confidence = None
            try:
                reliability = finite_number(reliability_text) if reliability_text else None
            except ValueError:
                reliability = None
            try:
                aspect_confidence = finite_number(aspect_text) if aspect_text else None
            except ValueError:
                aspect_confidence = None
            try:
                aggregation_weight = finite_number(weight_text) if weight_text else None
            except ValueError:
                aggregation_weight = None
            if confidence is not None and confidence < SEMANTIC_CONFIDENCE_THRESHOLD:
                semantic_low_confidence_rows += 1
            if figurative in {"true", "1", "yes", "是"}:
                semantic_figurative_rows += 1
            if lexicon_status == "zero_hit":
                semantic_lexicon_zero_hit_rows += 1
            if coding_status == "candidate_generated":
                semantic_coding_candidate_rows += 1
            elif coding_status == "started_unresolved":
                semantic_coding_unresolved_rows += 1
            if language and language not in {"zh", "en", "mixed", "unknown"}:
                errors.append(f"worksheet 样本明细 row {row_number} has an invalid semantic language")
            if lexicon_status and lexicon_status not in {"full_match", "dimension_only", "polarity_only", "zero_hit", "not_applicable"}:
                errors.append(f"worksheet 样本明细 row {row_number} has an invalid lexicon match status")
            if coding_status and coding_status not in {"not_needed", "candidate_generated", "started_unresolved", "not_applicable"}:
                errors.append(f"worksheet 样本明细 row {row_number} has an invalid coding parse status")
            for value, expected, label_name in (
                (lexicon_trace, dict, "lexicon match trace"),
                (coding_candidates, list, "coding parse candidates"),
                (coding_trace, dict, "coding parse trace"),
            ):
                if not value:
                    continue
                try:
                    if not isinstance(json.loads(value), expected):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    errors.append(f"worksheet 样本明细 row {row_number} has a non-JSON {label_name}")
            if method == "coding_parse_fallback":
                if formal_candidate:
                    errors.append(f"worksheet 样本明细 row {row_number} scores an unresolved coding-parse fallback")
                if status != "review_required":
                    errors.append(f"worksheet 样本明细 row {row_number} coding-parse fallback is not review_required")
                if not re.fullmatch(r"OPEN-[0-9a-f]{12}", open_code):
                    errors.append(f"worksheet 样本明细 row {row_number} coding-parse fallback lacks a stable open-code ID")
            if formal_candidate and method not in {
                "source_native_numeric", "rule_codebook", "assisted_semantic", "manual_code"
            }:
                errors.append(f"worksheet 样本明细 row {row_number} scores a row without a valid quantification method")
            if formal_candidate and method == "source_native_numeric":
                native_values = []
                for column in (
                    native_value_column,
                    native_minimum_column,
                    native_maximum_column,
                    native_normalized_column,
                ):
                    native_values.append(row[column] if column >= 0 and len(row) > column else "")
                try:
                    native_value, native_minimum, native_maximum, native_normalized = map(float, native_values)
                    expected_native = -5 + 10 * (native_value - native_minimum) / (native_maximum - native_minimum)
                    if native_maximum <= native_minimum or not native_minimum <= native_value <= native_maximum:
                        raise ValueError
                    if abs(native_normalized - expected_native) > 0.01:
                        raise ValueError
                except (ValueError, ZeroDivisionError):
                    errors.append(f"worksheet 样本明细 row {row_number} has invalid source-native value, scale, or normalization")
            if formal_candidate and method != "source_native_numeric":
                if status not in {"auto_eligible", "human_confirmed"}:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text without an accepted review status")
                if not semantic_unit:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text without an analysis unit")
                if confidence is None or confidence < SEMANTIC_CONFIDENCE_THRESHOLD:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text below the confidence threshold")
                if reliability is None or reliability < EVIDENCE_RELIABILITY_THRESHOLD:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text below the evidence-reliability threshold")
                if aspect_confidence is None or aspect_confidence < ASPECT_CONFIDENCE_THRESHOLD:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text below the aspect-confidence threshold")
                if aggregation_weight is None:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text without an aggregation weight")
                elif confidence is not None and reliability is not None and abs(
                    aggregation_weight - confidence * reliability
                ) > 0.02:
                    errors.append(f"worksheet 样本明细 row {row_number} has an aggregation weight inconsistent with confidence × reliability")
                if not trace:
                    errors.append(f"worksheet 样本明细 row {row_number} scores semantic text without a rule trace")
                else:
                    try:
                        if not isinstance(json.loads(trace), list):
                            raise ValueError
                    except (json.JSONDecodeError, ValueError):
                        errors.append(f"worksheet 样本明细 row {row_number} has a non-JSON semantic rule trace")
                try:
                    if semantic_final_text and sentiment_score_text and int(finite_number(semantic_final_text)) != int(finite_number(sentiment_score_text)):
                        errors.append(f"worksheet 样本明细 row {row_number} has inconsistent semantic and sentiment integer scores")
                except ValueError:
                    errors.append(f"worksheet 样本明细 row {row_number} has a non-numeric semantic or sentiment score")
                if figurative in {"true", "1", "yes", "是"} and status != "human_confirmed":
                    errors.append(f"worksheet 样本明细 row {row_number} scores figurative-risk text without human confirmation")
                if status == "human_confirmed" and not (coder or adjudication):
                    errors.append(f"worksheet 样本明细 row {row_number} is human-confirmed without coder or adjudication evidence")
        search_matrix = matrices.get("检索日志", [])
        search_headers = search_matrix[0] if search_matrix else []
        search_rows = [row for row in search_matrix[1:] if any(str(value).strip() for value in row)] if search_matrix else []
        search_id_column = search_headers.index("检索记录编号") if "检索记录编号" in search_headers else -1
        search_status_column = search_headers.index("检索状态") if "检索状态" in search_headers else -1
        search_execution_id_column = search_headers.index("执行编号") if "执行编号" in search_headers else -1
        workbook_search_fields = {
            "任务运行编号": "task_run_id",
            "检索记录编号": "search_id",
            "迭代轮次": "iteration_round",
            "缺口目标": "gap_target",
            "关联维度目标": "query_dimension_targets",
            "迭代模式": "iteration_mode",
            "检索日期": "retrieved_at",
            "检索工具或入口": "search_tool",
            "实际检索词": "query",
            "目标来源类型": "source_category_target",
            "本次新增实际计分证据": "new_scored_evidence_units",
            "检索状态": "status",
            "下一动作": "next_action",
            "记录类型": "record_type",
            "计划编号": "plan_id",
            "执行编号": "execution_id",
            "查询编号": "query_id",
            "执行开始时间": "started_at",
            "执行结束时间": "finished_at",
            "规范查询意图": "normalized_query_intent",
            "重试序号": "retry_number",
            "重试原因": "retry_reason",
            "原执行编号": "original_execution_id",
            "重试间隔策略": "retry_interval_policy",
            "目标平台": "target_platform_id",
            "检索路径": "retrieval_route",
            "正负路径": "polarity",
            "目标主体": "target_subject",
            "目标时间情境": "target_time_range",
            "查询语言": "language",
        }
        header_indexes = {header: index for index, header in enumerate(search_headers)}
        search_records = [
            {
                field: (row[header_indexes[header]] if len(row) > header_indexes[header] else "")
                for header, field in workbook_search_fields.items()
                if header in header_indexes
            }
            for row in search_rows
        ]
        template_only = (
            minimum_pages == 0 and not search_records and not execution_records
            and not expected_source_ids and not scored_sample_rows
        )
        search_validation = (
            {"valid_rows": [], "valid_execution_ids": [], "invalid_records": []}
            if template_only else validate_execution_records(
                execution_records or [], schema_context=execution_schema_context)
        )
        original_by_id = {str(row['execution_id']): row for row in search_validation['valid_rows']}
        for displayed in search_records:
            original = original_by_id.get(str(displayed.get('execution_id', '')))
            if original is not None:
                for field, value in displayed.items():
                    if display_value(field, str(value)) != display_value(field, str(original.get(field, ''))):
                        errors.append('workbook execution field mismatch: ' + field + ':' + str(displayed.get('execution_id')))
        if not template_only:
            displayed_ids = [str(row.get('execution_id', '')) for row in search_records]
            expected_ids = [str(row.get('execution_id', '')) for row in (execution_records or [])]
            if sorted(displayed_ids) != sorted(expected_ids):
                errors.append('workbook execution record set mismatch')
        if search_validation["invalid_records"]:
            errors.append("workbook search log contains invalid executed query records")
        raw_by_execution_id: dict[str, list[str]] = {}
        for row in search_rows:
            if search_execution_id_column >= 0 and len(row) > search_execution_id_column:
                raw_by_execution_id.setdefault(row[search_execution_id_column], row)
        executed_search_rows = [
            raw_by_execution_id[execution_id]
            for execution_id in search_validation["valid_execution_ids"]
            if execution_id in raw_by_execution_id
        ]
        search_ids = {
            row[search_id_column]
            for row in executed_search_rows
            if search_id_column >= 0 and len(row) > search_id_column and row[search_id_column]
        }
        if not search_ids and minimum_pages > 0:
            errors.append("workbook search log contains no executed queries")
        iteration_column = search_headers.index("迭代轮次") if "迭代轮次" in search_headers else -1
        action_column = search_headers.index("下一动作") if "下一动作" in search_headers else -1
        new_samples_column = search_headers.index("本次新增实际计分证据") if "本次新增实际计分证据" in search_headers else -1
        source_category_column = search_headers.index("目标来源类型") if "目标来源类型" in search_headers else -1
        deep_rounds: set[int] = set()
        actions_by_round: dict[int, set[str]] = {}
        statuses_by_round: dict[int, set[str]] = {}
        new_samples_by_round: dict[int, int] = {}
        query_count_by_round: dict[int, int] = {}
        attempted_source_categories: set[str] = set()
        for row in executed_search_rows:
            if source_category_column >= 0 and len(row) > source_category_column:
                category = row[source_category_column]
                if category and category != "mixed":
                    attempted_source_categories.add(category)
            if iteration_column < 0 or len(row) <= iteration_column:
                continue
            try:
                round_number = int(finite_number(row[iteration_column]))
            except ValueError:
                continue
            if round_number > 0:
                deep_rounds.add(round_number)
                query_count_by_round[round_number] = query_count_by_round.get(round_number, 0) + 1
            if action_column >= 0 and len(row) > action_column:
                actions_by_round.setdefault(round_number, set()).add(row[action_column])
            if search_status_column >= 0 and len(row) > search_status_column:
                statuses_by_round.setdefault(round_number, set()).add(row[search_status_column])
            if new_samples_column >= 0 and len(row) > new_samples_column:
                try:
                    added = int(finite_number(row[new_samples_column]))
                except ValueError:
                    added = 0
                new_samples_by_round[round_number] = new_samples_by_round.get(round_number, 0) + added
        deepest_round = max(deep_rounds, default=0)
        target_met = scored_sample_rows >= minimum_effective_samples
        latest_two_rounds = sorted(deep_rounds)[-2:]
        low_yield_exhaustion = (
            len(latest_two_rounds) == 2
            and all(new_samples_by_round.get(round_number, 0) < 5 for round_number in latest_two_rounds)
        )
        blocked_or_empty_exhaustion = bool(deepest_round) and bool(statuses_by_round.get(deepest_round)) and all(
            status in {"blocked", "no_results"}
            for status in statuses_by_round[deepest_round]
        )
        deep_query_depth_met = bool(deep_rounds) and all(
            query_count_by_round.get(round_number, 0) >= 8 for round_number in deep_rounds
        )
        exhaustion_supported = (
            (low_yield_exhaustion or blocked_or_empty_exhaustion)
            and deep_query_depth_met
            and len(attempted_source_categories) >= 10
        )
        exhausted = (
            deepest_round > 0
            and "exhausted" in actions_by_round.get(deepest_round, set())
            and exhaustion_supported
        )
        if minimum_effective_samples > 0 and not target_met:
            bounded_terminal = (
                audit_status == "retrieval_terminated_with_shortfall"
                and bool(retrieval_termination_status)
            )
            if not bounded_terminal and (len(deep_rounds) < 3 or not exhausted):
                errors.append(
                    "effective scoring samples are below target without three substantive deep-search rounds, adequate source breadth, and a supported exhaustion record"
                )
            else:
                warnings.append(
                    f"only {scored_sample_rows} effective scoring samples; target is {minimum_effective_samples}, but a machine-audited terminal retrieval state is recorded"
                )
        dimension_workbook_audit: dict[str, object] = {
            "errors": [],
            "warnings": [],
            "metrics": {
                "minimum_dimension_confidence": "中",
                "dimensions": {},
                "dimensions_at_or_above_medium": 0,
                "dimensions_independently_exhausted": 0,
            },
        }
        if minimum_pages > 0:
            active_source_ids = None
            if source_records is not None:
                from execution_facts import validate_source_links
                linked = validate_source_links(source_records, search_validation)
                if linked['errors']:
                    errors.append('workbook source/execution binding invalid')
                active_source_ids = {row['source_id'] for row in linked['valid_sources']}
            dimension_workbook_audit = workbook_dimension_evidence_audit(matrices, active_source_ids)
            errors.extend(dimension_workbook_audit["errors"])
            warnings.extend(dimension_workbook_audit["warnings"])
        run_ids: set[str] = set()
        for matrix, header_name in (
            (sample_matrix, "任务运行编号"),
            (source_matrix, "任务运行编号"),
            (search_matrix, "任务运行编号"),
        ):
            if not matrix or header_name not in matrix[0]:
                continue
            column = matrix[0].index(header_name)
            run_ids.update(
                row[column]
                for row in matrix[1:]
                if len(row) > column and row[column]
            )
        if minimum_pages > 0 and len(run_ids) != 1:
            errors.append(f"workbook must contain exactly one task run ID; found {sorted(run_ids)}")
        if minimum_pages > 0 and formula_count < 20:
            errors.append(f"workbook contains only {formula_count} formulas; expected at least 20")
        elif minimum_pages == 0 and formula_count < 20:
            warnings.append(
                f"blank workbook template contains {formula_count} formulas; populated deliverables must contain at least 20"
            )
        chart_parts = [
            name for name in archive.namelist()
            if name.startswith("xl/charts/chart") and name.endswith(".xml")
        ]
        chart_count = len(chart_parts)
        if chart_count < 1:
            errors.append("workbook contains no seven-dimension chart")
        else:
            chart_blank_modes: list[str] = []
            for name in chart_parts:
                node = ET.fromstring(archive.read(name))
                blank_mode = node.find(
                    ".//{http://schemas.openxmlformats.org/drawingml/2006/chart}dispBlanksAs"
                )
                # OOXML omits this element for the Excel default, which is a gap.
                chart_blank_modes.append(
                    "gap" if blank_mode is None else blank_mode.attrib.get("val", "")
                )
            if any(mode != "gap" for mode in chart_blank_modes):
                errors.append("seven-dimension chart must preserve missing scores as gaps, not zero")
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "sheet_names": sheet_names,
            "source_page_rows": len(source_ids),
            "search_log_rows": len(search_ids),
            "scored_sample_rows": scored_sample_rows,
            "semantic_review_required_rows": semantic_review_required_rows,
            "semantic_low_confidence_rows": semantic_low_confidence_rows,
            "semantic_figurative_risk_rows": semantic_figurative_rows,
            "semantic_lexicon_zero_hit_rows": semantic_lexicon_zero_hit_rows,
            "semantic_coding_candidate_rows": semantic_coding_candidate_rows,
            "semantic_coding_unresolved_rows": semantic_coding_unresolved_rows,
            "minimum_effective_sample_target": minimum_effective_samples,
            "minimum_effective_sample_met": target_met,
            "effective_sample_shortfall": max(0, minimum_effective_samples - scored_sample_rows),
            "deep_iteration_rounds": len(deep_rounds),
            "deep_iteration_query_depth_met": deep_query_depth_met,
            "exhaustion_evidence_met": exhaustion_supported,
            "iteration_outcome": (
                "target_met"
                if target_met
                else "retrieval_terminated_with_shortfall"
                if audit_status == "retrieval_terminated_with_shortfall"
                else "exhausted_with_shortfall"
                if exhausted and len(deep_rounds) >= 3
                else "continue_deep_search"
            ),
            "task_run_id": next(iter(run_ids), "") if len(run_ids) == 1 else "",
            "formula_count": formula_count,
            "chart_count": chart_count,
            "header_styles": header_styles,
            "dimension_evidence": dimension_workbook_audit["metrics"],
        },
    }


def paragraph_text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.findall(".//w:t", DOCX_NS))


def paragraph_style(paragraph: ET.Element) -> str:
    node = paragraph.find("w:pPr/w:pStyle", DOCX_NS)
    return node.attrib.get(f"{{{DOCX_NS['w']}}}val", "") if node is not None else ""


def table_matrix(table: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall("w:tr", DOCX_NS):
        rows.append(
            [
                "".join(node.text or "" for node in cell.findall(".//w:t", DOCX_NS))
                for cell in row.findall("w:tc", DOCX_NS)
            ]
        )
    return rows


def direct_body_blocks(root: ET.Element) -> list[dict[str, object]]:
    body = root.find("w:body", DOCX_NS)
    if body is None:
        return []
    paragraph_tag = f"{{{DOCX_NS['w']}}}p"
    table_tag = f"{{{DOCX_NS['w']}}}tbl"
    blocks: list[dict[str, object]] = []
    for child in list(body):
        if child.tag == paragraph_tag:
            blocks.append(
                {
                    "kind": "paragraph",
                    "text": paragraph_text(child),
                    "style": paragraph_style(child),
                    "element": child,
                }
            )
        elif child.tag == table_tag:
            matrix = table_matrix(child)
            blocks.append(
                {
                    "kind": "table",
                    "text": "".join("".join(row) for row in matrix),
                    "style": "",
                    "element": child,
                    "matrix": matrix,
                }
            )
    return blocks


def section_block_text(blocks: list[dict[str, object]], start_heading: str, end_heading: str) -> str:
    start = next(
        (
            index
            for index, block in enumerate(blocks)
            if block["kind"] == "paragraph"
            and block["text"] == start_heading
            and block["style"] in {"Heading1", "1"}
        ),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index, block in enumerate(blocks[start + 1 :], start=start + 1)
            if block["kind"] == "paragraph"
            and block["text"] == end_heading
            and block["style"] in {"Heading1", "1"}
        ),
        len(blocks),
    )
    return "\n".join(str(block["text"]) for block in blocks[start:end])


def document_layout(root: ET.Element) -> dict[str, int | None]:
    body = root.find("w:body", DOCX_NS)
    section = body.find("w:sectPr", DOCX_NS) if body is not None else None
    page_size = section.find("w:pgSz", DOCX_NS) if section is not None else None
    margins = section.find("w:pgMar", DOCX_NS) if section is not None else None
    key = f"{{{DOCX_NS['w']}}}"
    def integer(node: ET.Element | None, name: str) -> int | None:
        if node is None:
            return None
        value = node.attrib.get(f"{key}{name}")
        return int(value) if value and value.isdigit() else None
    return {
        "page_width_twips": integer(page_size, "w"),
        "page_height_twips": integer(page_size, "h"),
        "top_margin_twips": integer(margins, "top"),
        "bottom_margin_twips": integer(margins, "bottom"),
        "left_margin_twips": integer(margins, "left"),
        "right_margin_twips": integer(margins, "right"),
        "header_distance_twips": integer(margins, "header"),
        "footer_distance_twips": integer(margins, "footer"),
    }


def cjk_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def normalized_narrative(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)
    return "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z]", text)).casefold()


def narrative_similarity(left: str, right: str) -> tuple[float, float]:
    a = normalized_narrative(left)
    b = normalized_narrative(right)
    if not a or not b:
        return 0.0, 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    a_grams = {a[index : index + 5] for index in range(max(0, len(a) - 4))}
    b_grams = {b[index : index + 5] for index in range(max(0, len(b) - 4))}
    union = a_grams | b_grams
    return ratio, len(a_grams & b_grams) / len(union) if union else 0.0


def english_gloss_violations(text: str) -> list[str]:
    cleaned = re.sub(r"https?://\S+|\b[\w.-]+@[\w.-]+\b", " ", text)
    cleaned = re.sub(
        r"任务运行编号(?:为|[:：])?\s*[A-Za-z][A-Za-z0-9._-]*",
        "任务运行编号",
        cleaned,
    )
    cleaned = re.sub(
        r"(?<![A-Za-z0-9])(?=[A-Za-z0-9._-]*\d)[A-Za-z][A-Za-z0-9._-]*(?![A-Za-z0-9])",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\b[A-Za-z]+_[A-Za-z0-9_-]+\b", " ", cleaned)
    violations: list[str] = []
    for match in ENGLISH_TERM_PATTERN.finditer(cleaned):
        term = match.group(1).strip()
        if re.fullmatch(r"[A-E](?:\s*[-—]\s*[A-E])?", term, flags=re.IGNORECASE):
            continue
        after = cleaned[match.end() : match.end() + 80]
        if re.match(r"\s*[（(][^）)]*[\u3400-\u4dbf\u4e00-\u9fff][^）)]*[）)]", after):
            continue
        before = cleaned[: match.start()]
        open_index = max(before.rfind("（"), before.rfind("("))
        close_index = max(before.rfind("）"), before.rfind(")"))
        if open_index > close_index:
            preceding = before[max(0, open_index - 30) : open_index]
            if cjk_count(preceding) > 0 and re.search(r"[）)]", after):
                continue
        violations.append(term)
    return violations


def explicit_docx_fonts(archive: zipfile.ZipFile, document_root: ET.Element) -> set[str]:
    used_style_ids = {
        node.attrib.get(f"{{{DOCX_NS['w']}}}val", "")
        for node in document_root.findall(".//w:pPr/w:pStyle", DOCX_NS)
    }
    font_nodes: list[ET.Element] = list(document_root.findall(".//w:rFonts", DOCX_NS))
    for name in archive.namelist():
        if not re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
            continue
        part = ET.fromstring(archive.read(name))
        font_nodes.extend(part.findall(".//w:rFonts", DOCX_NS))
    styles_root = ET.fromstring(archive.read("word/styles.xml"))
    for style in styles_root.findall("w:style", DOCX_NS):
        style_id = style.attrib.get(f"{{{DOCX_NS['w']}}}styleId", "")
        is_default = style.attrib.get(f"{{{DOCX_NS['w']}}}default") == "1"
        if style_id in used_style_ids or is_default:
            font_nodes.extend(style.findall(".//w:rFonts", DOCX_NS))
    values: set[str] = set()
    for node in font_nodes:
        for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
            value = node.attrib.get(f"{{{DOCX_NS['w']}}}{attribute}", "").strip()
            if value:
                values.add(value)
    return values


def audit_dimension_narratives(
    paragraphs: list[ET.Element], texts: list[str], styles: list[str]
) -> dict[str, object]:
    errors: list[str] = []
    metrics: dict[str, object] = {
        "dimension_sections_checked": 0,
        "dimension_required_subheadings": 0,
        "dimension_required_labels": 0,
        "dimension_hyperlinks": 0,
        "dimension_representative_hyperlinks": 0,
        "dimension_narrative_chinese_characters": 0,
        "dimension_evidence_summary": {},
    }
    try:
        section_end = next(
            index
            for index, (text, style) in enumerate(zip(texts, styles))
            if text == "6. 七维评价汇总表" and style in {"Heading1", "1"}
        )
    except StopIteration:
        section_end = len(texts)
    for number, dimension in enumerate(DIMENSIONS, start=1):
        title = f"5.{number} {dimension}"
        try:
            start = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == title and style in {"Heading2", "2"}
            ) + 1
        except StopIteration:
            errors.append(f"dimension narrative missing heading: {title}")
            continue
        if number < len(DIMENSIONS):
            next_title = f"5.{number + 1} {DIMENSIONS[number]}"
            try:
                end = next(
                    index
                    for index, (text, style) in enumerate(zip(texts, styles))
                    if text == next_title and style in {"Heading2", "2"}
                )
            except StopIteration:
                end = section_end
        else:
            end = section_end
        segment_texts = texts[start:end]
        segment_styles = styles[start:end]
        segment_paragraphs = paragraphs[start:end]
        metrics["dimension_sections_checked"] = int(metrics["dimension_sections_checked"]) + 1
        heading3_positions = [
            index
            for index, style in enumerate(segment_styles)
            if style in {"Heading3", "3"}
        ]
        if len(heading3_positions) != 3:
            errors.append(f"{title} must contain exactly three Heading 3 narrative modules")
        else:
            required_positions = heading3_positions
            heading3_texts = [segment_texts[index] for index in required_positions]
            if heading3_texts[:2] != DIMENSION_FIXED_SUBHEADINGS:
                errors.append(
                    f"{title} first two Heading 3 modules must be {DIMENSION_FIXED_SUBHEADINGS}; found {heading3_texts[:2]}"
                )
            if not heading3_texts[2].strip() or cjk_count(heading3_texts[2]) > 14:
                errors.append(f"{title} third Heading 3 must be a concise evidence-based difference or context title")
            metrics["dimension_required_subheadings"] = int(
                metrics["dimension_required_subheadings"]
            ) + 3
            positive_text = "".join(segment_texts[required_positions[0] + 1 : required_positions[1]])
            negative_text = "".join(segment_texts[required_positions[1] + 1 : required_positions[2]])
            summary_start = next(
                (
                    index
                    for index, text in enumerate(segment_texts[required_positions[2] + 1 :], start=required_positions[2] + 1)
                    if text.startswith("高频主题：")
                ),
                len(segment_texts),
            )
            difference_texts = [
                text for text in segment_texts[required_positions[2] + 1 : summary_start] if text.strip()
            ]
            if cjk_count(positive_text) < MIN_POSITIVE_CJK:
                errors.append(f"{title} positive narrative is too short; at least {MIN_POSITIVE_CJK} Chinese characters required")
            if cjk_count(negative_text) < MIN_NEGATIVE_CJK:
                errors.append(f"{title} negative narrative is too short; at least {MIN_NEGATIVE_CJK} Chinese characters required")
            if len(difference_texts) < MIN_DIFFERENCE_ITEMS or cjk_count("".join(difference_texts)) < MIN_COMBINED_DIFFERENCE_CJK:
                errors.append(
                    f"{title} third narrative module and fact-perception comparison require at least {MIN_DIFFERENCE_ITEMS} substantive paragraphs and {MIN_COMBINED_DIFFERENCE_CJK} Chinese characters"
                )
        segment_full_text = "\n".join(segment_texts)
        segment_cjk = cjk_count(segment_full_text)
        if segment_cjk < MIN_DIMENSION_CJK:
            errors.append(
                f"{title} detailed narrative contains only {segment_cjk} Chinese characters; at least {MIN_DIMENSION_CJK} required"
            )
        metrics["dimension_narrative_chinese_characters"] = int(
            metrics["dimension_narrative_chinese_characters"]
        ) + segment_cjk
        for label in DIMENSION_LABELS:
            occurrences = sum(text.count(f"{label}：") for text in segment_texts)
            if occurrences != 1:
                errors.append(f"{title} must contain exactly one labeled line: {label}")
            elif not any(
                re.search(rf"{re.escape(label)}：[^\n]+", text)
                for text in segment_texts
            ):
                errors.append(f"{title} labeled line is empty: {label}")
            else:
                metrics["dimension_required_labels"] = int(metrics["dimension_required_labels"]) + 1
        depth_contracts = {
            "作用机制": (120, ("因为", "由于", "通过", "使得", "导致", "从而", "机制", "路径")),
            "反证与替代解释": (100, ("但", "然而", "反例", "替代解释", "也可能", "不能排除", "相反")),
            "不确定性与适用边界": (100, ("样本", "来源", "平台", "时间", "边界", "仅限", "不能外推", "不确定")),
            "综合判断": (120, ("因此", "综合", "据此", "判断", "表明", "结论")),
        }
        for label, (minimum_cjk, markers) in depth_contracts.items():
            match = re.search(rf"{re.escape(label)}：([^\n]+)", segment_full_text)
            value = match.group(1).strip() if match else ""
            if cjk_count(value) < minimum_cjk:
                errors.append(f"{title} {label} contains fewer than {minimum_cjk} Chinese characters")
            if value and not any(marker in value for marker in markers):
                errors.append(f"{title} {label} lacks a required analytical reasoning relationship")
        if not re.search(r"提及率等级：[A-EU]，", segment_full_text):
            errors.append(f"{title} mention level must use A-E or U and include its range/basis")
        definition_match = re.search(r"维度定义：([^\n]+)", segment_full_text)
        boundary_match = re.search(r"编码边界：([^\n]+)", segment_full_text)
        if not definition_match or definition_match.group(1).strip() != DIMENSION_DEFINITIONS[dimension]:
            errors.append(f"{title} definition differs from the canonical framework")
        if not boundary_match or boundary_match.group(1).strip() != DIMENSION_BOUNDARIES[dimension]:
            errors.append(f"{title} coding boundary differs from the canonical framework")
        weight_match = re.search(
            r"初始研究权重：(10%|15%|20%)(?=跨平台等权倾向值：|\s|$)",
            segment_full_text,
        )
        expected_weight_text = f"{DIMENSION_WEIGHTS[dimension]:.0%}"
        if not weight_match or weight_match.group(1) != expected_weight_text:
            errors.append(f"{title} initial research weight must be {expected_weight_text}")
        tendency_match = re.search(
            r"跨平台等权倾向值：(数据不足|[+-]?(?:[0-4](?:\.\d+)?|5(?:\.0+)?))"
            r"(?=跨平台等权维度得分：|\s|$)",
            segment_full_text,
        )
        conversion_match = re.search(
            r"跨平台等权维度得分：(数据不足|(?:100|\d{1,2})(?:\.\d+)?分)", segment_full_text
        )
        if not tendency_match:
            errors.append(f"{title} tendency score is missing or malformed")
        if not conversion_match:
            errors.append(f"{title} conversion score is missing or malformed")
        if tendency_match and conversion_match:
            tendency_value = tendency_match.group(1)
            conversion_value = conversion_match.group(1)
            if (tendency_value == "数据不足") != (conversion_value == "数据不足"):
                errors.append(f"{title} tendency and conversion scores must both be present or both be data insufficient")
            elif tendency_value != "数据不足":
                expected_conversion = (float(tendency_value) + 5) * 10
                actual_conversion = float(conversion_value[:-1])
                if abs(actual_conversion - expected_conversion) > 1e-9:
                    errors.append(f"{title} conversion score does not match the tendency score")
        if not re.search(r"证据充分度：(高|中高|中|低|数据不足)", segment_full_text):
            errors.append(f"{title} evidence strength is missing or malformed")
        retrieved_match = re.search(r"检索有效证据数：(\d+)", segment_full_text)
        topic_match = re.search(r"主题覆盖证据数：(\d+)", segment_full_text)
        eligible_match = re.search(r"评分候选证据数：(\d+)", segment_full_text)
        evidence_match = re.search(r"实际计分证据数：(\d+)", segment_full_text)
        direction_match = re.search(r"计分正中负构成：正面(\d+)、中性(\d+)、负面(\d+)", segment_full_text)
        platform_match = re.search(
            r"有效计分平台：(\d+)/(\d+)；候选平台(\d+)；(已计分|平台样本不足)",
            segment_full_text,
        )
        source_match = re.search(r"独立来源页数：(\d+)", segment_full_text)
        category_match = re.search(r"来源类型数：(\d+)", segment_full_text)
        confidence_match = re.search(
            r"维度置信度：(高|中高|中|低|数据不足)", segment_full_text
        )
        status_match = re.search(
            r"维度证据状态：(达到中高或高置信度优先目标|达到明确选择的中置信度目标|达到中置信度并通过增强终止独立审计|检索按受控条件终止，保留符合最低正式门槛的结果并披露缺口|经独立审计确认穷尽后仍低于最低要求)",
            segment_full_text,
        )
        rounds_match = re.search(
            r"定向深检轮次：(.*?)(?=定向深检轮数：)", segment_full_text
        )
        round_count_match = re.search(
            r"定向深检轮数：(\d+)(?=独立穷尽审计：)", segment_full_text
        )
        exhaustion_match = re.search(
            r"独立穷尽审计：(通过|不适用)(?=中置信度终止审计：)", segment_full_text
        )
        medium_audit_match = re.search(
            r"中置信度终止审计：(通过|不适用)(?=中置信度终止依据：)", segment_full_text
        )
        medium_basis_match = re.search(
            r"中置信度终止依据：([^\n]+)(?=代表性来源：)", segment_full_text
        )
        if not all(
            (
                evidence_match,
                retrieved_match,
                topic_match,
                eligible_match,
                direction_match,
                platform_match,
                source_match,
                category_match,
                confidence_match,
                status_match,
                rounds_match,
                round_count_match,
                exhaustion_match,
                medium_audit_match,
                medium_basis_match,
            )
        ):
            errors.append(f"{title} dimension evidence audit summary is malformed")
        else:
            rounds_text = rounds_match.group(1).strip()
            if rounds_text == "未触发或无需触发":
                rounds: list[int] = []
            elif re.fullmatch(r"\d+(?:、\d+)*", rounds_text):
                rounds = [int(value) for value in rounds_text.split("、")]
            else:
                rounds = []
                errors.append(f"{title} targeted deep-search rounds are malformed")
            if int(round_count_match.group(1)) != len(rounds):
                errors.append(f"{title} targeted deep-search round count is inconsistent")
            dimension_summary = metrics["dimension_evidence_summary"]
            assert isinstance(dimension_summary, dict)
            dimension_summary[dimension] = {
                "initial_research_weight": DIMENSION_WEIGHTS[dimension],
                "retrieved_evidence_units": int(retrieved_match.group(1)),
                "topic_coverage_evidence_units": int(topic_match.group(1)),
                "eligible_evidence_units": int(eligible_match.group(1)),
                "scored_evidence_units": int(evidence_match.group(1)),
                "evidence_units": int(evidence_match.group(1)),
                "scoring_positive_units": int(direction_match.group(1)),
                "scoring_neutral_units": int(direction_match.group(2)),
                "scoring_negative_units": int(direction_match.group(3)),
                "valid_scoring_platforms": int(platform_match.group(1)),
                "dimension_scorable_platform_count": int(platform_match.group(1)),
                "run_included_platform_count": int(platform_match.group(2)),
                "dimension_candidate_platform_count": int(platform_match.group(3)),
                "platform_minimum_rule_status": (
                    "scored" if platform_match.group(4) == "已计分"
                    else "insufficient_platform_samples"
                ),
                "tendency_score": None if tendency_match.group(1) == "数据不足" else float(tendency_match.group(1)),
                "conversion_score": None if conversion_match.group(1) == "数据不足" else float(conversion_match.group(1)[:-1]),
                "source_pages": int(source_match.group(1)),
                "source_categories": int(category_match.group(1)),
                "confidence": confidence_match.group(1),
                "status": status_match.group(1),
                "targeted_rounds": rounds,
                "targeted_round_count": int(round_count_match.group(1)),
                "independent_exhaustion_audit_passed": exhaustion_match.group(1) == "通过",
                "medium_completion_audit_passed": medium_audit_match.group(1) == "通过",
                "medium_completion_basis": medium_basis_match.group(1).strip(),
            }
        hyperlink_count = sum(
            len(paragraph.findall(".//w:hyperlink", DOCX_NS)) for paragraph in segment_paragraphs
        )
        metrics["dimension_hyperlinks"] = int(metrics["dimension_hyperlinks"]) + hyperlink_count
        if hyperlink_count < 1:
            errors.append(f"{title} must contain at least one clickable representative source")
        representative_paragraphs = [
            paragraph
            for paragraph, text in zip(segment_paragraphs, segment_texts)
            if "代表性来源：" in text
        ]
        representative_hyperlink_count = sum(
            len(paragraph.findall(".//w:hyperlink", DOCX_NS))
            for paragraph in representative_paragraphs
        )
        metrics["dimension_representative_hyperlinks"] = int(
            metrics["dimension_representative_hyperlinks"]
        ) + representative_hyperlink_count
        if representative_hyperlink_count < 1:
            errors.append(f"{title} representative-source line must contain a clickable hyperlink")
    return {"errors": errors, "metrics": metrics}


def audit_rules_xlsx(path: Path) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return {"status": "invalid", "errors": [f"semantic-rules workbook not found: {path}"], "warnings": []}
    if path.suffix.lower() != ".xlsx":
        return {"status": "invalid", "errors": ["semantic-rules workbook must be XLSX"], "warnings": []}
    package_errors = zip_integrity(path,
        {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"})
    if package_errors:
        return {"status": "invalid", "errors": package_errors, "warnings": []}
    if not re.fullmatch(r"非量化文本量化评价规则_.+_\d{8}\.xlsx", path.name):
        errors.append("semantic-rules workbook filename does not follow the fixed naming contract")

    matrices: dict[str, list[list[str]]] = {}
    styles: dict[str, dict[str, object]] = {}
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = relationship_targets(archive, "xl/_rels/workbook.xml.rels")
        shared_strings = load_shared_strings(archive)
        names: list[str] = []
        parts: dict[str, str] = {}
        for sheet in workbook.findall("x:sheets/x:sheet", XLSX_NS):
            name = sheet.attrib.get("name", "")
            relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS['r']}}}id", "")
            target = rels.get(relationship_id, "")
            if target:
                names.append(name)
                parts[name] = resolve_part("xl/workbook.xml", target)
        if names != RULE_WORKBOOK_SHEETS:
            errors.append(f"semantic-rules workbook sheets must be exactly {RULE_WORKBOOK_SHEETS}; found {names}")
        errors.extend(audit_external_relationships(archive))
        for name in names:
            part = parts.get(name, "")
            if not part or part not in archive.namelist():
                errors.append(f"semantic-rules workbook is missing worksheet XML for {name}")
                continue
            xml = archive.read(part)
            errors.extend(audit_formula_cells(xml, name))
            matrix, _, row1_attributes = sheet_matrix(xml, shared_strings)
            matrix = normalize_matrix_for_validation(matrix)
            matrices[name] = matrix
            if name not in RULE_WORKBOOK_HEADERS:
                errors.append(f"semantic-rules unexpected worksheet: {name}")
                continue
            if not matrix:
                errors.append(f"semantic-rules worksheet {name} is empty")
                continue
            if matrix[0] != RULE_WORKBOOK_HEADERS[name]:
                errors.append(f"semantic-rules worksheet {name} headers differ from the fixed contract")
            style = header_style(archive, xml)
            styles[name] = style
            if style.get("font_name") != "Carlito" or style.get("font_size") not in {"10", "10.0"}:
                errors.append(f"semantic-rules worksheet {name} header font must be Carlito 10")
            if not style.get("bold") or style.get("font_color") not in {"FFFFFFFF", "FFFFFF"}:
                errors.append(f"semantic-rules worksheet {name} header must use bold white font")
            if style.get("fill_color") not in {"FF17324D", "17324D"}:
                errors.append(f"semantic-rules worksheet {name} header fill must be #17324D")
            if style.get("horizontal") != "center" or style.get("vertical") != "center":
                errors.append(f"semantic-rules worksheet {name} header must be centered")
            if style.get("wrap_text") not in {"1", "true", True}:
                errors.append(f"semantic-rules worksheet {name} header must wrap text")
            if row1_attributes.get("ht") not in {"34", "34.0"}:
                warnings.append(f"semantic-rules worksheet {name} header row height is not 34")

    data_rows = {
        name: [row for row in matrix[1:] if any(cell.strip() for cell in row)]
        for name, matrix in matrices.items()
    }
    protocol_version = ""
    codebook_version = ""
    codebook_sha256 = ""
    summary_rows = data_rows.get("评价协议摘要", [])
    if len(summary_rows) != 1:
        errors.append("semantic-rules workbook must contain exactly one evaluation-protocol summary row")
    else:
        row = summary_rows[0]
        place = row[0] if len(row) > 0 else ""
        task_run_id = row[1] if len(row) > 1 else ""
        retrieval_date = row[2] if len(row) > 2 else ""
        protocol_version = row[3] if len(row) > 3 else ""
        codebook_version = row[4] if len(row) > 4 else ""
        released_at = row[5] if len(row) > 5 else ""
        mode = row[6] if len(row) > 6 else ""
        if not place or not task_run_id:
            errors.append("semantic-rules evaluation-protocol summary requires place and task_run_id")
        if not protocol_version or not codebook_version:
            errors.append("semantic-rules evaluation-protocol summary requires protocol and codebook versions")
        if mode not in {"online_updated", "online_verified_no_change"}:
            errors.append("semantic-rules evaluation-protocol summary has an invalid release-calibration mode")
        try:
            if int(finite_number(row[7])) < 3 or int(finite_number(row[8])) < 3 or int(finite_number(row[9])) < 2 or int(finite_number(row[10])) < 1:
                raise ValueError
        except (ValueError, IndexError):
            errors.append("semantic-rules release calibration does not meet query/source/domain/decision minima")
        if len(row) <= 11 or not re.fullmatch(r"[0-9a-f]{64}", row[11]):
            errors.append("semantic-rules evaluation-protocol summary requires the locked codebook SHA-256")
        else:
            codebook_sha256 = row[11]
        if len(row) <= 12 or row[12] != "否":
            errors.append("semantic-rules evaluation-protocol summary must state that task-time rule changes were not performed")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", released_at):
            errors.append("semantic-rules protocol release date must use YYYY-MM-DD")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", retrieval_date):
            safe_place = re.sub(r'[<>:"/\\|?*]+', "_", place.strip())
            safe_place = re.sub(r"\s+", "_", safe_place).strip("._ ")
            expected_name = f"非量化文本量化评价规则_{safe_place}_{retrieval_date.replace('-', '')}.xlsx"
            if path.name != expected_name:
                errors.append(f"semantic-rules workbook filename must be exactly {expected_name}")
        else:
            errors.append("semantic-rules task retrieval date must use YYYY-MM-DD")

    online_rows = data_rows.get("发布校准来源", [])
    if len(online_rows) < 3:
        errors.append("semantic-rules workbook requires at least three release-calibration method sources")
    urls = {row[4] for row in online_rows if len(row) > 4 and row[4].startswith(("https://", "http://"))}
    domains = {normalize_host(urlparse(url).hostname) for url in urls if urlparse(url).hostname}
    if len(urls) < 3 or len(domains) < 2:
        errors.append("semantic-rules release-calibration sources require at least three unique URLs across two domains")
    if any(len(row) <= 8 or not row[1] or row[6] not in {"full", "partial"} or not row[8] for row in online_rows):
        errors.append("semantic-rules release-calibration sources require title, readable status, and a calibration finding")

    decision_values = data_rows.get("发布校准决策", [])
    if not decision_values:
        errors.append("semantic-rules workbook requires at least one release-calibration decision")
    if any(len(row) <= 4 or row[1] not in {"retain", "modify", "add", "deprecate"} or not row[2] or not row[3] or not row[4] for row in decision_values):
        errors.append("semantic-rules update decisions are incomplete or contain an invalid action")

    polarity_values = data_rows.get("极性词表", [])
    polarity_languages = {row[3] for row in polarity_values if len(row) > 3}
    polarity_types = {row[4] for row in polarity_values if len(row) > 4}
    if len(polarity_values) < 80 or not {"zh", "en"}.issubset(polarity_languages):
        errors.append("semantic-rules polarity lexicon lacks sufficient bilingual rows")
    if "近义释义" not in polarity_types:
        errors.append("semantic-rules polarity lexicon lacks machine-readable near-synonym rows")
    if any(len(row) <= 8 or not row[0] or row[1] not in {"positive", "negative"} or not row[5] or not row[6] or not row[7] or not row[8] for row in polarity_values):
        errors.append("semantic-rules polarity lexicon has incomplete concept, gloss, or machine-path fields")

    dimension_values = data_rows.get("七维词表", [])
    dimension_ids = {row[0] for row in dimension_values if row}
    dimension_languages = {row[4] for row in dimension_values if len(row) > 4}
    dimension_types = {row[5] for row in dimension_values if len(row) > 5}
    if len(dimension_ids) != len(DIMENSIONS) or not {"zh", "en"}.issubset(dimension_languages):
        errors.append("semantic-rules dimension lexicon must cover the seven canonical dimensions in Chinese and English")
    dimension_names = {row[1] for row in dimension_values if len(row) > 1}
    if dimension_names != set(DIMENSIONS):
        errors.append("semantic-rules dimension lexicon contains missing or non-canonical dimension names")
    if "近义释义" not in dimension_types:
        errors.append("semantic-rules dimension lexicon lacks machine-readable near-synonym rows")
    if any(len(row) <= 9 or not all((row[0], row[1], row[2], row[6], row[7], row[8], row[9])) for row in dimension_values):
        errors.append("semantic-rules dimension lexicon has incomplete names, glosses, or machine paths")
    for row in dimension_values:
        if len(row) <= 3 or row[1] not in DIMENSION_WEIGHTS:
            continue
        actual_weight = numeric_cell(row[3])
        expected_weight = DIMENSION_WEIGHTS[row[1]]
        if actual_weight is None or abs(actual_weight - expected_weight) > 1e-12:
            errors.append(
                f"semantic-rules dimension lexicon weight for {row[1]} must equal {expected_weight:.0%}"
            )
            break

    context_values = data_rows.get("上下文规则", [])
    context_types = {row[0] for row in context_values if row}
    if not {"否定", "转折", "弱化", "反讽风险", "程度", "参数"}.issubset(context_types):
        errors.append("semantic-rules context sheet does not cover all mandatory rule families")
    coding_values = data_rows.get("编码解析规则", [])
    if len(coding_values) != 8:
        errors.append("semantic-rules coding-parse sheet must contain exactly eight fixed rules")
    if not any(
        "确认前正式评分资格为否且不进入平台计分" in " ".join(row)
        for row in coding_values
    ):
        errors.append("semantic-rules coding-parse sheet does not prohibit pre-confirmation scoring")

    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "sheet_names": list(matrices),
            "method_source_rows": len(online_rows),
            "method_source_domains": len(domains),
            "method_decision_rows": len(decision_values),
            "polarity_lexicon_rows": len(polarity_values),
            "dimension_lexicon_rows": len(dimension_values),
            "dimension_count": len(dimension_ids),
            "context_rule_rows": len(context_values),
            "coding_parse_rule_rows": len(coding_values),
            "evaluation_protocol_version": protocol_version,
            "semantic_codebook_version": codebook_version,
            "semantic_codebook_sha256": codebook_sha256,
        },
    }


def audit_docx(
    path: Path,
    sources: list[dict[str, str]],
    minimum_pages: int,
    minimum_effective_samples: int = 100,
    expected_effective_samples: int | None = None,
) -> dict[str, object]:
    errors = zip_integrity(
        path,
        {"[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels", "word/styles.xml"},
    )
    warnings: list[str] = []
    if errors:
        return {"status": "invalid", "errors": errors, "warnings": warnings}
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        report_fonts = explicit_docx_fonts(archive, root)
        invalid_fonts = sorted(report_fonts - ALLOWED_REPORT_FONTS)
        if invalid_fonts:
            errors.append(
                "DOCX uses fonts outside the allowed set 黑体、宋体、Times New Roman: "
                + ", ".join(invalid_fonts)
            )
        paragraphs = root.findall(".//w:body/w:p", DOCX_NS)
        texts = [paragraph_text(paragraph) for paragraph in paragraphs]
        styles = [paragraph_style(paragraph) for paragraph in paragraphs]
        body_blocks = direct_body_blocks(root)
        nonempty = [
            (index, text, style)
            for index, (text, style) in enumerate(zip(texts, styles))
            if text.strip()
        ]
        if len(nonempty) < 3 or [item[2] for item in nonempty[:3]] != ["Title", "Subtitle", "Date"]:
            errors.append("DOCX cover must begin with Title, Subtitle, and Date paragraphs")
        elif not nonempty[2][1].startswith("检索日期："):
            errors.append("DOCX cover Date paragraph must begin with 检索日期：")
        page_break_count = sum(
            1
            for paragraph in paragraphs
            for node in paragraph.findall(".//w:br", DOCX_NS)
            if node.attrib.get(f"{{{DOCX_NS['w']}}}type") == "page"
        )
        if page_break_count != 2:
            errors.append(f"DOCX must contain exactly two cover-and-contents page breaks; found {page_break_count}")
        heading1 = [text for text, style in zip(texts, styles) if style in {"Heading1", "1"}]
        expected_heading1 = ["研究口径说明"] + [
            f"{index}. {title}" for index, title in enumerate(SECTIONS, start=1)
        ]
        if heading1 != expected_heading1:
            errors.append(f"DOCX Heading 1 sequence is incorrect; found {heading1}")
        try:
            toc_index = texts.index("目录")
            scope_heading_index = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == "研究口径说明" and style in {"Heading1", "1"}
            )
            toc_items = [text for text in texts[toc_index + 1 : scope_heading_index] if text.strip()]
            expected_toc = ["研究口径说明"] + [
                f"{index}. {title}" for index, title in enumerate(SECTIONS, start=1)
            ]
            if toc_items != expected_toc:
                errors.append(f"DOCX contents list is incomplete or out of order; found {toc_items}")
            first_section_index = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == "1. 地点基本信息" and style in {"Heading1", "1"}
            )
            scope_paragraphs = [
                text for text in texts[scope_heading_index + 1 : first_section_index] if text.strip()
            ]
            scope_cjk = cjk_count("".join(scope_paragraphs))
            if len(scope_paragraphs) < 4 or scope_cjk < MIN_SCOPE_CJK:
                errors.append(
                    f"DOCX research scope must contain at least four paragraphs and {MIN_SCOPE_CJK} Chinese characters"
                )
        except (ValueError, StopIteration):
            toc_items = []
            scope_paragraphs = []
            scope_cjk = 0
            errors.append("DOCX cover, contents, or research-scope structure is incomplete")
        heading2 = [text for text, style in zip(texts, styles) if style in {"Heading2", "2"}]
        if heading2 != EXPECTED_HEADING2:
            errors.append(f"DOCX Heading 2 sequence is incorrect; found {heading2}")
        try:
            platform_start = texts.index("9.1 平台特征")
            platform_end = texts.index("9.2 平台评分表")
            platform_heading3_count = sum(
                style in {"Heading3", "3"}
                for style in styles[platform_start + 1 : platform_end]
            )
            if platform_heading3_count < 3:
                errors.append("DOCX section 9 must contain at least three platform or source-group Heading 3 analyses")
        except ValueError:
            platform_heading3_count = 0
        dimension_audit = audit_dimension_narratives(paragraphs, texts, styles)
        errors.extend(dimension_audit["errors"])
        full_text = "\n".join(node.text or "" for node in root.findall(".//w:t", DOCX_NS))
        semantic_disclosure_terms = [
            "语义量化",
            "语义置信度",
            "证据可靠性",
            "否定",
            "转折",
            "复核",
            "评价协议版本",
            "语义代码簿版本",
            "发布校准",
            "任务期不更新",
            "双语",
            "词表",
            "编码解析",
        ]
        missing_semantic_terms = [term for term in semantic_disclosure_terms if term not in full_text]
        if missing_semantic_terms:
            errors.append(
                "DOCX must disclose the semantic quantification method, context rules, confidence, reliability, and review process; missing: "
                + ", ".join(missing_semantic_terms)
            )
        protocol_match = re.search(r"评价协议标识：([A-Za-z0-9._-]+)", full_text)
        codebook_match = re.search(r"语义代码簿标识：([A-Za-z0-9._-]+)", full_text)
        codebook_sha_match = re.search(
            r"SHA-256（安全散列算法校验值）：([0-9a-f]{64})",
            full_text,
        )
        if not protocol_match or not codebook_match or not codebook_sha_match:
            errors.append(
                "DOCX research scope must disclose the locked evaluation-protocol identifier, "
                "semantic-codebook identifier, and SHA-256 with a Chinese gloss"
            )
        report_protocol_version = protocol_match.group(1) if protocol_match else ""
        report_codebook_version = codebook_match.group(1) if codebook_match else ""
        report_codebook_sha256 = codebook_sha_match.group(1) if codebook_sha_match else ""

        def numbered_section_text(number: int) -> str:
            start_heading = f"{number}. {SECTIONS[number - 1]}"
            end_heading = f"{number + 1}. {SECTIONS[number]}" if number < len(SECTIONS) else ""
            try:
                start = next(
                    index
                    for index, (text, style) in enumerate(zip(texts, styles))
                    if text == start_heading and style in {"Heading1", "1"}
                ) + 1
                end = next(
                    index
                    for index, (text, style) in enumerate(zip(texts, styles))
                    if text == end_heading and style in {"Heading1", "1"}
                ) if end_heading else len(texts)
            except StopIteration:
                return ""
            return "\n".join(texts[start:end])

        semantic_section_requirements = {
            "研究口径说明": (
                "\n".join(scope_paragraphs),
                ["语义量化", "原生数值", "语义推断", "评价协议版本", "语义代码簿版本", "发布校准", "任务期不更新", "双语", "词表", "编码解析", "复核"],
            ),
            "第4节": (
                numbered_section_text(4),
                ["语义量化", "语义置信度", "证据可靠性", "否定", "转折", "编码解析", "复核"],
            ),
            "第5节": (
                numbered_section_text(5),
                ["语义量化", "摘要原文", "语义置信度", "证据可靠性", "否定", "转折"],
            ),
            "第13节": (
                numbered_section_text(13),
                ["语义量化", "原生数值", "评价协议版本", "语义代码簿版本", "发布校准", "任务期不更新", "词表", "编码解析", "低置信", "复核"],
            ),
        }
        semantic_disclosure_sections = 0
        for section_label, (section_text, required_terms) in semantic_section_requirements.items():
            missing = [term for term in required_terms if term not in section_text]
            if missing:
                errors.append(f"DOCX {section_label} semantic disclosure is incomplete; missing: {', '.join(missing)}")
            else:
                semantic_disclosure_sections += 1
        for placeholder in ("待填写", "【待", "TODO", "TBD"):
            if placeholder in full_text:
                errors.append(f"DOCX contains unresolved placeholder: {placeholder}")
        try:
            conclusion_start = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == "13. 结论" and style in {"Heading1", "1"}
            ) + 1
            conclusion_end = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == "14. 来源清单" and style in {"Heading1", "1"}
            )
            conclusion = "\n".join(texts[conclusion_start:conclusion_end])
            conclusion_paragraph_count = sum(
                bool(text.strip()) and style not in {"Heading1", "1", "Heading2", "2", "Heading3", "3"}
                for text, style in zip(
                    texts[conclusion_start:conclusion_end],
                    styles[conclusion_start:conclusion_end],
                )
            )
        except (ValueError, StopIteration):
            conclusion = ""
            conclusion_paragraph_count = 0
            conclusion_end = len(texts)
        conclusion_cjk = cjk_count(conclusion)
        total_cjk = cjk_count(full_text)
        analytical_end = next(
            (
                index
                for index, block in enumerate(body_blocks)
                if block["kind"] == "paragraph"
                and block["text"] == "14. 来源清单"
                and block["style"] in {"Heading1", "1"}
            ),
            len(body_blocks),
        )
        analytical_body = "\n".join(str(block["text"]) for block in body_blocks[:analytical_end])
        analytical_body_cjk = cjk_count(analytical_body)
        if conclusion_cjk < MIN_CONCLUSION_CJK:
            errors.append(f"DOCX conclusion contains only {conclusion_cjk} Chinese characters; at least {MIN_CONCLUSION_CJK} required")
        if conclusion_paragraph_count < MIN_CONCLUSION_PARAGRAPHS:
            errors.append(
                f"DOCX conclusion contains only {conclusion_paragraph_count} substantive paragraphs; at least {MIN_CONCLUSION_PARAGRAPHS} required"
            )
        if not re.search(r"网络[^\n]{0,80}不(?:代表|等同于)全部", conclusion):
            errors.append("DOCX conclusion must state that network samples do not represent all residents or visitors")
        if analytical_body_cjk < MIN_ANALYTICAL_BODY_CJK:
            errors.append(
                f"DOCX analytical body contains only {analytical_body_cjk} Chinese characters; at least {MIN_ANALYTICAL_BODY_CJK} required"
            )
        narrative_paragraphs = [
            text
            for text, style in zip(texts[:conclusion_end if conclusion_end else len(texts)], styles)
            if text.strip()
            and style not in {"Title", "Subtitle", "Date", "Heading1", "1", "Heading2", "2", "Heading3", "3", "TOCHeading"}
            and cjk_count(text) >= 20
            and sum(text.count(f"{label}：") for label in DIMENSION_LABELS) < 4
        ]
        repeated_sentences = Counter(
            re.sub(r"\s+", "", sentence)
            for paragraph in narrative_paragraphs
            for sentence in re.split(r"[。！？!?\n]+", paragraph)
            if cjk_count(sentence) >= 20
        )
        excessive_repetition = [
            sentence for sentence, count in repeated_sentences.items() if sentence and count > 1
        ]
        if excessive_repetition:
            errors.append(
                "DOCX analytical body repeats a substantive sentence; remove templated padding and write evidence-specific analysis"
            )
        near_duplicate_pairs: list[tuple[int, int, float, float]] = []
        eligible_paragraphs = [text for text in narrative_paragraphs if cjk_count(text) >= 60]
        for left_index, left in enumerate(eligible_paragraphs):
            for right_index, right in enumerate(eligible_paragraphs[left_index + 1 :], start=left_index + 1):
                if normalized_narrative(left) == normalized_narrative(right):
                    near_duplicate_pairs.append((left_index, right_index, 1.0, 1.0))
                    continue
                ratio, jaccard = narrative_similarity(left, right)
                if ratio >= 0.92 or (ratio >= 0.86 and jaccard >= 0.78):
                    near_duplicate_pairs.append((left_index, right_index, ratio, jaccard))
        if near_duplicate_pairs:
            errors.append(
                "DOCX analytical body contains exact or near-duplicate paragraphs across sections; "
                "the conclusion and dimension analyses must synthesize rather than restate prior prose"
            )
        narrative_english_violations = english_gloss_violations("\n".join(narrative_paragraphs))
        if narrative_english_violations:
            errors.append(
                "DOCX report body contains English without an adjacent Chinese gloss: "
                + ", ".join(narrative_english_violations[:10])
            )
        preferred_minimum, preferred_maximum = PREFERRED_ANALYTICAL_BODY_CJK
        if analytical_body_cjk < preferred_minimum or analytical_body_cjk > preferred_maximum:
            warnings.append(
                f"DOCX analytical body contains {analytical_body_cjk} Chinese characters; preferred range is {preferred_minimum} to {preferred_maximum}"
            )
        section_cjk: dict[str, int] = {}
        numbered_headings = [f"{index}. {title}" for index, title in enumerate(SECTIONS, start=1)]
        for index, heading in enumerate(numbered_headings[:13]):
            end_heading = numbered_headings[index + 1]
            section_text = section_block_text(body_blocks, heading, end_heading)
            measured = cjk_count(section_text)
            section_cjk[heading] = measured
            minimum = SECTION_MIN_CJK[heading]
            if measured < minimum:
                errors.append(
                    f"DOCX {heading} contains only {measured} Chinese characters across narrative and analytical tables; at least {minimum} required"
                )
        try:
            judgment_start = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == "12. 历史文化活态传承感知评价" and style in {"Heading1", "1"}
            ) + 1
            judgment_end = next(
                index
                for index, (text, style) in enumerate(zip(texts, styles))
                if text == "13. 结论" and style in {"Heading1", "1"}
            )
            judgment_paragraph_count = sum(
                bool(text.strip()) and style not in {"Heading1", "1", "Heading2", "2", "Heading3", "3"}
                for text, style in zip(
                    texts[judgment_start:judgment_end],
                    styles[judgment_start:judgment_end],
                )
            )
            result_segment = "\n".join(texts[judgment_start:judgment_end])
            required_result_labels = [
                "综合分", "核心优势", "核心风险", "底线性问题", "证据充分度", "评价置信度"
            ]
            if judgment_paragraph_count != len(required_result_labels):
                errors.append("DOCX section 12 must contain exactly six separate +1 result fields")
            for label in required_result_labels:
                if result_segment.count(f"{label}：") != 1:
                    errors.append(f"DOCX section 12 must contain exactly one {label} field")
            if "底线性问题：" in result_segment and not any(
                term in result_segment for term in ("真实性", "居民", "文化实践", "保护", "无明显底线")
            ):
                errors.append("DOCX section 12 bottom-line field lacks a living-heritage bottom-line judgment")
        except (ValueError, StopIteration):
            judgment_paragraph_count = 0
        if expected_effective_samples is not None:
            sample_patterns = [
                rf"有效(?:纳入)?评分样本(?:数|量)?[^\d]{{0,20}}{expected_effective_samples}",
                rf"{expected_effective_samples}[^\d]{{0,12}}个有效(?:纳入)?评分样本",
            ]
            if not any(re.search(pattern, full_text) for pattern in sample_patterns):
                errors.append(
                    "DOCX must state the actual effective scoring sample count"
                )
            if expected_effective_samples < minimum_effective_samples:
                if "深度迭代" not in full_text or not re.search(r"样本(?:缺口|不足)", full_text):
                    errors.append(
                        "DOCX sample-shortfall case must explain the deep-search iterations and remaining sample gap"
                    )
        machine_lines = [text.strip() for text in texts if text.count("｜") >= 18]
        if len(machine_lines) != 1 or len(machine_lines[0].split("｜")) != 19:
            errors.append("DOCX must contain exactly one 19-field machine-readable line")
        tables = root.findall(".//w:body/w:tbl", DOCX_NS)
        matrices = [table_matrix(table) for table in tables]
        table_headers = [matrix[0] if matrix else [] for matrix in matrices]
        table_count = len(tables)
        if table_count != len(DOCX_TABLE_HEADERS):
            errors.append(f"DOCX must contain exactly {len(DOCX_TABLE_HEADERS)} tables; found {table_count}")
        if table_headers != DOCX_TABLE_HEADERS:
            errors.append(f"DOCX table header sequence is incorrect; found {table_headers}")
        if len(matrices) > 5:
            dimension_weight_rows = {
                row[0]: numeric_cell(row[1])
                for row in matrices[5][1:]
                if len(row) > 1 and row[0]
            }
            if set(dimension_weight_rows) != set(DIMENSION_WEIGHTS):
                errors.append("DOCX seven-dimension summary table must contain exactly the seven canonical dimensions")
            else:
                for dimension, expected_weight in DIMENSION_WEIGHTS.items():
                    actual_weight = dimension_weight_rows[dimension]
                    if actual_weight is None or abs(actual_weight - expected_weight) > 1e-12:
                        errors.append(
                            f"DOCX seven-dimension summary weight for {dimension} must equal {expected_weight:.0%}"
                        )
        expected_source_ids = {row.get("source_id", "") for row in sources if row.get("source_id")}
        if len(expected_source_ids) < minimum_pages:
            errors.append(f"source ledger contains only {len(expected_source_ids)} source pages; minimum is {minimum_pages}")
        if matrices and len(matrices[-1]) != len(expected_source_ids) + 1:
            errors.append(
                f"DOCX source table contains {max(0, len(matrices[-1]) - 1)} rows; expected {len(expected_source_ids)}"
            )
        layout = document_layout(root)
        layout_targets = {
            "page_width_twips": 11906,
            "page_height_twips": 16838,
            "top_margin_twips": 1361,
            "bottom_margin_twips": 1247,
            "left_margin_twips": 1361,
            "right_margin_twips": 1247,
            "header_distance_twips": 624,
            "footer_distance_twips": 624,
        }
        for field, target in layout_targets.items():
            value = layout.get(field)
            if value is None or abs(value - target) > 35:
                errors.append(f"DOCX page layout {field} must be approximately {target} twips; found {value}")
        rels = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        targets = {
            item.attrib.get("Target", "")
            for item in rels.findall("r:Relationship", REL_NS)
            if item.attrib.get("TargetMode") == "External"
            and item.attrib.get("Type", "").endswith("/hyperlink")
        }
        expected_urls = {row.get("url", "") for row in sources if re.match(r"^https?://", row.get("url", ""))}
        missing_urls = sorted(expected_urls - targets)
        if missing_urls:
            errors.append(f"DOCX source table omits hyperlinks: {missing_urls[:5]}")
        for url in targets:
            leaked = sorted({key for key in query_parameter_names(urlparse(url).query) if key in SENSITIVE_QUERY_KEYS})
            if leaked:
                errors.append(f"DOCX hyperlink exposes sensitive query keys: {', '.join(leaked)}")
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "heading1_count": len(heading1),
            "heading2_count": len(heading2),
            "platform_heading3_count": platform_heading3_count,
            "contents_item_count": len(toc_items),
            "research_scope_chinese_characters": scope_cjk,
            "semantic_disclosure_sections": semantic_disclosure_sections,
            "evaluation_protocol_version": report_protocol_version,
            "semantic_codebook_version": report_codebook_version,
            "semantic_codebook_sha256": report_codebook_sha256,
            "page_break_count": page_break_count,
            **dimension_audit["metrics"],
            "conclusion_chinese_characters": conclusion_cjk,
            "conclusion_paragraph_count": conclusion_paragraph_count,
            "analytical_body_chinese_characters": analytical_body_cjk,
            "total_chinese_characters": total_cjk,
            "section_chinese_characters": section_cjk,
            "judgment_paragraph_count": judgment_paragraph_count,
            "repeated_substantive_sentence_count": len(excessive_repetition),
            "repeated_substantive_sentences": excessive_repetition[:10],
            "near_duplicate_paragraph_pair_count": len(near_duplicate_pairs),
            "unglossed_english_term_count": len(narrative_english_violations),
            "explicit_font_names": sorted(report_fonts),
            "allowed_font_contract_passed": not invalid_fonts,
            "declared_effective_samples": expected_effective_samples,
            "table_count": table_count,
            "table_headers": table_headers,
            "source_id_count": len(expected_source_ids),
            "hyperlink_target_count": len(targets),
            "layout": layout,
        },
    }


def audit_execution_integrity(
    *,
    sources_path: Path,
    raw_evidence_path: Path,
    semantic_evidence_path: Path,
    formal_evidence_path: Path,
    search_log_path: Path,
    scores_path: Path,
    dimension_audit_path: Path,
    detailed_log_path: Path,
    skill_root: Path,
    release_manifest_path: Path,
    state_path: Path | None = None,
    writer_ledger_path: Path | None = None,
    truth_freeze_path: Path | None = None,
    preflight_path: Path | None = None,
    source_capture_manifest_path: Path | None = None,
    canonical_sources_path: Path | None = None,
    canonical_evidence_path: Path | None = None,
    correction_ledger_path: Path | None = None,
    locator_audit_path: Path | None = None,
    collision_audit_path: Path | None = None,
    admission_audit_path: Path | None = None,
    report_truth_path: Path | None = None,
    report_narrative_path: Path | None = None,
    execution_schema_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Independently enforce release, ledger, audit, and real-time-log truth."""
    errors: list[str] = []
    metrics: dict[str, object] = {}
    def check(code: str, action):
        try:
            return action()
        except Exception as exc:
            errors.append(f"{code}: {exc}")
            return None

    sources = check("EXEC_SOURCE_LEDGER", lambda: read_sources(sources_path))
    raw_evidence = check("EXEC_RAW_EVIDENCE", lambda: read_sources(raw_evidence_path))
    semantic_evidence = check(
        "EXEC_SEMANTIC_EVIDENCE", lambda: read_sources(semantic_evidence_path)
    )
    formal_evidence = check(
        "EXEC_FORMAL_EVIDENCE", lambda: read_sources(formal_evidence_path)
    )
    search_rows = check("EXEC_SEARCH_LOG", lambda: read_sources(search_log_path))
    ledgers = [
        rows
        for rows in (sources, raw_evidence, semantic_evidence, formal_evidence, search_rows)
        if isinstance(rows, list)
    ]
    run_ids = {
        str(row.get("task_run_id", "")).strip()
        for rows in ledgers
        for row in rows
        if str(row.get("task_run_id", "")).strip()
    }
    task_run_id = next(iter(run_ids)) if len(run_ids) == 1 else ""
    if len(run_ids) != 1:
        errors.append("EXEC_TASK_RUN: 正式台账必须且只能包含一个 task_run_id")
    if task_run_id:
        release = check(
            "EXEC_RELEASE",
            lambda: verify_release_manifest(
                skill_root, release_manifest_path, task_run_id=task_run_id
            ),
        )
        if release is not None:
            metrics["release_integrity"] = "valid"
    state: dict[str, object] | None = None
    if state_path is not None:
        loaded_state = check("EXEC_STATE_CONTEXT", lambda: load_runtime_state(state_path))
        if isinstance(loaded_state, dict):
            state = loaded_state
            bound_context = check(
                "EXEC_EXECUTION_SCHEMA_CONTEXT",
                lambda: execution_schema_context_from_state(
                    loaded_state,
                    config=load_retrieval_config(),
                    state_path=state_path,
                ),
            )
            if isinstance(bound_context, dict):
                if execution_schema_context is not None and dict(execution_schema_context) != dict(bound_context):
                    errors.append("EXEC_EXECUTION_SCHEMA_CONTEXT: explicit context differs from state")
                execution_schema_context = bound_context
    if all(isinstance(rows, list) for rows in (sources, raw_evidence, semantic_evidence, formal_evidence)):
        chain_metrics = check(
            "EXEC_EVIDENCE_CHAIN",
            lambda: validate_evidence_truth_chain(
                sources, raw_evidence, semantic_evidence, formal_evidence  # type: ignore[arg-type]
            ),
        )
        if isinstance(chain_metrics, dict):
            metrics.update(chain_metrics)

    dimension_payload = check(
        "EXEC_DIMENSION_AUDIT_JSON",
        lambda: json.loads(dimension_audit_path.read_text(encoding="utf-8-sig")),
    )
    if dimension_payload is not None and not isinstance(dimension_payload, dict):
        errors.append("EXEC_DIMENSION_AUDIT_JSON: 正式维度审计必须为 JSON 对象")
        dimension_payload = None
    validated_dimension_audit = None
    if (
        task_run_id
        and isinstance(dimension_payload, dict)
        and isinstance(formal_evidence, list)
        and isinstance(sources, list)
        and isinstance(search_rows, list)
    ):
        validated_dimension_audit = check(
            "EXEC_DIMENSION_AUDIT_BINDING",
            lambda: validate_machine_dimension_audit(
                dimension_payload,
                task_run_id=task_run_id,
                evidence=formal_evidence,
                sources=sources,
                search_rows=search_rows,
                execution_schema_context=execution_schema_context,
            ),
        )
        if validated_dimension_audit is not None:
            metrics["dimension_audit_binding"] = "valid"
        summary = dimension_payload.get("summary", {})
        retrieval = summary.get("retrieval_termination", {}) if isinstance(summary, dict) else {}
        deep_rounds = summary.get("deep_iteration_round_numbers", []) if isinstance(summary, dict) else []
        query_metrics = executed_query_metrics(
            search_rows,
            config=load_retrieval_config(),
            schema_context=execution_schema_context,
        )
        metrics["executed_query_contract"] = query_metrics
        from execution_facts import validate_source_links
        linked = validate_source_links(sources, validate_execution_records(
            search_rows, schema_context=execution_schema_context))
        if linked['status'] != 'valid':
            errors.extend('EXEC_SOURCE_QUERY_BINDING: ' + json.dumps(item, ensure_ascii=False, sort_keys=True) for item in linked['errors'])
        if query_metrics.get("invalid_query_records"):
            errors.append(
                "EXEC_QUERY_CONTRACT: "
                + json.dumps(
                    query_metrics["invalid_query_records"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        query_budget = validate_query_budget_compliance(
            query_metrics,
            config=load_retrieval_config(),
        )
        metrics["query_budget_compliance"] = query_budget
        if query_budget["status"] != "valid":
            errors.append(
                "EXEC_QUERY_BUDGET: "
                + json.dumps(
                    query_budget,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        if isinstance(deep_rounds, list) and deep_rounds:
            required = retrieval.get("remaining_dimensions", []) if isinstance(retrieval, dict) else []
            controls = load_retrieval_config()
            maximum_rounds = budget_values(controls)["maximum_iteration_rounds"]
            round_result = validate_round_completion(
                search_rows,
                config=controls,
                required_dimensions=required if isinstance(required, list) else [],
                expected_round_count=min(
                    max(
                        int(value) for value in deep_rounds
                        if isinstance(value, int) and not isinstance(value, bool)
                    ),
                    maximum_rounds,
                ),
                schema_context=execution_schema_context,
            )
            metrics["deep_round_completion"] = round_result
            if round_result["status"] != "valid":
                errors.append(
                    "EXEC_DEEP_ROUND_SEQUENCE: "
                    + json.dumps(round_result, ensure_ascii=False, separators=(",", ":"))
                )

    scores = check(
        "EXEC_SCORING_JSON",
        lambda: json.loads(scores_path.read_text(encoding="utf-8-sig")),
    )
    if scores is not None and not isinstance(scores, dict):
        errors.append("EXEC_SCORING_JSON: 综合评分必须为 JSON 对象")
        scores = None
    if isinstance(scores, dict) and task_run_id and scores.get("task_run_id") != task_run_id:
        errors.append("EXEC_SCORING_TASK: 综合评分 task_run_id 与正式台账不一致")
    if isinstance(scores, dict) and isinstance(validated_dimension_audit, dict):
        if scores.get("dimension_evidence_audit") != validated_dimension_audit:
            errors.append("EXEC_SCORING_AUDIT: 综合评分未使用当前台账独立复算的维度审计")
    if isinstance(validated_dimension_audit, dict) and isinstance(state, dict):
        state_target = str(state.get("target_confidence", ""))
        audit_target = str(validated_dimension_audit.get("target_confidence", ""))
        if state_target not in {"中", "中高", "高"} or audit_target != state_target:
            errors.append("EXEC_TARGET_CONFIDENCE: 正式维度审计目标与运行状态绑定不一致")
        if isinstance(scores, dict):
            score_target = str(scores.get("target_confidence", audit_target))
            if score_target != state_target:
                errors.append("EXEC_TARGET_CONFIDENCE: 综合评分目标与运行状态绑定不一致")
    if isinstance(scores, dict):
        platform_input = scores.get("formal_scoring_input")
        if isinstance(platform_input, dict):
            check("EXEC_SCORING_GATE", lambda: verify_scoring_input_gate(platform_input))
        else:
            errors.append("EXEC_SCORING_GATE: 综合评分缺少正式评分输入")

    if isinstance(sources, list):
        unique_urls = {
            normalized_url_sha256(row.get("url", ""), row.get("final_url", ""))
            for row in sources
            if normalized_url_sha256(row.get("url", ""), row.get("final_url", ""))
        }
        metrics["source_ledger_rows"] = len(sources)
        metrics["unique_source_urls"] = len(unique_urls)
    if isinstance(raw_evidence, list):
        metrics["raw_evidence_units"] = len(
            {row.get("evidence_id", "") for row in raw_evidence}
        )
    if isinstance(semantic_evidence, list):
        metrics["semantic_evidence_units"] = len(
            {row.get("evidence_id", "") for row in semantic_evidence}
        )
    if isinstance(validated_dimension_audit, dict):
        metrics["eligible_evidence_units"] = sum(
            int(item.get("eligible_evidence_units", 0))
            for item in validated_dimension_audit["dimensions"].values()
        )
        metrics["scored_evidence_units"] = sum(
            int(item.get("scored_evidence_units", 0))
            for item in validated_dimension_audit["dimensions"].values()
        )
    if task_run_id:
        log_result = check(
            "EXEC_DETAILED_LOG",
            lambda: audit_log(detailed_log_path, task_run_id=task_run_id),
        )
        if isinstance(log_result, dict):
            if log_result.get("status") != "valid":
                errors.extend(
                    f"EXEC_DETAILED_LOG: {item}" for item in log_result.get("errors", [])
                )
            metrics["detailed_log"] = log_result
    metrics["task_run_id"] = task_run_id
    if state_path is not None:
        try:
            if state is None:
                state = load_runtime_state(state_path)
            if state.get("phase") != "VALIDATE" or state.get("status") != "validating":
                raise ValueError("formal validator must run in VALIDATE/validating state")
            alignment = assert_state_log_alignment(detailed_log_path, state)
            if alignment["status"] != "valid":
                errors.extend(str(item) for item in alignment["errors"])
            metrics["state_log_alignment"] = alignment
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"EXEC_STATE_LOG: {exc}")
    if all(path is not None for path in (state_path, writer_ledger_path, truth_freeze_path, preflight_path)):
        freeze = check(
            "EXEC_TRUTH_FREEZE",
            lambda: verify_truth_freeze(
                state_path=state_path,  # type: ignore[arg-type]
                writer_ledger_path=writer_ledger_path,  # type: ignore[arg-type]
                manifest_path=truth_freeze_path,  # type: ignore[arg-type]
                require_scoring=True,
            ),
        )
        preflight = check(
            "EXEC_PREFLIGHT",
            lambda: verify_preflight(
                state_path=state_path,  # type: ignore[arg-type]
                writer_ledger_path=writer_ledger_path,  # type: ignore[arg-type]
                preflight_path=preflight_path,  # type: ignore[arg-type]
            ),
        )
        protected = {
            "source_ledger": sources_path,
            "raw_evidence": raw_evidence_path,
            "semantic_evidence": semantic_evidence_path,
            "formal_evidence": formal_evidence_path,
            "search_log": search_log_path,
            "evidence_audit": dimension_audit_path,
            "scoring_output": scores_path,
        }
        provenance_ok = True
        for role, artifact in protected.items():
            if check(
                f"EXEC_WRITER_{role.upper()}",
                lambda role=role, artifact=artifact: verify_artifact_writer(
                    state_path=state_path,  # type: ignore[arg-type]
                    writer_ledger_path=writer_ledger_path,  # type: ignore[arg-type]
                    output_role=role,
                    output_path=artifact,
                ),
            ) is None:
                provenance_ok = False
        if provenance_ok:
            metrics["protected_writer_provenance"] = "valid"
        if freeze is not None:
            metrics["truth_freeze"] = "valid"
        if preflight is not None:
            metrics["preflight"] = "valid"
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "warnings": [],
        "metrics": metrics,
    }


def validate(
    report: Path,
    workbook: Path,
    rules_workbook: Path,
    sources_path: Path,
    evidence_path: Path | None = None,
    search_log_path: Path | None = None,
    scores_path: Path | None = None,
    report_data_path: Path | None = None,
    raw_evidence_path: Path | None = None,
    semantic_evidence_path: Path | None = None,
    dimension_audit_path: Path | None = None,
    detailed_log_path: Path | None = None,
    skill_root: Path | None = None,
    release_manifest_path: Path | None = None,
    state_path: Path | None = None,
    writer_ledger_path: Path | None = None,
    truth_freeze_path: Path | None = None,
    preflight_path: Path | None = None,
    source_capture_manifest_path: Path | None = None,
    canonical_sources_path: Path | None = None,
    canonical_evidence_path: Path | None = None,
    correction_ledger_path: Path | None = None,
    locator_audit_path: Path | None = None,
    collision_audit_path: Path | None = None,
    admission_audit_path: Path | None = None,
    report_truth_path: Path | None = None,
    report_narrative_path: Path | None = None,
    minimum_pages: int = 150,
    minimum_effective_samples: int | None = None,
    execution_schema_context: dict[str, object] | None = None,
) -> dict[str, object]:
    error_objects: list[dict[str, object]] = []

    def append_error_object(
        *, code: str, subsystem: str, origin_layer: str, truth_layer: str,
        message: str, artifacts: list[str], upstream_truth_valid: bool,
        repair_scope: str, requires_new_run: bool,
    ) -> None:
        error_objects.append(stable_error(
            error_code=code,
            subsystem=subsystem,
            origin_layer=origin_layer,
            truth_layer=truth_layer,
            message=message,
            affected_artifacts=artifacts,
            upstream_truth_valid=upstream_truth_valid,
            repair_scope=repair_scope,
            requires_new_run=requires_new_run,
        ))

    def safe_result(name: str, action):
        try:
            return action()
        except Exception as exc:
            return {
                "status": "invalid",
                "errors": [f"{name} raised {type(exc).__name__}: {exc}"],
                "warnings": [],
                "metrics": {},
            }

    try:
        sources = read_sources(sources_path)
    except Exception:
        sources = []
    formal_audit_status = ""
    formal_retrieval_termination_status = ""
    audited_effective_target: int | None = None
    if dimension_audit_path is not None and dimension_audit_path.exists():
        try:
            dimension_audit_payload = json.loads(
                dimension_audit_path.read_text(encoding="utf-8-sig")
            )
            audit_summary = dimension_audit_payload.get("summary", {})
            if isinstance(audit_summary, dict):
                target_value = audit_summary.get("minimum_effective_sample_target")
                if isinstance(target_value, int) and not isinstance(target_value, bool):
                    audited_effective_target = target_value
                retrieval_state = audit_summary.get("retrieval_termination", {})
                if isinstance(retrieval_state, dict):
                    formal_retrieval_termination_status = str(
                        retrieval_state.get("termination_status", "")
                    )
            formal_audit_status = str(dimension_audit_payload.get("status", ""))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if minimum_effective_samples is None:
        default_target = int(
            research_target_summary()["theoretical_minimum_scored_evidence"]
        )
        minimum_effective_samples = audited_effective_target or default_target
    elif audited_effective_target is not None and minimum_effective_samples != audited_effective_target:
        raise ValueError(
            "requested minimum effective samples differ from the formal evidence audit target"
        )
    expected_source_ids = {row.get("source_id", "") for row in sources if row.get("source_id")}
    source_grounding_result: dict[str, object] = {
        "status": "not_checked",
        "source_grounded_evidence_validated": False,
    }
    grounding_artifact_paths = (
        source_capture_manifest_path,
        canonical_sources_path,
        canonical_evidence_path,
        correction_ledger_path,
        locator_audit_path,
        collision_audit_path,
        admission_audit_path,
    )
    grounding_paths = (
        *grounding_artifact_paths,
        raw_evidence_path,
    )
    if all(path is not None for path in grounding_paths):
        try:
            task_run_ids = {row.get("task_run_id", "") for row in sources if row.get("task_run_id")}
            task_run_id = next(iter(task_run_ids), "")
            verify_capture_manifest(source_capture_manifest_path, task_run_id=task_run_id)  # type: ignore[arg-type]
            if writer_ledger_path is None:
                raise ValueError("atomic capture verification requires the protected writer ledger")
            verify_capture_transaction_chain(
                manifest_path=source_capture_manifest_path,  # type: ignore[arg-type]
                writer_ledger_path=writer_ledger_path,
                task_run_id=task_run_id,
                require_all=True,
            )
            verify_canonical_active_views(
                task_run_id=task_run_id,
                canonical_sources_path=canonical_sources_path,  # type: ignore[arg-type]
                canonical_evidence_path=canonical_evidence_path,  # type: ignore[arg-type]
                correction_ledger_path=correction_ledger_path,  # type: ignore[arg-type]
                active_sources_path=sources_path,
                active_evidence_path=raw_evidence_path,  # type: ignore[arg-type]
                collision_audit_path=collision_audit_path,  # type: ignore[arg-type]
            )
            verify_admission_commit(
                admission_audit_path,  # type: ignore[arg-type]
                task_run_id=task_run_id,
                output_paths={
                    "canonical_source_ledger": canonical_sources_path,  # type: ignore[dict-item]
                    "canonical_evidence_ledger": canonical_evidence_path,  # type: ignore[dict-item]
                    "correction_event_ledger": correction_ledger_path,  # type: ignore[dict-item]
                    "source_ledger": sources_path,
                    "raw_evidence": raw_evidence_path,  # type: ignore[dict-item]
                    "locator_audit": locator_audit_path,  # type: ignore[dict-item]
                    "collision_audit": collision_audit_path,  # type: ignore[dict-item]
                },
            )
            source_grounding_result = verify_source_grounding(
                task_run_id=task_run_id,
                capture_manifest_path=source_capture_manifest_path,  # type: ignore[arg-type]
                active_evidence_path=raw_evidence_path,  # type: ignore[arg-type]
                locator_audit_path=locator_audit_path,  # type: ignore[arg-type]
                collision_audit_path=collision_audit_path,  # type: ignore[arg-type]
                admission_audit_path=admission_audit_path,  # type: ignore[arg-type]
            )
        except Exception as exc:
            source_grounding_result = {
                "status": "invalid",
                "source_grounded_evidence_validated": False,
                "errors": [str(exc)],
            }
    workbook_result = safe_result(
        "XLSX",
        lambda: audit_xlsx(
            workbook,
            expected_source_ids,
            minimum_pages,
            minimum_effective_samples,
            formal_audit_status,
            formal_retrieval_termination_status,
            execution_records=read_sources(search_log_path) if search_log_path else None,
            source_records=sources,
            execution_schema_context=execution_schema_context_from_state(
                load_runtime_state(state_path), state_path=state_path) if state_path else execution_schema_context,
        ),
    )
    scored_sample_rows = workbook_result.get("metrics", {}).get("scored_sample_rows")
    report_result = safe_result(
        "DOCX",
        lambda: audit_docx(
            report,
            sources,
            minimum_pages,
            minimum_effective_samples,
            int(scored_sample_rows) if isinstance(scored_sample_rows, int) else None,
        ),
    )
    rules_result = safe_result("RULES_XLSX", lambda: audit_rules_xlsx(rules_workbook))
    execution_paths = (
        raw_evidence_path,
        semantic_evidence_path,
        evidence_path,
        search_log_path,
        scores_path,
        dimension_audit_path,
        detailed_log_path,
        skill_root,
        release_manifest_path,
    )
    if all(path is not None for path in execution_paths):
        execution_result = safe_result("EXECUTION_INTEGRITY", lambda: audit_execution_integrity(
            sources_path=sources_path,
            raw_evidence_path=raw_evidence_path,  # type: ignore[arg-type]
            semantic_evidence_path=semantic_evidence_path,  # type: ignore[arg-type]
            formal_evidence_path=evidence_path,  # type: ignore[arg-type]
            search_log_path=search_log_path,  # type: ignore[arg-type]
            scores_path=scores_path,  # type: ignore[arg-type]
            dimension_audit_path=dimension_audit_path,  # type: ignore[arg-type]
            detailed_log_path=detailed_log_path,  # type: ignore[arg-type]
            skill_root=skill_root,  # type: ignore[arg-type]
            release_manifest_path=release_manifest_path,  # type: ignore[arg-type]
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            truth_freeze_path=truth_freeze_path,
            preflight_path=preflight_path,
            execution_schema_context=execution_schema_context,
        ))
    else:
        execution_result = {
            "status": "invalid",
            "errors": [
                "final validation requires raw, semantic, and formal evidence ledgers, "
                "the bound dimension audit, detailed run log, and immutable-release manifest"
            ],
            "warnings": [],
            "metrics": {},
        }
    if all(path is not None for path in (evidence_path, search_log_path, scores_path, report_data_path)):
        scoring_result = safe_result("SCORING_CHAIN", lambda: independent_formal_scoring_audit(
            evidence_path,  # type: ignore[arg-type]
            sources_path,
            search_log_path,  # type: ignore[arg-type]
            scores_path,  # type: ignore[arg-type]
            report_data_path,  # type: ignore[arg-type]
            workbook,
            execution_schema_context=execution_schema_context_from_state(
                load_runtime_state(state_path), state_path=state_path) if state_path else execution_schema_context,
        ))
    else:
        scoring_result = {
            "status": "invalid",
            "errors": [
                "final validation requires bottom evidence, search log, composite scores, and report-data JSON"
            ],
            "warnings": [],
            "metrics": {},
        }
    errors = [f"XLSX: {item}" for item in workbook_result.get("errors", [])]
    from input_safety import office_privacy_errors
    for artifact in (workbook, report, rules_workbook):
        if artifact is not None and artifact.is_file():
            privacy = safe_result("PRIVACY", lambda: {"errors": office_privacy_errors(artifact)})
            errors.extend("PRIVACY: " + issue for issue in privacy.get("errors", []))
    errors.extend(f"DOCX: {item}" for item in report_result.get("errors", []))
    errors.extend(f"RULES_XLSX: {item}" for item in rules_result.get("errors", []))
    errors.extend(f"SCORING_CHAIN: {item}" for item in scoring_result.get("errors", []))
    errors.extend(f"EXECUTION_INTEGRITY: {item}" for item in execution_result.get("errors", []))
    subsystem_specs = (
        (workbook_result, "XLSX_CONTRACT_INVALID", "research_workbook", "presentation", [str(workbook)], True, "generator_code", False),
        (report_result, "DOCX_CONTRACT_INVALID", "docx_report", "presentation", [str(report)], True, "generator_code", False),
        (rules_result, "RULES_XLSX_CONTRACT_INVALID", "rules_workbook", "presentation", [str(rules_workbook)], True, "generator_code", False),
        (scoring_result, "FORMAL_SCORING_RECOMPUTATION_MISMATCH", "formal_scoring_chain", "scoring", [str(evidence_path or ""), str(scores_path or "")], False, "new_run", True),
        (execution_result, "EXECUTION_INTEGRITY_INVALID", "execution_integrity", "truth_freeze", [str(state_path or "")], False, "new_run", True),
    )
    for result, code, subsystem, origin, artifacts, upstream_valid, repair_scope, new_run in subsystem_specs:
        existing_objects = result.get("error_objects", []) if isinstance(result, dict) else []
        if isinstance(existing_objects, list) and existing_objects:
            error_objects.extend(item for item in existing_objects if isinstance(item, dict))
            continue
        for message in result.get("errors", []) if isinstance(result, dict) else []:
            append_error_object(
                code=code,
                subsystem=subsystem,
                origin_layer=origin,
                truth_layer="frozen_formal_chain" if upstream_valid else origin,
                message=str(message),
                artifacts=artifacts,
                upstream_truth_valid=upstream_valid,
                repair_scope=repair_scope,
                requires_new_run=new_run,
            )
    if any(path is not None for path in grounding_artifact_paths) and source_grounding_result.get("status") != "valid":
        for message in source_grounding_result.get("errors", ["source-grounding inputs are missing"]):
            display = "SOURCE_GROUNDING: " + str(message)
            errors.append(display)
            append_error_object(
                code="SOURCE_GROUNDED_EVIDENCE_INVALID",
                subsystem="source_grounding",
                origin_layer="canonical",
                truth_layer="source_snapshot_locator",
                message=str(message),
                artifacts=[str(path or "") for path in grounding_paths],
                upstream_truth_valid=False,
                repair_scope="new_run",
                requires_new_run=True,
            )
    report_truth_result: dict[str, object] = {"status": "not_checked"}
    if report_truth_path is not None and report_narrative_path is not None and report_data_path is not None:
        try:
            truth = json.loads(report_truth_path.read_text(encoding="utf-8-sig"))
            narrative = json.loads(report_narrative_path.read_text(encoding="utf-8-sig"))
            report_data = json.loads(report_data_path.read_text(encoding="utf-8-sig"))
            if not all(isinstance(item, dict) for item in (truth, narrative, report_data)):
                raise ValueError("report truth, narrative, and report data must be objects")
            declared_truth_hash = str(truth.get("report_truth_sha256", ""))
            truth_core = {key: value for key, value in truth.items() if key != "report_truth_sha256"}
            if declared_truth_hash != canonical_sha256(truth_core):
                raise ValueError("report truth self hash is invalid")
            narrative_hash = canonical_sha256(narrative)
            if report_data.get("report_truth_sha256") != declared_truth_hash:
                raise ValueError("report data is not bound to report truth")
            if report_data.get("report_narrative_sha256") != narrative_hash:
                raise ValueError("report data is not bound to report narrative")
            declared_report_hash = str(report_data.get("report_data_sha256", ""))
            report_core = {
                key: value for key, value in report_data.items() if key != "report_data_sha256"
            }
            if declared_report_hash != canonical_sha256(report_core):
                raise ValueError("report data self hash is invalid")
            report_truth_result = {
                "status": "valid",
                "report_truth_sha256": declared_truth_hash,
                "report_narrative_sha256": narrative_hash,
                "report_data_sha256": declared_report_hash,
            }
        except Exception as exc:
            report_truth_result = {"status": "invalid", "errors": [str(exc)]}
            errors.append("REPORT_TRUTH: " + str(exc))
            append_error_object(
                code="REPORT_TRUTH_BINDING_INVALID",
                subsystem="report_truth",
                origin_layer="presentation",
                truth_layer="report_truth",
                message=str(exc),
                artifacts=[str(report_truth_path), str(report_narrative_path), str(report_data_path)],
                upstream_truth_valid=True,
                repair_scope="generator_code",
                requires_new_run=False,
            )
    if state_path is not None and writer_ledger_path is not None:
        protected_artifacts: list[tuple[str, Path]] = [
            ("docx_report", report),
            ("research_workbook", workbook),
            ("rules_workbook", rules_workbook),
        ]
        for role, path in (
            ("source_capture_manifest", source_capture_manifest_path),
            ("canonical_source_ledger", canonical_sources_path),
            ("canonical_evidence_ledger", canonical_evidence_path),
            ("correction_event_ledger", correction_ledger_path),
            ("locator_audit", locator_audit_path),
            ("collision_audit", collision_audit_path),
            ("pre_admission_audit", admission_audit_path),
            ("report_truth", report_truth_path),
            ("report_narrative", report_narrative_path),
        ):
            if path is not None:
                protected_artifacts.append((role, path))
        for role, artifact in protected_artifacts:
            try:
                verify_artifact_writer(
                    state_path=state_path,
                    writer_ledger_path=writer_ledger_path,
                    output_role=role,
                    output_path=artifact,
                )
            except Exception as exc:
                errors.append(f"EXECUTION_INTEGRITY: {exc}")
                append_error_object(
                    code="UNTRUSTED_FORMAL_ARTIFACT_WRITER",
                    subsystem="artifact_provenance",
                    origin_layer="truth_freeze",
                    truth_layer="protected_writer_ledger",
                    message=str(exc),
                    artifacts=[str(artifact)],
                    upstream_truth_valid=False,
                    repair_scope="new_run",
                    requires_new_run=True,
                )
        if report_data_path is not None:
            try:
                verify_artifact_writer(
                    state_path=state_path,
                    writer_ledger_path=writer_ledger_path,
                    output_role="report_data",
                    output_path=report_data_path,
                )
            except Exception as exc:
                errors.append(f"EXECUTION_INTEGRITY: {exc}")
                append_error_object(
                    code="UNTRUSTED_REPORT_DATA_WRITER",
                    subsystem="artifact_provenance",
                    origin_layer="presentation",
                    truth_layer="report_truth",
                    message=str(exc),
                    artifacts=[str(report_data_path)],
                    upstream_truth_valid=True,
                    repair_scope="generator_code",
                    requires_new_run=False,
                )
    workbook_dimensions = (
        workbook_result.get("metrics", {})
        .get("dimension_evidence", {})
        .get("dimensions", {})
    )
    report_dimensions = report_result.get("metrics", {}).get("dimension_evidence_summary", {})
    if isinstance(workbook_dimensions, dict) and isinstance(report_dimensions, dict):
        for dimension in DIMENSIONS:
            workbook_item = workbook_dimensions.get(dimension)
            report_item = report_dimensions.get(dimension)
            if not isinstance(workbook_item, dict) or not isinstance(report_item, dict):
                errors.append(f"CROSS: dimension evidence summary is missing for {dimension}")
                append_error_object(
                    code="CROSS_OFFICE_DIMENSION_SUMMARY_MISSING",
                    subsystem="cross_office",
                    origin_layer="presentation",
                    truth_layer="report_truth",
                    message=f"dimension summary missing: {dimension}",
                    artifacts=[str(workbook), str(report)],
                    upstream_truth_valid=True,
                    repair_scope="generator_code",
                    requires_new_run=False,
                )
                continue
            expected_report_status = {
                "优先目标达标": "达到中高或高置信度优先目标",
                "明确中置信度目标达标": "达到明确选择的中置信度目标",
                "中置信度审计终止": "达到中置信度并通过增强终止独立审计",
                "受控终止后保留合格结果": "检索按受控条件终止，保留符合最低正式门槛的结果并披露缺口",
                "穷尽后仅作定性说明": "经独立审计确认穷尽后仍低于最低要求",
            }.get(str(workbook_item.get("status", "")), "")
            comparisons = {
                "initial_research_weight": workbook_item.get("initial_research_weight"),
                "retrieved_evidence_units": workbook_item.get("retrieved_evidence_units"),
                "topic_coverage_evidence_units": workbook_item.get("topic_coverage_evidence_units"),
                "eligible_evidence_units": workbook_item.get("eligible_evidence_units"),
                "scored_evidence_units": workbook_item.get("scored_evidence_units"),
                "evidence_units": workbook_item.get("evidence_units"),
                "scoring_positive_units": workbook_item.get("scoring_positive_units"),
                "scoring_neutral_units": workbook_item.get("scoring_neutral_units"),
                "scoring_negative_units": workbook_item.get("scoring_negative_units"),
                "valid_scoring_platforms": workbook_item.get("valid_scoring_platforms"),
                "dimension_candidate_platform_count": workbook_item.get(
                    "dimension_candidate_platform_count"
                ),
                "dimension_scorable_platform_count": workbook_item.get(
                    "dimension_scorable_platform_count"
                ),
                "run_included_platform_count": workbook_item.get(
                    "run_included_platform_count"
                ),
                "platform_minimum_rule_status": workbook_item.get("platform_minimum_rule_status"),
                "tendency_score": workbook_item.get("tendency_score"),
                "conversion_score": workbook_item.get("conversion_score"),
                "source_pages": workbook_item.get("source_pages"),
                "source_categories": workbook_item.get("source_categories"),
                "confidence": workbook_item.get("confidence"),
                "status": expected_report_status,
                "targeted_round_count": workbook_item.get("targeted_round_count"),
                "independent_exhaustion_audit_passed": workbook_item.get(
                    "independent_exhaustion_audit_passed"
                ),
                "medium_completion_audit_passed": workbook_item.get(
                    "medium_completion_audit_passed"
                ),
                "medium_completion_basis": workbook_item.get("medium_completion_basis"),
            }
            for field, expected in comparisons.items():
                if report_item.get(field) != expected:
                    message = f"{dimension} {field} differs between DOCX and independently audited XLSX"
                    errors.append("CROSS: " + message)
                    code = (
                        "DOCX_TENDENCY_DISPLAY_PRECISION_MISMATCH"
                        if field in {"tendency_score", "conversion_score"}
                        else "CROSS_OFFICE_DIMENSION_FIELD_MISMATCH"
                    )
                    append_error_object(
                        code=code,
                        subsystem="cross_office",
                        origin_layer="presentation",
                        truth_layer="report_truth",
                        message=message,
                        artifacts=[str(workbook), str(report)],
                        upstream_truth_valid=True,
                        repair_scope="generator_code",
                        requires_new_run=False,
                    )
    report_protocol_metrics = report_result.get("metrics", {})
    rules_protocol_metrics = rules_result.get("metrics", {})
    for field in (
        "evaluation_protocol_version",
        "semantic_codebook_version",
        "semantic_codebook_sha256",
    ):
        report_value = report_protocol_metrics.get(field)
        rules_value = rules_protocol_metrics.get(field)
        if not report_value or report_value != rules_value:
            message = f"{field} differs between the DOCX disclosure and semantic-rules XLSX"
            errors.append("CROSS: " + message)
            append_error_object(
                code="CROSS_OFFICE_PROTOCOL_DISCLOSURE_MISMATCH",
                subsystem="cross_office",
                origin_layer="presentation",
                truth_layer="evaluation_protocol",
                message=message,
                artifacts=[str(report), str(rules_workbook)],
                upstream_truth_valid=True,
                repair_scope="generator_code",
                requires_new_run=False,
            )
    warnings = [f"XLSX: {item}" for item in workbook_result.get("warnings", [])]
    warnings.extend(f"DOCX: {item}" for item in report_result.get("warnings", []))
    warnings.extend(f"RULES_XLSX: {item}" for item in rules_result.get("warnings", []))
    warnings.extend(f"SCORING_CHAIN: {item}" for item in scoring_result.get("warnings", []))
    warnings.extend(f"EXECUTION_INTEGRITY: {item}" for item in execution_result.get("warnings", []))
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "error_count": len(errors),
        "error_objects": error_objects,
        "warnings": warnings,
        "workbook": workbook_result,
        "rules_workbook": rules_result,
        "report": report_result,
        "formal_scoring_chain": scoring_result,
        "execution_integrity": execution_result,
        "source_grounding": source_grounding_result,
        "report_truth_binding": report_truth_result,
        "validation_semantics": {
            "internal_consistency_validated": not any(
                item.get("origin_layer") == "presentation" for item in error_objects
            ),
            "source_grounded_evidence_validated": source_grounding_result.get(
                "source_grounded_evidence_validated", False
            ) is True,
            "formal_research_evidence_chain_validated": (
                not errors
                and source_grounding_result.get("source_grounded_evidence_validated", False) is True
            ),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--rules-workbook", required=True, type=Path)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--raw-evidence", required=True, type=Path)
    parser.add_argument("--semantic-evidence", required=True, type=Path)
    parser.add_argument("--search-log", required=True, type=Path)
    parser.add_argument("--scores", required=True, type=Path)
    parser.add_argument("--report-data", required=True, type=Path)
    parser.add_argument("--dimension-audit", required=True, type=Path)
    parser.add_argument("--detailed-log", required=True, type=Path)
    parser.add_argument("--skill-root", required=True, type=Path)
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--writer-ledger", required=True, type=Path)
    parser.add_argument("--truth-freeze", required=True, type=Path)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--source-capture-manifest", required=True, type=Path)
    parser.add_argument("--canonical-sources", required=True, type=Path)
    parser.add_argument("--canonical-evidence", required=True, type=Path)
    parser.add_argument("--corrections", required=True, type=Path)
    parser.add_argument("--locator-audit", required=True, type=Path)
    parser.add_argument("--collision-audit", required=True, type=Path)
    parser.add_argument("--admission-audit", required=True, type=Path)
    parser.add_argument("--report-truth", required=True, type=Path)
    parser.add_argument("--report-narrative", required=True, type=Path)
    parser.add_argument("--minimum-pages", type=int, default=150)
    parser.add_argument("--minimum-effective-samples", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--error-manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.minimum_pages < 1:
        raise SystemExit("--minimum-pages must be at least 1")
    if args.minimum_effective_samples is not None and args.minimum_effective_samples < 1:
        raise SystemExit("--minimum-effective-samples must be at least 1")
    try:
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="validate_deliverables.py",
            output_role="validation_report",
            expected_phase="VALIDATE",
            output_path=args.output,
        )
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="validate_deliverables.py",
            output_role="validation_error_manifest",
            expected_phase="VALIDATE",
            output_path=args.error_manifest,
        )
        before_freeze = verify_truth_freeze(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            manifest_path=args.truth_freeze,
            require_scoring=True,
        )
        result = validate(
            args.report,
            args.workbook,
            args.rules_workbook,
            args.sources,
            args.evidence,
            args.search_log,
            args.scores,
            args.report_data,
            args.raw_evidence,
            args.semantic_evidence,
            args.dimension_audit,
            args.detailed_log,
            args.skill_root,
            args.release_manifest,
            args.state,
            args.writer_ledger,
            args.truth_freeze,
            args.preflight,
            source_capture_manifest_path=args.source_capture_manifest,
            canonical_sources_path=args.canonical_sources,
            canonical_evidence_path=args.canonical_evidence,
            correction_ledger_path=args.corrections,
            locator_audit_path=args.locator_audit,
            collision_audit_path=args.collision_audit,
            admission_audit_path=args.admission_audit,
            report_truth_path=args.report_truth,
            report_narrative_path=args.report_narrative,
            minimum_pages=args.minimum_pages,
            minimum_effective_samples=args.minimum_effective_samples,
        )
        after_freeze = verify_truth_freeze(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            manifest_path=args.truth_freeze,
            require_scoring=True,
        )
        if before_freeze.get("freeze_sha256") != after_freeze.get("freeze_sha256"):
            message = "EXECUTION_INTEGRITY: post_validate_upstream_mutation"
            result.setdefault("errors", []).append(message)
            result.setdefault("error_objects", []).append(stable_error(
                error_code="POST_VALIDATE_UPSTREAM_MUTATION",
                subsystem="execution_integrity",
                origin_layer="truth_freeze",
                truth_layer="run_truth_freeze",
                message=message,
                affected_artifacts=[str(args.truth_freeze)],
                upstream_truth_valid=False,
                repair_scope="new_run",
                requires_new_run=True,
            ))
            result["status"] = "invalid"
            result["error_count"] = len(result["errors"])
    except (OSError, csv.Error, ET.ParseError, zipfile.BadZipFile, ValueError, json.JSONDecodeError) as exc:
        message = str(exc)
        result = {
            "status": "invalid",
            "errors": [message],
            "error_objects": [stable_error(
                error_code="VALIDATION_EXECUTION_FAILURE",
                subsystem="validation_runtime",
                origin_layer="validation",
                truth_layer="validation_execution",
                message=message,
                affected_artifacts=[str(args.output)],
                upstream_truth_valid=False,
                repair_scope="validator_code",
                requires_new_run=False,
            )],
            "warnings": [],
            "error_count": 1,
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    state = load_runtime_state(args.state)
    error_manifest = build_error_manifest(result, task_run_id=str(state["task_run_id"]))
    error_manifest["repair_plan"] = build_repair_plan(error_manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_tmp = args.output.with_name(args.output.name + ".tmp")
    output_tmp.write_text(payload, encoding="utf-8")
    os.replace(output_tmp, args.output)
    args.error_manifest.parent.mkdir(parents=True, exist_ok=True)
    error_tmp = args.error_manifest.with_name(args.error_manifest.name + ".tmp")
    error_tmp.write_text(
        json.dumps(error_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(error_tmp, args.error_manifest)
    validation_inputs = {
        "report": args.report,
        "workbook": args.workbook,
        "rules_workbook": args.rules_workbook,
        "sources": args.sources,
        "formal_evidence": args.evidence,
        "raw_evidence": args.raw_evidence,
        "semantic_evidence": args.semantic_evidence,
        "search_log": args.search_log,
        "scores": args.scores,
        "report_data": args.report_data,
        "dimension_audit": args.dimension_audit,
        "detailed_log": args.detailed_log,
        "truth_freeze": args.truth_freeze,
        "preflight": args.preflight,
        "source_capture_manifest": args.source_capture_manifest,
        "canonical_sources": args.canonical_sources,
        "canonical_evidence": args.canonical_evidence,
        "corrections": args.corrections,
        "locator_audit": args.locator_audit,
        "collision_audit": args.collision_audit,
        "admission_audit": args.admission_audit,
        "report_truth": args.report_truth,
        "report_narrative": args.report_narrative,
    }
    register_protected_artifact(
        state_path=args.state,
        writer_ledger_path=args.writer_ledger,
        writer_script_id="validate_deliverables.py",
        output_role="validation_report",
        output_path=args.output,
        input_paths=validation_inputs,
        expected_phase="VALIDATE",
    )
    register_protected_artifact(
        state_path=args.state,
        writer_ledger_path=args.writer_ledger,
        writer_script_id="validate_deliverables.py",
        output_role="validation_error_manifest",
        output_path=args.error_manifest,
        input_paths={"validation": args.output},
        expected_phase="VALIDATE",
    )
    print(json.dumps({"status": result["status"], "output": str(args.output.resolve()), "error_manifest": str(args.error_manifest.resolve())}, ensure_ascii=False))
    return 0 if result["status"] == "valid" else 1


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ['verify-delivery']:
        parser=argparse.ArgumentParser(description='Read-only independent verification of the sealed deliverable manifest')
        parser.add_argument('--state',type=Path,required=True)
        parser.add_argument('--writer-ledger',type=Path,required=True)
        parser.add_argument('--manifest',type=Path,required=True)
        args=parser.parse_args(sys.argv[2:])
        try:
            from artifact_provenance import verify_delivery_provenance, _artifact_record_path
            manifest=verify_delivery_provenance(state_path=args.state,writer_ledger_path=args.writer_ledger,
                seal_path=args.manifest)
            validation=_artifact_record_path(manifest['artifacts']['validation'],args.state)
            if json.loads(validation.read_text(encoding='utf-8-sig')).get('status')!='valid':
                raise ValueError('deliverable_manifest_validation_not_valid')
            print(json.dumps({'status':'valid','task_run_id':manifest['task_run_id'],
                'manifest_sha256':manifest['seal_sha256'],'read_only':True},ensure_ascii=False))
            raise SystemExit(0)
        except (ValueError,OSError,KeyError) as exc:
            print(json.dumps({'status':'invalid','errors':[str(exc)]},ensure_ascii=False))
            raise SystemExit(1)
    raise SystemExit(main())
