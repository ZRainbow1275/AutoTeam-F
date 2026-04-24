"""CloudMail API 客户端 — 适配 dreamhunter2333/cloudflare_temp_email 后端。

对外接口保持与旧版 cloud-mail 客户端一致，内部重写为：
- 管理员鉴权：header `x-admin-auth: {CLOUDMAIL_PASSWORD}`
- 地址管理：/admin/new_address, /admin/delete_address/{id}, /admin/address
- 邮件读取：/admin/mails?address={email}
- 邮件删除：DELETE /admin/mails/{id} 或 DELETE /admin/clear_inbox/{email}

`raw` 字段是完整 MIME，用 stdlib `email` 解析出 subject/text/html。
"""

import base64
import email as email_pkg
import html as html_lib
import json
import logging
import re
import time
import uuid
from email.header import decode_header, make_header

import requests

from autoteam.config import (
    CLOUDMAIL_BASE_URL,
    CLOUDMAIL_EMAIL,  # noqa: F401 — 兼容旧配置，不实际使用
    CLOUDMAIL_PASSWORD,
    EMAIL_POLL_INTERVAL,
    EMAIL_POLL_TIMEOUT,
)

logger = logging.getLogger(__name__)

_VERIFICATION_CODE_PATTERNS = (
    r"(?:temporary\s+(?:openai|chatgpt)\s+login\s+code(?:\s+is)?|verification\s+code(?:\s+is)?|login\s+code(?:\s+is)?|code(?:\s+is)?|验证码(?:为|是)?)\D{0,24}(\d{6})",
    r"\b(\d{6})\b",
)


