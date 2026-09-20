#!/usr/bin/env python3
"""Calibrate and lock the semantic evaluation protocol for a Skill release.

This is a release-maintenance tool. It must not be run inside an individual
place-research task. A task only reads the released protocol and codebook.
"""

from __future__ import annotations

import argparse
import hashlib
import strict_json as json
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
from input_safety import safe_urlparse as urlparse, normalize_host

from dimension_framework import DIMENSION_WEIGHTS
from quantify_text_semantics import load_codebook


ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets"
DEFAULT_BASE_CODEBOOK = ASSET_ROOT / "semantic-quantification-codebook.json"
DEFAULT_PROTOCOL = ASSET_ROOT / "evaluation-protocol.json"
READABLE_ACCESS = {"full", "partial"}
DECISION_ACTIONS = {"retain", "modify", "add", "deprecate"}
CHANGE_CLASSES = {
    "initial_protocol_lock",
    "scoring_rule_change",
    "semantic_codebook_change",
    "implementation_only",
}
ALLOWED_PATCH_ROOTS = {
    "thresholds",
    "scope",
    "negators",
    "contrast_markers",
    "hedges",
    "sarcasm_or_figurative_cues",
    "degree_modifiers",
    "positive_lexicon",
    "negative_lexicon",
    "neutral_cues",
    "promotion_cues",
    "polarity_concepts",
    "dimensions",
    "confidence_components",
}
LOCKED_TASK_FIELDS = [
    "dimension_weights",
    "score_scale",
    "thresholds",
    "scope",
    "polarity_lexicons",
    "dimension_lexicons",
    "context_rules",
    "confidence_components",
    "coding_parse_rules",
    "formal_scoring",
]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_json_object(path: Path, label: str) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} root must be a JSON object")
    return data


def deep_merge(base: dict[str, object], patch: dict[str, object]) -> dict[str, object]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = deepcopy(value)
    return merged


