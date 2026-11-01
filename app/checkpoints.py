"""Immutable chunks, fenced publication and per-attempt reuse lineage."""
from sqlalchemy import func, select
from app import artifacts, service
from app.db import Session, db_now
from app.models import Attempt, Checkpoint, CheckpointUse, Step, uid
from app.pipeline import digest


def definitions(spec):
    partition = spec["semantic"].get("partition")
    if not partition:
        return []
    result = []
    for index, start in enumerate(range(0, partition["frames"], partition["chunk_size"])):
        identity = {"schema": 1, "semantic": spec["semantic"], "step_cache_key": spec["cache_key"],
                    "partition_version": partition["version"], "start": start,
                    "end": min(start + partition["chunk_size"], partition["frames"])}
        result.append({"index": index, "identity": identity, "chunk_key": digest(identity)})
    return result


def prepare(spec):
    chunks = definitions(spec)
    if not chunks:
        return []
    candidates, bad_ids = {}, []
    with Session() as s:
        for chunk in chunks:
            rows = list(s.scalars(select(Checkpoint).where(Checkpoint.step_id == spec["step_id"],
                Checkpoint.chunk_key == chunk["chunk_key"], Checkpoint.valid.is_(True))
                .order_by(Checkpoint.committed_at.desc())))
            for cp in rows:
                if artifacts.verify(cp.directory, cp.manifest_digest):
                    candidates[chunk["index"]] = {"id": cp.id, "directory": cp.directory,
                        "manifest_digest": cp.manifest_digest, "producer_attempt_id": cp.producer_attempt_id}
                    break
                bad_ids.append(cp.id)
    with Session.begin() as s:
        task, step, attempt = service.current(s, spec["id"])
        if not service.authorized(task, step, attempt, spec["worker_id"], spec["fence"], db_now(s)):
            service.reject(s, task, step, attempt, "prepare_checkpoints")
            return None
        for cid in bad_ids:
            cp = s.get(Checkpoint, cid)
            cp.valid = False
            service.event(s, task, "CHECKPOINT_INVALIDATED", step, attempt, checkpoint_id=cid)
        for chunk in chunks:
            existing = candidates.get(chunk["index"])
            if existing:
                cp = s.get(Checkpoint, existing["id"])
                if cp.valid:
                    s.add(CheckpointUse(attempt_id=attempt.id, checkpoint_id=cp.id,
                        chunk_index=chunk["index"], reused=True, at=db_now(s)))
                    service.event(s, task, "CHECKPOINT_REUSED", step, attempt,
                        chunk_index=chunk["index"], checkpoint_id=cp.id, source_attempt=cp.producer_attempt_id)
                    chunk["reuse"] = existing
    return chunks


def register(spec, index, blob):
    chunks = definitions(spec)
    if index < 0 or index >= len(chunks):
        raise service.Problem("INVALID_CHUNK", "Chunk index outside frozen partition")
    chunk = chunks[index]
    if (blob["directory"] != f'checkpoints/{spec["id"]}/{index}'
            or not artifacts.verify(blob["directory"], blob["manifest_digest"])
            or blob["manifest"].get("semantic_key") != chunk["chunk_key"]):
        raise service.Problem("CHECKPOINT_INVALID", "Chunk failed integrity/identity validation")
    with Session.begin() as s:
        task, step, a = service.current(s, spec["id"])
        if not service.authorized(task, step, a, spec["worker_id"], spec["fence"], db_now(s)):
            service.reject(s, task, step, a, "commit_checkpoint")
            return False
        if (step.cache_key != spec["cache_key"] or digest(spec["semantic"]) != step.cache_key
                or step.spec.get("partition") != spec["semantic"].get("partition")):
            raise service.Problem("CHUNK_IDENTITY_CONFLICT", "Step identity changed")
        existing = s.scalar(select(CheckpointUse).where(CheckpointUse.attempt_id == a.id,
                                                      CheckpointUse.chunk_index == index))
        if existing:
            cp = s.get(Checkpoint, existing.checkpoint_id)
            return cp.manifest_digest == blob["manifest_digest"] and cp.chunk_key == chunk["chunk_key"]
        cp = Checkpoint(id=uid(), step_id=step.id, producer_attempt_id=a.id, chunk_index=index,
                        chunk_key=chunk["chunk_key"], identity=chunk["identity"], **blob,
                        epoch=a.epoch, fence=a.fence, valid=True, committed_at=db_now(s))
        s.add(cp)
        s.flush()
        s.add(CheckpointUse(attempt_id=a.id, checkpoint_id=cp.id, chunk_index=index,
                            reused=False, at=db_now(s)))
        count = s.scalar(select(func.count()).select_from(CheckpointUse).where(CheckpointUse.attempt_id == a.id))
        step.progress, step.last_progress_at = 100 * count / len(chunks), db_now(s)
        service.event(s, task, "CHECKPOINT_COMMITTED", step, a,
                      checkpoint_id=cp.id, chunk_index=index, chunk_key=cp.chunk_key,
                      frame_start=chunk["identity"]["start"], frame_end=chunk["identity"]["end"])
        return True


def completion_proof(attempt_id):
    with Session() as s:
        attempt = s.get(Attempt, attempt_id)
        step = s.get(Step, attempt.step_id)
        partition = step.spec.get("partition")
        if not partition:
            return True
        uses = list(s.scalars(select(CheckpointUse).where(CheckpointUse.attempt_id == attempt_id)))
        count = (partition["frames"] + partition["chunk_size"] - 1) // partition["chunk_size"]
        if {u.chunk_index for u in uses} != set(range(count)):
            return False
        for use in uses:
            cp = s.get(Checkpoint, use.checkpoint_id)
            if not cp.valid or cp.identity["step_cache_key"] != step.cache_key or not artifacts.verify(cp.directory, cp.manifest_digest):
                return False
        return True
