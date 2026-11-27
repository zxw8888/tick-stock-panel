"""weisoft 插件契约测试 —— 不依赖真实金字塔安装, 全部用临时构造的文件。

覆盖:
  - 数据文件解析 (日线 12 字节头 / 分钟线 16 字节头, 尾部残留字节)
  - 单位换算 (金字塔 volume 股 → TSP 手)
  - 代码格式 (SH510300 → 510300.SH)
  - 包解析 (代码 + 保留 + 记录数 + N×50)
  - 合并 (按日期去重, 同日期以新为准, 升序)
  - 文件头重建 (记录数 / 最后交易日索引)
  - provider 契约 (iter_daily / iter_minute 分批, 全缺失时明确报错)
"""

from __future__ import annotations

import datetime as dt
import io
import struct
import zipfile
from pathlib import Path

import pytest

from app.data_providers.base import ProviderRequestError
from app.plugins.weisoft import datfile
from app.plugins.weisoft import sync as ws
from app.plugins.weisoft.provider import WeisoftProvider

OLE_EPOCH = dt.datetime(1899, 12, 30)


def _ole(d: dt.date) -> float:
    return (dt.datetime(d.year, d.month, d.day) - OLE_EPOCH).total_seconds() / 86400.0


def _rec(d: dt.date, o: float, h: float, lo: float, c: float,
         vol: float, amt: float) -> bytes:
    """构造一条 50 字节记录。

    布局: 8 日期 + 16 OHLC + 4 保留 + 4 量 + 4 额 + 14 明细(uint16×7) = 50。
    """
    return (
        struct.pack("<d", _ole(d))                 # 0   日期
        + struct.pack("<4f", o, h, lo, c)          # 8   OHLC
        + struct.pack("<f", 0.0)                   # 24  保留
        + struct.pack("<2f", vol, amt)             # 28  量 / 额
        + struct.pack("<7H", 0, 0, 0, 0, 0, 0, 0)  # 36  明细
    )


def _write_day_file(path: Path, bars: list[tuple]) -> None:
    """写日线文件 (12 字节头)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(_rec(*b) for b in bars)
    head = bytearray(12)
    if bars:
        struct.pack_into("<d", head, 0, _ole(bars[-1][0]))
        struct.pack_into("<i", head, 8, len(bars))
    path.write_bytes(bytes(head) + body)


def _write_min_file(path: Path, bars: list[tuple]) -> None:
    """写分钟线文件 (16 字节头)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(_rec(*b) for b in bars)
    head = bytearray(16)
    struct.pack_into("<i", head, 0, len(bars))
    struct.pack_into("<f", head, 8, 7.2073)
    path.write_bytes(bytes(head) + body)


# ---------------------------------------------------------------- 解析


def test_read_day_file(tmp_path: Path) -> None:
    p = tmp_path / "510300"
    _write_day_file(p, [
        (dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 1_000_000.0, 2_100_000.0),
        (dt.date(2024, 1, 3), 2.1, 2.3, 2.0, 2.2, 1_200_000.0, 2_640_000.0),
    ])
    bars = datfile.read_bars(p)
    assert len(bars) == 2
    assert bars[0].dt.date() == dt.date(2024, 1, 2)
    assert bars[1].close == pytest.approx(2.2)
    assert bars[1].volume == pytest.approx(1_200_000.0)


def test_read_minute_file(tmp_path: Path) -> None:
    p = tmp_path / "510300"
    _write_min_file(p, [
        (dt.date(2024, 1, 2), 2.0, 2.1, 1.95, 2.05, 1000.0, 2050.0),
    ])
    bars = datfile.read_bars(p)
    assert len(bars) == 1
    assert bars[0].close == pytest.approx(2.05)


def test_header_detect_tolerates_trailing_bytes(tmp_path: Path) -> None:
    """文件尾残留不足一条记录的字节时仍能正确定位头部。"""
    p = tmp_path / "510300"
    _write_day_file(p, [
        (dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 100.0, 210.0),
    ])
    p.write_bytes(p.read_bytes() + b"\x00" * 42)
    assert datfile.detect_header_size(p) == 12
    assert len(datfile.read_bars(p)) == 1


def test_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert datfile.read_bars(p) == []


