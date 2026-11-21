"""Local process ownership and read-only scheduler diagnostics."""
import os
import time
from contextlib import contextmanager

from .store import transaction


@contextmanager
def instance_lock(file):
    with open(file,"a+b") as lock:
        lock.seek(0)
        if os.name=="nt":
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def health(path,details=False):
    now=int(time.time())
    with transaction(path,False) as db:
        seen=db.execute("SELECT seen FROM health WHERE name='worker'").fetchone()
        beat=seen[0] if seen else None
        result=dict(application="gaoxu",mode="dry-run",checked=now,worker_seen=beat,
                    worker_online=beat is not None and 0<=now-beat<18,api_pid=os.getpid())
        if details:
            queue=db.execute("""SELECT
                count(CASE WHEN j.state='ready' AND j.due<=? THEN 1 END) due,
                min(CASE WHEN j.state='ready' AND j.due<=? THEN j.due END) oldest_due,
                count(CASE WHEN j.state='running' THEN 1 END) running,
                count(CASE WHEN j.stage='deliver' AND j.state='ready' AND j.due>j.slot THEN 1 END) deferred_deliveries,
                count(CASE WHEN j.state='running' AND j.lease_until<=? THEN 1 END) expired,
                count(CASE WHEN j.state='failed' THEN 1 END) failed,
                min(CASE WHEN j.state='ready' AND j.due>? THEN j.due END) next_due
                FROM jobs j JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
                WHERE b.control='active' AND j.state IN ('ready','running','failed')""",(now,now,now,now)).fetchone()
            result.update(queue=dict(queue),
                          last_claim=db.execute("SELECT max(started) FROM attempts").fetchone()[0],
                          unknown_drafts=db.execute("SELECT count(*) FROM platform_drafts WHERE state='unknown'").fetchone()[0])
    return result
