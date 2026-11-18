from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.config.database import get_db
from src.controllers.actions import audit
from src.middlewares.auth import admin, current_user, writable
from src.services import sandbox_service as sandbox
from src.utils.serialize import record

router = APIRouter()


class OrderInput(BaseModel):
    order_no: str = Field(min_length=1, max_length=80)


class CorrectionInput(BaseModel):
    order_no: str = Field(min_length=1, max_length=80)


class BuildInput(BaseModel):
    attempt_key: str = Field(min_length=1, max_length=80)


class TickInput(BaseModel):
    steps: int = Field(default=1, ge=1, le=60)


class FailoverInput(BaseModel):
    attempt_key: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=80)


@router.get('/sandbox')
def sandbox_state(db=Depends(get_db), user=Depends(current_user)):
    return sandbox.get_state(db)


@router.post('/sandbox/orders', status_code=201)
def create_order(data: OrderInput, db=Depends(get_db), user=Depends(writable)):
    order = sandbox.insert_order(db, data.order_no)
    audit(db, user, f'order:insert:{order.id}:{order.order_no}')
    db.commit()
    return record(order)


@router.post('/sandbox/orders/{order_id}/correct')
def correct_order(order_id: int, data: CorrectionInput, db=Depends(get_db), user=Depends(writable)):
    order = sandbox.correct_order(db, order_id, data.order_no)
    audit(db, user, f'order:correct:{order.id}:{order.order_no}')
    db.commit()
    return record(order)


@router.post('/sandbox/builds', status_code=201)
def start_build(data: BuildInput, db=Depends(get_db), user=Depends(writable)):
    build = sandbox.start_build(db, data.attempt_key)
    audit(db, user, f'index_build:start:{build.id}:{build.attempt_key}')
    db.commit()
    db.refresh(build)
    return sandbox.build_view(db, build)


@router.post('/sandbox/builds/{build_id}/tick')
def tick_build(build_id: int, data: TickInput | None = None, db=Depends(get_db), user=Depends(writable)):
    build = sandbox.advance(db, build_id, data.steps if data else 1)
    audit(db, user, f'index_build:tick:{build.id}')
    db.commit()
    db.refresh(build)
    return sandbox.build_view(db, build)


@router.post('/sandbox/builds/{build_id}/interrupt')
def interrupt_build(build_id: int, db=Depends(get_db), user=Depends(writable)):
    build = sandbox.interrupt_build(db, build_id)
    audit(db, user, f'index_build:interrupt:{build.id}')
    db.commit()
    db.refresh(build)
    return sandbox.build_view(db, build)


@router.post('/sandbox/failovers', status_code=201)
def request_failover(data: FailoverInput, db=Depends(get_db), user=Depends(writable)):
    row = sandbox.request_failover(db, data.attempt_key, data.target)
    audit(db, user, f'failover:request:{row.id}:{row.status}')
    db.commit()
    db.refresh(row)
    return record(row)


@router.post('/sandbox/replica-check')
def replica_check(data: OrderInput, db=Depends(get_db), user=Depends(current_user)):
    return sandbox.replica_check(db, data.order_no)


@router.post('/sandbox/reset')
def reset_sandbox(db=Depends(get_db), user=Depends(admin)):
    state = sandbox.reset_sandbox(db)
    audit(db, user, 'sandbox:reset')
    db.commit()
    return state
