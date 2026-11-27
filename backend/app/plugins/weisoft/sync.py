"""金字塔历史数据在线同步 —— 下载 → 解压 → 合并进本地 Data 目录。

复刻金字塔「数据补充」的三步流程, 全程命令行, 不依赖 GUI。

服务器 (Setting\\ServerIPAddress.ini 实测):
    FileHistoryServer = 210.14.65.73:7170    ← 文件包 (本模块使用)
    HistoryServer     = 210.14.65.73:5101    ← 实时协议, 非 HTTP

URL 规则 (从 WinStock.exe 提取, 实测 SHA-256 与本地缓存一致):
    http://<server>/FileData/<市场>/<周期>/<YYYYMMDD>.zip

包内结构 (2026-11 用 5 个真实包实测: 顺序解析 endpos 与文件长度精确相等):
    [0..3]    int32  头长度 (实测 100, **不是**首品种偏移)
    [4..53]   GBK    版本横幅, 如 "金字塔决策交易系统(x64) 2025 (V7.20)"
    [54..]    按品种串联, 每个品种:
        char[8]  品种代码 (如 "SH000001" / "SHZS8196")
        uint32   保留 (实测 0)
        uint32   记录数 N
        N × 50   记录区 (格式同 datfile.RECORD_SIZE)
    zip 内可能还有一个 <日期>Ex.dat (附加字段), 本模块忽略。

为什么必须顺序解析:
    品种代码不只有「市场前缀 + 6 位数字」。全量包里有 "SHZS2000" 这类自编
    指数, 正则漏掉它们会把前一个品种的记录区错误延伸到下一个可识别代码,
    单个品种吞掉几千万字节 (20240701 包实测: SH900957 吞了 26.8MB)。
    顺序解析用「记录数 × 50」精确跳步, 不依赖正则。

服务端包分布 (2026-11 实测):
    Day   仅保留 2024-07-01 之后; 2024-07-01 当天是一个 295MB(SH) / 348MB(SZ)
          的全量包, 含 1990 年至今全部品种。之前的日期一律 404。
    Min1  每天一个包, 可回溯到 2010-01-04。
    Min5  每天一个包, 可回溯到 2000-01-04。

合并规则:
    按 OLE 日期去重, 同日期以新数据为准, 结果按日期升序。
    合并后重写文件头 (记录数 / 最后交易日索引), 保留原版本号字节。
"""

from __future__ import annotations

import io
import struct
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.plugins.weisoft.datfile import PERIOD_HEADER, RECORD_SIZE

DEFAULT_SERVER = "210.14.65.73:7170"
# 首个品种代码的偏移: [0..3] 头长度, [4..53] GBK 版本横幅 + 对齐
PKG_FIRST_SYMBOL_OFFSET = 54
# 品种代码市场前缀白名单 (顺序解析时校验, 防止把记录区误当代码)
_MARKET_PREFIXES = frozenset({
    "SH", "SZ", "BJ", "ZJ",
    "CF", "IF", "IH", "IC", "IM", "TF", "TS",
})

# 日线全量包: 该日期之后的包才是增量, 之前不存在
FULL_DAY_PACKAGE = "20240701"


def available_packages(data_root: Path, market: str, period: str) -> list[str]:
    """列出本地 DataCache 里已有的包日期 (升序 YYYYMMDD)。

    金字塔 GUI 的「数据补充」会把下载的 zip 留在 DataCache; 有缓存时
    同步完全不需要联网。
    """
    pdir = data_root.parent / "DataCache" / market / period
    if not pdir.is_dir():
        return []
    return sorted(f.stem for f in pdir.glob("*.zip") if f.stem.isdigit())


class SyncError(RuntimeError):
    """同步明确失败; 调用方不得当成「无数据」处理。"""


@dataclass(frozen=True)
class PackageSymbol:
    code: str          # 包内格式, 如 SH000001
    records: bytes     # N × 50 原始字节

    @property
    def local_code(self) -> str:
        """包内代码 → 本地文件名 (去掉 2 字符市场前缀)。"""
        return self.code[2:]

    @property
    def market(self) -> str:
        return self.code[:2]


def package_url(server: str, market: str, period: str, ymd: str) -> str:
    return f"http://{server}/FileData/{market}/{period}/{ymd}.zip"


def cache_path(data_root: Path, market: str, period: str, ymd: str) -> Path:
    """金字塔自己的下载缓存位置: <安装目录>/DataCache/<市场>/<周期>/<日期>.zip。

    data_root 是 .../Data, 缓存目录与它同级。用户若已在金字塔 GUI 里
    「数据补充」过, 这里就有现成的包, 不必再联网。
    """
    return data_root.parent / "DataCache" / market / period / f"{ymd}.zip"


