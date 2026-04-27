"""Round 8 — OAuth Personal Workspace 显式选择单测。

覆盖 spec `prompts/0426/spec/shared/oauth-workspace-selection.md` 关键路径:
  W-I1 三层兜底次序(decode → primary HTTP → UI fallback)
  W-I2 OAUTH_WS_NO_PERSONAL fail-fast 不重试
  W-I4 personal 三条件 OR 识别(structure / plan_type=free / is_personal)
  W-I6 evidence 不含 token / cookie 原值
  W-I7 不存在 time.sleep(8) 残留
"""
from __future__ import annotations

import base64
import json


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, _url=None):
        return self._cookies


class _FakePage:
    """模拟 Playwright Page — 支持 cookies、evaluate、locator、goto。"""

    def __init__(self, cookies=None, evaluate_result=None, evaluate_exc=None,
                 url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                 locator_visible_count=0, goto_exc=None,
                 inner_text_value="Personal workspace",
                 page_title_value="OpenAI"):
        self.context = _FakeContext(cookies or [])
        self._evaluate_result = evaluate_result
        self._evaluate_exc = evaluate_exc
        self.url = url
        self._locator_visible_count = locator_visible_count
        self._goto_exc = goto_exc
        self._inner_text = inner_text_value
        self._title = page_title_value
        self.evaluate_calls = []
        self.goto_calls = []

    def evaluate(self, js, args=None):
        self.evaluate_calls.append((js[:50], args))
        if self._evaluate_exc:
            raise self._evaluate_exc
        return self._evaluate_result

    def goto(self, url, **kw):
        self.goto_calls.append(url)
        if self._goto_exc:
            raise self._goto_exc

    def inner_text(self, _sel, timeout=None):
        return self._inner_text

    def content(self):
        return self._inner_text

    def title(self):
        return self._title

    def locator(self, _sel):
        # 返回一个 mock locator,模拟 personal 按钮可见 + 点击成功
        page = self

        class _Loc:
            def __init__(self):
                self._first_called = False

            @property
            def first(self):
                if self._first_called:
                    return self
                self._first_called = True
                return self

            def is_visible(self, timeout=None):
                # 通过递减计数模拟前 N 个 selector 不可见、第 N+1 个可见
                page._locator_visible_count -= 1
                return page._locator_visible_count <= 0

            def inner_text(self, timeout=None):
                return "Personal account"

            def click(self, **kw):
                pass

        return _Loc()


def _make_jwt_session(workspaces):
    """构造 oai-oauth-session cookie value(JWT-like 三段格式,首段为 base64url payload)。

    实施按 spec §2.2.1 规则:含 "." → 取首段 base64url decode,所以将 payload 放首段。
    """
    payload = {"workspaces": workspaces, "user": {"email": "x@y.com"}}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{body}.sig.tail"


def _make_plain_session(workspaces):
    """构造非 JWT 的 base64url 整段。"""
    payload = {"workspaces": workspaces}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_decode_oauth_session_cookie_jwt_format():
    """spec §2.2.1 — JWT 三段格式取首段 base64url decode。"""
    from autoteam.oauth_workspace import decode_oauth_session_cookie

    workspaces = [{"id": "ws-1", "structure": "personal", "plan_type": "free"}]
    val = _make_jwt_session(workspaces)
    page = _FakePage(cookies=[{"name": "oai-oauth-session", "value": val}])

    result = decode_oauth_session_cookie(page)
    assert result is not None
    assert result["workspaces"][0]["id"] == "ws-1"


def test_decode_oauth_session_cookie_plain_b64():
    """非 JWT 格式整段 b64url decode 也应成功。"""
    from autoteam.oauth_workspace import decode_oauth_session_cookie

    workspaces = [{"id": "ws-2", "structure": "personal"}]
    val = _make_plain_session(workspaces)
    page = _FakePage(cookies=[{"name": "oai-oauth-session", "value": val}])

    result = decode_oauth_session_cookie(page)
    assert result is not None
    assert result["workspaces"][0]["id"] == "ws-2"


def test_decode_oauth_session_cookie_alt_name():
    """W-I1 alt — 兼容 oai-client-auth-session 这个 alt cookie name。"""
    from autoteam.oauth_workspace import decode_oauth_session_cookie

    workspaces = [{"id": "ws-3", "structure": "personal"}]
    val = _make_plain_session(workspaces)
    page = _FakePage(cookies=[{"name": "oai-client-auth-session", "value": val}])

    result = decode_oauth_session_cookie(page)
    assert result is not None and result["workspaces"][0]["id"] == "ws-3"


