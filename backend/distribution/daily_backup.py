"""Opt-in daily snapshots managed by the standalone scheduler."""
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from .backup import DATABASES, checksum, create, database, verify_contents
from .store import BEIJING, ledger_path, transaction

KEEP = 7


def destination(path):
    return Path(os.environ.get("DIST_BACKUP_DIR") or Path(path).resolve().parent/"backups")


def status(db,path):
    row=db.execute("SELECT * FROM backup_policy WHERE singleton=1").fetchone()
    if not row:
        return dict(enabled=False,minute=180,revision=0,state="waiting",destination=str(destination(path)),
                    configured_destination=str(destination(path)),keep=KEEP)
    return {k:row[k] for k in ("enabled","minute","revision","state","destination","day","attempts",
                               "retry_at","last_success","last_bundle","message")}|{
                                   "keep":KEEP,"configured_destination":str(destination(path))}


def configure(path,data):
    enabled,minute,revision=(data.get(k) for k in ("enabled","minute","revision"))
    if type(enabled) is not bool or type(minute) is not int or not 0<=minute<1440 or type(revision) is not int:
        raise ValueError("请选择有效的备份开关和北京时间")
    with transaction(path) as db:
        old=db.execute("SELECT * FROM backup_policy WHERE singleton=1").fetchone()
        if revision!=(old["revision"] if old else 0):
            raise ValueError("备份设置已变更，请重新读取")
        output=destination(path)
        if enabled and not output.is_absolute():
            raise ValueError("DIST_BACKUP_DIR 须为绝对路径")
        if old and (bool(old["enabled"]),old["minute"],old["destination"])==(enabled,minute,str(output)):
            return status(db,path)
        db.execute("""INSERT INTO backup_policy(singleton,owner,revision,enabled,minute,destination,since)
            VALUES(1,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,
            enabled=excluded.enabled,minute=excluded.minute,destination=excluded.destination,since=excluded.since,
            day='',state='waiting',attempts=0,retry_at=0,message=''""",
            (uuid.uuid4().hex,revision+1,enabled,minute,str(output),int(time.time())))
        return status(db,path)


def plain(path):
    info=path.lstat()
    return not (stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&stat.FILE_ATTRIBUTE_REPARSE_POINT)


def verify_bundle(folder,owner):
    names=(*DATABASES,"manifest.json")
    if not plain(folder) or {p.name for p in folder.iterdir()}!=set(names):
        raise ValueError("自动备份目录含未识别的文件")
    for name in names:
        if not plain(folder/name) or not (folder/name).is_file():
            raise ValueError("自动备份文件类型无效")
    manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest,dict) or manifest.get("format")!=1
            or manifest.get("daily")!={"owner":owner,"day":folder.name}
            or not isinstance(manifest.get("sha256"),dict) or set(manifest["sha256"])!=set(DATABASES)):
        raise ValueError("自动备份清单与当前实例不一致")
    for name in DATABASES:
        if checksum(folder/name)!=manifest["sha256"][name]:
            raise ValueError("自动备份文件校验失败")
    verify_contents(folder)


def snapshot(path,policy,day,allow_create):
    owner=policy["owner"]
    if not re.fullmatch(r"[0-9a-f]{32}",owner):
        raise ValueError("自动备份实例标识无效")
    root=Path(policy["destination"])/("gaoxu-auto-"+owner)
    root.mkdir(parents=True,exist_ok=True)
    if not plain(root):
        raise ValueError("自动备份专用目录不能是链接")
    folder=root/day
    if folder.exists():
        verify_bundle(folder,owner)
    else:
        if not allow_create:
            raise ValueError("本轮备份多次中断，未找到完整副本")
        size=0
        for file in (path,Path(ledger_path(path))):
            with closing(database(file,"ro")) as db:
                size+=db.execute("PRAGMA page_count").fetchone()[0]*db.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(root).free<size+64*1024*1024:
            raise ValueError("备份磁盘可用空间不足")
        create(path,root,daily={"owner":owner,"day":day})
    older=sorted((p for p in root.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d",p.name) and p.name<=day),
                 key=lambda p:p.name,reverse=True)
    if len(older)<=KEEP:
        return folder,""
    warning=""
    verified=0
    for previous in older:
        try:
            if previous!=folder:
                verify_bundle(previous,owner)
            verified+=1
            if verified<=KEEP:
                continue
            for name in (*DATABASES,"manifest.json"):
                (previous/name).unlink()
            previous.rmdir()
        except (ValueError,OSError,sqlite3.DatabaseError):
            warning="已有新备份；较早的备份目录需要人工核对"
    return folder,warning


def tick(path,now=None):
    path=Path(path)
    now=int(time.time()) if now is None else now
    with transaction(path,False) as db:
        policy=db.execute("SELECT * FROM backup_policy WHERE singleton=1 AND enabled=1").fetchone()
    if not policy:
        return
    clock=datetime.fromtimestamp(now,BEIJING)
    due=clock.replace(hour=policy["minute"]//60,minute=policy["minute"]%60,second=0,microsecond=0)
    if due>clock:
        due-=timedelta(days=1)
    if due.date()<datetime.fromtimestamp(policy["since"],BEIJING).date():
        return
    day=due.date().isoformat()
    same=day==policy["day"]
    if day<policy["day"] or (same and (policy["state"]=="success" or policy["retry_at"]>now
                                        or policy["state"]=="failed" and policy["attempts"]>=3)):
        return
    allow_create=not same or policy["attempts"]<3
    attempts=min(3,policy["attempts"]+1) if same else 1
    with transaction(path) as db:
        claimed=db.execute("""UPDATE backup_policy SET state='running',attempts=?,day=?,retry_at=0,message=''
            WHERE singleton=1 AND enabled=1 AND revision=?""",(attempts,day,policy["revision"]))
        if not claimed.rowcount:
            return
    try:
        folder,message=snapshot(path,policy,day,allow_create)
    except Exception as exc:
        with transaction(path) as db:
            db.execute("""UPDATE backup_policy SET state='failed',message=?,retry_at=? WHERE revision=?""",
                       (str(exc)[:400],now+900*attempts if attempts<3 else 0,policy["revision"]))
        return
    with transaction(path) as db:
        db.execute("""UPDATE backup_policy SET state='success',last_success=?,last_bundle=?,message=?
                   WHERE revision=?""",(int(time.time()),str(folder),message,policy["revision"]))
