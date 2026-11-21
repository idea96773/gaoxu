"""Build and activate the local console without replacing the live index on a failed build."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
DIST = FRONT / "dist"
STATE = ROOT / "data" / "distribution"
NEXT = STATE / "console-next"
MANIFEST = "gaoxu-build.json"


def fingerprint(files):
    stamp = hashlib.sha256()
    for path in sorted(files):
        stamp.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        stamp.update(hashlib.sha256(path.read_bytes()).digest())
    return stamp.hexdigest()


def backend_id():
    modules = (ROOT / "backend" / "distribution").glob("*.py")
    return fingerprint([p for p in modules if not p.name.startswith("test_")]
                       + [ROOT / "scripts" / "gaoxu-requirements.txt"])


def console_id():
    files = [FRONT / name for name in ("index.html","package.json","package-lock.json","vite.config.js")]
    for folder in ("src","public"):
        files.extend(p for p in (FRONT / folder).rglob("*") if p.is_file())
    files.extend(p for p in FRONT.glob(".env*") if p.is_file() and p.name in
                 (".env",".env.local",".env.distribution",".env.distribution.local"))
    env = sorted((k,v) for k,v in os.environ.items() if k.startswith("VITE_"))
    return hashlib.sha256((fingerprint(files)+json.dumps(env)).encode()).hexdigest()


def current(folder, wanted):
    try:
        info = json.loads((folder / MANIFEST).read_bytes())
        return (info["source"] == wanted and isinstance(info["files"],dict) and "index.html" in info["files"]
                and all(is_safe_asset(name) for name in info["files"])
                and all(hashlib.sha256((folder/name).read_bytes()).hexdigest()==sha
                        for name,sha in info["files"].items()))
    except (OSError,ValueError,KeyError,TypeError):
        return False


def is_safe_asset(name):
    path=PurePosixPath(name)
    return (bool(path.parts) and name==path.as_posix() and not path.is_absolute()
            and not PureWindowsPath(name).drive and ".." not in path.parts
            and all(not part.endswith(("."," ")) and not PureWindowsPath(part).is_reserved()
                    and not any(ord(c)<32 or c in '<>:"|?*\\' for c in part) for part in path.parts)
            and name.casefold()!=MANIFEST.casefold())


def matches_installed_lock():
    try:
        actual=json.loads((FRONT/"node_modules/.package-lock.json").read_bytes())["packages"]
        required=json.loads((FRONT/"package-lock.json").read_bytes())["packages"]
        return (all(p in required and all(record.get(k)==required[p].get(k) for k in ("version","integrity","resolved"))
                    and (FRONT / p / "package.json").is_file() for p,record in actual.items())
                and all(not p or p in actual or record.get("optional") for p,record in required.items()))
    except (OSError,KeyError,TypeError,ValueError):
        return False


def copy_atomic(source, destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent,delete=False) as stream:
        tmp = Path(stream.name)
    try:
        shutil.copyfile(source,tmp)
        os.replace(tmp,destination)
    finally:
        tmp.unlink(missing_ok=True)


def prepare(wanted, force):
    if not force and current(DIST,wanted):
        return
    if not force and current(NEXT,wanted):
        if not (DIST / "index.html").exists():
            activate(wanted)
        return
    npm = shutil.which("npm.cmd" if os.name=="nt" else "npm")
    if not npm:
        raise ValueError("更新管理台需要 Node.js 和 npm")
    STATE.mkdir(parents=True,exist_ok=True)
    # Only reuse dependencies previously installed against these exact lock and package files.
    deps = fingerprint([FRONT / "package.json",FRONT / "package-lock.json"])
    installed = FRONT / "node_modules" / ".gaoxu-dependencies"
    matches=installed.is_file() and installed.read_bytes()==deps.encode("ascii")
    if not matches and not force:
        matches=matches_installed_lock()
    if force or not matches or not (FRONT / "node_modules/vite/bin/vite.js").is_file():
        subprocess.run([npm,"ci","--include=dev","--no-audit","--no-fund"],cwd=FRONT,check=True)
    installed.write_text(deps,encoding="ascii")
    with tempfile.TemporaryDirectory(prefix="console-build-",dir=STATE) as temp:
        built = Path(temp)
        subprocess.run([npm,"run","build","--","--mode","distribution","--outDir",str(built)],
                       cwd=FRONT,check=True)
        if not (built / "index.html").is_file() or not any((built / "assets").glob("Distribution-*.js")):
            raise ValueError("构建输出缺少稿序管理台入口")
        if wanted!=console_id():
            raise ValueError("构建期间源码发生变化，请重新启动稿序")
        files = {p.relative_to(built).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in built.rglob("*") if p.is_file()}
        info = dict(source=wanted,files=files)
        (built / MANIFEST).write_text(json.dumps(info),encoding="utf-8")
        for name in [*files,MANIFEST]:
            copy_atomic(built / name,NEXT / name)
    if not (DIST / "index.html").exists():
        activate(wanted)


def activate(wanted):
    if not current(NEXT,wanted):
        if current(DIST,wanted):
            return
        raise ValueError("待启用网页与当前源码不符，请重新启动稿序")
    files = json.loads((NEXT / MANIFEST).read_bytes())["files"]
    for name in files:
        if name!="index.html":
            copy_atomic(NEXT / name,DIST / name)
    copy_atomic(NEXT / "index.html",DIST / "index.html")
    copy_atomic(NEXT / MANIFEST,DIST / MANIFEST)
    # NEXT contains only our own staging output. Keep old hashed assets in DIST
    # available to browser tabs that opened before this upgrade.
    for name in [*files,MANIFEST]:
        (NEXT / name).unlink(missing_ok=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("backend-id","check","prepare","activate"))
    parser.add_argument("--force",action="store_true")
    args=parser.parse_args()
    try:
        if args.action=="backend-id":
            print(backend_id())
        else:
            wanted=console_id()
            if args.action=="check" and not current(DIST,wanted):
                raise ValueError("管理台需要更新，请双击启动稿序")
            if args.action=="prepare":
                prepare(wanted,args.force)
            if args.action=="activate":
                activate(wanted)
    except (OSError,ValueError,subprocess.CalledProcessError) as exc:
        parser.exit(1,str(exc)+"\n")
