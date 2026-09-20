#!/usr/bin/env python3
"""Validate the complete non-Office formal chain before report generation."""

from __future__ import annotations

import argparse
import csv
import strict_json as json
import os
from pathlib import Path

from formal_states import AUDIT_TERMINAL_STATUSES
from typing import Callable

from artifact_provenance import (
    register_protected_artifact,
    verify_artifact_writer,
    verify_truth_freeze,
)
from calculate_scores import calculate
from dimension_evidence import validate_machine_dimension_audit
from formal_scoring import build_formal_scoring_chain, load_protocol
from pre_admission_audit import (
    verify_admission_commit,
    verify_canonical_active_views,
    verify_source_grounding,
)
from runtime_guard import (
    authorize_runtime_write,
    canonical_sha256,
    load_runtime_state,
    sha256_file,
    validate_evidence_truth_chain,
    verify_release_manifest,
    verify_scoring_input_gate,
)
from source_capture import verify_capture_manifest, verify_capture_transaction_chain


ALLOWED_AUDIT_STATUSES = set(AUDIT_TERMINAL_STATUSES)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def preflight(
    *,
    state_path: Path,
    writer_ledger_path: Path,
    truth_freeze_path: Path,
    sources_path: Path,
    raw_evidence_path: Path,
    semantic_evidence_path: Path,
    formal_evidence_path: Path,
    search_log_path: Path,
    dimension_audit_path: Path,
    platform_scores_path: Path,
    scoring_output_path: Path,
    source_capture_manifest_path: Path,
    canonical_sources_path: Path,
    canonical_evidence_path: Path,
    correction_ledger_path: Path,
    locator_audit_path: Path,
    collision_audit_path: Path,
    admission_audit_path: Path,
    report_truth_path: Path | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    checks: dict[str, object] = {}

    def check(name: str, action: Callable[[], object]) -> object | None:
        try:
            result = action()
            checks[name] = "valid"
            return result
        except Exception as exc:
            checks[name] = "invalid"
            errors.append(f"{name}: {exc}")
            return None

    state = load_runtime_state(state_path)
    from execution_facts import context_from_state
    from retrieval_controls import load_retrieval_config
    execution_context = check("execution_schema_context", lambda: context_from_state(
        state, config=load_retrieval_config(), state_path=state_path))
    task_run_id = str(state.get("task_run_id", ""))
    if state.get("phase") != "SCORE":
        errors.append("state_phase: preflight 只能在 SCORE 阶段运行")
    check(
        "release_integrity",
        lambda: verify_release_manifest(
            Path(str(state.get("skill_root", ""))),
            Path(str(state.get("release_manifest", ""))),
            task_run_id=task_run_id,
        ),
    )
    artifacts = {
        "source_capture_manifest": source_capture_manifest_path,
        "canonical_source_ledger": canonical_sources_path,
        "canonical_evidence_ledger": canonical_evidence_path,
        "correction_event_ledger": correction_ledger_path,
        "locator_audit": locator_audit_path,
        "collision_audit": collision_audit_path,
        "pre_admission_audit": admission_audit_path,
        "source_ledger": sources_path,
        "raw_evidence": raw_evidence_path,
        "semantic_evidence": semantic_evidence_path,
        "formal_evidence": formal_evidence_path,
        "search_log": search_log_path,
        "evidence_audit": dimension_audit_path,
        "platform_scores": platform_scores_path,
        "scoring_output": scoring_output_path,
    }
    if report_truth_path is not None:
        artifacts["report_truth"] = report_truth_path
    for role, path in artifacts.items():
        check(
            f"writer_provenance/{role}",
            lambda role=role, path=path: verify_artifact_writer(
                state_path=state_path,
                writer_ledger_path=writer_ledger_path,
                output_role=role,
                output_path=path,
            ),
        )
    freeze = check(
        "truth_freeze",
        lambda: verify_truth_freeze(
            state_path=state_path,
            writer_ledger_path=writer_ledger_path,
            manifest_path=truth_freeze_path,
            require_scoring=True,
        ),
    )
    sources = check("read_sources", lambda: read_csv(sources_path))
    raw = check("read_raw_evidence", lambda: read_csv(raw_evidence_path))
    semantic = check("read_semantic_evidence", lambda: read_csv(semantic_evidence_path))
    formal = check("read_formal_evidence", lambda: read_csv(formal_evidence_path))
    search = check("read_search_log", lambda: read_csv(search_log_path))
    check(
        "source_capture_chain",
        lambda: verify_capture_manifest(source_capture_manifest_path, task_run_id=task_run_id),
    )
    check(
        "source_capture_atomic_transactions",
        lambda: verify_capture_transaction_chain(
            manifest_path=source_capture_manifest_path,
            writer_ledger_path=writer_ledger_path,
            task_run_id=task_run_id,
            require_all=True,
        ),
    )
    check(
        "canonical_active_views",
        lambda: verify_canonical_active_views(
            task_run_id=task_run_id,
            canonical_sources_path=canonical_sources_path,
            canonical_evidence_path=canonical_evidence_path,
            correction_ledger_path=correction_ledger_path,
            active_sources_path=sources_path,
            active_evidence_path=raw_evidence_path,
            collision_audit_path=collision_audit_path,
        ),
    )
    check(
        "admission_commit",
        lambda: verify_admission_commit(
            admission_audit_path,
            task_run_id=task_run_id,
            output_paths={
                "canonical_source_ledger": canonical_sources_path,
                "canonical_evidence_ledger": canonical_evidence_path,
                "correction_event_ledger": correction_ledger_path,
                "source_ledger": sources_path,
                "raw_evidence": raw_evidence_path,
                "locator_audit": locator_audit_path,
                "collision_audit": collision_audit_path,
            },
        ),
    )
    check(
        "source_grounded_evidence",
        lambda: verify_source_grounding(
            task_run_id=task_run_id,
            capture_manifest_path=source_capture_manifest_path,
            active_evidence_path=raw_evidence_path,
            locator_audit_path=locator_audit_path,
            collision_audit_path=collision_audit_path,
            admission_audit_path=admission_audit_path,
        ),
    )
    for name, path in (
        ("pre_admission_audit", admission_audit_path),
        ("locator_audit", locator_audit_path),
        ("collision_audit", collision_audit_path),
    ):
        payload = check(name, lambda path=path: json.loads(path.read_text(encoding="utf-8-sig")))
        if isinstance(payload, dict) and (
            payload.get("status") != "valid" or payload.get("task_run_id") != task_run_id
        ):
            errors.append(f"{name}: 工件未通过或 task_run_id 不一致")
    if report_truth_path is not None:
        report_truth = check(
            "report_truth",
            lambda: json.loads(report_truth_path.read_text(encoding="utf-8-sig")),
        )
        if isinstance(report_truth, dict) and (
            report_truth.get("status") != "valid"
            or report_truth.get("task_run_id") != task_run_id
        ):
            errors.append("report_truth: 机器事实编译未通过")
    if all(isinstance(value, list) for value in (sources, raw, semantic, formal, search)):
        check(
            "evidence_truth_chain",
            lambda: validate_evidence_truth_chain(sources, raw, semantic, formal),  # type: ignore[arg-type]
        )
        chain = check(
            "formal_scoring_chain",
            lambda: build_formal_scoring_chain(
                formal,  # type: ignore[arg-type]
                sources,  # type: ignore[arg-type]
                protocol=load_protocol(),
                require_source_contract=True,
            ),
        )
    else:
        chain = None
    audit_payload = check(
        "read_dimension_audit",
        lambda: json.loads(dimension_audit_path.read_text(encoding="utf-8-sig")),
    )
    if isinstance(audit_payload, dict):
        if audit_payload.get("status") not in ALLOWED_AUDIT_STATUSES or audit_payload.get("errors"):
            errors.append("dimension_audit_status: 正式证据审计未达到可评分终态")
        if all(isinstance(value, list) for value in (sources, formal, search)):
            check(
                "dimension_audit_binding",
                lambda: validate_machine_dimension_audit(
                    audit_payload,
                    task_run_id=task_run_id,
                    evidence=formal,  # type: ignore[arg-type]
                    sources=sources,  # type: ignore[arg-type]
                    search_rows=search,  # type: ignore[arg-type]
                    execution_schema_context=execution_context,
                ),
            )
    platform_data = check(
        "read_platform_scores",
        lambda: json.loads(platform_scores_path.read_text(encoding="utf-8-sig")),
    )
    score_data = check(
        "read_scoring_output",
        lambda: json.loads(scoring_output_path.read_text(encoding="utf-8-sig")),
    )
    if isinstance(platform_data, dict):
        check("scoring_input_gate", lambda: verify_scoring_input_gate(platform_data))
        recomputed = check("score_recomputation", lambda: calculate(platform_data))
        if isinstance(recomputed, dict) and isinstance(score_data, dict):
            if canonical_sha256(recomputed) != canonical_sha256(score_data):
                errors.append("score_recomputation: 综合评分不能由冻结平台输入独立复算")
    if isinstance(chain, dict) and isinstance(audit_payload, dict):
        audit_dimensions = audit_payload.get("summary", {}).get("dimension_evidence", {}).get("dimensions", {})
        if isinstance(audit_dimensions, dict):
            for dimension, item in chain.get("dimensions", {}).items():
                audit_item = audit_dimensions.get(dimension, {})
                if isinstance(audit_item, dict) and int(audit_item.get("scored_evidence_units", -1)) != int(item.get("scored_evidence_units", -2)):
                    errors.append(f"formal_chain_count: {dimension} 实际计分证据数不一致")
    if isinstance(freeze, dict) and freeze.get("task_run_id") != task_run_id:
        errors.append("truth_freeze: task_run_id 不一致")
    return {
        "status": "valid" if not errors else "invalid",
        "task_run_id": task_run_id,
        "errors": errors,
        "error_count": len(errors),
        "checks": checks,
        "input_hashes": {
            role: sha256_file(path)
            for role, path in artifacts.items()
            if path.is_file()
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    parser.add_argument("--truth-freeze", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--raw-evidence", type=Path, required=True)
    parser.add_argument("--semantic-evidence", type=Path, required=True)
    parser.add_argument("--formal-evidence", type=Path, required=True)
    parser.add_argument("--search-log", type=Path, required=True)
    parser.add_argument("--dimension-audit", type=Path, required=True)
    parser.add_argument("--platform-scores", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--source-capture-manifest", type=Path, required=True)
    parser.add_argument("--canonical-sources", type=Path, required=True)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--locator-audit", type=Path, required=True)
    parser.add_argument("--collision-audit", type=Path, required=True)
    parser.add_argument("--admission-audit", type=Path, required=True)
    parser.add_argument("--report-truth", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        authorize_runtime_write(
            state_path=args.state,
            writer_script_id="preflight_validate.py",
            output_role="preflight_report",
            expected_phase="SCORE",
            output_path=args.output,
        )
        result = preflight(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            truth_freeze_path=args.truth_freeze,
            sources_path=args.sources,
            raw_evidence_path=args.raw_evidence,
            semantic_evidence_path=args.semantic_evidence,
            formal_evidence_path=args.formal_evidence,
            search_log_path=args.search_log,
            dimension_audit_path=args.dimension_audit,
            platform_scores_path=args.platform_scores,
            scoring_output_path=args.scores,
            source_capture_manifest_path=args.source_capture_manifest,
            canonical_sources_path=args.canonical_sources,
            canonical_evidence_path=args.canonical_evidence,
            correction_ledger_path=args.corrections,
            locator_audit_path=args.locator_audit,
            collision_audit_path=args.collision_audit,
            admission_audit_path=args.admission_audit,
            report_truth_path=args.report_truth,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
        register_protected_artifact(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            writer_script_id="preflight_validate.py",
            output_role="preflight_report",
            output_path=args.output,
            input_paths={
                "truth_freeze": args.truth_freeze,
                "sources": args.sources,
                "raw_evidence": args.raw_evidence,
                "semantic_evidence": args.semantic_evidence,
                "formal_evidence": args.formal_evidence,
                "search_log": args.search_log,
                "dimension_audit": args.dimension_audit,
                "platform_scores": args.platform_scores,
                "scores": args.scores,
                "source_capture_manifest": args.source_capture_manifest,
                "canonical_sources": args.canonical_sources,
                "canonical_evidence": args.canonical_evidence,
                "corrections": args.corrections,
                "locator_audit": args.locator_audit,
                "collision_audit": args.collision_audit,
                "admission_audit": args.admission_audit,
                **({"report_truth": args.report_truth} if args.report_truth else {}),
            },
            expected_phase="SCORE",
        )
        if result["status"] != "valid":
            from manage_run_state import mark_terminal_failure

            mark_terminal_failure(args.state, "preflight_invalid", "报告构建前全链预检未通过")
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
