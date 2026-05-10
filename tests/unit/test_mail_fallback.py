"""Fallback mail provider chain 单元测试 — 全 mock provider class,无真实 HTTP。"""

from __future__ import annotations

import time

import pytest

from autoteam.mail.base import MailProvider
from autoteam.mail.fallback import (
    DEFAULT_COOLDOWN_SECS,
    DEFAULT_MAX_FAILURES,
    FallbackMailProvider,
    MailProviderChainExhausted,
    MailProviderUnavailable,
    _FailureTracker,
)

# ----------------------------------------------------------------- mock providers


class _BaseMockProvider(MailProvider):
    """Mock provider 基类,默认所有方法返 占位结果。"""

    provider_name = "mock"

    def login(self) -> str:
        return "mock-token"

    def create_temp_email(self, prefix=None, domain=None):
        return (1, f"{prefix or 'x'}@example.com")

    def list_accounts(self, size: int = 200):
        return []

    def delete_account(self, account_id):
        return {"code": 200}

    def search_emails_by_recipient(self, to_email, size: int = 10, account_id=None):
        return []

    def list_emails(self, account_id, size: int = 10):
        return []

    def delete_emails_for(self, to_email):
        return 0


class _AlwaysSucceeds(_BaseMockProvider):
    provider_name = "always_ok"

    def create_temp_email(self, prefix=None, domain=None):
        return (101, f"ok-{prefix or 'x'}@ok.com")


class _AlwaysFails(_BaseMockProvider):
    provider_name = "always_fail"

    def create_temp_email(self, prefix=None, domain=None):
        raise RuntimeError("provider broken")


class _UnavailableProvider:
    """构造时直接抛 MailProviderUnavailable(模拟 env 缺失)。"""

    def __init__(self):
        raise MailProviderUnavailable("config missing")


class _FailNTimesThenSucceeds(_BaseMockProvider):
    provider_name = "flaky"

    def __init__(self, fail_n: int = 2):
        super().__init__()
        self._fail_n = fail_n
        self._calls = 0

    def create_temp_email(self, prefix=None, domain=None):
        self._calls += 1
        if self._calls <= self._fail_n:
            raise RuntimeError(f"transient #{self._calls}")
        return (200, "flaky@ok.com")


# ----------------------------------------------------------------- helpers


@pytest.fixture
def tmp_state(tmp_path):
    """每个测试用独立 state 文件 + 极短 cooldown(便于测试)。"""
    state_file = tmp_path / "mail_provider_state.json"
    return _FailureTracker(
        state_file=state_file,
        max_failures=DEFAULT_MAX_FAILURES,
        cooldown_secs=DEFAULT_COOLDOWN_SECS,
    )


# ----------------------------------------------------------------- tests: tracker


def test_tracker_initial_state_no_blocks(tmp_state):
    assert tmp_state.is_blocked("foo") is False


def test_tracker_record_failure_increments(tmp_state):
    assert tmp_state.record_failure("p1", "err1") == 1
    assert tmp_state.record_failure("p1", "err2") == 2
    assert tmp_state.is_blocked("p1") is False
    assert tmp_state.record_failure("p1", "err3") == 3
    assert tmp_state.is_blocked("p1") is True


def test_tracker_record_success_resets(tmp_state):
    tmp_state.record_failure("p1")
    tmp_state.record_failure("p1")
    tmp_state.record_success("p1")
    assert tmp_state.is_blocked("p1") is False
    # 计数重置后,再失败一次只算 1 次
    assert tmp_state.record_failure("p1") == 1


def test_tracker_cooldown_expired_auto_resets(tmp_path):
    state_file = tmp_path / "state.json"
    tracker = _FailureTracker(state_file=state_file, max_failures=2, cooldown_secs=1)
    tracker.record_failure("p1")
    tracker.record_failure("p1")
    assert tracker.is_blocked("p1") is True
    # 等 cooldown 过期(>1s)
    time.sleep(1.1)
    # is_blocked 触发自动 reset
    assert tracker.is_blocked("p1") is False
    # 再失败一次,应是 1(已重置)
    assert tracker.record_failure("p1") == 1


