"""在线唯一索引构建沙盘。

模型说明（不连接真实复制集群）：
- orders 是业务写入表，存量数据里允许存在历史重复单号（origin=legacy）。
- index_builds 表示一次在线建唯一索引：先按 id 顺序扫描存量，再追平构建期间的写入，
  最后校验「旧重单已清完」且「构建期间写入已核对齐」，才会把索引标记为生效。
- index_entries 是影子索引。构建未生效前，副本只能看到已扫描/已双写的单号，
  因此副本接不住防重时，切主请求一律暂缓。
- 新写入的单号在服务层预检，已有单号（含旧重单）一律拒绝；其他单号照常写入，
  构建期间同时写入变更日志，供追赶阶段核对。
"""
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from src.models.entities import (Failover, IndexBuild, IndexEntry, IndexEvent, Order,
                                 OrderWriteLog, SandboxLock, now)

BATCH_SIZE = 3
IN_PROGRESS = ('queued', 'building', 'paused', 'interrupted')
STAGE_LABELS = {'scanning': '扫描存量', 'awaiting_correction': '等待纠正旧重单',
                'catching_up': '追平构建期写入', 'validating': '校验一致性',
                'active': '已生效', 'interrupted': '已中断'}


def _event(db, build_id, kind, message):
    db.add(IndexEvent(build_id=build_id, kind=kind, message=message))


def _lock_writes(db):
    """主库写入串行化：同一事务内锁住单号预检，避免并发写入绕过查重。

    这是主库的防重门闩；副本没有这层服务逻辑，只能依赖已生效的唯一索引，
    因此索引未生效时切换过去会收下重复单号。
    """
    dialect = db.get_bind().dialect.name if db.get_bind() is not None else 'mysql'
    stamp = now()
    if dialect in ('mysql', 'mariadb'):
        db.execute(text("INSERT INTO sandbox_locks (name, touched_at) VALUES ('orders-write', :ts) "
                        "ON DUPLICATE KEY UPDATE touched_at = :ts"), {'ts': stamp})
        db.execute(text("SELECT id FROM sandbox_locks WHERE name = 'orders-write' FOR UPDATE"))
    else:
        probe = db.scalar(select(SandboxLock).where(SandboxLock.name == 'orders-write'))
        if probe is None:
            db.add(SandboxLock(name='orders-write', touched_at=stamp))
            db.flush()
        else:
            probe.touched_at = stamp
            db.flush()


def _in_progress_build(db):
    return db.scalar(select(IndexBuild).where(IndexBuild.status.in_(IN_PROGRESS)).order_by(IndexBuild.id.desc()))


def _active_build(db):
    return db.scalar(select(IndexBuild).where(IndexBuild.status == 'active',
                                               IndexBuild.index_effective_at.is_not(None)).order_by(IndexBuild.id.desc()))


def _latest_build(db):
    return db.scalar(select(IndexBuild).order_by(IndexBuild.id.desc()).limit(1))


def _entry(db, build_id, order_id):
    return db.scalar(select(IndexEntry).where(IndexEntry.build_id == build_id,
                                              IndexEntry.order_id == order_id))


def _duplicate_groups(db):
    rows = db.execute(select(Order.order_no, func.count(Order.id), func.min(Order.id))
                      .group_by(Order.order_no).having(func.count(Order.id) > 1)
                      .order_by(func.min(Order.id))).all()
    return [{'order_no': r[0], 'count': int(r[1]), 'canonical_id': int(r[2])} for r in rows]


def _mark_duplicates(db):
    """把同组里 id 更大的重复单标记为可纠正（含外部直接写入的重复单）。"""
    groups = _duplicate_groups(db)
    for group in groups:
        rows = db.scalars(select(Order).where(Order.order_no == group['order_no'])
                          .order_by(Order.id)).all()
        for dup in rows[1:]:
            if dup.duplicate_of_id is None:
                dup.duplicate_of_id = rows[0].id
                if dup.original_order_no is None:
                    dup.original_order_no = dup.order_no
        group['blocking_id'] = rows[-1].id
    return groups


