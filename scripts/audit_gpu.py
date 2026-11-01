"""Final independent evidence audit; does not mutate task/attempt state."""
import json
import re
import subprocess
from datetime import datetime,timezone
from pathlib import Path
from xml.etree import ElementTree as ET
import httpx
from verify_gpu import dc

ROOT=Path(__file__).resolve().parents[1];E=ROOT/'evidence'
def read(name):return json.loads((E/name).read_text(encoding='utf-8'))
def main():
    cases=ET.parse(E/'gpu-regression-junit.xml').findall('.//testcase')
    assert len(cases)==43 and all(not any(c.find(k) is not None for k in ['failure','error','skipped']) for c in cases)
    for p in ['test_G01_','test_G02_','test_G03_','test_G04_','test_G05_','test_A25_','test_P02_']:
        assert any(c.attrib['name'].startswith(p) for c in cases),p
    h=read('gpu-hardware.json');extra=read('gpu-extra.json')
    assert h['passed'] and len(h['checks'])==7 and all(x['passed'] for x in h['checks'])
    assert extra['passed'] and 'process exit=-9' in json.dumps(extra)
    assert read('gpu-upgrade.json')['passed'] and read('gpu-clean-final.json')['passed'] and read('gpu-ui.json')['passed']
    checks={x['id']:x for x in h['checks']}
    assert 'cuMemAllocFromPoolAsync: CUDA 2:' in checks['H04_REAL_BOUNDED_OOM']['executor_log']
    assert 'pool_max_bytes=16777216' in checks['H04_REAL_BOUNDED_OOM']['executor_log']
    assert checks['H06_KILL_RECOVERY_FENCE']['retained_chunk_ids']
    assert 'stale chunk/progress/completion rejected' in checks['H06_KILL_RECOVERY_FENCE']['stale_proof']
    c=httpx.Client(base_url='http://127.0.0.1:18085',trust_env=False,timeout=20)
    ids=[checks[k]['snapshot']['id'] for k in ['H01_UUID_KERNEL_NVML','H05_CANCEL_CLEANUP','H06_KILL_RECOVERY_FENCE']]+[extra['snapshot']['id']]
    confirmed=[]
    for tid in ids:
        d=c.get('/api/tasks/'+tid).json()
        for step in d['steps']:
            for a in step['attempts']:
                if a['execution']:
                    assert a['cleanup_state']=='CONFIRMED' and a['execution']['executor_stopped_at']
                    confirmed.append({'task_id':tid,'attempt_id':a['id'],'state':a['status'],
                        'executor_stopped_at':a['execution']['executor_stopped_at']})
    resource=next(x for x in c.get('/api/resources').json()['slots'] if x.get('device',{}).get('mode')=='nvidia')
    assert resource['status']=='AVAILABLE' and resource['sample_fresh'] and resource['sample']['source']=='nvml'
    assert all(datetime.fromisoformat(resource['measured_at'])>=datetime.fromisoformat(a['executor_stopped_at']) for a in confirmed)
    metric=c.get('/metrics').text
    assert 'pipeline_nvml_valid_samples 1' in metric and 'task_id=' not in metric and 'attempt_id=' not in metric
    if resource['sample']['utilization_percent'] is None:assert 'pipeline_nvml_utilization_percent' not in metric
    for f in ['0001.sql','0001_initial.py','0002.sql','0002_p1.py']:
        path='migrations/versions/'+f
        prior=subprocess.run(['git','show','27cd3d0:'+path],capture_output=True,text=True,check=True).stdout
        assert prior==(ROOT/path).read_text(encoding='utf-8')
    for path in [ROOT/'README.md',ROOT/'docs/11-真实GPU实现与验收.md',E/'gpu-acceptance.md']:
        for target in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
            if '://' not in target and not target.startswith('#'):assert (path.parent/target).exists(),target
    proof={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'pytest_passed':len(cases),'hardware_scenarios':8,
        'physical_device':resource['device'],'confirmed_executors':confirmed,'final_resource':resource,
        'legacy_migrations_unchanged':True,'clean_public_checkout_verified':True,'default_mock_without_nvml_verified':True,
        'ui_explicit_nvidia_verified':True,'regression_overlap_invalidated_and_guard_added':True,
        'gpu_image_id':subprocess.run(['docker','inspect','taskd-gpu-worker-1','--format','{{.Image}}'],capture_output=True,text=True,check=True).stdout.strip(),
        'nvml_package_version':dc('exec','-T','worker','python','-c',"import importlib.metadata;print(importlib.metadata.version('nvidia-ml-py'))").strip(),
        'limitations':['Real CUDA integer probe, not AI inference','WSL utilization unavailable; process view may omit applications',
            'Pool-limited real driver OOM, not whole-card exhaustion','Single-node whole-card scheduling; no external application isolation']}
    (E/'gpu-completion-audit.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    (E/'gpu-metrics-final.prom').write_text(metric,encoding='utf-8')
    print(f'PASS final audit: {len(cases)} tests; 8 hardware cases; {len(confirmed)} exited GPU executors; migrations intact')


if __name__=='__main__':main()
