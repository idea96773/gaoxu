"""Manually triggered discovery inside the configured Easy WeChat export root."""
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

from .folders import MEDIA, parse_folders
from .store import canonicalize, transaction

DEFAULT_LIBRARY = Path("E:/新公众号文章下载EasyWechatDownload/10个账号文章筛选")

def safe_entry(path):
    info = path.lstat()
    return not (path.is_symlink() or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def discover(root):
    root=Path(root)
    if not root.is_dir():
        raise ValueError("固定保存目录不可用")
    found=[]
    for current,dirs,files in os.walk(root,followlinks=False):
        folder=Path(current)
        dirs[:]=sorted(d for d in dirs if d.lower()!="img" and safe_entry(folder/d))
        bodies=sorted(folder/n for n in files if n.lower().endswith(".md") and safe_entry(folder/n))
        if not bodies:
            continue
        rel=folder.relative_to(root).as_posix()
        ref=None
        try:
            with bodies[0].open(encoding="utf-8-sig") as f:
                header=f.read(8192)
            source=re.search(r"(?m)^原文地址[：:]\s*.*?(https://mp\.weixin\.qq\.com/[^\s)<>]+)",header)
            if source:
                ref=canonicalize(source[1])
        except (OSError,UnicodeError):
            pass
        found.append((rel,folder,ref))
    return sorted(found)

def catalog(path,root):
    with transaction(path,False) as db:
        seen={r[0] for r in db.execute("SELECT canonical FROM rows WHERE kind='article' AND canonical LIKE 'folder:%'")}
        imported={r[0] for r in db.execute("SELECT rel FROM library_imports")}
    return [{"directory":rel,"title":folder.name,"known":("folder:"+ref in seen if ref else rel in imported)}
            for rel,folder,ref in discover(root)]

def selected_directories(selected):
    if not isinstance(selected,list) or not 1<=len(selected)<=100 or not all(isinstance(r,str) for r in selected):
        raise ValueError("每批最多选择 100 个文章目录")
    return list(dict.fromkeys(selected))


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
    items=[]
    remaining=256*1024*1024
    for rel in selected_directories(selected):
        try:
            item,size=read_folder(root,rel,remaining)
            remaining-=size
        except (ValueError,OSError) as exc:
            item=dict(library_rel=rel,label=rel,url=rel,canonical=None,assets={},error=str(exc))
        items.append(item)
    return items
