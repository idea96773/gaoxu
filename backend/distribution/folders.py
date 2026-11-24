"""Read a user-selected EasyWechatDownload export without fetching any URL."""
import hashlib
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from .article_html import ArticleHTML
from .markdown_media import MARKDOWN, inline_tokens, local_path
from .store import canonicalize, compact, digest

SOURCE_LINE = re.compile(r"原文地址[：:][ \t]*(.*)")
METADATA_LINE = re.compile(r"(?:非?原创[ \t]*)?(?:作者|公号|公众号|发布时间)[：:]")
MEDIA = {".png",".jpg",".jpeg",".gif",".webp",".other"}
MAX_FILE_BYTES = 16*1024*1024

@dataclass(frozen=True)
class ExportHeader:
    title: str
    metadata: str
    url: str
    has_source_field: bool
    body_start: int


def source_target(value):
    """Read one bare URL, Markdown link, or autolink; use the actual target."""
    value=value.strip()
    if value.startswith("https://"):
        url=value.replace("&amp;","&")
    elif value.startswith(("[","<")):
        tokens=MARKDOWN.parseInline(value)[0].children or []
        if (not tokens or tokens[0].type!="link_open" or tokens[-1].type!="link_close"
                or any(t.type in ("link_open","link_close") for t in tokens[1:-1])):
            return ""
        url=tokens[0].attrGet("href") or ""
    else:
        return ""
    return url if re.fullmatch(r"https://[^\s<>]+",url) and canonicalize(url) else ""


def export_header(md,label):
    # Stop at the first body line, including quotes, code fences and prose.
    # Offsets refer to the original string so Markdown indentation is retained.
    lines=re.finditer(r"[^\n]*(?:\n|$)",md)
    heading=next(lines)
    title=heading[0].strip()
    if re.match(r"^#{1,6}(?:[ \t]|$)",title):
        title=re.sub(r"^#{1,6}(?:[ \t]+|$)","",title)
        title=re.sub(r"[ \t]+#+$","",title).strip()
    title=title or label
    body_start=heading.end()
    metadata=[]
    for line in lines:
        value=line[0].rstrip("\r\n")
        if line.start()==heading.end() and re.fullmatch(r"(?:=+|-+)[ \t]*",value):
            body_start=line.end()
        elif not value.strip():
            continue
        elif METADATA_LINE.match(value) or value in ("原创","非原创"):
            metadata.append(value)
        else:
            source=SOURCE_LINE.fullmatch(value)
            if source:
                return ExportHeader(title,"\n".join(metadata),source_target(source[1]),True,line.end())
            break
    return ExportHeader(title,"\n".join(metadata),"",False,body_start)


def export_metadata(header):
    author=re.search(r"作者[:：]\s*(.*?)\s+(?:公号|公众号)[:：]",header)
    source=re.search(r"(?:公号|公众号)[:：]\s*(.*?)\s+发布时间[:：]",header)
    stamp=re.search(r"发布时间[:：]\s*(\d{4}-\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})(?!\d)",header)
    published=None
    if stamp:
        try:
            published=datetime.strptime(stamp[1]+" "+stamp[2],"%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return dict(author=author[1].strip() if author else None,source_name=source[1].strip() if source else None,
                publish_time=published)

def image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a",b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12]==b"WEBP":
        return "image/webp"
    return None


class Images(ArticleHTML):
    def __init__(self):
        super().__init__()
        self.visible_images=[]

    def start_visible(self,tag,attrs,inside):
        if tag=="img":
            self.visible_images.append((inside,(attrs.get("src") or "").strip(),(attrs.get("data-src") or "").strip()))

    @property
    def refs(self):
        return self.references()

    def references(self,available=None):
        refs=[]
        for inside,src,lazy in self.visible_images:
            if self.roots==1 and not inside:
                continue
            # The downloader can leave a placeholder in either attribute. Prefer a
            # locally readable src, then data-src; retain unresolved refs for review.
            choices=[ref for ref in (src,lazy) if local_path(ref)]
            ref=next((ref for ref in choices if available is None or available(ref)),None)
            refs.append(ref or (choices[0] if choices else src or lazy))
        return refs

