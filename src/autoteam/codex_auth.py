"""Codex 认证管理 - OAuth 登录、token 管理、保存 CPA 兼容认证文件"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

import autoteam.display  # noqa: F401
from autoteam.accounts import is_supported_plan, normalize_plan_type
from autoteam.admin_state import (
    get_admin_email,
    get_admin_session_token,
    get_chatgpt_account_id,
    get_chatgpt_workspace_name,
)
from autoteam.auth_storage import AUTH_DIR, ensure_auth_dir, ensure_auth_file_permissions
from autoteam.config import get_playwright_launch_options
from autoteam.invite import assert_not_blocked  # SPEC-2 shared/add-phone-detection §3 — OAuth 流程复用
from autoteam.textio import write_text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"

# Codex OAuth 配置
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"

# SPEC-2 shared/quota-classification §4.4 I5 — Codex backend 最小推理端点(用于 uninitialized_seat 二次验证)
_CODEX_SMOKE_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
# quota 关键词:codex backend 返回 4xx 时若 body 含这些词同样视为 auth_invalid(可能是配额相关 API 错误)
_CODEX_SMOKE_QUOTA_HINTS = ("quota", "no_quota", "rate_limit", "billing", "exceeded")


def _generate_pkce():
    """生成 PKCE code_verifier 和 code_challenge"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _parse_jwt_payload(token):
    """解析 JWT payload（不验证签名）"""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    # 补齐 base64 padding
    payload += "=" * (4 - len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _screenshot(page, name):
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / name), full_page=True)


def _build_auth_url(code_challenge, state):
    params = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    return f"{CODEX_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_auth_code(auth_code, code_verifier, fallback_email=None):
    logger.info("[Codex] 获取到 auth code，交换 token...")

    import requests

    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": auth_code,
            "redirect_uri": CODEX_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if resp.status_code != 200:
        logger.error("[Codex] Token 交换失败: %d %s", resp.status_code, resp.text[:200])
        return None

    token_data = resp.json()
    id_token = token_data.get("id_token", "")
    claims = _parse_jwt_payload(id_token)
    auth_claims = claims.get("https://api.openai.com/auth", {})

    raw_plan = auth_claims.get("chatgpt_plan_type", "unknown")
    bundle = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "id_token": id_token,
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "email": claims.get("email", fallback_email or ""),
        # SPEC-2 shared/plan-type-whitelist §2.3:plan_type 已归一化为小写;
        # plan_type_raw 保留 OpenAI 原始字面量便于事后排查;
        # plan_supported 是白名单判定结果,下游消费方应只读该字段不再自己 .lower() 比对。
        "plan_type": normalize_plan_type(raw_plan),
        "plan_type_raw": raw_plan,
        "plan_supported": is_supported_plan(raw_plan),
        "expired": time.time() + token_data.get("expires_in", 3600),
    }

    logger.info(
        "[Codex] 登录成功: %s (plan: %s, supported: %s)",
        bundle["email"], bundle["plan_type"], bundle["plan_supported"],
    )
    return bundle


def _write_auth_file(filepath, bundle):
    filepath = Path(filepath)
    ensure_auth_dir()
    filepath.parent.mkdir(exist_ok=True)

    auth_data = {
        "type": "codex",
        "id_token": bundle.get("id_token", ""),
        "access_token": bundle.get("access_token", ""),
        "refresh_token": bundle.get("refresh_token", ""),
        "account_id": bundle.get("account_id", ""),
        "email": bundle.get("email", ""),
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bundle.get("expired", 0))),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    write_text(filepath, json.dumps(auth_data, indent=2))
    ensure_auth_file_permissions(filepath)
    logger.info("[Codex] 认证文件已保存: %s", filepath)
    return str(filepath)


def _click_primary_auth_button(page, field, labels):
    """
    只点击当前输入框所在表单的主按钮，避免误点 Continue with Google/Apple/Microsoft。
    """
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
        btn = page.get_by_role("button", name=label_re).last
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


def _is_google_redirect(page):
    url = (page.url or "").lower()
    if "accounts.google.com" in url:
        return True

    try:
        text = page.locator("body").inner_text(timeout=1000).lower()
        return "sign in with google" in text[:300]
    except Exception:
        return False


_OTP_INPUT_SELECTORS = (
    'input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"], '
    'input[placeholder*="验证码"], input[placeholder*="code" i]'
)
_OTP_INVALID_HINTS = (
    "invalid code",
    "incorrect code",
    "wrong code",
    "expired code",
    "check the code and try again",
    "验证码无效",
    "验证码错误",
    "验证码已过期",
)


def _is_otp_input_visible(page, timeout=500):
    try:
        return page.locator(_OTP_INPUT_SELECTORS).first.is_visible(timeout=timeout)
    except Exception:
        return False


def _detect_otp_error(page):
    try:
        body = page.locator("body").inner_text(timeout=1500).lower().replace("\n", " ")
    except Exception:
        return None

    for hint in _OTP_INVALID_HINTS:
        if hint in body:
            return hint
    return None


