import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, MealBatch, Handover, Recall
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import trace as trace_service
from src.utils.security import hash_password
from src.utils.serialization import checksum


def _seed_trace(db, job, admin):
    """幂等生成一条可追溯的演示链：实备 → 拆分/混装 → 交接 → 检验不合格召回。"""
    if db.scalar(select(func.count()).select_from(MealBatch)):
        return
    rows = (job.result or {}).get('table', {}).get('rows') or []
    if not rows:
        return
    date = rows[0]['date']
    plan = int(rows[0]['plan'])

    class Data:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    prepared = trace_service.create_prepared_batch(db, Data(
        name='演示 · 周三午餐实备', code='DEMO-PREP-01', meal_date=date,
        origin='prepared', job_id=job.id, actual_qty=plan, sources=[],
    ), admin)
    split = trace_service.create_derived_batch(db, Data(
        name='演示 · 研发区拆分盒餐', code='DEMO-SPLIT-01', meal_date=date,
        origin='split', actual_qty=None,
        sources=[Data(batch_id=prepared.id, qty=max(10, plan // 5))],
    ), admin)
    mixed = trace_service.create_derived_batch(db, Data(
        name='演示 · 会议混装餐', code='DEMO-MIX-01', meal_date=date,
        origin='mixed', actual_qty=None,
        sources=[
            Data(batch_id=prepared.id, qty=8),
            Data(batch_id=split.id, qty=6),
        ],
    ), admin)
    for batch, point, qty in [
        (prepared, '一号楼取餐点', 20),
        (split, '研发区取餐点', 15),
        (mixed, '会议中心取餐点', 10),
    ]:
        trace_service.add_handover(db, batch.id, Data(
            receiving_point=point, qty=qty, handed_at=time.time() - 5400, operator='演示交接',
        ), admin)
    db.commit()
    trace_service.set_inspection(db, prepared.id, Data(
        result='failed', note='演示：中心温度复检不合格', request_key='demo-recall-prep',
    ), admin)
    orders = list(db.scalars(select(trace_service.RecoveryOrder).where(
        trace_service.RecoveryOrder.source_recall_id.is_not(None)
    )))
    if orders:
        trace_service.add_recovery_entry(db, orders[-1].id, Data(
            qty=min(4, orders[-1].original_qty), note='演示：首批退回',
        ), admin)
    db.commit()


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        job=db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name']))
        if not job:
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
            job=j
        db.commit()
        admin=db.scalar(select(User).where(User.role=='admin'))
        _seed_trace(db, job, admin)
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,MealBatch,Handover,Recall]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
