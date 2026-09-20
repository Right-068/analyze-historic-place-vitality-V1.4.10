"""Unique, non-overlapping leaf blocks and exact evidence locators."""
from __future__ import annotations
import hashlib
from pathlib import Path
from input_safety import InputError

BLOCK_TYPES = frozenset({"page_body", "official_fact", "news_article", "academic_text",
    "user_post", "user_review", "comment", "reply", "metadata"})
USER_UNITS = frozenset({"user_post", "user_review", "comment", "reply"})
NON_USER_BLOCK_TYPES = frozenset({"official_fact", "news_article", "academic_text", "metadata"})

# This is the single released compatibility matrix for leaf-block identity.
# ``page_body`` remains contextual because a page-wide body may itself be a
# public user post.  Every semantically specific block has exactly one allowed
# user-origin value; a boolean with the wrong meaning is never sufficient.
BLOCK_USER_STATUS = {
    "page_body": frozenset({False, True}),
    **{name: frozenset({False}) for name in NON_USER_BLOCK_TYPES},
    **{name: frozenset({True}) for name in USER_UNITS},
}


def validate_block_semantics(block_type, is_user_generated, *, row=None):
    if not isinstance(block_type, str) or block_type not in BLOCK_TYPES:
        raise InputError("invalid_block_type", row=row)
    if type(is_user_generated) is not bool:
        raise InputError("block_user_status_must_be_boolean", row=row)
    if is_user_generated not in BLOCK_USER_STATUS[block_type]:
        raise InputError("block_type_user_status_conflict", row=row)


def _stored_bool(value):
    if type(value) is bool:
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def evidence_content_errors(row, *, require_locator=False, formal_scoring=False):
    """Validate one evidence record against the shared content matrix.

    The function is deliberately free of scoring thresholds.  Admission,
    evidence audit, formal eligibility, and final validation can therefore use
    the same content qualification without creating a second scoring method.
    """
    unit_type = str(row.get("unit_type", "")).strip()
    content_layer = str(row.get("content_layer", "")).strip()
    block_type = str(row.get("source_block_type", "")).strip()
    stored_user = row.get("source_block_is_user_generated", "")
    has_locator_claim = bool(block_type or str(row.get("source_block_id", "")).strip()
                             or str(row.get("locator_sha256", "")).strip())
    errors = []

    if unit_type == "source_native_numeric":
        if content_layer != "rating_only":
            errors.append("native_numeric_content_layer_conflict")
        if has_locator_claim or block_type:
            errors.append("native_numeric_locator_conflict")
        return errors

    if unit_type not in {"page_body", "official_fact", *USER_UNITS}:
        return errors
    if require_locator and not has_locator_claim:
        errors.append("text_evidence_locator_missing")
        return errors
    if not has_locator_claim:
        return errors

    parsed_user = _stored_bool(stored_user)
    try:
        validate_block_semantics(block_type, parsed_user)
    except InputError as exc:
        errors.append(exc.detail["code"])

    if unit_type in {*USER_UNITS, "official_fact"} and content_layer != unit_type:
        # Keep the generic unit/layer mismatch first so every caller reports
        # the same machine-readable contract error before a more specific
        # identity diagnosis.
        errors.append("unit_content_layer_conflict")

    if unit_type in USER_UNITS:
        if block_type != unit_type:
            errors.append("unit_block_type_conflict")
        if parsed_user is not True:
            errors.append("user_block_locator_required")
    elif unit_type == "official_fact":
        if content_layer != "official_fact" or block_type != "official_fact":
            errors.append("official_fact_content_identity_conflict")
        if parsed_user is not False:
            errors.append("official_fact_user_status_conflict")
        if formal_scoring:
            errors.append("official_fact_not_formally_scorable")
    else:  # page_body
        if content_layer != "page_body" or block_type != "page_body":
            errors.append("page_body_content_identity_conflict")
        if formal_scoring and parsed_user is not True:
            errors.append("page_body_lacks_user_origin")
    return list(dict.fromkeys(errors))


