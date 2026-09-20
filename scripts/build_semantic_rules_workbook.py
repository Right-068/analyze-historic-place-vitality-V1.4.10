#!/usr/bin/env python3
"""Build the fixed seven-sheet XLSX for non-numeric text quantification rules."""

from __future__ import annotations

import argparse
import hashlib
import strict_json as json
import re
from pathlib import Path
from spreadsheet_safety import safe_hyperlink

try:
    import xlsxwriter
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "xlsxwriter is required to build the semantic-rules XLSX deliverable."
    ) from exc

from artifact_provenance import (
    assert_frozen_release_asset,
    register_protected_artifact,
    verify_preflight,
    verify_truth_freeze,
)
from runtime_guard import authorize_runtime_write

from quantify_text_semantics import detect_language, load_codebook
from dimension_framework import DIMENSION_WEIGHTS
from excel_localization import display_value


RULE_WORKBOOK_SHEETS = [
    "评价协议摘要",
    "发布校准来源",
    "发布校准决策",
    "极性词表",
    "七维词表",
    "上下文规则",
    "编码解析规则",
]

RULE_WORKBOOK_HEADERS = {
    "评价协议摘要": [
        "地点名称", "任务运行编号", "本次检索日期", "评价协议版本", "语义代码簿版本",
        "协议发布日期", "发布校准模式", "校准查询数", "可读方法来源数", "独立来源域名数",
        "校准决策数", "语义代码簿SHA256", "任务期规则变更", "规则应用说明",
    ],
    "发布校准来源": [
        "来源编号", "页面标题", "发布机构", "来源域名", "来源URL", "检索日期", "读取状态",
        "方法领域", "发布校准发现",
    ],
    "发布校准决策": ["决策编号", "动作", "规则目标", "决策理由", "依据来源编号", "是否改变锁定代码簿"],
    "极性词表": [
        "概念编号", "极性", "基础权重", "语言", "词项类型", "词项", "中文释义", "英文释义", "机器读取路径",
    ],
    "七维词表": [
        "维度编号", "中文维度名", "英文维度名", "初始研究权重", "语言", "词项类型", "词项", "中文释义", "英文释义", "机器读取路径",
    ],
    "上下文规则": ["规则类别", "规则编号", "语言", "词项或参数", "参数值", "作用范围", "机器处理", "规则说明"],
    "编码解析规则": ["顺序", "触发条件", "输入", "处理规则", "输出", "阈值或上限", "评分政策", "复核要求"],
}

TABLE_NAMES = {
    "评价协议摘要": "EvaluationProtocolTable",
    "发布校准来源": "ReleaseCalibrationSourcesTable",
    "发布校准决策": "ReleaseCalibrationDecisionsTable",
    "极性词表": "PolarityLexiconTable",
    "七维词表": "DimensionLexiconTable",
    "上下文规则": "ContextRulesTable",
    "编码解析规则": "CodingParseRulesTable",
}

DEFAULT_PROTOCOL = Path(__file__).resolve().parents[1] / "assets" / "evaluation-protocol.json"


def safe_filename_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value.strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    if not cleaned:
        raise ValueError("place name cannot be empty after filename sanitization")
    return cleaned


def expected_output_name(place: str, retrieval_date: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", retrieval_date):
        raise ValueError("retrieval date must use YYYY-MM-DD")
    return f"非量化文本量化评价规则_{safe_filename_component(place)}_{retrieval_date.replace('-', '')}.xlsx"


def workbook_formats(workbook: object) -> dict[str, object]:
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
        "integer": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "border": 1,
                "border_color": "#D9E2F3",
                "num_format": "0",
            }
        ),
        "decimal": workbook.add_format(
            {
                "font_name": "Carlito",
                "font_size": 10,
                "align": "center",
                "valign": "vcenter",
                "border": 1,
                "border_color": "#D9E2F3",
                "num_format": "0.0000",
            }
        ),
        "url": workbook.add_format(
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
    }


