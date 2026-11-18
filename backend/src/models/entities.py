from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, JSON, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base

def now(): return datetime.now(timezone.utc).replace(tzinfo=None)
class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(250))
class Node(Base):
    __tablename__ = 'nodes'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    term: Mapped[int] = mapped_column(Integer)
    log_position: Mapped[int] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean)
    fenced: Mapped[bool] = mapped_column(Boolean)
    vote_enabled: Mapped[bool] = mapped_column(Boolean)
class Scenario(Base):
    __tablename__ = 'scenarios'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    fail_primary: Mapped[bool] = mapped_column(Boolean)
    fence_primary: Mapped[bool] = mapped_column(Boolean)
    max_data_loss: Mapped[int] = mapped_column(Integer)
    required_term: Mapped[int] = mapped_column(Integer)
class Drill(Base):
    __tablename__ = 'drills'
    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey('scenarios.id'))
    label: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default='queued')
    snapshot: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
class Audit(Base):
    __tablename__ = 'audits'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

# --- 在线唯一索引沙盘 -------------------------------------------------------
# orders 是业务表。存量数据允许存在历史重复单号；新单号由服务层预检拒绝。
# index_entries 是逐批填充的影子唯一索引，构建生效前只覆盖已扫描/已写入部分。
class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(80), index=True)
    origin: Mapped[str] = mapped_column(String(20), default='live')       # legacy / live
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_order_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class IndexBuild(Base):
    __tablename__ = 'index_builds'
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_key: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='queued')    # queued/building/paused/active/failed
    stage: Mapped[str] = mapped_column(String(30), default='scanning')   # scanning/awaiting_correction/catching_up/validating/active
    resume_stage: Mapped[str] = mapped_column(String(30), default='scanning')
    snapshot_max_id: Mapped[int] = mapped_column(Integer, default=0)
    total_at_start: Mapped[int] = mapped_column(Integer, default=0)
    scan_cursor: Mapped[int] = mapped_column(Integer, default=0)
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    wait_reason: Mapped[str] = mapped_column(String(300), default='')
    blocking_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_effective_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resumed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class IndexEntry(Base):
    __tablename__ = 'index_entries'
    __table_args__ = (UniqueConstraint('build_id', 'order_no', name='uq_index_entry_build_order'),
                      UniqueConstraint('build_id', 'order_id', name='uq_index_entry_build_orderid'))
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey('index_builds.id'), index=True)
    order_id: Mapped[int] = mapped_column(Integer)
    order_no: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class OrderWriteLog(Base):
    __tablename__ = 'order_write_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey('index_builds.id'), index=True)
    order_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))                     # insert / correct
    previous_order_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    order_no: Mapped[str] = mapped_column(String(80))
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class Failover(Base):
    __tablename__ = 'failovers'
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_key: Mapped[str] = mapped_column(String(80), unique=True)
    target: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default='deferred')  # deferred / completed
    wait_reason: Mapped[str] = mapped_column(String(300), default='')
    index_build_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    build_stage: Mapped[str] = mapped_column(String(30), default='none')
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class SandboxLock(Base):
    __tablename__ = 'sandbox_locks'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    touched_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class IndexEvent(Base):
    __tablename__ = 'index_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
