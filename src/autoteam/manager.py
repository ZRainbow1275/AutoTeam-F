#!/usr/bin/env python3
import autoteam.display  # noqa: F401 — 自动设置虚拟显示器

"""
账号轮转管理器

功能:
- 检查所有活跃账号的 Codex 额度
- 额度用完的账号移出 Team，放入 standby
- 从 standby 中选额度恢复的旧账号重新邀请
- 无可用旧账号时才创建新账号
- 自动完成注册并保存 Codex 认证文件

用法:
    python manager.py check     # 检查所有活跃账号额度
    python manager.py rotate    # 执行一次轮转（检查 + 替换）
    python manager.py add       # 手动添加一个新账号
    python manager.py status    # 查看所有账号状态
"""

import sys
import os
import json
import time
from pathlib import Path

from autoteam.accounts import (
    load_accounts, save_accounts, find_account, add_account, update_account,
    get_active_accounts, get_standby_accounts, get_next_reusable_account,
    STATUS_ACTIVE, STATUS_EXHAUSTED, STATUS_STANDBY, STATUS_PENDING,
)
from autoteam.chatgpt_api import ChatGPTTeamAPI
from autoteam.cloudmail import CloudMailClient
from autoteam.codex_auth import (
    login_codex_via_browser, save_auth_file, check_codex_quota,
    refresh_access_token,
)
from autoteam.config import CHATGPT_ACCOUNT_ID
from autoteam.cpa_sync import sync_to_cpa

MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))


def sync_account_states(chatgpt_api=None):
    """根据 Team 实际成员列表同步本地账号状态"""
    accounts = load_accounts()
    if not accounts:
        return

    # 获取 Team 实际成员
    need_stop = False
    if not chatgpt_api or not chatgpt_api.browser:
        try:
            chatgpt_api = ChatGPTTeamAPI()
            chatgpt_api.start()
            need_stop = True
        except Exception:
            # Playwright 不可用（event loop 冲突等），跳过同步
            return

    try:
        path = f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/users"
        result = chatgpt_api._api_fetch("GET", path)
        if result["status"] != 200:
            return

        data = json.loads(result["body"])
        members = data.get("items", data.get("users", data.get("members", [])))
        team_emails = {m.get("email", "").lower() for m in members}
    finally:
        if need_stop:
            chatgpt_api.stop()

    # 对照更新状态
    from autoteam.config import CLOUDMAIL_DOMAIN
    domain_suffix = CLOUDMAIL_DOMAIN.lstrip("@") if CLOUDMAIL_DOMAIN else ""

    changed = False
    local_email_set = {a["email"].lower() for a in accounts}

    for acc in accounts:
        email = acc["email"].lower()
        in_team = email in team_emails

        if in_team and acc["status"] in (STATUS_STANDBY, STATUS_PENDING):
            acc["status"] = STATUS_ACTIVE
            changed = True
        elif not in_team and acc["status"] == STATUS_ACTIVE:
            acc["status"] = STATUS_STANDBY
            changed = True

    # Team 中有我们域名但本地无记录的成员 → 自动添加
    if domain_suffix:
        for email in team_emails:
            if domain_suffix in email and email not in local_email_set:
                accounts.append({
                    "email": email,
                    "password": "",
                    "cloudmail_account_id": None,
                    "status": STATUS_ACTIVE,
                    "auth_file": None,
                    "quota_exhausted_at": None,
                    "quota_resets_at": None,
                    "created_at": time.time(),
                    "last_active_at": None,
                })
                changed = True
                print(f"[同步] 发现 Team 中新成员: {email}（已添加到本地）")

    if changed:
        save_accounts(accounts)


