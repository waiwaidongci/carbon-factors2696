"""Grouped factor errata: restatement from frozen drafts, atomic switch, race/recovery/idempotency."""
import time
import pytest
from src.config.database import Session
from src.models.source import Source, Policy
from src.models.run import Run, Schedule, Delivery
from src.models.errata import ErrataBatch, ErrataItem
from src.services import compute
from src.services import errata as errata_service
from src.services.delivery import tick
from tests.conftest import login, policy


def add_source(db, key, period, dept, activity, qty, unit):
    data = {'period': period, 'department': dept, 'activity': activity, 'quantity': qty, 'unit': unit}
    db.add(Source(natural_key=key, data=data))


def setup_group(client, admin, electricity=0.71, versions=('因子-v1', '因子-v2')):
    with Session() as db:
        db.add(Policy(version=versions[0], data=policy(versions[0], 0.57)))
        db.add(Policy(version=versions[1], data=policy(versions[1], electricity)))
        add_source(db, 'a1', '2026-11', '材料试验室', '电力', 10, 'kWh')
        add_source(db, 'a2', '2026-11', '材料试验室', '柴油', 5, 'L')
        add_source(db, 'a3', '2026-10', '工艺试验室', '通勤', 20, 'km')
        add_source(db, 'a4', '2026-10', '工艺试验室', '电力', 2, 'MWh')
        db.commit()
        policies = {p.version: p.id for p in db.query(Policy).all()}
    runs = []
    for name, ver, period in (('十月核算', versions[0], '2026-10'), ('十一月核算', versions[0], '2026-11')):
        r = client.post('/runs', headers=admin, json={'name': name, 'policy_id': policies[ver], 'period': period})
        assert r.status_code == 200, r.text
        runs.append(r.json()['id'])
    with Session() as db:
        for rid in runs:
            compute.process_run(db, rid)
        for rid in runs:
            run = db.get(Run, rid)
            assert run.status == 'completed', run.error
            run.approved = True
            run.review = '原复核'
        db.commit()
    return policies, runs


def create_schedule(client, admin, run_id, interval=3600):
    r = client.post('/schedules', headers=admin, json={'name': '周期结算', 'run_id': run_id, 'interval_seconds': interval})
    assert r.status_code == 200, r.text
    return r.json()['id']


def run_batch_jobs(db, enqueued):
    while enqueued['errata-jobs']:
        ids = enqueued['errata-jobs'][:]
        enqueued['errata-jobs'].clear()
        for item_id in ids:
            errata_service.process_item(db, item_id)


def test_create_only_selects_related_approved_reports_from_frozen_snapshot(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    # 修改底稿不影响冻结快照
    with Session() as db:
        src = db.query(Source).filter_by(natural_key='a1').first()
        src.data = {**src.data, 'quantity': 999}
        db.commit()
    r = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                    'new_policy_id': policies['因子-v2'], 'activities': ['电力']})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['status'] in ('calculating', 'review', 'failed')
    assert sorted(i['source_run_id'] for i in body['items']) == sorted(runs)
    with Session() as db:
        for item in db.query(ErrataItem).all():
            source = db.get(Run, item.source_run_id)
            assert any(s['activity'] == '电力' and s['quantity'] != 999 for s in source.snapshot['sources'])
    run_batch_jobs(Session(), enqueued)
    detail = client.get(f"/errata/{body['id']}", headers=admin).json()
    assert detail['status'] == 'review'
    assert detail['completed_count'] == detail['item_count'] == 2
    # 原报告与快照保留
    with Session() as db:
        for rid in runs:
            original = db.get(Run, rid)
            assert original.approved and original.review == '原复核'
            assert original.snapshot['policy']['version'] == '因子-v1'


def test_switch_requires_full_group_and_review(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    sid = create_schedule(client, admin, runs[0])
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    r = client.post(f"/errata/{created['id']}/switch", headers=admin)
    assert r.status_code == 409
    # 只算完一部分时不允许复核
    with Session() as db:
        first = enqueued['errata-jobs'].pop(0)
        errata_service.process_item(db, first)
    r = client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '数值已核对'})
    assert r.status_code == 409
    run_batch_jobs(Session(), enqueued)
    r = client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '数值已核对'})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'reviewed'
    # 复核后每条重述报告都被标记为已复核
    with Session() as db:
        for run in db.query(Run).filter(Run.errata_batch_id == created['id']).all():
            assert run.approved and '数值已核对' in run.review
    r = client.post(f"/errata/{created['id']}/switch", headers=admin)
    assert r.status_code == 200, r.text
    with Session() as db:
        schedule = db.get(Schedule, sid)
        assert schedule.generation == 1
        assert schedule.run_id != runs[0]
        assert db.get(Run, schedule.run_id).errata_batch_id == created['id']