def rule_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def block_visibility(level, ranges, start, end):
    if level != 'block_scoped':
        return level
    if not isinstance(ranges, list) or not ranges or any(
            not isinstance(r, list) or len(r) != 2 or any(type(n) is not int for n in r)
            or not 0 <= r[0] < r[1] for r in ranges):
        raise InputError('body_visibility_ranges_invalid')
    return 'unconfirmed' if any(start < right and end > left for left, right in ranges) else 'static_explicit'


def normalize_blocks(body, blocks, *, visibility_proof_level=None, visibility_unconfirmed_ranges=None):
    if not isinstance(body, str):
        raise InputError("text_must_be_string", field="visible_body")
    if not body:
        if blocks not in (None, "", []):
            raise InputError("blocks_without_body")
        return []
    if blocks in (None, "", []):
        blocks = [{"block_id": "B001", "block_type": "page_body", "start": 0,
                   "end": len(body), "is_user_generated": False}]
    if not isinstance(blocks, list):
        raise InputError("blocks_must_be_array")
    result = []
    ids = set()
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    for i, item in enumerate(blocks):
        if not isinstance(item, dict):
            raise InputError("block_must_be_object", row=i)
        block_id = item.get("block_id")
        if not isinstance(block_id, str) or not block_id or block_id in ids:
            raise InputError("duplicate_or_missing_block_id", row=i)
        ids.add(block_id)
        validate_block_semantics(item.get("block_type"), item.get("is_user_generated"), row=i)
        start, end = item.get("start"), item.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body):
            raise InputError("invalid_block_offsets", row=i)
        if item.get("parent_block_id") not in (None, ""):
            raise InputError("leaf_blocks_required", row=i)
        piece = body[start:end]
        if "text" in item and item["text"] != piece:
            raise InputError("block_text_mismatch", row=i)
        row = {key: item[key] for key in ("block_id", "block_type", "start", "end", "is_user_generated")}
        row.update(text_sha256=hashlib.sha256(piece.encode()).hexdigest(), visible_body_sha256=body_hash,
                   recognition_rule_sha256=rule_sha256(), parent_block_id="")
        if visibility_proof_level is not None:
            if visibility_proof_level not in {'tool_result', 'static_explicit', 'unconfirmed', 'block_scoped'}:
                raise InputError('body_visibility_contract_invalid')
            level = block_visibility(visibility_proof_level, visibility_unconfirmed_ranges, start, end)
            if item.get('visibility_proof_level', level) != level:
                raise InputError('block_visibility_binding_mismatch')
            row['visibility_proof_level'] = level
        for key in ("text_sha256", "visible_body_sha256", "recognition_rule_sha256"):
            if key in item and item[key] != row[key]:
                raise InputError("block_hash_or_rule_mismatch", field=key, row=i)
        result.append(row)
    result.sort(key=lambda b: (b["start"], b["end"], b["block_id"]))
    if any(a["end"] > b["start"] for a, b in zip(result, result[1:])):
        raise InputError("ambiguous_content_block")
    return result


def locate_leaf(capture, start, end):
    blocks = capture.get("visible_blocks")
    if not isinstance(blocks, list) or type(start) is not int or type(end) is not int or start >= end:
        raise InputError("invalid_evidence_locator")
    if any(not isinstance(block, dict) for block in blocks):
        raise InputError("block_must_be_object")
    matches = [b for b in blocks if type(b.get("start")) is int and type(b.get("end")) is int
               and b["start"] <= start < end <= b["end"]]
    if len(matches) != 1:
        raise InputError("ambiguous_content_block" if matches else "evidence_block_not_found")
    block = matches[0]
    if type(block.get("is_user_generated")) is not bool or block.get("recognition_rule_sha256") != rule_sha256():
        raise InputError("unverified_content_block")
    validate_block_semantics(block.get("block_type"), block.get("is_user_generated"))
    level = block_visibility(capture.get('visibility_proof_level'),
        capture.get('visibility_unconfirmed_ranges'), block['start'], block['end'])
    if block.get('visibility_proof_level') != level:
        raise InputError('block_visibility_binding_mismatch')
    if block.get('is_user_generated') and level == 'unconfirmed':
        raise InputError('body_visibility_unconfirmed_requires_rendered_read')
    return block