def fetch_package(server: str, market: str, period: str, ymd: str,
                  timeout: int = 300, data_root: Path | None = None) -> bytes | None:
    """取一个日包: 本地 DataCache 优先, 缺失再联网。

    404 返回 None (该日无包), 其他错误抛 SyncError。
    """
    if data_root is not None:
        cached = cache_path(data_root, market, period, ymd)
        if cached.is_file() and cached.stat().st_size > 0:
            return cached.read_bytes()
    url = package_url(server, market, period, ymd)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise SyncError(f"{url} → HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncError(f"{url} → {exc}") from exc


def _looks_like_symbol(blob: bytes, pos: int) -> str | None:
    """判断 pos 处是否是品种代码区。是则返回代码, 否则 None。"""
    if pos + 16 > len(blob):
        return None
    raw = blob[pos:pos + 8]
    head = raw.rstrip(b"\x00")
    if b"\x00" in head:          # 代码内部出现 NUL → 不是代码
        return None
    try:
        code = head.decode("ascii")
    except UnicodeDecodeError:
        return None
    if len(code) < 4 or code[:2] not in _MARKET_PREFIXES:
        return None
    return code


def parse_package(blob: bytes) -> list[PackageSymbol]:
    """顺序解析 .dat 包, 返回按品种拆分的记录区。

    包内是「代码 + 保留 + 记录数 + N×50」严格串联, 用记录数精确跳步。
    不依赖正则: 全量包里有 SHZS2000 这类非标准代码, 正则漏掉会把前一个
    品种的记录区错误延伸到下一个可识别代码。
    """
    total = len(blob)
    if total < PKG_FIRST_SYMBOL_OFFSET + 16:
        raise SyncError(f"package too small: {total}")

    out: list[PackageSymbol] = []
    pos = PKG_FIRST_SYMBOL_OFFSET
    while pos + 16 <= total:
        code = _looks_like_symbol(blob, pos)
        if code is None:
            # 有产物后遇到不对齐字节 = 包尾填充, 正常收尾;
            # 一开始就不对 = 结构不符, 明确报错而不是静默空数据
            if out:
                break
            raise SyncError(
                f"no symbol at offset {PKG_FIRST_SYMBOL_OFFSET}: "
                f"{blob[PKG_FIRST_SYMBOL_OFFSET:PKG_FIRST_SYMBOL_OFFSET + 16]!r}"
            )
        (_reserved, count) = struct.unpack_from("<II", blob, pos + 8)
        data_start = pos + 16
        data_end = data_start + count * RECORD_SIZE
        if data_end > total:
            raise SyncError(
                f"package truncated at {code}: need {data_end} bytes, have {total}"
            )
        if count:
            out.append(PackageSymbol(code=code, records=blob[data_start:data_end]))
        pos = data_end

    if not out:
        raise SyncError("no symbol records found in package")
    return out


def read_zip(blob: bytes) -> bytes:
    """从 zip 字节里取出主 .dat (忽略 *Ex.dat)。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".dat")]
        if not names:
            raise SyncError("zip 内没有 .dat")
        # 主文件是名字最短的那个 (附加文件带 Ex 后缀且更长)
        main = min(names, key=len)
        return zf.read(main)


# ---------------------------------------------------------------- 本地文件读写


def read_local(path: Path, header_size: int) -> tuple[bytes, list[tuple[float, bytes]]]:
    """读本地行情文件, 返回 (原文件头, [(OLE 日期, 50 字节记录)])。"""
    if not path.exists():
        return b"", []
    blob = path.read_bytes()
    if len(blob) <= header_size:
        return blob[:header_size], []
    head = blob[:header_size]
    body = blob[header_size:]
    n = len(body) // RECORD_SIZE
    recs: list[tuple[float, bytes]] = []
    for i in range(n):
        off = i * RECORD_SIZE
        (d,) = struct.unpack_from("<d", body, off)
        recs.append((d, body[off:off + RECORD_SIZE]))
    return head, recs


def records_from_bytes(raw: bytes) -> list[tuple[float, bytes]]:
    out: list[tuple[float, bytes]] = []
    for i in range(len(raw) // RECORD_SIZE):
        off = i * RECORD_SIZE
        (d,) = struct.unpack_from("<d", raw, off)
        out.append((d, raw[off:off + RECORD_SIZE]))
    return out


def merge_records(
    old: list[tuple[float, bytes]], new: list[tuple[float, bytes]],
) -> tuple[list[tuple[float, bytes]], int, int]:
    """按日期合并, 同日期以新为准。返回 (合并结果, 新增条数, 覆盖条数)。"""
    table: dict[float, bytes] = {d: rec for d, rec in old}
    added = replaced = 0
    for d, rec in new:
        prev = table.get(d)
        if prev is None:
            table[d] = rec
            added += 1
        else:
            if prev != rec:
                replaced += 1
            table[d] = rec
    return sorted(table.items(), key=lambda kv: kv[0]), added, replaced


def build_header(period: str, records: list[tuple[float, bytes]],
                 template: bytes = b"") -> bytes:
    """构造/更新文件头。保留模板里的版本号等未知字节。"""
    size = PERIOD_HEADER.get(period, 16)
    if template and len(template) >= size:
        head = bytearray(template[:size])
    else:
        head = bytearray(size)
        if period != "Day" and size >= 12:
            struct.pack_into("<f", head, 8, 7.2073)
    if not records:
        return bytes(head)

    count = len(records)
    last_day = int(records[-1][0])
    if period == "Day":
        struct.pack_into("<d", head, 0, float(last_day))
        struct.pack_into("<i", head, 8, count)
    else:
        struct.pack_into("<i", head, 0, count)
        struct.pack_into("<i", head, 4, 0)
        first_idx = count - 1
        for i in range(count - 1, -1, -1):
            if int(records[i][0]) == last_day:
                first_idx = i
            else:
                break
        struct.pack_into("<i", head, 12, first_idx)
    return bytes(head)


def write_local(path: Path, period: str,
                records: list[tuple[float, bytes]], template: bytes = b"") -> None:
    head = build_header(period, records, template)
    body = b"".join(rec for _, rec in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + body)


# ---------------------------------------------------------------- 高层动作


@dataclass
class MergeResult:
    code: str
    before: int
    after: int
    added: int
    replaced: int

    @property
    def delta(self) -> int:
        return self.after - self.before


def sync_one_day(
    data_root: Path,
    market: str,
    period: str,
    ymd: str,
    *,
    server: str = DEFAULT_SERVER,
    codes: set[str] | None = None,
    dry_run: bool = False,
    use_cache: bool = True,
) -> list[MergeResult]:
    """同步一天: 下载 → 解析 → 合并。返回每个品种的合并结果。

    codes 为本地 6 位代码集合 (如 {"510300"}); None 表示包内全部品种。
    该日无包时返回空列表。
    """
    blob = fetch_package(server, market, period, ymd,
                         data_root=data_root if use_cache else None)
    if blob is None:
        return []
    raw = read_zip(blob)
    symbols = parse_package(raw)

    header_size = PERIOD_HEADER.get(period, 16)
    out: list[MergeResult] = []
    for sym in symbols:
        if sym.market != market:
            continue
        if codes is not None and sym.local_code not in codes:
            continue
        path = data_root / market / period / sym.local_code
        old_head, old_recs = read_local(path, header_size)
        new_recs = records_from_bytes(sym.records)
        merged, added, replaced = merge_records(old_recs, new_recs)
        out.append(MergeResult(code=sym.local_code, before=len(old_recs),
                               after=len(merged), added=added, replaced=replaced))
        if not dry_run:
            write_local(path, period, merged, old_head)
    return out


def sync_range(
    data_root: Path,
    market: str,
    period: str,
    start_ymd: str,
    end_ymd: str,
    *,
    server: str = DEFAULT_SERVER,
    codes: set[str] | None = None,
    dry_run: bool = False,
    use_cache: bool = True,
    on_day_done=None,
) -> dict[str, list[MergeResult]]:
    """同步日期区间。返回 {日期: 合并结果}。无包的日子跳过。"""
    out: dict[str, list[MergeResult]] = {}
    for ymd in iter_trade_days(start_ymd, end_ymd):
        try:
            res = sync_one_day(data_root, market, period, ymd,
                               server=server, codes=codes, dry_run=dry_run,
                               use_cache=use_cache)
        except SyncError:
            if on_day_done:
                on_day_done(ymd, None)
            continue
        if res:
            out[ymd] = res
        if on_day_done:
            on_day_done(ymd, res)
    return out


def iter_trade_days(start_ymd: str, end_ymd: str):
    """按日历日遍历 (周末跳过; 节假日由 404 快速跳过)。"""
    import datetime as _dt

    d = _dt.datetime.strptime(start_ymd, "%Y%m%d").date()
    end = _dt.datetime.strptime(end_ymd, "%Y%m%d").date()
    while d <= end:
        if d.weekday() < 5:
            yield d.strftime("%Y%m%d")
        d += _dt.timedelta(days=1)
