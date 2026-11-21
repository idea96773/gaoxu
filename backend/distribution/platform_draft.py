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
        WHERE j.id=? AND j.platform='zhihu' AND d.provider='reprint' AND d.platform=j.platform AND d.account=j.account AND d.row_id=j.row_id""",(job,)).fetchone()
    if not row or row["approval"]!="approved" or row["snapshot"]!=row["frozen"] or digest(row["snapshot"])!=row["sha"]:
        raise ValueError("需要已审核、内容校验通过的知乎转载冻结稿")
    return row


def bridge_status(db):
    row=db.execute("SELECT user_id,name FROM draft_bridge WHERE singleton=1 AND seen>=?",(int(time.time())-45,)).fetchone()
    return dict(connected=bool(row),account=dict(row) if row and row["user_id"] else None)


def recipient(db,job,alias,existing):
    # Older outbox records already carry an explicitly confirmed recipient.
    pinned=db.execute("SELECT user_id,name FROM zhihu_draft_recipients WHERE job=?",(job,)).fetchone()
    if pinned:
        return dict(pinned),"frozen"
    if existing:
        return dict(user_id=existing["user_id"],name=existing["name"]),"record"
    binding=db.execute("SELECT user_id,name FROM zhihu_draft_bindings WHERE alias=? AND user_id IS NOT NULL",(alias,)).fetchone()
    return (dict(binding),"binding") if binding else (None,"unbound")


def register(api,path,body):
    def require_local():
        host=urlsplit(request.host_url).hostname
        origin=request.headers.get("Origin")
        if host not in ("127.0.0.1","localhost","::1") or (origin and origin!=request.host_url.rstrip("/")):
            raise ValueError("请从本机稿序管理台操作")

    @api.get("/zhihu-draft-accounts")
    def draft_accounts():
        require_local()
        with transaction(path,False) as db:
            rows=db.execute("""WITH aliases(alias) AS (
                    SELECT alias FROM zhihu_draft_bindings
                    UNION SELECT json_extract(t.value,'$.account') FROM reprint_batches rb
                    JOIN batches b ON b.id=rb.batch JOIN json_each(b.targets) t
                    WHERE json_extract(t.value,'$.platform')='zhihu'
                    UNION SELECT json_extract(t.value,'$.account') FROM reprint_defaults d
                    JOIN json_each(d.payload,'$.targets') t WHERE json_extract(t.value,'$.platform')='zhihu')
                SELECT a.alias,b.user_id,b.name,coalesce(b.revision,0) revision
                FROM aliases a LEFT JOIN zhihu_draft_bindings b ON b.alias=a.alias ORDER BY a.alias""")
            return jsonify(**bridge_status(db),items=[dict(r) for r in rows])

    @api.post("/zhihu-draft-accounts")
    def bind_draft_account():
        require_local()
        data=body()
        alias,expected=data.get("alias"),data.get("revision")
        if not isinstance(alias,str) or not re.fullmatch(r"[\w-]{1,40}",alias) or type(expected) is not int or expected<0:
            raise ValueError("请输入有效的分稿别名并读取当前绑定")
        with transaction(path) as db:
            current=db.execute("SELECT * FROM zhihu_draft_bindings WHERE alias=?",(alias,)).fetchone()
            if (current["revision"] if current else 0)!=expected:
                return jsonify(error="此别名的绑定已变化，请刷新核对"),409
            if data.get("action")=="bind":
                live=bridge_status(db)["account"]
                if not live or live["user_id"]!=data.get("user_id"):
                    raise ValueError("草稿桥连接的知乎账号已变化，请刷新核对")
                uid,name=live["user_id"],live["name"]
                other=db.execute("SELECT alias FROM zhihu_draft_bindings WHERE user_id=? AND alias<>?",(uid,alias)).fetchone()
                if other:
                    raise ValueError("这个知乎账号已绑定分稿别名「"+other["alias"]+"」")
            elif data.get("action")=="unlink" and current and current["user_id"] and data.get("user_id")==current["user_id"]:
                uid,name=None,""
            else:
                raise ValueError("请核对要更改的账号绑定")
            db.execute("""INSERT INTO zhihu_draft_bindings(alias,user_id,name,revision,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(alias) DO UPDATE SET user_id=excluded.user_id,name=excluded.name,
                revision=excluded.revision,updated=excluded.updated""",(alias,uid,name,expected+1,int(time.time())))
            return jsonify(revision=expected+1)

    @api.get("/jobs/<int:job>/platform-draft")
    def platform_draft_info(job):
        require_local()
        with transaction(path,False) as db:
            record=db.execute("SELECT * FROM platform_drafts WHERE job=?",(job,)).fetchone()
            row=db.execute("""SELECT j.account,b.control FROM jobs j JOIN rows r ON r.id=j.row_id
                JOIN batches b ON b.id=r.batch WHERE j.id=? AND j.platform='zhihu'""",(job,)).fetchone()
            if not row:
                raise KeyError("找不到知乎任务")
            target,via=recipient(db,job,row["account"],record)
            return jsonify(**bridge_status(db),recipient=target,recipient_source=via,control=row["control"],
                           record=dict(record) if record else None)

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
                target,_=recipient(db,job,row["account"],existing)
                if not target or target["user_id"]!=account["user_id"]:
                    raise ValueError("当前登录与此稿的目标账号不一致，请核对分稿别名和知乎账号 ID")
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
                    db.execute("INSERT OR IGNORE INTO zhihu_draft_recipients VALUES(?,?,?)",(job,account["user_id"],account["name"]))
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
        pin=db.execute("SELECT user_id FROM zhihu_draft_recipients WHERE job=?",(task["job"],)).fetchone()
        if pin and pin["user_id"]!=task["user_id"]:
            raise ValueError("草稿任务与冻结时锁定的知乎账号不一致")
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
