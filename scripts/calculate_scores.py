#!/usr/bin/env python3
"""Calculate the seven analysis dimensions and the separate 7+1 result layer."""

from __future__ import annotations

import argparse
import strict_json as json
import math
from pathlib import Path
from statistics import fmean

from dimension_evidence import DIMENSION_NAMES, validate_final_dimension_audit
from dimension_framework import (
    BOTTOM_LINE_DIMENSIONS,
    DIMENSION_WEIGHT_FORMULA,
    DIMENSION_WEIGHTS,
    RESULT_LAYER_NAME,
    WEIGHT_SCHEME_NAME,
)
from formal_scoring import load_protocol
from artifact_provenance import (
    register_protected_artifact,
    verify_artifact_writer,
    verify_truth_freeze,
)
from runtime_guard import authorize_runtime_write, verify_scoring_input_gate

_PROTOCOL = load_protocol()
_FORMAL_SCORING = _PROTOCOL["formal_scoring"]
_PLATFORM_RULE = _FORMAL_SCORING["platform_dimension"]
_AGGREGATION_RULE = _FORMAL_SCORING["platform_aggregation"]
_CONVERSION_RULE = _FORMAL_SCORING["tendency_conversion"]
MENTION_LEVELS = {"A", "B", "C", "D", "E", "U"}
RELIABILITY_LEVELS = {"exact", "bounded", "estimated", "unknown"}
PLATFORM_SAMPLE_STATUSES = {
    str(_PLATFORM_RULE["scored_status"]),
    str(_PLATFORM_RULE["insufficient_status"]),
}


def rounded_score(value: float | None) -> float | None:
    return None if value is None else round(value + 1e-12, 1)


def rounded_ratio(value: float | None) -> float | None:
    return None if value is None else round(value + 1e-12, 4)


def tendency_to_conversion(value: int) -> int:
    if _CONVERSION_RULE.get("formula") != "(tendency + 5) * 10":
        raise ValueError("unsupported or drifted tendency conversion formula")
    return (value + 5) * 10


def parse_dimension(platform: str, name: str, raw: object) -> dict[str, object]:
    if raw is None:
        raise ValueError(f"{platform}/{name}: formal platform dimension object is required")
    if not isinstance(raw, dict):
        raise ValueError(f"{platform}/{name}: dimension must be a formal scoring object")
    entry = raw

    tendency = entry.get("tendency")
    mention_level = entry.get("mention_level", "U")
    required_count_fields = (
        "eligible_evidence_units",
        "scored_evidence_units",
        "scoring_positive_units",
        "scoring_neutral_units",
        "scoring_negative_units",
        "minimum_dimension_units",
    )
    counts: dict[str, int] = {}
    for field in required_count_fields:
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{platform}/{name}: {field} must be a non-negative integer")
        counts[field] = value
    minimum_units = int(_PLATFORM_RULE["minimum_dimension_units"])
    if counts["minimum_dimension_units"] != minimum_units:
        raise ValueError(f"{platform}/{name}: minimum_dimension_units drifted from the fixed protocol")
    status = str(entry.get("platform_sample_status", ""))
    if status not in PLATFORM_SAMPLE_STATUSES:
        raise ValueError(f"{platform}/{name}: invalid platform_sample_status {status!r}")
    minimum_met = entry.get("minimum_sample_met")
    if not isinstance(minimum_met, bool):
        raise ValueError(f"{platform}/{name}: minimum_sample_met must be boolean")
    eligible_units = counts["eligible_evidence_units"]
    scored_units = counts["scored_evidence_units"]
    direction_total = sum(counts[field] for field in (
        "scoring_positive_units", "scoring_neutral_units", "scoring_negative_units"
    ))
    if direction_total != scored_units:
        raise ValueError(f"{platform}/{name}: scoring direction counts must sum to scored_evidence_units")
    expected_scored = eligible_units if eligible_units >= minimum_units else 0
    if scored_units != expected_scored:
        raise ValueError(f"{platform}/{name}: scored_evidence_units does not follow the platform minimum rule")
    expected_status = str(_PLATFORM_RULE["scored_status"] if expected_scored else _PLATFORM_RULE["insufficient_status"])
    if status != expected_status or minimum_met is not bool(expected_scored):
        raise ValueError(f"{platform}/{name}: platform sample status does not match eligible evidence count")

    if tendency is not None:
        if isinstance(tendency, bool) or not isinstance(tendency, (int, float)):
            raise ValueError(f"{platform}/{name}: tendency must be an integer from -5 to 5 or null")
        if not float(tendency).is_integer() or tendency < -5 or tendency > 5:
            raise ValueError(f"{platform}/{name}: tendency must be an integer from -5 to 5")
        tendency = int(tendency)
    if mention_level not in MENTION_LEVELS:
        raise ValueError(f"{platform}/{name}: invalid mention_level {mention_level!r}")
    if expected_scored and tendency is None:
        raise ValueError(f"{platform}/{name}: scored platform dimension requires tendency")
    if not expected_scored and tendency is not None:
        raise ValueError(f"{platform}/{name}: insufficient platform samples cannot have tendency")
    conversion = None if tendency is None else tendency_to_conversion(tendency)
    return {
        "tendency": tendency,
        "conversion": conversion,
        "mention_level": mention_level,
        "evidence_units": scored_units,
        **counts,
        "minimum_sample_met": minimum_met,
        "platform_sample_status": status,
    }


