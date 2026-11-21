"""Offline acceptance suite. Uses only generated exports, simulated clocks and receipts."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .api import batch_view, control_batch, create_app, job_action
from .engine import SimulatedFailure, claim, execute, tick
from .store import BEIJING, initialize, ledger_path, transaction

T = 1704067200
TARGETS=[{"platform":p,"account":"demo"} for p in ("zhihu","toutiao","xiaohongshu")]
def options(n,key="one",prefix="fixture",targets=None):
    return {"request_id":key,"urls":[f"https://mp.weixin.qq.com/s/{prefix}-{i}" for i in range(n)],
            "targets":targets or TARGETS,"rules":{"gap":30,"account_cap":10,"platform_cap":20}}

def drain(path, now, fault=None):
    # Move the test clock only when every currently eligible step has been serviced.
    for _ in range(20000):
        if tick(path,now,fault=fault):
            continue
        with transaction(path,False) as db:
            next_due=db.execute("""SELECT min(j.due) FROM jobs j JOIN rows r ON r.id=j.row_id
                                JOIN batches b ON b.id=r.batch WHERE state='ready' AND b.control='active'""").fetchone()[0]
        if next_due is None:
            return now
        now=max(now,next_due)
    raise AssertionError("工作流未收敛")

def export_folder(root,i):
    p=root/f"测试号/文章-{i}"
    (p/"img").mkdir(parents=True)
    url=f"https://mp.weixin.qq.com/s/test-export-{i}"
    (p/"正文.md").write_text(f"样本第{i}篇\n====\n\n原创 作者:测试作者 公号:测试号 发布时间:2026-01-01 08:00\n\n原文地址：[原文]({url})\n\n第{i}篇的本地正文。\n\n![配图](img/0.png)",encoding="utf-8")
    (p/"正文.html").write_text('<p>本地正文</p><img src="img/0.png" data-src="https://example.invalid/image">',encoding="utf-8")
    import base64
    from .engine import IMAGE
    (p/"img/0.png").write_bytes(base64.b64decode(IMAGE.split(",")[1]))
    return p

class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/"library"
        self.root.mkdir()
        self.path=Path(self.tmp.name)/"db.sqlite"
        self.client=create_app(self.path,self.root).test_client()
    def tearDown(self):
        self.tmp.cleanup()
    def create(self,payload):
        r=self.client.post("/api/distribution/batches",json=payload)
        self.assertEqual(r.status_code,201,r.json)
        return r.json["id"]
    def view(self,bid):
        return self.client.get("/api/distribution/batches/"+bid).json
    def check(self,bid,n):
        b=self.view(bid)
        self.assertEqual(b["simulated"],n);self.assertEqual(b["public"],0)
        self.assertEqual(len({p["receipt"] for p in b["publishes"] if p["state"]=="done"}),n)
        with transaction(ledger_path(self.path),False) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone()[0],n)
        return b
    def run_all(self,fault=None):
        import time
        return drain(self.path,int(time.time()),fault)
    def test_20_success(self):
        bid=self.create(options(20));self.run_all();self.assertEqual(self.check(bid,60)["counts"]["done"],20)
    def test_100_lines_10_duplicates(self):
        d=options(90);d["urls"]+=d["urls"][:10]
        bid=self.create(d);self.assertEqual(self.create(d),bid);self.run_all()
        b=self.check(bid,270);self.assertEqual((b["rows_total"],b["unique"],b["counts"]["duplicate"]),(100,90,10))
    def test_100_local_exports(self):
        for i in range(100):export_folder(self.root,i)
        catalog=self.client.get("/api/distribution/library").json
        self.assertEqual(len(catalog["items"]),100)
        data=options(0);data["directories"]=[r["directory"] for r in catalog["items"]]
        res=self.client.post("/api/distribution/library",json=data);self.assertEqual(res.status_code,201,res.json)
        self.run_all();b=self.check(res.json["id"],300)
        self.assertTrue(all(r["known"] for r in self.client.get("/api/distribution/library").json["items"]))
        a=self.client.get("/api/distribution/articles/"+str(b["rows"][0]["id"])).json
        self.assertIn("本地正文",a["document"]["markdown_content"]);self.assertEqual(a["document"]["missing_images"],[])
        self.assertEqual(self.client.get(a["document"]["images"][0]).mimetype,"image/png")
        self.assertEqual(len(a["variants"]),3)
    def test_stage_isolation(self):
        d=options(20);bid=self.create(d)
        def fault(j):
            if j["stage"]=="ingest" and j["row_id"]<=5:raise SimulatedFailure("模拟采集失败")
        self.run_all(fault);b=self.check(bid,45);self.assertEqual(len(b["failures"]),5)
        for j in b["failures"]:
            job_action(self.path,j["id"],"retry",{},T)
        self.run_all();self.check(bid,60)
        with transaction(self.path,False) as db:
            self.assertEqual(db.execute("SELECT sum(attempts) FROM jobs WHERE stage='ingest'").fetchone()[0],25)
    def test_adapt_isolation(self):
        bid=self.create(options(20))
        def fault(j):
            if j["stage"]=="adapt" and j["platform"]=="xiaohongshu" and j["row_id"]<=4:
                raise SimulatedFailure("单平台模拟失败")
        self.run_all(fault);b=self.check(bid,56);self.assertEqual(len(b["failures"]),4)
        for j in b["failures"]:job_action(self.path,j["id"],"retry",{},T)
        self.run_all();self.check(bid,60)
    def test_publish_isolation(self):
        bid=self.create(options(1,targets=[{"platform":p,"account":"a"} for p in ("zhihu","toutiao","baijiahao")]))
        def fault(j):
            if j["stage"]=="publish" and j["platform"]=="zhihu":raise SimulatedFailure("模拟拒绝")
        self.run_all(fault);b=self.check(bid,2);self.assertEqual(b["counts"]["partial"],1)
        job_action(self.path,b["failures"][0]["id"],"retry",{},T)
        self.run_all();b=self.check(bid,3);self.assertEqual(sum(j["attempts"] for j in b["publishes"]),4)
    def test_process_crash_after_receipt(self):
        import time
        now=int(time.time());bid=self.create(options(1,targets=TARGETS[:1]))
        while not self.view(bid)["publishes"]:self.assertTrue(tick(self.path,now))
        snippet="from distribution.engine import claim,execute;import os,sys;p=sys.argv[1];n=int(sys.argv[2]);execute(p,claim(p,n),n,after_send=lambda:os._exit(37))"
        run=subprocess.run([sys.executable,"-c",snippet,str(self.path),str(now)],cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(run.returncode,37)
        drain(self.path,now+31);b=self.check(bid,1);self.assertEqual(b["publishes"][0]["attempts"],2)
    def test_overlapping_imports(self):
        from .store import import_batch
        x=options(1);y=options(1,key="two");y["urls"][0]+="?scene=2"
        with ThreadPoolExecutor(max_workers=2) as pool:
            bids=list(pool.map(lambda p:import_batch(self.path,p,T),[x,y]))
        drain(self.path,T);b=[self.view(k) for k in bids]
        self.assertEqual(sum(a["unique"] for a in b),1);self.assertEqual(sum(a["simulated"] for a in b),3)
    def test_pause_resume_cancel(self):
        from .store import import_batch
        bid=import_batch(self.path,options(3),T);taken=claim(self.path,T)
        control_batch(self.path,bid,"pause",T);execute(self.path,taken,T)
        self.assertFalse(tick(self.path,T));control_batch(self.path,bid,"resume",T)
        while not self.view(bid)["simulated"]:self.assertTrue(tick(self.path,T))
        control_batch(self.path,bid,"cancel",T);drain(self.path,T+86400)
        b=self.check(bid,1);self.assertEqual(b["control"],"cancelled")
    def test_rate_and_retry(self):
        from .store import import_batch
        import datetime
        d=options(5,targets=[{"platform":"zhihu","account":a} for a in ("a","b")])
        d["rules"].update(account_cap=2,platform_cap=3,weekdays_only=True,holidays=["2024-01-02"])
        bid=import_batch(self.path,d,T)
        def fault(j):
            if j["stage"]=="publish" and j["attempts"]==1:raise TimeoutError("429")
        drain(self.path,T,fault);b=self.check(bid,10);seen={}
        old=0
        for j in sorted(b["publishes"],key=lambda j:j["finished"]):
            when=j["finished"];day=datetime.datetime.fromtimestamp(when,BEIJING).date()
            self.assertGreaterEqual(when-old,30);old=when;self.assertNotEqual(day.isoformat(),"2024-01-02")
            by=seen.setdefault(day,[]);by.append(j["account"]);self.assertLessEqual(len(by),3);self.assertLessEqual(by.count(j["account"]),2)
    def test_bad_lines_and_folder_independence(self):
        d=options(1);d["urls"]=["","https://example.invalid","  "]+d["urls"]
        bid=self.create(d);self.run_all();b=self.check(bid,3);self.assertEqual(b["counts"]["invalid"],1)
        export_folder(self.root,1);bad=export_folder(self.root,2);(bad/"正文.md").write_bytes(b"")
        r=self.client.post("/api/distribution/library",json=options(0,key="folders")|{"directories":["测试号/文章-1","测试号/文章-2"]})
        self.assertEqual(r.status_code,201);self.run_all();b=self.view(r.json["id"])
        self.assertEqual((b["simulated"],b["counts"]["invalid"]),(3,1))
    def test_image_with_markdown_title_and_other_extension(self):
        p=export_folder(self.root,1)
        (p/"img/0.png").rename(p/"img/0.other")
        md=p/"正文.md"
        md.write_text(md.read_text(encoding="utf-8").replace("img/0.png)",'img/0.other "配图标题")'),encoding="utf-8")
        (p/"正文.html").write_text('<img src="img/0.other" data-src="https://example.invalid/img">',encoding="utf-8")
        response=self.client.post("/api/distribution/library",json=options(0)|{"directories":["测试号/文章-1"]})
        self.run_all();b=self.check(response.json["id"],3)
        doc=self.client.get("/api/distribution/articles/"+str(b["rows"][0]["id"])).json["document"]
        self.assertEqual(len(doc["images"]),1);self.assertEqual(doc["missing_images"],[])
        self.assertEqual(self.client.get(doc["images"][0]).mimetype,"image/png")

if __name__=="__main__":
    unittest.main()
