"""Daily backup recovery and retention using two disposable SQLite databases."""
import tempfile
import unittest
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from . import daily_backup
from .backup import restore
from .store import BEIJING, initialize, transaction


class DailyBackup(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base=Path(temp.name)
        environment=patch.dict(os.environ,{"DIST_BACKUP_DIR":str(self.base/"archive")})
        environment.start()
        self.addCleanup(environment.stop)
        self.db=self.base/"state"/"dry-run.sqlite"
        initialize(self.db)
        self.start=int(datetime(2026,8,25,9,tzinfo=BEIJING).timestamp())
        with patch("distribution.daily_backup.time.time",return_value=self.start):
            daily_backup.configure(self.db,dict(enabled=True,minute=10*60,revision=0))

    def state(self,path=None):
        with transaction(path or self.db,False) as db:
            return daily_backup.status(db,path or self.db)

    def test_reuse_completed_backup_after_interruption_and_disable_on_restore(self):
        daily_backup.tick(self.db,self.start)
        self.assertEqual(self.state()["state"],"waiting")
        due=self.start+3600
        daily_backup.tick(self.db,due)
        folder=Path(self.state()["last_bundle"])
        self.assertTrue(folder.is_dir())
        with transaction(self.db) as db:
            db.execute("UPDATE backup_policy SET state='running',last_success=NULL")
        with patch("distribution.daily_backup.create",side_effect=AssertionError("duplicate")):
            daily_backup.tick(self.db,due+60)
        self.assertEqual(self.state()["state"],"success")
        copy=restore(folder,self.base/"restored")
        self.assertFalse(self.state(copy/"dry-run.sqlite")["enabled"])

    def test_retention_preserves_unrecognized_files_and_retries_are_bounded(self):
        due=self.start+3600
        daily_backup.tick(self.db,due)
        first=Path(self.state()["last_bundle"])
        extra=first/"operator-note.txt"
        extra.write_text("preserve",encoding="utf-8")
        for day in range(1,9):
            daily_backup.tick(self.db,due+86400*day)
        self.assertTrue(extra.exists())
        self.assertFalse((first.parent/"2026-08-26").exists())
        self.assertEqual(len(list(first.parent.iterdir())),8)
        day=due+9*86400
        with patch("distribution.daily_backup.snapshot",side_effect=OSError("disk offline")) as failed:
            for delta in (0,60,900,2700,3600):
                daily_backup.tick(self.db,day+delta)
            self.assertEqual(failed.call_count,3)
        self.assertEqual(self.state()["state"],"failed")
        self.assertTrue(Path(self.state()["last_bundle"]).is_dir())

    def test_manual_snapshot_survives_crash_and_is_not_replayed_on_restore(self):
        daily_backup.configure(self.db,dict(enabled=False,minute=600,revision=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests=list(pool.map(lambda _:daily_backup.request_manual(self.db,0),range(2)))
        self.assertEqual(requests[0],requests[1])
        job=requests[0]["id"]
        self.assertEqual(daily_backup.request_manual(self.db,job)["state"],"queued")
        create=daily_backup.create
        def interrupted(*args,**kwargs):
            create(*args,**kwargs)
            raise SystemExit("simulated process exit after final rename")
        with patch.object(daily_backup,"create",side_effect=interrupted),self.assertRaises(SystemExit):
            daily_backup.tick_manual(self.db)
        self.assertEqual(self.state()["manual"]["state"],"running")
        with patch.object(daily_backup,"create",side_effect=AssertionError("duplicate snapshot")):
            daily_backup.tick_manual(self.db)
        report=self.state()
        self.assertFalse(report["enabled"])
        self.assertEqual(report["manual"]["state"],"success")
        self.assertEqual(daily_backup.request_manual(self.db,0),report["manual"])
        bundle=Path(report["manual"]["last_bundle"])
        self.assertEqual(list(bundle.parent.iterdir()),[bundle])
        copy=restore(bundle,self.base/"restored")/"dry-run.sqlite"
        self.assertEqual(self.state(copy)["manual"]["state"],"idle")
        daily_backup.tick_manual(copy)
        self.assertEqual(list(bundle.parent.iterdir()),[bundle])

    def test_manual_failure_can_retry_without_losing_last_good_snapshot(self):
        first=daily_backup.request_manual(self.db,0)
        daily_backup.tick_manual(self.db)
        good=self.state()["manual"]["last_bundle"]
        second=daily_backup.request_manual(self.db,first["id"])
        with patch.object(daily_backup,"check_space",side_effect=ValueError("备份磁盘可用空间不足")):
            daily_backup.tick_manual(self.db)
        failed=self.state()["manual"]
        self.assertEqual((failed["state"],failed["message"],failed["last_bundle"]),("failed","备份磁盘可用空间不足",good))
        retry=daily_backup.request_manual(self.db,second["id"])
        self.assertGreater(retry["id"],second["id"])
        daily_backup.tick_manual(self.db)
        self.assertNotEqual(self.state()["manual"]["last_bundle"],good)
        self.assertTrue(Path(good).is_dir())
