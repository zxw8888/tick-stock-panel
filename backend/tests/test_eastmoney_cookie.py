"""东方财富 Cookie 配置契约。

锁定四件事:
① secrets.json 读写 round-trip, 换行被剔除 (HTTP 头注入防护);
② 未配置时不发送 Cookie 头, 配置后所有东财请求头自动携带;
③ /api/settings 暴露 has/masked, 绝不回明文;
④ POST/DELETE 端点可保存与清除。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import secrets_store
from app.services import eastmoney_http


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    yield tmp_path


def test_cookie_roundtrip_strips_newlines(data_dir: Path) -> None:
    value = secrets_store.set_eastmoney_cookie("a=1; b=2\r\nX-Injected: 1")

    assert value == "a=1; b=2X-Injected: 1"
    assert secrets_store.get_eastmoney_cookie() == value
    assert "X-Injected" in value  # 换行被删, 不会变成独立请求头


def test_headers_only_include_cookie_when_configured(data_dir: Path) -> None:
    assert "Cookie" not in eastmoney_http.headers()

    secrets_store.set_eastmoney_cookie("qgqp_b_id=abc; ut=xyz")
    headers = eastmoney_http.headers({"Referer": "https://example.test/"})

    assert headers["Cookie"] == "qgqp_b_id=abc; ut=xyz"
    assert headers["Referer"] == "https://example.test/"  # extra 覆盖默认值
    assert "Mozilla" in headers["User-Agent"]


def test_settings_endpoints_save_and_clear_cookie(data_dir: Path) -> None:
    from app.api import settings as settings_api

    saved = settings_api.save_eastmoney_cookie(
        settings_api.EastmoneyCookieIn(cookie="qgqp_b_id=abc; ut=xyz"),
    )
    assert saved["ok"] is True
    assert saved["has_eastmoney_cookie"] is True
    assert saved["eastmoney_cookie_masked"] == "•" * 6
    assert "abc" not in saved["eastmoney_cookie_masked"]

    payload = settings_api.get_settings()
    assert payload["has_eastmoney_cookie"] is True
    assert payload["eastmoney_cookie_masked"] == "•" * 6

    cleared = settings_api.clear_eastmoney_cookie()
    assert cleared["ok"] is True
    assert cleared["has_eastmoney_cookie"] is False
    assert secrets_store.get_eastmoney_cookie() == ""


def test_settings_endpoint_rejects_empty_cookie(data_dir: Path) -> None:
    from app.api import settings as settings_api

    result = settings_api.save_eastmoney_cookie(settings_api.EastmoneyCookieIn(cookie="  \r\n "))
    assert result == {"ok": False, "error": "cookie empty"}
