#!/usr/bin/env python3
"""Build a free, timestamped A-share snapshot on GitHub Actions.

Tencent provides the full universe and detailed quotes. Sina independently
checks liquid candidates. When ``THS_API_KEY`` is configured, the official
HiThink Financial API adds independently classified sector breadth and special
market pools. A stale or incomplete snapshot is never marked valid.
"""
from __future__ import annotations

import json, math, os, re, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
LATEST_PATH = ROOT / "data" / "latest.json"
STATUS_PATH = ROOT / "data" / "status.json"
TX_RANK = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
TX_QUOTE = "https://qt.gtimg.cn/q="
SINA_QUOTE = "https://hq.sinajs.cn/list="
THS_BASE = "https://fuyao.aicubes.cn"
INDEX_SYMBOLS = {"上证指数":"sh000001", "深证成指":"sz399001", "创业板指":"sz399006", "科创50":"sh000688"}

class CollectorError(RuntimeError): pass

class HttpClient:
    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float = 25,
            referer: str = "https://gu.qq.com/", headers: dict[str,str] | None = None) -> bytes:
        if params: url = f"{url}?{urlencode(params)}"
        request_headers={"User-Agent":"Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36",
                         "Accept":"application/json,text/plain,*/*", "Referer":referer}
        request_headers.update(headers or {})
        req = Request(url, headers=request_headers)
        with urlopen(req, timeout=timeout) as response: return response.read()

def now_shanghai() -> datetime: return datetime.now(tz=SH_TZ)
def iso(value: datetime | None) -> str | None:
    return value.astimezone(SH_TZ).isoformat(timespec="seconds") if value else None
def safe_float(value: Any) -> float | None:
    if value in (None, "", "-", "--"): return None
    try: number = float(value)
    except (TypeError, ValueError): return None
    return number if math.isfinite(number) else None
def safe_int(value: Any) -> int | None:
    n = safe_float(value); return int(n) if n is not None else None
def percentile(values: list[float], q: float) -> float | None:
    if not values: return None
    ordered = sorted(values); pos = (len(ordered)-1)*q; lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] if lo == hi else ordered[lo]*(hi-pos)+ordered[hi]*(pos-lo)

def median_datetime(values: list[datetime]) -> datetime | None:
    if not values: return None
    ordered = sorted(values)
    return ordered[len(ordered)//2]

def minute_of_day(value: datetime) -> int:
    return value.hour*60+value.minute

def midday_close_profile(generated: datetime, primary_latest: datetime | None,
                          secondary_anchor: datetime | None, core: bool,
                          price_cross: bool) -> bool:
    if not all((primary_latest, secondary_anchor, core, price_cross)): return False
    assert primary_latest is not None and secondary_anchor is not None
    execution_minute=minute_of_day(generated); secondary_minute=minute_of_day(secondary_anchor)
    return bool(primary_latest.date()==generated.date()==secondary_anchor.date()
                and 11*60+29<=execution_minute<=13*60
                and 11*60+29<=secondary_minute<=11*60+35)

def get_json(client: HttpClient, url: str, params: dict[str, Any], retries: int = 3) -> Any:
    error = None
    for attempt in range(1, retries+1):
        try: return json.loads(client.get(url, params=params).decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            error = exc
            if attempt < retries: time.sleep(attempt)
    raise CollectorError(f"GET {url} failed: {error}")

def get_ths_json(client: HttpClient, path: str, params: dict[str,Any], api_key: str,
                 retries: int = 3) -> dict[str,Any]:
    """Call the official HiThink REST API without ever serializing the key."""
    error = None
    for attempt in range(1,retries+1):
        try:
            raw=client.get(THS_BASE+path,params=params,referer=THS_BASE+"/",
                           headers={"X-api-key":api_key})
            payload=json.loads(raw.decode("utf-8"))
            if safe_int(payload.get("code")) != 0:
                raise CollectorError(f"HiThink {path}: code={payload.get('code')} message={payload.get('message')}")
            data=payload.get("data")
            if not isinstance(data,dict): raise CollectorError(f"HiThink {path}: missing data object")
            return data
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,CollectorError) as exc:
            error=exc
            if attempt < retries: time.sleep(attempt)
    raise CollectorError(f"HiThink {path} failed: {error}")

def tx_to_thscode(symbol: str) -> str | None:
    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}",symbol): return None
    suffix={"sh":"SH","sz":"SZ","bj":"BJ"}[symbol[:2]]
    return f"{symbol[2:]}.{suffix}"

