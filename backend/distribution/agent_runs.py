"""Record metadata for MCP requests that reach the local management API."""
import logging
import re
import sqlite3
import time

from flask import g, jsonify, request

from .store import transaction


TOOLS = frozenset("""
    library_list reprint_settings reprint_prepare reprint_prepare_saved reprint_preview article_search scheduler_status
    library_import article_batch_import dashboard_summary batch_status batch_control article_detail
    publish_status publish_cancel failed_jobs failed_jobs_retry stage_retry publish_reschedule draft_list draft_submit
    draft_schedule schedule_calendar account_list publish_available_slot
""".split())
IMPORTS = {"reprint_prepare", "reprint_prepare_saved", "article_batch_import", "library_import"}


def subject_from_route(args):
    parts = []
    if re.fullmatch(r"[0-9a-f]{32}", args.get("bid", "")):
        parts.append("批次 " + args["bid"])
    for key, label in (("rid", "文章"), ("jid", "任务"), ("did", "成稿")):
        if type(args.get(key)) is int:
            parts.append(f"{label} {args[key]}")
    if args.get("action") in ("pause", "resume", "cancel"):
        parts.append({"pause":"暂停", "resume":"继续", "cancel":"取消"}[args["action"]])
    return " · ".join(parts)


def register(api, path):
    @api.before_request
    def record_arrival():
        tool = request.headers.get("X-Gaoxu-MCP-Tool")
        if tool not in TOOLS:
            return
        with transaction(path) as db:
            g.agent_run = db.execute(
                "INSERT INTO agent_runs(tool,started,subject) VALUES(?,?,?)",
                (tool, int(time.time()), subject_from_route(request.view_args or {}))
            ).lastrowid
            db.execute("DELETE FROM agent_runs WHERE id <= ?", (g.agent_run - 5000,))

    @api.after_request
    def record_response(response):
        run_id = getattr(g, "agent_run", None)
        if run_id is None:
            return response
        tool = request.headers["X-Gaoxu-MCP-Tool"]
        subject = None
        applied = requested = None
        if request.method == "POST" and response.is_json and response.status_code < 300:
            route = request.url_rule.rule.removeprefix("/api/distribution/")
            if tool in IMPORTS and route in ("reprints", "batches", "library"):
                batch = response.get_json().get("id")
                if isinstance(batch, str) and re.fullmatch(r"[0-9a-f]{32}", batch):
                    subject = "批次 " + batch
            elif (tool, route) in (("draft_schedule", "editorial/<int:did>/schedule"),
                                  ("draft_submit", "articles/<int:rid>/editorial")):
                result_id = response.get_json().get("id")
                if type(result_id) is int:
                    label = "任务" if tool == "draft_schedule" else "成稿"
                    subject = f"{subject_from_route(request.view_args)} · {label} {result_id}"
            elif tool == "failed_jobs_retry" and route == "failed-jobs/retry":
                items = response.get_json().get("items", [])
                requested = len(items)
                applied = sum(item.get("queued") is True for item in items)
        try:
            with transaction(path) as db:
                db.execute("""UPDATE agent_runs SET finished=?,status_code=?,subject=coalesce(?,subject),
                           applied=?,requested=? WHERE id=?""",
                           (int(time.time()), response.status_code, subject, applied, requested, run_id))
        except sqlite3.Error:
            # Preserve an already committed operation's HTTP result if logging is interrupted.
            logging.getLogger(__name__).exception("无法写入 Agent 调用结束记录 %d", run_id)
        return response

    @api.get("/agent-runs")
    def agent_runs():
        before = request.args.get("before")
        if before is not None and not re.fullmatch(r"[1-9][0-9]{0,17}", before):
            raise ValueError("翻页位置无效")
        condition = " WHERE id < ?" if before else ""
        with transaction(path, False) as db:
            rows = db.execute("SELECT * FROM agent_runs" + condition + " ORDER BY id DESC LIMIT 26",
                              (int(before),) if before else ()).fetchall()
        return jsonify(items=[dict(row) for row in rows[:25]], next=rows[24]["id"] if len(rows)>25 else None)
