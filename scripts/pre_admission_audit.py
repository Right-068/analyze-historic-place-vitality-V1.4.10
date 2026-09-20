#!/usr/bin/env python3
"""Audit staged candidates and atomically append immutable canonical records.

This module is the only released writer allowed to create a canonical source or
canonical evidence record.  Model-authored text is never used to complete an
unseen quotation: every text evidence unit must be located in an immutable
source capture before promotion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import strict_json as json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from input_safety import safe_urlparse as urlparse

from artifact_provenance import register_protected_artifact, verify_artifact_writer
from collision_review import load_trusted_collision_reviews
from temporal_fields import date_errors
from public_network import NETWORK_FIELDS
from host_receipts import FIELDS as ATTESTATION_FIELDS, proof_errors
from source_identity import IDENTITY_FIELDS
from input_safety import finite_number, native_numbers, redact_text
from content_blocks import evidence_content_errors
from audit_evidence import (
    CONTENT_LAYERS as SOURCE_CONTENT_LAYERS,
    EVIDENCE_FIELDS,
    SOURCE_CATEGORIES,
    SOURCE_FIELDS,
    USER_CATEGORIES,
)
from dimension_framework import DIMENSION_NAMES
from runtime_guard import (
    authorize_runtime_write,
    canonical_sha256,
    canonical_url,
    normalized_url_sha256,
    sha256_file,
)
from source_capture import (
    TEXT_ACCESS_STATUSES,
    derive_platform,
    resolve_capture_snapshot,
    verify_capture_manifest,
)
from source_identity import (
    annotate_page_entities,
    canonical_url as machine_canonical_url,
    normalize_platform_id,
    page_entity_id_for_source,
    platform_identity,
    trusted_independent_source_ids,
)


ADMISSION_SCHEMA_VERSION = "pre-admission-1"
CANDIDATE_SOURCE_SCHEMA_VERSION = "candidate-source-1"
CANDIDATE_EVIDENCE_SCHEMA_VERSION = "candidate-evidence-1"
CORRECTION_LEDGER_SCHEMA_VERSION = "canonical-corrections-1"

CANONICAL_SOURCE_EXTRA_FIELDS = [
    *NETWORK_FIELDS,
    *ATTESTATION_FIELDS,
    *IDENTITY_FIELDS,
    "research_cutoff",
    "platform_id",
    "original_hostname",
    "platform_resolution_basis",
    "canonical_url",
    "page_entity_id",
    "capture_id",
    "source_capture_id",
    "capture_event_sha256",
    "capture_snapshot_sha256",
    "canonical_record_sha256",
    "promotion_batch_id",
    "promotion_audit_id",
    "promotion_status",
    "recorded_at",
]
CANONICAL_EVIDENCE_EXTRA_FIELDS = [
    "collision_review_proof",
    "block_text_sha256", "visible_body_sha256", "recognition_rule_sha256",
    "capture_id",
    "source_capture_id",
    "candidate_evidence_id",
    "query_id",
    "content_layer",
    "source_user_status",
    "source_snapshot_sha256",
    "source_text_start",
    "source_text_end",
    "source_text_start_offset",
    "source_text_end_offset",
    "source_block_id",
    "source_block_type",
    "source_block_is_user_generated",
    "locator_sha256",
    "evidence_text_sha256",
    "collision_status",
    "cross_source_text_collision_status",
    "collision_group_id",
    "collision_match_method",
    "canonical_record_sha256",
    "promotion_batch_id",
    "promotion_audit_id",
    "promotion_status",
    "promoted_at",
    "recorded_at",
]

USER_UNITS = {"user_post", "user_review", "comment", "reply"}
TEXT_UNITS = {"page_body", *USER_UNITS}
UNIT_TYPES = {"page_body", *USER_UNITS, "source_native_numeric", "official_fact"}
CONTENT_LAYERS = set(SOURCE_CONTENT_LAYERS)
ENTITY_LEVELS = {"poi", "area_direct", "area_aggregate"}
PROMOTION_STATUSES = {"promoted"}
COLLISION_STATUSES = {
    "unique",
    "verified_independent_occurrence",
    "repost",
    "mirror",
    "duplicated_extraction",
    "unresolved_collision",
}
CORRECTION_TYPES = {"retract", "supersede"}
USER_BLOCK_TYPES = USER_UNITS


class AdmissionError(ValueError):
    """A stable pre-admission rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise AdmissionError("missing_staging_input", f"missing JSONL: {path.name}")
    rows: list[dict[str, object]] = []
    from storage_contract import read_staging_bytes
    for number, line in enumerate(read_staging_bytes(path).decode("utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdmissionError("invalid_staging_jsonl", f"invalid JSON at line {number}") from exc
        if not isinstance(item, dict):
            raise AdmissionError("invalid_staging_record", f"line {number} must be an object")
        rows.append(item)
    return rows


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise AdmissionError("invalid_canonical_ledger", f"missing CSV header: {path.name}")
        return list(reader.fieldnames), [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def _write_csv_atomic(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _csv_bytes(
    fields: Sequence[str], rows: Sequence[Mapping[str, object]]
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _transactional_replace(payloads: Mapping[Path, bytes]) -> None:
    """Replace a promotion set with rollback if any path replacement fails."""
    if len(payloads) != len(set(payloads)):
        raise AdmissionError("atomic_promotion_path_collision", "duplicate output path")
    token = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
    staged: dict[Path, Path] = {}
    originals: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        for path, content in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f"{path.name}.pending-{token}")
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if hashlib.sha256(temporary.read_bytes()).digest() != hashlib.sha256(content).digest():
                raise AdmissionError("atomic_promotion_staging_failed", path.name)
            staged[path] = temporary
            originals[path] = path.read_bytes() if path.exists() else None
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except Exception as exc:
        rollback_errors: list[str] = []
        for path in reversed(replaced):
            try:
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    restore = path.with_name(f"{path.name}.rollback-{token}")
                    restore.write_bytes(original)
                    os.replace(restore, path)
            except OSError:
                rollback_errors.append(path.name)
        if rollback_errors:
            raise AdmissionError(
                "atomic_promotion_rollback_failed", ",".join(sorted(rollback_errors))
            ) from exc
        if isinstance(exc, AdmissionError):
            raise
        raise AdmissionError("atomic_promotion_commit_failed", str(exc)) from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _archive_content_addressed(source: Path, archive_dir: Path, label: str) -> Path:
    """Preserve an exact promotion input under a content-addressed name."""
    digest = sha256_file(source)
    suffix = source.suffix or ".bin"
    destination = archive_dir / f"{label}-{digest}{suffix}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != digest:
            raise AdmissionError("promotion_archive_hash_conflict", destination.name)
        return destination
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(source.read_bytes())
    if sha256_file(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise AdmissionError("promotion_archive_copy_failed", source.name)
    os.replace(temporary, destination)
    return destination


def verify_admission_commit(
    path: Path,
    *,
    task_run_id: str,
    output_paths: Mapping[str, Path],
) -> dict[str, object]:
    """Verify the last committed batch and every registered formal output."""
    if not path.exists():
        return {"status": "not_initialized", "batch_count": 0}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("task_run_id") != task_run_id:
        raise AdmissionError("admission_commit_invalid", "admission audit belongs to another run")
    if payload.get("status") != "valid" or not payload.get("atomic_promotion"):
        raise AdmissionError("admission_commit_invalid", "last promotion is not committed")
    declared = payload.get("output_sha256")
    if not isinstance(declared, dict):
        raise AdmissionError("admission_commit_invalid", "output hash manifest is missing")
    for role, output in output_paths.items():
        expected = str(declared.get(role, ""))
        if not output.is_file() or not expected or sha256_file(output) != expected:
            raise AdmissionError("admission_output_hash_mismatch", role)
    commit = str(payload.get("promotion_commit_sha256", ""))
    core = {key: value for key, value in payload.items() if key != "promotion_commit_sha256"}
    if commit != canonical_sha256(core):
        raise AdmissionError("admission_commit_hash_invalid", "promotion commit hash is invalid")
    batches = _read_admission_history(path, task_run_id)
    for batch in batches:
        archives = batch.get("immutable_input_snapshots")
        if not isinstance(archives, dict):
            raise AdmissionError("promotion_archive_missing", str(batch.get("promotion_batch_id", "")))
        for item in archives.values():
            if not isinstance(item, dict):
                raise AdmissionError("promotion_archive_invalid", "archive descriptor must be an object")
            snapshot = Path(str(item.get("path", "")))
            if not snapshot.is_absolute():
                snapshot = (path.parent / snapshot).resolve()
            expected = str(item.get("sha256", ""))
            if not snapshot.is_file() or not expected or sha256_file(snapshot) != expected:
                raise AdmissionError("promotion_archive_hash_mismatch", snapshot.name)
    correction_commits = payload.get("correction_commits", [])
    if not isinstance(correction_commits, list):
        raise AdmissionError("admission_commit_invalid", "correction commits must be an array")
    previous_correction = ""
    for item in correction_commits:
        if not isinstance(item, dict):
            raise AdmissionError("admission_commit_invalid", "correction commit must be an object")
        if item.get("previous_correction_commit_sha256", "") != previous_correction:
            raise AdmissionError("correction_commit_chain_broken", "correction commit chain is broken")
        declared_commit = str(item.get("correction_commit_sha256", ""))
        item_core = {
            key: value for key, value in item.items() if key != "correction_commit_sha256"
        }
        if declared_commit != canonical_sha256(item_core):
            raise AdmissionError("correction_commit_hash_invalid", "correction commit hash is invalid")
        previous_correction = declared_commit
    return {"status": "valid", "batch_count": len(batches), "promotion_commit_sha256": commit}


def _read_admission_history(path: Path, task_run_id: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("task_run_id") != task_run_id:
        raise AdmissionError("admission_history_invalid", "pre-admission audit belongs to another run")
    batches = payload.get("batches", [])
    if not isinstance(batches, list) or not all(isinstance(item, dict) for item in batches):
        raise AdmissionError("admission_history_invalid", "pre-admission batches must be objects")
    previous = ""
    for item in batches:
        if item.get("previous_batch_commit_sha256", "") != previous:
            raise AdmissionError("admission_history_chain_broken", "promotion batch chain is broken")
        commit = str(item.get("batch_commit_sha256", ""))
        core = {key: value for key, value in item.items() if key != "batch_commit_sha256"}
        if commit != canonical_sha256(core):
            raise AdmissionError("admission_history_hash_invalid", "promotion batch hash is invalid")
        previous = commit
    return [dict(item) for item in batches]


def _capture_events(path: Path, task_run_id: str) -> dict[str, dict[str, object]]:
    verify_capture_manifest(path, task_run_id=task_run_id)
    events = _read_jsonl(path)
    return {str(item["capture_id"]): item for item in events}


def _truth(value: object) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "是"}


def _locate_text(body: str, text: str) -> tuple[int, int] | None:
    if not text.strip():
        return None
    start = body.find(text)
    if start < 0:
        return None
    if body.find(text, start + 1) >= 0:
        raise AdmissionError("ambiguous_evidence_text", "provide exact source offsets")
    return start, start + len(text)


def _locator(capture: Mapping[str, object], start: int, end: int) -> dict[str, object]:
    from content_blocks import locate_leaf
    block = locate_leaf(capture, start, end)
    result = {
        "capture_id": str(capture.get("capture_id", "")),
        "snapshot_sha256": str(capture.get("snapshot_sha256", "")),
        "char_start": start, "char_end": end,
        "block_id": block["block_id"], "block_type": block["block_type"],
        "is_user_generated": block["is_user_generated"],
        "block_text_sha256": block["text_sha256"],
        "visible_body_sha256": block["visible_body_sha256"],
        "recognition_rule_sha256": block["recognition_rule_sha256"],
    }
    result["locator_sha256"] = canonical_sha256(result)
    return result


def _validate_unit_layer(unit_type: str, content_layer: str, locator: Mapping[str, object] | None) -> None:
    if unit_type not in UNIT_TYPES:
        raise AdmissionError("invalid_unit_type", f"unsupported unit_type: {unit_type}")
    if content_layer not in CONTENT_LAYERS:
        raise AdmissionError("invalid_content_layer", f"unsupported content_layer: {content_layer}")
    compatibility = evidence_content_errors({
        "unit_type": unit_type,
        "content_layer": content_layer,
        "source_block_id": locator.get("block_id", "") if locator else "",
        "source_block_type": locator.get("block_type", "") if locator else "",
        "source_block_is_user_generated": locator.get("is_user_generated", "") if locator else "",
        "locator_sha256": locator.get("locator_sha256", "") if locator else "",
    }, require_locator=unit_type != "source_native_numeric")
    if compatibility:
        raise AdmissionError(compatibility[0], "evidence content qualification is inconsistent")


def _canonical_digest(row: Mapping[str, object]) -> str:
    return canonical_sha256({
        key: "" if value is None else str(value).strip()
        for key, value in row.items()
        if key != "canonical_record_sha256"
    })


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    return f"{prefix}-" + canonical_sha256(payload)[:20]


def _correction_events(path: Path, task_run_id: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events = _read_jsonl(path)
    previous = ""
    for event in events:
        if event.get("task_run_id") != task_run_id:
            raise AdmissionError("correction_task_mismatch", "correction ledger mixes task runs")
        event_hash = str(event.get("event_sha256", ""))
        core = {key: value for key, value in event.items() if key != "event_sha256"}
        if canonical_sha256(core) != event_hash:
            raise AdmissionError("correction_hash_invalid", "correction event hash is invalid")
        if str(event.get("previous_event_sha256", "")) != previous:
            raise AdmissionError("correction_chain_broken", "correction ledger hash chain is broken")
        previous = event_hash
    return events


def _correction_text(events: Sequence[Mapping[str, object]]) -> str:
    return "".join(json.dumps(dict(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in events)


def _active_ids(events: Sequence[Mapping[str, object]], record_type: str) -> set[str]:
    inactive: set[str] = set()
    for event in events:
        if event.get("record_type") == record_type and event.get("correction_type") in CORRECTION_TYPES:
            inactive.add(str(event.get("target_record_id", "")))
    return inactive


def _materialize_active(
    canonical_sources: Sequence[Mapping[str, object]],
    canonical_evidence: Sequence[Mapping[str, object]],
    correction_events: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    inactive_sources = _active_ids(correction_events, "source")
    inactive_evidence = _active_ids(correction_events, "evidence")
    sources = [dict(row) for row in canonical_sources if str(row.get("source_id", "")) not in inactive_sources]
    source_ids = {str(row.get("source_id", "")) for row in sources}
    evidence = [
        dict(row)
        for row in canonical_evidence
        if str(row.get("evidence_id", "")) not in inactive_evidence
        and str(row.get("source_id", "")) in source_ids
    ]
    return sources, evidence


def _apply_collision_view(
    rows: Sequence[Mapping[str, object]],
    *,
    assignments: Mapping[str, Mapping[str, str]] | None = None,
    source_by_id: Mapping[str, Mapping[str, object]] | None = None,
    trusted_reviews: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Apply current collision decisions to the derived active view only.

    A later promotion can reveal that an older, formerly unique text is part of
    a collision group.  The canonical row remains immutable; this reproducible
    active view removes unresolved groups from scoring without rewriting that
    historical row.
    """
    materialized = [dict(row) for row in rows]
    resolved_assignments = assignments or _collision_assignments(
        materialized,
        source_by_id=source_by_id,
        trusted_reviews=trusted_reviews,
    )
    for row in materialized:
        evidence_id = str(row.get("evidence_id", ""))
        assignment = resolved_assignments.get(evidence_id)
        if assignment is None:
            continue
        status = assignment["status"]
        row["collision_status"] = status
        row["cross_source_text_collision_status"] = status
        row["collision_group_id"] = assignment["group_id"]
        row["collision_match_method"] = assignment["match_method"]
        row['collision_review_proof'] = assignment.get('collision_review_proof', '')
        if status not in {"unique", "verified_independent_occurrence"}:
            row["is_valid"] = "false"
            row["used_for_scoring"] = "false"
            row["formal_scoring_eligible"] = "false"
            row["included_in_platform_score"] = "false"
            row["platform_sample_status"] = "not_eligible"
            row["formal_scoring_exclusion_reasons"] = status
            row["validity_reason"] = status
    return materialized


def _collision_assignments_from_audit(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        raise AdmissionError("collision_audit_invalid", "collision records must be an array")
    from review_trust import verify_proof
    for item in records:
        if isinstance(item, dict) and item.get('collision_status') == 'verified_independent_occurrence':
            try:
                signed = verify_proof(item.get('collision_review_proof'), 'collision')
                if (signed.get('task_run_id') != item.get('task_run_id')
                        or signed.get('conclusion') != item.get('collision_status')
                        or signed.get('collision_group_id') != item.get('collision_group_id')
                        or item.get('evidence_id') not in signed.get('evidence_ids', [])
                        or item.get('source_id') not in signed.get('source_ids', [])):
                    raise ValueError('collision_review_scope_mismatch')
            except (ValueError, TypeError, KeyError) as exc:
                raise AdmissionError('collision_review_identity_invalid', str(exc)) from exc
    return {
        str(item.get("evidence_id", "")): {
            "status": str(item.get("collision_status", "")),
            "group_id": str(item.get("collision_group_id", "")),
            "match_method": str(item.get("collision_match_method", "")),
            "trusted_review_id": str(item.get("trusted_review_id", "")),
            "trusted_review_basis": str(item.get("trusted_review_basis", "")),
            "trusted_review_sha256": str(item.get("trusted_review_sha256", "")),
            "collision_review_proof": item.get('collision_review_proof', ''),
        }
        for item in records
        if isinstance(item, dict) and str(item.get("evidence_id", ""))
    }


def verify_canonical_active_views(
    *,
    task_run_id: str,
    canonical_sources_path: Path,
    canonical_evidence_path: Path,
    correction_ledger_path: Path,
    active_sources_path: Path,
    active_evidence_path: Path,
    collision_audit_path: Path | None = None,
) -> dict[str, object]:
    """Independently reproduce active views from append-only canonical truth."""
    _, canonical_sources = _read_csv(canonical_sources_path)
    _, canonical_evidence = _read_csv(canonical_evidence_path)
    _, active_sources = _read_csv(active_sources_path)
    _, active_evidence = _read_csv(active_evidence_path)
    events = _correction_events(correction_ledger_path, task_run_id)
    for row in canonical_sources:
        if row.get("canonical_record_sha256") != _canonical_digest(row):
            raise AdmissionError("canonical_source_mutation", str(row.get("source_id", "")))
    for row in canonical_evidence:
        if row.get("canonical_record_sha256") != _canonical_digest(row):
            raise AdmissionError("canonical_evidence_mutation", str(row.get("evidence_id", "")))
    expected_sources, expected_evidence = _materialize_active(
        canonical_sources,
        canonical_evidence,
        events,
    )
    collision_assignments: dict[str, dict[str, str]] | None = None
    if collision_audit_path is not None and collision_audit_path.is_file():
        collision_assignments = _collision_assignments_from_audit(collision_audit_path)
    expected_evidence = _apply_collision_view(
        expected_evidence,
        assignments=collision_assignments,
        source_by_id={str(row.get("source_id", "")): row for row in expected_sources},
    )
    if canonical_sha256(expected_sources) != canonical_sha256(active_sources):
        raise AdmissionError("active_source_view_mismatch", "active source view is not reproducible")
    if canonical_sha256(expected_evidence) != canonical_sha256(active_evidence):
        raise AdmissionError("active_evidence_view_mismatch", "active evidence view is not reproducible")
    return {
        "status": "valid",
        "canonical_source_count": len(canonical_sources),
        "canonical_evidence_count": len(canonical_evidence),
        "active_source_count": len(active_sources),
        "active_evidence_count": len(active_evidence),
        "correction_event_count": len(events),
    }


def verify_source_grounding(
    *,
    task_run_id: str,
    capture_manifest_path: Path,
    active_evidence_path: Path,
    locator_audit_path: Path,
    collision_audit_path: Path,
    admission_audit_path: Path,
) -> dict[str, object]:
    """Re-read snapshots and prove every active evidence unit is source-grounded."""
    captures = _capture_events(capture_manifest_path, task_run_id)
    _, evidence = _read_csv(active_evidence_path)
    locator_payload = json.loads(locator_audit_path.read_text(encoding="utf-8-sig"))
    collision_payload = json.loads(collision_audit_path.read_text(encoding="utf-8-sig"))
    history = _read_admission_history(admission_audit_path, task_run_id)
    if not isinstance(locator_payload, dict) or locator_payload.get("task_run_id") != task_run_id:
        raise AdmissionError("locator_audit_invalid", "locator audit belongs to another run")
    if not isinstance(collision_payload, dict) or collision_payload.get("task_run_id") != task_run_id:
        raise AdmissionError("collision_audit_invalid", "collision audit belongs to another run")
    locator_records = locator_payload.get("records", [])
    collision_records = collision_payload.get("records", [])
    if not isinstance(locator_records, list) or not isinstance(collision_records, list):
        raise AdmissionError("grounding_audit_schema_invalid", "audit records must be arrays")
    locators = {str(item.get("evidence_id", "")): item for item in locator_records if isinstance(item, dict)}
    collisions = {str(item.get("evidence_id", "")): item for item in collision_records if isinstance(item, dict)}
    for row in evidence:
        evidence_id = row.get("evidence_id", "")
        capture = captures.get(row.get("capture_id", ""))
        if capture is None:
            raise AdmissionError("evidence_capture_missing", evidence_id)
        if proof_errors(capture, label="capture"):
            raise AdmissionError("unattested_evidence_capture", evidence_id)
        if row.get("source_snapshot_sha256", "") != capture.get("snapshot_sha256", ""):
            raise AdmissionError("evidence_snapshot_hash_mismatch", evidence_id)
        unit_type = row.get("unit_type", "")
        if unit_type == "source_native_numeric":
            if row.get("locator_sha256", "") or row.get("original_summary_text", ""):
                raise AdmissionError("native_numeric_grounding_conflict", evidence_id)
            if any(not row.get(field, "") for field in (
                "native_rating_value", "native_rating_scale_min", "native_rating_scale_max"
            )):
                raise AdmissionError("native_numeric_fields_incomplete", evidence_id)
            observation = capture.get("native_numeric_observation")
            if not isinstance(observation, dict) or any(
                finite_number(row[field]) != finite_number(observation.get(field))
                for field in ("native_rating_value", "native_rating_scale_min", "native_rating_scale_max")
            ):
                raise AdmissionError("native_numeric_snapshot_mismatch", evidence_id)
        else:
            locator = locators.get(evidence_id)
            if locator is None or locator.get("status") != "verified":
                raise AdmissionError("evidence_locator_missing", evidence_id)
            snapshot = resolve_capture_snapshot(
                capture_manifest_path,
                str(capture.get("snapshot_path", "")),
            )
            body = snapshot.read_text(encoding="utf-8")
            try:
                start = int(row.get("source_text_start", ""))
                end = int(row.get("source_text_end", ""))
            except ValueError as exc:
                raise AdmissionError("evidence_locator_invalid", evidence_id) from exc
            text = row.get("original_summary_text", "")
            if not text or start < 0 or end <= start or end > len(body):
                raise AdmissionError("evidence_locator_invalid", evidence_id)
            if body[start:end] != text:
                raise AdmissionError("evidence_text_snapshot_mismatch", evidence_id)
            if row.get("evidence_text_sha256", "") != hashlib.sha256(text.encode("utf-8")).hexdigest():
                raise AdmissionError("evidence_text_hash_mismatch", evidence_id)
            rebuilt = _locator(capture, start, end)
            if row.get("locator_sha256", "") != rebuilt["locator_sha256"]:
                raise AdmissionError("evidence_locator_hash_mismatch", evidence_id)
            if any(str(row.get(k, "")) != str(rebuilt[k]) for k in
                   ("block_text_sha256", "visible_body_sha256", "recognition_rule_sha256")):
                raise AdmissionError("evidence_block_binding_mismatch", evidence_id)
            if (row.get("source_block_id") != rebuilt["block_id"]
                    or row.get("source_block_type") != rebuilt["block_type"]
                    or row.get("source_block_is_user_generated") != str(rebuilt["is_user_generated"]).lower()):
                raise AdmissionError("evidence_block_binding_mismatch", evidence_id)
            _validate_unit_layer(unit_type, row.get("content_layer", ""), rebuilt)
        collision = collisions.get(evidence_id)
        if collision is None or collision.get("collision_status") != row.get("collision_status"):
            raise AdmissionError("collision_audit_mismatch", evidence_id)
        if collision.get("collision_group_id", "") != row.get("collision_group_id", ""):
            raise AdmissionError("collision_group_mismatch", evidence_id)
        if collision.get("collision_match_method", "") != row.get("collision_match_method", ""):
            raise AdmissionError("collision_method_mismatch", evidence_id)
        if row.get("collision_status") not in {"unique", "verified_independent_occurrence"} and _truth(
            row.get("used_for_scoring")
        ):
            raise AdmissionError("unresolved_collision_scored", evidence_id)
    return {
        "status": "valid",
        "source_grounded_evidence_validated": True,
        "active_evidence_count": len(evidence),
        "promotion_batch_count": len(history),
    }


def _collision_assignments(
    rows: Sequence[dict[str, object]],
    source_by_id: Mapping[str, Mapping[str, object]] | None = None,
    trusted_reviews: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, dict[str, str]]:
    """Classify collisions; candidate self-assertions never promote a record."""
    records = [row for row in rows if str(row.get("evidence_id", ""))]
    from input_safety import comparison_text
    normalized = {
        str(row["evidence_id"]): re.sub(
            r"\s+", " ", comparison_text(str(row.get("original_summary_text", "")))
        ).strip().casefold()
        for row in records
    }
    sources = source_by_id or {}
    reviews = dict(trusted_reviews or {})
    from review_trust import verify_proof
    for row in records:
        proof = row.get('collision_review_proof')
        if proof:
            signed = verify_proof(proof, 'collision')
            reviews.setdefault(signed['collision_group_id'], {
                **signed, 'review_trust_proof': proof,
                'decision_payload_sha256': canonical_sha256(signed),
            })
    parent = {str(row["evidence_id"]): str(row["evidence_id"]) for row in records}
    methods: defaultdict[str, set[str]] = defaultdict(set)

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str, method: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            parent[right_root] = left_root
        methods[left].add(method)
        methods[right].add(method)

    exact: defaultdict[str, list[str]] = defaultdict(list)
    for evidence_id, text in normalized.items():
        if text:
            exact[hashlib.sha256(text.encode("utf-8")).hexdigest()].append(evidence_id)
    for ids in exact.values():
        for other in ids[1:]:
            union(ids[0], other, "exact_normalized_text")

    # A repeated fixed tail across three or more rows is treated as a template
    # collision, not as independent public testimony.
    tails: defaultdict[str, list[str]] = defaultdict(list)
    for evidence_id, text in normalized.items():
        if len(text) >= 24:
            tails[text[-20:]].append(evidence_id)
    for ids in tails.values():
        if len(ids) >= 3:
            for other in ids[1:]:
                union(ids[0], other, "repeated_template_tail")

    # Near-comparison is constrained by length and a shared prefix or suffix so
    # that it remains practical for large research batches.
    ids = [item for item, text in normalized.items() if len(text) >= 16]
    for index, left in enumerate(ids):
        left_text = normalized[left]
        for right in ids[index + 1:]:
            right_text = normalized[right]
            if left_text == right_text:
                continue
            length_ratio = min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
            if length_ratio < 0.85:
                continue
            if left_text[:8] != right_text[:8] and left_text[-10:] != right_text[-10:]:
                continue
            if SequenceMatcher(None, left_text, right_text, autojunk=False).ratio() >= 0.92:
                union(left, right, "high_similarity_text")

    components: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for row in records:
        components[find(str(row["evidence_id"]))].append(row)
    result: dict[str, dict[str, str]] = {}
    for root, items in components.items():
        component_ids = sorted(str(item["evidence_id"]) for item in items)
        component_methods = sorted({method for item in component_ids for method in methods[item]})
        method = "+".join(component_methods) or "none"
        group_id = "COL-" + canonical_sha256(
            {"ids": component_ids, "method": method}
        )[:20]
        if len(items) == 1 or not normalized.get(component_ids[0], ""):
            result[component_ids[0]] = {
                "status": "unique",
                "group_id": group_id,
                "match_method": "none",
            }
            continue
        source_ids = {str(item.get("source_id", "")) for item in items}
        snapshots = {str(item.get("source_snapshot_sha256", "")) for item in items}
        page_entity_ids = {
            page_entity_id_for_source(
                sources[source_id], trusted_independent_occurrence=True
            )
            for source_id in source_ids
            if source_id in sources
            and page_entity_id_for_source(
                sources[source_id], trusted_independent_occurrence=True
            )
        }
        trusted = reviews.get(group_id)
        trusted_conclusion = ""
        trusted_review_id = ""
        trusted_basis = ""
        trusted_sha = ""
        if trusted is not None:
            from collision_review import verify_collision_identity
            try:
                verify_collision_identity(trusted)
                if any(item.get('task_run_id') and item['task_run_id'] != trusted.get('task_run_id') for item in items):
                    raise ValueError('collision_review_task_mismatch')
            except (ValueError, TypeError, KeyError) as exc:
                raise AdmissionError('collision_review_identity_invalid', str(exc)) from exc
            from temporal_fields import review_time
            try:
                review_time(trusted.get("decision_time", ""), not_before=[
                    sources[source_id][field] for source_id in source_ids if source_id in sources
                    for field in ("retrieved_at", "captured_at") if sources[source_id].get(field)
                ])
            except ValueError as exc:
                raise AdmissionError("collision_review_time_invalid", group_id) from exc
            expected_evidence = sorted(component_ids)
            expected_sources = sorted(item for item in source_ids if item)
            declared_pages = sorted(str(item) for item in trusted.get("page_entity_ids", []))
            if sorted(str(item) for item in trusted.get("evidence_ids", [])) != expected_evidence:
                raise AdmissionError("collision_review_scope_mismatch", group_id)
            if sorted(str(item) for item in trusted.get("source_ids", [])) != expected_sources:
                raise AdmissionError("collision_review_scope_mismatch", group_id)
            if declared_pages != sorted(page_entity_ids):
                raise AdmissionError("collision_review_page_scope_mismatch", group_id)
            trusted_conclusion = str(trusted.get("conclusion", ""))
            if trusted_conclusion == "verified_independent_occurrence" and len(page_entity_ids) < 2:
                raise AdmissionError("collision_review_not_independent_pages", group_id)
            trusted_review_id = str(trusted.get("review_record_id", ""))
            trusted_basis = str(trusted.get("review_basis", ""))
            trusted_sha = str(trusted.get("decision_payload_sha256", ""))
        for item in items:
            evidence_id = str(item["evidence_id"])
            if len(source_ids) == 1:
                status = "duplicated_extraction"
            elif trusted_conclusion:
                status = trusted_conclusion
            elif len(snapshots) == 1:
                status = "mirror"
            else:
                status = "unresolved_collision"
            result[evidence_id] = {
                "status": status,
                "group_id": group_id,
                "match_method": method,
                "trusted_review_id": trusted_review_id,
                "trusted_review_basis": trusted_basis,
                "trusted_review_sha256": trusted_sha,
                "collision_review_proof": trusted.get('review_trust_proof', '') if trusted else '',
            }
    return result


def build_admission_batch(
    *,
    task_run_id: str,
    capture_manifest_path: Path,
    candidate_sources_path: Path,
    candidate_evidence_path: Path,
    existing_sources: Sequence[Mapping[str, object]] = (),
    existing_evidence: Sequence[Mapping[str, object]] = (),
    collision_review_path: Path | None = None,
    trusted_review_key_path: Path | None = None,
) -> dict[str, object]:
    captures = _capture_events(capture_manifest_path, task_run_id)
    candidate_sources = _read_jsonl(candidate_sources_path)
    candidate_evidence = _read_jsonl(candidate_evidence_path)
    if not candidate_sources:
        raise AdmissionError("empty_promotion_batch", "candidate source batch must be non-empty")
    sources_with_units = {str(unit.get("candidate_source_id", "")) for unit in candidate_evidence}
    audit_id = "PAA-" + canonical_sha256(
        {
            "capture_manifest": sha256_file(capture_manifest_path),
            "candidate_sources": sha256_file(candidate_sources_path),
            "candidate_evidence": sha256_file(candidate_evidence_path),
        }
    )[:20]
    batch_id = "PB-" + canonical_sha256({"task_run_id": task_run_id, "audit_id": audit_id})[:20]
    now = utc_now()
    existing_source_ids = {str(row.get("source_id", "")) for row in existing_sources}
    existing_evidence_ids = {str(row.get("evidence_id", "")) for row in existing_evidence}
    promoted_source_ids: set[str] = set()
    promoted_evidence_ids: set[str] = set()

    source_by_candidate: dict[str, dict[str, object]] = {}
    promoted_sources: list[dict[str, object]] = []
    source_order = sorted(SOURCE_FIELDS) + [field for field in CANONICAL_SOURCE_EXTRA_FIELDS if field not in SOURCE_FIELDS]
    evidence_order = sorted(EVIDENCE_FIELDS) + [field for field in CANONICAL_EVIDENCE_EXTRA_FIELDS if field not in EVIDENCE_FIELDS]
    for number, raw in enumerate(candidate_sources, start=1):
        temporal_errors = date_errors(raw, label=f'candidate source row {number}')
        if temporal_errors:
            raise AdmissionError('invalid_publication_date', ';'.join(temporal_errors))
        if raw.get("schema_version") not in {None, "", CANDIDATE_SOURCE_SCHEMA_VERSION}:
            raise AdmissionError("candidate_source_schema_mismatch", f"candidate source {number}")
        if str(raw.get("task_run_id", "")) != task_run_id:
            raise AdmissionError("candidate_task_mismatch", f"candidate source {number}")
        candidate_id = str(raw.get("candidate_source_id", "")).strip()
        capture_id = str(raw.get("capture_id", "")).strip()
        if not candidate_id or candidate_id in source_by_candidate:
            raise AdmissionError("duplicate_candidate_source", f"candidate source {number}")
        capture = captures.get(capture_id)
        if capture is None:
            raise AdmissionError("capture_not_found", f"candidate source {candidate_id}")
        attestation_errors = proof_errors(capture, label="capture")
        if attestation_errors:
            raise AdmissionError("unattested_source_not_formal", ";".join(attestation_errors))
        temporal_errors = date_errors({**raw, 'retrieved_at': capture.get('retrieved_at'),
            'research_cutoff': capture.get('research_cutoff')}, label=f'candidate source {candidate_id}', capture=True)
        if temporal_errors:
            raise AdmissionError('invalid_publication_date', ';'.join(temporal_errors))
        native_observation = capture.get("native_numeric_observation")
        has_native_observation = isinstance(native_observation, dict) and bool(native_observation)
        if capture.get("access_status") not in TEXT_ACCESS_STATUSES and not has_native_observation:
            raise AdmissionError("capture_not_readable", f"candidate source {candidate_id}")
        if not str(capture.get("page_title", "")).strip():
            raise AdmissionError("missing_page_title", f"candidate source {candidate_id}")
        entity_level = str(raw.get("entity_level", "")).strip()
        if entity_level not in ENTITY_LEVELS:
            raise AdmissionError("invalid_entity_level", f"candidate source {candidate_id}")
        source_id = _stable_id("S", {"task_run_id": task_run_id, "capture_id": capture_id})
        if source_id in existing_source_ids or source_id in promoted_source_ids:
            raise AdmissionError("canonical_source_already_exists", source_id)
        promoted_source_ids.add(source_id)
        url = str(capture.get("url", ""))
        canonical = str(capture.get("canonical_url", ""))
        identity = platform_identity(canonical)
        platform = identity["platform_id"]
        declared_platform = normalize_platform_id(raw.get("platform", ""))
        if declared_platform and declared_platform != platform:
            raise AdmissionError("candidate_platform_conflict", f"candidate source {candidate_id}")
        captured_category = str(capture.get("source_type", "")).strip()
        declared_category = str(raw.get("source_category", "")).strip()
        captured_content_layer = str(capture.get("content_layer") or "page_body").strip()
        declared_content_layer = str(raw.get("content_layer") or "").strip()
        if captured_category not in SOURCE_CATEGORIES:
            raise AdmissionError("invalid_captured_source_category", f"candidate source {candidate_id}")
        if declared_category and declared_category not in SOURCE_CATEGORIES:
            raise AdmissionError("invalid_candidate_source_category", f"candidate source {candidate_id}")
        # Capture is the authoritative observation layer.  Candidate metadata
        # may describe the record, but cannot silently replace source_type.
        category = captured_category
        category_note = ""
        if declared_category and declared_category != captured_category:
            category_note = (
                "candidate_source_category_restored_to_capture:"
                f"{declared_category}->{captured_category}"
            )
        if captured_content_layer not in CONTENT_LAYERS:
            raise AdmissionError("invalid_content_layer", f"candidate source {candidate_id}")
        if has_native_observation and declared_content_layer and declared_content_layer != "rating_only":
            raise AdmissionError(
                "native_numeric_content_layer_conflict",
                f"candidate source {candidate_id}",
            )
        if declared_content_layer and declared_content_layer != captured_content_layer:
            raise AdmissionError(
                "candidate_content_layer_conflict",
                f"candidate source {candidate_id}",
            )
        content_layer = captured_content_layer
        if has_native_observation and content_layer != "rating_only":
            raise AdmissionError(
                "native_numeric_content_layer_conflict",
                f"candidate source {candidate_id}",
            )
        if not _truth(raw.get("is_relevant", True)):
            raise AdmissionError("candidate_source_not_relevant", f"candidate source {candidate_id}")
        verified_user_block = any(
            isinstance(block, Mapping) and block.get("is_user_generated") is True
            and block.get('visibility_proof_level') in {'tool_result', 'static_explicit'}
            for block in capture.get("visible_blocks", [])
        )
        is_user = bool(verified_user_block or (
            has_native_observation and category in USER_CATEGORIES
        ))
        if is_user and candidate_id not in sources_with_units:
            raise AdmissionError("candidate_user_source_requires_evidence", candidate_id)
        place_name = str(raw.get("place_name", "")).strip()
        if not place_name:
            raise AdmissionError("missing_place_name", f"candidate source {candidate_id}")
        row: dict[str, object] = {field: "" for field in source_order}
        row.update(
            {
                "task_run_id": task_run_id,
                "source_id": source_id,
                "place_name": place_name,
                "entity_level": entity_level,
                "platform": platform,
                "platform_id": platform,
                "domain": capture["content_domain"],
                "original_hostname": identity["original_hostname"],
                "platform_resolution_basis": identity["resolution_basis"],
                "canonical_url": machine_canonical_url(
                    capture.get("url", ""), capture.get("final_url", "")
                ),
                "page_entity_id": page_entity_id_for_source(
                    {
                        "url": url,
                        "final_url": capture.get("final_url", url),
                        "canonical_url": machine_canonical_url(
                            capture.get("url", ""), capture.get("final_url", "")
                        ),
                        "snapshot_sha256": capture.get("snapshot_sha256", ""),
                        "content_layer": content_layer,
                    },
                    trusted_independent_occurrence=True,
                ),
                "source_category": category,
                "page_title": str(capture.get("page_title", "")),
                "published_at": str(raw.get("published_at", "")).strip(),
                "retrieved_at": str(capture.get("retrieved_at", "")),
                "url": url,
                **{field: json.dumps(capture[field], ensure_ascii=False, sort_keys=True)
                    if isinstance(capture.get(field), dict) else str(capture.get(field, ''))
                    for field in (*NETWORK_FIELDS, *ATTESTATION_FIELDS, *IDENTITY_FIELDS, 'research_cutoff')},
                "normalized_url_sha256": normalized_url_sha256(capture["source_identity_url"]),
                "query_id": str(capture.get("query_id", "")),
                "query_text": str(capture.get("query", "")),
                **{key: str(capture.get(key, "")) for key in __import__("execution_facts").SOURCE_BINDING_FIELDS},
                "result_rank": str(raw.get("result_rank", "")).strip(),
                "access_status": str(capture.get("access_status", "")),
                "content_layer": content_layer,
                "selection_mechanism": str(raw.get("selection_mechanism", "search_result")).strip(),
                "is_relevant": "true" if _truth(raw.get("is_relevant", True)) else "false",
                "is_user_source": "true" if is_user else "false",
                "suspected_promotion": "true" if _truth(raw.get("suspected_promotion")) else "false",
                "promotion_basis": "; ".join(
                    item for item in (
                        redact_text(str(raw.get("promotion_basis", "")).strip()),
                        category_note,
                    ) if item
                ),
                "used_for_scoring": "true" if is_user and not _truth(raw.get("suspected_promotion")) else "false",
                "score_scope": "direct" if entity_level in {"poi", "area_direct"} else "aggregate_context",
                "dedup_group": page_entity_id_for_source(
                    {
                        "url": url,
                        "final_url": capture.get("final_url", url),
                        "canonical_url": canonical,
                        "snapshot_sha256": capture.get("snapshot_sha256", ""),
                        "content_layer": content_layer,
                    },
                    trusted_independent_occurrence=True,
                ),
                "evidence_locator": "capture_snapshot",
                "notes": "; ".join(
                    item for item in (
                        redact_text(str(raw.get("researcher_summary", "")).strip()),
                        category_note,
                        f"platform_resolution_basis={identity['resolution_basis']}",
                        f"original_hostname={identity['original_hostname']}",
                    ) if item
                ),
                "capture_id": capture_id,
                "source_capture_id": capture_id,
                "capture_event_sha256": str(capture.get("event_sha256", "")),
                "capture_snapshot_sha256": str(capture.get("snapshot_sha256", "")),
                "promotion_batch_id": batch_id,
                "promotion_audit_id": audit_id,
                "promotion_status": "promoted",
                "recorded_at": now,
            }
        )
        row["canonical_record_sha256"] = _canonical_digest(row)
        source_by_candidate[candidate_id] = {"row": row, "capture": capture}
        promoted_sources.append(row)

    promoted_evidence: list[dict[str, object]] = []
    locator_records: list[dict[str, object]] = []
    seen_candidate_evidence: set[str] = set()
    for number, raw in enumerate(candidate_evidence, start=1):
        temporal_errors = date_errors(raw, label=f'candidate evidence row {number}')
        if temporal_errors:
            raise AdmissionError('invalid_publication_date', ';'.join(temporal_errors))
        if raw.get("schema_version") not in {None, "", CANDIDATE_EVIDENCE_SCHEMA_VERSION}:
            raise AdmissionError("candidate_evidence_schema_mismatch", f"candidate evidence {number}")
        if str(raw.get("task_run_id", "")) != task_run_id:
            raise AdmissionError("candidate_task_mismatch", f"candidate evidence {number}")
        candidate_id = str(raw.get("candidate_evidence_id", "")).strip()
        source_candidate_id = str(raw.get("candidate_source_id", "")).strip()
        if not candidate_id or candidate_id in seen_candidate_evidence:
            raise AdmissionError("duplicate_candidate_evidence", f"candidate evidence {number}")
        seen_candidate_evidence.add(candidate_id)
        source_bundle = source_by_candidate.get(source_candidate_id)
        if source_bundle is None:
            raise AdmissionError("candidate_source_not_found", candidate_id)
        source = source_bundle["row"]
        capture = source_bundle["capture"]
        assert isinstance(source, dict) and isinstance(capture, dict)
        temporal_errors = date_errors({**raw, 'retrieved_at': capture.get('retrieved_at'),
            'research_cutoff': capture.get('research_cutoff')}, label=f'candidate evidence {candidate_id}')
        if temporal_errors:
            raise AdmissionError('invalid_publication_date', ';'.join(temporal_errors))
        unit_type = str(raw.get("unit_type", "")).strip()
        content_layer = str(raw.get("content_layer") or source.get("content_layer", "")).strip()
        declared_platform = normalize_platform_id(raw.get("platform", ""))
        if declared_platform and declared_platform != source.get("platform"):
            raise AdmissionError("candidate_platform_conflict", candidate_id)
        original_text = redact_text(str(raw.get("original_visible_text", "")))
        locator: dict[str, object] | None = None
        if unit_type in TEXT_UNITS or original_text.strip():
            snapshot_path = resolve_capture_snapshot(
                capture_manifest_path,
                str(capture.get("snapshot_path", "")),
            )
            body = snapshot_path.read_text(encoding="utf-8")
            explicit_start, explicit_end = raw.get("source_text_start_offset"), raw.get("source_text_end_offset")
            if explicit_start is not None or explicit_end is not None:
                if (type(explicit_start) is not int or type(explicit_end) is not int
                        or not 0 <= explicit_start < explicit_end <= len(body)
                        or body[explicit_start:explicit_end] != original_text):
                    raise AdmissionError("invalid_explicit_locator", candidate_id)
                found = (explicit_start, explicit_end)
            else:
                found = _locate_text(body, original_text)
            if found is None:
                raise AdmissionError(
                    "text_not_found_in_source_snapshot",
                    f"candidate evidence {candidate_id}",
                )
            locator = _locator(capture, *found)
        elif unit_type != "source_native_numeric":
            raise AdmissionError("missing_original_visible_text", f"candidate evidence {candidate_id}")
        _validate_unit_layer(unit_type, content_layer, locator)
        dimension = str(raw.get("primary_dimension", "")).strip()
        if dimension not in DIMENSION_NAMES:
            raise AdmissionError("invalid_primary_dimension", candidate_id)
        evidence_id = _stable_id(
            "E",
            {
                "task_run_id": task_run_id,
                "candidate_evidence_id": candidate_id,
                "source_id": source["source_id"],
                "locator": locator.get("locator_sha256", "") if locator else "native_numeric",
            },
        )
        if evidence_id in existing_evidence_ids or evidence_id in promoted_evidence_ids:
            raise AdmissionError("canonical_evidence_already_exists", evidence_id)
        promoted_evidence_ids.add(evidence_id)
        direct = str(source.get("entity_level", "")) in {"poi", "area_direct"}
        user_unit = (unit_type in USER_UNITS or unit_type == "page_body") and bool(
            locator and locator.get("is_user_generated") is True)
        is_valid = direct and (user_unit or unit_type == "source_native_numeric")
        native_observation = capture.get("native_numeric_observation")
        native_values = {field: "" for field in (
            "native_rating_value", "native_rating_scale_min", "native_rating_scale_max"
        )}
        if unit_type == "source_native_numeric":
            if not isinstance(native_observation, dict):
                raise AdmissionError("native_numeric_capture_missing", candidate_id)
            try:
                native_values = {
                    field: str(finite_number(native_observation[field], field=field))
                    for field in native_values
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise AdmissionError("native_numeric_capture_missing", candidate_id) from exc
            for field, captured_value in native_values.items():
                declared = str(raw.get(field, "")).strip()
                if declared and finite_number(raw[field], field=field) != finite_number(captured_value):
                    raise AdmissionError("native_numeric_candidate_conflict", candidate_id)
        row: dict[str, object] = {field: "" for field in evidence_order}
        row.update(
            {
                "task_run_id": task_run_id,
                "evidence_id": evidence_id,
                "source_id": source["source_id"],
                "platform": source["platform"],
                "unit_type": unit_type,
                "published_at": str(raw.get("published_at", "")).strip(),
                "time_context": str(raw.get("time_context", "")).strip(),
                "user_group": str(raw.get("user_group", "")).strip(),
                "entity_level": source["entity_level"],
                "is_direct_place_evidence": "true" if direct else "false",
                "explicit_visit": "true" if _truth(raw.get("explicit_visit")) else "false",
                "place_relevance": str(raw.get("place_relevance", "direct")).strip(),
                "content_length_category": str(raw.get("content_length_category", "")).strip(),
                "is_valid": "true" if is_valid else "false",
                "validity_reason": "source_snapshot_locator_verified" if is_valid else "not_direct_or_not_user_evidence",
                "normalized_theme": str(raw.get("normalized_theme", "")).strip(),
                "primary_dimension": dimension,
                "secondary_dimension": str(raw.get("secondary_dimension", "")).strip(),
                "evidence_type": str(raw.get("evidence_type", "public_network_text")).strip(),
                "dimension_tags": str(raw.get("dimension_tags", "")).strip(),
            "sentiment": "na",
                "stance_strength": "",
                "sentiment_score": "",
                "interaction_count": str(raw.get("interaction_count", "")).strip(),
                "displayed_total_count": str(raw.get("displayed_total_count", "")).strip(),
                "time_complete": "true" if _truth(raw.get("time_complete")) else "false",
                "used_for_scoring": "true" if is_valid and source.get("used_for_scoring") == "true" else "false",
                "score_scope": "direct" if direct else "aggregate_context",
                "dedup_group": str(raw.get("dedup_hint", "")).strip(),
                "original_summary_text": original_text,
                "excerpt_or_summary": original_text,
                **native_values,
                "semantic_unit_text": original_text,
                "formal_scoring_eligible": "false",
                "platform_sample_status": "not_evaluated",
                "included_in_platform_score": "false",
                "notes": redact_text(str(raw.get("researcher_summary", "")).strip()),
                **{key: locator[key] if locator else "" for key in
                   ("block_text_sha256", "visible_body_sha256", "recognition_rule_sha256")},
                "capture_id": capture["capture_id"],
                "source_capture_id": capture["capture_id"],
                "candidate_evidence_id": candidate_id,
                "query_id": source["query_id"],
                "content_layer": content_layer,
                "source_user_status": source["is_user_source"],
                "source_snapshot_sha256": capture["snapshot_sha256"],
                "source_text_start": locator["char_start"] if locator else "",
                "source_text_end": locator["char_end"] if locator else "",
                "source_text_start_offset": locator["char_start"] if locator else "",
                "source_text_end_offset": locator["char_end"] if locator else "",
                "source_block_id": locator["block_id"] if locator else "",
                "source_block_type": locator["block_type"] if locator else "",
                "source_block_is_user_generated": "true"
                if locator and locator.get("is_user_generated")
                else "false",
                "locator_sha256": locator["locator_sha256"] if locator else "",
                "evidence_locator": json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if locator
                else "",
                "evidence_text_sha256": hashlib.sha256(original_text.encode("utf-8")).hexdigest()
                if original_text
                else "",
                "promotion_batch_id": batch_id,
                "promotion_audit_id": audit_id,
                "promotion_status": "promoted",
                "promoted_at": now,
                "recorded_at": now,
                "_requested_collision_status": str(raw.get("collision_status", "")).strip(),
            }
        )
        promoted_evidence.append(row)
        if locator:
            locator_records.append(
                {
                    "task_run_id": task_run_id,
                    "evidence_id": evidence_id,
                    "source_id": source["source_id"],
                    **locator,
                    "status": "verified",
                }
            )

    all_sources, _page_identity_audit = annotate_page_entities([*existing_sources, *promoted_sources])
    source_lookup = {str(row.get("source_id", "")): row for row in all_sources}
    trusted_reviews = load_trusted_collision_reviews(
        collision_review_path,
        trusted_review_key_path,
        task_run_id=task_run_id,
    )
    all_for_collision = [dict(row) for row in existing_evidence] + promoted_evidence
    collision_map = _collision_assignments(
        all_for_collision,
        source_by_id=source_lookup,
        trusted_reviews=trusted_reviews,
    )
    for row in promoted_evidence:
        assignment = collision_map[str(row["evidence_id"])]
        status = assignment["status"]
        if status not in COLLISION_STATUSES:
            raise AdmissionError("invalid_collision_status", str(row["evidence_id"]))
        row["collision_status"] = status
        row["cross_source_text_collision_status"] = status
        row["collision_group_id"] = assignment["group_id"]
        row["collision_match_method"] = assignment["match_method"]
        row['collision_review_proof'] = assignment.get('collision_review_proof', '')
        if status not in {"unique", "verified_independent_occurrence"}:
            row["is_valid"] = "false"
            row["used_for_scoring"] = "false"
            row["formal_scoring_exclusion_reasons"] = status
            row["validity_reason"] = status
        # Canonical hashes cover exactly the released canonical CSV schema.
        # Staging-only helpers and later derived scoring fields never enter the
        # append-only canonical record.
        for key in tuple(row):
            if key not in evidence_order:
                row.pop(key, None)
        row["canonical_record_sha256"] = _canonical_digest(row)

    collision_records = [
        {
            "task_run_id": task_run_id,
            "evidence_id": row["evidence_id"],
            "source_id": row["source_id"],
            "collision_status": row["collision_status"],
            "collision_group_id": row["collision_group_id"],
            "collision_match_method": row["collision_match_method"],
            "trusted_review_id": collision_map[str(row["evidence_id"])].get("trusted_review_id", ""),
            "trusted_review_basis": collision_map[str(row["evidence_id"])].get("trusted_review_basis", ""),
            "trusted_review_sha256": collision_map[str(row["evidence_id"])].get("trusted_review_sha256", ""),
            "collision_review_proof": collision_map[str(row['evidence_id'])].get('collision_review_proof', ''),
            "review_provenance_valid": bool(
                collision_map[str(row["evidence_id"])].get("trusted_review_sha256", "")
            ),
            "text_sha256": hashlib.sha256(
                re.sub(r"\s+", " ", str(row.get("original_summary_text", ""))).strip().casefold().encode("utf-8")
            ).hexdigest() if row.get("original_summary_text") else "",
        }
        for row in promoted_evidence
    ]
    return {
        "audit_id": audit_id,
        "batch_id": batch_id,
        "source_fields": source_order,
        "evidence_fields": evidence_order,
        "sources": promoted_sources,
        "evidence": promoted_evidence,
        "locator_records": locator_records,
        "collision_records": collision_records,
        "collision_assignments": collision_map,
        "capture_manifest_sha256": sha256_file(capture_manifest_path),
        "candidate_sources_sha256": sha256_file(candidate_sources_path),
        "candidate_evidence_sha256": sha256_file(candidate_evidence_path),
    }


def promote_batch(
    *,
    task_run_id: str,
    capture_manifest_path: Path,
    candidate_sources_path: Path,
    candidate_evidence_path: Path,
    canonical_sources_path: Path,
    canonical_evidence_path: Path,
    correction_ledger_path: Path,
    active_sources_path: Path,
    active_evidence_path: Path,
    locator_audit_path: Path,
    collision_audit_path: Path,
    admission_audit_path: Path,
    collision_review_path: Path | None = None,
    trusted_review_key_path: Path | None = None,
) -> dict[str, object]:
    commit_outputs = {
        "canonical_source_ledger": canonical_sources_path,
        "canonical_evidence_ledger": canonical_evidence_path,
        "correction_event_ledger": correction_ledger_path,
        "source_ledger": active_sources_path,
        "raw_evidence": active_evidence_path,
        "locator_audit": locator_audit_path,
        "collision_audit": collision_audit_path,
    }
    if admission_audit_path.exists():
        verify_admission_commit(
            admission_audit_path,
            task_run_id=task_run_id,
            output_paths=commit_outputs,
        )
    source_fields, existing_sources = _read_csv(canonical_sources_path)
    evidence_fields, existing_evidence = _read_csv(canonical_evidence_path)
    _, existing_active_sources = _read_csv(active_sources_path)
    _, existing_active_evidence = _read_csv(active_evidence_path)
    existing_corrections = (
        _correction_events(correction_ledger_path, task_run_id)
        if correction_ledger_path.exists()
        else []
    )
    if not admission_audit_path.exists() and (
        existing_sources
        or existing_evidence
        or existing_active_sources
        or existing_active_evidence
        or existing_corrections
    ):
        raise AdmissionError(
            "untrusted_canonical_initial_state",
            "formal ledgers cannot pre-exist without a committed pre-admission audit",
        )
    batch = build_admission_batch(
        task_run_id=task_run_id,
        capture_manifest_path=capture_manifest_path,
        candidate_sources_path=candidate_sources_path,
        candidate_evidence_path=candidate_evidence_path,
        existing_sources=existing_sources,
        existing_evidence=existing_evidence,
        collision_review_path=collision_review_path,
        trusted_review_key_path=trusted_review_key_path,
    )
    expected_source_fields = list(batch["source_fields"])
    expected_evidence_fields = list(batch["evidence_fields"])
    if source_fields and source_fields != expected_source_fields:
        raise AdmissionError("canonical_source_schema_drift", "canonical source header changed")
    if evidence_fields and evidence_fields != expected_evidence_fields:
        raise AdmissionError("canonical_evidence_schema_drift", "canonical evidence header changed")

    correction_events = _correction_events(correction_ledger_path, task_run_id)
    if not correction_events:
        genesis: dict[str, object] = {
            "schema_version": CORRECTION_LEDGER_SCHEMA_VERSION,
            "event_id": "CORRECTION-000001",
            "previous_event_sha256": "",
            "task_run_id": task_run_id,
            "correction_type": "ledger_initialized",
            "record_type": "",
            "target_record_id": "",
            "replacement_record_id": "",
            "reason": "append_only_correction_ledger_initialized",
            "recorded_at": utc_now(),
        }
        genesis["event_sha256"] = canonical_sha256(genesis)
        correction_events = [genesis]

    combined_sources = [*existing_sources, *batch["sources"]]
    combined_evidence = [*existing_evidence, *batch["evidence"]]
    active_sources, active_evidence = _materialize_active(combined_sources, combined_evidence, correction_events)
    active_source_lookup = {
        str(row.get("source_id", "")): row
        for row in annotate_page_entities(
            active_sources,
            trusted_independent_source_ids=trusted_independent_source_ids(
                active_evidence
            ),
        )[0]
    }
    active_assignments = _collision_assignments(
        active_evidence,
        source_by_id=active_source_lookup,
        trusted_reviews=load_trusted_collision_reviews(
            collision_review_path,
            trusted_review_key_path,
            task_run_id=task_run_id,
        ),
    )
    active_evidence = _apply_collision_view(
        active_evidence,
        assignments=active_assignments,
        source_by_id=active_source_lookup,
    )
    cumulative_locator_records = [
        {
            "task_run_id": task_run_id,
            "evidence_id": row.get("evidence_id", ""),
            "source_id": row.get("source_id", ""),
            "capture_id": row.get("capture_id", ""),
            "snapshot_sha256": row.get("source_snapshot_sha256", ""),
            "char_start": int(row["source_text_start"]),
            "char_end": int(row["source_text_end"]),
            "block_id": row.get("source_block_id", ""),
            "block_type": row.get("source_block_type", ""),
            "is_user_generated": _truth(row.get("source_block_is_user_generated", "")),
            "locator_sha256": row.get("locator_sha256", ""),
            **{key: row.get(key, "") for key in ("block_text_sha256", "visible_body_sha256", "recognition_rule_sha256")},
            "status": "verified",
        }
        for row in active_evidence
        if row.get("locator_sha256", "")
    ]
    cumulative_collision_records = [
        {
            "task_run_id": task_run_id,
            "evidence_id": row.get("evidence_id", ""),
            "source_id": row.get("source_id", ""),
            "collision_status": row.get("collision_status", ""),
            "collision_group_id": row.get("collision_group_id", ""),
            "collision_match_method": row.get("collision_match_method", ""),
            "trusted_review_id": active_assignments.get(
                str(row.get("evidence_id", "")), {}
            ).get("trusted_review_id", ""),
            "trusted_review_basis": active_assignments.get(
                str(row.get("evidence_id", "")), {}
            ).get("trusted_review_basis", ""),
            "trusted_review_sha256": active_assignments.get(
                str(row.get("evidence_id", "")), {}
            ).get("trusted_review_sha256", ""),
            "collision_review_proof": active_assignments.get(str(row.get('evidence_id', '')), {}).get('collision_review_proof', ''),
            "review_provenance_valid": bool(
                active_assignments.get(
                    str(row.get("evidence_id", "")), {}
                ).get("trusted_review_sha256", "")
            ),
            "text_sha256": hashlib.sha256(
                re.sub(r"\s+", " ", str(row.get("original_summary_text", ""))).strip().casefold().encode("utf-8")
            ).hexdigest() if row.get("original_summary_text") else "",
        }
        for row in active_evidence
    ]
    locator_payload = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "task_run_id": task_run_id,
        "audit_id": batch["audit_id"],
        "status": "valid",
        "located_text_units": len(cumulative_locator_records),
        "records": cumulative_locator_records,
    }
    collision_payload = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "task_run_id": task_run_id,
        "audit_id": batch["audit_id"],
        "status": "valid",
        "records": cumulative_collision_records,
    }
    prior_batches = _read_admission_history(admission_audit_path, task_run_id)
    archive_dir = admission_audit_path.parent / "promotion_batches" / str(batch["batch_id"])
    immutable_snapshots = {
        "capture_manifest": _archive_content_addressed(
            capture_manifest_path, archive_dir, "capture-manifest"
        ),
        "candidate_sources": _archive_content_addressed(
            candidate_sources_path, archive_dir, "candidate-sources"
        ),
        "candidate_evidence": _archive_content_addressed(
            candidate_evidence_path, archive_dir, "candidate-evidence"
        ),
    }
    if collision_review_path is not None:
        immutable_snapshots["collision_reviews"] = _archive_content_addressed(
            collision_review_path, archive_dir, "collision-reviews"
        )
    batch_event: dict[str, object] = {
        "previous_batch_commit_sha256": str(prior_batches[-1].get("batch_commit_sha256", ""))
        if prior_batches
        else "",
        "audit_id": batch["audit_id"],
        "promotion_batch_id": batch["batch_id"],
        "candidate_source_count": len(batch["sources"]),
        "candidate_evidence_count": len(batch["evidence"]),
        "promoted_source_ids": [row["source_id"] for row in batch["sources"]],
        "promoted_evidence_ids": [row["evidence_id"] for row in batch["evidence"]],
        "capture_manifest_sha256": batch["capture_manifest_sha256"],
        "candidate_sources_sha256": batch["candidate_sources_sha256"],
        "candidate_evidence_sha256": batch["candidate_evidence_sha256"],
        "immutable_input_snapshots": {
            role: {
                "path": Path(os.path.relpath(path, admission_audit_path.parent)).as_posix(),
                "sha256": sha256_file(path),
            }
            for role, path in immutable_snapshots.items()
        },
        "created_at": utc_now(),
    }
    batch_event["batch_commit_sha256"] = canonical_sha256(batch_event)
    all_batches = [*prior_batches, batch_event]
    audit_payload: dict[str, object] = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "task_run_id": task_run_id,
        "audit_id": batch["audit_id"],
        "promotion_batch_id": batch["batch_id"],
        "status": "valid",
        "atomic_promotion": True,
        "candidate_source_count": sum(int(item["candidate_source_count"]) for item in all_batches),
        "candidate_evidence_count": sum(int(item["candidate_evidence_count"]) for item in all_batches),
        "promoted_source_count": len(combined_sources),
        "promoted_evidence_count": len(combined_evidence),
        "capture_manifest_sha256": batch["capture_manifest_sha256"],
        "candidate_sources_sha256": batch["candidate_sources_sha256"],
        "candidate_evidence_sha256": batch["candidate_evidence_sha256"],
        "created_at": utc_now(),
        "batches": all_batches,
    }

    # Serialize the entire formal promotion before changing any formal path.
    formal_payloads: dict[Path, bytes] = {
        canonical_sources_path: _csv_bytes(expected_source_fields, combined_sources),
        canonical_evidence_path: _csv_bytes(expected_evidence_fields, combined_evidence),
        correction_ledger_path: _correction_text(correction_events).encode("utf-8"),
        active_sources_path: _csv_bytes(expected_source_fields, active_sources),
        active_evidence_path: _csv_bytes(expected_evidence_fields, active_evidence),
        locator_audit_path: _json_bytes(locator_payload),
        collision_audit_path: _json_bytes(collision_payload),
    }
    audit_payload["output_sha256"] = {
        role: _sha256_bytes(formal_payloads[path])
        for role, path in commit_outputs.items()
    }
    audit_payload["promotion_commit_sha256"] = canonical_sha256(audit_payload)
    formal_payloads[admission_audit_path] = _json_bytes(audit_payload)
    _transactional_replace(formal_payloads)
    return audit_payload


def append_correction(
    *,
    task_run_id: str,
    record_type: str,
    correction_type: str,
    target_record_id: str,
    reason: str,
    correction_ledger_path: Path,
    canonical_sources_path: Path,
    canonical_evidence_path: Path,
    active_sources_path: Path,
    active_evidence_path: Path,
    locator_audit_path: Path,
    collision_audit_path: Path,
    admission_audit_path: Path,
    replacement_record_id: str = "",
) -> dict[str, object]:
    if record_type not in {"source", "evidence"} or correction_type not in CORRECTION_TYPES:
        raise AdmissionError("invalid_correction_type", "unsupported correction event")
    if not reason.strip():
        raise AdmissionError("missing_correction_reason", "correction reason is required")
    source_fields, sources = _read_csv(canonical_sources_path)
    evidence_fields, evidence = _read_csv(canonical_evidence_path)
    verify_admission_commit(
        admission_audit_path,
        task_run_id=task_run_id,
        output_paths={
            "canonical_source_ledger": canonical_sources_path,
            "canonical_evidence_ledger": canonical_evidence_path,
            "correction_event_ledger": correction_ledger_path,
            "source_ledger": active_sources_path,
            "raw_evidence": active_evidence_path,
            "locator_audit": locator_audit_path,
            "collision_audit": collision_audit_path,
        },
    )
    available = {
        str(row.get("source_id", "")) for row in sources
    } if record_type == "source" else {
        str(row.get("evidence_id", "")) for row in evidence
    }
    if target_record_id not in available:
        raise AdmissionError("correction_target_not_found", target_record_id)
    if correction_type == "supersede" and replacement_record_id not in available:
        raise AdmissionError("replacement_record_not_found", replacement_record_id)
    events = _correction_events(correction_ledger_path, task_run_id)
    if any(
        event.get("target_record_id") == target_record_id
        and event.get("record_type") == record_type
        and event.get("correction_type") in CORRECTION_TYPES
        for event in events
    ):
        raise AdmissionError("record_already_corrected", target_record_id)
    event: dict[str, object] = {
        "schema_version": CORRECTION_LEDGER_SCHEMA_VERSION,
        "event_id": f"CORRECTION-{len(events) + 1:06d}",
        "previous_event_sha256": str(events[-1].get("event_sha256", "")) if events else "",
        "task_run_id": task_run_id,
        "correction_type": correction_type,
        "record_type": record_type,
        "target_record_id": target_record_id,
        "replacement_record_id": replacement_record_id,
        "reason": reason.strip(),
        "recorded_at": utc_now(),
    }
    event["event_sha256"] = canonical_sha256(event)
    events.append(event)
    active_sources, active_evidence = _materialize_active(sources, evidence, events)
    prior_collision_assignments = _collision_assignments_from_audit(collision_audit_path)
    active_evidence = _apply_collision_view(
        active_evidence,
        assignments={
            evidence_id: assignment
            for evidence_id, assignment in prior_collision_assignments.items()
            if any(str(row.get("evidence_id", "")) == evidence_id for row in active_evidence)
        },
        source_by_id={
            str(row.get("source_id", "")): row
            for row in annotate_page_entities(active_sources)[0]
        },
    )
    locator_payload = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "task_run_id": task_run_id,
        "audit_id": "CORRECTION-" + str(event["event_id"]),
        "status": "valid",
        "located_text_units": sum(bool(row.get("locator_sha256")) for row in active_evidence),
        "records": [
            {
                "task_run_id": task_run_id,
                "evidence_id": row.get("evidence_id", ""),
                "source_id": row.get("source_id", ""),
                "capture_id": row.get("capture_id", ""),
                "snapshot_sha256": row.get("source_snapshot_sha256", ""),
                "char_start": int(row["source_text_start"]),
                "char_end": int(row["source_text_end"]),
                "block_id": row.get("source_block_id", ""),
                "block_type": row.get("source_block_type", ""),
                "is_user_generated": _truth(row.get("source_block_is_user_generated", "")),
                "locator_sha256": row.get("locator_sha256", ""),
                **{key: row.get(key, "") for key in ("block_text_sha256", "visible_body_sha256", "recognition_rule_sha256")},
                "status": "verified",
            }
            for row in active_evidence
            if row.get("locator_sha256", "")
        ],
    }
    collision_payload = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "task_run_id": task_run_id,
        "audit_id": "CORRECTION-" + str(event["event_id"]),
        "status": "valid",
        "records": [
            {
                "task_run_id": task_run_id,
                "evidence_id": row.get("evidence_id", ""),
                "source_id": row.get("source_id", ""),
                "collision_status": row.get("collision_status", ""),
                "collision_group_id": row.get("collision_group_id", ""),
                "collision_match_method": row.get("collision_match_method", ""),
                "trusted_review_id": prior_collision_assignments.get(
                    str(row.get("evidence_id", "")), {}
                ).get("trusted_review_id", ""),
                "trusted_review_basis": prior_collision_assignments.get(
                    str(row.get("evidence_id", "")), {}
                ).get("trusted_review_basis", ""),
                "trusted_review_sha256": prior_collision_assignments.get(
                    str(row.get("evidence_id", "")), {}
                ).get("trusted_review_sha256", ""),
                "collision_review_proof": prior_collision_assignments.get(str(row.get('evidence_id', '')), {}).get('collision_review_proof', ''),
                "review_provenance_valid": bool(
                    prior_collision_assignments.get(
                        str(row.get("evidence_id", "")), {}
                    ).get("trusted_review_sha256", "")
                ),
                "text_sha256": hashlib.sha256(
                    re.sub(r"\s+", " ", str(row.get("original_summary_text", "")))
                    .strip().casefold().encode("utf-8")
                ).hexdigest() if row.get("original_summary_text") else "",
            }
            for row in active_evidence
        ],
    }
    admission = json.loads(admission_audit_path.read_text(encoding="utf-8-sig"))
    correction_commits = admission.get("correction_commits", [])
    if not isinstance(correction_commits, list):
        raise AdmissionError("admission_commit_invalid", "correction commits must be an array")
    output_paths = {
        "canonical_source_ledger": canonical_sources_path,
        "canonical_evidence_ledger": canonical_evidence_path,
        "correction_event_ledger": correction_ledger_path,
        "source_ledger": active_sources_path,
        "raw_evidence": active_evidence_path,
        "locator_audit": locator_audit_path,
        "collision_audit": collision_audit_path,
    }
    updated_payloads: dict[Path, bytes] = {
        correction_ledger_path: _correction_text(events).encode("utf-8"),
        active_sources_path: _csv_bytes(source_fields, active_sources),
        active_evidence_path: _csv_bytes(evidence_fields, active_evidence),
        locator_audit_path: _json_bytes(locator_payload),
        collision_audit_path: _json_bytes(collision_payload),
    }

    def committed_output_sha256(role: str, path: Path) -> str:
        content = updated_payloads.get(path)
        return _sha256_bytes(content) if content is not None else sha256_file(path)

    correction_commit: dict[str, object] = {
        "previous_correction_commit_sha256": str(
            correction_commits[-1].get("correction_commit_sha256", "")
        ) if correction_commits else "",
        "event_id": event["event_id"],
        "event_sha256": event["event_sha256"],
        "output_sha256": {
            role: committed_output_sha256(role, path)
            for role, path in output_paths.items()
        },
        "created_at": utc_now(),
    }
    correction_commit["correction_commit_sha256"] = canonical_sha256(correction_commit)
    admission["correction_commits"] = [*correction_commits, correction_commit]
    admission["output_sha256"] = correction_commit["output_sha256"]
    admission["promotion_commit_sha256"] = canonical_sha256(
        {key: value for key, value in admission.items() if key != "promotion_commit_sha256"}
    )
    updated_payloads[admission_audit_path] = _json_bytes(admission)
    _transactional_replace(updated_payloads)
    return event


def _register_outputs(args: argparse.Namespace, inputs: Mapping[str, Path]) -> None:
    outputs = {
        "canonical_source_ledger": args.canonical_sources,
        "canonical_evidence_ledger": args.canonical_evidence,
        "correction_event_ledger": args.corrections,
        "source_ledger": args.active_sources,
        "raw_evidence": args.active_evidence,
        "locator_audit": args.locator_audit,
        "collision_audit": args.collision_audit,
        "pre_admission_audit": args.admission_audit,
    }
    for role, path in outputs.items():
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="pre_admission_audit.py",
            output_role=role,
            output_path=path,
            input_paths=inputs,
            expected_phase="EVIDENCE_BUILD",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    parser.add_argument("--capture-manifest", type=Path, required=True)
    parser.add_argument("--candidate-sources", type=Path, required=True)
    parser.add_argument("--candidate-evidence", type=Path, required=True)
    parser.add_argument("--canonical-sources", type=Path, required=True)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--active-sources", type=Path, required=True)
    parser.add_argument("--active-evidence", type=Path, required=True)
    parser.add_argument("--locator-audit", type=Path, required=True)
    parser.add_argument("--collision-audit", type=Path, required=True)
    parser.add_argument("--admission-audit", type=Path, required=True)
    parser.add_argument("--collision-review", type=Path)
    parser.add_argument("--trusted-review-key", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        state = authorize_runtime_write(
            state_path=args.state,
            writer_script_id="pre_admission_audit.py",
            output_role="pre_admission_audit",
            expected_phase="EVIDENCE_BUILD",
            output_path=args.admission_audit,
        )["state"]
        task_run_id = str(state["task_run_id"])
        for role, path in (
            ("canonical_source_ledger", args.canonical_sources),
            ("canonical_evidence_ledger", args.canonical_evidence),
        ):
            if path.exists():
                verify_artifact_writer(
                    state_path=args.state,
                    writer_ledger_path=args.writer_ledger,
                    output_role=role,
                    output_path=path,
                )
        result = promote_batch(
            task_run_id=task_run_id,
            capture_manifest_path=args.capture_manifest,
            candidate_sources_path=args.candidate_sources,
            candidate_evidence_path=args.candidate_evidence,
            canonical_sources_path=args.canonical_sources,
            canonical_evidence_path=args.canonical_evidence,
            correction_ledger_path=args.corrections,
            active_sources_path=args.active_sources,
            active_evidence_path=args.active_evidence,
            locator_audit_path=args.locator_audit,
            collision_audit_path=args.collision_audit,
            admission_audit_path=args.admission_audit,
            collision_review_path=args.collision_review,
            trusted_review_key_path=args.trusted_review_key,
        )
        inputs = {
            "capture_manifest": args.capture_manifest,
            "candidate_sources": args.candidate_sources,
            "candidate_evidence": args.candidate_evidence,
        }
        if args.collision_review is not None:
            inputs["collision_reviews"] = args.collision_review
        _register_outputs(
            args,
            inputs,
        )
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "valid", "result": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