def thscode_to_tx(thscode: str) -> str | None:
    m=re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)",str(thscode).upper())
    if not m:return None
    return {"SH":"sh","SZ":"sz","BJ":"bj"}[m.group(2)]+m.group(1)

def ths_snapshot_batches(client: HttpClient, path: str, codes: list[str], api_key: str,
                         batch: int = 80) -> tuple[list[dict[str,Any]],list[int]]:
    rows=[]; timestamps=[]
    for start in range(0,len(codes),batch):
        data=get_ths_json(client,path,{"thscodes":",".join(codes[start:start+batch])},api_key)
        rows.extend(data.get("item") or [])
        stamp=safe_int(data.get("timestamp"))
        if stamp:timestamps.append(stamp)
    return rows,timestamps

def fetch_hithink_price_cross(client: HttpClient, api_key: str,
                              candidates: list[dict[str,Any]]) -> dict[str,Any]:
    selected=candidates[:120]
    codes=[x for x in (tx_to_thscode(s["symbol"]) for s in selected) if x]
    rows,timestamps=ths_snapshot_batches(client,"/api/a-share/prices/snapshot",codes,api_key)
    by_symbol={thscode_to_tx(str(row.get("thscode") or "")):row for row in rows}
    diffs=[]; matches=0
    for stock in selected:
        alt=by_symbol.get(stock["symbol"])
        primary=safe_float(stock.get("last")); secondary=safe_float((alt or {}).get("last_price"))
        if primary and secondary:
            diff=abs(primary-secondary)/primary; diffs.append(diff); matches+=1
            stock["hithink_last"]=secondary
            stock["hithink_price_diff"]=round(diff,6)
    p95=percentile(diffs,.95)
    return {"status":"success","requested":len(codes),"matches":matches,
            "price_diff_p95":round(p95,6) if p95 is not None else None,
            "pass":bool(matches>=30 and p95 is not None and p95<=.003),
            "data_timestamps_ms":timestamps,"endpoint":"HiThink A-share prices snapshot"}

def fetch_hithink_special_pools(client: HttpClient, api_key: str) -> dict[str,Any]:
    output={}; timestamps=[]
    for name,path in (("limit_up","limit-up-pool"),("limit_down","limit-down-pool"),
                      ("limit_break","limit-break-pool")):
        data=get_ths_json(client,f"/api/a-share/special-data/{path}",{"page":1,"size":200},api_key)
        page=data.get("pagination") or {}; rows=data.get("item") or []
        output[name]={"total":safe_int(page.get("total")) if page else len(rows),"item":rows}
        stamp=safe_int(data.get("timestamp"))
        if stamp:timestamps.append(stamp)
    output.update({"status":"success","data_timestamps_ms":timestamps,
                   "endpoint":"HiThink limit-up/down/break pools"})
    return output

