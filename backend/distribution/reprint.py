"""Manual library selection, immutable review and local-only reprint scheduling."""
import html
import json
import re
import time
import uuid
from pathlib import Path, PurePosixPath

from flask import jsonify, request

from .delivery import existing_deliveries, schedule_in
from .editorial import checked_link
from .library import read_folder, selected_header, selection_key, validate_directories
from .markdown_media import MARKDOWN, inline_tokens, local_path
from .store import canonicalize, clean_rules, compact, digest, enqueue, transaction

DESTINATION = "https://mp.weixin.qq.com/s/YFkz4nZBTLm-GHtqz_5lUw"
CHANNELS = ("zhihu", "toutiao")


def pools_for(targets):
    if not isinstance(targets,list) or not 1<=len(targets)<=28:
        raise ValueError("请勾选 1 至 28 个模拟账号")
    pools={}
    for target in targets:
        if not isinstance(target,dict):
            raise ValueError("账号格式无效")
        p,a=target.get("platform"),target.get("account")
        if p not in CHANNELS or not isinstance(a,str) or not re.fullmatch(r"[\w-]{1,40}",a):
            raise ValueError("仅支持知乎、头条的模拟账号别名")
        if a not in pools.setdefault(p,[]):
            pools[p].append(a)
    return {p:sorted(accounts) for p,accounts in pools.items()}


def review_snapshot(doc, destination, platforms, links):
    if doc["missing_images"]:
        # Keep local references actionable and avoid echoing remote image URLs into logs.
        missing=doc["missing_images"]
        local=[ref for ref in missing if ref.startswith("img/") and "?" not in ref and "#" not in ref]
        names="、".join(ref[:120] for ref in local[:3])
        raise ValueError(f"有 {len(missing)} 处配图无法本地读取，请核对 Markdown、HTML 与 img 文件夹"+(f"（{names}）" if names else ""))
    if not canonicalize(doc["source_url"] or ""):
        raise ValueError("Markdown 缺少有效的公众号原文地址，请检查导出设置并补全资料")
    body=doc["markdown_content"]
    tokens=MARKDOWN.parse(body)
    images,files=[],{}
    media=doc["media_map"]
    by_name={local_path(ref):asset for ref,asset in media.items()}
    for token in inline_tokens(tokens):
        if token.type!="image":
            continue
        ref=token.attrGet("src") or ""
        name=local_path(ref)
        asset=media.get(ref) or by_name.get(name)
        if not asset or not name or not name.startswith("img/") or re.search(r'[<>:"|?*\x00-\x1f]',name):
            raise ValueError("正文含无法本地打包的配图")
        images.append(asset)
        files[name]=asset
        token.attrSet("src",asset)
    for token in inline_tokens(tokens):
        if token.type=="link_open":
            asset=files.get(local_path(token.attrGet("href") or ""))
            if asset:
                token.attrSet("href",asset)
    if set(images)!=set(doc["images"]):
        raise ValueError("HTML 与 Markdown 配图不一致，请检查下载文件")
    title=doc["title"]
    source=" · ".join(x for x in (doc["author"],doc["source_name"],doc["publish_time"]) if x)
    credit="转载"+(" · "+source if source else "")
    source_url=doc["source_url"]
    header=f'<h1>{html.escape(title)}</h1><p>{html.escape(credit)}　<a href="{html.escape(source_url,quote=True)}">原文地址</a></p>'
    raw=MARKDOWN.renderer.render(tokens,MARKDOWN.options,{})
    heading=f"# {title}\n\n{credit}　[原文地址]({source_url})\n\n"

    def edition(rule):
        mode=rule.get("mode","direct")
        short=rule.get("short","")
        url=short if mode=="short" else destination
        anchor=f'<p><a href="{html.escape(url,quote=True)}">延伸阅读</a></p>'
        append=mode!="omit" and not raw.rstrip().endswith(anchor)
        # Leave the author's original links intact in every representation.
        markdown=heading+body+(f"\n\n[延伸阅读]({url})" if append else "")+"\n"
        return dict(kind="reprint",title=title,text=body+("\n\n延伸阅读："+url if append else ""),
                    source_url=source_url,images=images,html=header+raw+(anchor if append else ""),
                    markdown=markdown,files=files,format="原文转载",
                    link=dict(mode=mode,destination=destination if mode!="omit" else "",short=short,label="延伸阅读"))

    if not links:
        return edition({})
    return {"editions":{platform:edition(links.get(platform,{})) for platform in platforms}}