def test_minute_datetime_rounded_to_whole_second(tmp_path: Path) -> None:
    """金字塔 OLE 浮点日期会带出 xx:44:59.999999 尾差。

    契约要求北京墙钟整秒 (见 docs/plugin-development.md), provider 出口必须
    四舍五入到最近整秒 —— 否则 14:44:59.999999 会被落成 14:44:59, 与
    14:45:00 的真实 bar 时间错位 1 秒。
    """
    p = tmp_path / "SH" / "Min1" / "510300"
    day = dt.date(2024, 1, 2)
    # 手工写两条带微秒尾差的记录
    base = _ole(day)
    recs = b""
    for frac, close in ((0.614583333, 2.05), (0.6145833329, 2.06)):
        recs += struct.pack("<d", base + frac)
        recs += struct.pack("<4f", 2.0, 2.1, 1.95, close)
        recs += struct.pack("<f", 0.0)
        recs += struct.pack("<2f", 1000.0, 2050.0)
        recs += struct.pack("<7H", 0, 0, 0, 0, 0, 0, 0)
    head = bytearray(16)
    struct.pack_into("<i", head, 0, 2)
    struct.pack_into("<f", head, 8, 7.2073)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(head) + recs)

    root = tmp_path
    prov = WeisoftProvider()
    prov._root = root
    df = prov.get_minute(["510300.SH"], None, None, freq="1m")
    assert df.height == 2
    stamps = df["datetime"].to_list()
    assert all(x.microsecond == 0 for x in stamps)
    assert stamps[0].strftime("%H:%M:%S") == "14:45:00"


# ---------------------------------------------------------------- 包解析


def _build_package(symbols: list[tuple[str, list[tuple]]]) -> bytes:
    """构造一个金字塔 .dat 包。

    真实布局 (2026-11 实测): [0..3] 头长度 100, [4..53] GBK 版本横幅 + 对齐,
    [54..] 品种串联。品种代码不限于「市场前缀 + 6 位数字」——
    全量包里有 SHZS2000 这类自编指数, 测试必须覆盖。
    """
    out = bytearray(54)
    struct.pack_into("<i", out, 0, 100)
    banner = "金字塔决策交易系统(x64) 2025 (V7.20)".encode("gbk")
    out[4:4 + len(banner)] = banner
    for code, bars in symbols:
        out += code.encode("ascii").ljust(8, b"\x00")
        out += struct.pack("<I", 0)          # 保留
        out += struct.pack("<I", len(bars))  # 记录数
        for b in bars:
            out += _rec(*b)
    return bytes(out)


def test_parse_package() -> None:
    blob = _build_package([
        ("SH510300", [
            (dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 1000.0, 2100.0),
            (dt.date(2024, 1, 3), 2.1, 2.3, 2.0, 2.2, 1200.0, 2640.0),
        ]),
        ("SZ000001", [
            (dt.date(2024, 1, 2), 10.0, 10.5, 9.9, 10.2, 5000.0, 51000.0),
        ]),
    ])
    syms = ws.parse_package(blob)
    assert len(syms) == 2
    by_code = {s.code: s for s in syms}
    assert by_code["SH510300"].local_code == "510300"
    assert by_code["SH510300"].market == "SH"
    assert len(by_code["SH510300"].records) == 2 * datfile.RECORD_SIZE
    assert len(by_code["SZ000001"].records) == datfile.RECORD_SIZE


def test_parse_package_no_symbols() -> None:
    with pytest.raises(ws.SyncError, match="no symbol at offset"):
        ws.parse_package(b"\x00" * 200)


def test_parse_package_handles_non_numeric_codes() -> None:
    """全量包里有 SHZS2000 这类自编指数代码。

    旧的正则解析会漏掉它们, 把前一个品种的记录区错误延伸到下一个可识别
    代码 (实测 20240701 包 SH900957 吞掉 26.8MB)。顺序解析必须精确对齐。
    """
    blob = _build_package([
        ("SH900957", [(dt.date(2024, 1, 2), 1.0, 1.1, 0.9, 1.0, 100.0, 100.0)]),
        ("SHZS2000", [
            (dt.date(2024, 1, 2), 2.0, 2.1, 1.9, 2.0, 200.0, 400.0),
            (dt.date(2024, 1, 3), 2.0, 2.2, 1.9, 2.1, 300.0, 630.0),
        ]),
        ("SH900999", [(dt.date(2024, 1, 2), 3.0, 3.1, 2.9, 3.0, 300.0, 900.0)]),
    ])
    syms = ws.parse_package(blob)
    by_code = {s.code: s for s in syms}
    assert set(by_code) == {"SH900957", "SHZS2000", "SH900999"}
    assert len(by_code["SH900957"].records) == 1 * datfile.RECORD_SIZE
    assert len(by_code["SHZS2000"].records) == 2 * datfile.RECORD_SIZE
    assert len(by_code["SH900999"].records) == 1 * datfile.RECORD_SIZE


