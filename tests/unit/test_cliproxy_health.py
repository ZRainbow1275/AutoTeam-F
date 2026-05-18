import pytest
import requests

from autoteam import cliproxy_health


class _Response:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    cliproxy_health.clear_cliproxy_health_cache()
    yield
    cliproxy_health.clear_cliproxy_health_cache()


def _set_cpa_env(monkeypatch):
    monkeypatch.setattr("autoteam.config.CPA_URL", "http://cpa.example.test")
    monkeypatch.setattr("autoteam.config.CPA_KEY", "secret-key")


def test_cliproxy_health_reports_provider_auth_unavailable(monkeypatch):
    _set_cpa_env(monkeypatch)
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return _Response(
            payload={
                "files": [
                    {"name": "codex-a.json", "provider": "codex", "status": "error", "unavailable": True},
                    {"name": "claude-a.json", "provider": "claude", "status": "active"},
                ]
            }
        )

    monkeypatch.setattr(cliproxy_health.requests, "get", fake_get)

    result = cliproxy_health.get_cliproxy_health(timeout=1, cache_ttl=1, force_refresh=True)

    assert result["ok"] is False
    assert result["safe_read_only"] is True
    assert result["management_api"]["ok"] is True
    assert result["provider_auth"]["ok"] is False
    assert result["provider_auth"]["reason"] == "provider_auth_all_unavailable"
    assert result["provider_auth"]["total"] == 1
    assert result["provider_auth"]["available"] == 0
    assert result["provider_auth"]["error"] == 1
    assert result["provider_auth"]["canary_required"] is True
    assert calls == [
        (
            "http://cpa.example.test/v0/management/auth-files",
            {"Authorization": "Bearer secret-key"},
            1,
        )
    ]


def test_cliproxy_health_reports_provider_auth_candidates(monkeypatch):
    _set_cpa_env(monkeypatch)

    monkeypatch.setattr(
        cliproxy_health.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            payload={
                "files": [
                    {
                        "name": "codex-team.json",
                        "provider": "codex",
                        "status": "active",
                        "id_token": {"plan_type": "team"},
                    },
                    {"name": "codex-disabled.json", "type": "codex", "status": "disabled", "disabled": True},
                ]
            }
        ),
    )

    result = cliproxy_health.get_cliproxy_health(timeout=1, cache_ttl=1, force_refresh=True)

    assert result["ok"] is True
    assert result["provider_auth"]["ok"] is True
    assert result["provider_auth"]["reason"] == "provider_auth_has_candidates"
    assert result["provider_auth"]["total"] == 2
    assert result["provider_auth"]["available"] == 1
    assert result["provider_auth"]["disabled"] == 1
    assert result["provider_auth"]["plan_counts"]["team"] == 1


def test_cliproxy_health_treats_management_timeout_as_unavailable(monkeypatch):
    _set_cpa_env(monkeypatch)

    def fake_get(*_args, **_kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(cliproxy_health.requests, "get", fake_get)

    result = cliproxy_health.get_cliproxy_health(timeout=1, cache_ttl=1, force_refresh=True)

    assert result["ok"] is False
    assert result["management_api"]["ok"] is False
    assert result["management_api"]["reason"] == "request_failed"
    assert result["management_api"]["error_type"] == "Timeout"
    assert result["provider_auth"]["reason"] == "management_api_unavailable"


def test_cliproxy_health_uses_short_ttl_cache(monkeypatch):
    _set_cpa_env(monkeypatch)
    calls = {"count": 0}

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        return _Response(payload={"files": [{"name": "codex-team.json", "provider": "codex", "status": "active"}]})

    monkeypatch.setattr(cliproxy_health.requests, "get", fake_get)

    first = cliproxy_health.get_cliproxy_health(timeout=1, cache_ttl=60)
    second = cliproxy_health.get_cliproxy_health(timeout=1, cache_ttl=60)

    assert first["cached"] is False
    assert second["cached"] is True
    assert calls["count"] == 1
