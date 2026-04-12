"""ChatGPT Team API 客户端 - 通过 Playwright 绕过 Cloudflare 调用内部 API"""

import json
import logging
import re
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

import autoteam.display  # noqa: F401
from autoteam.admin_state import (
    get_admin_session_token,
    get_chatgpt_account_id,
    get_chatgpt_workspace_name,
    update_admin_state,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
BASE_DIR = PROJECT_ROOT
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"


class ChatGPTTeamAPI:
    """通过浏览器内 fetch 调用 ChatGPT Team 内部 API。"""

    EMAIL_INPUT_SELECTORS = [
        'input[name="email"]',
        'input[id="email-input"]',
        'input[id="email"]',
        'input[type="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="邮箱"]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
    ]
    PASSWORD_INPUT_SELECTORS = [
        'input[name="password"]',
        'input[type="password"]',
    ]
    CODE_INPUT_SELECTORS = [
        'input[name="code"]',
        'input[placeholder*="验证码"]',
        'input[placeholder*="code" i]',
        'input[inputmode="numeric"]',
        'input[autocomplete="one-time-code"]',
    ]

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.access_token = None
        self.session_token = None
        self.account_id = get_chatgpt_account_id()
        self.workspace_name = get_chatgpt_workspace_name()
        self.oai_device_id = str(uuid.uuid4())
        self.login_email = None
        self.login_password = None
        self.workspace_options_cache = []

    def _visible_locator_in_frames(self, selectors, timeout_ms=5000):
        selector = ", ".join(selectors)
        deadline = time.time() + timeout_ms / 1000

        while time.time() < deadline:
            frames = [self.page.main_frame]
            frames.extend(frame for frame in self.page.frames if frame != self.page.main_frame)
            for frame in frames:
                try:
                    locator = frame.locator(selector).first
                    if locator.is_visible(timeout=250):
                        return locator
                except Exception:
                    pass
            time.sleep(0.2)

        return None

    def _launch_browser(self):
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        self.page = self.context.new_page()

    def _log_login_state(self, label):
        try:
            body_excerpt = self.page.locator("body").inner_text(timeout=1500)[:300].replace("\n", " ")
        except Exception:
            body_excerpt = ""

        logger.info(
            "[ChatGPT] %s | URL=%s | body=%s",
            label,
            self.page.url,
            body_excerpt,
        )

    def _wait_for_cloudflare(self):
        for i in range(12):
            html = self.page.content()[:1000].lower()
            if "verify you are human" not in html and "challenge" not in self.page.url:
                return
            logger.info("[ChatGPT] 等待 Cloudflare... (%ds)", i * 5)
            time.sleep(5)

    def _build_session_cookies(self, session_token, domain):
        if len(session_token) > 3800:
            return [
                {
                    "name": "__Secure-next-auth.session-token.0",
                    "value": session_token[:3800],
                    "domain": domain,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "__Secure-next-auth.session-token.1",
                    "value": session_token[3800:],
                    "domain": domain,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        return [
            {
                "name": "__Secure-next-auth.session-token",
                "value": session_token,
                "domain": domain,
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]

    def _click_auth_button(self, field, labels):
        label_re = re.compile(rf"^(?:{'|'.join(re.escape(label) for label in labels)})$", re.I)
        try:
            form = field.locator("xpath=ancestor::form[1]").first
            btn = form.get_by_role("button", name=label_re).first
            if btn.is_visible(timeout=2000):
                btn.click()
                return True
        except Exception:
            pass

        try:
            form = field.locator("xpath=ancestor::form[1]").first
            btn = form.locator('button[type="submit"], input[type="submit"]').first
            if btn.is_visible(timeout=2000):
                btn.click()
                return True
        except Exception:
            pass

        try:
            field.press("Enter")
            return True
        except Exception:
            return False

    def _extract_session_token(self):
        cookies = self.context.cookies()
        session_parts = {}
        session_token = None
        for cookie in cookies:
            name = cookie["name"]
            if name == "__Secure-next-auth.session-token":
                session_token = cookie["value"]
            elif name.startswith("__Secure-next-auth.session-token."):
                suffix = name.rsplit(".", 1)[-1]
                session_parts[suffix] = cookie["value"]

        if not session_token and session_parts:
            session_token = "".join(session_parts[k] for k in sorted(session_parts))

        self.session_token = session_token
        return session_token

    def _inject_session(self, session_token):
        cookies = self._build_session_cookies(session_token, "chatgpt.com")
        if self.account_id:
            cookies.append(
                {
                    "name": "_account",
                    "value": self.account_id,
                    "domain": "chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        cookies.append(
            {
                "name": "oai-did",
                "value": self.oai_device_id,
                "domain": "chatgpt.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
        self.context.add_cookies(cookies)
        self.session_token = session_token
        logger.info("[ChatGPT] 已注入 session cookies")

    def _open_login_page(self):
        self.page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        self._wait_for_cloudflare()
        self._log_login_state("打开登录页后")

        try:
            login_btn = self.page.locator('button:has-text("登录"), button:has-text("Log in")').first
            if login_btn.is_visible(timeout=3000):
                login_btn.click()
                time.sleep(2)
                self._log_login_state("点击登录按钮后")
        except Exception:
            pass

    def _list_workspace_options(self):
        url = (self.page.url or "").lower()
        if "workspace" not in url and "organization" not in url:
            return []

        logger.info("[ChatGPT] 检测到 workspace 选择页，开始收集组织候选 | URL=%s", self.page.url)
        try:
            self.page.screenshot(path=str(SCREENSHOT_DIR / "admin_login_workspace_before_select.png"), full_page=True)
        except Exception:
            pass

        candidates = []
        seen_texts = set()
        exclude_keywords = (
            "personal account",
            "personal",
            "个人账户",
            "个人账号",
            "free",
            "免费",
            "new organization",
            "新组织",
            "create organization",
            "创建组织",
        )

        for selector in ("button", '[role="button"]', "a", '[role="option"]'):
            try:
                for loc in self.page.locator(selector).all():
                    try:
                        if not loc.is_visible(timeout=200):
                            continue
                        text = loc.inner_text(timeout=500).strip()
                    except Exception:
                        continue

                    if not text or text in seen_texts:
                        continue
                    seen_texts.add(text)

                    text_l = text.lower()
                    if len(text) > 80:
                        continue
                    kind = "fallback" if any(key in text_l for key in exclude_keywords) else "preferred"
                    candidates.append(
                        {
                            "id": str(len(candidates)),
                            "label": text,
                            "kind": kind,
                        }
                    )
            except Exception:
                pass

        logger.info("[ChatGPT] workspace 候选数: %d | candidates=%s", len(candidates), [c["label"] for c in candidates])
        self.workspace_options_cache = candidates
        return candidates

    def list_workspace_options(self):
        if self.workspace_options_cache:
            return self.workspace_options_cache
        return self._list_workspace_options()

    def select_workspace_option(self, option_id):
        options = self._list_workspace_options()
        for option in options:
            if option["id"] != str(option_id):
                continue

            label = option["label"]
            # 重新按同样顺序扫描并点击对应索引
            current_options = []
            seen_texts = set()
            for selector in ("button", '[role="button"]', "a", '[role="option"]'):
                try:
                    for loc in self.page.locator(selector).all():
                        try:
                            if not loc.is_visible(timeout=200):
                                continue
                            text = loc.inner_text(timeout=500).strip()
                        except Exception:
                            continue
                        if not text or text in seen_texts or len(text) > 80:
                            continue
                        seen_texts.add(text)
                        current_options.append((str(len(current_options)), text, loc))
                except Exception:
                    pass

            for cur_id, cur_label, loc in current_options:
                if cur_id == str(option_id):
                    logger.info("[ChatGPT] 用户选择 workspace: %s", cur_label)
                    loc.click()
                    time.sleep(3)
                    self.workspace_options_cache = []
                    self._log_login_state("选择 workspace 后")
                    step, detail = self._detect_login_step()
                    logger.info("[ChatGPT] 选择 workspace 后结果: %s | detail=%s", step, detail)
                    return {"step": step, "detail": detail}

            raise RuntimeError(f"未找到可点击的 workspace 选项: {label}")

        raise RuntimeError(f"无效的 workspace 选项: {option_id}")

    def _detect_login_step(self):
        if "accounts.google.com" in self.page.url:
            logger.warning("[ChatGPT] 登录步骤检测: 误跳转 Google | URL=%s", self.page.url)
            return "error", "误跳转到了 Google 登录"

        if "workspace" in (self.page.url or "").lower() or "organization" in (self.page.url or "").lower():
            logger.info("[ChatGPT] 登录步骤检测: workspace 页面 | URL=%s", self.page.url)
            return "workspace_required", None

        if "email-verification" in self.page.url:
            logger.info("[ChatGPT] 登录步骤检测: code_required | URL=%s", self.page.url)
            return "code_required", None

        if self._visible_locator_in_frames(self.CODE_INPUT_SELECTORS, timeout_ms=1200):
            logger.info("[ChatGPT] 登录步骤检测: code_required | URL=%s", self.page.url)
            return "code_required", None

        if self._visible_locator_in_frames(self.PASSWORD_INPUT_SELECTORS, timeout_ms=1200):
            logger.info("[ChatGPT] 登录步骤检测: password_required | URL=%s", self.page.url)
            return "password_required", None

        session_token = self._extract_session_token()
        if session_token:
            logger.info("[ChatGPT] 登录步骤检测: completed(session) | URL=%s", self.page.url)
            return "completed", None

        if "chatgpt.com" in self.page.url and "auth" not in self.page.url:
            logger.info("[ChatGPT] 登录步骤检测: completed(chatgpt) | URL=%s", self.page.url)
            return "completed", None

        logger.info("[ChatGPT] 登录步骤检测: unknown | URL=%s", self.page.url)
        return "unknown", self.page.url

    def begin_admin_login(self, email):
        self.login_email = email
        if not self.browser:
            self._launch_browser()

        logger.info("[ChatGPT] 开始管理员登录: %s", email)
        self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        self._wait_for_cloudflare()
        self._log_login_state("进入 chatgpt.com 后")
        self._open_login_page()

        step, detail = self._detect_login_step()
        if step == "workspace_required":
            self._list_workspace_options()
        if step in ("password_required", "code_required", "workspace_required", "completed", "error"):
            logger.info("[ChatGPT] 管理员登录初始步骤: %s | detail=%s", step, detail)
            return {"step": step, "detail": detail}

        email_input = self._visible_locator_in_frames(self.EMAIL_INPUT_SELECTORS, timeout_ms=15000)
        if not email_input:
            try:
                self.page.screenshot(path=str(SCREENSHOT_DIR / "admin_login_missing_email.png"), full_page=True)
            except Exception:
                pass
            body_excerpt = ""
            try:
                body_excerpt = self.page.locator("body").inner_text(timeout=2000)[:300]
            except Exception:
                pass
            raise RuntimeError(f"未找到管理员邮箱输入框，当前 URL: {self.page.url}，页面片段: {body_excerpt}")

        email_input.fill(email)
        time.sleep(0.5)
        self._click_auth_button(email_input, ["Continue", "继续"])
        time.sleep(3)
        self._log_login_state("管理员邮箱提交后")

        step, detail = self._detect_login_step()
        if step == "workspace_required":
            self._list_workspace_options()
        logger.info("[ChatGPT] 管理员邮箱提交结果: %s | detail=%s", step, detail)
        return {"step": step, "detail": detail}

    def submit_admin_password(self, password):
        self.login_password = password
        password_input = self._visible_locator_in_frames(self.PASSWORD_INPUT_SELECTORS, timeout_ms=5000)
        if not password_input:
            raise RuntimeError("当前不是密码输入步骤")

        logger.info("[ChatGPT] 提交管理员密码前 | URL=%s", self.page.url)
        password_input.fill(password)
        time.sleep(0.5)
        self._click_auth_button(password_input, ["Continue", "继续", "Log in"])
        time.sleep(8)
        self._log_login_state("管理员密码提交后")

        step, detail = self._detect_login_step()
        if step == "workspace_required":
            self._list_workspace_options()
        logger.info("[ChatGPT] 管理员密码提交结果: %s | detail=%s", step, detail)
        return {"step": step, "detail": detail}

    def submit_admin_code(self, code):
        code_input = self._visible_locator_in_frames(self.CODE_INPUT_SELECTORS, timeout_ms=5000)
        if not code_input and "email-verification" not in self.page.url:
            raise RuntimeError("当前不是验证码输入步骤")

        logger.info("[ChatGPT] 提交管理员验证码前 | URL=%s | code_len=%d", self.page.url, len(code))
        try:
            self.page.screenshot(path=str(SCREENSHOT_DIR / "admin_login_code_before_submit.png"), full_page=True)
        except Exception:
            pass
        code_input.fill(code)
        time.sleep(0.5)
        self._click_auth_button(code_input, ["Continue", "继续", "Verify"])
        time.sleep(8)
        try:
            self.page.screenshot(path=str(SCREENSHOT_DIR / "admin_login_code_after_submit.png"), full_page=True)
        except Exception:
            pass
        self._log_login_state("管理员验证码提交后")

        step, detail = self._detect_login_step()
        if step == "workspace_required":
            self._list_workspace_options()
        logger.info("[ChatGPT] 管理员验证码提交结果: %s | detail=%s", step, detail)
        return {"step": step, "detail": detail}

    def _guess_account_info(self):
        try:
            data = self.page.evaluate("""async () => {
                const out = {};
                for (const path of ['/backend-api/accounts', '/backend-api/me', '/api/auth/session']) {
                    try {
                        const resp = await fetch(path);
                        out[path] = { status: resp.status, data: await resp.json() };
                    } catch (e) {
                        out[path] = { error: String(e) };
                    }
                }
                return out;
            }""")
        except Exception:
            data = {}

        candidates = []
        _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

        def walk(node):
            if isinstance(node, dict):
                # account_id 必须是 UUID 格式（排除 user-xxx 等非 account ID）
                account_id = node.get("account_id")
                if not account_id or not _uuid_re.match(str(account_id)):
                    account_id = node.get("id")
                    if not account_id or not _uuid_re.match(str(account_id)):
                        account_id = None
                # workspace_name 只取 workspace_name 字段，不取 name/display_name（那可能是用户名）
                name = node.get("workspace_name") or ""
                if account_id:
                    candidates.append(
                        {
                            "account_id": account_id,
                            "workspace_name": name,
                            "type": str(node.get("type", "")),
                        }
                    )
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)

        chosen = None
        for cand in candidates:
            if cand["workspace_name"] and cand["workspace_name"].lower() not in ("personal",):
                chosen = cand
                break
        if not chosen and candidates:
            chosen = candidates[0]

        try:
            self.page.goto("https://chatgpt.com/admin", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            dom_name = self.page.evaluate("""() => {
                const headings = document.querySelectorAll('h1, h2, h3, [class*="title"], [class*="name"]');
                for (const h of headings) {
                    const text = h.textContent.trim();
                    if (text && text.length < 50 && text.length > 1
                        && !["常规", "成员", "设置", "General", "Members", "Settings"].includes(text)) {
                        return text;
                    }
                }
                return null;
            }""")
        except Exception:
            dom_name = None

        account_id = (chosen or {}).get("account_id") or self.account_id
        workspace_name = (chosen or {}).get("workspace_name") or dom_name or self.workspace_name
        return account_id, workspace_name

    def complete_admin_login(self):
        session_token = self._extract_session_token()
        if not session_token:
            raise RuntimeError("管理员登录成功后未提取到 session token")

        self._fetch_access_token()
        account_id, workspace_name = self._guess_account_info()
        if account_id:
            self.account_id = account_id
        if workspace_name:
            self.workspace_name = workspace_name

        payload = dict(
            email=self.login_email or "",
            session_token=session_token,
            account_id=self.account_id,
            workspace_name=self.workspace_name,
        )
        if self.login_password:
            payload["password"] = self.login_password

        update_admin_state(**payload)

        logger.info("[ChatGPT] 管理员登录状态已保存")
        return {
            "email": self.login_email or "",
            "account_id": self.account_id,
            "workspace_name": self.workspace_name,
            "session_len": len(session_token),
        }

    def start(self):
        """用已保存的管理员 session 启动 Team API 客户端。"""
        session_token = get_admin_session_token()
        self.account_id = get_chatgpt_account_id()
        self.workspace_name = get_chatgpt_workspace_name()
        if not session_token:
            raise FileNotFoundError("请先完成管理员登录")
        if not self.account_id:
            raise RuntimeError("缺少已保存的 workspace/account ID，请重新完成管理员登录")

        self._launch_browser()
        logger.info("[ChatGPT] 访问 chatgpt.com 过 Cloudflare...")
        self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        self._wait_for_cloudflare()
        self._inject_session(session_token)
        self._fetch_access_token()
        self._auto_detect_workspace()

    def _auto_detect_workspace(self):
        if self.workspace_name:
            return self.workspace_name
        if not self.account_id:
            logger.warning("[ChatGPT] 未能自动获取 workspace 名称：account_id 缺失")
            return ""

        result = self.page.evaluate(
            """async (accountId) => {
            try {
                const resp = await fetch("/backend-api/accounts/" + accountId + "/settings", {
                    headers: { "chatgpt-account-id": accountId }
                });
                return await resp.json();
            } catch(e) { return null; }
        }""",
            self.account_id,
        )

        if result and result.get("workspace_name"):
            self.workspace_name = result["workspace_name"]
            update_admin_state(workspace_name=self.workspace_name, account_id=self.account_id)
            logger.info("[ChatGPT] 自动检测到 workspace 名称: %s", self.workspace_name)
            return self.workspace_name

        try:
            self.page.goto("https://chatgpt.com/admin", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            name = self.page.evaluate("""() => {
                const headings = document.querySelectorAll('h1, h2, h3, [class*="title"], [class*="name"]');
                for (const h of headings) {
                    const text = h.textContent.trim();
                    if (text && text.length < 50 && text.length > 1
                        && !["常规", "成员", "设置", "General", "Members", "Settings"].includes(text)) {
                        return text;
                    }
                }
                return null;
            }""")
            if name:
                self.workspace_name = name
                update_admin_state(workspace_name=self.workspace_name, account_id=self.account_id)
                logger.info("[ChatGPT] 自动检测到 workspace 名称: %s", name)
                return name
        except Exception:
            pass

        logger.warning("[ChatGPT] 未能自动获取 workspace 名称")
        return ""

    def _fetch_access_token(self):
        result = self.page.evaluate("""async () => {
            try {
                const resp = await fetch("/api/auth/session");
                const data = await resp.json();
                return { ok: true, data: data };
            } catch(e) {
                return { ok: false, error: e.message };
            }
        }""")

        if result.get("ok") and "accessToken" in result.get("data", {}):
            self.access_token = result["data"]["accessToken"]
            logger.info("[ChatGPT] 已获取 access token")
            return

        bearer_file = BASE_DIR / "bearer_token"
        if bearer_file.exists():
            self.access_token = bearer_file.read_text().strip()
            logger.info("[ChatGPT] 从 bearer_token 文件加载 access token")
            return

        logger.info("[ChatGPT] 尝试通过页面获取 access token...")
        self.page.goto("https://chatgpt.com/", wait_until="networkidle", timeout=60000)
        time.sleep(10)

        token = self.page.evaluate("""() => {
            try {
                const keys = Object.keys(localStorage);
                for (const key of keys) {
                    const val = localStorage.getItem(key);
                    if (val && val.includes("eyJ") && val.length > 500) {
                        return val;
                    }
                }
            } catch(e) {}
            return null;
        }""")

        if token:
            self.access_token = token
            logger.info("[ChatGPT] 从页面获取到 access token")
        else:
            logger.warning("[ChatGPT] 未能获取 access token，将尝试无 token 调用")

    def _api_fetch(self, method, path, body=None):
        headers_js = {
            "Content-Type": "application/json",
            "chatgpt-account-id": self.account_id,
            "oai-device-id": self.oai_device_id,
            "oai-language": "en-US",
        }
        if self.access_token:
            headers_js["authorization"] = f"Bearer {self.access_token}"

        js_code = """async ([method, url, headers, body]) => {
            try {
                const opts = { method, headers };
                if (body) opts.body = body;
                const resp = await fetch(url, opts);
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch(e) {
                return { status: 0, body: e.message };
            }
        }"""

        return self.page.evaluate(
            js_code,
            [method, f"https://chatgpt.com{path}", headers_js, json.dumps(body) if body else None],
        )

    def invite_member(self, email, seat_type="usage_based"):
        path = f"/backend-api/accounts/{self.account_id}/invites"
        body = {
            "email_addresses": [email],
            "role": "standard-user",
            "seat_type": seat_type,
            "resend_emails": True,
        }

        logger.info("[ChatGPT] 发送邀请到 %s (seat_type=%s)...", email, seat_type)
        result = self._api_fetch("POST", path, body)

        status = result["status"]
        resp_body = result["body"]
        logger.info("[ChatGPT] 响应状态: %d", status)

        try:
            data = json.loads(resp_body)
            logger.debug("[ChatGPT] 响应内容: %s", json.dumps(data, indent=2)[:500])
        except Exception:
            data = resp_body
            logger.debug("[ChatGPT] 响应内容: %s", resp_body[:500])

        if status == 200 and seat_type == "usage_based" and isinstance(data, dict):
            invites = data.get("account_invites", [])
            for inv in invites:
                invite_id = inv.get("id")
                if invite_id:
                    self._update_invite_seat_type(invite_id, "default")

        return status, data

    def _update_invite_seat_type(self, invite_id, seat_type):
        path = f"/backend-api/accounts/{self.account_id}/invites/{invite_id}"
        body = {"seat_type": seat_type}

        logger.info("[ChatGPT] 修改邀请 seat_type -> %s...", seat_type)
        result = self._api_fetch("PATCH", path, body)

        if result["status"] == 200:
            logger.info("[ChatGPT] seat_type 已改为 %s", seat_type)
        else:
            logger.error("[ChatGPT] 修改 seat_type 失败: %d %s", result["status"], result["body"][:200])

    def list_invites(self):
        path = f"/backend-api/accounts/{self.account_id}/invites"
        result = self._api_fetch("GET", path)
        try:
            return json.loads(result["body"])
        except Exception:
            return result["body"]

    def stop(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
