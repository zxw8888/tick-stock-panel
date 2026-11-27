"""金字塔品种名称表 —— 两个来源合并, 与行情目录互为超集。

金字塔**没有单一的「完整品种表」文件**。名称分散在两个互不覆盖的地方
(2026-11-26 在真实安装上实测):

来源 A: Setting\\<市场>_Static_Market.dat    127 字节/条, 每日更新
    +0    10B  ASCII 代码
    +20   40B  GBK 名称
    只含 A 股股票 (SH 2319 / SZ 1494), **没有 ETF/LOF/债券/指数**。

来源 B: Data\\<市场>\\REPORT2_64.DAT          589 字节/条, 随安装包的 2015 快照
    +148  40B  GBK 名称
    +196  10B  ASCII 代码
    含指数/ETF/B股, 但**没有 688981 / 605499 / 588000** 这类新标的。

「有哪些品种」以行情目录为准 (Data\\<市场>\\Day 的文件名), 名称表只用来补
中文名; 查不到名称时返回空串, 由调用方决定回落成代码。

资产分类只能按代码段推断 —— 金字塔的文件名不带类型。规则见 classify()。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# --- 来源 A: Setting\<市场>_Static_Market.dat ---
_STATIC_REC = 127
_STATIC_CODE_OFF, _STATIC_CODE_LEN = 0, 10
_STATIC_NAME_OFF, _STATIC_NAME_LEN = 20, 40

# --- 来源 B: Data\<市场>\REPORT2_64.DAT ---
_REPORT_REC = 589
_REPORT_NAME_OFF, _REPORT_NAME_LEN = 148, 40
_REPORT_CODE_OFF, _REPORT_CODE_LEN = 196, 10

_NUM6 = re.compile(r"^\d{6}$")


def _gbk(raw: bytes) -> str:
    raw = raw.split(b"\x00")[0]
    if not raw:
        return ""
    return raw.decode("gbk", "ignore").strip()


def _ascii(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("ascii", "ignore").strip()


def _load_fixed(path: Path, rec: int, code_off: int, code_len: int,
                name_off: int, name_len: int) -> dict[str, str]:
    if not path.is_file():
        return {}
    blob = path.read_bytes()
    out: dict[str, str] = {}
    for i in range(len(blob) // rec):
        off = i * rec
        code = _ascii(blob[off + code_off:off + code_off + code_len])
        if not _NUM6.match(code):
            continue
        name = _gbk(blob[off + name_off:off + name_off + name_len])
        if name:
            out[code] = name
    return out


@lru_cache(maxsize=8)
def _names_cached(install_root: str, market: str, mtime_sig: tuple) -> dict[str, str]:
    root = Path(install_root)
    # 来源 B (老快照, 含指数/ETF) 打底, 来源 A (每日更新) 覆盖
    merged = _load_fixed(
        root / "Data" / market / "REPORT2_64.DAT",
        _REPORT_REC, _REPORT_CODE_OFF, _REPORT_CODE_LEN,
        _REPORT_NAME_OFF, _REPORT_NAME_LEN,
    )
    merged.update(_load_fixed(
        root / "Setting" / f"{market}_Static_Market.dat",
        _STATIC_REC, _STATIC_CODE_OFF, _STATIC_CODE_LEN,
        _STATIC_NAME_OFF, _STATIC_NAME_LEN,
    ))
    return merged


def load_names(data_root: Path, market: str) -> dict[str, str]:
    """{6 位代码: 中文名}。data_root 是 .../Data, 安装目录是它的父级。"""
    root = data_root.parent
    sig: list = []
    for rel in (Path("Data") / market / "REPORT2_64.DAT",
                Path("Setting") / f"{market}_Static_Market.dat"):
        p = root / rel
        try:
            st = p.stat()
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((0, 0))
    return dict(_names_cached(str(root), market, tuple(sig)))


def classify(code: str, market: str) -> str:
    """按代码段推断资产类型 (金字塔文件名不带类型)。

    返回值是 TSP 的资产类型集合 ∪ {"bond", "other"}:
        stock / index / etf / lof  → 会被 get_instruments 按类型返回
        bond / other               → 一律不返回 (TSP 没有对应分区)

    **stock 走白名单**, 不做「不像别的就当股票」的兜底。理由: 金字塔 Day
    目录里有国债(019xxx)、可转债(110/113/123/127/128)、国债逆回购(204001)、
    B 股(900/200)、REITs(508)、自编指数(ZSxxxx) —— 一旦默认塞进股票池,
    换手率/市值/筛选/回测全部会被污染, 且是静默错误。宁可漏, 不可错。

    白名单 (2026-11-26 在真实安装上按前缀清点后确认):
        SH  600 601 603 605 688 689              A 股
        SZ  000 001 002 003 300 301 302          A 股
        BJ  43x 83x 87x 92x                      北交所
    其余按下列规则归到 index / etf / lof / bond / other。
    """
    c = code.strip().upper()
    if not c:
        return "other"
    if c.startswith("ZS"):            # 金字塔自编指数 SHZS2000 …
        return "index"
    if not _NUM6.match(c):
        return "other"                # 510300.bak 之类残留文件
    p3, p2 = c[:3], c[:2]

    if market == "SH":
        if p3 in {"600", "601", "603", "605", "688", "689"}:
            return "stock"
        if p3 in {"000", "880", "881", "882", "883", "884", "885", "886",
                  "887", "888", "889"}:
            return "index"            # 上证指数 / 通达信板块指数
        if p3 in {"501", "502", "505", "506"}:
            return "lof"
        if p3 in {"508"}:
            return "other"            # REITs, TSP 无分区
        if p2 in {"50", "51", "52", "53", "55", "56", "58"}:
            return "etf"
        if p2 in {"01", "02", "11", "12", "13", "20"}:
            return "bond"             # 国债/国开/可转债/逆回购
        if p2 == "90":
            return "other"            # B 股, TSP 无分区
        return "other"

    if market == "SZ":
        if p3 in {"000", "001", "002", "003", "300", "301", "302"}:
            return "stock"
        if p3 in {"399", "980", "988"}:
            return "index"            # 深证指数系列 (980001 湾创100 实测)
        if p3 in {"158", "159"}:
            return "etf"              # 158001 价值ETF嘉实 / 159915 创业板ETF
        if p2 == "16":
            return "lof"              # 160-169 场内 LOF (160105 南方积配)
        if p2 == "18":
            return "other"            # 180101 博时蛇口产园REIT 等公募 REITs
        if p2 in {"10", "11", "12", "13"}:
            return "bond"             # 债/逆回购 (131800 Ｒ-003)
        if p2 == "20":
            return "other"            # B 股
        return "other"

    if market == "BJ":
        if p2 in {"43", "83", "87", "92"}:
            return "stock"
        return "other"

    return "other"