def test_tracker_persists_across_instances(tmp_path):
    state_file = tmp_path / "state.json"
    t1 = _FailureTracker(state_file=state_file, max_failures=3, cooldown_secs=99999)
    t1.record_failure("p1", "err")
    t1.record_failure("p1", "err")

    t2 = _FailureTracker(state_file=state_file, max_failures=3, cooldown_secs=99999)
    # 复用同一文件 → 计数应可见
    assert t2.record_failure("p1") == 3
    assert t2.is_blocked("p1") is True


def test_tracker_handles_corrupt_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not json {{{", encoding="utf-8")
    tracker = _FailureTracker(state_file=state_file, max_failures=3, cooldown_secs=99999)
    # 损坏文件应被静默重置(只 warn)
    assert tracker.is_blocked("p1") is False


# ----------------------------------------------------------------- tests: dispatch


def test_fallback_first_provider_succeeds_no_failover(tmp_state):
    chain = FallbackMailProvider(
        [("a", _AlwaysSucceeds), ("b", _AlwaysFails)],
        tracker=tmp_state,
    )
    aid, email = chain.create_temp_email(prefix="t")
    assert email.startswith("ok-t")
    assert chain.current_provider_name == "a"


def test_fallback_first_fails_second_succeeds(tmp_state):
    chain = FallbackMailProvider(
        [("a", _AlwaysFails), ("b", _AlwaysSucceeds)],
        tracker=tmp_state,
    )
    aid, email = chain.create_temp_email(prefix="t")
    assert email.startswith("ok-")
    assert chain.current_provider_name == "b"
    # a 应记一次失败
    assert tmp_state.record_failure("a", "test") >= 2


def test_fallback_unavailable_provider_skipped_no_failure_count(tmp_state):
    chain = FallbackMailProvider(
        [("u", _UnavailableProvider), ("ok", _AlwaysSucceeds)],
        tracker=tmp_state,
    )
    chain.create_temp_email(prefix="t")
    assert chain.current_provider_name == "ok"
    # u 因为 MailProviderUnavailable 不计入失败计数
    # → 下次再访问还是先尝试 u(再次 raise unavailable),不变 blocked
    assert tmp_state.is_blocked("u") is False


def test_fallback_all_providers_fail_raises_exhausted(tmp_state):
    chain = FallbackMailProvider(
        [("a", _AlwaysFails), ("b", _AlwaysFails)],
        tracker=tmp_state,
    )
    with pytest.raises(MailProviderChainExhausted) as exc:
        chain.create_temp_email(prefix="t")
    assert "a" in exc.value.errors
    assert "b" in exc.value.errors
    assert "RuntimeError" in exc.value.errors["a"]


def test_fallback_blocked_provider_skipped(tmp_path):
    state_file = tmp_path / "state.json"
    tracker = _FailureTracker(state_file=state_file, max_failures=2, cooldown_secs=99999)
    # 预先把 a 标记为 blocked
    tracker.record_failure("a", "pre1")
    tracker.record_failure("a", "pre2")
    assert tracker.is_blocked("a")

    # _AlwaysFails should not even be constructed when blocked
    construct_count = {"n": 0}

    class _CountingFails(_AlwaysFails):
        def __init__(self):
            construct_count["n"] += 1
            super().__init__()

    chain = FallbackMailProvider(
        [("a", _CountingFails), ("b", _AlwaysSucceeds)],
        tracker=tracker,
    )
    chain.create_temp_email(prefix="t")
    assert construct_count["n"] == 0  # blocked → 不构造
    assert chain.current_provider_name == "b"


def test_fallback_success_resets_failure_count(tmp_path):
    state_file = tmp_path / "state.json"
    tracker = _FailureTracker(state_file=state_file, max_failures=3, cooldown_secs=99999)
    # 让 a 累计 1 次失败,但还没 blocked
    tracker.record_failure("a", "old")
    assert tracker.is_blocked("a") is False

    chain = FallbackMailProvider([("a", _AlwaysSucceeds)], tracker=tracker)
    chain.create_temp_email(prefix="t")
    # 业务成功 → 计数清零
    assert tracker.record_failure("a", "fresh") == 1


def test_fallback_provider_error_drops_instance_for_next_init(tmp_state):
    """provider 抛异常后,下次访问应重新构造,避开半坏状态。"""

    class _ResetCounter:
        n_init = 0

    class _BrokenFirstThenOk(_BaseMockProvider):
        provider_name = "self_heal"

        def __init__(self):
            super().__init__()
            _ResetCounter.n_init += 1
            self._broken = _ResetCounter.n_init == 1

        def create_temp_email(self, prefix=None, domain=None):
            if self._broken:
                raise RuntimeError("init #1 broken")
            return (300, "healed@ok.com")

    chain = FallbackMailProvider([("h", _BrokenFirstThenOk)], tracker=tmp_state)
    # 第一次:实例 1 → 抛错 → drop
    with pytest.raises(MailProviderChainExhausted):
        chain.create_temp_email(prefix="t")
    # 第二次:重新构造实例 2 → 成功
    aid, email = chain.create_temp_email(prefix="t")
    assert email == "healed@ok.com"
    assert _ResetCounter.n_init == 2


def test_fallback_empty_providers_list_raises():
    with pytest.raises(ValueError):
        FallbackMailProvider([])


def test_fallback_configured_chain_property(tmp_state):
    chain = FallbackMailProvider(
        [("a", _AlwaysSucceeds), ("b", _AlwaysFails)],
        tracker=tmp_state,
    )
    assert chain.configured_chain == ["a", "b"]


def test_fallback_dispatches_all_abc_methods(tmp_state):
    chain = FallbackMailProvider([("a", _AlwaysSucceeds)], tracker=tmp_state)
    # 全 ABC 方法应可调用且不抛
    assert chain.login() == "mock-token"
    assert chain.create_temp_email() == (101, "ok-x@ok.com")
    assert chain.list_accounts() == []
    assert chain.delete_account(1) == {"code": 200}
    assert chain.search_emails_by_recipient("x@y.com") == []
    assert chain.list_emails(1) == []
    assert chain.delete_emails_for("x@y.com") == 0


# ----------------------------------------------------------------- factory integration


def test_factory_returns_fallback_when_chain_env_set(monkeypatch):
    """当 MAIL_PROVIDER_CHAIN 设置时,get_mail_client 返回 FallbackMailProvider。"""
    from autoteam.mail import get_mail_client

    # 用 cf_temp_email 走通 factory(其 __init__ 不需要必填 env)
    monkeypatch.setenv("MAIL_PROVIDER_CHAIN", "cf_temp_email")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "any")

    client = get_mail_client()
    assert isinstance(client, FallbackMailProvider)
    assert client.configured_chain == ["cf_temp_email"]


def test_factory_skips_unknown_provider_in_chain_env(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER_CHAIN", "unknown_xx,cf_temp_email")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "any")

    from autoteam.mail import get_mail_client

    client = get_mail_client()
    assert isinstance(client, FallbackMailProvider)
    # unknown 被跳过,只剩 cf_temp_email
    assert client.configured_chain == ["cf_temp_email"]


def test_factory_chain_env_all_unknown_raises(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER_CHAIN", "unknown_a,unknown_b")
    from autoteam.mail import get_mail_client

    with pytest.raises(ValueError):
        get_mail_client()


def test_factory_falls_back_to_single_provider_when_chain_unset(monkeypatch):
    """无 MAIL_PROVIDER_CHAIN 时,保留旧 MAIL_PROVIDER 行为。"""
    monkeypatch.delenv("MAIL_PROVIDER_CHAIN", raising=False)
    monkeypatch.setenv("MAIL_PROVIDER", "cf_temp_email")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "any")

    from autoteam.mail import get_mail_client

    client = get_mail_client()
    # 不是 FallbackMailProvider — 是单 provider 实例
    assert not isinstance(client, FallbackMailProvider)


def test_factory_resolves_addy_io_alias(monkeypatch):
    """factory 应识别 addy_io 别名 (addy / anonaddy)。"""
    from autoteam.mail import _resolve_provider_factory

    f1 = _resolve_provider_factory("addy_io")
    f2 = _resolve_provider_factory("addy")
    f3 = _resolve_provider_factory("anonaddy")
    assert f1 is f2 is f3


def test_factory_resolves_simplelogin_alias():
    from autoteam.mail import _resolve_provider_factory

    f1 = _resolve_provider_factory("simplelogin")
    f2 = _resolve_provider_factory("sl")
    assert f1 is f2
