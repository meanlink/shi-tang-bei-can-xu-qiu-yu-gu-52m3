from sqlalchemy import select
from src.config.database import Base, Session, engine
from src import models
def migrate():
    Base.metadata.create_all(engine)
    # 追溯来源关联依赖固定的串行锁行；启动时幂等补齐。
    with Session() as db:
        if db.get(models.TraceLock, 1) is None:
            db.add(models.TraceLock(id=1, updated_at=0.0))
            db.commit()
if __name__=='__main__':migrate()