def _uncovered(db, build):
    if build is None:
        return []
    rows = db.execute(select(Order).outerjoin(
        IndexEntry, (IndexEntry.build_id == build.id) & (IndexEntry.order_id == Order.id))
        .where(IndexEntry.id.is_(None)).order_by(Order.id)).scalars().all()
    return rows


def _mismatched(db, build):
    if build is None:
        return []
    rows = db.execute(select(IndexEntry, Order).join(Order, Order.id == IndexEntry.order_id)
                      .where(IndexEntry.build_id == build.id,
                             IndexEntry.order_no != Order.order_no)).all()
    return rows


def _covered_count(db, build):
    if build is None:
        return 0
    return int(db.scalar(select(func.count(IndexEntry.id)).where(IndexEntry.build_id == build.id)) or 0)


def _pending_logs(db, build):
    if build is None:
        return 0
    return int(db.scalar(select(func.count(OrderWriteLog.id)).where(
        OrderWriteLog.build_id == build.id, OrderWriteLog.processed.is_(False))) or 0)


def build_view(db, build):
    if build is None:
        return None
    covered = _covered_count(db, build)
    total = build.total_at_start or int(db.scalar(select(func.count(Order.id))) or 0)
    return {
        'id': build.id, 'attempt_key': build.attempt_key, 'status': build.status,
        'stage': build.stage, 'resume_stage': build.resume_stage,
        'stage_label': STAGE_LABELS.get(build.stage, build.stage),
        'snapshot_max_id': build.snapshot_max_id, 'total_at_start': build.total_at_start,
        'scan_cursor': build.scan_cursor, 'scanned': build.scanned,
        'progress': round(100 * build.scanned / total, 1) if total else 0,
        'wait_reason': build.wait_reason, 'blocking_order_id': build.blocking_order_id,
        'index_effective_at': build.index_effective_at, 'interrupted_at': build.interrupted_at,
        'resumed_count': build.resumed_count, 'created_at': build.created_at,
        'updated_at': build.updated_at, 'index_entries': covered,
        'duplicates_remaining': len(_duplicate_groups(db)),
        'pending_write_logs': _pending_logs(db, build),
    }


def replica_view(db):
    """副本视角：只读影子索引，能否拒绝重复单号。"""
    active = _active_build(db)
    if active:
        return {'can_detect_duplicates': True, 'index_build_id': active.id, 'coverage': '全量',
                'reason': '唯一索引已生效，副本按索引拒绝重复单号。'}
    build = _in_progress_build(db) or _latest_build(db)
    if build is None:
        return {'can_detect_duplicates': False, 'index_build_id': None, 'coverage': '0',
                'reason': '尚无在线唯一索引，副本无法识别重复单号。'}
    total = build.total_at_start or int(db.scalar(select(func.count(Order.id))) or 0)
    covered = _covered_count(db, build)
    return {'can_detect_duplicates': False, 'index_build_id': build.id,
            'coverage': f'{covered}/{total}',
            'reason': f"索引{STAGE_LABELS.get(build.stage, build.stage)}（第 {build.scan_cursor} 行，已覆盖 {covered} 条），副本尚不能拒绝全部重复单号。"}


def _failover_gate(db):
    active = _active_build(db)
    if active:
        return True, '唯一索引已生效，副本可按索引拒绝重复单号。', active
    build = _in_progress_build(db) or _latest_build(db)
    if build is None:
        return False, '尚无生效的唯一索引，副本接不住防重，暂缓切主。', None
    if build.status == 'interrupted':
        reason = (f"索引构建在{STAGE_LABELS.get(build.resume_stage, build.resume_stage)}阶段中断"
                  f"（已扫描 {build.scanned}/{build.total_at_start}），副本接不住防重，暂缓切主。")
    elif build.status == 'failed':
        reason = '最近一次索引构建失败，副本接不住防重，暂缓切主。'
    else:
        reason = (f"索引{STAGE_LABELS.get(build.stage, build.stage)}（已扫描 {build.scanned}/{build.total_at_start}），"
                  f"副本尚不能拒绝重复单号，暂缓切主。")
    return False, reason, build


