"""Local outbox for operator-authorized Zhihu drafts, independent of simulation receipts."""
import json
import re
import time
from pathlib import Path

from urllib.parse import urlsplit

from flask import jsonify, request

from . import draft_images, frozen_reprint, rights
from .draft_bridge import status as bridge_status
from .store import compact, transaction


class DraftUrlConflict(ValueError):
    """An edit address already belongs to another task or its pending receipt."""


def edit_url(value):
    if not isinstance(value,str) or not re.fullmatch(r"https://zhuanlan\.zhihu\.com/p/[1-9]\d{0,24}/edit",value):
        raise ValueError("请输入知乎草稿编辑页地址")
    return value


def checked_url(db,record,value,dismissed_at=None):
    url=edit_url(value)
    if record["url"] and record["url"]!=url:
        raise ValueError("编辑地址与已有回执不一致")
    other=db.execute("SELECT job FROM platform_drafts WHERE url=? AND id!=?",(url,record["id"])).fetchone()
    if other:
        raise DraftUrlConflict(f"该知乎编辑地址已关联任务 #{other['job']}，请核对账号与稿件")
    if not record["url"]:
        dismissed=db.execute("""SELECT d.dismissed_at FROM zhihu_dismissed_receipts d
            JOIN zhihu_draft_receipts r ON r.outbox=d.outbox WHERE d.outbox=? AND r.edit_url=?""",
            (record["id"],url)).fetchone()
        if dismissed and (type(dismissed_at) is not int or dismissed_at!=dismissed["dismissed_at"]):
            raise ValueError("这个地址已标记为与本稿不符，请重新打开核对表并核对实际草稿")
        pending=db.execute("""SELECT p.job FROM zhihu_draft_receipts r JOIN platform_drafts p ON p.id=r.outbox
            LEFT JOIN zhihu_dismissed_receipts d ON d.outbox=p.id
            WHERE r.edit_url=? AND p.id!=? AND p.url='' AND d.outbox IS NULL ORDER BY p.id LIMIT 1""",
            (url,record["id"])).fetchone()
        if pending:
            raise DraftUrlConflict(f"任务 #{pending['job']} 正在核对这个知乎编辑地址，请先核对该任务的回执")
    return url


def remember_receipt(path,task,value):
    """Persist the extension reply before a second network call checks the browser identity."""
    with transaction(path) as db:
        record=db.execute("""SELECT id,url FROM platform_drafts WHERE id=? AND sha=? AND user_id=?
            AND state='sending'""",(task["id"],task["sha"],task["user_id"])).fetchone()
        if not record:
            raise ValueError("此任务已退出保存状态")
        url=checked_url(db,record,value)
        previous=db.execute("SELECT edit_url FROM zhihu_draft_receipts WHERE outbox=?",(record["id"],)).fetchone()
        if previous:
            if previous["edit_url"]!=url:
                raise ValueError("这次请求返回了不同的草稿地址，请在指定账号核对")
            return url
        db.execute("INSERT INTO zhihu_draft_receipts(outbox,edit_url) VALUES(?,?)",(record["id"],url))
        return url


def record_inspection(db,job,record,data,now):
    if not record or record["state"] not in ("saved","unknown","needs_review","verified"):
        raise ValueError("请先在指定账号草稿箱找到对应草稿")
    row=source(db,job)
    target,_=recipient(db,job,row["account"],record)
    if (row["sha"]!=record["sha"] or data.get("sha")!=row["sha"] or data.get("user_id")!=record["user_id"]
            or target["user_id"]!=record["user_id"]):
        raise ValueError("冻结稿或草稿接收账号已变化")
    url=checked_url(db,record,data.get("url"),data.get("dismissed_at"))
    checks=data.get("checks")
    if (not isinstance(checks,dict) or set(checks)!={"title","layout","images","footer"}
            or any(v not in ("pass","issue","pending") for v in checks.values())
            or all(v=="pending" for v in checks.values())):
        raise ValueError("请填写至少一个核对结果，并保留其余待核对项")
    payload=compact(checks)
    last=db.execute("SELECT id,checks FROM draft_inspections WHERE outbox=? ORDER BY id DESC LIMIT 1",
                    (record["id"],)).fetchone()
    if last and last["checks"]==payload and record["url"]==url:
        return
    if (type(data.get("revision")) is not int or data["revision"]!=(last["id"] if last else 0)
            or data.get("expected_state")!=record["state"]):
        raise ValueError("已有新的草稿回执或核对记录，请重新打开核对表")
    state="needs_review" if "issue" in checks.values() else ("saved" if "pending" in checks.values() else "verified")
    db.execute("""INSERT INTO draft_inspections(outbox,sha,user_id,url,checks,checked_at)
               VALUES(?,?,?,?,?,?)""",(record["id"],row["sha"],record["user_id"],url,payload,now))
    db.execute("UPDATE platform_drafts SET state=?,url=?,reason='',updated=? WHERE id=?",(state,url,now,record["id"]))


