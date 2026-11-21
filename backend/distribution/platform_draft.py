"""Local outbox for operator-authorized Zhihu drafts, independent of simulation receipts."""
import json
import re
import time

from urllib.parse import urlsplit

from flask import jsonify, request

from .store import digest, transaction


def edit_url(value):
    if not isinstance(value,str) or not re.fullmatch(r"https://zhuanlan\.zhihu\.com/p/[1-9]\d{0,24}/edit",value):
        raise ValueError("请输入知乎草稿编辑页地址")
    return value


def source(db, job):
    row=db.execute("""SELECT j.id,j.state,j.account,r.canonical,b.control,f.sha,f.snapshot,d.frozen,d.state AS approval
        FROM jobs j JOIN frozen_deliveries f ON f.job=j.id JOIN drafts d ON d.id=f.draft
        JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
        WHERE j.id=? AND j.platform='zhihu' AND d.provider='reprint'""",(job,)).fetchone()
    if not row or row["approval"]!="approved" or row["snapshot"]!=row["frozen"] or digest(row["snapshot"])!=row["sha"]:
        raise ValueError("需要已审核、内容校验通过的知乎转载冻结稿")
    return row


def bridge_status(db):
    row=db.execute("SELECT user_id,name FROM draft_bridge WHERE singleton=1 AND seen>=?",(int(time.time())-45,)).fetchone()
    return dict(connected=bool(row),account=dict(row) if row and row["user_id"] else None)


def register(api,path,body):
    def require_local():
        host=urlsplit(request.host_url).hostname
        origin=request.headers.get("Origin")
        if host not in ("127.0.0.1","localhost","::1") or (origin and origin!=request.host_url.rstrip("/")):
            raise ValueError("请从本机稿序管理台操作")

    @api.get("/jobs/<int:job>/platform-draft")
    def platform_draft_info(job):
        require_local()
        with transaction(path,False) as db:
            record=db.execute("SELECT * FROM platform_drafts WHERE job=?",(job,)).fetchone()
            return jsonify(**bridge_status(db),record=dict(record) if record else None)

    @api.post("/jobs/<int:job>/platform-draft")
    def change_platform_draft(job):
        require_local()
        data=body()
        action=data.get("action")
        with transaction(path) as db:
            now=int(time.time())
            existing=db.execute("SELECT * FROM platform_drafts WHERE job=?",(job,)).fetchone()
            if action=="queue":
                row=source(db,job)
                if row["control"]!="active" or row["state"]=="cancelled":
                    raise ValueError("批次暂停或已取消")
                account=bridge_status(db)["account"]
                if (not account or account["user_id"]!=data.get("user_id") or data.get("sha")!=row["sha"]
                        or data.get("consent") is not True):
                    raise ValueError("请核对当前知乎账号与冻结版本，确认允许发送正文和图片")
                if existing:
                    if existing["user_id"]!=account["user_id"] or existing["sha"]!=row["sha"]:
                        raise ValueError("此稿已有其他账号或版本的保存记录")
                    if existing["state"] in ("failed","cancelled"):
                        db.execute("UPDATE platform_drafts SET state='queued',reason='',updated=? WHERE id=?",(now,existing["id"]))
                else:
                    prior=db.execute("SELECT job FROM platform_drafts WHERE canonical=? AND user_id=?",
                                     (row["canonical"],account["user_id"])).fetchone()
                    if prior:
                        raise ValueError(f"该账号已有同一篇文章的草稿任务 #{prior[0]}，请查看原任务")
                    db.execute("""INSERT INTO platform_drafts(job,canonical,sha,user_id,name,state,updated)
                        VALUES(?,?,?,?,?,'queued',?)""",(job,row["canonical"],row["sha"],account["user_id"],account["name"],now))
            elif action=="cancel" and existing and existing["state"] in ("queued","preparing"):
                db.execute("UPDATE platform_drafts SET state='cancelled',updated=? WHERE job=?",(now,job))
            elif action=="confirm" and existing and existing["state"] in ("saved","unknown","verified"):
                if data.get("checked") is not True:
                    raise ValueError("请先在指定账号的草稿箱核对正文、图片与文末链接")
                url=edit_url(data.get("url"))
                if existing["url"] and existing["url"]!=url:
                    raise ValueError("编辑地址与已有回执不一致")
                db.execute("UPDATE platform_drafts SET state='verified',url=?,reason='',updated=? WHERE job=?",(url,now,job))
            else:
                raise ValueError("当前状态无法执行此操作，请刷新")
        return jsonify(ok=True)


def claim(path,user_id):
    with transaction(path) as db:
        row=db.execute("""SELECT t.* FROM platform_drafts t JOIN jobs j ON j.id=t.job JOIN rows r ON r.id=j.row_id
             JOIN batches b ON b.id=r.batch WHERE t.state='queued' AND t.user_id=? AND b.control='active'
             AND j.state!='cancelled' ORDER BY t.id LIMIT 1""",(user_id,)).fetchone()
        if not row:
            return None
        db.execute("UPDATE platform_drafts SET state='preparing',updated=? WHERE id=?",(int(time.time()),row["id"]))
        return dict(row)


def payload(path,task):
    with transaction(path,False) as db:
        row=source(db,task["job"])
        if row["sha"]!=task["sha"]:
            raise ValueError("审核版本已变化")
        return json.loads(row["snapshot"])


def transition(path,task,old,new,reason="",url=""):
    with transaction(path) as db:
        if new=="sending":
            row=source(db,task["job"])
            if row["sha"]!=task["sha"] or row["control"]!="active" or row["state"]=="cancelled":
                raise ValueError("批次或冻结稿状态已变化")
        updated=db.execute("""UPDATE platform_drafts SET state=?,reason=?,url=?,updated=?
             WHERE id=? AND state=?""",(new,reason,url,int(time.time()),task["id"],old))
        return updated.rowcount==1
