"""Exercise the local editorial API and restartable worker against an isolated database."""
import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from distribution.api import create_app
from distribution.editorial import tick_editorial
from distribution.engine import SimulatedFailure, claim, execute, tick
from distribution.store import BEIJING, compact, ledger_path, transaction
from distribution.test_workflow import export_folder

URL = "https://mp.weixin.qq.com/s/YFkz4nZBTLm-GHtqz_5lUw"
POOLS = {"zhihu": ["知乎甲", "知乎乙", "知乎丙"], "toutiao": ["头条甲", "头条乙"]}
NAMES = {"zhihu": "知乎", "toutiao": "今日头条"}
RULES = dict(gap=900, account_cap=4, platform_cap=9, start_hour=9, end_hour=18)
BASE = "/api/distribution/"
START = int(datetime(2026,11,21,9,0,tzinfo=BEIJING).timestamp())


def bjt(stamp):
    return datetime.fromtimestamp(stamp, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def targets(number):
    return {p:pool[(number-1) % len(pool)] for p,pool in POOLS.items()}


def copy_for_test(doc, platform):
    # Deterministic test double at the model boundary; production editorial queue still runs.
    return (NAMES[platform]+"稿｜"+doc["title"], NAMES[platform]+"版\n\n"+doc["markdown_content"].split("\n\n")[0])


def next_due(path):
    with transaction(path,False) as db:
        return db.execute("SELECT min(due) FROM jobs WHERE state='ready'").fetchone()[0]


def worker(path, now, fail, crash):
    def inject(job):
        if job["id"]==fail and job["attempts"]==1:
            raise SimulatedFailure("演练：单个账号交付失败")
    for _ in range(20000):
        ticket=claim(path,now)
        if ticket:
            execute(path,ticket,now,fault=inject,after_send=(lambda:os._exit(37)) if ticket[0]==crash else None)
            continue
        due=next_due(path)
        if due is None:
            return
        now=max(now,due)
    raise AssertionError("模拟队列未收敛")


def run_worker(path, now, failed=-1, crash=-1):
    proc=subprocess.run([sys.executable,__file__,"worker",str(path),str(now),str(failed),str(crash)],timeout=300)
    assert proc.returncode==(37 if crash!=-1 else 0),proc.returncode


def received(path):
    with transaction(ledger_path(path),False) as remote:
        return [dict(r) for r in remote.execute("SELECT * FROM delivered_drafts ORDER BY submitted,row_id,platform")]


def limits(records, field):
    per_day, per_account, times = Counter(), Counter(), defaultdict(list)
    for r in records:
        ts=r[field]
        moment=datetime.fromtimestamp(ts,BEIJING)
        assert RULES["start_hour"]<=moment.hour<RULES["end_hour"]
        day=moment.date().isoformat()
        p,a=r["platform"],r["account"]
        per_day[day,p]+=1
        per_account[day,p,a]+=1
        times[p].append(ts)
    assert max(per_day.values())<=RULES["platform_cap"]
    assert max(per_account.values())<=RULES["account_cap"]
    spans=[y-x for stamps in times.values() for x,y in zip(sorted(stamps),sorted(stamps)[1:])]
    assert min(spans)>=RULES["gap"]
    return per_day,per_account,min(spans)


def run(out, count):
    out.mkdir(parents=True,exist_ok=False)
    path=out/"dry-run.sqlite"
    library=out/"模拟源文件"
    client=create_app(path,library).test_client()
    now=START
    def post(route,data,status=None):
        with patch("time.time",return_value=now):
            result=client.post(BASE+route,json=data)
        assert result.status_code==status if status else result.status_code in (200,201),(route,result.json)
        return result.json
    def get(route):
        result=client.get(BASE+route)
        assert result.status_code==200,(route,result.json)
        return result.json

    directories=[export_folder(library,i).relative_to(library).as_posix() for i in range(1,count+1)]
    manifest=dict(request_id="bulk-dry-run",account_routing="rotate",directories=directories,
                  rules=RULES,targets=[dict(platform=p,account=a) for p,pool in POOLS.items() for a in pool])
    assert len(get("library")["items"])==count
    bid=post("library",manifest)["id"]
    assert post("library",manifest)["id"]==bid
    batch=lambda:get("batches/"+bid)
    rows=batch()["rows"]
    assert len(rows)==count and all(r["url"]==f"https://mp.weixin.qq.com/s/test-export-{r['line']}" for r in rows)
    ids={r["line"]:r["id"] for r in rows}
    intake_num,fail_num,crash_num=(17,42,36) if count==100 else (2,4,3)
    def intake_fault(job):
        if job["row_id"]==ids[intake_num] and job["stage"]=="ingest" and job["attempts"]==1:
            raise SimulatedFailure("演练：单篇导入失败")
    while tick(path,now,fault=intake_fault):
        pass
    failed_ingest,=batch()["failures"]
    assert (failed_ingest["row_id"],failed_ingest["stage"])==(ids[intake_num],"ingest")
    expected={}
    configured=set()
    def generate_pending():
        with patch("distribution.editorial.provider_ready",return_value=True), \
             patch.dict(os.environ,{"DIST_AI_MODEL":"offline-fixture"}), \
             patch("distribution.editorial.generate",side_effect=copy_for_test):
            while tick_editorial(path):
                pass

    def freeze_and_schedule(numbers):
        for n in numbers:
            rid=ids[n]
            route=f"articles/{rid}/editorial"
            info=get(route)
            assert {t["platform"]:t["account"] for t in info["targets"]}==targets(n)
            assert len(info["drafts"])==len(POOLS)
            for d in info["drafts"]:
                p,a=d["platform"],d["account"]
                assert a==targets(n)[p] and d["state"]=="editing" and d["rev"]==1
                if (p,a) not in configured:
                    post(route,dict(platform=p,account=a,action="link",link=dict(mode="direct",destination=URL,label="延伸阅读",verified=True)))
                    configured.add((p,a))
                ep=f"editorial/{d['id']}"
                edit={k:d[k] for k in ("title","body","edit_no")}
                view=post(ep,edit|dict(action="preview"))
                assert view["source_url"]==f"https://mp.weixin.qq.com/s/test-export-{n}"
                assert view["text"].count(URL)==1 and view["text"].endswith("延伸阅读："+URL)
                assert str(n) in view["title"] and view["images"]
                post(ep,edit|dict(action="approve",preview=view,rights=True,facts=True))
                job=post(ep+"/schedule",{})
                assert job["created"]
                repeat=post(ep+"/schedule",{})
                assert repeat["id"]==job["id"] and not repeat["created"]
                expected[n,p]=dict(number=n,row_id=rid,platform=p,account=a,draft=d["id"],revision=d["rev"],
                                   planned=job["slot"],job=job["id"],snapshot=view)
    with patch("distribution.editorial.provider_ready",return_value=True),patch.dict(os.environ,{"DIST_AI_MODEL":"offline-fixture"}):
        assert post("batches/"+bid+"/editorial",{"authorized":True})["created"]==count*2
    generate_pending()
    freeze_and_schedule(n for n in ids if n!=intake_num)
    crash=expected[crash_num,"toutiao"]
    failed=expected[fail_num,"zhihu"]
    print(f"{count} 篇入库，{len(expected)} 个冻结任务已排期；开始故障与恢复演练",flush=True)
    run_worker(path,now,failed["job"],crash["job"])
    interrupted=get(f"jobs/{crash['job']}")
    assert interrupted["job"]["state"]=="running" and not interrupted["job"]["receipt"]
    prior,=[r for r in received(path) if r["draft"]==crash["draft"]]
    assert json.loads(prior["snapshot"])==crash["snapshot"]
    assert any(y["state"]=="ready" for y in batch()["deliveries"])
    # Restart API and worker against persisted files. Resume after the lost worker's lease.
    client=create_app(path,library).test_client()
    now=interrupted["job"]["lease_until"]+1
    run_worker(path,now,failed["job"])
    first=received(path)
    by_row={rid:n for n,rid in ids.items()}
    first_keys=[(by_row[r["row_id"]],r["platform"]) for r in first]
    want={(n,p) for n in ids for p in POOLS}
    assert len(first_keys)==len(set(first_keys)) and want-set(first_keys)=={(intake_num,p) for p in POOLS}|{(fail_num,"zhihu")}
    assert len(first)==count*2-3
    assert get(f"jobs/{failed['job']}")["job"]["state"]=="failed"
    recovered=get(f"jobs/{crash['job']}")
    assert [a["outcome"] for a in recovered["history"]]==["interrupted","done"]
    assert recovered["job"]["receipt"]==prior["remote_id"]
    print(f"新工作进程完成对账；独立重试前已收到 {len(first)} 份",flush=True)
    with transaction(path,False) as db:
        now=max(now,db.execute("SELECT max(finished) FROM jobs").fetchone()[0])+1
    post(f"jobs/{failed_ingest['id']}/retry",{})
    post(f"jobs/{failed['job']}/retry",{})
    while tick(path,now):
        pass
    generate_pending()
    freeze_and_schedule([intake_num])
    run_worker(path,now)
    final=received(path)
    assert {(by_row[r["row_id"]],r["platform"]) for r in final}==want and len(final)==len(want)
    assert len({r["remote_id"] for r in final})==len(final)==len({r["key"] for r in final})
    final_batch=batch()
    assert final_batch["public"]==0 and not final_batch["failures"]
    assert final_batch["counts"]["done"]==count and final_batch["simulated"]==2*count
    deliveries={j["id"]:j for j in final_batch["deliveries"]}
    assert len(deliveries)==len(final) and all(j["state"]=="done" for j in deliveries.values())
    rows_csv=[]
    for r in final:
        n=by_row[r["row_id"]]
        exp=expected[n,r["platform"]]
        for k in ("row_id","platform","account","draft","revision","planned"):
            assert r[k]==exp[k],(n,k)
        snap=json.loads(r["snapshot"])
        assert snap==exp["snapshot"] and r["sha"]==hashlib.sha256(compact(snap).encode()).hexdigest()
        assert r["submitted"]>=r["planned"] and snap["text"].endswith("延伸阅读："+URL) and snap["text"].count(URL)==1
        scheduled=deliveries[exp["job"]]
        assert scheduled["finished"]==r["submitted"] and scheduled["receipt"]==r["remote_id"]
        repeat=post(f"editorial/{r['draft']}/schedule",{})
        assert not repeat["created"] and repeat["id"]==exp["job"]
        rows_csv.append([n,NAMES[r["platform"]],r["account"],r["draft"],r["revision"],exp["job"],bjt(r["planned"]),
                         bjt(r["submitted"]),r["submitted"]-r["planned"],snap["source_url"],URL,r["remote_id"],r["sha"]])
    assert received(path)==final
    planned,pa,gap_p=limits(final,"planned")
    actual,aa,gap_a=limits(final,"submitted")
    if count==100:
        assert max(actual.values())==9 and max(aa.values())==4
        assert max(planned.values())==9 and max(pa.values())==4
    attempts=get(f"jobs/{failed['job']}")["history"]
    assert attempts[0]["outcome"]=="failed" and attempts[-1]["outcome"]=="done"
    assert all(a["outcome"]=="ready" and "频率顺延" in a["reason"] for a in attempts[1:-1])
    assert [a["outcome"] for a in get(f"jobs/{failed_ingest['id']}")["history"]]==["failed","done"]
    with transaction(path,False) as db:
        assert db.execute("SELECT count(*) FROM drafts WHERE state='approved'").fetchone()[0]==len(final)
        assert db.execute("SELECT count(*) FROM jobs WHERE state!='done'").fetchone()[0]==0
    write_results(out,count,bid,rows_csv,planned,actual,pa,aa,gap_p,gap_a,intake_num,fail_num,crash_num,first,prior)


def write_results(out,count,bid,rows,planned,actual,pa,aa,gap_p,gap_a,intake_num,fail_num,crash_num,first,prior):
    def csv_file(name,header,records):
        with (out/name).open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.writer(stream)
            writer.writerow(header)
            writer.writerows(records)
    csv_file("逐任务对账.csv",["文章序号","平台","模拟账号","冻结稿","版本","任务","计划北京时间","实际北京时间","延后秒数",
                        "模拟原文地址","接收正文核验的文末链接","模拟回执","接收内容SHA256"],sorted(rows))
    days=sorted({day for day,_ in set(planned)|set(actual)})
    daily=[]
    for day in days:
        for p,accounts in POOLS.items():
            for a in accounts:
                daily.append([day,NAMES[p],a,pa[day,p,a],aa[day,p,a],RULES["account_cap"],
                              planned[day,p],actual[day,p],RULES["platform_cap"]])
    csv_file("逐日限额.csv",["北京时间日期","平台","模拟账号","账号计划数","账号实收数","账号日上限","平台计划数","平台实收数","平台日上限"],daily)
    delay=[r[8] for r in rows]
    report=["# 冻结稿批量交付验收","",f"批次 `{bid}`。使用 {count} 个独立本地 Markdown、HTML 与配图目录，"
            f"预期 {count*2} 个文章与平台组合，独立模拟接收账本确认 {len(rows)} 条唯一交付。漏交、重复、错账号、版本不符和文末链接异常均为 0。","",
            "演练使用北京时间模拟时钟，模型返回由本地确定性样文模拟，审核标记仅用于测试。所有账号均为虚构别名，公开发布数为 0。"
            "接收账本的 delivered_drafts 表作为交付证据，普通模拟发布回执未计入。","",
            "## 分稿和排期","",f"观察日期 {days[0]} 至 {days[-1]}。窗口 09:00–18:00，平台间隔 15 分钟，账号每天最多 4 份、平台每天最多 9 份。"
            f"计划和实收两套时间均独立校验。最短计划间隔 {gap_p} 秒，最短实收间隔 {gap_a} 秒。","",
            "| 平台 | 分配到各账号的总份数 | 平台单日最高（计划 / 实收） | 单账号单日最高（计划 / 实收） |",
            "|---|---|---|---|"]
    for p,pool in POOLS.items():
        counts=Counter(r[2] for r in rows if r[1]==NAMES[p])
        amounts="、".join(f"{a} {counts[a]}" for a in pool)
        report.append(f"| {NAMES[p]} | {amounts} | {max(planned[d,p] for d in days)} / {max(actual[d,p] for d in days)} | "
                      f"{max(pa[d,p,a] for d in days for a in pool)} / {max(aa[d,p,a] for d in days for a in pool)} |")
    report.extend(["",f"按原计划秒数交付 {delay.count(0)} 份，延后 {sum(x>0 for x in delay)} 份，提前 0 份；最长延后 {max(delay)} 秒。演练刻意等其余可执行任务完成后才重试失败项，该延后包含测试等待。",
            "","## 故障与恢复","",
            f"- 第 {intake_num} 篇在导入阶段失败，第 {fail_num} 篇仅知乎交付失败。重试前已经收到 {len(first)} 份，"
            f"缺少的正好是第 {intake_num} 篇的两个平台版本和第 {fail_num} 篇的知乎版本。仅重试这两个失败步骤后收齐。",
            f"- 第 {crash_num} 篇头条稿在独立接收库写入后、工作队列确认前强制退出工作进程。重建 API 实例后启动新工作进程恢复。"
            f"尝试记录为“中断、完成”，两次核对得到同一回执 `{prior['remote_id']}`。",
            f"- 每个冻结稿在排期后和交付后各重复提交一次，{2*len(rows)} 次均返回原任务号。",
            f"- 对每条接收记录核对文章来源、账号、版本、原定时间、完整冻结快照、SHA-256 和文末唯一的延伸阅读链接。目标为 {URL}。",
            "","[逐任务对账](逐任务对账.csv) · [逐日限额](逐日限额.csv)"])
    (out/"验收报告.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    print("\n".join(report),flush=True)


if __name__=="__main__":
    if sys.argv[1]=="worker":
        worker(Path(sys.argv[2]),*(int(x) for x in sys.argv[3:]))
    else:
        run(Path(sys.argv[1]),int(sys.argv[2]))

