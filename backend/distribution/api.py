"""Read models and same-origin local API for the QianFan dry-run console."""
import argparse
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from flask import Blueprint, Flask, Response, jsonify, request, send_from_directory
from waitress import serve

from . import delivery, reprint, search, platform_draft
from .scheduling import effective_limits
from .engine import close_attempt, lookup_receipt, operation_key
from .store import DEFAULT_DB, PLATFORMS, import_batch, initialize, next_slot, transaction
from .library import DEFAULT_LIBRARY, catalog, read_selected

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
    spec=db.execute("SELECT destination FROM reprint_batches WHERE batch=?",(bid,)).fetchone()
    if spec:
        reviews=reprint.overview(db,bid)
        counts=Counter()
        platforms=defaultdict(Counter)
        for review in reviews:
            states=[a["state"] for a in review["assignments"] if not a["reused"]]
            for a in review["assignments"]:
                if not a["reused"]:
                    platforms[a["platform"]][a["state"]]+=1
            state=review["state"]
            if states:
                if all(s=="done" for s in states):
                    state="done"
                elif any(s in ("ready","running") for s in states):
                    state="processing"
                else:
                    state="partial" if "done" in states else ("failed" if "failed" in states else "cancelled")
            elif state in ("pending","preparing","invalid") and batch["control"]=="cancelled":
                state="cancelled"
            elif state=="preparing" and review["prepare_state"]=="running":
                state="reading"
            review["progress"]=state
            counts[state]+=1
        result.update(reprint=True,destination=spec[0],counts=counts,platforms=platforms,
                      unique=len(reviews)-counts["duplicate"]-counts["invalid"],
                      simulated=sum(c["done"] for c in platforms.values()),
                      expected=sum(sum(c.values()) for c in platforms.values()))
        if detail:
            result["reviews"]=reviews
            result["target_limits"]=[t|asdict(effective_limits(result["rules"],t["platform"],t["account"])) for t in targets]
    return result

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
            db.execute("""UPDATE platform_drafts SET state='cancelled',updated=? WHERE state IN ('queued','preparing')
                AND job IN (SELECT j.id FROM jobs j JOIN rows r ON r.id=j.row_id WHERE r.batch=?)""",(now,bid))
            affected=db.execute("""SELECT j.*,r.canonical FROM jobs j JOIN rows r ON r.id=j.row_id
                  WHERE r.batch=? AND j.state IN ('ready','running','failed')""",(bid,)).fetchall()
            for j in affected:
                prior=lookup_receipt(path,operation_key(j)) if j["stage"]=="publish" else None
                if j["stage"] == "deliver":
                    prior = delivery.receipt(path,j)
                if prior:
                    db.execute("UPDATE jobs SET receipt=? WHERE id=?",(prior["remote_id"],j["id"]))
                    close_attempt(db,j,now,"done",received_at=prior["submitted"])
                else:
                    db.execute("UPDATE jobs SET state='cancelled',token=NULL,lease_until=NULL WHERE id=?",(j["id"],))
                    db.execute("UPDATE attempts SET outcome='cancelled',ended=? WHERE job=? AND ended IS NULL",(now,j["id"]))

def job_action(path,jid,action,data,now):
    with transaction(path) as db:
        j=db.execute("""SELECT j.*,b.control,b.rules,r.canonical FROM jobs j JOIN rows r ON r.id=j.row_id
             JOIN batches b ON b.id=r.batch WHERE j.id=?""",(jid,)).fetchone()
        if not j:
            raise KeyError("找不到任务")
        if j["control"]=="cancelled":
            raise ValueError("批次已取消")
        if action=="retry":
            if j["state"]=="done":
                return
            if j["state"]!="failed":
                raise ValueError("只能重试失败的步骤")
            db.execute("UPDATE jobs SET state='ready',due=?,failure='' WHERE id=?",(now,jid))
            if j["stage"]=="reprint_prepare":
                db.execute("UPDATE rows SET note='' WHERE id=?",(j["row_id"],))
                db.execute("UPDATE reprint_reviews SET state='preparing' WHERE row_id=? AND state='invalid'",(j["row_id"],))
        elif action=="reschedule":
            if j["stage"]=="deliver":
                return delivery.move_slot(path,db,j,data,now)
            if j["stage"]!="publish" or j["state"]!="ready":
                raise ValueError("只能改期尚未领取的发布任务")
            stamp=data.get("time")
            if type(stamp) is not int or not now<=stamp<=now+366*86400:
                raise ValueError("新时间应在未来一年内")
            slot=next_slot(db,j["platform"],j["account"],stamp,json.loads(j["rules"]),jid)
            db.execute("UPDATE jobs SET slot=?,due=? WHERE id=?",(slot,slot,jid))
        else:
            raise ValueError("未知操作")

def make_blueprint(path,root):
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
        return jsonify(health(path))

    @api.get("/runtime-status")
    def runtime_status():
        return jsonify(health(path,details=True))

    @api.get("/batches")
    def batches():
        with transaction(path,False) as db:
            recent=db.execute("SELECT id,created FROM batches ORDER BY created DESC,rowid DESC LIMIT 200").fetchall()
            if request.args.get("summary")=="1":
                records=[dict(batch)|{"rows_total":db.execute("SELECT count(*) FROM rows WHERE batch=?",
                         (batch["id"],)).fetchone()[0]} for batch in recent]
            else:
                records=[batch_view(db,batch["id"],False) for batch in recent]
            seen=db.execute("SELECT seen FROM health WHERE name='worker'").fetchone()
            return jsonify(batches=records,platforms=PLATFORMS,mode="dry-run",checked=int(time.time()),
                           worker_seen=seen[0] if seen else None)
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
    @api.get("/library")
    def library():
        return jsonify(root=str(root),items=catalog(path,root))
    @api.post("/library")
    def import_library():
        options=body()
        inputs=read_selected(root,options.get("directories"))
        options["urls"]=[f["url"] for f in inputs]
        return jsonify(id=import_batch(path,options,folders=inputs)),201
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
            return jsonify(job=dict(j),delivery=delivery.details(path,db,j) if j["stage"]=="deliver" else None,history=[dict(r) for r in history],mode="dry-run")
    @api.post("/jobs/<int:jid>/<action>")
    def change_job(jid,action):
        job_action(path,jid,action,body(),int(time.time()))
        return jsonify(ok=True)
    from .editorial import register
    register(api,path,body)
    reprint.register(api,path,root,body)
    search.register(api,path)
    platform_draft.register(api,path,body)
    return api

def create_app(path=DEFAULT_DB,library_root=None):
    initialize(path)
    app=Flask(__name__,static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=260*1024*1024,MAX_FORM_PARTS=12000)
    app.register_blueprint(make_blueprint(path,Path(library_root or os.getenv("DIST_LIBRARY") or DEFAULT_LIBRARY)))
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
    serve(create_app(args.db),host="127.0.0.1",port=args.port,max_request_body_size=260*1024*1024)
