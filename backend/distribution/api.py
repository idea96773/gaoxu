"""Read models and same-origin local API for the QianFan dry-run console."""
import argparse
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Flask, Response, jsonify, request, send_from_directory

from . import delivery, frozen_reprint, reprint, search, platform_draft
from .scheduling import available_slot, effective_limits
from .engine import close_attempt, lookup_receipt
from .store import BEIJING, DEFAULT_DB, PLATFORMS, booked_slot, current_bookings, import_batch, initialize, operation_key, transaction
from .library import catalog, catalog_page, check_library_root, configure_library, library_settings, match_links, read_selected

def outcome(row, jobs, target_count):
    if row["kind"] != "article":
        return row["kind"]
    states = {j["state"] for j in jobs}
    completed = sum(j["stage"]=="publish" and j["state"]=="done" for j in jobs)
    if completed==target_count:
        return "done"
    if states & {"ready","running"}:
        return "processing"
    if completed:
        return "partial"
    return "failed" if "failed" in states else "cancelled"


def assigned_progress(assignments):
    states={job["display_state"] for job in assignments}
    if states=={"done"}:
        return "done"
    if states & {"ready","running","paused"}:
        return "processing"
    if "unconfirmed" in states:
        return "unconfirmed"
    if "done" in states:
        return "partial"
    return "failed" if "failed" in states else "cancelled"


def delivery_coverage(reviews,bid,detail):
    # Several submitted directories can refer to the same platform job.
    jobs={}
    totals=Counter()
    platforms=defaultdict(Counter)
    for review in reviews:
        for job in review["assignments"]:
            if job["id"] in jobs:
                continue
            jobs[job["id"]]=job
            scope="created" if job["source_batch"]==bid else "reused"
            for counter in (totals,platforms[job["platform"]]):
                counter.update(["total",scope,job["display_state"]])
    coverage=dict(totals=totals,platforms=platforms)
    if detail:
        fields=("id","platform","account","slot","due","finished","receipt","draft","revision","title",
                "display_state","source_batch","source_line")
        coverage["jobs"]=[{k:job[k] for k in fields} for job in
                          sorted(jobs.values(),key=lambda job:(job["slot"],job["id"]))]
    return coverage


