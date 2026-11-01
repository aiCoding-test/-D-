import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
from sqlalchemy.exc import SQLAlchemyError

from app import artifacts, service
from app.config import settings
from app.execution_lock import acquire, is_stopped
from app.models import uid
from app.logging_setup import configure

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
configure()
log = logging.getLogger("worker")


class Worker:
    def __init__(self):
        self.id = uid()
        self.active = {}
        self.running = True
        self.last_heartbeat = 0.0
        self.watchdog_done = threading.Event()

    def register(self):
        service.worker_register(self.id, os.getpid(), psutil.Process().create_time(), socket.gethostname())

    def fault(self, phase, spec):
        """Local integration-test hook; never accepted through the HTTP API."""
        if (settings.test_faults and os.getenv("TEST_CRASH_PHASE") == phase
                and spec["key"] == os.getenv("TEST_CRASH_STEP", "remove_background")
                and spec["epoch_number"] == 1):
            os._exit(91)

    def launch(self, spec):
        self.fault("after_claim", spec)
        lock = acquire(spec["id"])
        if lock is None:
            return
        try:
            if not service.start_authorized(spec["id"], self.id, spec["fence"]):
                return
            work = artifacts.folder(f'work/{spec["id"]}')
            work.mkdir(parents=True, exist_ok=True)
            artifacts.write_json(work / "spec.json", spec)
            logfile = (work / "executor.log").open("ab")
            try:
                p = subprocess.Popen([sys.executable, "-m", "app.executor", spec["id"]],
                    stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile,
                    pass_fds=(lock.fileno(),), start_new_session=True)
            finally:
                logfile.close()
            # The child inherited the locked open-file description. Parent closing
            # it does not release the child's lock. This also closes a crash window.
            self.active[spec["id"]] = {"p": p, "spec": spec, "last_ack": time.monotonic()}
            if not service.started(spec["id"], self.id, spec["fence"], p.pid, psutil.Process(p.pid).create_time()):
                self.stop_child(spec["id"])
        finally:
            lock.close()

    def stop_child(self, aid):
        entry = self.active.get(aid)
        if entry:
            p = entry["p"]
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait(timeout=3)
            self.active.pop(aid, None)

    def supervise(self, heartbeat_due):
        for aid, entry in list(self.active.items()):
            spec, p = entry["spec"], entry["p"]
            work = artifacts.folder(f"work/{aid}")
            if not service.start_authorized(aid, self.id, spec["fence"]):
                self.stop_child(aid)
                continue
            if p.poll() is not None:
                result_path = work / "result.json"
                if result_path.exists() and p.returncode == 0:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if result.get("ok"):
                        blob = artifacts.publish(aid)
                        self.fault("after_publish", spec)
                        service.completed(aid, self.id, spec["fence"], blob)
                        self.fault("after_commit", spec)
                    else:
                        service.failed(aid, self.id, spec["fence"], result["code"], result["retryable"])
                else:
                    service.failed(aid, self.id, spec["fence"], "EXECUTOR_EXITED", True)
                self.active.pop(aid, None)
            elif heartbeat_due:
                progress = None
                try:
                    progress = json.loads((work / "progress.json").read_text(encoding="utf-8"))["done"]
                except (OSError, ValueError, KeyError):
                    pass
                if service.heartbeat(aid, self.id, spec["fence"], progress):
                    entry["last_ack"] = time.monotonic()
                else:
                    self.stop_child(aid)

    def cleanup(self):
        for aid, _ in service.quarantine_candidates():
            if is_stopped(aid):
                service.release_confirmed(aid)

    def tick(self):
        due = time.monotonic() - self.last_heartbeat >= settings.heartbeat_seconds
        if due:
            service.worker_heartbeat(self.id)
            self.last_heartbeat = time.monotonic()
        service.reconcile()
        self.supervise(due)
        self.cleanup()
        if len(self.active) < 3:
            spec = service.claim(self.id)
            if spec and not spec.get("cached"):
                self.launch(spec)

    def watchdog(self):
        # This thread performs no DB I/O: DNS/connect/driver timeouts must not
        # prevent stopping computation when a lease can no longer be trusted.
        while not self.watchdog_done.wait(min(.1, settings.heartbeat_seconds)):
            for aid, entry in list(self.active.items()):
                if time.monotonic() - entry["last_ack"] >= settings.lease_seconds * .7:
                    try:
                        self.stop_child(aid)
                    except (OSError, subprocess.TimeoutExpired):
                        log.exception("lease_watchdog_stop_failed attempt=%s", aid)

    def run(self):
        self.register()
        watchdog = threading.Thread(target=self.watchdog, daemon=True, name="lease-watchdog")
        watchdog.start()
        log.info("worker_started id=%s node=%s", self.id, settings.node_id)
        try:
            while self.running:
                try:
                    self.tick()
                except SQLAlchemyError:
                    # No new work while disconnected. Never assume renewal succeeded.
                    log.exception("database_unavailable")
                    for aid, entry in list(self.active.items()):
                        if time.monotonic() - entry["last_ack"] >= settings.lease_seconds * 0.7:
                            self.stop_child(aid)
                except (OSError, ValueError, service.Problem):
                    log.exception("worker_operation_failed")
                time.sleep(settings.poll_seconds)
        finally:
            self.watchdog_done.set()
            watchdog.join(timeout=1)
            for aid in list(self.active):
                self.stop_child(aid)
            # Leave persisted leases for recovery; do not erase execution history.


if __name__ == "__main__":
    worker = Worker()
    signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "running", False))
    signal.signal(signal.SIGINT, lambda *_: setattr(worker, "running", False))
    worker.run()
