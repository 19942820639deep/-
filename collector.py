#!/usr/bin/env python3
"""Build a timestamped A-share tail-session market snapshot from free sources.

The collector intentionally separates raw market-data acquisition from any
trading judgment.  Eastmoney public quote endpoints are the primary source;
Tencent quotes cross-check the liquid candidate set.  A snapshot is marked
valid only when freshness, coverage, board data and cross-source prices pass.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"

EM_CLIST = "https://82.push2.eastmoney.com/api/qt/clist/get"
EM_STOCK = "https://push2.eastmoney.com/api/qt/stock/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"

FULL_MARKET_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
INDUSTRY_FS = "m:90+t:2+f:!50"
CONCEPT_FS = "m:90+t:3+f:!50"

INDEX_SECIDS = {
    "上证指数": "1.000001",
    "深证成指": "0.399001",
    "创业板指": "0.399006",
    "科创50": "1.000688",
}

FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f22,f23,f24,f25,f62"
)
BOARD_FIELDS = "f2,f3,f4,f5,f6,f8,f12,f14,f20,f62,f104,f105,f106,f128,f136"


class CollectorError(RuntimeError):
    pass


@dataclass
class FetchResult:
    payload: Any
    fetched_at: datetime
    attempts: int


class HttpClient:
    def __init__(self) -> None:
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
        }

    def get_bytes(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        if params:
            url = f"{url}?{urlencode(params)}"
        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers)
        with urlopen(request, timeout=timeout) as response:
            return response.read()


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(SH_TZ).isoformat(timespec="seconds") if dt else None


def now_shanghai() -> datetime:
    return datetime.now(tz=SH_TZ)


def safe_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def safe_int(value: Any) -> int | None:
    number = safe_float(value)
    return int(number) if number is not None else None


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def make_session() -> HttpClient:
    return HttpClient()


def get_json(
    session: HttpClient,
    url: str,
    params: dict[str, Any],
    retries: int = 4,
    timeout: float = 15,
) -> FetchResult:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            raw = session.get_bytes(url, params=params, timeout=timeout)
            payload = json.loads(raw.decode("utf-8"))
            return FetchResult(payload, now_shanghai(), attempt)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 6))
    raise CollectorError(f"GET {url} failed after {retries} attempts: {last_error}")


def em_list(
    session: HttpClient,
    fs: str,
    fields: str,
    sort_field: str = "f3",
    page_size: int = 10000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = {
        "pn": 1,
        "pz": page_size,
        "po": 1,
        "np": 1,
        "ut": EM_UT,
        "fltt": 2,
        "invt": 2,
        "fid": sort_field,
        "fs": fs,
        "fields": fields,
    }
    first = get_json(session, EM_CLIST, base)
    data = (first.payload or {}).get("data") or {}
    rows = list(data.get("diff") or [])
    total = safe_int(data.get("total")) or len(rows)
    meta = {"fetched_at": iso(first.fetched_at), "attempts": first.attempts, "reported_total": total}

    if rows and len(rows) < total:
        actual_page = len(rows)
        pages = min(math.ceil(total / actual_page), 80)
        for page in range(2, pages + 1):
            params = dict(base)
            params["pn"] = page
            params["pz"] = actual_page
            result = get_json(session, EM_CLIST, params, retries=3)
            chunk = (((result.payload or {}).get("data") or {}).get("diff") or [])
            if not chunk:
                break
            rows.extend(chunk)
            if len(rows) >= total:
                break
            time.sleep(0.08)

    deduped: dict[tuple[Any, Any], dict[str, Any]] = {}
    for row in rows:
        deduped[(row.get("f13"), row.get("f12"))] = row
    return list(deduped.values()), meta


def normalize_stock(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(row.get("f12") or ""),
        "market": safe_int(row.get("f13")),
        "name": str(row.get("f14") or ""),
        "last": safe_float(row.get("f2")),
        "pct": safe_float(row.get("f3")),
        "change": safe_float(row.get("f4")),
        "volume_lot": safe_float(row.get("f5")),
        "amount": safe_float(row.get("f6")),
        "amplitude": safe_float(row.get("f7")),
        "turnover": safe_float(row.get("f8")),
        "volume_ratio": safe_float(row.get("f10")),
        "high": safe_float(row.get("f15")),
        "low": safe_float(row.get("f16")),
        "open": safe_float(row.get("f17")),
        "prev_close": safe_float(row.get("f18")),
        "total_mv": safe_float(row.get("f20")),
        "float_mv": safe_float(row.get("f21")),
        "speed": safe_float(row.get("f22")),
        "pe_ttm": safe_float(row.get("f23")),
        "pct_60d": safe_float(row.get("f24")),
        "pct_ytd": safe_float(row.get("f25")),
        "main_net": safe_float(row.get("f62")),
    }


def normalize_board(row: dict[str, Any]) -> dict[str, Any]:
    up = safe_int(row.get("f104"))
    down = safe_int(row.get("f105"))
    flat = safe_int(row.get("f106"))
    width_base = sum(x or 0 for x in (up, down, flat))
    return {
        "code": str(row.get("f12") or ""),
        "name": str(row.get("f14") or ""),
        "last": safe_float(row.get("f2")),
        "pct": safe_float(row.get("f3")),
        "amount": safe_float(row.get("f6")),
        "turnover": safe_float(row.get("f8")),
        "main_net": safe_float(row.get("f62")),
        "up": up,
        "down": down,
        "flat": flat,
        "breadth": (up / width_base) if up is not None and width_base else None,
        "leader_code": str(row.get("f128") or ""),
        "leader_pct": safe_float(row.get("f136")),
    }


def em_stock_quote(session: HttpClient, secid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    params = {
        "secid": secid,
        "ut": EM_UT,
        "fltt": 2,
        "invt": 2,
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f86,f169,f170",
    }
    result = get_json(session, EM_STOCK, params)
    data = (result.payload or {}).get("data") or {}
    epoch = safe_int(data.get("f86"))
    trade_time = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(SH_TZ) if epoch else None
    quote = {
        "code": str(data.get("f57") or ""),
        "name": str(data.get("f58") or ""),
        "last": safe_float(data.get("f43")),
        "high": safe_float(data.get("f44")),
        "low": safe_float(data.get("f45")),
        "open": safe_float(data.get("f46")),
        "volume_lot": safe_float(data.get("f47")),
        "amount": safe_float(data.get("f48")),
        "prev_close": safe_float(data.get("f60")),
        "change": safe_float(data.get("f169")),
        "pct": safe_float(data.get("f170")),
        "trade_time": iso(trade_time),
    }
    return quote, {"fetched_at": iso(result.fetched_at), "attempts": result.attempts}


def is_special(stock: dict[str, Any]) -> bool:
    name = stock.get("name", "").upper().strip()
    return "ST" in name or "退" in name or name.startswith(("N", "C"))


def price_limit_ratio(code: str) -> Decimal:
    if code.startswith(("300", "301", "688", "689")):
        return Decimal("0.20")
    if code.startswith(("4", "8", "92")):
        return Decimal("0.30")
    return Decimal("0.10")


def limit_price(prev_close: float, ratio: Decimal, up: bool) -> float:
    base = Decimal(str(prev_close))
    factor = Decimal("1") + ratio if up else Decimal("1") - ratio
    return float((base * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def market_summary(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [s for s in stocks if s.get("last") and s.get("prev_close") and s.get("pct") is not None]
    non_st = [s for s in valid if not is_special(s)]
    up = sum(1 for s in non_st if s["pct"] > 0)
    down = sum(1 for s in non_st if s["pct"] < 0)
    flat = len(non_st) - up - down
    limit_up = limit_down = touched_up = 0
    for stock in non_st:
        ratio = price_limit_ratio(stock["code"])
        upper = limit_price(stock["prev_close"], ratio, True)
        lower = limit_price(stock["prev_close"], ratio, False)
        last = stock["last"]
        high = stock.get("high") or last
        low = stock.get("low") or last
        epsilon = 0.0051
        if last >= upper - epsilon:
            limit_up += 1
        elif high >= upper - epsilon:
            touched_up += 1
        if last <= lower + epsilon or low <= lower + epsilon and last <= lower + epsilon:
            limit_down += 1
    amounts = [s.get("amount") or 0 for s in valid]
    return {
        "universe_count": len(stocks),
        "valid_count": len(valid),
        "non_st_count": len(non_st),
        "up": up,
        "down": down,
        "flat": flat,
        "advance_decline_ratio": round(up / down, 4) if down else None,
        "limit_up_non_st": limit_up,
        "limit_down_non_st": limit_down,
        "opened_limit_up_non_st": touched_up,
        "turnover_amount": round(sum(amounts), 2),
        "median_pct": round(statistics.median([s["pct"] for s in non_st]), 4) if non_st else None,
    }


def code_prefix(stock: dict[str, Any]) -> str:
    code = stock["code"]
    if code.startswith(("4", "8", "92")):
        return "bj" + code
    return ("sh" if stock.get("market") == 1 else "sz") + code


def fetch_tencent_quotes(
    session: HttpClient, stocks: list[dict[str, Any]], batch_size: int = 60
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    attempts = 0
    fetched_at: datetime | None = None
    for start in range(0, len(stocks), batch_size):
        batch = stocks[start : start + batch_size]
        query = ",".join(code_prefix(s) for s in batch)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            attempts += 1
            try:
                raw = session.get_bytes(
                    TENCENT_QUOTE + query,
                    timeout=15,
                    headers={"Referer": "https://gu.qq.com/"},
                )
                text = raw.decode("gbk", errors="ignore")
                fetched_at = now_shanghai()
                for line in text.splitlines():
                    match = re.search(r'v_(?:sh|sz|bj)(\d{6})="(.*)";', line)
                    if not match:
                        continue
                    code, body = match.groups()
                    parts = body.split("~")
                    if len(parts) < 6:
                        continue
                    timestamps = [p for p in parts if re.fullmatch(r"20\d{12}", p)]
                    trade_time = None
                    if timestamps:
                        try:
                            trade_time = datetime.strptime(timestamps[0], "%Y%m%d%H%M%S").replace(tzinfo=SH_TZ)
                        except ValueError:
                            trade_time = None
                    output[code] = {
                        "code": code,
                        "name": parts[1],
                        "last": safe_float(parts[3]),
                        "prev_close": safe_float(parts[4]),
                        "open": safe_float(parts[5]),
                        "trade_time": iso(trade_time),
                    }
                last_error = None
                break
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(attempt)
        if last_error:
            raise CollectorError(f"Tencent quote failed: {last_error}")
        time.sleep(0.08)
    return output, {"fetched_at": iso(fetched_at), "attempts": attempts, "matches": len(output)}


def ema(values: Iterable[float], span: int) -> list[float]:
    seq = list(values)
    if not seq:
        return []
    alpha = 2 / (span + 1)
    output = [seq[0]]
    for value in seq[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def fetch_ema60(session: HttpClient, stock: dict[str, Any]) -> dict[str, Any] | None:
    secid = f"{stock.get('market', 0)}.{stock['code']}"
    params = {
        "secid": secid,
        "ut": EM_UT,
        "klt": 60,
        "fqt": 1,
        "lmt": 90,
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    try:
        result = get_json(session, EM_KLINE, params, retries=2, timeout=12)
        lines = ((((result.payload or {}).get("data") or {}).get("klines")) or [])
        closes: list[float] = []
        times: list[str] = []
        for line in lines:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            close = safe_float(parts[2])
            if close is not None:
                times.append(parts[0])
                closes.append(close)
        if len(closes) < 50:
            return None
        ema12 = ema(closes, 12)
        ema50 = ema(closes, 50)
        return {
            "bar_time": times[-1],
            "close": closes[-1],
            "ema12": round(ema12[-1], 4),
            "ema50": round(ema50[-1], 4),
            "ema12_slope": round(ema12[-1] - ema12[-2], 4),
            "state": "bull" if closes[-1] > ema12[-1] > ema50[-1] else (
                "bear" if closes[-1] < ema12[-1] < ema50[-1] else "mixed"
            ),
        }
    except CollectorError:
        return None


def candidate_pool(
    stocks: list[dict[str, Any]], sector_members: dict[str, list[str]]
) -> list[dict[str, Any]]:
    liquid = [
        s for s in stocks
        if not is_special(s)
        and (s.get("amount") or 0) >= 300_000_000
        and (s.get("last") or 0) > 0
        and s.get("pct") is not None
    ]
    liquid_by_code = {stock["code"]: stock for stock in liquid}
    chosen: dict[str, dict[str, Any]] = {}
    for stock in sorted(liquid, key=lambda s: s.get("amount") or 0, reverse=True)[:60]:
        chosen[stock["code"]] = dict(stock)
    for stock in sorted(liquid, key=lambda s: s.get("pct") or -999, reverse=True)[:60]:
        chosen[stock["code"]] = dict(stock)
    for stock in sorted(liquid, key=lambda s: s.get("speed") or -999, reverse=True)[:40]:
        chosen[stock["code"]] = dict(stock)

    # Ensure the most liquid capacity names in the strongest industries are
    # present even when they are not among the day's largest gainers.
    for codes in sector_members.values():
        members = [liquid_by_code[code] for code in codes if code in liquid_by_code]
        for stock in sorted(members, key=lambda s: s.get("amount") or 0, reverse=True)[:8]:
            chosen[stock["code"]] = dict(stock)

    code_to_sectors: dict[str, list[str]] = {}
    for sector, codes in sector_members.items():
        for code in codes:
            code_to_sectors.setdefault(code, []).append(sector)
    for code, sectors in code_to_sectors.items():
        if code in chosen:
            chosen[code]["top_industries"] = sectors

    return sorted(
        chosen.values(),
        key=lambda s: ((s.get("amount") or 0), (s.get("pct") or -999)),
        reverse=True,
    )[:140]


def fetch_top_sector_members(
    session: HttpClient,
    boards: list[dict[str, Any]],
    stock_by_code: dict[str, dict[str, Any]],
    limit: int = 12,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    memberships: dict[str, list[str]] = {}
    errors: list[str] = []
    for board in boards[:limit]:
        code = board.get("code")
        if not code:
            continue
        try:
            rows, _ = em_list(session, f"b:{code}+f:!50", FIELDS, sort_field="f6", page_size=500)
            codes = [str(row.get("f12") or "") for row in rows]
            codes = [code for code in codes if code in stock_by_code]
            memberships[board["name"]] = codes
        except CollectorError as exc:
            errors.append(f"{board.get('name')}: {exc}")
        time.sleep(0.08)
    return memberships, {"board_count": len(memberships), "errors": errors}


def load_previous() -> dict[str, Any] | None:
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def compact_timeline_entry(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": snapshot.get("generated_at"),
        "market": snapshot.get("market"),
        "indices": snapshot.get("indices"),
        "top_industries": [
            {k: board.get(k) for k in ("code", "name", "pct", "amount", "breadth")}
            for board in snapshot.get("industries", [])[:12]
        ],
    }


def build_snapshot() -> dict[str, Any]:
    generated = now_shanghai()
    previous = load_previous()
    errors: list[str] = []
    warnings: list[str] = []
    session = make_session()

    raw_stocks, market_meta = em_list(session, FULL_MARKET_FS, FIELDS, page_size=10000)
    stocks = [normalize_stock(row) for row in raw_stocks]
    stocks = [stock for stock in stocks if stock["code"] and stock["name"]]
    stock_by_code = {stock["code"]: stock for stock in stocks}
    market = market_summary(stocks)

    indices: dict[str, dict[str, Any]] = {}
    index_meta: dict[str, Any] = {}
    trade_times: list[datetime] = []
    for name, secid in INDEX_SECIDS.items():
        try:
            quote, meta = em_stock_quote(session, secid)
            indices[name] = quote
            index_meta[name] = meta
            if quote.get("trade_time"):
                trade_times.append(datetime.fromisoformat(quote["trade_time"]))
        except CollectorError as exc:
            errors.append(f"index {name}: {exc}")

    raw_industries, industries_meta = em_list(session, INDUSTRY_FS, BOARD_FIELDS, page_size=500)
    raw_concepts, concepts_meta = em_list(session, CONCEPT_FS, BOARD_FIELDS, page_size=800)
    industries = sorted(
        [normalize_board(row) for row in raw_industries],
        key=lambda row: row.get("pct") if row.get("pct") is not None else -999,
        reverse=True,
    )
    concepts = sorted(
        [normalize_board(row) for row in raw_concepts],
        key=lambda row: row.get("pct") if row.get("pct") is not None else -999,
        reverse=True,
    )

    sector_members, sector_meta = fetch_top_sector_members(session, industries, stock_by_code)
    candidates = candidate_pool(stocks, sector_members)

    tencent_meta: dict[str, Any] = {"matches": 0}
    tencent_quotes: dict[str, dict[str, Any]] = {}
    try:
        tencent_quotes, tencent_meta = fetch_tencent_quotes(session, candidates[:100])
    except CollectorError as exc:
        errors.append(str(exc))

    price_diffs: list[float] = []
    match_count = 0
    for candidate in candidates:
        secondary = tencent_quotes.get(candidate["code"])
        if secondary and secondary.get("last") and candidate.get("last"):
            match_count += 1
            diff = abs(candidate["last"] - secondary["last"]) / candidate["last"]
            price_diffs.append(diff)
            candidate["secondary_last"] = secondary["last"]
            candidate["secondary_trade_time"] = secondary.get("trade_time")
            candidate["source_price_diff"] = round(diff, 6)

    for candidate in candidates[:24]:
        technical = fetch_ema60(session, candidate)
        if technical:
            candidate["ema60"] = technical
        time.sleep(0.05)

    latest_trade_time = max(trade_times) if trade_times else None
    trade_date = latest_trade_time.date().isoformat() if latest_trade_time else None
    is_current_trade_day = bool(latest_trade_time and latest_trade_time.date() == generated.date())
    freshness_seconds = (
        max(0, (generated - latest_trade_time).total_seconds()) if latest_trade_time else None
    )

    previous_count = None
    if previous:
        previous_count = safe_int((previous.get("market") or {}).get("universe_count"))
    coverage_floor = max(4500, math.floor((previous_count or 0) * 0.95))
    coverage_pass = market["universe_count"] >= coverage_floor
    board_pass = len(industries) >= 30 and sector_meta["board_count"] >= 6
    freshness_pass = bool(freshness_seconds is not None and freshness_seconds <= 180)
    crosscheck_p95 = percentile(price_diffs, 0.95)
    crosscheck_pass = match_count >= 10 and crosscheck_p95 is not None and crosscheck_p95 <= 0.003

    checks = {
        "current_trade_day": is_current_trade_day,
        "freshness_seconds": round(freshness_seconds, 1) if freshness_seconds is not None else None,
        "freshness_pass": freshness_pass,
        "coverage_floor": coverage_floor,
        "coverage_pass": coverage_pass,
        "industry_board_pass": board_pass,
        "cross_source_matches": match_count,
        "cross_source_price_diff_p95": round(crosscheck_p95, 6) if crosscheck_p95 is not None else None,
        "cross_source_pass": crosscheck_pass,
    }
    valid = all((is_current_trade_day, freshness_pass, coverage_pass, board_pass, crosscheck_pass))
    if not coverage_pass:
        warnings.append("Full-market coverage below threshold")
    if not board_pass:
        warnings.append("Industry board coverage below threshold")
    if not crosscheck_pass:
        warnings.append("Tencent cross-source validation failed")
    if not freshness_pass:
        warnings.append("Index server timestamp is stale")

    snapshot: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": iso(generated),
        "trade_date": trade_date,
        "is_trading_day": is_current_trade_day,
        "valid_for_tail_selection": valid,
        "validation": checks,
        "market": market,
        "indices": indices,
        "industries": industries[:40],
        "concepts": concepts[:50],
        "top_industry_members": sector_members,
        "candidates": candidates,
        "sources": {
            "eastmoney_market": market_meta,
            "eastmoney_indices": index_meta,
            "eastmoney_industries": industries_meta,
            "eastmoney_concepts": concepts_meta,
            "tencent_candidates": tencent_meta,
        },
        "warnings": warnings,
        "errors": errors + sector_meta["errors"],
        "timeline": [],
        "methodology": {
            "market_breadth": "recomputed from non-ST full-market quotes",
            "limit_statistics": "recomputed from previous close and board price limits; new listings excluded",
            "sector_signal": "board price/breadth plus top-sector constituent membership",
            "candidate_scope": "liquid leaders by amount, gain and speed; no trading recommendation is produced here",
            "technical": "EMA12/EMA50 on 60-minute bars for the most liquid candidates; auxiliary only",
        },
    }

    timeline: list[dict[str, Any]] = []
    if previous and previous.get("trade_date") == snapshot["trade_date"]:
        timeline = list(previous.get("timeline") or [])[-7:]
        timeline.append(compact_timeline_entry(previous))
    snapshot["timeline"] = timeline[-8:]
    return snapshot


def write_snapshot(snapshot: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = LATEST_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, LATEST_PATH)


def main() -> int:
    try:
        snapshot = build_snapshot()
        write_snapshot(snapshot)
        print(
            json.dumps(
                {
                    "generated_at": snapshot["generated_at"],
                    "trade_date": snapshot["trade_date"],
                    "valid": snapshot["valid_for_tail_selection"],
                    "universe": snapshot["market"]["universe_count"],
                    "checks": snapshot["validation"],
                    "errors": len(snapshot["errors"]),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:  # Keep workflow failure visible; never publish guessed data.
        print(f"collector failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