# --------------------------------------------------------------------------
# 写单
# --------------------------------------------------------------------------
def _order_no(value):
    value = (value or '').strip()
    if not value:
        raise HTTPException(422, '单号不能为空')
    if len(value) > 80:
        raise HTTPException(422, '单号最长 80 个字符')
    return value


def _reject_duplicate(db, order_no):
    query = select(Order).where(Order.order_no == order_no).order_by(Order.id)
    dialect = db.get_bind().dialect.name if db.get_bind() is not None else 'mysql'
    if dialect not in ('sqlite',):
        # 锁定读，确保看到锁释放前已提交的单号，避免并发写入绕过查重。
        query = query.with_for_update()
    existing = db.scalar(query)
    if existing:
        raise HTTPException(409, f'单号 {order_no} 已存在（订单 #{existing.id}），重复单号已拒绝')


def insert_order(db, order_no, origin='live'):
    order_no = _order_no(order_no)
    _lock_writes(db)
    _reject_duplicate(db, order_no)
    order = Order(order_no=order_no, origin=origin)
    db.add(order)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f'单号 {order_no} 已存在，重复单号已拒绝')
    build = _active_build(db) or _in_progress_build(db)
    if build is None:
        _event(db, None, 'order_insert', f"写入订单 #{order.id} 单号 {order.order_no}。")
    elif build.status == 'active':
        db.add(IndexEntry(build_id=build.id, order_id=order.id, order_no=order.order_no))
        db.add(OrderWriteLog(build_id=build.id, order_id=order.id, action='insert',
                             order_no=order.order_no, processed=True, processed_at=now()))
        _event(db, build.id, 'order_insert', f"索引已生效，订单 #{order.id} 单号 {order.order_no} 直接写入唯一索引。")
    else:
        # 构建中（含等待纠正/中断）：业务写照常落库，变更记入日志，由追赶阶段核对。
        db.add(OrderWriteLog(build_id=build.id, order_id=order.id, action='insert',
                             order_no=order.order_no, processed=False))
        _event(db, build.id, 'order_insert',
               f"构建期间写入订单 #{order.id} 单号 {order.order_no}，已记入待核对日志。")
    db.flush()
    return order