def add_fixed_sheet(
    workbook: object,
    formats: dict[str, object],
    name: str,
    rows: list[list[object]],
    widths: list[int],
    url_column: int | None = None,
) -> None:
    headers = RULE_WORKBOOK_HEADERS[name]
    if len(widths) != len(headers):
        raise ValueError(f"fixed width contract does not match headers for {name}")
    worksheet = workbook.add_worksheet(name)
    worksheet.freeze_panes(1, 0)
    worksheet.set_row(0, 34)
    for column, width in enumerate(widths):
        worksheet.set_column(column, column, width)
        worksheet.write(0, column, headers[column], formats["header"])
    displayed = rows if rows else [[""] * len(headers)]
    for row_index, values in enumerate(displayed, start=1):
        worksheet.set_row(row_index, 42)
        for column, value in enumerate(values):
            if url_column == column and isinstance(value, str) and safe_hyperlink(value):
                worksheet.write_url(row_index, column, value, formats["url"], string=value)
            elif isinstance(value, int) and not isinstance(value, bool):
                worksheet.write_number(row_index, column, value, formats["integer"])
            elif isinstance(value, float):
                worksheet.write_number(row_index, column, value, formats["decimal"])
            else:
                worksheet.write(
                    row_index,
                    column,
                    "" if value is None else display_value(headers[column], value),
                    formats["body"],
                )
    worksheet.add_table(
        0,
        0,
        len(displayed),
        len(headers) - 1,
        {
            "name": TABLE_NAMES[name],
            "style": "Table Style Medium 2",
            "columns": [{"header": header, "header_format": formats["header"]} for header in headers],
        },
    )


def protocol_summary_rows(
    place: str,
    task_run_id: str,
    retrieval_date: str,
    protocol: dict[str, object],
) -> list[list[object]]:
    calibration = protocol.get("calibration", {})
    assert isinstance(calibration, dict)
    return [[
        place,
        task_run_id,
        retrieval_date,
        str(protocol.get("evaluation_protocol_version", "")),
        str(protocol.get("semantic_codebook_version", "")),
        str(protocol.get("released_at", "")),
        str(calibration.get("mode", "")),
        int(calibration.get("query_count", 0)),
        int(calibration.get("source_count", 0)),
        int(calibration.get("source_domain_count", 0)),
        int(calibration.get("decision_count", 0)),
        str(protocol.get("semantic_codebook_sha256", "")),
        "否",
        "本工作簿导出 Skill 发布时完成在线校准并锁定的评价协议。单次地点任务不联网改写规则、阈值、权重或词表；未命中表达启动编码解析并进入复核队列，确认前不得计分。",
    ]]


def source_rows(calibration: dict[str, object]) -> list[list[object]]:
    rows: list[list[object]] = []
    values = calibration.get("sources", [])
    if not isinstance(values, list):
        return rows
    for item in values:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))
        domain_match = re.match(r"https?://([^/]+)", url)
        rows.append(
            [
                str(item.get("source_id", "")),
                str(item.get("title", "")),
                str(item.get("publisher", "")),
                domain_match.group(1).lower() if domain_match else "",
                url,
                str(item.get("retrieved_at", "")),
                str(item.get("access_status", "")),
                str(item.get("method_area", "")),
                str(item.get("finding", "")),
            ]
        )
    return rows


def decision_rows(calibration: dict[str, object]) -> list[list[object]]:
    rows: list[list[object]] = []
    decisions = calibration.get("decisions", [])
    if not isinstance(decisions, list):
        return rows
    for item in decisions:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", ""))
        cited = item.get("source_ids", [])
        rows.append(
            [
                str(item.get("decision_id", "")),
                action,
                str(item.get("target", "")),
                str(item.get("rationale", "")),
                "|".join(str(value) for value in cited) if isinstance(cited, list) else "",
                "是" if item.get("affects_locked_codebook") is True else "否",
            ]
        )
    return rows


