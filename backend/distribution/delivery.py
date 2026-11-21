"""Delivery of approved editorial snapshots to the local, persistent receiver."""
import json
from datetime import datetime

from .store import BEIJING, available_time, compact, digest, ledger_path, transaction


def delivery_key(job):
    return digest(compact(["frozen-dry-run", job["canonical"], job["platform"], job["account"]]))


def schedule(path, draft_id, data, now):
    with transaction(path) as db:
        return schedule_in(db, draft_id, data, now)


def schedule_in(db, draft_id, data, now):
    if set(data) - {"time"}:
        raise ValueError("版本、平台、账号及内容由冻结稿确定")
    requested = data.get("time")
    if requested is not None and type(requested) is not int:
        raise ValueError("排期时间格式无效")
    draft = db.execute("""SELECT d.*, b.control, b.rules FROM drafts d
        JOIN rows r ON r.id=d.row_id JOIN batches b ON b.id=r.batch WHERE d.id=?""", (draft_id,)).fetchone()
    if not draft:
        raise KeyError("找不到平台成稿")
    if draft["state"] != "approved" or not draft["frozen"]:
        raise ValueError("请先审核并冻结这个账号的成稿")
    target = (draft["row_id"], draft["platform"], draft["account"])
    old = db.execute("""SELECT f.*, j.state FROM frozen_deliveries f JOIN jobs j ON j.id=f.job
        WHERE j.row_id=? AND j.platform=? AND j.account=?""", target).fetchone()
    if old:
        if old["draft"] != draft_id:
            raise ValueError("这篇文章在此账号已有其他冻结版的交付任务")
        if requested != old["requested"]:
            raise ValueError("该冻结稿已有排期，请查看已有任务")
        return {"id":old["job"], "slot":old["planned"], "state":old["state"], "created":False}
    if draft["control"] != "active":
        raise ValueError("批次已暂停或取消")
    if requested is not None and not now <= requested <= now+366*86400:
        raise ValueError("请选择未来一年内的北京时间")
    limits = json.loads(draft["rules"])
    bookings = db.execute("""SELECT coalesce(finished,slot) mark, account FROM jobs
        WHERE stage='deliver' AND platform=? AND state!='cancelled' ORDER BY mark""", (draft["platform"],)).fetchall()
    slot = available_time(requested or now, draft["account"], limits, bookings, check_gaps=True)
    if requested is not None and requested != slot:
        when = datetime.fromtimestamp(slot, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        raise ValueError("所选排期与时间窗、间隔或日上限冲突；可选北京时间 " + when)
    job = db.execute("""INSERT INTO jobs(row_id,stage,platform,account,revision,due,slot)
        VALUES(?,'deliver',?,?,?,?,?)""", (*target,draft["rev"],slot,slot)).lastrowid
    payload = draft["frozen"]
    db.execute("INSERT INTO frozen_deliveries VALUES(?,?,?,?,?,?)",
               (job,draft_id,requested,slot,payload,digest(payload)))
    return {"id":job, "slot":slot, "state":"ready", "created":True}


def receipt(path, job):
    with transaction(ledger_path(path),False) as remote:
        return remote.execute("SELECT remote_id,submitted FROM delivered_drafts WHERE key=?",
                              (delivery_key(job),)).fetchone()


def deliver(path, db, job, now):
    bound = db.execute("""SELECT f.*, d.state, d.frozen, d.rev, d.platform, d.account, d.row_id
        FROM frozen_deliveries f JOIN drafts d ON d.id=f.draft WHERE f.job=?""",(job["id"],)).fetchone()
    if not bound or bound["state"]!="approved" or bound["frozen"]!=bound["snapshot"] or digest(bound["snapshot"])!=bound["sha"]:
        raise ValueError("冻结快照校验失败")
    if (bound["row_id"],bound["platform"],bound["account"],bound["rev"],bound["planned"]) != (
            job["row_id"],job["platform"],job["account"],job["revision"],job["slot"]):
        raise ValueError("任务账号、版本或排期与冻结绑定不一致")
    key = delivery_key(job)
    with transaction(ledger_path(path)) as remote:
        existing = remote.execute("SELECT * FROM delivered_drafts WHERE key=?",(key,)).fetchone()
        if existing:
            if (existing["sha"],existing["draft"],existing["planned"]) != (bound["sha"],bound["draft"],bound["planned"]):
                raise ValueError("模拟接收端记录与本次交付不一致")
            return existing["remote_id"], existing["submitted"]
        confirmed = remote.execute("""SELECT submitted mark,account FROM delivered_drafts
            WHERE platform=? ORDER BY submitted""", (job["platform"],)).fetchall()
        due = available_time(max(job["slot"],now),job["account"],json.loads(job["rules"]),confirmed,check_gaps=True)
        if due>now:
            return None,due
        remote_id = "draft-sim-"+key[:24]
        remote.execute("INSERT INTO delivered_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (key,remote_id,now,job["row_id"],bound["draft"],job["revision"],job["platform"],
                        job["account"],bound["planned"],bound["sha"],bound["snapshot"]))
        return remote_id,now


def details(path, db, job):
    bound=db.execute("SELECT * FROM frozen_deliveries WHERE job=?",(job["id"],)).fetchone()
    if not bound:
        return None
    with transaction(ledger_path(path),False) as remote:
        delivered=remote.execute("""SELECT * FROM delivered_drafts
            WHERE remote_id=?""",(job["receipt"],)).fetchone()
    received = None
    if delivered:
        expected = dict(row_id=job["row_id"],draft=bound["draft"],revision=job["revision"],platform=job["platform"],
                        account=job["account"],planned=bound["planned"],sha=bound["sha"],snapshot=bound["snapshot"])
        received = {k:delivered[k] for k in ("draft","revision","platform","account","planned","sha")}
        received.update(id=delivered["remote_id"], time=delivered["submitted"],
                        matches=all(delivered[k]==v for k,v in expected.items()))
    return {"draft_id":bound["draft"],"sha":bound["sha"],"planned":bound["planned"],
            "snapshot":json.loads(bound["snapshot"]),
            "received":received}

