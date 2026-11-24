"""Read-only article and delivery lookup across the entire simulation database."""
from datetime import date, datetime, time, timedelta
from calendar import monthrange

from flask import jsonify, request

from .platform_draft import local_console_request
from .store import BEIJING, EXECUTION_SLOT, PLATFORMS, transaction


SCHEDULE_STATE = """CASE WHEN {stage}='deliver' AND {state}='done' AND ({received}) IS NULL THEN 'unconfirmed'
    WHEN {control}='paused' AND {state}='ready' THEN 'paused' ELSE {state} END"""
RECEIVED = "CASE WHEN j.state='done' AND nullif(j.receipt,'') IS NOT NULL THEN j.finished END"
JOB_SCHEDULE_STATE = SCHEDULE_STATE.format(stage="j.stage",state="j.state",received=RECEIVED,control="b.control")


def selected_schedule_state(args):
    state=args.get("schedule_state","")
    if state not in ("","ready","running","done","failed","paused","cancelled","unconfirmed"):
        raise ValueError("未知的模拟排期状态")
    return state


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
           """+EXECUTION_SLOT+""" AS execution_slot,
           """+RECEIVED+""" AS received,
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
                ELSE 'unscheduled' END,NULL,NULL,NULL,NULL
    FROM targets t JOIN articles a ON a.row_id=t.row_id
    WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.row_id=t.row_id AND j.stage='publish'
                      AND j.platform=t.platform AND j.account=t.account)
    UNION ALL
    SELECT q.row_id,NULL,NULL,'review',NULL,
           CASE WHEN a.control='cancelled' THEN 'cancelled' ELSE q.state END,NULL,NULL,NULL,NULL
    FROM reprint_reviews q JOIN articles a ON a.row_id=q.row_id WHERE q.state IN ('preparing','pending','invalid','returned','scheduling')
)
SELECT a.row_id,a.batch,a.created,a.control,a.title,x.edition,x.stage,x.platform,x.account,
       x.job_id,x.state,x.slot,x.execution_slot,x.received,p.state AS draft_state,p.updated AS draft_updated,
       coalesce(pin.user_id,p.user_id) AS recipient_id,coalesce(pin.name,p.name) AS recipient_name,
       CASE WHEN x.job_id IS NOT NULL THEN """+SCHEDULE_STATE.format(control="a.control",state="x.state",stage="x.stage",received="x.received")+""" END AS schedule_state
       FROM records x JOIN articles a ON a.row_id=x.row_id LEFT JOIN platform_drafts p ON p.job=x.job_id
       LEFT JOIN zhihu_draft_recipients pin ON pin.job=x.job_id
