"""Typed external data boundaries. No network access or executable text."""
from __future__ import annotations
import hashlib
import ipaddress
import math
import re
from functools import lru_cache
from urllib.parse import ParseResult, unquote_to_bytes, urlparse, urljoin

MAX_URL_LENGTH = 8192
MAX_REDIRECT_HOPS = 20
SENSITIVE_QUERY_KEYS = frozenset({"token", "xsec_token", "auth", "authorization",
    "session", "cookie", "code", "password", "access_token", "api_key"})
NATIVE_FIELDS = ("native_rating_value", "native_rating_scale_min", "native_rating_scale_max")
CONTACT_PATTERNS = (
    ("email", re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)),
    ("mobile", re.compile(r"(?<![A-Z0-9])(?:\+?86[ -]?)?1[3-9]\d{9}(?![A-Z0-9])", re.I)),
    ("contact", re.compile(r"(?:电话|手机|联系方式|联系电话|phone|tel)"
        r"\s*[:：=]?\s*(\+?[0-9][0-9(). -]{4,29}[0-9])", re.I)),
    ("account", re.compile(r"(?:微信|微\s*信|QQ|weixin|wechat|账号|账户|account)"
        r"(?:\s*[:：=]\s*|\s+)([A-Z0-9_][A-Z0-9_.-]{4,39})", re.I)),
    ("profile", re.compile(r"https?://[^\s/]+/(?:users?|profile|people|members?)/[^\s<>，。；]+", re.I)),
)
URL_TOKEN = re.compile(r"https?://[^\s<>，。；]+", re.I)
CONTACT_QUERY_KEYS = frozenset({
    "phone", "mobile", "telephone", "tel", "contact", "contact_phone",
    "联系电话", "手机", "电话", "联系方式",
})
CONTENT_ID_KEYS = frozenset({
    "id", "item", "item_id", "article", "article_id", "post", "post_id",
    "page", "page_id", "record", "record_id", "news", "news_id", "detail",
})
CONTENT_ID_PATH_MARKERS = frozenset({
    "id", "item", "article", "post", "page", "record", "news", "detail",
    "entry", "document", "content",
})
PROFILE_PATH_MARKERS = frozenset({"user", "users", "profile", "people", "member", "members"})


class InputError(ValueError):
    def __init__(self, code, *, field="input", value=None, row=None):
        try:
            representation = str(value)
        except (ValueError, OverflowError, RecursionError):
            representation = "unrepresentable"
        self.detail = {"code": code, "field": field, "row": row,
                       "summary": "sha256:" + hashlib.sha256((type(value).__name__ + ":" + representation).encode('utf-8', 'backslashreplace')).hexdigest()[:16]}
        super().__init__(f"{code}:{field}:{self.detail['summary']}")


def finite_number(value, *, field="number"):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise InputError("finite_number_required", field=field, value=value)
    if isinstance(value, str) and (len(value) > 128 or not re.fullmatch(
            r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip())):
        raise InputError("finite_number_required", field=field, value=value)
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise InputError("finite_number_required", field=field, value=value) from exc
    if not math.isfinite(result):
        raise InputError("finite_number_required", field=field, value=value)
    return result


def native_numbers(row):
    values = {key: finite_number(row.get(key), field=key) for key in NATIVE_FIELDS}
    value, minimum, maximum = (values[key] for key in NATIVE_FIELDS)
    if maximum <= minimum or not minimum <= value <= maximum or not math.isfinite(maximum-minimum):
        raise InputError("invalid_native_scale", field="native_rating")
    return values


def normalize_host(host):
    """One IDNA 2008/UTS-46 non-transitional host identity, never casefold."""
    value = host.rstrip(".")
    # Memoize only bounded immutable input conversion, never network facts,
    # files, trust decisions or admission results. Keep non-string behavior.
    if type(value) is str and len(value) <= 253:
        return _cached_host_identity(value)
    return _host_identity(value)


def _host_identity(value):
    import idna
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return idna.encode(value, uts46=True, transitional=False, std3_rules=True).decode("ascii")


_cached_host_identity = lru_cache(maxsize=1024)(_host_identity)


def parse_public_url(value, *, field="url", row=None):
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise InputError("invalid_url_type_or_length", field=field, value=value, row=row)
    try:
        if (any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value)
                or re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f)', value, re.I)):
            raise ValueError()
        p = urlparse(value)
        if (p.scheme.lower() not in {"http", "https"} or p.username is not None
                or p.password is not None or p.netloc.endswith(":")):
            raise ValueError()
        host = p.hostname or ""
        if not host or host.startswith(".") or host.endswith("..") or "%" in host:
            raise ValueError()
        host = normalize_host(host)
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or "." not in host or re.fullmatch(r"[0-9.]+", host)
                    or host.split(".")[-1] in {"localhost", "local", "invalid", "test", "example"}
                    or any(label.startswith("0x") or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split("."))):
                raise ValueError()
        else:
            from public_network import global_address
            global_address(host)
        return p, host
    except (ValueError, UnicodeError, OverflowError) as exc:
        raise InputError("source_url_host_invalid", field=field, value=value, row=row) from exc


