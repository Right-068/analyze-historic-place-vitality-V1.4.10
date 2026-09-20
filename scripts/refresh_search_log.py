#!/usr/bin/env python3
"""Refresh machine-derived scored-evidence counts in the search ledger."""

from __future__ import annotations

import argparse
import csv
import strict_json as json
import os
from collections import Counter
from pathlib import Path

from formal_scoring import build_formal_scoring_chain, load_protocol
from artifact_provenance import register_protected_artifact, verify_artifact_writer
from retrieval_controls import (
    executed_query_metrics,
    execution_schema_context_from_state,
    validate_execution_records,
    validate_query_intent_signature,
)
from runtime_guard import authorize_runtime_write, canonical_sha256
from execution_facts import event_key, validate_source_links
from process_lock import ProcessFileLock


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def refresh(
    search_log_path: Path,
    source_path: Path,
    evidence_path: Path,
    output_path: Path,
    *,
    execution_schema_context: dict[str, object] | None = None,
) -> dict[str, object]:
    fields, searches = read_csv(search_log_path)
    _, sources = read_csv(source_path)
    _, evidence = read_csv(evidence_path)
    required = {
        "new_scored_evidence_units",
        "cumulative_scored_evidence_units",
        "query_id",
    }
    if not required.issubset(fields):
        raise ValueError("检索日志缺少计分证据刷新字段")
    if "normalized_query_intent" not in fields:
        # Deterministic migration for legacy ledgers that predate the stored
        # integrity checksum.  The value is still recomputed on every read.
        fields.append("normalized_query_intent")
    execution_validation = validate_execution_records(
        searches,
        schema_context=execution_schema_context,
    )
    if execution_validation["invalid_records"]:
        raise ValueError(
            "search_execution_contract_invalid: "
            + json.dumps(
                execution_validation["invalid_records"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    valid_by_id = {
        str(row['execution_id']): row
        for row in execution_validation["valid_rows"]
    }
    migrated_digests = {
        row['original_record_sha256']: row['migrated_record_sha256']
        for row in execution_validation['migration']['rows']
    }
    valid_by_digest = {row['_record_sha256']: row for row in valid_by_id.values()}
    # Materialize only the explicit, verified migration mapping. Merely adding
    # counts to an old row would leave it incompatible with the strict readers.
    for row in searches:
        digest = migrated_digests.get(canonical_sha256(row))
        if digest is not None:
            validated = valid_by_digest.get(digest)
            if validated is None:
                raise ValueError('legacy_migration_validated_record_missing')
            row.clear()
            row.update({str(k): v for k, v in validated.items() if not str(k).startswith('_')})
    fields.extend(sorted({key for row in searches for key in row} - set(fields)))
    source_links = validate_source_links(sources, execution_validation)
    if source_links['status'] != 'valid':
        raise ValueError('source_execution_chain_invalid: ' + json.dumps(source_links['errors'], ensure_ascii=False, sort_keys=True))
    source_execution = {
        row.get("source_id", ""): row.get("execution_id", "")
        for row in sources
        if row.get("source_id", "")
    }
    from temporal_fields import date_errors
    invalid_dates = [error for row in evidence for error in date_errors(row, label='evidence:' + row.get('evidence_id', ''))]
    if invalid_dates:
        raise ValueError(';'.join(invalid_dates))
    sources = list(source_links['valid_sources'])
    allowed = {row['source_id'] for row in sources}
    evidence = [row for row in evidence if row.get('source_id') in allowed]
    chain = build_formal_scoring_chain(evidence, sources, protocol=load_protocol())
    scored_by_execution = Counter(
        source_execution.get(row.get("source_id", ""), "")
        for row in chain["scored_rows"]
    )
    cumulative = 0
    cumulative_by_execution = {}
    for validated in sorted(valid_by_id.values(), key=event_key):
        execution_id = str(validated['execution_id'])
        cumulative += int(scored_by_execution.get(execution_id, 0))
        cumulative_by_execution[execution_id] = cumulative
    for row in searches:
        execution_id = str(row.get('execution_id', ''))
        validated = valid_by_id.get(execution_id)
        if validated is not None:
            signature = validate_query_intent_signature(validated)
            row["normalized_query_intent"] = str(signature["derived"])
            new_count = int(scored_by_execution.get(execution_id, 0))
        else:
            new_count = 0
        row["new_scored_evidence_units"] = str(new_count)
        row["cumulative_scored_evidence_units"] = str(cumulative_by_execution.get(execution_id, 0))
    with ProcessFileLock(output_path):
        write_csv(output_path, fields, searches)
    metrics = executed_query_metrics(
        searches,
        schema_context=execution_schema_context,
    )
    return {
        "status": "created",
        "search_rows": len(searches),
        "scored_evidence_units": cumulative,
        "unique_query_intents": metrics["unique_query_intent_count"],
        "actual_execution_attempts": metrics["actual_execution_attempt_count"],
        "controlled_retry_attempts": metrics["controlled_retry_count"],
        "execution_schema_version": metrics["execution_schema_version"],
        "output": str(output_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-log", required=True, type=Path)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--writer-ledger", required=True, type=Path)
    args = parser.parse_args()
    authorize_runtime_write(
        state_path=args.state,
        writer_script_id="refresh_search_log.py",
        output_role="search_log",
        expected_phase="SEMANTIC_QUANTIFICATION",
        output_path=args.output,
    )
    for role, path in (
        ("search_log", args.search_log),
        ("source_ledger", args.sources),
        ("formal_evidence", args.evidence),
    ):
        verify_artifact_writer(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            output_role=role,
            output_path=path,
        )
    state_payload = json.loads(args.state.read_text(encoding="utf-8-sig"))
    if not isinstance(state_payload, dict):
        raise ValueError("run state must be a JSON object")
    execution_schema_context = execution_schema_context_from_state(state_payload, state_path=args.state)
    result = refresh(
        args.search_log,
        args.sources,
        args.evidence,
        args.output,
        execution_schema_context=execution_schema_context,
    )
    register_protected_artifact(
        state_path=args.state,
        writer_ledger_path=args.writer_ledger,
        writer_script_id="refresh_search_log.py",
        output_role="search_log",
        output_path=args.output,
        input_paths={
            "search_log": args.search_log,
            "sources": args.sources,
            "evidence": args.evidence,
        },
        expected_phase="SEMANTIC_QUANTIFICATION",
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