def source(db, job):
    return frozen_reprint.source(db,job,platform="zhihu")


def recipient(db,job,alias,existing):
    # Older outbox records already carry an explicitly confirmed recipient.
    pinned=db.execute("SELECT user_id,name FROM zhihu_draft_recipients WHERE job=?",(job,)).fetchone()
    if pinned:
        return dict(pinned),"frozen"
    if existing:
        return dict(user_id=existing["user_id"],name=existing["name"]),"record"
    binding=db.execute("SELECT user_id,name FROM zhihu_draft_bindings WHERE alias=? AND user_id IS NOT NULL",(alias,)).fetchone()
    return (dict(binding),"binding") if binding else (None,"unbound")


def local_console_request():
    host=urlsplit(request.host_url).hostname
    origin=request.headers.get("Origin")
    return host in ("127.0.0.1","localhost","::1") and (not origin or origin==request.host_url.rstrip("/"))


def restore_context(db,job):
    return db.execute("SELECT generation,restored_at FROM restore_context WHERE max_job>=?",(job,)).fetchone()


def new_draft_record(db,row,target,state,now):
    uid=target["user_id"]
    prior=db.execute("SELECT job FROM platform_drafts WHERE canonical=? AND user_id=?",(row["canonical"],uid)).fetchone()
    if prior:
        raise ValueError(f"该账号已有同一篇文章的草稿任务 #{prior[0]}，请查看原任务")
    outbox=db.execute("""INSERT INTO platform_drafts(job,canonical,sha,user_id,name,state,updated)
        VALUES(?,?,?,?,?,?,?)""",(row["id"],row["canonical"],row["sha"],uid,target["name"],state,now)).lastrowid
    db.execute("INSERT OR IGNORE INTO zhihu_draft_recipients VALUES(?,?,?)",(row["id"],uid,target["name"]))
    return outbox