def resolve_public_redirect(base, location):
    """Resolve one observed Location without granting page equivalence."""
    parse_public_url(base)
    if (not isinstance(location, str) or not location or len(location) > MAX_URL_LENGTH
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in location)
            or "\\" in location):
        raise InputError("invalid_redirect_location", field="location", value=location)
    resolved = urljoin(base, location)
    parse_public_url(resolved, field="location")
    return resolved


def pinned_transport_url(url, address):
    """Replace only the connection authority, not the public evidence identity."""
    from public_network import global_address
    parsed, host = parse_public_url(url)
    address = global_address(address)
    authority = '[' + address + ']' if ':' in address else address
    original = '[' + host + ']' if ':' in host else host
    if parsed.port is not None:
        authority += ':' + str(parsed.port)
        original += ':' + str(parsed.port)
    return parsed._replace(netloc=authority).geturl(), original


def normalized_transport_url(url):
    """Use the same ASCII host as DNS pinning; retain path and query octets."""
    parsed, host = parse_public_url(url)
    authority = '[' + host + ']' if ':' in host else host
    if parsed.port is not None:
        authority += ':' + str(parsed.port)
    return parsed._replace(netloc=authority).geturl()


def safe_urlparse(value):
    """Total parser for reporting/error collection, never grants admission."""
    try:
        return parse_public_url(value)[0]
    except InputError:
        return ParseResult("", "", "", "", "", "")


def raw_query_parts(query):
    """Ordered raw components; never decode values or erase empty delimiters."""
    return [part.partition('=') for part in query.split('&')] if query else []


def query_key(raw):
    """Decode a parameter name once, losslessly; unknown bytes remain raw."""
    try:
        return unquote_to_bytes(raw).decode('utf-8', errors='strict').casefold()
    except UnicodeError:
        return raw.casefold()


def query_parameter_names(query):
    return [query_key(key) for key, _equals, _value in raw_query_parts(query)]


def privacy_url_component(raw):
    """One-pass display-only decoding. Escaped unknown bytes retain provenance.

    Never use this view for request identity, hashing, or page equivalence.
    Plus signs are literal; this is not HTML form decoding.
    """
    return unquote_to_bytes(raw).decode('utf-8', errors='backslashreplace')


def _raw_contact_spans(text):
    if not isinstance(text, str):
        raise InputError("text_must_be_string", field="text")
    spans = []
    for kind, pattern in CONTACT_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(1) if kind in {"contact", "account"} else match.span()
            spans.append((start, end, kind))
    return sorted(set(spans))


def url_privacy_findings(value, *, field="url"):
    """Classify identifiers in a URL without changing its signed identity."""
    parsed, _ = parse_public_url(value, field=field)
    findings = []
    for raw_key, _equals, raw_item in raw_query_parts(parsed.query):
        key, item = privacy_url_component(raw_key), privacy_url_component(raw_item)
        normalized_key = key.strip().casefold()
        key_kinds = {kind for _start, _end, kind in _raw_contact_spans(key)}
        if key_kinds & {'email', 'account', 'profile', 'contact'}:
            findings.append({'status':'confirmed', 'location':'query_name', 'field':'parameter_name'})
        raw_matches = _raw_contact_spans(item)
        if normalized_key in CONTACT_QUERY_KEYS and item:
            findings.append({"status": "confirmed", "location": "query", "field": key})
        elif raw_matches:
            # A complete e-mail/account/profile value is an identifier even
            # under a generic query key.  Bare phone-like digit sequences stay
            # suspected unless surrounding URL context identifies a contact
            # field, which avoids treating ordinary numeric content IDs as
            # telephone numbers.
            kinds = {kind for _start, _end, kind in raw_matches}
            if normalized_key in CONTENT_ID_KEYS and item.isascii() and item.isdigit() and kinds <= {"mobile"}:
                continue
            status = "confirmed" if kinds & {"email", "account", "profile", "contact"} else "suspected"
            findings.append({"status": status, "location": "query", "field": key})
    # Include semicolon parameters and the fragment in the privacy view only.
    segments = [privacy_url_component(part) for part in (parsed.path + (';' + parsed.params if parsed.params else '')).split("/") if part]
    for index, segment in enumerate(segments):
        previous = segments[index - 1].strip().casefold() if index else ""
        if previous in PROFILE_PATH_MARKERS:
            findings.append({"status": "confirmed", "location": "path", "field": previous})
            continue
        kinds = {kind for _start, _end, kind in _raw_contact_spans(segment)}
        if not kinds:
            continue
        if previous in CONTENT_ID_PATH_MARKERS and segment.isascii() and segment.isdigit() and kinds <= {"mobile"}:
            continue
        if previous in CONTACT_QUERY_KEYS or kinds & {"email", "account", "profile", "contact"}:
            findings.append({"status": "confirmed", "location": "path", "field": previous})
        else:
            findings.append({"status": "suspected", "location": "path", "field": previous})
    if _raw_contact_spans(privacy_url_component(parsed.fragment)):
        findings.append({"status": "confirmed", "location": "fragment", "field": "fragment"})
    return findings


