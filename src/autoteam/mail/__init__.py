"""Mail provider 工厂 + 向后兼容别名。

调用方继续用:
    from autoteam.mail import CloudMailClient
    client = CloudMailClient()  # 实际由 MAIL_PROVIDER 决定 provider

新代码也可以用更明确的:
    from autoteam.mail import get_mail_client
    client = get_mail_client()

Round 12 S2:支持 MAIL_PROVIDER_CHAIN 多 provider 失败回退链。
    MAIL_PROVIDER_CHAIN=maillab,addy_io,simplelogin,cf_temp_email
设置后,`get_mail_client()` 返回 `FallbackMailProvider`,按优先级失败降级。
未设置时保留旧行为(单 provider,完全向后兼容)。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from autoteam.mail.base import Account, Email, MailProvider

logger = logging.getLogger(__name__)

__all__ = [
    "Account",
    "CloudMailClient",
    "Email",
    "MailProvider",
    "get_mail_client",
]


def _resolve_provider_factory(name: str) -> Callable[[], MailProvider]:
    """name → factory(无副作用,只导入对应模块返回类)。

    抛 `ValueError` 当 name 不识别。具体 provider `__init__` 中的配置缺失
    检查会在 factory 调用时(不在这里)抛 `MailProviderUnavailable`,以便
    fallback 链跳过。
    """
    raw = (name or "").strip().lower()
    if raw in ("cf_temp_email", "cloudflare_temp_email"):
        from autoteam.mail.cf_temp_email import CfTempEmailClient
        return CfTempEmailClient
    if raw == "maillab":
        from autoteam.mail.maillab import MaillabClient
        return MaillabClient
    if raw in ("addy_io", "addy", "anonaddy"):
        from autoteam.mail.addy_io import AddyIoClient
        return AddyIoClient
    if raw in ("simplelogin", "sl"):
        from autoteam.mail.simplelogin import SimpleLoginClient
        return SimpleLoginClient
    raise ValueError(
        f"未知 mail provider name={name!r}"
        " (可选: cf_temp_email | maillab | addy_io | simplelogin)"
    )


def _build_chain_from_env(chain_env: str) -> MailProvider:
    """解析 MAIL_PROVIDER_CHAIN 字符串,返回 FallbackMailProvider。"""
    from autoteam.mail.fallback import FallbackMailProvider

    names = [n.strip() for n in chain_env.split(",") if n.strip()]
    if not names:
        raise ValueError("MAIL_PROVIDER_CHAIN 解析后为空")

    providers: list[tuple[str, Callable[[], MailProvider]]] = []
    for name in names:
        try:
            factory = _resolve_provider_factory(name)
        except ValueError as exc:
            logger.warning("[mail-factory] %s,跳过", exc)
            continue
        providers.append((name.lower(), factory))

    if not providers:
        raise ValueError(f"MAIL_PROVIDER_CHAIN={chain_env!r} 解析后无任何已知 provider")

    logger.info("[mail-factory] 启用 fallback 链: %s", [n for n, _ in providers])
    return FallbackMailProvider(providers)


def get_mail_client() -> MailProvider:
    """根据环境变量返回对应 provider 实例。

    优先级:
      1. `MAIL_PROVIDER_CHAIN`(逗号分隔多 provider) → FallbackMailProvider
      2. `MAIL_PROVIDER` 单值                       → 单 provider 实例(向后兼容)
      3. 默认 cf_temp_email

    单 provider 模式下 provider 名拼写错误抛 `ValueError`。
    fallback 模式下未知 provider 名跳过(只警告)。
    """
    chain_env = (os.environ.get("MAIL_PROVIDER_CHAIN") or "").strip()
    if chain_env:
        return _build_chain_from_env(chain_env)

    raw = (os.environ.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    if raw in ("cf_temp_email", "cloudflare_temp_email", ""):
        from autoteam.mail.cf_temp_email import CfTempEmailClient
        return CfTempEmailClient()
    if raw == "maillab":
        from autoteam.mail.maillab import MaillabClient
        return MaillabClient()
    if raw in ("addy_io", "addy", "anonaddy"):
        from autoteam.mail.addy_io import AddyIoClient
        return AddyIoClient()
    if raw in ("simplelogin", "sl"):
        from autoteam.mail.simplelogin import SimpleLoginClient
        return SimpleLoginClient()
    raise ValueError(
        f"未知 MAIL_PROVIDER={raw!r}"
        " (可选: cf_temp_email | maillab | addy_io | simplelogin;"
        " 多 provider 用 MAIL_PROVIDER_CHAIN)"
    )


# 历史 47 处对 `CloudMailClient()` 的调用零改动 — 工厂返回 provider 实例,
# `CloudMailClient()` 语法等价于 `get_mail_client()`。
CloudMailClient = get_mail_client
