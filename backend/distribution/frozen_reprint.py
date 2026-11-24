"""Local inspection of the exact approved HTML and media used for reprint delivery."""
import html
import json
import re
from html.parser import HTMLParser

from .delivery import frozen_binding
from .editorial import export_image
from .folders import image_type
from .store import canonicalize


class ContentError(ValueError):
    """A local content problem whose message may be shown in the task history."""


def source(db,job,platform=None):
    row=db.execute("""SELECT j.*,r.canonical,b.control FROM jobs j JOIN rows r ON r.id=j.row_id
        JOIN batches b ON b.id=r.batch WHERE j.id=? AND j.stage='deliver'""",(job,)).fetchone()
    if not row or (platform and row["platform"]!=platform):
        raise ValueError("请选择对应平台的转载交付任务")
    bound=frozen_binding(db,row)
    if bound["provider"]!="reprint":
        raise ValueError("请选择已审核的原文转载稿")
    return dict(row)|dict(draft=bound["draft"],sha=bound["sha"],snapshot=bound["snapshot"],approval=bound["approval"])


class BodyImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs=[]

    def handle_starttag(self,tag,attrs):
        if tag=="img":
            self.refs.append(dict(attrs).get("src"))


def checked_image(db,ref,position):
    if not isinstance(ref,str) or not re.fullmatch(r"/api/distribution/media/[a-f0-9]{64}",ref):
        raise ContentError(f"正文第 {position} 处配图没有绑定本地归档图片")
    try:
        asset=export_image(db,ref)
    except ValueError as exc:
        raise ContentError(f"正文第 {position} 处配图缺失或内容摘要不符，请检查归档图片") from exc
    if not asset["mime"] or image_type(asset["data"])!=asset["mime"]:
        raise ContentError(f"正文第 {position} 处配图的文件格式与归档记录不符")
    return asset


def inspect(db,row):
    view=json.loads(row["snapshot"])
    if not isinstance(view,dict) or view.get("kind")!="reprint":
        raise ContentError("冻结稿的转载内容格式无效")
    title=view.get("title")
    markup=view.get("html")
    if not isinstance(title,str) or not title.strip() or not isinstance(markup,str):
        raise ContentError("冻结稿缺少标题或正文 HTML")
    heading="<h1>"+html.escape(title)+"</h1>"
    if not markup.startswith(heading):
        raise ContentError("正文标题与冻结稿标题不一致")
    # The platform editor receives the title separately.
    markup=markup[len(heading):]
    credit,closing,body=markup.partition("</p>")
    source_url=view.get("source_url")
    canonical=canonicalize(source_url) if isinstance(source_url,str) else None
    if (not canonical or row["canonical"]!="folder:"+canonical or not closing or not body.strip()
            or not credit.endswith('<a href="'+html.escape(source_url,quote=True)+'">原文地址</a>')):
        raise ContentError("正文或原文署名链接与本篇转载记录不一致")

    link=view.get("link")
    if not isinstance(link,dict) or link.get("mode") not in ("direct","short","omit"):
        raise ContentError("冻结稿文末链接设置无效")
    mode=link["mode"]
    reader_url=""
    if mode!="omit":
        destination=link.get("destination")
        reader_url=link.get("short") if mode=="short" else destination
        if (not isinstance(destination,str) or not canonicalize(destination) or
                not isinstance(reader_url,str) or not reader_url.startswith("https://")):
            raise ContentError("文末读者链接或目标公众号地址缺失")
        target='<a href="'+html.escape(reader_url,quote=True)+'">'
        if not re.search(re.escape(target)+r'[^<]+</a></p>\s*$',body):
            raise ContentError("文末可点击链接与该平台冻结设置不一致")

    parser=BodyImages()
    parser.feed(markup)
    parser.close()
    if parser.refs!=view.get("images"):
        raise ContentError("正文配图的数量或顺序与冻结稿清单不一致")
    distinct=set()
    size=0
    for position,ref in enumerate(parser.refs,1):
        if ref not in distinct:
            asset=checked_image(db,ref,position)
            distinct.add(ref)
            size+=len(asset["data"])
    return dict(job=row["id"],draft=row["draft"],revision=row["revision"],sha=row["sha"],planned=row["slot"],approval=row["approval"],
                platform=row["platform"],account=row["account"],title=title,html=markup,images=parser.refs,
                unique_images=len(distinct),media_bytes=size,source_url=source_url,link_mode=mode,
                reader_url=reader_url,destination=link.get("destination",""),body_comparison=view.get("body_comparison"))
