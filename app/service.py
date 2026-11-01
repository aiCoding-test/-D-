"""Durable state transitions. Lock order: task -> ordered steps -> attempts -> slots.

Only the worker control plane calls attempt mutation functions. HTTP clients
cannot manufacture heartbeats, completions, resource releases or fault points.
"""
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app import artifacts
from app.config import settings
from app.db import Session, db_now
from app.models import (Artifact, Attempt, CacheEntry, CommandReceipt, Event,
                        ResourceSlot, Step, Task, WorkerSession, uid)
from app.pipeline import OK, SPECS, LEGACY_SPECS, TERMINAL, TaskInput, asset_digest, digest, semantic

log = logging.getLogger("pipeline")


class Problem(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def event(s, task, kind, step=None, attempt=None, **payload):
    task.event_seq += 1
    task.revision += 1
    s.add(Event(task_id=task.id, seq=task.event_seq, epoch=task.epoch, type=kind,
                step_key=step.key if step else None, attempt_id=attempt.id if attempt else None,
                at=db_now(s), payload=payload))
    log.info(kind, extra={"context": {"task_id": task.id, "epoch": task.epoch, "event": kind,
                    "step": step.key if step else None, "attempt_id": attempt.id if attempt else None,
                    **payload}})


def locked(s, task_id, skip=False):
    task = s.scalar(select(Task).where(Task.id == task_id).with_for_update(skip_locked=skip))
    if not task:
        if skip:
            return None, [], []
        raise Problem("NOT_FOUND", "Task not found", 404)
    steps = list(s.scalars(select(Step).where(Step.task_id == task_id).order_by(Step.key).with_for_update()))
    attempts = list(s.scalars(select(Attempt).where(Attempt.task_id == task_id).order_by(Attempt.id).with_for_update()))
    return task, steps, attempts


def slot_lock(s, slot_id):
    return s.scalar(select(ResourceSlot).where(ResourceSlot.id == slot_id).with_for_update())


def submit(data: TaskInput, key):
    request = data.model_dump(mode="json")
    specs = SPECS if data.pipeline_version == "animation-v2" else LEGACY_SPECS
    if data.execution_profile == "hourly":
        if data.pipeline_version != "animation-v2" or data.scenario != "normal":
            raise Problem("INVALID_PROFILE", "Hourly profile requires v2 and normal scenario", 422)
        request.update(cache_read=False, step_timeout_seconds=2700, task_timeout_seconds=9000)
    elif data.pipeline_version == "animation-v1":
        request.pop("execution_profile", None)
    fingerprint = digest(request)
    source_digest = asset_digest(data.asset_id)
    with Session.begin() as s:
        now = db_now(s)
        tid = uid()
        inserted = s.scalar(insert(Task).values(
            id=tid, idempotency_key=key, request_hash=fingerprint, request=request,
            input_digest=source_digest, pipeline=specs, status="QUEUED", epoch=1,
            revision=1, event_seq=0, created_at=now, epoch_started_at=now,
            deadline_at=now + timedelta(seconds=request["task_timeout_seconds"])
        ).on_conflict_do_nothing(index_elements=[Task.idempotency_key]).returning(Task.id))
        if not inserted:
            existing = s.scalar(select(Task).where(Task.idempotency_key == key))
            if existing.request_hash != fingerprint:
                raise Problem("IDEMPOTENCY_CONFLICT", "The key was used with a different request")
            return {"task_id": existing.id, "status": existing.status, "reused_request": True}
        task = s.get(Task, tid)
        for spec in specs:
            s.add(Step(task_id=tid, key=spec["key"], spec=spec))
        event(s, task, "TASK_ACCEPTED", simulated=True, pipeline=data.pipeline_version)
        return {"task_id": tid, "status": "QUEUED", "reused_request": False}


def authorized(task, step, attempt, worker_id, fence, now):
    return (task.status == "RUNNING" and not task.stop_reason and task.deadline_at > now
            and task.epoch == attempt.epoch and step.status == "RUNNING"
            and step.current_attempt_id == attempt.id and step.fence == fence == attempt.fence
            and attempt.worker_id == worker_id and attempt.status == "RUNNING"
            and attempt.lease_expires_at > now)


def current(s, attempt_id):
    identity = s.execute(select(Attempt.task_id, Attempt.step_id).where(Attempt.id == attempt_id)).first()
    if not identity:
        raise Problem("NOT_FOUND", "Attempt not found", 404)
    task, steps, attempts = locked(s, identity.task_id)
    return task, next(x for x in steps if x.id == identity.step_id), next(x for x in attempts if x.id == attempt_id)


def reject(s, task, step, attempt, operation):
    event(s, task, "STALE_ATTEMPT_REJECTED", step, attempt, operation=operation)


def worker_register(worker_id, pid, process_created, host_name):
    with Session.begin() as s:
        s.add(WorkerSession(id=worker_id, node_id=settings.node_id, pid=pid,
                            process_created=process_created, host_name=host_name, heartbeat_at=db_now(s)))
        for key in ["cpu-0", "cpu-1", "mock-gpu-0"]:
            s.execute(insert(ResourceSlot).values(id=f"{settings.node_id}:{key}", node_id=settings.node_id,
                      key=key, kind="gpu" if "gpu" in key else "cpu", status="AVAILABLE", sample={})
                      .on_conflict_do_nothing(index_elements=[ResourceSlot.id]))


def worker_heartbeat(worker_id):
    with Session.begin() as s:
        w = s.get(WorkerSession, worker_id)
        w.heartbeat_at = db_now(s)
        # Samples do not determine allocation and never override ownership fields.
        slots = list(s.scalars(select(ResourceSlot).where(ResourceSlot.node_id == settings.node_id)
                               .order_by(ResourceSlot.id).with_for_update()))
        for slot in slots:
            busy = slot.status == "BUSY"
            slot.sample = {"source": "mock", "simulated": True, "valid": slot.status != "QUARANTINED",
                           "unavailable_reason": slot.reason,
                           "utilization_percent": 72 if busy else 0,
                           "memory_used_mb": 4096 if busy else 0, "memory_total_mb": 8192,
                           "temperature_c": 58 if busy else 32}
            slot.measured_at = db_now(s)
            from app.telemetry import capture
            capture(s, slot, slot.measured_at)


def end_attempt(s, task, step, attempt, status, code, retryable=False, stopped=False):
    now = db_now(s)
    attempt.status, attempt.finished_at = status, now
    attempt.error_code, attempt.error, attempt.retryable = code, code, retryable
    attempt.cleanup_state = "CONFIRMED" if stopped else "QUARANTINED"
    slot = slot_lock(s, attempt.slot_id)
    if slot.holder_id == attempt.id:
        slot.status = "AVAILABLE" if stopped else "QUARANTINED"
        slot.holder_id = None if stopped else attempt.id
        slot.holder_fence = None if stopped else attempt.fence
        slot.reason = None if stopped else "RESOURCE_CLEANUP_REQUIRED"
    step.current_attempt_id = None
    step.error = code
    event(s, task, f"ATTEMPT_{status}", step, attempt, code=code, retryable=retryable)
    if task.status == "CANCELLING":
        step.status, step.wait_reason = "CANCELLED", "USER_CANCELLED"
    elif task.stop_reason:
        step.status, step.wait_reason = "BLOCKED", "TASK_ABORTED"
    else:
        count = s.scalar(select(func.count()).select_from(Attempt).where(
            Attempt.step_id == step.id, Attempt.epoch == task.epoch))
        if retryable and count < 3:
            step.status = "RETRY_WAIT"
            # Stable jitter makes fault replays reproducible. Persist the chosen instant.
            jitter = int(attempt.id[:2], 16) / 2550
            delay = min(settings.retry_base_seconds * 2 ** (count - 1), 30) * (1 + jitter)
            step.next_run_at = now + timedelta(seconds=delay)
            step.wait_reason = "RETRY_BACKOFF"
            event(s, task, "STEP_RETRY_SCHEDULED", step, attempt, next_run_at=step.next_run_at.isoformat())
        else:
            step.status = "FAILED"
            task.stop_reason = code


def settle(s, task, steps, attempts):
    """Converge cancellation/failure before exposing a terminal task state."""
    if task.stop_reason or task.status == "CANCELLING":
        for step in sorted(steps, key=lambda x: x.key):
            if step.status == "RUNNING":
                a = next(a for a in attempts if a.id == step.current_attempt_id)
                end_attempt(s, task, step, a, "CANCELLED", task.stop_reason or "USER_CANCELLED")
            elif step.status not in OK | {"FAILED", "CANCELLED", "BLOCKED"}:
                step.status = "CANCELLED" if task.status == "CANCELLING" else "BLOCKED"
                step.wait_reason = "USER_CANCELLED" if task.status == "CANCELLING" else "TASK_ABORTED"
                event(s, task, f"STEP_{step.status}", step, reason=step.wait_reason)
        new_status = "CANCELLED" if task.status == "CANCELLING" else "FAILED"
        if task.status != new_status:
            task.status, task.finished_at = new_status, db_now(s)
            event(s, task, f"TASK_{new_status}", reason=task.stop_reason)
        return
    by_key = {x.key: x for x in steps}
    now = db_now(s)
    for step in steps:
        ready = (step.status == "PENDING" and all(by_key[k].status in OK for k in step.spec["deps"]))
        due = step.status == "RETRY_WAIT" and step.next_run_at <= now
        if ready or due:
            step.status, step.ready_at, step.wait_reason = "READY", now, "WAITING_WORKER"
            event(s, task, "STEP_READY", step)
    if all(x.status in OK for x in steps):
        task.status, task.finished_at = "SUCCEEDED", now
        event(s, task, "TASK_SUCCEEDED")


def reconcile():
    with Session() as s:
        ids = list(s.scalars(select(Task.id).where(Task.status.not_in(TERMINAL)).order_by(Task.created_at).limit(100)))
    for tid in ids:
        with Session.begin() as s:
            task, steps, attempts = locked(s, tid, skip=True)
            if not task or task.status in TERMINAL:
                continue
            now = db_now(s)
            if task.deadline_at <= now and task.status != "CANCELLING":
                task.stop_reason = "TASK_DEADLINE_EXCEEDED"
            for a in attempts:
                if a.status != "RUNNING":
                    continue
                step = next(x for x in steps if x.id == a.step_id)
                if a.lease_expires_at <= now:
                    end_attempt(s, task, step, a, "LOST", "LEASE_EXPIRED", True)
                elif a.run_deadline_at and a.run_deadline_at <= now:
                    end_attempt(s, task, step, a, "TIMED_OUT", "STEP_RUN_TIMEOUT", True)
                elif not a.started_at and (now - a.claimed_at).total_seconds() > 30:
                    end_attempt(s, task, step, a, "TIMED_OUT", "STEP_START_TIMEOUT", True)
            settle(s, task, steps, attempts)


def preflight(step_id):
    """File I/O outside the claiming transaction. Immutable references rechecked inside."""
    with Session() as s:
        step = s.get(Step, step_id)
        task = s.get(Task, step.task_id)
        parents = list(s.scalars(select(Step).where(Step.task_id == task.id, Step.key.in_(step.spec["deps"]))))
        refs, inputs, invalid = {}, {}, []
        for p in parents:
            refs[p.key] = p.artifact_id
            a = s.get(Artifact, p.artifact_id) if p.artifact_id else None
            if not a or not a.valid or not artifacts.verify(a.directory, a.manifest_digest):
                invalid.append(p.key)
            else:
                inputs[p.key] = a.content_digest
        identity = semantic(task, step.spec, inputs)
        key = digest(identity)
        cache = s.get(CacheEntry, key)
        cached = s.get(Artifact, cache.artifact_id) if cache else None
        valid_cache = bool(cached and cached.valid and artifacts.verify(cached.directory, cached.manifest_digest))
        return {"task_id": task.id, "step_id": step.id, "epoch": task.epoch, "refs": refs,
                "invalid": invalid, "semantic": identity, "cache_key": key,
                "cache_artifact": cached.id if cached else None, "cache_valid": valid_cache}


def claim(worker_id):
    with Session() as s:
        ids = list(s.scalars(select(Step.id).join(Task).where(
            Step.status == "READY", Task.status.in_(["QUEUED", "RUNNING"]), Task.stop_reason.is_(None)
        ).order_by(Task.created_at, Step.key).limit(50)))
    for sid in ids:
        probe = preflight(sid)
        with Session.begin() as s:
            task, steps, attempts = locked(s, probe["task_id"], skip=True)
            if not task or task.status not in {"QUEUED", "RUNNING"} or task.stop_reason:
                continue
            step = next(x for x in steps if x.id == sid)
            if step.status != "READY" or task.epoch != probe["epoch"] or task.deadline_at <= db_now(s):
                continue
            by_key = {x.key: x for x in steps}
            version = step.spec.get("processor_version", "mock-1")
            supported = ({"mock-1", "mock-2"} if step.key == "render_preview" else
                         {"mock-1", "mock-chunks-1"} if step.key in {"remove_background", "enhance_frames"} else {"mock-1"})
            if version not in supported:
                step.status, step.error = "FAILED", "PROCESSOR_VERSION_UNAVAILABLE"
                task.stop_reason = step.error
                event(s, task, "PROCESSOR_VERSION_UNAVAILABLE", step, version=version)
                settle(s, task, steps, attempts)
                continue
            if any(by_key[k].status not in OK or by_key[k].artifact_id != aid for k, aid in probe["refs"].items()):
                continue
            if probe["invalid"]:
                step.status, step.error = "FAILED", "ARTIFACT_INVALID"
                task.stop_reason = "ARTIFACT_INVALID"
                event(s, task, "ARTIFACT_INVALID", step, parents=probe["invalid"])
                settle(s, task, steps, attempts)
                continue
            step.cache_key = probe["cache_key"]
            cache = s.get(CacheEntry, step.cache_key)
            if cache and cache.artifact_id == probe["cache_artifact"] and not probe["cache_valid"]:
                cached = s.get(Artifact, cache.artifact_id)
                cached.valid = False
                s.delete(cache)
                event(s, task, "CACHE_INVALIDATED", step, artifact_id=cached.id)
            if task.request["cache_read"] and cache and probe["cache_valid"] and cache.artifact_id == probe["cache_artifact"]:
                a = s.get(Artifact, cache.artifact_id)
                if a.valid:
                    step.status, step.artifact_id, step.progress, step.wait_reason = "CACHED", a.id, 100, None
                    step.next_run_at, step.error = None, None
                    task.status = "RUNNING"
                    event(s, task, "STEP_CACHED", step, artifact_id=a.id, source_attempt=a.producer_attempt_id)
                    settle(s, task, steps, attempts)
                    return {"cached": True}
            slot = s.scalar(select(ResourceSlot).where(ResourceSlot.node_id == settings.node_id,
                    ResourceSlot.kind == step.spec["resource"], ResourceSlot.status == "AVAILABLE")
                    .order_by(ResourceSlot.id).with_for_update(skip_locked=True).limit(1))
            if not slot:
                reason = "GPU_BUSY" if step.spec["resource"] == "gpu" else "CPU_BUSY"
                if step.wait_reason != reason:
                    step.wait_reason = reason
                    event(s, task, "STEP_WAITING_RESOURCE", step, reason=reason)
                continue
            now, aid = db_now(s), uid()
            step.fence += 1
            previous = [a for a in attempts if a.step_id == sid]
            a = Attempt(id=aid, task_id=task.id, step_id=sid, number=len(previous) + 1,
                        epoch=task.epoch, fence=step.fence, worker_id=worker_id, slot_id=slot.id,
                        queue_seconds=max(0, (now-step.ready_at).total_seconds()) if step.ready_at else None,
                        status="RUNNING", phase="STARTING", claimed_at=now, heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=settings.lease_seconds))
            s.add(a)
            s.flush()  # insert Attempt before slot holder FK
            slot.status, slot.holder_id, slot.holder_fence, slot.reason = "BUSY", aid, step.fence, None
            step.status, step.current_attempt_id, step.progress, step.wait_reason = "RUNNING", aid, 0, None
            step.next_run_at, step.error, step.last_progress_at = None, None, None
            task.status = "RUNNING"
            event(s, task, "ATTEMPT_CLAIMED", step, a, fence=a.fence, slot_id=slot.id)
            return {"cached": False, "id": aid, "worker_id": worker_id, "fence": a.fence,
                    "task_id": task.id, "step_id": sid, "key": step.key, "epoch": task.epoch,
                    "epoch_number": 1 + sum(x.epoch == task.epoch for x in previous),
                    "scenario": task.request["scenario"], "seconds": (1800 if step.spec.get("partition") else 0.2)
                    if task.request.get("execution_profile") == "hourly" else task.request["step_seconds"],
                    "timeout": task.request["step_timeout_seconds"],
                    "semantic": probe["semantic"], "cache_key": step.cache_key}
    return None


