import time
from fastapi import HTTPException
from sqlalchemy import select
from src.models.run import Run,Schedule,Delivery
from src.services.audit import log


def add_schedule(db,user,body):
 run=db.get(Run,body.run_id)
 if not run or not run.approved: raise HTTPException(409,'定时交付需引用已复核报告')
 if run.status!='completed': raise HTTPException(409,'定时交付需引用已完成报告')
 obj=Schedule(name=body.name,run_id=run.id,interval_seconds=body.interval_seconds,next_at=time.time()+body.interval_seconds,enabled=True,generation=0)
 db.add(obj);log(db,user.email,'schedule.create',body.name);db.commit();return obj


def _issue(db, schedule, run, due_at, now):
    key=f'{schedule.id}:{due_at}'
    existing=db.query(Delivery).filter_by(due_key=key).first()
    if existing: return existing
    obj=Delivery(schedule_id=schedule.id,run_id=run.id,due_key=key,created=now,
                 payload={'name':run.name,'checksum':run.checksum,'result':run.result,'review':run.review,
                          'policy':run.snapshot['policy'],'run_id':run.id,
                          'restated_from':run.restated_from,'errata_batch_id':run.errata_batch_id},
                 errata_batch_id=run.errata_batch_id)
    db.add(obj)
    log(db,'worker','delivery.created',key)
    return obj


def issue_due(db, schedule, run, due_at, now, expected_generation=None):
    """锁定计划行后再写交付；执行者持有的报告/代次过期时拒绝写入，旧执行者晚到不会改写新组。"""
    locked=db.execute(select(Schedule).where(Schedule.id==schedule.id).with_for_update()).scalar_one_or_none()
    if not locked or not run or not run.approved or run.status!='completed':
        return None
    stale=locked.run_id!=run.id or (expected_generation is not None and (locked.generation or 0)!=expected_generation)
    if stale:
        log(db,'worker','delivery.rejected_stale',f'计划{locked.id} 执行者报告{run.id} 当前报告{locked.run_id} 代次{locked.generation}')
        return None
    return _issue(db,locked,run,due_at,now)


def tick(db):
    """Issue due deliveries under a row lock so an errata switch cannot race a late old-group write."""
    now=time.time()
    due=db.execute(select(Schedule).where(Schedule.enabled==True,Schedule.next_at<=now)
                   .order_by(Schedule.id).with_for_update(skip_locked=True)).scalars().all()
    for schedule in due:
        run=db.get(Run,schedule.run_id)
        issue_due(db,schedule,run,schedule.next_at,now,expected_generation=schedule.generation or 0)
        schedule.next_at=now+schedule.interval_seconds
    db.commit()
