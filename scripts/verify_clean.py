"""Verify a fresh checkout against new isolated volumes; never removes volumes."""
import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx


def main():
    p=argparse.ArgumentParser()
    p.add_argument("checkout",type=Path)
    args=p.parse_args()
    root=args.checkout.resolve()
    output=Path(__file__).resolve().parents[1]/"evidence/p1-clean-start.json"
    env=dict(os.environ,TASKD_SUBNET="10.233.87.0/24")
    override=root/"runtime/compose.clean.yaml"
    override.parent.mkdir(exist_ok=True)
    override.write_text('''services:
  postgres:
    ports: !override ["127.0.0.1:55435:5432"]
  api:
    ports: !override ["127.0.0.1:18082:8000"]
networks:
  default:
    ipam:
      config: !override [{subnet: 10.233.87.0/24}]
''',encoding="utf-8")
    dc=["docker","compose","-p","taskd-p1-clean","-f",str(root/"compose.yaml"),"-f",str(override)]
    def run(cmd,timeout=60):
        r=subprocess.run(cmd,cwd=root,env=env,text=True,capture_output=True,timeout=timeout)
        assert r.returncode==0,r.stdout+r.stderr
        return r.stdout
    existing=run(["docker","volume","ls","--format","{{.Name}}"])
    volumes=["taskd-p1-clean_database","taskd-p1-clean_artifacts"]
    assert not any(v in existing.splitlines() for v in volumes),"Fresh-volume proof requires new volumes"
    record={"passed":False,"started_at":datetime.now(timezone.utc).isoformat(),
        "source_commit":run(["git","rev-parse","HEAD"]).strip(),"source_remote":run(["git","remote","get-url","origin"]).strip(),
        "project":"taskd-p1-clean","fresh_volumes":volumes,
        "notes":"Fresh anonymous checkout and new data volumes; cached base images and locked dependency layers reused, not an empty host."}
    run([*dc,"up","--build","-d","api","worker"],timeout=180)
    with httpx.Client(base_url="http://127.0.0.1:18082",trust_env=False,timeout=15) as c:
        for _ in range(80):
            try:
                if c.get("/health/ready").status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.5)
        initial=c.get("/api/tasks").json()
        assert initial["items"]==[],initial
        record["initial_tasks"]=0
        record["migration"]=run([*dc,"run","--rm","migrate","alembic","current"]).strip()
        assert "0002" in record["migration"]
        results=[]
        for version in ["animation-v2","animation-v1"]:
            r=c.post("/api/tasks",headers={"Idempotency-Key":str(uuid.uuid4())},json={
                "name":"P1 fresh checkout "+version,"pipeline_version":version,"cache_read":False,"step_seconds":.1})
            assert r.status_code==202,r.text
            tid=r.json()["task_id"]
            end=time.monotonic()+50
            while time.monotonic()<end:
                d=c.get(f"/api/tasks/{tid}").json()
                if d["status"] in {"SUCCEEDED","FAILED"}:break
                time.sleep(.2)
            assert d["status"]=="SUCCEEDED",d
            downloads=[]
            for step in d["steps"]:
                a=step["artifact"]
                for f in a["manifest"]["files"]:
                    r=c.get(f'/api/artifacts/{a["id"]}/files/{f["key"]}')
                    assert r.status_code==200 and hashlib.sha256(r.content).hexdigest()==f["sha256"]
                    downloads.append({"step":step["key"],"file":f["key"],"sha256":f["sha256"]})
            if version=="animation-v2":
                assert all(s["checkpoints"]["completed"]==4 for s in d["steps"] if s["spec"].get("partition"))
            results.append({"version":version,"snapshot":d,"verified_downloads":downloads})
        run([*dc,"restart","api","worker"])
        for _ in range(50):
            try:
                if c.get("/health/ready").status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.5)
        for result in results:
            again=c.get('/api/tasks/'+result['snapshot']['id']).json()
            assert again["status"]=="SUCCEEDED"
            assert [s['artifact_id'] for s in again['steps']]==[s['artifact_id'] for s in result['snapshot']['steps']]
        record.update(passed=True,finished_at=datetime.now(timezone.utc).isoformat(),results=results,restart_persistence=True)
        output.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"PASS fresh checkout {record['source_commit']}: v2 + frozen v1, downloads, restart persistence")


if __name__=="__main__":main()
