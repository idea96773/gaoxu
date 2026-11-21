"""Read a user-selected EasyWechatDownload export without fetching any URL."""
import hashlib
import posixpath
import re
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from .store import canonicalize, compact, digest

SOURCE_LINK = re.compile(r"(?m)^原文地址[：:]\s*.*?(https://mp\.weixin\.qq\.com/[^\s)<>]+)")
MEDIA = {".png",".jpg",".jpeg",".gif",".webp",".other"}
MAX_FILE_BYTES = 16*1024*1024

def export_metadata(markdown):
    header=markdown[:1600].partition("\n")[2]
    header=re.split(r"(?m)^原文地址[:：]",header,maxsplit=1)[0]
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
class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs=[]
    def handle_starttag(self, tag, attrs):
        if tag=="img":
            a=dict(attrs)
            ref=a.get("src") or a.get("data-src")
            if ref:
                self.refs.append(ref)

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
            lines=md.splitlines()
            if not lines:
                raise ValueError("Markdown 为空")
            title=lines[0].strip("# ").strip() or label
            source=SOURCE_LINK.search(md)
            url=source[1] if source else ""
            canonical=canonicalize(url) if url else None
            if url and not canonical:
                raise ValueError("原文链接无效")
            body=md[source.end():].removeprefix(")").lstrip("\n") if source else "\n".join(lines[1:]).lstrip("= \n")
            if not body.strip():
                raise ValueError("缺少正文")
            tags=Images(); tags.feed(html)
            md_refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)",body)
            # Easy WeChat may append a quoted Markdown title after the image path.
            md_refs = [re.split(r"""\s+["']""", ref, maxsplit=1)[0].strip("<> ") for ref in md_refs]
            refs=list(dict.fromkeys(md_refs+tags.refs))
            assets={}
            missing=[]
            for ref in refs:
                u=urlsplit(ref)
                relative=unquote(u.path).replace("\\","/")
                if u.scheme or u.netloc or relative.startswith("/") or ".." in relative.split("/"):
                    missing.append(ref)
                    continue
                target=posixpath.normpath(posixpath.join(folder,relative))
                data=files.get(target,b"")
                mime=image_type(data)
                if mime:
                    assets[ref]=(hashlib.sha256(data).hexdigest(),mime,data)
                else:
                    missing.append(ref)
            metadata=export_metadata(md)
            images=["/api/distribution/media/"+v[0] for v in assets.values()]
            doc=dict(metadata,title=title,source_url=url or None,source_folder=label,original_html=html,
                     markdown_content=body,images=list(dict.fromkeys(images)),cover=images[0] if images else None,
                     media_map={k:"/api/distribution/media/"+v[0] for k,v in assets.items()},
                     missing_images=missing,copyright_status="unknown",mode="local-dry-run")
            items.append(dict(label=label,url=url or label,canonical=canonical or "local:"+digest(compact([title,body])),
                              markdown_sha=hashlib.sha256(files[group[".md"][0]]).hexdigest(),
                              doc=doc,assets=assets,error=""))
        except (UnicodeError,ValueError) as exc:
            items.append(dict(label=label,url=label,canonical=None,error=str(exc),assets={}))
    return items
