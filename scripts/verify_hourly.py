"""Real >=60min MOCK run, interrupted after two committed chunks. Host helper.

Start in a background process; evidence is written after each poll. Reinvoking
does not create another task if a live controller is already running: check its
PID/process identity and observation timestamp before launching another helper.
"""
import json
import os
import subprocess
import time
import uuid
import argparse
from datetime import datetime, timezone
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
ENV=dict(os.environ,TASKD_SUBNET="10.233.86.0/24")
DC=["docker","compose","-p","taskd-hour","-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose.hourly.yaml")]


def dc(*args):
    p=subprocess.run([*DC,*args],env=ENV,cwd=ROOT,text=True,capture_output=True,timeout=45)
    if p.returncode:raise RuntimeError(p.stdout+p.stderr)
    return p.stdout


def utc():return datetime.now(timezone.utc).isoformat()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--resume",action="store_true",help="Reattach a stopped controller to the same task; never submit another task")
    args=parser.parse_args()
    out=ROOT/"evidence"
    out.mkdir(exist_ok=True)
    status_path=out/"hourly.json"
    if status_path.exists() and not args.resume:
        raise RuntimeError("Existing hourly evidence: inspect it and its controller before starting another run")
    import psutil
    record={"passed":False,"status":"STARTING","controller_pid":os.getpid(),
            "controller_created":psutil.Process().create_time(),"controller_started_at":utc(),
            "config":{"profile":"hourly","gpu_step_seconds":1800,"chunk_seconds":450,
                      "heartbeat_seconds":5,"lease_seconds":30,"project":"taskd-hour"}}
    if args.resume:
        previous=json.loads(status_path.read_text(encoding="utf-8"))
        try:
            p=psutil.Process(previous["controller_pid"])
            if p.is_running() and abs(p.create_time()-previous["controller_created"])<.1:
                raise RuntimeError("The previous controller is still live; do not start a duplicate")
        except psutil.NoSuchProcess:
            pass
        if not previous.get("task_id"):raise RuntimeError("No submitted task to resume observing")
        if previous.get("passed"):raise RuntimeError("This run already completed")
        interruptions=previous.get("controller_interruptions",[])
        interruptions.append({"failure":previous.get("failure"),"last_observation_at":previous.get("last_observation_at"),"reattached_at":utc()})
        record={**previous,"status":"RUNNING","passed":False,"controller_pid":os.getpid(),
                "controller_created":psutil.Process().create_time(),"controller_interruptions":interruptions}
        record.pop("failure",None);record.pop("finished_at",None)
    def save():status_path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    save()
    try:
        if not args.resume:dc("up","--no-build","-d","api","worker")
        c=httpx.Client(base_url="http://127.0.0.1:18081",timeout=10,trust_env=False)
        for _ in range(60):
            try:
                if c.get("/health/ready").status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.5)
        if args.resume:
            tid=record["task_id"]
            started=time.monotonic()-(datetime.now(timezone.utc)-datetime.fromisoformat(record["started_at"])).total_seconds()
        else:
            started=time.monotonic()
            r=c.post("/api/tasks",headers={"Idempotency-Key":str(uuid.uuid4())},json={
                "name":"P1 · real 60-minute recovery verification","pipeline_version":"animation-v2",
                "execution_profile":"hourly","step_seconds":.2,"cache_read":False})
            assert r.status_code==202,r.text
            tid=r.json()["task_id"]
            record.update(task_id=tid,started_at=utc(),status="RUNNING",polls=0,kill=None)
        save()
        old_chunk_ids=record.get("kill",{}).get("checkpoint_ids",[]) if record.get("kill") else []
        last_cursor=0
        end=started+10800
        with (out/"hourly-observations.jsonl").open("a",encoding="utf-8",buffering=1) as log:
            while time.monotonic()<end:
                try:
                    response=c.get(f"/api/tasks/{tid}");response.raise_for_status();d=response.json()
                    response=c.get("/api/resources");response.raise_for_status();resources=response.json()
                except httpx.HTTPError as exc:
                    log.write(json.dumps({"at":utc(),"observation_gap":repr(exc)})+"\n")
                    record["last_observation_error"]=repr(exc);save();time.sleep(5);continue
                assert "steps" in d,d
                matte=next(x for x in d["steps"] if x["key"]=="remove_background")
                cp=matte["checkpoints"]
                observation={"at":utc(),"elapsed_seconds":time.monotonic()-started,
                    "status":d["status"],"event_cursor":d["event_cursor"],
                    "steps":[{"key":x["key"],"status":x["status"],"progress":x["progress"],
                        "checkpoints":x["checkpoints"],"attempts":[{k:a[k] for k in
                        ["id","number","status","heartbeat_at","lease_expires_at"]} for a in x["attempts"]]}
                        for x in d["steps"]],"resource_states":[{"slot":x["id"],"status":x["status"],
                        "holder":x["holder"],"sample_fresh":x["sample_fresh"]} for x in resources["slots"]]}
                log.write(json.dumps(observation,ensure_ascii=False)+"\n")
                record.update(last_observation_at=observation["at"],elapsed_seconds=observation["elapsed_seconds"],polls=record["polls"]+1)
                assert d["event_cursor"]>=last_cursor
                last_cursor=d["event_cursor"]
                if not record["kill"] and cp and cp["completed"]==2:
                    old_chunk_ids=[x["id"] for x in cp["items"]]
                    record["kill"]={"at":utc(),"old_attempt":matte["attempts"][-1]["id"],
                                    "checkpoint_ids":old_chunk_ids,"command":"docker compose kill -s SIGKILL worker"}
                    save()
                    dc("kill","-s","SIGKILL","worker")
                    assert c.get(f"/api/tasks/{tid}").status_code==200
                    dc("up","--no-build","-d","worker")
                if d["status"] in {"FAILED","CANCELLED"}:raise AssertionError(d)
                if d["status"]=="SUCCEEDED":
                    elapsed=time.monotonic()-started
                    assert elapsed>=3600,elapsed
                    database_elapsed=(datetime.fromisoformat(d["finished_at"])-datetime.fromisoformat(d["created_at"])).total_seconds()
                    assert database_elapsed>=3600,database_elapsed
                    assert record["kill"]
                    assert [a["status"] for a in matte["attempts"]]==["LOST","SUCCEEDED"]
                    assert cp["reused"]==2 and [x["id"] for x in cp["items"][:2]]==old_chunk_ids
                    commits=[]
                    cursor=0
                    while True:
                        page=c.get(f"/api/tasks/{tid}/events",params={"after_seq":cursor,"limit":500}).json()
                        commits.extend(e for e in page["items"] if e["type"]=="CHECKPOINT_COMMITTED")
                        if not page["items"]:break
                        cursor=page["next_cursor"]
                    assert len(commits)==8 and len({e["payload"]["chunk_key"] for e in commits})==8
                    for _ in range(20):
                        resources=c.get("/api/resources").json()
                        if all(x["status"]=="AVAILABLE" for x in resources["slots"]):break
                        time.sleep(.5)
                    assert all(x["status"]=="AVAILABLE" for x in resources["slots"])
                    record.update(status="SUCCEEDED",passed=True,finished_at=utc(),elapsed_seconds=elapsed,
                                  database_elapsed_seconds=database_elapsed,
                                  final_snapshot=d,checkpoint_commits=commits,final_resources=resources)
                    save()
                    print(f"PASS actual elapsed={elapsed:.2f}s task={tid}",flush=True)
                    return
                save()
                time.sleep(10)
        raise TimeoutError("Long run exceeded its 3h verification bound")
    except Exception as exc:
        record.update(status="FAILED",failure=repr(exc),finished_at=utc())
        save()
        raise


if __name__=="__main__":main()
