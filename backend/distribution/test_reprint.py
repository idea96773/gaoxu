"""Batch reprint acceptance with generated folders and a private SQLite receiver."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from .api import create_app
from .engine import claim, execute, tick
from .reprint import DESTINATION
from .store import BEIJING
from .test_workflow import drain, export_folder

BASE = "/api/distribution/"


class Reprint(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "main.sqlite"
        self.app = create_app(self.path, self.root)
        self.client = self.app.test_client()
        self.targets = [dict(platform=p, account=a) for p in ("zhihu", "toutiao") for a in ("模拟A", "模拟B")]

    def source(self, i):
        return export_folder(self.root, i).relative_to(self.root).as_posix()

    def create(self, dirs, key="batch"):
        response = self.client.post(BASE+"reprints", json=dict(
            directories=dirs, request_id=key, targets=self.targets,
            rules=dict(gap=30, account_cap=2, platform_cap=3, start_hour=9, end_hour=18)))
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def prepare(self):
        while tick(self.path,preparation=True):
            pass

    def batch(self, bid):
        return self.client.get(BASE+"batches/"+bid).json

    def approve(self, bid, rows):
        items = [dict(id=r["id"], sha=r["sha"], revision=r["revision"]) for r in rows]
        with self.app.test_client() as client:
            result = client.post(BASE+"reprints/"+bid+"/approve", json=dict(approved=True, items=items))
            self.assertEqual(result.status_code, 200)
            return result.json["items"]

    def test_review_snapshot_isolation_export_and_duplicate(self):
        dirs = [self.source(i) for i in range(3)]
        md = self.root / dirs[0] / "正文.md"
        md.write_text(md.read_text(encoding="utf-8")+"\n\n保留原句\n\n![再次配图](img/0.png)", encoding="utf-8")
        bid = self.create(dirs+["失效目录"])
        self.assertEqual(self.create(dirs+["失效目录"]), bid)
        self.assertEqual(self.batch(bid)["counts"], {"preparing":4})
        self.prepare()
        data = self.batch(bid)
        self.assertEqual(data["counts"], dict(pending=3,invalid=1))
        self.assertEqual(data["deliveries"], [])
        first,second,third,_ = data["reviews"]
        url = BASE+f"reprints/{bid}/{first['id']}"
        before = self.client.get(url).json
        self.assertEqual(len(before["snapshot"]["images"]), 2)
        md.write_text("审核后外部文件变更", encoding="utf-8")
        self.assertEqual(self.client.get(url).json, before)
        result = self.approve(bid, [second | {"sha":"过期版本"}, first])
        self.assertIn("error", result[0])
        self.assertEqual(result[1]["state"], "approved")
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _:self.approve(bid,[first,second]), range(2)))
        data = self.batch(bid)
        self.assertEqual(len(data["deliveries"]), 4)
        self.assertEqual(data["reviews"][2]["state"], "pending")
        self.assertEqual([r["assignments"][0]["account"] for r in data["reviews"][:2]], ["模拟A","模拟B"])
        for assn in data["reviews"][0]["assignments"]:
            j = self.client.get(BASE+"jobs/"+str(assn["id"])).json["delivery"]
            self.assertEqual((j["sha"], j["snapshot"]), (first["sha"], before["snapshot"]))
            pack = self.client.get(BASE+f"editorial/{assn['draft']}/export")
            with zipfile.ZipFile(io.BytesIO(pack.data)) as zipf:
                html = zipf.read("article.html").decode()
                self.assertEqual(html.count('src="img/0.png"'), 2)
                self.assertTrue(zipf.read("article.md").decode().rstrip().endswith(f"]({DESTINATION})"))
                self.assertIn("保留原句", html)
                self.assertNotIn("审核后外部文件变更", html)
                self.assertTrue(zipf.read("img/0.png"))
        other = self.create([dirs[1]], "duplicate")
        self.prepare()
        self.assertEqual(self.approve(other,self.batch(other)["reviews"])[0]["state"], "duplicate")
        dup = self.batch(other)
        self.assertEqual(dup["expected"], 0)
        self.assertEqual({r["id"] for r in dup["reviews"][0]["assignments"]},
                         {r["id"] for r in data["reviews"][1]["assignments"]})
        self.client.post(BASE+f"batches/{bid}/pause", json={})
        self.assertIn("error", self.approve(bid,[third])[0])
        self.client.post(BASE+f"batches/{bid}/resume", json={})
        self.approve(bid,[third])
        self.assertEqual(self.batch(bid)["reviews"][2]["assignments"][0]["account"], "模拟A")

    def test_prepare_recovery_pause_and_independent_retry(self):
        bid=self.create([self.source(1),"测试号/文章-2","../outside"])
        now=int(time.time())
        stale=claim(self.path,now,preparation=True)
        row=self.batch(bid)["reviews"][0]
        self.assertEqual(row["progress"],"reading")
        self.assertEqual(self.client.get(BASE+f"reprints/{bid}/{row['id']}").status_code,409)
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.client=create_app(self.path,self.root).test_client()
        self.assertIsNone(claim(self.path,now+301,preparation=True))
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        replacement=claim(self.path,now+302,preparation=True)
        execute(self.path,stale,now+303)
        self.assertEqual(self.batch(bid)["reviews"][0]["progress"],"reading")
        execute(self.path,replacement,now+304)
        self.prepare()
        first,missing,invalid=self.batch(bid)["reviews"]
        self.assertEqual([r["state"] for r in (first,missing,invalid)],["pending","invalid","invalid"])
        self.source(2)
        self.client.post(BASE+f"jobs/{missing['prepare_job']}/retry",json={})
        self.prepare()
        current=self.batch(bid)["reviews"]
        self.assertEqual(current[0],first)
        self.assertEqual(current[1]["state"],"pending")
        self.assertEqual(current[2],invalid)

    def test_reread_requires_review_again_even_when_bytes_match(self):
        bid=self.create([self.source(3)])
        self.prepare()
        first=self.batch(bid)["reviews"][0]
        url=BASE+f"reprints/{bid}/{first['id']}/reread"
        seen=dict(sha=first["sha"],revision=first["revision"])
        self.assertEqual(self.client.post(url,json=seen).status_code,200)
        self.assertEqual(self.client.post(url,json=seen).status_code,409)
        self.prepare()
        latest=self.batch(bid)["reviews"][0]
        self.assertEqual(latest["sha"],first["sha"])
        self.assertGreater(latest["revision"],first["revision"])
        self.assertIn("error",self.approve(bid,[first])[0])
        self.assertEqual(self.approve(bid,[latest])[0]["state"],"approved")
        self.assertEqual(self.client.post(url,json=dict(sha=latest["sha"],revision=latest["revision"])).status_code,409)

    def test_failed_queue_retries_only_selected_attempts(self):
        a=self.create(["资料甲",self.source(1)])
        b=self.create(["资料乙"],"batch-b")
        self.prepare()
        first=self.batch(a)["reviews"][1]
        self.approve(a,[first])
        scheduled=self.batch(a)["deliveries"]
        failed=self.client.get(BASE+"failed-jobs?stage=reprint_prepare").json
        self.assertEqual(failed["total"],2)
        items={row["batch"]:dict(id=row["id"],batch=row["batch"],expected_attempt=row["attempts"])
               for row in failed["items"]}
        scoped=self.client.get(BASE+"failed-jobs?batch="+a).json
        self.assertEqual([r["directory"] for r in scoped["items"]],["资料甲"])
        failure_day=datetime.fromtimestamp(scoped["items"][0]["failed_at"],BEIJING).date().isoformat()
        self.assertEqual(self.client.get(BASE+"failed-jobs?day="+failure_day+"&batch="+a).json["total"],1)
        endpoint=BASE+"failed-jobs/retry"
        results=self.client.post(endpoint,json=dict(items=[items[a],items[b]|{"batch":a}])).json["items"]
        self.assertEqual([r["queued"] for r in results],[True,False])
        self.prepare()
        self.assertFalse(self.client.post(endpoint,json=dict(items=[items[a]])).json["items"][0]["queued"])
        self.client.post(BASE+f"batches/{b}/pause",json={})
        self.assertTrue(self.client.post(endpoint,json=dict(items=[items[b]])).json["items"][0]["queued"])
        self.prepare()
        self.assertEqual(self.batch(b)["reviews"][0]["prepare_state"],"ready")
        self.assertEqual(self.batch(a)["deliveries"],scheduled)

    def test_100_articles_200_independent_receipts(self):
        bid = self.create([self.source(i) for i in range(100)])
        self.prepare()
        rows = self.batch(bid)["reviews"]
        result = self.approve(bid,rows)
        self.assertEqual(Counter(x.get("state") for x in result), {"approved":100})
        data = self.batch(bid)
        self.assertEqual(data["counts"], {"processing":100})
        jobs = data["deliveries"]
        self.assertEqual(len(jobs), 200)
        for platform in ("zhihu","toutiao"):
            assignments = [j for j in jobs if j["platform"]==platform]
            self.assertEqual(Counter(j["account"] for j in assignments), {"模拟A":50,"模拟B":50})
            slots = [j["slot"] for j in assignments]
            self.assertTrue(all(b-a>=30 for a,b in zip(slots,slots[1:])))
            days = [datetime.fromtimestamp(s,BEIJING) for s in slots]
            self.assertTrue(all(9<=d.hour<18 for d in days))
            self.assertLessEqual(max(Counter(d.date() for d in days).values()), 3)
            self.assertLessEqual(max(Counter((d.date(),j["account"]) for d,j in zip(days,assignments)).values()), 2)
        # Reopen persistent state in a fresh application before draining the due jobs.
        self.client = create_app(self.path,self.root).test_client()
        self.assertEqual(self.batch(bid)["deliveries"], jobs)
        drain(self.path,min(j["slot"] for j in jobs))
        final = self.batch(bid)
        self.assertEqual(final["counts"], {"done":100})
        self.assertEqual(final["simulated"], 200)
        self.assertEqual(len({j["receipt"] for j in final["deliveries"]}), 200)

