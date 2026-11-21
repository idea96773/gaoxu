"""Cross-batch lookup with dated historical rows in an isolated simulation database."""
import tempfile
import unittest
from pathlib import Path

from .api import create_app
from .search import day_start
from .store import compact, transaction


class ArticleSearch(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path=Path(temp.name)/"sim.sqlite"
        self.client=create_app(path).test_client()
        midnight=day_start("2026-11-21")
        targets=compact([{"platform":"zhihu","account":"甲"},{"platform":"toutiao","account":"甲"}])
        with transaction(path) as db:
            for i in range(202):
                db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?)",
                           (str(i),str(i),"test",midnight+i,"active",targets,"{}"))
            for rid in (1,2):
                db.execute("INSERT INTO rows(id,batch,line,url,kind) VALUES(?,?,1,'fixture','article')",
                           (rid,str(rid-1)))
                db.execute("INSERT INTO documents(row_id,payload) VALUES(?,?)",
                           (rid,compact({"title":"旧稿 50%_"+str(rid)})))
            for platform,delta,state in (("zhihu",-1,"done"),("toutiao",0,"cancelled")):
                done=state=="done"
                db.execute("INSERT INTO jobs(row_id,stage,platform,account,due,slot,state,finished,receipt) "
                           "VALUES(1,'publish',?,'甲',0,?,?,?,?)",
                           (platform,midnight+delta,state,midnight if done else None,"mock" if done else None))
            db.execute("INSERT INTO variants VALUES(1,'zhihu',1,?,1)",(compact({"title":"定稿甲版"}),))
            db.execute("INSERT INTO row_targets VALUES(2,?)",
                       (compact([{"platform":"zhihu","account":"乙"}]),))

    def query(self, **args):
        response=self.client.get("/api/distribution/article-search",query_string=args)
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def test_cross_batch_titles_assignment_and_literal_search(self):
        rows=self.query(title="50%_")["items"]
        self.assertEqual({r["batch"] for r in rows},{"0","1"})
        self.assertEqual(len(rows),3)
        self.assertEqual(self.query(title="50%%")["total"],0)
        self.assertEqual(self.query(title="定稿甲",platform="zhihu",account="甲")["total"],1)
        row,=self.query(account="乙")["items"]
        self.assertEqual((row["platform"],row["state"],row["slot"]),("zhihu","unscheduled",None))
        self.assertEqual(self.query(platform="toutiao",account="乙")["total"],0)

    def test_beijing_day_cutover(self):
        window={"start":"2026-11-21","end":"2026-11-21"}
        row,=self.query(**window)["items"]
        self.assertEqual((row["state"],row["received"]),("cancelled",None))
        row,=self.query(date_field="delivered",**window)["items"]
        self.assertEqual((row["state"],row["edition"]),("done","定稿甲版"))
        self.assertEqual(self.query(date_field="imported",**window)["total"],3)
        for args in (dict(start="2026-02-30"),dict(start="2026-11-22",end="2026-11-21"),dict(page=-1)):
            self.assertEqual(self.client.get("/api/distribution/article-search",query_string=args).status_code,400)

