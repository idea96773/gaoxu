"""Daily backup recovery and retention using two disposable SQLite databases."""
import tempfile
import unittest
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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
