"""Execution-boundary integrity over the existing approved-writer hash chain.

This is local tamper detection, not an operating-system security boundary.
Candidate input files remain editable; formal and scheduling facts do not.
"""
import inspect
from pathlib import Path
from runtime_guard import (RuntimeAuthorizationError, load_runtime_state,
                           sha256_file, WRITER_PERMISSIONS)

CODE = "protected_artifact_modified_outside_approved_writer"
OPERATIONAL_WRITERS = {"run_research.py", "action_dispatch.py", "batch_pipeline.py",
                       "tool_batch.py", "runtime_dispatch.py", "prepare_host_input.py", "cost_metrics.py"}


def _operational_state(path):
    path = Path(path).resolve()
    for root in path.parents:
        state = root / "运行状态.json"
        if state.is_file():
            relative = path.relative_to(root).as_posix()
            if relative.startswith(("staging/pipeline/operational/",
                                    "staging/tool-facts/", "staging/dispatch/operational/",
                                    "staging/helpers/operational/", "staging/cost/operational/")):
                if path.suffix == ".json" and not path.name.startswith((".", "rejected-")):
                    return state
            return None
    return None


def _deny(path, expected, actual, writer=""):
    import strict_json as json
    raise RuntimeAuthorizationError(CODE, json.dumps({
        "path": str(path), "expected_sha256": expected, "actual_sha256": actual,
        "last_approved_writer": writer}, ensure_ascii=False))


def check_operational_before_write(path):
    state = _operational_state(path)
    if state is not None and Path(path).exists():
        from artifact_provenance import verify_artifact_writer
        info = load_runtime_state(state)
        try:
            verify_artifact_writer(state_path=state,
                writer_ledger_path=Path(info["approved_writer_ledger"]),
                output_role="operational_checkpoint", output_path=Path(path))
        except ValueError as exc:
            _deny(Path(path).name, "approved operational checkpoint", sha256_file(Path(path)), str(exc))


def register_operational_write(path):
    state = _operational_state(path)
    if state is None:
        return
    info = load_runtime_state(state)
    root = Path(info["skill_root"]).resolve() / "scripts"
    frame = inspect.currentframe().f_back
    owner = None
    try:
        while frame:
            candidate = Path(frame.f_code.co_filename).resolve()
            cost_scope=Path(path).resolve().is_relative_to(state.resolve().parent/'staging/cost/operational')
            if candidate.parent == root and candidate.name in OPERATIONAL_WRITERS and (candidate.name!='cost_metrics.py' or cost_scope):
                owner = candidate.name
                break
            frame = frame.f_back
    finally:
        del frame
    if owner is None:
        raise RuntimeAuthorizationError("untrusted_writer", "checkpoint requires a released caller")
    from artifact_provenance import register_protected_artifact
    register_protected_artifact(state_path=state,
        writer_ledger_path=Path(info["approved_writer_ledger"]), writer_script_id=owner,
        output_role="operational_checkpoint", output_path=Path(path))


def write_operational(path, raw):
    """Commit through a registered intent so interrupted writes can be replayed exactly."""
    state = _operational_state(path)
    if state is None:
        return False
    check_operational_before_write(path)
    from storage_contract import prepare_staging_payload
    raw=prepare_staging_payload(path,raw)
    import base64
    import hashlib
    import strict_json as json
    from storage_contract import write_integrity_artifact
    from artifact_provenance import register_protected_artifact
    info = load_runtime_state(state)
    root = state.resolve().parent
    target = Path(path).resolve()
    if target.is_file() and target.read_bytes()==raw:
        return True
    frame = inspect.currentframe().f_back
    owner = None
    try:
        while frame:
            candidate=Path(frame.f_code.co_filename).resolve()
            cost_scope=target.is_relative_to(root/'staging/cost/operational')
            if candidate.parent == Path(info['skill_root']).resolve()/'scripts' and candidate.name in OPERATIONAL_WRITERS and (candidate.name!='cost_metrics.py' or cost_scope):
                owner=candidate.name;break
            frame=frame.f_back
    finally:
        del frame
    if owner is None:
        raise RuntimeAuthorizationError('untrusted_writer','operational write requires released caller')
    payload={'task_run_id':info['task_run_id'], 'target':target.relative_to(root).as_posix(),
        'previous_sha256':sha256_file(target) if target.is_file() else '',
        'next_sha256':hashlib.sha256(raw).hexdigest(), 'content':base64.b64encode(raw).decode('ascii')}
    encoded=json.dumps(payload,sort_keys=True).encode('utf8')
    intent=root/'staging/operational-transactions/operational'/('intent-'+hashlib.sha256(encoded).hexdigest()+'.json')
    if not intent.exists():write_integrity_artifact(intent,encoded,transaction_id='checkpoint-intent')
    elif intent.read_bytes()!=encoded:raise ValueError('operational_intent_conflict')
    ledger=Path(info['approved_writer_ledger'])
    register_protected_artifact(state_path=state,writer_ledger_path=ledger,writer_script_id=owner,
        output_role='operational_intent',output_path=intent)
    _finish_operational(state,intent)
    return True