def platform_links(raw,pools,destination):
    if not isinstance(raw,dict) or set(raw)-set(pools):
        raise ValueError("文末链接只能设置在本批选中的平台")
    links={}
    for platform,rule in raw.items():
        if not isinstance(rule,dict) or rule.get("mode") not in ("direct","omit","short"):
            raise ValueError("请为平台选择有效的文末链接方式")
        mode=rule["mode"]
        if mode=="short":
            value=checked_link(dict(mode=mode,destination=destination,short=rule.get("short"),verified=rule.get("verified")))
            links[platform]=dict(mode=mode,short=value["short"],verified=True)
        elif mode=="omit":
            links[platform]=dict(mode=mode)
    return links


def validated_settings(data):
    destination=data.get("destination",DESTINATION)
    if not isinstance(destination,str) or not canonicalize(destination) or re.search(r'[\s<>()\\\[\]]',destination):
        raise ValueError("请输入 HTTPS 公众号文章地址")
    pools=pools_for(data.get("targets"))
    targets=[dict(platform=p,account=a) for p,accounts in pools.items() for a in accounts]
    settings=dict(targets=targets,destination=destination,rules=clean_rules(data.get("rules",{}),targets))
    links=platform_links(data.get("links",{}),pools,destination)
    if links:
        settings["links"]=links
    return settings


def create(path,root,data):
    key=data.get("request_id")
    if not isinstance(key,str) or not 1<=len(key)<=128:
        raise ValueError("缺少请求编号")
    settings=validated_settings(data)
    targets,destination,rules=settings["targets"],settings["destination"],settings["rules"]
    pools=pools_for(targets)
    dirs=data.get("directories")
    validate_directories(dirs)
    selections=data.get("selections")
    identity=["reprint",dirs,pools,rules,destination]
    if "links" in settings:
        identity.append(settings["links"])
    if selections is not None:
        if not isinstance(selections,dict) or selections.keys()!=set(dirs):
            raise ValueError("选稿记录与提交的目录不一致，请重新选择")
        for mark in selections.values():
            if (not isinstance(mark,dict) or not isinstance(mark.get("title"),str) or not mark["title"].strip()
                    or len(mark["title"])>8192 or not isinstance(mark.get("key"),str)
                    or not re.fullmatch(r"(url|file)-[0-9a-f]{64}",mark["key"]) or set(mark)!={"title","key"}):
                raise ValueError("选稿记录无效，请重新读取目录")
        identity.append({"selections":selections})
    fingerprint=digest(compact(identity))
    def previous(db):
        row=db.execute("SELECT id,digest FROM batches WHERE request_key=?",(key,)).fetchone()
        if row and row["digest"]!=fingerprint:
            raise ValueError("请求编号已用于不同的批次")
        return row["id"] if row else None
    now=int(time.time())
    with transaction(path) as db:
        old=previous(db)
        if old:
            return old
        bid=uuid.uuid4().hex
        db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?)",
                   (bid,key,fingerprint,int(time.time()),"active",compact(targets),compact(rules)))
        db.execute("INSERT INTO reprint_batches VALUES(?,?)",(bid,destination))
        if "links" in settings:
            db.execute("INSERT INTO reprint_link_rules VALUES(?,?)",(bid,compact(settings["links"])))
        owners={}
        for line,directory in enumerate(dirs,1):
            if directory in owners:
                original,original_line=owners[directory]
                rid=db.execute("""INSERT INTO rows(batch,line,url,kind,owner,note)
                    VALUES(?,?,?,'duplicate',?,?)""",
                    (bid,line,directory,original,f"同一目录重复提交，处理进度见第 {original_line} 行。")).lastrowid
                db.execute("INSERT INTO reprint_reviews VALUES(?,?,'duplicate','','null')",
                           (rid,PurePosixPath(directory).name or directory))
                continue
            rid=db.execute("INSERT INTO rows(batch,line,url,kind) VALUES(?,?,?,'reprint')",
                           (bid,line,directory)).lastrowid
            owners[directory]=(rid,line)
            db.execute("INSERT INTO reprint_sources VALUES(?,?,?)",(rid,str(Path(root).absolute()),directory))
            if selections is not None:
                db.execute("INSERT INTO reprint_selections VALUES(?,?)",(rid,selections[directory]["key"]))
            db.execute("INSERT INTO reprint_reviews VALUES(?,?, 'preparing','','null')",
                       (rid,selections[directory]["title"] if selections is not None else PurePosixPath(directory).name or directory))
            enqueue(db,rid,"reprint_prepare",now)
        return bid