"""
DATE_FIELDS = {"scheduled":"slot", "execution":"execution_slot", "delivered":"received", "imported":"created",
               "draft_updated":"draft_updated"}
PAGE_SIZE = 25


def overview(db,now):
    today=datetime.fromtimestamp(now,BEIJING).date().isoformat()
    counts=db.execute("""SELECT
        (SELECT count(*) FROM reprint_reviews q JOIN rows r ON r.id=q.row_id
         JOIN batches b ON b.id=r.batch WHERE q.state='pending' AND b.control='active') pending_reprint,
        (SELECT count(*) FROM reprint_reviews q JOIN rows r ON r.id=q.row_id
         JOIN batches b ON b.id=r.batch WHERE q.state='returned' AND b.control!='cancelled') returned_reprint,
        (SELECT count(*) FROM jobs j JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
         WHERE j.stage='deliver' AND j.state='ready' AND b.control='active') queued_delivery,
        (SELECT count(*) FROM jobs WHERE stage='deliver' AND state='done' AND nullif(receipt,'') IS NOT NULL
         AND finished>=? AND finished<?) delivered_today,
        (SELECT count(*) FROM jobs WHERE state='failed') failed_steps""",
        (day_start(today),day_start(today,following=True))).fetchone()
    by_platform={}
    results=db.execute(f"""SELECT j.platform,{JOB_SCHEDULE_STATE} AS outcome,count(*) AS n
        FROM jobs j JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
        WHERE j.stage='deliver' GROUP BY j.platform,outcome ORDER BY j.platform""")
    for entry in results:
        by_platform.setdefault(entry["platform"],{})[entry["outcome"]]=entry["n"]
    for states in by_platform.values():
        states["total"]=sum(states.values())
        done=states.get("done",0)
        terminal=done+states.get("failed",0)
        states["success_percent"]=round(done*100/terminal,1) if terminal else None
    return dict(counts)|{"today":today,"delivery_platforms":by_platform}


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


def search(db, args, *, include_recipients=False):
    field = DATE_FIELDS.get(args.get("date_field", "scheduled"))
    if not field:
        raise ValueError("未知的日期类型")
    filters, values = [], []
    schedule_state=selected_schedule_state(args)
    if schedule_state:
        filters.append("schedule_state=?")
        values.append(schedule_state)
    if field=="draft_updated":
        filters.append("draft_updated IS NOT NULL")
    recipient=args.get("draft_recipient","").strip()
    if recipient:
        if not include_recipients:
            raise ValueError("请从本机稿序管理台查询知乎接收账号")
        filters.append("(recipient_id=? OR instr(lower(recipient_name),lower(?))>0)")
        values.extend((recipient,recipient))
    work=args.get("work","")
    if work:
        scopes={"pending_reprint":"stage='review' AND state='pending' AND control='active'",
                "returned_reprint":"stage='review' AND state='returned'",
                "frozen_delivery":"stage='deliver'",
                "queued_delivery":"stage='deliver' AND state='ready' AND control='active'",
                "confirmed_delivery":"stage='deliver' AND state='done' AND received IS NOT NULL"}
        if work not in scopes:
            raise ValueError("未知的任务筛选条件")
        filters.append(scopes[work])
    review=args.get("draft_review","")
    if review:
        conditions={"awaiting":"draft_state IN ('saved','unknown')","issue":"draft_state='needs_review'",
                    "verified":"draft_state='verified'","waiting":"draft_state='queued'",
                    "saving":"draft_state IN ('preparing','sending')","failed":"draft_state='failed'",
                    "uncertain":"draft_state='unknown'","cancelled":"draft_state='cancelled'"}
        conditions["unsubmitted_reprint"]="""platform='zhihu' AND stage='deliver' AND state!='cancelled'
            AND control='active' AND draft_state IS NULL AND EXISTS (
                SELECT 1 FROM frozen_deliveries f JOIN drafts d ON d.id=f.draft
                WHERE f.job=job_id AND d.provider='reprint' AND d.state='approved')"""
        if review not in conditions:
            raise ValueError("未知的平台草稿核对状态")
        filters.append(conditions[review])
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
    columns="*" if include_recipients else ("row_id,batch,created,control,title,edition,stage,platform,account,"
        "job_id,state,slot,execution_slot,received,draft_state,draft_updated,schedule_state")
    rows = db.execute("SELECT "+columns+sql+" ORDER BY "+field+" DESC, created DESC,row_id DESC,"
                      "job_id DESC,platform,account LIMIT ? OFFSET ?",values+[PAGE_SIZE,(page-1)*PAGE_SIZE])
    return dict(items=[dict(r) for r in rows],total=total,page=page,page_size=PAGE_SIZE)


def failed_jobs(db,args):
    filters=["j.state='failed'"]
    values=[]
    for key,column in (("batch","r.batch"),("stage","j.stage")):
        if args.get(key):
            filters.append(column+"=?")
            values.append(args[key])
    if args.get("day"):
        filters.extend(("a.ended>=?","a.ended<?"))
        values.extend((day_start(args["day"]),day_start(args["day"],following=True)))
    try:
        page=int(args.get("page",1))
        if page<1:
            raise ValueError
    except ValueError:
        raise ValueError("页码应为正整数") from None
    source=""" FROM jobs j JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
        LEFT JOIN attempts a ON a.job=j.id AND a.seq=j.attempts """
    where=" WHERE "+" AND ".join(filters)
    total=db.execute("SELECT count(*)"+source+where,values).fetchone()[0]
    page=min(page,max(1,(total+PAGE_SIZE-1)//PAGE_SIZE))
    rows=db.execute("""SELECT j.id,j.row_id,j.stage,j.account,j.platform,j.attempts,j.failure,
        a.ended AS failed_at,r.batch,b.control,s.directory,
        coalesce(d.title,q.title,json_extract(doc.payload,'$.title'),json_extract(i.payload,'$.title'),r.url) AS title
        """+source+"""
        LEFT JOIN reprint_sources s ON s.row_id=r.id LEFT JOIN reprint_reviews q ON q.row_id=r.id
        LEFT JOIN frozen_deliveries f ON f.job=j.id LEFT JOIN drafts d ON d.id=f.draft
        LEFT JOIN documents doc ON doc.row_id=r.id LEFT JOIN local_inputs i ON i.row_id=r.id
        """+where+" ORDER BY a.ended DESC,j.id DESC LIMIT ? OFFSET ?",values+[PAGE_SIZE,(page-1)*PAGE_SIZE])
    return dict(items=[dict(r) for r in rows],total=total,page=page,page_size=PAGE_SIZE,platforms=PLATFORMS)


def register(api,path):
    @api.get("/failed-jobs")
    def list_failed_jobs():
        with transaction(path,False) as db:
            return jsonify(failed_jobs(db,request.args))

    @api.get("/schedule-calendar")
    def schedule_calendar():
        today=datetime.now(BEIJING).date()
        month=request.args.get("month") or today.strftime("%Y-%m")
        start=day_start(month+"-01")
        first=date.fromisoformat(month+"-01")
        size=monthrange(first.year,first.month)[1]
        calendar_field=request.args.get("date_field","scheduled")
        if calendar_field not in ("scheduled","execution"):
            raise ValueError("日历仅支持计划排期日或预计执行日")
        mark=EXECUTION_SLOT if calendar_field=="execution" else "j.slot"
        filters=["stage IN ('deliver','publish')",mark+">=?",mark+"<?"]
        values=[start,start+size*86400]
        work=request.args.get("work","")
        if work not in ("","frozen_delivery"):
            raise ValueError("未知的日历任务范围")
        if work:
            filters.append("j.stage='deliver'")
        state=selected_schedule_state(request.args)
        status=JOB_SCHEDULE_STATE
        if state:
            filters.append(status+"=?")
            values.append(state)
        platform=request.args.get("platform","")
        if platform and platform not in PLATFORMS:
            raise ValueError("未知的平台")
        for field,value in (("platform",platform),("account",request.args.get("account",""))):
            if value:
                filters.append(field+"=?")
                values.append(value)
        with transaction(path,False) as db:
            counts=db.execute(f"""SELECT date({mark},'unixepoch','+8 hours') day,
                {status} status,
                count(*) n FROM jobs j JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch
                WHERE """+" AND ".join(filters)+" GROUP BY day,status",values)
            days={}
            for row in counts:
                days.setdefault(row["day"],{})[row["status"]]=row["n"]
        return jsonify(month=month,date_field=calendar_field,today=today.isoformat(),size=size,weekday=first.weekday(),days=days)

    @api.get("/article-search")
    def search_articles():
        with transaction(path,False) as db:
            return jsonify(search(db,request.args,include_recipients=local_console_request()))

    @api.get("/article-search/accounts")
    def search_accounts():
        with transaction(path,False) as db:
            choices=db.execute("WITH targets AS ("+TARGETS+") SELECT platform,account FROM targets "
                               "UNION SELECT platform,account FROM jobs WHERE stage IN ('deliver','publish') "
                               "ORDER BY platform,account")
            return jsonify(items=[dict(r) for r in choices],platforms=PLATFORMS)

