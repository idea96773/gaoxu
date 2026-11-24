"""Optional interactive WechatSync draft bridge. Start separately from the simulator."""
import argparse
import base64
import getpass
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .markdown_media import replace_media_urls
from .draft_bridge import heartbeat, listener
from . import draft_images, frozen_reprint
from .platform_draft import DraftUrlConflict, authorized_source, claim, remember_receipt, require_rights, transition
from .runtime import instance_lock
from .store import DEFAULT_DB, initialize, transaction

CHUNK = 512 * 1024


class PreparationStopped(ValueError):
    """A local task decision; the extension connection is still usable."""


class DraftDeferred(PreparationStopped):
    """Keep the confirmed task queued until its batch and account are ready."""


def check_account(bridge,task):
    if bridge.identity()[0]!=task["user_id"]:
        raise DraftDeferred("请在指定知乎账号登录后继续保存草稿")


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

    def upload(self,blob,mime,check_task,check_transfer):
        encoded=base64.b64encode(blob).decode("ascii")
        check_transfer()
        if len(encoded)<=CHUNK:
            result=self.request("uploadImage",dict(platform="zhihu",mimeType=mime,imageData=encoded),180)
        else:
            upload=uuid.uuid4().hex
            self.request("uploadImage:start",dict(uploadId=upload,totalChunks=(len(encoded)+CHUNK-1)//CHUNK,
                                                 mimeType=mime,platform="zhihu"))
            for index,offset in enumerate(range(0,len(encoded),CHUNK)):
                check_task()
                self.request("uploadImage:chunk",dict(uploadId=upload,chunkIndex=index,data=encoded[offset:offset+CHUNK]))
            # Chunks are buffered in the extension; complete performs the actual
            # platform upload with whichever browser account is logged in now.
            check_transfer()
            result=self.request("uploadImage:complete",dict(uploadId=upload),180)
        url=result.get("url","")
        parsed=urlsplit(url)
        host=parsed.hostname or ""
        if (result.get("platform")!="zhihu" or parsed.scheme!="https" or parsed.username or parsed.password
                or not (host=="zhimg.com" or host.endswith(".zhimg.com"))):
            raise ValueError("知乎图片转存没有返回有效地址")
        return url


def prepare_content(path,task,bridge):
    with transaction(path,False) as db:
        try:
            report=frozen_reprint.inspect(db,authorized_source(db,task))
            revision=require_rights(db,task)
            reusable=draft_images.confirmed(db,task["id"],revision)
        except frozen_reprint.ContentError:
            raise
        except ValueError as exc:
            raise PreparationStopped("冻结稿、接收账号或使用授权校验失败，请核对任务详情") from exc
    def check_preparing(db):
        try:
            current=require_rights(db,task)
        except ValueError as exc:
            raise PreparationStopped("此稿发送到指定账号的授权已失效") from exc
        if current!=revision:
            raise PreparationStopped("配图准备期间授权已变更，请重新核对")
        status=db.execute("""SELECT t.state,b.control,j.state AS job_state FROM platform_drafts t JOIN jobs j ON j.id=t.job
            JOIN rows r ON r.id=j.row_id JOIN batches b ON b.id=r.batch WHERE t.id=?""",(task["id"],)).fetchone()
        if not status or status["state"]!="preparing" or status["job_state"]=="cancelled" or status["control"]=="cancelled":
            raise PreparationStopped("草稿任务已撤出队列")
        if status["control"]=="paused":
            raise DraftDeferred("批次已暂停，继续批次后按原授权保存草稿")
    def check_task():
        with transaction(path,False) as db:
            check_preparing(db)
    def check_transfer():
        check_account(bridge,task)
        check_task()
    uploaded={}
    for position,ref in enumerate(report["images"],1):
        if ref in uploaded:
            continue
        if ref in reusable:
            uploaded[ref]=reusable[ref]
            continue
        with transaction(path,False) as db:
            media=frozen_reprint.checked_image(db,ref,position)
        url=bridge.upload(media["data"],media["mime"],check_task,check_transfer)
        check_account(bridge,task)
        with transaction(path) as db:
            check_preparing(db)
            draft_images.remember(db,task["id"],revision,ref,url)
        uploaded[ref]=url
    return {"title":report["title"],"content":replace_media_urls(report["html"],uploaded)}