def fetch_hithink_sectors(client: HttpClient, api_key: str,
                          stocks: list[dict[str,Any]], top_n: int = 6) -> dict[str,Any]:
    """Rank THS industry/concept indices, then verify leaders with constituent breadth."""
    catalog=[]; timestamps=[]
    for tag in ("industry","cn_concept"):
        data=get_ths_json(client,"/api/a-share-index/catalog/ths-index-list",{"tag":tag},api_key)
        stamp=safe_int(data.get("timestamp"))
        if stamp:timestamps.append(stamp)
        for row in data.get("item") or []:
            if row.get("thscode"):catalog.append({**row,"tag":tag})
    codes=[str(row["thscode"]) for row in catalog]
    prices,price_timestamps=ths_snapshot_batches(client,"/api/a-share-index/prices/snapshot",codes,api_key)
    timestamps.extend(price_timestamps)
    meta={str(row["thscode"]):row for row in catalog}
    ranked=[]
    for row in prices:
        pct=safe_float(row.get("price_change_ratio_pct")); code=str(row.get("thscode") or "")
        if pct is None or code not in meta:continue
        ranked.append({**meta[code],"pct":pct,"last":safe_float(row.get("last_price")),
                       "amount":safe_float(row.get("turnover"))})
    # Keep both taxonomies represented; duplicates by name are de-duplicated.
    selected=[]; seen=set()
    for tag in ("industry","cn_concept"):
        for row in sorted((x for x in ranked if x["tag"]==tag),key=lambda x:x["pct"],reverse=True)[:top_n]:
            if row["name"] not in seen:selected.append(row);seen.add(row["name"])
    stock_map={s["symbol"]:s for s in stocks}; verified=[]
    for sector in selected:
        data=get_ths_json(client,"/api/a-share-index/constituents/ths-stock-list",
                          {"thscode":sector["thscode"]},api_key)
        stamp=safe_int(data.get("timestamp"))
        if stamp:timestamps.append(stamp)
        members=[]
        for member in data.get("item") or []:
            symbol=thscode_to_tx(str(member.get("thscode") or "")); quote=stock_map.get(symbol or "")
            if quote and quote.get("pct") is not None:
                members.append({"symbol":symbol,"code":quote["code"],"name":member.get("name") or quote.get("name"),
                                "pct":quote["pct"],"amount":quote.get("amount"),"last":quote.get("last")})
        if len(members)<3:continue
        pcts=[float(x["pct"]) for x in members]; up=sum(x>0 for x in pcts); down=sum(x<0 for x in pcts)
        leaders=sorted(members,key=lambda x:(x.get("amount") or 0),reverse=True)[:5]
        verified.append({**sector,"constituent_count":len(data.get("item") or []),"priced_count":len(members),
                         "up":up,"down":down,"flat":len(members)-up-down,"breadth_up_ratio":round(up/len(members),4),
                         "median_constituent_pct":round(statistics.median(pcts),4),
                         "average_constituent_pct":round(statistics.fmean(pcts),4),
                         "constituent_turnover":round(sum(x.get("amount") or 0 for x in members),2),
                         "capacity_leaders":leaders})
    verified.sort(key=lambda x:(x["pct"],x["breadth_up_ratio"]),reverse=True)
    return {"status":"verified" if len(verified)>=6 else "insufficient","verified_count":len(verified),
            "pass":len(verified)>=6,"top_sectors":verified,"data_timestamps_ms":timestamps,
            "rule":"Sector conclusions require index strength, constituent breadth and at least three priced constituents.",
            "endpoint":"HiThink THS index catalog/snapshot/constituents"}

def fetch_tencent_rank(client: HttpClient, count: int = 200, workers: int = 6) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    base = {"_appver":"11.17.0", "board_code":"aStock", "sort_type":"price", "direct":"down", "count":count}
    first = get_json(client, TX_RANK, {**base,"offset":0}); data = (first or {}).get("data") or {}
    total = safe_int(data.get("total")) or 0; rows = list(data.get("rank_list") or [])
    if not rows: raise CollectorError("Tencent rank returned no rows")
    offsets = list(range(count, total, count))
    def fetch_page(offset: int) -> list[dict[str,Any]]:
        payload = get_json(client, TX_RANK, {**base,"offset":offset})
        return list((((payload or {}).get("data") or {}).get("rank_list") or []))
    # A small pool removes the two-minute serial bottleneck while staying well
    # below the burst level that commonly triggers public-endpoint throttling.
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(offsets)))) as pool:
        future_to_offset = {pool.submit(fetch_page, offset): offset for offset in offsets}
        pages = {}
        for future in as_completed(future_to_offset):
            pages[future_to_offset[future]] = future.result()
    for offset in offsets:
        rows.extend(pages.get(offset) or [])
    rows = list({str(x.get("code")):x for x in rows if x.get("code")}.values())
    return rows, {"fetched_at":iso(now_shanghai()),"reported_total":total,"rows":len(rows),"endpoint":"Tencent getBoardRankList"}

