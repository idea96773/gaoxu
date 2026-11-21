"""Durable stages. All content and receipts in this module are simulated."""
import argparse
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .delivery import deliver
from .store import (BEIJING, DEFAULT_DB, PLATFORMS, compact, digest, enqueue, initialize,
                    ledger_path, next_slot, transaction)

STAGES = ("ingest","normalize","analyze","adapt","check","plan","publish")
IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0pQAAAAASUVORK5CYII="
class SimulatedFailure(Exception):
    pass

def claim(path, now, lease=30):
    with transaction(path) as db:
        # A recovered lease never invalidates a completed checkpoint.
        expired = db.execute("SELECT id FROM jobs WHERE state='running' AND lease_until<=?",(now,)).fetchall()
        for row in expired:
            db.execute("UPDATE attempts SET ended=?,outcome='interrupted' WHERE job=? AND ended IS NULL",
                       (now,row["id"]))
            db.execute("UPDATE jobs SET state='ready',token=NULL WHERE id=?",(row["id"],))
        job = db.execute("""SELECT j.id FROM jobs j JOIN rows r ON r.id=j.row_id
              JOIN batches b ON b.id=r.batch WHERE j.state='ready' AND j.due<=? AND b.control='active'
              ORDER BY j.due,j.id LIMIT 1""",(now,)).fetchone()
        if not job:
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE jobs SET state='running',token=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                   (token,now+lease,job["id"]))
        db.execute("""INSERT INTO attempts(job,seq,started,outcome) SELECT id,attempts,?,'running'
                      FROM jobs WHERE id=?""",(now,job["id"]))
        return job["id"], token

def close_attempt(db, j, now, state, reason="", due=None, received_at=None):
    db.execute("""UPDATE jobs SET state=?,failure=?,token=NULL,lease_until=NULL,due=coalesce(?,due),
                finished=CASE WHEN ?='done' THEN ? ELSE NULL END WHERE id=?""",
               (state,reason,due,state,now if received_at is None else received_at,j["id"]))
    db.execute("UPDATE attempts SET ended=?,outcome=?,reason=? WHERE job=? AND seq=?",
               (now,state,reason,j["id"],j["attempts"]))

def simulated_receipt(path, key, now):
    with transaction(ledger_path(path)) as ledger:
        ledger.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?)",(key,"sim-"+digest(key)[:24],now))
        return dict(ledger.execute("SELECT * FROM receipts WHERE key=?",(key,)).fetchone())

def lookup_receipt(path, key):
    with transaction(ledger_path(path),False) as ledger:
        return ledger.execute("SELECT * FROM receipts WHERE key=?",(key,)).fetchone()

def operation_key(j):
    return digest(compact(["dry-run",j["canonical"],j["platform"],j["account"]]))

def sample_document(j, now):
    label = digest(j["canonical"])[:8]
    title = f"样本 {label} · 社区阅读活动"
    body = "社区图书馆安排了一场读书交流。参与者可以分享一本书，并留下阅读笔记。"
    return {"article_id": f"article-{j['row_id']}", "source_url":j["url"], "title":title, "author":"模拟作者",
            "source_name":"模拟公众号","publish_time":None,"original_html":f"<p>{body}</p>",
            "fixture_text":body,"images":[IMAGE],"cover":IMAGE,"copyright_status":"fixture_only",
            "collected_at":now,"mode":"dry-run"}

def make_variant(p, doc, analysis):
    titles = {"zhihu":"一次社区读书交流可以讨论什么？","xiaohongshu":"读书会｜带一本书出发",
              "toutiao":"社区图书馆安排读书交流","baijiahao":"社区阅读交流活动介绍",
              "sohu":"社区读书会的两项活动","weibo":"一起聊聊最近读过的书","weixin":"读书会活动说明"}
    if doc["mode"]=="local-dry-run":
        titles = {platform:doc["title"] for platform in PLATFORMS}
    points = analysis["points"]
    if p == "xiaohongshu":
        body = "\n".join([f"卡片 {i}｜{pt}" for i,pt in enumerate(points,1)])
    elif p == "weibo":
        body = " ".join(points)
    else:
        body = titles[p] + "\n\n" + "\n\n".join(points)
    title = titles[p] if doc["mode"]=="local-dry-run" else titles[p]+" · "+doc["title"].split(" · ")[0]
    return {"title":title,"body":body,"format":
            {"xiaohongshu":"图片笔记","weibo":"动态"}.get(p,"文章"),"images":doc["images"],
            "cover":doc["cover"],"tags":[analysis["topic"]],"source":doc["source_url"],
            "missing_images":doc.get("missing_images",[]),"model":"local-fixture-v1"}