def test_parse_package_truncated_raises() -> None:
    blob = _build_package([
        ("SH510300", [(dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 1000.0, 2100.0)]),
    ])
    with pytest.raises(ws.SyncError, match="truncated"):
        ws.parse_package(blob[:-10])


def test_read_zip_skips_ex_file() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20240102.dat", b"MAIN")
        zf.writestr("20240102Ex.dat", b"EXTRA-LONG-NAME")
    assert ws.read_zip(buf.getvalue()) == b"MAIN"


# ---------------------------------------------------------------- 合并


def test_merge_dedup_and_order() -> None:
    old = [(_ole(dt.date(2024, 1, 3)), b"old3"), (_ole(dt.date(2024, 1, 2)), b"old2")]
    new = [(_ole(dt.date(2024, 1, 3)), b"new3"), (_ole(dt.date(2024, 1, 4)), b"new4")]
    merged, added, replaced = ws.merge_records(old, new)
    assert [r[1] for r in merged] == [b"old2", b"new3", b"new4"]
    assert added == 1
    assert replaced == 1


def test_merge_same_content_not_counted_as_replaced() -> None:
    d = _ole(dt.date(2024, 1, 2))
    merged, added, replaced = ws.merge_records([(d, b"same")], [(d, b"same")])
    assert added == 0
    assert replaced == 0
    assert len(merged) == 1


# ---------------------------------------------------------------- 文件头重建


def test_build_header_day() -> None:
    recs = [(_ole(dt.date(2024, 1, 2)), b"a"), (_ole(dt.date(2024, 1, 3)), b"b")]
    head = ws.build_header("Day", recs)
    assert len(head) == 12
    assert struct.unpack_from("<d", head, 0)[0] == pytest.approx(_ole(dt.date(2024, 1, 3)))
    assert struct.unpack_from("<i", head, 8)[0] == 2


def test_build_header_minute_tracks_last_day_index() -> None:
    """分钟线头部 [12..15] 是最后交易日的首根 bar 下标。"""
    day1 = dt.date(2024, 1, 2)
    day2 = dt.date(2024, 1, 3)
    recs = [(_ole(day1), b"a"), (_ole(day1), b"b"),
            (_ole(day2), b"c"), (_ole(day2), b"d")]
    head = ws.build_header("Min1", recs)
    assert struct.unpack_from("<i", head, 0)[0] == 4
    assert struct.unpack_from("<i", head, 12)[0] == 2


def test_build_header_preserves_version_bytes() -> None:
    template = bytearray(16)
    struct.pack_into("<f", template, 8, 7.2073)
    recs = [(_ole(dt.date(2024, 1, 2)), b"a")]
    head = ws.build_header("Min1", recs, bytes(template))
    assert struct.unpack_from("<f", head, 8)[0] == pytest.approx(7.2073)


# ---------------------------------------------------------------- provider


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "Data"
    _write_day_file(root / "SH" / "Day" / "510300", [
        (dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 1_000_000.0, 2_100_000.0),
        (dt.date(2024, 1, 3), 2.1, 2.3, 2.0, 2.2, 1_200_000.0, 2_640_000.0),
    ])
    _write_min_file(root / "SH" / "Min1" / "510300", [
        (dt.date(2024, 1, 2), 2.0, 2.1, 1.95, 2.05, 100_000.0, 205_000.0),
    ])
    return root


def _provider(root: Path) -> WeisoftProvider:
    p = WeisoftProvider()
    p._root = root
    return p


def test_provider_daily_units(data_root: Path) -> None:
    p = _provider(data_root)
    df = p.get_daily(["510300.SH"], None, None)
    assert df.height == 2
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    row = df.row(0, named=True)
    assert row["symbol"] == "510300.SH"
    # 金字塔 volume 本身就是手 → 直接透传, 不除 100
    # (真实数据自验: 600000.SH amt÷(vol×close)≈99.49 ⇒ vol 单位是手)
    assert row["volume"] == 1_000_000
    assert row["amount"] == pytest.approx(2_100_000.0)


