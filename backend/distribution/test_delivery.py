"""Approved-only, local receiver acceptance tests with real SQLite process restarts."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime

from .api import create_app
from .engine import tick
from .store import BEIJING, compact, digest, ledger_path, transaction
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

    def test_edited_delivery_cannot_satisfy_a_reprint_or_duplicate_its_receipt(self):
        edited, _ = self.freeze(self.targets[0])
        old_job = self.schedule(edited, self.slot)["id"]
        drain(self.path, self.slot)
        article, = self.client.get(BASE+"library").json["items"]
        self.assertFalse(article["deliveries"])
        self.assertEqual([j["id"] for j in article["edited_deliveries"]], [old_job])

        def submit(key, target):
            batch = self.post("reprints", dict(request_id=key, directories=[article["directory"]],
                                              targets=[target]))["id"]
            while tick(self.path, preparation=True):
                pass
            row, = self.client.get(BASE+"batches/"+batch).json["reviews"]
            return batch, row

        batch, row = submit("same-account", self.targets[0])
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["prior_jobs"], [])
        item = dict(id=row["id"], sha=row["sha"], revision=row["revision"], expected_prior={})
        planned, = self.post(f"reprints/{batch}/plan", dict(items=[item]))["items"]
        approved, = self.post(f"reprints/{batch}/approve", dict(items=[item], approved=True))["items"]
        for attempt in (planned, approved):
            self.assertIn(f"#{old_job}", attempt["error"])
        self.assertEqual(self.client.get(BASE+"batches/"+batch).json["deliveries"], [])

        second, row = submit("other-account", self.targets[1])
        item = dict(id=row["id"], sha=row["sha"], revision=row["revision"], expected_prior={})
        self.post(f"reprints/{second}/approve", dict(items=[item], approved=True))
        job, = self.client.get(BASE+"batches/"+second).json["deliveries"]
        drain(self.path, max(job["slot"], self.slot))
        received = self.detail(job["id"])["delivery"]
        self.assertTrue(received["received"]["matches"])
        self.assertEqual(received["snapshot"]["kind"], "reprint")
        _, replay = submit("repeated", self.targets[1])
        self.assertEqual(replay["state"], "duplicate")
        self.assertEqual([a["id"] for a in replay["assignments"]], [job["id"]])
        later_edit, _ = self.freeze(self.targets[1])
        conflict = self.client.post(BASE+f"editorial/{later_edit}/schedule", json={})
        self.assertEqual(conflict.status_code, 400)
        self.assertIn(f"#{job['id']}", conflict.json["error"])
        with transaction(ledger_path(self.path), False) as remote:
            self.assertEqual(remote.execute("SELECT count(*) FROM delivered_drafts").fetchone()[0], 2)

    def test_preview_releases_failed_booking_but_keeps_unreconciled_receipt(self):
        draft,_=self.freeze(self.targets[0])
        peer,_=self.freeze(self.targets[1])
        first=self.schedule(draft,self.slot)["id"]
        other=self.schedule(peer,self.slot+90)["id"]
        route=BASE+f"jobs/{other}/available-slot"
        def preview():
            result=self.client.get(route,query_string={"time":self.slot})
            self.assertEqual(result.status_code,200,result.json)
            return result.json
        proposal=preview()
        self.assertEqual(proposal["available"],self.slot+5)
        self.assertEqual(self.detail(other)["delivery"]["reschedules"],[])
        with transaction(self.path) as db:
            db.execute("UPDATE jobs SET state='failed',attempts=1 WHERE id=?",(first,))
        self.assertEqual(preview()["available"],self.slot)
        self.post(f"jobs/{first}/retry",{})
        body=dict(time=self.slot,expected_available=self.slot,
                  expected_slot=proposal["expected_slot"],expected_execution=proposal["expected_execution"])
        self.assertEqual(self.client.post(BASE+f"jobs/{other}/reschedule",json=body).status_code,400)
        self.assertEqual(self.detail(other)["job"]["slot"],self.slot+90)
        self.crash(self.slot)
        with transaction(self.path) as db:
            db.execute("UPDATE jobs SET state='failed' WHERE id=?",(first,))
        self.assertEqual(preview()["available"],self.slot+5)
        agreed=self.slot+5
        self.assertEqual(self.post(f"jobs/{other}/reschedule",body|dict(time=agreed,expected_available=agreed))["slot"],agreed)
        self.assertEqual(self.detail(other)["delivery"]["planned"],agreed)

    def test_backup_verifies_crash_window_then_rejects_mismatched_ledger(self):
        from . import backup
        draft,_=self.freeze(self.targets[0])
        job=self.schedule(draft,self.slot)["id"]
        self.crash(self.slot)
        bundle=backup.create(self.path,self.path.parent/"archive")
        report=backup.verify(bundle)
        self.assertEqual((report["delivery_receipts"],report["pending_reconciliation"]),(1,1))
        restored=backup.restore(bundle,self.path.parent/"restore")
        drain(restored/backup.DATABASES[0],self.slot+31)
        self.assertEqual(self.detail(job)["job"]["state"],"running")
        with transaction(restored/backup.DATABASES[0],False) as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?",(job,)).fetchone()[0],"done")

        # Both SQLite files are individually healthy and still match their manifest.
        ledger=bundle/backup.DATABASES[1]
        with transaction(ledger) as db:
            db.execute("UPDATE delivered_drafts SET account='另一模拟账号'")
        manifest=bundle/"manifest.json"
        content=json.loads(manifest.read_text(encoding="utf-8"))
        content["sha256"][ledger.name]=backup.checksum(ledger)
        manifest.write_text(json.dumps(content),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"冻结稿任务"):
            backup.verify(bundle)
        destination=self.path.parent/"bad-restore"
        with self.assertRaisesRegex(ValueError,"冻结稿任务"):
            backup.restore(bundle,destination)
        self.assertFalse(destination.exists())

    def test_crash_before_and_after_acceptance(self):
        did,frozen=self.freeze(self.targets[0])
        peer,_=self.freeze(self.targets[1])
        jid=self.schedule(did,self.slot)["id"]
        self.crash(self.slot,before_send=True)
        self.crash(self.slot+31)
        self.assertEqual(self.detail(jid)["job"]["state"],"running")
        # The receiver accepted the first account 31 seconds late. Use that
        # receipt when evaluating a reservation during the unexpired lease.
        conflict=self.client.post(BASE+f"editorial/{peer}/schedule",json={"time":self.slot+32})
        self.assertEqual(conflict.status_code,400)
        self.assertIn(datetime.fromtimestamp(self.slot+36,BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
                      conflict.json["error"])
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

    def test_cancel_one_account_preserves_peer_freeze_and_rejects_stale_schedule(self):
        first,_=self.freeze(self.targets[0])
        peer,_=self.freeze(self.targets[1])
        jid=self.schedule(first,self.slot)["id"]
        other=self.schedule(peer,self.slot+86400)["id"]
        before=self.detail(jid)
        sibling=self.detail(other)
        url=f"jobs/{jid}/cancel"
        stale=dict(before["cancel_guard"],due=before["cancel_guard"]["due"]+1)
        self.assertEqual(self.client.post(BASE+url,json={"expected":stale}).status_code,400)
        self.assertEqual(self.detail(jid)["job"]["state"],"ready")
        self.post("batches/"+self.bid+"/pause",{})
        for _ in range(2):
            self.assertEqual(self.post(url,{"expected":before["cancel_guard"]})["state"],"cancelled")
        after=self.detail(jid)
        self.assertEqual(after["cancellation"]["scope"],"task")
        self.assertIsNone(after["cancel_guard"])
        self.assertEqual(after["delivery"],before["delivery"])
        self.assertEqual(self.detail(other),sibling)
        self.post("batches/"+self.bid+"/resume",{})
        drain(self.path,self.slot+3*86400)
        self.assertEqual(self.detail(jid)["job"]["state"],"cancelled")
        self.assertIsNone(self.detail(jid)["job"]["receipt"])
        self.assertEqual(self.detail(other)["job"]["state"],"done")

    def test_cancel_one_recovers_existing_receipt_after_crash(self):
        draft,_=self.freeze(self.targets[0])
        jid=self.schedule(draft,self.slot)["id"]
        self.crash(self.slot)
        detail=self.detail(jid)
        self.assertEqual(detail["job"]["state"],"running")
        self.assertEqual(self.post(f"jobs/{jid}/cancel",{"expected":detail["cancel_guard"]})["state"],"done")
        reconciled=self.detail(jid)
        self.assertIsNone(reconciled["cancellation"])
        self.assertTrue(reconciled["delivery"]["received"]["matches"])

    def test_resume_preserves_future_booking_and_displays_execution_day(self):
        overdue,_=self.freeze(self.targets[0])
        booked,_=self.freeze(self.targets[1])
        first=self.schedule(overdue,self.slot)["id"]
        tomorrow=self.slot+86400
        second=self.schedule(booked,tomorrow)["id"]
        tick(self.path,tomorrow-1)
        delayed=self.detail(first)
        self.assertEqual((delayed["job"]["slot"],delayed["job"]["due"]),(self.slot,tomorrow+5))
        self.assertIsNone(delayed["delivery"]["received"])
        self.assertEqual(self.client.post(BASE+f"jobs/{first}/reschedule",json={
            "time":tomorrow+15,"expected_slot":self.slot,"expected_execution":self.slot}).status_code,400)
        self.client=create_app(self.path).test_client()
        day=datetime.fromtimestamp(tomorrow+5,BEIJING).date().isoformat()
        query=dict(start=day,end=day,date_field="execution",platform="zhihu",account=self.targets[0]["account"])
        matches=self.client.get(BASE+"article-search",query_string=query).json["items"]
        self.assertTrue(any(r["job_id"]==first and r["execution_slot"]==tomorrow+5 for r in matches))
        groups=self.client.get(BASE+"schedule-calendar",query_string=dict(month=day[:7],date_field="execution",
                               platform="zhihu",account=self.targets[0]["account"])).json["days"]
        self.assertGreaterEqual(groups[day]["ready"],1)
        self.assertTrue(tick(self.path,tomorrow))
        self.assertEqual(self.detail(second)["job"]["finished"],tomorrow)
        self.assertFalse(tick(self.path,tomorrow+4))
        self.assertTrue(tick(self.path,tomorrow+5))
        self.assertEqual(self.detail(first)["delivery"]["received"]["planned"],self.slot)

    def test_resume_honors_daily_cap_across_reserved_and_confirmed_jobs(self):
        with transaction(self.path) as db:
            rules=json.loads(db.execute("SELECT rules FROM batches WHERE id=?",(self.bid,)).fetchone()[0])
            rules["platform_cap"]=1
            db.execute("UPDATE batches SET rules=? WHERE id=?",(compact(rules),self.bid))
        frozen,_=self.freeze(self.targets[0])
        on_time,_=self.freeze(self.targets[1])
        first=self.schedule(frozen,self.slot)["id"]
        tomorrow=self.slot+86400
        second=self.schedule(on_time,tomorrow)["id"]
        self.assertTrue(tick(self.path,tomorrow))
        next_time=self.detail(first)["job"]["due"]
        self.assertGreater(datetime.fromtimestamp(next_time,BEIJING).date(),datetime.fromtimestamp(tomorrow,BEIJING).date())
        self.assertTrue(tick(self.path,tomorrow))
        self.assertEqual(self.detail(second)["job"]["finished"],tomorrow)
        self.assertTrue(tick(self.path,next_time))
        self.assertTrue(self.detail(first)["delivery"]["received"]["matches"])

    def test_conflicting_receiver_record_does_not_confirm_on_cancel(self):
        one,_=self.freeze(self.targets[0])
        two,_=self.freeze(self.targets[1])
        first=self.schedule(one,self.slot)["id"]
        second=self.schedule(two,self.slot+5)["id"]
        self.crash(self.slot)
        with transaction(ledger_path(self.path)) as db:
            db.execute("UPDATE delivered_drafts SET revision=revision+1 WHERE draft=?",(one,))
        self.post("batches/"+self.bid+"/cancel",{})
        self.assertEqual(self.detail(first)["job"]["state"],"failed")
        self.assertIsNone(self.detail(first)["job"]["receipt"])
        self.assertEqual(self.detail(second)["job"]["state"],"cancelled")