def _decode_header(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _decode_jwt_payload(jwt):
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _part_to_text(part):
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        try:
            return str(part.get_payload())
        except Exception:
            return ""


def _parse_mime(raw):
    """解析 MIME 消息，返回 (subject, text, html, from_addr, to_addr, message_id)。"""
    if not raw:
        return "", "", "", "", "", ""
    try:
        msg = email_pkg.message_from_string(raw)
    except Exception:
        return "", raw, "", "", "", ""

    subject = _decode_header(msg.get("Subject", ""))
    from_addr = _decode_header(msg.get("From", ""))
    to_addr = _decode_header(msg.get("To", ""))
    message_id = (msg.get("Message-ID") or "").strip()

    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            dispo = (part.get("Content-Disposition") or "").lower()
            if "attachment" in dispo:
                continue
            if ctype == "text/plain" and not text_body:
                text_body = _part_to_text(part)
            elif ctype == "text/html" and not html_body:
                html_body = _part_to_text(part)
    else:
        decoded = _part_to_text(msg)
        if msg.get_content_type() == "text/html":
            html_body = decoded
        else:
            text_body = decoded

    return subject, text_body, html_body, from_addr, to_addr, message_id


class CloudMailClient:
    def __init__(self):
        self.base_url = (CLOUDMAIL_BASE_URL or "").rstrip("/")
        self.admin_password = CLOUDMAIL_PASSWORD
        self.session = requests.Session()
        # 占位符，为兼容旧代码 `self.token`
        self.token = None
        # address (lower) -> JWT 缓存
        self._address_jwts = {}

    # ------------------------------------------------------------------ helpers

    def _admin_headers(self):
        return {
            "Content-Type": "application/json",
            "x-admin-auth": self.admin_password or "",
        }

    def _url(self, path):
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _admin_get(self, path, params=None):
        r = self.session.get(self._url(path), headers=self._admin_headers(), params=params, timeout=30)
        return r

    def _admin_post(self, path, data=None):
        r = self.session.post(self._url(path), headers=self._admin_headers(), json=data, timeout=30)
        return r

    def _admin_delete(self, path):
        r = self.session.delete(self._url(path), headers=self._admin_headers(), timeout=30)
        return r

    @staticmethod
    def _normalize_email(value):
        return str(value or "").strip().lower()

    @staticmethod
    def _sanitize_prefix(prefix):
        """cloudflare_temp_email 只允许字母、数字、点、下划线，长度 <= 64；其余字符剔除。"""
        if not prefix:
            return uuid.uuid4().hex[:10]
        cleaned = re.sub(r"[^A-Za-z0-9._]", "", str(prefix))
        cleaned = cleaned.strip("._")
        return cleaned[:60] or uuid.uuid4().hex[:10]

    # ------------------------------------------------------------------ auth

    def login(self):
        """兼容旧接口：cloudflare_temp_email 无需登录，调用管理员列表验证密码可用。"""
        if not self.admin_password:
            raise Exception("CloudMail 登录失败: 未配置 CLOUDMAIL_PASSWORD（作为 admin password）")

        r = self._admin_get("/admin/address", params={"limit": 1, "offset": 0})
        if r.status_code == 401 or r.status_code == 403:
            raise Exception(f"CloudMail 登录失败: admin 密码无效 (HTTP {r.status_code})")
        if r.status_code != 200:
            body = (r.text or "")[:200]
            raise Exception(f"CloudMail 登录失败: HTTP {r.status_code} {body}")

        self.token = "admin-" + self.admin_password[:6]
        logger.info("[CloudMail] 管理员鉴权通过")
        return self.token

    # ------------------------------------------------------------------ accounts

    def create_temp_email(self, prefix=None, domain=None):
        """创建临时邮箱，返回 (accountId, email)。

        domain 优先级：
        1. 显式传入的 domain 参数
        2. runtime_config.json 的 register_domain
        3. 环境变量 CLOUDMAIL_DOMAIN
        """
        if domain:
            domain = domain.lstrip("@").strip()
        else:
            from autoteam.runtime_config import get_register_domain

            domain = get_register_domain()
        if not domain:
            raise Exception("创建邮箱失败: 未配置注册域名")

        cleaned = self._sanitize_prefix(prefix)
        r = self._admin_post(
            "/admin/new_address",
            {"name": cleaned, "domain": domain, "enablePrefix": False},
        )
        if r.status_code != 200:
            raise Exception(f"创建邮箱失败: HTTP {r.status_code} {(r.text or '')[:200]}")

        data = {}
        try:
            data = r.json()
        except Exception:
            pass

        address = data.get("address")
        jwt = data.get("jwt") or ""
        payload = _decode_jwt_payload(jwt) if jwt else {}
        address_id = data.get("address_id") or payload.get("address_id")

        if not address_id:
            # Fallback：按名称查询
            try:
                listed = self._admin_get(
                    "/admin/address", params={"limit": 1, "offset": 0, "query": address or cleaned}
                )
                if listed.status_code == 200:
                    results = (listed.json() or {}).get("results") or []
                    if results:
                        address_id = results[0].get("id")
                        address = address or results[0].get("name")
            except Exception:
                pass

        if not address:
            raise Exception(f"创建邮箱失败: 响应缺少 address 字段: {data}")

        if jwt and address:
            self._address_jwts[self._normalize_email(address)] = jwt

        logger.info("[CloudMail] 临时邮箱已创建: %s (accountId=%s)", address, address_id)
        return address_id, address

    def list_accounts(self, size=200):
        """列出邮箱地址。返回与旧接口兼容的字典列表：{accountId, email, ...}"""
        r = self._admin_get("/admin/address", params={"limit": size, "offset": 0})
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except Exception:
            return []

        out = []
        for row in data.get("results", []):
            out.append(
                {
                    "accountId": row.get("id"),
                    "email": row.get("name"),
                    "password": row.get("password"),
                    "createTime": row.get("created_at"),
                    "updateTime": row.get("updated_at"),
                    "mailCount": row.get("mail_count"),
                    "sendCount": row.get("send_count"),
                    "sourceMeta": row.get("source_meta"),
                    "raw": row,
                }
            )
        return out

    def delete_account(self, account_id):
        """删除临时邮箱账户。account_id 可以是数字 id，也可以是 email（自动查 id）。"""
        real_id = self._resolve_address_id(account_id)
        if not real_id:
            logger.warning("[CloudMail] delete_account: 找不到对应的 address id (%s)", account_id)
            return {"code": 404, "message": "address not found"}

        r = self._admin_delete(f"/admin/delete_address/{real_id}")
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                body = {}
            if body.get("success"):
                logger.info("[CloudMail] 临时邮箱已删除 (accountId=%s)", real_id)
                return {"code": 200}
        return {"code": r.status_code, "message": (r.text or "")[:200]}

    # ------------------------------------------------------------------ mails

    def _resolve_address_id(self, value):
        """把 account_id（可能是 id 或 email）统一解析为数字 id。"""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

        email_str = self._normalize_email(value)
        if "@" not in email_str:
            return None
        try:
            r = self._admin_get("/admin/address", params={"limit": 5, "offset": 0, "query": email_str})
            if r.status_code != 200:
                return None
            results = (r.json() or {}).get("results") or []
            for row in results:
                if self._normalize_email(row.get("name")) == email_str:
                    return row.get("id")
        except Exception:
            return None
        return None

    def _resolve_address_email(self, account_id):
        """数字 id → email 名称。"""
        if not account_id:
            return None
        try:
            int(account_id)
        except (TypeError, ValueError):
            return str(account_id) if "@" in str(account_id) else None

        try:
            r = self._admin_get("/admin/address", params={"limit": 20, "offset": 0})
            if r.status_code != 200:
                return None
            for row in (r.json() or {}).get("results") or []:
                if str(row.get("id")) == str(account_id):
                    return row.get("name")
        except Exception:
            return None
        return None

    def _normalize_mail_record(self, row):
        """把 /admin/mails 返回的一条 raw MIME 记录转成 AutoTeam 期望的字典。"""
        raw = row.get("raw") or ""
        subject, text, html_body, from_addr, to_addr, message_id = _parse_mime(raw)

        return {
            "emailId": row.get("id"),
            "accountId": None,  # 上层会自己填入 account_id 或用 email 匹配
            "accountEmail": row.get("address"),
            "receiveEmail": row.get("address"),
            "toEmail": to_addr or row.get("address"),
            "sendEmail": row.get("source") or from_addr,
            "sender": from_addr,
            "subject": subject,
            "text": text,
            "content": html_body,
            "messageId": message_id or row.get("message_id"),
            "createTime": row.get("created_at"),
            "raw": raw,
        }

    def list_emails(self, account_id, size=10):
        """获取指定账户的收件列表。兼容旧接口：account_id 是数字 id 或 email。"""
        target_email = None
        if isinstance(account_id, str) and "@" in account_id:
            target_email = account_id
        else:
            target_email = self._resolve_address_email(account_id)

        if not target_email:
            return []

        return self.search_emails_by_recipient(target_email, size=size, account_id=account_id)

    def get_latest_emails(self, account_id, email_id=0, all_receive=0):
        """兼容旧接口：返回该账户最新邮件；用同一个 /admin/mails 查询替代。"""
        return self.list_emails(account_id, size=5)

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        """按收件人查邮件（最新优先）。"""
        target = self._normalize_email(to_email)
        if not target:
            return []

        r = self._admin_get(
            "/admin/mails",
            params={"limit": size, "offset": 0, "address": target},
        )
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except Exception:
            return []

        out = []
        for row in data.get("results", []):
            row_addr = self._normalize_email(row.get("address"))
            if row_addr and row_addr != target:
                continue
            normalized = self._normalize_mail_record(row)
            if account_id is not None:
                normalized["accountId"] = account_id
            out.append(normalized)
        return out

    def delete_emails_for(self, to_email):
        """删除指定收件人的全部邮件。优先 clear_inbox，其次逐封删除。"""
        target = self._normalize_email(to_email)
        if not target:
            return 0

        r = self._admin_delete(f"/admin/clear_inbox/{target}")
        if r.status_code == 200:
            try:
                body = r.json() or {}
            except Exception:
                body = {}
            if body.get("success"):
                logger.info("[CloudMail] 已清空 %s 的收件箱", target)
                return 1

        # 回退：逐封删除
        mails = self.search_emails_by_recipient(target, size=100)
        deleted = 0
        for mail in mails:
            mid = mail.get("emailId")
            if not mid:
                continue
            try:
                resp = self._admin_delete(f"/admin/mails/{mid}")
                if resp.status_code == 200:
                    deleted += 1
            except Exception:
                pass
        if deleted:
            logger.info("[CloudMail] 已逐封删除 %s 的 %d 封旧邮件", target, deleted)
        return deleted

    # ------------------------------------------------------------------ OTP / 链接

    @staticmethod
    def _html_to_visible_text(value):
        content = str(value or "")
        if not content:
            return ""

        content = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", content)
        content = re.sub(r"(?is)<!--.*?-->", " ", content)
        content = re.sub(r"(?i)<br\s*/?>", "\n", content)
        content = re.sub(r"(?i)</(?:p|div|tr|table|h[1-6]|li|td|section|article)>", "\n", content)
        content = re.sub(r"(?s)<[^>]+>", " ", content)
        content = html_lib.unescape(content)
        content = re.sub(r"[\t\r\f\v ]+", " ", content)
        content = re.sub(r"\n\s+", "\n", content)
        content = re.sub(r"\n{2,}", "\n", content)
        return content.strip()

    def extract_verification_code(self, email_data):
        """从邮件正文中提取 6 位验证码。"""
        sources = []

        plain_text = str(email_data.get("text") or "").strip()
        if plain_text:
            sources.append(plain_text)

        html_text = self._html_to_visible_text(email_data.get("content"))
        if html_text and html_text not in sources:
            sources.append(html_text)

        for source in sources:
            for pattern in _VERIFICATION_CODE_PATTERNS:
                match = re.search(pattern, source, re.IGNORECASE)
                if match:
                    return match.group(1)
        return None

    def extract_invite_link(self, email_data):
        """从 OpenAI 邀请邮件中提取邀请链接。"""
        html_body = email_data.get("content", "") or ""
        text = email_data.get("text", "") or ""

        links = re.findall(r'href="(https://chatgpt\.com/auth/login\?[^"]*)"', html_body)
        if links:
            link = links[0]
            logger.info("[CloudMail] 提取到邀请链接: %s...", link[:80])
            return link

        links = re.findall(r'(https://chatgpt\.com/auth/login\?[^\s<>"\']+)', text)
        if links:
            link = links[0]
            logger.info("[CloudMail] 提取到邀请链接: %s...", link[:80])
            return link

        link_pattern = r'https?://[^\s<>"\']+(?:invite|accept|join|workspace)[^\s<>"\']*'
        match = re.search(link_pattern, html_body or text, re.IGNORECASE)
        if match:
            link = match.group(0)
            logger.info("[CloudMail] 提取到链接: %s...", link[:80])
            return link
        return None

    # ------------------------------------------------------------------ wait

    def wait_for_email(self, to_email, timeout=None, sender_keyword=None):
        """轮询等待邮件到达。"""
        timeout = timeout or EMAIL_POLL_TIMEOUT
        logger.info("[CloudMail] 等待邮件到达 %s... (超时 %ds)", to_email, timeout)
        start = time.time()

        while time.time() - start < timeout:
            emails = self.search_emails_by_recipient(to_email, size=10)
            for em in emails:
                sender = em.get("sendEmail", "") or ""
                if sender_keyword and sender_keyword.lower() not in sender.lower():
                    continue
                subject = em.get("subject", "")
                logger.info("[CloudMail] 收到邮件: %s (from: %s)", subject, sender)
                return em

            elapsed = int(time.time() - start)
            print(f"\r[CloudMail] 等待中... ({elapsed}s)", end="", flush=True)
            time.sleep(EMAIL_POLL_INTERVAL)

        print()
        raise TimeoutError("等待邮件超时")
