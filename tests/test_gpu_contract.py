"""CPU-only contract tests; these are never labelled as hardware verification."""
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from app.pipeline import TaskInput,semantic,digest,SPECS
from app.gpu import eligible
from app import service
from test_pipeline import create,terminal
from conftest import step,wait_for


def test_G01_explicit_mode_and_semantic_isolation():
    with pytest.raises(ValidationError):TaskInput(gpu_mode='nvidia',scenario='gpu_oom')
    with pytest.raises(ValidationError):TaskInput(gpu={'probe_memory_mb':16})
    s=next(s for s in SPECS if s['key']=='remove_background')
    mock=TaskInput();real=TaskInput(gpu_mode='nvidia')
    def key(data):return digest(semantic(SimpleNamespace(request=data.model_dump(),input_digest='input'),s,{}))
    assert key(mock)!=key(real)
    assert key(real)!=key(real.model_copy(update={'gpu':real.gpu.model_copy(update={'probe_memory_mb':16})}))


def test_G02_device_requirements_and_freshness():
    now=datetime.now(timezone.utc);data=TaskInput(gpu_mode='nvidia').model_dump()
    slot=SimpleNamespace(device={'mode':'nvidia','uuid':'GPU-x','memory_total_mb':8192,'compute_capability':[8,9]},
        measured_at=now,sample={'valid':True,'health':'HEALTHY','memory_free_mb':7000,'processes':[]})
    assert eligible(slot,data,now) is None
    data['gpu']['min_memory_mb']=16000;assert eligible(slot,data,now)=='GPU_CAPACITY_MISMATCH'
    data['gpu']['min_memory_mb']=512;slot.measured_at=now-timedelta(seconds=16)
    assert eligible(slot,data,now)=='GPU_SAMPLE_STALE'
    slot.measured_at=now;slot.sample['processes']=[{'pid':123}]
    assert eligible(slot,data,now)=='GPU_EXTERNAL_PROCESS'
    slot.sample['processes']=[];slot.sample['health']='UNKNOWN'
    assert eligible(slot,data,now)=='GPU_HEALTH_UNAVAILABLE'


def test_G03_no_gpu_queues_without_mock_fallback(client,workers):
    workers()
    tid=create(client,pipeline_version='animation-v2',gpu_mode='nvidia')
    d=wait_for(client,tid,lambda d:step(d,'remove_background')['status']=='READY'
        and step(d,'remove_background')['wait_reason']=='NO_MATCHING_NVIDIA_GPU')
    assert not step(d,'remove_background')['attempts']
    assert d['status']=='RUNNING'


def test_G04_legacy_mock_idempotency_fields(client):
    data=TaskInput(name='unchanged')
    result=service.submit(data,'gpu-compatibility')
    from app.db import Session
    from app.models import Task
    with Session() as s:
        t=s.get(Task,result['task_id'])
        assert 'gpu_mode' not in t.request and 'gpu' not in t.request
    assert service.submit(data,'gpu-compatibility')['reused_request']


def test_G05_post_exit_sample_required_before_release(client,workers):
    # Synthetic adapter data checks the control contract, never hardware evidence.
    from app.config import settings
    from app.db import Session,db_now
    from app.models import ResourceSlot,WorkerSession,Attempt
    from sqlalchemy import select
    p=workers();tid=create(client,pipeline_version='animation-v2',gpu_mode='nvidia')
    wait_for(client,tid,lambda d:step(d,'remove_background')['wait_reason']=='NO_MATCHING_NVIDIA_GPU')
    p.terminate();p.wait(timeout=5)
    with Session.begin() as s:
        wid=s.scalar(select(WorkerSession.id))
        slot=ResourceSlot(id=settings.node_id+':GPU-contract',node_id=settings.node_id,key='GPU-contract',kind='gpu',status='AVAILABLE',
            device={'mode':'nvidia','uuid':'GPU-contract','memory_total_mb':8192,'compute_capability':[8,9]},
            measured_at=db_now(s),sample={'valid':True,'health':'HEALTHY','source':'nvml','processes':[],'memory_free_mb':7000})
        s.add(slot)
    old=settings.gpu_mode
    object.__setattr__(settings,'gpu_mode','nvidia')
    try:spec=service.claim(wid)
    finally:object.__setattr__(settings,'gpu_mode',old)
    assert spec and spec['execution']['gpu_uuid']=='GPU-contract'
    import uuid
    service.command(tid,'cancel',str(uuid.uuid4()))
    service.release_confirmed(spec['id'])  # Records first OS-stop confirmation.
    service.release_confirmed(spec['id'])  # Pre-exit healthy sample cannot release.
    with Session.begin() as s:
        slot=s.get(ResourceSlot,settings.node_id+':GPU-contract')
        assert slot.status=='QUARANTINED'
        assert s.get(Attempt,spec['id']).execution['executor_stopped_at']
        slot.measured_at=db_now(s)
    service.release_confirmed(spec['id'])
    with Session() as s:
        assert s.get(ResourceSlot,settings.node_id+':GPU-contract').status=='AVAILABLE'
