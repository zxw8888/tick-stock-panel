"""东方财富 HTTP 请求头 (可选登录态 Cookie 注入)。

东财 push2 / push2his / fund 接口本身是公开端点, 不带 Cookie 也能取数;
但匿名高频访问更容易触发限流 (429 / 空 data), 登录态 Cookie 能显著降低
触发概率。Cookie 统一存 ``data/user_data/secrets.json`` 的
``eastmoney_cookie`` 字段, 设置页「数据源 → 东方财富 Cookie」可直接维护。

设计约束:
  - 未配置时绝不发送 Cookie 头, 保持匿名路径与旧行为一致;
  - Cookie 值只来自本地 secrets, 不接受请求参数直接透传, 避免 SSRF/头注入;
  - Referer 固定为东财行情页, 与浏览器登录态来源一致。
"""
from __future__ import annotations

from app import secrets_store

EASTMONEY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
EASTMONEY_REFERER = "https://quote.eastmoney.com/"


def cookie() -> str:
    """当前配置的东财 Cookie; 未配置返回空串。"""
    return secrets_store.get_eastmoney_cookie()


def headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """东财请求头。extra 中的同名键优先 (大小写敏感, 调用方按需覆盖)。"""
    out = {
        "User-Agent": EASTMONEY_UA,
        "Referer": EASTMONEY_REFERER,
        "Accept": "*/*",
    }
    value = cookie()
    if value:
        out["Cookie"] = value
    if extra:
        out.update(extra)
    return out
