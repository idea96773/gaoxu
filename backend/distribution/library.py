"""Manually triggered discovery inside the configured Easy WeChat export root."""
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from .delivery import existing_deliveries
from .folders import MEDIA, SOURCE_LINK, export_metadata, parse_folders
from .search import day_start
from .store import canonicalize, transaction

DEFAULT_LIBRARY = Path("E:/新公众号文章下载EasyWechatDownload/10个账号文章筛选")

def safe_entry(path):
    info = path.lstat()
    return not (path.is_symlink() or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def article_header(folder,bodies):
    if len(bodies)!=1:
        return dict(title=folder.name,url=None,source=None,source_name=None,publish_time=None)
    with bodies[0].open("rb") as stream:
        sample=stream.read(8193)
    if len(sample)>8192:
        # A truncated URL can still look valid. Only match complete lines.
        sample=sample[:8192].rpartition(b"\n")[0]
    header=sample.decode("utf-8-sig").strip()
    title=header.partition("\n")[0].strip("# ").strip() or folder.name
    match=SOURCE_LINK.search(header)
    url=match[1] if match else None
    meta=export_metadata(header)
    return dict(title=title,url=url,source=canonicalize(url) if url else None,
                source_name=meta["source_name"],publish_time=meta["publish_time"])


def selected_header(root,relative):
    folder=selected_folder(root,relative)
    bodies=[p for p in folder.iterdir() if p.suffix.lower()==".md" and safe_entry(p) and p.is_file()]
    header=article_header(folder,bodies)
    return header["title"],header["url"],header["source"]


def discover(root):
    root=Path(root)
    if not root.is_dir() or not safe_entry(root):
        raise ValueError("固定保存目录不可用")
    found=[]
    def safe_child(path):
        try:
            return safe_entry(path)
        except OSError:
            return False
    for current,dirs,files in os.walk(root,followlinks=False):
        folder=Path(current)
        dirs[:]=sorted(d for d in dirs if d.lower()!="img" and safe_child(folder/d))
        bodies=sorted(folder/n for n in files if n.lower().endswith(".md") and safe_child(folder/n))
        if not bodies:
            continue
        rel=folder.relative_to(root).as_posix()
        try:
            header=article_header(folder,bodies)
        except (OSError,UnicodeError):
            header=article_header(folder,[])
        header.pop("url")
        found.append(dict(directory=rel,**header))
    return sorted(found,key=lambda entry:entry["directory"])

def catalog_records(path,entries):
    with transaction(path,False) as db:
        seen={r[0] for r in db.execute("SELECT canonical FROM rows WHERE kind='article' AND canonical LIKE 'folder:%'")}
        imported={r[0] for r in db.execute("SELECT rel FROM library_imports")}
        history=existing_deliveries(db,("folder:"+item["source"] for item in entries if item["source"]))
    for item in entries:
        key="folder:"+item["source"] if item["source"] else None
        item.update(known=key in seen if key else item["directory"] in imported,deliveries=history.get(key,{}))
    return entries


def catalog(path,root):
    return catalog_records(path,discover(root))


def catalog_page(path,root,args):
    start,end=args.get("start",""),args.get("end","")
    for value in (start,end):
        if value:
            day_start(value)
    if start and end and start>end:
        raise ValueError("结束日期应等于或晚于开始日期")
    try:
        page=int(args.get("page",1))
        limit=int(args.get("page_size",30))
        if page<1 or not 1<=limit<=100:
            raise ValueError
    except ValueError:
        raise ValueError("页码须为正整数，每页可读取 1 至 100 篇") from None
    order=args.get("order","directory")
    if order not in ("directory","newest"):
        raise ValueError("未知的目录排序方式")
    keyword=args.get("keyword","").strip().casefold()
    source=args.get("source_name","")
    found=[]
    for item in discover(root):
        day=(item["publish_time"] or "")[:10]
        if source and item["source_name"]!=source:
            continue
        if (start and day<start) or (end and (not day or day>end)):
            continue
        if keyword and not any(keyword in item[k].casefold() for k in ("title","directory")):
            continue
        found.append(item)
    if order=="newest":
        found.sort(key=lambda item:item["publish_time"] or "",reverse=True)
    total=len(found)
    page=min(page,max(1,(total+limit-1)//limit))
    offset=(page-1)*limit
    return dict(items=catalog_records(path,found[offset:offset+limit]),total=total,page=page,page_size=limit)

def validate_directories(selected):
    if not isinstance(selected,list) or not 1<=len(selected)<=100 or not all(isinstance(r,str) for r in selected):
        raise ValueError("每批最多选择 100 个文章目录")


def selected_folder(root, rel):
    root=Path(root)
    if not root.is_dir() or not safe_entry(root):
        raise ValueError("固定保存目录不可用")
    folder=root
    for part in ([] if rel=="." else rel.split("/")):
        if (not part or part.endswith((" ",".")) or Path(part).is_reserved()
                or any(ord(ch)<32 or ch in '<>:"|?*\\' for ch in part)):
            raise ValueError("请选择固定保存目录中的文章")
        folder=folder/part
        if not safe_entry(folder) or not folder.is_dir():
            raise ValueError("目录已移动或下载未完成")
    return folder


def read_folder(root,rel,remaining=256*1024*1024):
    folder=selected_folder(root,rel)
    paths=[p for p in folder.iterdir() if safe_entry(p) and p.is_file() and p.suffix.lower() in (".md",".html")]
    imgs=folder/"img"
    if imgs.is_dir() and safe_entry(imgs):
        paths.extend(p for p in imgs.iterdir() if safe_entry(p) and p.is_file() and p.suffix.lower() in MEDIA)
    size=sum(p.stat().st_size for p in paths)
    if size>remaining:
        raise ValueError("导入文件超过 256 MB")
    with ExitStack() as stack:
        entries=[((Path(folder.name)/p.relative_to(folder)).as_posix(),stack.enter_context(p.open("rb"))) for p in paths]
        item=parse_folders(entries)[0]
    item.update(library_rel=rel,label=folder.name)
    return item,size


def read_selected(root,selected):
    validate_directories(selected)
    items=[]
    remaining=256*1024*1024
    for rel in dict.fromkeys(selected):
        try:
            item,size=read_folder(root,rel,remaining)
            remaining-=size
        except (ValueError,OSError) as exc:
            item=dict(library_rel=rel,label=rel,url=rel,canonical=None,assets={},error=str(exc))
        items.append(item)
    return items