def test_failure_keeps_old_group_and_retry_is_idempotent(client, enqueued):
    admin = login(client)
    with Session() as db:
        db.add(Policy(version='因子-v1', data=policy('因子-v1')))
        # 新版本把电力因子单位登记为 L：电力因子确有变化，但与原底稿 kWh 不兼容，重算会失败
        broken = policy('因子-v2', 0.7)
        broken['factors']['电力']['unit'] = 'L'
        db.add(Policy(version='因子-v2', data=broken))
        add_source(db, 'b1', '2026-11', '材料试验室', '电力', 10, 'kWh')
        db.commit()
        pid = db.query(Policy).filter_by(version='因子-v1').first().id
    rid = client.post('/runs', headers=admin, json={'name': '电力核算', 'policy_id': pid, 'period': '2026-11'}).json()['id']
    with Session() as db:
        compute.process_run(db, rid)
        db.get(Run, rid).approved = True
        db.commit()
    sid = create_schedule(client, admin, rid)
    resp = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': pid, 'new_policy_id': pid + 1,
                                                          'activities': ['电力']})
    assert resp.status_code == 200, resp.text
    created = resp.json()
    run_batch_jobs(Session(), enqueued)
    with Session() as db:
        assert db.get(ErrataBatch, created['id']).status == 'failed'
        assert db.get(Schedule, sid).run_id == rid and db.get(Schedule, sid).generation == 0
        assert db.query(Run).filter(Run.errata_batch_id == created['id']).count() == 1
    r = client.post(f"/errata/{created['id']}/switch", headers=admin)
    assert r.status_code == 409
    r = client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '不该通过'})
    assert r.status_code == 409
    # 重复投递同一明细不会重复生成报告
    del enqueued['errata-jobs'][:]
    assert client.post(f"/errata/{created['id']}/retry", headers=admin).status_code == 200
    duplicate = enqueued['errata-jobs'][:]
    assert duplicate
    enqueued['errata-jobs'] = duplicate + duplicate + duplicate
    run_batch_jobs(Session(), enqueued)
    with Session() as db:
        assert db.query(Run).filter(Run.errata_batch_id == created['id']).count() == 1
        assert db.get(ErrataBatch, created['id']).status == 'failed'


def test_stale_switch_is_rejected_after_concurrent_edit(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    sid = create_schedule(client, admin, runs[0])
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    assert client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '整组核对'}).status_code == 200
    # 并发操作者把计划切到别的报告并推进代次
    with Session() as db:
        schedule = db.get(Schedule, sid)
        schedule.run_id = runs[1]
        schedule.generation = 7
        db.commit()
    r = client.post(f"/errata/{created['id']}/switch", headers=admin)
    assert r.status_code == 409
    assert '拒绝过时切换' in r.json()['detail']
    with Session() as db:
        batch = db.get(ErrataBatch, created['id'])
        assert batch.status == 'reviewed'
        assert db.get(Schedule, sid).run_id == runs[1]


def test_overdue_delivery_after_switch_belongs_to_new_group(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    sid = create_schedule(client, admin, runs[0], interval=5)
    # 先发出一次旧组交付
    with Session() as db:
        db.get(Schedule, sid).next_at = time.time() - 30
        db.commit()
        tick(db)
        old_deliveries = [d.id for d in db.query(Delivery).filter_by(schedule_id=sid).all()]
        assert len(old_deliveries) == 1
    # 下一次结算再次到期但尚未发出，此时完成整组切换
    with Session() as db:
        db.get(Schedule, sid).next_at = time.time() - 5
        db.commit()
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '整组核对'})
    r = client.post(f"/errata/{created['id']}/switch", headers=admin)
    assert r.status_code == 200, r.text
    with Session() as db:
        schedule = db.get(Schedule, sid)
        new_run = schedule.run_id
        deliveries = db.query(Delivery).filter_by(schedule_id=sid).order_by(Delivery.id).all()
        # 已发交付保留原报告，不重复生成
        assert [d.id for d in deliveries if d.id in old_deliveries] == old_deliveries
        assert all(d.run_id == runs[0] for d in deliveries if d.id in old_deliveries)
        # 切换时已到期未发的交付归新组
        fresh = [d for d in deliveries if d.id not in old_deliveries]
        assert len(fresh) == 1 and fresh[0].run_id == new_run
        assert fresh[0].errata_batch_id == created['id']
        assert schedule.next_at > time.time()
    assert len(client.get('/deliveries', headers=admin).json()) == 2


