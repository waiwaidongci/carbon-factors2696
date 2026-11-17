import hashlib, json, time
from fastapi import HTTPException
from sqlalchemy import select
from src.models.source import Policy
from src.models.run import Run, Schedule
from src.models.errata import ErrataBatch, ErrataItem, ErrataScheduleTarget
from src.services.audit import log
from src.services.delivery import issue_due
from src.services import queue

ACTIVE_ITEM_STATUSES = ('queued', 'running')
STALE_ITEM_SECONDS = 120


def _checksum(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _factors(policy):
    return (policy or {}).get('factors') or {}


def _normalise_activities(values):
    if not isinstance(values, list):
        raise HTTPException(422, '受影响活动必须是列表')
    cleaned = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(422, '受影响活动名称不能为空')
        name = value.strip()
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise HTTPException(422, '至少选择一个受影响活动')
    if len(cleaned) > 100:
        raise HTTPException(422, '受影响活动过多')
    return cleaned


def create_batch(db, user, body):
    old = db.get(Policy, body.old_policy_id)
    new = db.get(Policy, body.new_policy_id)
    if not old or not new:
        raise HTTPException(404, '旧或新因子版本不存在')
    if old.id == new.id:
        raise HTTPException(422, '旧版本与新版本必须不同')
    activities = _normalise_activities(body.activities)
    old_factors, new_factors = _factors(old.data), _factors(new.data)
    for activity in activities:
        if activity not in old_factors or activity not in new_factors:
            raise HTTPException(422, f'活动 {activity} 在旧或新版本中缺少因子')
        before, after = old_factors[activity], new_factors[activity]
        if before.get('unit') == after.get('unit') and before.get('kg_per_unit') == after.get('kg_per_unit'):
            raise HTTPException(422, f'活动 {activity} 的因子在新旧版本中没有变化')
    changed = {a for a in set(old_factors) | set(new_factors)
               if old_factors.get(a, {}).get('unit') != new_factors.get(a, {}).get('unit')
               or old_factors.get(a, {}).get('kg_per_unit') != new_factors.get(a, {}).get('kg_per_unit')}
    if changed - set(activities):
        raise HTTPException(422, '受影响活动之外还存在因子变化，请先拆分版本：' + '、'.join(sorted(changed - set(activities))))

    related = []
    for run in db.query(Run).filter(Run.approved == True, Run.status == 'completed').order_by(Run.id).all():
        snapshot = run.snapshot or {}
        if ((snapshot.get('policy') or {}).get('version')) != old.version:
            continue
        used = {s.get('activity') for s in snapshot.get('sources') or [] if isinstance(s, dict)}
        if used & set(activities):
            related.append(run)
    if related:
        # 已被某次切换取代的旧报告不再参与新的勘误，避免围绕失效旧组再建批次。
        switched = db.query(ErrataBatch).filter_by(status='switched').with_entities(ErrataBatch.id).all()
        switched_ids = [x[0] for x in switched]
        if switched_ids:
            superseded = {i.source_run_id for i in db.query(ErrataItem).filter(
                ErrataItem.batch_id.in_(switched_ids), ErrataItem.status == 'completed').all()}
            related = [r for r in related if r.id not in superseded]
    if not related:
        raise HTTPException(422, '没有使用旧版本且包含受影响活动、仍生效的已复核报告')
    related_ids = [r.id for r in related]
    # 同一批原报告不能被两个进行中的勘误批次同时重述，避免生成互相争用的新组。
    busy = db.query(ErrataItem).join(ErrataBatch, ErrataBatch.id == ErrataItem.batch_id).filter(
        ErrataItem.source_run_id.in_(related_ids),
        ErrataBatch.status.in_(('calculating', 'review', 'reviewed'))).all()
    if busy:
        raise HTTPException(409, '所选报告已被其他进行中的勘误批次重述，请先完成或等待该批次')

    now = time.time()
    batch = ErrataBatch(name=body.name, old_policy_id=old.id, new_policy_id=new.id, activities=activities,
                        status='calculating', created=now, created_by=user.email, review='')
    db.add(batch)
    db.flush()
    for run in related:
        db.add(ErrataItem(batch_id=batch.id, source_run_id=run.id, status='queued', attempts=0, queued_at=now))
    targets = []
    for schedule in db.query(Schedule).filter(Schedule.run_id.in_(related_ids)).order_by(Schedule.id).all():
        target = ErrataScheduleTarget(batch_id=batch.id, schedule_id=schedule.id, old_run_id=schedule.run_id,
                                      expected_generation=schedule.generation or 0, switched=False)
        db.add(target)
        targets.append(target)
    log(db, user.email, 'errata.create', f'{batch.name} 旧{old.version}→新{new.version} 活动{"、".join(activities)} 报告{len(related)} 计划{len(targets)}')
    db.commit()
    items = db.query(ErrataItem).filter_by(batch_id=batch.id).order_by(ErrataItem.id).all()
    queue.enqueue_many('errata-jobs', [i.id for i in items if i.status == 'queued'])
    return batch


def _policy_for(db, batch):
    policy = db.get(Policy, batch.new_policy_id)
    if not policy:
        raise HTTPException(409, '新因子版本已不存在')
    return policy


def process_item(db, item_id):
    item = db.execute(select(ErrataItem).where(ErrataItem.id == item_id).with_for_update()).scalar_one_or_none()
    # 只有 queued 的明细会被执行：重复投递的旧消息、已完成的明细或正在执行的明细都直接忽略。
    if not item or item.status != 'queued':
        return False
    batch = db.get(ErrataBatch, item.batch_id)
    if not batch:
        item.status = 'failed'
        item.error = '勘误批次不存在'
        db.commit()
        return False
    if batch.status == 'switched':
        # 批次已切换：迟到的队列消息不得再生成报告。仅有已存在的重述报告才记为完成。
        item.status = 'completed' if item.run_id else 'failed'
        item.error = None if item.run_id else '批次已切换，忽略迟到的重算消息'
        db.commit()
        return False
    source_run = db.get(Run, item.source_run_id)
    if not source_run:
        item.status = 'failed'
        item.error = '原报告不存在'
        db.commit()
        _reconcile(db, batch.id)
        return False

    existing = db.query(Run).filter_by(errata_batch_id=batch.id, restated_from=source_run.id).first()
    if existing and existing.status == 'completed':
        item.run_id = existing.id
        item.status = 'completed'
        item.error = None
        db.commit()
        _reconcile(db, batch.id)
        return False

    # 冻结底稿完整性：快照必须与原报告提交时的 SHA256 一致，且只使用该冻结底稿。
    if _checksum(source_run.snapshot) != source_run.checksum:
        item.status = 'failed'
        item.error = '原报告冻结底稿校验失败，拒绝重算'
        db.commit()
        log(db, 'worker', 'errata.frozen_mismatch', f'批次{batch.id} 原报告{source_run.id}')
        db.commit()
        _reconcile(db, batch.id)
        return False

    item.status = 'running'
    item.started = time.time()
    item.attempts = (item.attempts or 0) + 1
    db.commit()

    try:
        policy = _policy_for(db, batch)
        snapshot = {
            'policy': policy.data,
            'sources': source_run.snapshot.get('sources') or [],
            'period': source_run.snapshot.get('period', '全部'),
            'errata': {'batch_id': batch.id, 'batch_name': batch.name,
                       'source_run_id': source_run.id, 'activities': batch.activities,
                       'old_policy': (source_run.snapshot.get('policy') or {}).get('version')},
        }
        if existing:
            run = existing
            run.snapshot = snapshot
            run.checksum = _checksum(snapshot)
            run.name = f'{source_run.name}（勘误重述·{batch.name}）'
            run.status = 'running'
            run.started = time.time()
            run.approved = False
            run.review = ''
            db.commit()
        else:
            run = Run(name=f'{source_run.name}（勘误重述·{batch.name}）', status='running',
                      snapshot=snapshot, checksum=_checksum(snapshot), created=time.time(),
                      approved=False, restated_from=source_run.id, errata_batch_id=batch.id,
                      started=time.time())
            db.add(run)
            db.commit()
        item.run_id = run.id
        db.commit()

        from src.services.calculation import calculate
        run.result = calculate(run.snapshot['sources'], run.snapshot['policy'])
        run.status = 'completed'
        run.error = None
        item.status = 'completed'
        item.error = None
        db.commit()
        log(db, 'worker', 'errata.restated', f'批次{batch.id} 原报告{source_run.id} → 重述{run.id}')
        db.commit()
    except Exception as exc:
        db.rollback()
        item = db.get(ErrataItem, item_id)
        batch = db.get(ErrataBatch, item.batch_id)
        run = db.query(Run).filter_by(errata_batch_id=item.batch_id, restated_from=item.source_run_id).first()
        if run:
            run.status = 'failed'
            run.error = str(exc)
        item.status = 'failed'
        item.error = str(exc)
        db.commit()
        log(db, 'worker', 'errata.item_failed', f'批次{item.batch_id} 原报告{item.source_run_id}：{exc}')
        db.commit()
    _reconcile(db, item.batch_id)
    return True


def _reconcile(db, batch_id):
    batch = db.execute(select(ErrataBatch).where(ErrataBatch.id == batch_id).with_for_update()).scalar_one_or_none()
    if not batch or batch.status not in ('calculating', 'review', 'failed'):
        return
    items = db.query(ErrataItem).filter_by(batch_id=batch_id).all()
    if not items:
        return
    if any(i.status == 'failed' for i in items):
        batch.status = 'failed'
        batch.error = '存在失败的重述报告，旧组继续生效'
    elif all(i.status == 'completed' for i in items):
        batch.status = 'review'
        batch.error = None
    else:
        batch.status = 'calculating'
        batch.error = None
    db.commit()


def retry_batch(db, user, batch_id):
    # 先锁明细、再锁批次，与 worker 的加锁顺序一致，避免并发重试与重算互相等待成环。
    items = db.query(ErrataItem).filter_by(batch_id=batch_id).order_by(ErrataItem.id).with_for_update().all()
    batch = db.execute(select(ErrataBatch).where(ErrataBatch.id == batch_id).with_for_update()).scalar_one_or_none()
    if not batch:
        raise HTTPException(404, '勘误批次不存在')
    if batch.status == 'switched':
        raise HTTPException(409, '批次已切换，不能重试')
    if batch.status == 'reviewed':
        raise HTTPException(409, '批次已复核，请直接切换')
    if batch.status == 'review':
        raise HTTPException(409, '整组已算完，请直接复核')
    retry = []
    for item in items:
        if item.status == 'failed':
            # 复用已失败的重述报告行原地重算，不删除历史，也不会重复生成报告。
            item.status = 'queued'
            item.error = None
            item.queued_at = time.time()
            retry.append(item)
        elif item.status == 'queued':
            retry.append(item)
    if not retry:
        raise HTTPException(409, '没有需要重试的重述明细')
    batch.status = 'calculating'
    batch.error = None
    log(db, user.email, 'errata.retry', f'批次{batch.id}')
    db.commit()
    queue.enqueue_many('errata-jobs', [i.id for i in retry])
    return batch


def recover_items(db):
    """Requeue interrupted errata items; duplicate deliveries are harmless because processing is idempotent."""
    now = time.time()
    changed = False
    retry = []
    for item in db.query(ErrataItem).filter(ErrataItem.status.in_(ACTIVE_ITEM_STATUSES)).all():
        if item.status == 'running' and item.started and now - item.started <= STALE_ITEM_SECONDS:
            continue
        if item.status == 'queued' and item.queued_at and now - item.queued_at <= STALE_ITEM_SECONDS:
            continue
        item.status = 'queued'
        item.started = None
        item.queued_at = now
        retry.append(item.id)
        changed = True
    for batch in db.query(ErrataBatch).filter(ErrataBatch.status.in_(('calculating', 'failed'))).all():
        _reconcile(db, batch.id)
    if changed:
        db.commit()
    if retry:
        queue.enqueue_many('errata-jobs', retry)
    return retry


def _row_map(result):
    rows = {}
    for row in (result or {}).get('rows', []):
        key = (row.get('月份'), row.get('部门'), row.get('活动'))
        rows[key] = row
    return rows


def diff(db, batch, include_rows=True):
    items = []
    for item in db.query(ErrataItem).filter_by(batch_id=batch.id).order_by(ErrataItem.id).all():
        source = db.get(Run, item.source_run_id)
        restated = db.get(Run, item.run_id) if item.run_id else None
        entry = {'id': item.id, 'source_run_id': item.source_run_id, 'run_id': item.run_id,
                 'source_name': source.name if source else None,
                 'status': item.status, 'error': item.error,
                 'old_policy': ((source.snapshot or {}).get('policy') or {}).get('version') if source else None,
                 'new_policy': ((restated.snapshot or {}).get('policy') or {}).get('version') if restated else None,
                 'old_total': None, 'new_total': None, 'total_delta': None, 'rows': []}
        if include_rows:
            old_rows = _row_map(source.result if source else None)
            new_rows = _row_map(restated.result if restated else None)
            rows = []
            for key in sorted(set(old_rows) | set(new_rows), key=lambda k: tuple('' if x is None else str(x) for x in k)):
                old_row = old_rows.get(key) or {}
                new_row = new_rows.get(key) or {}
                old_value = old_row.get('排放kgCO2e')
                new_value = new_row.get('排放kgCO2e')
                rows.append({'月份': key[0], '部门': key[1], '活动': key[2],
                             '原排放kgCO2e': old_value, '重述排放kgCO2e': new_value,
                             '差额kgCO2e': None if old_value is None or new_value is None else round(new_value - old_value, 6)})
            entry['rows'] = rows
        old_total = (source.result or {}).get('summary', {}).get('核算kgCO2e') if source and source.result else None
        new_total = (restated.result or {}).get('summary', {}).get('核算kgCO2e') if restated and restated.result else None
        entry['old_total'] = old_total
        entry['new_total'] = new_total
        entry['total_delta'] = None if old_total is None or new_total is None else round(new_total - old_total, 6)
        items.append(entry)
    targets = []
    for target in db.query(ErrataScheduleTarget).filter_by(batch_id=batch.id).order_by(ErrataScheduleTarget.id).all():
        schedule = db.get(Schedule, target.schedule_id)
        targets.append({'id': target.id, 'schedule_id': target.schedule_id,
                        'schedule_name': schedule.name if schedule else None,
                        'old_run_id': target.old_run_id, 'new_run_id': target.new_run_id,
                        'expected_generation': target.expected_generation,
                        'current_generation': schedule.generation if schedule else None,
                        'current_run_id': schedule.run_id if schedule else None,
                        'switched': bool(target.switched)})
    return items, targets


def serialise(db, batch, detailed=False):
    old = db.get(Policy, batch.old_policy_id)
    new = db.get(Policy, batch.new_policy_id)
    data = {'id': batch.id, 'name': batch.name, 'status': batch.status, 'activities': batch.activities or [],
            'old_policy_id': batch.old_policy_id, 'new_policy_id': batch.new_policy_id,
            'old_policy': old.version if old else None, 'new_policy': new.version if new else None,
            'created': batch.created, 'created_by': batch.created_by, 'review': batch.review,
            'reviewed_by': batch.reviewed_by, 'reviewed_at': batch.reviewed_at,
            'switched_at': batch.switched_at, 'error': batch.error}
    items, targets = diff(db, batch, include_rows=detailed)
    data['items'] = items
    data['targets'] = targets
    data['item_count'] = len(items)
    data['failed_count'] = sum(1 for i in items if i['status'] == 'failed')
    data['completed_count'] = sum(1 for i in items if i['status'] == 'completed')
    return data


def review(db, user, batch_id, note):
    batch = db.execute(select(ErrataBatch).where(ErrataBatch.id == batch_id).with_for_update()).scalar_one_or_none()
    if not batch:
        raise HTTPException(404, '勘误批次不存在')
    if batch.status == 'switched':
        raise HTTPException(409, '批次已切换')
    if batch.status == 'failed':
        raise HTTPException(409, '存在失败的重述报告，请先重试；旧组继续生效')
    if batch.status == 'calculating':
        raise HTTPException(409, '整组重述尚未算完')
    if batch.status == 'reviewed':
        raise HTTPException(409, '批次已经复核')
    if not db.get(Policy, batch.new_policy_id):
        raise HTTPException(409, '新因子版本已不存在')
    items = db.query(ErrataItem).filter_by(batch_id=batch_id).all()
    if not items or any(i.status != 'completed' or not i.run_id for i in items):
        raise HTTPException(409, '整组重述报告不完整')
    for item in items:
        run = db.get(Run, item.run_id)
        if not run or run.status != 'completed' or run.errata_batch_id != batch.id:
            raise HTTPException(409, '重述报告不完整或不属于本批次')
        run.approved = True
        run.review = f'勘误成组复核：{note}'
    batch.status = 'reviewed'
    batch.review = note
    batch.reviewed_by = user.email
    batch.reviewed_at = time.time()
    log(db, user.email, 'errata.review', f'批次{batch.id}：{note}')
    db.commit()
    return batch


def switch(db, user, batch_id):
    batch = db.execute(select(ErrataBatch).where(ErrataBatch.id == batch_id).with_for_update()).scalar_one_or_none()
    if not batch:
        raise HTTPException(404, '勘误批次不存在')
    if batch.status == 'switched':
        raise HTTPException(409, '批次已经切换')
    if batch.status != 'reviewed':
        raise HTTPException(409, '整组重述算完并复核后才能切换')
    items = db.query(ErrataItem).filter_by(batch_id=batch.id).all()
    if not items or any(i.status != 'completed' or not i.run_id for i in items):
        raise HTTPException(409, '整组重述报告不完整')
    targets = db.query(ErrataScheduleTarget).filter_by(batch_id=batch.id).order_by(ErrataScheduleTarget.id).all()
    # 批次建立后新建、仍引用旧组的计划也纳入切换范围（以当前代次为准）。
    targeted = {t.schedule_id for t in targets}
    old_run_ids = [i.source_run_id for i in items]
    for schedule in db.query(Schedule).filter(Schedule.run_id.in_(old_run_ids)).order_by(Schedule.id).all():
        if schedule.id in targeted:
            continue
        target = ErrataScheduleTarget(batch_id=batch.id, schedule_id=schedule.id, old_run_id=schedule.run_id,
                                      expected_generation=schedule.generation or 0, switched=False)
        db.add(target)
        targets.append(target)
    db.flush()
    if not targets:
        raise HTTPException(409, '批次没有关联交付计划，无法切换')
    stale = []
    for target in targets:
        schedule = db.execute(select(Schedule).where(Schedule.id == target.schedule_id).with_for_update()).scalar_one_or_none()
        if not schedule or schedule.run_id != target.old_run_id or (schedule.generation or 0) != target.expected_generation:
            stale.append(target.schedule_id)
            continue
        run = db.get(Run, target.new_run_id) if target.new_run_id else db.query(Run).filter_by(
            errata_batch_id=batch.id, restated_from=target.old_run_id).first()
        if not run or run.status != 'completed' or not run.approved:
            stale.append(target.schedule_id)
            continue
        target.new_run_id = run.id
    if stale:
        db.rollback()
        raise HTTPException(409, '交付计划已被其他操作更新，拒绝过时切换：' + '、'.join(str(x) for x in stale))

    now = time.time()
    for target in targets:
        schedule = db.get(Schedule, target.schedule_id)
        schedule.run_id = target.new_run_id
        schedule.generation = (schedule.generation or 0) + 1
        schedule.last_errata_batch_id = batch.id
        target.switched = True
        # 切换时已经到期但尚未发出的结算归新组：在计划行锁内补发新组交付并推进到期时间。
        if schedule.enabled and schedule.next_at <= now:
            run = db.get(Run, target.new_run_id)
            issue_due(db, schedule, run, schedule.next_at, now)
            schedule.next_at = now + schedule.interval_seconds
    batch.status = 'switched'
    batch.switched_at = now
    log(db, user.email, 'errata.switch', f'批次{batch.id} 切换计划{len(targets)}个')
    db.commit()
    return batch
