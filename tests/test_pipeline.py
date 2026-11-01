import json
import os
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app import artifacts, service
from app.config import settings
from app.db import Session, db_now, engine
from app.execution_lock import acquire, is_stopped
from app.models import Artifact, Attempt, CacheEntry, Event, ResourceSlot, Step, Task
from app.pipeline import TaskInput, digest, validate_dag
from app.worker import Worker
from conftest import step, wait_for


def create(client, **body):
    # Preserve the original P0 assertions against its frozen processor versions;
    # P1 scenarios explicitly request animation-v2 in test_p1.py.
    data = {"pipeline_version": "animation-v1", "step_seconds": .07, "cache_read": False, **body}
    r = client.post("/api/tasks", json=data, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


def terminal(client, tid):
    return wait_for(client, tid, lambda t: t["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"})


def command(client, tid, action, key=None, body=None):
    return client.post(f"/api/tasks/{tid}/{action}", json=body or {}, headers={"Idempotency-Key": key or str(uuid.uuid4())})


def manual_claim(client, **body):
    tid = create(client, **body)
    w = Worker()
    w.register()
    service.reconcile()
    spec = service.claim(w.id)
    assert spec and not spec.get("cached")
    return tid, w, spec


def blob_for(spec):
    p = artifacts.folder(f'work/{spec["id"]}')
    p.mkdir(parents=True, exist_ok=True)
    artifacts.write_json(p / "output.json", {"simulated": True, "key": spec["cache_key"]})
    artifacts.seal(p, spec["cache_key"])
    return artifacts.publish(spec["id"])


def test_A01_normal_dag_downloads(client, workers):
    workers()
    tid = create(client, step_seconds=.2)
    d = terminal(client, tid)
    assert d["status"] == "SUCCEEDED"
    assert len(d["steps"]) == 7 and all(x["status"] == "SUCCEEDED" for x in d["steps"])
    for x in d["steps"]:
        for parent in x["spec"]["deps"]:
            assert step(d, parent)["attempts"][-1]["finished_at"] <= x["attempts"][0]["claimed_at"]
        a = x["artifact"]
        assert a["available"]
        for f in a["manifest"]["files"]:
            r = client.get(f'/api/artifacts/{a["id"]}/files/{f["key"]}')
            assert r.status_code == 200
    assert client.get("/").status_code == 200
    assert client.get(f"/tasks/{tid}").status_code == 200


def test_A02_A03_concurrent_idempotency(client):
    key = str(uuid.uuid4())
    def send():
        return client.post("/api/tasks", json={}, headers={"Idempotency-Key": key})
    with ThreadPoolExecutor(6) as pool:
        rs = list(pool.map(lambda _: send(), range(6)))
    assert all(x.status_code == 202 for x in rs)
    assert len({x.json()["task_id"] for x in rs}) == 1
    assert client.post("/api/tasks", json={"parameters": {"seed": 99}}, headers={"Idempotency-Key": key}).status_code == 409
    with Session() as s:
        assert len(list(s.scalars(select(Task)))) == 1


def test_A04_transient_retry_preserves_upstream(client, workers):
    workers()
    d = terminal(client, create(client, scenario="transient_once"))
    assert d["status"] == "SUCCEEDED"
    assert [a["status"] for a in step(d, "remove_background")["attempts"]] == ["FAILED", "SUCCEEDED"]
    assert len(step(d, "normalize_extract")["attempts"]) == 1


@pytest.mark.parametrize("scenario,attempts", [("always_fail", 3), ("gpu_oom", 1), ("invalid_input", 1)])
def test_A05_A06_retry_budget_and_permanent_errors(client, workers, scenario, attempts):
    workers()
    d = terminal(client, create(client, scenario=scenario))
    assert d["status"] == "FAILED"
    target = "inspect" if scenario == "invalid_input" else "remove_background"
    assert len(step(d, target)["attempts"]) == attempts
    assert step(d, "package")["status"] == "BLOCKED"


def test_A07_A10_real_supervisor_and_child_kill(client, workers):
    p = workers()
    tid = create(client, scenario="slow_step", step_seconds=.1)
    d = wait_for(client, tid, lambda t: step(t, "remove_background")["attempts"] and step(t, "remove_background")["attempts"][0]["started_at"])
    old = step(d, "remove_background")["attempts"][0]
    deadline = time.monotonic() + 3
    while not artifacts.folder(f'work/{old["id"]}/partial.tmp').exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert artifacts.folder(f'work/{old["id"]}/partial.tmp').exists()
    # Real SIGKILL, not a database state change. Kill child too to emulate container termination.
    os.kill(old["executor_pid"], signal.SIGKILL)
    p.kill(); p.wait()
    assert client.get(f"/api/tasks/{tid}").status_code == 200
    assert not step(client.get(f"/api/tasks/{tid}").json(), "remove_background")["artifact"]
    workers()
    d = terminal(client, tid)
    assert d["status"] == "SUCCEEDED"
    assert [a["status"] for a in step(d, "remove_background")["attempts"]] == ["LOST", "SUCCEEDED"]
    assert len(step(d, "normalize_extract")["attempts"]) == 1


def test_A08_stale_completion_renewal_and_progress(client):
    tid, w, old = manual_claim(client)
    blob = blob_for(old)
    time.sleep(settings.lease_seconds + .1)
    service.reconcile()
    service.release_confirmed(old["id"])
    time.sleep(settings.retry_base_seconds * 1.2)
    service.reconcile()
    new = service.claim(w.id)
    assert new["fence"] > old["fence"]
    assert not service.completed(old["id"], w.id, old["fence"], blob)
    assert not service.heartbeat(old["id"], w.id, old["fence"], 99)
    assert service.heartbeat(new["id"], w.id, new["fence"], 20)
    d = client.get(f"/api/tasks/{tid}").json()
    assert step(d, "inspect")["progress"] == 20
    assert step(d, "inspect")["attempts"][0]["status"] == "LOST"


def test_completion_ack_replay_is_idempotent(client):
    tid, w, spec = manual_claim(client)
    blob = blob_for(spec)
    assert service.completed(spec["id"], w.id, spec["fence"], blob)
    assert service.completed(spec["id"], w.id, spec["fence"], blob)
    assert not service.completed(spec["id"], w.id, spec["fence"], {**blob,"manifest_digest":"0"*64})
    with Session() as s:
        assert len(list(s.scalars(select(Artifact).where(Artifact.producer_attempt_id==spec["id"])))) == 1


@pytest.mark.parametrize("phase", ["after_claim", "after_publish", "after_commit"])
def test_A09_A11_A12_commit_crash_windows(client, workers, phase):
    p = workers(ENABLE_TEST_FAULTS="1", TEST_CRASH_PHASE=phase, TEST_CRASH_STEP="remove_background")
    tid = create(client)
    deadline = time.monotonic() + 12
    while p.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert p.poll() == 91
    before = client.get(f"/api/tasks/{tid}").json()
    old = step(before, "remove_background")["attempts"][0]
    if phase == "after_publish":
        assert artifacts.folder(f'published/{old["id"]}/manifest.json').exists()
        assert step(before, "remove_background")["artifact"] is None
    workers()
    after = terminal(client, tid)
    assert after["status"] == "SUCCEEDED"
    states = [a["status"] for a in step(after, "remove_background")["attempts"]]
    assert states == (["SUCCEEDED"] if phase == "after_commit" else ["LOST", "SUCCEEDED"])


def test_A13_A27_resume_invalidates_corrupt_ancestors(client, workers):
    workers()
    tid = create(client, scenario="resume_preview")
    d = terminal(client, tid)
    assert d["status"] == "FAILED"
    assert len(step(d, "render_preview")["attempts"]) == 3
    # Corrupt an ancestor only after task failure, then explicitly resume.
    aid = step(d, "enhance_frames")["artifact_id"]
    with Session() as s:
        a = s.get(Artifact, aid)
        artifacts.folder(a.directory + "/output.json").write_text("damaged")
    key = str(uuid.uuid4())
    first = command(client, tid, "resume", key)
    assert first.status_code == 202
    assert command(client, tid, "resume", key).json() == first.json()
    d = terminal(client, tid)
    assert d["status"] == "SUCCEEDED" and d["epoch"] == 2
    assert len(step(d, "remove_background")["attempts"]) == 1
    assert len(step(d, "enhance_frames")["attempts"]) == 2
    assert len(step(d, "build_atlas")["attempts"]) == 2
    assert len(step(d, "render_preview")["attempts"]) == 4


def test_A14_A15_A16_A17_cache_semantics_integrity(client, workers):
    workers()
    first = terminal(client, create(client))
    assert first["status"] == "SUCCEEDED"
    second = terminal(client, create(client, cache_read=True))
    assert all(x["status"] == "CACHED" and not x["attempts"] for x in second["steps"])
    changed = terminal(client, create(client, cache_read=True, parameters={"preview_format": "json"}))
    assert step(changed, "build_atlas")["status"] == "CACHED"
    assert step(changed, "render_preview")["status"] == "SUCCEEDED"
    assert step(changed, "package")["status"] == "SUCCEEDED"
    seeded = terminal(client, create(client, cache_read=True, parameters={"seed": 77, "enhance_model": "enhance-mock-v2"}))
    assert step(seeded, "normalize_extract")["status"] == "CACHED"
    assert step(seeded, "remove_background")["status"] == "SUCCEEDED"
    assert step(seeded, "enhance_frames")["status"] == "SUCCEEDED"
    # Real execution mode is deliberately unavailable in P0, but semantic identities
    # cannot collide with a future real adapter's identity.
    with Session() as s:
        t = s.get(Task, first["id"])
        identity = service.semantic(t, t.pipeline[0], {})
    assert digest(identity) != digest({**identity, "simulated": False})
    key = step(first, "remove_background")["cache_key"]
    with Session() as s:
        cache = s.get(CacheEntry, key)
        a = s.get(Artifact, cache.artifact_id)
        artifacts.folder(a.directory + "/output.json").unlink()
    bad = terminal(client, create(client, cache_read=True))
    assert bad["status"] == "SUCCEEDED"
    assert step(bad, "remove_background")["status"] == "SUCCEEDED"
    assert any(e["type"] == "CACHE_INVALIDATED" for e in bad["events"])


def test_A18_single_gpu_queue_without_attempt(client, workers):
    workers()
    first = create(client, scenario="slow_step", step_seconds=.1)
    wait_for(client, first, lambda t: step(t, "remove_background")["status"] == "RUNNING")
    second = create(client)
    d = wait_for(client, second, lambda t: step(t, "remove_background")["wait_reason"] == "GPU_BUSY")
    assert not step(d, "remove_background")["attempts"]
    rs = client.get("/api/resources").json()
    gpu = next(x for x in rs["slots"] if x["kind"] == "gpu")
    assert gpu["holder"]["task_id"] == first and gpu["sample"]["source"] == "mock"
    assert command(client, first, "cancel").status_code == 202
    assert terminal(client, second)["status"] == "SUCCEEDED"


def test_A19_orphan_child_keeps_resource_quarantined(client, workers):
    p = workers()
    tid = create(client, scenario="slow_step", step_seconds=.1)
    d = wait_for(client, tid, lambda t: step(t, "remove_background")["attempts"] and step(t, "remove_background")["attempts"][0]["started_at"])
    old = step(d, "remove_background")["attempts"][0]
    p.kill(); p.wait()  # Only supervisor dies. The separate-session child survives.
    try:
        time.sleep(settings.lease_seconds + .2)
        service.reconcile()
        assert not is_stopped(old["id"])
        with Session() as s:
            assert s.get(ResourceSlot, old["slot_id"]).status == "QUARANTINED"
        w = Worker(); w.register(); w.cleanup()
        time.sleep(settings.retry_base_seconds * 1.3)
        service.reconcile()
        assert service.claim(w.id) is None
        with Session() as s:
            assert len(list(s.scalars(select(Attempt).where(Attempt.step_id==old["step_id"])))) == 1
        w.cleanup()
        with Session() as s:
            assert s.get(ResourceSlot, old["slot_id"]).status == "QUARANTINED"
    finally:
        os.kill(old["executor_pid"], signal.SIGKILL)
    deadline = time.monotonic() + 3
    while not is_stopped(old["id"]) and time.monotonic() < deadline:
        time.sleep(.05)
    w.cleanup()
    with Session() as s:
        assert s.get(ResourceSlot, old["slot_id"]).status == "AVAILABLE"


def test_A20_heartbeat_distinct_from_hung_progress(client, workers):
    workers()
    tid = create(client, scenario="hang_step", step_timeout_seconds=.9)
    d = wait_for(client, tid, lambda t: step(t, "remove_background")["attempts"] and step(t, "remove_background")["attempts"][0]["phase"] == "EXECUTING")
    original = step(d, "remove_background")["attempts"][0]
    time.sleep(.45)
    current = step(client.get(f"/api/tasks/{tid}").json(), "remove_background")
    assert current["progress"] == 0
    assert current["attempts"][0]["heartbeat_at"] > original["heartbeat_at"]
    final = terminal(client, tid)
    assert final["status"] == "FAILED"
    assert all(a["status"] == "TIMED_OUT" for a in step(final, "remove_background")["attempts"])


@pytest.mark.parametrize("winner", ["cancel", "complete"])
def test_A21_cancellation_completion_order(client, winner):
    tid, w, spec = manual_claim(client)
    blob = blob_for(spec)
    if winner == "cancel":
        assert command(client, tid, "cancel").status_code == 202
        assert not service.completed(spec["id"], w.id, spec["fence"], blob)
        assert service.claim(w.id) is None
    else:
        # Finish each DAG node under real service transactions, then cancellation conflicts.
        assert service.completed(spec["id"], w.id, spec["fence"], blob)
        for _ in range(6):
            service.reconcile()
            spec = service.claim(w.id)
            assert spec
            assert service.completed(spec["id"], w.id, spec["fence"], blob_for(spec))
        assert command(client, tid, "cancel").status_code == 409


def test_A23_retry_backoff_survives_worker_restart(client, workers):
    p = workers()
    # Increase retry delay in BOTH workers so a restart cannot move the deadline.
    p.terminate(); p.wait()
    p = workers(RETRY_BASE_SECONDS="2")
    tid = create(client, scenario="transient_once")
    d = wait_for(client, tid, lambda t: step(t, "remove_background")["status"] == "RETRY_WAIT")
    next_at = step(d, "remove_background")["next_run_at"]
    p.terminate(); p.wait()
    workers(RETRY_BASE_SECONDS="2")
    time.sleep(.3)
    d = client.get(f"/api/tasks/{tid}").json()
    assert step(d, "remove_background")["next_run_at"] == next_at
    assert len(step(d, "remove_background")["attempts"]) == 1
    assert terminal(client, tid)["status"] == "SUCCEEDED"


def test_concurrent_final_completion_and_cancel(client):
    import threading
    tid, w, spec = manual_claim(client)
    for _ in range(6):
        assert service.completed(spec["id"], w.id, spec["fence"], blob_for(spec))
        spec = service.claim(w.id)
        assert spec
    assert spec["key"] == "package"
    blob = blob_for(spec)
    gate = threading.Barrier(2)
    def finish():
        gate.wait()
        return service.completed(spec["id"], w.id, spec["fence"], blob)
    def cancel():
        gate.wait()
        try:
            return service.command(tid,"cancel",str(uuid.uuid4()))["status"]
        except service.Problem as exc:
            assert exc.status==409
            return "CONFLICT"
    with ThreadPoolExecutor(2) as pool:
        f1, f2 = pool.submit(finish), pool.submit(cancel)
        result, cancelled = f1.result(), f2.result()
    d=client.get(f"/api/tasks/{tid}").json()
    assert (result and cancelled=="CONFLICT" and d["status"]=="SUCCEEDED") or (not result and cancelled=="CANCELLED" and d["status"]=="CANCELLED")


def test_A24_A26_snapshots_reconnect_and_event_cursor(client, workers):
    workers()
    tid = create(client, step_seconds=.12)
    seen, cursor = [], 0
    for _ in range(150):
        d = client.get(f"/api/tasks/{tid}").json()
        if d["status"] == "SUCCEEDED":
            assert all(x["status"] in {"SUCCEEDED", "CACHED"} for x in d["steps"])
        r = client.get(f"/api/tasks/{tid}/events?after_seq={cursor}&limit=3").json()
        seen.extend(e["seq"] for e in r["items"])
        cursor = r["next_cursor"]
        if d["status"] == "SUCCEEDED" and cursor == d["event_cursor"]:
            break
        time.sleep(.03)
    assert seen == list(range(1, cursor + 1))
    # New HTTP client, no retained page state.
    from fastapi.testclient import TestClient
    from app.api import app
    with TestClient(app) as new_client:
        final = new_client.get(f"/api/tasks/{tid}").json()
        assert final["status"] == "SUCCEEDED" and final["event_cursor"] == cursor


def test_A25_two_workers_one_current_attempt(client):
    tid = create(client)
    w1, w2 = Worker(), Worker()
    w1.register(); w2.register()
    service.reconcile()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda w: service.claim(w.id), [w1, w2]))
    assert sum(r is not None for r in results) == 1
    with Session() as s:
        attempts = list(s.scalars(select(Attempt).where(Attempt.task_id == tid)))
        assert len(attempts) == 1
        slots = list(s.scalars(select(ResourceSlot).where(ResourceSlot.holder_id == attempts[0].id)))
        assert len(slots) == 1


def test_A28_api_validation_and_command_dedup(client):
    assert client.get("/api/tasks/does-not-exist").status_code == 404
    assert client.post("/api/tasks", json={}).status_code == 422
    for body in [{"pipeline_version": "unknown"}, {"parameters": {"fps": -1}}, {"asset_id": "../../secret"}, {"simulated": False}]:
        assert client.post("/api/tasks", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 422
    tid = create(client)
    assert command(client, tid, "resume").status_code == 409
    key = str(uuid.uuid4())
    first = command(client, tid, "cancel", key)
    assert command(client, tid, "cancel", key).json() == first.json()
    assert command(client, tid, "resume").status_code == 409
    for graph in [[{"key":"a","deps":["missing"]}], [{"key":"a","deps":["b"]},{"key":"b","deps":["a"]}]]:
        with pytest.raises(ValueError): validate_dag(graph)


def test_task_deadline_does_not_reset_on_restart(client, workers):
    workers()
    d = terminal(client, create(client, task_timeout_seconds=.4, step_seconds=2))
    assert d["status"] == "FAILED" and d["stop_reason"] == "TASK_DEADLINE_EXCEEDED"
