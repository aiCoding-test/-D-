"""Host-side, real container faults in a separate disposable Compose project.

Run with Python 3.12 and httpx installed. Never stops the user's demo services.
Keeps its volumes and machine-readable evidence; `--cleanup` removes only this
script's named test containers/network, retaining volumes for inspection.
"""
import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, API_PORT="18080", TASKD_SUBNET="10.233.85.0/24",
           LEASE_SECONDS="4", HEARTBEAT_SECONDS="0.5", POLL_SECONDS="0.1", RETRY_BASE_SECONDS="0.2")
# This file intentionally binds a different database host port to avoid collision.
COMPOSE = ["docker", "compose", "-p", "taskd-e2e", "-f", str(ROOT / "compose.yaml"),
           "-f", str(ROOT / "compose.e2e.yaml")]


def dc(*args):
    p = subprocess.run([*COMPOSE, *args], env=ENV, cwd=ROOT, text=True, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stdout + p.stderr)
    return p.stdout


def wait(c, tid, predicate, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d = c.get(f"/api/tasks/{tid}").json()
        if predicate(d):
            return d
        time.sleep(.15)
    raise AssertionError(d)


def step(d, key):
    return next(x for x in d["steps"] if x["key"] == key)


def create(c, **kwargs):
    data = {"step_seconds": .2, "cache_read": False, **kwargs}
    r = c.post("/api/tasks", json=data, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--prefix", default="runtime", choices=["runtime","p1-runtime"])
    args = parser.parse_args()
    if args.cleanup:
        print(dc("down"))
        return
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "project": "taskd-e2e", "checks": []}
    c = httpx.Client(base_url="http://127.0.0.1:18080", timeout=20, trust_env=False)
    dc("up", "-d", "api", "worker")
    for _ in range(50):
        try:
            if c.get("/health/ready").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.3)
    try:
        tid = create(c, name="E2E · kill Worker container", scenario="slow_step")
        before = wait(c, tid, lambda d: step(d,"remove_background")["attempts"] and step(d,"remove_background")["attempts"][0]["started_at"])
        dc("kill", "-s", "SIGKILL", "worker")
        assert c.get(f"/api/tasks/{tid}").status_code == 200
        dc("up", "-d", "worker")
        after = wait(c, tid, lambda d: d["status"] in {"SUCCEEDED", "FAILED"})
        assert after["status"] == "SUCCEEDED", after
        assert [a["status"] for a in step(after,"remove_background")["attempts"]] == ["LOST","SUCCEEDED"]
        assert len(step(after,"normalize_extract")["attempts"]) == 1
        evidence["checks"].append({"ids":["A07","A10"],"passed":True,"task_id":tid,"operation":"docker compose kill -s SIGKILL worker; up -d worker","snapshot":after})
        print("PASS A07/A10: actual Worker container killed and recovered", flush=True)

        tid = create(c, name="E2E · PostgreSQL outage", scenario="slow_step")
        before = wait(c, tid, lambda d: step(d,"remove_background")["attempts"] and step(d,"remove_background")["attempts"][0]["started_at"])
        old = step(before,"remove_background")["attempts"][0]
        dc("stop", "postgres")
        r = c.get(f"/api/tasks/{tid}")
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "DATABASE_UNAVAILABLE"
        time.sleep(6)  # Exceed the configured 4s lease; not a substitute for killing DB.
        dc("exec", "-T", "worker", "python", "-c",
           "from app.execution_lock import is_stopped; "
           f"assert is_stopped('{old['id']}'), 'Old executor still holds its lifetime lock'")
        dc("start", "postgres")
        end=time.monotonic()+15
        while time.monotonic()<end:
            if c.get("/health/ready").status_code==200:
                break
            time.sleep(.2)
        after=wait(c,tid,lambda d:d["status"] in {"SUCCEEDED","FAILED"})
        assert after["status"]=="SUCCEEDED",after
        assert step(after,"remove_background")["attempts"][0]["status"]=="LOST"
        evidence["checks"].append({"ids":["A22"],"passed":True,"task_id":tid,"old_attempt_id":old["id"],
                                  "old_executor_stopped_while_db_down":True,"api_while_db_down":r.json(),"snapshot":after})
        print("PASS A22: actual PostgreSQL stopped; API 503; lease recovery",flush=True)

        tid=create(c,name="E2E · API restart",step_seconds=.8)
        wait(c,tid,lambda d:step(d,"inspect")["status"]=="SUCCEEDED")
        dc("restart","api")
        end=time.monotonic()+15
        while time.monotonic()<end:
            try:
                if c.get("/health/ready").status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.2)
        after=wait(c,tid,lambda d:d["status"]=="SUCCEEDED")
        assert len(after["events"])>10 and all(len(x["attempts"])==1 for x in after["steps"])
        evidence["checks"].append({"ids":["A24"],"passed":True,"task_id":tid,"operation":"docker compose restart api","snapshot":after})
        print("PASS A24: API container restarted; Worker continued; history retained",flush=True)
        evidence["passed"]=True
    except Exception as exc:
        evidence["passed"]=False
        evidence["failure"]=repr(exc)
        raise
    finally:
        # Restore only this script's test DB even when an assertion fails.
        dc("start", "postgres")
        evidence["finished_at"]=datetime.now(timezone.utc).isoformat()
        (ROOT/"evidence").mkdir(exist_ok=True)
        (ROOT/"evidence"/f"{args.prefix}.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
        raw_logs=dc("logs","--no-color","--tail","300","worker")
        (ROOT/"evidence"/f"{args.prefix}-worker.log").write_text("\n".join(line.rstrip() for line in raw_logs.splitlines())+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
