#!/usr/bin/env python3
"""Immutable-release, workflow, ledger, and formal-scoring guards.

This module contains execution controls only.  It intentionally does not own
dimension definitions, weights, score mappings, or evidence thresholds.
"""

from __future__ import annotations

import hashlib
import inspect
import strict_json as json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from formal_states import AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
from run_paths import logical_path, rebind_state_paths
from source_identity import (
    canonical_url as resolve_canonical_url,
    normalized_url_sha256 as resolve_normalized_url_sha256,
    page_entity_id_for_source,
)


RUNTIME_FOLDERS = ("agents", "assets", "references", "scripts")
IGNORED_NAMES = {"__pycache__", ".pytest_cache"}
PHASES = (
    "INITIALIZE",
    "SEARCH",
    "EVIDENCE_BUILD",
    "SEMANTIC_QUANTIFICATION",
    "EVIDENCE_AUDIT",
    "SCORE",
    "REPORT_BUILD",
    "VALIDATE",
    "DELIVER",
)
PHASE_INDEX = {name: index for index, name in enumerate(PHASES)}
PHASE_ALIASES = {
    "initializing": "INITIALIZE",
    "retrieval": "SEARCH",
    "search": "SEARCH",
    "evidence_build": "EVIDENCE_BUILD",
    "semantic_quantification": "SEMANTIC_QUANTIFICATION",
    "evidence_audit": "EVIDENCE_AUDIT",
    "score": "SCORE",
    "scoring": "SCORE",
    "report_build": "REPORT_BUILD",
    "generation": "REPORT_BUILD",
    "validation": "VALIDATE",
    "validating": "VALIDATE",
    "delivered": "DELIVER",
}

TERMINAL_FAILURE_STATUSES = {
    "preflight_invalid",
    "validation_nonconvergent",
    "post_validate_upstream_mutation",
    "untrusted_artifact_provenance",
    "state_log_divergence",
    "review_provenance_invalid",
    "pre_admission_invalid",
    "untrusted_canonical_writer",
    "report_narrative_nonconvergent",
}

