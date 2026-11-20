# freetdx — 通达信公网免费源 provider (零费用, 无 Key)。
#
# 数据集与实现口径 (2026-11-19 实测):
#   - minute       pytdx get_history_minute_time_data 逐日分时 → 1 分钟K
#                  (该接口对"当日"也生效, 盘中可拉实时分时; 单位: price 元/绝对价,
#                   vol 手 — 与腾讯快照金额交叉验证: 茅台 09:30 vol=994 手 ↔ 1.43 亿元)。
#                  OHLC 口径: 分时每分钟只有一个价点 → open=high=low=close=分钟价,
#                  即"分钟收盘价线", 无影线; amount 按固定口径推导 price×vol×100。
#   - full_minute  同一分时接口的当日窗口 (修复轮); 不实现 get_intraday_latest,
#                  minute_refresh 自动降级为仅修复轮 (节奏下限 60s)。
#   - depth5       腾讯免费 HTTP 快照 (qt.gtimg.cn, 60 只/批, 量=手)。
#
# 历史深度: 通达信分时可回溯约半年 (实测 2026-06 可拉), minute_history_days=120。
# 未声明 daily/adj_factor/realtime/financial → 自动回退 (fuyao / TickFlow free-api)。

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from app.plugins.freetdx.client import SERVER_POOL, TdxPool, to_tdx_code

logger = logging.getLogger(__name__)

_DATASETS = ("minute", "depth5", "full_minute")
_BEIJING = ZoneInfo("Asia/Shanghai")

# 分时 240 点 → 时间轴: 0..119 → 09:31..11:30, 120..239 → 13:01..15:00
# (通达信 1 分钟K 行业口径: 集合竞价并入 09:31 首根; 已与腾讯分时/实时价对齐验证)
_BARS_PER_DAY = 240

MINUTE_HISTORY_DAYS = 120  # 1 分钟历史深度(交易日), 实测约半年可用
_MINUTE_INTERVAL_REQUEST = 0.0  # 节流在 client 层按连接执行, provider 不再加
_MINUTE_OUTPUT_SYMBOLS = 50
_MINUTE_OUTPUT_ROWS = 100_000
_POOL_SIZE = 4
_FAILURE_STOP_RATIO = 0.6  # 失败率超过该比例(且样本足够)时中止整批同步
_FAILURE_STOP_MIN = 20

_TENCENT_BATCH = 60
_TENCENT_INTERVAL_S = 0.25

_MINUTE_SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Int64, "amount": pl.Float64,
}

_DEPTH_STRUCT = {
    "bid_prices": [1500.0] * 5, "bid_volumes": [10] * 5,
    "ask_prices": [1500.1] * 5, "ask_volumes": [12] * 5,
    "timestamp": 1788505200000,
}


@dataclass
class _FreetdxConfig:
    name: str = "freetdx"
    display_name: str = "通达信免费源 (pytdx)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def availability() -> tuple[bool, str]:
    """插件就绪检测: pytdx 可导入 + (源码态) 任一公网服务器连通。

    桌面版 (frozen) 不再在启动自检里做公网探测: pytdx 及依赖已随包分发,
    网络连通性属于运行时条件 —— 若在此处探测, 别人机器上公司防火墙封 7709
    端口就会把插件判为"不可用", 设置页随之出现无意义的"安装"按钮(桌面版
    根本不能 pip install), 表现为"通达信插件用不了"。连通性问题交给实际
    试拉/同步报错, 用户能直接看到具体原因。
    """
    try:
        import pytdx  # noqa: F401
    except ImportError:
        return False, "缺少 pytdx 依赖 (pip install pytdx)"
    if getattr(sys, "frozen", False):
        return True, "依赖已内置；连通性以试拉结果为准"
    try:
        pool = TdxPool(size=1)
        try:
            cnt = pool.call("get_security_count", 1, retries=1)
        finally:
            pool.close()
        if cnt:
            return True, "ok"
        return False, f"通达信公网服务器均不可达 (池: {SERVER_POOL[0][0]} 等 {len(SERVER_POOL)} 台)"
    except Exception as exc:  # noqa: BLE001
        return False, f"freetdx 连接失败: {type(exc).__name__}: {exc}"


def _candidate_dates(start: datetime, end: datetime) -> list[date]:
    """[start, end] 窗口内的工作日候选(升序, 截到今天); 节假日由空响应快速跳过。"""
    today = datetime.now(_BEIJING).date()
    d = start.date()
    dates: list[date] = []
    while d <= end.date() and d <= today:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _minute_time_of(index: int) -> tuple[int, int]:
    """分时点序号 → (hour, minute)。"""
    if index < 120:
        minute_of_day = 9 * 60 + 31 + index  # 09:31..11:30
    else:
        minute_of_day = 13 * 60 + 1 + (index - 120)  # 13:01..15:00
    return minute_of_day // 60, minute_of_day % 60


