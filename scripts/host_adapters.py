"""Normalize saved provider responses; discovery never becomes page evidence."""
import re
import strict_json as json
from source_identity import strict_url_identity


def result_array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("results", "items", "organic_results", "organic", "value"):
            if key in value:
                if not isinstance(value[key], list):
                    raise ValueError("provider_results_must_be_array")
                return value[key]
        for key in ("web", "webPages", "data"):
            if key in value:
                found = result_array(value[key])
                if found is not None:
                    return found
    return None


def normalize_result(item):
    if isinstance(item, str):
        item = {"url": item}
    if not isinstance(item, dict):
        raise ValueError("search_result_must_be_object_or_url")
    url = item.get("url") or item.get("link") or item.get("href")
    result = {key: str(item[key]) for key in ("title", "snippet", "description")
              if item.get(key) is not None}
    if url:
        value = strict_url_identity(url)
        if not value:
            raise ValueError("search_url_invalid_public_http_required")
        result["url"] = value
    else:
        reason = item.get("unavailable_reason")
        result["unavailable_reason"] = (reason.strip() if isinstance(reason, str) and reason.strip()
                                        else "provider_result_without_url")
    return result


def normalize_discovery(response_text, *, search_results=None, urls=None, expected_count=None):
    if not isinstance(response_text, str):
        raise ValueError("search_response_must_be_text")
    if expected_count is not None and (type(expected_count) is not int or expected_count < 0):
        raise ValueError("result_count_must_be_nonnegative_integer")
    if urls is not None and not isinstance(urls, list):
        raise ValueError("search_urls_must_be_array")
    supplied = [normalize_result(value)["url"] for value in urls or []]
    try:
        structured = json.loads(response_text)
    except ValueError:
        structured = None
    entries = result_array(structured)
    basis = "structured_response"
    if entries is None:
        basis = "declared_result_list"
        entries = search_results
        if entries is None and urls is not None:
            entries = urls
        if entries is None:
            basis = "text_response"
            entries = re.findall(r"https?://[^\s<>\]\"']+", response_text)
            entries = [url.rstrip(").,;，。；") for url in entries]
            if not entries and response_text.strip():
                entries = [{"unavailable_reason": "provider_result_without_url"}]
    if not isinstance(entries, list):
        raise ValueError("provider_results_must_be_array")
    normalized = [normalize_result(entry) for entry in entries]
    count = len(normalized)
    if expected_count is not None and count != expected_count:
        raise ValueError("discovery_result_count_mismatch_reuse_saved_tool_response")
    discovered = list(dict.fromkeys(item["url"] for item in normalized if "url" in item))
    if any(url not in discovered for url in supplied):
        raise ValueError("search_urls_not_in_result_list")
    if basis == "structured_response" and search_results is not None:
        if not isinstance(search_results, list):
            raise ValueError("provider_results_must_be_array")
        declared = [normalize_result(entry) for entry in search_results]
        # Compare all positions, including duplicate and URL-less records.
        identity = lambda rows: [item.get("url") for item in rows]
        if identity(declared) != identity(normalized):
            raise ValueError("search_result_list_conflicts_with_tool_response")
    return discovered, {"result_count": count, "result_entries": normalized,
        "unique_result_url_count": len(discovered), "persisted_result_url_count": len(discovered),
        "non_url_result_count": sum("url" not in item for item in normalized),
        "completeness_basis": basis}
FETCH_ADAPTERS = {
    'host_web_fetch': {'tool_name':'host_page_read', 'execution':'retained_host_response'},
    'browser_fetch': {'tool_name':'browser_page_read', 'execution':'retained_host_response'},
    'provider_page_read': {'tool_name':'provider_page_read', 'execution':'retained_host_response'},
    'curl': {'tool_name':'curl', 'execution':'local_subprocess'},
    'requests': {'tool_name':'requests', 'execution':'local_http'},
}


def page_identity(adapter_id, claimed_tool=None):
    adapter = FETCH_ADAPTERS.get(adapter_id)
    if adapter is None:
        raise ValueError('unregistered_fetch_adapter')
    if claimed_tool and claimed_tool != adapter['tool_name']:
        raise ValueError('fetch_adapter_identity_mismatch')
    return {'tool_name':adapter['tool_name'], 'actual_fetch_method':adapter_id}


def validate_user_basis(basis, visible):
    if not isinstance(basis,dict) or set(basis)!={'author_type','basis_excerpt','user_content_excerpt'}:
        raise ValueError('observed_user_origin_basis_required')
    if basis['author_type'] not in ('individual','community_participant'):
        raise ValueError('user_origin_not_established')
    for key in ('basis_excerpt','user_content_excerpt'):
        text=basis[key]
        if not isinstance(text,str) or not text.strip() or visible.count(text)!=1:
            raise ValueError('user_origin_basis_not_in_retained_page')
    return basis


def imported_page(value, *, adapter_id='host_web_fetch'):
    """Host response import is traceable evidence, not an independently attested fetch."""
    from input_safety import parse_public_url
    from temporal_fields import timestamp, utc_now
    adapter = FETCH_ADAPTERS.get(adapter_id)
    if not adapter or adapter['execution'] != 'retained_host_response':
        raise ValueError('local_fetch_requires_executing_adapter')
    for key in ('request_url', 'final_url'):
        parse_public_url(value.get(key))
    if not isinstance(value.get('page_body'), str) or not value['page_body'].strip():
        raise ValueError('actual_page_body_required')
    identity = page_identity(adapter_id, value.get('tool_name'))
    if value.get('actual_fetch_method') not in (None, adapter_id):
        raise ValueError('fetch_adapter_identity_mismatch')
    at = value.get('retrieved_at') or utc_now()
    timestamp(at)
    return {**value, **identity, 'retrieved_at':at}
