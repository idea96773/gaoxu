"""Delivery of approved editorial snapshots to the local, persistent receiver."""
import json
from datetime import datetime
from itertools import chain

from . import rights
from .reprint_approval import origin

from .scheduling import available_slot
from .store import BEIJING, booked_slot, compact, delivery_key, digest, ledger_path, reserved_peers, transaction


def existing_deliveries(db, identities):
    """Return the earliest uncancelled frozen job for each source and platform."""
    keys=list(dict.fromkeys(key for key in identities if key))
    by_source={}
    for start in range(0,len(keys),400):
        group=keys[start:start+400]
        placeholders=",".join("?" for _ in group)
        rows=db.execute(f"""SELECT r.canonical,r.batch,j.id,j.platform,j.account,j.state,j.slot,b.control
            FROM rows r JOIN jobs j ON j.row_id=r.id JOIN frozen_deliveries f ON f.job=j.id
            JOIN batches b ON b.id=r.batch WHERE r.canonical IN ({placeholders})
            AND j.stage='deliver' AND j.state!='cancelled' ORDER BY j.id""",group)
        for row in rows:
            by_source.setdefault(row["canonical"],{}).setdefault(row["platform"],dict(row))
    return by_source


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
    slot = free_slot(db,draft["platform"],draft["account"],requested or now,limits)
    if requested is not None and requested != slot:
        when = datetime.fromtimestamp(slot, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        raise ValueError("所选排期与时间窗、间隔或日上限冲突；可选北京时间 " + when)
    job = db.execute("""INSERT INTO jobs(row_id,stage,platform,account,revision,due,slot)
        VALUES(?,'deliver',?,?,?,?,?)""", (*target,draft["rev"],slot,slot)).lastrowid
    payload = draft["frozen"]
    sha=digest(payload)
    db.execute("INSERT INTO frozen_deliveries VALUES(?,?,?,?,?,?)",
               (job,draft_id,requested,slot,payload,sha))
    if draft["provider"]=="reprint":
        rights.record(db,job,sha,"local","",now)
    if draft["provider"]=="reprint" and draft["platform"]=="zhihu":
        db.execute("""INSERT INTO zhihu_draft_recipients(job,user_id,name)
            SELECT ?,user_id,name FROM zhihu_draft_bindings WHERE alias=? AND user_id IS NOT NULL""",
            (job,draft["account"]))
    return {"id":job, "slot":slot, "state":"ready", "created":True}


def free_slot(db, platform, account, stamp, limits, exclude=0):
    return booked_slot(db,"deliver",platform,account,stamp,limits,exclude)


def apply_move(db,job,slot,now):
    previous=job["slot"]
    bound=db.execute("UPDATE frozen_deliveries SET planned=? WHERE job=? AND planned=?",
                     (slot,job["id"],previous))
    if bound.rowcount!=1:
        raise ValueError("任务排期与冻结绑定不一致")
    db.execute("UPDATE jobs SET slot=?,due=? WHERE id=?",(slot,slot,job["id"]))
    db.execute("INSERT INTO delivery_reschedules(job,before_slot,after_slot,changed) VALUES(?,?,?,?)",
               (job["id"],previous,slot,now))


def confirmed_receipt(remote,db,job):
    accepted=remote.execute("SELECT * FROM delivered_drafts WHERE key=?",(delivery_key(job),)).fetchone()
    if not accepted:
        return None
    bound=db.execute("""SELECT f.draft,f.planned,f.sha,f.snapshot,d.frozen,d.state,d.rev,d.row_id,d.platform,d.account
        FROM frozen_deliveries f JOIN drafts d ON d.id=f.draft WHERE f.job=?""",(job["id"],)).fetchone()
    if (not bound or any(accepted[k]!=job[k] for k in ("row_id","platform","account","revision"))
            or any(accepted[k]!=bound[k] for k in ("draft","planned","sha"))
            or bound["state"]!="approved" or bound["rev"]!=job["revision"]
            or any(bound[k]!=job[k] for k in ("row_id","platform","account"))
            or not bound["frozen"]==bound["snapshot"]==accepted["snapshot"]
            or job["slot"]!=bound["planned"] or digest(accepted["snapshot"])!=bound["sha"]):
        raise ValueError("模拟回执与本任务的冻结稿、账号或排期不一致，请人工核对")
    return accepted


def receipt(path, db, job):
    with transaction(ledger_path(path),False) as remote:
        return confirmed_receipt(remote,db,job)


def frozen_binding(db, job):
    bound = db.execute("""SELECT f.*, d.state, d.frozen, d.rev, d.platform, d.account, d.row_id,d.provider
        FROM frozen_deliveries f JOIN drafts d ON d.id=f.draft WHERE f.job=?""",(job["id"],)).fetchone()
    if not bound or bound["state"]!="approved" or bound["frozen"]!=bound["snapshot"] or digest(bound["snapshot"])!=bound["sha"]:
        raise ValueError("冻结快照校验失败")
    if (bound["row_id"],bound["platform"],bound["account"],bound["rev"],bound["planned"]) != (
            job["row_id"],job["platform"],job["account"],job["revision"],job["slot"]):
        raise ValueError("任务账号、版本或排期与冻结绑定不一致")
    if bound["provider"]=="reprint":
        return dict(bound,approval=origin(db,job["row_id"],job["platform"],bound["sha"],bound["rev"]))
    return dict(bound,approval=None)


def deliver(path, db, job, now):
    bound = frozen_binding(db,job)
    key = delivery_key(job)
    with transaction(ledger_path(path)) as remote:
        existing = confirmed_receipt(remote,db,job)
        if existing:
            return existing["remote_id"], existing["submitted"]
        stamp=max(job["slot"],job["due"],now)
        confirmed=remote.execute("""SELECT row_id,submitted mark,account FROM delivered_drafts
            WHERE platform=? AND submitted>=?""",(job["platform"],stamp-86400)).fetchall()
        ids=[r["row_id"] for r in confirmed]
        rule_rows=db.execute("""SELECT r.id,b.rules FROM rows r JOIN batches b ON b.id=r.batch
            WHERE r.id IN (SELECT value FROM json_each(?))""",(compact(ids),))
        prior_rules=dict(rule_rows)
        if any(rid not in prior_rules for rid in ids):
            raise ValueError("模拟回执关联的批次缺失，请核对数据库与备份")
        accepted=(dict(r)|{"rules":prior_rules[r["row_id"]]} for r in confirmed)
        accepted_rows={(r["row_id"],r["account"]) for r in confirmed}
        reservations=(r for r in reserved_peers(db,job,stamp) if (r["row_id"],r["account"]) not in accepted_rows)
        due=available_slot(stamp,job["platform"],job["account"],json.loads(job["rules"]),chain(accepted,reservations))
        if due>now:
            return None,due
        remote_id = "draft-sim-"+key[:24]
        remote.execute("INSERT INTO delivered_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (key,remote_id,now,job["row_id"],bound["draft"],job["revision"],job["platform"],
                        job["account"],bound["planned"],bound["sha"],bound["snapshot"]))
        return remote_id,now


def details(path, db, job):
    bound=db.execute("""SELECT f.*,d.provider,d.rev FROM frozen_deliveries f JOIN drafts d ON d.id=f.draft
        WHERE f.job=?""",(job["id"],)).fetchone()
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
    approval=None
    if bound["provider"]=="reprint":
        try:
            approval=origin(db,job["row_id"],job["platform"],bound["sha"],bound["rev"])
        except ValueError as exc:
            approval={"error":str(exc)}
    return {"draft_id":bound["draft"],"sha":bound["sha"],"planned":bound["planned"],"approval":approval,
            "snapshot":json.loads(bound["snapshot"]),
            "received":received, "reschedules":[dict(r) for r in db.execute(
                "SELECT before_slot,after_slot,changed FROM delivery_reschedules WHERE job=? ORDER BY id",(job["id"],))]}

