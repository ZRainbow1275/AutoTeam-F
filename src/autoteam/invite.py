#!/usr/bin/env python3
import autoteam.display  # noqa: F401 — 自动设置虚拟显示器

"""
ChatGPT Team 自动邀请 + 注册工具

完整流程:
1. CloudMail 创建临时邮箱
2. ChatGPT API 发送 Team 邀请
3. CloudMail 收取邀请邮件，提取邀请链接
4. Playwright 打开邀请链接，注册 ChatGPT 账号
5. CloudMail 收取验证码邮件，自动填入
6. 完成注册并加入 workspace

用法:
    python invite.py
"""

import logging
import os
import sys
import time

from playwright.sync_api import sync_playwright

from autoteam.accounts import (
    SEAT_CHATGPT,
    SEAT_CODEX,
    SEAT_UNKNOWN,
    add_account,
    update_account,
)
from autoteam.chatgpt_api import ChatGPTTeamAPI
from autoteam.cloudmail import CloudMailClient
from autoteam.config import get_playwright_launch_options
from autoteam.identity import random_age, random_birthday, random_full_name, random_password


def _seat_label_from_raw(raw_seat: str) -> str:
    """把 invite_member 返回的 _seat_type 字面量翻译成 accounts.SEAT_* 常量。"""
    return {
        "chatgpt": SEAT_CHATGPT,
        "usage_based": SEAT_CODEX,
    }.get(raw_seat or "", SEAT_UNKNOWN)


logger = logging.getLogger(__name__)

MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))
SCREENSHOT_DIR = "screenshots"


class RegisterBlocked(Exception):
    """
    注册流程被风控或确定性错误阻断时抛出；调用方按 reason 做分流处理：
    - is_phone=True: OpenAI 要求手机验证，当前账号放弃（用户明确不绕过）
    - is_duplicate=True: 邮箱已被占用，当前账号放弃，换邮箱重来
    - 其他: 单步逻辑错误，按现有 retry 流程处理
    """

    def __init__(self, step, reason, *, is_phone=False, is_duplicate=False):
        super().__init__(f"[{step}] {reason}")
        self.step = step
        self.reason = reason
        self.is_phone = is_phone
        self.is_duplicate = is_duplicate


# 手机验证页面的识别特征（URL 片段 + 页面文本）
# URL 是强信号；文本只匹配"动作 + phone"短语，不匹配裸 "phone number" / "sms"，避免
# 注册帮助区里偶尔出现的短语触发误报。
_PHONE_URL_HINTS = ("verify-phone", "add-phone", "/phone", "phone_verification", "phone-number")
_PHONE_TEXT_HINTS = (
    "verify your phone",
    "add your phone",
    "verify phone",
    "verification code to your phone",
    "add a phone number",
    "add a phone",
    "enter your phone",
    "phone verification",
    "we'll text you",
    "请输入手机号",
    "手机号码",
    "验证手机",
    "添加手机",
)

# 邮箱重复的识别特征（文案；各语言/版本都要覆盖）
_DUPLICATE_TEXT_HINTS = (
    "already have an account",
    "already exists",
    "already been used",
    "this user already exists",
    "please use a different email",
    "different email",
    "email is already taken",
    "account with this email",
    "该邮箱已被使用",
    "邮箱已存在",
    "请使用其他邮箱",
    "电子邮件已被使用",
)


def detect_phone_verification(page):
    """若当前页面要求手机验证返回 True。URL 命中优先；文本命中需配合电话输入框。"""
    try:
        url = (page.url or "").lower()
        if any(hint in url for hint in _PHONE_URL_HINTS):
            return True
        body = page.inner_text("body")[:1500].lower()
        if not any(hint in body for hint in _PHONE_TEXT_HINTS):
            return False
        # 仅当页面上真的有电话输入控件时才判为阻塞；否则可能是说明文字/footer
        try:
            tel_input = page.locator('input[type="tel"], input[name*="phone" i], input[autocomplete*="tel" i]').first
            if tel_input.is_visible(timeout=500):
                return True
        except Exception as exc:
            logger.debug("[注册] detect_phone tel_input 探测异常: %s", exc)
        return False
    except Exception as exc:
        logger.debug("[注册] detect_phone_verification 异常（当作未阻塞处理）: %s", exc)
        return False


def detect_duplicate_email(page):
    """若当前页面提示邮箱已被占用返回 True。"""
    try:
        body = page.inner_text("body")[:1500].lower()
        return any(hint in body for hint in _DUPLICATE_TEXT_HINTS)
    except Exception as exc:
        logger.debug("[注册] detect_duplicate_email 异常（当作无 duplicate 处理）: %s", exc)
        return False