def test_worker_never_writes_old_group_after_switch(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    sid = create_schedule(client, admin, runs[0], interval=5)
    with Session() as db:
        db.get(Schedule, sid).next_at = time.time() - 30
        db.commit()
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '整组核对'})
    client.post(f"/errata/{created['id']}/switch", headers=admin)
    with Session() as db:
        schedule = db.get(Schedule, sid)
        # 模拟旧执行者晚到：持有旧报告 id 在切换后才写交付
        old_run = db.get(Run, runs[0])
        from src.services.delivery import issue_due
        issue_due(db, schedule, old_run, schedule.next_at, time.time())
        db.commit()
        deliveries = db.query(Delivery).filter_by(schedule_id=sid).all()
        assert all(d.run_id == schedule.run_id for d in deliveries)
        # 计划代次已变化，旧批次不能再次切换
        r = client.post(f"/errata/{created['id']}/switch", headers=admin)
        assert r.status_code == 409


def test_interrupted_computation_recovers_without_duplicate_reports(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    with Session() as db:
        item = db.query(ErrataItem).filter_by(batch_id=created['id']).first()
        item.status = 'running'
        item.started = time.time() - 600
        db.commit()
        ids = errata_service.recover_items(db)
        assert item.id in ids
        assert db.get(ErrataItem, item.id).status == 'queued'
    before = len([])
    run_batch_jobs(Session(), enqueued)
    with Session() as db:
        assert db.get(ErrataBatch, created['id']).status == 'review'
        restated = db.query(Run).filter(Run.errata_batch_id == created['id']).all()
        assert len(restated) == len(runs)
        assert all(r.restated_from in runs for r in restated)
        # 重跑恢复不会再产生报告
        errata_service.recover_items(db)
        run_batch_jobs(Session(), enqueued)
        assert db.query(Run).filter(Run.errata_batch_id == created['id']).count() == len(runs)


def test_validation_rejects_unrelated_or_unchanged_activities(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    # 电力没有变化
    with Session() as db:
        db.add(Policy(version='因子-v3', data=policy('因子-v3', 0.57)))
        db.commit()
        v3 = db.query(Policy).filter_by(version='因子-v3').first().id
    r = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                    'new_policy_id': v3, 'activities': ['电力']})
    assert r.status_code == 422
    # 新版本中柴油也变了，但只声明电力
    with Session() as db:
        data = policy('因子-v4', 0.7)
        data['factors']['柴油']['kg_per_unit'] = 3.0
        db.add(Policy(version='因子-v4', data=data))
        db.commit()
        v4 = db.query(Policy).filter_by(version='因子-v4').first().id
    r = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                    'new_policy_id': v4, 'activities': ['电力']})
    assert r.status_code == 422
    assert '柴油' in r.json()['detail']


def test_only_admin_can_operate(client, enqueued):
    admin = login(client)
    analyst = login(client, 'analyst')
    policies, runs = setup_group(client, admin)
    r = client.post('/errata', headers=analyst, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                      'new_policy_id': policies['因子-v2'], 'activities': ['电力']})
    assert r.status_code == 403


def test_schedule_created_after_batch_is_switched_too(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    # 批次建立后才新建、仍引用旧组的计划
    late_sid = create_schedule(client, admin, runs[1], interval=3600)
    run_batch_jobs(Session(), enqueued)
    client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '整组核对'})
    assert client.post(f"/errata/{created['id']}/switch", headers=admin).status_code == 200
    with Session() as db:
        schedule = db.get(Schedule, late_sid)
        assert schedule.run_id != runs[1]
        assert db.get(Run, schedule.run_id).errata_batch_id == created['id']


def test_restated_run_cannot_be_approved_individually(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    detail = client.get(f"/errata/{created['id']}", headers=admin).json()
    restated = detail['items'][0]['run_id']
    r = client.post(f'/runs/{restated}/approve', headers=admin, json={'note': '单独复核'})
    assert r.status_code == 409
    with Session() as db:
        assert not db.get(Run, restated).approved


def test_viewer_cannot_create_or_switch(client, enqueued):
    admin = login(client)
    viewer = login(client, 'viewer')
    policies, runs = setup_group(client, admin)
    r = client.post('/errata', headers=viewer, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                     'new_policy_id': policies['因子-v2'], 'activities': ['电力']})
    assert r.status_code == 403
    assert client.get('/errata', headers=viewer).json() == []


