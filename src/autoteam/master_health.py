"""Master 母号 ChatGPT Team 订阅健康度探针 — L1 fail-fast + grace 期 healthy。

详见 SPEC `prompts/0426/spec/shared/master-subscription-health.md` v1.2(2026-04-28 Round 11)。

根因(Round 8):母号 Team 订阅 cancel(eligible_for_auto_reactivation=true)时,workspace
       实体仍在但子号 invite 后必拿 plan_type=free。本模块在 fill 任务起点先验证母号订阅
       健康度,不健康即 fail-fast,避免浪费 OAuth 周期。
修订(Round 11):用户实证 cancel_at_period_end=true 时 ChatGPT 网页 team 权限**仍然有效**
       (grace 期内权益不变,新 invite 仍能拿 plan_type=team)。Round 8 假设 "eligible 必拿 free"
       是错的,引入 subscription_grace 状态:`eligible=true` + JWT chatgpt_subscription_active_until
       未到期时,返回 (healthy=True, reason="subscription_grace") — fail-fast 入口对 healthy=True
       自动放行,UI banner 渲染 warning 而非 critical。

不变量(M-I1~I12):
  - is_master_subscription_healthy 永不抛异常(任何 Exception → network_error)
  - auth_invalid 与 network_error 严格区分(401/403 是 auth_invalid 唯一来源)
  - healthy ⇔ reason ∈ {"active", "subscription_grace"}(Round 11 扩展双向蕴含)
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

CACHE_SCHEMA_VERSION = 2  # Round 11 重发布:旧 cache(v1)未启用 chatgpt_api access_token 解 grace_until,统一作废
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


def _chatgpt_api_ready(chatgpt_api) -> bool:
    """Return whether a ChatGPTTeamAPI-like object has a usable API session.

    `CHATGPT_API_TRANSPORT=auto` can start a backend API session through HTTP
    transport only. Retroactive master-health checks only need `_api_fetch`, so
    a browser object is not required here.
    """
    if chatgpt_api is None:
        return False
    is_started = getattr(chatgpt_api, "is_started", None)
    if callable(is_started):
        try:
            return bool(is_started())
        except Exception:
            pass
    return bool(
        getattr(chatgpt_api, "browser", None)
        or getattr(chatgpt_api, "http_transport", None)
    )


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


def _load_admin_id_token(chatgpt_api=None) -> str | None:
    """Round 11 — 加载用于解 grace_until 的 JWT token。

    优先级(Round 11 修订):
      1. chatgpt_api.access_token(ChatGPT web JWT,/api/auth/session 拿到,含
         chatgpt_subscription_active_until claim)— 走 web session 路径的用户主路径
      2. accounts/codex-main-*.json 最近修改文件的 id_token(走 OAuth 重登路径的兜底)
      3. None

    永不抛异常(M-I1)。
    """
    # 1. ChatGPT web access_token(用户登录态对应的 JWT)
    if chatgpt_api is not None:
        try:
            tok = getattr(chatgpt_api, "access_token", None)
            if tok and isinstance(tok, str):
                return tok
        except Exception:
            pass
    # 2. codex-main-*.json id_token(OAuth 路径)
    try:
        if not ACCOUNTS_DIR.exists():
            return None
        candidates = sorted(
            ACCOUNTS_DIR.glob("codex-main-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                _, id_token = _read_access_token_from_auth_file(path)
                if id_token:
                    return id_token
            except Exception:
                continue
        return None
    except Exception:
        return None


def _classify_l1(
    items: list,
    account_id: str,
    *,
    id_token: str | None = None,
) -> tuple[bool, str, dict]:
    """L1 主探针 — 在 /backend-api/accounts items[] 中找目标 account_id 并分类。

    Round 11:`eligible_for_auto_reactivation=true` 时先解 admin id_token JWT 拿
    chatgpt_subscription_active_until,若仍在 grace 期内返回 (True, "subscription_grace", ...)
    healthy=True;过期或 JWT 缺失保持 Round 8 (False, "subscription_cancelled", ...) 行为。
    """
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
        # Round 11 — grace 期判定:JWT chatgpt_subscription_active_until 仍未过期 → healthy=True
        grace_until = extract_grace_until_from_jwt(id_token) if id_token else None
        if grace_until and grace_until > time.time():
            return True, "subscription_grace", {
                "current_user_role": role,
                "raw_item": target,
                "grace_until": grace_until,
                "grace_remain_seconds": grace_until - time.time(),
            }
        # Round 11 二轮 — grace_until 解不出(web session JWT 路径,只含 chatgpt_plan_type
        # 而无 chatgpt_subscription_active_until):用 chatgpt_plan_type fallback
        # 当前权益仍为付费层 → 视为 grace 期内 healthy=True
        plan_type = extract_plan_type_from_jwt(id_token) if id_token else None
        _PAID_PLAN_TYPES = ("team", "business", "enterprise", "edu")
        if plan_type in _PAID_PLAN_TYPES:
            return True, "subscription_grace", {
                "current_user_role": role,
                "raw_item": target,
                "grace_until": grace_until,  # 可能 None,前端不显示倒计时
                "plan_type_jwt": plan_type,  # 区分 grace 来源(诊断用)
            }
        return False, "subscription_cancelled", {
            "current_user_role": role,
            "raw_item": target,
            "grace_until": grace_until,  # 可能 None / 已过期
            "plan_type_jwt": plan_type,  # 可能 "free" / None
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
                # M-I3 守卫(Round 11):healthy ⇔ reason ∈ {"active", "subscription_grace"}
                healthy_reasons = ("active", "subscription_grace")
                guard_ok = (
                    (healthy and reason in healthy_reasons)
                    or (not healthy and reason and reason not in healthy_reasons)
                )
                if not guard_ok:
                    logger.warning(
                        "[master_health] cache 项违反 M-I3 不变量:healthy=%s reason=%r,"
                        "丢弃 cache 走 L1 实测",
                        healthy, reason,
                    )
                if guard_ok:
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
                    # Round 11 — grace 期 evidence 还原 grace_until / grace_remain_seconds
                    grace_until = raw_ev.get("grace_until")
                    if grace_until is not None:
                        ev["grace_until"] = grace_until
                        try:
                            ev["grace_remain_seconds"] = float(grace_until) - time.time()
                        except Exception:
                            ev["grace_remain_seconds"] = None
                    # Round 11 二轮 — plan_type_jwt 也要还原(诊断字段)
                    plan_type_jwt = raw_ev.get("plan_type_jwt")
                    if plan_type_jwt is not None:
                        ev["plan_type_jwt"] = plan_type_jwt
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

    # Round 11 — 加载 admin id_token 给 _classify_l1 解 grace_until
    id_token = _load_admin_id_token(chatgpt_api)
    healthy, reason, l1_extras = _classify_l1(items, account_id, id_token=id_token)
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
        # Round 11 — grace 期 + cancelled 路径都把 grace_until 透到 evidence
        grace_until = l1_extras.get("grace_until")
        if grace_until is not None:
            evidence["grace_until"] = grace_until
            if reason == "subscription_grace":
                try:
                    evidence["grace_remain_seconds"] = float(grace_until) - time.time()
                except Exception:
                    evidence["grace_remain_seconds"] = None
        # Round 11 二轮 — plan_type_jwt 透到 evidence(grace fallback / cancelled 路径都用)
        plan_type_jwt = l1_extras.get("plan_type_jwt")
        if plan_type_jwt is not None:
            evidence["plan_type_jwt"] = plan_type_jwt

    # M-I3 守卫(Round 11):healthy ⇔ reason ∈ {"active", "subscription_grace"}
    healthy = bool(healthy)
    healthy_reasons = ("active", "subscription_grace")
    if healthy and reason not in healthy_reasons:
        logger.error(
            "[master_health] M-I3 守卫触发:healthy=True 但 reason=%s,降级 network_error",
            reason,
        )
        return False, "network_error", evidence
    if (not healthy) and reason in healthy_reasons:
        logger.error(
            "[master_health] M-I3 守卫触发:healthy=False 但 reason=%s,降级 network_error",
            reason,
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
                    # Round 11 — grace_until 持久化(grace_remain_seconds 不持久化,
                    # 命中 cache 时按 now-time 重算)
                    "grace_until": evidence.get("grace_until"),
                    # Round 11 二轮 — plan_type_jwt 持久化(诊断字段,不参与判定)
                    "plan_type_jwt": evidence.get("plan_type_jwt"),
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


# ---------------------------------------------------------------------------
# Round 12 S7 — WorkspacePool failover hook
# ---------------------------------------------------------------------------

# reasons that should NOT trip workspace failover (transient / classification noise)
_TRANSIENT_FAIL_REASONS = ("network_error",)
# reasons that SHOULD trip failover (real master-side failure)
_HARD_FAIL_REASONS = ("auth_invalid", "subscription_cancelled", "workspace_missing", "role_not_owner")


def apply_pool_health_signal(
    healthy: bool,
    reason: str,
    evidence: dict | None = None,
    *,
    pool=None,
) -> dict | None:
    """Round 12 S7 — feed a master_health probe result into the WorkspacePool.

    Call this from any caller of :func:`is_master_subscription_healthy` to
    drive auto failover. Behavior:

    - healthy=True (active or subscription_grace) → pool.mark_healthy(active_id)
    - healthy=False AND reason in _HARD_FAIL_REASONS → pool.mark_unhealthy(...)
      which accumulates fail_count + (when ≥ threshold) auto-promotes a
      warm/cold candidate.
    - healthy=False AND reason in _TRANSIENT_FAIL_REASONS → no-op (don't
      flap on intermittent network glitches).

    Returns the **post-call** active workspace snapshot (None if pool empty
    or failed). Never raises — M-I1 alignment.

    ``pool`` defaults to ``workspace_pool.default_pool``; tests can inject
    their own.
    """
    try:
        if pool is None:
            from autoteam.workspace_pool import default_pool as pool_obj
            pool = pool_obj
        active = pool.get_active()
        if active is None:
            return None
        ws_id = active.get("id")
        if not ws_id:
            return None
        if healthy and reason in ("active", "subscription_grace"):
            return pool.mark_healthy(ws_id)
        if (not healthy) and reason in _HARD_FAIL_REASONS:
            return pool.mark_unhealthy(ws_id, reason)
        # transient or unknown → leave state alone but bubble snapshot
        return active
    except Exception as exc:
        logger.warning("[master_health] apply_pool_health_signal failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Round 9 SPEC v1.1 §11~§12 — Retroactive helper + grace 期 JWT 解析
# ---------------------------------------------------------------------------


def extract_grace_until_from_jwt(token):
    """从 access/id_token JWT payload 解析 chatgpt_subscription_active_until → epoch seconds。

    spec/shared/master-subscription-health.md v1.1 §12.2。token 既可以是 access_token
    也可以是 id_token,只要 payload 中含 https://api.openai.com/auth.chatgpt_subscription_active_until。
    返回:
        epoch seconds(float) — 字段存在且解析成功
        None — token 缺失 / 字段缺失 / 格式错误 / 解析异常(永不抛)
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        import base64

        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    auth_claims = payload.get("https://api.openai.com/auth") or {}
    raw = auth_claims.get("chatgpt_subscription_active_until")
    if raw is None:
        return None
    # raw 可能是 epoch(int / float)或 ISO-8601 字符串
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            from datetime import datetime

            normalized = raw.strip().replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None
    return None


