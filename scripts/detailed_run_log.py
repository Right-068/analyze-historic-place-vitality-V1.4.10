#!/usr/bin/env python3
"""Append-only detailed execution log with real Asia/Shanghai timestamps."""

from __future__ import annotations

import argparse
import csv
import hashlib
import strict_json as json
import os
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from runtime_guard import canonical_sha256, normalize_phase, sha256_file, state_binding_sha256


SHANGHAI = ZoneInfo("Asia/Shanghai")
PREFIX = "机器记录："
SUMMARY_PREFIX = "机器摘要："
AFTER_THE_FACT_TITLE = "事后审计摘要"


def now_text() -> str:
    return datetime.now(SHANGHAI).replace(microsecond=0).isoformat()


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".log-transaction.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def make_log_append_guard(
    path: Path,
    append_text: str,
    *,
    transaction_id: str,
) -> dict[str, object]:
    prefix = path.read_bytes() if path.is_file() else b""
    return {
        "schema": "guarded-log-append-1",
        "transaction_id": transaction_id,
        "safe_offset": len(prefix),
        "safe_prefix_sha256": _bytes_sha256(prefix),
        "append_text": append_text,
        "append_sha256": _bytes_sha256(append_text.encode("utf-8")),
    }


def apply_log_append_guard(path: Path, guard: Mapping[str, object]) -> dict[str, object]:
    """Finish one prepared append, repairing only its provable torn tail."""

    if guard.get("schema") != "guarded-log-append-1":
        raise ValueError("detailed_log_transaction_invalid: unsupported append guard")
    try:
        safe_offset = int(guard["safe_offset"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("detailed_log_transaction_invalid: missing safe offset") from exc
    expected = str(guard.get("append_text", "")).encode("utf-8")
    if not expected or guard.get("append_sha256") != _bytes_sha256(expected):
        raise ValueError("detailed_log_transaction_invalid: prepared append hash mismatch")
    raw = path.read_bytes() if path.is_file() else b""
    if len(raw) < safe_offset:
        raise ValueError("detailed_log_prefix_mismatch: log is shorter than the confirmed prefix")
    prefix = raw[:safe_offset]
    if guard.get("safe_prefix_sha256") != _bytes_sha256(prefix):
        raise ValueError("detailed_log_prefix_mismatch: confirmed log prefix has changed")
    tail = raw[safe_offset:]
    if tail == expected or tail.startswith(expected):
        return {
            "status": "already_complete",
            "safe_offset": safe_offset,
            "transaction_id": guard.get("transaction_id", ""),
        }
    if not expected.startswith(tail):
        raise ValueError(
            "detailed_log_tail_conflict: suffix is not the prepared append or a recoverable torn prefix"
        )
    _atomic_bytes(path, prefix + expected)
    return {
        "status": "recovered_torn_tail" if tail else "appended",
        "removed_bytes": len(tail),
        "safe_offset": safe_offset,
        "transaction_id": guard.get("transaction_id", ""),
    }


def prepare_log_append_recovery(path: Path, guard: Mapping[str, object]) -> dict[str, object]:
    """Classify a prepared append and truncate only a provable torn suffix."""

    if guard.get("schema") != "guarded-log-append-1":
        raise ValueError("detailed_log_transaction_invalid: unsupported append guard")
    try:
        safe_offset = int(guard["safe_offset"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("detailed_log_transaction_invalid: missing safe offset") from exc
    expected = str(guard.get("append_text", "")).encode("utf-8")
    if not expected or guard.get("append_sha256") != _bytes_sha256(expected):
        raise ValueError("detailed_log_transaction_invalid: prepared append hash mismatch")
    raw = path.read_bytes() if path.is_file() else b""
    if len(raw) < safe_offset:
        raise ValueError("detailed_log_prefix_mismatch: log is shorter than the confirmed prefix")
    prefix = raw[:safe_offset]
    if guard.get("safe_prefix_sha256") != _bytes_sha256(prefix):
        raise ValueError("detailed_log_prefix_mismatch: confirmed log prefix has changed")
    tail = raw[safe_offset:]
    if tail == expected or tail.startswith(expected):
        return {"status": "complete", "safe_offset": safe_offset}
    if not expected.startswith(tail):
        raise ValueError(
            "detailed_log_tail_conflict: suffix is not the prepared append or a recoverable torn prefix"
        )
    if tail:
        _atomic_bytes(path, prefix)
    return {
        "status": "pending",
        "safe_offset": safe_offset,
        "truncated_torn_bytes": len(tail),
    }


def initialize_log(
    path: Path,
    task_run_id: str,
    analysis_object: str,
    *,
    state_revision: int = 1,
    state_binding: str = "",
    transaction_id: str = "",
) -> dict[str, object]:
    if path.exists():
        events = read_events(path)
        if not events or events[0].get("task_run_id") != task_run_id:
            raise ValueError("详细运行日志已存在且属于其他任务")
        return events[0]
    event = {
        "event_id": "LOG-000001",
        "timestamp": now_text(),
        "task_run_id": task_run_id,
        "phase": "INITIALIZE",
        "action": "初始化详细运行日志",
        "status": "running",
        "analysis_object": analysis_object,
        "state_revision": state_revision,
        "state_binding_sha256": state_binding,
        "transaction_id": transaction_id,
    }
    _append(
        path,
        "# 详细运行日志\n"
        "本文件由程序在任务执行时实时追加；不得事后补造时间戳。\n\n"
        + render_event(event),
    )
    return event


def render_event(event: Mapping[str, object]) -> str:
    from input_safety import assert_public_text
    assert_public_text(dict(event))
    return (
        f"## {event.get('event_id')}\n"
        f"真实时间：{event.get('timestamp')}\n"
        f"阶段：{event.get('phase')}\n"
        f"动作：{event.get('action')}\n"
        f"状态：{event.get('status')}\n"
        f"{PREFIX}{json.dumps(dict(event), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n\n"
    )


def initial_log_text(event: Mapping[str, object]) -> str:
    return (
        "# 详细运行日志\n"
        "本文件由程序在任务执行时实时追加；不得事后补造时间戳。\n\n"
        + render_event(event)
    )


def append_state_event(
    path: Path,
    task_run_id: str,
    phase: str,
    action: str,
    status: str,
    *,
    state_revision: int,
    state_binding: str,
    **details: object,
) -> dict[str, object]:
    events = read_events(path)
    if not events:
        raise ValueError("详细运行日志尚未初始化；不能事后伪造实时事件")
    if any(event.get("task_run_id") != task_run_id for event in events):
        raise ValueError("详细运行日志混入不同 task_run_id")
    event = {
        "event_id": f"LOG-{len(events) + 1:06d}",
        "timestamp": now_text(),
        "task_run_id": task_run_id,
        "phase": phase,
        "action": action,
        "status": status,
        "state_revision": state_revision,
        "state_binding_sha256": state_binding,
        **details,
    }
    _append(path, render_event(event))
    return event


# Backward-compatible import name.  Formal phase events are no longer exposed
# as a CLI operation; callers must provide an exact state binding.
append_event = append_state_event


def read_events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    events: list[dict[str, object]] = []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for number, line in enumerate(lines, start=1):
        if line.startswith(PREFIX):
            try:
                value = json.loads(line[len(PREFIX):])
            except json.JSONDecodeError as exc:
                raise ValueError(f"详细运行日志第 {number} 条机器记录损坏") from exc
            if not isinstance(value, dict):
                raise ValueError("详细运行日志机器记录格式无效")
            events.append(value)
        elif number == len(lines) and line and PREFIX.startswith(line):
            raise ValueError("详细运行日志末尾机器记录前缀不完整")
    return events


def append_final_summary(
    path: Path,
    task_run_id: str,
    summary: Mapping[str, object],
    *,
    transaction_id: str = "",
    timestamp: str = "",
) -> dict[str, object]:
    events = read_events(path)
    if not events:
        raise ValueError("没有实时运行日志；只能另建事后审计摘要，不能冒充详细运行日志")
    if any(event.get("task_run_id") != task_run_id for event in events):
        raise ValueError("详细运行日志 task_run_id 不一致")
    if events[-1].get("phase") != "DELIVER" or events[-1].get("status") != "complete":
        raise ValueError("最终运行摘要只能在正式 DELIVER 终态事件之后生成")
    payload = {
        "timestamp": timestamp or now_text(),
        "task_run_id": task_run_id,
        "finalization_transaction_id": transaction_id,
        **dict(summary),
    }
    payload["event_chain_sha256"] = canonical_sha256(events)
    from input_safety import assert_public_text
    assert_public_text(payload)
    existing = read_final_summary(path)
    if existing is not None:
        if existing == payload:
            return existing
        raise ValueError("详细运行日志已存在不属于本次最终化事务的最终摘要")
    append_text = (
        "# 最终运行摘要\n"
        + SUMMARY_PREFIX
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    _atomic_bytes(path, path.read_bytes() + append_text.encode("utf-8"))
    return payload


def read_final_summary(path: Path) -> dict[str, object] | None:
    summaries = [
        json.loads(line[len(SUMMARY_PREFIX):])
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.startswith(SUMMARY_PREFIX)
    ]
    if not summaries:
        return None
    if len(summaries) != 1 or not isinstance(summaries[0], dict):
        raise ValueError("详细运行日志必须且只能有一个最终运行摘要")
    return summaries[0]


def audit_log(
    path: Path,
    *,
    task_run_id: str,
    expected_summary: Mapping[str, object] | None = None,
    expected_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    events = read_events(path)
    errors: list[str] = []
    from input_safety import contact_spans
    if path.is_file() and contact_spans(path.read_text(encoding="utf-8-sig")):
        errors.append("详细运行日志含未最小化的个人联系方式")
    if not events:
        errors.append("缺少实时详细运行日志")
    for index, event in enumerate(events, start=1):
        if event.get("event_id") != f"LOG-{index:06d}":
            errors.append("详细运行日志操作编号不连续")
        if event.get("task_run_id") != task_run_id:
            errors.append("详细运行日志 task_run_id 不一致")
        revision = event.get("state_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != index:
            errors.append("详细运行日志 state_revision 与事件序号不一致")
        if not str(event.get("state_binding_sha256", "")):
            errors.append("详细运行日志缺少状态绑定哈希")
        timestamp = str(event.get("timestamp", ""))
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.utcoffset() is None:
                raise ValueError
        except ValueError:
            errors.append("详细运行日志包含无效或无时区时间戳")
    timestamps = [str(event.get("timestamp", "")) for event in events]
    if timestamps != sorted(timestamps):
        errors.append("详细运行日志时间顺序倒退")
    summary = read_final_summary(path) if path.is_file() else None
    if expected_state is not None and events:
        latest = events[-1]
        expected_phase = normalize_phase(str(expected_state.get("phase", "")))
        if latest.get("phase") != expected_phase:
            errors.append("详细运行日志最新阶段与正式状态不一致")
        if latest.get("status") != expected_state.get("status"):
            errors.append("详细运行日志最新状态与正式状态不一致")
        if latest.get("state_revision") != expected_state.get("state_revision"):
            errors.append("详细运行日志最新修订号与正式状态不一致")
        if latest.get("state_binding_sha256") != state_binding_sha256(expected_state):
            errors.append("详细运行日志最新状态绑定哈希无效")
    if expected_summary is not None:
        if summary is None:
            errors.append("详细运行日志缺少机器生成的最终运行摘要")
        else:
            for key, value in expected_summary.items():
                if summary.get(key) != value:
                    errors.append(f"详细运行日志最终摘要与机器台账不一致：{key}")
            if summary.get("event_chain_sha256") != canonical_sha256(events):
                errors.append("详细运行日志事件链哈希与实际事件不一致")
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "event_count": len(events),
        "log_sha256": sha256_file(path) if path.is_file() else "",
    }


def assert_state_log_alignment(
    path: Path,
    state: Mapping[str, object],
) -> dict[str, object]:
    return audit_log(
        path,
        task_run_id=str(state.get("task_run_id", "")),
        expected_state=state,
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--log", required=True, type=Path)
    init.add_argument("--task-run-id", required=True)
    init.add_argument("--analysis-object", required=True)
    check = subparsers.add_parser("validate")
    check.add_argument("--log", required=True, type=Path)
    check.add_argument("--task-run-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "init":
        result = initialize_log(args.log, args.task_run_id, args.analysis_object)
    else:
        result = audit_log(args.log, task_run_id=args.task_run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "invalid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