def execute(path, ticket, now, fault=None, after_send=None):
    jid, token = ticket
    with transaction(path) as db:
        j = db.execute("""SELECT j.*,r.url,r.canonical,b.control,b.targets,b.rules FROM jobs j
           JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
           WHERE j.id=? AND j.token=? AND j.state='running'""",(jid,token)).fetchone()
        if not j:
            return
        st, rid, p, acct = j["stage"], j["row_id"], j["platform"], j["account"]
        # Look up a previously accepted send before gate checks or retries.
        if st == "publish":
            prior = lookup_receipt(path,operation_key(j))
            if prior:
                db.execute("UPDATE jobs SET receipt=? WHERE id=?",(prior["remote_id"],jid))
                close_attempt(db,j,now,"done",received_at=prior["submitted"])
                return
        if j["control"] != "active":
            close_attempt(db,j,now,"cancelled" if j["control"]=="cancelled" else "ready")
            return
        assigned = db.execute("SELECT targets FROM row_targets WHERE row_id=?", (j["row_id"],)).fetchone()
        targets, rules = json.loads(assigned[0] if assigned else j["targets"]), json.loads(j["rules"])
        try:
            if fault:
                fault(j)
            # Deliberately named demo URLs let users exercise the retry button with no network.
            if j["canonical"].startswith("fixture:") and "/s/fixture-fail-once-" in j["url"] and st=="ingest" and j["attempts"]==1:
                raise SimulatedFailure("模拟正文获取失败，可单独重试")
            if st == "ingest":
                local = db.execute("SELECT payload FROM local_inputs WHERE row_id=?",(rid,)).fetchone()
                doc = json.loads(local[0]) if local else sample_document(j,now)
                doc.update(article_id=f"article-{rid}",collected_at=now)
                db.execute("INSERT INTO documents(row_id,payload) VALUES(?,?)",(rid,compact(doc)))
                enqueue(db,rid,"normalize",now)
            elif st == "normalize":
                doc = json.loads(db.execute("SELECT payload FROM documents WHERE row_id=?",(rid,)).fetchone()[0])
                if "fixture_text" in doc:
                    doc["markdown_content"] = doc.pop("fixture_text")
                doc["content_hash"] = digest(doc["markdown_content"])
                db.execute("UPDATE documents SET payload=? WHERE row_id=?",(compact(doc),rid))
                enqueue(db,rid,"analyze",now)
            elif st == "analyze":
                doc = json.loads(db.execute("SELECT payload FROM documents WHERE row_id=?",(rid,)).fetchone()[0])
                analysis = {"topic":"社区阅读","audience":"社区读者","type":"活动说明",
                            "points":[s+"。" for s in doc["markdown_content"].split("。") if s],
                            "source_hash":doc["content_hash"]}
                if doc["mode"]=="local-dry-run":
                    paragraphs = [s.strip() for s in doc["markdown_content"].splitlines()
                                  if s.strip() and not s.lstrip().startswith("![")]
                    analysis.update(topic=doc["title"],audience="待评估",type="本地文稿摘录",
                                    points=paragraphs[:3],rights="待人工核实")
                db.execute("UPDATE documents SET analysis=? WHERE row_id=?",(compact(analysis),rid))
                for platform in sorted({t["platform"] for t in targets}):
                    enqueue(db,rid,"adapt",now,platform)
            elif st == "adapt":
                d = db.execute("SELECT * FROM documents WHERE row_id=?",(rid,)).fetchone()
                variant = make_variant(p,json.loads(d["payload"]),json.loads(d["analysis"]))
                db.execute("INSERT INTO variants VALUES(?,?,?,?,0)",(rid,p,j["revision"],compact(variant)))
                enqueue(db,rid,"check",now,p)
            elif st == "check":
                v = db.execute("SELECT payload FROM variants WHERE row_id=? AND platform=? AND revision=?",
                               (rid,p,j["revision"])).fetchone()
                content = json.loads(v[0])
                if not content["source"]:
                    raise SimulatedFailure("缺少原文链接，需要核对来源")
                if not content["title"] or not content["body"] or content["source"]!=j["url"]:
                    raise SimulatedFailure("版本质量检查未通过")
                if content["missing_images"] or (p=="xiaohongshu" and not content["images"]):
                    raise SimulatedFailure("本地图片缺失或仍引用远程资源")
                db.execute("UPDATE variants SET checked=1 WHERE row_id=? AND platform=? AND revision=?",(rid,p,j["revision"]))
                for t in targets:
                    if t["platform"]==p:
                        enqueue(db,rid,"plan",now,p,t["account"])
            elif st == "plan":
                slot = next_slot(db,p,acct,now,rules)
                enqueue(db,rid,"publish",slot,p,acct,slot)
            elif st == "deliver":
                try:
                    remote_id, stamp = deliver(path,db,j,now)
                except ValueError as exc:
                    raise SimulatedFailure(str(exc)) from exc
                if remote_id is None:
                    close_attempt(db,j,now,"ready","到点后按实际交付频率顺延",stamp)
                    return
                if after_send:
                    after_send()
                db.execute("UPDATE jobs SET receipt=? WHERE id=?",(remote_id,jid))
                close_attempt(db,j,now,"done",received_at=stamp)
                return
            elif st == "publish":
                slot = next_slot(db,p,acct,now,rules,jid,actual_only=True)
                if slot>now:
                    db.execute("UPDATE jobs SET slot=? WHERE id=?",(slot,jid))
                    close_attempt(db,j,now,"ready","恢复后顺延至合法时隙",slot)
                    return
                accepted = simulated_receipt(path,operation_key(j),now)
                if after_send:
                    after_send()
                db.execute("UPDATE jobs SET receipt=? WHERE id=?",(accepted["remote_id"],jid))
            close_attempt(db,j,now,"done")
        except SimulatedFailure as e:
            close_attempt(db,j,now,"failed",str(e))
        except TimeoutError as e:
            # Confirmed transient failure in the simulator. Recovery checks the remote key first.
            budget = db.execute("SELECT count(*) FROM attempts WHERE job=? AND reason LIKE '暂时错误%'",(jid,)).fetchone()[0]
            reason = f"暂时错误：{e}"
            close_attempt(db,j,now,"ready" if budget<2 else "failed",reason,now+min(300,2**(budget+1)))

