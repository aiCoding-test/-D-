import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    input_digest: Mapped[str] = mapped_column(String(64))
    pipeline: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    epoch_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("task_id", "key"), Index("ix_step_ready", "status", "next_run_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    key: Mapped[str] = mapped_column(String(40))
    spec: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    fence: Mapped[int] = mapped_column(BigInteger, default=0)
    current_attempt_id: Mapped[str | None] = mapped_column(String(36))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wait_reason: Mapped[str | None] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(Text)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id", use_alter=True))
    cache_key: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerSession(Base):
    __tablename__ = "worker_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(100))
    pid: Mapped[int] = mapped_column(Integer)
    process_created: Mapped[float] = mapped_column(Float)
    host_name: Mapped[str] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("step_id", "number"),
        Index("uq_running_step", "step_id", unique=True, postgresql_where=text("status = 'RUNNING'")),
        Index("ix_attempt_lease", "status", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    number: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    fence: Mapped[int] = mapped_column(BigInteger)
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_sessions.id"))
    slot_id: Mapped[str] = mapped_column(ForeignKey("resource_slots.id", use_alter=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    phase: Mapped[str] = mapped_column(String(20), default="STARTING")
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    executor_pid: Mapped[int | None] = mapped_column(Integer)
    executor_created: Mapped[float | None] = mapped_column(Float)
    cleanup_state: Mapped[str] = mapped_column(String(20), default="PENDING")
    manifest_digest: Mapped[str | None] = mapped_column(String(64))


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    producer_attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), unique=True)
    directory: Mapped[str] = mapped_column(String(200), unique=True)
    manifest_digest: Mapped[str] = mapped_column(String(64))
    content_digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CacheEntry(Base):
    __tablename__ = "cache_entries"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))


class ResourceSlot(Base):
    __tablename__ = "resource_slots"
    __table_args__ = (UniqueConstraint("node_id", "key"),
                     CheckConstraint("(status = 'AVAILABLE' AND holder_id IS NULL) OR (status IN ('BUSY','QUARANTINED') AND holder_id IS NOT NULL)", name="slot_holder_state"))
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")
    holder_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id", use_alter=True), unique=True)
    holder_fence: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    sample: Mapped[dict] = mapped_column(JSONB, default=dict)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "task_events"
    __table_args__ = (UniqueConstraint("task_id", "seq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    seq: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80))
    step_key: Mapped[str | None] = mapped_column(String(40))
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (UniqueConstraint("task_id", "action", "operation_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    action: Mapped[str] = mapped_column(String(30))
    operation_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
