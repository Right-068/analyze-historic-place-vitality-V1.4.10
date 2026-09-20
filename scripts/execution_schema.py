"""One execution contract: observations, identity selectors and writer-owned facts."""
from temporal_fields import timestamp

SCHEMA_VERSION = 'execution-record-contract-2'
PLAN_FIELDS = (
    'task_run_id', 'plan_id', 'query_id', 'place', 'place_identity_sha256', 'query',
    'normalized_query_intent', 'query_dimension_targets', 'source_category_target',
    'iteration_mode', 'planned_iteration_round', 'target_time_range', 'language',
    'target_platform_id', 'polarity', 'target_subject', 'retrieval_entry',
    'gap_target', 'target_confidence', 'dimension_confidence_before_round',
    'audit_snapshot_sha256', 'plan_binding_sha256',
)
OBSERVATION_FIELDS = (
    'execution_id', 'started_at', 'finished_at', 'retrieved_at', 'search_tool',
    'status', 'next_action', 'returned_results', 'opened_pages', 'relevant_pages',
    'duplicate_pages', 'login_triggered', 'restriction_triggered', 'retry_number',
    'retry_reason', 'original_execution_id', 'retry_interval_policy', 'error_type', 'notes',
)
GENERATED_FIELDS = ('search_id', 'record_type', 'iteration_round', 'event_sequence',
    'state_updated_at', 'query_definition_sha256', 'plan_row_sha256', 'execution_binding_sha256',
    'route_id', 'new_scored_evidence_units', 'cumulative_scored_evidence_units')
TARGET_BASIS_FIELDS = ('target_audit_file', 'target_audit_sha256',
    'target_audit_state_file', 'target_audit_writer_ledger')
IDENTITY_SELECTORS = ('plan_id', 'query_id')
RAW_FIELDS = set(OBSERVATION_FIELDS + IDENTITY_SELECTORS + TARGET_BASIS_FIELDS)
SEARCH_LOG_FIELDS = set(PLAN_FIELDS + OBSERVATION_FIELDS + GENERATED_FIELDS + TARGET_BASIS_FIELDS)
BOOL_FIELDS = ('login_triggered', 'restriction_triggered')
TOOLS = ('web_search', 'browser', 'web_fetch', 'connector', 'academic_search', 'search_api')
REQUIRED_OBSERVATIONS = ('execution_id', 'started_at', 'finished_at', 'retrieved_at',
    'search_tool', 'status', 'next_action', 'returned_results', 'opened_pages',
    'relevant_pages', 'duplicate_pages', *BOOL_FIELDS, 'retry_number')


def field_contract():
    integer_fields = {'returned_results', 'opened_pages', 'relevant_pages', 'duplicate_pages', 'retry_number'}
    from execution_semantics import ITERATION_ACTIONS
    from retrieval_controls import EXECUTED_STATUSES
    return {'schema_version': SCHEMA_VERSION, 'plan_owned_fields': list(PLAN_FIELDS),
        'observer_fields': {k: {'required': k in REQUIRED_OBSERVATIONS,
            'type': 'boolean' if k in BOOL_FIELDS else 'nonnegative_integer_or_decimal_string' if k in integer_fields else 'string',
            'origin': 'observation'}
            for k in OBSERVATION_FIELDS},
        'generated_fields': list(GENERATED_FIELDS), 'search_tool_enum': list(TOOLS),
        'status_enum': sorted(EXECUTED_STATUSES), 'next_action_enum': sorted(ITERATION_ACTIONS),
        'plan_field_origin': 'verified_registered_query_plan',
        'generated_field_origins': {k: ('formal_scoring_chain' if k.endswith('scored_evidence_units') else
            'writer_ingestion_sequence' if k == 'event_sequence' else
            'verified_registered_query_plan' if k == 'route_id' else
            'verified_plan_and_execution' if k.endswith('sha256') or k == 'iteration_round' else
            'execution_id' if k == 'search_id' else 'finished_at' if k == 'state_updated_at' else
            'writer_constant') for k in GENERATED_FIELDS},
        'identity_selectors': list(IDENTITY_SELECTORS),
        'additional_properties': False,
        'conditional_observation_fields': {k: {'type': 'string',
            'required_when': {'next_action': 'target_met'}, 'otherwise': 'absent_or_empty',
            'validation': 'same_task_protected_evidence_audit'} for k in TARGET_BASIS_FIELDS},
        'timestamp_format': 'YYYY-MM-DDTHH:mm:ss[.ffffff]Z or explicit +/-HH:mm',
        'boolean_storage': ['true', 'false']}


