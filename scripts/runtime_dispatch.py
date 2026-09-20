"""Bounded dispatch over the registered plan; never a second research state machine."""
from pathlib import Path
import hashlib
import strict_json as json
from temporal_fields import utc_now
from storage_contract import write_integrity_artifact

CONFIG = Path(__file__).resolve().parents[1]/'assets/runtime-dispatch.json'
CONTINUATIONS = {'AUTO_CONTINUE','SOFT_RETRY','HARD_BLOCKER','USER_INPUT_REQUIRED','COMPLETED','TURN_CHECKPOINT'}

def config():
    value=json.loads(CONFIG.read_text(encoding='utf8'))
    if not (1 <= value['minimum_window_size'] <= value['default_window_size'] <= value['maximum_window_size']):
        raise ValueError('invalid_dispatch_configuration')
    return value

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def classify_audit_failure(audit):
    """Only explicitly reconstructible derived data may be automatically repaired."""
    import re
    errors=audit.get('errors')
    if not isinstance(errors,list) or not errors or any(not isinstance(e,str) for e in errors):
        return 'fatal','restore_verified_checkpoint'
    count_error=r'search log row \d+: cumulative_scored_evidence_units cannot decrease'
    page_error=r'only \d+ deduplicated relevant searched pages; minimum is \d+'
    if any(re.fullmatch(count_error,e) for e in errors) and all(
            re.fullmatch(count_error,e) or re.fullmatch(page_error,e) for e in errors):
        return 'repairable','refresh_scored_counts'
    if all(re.fullmatch(page_error,e) for e in errors):
        return 'repairable','return_to_search'
    return 'fatal','restore_verified_checkpoint'

class DispatchMixin:
    def events(self):
        directory=self.root/'staging/dispatch/operational'
        previous='';result=[]
        for index,path in enumerate(sorted(directory.glob('event-*.json')),1):
            from storage_contract import read_staging_json
            row=read_staging_json(path)
            signature=row.pop('sha256',None)
            if row.get('sequence')!=index or row.get('previous_sha256')!=previous or signature!=digest(row):
                raise ValueError('dispatch_journal_corrupt')
            previous=signature;result.append({**row,'sha256':signature})
        return result

    def event(self,kind,**detail):
        events=self.events()
        value={'sequence':len(events)+1,'previous_sha256':events[-1]['sha256'] if events else '',
               'at':utc_now(),'kind':kind,**detail}
        value['sha256']=digest(value)
        path=self.root/'staging/dispatch/operational'/f"event-{len(events)+1:08d}.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        raw=(json.dumps(value,ensure_ascii=False,sort_keys=True)+'\n').encode()
        from protected_runtime import write_operational
        if not write_operational(path,raw):
            write_integrity_artifact(path,raw,transaction_id='dispatch-event')
        return value

    def dispatch_window(self,state,planned,pending,history):
        plan_id=planned[0]['plan_id'] if planned else ''
        events=self.events()
        windows=[e for e in events if e['kind']=='window' and e.get('task_run_id')==state['task_run_id']]
        completed={r['execution_id'] for r in history}
        by_execution={r['execution_id']:r for r in pending}
        active=windows[-1] if windows and windows[-1]['plan_id']==plan_id else None
        if active and any(eid not in completed for eid in active['execution_ids']):
            ids=[eid for eid in active['execution_ids'] if eid not in completed]
            if any(eid not in by_execution for eid in ids):raise ValueError('dispatch_execution_plan_conflict')
            assigned=[by_execution[eid] for eid in ids]
        elif pending:
            cfg=config();size=cfg['default_window_size']
            failures={e.get('execution_id') for e in events if e['kind']=='soft_error'}
            done=[w for w in windows if all(eid in completed for eid in w['execution_ids'])]
            recent=done[-max(cfg['healthy_windows_to_expand'],cfg['failed_windows_to_reduce']):]
            unhealthy=[any(eid in failures or any(h['execution_id']==eid and h['status'] in ('failed','blocked','partial') for h in history)
                           for eid in w['execution_ids']) for w in recent]
            if len(unhealthy)>=cfg['failed_windows_to_reduce'] and all(unhealthy[-cfg['failed_windows_to_reduce']:]):
                size=cfg['minimum_window_size']
            elif len(unhealthy)>=cfg['healthy_windows_to_expand'] and not any(unhealthy[-cfg['healthy_windows_to_expand']:]):
                size=cfg['maximum_window_size']
            assigned=pending[:size]
            active=self.event('window',task_run_id=state['task_run_id'],plan_id=plan_id,
                window_id='WIN-'+digest([state['task_run_id'],plan_id,len(windows)+1,[p['execution_id'] for p in assigned]])[:24],
                window_size=size,query_ids=[p['query_id'] for p in assigned],execution_ids=[p['execution_id'] for p in assigned])
        else:
            assigned=[]
        pending_queries={r['query_id'] for r in pending}
        count=sum(r['query_id'] not in pending_queries for r in planned)
        cursor=next((i for i,r in enumerate(planned) if r['query_id'] in pending_queries),len(planned))
        return {'assigned_queries':assigned,'window_id':active['window_id'] if active else '',
            'window_size':active['window_size'] if active else config()['default_window_size'],
            'window_query_ids':[p['query_id'] for p in assigned],
            'plan_total':len(planned),'completed_query_count':count,
            'remaining_query_count':len(pending_queries),'next_cursor':cursor}

    def packet(self,result):
        result=dict(result)
        if 'continuation' not in result:
            if result.get('status')=='complete':result['continuation']='COMPLETED'
            elif result.get('terminal_failure_code'):result['continuation']='HARD_BLOCKER'
            elif result.get('status') in ('invalid','blocked'):result['continuation']='SOFT_RETRY'
            else:result['continuation']='AUTO_CONTINUE'
        if result['continuation'] not in CONTINUATIONS:raise ValueError('unknown_continuation')
        result['host_instruction']=('Deliver only verified artifacts.' if result['continuation']=='COMPLETED' else
            'Checkpoint is retained. Report the exact capability or required input; never invent a workaround.'
            if result['continuation'] in ('HARD_BLOCKER','USER_INPUT_REQUIRED','TURN_CHECKPOINT') else
            'Execute the returned action now while host tools remain available. Progress is not completion. '
            'Persist each action; if the host forcibly ends this turn, resume the same run with next.')
        return result

    def soft_error(self,code,*,execution_id='',action='continue_next_candidate',detail=''):
        previous=sum(e['kind']=='soft_error' and e.get('execution_id')==execution_id and e.get('error_code')==code
                     and e.get('detail')==str(detail)[-1600:] for e in self.events())
        attempts=previous+1
        if attempts>=config()['local_repair_attempts']:action='continue_next_candidate_without_repeating_failed_item'
        self.event('soft_error',execution_id=execution_id,error_code=code,action=action,detail=str(detail)[-1600:])
        return self.packet({'status':'retry','continuation':'SOFT_RETRY','action':action,
            'execution_id':execution_id,'error_code':code,'error_detail':str(detail)[-1600:],
            'local_attempt':attempts,'local_retry_remaining':max(0,config()['local_repair_attempts']-attempts)})