def test_decode_oauth_session_cookie_missing_returns_none():
    """没有目标 cookie 时应返回 None,不抛异常。"""
    from autoteam.oauth_workspace import decode_oauth_session_cookie

    page = _FakePage(cookies=[{"name": "other-cookie", "value": "x"}])
    assert decode_oauth_session_cookie(page) is None


def test_decode_oauth_session_cookie_corrupt_value_returns_none():
    """value 不可解码时应返回 None,不抛异常。"""
    from autoteam.oauth_workspace import decode_oauth_session_cookie

    page = _FakePage(cookies=[{"name": "oai-oauth-session", "value": "!@#$%^&*"}])
    assert decode_oauth_session_cookie(page) is None


def test_is_personal_workspace_three_or_conditions():
    """W-I4 — structure / plan_type=free / is_personal 三条件 OR 识别。"""
    from autoteam.oauth_workspace import _is_personal_workspace

    assert _is_personal_workspace({"structure": "personal"})
    assert _is_personal_workspace({"structure": "personal_v2"})
    assert _is_personal_workspace({"plan_type": "free"})
    assert _is_personal_workspace({"is_personal": True})
    # 三条件都不命中 → False
    assert not _is_personal_workspace(
        {"structure": "workspace", "plan_type": "team", "is_personal": False}
    )


def test_select_oauth_workspace_success_2xx():
    """spec §2.2.2 — 200 + body 含 continue_url → success+redirect。"""
    from autoteam.oauth_workspace import select_oauth_workspace

    page = _FakePage(evaluate_result={
        "ok": True, "status": 200,
        "body": json.dumps({"continue_url": "https://auth.openai.com/callback?code=X"}),
        "location": "",
    })
    ok, redirect, evidence = select_oauth_workspace(
        page, "ws-1", consent_url="https://auth.openai.com/consent",
    )
    assert ok is True
    assert "callback" in redirect
    assert evidence["http_status"] == 200


def test_select_oauth_workspace_success_302_with_location():
    """302 redirect + Location 头 → success+location。"""
    from autoteam.oauth_workspace import select_oauth_workspace

    page = _FakePage(evaluate_result={
        "ok": False, "status": 302,
        "body": "",
        "location": "https://auth.openai.com/authorize/callback?code=ABC",
    })
    ok, redirect, _ = select_oauth_workspace(
        page, "ws-1", consent_url="https://auth.openai.com/consent",
    )
    assert ok is True
    assert "callback" in redirect


def test_select_oauth_workspace_404_endpoint_error():
    """404 → ok=False evidence 含 http_status。"""
    from autoteam.oauth_workspace import select_oauth_workspace

    page = _FakePage(evaluate_result={
        "ok": False, "status": 404, "body": "Not Found", "location": "",
    })
    ok, redirect, evidence = select_oauth_workspace(
        page, "ws-1", consent_url="https://auth.openai.com/consent",
    )
    assert ok is False
    assert redirect is None
    assert evidence["http_status"] == 404


def test_select_oauth_workspace_401_marked_auth_required():
    """401/403 evidence detail 标 auth_or_sentinel_required。"""
    from autoteam.oauth_workspace import select_oauth_workspace

    page = _FakePage(evaluate_result={
        "ok": False, "status": 401, "body": "", "location": "",
    })
    ok, _, evidence = select_oauth_workspace(
        page, "ws-1", consent_url="https://auth.openai.com/consent",
    )
    assert ok is False
    assert evidence.get("detail") == "auth_or_sentinel_required"


def test_select_oauth_workspace_evaluate_exception_returns_false():
    """page.evaluate 抛异常 → ok=False evidence 含 exception 类型,不向上传。"""
    from autoteam.oauth_workspace import select_oauth_workspace

    page = _FakePage(evaluate_exc=RuntimeError("boom"))
    ok, _, evidence = select_oauth_workspace(
        page, "ws-1", consent_url="https://auth.openai.com/consent",
    )
    assert ok is False
    assert "RuntimeError" in (evidence.get("exception") or "")


def test_ensure_personal_no_personal_workspace_fail_fast():
    """W-I2 — workspaces[] 不含 personal → OAUTH_WS_NO_PERSONAL,不走 fallback。"""
    from autoteam.oauth_workspace import (
        OAUTH_WS_NO_PERSONAL,
        ensure_personal_workspace_selected,
    )

    val = _make_plain_session([
        {"id": "ws-team", "structure": "workspace", "plan_type": "team"}
    ])
    page = _FakePage(cookies=[{"name": "oai-oauth-session", "value": val}])

    ok, fail_cat, evidence = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/consent",
    )
    assert ok is False
    assert fail_cat == OAUTH_WS_NO_PERSONAL
    # 不应走 fallback(W-I2)
    assert "fallback" not in evidence


