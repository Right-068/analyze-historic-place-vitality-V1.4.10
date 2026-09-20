"""Staging-only adapters and batch helpers; never write formal evidence or scores."""
import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path
import strict_json as json
from temporal_fields import utc_now
from host_adapters import normalize_discovery, imported_page, page_identity, FETCH_ADAPTERS
from input_safety import (parse_public_url, resolve_public_redirect, pinned_transport_url,
                          normalized_transport_url)


def public_target(url):
    """Resolve each hop once; reject mixed/private answers and pin the transport."""
    import socket
    from public_network import global_address
    parsed, host = parse_public_url(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = sorted({global_address(answer[4][0]) for answer in answers})
    if not addresses:
        raise ValueError('local_fetch_no_public_address')
    return parsed, host, port, addresses[0]


def pinned_session(requests, host, secure):
    """Keep original SNI/certificate checks while connecting to a checked IP."""
    class HostAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
            if secure:
                kwargs.update(server_hostname=host, assert_hostname=host)
            return super().init_poolmanager(connections, maxsize, block=block, **kwargs)
    session = requests.Session()
    session.trust_env = False
    session.mount('https://' if secure else 'http://', HostAdapter())
    return session


def fetch(url, adapter):
    """Execute the selected local reader; redirects are checked before each request."""
    import tempfile
    from email.message import Message
    current = url
    for _ in range(10):
        if adapter not in {'curl', 'requests'}:
            raise ValueError('local_fetch_adapter_required')
        if adapter == 'requests':
            try:
                import requests
            except ImportError as exc:
                raise ValueError('requests_adapter_unavailable') from exc
        parsed, host, port, address = public_target(current)
        if adapter == 'curl':
            executable = shutil.which('curl.exe') or shutil.which('curl')
            if not executable:
                raise ValueError('curl_adapter_unavailable')
            with tempfile.TemporaryDirectory(prefix='page-reader-') as tmp:
                body, headers = Path(tmp)/'body', Path(tmp)/'headers'
                target = '[' + address + ']' if ':' in address else address
                resolution = [] if host == address else ['--resolve', f'{host}:{port}:{target}']
                result = subprocess.run([executable, '--disable', '--silent', '--show-error',
                    '--noproxy', '*', '--globoff', *resolution, '--proto', '=http,https',
                    '--max-time', '30', '--max-filesize', '10485760', '--max-redirs', '0',
                    '--output', str(body), '--dump-header', str(headers), '--write-out', '%{json}',
                    normalized_transport_url(current)],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
                if result.returncode:
                    raise ValueError('curl_fetch_failed:' + str(result.returncode))
                meta = json.loads(result.stdout)
                from public_network import global_address
                if global_address(meta.get('remote_ip')) != address:
                    raise ValueError('local_fetch_peer_mismatch')
                status = int(meta['http_code'])
                final = meta['url_effective']
                content_type = meta.get('content_type') or 'text/plain'
                location = meta.get('redirect_url') or ''
                raw = body.read_bytes()
        elif adapter == 'requests':
            pinned, authority = pinned_transport_url(current, address)
            with pinned_session(requests, host, parsed.scheme == 'https') as session:
                with session.get(pinned, headers={'Host': authority}, verify=True,
                                 allow_redirects=False, timeout=30, stream=True) as response:
                    if parse_public_url(response.url) != parse_public_url(pinned):
                        raise ValueError('local_fetch_unexpected_transport_url')
                    status, final = response.status_code, current
                    location = response.headers.get('Location', '')
                    content_type = response.headers.get('Content-Type', 'text/plain')
                    chunks=[]; size=0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 10485760:
                            raise ValueError('page_response_too_large')
                        chunks.append(chunk)
                    raw=b''.join(chunks)
        if 300 <= status < 400 and location:
            current=resolve_public_redirect(current, location)
            continue
        if status != 200:
            raise ValueError('page_http_status:' + str(status))
        parse_public_url(final)
        message=Message();message['content-type']=content_type
        charset=message.get_content_charset() or 'utf-8'
        text=raw.decode(charset, errors='strict')
        kind='text/html' if 'html' in content_type.lower() else 'text/plain'
        title=''
        if kind=='text/html':
            from html.parser import HTMLParser
            class Title(HTMLParser):
                def __init__(self):super().__init__();self.active=False;self.parts=[]
                def handle_starttag(self,tag,attrs):
                    if tag=='title':self.active=True
                def handle_endtag(self,tag):
                    if tag=='title':self.active=False
                def handle_data(self,data):
                    if self.active:self.parts.append(data)
            parser=Title();parser.feed(text);title=''.join(parser.parts).strip()
        return {'request_url':url, 'final_url':final, 'page_title':title, 'page_body':text,
            'body_format':kind, 'retrieved_at':utc_now(), **page_identity(adapter)}
    raise ValueError('page_redirect_limit')


def judgments(action, decisions):
    """Join judgments to program-issued selectors; no caller-supplied IDs or hashes."""
    items=action.get('items',[])
    if not isinstance(decisions,list) or len(decisions)!=len(items):
        raise ValueError('one_decision_per_assigned_item_required')
    return {'action_id':action['action_id'], 'records':[
        {'page_id':item['page_id'],'analysis_fingerprint':item['analysis_fingerprint'],'decision':decision}
        for item,decision in zip(items,decisions)]}


def save_candidate(path, payload):
    """A helper result is editable staging input, not private or formal evidence."""
    import os
    raw=(json.dumps(payload,ensure_ascii=False,indent=2)+'\n').encode('utf8')
    from storage_contract import prepare_staging_payload, write_integrity_artifact
    raw=prepare_staging_payload(path,raw)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()!=raw:raise ValueError('helper_output_conflict_choose_new_path')
        return
    write_integrity_artifact(path,raw)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['normalize-search','normalize-page','fetch-page',
        'build-triage-batch','build-extraction-batch','build-coding-batch'])
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--action-id',required=True)
    p.add_argument('--input',type=Path,required=True,help='Retained responses or decisions in issued item order')
    p.add_argument('--adapter',choices=list(FETCH_ADAPTERS),default='host_web_fetch')
    p.add_argument('--output',type=Path,required=True,help='Must be inside staging/helpers')
    args=p.parse_args()
    from run_research import Workflow, read
    from protected_runtime import verify_before_action
    w=Workflow(args.run_dir);verify_before_action(w.p['state'])
    output=args.output.resolve()
    if not output.is_relative_to(w.root/'staging/helpers') or '.private-response' in output.parts:
        raise ValueError('helper_output_must_be_staging_only')
    issued=read(w.action_path(args.action_id));action=issued['packet']
    action={**action,'action_id':args.action_id}
    values=read(args.input)
    if args.command.startswith('build-'):
        expected='record-'+args.command.removeprefix('build-')
        if issued['operation']!=expected:
            raise ValueError('helper_action_type_mismatch')
        payload=judgments(action,values)
    else:
        search=args.command=='normalize-search'
        if issued['operation']!=('record-search-batch' if search else 'record-pages-batch'):
            raise ValueError('helper_action_type_mismatch')
        assigned=action['assigned_queries' if search else 'items']
        if not isinstance(values,list) or len(values)!=len(assigned):
            raise ValueError('one_response_per_assigned_item_required')
        records=[]
        for item,value in zip(assigned,values):
            if search:
                raw=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
                normalize_discovery(raw)
                record={'execution_id':item['execution_id'],'response_text':raw}
            else:
                if args.command=='fetch-page':
                    record=fetch(item['url'],args.adapter)
                    # A local adapter identity is accepted only with its released-writer receipt.
                    raw=json.dumps(record,ensure_ascii=False,sort_keys=True).encode()
                    receipt=w.root/'staging/helpers/operational'/('fetch-'+hashlib.sha256(raw).hexdigest()+'.json')
                    from protected_runtime import write_operational
                    if not write_operational(receipt,raw):
                        raise ValueError('adapter_receipt_requires_registered_run')
                    record['adapter_receipt']=receipt.relative_to(w.root).as_posix()
                else:
                    record=imported_page(value,adapter_id=args.adapter)
                if record['request_url']!=item['url']:
                    raise ValueError('adapter_request_not_assigned')
                record['execution_id']=item['execution_id']
            records.append(record)
        payload={'action_id':args.action_id,'records':records}
    save_candidate(output,payload)
    print(json.dumps({'status':'prepared','output':str(output),'formal_write':False},ensure_ascii=False))


if __name__=='__main__':main()
