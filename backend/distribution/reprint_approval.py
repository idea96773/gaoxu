"""Identify the article review that supplied a particular frozen platform edition."""
import json

from .store import compact, digest


# A reread advances the preparation job. Reopening the same bytes advances
# the review round independently, including for older rows without that job.
REVIEW_REVISION = """(coalesce(j.revision,1)+(SELECT count(*) FROM reprint_returns h
    WHERE h.row_id=q.row_id AND h.resumed_as='snapshot'))"""


def reviewed_snapshot(row):
    if digest(row["snapshot"]) != row["sha"]:
        raise ValueError("送审快照与审核摘要不一致，请核对文章审核记录")
    snapshot = json.loads(row["snapshot"])
    if not isinstance(snapshot, dict):
        raise ValueError("找不到文章的送审内容")
    return snapshot


def origin(db, row_id, platform, edition_sha, frozen_revision):
    review = db.execute(f"""SELECT r.batch,r.line,q.state,q.snapshot,q.sha,{REVIEW_REVISION} revision,
            a.approved_at,a.review_sha,a.revision AS recorded_revision
        FROM reprint_reviews q JOIN rows r ON r.id=q.row_id
        LEFT JOIN jobs j ON j.row_id=q.row_id AND j.stage='reprint_prepare'
        LEFT JOIN reprint_approvals a ON a.row_id=q.row_id WHERE q.row_id=?""",(row_id,)).fetchone()
    if not review or review["state"] != "approved":
        raise ValueError("找不到这份冻结稿所属文章的审核通过记录")
    snapshot = reviewed_snapshot(review)
    if review["approved_at"] is not None and (
            review["recorded_revision"] != review["revision"] or review["recorded_revision"] != frozen_revision
            or review["review_sha"] != review["sha"]):
        raise ValueError("审核通过时记录的版次或摘要与文章不一致")
    if "editions" in snapshot:
        editions = snapshot["editions"]
        edition = editions.get(platform) if isinstance(editions,dict) else None
        matches = isinstance(edition,dict) and digest(compact(edition)) == edition_sha
    else:
        matches = review["sha"] == edition_sha
    if not matches:
        raise ValueError("冻结的平台稿与通过审核的对应版本不一致")
    return dict(batch=review["batch"], row_id=row_id, line=review["line"], revision=review["revision"],
                review_sha=review["sha"], edition_sha=edition_sha, approved_at=review["approved_at"])
