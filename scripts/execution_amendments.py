"""Append-only execution amendments; observation facts cannot be invented here."""
from __future__ import annotations
import base64
import csv
import io
import strict_json as json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from process_lock import ProcessFileLock
from runtime_guard import canonical_sha256, sha256_file
from temporal_fields import timestamp

DERIVED_COUNTS = {'new_scored_evidence_units', 'cumulative_scored_evidence_units'}


def record_hash(row):
    return canonical_sha256({k: v for k, v in row.items() if not k.startswith('_') and k not in DERIVED_COUNTS})


def ledger_path(root):
    return Path(root)/'.execution-events/events.json'


def read_events(root):
    path = ledger_path(root)
    if not path.exists():
        return []
    from run_paths import safe_run_relative_path
    from artifact_provenance import verify_artifact_writer
    payload = json.loads(path.read_text(encoding='utf-8'))
    verify_artifact_writer(state_path=safe_run_relative_path(payload['state_file'], Path(root)),
        writer_ledger_path=safe_run_relative_path(payload['writer_ledger'], Path(root)),
        output_role='execution_events', output_path=path)
    events = payload['events']
    previous, ids = '', set()
    for event in events:
        if event['event_id'] in ids or event['previous_event_sha256'] != previous:
            raise ValueError('execution_amendment_chain_conflict')
        core = {k: v for k, v in event.items() if k != 'event_sha256'}
        if canonical_sha256(core) != event['event_sha256']:
            raise ValueError('execution_amendment_hash_mismatch')
        archive = path.parent/'archive'/(event['event_sha256']+'.json')
        if json.loads(archive.read_text(encoding='utf-8')) != event:
            raise ValueError('execution_amendment_archive_mismatch')
        timestamp(event['event_time'])
        previous = event['event_sha256']
        ids.add(event['event_id'])
    return events