def batch_view(db, bid, detail=True):
    batch = db.execute("SELECT * FROM batches WHERE id=?",(bid,)).fetchone()
    if not batch:
        raise KeyError("找不到批次")
    targets = json.loads(batch["targets"])
    rows = db.execute("SELECT * FROM rows WHERE batch=? ORDER BY line",(bid,)).fetchall()
    jobs = db.execute("""SELECT j.* FROM jobs j JOIN rows r ON r.id=j.row_id
                           WHERE r.batch=? ORDER BY j.id""",(bid,)).fetchall()
    grouped = defaultdict(list)
    platforms = defaultdict(Counter)
    failures, tasks = [], []
    for j in jobs:
        if j["stage"] != "deliver":
            grouped[j["row_id"]].append(j)
        if j["stage"]=="publish":
            platforms[j["platform"]][j["state"]]+=1
            if detail:
                tasks.append(dict(j))
        if detail and j["state"]=="failed":
            failures.append(dict(j))
    counts, items = Counter(), []
    assigned = {v[0]: json.loads(v[1]) for v in db.execute(
        "SELECT t.row_id,t.targets FROM row_targets t JOIN rows r ON r.id=t.row_id WHERE r.batch=?", (bid,))}
    expected = 0
    for r in rows:
        n = len(assigned.get(r["id"],targets))
        if r["kind"] == "article":
            expected += n
        state = outcome(r,grouped[r["id"]],n)
        counts[state]+=1
        if detail:
            pending = [j["stage"] for j in grouped[r["id"]] if j["state"] in ("ready","running","failed")]
            items.append(dict(r) | {"status":state,"stage":", ".join(dict.fromkeys(pending))})
    unique = sum(r["kind"]=="article" for r in rows)
    result = {"id":bid,"created":batch["created"],"control":batch["control"],"rules":json.loads(batch["rules"]),
              "targets":targets,"rows_total":len(rows),"unique":unique,"counts":counts,
              "expected":expected,"simulated":sum(c["done"] for c in platforms.values()),
              "public":0,"platforms":platforms}
    if detail:
        result["editorial"] = {r[0]:r[1] for r in db.execute(
            "SELECT d.state,count(*) FROM drafts d JOIN rows r ON r.id=d.row_id WHERE r.batch=? GROUP BY d.state",(bid,))}
        result["has_local"] = bool(db.execute(
            "SELECT 1 FROM rows r JOIN local_inputs i ON i.row_id=r.id WHERE r.batch=? LIMIT 1",(bid,)).fetchone())
        from .editorial import provider_ready
        result["ai_ready"] = provider_ready()
        result["deliveries"] = [dict(x) for x in db.execute("""SELECT j.id,j.row_id,j.platform,j.account,
            j.revision,j.slot,j.due,j.state,j.finished,j.receipt,f.draft AS draft_id,f.sha
            FROM jobs j JOIN frozen_deliveries f ON f.job=j.id JOIN rows r ON r.id=j.row_id
            WHERE r.batch=? ORDER BY j.slot,j.id""",(bid,))]
        result.update(rows=items,failures=failures,publishes=tasks)
    spec=db.execute("""SELECT rb.destination,lr.payload,s.revision FROM reprint_batches rb
        LEFT JOIN reprint_link_rules lr ON lr.batch=rb.batch
        LEFT JOIN reprint_settings_used s ON s.batch=rb.batch WHERE rb.batch=?""",(bid,)).fetchone()
    if spec:
        reviews=reprint.overview(db,bid,sorted({t["platform"] for t in targets}) if detail else ())
        counts=Counter()
        platforms=defaultdict(Counter)
        for review in reviews:
            for a in review["assignments"]:
                if not a["reused"]:
                    platforms[a["platform"]][a["state"]]+=1
            state=review["state"]
            if review["assignments"]:
                review["delivery_progress"]=assigned_progress(review["assignments"])
                if state=="approved":
                    state=review["delivery_progress"]
            elif state in ("pending","preparing","invalid","returned","scheduling") and batch["control"]=="cancelled":
                state="cancelled"
            elif state=="preparing" and review["prepare_state"]=="running":
                state="reading"
            review["progress"]=state
            counts[state]+=1
        result.update(reprint=True,destination=spec["destination"],saved_revision=spec["revision"],counts=counts,platforms=platforms,
                      delivery_coverage=delivery_coverage(reviews,bid,detail),
                      unique=len(reviews)-counts["duplicate"]-counts["invalid"],
                      simulated=sum(c["done"] for c in platforms.values()),
                      expected=sum(sum(c.values()) for c in platforms.values()))
        if detail:
            result["links"]=json.loads(spec["payload"] or "{}")
            result["reviews"]=reviews
            result["target_limits"]=[t|asdict(effective_limits(result["rules"],t["platform"],t["account"])) for t in targets]
    return result

def cancel_pending(path,db,job,now,scope):
    jid=job["id"]
    stage=job["stage"]
    prior=None
    if stage=="publish":
        prior=lookup_receipt(path,operation_key(job))
    elif stage=="deliver":
        prior=delivery.receipt(path,db,job)
    if job["receipt"] and (not prior or prior["remote_id"]!=job["receipt"]):
        raise ValueError("本地回执与接收记录无法核对，请先核对任务")
    if prior:
        db.execute("""UPDATE jobs SET state='done',receipt=?,finished=?,failure='',token=NULL,
                   lease_until=NULL WHERE id=?""",(prior["remote_id"],prior["submitted"],jid))
        db.execute("UPDATE attempts SET outcome='done',reason='',ended=? WHERE job=? AND ended IS NULL",(now,jid))
        return "done"
    db.execute("UPDATE jobs SET state='cancelled',token=NULL,lease_until=NULL WHERE id=?",(jid,))
    db.execute("UPDATE attempts SET outcome='cancelled',ended=? WHERE job=? AND ended IS NULL",(now,jid))
    if stage in ("publish","deliver"):
        db.execute("INSERT INTO publish_cancellations VALUES(?,?,?)",(jid,now,scope))
    if stage=="deliver" and scope=="task":
        db.execute("""UPDATE platform_drafts SET state='cancelled',reason='关联排期已取消',updated=?
                   WHERE job=? AND state IN ('queued','preparing')""",(now,jid))
    return "cancelled"