def cmd_status():
    """显示所有账号状态（先同步 Team 实际状态，active 账号实时查询额度）"""
    sync_account_states()

    accounts = load_accounts()
    if not accounts:
        print("暂无账号")
        return

    # active 账号实时查询额度
    quota_cache = {}
    for acc in accounts:
        if acc["status"] == STATUS_ACTIVE and acc.get("auth_file") and Path(acc["auth_file"]).exists():
            auth_data = json.loads(Path(acc["auth_file"]).read_text())
            access_token = auth_data.get("access_token")
            if access_token:
                status, info = check_codex_quota(access_token)
                if status == "ok" and isinstance(info, dict):
                    quota_cache[acc["email"]] = info

    print(f"\n{'邮箱':<40} {'状态':<10} {'5h额度':<12} {'周额度':<12} {'5h重置':<14} {'周重置':<14}")
    print("-" * 105)
    for acc in accounts:
        email = acc["email"]
        status = acc["status"]
        qi = quota_cache.get(email)

        if qi:
            p_pct = f"{100 - qi['primary_pct']}%"
            w_pct = f"{100 - qi['weekly_pct']}%"
            p_reset = time.strftime("%m-%d %H:%M", time.localtime(qi["primary_resets_at"])) if qi.get("primary_resets_at") else "-"
            w_reset = time.strftime("%m-%d %H:%M", time.localtime(qi["weekly_resets_at"])) if qi.get("weekly_resets_at") else "-"
        elif acc.get("quota_resets_at"):
            rt = acc["quota_resets_at"]
            if time.time() >= rt:
                p_pct = "已恢复"
            else:
                p_pct = "100%"
            w_pct = "-"
            p_reset = time.strftime("%m-%d %H:%M", time.localtime(rt)) if time.time() < rt else "已恢复"
            w_reset = "-"
        else:
            p_pct = "-"
            w_pct = "-"
            p_reset = "-"
            w_reset = "-"

        print(f"{email:<40} {status:<10} {p_pct:<12} {w_pct:<12} {p_reset:<14} {w_reset:<14}")

    active = [a for a in accounts if a["status"] == STATUS_ACTIVE]
    standby = [a for a in accounts if a["status"] == STATUS_STANDBY]
    exhausted = [a for a in accounts if a["status"] == STATUS_EXHAUSTED]
    print(f"\n活跃: {len(active)}  待命: {len(standby)}  额度用完: {len(exhausted)}  总计: {len(accounts)}")


def _check_and_refresh(acc):
    """检查单个账号额度，401 时自动刷新 token。返回 (status_str, info)
    info: exhausted 时为 resets_at，ok 时为 quota_info dict
    """
    email = acc["email"]
    auth_file = acc.get("auth_file")

    if not auth_file or not Path(auth_file).exists():
        return "no_auth", None

    auth_data = json.loads(Path(auth_file).read_text())
    access_token = auth_data.get("access_token")
    rt = auth_data.get("refresh_token")

    if not access_token:
        return "no_auth", None

    status, info = check_codex_quota(access_token)

    # token 过期，尝试刷新
    if status == "auth_error" and rt:
        print(f"[{email}] token 过期，尝试刷新...")
        new_tokens = refresh_access_token(rt)
        if new_tokens:
            auth_data["access_token"] = new_tokens["access_token"]
            auth_data["refresh_token"] = new_tokens.get("refresh_token", rt)
            auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            Path(auth_file).write_text(json.dumps(auth_data, indent=2))
            print(f"[{email}] token 已刷新，重新检查额度...")
            status, info = check_codex_quota(new_tokens["access_token"])
        else:
            print(f"[{email}] token 刷新失败")

    return status, info


def cmd_check():
    """只检查 active 账号的额度，无认证文件或 auth_error 的自动重新登录 Codex"""
    from autoteam.config import CLOUDMAIL_DOMAIN

    accounts = load_accounts()
    all_active = [a for a in accounts if a["status"] == STATUS_ACTIVE]

    # 区分：有认证文件的 vs 无认证文件的
    active_with_auth = []
    no_auth_list = []
    for a in all_active:
        if a.get("auth_file") and Path(a["auth_file"]).exists():
            active_with_auth.append(a)
        else:
            # 只管我们域名的账号
            if CLOUDMAIL_DOMAIN and CLOUDMAIL_DOMAIN.lstrip("@") in a["email"]:
                no_auth_list.append(a)

    if not active_with_auth and not no_auth_list:
        print("没有可检查的 active 账号")
        return []

    # 检查有认证文件的账号额度
    exhausted_list = []
    auth_error_list = []

    if active_with_auth:
        print(f"\n检查 {len(active_with_auth)} 个 active 账号的额度...\n")
        for acc in active_with_auth:
            email = acc["email"]
            status_str, info = _check_and_refresh(acc)

            if status_str == "ok":
                if isinstance(info, dict):
                    p_remain = 100 - info.get("primary_pct", 0)
                    w_remain = 100 - info.get("weekly_pct", 0)
                    p_reset = info.get("primary_resets_at", 0)
                    w_reset = info.get("weekly_resets_at", 0)
                    p_time = time.strftime("%m-%d %H:%M", time.localtime(p_reset)) if p_reset else "?"
                    w_time = time.strftime("%m-%d %H:%M", time.localtime(w_reset)) if w_reset else "?"
                    print(f"[{email}] ✅ 5h: {p_remain}% (重置 {p_time}) | 周: {w_remain}% (重置 {w_time})")
                else:
                    print(f"[{email}] ✅ 额度可用")
            elif status_str == "exhausted":
                resets_at = info
                print(f"[{email}] ❌ 额度已用完")
                update_account(email,
                    status=STATUS_EXHAUSTED,
                    quota_exhausted_at=time.time(),
                    quota_resets_at=resets_at,
                )
                exhausted_list.append(acc)
            elif status_str == "auth_error":
                print(f"[{email}] ⚠️  认证失败，需要重新登录 Codex")
                auth_error_list.append(acc)

    # 无认证文件的 active 账号也需要重新登录
    if no_auth_list:
        print(f"\n发现 {len(no_auth_list)} 个 active 账号无认证文件，需要登录 Codex:")
        for a in no_auth_list:
            print(f"  {a['email']}")
        auth_error_list.extend(no_auth_list)

    # auth_error + 无认证文件的统一重新登录 Codex
    if auth_error_list:
        print(f"\n=== 重新登录 {len(auth_error_list)} 个 token 失效的账号 ===")
        mail_client = CloudMailClient()
        mail_client.login()
        for acc in auth_error_list:
            email = acc["email"]
            password = acc.get("password", "")
            print(f"\n[{email}] 重新 Codex 登录...")
            bundle = login_codex_via_browser(email, password, mail_client=mail_client)
            if bundle:
                auth_file = save_auth_file(bundle)
                update_account(email, auth_file=auth_file)
                print(f"[{email}] ✅ token 已更新")
                # 重新检查额度
                status_str, info = _check_and_refresh(
                    find_account(load_accounts(), email))
                if status_str == "exhausted":
                    update_account(email,
                        status=STATUS_EXHAUSTED,
                        quota_exhausted_at=time.time(),
                        quota_resets_at=info,
                    )
                    exhausted_list.append(acc)
                    print(f"[{email}] ❌ 额度已用完")
                elif status_str == "ok":
                    print(f"[{email}] ✅ 额度可用")
            else:
                print(f"[{email}] ❌ Codex 登录失败，标记为 standby")
                update_account(email, status=STATUS_STANDBY)

    return exhausted_list


