"""Approved-only, local receiver acceptance tests with real SQLite process restarts."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from .api import create_app
from .engine import tick
from .store import compact, digest, ledger_path, transaction
from .test_workflow import drain, export_folder

BASE = "/api/distribution/"
URL = "https://mp.weixin.qq.com/s/test-frozen-destination"


class FrozenDelivery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.path = root/"db.sqlite"
        self.app = create_app(self.path,root)
        self.client = self.app.test_client()
        directory = export_folder(root,1)
        self.targets = [dict(platform="zhihu",account=name) for name in ("甲","乙")]
        self.bid = self.post("library",dict(request_id="case",directories=[directory.relative_to(root).as_posix()],
                             targets=self.targets,rules={"gap":5}))["id"]
        self.slot = int(time.time())+90
        drain(self.path,self.slot-90)
        self.rid = self.client.get(BASE+"batches/"+self.bid).json["rows"][0]["id"]

    def post(self, path, data):
        res = self.client.post(BASE+path,json=data)
        self.assertLess(res.status_code,300,res.json)
        return res.json

    def freeze(self, target, body="审核内容", url=URL):
        path=f"articles/{self.rid}/editorial"
        self.post(path,target|dict(action="link",link=dict(mode="direct",destination=url,verified=True)))
        did=self.post(path,target|dict(action="agent",title="证据核对",body=body))["id"]
        edit=dict(title="证据核对",body=body,edit_no=0)
        self.assertEqual(self.client.post(BASE+f"editorial/{did}/schedule",json={}).status_code,400)
        preview=self.post(f"editorial/{did}",edit|dict(action="preview"))
        self.post(f"editorial/{did}",edit|dict(action="approve",rights=True,facts=True,preview=preview))
        return did,preview

    def schedule(self,did,slot):
        return self.post(f"editorial/{did}/schedule",{"time":slot})

    def detail(self,jid):
        return self.client.get(BASE+f"jobs/{jid}").json

    def crash(self, when, before_send=False):
        code = ("from distribution.engine import claim,execute; import os,sys; "
                "p=sys.argv[1]; n=int(sys.argv[2]); ticket=claim(p,n); assert ticket; ")
        code += "os._exit(37)" if before_send else "execute(p,ticket,n,after_send=lambda:os._exit(37))"
        res = subprocess.run([sys.executable,"-c",code,str(self.path),str(when)],cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(res.returncode,37)

    def test_binding_and_concurrent_duplicates(self):
        did,frozen=self.freeze(self.targets[0])
        path=f"editorial/{did}/schedule"
        def submit(_):
            with self.app.test_client() as c:
                res=c.post(BASE+path,json={"time":self.slot})
                return res.status_code,res.json["id"]
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(submit,range(8)))
        jid=results[0][1]
        self.assertEqual({pair[1] for pair in results},{jid})
        self.assertEqual(sum(code==201 for code,_ in results),1)
        self.assertEqual(self.client.post(BASE+path,json={"time":self.slot+6}).status_code,400)
        self.assertEqual(self.client.post(BASE+path,json={"account":"乙"}).status_code,400)
        v2,_=self.freeze(self.targets[0],"已修改的第二版","https://mp.weixin.qq.com/s/other")
        self.assertEqual(self.client.post(BASE+f"editorial/{v2}/schedule",json={}).status_code,400)
        other,_=self.freeze(self.targets[1])
        self.assertEqual(self.client.post(BASE+f"editorial/{other}/schedule",json={"time":self.slot+1}).status_code,400)
        other_job=self.schedule(other,self.slot+5)["id"]
        self.assertFalse(tick(self.path,self.slot-1))
        drain(self.path,self.slot)
        d=self.detail(jid)
        self.assertEqual((d["job"]["revision"],d["job"]["account"],d["job"]["slot"]),(1,"甲",self.slot))
        self.assertEqual(d["delivery"]["snapshot"],frozen)
        self.assertTrue(d["delivery"]["received"]["matches"])
        self.assertEqual(d["delivery"]["received"]["sha"],digest(compact(frozen)))
        self.assertTrue(frozen["text"].endswith(URL))
        self.assertNotEqual(self.detail(other_job)["job"]["receipt"],d["job"]["receipt"])
        self.assertEqual(self.schedule(did,self.slot)["id"],jid)

    def test_crash_before_and_after_acceptance(self):
        did,frozen=self.freeze(self.targets[0])
        jid=self.schedule(did,self.slot)["id"]
        self.crash(self.slot,before_send=True)
        self.crash(self.slot+31)
        self.assertEqual(self.detail(jid)["job"]["state"],"running")
        self.assertEqual(self.schedule(did,self.slot)["id"],jid)
        code="from distribution.engine import tick; import sys; assert tick(sys.argv[1],int(sys.argv[2]))"
        subprocess.run([sys.executable,"-c",code,str(self.path),str(self.slot+62)],
                       cwd=Path(__file__).resolve().parents[1],check=True)
        d=self.detail(jid)
        self.assertTrue(all(a["ended"]>=a["started"] for a in d["history"]))
        self.assertEqual(d["delivery"]["received"]["account"],self.targets[0]["account"])
        self.assertEqual(d["job"]["finished"],self.slot+31)
        self.assertEqual([a["outcome"] for a in d["history"]],["interrupted","interrupted","done"])
        with transaction(ledger_path(self.path),False) as remote:
            rows=remote.execute("SELECT * FROM delivered_drafts").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(json.loads(rows[0]["snapshot"]),frozen)
            self.assertEqual(rows[0]["remote_id"],d["job"]["receipt"])

    def test_pause_and_cancel_reconcile(self):
        did,_=self.freeze(self.targets[0])
        jid=self.schedule(did,self.slot)["id"]
        self.post("batches/"+self.bid+"/pause",{})
        self.assertFalse(tick(self.path,self.slot))
        self.post("batches/"+self.bid+"/resume",{})
        self.crash(self.slot)
        self.post("batches/"+self.bid+"/cancel",{})
        self.assertTrue(self.detail(jid)["delivery"]["received"]["matches"])
        self.assertEqual(self.detail(jid)["job"]["state"],"done")

