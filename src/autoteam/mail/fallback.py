"""Fallback mail provider chain — Tier-priority failover.

设计要点(详见 `.trellis/tasks/05-11-s2-mail-provider-fallback-gmx/prd.md` Q3-Q5):

1. **Lazy factory**: 接收 `[(name, factory), ...]`,延迟构造。任一 provider `__init__`
   抛 `MailProviderUnavailable`(配置缺失)时,fallback 链自动跳过该项,不污染
   失败计数 — 配置缺失是用户主动放弃,而非业务失败。

2. **失败计数**: 状态文件 `mail_provider_state.json`(项目根),schema:
       {"provider_name": {"fail_count": int, "last_fail_ts": float}}
   连续失败 ≥ MAIL_PROVIDER_MAX_FAILURES(默认 3)→ blocked。

3. **Cooldown 自动重置**: 单 provider blocked 后,`last_fail_ts + cooldown_secs`
   (默认 24h)过后,下次访问时 lazy reset(读时检查 ts 差,自动清零)。

4. **聚合异常**: 全部 provider 都失败 → 抛 `MailProviderChainExhausted`,内含
   每个 provider 的最后一条错误。

5. **不抛只 warn 的方法**: alias forwarding 类 provider(addy_io / simplelogin)
   的 read 方法返回 `[]` 不抛,因此 fallback 链对读路径不会过度降级。
   抛异常的方法(login / create_temp_email / delete_account)才会触发 failover。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autoteam.mail.base import MailProvider
from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- exceptions


class MailProviderUnavailable(Exception):
    """provider 配置不完整(env 缺失等)— 静默跳过,不计入失败计数。"""


class MailProviderChainExhausted(Exception):
    """fallback 链上所有 provider 都失败 — 业务层应直接放弃此操作。"""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.errors: dict[str, str] = errors or {}


# ----------------------------------------------------------------- state file


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_STATE_FILE = PROJECT_ROOT / "mail_provider_state.json"
DEFAULT_MAX_FAILURES = 3
DEFAULT_COOLDOWN_SECS = 24 * 3600


class _FailureTracker:
    """JSON 文件持久化的 per-provider 失败计数器。

    并发: `_LOCK` 串行化所有读改写,与 `register_failures.py` 的 `_LOCK`
    设计一致;单进程足够,多进程并发场景下文件锁由调用方负责。
    """

    def __init__(
        self,
        state_file: Path | str | None = None,
        max_failures: int = DEFAULT_MAX_FAILURES,
        cooldown_secs: int = DEFAULT_COOLDOWN_SECS,
    ):
        self.state_file: Path = Path(state_file) if state_file else DEFAULT_STATE_FILE
        self.max_failures = max(1, int(max_failures))
        self.cooldown_secs = max(1, int(cooldown_secs))
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.state_file.exists():
            return {}
        try:
            raw = read_text(self.state_file).strip()
            if not raw:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("[mail-fallback] 状态文件 %s 解析失败,重置为空: %s", self.state_file, exc)
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        try:
            write_text(self.state_file, json.dumps(data, indent=2, ensure_ascii=False))
            try:
                os.chmod(self.state_file, 0o666)
            except Exception:
                pass
        except Exception as exc:
            logger.error("[mail-fallback] 状态文件 %s 写入失败: %s", self.state_file, exc)

    def _is_cooldown_expired(self, entry: dict[str, Any], now: float) -> bool:
        last_ts = float(entry.get("last_fail_ts") or 0)
        return last_ts > 0 and (now - last_ts) >= self.cooldown_secs

    def is_blocked(self, name: str) -> bool:
        """检查 provider 是否处于 blocked 状态(连续失败超阈值且未过 cooldown)。"""
        with self._lock:
            data = self._load()
            entry = data.get(name)
            if not entry:
                return False
            now = time.time()
            if self._is_cooldown_expired(entry, now):
                data.pop(name, None)
                self._save(data)
                logger.info("[mail-fallback] provider=%s cooldown 已过,失败计数自动重置", name)
                return False
            fail_count = int(entry.get("fail_count") or 0)
            return fail_count >= self.max_failures

    def record_failure(self, name: str, error: str = "") -> int:
        """记录一次失败,返回当前累计失败次数。"""
        with self._lock:
            data = self._load()
            entry = data.get(name) or {}
            now = time.time()
            if self._is_cooldown_expired(entry, now):
                entry = {}
            fail_count = int(entry.get("fail_count") or 0) + 1
            data[name] = {
                "fail_count": fail_count,
                "last_fail_ts": now,
                "last_error": (error or "")[:300],
            }
            self._save(data)
            logger.warning(
                "[mail-fallback] provider=%s 失败 %d/%d: %s",
                name,
                fail_count,
                self.max_failures,
                (error or "")[:120],
            )
            return fail_count

    def record_success(self, name: str) -> None:
        """业务成功后重置失败计数。"""
        with self._lock:
            data = self._load()
            if name in data:
                data.pop(name, None)
                self._save(data)
                logger.info("[mail-fallback] provider=%s 业务成功,失败计数已重置", name)


# ----------------------------------------------------------------- fallback chain


_DISPATCH_METHODS_REQUIRE_FAILOVER = (
    "login",
    "create_temp_email",
    "list_accounts",
    "delete_account",
    "search_emails_by_recipient",
    "list_emails",
    "delete_emails_for",
    "get_latest_emails",
)


class FallbackMailProvider(MailProvider):
    """按优先级尝试一组 mail provider,失败自动降级到下一个。

    Args:
        providers: `[(name, factory), ...]`。factory 是 `Callable[[], MailProvider]`。
                   factory 抛 `MailProviderUnavailable` 时跳过(不计失败计数);
                   抛其他 Exception 视为构造失败,计入失败计数。
        tracker:   失败计数追踪器。None 时使用默认全局状态文件 + 默认阈值。

    Usage:
        chain = FallbackMailProvider([
            ("maillab", MaillabClient),
            ("addy_io", AddyIoClient),
            ("simplelogin", SimpleLoginClient),
            ("cf_temp_email", CfTempEmailClient),
        ])
        chain.create_temp_email(prefix="autoteam")  # 自动 dispatch
    """

    provider_name = "fallback"

    def __init__(
        self,
        providers: list[tuple[str, Callable[[], MailProvider]]],
        tracker: _FailureTracker | None = None,
    ):
        if not providers:
            raise ValueError("FallbackMailProvider 至少需要一个 provider")
        self._providers: list[tuple[str, Callable[[], MailProvider]]] = list(providers)
        self._tracker = tracker or _FailureTracker()
        # name → 已实例化的 provider(lazy)
        self._instances: dict[str, MailProvider] = {}
        # 当前 active provider 名(供日志/UI 展示)
        self._current_name: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------- pub
    @property
    def current_provider_name(self) -> str | None:
        """当前正在使用(或最近一次成功使用)的 provider name。"""
        return self._current_name

    @property
    def configured_chain(self) -> list[str]:
        """链上所有 provider 名(按优先级)。"""
        return [name for name, _ in self._providers]

    # ------------------------------------------------------------------- private
    def _get_or_create(self, name: str, factory: Callable[[], MailProvider]) -> MailProvider | None:
        """返回 provider 实例;不可用(构造失败 / blocked)时返回 None。"""
        with self._lock:
            if name in self._instances:
                return self._instances[name]
        if self._tracker.is_blocked(name):
            logger.info("[mail-fallback] provider=%s 处于 blocked 状态,跳过", name)
            return None
        try:
            instance = factory()
        except MailProviderUnavailable as exc:
            logger.info("[mail-fallback] provider=%s 配置不可用,跳过: %s", name, exc)
            return None
        except Exception as exc:
            self._tracker.record_failure(name, f"__init__: {exc}")
            return None
        with self._lock:
            self._instances[name] = instance
        return instance

    def _dispatch(self, method_name: str, *args, **kwargs):
        """按优先级遍历 provider,首个成功的返回结果;全部失败抛聚合异常。"""
        errors: dict[str, str] = {}
        last_error: Exception | None = None

        for name, factory in self._providers:
            instance = self._get_or_create(name, factory)
            if instance is None:
                errors[name] = "unavailable_or_blocked"
                continue

            method = getattr(instance, method_name, None)
            if method is None or not callable(method):
                errors[name] = f"method {method_name} not implemented"
                continue

            try:
                result = method(*args, **kwargs)
            except Exception as exc:
                self._tracker.record_failure(name, f"{method_name}: {exc}")
                errors[name] = f"{type(exc).__name__}: {exc}"
                last_error = exc
                # drop instance — 下次重新 init,避开半坏状态
                with self._lock:
                    self._instances.pop(name, None)
                continue

            # 成功:重置失败计数 + 记录 active
            self._tracker.record_success(name)
            with self._lock:
                self._current_name = name
            return result

        # 全部失败
        msg = f"mail provider chain exhausted ({method_name}): {errors}"
        logger.error("[mail-fallback] %s", msg)
        raise MailProviderChainExhausted(msg, errors=errors) from last_error

    # ------------------------------------------------------------------- ABC

    def login(self) -> str:
        return self._dispatch("login")

    def create_temp_email(
        self, prefix: str | None = None, domain: str | None = None
    ) -> tuple[int | str, str]:
        return self._dispatch("create_temp_email", prefix, domain)

    def list_accounts(self, size: int = 200) -> list[dict]:
        return self._dispatch("list_accounts", size)

    def delete_account(self, account_id: int | str) -> dict:
        return self._dispatch("delete_account", account_id)

    def search_emails_by_recipient(
        self, to_email: str, size: int = 10, account_id: int | str | None = None
    ) -> list[dict]:
        return self._dispatch("search_emails_by_recipient", to_email, size, account_id)

    def list_emails(self, account_id: int | str, size: int = 10) -> list[dict]:
        return self._dispatch("list_emails", account_id, size)

    def delete_emails_for(self, to_email: str) -> int:
        return self._dispatch("delete_emails_for", to_email)

    def get_latest_emails(
        self, account_id: int | str, email_id: int = 0, all_receive: int = 0
    ) -> list[dict]:
        return self._dispatch("get_latest_emails", account_id, email_id, all_receive)


__all__ = [
    "DEFAULT_COOLDOWN_SECS",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_STATE_FILE",
    "FallbackMailProvider",
    "MailProviderChainExhausted",
    "MailProviderUnavailable",
    "_FailureTracker",
]
