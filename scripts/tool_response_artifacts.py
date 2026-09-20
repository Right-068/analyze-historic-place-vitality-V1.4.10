"""Controlled page-tool response registration; no network attestation claim.

Only a page-tool adapter supplies response text and its actual tool reference.
An arbitrary existing pathname is not an import format. Formal registration
uses the existing SEARCH authorization and protected writer ledger.
"""
from pathlib import Path
import hashlib
import os
import strict_json as json
from input_safety import InputError, redact_text
from process_lock import ProcessFileLock

IDENTITY = ('task_run_id', 'plan_id', 'query_id', 'execution_id')


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _binding(record, raw_hash):
    from public_network import normalize_tool_trace
    if not isinstance(record, dict) or any(
            not isinstance(record.get(k), str) or not record[k]
            for k in (*IDENTITY, 'url', 'retrieved_at')):
        raise InputError('tool_artifact_identity_missing')
    if not isinstance(record.get('tool_trace'), dict):
        raise InputError('tool_artifact_trace_required')
    trace = normalize_tool_trace(record['tool_trace'], url=record['url'],
        final_url=record.get('final_url') or record['url'], retrieved_at=record['retrieved_at'])
    trace['page_title'] = redact_text(trace['page_title'])
    return dict(schema_version='tool-response-artifact-1',
        **{k: record[k] for k in IDENTITY}, tool_trace=trace, response_sha256=raw_hash)


def _resolved_matches(path):
    resolved = str(path.resolve())
    # Windows can retain the extended prefix when a missing descendant becomes
    # present between its two GetFinalPathName calls. This changes spelling,
    # not identity; keep the resolved target and all link/escape checks intact.
    if os.name == 'nt':
        if resolved.startswith('\\\\?\\UNC\\'):
            resolved = '\\\\' + resolved[8:]
        elif resolved.startswith('\\\\?\\') and len(resolved) > 6 and resolved[5:7] == ':\\':
            resolved = resolved[4:]
    return Path(resolved) == path.absolute()


def _paths(root, binding, storage_class=None):
    root = Path(root).absolute()
    if not _resolved_matches(root):
        raise InputError('tool_artifact_path_escape')
    trace = binding['tool_trace']
    identity = [binding['task_run_id'], trace['tool_name'], trace['tool_call_id'], trace['result_ref']]
    if not trace['tool_call_id'] or not trace['result_ref']:
        # A machine registration identity is not a fabricated host call/reference.
        # Keep legacy registrations byte-compatible when both host IDs exist.
        identity.extend([binding[k] for k in IDENTITY[1:]])
        identity.extend([trace['request_url'], trace['final_url'], trace['retrieved_at'],
                         binding['response_sha256']])
    owner = root / '.tool-responses' / ('TA-' + _sha(_bytes(identity)))
    if storage_class is None:
        choices=[p for p in (owner/'public-raw',owner/'.private-response') if (p/'registration.json').is_file()]
        if len(choices)>1: raise InputError('tool_artifact_storage_ambiguous')
        directory=choices[0] if choices else owner/'public-raw'
    else:
        directory=owner/('.private-response' if storage_class=='confidential' else 'public-raw')
        other=owner/('public-raw' if storage_class=='confidential' else '.private-response')
        if (other/'response.bin').exists() or (other/'registration.json').exists():
            raise InputError('artifact_hash_conflict')
    for path in (root / '.tool-responses', owner, directory, directory/'response.bin', directory/'registration.json'):
        if path.is_symlink() or not _resolved_matches(path):
            raise InputError('tool_artifact_path_escape')
    return directory/'response.bin', directory/'registration.json'


