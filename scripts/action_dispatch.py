"""One resumable host action; contracts derive from the released consumers."""
from pathlib import Path
from tool_batch import read, save
from runtime_dispatch import digest
from cost_metrics import atomic_json
import json


def contract(operation):
    from source_capture import SOURCE_CATEGORIES, CONTENT_LAYERS
    from content_blocks import BLOCK_TYPES
    from pre_admission_audit import ENTITY_LEVELS
    from execution_schema import TOOLS
    from execution_semantics import STATUS_MATRIX
    from dimension_framework import DIMENSION_NAMES
    common = {'envelope': {'action_id': 'returned action_id', 'records': 'array'},
        'batch_rule': 'Submit every assigned item once. Failed items alone are reissued; no re-search of saved facts.',
        'forbidden': ['sentiment_score', 'formal_scoring_eligible', 'weighted_score', 'human_confirmed']}
    if operation == 'record-search-batch':
        return {**common, 'required_fields': ['execution_id','response_text'],
            'optional_fields': ['tool_name','status','error_type','login_triggered','restriction_triggered','search_results','result_count','urls'],
            'enums': {'tool_name': sorted(TOOLS), 'status': sorted(STATUS_MATRIX)},
            'search_results_schema': ['HTTP(S) URL string', {'url':'actual link when available','title':'observed title','snippet':'observed snippet'}],
            'note': 'Discovery only. Submit the retained provider response. Counts and URL-less reasons are derived; arrays, results/items/organic_results and web.results/webPages.value are accepted. Preserve duplicate entries. Optional result_count is a consistency assertion, not a source of counts. Reuse saved responses for format repair; do not search again.'}
    if operation == 'record-pages-batch':
        return {**common, 'required_fields': ['execution_id','request_url','final_url','page_title','page_body'],
            'optional_fields': ['retrieved_at','body_format','tool_name','tool_call_id','result_ref','access_status','actual_fetch_method','adapter_receipt'],
            'failure_fields': ['execution_id','request_url','error_type'],
            'enums': {'access_status':['full','partial'],'body_format':['text/plain','text/html']},
            'fetch_adapters': __import__('host_adapters').FETCH_ADAPTERS,
            'note': 'Only real retained response. Default host import records host_page_read, not an invented product name. Local curl/requests require the official fetch-page helper receipt. No source_type, primary_dimension or rating guesses.'}
    base = {**common, 'required_fields':['page_id','analysis_fingerprint','decision'],
        'read_more': {'action_id':'same action_id','page_view':{'page_id':'assigned page_id','offset':0}}}
    if operation == 'record-triage-batch':
        return {**base, 'decision_fields':['relevant','source_category','content_layer','entity_level','promotion_status'],
            'conditional_field': {'classification_basis': {'author_type':['individual','community_participant'],
                'basis_excerpt':'unique visible author/participation evidence',
                'user_content_excerpt':'unique visible user-content range; exclude editorial surroundings'}},
            'enums': {'relevant':[True,False],'source_category':sorted(SOURCE_CATEGORIES),
                'content_layer':sorted(BLOCK_TYPES & CONTENT_LAYERS),'entity_level':sorted(ENTITY_LEVELS),
                'promotion_status':['unknown','suspected','not_suspected']},
            'note':'Classify actual visible content and author, never query target. User layers require classification_basis. Editorial travel guides and organization text are context_only. Unknown origin stays context_only. An actual comment on an official page may qualify only inside its observed user range.'}
    if operation == 'record-extraction-batch':
        return {**base, 'decision': [{'text':'unique verbatim excerpt in retained visible text'}],
            'empty_decision_allowed': True}
    if operation == 'record-coding-batch':
        return {**base, 'decision':[{'primary_dimension':'one canonical dimension per extracted unit'}],
            'enums': {'primary_dimension':list(DIMENSION_NAMES)},
            'note':'No sentiment/score/review identity. Use original unit order.'}
    return {'note':'Deterministic operation. Call execute-next-action without an input file.'}


