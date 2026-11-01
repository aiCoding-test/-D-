import json
import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from app import artifacts, checkpoints, service
from app.db import Session
from app.models import Checkpoint, CheckpointUse, Step, Attempt, Artifact, ResourceSample, ResourceSlot, Event, Task
from app.worker import Worker
from conftest import step, wait_for
from test_pipeline import create, terminal, command


def v2(client, **body):
    return create(client, pipeline_version="animation-v2", **body)


def test_P01_chunk_pipeline_and_cache(client, workers):
    workers()
    d = terminal(client, v2(client))
    assert d["status"] == "SUCCEEDED"
    for key in ["remove_background", "enhance_frames"]:
        x = step(d, key)
        assert x["checkpoints"]["completed"] == 4
        assert x["checkpoints"]["reused"] == 0
        assert len(x["artifact"]["manifest"]["files"]) == 5
        for f in x["artifact"]["manifest"]["files"]:
            assert client.get(f'/api/artifacts/{x["artifact_id"]}/files/{f["key"]}').status_code == 200
    cached = terminal(client, v2(client, cache_read=True))
    assert all(x["status"] == "CACHED" and not x["attempts"] for x in cached["steps"])
    for key in ["remove_background","enhance_frames"]:
        assert step(cached,key)["checkpoints"]["mode"]=="whole_step_cache"
        assert step(cached,key)["checkpoints"]["completed"] is None


def test_P02_two_committed_chunks_then_real_kill(client, workers):
    p = workers()
    tid = v2(client, step_seconds=2)
    d = wait_for(client, tid, lambda d: step(d,"remove_background")["checkpoints"]["completed"] == 2)
    old = step(d, "remove_background")["attempts"][-1]
    source_ids = [x["id"] for x in step(d,"remove_background")["checkpoints"]["items"]]
    os.kill(old["executor_pid"], signal.SIGKILL)
    p.kill(); p.wait()
    workers()
    final = terminal(client, tid)
    assert final["status"] == "SUCCEEDED"
    x = step(final, "remove_background")
    assert [a["status"] for a in x["attempts"]] == ["LOST", "SUCCEEDED"]
    assert x["checkpoints"]["reused"] == 2
    assert [c["id"] for c in x["checkpoints"]["items"][:2]] == source_ids
    with Session() as s:
        assert len(list(s.scalars(select(Checkpoint).where(Checkpoint.step_id == x["id"])))) == 4


def test_P03_partial_chunk_is_not_reused(client, workers):
    p = workers(ENABLE_TEST_FAULTS="1", TEST_PARTIAL_CHUNK="2")
    tid = v2(client)
    d = wait_for(client, tid, lambda d: step(d,"remove_background")["checkpoints"]["completed"]==2)
    old = step(d,"remove_background")["attempts"][-1]
    path = artifacts.folder(f'checkpoint-work/{old["id"]}/2/partial.tmp')
    end=time.monotonic()+3
    while not path.exists() and time.monotonic()<end:time.sleep(.03)
    assert path.exists()
    os.kill(old["executor_pid"],signal.SIGKILL);p.kill();p.wait()
    workers()
    final=terminal(client,tid)
    assert final["status"]=="SUCCEEDED"
    assert step(final,"remove_background")["checkpoints"]["reused"]==2


def test_P04_P05_checkpoint_commit_windows(client, workers):
    # Two independent crash phases; inspect checkpoint lineage, not just task status.
    for phase,expected_reuse in [("chunk_after_publish",1),("chunk_after_commit",2)]:
        p=workers(ENABLE_TEST_FAULTS="1",TEST_CRASH_PHASE=phase,TEST_CRASH_CHUNK="1")
        tid=v2(client,parameters={"seed":100+expected_reuse})
        end=time.monotonic()+10
        while p.poll() is None and time.monotonic()<end:time.sleep(.04)
        assert p.poll()==91
        before=client.get(f'/api/tasks/{tid}').json()
        x=step(before,"remove_background")
        assert x["checkpoints"]["completed"]==expected_reuse
        # Child survives supervisor os._exit and is waiting for an acknowledgement.
        os.kill(x["attempts"][-1]["executor_pid"],signal.SIGKILL)
        next_worker=workers()
        final=terminal(client,tid)
        assert final["status"]=="SUCCEEDED"
        assert step(final,"remove_background")["checkpoints"]["reused"]==expected_reuse
        next_worker.terminate();next_worker.wait(timeout=4)


