"""Round 11 四轮 — chatgpt.com session_token 注入到 personal OAuth 跳过 /log-in.

阻塞场景:
- 刚被 master kick 出 Team 的子号在 OAuth /log-in 页 fill email 后,Continue 按钮变灰禁用,
  login flow 永远卡在 email 步骤,bundle=None。
- 5 次外层重试全失败,W-I9 / 二轮 / 三轮 修复都救不回来。

修复路径(本测试文件覆盖):
1. `_register_direct_once` 注册成功前从 chatgpt.com BrowserContext 抽
   `__Secure-next-auth.session-token` (含 chunked .0 / .1) 并返回 (success, session_token)。
2. `create_account_direct` 把 session_token 透给 `_run_post_register_oauth`。
3. `_run_post_register_oauth` 把 session_token 透给 `login_codex_via_browser`。
4. `login_codex_via_browser` 在 use_personal=True + session_token 时,把 cookie 注入
   `auth.openai.com`,/oauth/authorize 看到有效 session 直接跳过 /log-in。

参考:`SessionCodexAuthFlow._inject_auth_cookies`(主号专用 cookie 注入模式),
本轮把模式扩展给 personal 子号。
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from autoteam import codex_auth, manager

# ---------------------------------------------------------------------------
# Helper: _extract_session_token_from_context
# ---------------------------------------------------------------------------


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


def test_extract_session_token_single_cookie():
    ctx = _FakeContext(
        [
            {"name": "__Secure-next-auth.session-token", "value": "abc123"},
            {"name": "_account", "value": "ws-xxx"},
        ]
    )
    assert manager._extract_session_token_from_context(ctx) == "abc123"


def test_extract_session_token_chunked_cookies():
    """大 token 切成 .0 / .1 — 必须按 suffix 排序拼回。"""
    ctx = _FakeContext(
        [
            {"name": "__Secure-next-auth.session-token.1", "value": "_part1"},
            {"name": "__Secure-next-auth.session-token.0", "value": "part0"},
        ]
    )
    assert manager._extract_session_token_from_context(ctx) == "part0_part1"


def test_extract_session_token_missing_returns_none():
    ctx = _FakeContext([{"name": "_account", "value": "ws-xxx"}])
    assert manager._extract_session_token_from_context(ctx) is None


def test_extract_session_token_handles_cookies_exception():
    ctx = MagicMock()
    ctx.cookies.side_effect = RuntimeError("playwright dead")
    assert manager._extract_session_token_from_context(ctx) is None


# ---------------------------------------------------------------------------
# _inject_personal_session_cookies
# ---------------------------------------------------------------------------


class _RecordingContext:
    def __init__(self):
        self.added = []

    def add_cookies(self, cookies):
        self.added.extend(cookies)


def test_inject_session_cookies_short_token_dual_domain():
    """短 token 双域注入:chatgpt.com + auth.openai.com 各 1 个 cookie。"""
    ctx = _RecordingContext()
    codex_auth._inject_personal_session_cookies(ctx, "short_token")
    assert len(ctx.added) == 2

    domains = sorted(c["domain"] for c in ctx.added)
    assert domains == ["auth.openai.com", "chatgpt.com"]

    for cookie in ctx.added:
        assert cookie["name"] == "__Secure-next-auth.session-token"
        assert cookie["value"] == "short_token"
        assert cookie["httpOnly"] is True
        assert cookie["secure"] is True


def test_inject_session_cookies_chunked_token_dual_domain():
    """>3800 字节双域 × 切两段 = 4 个 cookies。"""
    ctx = _RecordingContext()
    big_token = "X" * 4000
    codex_auth._inject_personal_session_cookies(ctx, big_token)

    assert len(ctx.added) == 4

    # 每个域应该有 .0 和 .1 两个分片
    by_domain = {}
    for cookie in ctx.added:
        by_domain.setdefault(cookie["domain"], []).append(cookie)

    assert set(by_domain.keys()) == {"chatgpt.com", "auth.openai.com"}
    for domain, cookies in by_domain.items():
        names = sorted(c["name"] for c in cookies)
        assert names == [
            "__Secure-next-auth.session-token.0",
            "__Secure-next-auth.session-token.1",
        ], f"domain {domain} 缺少分片"
        chunks = {c["name"]: c["value"] for c in cookies}
        assert chunks["__Secure-next-auth.session-token.0"] == "X" * 3800
        assert chunks["__Secure-next-auth.session-token.1"] == "X" * 200
        for c in cookies:
            assert c["httpOnly"] is True
            assert c["secure"] is True


def test_inject_session_cookies_empty_token_noop():
    ctx = _RecordingContext()
    codex_auth._inject_personal_session_cookies(ctx, None)
    codex_auth._inject_personal_session_cookies(ctx, "")
    assert ctx.added == []


# ---------------------------------------------------------------------------
# Signature contract checks (防御性,签名变更需要更新本测试)
# ---------------------------------------------------------------------------


def test_login_codex_via_browser_accepts_session_token_kwarg():
    sig = inspect.signature(codex_auth.login_codex_via_browser)
    assert "chatgpt_session_token" in sig.parameters
    assert sig.parameters["chatgpt_session_token"].default is None
    assert (
        sig.parameters["chatgpt_session_token"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )


def test_run_post_register_oauth_accepts_session_token_kwarg():
    sig = inspect.signature(manager._run_post_register_oauth)
    assert "chatgpt_session_token" in sig.parameters
    assert sig.parameters["chatgpt_session_token"].default is None


def test_register_direct_once_returns_tuple():
    """source 应表明返回 (success, session_token) 元组。"""
    src = inspect.getsource(manager._register_direct_once)
    # 函数体里至少有一处 `return success, session_token` (主路径) 和早期 return False 转 False, None
    assert "return success, session_token" in src


def test_register_direct_once_no_bare_return_false_or_true():
    """所有 return 都必须是 tuple 形式,不能有 `return False` / `return True` 单值。

    实战 bug:把 final return 改成 tuple,但漏改了多处 early-return,
    `success, session_token = _register_direct_once(...)` 解构时炸 cannot unpack non-iterable bool。
    """
    import re

    src = inspect.getsource(manager._register_direct_once)
    bad_returns = []
    for m in re.finditer(r"^\s*return\s+(.*)$", src, re.MULTILINE):
        val = m.group(1).strip()
        # 允许:return False, None / return True, ... / return success, session_token
        # 禁止:return False / return True / return success / return None
        if val in ("False", "True", "success", "None"):
            bad_returns.append(val)
    assert not bad_returns, f"_register_direct_once 仍有非 tuple return: {bad_returns}"


# ---------------------------------------------------------------------------
# 整合点 — _run_post_register_oauth 透传 session_token 到 login_codex_via_browser
# ---------------------------------------------------------------------------


def test_post_register_oauth_forwards_session_token_to_login(monkeypatch):
    """personal 路径下,_run_post_register_oauth 必须把 chatgpt_session_token 透给 login_codex_via_browser。"""
    captured = {}

    def fake_login(
        email,
        password,
        *,
        mail_client=None,
        use_personal=False,
        chatgpt_session_token=None,
        signup_profile=None,
    ):
        captured["use_personal"] = use_personal
        captured["chatgpt_session_token"] = chatgpt_session_token
        captured["signup_profile"] = signup_profile
        # 直接返回失败 bundle 让上层 5 次重试结束(再失败也继续测试关注的 forward 行为)
        return None

    # _run_post_register_oauth 里 login_codex_via_browser 是从 codex_auth 模块导入,
    # monkey patch 模块属性即可。
    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    # 让 master health probe 直接通过(避免触发真 API)
    fake_master = MagicMock()
    fake_master.start.return_value = None
    fake_master.stop.return_value = None
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda *a, **kw: fake_master)

    # is_master_subscription_healthy 必须 True 才走 kick
    monkeypatch.setattr(
        "autoteam.master_health.is_master_subscription_healthy",
        lambda api: (True, "active", {"account_id": "x"}),
    )

    # remove_from_team 走通
    monkeypatch.setattr(manager, "remove_from_team", lambda api, email, return_status: "removed")

    # update_account / record_failure / delete_account 全部桩成 noop,避免写文件
    monkeypatch.setattr(manager, "update_account", lambda *a, **kw: None)
    monkeypatch.setattr(manager, "record_failure", lambda *a, **kw: None)
    monkeypatch.setattr(manager, "delete_account", lambda *a, **kw: None)

    # 重试间 sleep 太长,patch 掉
    monkeypatch.setattr(manager.time, "sleep", lambda *a, **kw: None)

    out = {}
    result = manager._run_post_register_oauth(
        "test@example.com",
        "pwd",
        mail_client=MagicMock(),
        leave_workspace=True,
        out_outcome=out,
        chatgpt_session_token="my_session_token_12345",
    )

    # bundle=None 5 次重试都失败 → 返回 None,但 forward 应已发生
    assert result is None
    assert captured.get("use_personal") is True
    assert captured.get("chatgpt_session_token") == "my_session_token_12345"


# ---------------------------------------------------------------------------
# Source-level invariant — login_codex_via_browser 在 personal+token 路径调用 inject helper
# ---------------------------------------------------------------------------


def test_login_codex_calls_inject_helper_in_personal_with_token():
    """source 必须包含 `if use_personal and chatgpt_session_token:` 守卫和注入调用。"""
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    assert "use_personal and chatgpt_session_token" in src
    assert "_inject_personal_session_cookies" in src


# ---------------------------------------------------------------------------
# Source invariant — manager 透传链不丢失
# ---------------------------------------------------------------------------


def test_create_account_direct_threads_session_token_into_post_oauth():
    src = inspect.getsource(manager.create_account_direct)
    assert "chatgpt_session_token=session_token" in src


def test_register_direct_once_extracts_session_token_before_cleanup():
    """注册主路径里必须在统一 Playwright cleanup 之前调用 extract helper。

    `_register_direct_once` 有多处 early-return cleanup(失败路径),
    成功路径的 cleanup 是最后一个,extract 必须在它之前。
    """
    src = inspect.getsource(manager._register_direct_once)
    extract_idx = src.find("_extract_session_token_from_context")
    final_cleanup_idx = src.rfind("cleanup_direct_register()")
    assert extract_idx != -1, "_register_direct_once 必须调 _extract_session_token_from_context"
    assert final_cleanup_idx != -1
    assert extract_idx < final_cleanup_idx, "extract 必须在成功路径的最终 cleanup 之前"


def test_register_direct_once_uses_unified_playwright_cleanup():
    src = inspect.getsource(manager._register_direct_once)
    assert "browser.close()" not in src
    assert "close_playwright_objects(page, context, browser" in src


# ---------------------------------------------------------------------------
# 完整闭环 — fill 个人 / Team 共存性,Team 路径不该带 token
# ---------------------------------------------------------------------------


def test_team_path_does_not_inject_session_token():
    """Team 模式(use_personal=False)即使 caller 传了 session_token,也不应该注入。"""
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # 守卫确保只有 personal 路径触发注入
    inject_block_idx = src.find("_inject_personal_session_cookies(context, chatgpt_session_token)")
    guard_idx = src.find("if use_personal and chatgpt_session_token:")
    assert inject_block_idx > guard_idx > 0, "注入必须被 personal+token 守卫包裹"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
