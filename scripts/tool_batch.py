"""Small factual host inputs -> the existing capture/admission input contract.

Only local identifiers, bindings and serialization are derived. Search excerpts
remain discovery data; an actual page response and classification are separate.
"""
from pathlib import Path
import hashlib
import re
import strict_json as json
from temporal_fields import utc_now
from storage_contract import write_integrity_artifact
from runtime_dispatch import digest
from functools import wraps
from contextlib import contextmanager

def legacy_adapter_only(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        if (self.action_dispatch_enabled() or self.pipeline_read()) and not getattr(self, '_in_pipeline_adapter', False):
            raise ValueError('legacy_interface_disabled_use_execute_next_action')
        return function(self, *args, **kwargs)
    return guarded

def read(path):
    from storage_contract import read_staging_json
    return read_staging_json(path)

def save(path,value,*,rebuild=False):
    from protected_runtime import check_operational_before_write, register_operational_write
    check_operational_before_write(path)
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    from storage_contract import prepare_staging_payload
    raw=prepare_staging_payload(path,raw)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()==raw:return
        if not rebuild:raise ValueError('recorded_tool_fact_conflict')
        old=path.read_bytes()
        backup=path.with_name('rejected-'+hashlib.sha256(old).hexdigest()+'.bin')
        if not backup.exists():write_integrity_artifact(backup,old,transaction_id='preserve-invalid-staging')
    from protected_runtime import write_operational
    if write_operational(path,raw):return
    if path.exists():path.unlink()  # Non-protected staging; rejected bytes retained above.
    write_integrity_artifact(path,raw,transaction_id='tool-fact')
    register_operational_write(path)


def discovery_results(response_text, result_count=None, urls=None, search_results=None):
    from host_adapters import normalize_discovery
    return normalize_discovery(response_text, expected_count=result_count,
                               urls=urls, search_results=search_results)

class ToolBatchMixin:
    @contextmanager
    def pipeline_adapter(self):
        """Internal compatibility bridge, not a host command or authorization."""
        previous = getattr(self, '_in_pipeline_adapter', False)
        self._in_pipeline_adapter = True
        try:
            yield
        finally:
            self._in_pipeline_adapter = previous

    def tool_folder(self,execution_id):
        if not re.fullmatch(r'EX-[A-Za-z0-9_-]+',execution_id):raise ValueError('invalid_execution_selector')
        return self.root/'staging/tool-facts'/execution_id/'operational'

    @legacy_adapter_only
    def begin_query(self,execution_id):
        folder=self.tool_folder(execution_id)
        if (folder/'started.json').exists():
            if (folder/'normalized-batch.json').exists():
                return self.packet({'status':'recorded','action':'replay_commit_then_advance','execution_id':execution_id})
            return self.packet({'status':'recorded','action':'open_discovered_pages' if (folder/'search.json').exists() else 'run_native_search',
                                'execution':read(folder/'started.json')['execution']})
        task=self.next()
        query=next((q for q in task.get('assigned_queries',[]) if q['execution_id']==execution_id),None)
        if query is None:raise ValueError('execution_not_in_current_window')
        save(folder/'started.json',{'execution':query,'started_at':utc_now(),'task_run_id':task['task_run_id']})
        return self.packet({'status':'recorded','action':'run_native_search','execution':query})

    @legacy_adapter_only
    def record_search(self,execution_id,response_text,*,tool_name='web_search',result_count=None,urls=None,
                      status='completed',error_type='',login_triggered=False,restriction_triggered=False,
                      defer_complete=False,search_results=None):
        folder=self.tool_folder(execution_id)
        start=read(folder/'started.json')
        if not isinstance(response_text,str):raise ValueError('search_response_must_be_text')
        from execution_schema import TOOLS
        from execution_semantics import STATUS_MATRIX
        if tool_name not in TOOLS or status not in STATUS_MATRIX:raise ValueError('invalid_search_outcome_enum')
        discovered, completeness = discovery_results(response_text,result_count,urls,search_results)
        result_count = completeness['result_count']
        if status=='completed' and result_count==0:status='no_results'
        value={'response_text':response_text,'search_tool':tool_name,'returned_results':result_count,
               'urls':discovered,'discovery_completeness':completeness,'status':status,'error_type':error_type,
               'login_triggered':bool(login_triggered),'restriction_triggered':bool(restriction_triggered)}
        path=folder/'search.json'
        if path.exists():
            existing=read(path)
            if {k:v for k,v in existing.items() if k!='finished_at'}!=value:raise ValueError('recorded_search_conflict')
        else:save(path,{**value,'finished_at':utc_now()})
        if not defer_complete and (status=='no_results' or status in ('failed','blocked')):
            result=self.complete_query(execution_id)
            return {**result,'search_outcome':status}
        from batch_pipeline import config
        return self.packet({'status':'recorded','action':'open_discovered_pages','execution_id':execution_id,
            'discovery_count':len(discovered),'discovery_file':str(path),'next_page_urls':discovered[:config()['candidate_urls_per_query']],
            'note':'Search response is discovery only. Read actual page bodies before capture.'})

    @legacy_adapter_only
    def record_page(self,execution_id,**facts):
        folder=self.tool_folder(execution_id)
        original=folder/('input-'+digest(facts)+'.json')
        if original.exists():observed=read(original)
        else:
            observed={'facts':facts,'observed_at':utc_now()};save(original,observed)
        facts={**facts,'retrieved_at':facts.get('retrieved_at') or observed['observed_at']}
        try:return self._record_page(execution_id,**facts)
        except (ValueError,TypeError,KeyError) as exc:
            from workflow_errors import raise_if_storage_error
            raise_if_storage_error(exc)
            return self.soft_error('page_input_requires_repair',execution_id=execution_id,
                action='repair_from_actual_tool_result_or_next_page',detail=str(exc))

    def _record_page(self,execution_id,*,request_url,final_url,page_title,page_body,retrieved_at=None,
                    tool_name='web_fetch',tool_call_id='',result_ref='',body_format='text/plain',actual_fetch_method='',
                    source_category=None,content_layer='page_body',entity_level='area_direct',
                    relevant=False,promotion_status='unknown',evidence=None,kind='page',access_status='full',classification_basis=None):
        folder=self.tool_folder(execution_id)
        read(folder/'started.json');read(folder/'search.json')
        if kind!='page':
            return self.soft_error('discovery_requires_page_read',execution_id=execution_id,action='open_actual_page',detail=request_url)
        if not page_title and body_format=='text/html':
            from html.parser import HTMLParser
            class Title(HTMLParser):
                active=False
                parts=[]
                def handle_starttag(self,tag,attrs):
                    if tag.lower()=='title':self.active=True
                def handle_endtag(self,tag):
                    if tag.lower()=='title':self.active=False
                def handle_data(self,data):
                    if self.active:self.parts.append(data)
            parser=Title();parser.parts=[];parser.feed(page_body);page_title=''.join(parser.parts).strip()
        if not page_title or not final_url or not page_body.strip():
            return self.soft_error('page_minimum_facts_missing',execution_id=execution_id,action='read_metadata_or_next_page',detail=request_url)
        if access_status not in ('full','partial'):
            return self.page_failure(execution_id,request_url,access_status)
        from source_capture import SOURCE_CATEGORIES, CONTENT_LAYERS
        from content_blocks import USER_UNITS, BLOCK_TYPES
        if source_category not in SOURCE_CATEGORIES or content_layer not in CONTENT_LAYERS:
            return self.soft_error('page_classification_required',execution_id=execution_id,action='classify_recorded_page',detail=request_url)
        if content_layer not in BLOCK_TYPES:
            return self.soft_error('text_unit_layer_required',execution_id=execution_id,
                action='select_page_body_or_observed_specific_text_unit_layer',detail=request_url)
        if promotion_status not in ('unknown','suspected','not_suspected'):raise ValueError('invalid_promotion_status')
        from public_network import normalize_tool_trace
        from host_receipts import analyze_body
        visible, _, _ = analyze_body(page_body.encode('utf8'),body_format)
        if not visible.strip():
            return self.soft_error('page_has_no_visible_text',execution_id=execution_id,action='read_next_public_page',detail=request_url)
        if classification_basis is not None and content_layer in USER_UNITS:
            from host_adapters import validate_user_basis
            validate_user_basis(classification_basis,visible)
        facts={'url':request_url,'final_url':final_url,'page_title':page_title,'visible_body':visible,
            'access_status':access_status,'source_type':source_category,'content_layer':content_layer}
        semantic={'place_name':self.state()['place'],'entity_level':entity_level,'source_category':source_category,
                  'content_layer':content_layer,'is_relevant':bool(relevant),'selection_mechanism':'search_result',
                  'suspected_promotion':promotion_status!='not_suspected'}
        candidate_evidence=[]
        for unit in evidence or []:
            if set(unit)-{'text','primary_dimension','unit_type','place_relevance'}:raise ValueError('candidate_judgement_fields_only')
            if not isinstance(unit.get('text'),str) or unit['text'] not in visible:raise ValueError('candidate_text_not_in_response')
            if visible.count(unit['text'])!=1:
                return self.soft_error('candidate_excerpt_ambiguous',execution_id=execution_id,
                    action='select_unique_context_from_retained_page_without_searching_again',detail=request_url)
            candidate_evidence.append({'original_visible_text':unit['text'],'primary_dimension':unit.get('primary_dimension',''),
                'unit_type':unit.get('unit_type',content_layer),
                'place_relevance':unit.get('place_relevance','direct'),'content_layer':content_layer,'evidence_type':'public_network_text'})
        payload={'facts':facts,'source':semantic,'evidence':candidate_evidence,'response_text':page_body,
                 'tool_name':tool_name,'tool_call_id':tool_call_id,'result_ref':result_ref,'body_format':body_format,
                 'actual_fetch_method':actual_fetch_method,'classification_basis':classification_basis}
        page_key=digest(payload)
        path=folder/f'page-{page_key}.json'
        if not path.exists():
            import csv
            if self.p['search'].exists():
                with self.p['search'].open(encoding='utf-8-sig',newline='') as stream:
                    closed=any(r['execution_id']==execution_id for r in csv.DictReader(stream))
                if closed:
                    return self.soft_error('execution_already_closed',execution_id=execution_id,
                        action='continue_next_unexecuted_query_without_changing_closed_facts',detail=request_url)
        if path.exists():
            value=read(path)
            if retrieved_at and retrieved_at!=value['record']['retrieved_at']:raise ValueError('page_observation_time_conflict')
        else:
            at=retrieved_at or utc_now()
            trace={'schema_version':'tool-page-trace-1','tool_name':tool_name,'tool_call_id':tool_call_id,'result_ref':result_ref,
                   'request_url':request_url,'final_url':final_url,'page_title':page_title,'retrieved_at':at,'body_format':body_format}
            # Existing normalizer validates URL, timestamp and optional enhanced metadata.
            normalize_tool_trace(trace,url=request_url,final_url=final_url,retrieved_at=at)
            record={**facts,'retrieved_at':at,'tool_trace':trace}
            # Existing extraction remains authoritative (including HTML visibility).
            if classification_basis is not None and content_layer in USER_UNITS:
                scope=classification_basis['user_content_excerpt']
                start=visible.index(scope);end=start+len(scope)
                blocks=[]
                if start:blocks.append({'block_id':'B0','block_type':'page_body','start':0,'end':start,'is_user_generated':False})
                blocks.append({'block_id':'B1','block_type':content_layer,'start':start,'end':end,'is_user_generated':True})
                if end<len(visible):blocks.append({'block_id':'B2','block_type':'page_body','start':end,'end':len(visible),'is_user_generated':False})
                record['visible_blocks']=blocks
            elif body_format=='text/plain':
                record['visible_body']=page_body
                record['visible_blocks']=[{'block_id':'B1','block_type':content_layer,'start':0,'end':len(page_body),
                                          'is_user_generated':content_layer in USER_UNITS}]
            value={'execution_id':execution_id,'record':record,'response_text':page_body,
                   'candidate_source':semantic,'candidate_evidence':candidate_evidence}
            save(path,value)
        return self.packet({'status':'recorded','action':'read_next_page_or_complete_query','execution_id':execution_id,
            'page_key':page_key,'fact_file':str(path),'candidate_units':len(value['candidate_evidence'])})

    @legacy_adapter_only
    def page_failure(self,execution_id,url,reason):
        folder=self.tool_folder(execution_id)
        read(folder/'started.json')
        value={'url':url,'reason':reason}
        save(folder/('failed-'+digest(value)+'.json'),value)
        return self.soft_error('page_unavailable',execution_id=execution_id,detail=reason,
            action='continue_other_public_result_without_bypassing_access_limits')

    @legacy_adapter_only
    def complete_query(self,execution_id):
        folder=self.tool_folder(execution_id)
        start=read(folder/'started.json');search=read(folder/'search.json');query=start['execution']
        pages=[read(p) for p in sorted(folder.glob('page-*.json'))]
        failures=[read(p) for p in folder.glob('failed-*.json')]
        completion=folder/'completed.json'
        if not completion.exists():save(completion,{'finished_at':utc_now()})
        observation={k:query[k] for k in ('plan_id','query_id','execution_id','retry_number','retry_reason','original_execution_id','retry_interval_policy')}
        observation.update(started_at=start['started_at'],finished_at=search['finished_at'],retrieved_at=search['finished_at'],
            search_tool=search['search_tool'],status=search['status'],next_action='continue',returned_results=search['returned_results'],
            opened_pages=len(pages)+len(failures),relevant_pages=sum(p['candidate_source']['is_relevant'] for p in pages),
            duplicate_pages=0,login_triggered=search['login_triggered'],restriction_triggered=search['restriction_triggered'],error_type=search['error_type'])
        # Execution finish bounds include actual page reading; replay uses recorded times.
        times=[observation['finished_at'],read(completion)['finished_at']]+[p['record']['retrieved_at'] for p in pages]
        from temporal_fields import timestamp
        observation['finished_at']=observation['retrieved_at']=max(times,key=timestamp)
        batch={'observations':[observation],'pages':pages}
        # Immutable input reconstructs serialization; it never repairs a missing fact by guessing.
        save(folder/'normalized-batch.json',batch,rebuild=True)
        result=self.submit(batch)
        return self.packet({**result,'action':'advance_then_next','execution_id':execution_id})
