"""One serialization boundary for execution records and their amendments."""
from __future__ import annotations
import strict_json as json
from contextlib import contextmanager
from pathlib import Path
from process_lock import ProcessFileLock


def lock(root):
    return ProcessFileLock(Path(root) / '.execution-coordinator')


def pending(root):
    root = Path(root).resolve()
    records = sorted(root.rglob('*.execution-transaction.json'))
    amendment = root / '.execution-events/events.transaction.json'
    return records + ([amendment] if amendment.is_file() else [])


def assert_no_pending(root):
    items = pending(root)
    if items:
        raise ValueError('execution_transaction_recovery_required:' +
            ','.join(str(p.relative_to(Path(root).resolve())) for p in items))


@contextmanager
def coordinated(*, state_path, writer_ledger_path, fault_at=''):
    with lock(state_path.resolve().parent):
        recover_locked(state_path=state_path, writer_ledger_path=writer_ledger_path, fault_at=fault_at)
        yield


def recover_locked(*, state_path, writer_ledger_path, fault_at=''):
    """Caller holds global lock. Replay original transactions, never rewrite dependencies."""
    from execution_facts import _finish_execution_transaction, context_from_state, _safe_path
    from execution_amendments import _finish
    from retrieval_controls import load_retrieval_config
    root = state_path.resolve().parent
    items = pending(root)
    # The coordinator never permits an execution and amendment journal together.
    # A pre-existing ambiguous pair cannot be ordered by guessing file mtimes.
    if len(items) > 1:
        raise ValueError('execution_transaction_concurrent_pending_conflict')
    for journal in items:
        payload = json.loads(journal.read_text(encoding='utf-8'))
        if journal.name == 'events.transaction.json':
            search = Path(payload['search_file']).resolve()
            if not search.is_relative_to(root):
                raise ValueError('execution_amendment_search_path_escape')
            with ProcessFileLock(search), ProcessFileLock(journal.with_name('events.json')):
                _finish(journal, state_path=state_path, writer_ledger_path=writer_ledger_path, fault_at=fault_at)
        else:
            output = _safe_path(root, payload['output'])
            context = context_from_state(json.loads(state_path.read_text(encoding='utf-8-sig')),
                config=load_retrieval_config(), state_path=state_path)
            with ProcessFileLock(output):
                _finish_execution_transaction(journal, state_path=state_path, writer_ledger_path=writer_ledger_path,
                    output=output, context=context, fault_at=fault_at)
    assert_no_pending(root)
    return {'status': 'recovered', 'transaction_count': len(items)}
