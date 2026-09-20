"""Disposable capability preflight; never creates a research task."""
import argparse
from importlib import metadata, util
import os
from pathlib import Path
import sys
import tempfile
import json

def dependencies(skill_root=None):
    root=Path(skill_root or Path(__file__).resolve().parents[1])
    lines=[v.strip() for v in (root/'requirements.txt').read_text(encoding='utf8').splitlines()
           if v.strip() and not v.lstrip().startswith('#')]
    result=[]
    for line in lines:
        name,version=line.split('==',1)
        try: actual=metadata.version(name)
        except metadata.PackageNotFoundError: actual=None
        result.append({'name':name,'required':version,'installed':actual,'available':actual is not None,
                       'matches_required':actual==version})
    timezone=False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('Asia/Shanghai'); timezone=True
    except (ImportError,KeyError): pass
    return {'python_supported':sys.version_info>=(3,11),'dependencies':result,'timezone_available':timezone,
            'ready':sys.version_info>=(3,11) and timezone and all(r['matches_required'] for r in result)}

def probe_storage(workspace):
    result={key:False for key in ('ordinary_workspace_write_available','atomic_replace_available',
        'fsync_available','private_acl_available','private_file_mode_available','write_dac_available')}
    workspace=Path(workspace).resolve()
    # Probe only an existing writable ancestor. No plan, state or task ID.
    while not workspace.exists() and workspace!=workspace.parent: workspace=workspace.parent
    try:
        with tempfile.TemporaryDirectory(prefix='.storage-probe-',dir=workspace) as folder:
            root=Path(folder); path=root/'probe.bin'
            with path.open('xb') as stream:
                stream.write(b'capability'); stream.flush()
                result['ordinary_workspace_write_available']=True
                os.fsync(stream.fileno()); result['fsync_available']=True
            os.replace(path,root/'committed.bin'); result['atomic_replace_available']=True
            try:
                from private_artifacts import secure_directory, write_private_artifact
                private=root/'.private-response'; private.mkdir()
                secure_directory(private); write_private_artifact(private/'probe.bin',b'capability')
                result['private_acl_available']=os.name=='nt'
                result['private_file_mode_available']=os.name!='nt'
                result['write_dac_available']=os.name=='nt'
            except (ValueError,OSError): pass  # Probe reports absence; never stores private research bytes.
    except OSError as exc:
        result['error_type']=type(exc).__name__
    result['operational_storage_available']=all(result[k] for k in (
        'ordinary_workspace_write_available','atomic_replace_available','fsync_available'))
    result['confidential_storage_available']=result['private_acl_available'] or result['private_file_mode_available']
    return result

def doctor(workspace, skill_root=None):
    dep=dependencies(skill_root)
    storage=probe_storage(workspace)
    return {'status':'ready' if dep['ready'] and storage['operational_storage_available'] else 'blocked',
            'dependency_check':dep,'storage':storage,'network_tool_capability':'requires_actual_host_tool_probe',
            'creates_task':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=Path.cwd())
    args=parser.parse_args()
    print(json.dumps(doctor(args.workspace),ensure_ascii=False))

if __name__=='__main__': main()
