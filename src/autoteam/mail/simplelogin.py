"""SimpleLogin SaaS / 自托管 alias forwarding 后端的 MailProvider 实现。

SimpleLogin(被 Proton 收购)是 alias forwarding 服务,Premium 版($4/月)无限 alias。
其 alias 域不在公开 disposable email blocklist,因此可绕过 OpenAI 邮箱黑名单。

API 参考(基于 SimpleLogin 官方 API 文档):
  - Auth: `Authentication: <api_key>` header(注意拼写不是 Authorization)
  - 创建 random alias:GET /api/alias/random/new?hostname=<host>
        body 可选: {note, mode: "uuid"|"word"}
  - 创建 custom alias:POST /api/v3/alias/custom/new?hostname=<host>
        body: {alias_prefix, signed_suffix, mailbox_ids: [int]}
  - 列出 alias:GET /api/v2/aliases?page_id=N
  - alias 详情:GET /api/aliases/{id}
  - 删除 alias:DELETE /api/aliases/{id}

⚠️ SimpleLogin 是 alias forwarding,**没有内置 inbox**,行为与 Addy.io 一致。
   读邮箱方法 `[]` + warning。
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


DEFAULT_BASE_URL = "https://app.simplelogin.io"


class SimpleLoginClient(MailProvider):
    """SimpleLogin alias forwarding 客户端。"""

    provider_name = "simplelogin"

    def __init__(self):
        api_key = os.environ.get("SIMPLELOGIN_API_KEY") or ""
        if not api_key:
            raise MailProviderUnavailable(
                "SimpleLogin 配置缺失: SIMPLELOGIN_API_KEY — fallback 链将跳过此 provider"
            )

        self.api_key: str = api_key
        self.base_url: str = (os.environ.get("SIMPLELOGIN_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.hostname: str = (os.environ.get("SIMPLELOGIN_HOSTNAME") or "openai.com").strip()
        self.session: requests.Session = requests.Session()

    # ------------------------------------------------------------------ http
    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authentication": self.api_key,  # SimpleLogin 自有 header 名
            "Accept": "application/json",
            "Content-Type": "application/json",
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
            raise Exception(f"SimpleLogin {path} 鉴权失败 HTTP {r.status_code}: 检查 SIMPLELOGIN_API_KEY")
        if r.status_code == 404:
            raise Exception(f"SimpleLogin {path} 路由 404: 检查 SIMPLELOGIN_BASE_URL")
        if r.status_code >= 500:
            raise Exception(f"SimpleLogin {path} 服务端错误 HTTP {r.status_code}")
        if r.status_code not in (200, 201, 204):
            raise Exception(f"SimpleLogin {path} HTTP {r.status_code}: {(r.text or '')[:200]}")
        if r.status_code == 204 or not (r.text or "").strip():
            return {}
        try:
            return r.json() or {}
        except Exception as exc:
            raise Exception(f"SimpleLogin {path} 响应非 JSON: {exc}") from exc

    # ------------------------------------------------------------------ auth
    def login(self) -> str:
        """SimpleLogin 用 long-lived API key,无需 login。本方法做一次 token 校验。"""
        r = self._request("GET", "/api/user_info")
        data = self._parse_json(r, "/api/user_info")
        email = data.get("email") if isinstance(data, dict) else None
        logger.info("[simplelogin] API key 校验通过 (user=%s, base=%s)", email or "?", self.base_url)
        return self.api_key

    # ------------------------------------------------------------------ aliases
    @staticmethod
    def _sanitize_alias_prefix(prefix: str | None) -> str:
        if not prefix:
            return uuid.uuid4().hex[:10]
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(prefix)).strip(".-_")
        return cleaned[:60] or uuid.uuid4().hex[:10]

    def create_temp_email(
        self, prefix: str | None = None, domain: str | None = None
    ) -> tuple[int | str, str]:
        """创建 alias。

        - prefix 缺省 → GET /api/alias/random/new?hostname=<host>(系统生成)
        - prefix 提供 → POST /api/v3/alias/custom/new?hostname=<host>
              需要先 GET /api/v5/alias/options 拿 signed_suffix。
              简化处理:把 prefix 作 description,仍走 random alias 路径,以避免
              alias_options 的额外往返。当用户 prefer custom prefix 时,可在 S4
              扩展实现 v3/custom/new 完整流程。

        Args:
            prefix: 期望的 alias prefix(custom 模式)或 description(random 模式)
            domain: SimpleLogin 不接受任意 domain,忽略此参数(由 alias_options 决定)

        Returns:
            (alias_id, alias_email)
        """
        host = self.hostname
        note = self._sanitize_alias_prefix(prefix) if prefix else f"autoteam-{uuid.uuid4().hex[:6]}"

        params = {"hostname": host}
        body = {"note": note}

        r = self._request("POST", "/api/alias/random/new", params=params, json=body)
        data = self._parse_json(r, "/api/alias/random/new")
        alias = data if isinstance(data, dict) else {}

        alias_id = alias.get("id") or alias.get("alias_id")
        email = alias.get("alias") or alias.get("email")
        if not alias_id or not email:
            raise Exception(f"SimpleLogin 创建 alias 响应缺 id/email: {alias!r}")

        logger.info("[simplelogin] alias 已创建: %s (id=%s)", email, alias_id)
        return alias_id, email

    def list_accounts(self, size: int = 200) -> list[dict]:
        """GET /api/v2/aliases?page_id=N — 翻页拉到 size 条。"""
        out: list[dict] = []
        page_id = 0
        target = max(1, int(size or 1))
        # 防御:理论上不超过 size/20 页;给上限避免无限翻
        max_pages = max(1, target // 20 + 5)
        for _ in range(max_pages):
            r = self._request("GET", "/api/v2/aliases", params={"page_id": page_id})
            data = self._parse_json(r, "/api/v2/aliases")
            aliases = data.get("aliases") if isinstance(data, dict) else None
            if not isinstance(aliases, list) or not aliases:
                break
            for row in aliases:
                if not isinstance(row, dict):
                    continue
                out.append({
                    "accountId": row.get("id"),
                    "email": row.get("email") or row.get("alias"),
                    "password": None,
                    "createTime": row.get("creation_timestamp"),
                    "description": row.get("note"),
                    "active": not row.get("disabled", False),
                    "raw": row,
                })
                if len(out) >= target:
                    return out
            page_id += 1
        return out

    def delete_account(self, account_id: int | str) -> dict:
        """DELETE /api/aliases/{id}。account_id 允许 email,自动解析。"""
        real_id = self._resolve_alias_id(account_id)
        if not real_id:
            logger.warning("[simplelogin] delete_account: 找不到对应 alias (%s)", account_id)
            return {"code": 404, "message": "alias not found"}

        r = self._request("DELETE", f"/api/aliases/{real_id}")
        if r.status_code in (200, 204):
            logger.info("[simplelogin] alias 已删除 (id=%s)", real_id)
            return {"code": 200}
        try:
            self._parse_json(r, f"/api/aliases/{real_id}")
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
        for row in self.list_accounts(size=500):
            if normalize_email_addr(row.get("email")) == target:
                aid = row.get("accountId")
                return str(aid) if aid else None
        return None

    # ------------------------------------------------------------------ inbox (no-op)
    def search_emails_by_recipient(
        self, to_email: str, size: int = 10, account_id: int | str | None = None
    ) -> list[dict]:
        logger.warning(
            "[simplelogin] search_emails_by_recipient(%s) — alias forwarding 无内置 inbox,"
            "需配对 reader provider",
            to_email,
        )
        return []

    def list_emails(self, account_id: int | str, size: int = 10) -> list[dict]:
        logger.warning("[simplelogin] list_emails(%s) — alias forwarding 无 inbox", account_id)
        return []

    def delete_emails_for(self, to_email: str) -> int:
        logger.warning("[simplelogin] delete_emails_for(%s) — alias forwarding 无 inbox", to_email)
        return 0
