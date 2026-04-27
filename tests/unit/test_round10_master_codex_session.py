"""Round 10 (2026-04-28) — `login_codex_via_session` thin-wrapper + `refresh_main_auth_file` 单测。

PRD: `.trellis/tasks/04-28-master-codex-oauth-session-fallback/prd.md`
- B1: `login_codex_via_session` 重构为 thin wrapper,委托给 `SessionCodexAuthFlow`
- B2: 5 个 case 覆盖 wrapper / refresh 路径

契约约束(对齐 upstream cnitlrt/AutoTeam codex_auth.py:1017-1043):
1. wrapper 调 `flow.start()`;若 step == "completed" → 调 `flow.complete()` 取 bundle 返回
2. step != "completed" → 返回 None + log warning("未直接完成")
3. flow.start() raise → 异常往上抛,但 finally 必跑 flow.stop()
4. refresh_main_auth_file:bundle 非空 → 调 save_main_auth_file + 返回 dict
5. refresh_main_auth_file:bundle=None → 抛 RuntimeError("无法基于管理员登录态生成主号 Codex 认证文件")
   (文案保留,API 层依赖此错误信息)
"""

from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# Fixtures — mock admin_state 提供 email/session/account_id/workspace_name
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_admin_state(monkeypatch):
    """所有 case 默认提供合法的 admin 凭证,避免 SessionCodexAuthFlow.__init__ 中 _build_auth_url 失败。"""
    monkeypatch.setattr("autoteam.codex_auth.get_admin_email", lambda: "admin@example.com")
    monkeypatch.setattr("autoteam.codex_auth.get_admin_session_token", lambda: "fake-session-token-abc123")
    monkeypatch.setattr("autoteam.codex_auth.get_chatgpt_account_id", lambda: "ws-account-uuid-xyz")
    monkeypatch.setattr("autoteam.codex_auth.get_chatgpt_workspace_name", lambda: "Master Team")


def _make_complete_bundle():
    """构造一个完整的 OAuth bundle,模拟 _exchange_auth_code 的成功返回。"""
    return {
        "access_token": "fake.access.token",
        "refresh_token": "fake_refresh_token_long_string_abc",
        "id_token": "fake.id.token.jwt",
        "account_id": "ws-account-uuid-xyz",
        "email": "admin@example.com",
        "plan_type": "team",
        "expired": 1779000000.0,
    }


# ---------------------------------------------------------------------------
# Test 1: wrapper.start() returns step=completed → complete() + return bundle
# ---------------------------------------------------------------------------


def test_wrapper_completes_returns_bundle(monkeypatch):
    """flow.start() 返回 step=completed → wrapper 调 flow.complete() 拿 bundle 返回."""
    from autoteam import codex_auth

    bundle = _make_complete_bundle()
    calls = {"start": 0, "complete": 0, "stop": 0}

    class _FakeFlow:
        def __init__(self, **kwargs):
            # 验证关键参数从 admin_state 注入
            assert kwargs["email"] == "admin@example.com"
            assert kwargs["session_token"] == "fake-session-token-abc123"
            assert kwargs["account_id"] == "ws-account-uuid-xyz"
            assert kwargs["workspace_name"] == "Master Team"
            assert kwargs["password"] == ""
            assert kwargs["password_callback"] is None
            assert callable(kwargs["auth_file_callback"])

        def start(self):
            calls["start"] += 1
            return {"step": "completed", "detail": None}

        def complete(self):
            calls["complete"] += 1
            return {"email": bundle["email"], "auth_file": "", "plan_type": "team", "bundle": bundle}

        def stop(self):
            calls["stop"] += 1

    monkeypatch.setattr(codex_auth, "SessionCodexAuthFlow", _FakeFlow)

    result = codex_auth.login_codex_via_session()

    assert result == bundle
    assert calls == {"start": 1, "complete": 1, "stop": 1}


# ---------------------------------------------------------------------------
# Test 2: wrapper.start() returns step=email_required → return None + log warning
# ---------------------------------------------------------------------------


