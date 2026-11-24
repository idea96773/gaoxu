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

    def test_relocate_failed_source_preserves_selection_and_peer_schedule(self):
        dirs=[self.source(i) for i in (1,2)]
        catalog=self.client.get(BASE+"library").json
        request=dict(request_id="move",directories=dirs,targets=self.targets,rules={},
                     selections={item["directory"]:dict(title=item["title"],key=item["selection_key"]) for item in catalog["items"]})
        bid=self.client.post(BASE+"reprints",json=request).json["id"]
        new=self.root/"移动后的文章"
        (self.root/dirs[0]).rename(new)
        self.prepare()
        failed,peer=self.batch(bid)["reviews"]
        self.assertEqual(failed["state"],"invalid")
        self.approve(bid,[peer])
        scheduled=self.batch(bid)["deliveries"]
        options={r["directory"]:r["selection_key"] for r in self.client.get(BASE+"library").json["items"]}
        route=BASE+f"reprints/{bid}/{failed['id']}/relocate"
        def relocate(directory,**overrides):
            payload=dict(directory=directory,expected_root=catalog["root"],selection_key=options[directory],
                         expected_attempt=failed["prepare_attempts"])
            return self.client.post(route,json=payload|overrides)
        self.assertEqual(relocate(dirs[1]).status_code,400)
        self.assertEqual(relocate(new.name,expected_root="old").status_code,400)
        self.assertEqual(relocate(new.name,selection_key=options[dirs[1]]).status_code,400)
        self.assertEqual(relocate(new.name,expected_attempt=0).status_code,400)
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.assertEqual(relocate(new.name).status_code,400)
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        self.assertEqual(relocate(new.name).json,{"job":failed["prepare_job"]})
        self.client=create_app(self.path,self.root).test_client()
        self.prepare()
        self.assertEqual(relocate(new.name).json,{"job":failed["prepare_job"]})
        batch=self.batch(bid)
        recovered=batch["reviews"][0]
        self.assertEqual((recovered["state"],recovered["relocated_directory"],recovered["prepare_attempts"]),
                         ("pending",new.name,failed["prepare_attempts"]+1))
        self.assertEqual(batch["deliveries"],scheduled)
        self.assertEqual(self.client.post(BASE+"reprints",json=request).json["id"],bid)

    def test_background_approval_recovers_and_rolls_back_only_the_conflicting_article(self):
        bid=self.create([self.source(i) for i in (11,12,13)])
        self.prepare()
        rows=self.batch(bid)["reviews"]
        items=[dict(id=r["id"],sha=r["sha"],revision=r["revision"],expected_prior={}) for r in rows]
        items[1].update(expected_accounts={"zhihu":"模拟B","toutiao":"模拟B"},
                        expected_slots={"zhihu":1,"toutiao":1})
        def submit(selected):
            return self.client.post(BASE+f"reprints/{bid}/approve",json=dict(approved=True,background=True,items=selected)).json["items"]
        received=submit(items)
        self.assertEqual(received,submit(items))
        self.assertEqual([r["state"] for r in received],["scheduling"]*3)
        self.assertEqual(self.batch(bid)["deliveries"],[])
        stamp=int(time.time())
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.assertIsNone(claim(self.path,stamp))
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        stale=claim(self.path,stamp)
        self.assertEqual(stale[0],received[0]["job"])
        self.assertIsNone(claim(self.path,stamp))
        self.client=create_app(self.path,self.root).test_client()
        replacement=claim(self.path,stamp+31)
        self.assertEqual(replacement[0],stale[0])
        execute(self.path,stale,stamp+31)
        execute(self.path,replacement,stamp+31)
        for pending in received[1:]:
            ticket=claim(self.path,stamp+31)
            self.assertEqual(ticket[0],pending["job"])
            execute(self.path,ticket,stamp+31)
        batch=self.batch(bid)
        self.assertEqual([r["state"] for r in batch["reviews"]],["approved","pending","approved"])
        self.assertEqual([j["account"] for j in batch["reviews"][2]["assignments"]],["模拟A","模拟A"])
        self.assertEqual(len(batch["deliveries"]),4)
        self.assertEqual(batch["reviews"][1]["assignments"],[])
        self.assertIn("未来一年",batch["reviews"][1]["schedule_failure"])
        replay=submit(items)
        self.assertIn("error",replay[1])
        updated=items[1]|dict(previous_attempt=1,expected_slots=None)
        self.assertEqual(submit([updated])[0]["job"],received[1]["job"])
        self.client.post(BASE+f"batches/{bid}/cancel",json={})
        self.assertEqual(self.client.get(BASE+f"jobs/{received[1]['job']}").json["job"]["state"],"cancelled")

    def test_plan_matches_rotation_and_quota_without_reserving_work(self):
        bid=self.create([self.source(i) for i in range(5)])
        self.prepare()
        rows=self.batch(bid)["reviews"]
        items=[dict(id=r["id"],revision=r["revision"],sha=r["sha"],expected_prior={}) for r in rows]
        items[1]["sha"]="stale"
        stamp=int(datetime(2030,1,7,9,tzinfo=BEIJING).timestamp())
        with patch("distribution.reprint.time.time",return_value=stamp):
            def forecast():
                response=self.client.post(BASE+f"reprints/{bid}/plan",json=dict(items=items))
                self.assertEqual(response.status_code,200,response.json)
                return response.json["items"]
            plans=forecast()
            self.assertEqual(plans,forecast())
            self.assertIn("error",plans[1])
            self.assertEqual(self.batch(bid)["deliveries"],[])
            planned=[a for r in plans for a in r.get("assignments",[]) if a["platform"]=="zhihu"]
            self.assertEqual([a["account"] for a in planned],["模拟A","模拟B","模拟A","模拟B"])
            self.assertEqual([a["slot"] for a in planned],[stamp,stamp+30,stamp+60,stamp+86400])
            guarded=[item|dict(expected_accounts={a["platform"]:a["account"] for a in plan["assignments"]})
                     for item,plan in zip(items,plans) if "assignments" in plan]
            scheduled=self.client.post(BASE+f"reprints/{bid}/approve",json=dict(approved=True,items=guarded))
            self.assertTrue(all(r.get("state")=="approved" for r in scheduled.json["items"]))
            jobs=self.batch(bid)["deliveries"]
            self.assertEqual(sorted((j["row_id"],j["platform"],j["account"],j["slot"]) for j in jobs),
                             sorted((r["id"],a["platform"],a["account"],a["slot"]) for r in plans for a in r.get("assignments",[])))

    def test_withdraw_batch_and_restore_one_review_without_changing_scheduled_peer(self):
        dirs=[self.source(i) for i in (40,41,42)]
        bid=self.create(dirs)
        self.prepare()
        first,second,peer=self.batch(bid)["reviews"]
        decision=dict(id=first["id"],sha=first["sha"],revision=first["revision"],expected_prior={},
                      expected_accounts={"zhihu":"模拟A","toutiao":"模拟A"},expected_slots={"zhihu":1,"toutiao":1})
        received=self.client.post(BASE+f"reprints/{bid}/approve",json=dict(approved=True,background=True,items=[decision]))
        schedule_job=received.json["items"][0]["job"]
        while_scheduling=self.client.post(BASE+f"reprints/{bid}/withdraw",json={"items":[decision]})
        self.assertIn("error",while_scheduling.json["items"][0])
        self.assertTrue(tick(self.path,preparation=False))
        self.assertEqual(self.client.get(BASE+f"jobs/{schedule_job}").json["job"]["state"],"failed")
        self.approve(bid,[peer])
        jobs=self.batch(bid)["deliveries"]
        original=(self.root/dirs[0]/"正文.md").read_bytes()
        base=BASE+f"reprints/{bid}/"
        def items(*rows):
            return {"items":[{k:r[k] for k in ("id","sha","revision")} for r in rows]}
        def change(action,*rows):
            return self.client.post(base+action,json=items(*rows)).json["items"]
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        stale=first|{"revision":first["revision"]+1}
        withdrawn=change("withdraw",stale,second,first,peer)
        self.assertIn("error",withdrawn[0])
        self.assertIn("error",withdrawn[3])
        self.assertEqual([r["state"] for r in withdrawn[1:3]],["withdrawn","withdrawn"])
        self.assertEqual(change("withdraw",first),withdrawn[2:3])
        self.client=create_app(self.path,self.root).test_client()
        batch=self.batch(bid)
        self.assertEqual(batch["counts"]["withdrawn"],2)
        self.assertEqual(batch["deliveries"],jobs)
        self.assertEqual(len(batch["reviews"][0]["withdrawals"]),1)
        self.assertEqual(self.client.get(BASE+f"jobs/{schedule_job}").json["job"]["state"],"cancelled")
        found=self.client.get(BASE+"article-search",query_string={"work":"withdrawn_reprint","date_field":"imported"}).json
        self.assertEqual({r["row_id"] for r in found["items"]},{first["id"],second["id"]})
        self.assertEqual(self.client.get(base+str(first["id"])+"?platform=zhihu").json["sha"],first["sha"])
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        self.assertIn("error",self.approve(bid,[first])[0])
        restored=change("restore-selection",first)
        self.assertEqual(change("restore-selection",first),restored)
        self.assertIn("error",change("withdraw",first)[0])
        self.assertIn("error",self.approve(bid,[first])[0])
        fresh=self.batch(bid)["reviews"][0]
        self.assertEqual((fresh["revision"],fresh["state"],fresh["sha"]),(first["revision"]+1,"pending",first["sha"]))
        self.assertIsNotNone(fresh["withdrawals"][0]["restored_at"])
        self.approve(bid,[fresh])
        self.assertEqual(self.client.get(BASE+f"jobs/{schedule_job}").json["job"]["state"],"done")
        end=self.batch(bid)
        self.assertEqual(len(end["deliveries"]),4)
        self.assertEqual(end["reviews"][1]["assignments"],[])
        self.assertEqual((self.root/dirs[0]/"正文.md").read_bytes(),original)
        self.client.post(BASE+f"batches/{bid}/cancel",json={})
        self.assertIn("error",change("restore-selection",second)[0])

    def test_postpone_owned_jobs_with_conflicts_and_replay(self):
        from datetime import timedelta
        self.targets=[dict(platform="zhihu",account="甲"),dict(platform="zhihu",account="乙")]
        dirs=[self.source(i) for i in range(3)]
        data=dict(request_id="postpone",directories=dirs,targets=self.targets,rules=dict(platform_cap=2,gap=60))
        bid=self.client.post(BASE+"reprints",json=data).json["id"]
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        jobs=self.batch(bid)["deliveries"]
        start=int((datetime.now(BEIJING)+timedelta(days=5)).replace(hour=10,minute=0,second=0,microsecond=0).timestamp())
        route=BASE+f"batches/{bid}/postpone/"
        request=dict(start=start,items=[dict(id=j["id"]) for j in jobs])
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        plan=self.client.post(route+"preview",json=request).json["items"]
        self.assertEqual([p["time"] for p in plan[:2]],[start,start+60])
        self.assertGreater(datetime.fromtimestamp(plan[2]["time"],BEIJING).date(),datetime.fromtimestamp(start,BEIJING).date())
        self.assertEqual(self.batch(bid)["deliveries"],jobs)
        other=self.create([dirs[0]],"other-batch")
        self.assertIn("error",self.client.post(BASE+f"batches/{other}/postpone/preview",json=request).json["items"][0])
        first=jobs[0]
        moved=self.client.post(BASE+f"jobs/{first['id']}/reschedule",json={
            "time":start+7*86400,"expected_slot":first["slot"],"expected_execution":first["due"]})
        self.assertEqual(moved.status_code,200,moved.json)
        confirmed=self.client.post(route+"confirm",json=dict(start=start,items=plan)).json["items"]
        self.assertIn("error",confirmed[0])
        self.assertEqual([p["time"] for p in confirmed[1:]],[p["time"] for p in plan[1:]])
        replay=self.client.post(route+"confirm",json=dict(start=start,items=plan)).json["items"]
        self.assertIn("error",replay[0])
        self.assertTrue(all(entry["already"] for entry in replay[1:]))
        self.client=create_app(self.path,self.root).test_client()
        after={j["id"]:j for j in self.batch(bid)["deliveries"]}
        for j,expected in zip(jobs,[start+7*86400,plan[1]["time"],plan[2]["time"]]):
            current=after[j["id"]]
            self.assertEqual((current["slot"],current["due"]),(expected,expected))
            self.assertEqual((current["sha"],current["account"],current["draft_id"]),(j["sha"],j["account"],j["draft_id"]))
            self.assertEqual(len(self.client.get(BASE+f"jobs/{j['id']}").json["delivery"]["reschedules"]),1)
        self.assertEqual(self.batch(bid)["control"],"paused")

    def test_account_rotation_change_after_preview_leaves_article_pending(self):
        bid=self.create([self.source(1),self.source(2)])
        self.prepare()
        first,other=self.batch(bid)["reviews"]
        item=dict(id=first["id"],revision=first["revision"],sha=first["sha"],expected_prior={})
        plan=self.client.post(BASE+f"reprints/{bid}/plan",json={"items":[item]}).json["items"][0]
        item["expected_accounts"]={a["platform"]:a["account"] for a in plan["assignments"]}
        self.approve(bid,[other])
        result=self.client.post(BASE+f"reprints/{bid}/approve",json=dict(approved=True,items=[item]))
        self.assertIn("账号已变化",result.json["items"][0]["error"])
        unchanged=self.batch(bid)["reviews"][0]
        self.assertEqual((unchanged["state"],unchanged["assignments"]),("pending",[]))

    def test_background_account_turns_are_shared_before_execution_and_replay_is_idempotent(self):
        batches=[self.create([self.source(i)],str(i)) for i in (81,82,83)]
        self.prepare()

        def preview(bid):
            row=self.batch(bid)["reviews"][0]
            item={k:row[k] for k in ("id","sha","revision")} | {"expected_prior":{}}
            plan=self.client.post(BASE+f"reprints/{bid}/plan",json={"items":[item]}).json["items"][0]
            return item | {"expected_accounts":{a["platform"]:a["account"] for a in plan["assignments"]}}

        def accept(bid,item):
            return self.client.post(BASE+f"reprints/{bid}/approve",
                                    json=dict(items=[item],approved=True,background=True)).json["items"][0]

        first,second,third=batches
        first_item=preview(first)
        stale=preview(second)
        accepted=accept(first,first_item)
        self.assertEqual(accepted["state"],"scheduling")
        self.client=create_app(self.path,self.root).test_client()
        self.assertEqual(accept(first,first_item),accepted)
        self.assertIn("账号已变化",accept(second,stale)["error"])
        second_item=preview(second)
        self.assertEqual(set(second_item["expected_accounts"].values()),{"模拟B"})
        self.assertEqual(accept(second,second_item)["state"],"scheduling")
        self.assertEqual(set(preview(third)["expected_accounts"].values()),{"模拟A"})
        self.client.post(BASE+f"batches/{first}/cancel",json={})
        drain(self.path,int(time.time()))
        jobs=self.batch(second)["deliveries"]
        self.assertEqual([(j["account"],j["state"]) for j in jobs],[("模拟B","done")]*2)
        self.assertEqual(self.batch(first)["deliveries"],[])
        self.assertEqual(set(preview(third)["expected_accounts"].values()),{"模拟A"})

    def test_one_failed_intake_write_does_not_consume_a_turn_or_rollback_peers(self):
        bid=self.create([self.source(i) for i in (84,85,86)])
        self.prepare()
        items=[{k:r[k] for k in ("id","revision","sha")} for r in self.batch(bid)["reviews"]]
        with transaction(self.path) as db:
            db.execute(f"""CREATE TRIGGER fixture_refuse_decision BEFORE INSERT ON reprint_schedule_inputs
                WHEN (SELECT row_id FROM jobs WHERE id=NEW.job)={int(items[1]['id'])}
                BEGIN SELECT RAISE(ABORT,'fixture'); END""")
        results=self.client.post(BASE+f"reprints/{bid}/approve",json=dict(
            approved=True,background=True,items=items)).json["items"]
        self.assertIn("error",results[1])
        self.assertEqual([results[i]["state"] for i in (0,2)],["scheduling"]*2)
        drain(self.path,int(time.time()))
        first,failed,last=self.batch(bid)["reviews"]
        self.assertEqual((failed["state"],failed["schedule_job"]),("pending",None))
        for row,account in ((first,"模拟A"),(last,"模拟B")):
            self.assertEqual([(j["account"],j["state"]) for j in row["assignments"]],[(account,"done")]*2)

    def test_locked_schedule_survives_restart_and_isolates_a_lost_slot(self):
        bid=self.create([self.source(31),self.source(32)])
        self.targets=[dict(platform="toutiao",account="乙")]
        other=self.create([self.source(33)],"competing")
        self.prepare()
        start=int(datetime.now(BEIJING).replace(hour=9,minute=0,second=0,microsecond=0).timestamp())+86400

        def preview(batch):
            items=[dict(id=r["id"],sha=r["sha"],revision=r["revision"],expected_prior={})
                   for r in self.batch(batch)["reviews"]]
            url=BASE+f"reprints/{batch}/plan"
            self.assertEqual(self.client.post(url,json=dict(items=items,start_at=1)).status_code,400)
            result=self.client.post(url,json=dict(items=items,start_at=start)).json
            for item,plan in zip(items,result["items"],strict=True):
                item["expected_accounts"]={a["platform"]:a["account"] for a in plan["assignments"]}
                item["expected_slots"]={a["platform"]:a["slot"] for a in plan["assignments"]}
            return items

        items=preview(bid)
        self.assertEqual(items[0]["expected_slots"],dict(zhihu=start,toutiao=start))
        self.assertEqual(items[1]["expected_slots"],dict(zhihu=start+30,toutiao=start+30))
        competitor=preview(other)
        def approve(batch,selected,background):
            return self.client.post(BASE+f"reprints/{batch}/approve",
                    json=dict(items=selected,approved=True,background=background)).json["items"]
        self.assertEqual(approve(other,competitor,False)[0]["state"],"approved")
        queued=approve(bid,items,True)
        self.assertEqual(approve(bid,items,True),queued)
        for job,item in zip(queued,items,strict=True):
            decision=self.client.get(BASE+f"jobs/{job['job']}").json["review_decision"]
            self.assertEqual({p:a["account"] for p,a in decision["allocation"].items()},item["expected_accounts"])
        query=dict(work="scheduling_reprint",platform="zhihu",account="模拟B",start=datetime.fromtimestamp(start,BEIJING).date().isoformat())
        assignments=self.client.get(BASE+"article-search",query_string=query).json["items"]
        self.assertEqual([(r["row_id"],r["slot"],r["job_id"]) for r in assignments],[(items[1]["id"],start+30,None)])
        self.assertIn(dict(platform="zhihu",account="模拟B"),self.client.get(BASE+"article-search/accounts").json["items"])
        self.client=create_app(self.path,self.root).test_client()
        for accepted in queued:
            ticket=claim(self.path,int(time.time()))
            self.assertEqual(ticket[0],accepted["job"])
            execute(self.path,ticket,int(time.time()))
        batch=self.batch(bid)
        failed,approved=batch["reviews"]
        self.assertEqual((failed["state"],failed["assignments"]),("pending",[]))
        self.assertIn("冲突",failed["schedule_failure"])
        jobs=batch["deliveries"]
        self.assertEqual(approved["state"],"approved")
        self.assertEqual(self.client.get(BASE+"article-search",query_string=query).json["total"],0)
        self.assertCountEqual([(j["row_id"],j["platform"],j["account"],j["slot"]) for j in jobs],
                              [(approved["id"],p,"模拟B",start+30) for p in ("zhihu","toutiao")])

    def test_restart_after_locked_slots_keeps_original_plan_and_uses_remaining_quota(self):
        bid=self.create([self.source(i) for i in (71,72,73)])
        self.targets=[dict(platform="zhihu",account="其他账号")]
        other=self.create([self.source(74)],"other")
        self.prepare()
        start=int(datetime(2030,1,7,9,tzinfo=BEIJING).timestamp())
        resume=start+3600
        with patch("distribution.reprint.time.time",return_value=start-60):
            jobs_url=BASE+f"reprints/{bid}/"
            selected=[{k:r[k] for k in ("id","revision","sha")} | {"expected_prior":{}}
                      for r in self.batch(bid)["reviews"]]
            forecast=self.client.post(jobs_url+"plan",json=dict(items=selected,start_at=start)).json["items"]
            for item,plan in zip(selected,forecast,strict=True):
                item["expected_accounts"]={a["platform"]:a["account"] for a in plan["assignments"]}
                item["expected_slots"]={a["platform"]:a["slot"] for a in plan["assignments"]}
            decision=dict(approved=True,background=True,items=selected)
            response=self.client.post(jobs_url+"approve",json=decision).json["items"]
            self.assertEqual([r["state"] for r in response],["scheduling"]*3)
        # A different account already has a Zhihu reservation at recovery time.
        with patch("distribution.reprint.time.time",return_value=resume):
            self.approve(other,self.batch(other)["reviews"])
        self.client=create_app(self.path,self.root).test_client()
        drain(self.path,resume)
        jobs=self.batch(bid)["deliveries"]
        self.assertEqual(len(jobs),6)
        for platform,actual in (("zhihu",[resume+30,resume+60,start+86400]),
                                ("toutiao",[resume,resume+30,resume+60])):
            for job,item,received_at in zip(sorted((j for j in jobs if j["platform"]==platform),
                                                   key=lambda j:j["slot"]),selected,actual,strict=True):
                detail=self.client.get(BASE+f"jobs/{job['id']}").json
                receipt=detail["delivery"]["received"]
                self.assertTrue(receipt["matches"])
                self.assertEqual((receipt["planned"],receipt["account"],receipt["time"]),
                                 (item["expected_slots"][platform],item["expected_accounts"][platform],received_at))
        self.client.post(jobs_url+"approve",json=decision)
        self.assertEqual(self.batch(bid)["deliveries"],jobs)

    def test_expired_slot_at_review_time_cannot_be_backdated(self):
        bid=self.create([self.source(75)])
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        confirmed=int(datetime(2030,1,7,10,tzinfo=BEIJING).timestamp())
        item={k:row[k] for k in ("id","revision","sha")}
        item.update(expected_prior={},expected_accounts={p:"模拟A" for p in ("zhihu","toutiao")},
                    expected_slots={p:confirmed-1 for p in ("zhihu","toutiao")})
        with patch("distribution.reprint.time.time",return_value=confirmed):
            result=self.client.post(BASE+f"reprints/{bid}/approve",json=dict(approved=True,background=True,items=[item]))
            self.assertEqual(result.json["items"][0]["state"],"scheduling")
        drain(self.path,confirmed+3600)
        batch=self.batch(bid)
        self.assertEqual(batch["deliveries"],[])
        self.assertEqual(batch["reviews"][0]["state"],"pending")
        self.assertIn("未来一年",batch["reviews"][0]["schedule_failure"])

    def test_saved_settings_are_bound_once_and_replay_survives_rule_change(self):
        directory=self.source(14)
        catalog=self.client.get(BASE+"library").json
        article,=catalog["items"]
        data=dict(request_id="saved",directories=[directory],saved_revision=1,expected_root=catalog["root"],
                  selections={directory:dict(key=article["selection_key"],title=article["title"])})
        def submit(payload):
            return self.client.post(BASE+"reprints",json=payload,headers={"X-Gaoxu-MCP-Tool":"reprint_prepare_saved"})
        self.assertEqual(submit(data).status_code,400)
        config=dict(revision=0,targets=self.targets,links={"toutiao":{"mode":"omit"}},rules={"gap":61,"account_cap":2})
        saved=self.client.post(BASE+"reprint-settings",json=config).json
        self.assertEqual(saved["revision"],1)
        self.assertEqual(submit(data|dict(targets=self.targets)).status_code,400)
        result=submit(data)
        self.assertEqual(result.status_code,201,result.json)
        bid=result.json["id"]
        changed=self.client.post(BASE+"reprint-settings",json=config|dict(revision=1,rules={"gap":180},links={}))
        self.assertEqual(changed.json["revision"],2)
        self.assertEqual(submit(data).json["id"],bid)
        self.assertEqual(submit(data|dict(saved_revision=2)).status_code,400)
        self.assertEqual(submit(data|dict(request_id="new")).status_code,400)
        batch=self.batch(bid)
        self.assertEqual(batch["saved_revision"],1)
        for field in ("rules","targets","links","destination"):
            self.assertEqual(batch[field],saved["settings"][field])
        self.prepare()
        self.assertEqual(self.batch(bid)["reviews"][0]["state"],"pending")
        self.assertEqual(self.batch(bid)["deliveries"],[])
        runs=self.client.get(BASE+"agent-runs").json["items"]
        self.assertTrue(any(r["tool"]=="reprint_prepare_saved" and r["subject"]=="批次 "+bid for r in runs))

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

    def test_global_platform_rates_match_unique_delivery_search_results(self):
        from .test_workflow import options
        flow=self.client.post(BASE+"batches",json=options(1,targets=[dict(platform="zhihu",account="模拟")])).json
        drain(self.path,int(time.time()))
        pipeline,=self.batch(flow["id"])["publishes"]
        pipeline_month=datetime.fromtimestamp(pipeline["slot"],BEIJING).strftime("%Y-%m")
        dirs=[self.source(i) for i in range(10)]
        bid=self.create(dirs)
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        repeated=self.create(dirs[:2],"same-sources")
        self.prepare()
        self.assertEqual(self.batch(repeated)["counts"],{"duplicate":2})
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        with transaction(self.path) as db:
            jobs=db.execute("""SELECT j.id,j.slot FROM jobs j JOIN rows r ON r.id=j.row_id
                WHERE j.stage='deliver' AND j.platform='zhihu' AND r.batch=? ORDER BY j.id""",(bid,)).fetchall()
            for job,receipt in zip(jobs[:4],("simulated-receipt-1","simulated-receipt-2","",None)):
                db.execute("UPDATE jobs SET state='done',finished=slot,receipt=? WHERE id=?",(receipt,job["id"]))
            db.execute("UPDATE jobs SET state='done',receipt='missing-time' WHERE id=?",(jobs[4]["id"],))
            for job,state in zip(jobs[5:8],("failed","running","cancelled")):
                db.execute("UPDATE jobs SET state=? WHERE id=?",(state,job["id"]))
        platforms=self.client.get(BASE+"batches?summary=1").json["overview"]["delivery_platforms"]
        self.assertEqual(platforms["zhihu"],dict(total=10,done=2,unconfirmed=3,failed=1,running=1,
                                                cancelled=1,paused=2,success_percent=66.7))
        self.assertEqual(platforms["toutiao"],dict(total=10,paused=10,success_percent=None))
        for platform,counts in platforms.items():
            for state,n in counts.items():
                if state=="success_percent":
                    continue
                args=dict(work="frozen_delivery",platform=platform)
                if state!="total":
                    args["schedule_state"]=state
                result=self.client.get(BASE+"article-search",query_string=args).json
                self.assertEqual(result["total"],n)
                self.assertEqual(len({row["job_id"] for row in result["items"]}),n)
                self.assertTrue(all(row["stage"]=="deliver" for row in result["items"]))
        month=datetime.fromtimestamp(jobs[2]["slot"],BEIJING).strftime("%Y-%m")
        days=self.client.get(BASE+"schedule-calendar",query_string=dict(month=month,platform="zhihu",
            schedule_state="unconfirmed")).json["days"]
        self.assertEqual(sum(day["unconfirmed"] for day in days.values()),sum(
            datetime.fromtimestamp(j["slot"],BEIJING).strftime("%Y-%m")==month for j in jobs[2:5]))
        args=dict(month=pipeline_month,platform="zhihu",schedule_state="done")
        def count_calendar(filters):
            days=self.client.get(BASE+"schedule-calendar",query_string=filters).json["days"]
            return sum(sum(day.values()) for day in days.values())
        self.assertEqual(count_calendar(args),1+count_calendar(args|dict(work="frozen_delivery")))

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

    def test_match_downloaded_links_then_prepare_selected_copies(self):
        import shutil
        first=self.source(41)
        shared=self.source(42)
        copy="其他下载/同一原文"
        shutil.copytree(self.root/shared,self.root/copy)
        long_header=self.source(43)
        md=self.root/long_header/"正文.md"
        original=md.read_text(encoding="utf-8")
        source="https://mp.weixin.qq.com/s?__biz=test&mid=57&idx=2"
        md.write_text(original.replace("测试作者","甲"*4000).replace("https://mp.weixin.qq.com/s/test-export-43",source),
                      encoding="utf-8")
        url="https://mp.weixin.qq.com/s/test-export-41"
        endpoint=BASE+"library/match-links"
        lines="\n".join([url+"?scene=1","",url+"#read","https://example.invalid/s/42",
                         "https://mp.weixin.qq.com/s/no-local-copy","https://mp.weixin.qq.com/s/test-export-42",
                         "https://mp.weixin.qq.com/s?mid=57&idx=2&__biz=test&scene=1"])
        response=self.client.post(endpoint+"?catalog=1",json=dict(urls=lines,expected_root=str(self.root)))
        self.assertEqual(response.status_code,200,response.json)
        rows=response.json["matches"]
        self.assertEqual([(r["line"],r["status"]) for r in rows],[(1,"matched"),(3,"duplicate"),(4,"invalid"),
                         (5,"missing"),(6,"ambiguous"),(7,"matched")])
        self.assertEqual(rows[1]["first_line"],1)
        self.assertCountEqual([c["directory"] for c in rows[4]["candidates"]],[shared,copy])
        chosen=[rows[0]["candidates"][0],next(c for c in rows[4]["candidates"] if c["directory"]==copy),
                rows[5]["candidates"][0]]
        self.assertEqual([c["directory"] for c in chosen],[first,copy,long_header])
        with transaction(self.path,False) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0],0)
        data=dict(request_id="from-links",expected_root=str(self.root),targets=self.targets,
                  directories=[c["directory"] for c in chosen],
                  selections={c["directory"]:dict(title=c["title"],key=c["selection_key"]) for c in chosen})
        bid=self.client.post(BASE+"reprints",json=data).json["id"]
        self.prepare()
        self.assertEqual([row["state"] for row in self.batch(bid)["reviews"]],["pending"]*3)
        self.assertEqual(self.batch(bid)["deliveries"],[])
        self.assertEqual(self.client.post(BASE+"reprints",json=data).json["id"],bid)
        bulk=self.client.post(endpoint,json=dict(urls=[url]*100)).json
        self.assertNotIn("items",bulk)
        self.assertEqual(Counter(r["status"] for r in bulk["matches"]),{"matched":1,"duplicate":99})
        for invalid in (dict(urls=[""]*3),dict(urls=[url]*101),dict(urls=url,expected_root="old")):
            self.assertEqual(self.client.post(endpoint,json=invalid).status_code,400)

    def test_clean_install_waits_for_local_library_configuration(self):
        from .library import library_settings
        fresh=self.root/"new-install.sqlite"
        self.assertEqual(library_settings(fresh),dict(root="",revision=0,editable=True))
        self.assertFalse(fresh.exists())
        with patch.dict("os.environ",{"DIST_LIBRARY":""}):
            ui=create_app(fresh).test_client()
            with patch("distribution.library.os.walk",side_effect=AssertionError("未配置时不能扫描目录")):
                self.assertEqual(ui.get(BASE+"library").json,dict(items=[],root=""))
                self.assertEqual(ui.get(BASE+"library?page=1&page_size=5").json["total"],0)
                for endpoint in ("library","reprints"):
                    result=ui.post(BASE+endpoint,json=dict(directories=["."],request_id="choose-first",targets=self.targets))
                    self.assertEqual(result.status_code,400)
                    self.assertIn("保存目录设置",result.json["error"])
            folder=self.root/"我的导出"
            rel=export_folder(folder,1).relative_to(folder).as_posix()
            response=ui.post(BASE+"library-settings",json=dict(root=str(folder),revision=0))
            self.assertEqual(response.json,dict(root=str(folder),revision=1,editable=True))
            reopened=create_app(fresh).test_client()
            result=reopened.get(BASE+"library").json
            self.assertEqual((result["root"],result["items"][0]["directory"]),(str(folder),rel))
            self.assertEqual(reopened.post(BASE+"reprints",json=dict(directories=[rel],request_id="choose-first",
                             targets=self.targets,expected_root=str(folder))).status_code,201)

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

    def test_html_image_order_and_repeats_survive_review_retry_and_export(self):
        from .folders import Images
        dirs=[self.source(i) for i in (78,79,80)]
        for directory in dirs[:2]:
            folder=self.root/directory
            (folder/"img/b.gif").write_bytes(bytes.fromhex(
                "47494638396101000100800000000000ffffff2c00000000010001000002024401003b"))
            with (folder/"正文.md").open("a",encoding="utf-8") as stream:
                stream.write("\n\n第二段\n\n![第二张](img/b.gif)")
        # Both bad inputs have the same distinct images in HTML and Markdown.
        def html_order(directory,names):
            (self.root/directory/"正文.html").write_text("".join(f'<img src="img/{name}">' for name in names),encoding="utf-8")
        html_order(dirs[0],["0.png","b.gif","0.png"])
        html_order(dirs[1],["b.gif","0.png"])
        bid=self.create(dirs)
        self.prepare()
        short,swapped,peer=self.batch(bid)["reviews"]
        self.assertEqual([r["state"] for r in (short,swapped,peer)],["invalid","invalid","pending"])
        self.assertIn("HTML 第 3 处配图",short["note"])
        self.assertIn("顺序不同",swapped["note"])
        with (self.root/dirs[0]/"正文.md").open("a",encoding="utf-8") as stream:
            stream.write("\n\n结尾段落\n\n![再次引用](img/0.png)")
        self.assertEqual(self.client.post(BASE+f"jobs/{short['prepare_job']}/retry",json={}).status_code,200)
        self.prepare()
        repaired,still_bad,still_pending=self.batch(bid)["reviews"]
        self.assertEqual((repaired["state"],still_bad,still_pending),("pending",swapped,peer))
        snapshot=self.client.get(BASE+f"reprints/{bid}/{repaired['id']}?platform=zhihu").json["snapshot"]
        self.assertEqual((snapshot["html_image_count"],len(snapshot["images"])),(3,3))
        self.assertEqual(snapshot["images"][0],snapshot["images"][2])
        self.assertNotEqual(snapshot["images"][0],snapshot["images"][1])
        self.approve(bid,[repaired])
        for job in self.batch(bid)["reviews"][0]["assignments"]:
            frozen=self.client.get(BASE+f"jobs/{job['id']}/reprint-check").json
            self.assertEqual(frozen["images"],snapshot["images"])
            with zipfile.ZipFile(io.BytesIO(self.client.get(BASE+f"editorial/{job['draft']}/export").data)) as archive:
                markup=archive.read("article.html").decode()
                metadata=json.loads(archive.read("metadata.json"))
                self.assertEqual((metadata["html_image_count"],metadata["body_image_count"]),(3,3))
                images=Images()
                images.feed(markup)
                self.assertEqual(images.refs,["img/0.png","img/b.gif","img/0.png"])
                self.assertLess(markup.index("第二段"),markup.index("结尾段落"))
                self.assertTrue(markup.rstrip().endswith(f'<a href="{DESTINATION}">延伸阅读</a></p>'))

    def test_reprint_images_use_visible_article_and_keep_markdown_checks(self):
        dirs=[self.source(i) for i in (81,82,83)]
        def source_html(index,body):
            (self.root/dirs[index]/"正文.html").write_text(
                '<header><img src="https://example.invalid/avatar"></header>'
                '<template><section id="js_content"><img src="img/template.png"></section></template>'
                '<article id="js_content">'+body+'<span style="display:none !important"><img src="img/hidden.png"></span>'
                '<img hidden src="img/invisible.png"></article><footer><img src="img/qrcode.png"></footer>',
                encoding="utf-8")
        source_html(0,'<p>第81篇的本地正文。</p><img src="img/missing-placeholder.png" data-src="img/0.png">')
        source_html(1,'<p>第82篇的本地正文。</p><img src="img/0.png"><img alt="缺图">')
        source_html(2,'<p>第83篇的本地正文。</p><img src="img/0.png">')
        with (self.root/dirs[2]/"正文.md").open("a",encoding="utf-8") as stream:
            stream.write("\n\n![正文附图](img/supplement.png)")
        bid=self.create(dirs)
        self.prepare()
        ready,missing,extra=self.batch(bid)["reviews"]
        self.assertEqual([r["state"] for r in (ready,missing,extra)],["pending","invalid","invalid"])
        self.assertIn("第 2 处配图缺少图片地址",missing["note"])
        self.assertIn("supplement.png",extra["note"])
        self.approve(bid,[ready])
        for job in self.batch(bid)["reviews"][0]["assignments"]:
            report=self.client.get(BASE+f"jobs/{job['id']}/reprint-check").json
            self.assertEqual((len(report["images"]),report["unique_images"],report["reader_url"]),(1,1,DESTINATION))
            self.assertEqual(report["body_comparison"]["outside_images"],2)
            with zipfile.ZipFile(io.BytesIO(self.client.get(BASE+f"editorial/{job['draft']}/export").data)) as archive:
                self.assertEqual([n for n in archive.namelist() if n.startswith("img/")],["img/0.png"])
                self.assertEqual(json.loads(archive.read("metadata.json"))["html_image_count"],1)
        source_html(1,'<p>第82篇的本地正文。</p><img src="img/0.png">')
        self.assertEqual(self.client.post(BASE+f"jobs/{missing['prepare_job']}/retry",json={}).status_code,200)
        self.prepare()
        accepted,repaired,unresolved=self.batch(bid)["reviews"]
        self.assertEqual((accepted["state"],repaired["state"],unresolved["state"]),("approved","pending","invalid"))

    def test_images_without_unique_article_root_use_visible_document_order(self):
        from .folders import Images
        for roots in ([],['id="js_content"','id="js_content"']):
            parser=Images()
            parser.feed('<img src="img/first.png"><div style="visibility:hidden"><img src="hidden.png"></div>'
                        +''.join('<section '+flag+'><img src="img/repeat.png"></section>' for flag in roots)
                        +'<img src="img/last.png">')
            parser.close()
            self.assertEqual(parser.refs,["img/first.png"]+["img/repeat.png"]*len(roots)+["img/last.png"])

    def test_moved_repeated_image_shows_its_text_position_in_review(self):
        directory=self.source(84)
        folder=self.root/directory
        md=folder/"正文.md"
        md.write_text(md.read_text(encoding="utf-8").replace("第84篇的本地正文。",
                      "甲\n\n![插图](img/0.png)\n\n乙丙"),encoding="utf-8")
        (folder/"正文.html").write_text('<div id="js_content"><p>甲</p><img src="img/0.png">'
            '<p>乙<img src="img/0.png">丙</p></div>',encoding="utf-8")
        bid=self.create([directory])
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        self.assertEqual((row["state"],row["text_changed"],row["structure_changed"]),("pending",0,1))
        snapshot=self.client.get(BASE+f"reprints/{bid}/{row['id']}?platform=zhihu").json["snapshot"]
        self.assertEqual(snapshot["body_comparison"]["image_placement"],dict(count=1,position=2,
            source=dict(before="甲乙",after="丙"),draft=dict(before="甲乙丙",after="")))

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

    def test_resume_image_preparation_uses_only_confirmed_objects_for_current_grant(self):
        from . import draft_images, rights
        from .zhihu_draft_worker import DraftDeferred, prepare_content
        self.targets=[dict(platform="zhihu",account="模拟")]
        directory=self.source(73)
        folder=self.root/directory
        (folder/"img/last.gif").write_bytes(bytes.fromhex(
            "47494638396101000100800000000000ffffff2c00000000010001000002024401003b"))
        with (folder/"正文.md").open("a",encoding="utf-8") as stream:
            stream.write("\n\n![重复](img/0.png)\n\n![末图](img/last.gif)")
        bid=self.create([directory])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        job,=self.batch(bid)["deliveries"]
        uid="fixture"
        with transaction(self.path) as db:
            rights.record(db,job["id"],job["sha"],"zhihu_draft",uid,int(time.time()))
            db.execute("""INSERT INTO platform_drafts(job,canonical,sha,user_id,name,state,updated)
                SELECT ?,canonical,?,?,'测试账号','preparing',0 FROM rows WHERE id=?""",
                (job["id"],job["sha"],uid,job["row_id"]))
            task=dict(db.execute("SELECT * FROM platform_drafts WHERE job=?",(job["id"],)).fetchone())

        bridge=Mock()
        bridge.identity.return_value=(uid,"测试账号")
        cdn="https://pic1.zhimg.com/v2-"+("a"*32)+".png"
        temporary="https://pic1.zhimg.com/v2-"+("b"*32)+".gif?size=original"
        bridge.upload.side_effect=[cdn,TimeoutError()]
        with self.assertRaises(TimeoutError):
            prepare_content(self.path,task,bridge)
        info=self.client.get(BASE+f"jobs/{job['id']}/platform-draft").json
        self.assertEqual(info["reusable_images"],1)
        bridge.upload.reset_mock()
        bridge.upload.side_effect=[temporary]
        edition=prepare_content(self.path,task,bridge)
        self.assertEqual(bridge.upload.call_count,1)
        self.assertEqual(edition["content"].count(cdn),2)
        self.assertIn(temporary,edition["content"])
        self.assertIn(DESTINATION,edition["content"])
        with transaction(self.path) as db:
            grant=rights.current(db,job["id"])["revision"]
            self.assertEqual(list(draft_images.confirmed(db,task["id"],grant).values()),[cdn])
            db.execute("UPDATE zhihu_draft_images SET confirmed=0")
            self.assertFalse(draft_images.confirmed(db,task["id"],grant))
            db.execute("UPDATE zhihu_draft_images SET confirmed=?",(int(time.time()),))
            rights.record(db,job["id"],job["sha"],"local","",int(time.time()))
            rights.record(db,job["id"],job["sha"],"zhihu_draft",uid,int(time.time()))
        bridge.identity.return_value=("different-user","测试账号")
        bridge.upload.side_effect=[cdn]
        with self.assertRaises(DraftDeferred):
            prepare_content(self.path,task,bridge)
        self.assertEqual(self.client.get(BASE+f"jobs/{job['id']}/platform-draft").json["reusable_images"],0)

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

    def test_compare_visible_original_and_restore_only_verified_emphasis(self):
        from .body_compare import checked_render
        from .markdown_media import MARKDOWN
        def compare(original,md):
            return checked_render('<div id="js_content">'+original+'</div>',MARKDOWN.parse(md))
        html,report=compare('<p>甲<strong>《原文》</strong>乙</p><style>ignore</style><p hidden>ignore</p>',
                            '甲**《原文》**乙')
        self.assertIn('甲<strong>《原文》</strong>乙',html)
        self.assertEqual(report["restored_bold"],1)
        self.assertNotIn("difference",report)
        for original,md in [("甲《原文》乙","甲**《原文》**乙"),
                            ("甲<strong>《原文》</strong>乙","甲`**《原文》**`乙"),
                            ("甲<strong>《原文》</strong>乙丙","甲**《原文》**乙"),
                            ("甲《原文》乙<strong>《原文》</strong>","甲**《原文》**乙《原文》")]:
            html,report=compare(original,md)
            self.assertEqual(report["restored_bold"],0)
            self.assertIn('**',html)
        _,literal=compare("甲**《原文》**乙","甲**《原文》**乙")
        self.assertNotIn("difference",literal)
        self.assertEqual(checked_render("<p>文章</p>",[])[1]["status"],"no_body")
        self.assertEqual(checked_render("",[])[1]["status"],"no_html")
        self.assertEqual(checked_render('<div id="js_content"></div>'*2,[])[1]["status"],"no_body")

    def test_body_link_change_with_identical_text_is_visible_until_reread(self):
        directory=self.source(49)
        folder=self.root/directory
        md=folder/"正文.md"
        original="https://example.invalid/p?id=8&v=1#references"
        changed=original.replace("id=8","id=9")
        md.write_text(md.read_text(encoding="utf-8").replace("第49篇的本地正文。",f"请[看文献]({changed})。"),encoding="utf-8")
        (folder/"正文.html").write_text('<a href="https://outside.invalid">顶部</a><div id="js_content">'
            '<p>请<a href="'+original.replace("&","&amp;")+'"><b>看</b>文献</a>。</p>'
            '<a hidden href="https://hidden.invalid">装饰</a><img src="img/0.png"></div>',encoding="utf-8")
        bid=self.create([directory])
        self.prepare()
        row,=self.batch(bid)["reviews"]
        self.assertFalse(row["text_changed"] or row["structure_changed"])
        self.assertTrue(row["links_changed"])
        route=BASE+f"reprints/{bid}/{row['id']}"
        report=self.client.get(route+"?platform=zhihu").json["snapshot"]["body_comparison"]["web_links"]
        self.assertTrue(report["positions_compared"])
        self.assertEqual((report["source_count"],report["draft_count"]),(1,1))
        self.assertEqual(report["source_only"]["examples"],[dict(position=2,label="看文献",url=original)])
        self.assertEqual(report["draft_only"]["examples"][0]["url"],changed)
        md.write_text(md.read_text(encoding="utf-8").replace(changed,original),encoding="utf-8")
        self.assertEqual(self.client.post(route+"/reread",json=dict(sha=row["sha"],revision=row["revision"])).status_code,200)
        self.prepare()
        fresh,=self.batch(bid)["reviews"]
        self.assertFalse(fresh["links_changed"])
        self.approve(bid,[fresh])
        for job in self.batch(bid)["deliveries"]:
            frozen=self.client.get(BASE+f"jobs/{job['id']}/reprint-check").json
            self.assertEqual(frozen["body_comparison"]["web_links"],dict(source_count=1,draft_count=1,positions_compared=True))
            self.assertIn(original.replace("&","&amp;"),frozen["html"])
            self.assertEqual(frozen["reader_url"],DESTINATION)

    def test_body_link_comparison_tracks_occurrences_and_order(self):
        from .body_compare import checked_render
        from .markdown_media import MARKDOWN
        def compare(source,md):
            return checked_render('<div id="js_content">'+source+'</div>',MARKDOWN.parse(md))[1]["web_links"]
        a='<a href="https://a.invalid">甲</a>'
        b='<a href="https://b.invalid">乙</a>'
        diff=compare(a+a,'[甲](https://a.invalid)甲')
        self.assertEqual(diff["source_only"]["count"],1)
        self.assertEqual(diff["draft_only"]["count"],0)
        diff=compare(a+"甲",'甲[甲](https://a.invalid)')
        self.assertEqual(diff["draft_only"]["examples"][0]["position"],2)
        diff=compare(a+b,'新增[乙](https://b.invalid)[甲](https://a.invalid)')
        self.assertFalse(diff["positions_compared"])
        self.assertEqual(diff["order"]["position"],1)
        diff=compare('原段'+a,'修改后的段落[甲](https://a.invalid)')
        self.assertNotIn('source_only',diff)
        diff=compare('<a href="//example.invalid/文">相同</a>','[相同](https://example.invalid/%E6%96%87)')
        self.assertNotIn('source_only',diff)

    def test_source_breaks_survive_review_freeze_and_export(self):
        directory=self.source(48)
        folder=self.root/directory
        md=folder/"正文.md"
        md.write_text(md.read_text(encoding="utf-8").replace("第48篇的本地正文。","甲 & 乙丙丁"),encoding="utf-8")
        original=('<div id="js_content"><p>甲 &amp; 乙</p><section><p>丙<br>丁</p></section>'
                  '<p hidden>排版外文字</p><img src="img/0.png"></div>')
        (folder/"正文.html").write_text(original,encoding="utf-8")
        bid=self.create([directory])
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        view=self.client.get(BASE+f"reprints/{bid}/{row['id']}?platform=zhihu").json["snapshot"]
        self.assertEqual(view["body_comparison"]["restored_breaks"],2)
        self.assertTrue(view["body_comparison"]["structure_matches"])
        segment="<p>甲 &amp; 乙</p>\n<p>丙<br />\n丁</p>"
        self.assertIn(segment,view["html"])
        self.approve(bid,[row])
        jobs=self.batch(bid)["reviews"][0]["assignments"]
        for job in jobs:
            inspected=self.client.get(BASE+f"jobs/{job['id']}/reprint-check").json
            self.assertIn(segment,inspected["html"])
            self.assertEqual((len(inspected["images"]),inspected["reader_url"]),(1,DESTINATION))
            archive=self.client.get(BASE+f"editorial/{job['draft']}/export")
            with zipfile.ZipFile(io.BytesIO(archive.data)) as pack:
                self.assertIn(segment,pack.read("article.html").decode())
                self.assertIn("甲 & 乙丙丁",pack.read("article.md").decode())

    def test_source_breaks_compare_visible_positions_and_preserve_markup(self):
        from .body_compare import checked_render
        from .markdown_media import MARKDOWN
        def compare(original,md):
            return checked_render('<div id="js_content">'+original+'</div>',MARKDOWN.parse(md))
        markup,report=compare("<section><p>甲</p></section><div hidden><br>装饰</div><p>乙</p>","甲\n\n乙")
        self.assertTrue(report["structure_matches"])
        self.assertNotIn("break_example",report)
        markup,report=compare('<p>甲<section style="display:inline-block">乙</section>丙</p>',"甲乙丙")
        self.assertEqual(markup,"<p>甲乙丙</p>\n")
        self.assertTrue(report["structure_matches"])
        markup,report=compare("<p>甲</p><p>乙</p>","[甲乙](https://example.invalid/)")
        self.assertIn('>甲乙</a>',markup)
        self.assertEqual((report["missing_breaks"],report["extra_breaks"]),(1,0))
        self.assertTrue(report["break_example"]["missing"])
        _,report=compare("<p>甲乙</p>","甲\n\n乙")
        self.assertEqual((report["missing_breaks"],report["extra_breaks"]),(0,1))
        markup,report=compare("<p>甲</p><p>乙</p>","甲丁")
        self.assertIn("difference",report)
        self.assertNotIn("restored_breaks",report)
        markup,report=compare("<p>甲</p><p>乙<img src='a'>丙</p>","甲乙丙![图](a)")
        self.assertNotIn("restored_breaks",report)

    def test_body_difference_shown_on_review_then_cleared_on_reread_and_frozen(self):
        directory=self.source(49)
        folder=self.root/directory
        original='<div id="js_content"><h2>示例</h2><p>正文保留尾段</p><img src="img/0.png"></div>'
        (folder/"正文.html").write_text(original,encoding="utf-8")
        md=folder/"正文.md"
        md.write_text(md.read_text(encoding="utf-8").replace("第49篇的本地正文。","示例\n\n正文"),encoding="utf-8")
        bid=self.create([directory])
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        self.assertTrue(row["text_changed"] and row["structure_changed"])
        route=BASE+f"reprints/{bid}/{row['id']}"
        view=self.client.get(route+"?platform=zhihu").json["snapshot"]
        diff=view["body_comparison"]
        self.assertEqual(diff["difference"]["source"],"保留尾段")
        self.assertEqual(diff["structure_counts"]["h2"],[1,0])
        md.write_text(md.read_text(encoding="utf-8").replace("示例\n\n正文","## 示例\n\n正文保留尾段"),encoding="utf-8")
        response=self.client.post(route+"/reread",json={"sha":row["sha"],"revision":row["revision"]})
        self.assertEqual(response.status_code,200,response.json)
        self.prepare()
        row=self.batch(bid)["reviews"][0]
        self.assertFalse(row["text_changed"] or row["structure_changed"])
        self.approve(bid,[row])
        assignment=self.batch(bid)["reviews"][0]["assignments"][0]
        frozen=self.client.get(BASE+f"jobs/{assignment['id']}/reprint-check").json
        self.assertTrue(frozen["body_comparison"]["structure_matches"])
        self.assertNotIn("difference",frozen["body_comparison"])
        self.assertIn("<h2>示例</h2>",frozen["html"])
        archive=self.client.get(BASE+f"editorial/{assignment['draft']}/export")
        with zipfile.ZipFile(io.BytesIO(archive.data)) as pack:
            self.assertEqual(json.loads(pack.read("metadata.json"))["body_comparison"],frozen["body_comparison"])

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
        plan=self.client.post(BASE+f"reprints/{second}/plan",json={"items":[item(row,{"zhihu":original})]})
        projected={a["platform"]:a for a in plan.json["items"][0]["assignments"]}
        self.assertEqual(projected["zhihu"]["prior_job"],original)
        self.assertEqual(projected["zhihu"]["slot"],self.batch(first)["deliveries"][0]["slot"])
        self.assertIsNone(projected["toutiao"]["prior_job"])
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

    def test_directory_copies_share_one_review_and_frozen_delivery_per_platform(self):
        import shutil
        first=self.source(41)
        other=self.source(42)
        copy="另存/优先采用这份"
        shutil.copytree(self.root/first,self.root/copy)
        md=self.root/copy/"正文.md"
        md.write_text(md.read_text(encoding="utf-8").replace("本地正文","优先版本").replace("test-export-41)",
                      "test-export-41?scene=2)"),encoding="utf-8")
        catalog=self.client.get(BASE+"library").json
        marks={r["directory"]:dict(source=r["source"],title=r["title"],key=r["selection_key"]) for r in catalog["items"]}
        data=dict(request_id="copies",targets=self.targets,directories=[copy,other,first,copy],
                  expected_root=catalog["root"],selections=marks)
        # A mismatched identity must not suppress an unrelated original.
        wrong=marks|{other:marks[other]|dict(source=marks[copy]["source"])}
        self.assertEqual(self.client.post(BASE+"reprints",json=data|dict(selections=wrong)).status_code,400)
        bid=self.client.post(BASE+"reprints",json=data).json["id"]
        self.assertEqual(self.client.post(BASE+"reprints",json=data).json["id"],bid)
        batch=self.batch(bid)
        self.assertEqual(batch["counts"],dict(preparing=2,duplicate=2))
        self.assertEqual([r["owner"] for r in batch["reviews"]],[None,None,batch["reviews"][0]["id"],batch["reviews"][0]["id"]])
        self.assertEqual(sum(bool(r["prepare_job"]) for r in batch["reviews"]),2)
        self.prepare()
        primary,peer,*copies=self.batch(bid)["reviews"]
        preview=self.client.get(BASE+f"reprints/{bid}/{primary['id']}?platform=zhihu").json
        self.assertIn("优先版本",preview["snapshot"]["text"])
        self.approve(bid,[primary,peer])
        result=self.batch(bid)
        self.assertEqual(len(result["deliveries"]),4)
        ids=[a["id"] for a in result["reviews"][0]["assignments"]]
        for row in result["reviews"][2:]:
            self.assertEqual([a["id"] for a in row["assignments"]],ids)
            self.assertTrue(all(a["reused"] for a in row["assignments"]))

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

    def test_bridge_diagnostics_match_extension_envelopes_without_exposing_messages(self):
        from .zhihu_draft_worker import Bridge, BridgeProblem
        socket=Mock()
        bridge=Bridge(socket,"fixture")
        def reply(payload):
            socket.recv.side_effect=lambda **_:json.dumps(dict(id=json.loads(socket.send.call_args.args[0])["id"],**payload))
        for payload,phase in [
            ({"error":{"code":401,"message":"private fixture detail"}},"token_missing"),
            ({"error":{"code":403,"message":"private fixture detail"}},"token_mismatch"),
            ({"result":{"isAuthenticated":False,"error":"private fixture detail"}},"identity_unavailable"),
            ({"result":{"isAuthenticated":"false"}},"identity_invalid"),
            ({"result":{"isAuthenticated":True,"userId":"id","username":[]}},"identity_invalid")]:
            reply(payload)
            with self.assertRaises(BridgeProblem) as caught:
                bridge.identity()
            self.assertEqual(caught.exception.phase,phase)
            self.assertNotIn("private fixture detail",str(caught.exception))
        reply({"result":{"isAuthenticated":False}})
        self.assertEqual(bridge.identity(),("",""))
        reply({"result":{"isAuthenticated":True,"userId":"fixture-id","username":"测试号"}})
        self.assertEqual(bridge.identity(),("fixture-id","测试号"))

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

    def test_scoped_bridge_leaves_older_job_for_same_account_queued(self):
        from .draft_bridge import heartbeat, listener
        from .platform_draft import claim as claim_draft
        self.targets=[dict(platform="zhihu",account="模拟A")]
        bid=self.create([self.source(71),self.source(72)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        older,selected=sorted(self.batch(bid)["deliveries"],key=lambda job:job["id"])
        candidates=BASE+"article-search?draft_review=unsubmitted_reprint&platform=zhihu"
        self.assertEqual({r["job_id"] for r in self.client.get(candidates).json["items"]},{older["id"],selected["id"]})
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.assertEqual(self.client.get(candidates).json["total"],0)
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        accounts=BASE+"zhihu-draft-accounts"

        def endpoint(job):
            return BASE+f"jobs/{job['id']}/platform-draft"

        def queue(job):
            url=endpoint(job)
            revision=self.client.get(url).json["rights"]["revision"]
            return self.client.post(url,json=dict(action="queue",scope_revision=revision,sha=job["sha"],
                                                   user_id="fixture",consent=True))

        with listener(self.path,9541,False,"original"):
            heartbeat(self.path,"fixture","测试账号",session="original")
            bound=self.client.post(accounts,json=dict(action="bind",alias="模拟A",revision=0,user_id="fixture"))
            self.assertEqual(bound.status_code,200)
            for job in (older,selected):
                url=endpoint(job)
                revision=self.client.get(url).json["rights"]["revision"]
                granted=self.client.post(url,json=dict(action="scope",scope="zhihu_draft",rights_confirmed=True,
                    sha=job["sha"],user_id="fixture",scope_revision=revision))
                self.assertEqual(granted.status_code,200)
            self.assertEqual(queue(older).status_code,200)
        with listener(self.path,9541,False,"one",selected["id"]):
            heartbeat(self.path,"fixture","测试账号",session="one")
            self.assertEqual(self.client.get(accounts).json["bridge"]["job_id"],selected["id"])
            self.assertIn("仅处理任务",queue(older).json["error"])
            self.assertEqual(queue(selected).status_code,200)
            launch=self.client.get(endpoint(selected)+"?launch=1").json
            self.assertEqual((launch["database"],launch["sha"],launch["job"],launch["account"]),
                             (str(self.path.resolve()),selected["sha"],selected["id"],"模拟A"))
            self.assertIsNone(claim_draft(self.path,"other",selected["id"]))
            self.assertIsNone(claim_draft(self.path,"fixture",selected["id"],older["sha"]))
            self.assertEqual(claim_draft(self.path,"fixture",selected["id"],launch["sha"])["job"],selected["id"])
            self.assertIsNone(claim_draft(self.path,"fixture",selected["id"]))
            self.assertEqual(self.client.get(endpoint(older)).json["record"]["state"],"queued")
        self.assertIsNone(self.client.get(accounts).json["bridge"]["job_id"])
        self.assertEqual(claim_draft(self.path,"fixture")["job"],older["id"])
        self.assertEqual(self.client.get(candidates).json["total"],0)

    def test_console_choice_confirms_one_frozen_job_per_bridge_session(self):
        from .draft_bridge import heartbeat, listener
        from .platform_draft import claim as claim_draft, transition
        self.targets=[dict(platform="zhihu",account="模拟A")]
        bid=self.create([self.source(i) for i in (70,71,72)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        older,*candidates=self.batch(bid)["deliveries"]
        def url(j):
            return BASE+f"jobs/{j['id']}/platform-draft"
        def queue(j,session,**overrides):
            with self.app.test_client() as client:
                revision=client.get(url(j)).json["rights"]["revision"]
                return client.post(url(j),json=dict(action="queue",scope_revision=revision,sha=j["sha"],
                    user_id="fixture",consent=True,choice_session=session)|overrides)
        def recover(j,session):
            detail=self.client.get(url(j)).json
            return self.client.post(url(j),json=dict(action="recover_interrupted",choice_session=session,
                expected_event=detail["state_history"][0]["id"],user_id="fixture",sha=j["sha"]))
        with listener(self.path,9541,False,"queue"):
            heartbeat(self.path,"fixture","测试账号",session="queue")
            bound=self.client.post(BASE+"zhihu-draft-accounts",json=dict(action="bind",alias="模拟A",revision=0,user_id="fixture"))
            self.assertEqual(bound.status_code,200)
            for j in [older,*candidates]:
                revision=self.client.get(url(j)).json["rights"]["revision"]
                result=self.client.post(url(j),json=dict(action="scope",scope="zhihu_draft",rights_confirmed=True,
                    scope_revision=revision,user_id="fixture",sha=j["sha"]))
                self.assertEqual(result.status_code,200)
            self.assertEqual(queue(older,None).status_code,200)
        with listener(self.path,9541,False,"choice",pick_one=True):
            heartbeat(self.path,"fixture","测试账号",session="choice")
            self.assertIsNone(claim_draft(self.path,"fixture",choice_session="choice"))
            self.assertEqual(queue(older,None).status_code,409)
            self.assertEqual(queue(candidates[0],"choice",sha=older["sha"]).status_code,400)
            self.assertEqual(queue(candidates[0],"choice",expected_slot=candidates[0]["slot"]+60).status_code,409)
            self.assertIsNone(self.client.get(url(candidates[0])).json["record"])
            self.assertIsNone(self.client.get(url(older)).json["bridge"]["job_id"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda j:queue(j,"choice",expected_slot=j["slot"]),candidates))
            self.assertCountEqual([r.status_code for r in results],[200,400])
            winner=candidates[next(i for i,r in enumerate(results) if r.status_code==200)]
            self.assertEqual(self.client.get(url(older)).json["bridge"]["job_id"],winner["id"])
            self.assertEqual(queue(winner,"choice").status_code,200)
            self.assertIsNone(claim_draft(self.path,"wrong",choice_session="choice"))
            received=claim_draft(self.path,"fixture",choice_session="choice")
            self.assertEqual((received["job"],received["sha"]),(winner["id"],winner["sha"]))
            self.assertEqual(recover(winner,"choice").status_code,400)
            self.assertIsNone(claim_draft(self.path,"fixture",choice_session="choice"))
        with listener(self.path,9541,False,"next",pick_one=True):
            heartbeat(self.path,"wrong","其他账号",session="next")
            self.assertEqual(recover(winner,"next").status_code,400)
            heartbeat(self.path,"fixture","测试账号",session="next")
            self.assertEqual(recover(winner,"next").status_code,200)
            self.assertEqual(self.client.get(url(winner)).json["record"]["state"],"failed")
            self.assertIsNone(claim_draft(self.path,"fixture",choice_session="next"))
            self.assertEqual(queue(older,"choice").status_code,409)
            self.assertEqual(queue(older,"next").status_code,200)
            self.assertIsNone(claim_draft(self.path,"fixture",choice_session="choice"))
            sending=claim_draft(self.path,"fixture",choice_session="next")
            self.assertEqual(sending["job"],older["id"])
            self.assertTrue(transition(self.path,sending,"preparing","sending"))
        with listener(self.path,9541,False,"recover",pick_one=True):
            heartbeat(self.path,"fixture","测试账号",session="recover")
            self.assertEqual(recover(older,"recover").status_code,200)
            self.assertEqual(self.client.get(url(older)).json["record"]["state"],"unknown")
            self.assertEqual(queue(older,"recover").status_code,400)
            self.assertIsNone(claim_draft(self.path,"fixture",choice_session="recover"))

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
        query=BASE+"article-search"
        def by_recipient(value):
            return self.client.get(query,query_string={"draft_recipient":value,"draft_review":"waiting"}).json["items"]
        self.assertEqual({item["job_id"] for item in by_recipient("fixture-a")},
                         {int(url.split("/")[-2]) for url in (early,frozen)})
        self.assertEqual([item["job_id"] for item in by_recipient("fixture-b")],[int(later.split("/")[-2])])
        self.assertEqual({item["recipient_id"] for item in by_recipient("相同昵称")},{"fixture-a","fixture-b"})
        self.assertFalse(by_recipient("fixture-"))
        external={"Origin":"https://example.invalid"}
        self.assertNotIn("recipient_id",self.client.get(query,headers=external).json["items"][0])
        self.assertEqual(self.client.get(query,query_string={"draft_recipient":"fixture-a"},headers=external).status_code,400)

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
        self.assertEqual(self.client.post(url,json=queued|dict(expected_event=0)).status_code,200)
        first_event=self.client.get(url).json["state_history"][0]["id"]
        task=platform_draft.claim(self.path,uid)
        self.assertEqual(task["job"],a["id"])
        self.client.post(BASE+f"batches/{bid}/pause",json={})
        self.assertFalse(platform_draft.transition(self.path,task,"preparing","sending"))
        self.assertIsNone(platform_draft.claim(self.path,uid))
        self.client.post(BASE+f"batches/{bid}/resume",json={})
        current=self.client.get(url).json["state_history"]
        self.assertEqual([e["state"] for e in current],["queued","preparing","queued"])
        self.assertIn("暂停",current[0]["reason"])
        self.assertEqual(self.client.post(url,json=dict(action="cancel",expected_event=first_event)).status_code,409)
        self.assertEqual(self.client.post(url,json=dict(action="cancel",expected_event=current[0]["id"])).status_code,200)
        self.assertEqual(self.client.post(url,json=queued|dict(expected_event=0)).status_code,409)
        cancelled=self.client.get(url).json["state_history"][0]
        self.assertEqual((cancelled["state"],cancelled["baseline"]),("cancelled",0))
        self.assertEqual(self.client.post(url,json=queued|dict(expected_event=cancelled["id"])).status_code,200)
        self.assertEqual(platform_draft.claim(self.path,uid)["id"],task["id"])
        with transaction(self.path) as db:
            db.execute("UPDATE reprint_reviews SET sha='damaged' WHERE row_id=?",(row["id"],))
        self.assertEqual(self.client.post(url,json=decision|dict(scope_revision=granted,scope="local")).status_code,200)
        with transaction(self.path) as db:
            db.execute("UPDATE reprint_reviews SET sha=? WHERE row_id=?",(row["sha"],row["id"]))
        info=self.client.get(url).json
        self.assertEqual((info["record"]["state"],info["rights"]["scope"]),("cancelled","local"))
        self.assertEqual([e["state"] for e in reversed(info["state_history"])],
                         ["queued","preparing","queued","cancelled","queued","preparing","cancelled"])
        self.assertIn("授权",info["state_history"][0]["reason"])
        self.assertEqual(self.client.post(url,json=decision).status_code,409)
        self.assertEqual(self.client.post(url,json=queued).status_code,400)

        # Simulate a pre-history installation, then restart twice to verify the
        # single retained state is labelled without inventing past attempts.
        with transaction(self.path) as db:
            db.execute("DROP TRIGGER platform_draft_created")
            db.execute("DROP TRIGGER platform_draft_state_changed")
            db.execute("DROP TABLE platform_draft_events")
        for _ in range(2):
            old=create_app(self.path,self.root).test_client().get(url).json["state_history"]
            self.assertEqual([(e["state"],e["baseline"]) for e in old],[("cancelled",1)])
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

    def test_receipt_survives_disconnect_before_account_confirmation(self):
        from . import platform_draft, rights, zhihu_draft_worker

        self.targets=[dict(platform="zhihu",account="模拟")]
        bid=self.create([self.source(65)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        job,=self.batch(bid)["deliveries"]
        uid="fixture-account"
        with transaction(self.path) as db:
            rights.record(db,job["id"],job["sha"],"zhihu_draft",uid,0)
            db.execute("""INSERT INTO platform_drafts(job,canonical,sha,user_id,name,state,updated)
                SELECT ?,canonical,?,?,'模拟','queued',0 FROM rows WHERE id=?""",
                (job["id"],job["sha"],uid,job["row_id"]))
        task=platform_draft.claim(self.path,uid)
        link="https://zhuanlan.zhihu.com/p/65123/edit"
        bridge=Mock()
        bridge.identity.side_effect=[(uid,"模拟"),ConnectionError()]
        bridge.request.return_value=dict(results=[dict(platform="zhihu",draftOnly=True,success=True,postUrl=link)])
        with patch.object(zhihu_draft_worker,"prepare_content",return_value={"title":"测试","content":"测试"}):
            with self.assertRaises(ConnectionError):
                zhihu_draft_worker.run_task(self.path,task,bridge)
        bridge.request.assert_called_once()
        client=create_app(self.path,self.root).test_client()
        url=BASE+f"jobs/{job['id']}/platform-draft"
        info=client.get(url).json
        self.assertEqual((info["record"]["state"],info["record"]["url"],info["unverified_edit_url"]),
                         ("unknown","",link))
        self.assertIsNone(platform_draft.claim(self.path,uid))
        actual="https://zhuanlan.zhihu.com/p/65999/edit"
        checked=client.post(url,json=dict(action="inspect",sha=job["sha"],user_id=uid,url=actual,
            expected_state="unknown",revision=0,checks=dict.fromkeys(("title","layout","images","footer"),"pass")))
        self.assertEqual(checked.status_code,200)
        info=client.get(url).json
        self.assertEqual((info["record"]["state"],info["record"]["url"],info["unverified_edit_url"]),
                         ("verified",actual,None))

    def test_restore_reconciles_drafts_saved_after_snapshot_before_queueing(self):
        from . import backup, platform_draft
        from .draft_bridge import heartbeat
        uid="restore-fixture"
        self.targets=[dict(platform="zhihu",account="模拟")]
        heartbeat(self.path,uid,"模拟")
        bind=self.client.post(BASE+"zhihu-draft-accounts",json=dict(action="bind",alias="模拟",revision=0,user_id=uid))
        self.assertEqual(bind.status_code,200)
        bid=self.create([self.source(i) for i in (81,82,83)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        found,absent,queued=self.batch(bid)["deliveries"]
        def endpoint(job):
            return BASE+f"jobs/{job['id']}/platform-draft"
        def grant(client,job):
            info=client.get(endpoint(job)).json
            result=client.post(endpoint(job),json=dict(action="scope",scope="zhihu_draft",user_id=uid,sha=job["sha"],
                               scope_revision=info["rights"]["revision"],rights_confirmed=True))
            self.assertEqual(result.status_code,200)
            return client.get(endpoint(job)).json["rights"]["revision"]
        def send(job,revision,**extra):
            return dict(action="queue",consent=True,user_id=uid,sha=job["sha"],scope_revision=revision,**extra)
        old={j["id"]:grant(self.client,j) for j in (found,absent,queued)}
        self.assertEqual(self.client.post(endpoint(queued),json=send(queued,old[queued["id"]])).status_code,200)
        bundle=backup.create(self.path,self.root/"backup")
        link="https://zhuanlan.zhihu.com/p/81001/edit"
        self.assertEqual(self.client.post(endpoint(found),json=send(found,old[found["id"]])).status_code,200)
        original=platform_draft.claim(self.path,uid,found["id"])
        platform_draft.transition(self.path,original,"preparing","sending")
        platform_draft.transition(self.path,original,"sending","saved",url=link)
        restored=backup.restore(bundle,self.root/"recovered")/backup.DATABASES[0]
        client=create_app(restored,self.root).test_client()
        self.assertIsNone(platform_draft.claim(restored,uid))
        self.assertEqual(client.get(endpoint(queued)).json["record"]["state"],"unknown")
        info=client.get(endpoint(found)).json
        self.assertEqual(info["rights"]["scope"],"local")
        self.assertIsNone(info["record"])
        generation=info["recovery"]["generation"]
        register=dict(action="recover_existing",checked=True,generation=generation,user_id=uid,sha=found["sha"],url=link)
        self.assertEqual(client.post(endpoint(found),json=register|dict(generation="stale")).status_code,400)
        for _ in range(2):
            self.assertEqual(client.post(endpoint(found),json=register).status_code,200)
        recovered=client.get(endpoint(found)).json
        self.assertEqual(recovered["recovery_review"]["result"],"found")
        self.assertEqual((recovered["record"]["state"],recovered["record"]["url"]),("saved",link))
        self.assertFalse(recovered["permitted"])
        self.assertEqual(client.post(endpoint(absent),json=register|dict(sha=absent["sha"])).status_code,400)
        self.assertIsNone(client.get(endpoint(absent)).json["record"])
        heartbeat(restored,uid,"模拟")
        revision=grant(client,absent)
        self.assertNotEqual(revision,old[absent["id"]])
        payload=send(absent,revision)
        self.assertEqual(client.post(endpoint(absent),json=payload).status_code,400)
        for _ in range(2):
            self.assertEqual(client.post(endpoint(absent),json=payload|dict(generation=generation,checked_absent=True)).status_code,200)
        self.assertEqual(platform_draft.claim(restored,uid)["job"],absent["id"])
        self.assertEqual(client.get(endpoint(absent)).json["recovery_review"]["result"],"absent")
        self.assertIsNone(platform_draft.claim(restored,uid))
        self.assertTrue(self.client.get(endpoint(found)).json["permitted"])
        self.assertEqual(self.client.get(endpoint(queued)).json["record"]["state"],"queued")
        self.assertIsNone(self.client.get(endpoint(absent)).json["recovery"])

    def test_pending_draft_address_cannot_be_claimed_by_two_tasks(self):
        from . import platform_draft, rights, zhihu_draft_worker

        self.targets=[dict(platform="zhihu",account="模拟")]
        bid=self.create([self.source(85),self.source(86)])
        self.prepare()
        self.approve(bid,self.batch(bid)["reviews"])
        uid="fixture-account"
        deliveries=self.batch(bid)["deliveries"]
        with transaction(self.path) as db:
            for job in deliveries:
                db.execute("""INSERT INTO platform_drafts(job,canonical,sha,user_id,name,state,updated)
                    SELECT ?,canonical,?,?,'模拟','sending',0 FROM rows WHERE id=?""",
                    (job["id"],job["sha"],uid,job["row_id"]))
            tasks=[dict(row) for row in db.execute("SELECT * FROM platform_drafts ORDER BY id")]
        link="https://zhuanlan.zhihu.com/p/8586/edit"
        def record(task):
            try:
                platform_draft.remember_receipt(self.path,task,link)
                return True
            except platform_draft.DraftUrlConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(record,tasks))
        self.assertCountEqual(result,[False,True])
        first,other=tasks[result.index(True)],tasks[result.index(False)]
        self.assertEqual(platform_draft.remember_receipt(self.path,first,link),link)
        platform_draft.transition(self.path,first,"sending","unknown")
        with transaction(self.path) as db:
            db.execute("UPDATE platform_drafts SET state='preparing' WHERE id=?",(other["id"],))
            rights.record(db,other["job"],other["sha"],"zhihu_draft",uid,0)
        bridge=Mock()
        bridge.identity.return_value=(uid,"模拟")
        bridge.request.return_value=dict(results=[dict(platform="zhihu",draftOnly=True,success=True,postUrl=link)])
        with patch.object(zhihu_draft_worker,"prepare_content",return_value={"title":"样稿","content":"正文"}):
            zhihu_draft_worker.run_task(self.path,other,bridge)
        bridge.request.assert_called_once()
        self.client=create_app(self.path,self.root).test_client()
        def path(task):
            return BASE+f"jobs/{task['job']}/platform-draft"
        uncertain=self.client.get(path(other)).json["record"]
        self.assertEqual(uncertain["state"],"unknown")
        self.assertIn(f"#{first['job']}",uncertain["reason"])
        def inspect(task,url):
            return dict(action="inspect",sha=task["sha"],user_id=uid,url=url,expected_state="unknown",
                        revision=0,checks=dict.fromkeys(("title","layout","images","footer"),"pass"))
        denied=self.client.post(path(other),json=inspect(other,link))
        self.assertEqual(denied.status_code,400)
        self.assertIn(f"#{first['job']}",denied.json["error"])
        self.assertEqual(self.client.post(path(other),json=dict(action="confirm",checked=True,url=link)).status_code,400)
        dismiss=dict(action="dismiss_receipt",sha=first["sha"],url=link,user_id=uid,checked=True)
        self.assertEqual(self.client.post(path(first),json=dismiss|{"user_id":"other"}).status_code,400)
        for _ in range(2):
            self.assertEqual(self.client.post(path(first),json=dismiss).status_code,200)
        info=self.client.get(path(first)).json
        self.assertEqual(info["record"]["state"],"unknown")
        self.assertIsNone(info["unverified_edit_url"])
        self.assertEqual(info["dismissed_receipt"]["edit_url"],link)
        self.assertEqual(self.client.post(path(first),json=inspect(first,link)).status_code,400)
        self.assertEqual(self.client.post(path(other),json=inspect(other,link)).status_code,200)
        self.assertEqual(self.client.post(path(first),json=inspect(first,link)|
            {"dismissed_at":info["dismissed_receipt"]["dismissed_at"]}).status_code,400)
        actual="https://zhuanlan.zhihu.com/p/85000/edit"
        self.assertEqual(self.client.post(path(first),json=inspect(first,actual)).status_code,200)
        self.assertEqual([self.client.get(path(t)).json["record"]["url"] for t in (first,other)],[actual,link])
        self.assertIsNone(platform_draft.claim(self.path,uid))

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
        self.assertEqual(info["inspections"][0]["notes"],{})
        self.assertEqual(self.client.get(BASE+"article-search?draft_review=issue").json["items"][0]["job_id"],one["id"])
        def matching(field):
            return [r["job_id"] for r in self.client.get(BASE+"article-search?draft_review=issue_"+field).json["items"]]
        self.assertEqual(matching("images"),[one["id"]])
        self.assertEqual(matching("footer"),[])
        updated=initial|dict(expected_state="needs_review",revision=info["inspections"][0]["id"],
                             checks=checks|{"footer":"issue"},notes={"images":"第二张图片空白","footer":"文末地址显示为纯文本"})
        for notes in ({"other":"无此项"},{"title":"与结果不符"},{"images":"字"*301}):
            self.assertEqual(self.client.post(url,json=updated|dict(notes=notes)).status_code,400)
        for _ in range(2):
            self.assertEqual(self.client.post(url,json=updated).status_code,200)
        self.client=create_app(self.path,self.root).test_client()
        info=self.client.get(url).json
        self.assertEqual(len(info["inspections"]),2)
        self.assertEqual(info["inspections"][0]["notes"],updated["notes"])
        self.assertEqual(matching("footer"),[one["id"]])
        self.assertEqual(self.client.post(url,json=updated|{"notes":{"footer":"过期修改"}}).status_code,400)
        # The earlier console used only checks; keep the unresolved footer description.
        partial=initial|dict(expected_state="needs_review",revision=info["inspections"][0]["id"],
                             checks=updated["checks"]|{"images":"pass"})
        self.assertEqual(self.client.post(url,json=partial).status_code,200)
        info=self.client.get(url).json
        self.assertEqual(info["inspections"][0]["notes"],{"footer":updated["notes"]["footer"]})
        self.assertEqual(info["inspections"][1]["notes"]["images"],"第二张图片空白")
        self.assertEqual(matching("images"),[])
        passed=initial|{"checks":dict.fromkeys(checks,"pass")}
        self.assertEqual(self.client.post(url,json=passed).status_code,400)
        guarded=passed|dict(revision=info["inspections"][0]["id"],expected_state="needs_review")
        self.assertEqual(self.client.post(url,json=guarded).status_code,200)
        self.assertEqual(self.client.get(url).json["record"]["state"],"verified")
        self.assertEqual(matching("footer"),[])
        self.assertEqual(self.client.post(BASE+f"jobs/{two['id']}/platform-draft",
                                          json=initial|{"sha":two["sha"]}).status_code,400)
        self.assertEqual(self.client.get(BASE+"article-search?draft_review=issue").json["total"],0)
        # Older records without a pin keep the account recorded at submission.
        updated=self.client.get(url).json["record"]["updated"]
        day=datetime.fromtimestamp(updated,BEIJING).date().isoformat()
        args=dict(draft_recipient="test-account",date_field="draft_updated",start=day,end=day)
        row,=self.client.get(BASE+"article-search",query_string=args).json["items"]
        self.assertEqual((row["job_id"],row["recipient_id"],row["draft_updated"]),(one["id"],"test-account",updated))
        uncertain=self.client.get(BASE+"article-search",query_string=dict(draft_review="uncertain")).json["items"]
        self.assertEqual([r["job_id"] for r in uncertain],[two["id"]])

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

    def test_export_changing_during_read_retries_only_that_article(self):
        from .library import parse_folders

        first=self.source(91)
        bid=self.create([first,self.source(92)])
        now=int(time.time())
        def edit_after_read(parts):
            items=parse_folders(parts)
            with (self.root/first/"正文.md").open("a",encoding="utf-8") as stream:
                stream.write("\n下载完成后的末段。")
            return items

        with patch("distribution.library.parse_folders",side_effect=edit_after_read):
            self.assertTrue(tick(self.path,now,preparation=True))
        self.assertTrue(tick(self.path,now,preparation=True))
        waiting,peer=self.batch(bid)["reviews"]
        self.assertEqual((waiting["state"],waiting["sha"],peer["state"]),("preparing","","pending"))
        self.assertIn("发生变化",waiting["note"])
        due=waiting["prepare_retry_at"]
        self.assertGreaterEqual(due,now+5)
        self.assertFalse(tick(self.path,due-1,preparation=True))
        self.assertTrue(tick(self.path,due,preparation=True))
        ready,unchanged=self.batch(bid)["reviews"]
        self.assertEqual((ready["state"],ready["prepare_attempts"],ready["prepare_retry_at"]),("pending",2,None))
        self.assertEqual(unchanged,peer)
        preview=self.client.get(BASE+f"reprints/{bid}/{ready['id']}?platform=zhihu").json
        self.assertIn("下载完成后的末段",preview["snapshot"]["text"])

    def test_body_citation_cannot_reuse_the_cited_articles_delivery(self):
        from .library import read_folder
        original,missing,valid=(self.source(i) for i in (81,82,83))
        prior=self.create([original],"cited")
        self.prepare()
        self.approve(prior,self.batch(prior)["reviews"])
        cited="https://mp.weixin.qq.com/s/test-export-81"
        actual="https://mp.weixin.qq.com/s/test-export-83"
        body=f"这段引言需要保留。\n\n原文地址：{cited}\n\n![配图](img/0.png)"
        (self.root/missing/"正文.md").write_text("正文中的参考资料\n====\n\n"+body,encoding="utf-8")
        # A link label can itself be a URL. Only its actual href identifies this article.
        (self.root/valid/"正文.md").write_text(f"# 认识 C#\n\n非原创 公号:测试号 发布时间:2026-01-01 08:00\n\n原文地址：[{cited}]({actual} \"阅读\")\n\n"+body,encoding="utf-8")
        catalog={r["directory"]:r for r in self.client.get(BASE+"library").json["items"]}
        self.assertIsNone(catalog[missing]["source"])
        self.assertEqual(catalog[missing]["deliveries"],{})
        self.assertTrue(catalog[missing]["selection_key"].startswith("file-"))
        self.assertEqual((catalog[valid]["title"],catalog[valid]["source"]),("认识 C#",actual))
        for directory in (missing,valid):
            item,_=read_folder(self.root,directory)
            self.assertEqual(item["doc"]["markdown_content"],body)
        bid=self.create([missing,valid],"cites-another-article")
        self.prepare()
        unresolved,ready=self.batch(bid)["reviews"]
        self.assertEqual((unresolved["state"],unresolved["assignments"],ready["state"]),("invalid",[],"pending"))

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
        route=BASE+f"reprints/{bid}/"
        items=[{k:r[k] for k in ("id","revision","sha")} | {"expected_prior":{}} for r in rows]
        forecast=self.client.post(route+"plan",json=dict(items=items,start_at=int(time.time())+86400)).json["items"]
        for item,plan in zip(items,forecast,strict=True):
            item["expected_accounts"]={a["platform"]:a["account"] for a in plan["assignments"]}
            item["expected_slots"]={a["platform"]:a["slot"] for a in plan["assignments"]}
        payload=dict(approved=True,background=True,items=items)
        result=self.client.post(route+"approve",json=payload).json["items"]
        self.assertEqual(Counter(x.get("state") for x in result),{"scheduling":100})
        self.client=create_app(self.path,self.root).test_client()
        self.assertEqual(self.client.post(route+"approve",json=payload).json["items"],result)
        while tick(self.path,preparation=False):
            pass
        data = self.batch(bid)
        self.assertEqual(data["counts"], {"processing":100})
        jobs = data["deliveries"]
        self.assertEqual(len(jobs), 200)
        self.assertEqual({(j["row_id"],j["platform"],j["account"],j["slot"]) for j in jobs},
                         {(r["id"],a["platform"],a["account"],a["slot"]) for r in forecast for a in r["assignments"]})
        start=max(j["slot"] for j in jobs)+2*86400
        route=BASE+f"batches/{bid}/postpone/"
        preview=self.client.post(route+"preview",json=dict(start=start,items=[dict(id=j["id"]) for j in jobs])).json["items"]
        confirmed=self.client.post(route+"confirm",json=dict(start=start,items=preview)).json["items"]
        self.assertEqual(confirmed,preview)
        planned={p["id"]:p["time"] for p in preview}
        self.assertGreaterEqual(min(planned.values()),start)
        after=self.batch(bid)["deliveries"]
        self.assertEqual({j["id"]:(j["account"],j["sha"],planned[j["id"]]) for j in jobs},
                         {j["id"]:(j["account"],j["sha"],j["slot"]) for j in after})
        jobs=after
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

