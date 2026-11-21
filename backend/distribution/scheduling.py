"""Beijing scheduling windows and quotas for a platform and its individual accounts."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True)
class Limits:
    start: int
    end: int
    platform_cap: int
    account_cap: int
    platform_gap: int
    account_gap: int


@dataclass
class DayUsage:
    platform_cap: int
    account_cap: int
    platform_count: int = 0
    account_count: int = 0


def minutes(value, *, end=False):
    if end and value=="24:00":
        return 1440
    if not isinstance(value,str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]",value):
        raise ValueError("时段请填写 HH:MM，结束时间也可用 24:00")
    return int(value[:2])*60+int(value[3:])


def effective_limits(rules,platform,account):
    by_account={r.get("account"):r for r in rules.get("overrides",[]) if r["platform"]==platform}
    channel=by_account.get(None,{})
    profile=by_account.get(account,{})
    start=minutes(channel["start"]) if "start" in channel else rules["start_hour"]*60
    end=minutes(channel["end"],end=True) if "end" in channel else rules["end_hour"]*60
    if start>=end:
        raise ValueError("平台结束时间须晚于开始时间")
    first=minutes(profile["start"]) if "start" in profile else start
    last=minutes(profile["end"],end=True) if "end" in profile else end
    if not start<=first<last<=end:
        raise ValueError(f"{platform} 的账号「{account}」需要在平台时段内设置起止时间")
    return Limits(first,last,channel.get("daily_cap",rules["platform_cap"]),profile.get("daily_cap",rules["account_cap"]),
                  channel.get("gap",rules["gap"]),profile.get("gap",0))


def validate_overrides(rules,targets):
    scopes=rules.get("overrides",[])
    if not isinstance(scopes,list) or len(scopes)>35:
        raise ValueError("独立排期规则格式无效")
    accounts={(t["platform"],t["account"]) for t in targets}
    allowed=accounts|{(p,None) for p,_ in accounts}
    used=set()
    for scope in scopes:
        fields={"start","end","gap","daily_cap"}
        if (not isinstance(scope,dict) or set(scope)-fields-{"platform","account"} or not fields.intersection(scope)
                or not isinstance(scope.get("platform"),str)
                or ("account" in scope and not isinstance(scope["account"],str))):
            raise ValueError("请为选中的平台或账号至少设置一项规则")
        key=(scope["platform"],scope.get("account"))
        if key not in allowed or key in used:
            raise ValueError("独立规则目标未选中或有重复")
        used.add(key)
        for name,maximum in (("gap",86400),("daily_cap",1000 if key in accounts else 10000)):
            if name in scope and (type(scope[name]) is not int or not 1<=scope[name]<=maximum):
                raise ValueError("日上限或间隔超出范围")
    for p,a in accounts:
        effective_limits(rules,p,a)


@lru_cache(maxsize=512)
def stored_limits(raw,platform,account):
    return effective_limits(json.loads(raw),platform,account)


def available_slot(stamp,platform,account,rules,bookings,*,append=False):
    limits=effective_limits(rules,platform,account)
    # Convert each reservation into its blocked range of integer Unix seconds.
    # Sorting by range start also handles overlapping gaps from different batches.
    blocked=[]
    days={}
    empty=DayUsage(limits.platform_cap,limits.account_cap)
    for row in bookings:
        mark=row["mark"]
        prior=stored_limits(row["rules"],platform,row["account"])
        gap=max(limits.platform_gap,prior.platform_gap)
        day=datetime.fromtimestamp(mark,BEIJING).date()
        counts=days.setdefault(day,DayUsage(limits.platform_cap,limits.account_cap))
        counts.platform_count+=1
        counts.platform_cap=min(counts.platform_cap,prior.platform_cap)
        if row["account"]==account:
            counts.account_count+=1
            counts.account_cap=min(counts.account_cap,prior.account_cap)
            gap=max(gap,limits.account_gap,prior.account_gap)
        blocked.append((mark-gap+1,mark+gap))
        if append:
            stamp=max(stamp,mark+gap)
    blocked.sort()
    pos=0
    day=datetime.fromtimestamp(stamp,BEIJING).replace(hour=0,minute=0,second=0,microsecond=0)
    while True:
        iso=day.date().isoformat()
        opening=iso in rules["workdays"] or (iso not in rules["holidays"] and
                 (not rules["weekdays_only"] or day.weekday()<5))
        usage=days.get(day.date(),empty)
        if opening and usage.platform_count<usage.platform_cap and usage.account_count<usage.account_cap:
            midnight=int(day.timestamp())
            first=max(stamp,midnight+limits.start*60)
            while pos<len(blocked) and blocked[pos][0]<=first:
                first=max(first,blocked[pos][1])
                pos+=1
            if first<midnight+limits.end*60:
                return first
            # A previous day's gap may extend beyond the next day's opening.
            stamp=max(stamp,first)
        day+=timedelta(days=1)
