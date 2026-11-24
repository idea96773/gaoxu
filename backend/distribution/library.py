"""Manually triggered discovery inside the configured Easy WeChat export root."""
import os
import stat
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from .delivery import existing_deliveries
from .folders import MAX_FILE_BYTES, MEDIA, export_header, export_metadata, markdown_fields, parse_folders
from .search import day_start
from .store import canonicalize, compact, digest, transaction

DEFAULT_LIBRARY = Path("E:/新公众号文章下载EasyWechatDownload/10个账号文章筛选")
EXPORT_CHANGED = "读取期间下载目录中的正文或配图发生变化，请等待这篇下载完成"


def file_signature(info):
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns

def library_settings(path, fixed=None):
    if fixed:
        return dict(root=str(Path(fixed).absolute()),revision=0,editable=False)
    # The Windows launcher's read-only startup check also runs before the first
    # database has been created, and before migrations on an older installation.
    saved=None
    if Path(path).is_file():
        db=sqlite3.connect(Path(path).absolute().as_uri()+"?mode=ro",uri=True)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='library_settings' AND type='table'").fetchone():
                saved=db.execute("SELECT root,revision FROM library_settings WHERE singleton=1").fetchone()
        finally:
            db.close()
    return dict(root=saved[0] if saved else str(DEFAULT_LIBRARY),revision=saved[1] if saved else 0,editable=True)


def configure_library(path,fixed,data):
    if fixed:
        raise ValueError("文章目录由启动配置指定，请在启动配置中更改")
    value=data.get("root")
    if not isinstance(value,str) or not value.strip() or "\x00" in value:
        raise ValueError("请粘贴 Easy WeChat 保存目录的完整路径")
    folder=Path(value.strip())
    if not folder.is_absolute() or ".." in folder.parts or (os.name=="nt" and not (
            len(folder.drive)==2 and folder.drive[0].isalpha() and folder.drive[1]==":")):
        raise ValueError("请选择本机磁盘上的绝对目录")
    try:
        if not folder.is_dir() or not all(safe_entry(p) for p in (folder,*folder.parents)):
            raise ValueError("请选择可读取的本机文件夹，目录路径中不能含有符号链接或联接")
        with os.scandir(folder) as entries:
            next(entries,None)
    except OSError as exc:
        raise ValueError("后台无法读取该文件夹，请核对路径和权限") from exc
    with transaction(path) as db:
        row=db.execute("SELECT root,revision FROM library_settings WHERE singleton=1").fetchone()
        revision=row["revision"] if row else 0
        current=Path(row["root"]) if row else DEFAULT_LIBRARY
        if folder!=current:
            if type(data.get("revision")) is not int or data["revision"]!=revision:
                raise ValueError("另一窗口已更换目录，请重新读取设置后保存")
            revision+=1
            db.execute("INSERT OR REPLACE INTO library_settings VALUES(1,?,?)",(str(folder),revision))
    return dict(root=str(folder),revision=revision,editable=True)


def check_library_root(root,data):
    if "expected_root" in data and data["expected_root"]!=str(root):
        raise ValueError("文章保存目录已更换，请重新读取目录并选稿")


def selection_key(root,directory,kind,value):
    location=os.path.normcase(os.path.abspath(root))
    return kind+"-"+digest(compact([location,directory,value]))

def safe_entry(path):
    info = path.lstat()
    return not (path.is_symlink() or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def article_header(folder,bodies):
    if len(bodies)!=1:
        return dict(title=folder.name,url=None,source=None,source_name=None,publish_time=None,markdown_sha=None)
    with bodies[0].open("rb") as stream:
        before=os.fstat(stream.fileno())
        sample=stream.read(8193)
        # A truncated URL can still look valid. Only match complete lines.
        prefix=sample[:8192].rpartition(b"\n")[0] if len(sample)>8192 else sample
        header=export_header(prefix.decode("utf-8-sig").strip(),folder.name)
        title=header.title
        url=header.url or None
        source=canonicalize(url) if url else None
        markdown_sha=None
        identity=source
        if not source:
            size=before.st_size
            if len(sample)<=size<=MAX_FILE_BYTES:
                raw=sample+stream.read(size-len(sample)+1)
                if len(raw)==size:
                    markdown_sha=hashlib.sha256(raw).hexdigest()
                    try:
                        md=raw.decode("utf-8-sig").replace("\r\n","\n").strip()
                        _,_,identity,_=markdown_fields(md,folder.name)
                    except ValueError:
                        pass
        if file_signature(bodies[0].lstat())!=file_signature(before):
            raise BlockingIOError(EXPORT_CHANGED)
    meta=export_metadata(header.metadata)
    return dict(title=title,url=url,source=source,markdown_sha=markdown_sha,identity=identity,
                source_name=meta["source_name"],publish_time=meta["publish_time"])


def selected_header(root,relative):
    folder=selected_folder(root,relative)
    try:
        bodies=[p for p in folder.iterdir() if p.suffix.lower()==".md" and safe_entry(p) and p.is_file()]
        return article_header(folder,bodies)
    except FileNotFoundError as exc:
        raise BlockingIOError(EXPORT_CHANGED) from exc


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
        markdown_sha=header.pop("markdown_sha")
        kind,value=("url",header["source"]) if header["source"] else ("file",markdown_sha)
        header["selection_key"]=selection_key(root,rel,kind,value) if value else None
        found.append(dict(directory=rel,**header))
    return sorted(found,key=lambda entry:entry["directory"])

def catalog_records(path,entries):
    with transaction(path,False) as db:
        seen={r[0] for r in db.execute("SELECT canonical FROM rows WHERE kind='article' AND canonical LIKE 'folder:%'")}
        history=existing_deliveries(db,("folder:"+item["source"] for item in entries if item["source"]))
    for item in entries:
        key="folder:"+item["source"] if item["source"] else None
        identity=item.pop("identity",None)
        item.update(known=bool(identity and "folder:"+identity in seen),deliveries=history.get(key,{}))
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


def export_files(folder):
    def entries(directory,extensions):
        for path in directory.iterdir():
            if path.suffix.lower() not in extensions:
                continue
            info=path.lstat()
            if stat.S_ISREG(info.st_mode) and not getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                yield path,file_signature(info)

    files=dict(entries(folder,(".md",".html")))
    imgs=folder/"img"
    if imgs.is_dir() and safe_entry(imgs):
        files.update(entries(imgs,MEDIA))
    return files


def export_streams(folder,files):
    for path,expected in files.items():
        with path.open("rb") as stream:
            if file_signature(os.fstat(stream.fileno()))!=expected:
                raise BlockingIOError(EXPORT_CHANGED)
            yield path.relative_to(folder.parent).as_posix(),stream
            if file_signature(os.fstat(stream.fileno()))!=expected:
                raise BlockingIOError(EXPORT_CHANGED)


def read_folder(root,rel,remaining=256*1024*1024):
    folder=selected_folder(root,rel)
    try:
        files=export_files(folder)
        size=sum(length for _,_,length,_ in files.values())
        if size>remaining:
            raise ValueError("导入文件超过 256 MB")
        try:
            with closing(export_streams(folder,files)) as streams:
                item=parse_folders(streams)[0]
        finally:
            if export_files(folder)!=files:
                raise BlockingIOError(EXPORT_CHANGED)
    except FileNotFoundError as exc:
        raise BlockingIOError(EXPORT_CHANGED) from exc
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
