from __future__ import annotations

import argparse
import csv
import strict_json as json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from artifact_provenance import (
    create_delivery_provenance,
    initialize_writer_ledger,
    verify_artifact_writer,
    verify_delivery_provenance,
    verify_truth_freeze,
)
from detailed_run_log import (
    apply_log_append_guard,
    append_final_summary,
    append_state_event,
    assert_state_log_alignment,
    initial_log_text,
    initialize_log,
    make_log_append_guard,
    now_text as log_now_text,
    prepare_log_append_recovery,
    read_events,
    read_final_summary,
    render_event,
)
from process_lock import ProcessFileLock
from retrieval_controls import (
    CURRENT_EXECUTION_SCHEMA_VERSION,
    TARGET_CONFIDENCES,
    budget_values,
    load_retrieval_config,
    validate_round_metrics,
)
from formal_states import (
    AUDIT_COMPLETE_STATUSES,
    AUDIT_KNOWN_STATUSES,
    AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL,
    AUDIT_TERMINAL_STATUSES,
)
from runtime_guard import (
    TERMINAL_FAILURE_STATUSES,
    assert_post_audit_permitted,
    canonical_sha256,
    create_release_manifest,
    normalize_phase,
    sha256_file,
    state_binding_sha256,
    validate_phase_transition,
    verify_release_manifest,
)
from run_paths import portable_state_references, rebind_state_paths, resolve_run_path


ACTIVE_STATUSES = {
    "running",
    "auditing",
    "audit_passed",
    "needs_iteration",
    "ready_to_build",
    "validating",
    "validation_passed",
    "repair_required",
    "finalizing",
    "paused_by_host_limit",
}
AUDIT_STATUSES = set(AUDIT_KNOWN_STATUSES)
VALIDATION_STATUSES = set(AUDIT_TERMINAL_STATUSES)
MAX_VALIDATION_ROUNDS = 2
MAX_REPAIR_CYCLES = 1
MAX_PIPELINE_REBUILDS_AFTER_VALIDATION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_transaction_fault(stage: str) -> None:
    """No-op fault hook used by cross-process crash-recovery tests."""


def _finalization_transaction_fault(stage: str) -> None:
    """No-op fault hook used by final-delivery crash-recovery tests."""


def _transaction_path(path: Path) -> Path:
    return path.with_name(path.name + ".state-log-transaction.json")


def _finalization_path(path: Path) -> Path:
    return path.with_name(path.name + ".finalization-transaction.json")


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_parent(path)


def _raw_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not data.get("task_run_id"):
        raise ValueError("运行状态缺少 task_run_id")
    return rebind_state_paths(data, path)


def _transaction_checksum(payload: Mapping[str, object]) -> str:
    return canonical_sha256(
        {key: value for key, value in payload.items() if key != "transaction_sha256"}
    )