def start_authorized(attempt_id, worker_id, fence):
    with Session.begin() as s:
        task, step, a = current(s, attempt_id)
        return authorized(task, step, a, worker_id, fence, db_now(s))


def started(attempt_id, worker_id, fence, pid, created):
    with Session.begin() as s:
        task, step, a = current(s, attempt_id)
        now = db_now(s)
        if not authorized(task, step, a, worker_id, fence, now):
            reject(s, task, step, a, "start")
            return False
        a.executor_pid, a.executor_created = pid, created
        a.phase, a.started_at = "EXECUTING", now
        a.run_deadline_at = now + timedelta(seconds=task.request["step_timeout_seconds"])
        event(s, task, "ATTEMPT_STARTED", step, a, pid=pid)
        return True


def heartbeat(attempt_id, worker_id, fence, progress=None):
    with Session.begin() as s:
        task, step, a = current(s, attempt_id)
        now = db_now(s)
        if not authorized(task, step, a, worker_id, fence, now):
            reject(s, task, step, a, "heartbeat/progress")
            return False
        a.heartbeat_at, a.lease_expires_at = now, now + timedelta(seconds=settings.lease_seconds)
        if progress is not None and progress > step.progress:
            step.progress, step.last_progress_at = min(progress, 100), now
        return True