def normalize_rank(row: dict[str,Any]) -> dict[str,Any]:
    symbol = str(row.get("code") or "")
    return {"symbol":symbol,"code":symbol[-6:],"name":str(row.get("name") or ""),
            "last":safe_float(row.get("zxj")),"change":safe_float(row.get("zd")),"pct":safe_float(row.get("zdf")),
            "amplitude":safe_float(row.get("zf")),"turnover_rate":safe_float(row.get("hsl")),
            "volume_ratio":safe_float(row.get("lb")),"amount":(safe_float(row.get("turnover")) or 0)*10000,
            "volume":safe_float(row.get("volume")),"speed":safe_float(row.get("speed")),"pe_ttm":safe_float(row.get("pe_ttm")),
            "float_mv":(safe_float(row.get("ltsz")) or 0)*1e8,"total_mv":(safe_float(row.get("zsz")) or 0)*1e8,
            "main_net_hint":safe_float(row.get("zljlr")),"pct_5d":safe_float(row.get("zdf_d5")),
            "pct_10d":safe_float(row.get("zdf_d10")),"pct_20d":safe_float(row.get("zdf_d20")),
            "pct_60d":safe_float(row.get("zdf_d60"))}

def parse_tx_line(line: str) -> dict[str,Any] | None:
    m = re.search(r'v_((?:sh|sz|bj)\d+)="(.*)";', line)
    if not m: return None
    symbol, body = m.groups(); p = body.split("~")
    if len(p) < 38: return None
    stamp = None
    if re.fullmatch(r"20\d{12}", p[30] or ""):
        try: stamp = datetime.strptime(p[30],"%Y%m%d%H%M%S").replace(tzinfo=SH_TZ)
        except ValueError: pass
    amount = None; triple = p[35].split("/")
    if len(triple) >= 3: amount = safe_float(triple[2])
    return {"symbol":symbol,"code":p[2],"name":p[1],"last":safe_float(p[3]),"prev_close":safe_float(p[4]),
            "open":safe_float(p[5]),"volume":safe_float(p[6]),"trade_time":iso(stamp),"change":safe_float(p[31]),
            "pct":safe_float(p[32]),"high":safe_float(p[33]),"low":safe_float(p[34]),"amount":amount}

def fetch_tencent_detail(client: HttpClient, symbols: list[str], batch: int = 80,
                         workers: int = 6) -> tuple[dict[str,dict[str,Any]],dict[str,Any]]:
    output = {}; batches = [symbols[start:start+batch] for start in range(0,len(symbols),batch)]
    def fetch_batch(batch_symbols: list[str]) -> list[dict[str,Any]]:
        query = ",".join(batch_symbols); error = None
        for attempt in range(1,4):
            try:
                payload = client.get(TX_QUOTE+query).decode("gbk",errors="ignore")
                parsed = []
                for line in payload.splitlines():
                    q = parse_tx_line(line)
                    if q: parsed.append(q)
                return parsed
            except (HTTPError,URLError,TimeoutError,OSError) as exc:
                error = exc
                if attempt < 3: time.sleep(attempt)
        raise CollectorError(f"Tencent detail failed: {error}")
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(batches)))) as pool:
        futures = [pool.submit(fetch_batch, item) for item in batches]
        for future in as_completed(futures):
            for q in future.result(): output[q["symbol"]] = q
    return output,{"fetched_at":iso(now_shanghai()),"requested":len(symbols),"matches":len(output),"calls":len(batches),"endpoint":"Tencent qt.gtimg.cn"}

