"""Independent live/final audit of the real hourly task; never changes task state."""
import argparse
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
import psutil

ROOT=Path(__file__).resolve().parents[1]
E=ROOT/"evidence"
def utc():return datetime.now(timezone.utc).isoformat()
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
def load(name):return json.loads((E/name).read_text(encoding="utf-8"))
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--observe",action="store_true")
    args=p.parse_args()
    record=load("hourly.json")
    with httpx.Client(base_url="http://127.0.0.1:18081",trust_env=False,timeout=15) as c:
        if args.observe:
            proc=psutil.Process(record["controller_pid"])
            assert proc.is_running() and abs(proc.create_time()-record["controller_created"])<.1
            d=c.get('/api/tasks/'+record['task_id']).json()
            target=next(s for s in d['steps'] if s['status']=='RUNNING')['attempts'][-1]
            dc=['docker','compose','-p','taskd-hour','-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'compose.hourly.yaml')]
            def signal_executor(sig):
                subprocess.run([*dc,'exec','-T','worker','python','-c',
                    f"import os,signal;os.kill({int(target['executor_pid'])},signal.{sig})"],
                    text=True,capture_output=True,timeout=15,check=True)
            rows=[];pause_at=utc()
            signal_executor('SIGSTOP')
            try:
                time.sleep(1)  # Let the supervisor consume the final existing progress file.
                for i in range(2):
                    r=c.get('/api/tasks/'+record['task_id']);r.raise_for_status();d=r.json()
                    active=next(s for s in d['steps'] if s['status']=='RUNNING')
                    a=active['attempts'][-1]
                    assert a['id']==target['id']
                    rows.append({'at':utc(),'step':active['key'],'attempt':a['id'],'progress':active['progress'],
                        'business_progress_at':active['last_progress_at'],'heartbeat_at':a['heartbeat_at'],
                        'lease_expires_at':a['lease_expires_at'],'completed_chunks':active['checkpoints']['completed'],
                        'cursor':d['event_cursor']})
                    if i==0:time.sleep(7)
            finally:
                signal_executor('SIGCONT')
            assert rows[0]['attempt']==rows[1]['attempt']
            assert rows[0]['heartbeat_at']!=rows[1]['heartbeat_at']
            assert rows[0]['progress']==rows[1]['progress']
            assert rows[0]['business_progress_at']==rows[1]['business_progress_at']
            assert rows[1]['business_progress_at']!=rows[1]['heartbeat_at']
            proof={'passed':True,'controller_identity_verified':True,'observations':rows,
                'executor_pause':{'pid':target['executor_pid'],'at':pause_at,'resumed_at':utc(),
                    'operation':'Actual child SIGSTOP, then SIGCONT in finally; worker/control loop not stopped'},
                'meaning':'Heartbeat and lease advance while business progress is unchanged during a real hourly task; hanging-progress timeout also covered by P0 test_A20.'}
            (E/'p1-hourly-live.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS live API/heartbeat/business-progress separation')
            return
        assert record.get('passed') and record['status']=='SUCCEEDED','Wait for actual hourly completion'
        r=c.get('/api/tasks/'+record['task_id']);r.raise_for_status();d=r.json()
        elapsed=(datetime.fromisoformat(d['finished_at'])-datetime.fromisoformat(d['created_at'])).total_seconds()
        assert elapsed>=3600 and record['elapsed_seconds']>=3600
        assert load('p1-hourly-live.json')['passed'] and load('p1-hourly-browser.json')['passed']
        matte=next(x for x in d['steps'] if x['key']=='remove_background')
        assert [a['status'] for a in matte['attempts']]==['LOST','SUCCEEDED']
        assert matte['checkpoints']['reused']==2
        assert [x['id'] for x in matte['checkpoints']['items'][:2]]==record['kill']['checkpoint_ids']
        assert len(record['checkpoint_commits'])==8
        assert len({e['payload']['chunk_key'] for e in record['checkpoint_commits']})==8
        downloads=[]
        for s in d['steps']:
            a=s['artifact'];assert a['available']
            for f in a['manifest']['files']:
                r=c.get(f'/api/artifacts/{a["id"]}/files/{f["key"]}')
                assert r.status_code==200 and hashlib.sha256(r.content).hexdigest()==f['sha256']
                if f['key'].startswith('block-'):
                    index=int(f['key'].removeprefix('block-').removesuffix('.json'))
                    payload=r.json();identity=payload['chunk_identity']
                    cp=next(x for x in s['checkpoints']['items'] if x['index']==index)
                    assert set(payload)=={'simulated','chunk_identity'} and payload['simulated'] is True
                    assert identity['schema']==1 and digest(identity)==cp['chunk_key']
                    assert (identity['start'],identity['end'])==(cp['start'],cp['end'])
                downloads.append({'step':s['key'],'file':f['key'],'sha256':f['sha256']})
        samples=[];cursor=None
        while True:
            page=c.get('/api/resources/history',params={'limit':500,**({'cursor':cursor} if cursor else {})}).json()
            samples.extend(page['items']);cursor=page['next_cursor']
            if not cursor:break
        assert samples and all(s['sample']['source']=='mock' for s in samples)
        resources=c.get('/api/resources').json()
        assert all(s['status']=='AVAILABLE' and not s['holder'] for s in resources['slots'])
        metric=c.get('/metrics');assert metric.status_code==200
        assert 'task_id=' not in metric.text and 'attempt_id=' not in metric.text
        observations=[json.loads(line) for line in (E/'hourly-observations.jsonl').read_text(encoding='utf-8').splitlines()]
        measured=[x for x in observations if 'elapsed_seconds' in x]
        assert measured[-1]['elapsed_seconds']>=3600
        event_end=utc()
        events=subprocess.run(['docker','events','--since',record['started_at'],'--until',event_end,
            '--filter','type=container','--filter','label=com.docker.compose.project=taskd-hour','--format','{{json .}}'],
            text=True,encoding='utf-8',capture_output=True,timeout=20,check=True).stdout
        (E/'p1-hourly-docker-events.jsonl').write_text(events,encoding='utf-8')
        proof={'passed':True,'at':utc(),'task_id':d['id'],'created_at':d['created_at'],'finished_at':d['finished_at'],
            'database_elapsed_seconds':elapsed,'observer_elapsed_seconds':record['elapsed_seconds'],
            'kill':record['kill'],'lease_seconds':30,'heartbeat_seconds':5,
            'lease_intervals_seconds':[(datetime.fromisoformat(a['lease_expires_at'])-datetime.fromisoformat(a['heartbeat_at'])).total_seconds()
                for s in d['steps'] for a in s['attempts']],
            'successful_chunk_commits':len(record['checkpoint_commits']),'reused_chunk_ids':record['kill']['checkpoint_ids'],
            'verified_downloads':downloads,'resource_history_count':len(samples),
            'history_resolutions':sorted({s['resolution'] for s in samples}),
            'resource_policy':page['policy'],'final_resources':resources,
            'api_observations':len(measured),'observation_gaps':len([x for x in observations if 'observation_gap' in x]),
            'controller_interruptions':record.get('controller_interruptions',[]),
            'docker_events_note':'Docker historical buffer is bounded; older SIGKILL may no longer be present. The live controller operation record, old LOST/new Attempt timeline and checkpoint lineage are the primary interruption evidence.',
            'boundary':'Actual elapsed MOCK control and chunk recovery, not real GPU/model throughput or model-state restoration.'}
        assert all(abs(t-30)<.01 for t in proof['lease_intervals_seconds'])
        (E/'p1-hourly-audit.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
        (E/'p1-hourly-metrics.prom').write_text(metric.text,encoding='utf-8')
        print(f'PASS final independent audit: database elapsed={elapsed:.2f}s; downloads={len(downloads)}; samples={len(samples)}')


if __name__=='__main__':main()
