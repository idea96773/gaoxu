"""Persistent, local-only distribution dry-run state."""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .scheduling import BEIJING, available_slot, validate_overrides
PLATFORMS = {"zhihu": "知乎", "xiaohongshu": "小红书", "toutiao": "今日头条",
             "baijiahao": "百家号", "weibo": "微博", "sohu": "搜狐号", "weixin": "微信公众号"}
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "distribution" / "dry-run.sqlite"
DEFAULT_RULE = {"gap": 60, "account_cap": 10, "platform_cap": 20, "start_hour": 0, "end_hour": 24,
                "weekdays_only": False, "holidays": [], "workdays": []}
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, created INTEGER NOT NULL,
 control TEXT NOT NULL, targets TEXT NOT NULL, rules TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rows (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(id), line INTEGER NOT NULL,
 url TEXT NOT NULL, canonical TEXT, kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 owner INTEGER REFERENCES rows(id), UNIQUE(batch,line));
CREATE UNIQUE INDEX IF NOT EXISTS owner_url ON rows(canonical) WHERE kind='article';
CREATE INDEX IF NOT EXISTS rows_batch ON rows(batch);
CREATE TABLE IF NOT EXISTS account_rotation (pool TEXT PRIMARY KEY, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS row_targets (
 row_id INTEGER PRIMARY KEY REFERENCES rows(id), targets TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draft_settings (
 platform TEXT NOT NULL, account TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(platform,account));
CREATE TABLE IF NOT EXISTS drafts (
 id INTEGER PRIMARY KEY, row_id INTEGER NOT NULL REFERENCES rows(id), platform TEXT NOT NULL, account TEXT NOT NULL,
 rev INTEGER NOT NULL, state TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
 lease INTEGER, reason TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL, frozen TEXT, edit_no INTEGER NOT NULL DEFAULT 0,
 UNIQUE(row_id,platform,account,rev));
CREATE TABLE IF NOT EXISTS documents (
 row_id INTEGER PRIMARY KEY REFERENCES rows(id), payload TEXT NOT NULL, analysis TEXT);
CREATE TABLE IF NOT EXISTS local_inputs (row_id INTEGER PRIMARY KEY REFERENCES rows(id), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS library_imports (rel TEXT PRIMARY KEY, row_id INTEGER NOT NULL REFERENCES rows(id));
CREATE TABLE IF NOT EXISTS media (sha TEXT PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS variants (
 row_id INTEGER REFERENCES rows(id), platform TEXT NOT NULL, revision INTEGER NOT NULL,
 payload TEXT NOT NULL, checked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(row_id,platform,revision));
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, row_id INTEGER NOT NULL REFERENCES rows(id), stage TEXT NOT NULL,
 platform TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'ready',
 due INTEGER NOT NULL, slot INTEGER, revision INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
 failure TEXT NOT NULL DEFAULT '', token TEXT, lease_until INTEGER, receipt TEXT, finished INTEGER,
 UNIQUE(row_id,stage,platform,account));
CREATE TABLE IF NOT EXISTS frozen_deliveries (
 job INTEGER PRIMARY KEY REFERENCES jobs(id), draft INTEGER UNIQUE NOT NULL REFERENCES drafts(id),
 requested INTEGER, planned INTEGER NOT NULL, snapshot TEXT NOT NULL, sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reprint_defaults (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reprint_batches (
 batch TEXT PRIMARY KEY REFERENCES batches(id), destination TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reprint_sources (
 row_id INTEGER PRIMARY KEY REFERENCES rows(id), root TEXT NOT NULL, directory TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reprint_reviews (
 row_id INTEGER PRIMARY KEY REFERENCES rows(id), title TEXT NOT NULL, state TEXT NOT NULL,
 sha TEXT NOT NULL, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reprint_assignments (
 row_id INTEGER REFERENCES rows(id), platform TEXT NOT NULL, job INTEGER NOT NULL REFERENCES jobs(id),
 reused INTEGER NOT NULL, PRIMARY KEY(row_id,platform));
CREATE INDEX IF NOT EXISTS reprint_identity ON rows(canonical) WHERE kind='reprint';
CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(state,due,id);
CREATE INDEX IF NOT EXISTS calendar ON jobs(stage,platform,slot);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL,
 started INTEGER NOT NULL, ended INTEGER, outcome TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 UNIQUE(job,seq));
CREATE TABLE IF NOT EXISTS delivery_reschedules (
 id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES frozen_deliveries(job),
 before_slot INTEGER NOT NULL, after_slot INTEGER NOT NULL, changed INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS delivery_reschedule_job ON delivery_reschedules(job,id);
CREATE TABLE IF NOT EXISTS draft_bridge (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), user_id TEXT NOT NULL, name TEXT NOT NULL, seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS platform_drafts (
 id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES frozen_deliveries(job), canonical TEXT NOT NULL,
 sha TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
 updated INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
 UNIQUE(canonical,user_id));
CREATE UNIQUE INDEX IF NOT EXISTS platform_drafts_job ON platform_drafts(job);
CREATE INDEX IF NOT EXISTS platform_drafts_queue ON platform_drafts(state,user_id,id);
CREATE TABLE IF NOT EXISTS health (name TEXT PRIMARY KEY, seen INTEGER NOT NULL);
"""
def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def connect(path):
    db = sqlite3.connect(path, timeout=20, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

@contextmanager
def transaction(path, write=True):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

def initialize(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
    finally:
        db.close()
    with transaction(ledger_path(path)) as remote:
        remote.execute("""CREATE TABLE IF NOT EXISTS receipts(
          key TEXT PRIMARY KEY, remote_id TEXT UNIQUE, submitted INTEGER NOT NULL)""")

        remote.execute("""CREATE TABLE IF NOT EXISTS delivered_drafts(
            key TEXT PRIMARY KEY, remote_id TEXT NOT NULL UNIQUE, submitted INTEGER NOT NULL,
            row_id INTEGER NOT NULL, draft INTEGER NOT NULL, revision INTEGER NOT NULL,
            platform TEXT NOT NULL, account TEXT NOT NULL, planned INTEGER NOT NULL,
            sha TEXT NOT NULL, snapshot TEXT NOT NULL)""")

def ledger_path(path):
    return str(path) + ".simulator"

def canonicalize(url):
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.netloc.lower() != "mp.weixin.qq.com":
            return None
        if re.fullmatch(r"/s/[A-Za-z0-9_-]+", p.path):
            return "https://mp.weixin.qq.com" + p.path
        if p.path == "/s":
            query = parse_qs(p.query)
            keys = ("__biz", "mid", "idx")
            if all(len(query.get(k, [])) == 1 for k in keys) and all(
                re.fullmatch(r"[0-9]+", query[k][0]) for k in ("mid", "idx")):
                return "https://mp.weixin.qq.com/s?" + urlencode([(k, query[k][0]) for k in keys])
    except ValueError:
        pass
    return None

def clean_rules(raw, targets=()):
    if not isinstance(raw, dict) or set(raw) - DEFAULT_RULE.keys() - {"overrides"}:
        raise ValueError("排期规则格式无效")
    rules = DEFAULT_RULE | raw
    for k, low, high in [("gap", 1, 86400), ("account_cap", 1, 1000), ("platform_cap", 1, 10000),
                         ("start_hour", 0, 23), ("end_hour", 1, 24)]:
        if type(rules[k]) is not int or not low <= rules[k] <= high:
            raise ValueError(f"排期规则 {k} 超出范围")
    if rules["end_hour"] <= rules["start_hour"] or type(rules["weekdays_only"]) is not bool:
        raise ValueError("时间窗或工作日规则无效")
    for key in ("holidays", "workdays"):
        dates = rules[key]
        if not isinstance(dates, list) or len(dates) > 366:
            raise ValueError("日历必须是日期列表")
        for day in dates:
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("日历日期无效")
            datetime.strptime(day, "%Y-%m-%d")
    if "overrides" in rules:
        validate_overrides(rules,targets)
        if rules["overrides"]:
            rules["overrides"]=sorted(rules["overrides"],key=lambda s:(s["platform"],s.get("account","")))
        else:
            del rules["overrides"]
    return rules

def enqueue(db, row_id, stage, now, platform="", account="", slot=None):
    db.execute("""INSERT INTO jobs(row_id,stage,platform,account,due,slot)
         VALUES(?,?,?,?,?,?) ON CONFLICT(row_id,stage,platform,account) DO NOTHING""",
               (row_id, stage, platform, account, now, slot))

def import_batch(path, data, now=None, folders=None):
    now = int(time.time()) if now is None else now
    raw, key, targets = data.get("urls"), data.get("request_id"), data.get("targets")
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list) or not all(isinstance(u, str) and len(u) <= 4096 for u in raw):
        raise ValueError("请粘贴每行一条的文章链接")
    lines = [(i, u.strip()) for i,u in enumerate(raw, 1) if u.strip()]
    if not 1 <= len(lines) <= 1000 or not isinstance(key,str) or not 1 <= len(key) <= 128:
        raise ValueError("每批支持 1–1000 个非空行，需提供请求编号")
    if not isinstance(targets,list) or not 1 <= len(targets) <= 28:
        raise ValueError("请选择模拟目标")
    selected = []
    for t in targets:
        if not isinstance(t,dict) or t.get("platform") not in PLATFORMS or not isinstance(t.get("account"),str) or not re.fullmatch(r"[\w-]{1,40}", t["account"]):
            raise ValueError("平台或模拟账号名称无效")
        pair = {"platform": t["platform"], "account": t["account"]}
        if pair not in selected:
            selected.append(pair)
    rules = clean_rules(data.get("rules", {}), selected)
    manifest = [digest(compact(f.get("doc", f["error"]))) for f in folders] if folders is not None else []
    routing = data.get("account_routing", "all")
    if routing not in ("rotate", "all"):
        raise ValueError("未知账号分配方式")
    pools = {}
    for target in selected:
        pools.setdefault(target["platform"], []).append(target)
    fingerprint = digest(compact([lines, selected, rules, manifest] + (["rotate"] if routing == "rotate" else [])))
    with transaction(path) as db:
        old = db.execute("SELECT id,digest FROM batches WHERE request_key=?", (key,)).fetchone()
        if old:
            if fingerprint != old["digest"]:
                raise ValueError("请求编号与已有批次内容冲突")
            return old["id"]
        bid = uuid.uuid4().hex
        db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?)",
                   (bid,key,fingerprint,now,"active",compact(selected),compact(rules)))
        pool_keys = {p:compact(pool) for p,pool in pools.items()}
        offsets = {}
        if routing == "rotate":
            for p,pool_key in pool_keys.items():
                saved = db.execute("SELECT position FROM account_rotation WHERE pool=?", (pool_key,)).fetchone()
                offsets[p] = saved[0] if saved else 0
        article_index = 0
        for line,url in lines:
            folder = folders[line-1] if folders is not None else None
            identity = folder["canonical"] if folder is not None else canonicalize(url)
            if identity:
                identity = ("folder:" if folder is not None else "fixture:") + identity
            owner = db.execute("SELECT id FROM rows WHERE canonical=? AND kind='article'", (identity,)).fetchone()
            kind = ("duplicate" if owner else "article") if identity else "invalid"
            note = {"article":"","duplicate":"已关联原任务","invalid":"需要有效的微信公众号文章链接"}[kind]
            if folder and folder["error"]:
                note = folder["error"]
            rid = db.execute("INSERT INTO rows(batch,line,url,canonical,kind,note,owner) VALUES(?,?,?,?,?,?,?)",
                             (bid,line,url,identity,kind,note,owner["id"] if owner else None)).lastrowid
            if kind == "article":
                if routing == "rotate":
                    assigned = [pool[(offsets[p]+article_index) % len(pool)] for p,pool in pools.items()]
                    db.execute("INSERT INTO row_targets VALUES(?,?)", (rid,compact(assigned)))
                    article_index += 1
                if folder is not None:
                    db.execute("INSERT INTO local_inputs VALUES(?,?)",(rid,compact(folder["doc"])))
                    for sha,mime,blob in folder["assets"].values():
                        db.execute("INSERT OR IGNORE INTO media VALUES(?,?,?)",(sha,mime,blob))
                enqueue(db,rid,"ingest",now)
            if identity and folder and folder.get("library_rel"):
                db.execute("INSERT OR REPLACE INTO library_imports VALUES(?,?)",(folder["library_rel"],rid))
        if routing == "rotate" and article_index:
            for p,pool_key in pool_keys.items():
                db.execute("INSERT OR REPLACE INTO account_rotation VALUES(?,?)",
                           (pool_key,(offsets[p]+article_index)%len(pools[p])))
    return bid

def booked_slot(db, stage, platform, account, now, rules, exclude=-1, actual_only=False):
    predicate = "j.state='done'" if actual_only else "j.state!='cancelled'"
    mark = "j.finished" if actual_only else "coalesce(j.finished,j.slot)"
    reservations = db.execute(f"""SELECT {mark} mark,j.account,b.rules FROM jobs j
        JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch WHERE j.stage=?
        AND j.platform=? AND j.id!=? AND {predicate} AND {mark}>=?""",(stage,platform,exclude,now-86400))
    return available_slot(now,platform,account,rules,reservations,
                          append=stage=="publish" and not rules.get("overrides"))


def next_slot(db, platform, account, now, rules, exclude=-1, actual_only=False):
    return booked_slot(db,"publish",platform,account,now,rules,exclude,actual_only)