def test_P06_corrupt_one_checkpoint_and_manual_resume(client, workers):
    p=workers()
    tid=v2(client,scenario="resume_preview")
    before=terminal(client,tid)
    assert before["status"]=="FAILED"
    x=step(before,"remove_background")
    with Session() as s:
        cp=s.get(Checkpoint,x["checkpoints"]["items"][1]["id"])
        artifacts.folder(cp.directory+"/output.json").write_text("damaged")
        a=s.get(Artifact,x['artifact_id'])
        artifacts.folder(a.directory+"/output.json").write_text("damaged final")
    assert command(client,tid,"resume").status_code==202
    final=terminal(client,tid)
    assert final["status"]=="SUCCEEDED"
    x=step(final,"remove_background")
    assert x["checkpoints"]["reused"]==3
    assert len(step(final,"normalize_extract")["attempts"])==1
    assert step(final,"enhance_frames")["checkpoints"]["reused"]==4


def test_P07_chunk_identity_includes_all_semantics():
    spec={"cache_key":"key","semantic":{"processor_version":"mock-chunks-1","simulated":True,
          "inputs":{"frames":"abc"},"parameters":{"seed":42,"model":"v1"},
          "partition":{"version":"frames-24-v1","frames":96,"chunk_size":24}}}
    key=checkpoints.definitions(spec)[0]["chunk_key"]
    for field,value in [("inputs",{"frames":"changed"}),("parameters",{"seed":43,"model":"v2"}),
                        ("processor_version","v2"),("partition",{"version":"frames-12-v2","frames":96,"chunk_size":12}),
                        ("simulated",False)]:
        changed={**spec,"semantic":{**spec["semantic"],field:value}}
        assert checkpoints.definitions(changed)[0]["chunk_key"]!=key


def test_P13_hash_valid_but_wrong_chunk_schema_is_rejected():
    from app.pipeline import digest
    identity={"schema":1,"start":0,"end":24,"step_cache_key":"test"}
    key=digest(identity)
    work=artifacts.folder("schema-test")
    work.mkdir()
    # Even a correctly sealed file cannot substitute progress for a checkpoint.
    artifacts.write_json(work/"output.json",{"simulated":True,"frame_index":24})
    artifacts.seal(work,key)
    sha=artifacts.file_hash(work/"manifest.json")
    assert artifacts.verify("schema-test",sha)
    assert not checkpoints.valid_chunk("schema-test",sha,identity,key)
    artifacts.write_json(work/"output.json",{"simulated":True,"chunk_identity":identity})
    artifacts.seal(work,key)
    sha=artifacts.file_hash(work/"manifest.json")
    assert checkpoints.valid_chunk("schema-test",sha,identity,key)
    assert not checkpoints.valid_chunk("schema-test",sha,{**identity,"end":48},key)


def test_P12_interrupted_step_recomputes_only_bad_and_missing_chunks(client,workers):
    p=workers(ENABLE_TEST_FAULTS="1",TEST_PARTIAL_CHUNK="2")
    tid=v2(client)
    before=wait_for(client,tid,lambda d:step(d,"remove_background")["checkpoints"]["completed"]==2)
    x=step(before,"remove_background")
    old=x["attempts"][-1]
    os.kill(old["executor_pid"],signal.SIGKILL);p.kill();p.wait()
    with Session() as s:
        bad=s.get(Checkpoint,x["checkpoints"]["items"][1]["id"])
        artifacts.folder(bad.directory+"/output.json").write_text("corrupted before recovery")
    workers()
    final=terminal(client,tid)
    assert final["status"]=="SUCCEEDED"
    result=step(final,"remove_background")["checkpoints"]
    assert result["reused"]==1
    assert result["items"][0]["id"]==x["checkpoints"]["items"][0]["id"]
    assert result["items"][1]["id"]!=bad.id
    with Session() as s:
        assert s.get(Checkpoint,bad.id).valid is False
        new_attempt=step(final,"remove_background")["attempts"][-1]["id"]
        assert len(list(s.scalars(select(Checkpoint).where(Checkpoint.producer_attempt_id==new_attempt))))==3


def test_P08_stale_checkpoint_progress_and_summary_rejected(client,workers):
    p=workers(ENABLE_TEST_FAULTS="1",TEST_PARTIAL_CHUNK="2")
    tid=v2(client)
    d=wait_for(client,tid,lambda d:step(d,"remove_background")["checkpoints"]["completed"]==2)
    a=step(d,"remove_background")["attempts"][-1]
    spec=json.loads(artifacts.folder(f'work/{a["id"]}/spec.json').read_text())
    os.kill(a["executor_pid"],signal.SIGKILL);p.kill();p.wait()
    workers()
    final=terminal(client,tid)
    assert final["status"]=="SUCCEEDED"
    with Session() as s:
        cp=s.scalar(select(Checkpoint).where(Checkpoint.producer_attempt_id==a["id"],Checkpoint.chunk_index==0))
        blob={k:getattr(cp,k) for k in ["directory","manifest_digest","content_digest","manifest"]}
    assert not checkpoints.register(spec,0,blob)
    assert not service.heartbeat(a["id"],spec["worker_id"],spec["fence"],99)
    assert not service.completed(a["id"],spec["worker_id"],spec["fence"],blob)
    again=client.get(f'/api/tasks/{tid}').json()
    assert step(again,"remove_background")["artifact_id"]==step(final,"remove_background")["artifact_id"]