def _finish_operational(state, intent):
    import base64
    import hashlib
    import os
    import strict_json as json
    from run_paths import safe_run_relative_path
    from storage_contract import write_integrity_artifact
    from artifact_provenance import register_protected_artifact, verify_artifact_writer
    info=load_runtime_state(state);ledger=Path(info['approved_writer_ledger'])
    verify_artifact_writer(state_path=state,writer_ledger_path=ledger,output_role='operational_intent',output_path=intent)
    pending=json.loads(intent.read_text(encoding='utf8'))
    if pending.get('task_run_id')!=info['task_run_id']:raise ValueError('operational_intent_task_mismatch')
    target=safe_run_relative_path(pending['target'],state.resolve().parent)
    if _operational_state(target)!=state:raise ValueError('operational_intent_scope_invalid')
    raw=base64.b64decode(pending['content'],validate=True)
    if hashlib.sha256(raw).hexdigest()!=pending['next_sha256']:raise ValueError('operational_intent_content_changed')
    actual=sha256_file(target) if target.is_file() else ''
    if actual not in (pending['previous_sha256'],pending['next_sha256']):
        _deny(target.name,pending['previous_sha256'],actual)
    if actual!=pending['next_sha256']:
        temporary=target.with_name('.operational-'+pending['next_sha256']+'.json')
        if not temporary.exists():write_integrity_artifact(temporary,raw,transaction_id='checkpoint-commit')
        elif temporary.read_bytes()!=raw:raise ValueError('operational_temporary_conflict')
        try:
            os.replace(temporary,target)
            from storage_contract import fsync_directory
            fsync_directory(target.parent)
        finally:
            if temporary.exists(): temporary.unlink()
    register_protected_artifact(state_path=state,writer_ledger_path=ledger,writer_script_id='protected_runtime.py',
        output_role='operational_checkpoint',output_path=target,input_paths={'operational_intent':intent})


def recover_operational(state_path):
    """Only intents already accepted by the approved-writer chain may be resumed."""
    from artifact_provenance import read_writer_ledger, _event_output_path
    state_path=Path(state_path);state=load_runtime_state(state_path)
    events=read_writer_ledger(Path(state['approved_writer_ledger']),task_run_id=state['task_run_id'])
    committed={event.get('inputs',{}).get('operational_intent',{}).get('sha256') for event in events
               if event.get('output_role')=='operational_checkpoint'}
    for event in events:
        if event.get('output_role')=='operational_intent' and event['output_sha256'] not in committed:
            _finish_operational(state_path,_event_output_path(event,state_path.resolve().parent))


def operational_origin(state_path, output_path):
    """Resolve the initiating released writer through an exact committed intent."""
    import strict_json as json
    from artifact_provenance import verify_artifact_writer, _artifact_record_path
    state_path=Path(state_path);output_path=Path(output_path)
    state=load_runtime_state(state_path);ledger=Path(state['approved_writer_ledger'])
    event=verify_artifact_writer(state_path=state_path,writer_ledger_path=ledger,
        output_role='operational_checkpoint',output_path=output_path)
    if event['writer_script_id']!='protected_runtime.py':return event['writer_script_id']
    binding=event.get('inputs',{}).get('operational_intent')
    if not isinstance(binding,dict):raise ValueError('operational_origin_missing')
    intent=_artifact_record_path(binding,state_path)
    origin=verify_artifact_writer(state_path=state_path,writer_ledger_path=ledger,
        output_role='operational_intent',output_path=intent)
    pending=json.loads(intent.read_text(encoding='utf8'))
    if (sha256_file(intent)!=binding.get('sha256')
        or pending['target']!=output_path.resolve().relative_to(state_path.resolve().parent).as_posix()
        or pending['next_sha256']!=event['output_sha256']):
        raise ValueError('operational_origin_binding_mismatch')
    return origin['writer_script_id']


