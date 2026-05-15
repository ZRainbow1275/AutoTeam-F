import pytest

from autoteam import chatgpt_api, codex_auth, config


class _FakeTransport:
    name = "curl_cffi"

    def __init__(self, responder):
        self._responder = responder
        self.calls = []
        self.closed = False

    def request(self, method, path, *, headers=None, body=None):
        call = {
            "method": method,
            "path": path,
            "headers": headers or {},
            "body": body,
        }
        self.calls.append(call)
        return self._responder(call, len(self.calls))

    def close(self):
        self.closed = True


def test_chatgpt_api_transport_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("CHATGPT_API_TRANSPORT", raising=False)

    assert config.get_chatgpt_api_transport() == "auto"


def test_start_with_session_prefers_curl_cffi_transport(monkeypatch):
    transport = _FakeTransport(
        lambda call, _idx: (
            {"status": 200, "body": '{"accessToken":"tok-1"}'}
            if call["path"] == "/api/auth/session"
            else {"status": 200, "body": '{"workspace_name":"Idapro"}'}
        )
    )
    updates = []

    monkeypatch.setattr(chatgpt_api, "build_chatgpt_transport", lambda **_kwargs: transport)
    monkeypatch.setattr(chatgpt_api, "update_admin_state", lambda **kwargs: updates.append(kwargs))

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(
        client, "_start_browser_session", lambda _session_token: (_ for _ in ()).throw(AssertionError())
    )

    client.start_with_session("session-1", "acc-1")

    assert client.http_transport is transport
    assert client.browser is None
    assert client.access_token == "tok-1"
    assert client.workspace_name == "Idapro"
    assert updates[-1]["workspace_name"] == "Idapro"


def test_start_with_session_cleans_http_transport_when_workspace_update_fails(monkeypatch):
    transport = _FakeTransport(
        lambda call, _idx: (
            {"status": 200, "body": '{"accessToken":"tok-1"}'}
            if call["path"] == "/api/auth/session"
            else {"status": 200, "body": '{"workspace_name":"Idapro"}'}
        )
    )

    monkeypatch.setattr(chatgpt_api, "build_chatgpt_transport", lambda **_kwargs: transport)
    monkeypatch.setattr(
        chatgpt_api,
        "update_admin_state",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("state write failed")),
    )

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(
        client, "_start_browser_session", lambda _session_token: (_ for _ in ()).throw(AssertionError())
    )

    with pytest.raises(RuntimeError, match="state write failed"):
        client.start_with_session("session-1", "acc-1")

    assert transport.closed is True
    assert client.http_transport is None
    assert client.transport_name is None


def test_start_with_session_require_browser_skips_curl_cffi(monkeypatch):
    browser_sessions = []

    monkeypatch.setattr(
        chatgpt_api,
        "build_chatgpt_transport",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not build curl_cffi transport")),
    )

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(client, "_start_browser_session", lambda session_token: browser_sessions.append(session_token))

    client.start_with_session("session-2", "acc-2", require_browser=True)

    assert browser_sessions == ["session-2"]


def test_session_codex_auth_flow_requires_browser_context(monkeypatch):
    start_calls = []

    class _FakePage:
        def goto(self, *_args, **_kwargs):
            return None

    class _FakeContext:
        def new_page(self):
            return _FakePage()

    class _FakeChatGPTTeamAPI:
        def __init__(self):
            self.context = _FakeContext()

        def start_with_session(self, *args, **kwargs):
            start_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(chatgpt_api, "ChatGPTTeamAPI", _FakeChatGPTTeamAPI)
    monkeypatch.setattr(codex_auth.SessionCodexAuthFlow, "_attach_callback_listeners", lambda self: None)
    monkeypatch.setattr(codex_auth.SessionCodexAuthFlow, "_inject_auth_cookies", lambda self: None)
    monkeypatch.setattr(codex_auth.SessionCodexAuthFlow, "_advance", lambda self: {"step": "completed"})
    monkeypatch.setattr(codex_auth.time, "sleep", lambda _seconds: None)

    flow = codex_auth.SessionCodexAuthFlow(
        email="admin@example.com",
        session_token="session-token",
        account_id="account-id",
        workspace_name="workspace",
        password="",
        auth_file_callback=lambda _bundle: "",
    )

    assert flow.start() == {"step": "completed"}
    assert start_calls == [
        {
            "args": ("session-token", "account-id", "workspace"),
            "kwargs": {"require_browser": True},
        }
    ]


