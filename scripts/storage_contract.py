"""Durable integrity storage, separate from confidential OS access control.

Operational callers retain approved-writer transactions; public raw callers
retain capture/provenance hashes. Neither requires changing filesystem ACLs.
"""
import hashlib
import os
from pathlib import Path
import re
import uuid
from input_safety import InputError, redact_text
from process_lock import ProcessFileLock

_CREDENTIAL = re.compile(r'''(?ix)(?:["']?(?:authorization|proxy-authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|session[_-]?(?:token|credential)|password)["']?\s*[:=]\s*["']?)(?!null\b|false\b|["']?\s*[,}])[^\s"',}]+''')

def reject_credentials(data):
    import json
    pending=[data.decode('utf8', errors='replace')]
    seen=set()
    while pending:
        text=pending.pop()
        if text in seen: continue
        seen.add(text)
        if _CREDENTIAL.search(text):
            raise InputError('credential_storage_forbidden')
        try: value=json.loads(text)
        except (ValueError,RecursionError):
            if '\n' in text: pending.extend(text.splitlines())
            continue
        values=[value]
        while values:
            item=values.pop()
            if isinstance(item,dict):
                for key,child in item.items():
                    if re.fullmatch(r'(?i)authorization|proxy-authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|session[_-]?(?:token|credential)|password',key) and child not in (None,False,''):
                        raise InputError('credential_storage_forbidden')
                values.extend(item.values())
            elif isinstance(item,list): values.extend(item)
            elif isinstance(item,str): pending.append(item)

def prepare_staging_payload(path, data):
    """Keep raw personal text out of operational intents and journals.

    Ordinary metadata remains JSON. A sensitive staged payload is an immutable
    private blob plus an integrity-checked local reference, never a plaintext
    copy in the operational transaction's base64 content.
    """
    reject_credentials(data)
    if redact_text(data.decode('utf8',errors='replace'))==data.decode('utf8',errors='replace'):
        return data
    from private_artifacts import write_private_artifact
    import json
    digest=hashlib.sha256(data).hexdigest()
    blob=Path(path).parent/'.private-response'/('PAYLOAD-'+digest+'.bin')
    write_private_artifact(blob,data)
    return json.dumps({'storage_reference':'confidential-staging-1','sha256':digest},
                      sort_keys=True).encode('utf8')

def read_staging_bytes(path):
    import json
    path=Path(path);raw=path.read_bytes()
    try: value=json.loads(raw)
    except (ValueError,UnicodeError): return raw
    if not isinstance(value,dict) or value.get('storage_reference')!='confidential-staging-1':return raw
    if set(value)!={'storage_reference','sha256'} or not re.fullmatch('[0-9a-f]{64}',str(value['sha256'])):
        raise InputError('confidential_staging_reference_invalid')
    blob=path.parent/'.private-response'/('PAYLOAD-'+value['sha256']+'.bin')
    if blob.is_symlink() or blob.resolve()!=blob.absolute() or not blob.is_file():
        raise InputError('confidential_staging_blob_missing')
    data=blob.read_bytes()
    if hashlib.sha256(data).hexdigest()!=value['sha256']:raise InputError('confidential_staging_blob_changed')
    return data

def read_staging_json(path):
    import strict_json as json
    return json.loads(read_staging_bytes(path).decode('utf-8-sig'))

def raw_storage_class(data, *, confidential=False):
    reject_credentials(data)
    text = data.decode('utf8', errors='replace')
    return 'confidential' if confidential or redact_text(text) != text else 'public_raw'

def raw_directory(root, data, *, confidential=False):
    return Path(root) / ('.private-response' if raw_storage_class(data, confidential=confidential)=='confidential' else 'public-raw')

def raw_path_allowed(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path.parent in (root/'public-raw', root/'.private-response')

def fsync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

def write_integrity_artifact(path, data, *, transaction_id=None, phase_callback=None):
    """Immutable hash-verified commit, with transaction-owned temp cleanup."""
    path = Path(path).absolute()
    if path.resolve()!=path or '.private-response' in path.parts:
        raise InputError('integrity_artifact_path_invalid')
    if not isinstance(data,bytes): raise InputError('artifact_bytes_required')
    reject_credentials(data)
    tx = str(transaction_id or 'TX-'+uuid.uuid4().hex)
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',tx): raise InputError('artifact_transaction_id_invalid')
    temporary = path.with_name(f'.{path.name}.{tx}.tmp')
    digest = hashlib.sha256(data).hexdigest()
    meta = dict(target_name=path.name, temporary_name=temporary.name, transaction_id=tx,
                expected_sha256=digest, expected_size=len(data))
    def phase(stage):
        if phase_callback is not None: phase_callback({**meta,'stage':stage})
    path.parent.mkdir(parents=True,exist_ok=True)
    with ProcessFileLock(path):
        if path.exists():
            if path.read_bytes()!=data: raise InputError('artifact_hash_conflict')
            phase('reused'); return {**meta,'status':'reused'}
        try:
            if temporary.exists(): temporary.unlink()
            phase('before_write')
            with temporary.open('xb') as stream:
                split=max(1,len(data)//2) if data else 0
                stream.write(data[:split]); stream.flush(); phase('partial_written')
                stream.write(data[split:]); phase('fully_written'); stream.flush(); os.fsync(stream.fileno())
            phase('synced')
            if temporary.stat().st_size!=len(data) or hashlib.sha256(temporary.read_bytes()).hexdigest()!=digest:
                raise InputError('artifact_hash_conflict')
            phase('verified'); os.replace(temporary,path); fsync_directory(path.parent)
            phase('committed'); return {**meta,'status':'committed'}
        finally:
            if temporary.exists(): temporary.unlink()

def write_raw_artifact(path, data, **kwargs):
    path=Path(path)
    category=raw_storage_class(data,confidential=path.parent.name=='.private-response')
    if category=='confidential':
        if path.parent.name!='.private-response': raise InputError('confidential_storage_path_required')
        from private_artifacts import write_private_artifact
        return write_private_artifact(path,data,**kwargs)
    if path.parent.name!='public-raw': raise InputError('public_raw_storage_path_required')
    return write_integrity_artifact(path,data,**kwargs)
