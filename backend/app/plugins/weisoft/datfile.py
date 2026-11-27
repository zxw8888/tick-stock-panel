"""金字塔(WinStock)本地数据文件解析器 —— 纯标准库, 无第三方依赖。

文件布局 (V7.21 x64 实测, 已在日线/5分钟/1分钟三个周期交叉验证):

    文件头:
        日线   12 字节
            [0..7]   double  最后一个交易日的 OLE 日期
            [8..11]  int32   记录数
        分钟线 16 字节
            [0..3]   int32   记录数
            [4..7]   int32   0 (保留)
            [8..11]  float   版本号, 实测 7.2073
            [12..15] int32   最后一个交易日的「首根 bar 下标」

    记录 (定长 50 字节, 全部小端):
        0   double  OLE Automation 日期 (1899-12-30 起的天数, 小数=时分秒)
        8   float   Open
        12  float   High
        16  float   Low
        20  float   Close
        24  float   保留 (实测恒为 0)
        28  float   Volume
        32  float   Amount
        36  uint16 x7  明细 (日线无意义; 分笔含买卖档)

    文件尾可能残留不足一条记录的字节, 按 (size-header)//50 截断。

目录布局:
    Data\\<市场>\\<周期>\\<代码>            行情本体, 无扩展名
    Data\\<市场>\\<周期>\\<代码>_Ex2        附加字段(复权/财务因子), 不是行情

单位口径 (与 TSP 内部契约的差异, 由 provider 层负责换算):
    Volume  金字塔为「股」, TSP 契约为「手」 → 除以 100
    Amount  金字塔为「元」, 与 TSP 一致
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass
from pathlib import Path

OLE_EPOCH = _dt.datetime(1899, 12, 30)
RECORD_SIZE = 50

OFF_DATE = 0
OFF_OPEN = 8
OFF_HIGH = 12
OFF_LOW = 16
OFF_CLOSE = 20
OFF_RESERVED = 24
OFF_VOLUME = 28
OFF_AMOUNT = 32

# 周期目录名 → (文件头长度, TSP freq)
PERIOD_HEADER = {"Day": 12, "Min1": 16, "Min5": 16}
PERIOD_TO_FREQ = {"Day": "1d", "Min1": "1m", "Min5": "5m"}
FREQ_TO_PERIOD = {v: k for k, v in PERIOD_TO_FREQ.items()}


@dataclass(frozen=True)
class Bar:
    dt: _dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


def ole_to_datetime(value: float) -> _dt.datetime:
    """OLE Automation 日期 → datetime。"""
    return OLE_EPOCH + _dt.timedelta(days=value)


def read_bars(path: Path, header_size: int | None = None) -> list[Bar]:
    """读一个行情文件。header_size 为 None 时按文件大小自动探测。"""
    blob = path.read_bytes()
    total = len(blob)
    if total <= RECORD_SIZE:
        return []
    hs = header_size if header_size is not None else detect_header_size(path, blob)
    if total <= hs:
        return []
    count = (total - hs) // RECORD_SIZE
    out: list[Bar] = []
    for i in range(count):
        off = hs + i * RECORD_SIZE
        (raw_date,) = struct.unpack_from("<d", blob, off + OFF_DATE)
        try:
            dt = ole_to_datetime(raw_date)
        except (OverflowError, ValueError):
            continue
        o, h, lo, c = struct.unpack_from("<4f", blob, off + OFF_OPEN)
        _res, vol, amt = struct.unpack_from("<3f", blob, off + OFF_RESERVED)
        out.append(Bar(dt=dt, open=o, high=h, low=lo, close=c, volume=vol, amount=amt))
    return out


def detect_header_size(path: Path, blob: bytes | None = None) -> int:
    """自动探测文件头长度。

    优先「(size - hs) 能被 50 整除 + 首条日期合理 + OHLC 合法」;
    整除候选唯一时直接采用, 避免空文件误判。
    """
    if blob is None:
        blob = path.read_bytes()
    total = len(blob)
    exact: list[int] = []
    for hs in range(0, 33):
        if total <= hs + RECORD_SIZE:
            continue
        if (total - hs) % RECORD_SIZE != 0:
            continue
        try:
            (raw_date,) = struct.unpack_from("<d", blob, hs + OFF_DATE)
            o, h, low_, c = struct.unpack_from("<4f", blob, hs + OFF_OPEN)
        except struct.error:
            continue
        if not (30000 < raw_date < 60000):  # 1982 ~ 2064
            continue
        if not (0 < low_ <= min(o, c) and max(o, c) <= h < 1e6):
            continue
        exact.append(hs)
    if exact:
        return min(exact)

    # 容忍尾部残留字节: 抽查合法率
    best: tuple[int, int] | None = None
    for hs in range(0, 33):
        if total < hs + RECORD_SIZE:
            continue
        try:
            (raw_date,) = struct.unpack_from("<d", blob, hs + OFF_DATE)
        except struct.error:
            continue
        if not (30000 < raw_date < 60000):
            continue
        count = (total - hs) // RECORD_SIZE
        step = max(1, count // 50)
        ok = checked = 0
        for i in range(0, count, step):
            off = hs + i * RECORD_SIZE
            try:
                o, h, lo, c = struct.unpack_from("<4f", blob, off + OFF_OPEN)
            except struct.error:
                break
            checked += 1
            if 0 < lo <= min(o, c) and max(o, c) <= h < 1e6:
                ok += 1
        if checked and ok / checked > 0.95 and (best is None or ok > best[1]):
            best = (hs, ok)
    return best[0] if best else PERIOD_HEADER["Day"]


def list_periods(root: Path, market: str) -> list[str]:
    """某市场下实际存在的周期目录。"""
    mdir = root / market
    if not mdir.is_dir():
        return []
    return [p for p in PERIOD_HEADER if (mdir / p).is_dir()]


def list_codes(root: Path, market: str, period: str) -> list[str]:
    """某市场某周期下的品种代码(剔除 _Ex2 等附加文件)。"""
    pdir = root / market / period
    if not pdir.is_dir():
        return []
    return sorted(
        f.name for f in pdir.iterdir()
        if f.is_file() and not f.name.endswith("_Ex2") and not f.name.startswith(".")
    )


def list_markets(root: Path) -> list[str]:
    """Data 下实际有行情目录的市场。"""
    if not root.is_dir():
        return []
    out = []
    for m in sorted(root.iterdir()):
        if not m.is_dir() or m.name.startswith("$"):
            continue
        if any((m / p).is_dir() for p in PERIOD_HEADER):
            out.append(m.name)
    return out


def data_path(root: Path, market: str, period: str, code: str) -> Path:
    return root / market / period / code
