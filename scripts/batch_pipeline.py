"""Sequential, resumable host batches above the unchanged formal evidence chain.

This sidecar schedules work, never authorizes a source, score or audit outcome.
Raw observations and judgments remain separate until the released writer runs.
"""
from pathlib import Path
import hashlib
import strict_json as json
from tool_batch import read, save
from runtime_dispatch import digest
from temporal_fields import utc_now
from page_views import clean_body, compact_view, content_hash, similarity, bounded
from cost_metrics import atomic_json, record as cost_record

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('DISCOVERY','PAGE_CAPTURE','NORMALIZE','TRIAGE','EVIDENCE_EXTRACTION','SEMANTIC_CODING','EVIDENCE_AUDIT')


def config():
    value = read(ROOT/'assets/batch-execution.json')
    bounds = {'query_window_size':(1,8),'candidate_urls_per_query':(1,3),'initial_pages_per_query':(1,2),
              'page_capture_batch_size':(1,12),'triage_batch_size':(1,16),'semantic_batch_size':(1,12),
              'max_agent_visible_chars':(1000,24000),'page_view_chars':(100,1800)}
    for key, (low,high) in bounds.items():
        if type(value.get(key)) is not int or not low <= value[key] <= high:
            raise ValueError('invalid_batch_configuration:'+key)
    return value


def agent_view(value):
    # Rich machine results are retained locally; keep only actionable selectors.
    allowed = {'status','continuation','action','phase','module','batch_id','task_run_id','counts','soft_errors',
        'assigned_queries','items','completed_query_count','remaining_query_count','plan_total','window_query_ids',
        'window_id','window_size','next_cursor','execution_id','execution','next_page_urls','discovery_count',
        'error','error_code','error_detail','reason','artifacts','report_truth','narrative_input','limitations_input',
        'review_required_count','semantic_evidence','audit_repair_attempt','audit_repair_action',
        'action_id','operation','input_contract','module_summary','collection_coverage','pending_review_policy',
        'current_page_entities','minimum_page_target','contract','has_more','view','metrics'}
    return {key:item for key,item in value.items() if key in allowed}


