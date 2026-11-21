"""Operator-recorded use of a specific frozen reprint and its inline assets."""

def current(db,job):
    row=db.execute("""SELECT id revision,sha,scope,user_id,confirmed FROM reprint_rights
        WHERE job=? ORDER BY id DESC LIMIT 1""",(job,)).fetchone()
    return dict(row) if row else dict(revision=0,sha=None,scope="local",user_id="",confirmed=None)


def permits(record,sha,user_id):
    return bool(user_id and record["scope"]=="zhihu_draft" and record["sha"]==sha and record["user_id"]==user_id)


def record(db,job,sha,scope,user_id,now):
    db.execute("INSERT INTO reprint_rights(job,sha,scope,user_id,confirmed) VALUES(?,?,?,?,?)",
               (job,sha,scope,user_id,now))
