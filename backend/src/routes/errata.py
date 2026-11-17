from fastapi import APIRouter, Depends, HTTPException
from src.config.database import database
from src.controllers.auth import current, admin
from src.models.errata import ErrataBatch
from src.schemas.requests import ErrataInput, ErrataReviewInput
from src.services import errata as service
router = APIRouter()


def _load(db, batch_id):
    batch = db.get(ErrataBatch, batch_id)
    if not batch:
        raise HTTPException(404, '勘误批次不存在')
    return batch


@router.get('/errata')
def batches(db=Depends(database), u=Depends(current)):
    return [service.serialise(db, b, detailed=True) for b in db.query(ErrataBatch).order_by(ErrataBatch.id.desc()).all()]


@router.get('/errata/{id}')
def detail(id: int, db=Depends(database), u=Depends(current)):
    return service.serialise(db, _load(db, id), detailed=True)


@router.post('/errata')
def create(b: ErrataInput, db=Depends(database), u=Depends(admin)):
    batch = service.create_batch(db, u, b)
    return service.serialise(db, _load(db, batch.id))


@router.post('/errata/{id}/retry')
def retry(id: int, db=Depends(database), u=Depends(admin)):
    service.retry_batch(db, u, id)
    return service.serialise(db, _load(db, id))


@router.post('/errata/{id}/review')
def review(id: int, b: ErrataReviewInput, db=Depends(database), u=Depends(admin)):
    service.review(db, u, id, b.note)
    return service.serialise(db, _load(db, id))


@router.post('/errata/{id}/switch')
def switch(id: int, db=Depends(database), u=Depends(admin)):
    service.switch(db, u, id)
    return service.serialise(db, _load(db, id))


@router.get('/errata/{id}/items/{item_id}/csv')
def item_csv(id: int, item_id: int, db=Depends(database), u=Depends(current)):
    from fastapi.responses import Response
    from src.models.errata import ErrataItem
    from src.models.run import Run
    from src.services.export import csv_text
    batch = _load(db, id)
    item = db.get(ErrataItem, item_id)
    if not item or item.batch_id != batch.id:
        raise HTTPException(404, '勘误明细不存在')
    run = db.get(Run, item.run_id) if item.run_id else None
    if not run or run.status != 'completed':
        raise HTTPException(409, '重述报告尚未完成')
    if u.role == 'viewer' and not run.approved:
        raise HTTPException(403, '业务阅读者仅可下载已复核报告')
    return Response(csv_text(run.result), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="errata-{id}-run-{run.id}.csv"'})