def cancel_guard(job):
    if job["stage"] in ("publish","deliver") and job["state"] in ("ready","running","failed"):
        return {key:job[key] for key in ("state","attempts","slot","due")}
    return None


def control_batch(path,bid,action,now):
    if action not in ("pause","resume","cancel"):
        raise ValueError("未知操作")
    with transaction(path) as db:
        b=db.execute("SELECT control FROM batches WHERE id=?",(bid,)).fetchone()
        if not b:
            raise KeyError("找不到批次")
        if b[0]=="cancelled" and action!="cancel":
            raise ValueError("已取消的批次无法继续")
        db.execute("UPDATE batches SET control=? WHERE id=?",({"pause":"paused","resume":"active","cancel":"cancelled"}[action],bid))
        if action=="cancel":
            db.execute("""UPDATE platform_drafts SET state='cancelled',reason='所属批次已取消',updated=? WHERE state IN ('queued','preparing')
                AND job IN (SELECT j.id FROM jobs j JOIN rows r ON r.id=j.row_id WHERE r.batch=?)""",(now,bid))
            affected=db.execute("""SELECT j.*,r.canonical FROM jobs j JOIN rows r ON r.id=j.row_id
                  WHERE r.batch=? AND j.state IN ('ready','running','failed')""",(bid,)).fetchall()
            for j in affected:
                try:
                    cancel_pending(path,db,j,now,"batch")
                except ValueError as exc:
                    if j["state"]=="running":
                        close_attempt(db,j,now,"failed",str(exc))
                    else:
                        db.execute("UPDATE jobs SET state='failed',failure=? WHERE id=?",(str(exc),j["id"]))

def scheduling_job(db,jid):
    job=db.execute("""SELECT j.*,b.control,b.rules,r.canonical,r.batch FROM jobs j JOIN rows r ON r.id=j.row_id
        JOIN batches b ON b.id=r.batch WHERE j.id=?""",(jid,)).fetchone()
    if not job:
        raise KeyError("找不到任务")
    return job


def check_movable(path,db,job):
    if job["stage"] not in ("publish","deliver") or job["state"]!="ready" or job["control"]=="cancelled":
        raise ValueError("只能改期尚未领取的发布或冻结稿交付任务")
    if job["stage"]=="deliver":
        received=delivery.receipt(path,db,job)
    else:
        received=job["attempts"] and lookup_receipt(path,operation_key(job))
    if received:
        raise ValueError("接收端已有回执，请刷新任务核对交付结果")


def reschedule_candidate(db,job,stamp,now):
    if type(stamp) is not int or not now<=stamp<=now+366*86400:
        raise ValueError("请选择未来一年内的北京时间")
    slot=booked_slot(db,job["stage"],job["platform"],job["account"],stamp,json.loads(job["rules"]),job["id"])
    if slot>now+366*86400:
        raise ValueError("未来一年内没有满足当前账号和平台规则的空档")
    return slot


