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

import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

from autoteam.account_ops import delete_managed_account, fetch_team_state
from autoteam.accounts import (
    STATUS_ACTIVE,
    STATUS_EXHAUSTED,
    STATUS_PENDING,
    STATUS_PERSONAL,
    STATUS_STANDBY,
    add_account,
    delete_account,
    find_account,
    get_standby_accounts,
    load_accounts,
    save_accounts,
    update_account,
)
from autoteam.admin_state import get_admin_email, get_admin_state_summary, get_chatgpt_account_id
from autoteam.chatgpt_api import ChatGPTTeamAPI
from autoteam.cloudmail import CloudMailClient
from autoteam.codex_auth import (
    MainCodexSyncFlow,
    _click_primary_auth_button,
    _is_google_redirect,
    check_codex_quota,
    get_quota_exhausted_info,
    get_saved_main_auth_file,
    login_codex_via_browser,
    quota_result_quota_info,
    quota_result_resets_at,
    refresh_access_token,
    refresh_main_auth_file,
    save_auth_file,
)
from autoteam.config import get_playwright_launch_options
from autoteam.cpa_sync import sync_from_cpa, sync_main_codex_to_cpa, sync_to_cpa
from autoteam.identity import random_age, random_birthday, random_full_name, random_password
from autoteam.register_failures import record_failure
from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)

MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))


def _normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_main_account_email(email: str | None) -> bool:
    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


_GOOGLE_AUTO_REUSE_DOMAINS = {"gmail.com", "googlemail.com"}


def _get_account_login_provider(acc: dict | None) -> str:
    acc = acc or {}
    for key in ("login_provider", "auth_provider", "oauth_provider"):
        provider = (acc.get(key) or "").strip().lower()
        if provider:
            return provider

    email = _normalized_email(acc.get("email"))
    if "@" in email and email.rsplit("@", 1)[-1] in _GOOGLE_AUTO_REUSE_DOMAINS:
        return "google"

    return ""


def _auto_reuse_skip_reason(acc: dict | None) -> str | None:
    provider = _get_account_login_provider(acc)
    if provider == "google":
        return "Google 登录账号暂不支持自动复用"
    return None


def sync_account_states(chatgpt_api=None):
    """根据 Team 实际成员列表同步本地账号状态"""
    account_id = get_chatgpt_account_id()
    if not account_id:
        return
    accounts = load_accounts()
    team_emails = set()

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
        path = f"/backend-api/accounts/{account_id}/users"
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
            if _is_main_account_email(email):
                continue
            if domain_suffix in email and email not in local_email_set:
                accounts.append(
                    {
                        "email": email,
                        "password": "",
                        "cloudmail_account_id": None,
                        "status": STATUS_ACTIVE,
                        "auth_file": None,
                        "quota_exhausted_at": None,
                        "quota_resets_at": None,
                        "created_at": time.time(),
                        "last_active_at": None,
                    }
                )
                changed = True
                logger.info("[同步] 发现 Team 中新成员: %s（已添加到本地）", email)

    # auths 目录中有认证文件但本地无记录的 → 自动添加为 standby
    from autoteam.codex_auth import AUTH_DIR

    local_email_set = {a["email"].lower() for a in accounts}  # 刷新一下
    if AUTH_DIR.exists():
        for auth_file in AUTH_DIR.glob("codex-*.json"):
            try:
                auth_data = json.loads(read_text(auth_file))
                email = auth_data.get("email", "").lower()
                if not email or email in local_email_set or _is_main_account_email(email):
                    continue
                # 判断是否在 Team 中
                in_team = email in team_emails
                status = STATUS_ACTIVE if in_team else STATUS_STANDBY
                accounts.append(
                    {
                        "email": email,
                        "password": "",
                        "cloudmail_account_id": None,
                        "status": status,
                        "auth_file": str(auth_file),
                        "quota_exhausted_at": None,
                        "quota_resets_at": None,
                        "created_at": time.time(),
                        "last_active_at": None,
                    }
                )
                local_email_set.add(email)
                changed = True
                logger.info("[同步] 从 auths 目录恢复账号: %s（%s）", email, status)
            except Exception:
                continue

    if changed:
        save_accounts(accounts)


def _print_status_table(accounts, quota_cache=None):
    """打印账号状态表格（使用 rich）"""
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    if quota_cache is None:
        quota_cache = {}

    console = Console(width=120)

    table = Table(
        title="AutoTeam 账号状态",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        title_style="bold white",
        padding=(0, 1),
        expand=True,
    )

    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("邮箱", style="white", no_wrap=True)
    table.add_column("状态", justify="center", width=10)
    table.add_column("5h 剩余", justify="right", width=8)
    table.add_column("周 剩余", justify="right", width=8)
    table.add_column("5h 重置", justify="center", width=12)
    table.add_column("周 重置", justify="center", width=12)

    STATUS_STYLE = {
        STATUS_ACTIVE: ("bold green", "● active"),
        STATUS_EXHAUSTED: ("bold red", "✗ used up"),
        STATUS_STANDBY: ("yellow", "○ standby"),
        STATUS_PENDING: ("dim", "… pending"),
    }

    for idx, acc in enumerate(accounts, 1):
        email = acc["email"]
        qi = quota_cache.get(email) or acc.get("last_quota")
        status = acc["status"]

        style, status_label = STATUS_STYLE.get(status, ("dim", status))
        status_text = Text(status_label, style=style)

        if qi:
            p_val = 100 - qi.get("primary_pct", 0)
            w_val = 100 - qi.get("weekly_pct", 0)
            p_pct = Text(f"{p_val}%", style="green" if p_val > 30 else "yellow" if p_val > 0 else "red")
            w_pct = Text(f"{w_val}%", style="green" if w_val > 30 else "yellow" if w_val > 0 else "red")
            p_reset = (
                time.strftime("%m-%d %H:%M", time.localtime(qi["primary_resets_at"]))
                if qi.get("primary_resets_at")
                else "-"
            )
            w_reset = (
                time.strftime("%m-%d %H:%M", time.localtime(qi["weekly_resets_at"]))
                if qi.get("weekly_resets_at")
                else "-"
            )
        else:
            p_pct = Text("-", style="dim")
            w_pct = Text("-", style="dim")
            p_reset = "-"
            w_reset = "-"

        table.add_row(
            str(idx),
            email,
            status_text,
            p_pct,
            w_pct,
            Text(p_reset, style="dim"),
            Text(w_reset, style="dim"),
        )

    console.print()
    console.print(table)

    # 统计摘要
    active = sum(1 for a in accounts if a["status"] == STATUS_ACTIVE)
    standby = sum(1 for a in accounts if a["status"] == STATUS_STANDBY)
    exhausted = sum(1 for a in accounts if a["status"] == STATUS_EXHAUSTED)
    console.print(
        f"  [green]● 活跃 {active}[/]  "
        f"[yellow]○ 待命 {standby}[/]  "
        f"[red]✗ 用完 {exhausted}[/]  "
        f"[dim]总计 {len(accounts)}[/]",
    )


def cmd_status():
    """显示所有账号状态（先同步 Team 实际状态，active 账号实时查询额度）"""
    logger.info("[状态] 同步 Team 实际状态...")
    sync_account_states()

    accounts = load_accounts()
    if not accounts:
        logger.info("[状态] 暂无账号")
        return

    # active 账号实时查询额度
    quota_cache = {}
    active_count = sum(
        1 for a in accounts if a["status"] == STATUS_ACTIVE and a.get("auth_file") and Path(a["auth_file"]).exists()
    )
    if active_count:
        logger.info("[状态] 查询 %d 个 active 账号额度...", active_count)
    for acc in accounts:
        if acc["status"] == STATUS_ACTIVE and acc.get("auth_file") and Path(acc["auth_file"]).exists():
            auth_data = json.loads(read_text(Path(acc["auth_file"])))
            access_token = auth_data.get("access_token")
            if access_token:
                status, info = check_codex_quota(access_token)
                if status == "ok" and isinstance(info, dict):
                    quota_cache[acc["email"]] = info
                elif status == "exhausted":
                    quota_info = quota_result_quota_info(info)
                    if quota_info:
                        quota_cache[acc["email"]] = quota_info

    _print_status_table(accounts, quota_cache)


def _check_and_refresh(acc):
    """检查单个账号额度，401 时自动刷新 token。返回 (status_str, info)
    info: exhausted 时为 exhausted_info，ok 时为 quota_info dict
    """
    email = acc["email"]
    auth_file = acc.get("auth_file")

    if not auth_file or not Path(auth_file).exists():
        return "no_auth", None

    auth_data = json.loads(read_text(Path(auth_file)))
    access_token = auth_data.get("access_token")
    rt = auth_data.get("refresh_token")

    if not access_token:
        return "no_auth", None

    status, info = check_codex_quota(access_token)

    # token 过期，尝试刷新
    if status == "auth_error" and rt:
        logger.info("[%s] token 过期，尝试刷新...", email)
        new_tokens = refresh_access_token(rt)
        if new_tokens:
            auth_data["access_token"] = new_tokens["access_token"]
            auth_data["refresh_token"] = new_tokens.get("refresh_token", rt)
            auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_text(Path(auth_file), json.dumps(auth_data, indent=2))
            logger.info("[%s] token 已刷新，重新检查额度...", email)
            status, info = check_codex_quota(new_tokens["access_token"])
        else:
            logger.error("[%s] token 刷新失败", email)

    return status, info


