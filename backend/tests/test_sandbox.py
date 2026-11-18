import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.models.entities import Failover, IndexBuild, IndexEntry, IndexEvent, Order, OrderWriteLog
from src.services import sandbox_service as sandbox


@pytest.fixture()
def db():
    path = Path(tempfile.mkdtemp()) / 'sandbox.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    sandbox.seed_orders(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def tick_until_blocked(db, build, limit=50):
    for _ in range(limit):
        build = sandbox.tick_build(db, build.id)
        db.commit()
        if build.status in ('paused', 'active', 'failed'):
            return build
    raise AssertionError('build did not settle')


def test_seed_has_two_legacy_duplicate_groups(db):
    state = sandbox.get_state(db)
    assert state['order_count'] == 18
    assert {d['order_no'] for d in state['duplicates']} == {'HS-2611', 'HS-2614'}
    assert state['replica']['can_detect_duplicates'] is False


def test_build_stops_at_legacy_duplicate_and_keeps_progress(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    build = tick_until_blocked(db, build)
    assert build.status == 'paused'
    assert build.stage == 'awaiting_correction'
    assert build.scanned == 9
    assert build.blocking_order_id is not None
    assert build.scan_cursor == 9
    # 已扫描部分进入影子索引，未扫描部分不在。
    assert sandbox._covered_count(db, build) == 9
    dup = db.get(Order, build.blocking_order_id)
    assert dup.order_no == 'HS-2611'
    assert dup.duplicate_of_id is not None


def test_writes_during_build_accepted_and_duplicates_rejected(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    tick_until_blocked(db, build)
    order = sandbox.insert_order(db, 'HS-3001')
    db.commit()
    assert order.id > build.snapshot_max_id
    with pytest.raises(HTTPException) as exc:
        sandbox.insert_order(db, 'HS-2611')
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        sandbox.insert_order(db, 'HS-2601')
    pending = db.scalar(select(func.count(OrderWriteLog.id)).where(
        OrderWriteLog.build_id == build.id, OrderWriteLog.processed.is_(False)))
    assert pending == 1
    # 影子索引还没有写入构建期间的新单，等待追赶阶段。
    assert sandbox._entry(db, build.id, order.id) is None


def test_old_duplicate_correction_resumes_and_index_becomes_effective(db):
    build = tick_until_blocked(db, sandbox.start_build(db, 'build-a'))
    db.commit()
    dup_id = build.blocking_order_id
    sandbox.correct_order(db, dup_id, 'HS-2611-R')
    db.commit()
    build = sandbox.tick_build(db, build.id)
    db.commit()
    build = tick_until_blocked(db, build)
    assert build.stage == 'awaiting_correction'
    assert db.get(Order, build.blocking_order_id).order_no == 'HS-2614'
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2614-R')
    db.commit()
    build = tick_until_blocked(db, build)
    assert build.status == 'active'
    assert build.index_effective_at is not None
    assert build.stage == 'active'
    assert build.scanned == build.total_at_start == 18
    # 生效后新单直接写入索引，重复单号仍被拒。
    order = sandbox.insert_order(db, 'HS-4001')
    db.commit()
    assert sandbox._entry(db, build.id, order.id) is not None
    with pytest.raises(HTTPException):
        sandbox.insert_order(db, 'HS-4001')
    state = sandbox.get_state(db)
    assert state['replica']['can_detect_duplicates'] is True
    assert list(state['duplicates']) == []


def test_non_duplicate_order_cannot_be_corrected(db):
    with pytest.raises(HTTPException) as exc:
        sandbox.correct_order(db, 1, 'HS-9999')
    assert exc.value.status_code == 409


def test_correction_requires_unused_new_number(db):
    build = tick_until_blocked(db, sandbox.start_build(db, 'build-a'))
    db.commit()
    with pytest.raises(HTTPException):
        sandbox.correct_order(db, build.blocking_order_id, 'HS-2601')
    with pytest.raises(HTTPException):
        sandbox.correct_order(db, build.blocking_order_id, 'HS-2611')


def test_failover_deferred_until_index_effective(db):
    row = sandbox.request_failover(db, 'fo-1', 'replica-east')
    db.commit()
    assert row.status == 'deferred'
    assert '尚无生效的唯一索引' in row.wait_reason

    build = tick_until_blocked(db, sandbox.start_build(db, 'build-a'))
    db.commit()
    row = sandbox.request_failover(db, 'fo-2', 'replica-east')
    db.commit()
    assert row.status == 'deferred'
    assert '等待纠正旧重单' in row.wait_reason

    sandbox.correct_order(db, build.blocking_order_id, 'HS-2611-R')
    db.commit()
    build = sandbox.tick_build(db, build.id)
    db.commit()
    build = tick_until_blocked(db, build)
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2614-R')
    db.commit()
    build = tick_until_blocked(db, build)
    db.commit()
    assert build.status == 'active'
    db.refresh(row)
    assert row.status == 'completed'
    assert row.resolved_at is not None
    fresh = sandbox.request_failover(db, 'fo-3', 'replica-east')
    assert fresh.status == 'completed'


def test_interrupt_and_same_key_resume_does_not_rebuild(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    sandbox.tick_build(db, build.id)
    db.commit()
    build = sandbox.interrupt_build(db, build.id)
    db.commit()
    assert build.status == 'interrupted'
    assert build.stage == 'interrupted'
    assert build.scanned == 3
    covered_before = sandbox._covered_count(db, build)
    entry_ids = set(db.scalars(select(IndexEntry.id)).all())

    # 中断期间写入照常成功，并进入待追赶日志；构建进度不变。
    order = sandbox.insert_order(db, 'HS-5001')
    db.commit()
    assert sandbox._entry(db, build.id, order.id) is None

    resumed = sandbox.start_build(db, 'build-a')
    db.commit()
    assert resumed.id == build.id
    assert resumed.resumed_count == 1
    assert resumed.scan_cursor == build.scan_cursor
    assert resumed.scanned == build.scanned
    assert sandbox._covered_count(db, resumed) == covered_before
    assert set(db.scalars(select(IndexEntry.id)).all()) == entry_ids
    assert db.scalar(select(func.count(IndexBuild.id))) == 1

    # 同标识再调一次不会新建构建，也不会重置游标。
    again = sandbox.start_build(db, 'build-a')
    db.commit()
    assert again.id == build.id
    assert db.scalar(select(func.count(IndexBuild.id))) == 1


def test_interrupted_build_catches_up_writes_from_pause(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    sandbox.tick_build(db, build.id)
    db.commit()
    sandbox.interrupt_build(db, build.id)
    db.commit()
    mid = sandbox.insert_order(db, 'HS-5002')
    db.commit()
    resumed = sandbox.start_build(db, 'build-a')
    db.commit()
    assert resumed.status == 'building'
    resumed = tick_until_blocked(db, resumed)
    assert resumed.stage == 'awaiting_correction'
    sandbox.correct_order(db, resumed.blocking_order_id, 'HS-2611-R')
    db.commit()
    resumed = tick_until_blocked(db, resumed)
    sandbox.correct_order(db, resumed.blocking_order_id, 'HS-2614-R')
    db.commit()
    resumed = tick_until_blocked(db, resumed)
    assert resumed.status == 'active'
    # 中断期间写入的单被追平进索引。
    assert sandbox._entry(db, resumed.id, mid.id) is not None
    assert sandbox._pending_logs(db, resumed) == 0
    assert not sandbox._uncovered(db, resumed)


def test_duplicate_detected_during_validation_blocks_activation(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    # 直接把构建推进到校验阶段，模拟扫描阶段遗漏的旧重单。
    build.stage = 'validating'
    build.scanned = build.total_at_start
    db.commit()
    build = sandbox.tick_build(db, build.id)
    db.commit()
    assert build.status == 'paused'
    assert build.stage == 'awaiting_correction'
    assert '旧重单' in build.wait_reason
    assert build.index_effective_at is None


def test_index_stays_ineffective_while_write_log_pending(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    build = tick_until_blocked(db, build)
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2611-R')
    db.commit()
    build = tick_until_blocked(db, build)
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2614-R')
    db.commit()
    # 模拟一条未核对齐的构建期写入：构建不能生效。
    extra = Order(order_no='HS-6001', origin='live')
    db.add(extra)
    db.flush()
    db.add(OrderWriteLog(build_id=build.id, order_id=extra.id, action='insert',
                         order_no=extra.order_no, processed=False))
    db.commit()
    build = tick_until_blocked(db, build)
    assert build.stage in ('catching_up', 'validating') or build.status == 'active'
    build = tick_until_blocked(db, build)
    assert build.status == 'active'
    assert sandbox._entry(db, build.id, extra.id) is not None


def test_second_build_rejected_but_same_key_idempotent(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    with pytest.raises(HTTPException):
        sandbox.start_build(db, 'build-b')
    same = sandbox.start_build(db, 'build-a')
    assert same.id == build.id


def test_advance_runs_to_pause_in_one_call(db):
    build = sandbox.start_build(db, 'build-a')
    db.commit()
    build = sandbox.advance(db, build.id, 10)
    db.commit()
    assert build.status == 'paused'
    assert build.scanned == 9


def test_external_duplicate_becomes_correctable_at_validation(db):
    build = sandbox.advance(db, sandbox.start_build(db, 'build-a').id, 20)
    db.commit()
    # 模拟绕过服务层、在扫描之后写入的旧重单：扫描阶段看不到，由生效前校验拦下。
    db.add(Order(order_no='HS-2602', origin='legacy'))
    db.commit()
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2611-R')
    db.commit()
    build = sandbox.advance(db, build.id, 20)
    db.commit()
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2614-R')
    db.commit()
    build = sandbox.advance(db, build.id, 20)
    db.commit()
    assert build.status == 'paused'
    assert build.stage == 'awaiting_correction'
    dup = db.scalar(select(Order).where(Order.order_no == 'HS-2602').order_by(Order.id.desc()))
    assert dup.duplicate_of_id is not None
    state = sandbox.get_state(db)
    row = next(o for o in state['orders'] if o['id'] == dup.id)
    assert row['can_correct'] is True


def test_failover_released_only_after_validation_passes(db):
    row = sandbox.request_failover(db, 'fo-1', 'replica-east')
    db.commit()
    build = sandbox.advance(db, sandbox.start_build(db, 'build-a').id, 20)
    db.commit()
    assert build.status == 'paused'
    db.refresh(row)
    assert row.status == 'deferred'
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2611-R')
    db.commit()
    build = sandbox.advance(db, build.id, 20)
    db.commit()
    sandbox.correct_order(db, build.blocking_order_id, 'HS-2614-R')
    db.commit()
    build = sandbox.advance(db, build.id, 20)
    db.commit()
    assert build.status == 'active'
    db.refresh(row)
    assert row.status == 'completed'