def parse_sina_line(line: str) -> dict[str,Any] | None:
    m = re.search(r'var hq_str_((?:sh|sz|bj)\d+)="(.*)";',line)
    if not m: return None
    symbol,body = m.groups(); p = body.split(",")
    if len(p) < 32 or not p[0]: return None
    stamp = None
    try: stamp = datetime.strptime(f"{p[30]} {p[31]}","%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ)
    except (ValueError,IndexError): pass
    return {"symbol":symbol,"name":p[0],"open":safe_float(p[1]),"prev_close":safe_float(p[2]),"last":safe_float(p[3]),
            "high":safe_float(p[4]),"low":safe_float(p[5]),"volume":safe_float(p[8]),"amount":safe_float(p[9]),"trade_time":iso(stamp)}

def fetch_sina_quotes(client: HttpClient, symbols: list[str], batch: int = 80) -> tuple[dict[str,dict[str,Any]],dict[str,Any]]:
    output = {}; calls = 0
    for start in range(0,len(symbols),batch):
        text = client.get(SINA_QUOTE+",".join(symbols[start:start+batch]),referer="https://finance.sina.com.cn/").decode("gbk",errors="ignore")
        calls += 1
        for line in text.splitlines():
            q = parse_sina_line(line)
            if q: output[q["symbol"]] = q
        time.sleep(.08)
    return output,{"fetched_at":iso(now_shanghai()),"requested":len(symbols),"matches":len(output),"calls":calls,"endpoint":"Sina hq.sinajs.cn"}

def is_special(s: dict[str,Any]) -> bool:
    name = str(s.get("name") or "").upper().strip()
    return "ST" in name or "退" in name or name.startswith(("N","C"))
def price_limit_ratio(code: str) -> Decimal:
    if code.startswith(("300","301","688","689")): return Decimal(".20")
    if code.startswith(("4","8","92")): return Decimal(".30")
    return Decimal(".10")
def limit_price(prev: float, ratio: Decimal, up: bool) -> float:
    factor = Decimal("1")+ratio if up else Decimal("1")-ratio
    return float((Decimal(str(prev))*factor).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))

def market_summary(stocks: list[dict[str,Any]]) -> dict[str,Any]:
    valid = [s for s in stocks if s.get("last") and s.get("prev_close") and s.get("pct") is not None]
    normal = [s for s in valid if not is_special(s)]; up = sum(s["pct"]>0 for s in normal); down = sum(s["pct"]<0 for s in normal)
    limit_up=limit_down=opened=0
    for s in normal:
        ratio=price_limit_ratio(s["code"]); upper=limit_price(s["prev_close"],ratio,True); lower=limit_price(s["prev_close"],ratio,False)
        if s["last"] >= upper-.0051: limit_up += 1
        elif (s.get("high") or s["last"]) >= upper-.0051: opened += 1
        if s["last"] <= lower+.0051 and (s.get("low") or s["last"]) <= lower+.0051: limit_down += 1
    return {"universe_count":len(stocks),"valid_count":len(valid),"non_st_count":len(normal),"up":up,"down":down,
            "flat":len(normal)-up-down,"advance_decline_ratio":round(up/down,4) if down else None,"limit_up_non_st":limit_up,
            "limit_down_non_st":limit_down,"opened_limit_up_non_st":opened,"turnover_amount":round(sum(s.get("amount") or 0 for s in valid),2),
            "median_pct":round(statistics.median([s["pct"] for s in normal]),4) if normal else None}

def ema(values: Iterable[float], span: int) -> list[float]:
    seq=list(values)
    if not seq:return []
    alpha=2/(span+1); out=[seq[0]]
    for value in seq[1:]:out.append(alpha*value+(1-alpha)*out[-1])
    return out
def load_previous() -> dict[str,Any] | None:
    try:return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError,json.JSONDecodeError):return None
def choose_candidates(stocks: list[dict[str,Any]]) -> list[dict[str,Any]]:
    eligible=[s for s in stocks if not is_special(s) and (s.get("last") or 0)>0 and (s.get("amount") or 0)>=1e8 and s.get("pct") is not None]
    selected={}
    for key,count in (("amount",90),("pct",90),("speed",50)):
        for s in sorted(eligible,key=lambda x:x.get(key) if x.get(key) is not None else -999,reverse=True)[:count]:selected[s["symbol"]]=dict(s)
    return sorted(selected.values(),key=lambda x:x.get("amount") or 0,reverse=True)[:180]

