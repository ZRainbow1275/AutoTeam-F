"""Round 12 S4 — 注册收尾双路径 / mail provider rotation 单元测试。

详见 `.trellis/tasks/05-11-s4-register-dual-path-fix/prd.md`。

覆盖:
1. `classify_register_failure` 四类映射(>=4 case)
2. `RegisterPathRotator.try_each` 7+ 关键分支
3. `AliasWithReaderProvider` 写/读路径分发(4+ case)
4. `register_failures` 兼容 `provider_chain_history`(旧记录无该字段 / 新记录读写一致)
5. manager 三入口 mail_client=None 路由(向后兼容)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autoteam.mail.alias_reader_pair import AliasWithReaderProvider
from autoteam.mail.base import MailProvider
from autoteam.mail.fallback import MailProviderUnavailable, _FailureTracker
from autoteam.mail.register_dual_path import (
    InviteLinkMissingError,
    RegisterFailureType,
    RegisterPathExhausted,
    RegisterPathRotator,
    classify_register_failure,
    should_rotate_on,
)

# --------------------------------------------------------------------- helpers


class _FakeProvider(MailProvider):
    """最小可工作 MailProvider 实现 — 单测专用,不发任何 HTTP。"""

    provider_name = "fake"

    def __init__(self, name: str = "fake"):
        self.provider_name = name
        self.login_called = 0
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, method, *a, **kw):
        self.calls.append((method, a, kw))

    def login(self) -> str:
        self.login_called += 1
        self._record("login")
        return f"token-{self.provider_name}"

    def create_temp_email(self, prefix=None, domain=None):
        self._record("create_temp_email", prefix, domain)
        return (f"id-{self.provider_name}-{len(self.calls)}", f"user{len(self.calls)}@{self.provider_name}.test")

    def list_accounts(self, size: int = 200):
        self._record("list_accounts", size)
        return [{"accountId": "1", "email": f"u@{self.provider_name}.test"}]

    def delete_account(self, account_id):
        self._record("delete_account", account_id)
        return {"code": 200}

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        self._record("search_emails_by_recipient", to_email, size, account_id)
        return [{"subject": "hello", "sendEmail": "noreply@openai.com"}]

    def list_emails(self, account_id, size=10):
        self._record("list_emails", account_id, size)
        return []

    def delete_emails_for(self, to_email):
        self._record("delete_emails_for", to_email)
        return 1

    def get_latest_emails(self, account_id, email_id=0, all_receive=0):
        self._record("get_latest_emails", account_id, email_id, all_receive)
        return []

    def wait_for_email(self, to_email, timeout=None, sender_keyword=None):
        self._record("wait_for_email", to_email, timeout, sender_keyword)
        return {"subject": "OTP", "content": "code is 123456", "sendEmail": "noreply@openai.com"}


@pytest.fixture()
def tmp_tracker(tmp_path: Path) -> _FailureTracker:
    return _FailureTracker(state_file=tmp_path / "state.json", max_failures=2, cooldown_secs=60)


# --------------------------------------------------------------------- 1. classifier


class _FakeRegisterBlocked(Exception):
    """模拟 invite.RegisterBlocked,duck-typed 含 reason/step 属性。"""

    def __init__(self, step, reason, is_phone=False, is_duplicate=False):
        super().__init__(f"[{step}] {reason}")
        self.step = step
        self.reason = reason
        self.is_phone = is_phone
        self.is_duplicate = is_duplicate


def test_classify_timeout_error_is_otp_timeout():
    assert classify_register_failure(TimeoutError("等待邮件超时")) == RegisterFailureType.OTP_TIMEOUT


def test_classify_timeout_text_match():
    assert classify_register_failure("wait_for_email exceeded 180s") == RegisterFailureType.OTP_TIMEOUT


def test_classify_invite_link_missing_error_type():
    assert classify_register_failure(InviteLinkMissingError("no link")) == RegisterFailureType.INVITE_LINK_MISSING


def test_classify_invite_link_missing_text():
    assert classify_register_failure("invite link not found in 5 emails") == RegisterFailureType.INVITE_LINK_MISSING


def test_classify_domain_rejected_register_blocked():
    exc = _FakeRegisterBlocked("email", "this email is not allowed (disposable)")
    assert classify_register_failure(exc) == RegisterFailureType.DOMAIN_REJECTED


def test_classify_domain_rejected_chinese():
    exc = _FakeRegisterBlocked("email", "请使用其他邮箱")
    assert classify_register_failure(exc) == RegisterFailureType.DOMAIN_REJECTED


def test_classify_duplicate_is_other_not_domain():
    """duplicate 不应被误判为域名被拒(走原有 RegisterBlocked.is_duplicate 路径)。"""
    exc = _FakeRegisterBlocked("email", "this user already exists", is_duplicate=True)
    assert classify_register_failure(exc) == RegisterFailureType.OTHER


def test_classify_other_for_random_exception():
    assert classify_register_failure(ValueError("playwright crashed")) == RegisterFailureType.OTHER


def test_classify_none_is_other():
    assert classify_register_failure(None) == RegisterFailureType.OTHER


def test_should_rotate_on_truth_table():
    assert should_rotate_on(RegisterFailureType.OTP_TIMEOUT)
    assert should_rotate_on(RegisterFailureType.INVITE_LINK_MISSING)
    assert should_rotate_on(RegisterFailureType.DOMAIN_REJECTED)
    assert not should_rotate_on(RegisterFailureType.OTHER)


# --------------------------------------------------------------------- 2. rotator


def _strategy(name: str, provider: MailProvider | Exception | None = None):
    """构造 (name, factory),factory 返回 provider 或抛异常。"""

    def factory():
        if isinstance(provider, BaseException):
            raise provider
        return provider if provider is not None else _FakeProvider(name)

    return (name, factory)


def test_rotator_first_provider_success(tmp_tracker):
    p_a = _FakeProvider("A")
    rot = RegisterPathRotator([_strategy("A", p_a), _strategy("B")], tracker=tmp_tracker)

    def action(client, name, ctx):
        assert client is p_a
        assert name == "A"
        return "DONE"

    result = rot.try_each(action)
    assert result == "DONE"
    assert [h["provider"] for h in rot.provider_chain_history] == ["A"]
    assert rot.provider_chain_history[0]["error_type"] == "OK"


def test_rotator_skip_to_next_on_otp_timeout(tmp_tracker):
    p_a = _FakeProvider("A")
    p_b = _FakeProvider("B")
    rot = RegisterPathRotator(
        [_strategy("A", p_a), _strategy("B", p_b)],
        tracker=tmp_tracker,
    )

    calls = []

    def action(client, name, ctx):
        calls.append(name)
        if name == "A":
            raise TimeoutError("等待邮件超时 (180s)")
        return f"ok-{name}"

    result = rot.try_each(action)
    assert result == "ok-B"
    assert calls == ["A", "B"]
    types = [h["error_type"] for h in rot.provider_chain_history]
    assert types == ["OTP_TIMEOUT", "OK"]


def test_rotator_skip_on_domain_rejected(tmp_tracker):
    rot = RegisterPathRotator(
        [_strategy("addy"), _strategy("maillab")],
        tracker=tmp_tracker,
    )

    def action(client, name, ctx):
        if name == "addy":
            raise _FakeRegisterBlocked("email", "Please use a different email (disposable)")
        return "ok"

    assert rot.try_each(action) == "ok"
    assert rot.provider_chain_history[0]["error_type"] == "DOMAIN_REJECTED"


def test_rotator_skip_on_invite_link_missing(tmp_tracker):
    rot = RegisterPathRotator(
        [_strategy("addy"), _strategy("maillab")],
        tracker=tmp_tracker,
    )

    def action(client, name, ctx):
        if name == "addy":
            raise InviteLinkMissingError("extract_invite_link returned None")
        return "ok"

    assert rot.try_each(action) == "ok"
    assert rot.provider_chain_history[0]["error_type"] == "INVITE_LINK_MISSING"


def test_rotator_other_exception_does_not_rotate(tmp_tracker):
    rot = RegisterPathRotator(
        [_strategy("A"), _strategy("B")],
        tracker=tmp_tracker,
    )
    calls = []

    def action(client, name, ctx):
        calls.append(name)
        raise ValueError("playwright crashed")

    with pytest.raises(ValueError, match="playwright crashed"):
        rot.try_each(action)

    # 只调用了第一个 provider,没切到第二个
    assert calls == ["A"]
    assert rot.provider_chain_history[0]["error_type"] == "OTHER"


def test_rotator_all_fail_raises_path_exhausted(tmp_tracker):
    rot = RegisterPathRotator(
        [_strategy("A"), _strategy("B"), _strategy("C")],
        tracker=tmp_tracker,
    )

    def action(client, name, ctx):
        raise TimeoutError("等待邮件超时")

    with pytest.raises(RegisterPathExhausted) as ei:
        rot.try_each(action)

    err = ei.value
    assert len(err.history) == 3
    assert {h["provider"] for h in err.history} == {"A", "B", "C"}
    assert all(h["error_type"] == "OTP_TIMEOUT" for h in err.history)


def test_rotator_skips_unavailable_provider(tmp_tracker):
    rot = RegisterPathRotator(
        [
            _strategy("addy", MailProviderUnavailable("ADDY_IO_TOKEN missing")),
            _strategy("maillab"),
        ],
        tracker=tmp_tracker,
    )

    def action(client, name, ctx):
        return f"ok-{name}"

    assert rot.try_each(action) == "ok-maillab"
    types = [h["error_type"] for h in rot.provider_chain_history]
    assert types == ["UNAVAILABLE", "OK"]


def test_rotator_skips_blocked_provider(tmp_path):
    state = tmp_path / "state.json"
    # 手工把 A 标 blocked
    state.write_text(json.dumps({"A": {"fail_count": 5, "last_fail_ts": time.time()}}))
    tracker = _FailureTracker(state_file=state, max_failures=2, cooldown_secs=3600)
    rot = RegisterPathRotator([_strategy("A"), _strategy("B")], tracker=tracker)

    def action(client, name, ctx):
        return f"ok-{name}"

    assert rot.try_each(action) == "ok-B"
    assert rot.provider_chain_history[0]["error_type"] == "BLOCKED"
    assert rot.provider_chain_history[1]["error_type"] == "OK"


def test_rotator_records_success_resets_tracker(tmp_tracker):
    # A 之前失败过 1 次(未到 max_failures=2),成功后应被清零
    tmp_tracker.record_failure("A", "prev")
    assert not tmp_tracker.is_blocked("A")

    rot = RegisterPathRotator([_strategy("A")], tracker=tmp_tracker)
    rot.try_each(lambda c, n, ctx: "ok")

    # tracker state 应清零
    state = json.loads(tmp_tracker.state_file.read_text())
    assert "A" not in state


def test_rotator_empty_strategies_rejected():
    with pytest.raises(ValueError, match="至少需要一个"):
        RegisterPathRotator([])


def test_rotator_configured_chain():
    rot = RegisterPathRotator([_strategy("X"), _strategy("Y"), _strategy("Z")])
    assert rot.configured_chain == ["X", "Y", "Z"]


# --------------------------------------------------------------------- 3. AliasWithReaderProvider


def test_alias_reader_write_methods_go_to_alias():
    alias = _FakeProvider("addy")
    reader = _FakeProvider("maillab")
    pair = AliasWithReaderProvider(alias=alias, reader=reader)

    pair.create_temp_email(prefix="foo")
    pair.delete_account("id-1")
    pair.list_accounts(size=50)

    # 写方法都打到 alias
    assert [c[0] for c in alias.calls] == ["create_temp_email", "delete_account", "list_accounts"]
    # reader 没被调用(login 除外,这里没调 login)
    assert reader.calls == []


def test_alias_reader_read_methods_go_to_reader():
    alias = _FakeProvider("addy")
    reader = _FakeProvider("maillab")
    pair = AliasWithReaderProvider(alias=alias, reader=reader)

    pair.search_emails_by_recipient("u@addy.test")
    pair.list_emails("id-1")
    pair.delete_emails_for("u@addy.test")
    pair.get_latest_emails("id-1")
    pair.wait_for_email("u@addy.test", timeout=10)

    methods = [c[0] for c in reader.calls]
    assert "search_emails_by_recipient" in methods
    assert "list_emails" in methods
    assert "delete_emails_for" in methods
    assert "get_latest_emails" in methods
    assert "wait_for_email" in methods
    # alias 写端没被调
    assert alias.calls == []


def test_alias_reader_login_calls_both():
    alias = _FakeProvider("addy")
    reader = _FakeProvider("maillab")
    pair = AliasWithReaderProvider(alias=alias, reader=reader)

    token = pair.login()
    assert alias.login_called == 1
    assert reader.login_called == 1
    # Round 12 wire-up (minor m1) — login() now returns a fixed "ok" literal
    # instead of concatenating real tokens (avoids accidental token leakage
    # through accounts.json / log).
    assert token == "ok"
    assert "token" not in token


def test_alias_reader_composite_name():
    alias = _FakeProvider("addy_io")
    reader = _FakeProvider("maillab")
    pair = AliasWithReaderProvider(alias=alias, reader=reader)
    assert "addy_io" in pair.provider_name
    assert "maillab" in pair.provider_name
    desc = pair.describe()
    assert desc == {"type": "alias_with_reader", "alias": "addy_io", "reader": "maillab"}


def test_alias_reader_rejects_none():
    with pytest.raises(ValueError):
        AliasWithReaderProvider(alias=None, reader=_FakeProvider("r"))
    with pytest.raises(ValueError):
        AliasWithReaderProvider(alias=_FakeProvider("a"), reader=None)


# --------------------------------------------------------------------- 4. register_failures 兼容


def test_record_failure_with_provider_chain_history(tmp_path, monkeypatch):
    import autoteam.register_failures as rf

    monkeypatch.setattr(rf, "FAILURES_FILE", tmp_path / "rf.json")

    history = [
        {"provider": "addy_io", "error_type": "OTP_TIMEOUT", "ts": 1715472000.0},
        {"provider": "maillab", "error_type": "OK", "ts": 1715472180.0},
    ]
    rf.record_failure(
        "u@addy.test",
        rf.MAIL_OTP_TIMEOUT,
        "OTP 等待超时, 自动切到下一 provider",
        provider_chain_history=history,
        stage="create_account_direct",
    )

    recs = rf.list_failures(limit=10)
    assert len(recs) == 1
    r = recs[0]
    assert r["email"] == "u@addy.test"
    assert r["category"] == "mail_otp_timeout"
    assert r["provider_chain_history"] == history


def test_record_failure_backward_compat_no_chain_field(tmp_path, monkeypatch):
    """旧记录无 provider_chain_history 字段也能正常读出。"""
    import autoteam.register_failures as rf

    fail_file = tmp_path / "rf.json"
    monkeypatch.setattr(rf, "FAILURES_FILE", fail_file)

    # 直接写一条无 provider_chain_history 的旧格式记录
    fail_file.write_text(
        json.dumps([
            {
                "timestamp": time.time(),
                "email": "old@x.test",
                "category": "phone_blocked",
                "reason": "legacy",
            }
        ])
    )

    recs = rf.list_failures(limit=10)
    assert len(recs) == 1
    # 读时用 .get 兜底,不抛
    assert recs[0].get("provider_chain_history", []) == []


def test_new_mail_categories_exposed():
    import autoteam.register_failures as rf

    assert rf.MAIL_OTP_TIMEOUT == "mail_otp_timeout"
    assert rf.MAIL_INVITE_LINK_MISSING == "mail_invite_link_missing"
    assert rf.MAIL_DOMAIN_REJECTED == "mail_domain_rejected"


# --------------------------------------------------------------------- 5. manager 入口适配


def test_resolve_mail_client_passthrough_when_provided():
    from autoteam.manager import _resolve_mail_client_or_default

    sentinel = _FakeProvider("explicit")
    result = _resolve_mail_client_or_default(sentinel, acc={"email": "x"})
    assert result is sentinel


def test_resolve_mail_client_routes_via_helper(monkeypatch):
    """mail_client=None 时调用 _get_mail_client_for_account。"""
    import autoteam.manager as mgr

    fake = _FakeProvider("routed")
    captured = {}

    def fake_router(acc):
        captured["acc"] = acc
        return fake

    monkeypatch.setattr(mgr, "_get_mail_client_for_account", fake_router)
    result = mgr._resolve_mail_client_or_default(None, acc={"email": "y"})
    assert result is fake
    assert captured["acc"] == {"email": "y"}


def test_get_mail_client_for_account_calls_get_mail_client(monkeypatch):
    import autoteam.manager as mgr

    fake = _FakeProvider("from_env")
    # patch get_mail_client used internally
    import autoteam.mail as mail_pkg
    monkeypatch.setattr(mail_pkg, "get_mail_client", lambda: fake)

    result = mgr._get_mail_client_for_account({"email": "z@x.test"})
    assert result is fake
    # login 应被调
    assert fake.login_called == 1


def test_create_new_account_accepts_optional_mail_client(monkeypatch):
    """create_new_account(mail_client=None) 不应抛 — 走默认路由。"""
    import autoteam.manager as mgr

    routed = _FakeProvider("routed")
    monkeypatch.setattr(mgr, "_get_mail_client_for_account", lambda acc: routed)
    # 把 create_account_direct mock 成直接返回 routed.email,避免触发真实流程
    monkeypatch.setattr(mgr, "create_account_direct", lambda mc, **kw: f"ok:{mc.provider_name}")

    chatgpt = MagicMock()
    chatgpt.browser = None  # 跳过 pending invites

    result = mgr.create_new_account(chatgpt, mail_client=None)
    assert result == "ok:routed"


def test_create_account_direct_accepts_optional_mail_client(monkeypatch):
    """create_account_direct(mail_client=None) 走默认路由 + 内部 mail_client.create_temp_email。"""
    import autoteam.manager as mgr

    routed = _FakeProvider("routed")
    monkeypatch.setattr(mgr, "_get_mail_client_for_account", lambda acc: routed)
    # patch 内部 _register_direct_once 让它直接返回 success
    monkeypatch.setattr(mgr, "_register_direct_once", lambda mc, e, p, cloudmail_account_id=None: (True, None))
    monkeypatch.setattr(mgr, "add_account", lambda *a, **kw: None)
    monkeypatch.setattr(mgr, "_run_post_register_oauth", lambda *a, **kw: kw.get("email") or a[0])
    monkeypatch.setattr(mgr, "get_chatgpt_account_id", lambda: None)

    result = mgr.create_account_direct(mail_client=None)
    # 应当能拿到一个 email(_FakeProvider.create_temp_email 自动产生)
    assert result and "@routed.test" in result
