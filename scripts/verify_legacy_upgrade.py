"""Real P0 image/0001 failed task -> 0002/P1 manual continuation, separate volumes."""
import json
import argparse
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',default='taskd-p1-legacy')
    args=parser.parse_args()
    override=ROOT/'runtime/compose.legacy.yaml'
    override.parent.mkdir(exist_ok=True)
    def write(image):
        override.write_text(f'''services:
  migrate:
    image: {image}
  api:
    image: {image}
    ports: !override ["127.0.0.1:18084:8000"]
  worker:
    image: {image}
  postgres:
    ports: !override ["127.0.0.1:55437:5432"]
networks:
  default:
    ipam:
      config: !override [{{subnet: 10.233.89.0/24}}]
''',encoding='utf-8')
    dc=['docker','compose','-p',args.project,'-f',str(ROOT/'compose.yaml'),'-f',str(override)]
    def run(cmd):
        r=subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True,timeout=60)
        assert r.returncode==0,r.stdout+r.stderr
        return r.stdout
    assert args.project+'_database' not in run(['docker','volume','ls','--format','{{.Name}}']).splitlines()
    old_image=run(['docker','image','inspect','taskd-pipeline:0.1','--format','{{.Id}}']).strip()
    record={'passed':False,'started_at':datetime.now(timezone.utc).isoformat(),'old_image':old_image,'project':args.project}
    with httpx.Client(base_url='http://127.0.0.1:18084',trust_env=False,timeout=15) as c:
        def ready():
            for _ in range(60):
                try:
                    if c.get('/health/ready').status_code==200:return
                except httpx.HTTPError:pass
                time.sleep(.5)
            raise AssertionError('API not ready')
        def terminal(tid):
            for _ in range(200):
                d=c.get('/api/tasks/'+tid).json()
                if d['status'] in {'SUCCEEDED','FAILED'}:return d
                time.sleep(.25)
            raise AssertionError(d)
        write('taskd-pipeline:0.1');run([*dc,'up','--no-build','-d','api','worker']);ready()
        revision=run([*dc,'run','--rm','migrate','alembic','current']).strip();assert '0001' in revision
        r=c.post('/api/tasks',headers={'Idempotency-Key':str(uuid.uuid4())},json={
            'name':'Unfinished P0 task upgraded to P1','scenario':'resume_preview','step_seconds':.1,'cache_read':False})
        assert r.status_code==202,r.text
        tid=r.json()['task_id'];before=terminal(tid)
        assert before['status']=='FAILED' and before['request']['pipeline_version']=='animation-v1'
        assert 'execution_profile' not in before['request']
        run([*dc,'stop','api','worker'])
        write('taskd-pipeline:0.2');run([*dc,'up','--no-build','-d','api','worker']);ready()
        new_revision=run([*dc,'run','--rm','migrate','alembic','current']).strip();assert '0002' in new_revision
        after_migration=c.get('/api/tasks/'+tid).json()
        assert after_migration['request']==before['request'] and after_migration['pipeline']==before['pipeline']
        r=c.post('/api/tasks/'+tid+'/resume',headers={'Idempotency-Key':str(uuid.uuid4())},json={'expected_revision':after_migration['revision']})
        assert r.status_code==202,r.text
        final=terminal(tid);assert final['status']=='SUCCEEDED' and final['epoch']==2
        assert final['pipeline']==before['pipeline']
        for old in before['steps']:
            current=next(s for s in final['steps'] if s['key']==old['key'])
            assert current['spec']==old['spec'] and not current['checkpoints']
            if old['status']=='SUCCEEDED':
                assert current['artifact_id']==old['artifact_id']
                assert len(current['attempts'])==len(old['attempts'])
                for a,b in zip(old['attempts'],current['attempts']):
                    assert all(b[k]==v for k,v in a.items()),(a,b)
                    assert b['queue_seconds'] is None  # Not measured by the P0 executor.
        record.update(passed=True,task_id=tid,before_revision=revision,after_revision=new_revision,
            before=before,after_migration=after_migration,final=final,finished_at=datetime.now(timezone.utc).isoformat())
        (ROOT/'evidence/p1-legacy-upgrade.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print('PASS actual P0 image + 0001 -> P1 + 0002, frozen old task resumed, successful ancestors retained')
    run([*dc,'down'])  # Containers only; preserve the dedicated evidence volumes.


if __name__=='__main__':main()
