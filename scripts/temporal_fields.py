"""Exact calendar and timezone parsing shared by intake and independent audits."""
import re
import base64
import hashlib
import strict_json as json
from datetime import date, datetime, timezone

TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00))')
PUBLICATION = re.compile(r'\d{4}(?:-\d{2}(?:-\d{2})?)?')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def plan_state_binding(state_path):
    if state_path is None:
        return {}
    content = state_path.read_bytes()
    return {'state_snapshot_base64': base64.b64encode(content).decode('ascii'),
            'state_snapshot_sha256': hashlib.sha256(content).hexdigest()}


def plan_time_bounds(binding, registration, *, task_run_id, task_created_at):
    """Verify protected plan-time snapshots; no observation-owned timestamp is accepted."""
    proof = binding.get('temporal_binding', {})
    try:
        content = base64.b64decode(proof['state_snapshot_base64'], validate=True)
        if hashlib.sha256(content).hexdigest() != proof['state_snapshot_sha256']:
            raise ValueError('plan_state_temporal_hash_mismatch')
        state = json.loads(content.decode('utf-8-sig'))
        if state.get('task_run_id') != task_run_id or state.get('created_at') != task_created_at:
            raise ValueError('plan_state_temporal_identity_mismatch')
        entries = [timestamp(e['at']) for e in state.get('history', [])
                   if e.get('phase') == 'SEARCH']
        if state.get('phase') != 'SEARCH' or not entries:
            raise ValueError('plan_search_phase_proof_missing')
        bounds = {'task_created_at': timestamp(task_created_at), 'search_phase_entered_at': max(entries),
            'plan_generated_at': timestamp(binding['generated_at']),
            'plan_registered_at': timestamp(registration['registered_at'])}
        if not bounds['task_created_at'] <= bounds['search_phase_entered_at'] <= bounds['plan_generated_at'] <= bounds['plan_registered_at']:
            raise ValueError('plan_time_lineage_reversed')
        if int(binding['iteration_round']) > 0:
            bounds['iteration_admitted_at'] = timestamp(binding['audit_snapshot']['plan_generation_time'])
            if bounds['iteration_admitted_at'] > bounds['plan_generated_at']:
                raise ValueError('iteration_admission_after_plan')
        return bounds
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('plan_time_lineage_invalid:' + str(exc)) from exc


def timestamp(value):
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise ValueError('timestamp_requires_exact_iso_timezone')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError('timestamp_invalid_calendar') from exc


def review_time(value, *, not_before=()):
    """Check review chronology without rewriting the signed time string."""
    instant = timestamp(value)
    if value.endswith('-00:00'):
        raise ValueError('review_time_timezone_unknown')
    if instant > datetime.now(timezone.utc):
        raise ValueError('review_time_in_future')
    for boundary in not_before:
        if instant < timestamp(boundary):
            raise ValueError('review_time_precedes_evidence')
    return instant


def publication(value):
    """Return (year, precision); absent publication is not an invented date."""
    if value in ('', None):
        return None
    if not isinstance(value, str) or not PUBLICATION.fullmatch(value):
        raise ValueError('publication_requires_year_month_or_day')
    parts = [int(p) for p in value.split('-')]
    try:
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError as exc:
        raise ValueError('publication_invalid_calendar') from exc
    return parts[0], ('year', 'month', 'day')[len(parts)-1]


def date_errors(row, *, label, capture=False):
    errors = []
    for field, parser in [('published_at', publication)] + ([('retrieved_at', timestamp)] if capture else []):
        try:
            parser(row.get(field, ''))
        except ValueError as exc:
            errors.append(f'{label}: {field}={row.get(field)!r}: {exc}')
    limits = [datetime.now(timezone.utc).date()]
    for field in ('retrieved_at', 'captured_at', 'research_cutoff'):
        value = row.get(field)
        if value:
            try:
                limits.append(date.fromisoformat(value) if field == 'research_cutoff' and len(value) == 10
                              else timestamp(value).date())
            except (ValueError, TypeError):
                errors.append(f'{label}: {field}: invalid_time_limit')
    try:
        if publication(row.get('published_at')):
            parts = [int(p) for p in row['published_at'].split('-')]
            earliest = date(parts[0], parts[1] if len(parts) > 1 else 1,
                            parts[2] if len(parts) > 2 else 1)
            if earliest > min(limits):
                errors.append(f'{label}: published_at={row["published_at"]!r}: publication_after_capture_or_cutoff')
    except (ValueError, TypeError):
        if not errors:
            errors.append(f'{label}: publication_time_comparison_invalid')
    return errors


def publication_year(value):
    try:
        parsed = publication(value)
        return str(parsed[0]) if parsed else ''
    except ValueError:
        return ''