def build_snapshot() -> dict[str,Any]:
    generated=now_shanghai(); previous=load_previous(); client=HttpClient(); warnings=[]; errors=[]
    ths_api_key=os.environ.get("THS_API_KEY","").strip()
    raw,rank_meta=fetch_tencent_rank(client); stocks=[normalize_rank(x) for x in raw]; stocks=[s for s in stocks if s["symbol"] and s["name"]]
    details,detail_meta=fetch_tencent_detail(client,[s["symbol"] for s in stocks]+list(INDEX_SYMBOLS.values()))
    trade_times=[]
    for s in stocks:
        q=details.get(s["symbol"])
        if q:
            s.update({k:v for k,v in q.items() if v is not None})
            if q.get("trade_time"):trade_times.append(datetime.fromisoformat(q["trade_time"]))
    indices={}
    for name,symbol in INDEX_SYMBOLS.items():
        q=details.get(symbol)
        if q:
            indices[name]=q
            if q.get("trade_time"):trade_times.append(datetime.fromisoformat(q["trade_time"]))
    market=market_summary(stocks); candidates=choose_candidates(stocks); secondary={}; sina_meta={"matches":0}
    refresh_meta={"requested":0,"matches":0,"calls":0,"endpoint":"Tencent qt.gtimg.cn candidate refresh"}
    refresh_symbols=list(INDEX_SYMBOLS.values())+[s["symbol"] for s in candidates[:120]]
    try:
        refreshed,refresh_meta=fetch_tencent_detail(client,refresh_symbols)
        refresh_meta["endpoint"]="Tencent qt.gtimg.cn candidate refresh"
        for s in candidates[:120]:
            q=refreshed.get(s["symbol"])
            if q:
                s.update({k:v for k,v in q.items() if v is not None})
                if q.get("trade_time"):trade_times.append(datetime.fromisoformat(q["trade_time"]))
        for name,symbol in INDEX_SYMBOLS.items():
            q=refreshed.get(symbol)
            if q:
                indices[name]=q
                if q.get("trade_time"):trade_times.append(datetime.fromisoformat(q["trade_time"]))
    except Exception as exc:errors.append(f"Tencent candidate refresh: {exc}")
    try: secondary,sina_meta=fetch_sina_quotes(client,list(INDEX_SYMBOLS.values())+[s["symbol"] for s in candidates[:120]])
    except Exception as exc:errors.append(f"Sina cross-check: {exc}")
    diffs=[]; time_diffs=[]; secondary_trade_times=[]; matches=0
    for s in candidates:
        alt=secondary.get(s["symbol"])
        if alt and alt.get("last") and s.get("last"):
            matches+=1; diff=abs(s["last"]-alt["last"])/s["last"]; diffs.append(diff)
            s.update({"secondary_last":alt["last"],"secondary_trade_time":alt.get("trade_time"),"source_price_diff":round(diff,6)})
            if alt.get("trade_time"):
                secondary_trade_times.append(datetime.fromisoformat(alt["trade_time"]))
            if alt.get("trade_time") and s.get("trade_time"):
                time_diffs.append(abs((datetime.fromisoformat(s["trade_time"])-datetime.fromisoformat(alt["trade_time"])).total_seconds()))
    hithink_price={"status":"not_configured","pass":False,"matches":0}
    hithink_sectors={"status":"not_configured","pass":False,"top_sectors":[]}
    hithink_special={"status":"not_configured"}
    if ths_api_key:
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={"price":pool.submit(fetch_hithink_price_cross,client,ths_api_key,candidates),
                  "sectors":pool.submit(fetch_hithink_sectors,client,ths_api_key,stocks),
                  "special":pool.submit(fetch_hithink_special_pools,client,ths_api_key)}
            for name,future in jobs.items():
                try:
                    result=future.result()
                    if name=="price":hithink_price=result
                    elif name=="sectors":hithink_sectors=result
                    else:hithink_special=result
                except Exception as exc:
                    errors.append(f"HiThink {name}: {exc}")
    secondary_close_anchor=median_datetime(secondary_trade_times)
    latest=max(trade_times) if trade_times else None; trade_date=latest.date().isoformat() if latest else None
    current=bool(latest and latest.date()==generated.date()); freshness=max(0,(generated-latest).total_seconds()) if latest else None
    previous_count=safe_int(((previous or {}).get("market") or {}).get("universe_count")) or 0; floor=max(5000,math.floor(previous_count*.95))
    ratio=market["valid_count"]/len(stocks) if stocks else 0; p95=percentile(diffs,.95); time_p95=percentile(time_diffs,.95)
    coverage=len(stocks)>=floor and ratio>=.95; index_pass=len(indices)==len(INDEX_SYMBOLS)
    price_cross=matches>=30 and p95 is not None and p95<=.003
    time_cross=time_p95 is not None and time_p95<=300
    cross=price_cross and time_cross
    fresh=freshness is not None and freshness<=240
    core=coverage and index_pass and price_cross
    live_analysis=bool(current and core and time_cross and freshness is not None and freshness<=300)
    midday_execution_window=bool(11*60+29<=minute_of_day(generated)<=13*60)
    midday_secondary_time_pass=bool(secondary_close_anchor
                                    and secondary_close_anchor.date()==generated.date()
                                    and 11*60+29<=minute_of_day(secondary_close_anchor)<=11*60+35)
    midday_analysis=midday_close_profile(generated,latest,secondary_close_anchor,core,price_cross)
    previous_close_analysis=bool(core and latest and (latest.hour>14 or (latest.hour==14 and latest.minute>=55)))
    checks={"current_trade_day":current,"latest_trade_time":iso(latest),"freshness_seconds":round(freshness,1) if freshness is not None else None,
            "freshness_limit_seconds":240,"freshness_pass":fresh,"coverage_floor":floor,"coverage_ratio":round(ratio,4),"coverage_pass":coverage,
            "index_pass":index_pass,"cross_source_matches":matches,"cross_source_price_diff_p95":round(p95,6) if p95 is not None else None,
            "cross_source_time_diff_p95_seconds":round(time_p95,1) if time_p95 is not None else None,"cross_source_time_pass":bool(time_p95 is not None and time_p95<=300),
            "cross_source_pass":cross,"midday_close_anchor_time":iso(secondary_close_anchor),
            "midday_execution_window_pass":midday_execution_window,
            "midday_secondary_time_pass":midday_secondary_time_pass,
            "midday_price_cross_pass":price_cross,"midday_close_pass":midday_analysis,
            "hithink_configured":bool(ths_api_key),"hithink_price_cross_matches":safe_int(hithink_price.get("matches")) or 0,
            "hithink_price_diff_p95":safe_float(hithink_price.get("price_diff_p95")),
            "hithink_price_cross_pass":bool(hithink_price.get("pass")),
            "hithink_sector_count":safe_int(hithink_sectors.get("verified_count")) or 0,
            "sector_data_pass":bool(hithink_sectors.get("pass"))}
    valid=all((current,fresh,coverage,index_pass,cross))
    tail_valid=bool(valid and hithink_price.get("pass") and hithink_sectors.get("pass"))
    if not fresh:warnings.append("Quote timestamp is stale for tail-session use")
    if not coverage:warnings.append("Full-market detailed quote coverage below threshold")
    if not price_cross:warnings.append("Tencent/Sina price cross-check failed")
    elif not time_cross and midday_analysis:
        warnings.append("Live cross-source timestamps diverge during lunch; midday close accepted from the 11:30 secondary anchor and matching prices")
    elif not time_cross:
        warnings.append("Tencent/Sina timestamps are not synchronized for live intraday use")
    if not ths_api_key:
        warnings.append("THS_API_KEY is not configured; independently classified sector breadth is unavailable")
    elif not hithink_price.get("pass"):
        warnings.append("HiThink candidate price cross-check did not pass")
    if not hithink_sectors.get("pass"):
        warnings.append("HiThink sector breadth verification did not pass; do not assert a main line from this snapshot")
    return {"schema_version":"2.1.0","generated_at":iso(generated),"trade_date":trade_date,"is_trading_day":current,
            "valid_for_tail_selection":tail_valid,"validity_profiles":{"live_intraday":live_analysis,"midday_close":midday_analysis,
            "previous_close":previous_close_analysis,"core_market_data":core},"validation":checks,"market":market,"indices":indices,"candidates":candidates,
            "market_segments":{"main":[s for s in candidates if s["code"].startswith(("600","601","603","605","000","001","002","003"))][:30],
                               "chinext":[s for s in candidates if s["code"].startswith(("300","301"))][:30],
                               "star":[s for s in candidates if s["code"].startswith(("688","689"))][:30],
                               "beijing":[s for s in candidates if s["symbol"].startswith("bj")][:20]},
            "sector_data":hithink_sectors,"special_data":hithink_special,
            "sources":{"tencent_full_market":rank_meta,"tencent_detailed_quotes":detail_meta,
                       "tencent_candidate_refresh":refresh_meta,"sina_independent_check":sina_meta,
                       "hithink_price_check":hithink_price,
                       "hithink_sector_source":{"status":hithink_sectors.get("status"),"endpoint":hithink_sectors.get("endpoint")},
                       "hithink_special_source":{"status":hithink_special.get("status"),"endpoint":hithink_special.get("endpoint")}},
            "warnings":warnings,"errors":errors,"methodology":{"primary":"Tencent full-market rank plus detailed quotes","secondary":"Sina and HiThink candidate-price checks",
            "breadth_limits_turnover":"recomputed from full-market detailed quotes; HiThink special pools retained separately",
            "sector_limit":"HiThink sector index strength is verified against Tencent constituent breadth and capacity leaders",
            "cross_source_timing":"Live intraday use requires near-synchronous quotes. During the lunch break, the 11:30 Sina consensus timestamp anchors the close while Tencent may advance its display timestamp; prices must still match.",
            "midday_close":"Valid from 11:29 through 13:00 when the secondary consensus timestamp is 11:29-11:35 on the trade date and cross-source prices pass.",
            "technical":"EMA is auxiliary and omitted while no stable free 60-minute source is verified"}}