def polarity_rows(codebook: dict[str, object]) -> list[list[object]]:
    rows: list[list[object]] = []
    represented: set[tuple[str, str]] = set()
    concepts = codebook.get("polarity_concepts", [])
    assert isinstance(concepts, list)
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        concept_id = str(concept.get("concept_id", ""))
        polarity = str(concept.get("polarity", ""))
        weight = float(concept.get("weight", 0))
        gloss = concept.get("gloss", {})
        gloss_zh = str(gloss.get("zh", "")) if isinstance(gloss, dict) else ""
        gloss_en = str(gloss.get("en", "")) if isinstance(gloss, dict) else ""
        for field, entry_type in (("terms", "表层词项"), ("near_synonyms", "近义释义")):
            multilingual = concept.get(field, {})
            if not isinstance(multilingual, dict):
                continue
            for language in ("zh", "en"):
                values = multilingual.get(language, [])
                if not isinstance(values, list):
                    continue
                for term in values:
                    term_text = str(term)
                    represented.add((polarity, term_text.casefold()))
                    rows.append(
                        [
                            concept_id,
                            polarity,
                            weight,
                            language,
                            entry_type,
                            term_text,
                            gloss_zh,
                            gloss_en,
                            f"polarity_concepts.{concept_id}.{field}.{language}",
                        ]
                    )
    for polarity, field in (("positive", "positive_lexicon"), ("negative", "negative_lexicon")):
        lexicon = codebook.get(field, {})
        assert isinstance(lexicon, dict)
        for term, weight in sorted(lexicon.items(), key=lambda item: str(item[0]).casefold()):
            term_text = str(term)
            if (polarity, term_text.casefold()) in represented:
                continue
            language = detect_language(term_text)
            rows.append(
                [
                    f"surface_{polarity}",
                    polarity,
                    float(weight),
                    language,
                    "补充表层词项",
                    term_text,
                    "仅作表层极性线索，具体含义须结合上下文规则。",
                    "Surface polarity cue; interpret only with the contextual rules.",
                    f"{field}.{term_text}",
                ]
            )
    return rows


def dimension_rows(codebook: dict[str, object]) -> list[list[object]]:
    rows: list[list[object]] = []
    dimensions = codebook.get("dimensions", [])
    assert isinstance(dimensions, list)
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        dimension_id = str(dimension.get("dimension_id", ""))
        name_zh = str(dimension.get("name", ""))
        name_en = str(dimension.get("name_en", ""))
        gloss = dimension.get("gloss", {})
        gloss_zh = str(gloss.get("zh", "")) if isinstance(gloss, dict) else ""
        gloss_en = str(gloss.get("en", "")) if isinstance(gloss, dict) else ""
        represented: set[str] = set()
        for field, entry_type in (("cues_by_language", "维度词项"), ("near_synonyms", "近义释义")):
            multilingual = dimension.get(field, {})
            if not isinstance(multilingual, dict):
                continue
            for language in ("zh", "en"):
                values = multilingual.get(language, [])
                if not isinstance(values, list):
                    continue
                for term in values:
                    term_text = str(term)
                    represented.add(term_text.casefold())
                    rows.append(
                        [
                            dimension_id,
                            name_zh,
                            name_en,
                            DIMENSION_WEIGHTS[name_zh],
                            language,
                            entry_type,
                            term_text,
                            gloss_zh,
                            gloss_en,
                            f"dimensions.{dimension_id}.{field}.{language}",
                        ]
                    )
        cues = dimension.get("cues", [])
        if isinstance(cues, list):
            for term in cues:
                term_text = str(term)
                if term_text.casefold() in represented:
                    continue
                rows.append(
                    [
                        dimension_id,
                        name_zh,
                        name_en,
                        DIMENSION_WEIGHTS[name_zh],
                        detect_language(term_text),
                        "补充表层词项",
                        term_text,
                        gloss_zh,
                        gloss_en,
                        f"dimensions.{dimension_id}.cues",
                    ]
                )
    return rows


