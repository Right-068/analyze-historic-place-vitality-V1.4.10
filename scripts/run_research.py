"""Single host-neutral workflow entry. Delegate all formal writes to released CLIs.

The host supplies observed search/page results and research judgements. This
module never searches by itself, assigns scores, or writes canonical ledgers.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import os
import re
from pathlib import Path
import subprocess
import sys
import uuid
import strict_json as json
from process_lock import ProcessFileLock
from runtime_dispatch import DispatchMixin
from tool_batch import ToolBatchMixin, legacy_adapter_only
from batch_pipeline import BatchPipelineMixin
from action_dispatch import ActionDispatchMixin

SKILL = Path(__file__).resolve().parents[1]


def safe_filename_component(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    if not name: raise ValueError('empty_filename_component')
    return name[:100]
PATHS = {
 'state':'运行状态.json','writer':'protected_artifact_write_ledger.jsonl','release':'release.json',
 'capture':'capture/source-capture-manifest.jsonl','snapshots':'capture/source_snapshots',
 'search':'derived/search.csv','refreshed':'derived/refreshed-search.csv',
 'sources':'canonical/active-source-ledger.csv','raw':'canonical/active-evidence-ledger.csv',
 'canonical_sources':'canonical/canonical-source-ledger.csv','canonical_evidence':'canonical/canonical-evidence-ledger.csv',
 'corrections':'canonical/correction-events.jsonl','locator':'audit/locator.json','collision':'audit/collision.json',
 'admission':'audit/admission.json','semantic':'derived/semantic-evidence.csv','formal':'derived/formal-evidence.csv',
 'reviewed':'derived/reviewed-evidence.csv','review_changes':'audit/review-changes.csv',
 'queue':'staging/coding-queue.jsonl','audit':'audit/evidence.json','freeze':'truth_freeze_manifest.json',
 'platform':'derived/platform-scores.json','scores':'derived/composite-scores.json','truth':'derived/report-truth.json',
 'narrative':'staging/report-narrative.json','report_narrative':'derived/report-narrative.json',
 'report_data':'derived/report-data.json','limitations':'staging/limitations.csv','formal_limitations':'derived/limitations.csv',
 'preflight':'audit/preflight.json','validation':'audit/validation.json','errors':'audit/validation-errors.json',
 'seal':'deliverable-manifest.json',
 'score_gate':'score-gate.json',
}


def read(path):
    from storage_contract import read_staging_json
    return read_staging_json(path)


def rows(path):
    if not Path(path).exists(): return []
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def immutable(path, value, *, jsonl=False):
    """Owned staging only. Hash-named submissions are not proof of source truth."""
    raw = (''.join(json.dumps(x,ensure_ascii=False,sort_keys=True)+'\n' for x in value) if jsonl
           else json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
    from storage_contract import prepare_staging_payload
    raw=prepare_staging_payload(path,raw)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()!=raw: raise ValueError('staging_input_conflict:'+path.name)
        return
    from storage_contract import write_integrity_artifact
    write_integrity_artifact(path,raw,transaction_id='orchestrator-staging')


class Workflow(ActionDispatchMixin, DispatchMixin, ToolBatchMixin, BatchPipelineMixin):
    def __init__(self, root):
        self.root=Path(root).resolve()
        if self.root == SKILL or self.root.is_relative_to(SKILL):
            raise ValueError('run_directory_must_be_outside_skill')
        self.root.mkdir(parents=True,exist_ok=True)
        self.p={k:self.root/v for k,v in PATHS.items()}

    def call(self, script, *arguments, allowed=(0,)):
        # Fixed script names and argv lists; never execute web text or shell strings.
        command=[sys.executable,'-X','utf8','-B',str(SKILL/'scripts'/script),*map(str,arguments)]
        result=subprocess.run(command,cwd=self.root,text=True,encoding='utf8',capture_output=True)
        try: payload=json.loads(result.stdout)
        except ValueError: payload={'message':result.stdout[-12000:], 'diagnostic':result.stderr[-6000:]}
        if result.returncode not in allowed:
            raise WorkflowError(script,result.returncode,payload)
        return payload

    def state(self):
        return self.call('manage_run_state.py','show','--state',self.p['state'])

    def auth(self): return ['--state',self.p['state'],'--writer-ledger',self.p['writer']]

    def checkpoint(self, phase, **extra):
        args=['checkpoint','--state',self.p['state'],'--phase',phase,'--last-action','编排器完成前置阶段并提交检查点']
        for key,value in extra.items():args += ['--'+key.replace('_','-'),value]
        return self.call('manage_run_state.py',*args)

    def initialize(self, place, target=None, query_context=None):
        if self.p['state'].exists():
            self._initialize_state(place, target, query_context)
            return self.execute_next_action()
        from check_runtime_environment import doctor
        from workflow_errors import classify_exception
        from storage_contract import write_integrity_artifact, fsync_directory
        import tempfile
        capability=None
        committed=False
        try:
            capability=doctor(self.root)
            if not capability['dependency_check']['ready']: raise ValueError('dependency_missing')
            if not capability['storage']['operational_storage_available']: raise ValueError('operational_storage_unavailable')
            existing=list(self.root.iterdir())
            if any(p.name!='initialization-diagnostic.json' for p in existing):
                raise ValueError('initialization_target_not_empty')
            # Same-volume staging: no formal state is visible until the first
            # complete action and all bootstrap transactions have committed.
            with tempfile.TemporaryDirectory(prefix='.'+self.root.name+'-initializing-',dir=self.root.parent) as temporary:
                staged=Workflow(temporary)
                staged._initialize_state(place,target,query_context)
                packet=staged.execute_next_action()
                if not packet.get('action_id') or packet.get('operation')!='record-search-batch':
                    raise ValueError('initialization_first_action_invalid')
                from protected_runtime import verify_before_action
                verify_before_action(staged.p['state'])
                if list(staged.root.rglob('*.tmp')):
                    raise ValueError('initialization_pending_temporary')
                # Both diagnostic and empty destination are owned by this init.
                for path in existing: path.unlink()
                self.root.rmdir()
                os.replace(staged.root,self.root)
                committed=True
                fsync_directory(self.root.parent)
                def rebind(value):
                    if isinstance(value,dict): return {k:rebind(v) for k,v in value.items()}
                    if isinstance(value,list): return [rebind(v) for v in value]
                    if isinstance(value,str) and value.startswith(str(staged.root)):
                        return str(self.root)+value[len(str(staged.root)):]
                    return value
                return rebind(packet)
        except Exception as exc:
            if committed:
                return {**classify_exception(exc),'initialization_committed':True,
                        'action':'resume_verified_checkpoint','capability':capability}
            result={**classify_exception(exc,initializing=True),'capability':capability}
            if result['error_code']=='internal_invariant_failure': result['process_status']='failed'
            # Diagnostics are not a state file and cannot authorize recovery.
            self.root.mkdir(parents=True,exist_ok=True)
            diagnostic=self.root/'initialization-diagnostic.json'
            if diagnostic.exists(): diagnostic.unlink()
            try: write_integrity_artifact(diagnostic,json.dumps(result,ensure_ascii=False).encode('utf8'))
            except OSError: pass  # No writable destination: return diagnostic to host.
            return result

    def _initialize_state(self, place, target=None, query_context=None):
        """Internal state bootstrap, not a public host execution mode."""
        context_path=self.root/'staging/context/operational/query-context.json'
        if query_context is not None:
            if set(query_context)!={'alias','building','local-term'} or any(
                not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values)
                for values in query_context.values()):raise ValueError('invalid_query_context')
        if self.p['state'].exists():
            state=self.state()
            if state['place']!=place or target and target!=state['target_confidence']:
                raise ValueError('existing_run_identity_conflict')
            if query_context is not None:
                if not context_path.exists():raise ValueError('query_context_must_be_set_at_initialization')
                immutable(context_path,query_context)
            return self.next()
        if query_context is not None:immutable(context_path,query_context)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        name=safe_filename_component(place)
        stamp=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')
        args=['init','--state',self.p['state'],'--task-run-id','RUN-'+uuid.uuid4().hex,
              '--place',place,'--skill-root',SKILL,'--release-manifest',self.p['release'],
              '--writer-ledger',self.p['writer'],'--detailed-log',self.root/f'详细运行日志_{name}_{stamp}.txt']
        if target:args+=['--target-confidence',target]
        self.call('manage_run_state.py',*args)
        self.checkpoint('SEARCH')
        return self.next()

    def next(self):
        return self.packet(self._next())

    def _next(self, *, all_pending=False):
        s=self.state()
        result={'status':'ACTION_REQUIRED','phase':s['phase'],'task_run_id':s['task_run_id'],
                'state_status':s['status'],'next_action':s['next_action']}
        if s['status']=='complete':
            self.call('manage_run_state.py','assert-final','--state',self.p['state'])
            return {**result,'status':'complete','artifacts':s['artifacts']}
        if s.get('terminal_failure_code'):
            return {**result,'status':'blocked','terminal_failure_code':s['terminal_failure_code']}
        if (s['phase']=='EVIDENCE_AUDIT' or s['phase']=='SEMANTIC_QUANTIFICATION'
                and s.get('audit_repair_required')) and self.p['audit'].exists():
            pending=self.audit_repair_status()
            if pending:return {**result,**pending}
        if s['phase']=='INITIALIZE': self.checkpoint('SEARCH'); return self.next()
        if s['phase']=='SEARCH':
            iteration=s['iteration_round']+1 if s.get('artifacts',{}).get('evidence_audit') else 0
            plan=self.root/f'staging/plan-{iteration}.csv'
            if not plan.exists():
                args=['--state',self.p['state'],'--task-run-id',s['task_run_id'],'--place',s['place'],
                      '--iteration-round',iteration,'--output',plan]
                context_path=self.root/'staging/context/operational/query-context.json'
                if context_path.exists():
                    context=read(context_path)
                    if set(context)!={'alias','building','local-term'}:raise ValueError('invalid_query_context')
                    for flag,values in context.items():
                        if not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values):
                            raise ValueError('invalid_query_context')
                        for value in values:args+=['--'+flag,value]
                if iteration:
                    args+=['--audit',self.p['audit'],'--history',self.p['refreshed']]
                    if read(self.p['audit']).get('collection_blockers'):
                        args+=['--gap','公开来源覆盖']
                planning=self.call('build_query_plan.py',*args)
                if not plan.exists():
                    return {**result,'status':'blocked','continuation':'HARD_BLOCKER',
                        'action':'inspect_audit_planner_contract','reason':'no_executable_dimension_target',
                        'planning_diagnostic':planning,
                        'note':'No plan was committed. Do not invent targets, drop queries or advance to scoring.'}
            from execution_facts import load_registered_queries
            from retrieval_controls import execution_schema_context_from_state
            registered=load_registered_queries(execution_schema_context_from_state(s,state_path=self.p['state']))
            planned=rows(plan)
            for row in planned:
                committed=registered.get((row['plan_id'],row['query_id']))
                if committed is None or any(committed.get(k)!=v for k,v in row.items()):
                    raise ValueError('active_query_plan_changed')
            history=rows(self.p['search'])
            if self.p['search'].exists():
                from artifact_provenance import verify_artifact_writer
                verify_artifact_writer(state_path=self.p['state'],writer_ledger_path=self.p['writer'],
                    output_role='search_log',output_path=self.p['search'])
            expose=('plan_id','query_id','query','exact_query','polarity','purpose','source_category_target','query_dimension_targets')
            pending=[]
            from execution_semantics import retryable_error
            from retrieval_controls import load_retrieval_config
            cap=load_retrieval_config()['budgets']['maximum_controlled_retries_per_intent']
            for r in planned:
                attempts=sorted((h for h in history if h['query_id']==r['query_id']),key=lambda h:int(h['retry_number']))
                number=int(attempts[-1]['retry_number'])+1 if attempts else 0
                reason=retryable_error(attempts[-1]) if attempts else ''
                if attempts and (not reason or number>cap):continue
                identity=hashlib.sha256((s['task_run_id']+'|'+r['plan_id']+'|'+r['query_id']+'|'+str(number)).encode()).hexdigest()[:24]
                pending.append({**{k:r.get(k,'') for k in expose},'execution_id':'EX-'+identity,
                    'retry_number':number,'retry_reason':reason,
                    'original_execution_id':attempts[0]['execution_id'] if attempts else '',
                    'retry_interval_policy':'host_managed_backoff' if attempts else ''})
            window=self.dispatch_window(s,planned,pending,history)
            if all_pending:
                window={**window,'assigned_queries':pending}
            collection_checked=any(e['kind']=='empty_collection_checked' and e.get('plan_id')==planned[0]['plan_id']
                                   for e in self.events()) if planned else False
            if planned and not pending and not self.p['raw'].exists() and collection_checked:
                return {**result,'query_plan':str(plan),**window,'continuation':'USER_INPUT_REQUIRED',
                    'action':'confirm_research_context_or_public_entry',
                    'reason':'full_registered_plan_completed_without_admissible_material',
                    'note':'All registered queries and permitted retries are recorded, but no material was admitted. '
                           'Retain all facts; request research-context clarification before changing the fixed plan. '
                           'This is neither an exhaustion ruling nor permission to score.'}
            return {**result,'action':'search_and_read' if pending else 'advance','query_plan':str(plan),**window,
                'input_contract':str(SKILL/'references/orchestrator.md'),
                'note':'Only actual host search/page responses may be submitted; no automatic background execution.'}
        if s['phase']=='REPORT_BUILD':
            return {**result,'action':'write_evidence_grounded_narrative','report_truth':str(self.p['truth']),
                    'narrative_input':str(self.p['narrative']),'limitations_input':str(self.p['limitations'])}
        return {**result,'action':'advance'}

    @legacy_adapter_only
    def submit(self, batch):
        if self.state()['phase']!='SEARCH':raise ValueError('batch_requires_search_phase')
        if not isinstance(batch,dict) or set(batch)!={'observations','pages'}:
            raise ValueError('batch_requires_observations_and_pages')
        if not isinstance(batch['observations'],list) or not isinstance(batch['pages'],list):
            raise ValueError('batch_arrays_required')
        from execution_facts import SOURCE_BINDING_FIELDS
        from source_capture import validate_initial_tool_input
        # Reject malformed/machine-owned page inputs before recording any execution.
        for page in batch['pages']:
            if not isinstance(page,dict) or set(page)!={'execution_id','record','response_text','candidate_source','candidate_evidence'}:
                raise ValueError('page_input_fields_invalid')
            validate_initial_tool_input(page['record'])
            if set(page['record']) & (set(SOURCE_BINDING_FIELDS)|{'task_run_id','query_id','query','capture_id'}):
                raise ValueError('page_execution_binding_is_machine_owned')
            if not isinstance(page['response_text'],str) or not isinstance(page['candidate_evidence'],list):
                raise ValueError('page_content_input_invalid')
            forbidden={'schema_version','task_run_id','source_id','evidence_id','capture_id','candidate_source_id',
                'candidate_evidence_id','used_for_scoring','formal_scoring_eligible','included_in_platform_score',
                'score_scope','sentiment','sentiment_score','stance_strength','weighted_score','tendency','dedup_group'}
            for candidate in [page['candidate_source'],*page['candidate_evidence']]:
                if not isinstance(candidate,dict) or forbidden.intersection(candidate) or any(
                        key.startswith(('semantic_','review_','aggregation_')) for key in candidate):
                    raise ValueError('candidate_machine_or_scoring_fields_forbidden')
        sha=hashlib.sha256(json.dumps(batch,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        folder=self.root/'staging/batches'/sha/'operational'
        immutable(folder/'batch.json',batch)
        immutable(folder/'observations.jsonl',batch['observations'],jsonl=True)
        self.call('execution_facts.py',*self.auth(),'--observations',folder/'observations.jsonl','--output',self.p['search'])
        executions={r['execution_id']:r for r in rows(self.p['search'])}
        sources,evidence=[],[]
        # Validate recovery state once while holding the outer workflow lock.
        # New captures still pass the unchanged official capture CLI individually.
        captured_by_id={}
        if batch['pages'] and self.p['capture'].exists():
            from source_capture import verify_capture_manifest
            from artifact_provenance import verify_artifact_writer
            verify_artifact_writer(state_path=self.p['state'],writer_ledger_path=self.p['writer'],
                output_role='source_capture_manifest',output_path=self.p['capture'])
            if verify_capture_manifest(self.p['capture'])['status']!='valid':
                raise ValueError('capture_resume_verification_failed')
            captured_by_id={r['capture_id']:r for line in self.p['capture'].read_text(encoding='utf8').splitlines()
                if line for r in [json.loads(line)]}
        for index,page in enumerate(batch['pages']):
            execution=executions.get(page['execution_id'])
            if execution is None:raise ValueError('page_execution_not_recorded')
            record={**page['record'],**{k:execution[k] for k in SOURCE_BINDING_FIELDS},
                    **{k:execution[k] for k in ('task_run_id','query_id','query')}}
            record['capture_id']='CAP-'+sha[:24]+'-'+str(index)
            immutable(folder/f'page-{index}.json',record)
            immutable(folder/f'response-{index}.json',{'response_text':page['response_text']})
            captured=captured_by_id.get(record['capture_id'])
            if captured is not None and (captured.get('execution_id')!=execution['execution_id'] or
                    captured.get('raw_response_sha256')!=hashlib.sha256(page['response_text'].encode()).hexdigest()):
                raise ValueError('capture_resume_identity_conflict')
            if captured is None:
                captured=self.call('source_capture.py','capture',*self.auth(),'--manifest',self.p['capture'],
                    '--snapshots-dir',self.p['snapshots'],'--record',folder/f'page-{index}.json',
                    '--tool-result',folder/f'response-{index}.json')['result']
                captured_by_id[record['capture_id']]=captured
            sid='CS-'+sha[:16]+'-'+str(index)
            source=dict(page['candidate_source'])
            source.update(schema_version='candidate-source-1',task_run_id=execution['task_run_id'],
                          candidate_source_id=sid,capture_id=captured['capture_id'])
            sources.append(source)
            for j,unit in enumerate(page['candidate_evidence']):
                e=dict(unit);e.update(schema_version='candidate-evidence-1',task_run_id=execution['task_run_id'],
                                     candidate_evidence_id='CE-'+sha[:16]+f'-{index}-{j}',candidate_source_id=sid)
                evidence.append(e)
        immutable(folder/'sources.jsonl',sources,jsonl=True)
        immutable(folder/'evidence.jsonl',evidence,jsonl=True)
        return {'status':'captured','batch':sha,'page_count':len(sources),'next_action':'continue_search_or_advance'}

    def admission_args(self):
        return ['--capture-manifest',self.p['capture'],'--canonical-sources',self.p['canonical_sources'],
            '--canonical-evidence',self.p['canonical_evidence'],'--corrections',self.p['corrections'],
            '--active-sources',self.p['sources'],'--active-evidence',self.p['raw'],
            '--locator-audit',self.p['locator'],'--collision-audit',self.p['collision'],'--admission-audit',self.p['admission']]

    def admit_candidates(self,sources,evidence):
        """Use the unchanged admission writer; isolate bad candidates, not its gates."""
        rejected={e.get('candidate_source_id') for e in self.events() if e['kind']=='candidate_rejected'}
        sources=[s for s in sources if s['candidate_source_id'] not in rejected]
        if not sources:return
        ids={s['candidate_source_id'] for s in sources}
        evidence=[e for e in evidence if e['candidate_source_id'] in ids]
        has_evidence={e['candidate_source_id'] for e in evidence}
        from content_blocks import USER_UNITS
        context_sources={s['candidate_source_id'] for s in sources
                         if s.get('content_layer') and s['content_layer'] not in USER_UNITS}
        for source in sources:
            if source['candidate_source_id'] not in has_evidence | context_sources:
                self.event('candidate_deferred',candidate_source_id=source['candidate_source_id'],reason='no_evidence_judgement_yet')
        sources=[s for s in sources if s['candidate_source_id'] in has_evidence | context_sources]
        if not sources:return
        sha=hashlib.sha256(json.dumps([sources,evidence],sort_keys=True).encode()).hexdigest()
        folder=self.root/'staging/admission-inputs'/sha/'operational'
        immutable(folder/'sources.jsonl',sources,jsonl=True);immutable(folder/'evidence.jsonl',evidence,jsonl=True)
        try:
            self.call('pre_admission_audit.py',*self.auth(),*self.admission_args(),
                '--candidate-sources',folder/'sources.jsonl','--candidate-evidence',folder/'evidence.jsonl')
        except WorkflowError as exc:
            detail=json.dumps(exc.payload,ensure_ascii=False)
            local_codes=('candidate_', 'capture_not_readable','missing_page_title','invalid_entity_level',
                'invalid_captured_source_category','invalid_content_layer','missing_place_name','invalid_primary_dimension',
                'missing_original_visible_text','invalid_explicit_locator','ambiguous_evidence_text',
                'invalid_unit_type','unit_content_layer_conflict','unit_block_type_conflict',
                'page_body_content_identity_conflict','user_block_locator_required','body_visibility_unconfirmed',
                'native_numeric_candidate_conflict','native_numeric_capture_missing','invalid_publication_date')
            if not any(code in detail for code in local_codes):raise
            if len(sources)>1:
                middle=len(sources)//2
                self.admit_candidates(sources[:middle],evidence);self.admit_candidates(sources[middle:],evidence)
            else:
                self.event('candidate_rejected',candidate_source_id=sources[0]['candidate_source_id'],detail=detail[-1800:])
                self.soft_error('candidate_not_admitted',action='continue_next_candidate',detail=detail)

    def common(self):
        return ['--sources',self.p['sources'],'--raw-evidence',self.p['raw'],'--semantic-evidence',self.p['semantic'],
            '--formal-evidence',self.p['formal'],'--search-log',self.p['refreshed'],'--dimension-audit',self.p['audit'],
            '--platform-scores',self.p['platform'],'--scores',self.p['scores']]

    def lineage(self):
        return ['--source-capture-manifest',self.p['capture'],'--canonical-sources',self.p['canonical_sources'],
            '--canonical-evidence',self.p['canonical_evidence'],'--corrections',self.p['corrections'],
            '--locator-audit',self.p['locator'],'--collision-audit',self.p['collision'],'--admission-audit',self.p['admission']]

    def semantic_args(self,s):
        return ['--sources',self.p['sources'],'--codebook',SKILL/'assets/semantic-quantification-codebook.json',
                '--protocol',SKILL/'assets/evaluation-protocol.json','--place',s['place'],'--apply-auto-codes',*self.auth()]

    def coding_source(self):
        """A review from an earlier evidence batch cannot drop new evidence."""
        if not self.p['reviewed'].exists():return self.p['semantic']
        from artifact_provenance import verify_artifact_writer
        writer=verify_artifact_writer(state_path=self.p['state'],writer_ledger_path=self.p['writer'],
            output_role='reviewed_evidence',output_path=self.p['reviewed'])
        origin=writer.get('inputs',{}).get('evidence',{})
        if origin.get('sha256')!=hashlib.sha256(self.p['semantic'].read_bytes()).hexdigest():
            raise ValueError('review_input_is_stale: apply trusted decisions to the current semantic evidence before finalizing')
        return self.p['reviewed']

    def outputs(self,s):
        name=safe_filename_component(s['place']);date=s['research_cutoff'].replace('-','')
        return dict(report=self.root/f'历史文化活力分析报告_{name}_{date}.docx',
                    workbook=self.root/f'网络检索与量化编码_{name}_{date}.xlsx',
                    rules_workbook=self.root/f'非量化文本量化评价规则_{name}_{date}.xlsx')

    def advance(self, *, finalize_coding=False):
        pipeline=self.pipeline_read()
        if pipeline and self.state()['phase']=='SEARCH' and pipeline['module']!='EVIDENCE_AUDIT':
            return self.batch_packet(pipeline)
        return self.packet(self._advance(finalize_coding=finalize_coding))

    def audit_inputs_digest(self):
        return __import__('runtime_dispatch').digest({
            k:hashlib.sha256(self.p[k].read_bytes()).hexdigest() if self.p[k].exists() else None
            for k in ('search','refreshed','sources','raw','formal')})

    def audit_repair_status(self):
        from artifact_provenance import verify_artifact_writer
        from runtime_dispatch import classify_audit_failure, digest, config
        verify_artifact_writer(state_path=self.p['state'],writer_ledger_path=self.p['writer'],
            output_role='evidence_audit',output_path=self.p['audit'])
        audit=read(self.p['audit'])
        if audit.get('status')!='invalid':return None
        classification,action=classify_audit_failure(audit)
        fingerprint=digest(sorted(audit.get('errors') or ['missing_audit_errors']))
        inputs=self.audit_inputs_digest()
        attempts=[e for e in self.events() if e['kind']=='audit_repair_attempt'
            and e['last_audit_failure_fingerprint']==fingerprint]
        unchanged=any(e['input_fingerprint']==inputs for e in attempts)
        state=self.state()
        resuming=state['phase']=='SEMANTIC_QUANTIFICATION' and state.get('audit_repair_required')
        blocked=classification=='fatal' or not resuming and (unchanged or len(attempts)>=config()['local_repair_attempts'])
        return {'status':'blocked' if blocked else 'repair_required',
            'continuation':'HARD_BLOCKER' if blocked else 'SOFT_RETRY',
            'action':'restore_verified_checkpoint' if blocked else 'repair_audit_errors',
            'reason':'audit_repair_no_progress' if unchanged else 'audit_invalid',
            'audit_repair_required':True,'audit_repair_action':action,
            'audit_repair_attempt':len(attempts),'last_audit_failure_fingerprint':fingerprint,
            'input_fingerprint':inputs,'classification':classification,
            'command':'repair-audit' if not blocked else None,'audit_errors':audit.get('errors',[])}

    def repair_audit(self):
        state=self.state()
        resuming=state['phase']=='SEMANTIC_QUANTIFICATION' and state.get('audit_repair_required')
        if state['phase']!='EVIDENCE_AUDIT' and not resuming:raise ValueError('audit_repair_requires_audit_phase')
        pending=self.audit_repair_status()
        if not pending:return self.next()
        if pending['continuation']=='HARD_BLOCKER':return self.packet(pending)
        if not resuming:
            self.event('audit_repair_attempt',last_audit_failure_fingerprint=pending['last_audit_failure_fingerprint'],
                input_fingerprint=pending['input_fingerprint'],audit_repair_action=pending['audit_repair_action'])
        if pending['audit_repair_action']=='refresh_scored_counts':
            if not resuming:self.call('manage_run_state.py','begin-audit-repair','--state',self.p['state'],'--audit',self.p['audit'])
            self.call('refresh_search_log.py','--search-log',self.p['search'],'--sources',self.p['sources'],
                '--evidence',self.p['formal'],'--output',self.p['refreshed'],*self.auth())
            self.checkpoint('EVIDENCE_AUDIT')
        # A page-only shortage is re-audited by the existing feedback adapter;
        # only its newly registered needs_iteration result authorizes SEARCH.
        return self.packet(self._advance(reaudit=True))

    def _advance(self, *, finalize_coding=False, reaudit=False):
        # At most one pass through the fixed phases. Retrieval/review/narrative
        # are explicit host yields, not invented results or automatic scoring.
        for _ in range(9):
            s=self.state();phase=s['phase']
            if s['status']=='complete' or s.get('terminal_failure_code'):return self.next()
            if phase=='SEMANTIC_QUANTIFICATION' and s.get('audit_repair_required'):
                return self.audit_repair_status()
            if phase=='INITIALIZE':self.checkpoint('SEARCH');return self.next()
            if phase=='SEARCH':
                if not list((self.root/'staging/batches').glob('*/operational/sources.jsonl')):return self.next()
                self.checkpoint('EVIDENCE_BUILD')
            elif phase=='EVIDENCE_BUILD':
                sources=[];evidence=[]
                admitted=set()
                if self.p['canonical_sources'].exists():
                    from artifact_provenance import verify_artifact_writer
                    verify_artifact_writer(state_path=self.p['state'],writer_ledger_path=self.p['writer'],
                        output_role='canonical_source_ledger',output_path=self.p['canonical_sources'])
                    admitted={r['source_capture_id'] for r in rows(self.p['canonical_sources'])}
                for path in sorted((self.root/'staging/batches').glob('*/operational/sources.jsonl')):
                    from storage_contract import read_staging_bytes
                    incoming=[json.loads(x) for x in read_staging_bytes(path).decode('utf8').splitlines() if x]
                    incoming=[r for r in incoming if r['capture_id'] not in admitted]
                    selected={r['candidate_source_id'] for r in incoming}
                    sources+=incoming
                    evidence += [r for x in read_staging_bytes(path.with_name('evidence.jsonl')).decode('utf8').splitlines()
                                 if x for r in [json.loads(x)] if r['candidate_source_id'] in selected]
                batch_sha=hashlib.sha256(json.dumps([sources,evidence],sort_keys=True).encode()).hexdigest()
                folder=self.root/'staging/admission-inputs'/batch_sha/'operational'
                immutable(folder/'sources.jsonl',sources,jsonl=True);immutable(folder/'evidence.jsonl',evidence,jsonl=True)
                if sources:self.admit_candidates(sources,evidence)
                from retrieval_controls import load_retrieval_config
                from source_identity import page_entity_id_for_source
                minimum=load_retrieval_config()['research_targets']['minimum_deduplicated_relevant_pages']
                page_count=len({page_entity_id_for_source(r) for r in rows(self.p['sources']) if r.get('is_relevant')=='true'})
                plan=self.root/f"staging/plan-{s['iteration_round']+1 if s.get('artifacts',{}).get('evidence_audit') else 0}.csv"
                performed={r['query_id'] for r in rows(self.p['search'])}
                still_unexecuted=any(r['query_id'] not in performed for r in rows(plan))
                from execution_semantics import retryable_error
                cap=load_retrieval_config()['budgets']['maximum_controlled_retries_per_intent']
                for query in rows(plan):
                    attempts=[h for h in rows(self.p['search']) if h['query_id']==query['query_id']]
                    if attempts:
                        last=max(attempts,key=lambda h:int(h['retry_number']))
                        still_unexecuted |= bool(retryable_error(last) and int(last['retry_number'])<cap)
                if still_unexecuted:
                    self.call('manage_run_state.py','resume-collection','--state',self.p['state'])
                    return {**self.next(),'current_page_entities':page_count,'minimum_page_target':minimum}
                if not self.p['raw'].exists():
                    self.event('empty_collection_checked',plan_id=rows(plan)[0]['plan_id'])
                    self.call('manage_run_state.py','resume-collection','--state',self.p['state'])
                    return self.next()
                self.checkpoint('SEMANTIC_QUANTIFICATION')
            elif phase=='SEMANTIC_QUANTIFICATION':
                self.call('quantify_text_semantics.py','--input',self.p['raw'],'--output',self.p['semantic'],
                    '--audit-output',self.root/'audit/semantic.json',*self.semantic_args(s))
                if not finalize_coding and not self.action_dispatch_enabled():
                    return {'status':'ACTION_REQUIRED','phase':phase,'action':'resolve_coding',
                        'semantic_evidence':str(self.p['semantic']),
                        'review_required_count':sum(r.get('semantic_review_status')=='review_required'
                            for r in rows(self.p['semantic'])),
                        'next_action':'review_if_authorized_then_finalize_coding',
                        'note':'No judgement is confirmed by the orchestrator; unresolved records retain released non-scoring rules.'}
                # Trusted decisions are optional. The host supplies only a decision
                # file and trusted external key via the separate review command.
                source=self.coding_source()
                self.call('quantify_text_semantics.py','--input',source,'--output',self.p['formal'],
                    '--output-role','formal_evidence','--audit-output',self.root/'audit/formal-semantic.json',*self.semantic_args(s))
                self.call('refresh_search_log.py','--search-log',self.p['search'],'--sources',self.p['sources'],
                    '--evidence',self.p['formal'],'--output',self.p['refreshed'],*self.auth())
                self.checkpoint('EVIDENCE_AUDIT')
            elif phase=='EVIDENCE_AUDIT':
                if self.p['audit'].exists() and not reaudit:
                    pending=self.audit_repair_status()
                    if pending:return pending
                reaudit=False
                audit_result=self.call('audit_evidence.py','--sources',self.p['sources'],'--evidence',self.p['formal'],
                    '--search-log',self.p['refreshed'],'--target-confidence',s['target_confidence'],
                    '--iteration-feedback','--output',self.p['audit'],*self.auth(),allowed=(0,1,2))
                if not self.p['audit'].is_file():
                    raise WorkflowError('audit_evidence.py',1,audit_result)
                if read(self.p['audit']).get('status')=='invalid':
                    pending=self.audit_repair_status()
                    self.event('audit_repair_required',**{k:pending[k] for k in
                        ('audit_repair_required','audit_repair_action','audit_repair_attempt',
                         'last_audit_failure_fingerprint','input_fingerprint')})
                    return pending
                s=self.call('manage_run_state.py','sync-audit','--state',self.p['state'],'--audit',self.p['audit'])
                if s['audit_status']=='needs_iteration':
                    self.checkpoint('SEARCH');return self.next()
                freeze_roles={'search_log':'refreshed','source_capture_manifest':'capture','canonical_source_ledger':'canonical_sources',
                    'canonical_evidence_ledger':'canonical_evidence','correction_event_ledger':'corrections','locator_audit':'locator',
                    'collision_audit':'collision','pre_admission_audit':'admission','source_ledger':'sources','raw_evidence':'raw',
                    'semantic_evidence':'semantic','formal_evidence':'formal','evidence_audit':'audit'}
                if self.p['reviewed'].exists():freeze_roles.update(reviewed_evidence='reviewed',review_change_ledger='review_changes')
                args=[]
                for role,key in freeze_roles.items():args+=['--artifact',role+'='+str(self.p[key])]
                self.call('artifact_provenance.py','freeze',*self.auth(),'--output',self.p['freeze'],*args)
                self.checkpoint('SCORE',truth_freeze=self.p['freeze'])
            elif phase=='SCORE':
                self.call('quantify_text_semantics.py','--input',self.p['formal'],'--output',self.p['formal'],
                    '--output-role','formal_evidence','--platform-scores',self.p['platform'],'--search-log',self.p['refreshed'],
                    '--dimension-audit',self.p['audit'],*self.semantic_args(s))
                self.call('calculate_scores.py','--input',self.p['platform'],'--output',self.p['scores'],
                    '--truth-freeze',self.p['freeze'],*self.auth())
                self.call('artifact_provenance.py','extend-scores',*self.auth(),'--manifest',self.p['freeze'],
                    '--artifact','platform_scores='+str(self.p['platform']),'--artifact','scoring_output='+str(self.p['scores']))
                common=self.common()
                # compile truth does not accept semantic-evidence.
                i=common.index('--semantic-evidence');common[i:i+2]=[]
                self.call('compile_report_truth.py',*common,*self.auth(),'--truth-freeze',self.p['freeze'],
                    '--protocol',SKILL/'assets/evaluation-protocol.json','--codebook',SKILL/'assets/semantic-quantification-codebook.json',
                    '--output',self.p['truth'])
                self.call('preflight_validate.py',*self.common(),*self.lineage(),*self.auth(),
                    '--truth-freeze',self.p['freeze'],'--report-truth',self.p['truth'],'--output',self.p['preflight'])
                self.checkpoint('REPORT_BUILD',preflight=self.p['preflight'])
                return self.next()
            elif phase=='REPORT_BUILD':
                if not self.p['narrative'].is_file() or not self.p['limitations'].is_file():return self.next()
                gates=[*self.auth(),'--truth-freeze',self.p['freeze'],'--preflight',self.p['preflight']]
                self.call('assemble_report_data.py',*self.auth(),'--report-truth',self.p['truth'],
                    '--narrative-input',self.p['narrative'],'--narrative-output',self.p['report_narrative'],
                    '--template',SKILL/'assets/report-data-template.json','--output',self.p['report_data'])
                self.call('artifact_provenance.py','promote',*self.auth(),'--input',self.p['limitations'],
                    '--output',self.p['formal_limitations'],'--role','limitations')
                out=self.outputs(s)
                self.call('build_research_workbook.py',*gates,'--sources',self.p['sources'],'--evidence',self.p['formal'],
                    '--search-log',self.p['refreshed'],'--limitations',self.p['formal_limitations'],'--scores',self.p['scores'],
                    '--place',s['place'],'--retrieval-date',s['research_cutoff'],'--output',out['workbook'])
                self.call('build_semantic_rules_workbook.py',*gates,'--codebook',SKILL/'assets/semantic-quantification-codebook.json',
                    '--protocol',SKILL/'assets/evaluation-protocol.json','--task-run-id',s['task_run_id'],'--place',s['place'],
                    '--retrieval-date',s['research_cutoff'],'--output',out['rules_workbook'])
                self.call('build_docx_report.py',*gates,'--input',self.p['report_data'],'--sources',self.p['sources'],
                    '--scores',self.p['scores'],'--output',out['report'])
                self.checkpoint('VALIDATE')
            elif phase=='VALIDATE':
                out=self.outputs(s);common=self.common()
                for flag in ('--formal-evidence','--platform-scores'):
                    i=common.index(flag);value=common[i+1];common[i:i+2]=[]
                    if flag=='--formal-evidence':common+=['--evidence',value]
                args=[]
                for key,path in out.items():args+=['--'+key.replace('_','-'),path]
                self.call('validate_deliverables.py',*args,*common,*self.lineage(),*self.auth(),
                    '--report-data',self.p['report_data'],'--report-truth',self.p['truth'],'--report-narrative',self.p['report_narrative'],
                    '--detailed-log',s['detailed_log'],'--skill-root',SKILL,'--release-manifest',self.p['release'],
                    '--truth-freeze',self.p['freeze'],'--preflight',self.p['preflight'],
                    '--output',self.p['validation'],'--error-manifest',self.p['errors'],allowed=(0,1))
                self.call('manage_run_state.py','record-validation','--state',self.p['state'],
                    '--validation',self.p['validation'],'--error-manifest',self.p['errors'])
                if read(self.p['validation']).get('errors'):
                    return {**self.next(),'status':'blocked','validation':read(self.p['validation'])}
                self.call('manage_run_state.py','finish','--state',self.p['state'],'--docx',out['report'],
                    '--xlsx',out['workbook'],'--rules-xlsx',out['rules_workbook'],'--detailed-log',s['detailed_log'],
                    '--validation',self.p['validation'],'--validation-error-manifest',self.p['errors'],
                    '--delivery-provenance',self.p['seal'])
                return self.next()
            else:return self.next()
        return self.next()


class WorkflowError(ValueError):
    def __init__(self,script,code,payload):
        self.payload={'status':'blocked','script':script,'exit_code':code,'details':payload}
        super().__init__(json.dumps(self.payload,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['doctor','init','next','execute-next-action','submit','advance','probe','review','finalize-coding','finalize-coding-without-review','contract',
        'begin-query','record-search','record-page','page-failure','complete-query','rebuild-batch','repair-audit',
        'batch-next','record-search-batch','record-pages-batch','finish-capture','normalize-batch',
        'record-triage-batch','record-extraction-batch','record-coding-batch','commit-collection','expand-candidates',
        'page-view','cost-metrics','record-cost'])
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--place')
    parser.add_argument('--target-confidence',choices=['中','中高','高'])
    for flag in ('alias','building','local-term'):
        parser.add_argument('--'+flag,action='append',default=None,help='Existing planner context; repeatable at initialization')
    parser.add_argument('--input',type=Path,help='Host batch, or page metadata for probe')
    parser.add_argument('--tool-result',type=Path,help='Actual response_text JSON for probe')
    parser.add_argument('--trusted-review-key',type=Path,help='Existing publisher-trusted external human key; never generated here')
    parser.add_argument('--execution-id')
    parser.add_argument('--debug',action='store_true',help='Full machine JSON; default is compact agent view')
    parser.add_argument('--agent-view',action='store_true',help='Compact output (default)')
    parser.add_argument('--page-id')
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--body-file',type=Path,help='Actual host response text; not a research summary')
    parser.add_argument('--tool-name',default='web_search')
    parser.add_argument('--result-count',type=int)
    parser.add_argument('--url',action='append',default=[])
    parser.add_argument('--final-url',default='')
    parser.add_argument('--page-title',default='')
    parser.add_argument('--retrieved-at')
    parser.add_argument('--tool-call-id',default='')
    parser.add_argument('--result-ref',default='')
    parser.add_argument('--body-format',default='text/plain')
    parser.add_argument('--access-status',choices=['full','partial'],default='full')
    parser.add_argument('--source-category')
    parser.add_argument('--content-layer',default='page_body')
    parser.add_argument('--entity-level',default='area_direct')
    parser.add_argument('--relevant',action='store_true')
    parser.add_argument('--promotion-status',default='unknown')
    parser.add_argument('--evidence-file',type=Path,help='Only observed excerpts and classification judgements, not machine fields')
    parser.add_argument('--kind',default='page')
    parser.add_argument('--outcome',default='completed')
    parser.add_argument('--error-type',default='')
    parser.add_argument('--login-triggered',action='store_true')
    parser.add_argument('--restriction-triggered',action='store_true')
    args=parser.parse_args()
    if args.command=='doctor':
        from check_runtime_environment import doctor
        print(json.dumps(doctor(args.run_dir),ensure_ascii=False)); return 0
    try:
        w=Workflow(args.run_dir)
        lock=(w.root.parent/('.'+w.root.name+'-init-lock') if args.command=='init' else w.root/'orchestration-lock')
        from contextlib import ExitStack
        with ExitStack() as locks:
            locks.enter_context(ProcessFileLock(lock))
            if args.command=='init' and w.p['state'].exists():
                locks.enter_context(ProcessFileLock(w.root/'orchestration-lock'))
            if w.action_dispatch_enabled() and args.command not in (
                    'init','next','execute-next-action','contract','probe','review','cost-metrics','record-cost',
                    'finalize-coding-without-review'):
                raise ValueError('action_dispatch_active_use_execute_next_action')
            if w.pipeline_read() and args.command in ('begin-query','record-search','record-page','submit','complete-query','rebuild-batch','page-failure'):
                raise ValueError('use_current_batch_command_in_sequential_mode')
            if args.command=='contract':
                from execution_schema import field_contract, observation_template
                from source_capture import ACCESS_STATUSES, CONTENT_LAYERS, SOURCE_CATEGORIES
                from pre_admission_audit import UNIT_TYPES, ENTITY_LEVELS
                result={'schema_version':'orchestrator-input-2','observations':field_contract(),
                    'observation_template':observation_template(),
                    'page_fields':['execution_id','record','response_text','candidate_source','candidate_evidence'],
                    'record_enums':{'access_status':sorted(ACCESS_STATUSES),'source_type':sorted(SOURCE_CATEGORIES),
                        'content_layer':sorted(CONTENT_LAYERS)},
                    'candidate_enums':{'source_category':sorted(SOURCE_CATEGORIES),'content_layer':sorted(CONTENT_LAYERS),
                        'unit_type':sorted(UNIT_TYPES),'entity_level':sorted(ENTITY_LEVELS)},
                    'candidate_source_template':read(SKILL/'assets/candidate-source-template.jsonl'),
                    'candidate_evidence_template':read(SKILL/'assets/candidate-evidence-template.jsonl'),
                    'default_host_path':['init','execute-next-action'],
                    'batch_configuration':__import__('batch_pipeline').config(),
                    'page_fact_fields':['request_url','final_url','page_title','page_body','retrieved_at'],
                    'optional_host_metadata':['tool_call_id','result_ref'],
                    'judgement_fields':['source_category','content_layer','entity_level','relevant','promotion_status',
                                        'evidence[].text','evidence[].primary_dimension','evidence[].unit_type','evidence[].place_relevance'],
                    'dispatch_configuration':__import__('runtime_dispatch').config(),
                    'continuations':sorted(__import__('runtime_dispatch').CONTINUATIONS)}
            elif args.command=='init':
                if not args.place:raise ValueError('place_required')
                context={flag:getattr(args,flag.replace('-','_')) or [] for flag in ('alias','building','local-term')}
                result=w.initialize(args.place,args.target_confidence,context if any(context.values()) else None)
            elif args.command=='execute-next-action':result=w.execute_next_action(read(args.input) if args.input else None)
            elif args.command=='submit':result=w.submit(read(args.input))
            elif args.command=='begin-query':result=w.begin_query(args.execution_id)
            elif args.command=='record-search':
                result=w.record_search(args.execution_id,args.body_file.read_text(encoding='utf-8-sig'),tool_name=args.tool_name,
                    result_count=args.result_count,urls=args.url,status=args.outcome,error_type=args.error_type,
                    login_triggered=args.login_triggered,restriction_triggered=args.restriction_triggered)
            elif args.command=='record-page':
                result=w.record_page(args.execution_id,request_url=args.url[0] if args.url else '',final_url=args.final_url,
                    page_title=args.page_title,page_body=args.body_file.read_text(encoding='utf-8-sig'),retrieved_at=args.retrieved_at,
                    tool_name=args.tool_name,tool_call_id=args.tool_call_id,result_ref=args.result_ref,body_format=args.body_format,
                    source_category=args.source_category,content_layer=args.content_layer,entity_level=args.entity_level,
                    relevant=args.relevant,promotion_status=args.promotion_status,
                    evidence=read(args.evidence_file) if args.evidence_file else [],kind=args.kind,access_status=args.access_status)
            elif args.command=='page-failure':result=w.page_failure(args.execution_id,args.url[0] if args.url else '',args.error_type)
            elif args.command in ('complete-query','rebuild-batch'):result=w.complete_query(args.execution_id)
            elif args.command=='batch-next':result=w.batch_next()
            elif args.command=='record-search-batch':result=w.record_search_batch(read(args.input))
            elif args.command=='record-pages-batch':result=w.record_pages_batch(read(args.input))
            elif args.command=='finish-capture':result=w.finish_capture()
            elif args.command=='normalize-batch':result=w.normalize_batch()
            elif args.command in ('record-triage-batch','record-extraction-batch','record-coding-batch'):
                result=w.record_judgments_batch(args.command.split('-')[1],read(args.input))
            elif args.command=='commit-collection':result=w.commit_collection()
            elif args.command=='expand-candidates':result=w.expand_candidates(args.execution_id,args.error_type)
            elif args.command=='page-view':result=w.read_page_view(args.page_id,args.offset)
            elif args.command in ('cost-metrics','record-cost'):
                from cost_metrics import record as record_cost, summary as cost_summary
                if args.command=='record-cost':
                    observation=read(args.input)
                    record_cost(w.root,observation['event_id'],observation['values'])
                result={'status':'ready','action':'continue_current_module','metrics':cost_summary(w.root)}
                from reachability import diagnostics
                from retrieval_controls import research_target_summary
                result['reachability']=diagnostics(w.root,target_units=research_target_summary(
                    target_confidence=w.state().get('target_confidence'))['theoretical_minimum_scored_evidence'])
            elif args.command=='next':
                result=w.execute_next_action() if w.action_dispatch_enabled() else w.batch_next() if w.pipeline_read() else w.next()
            elif args.command=='advance':result=w.advance()
            elif args.command=='repair-audit':result=w.repair_audit()
            elif args.command in ('finalize-coding','finalize-coding-without-review'):
                if w.state()['phase']!='SEMANTIC_QUANTIFICATION':raise ValueError('finalize_coding_requires_semantic_phase')
                result=w.advance(finalize_coding=True)
            elif args.command=='probe':
                from host_receipts import capability
                from execution_facts import SOURCE_BINDING_FIELDS
                sample=read(args.input)
                execution=next((r for r in rows(w.p['search']) if r['execution_id']==sample.get('execution_id')),None)
                if execution:
                    sample.update({k:execution[k] for k in (*SOURCE_BINDING_FIELDS,'task_run_id','query_id','query')})
                result=capability(network_search_available=True if rows(w.p['search']) else None,page_read_available=True,
                    sample_record=sample,response_text=read(args.tool_result).get('response_text'))
            else:
                if not args.input or not args.trusted_review_key:raise ValueError('trusted_review_input_required')
                result=w.call('apply_review_changes.py',*w.auth(),'--evidence',w.p['semantic'],'--decisions',args.input,
                    '--trusted-review-key',args.trusted_review_key,'--output',w.p['reviewed'],'--change-ledger',w.p['review_changes'])
        if result.get('status')=='initialization_failed' or result.get('initialization_committed'):
            print(json.dumps(result,ensure_ascii=False));return 1 if result.get('process_status')=='failed' else 0
        result=w.packet(result)
        from batch_pipeline import agent_view
        from cost_metrics import atomic_json, record as record_cost
        if not args.debug and args.command!='contract' and result.get('continuation')!='COMPLETED':
            atomic_json(w.pipeline_folder()/'machine-state.json',result)
            result=agent_view(result)
        output=json.dumps(result,ensure_ascii=False,indent=2 if args.debug else None)
        record_cost(w.root,'cli-'+uuid.uuid4().hex,{'python_cli_calls':1,'orchestrator_calls':1,
            'bytes_returned_to_agent':len(output.encode('utf8'))})
        print(output);return 0
    except FileNotFoundError as exc:
        print(json.dumps({'process_status':'failed','error_type':type(exc).__name__},ensure_ascii=False),file=sys.stderr)
        return 1
    except OSError as exc:
        from workflow_errors import classify_exception
        initialized='w' in locals() and w.p['state'].exists()
        print(json.dumps(classify_exception(exc,initializing=args.command=='init' and not initialized),ensure_ascii=False));return 0
    except (json.JSONDecodeError,TypeError,KeyError,AttributeError) as exc:
        # Software/input failures are not recoverable workflow observations.
        print(json.dumps({'process_status':'failed','error_type':type(exc).__name__,
                          'error':str(exc)[-3000:]},ensure_ascii=False),file=sys.stderr)
        return 1
    except ValueError as exc:
        text=str(exc)
        from workflow_errors import classify_exception
        classified=classify_exception(exc,initializing=args.command=='init')
        if classified['error_code'] in ('private_confidential_storage_unavailable','dependency_missing',
                'operational_storage_unavailable','credential_storage_forbidden'):
            print(json.dumps(classified,ensure_ascii=False));return 0
        if classified['error_code']=='internal_invariant_failure' and not isinstance(exc,WorkflowError):
            print(json.dumps({'process_status':'failed',**classified},ensure_ascii=False),file=sys.stderr)
            return 1
        if text in ('unknown_continuation','invalid_dispatch_configuration'):
            print(json.dumps({'process_status':'failed','error':text}),file=sys.stderr)
            return 1
        hard=isinstance(exc,WorkflowError) or any(code in text for code in
            ('dispatch_journal_corrupt','dispatch_execution_plan_conflict','active_query_plan_changed',
             'release_integrity','release_file_changed','发布版 Skill 已发生改变','发布版完整性清单',
             'state_log_transaction_','invalid_run_state',
             'protected_artifact_modified_outside_approved_writer',
             'capture_resume_verification_failed','capture_resume_identity_conflict'))
        user=text in ('place_required','existing_run_identity_conflict','trusted_review_input_required')
        result={'status':'blocked' if hard or user else 'retry','error':text[-3000:],
            'continuation':'HARD_BLOCKER' if hard else 'USER_INPUT_REQUIRED' if user else 'SOFT_RETRY',
            'action':'restore_verified_checkpoint' if hard else 'supply_required_information' if user else
                'rebuild_batch_from_recorded_tool_facts_or_continue_next_candidate'}
        if 'w' in locals():
            try:
                with ProcessFileLock(w.root/'orchestration-lock'):
                    w.event('command_error',command=args.command,execution_id=args.execution_id or '',**result)
            except (ValueError,OSError):pass
            if not hard and not user and w.action_dispatch_enabled():
                try:
                    current=w.pipeline_read()
                    if current and w.state()['phase']=='SEARCH':
                        result={**w.issue_action(w.batch_packet(current)), 'continuation':'SOFT_RETRY',
                                'error':text[-1200:]}
                except (ValueError,OSError,KeyError):pass
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