def validate_manifest(
    manifest: dict[str, object], *, allow_draft: bool = False
) -> dict[str, object]:
    protocol_version = str(manifest.get("evaluation_protocol_version", "")).strip()
    codebook_version = str(manifest.get("semantic_codebook_version", "")).strip()
    released_at = str(manifest.get("released_at", "")).strip()
    change_class = str(manifest.get("change_class", "")).strip()
    if not protocol_version or not codebook_version:
        raise ValueError("release calibration requires evaluation and codebook versions")
    try:
        date.fromisoformat(released_at)
    except ValueError as exc:
        raise ValueError("release calibration date must use YYYY-MM-DD") from exc
    if change_class not in CHANGE_CLASSES:
        raise ValueError(f"release calibration change_class must be one of {sorted(CHANGE_CLASSES)}")

    queries = manifest.get("queries")
    if not isinstance(queries, list) or len(queries) < 3:
        raise ValueError("release calibration must record at least three online method queries")
    for index, item in enumerate(queries, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"release calibration query {index} must be an object")
        if not str(item.get("query", "")).strip() or str(item.get("executed_at", "")).strip() != released_at:
            raise ValueError(f"release calibration query {index} requires a query executed on the release date")

    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) < 3:
        raise ValueError("release calibration must cite at least three readable method sources")
    source_ids: set[str] = set()
    urls: set[str] = set()
    domains: set[str] = set()
    for index, item in enumerate(sources, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"release calibration source {index} must be an object")
        source_id = str(item.get("source_id", "")).strip()
        url = str(item.get("url", "")).strip()
        domain = normalize_host(urlparse(url).hostname) if urlparse(url).hostname else ""
        if not source_id or source_id in source_ids:
            raise ValueError(f"release calibration source {index} requires a unique source_id")
        if not url.startswith(("https://", "http://")) or not domain or url in urls:
            raise ValueError(f"release calibration source {source_id} requires a unique HTTP(S) URL")
        if str(item.get("access_status", "")).strip() not in READABLE_ACCESS:
            raise ValueError(f"release calibration source {source_id} must be full or partial readable")
        if str(item.get("retrieved_at", "")).strip() != released_at:
            raise ValueError(f"release calibration source {source_id} must be checked on the release date")
        if not str(item.get("title", "")).strip() or not str(item.get("finding", "")).strip():
            raise ValueError(f"release calibration source {source_id} requires title and finding")
        source_ids.add(source_id)
        urls.add(url)
        domains.add(domain)
    if len(domains) < 2:
        raise ValueError("release calibration requires at least two independent source domains")

    decisions = manifest.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("release calibration must record at least one decision")
    decision_ids: set[str] = set()
    action_counts: Counter[str] = Counter()
    for index, item in enumerate(decisions, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"release calibration decision {index} must be an object")
        decision_id = str(item.get("decision_id", "")).strip()
        action = str(item.get("action", "")).strip()
        cited = item.get("source_ids")
        if not decision_id or decision_id in decision_ids:
            raise ValueError(f"release calibration decision {index} requires a unique decision_id")
        if action not in DECISION_ACTIONS:
            raise ValueError(f"release calibration decision {decision_id} has invalid action")
        if not str(item.get("target", "")).strip() or not str(item.get("rationale", "")).strip():
            raise ValueError(f"release calibration decision {decision_id} requires target and rationale")
        if not isinstance(cited, list) or not cited:
            raise ValueError(f"release calibration decision {decision_id} requires source_ids")
        unknown = sorted({str(value) for value in cited} - source_ids)
        if unknown:
            raise ValueError(f"release calibration decision {decision_id} cites unknown sources: {', '.join(unknown)}")
        decision_ids.add(decision_id)
        action_counts[action] += 1

    patch = manifest.get("codebook_patch", {})
    if not isinstance(patch, dict):
        raise ValueError("release calibration codebook_patch must be an object")
    forbidden = sorted(set(patch) - ALLOWED_PATCH_ROOTS)
    if forbidden:
        raise ValueError("release calibration patch contains forbidden roots: " + ", ".join(forbidden))
    changing = action_counts["modify"] + action_counts["add"] + action_counts["deprecate"]
    codebook_decisions = [
        item for item in decisions
        if isinstance(item, dict) and str(item.get("affects_locked_codebook", "")).lower() == "true"
    ]
    if patch and not codebook_decisions:
        raise ValueError("a codebook patch requires a decision marked affects_locked_codebook=true")
    if codebook_decisions and not patch and change_class in {"scoring_rule_change", "semantic_codebook_change"}:
        raise ValueError("a locked-codebook change requires a non-empty codebook_patch")
    if changing == 0 and patch:
        raise ValueError("a codebook patch requires a modifying release decision")

    review = manifest.get("release_review")
    if not isinstance(review, dict):
        raise ValueError("release calibration requires release_review")
    if str(review.get("method_review_status", "")) != "approved":
        raise ValueError("release method review must be approved")
    for field in ("regression_status", "benchmark_status"):
        status = str(review.get(field, ""))
        allowed = {"passed"} if not allow_draft else {"planned", "passed"}
        if status not in allowed:
            raise ValueError(f"release {field} must be passed before publication")
    if not str(review.get("review_summary", "")).strip():
        raise ValueError("release review requires a review_summary")

    formal_scoring = manifest.get("formal_scoring")
    if not isinstance(formal_scoring, dict):
        raise ValueError("release calibration requires formal_scoring")
    if formal_scoring.get("dimension_assignment_field") != "primary_dimension":
        raise ValueError("formal scoring dimension assignment must be primary_dimension")
    if formal_scoring.get("topic_coverage_field") != "dimension_tags":
        raise ValueError("formal scoring topic coverage field must be dimension_tags")
    platform_dimension = formal_scoring.get("platform_dimension")
    if not isinstance(platform_dimension, dict):
        raise ValueError("formal scoring requires platform_dimension")
    base_thresholds = read_json_object(DEFAULT_BASE_CODEBOOK, "base semantic codebook").get("thresholds")
    if not isinstance(base_thresholds, dict):
        raise ValueError("base semantic codebook thresholds are invalid")
    if platform_dimension.get("minimum_dimension_units") != base_thresholds.get("minimum_dimension_units"):
        raise ValueError("formal scoring platform minimum must equal the released semantic threshold")
    confidence_thresholds = formal_scoring.get("dimension_confidence_thresholds")
    if not isinstance(confidence_thresholds, dict) or list(confidence_thresholds) != ["中", "中高", "高"]:
        raise ValueError("formal scoring confidence thresholds must be ordered 中, 中高, 高")
    previous: tuple[int, int, int, int] | None = None
    for label, item in confidence_thresholds.items():
        if not isinstance(item, dict):
            raise ValueError(f"formal scoring confidence threshold {label} must be an object")
        current = tuple(
            int(item.get(field, 0))
            for field in ("scored_evidence_units", "distinct_source_pages", "source_categories", "scorable_platforms")
        )
        if any(value < 1 for value in current):
            raise ValueError(f"formal scoring confidence threshold {label} must be positive")
        if previous is not None and any(current[index] < previous[index] for index in range(4)):
            raise ValueError("formal scoring confidence thresholds must be non-decreasing")
        previous = current

    return {
        "evaluation_protocol_version": protocol_version,
        "semantic_codebook_version": codebook_version,
        "released_at": released_at,
        "change_class": change_class,
        "query_count": len(queries),
        "source_count": len(sources),
        "source_domain_count": len(domains),
        "decision_count": len(decisions),
        "mode": "online_updated" if patch else "online_verified_no_change",
        "sources": deepcopy(sources),
        "decisions": deepcopy(decisions),
        "release_review": deepcopy(review),
        "formal_scoring": deepcopy(formal_scoring),
    }