def test_api_fetch_falls_back_to_browser_when_curl_cffi_returns_html(monkeypatch):
    transport = _FakeTransport(lambda _call, _idx: {"status": 200, "body": "<!doctype html><html>challenge</html>"})
    ensured = []

    client = chatgpt_api.ChatGPTTeamAPI()
    client.session_token = "session-3"
    client.account_id = "acc-3"
    client.http_transport = transport
    monkeypatch.setattr(client, "_ensure_browser_session", lambda: ensured.append(True))
    monkeypatch.setattr(
        client, "_browser_api_fetch", lambda method, path, body=None: {"status": 200, "body": '{"ok":true}'}
    )

    result = client._api_fetch("GET", "/backend-api/accounts/acc-3/users")

    assert ensured == [True]
    assert result == {"status": 200, "body": '{"ok":true}'}
    assert transport.closed is True
    assert client.http_transport is None


def test_api_fetch_closes_transport_and_falls_back_when_curl_cffi_raises(monkeypatch):
    def fail_request(_call, _idx):
        raise RuntimeError("transport socket reset")

    transport = _FakeTransport(fail_request)
    ensured = []

    client = chatgpt_api.ChatGPTTeamAPI()
    client.session_token = "session-3"
    client.account_id = "acc-3"
    client.http_transport = transport
    monkeypatch.setattr(client, "_ensure_browser_session", lambda: ensured.append(True))
    monkeypatch.setattr(
        client, "_browser_api_fetch", lambda method, path, body=None: {"status": 200, "body": '{"ok":true}'}
    )

    result = client._api_fetch("GET", "/backend-api/accounts/acc-3/users")

    assert ensured == [True]
    assert result == {"status": 200, "body": '{"ok":true}'}
    assert transport.closed is True
    assert client.http_transport is None


def test_direct_api_fetch_refreshes_access_token_before_retry(monkeypatch):
    def responder(call, idx):
        if idx == 1:
            return {"status": 401, "body": '{"detail":{"message":"Unauthorized - Access token is missing"}}'}
        if call["path"] == "/api/auth/session":
            return {"status": 200, "body": '{"accessToken":"tok-2"}'}
        return {"status": 200, "body": '{"items":[]}'}

    transport = _FakeTransport(responder)

    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-4"
    client.session_token = "session-4"
    client.http_transport = transport
    monkeypatch.setattr(client, "_ensure_browser_session", lambda: (_ for _ in ()).throw(AssertionError()))

    result = client._api_fetch("GET", "/backend-api/accounts/acc-4/users")

    assert client.access_token == "tok-2"
    assert result == {"status": 200, "body": '{"items":[]}'}
    assert [call["path"] for call in transport.calls] == [
        "/backend-api/accounts/acc-4/users",
        "/api/auth/session",
        "/backend-api/accounts/acc-4/users",
    ]


def test_direct_api_fetch_falls_back_when_retry_after_refresh_raises(monkeypatch):
    def responder(call, idx):
        if idx == 1:
            return {"status": 401, "body": '{"detail":{"message":"Unauthorized - Access token is missing"}}'}
        if call["path"] == "/api/auth/session":
            return {"status": 200, "body": '{"accessToken":"tok-3"}'}
        raise RuntimeError("retry socket reset")

    transport = _FakeTransport(responder)
    ensured = []

    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-5"
    client.session_token = "session-5"
    client.http_transport = transport
    monkeypatch.setattr(client, "_ensure_browser_session", lambda: ensured.append(True))
    monkeypatch.setattr(
        client, "_browser_api_fetch", lambda method, path, body=None: {"status": 200, "body": '{"ok":true}'}
    )

    result = client._api_fetch("GET", "/backend-api/accounts/acc-5/users")

    assert client.access_token == "tok-3"
    assert ensured == [True]
    assert result == {"status": 200, "body": '{"ok":true}'}
    assert transport.closed is True
    assert client.http_transport is None
    assert [call["path"] for call in transport.calls] == [
        "/backend-api/accounts/acc-5/users",
        "/api/auth/session",
        "/backend-api/accounts/acc-5/users",
    ]


def test_stop_closes_curl_cffi_transport():
    transport = _FakeTransport(lambda _call, _idx: {"status": 200, "body": "{}"})

    client = chatgpt_api.ChatGPTTeamAPI()
    client.http_transport = transport

    client.stop()

    assert transport.closed is True
    assert client.http_transport is None
    assert client.transport_name is None
