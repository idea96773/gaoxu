"""Consistent portable backup of the article database and the separate simulator ledger."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path

from .store import BEIJING, DEFAULT_DB, ledger_path

DATABASES=("dry-run.sqlite","dry-run.sqlite.simulator")


def database(file,mode):
    return sqlite3.connect(file.resolve().as_uri()+"?mode="+mode,uri=True,timeout=20,isolation_level=None)


def checksum(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def inspect(file):
    with closing(database(file,"ro")) as db:
        if db.execute("PRAGMA quick_check").fetchall()!=[("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份数据库一致性检查失败")
        required={"batches","rows","jobs","frozen_deliveries","media"} if file.name==DATABASES[0] else {"receipts","delivered_drafts"}
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables):
            raise ValueError("备份缺少稿序数据表")


def staging(parent):
    parent.mkdir(parents=True,exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=".gaoxu-",dir=parent)


def create(source,output,*,daily=None):
    source=source.resolve()
    paths=(source,Path(ledger_path(source)))
    with staging(output) as temp:
        bundle=Path(temp)/"bundle"
        bundle.mkdir()
        with ExitStack() as readers:
            snapshots=[]
            # The worker locks the main database before recording a remote receipt.
            # Pin both read snapshots while briefly holding that same writer order.
            with ExitStack() as locks:
                for file in paths:
                    writer=locks.enter_context(closing(database(file,"rw")))
                    writer.execute("BEGIN IMMEDIATE")
                    reader=readers.enter_context(closing(database(file,"ro")))
                    reader.execute("BEGIN")
                    reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    snapshots.append(reader)
            for name,reader in reversed(list(zip(DATABASES,snapshots))):
                with closing(sqlite3.connect(bundle/name)) as target:
                    reader.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
                reader.rollback()
                inspect(bundle/name)
        now=datetime.now(BEIJING)
        manifest={"format":1,"created":now.isoformat(),"sha256":{n:checksum(bundle/n) for n in DATABASES}}
        if daily:
            manifest["daily"]=daily
        (bundle/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
        final=output/(daily["day"] if daily else now.strftime("gaoxu-%Y%m%d-%H%M%S-%f"))
        if final.exists():
            raise ValueError("备份目标已存在，请核对目录")
        bundle.rename(final)
        return final.resolve()


def restore(bundle,destination):
    if destination.exists():
        raise ValueError("恢复位置必须是新目录，以保留当前运行数据")
    manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format")!=1 or set(manifest.get("sha256",{}))!=set(DATABASES):
        raise ValueError("不支持的备份清单")
    with staging(destination.parent) as temp:
        result=Path(temp)/"restored"
        result.mkdir()
        for name in DATABASES:
            source=bundle/name
            if not source.is_file() or source.is_symlink():
                raise ValueError("缺少备份文件")
            copy=result/name
            shutil.copyfile(source,copy)
            if checksum(copy)!=manifest["sha256"][name]:
                raise ValueError("备份文件校验失败")
            inspect(copy)
        # Saved-but-unconfirmed receipts require a human check after a restore.
        # A backup can predate a subsequent external write: never replay live drafts.
        with closing(database(result/DATABASES[0],"rw")) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='backup_policy'").fetchone():
                db.execute("DELETE FROM backup_policy")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='platform_drafts'").fetchone():
                db.execute("BEGIN IMMEDIATE")
                db.execute("""UPDATE platform_drafts SET state='unknown',reason='从备份恢复，请先核对知乎草稿箱'
                    WHERE state IN ('queued','preparing','sending','failed','cancelled')""")
                db.execute("DELETE FROM draft_bridge")
                db.commit()
        result.rename(destination)
    return destination.resolve()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="稿序本机双库备份与隔离恢复")
    actions=parser.add_subparsers(dest="action",required=True)
    save=actions.add_parser("create")
    save.add_argument("--db",type=Path,default=Path(os.getenv("DIST_DB",str(DEFAULT_DB))))
    save.add_argument("--output",type=Path,required=True)
    recover=actions.add_parser("restore")
    recover.add_argument("bundle",type=Path)
    recover.add_argument("destination",type=Path)
    args=parser.parse_args()
    print(create(args.db,args.output) if args.action=="create" else restore(args.bundle,args.destination))
