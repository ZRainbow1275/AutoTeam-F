"""Alias + Reader 配对 MailProvider — 兼容 alias forwarding 注册场景。

Addy.io / SimpleLogin 这类 alias forwarding 服务的 alias 域不在 OpenAI 公开 disposable
黑名单,适合用作注册时的"未黑域名"邮箱来源,但**没有内置 inbox** —— 收到的邮件会被
转发到底层真实邮箱(用户的 ProtonMail / 自托管 Postfix / maillab / cf 等)。

`AliasWithReaderProvider` 把两个 provider 组合成一个 `MailProvider`,对外暴露统一接口:

- 写入路径(`create_temp_email` / `delete_account` / `list_accounts`)走 **alias**:
  拿到不在黑名单的临时邮箱
- 读取路径(`search_emails_by_recipient` / `list_emails` / `delete_emails_for` /
  `wait_for_email` / `get_latest_emails`)走 **reader**:在底层真实邮箱里查 OpenAI
  转发过来的 OTP / 邀请邮件

⚠️ 关键假设(运维侧配置,不在代码内):
   用户已在 Addy.io / SimpleLogin 控制台把 alias 的转发目标设为 reader 关联的真实邮箱
   (例如 maillab 创建的子邮箱地址)。本 provider 不负责验证转发链路,只负责调用对接。

详见 S4 PRD §Q3 与 S2 PRD §Q2(alias forwarding 留白)。
"""

from __future__ import annotations

import logging
from typing import Any

from autoteam.mail.base import MailProvider

logger = logging.getLogger(__name__)


class AliasWithReaderProvider(MailProvider):
    """组合一个 alias provider(写)+ 一个 reader provider(读)。

    Args:
        alias:  实现 `create_temp_email` / `delete_account` / `list_accounts`
                能拿到不在 OpenAI 黑名单的邮箱地址(如 Addy.io / SimpleLogin)。
        reader: 实现 `search_emails_by_recipient` / `list_emails` / `delete_emails_for`
                能读到 alias 转发过来的邮件(如 maillab / cf_temp_email / IMAP 自建)。

    Usage:
        pair = AliasWithReaderProvider(alias=AddyIoClient(), reader=MaillabClient())
        acc_id, email = pair.create_temp_email(prefix="autoteam")
        # OpenAI 注册 → OTP 邮件被转发到 reader 的真实邮箱
        otp_email = pair.wait_for_email(email, timeout=180, sender_keyword="openai")
    """

    provider_name = "alias_with_reader"

    def __init__(self, alias: MailProvider, reader: MailProvider):
        if alias is None or reader is None:
            raise ValueError("AliasWithReaderProvider 需要同时提供 alias 与 reader 两个 provider")
        self.alias: MailProvider = alias
        self.reader: MailProvider = reader
        # 复合名:便于日志 / cache_key 区分
        self.provider_name = (
            f"alias_with_reader[{getattr(alias, 'provider_name', 'alias')}+"
            f"{getattr(reader, 'provider_name', 'reader')}]"
        )

    # ------------------------------------------------------------------ auth
    def login(self) -> str:
        """同时初始化 alias 与 reader,任一抛异常视为整体不可用。

        返回值仅用于"是否成功登录"的简单语义,**不**包含任何真实 token —
        历史版本曾把两个 provider 的 token 用 ``|`` 拼接返回,有调用方误存到
        accounts.json / log 的风险(round-12 wire-up audit minor m1)。
        现在改为返回固定 ``"ok"`` 字面量,token 仅在 provider 实例内部保持。
        """
        self.alias.login()
        self.reader.login()
        logger.info(
            "[alias_with_reader] login OK (alias=%s, reader=%s)",
            getattr(self.alias, "provider_name", "alias"),
            getattr(self.reader, "provider_name", "reader"),
        )
        return "ok"

    # ------------------------------------------------------------------ write → alias
    def create_temp_email(
        self, prefix: str | None = None, domain: str | None = None
    ) -> tuple[int | str, str]:
        return self.alias.create_temp_email(prefix=prefix, domain=domain)

    def delete_account(self, account_id: int | str) -> dict:
        return self.alias.delete_account(account_id)

    def list_accounts(self, size: int = 200) -> list[dict]:
        return self.alias.list_accounts(size=size)

    # ------------------------------------------------------------------ read → reader
    def search_emails_by_recipient(
        self, to_email: str, size: int = 10, account_id: int | str | None = None
    ) -> list[dict]:
        return self.reader.search_emails_by_recipient(
            to_email, size=size, account_id=account_id
        )

    def list_emails(self, account_id: int | str, size: int = 10) -> list[dict]:
        return self.reader.list_emails(account_id, size=size)

    def delete_emails_for(self, to_email: str) -> int:
        return self.reader.delete_emails_for(to_email)

    def get_latest_emails(
        self, account_id: int | str, email_id: int = 0, all_receive: int = 0
    ) -> list[dict]:
        return self.reader.get_latest_emails(account_id, email_id=email_id, all_receive=all_receive)

    # ------------------------------------------------------------------ shared (text 工具) — 用 reader 的实现
    def extract_verification_code(self, email_data: dict) -> str | None:
        return self.reader.extract_verification_code(email_data)

    def extract_invite_link(self, email_data: dict) -> str | None:
        return self.reader.extract_invite_link(email_data)

    def wait_for_email(
        self,
        to_email: str,
        timeout: int | None = None,
        sender_keyword: str | None = None,
    ) -> dict:
        # 显式委派 reader,避免 base.wait_for_email 默认实现回头调本类的
        # search_emails_by_recipient(本类已委派到 reader,效果一致,但语义更明确)。
        return self.reader.wait_for_email(to_email, timeout=timeout, sender_keyword=sender_keyword)

    # ------------------------------------------------------------------ introspection
    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"AliasWithReaderProvider("
            f"alias={getattr(self.alias, 'provider_name', type(self.alias).__name__)}, "
            f"reader={getattr(self.reader, 'provider_name', type(self.reader).__name__)})"
        )

    def describe(self) -> dict[str, Any]:
        """供日志 / UI 展示的结构化描述。"""
        return {
            "type": "alias_with_reader",
            "alias": getattr(self.alias, "provider_name", type(self.alias).__name__),
            "reader": getattr(self.reader, "provider_name", type(self.reader).__name__),
        }


__all__ = ["AliasWithReaderProvider"]
