"""LOF 场内基金数据访问层。

数据源分工 (2026-11-18 实测 + 官方文档核对):
  - 维表   : fuyao /api/meta/tickers/list?asset_type=fund-lof (需 Key)
             无 Key 时回退东方财富基金总表按 LOF 代码段过滤
  - 日K    : 东方财富 push2his (TickFlow 免费档 fund=0/lof=400;
             fuyao 场内历史日线文档明确"仅支持 ETF", 故 LOF 只能用东财)
  - 实时   : fuyao /api/fund/market/snapshot (文档明确支持 ETF+LOF, 批量≤100;
             quote_service 场内基金补拉 ETF+LOF 统一走本通道)
             无 Key 时回退东财 push2 /api/qt/stock/get (ETF 缩放 /100, LOF /1000)
  - 净值   : fuyao /api/fund/performance/nav (unit_nav / adj_nav)

单位口径 (不可凭字段名推断):
  - 东财日K volume 已是「手」(实测 amount÷volume÷100≈当日均价);
    fuyao volume 是「股」→ 手需 /100。两者在本模块出口统一为手。
  - 东财 klines 列序: 日期,开,收,高,低,成交量,成交额,振幅 (注意收在高之前!)
  - 价格均为原始价 (fqt=0 / adjust=none); 复权交给 indicators.pipeline。
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime
from typing import Any

import httpx
import polars as pl

logger = logging.getLogger(__name__)

EASTMONEY_PUSH2 = "https://push2.eastmoney.com"
EASTMONEY_PUSH2HIS = "https://push2his.eastmoney.com"
EASTMONEY_FUND_LIST = "https://fund.eastmoney.com/js/fundcode_search.js"
FUYAO_BASE = "https://fuyao.aicubes.cn"

_UA = {"User-Agent": "Mozilla/5.0"}
# LOF 代码段: 深 16xxxx(160-169), 沪 501/502/505/506; 排除 508(REITs)
_LOF_PREFIXES = tuple(f"16{i}" for i in range(10)) + ("501", "502", "505", "506")
_DAILY_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
_HISTORY_RETRY = 2
_HISTORY_SLEEP = 0.12  # 东财 push2his 节流, 避免 429


def _price_scale(symbol: str) -> float:
    """东财 push2 价格字段放大倍数: LOF 3 位小数 → 1000; ETF 等 2 位 → 100。"""
    code = symbol.partition(".")[0]
    return 1000.0 if code.startswith(_LOF_PREFIXES) else 100.0


def _f(value: Any) -> float | None:
    """安全转 float; 空串/None/- 一律 None, 不伪造 0。"""
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _secid(symbol: str) -> str:
    """160216.SZ → 0.160216; 501060.SH → 1.501060。"""
    code, _, suffix = symbol.partition(".")
    return f"{'1' if suffix.upper() == 'SH' else '0'}.{code}"


def _symbol(code: str) -> str:
    """6 位代码 → 带市场后缀的 thscode。"""
    code = str(code).strip()
    return f"{code}.{'SH' if code.startswith(('5', '6', '9')) else 'SZ'}"


def _fuyo_key() -> str:
    """复用 fuyao 插件的 Key 配置(secrets.json 优先, 次 .env)。未配置返回空。"""
    from app.plugins.fuyao.provider import get_api_key

    return get_api_key()


# ---------------------------------------------------------------- 维表
def fetch_instruments() -> pl.DataFrame:
    """LOF 标的维表 [symbol, name, code, asset_type]。fuyao 优先, 东财兜底。"""
    df = _instruments_fuyao()
    if not df.is_empty():
        return df
    logger.warning("fuyao LOF 维表不可用, 回退东财基金总表代码段过滤")
    return _instruments_eastmoney()


def _instruments_fuyao() -> pl.DataFrame:
    key = _fuyo_key()
    if not key:
        return pl.DataFrame()
    rows: list[dict] = []
    offset = 0
    limit = 1000
    try:
        with httpx.Client(base_url=FUYAO_BASE, headers={"X-api-key": key}, timeout=20.0) as client:
            while True:
                resp = client.get(
                    "/api/meta/tickers/list",
                    params={"asset_type": "fund-lof", "limit": limit, "offset": offset},
                )
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("code") != 0:
                    logger.warning("fuyao tickers/list code=%s: %s", payload.get("code"), payload.get("message"))
                    break
                items = (payload.get("data") or {}).get("item") or []
                rows.extend(items)
                if len(items) < limit:
                    break
                offset += limit
                if offset > 10000:
                    break
    except Exception as e:  # noqa: BLE001
        logger.warning("fuyao LOF 维表拉取失败: %s", e)
        return pl.DataFrame()
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame([
            {
                "symbol": str(r.get("thscode") or _symbol(r.get("ticker", ""))),
                "name": r.get("name") or str(r.get("ticker", "")),
                "code": str(r.get("ticker") or ""),
                "asset_type": "lof",
            }
            for r in rows
            if r.get("thscode") or r.get("ticker")
        ])
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def _instruments_eastmoney() -> pl.DataFrame:
    """东财基金总表 (JS) 按 LOF 代码段过滤 —— 无 Key 时的兜底。"""
    try:
        resp = httpx.get(EASTMONEY_FUND_LIST, headers=_UA, timeout=30.0)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
        import json

        arr = json.loads(text[text.index("[") : text.rindex("]") + 1])
    except Exception as e:  # noqa: BLE001
        logger.warning("东财基金总表拉取失败: %s", e)
        return pl.DataFrame()
    rows = []
    for item in arr:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        code = str(item[0]).strip()
        if not code.startswith(_LOF_PREFIXES):
            continue
        rows.append({"symbol": _symbol(code), "name": str(item[2]), "code": code, "asset_type": "lof"})
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).unique(subset=["symbol"], keep="last").sort("symbol")


# ---------------------------------------------------------------- 日K
def fetch_daily(
    symbols: list[str],
    start: date | None = None,
    end: date | None = None,
    on_chunk_done=None,
) -> pl.DataFrame:
    """东财 push2his 逐标的拉日K → canonical 8 列 (volume 手, 原始价)。"""
    if not symbols:
        return pl.DataFrame()
    end = end or date.today()
    start = start or date(end.year - 1, end.month, end.day)
    beg = start.strftime("%Y%m%d")
    fin = end.strftime("%Y%m%d")
    frames: list[pl.DataFrame] = []
    total = len(symbols)
    with httpx.Client(base_url=EASTMONEY_PUSH2HIS, headers=_UA, timeout=25.0) as client:
        for i, symbol in enumerate(symbols):
            for attempt in range(_HISTORY_RETRY + 1):
                try:
                    resp = client.get(
                        "/api/qt/stock/kline/get",
                        params={
                            "secid": _secid(symbol),
                            "fields1": "f1,f2,f3",
                            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                            "klt": "101",  # 日K
                            "fqt": "0",  # 不复权
                            "beg": beg,
                            "end": fin,
                        },
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    data = payload.get("data") or {}
                    klines = data.get("klines") or []
                    rows = []
                    for line in klines:
                        cols = line.split(",")
                        if len(cols) < 7:
                            continue
                        # 东财列序: 日期,开,收,高,低,量(手),额(元),振幅
                        o, c, h, l = _f(cols[1]), _f(cols[2]), _f(cols[3]), _f(cols[4])
                        vol, amt = _f(cols[5]), _f(cols[6])
                        if None in (o, c, h, l, vol, amt):
                            continue
                        rows.append({
                            "symbol": symbol,
                            "date": datetime.strptime(cols[0], "%Y-%m-%d").date(),
                            "open": o, "high": h, "low": l, "close": c,
                            "volume": math.floor(vol), "amount": amt,
                        })
                    if rows:
                        frames.append(pl.DataFrame(rows, schema_overrides={"date": pl.Date}))
                    break  # 成功(含 legit 空)即退出重试
                except Exception as e:  # noqa: BLE001
                    if attempt == _HISTORY_RETRY:
                        logger.warning("东财 LOF 日K失败 %s: %s", symbol, e)
                    else:
                        time.sleep(0.3 * (attempt + 1))
            time.sleep(_HISTORY_SLEEP)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
    if not frames:
        return pl.DataFrame(schema={c: pl.Utf8 for c in _DAILY_COLUMNS})
    return pl.concat(frames, how="diagonal_relaxed").select(_DAILY_COLUMNS).sort(["symbol", "date"])


# ---------------------------------------------------------------- 实时
def fetch_realtime(symbols: list[str]) -> list[dict]:
    """场内基金 (LOF/ETF) 实时快照 → 内部 realtime record。fuyao 优先, 东财兜底。"""
    if not symbols:
        return []
    records = _realtime_fuyao(symbols)
    if records:
        return records
    return _realtime_eastmoney(symbols)


def _realtime_fuyao(symbols: list[str]) -> list[dict]:
    key = _fuyo_key()
    if not key:
        return []
    out: list[dict] = []
    try:
        with httpx.Client(base_url=FUYAO_BASE, headers={"X-api-key": key}, timeout=20.0) as client:
            for i in range(0, len(symbols), 100):
                chunk = symbols[i : i + 100]
                resp = client.get(
                    "/api/fund/market/snapshot", params={"thscode": ",".join(chunk)}
                )
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("code") != 0:
                    logger.warning("fuyao LOF 快照 code=%s: %s", payload.get("code"), payload.get("message"))
                    continue
                ts = (payload.get("data") or {}).get("timestamp") or int(time.time() * 1000)
                for row in (payload.get("data") or {}).get("item") or []:
                    rec = _map_fund_snapshot(row, int(ts))
                    if rec:
                        out.append(rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("fuyao LOF 快照失败: %s", e)
        return []
    return out


def _map_fund_snapshot(row: dict, fetched_ms: int) -> dict | None:
    symbol = row.get("thscode")
    if not symbol:
        return None
    last = _f(row.get("last_price"))
    prev = _f(row.get("prev_price"))
    pct = _f(row.get("price_change_ratio_pct"))
    vol = _f(row.get("volume"))
    return {
        "symbol": symbol,
        "name": row.get("name"),
        "last_price": last,
        "prev_close": prev,
        "open": _f(row.get("open_price")),
        "high": _f(row.get("high_price")),
        "low": _f(row.get("low_price")),
        # 场内基金快照 volume 口径随源而异, 由 amount/price 自验后定;
        # fuyao A 股快照为股, 基金快照实测前先沿用 /100 同口径, 空值不伪造。
        "volume": math.floor(vol / 100.0) if vol is not None else None,
        "amount": _f(row.get("turnover")),
        "change_pct": pct / 100.0 if pct is not None else None,
        "change_amount": _f(row.get("price_change")),
        "amplitude": None,
        "turnover_rate": None,
        "timestamp": fetched_ms,
        "session": None,
    }


def _realtime_eastmoney(symbols: list[str]) -> list[dict]:
    """东财 push2 单标的快照兜底 (每标的 1 次请求, 适合小批量订阅)。

    同时服务 LOF 与 ETF (fetch_realtime 已开放给场内基金通用补拉)。
    """
    out: list[dict] = []
    with httpx.Client(base_url=EASTMONEY_PUSH2, headers=_UA, timeout=15.0) as client:
        for symbol in symbols:
            try:
                resp = client.get(
                    "/api/qt/stock/get",
                    params={
                        "secid": _secid(symbol),
                        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
                    },
                )
                resp.raise_for_status()
                data = (resp.json() or {}).get("data") or {}
                if not data:
                    continue
                # f43 最新价等为放大整数: ETF (2 位小数) 除数 100;
                # LOF (3 位小数) 实测 160216: f43=566, last=0.566 → 除数 1000。
                scale = _price_scale(symbol)
                last = _f(data.get("f43"))
                prev = _f(data.get("f60"))
                if last is None:
                    continue
                out.append({
                    "symbol": symbol,
                    "name": data.get("f58"),
                    "last_price": last / scale,
                    "prev_close": prev / scale if prev is not None else None,
                    "open": (_f(data.get("f46")) or 0) / scale,
                    "high": (_f(data.get("f44")) or 0) / scale,
                    "low": (_f(data.get("f45")) or 0) / scale,
                    "volume": math.floor(_f(data.get("f47")) or 0),  # 手
                    "amount": _f(data.get("f48")),
                    "change_pct": (_f(data.get("f170")) or 0) / 1000.0,
                    "change_amount": (_f(data.get("f169")) or 0) / scale,
                    "amplitude": None,
                    "turnover_rate": None,
                    "timestamp": int(time.time() * 1000),
                    "session": None,
                })
            except Exception as e:  # noqa: BLE001
                logger.warning("东财 LOF 快照失败 %s: %s", symbol, e)
    return out


# ---------------------------------------------------------------- 净值
def fetch_nav(symbols: list[str], *, range_: str = "fyear") -> pl.DataFrame:
    """fuyao 单位/复权净值 → [symbol, nav_date, unit_nav, adj_nav]。需 Key。"""
    key = _fuyo_key()
    if not key:
        return pl.DataFrame()
    rows: list[dict] = []
    try:
        with httpx.Client(base_url=FUYAO_BASE, headers={"X-api-key": key}, timeout=20.0) as client:
            for symbol in symbols:
                resp = client.get(
                    "/api/fund/performance/nav",
                    params={"thscode": symbol, "range": range_, "nav_type": "unit,adj"},
                )
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("code") != 0:
                    logger.debug("fuyao NAV %s code=%s", symbol, payload.get("code"))
                    continue
                for item in (payload.get("data") or {}).get("item") or []:
                    ms = item.get("nav_date")
                    if not ms:
                        continue
                    rows.append({
                        "symbol": symbol,
                        "nav_date": datetime.fromtimestamp(int(ms) / 1000).date(),
                        "unit_nav": _f(item.get("unit_nav")),
                        "adj_nav": _f(item.get("adj_nav")),
                    })
                time.sleep(0.12)
    except Exception as e:  # noqa: BLE001
        logger.warning("fuyao LOF 净值拉取失败: %s", e)
        return pl.DataFrame()
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(rows, schema_overrides={"nav_date": pl.Date})
        .unique(subset=["symbol", "nav_date"], keep="last")
        .sort(["symbol", "nav_date"])
    )
