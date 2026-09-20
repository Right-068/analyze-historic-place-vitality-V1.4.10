#!/usr/bin/env python3
"""Committed query identities and order-independent execution facts."""
from __future__ import annotations

import csv
import io
import base64
import hashlib
import strict_json as json
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from process_lock import ProcessFileLock
from runtime_guard import canonical_sha256, sha256_file
from execution_schema import PLAN_FIELDS, basic_errors, validate_observation
from temporal_fields import timestamp

REGISTRY_SCHEMA = 'committed-query-registry-1'
CONTEXT_SCHEMA = 'execution-schema-context-1'
SOURCE_BINDING_FIELDS = (
    'execution_id', 'plan_id', 'query_definition_sha256', 'plan_row_sha256',
    'execution_binding_sha256', 'normalized_query_intent', 'query_dimension_targets',
    'planned_iteration_round', 'iteration_round', 'iteration_mode',
    'gap_target', 'target_confidence', 'dimension_confidence_before_round',
    'audit_snapshot_sha256', 'plan_binding_sha256',
)
QUERY_FIELDS = PLAN_FIELDS
BINDING_PARAMETER_FIELDS = (
    'task_run_id', 'place', 'place_identity_sha256', 'alias_set_sha256',
    'building_set_sha256', 'local_term_set_sha256', 'iteration_round',
    'gap_targets', 'enhancement_targets', 'audit_sha256', 'state_sha256',
    'history_sha256', 'audit_machine_binding', 'retrieval_control_sha256',
    'generator_sha256', 'audit_snapshot',
    'temporal_binding',
)
_COUNTS = ('returned_results', 'opened_pages', 'relevant_pages', 'duplicate_pages',
           'new_scored_evidence_units', 'cumulative_scored_evidence_units')


class ExecutionContext(dict):
    """Portable public facts with an explicit, non-serialized runtime location."""
    def __init__(self, value=(), *, run_root=None):
        super().__init__((k, v) for k, v in dict(value).items() if not k.startswith('_'))
        self.run_root = str(run_root or getattr(value, 'run_root', '') or dict(value).get('_run_root', ''))

    def get(self, key, default=None):
        return (self.run_root or default) if key == '_run_root' else super().get(key, default)

    def __getitem__(self, key):
        return self.run_root if key == '_run_root' else super().__getitem__(key)


def bind_runtime_context(stored: Mapping[str, object], state_path: Path) -> ExecutionContext:
    from retrieval_controls import load_retrieval_config
    actual = context_from_state(json.loads(state_path.read_text(encoding='utf-8-sig')),
                                config=load_retrieval_config(), state_path=state_path)
    if dict(actual) != {k: v for k, v in stored.items() if not k.startswith('_')}:
        raise ValueError('execution_schema_context_state_mismatch')
    return actual


def text(value: object) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())


def integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError('invalid_nonnegative_integer')
    result = int(str(value))
    if result < minimum or result > 2**53 - 1:
        raise ValueError('integer_out_of_range')
    return result


def utc(value: object) -> datetime:
    raw = str(value or '')
    if not re.match(r'^\d{4}-\d{2}-\d{2}T', raw):
        raise ValueError('execution_time_invalid')
    try:
        result = timestamp(raw)
    except (ValueError, OverflowError) as exc:
        raise ValueError('execution_time_invalid') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('execution_time_timezone_missing')
    try:
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError('execution_time_invalid') from exc


def event_key(row: Mapping[str, object]) -> tuple:
    """Logical order: observed time, causal identity and retry, never arrival order."""
    return (utc(row.get('state_updated_at') or row.get('finished_at')),
            str(row.get('task_run_id', '')), str(row.get('plan_id', '')),
            str(row.get('query_id', '')), str(row.get('query_definition_sha256', '')),
            integer(row.get('retry_number')), utc(row.get('started_at')),
            utc(row.get('finished_at')), str(row.get('execution_id', '')))


def set_hash(rows: Iterable[Mapping[str, object]]) -> str:
    return canonical_sha256(sorted(
        [{str(k): v for k, v in row.items() if not str(k).startswith('_') and k != 'event_sequence'} for row in rows],
        key=canonical_sha256))


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_rows(path: Path) -> list[dict[str, object]]:
    if path.suffix == '.json':
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
            raise ValueError('query_plan_content_invalid')
        return value
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def query_definition(row: Mapping[str, object]) -> dict[str, object]:
    from retrieval_controls import normalized_query_intent
    result = {k: text(row.get(k, '')) for k in QUERY_FIELDS}
    result['place'] = text(row.get('place')).casefold()
    result['planned_iteration_round'] = str(integer(row.get('planned_iteration_round', 0)))
    result['query_dimension_targets'] = sorted(json.loads(str(row.get('query_dimension_targets', '[]'))))
    result['normalized_query_intent'] = normalized_query_intent(row)
    result['language'] = text(row.get('language') or 'zh').casefold()
    return result


def finalize_plan_rows(rows: list[dict[str, str]], context: Mapping[str, object]) -> list[dict[str, str]]:
    from retrieval_controls import normalized_query_intent, validate_query_dimension_targets
    plan_id = 'PLAN-' + str(context['parameter_binding_sha256'])[:24]
    result = []
    seen = set()
    for original in rows:
        row = dict(original)
        if not text(row.get('query_id')) or row['query_id'] in seen or not text(row.get('query')):
            raise ValueError('query_identity_conflict: missing or duplicate plan query')
        seen.add(row['query_id'])
        row.update(task_run_id=str(context['task_run_id']), plan_id=plan_id,
                   place=str(context['place']), place_identity_sha256=str(context['place_identity_sha256']),
                   planned_iteration_round=str(context['iteration_round']))
        row.setdefault('iteration_round', str(context['iteration_round']))
        row.setdefault('iteration_mode', '')
        if validate_query_dimension_targets(row)['status'] != 'valid':
            raise ValueError('query_dimension_targets_invalid')
        row.setdefault('source_category_target', 'mixed')
        row.setdefault('language', 'zh')
        row.setdefault('retrieval_entry', '')
        row['plan_binding_sha256'] = str(context['parameter_binding_sha256'])
        snapshot = context.get('audit_snapshot') or {}
        row['audit_snapshot_sha256'] = canonical_sha256(snapshot) if snapshot else ''
        row['target_confidence'] = str(snapshot.get('target_confidence', ''))
        if int(context['iteration_round']) > 0:
            if not text(row.get('gap_target')):
                raise ValueError('deep_plan_gap_target_missing')
            if not snapshot:
                raise ValueError('deep_plan_audit_snapshot_missing')
            targets = json.loads(row.get('query_dimension_targets') or '[]')
            states = snapshot['dimension_states']
            selected = {name: states[name]['confidence'] for name in targets}
            if any(not value for value in selected.values()):
                raise ValueError('deep_plan_prior_confidence_missing')
            row['dimension_confidence_before_round'] = (next(iter(selected.values()))
                if len(set(selected.values())) == 1 else json.dumps(selected, ensure_ascii=False, sort_keys=True))
            if not targets:
                row['dimension_confidence_before_round'] = json.dumps(
                    {name: item['confidence'] for name, item in states.items()}, ensure_ascii=False, sort_keys=True)
        else:
            row.setdefault('gap_target', '')
            row['dimension_confidence_before_round'] = ''
        if validate_query_dimension_targets(row)['status'] != 'valid':
            raise ValueError('query_dimension_targets_invalid')
        row['normalized_query_intent'] = normalized_query_intent(row)
        identity = query_definition(row)
        row['query_definition_sha256'] = canonical_sha256(identity)
        row['plan_row_sha256'] = canonical_sha256({k: v for k, v in row.items() if k != 'plan_row_sha256'})
        result.append(row)
    return result