def reuse_existing(path,ticket,platforms,header,now):
    from .engine import close_attempt
    title,url,ref=header["title"],header["url"],header["source"]
    if not ref:
        return False
    identity="folder:"+ref
    with transaction(path) as db:
        job=db.execute("""SELECT j.*,b.control FROM jobs j JOIN rows r ON r.id=j.row_id
            JOIN batches b ON b.id=r.batch WHERE j.id=? AND j.token=? AND j.state='running'""",ticket).fetchone()
        if not job:
            return True
        if job["control"]!="active":
            close_attempt(db,job,now,"cancelled" if job["control"]=="cancelled" else "ready")
            return True
        prior=existing_deliveries(db,[identity]).get(identity,{})
        if not all(p in prior for p in platforms):
            return False
        rid=job["row_id"]
        db.executemany("INSERT INTO reprint_assignments VALUES(?,?,?,1)",
                       [(rid,p,prior[p]["id"]) for p in platforms])
        db.execute("UPDATE rows SET url=?,canonical=?,note=? WHERE id=?",
                   (url,identity,"所选平台均已有任务，可在原任务中查看冻结稿和回执。",rid))
        db.execute("UPDATE reprint_reviews SET title=?,state='duplicate',sha='',snapshot='null' WHERE row_id=?",(title,rid))
        close_attempt(db,job,now,"done")
        return True


def check_selected_source(source,ref,markdown_sha,full=False):
    previous=source["canonical"]
    if previous:
        if (ref or full) and previous!="folder:"+str(ref):
            raise ValueError("原文链接与上次读取的文章不同，请恢复原文件，或将新文章另行选稿")
        return
    key=source["selection_key"]
    if key:
        kind=key.split("-",1)[0]
        if kind=="url" and not ref and not full:
            return  # The export header may exceed the directory scan's read limit.
        value=ref if kind=="url" else markdown_sha
        if selection_key(source["root"],source["directory"],kind,value)!=key:
            raise ValueError("目录中的文章与选稿记录不一致，请恢复原文件后重试，或将新文章另行选稿")


def prepare_one(path,ticket,now):
    from .engine import close_attempt
    query="""SELECT j.*,b.control,s.root,s.directory,r.canonical,sel.selection_key,
        spec.destination,b.targets,links.payload links FROM jobs j
        JOIN reprint_sources s ON s.row_id=j.row_id JOIN rows r ON r.id=j.row_id
        LEFT JOIN reprint_selections sel ON sel.row_id=s.row_id
        JOIN batches b ON b.id=r.batch JOIN reprint_batches spec ON spec.batch=b.id
        LEFT JOIN reprint_link_rules links ON links.batch=b.id
        WHERE j.id=? AND j.token=? AND j.state='running' AND j.stage='reprint_prepare'"""
    with transaction(path,False) as db:
        source=db.execute(query,ticket).fetchone()
    if not source:
        return
    started=time.monotonic()
    transient=False
    error=None
    read_started=source["control"]=="active"
    if read_started:
        try:
            platforms=pools_for(json.loads(source["targets"]))
            header=selected_header(source["root"],source["directory"])
            check_selected_source(source,header["source"],header["markdown_sha"])
            if reuse_existing(path,ticket,platforms,header,now):
                return
            item,_=read_folder(source["root"],source["directory"])
            if item["error"]:
                raise ValueError(item["error"])
            parsed_ref=canonicalize(item["doc"]["source_url"] or "")
            if header["source"] and header["source"]!=parsed_ref:
                raise ValueError("读取期间原文链接已变化，请等下载完成后重新核对文章")
            check_selected_source(source,parsed_ref,item.get("markdown_sha"),full=True)
            view=review_snapshot(item["doc"],source["destination"],platforms,json.loads(source["links"] or "{}"))
            payload=compact(view)
            sha=digest(payload)
        except OSError as exc:
            error=str(exc)
            transient=isinstance(exc,(TimeoutError,BlockingIOError)) or getattr(exc,"winerror",0) in (32,33)
        except (ValueError,RecursionError,AssertionError) as exc:
            error=str(exc) or "文章格式异常，请检查下载文件"
    with transaction(path) as db:
        now+=int(time.monotonic()-started)
        job=db.execute(query,ticket).fetchone()
        if not job:
            return
        if job["control"]!="active" or not read_started:
            close_attempt(db,job,now,"cancelled" if job["control"]=="cancelled" else "ready")
            return
        rid=job["row_id"]
        if error is not None:
            db.execute("UPDATE rows SET note=? WHERE id=?",(error,rid))
            if transient:
                # A manual reread or retry starts a fresh automatic backoff budget.
                retries=db.execute("""SELECT count(*) FROM attempts WHERE job=? AND outcome='ready'
                    AND reason<>'' AND seq>coalesce((SELECT max(seq) FROM attempts WHERE job=?
                    AND outcome IN ('done','failed')),0)""",(job["id"],job["id"])).fetchone()[0]
                if retries<2:
                    close_attempt(db,job,now,"ready",error,now+5*2**retries)
                    return
            db.execute("UPDATE reprint_reviews SET state='invalid' WHERE row_id=?",(rid,))
            close_attempt(db,job,now,"failed",error)
            return
        for media_sha,mime,blob in item["assets"].values():
            db.execute("INSERT OR IGNORE INTO media VALUES(?,?,?)",(media_sha,mime,blob))
        db.execute("UPDATE rows SET url=?,canonical=?,note='' WHERE id=?",(item["url"],"folder:"+item["canonical"],rid))
        db.execute("UPDATE reprint_reviews SET title=?,state='pending',sha=?,snapshot=? WHERE row_id=?",
                   (item["doc"]["title"],sha,payload,rid))
        close_attempt(db,job,now,"done")