def _read_transaction(path: Path) -> dict[str, Any] | None:
    journal = _transaction_path(path)
    if not journal.is_file():
        return None
    try:
        payload = json.loads(journal.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("state_log_transaction_invalid: 事务日志无法读取，正式流程保持封闭") from exc
    if not isinstance(payload, dict) or payload.get("schema") not in {
        "state-log-transaction-1",
        "state-log-transaction-2",
    }:
        raise ValueError("state_log_transaction_invalid: 事务日志结构无效，正式流程保持封闭")
    if payload.get("transaction_sha256") != _transaction_checksum(payload):
        raise ValueError("state_log_transaction_invalid: 事务日志校验失败，正式流程保持封闭")
    return payload


def _remove_transaction(path: Path) -> None:
    journal = _transaction_path(path)
    if journal.exists():
        journal.unlink()
        _fsync_parent(journal)


def _apply_state_log_transaction_locked(path: Path, *, recovering: bool) -> dict[str, Any] | None:
    transaction = _read_transaction(path)
    if transaction is None:
        return _raw_state(path)
    new_state = transaction.get("new_state")
    event = transaction.get("event")
    if not isinstance(new_state, dict) or not isinstance(event, dict):
        raise ValueError("state_log_transaction_invalid: 事务缺少可恢复状态或事件")
    old_revision = int(transaction.get("expected_old_revision", -1))
    new_revision = int(transaction.get("new_revision", -1))
    transaction_id = str(transaction.get("transaction_id", ""))
    if not transaction_id or new_revision != old_revision + 1:
        raise ValueError("state_log_transaction_invalid: 事务修订边界无效")
    current = _raw_state(path)
    current_revision = int(current.get("state_revision", 0)) if current else 0
    if current_revision not in {old_revision, new_revision}:
        raise ValueError("state_log_transaction_conflict: 当前状态不在事务预期修订边界内")
    if current_revision == old_revision and old_revision > 0:
        if state_binding_sha256(current or {}) != transaction.get("expected_old_state_binding_sha256"):
            raise ValueError("state_log_transaction_conflict: 旧状态绑定与事务不一致")
    if current_revision == new_revision:
        if state_binding_sha256(current or {}) != transaction.get("new_state_binding_sha256"):
            raise ValueError("state_log_transaction_conflict: 新状态绑定与事务不一致")
    log_path = Path(str(transaction.get("log_path", "")))
    schema = str(transaction.get("schema", ""))
    guard = transaction.get("log_append_guard")
    append_pending = False
    if schema == "state-log-transaction-2":
        if not isinstance(guard, Mapping):
            raise ValueError("state_log_transaction_invalid: 事务缺少日志追加保护")
        append_state = prepare_log_append_recovery(log_path, guard)
        append_pending = append_state["status"] == "pending"
    events = read_events(log_path)
    log_revision = int(events[-1].get("state_revision", 0)) if events else 0
    if log_revision not in {old_revision, new_revision}:
        raise ValueError("state_log_transaction_conflict: 运行日志不在事务预期修订边界内")
    if log_revision == new_revision:
        latest = events[-1]
        if (
            latest.get("transaction_id") != transaction_id
            or latest.get("state_binding_sha256") != transaction.get("new_state_binding_sha256")
        ):
            raise ValueError("state_log_transaction_conflict: 新日志事件不属于待恢复事务")
    if current_revision == old_revision:
        write_state(path, new_state)
    _state_transaction_fault("recovery_after_state" if recovering else "after_state")
    if log_revision == old_revision:
        if schema == "state-log-transaction-2":
            if not append_pending:
                raise ValueError("state_log_transaction_conflict: 日志追加状态无法确定")
            apply_log_append_guard(log_path, guard)
        elif old_revision == 0:
            initialize_log(
                log_path,
                str(new_state["task_run_id"]),
                str(event.get("analysis_object", "")),
                state_revision=new_revision,
                state_binding=str(transaction["new_state_binding_sha256"]),
                transaction_id=transaction_id,
            )
        else:
            append_state_event(
                log_path,
                str(new_state["task_run_id"]),
                str(event["phase"]),
                str(event["action"]),
                str(event["status"]),
                state_revision=new_revision,
                state_binding=str(transaction["new_state_binding_sha256"]),
                transaction_id=transaction_id,
                **dict(event.get("details", {})),
            )
    _state_transaction_fault("recovery_after_log" if recovering else "after_log")
    restored = _raw_state(path)
    if restored is None or assert_state_log_alignment(log_path, restored)["status"] != "valid":
        raise ValueError("state_log_transaction_recovery_failed: 状态与日志恢复后仍不一致")
    _remove_transaction(path)
    return restored


def recover_state_log_transaction(path: Path) -> dict[str, Any] | None:
    """Recover one prepared state/log commit; repeated calls are idempotent."""

    with ProcessFileLock(path):
        return _apply_state_log_transaction_locked(path, recovering=True)


def _prepare_state_log_transaction_locked(
    path: Path,
    *,
    old_state: Mapping[str, object] | None,
    new_state: Mapping[str, object],
    event: Mapping[str, object],
) -> dict[str, Any]:
    old_revision = int(old_state.get("state_revision", 0)) if old_state else 0
    new_revision = int(new_state.get("state_revision", 0))
    transaction_id = uuid.uuid4().hex
    event_record: dict[str, object]
    if old_revision == 0:
        event_record = {
            "event_id": "LOG-000001",
            "timestamp": log_now_text(),
            "task_run_id": str(new_state["task_run_id"]),
            "phase": "INITIALIZE",
            "action": "初始化详细运行日志",
            "status": "running",
            "analysis_object": str(event.get("analysis_object", "")),
            "state_revision": new_revision,
            "state_binding_sha256": state_binding_sha256(new_state),
            "transaction_id": transaction_id,
        }
        append_text = initial_log_text(event_record)
    else:
        existing_events = read_events(Path(str(new_state["detailed_log"])))
        if not existing_events or int(existing_events[-1].get("state_revision", 0)) != old_revision:
            raise ValueError("state_log_transaction_conflict: 无法从当前日志准备下一修订事件")
        event_record = {
            "event_id": f"LOG-{new_revision:06d}",
            "timestamp": log_now_text(),
            "task_run_id": str(new_state["task_run_id"]),
            "phase": str(event["phase"]),
            "action": str(event["action"]),
            "status": str(event["status"]),
            "state_revision": new_revision,
            "state_binding_sha256": state_binding_sha256(new_state),
            "transaction_id": transaction_id,
            **dict(event.get("details", {})),
        }
        append_text = render_event(event_record)
    log_path = Path(str(new_state["detailed_log"]))
    transaction: dict[str, Any] = {
        "schema": "state-log-transaction-2",
        "transaction_id": transaction_id,
        "prepared_at": utc_now(),
        "state_path": str(path.resolve()),
        "log_path": str(Path(str(new_state["detailed_log"])).resolve()),
        "expected_old_revision": old_revision,
        "new_revision": new_revision,
        "expected_old_state_binding_sha256": state_binding_sha256(old_state or {}),
        "new_state_binding_sha256": state_binding_sha256(new_state),
        "new_state": dict(new_state),
        "event": dict(event),
        "event_record": event_record,
        "log_append_guard": make_log_append_guard(
            log_path,
            append_text,
            transaction_id=transaction_id,
        ),
    }
    transaction["transaction_sha256"] = _transaction_checksum(transaction)
    _atomic_json_write(_transaction_path(path), transaction)
    _state_transaction_fault("after_prepare")
    restored = _apply_state_log_transaction_locked(path, recovering=False)
    if restored is None:
        raise ValueError("state_log_transaction_failed: 提交后状态缺失")
    return restored


def load_state(path: Path) -> dict[str, Any]:
    from execution_coordinator import assert_no_pending
    assert_no_pending(path.resolve().parent)
    with ProcessFileLock(path):
        _apply_state_log_transaction_locked(path, recovering=True)
        _apply_finalization_transaction_locked(path, recovering=True)
    if not path.is_file():
        raise ValueError(f"运行状态文件不存在：{path}")
    data = _raw_state(path)
    if data is None:
        raise ValueError(f"运行状态文件不存在：{path}")
    return data


def write_state(path: Path, data: Mapping[str, object]) -> None:
    materialized = dict(data)
    materialized["path_references"] = portable_state_references(materialized, path)
    materialized["run_root_schema"] = "run-relative-1"
    _atomic_json_write(path, materialized)


def append_history(data: dict[str, Any], event: str) -> None:
    history = data.setdefault("history", [])
    history.append(
        {
            "at": utc_now(),
            "event": event,
            "state_revision": data.get("state_revision", 0),
            "status": data.get("status", ""),
            "phase": data.get("phase", ""),
            "unique_pages": data.get("unique_pages", 0),
            "effective_samples": data.get("effective_samples", 0),
            "iteration_round": data.get("iteration_round", 0),
            "dimensions_needing_iteration": data.get("dimensions_needing_iteration", 7),
            "next_action": data.get("next_action", ""),
        }
    )
    if len(history) > 200:
        data["history"] = history[-200:]


def _verify_release(data: Mapping[str, object]) -> None:
    verify_release_manifest(
        Path(str(data["skill_root"])),
        Path(str(data["release_manifest"])),
        task_run_id=str(data["task_run_id"]),
    )


def _ensure_mutable(data: Mapping[str, object]) -> None:
    status = str(data.get("status", ""))
    if status == "complete":
        raise ValueError("已完成运行不得恢复为非终态")
    if status in TERMINAL_FAILURE_STATUSES:
        raise ValueError(f"运行处于不可自动恢复的失败终态：{status}")


def _assert_current_log(data: Mapping[str, object]) -> None:
    result = assert_state_log_alignment(Path(str(data["detailed_log"])), data)
    if result["status"] != "valid":
        raise ValueError("state_log_divergence: " + "; ".join(result["errors"]))


def _commit_state(
    path: Path,
    data: dict[str, Any],
    *,
    event: str,
    action: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    with ProcessFileLock(path):
        _apply_state_log_transaction_locked(path, recovering=True)
        current = _raw_state(path)
        if current is None:
            raise ValueError("state_log_transaction_conflict: 正式状态在提交前缺失")
        if int(current.get("state_revision", 0)) != int(data.get("state_revision", 0)):
            raise ValueError("state_log_transaction_conflict: 状态已被其他进程更新，请重新读取后重试")
        data["state_revision"] = int(data.get("state_revision", 0)) + 1
        data["updated_at"] = utc_now()
        append_history(data, event)
        return _prepare_state_log_transaction_locked(
            path,
            old_state=current,
            new_state=data,
            event={
                "phase": str(data["phase"]),
                "action": action,
                "status": str(data["status"]),
                "details": dict(details or {}),
            },
        )


def initialize_state(
    path: Path,
    task_run_id: str,
    place: str,
    skill_root: Path,
    release_manifest: Path,
    detailed_log: Path,
    writer_ledger: Path | None = None,
    target_confidence: str | None = None,
) -> dict[str, Any]:
    if path.exists() or _transaction_path(path).exists():
        current = load_state(path)
        if current["task_run_id"] != task_run_id:
            raise ValueError("现有状态属于不同 task_run_id，不得覆盖")
        _verify_release(current)
        _assert_current_log(current)
        from execution_facts import anchor_state
        anchor_state(path)
        return current
    controls = load_retrieval_config()
    requested_target = target_confidence or str(controls["default_target_confidence"])
    if requested_target not in TARGET_CONFIDENCES:
        raise ValueError("target_confidence must be 中, 中高, or 高")
    create_release_manifest(skill_root, release_manifest, task_run_id)
    ledger_path = writer_ledger or path.with_name("approved-writer-ledger.jsonl")
    initialize_writer_ledger(ledger_path, task_run_id)
    data: dict[str, Any] = {
        "schema_revision": 6,
        "task_run_id": task_run_id,
        "execution_log_contract": {
            "schema_version": CURRENT_EXECUTION_SCHEMA_VERSION,
            "legacy_compatibility": False,
        },
        "place": place,
        "status": "running",
        "phase": "INITIALIZE",
        "state_revision": 1,
        "audit_status": "needs_iteration",
        "unique_pages": 0,
        "effective_samples": 0,
        "iteration_round": 0,
        "minimum_dimension_confidence": "中",
        "target_confidence": requested_target,
        "dimensions_needing_iteration": 7,
        "validation_rounds": 0,
        "repair_cycles": 0,
        "pipeline_rebuilds_after_validation": 0,
        "same_error_recurrence_max": 1,
        "last_validation_error_codes": [],
        "terminal_failure_code": "",
        "last_completed_action": "created_run_state",
        "next_action": "start_retrieval",
        "pause_reason": "",
        "artifacts": {},
        "skill_root": str(skill_root.resolve()),
        "release_manifest": str(release_manifest.resolve()),
        "approved_writer_ledger": str(ledger_path.resolve()),
        "detailed_log": str(detailed_log.resolve()),
        "created_at": utc_now(),
        "research_cutoff": utc_now()[:10],
        "updated_at": utc_now(),
        "history": [],
    }
    append_history(data, "init")
    with ProcessFileLock(path):
        if _raw_state(path) is not None or _read_transaction(path) is not None:
            raise ValueError("state_log_transaction_conflict: 初始化目标已被其他进程占用")
        initialized = _prepare_state_log_transaction_locked(
            path,
            old_state=None,
            new_state=data,
            event={
                "phase": "INITIALIZE",
                "action": "初始化详细运行日志",
                "status": "running",
                "analysis_object": place,
                "details": {},
            },
        )
        from execution_facts import anchor_state
        anchor_state(path)
        return initialized


def checkpoint_state(
    path: Path,
    *,
    phase: str,
    status: str = "running",
    last_action: str = "",
    next_action: str = "",
    preflight_path: Path | None = None,
    truth_freeze_path: Path | None = None,
    audit_status: str | None = None,
    unique_pages: int | None = None,
    effective_samples: int | None = None,
    iteration_round: int | None = None,
    dimensions_needing_iteration: int | None = None,
) -> dict[str, Any]:
    if any(
        value is not None
        for value in (
            audit_status,
            unique_pages,
            effective_samples,
            iteration_round,
            dimensions_needing_iteration,
        )
    ):
        raise ValueError(
            "审计状态、页面数、样本数、迭代轮次和维度缺口只能由正式审计 JSON 同步，"
            "不得通过 checkpoint 手工填写"
        )
    if status not in ACTIVE_STATUSES - {"paused_by_host_limit"}:
        raise ValueError(f"checkpoint 状态无效：{status}")
    data = load_state(path)
    _verify_release(data)
    _ensure_mutable(data)
    _assert_current_log(data)
    requested = normalize_phase(phase)
    current = normalize_phase(str(data.get("phase", "INITIALIZE")))
    if current == "EVIDENCE_AUDIT" and requested == "SEARCH":
        if data.get("audit_status") != "needs_iteration":
            raise ValueError("只有正式审计返回 needs_iteration 才能回到 SEARCH")
        requested_phase = "SEARCH"
        status = "needs_iteration"
        next_action = next_action or "continue_gap_retrieval"
    else:
        requested_phase = validate_phase_transition(current, requested)
    assert_post_audit_permitted(
        str(data.get("audit_status", "needs_iteration")),
        int(data.get("dimensions_needing_iteration", 7)),
        requested_phase,
    )
    if requested_phase == "EVIDENCE_AUDIT":
        status = "auditing"
        next_action = next_action or "run_formal_evidence_audit"
    if requested_phase == "SCORE":
        if truth_freeze_path is None:
            raise ValueError("进入 SCORE 必须提供已登记的真值冻结清单")
        verify_truth_freeze(
            state_path=path,
            writer_ledger_path=Path(str(data["approved_writer_ledger"])),
            manifest_path=truth_freeze_path,
        )
        data.setdefault("artifacts", {})["truth_freeze"] = str(truth_freeze_path.resolve())
    if requested_phase == "REPORT_BUILD":
        if preflight_path is None:
            raise ValueError("进入 REPORT_BUILD 必须提供正式 preflight 结果")
        writer = verify_artifact_writer(
            state_path=path,
            writer_ledger_path=Path(str(data["approved_writer_ledger"])),
            output_role="preflight_report",
            output_path=preflight_path,
        )
        payload = json.loads(preflight_path.read_text(encoding="utf-8-sig"))
        if payload.get("status") != "valid" or payload.get("task_run_id") != data["task_run_id"]:
            raise ValueError("preflight_invalid: 报告构建前全链预检未通过")
        truth_path = Path(str(data.get("artifacts", {}).get("truth_freeze", "")))
        verify_truth_freeze(
            state_path=path,
            writer_ledger_path=Path(str(data["approved_writer_ledger"])),
            manifest_path=truth_path,
            require_scoring=True,
        )
        data.setdefault("artifacts", {})["preflight"] = str(preflight_path.resolve())
        data["preflight_writer_event_sha256"] = writer["event_sha256"]
        status = "ready_to_build"
    if requested_phase == "VALIDATE":
        if int(data.get("pipeline_rebuilds_after_validation", 0)) > MAX_PIPELINE_REBUILDS_AFTER_VALIDATION:
            raise ValueError("validation_nonconvergent: 验收后整链重建次数已超限")
        status = "validating"
        next_action = next_action or "run_read_only_validation"
    data.update(
        {
            "status": status,
            "phase": requested_phase,
            "last_completed_action": last_action,
            "next_action": next_action,
            "pause_reason": "",
        }
    )
    return _commit_state(
        path,
        data,
        event="checkpoint",
        action=last_action or "保存正式运行检查点",
        details={"next_action": data["next_action"]},
    )


def resume_collection(path: Path) -> dict[str, Any]:
    """Return a partial intake batch to collection, never bypass a failed audit."""
    data = load_state(path)
    _verify_release(data)
    _ensure_mutable(data)
    _assert_current_log(data)
    if (data.get('phase') != 'EVIDENCE_BUILD' or data.get('audit_status') != 'needs_iteration'
            or data.get('artifacts', {}).get('truth_freeze')
            or data.get('validation_rounds', 0)):
        raise ValueError('collection_resume_not_permitted')
    data.update(phase='SEARCH', status='needs_iteration',
                last_completed_action='提交采集批次后继续既有计划', next_action='continue_collection')
    return _commit_state(path, data, event='checkpoint', action=data['last_completed_action'],
                         details={'next_action': data['next_action']})


def begin_audit_repair(path: Path, audit_path: Path) -> dict[str, Any]:
    """Reopen only the existing derived-count writer after a verified audit."""
    from runtime_dispatch import classify_audit_failure, digest
    data=load_state(path)
    _verify_release(data)
    _ensure_mutable(data)
    _assert_current_log(data)
    if data.get('phase')!='EVIDENCE_AUDIT' or data.get('artifacts',{}).get('truth_freeze'):
        raise ValueError('audit_repair_not_permitted')
    verify_artifact_writer(state_path=path,writer_ledger_path=Path(str(data['approved_writer_ledger'])),
        output_role='evidence_audit',output_path=audit_path)
    audit=json.loads(audit_path.read_text(encoding='utf-8-sig'))
    if audit.get('status')!='invalid' or classify_audit_failure(audit)!=('repairable','refresh_scored_counts'):
        raise ValueError('audit_repair_not_reconstructible')
    fingerprint=digest(sorted(audit['errors']))
    attempts=int(data.get('audit_repair_attempt',0))+1 if data.get('last_audit_failure_fingerprint')==fingerprint else 1
    from runtime_dispatch import config
    if attempts>config()['local_repair_attempts']:raise ValueError('audit_repair_no_progress')
    data.update(phase='SEMANTIC_QUANTIFICATION',status='repair_required',
        audit_status='invalid',audit_repair_required=True,audit_repair_action='refresh_scored_counts',
        audit_repair_attempt=attempts,last_audit_failure_fingerprint=fingerprint,
        next_action='refresh_scored_counts_then_reaudit')
    return _commit_state(path,data,event='audit_repair',action='按已登记真实输入重建派生计数',
                         details={'failure_fingerprint':fingerprint,'attempt':attempts})


def sync_audit_state(path: Path, audit_path: Path) -> dict[str, Any]:
    data = load_state(path)
    _verify_release(data)
    _ensure_mutable(data)
    _assert_current_log(data)
    if normalize_phase(str(data.get("phase", ""))) != "EVIDENCE_AUDIT":
        raise ValueError("只能在 EVIDENCE_AUDIT 阶段同步正式审计")
    verify_artifact_writer(
        state_path=path,
        writer_ledger_path=Path(str(data["approved_writer_ledger"])),
        output_role="evidence_audit",
        output_path=audit_path,
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("status") not in AUDIT_STATUSES:
        raise ValueError("正式审计 JSON 状态无效")
    summary = payload.get("summary")
    if not isinstance(summary, dict) or summary.get("task_run_id") != data["task_run_id"]:
        raise ValueError("正式审计 JSON 与 task_run_id 不一致")
    status = str(payload["status"])
    dimensions = int(summary.get("dimensions_needing_iteration_count", 7))
    unique_pages = int(summary.get("deduplicated_relevant_searched_pages", 0))
    effective_samples = int(summary.get("scored_direct_user_units", 0))
    deep_rounds = summary.get("deep_iteration_round_numbers", [])
    if not isinstance(deep_rounds, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in deep_rounds
    ):
        raise ValueError("正式审计 JSON 的深检轮次结构无效")
    if deep_rounds:
        retrieval = summary.get("retrieval_termination", {})
        metrics = retrieval.get("metrics", {}) if isinstance(retrieval, dict) else {}
        remaining = retrieval.get("remaining_dimensions", []) if isinstance(retrieval, dict) else []
        if not isinstance(metrics, dict):
            raise ValueError("状态恢复无法验证深检轮次：正式审计缺少执行指标")
        controls = load_retrieval_config()
        maximum_rounds = budget_values(controls)["maximum_iteration_rounds"]
        round_validation = validate_round_metrics(
            metrics,
            config=controls,
            required_dimensions=remaining if isinstance(remaining, list) else [],
            expected_round_count=min(max(deep_rounds), maximum_rounds),
        )
        if round_validation["status"] != "valid" or round_validation["round_numbers"] != deep_rounds:
            raise ValueError(
                "状态恢复拒绝不连续或未完成的深检轮次："
                + json.dumps(round_validation, ensure_ascii=False, separators=(",", ":"))
            )
    requested_target = str(data.get("target_confidence", ""))
    if requested_target not in TARGET_CONFIDENCES:
        raise ValueError("运行状态缺少合法的目标置信度绑定")
    dimension_audit = summary.get("dimension_evidence")
    if not isinstance(dimension_audit, dict):
        raise ValueError("正式审计 JSON 缺少机器维度审计")
    if (
        str(summary.get("target_confidence", "")) != requested_target
        or str(dimension_audit.get("target_confidence", "")) != requested_target
    ):
        raise ValueError("正式审计 JSON 的目标置信度与运行状态不一致")
    iteration = max(
        [int(value) for value in deep_rounds if isinstance(value, int)]
        or [int(data.get("iteration_round", 0))]
    )
    data.update(
        {
            "audit_status": status,
            "audit_repair_required": status == "invalid",
            "unique_pages": unique_pages,
            "effective_samples": effective_samples,
            "iteration_round": iteration,
            "dimensions_needing_iteration": dimensions,
            "audit_summary": summary,
            "last_completed_action": "synchronized_machine_evidence_audit",
            "status": (
                "repair_required" if status == "invalid"
                else "needs_iteration" if status == "needs_iteration"
                else "audit_passed"
            ),
            "next_action": (
                "resolve_audit_errors_then_reaudit"
                if status == "invalid"
                else "continue_gap_retrieval"
                if status == "needs_iteration"
                else "freeze_restricted_truth_then_score_eligible_dimensions"
                if status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
                else "freeze_truth_then_score"
            ),
        }
    )
    data.setdefault("artifacts", {})["evidence_audit"] = str(audit_path.resolve())
    return _commit_state(
        path,
        data,
        event="audit_sync",
        action="同步机器证据审计",
        details={
            "audit_status": status,
            "unique_pages": unique_pages,
            "effective_samples": effective_samples,
            "dimensions_needing_iteration": dimensions,
        },
    )


def pause_state(path: Path, reason: str, next_action: str) -> dict[str, Any]:
    data = load_state(path)
    _verify_release(data)
    _ensure_mutable(data)
    _assert_current_log(data)
    data.update(
        {
            "status": "paused_by_host_limit",
            "pause_reason": reason,
            "next_action": next_action,
        }
    )
    return _commit_state(
        path,
        data,
        event="host_pause",
        action="宿主限制暂停",
        details={"reason": reason, "next_action": next_action},
    )


def read_validation(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"验收文件不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("验收文件格式无效")
    return data


def _error_codes(manifest_path: Path) -> list[str]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("结构化验收错误清单格式无效")
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("结构化验收错误清单 errors 必须为数组")
    return sorted(
        {
            str(item.get("error_code", "unknown_validation_error"))
            for item in errors
            if isinstance(item, dict)
        }
    )


def mark_terminal_failure(
    path: Path,
    code: str,
    reason: str,
    *,
    state_updates: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    if code not in TERMINAL_FAILURE_STATUSES:
        raise ValueError(f"未知正式失败状态：{code}")
    data = load_state(path)
    _verify_release(data)
    if data.get("status") == "complete":
        raise ValueError("已完成运行不得改写为失败终态")
    _assert_current_log(data)
    data.update(dict(state_updates or {}))
    data.update(
        {
            "status": code,
            "terminal_failure_code": code,
            "failure_reason": reason,
            "last_completed_action": "terminal_failure_recorded",
            "next_action": "start_new_independent_run_after_root_cause_fix",
        }
    )
    return _commit_state(
        path,
        data,
        event="terminal_failure",
        action="记录不可自动恢复的正式失败",
        details={"failure_code": code, "reason": reason},
    )


def record_validation_result(
    path: Path,
    validation_path: Path,
    error_manifest_path: Path,
) -> dict[str, Any]:
    data = load_state(path)
    _verify_release(data)
    if data.get("status") == "complete":
        completed = assert_finalizable(path)
        expected_paths = {
            "docx": docx,
            "xlsx": xlsx,
            "rules_xlsx": rules_xlsx,
            "validation": validation,
            "detailed_log": detailed_log,
        }
        artifacts = completed.get("artifacts", {})
        if any(
            Path(str(artifacts.get(role, ""))).resolve() != artifact.resolve()
            for role, artifact in expected_paths.items()
        ):
            raise ValueError("已完成最终化事务的成果路径与本次请求不一致")
        return completed
    _ensure_mutable(data)
    _assert_current_log(data)
    if normalize_phase(str(data.get("phase", ""))) != "VALIDATE":
        raise ValueError("只能在 VALIDATE 阶段登记正式验收结果")
    ledger = Path(str(data["approved_writer_ledger"]))
    verify_artifact_writer(
        state_path=path,
        writer_ledger_path=ledger,
        output_role="validation_report",
        output_path=validation_path,
    )
    verify_artifact_writer(
        state_path=path,
        writer_ledger_path=ledger,
        output_role="validation_error_manifest",
        output_path=error_manifest_path,
    )
    truth_path = Path(str(data.get("artifacts", {}).get("truth_freeze", "")))
    try:
        freeze = verify_truth_freeze(
            state_path=path,
            writer_ledger_path=ledger,
            manifest_path=truth_path,
            require_scoring=True,
        )
    except Exception as exc:
        return mark_terminal_failure(path, "post_validate_upstream_mutation", str(exc))
    freeze_hash = str(freeze.get("freeze_sha256", ""))
    baseline = str(data.get("validation_truth_freeze_sha256", ""))
    if baseline and baseline != freeze_hash:
        return mark_terminal_failure(
            path,
            "post_validate_upstream_mutation",
            "正式验收后上游真值冻结哈希发生变化",
        )
    validation = read_validation(validation_path)
    codes = _error_codes(error_manifest_path)
    rounds = int(data.get("validation_rounds", 0)) + 1
    if rounds > MAX_VALIDATION_ROUNDS:
        return mark_terminal_failure(
            path,
            "validation_nonconvergent",
            "正式验收轮次超过上限",
            state_updates={"validation_rounds": rounds},
        )
    data["validation_rounds"] = rounds
    data["validation_truth_freeze_sha256"] = freeze_hash
    data["last_validation_error_codes"] = codes
    data.setdefault("artifacts", {})["validation"] = str(validation_path.resolve())
    data.setdefault("artifacts", {})["validation_error_manifest"] = str(error_manifest_path.resolve())
    valid = (
        validation.get("status") in VALIDATION_STATUSES
        and not validation.get("errors")
        and int(validation.get("error_count", 0) or 0) == 0
        and not codes
    )
    if valid:
        data.update(
            {
                "status": "validation_passed",
                "validation_status": validation["status"],
                "last_completed_action": "read_only_validation_passed",
                "next_action": "finish_and_seal_delivery",
            }
        )
        return _commit_state(
            path,
            data,
            event="validation_result",
            action="登记只读验收通过",
            details={"validation_round": rounds, "error_count": 0},
        )
    if rounds >= MAX_VALIDATION_ROUNDS:
        return mark_terminal_failure(
            path,
            "validation_nonconvergent",
            "第二次正式验收仍未通过，禁止第三次自动修复或验收",
            state_updates={
                "validation_rounds": rounds,
                "validation_truth_freeze_sha256": freeze_hash,
                "last_validation_error_codes": codes,
            },
        )
    if int(data.get("repair_cycles", 0)) >= MAX_REPAIR_CYCLES:
        return mark_terminal_failure(path, "validation_nonconvergent", "正式修复周期已用尽")
    data.update(
        {
            "phase": "REPORT_BUILD",
            "status": "repair_required",
            "repair_cycles": 1,
            "pipeline_rebuilds_after_validation": 1,
            "last_completed_action": "validation_failed_root_causes_recorded",
            "next_action": "rebuild_downstream_from_frozen_score",
        }
    )
    return _commit_state(
        path,
        data,
        event="validation_result",
        action="登记一次受限下游重建",
        details={"validation_round": rounds, "error_codes": codes},
    )


def _csv_count(path: Path, id_field: str) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return len(
            {
                str(row.get(id_field, "")).strip()
                for row in csv.DictReader(handle)
                if str(row.get(id_field, "")).strip()
            }
        )


def _final_log_summary(
    data: Mapping[str, object],
    truth_freeze: Mapping[str, object],
    run_root: Path,
) -> dict[str, object]:
    upstream = truth_freeze["upstream_artifacts"]
    scoring = truth_freeze["scoring_artifacts"]
    assert isinstance(upstream, dict) and isinstance(scoring, dict)
    source_path = resolve_run_path(upstream["source_ledger"]["path"], run_root)
    raw_path = resolve_run_path(upstream["raw_evidence"]["path"], run_root)
    semantic_path = resolve_run_path(upstream["semantic_evidence"]["path"], run_root)
    score_path = resolve_run_path(scoring["scoring_output"]["path"], run_root)
    score_data = json.loads(score_path.read_text(encoding="utf-8-sig"))
    audit_summary = data.get("audit_summary", {})
    return {
        "source_ledger_rows": _csv_count(source_path, "source_id"),
        "unique_source_urls": int(data.get("unique_pages", 0)),
        "raw_evidence_units": _csv_count(raw_path, "evidence_id"),
        "semantic_evidence_units": _csv_count(semantic_path, "evidence_id"),
        "eligible_evidence_units": int(
            audit_summary.get("eligible_direct_user_units", 0)
            if isinstance(audit_summary, dict)
            else 0
        ),
        "scored_evidence_units": int(data.get("effective_samples", 0)),
        "audit_status": str(data.get("audit_status", "")),
        "formal_score_status": str(score_data.get("status", "")),
    }


def _finalization_checksum(payload: Mapping[str, object]) -> str:
    return canonical_sha256(
        {key: value for key, value in payload.items() if key != "transaction_sha256"}
    )


def _read_finalization_transaction(path: Path) -> dict[str, Any] | None:
    journal = _finalization_path(path)
    if not journal.is_file():
        return None
    try:
        payload = json.loads(journal.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("finalization_transaction_invalid: 最终化事务无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "finalization-transaction-1":
        raise ValueError("finalization_transaction_invalid: 最终化事务结构无效")
    if payload.get("transaction_sha256") != _finalization_checksum(payload):
        raise ValueError("finalization_transaction_invalid: 最终化事务校验失败")
    return payload


def _write_finalization_transaction(path: Path, payload: dict[str, Any], stage: str) -> None:
    payload["stage"] = stage
    payload["updated_at"] = utc_now()
    payload["transaction_sha256"] = _finalization_checksum(payload)
    _atomic_json_write(_finalization_path(path), payload)


def _remove_finalization_transaction(path: Path) -> None:
    journal = _finalization_path(path)
    if journal.exists():
        journal.unlink()
        _fsync_parent(journal)


def _monotonic_finalization_timestamp(
    detailed_log: Path,
    *prepared_values: object,
) -> str:
    """Return a timezone-aware timestamp no earlier than the committed log tail."""

    candidates = [str(value) for value in prepared_values if str(value)]
    events = read_events(detailed_log)
    if events:
        candidates.append(str(events[-1].get("timestamp", "")))
    candidates.append(log_now_text())

    parsed: list[tuple[datetime, str]] = []
    for value in candidates:
        try:
            timestamp = __import__('temporal_fields').timestamp(value)
        except ValueError as exc:
            raise ValueError(
                "finalization_transaction_invalid: 最终化时间戳格式无效"
            ) from exc
        if timestamp.utcoffset() is None:
            raise ValueError("finalization_transaction_invalid: 最终化时间戳缺少时区")
        if timestamp > datetime.now(timestamp.tzinfo):
            raise ValueError("finalization_transaction_invalid: 最终化依据时间不能在未来")
        parsed.append((timestamp, value))
    return max(parsed, key=lambda item: item[0])[1]


def _verify_finalization_inputs(transaction: Mapping[str, object]) -> dict[str, Path]:
    paths = transaction.get("paths")
    hashes = transaction.get("input_sha256")
    if not isinstance(paths, Mapping) or not isinstance(hashes, Mapping):
        raise ValueError("finalization_transaction_invalid: 最终化事务缺少工件合同")
    resolved = {str(role): Path(str(value)) for role, value in paths.items()}
    for role, expected in hashes.items():
        artifact = resolved.get(str(role))
        if artifact is None or not artifact.is_file() or artifact.stat().st_size <= 0:
            raise ValueError(f"finalization_artifact_missing: {role}")
        if sha256_file(artifact) != expected:
            raise ValueError(f"finalization_artifact_changed: {role}")
    return resolved


def _atomic_final_state_write(path: Path, state: Mapping[str, object]) -> None:
    materialized = dict(state)
    materialized["path_references"] = portable_state_references(materialized, path)
    materialized["run_root_schema"] = "run-relative-1"
    temporary = path.with_name(path.name + ".final-state.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(materialized, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _finalization_transaction_fault("during_final_state_temp_write")
    os.replace(temporary, path)
    _fsync_parent(path)
    _finalization_transaction_fault("after_final_state_replace")


def _verify_completed_finalization(
    path: Path,
    transaction: Mapping[str, object],
    state: Mapping[str, object],
) -> None:
    transaction_id = str(transaction.get("transaction_id", ""))
    if (
        state.get("status") != "complete"
        or normalize_phase(str(state.get("phase", ""))) != "DELIVER"
        or state.get("finalization_transaction_id") != transaction_id
    ):
        raise ValueError("finalization_recovery_failed: 正式状态尚未完成同一最终化事务")
    paths = _verify_finalization_inputs(transaction)
    detailed_log = paths["detailed_log"]
    alignment = assert_state_log_alignment(detailed_log, state)
    if alignment["status"] != "valid":
        raise ValueError("finalization_recovery_failed: 状态与最终日志不一致")
    events = read_events(detailed_log)
    if not events or events[-1].get("transaction_id") != transaction_id:
        raise ValueError("finalization_recovery_failed: 最终事件缺少事务绑定")
    summary = read_final_summary(detailed_log)
    if not isinstance(summary, dict) or summary.get("finalization_transaction_id") != transaction_id:
        raise ValueError("finalization_recovery_failed: 最终摘要缺少事务绑定")
    seal = verify_delivery_provenance(
        state_path=path,
        writer_ledger_path=paths["writer_ledger"],
        seal_path=paths["delivery_provenance"],
    )
    if seal.get("finalization_transaction_id") != transaction_id:
        raise ValueError("finalization_recovery_failed: 交付封印缺少事务绑定")
    if state.get("delivery_provenance_sha256") != seal.get("seal_sha256"):
        raise ValueError("finalization_recovery_failed: 正式状态与交付封印哈希不一致")
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("finalization_recovery_failed: 正式状态缺少成果登记")
    for role in (
        "docx",
        "xlsx",
        "rules_xlsx",
        "detailed_log",
        "validation",
        "validation_error_manifest",
        "delivery_provenance",
    ):
        if Path(str(artifacts.get(role, ""))).resolve() != paths[role].resolve():
            raise ValueError(f"finalization_recovery_failed: 成果路径不一致：{role}")


def _apply_finalization_transaction_locked(
    path: Path,
    *,
    recovering: bool,
) -> dict[str, Any] | None:
    transaction = _read_finalization_transaction(path)
    if transaction is None:
        return _raw_state(path)
    paths = _verify_finalization_inputs(transaction)
    transaction_id = str(transaction.get("transaction_id", ""))
    if not transaction_id:
        raise ValueError("finalization_transaction_invalid: 缺少最终化事务编号")
    current = _raw_state(path)
    if current is None:
        raise ValueError("finalization_transaction_invalid: 正式状态不存在")
    if current.get("status") == "complete":
        _verify_completed_finalization(path, transaction, current)
        _remove_finalization_transaction(path)
        return dict(current)

    if current.get("status") == "validation_passed":
        if (
            int(current.get("state_revision", 0)) != int(transaction["expected_state_revision"])
            or state_binding_sha256(current) != transaction["expected_state_binding_sha256"]
        ):
            raise ValueError("finalization_transaction_conflict: 起始状态已改变")
        finalizing = dict(current)
        finalizing.update(
            {
                "state_revision": int(current.get("state_revision", 0)) + 1,
                "status": "finalizing",
                "phase": "DELIVER",
                "last_completed_action": "finalization_started",
                "next_action": "recover_finalization_transaction",
                "pause_reason": "",
                "finalization_transaction_id": transaction_id,
                "finalization_stage": "finalizing",
            }
        )
        append_history(finalizing, "finalization_started")
        current = _prepare_state_log_transaction_locked(
            path,
            old_state=current,
            new_state=finalizing,
            event={
                "phase": "DELIVER",
                "action": "启动可恢复正式交付",
                "status": "finalizing",
                "details": {"finalization_transaction_id": transaction_id},
            },
        )
        transaction["finalizing_state_revision"] = current["state_revision"]
        _write_finalization_transaction(path, transaction, "finalizing_state_committed")
        _finalization_transaction_fault(
            "recovery_after_finalizing_state" if recovering else "after_finalizing_state"
        )
    elif (
        current.get("status") != "finalizing"
        or current.get("finalization_transaction_id") != transaction_id
    ):
        raise ValueError("finalization_transaction_conflict: 当前状态不属于待恢复最终化事务")

    final_state = transaction.get("final_state")
    final_event_guard = transaction.get("final_event_guard")
    if not isinstance(final_state, dict) or not isinstance(final_event_guard, Mapping):
        transaction["log_event_timestamp"] = _monotonic_finalization_timestamp(
            paths["detailed_log"],
            transaction.get("log_event_timestamp", ""),
        )
        transaction["summary_timestamp"] = _monotonic_finalization_timestamp(
            paths["detailed_log"],
            transaction.get("summary_timestamp", ""),
            transaction["log_event_timestamp"],
        )
        final_state = dict(current)
        final_state.update(
            {
                "state_revision": int(current.get("state_revision", 0)) + 1,
                "status": "complete",
                "phase": "DELIVER",
                "last_completed_action": "joint_validation_passed",
                "next_action": "none",
                "pause_reason": "",
                "validation_status": transaction["validation_status"],
                "completed_at": transaction["completed_at"],
                "finalization_transaction_id": transaction_id,
                "finalization_stage": "complete",
            }
        )
        append_history(final_state, "finish")
        event = {
            "event_id": f"LOG-{int(final_state['state_revision']):06d}",
            "timestamp": str(transaction["log_event_timestamp"]),
            "task_run_id": str(final_state["task_run_id"]),
            "phase": "DELIVER",
            "action": "完成正式交付",
            "status": "complete",
            "state_revision": int(final_state["state_revision"]),
            "state_binding_sha256": state_binding_sha256(final_state),
            "transaction_id": transaction_id,
            "finalization_transaction_id": transaction_id,
            "validation_status": transaction["validation_status"],
        }
        final_event_guard = make_log_append_guard(
            paths["detailed_log"],
            render_event(event),
            transaction_id=transaction_id,
        )
        transaction["final_state"] = final_state
        transaction["final_event"] = event
        transaction["final_event_guard"] = final_event_guard
        _write_finalization_transaction(path, transaction, "final_event_prepared")

    prepare_log_append_recovery(paths["detailed_log"], final_event_guard)
    apply_log_append_guard(paths["detailed_log"], final_event_guard)
    _write_finalization_transaction(path, transaction, "final_event_written")
    _finalization_transaction_fault(
        "recovery_after_final_event" if recovering else "after_final_event"
    )

    truth = verify_truth_freeze(
        state_path=path,
        writer_ledger_path=paths["writer_ledger"],
        manifest_path=paths["truth_freeze"],
        require_scoring=True,
    )
    summary_values = _final_log_summary(final_state, truth, path.parent)
    append_final_summary(
        paths["detailed_log"],
        str(final_state["task_run_id"]),
        summary_values,
        transaction_id=transaction_id,
        timestamp=str(transaction["summary_timestamp"]),
    )
    _write_finalization_transaction(path, transaction, "final_summary_written")
    _finalization_transaction_fault(
        "recovery_after_final_summary" if recovering else "after_final_summary"
    )

    seal = create_delivery_provenance(
        state_path=path,
        writer_ledger_path=paths["writer_ledger"],
        truth_freeze_path=paths["truth_freeze"],
        output_path=paths["delivery_provenance"],
        artifacts={
            "docx": paths["docx"],
            "xlsx": paths["xlsx"],
            "rules_xlsx": paths["rules_xlsx"],
            "validation": paths["validation"],
            "validation_error_manifest": paths["validation_error_manifest"],
            "detailed_log": paths["detailed_log"],
        },
        finalization_transaction_id=transaction_id,
        created_at=str(transaction["seal_created_at"]),
    )
    _write_finalization_transaction(path, transaction, "delivery_seal_written")
    _finalization_transaction_fault(
        "recovery_after_delivery_seal" if recovering else "after_delivery_seal"
    )

    final_state["artifacts"] = {
        **dict(final_state.get("artifacts", {})),
        "docx": str(paths["docx"].resolve()),
        "xlsx": str(paths["xlsx"].resolve()),
        "rules_xlsx": str(paths["rules_xlsx"].resolve()),
        "detailed_log": str(paths["detailed_log"].resolve()),
        "validation": str(paths["validation"].resolve()),
        "validation_error_manifest": str(paths["validation_error_manifest"].resolve()),
        "delivery_provenance": str(paths["delivery_provenance"].resolve()),
    }
    final_state["delivery_provenance_sha256"] = seal["seal_sha256"]
    transaction["final_state"] = final_state
    transaction["delivery_provenance_sha256"] = seal["seal_sha256"]
    _write_finalization_transaction(path, transaction, "deliverables_registered")
    _finalization_transaction_fault(
        "recovery_after_deliverables_registered" if recovering else "after_deliverables_registered"
    )

    current = _raw_state(path)
    if current is None:
        raise ValueError("finalization_recovery_failed: 正式状态在最终提交前缺失")
    if current.get("status") != "complete":
        if (
            current.get("status") != "finalizing"
            or current.get("finalization_transaction_id") != transaction_id
        ):
            raise ValueError("finalization_transaction_conflict: 最终状态提交前发生冲突")
        _atomic_final_state_write(path, final_state)
    completed = _raw_state(path)
    if completed is None:
        raise ValueError("finalization_recovery_failed: 最终状态提交失败")
    _write_finalization_transaction(path, transaction, "final_state_committed")
    _verify_completed_finalization(path, transaction, completed)
    _remove_finalization_transaction(path)
    return completed


def recover_finalization_transaction(path: Path) -> dict[str, Any] | None:
    """Recover one prepared final delivery; repeated calls are idempotent."""

    with ProcessFileLock(path):
        _apply_state_log_transaction_locked(path, recovering=True)
        return _apply_finalization_transaction_locked(path, recovering=True)


def _prepare_finalization_transaction(
    path: Path,
    *,
    expected_state: Mapping[str, object],
    paths: Mapping[str, Path],
    validation_status: str,
) -> dict[str, Any]:
    with ProcessFileLock(path):
        _apply_state_log_transaction_locked(path, recovering=True)
        existing = _read_finalization_transaction(path)
        if existing is not None:
            restored = _apply_finalization_transaction_locked(path, recovering=True)
            if restored is None:
                raise ValueError("finalization_recovery_failed: 恢复后状态缺失")
            return restored
        current = _raw_state(path)
        if current is None or (
            int(current.get("state_revision", 0)) != int(expected_state.get("state_revision", -1))
            or state_binding_sha256(current) != state_binding_sha256(expected_state)
        ):
            raise ValueError("finalization_transaction_conflict: 验收后状态已改变")
        transaction_id = uuid.uuid4().hex
        immutable_roles = {
            role: artifact
            for role, artifact in paths.items()
            if role not in {"detailed_log", "delivery_provenance", "writer_ledger"}
        }
        transaction: dict[str, Any] = {
            "schema": "finalization-transaction-1",
            "transaction_id": transaction_id,
            "prepared_at": utc_now(),
            "updated_at": utc_now(),
            "stage": "prepared",
            "state_path": str(path.resolve()),
            "expected_state_revision": int(current.get("state_revision", 0)),
            "expected_state_binding_sha256": state_binding_sha256(current),
            "validation_status": validation_status,
            "completed_at": utc_now(),
            "log_event_timestamp": log_now_text(),
            "summary_timestamp": log_now_text(),
            "seal_created_at": utc_now(),
            "paths": {role: str(artifact.resolve()) for role, artifact in paths.items()},
            "input_sha256": {
                role: sha256_file(artifact) for role, artifact in immutable_roles.items()
            },
        }
        transaction["transaction_sha256"] = _finalization_checksum(transaction)
        _atomic_json_write(_finalization_path(path), transaction)
        _finalization_transaction_fault("after_finalization_prepare")
        restored = _apply_finalization_transaction_locked(path, recovering=False)
        if restored is None:
            raise ValueError("finalization_recovery_failed: 最终化后状态缺失")
        return restored


def finish_state(
    path: Path,
    docx: Path,
    xlsx: Path,
    rules_xlsx: Path,
    validation: Path,
    detailed_log: Path,
    validation_error_manifest: Path | None = None,
    delivery_provenance: Path | None = None,
) -> dict[str, Any]:
    data = load_state(path)
    _verify_release(data)
    _ensure_mutable(data)
    _assert_current_log(data)
    if normalize_phase(str(data.get("phase", ""))) != "VALIDATE":
        raise ValueError("任务必须完成正式验收阶段后才能交付")
    if data.get("status") != "validation_passed":
        raise ValueError("正式只读验收尚未通过")
    if detailed_log.resolve() != Path(str(data["detailed_log"])).resolve():
        raise ValueError("交付的详细运行日志与本次运行状态不一致")
    if int(data.get("validation_rounds", 0)) > MAX_VALIDATION_ROUNDS:
        raise ValueError("正式验收轮次超过允许上限")
    if int(data.get("repair_cycles", 0)) > MAX_REPAIR_CYCLES:
        raise ValueError("正式修复周期超过允许上限")
    if int(data.get("pipeline_rebuilds_after_validation", 0)) > MAX_PIPELINE_REBUILDS_AFTER_VALIDATION:
        raise ValueError("验收后整链重建次数超过允许上限")
    audit_status = str(data.get("audit_status", ""))
    restricted_terminal = audit_status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
    if int(data.get("unique_pages", 0)) < 150 and not restricted_terminal:
        raise ValueError("去重相关网页未达到正式门槛")
    if int(data.get("dimensions_needing_iteration", 7)) > 0 and not restricted_terminal:
        raise ValueError("仍有分析维度未通过正式审计")
    samples = int(data.get("effective_samples", 0))
    if restricted_terminal:
        audit_summary = data.get("audit_summary", {})
        retrieval_state = (
            audit_summary.get("retrieval_termination", {})
            if isinstance(audit_summary, dict)
            else {}
        )
        if (
            not isinstance(retrieval_state, dict)
            or retrieval_state.get("terminal") is not True
            or retrieval_state.get("target_met") is True
            or retrieval_state.get("restricted_delivery_allowed") is not True
            or not isinstance(retrieval_state.get("remaining_gaps"), dict)
        ):
            raise ValueError("受限交付缺少可复核的检索终止与剩余缺口")
    elif samples >= 100 and audit_status not in {"valid", "valid_with_dimension_shortfall"}:
        raise ValueError("样本达标但审计状态不是可交付终态")
    if not restricted_terminal and samples < 100 and (
        int(data.get("iteration_round", 0)) < 3
        or audit_status not in {
            "valid_with_sample_shortfall",
            "valid_with_sample_and_dimension_shortfall",
        }
    ):
        raise ValueError("样本不足且未满足正式深度迭代与穷尽条件")
    manifest_path = validation_error_manifest or Path(
        str(data.get("artifacts", {}).get("validation_error_manifest", ""))
    )
    if not manifest_path.is_file():
        raise ValueError("缺少结构化验收错误清单")
    ledger = Path(str(data["approved_writer_ledger"]))
    artifact_roles = {
        docx: "docx_report",
        xlsx: "research_workbook",
        rules_xlsx: "rules_workbook",
        validation: "validation_report",
        manifest_path: "validation_error_manifest",
    }
    for artifact, role in artifact_roles.items():
        verify_artifact_writer(
            state_path=path,
            writer_ledger_path=ledger,
            output_role=role,
            output_path=artifact,
        )
    validation_data = read_validation(validation)
    if (
        validation_data.get("status") not in VALIDATION_STATUSES
        or validation_data.get("errors")
        or int(validation_data.get("error_count", 0) or 0) != 0
        or _error_codes(manifest_path)
    ):
        raise ValueError("正式验收仍包含错误")
    truth_path = Path(str(data.get("artifacts", {}).get("truth_freeze", "")))
    truth = verify_truth_freeze(
        state_path=path,
        writer_ledger_path=ledger,
        manifest_path=truth_path,
        require_scoring=True,
    )
    if read_final_summary(detailed_log) is not None:
        raise ValueError("详细运行日志最终摘要只能由正式 finish 生成一次")
    seal_path = delivery_provenance or path.with_name("formal_delivery_provenance.json")
    return _prepare_finalization_transaction(
        path,
        expected_state=data,
        validation_status=str(validation_data["status"]),
        paths={
            "docx": docx,
            "xlsx": xlsx,
            "rules_xlsx": rules_xlsx,
            "validation": validation,
            "validation_error_manifest": manifest_path,
            "detailed_log": detailed_log,
            "delivery_provenance": seal_path,
            "truth_freeze": truth_path,
            "writer_ledger": ledger,
        },
    )


def assert_finalizable(path: Path) -> dict[str, Any]:
    data = load_state(path)
    if data.get("status") != "complete" or data.get("phase") != "DELIVER":
        raise ValueError(
            "任务尚未完成；不得发送完成式最终回复。"
            f"当前状态={data.get('status')}，下一动作={data.get('next_action')}"
        )
    _verify_release(data)
    _assert_current_log(data)
    if data.get("terminal_failure_code"):
        raise ValueError("运行包含正式失败终态，不得交付")
    if int(data.get("validation_rounds", 0)) > MAX_VALIDATION_ROUNDS:
        raise ValueError("正式验收轮次超限")
    if int(data.get("repair_cycles", 0)) > MAX_REPAIR_CYCLES:
        raise ValueError("正式修复周期超限")
    if int(data.get("pipeline_rebuilds_after_validation", 0)) > MAX_PIPELINE_REBUILDS_AFTER_VALIDATION:
        raise ValueError("验收后重建次数超限")
    artifacts = data.get("artifacts", {})
    required = (
        "docx",
        "xlsx",
        "rules_xlsx",
        "detailed_log",
        "validation",
        "validation_error_manifest",
        "truth_freeze",
        "preflight",
        "delivery_provenance",
    )
    for key in required:
        artifact = Path(str(artifacts.get(key, "")))
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            raise ValueError(f"终态文件不存在或为空：{key}")
    validation = read_validation(Path(str(artifacts["validation"])))
    if (
        validation.get("status") not in VALIDATION_STATUSES
        or validation.get("errors")
        or int(validation.get("error_count", 0) or 0) != 0
        or _error_codes(Path(str(artifacts["validation_error_manifest"])))
    ):
        raise ValueError("终态验收不是零错误通过")
    ledger = Path(str(data["approved_writer_ledger"]))
    verify_truth_freeze(
        state_path=path,
        writer_ledger_path=ledger,
        manifest_path=Path(str(artifacts["truth_freeze"])),
        require_scoring=True,
    )
    seal = verify_delivery_provenance(
        state_path=path,
        writer_ledger_path=ledger,
        seal_path=Path(str(artifacts["delivery_provenance"])),
    )
    if seal.get("seal_sha256") != data.get("delivery_provenance_sha256"):
        raise ValueError("运行状态与交付来源封印不一致")
    if read_final_summary(Path(str(artifacts["detailed_log"]))) is None:
        raise ValueError("详细运行日志缺少正式最终摘要")
    return data


def print_state(data: Mapping[str, object]) -> None:
    print(json.dumps(dict(data), ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="保存、恢复并验证长任务的正式运行状态")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--state", type=Path, required=True)
    init.add_argument("--task-run-id", required=True)
    init.add_argument("--place", required=True)
    init.add_argument("--skill-root", type=Path, required=True)
    init.add_argument("--release-manifest", type=Path, required=True)
    init.add_argument("--detailed-log", type=Path, required=True)
    init.add_argument("--writer-ledger", type=Path)
    init.add_argument("--target-confidence", choices=TARGET_CONFIDENCES)
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--state", type=Path, required=True)
    checkpoint.add_argument("--phase", required=True)
    checkpoint.add_argument(
        "--status",
        choices=sorted(ACTIVE_STATUSES - {"paused_by_host_limit"}),
        default="running",
    )
    checkpoint.add_argument("--last-action", default="")
    checkpoint.add_argument("--next-action", default="")
    checkpoint.add_argument("--preflight", type=Path)
    checkpoint.add_argument("--truth-freeze", type=Path)
    repair = sub.add_parser("begin-audit-repair")
    repair.add_argument("--state", required=True, type=Path)
    repair.add_argument("--audit", required=True, type=Path)
    sync = sub.add_parser("sync-audit")
    sync.add_argument("--state", type=Path, required=True)
    sync.add_argument("--audit", type=Path, required=True)
    collection = sub.add_parser("resume-collection")
    collection.add_argument("--state", type=Path, required=True)
    pause = sub.add_parser("pause")
    pause.add_argument("--state", type=Path, required=True)
    pause.add_argument("--reason", required=True)
    pause.add_argument("--next-action", required=True)
    validation = sub.add_parser("record-validation")
    validation.add_argument("--state", type=Path, required=True)
    validation.add_argument("--validation", type=Path, required=True)
    validation.add_argument("--error-manifest", type=Path, required=True)
    finish = sub.add_parser("finish")
    finish.add_argument("--state", type=Path, required=True)
    finish.add_argument("--docx", type=Path, required=True)
    finish.add_argument("--xlsx", type=Path, required=True)
    finish.add_argument("--rules-xlsx", type=Path, required=True)
    finish.add_argument("--validation", type=Path, required=True)
    finish.add_argument("--validation-error-manifest", type=Path)
    finish.add_argument("--detailed-log", type=Path, required=True)
    finish.add_argument("--delivery-provenance", type=Path)
    final_check = sub.add_parser("assert-final")
    final_check.add_argument("--state", type=Path, required=True)
    show = sub.add_parser("show")
    show.add_argument("--state", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init":
        data = initialize_state(
            args.state,
            args.task_run_id,
            args.place,
            args.skill_root,
            args.release_manifest,
            args.detailed_log,
            args.writer_ledger,
            args.target_confidence,
        )
    elif args.command == "checkpoint":
        data = checkpoint_state(
            args.state,
            phase=args.phase,
            status=args.status,
            last_action=args.last_action,
            next_action=args.next_action,
            preflight_path=args.preflight,
            truth_freeze_path=args.truth_freeze,
        )
    elif args.command == "begin-audit-repair":
        data = begin_audit_repair(args.state, args.audit)
    elif args.command == "sync-audit":
        data = sync_audit_state(args.state, args.audit)
    elif args.command == "resume-collection":
        data = resume_collection(args.state)
    elif args.command == "pause":
        data = pause_state(args.state, args.reason, args.next_action)
    elif args.command == "record-validation":
        data = record_validation_result(args.state, args.validation, args.error_manifest)
    elif args.command == "finish":
        data = finish_state(
            args.state,
            args.docx,
            args.xlsx,
            args.rules_xlsx,
            args.validation,
            args.detailed_log,
            args.validation_error_manifest,
            args.delivery_provenance,
        )
    elif args.command == "assert-final":
        data = assert_finalizable(args.state)
    else:
        data = load_state(args.state)
    print_state(data)


if __name__ == "__main__":
    main()