class ActionDispatchMixin:
    def action_dispatch_enabled(self):
        return (self.pipeline_folder()/'action-mode.json').exists()

    def action_path(self, action_id):
        import re
        if not isinstance(action_id,str) or not re.fullmatch(r'A-[a-f0-9]{32}',action_id):
            raise ValueError('invalid_action_id')
        return self.pipeline_folder()/('action-'+action_id+'.json')

    @staticmethod
    def action_selector(operation, row):
        if not isinstance(row,dict):raise ValueError('action_record_must_be_object')
        if operation == 'record-search-batch': return row.get('execution_id')
        if operation == 'record-pages-batch':
            from source_identity import strict_url_identity
            return [row.get('execution_id'), strict_url_identity(row.get('request_url') or row.get('url',''))]
        return row.get('page_id')

    def issue_action(self, packet):
        operation=packet.get('action')
        if packet.get('continuation') in ('HARD_BLOCKER','USER_INPUT_REQUIRED','COMPLETED'):
            return packet
        from batch_pipeline import config, agent_view
        from page_views import bounded
        packet=agent_view(packet)
        input_contract=contract(operation)
        field='assigned_queries' if 'assigned_queries' in packet else 'items'
        selected=packet.get(field,[])
        skeleton={**packet,field:[],'action':'execute-next-action','operation':operation,
                  'action_id':'A-'+'0'*32,'input_contract':input_contract}
        available=config()['max_agent_visible_chars']-len(json.dumps(skeleton,ensure_ascii=False))-128
        if selected:
            selected=bounded(selected,len(selected),available)
            packet={**packet,field:selected}
        key = [packet.get('task_run_id'),packet.get('batch_id'),operation,selected]
        action_id='A-'+digest(key)[:32]
        value={'action_id':action_id,'operation':operation,'phase':packet.get('phase'),
            'selectors':[self.action_selector(operation,r) for r in selected], 'packet':packet}
        path=self.action_path(action_id)
        if not path.exists():save(path,value)
        return {**packet,'action':'execute-next-action','operation':operation,
            'action_id':action_id,'input_contract':input_contract}

    def execute_next_action(self, payload=None):
        from temporal_fields import utc_now, timestamp
        import uuid
        from protected_runtime import verify_before_action
        verify_before_action(self.p['state'])
        turn_path = self.pipeline_folder() / 'turn-state.json'
        turn = read(turn_path) if turn_path.exists() else None
        if turn is None or turn.get('turn_status') == 'checkpointed':
            turn = {'turn_id':'TURN-' + uuid.uuid4().hex, 'started_at':utc_now(),
                    'completed_action_ids':[], 'actions_completed':0, 'turn_status':'running',
                    'task_run_id':self.state()['task_run_id'], 'host_auto_resume':'unavailable'}
        result = self._execute_next_action(payload)
        if result.get('continuation') == 'COMPLETED':
            return result
        if payload and 'records' in payload:
            receipt = self.action_path(payload['action_id']).with_suffix('.done.json')
            if receipt.exists() and payload['action_id'] not in turn['completed_action_ids']:
                turn['completed_action_ids'].append(payload['action_id'])
        turn.update(actions_completed=len(turn['completed_action_ids']), module=result.get('phase'),
                    current_action_id=result.get('action_id'), checkpoint_at=utc_now(),
                    recommended_resume_action='execute-next-action', research_status='running')
        # Scheduling slice only. No query or evidence budget is truncated.
        if (turn['actions_completed'] >= 80 or
                (timestamp(utc_now()) - timestamp(turn['started_at'])).total_seconds() >= 2400):
            turn['turn_status'] = 'checkpointed'
            result = {**result, 'status':'TURN_CHECKPOINT', 'continuation':'TURN_CHECKPOINT',
                      'research_status':'running', 'turn_status':'checkpointed',
                      'host_auto_resume':'unavailable', 'recommended_resume_action':'execute-next-action'}
        atomic_json(turn_path, turn)
        return result

    def _execute_next_action(self, payload=None):
        """Host public entry. Known low-level methods remain internal adapters."""
        formal=self.state()
        self.events()  # Resume must reject a damaged dispatch journal, even mid-module.
        mode=self.pipeline_folder()/'action-mode.json'
        if not mode.exists():save(mode,{'task_run_id':formal['task_run_id']})
        if read(mode)['task_run_id']!=formal['task_run_id']:raise ValueError('action_task_conflict')
        if payload is not None:
            if not isinstance(payload,dict) or set(payload) not in ({'action_id','records'},{'action_id','page_view'}):
                raise ValueError('action_envelope_invalid')
            path=self.action_path(payload['action_id']); action=read(path)
            if action['packet'].get('task_run_id')!=formal['task_run_id']:raise ValueError('action_task_conflict')
            op=action['operation']
            if 'page_view' in payload:
                view=payload['page_view']
                if not isinstance(view,dict) or set(view)!={'page_id','offset'} or view['page_id'] not in action['selectors']:
                    raise ValueError('page_view_requires_assigned_page')
                result=self.read_page_view(view['page_id'],view['offset'])
                compact=self.issue_action(action['packet'])
                compact.pop('items',None)
                return {**compact,'view':result['view']}
            records=payload['records']
            if not isinstance(records,list) or not records:raise ValueError('action_records_required')
            spec=contract(op)
            for row in records:
                if not isinstance(row,dict):raise ValueError('action_record_must_be_object')
                required=set(spec.get('required_fields',[]))
                allowed=required | set(spec.get('optional_fields',[]))
                if op=='record-pages-batch' and row.get('error_type'):
                    required=allowed=set(spec['failure_fields'])
                if not required.issubset(row) or set(row)-allowed:
                    raise ValueError('record_fields_must_match_input_contract')
            selectors=[self.action_selector(op,row) for row in records]
            if len({digest(x) for x in selectors})!=len(selectors):raise ValueError('duplicate_action_selector')
            receipt=path.with_suffix('.done.json')
            request_hash=digest(payload)
            if receipt.exists():
                if request_hash!=read(receipt)['input_sha256']:raise ValueError('completed_action_input_conflict')
                return self._execute_next_action()
            # Require the full issued batch; a retry action only contains failed
            # rows. Small tail/character-budget batches are already exact.
            if {digest(x) for x in selectors}!={digest(x) for x in action['selectors']}:
                raise ValueError('submit_complete_assigned_batch_no_manual_fragmentation')
            current=self.pipeline_read()
            attempt=path.with_suffix('.attempt.json')
            if not current or current['batch_id']!=action['packet']['batch_id'] or current['module']!=action['phase']:
                if attempt.exists() and read(attempt)['input_sha256']==request_hash:
                    save(receipt,{'input_sha256':request_hash})
                    return self._execute_next_action()
                raise ValueError('stale_action')
            atomic_json(attempt,{'input_sha256':request_hash})
            if op=='record-search-batch': result=self.record_search_batch(records)
            elif op=='record-pages-batch': result=self.record_pages_batch(records)
            elif op in ('record-triage-batch','record-extraction-batch','record-coding-batch'):
                result=self.record_judgments_batch(op.split('-')[1],records)
            else:raise ValueError('action_does_not_accept_records')
            if not result.get('soft_errors'):save(receipt,{'input_sha256':request_hash})
            else:
                # Successful items are already durable. Reissue current pending
                # items, never require the host to repeat completed web actions.
                return self.issue_action(result)
        # Consume mechanical actions internally. Bounded passes are a scheduling
        # yield, not a research stop or a semantic/audit gate bypass.
        for _ in range(12):
            s=self.state()
            if s['phase']=='SEARCH': packet=self.batch_next()
            elif s['phase']=='SEMANTIC_QUANTIFICATION':packet=self.advance(finalize_coding=True)
            elif s['phase']=='REPORT_BUILD' and self.p['narrative'].is_file() and self.p['limitations'].is_file():
                packet=self.advance(finalize_coding=True)
            else:packet=self.next()
            if packet.get('continuation') in ('HARD_BLOCKER','USER_INPUT_REQUIRED','COMPLETED'):
                return packet
            operation=packet.get('action')
            if operation=='finish-capture':self.finish_capture()
            elif operation=='normalize-batch':self.normalize_batch()
            elif operation=='commit-collection':
                result=self.commit_collection()
                if result.get('continuation') in ('HARD_BLOCKER','USER_INPUT_REQUIRED','COMPLETED'):return result
            elif operation=='advance':
                result=self.advance(finalize_coding=True)
                if result.get('continuation') in ('HARD_BLOCKER','USER_INPUT_REQUIRED','COMPLETED'):return result
            elif operation=='repair_audit_errors':
                result=self.repair_audit()
                if result.get('continuation') in ('HARD_BLOCKER','USER_INPUT_REQUIRED','COMPLETED'):return result
            else:return self.issue_action(packet)
        return {'status':'ready','continuation':'AUTO_CONTINUE','action':'execute-next-action',
                'phase':self.state()['phase'],'reason':'deterministic_batch_checkpoint'}
