import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app.config import settings
from app.db import engine


def pytest_sessionstart(session):
    # Tests truncate their dedicated DB; independent pytest runners must serialize.
    assert engine.url.database=='taskd_test','Use the dedicated taskd_test database'
    conn=engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    if not conn.scalar(text('SELECT pg_try_advisory_lock(771030)')):
        conn.close()
        pytest.exit('Another pytest runner owns taskd_test; use a separate database or wait',returncode=2)
    session._taskd_test_lock=conn


def pytest_sessionfinish(session,exitstatus):
    conn=getattr(session,'_taskd_test_lock',None)
    if conn is not None:
        conn.execute(text('SELECT pg_advisory_unlock(771030)'))
        conn.close()


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    # Explicit safety gate: tests are allowed to truncate only the dedicated test DB.
    assert engine.url.database == "taskd_test", "Use DATABASE_URL ending in /taskd_test"
    with engine.begin() as c:
        c.execute(text("TRUNCATE tasks, steps, attempts, artifacts, cache_entries, resource_slots, worker_sessions, task_events, command_receipts CASCADE"))
    old = settings.artifact_root
    object.__setattr__(settings, "artifact_root", tmp_path)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    yield
    object.__setattr__(settings, "artifact_root", old)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def workers(tmp_path):
    processes = []
    def launch(**env):
        process_env = dict(os.environ, **env)
        logfile = (tmp_path / f"worker-{len(processes)}.log").open("wb")
        p = subprocess.Popen([sys.executable, "-m", "app.worker"], env=process_env,
                             stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((p, logfile))
        return p
    yield launch
    import signal
    import psutil
    # Only processes started by this fixture; do not touch any unrelated worker.
    for p, logfile in processes:
        if p.poll() is None:
            children = psutil.Process(p.pid).children(recursive=True)
            p.terminate()
            try:
                p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        logfile.close()


def wait_for(client, tid, predicate, timeout=25):
    deadline = time.monotonic() + timeout
    data = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{tid}")
        assert response.status_code == 200, response.text
        data = response.json()
        if predicate(data):
            return data
        time.sleep(.06)
    raise AssertionError(f"Timed out waiting for {tid}: {data}")


def step(data, key):
    return next(x for x in data["steps"] if x["key"] == key)
