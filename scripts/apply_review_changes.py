#!/usr/bin/env python3
"""Apply schema-constrained human review decisions without granting scoring eligibility.

The decision file may confirm semantic judgements, but it may not write any
formal-scoring field.  The enriched evidence must subsequently pass through
``quantify_text_semantics.py`` and the released formal-scoring chain.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import strict_json as json
import os
import re
from temporal_fields import review_time
from pathlib import Path

from dimension_framework import DIMENSION_NAMES
from formal_scoring import FORMAL_SCORING_FIELDS
from quantify_text_semantics import SEMANTIC_FIELDS
from artifact_provenance import register_protected_artifact, verify_artifact_writer
from runtime_guard import authorize_runtime_write, canonical_sha256
from review_trust import authorize_key, make_proof, PROOF_FIELD


REQUIRED_DECISION_FIELDS = {
    "task_run_id",
    "evidence_id",
    "primary_dimension",
    "sentiment",
    "stance_strength",
    "reviewer_id",
    "decision",
    "decision_time",
    "decision_reason",
    "review_origin",
    "before_sha256",
    "review_record_id",
    "decision_payload_sha256",
    "attestation_hmac_sha256",
}
OPTIONAL_DECISION_FIELDS = {"semantic_method"}
ALLOWED_METHODS = {"manual_code", "assisted_semantic"}
ALLOWED_SENTIMENTS = {"positive", "neutral", "negative"}
FORBIDDEN_DECISION_FIELDS = {
    *FORMAL_SCORING_FIELDS,
    "used_for_scoring",
    "score_scope",
    "tendency",
    "formal_score",
    "conversion_score",
    "weighted_score",
    "composite_score",
    "semantic_score_raw",
    "semantic_score_final",
    "semantic_confidence",
    "evidence_reliability",
    "aggregation_weight",
}
CHANGE_LEDGER_FIELDS = [
    PROOF_FIELD,
    "task_run_id",
    "change_id",
    "evidence_id",
    "decision_at",
    "coder_id",
    "reviewer_id",
    "review_origin",
    "review_record_id",
    "review_provenance_sha256",
    "reason",
    "before_sha256",
    "after_sha256",
    "before_primary_dimension",
    "after_primary_dimension",
    "before_sentiment",
    "after_sentiment",
    "before_stance_strength",
    "after_stance_strength",
    "semantic_method",
    "adjudication_status",
]

REVIEW_PROVENANCE_FIELDS = [
    PROOF_FIELD,
    "reviewer_id",
    "review_origin",
    "review_record_id",
    "review_decision_time",
    "review_provenance_sha256",
    "review_provenance_valid",
]

SIGNED_DECISION_FIELDS = (
    "task_run_id",
    "evidence_id",
    "primary_dimension",
    "sentiment",
    "stance_strength",
    "reviewer_id",
    "decision",
    "decision_time",
    "decision_reason",
    "review_origin",
    "before_sha256",
    "review_record_id",
    "semantic_method",
)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV 缺少表头：{path}")
        return list(reader.fieldnames), [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def append_change_ledger(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    if exists:
        fields, existing = read_csv(path)
        if fields != CHANGE_LEDGER_FIELDS:
            raise ValueError("人工复核变更台账表头与发布合同不一致")
        existing_ids = {row["change_id"] for row in existing}
        duplicate = sorted(existing_ids & {row["change_id"] for row in rows})
        if duplicate:
            raise ValueError(f"人工复核变更台账存在重复 change_id：{duplicate[:3]}")
    with path.open("a", encoding="utf-8-sig" if not exists else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHANGE_LEDGER_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _decision_time(value: str, *, not_before=()) -> str:
    review_time(value, not_before=not_before)
    return value


def signed_decision_payload(decision: dict[str, str]) -> dict[str, str]:
    return {field: decision.get(field, "") for field in SIGNED_DECISION_FIELDS}


def decision_payload_sha256(decision: dict[str, str]) -> str:
    return canonical_sha256(signed_decision_payload(decision))


def review_attestation_hmac(decision: dict[str, str], trusted_key: bytes) -> str:
    if not trusted_key:
        raise ValueError("可信人工复核密钥为空")
    digest = decision_payload_sha256(decision).encode("ascii")
    return hmac.new(trusted_key, digest, hashlib.sha256).hexdigest()


def validate_review_attestation(decision: dict[str, str], trusted_key: bytes) -> str:
    authorize_key(decision.get('reviewer_id'), trusted_key, 'semantic')
    payload_hash = decision_payload_sha256(decision)
    if decision.get("decision_payload_sha256") != payload_hash:
        raise ValueError("人工复核决定载荷哈希无效")
    expected = review_attestation_hmac(decision, trusted_key)
    if not hmac.compare_digest(decision.get("attestation_hmac_sha256", ""), expected):
        raise ValueError("review_provenance_invalid: 人工复核证明无法由可信过程验证")
    return payload_hash


def _tags_with_dimension(value: str, dimension: str) -> str:
    tags = [part.strip() for part in re.split(r"[;；|]", value) if part.strip()]
    if dimension not in tags:
        tags.append(dimension)
    return ";".join(tags)


def validate_decision_contract(fields: list[str]) -> None:
    field_set = set(fields)
    missing = sorted(REQUIRED_DECISION_FIELDS - field_set)
    if missing:
        raise ValueError(f"人工复核决定缺少字段：{', '.join(missing)}")
    forbidden = sorted(FORBIDDEN_DECISION_FIELDS & field_set)
    if forbidden:
        raise ValueError(f"人工复核决定不得填写正式评分或派生字段：{', '.join(forbidden)}")
    unknown = sorted(field_set - REQUIRED_DECISION_FIELDS - OPTIONAL_DECISION_FIELDS)
    if unknown:
        raise ValueError(f"人工复核决定包含合同外字段：{', '.join(unknown)}")


def apply_review_changes(
    evidence_path: Path,
    decisions_path: Path,
    output_path: Path,
    change_ledger_path: Path,
    trusted_review_key_path: Path,
    *,
    evidence_registered_at: str | None = None,
) -> dict[str, object]:
    evidence_fields, evidence = read_csv(evidence_path)
    for field in REVIEW_PROVENANCE_FIELDS:
        if field not in evidence_fields:
            evidence_fields.append(field)
        for row in evidence:
            row.setdefault(field, "")
    decision_fields, decisions = read_csv(decisions_path)
    validate_decision_contract(decision_fields)
    if not trusted_review_key_path.is_file():
        raise ValueError("review_provenance_invalid: 缺少可信人工复核证明密钥")
    trusted_key = trusted_review_key_path.read_bytes()
    if len(trusted_key) < 32:
        raise ValueError("review_provenance_invalid: 可信人工复核密钥强度不足")
    by_id: dict[str, dict[str, str]] = {}
    for row in evidence:
        evidence_id = row.get("evidence_id", "")
        if not evidence_id or evidence_id in by_id:
            raise ValueError("证据台账存在空或重复 evidence_id")
        by_id[evidence_id] = row
    seen: set[str] = set()
    changes: list[dict[str, str]] = []
    for decision in decisions:
        evidence_id = decision["evidence_id"]
        if not evidence_id or evidence_id in seen:
            raise ValueError("人工复核决定存在空或重复 evidence_id")
        seen.add(evidence_id)
        target = by_id.get(evidence_id)
        if target is None:
            raise ValueError(f"人工复核决定无法关联 evidence_id：{evidence_id}")
        task_run_id = decision["task_run_id"]
        if not task_run_id or task_run_id != target.get("task_run_id", ""):
            raise ValueError(f"人工复核决定 task_run_id 不一致：{evidence_id}")
        unresolved = (
            target.get("semantic_review_status", "") == "review_required"
            or target.get("semantic_method", "") == "coding_parse_fallback"
        )
        if not unresolved:
            raise ValueError(f"人工复核只能处理待复核证据：{evidence_id}")
        dimension = decision["primary_dimension"]
        sentiment = decision["sentiment"]
        method = decision.get("semantic_method", "") or "manual_code"
        if dimension not in DIMENSION_NAMES:
            raise ValueError(f"人工复核主维度无效：{evidence_id}")
        if sentiment not in ALLOWED_SENTIMENTS:
            raise ValueError(f"人工复核情感方向无效：{evidence_id}")
        if method not in ALLOWED_METHODS:
            raise ValueError(f"人工复核方法无效：{evidence_id}")
        try:
            stance = int(decision["stance_strength"])
        except ValueError as exc:
            raise ValueError(f"人工复核强度必须为 1 至 5：{evidence_id}") from exc
        if stance not in {1, 2, 3, 4, 5}:
            raise ValueError(f"人工复核强度必须为 1 至 5：{evidence_id}")
        if not decision["reviewer_id"] or not decision["decision_reason"]:
            raise ValueError(f"人工复核必须保留 reviewer_id 与 decision_reason：{evidence_id}")
        if decision["decision"] != "human_confirmed":
            raise ValueError(f"人工复核 decision 必须为 human_confirmed：{evidence_id}")
        if decision["review_origin"] != "trusted_human_input":
            raise ValueError(f"review_provenance_invalid: review_origin 不可信：{evidence_id}")
        if not decision["review_record_id"]:
            raise ValueError(f"人工复核缺少 review_record_id：{evidence_id}")
        bounds = [target[field] for field in ("retrieved_at", "captured_at", "recorded_at", "review_decision_time")
                  if target.get(field)]
        if evidence_registered_at is not None:
            bounds.append(evidence_registered_at)
        decision_at = _decision_time(decision["decision_time"], not_before=bounds)
        before_sha = canonical_sha256(target)
        if decision["before_sha256"] != before_sha:
            raise ValueError(f"人工复核决定与当前证据版本不一致：{evidence_id}")
        provenance_sha = validate_review_attestation(decision, trusted_key)
        trust_proof = make_proof(signed_decision_payload(decision), trusted_key, 'semantic')

        before = dict(target)
        # Clear only derived fields.  The released quantifier and formal scorer
        # will reconstruct them; the decision never grants scoring eligibility.
        for field in [*SEMANTIC_FIELDS, *FORMAL_SCORING_FIELDS]:
            target[field] = ""
        target.update(
            {
                "primary_dimension": dimension,
                "dimension_tags": _tags_with_dimension(target.get("dimension_tags", ""), dimension),
                "sentiment": sentiment,
                "stance_strength": str(stance),
                "semantic_method": method,
                "semantic_review_status": "human_confirmed",
                "coder_id": decision["reviewer_id"],
                "adjudication_status": "human_confirmed",
                "reviewer_id": decision["reviewer_id"],
                "review_origin": decision["review_origin"],
                "review_record_id": decision["review_record_id"],
                "review_decision_time": decision_at,
                "review_provenance_sha256": provenance_sha,
                "review_provenance_valid": "true",
                PROOF_FIELD: trust_proof,
            }
        )
        after_sha = canonical_sha256(target)
        change_id = "REV-" + hashlib.sha256(
            f"{task_run_id}|{evidence_id}|{decision_at}|{after_sha}".encode("utf-8")
        ).hexdigest()[:16]
        changes.append(
            {
                "task_run_id": task_run_id,
                "change_id": change_id,
                "evidence_id": evidence_id,
                "decision_at": decision_at,
                "coder_id": decision["reviewer_id"],
                "reviewer_id": decision["reviewer_id"],
                "review_origin": decision["review_origin"],
                "review_record_id": decision["review_record_id"],
                "review_provenance_sha256": provenance_sha,
                PROOF_FIELD: trust_proof,
                "reason": decision["decision_reason"],
                "before_sha256": before_sha,
                "after_sha256": after_sha,
                "before_primary_dimension": before.get("primary_dimension", ""),
                "after_primary_dimension": dimension,
                "before_sentiment": before.get("sentiment", ""),
                "after_sentiment": sentiment,
                "before_stance_strength": before.get("stance_strength", ""),
                "after_stance_strength": str(stance),
                "semantic_method": method,
                "adjudication_status": "human_confirmed",
            }
        )
    if not changes:
        raise ValueError("人工复核决定为空")
    write_csv(output_path, evidence_fields, evidence)
    append_change_ledger(change_ledger_path, changes)
    return {
        "status": "applied_pending_requantification",
        "task_run_id": changes[0]["task_run_id"],
        "changed_evidence_units": len(changes),
        "output": str(output_path.resolve()),
        "change_ledger": str(change_ledger_path.resolve()),
        "formal_scoring_eligibility_written": False,
        "required_next_action": "rerun_quantify_text_semantics",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--change-ledger", type=Path, required=True)
    parser.add_argument("--trusted-review-key", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="apply_review_changes.py",
            output_role="reviewed_evidence",
            expected_phase="SEMANTIC_QUANTIFICATION",
            output_path=args.output,
        )
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="apply_review_changes.py",
            output_role="review_change_ledger",
            expected_phase="SEMANTIC_QUANTIFICATION",
            output_path=args.change_ledger,
        )
        evidence_writer = verify_artifact_writer(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            output_role="semantic_evidence",
            output_path=args.evidence,
        )
        result = apply_review_changes(
            args.evidence,
            args.decisions,
            args.output,
            args.change_ledger,
            args.trusted_review_key,
            evidence_registered_at=evidence_writer.get("timestamp", ""),
        )
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="apply_review_changes.py",
            output_role="reviewed_evidence",
            output_path=args.output,
            input_paths={"evidence": args.evidence, "decisions": args.decisions},
            expected_phase="SEMANTIC_QUANTIFICATION",
        )
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="apply_review_changes.py",
            output_role="review_change_ledger",
            output_path=args.change_ledger,
            input_paths={"decisions": args.decisions},
            expected_phase="SEMANTIC_QUANTIFICATION",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
