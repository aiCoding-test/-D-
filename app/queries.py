from datetime import datetime

from sqlalchemy import select

from app import artifacts
from app.db import Session, db_now
from app.models import Artifact, Attempt, Event, ResourceSlot, Step, Task, WorkerSession, Checkpoint, CheckpointUse
from app.pipeline import OK
from app.service import Problem


def row(obj, exclude=()):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in exclude}


def detail(tid):
    with Session.begin() as s:
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        t = s.get(Task, tid)
        if not t:
            raise Problem("NOT_FOUND", "Task not found", 404)
        out = row(t, ("idempotency_key", "request_hash"))
        out["steps"] = []
        for step in s.scalars(select(Step).where(Step.task_id == tid)):
            x = row(step)
            x["attempts"] = [row(a) for a in s.scalars(select(Attempt).where(Attempt.step_id == step.id).order_by(Attempt.number))]
            a = s.get(Artifact, step.artifact_id) if step.artifact_id else None
            x["artifact"] = row(a, ("directory",)) if a else None
            x["checkpoints"] = checkpoint_summary(s, step, x["attempts"])
            out["steps"].append(x)
        out["steps"].sort(key=lambda x: next(i for i, spec in enumerate(t.pipeline) if spec["key"] == x["key"]))
        out["event_cursor"] = t.event_seq
        out["completed_steps"] = sum(x["status"] in OK for x in out["steps"])
        out["server_time"] = db_now(s)
        out["events"] = [row(e) for e in s.scalars(select(Event).where(Event.task_id == tid).order_by(Event.seq.desc()).limit(100))][::-1]
    # Filesystem checks after releasing snapshot; never hold a DB lock across I/O.
    with Session() as s:
        for x in out["steps"]:
            if x["artifact"]:
                a = s.get(Artifact, x["artifact"]["id"])
                x["artifact"]["available"] = a.valid and artifacts.verify(a.directory, a.manifest_digest)
    return out


def checkpoint_summary(s, step, attempts):
    partition = step.spec.get("partition")
    if not partition:
        return None
    total = (partition["frames"] + partition["chunk_size"] - 1) // partition["chunk_size"]
    if step.status=="CACHED":
        return {"mode":"whole_step_cache","partition":partition,"total":total,
                "completed":None,"current_chunk":None,"reused":0,"items":[]}
    aid = step.current_attempt_id or (attempts[-1]["id"] if attempts else None)
    uses = list(s.scalars(select(CheckpointUse).where(CheckpointUse.attempt_id == aid))) if aid else []
    rows = []
    for use in sorted(uses, key=lambda u: u.chunk_index):
        cp = s.get(Checkpoint, use.checkpoint_id)
        rows.append({"id": cp.id, "index": cp.chunk_index, "start": cp.identity["start"],
                     "end": cp.identity["end"], "chunk_key": cp.chunk_key, "valid": cp.valid,
                     "source_attempt": cp.producer_attempt_id, "reused": use.reused,
                     "committed_at": cp.committed_at, "epoch": cp.epoch, "fence": cp.fence})
    total = (partition["frames"] + partition["chunk_size"] - 1) // partition["chunk_size"]
    completed = {x["index"] for x in rows if x["valid"]}
    return {"mode":"chunks","partition": partition, "total": total, "completed": len(completed),
            "current_chunk": next((i for i in range(total) if i not in completed), None),
            "reused": sum(x["reused"] for x in rows), "items": rows}


def tasks(status, cursor, limit):
    with Session() as s:
        stmt = select(Task).order_by(Task.created_at.desc(), Task.id.desc())
        if status:
            stmt = stmt.where(Task.status == status)
        if cursor:
            older = s.get(Task, cursor)
            if not older:
                raise Problem("INVALID_CURSOR", "Unknown cursor", 422)
            stmt = stmt.where((Task.created_at < older.created_at) | ((Task.created_at == older.created_at) & (Task.id < older.id)))
        rows = list(s.scalars(stmt.limit(limit + 1)))
        return {"items": [row(t, ("request_hash", "idempotency_key", "pipeline")) for t in rows[:limit]],
                "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


def resources():
    with Session() as s:
        now = db_now(s)
        slots = []
        for slot in s.scalars(select(ResourceSlot).order_by(ResourceSlot.id)):
            x = row(slot)
            x["sample_age_seconds"] = (now - slot.measured_at).total_seconds() if slot.measured_at else None
            x["sample_fresh"] = slot.measured_at is not None and x["sample_age_seconds"] < 15 and slot.sample.get("valid", False)
            a = s.get(Attempt, slot.holder_id) if slot.holder_id else None
            x["holder"] = {"task_id": a.task_id, "attempt_id": a.id, "fence": a.fence} if a else None
            slots.append(x)
        workers = [dict(row(w), online=(now - w.heartbeat_at).total_seconds() < 15)
                   for w in s.scalars(select(WorkerSession).order_by(WorkerSession.heartbeat_at.desc()).limit(20))]
        waiting = [row(x) for x in s.scalars(select(Step).where(Step.status.in_(["READY", "RETRY_WAIT"])))]
        return {"slots": slots, "workers": workers, "waiting": waiting, "server_time": now, "simulated": True}