def remove_from_team(chatgpt_api, email):
    """将账号从 Team 中移除"""
    # 先获取成员列表找到 user_id
    path = f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/users"
    result = chatgpt_api._api_fetch("GET", path)

    if result["status"] != 200:
        print(f"[Team] 获取成员列表失败: {result['status']}")
        return False

    try:
        data = json.loads(result["body"])
        members = data.get("items", data.get("users", data.get("members", [])))
    except Exception:
        print(f"[Team] 解析成员列表失败")
        return False

    # 找到对应邮箱的成员
    target_user_id = None
    for member in members:
        member_email = member.get("email", "")
        if member_email.lower() == email.lower():
            target_user_id = member.get("user_id") or member.get("id")
            break

    if not target_user_id:
        print(f"[Team] 未在成员列表中找到 {email}")
        # 可能已经不在 team 了
        return True

    # 删除成员
    delete_path = f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/users/{target_user_id}"
    result = chatgpt_api._api_fetch("DELETE", delete_path)

    if result["status"] in (200, 204):
        print(f"[Team] ✅ 已将 {email} 移出 Team")
        return True
    else:
        print(f"[Team] ❌ 移除失败: {result['status']} {result['body'][:200]}")
        return False


def invite_to_team(chatgpt_api, email, seat_type="default"):
    """邀请账号加入 Team。旧账号用 default，新账号用 usage_based。"""
    status, data = chatgpt_api.invite_member(email, seat_type=seat_type)
    if status == 200 and isinstance(data, dict):
        errored = data.get("errored_emails", [])
        if errored:
            err_msg = errored[0].get("error", "unknown")
            print(f"[Team] 邀请 {email} 被拒绝: {err_msg}")
            # default 失败则尝试 usage_based
            if seat_type == "default":
                print(f"[Team] 尝试 usage_based 方式...")
                return invite_to_team(chatgpt_api, email, seat_type="usage_based")
            return False
    return status == 200