def correct_order(db, order_id, new_order_no):
    _lock_writes(db)
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, '订单不存在')
    if order.duplicate_of_id is None:
        raise HTTPException(409, '只有历史重复单号可以纠正；其他订单保持原单号')
    new_order_no = _order_no(new_order_no)
    if new_order_no == order.order_no:
        raise HTTPException(409, '新单号必须与原单号不同')
    clash = db.scalar(select(Order).where(Order.order_no == new_order_no, Order.id != order.id))
    if clash:
        raise HTTPException(409, f'新单号 {new_order_no} 已被订单 #{clash.id} 占用')
    previous = order.order_no
    if order.original_order_no is None:
        order.original_order_no = previous
    order.order_no = new_order_no
    order.corrected_at = now()
    builds = db.scalars(select(IndexBuild).where(IndexBuild.status.in_(IN_PROGRESS + ('active',)))).all()
    for build in builds:
        entry = _entry(db, build.id, order.id)
        if entry is not None:
            entry.order_no = new_order_no
        processing = build.status in IN_PROGRESS
        db.add(OrderWriteLog(build_id=build.id, order_id=order.id, action='correct',
                             previous_order_no=previous, order_no=new_order_no,
                             processed=not processing,
                             processed_at=None if processing else now()))
        if build.status == 'paused' and build.stage == 'awaiting_correction' and build.blocking_order_id == order.id:
            resume = build.resume_stage or 'scanning'
            build.status = 'building'
            build.stage = resume
            build.blocking_order_id = None
            build.wait_reason = (f"旧重单 #{order.id} 已由 {previous} 纠正为 {new_order_no}，"
                                 f"从{STAGE_LABELS.get(resume, resume)}阶段继续，游标第 {build.scan_cursor} 行。")
            _event(db, build.id, 'resume_after_correction', build.wait_reason)
        else:
            _event(db, build.id, 'order_correct', f"旧重单 #{order.id} 由 {previous} 纠正为 {new_order_no}。")
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f'新单号 {new_order_no} 与索引中已有单号冲突')
    return order


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------
def start_build(db, attempt_key):
    attempt_key = (attempt_key or '').strip()
    # 与写单共用同一把锁：冻结快照与「是否记变更日志」不会在并发下错位。
    _lock_writes(db)
    if not attempt_key or len(attempt_key) > 80:
        raise HTTPException(422, '构建标识必须为 1-80 个字符')
    existing = db.scalar(select(IndexBuild).where(IndexBuild.attempt_key == attempt_key))
    if existing:
        # 同一次构建的重试：只恢复进度，不新建记录、不重复填充索引。
        if existing.status == 'interrupted':
            existing.status = 'building'
            existing.stage = existing.resume_stage or 'scanning'
            existing.resumed_count += 1
            existing.interrupted_at = None
            existing.wait_reason = (f"按原标识 {attempt_key} 从{STAGE_LABELS.get(existing.stage, existing.stage)}阶段恢复："
                                    f"游标停在第 {existing.scan_cursor} 行，已保留 {existing.scanned} 条索引条目，不重复构建。")
            existing.updated_at = now()
            _event(db, existing.id, 'resume', existing.wait_reason)
            db.flush()
            return existing
        if existing.status == 'paused':
            existing.wait_reason = existing.wait_reason or '构建暂停中，处理阻塞项后自动继续。'
            db.flush()
            return existing
        if existing.status == 'active':
            existing.wait_reason = '唯一索引已生效，同标识重试不会重复构建。'
            db.flush()
            return existing
        if existing.status == 'failed':
            raise HTTPException(409, f'构建 #{existing.id} 已失败（{existing.wait_reason}）；请用新的构建标识重试')
        existing.wait_reason = f'构建 #{existing.id} 已在执行（{STAGE_LABELS.get(existing.stage, existing.stage)}），同标识重试不会重复建索引。'
        db.flush()
        return existing
    active = _active_build(db)
    if active:
        raise HTTPException(409, f'唯一索引已生效（构建 #{active.id}），无需重复构建')
    running = _in_progress_build(db)
    if running:
        raise HTTPException(409, f'已有索引构建 #{running.id}（{STAGE_LABELS.get(running.stage)}）在进行，请等待或中断后重试')
    snapshot_max_id = int(db.scalar(select(func.max(Order.id))) or 0)
    total = int(db.scalar(select(func.count(Order.id)).where(Order.id <= snapshot_max_id)) or 0)
    build = IndexBuild(attempt_key=attempt_key, status='building', stage='scanning',
                       snapshot_max_id=snapshot_max_id, total_at_start=total,
                       wait_reason=f'开始在线建索引：共 {total} 条存量，按每批 {BATCH_SIZE} 条扫描。')
    db.add(build)
    db.flush()
    _event(db, build.id, 'build_start', f'开始构建唯一索引，存量 {total} 条，快照上界 id≤{snapshot_max_id}，批次 {BATCH_SIZE}。')
    db.flush()
    return build


def _scan_batch(db, build):
    rows = db.scalars(select(Order).where(Order.id > build.scan_cursor,
                                        Order.id <= build.snapshot_max_id)
                      .order_by(Order.id).limit(BATCH_SIZE)).all()
    if not rows:
        build.stage = 'catching_up'
        build.wait_reason = '存量扫描完成，追平构建期间写入。'
        build.updated_at = now()
        _event(db, build.id, 'scan_done', build.wait_reason)
        db.flush()
        return build
    for order in rows:
        clash = db.scalar(select(IndexEntry).where(IndexEntry.build_id == build.id,
                                                   IndexEntry.order_no == order.order_no,
                                                   IndexEntry.order_id != order.id))
        if clash:
            if order.duplicate_of_id is None:
                order.duplicate_of_id = clash.order_id
                order.original_order_no = order.order_no
            build.status = 'paused'
            build.resume_stage = 'scanning'
            build.stage = 'awaiting_correction'
            build.blocking_order_id = order.id
            build.wait_reason = (f"扫描到订单 #{order.id}（{order.order_no}）与 #{clash.order_id} 重复；"
                                 f"已停在已扫描 {build.scanned}/{build.total_at_start} 条处，纠正旧重单后继续。")
            build.updated_at = now()
            _event(db, build.id, 'duplicate_block', build.wait_reason)
            db.flush()
            return build
        entry = _entry(db, build.id, order.id)
        if entry is None:
            db.add(IndexEntry(build_id=build.id, order_id=order.id, order_no=order.order_no))
        elif entry.order_no != order.order_no:
            entry.order_no = order.order_no
        build.scan_cursor = order.id
        build.scanned += 1
    build.wait_reason = f'已扫描 {build.scanned}/{build.total_at_start} 条。'
    build.updated_at = now()
    _event(db, build.id, 'scan_batch', f'扫描第 {rows[0].id}-{rows[-1].id} 行，进度 {build.scanned}/{build.total_at_start}。')
    remaining = db.scalar(select(Order.id).where(Order.id > build.scan_cursor,
                                                 Order.id <= build.snapshot_max_id).limit(1))
    if remaining is None:
        build.stage = 'catching_up'
        build.wait_reason = '存量扫描完成，追平构建期间写入。'
        _event(db, build.id, 'scan_done', build.wait_reason)
    db.flush()
    return build


