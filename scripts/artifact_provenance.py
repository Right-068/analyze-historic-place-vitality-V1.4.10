#!/usr/bin/env python3
"""Protected-artifact provenance, truth-freeze, and delivery-seal controls.

This module is an execution-control layer only.  It does not define dimensions,
evidence thresholds, semantic rules, weights, or score mappings.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import strict_json as json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from runtime_guard import (
    RuntimeAuthorizationError,
    UPSTREAM_TRUTH_ROLES,
    authorize_runtime_write,
    canonical_sha256,
    load_runtime_state,
    machine_formal_dedup_sha256,
    normalized_url_sha256,
    sha256_file,
)
from process_lock import ProcessFileLock
from run_paths import logical_path, resolve_run_path
from formal_states import AUDIT_TERMINAL_STATUSES


WRITER_LEDGER_TYPE = "approved_writer_ledger"
TRUTH_FREEZE_TYPE = "evidence_truth_freeze"
DELIVERY_SEAL_TYPE = "formal_delivery_provenance"
REQUIRED_FREEZE_ROLES = {
    "search_log",
    "source_capture_manifest",
    "canonical_source_ledger",
    "canonical_evidence_ledger",
    "correction_event_ledger",
    "locator_audit",
    "collision_audit",
    "pre_admission_audit",
    "source_ledger",
    "raw_evidence",
    "semantic_evidence",
    "formal_evidence",
    "evidence_audit",
}
OPTIONAL_REVIEW_FREEZE_ROLES = {"reviewed_evidence", "review_change_ledger"}
REQUIRED_SCORE_ROLES = {"platform_scores", "scoring_output"}
DELIVERY_WRITER_ROLES = {
    "docx": "docx_report",
    "xlsx": "research_workbook",
    "rules_xlsx": "rules_workbook",
    "validation": "validation_report",
    "validation_error_manifest": "validation_error_manifest",
}
REPORT_CHAIN_ROLES = ("report_truth", "report_narrative", "report_data")
FROZEN_RELEASE_ASSETS = {
    "evaluation_protocol": "assets/evaluation-protocol.json",
    "semantic_codebook": "assets/semantic-quantification-codebook.json",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    # Windows readers may briefly deny replacement; never alter ACLs or adopt
    # unverified bytes. Persistent errors still stop the authorized writer.
    import time
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_text(path, json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n")


def _write_ledger_head(path: Path) -> None:
    raw = path.read_bytes()
    _atomic_json(path.with_name(path.name + ".head.json"),
                 {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})


def _verify_ledger_head(path: Path) -> None:
    head = path.with_name(path.name + ".head.json")
    actual = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    expected = json.loads(head.read_text(encoding="utf-8")) if head.is_file() else None
    if expected != actual:
        raise RuntimeAuthorizationError("protected_artifact_modified_outside_approved_writer",
            json.dumps({"path": path.name, "expected": expected, "actual": actual}, ensure_ascii=False))


def initialize_writer_ledger(path: Path, task_run_id: str) -> None:
    with ProcessFileLock(path):
        if path.exists():
            entries = _read_writer_ledger_unlocked(path, task_run_id=task_run_id)
            if entries:
                return
            if path.stat().st_size:
                raise ValueError("受保护写入台账已存在但格式无效")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        _write_ledger_head(path)


def _read_writer_ledger_unlocked(
    path: Path,
    *,
    task_run_id: str | None = None,
) -> list[dict[str, object]]:
    if not path.is_file():
        raise ValueError("缺少受保护写入台账")
    raw_bytes = path.read_bytes()
    if raw_bytes and not raw_bytes.endswith((b"\n", b"\r")):
        raise ValueError("受保护写入台账末行不完整，必须显式恢复后再继续")
    entries: list[dict[str, object]] = []
    previous = ""
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"受保护写入台账第 {number} 行损坏") from exc
        if not isinstance(entry, dict):
            raise ValueError("受保护写入台账记录必须为 JSON 对象")
        event_hash = str(entry.get("event_sha256", ""))
        core = {key: value for key, value in entry.items() if key != "event_sha256"}
        if event_hash != canonical_sha256(core):
            raise ValueError("受保护写入台账事件哈希无效")
        if str(entry.get("previous_event_sha256", "")) != previous:
            raise ValueError("受保护写入台账链断裂")
        if task_run_id is not None and entry.get("task_run_id") != task_run_id:
            raise ValueError("受保护写入台账混入不同 task_run_id")
        previous = event_hash
        entries.append(entry)
    for index, entry in enumerate(entries, start=1):
        if entry.get("event_id") != f"WRITE-{index:06d}":
            raise ValueError("受保护写入台账事件编号不连续")
    return entries


def read_writer_ledger(
    path: Path,
    *,
    task_run_id: str | None = None,
) -> list[dict[str, object]]:
    with ProcessFileLock(path):
        _verify_ledger_head(path)
        return _read_writer_ledger_unlocked(path, task_run_id=task_run_id)


def recover_incomplete_writer_ledger_tail(path: Path) -> dict[str, object]:
    """Quarantine only a torn final JSONL record, then revalidate the chain."""

    if not path.exists():
        return {"status": "missing", "removed_bytes": 0}
    raw = path.read_bytes()
    if not raw or raw.endswith((b"\n", b"\r")):
        return {"status": "unchanged", "removed_bytes": 0}
    boundary = raw.rfind(b"\n")
    good = raw[: boundary + 1] if boundary >= 0 else b""
    tail = raw[boundary + 1 :]
    quarantine = path.with_name(
        path.name + ".corrupt-tail-" + canonical_sha256({"bytes": tail.hex()})[:16] + ".bin"
    )
    if quarantine.exists() and quarantine.read_bytes() != tail:
        raise ValueError("受保护写入台账损坏尾部隔离文件冲突")
    quarantine.write_bytes(tail)
    temporary = path.with_name(path.name + ".recovered.tmp")
    temporary.write_bytes(good)
    os.replace(temporary, path)
    _read_writer_ledger_unlocked(path)
    return {
        "status": "recovered",
        "removed_bytes": len(tail),
        "quarantine_file": quarantine.name,
    }


def _append_writer_event_unlocked(path: Path, event: dict[str, object]) -> dict[str, object]:
    _verify_ledger_head(path)
    entries = _read_writer_ledger_unlocked(path, task_run_id=str(event["task_run_id"]))
    event["event_id"] = f"WRITE-{len(entries) + 1:06d}"
    event["previous_event_sha256"] = str(entries[-1]["event_sha256"]) if entries else ""
    event["event_sha256"] = canonical_sha256(event)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _write_ledger_head(path)
    return event


def _append_writer_event(path: Path, event: dict[str, object]) -> dict[str, object]:
    with ProcessFileLock(path):
        return _append_writer_event_unlocked(path, event)


def _portable_path(path: Path, run_root: Path) -> str:
    relative = logical_path(path, run_root)
    if not relative:
        raise ValueError(f"正式运行工件必须位于运行根目录内：{path.name}")
    return relative


def _portable_input_path(path: Path, run_root: Path, role: str) -> str:
    relative = logical_path(path, run_root)
    return relative or f"@release-or-external/{role}/{path.name}"


def _event_output_path(event: Mapping[str, object], run_root: Path) -> Path:
    reference = event.get("output_reference") or event.get("output_path", "")
    return resolve_run_path(reference, run_root, legacy_absolute=event.get("output_path", ""))


def _artifact_record_path(record: Mapping[str, object], state_path: Path) -> Path:
    reference = record.get("path_reference") or record.get("path", "")
    return resolve_run_path(
        reference,
        state_path.resolve().parent,
        legacy_absolute=record.get("path", ""),
    )


def _portable_artifact_record(path: Path, state_path: Path, role: str) -> dict[str, object]:
    logical = _portable_path(path, state_path.resolve().parent)
    return {
        "path": logical,
        "path_reference": {
            "path_kind": "run_relative",
            "logical_path": logical,
            "role": role,
        },
    }


def register_protected_artifact(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    writer_script_id: str,
    output_role: str,
    output_path: Path,
    input_paths: Mapping[str, Path] | None = None,
    expected_phase: str | None = None,
    provenance_context: Mapping[str, object] | None = None,
    atomic_registration: bool = False,
) -> dict[str, object]:
    authorization = authorize_runtime_write(
        state_path=state_path,
        writer_script_id=writer_script_id,
        output_role=output_role,
        expected_phase=expected_phase,
        output_path=output_path,
    )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeAuthorizationError("missing_written_artifact", "正式输出不存在或为空")
    task_run_id = str(authorization["task_run_id"])
    run_root = state_path.resolve().parent
    output_logical = _portable_path(output_path, run_root)
    output_hash = sha256_file(output_path)
    inputs = {
        name: {
            "path": _portable_input_path(path, run_root, name),
            "sha256": sha256_file(path),
        }
        for name, path in sorted((input_paths or {}).items())
        if path.is_file()
    }
    event = {
        "ledger_type": WRITER_LEDGER_TYPE,
        "task_run_id": task_run_id,
        "timestamp": utc_now(),
        "state_revision": int(authorization["state"].get("state_revision", 0)),
        "authorized_phase": authorization["phase"],
        "writer_script_id": writer_script_id,
        "writer_script_sha256": authorization["writer_script_sha256"],
        "release_manifest_sha256": authorization["release_manifest_sha256"],
        "output_role": output_role,
        "output_path": output_logical,
        "output_reference": {
            "path_kind": "run_relative",
            "logical_path": output_logical,
            "role": output_role,
        },
        "output_sha256": output_hash,
        "inputs": inputs,
    }
    context = dict(provenance_context or {})
    allowed_context = {
        "transaction_id",
        "capture_event_id",
        "capture_manifest_prefix_sha256",
        "transaction_journal_path",
    }
    unknown_context = set(context) - allowed_context
    if unknown_context:
        raise ValueError(
            "unsupported protected-artifact provenance context: "
            + ", ".join(sorted(unknown_context))
        )
    event.update({key: context[key] for key in sorted(context)})
    with ProcessFileLock(writer_ledger_path):
        if not writer_ledger_path.exists():
            writer_ledger_path.parent.mkdir(parents=True, exist_ok=True)
            writer_ledger_path.write_text("", encoding="utf-8")
            _write_ledger_head(writer_ledger_path)
        _verify_ledger_head(writer_ledger_path)
        entries = _read_writer_ledger_unlocked(writer_ledger_path, task_run_id=task_run_id)
        latest_same_path = next(
            (
                item
                for item in reversed(entries)
                if _event_output_path(item, run_root) == output_path.resolve()
            ),
            None,
        )
        if (
            latest_same_path is not None
            and latest_same_path.get("output_sha256") == output_hash
            and latest_same_path.get("output_role") == output_role
            and latest_same_path.get("writer_script_id") == writer_script_id
        ):
            return latest_same_path
        if atomic_registration:
            event["event_id"] = f"WRITE-{len(entries) + 1:06d}"
            event["previous_event_sha256"] = str(entries[-1]["event_sha256"]) if entries else ""
            event["event_sha256"] = canonical_sha256(event)
            from execution_facts import atomic_bytes
            payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            atomic_bytes(writer_ledger_path, writer_ledger_path.read_bytes() + payload.encode("utf-8"))
            _write_ledger_head(writer_ledger_path)
            return event
        return _append_writer_event_unlocked(writer_ledger_path, event)


def verify_artifact_writer(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    output_role: str,
    output_path: Path,
) -> dict[str, object]:
    state = load_runtime_state(state_path)
    task_run_id = str(state["task_run_id"])
    entries = read_writer_ledger(writer_ledger_path, task_run_id=task_run_id)
    run_root = state_path.resolve().parent
    matches = [
        item
        for item in entries
        if _event_output_path(item, run_root) == output_path.resolve()
        and item.get("output_role") == output_role
    ]
    if not matches:
        raise RuntimeAuthorizationError(
            "untrusted_artifact_provenance",
            f"{output_role} 缺少允许的正式写入记录",
        )
    latest = matches[-1]
    if not output_path.is_file() or latest.get("output_sha256") != sha256_file(output_path):
        raise RuntimeAuthorizationError(
            "untrusted_artifact_provenance",
            f"{output_role} 当前哈希不是正式写入台账中的最新哈希",
        )
    return latest


def verify_artifact_writer_any(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    output_roles: set[str] | tuple[str, ...] | list[str],
    output_path: Path,
) -> dict[str, object]:
    """Accept an input only when one explicitly allowed formal role owns it."""
    errors: list[str] = []
    for role in output_roles:
        try:
            return verify_artifact_writer(
                state_path=state_path,
                writer_ledger_path=writer_ledger_path,
                output_role=str(role),
                output_path=output_path,
            )
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    raise RuntimeAuthorizationError(
        "untrusted_artifact_provenance",
        f"正式输入未由允许的上游角色写入：{sorted(str(role) for role in output_roles)}",
    )


def assert_frozen_artifact_path(
    freeze: Mapping[str, object],
    *,
    role: str,
    path: Path,
) -> None:
    pools = [freeze.get("upstream_artifacts"), freeze.get("scoring_artifacts")]
    record: Mapping[str, object] | None = None
    for pool in pools:
        if isinstance(pool, dict) and isinstance(pool.get(role), dict):
            record = pool[role]
            break
    if record is None:
        raise ValueError(f"真值冻结清单缺少工件角色：{role}")
    raw_path = Path(str(record.get("path", "")))
    if raw_path.is_absolute():
        if raw_path.resolve() != path.resolve():
            raise ValueError(f"正式输入不是冻结清单绑定的工件：{role}")
    elif raw_path.name != path.name:
        raise ValueError(f"正式输入不是冻结清单绑定的工件：{role}")
    if not path.is_file() or record.get("sha256") != sha256_file(path):
        raise RuntimeAuthorizationError(
            "post_validate_upstream_mutation",
            f"冻结工件当前哈希不一致：{role}",
        )


def assert_frozen_release_asset(
    freeze: Mapping[str, object],
    *,
    role: str,
    path: Path,
) -> None:
    assets = freeze.get("release_assets")
    record = assets.get(role) if isinstance(assets, dict) else None
    if not isinstance(record, dict):
        raise ValueError(f"真值冻结清单缺少发布资产：{role}")
    logical = str(record.get("logical_path") or record.get("path", ""))
    if logical and logical not in {path.name, path.as_posix()} and not path.as_posix().endswith(logical):
        raise ValueError(f"当前发布资产不是冻结清单绑定文件：{role}")
    if not path.is_file() or record.get("sha256") != sha256_file(path):
        raise ValueError(f"冻结发布资产已改变：{role}")


def verify_preflight(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    preflight_path: Path,
) -> dict[str, object]:
    state = load_runtime_state(state_path)
    verify_artifact_writer(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        output_role="preflight_report",
        output_path=preflight_path,
    )
    payload = json.loads(preflight_path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "valid"
        or payload.get("task_run_id") != state.get("task_run_id")
        or payload.get("errors")
    ):
        raise RuntimeAuthorizationError("preflight_invalid", "正式全链预检未通过")
    return payload


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV 缺少表头")
        return list(reader.fieldnames), [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _normalize_promoted_csv(
    source_path: Path,
    output_path: Path,
    *,
    output_role: str,
    task_run_id: str,
) -> None:
    fields, rows = _read_csv(source_path)
    for row in rows:
        if row.get("task_run_id", "") != task_run_id:
            raise ValueError(f"{output_role} 包含不同或空 task_run_id")
    if output_role == "source_ledger":
        if "normalized_url_sha256" not in fields:
            fields.append("normalized_url_sha256")
        for row in rows:
            from source_identity import source_qualification_errors
            issues = source_qualification_errors(row)
            if issues:
                raise ValueError(";".join(issues))
    elif output_role == "raw_evidence":
        for field in (
            "formal_dedup_sha256",
            "reviewer_id",
            "review_origin",
            "review_record_id",
            "review_decision_time",
            "review_provenance_sha256",
            "review_provenance_valid",
        ):
            if field not in fields:
                fields.append(field)
            for row in rows:
                row.setdefault(field, "")
    _write_csv(output_path, fields, rows)


def promote_artifact(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    input_path: Path,
    output_path: Path,
    output_role: str,
) -> dict[str, object]:
    if output_role in {"source_ledger", "raw_evidence", "report_data"}:
        raise RuntimeAuthorizationError(
            "untrusted_canonical_writer",
            "正式来源、正式证据和报告数据只能由其发布版专用写入器生成",
        )
    authorization = authorize_runtime_write(
        state_path=state_path,
        writer_script_id="artifact_provenance.py",
        output_role=output_role,
        output_path=output_path,
    )
    if not input_path.is_file() or input_path.stat().st_size <= 0:
        raise ValueError("待提升工作文件不存在或为空")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if input_path.suffix.lower() == ".csv":
        _normalize_promoted_csv(
            input_path,
            output_path,
            output_role=output_role,
            task_run_id=str(authorization["task_run_id"]),
        )
    else:
        temporary = output_path.with_name(output_path.name + ".tmp")
        shutil.copyfile(input_path, temporary)
        os.replace(temporary, output_path)
    event = register_protected_artifact(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        writer_script_id="artifact_provenance.py",
        output_role=output_role,
        output_path=output_path,
        input_paths={"staging_input": input_path},
    )
    return {
        "status": "promoted",
        "task_run_id": authorization["task_run_id"],
        "output_role": output_role,
        "output": str(output_path.resolve()),
        "output_sha256": event["output_sha256"],
        "writer_event_sha256": event["event_sha256"],
    }


def _artifact_snapshot(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    artifacts: Mapping[str, Path],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for role, path in sorted(artifacts.items()):
        writer = verify_artifact_writer(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            output_role=role,
            output_path=path,
        )
        result[role] = {
            **_portable_artifact_record(path, state_path, role),
            "sha256": sha256_file(path),
            "writer_event_sha256": writer["event_sha256"],
        }
    return result


def _freeze_core(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "freeze_sha256"}


def create_truth_freeze(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    output_path: Path,
    artifacts: Mapping[str, Path],
) -> dict[str, object]:
    authorization = authorize_runtime_write(
        state_path=state_path,
        writer_script_id="artifact_provenance.py",
        output_role="truth_freeze",
        expected_phase="EVIDENCE_AUDIT",
        output_path=output_path,
    )
    state = authorization["state"]
    if str(state.get("audit_status", "")) not in AUDIT_TERMINAL_STATUSES:
        raise ValueError("正式证据审计未通过，不能冻结研究真值")
    roles = set(artifacts)
    if not REQUIRED_FREEZE_ROLES.issubset(roles):
        raise ValueError("真值冻结缺少正式上游基础工件")
    if not roles.issubset(REQUIRED_FREEZE_ROLES | OPTIONAL_REVIEW_FREEZE_ROLES):
        raise ValueError("真值冻结包含未授权的工件角色")
    review_roles = roles & OPTIONAL_REVIEW_FREEZE_ROLES
    if review_roles and review_roles != OPTIONAL_REVIEW_FREEZE_ROLES:
        raise ValueError("人工复核证据与复核变更台账必须同时冻结")
    _, formal_rows = _read_csv(artifacts["formal_evidence"])
    contains_human_confirmation = any(
        row.get("semantic_review_status") == "human_confirmed" for row in formal_rows
    )
    if contains_human_confirmation and review_roles != OPTIONAL_REVIEW_FREEZE_ROLES:
        raise ValueError("正式证据包含人工确认记录，必须冻结受信任复核证据及变更台账")
    upstream = _artifact_snapshot(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        artifacts=artifacts,
    )
    skill_root = Path(str(state.get("skill_root", "")))
    release_assets = {
        role: {
            "path": relative,
            "path_kind": "skill_relative",
            "logical_path": relative,
            "sha256": sha256_file(skill_root / relative),
        }
        for role, relative in FROZEN_RELEASE_ASSETS.items()
    }
    payload: dict[str, object] = {
        "manifest_type": TRUTH_FREEZE_TYPE,
        "task_run_id": authorization["task_run_id"],
        "created_at": utc_now(),
        "freeze_revision": 1,
        "release_manifest_sha256": authorization["release_manifest_sha256"],
        "upstream_artifacts": upstream,
        "upstream_root_sha256": canonical_sha256(upstream),
        "release_assets": release_assets,
        "release_assets_root_sha256": canonical_sha256(release_assets),
        "scoring_artifacts": {},
    }
    payload["manifest_path"] = _portable_path(output_path, state_path.resolve().parent)
    payload["freeze_sha256"] = canonical_sha256(_freeze_core(payload))
    _atomic_json(output_path, payload)
    register_protected_artifact(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        writer_script_id="artifact_provenance.py",
        output_role="truth_freeze",
        output_path=output_path,
        input_paths=artifacts,
        expected_phase="EVIDENCE_AUDIT",
    )
    return payload


def verify_truth_freeze(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    manifest_path: Path,
    require_scoring: bool = False,
) -> dict[str, object]:
    state = load_runtime_state(state_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("manifest_type") != TRUTH_FREEZE_TYPE:
        raise ValueError("真值冻结清单格式无效")
    if payload.get("task_run_id") != state.get("task_run_id"):
        raise ValueError("真值冻结清单属于不同 task_run_id")
    if payload.get("freeze_sha256") != canonical_sha256(_freeze_core(payload)):
        raise ValueError("真值冻结清单自身哈希无效")
    upstream = payload.get("upstream_artifacts")
    if (
        not isinstance(upstream, dict)
        or not REQUIRED_FREEZE_ROLES.issubset(set(upstream))
        or not set(upstream).issubset(REQUIRED_FREEZE_ROLES | OPTIONAL_REVIEW_FREEZE_ROLES)
    ):
        raise ValueError("真值冻结清单缺少正式上游工件")
    review_roles = set(upstream) & OPTIONAL_REVIEW_FREEZE_ROLES
    if review_roles and review_roles != OPTIONAL_REVIEW_FREEZE_ROLES:
        raise ValueError("真值冻结清单的人工复核工件不完整")
    if payload.get("upstream_root_sha256") != canonical_sha256(upstream):
        raise ValueError("真值冻结上游根哈希无效")
    release_assets = payload.get("release_assets")
    if not isinstance(release_assets, dict) or set(release_assets) != set(FROZEN_RELEASE_ASSETS):
        raise ValueError("真值冻结清单缺少评价协议或语义代码簿")
    if payload.get("release_assets_root_sha256") != canonical_sha256(release_assets):
        raise ValueError("真值冻结发布资产根哈希无效")
    for role, relative in FROZEN_RELEASE_ASSETS.items():
        expected_path = Path(str(state.get("skill_root", ""))) / relative
        assert_frozen_release_asset(payload, role=role, path=expected_path)
    for role, raw in upstream.items():
        if not isinstance(raw, dict):
            raise ValueError("真值冻结工件记录格式无效")
        path = _artifact_record_path(raw, state_path)
        if not path.is_file() or sha256_file(path) != raw.get("sha256"):
            raise RuntimeAuthorizationError(
                "post_validate_upstream_mutation",
                f"冻结上游工件已改变：{role}",
            )
        writer = verify_artifact_writer(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            output_role=str(role),
            output_path=path,
        )
        if writer.get("event_sha256") != raw.get("writer_event_sha256"):
            raise ValueError(f"真值冻结写入来源不一致：{role}")
    scoring = payload.get("scoring_artifacts")
    if not isinstance(scoring, dict):
        raise ValueError("真值冻结评分扩展格式无效")
    if require_scoring and set(scoring) != REQUIRED_SCORE_ROLES:
        raise ValueError("真值冻结尚未绑定完整正式评分工件")
    for role, raw in scoring.items():
        if not isinstance(raw, dict):
            raise ValueError("真值冻结评分工件格式无效")
        path = _artifact_record_path(raw, state_path)
        if not path.is_file() or sha256_file(path) != raw.get("sha256"):
            raise ValueError(f"冻结评分工件已改变：{role}")
        writer = verify_artifact_writer(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            output_role=str(role),
            output_path=path,
        )
        if writer.get("event_sha256") != raw.get("writer_event_sha256"):
            raise ValueError(f"真值冻结评分来源不一致：{role}")
    if require_scoring:
        from formal_gates import verify_score_gate
        verify_score_gate(state_path=state_path, writer_ledger_path=writer_ledger_path,
                          manifest_path=manifest_path, freeze=payload)
    return payload


def extend_truth_freeze_with_scores(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    manifest_path: Path,
    scoring_artifacts: Mapping[str, Path],
) -> dict[str, object]:
    authorize_runtime_write(
        state_path=state_path,
        writer_script_id="artifact_provenance.py",
        output_role="truth_freeze",
        expected_phase="SCORE",
        output_path=manifest_path,
    )
    payload = verify_truth_freeze(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        manifest_path=manifest_path,
    )
    if set(scoring_artifacts) != REQUIRED_SCORE_ROLES:
        raise ValueError("评分扩展必须精确包含平台评分输入和综合评分输出")
    scoring = _artifact_snapshot(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        artifacts=scoring_artifacts,
    )
    payload = dict(payload)
    payload["freeze_revision"] = int(payload.get("freeze_revision", 1)) + 1
    payload["scoring_bound_at"] = utc_now()
    payload["scoring_artifacts"] = scoring
    payload["freeze_sha256"] = canonical_sha256(_freeze_core(payload))
    _atomic_json(manifest_path, payload)
    register_protected_artifact(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        writer_script_id="artifact_provenance.py",
        output_role="truth_freeze",
        output_path=manifest_path,
        input_paths=scoring_artifacts,
        expected_phase="SCORE",
    )
    from formal_gates import gate_path, score_binding
    score_gate = gate_path(manifest_path)
    if score_gate.exists():
        verify_artifact_writer(state_path=state_path, writer_ledger_path=writer_ledger_path,
                               output_role="score_gate", output_path=score_gate)
    gate = score_binding(state_path=state_path, manifest_path=manifest_path, freeze=payload)
    _atomic_json(score_gate, {**gate, "created_at": utc_now()})
    register_protected_artifact(state_path=state_path, writer_ledger_path=writer_ledger_path,
        writer_script_id="artifact_provenance.py", output_role="score_gate", output_path=score_gate,
        input_paths={**scoring_artifacts, "truth_freeze": manifest_path}, expected_phase="SCORE")
    return payload


def _seal_core(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "seal_sha256"}


def _delivery_seal_fault(stage: str) -> None:
    """Deterministic test-only crash point for seal transaction recovery."""

    if os.environ.get("HISTORIC_VITALITY_DELIVERY_SEAL_FAULT", "") == stage:
        raise RuntimeError(f"delivery_seal_fault:{stage}")


def _writer_ledger_prefix_unlocked(
    path: Path,
    *,
    task_run_id: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    entries = _read_writer_ledger_unlocked(path, task_run_id=task_run_id)
    raw = path.read_bytes()
    last = entries[-1] if entries else {}
    return (
        {
            "writer_ledger_hash_scope": "prefix_before_delivery_registration",
            "writer_ledger_prefix_length": len(raw),
            "writer_ledger_prefix_sha256": hashlib.sha256(raw).hexdigest(),
            "writer_ledger_prefix_event_count": len(entries),
            "writer_ledger_prefix_last_event_id": str(last.get("event_id", "")),
            "writer_ledger_prefix_last_event_sha256": str(last.get("event_sha256", "")),
        },
        entries,
    )


def _delivery_registration_event(
    *,
    authorization: Mapping[str, object],
    state_path: Path,
    output_path: Path,
    input_paths: Mapping[str, Path],
    prefix: Mapping[str, object],
    finalization_transaction_id: str,
    seal_sha256: str,
) -> dict[str, object]:
    run_root = state_path.resolve().parent
    output_logical = _portable_path(output_path, run_root)
    state = authorization.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("交付来源封印缺少已授权状态")
    return {
        "ledger_type": WRITER_LEDGER_TYPE,
        "task_run_id": str(authorization["task_run_id"]),
        "timestamp": utc_now(),
        "state_revision": int(state.get("state_revision", 0)),
        "authorized_phase": authorization["phase"],
        "writer_script_id": "manage_run_state.py",
        "writer_script_sha256": authorization["writer_script_sha256"],
        "release_manifest_sha256": authorization["release_manifest_sha256"],
        "output_role": "delivery_provenance",
        "output_path": output_logical,
        "output_reference": {
            "path_kind": "run_relative",
            "logical_path": output_logical,
            "role": "delivery_provenance",
        },
        "output_sha256": sha256_file(output_path),
        "inputs": {
            name: {
                "path": _portable_input_path(path, run_root, name),
                "sha256": sha256_file(path),
            }
            for name, path in sorted(input_paths.items())
            if path.is_file()
        },
        "transaction_id": finalization_transaction_id,
        "delivery_seal_sha256": seal_sha256,
        **dict(prefix),
    }


def _delivery_registration_matches(
    event: Mapping[str, object],
    *,
    payload: Mapping[str, object],
    state_path: Path,
    seal_path: Path,
) -> bool:
    run_root = state_path.resolve().parent
    return bool(
        event.get("output_role") == "delivery_provenance"
        and _event_output_path(event, run_root) == seal_path.resolve()
        and event.get("output_sha256") == sha256_file(seal_path)
        and event.get("transaction_id") == payload.get("finalization_transaction_id")
        and event.get("delivery_seal_sha256") == payload.get("seal_sha256")
        and all(
            event.get(field) == payload.get(field)
            for field in (
                "writer_ledger_hash_scope",
                "writer_ledger_prefix_length",
                "writer_ledger_prefix_sha256",
                "writer_ledger_prefix_event_count",
                "writer_ledger_prefix_last_event_id",
                "writer_ledger_prefix_last_event_sha256",
            )
        )
    )


def _verify_delivery_ledger_closure(
    *,
    payload: Mapping[str, object],
    state_path: Path,
    writer_ledger_path: Path,
    seal_path: Path,
    allow_missing_registration: bool = False,
) -> dict[str, object]:
    task_run_id = str(payload.get("task_run_id", ""))
    with ProcessFileLock(writer_ledger_path):
        entries = _read_writer_ledger_unlocked(
            writer_ledger_path, task_run_id=task_run_id
        )
        raw = writer_ledger_path.read_bytes()
        try:
            prefix_length = int(payload.get("writer_ledger_prefix_length", -1))
            prefix_count = int(payload.get("writer_ledger_prefix_event_count", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("交付来源封印的写入台账前缀字段无效") from exc
        if prefix_length < 0 or prefix_count < 0 or prefix_length > len(raw):
            raise ValueError("交付来源封印的写入台账前缀范围无效")
        prefix_bytes = raw[:prefix_length]
        if hashlib.sha256(prefix_bytes).hexdigest() != payload.get(
            "writer_ledger_prefix_sha256"
        ):
            raise ValueError("交付来源封印绑定的写入台账前缀哈希不一致")
        if payload.get("approved_writer_ledger_sha256") != payload.get(
            "writer_ledger_prefix_sha256"
        ):
            raise ValueError("交付来源封印的写入台账批准哈希作用域不一致")
        if payload.get("writer_ledger_hash_scope") != "prefix_before_delivery_registration":
            raise ValueError("交付来源封印的写入台账哈希作用域无效")
        if prefix_count > len(entries):
            raise ValueError("交付来源封印的写入台账前缀事件数量无效")
        prefix_entries = entries[:prefix_count]
        last = prefix_entries[-1] if prefix_entries else {}
        if payload.get("writer_ledger_prefix_last_event_id") != str(
            last.get("event_id", "")
        ) or payload.get("writer_ledger_prefix_last_event_sha256") != str(
            last.get("event_sha256", "")
        ):
            raise ValueError("交付来源封印绑定的写入台账前缀尾事件不一致")
        suffix = entries[prefix_count:]
        if not suffix and allow_missing_registration:
            return {"status": "registration_missing", "prefix": dict(payload)}
        if len(suffix) != 1:
            raise ValueError("交付来源封印之后必须且只能存在一条封印登记事件")
        event = suffix[0]
        if not _delivery_registration_matches(
            event,
            payload=payload,
            state_path=state_path,
            seal_path=seal_path,
        ):
            raise ValueError("交付来源封印登记事件与封印内容不一致")
        return {"status": "valid", "registration_event": dict(event)}


def create_delivery_provenance(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    truth_freeze_path: Path,
    output_path: Path,
    artifacts: Mapping[str, Path],
    finalization_transaction_id: str = "",
    created_at: str = "",
) -> dict[str, object]:
    authorization = authorize_runtime_write(
        state_path=state_path,
        writer_script_id="manage_run_state.py",
        output_role="delivery_provenance",
        expected_phase="DELIVER",
        output_path=output_path,
    )
    truth = verify_truth_freeze(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        manifest_path=truth_freeze_path,
        require_scoring=True,
    )
    required = {*DELIVERY_WRITER_ROLES, "detailed_log"}
    if set(artifacts) != required:
        raise ValueError("交付来源封印缺少正式成果或详细运行日志")
    sealed: dict[str, object] = {}
    for name, path in sorted(artifacts.items()):
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"交付工件不存在或为空：{name}")
        item: dict[str, object] = {
            **_portable_artifact_record(path, state_path, name),
            "sha256": sha256_file(path),
        }
        role = DELIVERY_WRITER_ROLES.get(name)
        if role:
            writer = verify_artifact_writer(
                state_path=state_path,
                writer_ledger_path=writer_ledger_path,
                output_role=role,
                output_path=path,
            )
            item["writer_event_sha256"] = writer["event_sha256"]
        sealed[name] = item
    upstream = truth["upstream_artifacts"]
    scoring = truth["scoring_artifacts"]
    assert isinstance(upstream, dict) and isinstance(scoring, dict)
    writer_events = read_writer_ledger(
        writer_ledger_path,
        task_run_id=str(authorization["task_run_id"]),
    )
    report_chain: dict[str, dict[str, object]] = {}
    for role in REPORT_CHAIN_ROLES:
        event = next(
            (item for item in reversed(writer_events) if item.get("output_role") == role),
            None,
        )
        if event is None:
            raise ValueError(f"交付来源封印缺少正式报告链：{role}")
        path = _event_output_path(event, state_path.resolve().parent)
        verified = verify_artifact_writer(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            output_role=role,
            output_path=path,
        )
        report_chain[role] = {
            **_portable_artifact_record(path, state_path, role),
            "sha256": sha256_file(path),
            "writer_event_sha256": verified["event_sha256"],
        }
    payload: dict[str, object] = {
        "manifest_type": DELIVERY_SEAL_TYPE,
        "schema_version": "deliverable-manifest-1",
        "status": "valid",
        "task_run_id": authorization["task_run_id"],
        "created_at": created_at or utc_now(),
        "finalization_transaction_id": finalization_transaction_id,
        "release_manifest_sha256": authorization["release_manifest_sha256"],
        "truth_freeze_path": _portable_path(
            truth_freeze_path, state_path.resolve().parent
        ),
        "truth_freeze_reference": {
            "path_kind": "run_relative",
            "logical_path": _portable_path(
                truth_freeze_path, state_path.resolve().parent
            ),
            "role": "truth_freeze",
        },
        "truth_freeze_file_sha256": sha256_file(truth_freeze_path),
        "truth_freeze_sha256": json.loads(
            truth_freeze_path.read_text(encoding="utf-8-sig")
        ).get("freeze_sha256"),
        "source_ledger_sha256": upstream["source_ledger"]["sha256"],
        "source_capture_manifest_sha256": upstream["source_capture_manifest"]["sha256"],
        "canonical_source_ledger_sha256": upstream["canonical_source_ledger"]["sha256"],
        "canonical_evidence_ledger_sha256": upstream["canonical_evidence_ledger"]["sha256"],
        "correction_event_ledger_sha256": upstream["correction_event_ledger"]["sha256"],
        "locator_audit_sha256": upstream["locator_audit"]["sha256"],
        "collision_audit_sha256": upstream["collision_audit"]["sha256"],
        "pre_admission_audit_sha256": upstream["pre_admission_audit"]["sha256"],
        "raw_evidence_sha256": upstream["raw_evidence"]["sha256"],
        "semantic_evidence_sha256": upstream["semantic_evidence"]["sha256"],
        "formal_evidence_sha256": upstream["formal_evidence"]["sha256"],
        "search_log_sha256": upstream["search_log"]["sha256"],
        "dimension_audit_sha256": upstream["evidence_audit"]["sha256"],
        "scoring_input_sha256": scoring["platform_scores"]["sha256"],
        "scoring_output_sha256": scoring["scoring_output"]["sha256"],
        "report_truth_sha256": report_chain["report_truth"]["sha256"],
        "report_narrative_sha256": report_chain["report_narrative"]["sha256"],
        "report_data_sha256": report_chain["report_data"]["sha256"],
        "validation_sha256": sealed["validation"]["sha256"],
        "score_gate_sha256": sha256_file(truth_freeze_path.with_name("score-gate.json")),
        "builder_script_hashes": {name: sha256_file(Path(authorization["skill_root"]) / "scripts" / name)
            for name in ("build_docx_report.py", "build_research_workbook.py", "build_semantic_rules_workbook.py")},
        "validator_script_sha256": sha256_file(Path(authorization["skill_root"]) / "scripts/validate_deliverables.py"),
        "state_revision": int(authorization["state"].get("state_revision", 0)),
        "validation_rounds": int(authorization["state"].get("validation_rounds", 0)),
        "repair_cycles": int(authorization["state"].get("repair_cycles", 0)),
        "pipeline_rebuilds_after_validation": int(
            authorization["state"].get("pipeline_rebuilds_after_validation", 0)
        ),
        "artifacts": sealed,
        "report_chain": report_chain,
    }
    seal_inputs = {
        **artifacts,
        "truth_freeze": truth_freeze_path,
        **{
            role: _artifact_record_path(item, state_path)
            for role, item in report_chain.items()
        },
    }

    if output_path.is_file():
        existing = _verify_delivery_provenance_content(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            seal_path=output_path,
        )
        if (
            existing.get("finalization_transaction_id") != finalization_transaction_id
            or (created_at and existing.get("created_at") != created_at)
        ):
            raise ValueError("交付来源封印已存在且不属于同一最终化事务")
        closure = _verify_delivery_ledger_closure(
            payload=existing,
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            seal_path=output_path,
            allow_missing_registration=True,
        )
        if closure["status"] == "registration_missing":
            # Crash recovery is allowed only when the ledger is still exactly
            # the prefix captured by the already-written seal.
            with ProcessFileLock(writer_ledger_path):
                current_prefix, _entries = _writer_ledger_prefix_unlocked(
                    writer_ledger_path,
                    task_run_id=str(authorization["task_run_id"]),
                )
                for field in current_prefix:
                    if current_prefix[field] != existing.get(field):
                        raise ValueError("交付封印登记恢复期间写入台账已发生变化")
                event = _delivery_registration_event(
                    authorization=authorization,
                    state_path=state_path,
                    output_path=output_path,
                    input_paths=seal_inputs,
                    prefix=current_prefix,
                    finalization_transaction_id=finalization_transaction_id,
                    seal_sha256=str(existing["seal_sha256"]),
                )
                _append_writer_event_unlocked(writer_ledger_path, event)
        return verify_delivery_provenance(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            seal_path=output_path,
        )

    with ProcessFileLock(writer_ledger_path):
        if output_path.exists():
            raise ValueError("交付来源封印由并发最终化创建；请按同一事务重试恢复")
        prefix, _entries = _writer_ledger_prefix_unlocked(
            writer_ledger_path,
            task_run_id=str(authorization["task_run_id"]),
        )
        payload.update(prefix)
        # Compatibility field retained with an explicit immutable prefix
        # scope; it no longer pretends to hash the post-registration ledger.
        payload["approved_writer_ledger_sha256"] = prefix[
            "writer_ledger_prefix_sha256"
        ]
        payload["seal_sha256"] = canonical_sha256(_seal_core(payload))
        _atomic_json(output_path, payload)
        _delivery_seal_fault("after_seal_before_registration")
        event = _delivery_registration_event(
            authorization=authorization,
            state_path=state_path,
            output_path=output_path,
            input_paths=seal_inputs,
            prefix=prefix,
            finalization_transaction_id=finalization_transaction_id,
            seal_sha256=str(payload["seal_sha256"]),
        )
        _append_writer_event_unlocked(writer_ledger_path, event)
        _delivery_seal_fault("after_registration")
    return verify_delivery_provenance(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        seal_path=output_path,
    )


def _verify_delivery_provenance_content(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    seal_path: Path,
) -> dict[str, object]:
    state = load_runtime_state(state_path)
    payload = json.loads(seal_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("manifest_type") != DELIVERY_SEAL_TYPE:
        raise ValueError("交付来源封印格式无效")
    if payload.get("task_run_id") != state.get("task_run_id"):
        raise ValueError("交付来源封印属于不同 task_run_id")
    if payload.get("schema_version") != "deliverable-manifest-1" or payload.get("status") != "valid":
        raise ValueError("deliverable_manifest_not_valid")
    if payload.get("seal_sha256") != canonical_sha256(_seal_core(payload)):
        raise ValueError("交付来源封印自身哈希无效")
    truth_path = resolve_run_path(
        payload.get("truth_freeze_reference") or payload.get("truth_freeze_path", ""),
        state_path.resolve().parent,
        legacy_absolute=payload.get("truth_freeze_path", ""),
    )
    if not truth_path.is_file() or sha256_file(truth_path) != payload.get("truth_freeze_file_sha256"):
        raise ValueError("交付来源封印绑定的真值冻结文件已改变")
    freeze = verify_truth_freeze(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        manifest_path=truth_path,
        require_scoring=True,
    )
    if payload.get("truth_freeze_sha256") != freeze.get("freeze_sha256"):
        raise ValueError("交付来源封印与真值冻结根哈希不一致")
    if payload.get("score_gate_sha256") != sha256_file(truth_path.with_name("score-gate.json")):
        raise ValueError("deliverable_manifest_score_gate_mismatch")
    skill = Path(state["skill_root"])
    builders = {name: sha256_file(skill / "scripts" / name) for name in
        ("build_docx_report.py", "build_research_workbook.py", "build_semantic_rules_workbook.py")}
    if payload.get("builder_script_hashes") != builders or payload.get("validator_script_sha256") != sha256_file(skill / "scripts/validate_deliverables.py"):
        raise ValueError("deliverable_manifest_writer_hash_mismatch")
    upstream = freeze["upstream_artifacts"]
    scoring = freeze["scoring_artifacts"]
    assert isinstance(upstream, dict) and isinstance(scoring, dict)
    expected_hashes = {
        "source_ledger_sha256": upstream["source_ledger"]["sha256"],
        "source_capture_manifest_sha256": upstream["source_capture_manifest"]["sha256"],
        "canonical_source_ledger_sha256": upstream["canonical_source_ledger"]["sha256"],
        "canonical_evidence_ledger_sha256": upstream["canonical_evidence_ledger"]["sha256"],
        "correction_event_ledger_sha256": upstream["correction_event_ledger"]["sha256"],
        "locator_audit_sha256": upstream["locator_audit"]["sha256"],
        "collision_audit_sha256": upstream["collision_audit"]["sha256"],
        "pre_admission_audit_sha256": upstream["pre_admission_audit"]["sha256"],
        "raw_evidence_sha256": upstream["raw_evidence"]["sha256"],
        "semantic_evidence_sha256": upstream["semantic_evidence"]["sha256"],
        "formal_evidence_sha256": upstream["formal_evidence"]["sha256"],
        "search_log_sha256": upstream["search_log"]["sha256"],
        "dimension_audit_sha256": upstream["evidence_audit"]["sha256"],
        "scoring_input_sha256": scoring["platform_scores"]["sha256"],
        "scoring_output_sha256": scoring["scoring_output"]["sha256"],
    }
    for field, expected in expected_hashes.items():
        if payload.get(field) != expected:
            raise ValueError(f"交付来源封印的正式链哈希不一致：{field}")
    report_chain = payload.get("report_chain")
    if not isinstance(report_chain, dict) or set(report_chain) != set(REPORT_CHAIN_ROLES):
        raise ValueError("交付来源封印缺少正式报告真值链")
    for role in REPORT_CHAIN_ROLES:
        raw = report_chain.get(role)
        if not isinstance(raw, dict):
            raise ValueError(f"交付来源封印报告链格式无效：{role}")
        path = _artifact_record_path(raw, state_path)
        if not path.is_file() or sha256_file(path) != raw.get("sha256"):
            raise ValueError(f"交付来源封印报告链哈希不一致：{role}")
        writer = verify_artifact_writer(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            output_role=role,
            output_path=path,
        )
        if writer.get("event_sha256") != raw.get("writer_event_sha256"):
            raise ValueError(f"交付来源封印报告链写入来源不一致：{role}")
        if payload.get(f"{role}_sha256") != raw.get("sha256"):
            raise ValueError(f"交付来源封印报告链摘要不一致：{role}")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("交付来源封印缺少工件哈希")
    for name, raw in artifacts.items():
        if not isinstance(raw, dict):
            raise ValueError("交付来源封印工件格式无效")
        path = _artifact_record_path(raw, state_path)
        if not path.is_file() or sha256_file(path) != raw.get("sha256"):
            raise ValueError(f"交付工件哈希与封印不一致：{name}")
        role = DELIVERY_WRITER_ROLES.get(str(name))
        if role:
            writer = verify_artifact_writer(
                state_path=state_path,
                writer_ledger_path=writer_ledger_path,
                output_role=role,
                output_path=path,
            )
            if writer.get("event_sha256") != raw.get("writer_event_sha256"):
                raise ValueError(f"交付工件写入来源与封印不一致：{name}")
    validation = artifacts.get("validation")
    if not isinstance(validation, dict) or payload.get("validation_sha256") != validation.get("sha256"):
        raise ValueError("交付来源封印的验收哈希不一致")
    return payload


def verify_delivery_provenance(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    seal_path: Path,
) -> dict[str, object]:
    """Verify both delivery content and the immutable writer-ledger closure."""

    payload = _verify_delivery_provenance_content(
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        seal_path=seal_path,
    )
    _verify_delivery_ledger_closure(
        payload=payload,
        state_path=state_path,
        writer_ledger_path=writer_ledger_path,
        seal_path=seal_path,
    )
    return payload


def _parse_artifacts(values: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--artifact 必须使用 role=path")
        role, raw_path = value.split("=", 1)
        role = role.strip()
        if not role or role in artifacts:
            raise ValueError("--artifact role 为空或重复")
        artifacts[role] = Path(raw_path)
    return artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    promote = sub.add_parser("promote")
    promote.add_argument("--state", type=Path, required=True)
    promote.add_argument("--writer-ledger", type=Path, required=True)
    promote.add_argument("--input", type=Path, required=True)
    promote.add_argument("--output", type=Path, required=True)
    promote.add_argument(
        "--role",
        required=True,
        choices=["search_log", "limitations"],
    )
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--state", type=Path, required=True)
    freeze.add_argument("--writer-ledger", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--artifact", action="append", default=[], required=True)
    extend = sub.add_parser("extend-scores")
    extend.add_argument("--state", type=Path, required=True)
    extend.add_argument("--writer-ledger", type=Path, required=True)
    extend.add_argument("--manifest", type=Path, required=True)
    extend.add_argument("--artifact", action="append", default=[], required=True)
    check = sub.add_parser("verify-freeze")
    check.add_argument("--state", type=Path, required=True)
    check.add_argument("--writer-ledger", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--require-scoring", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "promote":
            result = promote_artifact(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                input_path=args.input,
                output_path=args.output,
                output_role=args.role,
            )
        elif args.command == "freeze":
            result = create_truth_freeze(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_path=args.output,
                artifacts=_parse_artifacts(args.artifact),
            )
        elif args.command == "extend-scores":
            result = extend_truth_freeze_with_scores(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                manifest_path=args.manifest,
                scoring_artifacts=_parse_artifacts(args.artifact),
            )
        else:
            result = verify_truth_freeze(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                manifest_path=args.manifest,
                require_scoring=args.require_scoring,
            )
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "valid", "result": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
