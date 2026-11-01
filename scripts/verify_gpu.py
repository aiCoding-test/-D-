"""Real NVIDIA acceptance on an isolated Compose project. Never resets a device.

Requires an idle, user-controlled GPU and a built optional image. OOM is confined
to a 16 MiB CUDA pool; it does not fill physical VRAM. Preserve every prior run.
"""
import hashlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
DC=['docker','compose','-p','taskd-gpu','-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'compose.gpu.yaml'),'-f',str(ROOT/'compose.gpu-test.yaml')]
def utc():return datetime.now(timezone.utc).isoformat()
def dc(*args):
    r=subprocess.run([*DC,*args],cwd=ROOT,text=True,encoding='utf-8',capture_output=True,timeout=60)
    assert r.returncode==0,r.stdout+r.stderr
    return r.stdout
def st(d,key):return next(s for s in d['steps'] if s['key']==key)


def main():
    out=ROOT/'evidence/gpu-hardware.json'
    if out.exists():
        archive=out.with_name('gpu-hardware-'+datetime.now(timezone.utc).strftime('%H%M%S')+'.json')
        archive.write_bytes(out.read_bytes())
    record={'passed':False,'started_at':utc(),'project':'taskd-gpu','checks':[]}
    seed=int(time.time())%(2**31-1)
    record['seed']=seed
    def save():out.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    c=httpx.Client(base_url='http://127.0.0.1:18085',trust_env=False,timeout=20)
    def detail(tid):
        r=c.get('/api/tasks/'+tid);r.raise_for_status();return r.json()
    def wait(tid,fn,timeout=65):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            d=detail(tid)
            if fn(d):return d
            time.sleep(.12)
        raise AssertionError(d)
    def done(tid):
        d=wait(tid,lambda d:d['status'] in ['SUCCEEDED','FAILED','CANCELLED'])
        assert d['status']=='SUCCEEDED',d
        return d
    def create(**body):
        r=c.post('/api/tasks',headers={'Idempotency-Key':str(uuid.uuid4())},json={
            'name':'Real GPU acceptance','gpu_mode':'nvidia','cache_read':False,'step_seconds':.3,
            'parameters':{'seed':seed},**body})
        assert r.status_code==202,r.text
        return r.json()['task_id']
    def check(key,**body):record['checks'].append({'id':key,'passed':True,'at':utc(),**body});save();print('PASS '+key,flush=True)
    def free():
        end=time.monotonic()+20
        while time.monotonic()<end:
            d=c.get('/api/resources').json()
            slots=[s for s in d['slots'] if s.get('device',{}).get('mode')=='nvidia']
            if slots and all(s['status']=='AVAILABLE' and s['sample_fresh'] for s in slots):return slots[0]
            time.sleep(.2)
        raise AssertionError(d)
    try:
        dc('up','--no-build','-d','api','worker')
        for _ in range(60):
            try:
                if c.get('/health/ready').status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.5)
        device=free()
        assert not device['sample'].get('processes'),device
        host=subprocess.run(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader'],
            text=True,capture_output=True,check=True).stdout.strip()
        assert not host,'Other compute processes found; do not run hardware faults/OOM'
        record['host_process_precheck']=host
        record['device']=device
        record['container_cuda']=dc('exec','-T','worker','python','-c',
            "import ctypes;v=ctypes.c_int();d=ctypes.CDLL('libcuda.so.1');assert d.cuInit(0)==0;assert d.cuDriverGetVersion(ctypes.byref(v))==0;print(v.value)")
        tid=create();normal=done(tid)
        downloads=[]
        for s in normal['steps']:
            a=s['artifact']
            for f in a['manifest']['files']:
                r=c.get(f'/api/artifacts/{a["id"]}/files/{f["key"]}')
                assert r.status_code==200 and hashlib.sha256(r.content).hexdigest()==f['sha256']
                if f['key'].startswith('block-'):
                    payload=r.json();probe=payload['cuda_probe']
                    assert payload['simulated'] is False and probe['physical_uuid']==device['device']['uuid']
                    assert probe['cuda_ordinal']==0 and probe['kernel_launches']>=1 and probe['checksum_first64']==seed*64+2016
                downloads.append({'step':s['key'],'file':f['key'],'sha256':f['sha256']})
        check('H01_UUID_KERNEL_NVML',snapshot=normal,verified_downloads=downloads,resource=free())
        t1=create(name='GPU queue owner',step_seconds=3)
        wait(t1,lambda d:st(d,'remove_background')['checkpoints']['completed']>=1)
        t2=create(name='GPU queue waiter',step_seconds=.3)
        queued=wait(t2,lambda d:st(d,'remove_background')['status']=='READY' and st(d,'remove_background')['wait_reason']=='GPU_BUSY')
        assert not st(queued,'remove_background')['attempts']
        check('H02_ATOMIC_QUEUE',queued=queued,owner=done(t1),waiter=done(t2))
        impossible=create(name='GPU minimum memory mismatch',gpu={'min_memory_mb':1048576})
        blocked=wait(impossible,lambda d:st(d,'remove_background')['wait_reason']=='GPU_CAPACITY_MISMATCH')
        assert not st(blocked,'remove_background')['attempts']
        assert c.post(f'/api/tasks/{impossible}/cancel',json={},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==202
        check('H03_CONSTRAINTS_NO_ATTEMPT',snapshot=blocked)
        free()
        oom=create(name='Real driver OOM in bounded 16MiB pool',gpu={'oom_test':True})
        d=wait(oom,lambda d:d['status']=='FAILED')
        a=st(d,'remove_background')['attempts']
        assert len(a)==1 and a[0]['error_code']=='CUDA_OOM' and not a[0]['retryable']
        assert 'cuMemAllocFromPoolAsync: CUDA 2:' in a[0]['error']
        time.sleep(1)
        assert len(st(detail(oom),'remove_background')['attempts'])==1
        log=dc('exec','-T','worker','cat',f'/data/work/{a[0]["id"]}/executor.log')
        assert 'pool_max_bytes=16777216' in log and 'CUDA 2:' in log
        check('H04_REAL_BOUNDED_OOM',snapshot=d,executor_log=log,resource_after=free())
        cancel=create(name='GPU cancellation',step_seconds=8)
        d=wait(cancel,lambda d:st(d,'remove_background')['checkpoints']['completed']==1)
        a=st(d,'remove_background')['attempts'][-1]
        assert c.post(f'/api/tasks/{cancel}/cancel',json={},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==202
        stopped=wait(cancel,lambda d:d['status']=='CANCELLED')
        resource=free()
        lock=dc('exec','-T','worker','python','-c',f"from app.execution_lock import is_stopped;assert is_stopped('{a['id']}');print('executor lifetime lock released')")
        check('H05_CANCEL_CLEANUP',snapshot=stopped,lock_proof=lock,resource=resource)
        interrupted=create(name='GPU interrupted after 2 chunks',step_seconds=4)
        d=wait(interrupted,lambda d:st(d,'remove_background')['checkpoints']['completed']==2)
        old=st(d,'remove_background')['attempts'][-1]
        ids=[x['id'] for x in st(d,'remove_background')['checkpoints']['items']]
        kill_at=utc();dc('kill','-s','SIGKILL','worker')
        assert c.get('/api/tasks/'+interrupted).status_code==200
        dc('up','--no-build','-d','worker')
        recovered=done(interrupted)
        x=st(recovered,'remove_background')
        assert [a['status'] for a in x['attempts']]==['LOST','SUCCEEDED']
        assert x['checkpoints']['reused']==2 and [p['id'] for p in x['checkpoints']['items'][:2]]==ids
        stale=dc('exec','-T','worker','python','-c',
            f"import json;from app import artifacts,checkpoints,service;from app.db import Session;from app.models import Artifact,Checkpoint;"
            f"spec=json.loads(artifacts.folder('work/{old['id']}/spec.json').read_text());"
            f"assert not service.heartbeat(spec['id'],spec['worker_id'],spec['fence'],100);"
            f"s=Session();cp=s.get(Checkpoint,'{ids[0]}');blob={{k:getattr(cp,k) for k in ['directory','manifest','manifest_digest','content_digest']}};"
            f"assert not checkpoints.register(spec,0,blob);"
            f"a=s.get(Artifact,'{x['artifact_id']}');blob={{k:getattr(a,k) for k in ['directory','manifest','manifest_digest','content_digest']}};"
            f"assert not service.completed(spec['id'],spec['worker_id'],spec['fence'],blob);print('stale chunk/progress/completion rejected')")
        check('H06_KILL_RECOVERY_FENCE',kill_at=kill_at,operation='docker compose kill -s SIGKILL worker; up -d worker',
            old_attempt=old,retained_chunk_ids=ids,snapshot=recovered,stale_proof=stale,resource=free())
        cached=done(create(name='real cache hit',cache_read=True))
        assert all(s['status']=='CACHED' for s in cached['steps'])
        mock=done(create(name='MOCK must not reuse CUDA',gpu_mode='mock',cache_read=True))
        assert all(st(mock,key)['status']=='SUCCEEDED' for key in ['remove_background','enhance_frames'])
        altered=done(create(name='changed effective allocation',cache_read=True,gpu={'probe_memory_mb':16}))
        assert st(altered,'remove_background')['status']=='SUCCEEDED'
        assert st(altered,'remove_background')['cache_key']!=st(normal,'remove_background')['cache_key']
        check('H07_CACHE_CONFIG_ISOLATION',real_cached=cached,mock=mock,changed=altered)
        samples=c.get('/api/resources/history',params={'limit':500}).json()
        nvml=[s for s in samples['items'] if s['sample']['source']=='nvml'];assert nvml
        metric=c.get('/metrics');assert metric.status_code==200 and 'pipeline_nvml_valid_samples 1' in metric.text
        assert 'task_id=' not in metric.text and 'attempt_id=' not in metric.text
        (ROOT/'evidence/gpu-metrics.prom').write_text(metric.text,encoding='utf-8')
        record.update(passed=True,finished_at=utc(),resource_history=samples,final_resources=free(),
            limitations=['CUDA probe is not an AI model','WSL NVML unavailable fields remain null; process view may be incomplete'])
        save();print('PASS real hardware acceptance',flush=True)
    except Exception as exc:
        record.update(failure=repr(exc),failed_at=utc());save();raise
    finally:
        (ROOT/'evidence/gpu-worker.log').write_text(dc('logs','--no-color','worker'),encoding='utf-8')


if __name__=='__main__':main()
