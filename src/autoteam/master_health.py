"""Master 母号 ChatGPT Team 订阅健康度探针 — L1 fail-fast。

详见 SPEC `prompts/0426/spec/shared/master-subscription-health.md` v1.0(2026-04-27 Round 8)。

根因:母号 Team 订阅 cancel(eligible_for_auto_reactivation=true)时,workspace 实体仍在
       但子号 invite 后必拿 plan_type=free。本模块在 fill 任务起点先验证母号订阅健康度,
       不健康即 fail-fast,避免浪费 OAuth 周期。

不变量(M-I1~I10):
  - is_master_subscription_healthy 永不抛异常(任何 Exception → network_error)
  - auth_invalid 与 network_error 严格区分(401/403 是 auth_invalid 唯一来源)
  - healthy ⇔ reason == "active"(双向蕴含)
  - cache 命中**不**发起 HTTP
  - eligible_for_auto_reactivation 严格 `is True` 比对(不 truthy)
  - 落盘 evidence 不含敏感字段
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
ACCOUNTS_DIR = PROJECT_ROOT / "accounts"
CACHE_FILE = ACCOUNTS_DIR / ".master_health_cache.json"
CACHE_FILE_MODE = 0o666

CACHE_SCHEMA_VERSION = 1
DEFAULT_CACHE_TTL = 300.0  # 5 min
DEFAULT_PROBE_TIMEOUT = 10.0

# spec §3.3 — owner-eligible 角色白名单
_OWNER_ROLES = ("account-owner", "admin", "org-admin", "workspace-owner")

# spec §2.3 — raw_account_item 落盘白名单(避免 token 入盘)
_RAW_ITEM_PERSIST_KEYS = (
    "id",
    "structure",
    "current_user_role",
    "eligible_for_auto_reactivation",
    "name",
    "workspace_name",
    "plan",
    "plan_type",
)

_LOCK = threading.Lock()


def _ensure_dir():
    try:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"schema_version": CACHE_SCHEMA_VERSION, "cache": {}}
    try:
        raw = read_text(CACHE_FILE).strip()
        if not raw:
            return {"schema_version": CACHE_SCHEMA_VERSION, "cache": {}}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"schema_version": CACHE_SCHEMA_VERSION, "cache": {}}
        # schema 不一致 → 整体丢弃
        if data.get("schema_version") != CACHE_SCHEMA_VERSION:
            return {"schema_version": CACHE_SCHEMA_VERSION, "cache": {}}
        cache = data.get("cache")
        if not isinstance(cache, dict):
            cache = {}
        return {"schema_version": CACHE_SCHEMA_VERSION, "cache": cache}
    except Exception as exc:
        logger.warning("[master_health] cache 解析失败: %s,作 miss 处理", exc)
        return {"schema_version": CACHE_SCHEMA_VERSION, "cache": {}}


def _save_cache(data: dict) -> None:
    _ensure_dir()
    try:
        target = CACHE_FILE.resolve()
        write_text(target, json.dumps(data, indent=2, ensure_ascii=False))
        try:
            os.chmod(target, CACHE_FILE_MODE)
        except Exception:
            pass
    except Exception as exc:
        logger.warning("[master_health] cache 写入失败: %s", exc)


def _redact_raw_item(item: Any) -> dict:
    """spec §2.3 + M-I6 — 裁剪 raw_account_item 用于落盘。"""
    if not isinstance(item, dict):
        return {}
    return {k: item.get(k) for k in _RAW_ITEM_PERSIST_KEYS if k in item}


def _build_evidence(
    *,
    account_id: str | None,
    raw_item: dict | None = None,
    http_status: int | None = None,
    detail: str | None = None,
    items_count: int | None = None,
    current_user_role: str | None = None,
    plan_field: str | None = None,
    cache_hit: bool = False,
    cache_age_seconds: float | None = None,
    probed_at: float | None = None,
) -> dict:
    ev: dict = {
        "account_id": account_id,
        "cache_hit": cache_hit,
        "cache_age_seconds": cache_age_seconds,
        "probed_at": probed_at if probed_at is not None else time.time(),
    }
    if raw_item is not None:
        ev["raw_account_item"] = _redact_raw_item(raw_item)
    if http_status is not None:
        ev["http_status"] = http_status
    if detail is not None:
        ev["detail"] = detail
    if items_count is not None:
        ev["items_count"] = items_count
    if current_user_role is not None:
        ev["current_user_role"] = current_user_role
    if plan_field is not None:
        ev["plan_field"] = plan_field
    return ev


def _classify_l1(items: list, account_id: str) -> tuple[bool, str, dict]:
    """L1 主探针 — 在 /backend-api/accounts items[] 中找目标 account_id 并分类。"""
    target = None
    for item in items or []:
        if isinstance(item, dict) and str(item.get("id") or "") == account_id:
            target = item
            break

    if not target:
        return False, "workspace_missing", {
            "items_count": len(items or []),
            "account_id": account_id,
        }

    role = str(target.get("current_user_role") or "").lower()
    if role and role not in _OWNER_ROLES:
        return False, "role_not_owner", {
            "current_user_role": role,
            "raw_item": target,
        }

    # spec M-I7 — 严格 is True
    if target.get("eligible_for_auto_reactivation") is True:
        return False, "subscription_cancelled", {
            "current_user_role": role,
            "raw_item": target,
        }

    return True, "active", {
        "current_user_role": role,
        "raw_item": target,
    }


def _try_l3_settings_probe(chatgpt_api, account_id: str, target: dict) -> tuple[bool, str, dict] | None:
    """L3 副判定 — 仅当 L1 主探针 active 但目标项缺 eligible_for_auto_reactivation 字段时调用。

    返回 None 表示 L3 不命中(active 维持);否则返回 (False, reason, evidence_extras)。
    """
    # L1 已命中 active 且字段存在 False/None → 不再调 L3(降低 HTTP 噪声)
    if "eligible_for_auto_reactivation" in (target or {}):
        return None
    if not account_id:
        return None
    try:
        res = chatgpt_api._api_fetch(
            "GET", f"/backend-api/accounts/{account_id}/settings",
        )
    except Exception:
        return None
    status = (res or {}).get("status")
    if status in (401, 403):
        return False, "auth_invalid", {"http_status": status}
    if status != 200:
        return None  # 5xx / network → 不反向判定 cancelled
    try:
        body = json.loads((res or {}).get("body") or "{}")
    except Exception:
        return None
    plan_field = None
    if isinstance(body, dict):
        plan_field = body.get("plan") or body.get("plan_type") or body.get("subscription_status")
    if isinstance(plan_field, str):
        plan_low = plan_field.strip().lower()
        if plan_low and plan_low not in ("team", "business", "enterprise", "edu"):
            return False, "subscription_cancelled", {
                "plan_field": plan_low,
                "http_status": 200,
            }
    return None


def is_master_subscription_healthy(
    chatgpt_api,
    *,
    account_id: str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    cache_ttl: float = DEFAULT_CACHE_TTL,
    force_refresh: bool = False,
) -> tuple[bool, str, dict]:
    """判定 master 母号 ChatGPT Team 订阅是否健康。

    spec §2.2。返回 (healthy, reason, evidence)。

    M-I1:函数永不抛异常(任何 Exception → network_error)。
    """
    # 1. 解析 account_id
    if not account_id:
        try:
            from autoteam.admin_state import get_chatgpt_account_id
            account_id = get_chatgpt_account_id() or None
        except Exception:
            account_id = None

    if not account_id:
        return False, "workspace_missing", _build_evidence(
            account_id=None,
            detail="no_admin_account_id",
            cache_hit=False,
        )

    # 2. cache 查询
    if cache_ttl > 0 and not force_refresh:
        with _LOCK:
            cache_data = _load_cache()
        entry = cache_data["cache"].get(account_id)
        if isinstance(entry, dict):
            probed_at = float(entry.get("probed_at") or 0)
            age = time.time() - probed_at
            if 0 <= age < cache_ttl:
                healthy = bool(entry.get("healthy"))
                reason = str(entry.get("reason") or "")
                # M-I3 守卫
                if (healthy and reason == "active") or (not healthy and reason and reason != "active"):
                    raw_ev = entry.get("evidence") or {}
                    ev = _build_evidence(
                        account_id=account_id,
                        raw_item=raw_ev.get("raw_account_item"),
                        http_status=raw_ev.get("http_status"),
                        detail=raw_ev.get("detail"),
                        items_count=raw_ev.get("items_count"),
                        current_user_role=raw_ev.get("current_user_role"),
                        plan_field=raw_ev.get("plan_field"),
                        cache_hit=True,
                        cache_age_seconds=age,
                        probed_at=probed_at,
                    )
                    return healthy, reason, ev

    # 3. L1 主探针
    try:
        result = chatgpt_api._api_fetch("GET", "/backend-api/accounts")
    except Exception as exc:
        return False, "network_error", _build_evidence(
            account_id=account_id,
            detail=f"exception:{type(exc).__name__}",
        )

    if not isinstance(result, dict):
        return False, "network_error", _build_evidence(
            account_id=account_id,
            detail="invalid_api_fetch_result",
        )

    status = result.get("status")
    if status in (401, 403):
        return False, "auth_invalid", _build_evidence(
            account_id=account_id,
            http_status=status,
            detail="api_fetch_auth_error",
        )
    if status == 0 or (isinstance(status, int) and status >= 500):
        return False, "network_error", _build_evidence(
            account_id=account_id,
            http_status=status if isinstance(status, int) else 0,
            detail="api_fetch_network",
        )
    if status != 200:
        return False, "network_error", _build_evidence(
            account_id=account_id,
            http_status=status if isinstance(status, int) else 0,
            detail="api_fetch_non_200",
        )

    try:
        body = json.loads(result.get("body") or "{}")
    except Exception as exc:
        return False, "network_error", _build_evidence(
            account_id=account_id,
            http_status=200,
            detail=f"json_parse_error:{type(exc).__name__}",
        )

    items = []
    if isinstance(body, dict):
        items = body.get("items") or body.get("data") or body.get("accounts") or []
    if not isinstance(items, list):
        items = []

    healthy, reason, l1_extras = _classify_l1(items, account_id)
    raw_target = l1_extras.get("raw_item")

    # 4. L3 副判定(可选)— 仅当 L1 active 但缺 eligible 字段
    if healthy and reason == "active" and isinstance(raw_target, dict):
        l3 = _try_l3_settings_probe(chatgpt_api, account_id, raw_target)
        if l3 is not None:
            healthy, reason, l3_extras = l3
            l1_extras.update(l3_extras)

    # 5. 构建 evidence
    if reason == "workspace_missing":
        evidence = _build_evidence(
            account_id=account_id,
            http_status=200,
            items_count=l1_extras.get("items_count"),
            detail="account_id_not_found",
        )
    else:
        evidence = _build_evidence(
            account_id=account_id,
            raw_item=raw_target,
            http_status=l1_extras.get("http_status", 200),
            current_user_role=l1_extras.get("current_user_role"),
            plan_field=l1_extras.get("plan_field"),
        )

    # M-I3 守卫
    healthy = bool(healthy)
    if healthy and reason != "active":
        # 路径错误,降级为 network_error 防止 healthy 不一致
        logger.error(
            "[master_health] M-I3 守卫触发:healthy=True 但 reason=%s,降级 network_error",
            reason,
        )
        return False, "network_error", evidence
    if (not healthy) and reason == "active":
        logger.error(
            "[master_health] M-I3 守卫触发:healthy=False 但 reason=active,降级 network_error",
        )
        return False, "network_error", evidence

    # 6. 写 cache
    if cache_ttl > 0:
        try:
            with _LOCK:
                data = _load_cache()
                # evidence 写盘前裁剪敏感字段(已在 _redact_raw_item 处理)
                persist_ev = {
                    "raw_account_item": evidence.get("raw_account_item"),
                    "http_status": evidence.get("http_status"),
                    "current_user_role": evidence.get("current_user_role"),
                    "plan_field": evidence.get("plan_field"),
                    "detail": evidence.get("detail"),
                    "items_count": evidence.get("items_count"),
                }
                # 删除值为 None 的键
                persist_ev = {k: v for k, v in persist_ev.items() if v is not None}
                data["cache"][account_id] = {
                    "healthy": healthy,
                    "reason": reason,
                    "probed_at": evidence["probed_at"],
                    "evidence": persist_ev,
                }
                _save_cache(data)
        except Exception as exc:
            logger.warning("[master_health] 写 cache 失败: %s", exc)

    return healthy, reason, evidence
