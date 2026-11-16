from sqlalchemy import (
    CheckConstraint,
    Double,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Boolean,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class TraceLock(Base):
    """单行串行锁，保证并发的来源关联登记不会各自通过成环检查。"""

    __tablename__ = 'trace_locks'

    id: Mapped[int] = mapped_column(primary_key=True)
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)


class MealBatch(Base):
    """实备餐批。prepared 批次来自已批准建议，split/mixed 批次由来源关联生成。"""

    __tablename__ = 'meal_batches'
    __table_args__ = (
        CheckConstraint('actual_qty > 0', name='ck_meal_batch_qty'),
        CheckConstraint(
            "origin in ('prepared','split','mixed')", name='ck_meal_batch_origin'
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    job_id: Mapped[int | None] = mapped_column(ForeignKey('jobs.id'), nullable=True)
    meal_date: Mapped[str] = mapped_column(String(10), index=True)
    origin: Mapped[str] = mapped_column(String(12), default='prepared')
    actual_qty: Mapped[int] = mapped_column(Integer)
    used_qty: Mapped[int] = mapped_column(Integer, default=0)
    suggested_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    frozen_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    frozen_by_recall_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inspection: Mapped[str] = mapped_column(String(12), default='pending', index=True)
    inspection_note: Mapped[str] = mapped_column(String(300), default='')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)


class BatchLink(Base):
    """拆分/混装来源关联：来源批次向目标批次提供多少份。"""

    __tablename__ = 'batch_links'
    __table_args__ = (
        UniqueConstraint('source_id', 'target_id', name='uq_batch_link_pair'),
        CheckConstraint('source_id <> target_id', name='ck_batch_link_not_self'),
        CheckConstraint('qty > 0', name='ck_batch_link_qty'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('meal_batches.id'), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey('meal_batches.id'), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)


class Handover(Base):
    """原交接事实：只新增，不因召回而改写份数、收餐点或交接时间。"""

    __tablename__ = 'handovers'
    __table_args__ = (CheckConstraint('qty > 0', name='ck_handover_qty'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meal_batches.id'), index=True)
    receiving_point: Mapped[str] = mapped_column(String(120))
    qty: Mapped[int] = mapped_column(Integer)
    handed_at: Mapped[float] = mapped_column(Double)
    operator: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[float] = mapped_column(Double)


class Recall(Base):
    """一次召回请求。request_key 保证原请求重试沿用同一召回与追缴单。"""

    __tablename__ = 'recalls'

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meal_batches.id'), index=True)
    request_key: Mapped[str] = mapped_column(String(160), unique=True)
    reason: Mapped[str] = mapped_column(String(300), default='')
    status: Mapped[str] = mapped_column(String(20), default='scanning', index=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class RecallBatch(Base):
    """本次召回已清查并冻结的餐批（含根批次与全部后代）。"""

    __tablename__ = 'recall_batches'
    __table_args__ = (UniqueConstraint('recall_id', 'batch_id', name='uq_recall_batch'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recall_id: Mapped[int] = mapped_column(ForeignKey('recalls.id'), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meal_batches.id'), index=True)
    is_root: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen_at: Mapped[float] = mapped_column(Double)


class RecoveryOrder(Base):
    """逐收餐点追缴单：同一餐批同一收餐点共用一张单，多起召回共享，避免重复追缴。"""

    __tablename__ = 'recovery_orders'
    __table_args__ = (
        UniqueConstraint('batch_id', 'receiving_point', name='uq_recovery_order_point'),
        CheckConstraint('original_qty >= 0', name='ck_recovery_original_qty'),
        CheckConstraint('recovered_qty >= 0', name='ck_recovery_recovered_qty'),
        CheckConstraint('recovered_qty <= original_qty', name='ck_recovery_within_original'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meal_batches.id'), index=True)
    source_recall_id: Mapped[int] = mapped_column(ForeignKey('recalls.id'))
    receiving_point: Mapped[str] = mapped_column(String(120))
    original_qty: Mapped[int] = mapped_column(Integer)
    recovered_qty: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default='pending', index=True)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class RecoveryOrderHandover(Base):
    """追缴单已计入的交接事实，保证同一份交接跨召回只计一次。"""

    __tablename__ = 'recovery_order_handovers'
    __table_args__ = (UniqueConstraint('handover_id', name='uq_recovery_order_handover'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recovery_orders.id'), index=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey('handovers.id'))
    qty: Mapped[int] = mapped_column(Integer)
    counted_at: Mapped[float] = mapped_column(Double)


class RecallOrderLink(Base):
    """召回与追缴单的关联；同一追缴单可被多起召回涉及，但只追缴一次。"""

    __tablename__ = 'recall_orders'
    __table_args__ = (UniqueConstraint('recall_id', 'order_id', name='uq_recall_order'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recall_id: Mapped[int] = mapped_column(ForeignKey('recalls.id'), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recovery_orders.id'), index=True)
    created_at: Mapped[float] = mapped_column(Double)


class RecoveryEntry(Base):
    """分次追缴登记；追缴单累计字段保证合计不超过原送出量。"""

    __tablename__ = 'recovery_entries'
    __table_args__ = (CheckConstraint('qty > 0', name='ck_recovery_entry_qty'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recovery_orders.id'), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(300), default='')
    actor: Mapped[str] = mapped_column(String(150))
    at: Mapped[float] = mapped_column(Double)
