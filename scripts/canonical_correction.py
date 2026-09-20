#!/usr/bin/env python3
"""Append a retraction or supersession event and rebuild canonical active views.

The immutable canonical ledgers are read-only here.  Corrections are terminal,
append-only events; no command in this module can update a canonical row in
place or restore a corrected record.
"""

from __future__ import annotations

import argparse
import strict_json as json
from pathlib import Path

from artifact_provenance import register_protected_artifact, verify_artifact_writer
from pre_admission_audit import append_correction
from runtime_guard import authorize_runtime_write


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    parser.add_argument("--record-type", choices=("source", "evidence"), required=True)
    parser.add_argument("--correction-type", choices=("retract", "supersede"), required=True)
    parser.add_argument("--target-record-id", required=True)
    parser.add_argument("--replacement-record-id", default="")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--canonical-sources", type=Path, required=True)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--active-sources", type=Path, required=True)
    parser.add_argument("--active-evidence", type=Path, required=True)
    parser.add_argument("--locator-audit", type=Path, required=True)
    parser.add_argument("--collision-audit", type=Path, required=True)
    parser.add_argument("--admission-audit", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        authorization = authorize_runtime_write(
            state_path=args.state,
            writer_script_id="canonical_correction.py",
            output_role="correction_event_ledger",
            expected_phase="EVIDENCE_BUILD",
            output_path=args.corrections,
        )
        task_run_id = str(authorization["task_run_id"])
        for role, path in (
            ("canonical_source_ledger", args.canonical_sources),
            ("canonical_evidence_ledger", args.canonical_evidence),
            ("correction_event_ledger", args.corrections),
            ("locator_audit", args.locator_audit),
            ("collision_audit", args.collision_audit),
            ("pre_admission_audit", args.admission_audit),
        ):
            verify_artifact_writer(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_role=role,
                output_path=path,
            )
        event = append_correction(
            task_run_id=task_run_id,
            record_type=args.record_type,
            correction_type=args.correction_type,
            target_record_id=args.target_record_id,
            replacement_record_id=args.replacement_record_id,
            reason=args.reason,
            correction_ledger_path=args.corrections,
            canonical_sources_path=args.canonical_sources,
            canonical_evidence_path=args.canonical_evidence,
            active_sources_path=args.active_sources,
            active_evidence_path=args.active_evidence,
            locator_audit_path=args.locator_audit,
            collision_audit_path=args.collision_audit,
            admission_audit_path=args.admission_audit,
        )
        canonical_inputs = {
            "canonical_source_ledger": args.canonical_sources,
            "canonical_evidence_ledger": args.canonical_evidence,
        }
        for role, path in (
            ("correction_event_ledger", args.corrections),
            ("source_ledger", args.active_sources),
            ("raw_evidence", args.active_evidence),
            ("locator_audit", args.locator_audit),
            ("collision_audit", args.collision_audit),
            ("pre_admission_audit", args.admission_audit),
        ):
            inputs = dict(canonical_inputs)
            if role != "correction_event_ledger":
                inputs["correction_event_ledger"] = args.corrections
            register_protected_artifact(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                writer_script_id="canonical_correction.py",
                output_role=role,
                output_path=path,
                input_paths=inputs,
                expected_phase="EVIDENCE_BUILD",
            )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "valid", "event": event}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
