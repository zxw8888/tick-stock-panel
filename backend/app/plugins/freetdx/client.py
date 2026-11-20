# freetdx — 通达信公网行情服务器的免费 pytdx 连接池。
#
# 背景(2026-11 实测): 通达信公网 hq 服务器对 get_security_bars / get_security_quotes /
# get_security_list / transaction 等命令返回 2 字节拒绝码(0x0320=800), pytdx 解析为
# "ret_count=800" 然后抛 "unpack requires a buffer of 4 bytes" 并静默返回 None;
# 而 get_history_minute_time_data(历史分时, 含当日)、get_finance_info、get_xdxr_info
# 仍然开放。因此本源用「分时转 1 分钟K」而不是 bars 拉分钟K, 单位与价格口径
# 已与腾讯快照交叉验证(price 为绝对价, vol 为手)。
#
# 服务器池: 常用公网 hq 服务器, 连接失败/调用异常自动轮换下一台。
# 线程模型: N 条 TCP 连接放入队列, worker 线程从队列借还, 每条连接自带节流。

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from queue import Empty, Queue

logger = logging.getLogger(__name__)

# 通达信公网行情服务器池 (2026-11 实测连通性: 前几台可用; 119/106 等老 IP 已超时)
SERVER_POOL: tuple[tuple[str, int], ...] = (
    ("218.75.126.9", 7709),
    ("115.238.56.198", 7709),
    ("124.71.187.122", 7709),
    ("110.41.147.114", 7709),
    ("111.229.247.189", 7709),
    ("180.153.18.170", 7709),
)

CONNECT_TIMEOUT_S = 3
DEFAULT_POOL_SIZE = 4
# 每条连接两次请求的最小间隔 — 通达信对 bars 类高频请求限流(实测快速连发即触发
# 拒绝码), 分时接口宽容些但统一保守节流; 多连接并发由池提供吞吐。
DEFAULT_INTERVAL_S = 0.12

_CODE_MAP_INDEX_HINT = ("000001.SH",)  # 上证指数走通达信 999999 别名
_INDEX_ALIAS = {"000001.SH": (1, "999999")}


def to_tdx_code(symbol: str) -> tuple[int, str] | None:
    """内部代码 `600519.SH` → 通达信 (market, code)。

    - .SH → market 1, .SZ → market 0; ETF/指数同映射。
    - 上证指数 000001.SH 映射到通达信别名 999999; 399xxx(深指数)直通。
    - 北交所(4/8/92 开头)与未知后缀不在公网 hq 覆盖内 → None (上层回空帧)。
    """
    if not symbol or "." not in symbol:
        return None
    code, _, suffix = symbol.partition(".")
    suffix = suffix.upper()
    if suffix == "SH":
        market = 1
    elif suffix == "SZ":
        market = 0
    else:
        return None
    if not code.isdigit() or len(code) != 6:
        return None
    if code[0] in ("4", "8") or code.startswith("92"):
        return None  # 北交所/老三板
    if symbol in _INDEX_ALIAS:
        return _INDEX_ALIAS[symbol]
    return (market, code)


class TdxConn:
    """单条通达信 TCP 连接: 懒连接、节流、异常自动换服务器重连(重试一次)。"""

    def __init__(self, pool_ref: "TdxPool") -> None:
        self._pool = pool_ref
        self._api = None
        self._server_idx = 0
        self._last_call = 0.0
        self._lock = threading.Lock()

    # -- 连接管理 ---------------------------------------------------------
    def _connect(self) -> None:
        from pytdx.hq import TdxHq_API

        pool = SERVER_POOL
        errors: list[str] = []
        for step in range(len(pool)):
            idx = (self._server_idx + step) % len(pool)
            ip, port = pool[idx]
            api = TdxHq_API()
            try:
                if api.connect(ip, port, time_out=CONNECT_TIMEOUT_S):
                    self._api = api
                    self._server_idx = (idx + 1) % len(pool)  # 下次从下一台开始
                    logger.info("freetdx 已连接 %s:%s", ip, port)
                    return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{ip}:{type(exc).__name__}")
        self._api = None
        raise ConnectionError(f"freetdx 全部服务器不可达: {errors}")

    def _ensure(self) -> None:
        if self._api is None:
            self._connect()

    def close(self) -> None:
        with self._lock:
            if self._api is not None:
                with contextlib_suppress():
                    self._api.disconnect()
                self._api = None

    # -- 调用 -------------------------------------------------------------
    def call(self, fn_name: str, *args, retries: int = 2):
        """调用 pytdx API; 节流 + 失败换服务器重试。返回 None 表示服务器拒绝/无数据。"""
        with self._lock:
            wait = DEFAULT_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            last_err: Exception | None = None
            for _ in range(max(1, retries)):
                try:
                    self._ensure()
                    self._last_call = time.monotonic()
                    return getattr(self._api, fn_name)(*args)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    # 断线/协议异常: 丢弃连接, 下一轮重连换服务器
                    with contextlib_suppress():
                        self._api.disconnect()
                    self._api = None
            logger.warning("freetdx %s%s 失败: %s", fn_name, args[:2], last_err)
            return None


class contextlib_suppress:
    """微型 contextlib.suppress 替代, 避免 import 噪音。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


class TdxPool:
    """N 条连接的借还池; map() 对一批标的做多连接并发拉取。"""

    def __init__(self, size: int = DEFAULT_POOL_SIZE) -> None:
        self._size = max(1, int(size))
        self._queue: Queue[TdxConn] = Queue()
        self._all: list[TdxConn] = []
        self._closed = False
        for _ in range(self._size):
            conn = TdxConn(self)
            self._all.append(conn)
            self._queue.put(conn)

    @property
    def size(self) -> int:
        return self._size

    def borrow(self, timeout: float = 30.0) -> TdxConn:
        return self._queue.get(timeout=timeout)

    def give_back(self, conn: TdxConn) -> None:
        if not self._closed:
            self._queue.put(conn)

    def call(self, fn_name: str, *args, retries: int = 2):
        """单次调用: 借一条连接执行后归还。"""
        conn = self.borrow()
        try:
            return conn.call(fn_name, *args, retries=retries)
        finally:
            self.give_back(conn)

    def map(
        self,
        fn: Callable[[TdxConn, object], object],
        items: list,
        on_progress: Callable[[int, int], None] | None = None,
        stop_on_exc: bool = False,
    ) -> Iterator[tuple[object, object]]:
        """并发消费 items: 每个任务借连接执行 fn(conn, item), yield (item, result)。

        fn 抛异常时: stop_on_exc=True 则中止整批(向上传播), 否则记日志 result=None 继续。
        on_progress(done, total) 覆盖失败批次, 可重复调用作为心跳。
        """
        from concurrent.futures import ThreadPoolExecutor

        total = len(items)
        if total == 0:
            if on_progress:
                on_progress(0, 0)
            return

        def _one(item):
            conn = self.borrow()
            try:
                return fn(conn, item)
            finally:
                self.give_back(conn)

        done = 0
        with ThreadPoolExecutor(max_workers=self._size) as pool:
            futures = []
            for item in items:
                futures.append((item, pool.submit(_one, item)))
            for item, fut in futures:
                done += 1
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    if stop_on_exc:
                        # 取消剩余任务后向上传播
                        for _, f2 in futures:
                            f2.cancel()
                        raise
                    logger.warning("freetdx map %s 失败: %s", item, exc)
                    result = None
                if on_progress:
                    on_progress(done, total)
                yield item, result

    def close(self) -> None:
        self._closed = True
        for conn in self._all:
            conn.close()
