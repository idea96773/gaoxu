"""Optional interactive WechatSync draft bridge. Start separately from the simulator."""
import argparse
import base64
import getpass
import hashlib
import html
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .platform_draft import claim, edit_url, payload, transition
from .store import DEFAULT_DB, initialize, transaction

CHUNK = 512 * 1024


@contextmanager
def exclusive_worker(path):
    # An OS lock disappears on process death. A stale PID cannot unlock another worker.
    with open(str(path)+".draft-worker.lock","a+b") as lock:
        lock.seek(0)
        if os.name=="nt":
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


class Bridge:
    def __init__(self,socket,token):
        self.socket=socket
        self.token=token

    def request(self,method,params,timeout=45):
        key=uuid.uuid4().hex
        self.socket.send(json.dumps(dict(id=key,token=self.token,method=method,params=params),ensure_ascii=False))
        message=json.loads(self.socket.recv(timeout=timeout))
        if message.get("id")!=key or message.get("error") or "result" not in message:
            raise ValueError("扩展未确认操作")
        return message["result"]

    def identity(self):
        data=self.request("checkAuth",{"platform":"zhihu"})
        if not data.get("isAuthenticated"):
            return "",""
        uid=data.get("userId")
        if not isinstance(uid,str) or not uid or not data.get("username"):
            raise ValueError("扩展未返回可核对的知乎账号")
        return uid,str(data["username"])

    def upload(self,blob,mime):
        encoded=base64.b64encode(blob).decode("ascii")
        if len(encoded)<=CHUNK:
            result=self.request("uploadImage",dict(platform="zhihu",mimeType=mime,imageData=encoded),180)
        else:
            upload=uuid.uuid4().hex
            self.request("uploadImage:start",dict(uploadId=upload,totalChunks=(len(encoded)+CHUNK-1)//CHUNK,
                                                 mimeType=mime,platform="zhihu"))
            for index,offset in enumerate(range(0,len(encoded),CHUNK)):
                self.request("uploadImage:chunk",dict(uploadId=upload,chunkIndex=index,data=encoded[offset:offset+CHUNK]))
            result=self.request("uploadImage:complete",dict(uploadId=upload),180)
        url=result.get("url","")
        parsed=urlsplit(url)
        host=parsed.hostname or ""
        if (result.get("platform")!="zhihu" or parsed.scheme!="https" or parsed.username or parsed.password
                or not (host=="zhimg.com" or host.endswith(".zhimg.com"))):
            raise ValueError("知乎图片转存没有返回有效地址")
        return url


def prepare_content(path,task,bridge):
    view=payload(path,task)
    markup=view["html"].removeprefix("<h1>"+html.escape(view["title"])+"</h1>")
    refs=re.findall(r'<img\b[^>]*?\ssrc="([^"]+)"',markup)
    if refs!=view["images"]:
        raise ValueError("冻结稿文内配图清单不一致")
    dest=html.escape(view["link"]["destination"],quote=True)
    if not re.search(r'<a href="'+re.escape(dest)+r'">[^<]*</a></p>\s*$',markup):
        raise ValueError("冻结稿文末链接校验失败")
    for ref in dict.fromkeys(refs):
        if not re.fullmatch(r"/api/distribution/media/[0-9a-f]{64}",ref):
            raise ValueError("冻结稿含未归档的图片")
        with transaction(path,False) as db:
            media=db.execute("SELECT mime,data FROM media WHERE sha=?",(ref.rsplit("/",1)[1],)).fetchone()
            status=db.execute("""SELECT t.state,b.control FROM platform_drafts t JOIN jobs j ON j.id=t.job
                JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch WHERE t.id=?""",(task["id"],)).fetchone()
        if status["state"]!="preparing" or status["control"]!="active":
            raise ValueError("任务状态已变化")
        if not media or hashlib.sha256(media["data"]).hexdigest()!=ref.rsplit("/",1)[1]:
            raise ValueError("本地图片校验失败")
        if bridge.identity()[0]!=task["user_id"]:
            raise ValueError("账号已切换")
        url=bridge.upload(media["data"],media["mime"])
        markup=markup.replace('src="'+ref+'"','src="'+html.escape(url,quote=True)+'"')
    return {"title":view["title"],"content":markup}


