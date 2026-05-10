"""SimpleLogin provider 单元测试 — 全 mock requests,无真实 HTTP。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from autoteam.mail import simplelogin as mod
from autoteam.mail.fallback import MailProviderUnavailable


def _make_response(status: int, body: dict | None = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text or ("" if body is None else "{}")
    r.json = MagicMock(return_value=body if body is not None else {})
    return r


def _make_client(monkeypatch) -> mod.SimpleLoginClient:
    monkeypatch.setenv("SIMPLELOGIN_API_KEY", "key-abc")
    monkeypatch.setenv("SIMPLELOGIN_BASE_URL", "https://sl.example.com")
    monkeypatch.setenv("SIMPLELOGIN_HOSTNAME", "openai.com")
    client = mod.SimpleLoginClient()
    client.session = MagicMock()
    return client


def test_init_missing_api_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("SIMPLELOGIN_API_KEY", raising=False)
    with pytest.raises(MailProviderUnavailable) as exc:
        mod.SimpleLoginClient()
    assert "SIMPLELOGIN_API_KEY" in str(exc.value)


def test_init_uses_default_base_url_when_unset(monkeypatch):
    monkeypatch.setenv("SIMPLELOGIN_API_KEY", "key-abc")
    monkeypatch.delenv("SIMPLELOGIN_BASE_URL", raising=False)
    client = mod.SimpleLoginClient()
    assert client.base_url == mod.DEFAULT_BASE_URL


def test_headers_use_simplelogin_authentication_header(monkeypatch):
    client = _make_client(monkeypatch)
    h = client._headers()
    assert h["Authentication"] == "key-abc"
    assert "Authorization" not in h
    assert h["Accept"] == "application/json"


def test_login_calls_user_info(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(200, {"email": "me@example.com"})
    out = client.login()
    assert out == "key-abc"
    args, _ = client.session.request.call_args
    assert args[0] == "GET"
    assert "/api/user_info" in args[1]


def test_login_auth_failure_raises(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(401, text="bad key")
    with pytest.raises(Exception, match="401"):
        client.login()


def test_create_temp_email_random_alias(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(
        201, {"id": 42, "alias": "auto.xyz@aleeas.com"}
    )
    aid, email = client.create_temp_email(prefix="autoteam")
    assert aid == 42
    assert email == "auto.xyz@aleeas.com"

    args, kw = client.session.request.call_args
    assert args[0] == "POST"
    assert "/api/alias/random/new" in args[1]
    assert kw["params"] == {"hostname": "openai.com"}
    assert "note" in kw["json"]


def test_create_temp_email_response_missing_fields_raises(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(201, {"id": 99})
    with pytest.raises(Exception, match="缺"):
        client.create_temp_email()


def test_list_accounts_paginates_and_stops_when_empty(monkeypatch):
    client = _make_client(monkeypatch)
    page0 = _make_response(
        200,
        {"aliases": [
            {"id": 1, "email": "a@x.com"},
            {"id": 2, "email": "b@x.com"},
        ]},
    )
    page1 = _make_response(200, {"aliases": []})
    client.session.request.side_effect = [page0, page1]
    rows = client.list_accounts(size=100)
    assert len(rows) == 2
    assert rows[0]["accountId"] == 1
    assert rows[0]["email"] == "a@x.com"
    # 翻了 2 页
    assert client.session.request.call_count == 2


def test_list_accounts_stops_at_target_size(monkeypatch):
    client = _make_client(monkeypatch)
    page0 = _make_response(
        200,
        {"aliases": [
            {"id": i, "email": f"x{i}@x.com"} for i in range(20)
        ]},
    )
    client.session.request.return_value = page0
    rows = client.list_accounts(size=5)
    # 单页就够,目标 5 条命中后立即停
    assert len(rows) == 5


def test_delete_account_by_id(monkeypatch):
    client = _make_client(monkeypatch)
    client.session.request.return_value = _make_response(204)
    res = client.delete_account(42)
    assert res["code"] == 200
    args, _ = client.session.request.call_args
    assert args[0] == "DELETE"
    assert "/api/aliases/42" in args[1]


def test_delete_account_by_email_resolves_id(monkeypatch):
    client = _make_client(monkeypatch)
    list_resp = _make_response(
        200,
        {"aliases": [{"id": 7, "email": "match@x.com"}]},
    )
    page2 = _make_response(200, {"aliases": []})
    del_resp = _make_response(204)
    client.session.request.side_effect = [list_resp, page2, del_resp]
    res = client.delete_account("match@x.com")
    assert res["code"] == 200
    last_args = client.session.request.call_args_list[-1]
    assert "/api/aliases/7" in last_args.args[1]


def test_inbox_methods_return_empty_with_warning(monkeypatch, caplog):
    client = _make_client(monkeypatch)
    with caplog.at_level("WARNING", logger="autoteam.mail.simplelogin"):
        assert client.search_emails_by_recipient("foo@x.com") == []
        assert client.list_emails(7) == []
        assert client.delete_emails_for("foo@x.com") == 0
    msgs = " ".join(r.message for r in caplog.records)
    assert "inbox" in msgs.lower() or "alias forwarding" in msgs.lower()


def test_implements_full_mail_provider_abc(monkeypatch):
    from autoteam.mail.base import MailProvider

    client = _make_client(monkeypatch)
    assert isinstance(client, MailProvider)
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
