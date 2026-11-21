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

def read_selected(root,selected):
    if not isinstance(selected,list) or not 1<=len(selected)<=100 or not all(isinstance(r,str) for r in selected):
        raise ValueError("每批最多选择 100 个文章目录")
    folders={rel:folder for rel,folder,_ in discover(root)}
    items=[]
    total=0
    for rel in dict.fromkeys(selected):
        if rel not in folders:
            items.append(dict(label=rel,url=rel or "未知目录",canonical=None,assets={},error="目录已移动或下载未完成"))
            continue
        folder=folders[rel]
        try:
            with ExitStack() as stack:
                paths=[p for p in folder.iterdir() if safe_entry(p) and p.is_file() and p.suffix.lower() in (".md",".html")]
                imgs=folder/"img"
                if imgs.is_dir() and safe_entry(imgs):
                    paths.extend(p for p in imgs.iterdir() if safe_entry(p) and p.is_file() and p.suffix.lower() in MEDIA)
                size=sum(p.stat().st_size for p in paths)
                if total+size>256*1024*1024:
                    raise ValueError("本批超过 256 MB，请拆分目录")
                total+=size
                entries=[(p.relative_to(root).as_posix(),stack.enter_context(p.open("rb"))) for p in paths]
                item=parse_folders(entries)[0]
        except (ValueError,OSError) as exc:
            item=dict(label=rel,url=rel,canonical=None,assets={},error=str(exc))
        item["library_rel"]=rel
        items.append(item)
    return items