def test_wrapper_email_required_returns_none(monkeypatch, caplog):
    """flow.start() 返回 step != completed → wrapper 返回 None + log warning."""
    from autoteam import codex_auth

    calls = {"start": 0, "complete": 0, "stop": 0}

    class _FakeFlow:
        def __init__(self, **kwargs):
            pass

        def start(self):
            calls["start"] += 1
            return {"step": "email_required", "detail": "still on email-input page"}

        def complete(self):
            calls["complete"] += 1
            raise AssertionError("complete 不应被调用")

        def stop(self):
            calls["stop"] += 1

    monkeypatch.setattr(codex_auth, "SessionCodexAuthFlow", _FakeFlow)

    with caplog.at_level(logging.WARNING, logger="autoteam.codex_auth"):
        result = codex_auth.login_codex_via_session()

    assert result is None
    assert calls == {"start": 1, "complete": 0, "stop": 1}
    # 验证 warning 文案包含"未直接完成"
    assert any("未直接完成" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Test 3: wrapper.start() raises → exception propagates AND flow.stop() still runs
# ---------------------------------------------------------------------------


def test_wrapper_exception_still_stops_flow(monkeypatch):
    """flow.start() raise → 异常往上抛,但 finally 必跑 flow.stop()."""
    from autoteam import codex_auth

    calls = {"start": 0, "complete": 0, "stop": 0}

    class _FakeFlow:
        def __init__(self, **kwargs):
            pass

        def start(self):
            calls["start"] += 1
            raise RuntimeError("boom — playwright crashed")

        def complete(self):
            calls["complete"] += 1
            raise AssertionError("complete 不应被调用")

        def stop(self):
            calls["stop"] += 1

    monkeypatch.setattr(codex_auth, "SessionCodexAuthFlow", _FakeFlow)

    with pytest.raises(RuntimeError, match="boom"):
        codex_auth.login_codex_via_session()

    # finally 块必须跑 stop()
    assert calls == {"start": 1, "complete": 0, "stop": 1}


# ---------------------------------------------------------------------------
# Test 4: refresh_main_auth_file — bundle 成功 → save_main_auth_file + return dict
# ---------------------------------------------------------------------------


def test_refresh_main_auth_file_saves_on_success(monkeypatch, tmp_path):
    """login_codex_via_session 返回 bundle → refresh 调 save_main_auth_file + 返回 dict."""
    from autoteam import codex_auth

    bundle = _make_complete_bundle()
    fake_path = str(tmp_path / "codex-main-ws-account-uuid-xyz.json")

    save_calls = []

    def _fake_save(b):
        save_calls.append(b)
        return fake_path

    monkeypatch.setattr(codex_auth, "login_codex_via_session", lambda: bundle)
    monkeypatch.setattr(codex_auth, "save_main_auth_file", _fake_save)

    result = codex_auth.refresh_main_auth_file()

    assert result == {
        "email": "admin@example.com",
        "auth_file": fake_path,
        "plan_type": "team",
    }
    assert len(save_calls) == 1
    assert save_calls[0] == bundle


# ---------------------------------------------------------------------------
# Test 5: refresh_main_auth_file — bundle=None → RuntimeError(文案保留)
# ---------------------------------------------------------------------------


def test_refresh_main_auth_file_raises_on_none(monkeypatch):
    """login_codex_via_session 返回 None → refresh 抛 RuntimeError 文案保留(向后兼容).

    API 层(api.py:1259)依赖此错误文案 swallow 进 info["main_auth_error"] 字段,
    任何修改都是 breaking change。
    """
    from autoteam import codex_auth

    monkeypatch.setattr(codex_auth, "login_codex_via_session", lambda: None)

    with pytest.raises(RuntimeError, match="无法基于管理员登录态生成主号 Codex 认证文件"):
        codex_auth.refresh_main_auth_file()


# ---------------------------------------------------------------------------
# Bonus: 验证 SessionCodexAuthFlow 关键方法存在(Round 10 实施前置条件)
# ---------------------------------------------------------------------------


def test_session_codex_auth_flow_has_required_methods():
    """SessionCodexAuthFlow 必须含 wrapper 调用所需的全套方法 — Round 10 前置条件.

    若任一方法缺失,Approach A 重构会运行时崩溃。
    """
    from autoteam.codex_auth import SessionCodexAuthFlow

    required = {
        "_auto_fill_email",
        "_advance",
        "_detect_step",
        "_inject_auth_cookies",
        "_attach_callback_listeners",
        "start",
        "complete",
        "stop",
    }
    actual = set(dir(SessionCodexAuthFlow))
    missing = required - actual
    assert not missing, f"SessionCodexAuthFlow 缺少必需方法: {missing}"


def test_inject_auth_cookies_guards_account_id():
    """_inject_auth_cookies 必须用 `if self.account_id:` 守护,避免空字符串污染 _account cookie.

    upstream codex_auth.py:1203 有此守卫;本地若没有需补。
    本测试以源码静态扫描方式验证 — 不要求实际跑 Playwright。
    """
    import inspect

    from autoteam.codex_auth import SessionCodexAuthFlow

    src = inspect.getsource(SessionCodexAuthFlow._inject_auth_cookies)
    # 必须有 `if self.account_id:` 这种守卫(允许空白 / 注释微差异)
    assert "if self.account_id" in src, (
        "SessionCodexAuthFlow._inject_auth_cookies 必须用 `if self.account_id:` 守 _account cookie"
    )
