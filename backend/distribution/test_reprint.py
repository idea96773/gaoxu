"""Batch reprint acceptance with generated folders and a private SQLite receiver."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
from pathlib import Path
from unittest.mock import patch
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

    def test_catalog_partial_coverage_and_fast_duplicate(self):
        path=self.source(31)
        self.targets=[dict(platform="zhihu",account="甲")]
        original=self.create([path],"zhihu-only")
        self.prepare()
        self.approve(original,self.batch(original)["reviews"])
        def catalog_item():
            return next(i for i in self.client.get(BASE+"library").json["items"] if i["directory"]==path)
        first=catalog_item()
        self.assertEqual(first["title"],"样本第31篇")
        self.assertEqual(set(first["deliveries"]),{"zhihu"})
        old=first["deliveries"]["zhihu"]["id"]
        self.targets.append(dict(platform="toutiao",account="乙"))
        partial=self.create([path],"fill-toutiao")
        self.prepare()
        row=self.batch(partial)["reviews"][0]
        self.assertEqual(row["missing_platforms"],["toutiao"])
        self.assertEqual([j["id"] for j in row["prior_jobs"]],[old])
        self.approve(partial,[row])
        assigned=self.batch(partial)["reviews"][0]["assignments"]
        self.assertEqual({j["platform"]:j["reused"] for j in assigned},{"zhihu":1,"toutiao":0})
        duplicate=self.create([path],"already-covered")
        with patch("distribution.reprint.read_folder",side_effect=AssertionError("重复稿不应读取正文配图")):
            self.prepare()
        skipped=self.batch(duplicate)
        self.assertEqual(skipped["counts"],{"duplicate":1})
        self.assertEqual(skipped["expected"],0)
        self.assertEqual(skipped["reviews"][0]["sha"],"")
        self.assertEqual({j["id"] for j in skipped["reviews"][0]["assignments"]},{j["id"] for j in assigned})
        self.client.post(BASE+f"batches/{original}/cancel",json={})
        self.assertEqual(set(catalog_item()["deliveries"]),{"toutiao"})

    def test_approval_detects_cancelled_prior_job(self):
        directory=self.source(32)
        self.targets=[dict(platform="zhihu",account="甲")]
        first=self.create([directory],"first")
        self.prepare()
        self.approve(first,self.batch(first)["reviews"])
        self.targets.append(dict(platform="toutiao",account="乙"))
        second=self.create([directory],"second")
        self.prepare()
        row=self.batch(second)["reviews"][0]
        expected={j["platform"]:j["id"] for j in row["prior_jobs"]}
        self.client.post(BASE+f"batches/{first}/cancel",json={})
        result=self.client.post(BASE+f"reprints/{second}/approve",json=dict(approved=True,items=[
            dict(id=row["id"],sha=row["sha"],revision=row["revision"],expected_prior=expected)]))
        self.assertIn("error",result.json["items"][0])
        self.assertEqual(self.batch(second)["deliveries"],[])
        self.assertEqual(set(self.batch(second)["reviews"][0]["missing_platforms"]),{"toutiao","zhihu"})

    def test_new_prior_after_preview_uses_actual_frozen_footer_and_isolates_approval(self):
        directory,other=self.source(41),self.source(42)
        self.targets=[dict(platform="zhihu",account="甲")]
        first=self.create([directory],"earlier")
        self.targets.append(dict(platform="toutiao",account="乙"))
        response=self.client.post(BASE+"reprints",json=dict(request_id="later",directories=[directory,other],
            targets=self.targets,links={"zhihu":{"mode":"omit"}}))
        second=response.json["id"]
        self.prepare()
        row,independent=self.batch(second)["reviews"]
        url=BASE+f"reprints/{second}/{row['id']}?platform=zhihu"
        self.assertEqual(self.client.get(url).json["snapshot"]["link"]["mode"],"omit")
        self.approve(first,self.batch(first)["reviews"])
        def confirm(items):
            return self.client.post(BASE+f"reprints/{second}/approve",json=dict(approved=True,items=items)).json["items"]
        def item(r,prior):
            return dict(id=r["id"],sha=r["sha"],revision=r["revision"],expected_prior=prior)
        conflict,ok=confirm([item(row,{}),item(independent,{})])
        self.assertIn("error",conflict)
        self.assertEqual(ok["state"],"approved")
        self.assertIn("error",self.approve(second,[row])[0])
        original=self.batch(first)["reviews"][0]["assignments"][0]["id"]
        before=self.client.get(url).json
        self.assertEqual((before["job"],before["account"],before["reused"]),(original,"甲",True))
        self.assertEqual(before["snapshot"]["link"]["destination"],DESTINATION)
        self.assertEqual(confirm([item(row,{"zhihu":original})])[0]["state"],"approved")
        self.assertEqual(self.client.get(url).json,before)
        toutiao=self.client.get(url.replace("=zhihu","=toutiao")).json
        self.assertFalse(toutiao["reused"])
        self.assertEqual(toutiao["account"],"乙")
        self.assertEqual(toutiao["edition_sha"],self.client.get(BASE+"jobs/"+str(toutiao["job"])).json["delivery"]["sha"])

    def test_duplicate_directories_count_input_rows_but_share_work(self):
        dirs=[self.source(i) for i in range(90)]
        bid=self.create(dirs+dirs[:10])
        before=self.batch(bid)
        self.assertEqual(before["rows_total"],100)
        self.assertEqual(before["counts"],{"preparing":90,"duplicate":10})
        self.assertEqual(sum(bool(r["prepare_job"]) for r in before["reviews"]),90)
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"][:1])
        batch=self.batch(bid)
        shared=batch["reviews"][90]["assignments"]
        self.assertEqual(len(shared),2)
        self.assertTrue(all(job["reused"] for job in shared))
        self.assertEqual(batch["expected"],2)

    def test_zhihu_draft_recipient_follows_frozen_identity(self):
        # All identities and heartbeats are fixtures in this test's temporary database.
        from .zhihu_draft_worker import heartbeat
        self.targets=[dict(platform="zhihu",account="科普")]
        accounts_url=BASE+"zhihu-draft-accounts"
        def bind(uid,revision,alias="科普"):
            return self.client.post(accounts_url,json=dict(action="bind",alias=alias,revision=revision,user_id=uid))
        def freeze(index):
            bid=self.create([self.source(index)],f"account-{index}")
            self.prepare()
            self.approve(bid,self.batch(bid)["reviews"])
            row=self.batch(bid)["reviews"][0]
            return BASE+f"jobs/{row['assignments'][0]['id']}/platform-draft",row["sha"]
        def grant(url,sha,uid):
            info=self.client.get(url).json
            return self.client.post(url,json=dict(action="scope",scope="zhihu_draft",rights_confirmed=True,
                sha=sha,user_id=uid,scope_revision=info["rights"]["revision"]))
        def queue(url,sha,uid):
            revision=self.client.get(url).json["rights"]["revision"]
            return self.client.post(url,json=dict(action="queue",consent=True,sha=sha,user_id=uid,scope_revision=revision))

        early,early_sha=freeze(1)
        heartbeat(self.path,"fixture-a","相同昵称")
        self.assertEqual(queue(early,early_sha,"fixture-a").status_code,400)
        self.assertEqual(bind("fixture-a",0).status_code,200)
        self.assertEqual(bind("fixture-a",0,"其他别名").status_code,400)
        self.assertEqual(self.client.get(early).json["rights"]["scope"],"local")
        self.assertEqual(queue(early,early_sha,"fixture-a").status_code,400)
        self.assertEqual(grant(early,early_sha,"fixture-a").status_code,200)
        self.assertEqual(queue(early,early_sha,"fixture-a").status_code,200)
        frozen,sha=freeze(2)
        self.assertEqual(self.client.get(frozen).json["recipient"]["user_id"],"fixture-a")
        heartbeat(self.path,"fixture-b","相同昵称")
        self.assertEqual(bind("fixture-b",0).status_code,409)
        self.assertEqual(bind("fixture-b",1).status_code,200)
        self.assertEqual(queue(frozen,sha,"fixture-b").status_code,400)
        self.assertEqual(self.client.get(early).json["record"]["user_id"],"fixture-a")
        later,later_sha=freeze(3)
        self.assertEqual(self.client.get(later).json["recipient"]["user_id"],"fixture-b")
        self.assertEqual(grant(later,later_sha,"fixture-b").status_code,200)
        self.assertEqual(queue(later,later_sha,"fixture-b").status_code,200)
        heartbeat(self.path,"fixture-a","改名后的昵称")
        self.assertEqual(grant(frozen,sha,"fixture-a").status_code,200)
        self.assertEqual(queue(frozen,sha,"fixture-a").status_code,200)
        self.assertEqual(self.client.get(frozen).json["record"]["user_id"],"fixture-a")

    def test_withdraw_recipient_rights_before_send(self):
        from . import platform_draft
        from .zhihu_draft_worker import heartbeat
        self.targets=[dict(platform="zhihu",account="试稿")]
        bid=self.create([self.source(1)])
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        self.approve(bid,[row])
        a=self.batch(bid)["reviews"][0]["assignments"][0]
        url=BASE+f"jobs/{a['id']}/platform-draft"
        uid="fixture-only"
        heartbeat(self.path,uid,"测试账号")
        self.client.post(BASE+"zhihu-draft-accounts",json=dict(action="bind",alias="试稿",revision=0,user_id=uid))
        initial=self.client.get(url).json["rights"]["revision"]
        decision=dict(action="scope",sha=row["sha"],user_id=uid,scope_revision=initial,scope="zhihu_draft",rights_confirmed=True)
        self.assertEqual(self.client.post(url,json=decision).status_code,200)
        info=self.client.get(url).json
        self.assertIsNone(info["record"])
        granted=info["rights"]["revision"]
        queued=dict(action="queue",consent=True,user_id=uid,scope_revision=granted,sha=row["sha"])
        self.assertEqual(self.client.post(url,json=queued).status_code,200)
        task=platform_draft.claim(self.path,uid)
        self.assertEqual(task["job"],a["id"])
        self.assertEqual(self.client.post(url,json=decision|dict(scope_revision=granted,scope="local")).status_code,200)
        info=self.client.get(url).json
        self.assertEqual((info["record"]["state"],info["rights"]["scope"]),("cancelled","local"))
        self.assertEqual(self.client.post(url,json=decision).status_code,409)
        self.assertEqual(self.client.post(url,json=queued).status_code,400)
        with self.assertRaises(ValueError):
            platform_draft.payload(self.path,task)
        with self.assertRaises(ValueError):
            platform_draft.transition(self.path,task,"preparing","sending")
        export=self.client.get(BASE+f"editorial/{a['draft']}/export")
        with zipfile.ZipFile(io.BytesIO(export.data)) as pack:
            meta=json.loads(pack.read("metadata.json"))
        self.assertEqual((meta["usage_rights"]["scope"],meta["usage_rights"]["sha"]),("local",row["sha"]))

    def test_platform_footer_review_and_export(self):
        short_url="https://example.invalid/read?ref=case&via=console"
        links={"zhihu":{"mode":"short","short":short_url,"verified":True},"toutiao":{"mode":"omit"}}
        data=dict(request_id="links",directories=[self.source(9)],targets=self.targets,destination=DESTINATION,links=links)
        rejected=data|{"links":{"zhihu":links["zhihu"]|{"verified":False}}}
        self.assertEqual(self.client.post(BASE+"reprints",json=rejected).status_code,400)
        bid=self.client.post(BASE+"reprints",json=data).json["id"]
        self.assertEqual(self.client.post(BASE+"reprints",json=data).json["id"],bid)
        self.assertEqual(self.client.post(BASE+"reprints",json=data|{"links":{}}).status_code,400)
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        url=BASE+f"reprints/{bid}/{row['id']}"
        self.assertEqual(self.client.get(url).status_code,400)
        self.assertEqual(self.client.get(url+"?platform=weibo").status_code,400)
        previews={p:self.client.get(url,query_string=dict(platform=p)).json for p in links}
        for preview in previews.values():
            self.assertEqual(preview["sha"],row["sha"])
        z,t=(previews[p]["snapshot"] for p in ("zhihu","toutiao"))
        self.assertTrue(z["markdown"].rstrip().endswith("]("+short_url+")"))
        self.assertIn("case&amp;via=console",z["html"])
        self.assertEqual(z["link"]["destination"],DESTINATION)
        self.assertNotIn("延伸阅读",t["markdown"])
        self.assertEqual(z["source_url"],t["source_url"])
        self.assertEqual(z["images"],t["images"])
        self.assertEqual(self.approve(bid,[row])[0]["state"],"approved")
        for assignment in self.batch(bid)["reviews"][0]["assignments"]:
            preview=previews[assignment["platform"]]
            job=self.client.get(BASE+f"jobs/{assignment['id']}").json["delivery"]
            self.assertEqual((job["sha"],job["snapshot"]),(preview["edition_sha"],preview["snapshot"]))
            archive=self.client.get(BASE+f"editorial/{assignment['draft']}/export")
            with zipfile.ZipFile(io.BytesIO(archive.data)) as pack:
                self.assertEqual(pack.read("article.md").decode(),preview["snapshot"]["markdown"])
                self.assertEqual(json.loads(pack.read("metadata.json"))["link"],preview["snapshot"]["link"])

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

    def test_selected_source_survives_restart_and_folder_replacement(self):
        dirs=[self.source(i) for i in (51,52)]
        items=self.client.get(BASE+"library").json["items"]
        marks={r["directory"]:dict(title=r["title"],key=r["selection_key"]) for r in items}
        params=dict(directories=dirs,selections=marks,request_id="picked",targets=self.targets)
        bid=self.client.post(BASE+"reprints",json=params).json["id"]
        md=self.root/dirs[0]/"正文.md"
        original=md.read_text(encoding="utf-8")
        md.write_text(original.replace("test-export-51","test-export-52"),encoding="utf-8")
        self.client=create_app(self.path,self.root).test_client()
        self.assertEqual(self.client.post(BASE+"reprints",json=params).json["id"],bid)
        self.prepare()
        rejected,peer=self.batch(bid)["reviews"]
        self.assertEqual((rejected["state"],peer["state"]),("invalid","pending"))
        self.assertEqual(rejected["assignments"],[])
        md.write_text(original+"\n更新配图说明",encoding="utf-8")
        self.client.post(BASE+f"jobs/{rejected['prepare_job']}/retry",json={})
        self.prepare()
        restored,unchanged=self.batch(bid)["reviews"]
        self.assertEqual(restored["state"],"pending")
        self.assertEqual(unchanged,peer)

    def test_catalog_file_selection_checks_full_markdown_beyond_header(self):
        directory=self.source(53)
        md=self.root/directory/"正文.md"
        md.write_text("# 同名\n"+"摘要\n"*3600+"\n原文地址：https://mp.weixin.qq.com/s/first\n正文",encoding="utf-8")
        mark=self.client.get(BASE+"library").json["items"][0]
        self.assertTrue(mark["selection_key"].startswith("file-"))
        bid=self.client.post(BASE+"reprints",json=dict(request_id="late-link",directories=[directory],
            selections={directory:dict(title=mark["title"],key=mark["selection_key"])},targets=self.targets)).json["id"]
        from .library import read_folder
        def replaced(*args):
            md.write_text(md.read_text(encoding="utf-8").replace("/s/first","/s/second"),encoding="utf-8")
            return read_folder(*args)
        with patch("distribution.reprint.read_folder",side_effect=replaced):
            self.prepare()
        self.assertEqual(self.batch(bid)["reviews"][0]["state"],"invalid")

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