def job_action(path,jid,action,data,now):
    with transaction(path) as db:
        j=scheduling_job(db,jid)
        if action=="cancel":
            if j["stage"] not in ("publish","deliver"):
                raise ValueError("只能单独取消平台发布或冻结稿交付任务")
            if j["state"] in ("done","cancelled"):
                return {"state":j["state"]}
            expected=data.get("expected")
            guard=cancel_guard(j)
            if (not guard or not isinstance(expected,dict) or expected!=guard
                    or any(type(expected[k]) is not type(v) for k,v in guard.items())):
                raise ValueError("任务状态、排期或执行次数已变化，请刷新任务后确认")
            return {"state":cancel_pending(path,db,j,now,"task")}
        if j["control"]=="cancelled":
            raise ValueError("批次已取消")
        if action=="retry":
            if "expected_attempt" in data:
                if (type(data["expected_attempt"]) is not int or j["attempts"]!=data["expected_attempt"]
                        or j["state"]!="failed" or data.get("batch",j["batch"])!=j["batch"]):
                    raise ValueError("任务状态或失败记录已变化，请刷新")
            if j["state"]=="done":
                return
            if j["state"]!="failed":
                raise ValueError("只能重试失败的步骤")
            if j["stage"]=="reprint_schedule":
                raise ValueError("请在「转载审核与分稿」核对当前版本与账号，再确认这一篇的排期")
            db.execute("UPDATE jobs SET state='ready',due=?,failure='' WHERE id=?",(now,jid))
            if j["stage"]=="reprint_prepare":
                db.execute("UPDATE rows SET note='' WHERE id=?",(j["row_id"],))
                db.execute("UPDATE reprint_reviews SET state='preparing' WHERE row_id=? AND state='invalid'",(j["row_id"],))
        elif action=="reschedule":
            check_movable(path,db,j)
            stamp=data.get("time")
            frozen=j["stage"]=="deliver"
            if frozen and (type(data.get("expected_slot")) is not int or type(stamp) is not int):
                raise ValueError("请刷新后选择新的北京时间")
            if frozen and j["slot"]==stamp and "expected_available" not in data:
                return {"slot":stamp}
            expected={"expected_slot":j["slot"], "expected_execution":max(j["slot"],j["due"])}
            if any(key in data and (type(data[key]) is not int or data[key]!=value)
                   for key,value in expected.items()):
                raise ValueError("排期或顺延时间已变化，请刷新任务后改期")
            slot=reschedule_candidate(db,j,stamp,now)
            if frozen or "expected_available" in data:
                accepted=data.get("expected_available",stamp)
                if type(accepted) is not int or accepted!=stamp or accepted!=slot:
                    when=datetime.fromtimestamp(slot,BEIJING).strftime("%Y-%m-%d %H:%M:%S")
                    raise ValueError("该时刻与现有排期规则冲突，可用北京时间 "+when)
            if frozen and slot==j["slot"]:
                return {"slot":slot}
            if frozen:
                delivery.apply_move(db,j,slot,now)
            else:
                db.execute("UPDATE jobs SET slot=?,due=? WHERE id=?",(slot,slot,jid))
            return {"slot":slot}
        else:
            raise ValueError("未知操作")

