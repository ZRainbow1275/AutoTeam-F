"""Addy.io 自托管 alias forwarding 后端的 MailProvider 实现。

Addy.io(原 anonaddy)是开源的 email alias forwarding 服务,自托管时支持自定义域名,
其 alias 域不在公开 disposable email blocklist,因此可绕过 OpenAI/ChatGPT 的邮箱黑名单。

API 参考(基于 Addy.io 官方文档,自托管与公网版路径一致):
  - Auth: `Authorization: Bearer <token>` + `Accept: application/json`
          + `X-Requested-With: XMLHttpRequest`(防 CSRF 守卫)
  - 创建 alias:POST /api/v1/aliases body
        {domain, format: "uuid"|"random_words"|"random_characters"|"custom",
         local_part?, description?, recipient_ids?:[]}
  - 列出 alias:GET /api/v1/aliases?filter[active]=true&page[size]=N
  - 删除 alias:DELETE /api/v1/aliases/{id}(软删除,deleted_at)
  - alias 详情:GET /api/v1/aliases/{id}

⚠️ Addy.io 是 alias forwarding,**没有内置 inbox**,邮件转发到底层真实邮箱
   (用户的 ProtonMail / 自托管 Postfix 等)。因此本 provider 的读邮箱方法
   `search_emails_by_recipient` / `list_emails` / `delete_emails_for` / `get_latest_emails`
   返回 `[]` + warning,不抛异常,以便 fallback 链不对读路径误判降级。
   实际读邮箱需配对 reader provider(S4 子任务)。
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import requests

from autoteam.mail.base import MailProvider, normalize_email_addr
from autoteam.mail.fallback import MailProviderUnavailable

logger = logging.getLogger(__name__)


# Addy.io alias 格式枚举(API 字段值)
ALIAS_FORMAT_UUID = "uuid"
ALIAS_FORMAT_CUSTOM = "custom"
ALIAS_FORMAT_RANDOM_CHARS = "random_characters"


class AddyIoClient(MailProvider):
    """Addy.io 自托管 alias forwarding 客户端。"""

    provider_name = "addy_io"

    def __init__(self):
        base_url = (os.environ.get("ADDY_IO_BASE_URL") or "").rstrip("/")
        token = os.environ.get("ADDY_IO_TOKEN") or ""
        domain = (os.environ.get("ADDY_IO_DOMAIN") or "").lstrip("@").strip()

        missing = [k for k, v in (
            ("ADDY_IO_BASE_URL", base_url),
            ("ADDY_IO_TOKEN", token),
            ("ADDY_IO_DOMAIN", domain),
        ) if not v]
        if missing:
            raise MailProviderUnavailable(
                f"Addy.io 配置缺失: {', '.join(missing)} — fallback 链将跳过此 provider"
            )

        self.base_url: str = base_url
        self.token: str = token
        self.domain: str = domain
        self.session: requests.Session = requests.Session()

    # ------------------------------------------------------------------ http
    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        return self.session.request(
            method,
            self._url(path),
            headers=self._headers(),
            timeout=30,
            **kw,
        )

    @staticmethod
    def _parse_json(r: requests.Response, path: str) -> dict:
        if r.status_code in (401, 403):
            raise Exception(
                f"Addy.io {path} 鉴权失败 HTTP {r.status_code}: 检查 ADDY_IO_TOKEN"
            )
        if r.status_code == 404:
            raise Exception(f"Addy.io {path} 路由 404: 检查 ADDY_IO_BASE_URL 是否指向 Addy.io 实例")
        if r.status_code >= 500:
            raise Exception(f"Addy.io {path} 服务端错误 HTTP {r.status_code}: {(r.text or '')[:200]}")
        if r.status_code not in (200, 201, 204):
            raise Exception(f"Addy.io {path} HTTP {r.status_code}: {(r.text or '')[:200]}")
        if r.status_code == 204 or not (r.text or "").strip():
            return {}
        try:
            return r.json() or {}
        except Exception as exc:
            raise Exception(f"Addy.io {path} 响应非 JSON: {exc}") from exc

    # ------------------------------------------------------------------ auth
    def login(self) -> str:
        """Addy.io 用 long-lived API token,无需 login。本方法做一次 token 校验。"""
        r = self._request("GET", "/api/v1/aliases", params={"page[size]": 1})
        self._parse_json(r, "/api/v1/aliases")
        logger.info("[addy_io] token 校验通过 (base=%s, domain=%s)", self.base_url, self.domain)
        return self.token

    # ------------------------------------------------------------------ aliases
    @staticmethod
    def _sanitize_local_part(prefix: str | None) -> str:
        """Addy.io local_part 字符集与 RFC 5321 一致;保守白名单。"""
        if not prefix:
            return uuid.uuid4().hex[:10]
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(prefix)).strip(".-_")
        return cleaned[:60] or uuid.uuid4().hex[:10]

    def create_temp_email(
        self, prefix: str | None = None, domain: str | None = None
    ) -> tuple[int | str, str]:
        """POST /api/v1/aliases — 创建一个 alias,返回 (alias_id, email)。

        - prefix 提供 → format=custom + local_part=prefix
        - prefix 缺省 → format=uuid(Addy.io 服务端生成)
        """
        target_domain = (domain or self.domain).lstrip("@").strip()
        if not target_domain:
            raise Exception("Addy.io 创建 alias 失败: domain 不能为空")

        local_part = self._sanitize_local_part(prefix)
        body: dict = {
            "domain": target_domain,
            "format": ALIAS_FORMAT_CUSTOM if prefix else ALIAS_FORMAT_UUID,
            "description": f"autoteam-{local_part}",
        }
        if prefix:
            body["local_part"] = local_part

        r = self._request("POST", "/api/v1/aliases", json=body)
        data = self._parse_json(r, "/api/v1/aliases")
        alias = data.get("data") or data  # Addy.io 返回 {data: {...}}
        if not isinstance(alias, dict):
            raise Exception(f"Addy.io 创建 alias 响应异常: {data!r}")

        alias_id = alias.get("id") or alias.get("uuid")
        email = alias.get("email") or alias.get("address")
        if not alias_id or not email:
            raise Exception(f"Addy.io 创建 alias 响应缺 id/email: {alias!r}")

        logger.info("[addy_io] alias 已创建: %s (id=%s)", email, alias_id)
        return alias_id, email

    def list_accounts(self, size: int = 200) -> list[dict]:
        """GET /api/v1/aliases?filter[active]=true&page[size]=N — 列出 alias。"""
        r = self._request(
            "GET",
            "/api/v1/aliases",
            params={"filter[active]": "true", "page[size]": min(int(size or 100), 100)},
        )
        data = self._parse_json(r, "/api/v1/aliases")
        rows = data.get("data") or []
        if not isinstance(rows, list):
            return []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append({
                "accountId": row.get("id") or row.get("uuid"),
                "email": row.get("email") or row.get("address"),
                "password": None,
                "createTime": None,
                "description": row.get("description"),
                "active": row.get("active", True),
                "raw": row,
            })
        return out

    def delete_account(self, account_id: int | str) -> dict:
        """DELETE /api/v1/aliases/{id} — 软删除。account_id 可以是 id 或 email,后者自动解析。"""
        real_id = self._resolve_alias_id(account_id)
        if not real_id:
            logger.warning("[addy_io] delete_account: 找不到对应 alias (%s)", account_id)
            return {"code": 404, "message": "alias not found"}

        r = self._request("DELETE", f"/api/v1/aliases/{real_id}")
        if r.status_code in (200, 204):
            logger.info("[addy_io] alias 已删除 (id=%s)", real_id)
            return {"code": 200}
        try:
            self._parse_json(r, f"/api/v1/aliases/{real_id}")
        except Exception as exc:
            return {"code": r.status_code, "message": str(exc)}
        return {"code": r.status_code, "message": (r.text or "")[:200]}

    def _resolve_alias_id(self, value: int | str) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if "@" not in text:
            return text
        target = normalize_email_addr(text)
        for row in self.list_accounts(size=200):
            if normalize_email_addr(row.get("email")) == target:
                aid = row.get("accountId")
                return str(aid) if aid else None
        return None

    # ------------------------------------------------------------------ inbox (no-op)
    def search_emails_by_recipient(
        self, to_email: str, size: int = 10, account_id: int | str | None = None
    ) -> list[dict]:
        logger.warning(
            "[addy_io] search_emails_by_recipient(%s) — alias forwarding 服务无内置 inbox,"
            "需配对 reader provider 才能读邮件",
            to_email,
        )
        return []

    def list_emails(self, account_id: int | str, size: int = 10) -> list[dict]:
        logger.warning("[addy_io] list_emails(%s) — alias forwarding 无 inbox", account_id)
        return []

    def delete_emails_for(self, to_email: str) -> int:
        logger.warning("[addy_io] delete_emails_for(%s) — alias forwarding 无 inbox", to_email)
        return 0