def capped_sample_weights(platforms: list[dict[str, object]], cap: float | None = None) -> dict[str, float]:
    cap = float(_AGGREGATION_RULE["sample_weight_cap"] if cap is None else cap)
    if len(platforms) < 2:
        raise ValueError("at least two platforms are required for capped sample weighting")
    if cap * len(platforms) < 1.0 - 1e-12:
        raise ValueError("weight cap is infeasible for the number of platforms")

    counts = {str(item["platform"]): float(item["effective_samples"]) for item in platforms}
    if any(value <= 0 for value in counts.values()):
        raise ValueError("sample-weighted platforms must have positive effective_samples")

    result = {name: 0.0 for name in counts}
    active = list(counts)
    remaining = 1.0
    while active:
        total = sum(counts[name] for name in active)
        tentative = {name: remaining * counts[name] / total for name in active}
        over = [name for name, weight in tentative.items() if weight > cap + 1e-12]
        if not over:
            for name, weight in tentative.items():
                result[name] = weight
            break
        for name in over:
            result[name] = cap
            active.remove(name)
            remaining -= cap

    total_weight = sum(result.values())
    if not math.isclose(total_weight, 1.0, abs_tol=1e-9):
        raise RuntimeError(f"sample weights sum to {total_weight}, not 1")
    if max(result.values()) > cap + 1e-9:
        raise RuntimeError("sample weight cap was not enforced")
    return result