def register(api,path,body):
    def require_local():
        if not local_console_request():
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
            if request.args.get("launch")=="1":
                frozen=source(db,job)
                if frozen["control"]=="cancelled" or frozen["state"]=="cancelled":
                    raise ValueError("此任务已取消，请重新选择知乎转载任务")
                return jsonify(application="gaoxu",database=str(Path(path).resolve()),job=job,
                               title=json.loads(frozen["snapshot"])["title"],sha=frozen["sha"],
                               account=frozen["account"],slot=frozen["slot"])
            record=db.execute("SELECT * FROM platform_drafts WHERE job=?",(job,)).fetchone()
            row=db.execute("""SELECT j.account,j.state,b.control,f.sha FROM jobs j JOIN frozen_deliveries f ON f.job=j.id JOIN rows r ON r.id=j.row_id
                JOIN batches b ON b.id=r.batch WHERE j.id=? AND j.platform='zhihu'""",(job,)).fetchone()
            if not row:
                raise KeyError("找不到知乎任务")
            target,via=recipient(db,job,row["account"],record)
            recovery=restore_context(db,job) if not record else None
            review=db.execute("SELECT result,checked_at FROM zhihu_restore_reviews WHERE outbox=?",
                              (record["id"],)).fetchone() if record else None
            grant=rights.current(db,job)
            events=[] if not record else [dict(event) for event in db.execute("""SELECT id,state,reason,changed,baseline
                FROM platform_draft_events WHERE outbox=? ORDER BY id DESC LIMIT 20""",(record["id"],))]
            changes=db.execute("""SELECT scope,user_id,confirmed FROM reprint_rights WHERE job=?
                ORDER BY id DESC LIMIT 10""",(job,))
            inspections=[] if not record else [dict(r)|{"checks":json.loads(r["checks"])} for r in db.execute(
                "SELECT id,checks,checked_at,sha,user_id,url FROM draft_inspections WHERE outbox=? ORDER BY id DESC LIMIT 10",
                (record["id"],))]
            receipt=None if not record else db.execute("""SELECT r.edit_url,d.dismissed_at
                FROM zhihu_draft_receipts r LEFT JOIN zhihu_dismissed_receipts d ON d.outbox=r.outbox
                WHERE r.outbox=?""",(record["id"],)).fetchone()
            pending_url=receipt["edit_url"] if (receipt and receipt["dismissed_at"] is None and
                record["state"]=="unknown" and not record["url"]) else None
            return jsonify(**bridge_status(db),recipient=target,recipient_source=via,control=row["control"],job_state=row["state"],
                           rights=grant,permitted=bool(target and rights.permits(grant,row["sha"],target["user_id"])),
                           rights_history=[dict(event) for event in changes],recovery=dict(recovery) if recovery else None,
                           recovery_review=dict(review) if review else None,
                           reusable_images=len(draft_images.confirmed(db,record["id"],grant["revision"])) if record else 0,
                           record=dict(record) if record else None,inspections=inspections,state_history=events,
                           unverified_edit_url=pending_url,
                           dismissed_receipt=dict(receipt) if receipt and receipt["dismissed_at"] is not None else None)

    @api.post("/jobs/<int:job>/platform-draft")
    def change_platform_draft(job):
        require_local()
        data=body()
        action=data.get("action")
        with transaction(path) as db:
            now=int(time.time())
            existing=db.execute("SELECT * FROM platform_drafts WHERE job=?",(job,)).fetchone()
            if action in ("queue","cancel") and "expected_event" in data:
                latest=db.execute("SELECT max(id) FROM platform_draft_events WHERE outbox=?",(existing["id"],)).fetchone()[0] if existing else 0
                if type(data["expected_event"]) is not int or data["expected_event"]!=latest:
                    return jsonify(error="草稿任务状态已变化，请核对最新处理记录后操作"),409
            if action=="scope":
                scope=data.get("scope")
                if scope=="local":
                    # Withdrawing a grant must also work if its source or media needs repair.
                    row=db.execute("""SELECT f.sha FROM frozen_deliveries f JOIN jobs j ON j.id=f.job
                        JOIN drafts d ON d.id=f.draft WHERE f.job=? AND j.platform='zhihu' AND d.provider='reprint'""",
                        (job,)).fetchone()
                    if not row:
                        raise ValueError("找不到对应的知乎转载任务")
                else:
                    row=source(db,job)
                grant=rights.current(db,job)
                if (row["sha"]!=data.get("sha") or type(data.get("scope_revision")) is not int
                        or data["scope_revision"]!=grant["revision"]):
                    return jsonify(error="稿件或授权记录已更新，请刷新后核对"),409
                uid=""
                if scope=="zhihu_draft":
                    target,_=recipient(db,job,row["account"],existing)
                    if (not target or target["user_id"]!=data.get("user_id") or data.get("rights_confirmed") is not True
                            or row["control"]=="cancelled" or row["state"]=="cancelled"):
                        raise ValueError("请核对接收账号，并确认正文和配图已获准用于该知乎草稿箱")
                    uid=target["user_id"]
                elif scope=="local":
                    if existing and existing["state"] in ("queued","preparing"):
                        db.execute("""UPDATE platform_drafts SET state='cancelled',reason='授权已改为仅本地',
                            updated=? WHERE job=?""",(now,job))
                else:
                    raise ValueError("未知的转载授权范围")
                if (grant["scope"],grant["user_id"],grant["sha"])!=(scope,uid,row["sha"]):
                    if existing:
                        db.execute("DELETE FROM zhihu_draft_images WHERE outbox=?",(existing["id"],))
                    rights.record(db,job,row["sha"],scope,uid,now)
            elif action=="recover_existing":
                row=source(db,job)
                recovery=restore_context(db,job)
                target,_=recipient(db,job,row["account"],existing)
                if (not recovery or data.get("generation")!=recovery["generation"] or data.get("checked") is not True
                        or not target or target["user_id"]!=data.get("user_id") or row["sha"]!=data.get("sha")):
                    raise ValueError("请核对恢复副本、冻结稿版本及指定接收账号")
                url=edit_url(data.get("url"))
                if existing:
                    if (existing["state"] not in ("saved","verified","needs_review") or existing["url"]!=url
                            or existing["sha"]!=row["sha"] or existing["user_id"]!=target["user_id"]):
                        raise ValueError("这份任务已有草稿记录，请刷新核对")
                else:
                    outbox=new_draft_record(db,row,target,"saved",now)
                    checked_url(db,{"id":outbox,"url":""},url)
                    db.execute("UPDATE platform_drafts SET url=? WHERE id=?",(url,outbox))
                    db.execute("INSERT INTO zhihu_restore_reviews VALUES(?,?,'found',?)",(outbox,recovery["generation"],now))
            elif action=="queue":
                row=source(db,job)
                if row["control"]!="active" or row["state"]=="cancelled":
                    raise ValueError("批次暂停或已取消")
                connection=bridge_status(db)
                if connection["bridge"]["check_only"]:
                    raise ValueError("当前仅核对连接。保存草稿前请切换草稿桥到交付模式")
                selected=connection["bridge"]["job_id"]
                if selected is not None and selected!=job:
                    raise ValueError(f"草稿桥仅处理任务 #{selected}，请核对所选任务")
                account=connection["account"]
                if (not account or account["user_id"]!=data.get("user_id") or data.get("sha")!=row["sha"]
                        or data.get("consent") is not True):
                    raise ValueError("请核对当前知乎账号与冻结版本，确认允许发送正文和图片")
                target,_=recipient(db,job,row["account"],existing)
                if not target or target["user_id"]!=account["user_id"]:
                    raise ValueError("当前登录与此稿的目标账号不一致，请核对分稿别名和知乎账号 ID")
                grant=rights.current(db,job)
                if (not rights.permits(grant,row["sha"],account["user_id"]) or type(data.get("scope_revision")) is not int
                        or data["scope_revision"]!=grant["revision"]):
                    raise ValueError("请先核对本份冻结稿及其配图对目标账号的草稿授权")
                if existing:
                    if existing["user_id"]!=account["user_id"] or existing["sha"]!=row["sha"]:
                        raise ValueError("此稿已有其他账号或版本的保存记录")
                    if existing["state"] in ("failed","cancelled"):
                        db.execute("UPDATE platform_drafts SET state='queued',reason='',updated=? WHERE id=?",(now,existing["id"]))
                else:
                    recovery=restore_context(db,job)
                    if recovery and (data.get("generation")!=recovery["generation"] or data.get("checked_absent") is not True):
                        raise ValueError("恢复点之后可能已保存过这篇稿，请先核对指定账号的草稿箱")
                    outbox=new_draft_record(db,row,account,"queued",now)
                    if recovery:
                        db.execute("INSERT INTO zhihu_restore_reviews VALUES(?,?,'absent',?)",(outbox,recovery["generation"],now))
            elif action=="cancel" and existing and existing["state"] in ("queued","preparing"):
                db.execute("UPDATE platform_drafts SET state='cancelled',reason='已在管理台取消草稿任务',updated=? WHERE job=?",(now,job))
            elif action=="inspect":
                record_inspection(db,job,existing,data,now)
            elif action=="dismiss_receipt":
                if (not existing or existing["state"]!="unknown" or existing["url"]
                        or data.get("sha")!=existing["sha"] or data.get("user_id")!=existing["user_id"]
                        or data.get("checked") is not True):
                    raise ValueError("请在指定账号核对这份回执不确定的草稿")
                receipt=db.execute("SELECT edit_url FROM zhihu_draft_receipts WHERE outbox=?",(existing["id"],)).fetchone()
                if not receipt or edit_url(data.get("url"))!=receipt["edit_url"]:
                    raise ValueError("待核对的回执地址已变化，请刷新")
                changed=db.execute("INSERT OR IGNORE INTO zhihu_dismissed_receipts VALUES(?,?)",(existing["id"],now))
                if changed.rowcount:
                    db.execute("UPDATE platform_drafts SET updated=? WHERE id=?",(now,existing["id"]))
            elif action=="confirm" and existing and existing["state"] in ("saved","unknown","verified"):
                if db.execute("SELECT 1 FROM draft_inspections WHERE outbox=?",(existing["id"],)).fetchone():
                    raise ValueError("此稿已有逐项核对记录，请使用当前核对表")
                if data.get("checked") is not True:
                    raise ValueError("请先在指定账号的草稿箱核对正文、图片与文末链接")
                url=checked_url(db,existing,data.get("url"))
                db.execute("UPDATE platform_drafts SET state='verified',url=?,reason='',updated=? WHERE job=?",(url,now,job))
            else:
                raise ValueError("当前状态无法执行此操作，请刷新")
        return jsonify(ok=True)