class BatchPipelineMixin:
    def pipeline_folder(self):
        return self.root/'staging/pipeline/operational'

    def pipeline_read(self):
        path = self.pipeline_folder()/'module-state.json'
        return read(path) if path.exists() else None

    def pipeline_save(self, value):
        atomic_json(self.pipeline_folder()/'module-state.json', value)

    def pipeline_stage(self, state, stage):
        if state['module'] == stage:
            return
        if STAGES.index(stage) != STAGES.index(state['module'])+1:
            raise ValueError('nonsequential_module_transition')
        previous = state['module']
        state['module'] = stage
        state['history'].append(stage)
        state['cursor'] = 0
        self.pipeline_save(state)
        pages=[self.pipeline_page(pid) for pid in state.get('pages',[])]
        key={'NORMALIZE':'clean_body','TRIAGE':'triage','EVIDENCE_EXTRACTION':'extraction','SEMANTIC_CODING':'coding'}.get(previous)
        completed=(len(state['queries']) if previous=='DISCOVERY' else
                   len(pages)+len(state['failures']) if previous=='PAGE_CAPTURE' else
                   sum(key in page for page in pages) if key else len(pages))
        reasons={}
        for failure in state.get('failures',[]):
            reasons[failure['reason']]=reasons.get(failure['reason'],0)+1
        atomic_json(self.pipeline_folder()/'module-summary.json', {
            'module_id':previous,'batch_id':state['batch_id'],'completed_count':completed,
            'valid_page_count':sum(page.get('triage',{}).get('relevant',False) for page in pages),
            'failed_count':len(state.get('failures',[])),'failure_reasons':reasons,'next_module':stage,
            'output_directory':self.pipeline_folder().relative_to(self.root).as_posix(),'checkpoint':utc_now()})
        self.event('module_completed', module=previous, next_module=stage,
                   completed_count=completed, batch_id=state['batch_id'])
        self.checkpoint('SEARCH', last_action='模块完成：'+previous+'；下一模块：'+stage)

    def pipeline_current(self, *modules):
        formal = self.state()  # Original release/state validation remains authoritative.
        state = self.pipeline_read()
        if not state or state['task_run_id'] != formal['task_run_id'] or state['module'] not in modules:
            raise ValueError('batch_command_requires_current_module')
        if formal['phase'] != 'SEARCH':
            raise ValueError('batch_command_requires_collection_phase')
        return state

    def batch_next(self):
        formal = self.state()
        state = self.pipeline_read()
        if formal['phase'] != 'SEARCH':
            return self.next()
        if state and state['module']=='EVIDENCE_AUDIT' and len(state['committed_queries'])<len(state['queries']):
            return self.batch_packet(state)
        if state and state['module'] != 'EVIDENCE_AUDIT':
            if state['task_run_id'] != formal['task_run_id']:
                raise ValueError('batch_task_conflict')
            return self.batch_packet(state)
        task = self._next(all_pending=True)
        queries = task.get('assigned_queries', [])
        if not queries:
            return task
        if state and queries[0]['plan_id'] == state['plan_id']:
            # Only the registered retry policy can create new executions here.
            prior = {q['execution_id'] for q in state['queries']}
            if any(q['execution_id'] in prior for q in queries):
                raise ValueError('collection_commit_incomplete')
        batch_id = 'B-'+digest([formal['task_run_id'],[q['execution_id'] for q in queries]])[:24]
        state = {'schema_version':'sequential-batch-1','task_run_id':formal['task_run_id'],
            'batch_id':batch_id,'plan_id':queries[0]['plan_id'],'module':'DISCOVERY','cursor':0,
            'history':['DISCOVERY'],'queries':queries,'pages':[],'failures':[],
            'committed_queries':[],'expanded':{},'checkpoint':utc_now()}
        self.pipeline_save(state)
        save(self.pipeline_folder()/('plan-'+batch_id+'.json'),state)
        return self.batch_packet(state)

    def batch_packet(self, state):
        cfg = config()
        stage = state['module']
        packet = {'status':'ACTION_REQUIRED','continuation':'AUTO_CONTINUE','phase':stage,'module':stage,
                  'batch_id':state['batch_id'],'task_run_id':state['task_run_id'],
                  'counts':{'queries':len(state['queries']),'pages':len(state['pages']),
                            'failed_items':len(state['failures'])}}
        if stage == 'DISCOVERY':
            pending = [q for q in state['queries'] if not (self.tool_folder(q['execution_id'])/'search.json').exists()]
            if not pending:
                self.pipeline_stage(state,'PAGE_CAPTURE')
                return self.batch_packet(state)
            selected = pending[:cfg['query_window_size']]
            for query in selected:
                folder = self.tool_folder(query['execution_id'])
                if not (folder/'started.json').exists():
                    save(folder/'started.json',{'execution':query,'started_at':utc_now(),'task_run_id':state['task_run_id']})
            return {**packet,'action':'record-search-batch','assigned_queries':[
                {k:q[k] for k in ('query_id','execution_id','query','source_category_target')} for q in selected],
                'counts':{**packet['counts'],'completed_queries':len(state['queries'])-len(pending),
                          'remaining_queries':len(pending)}}
        if stage == 'PAGE_CAPTURE':
            items = self.capture_queue(state)
            return {**packet,'action':'record-pages-batch' if items else 'finish-capture',
                    'items':items[:cfg['page_capture_batch_size']], 'counts':{**packet['counts'],'remaining_pages':len(items)}}
        if stage == 'NORMALIZE':
            return {**packet,'action':'normalize-batch'}
        if stage in ('TRIAGE','EVIDENCE_EXTRACTION','SEMANTIC_CODING'):
            key = {'TRIAGE':'triage','EVIDENCE_EXTRACTION':'extraction','SEMANTIC_CODING':'coding'}[stage]
            items = []
            for page_id in state['pages']:
                page = self.pipeline_page(page_id)
                if key in page:
                    continue
                if stage != 'TRIAGE' and not page['triage']['relevant']:
                    continue
                if stage != 'TRIAGE' and page.get('research_lane') == 'context_only':
                    continue
                if stage == 'SEMANTIC_CODING' and not page.get('extraction'):
                    continue
                # Exact-text reuse only after a real prior judgment with the same
                # released rules and content layer; near matches are hints only.
                other_id=page.get('duplicate_of')
                if stage!='TRIAGE' and page.get('similarity_hint')==1.0 and other_id:
                    other=self.pipeline_page(other_id)
                    if (key in other and other['triage']['content_layer']==page['triage']['content_layer']
                            and other['analysis_fingerprint']==page['analysis_fingerprint']
                            and (key!='coding' or other.get('extraction')==page.get('extraction'))):
                        page[key]=other[key]
                        atomic_json(self.pipeline_page_path(page_id),page)
                        cost_record(self.root,'automatic-cache-'+key+'-'+page_id,{'cache_analysis_hits':1})
                        continue
                    if (key not in other and other.get('triage',{}).get('relevant')
                            and other['triage']['content_layer']==page['triage']['content_layer']
                            and other['analysis_fingerprint']==page['analysis_fingerprint']):
                        continue  # Analyze its earlier master first, not the same text twice.
                view = compact_view(page['clean_body'],[self.state_place(state)],cfg['page_view_chars'])
                item = {'page_id':page_id,'url':page['facts']['final_url'],'title':page['facts']['page_title'],
                        'domain':page['domain'],'analysis_fingerprint':page['analysis_fingerprint']}
                if stage == 'SEMANTIC_CODING':
                    text='\n'.join(str(i)+': '+u['text'] for i,u in enumerate(page['extraction']))
                    item['view'] = text[:cfg['page_view_chars']]
                    item['unit_count'] = len(page['extraction'])
                    item['has_more'] = len(text)>cfg['page_view_chars']
                else:
                    item['view'] = view['text']
                    item['has_more'] = view['has_more']
                    item['offset'] = view['offset']
                    if page.get('duplicate_of'):
                        item['duplicate_of'] = page['duplicate_of']
                        item['similarity_hint'] = page['similarity_hint']
                        if page['similarity_hint']==1.0:
                            item['view']='Same retained text as '+page['duplicate_of']+'. Verify this source classification separately.'
                items.append(item)
            if not items:
                if stage=='TRIAGE' and self.action_dispatch_enabled() and self.expand_collection_coverage(state):
                    return self.batch_packet(state)
                self.pipeline_stage(state,STAGES[STAGES.index(stage)+1])
                return self.batch_packet(state)
            limit = cfg['semantic_batch_size'] if stage == 'SEMANTIC_CODING' else cfg['triage_batch_size']
            return {**packet,'action':'record-'+key+'-batch',
                    'items':bounded(items,limit,cfg['max_agent_visible_chars']),
                    'counts':{**packet['counts'],'remaining_items':len(items)}}
        return {**packet,'action':'commit-collection'}

    def state_place(self, state):
        if 'place' not in state:
            state['place'] = self.state()['place']
            self.pipeline_save(state)
        return state['place']

    def expand_collection_coverage(self, state):
        """Read retained discovery results before expensive judgement modules.

        This is NOT a formal count or a new query/round authorization. Once
        retained routes are exhausted, the original audit/planner must decide
        the next round; no fabricated dimension-gap audit is generated here.
        """
        from retrieval_controls import load_retrieval_config
        from source_identity import canonical_url
        minimum=load_retrieval_config()['research_targets']['minimum_deduplicated_relevant_pages']
        pages=[read(p) for p in self.pipeline_folder().glob('page-P-*.json')]
        relevant=[p for p in pages if p.get('triage',{}).get('relevant')]
        upper_bound=min(len({canonical_url(p['facts']['final_url']) for p in relevant}),
                        len({content_hash(p.get('clean_body',p['facts']['page_body'])) for p in relevant}))
        if upper_bound>=minimum:return False
        from math import ceil
        from source_identity import strict_url_identity
        cfg=config()
        gap=minimum-upper_bound
        attempts=len(pages)+len(list((self.root/'staging/tool-facts').glob('*/operational/failed-*.json')))
        yield_rate=upper_bound/max(1,attempts)
        wave_cap=2*cfg['page_capture_batch_size']  # Scheduling load only, not a research threshold.
        wave_limit=min(wave_cap,ceil(gap/yield_rate) if yield_rate else wave_cap)
        seen={strict_url_identity(p['facts'][field]) for p in pages for field in ('request_url','final_url')}
        seen.update(f['url'] for f in state['failures'])
        queries=state['queries']
        pools={}
        for query in queries:
            eid=query['execution_id']; search=read(self.tool_folder(eid)/'search.json')
            restricted=any(f.get('reason') in {'blocked','login_required','captcha','rate_limited','forbidden',
                'restriction','permission_denied'} for f in (read(p) for p in self.tool_folder(eid).glob('failed-*.json')))
            if search['status']!='completed' or restricted:continue
            pools[eid]=search['urls']
        cursor=state.get('expansion_cursor',0)%max(1,len(queries))
        selected=0
        idle=0
        while queries and selected<wave_limit and idle<len(queries):
            eid=queries[cursor]['execution_id']
            cursor=(cursor+1)%len(queries)
            pool=pools.get(eid,[])
            before=state['expanded'].get(eid,cfg['initial_pages_per_query'])
            index=before
            while index<len(pool) and pool[index] in seen:index+=1
            if index<len(pool):
                seen.add(pool[index]);selected+=1;index+=1;idle=0
            else:idle+=1
            if index>before:state['expanded'][eid]=index
        state['expansion_cursor']=cursor
        if selected and self.capture_queue(state):
            state['module']='PAGE_CAPTURE'
            state['history'].append('PAGE_CAPTURE')
            state['collection_coverage']={'observed_relevant_upper_bound':upper_bound,'required_pages':minimum,
                'remaining_gap':gap,'observed_yield':yield_rate,'wave_limit':wave_limit,
                'expanded_candidate_count':selected,
                'formal_gate_passed':False,'reason':'read_remaining_discovered_pages_before_coding'}
            self.pipeline_save(state)
            self.event('collection_coverage_expansion',batch_id=state['batch_id'],**state['collection_coverage'])
            self.checkpoint('SEARCH',last_action='页面覆盖尚不足，继续读取已发现的公开候选；未启动正式评分')
            return True
        self.pipeline_save(state)
        return False

    def pipeline_page_path(self, page_id):
        import re
        if not re.fullmatch(r'P-[a-f0-9]{24}',page_id):
            raise ValueError('invalid_page_selector')
        return self.pipeline_folder()/('page-'+page_id+'.json')

    def pipeline_page(self, page_id):
        return read(self.pipeline_page_path(page_id))

    def capture_queue(self, state):
        from source_identity import strict_url_identity
        cfg = config()
        saved=[read(path) for path in self.pipeline_folder().glob('page-P-*.json')]
        done = {strict_url_identity(page['facts'][field]) for page in saved for field in ('request_url','final_url')}
        done |= {f['url'] for f in state['failures']}
        items = []
        seen = set(done)
        for query in state['queries']:
            path = self.tool_folder(query['execution_id'])/'search.json'
            search = read(path)
            if search['status'] != 'completed':
                continue
            initial = state['expanded'].get(query['execution_id'],cfg['initial_pages_per_query'])
            candidates = search['urls'][:initial]
            for url in candidates:
                if url in seen:
                    own_completed=any(page['execution_id']==query['execution_id'] and
                                      strict_url_identity(page['facts']['request_url'])==url for page in saved)
                    if not own_completed and url not in {f['url'] for f in state['failures']}:
                        cost_record(self.root,'url-cache-'+state['batch_id']+'-'+digest([query['execution_id'],url]),{'cache_page_hits':1})
                    continue
                seen.add(url)
                items.append({'execution_id':query['execution_id'],'query_id':query['query_id'],'url':url})
        return items

    def record_search_batch(self, records):
        state = self.pipeline_current('DISCOVERY')
        cfg = config()
        if not isinstance(records,list) or not 1 <= len(records) <= cfg['query_window_size']:
            raise ValueError('search_batch_size_invalid')
        known = {q['execution_id'] for q in state['queries']}
        errors = []
        for item in records:
            eid = item.get('execution_id') if isinstance(item,dict) else None
            try:
                if not isinstance(item,dict):raise ValueError('search_record_required')
                if eid not in known or not (self.tool_folder(eid)/'started.json').exists():
                    raise ValueError('query_not_dispatched')
                facts = {k:v for k,v in item.items() if k!='execution_id'}
                with self.pipeline_adapter():
                    self.record_search(eid,**facts,defer_complete=True)
                cost_record(self.root,'search-'+eid,{'native_web_search_calls':1,
                    'search_result_count':read(self.tool_folder(eid)/'search.json')['returned_results']})
            except (ValueError,TypeError) as exc:
                from workflow_errors import raise_if_storage_error
                raise_if_storage_error(exc)
                errors.append({'execution_id':eid,'error':str(exc)})
        return {**self.batch_packet(state),'soft_errors':errors,
                'continuation':'SOFT_RETRY' if errors else 'AUTO_CONTINUE'}

    def record_pages_batch(self, records):
        state = self.pipeline_current('PAGE_CAPTURE')
        cfg = config()
        if not isinstance(records,list) or not 1 <= len(records) <= cfg['page_capture_batch_size']:
            raise ValueError('capture_batch_size_invalid')
        from source_identity import strict_url_identity
        from public_network import normalize_tool_trace
        errors = []
        for item in records:
            try:
                if not isinstance(item,dict):raise ValueError('page_facts_required')
                allowed = {'execution_id','request_url','final_url','page_title','page_body','retrieved_at',
                    'body_format','tool_name','tool_call_id','result_ref','access_status','error_type',
                    'actual_fetch_method','adapter_receipt'}
                if set(item)-allowed:raise ValueError('capture_is_factual_only')
                eid = item['execution_id']
                url = strict_url_identity(item['request_url'])
                old = next((self.pipeline_page(pid) for pid in state['pages']
                            if strict_url_identity(self.pipeline_page(pid)['facts']['request_url'])==url),None)
                if old:
                    if item.get('page_body') != old['facts']['page_body']:
                        raise ValueError('captured_body_changed_requires_explicit_new_observation')
                    cost_record(self.root,'cached-page-'+state['batch_id']+'-'+old['page_id'],{'cache_page_hits':1})
                    continue
                assigned = {(v['execution_id'],v['url']) for v in self.capture_queue(state)}
                expected_id=('P-'+digest([state['task_run_id'],url,content_hash(item['page_body'])])[:24]
                             if isinstance(item.get('page_body'),str) else None)
                saved_path=self.pipeline_page_path(expected_id) if expected_id else None
                saved=read(saved_path) if saved_path and saved_path.exists() else None
                restoring=(saved and saved['execution_id']==eid and
                           eid in {q['execution_id'] for q in state['queries']})
                if (eid,url) not in assigned and not restoring:raise ValueError('page_not_in_capture_batch')
                if item.get('error_type'):
                    with self.pipeline_adapter():
                        self.page_failure(eid,url,item['error_type'])
                    state['failures'].append({'execution_id':eid,'url':url,'reason':item['error_type']})
                    self.pipeline_save(state)
                    cost_record(self.root,'failed-read-'+digest([eid,url]),{'page_read_calls':1})
                    continue
                facts = {k:v for k,v in item.items() if k not in ('execution_id','adapter_receipt')}
                if saved and 'retrieved_at' not in facts:
                    facts['retrieved_at']=saved['facts']['retrieved_at']
                from host_adapters import imported_page, page_identity
                if item.get('adapter_receipt'):
                    from artifact_provenance import verify_artifact_writer
                    receipt=(self.root/item['adapter_receipt']).resolve()
                    if not receipt.is_relative_to(self.root/'staging/helpers/operational'):
                        raise ValueError('adapter_receipt_path_invalid')
                    from protected_runtime import operational_origin
                    if operational_origin(self.p['state'],receipt)!='prepare_host_input.py' or read(receipt)!=facts:
                        raise ValueError('adapter_receipt_binding_mismatch')
                    page_identity(facts.get('actual_fetch_method'),facts.get('tool_name'))
                else:
                    facts=imported_page(facts,adapter_id=facts.get('actual_fetch_method','host_web_fetch'))
                if not facts.get('page_body','').strip() or not facts.get('page_title','').strip():
                    raise ValueError('actual_body_and_title_required')
                facts.setdefault('retrieved_at',saved['facts']['retrieved_at'] if saved else utc_now())
                facts.setdefault('body_format','text/plain')
                facts.setdefault('access_status','full')
                if facts['access_status'] not in ('full','partial'):raise ValueError('unreadable_page_must_be_failure')
                trace = {'schema_version':'tool-page-trace-1',**{k:facts.get(k,'') for k in
                    ('tool_name','tool_call_id','result_ref','request_url','final_url','page_title','retrieved_at','body_format')}}
                normalize_tool_trace(trace,url=facts['request_url'],final_url=facts['final_url'],retrieved_at=facts['retrieved_at'])
                page_id = 'P-'+digest([state['task_run_id'],url,content_hash(facts['page_body'])])[:24]
                page = {'page_id':page_id,'execution_id':eid,'facts':facts}
                path = self.pipeline_page_path(page_id)
                if path.exists():
                    prior=read(path)
                    if prior['execution_id']!=eid or prior['facts']!=facts:raise ValueError('raw_page_conflict')
                else:save(path,page)
                # Each page survives a later item failure or process interruption.
                if page_id not in state['pages']:state['pages'].append(page_id)
                self.pipeline_save(state)
                cost_record(self.root,'page-'+page_id,{'page_read_calls':1,'successful_pages':1,
                    'raw_page_bytes':len(facts['page_body'].encode('utf8'))})
            except (ValueError,TypeError,KeyError) as exc:
                from workflow_errors import raise_if_storage_error
                raise_if_storage_error(exc)
                errors.append({'url':item.get('request_url','') if isinstance(item,dict) else '', 'error':str(exc)})
        return {**self.batch_packet(state),'soft_errors':errors,
                'continuation':'SOFT_RETRY' if errors else 'AUTO_CONTINUE'}

    def expand_candidates(self, execution_id, reason):
        state = self.pipeline_current('PAGE_CAPTURE')
        if reason not in {'unavailable','duplicate','irrelevant','insufficient_information'}:
            raise ValueError('expansion_reason_required')
        if execution_id not in {q['execution_id'] for q in state['queries']}:
            raise ValueError('unknown_query')
        search = read(self.tool_folder(execution_id)/'search.json')
        if search['status'] != 'completed':raise ValueError('restricted_query_cannot_expand')
        current = state['expanded'].get(execution_id,config()['initial_pages_per_query'])
        state['expanded'][execution_id] = min(len(search['urls']), current+config()['candidate_urls_per_query'])
        self.pipeline_save(state)
        return self.batch_packet(state)

    def finish_capture(self):
        state = self.pipeline_current('PAGE_CAPTURE')
        if self.capture_queue(state):raise ValueError('capture_batch_still_pending')
        self.pipeline_stage(state,'NORMALIZE')
        return self.batch_packet(state)

    def normalize_batch(self):
        state = self.pipeline_current('NORMALIZE')
        protocol = [content_hash((ROOT/'assets'/name).read_text(encoding='utf8')) for name in
                    ('semantic-quantification-codebook.json','evaluation-protocol.json')]
        previous = []
        for pid in state['pages']:
            page = self.pipeline_page(pid)
            if 'clean_body' not in page:
                # Domain parsing through the single released URL API.
                from source_identity import strict_url_identity, trusted_domain
                url = strict_url_identity(page['facts']['final_url'])
                domain = trusted_domain(url)
                body = clean_body(page['facts']['page_body'],page['facts']['body_format'],domain)
                page.update(domain=domain,clean_body=body,analysis_fingerprint=digest([
                    content_hash(page['facts']['page_body']),config()['triage_protocol_version'],protocol,self.state_place(state)]))
                matches = [(similarity(body,p['clean_body']),p['page_id']) for p in previous]
                if matches and max(matches)[0] >= .92:
                    score, other = max(matches)
                    page.update(duplicate_of=other,similarity_hint=score)
                    cost_record(self.root,'duplicate-'+pid,{'near_duplicate_hints':1})
                atomic_json(self.pipeline_page_path(pid),page)
                cost_record(self.root,'compact-'+pid,{'compact_page_bytes':len(body.encode('utf8'))})
            previous.append(page)
        self.pipeline_stage(state,'TRIAGE')
        return self.batch_packet(state)

    def read_page_view(self, page_id, offset=0):
        state = self.pipeline_current('TRIAGE','EVIDENCE_EXTRACTION','SEMANTIC_CODING')
        if page_id not in state['pages'] or offset < 0:raise ValueError('invalid_page_view_selector')
        page = self.pipeline_page(page_id)
        text=('\n'.join(str(i)+': '+u['text'] for i,u in enumerate(page.get('extraction',[])))
              if state['module']=='SEMANTIC_CODING' else page['clean_body'])
        return {'status':'ready','phase':state['module'],'action':'continue_current_batch',
                'view':compact_view(text,limit=config()['page_view_chars'],offset=offset)}

    def record_judgments_batch(self, kind, records):
        stage = {'triage':'TRIAGE','extraction':'EVIDENCE_EXTRACTION','coding':'SEMANTIC_CODING'}[kind]
        state = self.pipeline_current(stage)
        cfg = config()
        limit = cfg['semantic_batch_size'] if kind=='coding' else cfg['triage_batch_size']
        if not isinstance(records,list) or not 1 <= len(records) <= limit:raise ValueError('judgment_batch_size_invalid')
        errors=[]
        from source_capture import SOURCE_CATEGORIES, CONTENT_LAYERS
        from content_blocks import BLOCK_TYPES
        from pre_admission_audit import ENTITY_LEVELS
        from dimension_framework import DIMENSION_NAMES
        for item in records:
            try:
                if not isinstance(item,dict):raise ValueError('judgment_record_required')
                if set(item)!={'page_id','analysis_fingerprint','decision'}:raise ValueError('judgment_fields_invalid')
                pid=item['page_id']
                if pid not in state['pages']:raise ValueError('page_not_in_current_batch')
                page=self.pipeline_page(pid);decision=item['decision']
                if item['analysis_fingerprint']!=page['analysis_fingerprint']:raise ValueError('stale_analysis_fingerprint')
                if kind in page:
                    if page[kind]!=decision:raise ValueError('completed_judgment_conflict')
                    cost_record(self.root,'cache-'+kind+'-'+pid,{'cache_analysis_hits':1});continue
                if kind=='triage':
                    required={'relevant','source_category','content_layer','entity_level','promotion_status'}
                    if not isinstance(decision,dict) or set(decision)-{'classification_basis'}!=required:raise ValueError('triage_fields_invalid')
                    if type(decision['relevant']) is not bool or decision['source_category'] not in SOURCE_CATEGORIES or decision['content_layer'] not in BLOCK_TYPES & CONTENT_LAYERS or decision['entity_level'] not in ENTITY_LEVELS or decision['promotion_status'] not in ('suspected','not_suspected','unknown'):
                        raise ValueError('triage_enum_invalid')
                    from content_blocks import USER_UNITS
                    if decision['content_layer'] in USER_UNITS:
                        from host_adapters import validate_user_basis
                        from host_receipts import analyze_body
                        visible,_,_=analyze_body(page['facts']['page_body'].encode('utf8'),page['facts']['body_format'])
                        validate_user_basis(decision.get('classification_basis'),visible)
                    page['research_lane'] = ('scoring_candidate' if decision['relevant']
                        and decision['content_layer'] in USER_UNITS else 'context_only')
                elif kind=='extraction':
                    if not page['triage']['relevant']:raise ValueError('irrelevant_page_cannot_extract')
                    if not isinstance(decision,list):raise ValueError('excerpts_array_required')
                    from host_receipts import analyze_body
                    visible,_,_=analyze_body(page['facts']['page_body'].encode('utf8'),page['facts']['body_format'])
                    for unit in decision:
                        if set(unit)!={'text'} or not isinstance(unit['text'],str) or not unit['text'].strip() or visible.count(unit['text'])!=1:
                            raise ValueError('excerpt_must_be_unique_original_visible_text')
                        scope=page['triage'].get('classification_basis',{}).get('user_content_excerpt')
                        if scope is not None and unit['text'] not in scope:
                            raise ValueError('excerpt_outside_observed_user_content')
                else:
                    if not isinstance(decision,list) or len(decision)!=len(page['extraction']):raise ValueError('coding_unit_count_mismatch')
                    if any(set(unit)!={'primary_dimension'} or unit['primary_dimension'] not in DIMENSION_NAMES for unit in decision):
                        raise ValueError('canonical_dimension_required_no_scores')
                    cost_record(self.root,'semantic-'+pid,{'semantic_pages_processed':1,'evidence_units_generated':len(decision)})
                page[kind]=decision
                atomic_json(self.pipeline_page_path(pid),page)
            except (ValueError,TypeError,KeyError) as exc:
                from workflow_errors import raise_if_storage_error
                raise_if_storage_error(exc)
                errors.append({'page_id':item.get('page_id','') if isinstance(item,dict) else '', 'error':str(exc)})
        return {**self.batch_packet(state),'soft_errors':errors,'continuation':'SOFT_RETRY' if errors else 'AUTO_CONTINUE'}

    def commit_collection(self):
        state=self.pipeline_current('EVIDENCE_AUDIT')
        for query in state['queries']:
            eid=query['execution_id']
            if eid in state['committed_queries']:continue
            for pid in state['pages']:
                page=self.pipeline_page(pid)
                if page['execution_id']!=eid:continue
                triage=page['triage']
                evidence=[{**unit,**code} for unit,code in zip(page.get('extraction',[]),page.get('coding',[]))]
                with self.pipeline_adapter():
                    result=self.record_page(eid,**page['facts'],**triage,evidence=evidence)
                if result.get('continuation')!='AUTO_CONTINUE':return result
            with self.pipeline_adapter():
                self.complete_query(eid)
            state['committed_queries'].append(eid)
            self.pipeline_save(state)
        # All research gates, semantic algorithms and formal writes are unchanged.
        result=self.advance()
        if result.get('phase')=='SEARCH':return self.batch_next()
        return result