def failed(attempt_id, worker_id, fence, code, retryable, stopped=True):
    with Session.begin() as s:
        task, step, a = current(s, attempt_id)
        if not authorized(task, step, a, worker_id, fence, db_now(s)):
            reject(s, task, step, a, "failure")
            return False
        end_attempt(s, task, step, a, "FAILED", code, retryable, stopped)
        steps = list(s.scalars(select(Step).where(Step.task_id == task.id)))
        attempts = list(s.scalars(select(Attempt).where(Attempt.task_id == task.id)))
        settle(s, task, steps, attempts)
        return True


def completed(attempt_id, worker_id, fence, blob):
    from app.checkpoints import completion_proof
    chunks_valid = completion_proof(attempt_id)
    valid = artifacts.verify(blob["directory"], blob["manifest_digest"])
    with Session.begin() as s:
        task, step, a = current(s, attempt_id)
        if a.status == "SUCCEEDED":
            return a.worker_id == worker_id and a.fence == fence and a.manifest_digest == blob["manifest_digest"]
        if not authorized(task, step, a, worker_id, fence, db_now(s)):
            reject(s, task, step, a, "complete")
            return False
        if not valid or not chunks_valid or blob["manifest"].get("semantic_key") != step.cache_key:
            raise Problem("ARTIFACT_INVALID", "Output failed integrity/semantic validation")
        artifact = Artifact(id=uid(), producer_attempt_id=a.id, **blob, valid=True, created_at=db_now(s))
        s.add(artifact)
        s.flush()
        a.status, a.finished_at, a.phase = "SUCCEEDED", db_now(s), "FINALIZING"
        a.manifest_digest, a.cleanup_state = blob["manifest_digest"], "CONFIRMED"
        step.status, step.artifact_id, step.progress = "SUCCEEDED", artifact.id, 100
        step.next_run_at, step.error, step.last_progress_at = None, None, db_now(s)
        step.current_attempt_id, step.wait_reason = None, None
        slot = slot_lock(s, a.slot_id)
        if slot.holder_id != a.id or slot.holder_fence != fence:
            raise Problem("RESOURCE_OWNER_CONFLICT", "Slot owner mismatch")
        slot.status, slot.holder_id, slot.holder_fence, slot.reason = "AVAILABLE", None, None, None
        # Cache races may duplicate computation; the first committed valid entry wins.
        s.execute(insert(CacheEntry).values(key=step.cache_key, artifact_id=artifact.id)
                  .on_conflict_do_nothing(index_elements=[CacheEntry.key]))
        event(s, task, "ATTEMPT_SUCCEEDED", step, a, artifact_id=artifact.id, fence=fence)
        steps = list(s.scalars(select(Step).where(Step.task_id == task.id)))
        attempts = list(s.scalars(select(Attempt).where(Attempt.task_id == task.id)))
        settle(s, task, steps, attempts)
        return True


