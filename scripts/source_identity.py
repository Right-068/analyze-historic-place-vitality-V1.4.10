#!/usr/bin/env python3
"""Release-wide URL, platform, and page-entity identity helpers.

The functions in this module are deterministic and intentionally independent
from candidate evidence.  Capture, admission, audit, scoring, and reporting
must use these helpers instead of trusting a model-authored identity field.
"""

from __future__ import annotations

import hashlib
import strict_json as json
import posixpath
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlunparse
from input_safety import safe_urlparse as urlparse
from input_safety import (parse_public_url, safe_urlparse, InputError,
                          raw_query_parts, query_key, query_parameter_names)


def trusted_domain(value: object) -> str:
    """Full HTTP(S) hostname, never an author-supplied diversity label."""
    return parse_public_url(value)[1]


def domain_errors(row, *, label='source'):
    try:
        actual = trusted_domain(identity_url(row))
        if _clean_host(str(row.get('domain', ''))) != actual:
            return [f'{label}: domain: source_domain_mismatch']
    except ValueError as exc:
        return [f'{label}: {exc}']
    return []


def platform_errors(row, *, label='source', expected_source=None):
    identity = platform_identity(row.get('url', ''), row.get('final_url', '')) if expected_source is None else (
        platform_identity(expected_source.get('url', ''), expected_source.get('final_url', '')))
    errors = []
    for field in ('platform', 'platform_id'):
        value = row.get(field)
        if value and normalize_platform_id(value) != identity['platform_id']:
            errors.append(f'{label}: {field}: platform_identity_conflict')
    if expected_source is None and row.get('platform_mapping_sha256') != platform_mapping_sha256():
        errors.append(f'{label}: platform_mapping_hash_mismatch')
    return errors


def platform_mapping_sha256():
    return hashlib.sha256(PLATFORM_ALIASES_PATH.read_bytes()).hexdigest()


def source_qualification_errors(row, *, label='source'):
    from public_network import network_errors
    from temporal_fields import date_errors
    from host_receipts import proof_errors
    from input_safety import privacy_errors
    return (domain_errors(row, label=label) + platform_errors(row, label=label)
            + network_errors(row, label=label) + date_errors(row, label=label, capture=True)
            + identity_errors(row, label=label) + proof_errors(row, label=label)
            + privacy_errors(row, label=label))


def linked_evidence_errors(row, source, *, label='evidence'):
    from temporal_fields import date_errors
    if source is None:
        return [label + ': missing_linked_source']
    from input_safety import privacy_errors
    return privacy_errors(row, label=label) + platform_errors(row, label=label, expected_source=source) + date_errors(
        {**row, 'retrieved_at': source.get('retrieved_at'),
         'research_cutoff': source.get('research_cutoff')}, label=label)


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
}
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
DEFAULT_INDEX_NAMES = {
    "index.html",
    "index.htm",
    "index.shtml",
    "default.html",
    "default.htm",
}
MULTIPART_PUBLIC_SUFFIXES = {
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "com.hk",
    "com.tw",
    "co.uk",
    "org.uk",
    "com.au",
    "co.jp",
}
ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ALIASES_PATH = ROOT / "assets" / "platform-aliases.json"


def load_platform_aliases(path: Path = PLATFORM_ALIASES_PATH) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("platforms"), list):
        raise ValueError("controlled platform aliases are invalid")
    return payload


def trusted_independent_source_ids(
    evidence: Iterable[Mapping[str, object]],
) -> set[str]:
    """Return sources admitted through the signed independent-occurrence path.

    A candidate cannot create this status: the pre-admission layer replaces
    candidate collision assertions with its own deterministic decision and
    only emits ``verified_independent_occurrence`` after validating a signed
    collision review.  Downstream page identity may therefore use the released
    evidence status without reopening the private signing key.
    """

    from review_trust import collision_evidence_proof_valid
    result = set()
    for row in evidence:
        if str(row.get('collision_status', '')).strip() != 'verified_independent_occurrence':
            continue
        if collision_evidence_proof_valid(row):
            result.add(str(row['source_id']))
    return result