def context_rows(codebook: dict[str, object]) -> list[list[object]]:
    rows: list[list[object]] = []
    list_rules = [
        ("否定", "negators", "反转局部极性", "在否定窗口内按奇偶数处理"),
        ("转折", "contrast_markers", "切分前后分句并调整权重", "转折前后分别使用代码簿权重"),
        ("弱化", "hedges", "降低局部极性贡献", "仅作用于局部窗口"),
        ("反讽风险", "sarcasm_or_figurative_cues", "标记人工复核", "不得自动通过"),
        ("中性", "neutral_cues", "提供显式中性线索", "不能覆盖相反方向线索"),
        ("推广", "promotion_cues", "标记推广风险", "推广证据不得进入用户评分"),
    ]
    for rule_type, key, processing, explanation in list_rules:
        values = codebook.get(key, [])
        if not isinstance(values, list):
            continue
        for index, term in enumerate(values, start=1):
            term_text = str(term)
            rows.append([rule_type, f"{key}-{index}", detect_language(term_text), term_text, "", "局部或整句", processing, explanation])
    modifiers = codebook.get("degree_modifiers", {})
    if isinstance(modifiers, dict):
        for multiplier, values in modifiers.items():
            if not isinstance(values, list):
                continue
            for index, term in enumerate(values, start=1):
                term_text = str(term)
                rows.append(["程度", f"degree-{multiplier}-{index}", detect_language(term_text), term_text, float(multiplier), "极性词前局部窗口", "乘以局部极性贡献", "选择距离最近的程度词"])
    for section, explanation in (("thresholds", "质量门与候选阈值"), ("scope", "上下文窗口与修正系数")):
        parameters = codebook.get(section, {})
        if not isinstance(parameters, dict):
            continue
        for key, value in parameters.items():
            rows.append(["参数", f"{section}.{key}", "machine", key, value, section, "按字段名读取", explanation])
    return rows


def coding_rule_rows(codebook: dict[str, object]) -> list[list[object]]:
    thresholds = codebook.get("thresholds", {})
    assert isinstance(thresholds, dict)
    minimum = thresholds.get("coding_parse_candidate_similarity", "")
    strong = thresholds.get("coding_parse_strong_similarity", "")
    maximum = thresholds.get("coding_parse_max_candidates_per_type", "")
    return [
        [1, "维度或极性词表未完整命中", "原始可见文字与词表命中轨迹", "识别 full_match、dimension_only、polarity_only、zero_hit", "解析触发状态", "任一层缺失即触发", "候选不得直接计分", "自动记录 review_required"],
        [2, "解析已触发", "原始文字", "检测 zh、en、mixed 或 unknown", "语义语言", "混合语言占比阈值写入程序", "不形成分值", "保留检测轨迹"],
        [3, "解析已触发", "文字与双语概念资料", "英文词形归一；中文二至三字片段；移除停用词", "机器词元集合", "最多保留60个查询词元", "不形成分值", "保留实际词元"],
        [4, "缺失维度或极性", "词元与 terms、near_synonyms、gloss", "计算重叠系数与 Jaccard 的加权相似度", "维度或极性候选", f"候选≥{minimum}；强候选≥{strong}", "相似度不是概率", "候选必须复核"],
        [5, "存在多个候选", "候选相似度", "按相似度降序和概念编号稳定排序", "每类前若干候选", f"每类最多{maximum}项", "不得因排序直接计分", "检查第一、第二候选差异"],
        [6, "词表零命中或部分命中", "规范化原文", "计算稳定 SHA-1 前缀", "OPEN-* 开放编码编号", "12位十六进制前缀", "仅作追踪编号", "不得反推作者身份"],
        [7, "候选已生成或仍未解", "候选、原文与七维定义及边界", "启动结构约束辅助编码或人工开放编码", "候选维度、极性、理由与裁决", "必须绑定同一证据编号", "确认前正式评分资格为否且不进入平台计分", "填写编码者与裁决状态"],
        [8, "人工或辅助编码完成", "经确认的结构化编码", "改记 assisted_semantic 或 manual_code 并保留全部轨迹", "可审计语义编码", "仍须满足置信度与证据可靠性门槛", "只有确认后才可计分", "分歧进入双编码与裁决"],
    ]


