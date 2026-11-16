from pydantic import BaseModel, Field


class SourceInput(BaseModel):
    batch_id: int
    qty: int = Field(gt=0, le=100000)


class BatchInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    code: str = Field(default='', max_length=60)
    meal_date: str
    origin: str = Field(default='prepared')
    actual_qty: int | None = Field(default=None, gt=0, le=1000000)
    job_id: int | None = None
    sources: list[SourceInput] = Field(default_factory=list)


class SourceListInput(BaseModel):
    sources: list[SourceInput] = Field(min_length=1, max_length=50)


class HandoverInput(BaseModel):
    receiving_point: str = Field(min_length=1, max_length=120)
    qty: int = Field(gt=0, le=100000)
    handed_at: float | None = None
    operator: str = Field(default='', max_length=150)


class InspectionInput(BaseModel):
    result: str = Field(default='pending')
    note: str = Field(default='', max_length=300)
    request_key: str = Field(default='', max_length=160)


class RecallInput(BaseModel):
    request_key: str = Field(min_length=1, max_length=160)
    reason: str = Field(default='', max_length=300)


class RecoveryEntryInput(BaseModel):
    qty: int = Field(gt=0, le=100000)
    note: str = Field(default='', max_length=300)
