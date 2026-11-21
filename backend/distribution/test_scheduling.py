"""Regression cases for per-account windows, mixed batch quotas and Beijing calendar dates."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from .api import create_app
from .scheduling import BEIJING, available_slot
from .store import clean_rules, compact, transaction


def timestamp(day, clock):
    return int(datetime.fromisoformat(day+"T"+clock).replace(tzinfo=BEIJING).timestamp())


DAY="2026-11-21"
TARGETS=[dict(platform="zhihu",account=a) for a in ("甲","乙")]


class ScopedScheduling(unittest.TestCase):
    def test_minute_window_and_legacy_fingerprint(self):
        base={"gap":5}
        self.assertEqual(clean_rules(base),clean_rules(base|{"overrides":[]},TARGETS))
        rules=clean_rules(base|{"overrides":[{"platform":"zhihu","start":"08:00","end":"14:00"},
                                             {"platform":"zhihu","account":"甲","start":"11:15","end":"12:30"}]},TARGETS)
        for account,clock in (("甲","11:15"),("乙","08:00")):
            self.assertEqual(available_slot(timestamp(DAY,"07:00"),"zhihu",account,rules,[]),timestamp(DAY,clock))
        with self.assertRaises(ValueError):
            clean_rules({"overrides":[{"platform":"zhihu","end":"09:00"},{"platform":"zhihu","account":"甲","start":"10:00"}]},TARGETS)

    def test_previous_account_gap_after_midnight_and_other_account(self):
        earlier=clean_rules({"gap":30,"overrides":[{"platform":"zhihu","account":"甲","gap":7200}]},TARGETS)
        booking=dict(mark=timestamp("2026-11-20","23:45"),account="甲",rules=compact(earlier))
        later=clean_rules({"gap":5})
        start=timestamp(DAY,"00:00")
        self.assertEqual(available_slot(start,"zhihu","甲",later,[booking]),timestamp(DAY,"01:45"))
        self.assertEqual(available_slot(start,"zhihu","乙",later,[booking]),start)

    def test_earlier_strict_platform_quota_survives_new_batch(self):
        booking=dict(mark=timestamp(DAY,"17:00"),account="乙",rules=compact(clean_rules({"platform_cap":1})))
        later=clean_rules({"start_hour":9,"platform_cap":20})
        self.assertEqual(available_slot(timestamp(DAY,"09:00"),"zhihu","甲",later,[booking]),
                         timestamp("2026-11-22","09:00"))

    def test_calendar_drilldown_matches_scheduled_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"calendar.sqlite"
            client=create_app(path).test_client()
            stamp=timestamp(DAY,"00:00")
            with transaction(path) as db:
                db.execute("INSERT INTO batches VALUES('b','key','digest',?,'paused',?,'{}')",(stamp,compact(TARGETS)))
                rid=db.execute("INSERT INTO rows(batch,line,url,kind) VALUES('b',1,'fixture','article')").lastrowid
                for who,slot in (("甲",stamp),("乙",stamp-1)):
                    db.execute("INSERT INTO jobs(row_id,stage,platform,account,due,slot) VALUES(?,'publish','zhihu',?,?,?)",(rid,who,slot,slot))
            root="/api/distribution/"
            data=client.get(root+"schedule-calendar",query_string=dict(month="2026-11",platform="zhihu",account="甲")).json
            self.assertEqual(data["days"],{DAY:{"paused":1}})
            search=client.get(root+"article-search",query_string=dict(start=DAY,end=DAY,platform="zhihu",account="甲")).json
            self.assertEqual(search["total"],sum(data["days"][DAY].values()))