def test_P09_active_duplicate_chunk_ack_and_cancel_race(client,workers):
    p=workers(ENABLE_TEST_FAULTS="1",TEST_PARTIAL_CHUNK="2")
    tid=v2(client)
    d=wait_for(client,tid,lambda d:step(d,"remove_background")["checkpoints"]["completed"]==2)
    a=step(d,"remove_background")["attempts"][-1]
    spec=json.loads(artifacts.folder(f'work/{a["id"]}/spec.json').read_text())
    with Session() as s:
        cp=s.scalar(select(Checkpoint).where(Checkpoint.producer_attempt_id==a["id"],Checkpoint.chunk_index==1))
        blob={k:getattr(cp,k) for k in ["directory","manifest_digest","content_digest","manifest"]}
    assert checkpoints.register(spec,1,blob)
    assert checkpoints.register(spec,1,blob)
    with Session() as s:
        assert len(list(s.scalars(select(CheckpointUse).where(CheckpointUse.attempt_id==a["id"]))))==2
    import threading
    gate=threading.Barrier(2)
    def ack():gate.wait();return checkpoints.register(spec,1,blob)
    def cancel():gate.wait();return command(client,tid,"cancel")
    with ThreadPoolExecutor(2) as pool:
        f1,f2=pool.submit(ack),pool.submit(cancel)
        f1.result();assert f2.result().status_code==202
    final=terminal(client,tid)
    assert final["status"]=="CANCELLED"
    assert not checkpoints.register(spec,1,blob)


def test_P10_history_pagination_and_timing_metrics(client,workers):
    workers()
    tid=v2(client)
    d=terminal(client,tid)
    # Add real committed audit events, not fake task states, to exercise >100 pagination.
    for batch in range(3):
        with Session.begin() as s:
            t,steps,attempts=service.locked(s,tid)
            for i in range(45):
                target=steps[i%len(steps)]
                a=next(a for a in attempts if a.step_id==target.id)
                service.event(s,t,"TEST_MILESTONE",target,a,batch=batch,index=i)
    initial=client.get(f'/api/tasks/{tid}').json()
    cursor=initial["event_cursor"]+1
    seen=[]
    while True:
        r=client.get(f'/api/tasks/{tid}/events',params={"before_seq":cursor,"limit":23}).json()
        seen.extend(e["seq"] for e in r["items"])
        if not r["has_more"]:break
        cursor=r["older_cursor"]
    assert len(seen)>100 and sorted(seen)==list(range(1,initial["event_cursor"]+1))
    target=step(d,"remove_background")
    r=client.get(f'/api/tasks/{tid}/events',params={"step_key":target['key'],"limit":500}).json()
    assert r['items'] and all(e['step_key']==target['key'] for e in r['items'])
    aid=target['attempts'][0]['id']
    r=client.get(f'/api/tasks/{tid}/events',params={"attempt_id":aid,"limit":500}).json()
    assert r['items'] and all(e['attempt_id']==aid for e in r['items'])
    samples=client.get('/api/resources/history').json()
    assert samples['items'] and all(x['sample']['source']=='mock' for x in samples['items'])
    assert samples['policy']['raw_hours']==1 and samples['policy']['minute_days']==7
    timing=client.get(f'/api/tasks/{tid}/timings').json()
    assert all(x['queue_seconds'] is not None and x['run_seconds'] is not None for x in timing['items'])
    metrics=client.get('/metrics')
    assert metrics.status_code==200 and 'version=0.0.4' in metrics.headers['content-type']
    assert 'pipeline_checkpoints_total 8' in metrics.text
    assert not any(x in metrics.text for x in ['task_id=', 'attempt_id=', tid, aid])


def test_P11_telemetry_retention_keeps_control_and_gaps(client):
    from app.telemetry import maintain
    from app.db import db_now
    from datetime import timedelta
    w=Worker();w.register()
    with Session.begin() as s:
        now=db_now(s);slot=s.scalar(select(ResourceSlot))
        for i in range(5):
            s.add(ResourceSample(slot_id=slot.id,measured_at=(now-timedelta(hours=2)).replace(second=i,microsecond=0),
                resolution='raw',status='AVAILABLE',sample={'source':'mock','valid':False,'utilization_percent':None}))
        s.add(ResourceSample(slot_id=slot.id,measured_at=now-timedelta(days=8),resolution='minute',status='SUMMARY',sample={'valid':True}))
    maintain()
    with Session() as s:
        rows=list(s.scalars(select(ResourceSample)))
        assert len(rows)==1 and rows[0].resolution=='minute'
        assert rows[0].sample['valid'] is False and rows[0].sample['utilization_percent'] is None
        assert s.get(ResourceSlot,slot.id)
