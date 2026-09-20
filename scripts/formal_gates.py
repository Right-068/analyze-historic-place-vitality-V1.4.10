"""Explicit score authorization derived only from frozen official artifacts."""
import csv
from pathlib import Path
import strict_json as json
from formal_states import AUDIT_TERMINAL_STATUSES
from runtime_guard import load_runtime_state, sha256_file
from run_paths import resolve_run_path


def gate_path(manifest_path):
    return Path(manifest_path).with_name("score-gate.json")


def score_binding(*, state_path, manifest_path, freeze):
    state_path = Path(state_path)
    state = load_runtime_state(state_path)
    if state.get("audit_status") not in AUDIT_TERMINAL_STATUSES:
        raise ValueError("score_gate_audit_not_terminal")
    root = state_path.resolve().parent
    def artifact(role, scoring=False):
        entry = freeze["scoring_artifacts" if scoring else "upstream_artifacts"][role]
        return resolve_run_path(entry.get("path_reference") or entry["path"], root,
                                legacy_absolute=entry.get("path", ""))
    evidence = artifact("formal_evidence")
    audit = artifact("evidence_audit")
    score = artifact("scoring_output", True)
    audit_data = json.loads(audit.read_text(encoding="utf-8-sig"))
    score_data = json.loads(score.read_text(encoding="utf-8-sig"))
    if audit_data.get("status") not in AUDIT_TERMINAL_STATUSES or score_data.get("status") != "valid":
        raise ValueError("score_gate_official_inputs_not_valid")
    with evidence.open(encoding="utf-8-sig", newline="") as stream:
        units = sum(row.get("included_in_platform_score") == "true" for row in csv.DictReader(stream))
    cross = score_data.get("cross_platform", {})
    if units == 0:
        values = [cross.get(key) for key in ("platform_equal_score", "partial_platform_equal_score",
                                            "sample_weighted_score")]
        values.extend(item.get(key) for item in cross.get("dimensions", {}).values()
                      for key in ("platform_equal_score", "sample_weighted_score"))
        if any(value is not None for value in values):
            raise ValueError("score_gate_zero_scored_units_cannot_have_formal_number")
    skill = Path(state["skill_root"])
    return {"schema_version": "score-gate-1", "task_run_id": state["task_run_id"],
        "status": "valid", "audit_status": audit_data["status"],
        "numeric_scoring_authorized": units > 0, "scored_evidence_units": units,
        "reason": "official_frozen_scoring_chain" if units else "audited_shortfall_no_numeric_score",
        "freeze_hash": sha256_file(Path(manifest_path)), "formal_evidence_hash": sha256_file(evidence),
        "audit_hash": sha256_file(audit), "score_hash": sha256_file(score),
        "evaluation_protocol_sha256": sha256_file(skill / "assets/evaluation-protocol.json"),
        "scoring_script_sha256": sha256_file(skill / "scripts/calculate_scores.py"),
        "formal_scoring_script_sha256": sha256_file(skill / "scripts/formal_scoring.py")}


def verify_score_gate(*, state_path, writer_ledger_path, manifest_path, freeze):
    from artifact_provenance import verify_artifact_writer
    from temporal_fields import timestamp
    path = gate_path(manifest_path)
    verify_artifact_writer(state_path=Path(state_path), writer_ledger_path=Path(writer_ledger_path),
                           output_role="score_gate", output_path=path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    timestamp(payload.get("created_at"))
    expected = score_binding(state_path=state_path, manifest_path=manifest_path, freeze=freeze)
    if {key: value for key, value in payload.items() if key != "created_at"} != expected:
        raise ValueError("score_gate_binding_mismatch")
    return payload
