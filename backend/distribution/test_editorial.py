import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from .api import create_app
from . import editorial
from .editorial import tick_editorial
from .store import BEIJING, transaction
from .test_workflow import drain, export_folder, options

BASE="/api/distribution/"
POOLS=[{"platform":p,"account":a} for p in ("zhihu","toutiao") for a in ("主号","副号")]

class EditorialTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=self.root/"test.sqlite"
        self.client=create_app(self.db,self.root).test_client()

    def post(self,path,data):
        res=self.client.post(BASE+path,json=data)
        self.assertLess(res.status_code,300,res.json)
        return res.json

    def local(self,n=3):
        dirs=[export_folder(self.root,i).relative_to(self.root).as_posix() for i in range(n)]
        return self.post("library",dict(request_id="test",targets=POOLS,account_routing="rotate",
                                        directories=dirs,rules={"account_cap":1,"platform_cap":2}))["id"]

    def test_rotate_quotas_duplicate_and_cross_batch(self):
        bid=self.local()
        drain(self.db,2000000000)
        b=self.client.get(BASE+"batches/"+bid).json
        self.assertEqual((b["expected"],b["simulated"],b["counts"]["done"]),(6,6,3))
        for p in ("toutiao","zhihu"):
            jobs=sorted((j for j in b["publishes"] if j["platform"]==p),key=lambda j:j["row_id"])
            self.assertEqual([j["account"] for j in jobs],["主号","副号","主号"])
            days=[datetime.fromtimestamp(j["finished"],BEIJING).date() for j in jobs]
            self.assertEqual(days[0],days[1]);self.assertLess(days[1],days[2])
        data=options(1,targets=POOLS);data["account_routing"]="rotate"
        bid=self.post("batches",data)["id"];self.assertEqual(self.post("batches",data)["id"],bid)
        drain(self.db,2000000000)
        self.assertEqual({j["account"] for j in self.client.get(BASE+"batches/"+bid).json["publishes"]},{"副号"})

    def test_freeze_stale_preview_and_zip(self):
        bid=self.local(1);drain(self.db,2000000000)
        rid=self.client.get(BASE+"batches/"+bid).json["rows"][0]["id"]
        path=f"articles/{rid}/editorial"
        target=POOLS[0]
        did=self.post(path,target|dict(action="agent",title="测试标题",body="这是待核对的正文。"))["id"]
        edit=dict(edit_no=0,title="测试标题",body="这是待核对的正文。")
        dpath=f"editorial/{did}"
        self.assertEqual(self.client.get(BASE+dpath+"/export").status_code,400)
        self.post(dpath,edit|dict(action="save"))
        self.assertEqual(self.client.post(BASE+dpath,json=edit|dict(action="save")).status_code,400)
        edit["edit_no"]=1
        first=self.post(dpath,edit|dict(action="preview"))
        link=dict(mode="short",destination="https://mp.weixin.qq.com/s/guide",short="https://example.invalid/go",label="读者指南",verified=True)
        self.post(path,target|dict(action="link",link=link))
        approve=edit|dict(action="approve",rights=True,facts=True,preview=first)
        self.assertEqual(self.client.post(BASE+dpath,json=approve).status_code,400)
        approve["preview"]=self.post(dpath,edit|dict(action="preview"))
        self.post(dpath,approve)
        self.post(path,target|dict(action="link",link={"mode":"omit"}))
        self.assertEqual(self.client.post(BASE+dpath,json=approve).status_code,400)
        res=self.client.get(BASE+dpath+"/export")
        with zipfile.ZipFile(io.BytesIO(res.data)) as z:
            self.assertEqual(z.testzip(),None)
            self.assertIn("images/1.png",z.namelist())
            self.assertEqual(z.read("article.md").decode().count(link["short"]),1)
            self.assertIn(b'<a href=',z.read("article.html"))
            m=json.loads(z.read("metadata.json"))
            self.assertNotEqual(m["source_url"],link["destination"])
            self.assertEqual(m["link"],link)
        self.assertNotEqual(self.post(path,target|dict(action="manual"))["id"],did)

    @patch.dict("os.environ",{"DEEPSEEK_API_KEY":"test-only","DIST_AI_MODEL":"test-only"})
    def test_model_queue_and_failure_isolation(self):
        bid=self.local()
        path="batches/"+bid+"/editorial"
        self.assertEqual(self.client.post(BASE+path,json={}).status_code,400)
        self.assertEqual(self.post(path,{"authorized":True})["created"],6)
        self.assertEqual(self.post(path,{"authorized":True})["created"],0)
        drain(self.db,2000000000)
        self.post("batches/"+bid+"/pause",{})
        self.assertFalse(tick_editorial(self.db))
        self.post("batches/"+bid+"/resume",{})
        with patch("distribution.editorial.generate",side_effect=[ValueError("模拟模型失败")]+[("标题","正文")]*5) as g:
            for _ in range(6):
                self.assertTrue(tick_editorial(self.db))
            self.assertFalse(tick_editorial(self.db))
            self.assertEqual(g.call_count,6)
        self.assertEqual(self.client.get(BASE+"batches/"+bid).json["editorial"],{"failed":1,"editing":5})

    @patch.dict("os.environ",{"DEEPSEEK_API_KEY":"test-only","DIST_AI_MODEL":"original-model"})
    def test_model_rate_limit_survives_restart_and_keeps_authorized_model(self):
        bid=self.local(1)
        self.post("batches/"+bid+"/editorial",{"authorized":True})
        now=2000000000
        drain(self.db,now)
        with patch.dict("os.environ",{"DEEPSEEK_API_KEY":""}):
            self.assertFalse(tick_editorial(self.db))
        responses=[editorial.ModelRateLimited("90"),editorial.ModelRateLimited(None),
                   editorial.ModelRateLimited(None),("标题","正文")]
        with patch.object(editorial,"generate",side_effect=responses) as generate, \
                patch.dict("os.environ",{"DIST_AI_MODEL":"later-default"}),patch.object(editorial.time,"time") as clock:
            for attempt,delay in enumerate((90,60,120),1):
                clock.return_value=now
                self.assertTrue(tick_editorial(self.db))
                self.client=create_app(self.db,self.root).test_client()
                with transaction(self.db,False) as db:
                    row=db.execute("SELECT rejections,retry_at FROM model_backoff").fetchone()
                    self.assertEqual(tuple(row),(attempt,now+delay))
                clock.return_value=now+delay-1
                self.assertFalse(tick_editorial(self.db))
                self.assertEqual(generate.call_count,attempt)
                now+=delay
            clock.return_value=now
            self.assertTrue(tick_editorial(self.db))
            self.assertEqual([call.args[2] for call in generate.call_args_list],["original-model"]*4)
        self.assertEqual(self.client.get(BASE+"batches/"+bid).json["editorial"],{"failed":1,"editing":1})