def test_provider_daily_date_filter(data_root: Path) -> None:
    p = _provider(data_root)
    df = p.get_daily(["510300.SH"],
                     dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 31))
    assert df.height == 1
    assert df.row(0, named=True)["date"] == dt.date(2024, 1, 3)


def test_provider_minute(data_root: Path) -> None:
    p = _provider(data_root)
    df = p.get_minute(["510300.SH"], None, None, freq="1m")
    assert df.height == 1
    # 同上: 手口径透传
    assert df.row(0, named=True)["volume"] == 100_000


def test_provider_minute_accepts_tz_aware_bounds(data_root: Path) -> None:
    """kline_sync 传 CN_TZ aware datetime, .dat 出来的是 naive 墙钟。"""
    from app.market_time import CN_TZ

    p = _provider(data_root)
    df = p.get_minute(
        ["510300.SH"],
        dt.datetime(2024, 1, 1, tzinfo=CN_TZ),
        dt.datetime(2024, 1, 3, tzinfo=CN_TZ),
        freq="1m",
    )
    assert df.height == 1


def test_provider_rejects_unknown_freq(data_root: Path) -> None:
    p = _provider(data_root)
    with pytest.raises(ProviderRequestError, match="不支持 freq"):
        p.get_minute(["510300.SH"], None, None, freq="30m")


def test_provider_all_missing_raises(data_root: Path) -> None:
    """全部标的都没有本地数据时必须明确报错, 不能静默返回空。"""
    p = _provider(data_root)
    with pytest.raises(ProviderRequestError, match="本地没有任何这些标的"):
        p.get_daily(["999999.SH"], None, None)


def test_provider_bare_code_resolves_market(data_root: Path) -> None:
    """裸 6 位代码按首位猜市场, 并把真实市场回填到 symbol。"""
    p = _provider(data_root)
    df = p.get_daily(["510300"], None, None)
    assert df.height == 2
    assert df.row(0, named=True)["symbol"] == "510300.SH"


def test_provider_iter_chunks(data_root: Path) -> None:
    p = _provider(data_root)
    chunks = list(p.iter_daily(["510300.SH"], None, None))
    assert chunks
    assert sum(df.height for df in chunks) == 2


def test_provider_instruments(data_root: Path) -> None:
    """510300 是 ETF → 按 asset_type 过滤时不能出现在 stock 里。"""
    p = _provider(data_root)
    assert p.get_instruments("stock") == []
    ins = p.get_instruments("etf")
    assert [x["symbol"] for x in ins] == ["510300.SH"]
    assert ins[0]["source"] == "weisoft"
    assert ins[0]["asset_type"] == "etf"


def test_provider_instruments_rejects_unknown_type(data_root: Path) -> None:
    """bond/other 不是 TSP 资产类型 → 必须返回空, 不能把债券塞进股票池。"""
    p = _provider(data_root)
    assert p.get_instruments("bond") == []
    assert p.get_instruments("other") == []
    assert p.get_instruments("") == []


def test_provider_instruments_missing_name_falls_back_to_symbol(tmp_path: Path) -> None:
    """名称表缺失时回落为代码本身, 不伪造名称。"""
    root = tmp_path / "Data"
    _write_day_file(root / "SH" / "Day" / "600000", [
        (dt.date(2024, 1, 2), 10.0, 10.2, 9.9, 10.1, 1_000.0, 10_100.0),
    ])
    ins = _provider(root).get_instruments("stock")
    assert [x["symbol"] for x in ins] == ["600000.SH"]
    assert ins[0]["name"] == "600000.SH"


@pytest.mark.parametrize(("code", "market", "expected"), [
    # A 股白名单
    ("600000", "SH", "stock"),
    ("688981", "SH", "stock"),
    ("000001", "SZ", "stock"),
    ("300750", "SZ", "stock"),
    # 指数
    ("000001", "SH", "index"),
    ("399001", "SZ", "index"),
    ("980001", "SZ", "index"),
    ("ZS2000", "SH", "index"),
    # 场内基金
    ("510300", "SH", "etf"),
    ("588000", "SH", "etf"),
    ("159915", "SZ", "etf"),
    ("158001", "SZ", "etf"),
    ("501001", "SH", "lof"),
    ("160105", "SZ", "lof"),
    # 不进股票池
    ("019218", "SH", "bond"),
    ("113050", "SH", "bond"),
    ("204001", "SH", "bond"),
    ("128001", "SZ", "bond"),
    ("508000", "SH", "other"),      # REITs
    ("180101", "SZ", "other"),      # REITs
    ("900901", "SH", "other"),      # B 股
    ("510300.bak", "SH", "other"),  # 残留文件
])
def test_classify(code: str, market: str, expected: str) -> None:
    from app.plugins.weisoft.names import classify

    assert classify(code, market) == expected