def tick(path, now=None, **kwargs):
    now = int(time.time()) if now is None else int(now)
    ticket = claim(path,now)
    if ticket:
        execute(path,ticket,now,**kwargs)
    return bool(ticket)

def worker(path, concurrency=1):
    from threading import Event
    import logging
    stopped = Event()
    def loop():
        last_beat = 0
        while not stopped.is_set():
            try:
                now = int(time.time())
                if now-last_beat >= 5:
                    with transaction(path) as db:
                        db.execute("INSERT OR REPLACE INTO health VALUES('worker',?)",(now,))
                    last_beat = now
                if not tick(path,now):
                    stopped.wait(0.4)
            except Exception:
                logging.exception("分发工作进程异常")
                stopped.wait(1)
    def draft_loop():
        from .editorial import tick_editorial
        while not stopped.is_set():
            try:
                if not tick_editorial(path):
                    stopped.wait(2)
            except Exception:
                logging.exception("成稿队列异常")
                stopped.wait(2)
    with ThreadPoolExecutor(max_workers=concurrency+1) as pool:
        threads = [pool.submit(loop) for _ in range(concurrency)]
        threads.append(pool.submit(draft_loop))
        try:
            for t in threads:
                t.result()
        except KeyboardInterrupt:
            stopped.set()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DIST_DB",str(DEFAULT_DB)))
    ap.add_argument("--concurrency",type=int,choices=range(1,9),default=1)
    args = ap.parse_args()
    from .runtime import instance_lock
    initialize(args.db)
    with instance_lock(str(args.db)+'.scheduler.lock'):
        worker(args.db,args.concurrency)
