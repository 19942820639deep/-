#!/usr/bin/env python3
"""Build a free, timestamped A-share snapshot on GitHub Actions.

Tencent provides the full universe and detailed quotes. Sina independently
checks liquid candidates. A stale or incomplete snapshot is never marked valid.
"""
from __future__ import annotations

import json, math, os, re, statistics, sys, time
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
INDEX_SYMBOLS = {"上证指数":"sh000001", "深证成指":"sz399001", "创业板指":"sz399006", "科创50":"sh000688"}

class CollectorError(RuntimeError): pass

class HttpClient:
    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float = 25,
            referer: str = "https://gu.qq.com/") -> bytes:
        if params: url = f"{url}?{urlencode(params)}"
        req = Request(url, headers={"User-Agent":"Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36",
                                    "Accept":"application/json,text/plain,*/*", "Referer":referer})
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

def get_json(client: HttpClient, url: str, params: dict[str, Any], retries: int = 3) -> Any:
    error = None
    for attempt in range(1, retries+1):
        try: return json.loads(client.get(url, params=params).decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            error = exc
            if attempt < retries: time.sleep(attempt)
    raise CollectorError(f"GET {url} failed: {error}")

def fetch_tencent_rank(client: HttpClient, count: int = 200) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    base = {"_appver":"11.17.0", "board_code":"aStock", "sort_type":"price", "direct":"down", "count":count}
    first = get_json(client, TX_RANK, {**base,"offset":0}); data = (first or {}).get("data") or {}
    total = safe_int(data.get("total")) or 0; rows = list(data.get("rank_list") or [])
    if not rows: raise CollectorError("Tencent rank returned no rows")
    for offset in range(count, total, count):
        payload = get_json(client, TX_RANK, {**base,"offset":offset})
        chunk = (((payload or {}).get("data") or {}).get("rank_list") or [])
        if not chunk: break
        rows.extend(chunk); time.sleep(.04)
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

def fetch_tencent_detail(client: HttpClient, symbols: list[str], batch: int = 80) -> tuple[dict[str,dict[str,Any]],dict[str,Any]]:
    output = {}; calls = 0
    for start in range(0,len(symbols),batch):
        query = ",".join(symbols[start:start+batch]); error = None
        for attempt in range(1,4):
            try:
                text = client.get(TX_QUOTE+query).decode("gbk",errors="ignore"); calls += 1
                for line in text.splitlines():
                    q = parse_tx_line(line)
                    if q: output[q["symbol"]] = q
                error = None; break
            except (HTTPError,URLError,TimeoutError,OSError) as exc:
                error = exc
                if attempt < 3: time.sleep(attempt)
        if error: raise CollectorError(f"Tencent detail failed: {error}")
        time.sleep(.04)
    return output,{"fetched_at":iso(now_shanghai()),"requested":len(symbols),"matches":len(output),"calls":calls,"endpoint":"Tencent qt.gtimg.cn"}

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
    try: secondary,sina_meta=fetch_sina_quotes(client,list(INDEX_SYMBOLS.values())+[s["symbol"] for s in candidates[:120]])
    except Exception as exc:errors.append(f"Sina cross-check: {exc}")
    diffs=[]; time_diffs=[]; matches=0
    for s in candidates:
        alt=secondary.get(s["symbol"])
        if alt and alt.get("last") and s.get("last"):
            matches+=1; diff=abs(s["last"]-alt["last"])/s["last"]; diffs.append(diff)
            s.update({"secondary_last":alt["last"],"secondary_trade_time":alt.get("trade_time"),"source_price_diff":round(diff,6)})
            if alt.get("trade_time") and s.get("trade_time"):
                time_diffs.append(abs((datetime.fromisoformat(s["trade_time"])-datetime.fromisoformat(alt["trade_time"])).total_seconds()))
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
    midday_analysis=bool(current and core and latest and latest.hour==11 and 29<=latest.minute<=35 and generated.hour==11)
    previous_close_analysis=bool(core and latest and (latest.hour>14 or (latest.hour==14 and latest.minute>=55)))
    checks={"current_trade_day":current,"latest_trade_time":iso(latest),"freshness_seconds":round(freshness,1) if freshness is not None else None,
            "freshness_limit_seconds":240,"freshness_pass":fresh,"coverage_floor":floor,"coverage_ratio":round(ratio,4),"coverage_pass":coverage,
            "index_pass":index_pass,"cross_source_matches":matches,"cross_source_price_diff_p95":round(p95,6) if p95 is not None else None,
            "cross_source_time_diff_p95_seconds":round(time_p95,1) if time_p95 is not None else None,"cross_source_time_pass":bool(time_p95 is not None and time_p95<=300),
            "cross_source_pass":cross,"sector_data_pass":False}
    valid=all((current,fresh,coverage,index_pass,cross))
    if not fresh:warnings.append("Quote timestamp is stale for tail-session use")
    if not coverage:warnings.append("Full-market detailed quote coverage below threshold")
    if not cross:warnings.append("Tencent/Sina price cross-check failed")
    warnings.append("No independently verified live sector taxonomy; confirm a separate sector table and multiple constituents")
    return {"schema_version":"2.0.0","generated_at":iso(generated),"trade_date":trade_date,"is_trading_day":current,
            "valid_for_tail_selection":valid,"validity_profiles":{"live_intraday":live_analysis,"midday_close":midday_analysis,
            "previous_close":previous_close_analysis,"core_market_data":core},"validation":checks,"market":market,"indices":indices,"candidates":candidates,
            "market_segments":{"main":[s for s in candidates if s["code"].startswith(("600","601","603","605","000","001","002","003"))][:30],
                               "chinext":[s for s in candidates if s["code"].startswith(("300","301"))][:30],
                               "star":[s for s in candidates if s["code"].startswith(("688","689"))][:30],
                               "beijing":[s for s in candidates if s["symbol"].startswith("bj")][:20]},
            "sector_data":{"status":"unavailable","rule":"Do not infer a main line from this snapshot alone; verify a live sector table and multiple constituents."},
            "sources":{"tencent_full_market":rank_meta,"tencent_detailed_quotes":detail_meta,"sina_independent_check":sina_meta},
            "warnings":warnings,"errors":errors,"methodology":{"primary":"Tencent full-market rank plus detailed quotes","secondary":"Sina detailed quotes for liquid candidates",
            "breadth_limits_turnover":"recomputed from full-market detailed quotes","sector_limit":"sector taxonomy not asserted without an independent source",
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