def assert_not_blocked(page, step):
    """任何步骤后调用，检测到阻断项立刻 raise。"""
    if detect_phone_verification(page):
        logger.error("[注册] [%s] 触发 add-phone 手机验证，放弃当前账号 | URL=%s", step, page.url)
        raise RegisterBlocked(step, "add-phone 手机验证", is_phone=True)
    if detect_duplicate_email(page):
        logger.error("[注册] [%s] 邮箱已被占用，放弃当前账号 | URL=%s", step, page.url)
        raise RegisterBlocked(step, "duplicate email", is_duplicate=True)


def screenshot(page, name):
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = f"{SCREENSHOT_DIR}/{name}"
    page.screenshot(path=path, full_page=True)
    logger.debug("[截图] %s", path)


def find_and_click(page, selectors, label="元素", timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                logger.debug("[注册] 找到%s: %s", label, sel)
                loc.click()
                return True
        except Exception:
            continue
    return False


def find_visible(page, selectors, label="元素", timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                logger.debug("[注册] 找到%s: %s", label, sel)
                return loc
        except Exception:
            continue
    return None


def wait_for_cloudflare(page, max_wait=60):
    for i in range(max_wait // 5):
        html = page.content()[:2000].lower()
        if "verify you are human" not in html and "challenge" not in page.url:
            return True
        logger.info("[注册] 等待 Cloudflare... (%ds)", i * 5)
        time.sleep(5)
    return False


def register_with_invite(page, invite_link, email, mail_client, password=None):
    """用邀请链接注册 ChatGPT 账号并加入 workspace，返回 (success, password)"""

    logger.info("[注册] 打开邀请链接...")
    page.goto(invite_link, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    wait_for_cloudflare(page)
    screenshot(page, "reg_01_invite_page.png")
    logger.info("[注册] 当前 URL: %s", page.url)

    # 可能需要点击 Sign up
    find_and_click(
        page,
        [
            'button:has-text("Sign up")',
            'a:has-text("Sign up")',
            'button:has-text("Create account")',
            'a:has-text("Create account")',
            'button:has-text("注册")',
        ],
        "注册按钮",
        timeout=5000,
    )
    time.sleep(3)
    screenshot(page, "reg_02_signup.png")

    # 输入邮箱
    logger.info("[注册] 输入邮箱: %s", email)
    email_input = find_visible(
        page,
        [
            'input[name="email"]',
            'input[type="email"]',
            'input[placeholder*="email" i]',
            'input[id="email"]',
            "#email-input",
            'input[autocomplete="email"]',
        ],
        "邮箱输入框",
    )

    if email_input:
        email_input.fill(email)
        time.sleep(1)

        # 点击 Continue
        find_and_click(
            page,
            [
                'button:has-text("Continue")',
                'button:has-text("继续")',
                'button[type="submit"]',
            ],
            "继续按钮",
        )
        time.sleep(5)
        screenshot(page, "reg_03_after_email.png")
        assert_not_blocked(page, "email_submit")
    else:
        logger.info("[注册] 未找到邮箱输入框，可能页面已自动填入")
        screenshot(page, "reg_03_no_email_input.png")

    # 可能需要输入密码（注册流程）
    pwd_input = find_visible(
        page,
        [
            'input[name="password"]',
            'input[type="password"]',
            'input[id="password"]',
        ],
        "密码输入框",
        timeout=5000,
    )

    if pwd_input:
        if not password:
            password = random_password()
        logger.info("[注册] 设置密码（类人随机）")
        pwd_input.fill(password)
        time.sleep(1)

        find_and_click(
            page,
            [
                'button:has-text("Continue")',
                'button:has-text("继续")',
                'button[type="submit"]',
            ],
            "继续按钮",
        )
        time.sleep(5)
        screenshot(page, "reg_04_after_password.png")
        assert_not_blocked(page, "password_submit")

    # 等待验证码邮件
    logger.info("[注册] 等待 ChatGPT 发送验证码到 %s...", email)
    verification_code = None
    try:
        # 搜索来自 OpenAI 的验证码邮件（不是邀请邮件）
        start = time.time()
        while time.time() - start < MAIL_TIMEOUT:
            emails = mail_client.search_emails_by_recipient(email, size=10)
            for em in emails:
                subject = em.get("subject", "").lower()
                sender = em.get("sendEmail", "").lower()
                # 跳过邀请邮件，只要验证码邮件
                if "invited" in subject or "invitation" in subject:
                    continue
                if "openai" in sender or "chatgpt" in sender:
                    verification_code = mail_client.extract_verification_code(em)
                    if verification_code:
                        logger.info("[CloudMail] 收到验证码: %s", verification_code)
                        break
            if verification_code:
                break
            elapsed = int(time.time() - start)
            print(f"\r[CloudMail] 等待验证码... ({elapsed}s)", end="", flush=True)
            time.sleep(3)
    except Exception as e:
        logger.error("[注册] 等待验证码异常: %s", e)

    if not verification_code:
        logger.warning("[注册] 未自动获取到验证码")
        screenshot(page, "reg_05_no_code.png")
        return False, password

    # 输入验证码
    logger.info("[注册] 输入验证码: %s", verification_code)
    screenshot(page, "reg_05_before_code.png")

    # 检查是否是多个单字符输入框
    single_inputs = page.locator('input[maxlength="1"]').all()
    if len(single_inputs) >= 4:
        logger.debug("[注册] 检测到 %d 个单字符输入框", len(single_inputs))
        for i, char in enumerate(verification_code):
            if i < len(single_inputs):
                single_inputs[i].fill(char)
                time.sleep(0.2)
    else:
        code_input = find_visible(
            page,
            [
                'input[name="code"]',
                'input[placeholder*="code" i]',
                'input[placeholder*="验证" i]',
                'input[type="text"]',
                'input[inputmode="numeric"]',
            ],
            "验证码输入框",
        )
        if code_input:
            code_input.fill(verification_code)
        else:
            logger.warning("[注册] 未找到验证码输入框")
            screenshot(page, "reg_05_no_code_input.png")
            return False, password

    time.sleep(1)

    # 点击确认
    find_and_click(
        page,
        [
            'button:has-text("Continue")',
            'button:has-text("Verify")',
            'button:has-text("Submit")',
            'button[type="submit"]',
        ],
        "确认按钮",
    )

    time.sleep(8)
    screenshot(page, "reg_06_after_code.png")
    logger.info("[注册] 当前 URL: %s", page.url)
    assert_not_blocked(page, "code_submit")

    # 随机身份（每个账号不同，降低批量注册特征）
    bday = random_birthday()
    full_name = random_full_name()
    age_value = random_age()
    logger.info(
        "[注册] 本次身份: name=%s birthday=%s/%s/%s age=%s",
        full_name,
        bday["year"],
        bday["month"],
        bday["day"],
        age_value,
    )

    # 填写个人信息（全名 + 生日/年龄）
    name_input = find_visible(
        page,
        [
            'input[name="name"]',
            'input[placeholder*="name" i]',
            'input[id="name"]',
            'input[placeholder*="全名" i]',
        ],
        "名字输入框",
        timeout=5000,
    )

    if name_input:
        name_input.fill(full_name)
        time.sleep(0.5)

    # 自适应：生日日期（spinbutton）或年龄（普通 input）
    filled_age = False
    spinbuttons = page.locator('[role="spinbutton"]').all()
    if len(spinbuttons) >= 3:
        # 类型 A：React Aria DateField（年/月/日 spinbutton）
        try:
            page.locator("text=生日日期").click()
            time.sleep(0.5)
        except Exception:
            pass
        for sb, val in zip(spinbuttons[:3], [bday["year"], bday["month"], bday["day"]]):
            sb.click(force=True)
            time.sleep(0.2)
            page.keyboard.type(val, delay=80)
            time.sleep(0.3)
        logger.info("[注册] 填入生日: %s/%s/%s (spinbutton)", bday["year"], bday["month"], bday["day"])
        filled_age = True
    else:
        # 类型 B：普通年龄数字输入框
        age_input = find_visible(
            page,
            [
                'input[name="age"]',
                'input[id="age"]',
                'input[placeholder*="age" i]',
                'input[placeholder*="年龄" i]',
                'input[type="number"]',
            ],
            "年龄输入框",
            timeout=3000,
        )
        if age_input:
            age_input.fill(age_value)
            logger.info("[注册] 填入年龄: %s", age_value)
            filled_age = True

    if name_input or filled_age:
        find_and_click(
            page,
            [
                'button:has-text("完成帐户创建")',
                'button:has-text("Complete")',
                'button:has-text("Continue")',
                'button:has-text("Agree")',
                'button[type="submit"]',
            ],
            "完成按钮",
        )
        time.sleep(8)
        screenshot(page, "reg_07_after_profile.png")
        assert_not_blocked(page, "profile_submit")

    # 可能需要接受条款 / 加入 workspace
    find_and_click(
        page,
        [
            'button:has-text("Accept")',
            'button:has-text("Agree")',
            'button:has-text("Join")',
            'button:has-text("Join workspace")',
            'button:has-text("加入")',
            'button:has-text("Accept invite")',
        ],
        "加入/接受按钮",
        timeout=5000,
    )
    time.sleep(5)
    screenshot(page, "reg_08_final.png")

    # 检查结果
    current_url = page.url
    page_text = page.inner_text("body")[:500].lower()

    if "chatgpt.com" in current_url and "auth" not in current_url:
        logger.info("[注册] 注册成功并已加入 workspace!")
        return True, password
    elif "workspace" in page_text or "welcome" in page_text:
        logger.info("[注册] 已加入 workspace!")
        return True, password
    else:
        logger.warning("[注册] 注册流程可能未完成，请查看截图")
        return False, password


def run():
    mail_client = None
    account_id = None
    chatgpt = None

    try:
        # Step 1: 创建临时邮箱
        mail_client = CloudMailClient()
        mail_client.login()
        account_id, email = mail_client.create_temp_email()
        logger.info("[邀请] 临时邮箱: %s", email)

        # Step 2: 发送 Team 邀请。invite_member 内部已带 default→usage_based 兜底,
        # 我们只需读 _seat_type 字段决定落盘的 seat_type 常量。
        chatgpt = ChatGPTTeamAPI()
        chatgpt.start()
        status, data = chatgpt.invite_member(email, seat_type="default")

        raw_seat = (data or {}).get("_seat_type", "unknown") if isinstance(data, dict) else "unknown"
        seat_label = _seat_label_from_raw(raw_seat)

        if status != 200 or raw_seat == "unknown":
            err_kind = (data or {}).get("_error_kind", "unknown") if isinstance(data, dict) else "unknown"
            errored = (data or {}).get("_errored_emails") if isinstance(data, dict) else None
            logger.error(
                "[邀请] 邀请失败 (HTTP %d, kind=%s, errored=%s)",
                status,
                err_kind,
                bool(errored),
            )
            return False
        logger.info("[邀请] 邀请已发送 (seat_type=%s → %s)", raw_seat, seat_label)
        # 邀请发送成功就把账号入池(seat_type 落盘),即便后续注册流程失败,
        # 至少 accounts.json 留有一条记录给上游 reconcile / fill 使用。
        add_account(email, "", cloudmail_account_id=account_id, seat_type=seat_label)

        # Step 3: 等待邀请邮件
        logger.info("[邀请] 等待邀请邮件...")
        invite_link = None
        try:
            email_data = mail_client.wait_for_email(
                to_email=email,
                timeout=MAIL_TIMEOUT,
                sender_keyword="openai",
            )
            invite_link = mail_client.extract_invite_link(email_data)
        except TimeoutError:
            logger.error("[邀请] 等待邀请邮件超时")
        except Exception as e:
            logger.error("[邀请] 获取邀请邮件失败: %s", e)

        if not invite_link:
            logger.error("[邀请] 未获取到邀请链接")
            return False

        logger.info("[邀请] 邀请链接: %s", invite_link)

        # Step 4: 关闭 ChatGPT API 浏览器，开新浏览器做注册
        chatgpt.stop()
        chatgpt = None

        logger.info("[邀请] 开始注册 ChatGPT 账号")

        with sync_playwright() as p:
            browser = p.chromium.launch(**get_playwright_launch_options())
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            result, pwd = register_with_invite(page, invite_link, email, mail_client)

            screenshot(page, "final.png")
            browser.close()

        if result:
            logger.info("[邀请] %s 已注册并加入 ChatGPT Team", email)
            # 注册成功后再把 seat_type 复写一次 — 防止 add_account 时账号已存在被旧值覆盖
            update_account(email, seat_type=seat_label)
        else:
            logger.error("[邀请] 流程未完成，请查看 screenshots/ 目录")

        return result

    finally:
        if chatgpt:
            chatgpt.stop()
        # 不删除临时邮箱，保留账号
        if mail_client and account_id:
            logger.info("[邀请] 临时邮箱保留: %s (accountId=%s)", email, account_id)


def main():
    logger.info("ChatGPT Team 自动邀请 + 注册工具")
    result = run()
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