def _complete_registration(email, password, invite_link, mail_client):
    """完成注册 + Codex 登录（从已有邀请链接继续）"""
    from autoteam.invite import register_with_invite
    from playwright.sync_api import sync_playwright

    print(f"开始注册 {email}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        result, password = register_with_invite(page, invite_link, email, mail_client, password=password)
        browser.close()

    if not result:
        print(f"❌ 注册 {email} 失败")
        return None

    # Codex 登录
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    if bundle:
        auth_file = save_auth_file(bundle)
        update_account(email, status=STATUS_ACTIVE, auth_file=auth_file, last_active_at=time.time())
        print(f"✅ 账号就绪: {email}")
        return email
    else:
        update_account(email, status=STATUS_ACTIVE)
        print(f"⚠️  账号已加入 Team 但 Codex 登录失败: {email}")
        return email


def _check_pending_invites(chatgpt_api, mail_client):
    """
    检查 pending invites 中是否有已收到邮件的邀请，有则继续完成注册。
    返回成功完成的邮箱列表。
    """
    import uuid

    result = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/invites")
    if result["status"] != 200:
        return []

    inv_data = json.loads(result["body"])
    invites = inv_data if isinstance(inv_data, list) else inv_data.get("invites", inv_data.get("account_invites", []))

    if not invites:
        return []

    print(f"\n[Pending] 发现 {len(invites)} 个待处理邀请:")
    completed = []

    for inv in invites:
        inv_email = inv.get("email_address", "")
        print(f"  检查 {inv_email} 是否已收到邮件...")

        # 从 CloudMail 搜索该邮箱的邀请邮件
        emails = mail_client.search_emails_by_recipient(inv_email, size=5)
        invite_link = None
        for em in emails:
            sender = em.get("sendEmail", "").lower()
            if "openai" in sender:
                invite_link = mail_client.extract_invite_link(em)
                if invite_link:
                    break

        if not invite_link:
            print(f"    未收到邮件，跳过")
            continue

        print(f"    已收到邀请邮件，继续注册流程...")

        # 确保本地有账号记录
        acc = find_account(load_accounts(), inv_email)
        if acc:
            password = acc.get("password", f"Tmp_{uuid.uuid4().hex[:12]}!")
        else:
            password = f"Tmp_{uuid.uuid4().hex[:12]}!"
            add_account(inv_email, password)

        # 关闭 ChatGPT 浏览器再注册
        chatgpt_api.stop()

        email = _complete_registration(inv_email, password, invite_link, mail_client)
        if email:
            completed.append(email)

    return completed


def create_account_direct(mail_client):
    """
    直接注册模式（域名已配置自动加入 workspace，不需要邀请）。
    流程：创建邮箱 → 注册 ChatGPT → 自动加入 workspace → Codex 登录
    """
    from autoteam.invite import register_with_invite, screenshot
    from playwright.sync_api import sync_playwright
    import uuid

    # Step 1: 创建临时邮箱
    account_id, email = mail_client.create_temp_email()
    password = f"Tmp_{uuid.uuid4().hex[:12]}!"

    # Step 2: 记录账号
    add_account(email, password, cloudmail_account_id=account_id)

    # Step 3: 直接去 ChatGPT 注册页面
    print(f"\n[直接注册] {email}")
    signup_url = "https://chatgpt.com/auth/login"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        page.goto(signup_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # 等待 Cloudflare
        for i in range(12):
            html = page.content()[:2000].lower()
            if "verify you are human" not in html and "challenge" not in page.url:
                break
            print(f"  等待 Cloudflare... ({i*5}s)")
            time.sleep(5)

        screenshot(page, "direct_01_login_page.png")

        # 点击注册
        try:
            signup_btn = page.locator('button:has-text("注册"), button:has-text("Sign up"), a:has-text("Sign up"), a:has-text("注册")').first
            if signup_btn.is_visible(timeout=5000):
                signup_btn.click()
                time.sleep(3)
        except Exception:
            pass

        screenshot(page, "direct_02_signup.png")

        # 输入邮箱
        print(f"  输入邮箱: {email}")
        email_input = page.locator('input[name="email"], input[type="email"]').first
        try:
            if email_input.is_visible(timeout=5000):
                email_input.fill(email)
                time.sleep(0.5)
                page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(3)
        except Exception:
            pass

        screenshot(page, "direct_03_after_email.png")

        # 输入密码
        pwd_input = page.locator('input[type="password"]').first
        try:
            if pwd_input.is_visible(timeout=5000):
                print(f"  设置密码: {password}")
                pwd_input.fill(password)
                time.sleep(0.5)
                page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(5)
        except Exception:
            pass

        screenshot(page, "direct_04_after_password.png")

        # 等待验证码
        code_input = None
        try:
            code_input = page.locator('input[name="code"], input[placeholder*="验证码"], input[placeholder*="code" i]').first
            if not code_input.is_visible(timeout=5000):
                code_input = None
        except Exception:
            code_input = None

        if code_input:
            import re
            print(f"  等待验证码...")
            verification_code = None
            start_t = time.time()
            while time.time() - start_t < MAIL_TIMEOUT:
                emails = mail_client.search_emails_by_recipient(email, size=10)
                for em in emails:
                    text = em.get("text", "") or em.get("content", "")
                    match = re.search(r'\b(\d{6})\b', text)
                    if match:
                        verification_code = match.group(1)
                        break
                if verification_code:
                    break
                elapsed = int(time.time() - start_t)
                print(f"\r  等待验证码... ({elapsed}s)", end="", flush=True)
                time.sleep(3)

            if verification_code:
                print(f"\n  输入验证码: {verification_code}")
                code_input.fill(verification_code)
                time.sleep(0.5)
                page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(8)
            else:
                print(f"\n  ❌ 未收到验证码")
                browser.close()
                return None

        screenshot(page, "direct_05_after_code.png")
        print(f"  当前 URL: {page.url}")

        # 填写个人信息（全名 + 年龄/生日）
        name_input = page.locator('input[name="name"]').first
        try:
            if name_input.is_visible(timeout=5000):
                name_input.fill("User")
                time.sleep(0.5)

                # 自适应年龄/生日
                spinbuttons = page.locator('[role="spinbutton"]').all()
                if len(spinbuttons) >= 3:
                    try:
                        page.locator('text=生日日期').click()
                        time.sleep(0.5)
                    except Exception:
                        pass
                    for sb, val in zip(spinbuttons[:3], ["1995", "06", "15"]):
                        sb.click(force=True)
                        time.sleep(0.2)
                        page.keyboard.type(val, delay=80)
                        time.sleep(0.3)
                    print("  填入生日: 1995/06/15")
                else:
                    age_input = page.locator('input[name="age"]').first
                    try:
                        if age_input.is_visible(timeout=3000):
                            age_input.fill("25")
                            print("  填入年龄: 25")
                    except Exception:
                        pass

                page.locator('button:has-text("完成帐户创建"), button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(8)
        except Exception:
            pass

        screenshot(page, "direct_06_after_profile.png")
        print(f"  当前 URL: {page.url}")

        # 可能需要选择 workspace 或自动加入
        try:
            join_btn = page.locator('button:has-text("Accept"), button:has-text("Join"), button:has-text("加入")').first
            if join_btn.is_visible(timeout=5000):
                join_btn.click()
                time.sleep(5)
        except Exception:
            pass

        screenshot(page, "direct_07_final.png")

        # 检查结果
        current_url = page.url
        success = "chatgpt.com" in current_url and "auth" not in current_url
        if success:
            print(f"\n  ✅ 注册成功并已加入 workspace!")
        else:
            print(f"\n  ⚠️  注册可能未完成，URL: {current_url}")

        browser.close()

    if not success:
        return None

    # Step 4: Codex 登录
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    if bundle:
        auth_file = save_auth_file(bundle)
        update_account(email, status=STATUS_ACTIVE, auth_file=auth_file, last_active_at=time.time())
        print(f"✅ 账号就绪: {email}")
        return email
    else:
        update_account(email, status=STATUS_ACTIVE)
        print(f"⚠️  账号已加入 Team 但 Codex 登录失败: {email}")
        return email


def create_new_account(chatgpt_api, mail_client):
    """
    创建新账号。优先用直接注册模式（域名自动加入 workspace）。
    chatgpt_api 可为 None（直接注册不需要）。
    """
    # 先检查 pending invites
    if chatgpt_api and chatgpt_api.browser:
        print("\n[创建] 先检查 pending invites...")
        completed = _check_pending_invites(chatgpt_api, mail_client)
        if completed:
            print(f"\n从 pending invites 完成了 {len(completed)} 个账号")
            return completed[0]

    # 直接注册模式（不需要邀请）
    print("\n[创建] 使用直接注册模式...")
    if chatgpt_api and chatgpt_api.browser:
        chatgpt_api.stop()
    return create_account_direct(mail_client)


def reinvite_account(chatgpt_api, mail_client, acc):
    """
    恢复 standby 账号 — 直接登录（域名自动加入 workspace，不需要邀请）。
    登录后自动回到 workspace，然后刷新 Codex token。
    """
    from autoteam.invite import screenshot
    from playwright.sync_api import sync_playwright

    email = acc["email"]
    password = acc.get("password", "")

    print(f"\n[轮转] 恢复旧账号: {email}（直接登录）")

    # 关闭 ChatGPT API 浏览器避免冲突
    if chatgpt_api and chatgpt_api.browser:
        chatgpt_api.stop()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        # 直接去登录页
        page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # Cloudflare
        for i in range(12):
            html = page.content()[:2000].lower()
            if "verify you are human" not in html and "challenge" not in page.url:
                break
            time.sleep(5)

        # 点登录
        try:
            login_btn = page.locator('button:has-text("登录"), button:has-text("Log in")').first
            if login_btn.is_visible(timeout=5000):
                login_btn.click()
                time.sleep(3)
        except Exception:
            pass

        # 输入邮箱
        email_input = page.locator('input[name="email"], input[type="email"]').first
        try:
            if email_input.is_visible(timeout=5000):
                email_input.fill(email)
                time.sleep(0.5)
                page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(3)
        except Exception:
            pass

        # 输入密码
        pwd_input = page.locator('input[type="password"]').first
        try:
            if pwd_input.is_visible(timeout=5000):
                pwd_input.fill(password)
                time.sleep(0.5)
                page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(8)
        except Exception:
            pass

        # 可能需要邮箱验证码
        code_input = None
        try:
            code_input = page.locator('input[name="code"], input[placeholder*="验证码"]').first
            if not code_input.is_visible(timeout=5000):
                code_input = None
        except Exception:
            code_input = None

        if code_input and mail_client:
            import re
            print("  等待登录验证码...")
            otp = None
            start_t = time.time()
            while time.time() - start_t < 120:
                emails = mail_client.search_emails_by_recipient(email, size=10)
                for em in emails:
                    subj = em.get("subject", "").lower()
                    if "invited" in subj:
                        continue
                    text = em.get("text", "") or em.get("content", "")
                    match = re.search(r'\b(\d{6})\b', text)
                    if match:
                        otp = match.group(1)
                        break
                if otp:
                    break
                time.sleep(3)
            if otp:
                print(f"  输入验证码: {otp}")
                code_input.fill(otp)
                time.sleep(0.5)
                page.locator('button:has-text("Continue"), button:has-text("继续"), button[type="submit"]').first.click()
                time.sleep(5)

        screenshot(page, "reinvite_final.png")
        print(f"  当前 URL: {page.url}")
        browser.close()

    # 更新状态
    update_account(email, status=STATUS_ACTIVE, last_active_at=time.time())

    # 刷新 Codex token
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    if bundle:
        auth_file = save_auth_file(bundle)
        update_account(email, auth_file=auth_file)
        print(f"✅ 旧账号已恢复: {email}")
    else:
        # 尝试用已有的 refresh_token
        auth_file = acc.get("auth_file")
        if auth_file and Path(auth_file).exists():
            auth_data = json.loads(Path(auth_file).read_text())
            rt = auth_data.get("refresh_token")
            if rt:
                new_tokens = refresh_access_token(rt)
                if new_tokens:
                    auth_data["access_token"] = new_tokens["access_token"]
                    auth_data["refresh_token"] = new_tokens.get("refresh_token", rt)
                    auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    Path(auth_file).write_text(json.dumps(auth_data, indent=2))
                    print(f"✅ 旧账号已恢复（token 已刷新）: {email}")
                    return True
        print(f"⚠️  旧账号已登录但 Codex token 刷新失败: {email}")

    return True


def cmd_rotate(target_seats=5):
    """
    智能轮转 - 保持 Team 始终有 target_seats 个可用成员，尽量少创建新账号。

    逻辑:
    1. 检查所有账号额度，更新状态
    2. 将额度用完的 active 账号移出 Team → standby
    3. 统计当前 Team 空缺数
    4. 优先从 standby 中选额度已恢复的旧账号填补
    5. 仅当所有旧账号都不可用时，才创建新账号
    """
    TARGET = target_seats

    # === Step 0: 同步账号状态 ===
    print("\n=== Step 0: 同步 Team 实际状态 ===")
    sync_account_states()

    # === Step 1: 检查所有账号额度 ===
    print("\n=== Step 1: 检查所有账号额度 ===")
    exhausted = cmd_check()

    chatgpt = None
    mail_client = None

    def ensure_chatgpt():
        nonlocal chatgpt
        if not chatgpt or not chatgpt.browser:
            chatgpt = ChatGPTTeamAPI()
            chatgpt.start()
        return chatgpt

    def ensure_mail():
        nonlocal mail_client
        if not mail_client:
            mail_client = CloudMailClient()
            mail_client.login()
        return mail_client

    try:
        # === Step 2: 移出所有额度用完的账号（包括之前已标记为 exhausted 的）===
        all_accounts = load_accounts()
        all_exhausted = [a for a in all_accounts if a["status"] == STATUS_EXHAUSTED]

        if all_exhausted:
            print(f"\n=== Step 2: 移出 {len(all_exhausted)} 个额度用完的账号 ===")
            ensure_chatgpt()
            for acc in all_exhausted:
                email = acc["email"]
                if not chatgpt.browser:
                    chatgpt.start()
                if remove_from_team(chatgpt, email):
                    update_account(email, status=STATUS_STANDBY)
                    print(f"  {email} → standby")
        else:
            print("\n=== Step 2: 无需移出账号 ===")

        # === Step 3: 检查 Team 空缺 ===
        print(f"\n=== Step 3: 检查 Team 成员数 ===")
        if not chatgpt or not chatgpt.browser:
            ensure_chatgpt()
        current_count = get_team_member_count(chatgpt)
        vacancies = TARGET - current_count
        print(f"  当前成员: {current_count}，目标: {TARGET}，空缺: {max(0, vacancies)}")

        if vacancies <= 0:
            print(f"\nTeam 已满（{current_count}/{TARGET}），无需补充")
            sync_to_cpa()
            cmd_status()
            return

        # === Step 4: 优先复用旧账号 ===
        print(f"\n=== Step 4: 尝试复用旧账号填补 {vacancies} 个空缺 ===")
        filled = 0
        standby_list = get_standby_accounts()
        recovered = [a for a in standby_list if a.get("_quota_recovered")]
        print(f"  待命账号: {len(standby_list)}，额度已恢复: {len(recovered)}")

        for acc in recovered:
            if filled >= vacancies:
                break
            email = acc["email"]
            print(f"\n  复用: {email}")
            if not chatgpt or not chatgpt.browser:
                ensure_chatgpt()
            reinvite_account(chatgpt, ensure_mail(), acc)
            filled += 1

        remaining = vacancies - filled
        if remaining <= 0:
            print(f"\n已用旧账号填满所有空缺")
            sync_to_cpa()
            cmd_status()
            return

        # === Step 5: 判断是否需要创建新账号 ===
        # 检查当前 active 账号中是否还有额度可用的
        active_accounts = get_active_accounts()
        exhausted_emails = {a["email"] for a in load_accounts() if a["status"] == STATUS_EXHAUSTED}
        active_with_quota = [a for a in active_accounts if a["email"] not in exhausted_emails]

        not_recovered = [a for a in standby_list if not a.get("_quota_recovered")]

        if not_recovered and active_with_quota:
            # 还有活跃可用的账号在 Team 中，且有旧号等恢复 → 暂不创建
            print(f"\n还有 {len(not_recovered)} 个待命账号额度未恢复:")
            for a in not_recovered:
                rt = a.get("quota_resets_at")
                if rt:
                    remaining_time = max(0, rt - time.time())
                    print(f"    {a['email']} - 约 {int(remaining_time/60)} 分钟后恢复")
                else:
                    print(f"    {a['email']} - 恢复时间未知")
            print(f"\nTeam 中仍有 {len(active_with_quota)} 个可用账号，暂不创建新账号")
            sync_to_cpa()
            cmd_status()
            return

        # 所有账号都不可用（active 全额度用完 + standby 全等恢复）→ 必须创建新号
        print(f"\n=== Step 5: 无可复用账号，创建 {remaining} 个新账号 ===")
        for i in range(remaining):
            print(f"\n--- 创建第 {i+1}/{remaining} 个新账号 ---")
            if not chatgpt or not chatgpt.browser:
                ensure_chatgpt()
            create_new_account(chatgpt, ensure_mail())

    finally:
        if chatgpt and chatgpt.browser:
            chatgpt.stop()

    print("\n=== 轮转完成 ===")
    sync_to_cpa()
    cmd_status()


def cmd_add():
    """手动添加一个新账号"""
    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()
    mail_client = CloudMailClient()
    mail_client.login()

    try:
        result = create_new_account(chatgpt, mail_client)  # 内部会 stop chatgpt
        if result:
            print(f"\n✅ 新账号添加成功: {result}")
            sync_to_cpa()
        else:
            print(f"\n❌ 添加失败")
    finally:
        if chatgpt.browser:
            chatgpt.stop()


def get_team_member_count(chatgpt_api):
    """获取当前 Team 成员数"""
    path = f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/users"
    result = chatgpt_api._api_fetch("GET", path)
    if result["status"] != 200:
        return -1
    data = json.loads(result["body"])
    members = data.get("items", data.get("users", data.get("members", [])))
    return len(members)


def cmd_fill(target=5):
    """检测 Team 成员数，不足 target 则自动添加新账号补满"""
    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()
    mail_client = CloudMailClient()
    mail_client.login()

    try:
        current = get_team_member_count(chatgpt)
        if current < 0:
            print("❌ 获取成员列表失败")
            return

        print(f"\n当前 Team 成员数: {current}，目标: {target}")

        need = target - current
        if need <= 0:
            print(f"成员数已满足（{current} >= {target}），无需添加")
            return

        print(f"需要添加 {need} 个账号\n")

        for i in range(need):
            print(f"\n{'='*50}")
            print(f"  添加第 {i+1}/{need} 个账号")
            print(f"{'='*50}")

            # 优先复用 standby 中额度已恢复的旧账号
            reusable = get_next_reusable_account()
            if reusable and reusable.get("_quota_recovered"):
                email = reusable["email"]
                print(f"复用旧账号: {email}")
                # 确保 chatgpt 浏览器可用
                if not chatgpt.browser:
                    chatgpt.start()
                reinvite_account(chatgpt, mail_client, reusable)
            else:
                # 创建新账号
                print("创建新账号...")
                if not chatgpt.browser:
                    chatgpt.start()
                create_new_account(chatgpt, mail_client)

            # 验证成员数
            if not chatgpt.browser:
                chatgpt.start()
            new_count = get_team_member_count(chatgpt)
            if new_count >= 0:
                print(f"\n当前成员数: {new_count}/{target}")

        print(f"\n{'='*50}")
        print(f"  填充完成")
        print(f"{'='*50}")
        sync_to_cpa()
        cmd_status()

    finally:
        if chatgpt.browser:
            chatgpt.stop()


def cmd_cleanup(max_seats=None):
    """清理多余的 Team 成员，只移除本地 accounts.json 中管理的账号"""
    accounts = load_accounts()
    local_emails = {a["email"].lower() for a in accounts}

    if not local_emails:
        print("本地无管理的账号，无需清理")
        return

    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()

    try:
        # 获取当前成员列表
        path = f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/users"
        result = chatgpt._api_fetch("GET", path)

        if result["status"] != 200:
            print(f"❌ 获取成员列表失败: {result['status']}")
            return

        data = json.loads(result["body"])
        members = data.get("items", data.get("users", data.get("members", [])))

        total = len(members)
        print(f"\n当前 Team 成员数: {total}")

        # 区分：本地管理的 vs 手动添加的
        local_members = []
        external_members = []
        for m in members:
            email = m.get("email", "").lower()
            if email in local_emails:
                local_members.append(m)
            else:
                external_members.append(m)

        print(f"  手动添加的成员: {len(external_members)}")
        for m in external_members:
            print(f"    {m.get('email'):<45} ({m.get('role')})")
        print(f"  本地管理的成员: {len(local_members)}")
        for m in local_members:
            print(f"    {m.get('email'):<45} ({m.get('role')})")

        # 确定要移除的数量
        if max_seats is None:
            max_seats = len(external_members) + 2  # 保留外部成员 + 2 个本地席位
        to_remove_count = total - max_seats
        if to_remove_count <= 0:
            print(f"\n成员数 {total} 未超过上限 {max_seats}，无需清理")
            return

        # 从本地管理的账号中选择要移除的（优先移除额度已用完的）
        removable = sorted(local_members, key=lambda m: (
            # 额度用完的优先移除
            0 if find_account(accounts, m.get("email", "")) and
                 find_account(accounts, m.get("email", "")).get("status") == STATUS_EXHAUSTED else 1,
            # 其次按创建时间，旧的优先
            find_account(accounts, m.get("email", "")).get("created_at", 0) if
                find_account(accounts, m.get("email", "")) else 0,
        ))

        to_remove = removable[:to_remove_count]
        print(f"\n需要移除 {len(to_remove)} 个本地账号:")
        for m in to_remove:
            print(f"  {m.get('email')}")

        # 执行移除
        for m in to_remove:
            email = m.get("email", "")
            user_id = m.get("user_id") or m.get("id")

            delete_path = f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/users/{user_id}"
            result = chatgpt._api_fetch("DELETE", delete_path)

            if result["status"] in (200, 204):
                print(f"  ✅ 已移除 {email}")
                update_account(email, status=STATUS_STANDBY)
            else:
                print(f"  ❌ 移除 {email} 失败: {result['status']}")

        # 取消 pending invites 中本地管理的
        inv_result = chatgpt._api_fetch("GET", f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/invites")
        if inv_result["status"] == 200:
            inv_data = json.loads(inv_result["body"])
            invites = inv_data if isinstance(inv_data, list) else inv_data.get("invites", inv_data.get("account_invites", []))
            for inv in invites:
                inv_email = inv.get("email_address", "").lower()
                inv_id = inv.get("id")
                if inv_email in local_emails and inv_id:
                    del_result = chatgpt._api_fetch("DELETE",
                        f"/backend-api/accounts/{CHATGPT_ACCOUNT_ID}/invites/{inv_id}")
                    if del_result["status"] in (200, 204):
                        print(f"  ✅ 已取消邀请 {inv_email}")

        print(f"\n清理完成")
        sync_to_cpa()

    finally:
        chatgpt.stop()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="manager.py",
        description="ChatGPT Team 账号轮转管理器",
    )
    sub = parser.add_subparsers(dest="command", help="可用命令")

    sub.add_parser("status", help="查看所有账号状态")
    sub.add_parser("check", help="检查活跃账号 Codex 额度")
    rotate_p = sub.add_parser("rotate", help="智能轮转（检查额度 → 移出 → 复用旧号 → 万不得已才创建新号）")
    rotate_p.add_argument("target", type=int, nargs="?", default=5, help="目标成员数（默认 5）")
    sub.add_parser("add", help="手动添加一个新账号")

    fill_p = sub.add_parser("fill", help="补满 Team 成员到指定数量")
    fill_p.add_argument("target", type=int, nargs="?", default=5, help="目标成员数（默认 5）")

    cleanup_p = sub.add_parser("cleanup", help="清理多余成员（只移除本地管理的）")
    cleanup_p.add_argument("max_seats", type=int, nargs="?", default=None, help="最大席位数")

    sub.add_parser("sync", help="手动同步认证文件到 CPA")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "status":
        cmd_status()
    elif args.command == "check":
        cmd_check()
    elif args.command == "rotate":
        cmd_rotate(args.target)
    elif args.command == "add":
        cmd_add()
    elif args.command == "fill":
        cmd_fill(args.target)
    elif args.command == "cleanup":
        cmd_cleanup(args.max_seats)
    elif args.command == "sync":
        sync_to_cpa()


if __name__ == "__main__":
    main()
