"""Stable error-to-workflow mapping, independent of the host product name."""
import errno

def classify_exception(exc, *, initializing=False):
    text=str(exc)
    code=getattr(exc,'detail',{}).get('code','')
    category='internal_invariant_failure'; continuation='HARD_BLOCKER'; action='inspect_internal_failure'
    if any(v in text for v in ('private_artifact_permissions_failed','private_artifact_identity_unavailable','private_confidential_storage_unavailable')):
        category='private_confidential_storage_unavailable'; action='storage_capability_required'
    elif isinstance(exc,(ModuleNotFoundError,ImportError)) or 'dependency_missing' in text or 'ZoneInfoNotFound' in type(exc).__name__:
        category='dependency_missing'; action='install_release_requirements'
    elif isinstance(exc,OSError):
        if getattr(exc,'winerror',None) in (32,33) or exc.errno in (errno.EAGAIN,errno.EBUSY,errno.ETXTBSY):
            category='operational_storage_temporarily_locked'; continuation='SOFT_RETRY'; action='retry_same_storage_transaction'
        else: category='operational_storage_unavailable'; action='storage_capability_required'
    elif any(v in text for v in ('protected_artifact','release_integrity','release_file_changed','发布版','state_log_transaction','invalid_run_state','dispatch_journal_corrupt','active_query_plan_changed','capture_resume_')):
        category='protected_artifact_tamper'; action='restore_verified_checkpoint'
    elif 'operational_storage_unavailable' in text:
        category='operational_storage_unavailable'; action='storage_capability_required'
    elif 'credential_storage_forbidden' in text:
        category='credential_storage_forbidden'; action='remove_credentials_from_tool_input'
    elif text in ('place_required','existing_run_identity_conflict','trusted_review_input_required'):
        category='user_information_required'; continuation='USER_INPUT_REQUIRED'; action='supply_required_information'
    elif any(v in text for v in ('network_transient','timeout','page_unavailable')):
        category='network_transient'; continuation='SOFT_RETRY'; action='continue_allowed_public_candidate'
    elif getattr(exc,'code',None):
        category='protected_artifact_tamper'; action='restore_verified_checkpoint'
    elif isinstance(exc,ValueError) and type(exc).__name__!='WorkflowError' and (code or any(
            marker in text for marker in ('invalid_','_required','requires_','_must_','_not_','_conflict',
                '_mismatch','unknown_','_missing','_forbidden','_empty','_out_of_','_use_','use_current_batch_command'))):
        category=code or 'schema_repairable'; continuation='SOFT_RETRY'; action='repair_from_retained_tool_facts'
    result={'status':'initialization_failed' if initializing else 'retry' if continuation=='SOFT_RETRY' else 'blocked',
            'continuation':continuation,'error_code':category,'action':'initialization_blocked' if initializing else action}
    return result

def raise_if_storage_error(exc):
    if classify_exception(exc)['error_code'] in ('private_confidential_storage_unavailable',
            'operational_storage_unavailable','operational_storage_temporarily_locked','credential_storage_forbidden'):
        raise exc
