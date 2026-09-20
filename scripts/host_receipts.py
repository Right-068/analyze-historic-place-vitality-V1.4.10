"""Validate ordinary tool traces and optional host-signed page receipts.

An empty trust store is intentional: self-reported telemetry is not an
attestation. The publisher must obtain issuer keys from the actual host
administrator outside a research run. Hashes alone never enroll an issuer.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import strict_json as json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from collections.abc import Mapping
from input_safety import InputError, parse_public_url, MAX_REDIRECT_HOPS, redact_text, redaction_rule_sha256

TRUST_STORE = Path(__file__).resolve().parents[1] / "assets" / "host-receipt-trust.json"
SCHEMA = "host-page-receipt-1"
FIELDS = (
    "source_capture_id",
    "network_proof_level", "host_receipt", "host_receipt_sha256", "host_trust_sha256",
    "raw_response_sha256", "raw_visible_body_sha256", "visible_body_sha256",
    "extraction_rule_sha256", "redaction_rule_sha256", "body_offset_map_sha256",
    "body_format", "body_provenance", "visibility_proof_level",
)
PAYLOAD_FIELDS = {
    "schema_version", "issuer_id", "tool_call_id", "tool_type", "task_run_id", "plan_id",
    "query_id", "execution_id", "source_capture_id", "request_url", "redirect_chain",
    "final_url", "request_started_at", "response_finished_at", "response_status",
    "response_body_sha256", "page_title_sha256", "body_format",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def extraction_rule_sha256():
    return digest(Path(__file__).read_bytes())


def _roots():
    value = json.loads(TRUST_STORE.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "host-receipt-trust-1" or not isinstance(value.get("issuers"), list):
        raise InputError("invalid_host_trust_store")
    roots = {}
    for issuer in value["issuers"]:
        if (not isinstance(issuer, dict) or set(issuer) != {"issuer_id", "ed25519_public_key"}
                or not isinstance(issuer["issuer_id"], str) or not issuer["issuer_id"]
                or issuer["issuer_id"] in roots):
            raise InputError("invalid_host_trust_store")
        try:
            key = bytes.fromhex(issuer["ed25519_public_key"])
        except (ValueError, TypeError) as exc:
            raise InputError("invalid_host_trust_store") from exc
        if len(key) != 32:
            raise InputError("invalid_host_trust_store")
        roots[issuer["issuer_id"]] = key
    return roots


def capability(*, network_search_available=None, page_read_available=None,
               tool_result_text_available=None, tool_result_reference_available=None,
               tool_call_id_available=None, trace_persistence_available=None,
               sample_record=None, response_text=None):
    """Probe optional attestation separately from observed host tool capability.

    None means the caller has not yet tested its network tools, not unavailable.
    This local verifier cannot discover an application's tools by itself.
    """
    observations = dict(network_search_available=network_search_available,
        page_read_available=page_read_available, tool_result_text_available=tool_result_text_available,
        tool_result_reference_available=tool_result_reference_available,
        tool_call_id_available=tool_call_id_available, trace_persistence_available=trace_persistence_available)
    for observed in observations.values():
        if observed is not None and type(observed) is not bool:
            raise InputError("capability_observation_must_be_boolean")
    try:
        roots = _roots()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        available = bool(roots)
    except (OSError, ValueError, ImportError):
        roots, available = {}, False
    probe_error = ''
    minimum_trace_valid = None
    if sample_record is not None:
        try:
            from tool_response_artifacts import register_response, read_registered
            from tempfile import TemporaryDirectory
            if not isinstance(response_text, str) or not response_text.strip():
                raise InputError('tool_response_text_required')
            # Exercise the real registration/readback in a disposable private probe.
            # This neither captures a formal source nor claims network execution.
            with TemporaryDirectory(prefix='page-trace-probe-') as folder:
                binding = register_response(artifact_root=folder, record=sample_record, response_text=response_text)
                read_registered(artifact_root=folder, record={**sample_record, **binding})
            minimum_trace_valid = True
            observations.update(tool_result_text_available=True, trace_persistence_available=True,
                tool_result_reference_available=bool(sample_record['tool_trace'].get('result_ref')),
                tool_call_id_available=bool(sample_record['tool_trace'].get('tool_call_id')))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            minimum_trace_valid, probe_error = False, str(exc)
    required = (network_search_available, page_read_available,
                observations['tool_result_text_available'], observations['trace_persistence_available'], minimum_trace_valid)
    traceable = False if False in required else True if all(x is True for x in required) else None
    status = ('tool_probe_required' if traceable is None else 'ready' if traceable else
              'network_unavailable' if False in (network_search_available, page_read_available) else 'trace_unavailable')
    return {"status": status,
            "issuer_count": len(roots), "host_attestation_available": available,
            **observations, 'minimum_trace_valid': minimum_trace_valid, 'probe_error': probe_error,
            "tool_traceable_available": traceable, "formal_research_available": traceable,
            "formal_network_capture_available": traceable,
            "provenance_level": "tool_traceable" if traceable else "unverified_context",
            "note": "Search/read alone do not prove trace readiness. Probe the actual minimal metadata and retained response; host call/reference IDs and attestation are optional. This local check does not perform network access."}


def verify_receipt(envelope):
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature_ed25519"}:
        raise InputError("host_receipt_required")
    p = envelope["payload"]
    if not isinstance(p, dict) or set(p) != PAYLOAD_FIELDS or p.get("schema_version") != SCHEMA:
        raise InputError("invalid_host_receipt_schema")
    for field in ("issuer_id", "tool_call_id", "task_run_id", "plan_id", "query_id",
                  "execution_id", "source_capture_id"):
        if not isinstance(p.get(field), str) or not p[field].strip():
            raise InputError("host_receipt_identity_missing", field=field)
    key = _roots().get(p["issuer_id"])
    if key is None:
        raise InputError("untrusted_host_issuer")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        signature = bytes.fromhex(envelope["signature_ed25519"])
        Ed25519PublicKey.from_public_bytes(key).verify(signature, canonical_bytes(p))
    except Exception as exc:
        raise InputError("host_receipt_signature_invalid") from exc
    if not isinstance(p["tool_type"], str) or p["tool_type"] not in {"page_fetch", "browser_page_response"}:
        raise InputError("search_call_is_not_page_capture")
    from temporal_fields import timestamp
    from source_identity import redirect_node_identity, strict_url_identity
    from public_network import global_address
    start, finish = timestamp(p["request_started_at"]), timestamp(p["response_finished_at"])
    if start > finish or finish > datetime.now(timezone.utc):
        raise InputError("invalid_host_response_time")
    for key in ("response_body_sha256", "page_title_sha256"):
        if not isinstance(p[key], str) or len(p[key]) != 64 or any(c not in "0123456789abcdef" for c in p[key]):
            raise InputError("invalid_host_body_hash", field=key)
    if not isinstance(p["body_format"], str) or p["body_format"] not in {"text/plain", "text/html", "application/json"}:
        raise InputError("unsupported_host_body_format")
    hops = p["redirect_chain"]
    if not isinstance(hops, list) or not 1 <= len(hops) <= MAX_REDIRECT_HOPS:
        raise InputError("invalid_host_redirect_chain")
    seen, previous = set(), start
    for i, hop in enumerate(hops):
        if not isinstance(hop, dict) or set(hop) != {
                "url", "status_code", "observed_at", "resolved_addresses", "connected_address"}:
            raise InputError("invalid_host_redirect_node", row=i)
        parse_public_url(hop["url"], field="redirect_chain.url", row=i)
        normalized = redirect_node_identity(hop["url"])
        if normalized in seen:
            raise InputError("redirect_cycle", row=i)
        seen.add(normalized)
        status = hop["status_code"]
        if type(status) is not int or (i < len(hops)-1 and status not in {301, 302, 303, 307, 308}):
            raise InputError("redirect_status_chain_invalid", row=i)
        when = timestamp(hop["observed_at"])
        if not previous <= when <= finish:
            raise InputError("host_redirect_time_outside_response", row=i)
        previous = when
        addresses = hop["resolved_addresses"]
        if not isinstance(addresses, list) or not addresses:
            raise InputError("host_resolution_missing", row=i)
        public = {global_address(value) for value in addresses}
        if global_address(hop["connected_address"]) not in public:
            raise InputError("host_connection_resolution_mismatch", row=i)
    if (type(p["response_status"]) is not int or not 200 <= p["response_status"] <= 299
            or hops[-1]["status_code"] != p["response_status"]):
        raise InputError("host_response_not_readable")
    if (strict_url_identity(p["request_url"]) != strict_url_identity(hops[0]["url"])
            or strict_url_identity(p["final_url"]) != strict_url_identity(hops[-1]["url"])):
        raise InputError("host_redirect_identity_mismatch")
    parse_public_url(p["request_url"], field="request_url")
    parse_public_url(p["final_url"], field="final_url")
    return envelope


def network_from_receipt(envelope):
    p = verify_receipt(envelope)["payload"]
    return {"schema_version": "public-network-observation-1", "tool_call_id": p["tool_call_id"],
            "hops": [{k: hop[k] for k in ("url", "resolved_addresses", "connected_address", "observed_at")}
                     for hop in p["redirect_chain"]]}


class _VisibleHTML(HTMLParser):
    PARAGRAPH_TAGS = frozenset({'p','div','section','article','li','tr','h1','h2','h3','h4','h5','h6','br','hr'})
    NON_CONTENT_TAGS = frozenset({'script', 'style', 'noscript', 'template', 'head'})
    VOID_TAGS = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img',
                          'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'})
    def __init__(self, raw):
        super().__init__(convert_charrefs=False)
        self.raw, self.parts, self.offsets = raw, [], []
        self.lines = [0]
        for i, c in enumerate(raw):
            if c == "\n":
                self.lines.append(i+1)
        self.stack = []
        self.visibility_uncertainties = set()
        self.uncertain_offsets = []

    @property
    def hidden(self):
        return bool(self.stack and (self.stack[-1][1] or self.stack[-1][2]))

    @staticmethod
    def _style_properties(style):
        # Resolve only this declaration list; external cascade is not rendered.
        import re
        escape = re.compile(r'\\([0-9a-fA-F]{1,6}[ \t\r\n\f]?|[^\r\n\f])')
        def unescape(match):
            value = match.group(1)
            if re.fullmatch(r'[0-9a-fA-F]{1,6}[ \t\r\n\f]?', value):
                codepoint = int(value.strip(), 16)
                return chr(codepoint) if 0 < codepoint <= 0x10ffff else '\ufffd'
            return value
        # Scan before decoding escapes. Comment delimiters in strings are data;
        # escaped quotes/semicolons cannot alter declaration boundaries.
        declarations, current, quote, depth, cursor = [], [], '', 0, 0
        while cursor < len(style):
            char = style[cursor]
            if char == '\\':
                match = escape.match(style, cursor)
                end = match.end() if match else min(len(style), cursor + 2)
                current.append(style[cursor:end])
                cursor = end
                continue
            if quote:
                current.append(char)
                # A literal newline ends a CSS bad-string token. Do not let
                # malformed preceding text conceal a later hiding declaration.
                if char == quote or char in '\r\n\f':
                    quote = ''
            elif style.startswith('/*', cursor):
                end = style.find('*/', cursor + 2)
                if end < 0:
                    break
                cursor = end + 2
                continue
            elif char in ('"', "'"):
                quote = char
                current.append(char)
            elif char == '(':
                depth += 1
                current.append(char)
            elif char == ')':
                depth = max(0, depth - 1)
                current.append(char)
            elif char == ';' and depth == 0:
                declarations.append(''.join(current))
                current = []
            else:
                current.append(char)
            cursor += 1
        declarations.append(''.join(current))
        properties = {}
        for declaration in declarations:
            name, sep, value = declaration.partition(':')
            if sep:
                name = escape.sub(unescape, name)
                value = escape.sub(unescape, value)
                important = bool(re.search(r'!\s*important\s*$', value, flags=re.I))
                value = re.sub(r'\s*!\s*important\s*$', '', value.strip(), flags=re.I).strip().lower()
                name = name.strip().lower()
                accepted = {'display': {'none', 'block', 'inline', 'inline-block', 'flex', 'grid', 'contents'},
                            'visibility': {'visible', 'hidden', 'collapse'},
                            'content-visibility': {'visible', 'hidden', 'auto'}}
                if name in accepted and value not in accepted[name]:
                    # Unknown expressions require rendering; invalid later values
                    # must not erase a preceding valid hiding declaration.
                    properties['uncertain'] = ('true', False)
                    continue
                if name not in properties or important or not properties[name][1]:
                    properties[name] = (value, important)
        return {name: value for name, (value, _) in properties.items()}

    @classmethod
    def _hidden_style(cls, style):
        values = cls._style_properties(style)
        try:
            transparent = float(values.get('opacity', '1').rstrip('%')) == 0
        except ValueError:
            transparent = False
        return (values.get('display') == 'none' or values.get('visibility') in {'hidden', 'collapse'}
                or values.get('content-visibility') == 'hidden' or transparent)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == 'style' or tag == 'link' and 'stylesheet' in (attributes.get('rel') or '').lower().split():
            self.visibility_uncertainties.add('stylesheet_requires_rendered_read')
        if tag == 'script' and (attributes.get('type') or '').lower() not in {'application/json', 'application/ld+json'}:
            self.visibility_uncertainties.add('dynamic_document_requires_rendered_read')
        if any(name in {'class', 'id'} or name.startswith('on') for name, _ in attrs):
            self.visibility_uncertainties.add('selector_or_event_requires_rendered_read')
        properties = self._style_properties(attributes.get('style') or '')
        safe_styles = {'display', 'visibility', 'content-visibility', 'opacity'}
        if (set(properties) - safe_styles or properties.get('content-visibility') == 'auto'
                or any('var(' in value or 'calc(' in value for value in properties.values())):
            self.visibility_uncertainties.add('style_requires_rendered_read')
        if tag in {'iframe', 'object', 'embed', 'canvas', 'svg'} or '-' in tag:
            self.visibility_uncertainties.add('nonstatic_content_requires_rendered_read')
        inherited = self.hidden
        if (tag == 'summary' and self.stack and self.stack[-1][0] == 'details'
                and self.stack[-1][2] and not self.stack[-1][3]):
            inherited = self.stack[-1][1]
            self.stack[-1][3] = True
        excluded = (tag in self.NON_CONTENT_TAGS
                    or any(name == 'hidden' for name, _ in attrs)
                    or tag == 'dialog' and 'open' not in attributes
                    or any(name == 'style' and self._hidden_style(value or '') for name, value in attrs))
        if tag in self.PARAGRAPH_TAGS or excluded:
            self._separator()
        if tag not in self.VOID_TAGS:
            uncertain = 'popover' in attributes or bool(self.stack and self.stack[-1][4])
            self.stack.append([tag, inherited or excluded, tag == 'details' and 'open' not in attributes, False, uncertain])

    def handle_startendtag(self, tag, attrs):
        # In text/html a slash does not close a non-void element.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.VOID_TAGS:
            return
        index = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i][0] == tag), None)
        was_hidden = self.hidden
        if index is not None:
            # HTMLParser does not implement browser tree repair. Never pop an
            # unclosed hidden subtree by guessing how a malformed ancestor ends.
            if any(frame[1] for frame in self.stack[index+1:]) and not self.stack[index][1]:
                raise ValueError('html_visibility_structure_ambiguous')
            del self.stack[index:]
        if tag in self.PARAGRAPH_TAGS or was_hidden and not self.hidden:
            self._separator()

    def _separator(self):
        # Do not manufacture a new lexical match by joining different block
        # elements. Separator offsets point to the responsible markup token.
        if not self.hidden and self.parts and not self.parts[-1].endswith("\n"):
            self._add("\n", raw_length=0)

    def _add(self, text, raw_length=None):
        if not self.hidden:
            line, col = self.getpos()
            start = self.lines[line-1]+col
            self.parts.append(text)
            self.offsets.extend([start+i if raw_length is None else start for i in range(len(text))])
            self.uncertain_offsets.extend([bool(self.stack and self.stack[-1][4])] * len(text))

    def handle_data(self, data):
        self._add(data)

    def handle_entityref(self, name):
        from html import unescape
        self._add(unescape("&"+name+";"), len(name)+2)

    def handle_charref(self, name):
        from html import unescape
        self._add(unescape("&#"+name+";"), len(name)+3)


def extract_body(raw, body_format):
    text, offsets, _ = analyze_body(raw, body_format)
    return text, offsets


def analyze_body(raw, body_format):
    """Derive a bounded visibility claim, never a browser-rendering claim for HTML."""
    text = raw.decode("utf-8")
    if body_format == "text/html":
        parser = _VisibleHTML(text)
        parser.feed(text)
        parser.close()
        ranges = []
        for index, uncertain in enumerate(parser.uncertain_offsets):
            if uncertain:
                if ranges and ranges[-1][1] == index:
                    ranges[-1][1] = index + 1
                else:
                    ranges.append([index, index + 1])
        return "".join(parser.parts), parser.offsets, {
            'body_format': body_format, 'body_provenance': 'static_html_candidate',
            'visibility_proof_level': ('unconfirmed' if parser.visibility_uncertainties
                else 'block_scoped' if ranges else 'static_explicit'),
            'visibility_unconfirmed_ranges': ranges,
        }
    return text, list(range(len(text))), {
        'body_format': body_format,
        'body_provenance': 'native_numeric_result' if body_format == 'application/json' else 'rendered_visible_text',
        'visibility_proof_level': 'tool_result',
        'visibility_unconfirmed_ranges': [],
    }


def visibility_errors(row):
    """Shared qualification check; uncertain captures remain useful staging material."""
    kind, origin, level = (row.get(k) for k in ('body_format', 'body_provenance', 'visibility_proof_level'))
    allowed = {('text/html', 'static_html_candidate', 'static_explicit'),
               ('text/html', 'static_html_candidate', 'block_scoped'),
               ('text/html', 'static_html_candidate', 'unconfirmed'),
               ('text/plain', 'rendered_visible_text', 'tool_result'),
               ('application/json', 'native_numeric_result', 'tool_result')}
    if (kind, origin, level) not in allowed:
        return ['body_visibility_contract_invalid']
    if level == 'unconfirmed' and any(str(row.get(k, '')).lower() == 'true'
                                     for k in ('used_for_scoring', 'is_user_source', 'source_block_is_user_generated')):
        return ['body_visibility_unconfirmed']
    return []


def bind_tool_capture(record, *, capture_id, body, artifact_root=None):
    from public_network import normalize_tool_trace, TOOL_SCHEMA
    trace = normalize_tool_trace(record.get("tool_trace"), url=record.get("url"),
        final_url=record.get("final_url") or record.get("url"), retrieved_at=record.get("retrieved_at"))
    if trace["page_title"] != str(record.get("page_title", "")).strip():
        raise InputError("tool_capture_title_mismatch")
    if not record.get("raw_response_file"):
        raise InputError("tool_response_artifact_required")
    from tool_response_artifacts import read_registered
    raw, binding = read_registered(artifact_root=artifact_root, record=record)
    visible, offsets, visibility = analyze_body(raw, trace["body_format"])
    native = record.get("native_numeric_observation")
    if native:
        from input_safety import native_numbers
        if trace["body_format"] != "application/json" or native_numbers(json.loads(visible)) != native_numbers(native):
            raise InputError("tool_native_numeric_mismatch")
        visible, offsets = "", []
    if "visible_body" in record and body != visible:
        raise InputError("tool_visible_body_mismatch")
    observation = {**trace, "schema_version": TOOL_SCHEMA,
        **{key: record.get(key) for key in ("task_run_id", "plan_id", "query_id", "execution_id")},
        "source_capture_id": capture_id, "response_body_sha256": digest(raw),
        "page_title_sha256": digest(redact_text(trace["page_title"]).encode()),
        "tool_artifact": canonical_bytes(binding).decode()}
    normalize_tool_trace(observation, url=record["url"], final_url=trace["final_url"],
                         retrieved_at=trace["retrieved_at"], bound=True)
    # Keep original tool title privately; public metadata follows the same redaction.
    observation["page_title"] = redact_text(trace["page_title"])
    return {"network_proof_level": "tool_traceable", "host_receipt": "",
            "host_receipt_sha256": "", "host_trust_sha256": "",
            "network_observation": observation, **visibility}, raw, visible, offsets


def bind_capture(record, *, capture_id, body, artifact_root=None):
    """Read receipt/response supplied by the host; do not sign or infer facts."""
    from source_identity import strict_url_identity
    from temporal_fields import timestamp
    envelope = record.get("host_receipt")
    if record.get("host_receipt_file"):
        if envelope:
            raise InputError("duplicate_host_receipt_inputs")
        envelope = json.loads(Path(record["host_receipt_file"]).read_text(encoding="utf-8"))
    if ("host_receipt" in record and record["host_receipt"] != "") or record.get("host_receipt_file"):
        envelope = verify_receipt(envelope)
    if not envelope and record.get("tool_trace") is not None:
        return bind_tool_capture(record, capture_id=capture_id, body=body, artifact_root=artifact_root)
    if not envelope:
        return {"network_proof_level": "self_reported", "host_receipt": "",
                "host_receipt_sha256": "", "host_trust_sha256": "",
                **analyze_body(body.encode(), 'text/plain')[2]}, body.encode(), body, list(range(len(body)))
    envelope = verify_receipt(envelope)
    p = envelope["payload"]
    for key in ("task_run_id", "plan_id", "query_id", "execution_id"):
        if p[key] != record.get(key):
            raise InputError("host_receipt_binding_mismatch", field=key)
    if p["source_capture_id"] != capture_id:
        raise InputError("host_receipt_binding_mismatch", field="source_capture_id")
    if (strict_url_identity(record.get("url")) != strict_url_identity(p["request_url"])
            or strict_url_identity(record.get("final_url") or record.get("url")) != strict_url_identity(p["final_url"])):
        raise InputError("host_receipt_url_mismatch")
    if timestamp(record["retrieved_at"]) != timestamp(p["response_finished_at"]):
        raise InputError("host_capture_time_mismatch")
    if digest(str(record.get("page_title", "")).strip().encode()) != p["page_title_sha256"]:
        raise InputError("host_capture_title_mismatch")
    if not record.get("raw_response_file"):
        raise InputError("host_raw_response_artifact_required")
    raw = Path(record["raw_response_file"]).read_bytes()
    if digest(raw) != p["response_body_sha256"]:
        raise InputError("host_raw_response_hash_mismatch")
    visible, offsets, visibility = analyze_body(raw, p["body_format"])
    native = record.get("native_numeric_observation")
    if native and p["body_format"] != "application/json":
        raise InputError("native_numeric_requires_structured_host_response")
    if p["body_format"] == "application/json" and native:
        from input_safety import native_numbers
        if native_numbers(json.loads(visible)) != native_numbers(native):
            raise InputError("host_native_numeric_mismatch")
        visible, offsets = "", []
    if body and body != visible:
        raise InputError("host_visible_body_mismatch")
    return {"network_proof_level": "host_verified", "host_receipt": envelope,
            "host_receipt_sha256": digest(canonical_bytes(envelope)),
            "host_trust_sha256": digest(TRUST_STORE.read_bytes()), **visibility}, raw, visible, offsets


def proof_errors(row, *, label="source"):
    """Reverify the signature and every binding; cached 'verified' is never truth."""
    try:
        issues = visibility_errors(row)
        if issues:
            raise InputError(issues[0])
        if row.get("network_proof_level") == "tool_traceable":
            from public_network import normalize_observation, network_errors
            if any(row.get(key, "") != "" for key in ("host_receipt", "host_receipt_sha256", "host_trust_sha256")):
                raise InputError("attestation_cannot_downgrade")
            p = normalize_observation(row.get("network_observation"), url=row.get("url"),
                final_url=row.get("final_url"), retrieved_at=row.get("retrieved_at"))
            from public_network import TOOL_SCHEMA
            if p.get("schema_version") != TOOL_SCHEMA:
                raise InputError("tool_trace_required")
            from tool_response_artifacts import verify_binding
            verify_binding(p.get('tool_artifact'), p)
            if row.get('body_format') != p.get('body_format'):
                raise InputError('body_format_binding_mismatch')
            for key in ("task_run_id", "plan_id", "query_id", "execution_id", "source_capture_id"):
                if not row.get(key) or row[key] != p[key]:
                    raise InputError("tool_trace_binding_mismatch", field=key)
            if (row.get("raw_response_sha256") != p["response_body_sha256"]
                    or row.get("page_title") != p["page_title"]
                    or digest(str(row.get("page_title", "")).encode()) != p["page_title_sha256"]):
                raise InputError("tool_trace_content_mismatch")
            if row.get("extraction_rule_sha256") != extraction_rule_sha256() or row.get("redaction_rule_sha256") != redaction_rule_sha256():
                raise InputError("body_processing_rule_changed")
            return network_errors(row, label=label)
        if row.get("network_proof_level") != "host_verified":
            raise InputError("unattested_source_not_formal")
        envelope = verify_receipt(row.get("host_receipt"))
        p = envelope["payload"]
        if row.get('body_format') != p.get('body_format'):
            raise InputError('body_format_binding_mismatch')
        if digest(canonical_bytes(envelope)) != row.get("host_receipt_sha256"):
            raise InputError("host_receipt_hash_mismatch")
        if digest(TRUST_STORE.read_bytes()) != row.get("host_trust_sha256"):
            raise InputError("host_trust_changed")
        for key in ("task_run_id", "plan_id", "query_id", "execution_id", "source_capture_id"):
            if not row.get(key) or row[key] != p[key]:
                raise InputError("host_receipt_binding_mismatch", field=key)
        from source_identity import strict_url_identity
        from temporal_fields import timestamp
        from public_network import normalize_observation
        if (strict_url_identity(row.get("url")) != strict_url_identity(p["request_url"])
                or strict_url_identity(row.get("final_url")) != strict_url_identity(p["final_url"])):
            raise InputError("host_receipt_url_mismatch")
        if timestamp(row.get("retrieved_at")) != timestamp(p["response_finished_at"]):
            raise InputError("host_capture_time_mismatch")
        expected_network = normalize_observation(network_from_receipt(envelope), url=row["url"],
            final_url=row["final_url"], retrieved_at=row["retrieved_at"])
        actual = normalize_observation(row.get("network_observation"), url=row["url"],
            final_url=row["final_url"], retrieved_at=row["retrieved_at"])
        if expected_network != actual:
            raise InputError("host_network_observation_mismatch")
        if row.get("raw_response_sha256") != p["response_body_sha256"]:
            raise InputError("host_body_binding_mismatch")
        if row.get("extraction_rule_sha256") != extraction_rule_sha256() or row.get("redaction_rule_sha256") != redaction_rule_sha256():
            raise InputError("body_processing_rule_changed")
    except (ValueError, OSError, TypeError, KeyError, ImportError) as exc:
        return [f"{label}: {exc}"]
    return []


def reused_call_sources(rows):
    """Reject every participant in a reused call identity, independent of order."""
    calls = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            if row.get('network_proof_level') == 'tool_traceable':
                env = row.get('network_observation')
                if isinstance(env, str):
                    env = json.loads(env)
                key = (env['tool_name'], env['tool_call_id'], env['result_ref'])
                if not env.get('tool_call_id') or not env.get('result_ref'):
                    # No globally unique host invocation is asserted in this mode.
                    # Content/task/execution binding is independently reverified.
                    continue
            else:
                env = row.get('host_receipt')
                if isinstance(env, str):
                    env = json.loads(env)
                p = env['payload']
                key = (p['issuer_id'], p['tool_call_id'])
            calls.setdefault(key, []).append((row.get('source_id', row.get('capture_id', '')), digest(canonical_bytes(env))))
        except (TypeError, ValueError, KeyError):
            continue
    return {source for items in calls.values() if len({sha for _, sha in items}) > 1 for source, _ in items}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capabilities", action="store_true", required=True)
    parser.add_argument("--network-search-available", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--page-read-available", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--probe-record', type=Path, help='Actual page metadata; no fabricated host identifiers')
    parser.add_argument('--tool-result', type=Path, help='Actual response_text JSON paired with --probe-record')
    args = parser.parse_args()
    if bool(args.probe_record) != bool(args.tool_result):
        parser.error('--probe-record and --tool-result must be supplied together')
    from storage_contract import read_staging_json
    print(json.dumps(capability(network_search_available=args.network_search_available,
        page_read_available=args.page_read_available,
        sample_record=read_staging_json(args.probe_record) if args.probe_record else None,
        response_text=read_staging_json(args.tool_result).get('response_text') if args.tool_result else None),
        ensure_ascii=False, indent=2))