def run_task(path,task,bridge):
    try:
        article=prepare_content(path,task,bridge)
        check_account(bridge,task)
        try:
            sending=transition(path,task,"preparing","sending")
        except ValueError as exc:
            raise PreparationStopped("冻结稿、批次或使用授权已变化，请核对任务详情") from exc
        if not sending:
            return
    except (PreparationStopped,frozen_reprint.ContentError) as exc:
        state="queued" if isinstance(exc,DraftDeferred) else "failed"
        transition(path,task,"preparing",state,str(exc))
        return
    except Exception:
        transition(path,task,"preparing","failed","准备失败，请检查扩展连接、登录账号、冻结稿和配图后重试")
        raise

    try:
        response=bridge.request("syncArticle",dict(platforms=["zhihu"],article=article),360)
        results=response.get("results",[])
        if len(results)!=1 or results[0].get("platform")!="zhihu" or results[0].get("draftOnly") is not True or results[0].get("success") is not True:
            raise ValueError("未收到草稿回执")
        url=remember_receipt(path,task,results[0].get("postUrl"))
        if bridge.identity()[0]!=task["user_id"]:
            raise ValueError("回执所属账号需要核对")
        transition(path,task,"sending","saved",url=url)
    except DraftUrlConflict as exc:
        transition(path,task,"sending","unknown",str(exc))
    except Exception:
        transition(path,task,"sending","unknown","草稿请求已发出，请到指定账号的知乎草稿箱核对")
        raise


def serve_bridge(path,port,extension_id,token,check_only=False,job_id=None,expected_sha=None):
    from websockets.sync.server import serve
    gate=threading.Lock()
    session=uuid.uuid4().hex
    def handle(socket):
        if not gate.acquire(blocking=False):
            socket.close(1008,"已有扩展连接")
            return
        confirmed=False
        try:
            heartbeat(path,session=session,phase="checking")
            bridge=Bridge(socket,token)
            while True:
                uid,name=bridge.identity()
                confirmed=True
                heartbeat(path,uid,name,session=session)
                task=claim(path,uid,job_id,expected_sha) if uid and not check_only else None
                if task:
                    heartbeat(path,uid,name,session=session,phase="working")
                    run_task(path,task,bridge)
                else:
                    time.sleep(5)
        except Exception:
            print("草稿扩展连接已结束；已领取任务的结果记在管理台。",flush=True)
        finally:
            try:
                heartbeat(path,session=session,phase="disconnected" if confirmed else "unconfirmed")
            finally:
                gate.release()
    initialize(path)
    if job_id is not None:
        with transaction(path,False) as db:
            row=frozen_reprint.source(db,job_id,platform="zhihu")
            if expected_sha and row["sha"]!=expected_sha:
                raise ValueError("任务冻结版本与管理台不一致，请重新核对稿件")
            if row["control"]=="cancelled" or row["state"]=="cancelled":
                raise ValueError("所选任务已取消，请重新选择知乎转载任务")
    with instance_lock(str(path)+".draft-worker.lock"), serve(handle,"127.0.0.1",port,origins=["chrome-extension://"+extension_id]) as server:
        if not check_only:
            with transaction(path) as db:
                now=int(time.time())
                db.execute("""UPDATE platform_drafts SET state='failed',reason='准备过程中断，可重试',updated=?
                    WHERE state='preparing' AND (? IS NULL OR job=?)""",(now,job_id,job_id))
                db.execute("""UPDATE platform_drafts SET state='unknown',reason='保存期间中断，请核对草稿箱',updated=?
                    WHERE state='sending' AND (? IS NULL OR job=?)""",(now,job_id,job_id))
        with listener(path,port,check_only,session,job_id):
            mode="仅核对连接" if check_only else "草稿交付"
            scope=f"任务 #{job_id}" if job_id is not None else "账号已确认队列"
            print(f"稿序草稿桥已启动 ws://127.0.0.1:{port}（{mode} · {scope}）",flush=True)
            server.serve_forever()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="连接指定的文章同步助手扩展，仅处理人工确认的知乎草稿")
    parser.add_argument("--db",type=Path,default=Path(os.getenv("DIST_DB",str(DEFAULT_DB))))
    parser.add_argument("--port",type=int,default=9537)
    parser.add_argument("--extension-id",required=True)
    parser.add_argument("--check-only",action="store_true")
    parser.add_argument("--expected-sha",help="管理台返回的单篇冻结版本摘要")
    parser.add_argument("--job",type=int,help="只处理这个已冻结的知乎转载交付任务编号")
    args=parser.parse_args()
    if args.expected_sha and (args.job is None or not re.fullmatch(r"[0-9a-f]{64}",args.expected_sha)):
        parser.error("冻结版本摘要须与指定的任务编号一起使用")
    if not re.fullmatch(r"[a-p]{32}",args.extension_id):
        parser.error("请输入 Chrome 扩展管理页显示的扩展 ID")
    if args.job is not None and not 1<=args.job<=2**63-1:
        parser.error("请输入有效的正整数任务编号")
    secret=os.environ.get("WECHATSYNC_TOKEN") or getpass.getpass("请输入在扩展中设置的连接 Token（隐藏输入）: ")
    if not secret:
        parser.error("需要扩展连接 Token")
    try:
        serve_bridge(args.db,args.port,args.extension_id,secret,args.check_only,args.job,args.expected_sha)
    except ValueError as exc:
        parser.exit(1,f"{exc}\n")
