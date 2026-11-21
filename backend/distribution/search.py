"""Read-only article and delivery lookup across the entire simulation database."""
from datetime import date, datetime, time, timedelta
from calendar import monthrange

from flask import jsonify, request

from .store import BEIJING, PLATFORMS, transaction


# row_targets was introduced with rotation. Older all-account imports use batches.targets.
TARGETS = """
    SELECT r.id AS row_id, json_extract(t.value,'$.platform') AS platform,
           json_extract(t.value,'$.account') AS account
    FROM rows r JOIN batches b ON b.id=r.batch LEFT JOIN row_targets rt ON rt.row_id=r.id
    JOIN json_each(coalesce(rt.targets,b.targets)) t WHERE r.kind='article'
"""

RECORDS = """
WITH targets AS (""" + TARGETS + """),
articles AS MATERIALIZED (
    SELECT r.id AS row_id, r.batch, b.created, b.control,
           coalesce(q.title,json_extract(doc.payload,'$.title'),json_extract(i.payload,'$.title'),r.url) AS title
    FROM rows r JOIN batches b ON b.id=r.batch LEFT JOIN reprint_reviews q ON q.row_id=r.id
    LEFT JOIN documents doc ON doc.row_id=r.id LEFT JOIN local_inputs i ON i.row_id=r.id
    WHERE r.kind IN ('article','reprint')
),
records AS (
    SELECT j.row_id, j.platform, j.account, j.stage, j.id AS job_id, j.state, j.slot,
           CASE WHEN j.state='done' AND j.receipt IS NOT NULL THEN j.finished END AS received,
           coalesce(d.title,json_extract(v.payload,'$.title')) AS edition
    FROM jobs j LEFT JOIN frozen_deliveries f ON f.job=j.id LEFT JOIN drafts d ON d.id=f.draft
    LEFT JOIN variants v ON j.stage='publish' AND v.row_id=j.row_id AND v.platform=j.platform AND v.revision=j.revision
    WHERE j.stage IN ('publish','deliver')
    UNION ALL
    SELECT t.row_id,t.platform,t.account,'pipeline',NULL,
           CASE WHEN a.control='cancelled' THEN 'cancelled'
                WHEN EXISTS (SELECT 1 FROM jobs j WHERE j.row_id=t.row_id AND j.state='failed'
                    AND j.platform IN ('',t.platform) AND j.account IN ('',t.account))
                    THEN 'blocked'
                ELSE 'unscheduled' END,NULL,NULL,NULL
    FROM targets t JOIN articles a ON a.row_id=t.row_id
    WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.row_id=t.row_id AND j.stage='publish'
                      AND j.platform=t.platform AND j.account=t.account)
    UNION ALL
    SELECT q.row_id,NULL,NULL,'review',NULL,
           CASE WHEN a.control='cancelled' THEN 'cancelled' ELSE q.state END,NULL,NULL,NULL
    FROM reprint_reviews q JOIN articles a ON a.row_id=q.row_id WHERE q.state IN ('pending','invalid')
)
SELECT a.row_id,a.batch,a.created,a.control,a.title,x.edition,x.stage,x.platform,x.account,
       x.job_id,x.state,x.slot,x.received FROM records x JOIN articles a ON a.row_id=x.row_id
"""
DATE_FIELDS = {"scheduled":"slot", "delivered":"received", "imported":"created"}
PAGE_SIZE = 25


def day_start(value, following=False):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError
        if following:
            day += timedelta(days=1)
        return int(datetime.combine(day,time.min,BEIJING).timestamp())
    except (ValueError, OSError, OverflowError):
        raise ValueError("日期应为有效的年-月-日") from None


def search(db, args):
    field = DATE_FIELDS.get(args.get("date_field", "scheduled"))
    if not field:
        raise ValueError("未知的日期类型")
    filters, values = [], []
    keyword = args.get("title","").strip()
    if keyword:
        filters.append("(instr(lower(title),lower(?))>0 OR instr(lower(edition),lower(?))>0)")
        values.extend((keyword,keyword))
    platform = args.get("platform","")
    if platform and platform not in PLATFORMS:
        raise ValueError("未知的平台")
    for column,value in (("platform",platform),("account",args.get("account",""))):
        if value:
            filters.append(column+"=?")
            values.append(value)
    since, until = args.get("start",""), args.get("end","")
    if since:
        filters.append(field+">=?")
        values.append(day_start(since))
    if until:
        filters.append(field+"<?")
        values.append(day_start(until,following=True))
    if since and until and since>until:
        raise ValueError("结束日期应等于或晚于开始日期")
    try:
        page = int(args.get("page",1))
        if page<1:
            raise ValueError
    except ValueError:
        raise ValueError("页码应为正整数") from None
    sql = " FROM ("+RECORDS+")" + (" WHERE "+" AND ".join(filters) if filters else "")
    total = db.execute("SELECT count(*)"+sql,values).fetchone()[0]
    page = min(page,max(1,(total+PAGE_SIZE-1)//PAGE_SIZE))
    rows = db.execute("SELECT *"+sql+" ORDER BY "+field+" DESC, created DESC,row_id DESC,"
                      "job_id DESC,platform,account LIMIT ? OFFSET ?",values+[PAGE_SIZE,(page-1)*PAGE_SIZE])
    return dict(items=[dict(r) for r in rows],total=total,page=page,page_size=PAGE_SIZE)


def register(api,path):
    @api.get("/schedule-calendar")
    def schedule_calendar():
        today=datetime.now(BEIJING).date()
        month=request.args.get("month") or today.strftime("%Y-%m")
        start=day_start(month+"-01")
        first=date.fromisoformat(month+"-01")
        size=monthrange(first.year,first.month)[1]
        filters=["stage IN ('deliver','publish')","slot>=?","slot<?"]
        values=[start,start+size*86400]
        platform=request.args.get("platform","")
        if platform and platform not in PLATFORMS:
            raise ValueError("未知的平台")
        for field,value in (("platform",platform),("account",request.args.get("account",""))):
            if value:
                filters.append(field+"=?")
                values.append(value)
        with transaction(path,False) as db:
            counts=db.execute("""SELECT date(slot,'unixepoch','+8 hours') day,
                CASE WHEN b.control='paused' AND j.state='ready' THEN 'paused' ELSE j.state END status,
                count(*) n FROM jobs j JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
                WHERE """+" AND ".join(filters)+" GROUP BY day,status",values)
            days={}
            for row in counts:
                days.setdefault(row["day"],{})[row["status"]]=row["n"]
        return jsonify(month=month,today=today.isoformat(),size=size,weekday=first.weekday(),days=days)

    @api.get("/article-search")
    def search_articles():
        with transaction(path,False) as db:
            return jsonify(search(db,request.args))

    @api.get("/article-search/accounts")
    def search_accounts():
        with transaction(path,False) as db:
            choices=db.execute("WITH targets AS ("+TARGETS+") SELECT platform,account FROM targets "
                               "UNION SELECT platform,account FROM jobs WHERE stage IN ('deliver','publish') "
                               "ORDER BY platform,account")
            return jsonify(items=[dict(r) for r in choices],platforms=PLATFORMS)