def url_privacy_errors(value, *, field="url"):
    return [
        InputError("url_contains_personal_identifier", field=field, value=value)
        for finding in url_privacy_findings(value, field=field)
        if finding["status"] == "confirmed"
    ]


def contact_spans(text):
    """Return contacts in prose, excluding a confirmed content-ID URL case."""
    spans = _raw_contact_spans(text)
    urls = [(match.start(), match.end(), match.group()) for match in URL_TOKEN.finditer(text)]
    result = []
    confirmed_urls = set()
    for left, right, url in urls:
        try:
            if any(f['status'] == 'confirmed' for f in url_privacy_findings(url)):
                confirmed_urls.add(url)
                # Encoded identifiers may have no raw regex match. Mask the
                # entire display URL, preserving offsets and the raw source.
                result.append((left, right, 'url_identifier'))
        except InputError:
            confirmed_urls.add(url)
    for item in spans:
        start, end, _kind = item
        containing = next((url for left, right, url in urls if left <= start and end <= right), None)
        if containing is None:
            result.append(item)
            continue
        if containing in confirmed_urls:
            result.append(item)
    return result


def redact_text(text):
    """Length-preserving removal keeps historical language and offsets intact."""
    if not isinstance(text, str):
        raise InputError("text_must_be_string", field="text")
    result = list(text)
    for start, end, _ in contact_spans(text):
        result[start:end] = "＊" * (end-start)
    return "".join(result)


def privacy_errors(row, *, label="record"):
    text_fields = ("page_title", "notes", "capture_notes", "original_summary_text",
        "original_visible_text", "excerpt_or_summary", "semantic_unit_text", "researcher_summary")
    return [f"{label}:{field}:personal_contact_not_minimized" for field in text_fields
            if isinstance(row.get(field), str) and contact_spans(row[field])]


def redaction_rule_sha256():
    from pathlib import Path
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def comparison_text(text):
    """Exclude redaction placeholders from similarity, never from stored text."""
    # Otherwise a common masked contact footer becomes manufactured evidence
    # of copying. Exact substantive duplicates still have identical views.
    text = re.sub(r"(?:(?:电话|手机|联系方式|联系电话|邮箱|电子邮箱|微信|QQ|"
                  r"phone|tel|email|wechat|account)\s*[:：=]?\s*)?＊+", "", text, flags=re.I)
    return text.rstrip(" ，。；,.;：: \t\r\n")


def assert_public_text(value, *, field="output"):
    """Reject residual contact data before serializing a research deliverable."""
    if isinstance(value, str):
        if contact_spans(value):
            raise InputError("personal_contact_not_minimized", field=field)
    elif isinstance(value, dict):
        for key, item in value.items():
            # This is a typed machine sequence, not a contact-text field.
            # Numeric shape must still be valid; narrative fields are never
            # exempt merely because their contents resemble an identifier.
            if key == "event_sequence" and (type(item) is int or isinstance(item, str) and item.isascii() and item.isdigit()):
                finite_number(item, field=key)
            elif isinstance(item, str) and (key in {"url", "request_url", "final_url", "discovery_url",
                    "declared_canonical_url", "source_identity_url", "canonical_url"}
                    or key.endswith("_url")):
                errors = url_privacy_errors(item, field=key) if item else []
                if errors:
                    raise errors[0]
            else:
                assert_public_text(item, field=str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_public_text(item, field=field)
    elif isinstance(value, float):
        finite_number(value)


def office_privacy_errors(path):
    """Inspect actual OOXML text and external targets, never raw response files."""
    import zipfile
    import xml.etree.ElementTree as ET
    errors = []
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if not name.endswith((".xml", ".rels")):
                continue
            root = ET.fromstring(package.read(name))
            segments = [n.text or '' for n in root.iter() if n.tag.rsplit('}', 1)[-1] == 't']
            segments += [child.text or '' for node in root.iter()
                         if node.tag.rsplit('}',1)[-1] == 'c' and node.attrib.get('t') == 'str'
                         for child in node if child.tag.rsplit('}',1)[-1] == 'v']
            segments += [''.join(t.text or '' for t in node.iter() if t.tag.rsplit('}',1)[-1] == 't')
                         for node in root.iter() if node.tag.rsplit('}',1)[-1] in {'p','si'}]
            if any(contact_spans(text) for text in segments):
                errors.append('personal_contact_not_minimized:' + name)
            for node in root.iter():
                if node.attrib.get('TargetMode') == 'External':
                    url = node.attrib.get('Target', '')
                    try:
                        parse_public_url(url, field='office_relationship')
                        privacy = url_privacy_errors(url, field='office_relationship')
                        if privacy:
                            raise privacy[0]
                    except InputError as exc:
                        errors.append(str(exc))
    return errors
