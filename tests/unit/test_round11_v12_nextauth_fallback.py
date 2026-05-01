"""Round 11 V12 P0.1 — fetch_nextauth_backend_access_token 单测.

V11 探活实证 OpenAI ROPC password grant 已撤(30 变体全员 HTTP 400 unknown_parameter:username),
切到 chatgpt.com /api/auth/session accessToken 路径作 V12 fast-path primary。
本测试套件覆盖:
1. 200 + accessToken 字段 → 返回 token
2. 200 缺 accessToken 字段 → None
3. 401/404 → None
4. JSON parse 失败(非 application/json content-type)→ None
5. page.evaluate 抛异常 → None(不 propagate)
6. page=None → None(防御性短路)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from autoteam import codex_auth


def _make_page(evaluate_return):
    page = MagicMock()
    page.evaluate.return_value = evaluate_return
    return page


def test_nextauth_returns_access_token_on_200_with_field():
    page = _make_page({"status": 200, "accessToken": "eyJ.fake.bearer.token.value"})
    token = codex_auth.fetch_nextauth_backend_access_token(page)
    assert token == "eyJ.fake.bearer.token.value"
    page.evaluate.assert_called_once()


def test_nextauth_returns_none_on_200_missing_access_token_field():
    page = _make_page({"status": 200, "accessToken": None})
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_on_200_empty_access_token():
    page = _make_page({"status": 200, "accessToken": ""})
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_on_401():
    page = _make_page({"status": 401, "accessToken": None})
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_on_404():
    page = _make_page({"status": 404, "accessToken": None})
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_on_non_json_content_type():
    # JS 端检测到 content-type 不是 application/json 时,会带 raw='non-json' 标记
    page = _make_page({"status": 200, "accessToken": None, "raw": "non-json"})
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_when_page_evaluate_raises():
    page = MagicMock()
    page.evaluate.side_effect = RuntimeError("page closed")
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_when_page_is_none():
    assert codex_auth.fetch_nextauth_backend_access_token(None) is None


def test_nextauth_returns_none_when_evaluate_returns_non_dict():
    page = _make_page("unexpected string")
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


def test_nextauth_returns_none_when_evaluate_returns_none():
    page = _make_page(None)
    assert codex_auth.fetch_nextauth_backend_access_token(page) is None


# ---------------------------------------------------------------------------
# 集成路径 — 函数公开可见 + 与 fetch_personal_uuid 串联
# ---------------------------------------------------------------------------


def test_nextauth_helper_is_module_level_callable():
    assert callable(codex_auth.fetch_nextauth_backend_access_token)


def test_nextauth_then_fetch_personal_uuid_chained_returns_uuid():
    """集成:NextAuth 拿到 token → fetch_personal_uuid Bearer 路径 → 返回 UUID."""
    from unittest.mock import patch

    page = _make_page({"status": 200, "accessToken": "BEARER_FROM_NEXTAUTH"})
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"id": "ws-personal-uuid-xyz", "structure": "personal", "created": False}
    fake_resp.text = ""

    with patch("requests.post", return_value=fake_resp) as mock_post:
        token = codex_auth.fetch_nextauth_backend_access_token(page)
        assert token == "BEARER_FROM_NEXTAUTH"
        uuid = codex_auth.fetch_personal_uuid(token)
        assert uuid == "ws-personal-uuid-xyz"

    args, kwargs = mock_post.call_args
    assert args[0].endswith("/backend-api/accounts/personal")
    assert kwargs["headers"]["Authorization"] == "Bearer BEARER_FROM_NEXTAUTH"