def _tick_rows(symbol: str, day: date, ticks: list | None) -> list[dict]:
    """单日分时 240 点 → 契约分钟行; 空响应/坏数据返回 []。"""
    if not ticks or len(ticks) < _BARS_PER_DAY:
        return []
    rows: list[dict] = []
    base = datetime(day.year, day.month, day.day)
    for i, tick in enumerate(ticks[:_BARS_PER_DAY]):
        try:
            price = float(tick["price"])
            vol = int(tick["vol"])
        except (TypeError, ValueError, KeyError):
            return []  # 单点坏 → 整日不可信, 不得伪造
        if price <= 0 or vol < 0:
            continue  # 集合竞价前/坏点跳过, 不伪造
        hh, mm = _minute_time_of(i)
        rows.append({
            "symbol": symbol,
            "datetime": base.replace(hour=hh, minute=mm),
            "open": price, "high": price, "low": price, "close": price,
            "volume": vol,
            # 固定口径推导(契约允许): 成交额 ≈ 分钟价 × 手 × 100
            "amount": round(price * vol * 100, 2),
        })
    return rows


class FreetdxProvider:
    name = "freetdx"
    builtin = True
    minute_history_days = MINUTE_HISTORY_DAYS

    def __init__(self) -> None:
        self.config = _FreetdxConfig()
        self._pool: TdxPool | None = None
        self._lock = threading.Lock()

    # -- 内部 -------------------------------------------------------------
    def _get_pool(self) -> TdxPool:
        with self._lock:
            if self._pool is None:
                self._pool = TdxPool(size=_POOL_SIZE)
            return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    # -- 分钟K -------------------------------------------------------------
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        chunks = list(self.iter_minute(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, freq=freq, on_chunk_done=on_chunk_done,
        ))
        parts = [df for df in chunks if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_MINUTE_SCHEMA)

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        if not symbols or asset_type not in ("stock", "etf", "index") or freq != "1m":
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        now = datetime.now(_BEIJING)
        if start_time is not None and start_time.tzinfo is not None:
            start_time = start_time.astimezone(_BEIJING).replace(tzinfo=None)
        if end_time is not None and end_time.tzinfo is not None:
            end_time = end_time.astimezone(_BEIJING).replace(tzinfo=None)
        dates = _candidate_dates(start_time or (now - timedelta(days=10)), end_time or now)
        if not dates:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        pool = self._get_pool()
        failures = 0
        seen = 0
        parts: list[pl.DataFrame] = []
        pending_rows = 0

        def _fetch(conn, symbol: str) -> pl.DataFrame | None:
            """单标的整个日期窗口 (在同一连接上串行执行, 保持每连接节流)。"""
            rows: list[dict] = []
            for day in dates:
                ticks = conn.call("get_history_minute_time_data",
                                  *_to_tdx_args(symbol), int(day.strftime("%Y%m%d")))
                rows.extend(_tick_rows(symbol, day, ticks))
            return pl.DataFrame(rows, schema=_MINUTE_SCHEMA) if rows else pl.DataFrame(schema=_MINUTE_SCHEMA)

        for symbol, df in pool.map(_fetch, list(symbols), on_progress=on_chunk_done):
            seen += 1
            if df is None or df.is_empty():
                failures += 1
            if failures >= _FAILURE_STOP_MIN and failures / seen >= _FAILURE_STOP_RATIO:
                raise ConnectionError(
                    f"freetdx 分钟K同步中止: {failures}/{seen} 标的失败, 服务器可能不可用或限流")
            if df is not None and not df.is_empty():
                parts.append(df)
                pending_rows += df.height
            if len(parts) >= _MINUTE_OUTPUT_SYMBOLS or pending_rows >= _MINUTE_OUTPUT_ROWS:
                yield pl.concat(parts, how="diagonal_relaxed")
                parts, pending_rows = [], 0
        if parts:
            yield pl.concat(parts, how="diagonal_relaxed")

    # -- 全量分钟 (修复轮) --------------------------------------------------
    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock",
    ) -> pl.DataFrame:
        if not symbols or asset_type not in ("stock", "etf", "index"):
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        today = datetime.now(_BEIJING).date()
        pool = self._get_pool()

        def _fetch(conn, symbol: str) -> pl.DataFrame | None:
            ticks = conn.call("get_history_minute_time_data",
                              *_to_tdx_args(symbol), int(today.strftime("%Y%m%d")))
            rows = _tick_rows(symbol, today, ticks)
            return pl.DataFrame(rows, schema=_MINUTE_SCHEMA) if rows else pl.DataFrame(schema=_MINUTE_SCHEMA)

        parts = [df for _, df in pool.map(_fetch, list(symbols)) if df is not None and not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_MINUTE_SCHEMA)

    # -- 五档盘口 (腾讯免费 HTTP) -------------------------------------------
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        batch = [s for s in symbols if s][:240]
        for i in range(0, len(batch), _TENCENT_BATCH):
            chunk = batch[i: i + _TENCENT_BATCH]
            if i:
                time.sleep(_TENCENT_INTERVAL_S)
            out.update(_tencent_depth_batch(chunk))
        return out

    # -- 试拉 ---------------------------------------------------------------
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        now = datetime.now()
        try:
            if dataset == "minute":
                syms = [s for s in (symbols or [])][:3] or ["600519.SH"]
                df = self.get_minute(syms, now - timedelta(days=3), now)
                return _test_result(self.name, dataset, df)
            if dataset == "full_minute":
                syms = [s for s in (symbols or [])][:3] or ["600519.SH"]
                df = self.get_intraday_batch(syms)
                return _test_result(self.name, dataset, df)
            if dataset == "depth5":
                syms = [s for s in (symbols or [])][:5] or ["600519.SH", "000001.SZ"]
                depths = self.get_depth_batch(syms)
                return {"provider": self.name, "dataset": dataset, "rows": len(depths),
                        "columns": list(_DEPTH_STRUCT.keys()),
                        "preview": [{"symbol": c, **d} for c, d in list(depths.items())[:5]]}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(exc)}
        return {"provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"freetdx 未接入 {dataset} 数据集(自动回退)"}


def _to_tdx_args(symbol: str) -> tuple[int, str]:
    tdx = to_tdx_code(symbol)
    if tdx is None:
        raise ValueError(f"freetdx 不支持该标的: {symbol}")
    return tdx


def _test_result(name: str, dataset: str, df: pl.DataFrame) -> dict:
    head = df.head(5).to_dicts()
    for row in head:
        for k, v in list(row.items()):
            if isinstance(v, (date, datetime)):
                row[k] = v.isoformat()
    return {"provider": name, "dataset": dataset, "rows": df.height,
            "columns": df.columns, "preview": head}


# ---------------------------------------------------------------------------
# 腾讯免费快照 (五档)
# ---------------------------------------------------------------------------

_TENCENT_MARKET = {"SH": "sh", "SZ": "sz"}
_DEPTH_RE = re.compile(r"v_(\w+)=")


def _tencent_depth_batch(symbols: list[str]) -> dict[str, dict]:
    import httpx

    codes = []
    valid: dict[str, str] = {}
    for s in symbols:
        code, _, suffix = s.partition(".")
        prefix = _TENCENT_MARKET.get(suffix.upper())
        if prefix and code.isdigit():
            tencent_code = f"{prefix}{code}"
            codes.append(tencent_code)
            valid[tencent_code] = s
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    resp = httpx.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    out: dict[str, dict] = {}
    text = resp.content.decode("gbk", errors="replace")
    for line in text.strip().splitlines():
        m = _DEPTH_RE.search(line)
        if not m:
            continue
        tencent_code = m.group(1)
        symbol = valid.get(tencent_code)
        if not symbol:
            continue
        payload = line.partition('="')[2].rstrip('";')
        fields = payload.split("~")
        if len(fields) < 31:
            continue
        try:
            bid_prices = [float(fields[9 + 2 * k]) for k in range(5)]
            bid_volumes = [int(float(fields[10 + 2 * k])) for k in range(5)]
            ask_prices = [float(fields[19 + 2 * k]) for k in range(5)]
            ask_volumes = [int(float(fields[20 + 2 * k])) for k in range(5)]
        except (ValueError, IndexError):
            continue
        if not any(p > 0 for p in bid_prices + ask_prices):
            continue  # 停牌/无盘口
        ts_ms: int | None = None
        raw_ts = fields[30]
        if re.fullmatch(r"\d{14}", raw_ts or ""):
            try:
                dt = datetime.strptime(raw_ts, "%Y%m%d%H%M%S").replace(tzinfo=_BEIJING)
                ts_ms = int(dt.timestamp() * 1000)
            except ValueError:
                ts_ms = None
        out[symbol] = {
            "bid_prices": bid_prices, "bid_volumes": bid_volumes,
            "ask_prices": ask_prices, "ask_volumes": ask_volumes,
            "timestamp": ts_ms if ts_ms is not None else int(time.time() * 1000),
        }
    return out


__all__ = ["FreetdxProvider", "availability", "MINUTE_HISTORY_DAYS"]
