"""Validate captured connection facts without performing or inventing network access."""
from __future__ import annotations
import ipaddress
import strict_json as json
from collections.abc import Mapping
from datetime import datetime, timezone
from input_safety import MAX_REDIRECT_HOPS

NETWORK_FIELDS = ('network_observation', 'network_observation_sha256', 'platform_mapping_sha256', 'final_url')
SCHEMA = 'public-network-observation-1'
TOOL_SCHEMA = 'tool-page-observation-1'
TOOL_TRACE_FIELDS = {'schema_version', 'tool_name', 'tool_call_id', 'result_ref',
    'request_url', 'final_url', 'page_title', 'retrieved_at', 'body_format'}
OPTIONAL_TOOL_TRACE_FIELDS = {'tool_call_id', 'result_ref'}
TOOL_BINDING_FIELDS = {'task_run_id', 'plan_id', 'query_id', 'execution_id', 'source_capture_id',
    'response_body_sha256', 'page_title_sha256', 'tool_artifact'}


def normalize_tool_trace(value, *, url, final_url, retrieved_at, bound=False):
    """Validate retained page-tool metadata, without inventing transport facts."""
    from input_safety import InputError, parse_public_url
    from source_identity import strict_url_identity
    from temporal_fields import timestamp
    required = TOOL_TRACE_FIELDS | (TOOL_BINDING_FIELDS if bound else set())
    schema = TOOL_SCHEMA if bound else 'tool-page-trace-1'
    if (not isinstance(value, Mapping) or set(value) - required
            or not (required - OPTIONAL_TOOL_TRACE_FIELDS).issubset(value)
            or value.get('schema_version') != schema):
        raise InputError('invalid_tool_trace_schema')
    value = {**dict.fromkeys(OPTIONAL_TOOL_TRACE_FIELDS, ''), **value}
    if any(not isinstance(value[k], str) or
           (k not in OPTIONAL_TOOL_TRACE_FIELDS and not value[k].strip()) for k in required):
        raise InputError('tool_trace_field_missing')
    if value['body_format'] not in {'text/plain', 'text/html', 'application/json'}:
        raise InputError('tool_trace_body_format_invalid')
    for key in ('request_url', 'final_url'):
        parse_public_url(value[key])
    if (strict_url_identity(value['request_url']) != strict_url_identity(url)
            or strict_url_identity(value['final_url']) != strict_url_identity(final_url or url)):
        raise InputError('tool_trace_url_mismatch')
    if timestamp(value['retrieved_at']) != timestamp(retrieved_at):
        raise InputError('tool_trace_time_mismatch')
    if timestamp(retrieved_at) > datetime.now(timezone.utc):
        raise InputError('tool_trace_time_in_future')
    if bound:
        for key in ('response_body_sha256', 'page_title_sha256'):
            if len(value[key]) != 64 or any(c not in '0123456789abcdef' for c in value[key]):
                raise InputError('tool_trace_hash_invalid')
    return dict(value)


def global_address(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('network_address_invalid')
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError('network_address_invalid') from exc
    target = getattr(address, 'ipv4_mapped', None) or address
    if not target.is_global or any((target.is_loopback, target.is_link_local,
            target.is_multicast, target.is_unspecified, target.is_reserved, target.is_private)):
        raise ValueError('network_address_not_public')
    return str(address)


def normalize_observation(value, *, url, final_url='', retrieved_at):
    from source_identity import trusted_domain, redirect_node_identity, strict_url_identity
    from temporal_fields import timestamp
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError('network_observation_invalid_json') from exc
    if isinstance(value, Mapping) and value.get('schema_version') == TOOL_SCHEMA:
        return normalize_tool_trace(value, url=url, final_url=final_url,
                                    retrieved_at=retrieved_at, bound=True)
    if not isinstance(value, Mapping) or value.get('schema_version') != SCHEMA:
        raise ValueError('public_network_observation_required')
    if set(value) != {'schema_version', 'tool_call_id', 'hops'}:
        raise ValueError('network_observation_fields_invalid')
    if not isinstance(value['tool_call_id'], str) or not value['tool_call_id'].strip():
        raise ValueError('network_actual_tool_call_required')
    hops = value.get('hops')
    if not isinstance(hops, list) or not 1 <= len(hops) <= MAX_REDIRECT_HOPS:
        raise ValueError('network_connection_hops_required')
    result = []
    seen = set()
    previous_time = None
    for hop in hops:
        if not isinstance(hop, Mapping) or set(hop) != {'url', 'resolved_addresses', 'connected_address', 'observed_at'}:
            raise ValueError('network_connection_hop_fields_invalid')
        host = trusted_domain(hop['url'])
        normalized_url = redirect_node_identity(hop['url'])
        if normalized_url in seen:
            from input_safety import InputError
            raise InputError('redirect_cycle')
        seen.add(normalized_url)
        addresses = hop['resolved_addresses']
        if not isinstance(addresses, list) or not addresses:
            raise ValueError('network_actual_resolution_required')
        addresses = sorted(set(global_address(item) for item in addresses))
        connected = global_address(hop['connected_address'])
        if connected not in addresses:
            raise ValueError('network_connection_resolution_mismatch')
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal and str(literal) != connected:
            raise ValueError('network_literal_connection_mismatch')
        observed = timestamp(hop['observed_at'])
        if observed > timestamp(retrieved_at) or observed > datetime.now(timezone.utc):
            raise ValueError('network_observation_after_capture')
        if previous_time and observed < previous_time:
            raise ValueError('network_redirect_time_reversed')
        previous_time = observed
        # Transport evidence preserves the strict request identity. Page-level
        # canonicalization is reserved for deduplication and must not erase a
        # parameter, query order, or tracking-bearing URL that was observed.
        result.append({'url': strict_url_identity(hop['url']), 'resolved_addresses': addresses,
            'connected_address': connected, 'observed_at': hop['observed_at']})
    if (result[0]['url'] != strict_url_identity(url)
            or result[-1]['url'] != strict_url_identity(final_url or url)):
        raise ValueError('network_redirect_identity_mismatch')
    return {'schema_version': SCHEMA, 'tool_call_id': value['tool_call_id'], 'hops': result}


def network_errors(row, *, label='source'):
    from runtime_guard import canonical_sha256
    try:
        normalized = normalize_observation(row.get('network_observation'),
            url=row.get('url'), final_url=row.get('final_url', ''), retrieved_at=row.get('retrieved_at'))
        if row.get('network_observation_sha256') != canonical_sha256(normalized):
            raise ValueError('network_observation_hash_mismatch')
    except (ValueError, TypeError, KeyError) as exc:
        return [f'{label}: {exc}']
    return []
