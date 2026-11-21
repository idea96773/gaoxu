"""Listener and extension observations for the optional local draft bridge."""
import logging
import threading
import time
from contextlib import contextmanager

from .store import transaction


def status(db):
    now=int(time.time())
    runtime=db.execute("SELECT port,check_only,phase,seen FROM zhihu_bridge_runtime WHERE singleton=1").fetchone()
    identity=db.execute("SELECT user_id,name FROM draft_bridge WHERE singleton=1 AND seen BETWEEN ? AND ?",
                        (now-45,now)).fetchone()
    online=bool(runtime and 0<=now-runtime["seen"]<=30)
    phase=runtime["phase"] if online else "offline"
    if runtime and (not online or phase not in ("connected","working")):
        identity=None
    if not runtime and identity:  # A running bridge from an earlier version.
        phase="connected"
    connected=bool(identity or (online and phase=="working"))
    if phase=="connected" and not identity:
        phase="checking"
    return dict(connected=connected,account=dict(identity) if identity and identity["user_id"] else None,
                bridge=dict(online=online or bool(identity),phase=phase,port=runtime["port"] if online else None,
                            check_only=bool(online and runtime["check_only"])))


def heartbeat(path,uid=None,name="",*,session=None,phase="connected"):
    with transaction(path) as db:
        if session and not db.execute("UPDATE zhihu_bridge_runtime SET phase=? WHERE session=?",
                                      (phase,session)).rowcount:
            return
        if uid is None:
            db.execute("DELETE FROM draft_bridge WHERE singleton=1")
        else:
            db.execute("INSERT OR REPLACE INTO draft_bridge VALUES(1,?,?,?)",(uid,name,int(time.time())))


@contextmanager
def listener(path,port,check_only,session):
    stopped=threading.Event()
    with transaction(path) as db:
        db.execute("DELETE FROM draft_bridge")
        db.execute("INSERT OR REPLACE INTO zhihu_bridge_runtime VALUES(1,?,?,?,'listening',?)",
                   (session,port,check_only,int(time.time())))
    def pulse():
        while not stopped.wait(5):
            try:
                with transaction(path) as db:
                    db.execute("UPDATE zhihu_bridge_runtime SET seen=? WHERE session=?",(int(time.time()),session))
            except Exception:
                logging.exception("草稿桥心跳写入失败")
    thread=threading.Thread(target=pulse,daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        with transaction(path) as db:
            if db.execute("DELETE FROM zhihu_bridge_runtime WHERE session=?",(session,)).rowcount:
                db.execute("DELETE FROM draft_bridge")