def make_blueprint(path,fixed_root):
    api=Blueprint("distribution",__name__,url_prefix="/api/distribution")
    @api.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)),400
    @api.errorhandler(KeyError)
    def missing(exc):
        return jsonify(error=exc.args[0]),404
    def body():
        data=request.get_json()
        if not isinstance(data,dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data
    from .runtime import health
    @api.get("/health")
    def heartbeat():
        result=health(path)
        if platform_draft.local_console_request():
            result["database"]=str(Path(path).resolve())
        return jsonify(result)

    @api.get("/runtime-status")
    def runtime_status():
        return jsonify(health(path,details=True))

    @api.get("/windows-startup")
    def windows_startup():
        if not platform_draft.local_console_request():
            raise ValueError("请从本机稿序管理台查看 Windows 配置")
        from .runtime import windows_startup_status
        return jsonify(windows_startup_status(path))

    @api.post("/backup-settings")
    def backup_settings():
        from .daily_backup import configure
        return jsonify(configure(path,body()))

    @api.get("/batches")
    def batches():
        with transaction(path,False) as db:
            if request.args.get("summary")=="1":
                summary="""SELECT b.id,b.created,(SELECT count(*) FROM rows r WHERE r.batch=b.id) rows_total
                           FROM batches b"""
                records=[dict(row) for row in db.execute(summary+" ORDER BY b.created DESC,b.rowid DESC LIMIT 200")]
                selected=request.args.get("include")
                if selected and not any(b["id"]==selected for b in records):
                    pinned=db.execute(summary+" WHERE b.id=?",(selected,)).fetchone()
                    if pinned:
                        records.append(dict(pinned))
            else:
                recent=db.execute("SELECT id FROM batches ORDER BY created DESC,rowid DESC LIMIT 200")
                records=[batch_view(db,batch["id"],False) for batch in recent]
            seen=db.execute("SELECT seen FROM health WHERE name='worker'").fetchone()
            now=int(time.time())
            return jsonify(batches=records,platforms=PLATFORMS,mode="dry-run",checked=now,
                           overview=search.overview(db,now),worker_seen=seen[0] if seen else None)
    @api.post("/batches")
    def create():
        return jsonify(id=import_batch(path,body())),201
    @api.post("/folders")
    def folders():
        from .folders import parse_folders
        # Non-simple header prevents third-party forms from invoking the local upload.
        if request.headers.get("X-Distribution-Import")!="folder":
            raise ValueError("需要从分发中心选择目录")
        inputs = parse_folders((f.filename,f.stream) for f in request.files.getlist("files"))
        options = json.loads(request.form.get("options","{}"))
        if not isinstance(options,dict):
            raise ValueError("导入设置无效")
        options["urls"]=[f["url"] for f in inputs]
        return jsonify(id=import_batch(path,options,folders=inputs)),201
    @api.get("/library-settings")
    def read_library_settings():
        return jsonify(library_settings(path,fixed_root))

    @api.post("/library-settings")
    def update_library_settings():
        return jsonify(configure_library(path,fixed_root,body()))

    def current_root():
        return library_settings(path,fixed_root)["root"]

    @api.get("/library")
    def library():
        root=current_root()
        if any(k in request.args for k in ("source_name","keyword","start","end","order","page","page_size")):
            return jsonify(root=str(root),**catalog_page(path,root,request.args))
        return jsonify(root=str(root),items=catalog(path,root))
    @api.post("/library")
    def import_library():
        options=body()
        root=current_root()
        check_library_root(root,options)
        inputs=read_selected(root,options.get("directories"))
        options["urls"]=[f["url"] for f in inputs]
        return jsonify(id=import_batch(path,options,folders=inputs)),201

    @api.post("/library/match-links")
    def match_downloaded_links():
        return jsonify(match_links(path,current_root(),body(),include_catalog=request.args.get("catalog")=="1"))
    @api.get("/media/<sha>")
    def media(sha):
        with transaction(path,False) as db:
            asset=db.execute("SELECT mime,data FROM media WHERE sha=?",(sha,)).fetchone()
            if not asset:
                raise KeyError("找不到本地图片")
            return Response(asset["data"],mimetype=asset["mime"],headers={"X-Content-Type-Options":"nosniff"})
    @api.get("/batches/<bid>")
    def batch(bid):
        with transaction(path,False) as db:
            return jsonify(batch_view(db,bid))
    @api.post("/batches/<bid>/<action>")
    def control(bid,action):
        body()
        control_batch(path,bid,action,int(time.time()))
        return jsonify(ok=True)
    @api.get("/articles/<int:rid>")
    def article(rid):
        with transaction(path,False) as db:
            row=db.execute("SELECT * FROM rows WHERE id=?",(rid,)).fetchone()
            if not row:
                raise KeyError("找不到文章")
            source=row["owner"] or rid
            doc=db.execute("SELECT payload,analysis FROM documents WHERE row_id=?",(source,)).fetchone()
            variants=db.execute("SELECT * FROM variants WHERE row_id=? ORDER BY platform,revision",(source,)).fetchall()
            jobs=db.execute("SELECT * FROM jobs WHERE row_id=? ORDER BY id",(source,)).fetchall()
            return jsonify(row=dict(row),document=json.loads(doc[0]) if doc else None,
                analysis=json.loads(doc[1]) if doc and doc[1] else None,
                variants=[dict(v)|{"payload":json.loads(v["payload"])} for v in variants],jobs=[dict(j) for j in jobs])
    @api.post("/editorial/<int:did>/schedule")
    def schedule_frozen(did):
        result=delivery.schedule(path,did,body(),int(time.time()))
        return jsonify(result),201 if result["created"] else 200

    @api.get("/jobs/<int:jid>")
    def job(jid):
        with transaction(path,False) as db:
            j=db.execute("""SELECT j.*,r.url,r.batch FROM jobs j JOIN rows r ON r.id=j.row_id WHERE j.id=?""",(jid,)).fetchone()
            if not j:
                raise KeyError("找不到任务")
            history=db.execute("SELECT seq,started,ended,outcome,reason FROM attempts WHERE job=? ORDER BY seq",(jid,)).fetchall()
            cancelled=db.execute("SELECT cancelled_at,scope FROM publish_cancellations WHERE job=?",(jid,)).fetchone()
            received=None
            if j["stage"]=="reprint_schedule":
                record=db.execute("SELECT confirmed,decision FROM reprint_schedule_inputs WHERE job=?",(jid,)).fetchone()
                if record:
                    received=dict(confirmed=record["confirmed"],**json.loads(record["decision"]))
            return jsonify(job=dict(j),delivery=delivery.details(path,db,j) if j["stage"]=="deliver" else None,
                           review_decision=received,history=[dict(r) for r in history],cancel_guard=cancel_guard(j),
                           cancellation=dict(cancelled) if cancelled else None,mode="dry-run")
    @api.get("/jobs/<int:jid>/available-slot")
    def available_time(jid):
        with transaction(path,False) as db:
            job=scheduling_job(db,jid)
            check_movable(path,db,job)
            stamp=request.args.get("time",type=int)
            slot=reschedule_candidate(db,job,stamp,int(time.time()))
            return jsonify(job=jid,requested=stamp,available=slot,expected_slot=job["slot"],
                           expected_execution=max(job["slot"],job["due"])),200,{"Cache-Control":"no-store"}

    @api.get("/jobs/<int:jid>/reprint-check")
    def check_reprint(jid):
        with transaction(path,False) as db:
            report=frozen_reprint.inspect(db,frozen_reprint.source(db,jid))
            return jsonify(report),200,{"Cache-Control":"no-store"}

    @api.post("/batches/<bid>/postpone/<action>")
    def postpone_batch(bid,action):
        if action not in ("preview","confirm"):
            raise ValueError("未知的批量改期操作")
        data=body()
        start,items=data.get("start"),data.get("items")
        now=int(time.time())
        if type(start) is not int or not now<=start<=now+366*86400:
            raise ValueError("请选择未来一年内的顺延起点（北京时间）")
        if not isinstance(items,list) or not 1<=len(items)<=200:
            raise ValueError("每次可顺延 1 至 200 个冻结稿任务")
        if not all(isinstance(i,dict) and type(i.get("id")) is int and 0<i["id"]<=2**63-1 for i in items):
            raise ValueError("交付任务编号无效")
        if len({i["id"] for i in items})!=len(items):
            raise ValueError("同一交付任务只能选择一次")
        confirm=action=="confirm"
        results=[]
        bookings={}
        with transaction(path,confirm) as db:
            for item in items:
                jid=item["id"]
                try:
                    job=scheduling_job(db,jid)
                    if job["batch"]!=bid or job["stage"]!="deliver":
                        raise ValueError("请选择本批新建的冻结稿交付任务")
                    check_movable(path,db,job)
                    old=job["slot"]
                    due=max(old,job["due"])
                    moves=db.execute("SELECT id,before_slot,after_slot FROM delivery_reschedules WHERE job=? ORDER BY id DESC LIMIT 2",
                                     (jid,)).fetchall()
                    previous=moves[0]["id"] if moves else 0
                    guard=dict(slot=old,execution=due,move=previous,attempts=job["attempts"])
                    if confirm:
                        expected=item.get("expected")
                        if not isinstance(expected,dict) or expected.keys()!=guard.keys() or any(type(v) is not int for v in expected.values()):
                            raise ValueError("请先预览本批交付任务")
                        if (moves and type(item.get("time")) is int and start<=item["time"]==old==due==moves[0]["after_slot"]
                                and expected["slot"]==moves[0]["before_slot"] and expected["execution"]<start
                                and expected["move"]==(moves[1]["id"] if len(moves)>1 else 0)):
                            results.append(dict(id=jid,time=old,already=True))
                            continue
                        if expected!=guard:
                            raise ValueError("任务自预览后已变化，请刷新排期")
                    if due>=start:
                        raise ValueError("原执行时间已达到顺延起点")
                    platform=job["platform"]
                    if platform not in bookings:
                        bookings[platform]=[dict(r) for r in current_bookings(db,"deliver",platform,start)]
                    peers=[r for r in bookings[platform] if r.get("id")!=jid]
                    requested=item.get("time") if confirm else start
                    if type(requested) is not int or not start<=requested<=now+366*86400:
                        raise ValueError("预览时间无效，请重新试算")
                    slot=available_slot(requested,platform,job["account"],json.loads(job["rules"]),peers)
                    if slot>now+366*86400:
                        raise ValueError("未来一年内没有可用时段")
                    if confirm:
                        if slot!=requested:
                            raise ValueError("预览时段已被占用，请重新试算")
                        delivery.apply_move(db,job,slot,now)
                    bookings[platform]=peers+[dict(id=jid,account=job["account"],mark=slot,rules=job["rules"])]
                    results.append(dict(id=jid,time=slot,expected=guard))
                except (ValueError,KeyError) as exc:
                    results.append(dict(id=jid,error=exc.args[0]))
        return jsonify(items=results)

    @api.post("/failed-jobs/retry")
    def retry_failed_jobs():
        items=body().get("items")
        if not isinstance(items,list) or not 1<=len(items)<=100:
            raise ValueError("请选择 1 至 100 个失败任务")
        results=[]
        for item in items:
            job_id=item.get("id") if isinstance(item,dict) else None
            try:
                if (type(job_id) is not int or not 0<job_id<2**63 or type(item.get("expected_attempt")) is not int
                        or not isinstance(item.get("batch"),str)):
                    raise ValueError("缺少任务编号、所属批次或失败次数")
                job_action(path,job_id,"retry",item,int(time.time()))
                results.append(dict(id=job_id,queued=True))
            except (ValueError,KeyError) as exc:
                results.append(dict(id=job_id,queued=False,reason=str(exc)))
        return jsonify(items=results)

    @api.post("/jobs/<int:jid>/<action>")
    def change_job(jid,action):
        result=job_action(path,jid,action,body(),int(time.time()))
        return jsonify(ok=True,**(result or {}))
    from .editorial import register
    register(api,path,body)
    reprint.register(api,path,current_root,body)
    search.register(api,path)
    from . import agent_runs
    agent_runs.register(api,path)
    platform_draft.register(api,path,body)
    return api

def create_app(path=DEFAULT_DB,library_root=None):
    initialize(path)
    app=Flask(__name__,static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=260*1024*1024,MAX_FORM_PARTS=12000)
    app.register_blueprint(make_blueprint(path,library_root or os.getenv("DIST_LIBRARY")))
    root=Path(__file__).resolve().parents[2]/"frontend"/"dist"
    @app.get("/")
    def home():
        return send_from_directory(root,"index.html")
    @app.get("/<path:name>")
    def assets(name):
        return send_from_directory(root,name)
    return app

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--db",default=os.getenv("DIST_DB",str(DEFAULT_DB)))
    parser.add_argument("--port",type=int,default=5419)
    args=parser.parse_args()
    from .runtime import serve_console
    serve_console(create_app(args.db),args.port)