def approve_one(path,bid,rid,sha,now,revision=1,expected_prior=None):
    with transaction(path) as db:
        row=db.execute("""SELECT q.*,r.canonical,b.targets,b.control,coalesce(j.revision,1) revision
            FROM reprint_reviews q JOIN rows r ON r.id=q.row_id LEFT JOIN jobs j ON j.row_id=r.id AND j.stage='reprint_prepare'
            JOIN batches b ON b.id=r.batch WHERE b.id=? AND r.id=?""",(bid,rid)).fetchone()
        if not row or row["sha"]!=sha or type(revision) is not int or row["revision"]!=revision:
            raise ValueError("审核条目与预览版本不一致")
        if row["state"] in ("approved","duplicate"):
            return row["state"]
        if row["state"]!="pending" or row["control"]!="active":
            raise ValueError("仅运行中批次的待审文章可通过")
        reviewed=json.loads(row["snapshot"])
        assigned=[]
        history=existing_deliveries(db,[row["canonical"]]).get(row["canonical"],{})
        pools=pools_for(json.loads(row["targets"]))
        if expected_prior is not None:
            current={p:history[p]["id"] for p in pools if p in history}
            if (not isinstance(expected_prior,dict) or any(type(v) is not int for v in expected_prior.values())
                    or expected_prior!=current):
                raise ValueError("计划沿用的任务已变化，请刷新后重新核对需排期的平台")
        for p,accounts in pools.items():
            prior=history.get(p)
            view=reviewed["editions"][p] if "editions" in reviewed else reviewed
            if prior:
                if expected_prior is None:
                    frozen_sha=db.execute("SELECT sha FROM frozen_deliveries WHERE job=?",(prior["id"],)).fetchone()[0]
                    if frozen_sha!=digest(compact(view)):
                        raise ValueError("已有任务与本次送审稿不同，请按平台核对冻结稿并确认沿用任务")
                db.execute("INSERT INTO reprint_assignments VALUES(?,?,?,1)",(rid,p,prior["id"]))
                continue
            pool=compact(["reprint",p,accounts])
            cursor=db.execute("SELECT position FROM account_rotation WHERE pool=?",(pool,)).fetchone()
            position=cursor[0] if cursor else 0
            account=accounts[position%len(accounts)]
            did=db.execute("""INSERT INTO drafts(row_id,platform,account,rev,state,title,body,provider,frozen)
                VALUES(?,?,?,1,'approved',?,?,'reprint',?)""",(rid,p,account,view["title"],view["text"],compact(view))).lastrowid
            job=schedule_in(db,did,{},now)
            db.execute("INSERT INTO reprint_assignments VALUES(?,?,?,0)",(rid,p,job["id"]))
            db.execute("INSERT OR REPLACE INTO account_rotation VALUES(?,?)",(pool,(position+1)%len(accounts)))
            assigned.append(dict(platform=p,account=account))
        db.execute("INSERT INTO row_targets VALUES(?,?)",(rid,compact(assigned)))
        state="approved" if assigned else "duplicate"
        db.execute("UPDATE reprint_reviews SET state=? WHERE row_id=?",(state,rid))
        return state


