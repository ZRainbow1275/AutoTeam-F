"""Round 8 — Master 母号订阅健康度探针单测。

覆盖 spec `prompts/0426/spec/shared/master-subscription-health.md` 7 类返回:
  active / subscription_cancelled / network_error / auth_invalid / workspace_missing /
  role_not_owner

不变量验证:
  M-I1 函数永不抛异常
  M-I3 healthy ⇔ reason == "active"
  M-I7 eligible_for_auto_reactivation 严格 `is True` 比对
  cache TTL + schema_version 行为
"""
from __future__ import annotations

import json
import time

import pytest


class _FakeChatGPTAPI:
    """模拟 ChatGPTTeamAPI._api_fetch — 按路径预设响应,记录调用次数。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def _api_fetch(self, method, path):
        self.calls.append((method, path))
        if path in self.responses:
            r = self.responses[path]
            if isinstance(r, Exception):
                raise r
            return r
        return {"status": 404, "body": ""}


@pytest.fixture(autouse=True)
def _patch_account_id(monkeypatch, tmp_path):
    """所有测试默认 admin account_id=test-master,cache 写到 tmp 目录。"""
    monkeypatch.setattr(
        "autoteam.admin_state.get_chatgpt_account_id",
        lambda: "test-master",
    )
    # cache 写到 tmp,避免污染真实 accounts/.master_health_cache.json
    fake_cache = tmp_path / "master_health_cache.json"
    monkeypatch.setattr("autoteam.master_health.CACHE_FILE", fake_cache)
    yield
    if fake_cache.exists():
        fake_cache.unlink()


def _make_accounts_response(items):
    return {"status": 200, "body": json.dumps({"items": items})}


def test_master_subscription_active_when_eligible_false():
    """spec §3.2 — eligible_for_auto_reactivation=False(或不存在)且 role 是 owner → active。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {
                "id": "test-master",
                "structure": "workspace",
                "current_user_role": "account-owner",
                "eligible_for_auto_reactivation": False,
                "plan_type": "team",
            }
        ]),
    })
    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is True
    assert reason == "active"
    assert evidence["account_id"] == "test-master"
    assert evidence.get("current_user_role") == "account-owner"


def test_master_subscription_cancelled_when_eligible_true():
    """spec §3.2 — eligible_for_auto_reactivation is True → subscription_cancelled。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {
                "id": "test-master",
                "structure": "workspace",
                "current_user_role": "account-owner",
                "eligible_for_auto_reactivation": True,
                "plan_type": "team",
            }
        ]),
    })
    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "subscription_cancelled"


def test_master_subscription_eligible_truthy_string_does_not_trigger_cancel():
    """M-I7 — 严格 `is True`,字符串 "true" 等不应触发 cancelled(防误判)。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {
                "id": "test-master",
                "current_user_role": "account-owner",
                "eligible_for_auto_reactivation": "true",  # 字符串而非 bool
            }
        ]),
    })
    healthy, reason, _ = is_master_subscription_healthy(api, cache_ttl=0)
    # "true" 字符串不是 True bool → 走 active 路径(防御性)
    assert reason == "active"
    assert healthy is True


def test_master_subscription_workspace_missing():
    """spec §3.2 — items[] 中找不到目标 account_id → workspace_missing。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {"id": "other-account", "current_user_role": "account-owner"}
        ]),
    })
    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "workspace_missing"
    assert evidence.get("items_count") == 1


def test_master_subscription_role_not_owner():
    """spec §3.2 — role 不在 owner 白名单 → role_not_owner。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {"id": "test-master", "current_user_role": "member"}
        ]),
    })
    healthy, reason, _ = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "role_not_owner"


def test_master_subscription_auth_invalid_on_401():
    """spec §3.2 — _api_fetch status=401 → auth_invalid(不是 network_error)。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": {"status": 401, "body": "Unauthorized"},
    })
    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "auth_invalid"
    assert evidence.get("http_status") == 401


def test_master_subscription_network_error_on_500():
    """spec §3.2 — 5xx → network_error(不反向判 cancel)。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": {"status": 500, "body": "ISE"},
    })
    healthy, reason, _ = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "network_error"


def test_master_subscription_no_throw_on_underlying_exception():
    """M-I1 — _api_fetch 抛异常时函数也不抛,降级 network_error。"""
    from autoteam.master_health import is_master_subscription_healthy

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": RuntimeError("connection refused"),
    })
    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "network_error"
    assert "exception" in (evidence.get("detail") or "")