def observation_template():
    result = {'plan_id': '${PLAN_ID}', 'query_id': '${QUERY_ID}'}
    for field in OBSERVATION_FIELDS:
        result[field] = (False if field in BOOL_FIELDS else 0 if field == 'retry_number' else
            '${ACTUAL_' + field.upper() + '}' if field in REQUIRED_OBSERVATIONS else '')
    result['search_tool'] = 'web_search'
    return result


def validate_observation(row):
    from collections.abc import Mapping
    if not isinstance(row, Mapping):
        raise ValueError('execution_observation_must_be_object')
    issues = basic_errors(row, raw=True)
    if issues:
        raise ValueError('execution_observation_invalid:' + ';'.join(issues))


def basic_errors(row, *, raw=False):
    errors = []
    allowed = RAW_FIELDS if raw else SEARCH_LOG_FIELDS
    for field in row:
        if field not in allowed:
            errors.append('execution_field_ownership_forbidden:' + str(field))
    for field in TARGET_BASIS_FIELDS:
        if field in row and not isinstance(row[field], str):
            errors.append('execution_field_type_invalid:' + field)
        if row.get('next_action') == 'target_met' and not row.get(field):
            errors.append('execution_conditional_field_missing:' + field)
        if row.get('next_action') != 'target_met' and row.get(field) not in (None, ''):
            errors.append('execution_conditional_field_forbidden:' + field)
    if raw:
        for field in IDENTITY_SELECTORS:
            if not isinstance(row.get(field), str) or not row[field].strip():
                errors.append('execution_identity_selector_missing:' + field)
    integer_fields = {'returned_results', 'opened_pages', 'relevant_pages', 'duplicate_pages', 'retry_number'}
    from execution_facts import integer
    for field in integer_fields:
        try:
            integer(row.get(field))
        except ValueError:
            errors.append('execution_integer_invalid:' + field)
    if raw:
        for field in OBSERVATION_FIELDS:
            if field in row and field not in integer_fields and field not in BOOL_FIELDS and not isinstance(row[field], str):
                errors.append('execution_field_type_invalid:' + field)
    for field in REQUIRED_OBSERVATIONS + (() if raw else ('search_id',)):
        if field not in row or row[field] in ('', None):
            errors.append('execution_credential_missing:' + field)
    for field in BOOL_FIELDS:
        value = row.get(field)
        if not (isinstance(value, bool) if raw else isinstance(value, bool) or value in ('true', 'false')):
            errors.append('execution_boolean_invalid:' + field)
    if row.get('search_tool') not in TOOLS:
        errors.append('execution_search_tool_unregistered')
    from retrieval_controls import EXECUTED_STATUSES
    from execution_semantics import ITERATION_ACTIONS
    if not isinstance(row.get('status'), str) or row['status'] not in EXECUTED_STATUSES:
        errors.append('execution_status_invalid')
    if not isinstance(row.get('next_action'), str) or row['next_action'] not in ITERATION_ACTIONS:
        errors.append('execution_next_action_invalid')
    for field in ('started_at', 'finished_at', 'retrieved_at'):
        try:
            timestamp(row.get(field))
        except ValueError as exc:
            errors.append(f'execution_time_invalid:{field}={row.get(field)!r}:{exc}')
    try:
        if not timestamp(row['started_at']) <= timestamp(row['retrieved_at']) <= timestamp(row['finished_at']):
            errors.append('execution_retrieved_outside_window')
    except (ValueError, KeyError):
        pass
    return errors