def cmd_check():
    """只检查 active 账号的额度，无认证文件或 auth_error 的自动重新登录 Codex"""
    from autoteam.config import AUTO_CHECK_THRESHOLD, CLOUDMAIL_DOMAIN

    # API 运行时配置优先（前端可修改）
    try:
        from autoteam.api import _auto_check_config

        threshold = _auto_check_config.get("threshold", AUTO_CHECK_THRESHOLD)
    except ImportError:
        threshold = AUTO_CHECK_THRESHOLD

    accounts = load_accounts()

    pending_accounts = [a for a in accounts if a["status"] == STATUS_PENDING]
    if pending_accounts:
        logger.info("[检查] 对账 %d 个 pending 账号...", len(pending_accounts))
        chatgpt = None
        mail_client = None
        deleted_pending = 0
        try:
            chatgpt = ChatGPTTeamAPI()
            chatgpt.start()
            members, invites = fetch_team_state(chatgpt)
            team_emails = {(m.get("email", "") or "").lower() for m in members}
            invite_emails = {(inv.get("email_address") or inv.get("email") or "").lower() for inv in invites}

            for acc in pending_accounts:
                email = acc["email"]
                email_l = email.lower()

                if email_l in team_emails:
                    logger.info("[检查] pending 账号已在 Team 中，转为 active: %s", email)
                    update_account(email, status=STATUS_ACTIVE)
                    continue

                if email_l in invite_emails:
                    logger.info("[检查] pending 账号仍存在远端邀请，保留: %s", email)
                    continue

                logger.warning("[检查] pending 账号为失败孤儿，删除: %s", email)
                if mail_client is None:
                    mail_client = CloudMailClient()
                    mail_client.login()
                delete_managed_account(
                    email,
                    remove_remote=True,
                    remove_cloudmail=True,
                    sync_cpa_after=False,
                    chatgpt_api=chatgpt,
                    mail_client=mail_client,
                    remote_state=(members, invites),
                )
                deleted_pending += 1
        except Exception as exc:
            logger.warning("[检查] pending 对账失败，跳过本轮清理: %s", exc)
        finally:
            if chatgpt and chatgpt.browser:
                chatgpt.stop()

        if deleted_pending:
            logger.info("[检查] 已删除 %d 个失败 pending 账号", deleted_pending)
            sync_to_cpa()

        accounts = load_accounts()

    all_active = [a for a in accounts if a["status"] == STATUS_ACTIVE and not _is_main_account_email(a.get("email"))]

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
        logger.info("[检查] 没有可检查的 active 账号")
        return []

    # 检查有认证文件的账号额度
    exhausted_list = []
    auth_error_list = []

    if active_with_auth:
        logger.info("[检查] 检查 %d 个 active 账号的额度...", len(active_with_auth))
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
                    # 保存最新额度快照，供 status 离线展示
                    update_account(email, last_quota=info)
                    # 低于阈值视为用完
                    if p_remain < threshold:
                        resets_at = p_reset or (time.time() + 18000)
                        logger.warning(
                            "[%s] 5h剩余 %d%% < %d%%，标记为 exhausted (重置 %s)", email, p_remain, threshold, p_time
                        )
                        update_account(
                            email,
                            status=STATUS_EXHAUSTED,
                            quota_exhausted_at=time.time(),
                            quota_resets_at=resets_at,
                        )
                        exhausted_list.append(acc)
                    else:
                        logger.info(
                            "[%s] 额度可用 - 5h剩余: %d%% (重置 %s) | 周剩余: %d%% (重置 %s)",
                            email,
                            p_remain,
                            p_time,
                            w_remain,
                            w_time,
                        )
                else:
                    logger.info("[%s] 额度可用", email)
            elif status_str == "exhausted":
                quota_info = quota_result_quota_info(info) or {}
                resets_at = quota_result_resets_at(info) or int(time.time() + 18000)
                if quota_info:
                    update_account(email, last_quota=quota_info)
                    p_remain = max(0, 100 - quota_info.get("primary_pct", 0))
                    w_remain = max(0, 100 - quota_info.get("weekly_pct", 0))
                    window = info.get("window") if isinstance(info, dict) else ""
                    logger.warning(
                        "[%s] %s额度已用完 - 5h剩余: %d%% | 周剩余: %d%%",
                        email,
                        "周" if window == "weekly" else "5h和周" if window == "combined" else "5h",
                        p_remain,
                        w_remain,
                    )
                else:
                    logger.warning("[%s] 额度已用完", email)
                update_account(
                    email,
                    status=STATUS_EXHAUSTED,
                    quota_exhausted_at=time.time(),
                    quota_resets_at=resets_at,
                )
                exhausted_list.append(acc)
            elif status_str == "auth_error":
                # token 失效，先看历史额度（重置时间已过的不算）
                lq = acc.get("last_quota")
                if lq:
                    exhausted_info = _pending_historical_exhausted_info(lq)
                    if exhausted_info:
                        resets_at = quota_result_resets_at(exhausted_info) or int(time.time() + 18000)
                        window_label = _quota_window_label(exhausted_info.get("window"))
                        logger.warning("[%s] token 失效，但历史%s额度未恢复，直接标记 exhausted", email, window_label)
                        update_account(
                            email,
                            status=STATUS_EXHAUSTED,
                            quota_exhausted_at=time.time(),
                            quota_resets_at=resets_at,
                        )
                        exhausted_list.append(acc)
                        continue
                    p_resets = lq.get("primary_resets_at", 0)
                    if not (p_resets and time.time() >= p_resets):
                        # 重置时间未过，历史数据有效
                        p_remain = 100 - lq.get("primary_pct", 0)
                        if p_remain < threshold:
                            resets_at = p_resets or (time.time() + 18000)
                            logger.warning(
                                "[%s] token 失效，历史额度 %d%% < %d%%，直接标记 exhausted", email, p_remain, threshold
                            )
                            update_account(
                                email,
                                status=STATUS_EXHAUSTED,
                                quota_exhausted_at=time.time(),
                                quota_resets_at=resets_at,
                            )
                            exhausted_list.append(acc)
                            continue
                    else:
                        logger.info("[%s] token 失效但 5h 重置时间已过，需重新登录验证", email)
                logger.warning("[%s] 认证失败，需要重新登录 Codex", email)
                auth_error_list.append(acc)

    # 无认证文件的 active 账号也需要重新登录
    if no_auth_list:
        logger.info("[检查] 发现 %d 个 active 账号无认证文件，需要登录 Codex:", len(no_auth_list))
        for a in no_auth_list:
            logger.info("[检查]   %s", a["email"])
        auth_error_list.extend(no_auth_list)

    # auth_error + 无认证文件的统一重新登录 Codex
    if auth_error_list:
        logger.info("[检查] 重新登录 %d 个 token 失效的账号...", len(auth_error_list))
        mail_client = CloudMailClient()
        mail_client.login()
        for acc in auth_error_list:
            email = acc["email"]
            password = acc.get("password", "")
            logger.info("[%s] 重新 Codex 登录...", email)
            bundle = login_codex_via_browser(email, password, mail_client=mail_client)
            if bundle:
                auth_file = save_auth_file(bundle)
                update_account(email, auth_file=auth_file)
                logger.info("[%s] token 已更新", email)
                # 重新检查额度
                status_str, info = _check_and_refresh(find_account(load_accounts(), email))
                if status_str == "exhausted":
                    quota_info = quota_result_quota_info(info)
                    if quota_info:
                        update_account(email, last_quota=quota_info)
                    update_account(
                        email,
                        status=STATUS_EXHAUSTED,
                        quota_exhausted_at=time.time(),
                        quota_resets_at=quota_result_resets_at(info) or int(time.time() + 18000),
                    )
                    exhausted_list.append(acc)
                    logger.warning("[%s] 额度已用完", email)
                elif status_str == "ok" and isinstance(info, dict):
                    p_remain = 100 - info.get("primary_pct", 0)
                    update_account(email, last_quota=info)
                    if p_remain < threshold:
                        resets_at = info.get("primary_resets_at") or (time.time() + 18000)
                        logger.warning("[%s] 5h剩余 %d%% < %d%%，标记为 exhausted", email, p_remain, threshold)
                        update_account(
                            email,
                            status=STATUS_EXHAUSTED,
                            quota_exhausted_at=time.time(),
                            quota_resets_at=resets_at,
                        )
                        exhausted_list.append(acc)
                    else:
                        logger.info("[%s] 额度可用 (%d%%)", email, p_remain)
                elif status_str == "ok":
                    logger.info("[%s] 额度可用", email)
                elif status_str == "auth_error":
                    logger.warning("[%s] 重新登录后仍无法查询额度（可能未选中 Team workspace），标记为 standby", email)
                    update_account(email, status=STATUS_STANDBY)
            else:
                logger.error("[%s] Codex 登录失败，标记为 standby", email)
                update_account(email, status=STATUS_STANDBY)

    return exhausted_list


def remove_from_team(chatgpt_api, email, *, return_status=False):
    """将账号从 Team 中移除"""
    if _is_main_account_email(email):
        logger.warning("[Team] 跳过移除主号: %s", email)
        return "failed" if return_status else False

    account_id = get_chatgpt_account_id()
    # 先获取成员列表找到 user_id
    path = f"/backend-api/accounts/{account_id}/users"
    result = chatgpt_api._api_fetch("GET", path)

    if result["status"] != 200:
        logger.error("[Team] 获取成员列表失败: %d", result["status"])
        return "failed" if return_status else False

    try:
        data = json.loads(result["body"])
        members = data.get("items", data.get("users", data.get("members", [])))
    except Exception:
        logger.error("[Team] 解析成员列表失败")
        return "failed" if return_status else False

    # 找到对应邮箱的成员
    target_user_id = None
    for member in members:
        member_email = member.get("email", "")
        if member_email.lower() == email.lower():
            target_user_id = member.get("user_id") or member.get("id")
            break

    if not target_user_id:
        logger.info("[Team] 未在成员列表中找到 %s（可能已移出）", email)
        # 可能已经不在 team 了
        return "already_absent" if return_status else True

    # 删除成员
    delete_path = f"/backend-api/accounts/{account_id}/users/{target_user_id}"
    result = chatgpt_api._api_fetch("DELETE", delete_path)

    if result["status"] in (200, 204):
        logger.info("[Team] 已将 %s 移出 Team", email)
        return "removed" if return_status else True
    else:
        logger.error("[Team] 移除 %s 失败: %d %s", email, result["status"], result["body"][:200])
        return "failed" if return_status else False


def invite_to_team(chatgpt_api, email, seat_type="default"):
    """邀请账号加入 Team。旧账号用 default，新账号用 usage_based。"""
    status, data = chatgpt_api.invite_member(email, seat_type=seat_type)
    if status == 200 and isinstance(data, dict):
        errored = data.get("errored_emails", [])
        if errored:
            err_msg = errored[0].get("error", "unknown")
            logger.warning("[Team] 邀请 %s 被拒绝: %s", email, err_msg)
            # default 失败则尝试 usage_based
            if seat_type == "default":
                logger.info("[Team] 尝试 usage_based 方式...")
                return invite_to_team(chatgpt_api, email, seat_type="usage_based")
            return False
    return status == 200


def _run_post_register_oauth(email, password, mail_client, leave_workspace=False, out_outcome=None):
    """
    注册（加入 Team）成功后统一的收尾流程：
    - leave_workspace=False: 直接跑 Team 模式 Codex OAuth，状态置为 ACTIVE
    - leave_workspace=True: 主号 API 踢出子账号 → 走 personal 模式 OAuth → 保存 free plan 认证，状态置为 PERSONAL

    返回 email 表示账号已入账号池；None 表示流程失败。
    out_outcome: 可选 dict，函数内会写入 `{status, email, reason, ...}` 供上游统计/汇总。
    """

    def _record_outcome(status, **extra):
        if out_outcome is not None:
            out_outcome.clear()
            out_outcome.update(status=status, email=email, **extra)

    if leave_workspace:
        # 退出 Team 必须用主号权限，临时起一个 ChatGPTTeamAPI 实例完成 DELETE
        logger.info("[注册] leave_workspace=True，先将 %s 从 Team 中移出...", email)
        temp_api = ChatGPTTeamAPI()
        remove_status = "failed"  # 防御：start() 抛异常时 finally 走完仍有确定值，避免 NameError
        try:
            temp_api.start()
            remove_status = remove_from_team(temp_api, email, return_status=True)
        except Exception as exc:
            logger.error("[注册] 启动主号 API 或移出 Team 时出错: %s", exc)
        finally:
            temp_api.stop()

        if remove_status not in ("removed", "already_absent"):
            logger.error("[注册] 无法将 %s 移出 Team（status=%s），放弃 personal OAuth", email, remove_status)
            # 没能踢出 → 账号还在 Team 里，保留为 standby 由下次轮转接手
            update_account(email, status=STATUS_STANDBY)
            record_failure(email, "kick_failed", f"remove_from_team status={remove_status}")
            _record_outcome("kick_failed", reason=f"主号踢出失败 status={remove_status}")
            return None

        bundle = login_codex_via_browser(email, password, mail_client=mail_client, use_personal=True)
        if bundle:
            auth_file = save_auth_file(bundle)
            update_account(
                email,
                status=STATUS_PERSONAL,
                auth_file=auth_file,
                last_active_at=time.time(),
            )
            logger.info("[注册] 免费号就绪: %s (plan=%s)", email, bundle.get("plan_type"))
            _record_outcome("success", plan=bundle.get("plan_type"))
            return email

        # personal OAuth 失败 — 不留僵尸 PERSONAL 记录：直接从 accounts.json 删除，失败明细写 register_failures.json
        # 用户能在失败日志里看到发生了什么（哪个 email / 是什么阶段 / 什么时候），账号列表保持干净
        logger.error(
            "[注册] %s 已退出 Team 但 personal Codex OAuth 未返回认证 bundle，从账号池删除",
            email,
        )
        delete_account(email)
        record_failure(
            email,
            "oauth_failed",
            "已退出 Team 但 personal Codex OAuth 登录未返回 bundle",
            stage="post_leave_workspace",
        )
        _record_outcome("oauth_failed", reason="personal Codex OAuth 未返回 bundle")
        return None

    # 原有 Team 流程
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    if bundle:
        auth_file = save_auth_file(bundle)
        update_account(email, status=STATUS_ACTIVE, auth_file=auth_file, last_active_at=time.time())
        logger.info("[注册] 账号就绪: %s", email)
        _record_outcome("success", plan=bundle.get("plan_type"))
        return email
    # 部分成功：账号已入 Team(席位被占用)但 auth_file 缺失,需要用户手动"补登录"。
    # 上游 cmd_fill 依 `if email: produced+=1` 按席位计数,所以这里仍返回 email;
    # outcome 打 team_auth_missing 让汇总能显示"这批里有 X 个需要补登录"。
    update_account(email, status=STATUS_ACTIVE)
    logger.warning("[注册] 账号已加入 Team 但 Codex 登录失败,需要补登录: %s", email)
    _record_outcome("team_auth_missing", reason="已入 Team 席位但 Codex OAuth 未返回 bundle,需要补登录")
    return email