def register_response(*, artifact_root, record, response_text, state_path=None, writer_ledger_path=None):
    """Register the actual page result, not a source filename or researcher prose."""
    if not isinstance(response_text, str):
        raise InputError('tool_response_text_required')
    raw = response_text.encode('utf-8')
    binding = _binding(record, _sha(raw))
    from storage_contract import raw_storage_class, write_raw_artifact
    raw_path, registration = _paths(artifact_root, binding, raw_storage_class(raw))
    if state_path is not None:
        from runtime_guard import authorize_runtime_write
        authorization = authorize_runtime_write(state_path=state_path,
            writer_script_id='source_capture.py', output_role='tool_response_artifact',
            expected_phase='SEARCH', output_path=registration)
        if authorization['task_run_id'] != binding['task_run_id'] or writer_ledger_path is None:
            raise InputError('tool_artifact_task_mismatch')
    elif writer_ledger_path is not None:
        raise InputError('tool_artifact_state_required')
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(raw_path.parent/'registration-lock'):
        # Private immutable files use the same fsync/atomic replacement machinery
        # as capture responses. Retrying an interrupted registration is idempotent.
        write_raw_artifact(raw_path, raw, transaction_id='tool-response')
        write_raw_artifact(registration, _bytes(binding), transaction_id='tool-registration')
        if state_path is not None:
            from artifact_provenance import register_protected_artifact
            register_protected_artifact(state_path=state_path, writer_ledger_path=writer_ledger_path,
                writer_script_id='source_capture.py', output_role='tool_response_artifact',
                output_path=registration, input_paths={'tool_response':raw_path},
                expected_phase='SEARCH', atomic_registration=True)
    return {'raw_response_file':str(raw_path), 'tool_artifact':_bytes(binding).decode(),
            'response_status':'empty_page' if not raw.strip() else 'readable'}


def verify_binding(value, observation):
    if not isinstance(value, str):
        raise InputError('tool_artifact_registration_required')
    binding = json.loads(value)
    if not isinstance(binding, dict) or set(binding) != {'schema_version', *IDENTITY, 'tool_trace', 'response_sha256'}:
        raise InputError('tool_artifact_registration_invalid')
    trace = {k: observation[k] for k in __import__('public_network').TOOL_TRACE_FIELDS}
    trace['schema_version'] = 'tool-page-trace-1'
    expected = _binding({**{k:observation[k] for k in IDENTITY}, 'tool_trace':trace,
        'url':trace['request_url'], 'final_url':trace['final_url'], 'retrieved_at':trace['retrieved_at']},
        observation['response_body_sha256'])
    if binding != expected:
        raise InputError('tool_artifact_binding_mismatch')
    return binding


def read_registered(*, artifact_root, record=None, observation=None, state_path=None, writer_ledger_path=None):
    if artifact_root is None:
        raise InputError('tool_artifact_registration_required')
    if observation is not None:
        binding = verify_binding(observation.get('tool_artifact'), observation)
    else:
        if not isinstance(record, dict):
            raise InputError('tool_artifact_record_required')
        if not record.get('raw_response_file'):
            raise InputError('tool_response_artifact_required')
        try:
            binding = json.loads(record.get('tool_artifact', ''))
            expected = _binding(record, binding['response_sha256'])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError('tool_artifact_registration_required') from exc
        if binding != expected:
            raise InputError('tool_artifact_binding_mismatch')
    raw_path, registration = _paths(artifact_root, binding)
    if record is not None:
        supplied = Path(record['raw_response_file'])
        if '..' in supplied.parts or supplied.absolute() != raw_path or not _resolved_matches(supplied):
            raise InputError('tool_artifact_path_mismatch')
    if not registration.is_file() or registration.read_bytes() != _bytes(binding):
        raise InputError('tool_artifact_registration_missing_or_changed')
    if not raw_path.is_file():
        raise InputError('tool_response_artifact_missing')
    raw = raw_path.read_bytes()
    if _sha(raw) != binding['response_sha256']:
        raise InputError('tool_response_artifact_changed')
    if state_path is not None:
        from artifact_provenance import verify_artifact_writer
        event = verify_artifact_writer(state_path=state_path, writer_ledger_path=writer_ledger_path,
            output_role='tool_response_artifact', output_path=registration)
        if event['writer_script_id'] != 'source_capture.py' or event['task_run_id'] != binding['task_run_id']:
            raise InputError('tool_artifact_writer_mismatch')
    return raw, binding
