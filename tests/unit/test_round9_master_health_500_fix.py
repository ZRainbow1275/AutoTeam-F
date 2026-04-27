"""Round 9 SPEC v1.1 §13 — `/api/admin/master-health` HTTP 500 修复(M-I1 守恒)单测。

任何场景下 endpoint **永不返回 5xx**,所有内部异常映射 200 OK + business field:
  - ChatGPTTeamAPI.start() 抛 → reason='auth_invalid'
  - is_master_subscription_healthy 自身抛 → reason='network_error'
  - _pw_executor.run 调度异常 → reason='network_error'
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """绕过 auth_middleware:把 API_KEY 设为空字符串。"""
    monkeypatch.setattr("autoteam.api.API_KEY", "")
    from autoteam.api import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _free_playwright_lock(monkeypatch):
    """避免单测因 _playwright_lock 抢占失败 → 409。强制 acquire 立即成功。"""
    class _AlwaysFreeLock:
        def acquire(self, blocking=False):
            return True

        def release(self):
            pass

    monkeypatch.setattr("autoteam.api._playwright_lock", _AlwaysFreeLock())


def _patch_executor_passthrough(monkeypatch):
    """让 _pw_executor.run 直接同步执行 fn(),便于注入异常。"""
    class _PassThrough:
        def run(self, fn):
            return fn()

    monkeypatch.setattr("autoteam.api._pw_executor", _PassThrough())


def test_master_health_chatgpt_api_start_failure_returns_200_auth_invalid(client, monkeypatch):
    """spec §13.2 — ChatGPTTeamAPI.start() 抛 → 200 + reason=auth_invalid。"""
    _patch_executor_passthrough(monkeypatch)

    class _FailingAPI:
        def __init__(self):
            self.browser = None

        def start(self):
            raise RuntimeError("fake_oauth_token_expired")

        def stop(self):
            pass

    monkeypatch.setattr("autoteam.chatgpt_api.ChatGPTTeamAPI", _FailingAPI)

    resp = client.get("/api/admin/master-health")
    assert resp.status_code == 200, f"M-I1 守恒被破坏:{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["healthy"] is False
    assert body["reason"] == "auth_invalid"
    assert "chatgpt_api_start_failed" in body["evidence"]["detail"]
    assert body["evidence"]["http_status"] is None
    assert body["evidence"]["cache_hit"] is False
    assert body["evidence"]["probed_at"] > 0


def test_master_health_probe_unexpected_exception_returns_200_network_error(client, monkeypatch):
    """spec §13.2 — is_master_subscription_healthy 自身抛 → 200 + reason=network_error。"""
    _patch_executor_passthrough(monkeypatch)

    class _StubAPI:
        def __init__(self):
            self.browser = object()

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("autoteam.chatgpt_api.ChatGPTTeamAPI", _StubAPI)

    def _boom(api, force_refresh=False):
        raise ValueError("unexpected_internal_bug")

    monkeypatch.setattr(
        "autoteam.master_health.is_master_subscription_healthy", _boom,
    )

    resp = client.get("/api/admin/master-health")
    assert resp.status_code == 200, f"M-I1 守恒被破坏:{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["healthy"] is False
    assert body["reason"] == "network_error"
    assert "probe_unexpected_exception" in body["evidence"]["detail"]
    assert "ValueError" in body["evidence"]["detail"]


def test_master_health_executor_failure_returns_200_network_error(client, monkeypatch):
    """spec §13.3 — _pw_executor.run 调度本身抛 → 200 + reason=network_error。"""
    class _FailingExecutor:
        def run(self, fn):
            raise RuntimeError("executor_pool_full_or_dead")

    monkeypatch.setattr("autoteam.api._pw_executor", _FailingExecutor())

    resp = client.get("/api/admin/master-health")
    assert resp.status_code == 200, f"M-I1 守恒被破坏:{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["healthy"] is False
    assert body["reason"] == "network_error"
    assert "executor_failed" in body["evidence"]["detail"]
    assert "RuntimeError" in body["evidence"]["detail"]


def test_master_health_force_refresh_query_param_propagated(client, monkeypatch):
    """force_refresh=1 在异常路径下也透传到响应体,便于 UI 调试。"""
    _patch_executor_passthrough(monkeypatch)

    class _FailingAPI:
        def __init__(self):
            self.browser = None

        def start(self):
            raise RuntimeError("auth_dead")

        def stop(self):
            pass

    monkeypatch.setattr("autoteam.chatgpt_api.ChatGPTTeamAPI", _FailingAPI)

    resp = client.get("/api/admin/master-health?force_refresh=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["force_refresh"] is True


def test_master_health_happy_path_no_exception(client, monkeypatch):
    """正常路径 — probe 返回 (True, 'active', evidence) → 200 + healthy=True,确认 healthy 路径不被破坏。"""
    _patch_executor_passthrough(monkeypatch)

    class _StubAPI:
        def __init__(self):
            self.browser = object()

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("autoteam.chatgpt_api.ChatGPTTeamAPI", _StubAPI)

    fake_evidence = {
        "http_status": 200,
        "detail": "ok",
        "cache_hit": False,
        "probed_at": 1779000000.0,
    }
    monkeypatch.setattr(
        "autoteam.master_health.is_master_subscription_healthy",
        lambda api, force_refresh=False: (True, "active", fake_evidence),
    )

    resp = client.get("/api/admin/master-health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["reason"] == "active"
    assert body["evidence"] == fake_evidence
