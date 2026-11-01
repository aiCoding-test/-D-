"""Read models and low-cardinality Prometheus text exposition (0.0.4)."""
from collections import Counter
from datetime import timedelta
from sqlalchemy import select
from app.config import settings
from app.db import Session, db_now
from app.models import Attempt, Checkpoint, CheckpointUse, ResourceSample, ResourceSlot, Step, Task
from app.queries import row
from app.service import Problem


def timings(task_id):
    with Session() as s:
        if not s.get(Task,task_id):raise Problem("NOT_FOUND","Task not found",404)
        now=db_now(s)
        attempts=list(s.scalars(select(Attempt).where(Attempt.task_id==task_id).order_by(Attempt.step_id,Attempt.number)))
        previous={}
        items=[]
        for a in attempts:
            old=previous.get(a.step_id)
            start=a.started_at
            items.append({"attempt_id":a.id,"step_id":a.step_id,"number":a.number,"status":a.status,
                "queue_seconds":a.queue_seconds,
                "run_seconds":max(0,((a.finished_at or now)-start).total_seconds()) if start else None,
                "lease_detection_seconds":max(0,(a.finished_at-a.lease_expires_at).total_seconds())
                    if a.status=="LOST" else None,
                "recovery_start_seconds":max(0,(start-old.lease_expires_at).total_seconds())
                    if old and old.status=="LOST" and start else None})
            previous[a.step_id]=a
        return {"items":items,"server_time":now,"definitions":{
            "queue_seconds":"READY to claim; excludes dependencies and retry backoff; P0 rows may be null",
            "run_seconds":"executor start to terminal detection (or now); includes computation and finalization",
            "lease_detection_seconds":"LOST detection minus expired lease",
            "recovery_start_seconds":"new executor start minus previous LOST lease; includes quarantine/backoff"}}


def resource_history(slot_id, since, until, cursor, limit):
    with Session.begin() as s:
        s.connection(execution_options={"isolation_level":"REPEATABLE READ"})
        now=db_now(s)
        query=select(ResourceSample)
        if slot_id:
            if not s.get(ResourceSlot,slot_id):raise Problem("NOT_FOUND","Resource slot not found",404)
            query=query.where(ResourceSample.slot_id==slot_id)
        if since:query=query.where(ResourceSample.measured_at>=since)
        if until:query=query.where(ResourceSample.measured_at<=until)
        if cursor:
            anchor=s.get(ResourceSample,cursor)
            if not anchor:raise Problem("CURSOR_EXPIRED","History retention expired; refresh history",410)
            query=query.where((ResourceSample.measured_at<anchor.measured_at)|
                ((ResourceSample.measured_at==anchor.measured_at)&(ResourceSample.id<anchor.id)))
        samples=list(s.scalars(query.order_by(ResourceSample.measured_at.desc(),ResourceSample.id.desc()).limit(limit+1)))
        return {"items":[row(x) for x in samples[:limit]],
                "next_cursor":samples[limit-1].id if len(samples)>limit else None,"server_time":now,
                "policy":{"sample_seconds":settings.sample_seconds,"raw_hours":settings.raw_retention_hours,
                          "minute_days":settings.summary_retention_days,
                          "rollup":"per minute valid sample averages; invalid-only buckets remain unavailable; holder_ids retain lineage"}}


def metrics():
    lines=[]
    def emit(name,help_text,kind,samples):
        lines.extend([f"# HELP {name} {help_text}",f"# TYPE {name} {kind}"])
        for labels,value in samples:
            suffix="{"+",".join(f'{k}="{v}"' for k,v in sorted(labels.items()))+"}" if labels else ""
            lines.append(f"{name}{suffix} {value}")
    with Session.begin() as s:
        s.connection(execution_options={"isolation_level":"REPEATABLE READ"})
        tasks=list(s.scalars(select(Task)))
        attempts=list(s.scalars(select(Attempt)))
        steps=list(s.scalars(select(Step)))
        uses=list(s.scalars(select(CheckpointUse)))
        slots=list(s.scalars(select(ResourceSlot)))
        emit("pipeline_tasks","Current tasks by state","gauge",[({"state":state},n) for state,n in Counter(t.status for t in tasks).items()])
        emit("pipeline_attempts","Attempts by state","gauge",[({"state":state},n) for state,n in Counter(a.status for a in attempts).items()])
        emit("pipeline_additional_attempts_total","Attempts beyond the first, including manual resumes","counter",[({},sum(a.number>1 for a in attempts))])
        emit("pipeline_checkpoints_total","Committed immutable chunk records, including invalidated history","counter",
             [({},len(list(s.scalars(select(Checkpoint.id)))))])
        emit("pipeline_checkpoint_reuses_total","Committed reuse bindings across attempts","counter",[({},sum(u.reused for u in uses))])
        emit("pipeline_cached_steps","Current steps satisfied by whole-step cache","gauge",[({},sum(x.status=="CACHED" for x in steps))])
        recovery=[]
        previous={}
        for a in sorted(attempts,key=lambda a:(a.step_id,a.number)):
            old=previous.get(a.step_id)
            if old and old.status=="LOST" and a.started_at:
                recovery.append(max(0,(a.started_at-old.lease_expires_at).total_seconds()))
            previous[a.step_id]=a
        for field,help_text in [("queue_seconds","Sum of persisted READY-to-claim queue time"),
                                ("run_seconds","Sum of completed executor-to-terminal time"),
                                ("lease_detection_seconds","Sum of expired-lease-to-LOST detection time"),
                                ("recovery_start_seconds","Sum of expired lease to replacement executor start")]:
            if field=="queue_seconds":values=[a.queue_seconds for a in attempts if a.queue_seconds is not None]
            elif field=="run_seconds":values=[max(0,(a.finished_at-a.started_at).total_seconds()) for a in attempts if a.started_at and a.finished_at]
            elif field=="lease_detection_seconds":values=[max(0,(a.finished_at-a.lease_expires_at).total_seconds()) for a in attempts if a.status=="LOST"]
            else:values=recovery
            emit("pipeline_"+field+"_sum",help_text,"gauge",[({},sum(values))])
            emit("pipeline_"+field+"_count","Number of included observations","gauge",[({},len(values))])
        emit("pipeline_resource_slots","Slots by kind and state","gauge",
             [({"kind":kind,"state":state},n) for (kind,state),n in Counter((x.kind,x.status) for x in slots).items()])
        now=db_now(s)
        fresh=[x for x in slots if x.kind=="gpu" and x.sample.get('source')=='mock' and x.measured_at and (now-x.measured_at).total_seconds()<15 and x.sample.get("valid")]
        emit("pipeline_mock_gpu_valid_samples","Fresh valid MOCK GPU samples; 0 means unavailable, not idle","gauge",[({},len(fresh))])
        if fresh:
            emit("pipeline_mock_gpu_utilization_percent","Mean fresh simulated GPU utilization","gauge",
                 [({"source":"mock"},sum(x.sample["utilization_percent"] for x in fresh)/len(fresh))])
        real=[x for x in slots if x.sample.get('source')=='nvml' and x.measured_at
            and (now-x.measured_at).total_seconds()<15 and x.sample.get('valid')]
        emit('pipeline_nvml_valid_samples','Fresh valid physical NVIDIA device samples','gauge',[({},len(real))])
        for field in ['utilization_percent','memory_used_mb','temperature_c']:
            values=[x.sample[field] for x in real if isinstance(x.sample.get(field),(int,float))]
            if values:emit('pipeline_nvml_'+field,'Mean available NVML device value','gauge',[({'source':'nvml'},sum(values)/len(values))])
    return "\n".join(lines)+"\n"