def calculate(data: dict[str, object]) -> dict[str, object]:
    verify_scoring_input_gate(data)
    task_run_id = str(data.get("task_run_id", "")).strip()
    if not task_run_id:
        raise ValueError("formal scoring input requires task_run_id")
    place_name = str(data.get("place_name", "")).strip()
    if not place_name:
        raise ValueError("place_name is required")
    if data.get("formal_scoring_schema_version") != _FORMAL_SCORING["schema_version"]:
        raise ValueError("formal_scoring_schema_version is required and must match the fixed protocol")
    if data.get("evaluation_protocol_version") != _PROTOCOL["evaluation_protocol_version"]:
        raise ValueError("evaluation_protocol_version does not match the fixed protocol")
    if data.get("semantic_codebook_version") != _PROTOCOL["semantic_codebook_version"]:
        raise ValueError("semantic_codebook_version does not match the fixed protocol")
    if data.get("dimension_assignment_field") != "primary_dimension":
        raise ValueError("formal scoring dimension assignment must be primary_dimension")
    declared_scheme = data.get("dimension_weight_scheme")
    if declared_scheme is not None and str(declared_scheme).strip() != WEIGHT_SCHEME_NAME:
        raise ValueError(f"dimension_weight_scheme must be {WEIGHT_SCHEME_NAME!r}")
    declared_weights = data.get("dimension_weights")
    if declared_weights is not None:
        if not isinstance(declared_weights, dict) or set(declared_weights) != set(DIMENSION_WEIGHTS):
            raise ValueError("dimension_weights must contain exactly the seven canonical dimensions")
        for name, expected in DIMENSION_WEIGHTS.items():
            actual = declared_weights.get(name)
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                raise ValueError(f"dimension_weights/{name}: weight must be numeric")
            if not math.isclose(float(actual), expected, abs_tol=1e-12):
                raise ValueError(
                    f"dimension_weights/{name}: must equal canonical {WEIGHT_SCHEME_NAME} {expected:.0%}"
                )
    raw_dimension_audit = data.get("dimension_evidence_audit")
    if not isinstance(raw_dimension_audit, dict):
        raise ValueError("dimension_evidence_audit is required before scoring")
    dimension_audit = validate_final_dimension_audit(raw_dimension_audit)
    audited_dimensions = dimension_audit["dimensions"]
    assert isinstance(audited_dimensions, dict)
    raw_platforms = data.get("platforms")
    if not isinstance(raw_platforms, list) or not raw_platforms:
        raise ValueError("platforms must be a non-empty array")

    seen_platforms: set[str] = set()
    platforms: list[dict[str, object]] = []
    warnings: list[str] = []

    for raw in raw_platforms:
        if not isinstance(raw, dict):
            raise ValueError("each platform must be an object")
        platform = str(raw.get("platform", "")).strip()
        if not platform:
            raise ValueError("each platform requires a name")
        if platform in seen_platforms:
            raise ValueError(f"duplicate platform: {platform}")
        seen_platforms.add(platform)

        eligible_samples = raw.get("eligible_samples")
        effective_samples = raw.get("effective_samples")
        if isinstance(eligible_samples, bool) or not isinstance(eligible_samples, int) or eligible_samples < 0:
            raise ValueError(f"{platform}: eligible_samples must be a non-negative integer")
        if (
            isinstance(effective_samples, bool)
            or not isinstance(effective_samples, int)
            or effective_samples < 0
        ):
            raise ValueError(f"{platform}: effective_samples must be a non-negative integer")
        reliability = raw.get("sample_count_reliability", "unknown")
        if reliability not in RELIABILITY_LEVELS:
            raise ValueError(f"{platform}: invalid sample_count_reliability {reliability!r}")
        raw_dimensions = raw.get("dimensions")
        if not isinstance(raw_dimensions, dict):
            raise ValueError(f"{platform}: dimensions must be an object")
        unknown_dimensions = sorted(set(raw_dimensions) - set(DIMENSION_WEIGHTS))
        if unknown_dimensions:
            raise ValueError(f"{platform}: unknown dimensions: {', '.join(unknown_dimensions)}")
        missing_dimensions = [name for name in DIMENSION_NAMES if name not in raw_dimensions]
        if missing_dimensions:
            raise ValueError(
                f"{platform}: all seven analysis dimensions must be present; missing: {', '.join(missing_dimensions)}"
            )

        dimensions = {
            name: parse_dimension(platform, name, raw_dimensions.get(name))
            for name in DIMENSION_WEIGHTS
        }
        if eligible_samples != sum(int(item["eligible_evidence_units"]) for item in dimensions.values()):
            raise ValueError(f"{platform}: eligible_samples does not equal dimension candidates")
        if effective_samples != sum(int(item["scored_evidence_units"]) for item in dimensions.values()):
            raise ValueError(f"{platform}: effective_samples does not equal actual scored evidence")
        for name, item in dimensions.items():
            audited = audited_dimensions[name]
            if audited.get("numeric_score_permitted") is not True:
                item["diagnostic_conversion"] = None
                item["conversion"] = None
                item["formal_dimension_score_status"] = "dimension_scoring_insufficient"
            else:
                item["diagnostic_conversion"] = item["conversion"]
                item["formal_dimension_score_status"] = "scored" if item["conversion"] is not None else "missing_required_score"
        available = [name for name, value in dimensions.items() if value["conversion"] is not None]
        coverage_weight = sum(DIMENSION_WEIGHTS[name] for name in available)
        if available:
            partial_platform_score = sum(
                float(dimensions[name]["conversion"]) * DIMENSION_WEIGHTS[name] for name in available
            ) / coverage_weight
        else:
            partial_platform_score = None
        platform_score = partial_platform_score if len(available) == len(DIMENSION_WEIGHTS) else None
        if partial_platform_score is not None and effective_samples == 0:
            raise ValueError(f"{platform}: cannot have scored dimensions with zero effective samples")
        if coverage_weight < 1.0 and partial_platform_score is not None:
            warnings.append(
                f"{platform}: partial dimension result covers {coverage_weight:.0%}; overall platform score is withheld rather than redistributing missing dimension weight"
            )

        platforms.append(
            {
                "platform": platform,
                "eligible_samples": eligible_samples,
                "effective_samples": effective_samples,
                "sample_count_reliability": reliability,
                "dimensions": dimensions,
                "coverage_weight": rounded_ratio(coverage_weight),
                "platform_score": rounded_score(platform_score),
                "partial_platform_score": rounded_score(partial_platform_score),
                "complete_seven_dimensions": len(available) == len(DIMENSION_WEIGHTS),
            }
        )

    scored = [item for item in platforms if item["partial_platform_score"] is not None]
    minimum_platform_samples = int(_AGGREGATION_RULE["minimum_platform_effective_samples"])
    normally_eligible = [item for item in scored if int(item["effective_samples"]) >= minimum_platform_samples]
    all_small_exception = False
    if normally_eligible:
        included = normally_eligible
    else:
        included = scored
        all_small_exception = bool(scored)
        if scored:
            warnings.append(
                f"all scored platforms have fewer than {minimum_platform_samples} samples; exploratory all-small exception used"
            )

    excluded = [
        str(item["platform"])
        for item in scored
        if item not in included
    ]
    if excluded:
        warnings.append(
            f"platforms excluded for fewer than {minimum_platform_samples} samples: {', '.join(excluded)}"
        )

    partial_equal_score = (
        rounded_score(fmean(float(item["partial_platform_score"]) for item in included))
        if included else None
    )
    sample_weights: dict[str, float] | None = None
    sample_weighted_reliability = "unavailable"
    if len(included) >= 2:
        sample_weights = capped_sample_weights(included)
        sample_weighted_reliability = (
            "exact"
            if all(item["sample_count_reliability"] == "exact" for item in included)
            else "limited"
        )
        if sample_weighted_reliability != "exact":
            warnings.append("sample-weighted result uses at least one non-exact sample count")
    elif len(included) == 1:
        warnings.append("only one platform is eligible; sample-weighted cross-platform score is unavailable")
    else:
        warnings.append("no platform has a calculable score")

    cross_dimensions: dict[str, object] = {}
    for name in DIMENSION_WEIGHTS:
        available_platforms = [
            item for item in included if item["dimensions"][name]["conversion"] is not None
        ]
        equal_dimension = (
            rounded_score(
                fmean(float(item["dimensions"][name]["conversion"]) for item in available_platforms)
            )
            if available_platforms
            else None
        )
        weighted_dimension = None
        dimension_sample_weights = None
        if len(available_platforms) >= 2:
            dimension_sample_weights = capped_sample_weights(available_platforms)
            weighted_dimension = rounded_score(
                sum(
                    dimension_sample_weights[str(item["platform"])]
                    * float(item["dimensions"][name]["conversion"])
                    for item in available_platforms
                )
            )
        audit_item = audited_dimensions[name]
        eligible_count = sum(int(item["dimensions"][name]["eligible_evidence_units"]) for item in platforms)
        scored_count = sum(int(item["dimensions"][name]["scored_evidence_units"]) for item in platforms)
        positive_count = sum(int(item["dimensions"][name]["scoring_positive_units"]) for item in platforms)
        neutral_count = sum(int(item["dimensions"][name]["scoring_neutral_units"]) for item in platforms)
        negative_count = sum(int(item["dimensions"][name]["scoring_negative_units"]) for item in platforms)
        candidate_platform_count = sum(
            1 for item in platforms
            if int(item["dimensions"][name]["eligible_evidence_units"]) > 0
        )
        for field, actual in (
            ("eligible_evidence_units", eligible_count),
            ("scored_evidence_units", scored_count),
            ("scoring_positive_units", positive_count),
            ("scoring_neutral_units", neutral_count),
            ("scoring_negative_units", negative_count),
        ):
            if int(audit_item.get(field, -1)) != actual:
                raise ValueError(f"dimension evidence audit and platform input disagree on {name}/{field}")
        if audit_item.get("numeric_score_permitted") is True and not available_platforms:
            raise ValueError(f"dimension marked scoring-sufficient but has no formal platform score: {name}")
        if audit_item.get("numeric_score_permitted") is not True and available_platforms:
            raise ValueError(f"dimension is scoring-insufficient but has a formal dimension score: {name}")
        cross_dimensions[name] = {
            "weight": DIMENSION_WEIGHTS[name],
            "evidence_audit": audit_item,
            "eligible_evidence_units": eligible_count,
            "scored_evidence_units": scored_count,
            "scoring_positive_units": positive_count,
            "scoring_neutral_units": neutral_count,
            "scoring_negative_units": negative_count,
            "valid_scoring_platforms": len(available_platforms),
            "dimension_candidate_platform_count": candidate_platform_count,
            "dimension_scorable_platform_count": len(available_platforms),
            "run_included_platform_count": len(included),
            "platform_minimum_rule_status": (
                "scored" if available_platforms else "insufficient_platform_samples"
            ),
            "scoring_confidence": audit_item.get("scoring_confidence", audit_item.get("confidence")),
            "available_platforms": [str(item["platform"]) for item in available_platforms],
            "platform_equal_score": equal_dimension,
            "sample_weighted_score": weighted_dimension,
            "sample_weights": (
                {key: rounded_ratio(value) for key, value in dimension_sample_weights.items()}
                if dimension_sample_weights
                else None
            ),
        }

    complete_cross_dimensions = all(
        cross_dimensions[name]["platform_equal_score"] is not None for name in DIMENSION_WEIGHTS
    )
    equal_score = (
        rounded_score(
            sum(
                float(cross_dimensions[name]["platform_equal_score"]) * DIMENSION_WEIGHTS[name]
                for name in DIMENSION_WEIGHTS
            )
        )
        if complete_cross_dimensions
        else None
    )
    complete_weighted_dimensions = all(
        cross_dimensions[name]["sample_weighted_score"] is not None for name in DIMENSION_WEIGHTS
    )
    sample_weighted_score = (
        rounded_score(
            sum(
                float(cross_dimensions[name]["sample_weighted_score"]) * DIMENSION_WEIGHTS[name]
                for name in DIMENSION_WEIGHTS
            )
        )
        if complete_weighted_dimensions
        else None
    )
    if partial_equal_score is not None and equal_score is None:
        warnings.append(
            "at least one dimension lacks a cross-platform score; overall score is withheld and no weight is redistributed"
        )

    numerical_bottom_line_flags = [
        name
        for name in BOTTOM_LINE_DIMENSIONS
        if isinstance(cross_dimensions[name]["platform_equal_score"], (int, float))
        and float(cross_dimensions[name]["platform_equal_score"]) <= 40
    ]

    return {
        "status": "valid",
        "task_run_id": task_run_id,
        "formal_scoring_schema_version": _FORMAL_SCORING["schema_version"],
        "evaluation_protocol_version": _PROTOCOL["evaluation_protocol_version"],
        "semantic_codebook_version": _PROTOCOL["semantic_codebook_version"],
        "dimension_assignment_field": "primary_dimension",
        "place_name": place_name,
        "dimension_weight_scheme": WEIGHT_SCHEME_NAME,
        "dimension_weights": DIMENSION_WEIGHTS,
        "dimension_weight_formula": DIMENSION_WEIGHT_FORMULA,
        "dimension_evidence_audit": dimension_audit,
        "formal_scoring_input": data,
        "platform_results": platforms,
        "cross_platform": {
            "included_platforms": [str(item["platform"]) for item in included],
            "excluded_small_platforms": excluded,
            "all_small_exception": all_small_exception,
            "platform_equal_score": equal_score,
            "partial_platform_equal_score": partial_equal_score,
            "sample_weighted_score": sample_weighted_score,
            "sample_weighted_reliability": sample_weighted_reliability,
            "sample_weights": (
                {key: rounded_ratio(value) for key, value in sample_weights.items()}
                if sample_weights
                else None
            ),
            "dimensions": cross_dimensions,
        },
        "result_layer": {
            "name": RESULT_LAYER_NAME,
            "role": "七个分析维度共同形成的综合结果层，不是第八个并列维度",
            "weighting_note": "本结果层由七维初始研究权重聚合形成，自身不设置并列权重，也不重复参与加权。",
            "base_composite_score": equal_score,
            "sample_weighted_sensitivity_score": sample_weighted_score,
            "bottom_line_dimensions": list(BOTTOM_LINE_DIMENSIONS),
            "numerical_bottom_line_flags": numerical_bottom_line_flags,
            "required_outputs": [
                "综合分",
                "核心优势",
                "核心风险",
                "底线性问题",
                "证据充分度",
                "评价置信度",
            ],
            "interpretation_rule": (
                "基础综合分不得单独替代活态传承判断；真实性损失、居民退出、文化实践消失或保护底线问题不能由人流、商业收益或其他维度高分补偿。"
            ),
        },
        "recommendation": {
            "final_score": None,
            "instruction": (
                "在报告中选择并论证综合分，同时填写核心优势、核心风险、底线性问题、证据充分度和评价置信度；样本口径不可比时优先平台等权，且不得用综合分掩盖底线性问题。"
            ),
        },
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--writer-ledger", required=True, type=Path)
    parser.add_argument("--truth-freeze", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output is None:
        raise SystemExit("formal scoring requires --output")
    try:
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="calculate_scores.py",
            output_role="scoring_output",
            expected_phase="SCORE",
            output_path=args.output,
        )
        verify_truth_freeze(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            manifest_path=args.truth_freeze,
        )
        verify_artifact_writer(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            output_role="platform_scores",
            output_path=args.input,
        )
        data = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("input root must be a JSON object")
        result = calculate(data)
        exit_code = 0
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        result = {"status": "invalid", "errors": [str(exc)]}
        exit_code = 1

    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(args.output)
        if exit_code == 0:
            register_protected_artifact(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                writer_script_id="calculate_scores.py",
                output_role="scoring_output",
                output_path=args.output,
                input_paths={"platform_scores": args.input, "truth_freeze": args.truth_freeze},
                expected_phase="SCORE",
            )
        print(json.dumps({"status": result["status"], "output": str(args.output.resolve())}, ensure_ascii=False))
    else:
        print(text, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
