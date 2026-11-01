"""Optional NVML adapter. No CUDA context, no imports of GPU dependencies in MOCK."""
import threading
import time
from datetime import datetime, timezone
from app.config import settings


def discover():
    import pynvml as nv
    nv.nvmlInit()
    devices=[]
    allowed=set(filter(None,settings.gpu_uuids.split(',')))
    driver=nv.nvmlSystemGetDriverVersion()
    for i in range(nv.nvmlDeviceGetCount()):
        h=nv.nvmlDeviceGetHandleByIndex(i)
        uuid=nv.nvmlDeviceGetUUID(h)
        if allowed and uuid not in allowed:continue
        m=nv.nvmlDeviceGetMemoryInfo(h)
        try:cc=list(nv.nvmlDeviceGetCudaComputeCapability(h))
        except nv.NVMLError:cc=None
        devices.append({'uuid':uuid,'name':nv.nvmlDeviceGetName(h),'memory_total_mb':m.total//1048576,
            'compute_capability':cc,'driver':driver,'source':'nvml','mode':'nvidia','health':'UNKNOWN'})
    return devices


def sample(device):
    import pynvml as nv
    h=nv.nvmlDeviceGetHandleByUUID(device['uuid'])
    missing={}
    def get(field,fn):
        try:return fn()
        except nv.NVMLError as exc:missing[field]=str(exc);return None
    memory=get('memory',lambda:nv.nvmlDeviceGetMemoryInfo(h))
    utilization=get('utilization_percent',lambda:nv.nvmlDeviceGetUtilizationRates(h).gpu)
    temp=get('temperature_c',lambda:nv.nvmlDeviceGetTemperature(h,nv.NVML_TEMPERATURE_GPU))
    def processes():
        result=[]
        for p in nv.nvmlDeviceGetComputeRunningProcesses(h):
            used=p.usedGpuMemory
            result.append({'pid':p.pid,'memory_used_mb':used/1048576 if isinstance(used,int) and used<2**63 else None})
        return result
    procs=get('processes',processes)
    graphics=get('graphics_processes',lambda:[{'pid':p.pid} for p in nv.nvmlDeviceGetGraphicsRunningProcesses(h)])
    return {'source':'nvml','simulated':False,'valid':memory is not None,
        'health':'HEALTHY' if memory and (temp is None or temp<85) else 'UNHEALTHY',
        'unavailable_reason':None if memory else missing.get('memory'),
        'utilization_percent':utilization,'temperature_c':temp,
        'memory_used_mb':memory.used/1048576 if memory else None,
        'memory_free_mb':memory.free/1048576 if memory else None,
        'memory_total_mb':memory.total/1048576 if memory else None,
        'processes':procs,'graphics_processes':graphics,'unavailable_fields':missing,
        'process_scope':'NVML driver view; WSL/WDDM may omit processes, not proof of exclusive ownership'}


class Collector:
    """NVML may block: keep it outside the heartbeat/control loop and all DB locks."""
    def __init__(self):
        self.devices=discover()
        self.values={}
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True,name='nvml-collector')
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            for d in self.devices:
                try:value=sample(d)
                except Exception as exc:
                    value={'source':'nvml','simulated':False,'valid':False,'health':'UNKNOWN',
                        'unavailable_reason':str(exc),'utilization_percent':None,'memory_used_mb':None,
                        'memory_total_mb':None,'temperature_c':None,'processes':None}
                self.values[d['uuid']]=(time.monotonic(),datetime.now(timezone.utc),value)
            self.stop.wait(settings.sample_seconds)

    def snapshot(self):
        result={}
        for d in self.devices:
            when,at,value=self.values.get(d['uuid'],(0,None,{}))
            if time.monotonic()-when>=15:
                value={'source':'nvml','simulated':False,'valid':False,'health':'UNKNOWN',
                    'unavailable_reason':'NVML_SAMPLE_STALE','utilization_percent':None,
                    'memory_used_mb':None,'memory_total_mb':None,'temperature_c':None,'processes':None}
            result[d['uuid']]={'device':d,'sample':value,'measured_at':at}
        return result


def eligible(slot,request,now):
    """Return explicit rejection reason; memory is advisory, never a reservation."""
    d=slot.device or {};sample=slot.sample
    if request.get('gpu_mode','mock')=='mock':
        return None if d.get('mode','mock')=='mock' else 'NO_MATCHING_MOCK_GPU'
    if d.get('mode')!='nvidia':return 'NO_MATCHING_NVIDIA_GPU'
    need=request['gpu']
    if need['uuid'] and need['uuid']!=d.get('uuid'):return 'GPU_UUID_MISMATCH'
    if d.get('memory_total_mb',0)<need['min_memory_mb']:return 'GPU_CAPACITY_MISMATCH'
    cc=d.get('compute_capability')
    if not cc or cc[0]<need['min_compute_major']:return 'GPU_CAPABILITY_MISMATCH'
    if not slot.measured_at or (now-slot.measured_at).total_seconds()>=15:return 'GPU_SAMPLE_STALE'
    if not sample.get('valid') or sample.get('health')!='HEALTHY':return 'GPU_HEALTH_UNAVAILABLE'
    if sample.get('processes'):return 'GPU_EXTERNAL_PROCESS'
    if sample.get('memory_free_mb',0)<need['probe_memory_mb']+256:return 'GPU_MEMORY_PRESSURE'
    return None