def claim(path,user_id,job_id=None,expected_sha=None):
    with transaction(path) as db:
        row=db.execute("""SELECT t.* FROM platform_drafts t JOIN jobs j ON j.id=t.job JOIN rows r ON r.id=j.row_id
             JOIN batches b ON b.id=r.batch WHERE t.state='queued' AND t.user_id=? AND b.control='active'
             AND j.state!='cancelled' AND (? IS NULL OR t.job=?) AND (? IS NULL OR t.sha=?)
             ORDER BY t.id LIMIT 1""",(user_id,job_id,job_id,expected_sha,expected_sha)).fetchone()
        if not row:
            return None
        if not rights.permits(rights.current(db,row["job"]),row["sha"],row["user_id"]):
            db.execute("""UPDATE platform_drafts SET state='failed',reason='请在管理台确认此稿的知乎草稿授权',
                updated=? WHERE id=?""",(int(time.time()),row["id"]))
            return None
        db.execute("UPDATE platform_drafts SET state='preparing',reason='',updated=? WHERE id=?",(int(time.time()),row["id"]))
        return dict(row)


def authorized_source(db,task):
    row=source(db,task["job"])
    pin=db.execute("SELECT user_id FROM zhihu_draft_recipients WHERE job=?",(task["job"],)).fetchone()
    if pin and pin["user_id"]!=task["user_id"]:
        raise ValueError("草稿任务与冻结时锁定的知乎账号不一致")
    if row["sha"]!=task["sha"]:
        raise ValueError("审核版本已变化")
    require_rights(db,task)
    return row