def _catch_up(db, build):
    logs = db.scalars(select(OrderWriteLog).where(OrderWriteLog.build_id == build.id,
                                                  OrderWriteLog.processed.is_(False))
                      .order_by(OrderWriteLog.id)).all()
    for log in logs:
        order = db.get(Order, log.order_id)
        if order is None:
            log.processed = True
            log.processed_at = now()
            continue
        entry = _entry(db, build.id, order.id)
        if entry is None:
            db.add(IndexEntry(build_id=build.id, order_id=order.id, order_no=order.order_no))
        elif entry.order_no != order.order_no:
            entry.order_no = order.order_no
        log.processed = True
        log.processed_at = now()
    build.stage = 'validating'
    build.wait_reason = f'已追平 {len(logs)} 条构建期间写入，开始校验索引与订单一致。'
    build.updated_at = now()
    _event(db, build.id, 'catch_up', build.wait_reason)
    db.flush()
    return build


def _validate(db, build):
    if _pending_logs(db, build):
        build.stage = 'catching_up'
        build.wait_reason = f"构建期间仍有 {_pending_logs(db, build)} 条写入未核对，继续追平后才能生效。"
        build.updated_at = now()
        _event(db, build.id, 'validate_retry', build.wait_reason)
        db.flush()
        return build
    dups = _mark_duplicates(db)
    if dups:
        build.status = 'paused'
        build.resume_stage = 'validating'
        build.stage = 'awaiting_correction'
        build.blocking_order_id = dups[0]['blocking_id']
        build.wait_reason = (f"仍有 {len(dups)} 组旧重单未清理（如 {dups[0]['order_no']} ×{dups[0]['count']}），"
                             f"索引不能生效；请先纠正。")
        build.updated_at = now()
        _event(db, build.id, 'validate_block', build.wait_reason)
        db.flush()
        return build
    uncovered = _uncovered(db, build)
    mismatched = _mismatched(db, build)
    if uncovered or mismatched:
        if _pending_logs(db, build) == 0:
            build.status = 'failed'
            build.wait_reason = (f'校验失败：{len(uncovered)} 条订单未覆盖、{len(mismatched)} 条索引单号不一致，且没有可重放的写入日志。')
            build.updated_at = now()
            _event(db, build.id, 'validate_failed', build.wait_reason)
        else:
            build.stage = 'catching_up'
            build.wait_reason = f'构建期间写入尚未核对齐（{len(uncovered)} 条未覆盖），继续追平。'
            build.updated_at = now()
            _event(db, build.id, 'validate_retry', build.wait_reason)
        db.flush()
        return build
    build.status = 'active'
    build.stage = 'active'
    build.index_effective_at = now()
    build.blocking_order_id = None
    build.wait_reason = (f"存量旧重单已清完，构建期间的写入已核对，"
                         f"{_covered_count(db, build)} 条索引与订单逐条一致，唯一索引已生效。")
    build.updated_at = now()
    _event(db, build.id, 'activated', f'唯一索引已生效，覆盖 {_covered_count(db, build)} 条订单；副本现在可以拒绝重复单号。')
    waiting = db.scalars(select(Failover).where(Failover.status == 'deferred')).all()
    for row in waiting:
        row.status = 'completed'
        row.wait_reason = '唯一索引已生效，此前暂缓的切主可以放行。'
        row.build_stage = 'active'
        row.index_build_id = build.id
        row.resolved_at = now()
        _event(db, build.id, 'failover_released', f'暂缓的切主 {row.attempt_key} → {row.target} 随索引生效放行。')
    db.flush()
    return build


