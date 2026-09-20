"""Release-bound reviewer identity and portable, publicly verifiable decisions.

There is no runtime enrollment or trust-store override. An empty reviewer list
only leaves human-dependent records pending; ordinary research is unaffected.
"""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import strict_json as json

TRUST_STORE = Path(__file__).resolve().parents[1] / 'assets' / 'review-trust.json'
PERMISSIONS = frozenset({'semantic', 'collision'})
PROOF_FIELD = 'review_trust_proof'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def roots():
    raw = TRUST_STORE.read_bytes()
    config = json.loads(raw.decode('utf-8'))
    if (not isinstance(config, dict) or set(config) != {'schema_version', 'reviewers'}
            or config['schema_version'] != 'review-trust-1' or not isinstance(config['reviewers'], list)):
        raise ValueError('review_trust_config_invalid')
    records = {}
    for entry in config['reviewers']:
        if (not isinstance(entry, dict) or set(entry) != {'reviewer_id', 'key_sha256', 'ed25519_public_key', 'permissions'}
                or not isinstance(entry['reviewer_id'], str) or not entry['reviewer_id'].strip()
                or entry['reviewer_id'] in records or not isinstance(entry['permissions'], list)
                or not entry['permissions'] or any(not isinstance(p, str) or p not in PERMISSIONS for p in entry['permissions'])
                or len(set(entry['permissions'])) != len(entry['permissions'])):
            raise ValueError('review_trust_config_invalid')
        for field in ('key_sha256', 'ed25519_public_key'):
            value = entry[field]
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('review_trust_config_invalid')
        records[entry['reviewer_id']] = entry
    return hashlib.sha256(raw).hexdigest(), records


def _private(key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('review_key_invalid')
    # Domain-separated seed: the existing input HMAC key is not stored in a
    # decision or public artifact. The publisher registers the matching public
    # key outside a run, enabling final read-only verification without secrets.
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b'review-proof-1\0' + key).digest())


def authorize_key(reviewer_id, key, permission):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    trust_sha, records = roots()
    entry = records.get(reviewer_id)
    if permission not in PERMISSIONS or entry is None or permission not in entry['permissions']:
        raise ValueError('reviewer_not_preconfigured_for_permission')
    if not hmac.compare_digest(hashlib.sha256(key).hexdigest(), entry['key_sha256']):
        raise ValueError('reviewer_key_not_preconfigured')
    public = _private(key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    if not hmac.compare_digest(public, entry['ed25519_public_key']):
        raise ValueError('reviewer_public_key_mismatch')
    return trust_sha, entry


def make_proof(payload, key, permission):
    trust_sha, entry = authorize_key(payload.get('reviewer_id'), key, permission)
    core = {'schema_version': 'review-proof-1', 'review_trust_sha256': trust_sha,
            'reviewer_id': entry['reviewer_id'], 'key_sha256': entry['key_sha256'],
            'permission': permission, 'payload': payload}
    return json.dumps({**core, 'signature': _private(key).sign(canonical(core)).hex()},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def verify_proof(proof, permission):
    if isinstance(proof, str):
        proof = json.loads(proof)
    expected = {'schema_version', 'review_trust_sha256', 'reviewer_id', 'key_sha256', 'permission', 'payload', 'signature'}
    if not isinstance(proof, dict) or set(proof) != expected or proof['schema_version'] != 'review-proof-1':
        raise ValueError('review_trust_proof_missing_or_invalid')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    trust_sha, records = roots()
    entry = records.get(proof['reviewer_id'])
    if (proof['review_trust_sha256'] != trust_sha or entry is None
            or permission not in PERMISSIONS or proof['permission'] != permission
            or permission not in entry['permissions'] or proof['key_sha256'] != entry['key_sha256']
            or not isinstance(proof['payload'], dict) or proof['payload'].get('reviewer_id') != entry['reviewer_id']):
        raise ValueError('review_trust_binding_invalid')
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(entry['ed25519_public_key'])).verify(
            bytes.fromhex(proof['signature']), canonical({k: v for k, v in proof.items() if k != 'signature'}))
    except Exception as exc:
        raise ValueError('review_trust_signature_invalid') from exc
    return proof['payload']


def collision_evidence_proof_valid(row):
    try:
        signed = verify_proof(row.get('collision_review_proof'), 'collision')
        return (signed.get('task_run_id') == row.get('task_run_id')
                and row.get('source_id') in signed.get('source_ids', [])
                and row.get('evidence_id') in signed.get('evidence_ids', [])
                and signed.get('conclusion') == 'verified_independent_occurrence')
    except (ValueError, TypeError, KeyError, OSError, ImportError):
        return False


def semantic_proof_valid(row):
    try:
        payload = verify_proof(row.get(PROOF_FIELD), 'semantic')
        mapping = {'task_run_id': 'task_run_id', 'evidence_id': 'evidence_id',
                   'primary_dimension': 'primary_dimension', 'sentiment': 'sentiment',
                   'stance_strength': 'stance_strength', 'reviewer_id': 'reviewer_id',
                   'review_origin': 'review_origin', 'review_record_id': 'review_record_id',
                   'review_decision_time': 'decision_time', 'coder_id': 'reviewer_id'}
        if any(str(row.get(field, '')) != str(payload.get(signed, '')) for field, signed in mapping.items()):
            return False
        return (payload.get('decision') == 'human_confirmed'
                and bool(payload.get('decision_reason')) and len(str(payload.get('before_sha256', ''))) == 64
                and row.get('semantic_method') == (payload.get('semantic_method') or 'manual_code')
                and row.get('review_provenance_sha256') == hashlib.sha256(canonical(payload)).hexdigest())
    except (ValueError, TypeError, KeyError, OSError, ImportError):
        return False