def verify_before_action(state_path):
    """Re-read actual files. Never adopt unknown files or repair their hashes."""
    from artifact_provenance import read_writer_ledger, _event_output_path
    from detailed_run_log import assert_state_log_alignment
    from runtime_guard import verify_release_manifest
    state_path = Path(state_path)
    state = load_runtime_state(state_path)
    root = state_path.resolve().parent
    release = verify_release_manifest(Path(state["skill_root"]),
        Path(state["release_manifest"]), task_run_id=state["task_run_id"])
    alignment = assert_state_log_alignment(Path(state["detailed_log"]), state)
    if alignment["status"] != "valid":
        _deny(state_path.name, "state/log binding", sha256_file(state_path))
    recover_operational(state_path)
    try:
        entries = read_writer_ledger(Path(state["approved_writer_ledger"]),
                                     task_run_id=state["task_run_id"])
    except ValueError as exc:
        _deny("writer_ledger", "valid append-only chain", str(exc))
    import strict_json as json
    ledger = Path(state["approved_writer_ledger"])
    head_path = ledger.with_name(ledger.name + ".head.json")
    if not head_path.is_file():
        _deny(ledger.name, "approved writer ledger head", "missing")
    head = json.loads(head_path.read_text(encoding="utf-8"))
    if head != {"bytes": ledger.stat().st_size, "sha256": sha256_file(ledger)}:
        _deny(ledger.name, head.get("sha256", ""), sha256_file(ledger))
    latest = {}
    for event in entries:
        path = _event_output_path(event, root)
        writer = event.get("writer_script_id")
        if (event.get("writer_script_sha256") != release["files"].get("scripts/" + str(writer))
                or event.get("output_role") not in
                WRITER_PERMISSIONS.get(writer, {}).get(event.get("authorized_phase"), set())):
            _deny(path.name, "approved released writer", str(writer))
        latest[path] = event
    for path, event in latest.items():
        actual = sha256_file(path) if path.is_file() else ""
        if actual != event.get("output_sha256"):
            _deny(path.relative_to(root), event.get("output_sha256"), actual,
                  event.get("writer_script_id", ""))
        if event.get('output_role')=='operational_checkpoint':
            from storage_contract import read_staging_bytes
            try: read_staging_bytes(path)
            except ValueError as exc: _deny(path.name,'intact confidential reference',str(exc))
    # Missing registrations must fail too, including a newly hand-written score.
    from run_research import PATHS
    formal_keys = {"search", "refreshed", "capture", "sources", "raw", "canonical_sources",
        "canonical_evidence", "corrections", "locator", "collision", "admission", "semantic",
        "formal", "reviewed", "review_changes", "audit", "freeze", "platform", "scores",
        "truth", "report_data", "formal_limitations", "preflight", "validation", "errors", "seal", "score_gate"}
    for key in formal_keys:
        path = (root / PATHS[key]).resolve()
        if path.is_file() and path not in latest:
            _deny(PATHS[key], "registered writer event", sha256_file(path))
    for folder in ("staging/pipeline/operational", "staging/tool-facts",
                   "staging/dispatch/operational", "staging/helpers/operational", "staging/cost/operational"):
        for path in (root / folder).rglob("*.json"):
            if _operational_state(path) is not None and path.resolve() not in latest:
                _deny(path.relative_to(root), "registered operational event", sha256_file(path))
    if (root / ".query-plans/registry.json").exists():
        import csv
        from execution_facts import load_registered_queries
        from retrieval_controls import execution_schema_context_from_state
        registered = load_registered_queries(execution_schema_context_from_state(state, state_path=state_path))
        for path in (root / "staging").glob("plan-*.csv"):
            with path.open(encoding="utf-8-sig", newline="") as stream:
                planned = list(csv.DictReader(stream))
            for row in planned:
                original = registered.get((row.get("plan_id"), row.get("query_id")))
                if original is None or any(original.get(key) != value for key, value in row.items()):
                    _deny(path.relative_to(root), "registered query definition", sha256_file(path))
            if not planned:
                _deny(path.relative_to(root), "non-empty committed plan", sha256_file(path))
            plan_ids = {row['plan_id'] for row in planned}
            if len(plan_ids) != 1 or {row['query_id'] for row in planned} != {
                    query for (plan, query) in registered if plan in plan_ids}:
                _deny(path.relative_to(root), "complete registered plan", sha256_file(path))
    for pattern in ("*.docx", "*.xlsx"):
        for path in root.rglob(pattern):
            if path.resolve() not in latest and "staging" not in path.relative_to(root).parts:
                _deny(path.relative_to(root), "registered Office builder", sha256_file(path))
    return {"status": "valid", "checked_artifacts": len(latest), "task_run_id": state["task_run_id"]}