def tick_build(db, build_id):
    build = db.scalar(select(IndexBuild).where(IndexBuild.id == build_id).with_for_update())
    if build is None:
        raise HTTPException(404, '构建不存在')
    if build.status == 'interrupted':
        raise HTTPException(409, f'构建已中断；请用原构建标识 {build.attempt_key} 重试以恢复进度')
    if build.status == 'paused':
        raise HTTPException(409, f'构建暂停：{build.wait_reason}')
    if build.status in ('active', 'failed'):
        return build
    if build.stage == 'scanning':
        return _scan_batch(db, build)
    if build.stage == 'awaiting_correction':
        raise HTTPException(409, f'等待纠正旧重单：{build.wait_reason}')
    if build.stage == 'catching_up':
        return _catch_up(db, build)
    if build.stage == 'validating':
        return _validate(db, build)
    return build


def advance(db, build_id, steps=1):
    build = None
    for _ in range(max(1, min(int(steps or 1), 60))):
        build = tick_build(db, build_id)
        if build.status in ('paused', 'active', 'failed'):
            break
    return build


def interrupt_build(db, build_id):
    build = db.scalar(select(IndexBuild).where(IndexBuild.id == build_id).with_for_update())
    if build is None:
        raise HTTPException(404, '构建不存在')
    if build.status not in ('building', 'paused', 'queued'):
        raise HTTPException(409, f'只有进行中的构建可以中断（当前状态 {build.status}）')
    build.resume_stage = build.resume_stage if build.stage == 'awaiting_correction' else build.stage
    build.status = 'interrupted'
    build.stage = 'interrupted'
    build.interrupted_at = now()
    build.wait_reason = (f'构建在{STAGE_LABELS.get(build.resume_stage, build.resume_stage)}阶段中断，'
                         f'已保留 {build.scanned} 条索引条目，游标停在第 {build.scan_cursor} 行；'
                         f'用原构建标识 {build.attempt_key} 重试可恢复原进度。')
    build.updated_at = now()
    _event(db, build.id, 'interrupt', build.wait_reason)
    db.flush()
    return build


# --------------------------------------------------------------------------
# 切主
# --------------------------------------------------------------------------
def _apply_failover(db, row):
    allowed, reason, build = _failover_gate(db)
    row.wait_reason = reason
    row.build_stage = build.stage if build else 'none'
    row.index_build_id = build.id if build else None
    if allowed:
        if row.status != 'completed':
            row.status = 'completed'
            row.resolved_at = now()
            _event(db, build.id if build else None, 'failover_completed',
                   f'切主 {row.attempt_key} 完成：唯一索引已生效，副本接得住防重。')
    else:
        row.status = 'deferred'
        row.resolved_at = None
        _event(db, build.id if build else None, 'failover_deferred',
               f'切主 {row.attempt_key} 暂缓：{reason}')
    db.flush()
    return row


def request_failover(db, attempt_key, target):
    attempt_key = (attempt_key or '').strip()
    target = (target or '').strip()
    if not attempt_key or len(attempt_key) > 80:
        raise HTTPException(422, '切主标识必须为 1-80 个字符')
    if not target or len(target) > 80:
        raise HTTPException(422, '目标副本必须为 1-80 个字符')
    row = db.scalar(select(Failover).where(Failover.attempt_key == attempt_key))
    if row is None:
        row = Failover(attempt_key=attempt_key, target=target, status='deferred')
        db.add(row)
        db.flush()
        _event(db, None, 'failover_requested', f'请求切主到 {target}（{attempt_key}）。')
    return _apply_failover(db, row)


