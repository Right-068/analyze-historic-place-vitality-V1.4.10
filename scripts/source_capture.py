#!/usr/bin/env python3
"""Append-only capture of actually visible network content.

Capture is deliberately earlier than evidence admission.  It preserves what a
retrieval process could actually read and never creates a formal source or a
formal evidence unit.  Re-visiting a URL creates a new capture version; an
existing capture event or snapshot is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import strict_json as json
import os
import re
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from input_safety import safe_urlparse as urlparse

from artifact_provenance import (
    read_writer_ledger,
    recover_incomplete_writer_ledger_tail,
    register_protected_artifact,
)
from audit_evidence import CONTENT_LAYERS, SOURCE_CATEGORIES
from process_lock import ProcessFileLock
from runtime_guard import authorize_runtime_write, canonical_sha256, sha256_file
from source_identity import platform_identity, canonical_url


CAPTURE_SCHEMA_VERSION = "source-capture-1"
CAPTURE_TRANSACTION_SCHEMA_VERSION = "capture-transaction-1"
NATIVE_NUMERIC_FIELDS = (
    "native_rating_value",
    "native_rating_scale_min",
    "native_rating_scale_max",
)
ACCESS_STATUSES = {
    "full",
    "partial",
    "snippet_only",
    "metadata_only",
    "inaccessible",
    "blocked",
    "login_required",
    "captcha",
    "no_results",
    "empty_page",
}
TEXT_ACCESS_STATUSES = {"full", "partial"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # Binary writing preserves the exact bytes used to calculate the
    # content-addressed SHA-256 on every operating system.
    with temporary.open("wb") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_text(path, json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    raw_bytes = path.read_bytes()
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        raise ValueError("capture manifest has an incomplete trailing record")
    rows: list[dict[str, object]] = []
    previous = ""
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"capture manifest line {line_number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("capture manifest entries must be JSON objects")
        event_hash = str(row.get("event_sha256", ""))
        core = {key: value for key, value in row.items() if key != "event_sha256"}
        if event_hash != canonical_sha256(core):
            raise ValueError("capture manifest event hash is invalid")
        if str(row.get("previous_event_sha256", "")) != previous:
            raise ValueError("capture manifest hash chain is broken")
        previous = event_hash
        rows.append(row)
    return rows


def derive_platform(url: str) -> str:
    return platform_identity(url)["platform_id"]


def resolve_capture_snapshot(manifest_path: Path, raw_path: object) -> Path:
    from input_safety import InputError
    from run_paths import safe_run_relative_path
    if not isinstance(raw_path, str) or not raw_path:
        raise InputError("capture_snapshot_path_invalid")
    root = manifest_path.parent.resolve()
    path = Path(raw_path)
    if path.is_absolute():
        # Read-only compatibility for an in-capture legacy path only.
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise InputError("capture_snapshot_path_escape")
        return resolved
    return safe_run_relative_path(raw_path, root)


def recover_incomplete_manifest_tail(path: Path) -> dict[str, object]:
    """Explicitly quarantine and remove only an incomplete final JSONL tail."""

    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return {"status": "unchanged", "removed_bytes": 0}
    boundary = raw.rfind(b"\n")
    good = raw[: boundary + 1] if boundary >= 0 else b""
    tail = raw[boundary + 1 :]
    if not tail:
        return {"status": "unchanged", "removed_bytes": 0}
    quarantine = path.with_name(
        path.name + ".corrupt-tail-" + hashlib.sha256(tail).hexdigest()[:16] + ".bin"
    )
    if quarantine.exists() and quarantine.read_bytes() != tail:
        raise ValueError("capture tail quarantine hash conflict")
    quarantine.write_bytes(tail)
    temporary = path.with_name(path.name + ".recovered.tmp")
    temporary.write_bytes(good)
    os.replace(temporary, path)
    _read_jsonl(path)
    return {
        "status": "recovered",
        "removed_bytes": len(tail),
        "quarantine_file": quarantine.name,
    }


def _normalize_blocks(body: str, blocks: object, visibility_proof_level=None, visibility_unconfirmed_ranges=None) -> list[dict[str, object]]:
    from content_blocks import normalize_blocks
    return normalize_blocks(body, blocks, visibility_proof_level=visibility_proof_level,
        visibility_unconfirmed_ranges=visibility_unconfirmed_ranges)


def _normalize_native_numeric(value: object) -> dict[str, float] | None:
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise ValueError("native_numeric_observation must be an object")
    from input_safety import native_numbers
    return native_numbers(value)


def _capture_visible_source_unlocked(
    *,
    manifest_path: Path,
    snapshots_dir: Path,
    record: Mapping[str, object],
    transaction_journal_path: Path | None = None,
    artifact_phase_callback=None,
) -> dict[str, object]:
    """Append one immutable capture event and return its normalized record."""
    from input_safety import InputError
    from execution_facts import SOURCE_BINDING_FIELDS
    allowed = set(SOURCE_BINDING_FIELDS) | {
        "schema_version", "task_run_id", "query_id", "query", "url", "final_url",
        "declared_canonical_url", "page_title", "visible_body", "capture_notes",
        "native_numeric_observation", "source_type", "content_layer", "access_status",
        "retrieved_at", "published_at", "research_cutoff", "network_observation",
        "platform", "platform_id", "capture_id", "capture_tool", "visible_blocks",
        "host_receipt", "host_receipt_file", "raw_response_file", "tool_trace", "tool_artifact", "transaction_id"}
    if not isinstance(record, Mapping) or set(record) - allowed:
        raise InputError("unknown_capture_control_field")
    if not snapshots_dir.resolve().is_relative_to(manifest_path.parent.resolve()):
        raise InputError("capture_snapshot_path_escape")
    record = dict(record)
    from host_receipts import verify_receipt
    envelope = record.get("host_receipt")
    if record.get("host_receipt_file"):
        if envelope:
            raise InputError("duplicate_host_receipt_inputs")
        envelope = verify_receipt(json.loads(Path(record.pop("host_receipt_file")).read_text(encoding="utf-8")))
    if envelope:
        envelope = verify_receipt(envelope)
        record["host_receipt"] = envelope
        payload = envelope["payload"]
        for key, receipt_key in (("capture_id", "source_capture_id"), ("final_url", "final_url"),
                                 ("retrieved_at", "response_finished_at")):
            if not record.get(key):
                record[key] = payload[receipt_key]
    for field in ("url", "page_title", "visible_body", "query", "capture_notes"):
        if field in record and not isinstance(record[field], str):
            raise InputError("text_must_be_string", field=field)
    task_run_id = str(record.get("task_run_id", "")).strip()
    query_id = str(record.get("query_id", "")).strip()
    query = str(record.get("query", "")).strip()
    raw_url = str(record.get("url", "")).strip()
    final_url = str(record.get('final_url') or raw_url)
    normalized_url = canonical_url(raw_url, final_url)
    access_status = str(record.get("access_status", "")).strip()
    title = str(record.get("page_title", "")).strip()
    body = str(record.get("visible_body", ""))
    native_numeric = _normalize_native_numeric(record.get("native_numeric_observation"))
    source_type = str(record.get("source_type", "")).strip()
    content_layer = str(record.get("content_layer", "page_body")).strip()
    if not task_run_id or not query_id or not query:
        raise ValueError("task_run_id, query_id, and query are required")
    if not normalized_url:
        raise ValueError("capture URL must be a valid HTTP(S) URL")
    if access_status not in ACCESS_STATUSES:
        raise ValueError(f"unsupported access_status: {access_status}")
    if source_type not in SOURCE_CATEGORIES:
        raise ValueError(f"unsupported source_type: {source_type}")
    if content_layer not in CONTENT_LAYERS:
        raise ValueError(f"unsupported content_layer: {content_layer}")
    if access_status in TEXT_ACCESS_STATUSES and not body.strip() and native_numeric is None and not envelope and not record.get("tool_trace"):
        raise ValueError("readable captures must preserve non-empty visible_body")
    if access_status not in TEXT_ACCESS_STATUSES and body.strip():
        raise ValueError("non-readable access status cannot claim visible body text")
    if native_numeric is not None and access_status not in {*TEXT_ACCESS_STATUSES, "metadata_only"}:
        raise ValueError("native numeric observation requires readable or metadata-only access")

    events = _read_jsonl(manifest_path)
    if any(str(item.get("task_run_id", "")) != task_run_id for item in events):
        raise ValueError("capture manifest contains a different task_run_id")
    same_url = [item for item in events if item.get("canonical_url") == normalized_url]
    version = len(same_url) + 1
    retrieved_at = str(record.get("retrieved_at", "")).strip() or utc_now()
    from temporal_fields import timestamp, date_errors
    timestamp(retrieved_at)
    temporal_errors = date_errors({**record, 'retrieved_at': retrieved_at}, label='capture', capture=True)
    if temporal_errors:
        raise ValueError(';'.join(temporal_errors))
    from public_network import normalize_observation
    from source_identity import platform_mapping_sha256, normalize_platform_id, trusted_domain
    network = (normalize_observation(record.get('network_observation'),
        url=raw_url, final_url=final_url, retrieved_at=retrieved_at)
        if record.get("network_observation") else {})
    derived_platform = platform_identity(raw_url, final_url)['platform_id']
    for field in ('platform', 'platform_id'):
        if record.get(field) and normalize_platform_id(record[field]) != derived_platform:
            raise ValueError('capture_platform_identity_conflict:' + field)
    seed = {
        "task_run_id": task_run_id,
        "query_id": query_id,
        "canonical_url": normalized_url,
        "retrieved_at": retrieved_at,
        "version": version,
    }
    capture_id = str(record.get("capture_id", "")).strip() or (
        "CAP-" + canonical_sha256(seed)[:20]
    )
    if any(item.get("capture_id") == capture_id for item in events):
        raise ValueError("capture_id already exists; captures are append-only")

    from host_receipts import bind_capture, network_from_receipt, extraction_rule_sha256, canonical_bytes
    from input_safety import redact_text, redaction_rule_sha256, InputError
    from source_identity import identity_fields, identity_errors
    proof, raw_response, raw_visible, offset_map = bind_capture(
        {**record, "retrieved_at": retrieved_at}, capture_id=capture_id, body=body,
        artifact_root=manifest_path.parent)
    if proof["network_proof_level"] == "host_verified":
        signed_network = normalize_observation(network_from_receipt(proof["host_receipt"]),
            url=raw_url, final_url=final_url, retrieved_at=retrieved_at)
        if network and network != signed_network:
            raise InputError("host_network_observation_mismatch")
        network = signed_network
    elif proof["network_proof_level"] == "tool_traceable":
        tool_network = proof.pop("network_observation")
        if network and network != tool_network:
            raise InputError("tool_network_observation_mismatch")
        network = tool_network
        if not raw_response.strip() and native_numeric is None:
            access_status = "empty_page"
    if access_status in TEXT_ACCESS_STATUSES and not raw_visible.strip() and native_numeric is None:
        raise InputError("readable_host_body_empty")
    if access_status not in TEXT_ACCESS_STATUSES and raw_visible.strip():
        raise InputError("unreadable_capture_cannot_claim_text")
    identity_data = identity_fields(raw_url, final_url, record.get("declared_canonical_url", ""))
    identity_issues = identity_errors({**identity_data, "url": raw_url, "final_url": final_url,
        "normalized_url_sha256": hashlib.sha256(normalized_url.encode()).hexdigest()})
    if identity_issues:
        raise ValueError(";".join(identity_issues))
    # Validate typed block input before creating any response or snapshot file.
    raw_blocks = _normalize_blocks(raw_visible, record.get("visible_blocks"),
        proof['visibility_proof_level'], proof['visibility_unconfirmed_ranges'])
    body = redact_text(raw_visible)
    blocks = _normalize_blocks(body, [{key: b[key] for key in
        ("block_id", "block_type", "start", "end", "is_user_generated")} for b in raw_blocks],
        proof['visibility_proof_level'], proof['visibility_unconfirmed_ranges'])
    title = redact_text(title)
    from storage_contract import raw_directory, write_raw_artifact
    private_dir = raw_directory(manifest_path.parent, raw_response)
    private_dir.mkdir(parents=True, exist_ok=True)
    raw_hash = hashlib.sha256(raw_response).hexdigest()
    raw_path = private_dir / ("RAW-" + raw_hash + ".bin")
    def record_artifact_phase(progress: Mapping[str, object]) -> None:
        if transaction_journal_path is not None:
            persisted = json.loads(transaction_journal_path.read_text(encoding="utf-8-sig"))
            persisted.update({
                "state": "raw_artifact_" + str(progress["stage"]),
                "raw_artifact": {
                    "target_path": Path(os.path.relpath(
                        private_dir / str(progress["target_name"]), manifest_path.parent
                    )).as_posix(),
                    "temporary_path": Path(os.path.relpath(
                        private_dir / str(progress["temporary_name"]), manifest_path.parent
                    )).as_posix(),
                    "expected_sha256": str(progress["expected_sha256"]),
                    "expected_size": int(progress["expected_size"]),
                    "stage": str(progress["stage"]),
                    "transaction_id": str(progress["transaction_id"]),
                },
            })
            _atomic_json(transaction_journal_path, persisted)
        if artifact_phase_callback is not None:
            artifact_phase_callback(dict(progress))

    write_raw_artifact(
        raw_path,
        raw_response,
        transaction_id=str(record.get("transaction_id") or "capture-" + capture_id),
        phase_callback=record_artifact_phase,
    )
    # The private raw artifact is never an Office source or a downloadable result.
    proof.update(raw_response_sha256=raw_hash,
        raw_response_file=Path(os.path.relpath(raw_path, manifest_path.parent)).as_posix(),
        raw_visible_body_sha256=hashlib.sha256(raw_visible.encode()).hexdigest(),
        visible_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        extraction_rule_sha256=extraction_rule_sha256(),
        redaction_rule_sha256=redaction_rule_sha256(),
        body_offset_map=offset_map,
        body_offset_map_sha256=hashlib.sha256(canonical_bytes(offset_map)).hexdigest())
    snapshot_kind = "visible_text" if body else "native_numeric" if native_numeric is not None else "none"
    snapshot_text = body
    if native_numeric is not None and not body:
        snapshot_text = json.dumps(native_numeric, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_bytes = snapshot_text.encode("utf-8")
    body_hash = hashlib.sha256(body_bytes).hexdigest() if snapshot_text else ""
    snapshot_path = ""
    if snapshot_text:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".txt" if snapshot_kind == "visible_text" else ".json"
        snapshot = snapshots_dir / f"SC-{body_hash}{suffix}"
        if snapshot.exists():
            if sha256_file(snapshot) != body_hash:
                raise ValueError("content-addressed snapshot hash conflict")
        else:
            _atomic_text(snapshot, snapshot_text)
        snapshot_path = Path(os.path.relpath(snapshot, manifest_path.parent)).as_posix()

    previous = str(events[-1].get("event_sha256", "")) if events else ""
    identity = platform_identity(normalized_url)
    event_id = f"CAPTURE-{len(events) + 1:06d}"
    transaction_id = str(record.get("transaction_id", "")).strip()
    event: dict[str, object] = {
        **{key: str(record.get(key, "")) for key in __import__("execution_facts").SOURCE_BINDING_FIELDS},
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "event_id": event_id,
        "capture_event_id": event_id,
        "transaction_id": transaction_id,
        "previous_event_sha256": previous,
        "task_run_id": task_run_id,
        "capture_id": capture_id,
        "source_capture_id": capture_id,
        "capture_version": version,
        "source_capture_version": version,
        "query_id": query_id,
        "query": query,
        "url": raw_url,
        **identity_data,
        **proof,
        "original_url": raw_url,
        "final_url": final_url,
        "network_observation": network,
        "network_observation_sha256": canonical_sha256(network),
        "platform_mapping_sha256": platform_mapping_sha256(),
        "canonical_url": normalized_url,
        "normalized_url": normalized_url,
        "url_sha256": hashlib.sha256(normalized_url.encode("utf-8")).hexdigest(),
        "normalized_url_sha256": hashlib.sha256(normalized_url.encode("utf-8")).hexdigest(),
        "domain": identity_data["content_domain"],
        "platform": identity["platform_id"],
        "platform_id": identity["platform_id"],
        "original_hostname": identity["original_hostname"],
        "platform_resolution_basis": identity["resolution_basis"],
        "page_title": title,
        "retrieved_at": retrieved_at,
        "published_at": str(record.get('published_at', '')),
        "research_cutoff": str(record.get('research_cutoff', '')),
        "access_status": access_status,
        "source_type": source_type,
        "content_layer": content_layer,
        "snapshot_path": snapshot_path,
        "snapshot_sha256": body_hash,
        "source_snapshot_sha256": body_hash,
        "snapshot_kind": snapshot_kind,
        "native_numeric_observation": native_numeric or {},
        "visible_body_length": len(body),
        "visible_blocks": blocks,
        "capture_tool": str(record.get("capture_tool", "")).strip(),
        "capture_notes": redact_text(str(record.get("capture_notes", "")).strip()),
        "captured_at": utc_now(),
    }
    event["event_sha256"] = canonical_sha256(event)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def capture_visible_source(
    *,
    manifest_path: Path,
    snapshots_dir: Path,
    record: Mapping[str, object],
) -> dict[str, object]:
    """Append one event under a cross-process critical section."""

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(manifest_path):
        event = _capture_visible_source_unlocked(
            manifest_path=manifest_path,
            snapshots_dir=snapshots_dir,
            record=record,
        )
    # Keep the persisted manifest relocatable while retaining the historical
    # callable API, whose callers expect a directly openable snapshot path.
    returned = dict(event)
    if returned.get("snapshot_path"):
        returned["snapshot_path"] = str(
            resolve_capture_snapshot(manifest_path, returned["snapshot_path"])
        )
    return returned


def _transaction_directory(manifest_path: Path) -> Path:
    return manifest_path.parent / ".capture-transactions"


def _transaction_recovery_marker(manifest_path: Path) -> Path:
    return _transaction_directory(manifest_path) / "RECOVERY_REQUIRED"


def _transaction_lock_path(manifest_path: Path) -> Path:
    return manifest_path.parent / ".capture-commit-transaction"


def _manifest_prefix_sha256(manifest_path: Path, capture_event_id: str) -> str:
    lines = manifest_path.read_bytes().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("capture manifest contains an invalid transaction prefix") from exc
        if row.get("event_id") == capture_event_id:
            return hashlib.sha256(b"".join(lines[: index + 1])).hexdigest()
    raise ValueError(f"capture event is absent from manifest: {capture_event_id}")


def recover_capture_transactions(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    manifest_path: Path,
) -> dict[str, object]:
    """Finish or roll back interrupted capture commits while holding the transaction lock."""

    state = authorize_runtime_write(
        state_path=state_path,
        writer_script_id="source_capture.py",
        output_role="source_capture_manifest",
        expected_phase="SEARCH",
        output_path=manifest_path,
    )["state"]
    task_run_id = str(state["task_run_id"])
    if manifest_path.exists():
        recover_incomplete_manifest_tail(manifest_path)
    if writer_ledger_path.exists():
        recover_incomplete_writer_ledger_tail(writer_ledger_path)
    events = _read_jsonl(manifest_path)
    by_transaction = {
        str(event.get("transaction_id", "")): event
        for event in events
        if str(event.get("transaction_id", ""))
    }
    writer_entries = (
        read_writer_ledger(writer_ledger_path, task_run_id=task_run_id)
        if writer_ledger_path.exists()
        else []
    )
    writer_by_transaction = {
        str(event.get("transaction_id", "")): event
        for event in writer_entries
        if str(event.get("transaction_id", ""))
    }
    recovered = 0
    rolled_back = 0
    transaction_dir = _transaction_directory(manifest_path)
    if not transaction_dir.exists():
        return {"status": "valid", "recovered": 0, "rolled_back": 0}
    for journal_path in sorted(transaction_dir.glob("TX-*.json")):
        journal = json.loads(journal_path.read_text(encoding="utf-8-sig"))
        if not isinstance(journal, dict) or journal.get("schema_version") != CAPTURE_TRANSACTION_SCHEMA_VERSION:
            raise ValueError(f"capture transaction journal is invalid: {journal_path.name}")
        if journal.get("task_run_id") != task_run_id:
            raise ValueError("capture transaction journal mixes task_run_id values")
        # A committed journal is immutable and was already closed by the
        # transaction that created it.  Revalidating every historical prefix
        # before every later capture produces cubic I/O; the formal preflight
        # and final transaction-chain verifier independently recheck the full
        # history.  Recovery only needs to inspect non-terminal journals.
        if journal.get("state") == "committed":
            continue
        transaction_id = str(journal.get("transaction_id", ""))
        event = by_transaction.get(transaction_id)
        writer = writer_by_transaction.get(transaction_id)
        raw_artifact = journal.get("raw_artifact")
        if raw_artifact:
            if not isinstance(raw_artifact, dict):
                raise ValueError("capture transaction raw-artifact journal is invalid")
            from run_paths import safe_run_relative_path
            expected_sha256 = str(raw_artifact.get("expected_sha256", ""))
            expected_size = raw_artifact.get("expected_size")
            if (not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                    or type(expected_size) is not int or expected_size < 0
                    or raw_artifact.get("transaction_id") != transaction_id):
                raise ValueError("capture transaction raw-artifact binding is invalid")
            target = safe_run_relative_path(raw_artifact.get("target_path"), manifest_path.parent)
            temporary = safe_run_relative_path(raw_artifact.get("temporary_path"), manifest_path.parent)
            from storage_contract import raw_path_allowed
            if not raw_path_allowed(target, manifest_path.parent) or temporary.parent != target.parent:
                raise ValueError("capture transaction raw-artifact path escapes private storage")
            if target.name != "RAW-" + expected_sha256 + ".bin":
                raise ValueError("capture transaction raw-artifact target is not content addressed")
            expected_temporary = f".{target.name}.{transaction_id}.tmp"
            if temporary.name != expected_temporary:
                raise ValueError("capture transaction raw-artifact temporary ownership is invalid")
            if target.exists() and (target.stat().st_size != expected_size
                    or sha256_file(target) != expected_sha256):
                raise ValueError("capture transaction raw-artifact final target conflicts with its journal")
            if event is not None and not target.is_file():
                raise ValueError("capture manifest event references an uncommitted private response")
            if temporary.exists():
                # The exact transaction-derived name is proof of ownership.
                # A committed content-addressed target is retained for any
                # capture that shares the same raw body.
                temporary.unlink()
        if event is None:
            if writer is not None:
                raise ValueError("writer ledger references a capture transaction without a manifest event")
            if journal.get("state") not in {"rolled_back", "committed"}:
                journal.update({"state": "rolled_back", "recovered_at": utc_now()})
                _atomic_json(journal_path, journal)
                rolled_back += 1
            continue
        capture_event_id = str(event.get("event_id", ""))
        prefix_hash = _manifest_prefix_sha256(manifest_path, capture_event_id)
        if writer is None:
            writer = register_protected_artifact(
                state_path=state_path,
                writer_ledger_path=writer_ledger_path,
                writer_script_id="source_capture.py",
                output_role="source_capture_manifest",
                output_path=manifest_path,
                expected_phase="SEARCH",
                provenance_context={
                    "transaction_id": transaction_id,
                    "capture_event_id": capture_event_id,
                    "capture_manifest_prefix_sha256": prefix_hash,
                    "transaction_journal_path": journal_path.relative_to(state_path.parent).as_posix(),
                },
            )
            recovered += 1
        if (
            writer.get("capture_event_id") != capture_event_id
            or writer.get("capture_manifest_prefix_sha256") != prefix_hash
            or writer.get("output_sha256") != prefix_hash
        ):
            raise ValueError("capture transaction writer event does not match its manifest prefix")
        if journal.get("state") != "committed":
            journal.update(
                {
                    "state": "committed",
                    "capture_event_id": capture_event_id,
                    "capture_manifest_prefix_sha256": prefix_hash,
                    "writer_event_id": writer.get("event_id", ""),
                    "writer_event_sha256": writer.get("event_sha256", ""),
                    "recovered_at": utc_now(),
                }
            )
            _atomic_json(journal_path, journal)
    marker = _transaction_recovery_marker(manifest_path)
    marker.unlink(missing_ok=True)
    return {"status": "valid", "recovered": recovered, "rolled_back": rolled_back}


def register_tool_response(**kwargs):
    """The authorized page-adapter registration entry; no source is created."""
    from tool_response_artifacts import register_response
    return register_response(**kwargs)


def commit_capture_transaction(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    manifest_path: Path,
    snapshots_dir: Path,
    record: Mapping[str, object],
    input_path: Path | None = None,
    artifact_phase_callback=None,
) -> dict[str, object]:
    """Atomically commit capture manifest and writer-ledger provenance."""

    authorization = authorize_runtime_write(
        state_path=state_path,
        writer_script_id="source_capture.py",
        output_role="source_capture_manifest",
        expected_phase="SEARCH",
        output_path=manifest_path,
    )
    task_run_id = str(authorization["task_run_id"])
    if str(record.get("task_run_id", "")).strip() != task_run_id:
        raise ValueError("capture record task_run_id does not match the formal run")
    task_state = json.loads(state_path.read_text(encoding='utf-8-sig'))
    cutoff = task_state.get('research_cutoff') or str(task_state['created_at'])[:10]
    if record.get('research_cutoff') not in (None, '', cutoff):
        raise ValueError('capture_research_cutoff_differs_from_protected_task')
    record = {**record, 'research_cutoff': cutoff}
    if record.get('tool_trace') is not None and not record.get('host_receipt') and not record.get('host_receipt_file'):
        from tool_response_artifacts import read_registered
        read_registered(artifact_root=manifest_path.parent, record=record,
            state_path=state_path, writer_ledger_path=writer_ledger_path)
    transaction_id = "TX-" + uuid.uuid4().hex
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    transaction_dir = _transaction_directory(manifest_path)
    transaction_dir.mkdir(parents=True, exist_ok=True)
    journal_path = transaction_dir / f"{transaction_id}.json"
    journal: dict[str, object] = {
        "schema_version": CAPTURE_TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "task_run_id": task_run_id,
        "state": "prepared",
        "prepared_at": utc_now(),
        "manifest_path": manifest_path.relative_to(state_path.parent).as_posix(),
    }
    with ProcessFileLock(_transaction_lock_path(manifest_path)) as transaction_lock:
        if transaction_lock.abandoned:
            journal["abandoned_mutex_detected"] = True
        recovery_marker = _transaction_recovery_marker(manifest_path)
        if recovery_marker.exists():
            recover_capture_transactions(
                state_path=state_path,
                writer_ledger_path=writer_ledger_path,
                manifest_path=manifest_path,
            )
        recovery_marker.write_text(transaction_id + "\n", encoding="utf-8")
        _atomic_json(journal_path, journal)
        transaction_record = dict(record)
        transaction_record["transaction_id"] = transaction_id
        event = _capture_visible_source_unlocked(
            manifest_path=manifest_path,
            snapshots_dir=snapshots_dir,
            record=transaction_record,
            transaction_journal_path=journal_path,
            artifact_phase_callback=artifact_phase_callback,
        )
        journal = json.loads(journal_path.read_text(encoding="utf-8-sig"))
        capture_event_id = str(event["event_id"])
        prefix_hash = _manifest_prefix_sha256(manifest_path, capture_event_id)
        journal.update(
            {
                "state": "manifest_committed",
                "capture_event_id": capture_event_id,
                "capture_manifest_prefix_sha256": prefix_hash,
                "manifest_committed_at": utc_now(),
            }
        )
        _atomic_json(journal_path, journal)
        writer_event = register_protected_artifact(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            writer_script_id="source_capture.py",
            output_role="source_capture_manifest",
            output_path=manifest_path,
            input_paths={"capture_input": input_path} if input_path is not None else None,
            expected_phase="SEARCH",
            provenance_context={
                "transaction_id": transaction_id,
                "capture_event_id": capture_event_id,
                "capture_manifest_prefix_sha256": prefix_hash,
                "transaction_journal_path": journal_path.relative_to(state_path.parent).as_posix(),
            },
        )
        if writer_event.get("output_sha256") != prefix_hash:
            raise ValueError("capture transaction registered a stale manifest hash")
        journal.update(
            {
                "state": "committed",
                "writer_event_id": writer_event.get("event_id", ""),
                "writer_event_sha256": writer_event.get("event_sha256", ""),
                "committed_at": utc_now(),
            }
        )
        _atomic_json(journal_path, journal)
        # The recovery marker covers the complete two-ledger transaction.  It
        # must remain present until both the writer-ledger event and the final
        # committed journal are durable; otherwise a process exit between the
        # two files could leave a manifest-only transaction that the next
        # capture would not recover.
        recovery_marker.unlink(missing_ok=True)
    returned = dict(event)
    returned["transaction_id"] = transaction_id
    returned["capture_event_id"] = capture_event_id
    returned["capture_manifest_prefix_sha256"] = prefix_hash
    if returned.get("snapshot_path"):
        returned["snapshot_path"] = str(
            resolve_capture_snapshot(manifest_path, returned["snapshot_path"])
        )
    return returned


def verify_capture_transaction_chain(
    *,
    manifest_path: Path,
    writer_ledger_path: Path,
    task_run_id: str,
    require_all: bool = True,
) -> dict[str, object]:
    """Verify one ordered writer event for every committed capture event."""

    events = _read_jsonl(manifest_path)
    writers = read_writer_ledger(writer_ledger_path, task_run_id=task_run_id)
    capture_writers = [
        row
        for row in writers
        if row.get("output_role") == "source_capture_manifest"
        and row.get("writer_script_id") == "source_capture.py"
        and row.get("transaction_id")
    ]
    by_transaction = {str(row["transaction_id"]): row for row in capture_writers}
    if len(by_transaction) != len(capture_writers):
        raise ValueError("capture writer ledger contains duplicate transaction_id values")
    transaction_ids: set[str] = set()
    writer_numbers: list[int] = []
    verified = 0
    transaction_dir = _transaction_directory(manifest_path)
    for event in events:
        transaction_id = str(event.get("transaction_id", ""))
        if not transaction_id:
            if require_all:
                raise ValueError("capture event lacks an atomic transaction_id")
            continue
        if transaction_id in transaction_ids:
            raise ValueError("capture manifest contains duplicate transaction_id values")
        transaction_ids.add(transaction_id)
        capture_event_id = str(event.get("event_id", ""))
        if event.get("capture_event_id") != capture_event_id:
            raise ValueError("capture_event_id does not match the manifest event")
        writer = by_transaction.get(transaction_id)
        if writer is None:
            raise ValueError("capture transaction has no writer-ledger commit")
        prefix_hash = _manifest_prefix_sha256(manifest_path, capture_event_id)
        if (
            writer.get("capture_event_id") != capture_event_id
            or writer.get("capture_manifest_prefix_sha256") != prefix_hash
            or writer.get("output_sha256") != prefix_hash
        ):
            raise ValueError("capture transaction hash or event linkage is invalid")
        if event.get('network_proof_level') == 'tool_traceable':
            from tool_response_artifacts import read_registered, _paths
            _, binding = read_registered(artifact_root=manifest_path.parent, observation=event['network_observation'])
            _, registration = _paths(manifest_path.parent, binding)
            logical = (Path(writer['output_path']).parent / registration.relative_to(manifest_path.parent)).as_posix()
            matches = [w for w in writers if w.get('output_role') == 'tool_response_artifact'
                and w.get('writer_script_id') == 'source_capture.py' and w.get('output_path') == logical
                and w.get('output_sha256') == sha256_file(registration)]
            if not matches or int(matches[0]['event_id'].split('-')[-1]) >= int(writer['event_id'].split('-')[-1]):
                raise ValueError('tool_response_registration_not_committed_before_capture')
        try:
            writer_numbers.append(int(str(writer.get("event_id", "")).split("-")[-1]))
        except ValueError as exc:
            raise ValueError("capture writer event ID is invalid") from exc
        journal_path = transaction_dir / f"{transaction_id}.json"
        if not journal_path.is_file():
            raise ValueError("capture transaction has no immutable recovery journal")
        journal = json.loads(journal_path.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(journal, dict)
            or journal.get("schema_version") != CAPTURE_TRANSACTION_SCHEMA_VERSION
            or journal.get("state") != "committed"
            or journal.get("task_run_id") != task_run_id
            or journal.get("transaction_id") != transaction_id
            or journal.get("capture_event_id") != capture_event_id
            or journal.get("capture_manifest_prefix_sha256") != prefix_hash
            or journal.get("writer_event_id") != writer.get("event_id")
            or journal.get("writer_event_sha256") != writer.get("event_sha256")
        ):
            raise ValueError("capture transaction journal does not match its committed chain")
        raw_artifact = journal.get("raw_artifact")
        if not isinstance(raw_artifact, dict) or raw_artifact.get("stage") not in {"committed", "reused"}:
            raise ValueError("capture transaction journal lacks a committed private response")
        from run_paths import safe_run_relative_path
        raw_target = safe_run_relative_path(raw_artifact.get("target_path"), manifest_path.parent)
        from storage_contract import raw_path_allowed
        if (not raw_path_allowed(raw_target, manifest_path.parent)
                or raw_artifact.get("expected_sha256") != event.get("raw_response_sha256")
                or raw_artifact.get("target_path") != event.get("raw_response_file")
                or type(raw_artifact.get("expected_size")) is not int
                or not raw_target.is_file()
                or raw_target.stat().st_size != raw_artifact.get("expected_size")
                or sha256_file(raw_target) != raw_artifact.get("expected_sha256")):
            raise ValueError("capture transaction private response binding is invalid")
        verified += 1
    if set(by_transaction) != transaction_ids:
        raise ValueError("writer ledger contains an orphan capture transaction")
    if writer_numbers != sorted(writer_numbers) or len(writer_numbers) != len(set(writer_numbers)):
        raise ValueError("capture writer events are not ordered with capture commits")
    return {
        "status": "valid",
        "transaction_count": verified,
        "capture_count": len(events),
        "latest_manifest_sha256": sha256_file(manifest_path),
    }


def verify_capture_manifest(path: Path, *, task_run_id: str | None = None) -> dict[str, object]:
    events = _read_jsonl(path)
    capture_ids: set[str] = set()
    from host_receipts import reused_call_sources
    if reused_call_sources(events):
        raise ValueError("host_tool_call_reused_for_different_responses")
    previous_event_sha256 = ""
    for sequence, row in enumerate(events, start=1):
        if row.get("schema_version") != CAPTURE_SCHEMA_VERSION:
            raise ValueError("capture manifest schema_version is invalid")
        if row.get("event_id") != f"CAPTURE-{sequence:06d}":
            raise ValueError("capture manifest event sequence is invalid")
        if row.get("previous_event_sha256", "") != previous_event_sha256:
            raise ValueError("capture manifest hash chain is broken")
        declared_event_sha256 = str(row.get("event_sha256", ""))
        event_core = {key: value for key, value in row.items() if key != "event_sha256"}
        if not declared_event_sha256 or canonical_sha256(event_core) != declared_event_sha256:
            raise ValueError("capture manifest event hash is invalid")
        previous_event_sha256 = declared_event_sha256
        if task_run_id is not None and row.get("task_run_id") != task_run_id:
            raise ValueError("capture manifest contains a different task_run_id")
        capture_id = str(row.get("capture_id", ""))
        if not capture_id or capture_id in capture_ids:
            raise ValueError("capture manifest contains a missing or duplicate capture_id")
        capture_ids.add(capture_id)
        raw_url = str(row.get("url", ""))
        normalized_url = canonical_url(raw_url, row.get('final_url', ''))
        if not normalized_url or row.get("canonical_url") != normalized_url:
            raise ValueError(f"capture canonical URL is invalid: {capture_id}")
        if row.get("normalized_url") != normalized_url:
            raise ValueError(f"capture normalized URL is invalid: {capture_id}")
        expected_url_sha256 = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        if row.get("url_sha256") != expected_url_sha256 or row.get("normalized_url_sha256") != expected_url_sha256:
            raise ValueError(f"capture URL hash is invalid: {capture_id}")
        identity = platform_identity(normalized_url)
        expected_platform = identity["platform_id"]
        if row.get("platform") != expected_platform:
            raise ValueError(f"capture platform derivation is invalid: {capture_id}")
        from temporal_fields import date_errors
        from source_identity import domain_errors, platform_errors
        from public_network import network_errors
        field_errors = (date_errors(row, label='capture:' + capture_id, capture=True)
            + domain_errors(row, label='capture:' + capture_id)
            + platform_errors(row, label='capture:' + capture_id)
            + (network_errors(row, label='capture:' + capture_id) if row.get("network_observation") else []))
        if field_errors:
            raise ValueError(';'.join(field_errors))
        if row.get("source_type") not in SOURCE_CATEGORIES:
            raise ValueError(f"capture source_type is invalid: {capture_id}")
        if row.get("content_layer") not in CONTENT_LAYERS:
            raise ValueError(f"capture content_layer is invalid: {capture_id}")
        status = str(row.get("access_status", ""))
        if status not in ACCESS_STATUSES:
            raise ValueError(f"capture access_status is invalid: {capture_id}")
        snapshot_raw = str(row.get("snapshot_path", ""))
        snapshot_hash = str(row.get("snapshot_sha256", ""))
        snapshot_kind = str(row.get("snapshot_kind", "visible_text" if status in TEXT_ACCESS_STATUSES else "none"))
        native_numeric = _normalize_native_numeric(row.get("native_numeric_observation"))
        if status in TEXT_ACCESS_STATUSES or native_numeric is not None:
            snapshot = resolve_capture_snapshot(path, snapshot_raw)
            if not snapshot.is_file() or sha256_file(snapshot) != snapshot_hash:
                raise ValueError(f"capture snapshot is missing or changed: {capture_id}")
            if snapshot.stat().st_size <= 0:
                raise ValueError(f"capture snapshot is empty: {capture_id}")
            if native_numeric is not None and snapshot_kind == "native_numeric":
                expected = json.dumps(native_numeric, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if snapshot.read_text(encoding="utf-8") != expected:
                    raise ValueError(f"native numeric snapshot differs from capture manifest: {capture_id}")
        elif snapshot_raw or snapshot_hash:
            raise ValueError(f"non-readable capture has an unexpected snapshot: {capture_id}")
        verify_capture_body(path, row)
    return {
        "status": "valid",
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_count": len(events),
        "manifest_sha256": sha256_file(path),
        "latest_event_sha256": previous_event_sha256,
    }


def verify_capture_body(manifest_path, row):
    from host_receipts import proof_errors, analyze_body, extraction_rule_sha256, canonical_bytes
    from input_safety import redact_text, redaction_rule_sha256, InputError
    from source_identity import identity_errors
    problems = identity_errors(row, label="capture")
    if row.get("network_proof_level") in {"host_verified", "tool_traceable"}:
        problems += proof_errors(row, label="capture")
    elif row.get("network_proof_level") != "self_reported" or row.get("host_receipt"):
        problems.append("invalid_capture_proof_level")
    if problems:
        raise ValueError(";".join(problems))
    root = manifest_path.parent.resolve()
    raw_path = (root / str(row.get("raw_response_file", ""))).resolve()
    from storage_contract import raw_path_allowed
    if not raw_path_allowed(raw_path, root) or not raw_path.is_file():
        raise InputError("private_raw_response_missing")
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row.get("raw_response_sha256"):
        raise InputError("private_raw_response_changed")
    kind = "text/plain"
    if row.get("host_receipt"):
        envelope = row["host_receipt"]
        if isinstance(envelope, str):
            envelope = json.loads(envelope)
        kind = envelope["payload"]["body_format"]
    elif row.get("network_proof_level") == "tool_traceable":
        observation = row["network_observation"]
        if isinstance(observation, str):
            observation = json.loads(observation)
        kind = observation["body_format"]
        from tool_response_artifacts import read_registered
        registered_raw, _ = read_registered(artifact_root=root, observation=observation)
        if registered_raw != raw:
            raise InputError('tool_artifact_capture_content_mismatch')
    visible, offsets, visibility = analyze_body(raw, kind)
    if kind == "application/json" and row.get("native_numeric_observation"):
        if _normalize_native_numeric(json.loads(visible)) != row["native_numeric_observation"]:
            raise InputError("host_native_numeric_mismatch")
        visible, offsets = "", []
    body = redact_text(visible)
    expected = {
        **visibility,
        "raw_visible_body_sha256": hashlib.sha256(visible.encode()).hexdigest(),
        "visible_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "redaction_rule_sha256": redaction_rule_sha256(),
        "extraction_rule_sha256": extraction_rule_sha256(),
        "body_offset_map": offsets,
        "body_offset_map_sha256": hashlib.sha256(canonical_bytes(offsets)).hexdigest(),
    }
    if any(row.get(k) != v for k, v in expected.items()):
        raise InputError("body_processing_binding_mismatch")
    if body:
        snapshot = resolve_capture_snapshot(manifest_path, row["snapshot_path"])
        if snapshot.read_text(encoding="utf-8") != body:
            raise InputError("visible_body_not_from_host_response")
    blocks = _normalize_blocks(body, row.get("visible_blocks"), row.get('visibility_proof_level'),
        row.get('visibility_unconfirmed_ranges'))
    if blocks != row.get("visible_blocks"):
        raise InputError("content_blocks_not_canonical")
    return {"status": "valid", "network_proof_level": row["network_proof_level"]}


def rebuild_capture_identity(*, manifest_path: Path, expected_manifest_sha256: str,
                             output_dir: Path) -> dict[str, object]:
    """Maintenance-only reconstruction into a separate, non-formal bundle.

    Old events are hash-pinned and never edited. Only signed raw facts are
    replayed; old identities, derived eligibility, audit and seals are not
    accepted. This does not enroll a writer or grant runtime admission.
    """
    import shutil
    from host_receipts import verify_receipt, bind_capture, reused_call_sources
    from input_safety import InputError
    from source_identity import identity_rule_sha256
    from execution_facts import SOURCE_BINDING_FIELDS
    source = manifest_path.resolve()
    target = output_dir.resolve()
    if target == source.parent or target.is_relative_to(source.parent) or source.is_relative_to(target):
        raise InputError('identity_rebuild_requires_separate_directory')
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_manifest_sha256:
        raise InputError('identity_rebuild_original_hash_mismatch')
    rows = _read_jsonl(source)
    if not rows or reused_call_sources(rows):
        raise InputError('identity_rebuild_original_invalid')
    previous, task, seen, records = '', rows[0].get('task_run_id'), set(), []
    for i, row in enumerate(rows, 1):
        core = {k:v for k,v in row.items() if k != 'event_sha256'}
        if (row.get('event_id') != f'CAPTURE-{i:06d}' or row.get('previous_event_sha256', '') != previous
                or canonical_sha256(core) != row.get('event_sha256') or row.get('task_run_id') != task
                or not row.get('capture_id') or row['capture_id'] in seen):
            raise InputError('identity_rebuild_original_chain_invalid', row=i)
        previous = row['event_sha256']; seen.add(row['capture_id'])
        envelope = verify_receipt(row.get('host_receipt'))
        payload = envelope['payload']
        # No guessing from a former canonical URL or replacement characters.
        if row.get('url') != payload['request_url'] or row.get('final_url') != payload['final_url']:
            raise InputError('identity_rebuild_original_url_uncertain', row=i)
        raw_path = (source.parent / str(row.get('raw_response_file', ''))).resolve()
        from storage_contract import raw_path_allowed
        if not raw_path_allowed(raw_path, source.parent) or not raw_path.is_file():
            raise InputError('identity_rebuild_raw_missing', row=i)
        record = {k:row[k] for k in SOURCE_BINDING_FIELDS if k in row}
        record.update({k:row[k] for k in ('task_run_id','query_id','query','capture_id','url','final_url',
            'page_title','retrieved_at','access_status','source_type','content_layer')})
        record.update({k:row[k] for k in ('published_at','research_cutoff','declared_canonical_url',
            'capture_notes','native_numeric_observation') if row.get(k)})
        record.update(host_receipt=envelope, raw_response_file=str(raw_path), visible_body='')
        _proof, raw, _visible, _offsets = bind_capture(record, capture_id=row['capture_id'], body='')
        record['visible_blocks'] = [{k:b[k] for k in ('block_id','block_type','start','end','is_user_generated')}
                                    for b in row.get('visible_blocks', [])]
        records.append((record, raw))
    rule = identity_rule_sha256()
    from host_receipts import extraction_rule_sha256
    from input_safety import redaction_rule_sha256
    binding = {'original_manifest_sha256': expected_manifest_sha256, 'identity_rule_sha256':rule,
               'extraction_rule_sha256':extraction_rule_sha256(), 'redaction_rule_sha256':redaction_rule_sha256()}
    def reconstruction_report(rebuilt, manifest_sha):
        if len(rebuilt) != len(rows):
            raise InputError('identity_rebuild_record_count_changed')
        for old, new in zip(rows, rebuilt):
            for field in ('capture_id','task_run_id','query_id','query','url','final_url','plan_id','execution_id'):
                if old.get(field) != new.get(field):
                    raise InputError('identity_rebuild_fact_changed', field=field)
            if verify_receipt(old['host_receipt']) != verify_receipt(new['host_receipt']):
                raise InputError('identity_rebuild_receipt_changed')
        return {'schema_version':'source-identity-rebuild-1', 'status':'rebuilt_not_admitted',
                'task_run_id':task, 'binding':binding, 'original_manifest':str(source),
                'rebuilt_manifest_sha256':manifest_sha,
                'invalidated_roles':['source_ledger','raw_evidence','semantic_evidence','dimension_audit',
                                     'scores','report_truth','office','delivery_seal'],
                'records':[{'capture_id':old['capture_id'], 'old_identity':old.get('canonical_url',''),
                            'new_identity':new['canonical_url'], 'receipt_unchanged':True}
                           for old,new in zip(rows,rebuilt)]}
    target.parent.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(target):
        report_path = target/'identity-rebuild.json'
        if target.exists():
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if report.get('binding') != binding:
                raise InputError('identity_rebuild_target_conflict')
            if sha256_file(target/'original-manifest.jsonl') != expected_manifest_sha256:
                raise InputError('identity_rebuild_original_snapshot_changed')
            verified = verify_capture_manifest(target/'capture/manifest.jsonl', task_run_id=task)
            expected_report = reconstruction_report(_read_jsonl(target/'capture/manifest.jsonl'), verified['manifest_sha256'])
            if report != expected_report:
                raise InputError('identity_rebuild_report_changed')
            return report
        # Only this owned temporary directory is removed after a failed replay.
        stage = Path(tempfile.mkdtemp(prefix='identity-rebuild-', dir=target.parent))
        try:
            (stage/'original-manifest.jsonl').write_bytes(original)
            rebuilt=[]
            for record, raw in records:
                # Capture re-verifies the signed file before producing current
                # privacy, block, URL and platform views. Original receipt stays identical.
                if hashlib.sha256(raw).hexdigest() != sha256_file(Path(record['raw_response_file'])):
                    raise InputError('identity_rebuild_raw_changed_during_replay')
                rebuilt.append(capture_visible_source(manifest_path=stage/'capture/manifest.jsonl',
                    snapshots_dir=stage/'capture/snapshots', record=record))
            if source.read_bytes() != original:
                raise InputError('identity_rebuild_original_changed_during_replay')
            verified=verify_capture_manifest(stage/'capture/manifest.jsonl', task_run_id=task)
            report=reconstruction_report(rebuilt,verified['manifest_sha256'])
            (stage/'identity-rebuild.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(stage,target)
            return report
        except BaseException:
            if stage.exists() and stage.parent == target.parent and stage.name.startswith('identity-rebuild-'):
                shutil.rmtree(stage)
            raise


TOOL_MACHINE_FIELDS = frozenset({'raw_response_file', 'tool_artifact'})


def validate_initial_tool_input(payload):
    """Initial tool-result submissions do not own registered artifact fields."""
    if not isinstance(payload, dict):
        raise ValueError("capture record must be a JSON object")
    if TOOL_MACHINE_FIELDS.intersection(payload):
        raise ValueError('tool_artifact_input_conflict')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--state", type=Path, required=True)
    capture.add_argument("--writer-ledger", type=Path, required=True)
    capture.add_argument("--manifest", type=Path, required=True)
    capture.add_argument("--snapshots-dir", type=Path, required=True)
    capture.add_argument("--record", type=Path, required=True)
    capture.add_argument("--tool-result", type=Path, help="Actual page-tool JSON response_text; omit machine-owned raw_response_file and tool_artifact from --record")
    register = sub.add_parser("register-tool-response", help="Register an actual page-tool result before capture")
    register.add_argument("--state", type=Path, required=True)
    register.add_argument("--writer-ledger", type=Path, required=True)
    register.add_argument("--artifact-root", type=Path, required=True)
    register.add_argument("--record", type=Path, required=True)
    register.add_argument("--tool-result", type=Path, required=True, help="JSON object with response_text from the actual tool result")
    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--task-run-id")
    recover = sub.add_parser("recover-tail")
    recover.add_argument("--manifest", type=Path, required=True)
    recover_transactions = sub.add_parser("recover-transactions")
    recover_transactions.add_argument("--state", type=Path, required=True)
    recover_transactions.add_argument("--writer-ledger", type=Path, required=True)
    recover_transactions.add_argument("--manifest", type=Path, required=True)
    rebuild = sub.add_parser('rebuild-identity', help='Maintenance only: preserve old capture and create a non-admitted rebuild bundle')
    rebuild.add_argument('--manifest', type=Path, required=True)
    rebuild.add_argument('--expected-manifest-sha256', required=True)
    rebuild.add_argument('--output-dir', type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "register-tool-response":
            from tool_response_artifacts import register_response
            from storage_contract import read_staging_json
            record = read_staging_json(args.record)
            validate_initial_tool_input(record)
            response = read_staging_json(args.tool_result)
            if not isinstance(response, dict) or set(response) != {'response_text'}:
                raise ValueError('tool response envelope requires only response_text')
            result = register_tool_response(artifact_root=args.artifact_root, record=record,
                response_text=response['response_text'], state_path=args.state,
                writer_ledger_path=args.writer_ledger)
        elif args.command == "verify":
            result = verify_capture_manifest(args.manifest, task_run_id=args.task_run_id)
        elif args.command == 'rebuild-identity':
            result = rebuild_capture_identity(manifest_path=args.manifest,
                expected_manifest_sha256=args.expected_manifest_sha256, output_dir=args.output_dir)
        elif args.command == "recover-tail":
            with ProcessFileLock(args.manifest):
                result = recover_incomplete_manifest_tail(args.manifest)
        elif args.command == "recover-transactions":
            with ProcessFileLock(_transaction_lock_path(args.manifest)):
                result = recover_capture_transactions(
                    state_path=args.state,
                    writer_ledger_path=args.writer_ledger,
                    manifest_path=args.manifest,
                )
        else:
            from storage_contract import read_staging_json
            payload = read_staging_json(args.record)
            if not isinstance(payload, dict):
                raise ValueError("capture record must be a JSON object")
            if args.tool_result is not None:
                validate_initial_tool_input(payload)
                response = read_staging_json(args.tool_result)
                if not isinstance(response, dict) or set(response) != {'response_text'}:
                    raise ValueError('tool response envelope requires only response_text')
                registered = register_tool_response(artifact_root=args.manifest.parent, record=payload,
                    response_text=response['response_text'], state_path=args.state, writer_ledger_path=args.writer_ledger)
                for key in TOOL_MACHINE_FIELDS:
                    payload[key] = registered[key]
            result = commit_capture_transaction(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                manifest_path=args.manifest,
                snapshots_dir=args.snapshots_dir,
                record=payload,
                input_path=args.record,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "valid", "result": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
