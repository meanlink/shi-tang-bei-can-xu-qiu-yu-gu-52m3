from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import MealBatch, Handover, Recall, RecoveryOrder
from src.schemas.trace import (
    BatchInput,
    HandoverInput,
    InspectionInput,
    RecallInput,
    RecoveryEntryInput,
    SourceListInput,
)
from src.services import trace
router = APIRouter(prefix='/trace')


@router.get('/overview')
def overview(db=Depends(get_db), u=Depends(current_user)):
    return trace.trace_overview(db)


@router.get('/batches')
def batches(state: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    rows = []
    for batch in db.scalars(select(MealBatch).order_by(MealBatch.id.desc())):
        data = trace.batch_dict(db, batch)
        if state and data['trace_state'] != state:
            continue
        rows.append(data)
    return rows


@router.post('/batches', status_code=201)
def create_batch(data: BatchInput, db=Depends(get_db), u=Depends(can_write)):
    if data.origin == 'prepared':
        batch = trace.create_prepared_batch(db, data, u)
    elif data.origin in ('split', 'mixed'):
        batch = trace.create_derived_batch(db, data, u)
    else:
        raise HTTPException(422, '餐批来源须为 prepared、split 或 mixed')
    db.commit()
    return trace.batch_dict(db, batch, detailed=True)


@router.get('/batches/{id}')
def batch_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    batch = db.get(MealBatch, id)
    if not batch:
        raise HTTPException(404, '餐批不存在')
    return trace.batch_dict(db, batch, detailed=True)


@router.get('/batches/{id}/chain')
def batch_chain(id: int, db=Depends(get_db), u=Depends(current_user)):
    return trace.batch_chain(db, id)


@router.post('/batches/{id}/sources', status_code=201)
def add_sources(id: int, data: SourceListInput, db=Depends(get_db), u=Depends(can_write)):
    batch = trace.add_sources(db, id, data.sources, u)
    db.commit()
    return trace.batch_dict(db, batch, detailed=True)


@router.post('/batches/{id}/handovers', status_code=201)
def add_handover(id: int, data: HandoverInput, db=Depends(get_db), u=Depends(can_write)):
    handover = trace.add_handover(db, id, data, u)
    db.commit()
    return trace.handover_dict(db, handover)


@router.post('/batches/{id}/inspection')
def inspect(id: int, data: InspectionInput, db=Depends(get_db), u=Depends(admin_only)):
    batch, recall = trace.set_inspection(db, id, data, u)
    return {
        'batch': trace.batch_dict(db, batch, detailed=True),
        'recall': trace.recall_dict(db, recall, detailed=True) if recall else None,
    }


@router.get('/handovers')
def handovers(db=Depends(get_db), u=Depends(current_user)):
    return [
        trace.handover_dict(db, handover)
        for handover in db.scalars(select(Handover).order_by(Handover.id.desc()).limit(500))
    ]


@router.get('/recalls')
def recalls(db=Depends(get_db), u=Depends(current_user)):
    return [
        trace.recall_dict(db, recall)
        for recall in db.scalars(select(Recall).order_by(Recall.id.desc()))
    ]


@router.post('/batches/{id}/recalls', status_code=201)
def request_recall(id: int, data: RecallInput, db=Depends(get_db), u=Depends(admin_only)):
    recall = trace.request_recall(db, id, data.request_key, data.reason, u)
    return trace.recall_dict(db, recall, detailed=True)


@router.get('/recalls/{id}')
def recall_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    recall = db.get(Recall, id)
    if not recall:
        raise HTTPException(404, '召回不存在')
    return trace.recall_dict(db, recall, detailed=True)


@router.post('/recalls/{id}/resume')
def resume_recall(id: int, db=Depends(get_db), u=Depends(admin_only)):
    recall = db.get(Recall, id)
    if not recall:
        raise HTTPException(404, '召回不存在')
    trace.scan_recall(recall.id)
    db.expire_all()
    recall = db.get(Recall, id)
    return trace.recall_dict(db, recall, detailed=True)


@router.get('/orders')
def orders(status: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    query = select(RecoveryOrder).order_by(RecoveryOrder.id.desc())
    if status:
        query = query.where(RecoveryOrder.status == status)
    return [trace.order_dict(db, order, detailed=True) for order in db.scalars(query)]


@router.get('/orders/{id}')
def order_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    order = db.get(RecoveryOrder, id)
    if not order:
        raise HTTPException(404, '追缴单不存在')
    return trace.order_dict(db, order, detailed=True)


@router.post('/orders/{id}/entries', status_code=201)
def add_entry(id: int, data: RecoveryEntryInput, db=Depends(get_db), u=Depends(can_write)):
    order = trace.add_recovery_entry(db, id, data, u)
    db.commit()
    return trace.order_dict(db, order, detailed=True)