def _complete_registration(email, password, invite_link, mail_client, *, leave_workspace=False, out_outcome=None):
    """完成注册 + Codex 登录（从已有邀请链接继续）。out_outcome 透传给 _run_post_register_oauth。"""
    from playwright.sync_api import sync_playwright

    from autoteam.invite import register_with_invite

    logger.info("[注册] 开始注册 %s...", email)
    with sync_playwright() as p:
        browser = p.chromium.launch(**get_playwright_launch_options())
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        result, password = register_with_invite(page, invite_link, email, mail_client, password=password)
        browser.close()

    if not result:
        logger.error("[注册] 注册 %s 失败", email)
        if out_outcome is not None:
            out_outcome["status"] = "register_failed"
            out_outcome["reason"] = "invite 注册链路失败（register_with_invite 返回 False）"
            out_outcome["last_email"] = email
        return None

    return _run_post_register_oauth(
        email, password, mail_client, leave_workspace=leave_workspace, out_outcome=out_outcome
    )


def _check_pending_invites(chatgpt_api, mail_client, *, leave_workspace=False, out_outcome=None):
    """
    检查 pending invites 中是否有已收到邮件的邀请，有则继续完成注册。
    leave_workspace: 注册成功后是否自动退出 Team 走 personal OAuth。
    out_outcome:     透传给 _complete_registration / _run_post_register_oauth，
                     让上游（_cmd_fill_personal）能拿到 kick_failed / oauth_failed 的分类。
    返回成功完成的邮箱列表。
    """
    account_id = get_chatgpt_account_id()
    result = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/invites")
    if result["status"] != 200:
        return []

    inv_data = json.loads(result["body"])
    invites = inv_data if isinstance(inv_data, list) else inv_data.get("invites", inv_data.get("account_invites", []))

    if not invites:
        return []

    logger.info("[Pending] 发现 %d 个待处理邀请", len(invites))
    completed = []

    for inv in invites:
        inv_email = inv.get("email_address", "")
        logger.info("[Pending] 检查 %s 是否已收到邮件...", inv_email)

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
            logger.info("[Pending] %s 未收到邮件，跳过", inv_email)
            continue

        logger.info("[Pending] %s 已收到邀请邮件，继续注册流程...", inv_email)

        # 确保本地有账号记录
        acc = find_account(load_accounts(), inv_email)
        if acc:
            password = acc.get("password") or random_password()
        else:
            password = random_password()
            add_account(inv_email, password)

        # 关闭 ChatGPT 浏览器再注册
        chatgpt_api.stop()

        email = _complete_registration(
            inv_email,
            password,
            invite_link,
            mail_client,
            leave_workspace=leave_workspace,
            out_outcome=out_outcome,
        )
        if email:
            completed.append(email)

    return completed


def _is_email_in_team(email):
    """检查邮箱是否已实际进入 Team。"""
    chatgpt = None
    try:
        chatgpt = ChatGPTTeamAPI()
        chatgpt.start()
        members, _ = fetch_team_state(chatgpt)
        return any((m.get("email", "") or "").lower() == email.lower() for m in members)
    except Exception as exc:
        logger.warning("[直接注册] 检查 Team 成员失败: %s", exc)
        return False
    finally:
        if chatgpt and chatgpt.browser:
            chatgpt.stop()


_DIRECT_EMAIL_SELECTORS = (
    'input[name="email"], input[type="email"], input[id="email"], '
    'input[autocomplete="email"], input[autocomplete="username"], '
    'input[placeholder*="email" i], input[placeholder*="Email" i]'
)
_DIRECT_PASSWORD_SELECTORS = 'input[name="password"], input[type="password"]'
_DIRECT_CODE_SELECTORS = 'input[name="code"], input[placeholder*="验证码"], input[placeholder*="code" i]'


def _safe_invite_screenshot(page, name):
    from autoteam.invite import screenshot

    try:
        screenshot(page, name)
    except Exception as exc:
        logger.debug("[直接注册] 截图失败 %s: %s", name, exc)


def _page_excerpt(page, limit=240):
    try:
        return page.locator("body").inner_text(timeout=1500)[:limit].replace("\n", " ")
    except Exception:
        return ""


def _quota_window_label(window: str | None) -> str:
    if window == "weekly":
        return "周"
    if window == "combined":
        return "5h和周"
    if window == "primary":
        return "5h"
    return "额度"


def _pending_historical_exhausted_info(quota_info, now=None):
    """仅当历史额度快照对应的耗尽窗口尚未重置时，才返回耗尽详情。"""
    exhausted_info = get_quota_exhausted_info(quota_info)
    if not exhausted_info:
        return None

    current_ts = time.time() if now is None else now
    resets_at = quota_result_resets_at(exhausted_info)
    if resets_at and current_ts >= resets_at:
        return None

    return exhausted_info


def _first_visible_editable_locator(page, selectors, timeout=800):
    try:
        locator = page.locator(selectors).first
        if not locator.is_visible(timeout=timeout):
            return None
        if locator.is_editable(timeout=timeout):
            return locator
    except Exception:
        return None
    return None


def _collect_date_spinbutton_meta(page):
    try:
        return page.evaluate(
            """() => {
                const byIdsText = (rawIds) => {
                    return (rawIds || '')
                        .split(/\\s+/)
                        .filter(Boolean)
                        .map(id => {
                            const el = document.getElementById(id);
                            return el ? (el.textContent || '').trim() : '';
                        })
                        .filter(Boolean)
                        .join(' ');
                };

                return Array.from(document.querySelectorAll('[role="spinbutton"]')).map((el, index) => ({
                    index,
                    text: (el.textContent || '').trim(),
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaValueText: el.getAttribute('aria-valuetext') || '',
                    ariaValueMin: el.getAttribute('aria-valuemin') || '',
                    ariaValueMax: el.getAttribute('aria-valuemax') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    dataType: el.getAttribute('data-type') || el.dataset?.type || '',
                    labelledText: byIdsText(el.getAttribute('aria-labelledby')),
                    describedText: byIdsText(el.getAttribute('aria-describedby')),
                }));
            }"""
        )
    except Exception:
        return []


def _infer_date_spinbutton_kind(meta):
    text_parts = [
        meta.get("text", ""),
        meta.get("ariaLabel", ""),
        meta.get("ariaValueText", ""),
        meta.get("placeholder", ""),
        meta.get("dataType", ""),
        meta.get("labelledText", ""),
        meta.get("describedText", ""),
    ]
    lowered = " ".join(part for part in text_parts if part).lower()

    def _to_int(value):
        try:
            return int(str(value).strip())
        except Exception:
            return None

    max_val = _to_int(meta.get("ariaValueMax"))

    if any(token in lowered for token in ("year", "yyyy", "yy", "年")):
        return "year"
    if any(token in lowered for token in ("month", "mm", "月")):
        return "month"
    if any(token in lowered for token in ("day", "dd", "日")):
        return "day"

    if max_val is not None:
        if max_val > 31:
            return "year"
        if max_val == 12:
            return "month"
        if max_val <= 31:
            return "day"

    return None


def _fill_about_you_birthday_by_meta(page, desired=None):
    metas = _collect_date_spinbutton_meta(page)
    if len(metas) < 3:
        return False

    if not desired:
        desired = random_birthday()
    kind_to_meta = {}

    for meta in metas:
        kind = _infer_date_spinbutton_kind(meta)
        if kind and kind not in kind_to_meta:
            kind_to_meta[kind] = meta

    if not all(kind in kind_to_meta for kind in desired):
        logger.info("[直接注册] 无法可靠识别生日字段顺序，降级为位置猜测")
        return False

    try:
        for kind in ("year", "month", "day"):
            meta = kind_to_meta[kind]
            sb = page.locator('[role="spinbutton"]').nth(meta["index"])
            sb.click(force=True)
            time.sleep(0.2)
            try:
                page.keyboard.press("ControlOrMeta+A")
                time.sleep(0.1)
            except Exception:
                pass
            page.keyboard.type(desired[kind], delay=80)
            time.sleep(0.3)

        logger.info(
            "[直接注册] 已按字段识别填入生日: year=%s month=%s day=%s | order=%s",
            desired["year"],
            desired["month"],
            desired["day"],
            {kind: kind_to_meta[kind]["index"] for kind in ("year", "month", "day")},
        )
        return True
    except Exception as exc:
        logger.warning("[直接注册] 按字段填写生日失败，降级为位置猜测: %s", exc)
        return False


def _detect_direct_register_step(page):
    url = (page.url or "").lower()
    if _is_google_redirect(page):
        return "google"

    if "email-verification" in url:
        return "code"
    if "about-you" in url:
        return "profile"
    if "create-account/password" in url or url.endswith("/password"):
        return "password"
    if "chatgpt.com" in url and "auth" not in url:
        return "completed"

    try:
        if _first_visible_editable_locator(page, _DIRECT_PASSWORD_SELECTORS, timeout=300):
            return "password"
    except Exception:
        pass

    try:
        if _first_visible_editable_locator(page, _DIRECT_CODE_SELECTORS, timeout=300):
            return "code"
    except Exception:
        pass

    try:
        if page.locator('input[name="name"], [role="spinbutton"]').first.is_visible(timeout=300):
            return "profile"
    except Exception:
        pass

    try:
        if _first_visible_editable_locator(page, _DIRECT_EMAIL_SELECTORS, timeout=300):
            return "email"
    except Exception:
        pass

    if "log-in-or-create-account" in url or url.endswith("/auth/login"):
        return "email"
    if "create-account" in url or "password" in url:
        return "password"
    return "unknown"