def overview(db,bid,platforms=()):
    items=db.execute("""SELECT r.id,r.line,r.note,r.canonical,r.owner,q.title,q.state,q.sha,j.id AS prepare_job,
        coalesce(j.revision,1) revision,j.state AS prepare_state,j.attempts AS prepare_attempts FROM rows r
        JOIN reprint_reviews q ON r.id=q.row_id
        LEFT JOIN jobs j ON j.row_id=r.id AND j.stage='reprint_prepare' WHERE r.batch=? ORDER BY r.line""",(bid,)).fetchall()
    assignments=db.execute("""SELECT a.row_id,a.platform,a.reused,j.account,j.slot,j.due,j.state,j.id,j.receipt,f.draft,
        coalesce(pin.name,t.name) draft_recipient,coalesce(pin.user_id,t.user_id) draft_recipient_id,
        coalesce((SELECT e.scope FROM reprint_rights e WHERE e.job=j.id ORDER BY e.id DESC LIMIT 1),'local') usage_scope
        FROM reprint_assignments a JOIN rows r ON r.id=a.row_id JOIN jobs j ON j.id=a.job
        JOIN frozen_deliveries f ON j.id=f.job LEFT JOIN zhihu_draft_recipients pin ON pin.job=j.id
        LEFT JOIN platform_drafts t ON t.job=j.id WHERE r.batch=? ORDER BY a.row_id,a.platform""",(bid,)).fetchall()
    by_row={}
    for a in assignments:
        by_row.setdefault(a["row_id"],[]).append(dict(a))
    prior=existing_deliveries(db,[r["canonical"] for r in items if r["state"]=="pending"]) if platforms else {}
    result=[]
    for r in items:
        row=dict(r)
        history=prior.get(row.pop("canonical"),{})
        row["assignments"]=([dict(job,reused=1) for job in by_row.get(row["owner"],[])]
                            if row["owner"] else by_row.get(r["id"],[]))
        if platforms and row["state"]=="pending":
            row["prior_jobs"]=[history[p] for p in platforms if p in history]
            row["missing_platforms"]=[p for p in platforms if p not in history]
        result.append(row)
    return result