def test_ensure_personal_primary_path_success():
    """spec §2.2.4 — workspaces[] 含 personal + primary 200 → ok=True。"""
    from autoteam.oauth_workspace import ensure_personal_workspace_selected

    val = _make_plain_session([
        {"id": "ws-personal", "structure": "personal", "plan_type": "free"}
    ])
    page = _FakePage(
        cookies=[{"name": "oai-oauth-session", "value": val}],
        evaluate_result={
            "ok": True, "status": 200,
            "body": json.dumps({"continue_url": "https://x"}),
            "location": "",
        },
    )

    ok, fail_cat, evidence = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/consent",
    )
    assert ok is True
    assert fail_cat == ""
    assert evidence["primary"]["http_status"] == 200


def test_ensure_personal_primary_fail_fallback_success():
    """主路径失败 → UI fallback 成功 → ok=True(三层兜底)。"""
    from autoteam.oauth_workspace import ensure_personal_workspace_selected

    val = _make_plain_session([
        {"id": "ws-personal", "structure": "personal"}
    ])
    page = _FakePage(
        cookies=[{"name": "oai-oauth-session", "value": val}],
        evaluate_result={"ok": False, "status": 404, "body": "", "location": ""},
        url="https://auth.openai.com/workspace",  # _is_workspace_selection_page 触发
        inner_text_value="select a workspace",
        locator_visible_count=1,  # 第 1 个 selector 不可见,第 2 个可见
    )

    ok, fail_cat, evidence = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/consent",
    )
    # primary 失败 → fallback 应被调用
    assert evidence.get("primary_failed") is True
    assert "fallback" in evidence
    assert ok is True
    assert fail_cat == ""


def test_ensure_personal_primary_and_fallback_both_fail():
    """主路径 + fallback 都失败 → OAUTH_WS_ENDPOINT_ERROR。"""
    from autoteam.oauth_workspace import (
        OAUTH_WS_ENDPOINT_ERROR,
        ensure_personal_workspace_selected,
    )

    val = _make_plain_session([
        {"id": "ws-personal", "structure": "personal"}
    ])
    # primary 404 + fallback URL 不是 workspace 页
    page = _FakePage(
        cookies=[{"name": "oai-oauth-session", "value": val}],
        evaluate_result={"ok": False, "status": 404, "body": "", "location": ""},
        url="https://example.com",  # 非 workspace 页
        inner_text_value="other content",
    )

    ok, fail_cat, _ = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/consent",
    )
    assert ok is False
    assert fail_cat == OAUTH_WS_ENDPOINT_ERROR


def test_ensure_personal_decode_failed_falls_back_to_ui():
    """W-I3 — cookie 解码失败应 fallback 到 UI(不直接 fail)。"""
    from autoteam.oauth_workspace import ensure_personal_workspace_selected

    page = _FakePage(
        cookies=[],  # 没有 cookie → decode 返回 None
        url="https://example.com",
        inner_text_value="other",
    )
    # primary 不会执行(没 personal id),只走 fallback,fallback 失败
    ok, fail_cat, evidence = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/consent",
    )
    assert ok is False
    # primary skip,只看 fallback
    assert "fallback" in evidence


def test_redact_workspaces_strips_sensitive_fields():
    """W-I6 — _redact_workspaces 应只保留白名单字段,不暴露 token。"""
    from autoteam.oauth_workspace import _redact_workspaces

    raw = [{
        "id": "ws-1",
        "name": "My Personal",
        "structure": "personal",
        "role": "owner",
        "plan_type": "free",
        "access_token": "DO_NOT_PERSIST",
        "session_token": "DO_NOT_PERSIST",
    }]
    out = _redact_workspaces(raw)
    assert len(out) == 1
    assert "access_token" not in out[0]
    assert "session_token" not in out[0]
    assert out[0]["id"] == "ws-1"


def test_no_time_sleep_8_in_manager_personal_branch():
    """W-I7 — manager.py _run_post_register_oauth personal 分支不应残留可执行的 time.sleep(8)。

    注释里出现 "time.sleep(8)" 字面串(描述 Round 8 删除决策)是允许的,
    所以筛选去注释行后再校验。
    """
    import inspect
    import re

    from autoteam import manager

    src = inspect.getsource(manager._run_post_register_oauth)
    # 移除注释行(以 # 开头的整行)再校验,只检查可执行代码
    lines = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    code_only = "\n".join(lines)
    # 不应有可执行的 time.sleep(8) — Round 8 删了这个旧的"等 default unset"魔法等待
    assert not re.search(r"^\s*time\.sleep\(8\)", code_only, re.MULTILINE)
    assert "Round 8" in src  # 说明已应用 Round 8 改造
