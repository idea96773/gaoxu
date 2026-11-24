"""Local process ownership and read-only scheduler diagnostics."""
import os
import json
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .store import transaction


def windows_startup_status(path):
    if os.name!="nt":
        raise ValueError("此检查适用于 Windows 管理台")
    script=Path(__file__).resolve().parents[2]/"scripts"/"windows-startup.ps1"
    shell=Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        proc=subprocess.run([str(shell),"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
                             "-File",str(script),"-Action","Status","-AsJson"],capture_output=True,
                            check=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        info=json.loads(proc.stdout.decode("utf-8-sig"))
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        raise ValueError("无法读取 Windows 自启动配置，请核对当前用户的任务计划程序权限") from exc
    database=Path(path).resolve()
    expected=info["expected_database"]
    return dict(info,database=str(database),matches=Path(expected).resolve()==database if expected else None)


def reload_request():
    value=os.environ.get("GAOXU_RELOAD_FILE")
    return Path(value) if value else None


def serve_console(app,port):
    from waitress import create_server, serve
    settings=dict(host="127.0.0.1",port=port,max_request_body_size=260*1024*1024)
    marker=reload_request()
    if marker is None:
        return serve(app,**settings)
    server=create_server(app,**settings)
    checked=0
    try:
        while True:
            server.asyncore.loop(timeout=0.4,map=server._map,count=1)
            now=time.monotonic()
            if server.accepting and now>=checked:
                checked=now+0.5
                if marker.exists():
                    server.accepting=False
            if not server.accepting:
                # Only the I/O thread closes sockets. Let accepted request bodies,
                # application transactions and buffered responses finish first.
                for channel in list(server.active_channels.values()):
                    if not (channel.request or channel.requests or channel.total_outbufs_len):
                        channel.handle_close()
                if not server.active_channels:
                    dispatcher=server.task_dispatcher
                    with dispatcher.lock:
                        if not dispatcher.active_count and not dispatcher.queue:
                            break
    finally:
        server.task_dispatcher.shutdown()
        server.close()
        marker.unlink(missing_ok=True)


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
        worker=db.execute("SELECT build,pid FROM worker_build WHERE singleton=1").fetchone()
        result.update(api_build=os.environ.get("GAOXU_BUILD"),worker_build=worker["build"] if worker else None,
                      worker_pid=worker["pid"] if worker else None)
        if details:
            from .editorial import model_retry_at
            from .daily_backup import status as backup_status
            result["backup"]=backup_status(db,path)
            config=db.execute("SELECT name FROM health WHERE name IN ('model_configured','model_unconfigured')").fetchone()
            model_jobs=db.execute("""SELECT d.state,count(*) FROM drafts d JOIN rows r ON r.id=d.row_id
                JOIN batches b ON b.id=r.batch WHERE b.control='active' AND d.state IN ('queued','generating')
                GROUP BY d.state""")
            result["model_queue"]=dict(counts=dict(model_jobs),retry_at=model_retry_at(db,now),
                                       configured=config[0]=="model_configured" if config else None)
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