def _clean_host(host: str) -> str:
    from input_safety import normalize_host
    try:
        return normalize_host(host.strip())
    except (UnicodeError, ValueError):
        return ""


def _normalize_percent_encoding(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        character = chr(byte)
        return character if character in UNRESERVED else f"%{byte:02X}"

    return re.sub(r"%([0-9a-fA-F]{2})", replace, value)


def _legacy_normalized_path(value: str) -> str:
    """Former lossy spelling, only for read-only historical recognition."""
    raw = _normalize_percent_encoding(value or "/")
    raw = re.sub(r"/{2,}", "/", raw)
    trailing = raw.endswith("/")
    normalized = posixpath.normpath(raw)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized in {"/.", "."}:
        normalized = "/"
    final_name = normalized.rsplit("/", 1)[-1].casefold()
    if final_name in DEFAULT_INDEX_NAMES:
        normalized = normalized[: -(len(final_name))].rstrip("/") or "/"
    elif normalized != "/" and trailing:
        normalized = normalized.rstrip("/")
    return quote(normalized, safe="/%:@!$&'()*+,;=-._~")


def _normalized_path(value: str) -> str:
    """Keep resource-sensitive path segments; never infer server routing."""
    def percent(match):
        character = chr(int(match.group(1), 16))
        # Encoded dots can participate in server-specific routing. Keep them
        # encoded, just as encoded slashes remain non-structural bytes.
        return (character if character in UNRESERVED and character != "."
                else "%" + match.group(1).upper())
    normalized = re.sub(r"%([0-9a-fA-F]{2})", percent, value or "/")
    return quote(normalized, safe="/%:@!$&'()*+,;=-._~")


def strict_url_identity(value: object) -> str:
    """Normalize transport identity without applying page-equivalence rules.

    Host case and protocol-matched default ports are transport spelling, while
    path parameters, query order, repeated keys, and percent-encoded bytes are
    part of the signed request identity.  This function is therefore used for
    host-receipt binding, never for page-count deduplication.
    """
    try:
        parsed, _ = parse_public_url(value)
    except ValueError:
        return ""
    host = _clean_host(parsed.hostname or "")
    if not host:
        return ""
    try:
        port_number = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    default_port = 80 if scheme == "http" else 443
    port = "" if port_number in {None, default_port} else f":{port_number}"
    netloc = ('[' + host + ']' if ':' in host else host) + port
    # Reassembly through urlunparse loses a present-but-empty '?' or ';'.
    # Keep the exact request target after the validated authority instead.
    authority_tail = value.split('://', 1)[1]
    boundary = min((authority_tail.find(c) for c in '/?#' if c in authority_tail), default=len(authority_tail))
    target = authority_tail[boundary:].split('#', 1)[0]
    if not target.startswith('/'):
        target = '/' + target
    return scheme + '://' + netloc + target


def redirect_node_identity(value: object) -> str:
    """Validated GET transport node, scoped to one observed redirect chain.

    The released receipt does not support method changes; no inferred method
    or cross-attempt visited state is introduced here.
    """
    parse_public_url(value, field='redirect_chain.url')
    return strict_url_identity(value)


def _page_query(query: str) -> str:
    """Only remove explicitly allowed tracking names; keep business bytes."""
    kept = []
    for key, equals, value in raw_query_parts(query):
        name = query_key(key)
        if name.startswith('utm_') or name in TRACKING_QUERY_KEYS:
            continue
        kept.append(_normalize_percent_encoding(key) + equals + _normalize_percent_encoding(value))
    return '&'.join(kept)


def page_url_identity(value: object, final_url: object = "") -> str:
    """Return one canonical page URL while preserving business parameters.

    A verified final redirect URL wins over the originally requested URL.
    A scheme is never changed merely because HTTPS often exists.  Only
    protocol-matched default ports, fragments, high-confidence tracking
    parameters and safe character spellings are normalized. Default index
    names, trailing/repeated slashes, dot segments, encoded dots/slashes and
    case-sensitive path names are not inferred routing aliases.
    Query parameters retain their original order, including the relative
    order of repeated keys.  Ambiguous business parameters remain part of
    page identity.
    """

    candidate = final_url or value
    try:
        parsed, host = parse_public_url(candidate)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = _clean_host(parsed.hostname)
    if not host:
        return ""
    try:
        port_number = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    port = ""
    default_port = 80 if scheme == "http" else 443
    if port_number not in {None, default_port}:
        port = f":{port_number}"
    from input_safety import SENSITIVE_QUERY_KEYS
    names = query_parameter_names(parsed.query)
    # A signed/authenticated query may cover the entire target, including
    # apparent tracking keys. Do not apply equivalence rules to such targets.
    if any(name in SENSITIVE_QUERY_KEYS or name in {'signature', 'sig'}
           or name.startswith(('x-amz-', 'x-goog-')) for name in names):
        return strict_url_identity(candidate)
    query = _page_query(parsed.query)
    raw_target = strict_url_identity(candidate).split('://', 1)[1].split('/', 1)[1]
    raw_path = raw_target.split('?', 1)[0]
    params = quote(_normalize_percent_encoding(parsed.params), safe="/%:@!$&'()*+,;=-._~")
    target = _normalized_path(parsed.path)
    if parsed.params or raw_path.endswith(';'):
        target += ';' + params
    # Removing tracking fields must not also remove a retained empty field.
    retained_empty = ('', '', '') in raw_query_parts(parsed.query)
    if query or ('?' in raw_target and (not parsed.query or retained_empty)):
        target += '?' + query
    return scheme + '://' + ('[' + host + ']' if ':' in host else host) + port + target


def canonical_url(value: object, final_url: object = "") -> str:
    """Compatibility name for page identity ONLY; never transport or display."""
    return page_url_identity(value, final_url)


def normalized_url_sha256(value: object, final_url: object = "") -> str:
    normalized = canonical_url(value, final_url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def legacy_sorted_normalized_url_sha256(value: object, final_url: object = "") -> str:
    """Return the former sorted-query identity for read-only resume checks.

    New capture and page identity always use :func:`canonical_url`.  This
    helper exists only so a previously persisted hash can be recognized during
    migration without rewriting or merging its source records.
    """

    candidate = str(final_url or "").strip() or str(value or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = _clean_host(parsed.hostname)
    if not host:
        return ""
    try:
        port_number = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    default_port = 80 if scheme == "http" else 443
    port = "" if port_number in {None, default_port} else f":{port_number}"
    try:
        decoded = parse_qsl(parsed.query, keep_blank_values=True, errors='strict')
    except UnicodeError:
        # Lossy historical hashes cannot be reconstructed by guessing bytes.
        return ''
    query = [
        (key, item)
        for key, item in decoded
        if not key.casefold().startswith("utm_")
        and key.casefold() not in TRACKING_QUERY_KEYS
    ]
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    legacy = urlunparse(
        (
            scheme,
            host + port,
            _legacy_normalized_path(parsed.path),
            "",
            urlencode(query, doseq=True),
            "",
        )
    )
    return hashlib.sha256(legacy.encode("utf-8")).hexdigest()


def normalized_url_hash_is_compatible(
    stored_hash: object,
    value: object,
    final_url: object = "",
) -> bool:
    """Require the current final-content hash; legacy migration is explicit."""

    stored = str(stored_hash or "").strip().casefold()
    if not stored:
        return False
    return stored == normalized_url_sha256(value, final_url)


IDENTITY_FIELDS = ("request_url", "discovery_url", "discovery_domain", "declared_canonical_url",
                   "source_identity_url", "content_domain", "identity_rule_sha256")


def identity_rule_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def identity_url(row):
    """Final response is the only content identity; declarations never override it."""
    return canonical_url(row.get("final_url") or row.get("url"))


def identity_fields(request, final="", declared=""):
    request_host = trusted_domain(request)
    final = final or request
    content_host = trusted_domain(final)
    normalized = canonical_url(final)
    if declared:
        # Preserve a safe same-host declaration as a hint. It is deliberately
        # not an aliasing instruction from an untrusted page.
        if trusted_domain(declared) != content_host:
            raise InputError("cross_domain_canonical_rejected", field="declared_canonical_url")
    return {"request_url": request, "discovery_url": request, "discovery_domain": request_host,
            "declared_canonical_url": declared, "source_identity_url": normalized,
            "content_domain": content_host, "identity_rule_sha256": identity_rule_sha256()}


def identity_errors(row, *, label="source"):
    try:
        expected = identity_fields(row.get("url"), row.get("final_url"), row.get("declared_canonical_url", ""))
        for field, value in expected.items():
            if row.get(field) != value:
                raise InputError("source_identity_field_mismatch", field=field)
        if row.get("normalized_url_sha256") != normalized_url_sha256(expected["source_identity_url"]):
            raise InputError("source_identity_hash_mismatch", field="normalized_url_sha256")
        if "canonical_url" in row and row["canonical_url"] != expected["source_identity_url"]:
            raise InputError("source_identity_field_mismatch", field="canonical_url")
        for key in ("url", "request_url", "discovery_url", "final_url", "declared_canonical_url", "source_identity_url"):
            if row.get(key):
                parsed, _ = parse_public_url(row[key], field=key)
                from input_safety import SENSITIVE_QUERY_KEYS, url_privacy_errors
                if any(k in SENSITIVE_QUERY_KEYS for k in query_parameter_names(parsed.query)):
                    raise InputError("url_contains_sensitive_query", field=key)
                if url_privacy_errors(row[key], field=key):
                    raise InputError("url_contains_personal_identifier", field=key)
    except (ValueError, TypeError) as exc:
        return [f"{label}: {exc}"]
    return []


def registrable_domain(hostname: object) -> str:
    host = _clean_host(str(hostname or ""))
    parts = [part for part in host.split(".") if part]
    if len(parts) <= 2:
        return host
    suffix2 = ".".join(parts[-2:])
    if suffix2 in MULTIPART_PUBLIC_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return suffix2


def _platform_tables(
    payload: Mapping[str, object] | None = None,
) -> tuple[dict[str, dict[str, str]], list[tuple[str, dict[str, str]]], dict[str, str]]:
    config = dict(payload or load_platform_aliases())
    exact: dict[str, dict[str, str]] = {}
    suffixes: list[tuple[str, dict[str, str]]] = []
    labels: dict[str, str] = {}
    for raw in config.get("platforms", []):
        if not isinstance(raw, Mapping):
            continue
        platform_id = str(raw.get("platform_id", "")).strip()
        if not platform_id:
            continue
        metadata = {
            "platform_id": platform_id,
            "mapping_rule_source": str(raw.get("rule_source", "发布版受控平台别名表")),
            "display_name": str(raw.get("display_name", platform_id)),
        }
        for host in raw.get("hosts", []):
            exact[_clean_host(str(host))] = metadata
        for prefix in raw.get("host_path_prefixes", []):
            exact[_clean_host(str(prefix).split("/", 1)[0]) + "/" + str(prefix).split("/", 1)[1].strip("/")] = metadata
        for suffix in raw.get("host_suffixes", []):
            suffixes.append(("." + _clean_host(str(suffix).lstrip(".")), metadata))
        labels[platform_id.casefold()] = platform_id
        labels[str(raw.get("display_name", "")).strip().casefold()] = platform_id
        for label in raw.get("legacy_labels", []):
            labels[str(label).strip().casefold()] = platform_id
    return exact, suffixes, labels


def normalize_platform_id(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    _, _, labels = _platform_tables()
    return labels.get(raw.casefold(), raw)


def platform_identity(value: object, final_url: object = "") -> dict[str, str]:
    normalized = canonical_url(value, final_url)
    try:
        trusted_domain(value)
        if final_url:
            trusted_domain(final_url)
    except ValueError:
        normalized = ''
    try:
        parsed_original = urlparse(str(final_url or "").strip() or str(value or "").strip())
    except ValueError:
        parsed_original = urlparse("")
    original_host = _clean_host(parsed_original.hostname or "")
    canonical_host = _clean_host(urlparse(normalized).hostname or "")
    if not normalized:
        return {
            "platform_id": "",
            "original_hostname": original_host,
            "canonical_hostname": "",
            "resolution_basis": "invalid_url",
            "mapping_rule_source": "",
            "display_name": "",
        }
    exact, suffixes, _ = _platform_tables()
    path_key = (canonical_host + (urlparse(normalized).path or "")).rstrip("/")
    controlled = exact.get(canonical_host)
    if controlled is None:
        for alias, metadata in exact.items():
            if "/" in alias and (path_key == alias or path_key.startswith(alias + "/")):
                controlled = metadata
                break
    if controlled is None:
        for suffix, metadata in suffixes:
            if canonical_host.endswith(suffix) and canonical_host != suffix.lstrip("."):
                controlled = metadata
                break
    if controlled:
        platform_id = controlled["platform_id"]
        basis = "controlled_platform_alias"
        rule_source = controlled["mapping_rule_source"]
        display_name = controlled["display_name"]
    else:
        # ``www`` is a presentation hostname, not a service/channel identity.
        # Other prefixes (including mobile or content subdomains) remain
        # separate unless the released alias table explicitly maps them.
        platform_id = canonical_host[4:] if canonical_host.startswith("www.") else canonical_host
        basis = "generic_www_presentation_alias" if platform_id != canonical_host else "unmapped_service_hostname"
        rule_source = "通用仅去除www展示前缀" if platform_id != canonical_host else "保守独立策略"
        display_name = platform_id
    return {
        "platform_id": platform_id or canonical_host,
        "original_hostname": original_host,
        "canonical_hostname": canonical_host,
        "resolution_basis": basis,
        "mapping_rule_source": rule_source,
        "display_name": display_name,
    }


def content_fingerprint(source: Mapping[str, object]) -> str:
    if str(source.get("content_layer", "")) == "rating_only":
        return ""
    for field in (
        "capture_snapshot_sha256",
        "source_snapshot_sha256",
        "snapshot_sha256",
        "content_sha256",
    ):
        value = str(source.get(field, "")).strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def page_entity_id_for_source(
    source: Mapping[str, object],
    *,
    trusted_independent_occurrence: bool = False,
) -> str:
    """Derive a page entity without trusting source_id or candidate fields."""

    if source.get("url") and not platform_identity(source.get("url"), source.get("final_url", ""))["platform_id"]:
        return ""
    declared = str(source.get("page_entity_id", "")).strip()
    declared_basis = str(source.get("page_entity_identity_basis", ""))
    if (
        not trusted_independent_occurrence
        and declared.startswith("PAGE-")
        and declared_basis.startswith("machine_")
    ):
        return declared
    normalized = canonical_url(source.get("url", ""), source.get("final_url", ""))
    if not normalized:
        # Legacy in-memory fixtures and pre-migration ledgers may not yet have
        # a URL-derived identity.  Keep their rows distinct for compatibility;
        # the formal evidence audit still rejects a released source that lacks
        # its required real URL and URL hash.
        declared = str(source.get("page_entity_id", "")).strip()
        if declared:
            return declared
        source_id = str(source.get("source_id", "")).strip()
        if not source_id:
            return ""
        return "PAGE-LEGACY-" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:24]
    fingerprint = content_fingerprint(source)
    if fingerprint and not trusted_independent_occurrence:
        return "PAGE-CONTENT-" + fingerprint[:24]
    return "PAGE-URL-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def annotate_page_entities(
    sources: Iterable[Mapping[str, object]],
    *,
    trusted_independent_source_ids: set[str] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Return source copies with one deterministic page and platform identity."""

    trusted = trusted_independent_source_ids or set()
    rows = [dict(raw) for raw in sources]
    parent = list(range(len(rows)))
    merge_reasons: defaultdict[int, set[str]] = defaultdict(set)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int, reason: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            parent[right_root] = left_root
        merge_reasons[left].add(reason)
        merge_reasons[right].add(reason)

    canonicals: defaultdict[str, list[int]] = defaultdict(list)
    fingerprints: defaultdict[str, list[int]] = defaultdict(list)
    derived_urls = []
    for index, row in enumerate(rows):
        canonical = canonical_url(row.get("url", ""), row.get("final_url", ""))
        if row.get("url") and not platform_identity(row.get("url"), row.get("final_url", ""))["platform_id"]:
            canonical = ""
        # Identity annotation is not an admission or repair operation. Keep
        # supplied caches (including blank or conflicting hashes) visible to
        # qualification. Grouping only uses the independently derived URL.
        derived_urls.append(canonical)
        row.setdefault("canonical_url", canonical)
        if canonical:
            canonicals[canonical].append(index)
        fingerprint = content_fingerprint(row)
        source_id = str(row.get("source_id", ""))
        if canonical and fingerprint and source_id not in trusted:
            fingerprints[fingerprint].append(index)
    for indexes in canonicals.values():
        for other in indexes[1:]:
            union(indexes[0], other, "canonical_url")
    for indexes in fingerprints.values():
        for other in indexes[1:]:
            union(indexes[0], other, "identical_content_fingerprint")

    components: defaultdict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        components[find(index)].append(index)
    groups: defaultdict[str, list[str]] = defaultdict(list)
    reasons: defaultdict[str, set[str]] = defaultdict(set)
    for indexes in components.values():
        if all(rows[index].get("url") and not derived_urls[index] for index in indexes):
            for index in indexes:
                rows[index].update(page_entity_id="", page_entity_identity_basis="invalid_public_identity")
            continue
        component_urls = sorted({derived_urls[index] for index in indexes if derived_urls[index]})
        component_fingerprints = sorted({content_fingerprint(rows[index]) for index in indexes if content_fingerprint(rows[index])})
        if len(component_urls) > 1 and component_fingerprints:
            page_id = "PAGE-CONTENT-" + component_fingerprints[0][:24]
            identity_basis = "machine_content_collision"
        elif component_urls:
            page_id = "PAGE-URL-" + hashlib.sha256(component_urls[0].encode("utf-8")).hexdigest()[:24]
            identity_basis = "machine_canonical_url"
        else:
            source_ids = sorted(str(rows[index].get("source_id", "")) for index in indexes)
            page_id = "PAGE-LEGACY-" + hashlib.sha256("|".join(source_ids).encode("utf-8")).hexdigest()[:24]
            identity_basis = "machine_legacy_source_identity"
        component_reason_set = {
            reason
            for index in indexes
            for reason in merge_reasons[index]
        } or {"canonical_url" if component_urls else "legacy_source_identity"}
        for index in indexes:
            row = rows[index]
            source_id = str(row.get("source_id", ""))
            platform = platform_identity(row.get("url", ""), row.get("final_url", ""))
            # Keep a conflicting cache visible to the shared hard-error check.
            # Formal aggregation independently derives the authoritative ID.
            row["platform_id"] = row.get("platform_id") or platform["platform_id"]
            row["page_entity_id"] = page_id
            row["page_entity_identity_basis"] = identity_basis
            row["original_hostname"] = platform["original_hostname"]
            row["platform_resolution_basis"] = platform["resolution_basis"]
            row["platform_mapping_rule_source"] = platform["mapping_rule_source"]
            groups[page_id].append(source_id)
            reasons[page_id].update(component_reason_set)
    return rows, {
        "page_entity_count": len(groups),
        "groups": [
            {
                "page_entity_id": page_id,
                "source_ids": sorted(item for item in source_ids if item),
                "merge_reasons": sorted(reasons[page_id]),
            }
            for page_id, source_ids in sorted(groups.items())
        ],
    }