# --------------------------------------------------------------------------
# 沙盘状态与演示数据
# --------------------------------------------------------------------------
def replica_check(db, order_no=None):
    view = replica_view(db)
    if order_no:
        order_no = order_no.strip()
        hits = db.scalars(select(Order).where(Order.order_no == order_no).order_by(Order.id)).all()
        view['order_no'] = order_no
        view['orders_with_same_no'] = [{'id': o.id, 'origin': o.origin,
                                        'corrected_at': o.corrected_at,
                                        'duplicate_of_id': o.duplicate_of_id} for o in hits]
        view['would_be_rejected_on_replica'] = bool(view['can_detect_duplicates'] and hits)
    return view


def get_state(db):
    orders = db.scalars(select(Order).order_by(Order.id).limit(200)).all()
    builds = db.scalars(select(IndexBuild).order_by(IndexBuild.id.desc()).limit(10)).all()
    failovers = db.scalars(select(Failover).order_by(Failover.id.desc()).limit(10)).all()
    events = db.scalars(select(IndexEvent).order_by(IndexEvent.id.desc()).limit(40)).all()
    dup_ids = set()
    for group in _duplicate_groups(db):
        rows = db.scalars(select(Order.id).where(Order.order_no == group['order_no'])
                          .order_by(Order.id)).all()
        dup_ids.update(rows[1:])
    order_rows = []
    for o in orders:
        row = {k: getattr(o, k) for k in ('id', 'order_no', 'origin', 'duplicate_of_id',
                                          'original_order_no', 'corrected_at', 'created_at')}
        row['can_correct'] = o.id in dup_ids and o.corrected_at is None
        order_rows.append(row)
    latest = _latest_build(db)
    return {
        'orders': order_rows,
        'order_count': int(db.scalar(select(func.count(Order.id))) or 0),
        'builds': [build_view(db, b) for b in builds],
        'latest_build': build_view(db, latest),
        'failovers': [{k: getattr(f, k) for k in ('id', 'attempt_key', 'target', 'status',
                                                  'wait_reason', 'build_stage', 'index_build_id',
                                                  'requested_at', 'resolved_at')} for f in failovers],
        'events': [{k: getattr(e, k) for k in ('id', 'build_id', 'kind', 'message', 'created_at')} for e in events],
        'duplicates': _duplicate_groups(db),
        'replica': replica_view(db),
        'batch_size': BATCH_SIZE,
    }


DEMO_ORDERS = [
    ('HS-2601', None), ('HS-2602', None), ('HS-2603', None), ('HS-2604', None),
    ('HS-2605', None), ('HS-2606', None), ('HS-2607', None), ('HS-2608', None),
    ('HS-2611', None), ('HS-2611', 'dup'), ('HS-2612', None), ('HS-2613', None),
    ('HS-2614', None), ('HS-2615', None), ('HS-2616', None), ('HS-2614', 'dup'),
    ('HS-2617', None), ('HS-2618', None),
]


def ensure_lock(db):
    if db.scalar(select(SandboxLock).where(SandboxLock.name == 'orders-write')) is None:
        db.add(SandboxLock(name='orders-write'))
        db.flush()


def seed_orders(db, force=False):
    if not force and db.scalar(select(func.count(Order.id))):
        return False
    ensure_lock(db)
    canon = {}
    for order_no, kind in DEMO_ORDERS:
        if kind == 'dup':
            first = canon[order_no]
            db.add(Order(order_no=order_no, origin='legacy', duplicate_of_id=first.id if first.id else None,
                         original_order_no=order_no))
        else:
            order = Order(order_no=order_no, origin='legacy')
            db.add(order)
            db.flush()
            canon[order_no] = order
    db.flush()
    _event(db, None, 'seed', f'沙盘演示数据：{len(DEMO_ORDERS)} 条订单，含 2 组历史重复单号。')
    return True


def reset_sandbox(db):
    _lock_writes(db)
    for model in (OrderWriteLog, IndexEntry, IndexEvent, Failover, IndexBuild, Order):
        db.execute(delete(model))
    db.flush()
    seed_orders(db, force=True)
    db.commit()
    return get_state(db)