def test_viewer_can_download_errata_csv_only_after_group_review(client, enqueued):
    admin = login(client)
    viewer = login(client, 'viewer')
    policies, runs = setup_group(client, admin)
    created = client.post('/errata', headers=admin, json={'name': '电力勘误', 'old_policy_id': policies['因子-v1'],
                                                          'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    detail = client.get(f"/errata/{created['id']}", headers=admin).json()
    item = detail['items'][0]
    # 复核前：重述报告未复核，viewer 不可下载；原报告仍可下载
    r = client.get(f"/errata/{created['id']}/items/{item['id']}/csv", headers=viewer)
    assert r.status_code == 403
    assert client.get(f"/runs/{item['source_run_id']}/csv", headers=viewer).status_code == 200
    client.post(f"/errata/{created['id']}/review", headers=admin, json={'note': '整组核对完成'})
    r = client.get(f"/errata/{created['id']}/items/{item['id']}/csv", headers=viewer)
    assert r.status_code == 200
    assert '排放kgCO2e' in r.text
    # 重述报告出现在报告列表且可单独下载
    runs_list = {x['id'] for x in client.get('/runs', headers=viewer).json()}
    assert item['run_id'] in runs_list
    assert client.get(f"/runs/{item['run_id']}/csv", headers=viewer).status_code == 200


def test_second_batch_cannot_switch_same_schedule_after_first(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    sid = create_schedule(client, admin, runs[0])
    first = client.post('/errata', headers=admin, json={'name': '电力勘误A', 'old_policy_id': policies['因子-v1'],
                                                        'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    # 同一批原报告不能被两个进行中的勘误批次同时重述
    r = client.post('/errata', headers=admin, json={'name': '电力勘误并发', 'old_policy_id': policies['因子-v1'],
                                                    'new_policy_id': policies['因子-v2'], 'activities': ['电力']})
    assert r.status_code == 409
    run_batch_jobs(Session(), enqueued)
    client.post(f"/errata/{first['id']}/review", headers=admin, json={'note': '整组核对A'})
    assert client.post(f"/errata/{first['id']}/switch", headers=admin).status_code == 200
    # 切换后旧组报告已被取代，不能围绕失效旧组再建 batch
    with Session() as db:
        db.add(Policy(version='因子-v3', data=policy('因子-v3', 0.75)))
        db.commit()
        v3 = db.query(Policy).filter_by(version='因子-v3').first().id
    r = client.post('/errata', headers=admin, json={'name': '电力勘误B', 'old_policy_id': policies['因子-v1'],
                                                    'new_policy_id': v3, 'activities': ['电力']})
    assert r.status_code == 422
    assert '仍生效' in r.json()['detail']
    with Session() as db:
        schedule = db.get(Schedule, sid)
        assert schedule.generation == 1
        assert db.get(Run, schedule.run_id).errata_batch_id == first['id']


def test_chained_errata_restates_previous_restatement(client, enqueued):
    admin = login(client)
    policies, runs = setup_group(client, admin)
    sid = create_schedule(client, admin, runs[0])
    first = client.post('/errata', headers=admin, json={'name': '电力勘误A', 'old_policy_id': policies['因子-v1'],
                                                        'new_policy_id': policies['因子-v2'], 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    client.post(f"/errata/{first['id']}/review", headers=admin, json={'note': '整组核对A'})
    assert client.post(f"/errata/{first['id']}/switch", headers=admin).status_code == 200
    with Session() as db:
        db.add(Policy(version='因子-v3', data=policy('因子-v3', 0.75)))
        db.commit()
        v3 = db.query(Policy).filter_by(version='因子-v3').first().id
        first_restated = db.get(Schedule, sid).run_id
    # 对已切换的新组再做一次勘误：以 v2 重述报告为原报告，继续链式重述
    second = client.post('/errata', headers=admin, json={'name': '电力勘误B', 'old_policy_id': policies['因子-v2'],
                                                        'new_policy_id': v3, 'activities': ['电力']}).json()
    run_batch_jobs(Session(), enqueued)
    client.post(f"/errata/{second['id']}/review", headers=admin, json={'note': '链式整组核对'})
    assert client.post(f"/errata/{second['id']}/switch", headers=admin).status_code == 200
    with Session() as db:
        schedule = db.get(Schedule, sid)
        assert schedule.generation == 2
        latest = db.get(Run, schedule.run_id)
        assert latest.errata_batch_id == second['id']
        assert latest.snapshot['policy']['version'] == '因子-v3'
        # 重述来源正是上一轮的重述报告，且上一轮报告保留
        assert latest.restated_from == first_restated
        assert db.get(Run, first_restated).status == 'completed'
        assert db.get(Run, first_restated).approved