def _safe_path(root: Path, raw: object) -> Path:
    from run_paths import safe_run_relative_path
    return safe_run_relative_path(raw, root)


def register_committed_plan(output: Path, *, run_root: Path, _registry_locked: bool = False) -> None:
    """Content-address both committed members before the registry is atomically appended."""
    sidecar = output.with_name(output.name + '.binding.json')
    if output.with_name(output.name + '.transaction.json').exists():
        raise ValueError('query_plan_not_committed')
    binding = json.loads(sidecar.read_text(encoding='utf-8-sig'))
    validate_plan_binding(binding)
    plan_data, binding_data = output.read_bytes(), sidecar.read_bytes()
    if binding.get('query_plan_sha256') != sha256_file(output):
        raise ValueError('query_plan_hash_mismatch')
    registry = run_root / '.query-plans' / 'registry.json'
    with nullcontext() if _registry_locked else ProcessFileLock(registry):
        entry = {
            'plan_id': 'PLAN-' + str(binding['parameter_binding_sha256'])[:24],
            'plan_file': f"plans/{sha256_file(output)}{output.suffix}",
            'plan_sha256': sha256_file(output),
            'binding_file': f"bindings/{sha256_file(sidecar)}.json",
            'binding_sha256': sha256_file(sidecar),
            'registered_at': __import__('temporal_fields').utc_now(),
        }
        payload = {'schema_version': REGISTRY_SCHEMA, 'task_run_id': binding['task_run_id'], 'plans': []}
        if registry.exists():
            payload = json.loads(registry.read_text(encoding='utf-8-sig'))
            if payload.get('task_run_id') != binding['task_run_id'] or payload.get('schema_version') != REGISTRY_SCHEMA:
                raise ValueError('query_registry_task_mismatch')
        previous = [e for e in payload['plans'] if e['plan_id'] == entry['plan_id']]
        if previous:
            entry['registered_at'] = previous[0]['registered_at']
            if previous != [entry]:
                raise ValueError('query_plan_identity_conflict')
        else:
            old_queries = {}
            for e in payload['plans']:
                if sha256_file(_safe_path(registry.parent, e['plan_file'])) != e['plan_sha256']:
                    raise ValueError('query_plan_hash_mismatch')
                for r in read_rows(_safe_path(registry.parent, e['plan_file'])):
                    old_queries[str(r['query_id'])] = str(r['query_definition_sha256'])
            for r in read_rows(output):
                if r['query_id'] in old_queries and old_queries[r['query_id']] != r['query_definition_sha256']:
                    raise ValueError('query_identity_conflict: query_id is already committed')
            payload['plans'].append(entry)
        for field, data in (('plan_file', plan_data), ('binding_file', binding_data)):
            target = _safe_path(registry.parent, entry[field])
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError('query_snapshot_hash_conflict')
            else:
                atomic_bytes(target, data)
        payload['plans'].sort(key=lambda e: e['plan_id'])
        atomic_bytes(registry, (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())


def precheck_plan_identities(rows: list[dict[str, str]], *, run_root: Path) -> None:
    registry = run_root / '.query-plans/registry.json'
    if not registry.exists():
        return
    payload = json.loads(registry.read_text(encoding='utf-8-sig'))
    if rows and payload.get('task_run_id') != rows[0].get('task_run_id'):
        raise ValueError('query_registry_task_mismatch')
    previous = {}
    for entry in payload['plans']:
        path = _safe_path(registry.parent, entry['plan_file'])
        if sha256_file(path) != entry['plan_sha256']:
            raise ValueError('query_plan_hash_mismatch')
        for record in read_rows(path):
            previous[record['query_id']] = record['query_definition_sha256']
    for row in rows:
        if row['query_id'] in previous and previous[row['query_id']] != row['query_definition_sha256']:
            raise ValueError('query_identity_conflict: query_id is already committed')


def anchor_state(state_path: Path) -> None:
    """Persist the actual state bytes once; later mutable phase updates do not change the anchor."""
    anchor = state_path.parent / '.query-plans' / 'state-anchor.json'
    with ProcessFileLock(anchor):
        if anchor.exists():
            return
        digest = sha256_file(state_path)
        relative = 'states/' + digest + '.json'
        atomic_bytes(anchor.parent / relative, state_path.read_bytes())
        atomic_bytes(anchor, json.dumps({'file': relative, 'sha256': digest}, sort_keys=True).encode())


def context_from_state(state: Mapping[str, object], *, config: Mapping[str, object],
                       state_path: Path | None = None, run_root: Path | None = None) -> dict[str, object]:
    if not isinstance(state, Mapping):
        raise ValueError('execution_schema_state_not_object')
    contract = config['execution_log_contract']
    run_id = text(state.get('task_run_id'))
    created = str(state.get('created_at', ''))
    revision = integer(state.get('schema_revision', 0))
    if not run_id:
        raise ValueError('execution_schema_state_identity_missing')
    created_time = utc(created)
    declaration = state.get('execution_log_contract')
    legacy = not isinstance(declaration, Mapping)
    if legacy:
        if revision > 5 or created_time > utc(contract['legacy_state_cutoff_utc']):
            raise ValueError('legacy_execution_schema_not_allowed')
        schema = str(contract['legacy_schema_version'])
    else:
        schema = str(declaration.get('schema_version', ''))
        if schema != contract['current_schema_version']:
            raise ValueError('execution_schema_unsupported')
    root = run_root or (state_path.parent if state_path else None)
    registry = root / '.query-plans' / 'registry.json' if root else None
    migration = root / '.query-plans' / 'migration.json' if root else None
    identity = {k: state.get(k) for k in ('task_run_id', 'place', 'created_at', 'schema_revision', 'execution_log_contract', 'research_cutoff')}
    state_digest = canonical_sha256(identity)
    anchor = root / '.query-plans' / 'state-anchor.json' if root else None
    if anchor and anchor.is_file():
        pointer = json.loads(anchor.read_text(encoding='utf-8-sig'))
        snapshot = _safe_path(anchor.parent, pointer['file'])
        if sha256_file(snapshot) != pointer['sha256']:
            raise ValueError('execution_schema_state_anchor_changed')
        snapshot_state = json.loads(snapshot.read_text(encoding='utf-8-sig'))
        if any(snapshot_state.get(k) != v for k, v in identity.items()):
            raise ValueError('execution_schema_state_identity_mismatch')
        state_digest = pointer['sha256']
    elif state_path:
        raise ValueError('execution_schema_state_anchor_missing')
    result = {
        'schema_name': schema, 'schema_version': schema, 'schema_revision': 1,
        'context_schema': CONTEXT_SCHEMA, 'compatibility_mode': 'legacy_migration' if legacy else 'strict',
        'compatibility_basis': 'dated_state_before_cutoff' if legacy else 'declared_current_state',
        'task_run_id': run_id, 'task_created_at': created,
        'state_created_at': created, 'state_task_run_id': run_id, 'state_schema_revision': revision,
        'compatibility_cutoff': contract['legacy_state_cutoff_utc'],
        'state_file_sha256': state_digest,
        'state_identity': identity, 'place': text(state.get('place')).casefold(),
        'migration_status': ('committed' if migration and migration.is_file() else 'required') if legacy else 'not_required',
        'migration_sha256': sha256_file(migration) if legacy and migration and migration.is_file() else '',
        'current_strict_schema': contract['current_schema_version'],
        'registry_file': '.query-plans/registry.json',
        'registry_sha256': sha256_file(registry) if registry and registry.is_file() else '',
        'amendments_sha256': sha256_file(root/'.execution-events/events.json')
            if root and (root/'.execution-events/events.json').is_file() else '',
    }
    result['context_binding_sha256'] = canonical_sha256(result)
    return ExecutionContext(result, run_root=root.resolve() if root else None)


def validate_context(context: Mapping[str, object] | None, config: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(context, Mapping):
        raise ValueError('execution_schema_context_required')
    result = ExecutionContext(context)
    core = {k: v for k, v in result.items() if not k.startswith('_') and k != 'context_binding_sha256'}
    if result.get('context_schema') != CONTEXT_SCHEMA or canonical_sha256(core) != result.get('context_binding_sha256'):
        raise ValueError('execution_schema_context_binding_invalid')
    identity = result.get('state_identity', {})
    if not isinstance(identity, Mapping):
        raise ValueError('execution_schema_state_identity_missing')
    root = Path(str(result.get('_run_root', '')))
    pointer_path = root/'.query-plans/state-anchor.json'
    if not result.get('_run_root') or not pointer_path.is_file():
        raise ValueError('execution_schema_state_anchor_missing')
    pointer = json.loads(pointer_path.read_text(encoding='utf-8-sig'))
    snapshot = _safe_path(pointer_path.parent, pointer['file'])
    if sha256_file(snapshot) != result.get('state_file_sha256') or pointer['sha256'] != result['state_file_sha256']:
        raise ValueError('execution_schema_state_anchor_changed')
    actual = json.loads(snapshot.read_text(encoding='utf-8-sig'))
    if not isinstance(actual, Mapping) or any(actual.get(k) != v for k,v in identity.items()):
        raise ValueError('execution_schema_state_identity_mismatch')
    if (not result.get('task_run_id') or result['task_run_id'] != identity.get('task_run_id')
            or result['task_run_id'] != result.get('state_task_run_id')):
        raise ValueError('execution_schema_context_task_mismatch')
    legacy = result.get('compatibility_mode') == 'legacy_migration'
    if legacy and (integer(identity.get('schema_revision', 0)) > 5 or
                   utc(identity.get('created_at')) > utc(config['execution_log_contract']['legacy_state_cutoff_utc'])):
        raise ValueError('legacy_execution_schema_not_allowed')
    if not legacy and result.get('schema_version') != config['execution_log_contract']['current_schema_version']:
        raise ValueError('execution_schema_unsupported')
    # A self-consistent checksum is not authority to choose compatibility rules.
    # Rebuild every public context field from the actual anchored state and files.
    expected = context_from_state(actual, config=config, run_root=root)
    if dict(result) != dict(expected):
        raise ValueError('execution_schema_context_state_mismatch')
    return result


def load_registered_queries(context: Mapping[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    if not context.get('_run_root'):
        raise ValueError('query_registry_root_missing')
    registry = _safe_path(Path(str(context['_run_root'])), context['registry_file'])
    if not registry.is_file():
        raise ValueError('query_plan_registry_missing')
    if sha256_file(registry) != context.get('registry_sha256'):
        raise ValueError('query_plan_registry_hash_mismatch')
    payload = json.loads(registry.read_text(encoding='utf-8-sig'))
    if not isinstance(payload, Mapping) or not isinstance(payload.get('plans'), list):
        raise ValueError('query_plan_registry_structure_invalid')
    if payload.get('schema_version') != REGISTRY_SCHEMA or payload.get('task_run_id') != context['task_run_id']:
        raise ValueError('query_plan_registry_task_mismatch')
    result, by_id = {}, {}
    for entry in payload['plans']:
        if not isinstance(entry, Mapping):
            raise ValueError('query_plan_registry_entry_invalid')
        plan = _safe_path(registry.parent, entry['plan_file'])
        sidecar = _safe_path(registry.parent, entry['binding_file'])
        if not plan.is_file() or not sidecar.is_file():
            raise ValueError('query_plan_missing')
        if sha256_file(plan) != entry['plan_sha256'] or sha256_file(sidecar) != entry['binding_sha256']:
            raise ValueError('query_plan_hash_mismatch')
        b = json.loads(sidecar.read_text(encoding='utf-8-sig'))
        validate_plan_binding(b)
        if b.get('task_run_id') != context['task_run_id'] or text(b.get('place')).casefold() != context['place']:
            raise ValueError('query_plan_task_or_place_mismatch')
        if b.get('query_plan_sha256') != entry['plan_sha256'] or entry['plan_id'] != 'PLAN-' + str(b.get('parameter_binding_sha256'))[:24]:
            raise ValueError('query_plan_binding_invalid')
        rows = read_rows(plan)
        if finalize_plan_rows(rows, b) != rows:
            raise ValueError('query_plan_machine_fields_drift')
        if len(rows) != integer(b.get('query_count')):
            raise ValueError('query_plan_count_mismatch')
        for r in rows:
            if r.get('task_run_id') != context['task_run_id'] or r.get('plan_id') != entry['plan_id']:
                raise ValueError('query_plan_row_identity_mismatch')
            if canonical_sha256({k: v for k, v in r.items() if k != 'plan_row_sha256'}) != r.get('plan_row_sha256'):
                raise ValueError('query_plan_row_hash_mismatch')
            if canonical_sha256(query_definition(r)) != r.get('query_definition_sha256'):
                raise ValueError('query_definition_hash_mismatch')
            q = str(r['query_id'])
            if q in by_id or (entry['plan_id'], q) in result:
                raise ValueError('query_identity_conflict')
            by_id[q] = entry['plan_id']
            from temporal_fields import plan_time_bounds
            try:
                r['_temporal_bounds'] = plan_time_bounds(b, entry,
                    task_run_id=context['task_run_id'], task_created_at=context['task_created_at'])
            except ValueError as exc:
                r['_temporal_error'] = str(exc)
            result[(entry['plan_id'], q)] = r
    return result


def validate_plan_binding(binding: Mapping[str, object]) -> None:
    from retrieval_controls import load_retrieval_config
    if not isinstance(binding, Mapping) or any(k not in binding for k in BINDING_PARAMETER_FIELDS):
        raise ValueError('query_plan_binding_fields_missing')
    parameters = {k: binding[k] for k in BINDING_PARAMETER_FIELDS}
    if canonical_sha256(parameters) != binding.get('parameter_binding_sha256'):
        raise ValueError('query_plan_parameter_binding_invalid')
    if binding['generator_sha256'] != sha256_file(Path(__file__).with_name('build_query_plan.py')):
        raise ValueError('query_plan_generator_changed')
    if binding['retrieval_control_sha256'] != canonical_sha256(load_retrieval_config()):
        raise ValueError('query_plan_retrieval_rules_changed')
    if int(binding['iteration_round']) > 0:
        from build_query_plan import validate_plan_audit_snapshot
        validate_plan_audit_snapshot(binding)


def prepare_execution_record(plan: Mapping[str, object], observation: Mapping[str, object]) -> dict[str, str]:
    """Bind observed execution facts to a committed query; never infer success or timestamps."""
    validate_observation(observation)
    result = {str(k): str(v).lower() if isinstance(v, bool) else str(v)
              for k, v in observation.items() if not str(k).startswith('_')}
    for key in (*QUERY_FIELDS, 'query_definition_sha256', 'plan_row_sha256'):
        expected = str(plan.get(key, ''))
        if key in result and result[key] != expected:
            raise ValueError('query_identity_conflict:' + key)
        result[key] = expected
    if 'source_type' in result and result['source_type'] != str(plan.get('source_category_target', '')):
        raise ValueError('query_identity_conflict:source_type')
    result['record_type'] = 'execution'
    result['iteration_round'] = str(plan['planned_iteration_round'])
    result['state_updated_at'] = result['finished_at']
    result['search_id'] = result['execution_id']
    from execution_semantics import route_id
    result['route_id'] = route_id(plan)
    result['execution_binding_sha256'] = execution_binding(result)
    return result


def execution_binding(row: Mapping[str, object]) -> str:
    return canonical_sha256({k: str(row.get(k, '')) for k in (
        'task_run_id', 'plan_id', 'query_id', 'query_definition_sha256', 'plan_row_sha256',
        'execution_id', 'retry_number', 'original_execution_id', 'started_at', 'finished_at',
        'iteration_round', 'gap_target', 'target_confidence', 'dimension_confidence_before_round',
        'audit_snapshot_sha256', 'plan_binding_sha256')})


def register_legacy_migration(original_file: Path, *, state_path: Path, config: Mapping[str, object]) -> dict[str, object]:
    """Migrate explicit dated history, retaining its bytes; ambiguous times or identities fail."""
    state = json.loads(state_path.read_text(encoding='utf-8-sig'))
    context = context_from_state(state, config=config, state_path=state_path)
    if context['compatibility_mode'] != 'legacy_migration':
        raise ValueError('legacy_migration_not_authorized_by_state')
    queries = load_registered_queries(context)
    originals = read_rows(original_file)
    migrated = []
    for number, original in enumerate(sorted(originals, key=lambda r: (
            utc(r.get('finished_at')), str(r.get('search_id')))), 1):
        from execution_schema import SEARCH_LOG_FIELDS
        forbidden = set(original) - SEARCH_LOG_FIELDS
        if forbidden:
            raise ValueError('execution_field_ownership_forbidden:' + ','.join(sorted(forbidden)))
        qid = str(original.get('query_id') or original.get('search_id') or '')
        matches = [p for (_, q), p in queries.items() if q == qid]
        if len(matches) != 1:
            raise ValueError('legacy_query_plan_not_uniquely_bound:' + qid)
        p = matches[0]
        if text(original.get('query')) != text(p['query']):
            raise ValueError('legacy_query_plan_text_mismatch:' + qid)
        # Actual observed start/end remain mandatory. A date or guessed timezone is insufficient.
        utc(original.get('started_at'))
        utc(original.get('finished_at'))
        row = {str(k): str(v).lower() if isinstance(v, bool) else str(v) for k,v in original.items()}
        for k in (*QUERY_FIELDS, 'query_definition_sha256', 'plan_row_sha256'):
            if row.get(k) and query_definition({**p, k: row[k]}).get(k) != query_definition(p).get(k):
                if k in QUERY_FIELDS:
                    raise ValueError('legacy_query_identity_conflict:' + k)
            row[k] = str(p.get(k, ''))
        row.update(record_type='execution', query_id=qid,
                   execution_id=str(original.get('execution_id') or original.get('search_id') or ''),
                   event_sequence=str(original.get('event_sequence') or number),
                   state_updated_at=str(original['finished_at']))
        from execution_semantics import route_id
        row['route_id'] = route_id(p)
        row.setdefault('retry_number', '0')
        row['execution_binding_sha256'] = execution_binding(row)
        issues = basic_errors(row)
        if issues:
            raise ValueError('legacy_migration_record_invalid:' + ';'.join(issues))
        migrated.append({'original_sha256': canonical_sha256(original), 'record': row})
    manifest = state_path.parent/'.query-plans/migration.json'
    snapshot = manifest.parent/'legacy'/ (sha256_file(original_file)+original_file.suffix)
    payload = {'schema_version': 'execution-migration-1', 'task_run_id': context['task_run_id'],
               'state_file_sha256': context['state_file_sha256'],
               'original_file': snapshot.relative_to(manifest.parent).as_posix(),
               'original_file_sha256': sha256_file(original_file),
               'records': sorted(migrated, key=lambda r:r['original_sha256'])}
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()
    with ProcessFileLock(manifest):
        if manifest.exists() and manifest.read_bytes() != data:
            raise ValueError('legacy_migration_already_committed')
        atomic_bytes(snapshot, original_file.read_bytes())
        atomic_bytes(manifest, data)
    return context_from_state(state, config=config, state_path=state_path)


class DerivedExecution(dict):
    """Process-local reconstruction view; never an accepted external schema."""


def validate_facts(rows: Iterable[Mapping[str, object]], *, config: Mapping[str, object],
                   schema_context: Mapping[str, object] | None) -> dict[str, object]:
    from retrieval_controls import (EXECUTED_STATUSES, NON_EXECUTED_STATUSES, PLAN_RECORD_TYPES,
        CONTROLLED_RETRY_REASONS, CONTROLLED_RETRY_INTERVAL_POLICIES, EXECUTION_RECORD_TYPES, budget_values,
        validate_query_dimension_targets, validate_query_intent_signature)
    originals = [{k: v for k, v in row.items() if not str(k).startswith('_')}
                 if isinstance(row, DerivedExecution) else row for row in rows]
    observed_now = datetime.now(timezone.utc)
    migration_result = {'performed': False, 'schema_version': '', 'rows': [],
                        'migration_records_sha256': canonical_sha256([])}
    pending = []
    context_errors = []
    context = dict(schema_context) if isinstance(schema_context, Mapping) else {}
    queries = {}
    migrated_identities = set()
    def migrated_identity(row):
        # CSV serialization adds empty declared columns and refresh updates only these
        # two derived counts. Every observed/identity field remains bound to the archive.
        return canonical_sha256({k: str(v) for k, v in row.items()
            if v not in (None, '') and k not in
            {'new_scored_evidence_units', 'cumulative_scored_evidence_units'}})
    retracted = set()
    try:
        context = validate_context(schema_context, config)
        queries = load_registered_queries(context)
        from execution_amendments import apply_events, read_events
        events = read_events(context['_run_root'])
        if events:
            originals, retracted = apply_events(originals, events, context['task_run_id'])
        if context.get('compatibility_mode') == 'legacy_migration':
            manifest = Path(str(context['_run_root']))/'.query-plans/migration.json'
            if not manifest.is_file() or sha256_file(manifest) != context.get('migration_sha256'):
                raise ValueError('legacy_migration_manifest_missing_or_changed')
            payload = json.loads(manifest.read_text(encoding='utf-8-sig'))
            if (payload.get('task_run_id') != context['task_run_id'] or
                    payload.get('state_file_sha256') != context['state_file_sha256']):
                raise ValueError('legacy_migration_identity_mismatch')
            original_file = _safe_path(manifest.parent, payload['original_file'])
            if sha256_file(original_file) != payload['original_file_sha256']:
                raise ValueError('legacy_migration_original_changed')
            original_hashes = {canonical_sha256(r) for r in read_rows(original_file)}
            mapping = {m['original_sha256']: m['record'] for m in payload['records']}
            migrated_identities = {migrated_identity(r) for r in mapping.values()}
            if original_hashes != set(mapping):
                raise ValueError('legacy_migration_record_set_mismatch')
            migration_rows = [{'original_record_sha256': canonical_sha256(r),
                               'migrated_record_sha256': canonical_sha256(mapping[canonical_sha256(r)])}
                              for r in originals if canonical_sha256(r) in mapping]
            # New executions remain strict current-schema records. Only exact archived
            # originals receive migration; incomplete additions are never filled in.
            originals = [mapping.get(canonical_sha256(r), r) for r in originals]
            migration_rows.sort(key=lambda r:r['original_record_sha256'])
            migration_result = {'performed': True, 'schema_version': context['schema_version'],
                                'rows': migration_rows, 'migration_records_sha256': canonical_sha256(migration_rows)}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        context_errors.append(str(exc))
    budget = budget_values(config)
    for original in originals:
        malformed = not isinstance(original, Mapping)
        r = ({"invalid_original": original} if malformed else
             {str(k): v for k, v in original.items()})
        record_digest = canonical_sha256(r)
        invalid_types = [k for k, v in r.items() if not isinstance(v, (str, int, float, bool, type(None)))]
        for key in invalid_types:
            r[key] = json.dumps(r[key], ensure_ascii=False, sort_keys=True)
        if str(r.get('status', '')) in NON_EXECUTED_STATUSES or str(r.get('record_type', '')) in PLAN_RECORD_TYPES:
            continue
        issues = set(context_errors)
        if malformed:
            issues.add('execution_record_not_object')
        issues.update('execution_field_type_invalid:' + key for key in invalid_types)
        issues.update(basic_errors(r))
        r['_record_sha256'] = record_digest
        r['_errors'] = issues
        pending.append(r)
        if str(r.get('status', '')) not in EXECUTED_STATUSES:
            issues.add('execution_status_invalid')
        if r.get('record_type') not in EXECUTION_RECORD_TYPES:
            issues.add('execution_record_type_invalid')
        from audit_evidence import parse_bool
        from execution_semantics import ITERATION_ACTIONS, outcome_errors
        if r.get('next_action') and r['next_action'] not in ITERATION_ACTIONS:
            issues.add('execution_next_action_invalid')
        for field in ('login_triggered', 'restriction_triggered'):
            if r.get(field) not in (None, '') and parse_bool(str(r[field])) is None:
                issues.add('execution_boolean_invalid:' + field)
        for k in ('task_run_id','plan_id','query_id','execution_id','query','started_at','finished_at'):
            if not text(r.get(k)):
                issues.add('execution_credential_missing:' + k)
        if str(r.get('task_run_id', '')) != context.get('task_run_id'):
            issues.add('execution_task_run_mismatch')
        target = validate_query_dimension_targets(r, config=config)
        issues.update(target['error_codes'])
        r['_validated_targets'] = target['targets']
        intent = validate_query_intent_signature(r)
        r['_validated_intent'] = intent['derived']
        if intent['status'] != 'valid':
            issues.add('normalized_query_intent_mismatch')
        for k in ('retry_number','iteration_round','event_sequence', *_COUNTS):
            try:
                r['_' + k] = integer(r.get(k), minimum=1 if k == 'event_sequence' else 0)
            except ValueError:
                issues.add('execution_integer_invalid:' + k)
        number = r.get('_iteration_round', -1)
        r['_validated_round'] = number
        if number > budget['maximum_iteration_rounds'] or number < 0:
            issues.add('round_number_out_of_bounds')
        if number > 0 and not r['_validated_targets']:
            issues.add('deep_query_targets_empty')
        if number > 0 and r.get('iteration_mode') not in {'evidence_gap_fill','medium_enhancement'}:
            issues.add('iteration_mode_invalid')
        for k in ('started_at','finished_at'):
            try:
                r['_' + k] = utc(r.get(k))
                if r['_' + k] > observed_now:
                    issues.add('execution_time_in_future:' + k)
            except ValueError as exc:
                issues.add(str(exc) + ':' + k)
        if '_started_at' in r and '_finished_at' in r and r['_finished_at'] < r['_started_at']:
            issues.add('execution_finished_before_started')
        try:
            r['_event_key'] = event_key(r)
            if r['_event_key'][0] > observed_now:
                issues.add('execution_time_in_future:event')
            if '_finished_at' in r and r['_event_key'][0] < r['_finished_at']:
                issues.add('route_state_before_execution')
        except ValueError as exc:
            issues.add(str(exc) + ':event')
        plan = queries.get((str(r.get('plan_id', '')), str(r.get('query_id', ''))))
        if plan is None:
            issues.add('query_plan_unregistered_or_query_missing')
        else:
            try:
                if (context.get('compatibility_mode') != 'legacy_migration' or
                        migrated_identity(original) not in migrated_identities):
                    if plan.get('_temporal_error'):
                        issues.add(plan['_temporal_error'])
                    for field, boundary in plan.get('_temporal_bounds', {}).items():
                        if r.get('_started_at') and r['_started_at'] < boundary:
                            issues.add('execution_before_' + field)
                if query_definition(r) != query_definition(plan):
                    issues.add('query_plan_execution_mismatch')
                if r.get('plan_row_sha256') != plan['plan_row_sha256'] or r.get('query_definition_sha256') != plan['query_definition_sha256']:
                    issues.add('query_plan_execution_binding_mismatch')
                if r.get('_retry_number') == 0 and number != int(plan['planned_iteration_round']):
                    issues.add('first_attempt_plan_round_mismatch')
                if number < int(plan['planned_iteration_round']):
                    issues.add('retry_before_planned_round')
                from execution_semantics import route_id
                if r.get('route_id') != route_id(plan):
                    issues.add('execution_derived_field_mismatch:route_id')
                if r.get('state_updated_at') != r.get('finished_at'):
                    issues.add('execution_derived_field_mismatch:state_updated_at')
            except (ValueError, TypeError):
                issues.add('query_definition_invalid')
        if r.get('execution_binding_sha256') != execution_binding(r):
            issues.add('execution_binding_mismatch')
        issues.update(outcome_errors(r, context))
    by_execution, by_query, by_sequence, by_search = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for r in pending:
        by_execution[str(r.get('execution_id', ''))].append(r)
        by_query[str(r.get('query_id', ''))].append(r)
        by_search[str(r.get('search_id', ''))].append(r)
        if '_event_sequence' in r:
            by_sequence[r['_event_sequence']].append(r)
    for groups, code in ((by_execution,'duplicate_execution_id'), (by_sequence,'duplicate_event_sequence'), (by_search,'duplicate_search_id')):
        for items in groups.values():
            if len(items) > 1:
                for r in items:
                    r['_errors'].add(code)
    firsts, retries = [], []
    for items in by_query.values():
        definitions = set()
        for r in items:
            try:
                definitions.add(canonical_sha256(query_definition(r)))
            except (ValueError, TypeError):
                definitions.add(r['_record_sha256'])
        chain_issues = set()
        if len(definitions) != 1:
            chain_issues.add('query_identity_conflict')
        ordered = sorted(items, key=lambda r: (r.get('_retry_number', -1), r['_record_sha256']))
        sequence = [r.get('_retry_number', -1) for r in ordered]
        if sequence != list(range(len(items))) or len(items)-1 > budget['maximum_controlled_retries_per_intent']:
            chain_issues.add('controlled_retry_sequence_invalid')
        first = ordered[0]
        if not any(r.get('_retry_number') == 0 for r in ordered):
            chain_issues.add('orphan_retry')
        if first.get('_retry_number') != 0 or any(text(first.get(k)) for k in ('retry_reason','original_execution_id','retry_interval_policy')):
            chain_issues.add('first_attempt_has_retry_metadata')
        for previous, r in zip(ordered, ordered[1:]):
            if r.get('retry_reason') not in CONTROLLED_RETRY_REASONS:
                chain_issues.add('controlled_retry_reason_invalid')
            if r.get('original_execution_id') != first.get('execution_id'):
                chain_issues.add('controlled_retry_original_mismatch')
            if r.get('retry_interval_policy') not in CONTROLLED_RETRY_INTERVAL_POLICIES:
                chain_issues.add('controlled_retry_interval_invalid')
            if (r.get('_started_at') and previous.get('_finished_at') and
                    r['_started_at'] < previous['_finished_at']):
                chain_issues.add('retry_time_order_invalid')
            if (r.get('_event_key') and previous.get('_event_key') and
                    r['_event_key'][0] < previous['_event_key'][0]):
                chain_issues.add('retry_event_time_order_invalid')
            from execution_semantics import retryable_error
            if not retryable_error(previous):
                chain_issues.add('controlled_retry_predecessor_not_retryable')
            elif r.get('retry_reason') != retryable_error(previous):
                chain_issues.add('controlled_retry_reason_predecessor_mismatch')
        if chain_issues or any(r['_errors'] for r in items):
            for r in items:
                r['_errors'].update(chain_issues or {'retry_chain_invalid'})
        else:
            firsts.append(first)
            retries.extend(ordered[1:])
    for r in pending:
        earlier = [e['_finished_at'] for e in pending if '_finished_at' in e and
                   0 <= e['_validated_round'] < r['_validated_round']]
        if earlier and '_started_at' in r and r['_started_at'] < max(earlier):
            r['_errors'].add('round_execution_before_previous_completion')
    # Any invalid member invalidates its retry chain, never just the later physical row.
    for items in by_query.values():
        if any(r['_errors'] for r in items):
            for r in items:
                if not r['_errors']:
                    r['_errors'].add('retry_chain_invalid')
    valid = sorted([r for r in pending if not r['_errors']], key=lambda r: r['_event_key'])
    first_ids = {str(r.get('execution_id')) for r in firsts if not r['_errors']}
    retry_ids = {str(r.get('execution_id')) for r in retries if not r['_errors']}
    first_by_intent = {}
    for r in valid:
        if str(r.get('execution_id')) in first_ids:
            first_by_intent.setdefault(r['_validated_intent'], r)
        r['_new_independent_intent'] = first_by_intent.get(r['_validated_intent']) is r
        from execution_semantics import STATUS_MATRIX
        r['_retracted'] = str(r.get('execution_id')) in retracted
        r['_coverage_eligible'] = bool(STATUS_MATRIX[str(r['status'])]['coverage']) and not r['_retracted']
        r['_new_coverage_intent'] = r['_new_independent_intent'] and r['_coverage_eligible']
        r['_intent_status'] = (
            'new_independent_intent' if r['_new_independent_intent'] else
            'controlled_retry' if str(r.get('execution_id')) in retry_ids else 'duplicate_planned_intent')
    invalid = sorted([{
        'record_sha256': r['_record_sha256'], 'query_id': str(r.get('query_id', '')),
        'declared_iteration_round': str(r.get('iteration_round', '')),
        'execution_id': str(r.get('execution_id', '')), 'plan_id': str(r.get('plan_id', '')),
        'error_codes': sorted(r['_errors']), 'errors': [{'code': c} for c in sorted(r['_errors'])],
    } for r in pending if r['_errors']], key=lambda r: (r['execution_id'],r['record_sha256']))
    if context_errors and not invalid:
        invalid = [{'record_sha256': '', 'execution_id': '', 'query_id': '', 'plan_id': '',
                    'error_codes': sorted(context_errors), 'errors': [{'code': e} for e in sorted(context_errors)]}]
    return {'status': 'invalid' if invalid else 'valid', 'schema_version': context.get('schema_version', ''),
            'schema_context': context, 'valid_rows': [DerivedExecution(r) for r in valid],
            'attempt_rows': [DerivedExecution(r) for r in valid],
            'valid_execution_ids': sorted(str(r['execution_id']) for r in valid),
            'first_attempt_ids': sorted(first_ids), 'controlled_retry_ids': sorted(retry_ids),
            'valid_execution_set_sha256': set_hash(valid), 'invalid_records': invalid,
            'invalid_execution_set_sha256': canonical_sha256(invalid),
            'migration': migration_result,
            'retracted_execution_ids': sorted(retracted)}


def validate_source_links(sources: Iterable[Mapping[str, object]], facts: Mapping[str, object]) -> dict[str, object]:
    retracted = {str(r['execution_id']) for r in facts['valid_rows'] if r.get('_retracted')}
    executions = {str(r['execution_id']): r for r in facts['valid_rows']}
    valid, errors = [], []
    sources = list(sources)
    identity_counts = Counter(str(r.get('source_id', '')) for r in sources if isinstance(r, Mapping))
    from host_receipts import reused_call_sources
    conflicting_calls = reused_call_sources(sources)
    from execution_semantics import source_outcome_errors
    grouped = defaultdict(list)
    for source in sources:
        if isinstance(source, Mapping):
            grouped[str(source.get('execution_id', ''))].append(source)
    outcome_issues = {eid: source_outcome_errors(executions[eid], group)
                      for eid, group in grouped.items() if eid in executions}
    for original in sorted(sources, key=canonical_sha256):
        if not isinstance(original, Mapping):
            errors.append({'source_id': '', 'execution_id': '', 'query_id': '',
                'record_sha256': canonical_sha256(original), 'error_codes': ['source_record_not_object']})
            continue
        s = original
        issues = []
        from source_identity import source_qualification_errors
        issues.extend(source_qualification_errors(s, label='source-ledger:' + str(s.get('source_id', ''))))
        source_id = str(s.get('source_id', ''))
        if source_id in conflicting_calls:
            issues.append('host_tool_call_reused_for_different_responses')
        if not source_id:
            issues.append('source_identity_missing')
        elif identity_counts[source_id] != 1:
            issues.append('source_identity_conflict')
        e = executions.get(str(s.get('execution_id', '')))
        if e is None:
            issues.append('source_execution_missing_or_invalid')
        else:
            issues.extend(outcome_issues.get(str(s.get('execution_id', '')), []))
            for k in ('task_run_id', 'query_id', *SOURCE_BINDING_FIELDS):
                if str(s.get(k, '')) != str(e.get(k, '')):
                    issues.append('source_execution_mismatch:' + k)
            if text(s.get('query_text') or s.get('query')) != text(e.get('query')):
                issues.append('source_query_text_mismatch')
            try:
                at = utc(s.get('retrieved_at'))
                if at < utc(e['started_at']) or at > utc(e['finished_at']):
                    issues.append('source_outside_execution_window')
                receipt = s.get("host_receipt")
                if isinstance(receipt, str) and receipt:
                    receipt = json.loads(receipt)
                if isinstance(receipt, dict):
                    payload = receipt["payload"]
                    if (utc(payload["request_started_at"]) < utc(e["started_at"])
                            or utc(payload["response_finished_at"]) > utc(e["finished_at"])):
                        issues.append("host_response_outside_execution_window")
            except (ValueError, TypeError, KeyError):
                issues.append('source_retrieved_time_invalid')
        if issues:
            errors.append({'source_id': str(s.get('source_id','')), 'execution_id': str(s.get('execution_id','')),
                           'query_id': str(s.get('query_id','')), 'error_codes': sorted(issues)})
        elif str(s.get('execution_id', '')) not in retracted:
            valid.append(s)
    valid.sort(key=lambda r: (str(r.get('source_id', '')), canonical_sha256(r)))
    errors.sort(key=lambda r: (r['source_id'], canonical_sha256(r)))
    return {'status': 'invalid' if errors else 'valid', 'valid_sources': valid, 'errors': errors,
            'binding_sha256': set_hash(valid)}


def transaction_context(context: Mapping[str, object]) -> dict[str, object]:
    """Only immutable run identity, not the mutable whole registry digest."""
    return {k: v for k, v in context.items()
            if k not in {'registry_sha256', 'context_binding_sha256'} and not k.startswith('_')}


def transaction_dependencies(context: Mapping[str, object], rows: list[dict[str, str]]) -> dict[str, object]:
    queries = load_registered_queries(context)
    registry = _safe_path(Path(str(context['_run_root'])), context['registry_file'])
    entries = {r['plan_id']: r for r in json.loads(registry.read_text(encoding='utf-8-sig'))['plans']}
    plan_ids = {r['plan_id'] for r in rows}
    dependencies = []
    for row in rows:
        key = (row['plan_id'], row['query_id'])
        if key not in queries or queries[key]['plan_row_sha256'] != row['plan_row_sha256']:
            raise ValueError('execution_transaction_query_dependency_changed')
        dependencies.append({k: row[k] for k in ('task_run_id', 'execution_id', 'query_id',
            'plan_id', 'plan_row_sha256', 'query_definition_sha256', 'execution_binding_sha256')})
    return {'plans': {pid: {**entries[pid], 'entry_sha256': canonical_sha256(entries[pid])}
                      for pid in sorted(plan_ids)},
            'executions': sorted(dependencies, key=canonical_sha256)}


def _finish_execution_transaction(journal: Path, *, state_path: Path,
                                  writer_ledger_path: Path, output: Path,
                                  context: Mapping[str, object], fault_at: str = "") -> None:
    from artifact_provenance import read_writer_ledger, register_protected_artifact
    from run_paths import logical_path
    payload = json.loads(journal.read_text(encoding='utf-8-sig'))
    core = {k: v for k, v in payload.items() if k != 'transaction_id'}
    if payload.get('transaction_id') != 'EX-TX-' + canonical_sha256(core):
        raise ValueError('execution_transaction_hash_mismatch')
    root = state_path.resolve().parent
    content = base64.b64decode(payload['content_base64'], validate=True)
    if hashlib.sha256(content).hexdigest() != payload.get('new_sha256'):
        raise ValueError('execution_transaction_content_changed')
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    if (payload.get('schema') != 'execution-write-2' or
            payload.get('context') != transaction_context(context) or
            payload.get('output') != logical_path(output, root) or
            payload.get('writer_ledger') != logical_path(writer_ledger_path, root)):
        raise ValueError('execution_transaction_identity_mismatch')
    if payload.get('dependencies') != transaction_dependencies(context, rows):
        raise ValueError('execution_transaction_dependency_changed')
    from retrieval_controls import load_retrieval_config
    facts = validate_facts(rows, config=load_retrieval_config(), schema_context=context)
    if facts['status'] != 'valid':
        raise ValueError('execution_transaction_facts_invalid:' + json.dumps(facts['invalid_records'], ensure_ascii=False))
    prefix_size = integer(payload.get('writer_prefix_size'))
    current_writer = writer_ledger_path.read_bytes() if writer_ledger_path.exists() else b''
    if (len(current_writer) < prefix_size or
            hashlib.sha256(current_writer[:prefix_size]).hexdigest() != payload.get('writer_prefix_sha256')):
        raise ValueError('execution_transaction_writer_prefix_changed')
    if writer_ledger_path.exists():
        read_writer_ledger(writer_ledger_path, task_run_id=str(context['task_run_id']))
    current_hash = sha256_file(output) if output.exists() else ''
    if current_hash not in {payload.get('old_sha256'), payload.get('new_sha256')}:
        raise ValueError('execution_transaction_output_conflict')
    if current_hash == payload.get('old_sha256'):
        old_bytes = output.read_bytes() if output.exists() else b''
        if (len(old_bytes) != integer(payload.get('output_prefix_size')) or
                hashlib.sha256(old_bytes).hexdigest() != payload.get('output_prefix_sha256')):
            raise ValueError('execution_transaction_output_prefix_changed')
    archive = root/'.execution-transactions'/(payload['transaction_id']+'.json')
    if archive.exists() and archive.read_bytes() != journal.read_bytes():
        raise ValueError('execution_transaction_archive_conflict')
    if not archive.exists():
        atomic_bytes(archive, journal.read_bytes())
    atomic_bytes(output, content)
    if fault_at in {'after_log_replace', 'after_recovery_log_replace'}:
        raise RuntimeError('injected execution transaction fault:' + fault_at)
    register_protected_artifact(
        state_path=state_path, writer_ledger_path=writer_ledger_path,
        writer_script_id='execution_facts.py', output_role='search_log', output_path=output,
        expected_phase='SEARCH', input_paths={'execution_transaction': archive},
        provenance_context={'transaction_id': payload['transaction_id'],
                            'transaction_journal_path': logical_path(archive, root)},
        atomic_registration=True)
    if fault_at in {'after_writer_registration', 'after_recovery_writer_registration'}:
        raise RuntimeError('injected execution transaction fault:' + fault_at)
    journal.unlink()


def commit_execution_observations(*, state_path: Path, writer_ledger_path: Path,
                                  observations: list[dict[str, object]], output: Path,
                                  fault_at: str = "") -> dict[str, object]:
    """Record real outcomes without allowing observations to redefine committed queries."""
    import io
    from artifact_provenance import read_writer_ledger, verify_artifact_writer
    from runtime_guard import authorize_runtime_write, load_runtime_state
    from retrieval_controls import load_retrieval_config, execution_schema_context_from_state
    from audit_evidence import SEARCH_LOG_FIELDS
    from run_paths import logical_path
    authorize_runtime_write(state_path=state_path, writer_script_id='execution_facts.py',
                            output_role='search_log', expected_phase='SEARCH', output_path=output)
    from execution_coordinator import coordinated
    with coordinated(state_path=state_path, writer_ledger_path=writer_ledger_path, fault_at=fault_at), ProcessFileLock(output):
        config = load_retrieval_config()
        context = execution_schema_context_from_state(load_runtime_state(state_path), state_path=state_path)
        journal = output.with_name(output.name + '.execution-transaction.json')
        if journal.exists():
            _finish_execution_transaction(journal, state_path=state_path,
                writer_ledger_path=writer_ledger_path, output=output, context=context, fault_at=fault_at)
        queries = load_registered_queries(context)
        existing = read_rows(output) if output.exists() else []
        if output.exists():
            verify_artifact_writer(state_path=state_path, writer_ledger_path=writer_ledger_path,
                                   output_role='search_log', output_path=output)
        by_id = {r['execution_id']: r for r in existing}
        next_sequence = max((integer(r['event_sequence']) for r in existing), default=0)
        if not isinstance(observations, list) or any(not isinstance(r, Mapping) for r in observations):
            raise ValueError('execution_observations_must_be_objects')
        # event_sequence is an immutable ingestion receipt, not causal/event time.
        for observation in observations:
            key = (str(observation.get('plan_id', '')), str(observation.get('query_id', '')))
            if key not in queries:
                raise ValueError('query_plan_unregistered_or_query_missing')
            identity = str(observation.get('execution_id', ''))
            previous = by_id.get(identity)
            record = prepare_execution_record(queries[key], observation)
            record['event_sequence'] = str(previous['event_sequence'] if previous else next_sequence+1)
            for field in ('new_scored_evidence_units', 'cumulative_scored_evidence_units'):
                supplied = integer(record.get(field, '0') or '0')
                retained = integer(previous.get(field, '0') or '0') if previous else 0
                if supplied not in {0, retained}:
                    raise ValueError('execution_derived_count_input_forbidden:' + field)
                # Only the protected count refresher owns these fields. A replay
                # compares immutable observations, retaining its current derivation.
                record[field] = str(retained)
            for field in SEARCH_LOG_FIELDS:
                record.setdefault(field, '')
            if previous is not None:
                for field in previous:
                    record.setdefault(field, '')
                if record != previous:
                    raise ValueError('execution_identity_conflict:' + identity)
                continue
            next_sequence += 1
            existing.append(record)
            by_id[identity] = record
        validation = validate_facts(existing, config=config, schema_context=context)
        if validation['status'] != 'valid':
            raise ValueError('execution_contract_invalid:' + json.dumps(validation['invalid_records'], ensure_ascii=False))
        fields = sorted(set(SEARCH_LOG_FIELDS).union(*(r.keys() for r in existing)))
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(existing, key=event_key))
        content = buffer.getvalue().encode('utf-8-sig')
        if not output.exists() or output.read_bytes() != content:
            if writer_ledger_path.exists():
                read_writer_ledger(writer_ledger_path, task_run_id=str(context['task_run_id']))
            prefix = writer_ledger_path.read_bytes() if writer_ledger_path.exists() else b''
            root = state_path.resolve().parent
            payload = {
                'schema': 'execution-write-2', 'context': transaction_context(context),
                'dependencies': transaction_dependencies(context, existing),
                'output': logical_path(output, root), 'writer_ledger': logical_path(writer_ledger_path, root),
                'writer_prefix_size': len(prefix), 'writer_prefix_sha256': hashlib.sha256(prefix).hexdigest(),
                'old_sha256': sha256_file(output) if output.exists() else '',
                'output_prefix_size': output.stat().st_size if output.exists() else 0,
                'output_prefix_sha256': hashlib.sha256(output.read_bytes() if output.exists() else b'').hexdigest(),
                'new_sha256': hashlib.sha256(content).hexdigest(),
                'content_base64': base64.b64encode(content).decode('ascii'),
            }
            if not payload['output'] or not payload['writer_ledger']:
                raise ValueError('execution_transaction_path_outside_run')
            payload['transaction_id'] = 'EX-TX-' + canonical_sha256(payload)
            atomic_bytes(journal, (json.dumps(payload, ensure_ascii=False, sort_keys=True)+'\n').encode())
            if fault_at == 'after_journal_prepare':
                raise RuntimeError('injected execution transaction fault:' + fault_at)
            _finish_execution_transaction(journal, state_path=state_path, writer_ledger_path=writer_ledger_path,
                                          output=output, context=context, fault_at=fault_at)
    return {'status': 'recorded', 'execution_count': len(existing),
            'valid_execution_set_sha256': validation['valid_execution_set_sha256']}


def commit_execution_amendments(requests, *, state_path, writer_ledger_path, search_path, fault_at=''):
    from runtime_guard import authorize_runtime_write
    from execution_amendments import commit, ledger_path
    authorize_runtime_write(state_path=state_path, writer_script_id='execution_facts.py',
        output_role='execution_events', expected_phase='SEARCH', output_path=ledger_path(state_path.parent))
    return commit(requests, state_path=state_path, writer_ledger_path=writer_ledger_path,
        search_path=search_path, fault_at=fault_at)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--writer-ledger', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--observations', type=Path, help='Actual retrieval outcomes as JSONL; never a plan')
    mode.add_argument('--migrate-legacy', type=Path, help='Explicit immutable migration of dated history with committed query identities')
    mode.add_argument('--amendments', type=Path, help='Append-only correction/supersede/retract requests; --output is the protected search log')
    mode.add_argument('--recover-transactions', action='store_true', help='Resume pending execution or amendment transaction')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.recover_transactions:
            if not args.writer_ledger:
                parser.error('--recover-transactions requires --writer-ledger')
            from execution_coordinator import lock, recover_locked
            with lock(args.state.resolve().parent):
                result = recover_locked(state_path=args.state, writer_ledger_path=args.writer_ledger)
        elif args.amendments:
            if not args.writer_ledger or not args.output:
                parser.error('--amendments requires --writer-ledger and --output')
            from storage_contract import read_staging_bytes
            requests = [json.loads(line) for line in read_staging_bytes(args.amendments).decode('utf-8-sig').splitlines() if line.strip()]
            result = commit_execution_amendments(requests, state_path=args.state,
                writer_ledger_path=args.writer_ledger, search_path=args.output)
        elif args.migrate_legacy:
            from retrieval_controls import load_retrieval_config
            config = load_retrieval_config()
            context = register_legacy_migration(args.migrate_legacy, state_path=args.state, config=config)
            result = validate_facts(read_rows(args.migrate_legacy), config=config, schema_context=context)
            if result['status'] != 'valid':
                raise ValueError('legacy_migration_invalid:' + json.dumps(result['invalid_records'], ensure_ascii=False))
            result = {'status': 'migrated', 'schema_context': context, 'migration': result['migration']}
        else:
            if not args.writer_ledger or not args.output:
                parser.error('--observations requires --writer-ledger and --output')
            observations = []
            from storage_contract import read_staging_bytes
            for number, line in enumerate(read_staging_bytes(args.observations).decode('utf-8-sig').splitlines(), 1):
                if line.strip():
                    try:
                        observations.append(json.loads(line))
                    except ValueError as exc:
                        raise ValueError(f'execution_observation_json_invalid:line={number}') from exc
            result = commit_execution_observations(state_path=args.state,
                writer_ledger_path=args.writer_ledger, observations=observations, output=args.output)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'invalid', 'error': str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