def payload(path,task):
    with transaction(path,False) as db:
        return json.loads(authorized_source(db,task)["snapshot"])


def require_rights(db,task):
    grant=rights.current(db,task["job"])
    if not rights.permits(grant,task["sha"],task["user_id"]):
        raise ValueError("此份稿件向该账号发送正文与配图的授权已失效")
    return grant["revision"]


def transition(path,task,old,new,reason="",url=""):
    with transaction(path) as db:
        if new=="saved":
            record=db.execute("SELECT id,url FROM platform_drafts WHERE id=? AND state=?",(task["id"],old)).fetchone()
            if not record:
                return False
            url=checked_url(db,record,url)
        if new=="sending":
            row=authorized_source(db,task)
            if row["control"]=="paused":
                db.execute("""UPDATE platform_drafts SET state='queued',reason='批次已暂停，继续批次后保存草稿',updated=?
                    WHERE id=? AND state=?""",(int(time.time()),task["id"],old))
                return False
            if row["control"]!="active" or row["state"]=="cancelled":
                raise ValueError("批次或冻结稿状态已变化")
        updated=db.execute("""UPDATE platform_drafts SET state=?,reason=?,url=?,updated=?
             WHERE id=? AND state=?""",(new,reason,url,int(time.time()),task["id"],old))
        if updated.rowcount and new=="sending":
            db.execute("DELETE FROM zhihu_draft_images WHERE outbox=?",(task["id"],))
        return updated.rowcount==1
