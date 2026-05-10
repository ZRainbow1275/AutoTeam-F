"""Addy.io provider 单元测试 — 全 mock requests,无真实 HTTP。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from autoteam.mail import addy_io as mod
from autoteam.mail.fallback import MailProviderUnavailable


def _set_env(monkeypatch, base="https://addy.example.com", token="tok-abc", domain="example.com"):
    monkeypatch.setenv("ADDY_IO_BASE_URL", base)
    monkeypatch.setenv("ADDY_IO_TOKEN", token)
    monkeypatch.setenv("ADDY_IO_DOMAIN", domain)


def _make_response(status: int, body: dict | None = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text or ("" if body is None else "{}")
    r.json = MagicMock(return_value=body if body is not None else {})
    return r


def _make_client(monkeypatch) -> mod.AddyIoClient:
    _set_env(monkeypatch)
    client = mod.AddyIoClient()
    client.session = MagicMock()
    return client


def test_init_missing_env_raises_unavailable(monkeypatch):
    monkeypatch.delenv("ADDY_IO_BASE_URL", raising=False)
    monkeypatch.delenv("ADDY_IO_TOKEN", raising=False)
    monkeypatch.delenv("ADDY_IO_DOMAIN", raising=False)
    with pytest.raises(MailProviderUnavailable) as exc:
        mod.AddyIoClient()
    msg = str(exc.value)
    assert "ADDY_IO_BASE_URL" in msg
    assert "ADDY_IO_TOKEN" in msg
    assert "ADDY_IO_DOMAIN" in msg


def test_init_partial_env_raises_unavailable(monkeypatch):
    monkeypatch.setenv("ADDY_IO_BASE_URL", "https://addy.example.com")
    monkeypatch.delenv("ADDY_IO_TOKEN", raising=False)
    monkeypatch.delenv("ADDY_IO_DOMAIN", raising=False)
    with pytest.raises(MailProviderUnavailable):
        mod.AddyIoClient()


def test_init_strips_trailing_slash_and_at_sign(monkeypatch):
    monkeypatch.setenv("ADDY_IO_BASE_URL", "https://addy.example.com/")
    monkeypatch.setenv("ADDY_IO_TOKEN", "tok-abc")
    monkeypatch.setenv("ADDY_IO_DOMAIN", "@example.com")
    client = mod.AddyIoClient()
    assert client.base_url == "https://addy.example.com"
    assert client.domain == "example.com"


def test_headers_use_bearer_token_and_required_headers(monkeypatch):
    client = _make_client(monkeypatch)
    h = client._headers()
    assert h["Authorization"] == "Bearer tok-abc"
    assert h["Accept"] == "application/json"
    assert h["X-Requested-With"] == "XMLHttpRequest"
    assert h["Content-Type"] == "application/json"


def test_login_calls_aliases_endpoint(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(200, {"data": []})
    token = client.login()
    assert token == "tok-abc"
    args, kw = client.session.request.call_args
    assert args[0] == "GET"
    assert "/api/v1/aliases" in args[1]
    assert kw["params"] == {"page[size]": 1}


def test_login_auth_failure_raises(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(401, text="unauthorized")
    with pytest.raises(Exception) as exc:
        client.login()
    assert "401" in str(exc.value)


def test_create_temp_email_with_prefix_uses_custom_format(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(
        201,
        {"data": {"id": "alias-123", "email": "myprefix@example.com"}},
    )
    aid, email = client.create_temp_email(prefix="myprefix")
    assert aid == "alias-123"
    assert email == "myprefix@example.com"

    args, kw = client.session.request.call_args
    assert args[0] == "POST"
    body = kw["json"]
    assert body["domain"] == "example.com"
    assert body["format"] == mod.ALIAS_FORMAT_CUSTOM
    assert body["local_part"] == "myprefix"


def test_create_temp_email_without_prefix_uses_uuid_format(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(
        201,
        {"data": {"id": "alias-xyz", "email": "abc123@example.com"}},
    )
    aid, email = client.create_temp_email()
    assert aid == "alias-xyz"
    assert email == "abc123@example.com"

    body = client.session.request.call_args.kwargs["json"]
    assert body["format"] == mod.ALIAS_FORMAT_UUID
    assert "local_part" not in body


def test_create_temp_email_response_missing_fields_raises(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(201, {"data": {}})
    with pytest.raises(Exception) as exc:
        client.create_temp_email(prefix="x")
    assert "缺 id/email" in str(exc.value) or "缺" in str(exc.value)


def test_list_accounts_normalizes_rows(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(
        200,
        {
            "data": [
                {"id": "a-1", "email": "one@example.com", "active": True, "description": "d1"},
                {"id": "a-2", "email": "two@example.com", "active": False, "description": "d2"},
            ]
        },
    )
    rows = client.list_accounts(size=10)
    assert len(rows) == 2
    assert rows[0]["accountId"] == "a-1"
    assert rows[0]["email"] == "one@example.com"
    assert rows[0]["active"] is True
    assert rows[1]["active"] is False


def test_list_accounts_handles_missing_data_field(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(200, {})
    rows = client.list_accounts(size=10)
    assert rows == []


def test_delete_account_by_id(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(204)
    res = client.delete_account("alias-123")
    assert res["code"] == 200
    args, _ = client.session.request.call_args
    assert args[0] == "DELETE"
    assert "/api/v1/aliases/alias-123" in args[1]


def test_delete_account_by_email_resolves_id(monkeypatch):
    client = _make_client(monkeypatch)
    # 第一次:list_accounts;第二次:DELETE
    list_resp = _make_response(
        200,
        {"data": [{"id": "alias-x", "email": "match@example.com"}]},
    )
    del_resp = _make_response(204)
    client.session.request.side_effect = [list_resp, del_resp]
    res = client.delete_account("match@example.com")
    assert res["code"] == 200
    # 第二次调用(DELETE)的 path 应包含 alias-x
    last_args = client.session.request.call_args_list[-1]
    assert "/api/v1/aliases/alias-x" in last_args.args[1]


def test_delete_account_unknown_email_returns_404(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(200, {"data": []})
    res = client.delete_account("none@example.com")
    assert res["code"] == 404


def test_inbox_methods_return_empty_with_warning(monkeypatch, caplog):
    client = _make_client(monkeypatch)
    with caplog.at_level("WARNING", logger="autoteam.mail.addy_io"):
        assert client.search_emails_by_recipient("foo@example.com") == []
        assert client.list_emails("alias-x") == []
        assert client.delete_emails_for("foo@example.com") == 0
    msgs = " ".join(r.message for r in caplog.records)
    assert "alias forwarding" in msgs.lower() or "inbox" in msgs.lower()


def test_sanitize_local_part_filters_special_chars():
    assert mod.AddyIoClient._sanitize_local_part("hello@world!") == "helloworld"
    out = mod.AddyIoClient._sanitize_local_part(None)
    assert len(out) == 10  # uuid hex truncated
    assert mod.AddyIoClient._sanitize_local_part("---") != ""


def test_implements_full_mail_provider_abc(monkeypatch):
    """ABC 接口完整性自检。"""
    from autoteam.mail.base import MailProvider

    client = _make_client(monkeypatch)
    assert isinstance(client, MailProvider)
    # 确保所有抽象方法都被覆盖
    for name in (
        "login",
        "create_temp_email",
        "list_accounts",
        "delete_account",
        "search_emails_by_recipient",
        "list_emails",
        "delete_emails_for",
    ):
        assert callable(getattr(client, name))