def quarantine_candidates():
    with Session() as s:
        return [(a.id, a.task_id) for a in s.scalars(select(Attempt).where(
            Attempt.status != "RUNNING", Attempt.cleanup_state == "QUARANTINED"))]


def release_confirmed(attempt_id):
    # Caller must probe the executor's inherited lock outside the DB transaction.
    with Session.begin() as s:
        task, step, a = current(s, attempt_id)
        if a.status == "RUNNING" or a.cleanup_state != "QUARANTINED":
            return
        slot = slot_lock(s, a.slot_id)
        if slot.holder_id == a.id:
            slot.status, slot.holder_id, slot.holder_fence, slot.reason = "AVAILABLE", None, None, None
        a.cleanup_state = "CONFIRMED"
        event(s, task, "EXECUTOR_STOP_CONFIRMED", step, a)


def command(task_id, action, operation_key, expected_revision=None, step_key=None):
    request_hash = digest({"revision": expected_revision, "step_key": step_key})
    # File verification outside transaction; recheck immutable references under task lock.
    with Session() as s:
        refs = list(s.execute(select(Step.id, Step.artifact_id).where(Step.task_id == task_id)))
        valid = {}
        for sid, aid in refs:
            a = s.get(Artifact, aid) if aid else None
            valid[sid] = (aid, bool(a and a.valid and artifacts.verify(a.directory, a.manifest_digest)))
    with Session.begin() as s:
        task, steps, attempts = locked(s, task_id)
        receipt = s.scalar(select(CommandReceipt).where(CommandReceipt.task_id == task_id,
                    CommandReceipt.action == action, CommandReceipt.operation_key == operation_key))
        if receipt:
            if receipt.request_hash != request_hash:
                raise Problem("OPERATION_CONFLICT", "Operation key used with another command body")
            return receipt.response
        if expected_revision is not None and expected_revision != task.revision:
            raise Problem("REVISION_CONFLICT", "Task changed; refresh before retrying")
        if action == "cancel":
            if task.status in TERMINAL:
                raise Problem("TERMINAL_TASK", "Task is already terminal")
            task.status, task.stop_reason = "CANCELLING", "USER_CANCELLED"
            event(s, task, "TASK_CANCEL_REQUESTED")
            settle(s, task, steps, attempts)
        else:
            if task.status != "FAILED" or any(a.status == "RUNNING" for a in attempts):
                raise Problem("NOT_RESUMABLE", "Only a failed task without active attempts can resume")
            if task.epoch >= 6:
                raise Problem("RESUME_LIMIT", "Manual resume limit (5) reached")
            roots = [x for x in steps if x.status == "FAILED"]
            if step_key and (len(roots) != 1 or roots[0].key != step_key):
                raise Problem("MULTIPLE_FAILURES", "Use task-level resume for multiple failed roots")
            invalid = set()
            for x in steps:
                if x.status not in OK or valid.get(x.id) != (x.artifact_id, True):
                    invalid.add(x.key)
            while True:
                before = len(invalid)
                invalid.update(x.key for x in steps if any(k in invalid for k in x.spec["deps"]))
                if len(invalid) == before:
                    break
            old_epoch = task.epoch
            task.epoch += 1
            task.status, task.stop_reason, task.finished_at = "QUEUED", None, None
            task.epoch_started_at = db_now(s)
            task.deadline_at = task.epoch_started_at + timedelta(seconds=task.request["task_timeout_seconds"])
            event(s, task, "TASK_RESUMED", previous_epoch=old_epoch, invalidated=sorted(invalid))
            for x in steps:
                if x.key in invalid:
                    if x.status in OK:
                        event(s, task, "STEP_OUTPUT_INVALIDATED", x, artifact_id=x.artifact_id)
                    x.status, x.artifact_id, x.current_attempt_id = "PENDING", None, None
                    x.progress, x.next_run_at, x.error, x.wait_reason = 0, None, None, None
            settle(s, task, steps, attempts)
        result = {"task_id": task.id, "status": task.status, "epoch": task.epoch, "revision": task.revision}
        s.add(CommandReceipt(task_id=task_id, action=action, operation_key=operation_key,
                            request_hash=request_hash, response=result))
        return result