def run_task(path,task,bridge):
    stage="preparing"
    try:
        article=prepare_content(path,task,bridge)
        if bridge.identity()[0]!=task["user_id"]:
            raise ValueError("账号已切换")
        if not transition(path,task,stage,"sending"):
            return
        stage="sending"
        response=bridge.request("syncArticle",dict(platforms=["zhihu"],article=article),360)
        results=response.get("results",[])
        if len(results)!=1 or results[0].get("platform")!="zhihu" or results[0].get("draftOnly") is not True or results[0].get("success") is not True:
            raise ValueError("未收到草稿回执")
        url=edit_url(results[0].get("postUrl"))
        if bridge.identity()[0]!=task["user_id"]:
            raise ValueError("回执所属账号需要核对")
        transition(path,task,stage,"saved",url=url)
    except Exception:
        if stage=="sending":
            transition(path,task,stage,"unknown","草稿请求已发出，请到指定账号的知乎草稿箱核对")
        else:
            transition(path,task,stage,"failed","准备失败，请检查扩展连接、登录账号、冻结稿和配图后重试")
        # A late reply after a timeout must never be consumed as the next command.
        raise


def heartbeat(path,uid=None,name=""):
    with transaction(path) as db:
        if uid is None:
            db.execute("DELETE FROM draft_bridge WHERE singleton=1")
            return
        db.execute("INSERT OR REPLACE INTO draft_bridge VALUES(1,?,?,?)",(uid,name,int(time.time())))


def serve_bridge(path,port,extension_id,token):
    from websockets.sync.server import serve
    gate=threading.Lock()
    def handle(socket):
        if not gate.acquire(blocking=False):
            socket.close(1008,"已有扩展连接")
            return
        try:
            bridge=Bridge(socket,token)
            while True:
                uid,name=bridge.identity()
                heartbeat(path,uid,name)
                task=claim(path,uid) if uid else None
                if task:
                    run_task(path,task,bridge)
                else:
                    time.sleep(5)
        except Exception:
            print("草稿扩展连接已结束；已领取任务的结果记在管理台。",flush=True)
        finally:
            heartbeat(path)
            gate.release()
    initialize(path)
    with exclusive_worker(path), serve(handle,"127.0.0.1",port,origins=["chrome-extension://"+extension_id]) as server:
        with transaction(path) as db:
            now=int(time.time())
            db.execute("UPDATE platform_drafts SET state='failed',reason='准备过程中断，可重试',updated=? WHERE state='preparing'",(now,))
            db.execute("UPDATE platform_drafts SET state='unknown',reason='保存期间中断，请核对草稿箱',updated=? WHERE state='sending'",(now,))
        heartbeat(path)
        print(f"稿序草稿桥已启动 ws://127.0.0.1:{port}",flush=True)
        server.serve_forever()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="连接指定的文章同步助手扩展，仅处理人工确认的知乎草稿")
    parser.add_argument("--db",type=Path,default=Path(os.getenv("DIST_DB",str(DEFAULT_DB))))
    parser.add_argument("--port",type=int,default=9537)
    parser.add_argument("--extension-id",required=True)
    args=parser.parse_args()
    if not re.fullmatch(r"[a-p]{32}",args.extension_id):
        parser.error("请输入 Chrome 扩展管理页显示的扩展 ID")
    secret=os.environ.get("WECHATSYNC_TOKEN") or getpass.getpass("请输入在扩展中设置的连接 Token（隐藏输入）: ")
    if not secret:
        parser.error("需要扩展连接 Token")
    serve_bridge(args.db,args.port,args.extension_id,secret)
