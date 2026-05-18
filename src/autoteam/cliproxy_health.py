"""Read-only CLIProxyAPI health checks for Codex provider availability."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping

import requests

_CACHE_LOCK = threading.Lock()
_CACHE_VALUE: dict | None = None
_CACHE_WRITTEN_AT = 0.0


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _entry_provider(entry: Mapping[str, object]) -> str:
    return str(entry.get("provider") or entry.get("type") or "").strip().lower()


def _entry_plan_type(entry: Mapping[str, object]) -> str:
    id_token = entry.get("id_token")
    if isinstance(id_token, Mapping):
        plan = id_token.get("plan_type")
        if plan:
            return str(plan).strip().lower()
    for key in ("plan_type", "account_type"):
        value = entry.get(key)
        if value:
            return str(value).strip().lower()
    name = str(entry.get("name") or "").lower()
    for plan in ("team", "plus", "pro", "free"):
        if f"-{plan}-" in name or f"-{plan}." in name:
            return plan
    return "unknown"


def _safe_status(value: object) -> str:
    return str(value or "").strip().lower() or "unknown"


def _summarize_provider_auth(files: list[object], *, provider: str, model: str) -> dict:
    provider_files = [entry for entry in files if isinstance(entry, Mapping) and _entry_provider(entry) == provider]
    disabled_count = 0
    unavailable_count = 0
    error_count = 0
    active_count = 0
    plan_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}

    for entry in provider_files:
        status = _safe_status(entry.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
        plan = _entry_plan_type(entry)
        plan_counts[plan] = plan_counts.get(plan, 0) + 1

        disabled = _is_truthy(entry.get("disabled")) or status == "disabled"
        unavailable = _is_truthy(entry.get("unavailable"))
        errored = status == "error"

        if disabled:
            disabled_count += 1
        if unavailable:
            unavailable_count += 1
        if errored:
            error_count += 1
        if not disabled and not unavailable and not errored:
            active_count += 1

    if active_count > 0:
        reason = "provider_auth_has_candidates"
    elif provider_files:
        reason = "provider_auth_all_unavailable"
    else:
        reason = "no_provider_auth"

    return {
        "ok": active_count > 0,
        "provider": provider,
        "model": model,
        "reason": reason,
        "total": len(provider_files),
        "available": active_count,
        "disabled": disabled_count,
        "unavailable": unavailable_count,
        "error": error_count,
        "status_counts": status_counts,
        "plan_counts": plan_counts,
        "check_type": "management_metadata",
        "canary_required": True,
    }


def _unavailable_provider_auth(provider: str, model: str, reason: str) -> dict:
    return {
        "ok": False,
        "provider": provider,
        "model": model,
        "reason": reason,
        "total": 0,
        "available": 0,
        "check_type": "management_metadata",
        "canary_required": True,
    }


def _collect_cliproxy_health(*, timeout: float, provider: str, model: str) -> dict:
    from autoteam.config import CPA_KEY, CPA_URL

    base_url = (CPA_URL or "").rstrip("/")
    checked_at = time.time()
    if not base_url or not CPA_KEY:
        return {
            "ok": False,
            "checked_at": checked_at,
            "safe_read_only": True,
            "management_api": {"ok": False, "reason": "missing_config"},
            "provider_auth": _unavailable_provider_auth(provider, model, "management_api_unavailable"),
        }

    started = time.monotonic()
    try:
        response = requests.get(
            f"{base_url}/v0/management/auth-files",
            headers={"Authorization": f"Bearer {CPA_KEY}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "checked_at": checked_at,
            "safe_read_only": True,
            "management_api": {
                "ok": False,
                "reason": "request_failed",
                "error_type": type(exc).__name__,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
            "provider_auth": _unavailable_provider_auth(provider, model, "management_api_unavailable"),
        }

    latency_ms = int((time.monotonic() - started) * 1000)
    management_api = {"ok": response.status_code == 200, "status_code": response.status_code, "latency_ms": latency_ms}
    if response.status_code != 200:
        management_api["reason"] = "non_200"
        return {
            "ok": False,
            "checked_at": checked_at,
            "safe_read_only": True,
            "management_api": management_api,
            "provider_auth": _unavailable_provider_auth(provider, model, "management_api_unavailable"),
        }

    try:
        payload = response.json()
    except ValueError:
        management_api.update({"ok": False, "reason": "non_json"})
        files: list[object] = []
    else:
        files_value = payload.get("files") if isinstance(payload, dict) else None
        if isinstance(files_value, list):
            files = files_value
        else:
            management_api.update({"ok": False, "reason": "invalid_files_payload"})
            files = []

    provider_auth = _summarize_provider_auth(files, provider=provider, model=model)
    return {
        "ok": bool(management_api["ok"] and provider_auth["ok"]),
        "checked_at": checked_at,
        "safe_read_only": True,
        "management_api": management_api,
        "provider_auth": provider_auth,
    }


def get_cliproxy_health(
    *,
    timeout: float | None = None,
    cache_ttl: float | None = None,
    provider: str = "codex",
    model: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Return safe read-only CLIProxyAPI health without uploading, deleting, or refreshing auth."""
    global _CACHE_VALUE, _CACHE_WRITTEN_AT

    resolved_timeout = timeout if timeout is not None else _float_env("CLIPROXY_HEALTH_TIMEOUT", 2.0)
    resolved_ttl = cache_ttl if cache_ttl is not None else _float_env("CLIPROXY_HEALTH_CACHE_TTL", 30.0)
    resolved_model = (model or os.environ.get("CLIPROXY_HEALTH_MODEL") or "gpt-5.5").strip() or "gpt-5.5"
    resolved_provider = (provider or "codex").strip().lower()

    now = time.time()
    with _CACHE_LOCK:
        if not force_refresh and _CACHE_VALUE is not None and now - _CACHE_WRITTEN_AT < resolved_ttl:
            cached = dict(_CACHE_VALUE)
            cached["cached"] = True
            cached["cache_age_seconds"] = int(now - _CACHE_WRITTEN_AT)
            return cached

    value = _collect_cliproxy_health(timeout=resolved_timeout, provider=resolved_provider, model=resolved_model)
    value["cached"] = False
    with _CACHE_LOCK:
        _CACHE_VALUE = dict(value)
        _CACHE_WRITTEN_AT = now
    return value


def clear_cliproxy_health_cache() -> None:
    global _CACHE_VALUE, _CACHE_WRITTEN_AT

    with _CACHE_LOCK:
        _CACHE_VALUE = None
        _CACHE_WRITTEN_AT = 0.0
