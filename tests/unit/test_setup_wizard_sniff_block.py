"""SPEC-1 §5.1 — _verify_cloudmail 嗅探强阻断。

错配场景 → 直接 return False,不实例化 client。
"""

from __future__ import annotations

from autoteam import setup_wizard as mod


class _R:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


def test_verify_cloudmail_aborts_on_mismatch(monkeypatch, caplog):
    """provider=cf_temp_email 但 base_url 是 maillab → return False,不调 login。"""
    monkeypatch.setenv("MAIL_PROVIDER", "cf_temp_email")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "http://m.example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "x")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@a.com")
    monkeypatch.delenv("AUTOTEAM_SKIP_PROVIDER_SNIFF", raising=False)

    def fake_get(url, **kw):
        # /setting/websiteConfig 返 200 含 domainList → maillab 指纹
        if "websiteConfig" in url:
            return _R(200, {"domainList": ["@a.com"]})
        return _R(404)

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    # Sentinel:CloudMailClient 不应被实例化
    instantiated = []

    class _SentinelClient:
        def __init__(self):
            instantiated.append(True)

        def login(self):
            raise AssertionError("login 不应被调用")

    monkeypatch.setattr("autoteam.cloudmail.CloudMailClient", _SentinelClient)

    caplog.set_level("ERROR")
    result = mod._verify_cloudmail()
    assert result is False
    assert "协议错配" in caplog.text
    assert not instantiated, "错配时不应实例化 CloudMailClient"


def test_verify_cloudmail_skip_via_env(monkeypatch):
    """AUTOTEAM_SKIP_PROVIDER_SNIFF=1 时跳过嗅探。"""
    monkeypatch.setenv("MAIL_PROVIDER", "cf_temp_email")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "http://m.example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "x")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@a.com")
    monkeypatch.setenv("AUTOTEAM_SKIP_PROVIDER_SNIFF", "1")

    sniff_called = []
    monkeypatch.setattr(mod, "_sniff_provider_mismatch", lambda p: sniff_called.append(p) or (True, ""))

    # 让后续 client.login() 抛错快速结束(我们只测嗅探被跳过)
    class _BoomClient:
        def login(self):
            raise Exception("stop here")

    monkeypatch.setattr("autoteam.cloudmail.CloudMailClient", lambda: _BoomClient())
    result = mod._verify_cloudmail()
    assert result is False  # login 失败
    assert not sniff_called, "AUTOTEAM_SKIP_PROVIDER_SNIFF=1 时不应调 _sniff_provider_mismatch"


def test_sniff_provider_mismatch_returns_tuple():
    """SPEC §3.4 — _sniff_provider_mismatch 必须返回 (matched, reason)。"""
    # base 为空时直接 return (True, "")
    import os
    os.environ.pop("MAIL_PROVIDER", None)
    os.environ.pop("CLOUDMAIL_BASE_URL", None)
    os.environ.pop("MAILLAB_API_URL", None)
    matched, reason = mod._sniff_provider_mismatch("cf_temp_email")
    assert matched is True
    assert reason == ""
