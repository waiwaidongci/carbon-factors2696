import time, traceback
from sqlalchemy import select
from src.models.run import Run
from src.services.calculation import calculate
from src.services import queue

STALE_RUNNING_SECONDS = 120

def process_run(db, run_id):
    """Idempotently execute one queued run. Re-delivery of the same job is a no-op."""
    run = db.execute(select(Run).where(Run.id == run_id).with_for_update()).scalar_one_or_none()
    if not run or run.status != 'queued':
        return False
    run.status = 'running'
    run.started = time.time()
    db.commit()
    try:
        run.result = calculate(run.snapshot['sources'], run.snapshot['policy'])
        run.status = 'completed'
        run.error = None
    except Exception as exc:
        run.status = 'failed'
        run.error = str(exc)
        traceback.print_exc()
    db.commit()
    return True

def recover(db):
    """Requeue runs that were queued but lost their Redis job, and reclaim stale running runs."""
    now = time.time()
    stale = db.query(Run).filter(Run.status == 'running').all()
    for run in stale:
        if not run.started or now - run.started > STALE_RUNNING_SECONDS:
            run.status = 'queued'
            run.started = None
            run.queued_at = now
    db.commit()
    ids = [r.id for r in db.query(Run).filter(Run.status == 'queued').order_by(Run.id).all()
           if not r.queued_at or now - r.queued_at > 15]
    queue.enqueue_many('report-jobs', ids)
    return ids
