"""金字塔(WinStock)数据源 provider —— 本地 .dat 直读 + 在线包同步。

两种取数模式, 同一份数据:

  本地模式 (默认, 零网络)
    直接解析金字塔安装目录下 Data\\<市场>\\<周期>\\<代码>。
    优点是快、无外部依赖、能拿到 1990 年至今的完整历史;
    缺点是只能读到「金字塔已经下载过」的范围。

  同步模式 (按需, 走官方 FileData 服务器)
    下载 http://<server>/FileData/<市场>/<周期>/<YYYYMMDD>.zip,
    解包合并进同一批本地文件, 然后再由本地模式读取。
    这是唯一能把历史往前补的方式 (Min5 → 2000, Min1 → 2010, Day → 1990)。

单位换算 (金字塔 → TSP 内部契约, 见 CONTRIBUTING §3.1):
    volume  金字塔本身已是「手」, **直接透传, 不除 100**
            (实测 600000.SH 日线 vol=717,580 / amt=761,740,928 / close=10.67,
             amt÷(vol×close)=99.49 ⇒ vol 单位为手; TSP 现有落盘同值)
    amount  两者都是「元」, 直接透传
    OHLC    金字塔存的就是不复权原始价, 直接透传
    symbol  金字塔 SH510300 → TSP 510300.SH

时间口径:
    金字塔分钟线 OLE 日期是浮点数, 部分记录会落在 xx:44:59.999999 这种位置。
    契约要求北京墙钟整秒, 出口四舍五入到最近整秒。

未声明 realtime / depth5 / financial / adj_factor:
    金字塔本地文件不含这些数据, 声明了只会给出错误结果。provider_has_dataset
    返回 False, 框架自动回退 TickFlow。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.market_time import CN_TZ
from app.plugins.weisoft import datfile, names
from app.plugins.weisoft import sync as ws

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "minute")

_DAILY_SCHEMA = {
    "symbol": pl.String, "date": pl.Date,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Int64, "amount": pl.Float64,
}
_MINUTE_SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Int64, "amount": pl.Float64,
}

# 金字塔市场目录 → TSP 交易所后缀
_MARKET_SUFFIX = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}

# 环境变量 / secrets.json 键名
ENV_DATA_ROOT = "WEISOFT_DATA_ROOT"
ENV_SERVER = "WEISOFT_FILE_SERVER"
SECRETS_FIELD = "weisoft_data_root"

_DEFAULT_ROOTS = (
    r"D:\software\Weisoft Stock(x64)",
    r"C:\Weisoft Stock(x64)",
    r"D:\software\Weisoft Stock",
)

# 单次输出上限, 避免全市场深历史一次性堆在内存里
_OUTPUT_SYMBOLS = 50
_OUTPUT_ROWS = 200_000

# TSP 本地维表 (TickFlow / fuyao 同步留下的) —— 金字塔名称表对 ETF 覆盖率只有
# 4.8% (2015 年老快照), 用本地维表做二级回落, 不联网、不伪造
_TSP_INSTRUMENT_TABLES = (
    ("instruments", "instruments.parquet"),
    ("instruments_etf", "instruments_etf.parquet"),
    ("instruments_index", "instruments_index.parquet"),
    ("instruments_lof", "instruments_lof.parquet"),
)


def resolve_data_root() -> Path | None:
    """定位金字塔 Data 目录。环境变量 > secrets.json > 常见安装路径。"""
    env = os.environ.get(ENV_DATA_ROOT)
    if env:
        p = Path(env)
        return p if (p / "SH").is_dir() or p.is_dir() else None
    try:
        from app import secrets_store
        saved = secrets_store.get_env_backed_secret(SECRETS_FIELD, ENV_DATA_ROOT)
        if saved:
            p = Path(saved)
            if p.is_dir():
                return p
    except Exception:  # noqa: BLE001
        pass
    for base in _DEFAULT_ROOTS:
        p = Path(base) / "Data"
        if p.is_dir():
            return p
    return None


def resolve_server() -> str:
    return os.environ.get(ENV_SERVER) or ws.DEFAULT_SERVER


def availability() -> tuple[bool, str]:
    """插件就绪检测: 只要找到金字塔 Data 目录就算可用。

    不做网络探测 —— 同步是按需触发的动作, 网络不通不该让插件灰显。
    """
    root = resolve_data_root()
    if root is None:
        return False, (
            "未找到金字塔 Data 目录。请在设置中填写路径, "
            f"或设置环境变量 {ENV_DATA_ROOT}(例如 D:\\software\\Weisoft Stock(x64)\\Data)"
        )
    markets = datfile.list_markets(root)
    if not markets:
        return False, f"{root} 下没有任何行情目录"
    return True, f"ok ({root}, 市场: {','.join(markets)})"


def _to_symbol(market: str, code: str) -> str:
    """金字塔 (SH, 510300) → TSP 510300.SH。"""
    return f"{code}.{_MARKET_SUFFIX.get(market, market)}"


def _from_symbol(symbol: str) -> tuple[str, str]:
    """TSP 510300.SH → (SH, 510300)。也接受裸 6 位代码, 由调用方给市场。"""
    s = str(symbol).strip().upper()
    if "." in s:
        code, suffix = s.split(".", 1)
        return suffix, code
    return "", s


def _guess_market(code: str) -> str:
    """裸代码 → 市场。沪市 6/5/9 开头, 深市 0/1/2/3 开头。"""
    if not code:
        return "SH"
    c = code[0]
    if c in "69":
        return "SH"
    if c == "5":
        return "SH"
    if c in "0123":
        return "SZ"
    if c == "4" or c == "8":
        return "BJ"
    return "SH"


def _round_second(dt: datetime) -> datetime:
    """OLE 浮点日期 → 整秒。

    金字塔分钟线部分记录落在 xx:44:59.999999 这类位置 (浮点尾差),
    契约要求北京墙钟整秒, 这里四舍五入到最近整秒; 跨分钟时自然进位。
    """
    if dt.microsecond == 0:
        return dt
    if dt.microsecond >= 500_000:
        return (dt + timedelta(seconds=1)).replace(microsecond=0)
    return dt.replace(microsecond=0)


@lru_cache(maxsize=1)
def _tsp_local_names(sig: tuple) -> dict[str, str]:
    """TSP 自己同步过的维表 {symbol: name}。

    金字塔名称表里 ETF 只覆盖 4.8% (2015 快照), 而 TSP 的 instruments_etf
    是当日数据。这里只做**读本地 parquet**, 不联网、不改数据; 文件不存在
    或读失败就返回空, 由调用方回落代码本身。
    """
    out: dict[str, str] = {}
    from app.config import settings

    for folder, fname in _TSP_INSTRUMENT_TABLES:
        path = settings.data_dir / folder / fname
        if not path.is_file():
            continue
        try:
            import polars as pl
            df = pl.read_parquet(path, columns=["symbol", "name"])
        except Exception:  # noqa: BLE001
            continue
        for sym, name in zip(df["symbol"].to_list(), df["name"].to_list(), strict=False):
            if sym and name and str(name).strip():
                out.setdefault(str(sym), str(name).strip())
    return out


def _local_names() -> dict[str, str]:
    """按 (目录, 文件签名) 缓存 TSP 本地维表名称表。"""
    from app.config import settings

    sig: list = []
    for folder, fname in _TSP_INSTRUMENT_TABLES:
        p = settings.data_dir / folder / fname
        try:
            st = p.stat()
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((0, 0))
    return dict(_tsp_local_names(tuple(sig)))


@dataclass
class _WeisoftConfig:
    name: str = "weisoft"
    display_name: str = "金字塔 (WinStock)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class WeisoftProvider:
    name = "weisoft"
    builtin = True

    def __init__(self) -> None:
        self.config = _WeisoftConfig()
        self._root = resolve_data_root()

    # -- 内部 -------------------------------------------------------------

    def _require_root(self) -> Path:
        if self._root is None or not self._root.is_dir():
            self._root = resolve_data_root()
        if self._root is None:
            raise ProviderRequestError(
                "未配置金字塔 Data 目录; 请设置环境变量 "
                f"{ENV_DATA_ROOT} 或在设置页填写"
            )
        return self._root

    def _locate(self, symbol: str, period: str) -> Path | None:
        """按 symbol 找本地文件; 找不到时在 SH/SZ 两个市场里回退试探。"""
        root = self._require_root()
        market, code = _from_symbol(symbol)
        if market:
            p = root / market / period / code
            return p if p.exists() else None
        m = _guess_market(code)
        p = root / m / period / code
        if p.exists():
            return p
        for alt in ("SH", "SZ", "BJ"):
            p = root / alt / period / code
            if p.exists():
                return p
        return None

    def _read(self, symbol: str, period: str) -> tuple[list[datfile.Bar], str]:
        path = self._locate(symbol, period)
        if path is None:
            return [], symbol
        bars = datfile.read_bars(path)
        # 回填真实 market (裸代码场景)
        real_market = path.parent.parent.name
        return bars, _to_symbol(real_market, path.name)

    @staticmethod
    def _daily_frame(rows: list[dict]) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame(schema=_DAILY_SCHEMA)
        return pl.DataFrame(rows, schema=_DAILY_SCHEMA)

    @staticmethod
    def _minute_frame(rows: list[dict]) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        return pl.DataFrame(rows, schema=_MINUTE_SCHEMA)

    # -- 日K ---------------------------------------------------------------

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        chunks = list(self.iter_daily(
            symbols, start_time, end_time, asset_type=asset_type,
            on_chunk_done=on_chunk_done,
        ))
        parts = [df for df in chunks if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_DAILY_SCHEMA)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """分批产出日K。金字塔本地文件不分资产类型, 全部按同一格式读。"""
        syms = [s for s in (symbols or []) if s]
        if not syms:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return

        lo = start_time.date() if isinstance(start_time, datetime) else start_time
        hi = end_time.date() if isinstance(end_time, datetime) else end_time

        total = len(syms)
        parts: list[dict] = []
        pending_rows = 0
        missing: list[str] = []

        for i, sym in enumerate(syms, 1):
            try:
                bars, real_sym = self._read(sym, "Day")
            except Exception as exc:  # noqa: BLE001
                logger.warning("weisoft 日K读取失败 %s: %s", sym, exc)
                bars, real_sym = [], sym
            if not bars:
                missing.append(sym)
                if on_chunk_done:
                    on_chunk_done(i, total)
                continue
            for b in bars:
                d = b.dt.date()
                if lo and d < lo:
                    continue
                if hi and d > hi:
                    continue
                parts.append({
                    "symbol": real_sym,
                    "date": d,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    # 金字塔 volume 单位本身就是手 → TSP 契约也是手, 直接透传
                    "volume": round(b.volume),
                    "amount": float(b.amount),
                })
            pending_rows += len(parts)
            if len(parts) >= _OUTPUT_SYMBOLS or pending_rows >= _OUTPUT_ROWS:
                yield self._daily_frame(parts)
                parts, pending_rows = [], 0
            if on_chunk_done:
                on_chunk_done(i, total)

        if parts:
            yield self._daily_frame(parts)
        if missing and len(missing) == total:
            # 全部缺失 = 数据源没准备好, 明确报错而不是静默空数据
            raise ProviderRequestError(
                f"weisoft 本地没有任何这些标的的日K数据: {', '.join(missing[:5])}"
                f"{'...' if len(missing) > 5 else ''}; 请先在金字塔里补充数据"
            )
        if missing:
            logger.info("weisoft 日K 缺 %d/%d 个标的 (本地未下载)", len(missing), total)

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
            symbols, start_time, end_time, asset_type=asset_type,
            freq=freq, on_chunk_done=on_chunk_done,
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
        period = datfile.FREQ_TO_PERIOD.get(freq)
        if period is None or period == "Day":
            raise ProviderRequestError(
                f"weisoft 不支持 freq={freq!r}; 可用: 1m / 5m"
            )
        syms = [s for s in (symbols or []) if s]
        if not syms:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return

        lo = start_time
        hi = end_time
        # 调用方 (kline_sync) 传的是带时区的 CN_TZ datetime, 而 .dat 解析出来的
        # 是北京墙钟 naive datetime; 不统一会抛
        # "can't compare offset-naive and offset-aware datetimes"。
        if lo is not None and lo.tzinfo is not None:
            lo = lo.astimezone(CN_TZ).replace(tzinfo=None)
        if hi is not None and hi.tzinfo is not None:
            hi = hi.astimezone(CN_TZ).replace(tzinfo=None)

        total = len(syms)
        parts: list[dict] = []
        pending_rows = 0
        missing: list[str] = []

        for i, sym in enumerate(syms, 1):
            try:
                bars, real_sym = self._read(sym, period)
            except Exception as exc:  # noqa: BLE001
                logger.warning("weisoft 分钟K读取失败 %s: %s", sym, exc)
                bars, real_sym = [], sym
            if not bars:
                missing.append(sym)
                if on_chunk_done:
                    on_chunk_done(i, total)
                continue
            for b in bars:
                if lo and b.dt < lo:
                    continue
                if hi and b.dt > hi:
                    continue
                parts.append({
                    "symbol": real_sym,
                    # 金字塔存的是北京墙钟 naive 时间; OLE 浮点会带出
                    # xx:44:59.999999 这种尾差, 契约要求整秒 → 四舍五入
                    "datetime": _round_second(b.dt),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    # 金字塔 volume 单位本身就是手, 直接透传
                    "volume": round(b.volume),
                    "amount": float(b.amount),
                })
            pending_rows += len(parts)
            if len(parts) >= _OUTPUT_SYMBOLS or pending_rows >= _OUTPUT_ROWS:
                yield self._minute_frame(parts)
                parts, pending_rows = [], 0
            if on_chunk_done:
                on_chunk_done(i, total)

        if parts:
            yield self._minute_frame(parts)
        if missing and len(missing) == total:
            raise ProviderRequestError(
                f"weisoft 本地没有任何这些标的的 {period} 数据: "
                f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}"
            )
        if missing:
            logger.info("weisoft %s 缺 %d/%d 个标的 (本地未下载)",
                        period, len(missing), total)

    # -- 同步动作 (供 API / 脚本调用) ---------------------------------------

    def sync_day(self, market: str, period: str, ymd: str,
                 codes: set[str] | None = None, dry_run: bool = False,
                 use_cache: bool = True) -> list[ws.MergeResult]:
        """同步一天。period 用金字塔目录名 (Day/Min1/Min5)。"""
        return ws.sync_one_day(self._require_root(), market, period, ymd,
                               server=resolve_server(), codes=codes, dry_run=dry_run,
                               use_cache=use_cache)

    def sync_range(self, market: str, period: str, start_ymd: str, end_ymd: str,
                   codes: set[str] | None = None, dry_run: bool = False,
                   use_cache: bool = True,
                   on_day_done=None) -> dict[str, list[ws.MergeResult]]:
        return ws.sync_range(self._require_root(), market, period, start_ymd, end_ymd,
                             server=resolve_server(), codes=codes, dry_run=dry_run,
                             use_cache=use_cache, on_day_done=on_day_done)

    # -- 元数据 -------------------------------------------------------------

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """枚举本地标的, 按 asset_type 过滤后返回。

        金字塔文件名不带类型, 只能按代码段推断 (见 names.classify)。
        `asset_type` 是**过滤器**而非标注: instrument_sync 用 "stock" 调用
        本方法写股票维表, 若把指数/ETF 一并塞进去, 股票池、筛选、回测都会
        把指数当个股, 静默产出错误结果。

        名称来源优先级:
          1. Setting\\<市场>_Static_Market.dat  (每日更新的 A 股名)
          2. Data\\<市场>\\REPORT2_64.DAT      (2015 老快照, 含指数/ETF)
          3. TSP 本地维表 (instruments_etf/index/lof, 当日) —— 只读, 不联网
          4. 回落为 symbol 本身, 不伪造

        bond / other (REITs、B 股、510300.bak 这类残留文件) 不在 TSP 的
        资产类型集合内, 一律不返回 (否则会污染股票池)。
        """
        from app.asset_types import ASSET_TYPES

        root = self._require_root()
        want = (asset_type or "stock").strip().lower()
        if want not in ASSET_TYPES:
            return []
        markets = datfile.list_markets(root)
        name_maps = {m: names.load_names(root, m) for m in markets}
        tsp_names = _local_names()
        out: list[dict] = []
        for market in markets:
            name_map = name_maps.get(market) or {}
            for code in datfile.list_codes(root, market, "Day"):
                if names.classify(code, market) != want:
                    continue
                sym = _to_symbol(market, code)
                out.append({
                    "symbol": sym,
                    "name": name_map.get(code) or tsp_names.get(sym) or sym,
                    "code": code,
                    "exchange": _MARKET_SUFFIX.get(market, market),
                    "asset_type": want,
                    "type": want,
                    "source": self.name,
                })
        return out

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」。"""
        try:
            if dataset == "daily":
                syms = [s for s in (symbols or [])][:3] or ["510300.SH", "000001.SH"]
                df = self.get_daily(syms, None, None)
                return _test_result(self.name, dataset, df)
            if dataset == "minute":
                syms = [s for s in (symbols or [])][:3] or ["510300.SH"]
                df = self.get_minute(syms, None, None, freq="1m")
                return _test_result(self.name, dataset, df)
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "error": str(exc)}
        return {"provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"weisoft 未接入 {dataset} 数据集(自动回退)"}

    def close(self) -> None:
        self._root = None


def _test_result(name: str, dataset: str, df: pl.DataFrame) -> dict:
    if df is None or df.is_empty():
        return {"provider": name, "dataset": dataset, "rows": 0,
                "error": "本地无数据 (请先在金字塔里补充, 或调用同步接口)"}
    head = df.head(5).to_dicts()
    return {
        "provider": name,
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": head,
    }