def write_json(path: Path, payload: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); os.replace(temp,path)

def write_snapshot(snapshot: dict[str,Any]) -> None:
    write_json(LATEST_PATH,snapshot)

def write_status(status: dict[str,Any]) -> None:
    write_json(STATUS_PATH,status)

def workflow_context() -> dict[str,Any]:
    repository=os.environ.get("GITHUB_REPOSITORY"); run_id=os.environ.get("GITHUB_RUN_ID")
    return {"repository":repository,"run_id":run_id,"run_attempt":os.environ.get("GITHUB_RUN_ATTEMPT"),
            "event_name":os.environ.get("GITHUB_EVENT_NAME"),
            "workflow_url":f"https://github.com/{repository}/actions/runs/{run_id}" if repository and run_id else None}

def main() -> int:
    started=now_shanghai(); context=workflow_context()
    try:
        s=build_snapshot(); completed=now_shanghai()
        s["pipeline_status"]={"run_status":"success","completed_at":iso(completed),**context}
        write_snapshot(s)
        write_status({"schema_version":"1.0.0","run_status":"success","started_at":iso(started),
                      "completed_at":iso(completed),"snapshot_updated":True,"snapshot_generated_at":s["generated_at"],
                      "trade_date":s["trade_date"],"valid_for_tail_selection":s["valid_for_tail_selection"],
                      "validity_profiles":s["validity_profiles"],"validation":s["validation"],
                      "errors":s["errors"],**context})
        print(json.dumps({"generated_at":s["generated_at"],"trade_date":s["trade_date"],"valid":s["valid_for_tail_selection"],
            "universe":s["market"]["universe_count"],"checks":s["validation"],"errors":s["errors"]},ensure_ascii=False));return 0
    except Exception as exc:
        completed=now_shanghai(); previous=load_previous(); message=f"{type(exc).__name__}: {exc}"
        write_status({"schema_version":"1.0.0","run_status":"collector_failed","started_at":iso(started),
                      "completed_at":iso(completed),"snapshot_updated":False,
                      "previous_snapshot_generated_at":(previous or {}).get("generated_at"),
                      "previous_snapshot_trade_date":(previous or {}).get("trade_date"),
                      "error":message,**context})
        print(f"collector failed: {message}",file=sys.stderr);return 1
if __name__=="__main__":raise SystemExit(main())