def apply_events(rows, events, task_run_id):
    """Resolve one successor chain without adding another actual tool invocation."""
    by_id = {str(row.get('execution_id')): dict(row) for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('duplicate_execution_id')
    retracted, heads, head_events = set(), {}, {}
    for event in events:
        if event['task_run_id'] != task_run_id:
            raise ValueError('execution_amendment_task_mismatch')
        if event['event_type'] == 'correction_rejected':
            continue
        identity = event['target_execution_id']
        if identity not in by_id:
            raise ValueError('execution_amendment_original_missing')
        old, new = event['original_record'], event['effective_record']
        if record_hash(old) != event['old_record_sha256'] or record_hash(new) != event['new_record_sha256']:
            raise ValueError('execution_amendment_record_hash_mismatch')
        head = heads.get(identity, event['old_record_sha256'])
        if (head != event['old_record_sha256'] or identity in retracted or
                event.get('previous_effective_event_id', '') != head_events.get(identity, '')):
            raise ValueError('execution_amendment_fork_or_reopen')
        # A refreshed CSV may already contain the effective record. It must be one
        # of this chain's exact archived members; never an arbitrary patched row.
        allowed = {record_hash(e['original_record']) for e in events if e.get('target_execution_id') == identity and 'original_record' in e}
        allowed |= {record_hash(e['effective_record']) for e in events if e.get('target_execution_id') == identity and 'effective_record' in e}
        if record_hash(by_id[identity]) not in allowed:
            raise ValueError('execution_amendment_input_changed')
        counts = {k: by_id[identity].get(k, '0') for k in DERIVED_COUNTS}
        by_id[identity] = {**new, **counts}
        heads[identity] = event['new_record_sha256']
        head_events[identity] = event['event_id']
        if event['event_type'] == 'retract':
            retracted.add(identity)
    return list(by_id.values()), retracted


def reconstruct_record(original, plan):
    """Only missing deterministic metadata can be restored from a committed plan."""
    from execution_facts import QUERY_FIELDS, execution_binding
    from execution_schema import basic_errors
    row = dict(original)
    for field in (*QUERY_FIELDS, 'query_definition_sha256', 'plan_row_sha256'):
        expected = str(plan.get(field, ''))
        if row.get(field) not in ('', None, expected):
            raise ValueError('execution_amendment_cannot_change_query_identity:' + field)
        row[field] = expected
    row.setdefault('search_id', row['execution_id'])
    if not row['search_id']:
        row['search_id'] = row['execution_id']
    from execution_semantics import route_id
    for field, value in {'route_id': route_id(plan), 'state_updated_at': row.get('finished_at'),
                         'record_type': 'execution', 'iteration_round': str(plan.get('planned_iteration_round', ''))}.items():
        if row.get(field) not in ('', None, value):
            raise ValueError('execution_amendment_cannot_change_generated_fact:' + field)
        row[field] = value
    row['execution_binding_sha256'] = execution_binding(row)
    errors = basic_errors(row)
    if errors:
        raise ValueError('execution_amendment_observation_unprovable:' + ';'.join(errors))
    return row


def retracted_sources(context, sources):
    if not context or not context.get('_run_root'):
        return set()
    ids = {event['target_execution_id'] for event in read_events(context['_run_root']) if event['event_type'] == 'retract'}
    return {row['source_id'] for row in sources if str(row.get('execution_id')) in ids}


def exclude_retracted_evidence(rows, sources, context):
    """Derived eligibility overlay; canonical source content is never rewritten."""
    excluded = retracted_sources(context, sources)
    return [{**row, 'used_for_scoring': 'false', 'score_scope': 'not_scored'}
        if row.get('source_id') in excluded else row for row in rows]


def _finish(journal, *, state_path, writer_ledger_path, fault_at=''):
    from execution_facts import atomic_bytes
    from artifact_provenance import register_protected_artifact
    payload = json.loads(journal.read_text(encoding='utf-8'))
    core = {k: v for k, v in payload.items() if k != 'transaction_sha256'}
    if canonical_sha256(core) != payload['transaction_sha256']:
        raise ValueError('execution_amendment_transaction_changed')
    path = ledger_path(state_path.parent)
    data = base64.b64decode(payload['content_base64'], validate=True)
    import hashlib
    if path.exists() and sha256_file(path) not in {payload['old_sha256'], hashlib.sha256(data).hexdigest()}:
        raise ValueError('execution_amendment_prefix_changed')
    if payload['state_file'] != str(state_path.resolve()) or payload['writer_ledger'] != str(writer_ledger_path.resolve()):
        raise ValueError('execution_amendment_transaction_identity_mismatch')
    search = Path(payload['search_file']).resolve()
    if not search.is_relative_to(state_path.resolve().parent):
        raise ValueError('execution_amendment_search_path_escape')
    if sha256_file(search) != payload['search_sha256']:
        raise ValueError('execution_amendment_search_input_changed')
    document = json.loads(data)
    for event in document['events']:
        archive = path.parent/'archive'/(event['event_sha256']+'.json')
        serialized = json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
        if archive.exists() and archive.read_bytes() != serialized:
            raise ValueError('execution_amendment_archive_conflict')
        if not archive.exists():
            atomic_bytes(archive, serialized)
    archive = path.parent/'transactions'/(payload['transaction_sha256']+'.json')
    if archive.exists() and archive.read_bytes() != journal.read_bytes():
        raise ValueError('execution_amendment_transaction_archive_conflict')
    atomic_bytes(archive, journal.read_bytes())
    atomic_bytes(path, data)
    if fault_at == 'after_event_replace':
        raise RuntimeError('injected amendment interruption')
    register_protected_artifact(state_path=state_path, writer_ledger_path=writer_ledger_path,
        writer_script_id='execution_facts.py', output_role='execution_events', output_path=path,
        expected_phase='SEARCH', input_paths={'execution_amendment_transaction': archive},
        provenance_context={'transaction_id': payload['transaction_sha256']}, atomic_registration=True)
    if fault_at == 'after_event_registration':
        raise RuntimeError('injected amendment interruption')
    journal.unlink()


def commit(requests, *, state_path, writer_ledger_path, search_path, fault_at=''):
    from execution_facts import atomic_bytes, read_rows, load_registered_queries, context_from_state
    from retrieval_controls import load_retrieval_config
    from artifact_provenance import verify_artifact_writer
    from run_paths import logical_path
    if not isinstance(requests, (list, tuple)) or any(not isinstance(r, Mapping) for r in requests):
        raise ValueError('execution_amendment_requests_must_be_objects')
    root = state_path.resolve().parent
    path = ledger_path(root)
    journal = path.with_suffix('.transaction.json')
    from execution_coordinator import coordinated
    with coordinated(state_path=state_path, writer_ledger_path=writer_ledger_path, fault_at=fault_at), ProcessFileLock(search_path), ProcessFileLock(path):
        if journal.exists():
            _finish(journal, state_path=state_path, writer_ledger_path=writer_ledger_path, fault_at=fault_at)
        verify_artifact_writer(state_path=state_path, writer_ledger_path=writer_ledger_path,
            output_role='search_log', output_path=search_path)
        state = json.loads(state_path.read_text(encoding='utf-8-sig'))
        context = context_from_state(state, config=load_retrieval_config(), state_path=state_path)
        queries = load_registered_queries(context)
        events = read_events(root)
        rows = read_rows(search_path)
        rejected = []
        for request in requests:
            digest = canonical_sha256(request)
            request_id = request.get('event_id')
            if not isinstance(request_id, str) or not request_id.strip():
                request_id = 'REJECT-' + digest
            existing = next((e for e in events if e['event_id'] == request_id), None)
            if existing:
                if existing['request_sha256'] != digest:
                    raise ValueError('execution_amendment_event_identity_conflict')
                if existing['event_type'] == 'correction_rejected':
                    rejected.append(existing['rejection_reason'])
                continue
            active, retracted = apply_events(rows, events, context['task_run_id'])
            original = next((r for r in active if r.get('execution_id') == request.get('target_execution_id')), {})
            error = ''
            try:
                string_fields = ('event_id', 'event_type', 'target_execution_id', 'event_time',
                                 'reason', 'old_record_sha256', 'previous_effective_event_id')
                unknown = set(request) - set(string_fields) - {'updates', 'replacement'}
                if unknown:
                    raise ValueError('execution_amendment_field_not_allowed:' + ','.join(sorted(map(str, unknown))))
                for field in string_fields:
                    if not isinstance(request.get(field, ''), str):
                        raise ValueError('execution_amendment_field_type_invalid:' + field)
                timestamp(request.get('event_time'))
                if timestamp(request['event_time']) > datetime.now(timezone.utc):
                    raise ValueError('execution_amendment_time_in_future')
                if not request.get('event_id') or not str(request.get('reason', '')).strip():
                    raise ValueError('execution_amendment_identity_or_reason_missing')
                if request.get('event_type') not in {'correction', 'supersede', 'retract'} or not original:
                    raise ValueError('execution_amendment_type_or_target_invalid')
                if original['execution_id'] in retracted:
                    raise ValueError('execution_amendment_retract_is_terminal')
                if request.get('old_record_sha256') != record_hash(original):
                    raise ValueError('execution_amendment_stale_or_forked_request')
                previous_head = next((e['event_id'] for e in reversed(events)
                    if e['target_execution_id'] == original['execution_id'] and e['event_type'] != 'correction_rejected'), '')
                if request.get('previous_effective_event_id', '') != previous_head:
                    raise ValueError('execution_amendment_successor_conflict')
                if previous_head and timestamp(request['event_time']) < timestamp(next(
                        e['event_time'] for e in events if e['event_id'] == previous_head)):
                    raise ValueError('execution_amendment_before_previous_event')
                if timestamp(request['event_time']) < timestamp(original['finished_at']):
                    raise ValueError('execution_amendment_before_execution')
                if request.get('updates') or request.get('replacement'):
                    raise ValueError('execution_amendment_untrusted_observation_or_identity')
                new = (dict(original) if request['event_type'] == 'retract' else
                    reconstruct_record(original, queries[(original['plan_id'], original['query_id'])]))
            except (ValueError, KeyError) as exc:
                error, new = str(exc), dict(original)
                rejected.append(error)
            from artifact_provenance import utc_now
            event = {'event_id': request_id,
                'event_type': 'correction_rejected' if error else request['event_type'],
                'event_time': utc_now() if error else request['event_time'],
                'reason': str(request.get('reason', '')), 'rejection_reason': error,
                'request_sha256': digest, 'task_run_id': context['task_run_id'],
                'plan_id': original.get('plan_id', ''), 'query_id': original.get('query_id', ''),
                'target_execution_id': str(request.get('target_execution_id', '')),
                'previous_effective_event_id': str(request.get('previous_effective_event_id', '')),
                'original_record': original, 'effective_record': new,
                'old_record_sha256': record_hash(original), 'new_record_sha256': record_hash(new),
                'previous_event_sha256': events[-1]['event_sha256'] if events else ''}
            event['event_sha256'] = canonical_sha256(event)
            events.append(event)
        document = {'schema': 'execution-amendments-1', 'task_run_id': context['task_run_id'],
            'state_file': logical_path(state_path, root), 'writer_ledger': logical_path(writer_ledger_path, root), 'events': events}
        data = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
        if not path.exists() or path.read_bytes() != data:
            payload = {'state_file': str(state_path.resolve()), 'writer_ledger': str(writer_ledger_path.resolve()),
                'search_file': str(search_path.resolve()), 'search_sha256': sha256_file(search_path),
                'old_sha256': sha256_file(path) if path.exists() else '',
                'content_base64': base64.b64encode(data).decode('ascii')}
            payload['transaction_sha256'] = canonical_sha256(payload)
            atomic_bytes(journal, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
            if fault_at == 'after_event_prepare':
                raise RuntimeError('injected amendment interruption')
            _finish(journal, state_path=state_path, writer_ledger_path=writer_ledger_path, fault_at=fault_at)
        if rejected:
            raise ValueError('execution_amendment_rejected:' + ';'.join(rejected))
        return {'status': 'recorded', 'event_count': len(events), 'amendments_sha256': sha256_file(path)}