UPSTREAM_TRUTH_ROLES = {
    "tool_response_artifact",
    "execution_events",
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

# This table controls execution only.  It deliberately contains no dimension,
# score, confidence, or evidence-threshold value.
WRITER_PERMISSIONS: dict[str, dict[str, set[str]]] = {
    "execution_facts.py": {"SEARCH": {"search_log", "execution_events"}},
    "artifact_provenance.py": {
        "SEARCH": {"search_log"},
        "EVIDENCE_AUDIT": {"truth_freeze"},
        "SCORE": {"truth_freeze", "score_gate"},
        "REPORT_BUILD": {"limitations"},
        "VALIDATE": {"delivery_provenance"},
    },
    "source_capture.py": {
        "SEARCH": {"source_capture_manifest", "tool_response_artifact"},
    },
    "pre_admission_audit.py": {
        "EVIDENCE_BUILD": {
            "canonical_source_ledger",
            "canonical_evidence_ledger",
            "correction_event_ledger",
            "locator_audit",
            "collision_audit",
            "pre_admission_audit",
            "source_ledger",
            "raw_evidence",
        },
    },
    "canonical_correction.py": {
        "EVIDENCE_BUILD": {
            "correction_event_ledger",
            "source_ledger",
            "raw_evidence",
            "locator_audit",
            "collision_audit",
            "pre_admission_audit",
        },
    },
    "refresh_search_log.py": {
        "SEMANTIC_QUANTIFICATION": {"search_log"},
    },
    "apply_review_changes.py": {
        "SEMANTIC_QUANTIFICATION": {"reviewed_evidence", "review_change_ledger"},
    },
    "quantify_text_semantics.py": {
        "SEMANTIC_QUANTIFICATION": {
            "semantic_evidence",
            "formal_evidence",
            "semantic_audit",
            "coding_queue",
        },
        "SCORE": {"formal_evidence", "semantic_audit", "coding_queue", "platform_scores"},
    },
    "audit_evidence.py": {
        "EVIDENCE_AUDIT": {"evidence_audit"},
    },
    "calculate_scores.py": {
        "SCORE": {"scoring_output"},
    },
    "preflight_validate.py": {
        "SCORE": {"preflight_report"},
    },
    "compile_report_truth.py": {
        "SCORE": {"report_truth"},
    },
    "assemble_report_data.py": {
        "REPORT_BUILD": {"report_narrative", "report_data"},
    },
    "build_research_workbook.py": {
        "REPORT_BUILD": {"research_workbook"},
    },
    "build_semantic_rules_workbook.py": {
        "REPORT_BUILD": {"rules_workbook"},
    },
    "build_docx_report.py": {
        "REPORT_BUILD": {"docx_report"},
    },
    "validate_deliverables.py": {
        "VALIDATE": {"validation_report", "validation_error_manifest"},
    },
    "manage_run_state.py": {
        "DELIVER": {"delivery_provenance"},
    },
}

# Operational checkpoints are protected data, not scoring authority.
for _writer in ("run_research.py", "action_dispatch.py", "batch_pipeline.py",
                "tool_batch.py", "runtime_dispatch.py", "prepare_host_input.py", "protected_runtime.py", "cost_metrics.py"):
    WRITER_PERMISSIONS[_writer] = {
        phase: {"operational_checkpoint", "operational_intent"} for phase in PHASES
    }


class RuntimeAuthorizationError(ValueError):
    """A stable, machine-readable denial raised by the formal runtime guard."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def records_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    normalized = [
        {str(key): "" if value is None else str(value) for key, value in sorted(row.items())}
        for row in rows
    ]
    return canonical_sha256(sorted(normalized, key=canonical_sha256))


def canonical_url(value: object) -> str:
    """Return the release-wide canonical URL used for page identity."""
    return resolve_canonical_url(value)


def normalized_url_sha256(value: object) -> str:
    return resolve_normalized_url_sha256(value)


def normalized_semantic_text(row: Mapping[str, object]) -> str:
    text = str(
        row.get("semantic_unit_text")
        or row.get("original_summary_text")
        or row.get("excerpt_or_summary")
        or ""
    )
    return re.sub(r"\s+", " ", text).strip().casefold()


def machine_formal_dedup_sha256(
    row: Mapping[str, object],
    source: Mapping[str, object] | None = None,
) -> str:
    """Derive formal evidence identity without trusting an agent dedup label."""
    dimension = str(row.get("primary_dimension", "")).strip()
    text = normalized_semantic_text(row)
    if str(row.get("semantic_method", "")) == "source_native_numeric" or str(
        row.get("unit_type", "")
    ) == "source_native_numeric":
        values = [
            str(row.get(field, "")).strip()
            for field in (
                "native_rating_value",
                "native_rating_scale_min",
                "native_rating_scale_max",
            )
        ]
        if all(values):
            text = "native_numeric:" + ":".join(values)
    lineage = ""
    if source is not None:
        # Formal evidence identity stays stable when a later batch reveals a
        # cross-URL content collision.  Page-count audits use page_entity_id;
        # evidence dedup uses the canonical page URL lineage, while collision
        # admission prevents a mirrored text from being scored twice.
        lineage = str(source.get("normalized_url_sha256", "")).strip()
        if not lineage:
            from source_identity import identity_url
            lineage = normalized_url_sha256(identity_url(source))
        if not lineage:
            lineage = str(source.get("page_entity_id", "")).strip()
        if not lineage:
            lineage = page_entity_id_for_source(source)
    if not lineage:
        lineage = str(row.get("source_id", "")).strip()
    if not dimension or not text or not lineage:
        return ""
    return canonical_sha256(
        {
            "source_lineage": lineage,
            "primary_dimension": dimension,
            "semantic_text": text,
        }
    )


def state_binding_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "task_run_id": state.get("task_run_id"),
        "place": state.get("place"),
        "target_confidence": state.get("target_confidence"),
        "minimum_dimension_confidence": state.get("minimum_dimension_confidence"),
        "execution_log_contract": state.get("execution_log_contract"),
        "research_cutoff": state.get("research_cutoff"),
        "terminal_failure_code": state.get("terminal_failure_code"),
        "audit_repair_required": state.get("audit_repair_required"),
        "audit_repair_action": state.get("audit_repair_action"),
        "audit_repair_attempt": state.get("audit_repair_attempt"),
        "last_audit_failure_fingerprint": state.get("last_audit_failure_fingerprint"),
        "state_revision": state.get("state_revision"),
        "phase": state.get("phase"),
        "status": state.get("status"),
        "audit_status": state.get("audit_status"),
        "unique_pages": state.get("unique_pages"),
        "effective_samples": state.get("effective_samples"),
        "iteration_round": state.get("iteration_round"),
        "dimensions_needing_iteration": state.get("dimensions_needing_iteration"),
        "validation_rounds": state.get("validation_rounds", 0),
        "repair_cycles": state.get("repair_cycles", 0),
        "pipeline_rebuilds_after_validation": state.get(
            "pipeline_rebuilds_after_validation", 0
        ),
    }


def state_binding_sha256(state: Mapping[str, object]) -> str:
    return canonical_sha256(state_binding_payload(state))


def load_runtime_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeAuthorizationError("missing_run_state", "正式运行状态文件不存在")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeAuthorizationError("invalid_run_state", "正式运行状态无法读取") from exc
    if not isinstance(data, dict) or not str(data.get("task_run_id", "")).strip():
        raise RuntimeAuthorizationError("invalid_run_state", "正式运行状态缺少 task_run_id")
    return rebind_state_paths(data, path)


def _caller_contains(path: Path) -> bool:
    expected = path.resolve()
    frame = inspect.currentframe()
    try:
        frame = frame.f_back.f_back
        while frame is not None:
            if Path(frame.f_code.co_filename).resolve() == expected:
                return True
            frame = frame.f_back
    finally:
        del frame
    return False


def authorize_runtime_write(
    *,
    state_path: Path,
    writer_script_id: str,
    output_role: str,
    expected_phase: str | None = None,
    output_path: Path | None = None,
    require_caller: bool = True,
) -> dict[str, Any]:
    """Authorize a formal write against state, release, phase, and writer ID."""
    if writer_script_id != 'execution_facts.py':
        from execution_coordinator import assert_no_pending
        assert_no_pending(state_path.resolve().parent)
    state = load_runtime_state(state_path)
    status = str(state.get("status", ""))
    final_seal_write = (
        status == "complete"
        and writer_script_id == "manage_run_state.py"
        and output_role == "delivery_provenance"
    )
    if (status == "complete" and not final_seal_write) or status in TERMINAL_FAILURE_STATUSES:
        raise RuntimeAuthorizationError(
            "terminal_state_write_denied",
            f"终态运行不得写入正式产物：{status}",
        )
    task_run_id = str(state["task_run_id"])
    skill_root = Path(str(state.get("skill_root", "")))
    release_manifest = Path(str(state.get("release_manifest", "")))
    manifest = verify_release_manifest(
        skill_root,
        release_manifest,
        task_run_id=task_run_id,
    )
    phase = normalize_phase(str(state.get("phase", "")))
    if expected_phase is not None and phase != normalize_phase(expected_phase):
        raise RuntimeAuthorizationError(
            "phase_not_authorized",
            f"{writer_script_id} 要求阶段 {normalize_phase(expected_phase)}，当前为 {phase}",
        )
    allowed = WRITER_PERMISSIONS.get(writer_script_id, {}).get(phase, set())
    if output_role not in allowed:
        raise RuntimeAuthorizationError(
            "writer_not_authorized",
            f"{writer_script_id} 在 {phase} 无权写入 {output_role}",
        )
    if int(state.get("validation_rounds", 0) or 0) > 0 and output_role in UPSTREAM_TRUTH_ROLES:
        raise RuntimeAuthorizationError(
            "post_validate_upstream_mutation",
            "首次正式验收后禁止修改任何冻结上游真值文件",
        )
    if output_path is not None:
        _ensure_external_output(skill_root, output_path)
        if not logical_path(output_path, state_path.resolve().parent):
            raise RuntimeAuthorizationError(
                "run_root_escape",
                "正式运行输出必须位于当前 task_run_id 的运行根目录内",
            )
    writer_path = skill_root / "scripts" / writer_script_id
    if not writer_path.is_file():
        raise RuntimeAuthorizationError("untrusted_writer", "发布版中不存在正式写入脚本")
    files = manifest.get("files")
    expected_hash = files.get(f"scripts/{writer_script_id}") if isinstance(files, dict) else None
    if expected_hash != sha256_file(writer_path):
        raise RuntimeAuthorizationError("untrusted_writer", "正式写入脚本不在发布版哈希清单中")
    if require_caller and not _caller_contains(writer_path):
        raise RuntimeAuthorizationError(
            "untrusted_writer",
            "当前调用栈不是发布清单中的正式写入脚本",
        )
    return {
        "state": state,
        "task_run_id": task_run_id,
        "phase": phase,
        "skill_root": skill_root,
        "release_manifest": release_manifest,
        "release_manifest_sha256": sha256_file(release_manifest),
        "writer_script": writer_path,
        "writer_script_sha256": sha256_file(writer_path),
    }


def _runtime_files(skill_root: Path) -> list[Path]:
    root = skill_root.resolve()
    required = [root / "SKILL.md", *(root / name for name in RUNTIME_FOLDERS)]
    if not required[0].is_file() or not (root/"requirements.txt").is_file() or any(not item.is_dir() for item in required[1:]):
        raise ValueError("发布版 Skill 根目录不完整")
    files = [required[0], root/"requirements.txt"]
    for folder in required[1:]:
        files.extend(
            path
            for path in folder.rglob("*")
            if path.is_file()
            and not any(part in IGNORED_NAMES for part in path.parts)
            and path.suffix.lower() not in {".pyc", ".pyo"}
        )
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def release_snapshot(skill_root: Path) -> dict[str, str]:
    root = skill_root.resolve()
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in _runtime_files(root)
    }


def _ensure_external_output(skill_root: Path, output: Path) -> None:
    root = skill_root.resolve()
    resolved = output.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return
    raise ValueError("运行期清单必须保存在任务工作目录，不能写入发布版 Skill")


def create_release_manifest(
    skill_root: Path,
    output: Path,
    task_run_id: str,
) -> dict[str, object]:
    if not task_run_id.strip():
        raise ValueError("task_run_id 不能为空")
    _ensure_external_output(skill_root, output)
    snapshot = release_snapshot(skill_root)
    payload: dict[str, object] = {
        "manifest_type": "immutable_skill_release",
        "task_run_id": task_run_id.strip(),
        "skill_root_name": skill_root.resolve().name,
        "file_count": len(snapshot),
        "files": snapshot,
        "snapshot_sha256": canonical_sha256(snapshot),
        "dependency_contract": dependency_contract(skill_root),
        "approved_writer_registry": {
            script: {"relative_path": "scripts/" + script,
                     "sha256": snapshot["scripts/" + script],
                     "phase_roles": {phase: sorted(roles) for phase, roles in phases.items()}}
            for script, phases in WRITER_PERMISSIONS.items()
        },
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8-sig"))
        if existing != payload:
            raise ValueError("发布版完整性清单已存在且内容不同，不得覆盖")
        return existing
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return payload


def verify_release_manifest(
    skill_root: Path,
    manifest_path: Path,
    *,
    task_run_id: str | None = None,
) -> dict[str, object]:
    if not manifest_path.is_file():
        raise ValueError("缺少发布版完整性清单")
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("manifest_type") != "immutable_skill_release":
        raise ValueError("发布版完整性清单格式无效")
    if task_run_id is not None and payload.get("task_run_id") != task_run_id:
        raise ValueError("发布版完整性清单属于不同 task_run_id")
    expected = payload.get("files")
    if not isinstance(expected, dict):
        raise ValueError("发布版完整性清单缺少文件哈希")
    actual = release_snapshot(skill_root)
    if expected != actual or payload.get("snapshot_sha256") != canonical_sha256(actual):
        missing = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        changed = sorted(
            name for name in set(expected) & set(actual) if expected[name] != actual[name]
        )
        detail = {"missing": missing, "added": added, "changed": changed}
        raise ValueError(
            "任务运行期间发布版 Skill 已发生改变，正式流程立即失效："
            + json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        )
    registry = {script: {"relative_path": "scripts/" + script,
        "sha256": actual["scripts/" + script],
        "phase_roles": {phase: sorted(roles) for phase, roles in phases.items()}}
        for script, phases in WRITER_PERMISSIONS.items()}
    if payload.get("approved_writer_registry") != registry:
        raise ValueError("approved_writer_registry_mismatch")
    if payload.get("dependency_contract") != dependency_contract(skill_root):
        raise ValueError("release_dependency_contract_mismatch")
    return payload


def dependency_contract(skill_root):
    path=Path(skill_root)/"requirements.txt"
    requirements=sorted(line.strip() for line in path.read_text(encoding="utf8").splitlines()
                        if line.strip() and not line.lstrip().startswith("#"))
    if not any(line.startswith("tzdata==") for line in requirements):
        raise ValueError("release_dependency_missing_tzdata")
    return {"path":"requirements.txt","sha256":sha256_file(path),
            "bytes":path.stat().st_size,"required_dependencies":requirements}


def normalize_phase(value: str) -> str:
    text = value.strip()
    phase = PHASE_ALIASES.get(text.lower(), text.upper())
    if phase not in PHASE_INDEX:
        raise ValueError(f"未知工作流阶段：{value}")
    return phase


def validate_phase_transition(current: str, requested: str) -> str:
    old = normalize_phase(current)
    new = normalize_phase(requested)
    if old == new:
        return new
    if old == "EVIDENCE_AUDIT" and new == "SEARCH":
        return new
    if PHASE_INDEX[new] != PHASE_INDEX[old] + 1:
        raise ValueError(f"非法工作流跳转：{old} → {new}")
    return new


def assert_post_audit_permitted(
    audit_status: str,
    dimensions_needing_iteration: int,
    requested_phase: str,
) -> None:
    phase = normalize_phase(requested_phase)
    if phase not in {"SCORE", "REPORT_BUILD", "VALIDATE", "DELIVER"}:
        return
    restricted_shortfall = audit_status == AUDIT_RETRIEVAL_TERMINATED_WITH_SHORTFALL
    if audit_status == "needs_iteration" or (
        dimensions_needing_iteration > 0 and not restricted_shortfall
    ):
        raise ValueError(
            "正式证据审计仍要求继续检索；不得进入评分、报告、验收或交付阶段"
        )
    if audit_status == "invalid":
        raise ValueError("正式证据审计无效；不得进入后续正式阶段")


def task_run_ids(rows: Iterable[Mapping[str, object]]) -> set[str]:
    return {
        str(row.get("task_run_id", "")).strip()
        for row in rows
        if str(row.get("task_run_id", "")).strip()
    }


def assert_single_task_run(
    expected_task_run_id: str,
    *ledgers: tuple[str, Sequence[Mapping[str, object]]],
) -> None:
    if not expected_task_run_id.strip():
        raise ValueError("正式流程必须提供 task_run_id")
    for name, rows in ledgers:
        values = task_run_ids(rows)
        if values != {expected_task_run_id}:
            raise ValueError(
                f"{name} 的 task_run_id 与正式运行不一致：{sorted(values)}"
            )


def scoring_gate_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "task_run_id": data.get("task_run_id"),
        "formal_scoring_schema_version": data.get("formal_scoring_schema_version"),
        "evaluation_protocol_version": data.get("evaluation_protocol_version"),
        "semantic_codebook_version": data.get("semantic_codebook_version"),
        "dimension_assignment_field": data.get("dimension_assignment_field"),
        "dimension_evidence_audit": data.get("dimension_evidence_audit"),
        "dimension_summary": data.get("dimension_summary"),
        "platforms": data.get("platforms"),
    }


def seal_scoring_input(data: dict[str, object]) -> dict[str, object]:
    data["formal_scoring_gate"] = {
        "status": "passed",
        "payload_sha256": canonical_sha256(scoring_gate_payload(data)),
    }
    return data


def verify_scoring_input_gate(data: Mapping[str, object]) -> None:
    gate = data.get("formal_scoring_gate")
    if not isinstance(gate, dict) or gate.get("status") != "passed":
        raise ValueError("正式评分输入缺少不可绕过的审计门禁")
    expected = canonical_sha256(scoring_gate_payload(data))
    if gate.get("payload_sha256") != expected:
        raise ValueError("正式评分输入在门禁通过后发生改变")


def validate_evidence_truth_chain(
    sources: Sequence[Mapping[str, object]],
    raw_evidence: Sequence[Mapping[str, object]],
    semantic_evidence: Sequence[Mapping[str, object]],
    formal_evidence: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    source_ids = {str(row.get("source_id", "")).strip() for row in sources if str(row.get("source_id", "")).strip()}

    def ids(rows: Sequence[Mapping[str, object]], label: str) -> set[str]:
        values = [str(row.get("evidence_id", "")).strip() for row in rows]
        if any(not value for value in values):
            raise ValueError(f"{label} 存在空 evidence_id")
        if len(values) != len(set(values)):
            raise ValueError(f"{label} 存在重复 evidence_id")
        for row in rows:
            if str(row.get("source_id", "")).strip() not in source_ids:
                raise ValueError(f"{label} 存在无法关联来源台账的 evidence_id")
        return set(values)

    raw_ids = ids(raw_evidence, "原始证据台账")
    semantic_ids = ids(semantic_evidence, "语义证据台账")
    formal_ids = ids(formal_evidence, "正式评分证据台账")
    orphan_semantic = sorted(semantic_ids - raw_ids)
    orphan_formal = sorted(formal_ids - semantic_ids)
    if orphan_semantic:
        raise ValueError(f"语义证据台账存在孤立 evidence_id：{orphan_semantic[:5]}")
    if orphan_formal:
        raise ValueError(f"正式评分证据台账存在孤立 evidence_id：{orphan_formal[:5]}")

    source_by_id = {str(row.get("source_id", "")).strip(): row for row in sources}
    raw_by_id = {str(row.get("evidence_id", "")).strip(): row for row in raw_evidence}
    semantic_by_id = {
        str(row.get("evidence_id", "")).strip(): row for row in semantic_evidence
    }
    for row in [*semantic_evidence, *formal_evidence]:
        source = source_by_id[str(row.get("source_id", "")).strip()]
        declared = str(row.get("platform", "")).strip()
        authoritative = str(source.get("platform", "")).strip()
        if declared and declared != authoritative:
            raise ValueError("证据 platform 必须由来源台账唯一派生")
        evidence_id = str(row.get("evidence_id", "")).strip()
        upstream = raw_by_id.get(evidence_id)
        if upstream is None:
            raise ValueError("证据真值链缺少原始证据记录")
        for field in (
            "task_run_id",
            "source_id",
            "original_summary_text",
            "excerpt_or_summary",
        ):
            if str(row.get(field, "")) != str(upstream.get(field, "")):
                raise ValueError(f"禁止在正式处理链改写原始证据字段：{field}")
    for row in formal_evidence:
        evidence_id = str(row.get("evidence_id", "")).strip()
        semantic = semantic_by_id.get(evidence_id)
        if semantic is None:
            raise ValueError("正式评分证据缺少语义证据上游")
        for field in ("task_run_id", "source_id", "original_summary_text", "excerpt_or_summary"):
            if str(row.get(field, "")) != str(semantic.get(field, "")):
                raise ValueError(f"正式评分证据改写了语义上游字段：{field}")
    return {
        "source_ledger_rows": len(sources),
        "raw_evidence_units": len(raw_ids),
        "semantic_evidence_units": len(semantic_ids),
        "formal_evidence_units": len(formal_ids),
    }