def markdown_fields(md,label,header=None):
    if not md.strip():
        raise ValueError("Markdown 为空")
    header=header or export_header(md,label)
    title,url=header.title,header.url
    if header.has_source_field and not url:
        raise ValueError("Markdown 头部的原文地址无效，请核对下载文件中的公众号文章链接")
    canonical=canonicalize(url) if url else None
    body=md[header.body_start:].lstrip("\r\n")
    if not body.strip():
        raise ValueError("缺少正文")
    return title,url,canonical or "local:"+digest(compact([title,body])),body


def parse_folders(parts):
    files={}
    total=0
    for name, stream in parts:
        name=name.replace("\\","/")
        if not name or name.startswith("/") or any(x in ("",".","..") or ":" in x for x in name.split("/")):
            raise ValueError("文件路径无效")
        ext=PurePosixPath(name).suffix.lower()
        if ext not in MEDIA and ext not in (".html",".md"):
            continue
        data=stream.read(MAX_FILE_BYTES+1)
        total+=len(data)
        if len(data)>MAX_FILE_BYTES or total>256*1024*1024:
            raise ValueError("单文件上限为 16 MB，每批上限为 256 MB")
        if name in files:
            raise ValueError("同一相对路径重复")
        files[name]=data
    dirs=defaultdict(dict)
    for name in files:
        p=PurePosixPath(name)
        if p.suffix.lower() in (".html",".md"):
            dirs[str(p.parent)].setdefault(p.suffix.lower(),[]).append(name)
        elif p.parent.name.lower()=="img":
            dirs[str(p.parent.parent)]
    if not 1<=len(dirs)<=1000:
        raise ValueError("请选择包含文章 Markdown 的导出目录，每批最多 1000 篇")
    items=[]
    for folder, group in sorted(dirs.items()):
        label=PurePosixPath(folder).name
        try:
            if len(group.get(".md",[]))!=1 or len(group.get(".html",[]))>1:
                raise ValueError("每篇需一个 Markdown，最多一个 HTML")
            md=files[group[".md"][0]].decode("utf-8-sig").replace("\r\n","\n").strip()
            html=files[group[".html"][0]].decode("utf-8-sig") if group.get(".html") else ""
            header=export_header(md,label)
            title,url,canonical,body=markdown_fields(md,label,header)
            def asset_bytes(ref):
                relative=local_path(ref)
                return files.get(posixpath.join(folder,relative),b"") if relative else b""

            tags=Images()
            tags.feed(html)
            tags.close()
            html_refs=tags.references(lambda ref:image_type(asset_bytes(ref)))
            md_refs=[token.attrGet("src") or "" for token in inline_tokens(MARKDOWN.parse(body)) if token.type=="image"]
            refs=list(dict.fromkeys(md_refs+html_refs))
            assets={}
            missing=[]
            for ref in refs:
                data=asset_bytes(ref)
                mime=image_type(data)
                if mime:
                    assets[ref]=(hashlib.sha256(data).hexdigest(),mime,data)
                else:
                    missing.append(ref)
            metadata=export_metadata(header.metadata)
            images=["/api/distribution/media/"+v[0] for v in assets.values()]
            doc=dict(metadata,title=title,source_url=url or None,source_folder=label,original_html=html,
                     markdown_content=body,images=list(dict.fromkeys(images)),cover=images[0] if images else None,
                     media_map={k:"/api/distribution/media/"+v[0] for k,v in assets.items()},
                     missing_images=missing,copyright_status="unknown",mode="local-dry-run")
            if len(tags.visible_images)>len(html_refs):
                doc["html_outside_images"]=len(tags.visible_images)-len(html_refs)
            items.append(dict(label=label,url=url or label,canonical=canonical,html_image_refs=html_refs if html.strip() else None,
                              markdown_sha=hashlib.sha256(files[group[".md"][0]]).hexdigest(),
                              doc=doc,assets=assets,error=""))
        except (UnicodeError,ValueError) as exc:
            items.append(dict(label=label,url=label,canonical=None,error=str(exc),assets={}))
    return items
