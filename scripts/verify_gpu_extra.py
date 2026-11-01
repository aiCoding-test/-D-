"""Additional real child-exit and final-schema checks after full GPU acceptance."""
import json
import time
import uuid
from pathlib import Path
import httpx
from verify_gpu import dc,st,utc

ROOT=Path(__file__).resolve().parents[1]
def main():
    r={'passed':False,'started_at':utc()}
    c=httpx.Client(base_url='http://127.0.0.1:18085',trust_env=False,timeout=15)
    def detail(tid):return c.get('/api/tasks/'+tid).json()
    def wait(tid,fn):
        end=time.monotonic()+65
        while time.monotonic()<end:
            d=detail(tid)
            if fn(d):return d
            time.sleep(.12)
        raise AssertionError(d)
    response=c.post('/api/tasks',headers={'Idempotency-Key':str(uuid.uuid4())},json={
        'name':'GPU real child SIGKILL (supervisor survives)','gpu_mode':'nvidia','cache_read':False,'step_seconds':4,
        'parameters':{'seed':int(time.time())%(2**31-1)}})
    assert response.status_code==202,response.text
    tid=response.json()['task_id']
    d=wait(tid,lambda d:st(d,'remove_background')['checkpoints']['completed']==1)
    a=st(d,'remove_background')['attempts'][-1]
    dc('exec','-T','worker','python','-c',f"import os,signal;os.kill({int(a['executor_pid'])},signal.SIGKILL)")
    final=wait(tid,lambda d:d['status'] in ['SUCCEEDED','FAILED'])
    assert final['status']=='SUCCEEDED',final
    x=st(final,'remove_background')
    assert len(x['attempts'])==2 and x['attempts'][0]['error_code']=='EXECUTOR_EXITED'
    assert x['attempts'][0]['error']=='process exit=-9'
    assert x['checkpoints']['reused']==1
    # Validate ALL actual CUDA chunks using the current schema implementation,
    # including independently audited checksum/size/binding semantics.
    validation=dc('exec','-T','worker','python','-c',
        "from app.db import Session;from sqlalchemy import select;from app.models import Checkpoint;from app.checkpoints import valid_chunk;"
        "s=Session();rows=[p for p in s.scalars(select(Checkpoint)) if p.valid and p.identity['semantic'].get('simulated') is False];"
        "assert rows;assert all(valid_chunk(p.directory,p.manifest_digest,p.identity,p.chunk_key) for p in rows);"
        "print('validated real chunk count=',len(rows))")
    end=time.monotonic()+10
    while time.monotonic()<end:
        resources=c.get('/api/resources').json()
        slot=next(s for s in resources['slots'] if s.get('device',{}).get('mode')=='nvidia')
        if slot['status']=='AVAILABLE':break
        time.sleep(.2)
    assert slot['status']=='AVAILABLE'
    r.update(passed=True,finished_at=utc(),operation='actual child SIGKILL; worker survives',
        old_attempt=a,snapshot=final,schema_validation=validation,resource=slot)
    (ROOT/'evidence/gpu-extra.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS H08 real child exit -9, retry retains one block; all real chunks validate')


if __name__=='__main__':main()