def build_rules_workbook(
    codebook_path: Path,
    output_path: Path,
    place: str,
    retrieval_date: str,
    task_run_id: str,
    protocol_path: Path = DEFAULT_PROTOCOL,
) -> dict[str, object]:
    expected = expected_output_name(place, retrieval_date)
    if output_path.name != expected:
        raise ValueError(f"semantic-rules workbook filename must be exactly {expected}")
    if not task_run_id.strip():
        raise ValueError("semantic-rules workbook requires a non-empty task_run_id")
    codebook = load_codebook(codebook_path)
    from input_safety import assert_public_text
    assert_public_text([codebook, place, task_run_id])
    protocol = json.loads(protocol_path.read_text(encoding="utf-8-sig"))
    if not isinstance(protocol, dict):
        raise ValueError("evaluation protocol root must be an object")
    release = codebook.get("release_protocol")
    if not isinstance(release, dict):
        raise ValueError("semantic-rules workbook requires a released locked codebook")
    for field in ("evaluation_protocol_version", "semantic_codebook_version", "released_at"):
        if str(protocol.get(field, "")) != str(release.get(field, "")):
            raise ValueError(f"evaluation protocol and codebook disagree on {field}")
    actual_sha = hashlib.sha256(codebook_path.read_bytes()).hexdigest()
    if str(protocol.get("semantic_codebook_sha256", "")) != actual_sha:
        raise ValueError("semantic codebook SHA-256 does not match the evaluation protocol")
    calibration = protocol.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("evaluation protocol requires release calibration metadata")
    review = calibration.get("release_review")
    if not isinstance(review, dict) or any(
        str(review.get(field, "")) != "passed"
        for field in ("regression_status", "benchmark_status")
    ):
        raise ValueError("evaluation protocol is not publication-ready: regression and benchmark must pass")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(output_path, {"strings_to_formulas": False, "strings_to_urls": False})
    workbook.set_properties(
        {
            "title": "非量化文本量化评价规则",
            "subject": "发布时在线校准并锁定的双语语义评价协议与编码解析规则",
            "author": "Research workflow",
        }
    )
    formats = workbook_formats(workbook)
    add_fixed_sheet(workbook, formats, "评价协议摘要", protocol_summary_rows(place, task_run_id, retrieval_date, protocol), [20, 24, 16, 24, 24, 16, 22, 12, 16, 16, 12, 44, 16, 60])
    add_fixed_sheet(workbook, formats, "发布校准来源", source_rows(calibration), [14, 38, 22, 24, 48, 16, 14, 22, 54], url_column=4)
    add_fixed_sheet(workbook, formats, "发布校准决策", decision_rows(calibration), [14, 12, 28, 52, 24, 20])
    add_fixed_sheet(workbook, formats, "极性词表", polarity_rows(codebook), [26, 12, 12, 10, 16, 28, 46, 52, 42])
    add_fixed_sheet(workbook, formats, "七维词表", dimension_rows(codebook), [30, 24, 34, 14, 10, 16, 28, 48, 52, 46])
    add_fixed_sheet(workbook, formats, "上下文规则", context_rows(codebook), [16, 30, 10, 28, 14, 22, 36, 44])
    add_fixed_sheet(workbook, formats, "编码解析规则", coding_rule_rows(codebook), [8, 30, 36, 52, 34, 28, 34, 36])
    workbook.close()
    return {
        "status": "created",
        "output": str(output_path.resolve()),
        "sheet_names": RULE_WORKBOOK_SHEETS,
        "polarity_rows": len(polarity_rows(codebook)),
        "dimension_rows": len(dimension_rows(codebook)),
        "context_rows": len(context_rows(codebook)),
        "coding_rule_rows": len(coding_rule_rows(codebook)),
        "evaluation_protocol_version": protocol.get("evaluation_protocol_version"),
        "semantic_codebook_version": protocol.get("semantic_codebook_version"),
        "semantic_codebook_sha256": actual_sha,
        "method_source_rows": len(source_rows(calibration)),
        "method_decision_rows": len(decision_rows(calibration)),
    }


