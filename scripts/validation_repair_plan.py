#!/usr/bin/env python3
"""Create an error manifest and bounded repair plan from stable error objects.

Natural-language messages are display-only. No branch in this module searches
or classifies message text.
"""

from __future__ import annotations

import hashlib
import strict_json as json
from collections import defaultdict
from pathlib import Path
from typing import Mapping


REQUIRED_ERROR_FIELDS = {
    "error_code", "severity", "subsystem", "origin_layer", "truth_layer",
    "affected_artifacts", "upstream_truth_valid", "repair_scope",
    "requires_new_run", "message",
}
UPSTREAM_LAYERS = {"capture", "canonical", "semantic", "scoring", "truth_freeze", "state"}
DOWNSTREAM_REPAIR_SCOPES = {"generator_code", "report_narrative", "office_rebuild"}


def stable_error(
    *, error_code: str, subsystem: str, origin_layer: str, truth_layer: str,
    message: str, affected_artifacts: list[str] | None = None,
    severity: str = "fatal", upstream_truth_valid: bool = False,
    repair_scope: str = "new_run", requires_new_run: bool = True,
) -> dict[str, object]:
    return {
        "error_code": error_code,
        "severity": severity,
        "subsystem": subsystem,
        "origin_layer": origin_layer,
        "truth_layer": truth_layer,
        "affected_artifacts": list(affected_artifacts or []),
        "upstream_truth_valid": upstream_truth_valid,
        "repair_scope": repair_scope,
        "requires_new_run": requires_new_run,
        "message": message,
    }


def _normalize_error(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("validation error_objects entries must be objects")
    missing = REQUIRED_ERROR_FIELDS - set(raw)
    if missing:
        raise ValueError("validation error object missing fields: " + ", ".join(sorted(missing)))
    row = dict(raw)
    if not isinstance(row["affected_artifacts"], list):
        raise ValueError("affected_artifacts must be an array")
    if not isinstance(row["upstream_truth_valid"], bool) or not isinstance(row["requires_new_run"], bool):
        raise ValueError("error object boolean fields are invalid")
    if row["severity"] not in {"fatal", "error", "warning"}:
        raise ValueError("error severity is invalid")
    return row


def build_error_manifest(validation_result: Mapping[str, object], *, task_run_id: str) -> dict[str, object]:
    raw_objects = validation_result.get("error_objects", [])
    raw_messages = validation_result.get("errors", [])
    if not isinstance(raw_objects, list):
        raise ValueError("validation result error_objects must be an array")
    if not raw_objects and isinstance(raw_messages, list) and raw_messages:
        raw_objects = [stable_error(
            error_code="VALIDATION_ERROR_OBJECTS_MISSING",
            subsystem="validation_contract",
            origin_layer="validation",
            truth_layer="unknown",
            message="validator returned display errors without stable error objects",
            affected_artifacts=[],
            upstream_truth_valid=False,
            repair_scope="validator_code",
            requires_new_run=False,
        )]
    rows: list[dict[str, object]] = []
    for sequence, raw in enumerate(raw_objects, start=1):
        row = _normalize_error(raw)
        identity = {
            "task_run_id": task_run_id,
            "error_code": row["error_code"],
            "subsystem": row["subsystem"],
            "origin_layer": row["origin_layer"],
            "affected_artifacts": row["affected_artifacts"],
        }
        stable_id = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()[:16]
        row.update({
            "error_id": f"VAL-{stable_id}",
            "sequence": sequence,
            "stable_error_code": row["error_code"],
            "root_cause": row["origin_layer"],
            "upstream_truth_mutation_allowed": False,
        })
        rows.append(row)
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["origin_layer"])].append(str(row["error_id"]))
    return {
        "manifest_type": "validation_error_manifest",
        "task_run_id": task_run_id,
        "status": "valid" if not rows else "invalid",
        "error_count": len(rows),
        "errors": rows,
        "root_causes": [
            {"root_cause": layer, "error_ids": ids, "error_count": len(ids)}
            for layer, ids in sorted(grouped.items())
        ],
    }


def build_repair_plan(error_manifest: Mapping[str, object]) -> dict[str, object]:
    errors = error_manifest.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("error manifest errors must be an array")
    rows = [_normalize_error(item) for item in errors]
    error_codes = sorted({str(item["error_code"]) for item in rows})
    origin_layers = sorted({str(item["origin_layer"]) for item in rows})
    requires_new_run = any(bool(item["requires_new_run"]) for item in rows)
    upstream_invalid = any(
        str(item["origin_layer"]) in UPSTREAM_LAYERS and not bool(item["upstream_truth_valid"])
        for item in rows
    )
    downstream_only = bool(rows) and all(
        bool(item["upstream_truth_valid"])
        and str(item["repair_scope"]) in DOWNSTREAM_REPAIR_SCOPES
        for item in rows
    )
    if requires_new_run or upstream_invalid:
        return {
            "status": "terminal_root_cause_requires_new_run",
            "error_codes": error_codes,
            "origin_layers": origin_layers,
            "automatic_repair_allowed": False,
            "upstream_truth_mutation_allowed": False,
            "next_action": "start_new_independent_run_after_root_cause_fix",
        }
    if rows and not downstream_only:
        return {
            "status": "manual_code_fix_required",
            "error_codes": error_codes,
            "origin_layers": origin_layers,
            "automatic_repair_allowed": False,
            "upstream_truth_mutation_allowed": False,
            "next_action": "repair_released_validator_or_generator_then_rerun_validation",
        }
    return {
        "status": "one_downstream_rebuild_permitted" if rows else "no_repair_needed",
        "error_codes": error_codes,
        "origin_layers": origin_layers,
        "automatic_repair_allowed": bool(rows),
        "maximum_repair_cycles": 1,
        "maximum_rebuilds": 1,
        "upstream_truth_mutation_allowed": False,
        "rebuild_from": "frozen_scoring_output",
        "rebuild_outputs": [
            "report_narrative", "report_data", "research_workbook",
            "rules_workbook", "docx_report",
        ],
        "forbidden_actions": [
            "patch_existing_office_file", "modify_canonical_source",
            "modify_canonical_evidence", "modify_semantic_evidence",
            "modify_formal_evidence", "modify_dimension_audit",
            "modify_scoring_output",
        ],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--task-run-id", required=True)
    parser.add_argument("--error-manifest", type=Path, required=True)
    parser.add_argument("--repair-plan", type=Path, required=True)
    args = parser.parse_args()
    validation = json.loads(args.validation.read_text(encoding="utf-8-sig"))
    manifest = build_error_manifest(validation, task_run_id=args.task_run_id)
    plan = build_repair_plan(manifest)
    args.error_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.repair_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": manifest["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