def extract_plan_type_from_jwt(token):
    """从 access_token / id_token JWT payload 解析 chatgpt_plan_type → 字符串。

    Round 11 二轮修复:ChatGPT web access_token 不含 chatgpt_subscription_active_until
    claim,但含 chatgpt_plan_type 表示当前权益层级。grace 期内此字段仍为 "team" 等付费层。

    返回:
        小写字符串(如 "team", "free", "business")— 字段存在
        None — token 缺失 / 字段缺失 / 解析失败(永不抛)
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        import base64

        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    auth_claims = payload.get("https://api.openai.com/auth") or {}
    raw = auth_claims.get("chatgpt_plan_type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return None


def _read_access_token_from_auth_file(auth_file_path):
    """从 auth_file 读 access_token / id_token,优先返回 id_token(grace_until 在 id_token 里)。

    返回 (access_token, id_token);任一不可用返回 None。
    """
    if not auth_file_path:
        return None, None
    try:
        from pathlib import Path as _Path

        path_obj = _Path(auth_file_path)
        if not path_obj.exists():
            return None, None
        data = json.loads(path_obj.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    access_token = data.get("access_token") or (data.get("tokens") or {}).get("access_token")
    id_token = data.get("id_token") or (data.get("tokens") or {}).get("id_token")
    return access_token, id_token


def _apply_master_degraded_classification(
    workspace_id=None,
    grace_until=None,
    *,
    chatgpt_api=None,
    dry_run=False,
):
    """Round 9 SPEC v1.1 §11.2 — 母号订阅 retroactive 重分类 helper。

    抽 Round 8 _reconcile_master_degraded_subaccounts 子句为通用 helper,5 触发点共用:
    lifespan / _auto_check_loop / _reconcile_team_members / sync_account_states / cmd_rotate。

    行为:
      1. 调 is_master_subscription_healthy(走 5min cache,不主动 force_refresh)
      2. reason == "subscription_cancelled" → 进入"前进"路径
         - ACTIVE/EXHAUSTED + workspace 命中 + JWT grace_until 未过期 → DEGRADED_GRACE
         - DEGRADED_GRACE + grace 已到期                                → STANDBY
      3. reason == "active" → 进入"撤回"路径
         - DEGRADED_GRACE + master_account_id_at_grace == 当前 account_id → ACTIVE
      4. 其他 reason → skipped

    M-I1 / I11:永不抛异常,所有 Exception 捕获 + logger.warning。
    M-I12:GRACE 子号绝不被 KICK(本函数只改 status,不调远端)。

    返回 dict:
        {
          "skipped_reason": Optional[str],
          "marked_grace":   List[email],
          "marked_standby": List[email],
          "reverted_active": List[email],
          "errors":         List[dict],
        }
    """
    from autoteam.accounts import (
        STATUS_ACTIVE,
        STATUS_DEGRADED_GRACE,
        STATUS_EXHAUSTED,
        STATUS_STANDBY,
        load_accounts,
        update_account,
    )
    from autoteam.register_failures import MASTER_SUBSCRIPTION_DEGRADED, record_failure

    out = {
        "skipped_reason": None,
        "marked_grace": [],
        "marked_standby": [],
        "reverted_active": [],
        "errors": [],
        "dry_run": bool(dry_run),
    }

    # 1. master health probe(走 cache)
    api_owns = False
    api = chatgpt_api
    try:
        if not _chatgpt_api_ready(api):
            try:
                from autoteam.chatgpt_api import ChatGPTTeamAPI

                api = ChatGPTTeamAPI()
                api.start()
                api_owns = True
            except Exception as exc:
                out["skipped_reason"] = f"chatgpt_api_start_failed:{type(exc).__name__}"
                return out
        try:
            healthy, reason, evidence = is_master_subscription_healthy(api)
        except Exception as exc:
            out["skipped_reason"] = f"probe_exception:{type(exc).__name__}"
            return out

        # Round 12 wire-up (C2) — feed every retroactive helper probe into
        # WorkspacePool so connect 3 unhealthy → auto failover gets a chance
        # to trigger.  apply_pool_health_signal swallows all exceptions
        # internally (M-I1 alignment), so it cannot break the helper.
        try:
            apply_pool_health_signal(healthy, reason, evidence)
        except Exception as exc:  # pragma: no cover — defensive double-net
            logger.warning(
                "[master_health] retroactive helper: pool signal failed: %s", exc,
            )
    finally:
        if api_owns and api is not None:
            try:
                api.stop()
            except Exception:
                pass

    # 2. 解析 master account_id
    if workspace_id:
        master_aid = workspace_id
    else:
        try:
            from autoteam.admin_state import get_chatgpt_account_id

            master_aid = (evidence or {}).get("account_id") or get_chatgpt_account_id() or ""
        except Exception:
            master_aid = (evidence or {}).get("account_id") or ""

    if not master_aid:
        out["skipped_reason"] = "no_master_account_id"
        return out

    now_ts = time.time()

    # 3. 路径分流(Round 11):healthy 包括 active + subscription_grace 都走撤回 GRACE → ACTIVE
    if healthy and reason in ("active", "subscription_grace"):
        # 撤回路径:GRACE → ACTIVE(母号续费 / 仍在 grace 期内权益有效)
        try:
            for acc in load_accounts():
                if acc.get("status") != STATUS_DEGRADED_GRACE:
                    continue
                if (acc.get("master_account_id_at_grace") or "") != master_aid:
                    continue
                email = acc.get("email")
                if dry_run:
                    out["reverted_active"].append(email)
                    continue
                try:
                    update_account(
                        email,
                        status=STATUS_ACTIVE,
                        grace_until=None,
                        grace_marked_at=None,
                        master_account_id_at_grace=None,
                    )
                    out["reverted_active"].append(email)
                except Exception as exc:
                    out["errors"].append({"email": email, "stage": "revert", "error": str(exc)})
        except Exception as exc:
            out["errors"].append({"stage": "load_for_revert", "error": str(exc)})
        if out["reverted_active"]:
            logger.info(
                "[retroactive] master %s,GRACE → ACTIVE 撤回 %d 个",
                reason,
                len(out["reverted_active"]),
            )
        else:
            # Round 11 — skipped_reason 命名兼容 Round 9(active)+ Round 11(grace)
            out["skipped_reason"] = "master_active_no_grace_candidates"
        return out

    if reason != "subscription_cancelled":
        out["skipped_reason"] = f"master reason={reason} 非 cancelled,无需重分类"
        return out

    # subscription_cancelled — 前进路径
    try:
        accounts_now = load_accounts()
    except Exception as exc:
        out["skipped_reason"] = f"load_accounts_failed:{type(exc).__name__}"
        return out

    for acc in accounts_now:
        try:
            email = acc.get("email")
            cur_status = acc.get("status")
            cur_ws = acc.get("workspace_account_id") or ""

            # GRACE 到期检查:无论 workspace 是否一致,先处理已经标 GRACE 的
            if cur_status == STATUS_DEGRADED_GRACE:
                acc_grace_until = acc.get("grace_until")
                if acc_grace_until and now_ts >= float(acc_grace_until):
                    if dry_run:
                        out["marked_standby"].append(email)
                    else:
                        update_account(
                            email,
                            status=STATUS_STANDBY,
                            grace_until=None,
                            grace_marked_at=None,
                            # master_account_id_at_grace 保留供审计
                        )
                        out["marked_standby"].append(email)
                continue

            # 前进路径:仅 ACTIVE / EXHAUSTED 且属于此降级 workspace
            if cur_status not in (STATUS_ACTIVE, STATUS_EXHAUSTED):
                continue
            if cur_ws != master_aid:
                continue

            # 解析 grace_until — 优先用入参,否则从 auth_file id_token 解
            acc_grace_until = grace_until
            if acc_grace_until is None:
                _, id_token = _read_access_token_from_auth_file(acc.get("auth_file"))
                acc_grace_until = extract_grace_until_from_jwt(id_token)

            # 决策:
            #  - grace_until 解析成功 + 仍未过期 → 进 GRACE
            #  - grace_until 解析失败  → 没法判断,进保守 STANDBY
            #  - grace_until 已过期    → 直接 STANDBY,跳 GRACE
            target_status = None
            if acc_grace_until and now_ts < float(acc_grace_until):
                target_status = STATUS_DEGRADED_GRACE
            else:
                target_status = STATUS_STANDBY

            if dry_run:
                if target_status == STATUS_DEGRADED_GRACE:
                    out["marked_grace"].append(email)
                else:
                    out["marked_standby"].append(email)
                continue

            if target_status == STATUS_DEGRADED_GRACE:
                update_account(
                    email,
                    status=STATUS_DEGRADED_GRACE,
                    grace_until=float(acc_grace_until),
                    grace_marked_at=now_ts,
                    master_account_id_at_grace=master_aid,
                )
                try:
                    record_failure(
                        email,
                        MASTER_SUBSCRIPTION_DEGRADED,
                        "retroactive: master cancelled, in grace period",
                        stage="apply_master_degraded_classification",
                        master_account_id=master_aid,
                        grace_until=float(acc_grace_until),
                    )
                except Exception:
                    pass
                out["marked_grace"].append(email)
            else:
                update_account(
                    email,
                    status=STATUS_STANDBY,
                    grace_until=None,
                    grace_marked_at=None,
                )
                try:
                    record_failure(
                        email,
                        MASTER_SUBSCRIPTION_DEGRADED,
                        "retroactive: master cancelled, no grace period (jwt missing/expired)",
                        stage="apply_master_degraded_classification",
                        master_account_id=master_aid,
                    )
                except Exception:
                    pass
                out["marked_standby"].append(email)
        except Exception as exc:
            out["errors"].append({
                "email": acc.get("email"),
                "stage": "classify",
                "error": f"{type(exc).__name__}:{exc}",
            })

    if not (out["marked_grace"] or out["marked_standby"]):
        out["skipped_reason"] = "no_candidates"

    if out["marked_grace"] or out["marked_standby"]:
        logger.info(
            "[retroactive] master cancelled — 标 GRACE %d 个 / STANDBY %d 个 (dry_run=%s)",
            len(out["marked_grace"]),
            len(out["marked_standby"]),
            dry_run,
        )
    return out
