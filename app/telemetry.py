"""Bounded samples: raw 1h, minute rollups 7d; never delete control records."""
from collections import defaultdict
from datetime import timedelta
from sqlalchemy import select, text, delete
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.db import Session, db_now
from app.models import ResourceSample


def capture(s, slot, now):
    latest = s.scalar(select(ResourceSample.measured_at).where(
        ResourceSample.slot_id==slot.id, ResourceSample.resolution=="raw")
        .order_by(ResourceSample.measured_at.desc()).limit(1))
    if not latest or (now-latest).total_seconds() >= settings.sample_seconds:
        s.execute(insert(ResourceSample).values(slot_id=slot.id, measured_at=now,
            resolution="raw", status=slot.status, holder_id=slot.holder_id, sample=slot.sample)
            .on_conflict_do_nothing())


def maintain():
    with Session.begin() as s:
        if not s.scalar(text("SELECT pg_try_advisory_xact_lock(771029)")):
            return
        now=db_now(s)
        cutoff=(now-timedelta(hours=settings.raw_retention_hours)).replace(second=0,microsecond=0)
        rows=list(s.scalars(select(ResourceSample).where(ResourceSample.resolution=="raw",
            ResourceSample.measured_at<cutoff).order_by(ResourceSample.measured_at).limit(10000)))
        groups=defaultdict(list)
        for r in rows:
            minute=r.measured_at.replace(second=0,microsecond=0)
            if len(rows)==10000 and minute==rows[-1].measured_at.replace(second=0,microsecond=0):
                continue
            groups[(r.slot_id,minute)].append(r)
        for (slot_id,minute),group in groups.items():
            good=[r for r in group if r.sample.get("valid")]
            holders=sorted({r.holder_id for r in group if r.holder_id})
            sources=sorted({r.sample.get('source','unknown') for r in group})
            sample={"source":sources[0] if len(sources)==1 else 'mixed', "simulated":all(r.sample.get('simulated') for r in group), "valid":bool(good), "sample_count":len(group),
                    "valid_count":len(good), "holder_ids":holders}
            for key in ["utilization_percent","memory_used_mb","memory_total_mb","temperature_c"]:
                values=[r.sample[key] for r in good if isinstance(r.sample.get(key),(int,float))]
                sample[key]=sum(values)/len(values) if values else None
            s.execute(insert(ResourceSample).values(slot_id=slot_id,measured_at=minute,resolution="minute",
                status="SUMMARY",holder_id=holders[0] if len(holders)==1 else None,sample=sample)
                .on_conflict_do_nothing())
            s.execute(delete(ResourceSample).where(ResourceSample.id.in_([r.id for r in group])))
        s.execute(delete(ResourceSample).where(ResourceSample.resolution=="minute",
            ResourceSample.measured_at<now-timedelta(days=settings.summary_retention_days)))
