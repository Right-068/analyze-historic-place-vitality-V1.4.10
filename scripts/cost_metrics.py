"""Observed execution proxies. Missing host turns/tokens/credits stay unknown."""
from pathlib import Path
import os
import uuid
import strict_json as json
from storage_contract import write_integrity_artifact, read_staging_json
from temporal_fields import utc_now

COUNTERS = {'native_web_search_calls','page_read_calls','python_cli_calls','orchestrator_calls',
            'bytes_sent_to_agent','bytes_returned_to_agent','raw_page_bytes','compact_page_bytes',
            'semantic_pages_processed','evidence_units_generated','successful_pages',
            'search_result_count','admitted_sources','cache_page_hits','cache_analysis_hits',
            'near_duplicate_hints','model_turn_count','tokens','credits'}


def atomic_json(path, value):
    path = Path(path)
    parts=path.resolve().parts
    namespaces={('staging',kind,'operational') for kind in ('pipeline','dispatch','helpers','cost')}
    anchored=False
    for root in path.resolve().parents:
        if (root/'运行状态.json').is_file():
            parts=path.resolve().relative_to(root).parts
            anchored=True
            break
    allowed=tuple(parts[:3]) in namespaces if anchored else any(
        tuple(parts[i:i+3]) in namespaces for i in range(len(parts)-2))
    if path.suffix!='.json' or not allowed:
        raise ValueError('operational_path_required')
    from protected_runtime import check_operational_before_write, register_operational_write
    check_operational_before_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf8')
    from storage_contract import prepare_staging_payload
    raw=prepare_staging_payload(path,raw)
    from protected_runtime import write_operational
    if write_operational(path,raw):
        return
    temporary = path.with_name('.cost-'+uuid.uuid4().hex+'.json')
    try:
        write_integrity_artifact(temporary, raw, transaction_id='batch-checkpoint')
        os.replace(temporary, path)
        from storage_contract import fsync_directory
        fsync_directory(path.parent)
    finally:
        if temporary.exists():temporary.unlink()
    register_operational_write(path)


def _terminal(root):
    state=Path(root)/'运行状态.json'
    if not state.exists():return False
    from runtime_guard import load_runtime_state, TERMINAL_FAILURE_STATUSES
    return load_runtime_state(state)['status'] in ({'complete'} | set(TERMINAL_FAILURE_STATUSES))


def record(root, event_id, values):
    if set(values)-COUNTERS or any(isinstance(v, bool) or not isinstance(v, (int,float)) or v < 0 for v in values.values()):
        raise ValueError('invalid_cost_observation')
    if _terminal(root):return {'status':'not_recorded_terminal_state'}
    import hashlib
    folder = Path(root)/'staging/cost/operational'
    path = folder/('event-'+hashlib.sha256(event_id.encode()).hexdigest()+'.json')
    from protected_runtime import check_operational_before_write
    check_operational_before_write(path)
    if path.exists():
        if read_staging_json(path)['values'] != values:
            raise ValueError('cost_observation_conflict')
        return
    value = {'event_id': event_id, 'at': utc_now(), 'values': values}
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(path,value)


def summary(root):
    folder = Path(root)/'staging/cost/operational'
    state=Path(root)/'运行状态.json'
    if state.exists():
        from protected_runtime import verify_before_action
        verify_before_action(state)
    totals = {key: 0 for key in COUNTERS}
    observed = set()
    for path in sorted(folder.glob('event-*.json')):
        for key, value in read_staging_json(path)['values'].items():
            totals[key] += value
            observed.add(key)
    for key in ('model_turn_count','tokens','credits','admitted_sources'):
        if key not in observed:
            totals[key] = None
    sources=Path(root)/'canonical/active-source-ledger.csv'
    if sources.exists():
        import csv
        with sources.open(encoding='utf-8-sig',newline='') as stream:
            totals['admitted_sources']=sum(1 for _ in csv.DictReader(stream))
    def ratio(numerator, denominator):
        return totals[numerator]/totals[denominator] if totals.get(numerator) is not None and totals.get(denominator) else None
    result = {'schema_version':'cost-metrics-1','measurement':'observed local proxies; host counters optional',
        'real_credit_measurement':'observed' if totals['credits'] is not None else 'missing evidence',
        **totals, 'kpi': {
            'model_turns_per_successful_page': ratio('model_turn_count','successful_pages'),
            'cli_calls_per_successful_page': ratio('python_cli_calls','successful_pages'),
            'search_results_per_successful_page': ratio('search_result_count','successful_pages'),
            'agent_visible_bytes_per_successful_page': ratio('bytes_returned_to_agent','successful_pages'),
            'page_reads_per_admitted_source': ratio('page_read_calls','admitted_sources'),
            'model_turns_per_evidence_unit': ratio('model_turn_count','evidence_units_generated')}}
    if not _terminal(root):atomic_json(folder/'cost-metrics.json', result)
    return result


def regression_warnings(baseline, current, tolerance=0.15):
    """Warnings only, never a research stopping rule or a scoring condition."""
    warnings=[]
    for key in ('agent_dispatch_boundary_proxy','python_cli_calls','agent_visible_bytes'):
        old,new=baseline.get(key),current.get(key)
        if isinstance(old,(int,float)) and old>0 and isinstance(new,(int,float)) and new>old*(1+tolerance):
            warnings.append({'metric':key,'baseline':old,'current':new,'growth':new/old-1})
    return warnings
