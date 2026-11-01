"""Verify current normal demo upgrade from P1 without rewriting old data."""
import json
import hashlib
import subprocess
import time
from datetime import datetime,timezone
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
def main():
    c=httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=15)
    def snapshot():
        cursor=None;result=[]
        while True:
            page=c.get('/api/tasks',params={'limit':100,**({'cursor':cursor} if cursor else {})}).json()
            for t in page['items']:
                assert t['status'] in {'SUCCEEDED','FAILED','CANCELLED'},'Do not upgrade while user tasks run'
                result.append(c.get('/api/tasks/'+t['id']).json())
            cursor=page['next_cursor']
            if not cursor:return result
    before=snapshot()
    proc=subprocess.run(['docker','compose','up','--no-build','-d','api','worker'],cwd=ROOT,text=True,capture_output=True,timeout=60)
    assert proc.returncode==0,proc.stdout+proc.stderr
    for _ in range(60):
        try:
            if c.get('/health/ready').status_code==200:break
        except httpx.HTTPError:pass
        time.sleep(.5)
    verified=[]
    for old in before:
        d=c.get('/api/tasks/'+old['id']).json()
        assert (d['status'],d['request'],d['pipeline'],d['event_cursor'])==(old['status'],old['request'],old['pipeline'],old['event_cursor'])
        for a,b in zip(old['steps'],d['steps']):
            assert a['id']==b['id'] and a['artifact_id']==b['artifact_id']
            assert len(a['attempts'])==len(b['attempts'])
            for x,y in zip(a['attempts'],b['attempts']):assert all(y[k]==v for k,v in x.items())
            if b['artifact']:
                for f in b['artifact']['manifest']['files']:
                    r=c.get(f'/api/artifacts/{b["artifact_id"]}/files/{f["key"]}')
                    assert r.status_code==200 and hashlib.sha256(r.content).hexdigest()==f['sha256']
        verified.append({'task_id':d['id'],'status':d['status'],'preserved_request_pipeline_events_attempts_artifacts':True})
    migration=subprocess.run(['docker','compose','run','--rm','migrate','alembic','current'],cwd=ROOT,text=True,capture_output=True,timeout=30)
    assert migration.returncode==0 and '0003' in migration.stdout
    assert all(s['sample']['source']=='mock' for s in c.get('/api/resources').json()['slots'])
    deps=subprocess.run(['docker','compose','exec','-T','worker','python','-c',
        "import importlib.util;assert importlib.util.find_spec('pynvml') is None;print('no NVML package in default image')"],cwd=ROOT,text=True,capture_output=True,timeout=20)
    assert deps.returncode==0,deps.stdout+deps.stderr
    record={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'migration':migration.stdout.strip(),
        'old_tasks_verified':verified,'default_dependencies':deps.stdout.strip(),'notes':'Existing P1 user data kept; default Worker remains MOCK without GPU requests'}
    (ROOT/'evidence/gpu-upgrade.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'PASS P1->0003 upgrade: {len(verified)} existing tasks and all downloads preserved; default no NVML')


if __name__=='__main__':main()