def test_master_subscription_cache_hit_skips_http():
    """spec §4 — cache 命中时不发 HTTP。"""
    from autoteam.master_health import is_master_subscription_healthy

    items = [{
        "id": "test-master",
        "current_user_role": "account-owner",
        "eligible_for_auto_reactivation": False,
    }]
    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response(items),
    })
    # 第一次:写 cache
    h1, r1, _ = is_master_subscription_healthy(api, cache_ttl=300)
    assert h1 is True and r1 == "active"
    n_calls_after_first = len(api.calls)

    # 第二次:cache 命中,不再调 _api_fetch
    h2, r2, ev2 = is_master_subscription_healthy(api, cache_ttl=300)
    assert h2 is True and r2 == "active"
    assert ev2.get("cache_hit") is True
    assert len(api.calls) == n_calls_after_first  # 没新调用


def test_master_subscription_force_refresh_bypasses_cache():
    """force_refresh=True 应跳过 cache 强制重测。"""
    from autoteam.master_health import is_master_subscription_healthy

    items = [{
        "id": "test-master",
        "current_user_role": "account-owner",
        "eligible_for_auto_reactivation": False,
    }]
    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response(items),
    })
    is_master_subscription_healthy(api, cache_ttl=300)
    n_first = len(api.calls)

    is_master_subscription_healthy(api, cache_ttl=300, force_refresh=True)
    assert len(api.calls) > n_first  # force 强制走 HTTP


def test_master_subscription_cache_schema_version_mismatch_invalidates(tmp_path, monkeypatch):
    """spec §4.2 — schema_version 不匹配的 cache 整体丢弃。"""
    from autoteam.master_health import is_master_subscription_healthy

    fake_cache = tmp_path / "broken_cache.json"
    fake_cache.write_text(json.dumps({
        "schema_version": 999,  # 错误版本
        "cache": {
            "test-master": {
                "healthy": True,
                "reason": "active",
                "probed_at": time.time(),
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr("autoteam.master_health.CACHE_FILE", fake_cache)

    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {"id": "test-master", "current_user_role": "account-owner",
             "eligible_for_auto_reactivation": False}
        ]),
    })
    is_master_subscription_healthy(api, cache_ttl=300)
    # 旧 schema 应被丢弃 → 走 HTTP 重测
    assert len(api.calls) == 1


def test_master_subscription_evidence_redacts_sensitive_fields():
    """M-I6 — evidence 落盘不应含 token/cookie 等敏感字段。"""
    from autoteam.master_health import _redact_raw_item

    sensitive = {
        "id": "test",
        "structure": "workspace",
        "current_user_role": "account-owner",
        "eligible_for_auto_reactivation": False,
        "plan_type": "team",
        "access_token": "DO_NOT_PERSIST",
        "session_token": "DO_NOT_PERSIST",
        "cookies": {"oai_token": "secret"},
    }
    redacted = _redact_raw_item(sensitive)
    assert "access_token" not in redacted
    assert "session_token" not in redacted
    assert "cookies" not in redacted
    assert redacted.get("id") == "test"


def test_master_subscription_invariant_m_i3_healthy_iff_active():
    """M-I3 — 不变量验证:healthy=True ⇔ reason=='active'。"""
    from autoteam.master_health import is_master_subscription_healthy

    # active 必 healthy
    api = _FakeChatGPTAPI({
        "/backend-api/accounts": _make_accounts_response([
            {"id": "test-master", "current_user_role": "account-owner",
             "eligible_for_auto_reactivation": False}
        ]),
    })
    h, r, _ = is_master_subscription_healthy(api, cache_ttl=0)
    assert h is True and r == "active"

    # 各种 not_active reason 必 unhealthy
    for cancel_items in [
        [{"id": "test-master", "current_user_role": "account-owner",
          "eligible_for_auto_reactivation": True}],
        [],  # workspace_missing
        [{"id": "test-master", "current_user_role": "member"}],  # role_not_owner
    ]:
        api = _FakeChatGPTAPI({
            "/backend-api/accounts": _make_accounts_response(cancel_items),
        })
        h, r, _ = is_master_subscription_healthy(api, cache_ttl=0)
        assert h is False
        assert r != "active"