def calibrate_release(
    base_path: Path,
    manifest_path: Path,
    codebook_output: Path,
    protocol_output: Path = DEFAULT_PROTOCOL,
    *,
    allow_draft: bool = False,
) -> dict[str, object]:
    base = read_json_object(base_path, "base semantic codebook")
    manifest_bytes = manifest_path.read_bytes()
    manifest = read_json_object(manifest_path, "release calibration manifest")
    calibration = validate_manifest(manifest, allow_draft=allow_draft)
    patch = manifest.get("codebook_patch", {})
    assert isinstance(patch, dict)
    base.pop("runtime_refresh", None)
    base.pop("release_protocol", None)
    locked = deep_merge(base, patch)
    locked["release_protocol"] = {
        **calibration,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "task_rule_update_policy": "locked_at_skill_release",
        "task_level_rule_mutation_allowed": False,
        "locked_task_fields": LOCKED_TASK_FIELDS,
        "candidate_policy": "unmatched expressions enter a review queue and cannot mutate the released codebook during a task",
    }
    codebook_payload = (json.dumps(locked, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    codebook_sha256 = sha256_bytes(codebook_payload)
    protocol = {
        "protocol_type": "historic_place_vitality_semantic_evaluation",
        "evaluation_protocol_version": calibration["evaluation_protocol_version"],
        "semantic_codebook_version": calibration["semantic_codebook_version"],
        "released_at": calibration["released_at"],
        "change_class": calibration["change_class"],
        "semantic_codebook_file": codebook_output.name,
        "semantic_codebook_sha256": codebook_sha256,
        "task_execution_policy": {
            "rule_snapshot": "locked_at_skill_release",
            "task_level_online_rule_refresh": False,
            "task_level_rule_mutation": False,
            "unknown_expression_action": "start_coding_parse_and_queue_for_review",
            "future_update_route": "release_calibration_candidate_queue",
        },
        "immutable_during_task": LOCKED_TASK_FIELDS,
        "dimension_weights": DIMENSION_WEIGHTS,
        "score_scale": locked.get("score_scale"),
        "thresholds": locked.get("thresholds"),
        "scope": locked.get("scope"),
        "formal_scoring": calibration["formal_scoring"],
        "calibration": {
            "mode": calibration["mode"],
            "query_count": calibration["query_count"],
            "source_count": calibration["source_count"],
            "source_domain_count": calibration["source_domain_count"],
            "decision_count": calibration["decision_count"],
            "manifest_sha256": sha256_bytes(manifest_bytes),
            "sources": calibration["sources"],
            "decisions": calibration["decisions"],
            "release_review": calibration["release_review"],
        },
    }
    codebook_output.parent.mkdir(parents=True, exist_ok=True)
    protocol_output.parent.mkdir(parents=True, exist_ok=True)
    codebook_output.write_bytes(codebook_payload)
    protocol_output.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        load_codebook(codebook_output)
    except Exception:
        codebook_output.unlink(missing_ok=True)
        protocol_output.unlink(missing_ok=True)
        raise
    return {
        "status": "draft" if allow_draft else "released",
        "evaluation_protocol_version": calibration["evaluation_protocol_version"],
        "semantic_codebook_version": calibration["semantic_codebook_version"],
        "semantic_codebook_sha256": codebook_sha256,
        "codebook_output": str(codebook_output.resolve()),
        "protocol_output": str(protocol_output.resolve()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-codebook", type=Path, default=DEFAULT_BASE_CODEBOOK)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--codebook-output", type=Path, default=DEFAULT_BASE_CODEBOOK)
    parser.add_argument("--protocol-output", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--confirm-release-calibration", action="store_true")
    parser.add_argument("--allow-draft", action="store_true", help="Allow planned regression status for pre-release testing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_release_calibration:
        print(json.dumps({"status": "invalid", "errors": ["release calibration requires --confirm-release-calibration"]}, ensure_ascii=False, indent=2))
        return 1
    try:
        result = calibrate_release(
            args.base_codebook,
            args.manifest,
            args.codebook_output,
            args.protocol_output,
            allow_draft=args.allow_draft,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
