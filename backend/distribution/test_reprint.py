"""Batch reprint acceptance with generated folders and a private SQLite receiver."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
from pathlib import Path
from unittest.mock import Mock, patch
import tempfile
import time
import unittest
import zipfile

from .api import create_app
from .engine import claim, execute, tick
from .reprint import DESTINATION
from .store import BEIJING, transaction
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

    def test_overview_drills_down_to_active_rows_and_beijing_day_receipts(self):
        bid = self.create([self.source(1), self.source(2)])
        self.prepare()
        first, second = self.batch(bid)["reviews"]
        self.approve(bid, [first])

        def overview():
            return self.client.get(BASE+"batches?summary=1").json["overview"]

        def results(work, **filters):
            return self.client.get(BASE+"article-search", query_string=dict(work=work, **filters)).json

        self.assertEqual(overview()["pending_reprint"], 1)
        self.assertEqual([r["row_id"] for r in results("pending_reprint")["items"]], [second["id"]])
        self.assertEqual(overview()["queued_delivery"], 2)
        self.assertEqual(results("queued_delivery")["total"], 2)
        self.client.post(BASE+f"batches/{bid}/pause", json={})
        for scope in ("queued_delivery", "pending_reprint"):
            self.assertEqual(overview()[scope], 0)
            self.assertEqual(results(scope)["total"], 0)

        # 两个模拟回执分别在北京时间零点和零点前一秒落账。
        with patch("distribution.api.time.time", return_value=1800000000):
            today = overview()["today"]
            midnight = int(datetime.fromisoformat(today).replace(tzinfo=BEIJING).timestamp())
            with transaction(self.path) as db:
                jobs = db.execute("SELECT id FROM jobs WHERE row_id=? AND stage='deliver' ORDER BY id",
                                  (first["id"],)).fetchall()
                db.executemany("UPDATE jobs SET state='done', finished=?, receipt=? WHERE id=?",
                               [(midnight-i, "local-receipt-"+str(i), row["id"]) for i, row in enumerate(jobs)])
            self.assertEqual(overview()["delivered_today"], 1)
            self.assertEqual(results("confirmed_delivery", date_field="delivered", start=today, end=today)["total"], 1)

    def test_overview_keeps_an_older_selected_batch_accessible(self):
        bid = self.create([self.source(1)])
        with transaction(self.path) as db:
            db.executemany("""INSERT INTO batches(id,request_key,digest,created,control,targets,rules)
                SELECT ?,?,?,created+100,control,targets,rules FROM batches WHERE id=?""",
                [(f"recent-{i}", f"request-{i}", "fixture", bid) for i in range(200)])
        recent = self.client.get(BASE+"batches?summary=1").json["batches"]
        self.assertNotIn(bid, [b["id"] for b in recent])
        pinned = self.client.get(BASE+"batches", query_string={"summary":"1", "include":bid}).json["batches"]
        self.assertEqual(pinned[:-1], recent)
        self.assertEqual((pinned[-1]["id"], pinned[-1]["rows_total"]), (bid, 1))

    def test_library_switch_preserves_submitted_sources_and_rejects_stale_selection(self):
        from .library import library_settings
        old=self.root/"old"
        new=self.root/"new"
        rel=export_folder(old,1).relative_to(old).as_posix()
        replacement=export_folder(new,1)/"正文.md"
        replacement.write_text(replacement.read_text(encoding="utf-8").replace("test-export-1","different"),
                               encoding="utf-8")
        with patch.dict("os.environ",{"DIST_LIBRARY":""}):
            ui=create_app(self.path).test_client()
            set_path=BASE+"library-settings"
            self.assertFalse(self.client.get(set_path).json["editable"])
            self.assertEqual(ui.post(set_path,json=dict(root=str(old),revision=0)).json["revision"],1)
            item=ui.get(BASE+"library").json["items"][0]
            data=dict(directories=[rel],expected_root=str(old),request_id="old-root",targets=self.targets,
                      selections={rel:dict(title=item["title"],key=item["selection_key"])})
            response=ui.post(BASE+"reprints",json=data)
            self.assertEqual(response.status_code,201,response.json)
            bid=response.json["id"]
            self.assertEqual(ui.post(set_path,json=dict(root=str(new),revision=1)).json["revision"],2)
            self.assertEqual(ui.post(set_path,json=dict(root=str(old),revision=1)).status_code,400)
            self.assertEqual(ui.post(BASE+"reprints",json=data).json["id"],bid)
            stale=data|dict(request_id="other")
            for endpoint in ("reprints","library"):
                self.assertEqual(ui.post(BASE+endpoint,json=stale).status_code,400)
            self.assertEqual(ui.get(BASE+"library").json["items"][0]["source"],"https://mp.weixin.qq.com/s/different")
            self.assertEqual(library_settings(self.path)["root"],str(new))
            self.assertEqual(create_app(self.path).test_client().get(set_path).json["root"],str(new))
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        self.assertEqual(row["state"],"pending")
        with transaction(self.path,False) as db:
            self.assertEqual(db.execute("SELECT canonical FROM rows WHERE id=?",(row["id"],)).fetchone()[0],
                             "folder:https://mp.weixin.qq.com/s/test-export-1")

    def test_catalog_same_relative_directory_does_not_hide_a_different_unsourced_article(self):
        directory=self.source(7)
        md=self.root/directory/"正文.md"
        md.write_text("文章甲\n\n正文甲",encoding="utf-8")
        response=self.client.post(BASE+"library",json=dict(request_id="unsourced",directories=[directory],
                                                           targets=self.targets))
        self.assertEqual(response.status_code,201,response.json)
        self.assertTrue(self.client.get(BASE+"library").json["items"][0]["known"])
        other=self.root/"别的保存目录"
        export_folder(other,7).joinpath("正文.md").write_text("文章乙\n\n正文乙",encoding="utf-8")
        other_ui=create_app(self.path,other).test_client()
        self.assertFalse(other_ui.get(BASE+"library").json["items"][0]["known"])

    def test_review_snapshot_isolation_export_and_duplicate(self):
        dirs = [self.source(i) for i in range(3)]
        md = self.root / dirs[0] / "正文.md"
        md.write_text(md.read_text(encoding="utf-8")+"\n\n保留原句\n\n![再次配图](img/0.png)", encoding="utf-8")
        receipt=BASE+"reprint-request?request_id=batch"
        self.assertIsNone(self.client.get(receipt).json["id"])
        bid = self.create(dirs+["失效目录"])
        self.assertEqual(self.client.get(receipt).json["id"],bid)
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

    def test_frozen_reprint_checks_every_asset_before_platform_contact(self):
        from . import rights
        from .zhihu_draft_worker import prepare_content

        directory=self.source(71)
        folder=self.root/directory
        # GIF89a, 1×1 pixel.
        (folder/"img/last.gif").write_bytes(bytes.fromhex(
            "47494638396101000100800000000000ffffff2c00000000010001000002024401003b"))
        with (folder/"正文.md").open("a",encoding="utf-8") as stream:
            stream.write("\n\n![重复图片](img/0.png)\n\n![最后一张](img/last.gif)")
        bid=self.create([directory])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        assignments=self.batch(bid)["reviews"][0]["assignments"]
        self.assertEqual({a["platform"] for a in assignments},{"zhihu","toutiao"})
        for assignment in assignments:
            url=BASE+"jobs/"+str(assignment["id"])
            job=self.client.get(url).json
            report=self.client.get(url+"/reprint-check").json
            self.assertEqual((report["sha"],report["draft"],report["account"],report["planned"]),
                             (job["delivery"]["sha"],assignment["draft"],assignment["account"],job["job"]["slot"]))
            self.assertEqual((len(report["images"]),report["unique_images"],report["reader_url"]),(3,2,DESTINATION))
            self.assertTrue(report["html"].rstrip().endswith('href="'+DESTINATION+'">延伸阅读</a></p>'))
            if assignment["platform"]=="zhihu":
                task=dict(job=assignment["id"],sha=report["sha"],user_id="fixture-uid")
                last=report["images"][-1].rsplit("/",1)[1]

        with transaction(self.path) as db:
            rights.record(db,task["job"],task["sha"],"zhihu_draft",task["user_id"],int(time.time()))
            db.execute("UPDATE media SET data=? WHERE sha=?",(b"damaged",last))
        bridge=Mock()
        with self.assertRaisesRegex(ValueError,"第 3 处配图"):
            prepare_content(self.path,task,bridge)
        self.assertEqual(bridge.mock_calls,[])

        # A local failure ends just this task; the listener can use the same connection for the next one.
        from . import zhihu_draft_worker as worker
        with patch.object(worker,"transition") as update:
            worker.run_task(self.path,task,bridge)
            self.assertEqual(update.call_args.args[2:4],("preparing","failed"))
        self.assertEqual(bridge.mock_calls,[])

    def test_chunked_upload_checks_account_before_remote_transfer(self):
        from . import zhihu_draft_worker as worker
        bridge=worker.Bridge(None,"simulated")
        bridge.request=Mock(return_value={"success":True})
        transfer=Mock(side_effect=[None,worker.DraftDeferred("账号已切换")])
        with patch.object(worker,"CHUNK",4), self.assertRaises(worker.DraftDeferred):
            bridge.upload(b"abcdef","image/png",Mock(),transfer)
        self.assertEqual([call.args[0] for call in bridge.request.call_args_list],
                         ["uploadImage:start","uploadImage:chunk","uploadImage:chunk"])

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
        self.assertEqual(skipped["delivery_coverage"]["totals"],{"total":2,"reused":2,"ready":2})
        self.client.post(BASE+f"batches/{original}/pause",json={})
        drain(self.path,int(time.time()))
        result=self.batch(partial)
        self.assertEqual(result["counts"],{"processing":1})
        self.assertEqual(result["delivery_coverage"]["totals"],{"created":1,"reused":1,"total":2,"paused":1,"done":1})
        self.assertEqual(result["delivery_coverage"]["platforms"]["zhihu"]["paused"],1)
        self.client.post(BASE+f"batches/{original}/cancel",json={})
        self.assertEqual(self.batch(partial)["counts"],{"partial":1})
        self.assertEqual(self.batch(duplicate)["reviews"][0]["delivery_progress"],"partial")
        self.assertEqual(set(catalog_item()["deliveries"]),{"toutiao"})

    def test_linked_reference_images_remain_portable_and_code_examples_stay_text(self):
        from urllib.parse import quote
        directory=self.source(49)
        folder=self.root/directory
        filename="图 (1).png"
        (folder/"img/0.png").rename(folder/"img"/filename)
        name="img/"+filename
        href=quote(name)
        md=folder/"正文.md"
        body=md.read_text(encoding="utf-8").replace("![配图](img/0.png)",
            f'[![查看原图][pic]](<{name}>)\n\n![第二处][pic]\n\n[pic]: <{name}?v=1> "原文配图"'
            '\n\n`![只是示例](img/并不存在.png)`\n')
        md.write_text(body,encoding="utf-8")
        (folder/"正文.html").write_text(f'<img src="data:image/gif;base64,AA" data-src="{href}">',encoding="utf-8")
        bid=self.create([directory])
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        self.assertEqual(row["state"],"pending")
        url=BASE+f"reprints/{bid}/{row['id']}?platform=zhihu"
        view=self.client.get(url).json["snapshot"]
        ref=view["images"][0]
        self.assertEqual(view["images"],[ref,ref])
        self.assertEqual(view["files"],{name:ref})
        self.assertIn(f'<a href="{ref}"><img',view["html"])
        self.assertIn('img/并不存在.png',view["html"])
        self.approve(bid,[row])
        draft=self.batch(bid)["reviews"][0]["assignments"][0]["draft"]
        with zipfile.ZipFile(io.BytesIO(self.client.get(BASE+f"editorial/{draft}/export").data)) as archive:
            self.assertEqual(archive.namelist().count(name),1)
            markup=archive.read("article.html").decode()
            self.assertIn(f'<a href="{href}"><img',markup)
            self.assertEqual(markup.count(f'src="{href}"'),2)
            self.assertIn(DESTINATION,markup)
            self.assertEqual(archive.read("article.md").decode(),view["markdown"])

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
        self.assertEqual(before["approval"]["batch"],first)
        self.assertEqual(before["snapshot"]["link"]["destination"],DESTINATION)
        self.assertEqual(confirm([item(row,{"zhihu":original})])[0]["state"],"approved")
        self.assertEqual(self.client.get(url).json,before)
        toutiao=self.client.get(url.replace("=zhihu","=toutiao")).json
        self.assertFalse(toutiao["reused"])
        self.assertEqual((toutiao["approval"]["batch"],toutiao["approval"]["review_sha"]),(second,row["sha"]))
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
        self.assertEqual(batch["delivery_coverage"]["totals"],{"total":2,"created":2,"ready":2})
        self.assertEqual(len(shared),2)
        self.assertTrue(all(job["reused"] for job in shared))
        self.assertEqual(batch["expected"],2)

    def test_probe_mode_distinguishes_login_and_blocks_draft_queue(self):
        from .draft_bridge import heartbeat, listener, status
        from .store import transaction
        bid=self.create([self.source(64)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        job=next(j for j in self.batch(bid)["deliveries"] if j["platform"]=="zhihu")
        url=BASE+f"jobs/{job['id']}/platform-draft"
        accounts=BASE+"zhihu-draft-accounts"
        with listener(self.path,9541,True,"current"):
            heartbeat(self.path,"ignored","旧账号",session="earlier")
            self.assertEqual(self.client.get(accounts).json["bridge"]["phase"],"listening")
            heartbeat(self.path,"","",session="current")
            info=self.client.get(accounts).json
            self.assertTrue(info["connected"])
            self.assertIsNone(info["account"])
            heartbeat(self.path,"fixture","测试账号",session="current")
            info=self.client.get(url).json
            self.assertEqual(info["account"]["user_id"],"fixture")
            self.assertTrue(info["bridge"]["check_only"])
            rejected=self.client.post(url,json=dict(action="queue",sha=job["sha"],user_id="fixture",consent=True))
            self.assertIn("仅核对连接",rejected.json["error"])
            self.assertIsNone(self.client.get(url).json["record"])
            with transaction(self.path) as db:
                db.execute("UPDATE zhihu_bridge_runtime SET seen=0")
                self.assertFalse(status(db)["connected"])
        self.assertEqual(self.client.get(accounts).json["bridge"]["phase"],"offline")

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
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.assertFalse(platform_draft.transition(self.path,task,"preparing","sending"))
        self.assertIsNone(platform_draft.claim(self.path,uid))
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        self.assertEqual(platform_draft.claim(self.path,uid)["id"],task["id"])
        with transaction(self.path) as db:
            db.execute("UPDATE reprint_reviews SET sha='damaged' WHERE row_id=?",(row["id"],))
        self.assertEqual(self.client.post(url,json=decision|dict(scope_revision=granted,scope="local")).status_code,200)
        with transaction(self.path) as db:
            db.execute("UPDATE reprint_reviews SET sha=? WHERE row_id=?",(row["sha"],row["id"]))
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

    def test_platform_draft_inspection_tracks_cases_without_resending(self):
        from .store import transaction
        self.targets=[dict(platform="zhihu",account="模拟")]
        bid=self.create([self.source(61),self.source(62)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        one,two=self.batch(bid)["deliveries"]
        with transaction(self.path) as db:
            for entry in (one,two):
                db.execute("""INSERT INTO platform_drafts(job,canonical,sha,user_id,name,state,updated)
                    SELECT ?,canonical,?,'test-account','模拟','unknown',0 FROM rows WHERE id=?""",
                    (entry["id"],entry["sha"],entry["row_id"]))
        url=BASE+f"jobs/{one['id']}/platform-draft"
        checks=dict(title="pass",layout="pass",images="issue",footer="pending")
        initial=dict(action="inspect",revision=0,expected_state="unknown",sha=one["sha"],user_id="test-account",
                     url="https://zhuanlan.zhihu.com/p/62001/edit",checks=checks)
        self.assertEqual(self.client.post(url,json=initial|{"user_id":"different"}).status_code,400)
        for _ in range(2):
            self.assertEqual(self.client.post(url,json=initial).status_code,200)
        info=self.client.get(url).json
        self.assertEqual(info["record"]["state"],"needs_review")
        self.assertEqual(len(info["inspections"]),1)
        self.assertEqual(info["inspections"][0]["checks"],checks)
        self.assertEqual(self.client.get(BASE+"article-search?draft_review=issue").json["items"][0]["job_id"],one["id"])
        passed=initial|{"checks":dict.fromkeys(checks,"pass")}
        self.assertEqual(self.client.post(url,json=passed).status_code,400)
        guarded=passed|dict(revision=info["inspections"][0]["id"],expected_state="needs_review")
        self.assertEqual(self.client.post(url,json=guarded).status_code,200)
        self.assertEqual(self.client.get(url).json["record"]["state"],"verified")
        self.assertEqual(self.client.post(BASE+f"jobs/{two['id']}/platform-draft",
                                          json=initial|{"sha":two["sha"]}).status_code,400)
        self.assertEqual(self.client.get(BASE+"article-search?draft_review=issue").json["total"],0)

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

    def test_returned_review_is_isolated_and_requires_fresh_approval_after_reopen(self):
        bid=self.create([self.source(61),self.source(62)])
        self.prepare()
        first,other=self.batch(bid)["reviews"]
        url=BASE+f"reprints/{bid}/{first['id']}"
        guard=dict(sha=first["sha"],revision=first["revision"])
        reason="文末链接需要核对"
        self.assertEqual(self.client.post(url+"/return",json=guard|dict(reason=" ")).status_code,400)
        for _ in range(2):
            self.assertEqual(self.client.post(url+"/return",json=guard|dict(reason=reason)).status_code,200)
        blocked=self.batch(bid)
        self.assertEqual(blocked["counts"],dict(pending=1,returned=1))
        self.assertEqual(self.client.get(BASE+"batches?summary=1").json["overview"]["returned_reprint"],1)
        self.assertEqual(len(blocked["reviews"][0]["returns"]),1)
        self.assertEqual(blocked["reviews"][0]["returns"][0]["reason"],reason)
        self.assertEqual(self.client.get(url+"?platform=zhihu").json["sha"],first["sha"])
        self.assertEqual(self.client.post(url+"/reread",json=guard).status_code,409)
        self.assertEqual(self.client.get(BASE+"article-search?work=returned_reprint").json["items"][0]["row_id"],first["id"])
        self.assertIn("error",self.approve(bid,[first])[0])
        self.approve(bid,[other])
        self.assertEqual(len(self.batch(bid)["deliveries"]),2)
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.assertEqual(self.client.post(url+"/reopen",json=guard).status_code,409)
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        self.assertEqual(self.client.post(url+"/reopen",json=guard).status_code,200)
        reopened=self.batch(bid)["reviews"][0]
        self.assertEqual((reopened["state"],reopened["sha"],reopened["revision"]),("pending",first["sha"],first["revision"]+1))
        self.assertEqual(reopened["returns"][0]["resumed_as"],"snapshot")
        self.assertIn("error",self.approve(bid,[first])[0])
        self.assertEqual(self.client.post(url+"/return",json=guard|dict(reason=reason)).status_code,409)
        self.assertEqual(self.client.get(BASE+"article-search?work=returned_reprint").json["total"],0)
        self.assertEqual(self.client.get(BASE+"batches?summary=1").json["overview"]["returned_reprint"],0)
        self.assertEqual(self.approve(bid,[reopened])[0]["state"],"approved")
        self.assertEqual(len(self.batch(bid)["deliveries"]),4)
        for assigned in self.batch(bid)["reviews"][0]["assignments"]:
            job=self.client.get(BASE+"jobs/"+str(assigned["id"])).json
            self.assertEqual(job["job"]["revision"],reopened["revision"])
            approval=job["delivery"]["approval"]
            self.assertEqual((approval["revision"],approval["review_sha"],approval["edition_sha"]),
                             (reopened["revision"],reopened["sha"],job["delivery"]["sha"]))
            self.assertIsInstance(approval["approved_at"],int)
            with zipfile.ZipFile(io.BytesIO(self.client.get(BASE+f"editorial/{assigned['draft']}/export").data)) as archive:
                self.assertEqual(json.loads(archive.read("metadata.json"))["approval"],approval)

    def test_reread_returned_source_keeps_reason_and_advances_revision(self):
        directory=self.source(63)
        bid=self.create([directory])
        self.prepare()
        original=self.batch(bid)["reviews"][0]
        url=BASE+f"reprints/{bid}/{original['id']}"
        guard=dict(sha=original["sha"],revision=original["revision"])
        self.client.post(url+"/return",json=guard|dict(reason="需要补全正文"))
        md=self.root/directory/"正文.md"
        md.write_text(md.read_text(encoding="utf-8")+"\n\n补全的段落",encoding="utf-8")
        req=guard|dict(expected_state="returned")
        self.assertEqual(self.client.post(url+"/reread",json=req).status_code,200)
        self.assertEqual(self.client.post(url+"/reread",json=req).status_code,409)
        self.prepare()
        updated=self.batch(bid)["reviews"][0]
        self.assertEqual(updated["returns"][0]["resumed_as"],"directory")
        self.assertEqual(updated["revision"],original["revision"]+1)
        self.assertNotEqual(updated["sha"],original["sha"])
        self.assertEqual(updated["assignments"],[])
        self.assertIn("error",self.approve(bid,[original])[0])

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