def build_blank_template(output_path: Path) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(output_path, {"strings_to_formulas": False, "strings_to_urls": False})
    formats = workbook_formats(workbook)
    widths = {
        "评价协议摘要": [20, 24, 16, 24, 24, 16, 22, 12, 16, 16, 12, 44, 16, 60],
        "发布校准来源": [14, 38, 22, 24, 48, 16, 14, 22, 54],
        "发布校准决策": [14, 12, 28, 52, 24, 20],
        "极性词表": [26, 12, 12, 10, 16, 28, 46, 52, 42],
        "七维词表": [30, 24, 34, 14, 10, 16, 28, 48, 52, 46],
        "上下文规则": [16, 30, 10, 28, 14, 22, 36, 44],
        "编码解析规则": [8, 30, 36, 52, 34, 28, 34, 36],
    }
    for name in RULE_WORKBOOK_SHEETS:
        add_fixed_sheet(workbook, formats, name, [], widths[name], url_column=4 if name == "发布校准来源" else None)
    workbook.close()
    return {"status": "created", "output": str(output_path.resolve()), "sheet_names": RULE_WORKBOOK_SHEETS}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codebook", type=Path)
    parser.add_argument("--place")
    parser.add_argument("--retrieval-date")
    parser.add_argument("--task-run-id")
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--template-only", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--writer-ledger", type=Path)
    parser.add_argument("--truth-freeze", type=Path)
    parser.add_argument("--preflight", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.template_only:
            result = build_blank_template(args.output)
        else:
            if not args.codebook or not args.place or not args.retrieval_date or not args.task_run_id:
                raise ValueError("--codebook, --place, --retrieval-date, and --task-run-id are required outside template mode")
            if not args.state or not args.writer_ledger or not args.truth_freeze or not args.preflight:
                raise ValueError("formal rules workbook requires --state, --writer-ledger, --truth-freeze, and --preflight")
            authorize_runtime_write(
                state_path=args.state,
                writer_script_id="build_semantic_rules_workbook.py",
                output_role="rules_workbook",
                expected_phase="REPORT_BUILD",
                output_path=args.output,
            )
            freeze = verify_truth_freeze(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                manifest_path=args.truth_freeze,
                require_scoring=True,
            )
            assert_frozen_release_asset(freeze, role="semantic_codebook", path=args.codebook)
            assert_frozen_release_asset(freeze, role="evaluation_protocol", path=args.protocol)
            verify_preflight(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                preflight_path=args.preflight,
            )
            result = build_rules_workbook(
                args.codebook,
                args.output,
                args.place.strip(),
                args.retrieval_date.strip(),
                args.task_run_id.strip(),
                args.protocol,
            )
            register_protected_artifact(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                writer_script_id="build_semantic_rules_workbook.py",
                output_role="rules_workbook",
                output_path=args.output,
                input_paths={
                    "codebook": args.codebook,
                    "protocol": args.protocol,
                    "truth_freeze": args.truth_freeze,
                    "preflight": args.preflight,
                },
                expected_phase="REPORT_BUILD",
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
