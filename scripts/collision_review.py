#!/usr/bin/env python3
"""Signed review contract for cross-source text-collision decisions."""

from __future__ import annotations

import hashlib
import hmac
import strict_json as json
from temporal_fields import review_time
from pathlib import Path
from typing import Mapping
from review_trust import authorize_key, make_proof, verify_proof, PROOF_FIELD


SIGNED_FIELDS = (
    "task_run_id",
    "review_record_id",
    "collision_group_id",
    "evidence_ids",
    "source_ids",
    "page_entity_ids",
    "conclusion",
    "review_basis",
    "reviewer_id",
    "decision_time",
    "review_origin",
)
ALLOWED_CONCLUSIONS = {
    "verified_independent_occurrence",
    "repost",
    "mirror",
    "duplicated_extraction",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def signed_collision_payload(record: Mapping[str, object]) -> dict[str, object]:
    return {field: record.get(field, "") for field in SIGNED_FIELDS}


def collision_payload_sha256(record: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(signed_collision_payload(record))).hexdigest()


def collision_attestation_hmac(record: Mapping[str, object], trusted_key: bytes) -> str:
    if len(trusted_key) < 32:
        raise ValueError("trusted collision-review key must contain at least 32 bytes")
    return hmac.new(
        trusted_key,
        collision_payload_sha256(record).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _string_list(value: object, field: str) -> list[str]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON array") from exc
    if (not isinstance(parsed, list) or not parsed
            or any(not isinstance(item, str) or not item.strip() for item in parsed)
            or len(set(parsed)) != len(parsed)):
        raise ValueError(f"{field} must be a non-empty string array")
    return sorted({str(item).strip() for item in parsed})


def load_trusted_collision_reviews(
    path: Path | None,
    trusted_key_path: Path | None,
    *,
    task_run_id: str,
) -> dict[str, dict[str, object]]:
    if path is None and trusted_key_path is None:
        return {}
    if path is None or trusted_key_path is None:
        raise ValueError("collision review ledger and trusted key must be supplied together")
    key = trusted_key_path.read_bytes()
    if len(key) < 32:
        raise ValueError("trusted collision-review key is missing or too short")
    results: dict[str, dict[str, object]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"collision review line {number} must be an object")
        if str(raw.get("task_run_id", "")) != task_run_id:
            raise ValueError("collision review belongs to another task_run_id")
        group_id = str(raw.get("collision_group_id", "")).strip()
        if not group_id or group_id in results:
            raise ValueError("collision review group is empty or duplicated")
        if str(raw.get("conclusion", "")) not in ALLOWED_CONCLUSIONS:
            raise ValueError("collision review conclusion is invalid")
        if str(raw.get("review_origin", "")) != "trusted_human_input":
            raise ValueError("collision review origin is not trusted")
        for field in ("review_record_id", "review_basis", "reviewer_id", "decision_time"):
            if not str(raw.get(field, "")).strip():
                raise ValueError(f"collision review lacks {field}")
        review_time(raw["decision_time"])
        authorize_key(raw['reviewer_id'], key, 'collision')
        record = dict(raw)
        for field in ("evidence_ids", "source_ids", "page_entity_ids"):
            record[field] = _string_list(raw.get(field), field)
        payload_hash = collision_payload_sha256(record)
        if str(raw.get("decision_payload_sha256", "")) != payload_hash:
            raise ValueError("collision review payload SHA-256 is invalid")
        expected = collision_attestation_hmac(record, key)
        if not hmac.compare_digest(str(raw.get("attestation_hmac_sha256", "")), expected):
            raise ValueError("collision review attestation is invalid")
        record["review_provenance_valid"] = True
        record[PROOF_FIELD] = make_proof(signed_collision_payload(record), key, 'collision')
        results[group_id] = record
    return results


def verify_collision_identity(record):
    payload = verify_proof(record.get(PROOF_FIELD), 'collision')
    if payload != signed_collision_payload(record):
        raise ValueError('collision_review_signed_fields_changed')
    return payload