def register(api,path,root,body):
    @api.get("/reprint-settings")
    def read_defaults():
        with transaction(path,False) as db:
            row=db.execute("SELECT revision,payload FROM reprint_defaults WHERE singleton=1").fetchone()
            if row:
                return jsonify(revision=row["revision"],settings=json.loads(row["payload"]))
        return jsonify(revision=0,settings=None)

    @api.post("/reprint-settings")
    def write_defaults():
        data=body()
        expected=data.get("revision")
        if type(expected) is not int or expected<0:
            raise ValueError("请读取当前常用设置后保存")
        settings=validated_settings(data)
        with transaction(path) as db:
            row=db.execute("SELECT revision,payload FROM reprint_defaults WHERE singleton=1").fetchone()
            if row and json.loads(row["payload"])==settings:
                return jsonify(revision=row["revision"],settings=settings)
            if (row["revision"] if row else 0)!=expected:
                return jsonify(error="常用设置已被更新，请重新读取后保存"),409
            db.execute("INSERT OR REPLACE INTO reprint_defaults VALUES(1,?,?)",(expected+1,compact(settings)))
        return jsonify(revision=expected+1,settings=settings)

    @api.post("/reprints")
    def new_reprints():
        return jsonify(id=create(path,root,body())),201

    @api.post("/reprints/<bid>/<int:rid>/reread")
    def reread_reprint(bid,rid):
        data=body()
        with transaction(path) as db:
            row=db.execute("""SELECT q.state,q.sha,j.id,j.revision,j.state job_state,b.control
                FROM reprint_reviews q JOIN rows r ON r.id=q.row_id JOIN batches b ON b.id=r.batch
                JOIN jobs j ON j.row_id=r.id AND j.stage='reprint_prepare'
                JOIN reprint_sources s ON s.row_id=r.id WHERE r.id=? AND b.id=?""",(rid,bid)).fetchone()
            if not row:
                raise ValueError("此稿缺少可重读的来源目录，请重新选稿")
            if (row["control"]!="active" or row["state"]!="pending" or row["job_state"]!="done"
                    or data.get("sha")!=row["sha"] or type(data.get("revision")) is not int or data["revision"]!=row["revision"]):
                return jsonify(error="批次或预览版本已变化，请刷新后再操作"),409
            db.execute("""UPDATE jobs SET state='ready',due=?,finished=NULL,failure='',revision=revision+1
                WHERE id=?""",(int(time.time()),row["id"]))
            db.execute("UPDATE reprint_reviews SET state='preparing',sha='',snapshot='null' WHERE row_id=?",(rid,))
            return jsonify(id=row["id"],revision=row["revision"]+1)

    @api.get("/reprints/<bid>/<int:rid>")
    def preview_reprint(bid,rid):
        with transaction(path,False) as db:
            r=db.execute("""SELECT q.*,coalesce(j.revision,1) revision,b.targets,r.canonical,r.owner FROM reprint_reviews q JOIN rows r ON r.id=q.row_id
                JOIN batches b ON b.id=r.batch
                LEFT JOIN jobs j ON j.row_id=r.id AND j.stage='reprint_prepare'
                WHERE r.batch=? AND r.id=?""",(bid,rid)).fetchone()
            if not r:
                raise KeyError("找不到转载稿")
            if r["state"] not in ("pending","approved","duplicate"):
                return jsonify(error="这篇文章尚无可审核的稿件"),409
            platform=request.args.get("platform")
            platforms=pools_for(json.loads(r["targets"]))
            if platform is not None and platform not in platforms:
                raise ValueError("该平台不在本批次中")
            if r["state"]=="pending":
                history=existing_deliveries(db,[r["canonical"]]).get(r["canonical"],{})
                jobs={p:(history[p]["id"],True) for p in platforms if p in history}
            else:
                jobs={a["platform"]:(a["job"],bool(a["reused"] or r["owner"])) for a in db.execute(
                    "SELECT platform,job,reused FROM reprint_assignments WHERE row_id=?", (r["owner"] or rid,))}
            if not platform and any(reused for _,reused in jobs.values()):
                raise ValueError("已有平台任务，请选择平台核对对应冻结稿")
            if platform in jobs:
                job,reused=jobs[platform]
                frozen=db.execute("""SELECT f.snapshot,f.sha,j.account FROM frozen_deliveries f
                    JOIN jobs j ON j.id=f.job WHERE f.job=?""",(job,)).fetchone()
                if not frozen or digest(frozen["snapshot"])!=frozen["sha"]:
                    return jsonify(error="任务绑定的冻结稿校验失败，请核对任务记录"),409
                return jsonify(snapshot=json.loads(frozen["snapshot"]),platform=platform,
                               edition_sha=frozen["sha"],sha=r["sha"],revision=r["revision"],
                               job=job,account=frozen["account"],reused=reused)
            if not r["sha"]:
                return jsonify(error="该条目暂无可预览的平台稿件"),409
            reviewed=json.loads(r["snapshot"])
            if "editions" in reviewed and not platform:
                raise ValueError("此稿使用平台专属文末链接，请选择平台预览")
            view=reviewed["editions"][platform] if "editions" in reviewed else reviewed
            return jsonify(snapshot=view,platform=platform,edition_sha=digest(compact(view)),sha=r["sha"],
                           revision=r["revision"],job=None,account=None,reused=False)

    @api.post("/reprints/<bid>/approve")
    def approve_reprints(bid):
        data=body()
        reviews=data.get("items")
        if data.get("approved") is not True or not isinstance(reviews,list) or not 1<=len(reviews)<=100:
            raise ValueError("请先勾选已审核的稿件")
        result=[]
        for review in reviews:
            rid=review.get("id") if isinstance(review,dict) else None
            try:
                if type(rid) is not int:
                    raise ValueError("审核条目无效")
                state=approve_one(path,bid,rid,review.get("sha"),int(time.time()),
                                  review.get("revision",1),review.get("expected_prior"))
                result.append(dict(id=rid,state=state))
            except ValueError as exc:
                result.append(dict(id=rid,error=str(exc)))
        return jsonify(items=result)