def _wait_for_direct_register_step(page, allowed_steps, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = _detect_direct_register_step(page)
        if step in allowed_steps:
            return step
        time.sleep(0.5)
    return _detect_direct_register_step(page)


def _wait_for_direct_step_change(page, current_step, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = _detect_direct_register_step(page)
        if step != current_step:
            return step
        time.sleep(0.5)
    return _detect_direct_register_step(page)


def _complete_direct_about_you(page):
    """尽量完成 about-you 页面，兼容不同生日字段顺序。"""
    if "about-you" not in (page.url or "").lower():
        return True

    # 本账号整个注册周期内固定一份身份数据，避免多次点提交导致生日漂移
    identity_bday = random_birthday()
    identity_name = random_full_name()
    identity_age = random_age()

    # 字段顺序只尝试 3 种排列，但全部使用相同的随机生日值
    birthday_orders = [
        (identity_bday["year"], identity_bday["month"], identity_bday["day"]),
        (identity_bday["month"], identity_bday["day"], identity_bday["year"]),
        (identity_bday["day"], identity_bday["month"], identity_bday["year"]),
    ]

    for attempt, values in enumerate(birthday_orders, 1):
        if "about-you" not in (page.url or "").lower():
            return True

        try:
            name_input = page.locator('input[name="name"]').first
            if name_input.is_visible(timeout=2000):
                try:
                    if name_input.is_editable(timeout=500):
                        name_input.fill(identity_name)
                        logger.info("[直接注册] 填入姓名: %s", identity_name)
                        time.sleep(0.3)
                except Exception:
                    pass
        except Exception:
            name_input = None

        spinbuttons = []
        try:
            spinbuttons = page.locator('[role="spinbutton"]').all()
        except Exception:
            spinbuttons = []

        if len(spinbuttons) >= 3:
            filled = _fill_about_you_birthday_by_meta(page, desired=identity_bday)
            if not filled:
                for label_sel in ("text=生日日期", "text=Date of birth"):
                    try:
                        page.locator(label_sel).first.click(timeout=1000)
                        time.sleep(0.3)
                        break
                    except Exception:
                        continue

                try:
                    for sb, val in zip(spinbuttons[:3], values):
                        sb.click(force=True)
                        time.sleep(0.2)
                        try:
                            page.keyboard.press("ControlOrMeta+A")
                            time.sleep(0.1)
                        except Exception:
                            pass
                        page.keyboard.type(val, delay=80)
                        time.sleep(0.3)
                    logger.info("[直接注册] 尝试按位置填入生日（第 %d 次）: %s/%s/%s", attempt, *values)
                except Exception as exc:
                    logger.warning("[直接注册] 生日字段填写失败（第 %d 次）: %s", attempt, exc)
        else:
            try:
                age_input = page.locator(
                    'input[name="age"], input[placeholder*="年龄"], input[placeholder*="Age"]'
                ).first
                if age_input.is_visible(timeout=2000) and age_input.is_editable(timeout=500):
                    age_input.fill(identity_age)
                    logger.info("[直接注册] 填入年龄: %s", identity_age)
            except Exception:
                pass

        submitted = False
        for btn_selector in (
            'button:has-text("完成帐户创建")',
            'button:has-text("Create account")',
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button[type="submit"]',
        ):
            try:
                btn = page.locator(btn_selector).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

        next_step = _wait_for_direct_register_step(
            page,
            {"profile", "completed", "code", "password", "email", "google"},
            timeout=12,
        )
        logger.info("[直接注册] 提交资料后状态: %s | URL: %s", next_step, page.url)

        # 提交 about-you 后最容易撞 add-phone：这里直接检测并 raise，让上层放弃账号
        from autoteam.invite import assert_not_blocked  # 局部导入避开循环

        assert_not_blocked(page, "about_you_submit")

        if next_step != "profile":
            return True

    logger.warning("[直接注册] about-you 页面仍未完成 | URL: %s | body=%s", page.url, _page_excerpt(page))
    return False


def _register_direct_once(mail_client, email, password, cloudmail_account_id=None):
    """执行一次直接注册，返回是否完成注册并进入 Team。

    在邮箱/密码/验证码/about-you 四个提交节点调用 assert_not_blocked，
    一旦命中 add-phone / duplicate 就抛 RegisterBlocked，由 create_account_direct 分流处理。
    """
    from playwright.sync_api import sync_playwright

    from autoteam.invite import RegisterBlocked, assert_not_blocked

    logger.info("[直接注册] %s", email)
    signup_url = "https://chatgpt.com/auth/login"

    with sync_playwright() as p:
        launch_kwargs = get_playwright_launch_options()
        if sys.platform.startswith("win"):
            launch_kwargs["slow_mo"] = 100
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        page.goto(signup_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        for i in range(12):
            html = page.content()[:2000].lower()
            if "verify you are human" not in html and "challenge" not in page.url:
                break
            logger.info("[直接注册] 等待 Cloudflare... (%ds)", i * 5)
            time.sleep(5)

        _safe_invite_screenshot(page, "direct_01_login_page.png")

        # OpenAI 首页有多种 A/B 测试变体，需要逐步找到邮箱输入框
        try:
            email_visible = page.locator(_DIRECT_EMAIL_SELECTORS).first.is_visible(timeout=3000)
            if not email_visible:
                # 尝试按优先级点击各种按钮来展开/跳转到邮箱输入
                for sel, desc in [
                    ('button:has-text("More options")', "More options"),
                    ('button:has-text("更多选项")', "更多选项"),
                    ('a:has-text("Sign up for free")', "Sign up for free"),
                    ('button:has-text("Sign up for free")', "Sign up for free"),
                    ('a:has-text("Sign up")', "Sign up"),
                    ('button:has-text("Sign up")', "Sign up"),
                    ('a:has-text("注册")', "注册"),
                    ('button:has-text("注册")', "注册"),
                    ('a:has-text("Log in")', "Log in"),
                    ('button:has-text("Log in")', "Log in"),
                ]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=1000):
                            logger.info("[直接注册] 点击: %s", desc)
                            btn.click()
                            time.sleep(2)
                            # 检查邮箱输入框是否出现了
                            step = _wait_for_direct_register_step(
                                page,
                                {"email", "password", "code", "profile", "completed", "google"},
                                timeout=10,
                            )
                            if step != "unknown":
                                break
                    except Exception:
                        continue
        except Exception:
            pass

        _safe_invite_screenshot(page, "direct_02_signup.png")

        logger.info("[直接注册] 输入邮箱: %s", email)
        email_step = _wait_for_direct_register_step(
            page,
            {"email", "password", "code", "profile", "completed", "google"},
            timeout=15,
        )
        logger.info("[直接注册] 邮箱步骤初始状态: %s | URL: %s", email_step, page.url)

        if email_step == "google":
            logger.warning("[直接注册] 邮箱步骤误跳转到 Google 登录页")
            browser.close()
            return False
        if email_step == "unknown":
            logger.warning("[直接注册] 未识别到邮箱步骤 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser.close()
            return False

        try:
            for attempt in range(3):
                step = _detect_direct_register_step(page)
                if step != "email":
                    break

                email_input = _first_visible_editable_locator(page, _DIRECT_EMAIL_SELECTORS, timeout=1500)
                if not email_input:
                    logger.info("[直接注册] 邮箱输入框不可编辑，等待页面继续跳转...")
                    next_step = _wait_for_direct_step_change(page, "email", timeout=10)
                    if next_step != "email":
                        break
                    logger.warning("[直接注册] 邮箱输入框仍不可编辑，继续重试 | URL: %s", page.url)
                    continue

                email_input.fill(email)
                time.sleep(0.5)
                logger.info("[直接注册] 邮箱已填入，点击 Continue... (attempt %d)", attempt + 1)
                _safe_invite_screenshot(page, f"direct_02b_email_filled_{attempt}.png")
                _click_primary_auth_button(page, email_input, ["Continue", "继续"])

                next_step = _wait_for_direct_step_change(page, "email", timeout=15)
                logger.info("[直接注册] 点击 Continue 后状态: %s | URL: %s", next_step, page.url)
                _safe_invite_screenshot(page, f"direct_02c_after_continue_{attempt}.png")

                if next_step == "google":
                    _safe_invite_screenshot(page, f"direct_03_google_redirect_attempt{attempt + 1}.png")
                    logger.warning("[直接注册] 邮箱步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                    page.go_back(wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    continue
                if next_step != "email":
                    break

                email_input = _first_visible_editable_locator(page, _DIRECT_EMAIL_SELECTORS, timeout=600)
                if not email_input:
                    logger.info("[直接注册] 邮箱框已只读/跳转中，额外等待页面推进...")
                    next_step = _wait_for_direct_step_change(page, "email", timeout=10)
                    logger.info("[直接注册] 额外等待后状态: %s | URL: %s", next_step, page.url)
                    if next_step != "email":
                        break

                logger.warning(
                    "[直接注册] 点击 Continue 后仍停留在邮箱步骤，准备重试... | URL: %s | body=%s",
                    page.url,
                    _page_excerpt(page),
                )
        except Exception as exc:
            logger.warning("[直接注册] 邮箱步骤异常: %s | URL: %s", exc, page.url)

        _safe_invite_screenshot(page, "direct_03_after_email.png")
        current_step = _detect_direct_register_step(page)
        logger.info("[直接注册] 邮箱步骤结束状态: %s | URL: %s", current_step, page.url)
        if current_step == "google":
            logger.warning("[直接注册] 邮箱步骤仍停留在 Google 登录页")
            browser.close()
            return False
        if current_step == "email":
            logger.warning("[直接注册] 邮箱步骤未推进 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser.close()
            return False

        try:
            assert_not_blocked(page, "email_submit")
        except RegisterBlocked:
            browser.close()
            raise

        # 等待页面跳转完成（可能跳到 create-account/password）
        password_step = _wait_for_direct_register_step(
            page,
            {"password", "code", "profile", "completed", "google", "email"},
            timeout=15,
        )
        logger.info("[直接注册] 密码页检测状态: %s | URL: %s", password_step, page.url)
        _safe_invite_screenshot(page, "direct_03b_before_password.png")

        try:
            for attempt in range(2):
                if _detect_direct_register_step(page) != "password":
                    logger.info("[直接注册] 未检测到密码输入框，跳过")
                    break

                pwd_input = _first_visible_editable_locator(page, _DIRECT_PASSWORD_SELECTORS, timeout=1500)
                if not pwd_input:
                    logger.info("[直接注册] 密码输入框不可编辑，等待页面继续跳转...")
                    next_step = _wait_for_direct_step_change(page, "password", timeout=10)
                    if next_step != "password":
                        break
                    logger.warning("[直接注册] 密码输入框仍不可编辑，继续重试 | URL: %s", page.url)
                    continue

                logger.info("[直接注册] 设置密码")
                pwd_input.fill(password)
                time.sleep(0.5)
                _click_primary_auth_button(page, pwd_input, ["Continue", "继续", "Log in"])
                next_step = _wait_for_direct_step_change(page, "password", timeout=15)
                logger.info("[直接注册] 提交密码后状态: %s | URL: %s", next_step, page.url)

                if next_step == "google":
                    _safe_invite_screenshot(page, f"direct_04_google_redirect_attempt{attempt + 1}.png")
                    logger.warning("[直接注册] 密码步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                    page.go_back(wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    continue
                if next_step != "password":
                    break

                pwd_input = _first_visible_editable_locator(page, _DIRECT_PASSWORD_SELECTORS, timeout=600)
                if not pwd_input:
                    logger.info("[直接注册] 密码框已只读/跳转中，额外等待页面推进...")
                    next_step = _wait_for_direct_step_change(page, "password", timeout=10)
                    logger.info("[直接注册] 额外等待后状态: %s | URL: %s", next_step, page.url)
                    if next_step != "password":
                        break
        except Exception as exc:
            logger.warning("[直接注册] 密码步骤异常: %s | URL: %s", exc, page.url)

        _safe_invite_screenshot(page, "direct_04_after_password.png")
        current_step = _detect_direct_register_step(page)
        if current_step == "google":
            logger.warning("[直接注册] 密码步骤仍停留在 Google 登录页")
            browser.close()
            return False
        if current_step == "email":
            logger.warning("[直接注册] 提交密码前流程回退到邮箱页 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser.close()
            return False

        try:
            assert_not_blocked(page, "password_submit")
        except RegisterBlocked:
            browser.close()
            raise

        code_input = None
        try:
            code_input = page.locator(_DIRECT_CODE_SELECTORS).first
            if not code_input.is_visible(timeout=5000):
                code_input = None
        except Exception:
            code_input = None

        if code_input:
            logger.info("[直接注册] 等待验证码...")
            verification_code = None
            start_t = time.time()
            while time.time() - start_t < MAIL_TIMEOUT:
                emails = mail_client.search_emails_by_recipient(email, size=10, account_id=cloudmail_account_id)
                for em in emails:
                    verification_code = mail_client.extract_verification_code(em)
                    if verification_code:
                        break
                if verification_code:
                    break
                elapsed = int(time.time() - start_t)
                print(f"\r  等待验证码... ({elapsed}s)", end="", flush=True)
                time.sleep(3)

            if verification_code:
                logger.info("[直接注册] 输入验证码: %s", verification_code)
                code_input.fill(verification_code)
                time.sleep(0.5)
                _click_primary_auth_button(page, code_input, ["Continue", "继续"])
                time.sleep(8)
            else:
                logger.error("[直接注册] 未收到验证码")
                browser.close()
                return False

        _safe_invite_screenshot(page, "direct_05_after_code.png")
        logger.info("[直接注册] 当前 URL: %s", page.url)

        try:
            assert_not_blocked(page, "code_submit")
        except RegisterBlocked:
            browser.close()
            raise

        try:
            _complete_direct_about_you(page)
        except RegisterBlocked:
            # add-phone / duplicate 必须穿透给 create_account_direct 处理
            browser.close()
            raise
        except Exception as exc:
            logger.warning("[直接注册] about-you 步骤异常: %s | URL: %s", exc, page.url)

        _safe_invite_screenshot(page, "direct_06_after_profile.png")
        logger.info("[直接注册] 当前 URL: %s", page.url)

        try:
            join_btn = page.locator('button:has-text("Accept"), button:has-text("Join"), button:has-text("加入")').first
            if join_btn.is_visible(timeout=5000):
                join_btn.click()
                time.sleep(5)
        except Exception:
            pass

        _safe_invite_screenshot(page, "direct_07_final.png")

        current_url = page.url
        success = "chatgpt.com" in current_url and "auth" not in current_url and not _is_google_redirect(page)
        if success:
            logger.info("[直接注册] 注册成功并已加入 workspace!")
        else:
            logger.warning("[直接注册] 注册可能未完成，URL: %s", current_url)

        browser.close()
        return success


def create_account_direct(mail_client, *, leave_workspace=False, out_outcome=None):
    """
    直接注册模式（域名已配置自动加入 workspace，不需要邀请）。
    流程：创建邮箱 → 注册 ChatGPT → 自动加入 workspace → Codex 登录
    leave_workspace: 加入 workspace 后是否立即退出，转为 personal 模式跑 OAuth。
    out_outcome:     可选 dict，函数会把最终结局（success/phone_blocked/duplicate_exhausted/register_failed/...）
                     + 统计信息（register_attempts / duplicate_swaps / last_email / reason）写入，供上游汇总。

    捕获 RegisterBlocked：
    - is_phone=True:     当前邮箱已暴露给 OpenAI，立即删邮箱、整个账号放弃（return None）
    - is_duplicate=True: 换个临时邮箱继续尝试，独立计数不消耗 register_attempts
    - 其他异常:          归入现有 retry 计数
    """
    from autoteam.invite import RegisterBlocked

    account_id, email = mail_client.create_temp_email()
    password = random_password()

    def _record_outcome(status, **extra):
        if out_outcome is not None:
            out_outcome.clear()
            out_outcome.update(
                status=status,
                last_email=email,
                register_attempts=register_attempts,
                duplicate_swaps=duplicate_swaps,
                **extra,
            )

    def _discard_email(reason):
        try:
            mail_client.delete_account(account_id)
        except Exception as exc:
            logger.warning("[直接注册] 删除 %s 的临时邮箱失败（%s）: %s", reason, email, exc)

    # 注册失败（非 duplicate）最多重试 3 次；duplicate 额外独立上限，防止 CloudMail 异常导致无限换邮箱
    success = False
    MAX_REGISTER_ATTEMPTS = 3
    MAX_DUPLICATE_SWAPS = 5
    register_attempts = 0
    duplicate_swaps = 0
    while register_attempts < MAX_REGISTER_ATTEMPTS:
        logger.info(
            "[直接注册] 开始注册尝试: %s（已试 %d/%d，duplicate 换邮箱 %d/%d）",
            email,
            register_attempts,
            MAX_REGISTER_ATTEMPTS,
            duplicate_swaps,
            MAX_DUPLICATE_SWAPS,
        )
        try:
            success = _register_direct_once(mail_client, email, password, cloudmail_account_id=account_id)
        except RegisterBlocked as blocked:
            logger.error("[直接注册] %s 被阻断: %s", email, blocked)
            if blocked.is_phone:
                # 用户明确要求：不绕 add-phone，直接放弃本账号
                _discard_email("phone_block")
                record_failure(
                    email,
                    "phone_blocked",
                    f"add-phone 手机验证（step={blocked.step}）",
                    step=blocked.step,
                    register_attempts=register_attempts,
                    duplicate_swaps=duplicate_swaps,
                )
                _record_outcome("phone_blocked", reason=f"add-phone 手机验证 step={blocked.step}", step=blocked.step)
                return None
            if blocked.is_duplicate:
                # 邮箱重复 → 换一个全新的临时邮箱再来，不计入 register_attempts
                duplicate_swaps += 1
                if duplicate_swaps > MAX_DUPLICATE_SWAPS:
                    logger.error("[直接注册] duplicate 换邮箱已达上限 %d，放弃", MAX_DUPLICATE_SWAPS)
                    _discard_email("duplicate_exhausted")
                    record_failure(
                        email,
                        "duplicate_exhausted",
                        f"duplicate 换邮箱已达上限 {MAX_DUPLICATE_SWAPS}",
                        duplicate_swaps=duplicate_swaps,
                    )
                    _record_outcome(
                        "duplicate_exhausted",
                        reason=f"duplicate 换邮箱 {duplicate_swaps} 次仍失败",
                    )
                    return None
                _discard_email("duplicate")
                account_id, email = mail_client.create_temp_email()
                password = random_password()
                logger.info("[直接注册] 已换新临时邮箱: %s", email)
                continue
            # 其他阻断按普通失败处理
            success = False
        except Exception as exc:
            # Playwright 崩溃 / 网络异常等:不清理邮箱会让 CloudMail 积压,必须补一刀 discard 再抛。
            logger.error(
                "[直接注册] %s 注册时发生未分类异常,discard 邮箱后向上抛: %s",
                email,
                exc,
            )
            _discard_email("exception")
            record_failure(
                email,
                "exception",
                f"_register_direct_once 抛非 RegisterBlocked 异常: {exc}",
                register_attempts=register_attempts,
                duplicate_swaps=duplicate_swaps,
            )
            _record_outcome("exception", reason=f"未分类异常: {exc}")
            raise

        # 只有真正走完 _register_direct_once 的一次（无论成功失败）才消耗 register_attempts
        register_attempts += 1

        if success:
            break

        if _is_email_in_team(email):
            logger.info("[直接注册] 远端确认账号已在 Team 中，视为注册成功: %s", email)
            success = True
            break

        if register_attempts < MAX_REGISTER_ATTEMPTS:
            logger.warning("[直接注册] 注册失败且账号不在 Team 中，60 秒后重试: %s", email)
            time.sleep(60)

    if not success:
        logger.error(
            "[直接注册] %s 多次注册失败（register_attempts=%d, duplicate_swaps=%d），删除临时账号",
            email,
            register_attempts,
            duplicate_swaps,
        )
        _discard_email("register_failed")
        record_failure(
            email,
            "register_failed",
            f"连续 {register_attempts} 次注册尝试均未进入 Team",
            register_attempts=register_attempts,
            duplicate_swaps=duplicate_swaps,
        )
        _record_outcome("register_failed", reason=f"注册 {register_attempts} 次均未进入 Team")
        return None

    add_account(email, password, cloudmail_account_id=account_id)

    return _run_post_register_oauth(
        email,
        password,
        mail_client,
        leave_workspace=leave_workspace,
        out_outcome=out_outcome,
    )


def create_new_account(chatgpt_api, mail_client, *, leave_workspace=False, out_outcome=None):
    """
    创建新账号。优先用直接注册模式（域名自动加入 workspace）。
    chatgpt_api 可为 None（直接注册不需要）。
    leave_workspace: 注册成功后是否退出 Team 走 personal OAuth。
    out_outcome:     透传给 create_account_direct 的可选统计容器。
    """
    # 先检查 pending invites
    if chatgpt_api and chatgpt_api.browser:
        logger.info("[创建] 先检查 pending invites...")
        completed = _check_pending_invites(
            chatgpt_api,
            mail_client,
            leave_workspace=leave_workspace,
            out_outcome=out_outcome,
        )
        if completed:
            logger.info("[创建] 从 pending invites 完成了 %d 个账号", len(completed))
            return completed[0]

    # 直接注册模式（不需要邀请）
    logger.info("[创建] 使用直接注册模式...")
    if chatgpt_api and chatgpt_api.browser:
        chatgpt_api.stop()
    return create_account_direct(mail_client, leave_workspace=leave_workspace, out_outcome=out_outcome)


def reinvite_account(chatgpt_api, mail_client, acc):
    """
    恢复 standby 账号 — 复用统一的 Codex OAuth 登录流程。
    只有拿到 team plan 的认证结果，才视为恢复成功。
    """
    email = acc["email"]
    password = acc.get("password", "")

    logger.info("[轮转] 恢复旧账号: %s（统一 OAuth 登录）", email)

    # 关闭 ChatGPT API 浏览器避免冲突
    if chatgpt_api and chatgpt_api.browser:
        chatgpt_api.stop()

    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    if not bundle:
        logger.warning("[轮转] 旧账号 OAuth 登录失败，保持 standby: %s", email)
        update_account(email, status=STATUS_STANDBY)
        return False

    plan_type = (bundle.get("plan_type") or "").lower()
    if plan_type != "team":
        logger.warning("[轮转] 旧账号登录后 plan=%s，不是 team，恢复失败: %s", plan_type or "unknown", email)
        update_account(email, status=STATUS_STANDBY)
        return False

    auth_file = save_auth_file(bundle)
    update_account(email, status=STATUS_ACTIVE, last_active_at=time.time(), auth_file=auth_file)
    logger.info("[轮转] 旧账号已恢复: %s", email)
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

    from autoteam.config import AUTO_CHECK_THRESHOLD

    try:
        from autoteam.api import _auto_check_config

        threshold = _auto_check_config.get("threshold", AUTO_CHECK_THRESHOLD)
    except ImportError:
        threshold = AUTO_CHECK_THRESHOLD

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

    logger.info("[1/5] 同步 Team 状态...")
    sync_account_states()

    logger.info("[2/5] 检查额度...")
    cmd_check()

    try:
        # 移出所有 exhausted 账号（包括之前已标记的）
        all_accounts = load_accounts()
        all_exhausted = [
            a for a in all_accounts if a["status"] == STATUS_EXHAUSTED and not _is_main_account_email(a.get("email"))
        ]
        initial_api_count = -1
        removed_now = 0
        already_absent_count = 0

        if all_exhausted:
            logger.info("[3/5] 移出 %d 个额度用完的账号...", len(all_exhausted))
            ensure_chatgpt()
            initial_api_count = get_team_member_count(chatgpt)
            for acc in all_exhausted:
                email = acc["email"]
                if not chatgpt.browser:
                    chatgpt.start()
                remove_status = remove_from_team(chatgpt, email, return_status=True)
                if remove_status in ("removed", "already_absent"):
                    update_account(email, status=STATUS_STANDBY)
                    if remove_status == "removed":
                        removed_now += 1
                        logger.info("[3/5] %s → standby（已从 Team 移出）", email)
                    else:
                        already_absent_count += 1
                        logger.info("[3/5] %s → standby（远端已不存在）", email)
        else:
            logger.info("[3/5] 无需移出账号")
        if not chatgpt or not chatgpt.browser:
            ensure_chatgpt()
        api_count = get_team_member_count(chatgpt)
        logger.info(
            "[4/5] API 返回成员数: %d（实际移出: %d，远端已缺席: %d）",
            api_count,
            removed_now,
            already_absent_count,
        )
        if api_count <= 0:
            # API 返回异常，用本地 active 账号数兜底
            local_active = sum(1 for a in load_accounts() if a["status"] == STATUS_ACTIVE)
            logger.warning("[4/5] API 成员数异常 (%d)，使用本地 active 数: %d", api_count, local_active)
            current_count = local_active
        else:
            # 保守估算当前成员数：
            # - api_count 是移除后的最新观察值
            # - initial_api_count - removed_now 是基于移除前人数的理论下界
            # 若远端成员本就不存在（already_absent），不能再从 api_count 里额外扣减，否则会少算人数。
            estimates = [api_count]
            if initial_api_count > 0 and removed_now > 0:
                estimates.append(max(0, initial_api_count - removed_now))
            current_count = min(estimates)
            if len(estimates) > 1 and current_count != api_count:
                logger.info(
                    "[4/5] 成员数保守估算: %d（初始=%d，移出=%d）", current_count, initial_api_count, removed_now
                )
        vacancies = TARGET - current_count

        if vacancies <= 0:
            excess = current_count - TARGET
            if excess > 0:
                logger.info("[4/5] Team 超员 (%d/%d)，清理 %d 个多余成员...", current_count, TARGET, excess)
                # 只移除本地管理的账号，优先移除额度最低的
                all_accs = load_accounts()
                local_active = [
                    a for a in all_accs if a["status"] == STATUS_ACTIVE and not _is_main_account_email(a.get("email"))
                ]
                # 按额度排序，额度低的优先移除
                local_active.sort(key=lambda a: 100 - (a.get("last_quota") or {}).get("primary_pct", 0))
                removed = 0
                for acc in local_active:
                    if removed >= excess:
                        break
                    email = acc["email"]
                    if remove_from_team(chatgpt, email):
                        update_account(email, status=STATUS_STANDBY)
                        logger.info("[4/5] 超员清理: %s → standby", email)
                        removed += 1
                if removed:
                    logger.info("[4/5] 已清理 %d 个多余成员", removed)
            else:
                logger.info("[4/5] Team 已满 (%d/%d)", current_count, TARGET)
            return

        logger.info("[4/5] 填补 %d 个空缺 (当前 %d/%d)...", vacancies, current_count, TARGET)

        # 优先复用旧账号（先验证额度是否真的恢复了）
        filled = 0
        standby_list = [a for a in get_standby_accounts() if not _is_main_account_email(a.get("email"))]
        quota_skipped = []
        auto_reuse_skipped = []

        from autoteam import cancel_signal

        for acc in standby_list:
            if cancel_signal.is_cancelled():
                logger.warning("[轮转] 收到取消请求,中止 standby 复用阶段")
                break
            if filled >= vacancies:
                break
            email = acc["email"]
            auth_file = acc.get("auth_file")

            skip_reason = _auto_reuse_skip_reason(acc)
            if skip_reason:
                logger.info("[4/5] 跳过 %s（%s）", email, skip_reason)
                auto_reuse_skipped.append(acc)
                continue

            # 验证额度是否真的恢复了
            quota_ok = False
            if auth_file and Path(auth_file).exists():
                try:
                    auth_data = json.loads(read_text(Path(auth_file)))
                    access_token = auth_data.get("access_token")
                    if access_token:
                        status_str, info = check_codex_quota(access_token)
                        if status_str == "exhausted":
                            quota_info = quota_result_quota_info(info)
                            if quota_info:
                                update_account(email, last_quota=quota_info)
                            logger.info("[4/5] 跳过 %s（额度未恢复）", email)
                            quota_skipped.append(acc)
                            continue
                        if status_str == "ok" and isinstance(info, dict):
                            p_remain = 100 - info.get("primary_pct", 0)
                            if p_remain < threshold:
                                logger.info("[4/5] 跳过 %s（剩余 %d%% < %d%%）", email, p_remain, threshold)
                                quota_skipped.append(acc)
                                continue
                            quota_ok = True
                        # auth_error: token 失效，用 last_quota 判断（但重置时间已过的不算）
                        if status_str == "auth_error":
                            lq = acc.get("last_quota")
                            if lq:
                                exhausted_info = _pending_historical_exhausted_info(lq)
                                if exhausted_info:
                                    window_label = _quota_window_label(exhausted_info.get("window"))
                                    logger.info("[4/5] 跳过 %s（%s额度未恢复）", email, window_label)
                                    quota_skipped.append(acc)
                                    continue
                                p_resets = lq.get("primary_resets_at", 0)
                                if p_resets and time.time() >= p_resets:
                                    logger.info("[4/5] %s 的 5h 重置时间已过，视为额度已恢复", email)
                                    quota_ok = True
                                else:
                                    p_remain = 100 - lq.get("primary_pct", 0)
                                    if p_remain < threshold:
                                        logger.info("[4/5] 跳过 %s（上次额度 %d%% < %d%%）", email, p_remain, threshold)
                                        quota_skipped.append(acc)
                                        continue
                                    quota_ok = True
                except Exception:
                    pass

            # 没有认证文件或无法查询额度时，用 last_quota / quota_resets_at 兜底
            if not quota_ok:
                lq = acc.get("last_quota")
                if lq:
                    exhausted_info = _pending_historical_exhausted_info(lq)
                    if exhausted_info:
                        window_label = _quota_window_label(exhausted_info.get("window"))
                        logger.info("[4/5] 跳过 %s（%s额度未恢复）", email, window_label)
                        quota_skipped.append(acc)
                        continue
                    p_resets = lq.get("primary_resets_at", 0)
                    if p_resets and time.time() >= p_resets:
                        # 重置时间已过，旧数据作废，视为额度已恢复
                        logger.info("[4/5] %s 的 5h 重置时间已过，视为额度已恢复", email)
                    else:
                        p_remain = 100 - lq.get("primary_pct", 0)
                        if p_remain < threshold:
                            logger.info("[4/5] 跳过 %s（历史额度 %d%% < %d%%）", email, p_remain, threshold)
                            quota_skipped.append(acc)
                            continue
                else:
                    # 没有 last_quota，看 quota_resets_at 是否已过
                    resets_at = acc.get("quota_resets_at")
                    if resets_at and time.time() < resets_at:
                        mins = max(0, int((resets_at - time.time()) / 60))
                        logger.info("[4/5] 跳过 %s（%d 分钟后恢复）", email, mins)
                        quota_skipped.append(acc)
                        continue

            logger.info("[4/5] 复用: %s", email)
            if not chatgpt or not chatgpt.browser:
                ensure_chatgpt()
            if reinvite_account(chatgpt, ensure_mail(), acc):
                filled += 1
                current_count += 1
            else:
                quota_skipped.append(acc)

        if quota_skipped:
            logger.info("[4/5] 跳过 %d 个额度未恢复或复用失败的旧号", len(quota_skipped))
        if auto_reuse_skipped:
            logger.info("[4/5] 跳过 %d 个暂不支持自动复用的旧号", len(auto_reuse_skipped))

        remaining = TARGET - current_count
        if remaining <= 0:
            logger.info("[4/5] 已用旧账号填满空缺")
        else:
            # 必须创建新号
            logger.info("[5/5] 创建 %d 个新账号...", remaining)
            for i in range(remaining):
                if cancel_signal.is_cancelled():
                    logger.warning("[轮转] 收到取消请求,已创建 %d/%d 个新号", i, remaining)
                    break
                logger.info("[5/5] 创建第 %d/%d 个...", i + 1, remaining)
                if not chatgpt or not chatgpt.browser:
                    ensure_chatgpt()
                if create_new_account(chatgpt, ensure_mail()):
                    current_count += 1

        if not chatgpt or not chatgpt.browser:
            ensure_chatgpt()
        final_count = get_team_member_count(chatgpt)
        logger.info("[轮转] 最终 Team 成员数: %d（目标: %d）", final_count, TARGET)
        if final_count > TARGET:
            logger.warning("[轮转] 最终 Team 成员数超出目标，后续将按清理逻辑修正")
        elif 0 <= final_count < TARGET:
            logger.warning("[轮转] 最终 Team 成员数仍低于目标 (%d/%d)", final_count, TARGET)

    finally:
        if chatgpt and chatgpt.browser:
            chatgpt.stop()
        # 所有操作完成后统一同步 CPA，避免中途同步导致 CPA 不可用
        logger.info("[轮转] 轮转完成，同步 CPA...")
        sync_to_cpa()
        logger.info("[轮转] 完成，使用 status 命令查看最新状态")


def cmd_add():
    """手动添加一个新账号"""
    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()
    mail_client = CloudMailClient()
    mail_client.login()

    try:
        result = create_new_account(chatgpt, mail_client)  # 内部会 stop chatgpt
        if result:
            logger.info("[添加] 新账号添加成功: %s", result)
            sync_to_cpa()
        else:
            logger.error("[添加] 添加失败")
    finally:
        if chatgpt.browser:
            chatgpt.stop()


def cmd_manual_add():
    """手动添加账号：优先自动接收 localhost 回调，失败时再手动粘贴回调 URL。"""
    from autoteam.manual_account import ManualAccountFlow

    flow = ManualAccountFlow()
    try:
        result = flow.start()
        logger.info("[手动添加] 打开以下链接完成 OAuth 登录：\n%s", result["auth_url"])
        if result.get("auto_callback_available"):
            logger.info("[手动添加] 已启动本地回调服务 http://localhost:1455/auth/callback，可自动完成认证")
        else:
            logger.warning("[手动添加] 本地自动回调不可用：%s", result.get("auto_callback_error") or "未知错误")

        callback_url = input("登录成功后：若自动完成则直接回车；否则粘贴回调 URL（留空取消）: ").strip()
        if callback_url:
            result = flow.submit_callback(callback_url)
        else:
            result = flow.status()
            if result.get("status") != "completed":
                logger.warning("[手动添加] 未检测到自动回调，已取消")
                return None

        account = result.get("account") or {}
        logger.info(
            "[手动添加] 完成: %s (plan=%s, status=%s)",
            account.get("email") or "?",
            account.get("plan_type") or "?",
            account.get("status") or "?",
        )
        return result
    finally:
        flow.stop()


def _refresh_main_auth_after_admin_login():
    try:
        info = refresh_main_auth_file()
        logger.info("[管理员登录] 已保存主号认证文件: %s", info.get("auth_file"))
        return info
    except Exception as exc:
        logger.warning("[管理员登录] 主号认证文件生成失败: %s", exc)
        return None


def cmd_admin_login(email=None):
    """交互式完成管理员登录并保存到 state.json。"""
    email = (email or "").strip()
    if not email:
        email = input("管理员邮箱: ").strip()

    if not email:
        logger.error("[管理员登录] 邮箱不能为空")
        return None

    chatgpt = ChatGPTTeamAPI()

    try:
        logger.info("[管理员登录] 开始: %s", email)
        result = chatgpt.begin_admin_login(email)
        step = result.get("step")

        while True:
            if step == "completed":
                info = chatgpt.complete_admin_login()
                chatgpt.stop()
                _refresh_main_auth_after_admin_login()
                logger.info("[管理员登录] 登录完成: %s", info.get("email") or email)
                if info.get("account_id"):
                    logger.info("[管理员登录] Workspace ID: %s", info["account_id"])
                if info.get("workspace_name"):
                    logger.info("[管理员登录] Workspace 名称: %s", info["workspace_name"])
                return info

            if step == "password_required":
                password = getpass.getpass("管理员密码（留空取消）: ")
                if not password:
                    logger.warning("[管理员登录] 已取消")
                    return None
                result = chatgpt.submit_admin_password(password)
                step = result.get("step")
                continue

            if step == "code_required":
                code = input("邮箱验证码（留空取消）: ").strip()
                if not code:
                    logger.warning("[管理员登录] 已取消")
                    return None
                result = chatgpt.submit_admin_code(code)
                step = result.get("step")
                continue

            if step == "workspace_required":
                options = chatgpt.list_workspace_options()
                if not options:
                    raise RuntimeError("当前需要选择组织，但未获取到可选项")

                logger.info("[管理员登录] 请选择要进入的 workspace:")
                for idx, option in enumerate(options, 1):
                    suffix = " [推荐]" if option.get("kind") == "preferred" else ""
                    logger.info("[管理员登录]   %d. %s%s", idx, option["label"], suffix)

                choice = input("选择序号（留空取消）: ").strip()
                if not choice:
                    logger.warning("[管理员登录] 已取消")
                    return None
                if not choice.isdigit():
                    raise RuntimeError(f"无效的序号: {choice}")

                selected_index = int(choice) - 1
                if selected_index < 0 or selected_index >= len(options):
                    raise RuntimeError(f"序号超出范围: {choice}")

                result = chatgpt.select_workspace_option(options[selected_index]["id"])
                step = result.get("step")
                continue

            detail = result.get("detail") or "无法识别管理员登录步骤"
            raise RuntimeError(detail)

    except KeyboardInterrupt:
        logger.warning("[管理员登录] 已中断")
        return None
    finally:
        chatgpt.stop()


def cmd_admin_session(email=None):
    """手动导入管理员 session_token 并保存到 state.json。"""
    email = (email or "").strip()
    if not email:
        email = input("管理员邮箱: ").strip()

    if not email:
        logger.error("[管理员登录] 邮箱不能为空")
        return None

    session_token = getpass.getpass("session_token（留空取消）: ").strip()
    if not session_token:
        logger.warning("[管理员登录] 已取消")
        return None

    chatgpt = ChatGPTTeamAPI()
    try:
        logger.info("[管理员登录] 开始导入 session_token: %s", email)
        info = chatgpt.import_admin_session(email, session_token)
        chatgpt.stop()
        _refresh_main_auth_after_admin_login()
        logger.info("[管理员登录] session_token 导入完成: %s", info.get("email") or email)
        if info.get("account_id"):
            logger.info("[管理员登录] Workspace ID: %s", info["account_id"])
        if info.get("workspace_name"):
            logger.info("[管理员登录] Workspace 名称: %s", info["workspace_name"])
        return info
    finally:
        chatgpt.stop()


def cmd_main_codex_sync():
    """交互式同步主号 Codex 认证到 CPA。"""
    state = get_admin_state_summary()
    if not state.get("session_present") or not state.get("email"):
        logger.error("[主号 Codex] 缺少管理员登录态，请先执行 admin-login")
        return None

    saved_auth_file = get_saved_main_auth_file()
    if saved_auth_file:
        sync_main_codex_to_cpa(saved_auth_file)
        logger.info("[主号 Codex] 已直接同步现有认证文件: %s", saved_auth_file)
        return {"auth_file": saved_auth_file}

    flow = MainCodexSyncFlow()
    try:
        logger.info("[主号 Codex] 开始同步: %s", state.get("email"))
        result = flow.start()
        step = result.get("step")

        while True:
            if step == "completed":
                info = flow.complete()
                logger.info("[主号 Codex] 同步完成: %s", info.get("email") or state.get("email"))
                if info.get("plan_type"):
                    logger.info("[主号 Codex] Plan: %s", info["plan_type"])
                if info.get("auth_file"):
                    logger.info("[主号 Codex] Auth 文件: %s", info["auth_file"])
                return info

            if step == "password_required":
                password = getpass.getpass("主号密码（留空取消）: ")
                if not password:
                    logger.warning("[主号 Codex] 已取消")
                    return None
                result = flow.submit_password(password)
                step = result.get("step")
                continue

            if step == "code_required":
                code = input("主号验证码（留空取消）: ").strip()
                if not code:
                    logger.warning("[主号 Codex] 已取消")
                    return None
                result = flow.submit_code(code)
                step = result.get("step")
                continue

            detail = result.get("detail") or "无法识别主号 Codex 登录步骤"
            raise RuntimeError(detail)
    except KeyboardInterrupt:
        logger.warning("[主号 Codex] 已中断")
        return None
    finally:
        flow.stop()


def get_team_member_count(chatgpt_api):
    """获取当前 Team 成员数"""
    account_id = get_chatgpt_account_id()
    if not account_id:
        logger.error("[Team] account_id 为空，无法查询成员数")
        return -1
    path = f"/backend-api/accounts/{account_id}/users"
    result = chatgpt_api._api_fetch("GET", path)
    if result["status"] != 200:
        logger.error("[Team] 获取成员列表失败: %d %s", result["status"], result["body"][:200])
        return -1
    data = json.loads(result["body"])
    members = data.get("items", data.get("users", data.get("members", [])))
    return len(members)


def cmd_fill(target=5, leave_workspace=False):
    """
    补位流程。
    leave_workspace=False: 补满 Team 席位到 target（原行为），优先复用 standby 旧号
    leave_workspace=True:  按 target 作为"要生产的免费号数量"，每个账号注册后立刻退出 Team、走 personal OAuth
    """
    if leave_workspace:
        return _cmd_fill_personal(target)

    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()
    mail_client = CloudMailClient()
    mail_client.login()

    try:
        current = get_team_member_count(chatgpt)
        if current < 0:
            logger.error("[填充] 获取成员列表失败")
            return

        logger.info("[填充] 当前 Team 成员数: %d，目标: %d", current, target)

        need = target - current
        if need <= 0:
            logger.info("[填充] 成员数已满足（%d >= %d），无需添加", current, target)
            return

        logger.info("[填充] 需要添加 %d 个账号", need)
        standby_list = [
            a
            for a in get_standby_accounts()
            if a.get("_quota_recovered") and not _is_main_account_email(a.get("email"))
        ]
        standby_index = 0

        from autoteam import cancel_signal

        for i in range(need):
            if cancel_signal.is_cancelled():
                logger.warning("[填充] 收到取消请求,已完成 %d/%d", i, need)
                break
            logger.info("[填充] 添加第 %d/%d 个账号...", i + 1, need)

            # 优先复用 standby 中额度已恢复的旧账号
            added = False
            while standby_index < len(standby_list):
                reusable = standby_list[standby_index]
                standby_index += 1
                email = reusable["email"]
                skip_reason = _auto_reuse_skip_reason(reusable)
                if skip_reason:
                    logger.info("[填充] 跳过旧账号: %s（%s）", email, skip_reason)
                    continue
                logger.info("[填充] 复用旧账号: %s", email)
                # 确保 chatgpt 浏览器可用
                if not chatgpt.browser:
                    chatgpt.start()
                added = reinvite_account(chatgpt, mail_client, reusable)
                if added:
                    break
                logger.warning("[填充] 复用旧账号失败，尝试下一个旧账号: %s", email)

            if not added:
                # 创建新账号
                logger.info("[填充] 创建新账号...")
                if not chatgpt.browser:
                    chatgpt.start()
                added = create_new_account(chatgpt, mail_client)

            if not added:
                logger.warning("[填充] 本轮补位失败，第 %d/%d 个空缺仍未填上", i + 1, need)

            # 验证成员数
            if not chatgpt.browser:
                chatgpt.start()
            new_count = get_team_member_count(chatgpt)
            if new_count >= 0:
                logger.info("[填充] 当前成员数: %d/%d", new_count, target)

        logger.info("[填充] 填充完成")
        sync_to_cpa()
        cmd_status()

    finally:
        if chatgpt.browser:
            chatgpt.stop()


def _summarize_outcomes(outcomes):
    """把 outcome dict 列表按 status 聚合，返回 {status: count} 的 OrderedDict。"""
    from collections import OrderedDict

    counts = OrderedDict()
    for o in outcomes:
        st = (o or {}).get("status") or "unknown"
        counts[st] = counts.get(st, 0) + 1
    return counts


def _fetch_team_non_master_emails(chatgpt_api):
    """
    一次性快照 Team 当前的非主号成员邮箱集合。返回 (ok, emails_set)。
    ok=False 表示鉴权失败或网络问题,调用方可自行决定是重试还是放弃。

    失败时主动 log 具体 status + body 前 200 字,方便用户直接看到根因
    (401="session 失效"、0="playwright JS 抛错网络挂了"等)。
    """
    master_email = _normalized_email(get_admin_email())
    account_id = get_chatgpt_account_id()
    if not account_id:
        logger.error("[免费号] account_id 为空,无法确认席位")
        return False, set()
    try:
        result = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/users")
    except Exception as exc:
        # Playwright 页面崩溃/context 被关掉等底层错误——不是 JS fetch 异常,JS 的 try/catch 接不住
        logger.error("[免费号] 拉取 Team 成员列表抛异常(playwright 层): %s", exc)
        return False, set()
    status = result.get("status")
    if status != 200:
        body_excerpt = (result.get("body") or "")[:200].replace("\n", " ")
        logger.error(
            "[免费号] 拉取 Team 成员列表失败 status=%s body=%s "
            "(可用 POST /api/admin/fix-account-id 自动修正 account_id,或重新导入 session_token)",
            status,
            body_excerpt,
        )
        return False, set()
    try:
        data = json.loads(result["body"])
    except Exception as exc:
        logger.error("[免费号] 成员列表 JSON 解析失败: %s body=%s", exc, (result.get("body") or "")[:200])
        return False, set()
    members = data.get("items", data.get("users", data.get("members", [])))
    emails = {_normalized_email(m.get("email", "")) for m in members if m.get("email")}
    emails.discard(master_email)
    emails.discard("")
    return True, emails


def _wait_team_new_members_cleared(chatgpt_api, baseline_emails, max_wait=180, poll_interval=6):
    """
    等待"不在 baseline 里的新成员"全部被踢出。baseline 是进入 fill-personal 前就已经存在的
    非主号成员(比如 Team fill 创建的真实 Team 子号,用户明确要求保留它们)。

    返回 True: 新增成员已清空(可能还有 baseline 成员在,但那不归本任务管)。
    返回 False: 超时仍有新增成员;或连续 401/403 鉴权失败。

    风控背景:OpenAI 对批量邀请/踢人敏感,每批免费号(注册→主号踢出)完成后等后台真正
    同步完成再开始下一批,避免短时间内大量操作触发风控。
    """
    from autoteam import cancel_signal

    baseline_emails = {e for e in baseline_emails if e}
    master_email = _normalized_email(get_admin_email())
    deadline = time.time() + max_wait
    last_count = None
    # 401 累计计数:管理员 session_token 实际无 admin 权限时,401 会一直不变,
    # 与其傻等 180s 再超时,不如连续 3 次 401 就判定 session 失效,早停并给出可诊断信息
    unauthorized_hits = 0
    forbidden_hits = 0
    while time.time() < deadline:
        # 即使在等待清空,也允许用户点"停止任务"让流程尽早退出,不要硬等 180s
        if cancel_signal.is_cancelled():
            logger.warning("[免费号] 等待新成员清空期间收到取消请求,提前退出")
            return False
        account_id = get_chatgpt_account_id()
        if not account_id:
            logger.error("[免费号] account_id 为空，无法确认席位")
            return False
        path = f"/backend-api/accounts/{account_id}/users"
        result = chatgpt_api._api_fetch("GET", path)
        status = result["status"]
        if status != 200:
            body_excerpt = (result.get("body") or "")[:220].replace("\n", " ")
            logger.warning(
                "[免费号] 成员列表拉取失败: %d，body=%s，继续等待",
                status,
                body_excerpt,
            )
            # OpenAI 对 Team admin 接口:401=session 未认证,403=认证了但非 admin
            # 两种都不是"再等等就好"的状态,快速 fail-fast 比傻等 180s 更有信息量
            if status == 401:
                unauthorized_hits += 1
                if unauthorized_hits >= 3:
                    logger.error(
                        "[免费号] 连续 %d 次 401 鉴权失败，session_token 已失效或权限不足，"
                        "请在「设置」页重新导入管理员 session_token",
                        unauthorized_hits,
                    )
                    return False
            elif status == 403:
                forbidden_hits += 1
                if forbidden_hits >= 3:
                    logger.error(
                        "[免费号] 连续 %d 次 403，当前账号非 workspace admin，"
                        "生成免费号需要管理员在 Team 工作区里踢人的能力",
                        forbidden_hits,
                    )
                    return False
            time.sleep(poll_interval)
            continue

        try:
            data = json.loads(result["body"])
            members = data.get("items", data.get("users", data.get("members", [])))
        except Exception as exc:
            logger.warning("[免费号] 成员列表解析失败: %s", exc)
            time.sleep(poll_interval)
            continue

        emails_in_team = {_normalized_email(m.get("email", "")) for m in members if m.get("email")}
        emails_in_team.discard(master_email)
        emails_in_team.discard("")
        # 只关心"新增"(不在 baseline 里的),baseline 的成员是用户希望保留的 Team 席位
        new_members = emails_in_team - baseline_emails

        if not new_members:
            baseline_still = emails_in_team & baseline_emails
            logger.info(
                "[免费号] 新增成员已清空(baseline 保留 %d 个: %s)",
                len(baseline_still),
                sorted(baseline_still)[:6] or ["-"],
            )
            return True

        if last_count != len(new_members):
            logger.info(
                "[免费号] Team 仍有 %d 个未被踢出的新号: %s,等待清空...",
                len(new_members),
                sorted(new_members)[:6],
            )
            last_count = len(new_members)
        time.sleep(poll_interval)

    logger.error("[免费号] 等待新增成员清空超时(%ss),新号未被踢干净", max_wait)
    return False


def _cmd_fill_personal(count):
    """
    生产 count 个免费号:注册 → 主号踢出 → personal OAuth → 状态置 PERSONAL。

    风控策略(用户明确要求):
    1. 一个主号同时最多 4 个子号在 Team 里 → 每批限制 this_round = min(4, remaining)
    2. 不强制清空 Team 现有席位:进入时把非主号成员邮箱快照为 baseline(可能是 Team fill
       创建的真实 Team 子号,用户希望保留)。每批结束后只等"本批注册的新号"被踢干净,
       不管 baseline 成员是否还在。
    3. 每个账号之间随机 sleep 8-20s,每批之间 30-60s,避免节奏单一被识别
    4. chatgpt_api 在整个 fill 流程里懒加载一次,避免反复 start/stop 产生可疑痕迹
    """
    import random

    count = max(0, int(count or 0))
    if count <= 0:
        logger.info("[免费号] 数量为 0，跳过")
        return

    BATCH_SIZE = 4
    WAIT_TEAM_EMPTY_TIMEOUT = 180

    mail_client = CloudMailClient()
    mail_client.login()

    # 懒加载 chatgpt_api：只在需要查席位时启动
    chatgpt = [None]

    def _ensure_chatgpt():
        if not chatgpt[0] or not chatgpt[0].browser:
            chatgpt[0] = ChatGPTTeamAPI()
            chatgpt[0].start()
        return chatgpt[0]

    def _stop_chatgpt():
        if chatgpt[0] and chatgpt[0].browser:
            try:
                chatgpt[0].stop()
            except Exception as exc:
                logger.debug("[免费号] 关闭 chatgpt_api 异常: %s", exc)
        chatgpt[0] = None

    logger.info("[免费号] 目标 %d 个免费号，每批 %d 个", count, BATCH_SIZE)

    # 启动时快照:记录进入时已经在 Team 里的非主号成员,他们不归本任务管
    # (可能是 Team fill 创建的真实 Team 子号,用户希望保留)
    try:
        api_snap = _ensure_chatgpt()
        ok, baseline_emails = _fetch_team_non_master_emails(api_snap)
        if not ok:
            logger.error(
                "[免费号] 启动时无法拉取 Team 成员列表,鉴权失败或 session_token 无效。"
                "请先用 /api/admin/fix-account-id 或重新导入 session_token。"
            )
            _stop_chatgpt()
            return
        logger.info(
            "[免费号] baseline 非主号成员 %d 个: %s (这些席位不会被清空)",
            len(baseline_emails),
            sorted(baseline_emails)[:6] or ["-"],
        )
    finally:
        _stop_chatgpt()

    produced = 0
    remaining = count
    batch_idx = 0
    # 整轮生产的所有 outcome（每个子号一个 dict），批次末 + 结束时做分类统计
    outcomes = []

    from autoteam import cancel_signal

    try:
        while remaining > 0:
            if cancel_signal.is_cancelled():
                logger.warning("[免费号] 收到取消请求,停止后续批次")
                break
            batch_idx += 1
            # Team 席位总上限 4 人:baseline 已占了一部分,本批最多再加 (4 - baseline) 个,
            # 避免瞬间超过 4 个子号触发 OpenAI 风控。若 baseline 已占满 4,最少放 1 个让流程能推进
            # (用户允许 baseline 存在,但至少要允许本任务有产出空间)。
            max_new_this_batch = max(1, 4 - len(baseline_emails))
            this_round = min(BATCH_SIZE, remaining, max_new_this_batch)
            logger.info(
                "[免费号] === 第 %d 批开始(本批 %d 个,剩余 %d,baseline %d 个) ===",
                batch_idx,
                this_round,
                remaining,
                len(baseline_emails),
            )

            # 第一批进入时 Team 就是 baseline 状态,不需要等;从第二批开始等"上一批新号"被踢干净
            if batch_idx > 1:
                try:
                    api = _ensure_chatgpt()
                    ok = _wait_team_new_members_cleared(api, baseline_emails, max_wait=WAIT_TEAM_EMPTY_TIMEOUT)
                    if not ok:
                        logger.error(
                            "[免费号] 第 %d 批开始前上一批新号未踢干净,停止生产避免触发风控",
                            batch_idx,
                        )
                        break
                finally:
                    # 释放浏览器，让每个子号注册时拿到干净的 playwright 环境
                    _stop_chatgpt()

            batch_produced = 0
            batch_outcomes = []
            for i in range(this_round):
                if cancel_signal.is_cancelled():
                    logger.warning("[免费号] 收到取消请求,跳出本批剩余账号")
                    break
                seq = produced + batch_produced + 1
                logger.info("[免费号] 第 %d 批 第 %d/%d 个（累计 %d/%d）", batch_idx, i + 1, this_round, seq, count)
                # 单个账号内部的任何异常都不能终止整批（否则外层 finally 后的 sync_to_cpa 会丢失已产出的账号）
                outcome = {}
                try:
                    email = create_new_account(None, mail_client, leave_workspace=True, out_outcome=outcome)
                except Exception as exc:
                    logger.error(
                        "[免费号] 第 %d 批 第 %d 个 create_new_account 异常，跳过: %s",
                        batch_idx,
                        i + 1,
                        exc,
                    )
                    email = None
                    outcome = {"status": "exception", "reason": f"未捕获异常: {exc}"}
                    record_failure("", "exception", f"_cmd_fill_personal 里 create_new_account 抛异常: {exc}")

                if not outcome.get("status"):
                    # 例如从 _check_pending_invites 路径成功回来，outcome 没被 create_account_direct 填
                    outcome["status"] = "success" if email else "unknown_failure"

                batch_outcomes.append(outcome)
                outcomes.append(outcome)

                if email:
                    batch_produced += 1
                    logger.info(
                        "[免费号] 第 %d 批 第 %d 个完成: %s (status=%s)",
                        batch_idx,
                        i + 1,
                        email,
                        outcome.get("status"),
                    )
                else:
                    logger.warning(
                        "[免费号] 第 %d 批 第 %d 个生产失败：status=%s, reason=%s, last_email=%s",
                        batch_idx,
                        i + 1,
                        outcome.get("status"),
                        outcome.get("reason"),
                        outcome.get("last_email") or outcome.get("email"),
                    )

                # 账号间随机抖动
                if i < this_round - 1:
                    gap = random.uniform(8, 20)
                    logger.info("[免费号] 账号间间隔 %.1fs", gap)
                    time.sleep(gap)

            produced += batch_produced
            remaining = count - produced
            batch_stats = _summarize_outcomes(batch_outcomes)
            logger.info(
                "[免费号] === 第 %d 批完成：本批成功 %d / %d，累计 %d/%d，剩余 %d ===",
                batch_idx,
                batch_produced,
                this_round,
                produced,
                count,
                remaining,
            )
            logger.info("[免费号] 第 %d 批分类统计: %s", batch_idx, batch_stats)

            # 批次结束后:等本批注册的新号都被踢出(回到 baseline),否则停下
            if remaining > 0:
                try:
                    api = _ensure_chatgpt()
                    ok = _wait_team_new_members_cleared(api, baseline_emails, max_wait=WAIT_TEAM_EMPTY_TIMEOUT)
                    if not ok:
                        logger.error("[免费号] 第 %d 批结束后新号未踢干净,停止继续生产", batch_idx)
                        break
                finally:
                    _stop_chatgpt()

                cool_down = random.uniform(30, 60)
                logger.info("[免费号] 批次间冷却 %.1fs", cool_down)
                time.sleep(cool_down)
    finally:
        _stop_chatgpt()
        # 无论主循环以何种方式退出（完成 / 被阻断 / 异常），都汇总一次 + 把已生产的账号同步进 CPA
        total_stats = _summarize_outcomes(outcomes)
        logger.info(
            "[免费号汇总] 目标 %d，尝试 %d，成功 %d，失败 %d（共 %d 批）",
            count,
            len(outcomes),
            produced,
            len(outcomes) - produced,
            batch_idx,
        )
        logger.info("[免费号汇总] 各类分布: %s", total_stats)
        # 把每个失败账号的 last_email + status + reason 再打一条，方便直接定位
        for o in outcomes:
            if o.get("status") != "success":
                logger.info(
                    "[免费号汇总] FAIL email=%s status=%s reason=%s",
                    o.get("last_email") or o.get("email") or "",
                    o.get("status"),
                    o.get("reason"),
                )
        try:
            sync_to_cpa()
        except Exception as exc:
            logger.error("[免费号] sync_to_cpa 异常（已生产账号本地已入池，可稍后手动同步）: %s", exc)
        try:
            cmd_status()
        except Exception as exc:
            logger.error("[免费号] cmd_status 异常: %s", exc)


def cmd_cleanup(max_seats=None):
    """清理多余的 Team 成员，只移除本地 accounts.json 中管理的账号"""
    account_id = get_chatgpt_account_id()
    accounts = load_accounts()
    local_emails = {a["email"].lower() for a in accounts if not _is_main_account_email(a.get("email"))}

    if not local_emails:
        logger.info("[清理] 本地无管理的账号，无需清理")
        return

    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()

    try:
        # 获取当前成员列表
        path = f"/backend-api/accounts/{account_id}/users"
        result = chatgpt._api_fetch("GET", path)

        if result["status"] != 200:
            logger.error("[清理] 获取成员列表失败: %d", result["status"])
            return

        data = json.loads(result["body"])
        members = data.get("items", data.get("users", data.get("members", [])))

        total = len(members)
        logger.info("[清理] 当前 Team 成员数: %d", total)

        # 区分：本地管理的 vs 手动添加的
        local_members = []
        external_members = []
        for m in members:
            email = m.get("email", "").lower()
            if email in local_emails:
                local_members.append(m)
            else:
                external_members.append(m)

        logger.info("[清理] 手动添加的成员: %d", len(external_members))
        for m in external_members:
            logger.info("[清理]   %s (%s)", m.get("email"), m.get("role"))
        logger.info("[清理] 本地管理的成员: %d", len(local_members))
        for m in local_members:
            logger.info("[清理]   %s (%s)", m.get("email"), m.get("role"))

        # 确定要移除的数量
        if max_seats is None:
            max_seats = 5
            logger.info("[清理] 未指定上限，使用默认总人数: %d", max_seats)
        to_remove_count = total - max_seats
        if to_remove_count <= 0:
            logger.info("[清理] 成员数 %d 未超过上限 %d，无需清理", total, max_seats)
            return

        # 从本地管理的账号中选择要移除的（优先移除额度已用完的）
        removable = sorted(
            local_members,
            key=lambda m: (
                # 额度用完的优先移除
                0
                if find_account(accounts, m.get("email", ""))
                and find_account(accounts, m.get("email", "")).get("status") == STATUS_EXHAUSTED
                else 1,
                # 其次按创建时间，旧的优先
                find_account(accounts, m.get("email", "")).get("created_at", 0)
                if find_account(accounts, m.get("email", ""))
                else 0,
            ),
        )

        to_remove = removable[:to_remove_count]
        logger.info("[清理] 需要移除 %d 个本地账号:", len(to_remove))
        for m in to_remove:
            logger.info("[清理]   %s", m.get("email"))

        # 执行移除
        for m in to_remove:
            email = m.get("email", "")
            user_id = m.get("user_id") or m.get("id")

            delete_path = f"/backend-api/accounts/{account_id}/users/{user_id}"
            result = chatgpt._api_fetch("DELETE", delete_path)

            if result["status"] in (200, 204):
                logger.info("[清理] 已移除 %s", email)
                update_account(email, status=STATUS_STANDBY)
            else:
                logger.error("[清理] 移除 %s 失败: %d", email, result["status"])

        # 取消 pending invites 中本地管理的
        inv_result = chatgpt._api_fetch("GET", f"/backend-api/accounts/{account_id}/invites")
        if inv_result["status"] == 200:
            inv_data = json.loads(inv_result["body"])
            invites = (
                inv_data if isinstance(inv_data, list) else inv_data.get("invites", inv_data.get("account_invites", []))
            )
            for inv in invites:
                inv_email = inv.get("email_address", "").lower()
                inv_id = inv.get("id")
                if inv_email in local_emails and inv_id:
                    del_result = chatgpt._api_fetch("DELETE", f"/backend-api/accounts/{account_id}/invites/{inv_id}")
                    if del_result["status"] in (200, 204):
                        logger.info("[清理] 已取消邀请 %s", inv_email)

        logger.info("[清理] 清理完成")
        sync_to_cpa()

    finally:
        chatgpt.stop()


def cmd_pull_cpa():
    """从 CPA 反向同步认证文件到本地。"""
    result = sync_from_cpa()
    logger.info(
        "[CPA] 拉取完成: 新增文件 %d, 更新文件 %d, 新增账号 %d, 更新账号 %d, 跳过 %d",
        result.get("downloaded", 0),
        result.get("updated", 0),
        result.get("accounts_added", 0),
        result.get("accounts_updated", 0),
        result.get("skipped", 0),
    )
    return result


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
    sub.add_parser("manual-add", help="手动 OAuth 添加账号（打开链接登录后粘贴回调 URL）")
    admin_login_p = sub.add_parser("admin-login", help="交互式完成管理员主号登录")
    admin_login_p.add_argument("--email", help="管理员邮箱；不传则运行时交互输入")
    admin_session_p = sub.add_parser("admin-session", help="手动输入 session_token 导入管理员登录态")
    admin_session_p.add_argument("--email", help="管理员邮箱；不传则运行时交互输入")
    sub.add_parser("main-codex-sync", help="交互式同步主号 Codex 到 CPA")

    fill_p = sub.add_parser("fill", help="补满 Team 成员到指定数量")
    fill_p.add_argument("target", type=int, nargs="?", default=5, help="目标成员数（默认 5）")

    cleanup_p = sub.add_parser("cleanup", help="清理多余成员（只移除本地管理的）")
    cleanup_p.add_argument("max_seats", type=int, nargs="?", default=None, help="最大席位数")

    sub.add_parser("sync", help="手动同步认证文件到 CPA")
    sub.add_parser("pull-cpa", help="从 CPA 反向同步认证文件到本地")

    api_p = sub.add_parser("api", help="启动 HTTP API 服务器")
    api_p.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    api_p.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # 首次启动检查必填配置（api 命令在 start_server 里单独处理）
    if args.command not in ("api",):
        from autoteam.setup_wizard import check_and_setup

        check_and_setup(interactive=True)

    try:
        from autoteam.auth_storage import ensure_auth_file_permissions

        ensure_auth_file_permissions()
    except Exception:
        pass

    if args.command == "status":
        cmd_status()
    elif args.command == "check":
        cmd_check()
    elif args.command == "rotate":
        cmd_rotate(args.target)
    elif args.command == "add":
        cmd_add()
    elif args.command == "manual-add":
        cmd_manual_add()
    elif args.command == "admin-login":
        cmd_admin_login(args.email)
    elif args.command == "admin-session":
        cmd_admin_session(args.email)
    elif args.command == "main-codex-sync":
        cmd_main_codex_sync()
    elif args.command == "fill":
        cmd_fill(args.target)
    elif args.command == "cleanup":
        cmd_cleanup(args.max_seats)
    elif args.command == "sync":
        sync_to_cpa()
    elif args.command == "pull-cpa":
        cmd_pull_cpa()
    elif args.command == "api":
        from autoteam.api import start_server

        start_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
