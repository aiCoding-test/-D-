"""Linux flock inherited by the executor: survives supervisor death.

Recovery probes the same shared-volume file. An unlocked file proves there is
no authorized local executor retaining this attempt's lifetime lock, even across
container PID namespaces. Workers gate spawning with a fresh DB authorization.
"""
import fcntl

from app.artifacts import folder


def acquire(attempt_id):
    path = folder(f"locks/{attempt_id}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def is_stopped(attempt_id):
    f = acquire(attempt_id)
    if f is None:
        return False
    f.close()
    return True
