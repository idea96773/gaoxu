"""Short-lived transfer receipts for the images in one authorized Zhihu draft."""
import re
import time


# Only retain bare, content-named CDN objects. A signed or parameterized URL
# returned by the bridge remains usable for its current in-memory submission.
OBJECT_URL = re.compile(
    r"https://(?:[a-z0-9-]+\.)*zhimg\.com/(?:v2-)?[0-9a-f]{16,64}"
    r"(?:_[a-zA-Z0-9-]+)?\.(?:jpe?g|png|gif|webp)"
)
REUSE_SECONDS = 24 * 3600


def confirmed(db, outbox, grant):
    now = int(time.time())
    rows = db.execute("""SELECT media_ref,url FROM zhihu_draft_images
        WHERE outbox=? AND grant_id=? AND confirmed BETWEEN ? AND ?""",
        (outbox, grant, now - REUSE_SECONDS, now))
    return {ref: url for ref, url in rows if OBJECT_URL.fullmatch(url)}


def remember(db, outbox, grant, ref, url):
    if OBJECT_URL.fullmatch(url):
        db.execute("""INSERT OR REPLACE INTO zhihu_draft_images(outbox,grant_id,media_ref,url,confirmed)
            VALUES(?,?,?,?,?)""", (outbox, grant, ref, url, int(time.time())))
