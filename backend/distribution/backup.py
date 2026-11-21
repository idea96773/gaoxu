"""Consistent portable backup of the article database and the separate simulator ledger."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path

from .delivery import bound_snapshot, confirmed_receipt
from .editorial import export_image
from .store import BEIJING, DEFAULT_DB, delivery_key, ledger_path, operation_key

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


def verify_pair(main,ledger):
    """Read two snapshots from a completed bundle or an offline data directory."""
    totals=dict(publish_receipts=0,delivery_receipts=0,pending_reconciliation=0,frozen_tasks=0,images=0)
    checked_images=set()
    matched={"publish":set(),"deliver":set()}
    with closing(database(main,"ro")) as db, closing(database(ledger,"ro")) as remote:
        db.row_factory=remote.row_factory=sqlite3.Row
        db.execute("BEGIN")
        remote.execute("BEGIN")
        jobs=db.execute("""SELECT j.*,r.canonical FROM jobs j JOIN rows r ON r.id=j.row_id
            WHERE j.stage IN ('publish','deliver') ORDER BY j.id""")
        for job in jobs:
            jid=job["id"]
            if job["stage"]=="deliver":
                try:
                    bound=bound_snapshot(db,job)
                    check_frozen_images(db,bound["snapshot"],checked_images)
                    # A cancelled job may share a business key with a later
                    # replacement. Its receipt belongs to the replacement draft.
                    owner=remote.execute("SELECT draft FROM delivered_drafts WHERE key=?",(delivery_key(job),)).fetchone()
                    receipt=confirmed_receipt(remote,db,job) if owner and owner["draft"]==bound["draft"] else None
                except ValueError as exc:
                    raise ValueError(f"冻结稿任务 #{jid}：{exc}") from exc
                totals["frozen_tasks"]+=1
                counter="delivery_receipts"
            else:
                receipt=remote.execute("SELECT remote_id,submitted FROM receipts WHERE key=?",(operation_key(job),)).fetchone()
                counter="publish_receipts"
            if receipt and (not receipt["remote_id"] or type(receipt["submitted"]) is not int):
                raise ValueError(f"任务 #{jid} 的模拟接收回执不完整")
            if job["state"]=="done":
                if (not receipt or not job["receipt"] or receipt["remote_id"]!=job["receipt"]
                        or receipt["submitted"]!=job["finished"]):
                    raise ValueError(f"任务 #{jid} 的完成记录与模拟接收账本不一致")
            elif job["receipt"]:
                raise ValueError(f"任务 #{jid} 尚未完成但已有主库回执")
            elif receipt:
                if job["state"] not in ("ready","running","failed") or job["attempts"]<1:
                    raise ValueError(f"任务 #{jid} 有接收回执但没有可恢复的发送记录")
                totals["pending_reconciliation"]+=1
            if receipt:
                if receipt["remote_id"] in matched[job["stage"]]:
                    raise ValueError(f"任务 #{jid} 与其他任务引用同一笔模拟回执")
                matched[job["stage"]].add(receipt["remote_id"])
                totals[counter]+=1
        for table,counter in (("receipts","publish_receipts"),("delivered_drafts","delivery_receipts")):
            if remote.execute(f"SELECT count(*) FROM {table}").fetchone()[0]!=totals[counter]:
                raise ValueError("模拟接收账本中的回执无法与主库任务逐笔对应")
        if db.execute("SELECT count(*) FROM frozen_deliveries").fetchone()[0]!=totals["frozen_tasks"]:
            raise ValueError("备份包含未关联到交付任务的冻结稿排期")
    totals["images"]=len(checked_images)
    return totals


def check_frozen_images(db,snapshot,checked):
    view=json.loads(snapshot)
    if not isinstance(view,dict) or not isinstance(view.get("images"),list):
        raise ValueError("冻结稿缺少配图清单")
    files=view.get("files",{})
    if not isinstance(files,dict):
        raise ValueError("冻结稿配图文件清单无效")
    for ref in (*view["images"],*files.values()):
        if not isinstance(ref,str) or not re.fullmatch(r"/api/distribution/media/[a-f0-9]{64}",ref):
            raise ValueError("冻结稿含无法核对的归档配图地址")
        if ref not in checked:
            export_image(db,ref)
            checked.add(ref)


def verify_contents(folder):
    for file in DATABASES:
        inspect(folder/file)
    return verify_pair(*(folder/file for file in DATABASES))


def read_manifest(bundle):
    manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest,dict) or manifest.get("format")!=1
            or not isinstance(manifest.get("sha256"),dict) or set(manifest["sha256"])!=set(DATABASES)):
        raise ValueError("不支持的备份清单")
    return manifest


def verify(bundle):
    manifest=read_manifest(bundle)
    for name in DATABASES:
        source=bundle/name
        if not source.is_file() or source.is_symlink() or checksum(source)!=manifest["sha256"][name]:
            raise ValueError("备份文件缺失或摘要不一致")
    return verify_contents(bundle)


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
        verify_contents(bundle)
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
    manifest=read_manifest(bundle)
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
        verify_contents(result)
        # Saved-but-unconfirmed receipts require a human check after a restore.
        # A backup can predate a subsequent external write: never replay live drafts.
        with closing(database(result/DATABASES[0],"rw")) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='backup_policy'").fetchone():
                db.execute("DELETE FROM backup_policy")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='zhihu_bridge_runtime'").fetchone():
                db.execute("DELETE FROM zhihu_bridge_runtime")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='zhihu_bridge_scope'").fetchone():
                db.execute("DELETE FROM zhihu_bridge_scope")
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
    check=actions.add_parser("verify")
    check.add_argument("bundle",type=Path)
    recover=actions.add_parser("restore")
    recover.add_argument("bundle",type=Path)
    recover.add_argument("destination",type=Path)
    args=parser.parse_args()
    if args.action=="verify":
        print(json.dumps(verify(args.bundle)))
    elif args.action=="create":
        print(create(args.db,args.output))
    else:
        print(restore(args.bundle,args.destination))
