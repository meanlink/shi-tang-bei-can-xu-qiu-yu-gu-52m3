"""成品拆分/混装追溯与召回。

核心不变量：
* 每个来源批次的拆出份数 + 已交接份数不超过其实际份数；
* 来源关联保持有向无环；
* 冻结批次不再新增拆分/混装或交接；
* 每份交接事实最多计入一张追缴单（同一餐批同一收餐点共用），多起召回共享该单，
  累计追缴不超过原送出量；
* 召回按节点提交进度，中断后可从已冻结节点继续；request_key 重试沿用同一召回。
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from src.config.database import Session
from src.models import (
    BatchLink,
    Handover,
    Job,
    MealBatch,
    Recall,
    RecallBatch,
    RecallOrderLink,
    RecoveryEntry,
    RecoveryOrder,
    RecoveryOrderHandover,
    TraceLock,
    User,
)
from src.services.audit import record

PENDING = 'pending'
RECOVERING = 'recovering'
SETTLED = 'settled'
SCANNING = 'scanning'
COMPLETED = 'completed'


# ---------------------------------------------------------------------------
# 基础校验
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _new_code(prefix: str = 'MB') -> str:
    return f"{prefix}-{datetime.utcnow():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def _meal_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, '备餐日期须为 YYYY-MM-DD。') from exc


def _lock_graph(db: OrmSession) -> None:
    """来源关联的全局串行点。

    用一次真实写入抢占串行权：MySQL 下锁行，SQLite 下取得写锁，
    这样并发的成环检查不会各自读到旧图后同时通过。
    """
    result = db.execute(
        update(TraceLock).where(TraceLock.id == 1).values(updated_at=_now())
    )
    if result.rowcount == 0:
        try:
            with db.begin_nested():
                db.add(TraceLock(id=1, updated_at=_now()))
                db.flush()
        except IntegrityError:
            # 迁移或另一请求已补齐锁行。
            pass


def _lock_batches(db: OrmSession, batch_ids: Iterable[int]) -> dict[int, MealBatch]:
    """按主键顺序加行锁，避免并发登记时互相等待。"""
    locked: dict[int, MealBatch] = {}
    for batch_id in sorted(set(int(value) for value in batch_ids)):
        batch = db.execute(
            select(MealBatch).where(MealBatch.id == batch_id).with_for_update()
        ).scalar_one_or_none()
        if batch is None:
            raise HTTPException(404, f'餐批 {batch_id} 不存在')
        locked[batch.id] = batch
    return locked


def _outgoing_qty(db: OrmSession, batch_id: int) -> int:
    return int(db.scalar(
        select(func.coalesce(func.sum(BatchLink.qty), 0)).where(BatchLink.source_id == batch_id)
    ) or 0)


def _handed_qty(db: OrmSession, batch_id: int) -> int:
    return int(db.scalar(
        select(func.coalesce(func.sum(Handover.qty), 0)).where(Handover.batch_id == batch_id)
    ) or 0)


def _reserve(db: OrmSession, source: MealBatch, additional: int) -> None:
    """原子占用量：拆出与交接共用实备上限。

    条件更新在数据库端判定 used_qty + additional <= actual_qty，即使行锁被
    忽略（如 SQLite）也不会超发。
    """
    result = db.execute(
        update(MealBatch)
        .where(
            MealBatch.id == source.id,
            MealBatch.frozen.is_(False),
            MealBatch.used_qty + additional <= MealBatch.actual_qty,
        )
        .values(used_qty=MealBatch.used_qty + additional)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.refresh(source)
        if source.frozen:
            raise HTTPException(409, f'餐批 {source.code} 已冻结，不能继续流出')
        used = int(source.used_qty or 0) + additional
        raise HTTPException(
            409,
            f'餐批 {source.code} 来源总用量 {used} 份超过实备 {source.actual_qty} 份',
        )
    db.refresh(source)


def _assert_acyclic(db: OrmSession, source_id: int, target_id: int) -> None:
    if source_id == target_id:
        raise HTTPException(422, '拆分或混装的来源与目标不能是同一餐批')
    adjacency: dict[int, list[int]] = {}
    # 调用方已按主键顺序锁定相关餐批行，并发新增关联会在餐批行锁上串行化。
    for source, target in db.execute(
        select(BatchLink.source_id, BatchLink.target_id)
    ).all():
        adjacency.setdefault(int(source), []).append(int(target))
    # 新增 source -> target 后若成环，则从 target 可以走回 source。
    stack = [target_id]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if node == source_id:
            raise HTTPException(422, '来源关联会形成环，已拒绝登记')
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    if source_id in seen:
        raise HTTPException(422, '来源关联会形成环，已拒绝登记')


# ---------------------------------------------------------------------------
# 餐批登记
# ---------------------------------------------------------------------------

def _approved_suggestion_row(db: OrmSession, job_id: int, meal_date: str) -> dict:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, '分析任务不存在')
    if job.status != 'completed' or not job.approved:
        raise HTTPException(409, '实备餐批必须来自已完成且已批准的建议')
    rows = ((job.result or {}).get('table') or {}).get('rows') or []
    for row in rows:
        if str(row.get('date')) == meal_date:
            return row
    raise HTTPException(422, '备餐日期不在该已批准建议的预测范围内')


def _clean_name(value: str) -> str:
    name = (value or '').strip()
    if len(name) < 2:
        raise HTTPException(422, '餐批名称去除空格后至少需要两个字符')
    return name


def create_prepared_batch(db: OrmSession, data, user: User) -> MealBatch:
    meal_date = _meal_date(data.meal_date)
    name = _clean_name(data.name)
    if data.sources:
        raise HTTPException(422, '实备餐批不能同时登记拆分来源')
    if not data.job_id or not data.actual_qty:
        raise HTTPException(422, '实备餐批须关联已批准建议并填写实际备餐份数')
    row = _approved_suggestion_row(db, data.job_id, meal_date)
    code = (data.code or '').strip() or _new_code()
    if db.scalar(select(MealBatch.id).where(MealBatch.code == code)):
        raise HTTPException(409, '餐批编号已存在')
    batch = MealBatch(
        code=code,
        name=name,
        job_id=data.job_id,
        meal_date=meal_date,
        origin='prepared',
        actual_qty=int(data.actual_qty),
        suggested_qty=int(row['plan']) if row.get('plan') is not None else None,
        inspection='pending',
        created_by=user.id,
        created_at=_now(),
    )
    db.add(batch)
    db.flush()
    record(db, user.email, 'meal.batch.create', {
        'id': batch.id, 'code': code, 'origin': 'prepared', 'qty': batch.actual_qty,
    })
    return batch


def _normalize_sources(sources: Iterable) -> list[tuple[int, int]]:
    normalized: list[tuple[int, int]] = []
    seen: set[int] = set()
    for item in sources:
        if item.batch_id in seen:
            raise HTTPException(422, '同一来源餐批不能重复登记')
        seen.add(item.batch_id)
        if item.qty <= 0:
            raise HTTPException(422, '来源份数须为正整数')
        normalized.append((int(item.batch_id), int(item.qty)))
    if not normalized:
        raise HTTPException(422, '拆分或混装须至少登记一个来源餐批')
    return sorted(normalized)


def create_derived_batch(db: OrmSession, data, user: User) -> MealBatch:
    if data.origin not in ('split', 'mixed'):
        raise HTTPException(422, '来源关联批次须标记为拆分或混装')
    meal_date = _meal_date(data.meal_date)
    name = _clean_name(data.name)
    sources = _normalize_sources(data.sources)
    _lock_graph(db)
    locked = _lock_batches(db, [batch_id for batch_id, _ in sources])
    for batch_id, qty in sources:
        _reserve(db, locked[batch_id], qty)
    total = sum(qty for _, qty in sources)
    if data.actual_qty is not None and int(data.actual_qty) != total:
        raise HTTPException(422, f'目标批次份数须等于来源合计 {total} 份')
    code = (data.code or '').strip() or _new_code('SP' if data.origin == 'split' else 'MX')
    if db.scalar(select(MealBatch.id).where(MealBatch.code == code)):
        raise HTTPException(409, '餐批编号已存在')
    batch = MealBatch(
        code=code,
        name=name,
        job_id=None,
        meal_date=meal_date,
        origin=data.origin,
        actual_qty=total,
        suggested_qty=None,
        inspection='pending',
        created_by=user.id,
        created_at=_now(),
    )
    db.add(batch)
    db.flush()
    for source_id, qty in sources:
        db.add(BatchLink(
            source_id=source_id, target_id=batch.id, qty=qty,
            created_by=user.id, created_at=_now(),
        ))
    record(db, user.email, 'meal.batch.create', {
        'id': batch.id, 'code': code, 'origin': data.origin, 'qty': total,
        'sources': [source_id for source_id, _ in sources],
    })
    db.flush()
    return batch


def add_sources(db: OrmSession, target_id: int, sources: Iterable, user: User) -> MealBatch:
    normalized = _normalize_sources(sources)
    _lock_graph(db)
    locked = _lock_batches(db, [target_id] + [batch_id for batch_id, _ in normalized])
    target = locked[target_id]
    if target.origin not in ('split', 'mixed'):
        raise HTTPException(422, '只有拆分或混装餐批可以追加来源')
    if target.frozen:
        raise HTTPException(409, '目标餐批已冻结，不能追加来源')
    for source_id, qty in normalized:
        _reserve(db, locked[source_id], qty)
        _assert_acyclic(db, source_id, target.id)
        if db.scalar(select(BatchLink.id).where(
            BatchLink.source_id == source_id, BatchLink.target_id == target.id
        )):
            raise HTTPException(409, '该来源与本餐批的关联已存在，请合并份数后重新登记')
    added = sum(qty for _, qty in normalized)
    target.actual_qty = (target.actual_qty or 0) + added
    for source_id, qty in normalized:
        db.add(BatchLink(
            source_id=source_id, target_id=target.id, qty=qty,
            created_by=user.id, created_at=_now(),
        ))
    record(db, user.email, 'meal.batch.link', {
        'target': target.id,
        'sources': [source_id for source_id, _ in normalized],
        'qty': added,
    })
    db.flush()
    return target


# ---------------------------------------------------------------------------
# 交接
# ---------------------------------------------------------------------------

def add_handover(db: OrmSession, batch_id: int, data, user: User) -> Handover:
    batch = _lock_batches(db, [batch_id])[batch_id]
    if batch.frozen:
        raise HTTPException(409, '餐批已冻结，不能继续出餐交接')
    if isinstance(data.qty, bool) or int(data.qty) != data.qty or data.qty <= 0:
        raise HTTPException(422, '交接份数须为正整数')
    point = (data.receiving_point or '').strip()
    if not point:
        raise HTTPException(422, '收餐点不能为空')
    _reserve(db, batch, int(data.qty))
    handed_at = float(data.handed_at) if data.handed_at is not None else _now()
    if handed_at <= 0:
        raise HTTPException(422, '交接时间无效')
    handover = Handover(
        batch_id=batch.id,
        receiving_point=point[:120],
        qty=int(data.qty),
        handed_at=handed_at,
        operator=(data.operator or user.name).strip()[:150],
        created_at=_now(),
    )
    db.add(handover)
    db.flush()
    record(db, user.email, 'meal.handover.create', {
        'id': handover.id, 'batch': batch.id, 'point': handover.receiving_point,
        'qty': handover.qty,
    })
    return handover


# ---------------------------------------------------------------------------
# 召回扫描
# ---------------------------------------------------------------------------

def _ensure_recovery_order(db: OrmSession, handover: Handover, recall: Recall) -> RecoveryOrder:
    # 按“餐批 + 收餐点”建单；每份原交接事实通过关联表只计入一次。
    order = db.execute(
        select(RecoveryOrder).where(
            RecoveryOrder.batch_id == handover.batch_id,
            RecoveryOrder.receiving_point == handover.receiving_point,
        ).with_for_update()
    ).scalar_one_or_none()
    counted = db.scalar(select(RecoveryOrderHandover.id).where(
        RecoveryOrderHandover.handover_id == handover.id
    ))
    created_order = False
    if order is None:
        try:
            with db.begin_nested():
                order = RecoveryOrder(
                    batch_id=handover.batch_id,
                    source_recall_id=recall.id,
                    receiving_point=handover.receiving_point,
                    original_qty=handover.qty,
                    recovered_qty=0,
                    status='pending',
                    created_at=_now(),
                    updated_at=_now(),
                )
                db.add(order)
                db.flush()
            created_order = True
        except IntegrityError:
            order = db.execute(
                select(RecoveryOrder).where(
                    RecoveryOrder.batch_id == handover.batch_id,
                    RecoveryOrder.receiving_point == handover.receiving_point,
                ).with_for_update()
            ).scalar_one()
    if counted is None:
        try:
            with db.begin_nested():
                db.add(RecoveryOrderHandover(
                    order_id=order.id, handover_id=handover.id, qty=handover.qty,
                    counted_at=_now(),
                ))
                db.flush()
        except IntegrityError:
            # 并发扫描已计入同一交接，不加原送出量。
            pass
        else:
            if not created_order:
                # 同收餐点后续交接并入同一张单，仅计入尚未计入的份数。
                order.original_qty += handover.qty
            order.status = (
                SETTLED if order.recovered_qty == order.original_qty
                else ('pending' if order.recovered_qty == 0 else 'partial')
            )
            order.updated_at = _now()
    if db.scalar(select(RecallOrderLink.id).where(
        RecallOrderLink.recall_id == recall.id,
        RecallOrderLink.order_id == order.id,
    )) is None:
        try:
            with db.begin_nested():
                db.add(RecallOrderLink(
                    recall_id=recall.id, order_id=order.id, created_at=_now(),
                ))
                db.flush()
        except IntegrityError:
            # 另一并发 worker 已建立同一关联。
            pass
    return order


def _freeze_closure(db: OrmSession, recall: Recall) -> list[int]:
    """在召回受理事务内冻结根批次和全部后代，受影响餐批立即停止流出。

    调用前已取得来源关联串行锁，因此闭包读取期间不会有新关联写入。
    """
    adjacency: dict[int, list[int]] = {}
    for source, target in db.execute(
        select(BatchLink.source_id, BatchLink.target_id)
    ).all():
        adjacency.setdefault(int(source), []).append(int(target))
    closure: list[int] = []
    stack, seen = [recall.batch_id], set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        closure.append(node)
        stack.extend(adjacency.get(node, []))
    now = _now()
    for batch_id in sorted(closure):
        batch = db.execute(
            select(MealBatch).where(MealBatch.id == batch_id).with_for_update()
        ).scalar_one_or_none()
        if batch is None:
            continue
        if not batch.frozen:
            batch.frozen = True
            batch.frozen_at = now
            batch.frozen_by_recall_id = recall.id
        if db.scalar(select(RecallBatch.id).where(
            RecallBatch.recall_id == recall.id, RecallBatch.batch_id == batch.id
        )) is None:
            try:
                with db.begin_nested():
                    db.add(RecallBatch(
                        recall_id=recall.id, batch_id=batch.id,
                        is_root=(batch.id == recall.batch_id), frozen_at=now,
                    ))
                    db.flush()
            except IntegrityError:
                pass
    recall.updated_at = _now()
    return sorted(closure)


def _process_recall_node(db: OrmSession, recall: Recall, progress: dict) -> None:
    """在同一事务内冻结节点、登记追缴单并推进进度；崩溃则整个节点回滚。"""
    queue = list(progress.get('queue') or [])
    visited = set(progress.get('visited') or [])
    if not queue:
        progress['current'] = None
        progress['frozen'] = sorted(visited)
        return
    node_id = int(queue[0])
    batch = db.execute(
        select(MealBatch).where(MealBatch.id == node_id).with_for_update()
    ).scalar_one_or_none()
    if batch is not None:
        now = _now()
        if not batch.frozen:
            batch.frozen = True
            batch.frozen_at = now
            batch.frozen_by_recall_id = recall.id
        if db.scalar(select(RecallBatch.id).where(
            RecallBatch.recall_id == recall.id, RecallBatch.batch_id == batch.id
        )) is None:
            try:
                with db.begin_nested():
                    db.add(RecallBatch(
                        recall_id=recall.id, batch_id=batch.id,
                        is_root=(batch.id == recall.batch_id), frozen_at=now,
                    ))
                    db.flush()
            except IntegrityError:
                # 并发扫描已登记过该节点。
                pass
        children = [int(row[0]) for row in db.execute(
            select(BatchLink.target_id).where(BatchLink.source_id == batch.id)
        ).all()]
        # 扫描前已完成的交接也必须形成追缴单；扫描期间新交接被冻结拦截。
        handovers = list(db.execute(
            select(Handover).where(Handover.batch_id == batch.id).with_for_update()
        ).scalars())
        for handover in handovers:
            _ensure_recovery_order(db, handover, recall)
        visited.add(batch.id)
        for child in children:
            if child not in visited and child not in queue:
                queue.append(child)
    queue = queue[1:]
    progress.update({
        'queue': queue,
        'visited': sorted(visited),
        'frozen': sorted(visited),
        'orders': int(db.scalar(
            select(func.count()).select_from(RecallOrderLink).where(
                RecallOrderLink.recall_id == recall.id
            )
        ) or 0),
        'current': None,
    })
    recall.progress = dict(progress)
    recall.updated_at = _now()


def scan_recall(recall_id: int, max_nodes: int | None = None) -> None:
    """可重入扫描：逐节点提交，中断后保留已冻结节点与已建追缴单。"""
    processed = 0
    while max_nodes is None or processed < max_nodes:
        with Session() as db:
            recall = db.execute(
                select(Recall).where(Recall.id == recall_id).with_for_update()
            ).scalar_one_or_none()
            if recall is None or recall.status == COMPLETED:
                return
            progress = dict(recall.progress or {})
            if not (progress.get('queue')):
                visible = set(progress.get('visited') or [])
                # 已无待扫描节点；只要根批次被清查过即视为完成。
                if visible:
                    recall.status = COMPLETED
                    recall.finished_at = _now()
                    progress['current'] = None
                    progress['frozen'] = sorted(visible)
                    recall.progress = dict(progress)
                    recall.updated_at = _now()
                    record(db, 'system', 'meal.recall.complete', {
                        'recall': recall.id, 'frozen': len(visible),
                    })
                    db.commit()
                    return
                progress['queue'] = [recall.batch_id]
            _process_recall_node(db, recall, progress)
            db.commit()
            processed += 1


def request_recall(
    db: OrmSession,
    batch_id: int,
    request_key: str,
    reason: str,
    user: User,
    auto: bool = False,
) -> Recall:
    key = (request_key or '').strip()
    if not key:
        raise HTTPException(422, '召回请求须携带 request_key')
    existing = db.scalar(select(Recall).where(Recall.request_key == key))
    if existing:
        if existing.batch_id != batch_id:
            raise HTTPException(409, '同一 request_key 不能用于不同餐批的召回')
        db.commit()
        if existing.status != COMPLETED:
            scan_recall(existing.id)
            db.refresh(existing)
        return existing
    batch = db.get(MealBatch, batch_id)
    if not batch:
        raise HTTPException(404, '餐批不存在')
    if not auto and batch.inspection != 'failed':
        raise HTTPException(409, '仅检验不合格的餐批可以发起召回')
    _lock_graph(db)
    recall = Recall(
        batch_id=batch.id,
        request_key=key,
        reason=(reason or '').strip()[:300],
        status=SCANNING,
        progress={'queue': [batch.id], 'visited': [], 'frozen': [], 'orders': 0},
        created_by=user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(recall)
    db.flush()
    # 冻结闭包与召回请求同事务提交：清查未完成时受影响餐批也已停止流出。
    closure = _freeze_closure(db, recall)
    recall.progress = {
        **(recall.progress or {}),
        'frozen': closure,
    }
    try:
        db.commit()
    except IntegrityError:
        # 并发下同一 request_key 的另一次请求已写入；沿用已有召回。
        db.rollback()
        existing = db.scalar(select(Recall).where(Recall.request_key == key))
        if not existing:
            raise
        if existing.batch_id != batch_id:
            raise HTTPException(409, '同一 request_key 不能用于不同餐批的召回')
        recall = existing
    else:
        record(db, user.email, 'meal.recall.request', {
            'id': recall.id, 'batch': batch.id, 'key': key,
        })
        db.commit()
    scan_recall(recall.id)
    db.refresh(recall)
    return recall


def set_inspection(
    db: OrmSession, batch_id: int, data, user: User
) -> tuple[MealBatch, Recall | None]:
    if data.result not in ('pending', 'passed', 'failed'):
        raise HTTPException(422, '检验结果须为 pending、passed 或 failed')
    batch = _lock_batches(db, [batch_id])[batch_id]
    recall = None
    batch.inspection = data.result
    batch.inspection_note = (data.note or '')[:300]
    if data.result == 'failed' and not batch.frozen:
        # 检验不合格与冻结在同一事务完成，召回扫描开始前该批已停止流出。
        batch.frozen = True
        batch.frozen_at = _now()
    record(db, user.email, 'meal.batch.inspection', {
        'id': batch.id, 'result': data.result, 'note': batch.inspection_note,
    })
    db.commit()
    if data.result == 'failed':
        key = (data.request_key or '').strip() or f'inspection:{batch.id}'
        recall = request_recall(
            db, batch.id, key, data.note or '检验不合格召回', user, auto=True,
        )
    db.refresh(batch)
    return batch, recall


# ---------------------------------------------------------------------------
# 追缴登记
# ---------------------------------------------------------------------------

def add_recovery_entry(db: OrmSession, order_id: int, data, user: User) -> RecoveryOrder:
    if isinstance(data.qty, bool) or int(data.qty) != data.qty or data.qty <= 0:
        raise HTTPException(422, '追缴份数须为正整数')
    qty = int(data.qty)
    # 数据库端条件累加：并发分次登记也不会越过原送出量。
    result = db.execute(
        update(RecoveryOrder)
        .where(
            RecoveryOrder.id == order_id,
            RecoveryOrder.recovered_qty + qty <= RecoveryOrder.original_qty,
        )
        .values(
            recovered_qty=RecoveryOrder.recovered_qty + qty,
            status=case(
                (RecoveryOrder.recovered_qty + qty == RecoveryOrder.original_qty, SETTLED),
                else_='partial',
            ),
            updated_at=_now(),
        )
        .execution_options(synchronize_session=False)
    )
    order = db.get(RecoveryOrder, order_id)
    if result.rowcount != 1:
        if not order:
            raise HTTPException(404, '追缴单不存在')
        db.refresh(order)
        raise HTTPException(
            409,
            f'累计追缴 {order.recovered_qty + qty} 份超过原送出 {order.original_qty} 份',
        )
    db.add(RecoveryEntry(
        order_id=order.id, qty=qty, note=(data.note or '')[:300],
        actor=user.email, at=_now(),
    ))
    db.refresh(order)
    record(db, user.email, 'meal.recovery.entry', {
        'order': order.id, 'qty': qty, 'total': order.recovered_qty,
    })
    db.flush()
    return order


# ---------------------------------------------------------------------------
# 查询与序列化
# ---------------------------------------------------------------------------

def _recall_ids_for_batch(db: OrmSession, batch_id: int) -> list[int]:
    return [int(row[0]) for row in db.execute(
        select(RecallBatch.recall_id).where(RecallBatch.batch_id == batch_id)
    ).all()]


def _orders_for_recall(db: OrmSession, recall_id: int) -> list[RecoveryOrder]:
    return list(db.scalars(
        select(RecoveryOrder).join(
            RecallOrderLink, RecallOrderLink.order_id == RecoveryOrder.id
        ).where(RecallOrderLink.recall_id == recall_id).order_by(RecoveryOrder.id)
    ))


def recall_stage(db: OrmSession, recall: Recall) -> str:
    if recall.status != COMPLETED:
        return PENDING
    orders = _orders_for_recall(db, recall.id)
    if any(order.status != SETTLED for order in orders):
        return RECOVERING
    return SETTLED


def batch_trace_state(db: OrmSession, batch: MealBatch) -> str:
    recall_ids = _recall_ids_for_batch(db, batch.id)
    if not recall_ids:
        return PENDING
    recalls = list(db.scalars(select(Recall).where(Recall.id.in_(recall_ids))))
    stages = [recall_stage(db, recall) for recall in recalls]
    if PENDING in stages:
        return PENDING
    if RECOVERING in stages:
        return RECOVERING
    return SETTLED


def batch_dict(db: OrmSession, batch: MealBatch, detailed: bool = False) -> dict:
    outgoing = _outgoing_qty(db, batch.id)
    handed = _handed_qty(db, batch.id)
    sources = []
    for link in db.scalars(
        select(BatchLink).where(BatchLink.target_id == batch.id).order_by(BatchLink.id)
    ):
        source = db.get(MealBatch, link.source_id)
        sources.append({
            'batch_id': link.source_id,
            'qty': link.qty,
            'code': source.code if source else None,
            'name': source.name if source else None,
            'inspection': source.inspection if source else None,
        })
    handovers = []
    if detailed:
        for handover in db.scalars(
            select(Handover).where(Handover.batch_id == batch.id).order_by(Handover.id)
        ):
            handovers.append(handover_dict(db, handover))
    recalls = [
        {
            'id': recall.id,
            'request_key': recall.request_key,
            'status': recall.status,
            'stage': recall_stage(db, recall),
        }
        for recall in db.scalars(select(Recall).where(
            Recall.id.in_(_recall_ids_for_batch(db, batch.id) or [-1])
        ).order_by(Recall.id))
    ]
    return {
        'id': batch.id,
        'code': batch.code,
        'name': batch.name,
        'job_id': batch.job_id,
        'meal_date': batch.meal_date,
        'origin': batch.origin,
        'actual_qty': batch.actual_qty,
        'suggested_qty': batch.suggested_qty,
        'quantity': batch.actual_qty,
        'outgoing_qty': outgoing,
        'handed_qty': handed,
        'remaining_qty': max(0, (batch.actual_qty or 0) - outgoing - handed),
        'frozen': batch.frozen,
        'frozen_at': batch.frozen_at,
        'frozen_by_recall_id': batch.frozen_by_recall_id,
        'inspection': batch.inspection,
        'inspection_note': batch.inspection_note,
        'trace_state': batch_trace_state(db, batch),
        'sources': sources,
        'recalls': recalls,
        'handovers': handovers,
        'created_by': batch.created_by,
        'created_at': batch.created_at,
    }


def handover_dict(db: OrmSession, handover: Handover) -> dict:
    order = db.scalar(select(RecoveryOrder).where(
        RecoveryOrder.batch_id == handover.batch_id,
        RecoveryOrder.receiving_point == handover.receiving_point,
    ))
    batch = db.get(MealBatch, handover.batch_id)
    return {
        'id': handover.id,
        'batch_id': handover.batch_id,
        'batch_code': batch.code if batch else None,
        'receiving_point': handover.receiving_point,
        'qty': handover.qty,
        'handed_at': handover.handed_at,
        'operator': handover.operator,
        'created_at': handover.created_at,
        'recovery_order_id': order.id if order else None,
    }


def order_dict(db: OrmSession, order: RecoveryOrder, detailed: bool = False) -> dict:
    batch = db.get(MealBatch, order.batch_id)
    first = db.scalar(
        select(RecoveryOrderHandover).where(
            RecoveryOrderHandover.order_id == order.id
        ).order_by(RecoveryOrderHandover.id)
    )
    handover = db.get(Handover, first.handover_id) if first else None
    data = {
        'id': order.id,
        'batch_id': order.batch_id,
        'batch_code': batch.code if batch else None,
        'source_recall_id': order.source_recall_id,
        'receiving_point': order.receiving_point,
        'original_qty': order.original_qty,
        'recovered_qty': order.recovered_qty,
        'outstanding_qty': order.original_qty - order.recovered_qty,
        'status': order.status,
        'handed_at': handover.handed_at if handover else None,
        'operator': handover.operator if handover else None,
        'created_at': order.created_at,
        'updated_at': order.updated_at,
    }
    if detailed:
        data['entries'] = [
            {
                'id': entry.id, 'qty': entry.qty, 'note': entry.note,
                'actor': entry.actor, 'at': entry.at,
            }
            for entry in db.scalars(
                select(RecoveryEntry).where(
                    RecoveryEntry.order_id == order.id
                ).order_by(RecoveryEntry.id)
            )
        ]
        data['handover_ids'] = [
            int(row[0]) for row in db.execute(
                select(RecoveryOrderHandover.handover_id).where(
                    RecoveryOrderHandover.order_id == order.id
                ).order_by(RecoveryOrderHandover.id)
            ).all()
        ]
    return data


def recall_dict(db: OrmSession, recall: Recall, detailed: bool = False) -> dict:
    root = db.get(MealBatch, recall.batch_id)
    batch_ids = [int(row[0]) for row in db.execute(
        select(RecallBatch.batch_id).where(
            RecallBatch.recall_id == recall.id
        ).order_by(RecallBatch.id)
    ).all()]
    orders = _orders_for_recall(db, recall.id)
    data = {
        'id': recall.id,
        'batch_id': recall.batch_id,
        'batch_code': root.code if root else None,
        'batch_name': root.name if root else None,
        'request_key': recall.request_key,
        'reason': recall.reason,
        'status': recall.status,
        'stage': recall_stage(db, recall),
        'progress': recall.progress or {},
        'frozen_batch_ids': batch_ids,
        'frozen_count': len(batch_ids),
        'order_count': len(orders),
        'original_qty': sum(order.original_qty for order in orders),
        'outstanding_qty': sum(order.original_qty - order.recovered_qty for order in orders),
        'created_at': recall.created_at,
        'updated_at': recall.updated_at,
        'finished_at': recall.finished_at,
    }
    if detailed:
        data['frozen_batches'] = [
            batch_dict(db, batch, detailed=True)
            for batch in db.scalars(select(MealBatch).where(
                MealBatch.id.in_(batch_ids or [-1])
            ).order_by(MealBatch.id))
        ]
        data['orders'] = [order_dict(db, order, detailed=True) for order in orders]
    return data


def trace_overview(db: OrmSession) -> dict:
    batches = list(db.scalars(select(MealBatch)))
    states = {PENDING: 0, RECOVERING: 0, SETTLED: 0}
    for batch in batches:
        states[batch_trace_state(db, batch)] += 1
    recalls = list(db.scalars(select(Recall)))
    recall_states = {PENDING: 0, RECOVERING: 0, SETTLED: 0}
    for recall in recalls:
        recall_states[recall_stage(db, recall)] += 1
    orders = list(db.scalars(select(RecoveryOrder)))
    return {
        'batches': len(batches),
        'frozen_batches': sum(1 for batch in batches if batch.frozen),
        'failed_batches': sum(1 for batch in batches if batch.inspection == 'failed'),
        'pending_batches': states[PENDING],
        'recovering_batches': states[RECOVERING],
        'settled_batches': states[SETTLED],
        'recalls': len(recalls),
        'pending_recalls': recall_states[PENDING],
        'recovering_recalls': recall_states[RECOVERING],
        'settled_recalls': recall_states[SETTLED],
        'orders': len(orders),
        'original_qty': sum(order.original_qty for order in orders),
        'recovered_qty': sum(order.recovered_qty for order in orders),
        'outstanding_qty': sum(order.original_qty - order.recovered_qty for order in orders),
    }


def batch_chain(db: OrmSession, batch_id: int) -> dict:
    batch = db.get(MealBatch, batch_id)
    if not batch:
        raise HTTPException(404, '餐批不存在')
    links = list(db.execute(
        select(BatchLink.source_id, BatchLink.target_id, BatchLink.qty)
    ).all())
    parents: dict[int, list[tuple[int, int]]] = {}
    children: dict[int, list[tuple[int, int]]] = {}
    for source, target, qty in links:
        parents.setdefault(int(target), []).append((int(source), int(qty)))
        children.setdefault(int(source), []).append((int(target), int(qty)))
    cached: dict[int, MealBatch] = {batch.id: batch}

    def walk(start: int, adjacency) -> list[dict]:
        rows, stack, seen = [], [(start, 0)], {start}
        while stack:
            node_id, depth = stack.pop()
            for neighbor, qty in adjacency.get(node_id, []):
                if neighbor in seen or depth >= 64:
                    continue
                seen.add(neighbor)
                neighbor_batch = cached.get(neighbor) or db.get(MealBatch, neighbor)
                if neighbor_batch:
                    cached[neighbor] = neighbor_batch
                rows.append({
                    'batch_id': neighbor,
                    'code': neighbor_batch.code if neighbor_batch else None,
                    'name': neighbor_batch.name if neighbor_batch else None,
                    'origin': neighbor_batch.origin if neighbor_batch else None,
                    'qty': qty,
                    'depth': depth + 1,
                    'inspection': neighbor_batch.inspection if neighbor_batch else None,
                    'frozen': neighbor_batch.frozen if neighbor_batch else None,
                })
                stack.append((neighbor, depth + 1))
        rows.sort(key=lambda row: (row['depth'], row['batch_id']))
        return rows

    return {
        'batch': batch_dict(db, batch, detailed=True),
        'ancestors': walk(batch.id, parents),
        'descendants': walk(batch.id, children),
        'edges': [
            {'source_id': int(source), 'target_id': int(target), 'qty': int(qty)}
            for source, target, qty in links
        ],
    }
