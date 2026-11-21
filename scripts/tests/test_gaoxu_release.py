"""Windows launcher regressions using temporary build files and an isolated HTTP service."""
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from scripts import gaoxu_release as release


class ConsoleUpgrade(unittest.TestCase):
    def test_empty_scheduler_retires_its_own_instance_on_reload(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/"empty.sqlite"
            marker=Path(temp)/"retire"
            proc=subprocess.Popen([sys.executable,"-B","-m","distribution.engine","--db",str(path)],
                                  cwd=release.ROOT/"backend",env=os.environ|{"GAOXU_RELOAD_FILE":str(marker),
                                                                            "GAOXU_BUILD":"local-release"})
            try:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and proc.poll() is None:
                    if path.exists():
                        db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
                        try:
                            row=db.execute("""SELECT build,pid FROM worker_build JOIN health
                                ON health.name='worker' WHERE singleton=1""").fetchone()
                            if row and row[0]=="local-release" and row[1]>0:
                                break
                        except sqlite3.OperationalError:
                            pass  # The new process is still initializing its private database.
                        finally:
                            db.close()
                    time.sleep(.04)
                else:
                    self.fail("Fresh scheduler never reported its release and heartbeat")
                marker.touch()
                self.assertEqual(proc.wait(timeout=8),0)
                self.assertFalse(marker.exists())
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

    def test_incomplete_build_leaves_old_index_and_detects_same_size_damage(self):
        with TemporaryDirectory() as temp:
            live=Path(temp)/"live"
            staged=Path(temp)/"staged"
            live.mkdir()
            staged.mkdir()
            (live/"index.html").write_bytes(b"old")
            content={"index.html":b"new","app.js":b"content"}
            info=dict(source="next",files={k:hashlib.sha256(v).hexdigest() for k,v in content.items()})
            (staged/release.MANIFEST).write_text(json.dumps(info),encoding="ascii")
            (staged/"index.html").write_bytes(content["index.html"])
            with patch.multiple(release,DIST=live,NEXT=staged):
                with self.assertRaises(ValueError):
                    release.activate("next")
                self.assertEqual((live/"index.html").read_bytes(),b"old")
                (staged/"app.js").write_bytes(content["app.js"])
                release.activate("next")
                self.assertTrue(release.current(live,"next"))
                (live/"app.js").write_bytes(b"changed")
                self.assertFalse(release.current(live,"next"))

    def test_running_http_request_finishes_before_service_retires(self):
        child="""
import sys,time
from pathlib import Path
from unittest.mock import patch
from waitress import create_server
from distribution.runtime import serve_console
root=Path(sys.argv[1])
def bind(*args,**kwargs):
    server=create_server(*args,**kwargs)
    (root/'port').write_text(str(server.effective_port))
    return server
def response(environ,start_response):
    (root/'started').touch()
    while not (root/'finish').is_file():
        time.sleep(.04)
    start_response('200 OK',[('Content-Type','text/plain')])
    return [b'complete']
with patch('waitress.create_server',bind):
    serve_console(response,0)
"""
        with TemporaryDirectory() as temp:
            root=Path(temp)
            proc=subprocess.Popen([sys.executable,"-B","-c",child,temp],cwd=release.ROOT/"backend",
                                  env=os.environ|{"GAOXU_RELOAD_FILE":str(root/"reload")})
            def until(path):
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and proc.poll() is None:
                    if path.is_file() and (path.name!="port" or path.stat().st_size):
                        return
                    time.sleep(.04)
                self.fail(f"Service did not reach {path.name}")
            try:
                until(root/"port")
                client=build_opener(ProxyHandler({}))
                url="http://127.0.0.1:"+(root/"port").read_text()+"/"
                def request():
                    with client.open(url,timeout=12) as res:
                        return res.read()
                with ThreadPoolExecutor(1) as pool:
                    pending=pool.submit(request)
                    until(root/"started")
                    (root/"reload").touch()
                    time.sleep(1)
                    self.assertIsNone(proc.poll())
                    (root/"finish").touch()
                    self.assertEqual(pending.result(timeout=5),b"complete")
                self.assertEqual(proc.wait(timeout=8),0)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