def _wait_for_otp_submit_result(page, timeout=12):
    """
    等待验证码提交结果：
    - accepted: 验证码输入框已消失 / 页面已前进
    - invalid: 页面明确提示验证码错误
    - pending: 既没报错也没明显前进（常见于页面较慢或状态未稳定）
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        err = _detect_otp_error(page)
        if err:
            return "invalid", err
        if not _is_otp_input_visible(page, timeout=250):
            return "accepted", None
        time.sleep(0.5)

    err = _detect_otp_error(page)
    if err:
        return "invalid", err
    return "pending", None


def login_codex_via_browser(email, password, mail_client=None, *, use_personal=False):
    """
    通过 Playwright 自动完成 Codex OAuth 登录。
    mail_client: CloudMailClient 实例，用于自动读取登录验证码。
    use_personal: 若为 True，则走"个人账号"流程 —— 不注入 Team _account cookie，
                  workspace 选择时跳过 Team 直接用 Personal。用于已退出 Team 的子账号生成 free plan 的 rt/at。
    返回 auth bundle: {access_token, refresh_token, id_token, account_id, email, plan_type}
    """
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    _used_email_ids: set[int] = set()  # 记录已尝试过的邮件，避免重复提交同一封验证码邮件

    # personal 模式下不引导到 Team workspace
    chatgpt_account_id = "" if use_personal else get_chatgpt_account_id()

    auth_url = _build_auth_url(code_challenge, state)

    logger.info("[Codex] 开始 OAuth 登录: %s", email)

    auth_code = None

    with sync_playwright() as p:
        browser = p.chromium.launch(**get_playwright_launch_options())
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )

        # === Step 0: 先登录 ChatGPT 并切换到 Team workspace ===
        # 仅 Team 模式需要:登录前注入 _account cookie 引导登录进入 Team workspace。
        # personal 模式 chatgpt_account_id="" 无 cookie 可注入,step-0 反而会留下半成品
        # session(新账号 ChatGPT 登录 12s 内通常走不完) → auth_url 在同 context 下会看到
        # "欢迎回来"页,邮箱灰禁、consent 循环误点 Continue → OpenAI 返回 Operation timed out。
        # 所以 personal 模式直接跳过 step-0,auth_url 在干净 context 里自己走邮箱/密码/OTP。

        # 在登录开始前记录当前最新邮件 ID,后续只接受比这个更新的
        _email_id_before_login = 0
        if mail_client:
            try:
                _pre = mail_client.search_emails_by_recipient(email, size=1)
                if _pre:
                    _email_id_before_login = _pre[0].get("emailId", 0)
            except Exception:
                pass

        if use_personal:
            logger.info("[Codex] personal 模式: 跳过 step-0 ChatGPT 预登录,直接走 auth_url")
        else:
            if chatgpt_account_id:
                context.add_cookies(
                    [
                        {
                            "name": "_account",
                            "value": chatgpt_account_id,
                            "domain": "chatgpt.com",
                            "path": "/",
                            "secure": True,
                            "sameSite": "Lax",
                        },
                        {
                            "name": "_account",
                            "value": chatgpt_account_id,
                            "domain": "auth.openai.com",
                            "path": "/",
                            "secure": True,
                            "sameSite": "Lax",
                        },
                    ]
                )
                logger.debug("[Codex] 登录前已注入 _account cookie = %s", chatgpt_account_id)

            logger.info("[Codex] 先登录 ChatGPT 选择 Team workspace...")
            _page = context.new_page()
            _page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)

            # Cloudflare
            for _i in range(12):
                if "verify you are human" not in _page.content()[:2000].lower():
                    break
                time.sleep(5)

            # 点击登录
            try:
                _page.locator('button:has-text("登录"), button:has-text("Log in")').first.click()
                time.sleep(3)
            except Exception:
                pass

            # 输入邮箱（避免误点 Google/Microsoft 第三方登录按钮）
            try:
                ei = _page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
                if ei.is_visible(timeout=5000):
                    ei.fill(email)
                    time.sleep(0.5)
                    _click_primary_auth_button(_page, ei, ["Continue", "继续"])
                    time.sleep(3)
            except Exception:
                pass

            # 输入密码 / 点击一次性验证码登录
            try:
                pi = _page.locator('input[type="password"]').first
                if pi.is_visible(timeout=5000):
                    if password:
                        pi.fill(password)
                        time.sleep(0.5)
                        _click_primary_auth_button(_page, pi, ["Continue", "继续", "Log in"])
                    else:
                        # 没有密码，点击"使用一次性验证码登录"
                        otp_btn = _page.locator(
                            'button:has-text("一次性验证码"), button:has-text("one-time"), button:has-text("email login")'
                        ).first
                        if otp_btn.is_visible(timeout=3000):
                            logger.info("[Codex] 无密码，点击一次性验证码登录")
                            otp_btn.click()
                        else:
                            # fallback: 提交空密码让页面报错，然后找验证码按钮
                            _click_primary_auth_button(_page, pi, ["Continue", "继续", "Log in"])
                    time.sleep(8)
            except Exception:
                pass

            # 可能需要邮箱验证码
            try:
                ci = _page.locator('input[name="code"]').first
                if ci.is_visible(timeout=5000) and mail_client:
                    logger.info("[Codex] ChatGPT 登录需要验证码，等待 emailId > %d 的新邮件...", _email_id_before_login)
                    otp = None
                    otp_email_id = 0
                    t0 = time.time()
                    while time.time() - t0 < 120:
                        for em in mail_client.search_emails_by_recipient(email, size=5):
                            email_id = em.get("emailId", 0)
                            if email_id <= _email_id_before_login or email_id in _used_email_ids:
                                continue
                            otp = mail_client.extract_verification_code(em)
                            if otp:
                                otp_email_id = email_id
                                break
                        if otp:
                            break
                        time.sleep(3)
                    if otp:
                        _used_email_ids.add(otp_email_id)
                        ci.fill(otp)
                        time.sleep(0.5)
                        _page.locator('button[type="submit"]').first.click()
                        time.sleep(5)
            except Exception:
                pass

            _screenshot(_page, "codex_00_chatgpt_login.png")
            logger.info("[Codex] ChatGPT 登录后 URL: %s", _page.url)

            # 如果是 workspace 选择页面，Team 模式选配置的 workspace
            if "workspace" in _page.url:
                workspace_name = get_chatgpt_workspace_name()
                logger.info("[Codex] 检测到 workspace 选择页面...")
                try:
                    ws_btn = _page.locator(f'text="{workspace_name}"').first
                    if workspace_name and ws_btn.is_visible(timeout=3000):
                        logger.info("[Codex] 选择 workspace: %s", workspace_name)
                        ws_btn.click()
                        time.sleep(5)
                    else:
                        # fallback: 选第二个选项（第一个通常是"个人"）
                        options = _page.locator('a, button, [role="button"]').all()
                        for opt in options:
                            try:
                                text = opt.inner_text(timeout=1000).strip()
                                if (
                                    text
                                    and "个人" not in text
                                    and "Personal" not in text
                                    and text not in ("ChatGPT", "")
                                ):
                                    logger.info("[Codex] 选择 workspace: %s", text)
                                    opt.click()
                                    time.sleep(5)
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
                _screenshot(_page, "codex_00_after_workspace.png")
                logger.info("[Codex] 选择 workspace 后 URL: %s", _page.url)

            # _account cookie 已在登录前注入

            # 关闭 ChatGPT 页面但保留 context
            _page.close()

        # 通过监听请求来捕获 OAuth callback redirect
        def on_request(request):
            nonlocal auth_code
            url = request.url
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]
                if auth_code:
                    logger.info("[Codex] 捕获到 auth code!")

        # 也监听 response/framenavigated 来捕获 redirect URL
        def on_response(response):
            nonlocal auth_code
            url = response.url
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url and not auth_code:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]
                if auth_code:
                    logger.info("[Codex] 从 response 捕获到 auth code!")

        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        _screenshot(page, "codex_01_auth_page.png")

        # 输入邮箱（注意避免点到 Google/Microsoft/Apple 第三方登录按钮）
        try:
            for attempt in range(2):
                email_input = page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
                if not email_input.is_visible(timeout=5000):
                    break

                email_input.fill(email)
                time.sleep(0.5)
                _click_primary_auth_button(page, email_input, ["Continue", "继续"])
                time.sleep(3)

                if not _is_google_redirect(page):
                    break

                _screenshot(page, f"codex_02_google_redirect_attempt{attempt + 1}.png")
                logger.warning("[Codex] 邮箱步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            _screenshot(page, "codex_02_after_email.png")
        except Exception:
            _screenshot(page, "codex_02_no_email.png")

        # 输入密码
        try:
            for attempt in range(2):
                pwd_input = page.locator('input[name="password"], input[type="password"]').first
                if not pwd_input.is_visible(timeout=5000):
                    break

                pwd_input.fill(password)
                time.sleep(0.5)
                _click_primary_auth_button(page, pwd_input, ["Continue", "继续", "Log in"])
                time.sleep(5)

                if not _is_google_redirect(page):
                    break

                _screenshot(page, f"codex_03_google_redirect_attempt{attempt + 1}.png")
                logger.warning("[Codex] 密码步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            _screenshot(page, "codex_03_after_password.png")
        except Exception:
            _screenshot(page, "codex_03_no_password.png")

        # 可能需要邮箱登录验证码
        _screenshot(page, "codex_03b_check_otp.png")
        code_input = None
        try:
            code_input = page.locator(
                'input[name="code"], input[placeholder*="验证码"], input[placeholder*="code" i]'
            ).first
            if not code_input.is_visible(timeout=5000):
                code_input = None
        except Exception:
            code_input = None

        if code_input and mail_client:
            logger.info("[Codex] 需要登录验证码，等待 emailId > %d 的新邮件...", _email_id_before_login)

            start_t = time.time()
            otp_code = None
            otp_email_id = 0
            while time.time() - start_t < 120:
                emails = mail_client.search_emails_by_recipient(email, size=5)
                for em in emails:
                    email_id = em.get("emailId", 0)
                    if email_id <= _email_id_before_login or email_id in _used_email_ids:
                        continue
                    subj = em.get("subject", "").lower()
                    if "invited" in subj or "invitation" in subj:
                        continue
                    otp_code = mail_client.extract_verification_code(em)
                    if otp_code:
                        otp_email_id = email_id
                        break
                if otp_code:
                    break
                time.sleep(3)

            if otp_code:
                _used_email_ids.add(otp_email_id)
                logger.info("[Codex] 获取到验证码: %s", otp_code)
                code_input.fill(otp_code)
                time.sleep(0.5)
                page.locator(
                    'button:has-text("Continue"), button:has-text("继续"), button[type="submit"]'
                ).first.click()
                time.sleep(5)
                _screenshot(page, "codex_03c_after_otp.png")
            else:
                logger.warning("[Codex] 未获取到验证码")
        elif code_input:
            logger.warning("[Codex] 需要验证码但无 mail_client，无法自动获取")

        # SPEC-2 shared/add-phone-detection §4 (位点 C-P1):about-you 入口前先探针。
        # 注册流程提交 OTP 后 OpenAI 经常把账号引到 add-phone,等切到 about-you 再发现就太晚。
        assert_not_blocked(page, "oauth_about_you")

        # 处理 about-you 页面（可能出现在 OAuth 流程中）
        if "about-you" in page.url:
            logger.info("[Codex] 检测到 about-you 页面，填写个人信息...")
            try:
                name_input = page.locator('input[name="name"]').first
                if name_input.is_visible(timeout=3000):
                    name_input.fill("User")

                # 自适应：生日日期（spinbutton）或年龄（普通 input）
                spinbuttons = page.locator('[role="spinbutton"]').all()
                if len(spinbuttons) >= 3:
                    # 类型 A：React Aria DateField
                    try:
                        page.locator("text=生日日期").click()
                        time.sleep(0.5)
                    except Exception:
                        pass
                    for sb, val in zip(spinbuttons[:3], ["1995", "06", "15"]):
                        sb.click(force=True)
                        time.sleep(0.2)
                        page.keyboard.type(val, delay=80)
                        time.sleep(0.3)
                    logger.info("[Codex] 填入生日: 1995/06/15 (spinbutton)")
                else:
                    # 类型 B：普通年龄数字输入框
                    age_input = page.locator('input[name="age"], input[placeholder*="年龄"]').first
                    try:
                        if age_input.is_visible(timeout=3000):
                            age_input.fill("25")
                            logger.info("[Codex] 填入年龄: 25")
                    except Exception:
                        logger.warning("[Codex] 未找到年龄/生日输入框")

                time.sleep(0.5)
                page.locator(
                    'button:has-text("继续"), button:has-text("Continue"), button:has-text("完成帐户创建"), button[type="submit"]'
                ).first.click()
                time.sleep(5)
                _screenshot(page, "codex_03d_after_aboutyou.png")
                logger.info("[Codex] about-you 完成，当前 URL: %s", page.url)
            except Exception as e:
                logger.error("[Codex] about-you 处理失败: %s", e)

        # 处理多个授权/同意页面（可能有多步）
        for step in range(10):
            if auth_code:
                break

            # SPEC-2 shared/add-phone-detection §4 (位点 C-P2):consent 循环每轮开头探针。
            # 不在循环里拦,会被当作"workspace 没选好"反复重试,30s callback 等待白白耗费。
            assert_not_blocked(page, f"oauth_consent_{step}")

            _screenshot(page, f"codex_04_step{step + 1}_before.png")

            # 在任何页面中，如果有 workspace/组织选择，先选 Team（personal 模式下选个人）
            try:
                page_text = page.inner_text("body")[:1000]

                # personal 模式：检测到工作空间选择页时，直接选 Personal
                if use_personal and (
                    "选择一个工作空间" in page_text or "Select a workspace" in page_text or "选择工作空间" in page_text
                ):
                    _screenshot(page, f"codex_04_personal_ws_{step + 1}_before.png")
                    logger.info("[Codex] 检测到工作空间选择页 (step %d, personal 模式)", step + 1)
                    personal_selected = False
                    try:
                        personal_btn = page.locator("text=/个人|Personal/").first
                        if personal_btn.is_visible(timeout=2000):
                            personal_btn.click(force=True)
                            time.sleep(1)
                            personal_selected = True
                            logger.info("[Codex] 已选择 Personal workspace (step %d)", step + 1)
                    except Exception as e:
                        logger.warning("[Codex] 选择 Personal 失败: %s", e)
                    _screenshot(page, f"codex_04_personal_ws_{step + 1}_after.png")
                    if personal_selected:
                        try:
                            cont_btn = page.locator('button:has-text("继续"), button:has-text("Continue")').first
                            if cont_btn.is_visible(timeout=3000):
                                cont_btn.click()
                                time.sleep(3)
                        except Exception:
                            pass
                        continue

                # 选择 Team workspace（用配置的名称精确匹配）
                workspace_name = "" if use_personal else get_chatgpt_workspace_name()
                # 检测"选择一个工作空间"页面，点击 Team workspace
                if workspace_name and (
                    "选择一个工作空间" in page_text or "Select a workspace" in page_text or "选择工作空间" in page_text
                ):
                    selected = False
                    _screenshot(page, f"codex_04_workspace_{step + 1}_before.png")
                    logger.info("[Codex] 检测到工作空间选择页 (step %d)，尝试选择: %s", step + 1, workspace_name)

                    # 用 JS 直接点击包含 workspace 名称的元素（最可靠）
                    try:
                        clicked = page.evaluate(
                            """(name) => {
                            const els = document.querySelectorAll('*');
                            for (const el of els) {
                                const text = (el.textContent || '').trim();
                                if (text === name && !text.includes('个人') && !text.includes('Personal')) {
                                    // 找到最近的可点击父元素
                                    let target = el;
                                    while (target && target.tagName !== 'BODY') {
                                        const tag = target.tagName.toLowerCase();
                                        if (['button', 'a', 'li', 'label'].includes(tag)
                                            || target.getAttribute('role')
                                            || target.onclick
                                            || target.classList.length > 0) {
                                            target.click();
                                            return true;
                                        }
                                        target = target.parentElement;
                                    }
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                            workspace_name,
                        )
                        if clicked:
                            time.sleep(1)
                            selected = True
                            logger.info("[Codex] 已选择 workspace (JS): %s (step %d)", workspace_name, step + 1)
                    except Exception as e:
                        logger.warning("[Codex] JS 选择 workspace 失败: %s", e)

                    if not selected:
                        # fallback: Playwright 选择器
                        try:
                            ws_el = page.locator(f"text={workspace_name}").first
                            if ws_el.is_visible(timeout=2000):
                                ws_el.click(force=True)
                                time.sleep(1)
                                selected = True
                                logger.info(
                                    "[Codex] 已选择 workspace (force click): %s (step %d)", workspace_name, step + 1
                                )
                        except Exception:
                            pass

                    _screenshot(page, f"codex_04_workspace_{step + 1}_after.png")
                    if selected:
                        # 选完 workspace 后点"继续"按钮提交
                        try:
                            cont_btn = page.locator('button:has-text("继续"), button:has-text("Continue")').first
                            if cont_btn.is_visible(timeout=3000):
                                cont_btn.click()
                                time.sleep(3)
                                logger.info("[Codex] 已点击继续 (step %d)", step + 1)
                        except Exception:
                            pass
                        continue
                    else:
                        logger.warning("[Codex] 无法选择 workspace '%s' (step %d)", workspace_name, step + 1)

                elif workspace_name:
                    # 非工作空间选择页，但可能有 workspace 文本（如 organization 页）
                    try:
                        ws_btn = page.locator(f'text="{workspace_name}"').first
                        if ws_btn.is_visible(timeout=1000):
                            ws_btn.click()
                            time.sleep(1)
                            logger.info("[Codex] 已选择 workspace: %s (step %d)", workspace_name, step + 1)
                    except Exception:
                        pass

                # Organization 页面的下拉选择
                if "organization" in page.url:
                    dropdown = page.locator("[aria-expanded], [aria-haspopup]").first
                    if dropdown.is_visible(timeout=2000):
                        dropdown.click()
                        time.sleep(1)
                        options = page.locator('[role="option"]').all()
                        for opt in options:
                            text = opt.inner_text(timeout=1000).strip()
                            if text and "新组织" not in text and "New" not in text:
                                opt.click()
                                logger.info("[Codex] 选择已有组织: %s", text)
                                break
                        else:
                            if options:
                                options[0].click()
                        time.sleep(1)
            except Exception:
                pass

            # 处理密码页面（可能在 consent 流程中出现）
            try:
                pwd_field = page.locator('input[name="password"], input[type="password"]').first
                if pwd_field.is_visible(timeout=2000):
                    if password:
                        logger.info("[Codex] 需要重新输入密码 (step %d)...", step + 1)
                        pwd_field.fill(password)
                        time.sleep(0.5)
                        _click_primary_auth_button(page, pwd_field, ["Continue", "继续", "Log in"])
                    else:
                        # 没密码，点"使用一次性验证码登录"
                        otp_btn = page.locator(
                            'button:has-text("一次性验证码"), button:has-text("one-time"), button:has-text("email login")'
                        ).first
                        if otp_btn.is_visible(timeout=3000):
                            logger.info("[Codex] 无密码，点击一次性验证码登录 (step %d)", step + 1)
                            otp_btn.click()
                        else:
                            _click_primary_auth_button(page, pwd_field, ["Continue", "继续", "Log in"])
                    time.sleep(5)
                    _screenshot(page, f"codex_04_password_{step + 1}.png")
                    continue
            except Exception:
                pass

            # 处理邮箱验证码页面（可能在 consent 流程中出现）
            try:
                otp_input = page.locator(_OTP_INPUT_SELECTORS).first
                if otp_input.is_visible(timeout=2000) and mail_client:
                    logger.info(
                        "[Codex] 需要邮箱验证码 (step %d)，等待 emailId > %d 的新邮件...",
                        step + 1,
                        _email_id_before_login,
                    )
                    otp = None
                    otp_email_id = 0
                    page_left_code = False
                    t0 = time.time()
                    while time.time() - t0 < 120:
                        if not _is_otp_input_visible(page, timeout=300):
                            page_left_code = True
                            logger.info("[Codex] 验证码页已退出，继续后续授权流程")
                            break
                        for em in mail_client.search_emails_by_recipient(email, size=5):
                            # 只接受比快照更新的邮件
                            email_id = em.get("emailId", 0)
                            if email_id <= _email_id_before_login or email_id in _used_email_ids:
                                continue
                            sender = (em.get("sendEmail") or "").lower()
                            if "openai" not in sender and "chatgpt" not in sender:
                                continue
                            subj = (em.get("subject") or "").lower()
                            if "invited" in subj or "invitation" in subj:
                                continue
                            otp = mail_client.extract_verification_code(em)
                            if otp:
                                otp_email_id = email_id
                                break
                        if otp:
                            break
                        time.sleep(3)
                    if otp:
                        submit_ok = False
                        for submit_attempt in range(1, 3):
                            otp_input = page.locator(_OTP_INPUT_SELECTORS).first
                            if not otp_input.is_visible(timeout=2000):
                                submit_ok = True
                                break

                            otp_input.fill(otp)
                            time.sleep(0.5)
                            page.locator(
                                'button[type="submit"], button:has-text("Continue"), button:has-text("继续")'
                            ).first.click()
                            logger.info("[Codex] 已输入验证码: %s", otp)

                            submit_status, submit_detail = _wait_for_otp_submit_result(page, timeout=12)
                            if submit_status == "accepted":
                                submit_ok = True
                                break
                            if submit_status == "invalid":
                                _used_email_ids.add(otp_email_id)
                                detail_suffix = f"，命中提示: {submit_detail}" if submit_detail else ""
                                logger.warning(
                                    "[Codex] 验证码邮件 %s（code=%s）被页面判定无效%s，标记并跳过该邮件",
                                    otp_email_id,
                                    otp,
                                    detail_suffix,
                                )
                                break

                            if submit_attempt < 2:
                                logger.warning(
                                    "[Codex] 验证码邮件 %s（code=%s）提交后未确认成功，准备重试第 %d/2 次",
                                    otp_email_id,
                                    otp,
                                    submit_attempt + 1,
                                )
                                time.sleep(2)
                            else:
                                _used_email_ids.add(otp_email_id)
                                logger.warning(
                                    "[Codex] 验证码邮件 %s（code=%s）提交后仍未确认成功，标记并跳过该邮件",
                                    otp_email_id,
                                    otp,
                                )

                        if submit_ok:
                            _used_email_ids.add(otp_email_id)
                        continue
                    if page_left_code:
                        continue
            except Exception:
                pass

            try:
                consent_btn = page.locator(
                    'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")'
                ).first
                if consent_btn.is_visible(timeout=5000):
                    logger.info("[Codex] 点击同意/继续按钮 (step %d)...", step + 1)
                    consent_btn.click()
                    time.sleep(5)
                    _screenshot(page, f"codex_04_consent_{step + 1}.png")
                else:
                    break
            except Exception:
                break

        # SPEC-2 shared/add-phone-detection §4 (位点 C-P3):等 callback 前探针。
        # add-phone 阻塞 = "callback 永远不来"的根因,30s 等待白白浪费。
        assert_not_blocked(page, "oauth_callback_wait")

        # 等待 redirect callback 获取 auth code
        for _ in range(30):
            if auth_code:
                break
            # 也从当前 URL 尝试提取（CPA 可能接收了回调）
            try:
                cur = page.url
                if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in cur:
                    parsed = urllib.parse.urlparse(cur)
                    qs = urllib.parse.parse_qs(parsed.query)
                    auth_code = qs.get("code", [None])[0]
                    if auth_code:
                        logger.info("[Codex] 从 URL 捕获到 auth code!")
                        break
            except Exception:
                pass
            time.sleep(1)

        if not auth_code:
            _screenshot(page, "codex_05_no_callback.png")
            logger.warning("[Codex] 未获取到 auth code，当前 URL: %s", page.url)

        # SPEC-2 §3.4.5 (位点 C-P4) + Round 6 PRD-5 FR-P1.1:personal 拒收 bundle 之前的最后一道关卡。
        # 防御性 — 通常 C-P1~C-P3 已拦下,但 add-phone 可能在 callback 阶段后晚到一两秒;
        # 此处在 browser.close() 之前做最后一次探针,确保 page 仍可读。命中即抛 RegisterBlocked,
        # 上游 5 个调用方按 add-phone-detection.md §5.2 矩阵分类处置(personal/team/reinvite/api 各异)。
        try:
            assert_not_blocked(page, "oauth_personal_check")
        except Exception:
            # assert_not_blocked 命中 add-phone 会抛 RegisterBlocked — 必须传播给上层
            # 但要保证 browser 资源被释放
            try:
                browser.close()
            except Exception:
                pass
            raise

        browser.close()

    if not auth_code:
        logger.error("[Codex] OAuth 登录失败: 未获取到 authorization code")
        return None

    bundle = _exchange_auth_code(auth_code, code_verifier, fallback_email=email)
    if not bundle:
        return None

    # Personal 模式强校验 plan_type:当子号还挂在 Team workspace(OpenAI 后端 kick 同步延迟 /
    # default workspace 为 Team)时,auth.openai.com 会默认选 Team 颁发 token,拿到 plan_type=team
    # 的 bundle —— 这个 token 绑在 Team account_id 上,一旦子号离开 Team 就作废(refresh 401),
    # 本地却标成 PERSONAL,用户看到的是"可用免费号"但 Codex 跑不动。必须在这里拒收。
    if use_personal:
        plan = (bundle.get("plan_type") or "").lower()
        if plan != "free":
            logger.error(
                "[Codex] personal 模式拿到 plan_type=%s(期望 free),account_id=%s。"
                "说明账号仍在 Team workspace,OAuth 默认选了 Team → token 绑 Team 后会随踢出作废。"
                "拒收本次 bundle,触发上游 oauth_failed 分类。",
                plan or "unknown",
                bundle.get("account_id"),
            )
            return None

    return bundle


def login_codex_via_session():
    """使用主号 session 直接完成 Codex OAuth 登录。"""
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(code_challenge, state)

    from autoteam.chatgpt_api import ChatGPTTeamAPI

    logger.info("[Codex] 开始使用 session 登录主号 Codex...")
    auth_code = None
    chatgpt = ChatGPTTeamAPI()

    try:
        chatgpt.start()
        session_token = chatgpt.session_token
        if not session_token:
            logger.error("[Codex] 主号会话中未提取到 session token")
            return None
        cookies = []
        if len(session_token) > 3800:
            cookies.extend(
                [
                    {
                        "name": "__Secure-next-auth.session-token.0",
                        "value": session_token[:3800],
                        "domain": "auth.openai.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "__Secure-next-auth.session-token.1",
                        "value": session_token[3800:],
                        "domain": "auth.openai.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                ]
            )
        else:
            cookies.append(
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": "auth.openai.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )

        cookies.extend(
            [
                {
                    "name": "_account",
                    "value": chatgpt.account_id,
                    "domain": "auth.openai.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "oai-did",
                    "value": chatgpt.oai_device_id,
                    "domain": "auth.openai.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )
        chatgpt.context.add_cookies(cookies)
        page = chatgpt.context.new_page()

        def on_request(request):
            nonlocal auth_code
            url = request.url
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]

        def on_response(response):
            nonlocal auth_code
            url = response.url
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url and not auth_code:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]

        def open_oauth_page(tag):
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            _screenshot(page, f"codex_main_{tag}.png")

        page.on("request", on_request)
        page.on("response", on_response)
        open_oauth_page("01_auth_page")

        needs_login = False
        try:
            email_input = page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
            needs_login = email_input.is_visible(timeout=3000)
        except Exception:
            needs_login = False

        if needs_login:
            logger.warning("[Codex] 主号 OAuth 先落到了登录页，尝试先建立 ChatGPT 登录态后重试...")
            page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            _screenshot(page, "codex_main_login_bootstrap.png")
            open_oauth_page("02_auth_retry")

            try:
                email_input = page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
                if email_input.is_visible(timeout=3000):
                    logger.error("[Codex] session 无法直接用于主号 Codex OAuth，仍落在登录页")
                    _screenshot(page, "codex_main_invalid_session.png")
                    return None
            except Exception:
                pass

        for step in range(10):
            if auth_code:
                break

            try:
                workspace_name = get_chatgpt_workspace_name()
                if "workspace" in page.url and workspace_name:
                    ws_btn = page.locator(f'text="{workspace_name}"').first
                    if ws_btn.is_visible(timeout=2000):
                        ws_btn.click()
                        time.sleep(2)
                        logger.info("[Codex] 主号选择 workspace: %s", workspace_name)
                        continue
            except Exception:
                pass

            try:
                consent_btn = page.locator(
                    'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")'
                ).first
                if consent_btn.is_visible(timeout=3000):
                    logger.info("[Codex] 主号点击继续/授权 (step %d)...", step + 1)
                    consent_btn.click()
                    time.sleep(4)
                    continue
            except Exception:
                pass

            try:
                cur = page.url
                if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in cur:
                    parsed = urllib.parse.urlparse(cur)
                    qs = urllib.parse.parse_qs(parsed.query)
                    auth_code = qs.get("code", [None])[0]
                    if auth_code:
                        break
            except Exception:
                pass

            time.sleep(1)

        if not auth_code:
            _screenshot(page, "codex_main_no_callback.png")
            logger.warning("[Codex] 主号未获取到 auth code，当前 URL: %s", page.url)
            return None
    finally:
        chatgpt.stop()

    return _exchange_auth_code(auth_code, code_verifier)


class SessionCodexAuthFlow:
    EMAIL_SELECTORS = [
        'input[name="email"]',
        'input[id="email-input"]',
        'input[id="email"]',
        'input[type="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="邮箱"]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
    ]
    PASSWORD_SELECTORS = [
        'input[name="password"]',
        'input[type="password"]',
    ]
    CODE_SELECTORS = [
        'input[name="code"]',
        'input[placeholder*="验证码"]',
        'input[placeholder*="code" i]',
        'input[inputmode="numeric"]',
        'input[autocomplete="one-time-code"]',
    ]
    OTP_OPTION_SELECTORS = [
        'button:has-text("一次性验证码")',
        'button:has-text("邮箱验证码")',
        'button:has-text("Email login")',
        'button:has-text("email login")',
        'button:has-text("one-time")',
        'button:has-text("One-time")',
        'button:has-text("email code")',
        'button:has-text("Email code")',
        'a:has-text("一次性验证码")',
        'a:has-text("邮箱验证码")',
        'a:has-text("Email login")',
        'a:has-text("one-time")',
    ]

    def __init__(
        self,
        *,
        email,
        session_token,
        account_id,
        workspace_name="",
        password="",
        password_callback=None,
        auth_file_callback=None,
    ):
        self.email = email or ""
        self.password = password or ""
        self.workspace_name = workspace_name or ""
        self.account_id = account_id or ""
        self.session_token = session_token or ""
        self.password_callback = password_callback
        self.auth_file_callback = auth_file_callback or save_auth_file
        self.code_verifier, code_challenge = _generate_pkce()
        self.state = secrets.token_urlsafe(16)
        self.auth_url = _build_auth_url(code_challenge, self.state)
        self.auth_code = None
        self.chatgpt = None
        self.page = None

    def _visible_locator(self, selectors, timeout_ms=5000):
        if not self.page:
            return None

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

    def _detect_step(self):
        if self.auth_code:
            return "completed", None

        cur = self.page.url if self.page else ""
        if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in cur:
            parsed = urllib.parse.urlparse(cur)
            qs = urllib.parse.parse_qs(parsed.query)
            self.auth_code = qs.get("code", [None])[0]
            if self.auth_code:
                return "completed", None

        if self._visible_locator(self.CODE_SELECTORS, timeout_ms=800):
            return "code_required", None
        if self._visible_locator(self.PASSWORD_SELECTORS, timeout_ms=800):
            return "password_required", None
        if self._visible_locator(self.EMAIL_SELECTORS, timeout_ms=800):
            return "email_required", None
        return "unknown", cur

    def _attach_callback_listeners(self):
        def on_request(request):
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in request.url:
                parsed = urllib.parse.urlparse(request.url)
                qs = urllib.parse.parse_qs(parsed.query)
                self.auth_code = qs.get("code", [None])[0]

        def on_response(response):
            if self.auth_code:
                return
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in response.url:
                parsed = urllib.parse.urlparse(response.url)
                qs = urllib.parse.parse_qs(parsed.query)
                self.auth_code = qs.get("code", [None])[0]

        self.page.on("request", on_request)
        self.page.on("response", on_response)

    def _inject_auth_cookies(self):
        cookies = []
        if len(self.session_token) > 3800:
            cookies.extend(
                [
                    {
                        "name": "__Secure-next-auth.session-token.0",
                        "value": self.session_token[:3800],
                        "domain": "auth.openai.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "__Secure-next-auth.session-token.1",
                        "value": self.session_token[3800:],
                        "domain": "auth.openai.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                ]
            )
        else:
            cookies.append(
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": self.session_token,
                    "domain": "auth.openai.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )

        if self.account_id:
            cookies.append(
                {
                    "name": "_account",
                    "value": self.account_id,
                    "domain": "auth.openai.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )

        cookies.append(
            {
                "name": "oai-did",
                "value": self.chatgpt.oai_device_id,
                "domain": "auth.openai.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
        self.chatgpt.context.add_cookies(cookies)

    def _click_workspace_or_consent(self):
        acted = False

        try:
            if "workspace" in self.page.url and self.workspace_name:
                ws_btn = self.page.locator(f'text="{self.workspace_name}"').first
                if ws_btn.is_visible(timeout=1000):
                    ws_btn.click()
                    logger.info("[Codex] 主号选择 workspace: %s", self.workspace_name)
                    time.sleep(2)
                    acted = True
        except Exception:
            pass

        try:
            consent_btn = self.page.locator(
                'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")'
            ).first
            if consent_btn.is_visible(timeout=1000):
                consent_btn.click()
                logger.info("[Codex] 主号点击继续/授权")
                time.sleep(3)
                acted = True
        except Exception:
            pass

        return acted

    def _auto_fill_email(self):
        email_input = self._visible_locator(self.EMAIL_SELECTORS, timeout_ms=1000)
        if not email_input or not self.email:
            return False

        email_input.fill(self.email)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, email_input, ["Continue", "继续", "Log in"])
        time.sleep(3)
        return True

    def _auto_fill_password(self):
        password_input = self._visible_locator(self.PASSWORD_SELECTORS, timeout_ms=1000)
        if not password_input or not self.password:
            return False

        password_input.fill(self.password)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, password_input, ["Continue", "继续", "Log in"])
        time.sleep(5)
        return True

    def _switch_password_to_otp(self):
        otp_entry = self._visible_locator(self.OTP_OPTION_SELECTORS, timeout_ms=1500)
        if not otp_entry:
            return False

        try:
            otp_entry.click()
        except Exception:
            try:
                otp_entry.click(force=True)
            except Exception:
                return False

        logger.info("[Codex] 主号流程检测到密码页，自动切换到一次性验证码登录")
        time.sleep(3)
        return True

    def _advance(self, attempts=12):
        for _ in range(attempts):
            step, detail = self._detect_step()
            if step == "completed":
                return {"step": "completed", "detail": detail}
            if step == "code_required":
                return {"step": "code_required", "detail": detail}
            if step == "password_required":
                if self._switch_password_to_otp():
                    continue
                return {
                    "step": "unsupported_password",
                    "detail": "主号 Codex 当前停留在密码页，且未找到一次性验证码入口",
                }

            if step == "email_required":
                if self._auto_fill_email():
                    continue
                return {"step": "email_required", "detail": detail}

            if self._click_workspace_or_consent():
                continue

            time.sleep(1)

        final_step, detail = self._detect_step()
        return {"step": final_step, "detail": detail}

    def start(self):
        if not self.session_token:
            raise RuntimeError("缺少登录 session")
        if not self.email:
            raise RuntimeError("缺少登录邮箱")

        from autoteam.chatgpt_api import ChatGPTTeamAPI

        self.chatgpt = ChatGPTTeamAPI()
        self.chatgpt.start_with_session(self.session_token, self.account_id, self.workspace_name)
        self.page = self.chatgpt.context.new_page()
        self._attach_callback_listeners()
        self._inject_auth_cookies()
        self.page.goto(self.auth_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        return self._advance()

    def submit_password(self, password):
        self.password = password
        if self.password_callback:
            self.password_callback(password)
        password_input = self._visible_locator(self.PASSWORD_SELECTORS, timeout_ms=5000)
        if not password_input:
            raise RuntimeError("当前 Codex 登录不是密码输入步骤")

        password_input.fill(password)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, password_input, ["Continue", "继续", "Log in"])
        time.sleep(5)
        return self._advance()

    def submit_code(self, code):
        code_input = self._visible_locator(self.CODE_SELECTORS, timeout_ms=5000)
        if not code_input:
            raise RuntimeError("当前 Codex 登录不是验证码输入步骤")

        code_input.fill(code)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, code_input, ["Continue", "继续", "Verify"])
        time.sleep(5)
        return self._advance()

    def complete(self):
        if not self.auth_code:
            raise RuntimeError("未获取到 Codex authorization code")

        bundle = _exchange_auth_code(self.auth_code, self.code_verifier, fallback_email=self.email)
        if not bundle:
            raise RuntimeError("Codex token 交换失败")

        filepath = self.auth_file_callback(bundle)
        return {
            "email": bundle.get("email"),
            "auth_file": filepath,
            "plan_type": bundle.get("plan_type"),
            "bundle": bundle,
        }

    def stop(self):
        if self.chatgpt:
            self.chatgpt.stop()
        self.chatgpt = None
        self.page = None


class MainCodexSyncFlow(SessionCodexAuthFlow):
    def __init__(self):
        super().__init__(
            email=get_admin_email(),
            session_token=get_admin_session_token(),
            account_id=get_chatgpt_account_id(),
            workspace_name=get_chatgpt_workspace_name(),
            password="",
            password_callback=None,
            auth_file_callback=save_main_auth_file,
        )

    def complete(self):
        info = super().complete()

        from autoteam.cpa_sync import sync_main_codex_to_cpa

        sync_main_codex_to_cpa(info["auth_file"])
        return {
            "email": info.get("email"),
            "auth_file": info.get("auth_file"),
            "plan_type": info.get("plan_type"),
        }


def login_main_codex():
    """主号 Codex 登录：使用已保存的管理员 session。"""
    return login_codex_via_session()


def save_auth_file(bundle):
    """保存 CPA 兼容的认证文件。同一邮箱只保留一个文件，优先 team。"""
    ensure_auth_dir()

    email = bundle["email"]
    plan_type = bundle.get("plan_type", "unknown")
    account_id = bundle.get("account_id", "")
    hash_id = hashlib.md5(account_id.encode()).hexdigest()[:8]

    # 清理同一邮箱的旧文件（避免 free/team 并存）
    for old in AUTH_DIR.glob(f"codex-{email}-*.json"):
        old.unlink()
        logger.info("[Codex] 清理旧文件: %s", old.name)

    filename = f"codex-{email}-{plan_type}-{hash_id}.json"
    filepath = AUTH_DIR / filename
    return _write_auth_file(filepath, bundle)


def save_main_auth_file(bundle):
    """保存主号 Codex 认证文件，不进入账号池。"""
    account_id = bundle.get("account_id") or hashlib.md5(bundle.get("email", "main").encode()).hexdigest()[:8]

    for old in AUTH_DIR.glob("codex-main-*.json"):
        old.unlink()
        logger.info("[Codex] 清理旧主号文件: %s", old.name)

    filepath = AUTH_DIR / f"codex-main-{account_id}.json"
    return _write_auth_file(filepath, bundle)


def get_saved_main_auth_file():
    """获取本地已保存的主号 Codex 认证文件路径。"""
    candidates = []
    for path in AUTH_DIR.glob("codex-main-*.json"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except Exception:
            continue
        candidates.append((stat.st_mtime, path.name, path))

    if not candidates:
        return ""

    candidates.sort(reverse=True)
    return str(candidates[0][2].resolve())


def refresh_main_auth_file():
    """基于已保存的管理员登录态，刷新并保存主号 Codex 认证文件。"""
    bundle = login_codex_via_session()
    if not bundle:
        raise RuntimeError("无法基于管理员登录态生成主号 Codex 认证文件")

    auth_file = save_main_auth_file(bundle)
    return {
        "email": bundle.get("email"),
        "auth_file": auth_file,
        "plan_type": bundle.get("plan_type"),
    }


def quota_result_quota_info(info):
    """从 check_codex_quota 返回值中提取额度快照。"""
    if not isinstance(info, dict):
        return None
    quota_info = info.get("quota_info")
    if isinstance(quota_info, dict):
        return quota_info
    if "primary_pct" in info or "weekly_pct" in info:
        return info
    return None


def quota_result_resets_at(info):
    """从 check_codex_quota 返回值中提取恢复时间。"""
    if isinstance(info, dict):
        value = info.get("resets_at")
    else:
        value = info

    try:
        return int(value or 0)
    except Exception:
        return 0


def get_quota_exhausted_info(quota_info, *, limit_reached=False):
    """根据额度快照判断是否已耗尽，并返回耗尽详情。

    SPEC-2 shared/quota-classification §4.2:no_quota 优先级**高于** exhausted。
    `primary_total == 0` / `reset_at == 0 + used_pct == 0` 表示 workspace 未分配配额,
    返回 window="no_quota" 形态,resets_at 给 24h 占位(不应被用作重试依据)。
    """
    if not isinstance(quota_info, dict):
        return None

    primary_pct = int(quota_info.get("primary_pct", 0) or 0)
    weekly_pct = int(quota_info.get("weekly_pct", 0) or 0)
    primary_reset = int(quota_info.get("primary_resets_at", 0) or 0)
    weekly_reset = int(quota_info.get("weekly_resets_at", 0) or 0)
    primary_total = quota_info.get("primary_total")
    primary_remaining = quota_info.get("primary_remaining")

    # SPEC-2 shared/quota-classification I4 — no_quota 短路必须先于 exhausted 判定
    no_quota_signals = []
    if primary_total == 0:
        no_quota_signals.append("primary_total==0")
    elif primary_total is None and primary_pct == 0 and primary_reset == 0 and not limit_reached:
        # rate_limit 字段缺失 / primary_window 缺失走这一支(上游 quota_info 取值都是 0)
        no_quota_signals.append("rate_limit_empty")
    if primary_remaining == 0 and (primary_total == 0 or primary_total is None) and primary_pct == 0:
        no_quota_signals.append("remaining==0+total==0")

    if no_quota_signals and not limit_reached and primary_pct < 100 and weekly_pct < 100:
        return {
            "window": "no_quota",
            "resets_at": int(time.time() + 86400),  # 24h 占位
            "quota_info": quota_info,
            "limit_reached": False,
            "no_quota_signals": no_quota_signals,
        }

    # SPEC-2 shared/quota-classification §4.4 I5 (Round 6 PRD-5 FR-P0) — uninitialized_seat 形态。
    # OpenAI fresh seat 懒初始化:wham 给了占位 reset_at>0 但 total/remaining=null,且 pct=0。
    # 此形态在 wham 层无法与"真无配额"区分,**不**直接判 no_quota,而是返回 window=
    # "uninitialized_seat" + needs_codex_smoke=True,要求上游调 cheap_codex_smoke 二次验证。
    if (
        primary_total is None
        and primary_remaining is None
        and primary_pct == 0
        and weekly_pct == 0
        and primary_reset > 0
        and not limit_reached
    ):
        return {
            "window": "uninitialized_seat",
            "resets_at": int(time.time() + 86400),
            "quota_info": quota_info,
            "limit_reached": False,
            "needs_codex_smoke": True,
            "no_quota_signals": ["workspace_uninitialized"],
        }

    primary_exhausted = primary_pct >= 100
    weekly_exhausted = weekly_pct >= 100
    if not (limit_reached or primary_exhausted or weekly_exhausted):
        return None

    reset_candidates = []
    if primary_exhausted and primary_reset:
        reset_candidates.append(primary_reset)
    if weekly_exhausted and weekly_reset:
        reset_candidates.append(weekly_reset)

    if not reset_candidates:
        if primary_reset:
            reset_candidates.append(primary_reset)
        if weekly_reset:
            reset_candidates.append(weekly_reset)

    resets_at = max(reset_candidates) if reset_candidates else int(time.time() + 18000)

    if primary_exhausted and weekly_exhausted:
        window = "combined"
    elif weekly_exhausted:
        window = "weekly"
    elif primary_exhausted:
        window = "primary"
    else:
        window = "limit"

    return {
        "window": window,
        "resets_at": resets_at,
        "quota_info": quota_info,
        "limit_reached": bool(limit_reached),
    }


# Round 7 FR-D6 — manager 24h 去重 cheap_codex_smoke。
# 同一 account_id 在 24h 内重复调用 cheap_codex_smoke 时,直接返回上次落盘的结果,
# 不再走网络。R2 风险缓解(fresh seat 命中率 > 5% 时 smoke 调用密度爆炸)。
_CODEX_SMOKE_DEDUP_SECONDS = 86400


def _read_codex_smoke_cache(account_id):
    """从 accounts.json 读 last_codex_smoke_at + last_smoke_result。

    匹配规则:account_id 优先按 workspace_account_id 比对,其次 email 比对。
    返回 (epoch_at, result_str) 或 None(无 account_id / 无记录 / 字段缺失)。
    """
    if not account_id:
        return None
    try:
        from autoteam.accounts import load_accounts
        accounts = load_accounts()
    except Exception:
        return None
    target = str(account_id)
    for acc in accounts:
        if acc.get("workspace_account_id") == target or acc.get("email") == target:
            ts = acc.get("last_codex_smoke_at")
            res = acc.get("last_smoke_result")
            if ts and res:
                try:
                    return (float(ts), str(res))
                except (TypeError, ValueError):
                    return None
    return None


def _write_codex_smoke_cache(account_id, result):
    """落盘 last_codex_smoke_at + last_smoke_result,用于 24h 去重。

    匹配规则同 _read_codex_smoke_cache。result 取值 alive/auth_invalid/uncertain。
    任何异常静默吞,cache 失败不阻塞主流程。
    """
    if not account_id or not result:
        return
    try:
        from autoteam.accounts import load_accounts, update_account
    except Exception:
        return
    target = str(account_id)
    try:
        accounts = load_accounts()
    except Exception:
        return
    for acc in accounts:
        if acc.get("workspace_account_id") == target or acc.get("email") == target:
            try:
                update_account(
                    acc["email"],
                    last_codex_smoke_at=time.time(),
                    last_smoke_result=str(result),
                )
            except Exception as exc:
                logger.debug("[Codex smoke] cache 落盘失败(忽略): %s", exc)
            return


def cheap_codex_smoke(access_token, account_id=None, *, timeout=15.0, force=False):
    """SPEC-2 shared/quota-classification §4.4 — uninitialized_seat 二次验证。

    对 codex backend 发一个最小推理请求(reasoning.effort=none + max_output_tokens=1 + stream),
    只读第一帧 SSE 立即关流,不消耗多余 token。

    Round 7 FR-D6:24h 去重 cache。account_id 在 24h 内已有 cache 时直接返回,不走网络;
    传 force=True 可绕过 cache(用于强制刷新场景)。

    返回 (result, detail):
        ("alive", None)              — HTTP 200 + 第一帧含 response.created → 真活号
        ("auth_invalid", reason)     — HTTP 401/403/429 / 4xx 含 quota 关键词 → token/seat 真失效
        ("uncertain", reason)        — HTTP 5xx / network / timeout / 解析异常 → 保留原状态等下轮
        cache 命中时 detail 为 "cache_hit_<原 result>"
    """
    if not access_token:
        return "auth_invalid", "empty_access_token"

    if not account_id:
        try:
            account_id = get_chatgpt_account_id()
        except Exception:
            account_id = None

    # Round 7 FR-D6 — 24h 去重 cache 命中直接返回(force=True 时绕过)
    if not force and account_id:
        cached = _read_codex_smoke_cache(account_id)
        if cached:
            cached_at, cached_result = cached
            if (time.time() - cached_at) < _CODEX_SMOKE_DEDUP_SECONDS:
                logger.debug(
                    "[Codex smoke] 24h cache 命中 account_id=%s result=%s age=%.0fs",
                    account_id, cached_result, time.time() - cached_at,
                )
                return cached_result, f"cache_hit_{cached_result}"

    # cache miss / force=True — 调网络并写回 cache
    result, detail = _cheap_codex_smoke_network(access_token, account_id, timeout=timeout)
    _write_codex_smoke_cache(account_id, result)
    return result, detail


def _cheap_codex_smoke_network(access_token, account_id, *, timeout=15.0):
    """实际走网络的 cheap_codex_smoke 内部函数(Round 7 FR-D6 拆出)。

    与 cheap_codex_smoke 不同点:不查也不写 cache,直接调 codex backend。
    返回值语义同 cheap_codex_smoke。
    """
    import requests

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    payload = {
        "model": "gpt-5",
        "input": "ping",
        "max_output_tokens": 1,
        "stream": True,
        "reasoning": {"effort": "none"},
    }

    try:
        resp = requests.post(
            _CODEX_SMOKE_ENDPOINT,
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        )
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
    ) as exc:
        logger.warning("[Codex smoke] 网络异常(uncertain): %s", exc)
        return "uncertain", f"network:{type(exc).__name__}"
    except Exception as exc:
        logger.warning("[Codex smoke] 未知异常(uncertain): %s", exc)
        return "uncertain", f"exception:{type(exc).__name__}"

    try:
        status_code = resp.status_code
        if status_code in (401, 403, 429):
            try:
                resp.close()
            except Exception:
                pass
            return "auth_invalid", f"http_{status_code}"

        if 500 <= status_code < 600:
            try:
                resp.close()
            except Exception:
                pass
            return "uncertain", f"http_{status_code}"

        if status_code != 200:
            # 4xx 非 401/403/429:body 含 quota 关键词视为 auth_invalid;否则 uncertain
            try:
                body_preview = (resp.text or "").lower()[:1500]
            except Exception:
                body_preview = ""
            try:
                resp.close()
            except Exception:
                pass
            if any(hint in body_preview for hint in _CODEX_SMOKE_QUOTA_HINTS):
                return "auth_invalid", f"http_{status_code}_quota_hint"
            return "uncertain", f"http_{status_code}"

        # HTTP 200 — 读 SSE 第一帧
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if "response.created" in line:
                    return "alive", None
                # 安全兜底:读取多于 8 行仍未见 response.created 视为 uncertain
        except Exception as exc:
            logger.debug("[Codex smoke] iter_lines 异常(uncertain): %s", exc)
            return "uncertain", f"stream:{type(exc).__name__}"
        finally:
            try:
                resp.close()
            except Exception:
                pass

        return "uncertain", "no_response_created_frame"
    finally:
        try:
            resp.close()
        except Exception:
            pass


def check_codex_quota(access_token, account_id=None):
    """
    通过 /backend-api/wham/usage 查询 Codex 额度状态，不消耗额度。
    返回:
        ("ok", quota_info)         — HTTP 200 + 成功解析,额度未触发上限
        ("exhausted", info)        — HTTP 200 + quota 用尽(get_quota_exhausted_info 命中)
        ("auth_error", None)       — **仅** HTTP 401/403,token/seat 真失效
        ("network_error", None)    — DNS/timeout/SSL/连接异常 / 5xx / 429 / json 解析失败 / 其他临时错误

    auth_error 与 network_error 必须严格区分:auth_error 会触发"标记 AUTH_INVALID/重登"等
    破坏性流程,网络抖动绝不能落入该分支(否则一次网络故障可能批量误删账号)。
    quota_info = {"primary_pct": int, "primary_resets_at": int, "weekly_pct": int, "weekly_resets_at": int}
    """
    import requests

    if not account_id:
        account_id = get_chatgpt_account_id()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    try:
        resp = requests.get(
            "https://chatgpt.com/backend-api/wham/usage",
            headers=headers,
            timeout=30,
        )
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
    ) as e:
        logger.warning("[Codex] 网络异常(归类 network_error): %s", e)
        return "network_error", None
    except requests.exceptions.RequestException as e:
        # 其他 requests 异常(ChunkedEncodingError 等)同样归为 network_error,而不是 auth_error
        logger.warning("[Codex] requests 异常(归类 network_error): %s", e)
        return "network_error", None
    except Exception as e:
        # 兜底:未知异常宁可归 network_error,避免因为一次网络抖动批量误标 AUTH_INVALID
        logger.warning("[Codex] 未知异常(归类 network_error,保守处理): %s", e)
        return "network_error", None

    if resp.status_code in (401, 403):
        return "auth_error", None

    # 429 限流 / 5xx 服务端错误 → 临时性故障,归为 network_error,不动账号 status
    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        logger.warning("[Codex] wham/usage 临时错误 %d(归类 network_error): %s", resp.status_code, resp.text[:200])
        return "network_error", None

    if resp.status_code != 200:
        # 4xx(非 401/403/429) 也归为 network_error:可能是 OpenAI 接口在调整,
        # 不能因为一次接口变更把全部账号误判 token 失效
        logger.warning("[Codex] wham/usage 非预期状态 %d(归类 network_error): %s", resp.status_code, resp.text[:200])
        return "network_error", None

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("[Codex] wham/usage 响应 JSON 解析失败(归类 network_error): %s", e)
        return "network_error", None

    rate_limit = data.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}

    # SPEC-2 shared/quota-classification §2.2 — 扩 primary_total / primary_remaining,
    # 让 get_quota_exhausted_info 能区分 no_quota(workspace 未分配)与 exhausted(已用完)
    primary_total_raw = primary.get("limit", primary.get("total"))
    primary_remaining_raw = primary.get("remaining")
    quota_info = {
        "primary_pct": primary.get("used_percent", 0),
        "primary_resets_at": primary.get("reset_at", 0),
        "primary_total": primary_total_raw if isinstance(primary_total_raw, (int, float)) else None,
        "primary_remaining": primary_remaining_raw if isinstance(primary_remaining_raw, (int, float)) else None,
        "weekly_pct": secondary.get("used_percent", 0),
        "weekly_resets_at": secondary.get("reset_at", 0),
        # Round 7 P2.5:把 wham/usage 的原始 rate_limit + primary_window 子树注入 quota_info,
        # 让 manager 在 record_failure(no_quota_assigned) 时能附 raw_rate_limit 用于事后排查。
        "raw_rate_limit": rate_limit,
        "primary_window": primary,
    }

    # SPEC-2 shared/quota-classification §4.2 — no_quota 单独分支:rate_limit 字段
    # 完全缺失 / primary_window 缺失也归 no_quota(空载也是无配额信号)
    rate_limit_missing = (not rate_limit) or (not primary)
    if rate_limit_missing:
        return "no_quota", {
            "window": "no_quota",
            "resets_at": int(time.time() + 86400),
            "quota_info": quota_info,
            "limit_reached": False,
            "no_quota_signals": ["rate_limit_or_primary_missing"],
            "raw_rate_limit": rate_limit,
        }

    exhausted_info = get_quota_exhausted_info(quota_info, limit_reached=bool(rate_limit.get("limit_reached")))
    if exhausted_info:
        # window="no_quota" 是 get_quota_exhausted_info 内部短路出的形态;独立分类返回
        if exhausted_info.get("window") == "no_quota":
            return "no_quota", exhausted_info
        # window="uninitialized_seat"(I5)— Round 6 PRD-5 FR-P0:必须用 cheap_codex_smoke 二次验证
        if exhausted_info.get("window") == "uninitialized_seat":
            smoke_result, smoke_detail = cheap_codex_smoke(access_token, account_id=account_id)
            exhausted_info["last_smoke_result"] = smoke_result
            if smoke_detail is not None:
                exhausted_info["last_smoke_detail"] = smoke_detail
            if smoke_result == "alive":
                # fresh seat 真活,token 有效 → 维持 ok,但带 smoke_verified 标记
                quota_info_verified = dict(quota_info)
                quota_info_verified["smoke_verified"] = True
                quota_info_verified["last_smoke_result"] = "alive"
                return "ok", quota_info_verified
            if smoke_result == "auth_invalid":
                # codex backend 401/403/quota → 当作真 auth_error,触发 reconcile 重登
                return "auth_error", None
            # uncertain (5xx/network/timeout) → 保持原状态等下轮,归 network_error
            return "network_error", None
        return "exhausted", exhausted_info

    return "ok", quota_info


def refresh_access_token(refresh_token):
    """刷新 access token"""
    import requests

    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if resp.status_code != 200:
        logger.error("[Codex] Token 刷新失败: %d", resp.status_code)
        return None

    data = resp.json()
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token", refresh_token),
        "id_token": data.get("id_token", ""),
        "expires_in": data.get("expires_in", 3600),
    }