def test_provider_test_dataset(data_root: Path) -> None:
    p = _provider(data_root)
    out = p.test_dataset("daily", ["510300.SH"])
    assert out["rows"] == 2
    assert "preview" in out
    assert out["provider"] == "weisoft"


def test_provider_test_dataset_unsupported(data_root: Path) -> None:
    p = _provider(data_root)
    out = p.test_dataset("realtime", ["510300.SH"])
    assert "error" in out
    assert "未接入" in out["error"]


# ---------------------------------------------------------------- 同步端到端


def test_sync_one_day_writes_merged(tmp_path: Path, monkeypatch) -> None:
    """用构造的包验证「下载 → 解包 → 合并 → 落盘」全链路。"""
    root = tmp_path / "Data"
    _write_day_file(root / "SH" / "Day" / "510300", [
        (dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 1000.0, 2100.0),
    ])

    pkg = _build_package([
        ("SH510300", [
            (dt.date(2024, 1, 2), 2.0, 2.2, 1.9, 2.1, 1000.0, 2100.0),  # 已有, 相同
            (dt.date(2024, 1, 3), 2.1, 2.3, 2.0, 2.2, 1200.0, 2640.0),  # 新增
        ]),
        ("SH600000", [
            (dt.date(2024, 1, 3), 7.0, 7.2, 6.9, 7.1, 3000.0, 21300.0),
        ]),
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20240103.dat", pkg)

    def _fake_fetch(server, market, period, ymd, timeout=300, data_root=None):
        return buf.getvalue()

    monkeypatch.setattr(ws, "fetch_package", _fake_fetch)

    results = ws.sync_one_day(root, "SH", "Day", "20240103")
    by_code = {r.code: r for r in results}
    assert by_code["510300"].before == 1
    assert by_code["510300"].after == 2
    assert by_code["510300"].added == 1
    assert by_code["510300"].replaced == 0
    assert by_code["600000"].added == 1

    # 落盘后能被重新解析, 且按日期升序
    bars = datfile.read_bars(root / "SH" / "Day" / "510300")
    assert [b.dt.date() for b in bars] == [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]


def test_sync_one_day_codes_filter(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "Data"
    pkg = _build_package([
        ("SH510300", [(dt.date(2024, 1, 3), 2.1, 2.3, 2.0, 2.2, 1200.0, 2640.0)]),
        ("SH600000", [(dt.date(2024, 1, 3), 7.0, 7.2, 6.9, 7.1, 3000.0, 21300.0)]),
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20240103.dat", pkg)
    monkeypatch.setattr(ws, "fetch_package", lambda *a, **k: buf.getvalue())

    results = ws.sync_one_day(root, "SH", "Day", "20240103", codes={"510300"})
    assert [r.code for r in results] == ["510300"]
    assert not (root / "SH" / "Day" / "600000").exists()


def test_sync_one_day_dry_run_no_write(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "Data"
    pkg = _build_package([
        ("SH510300", [(dt.date(2024, 1, 3), 2.1, 2.3, 2.0, 2.2, 1200.0, 2640.0)]),
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20240103.dat", pkg)
    monkeypatch.setattr(ws, "fetch_package", lambda *a, **k: buf.getvalue())

    ws.sync_one_day(root, "SH", "Day", "20240103", dry_run=True)
    assert not (root / "SH" / "Day" / "510300").exists()


def test_sync_one_day_no_package(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "fetch_package", lambda *a, **k: None)
    assert ws.sync_one_day(tmp_path / "Data", "SH", "Day", "20240103") == []


def test_iter_trade_days_skips_weekend() -> None:
    days = list(ws.iter_trade_days("20240105", "20240109"))  # 周五 ~ 周二
    assert days == ["20240105", "20240108", "20240109"]
