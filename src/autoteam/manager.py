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

from playwright.sync_api import sync_playwright

from autoteam.account_ops import delete_managed_account, fetch_team_state
from autoteam.accounts import (
    STATUS_ACTIVE,
    STATUS_AUTH_INVALID,
    STATUS_EXHAUSTED,
    STATUS_ORPHAN,
    STATUS_PENDING,
    STATUS_PERSONAL,
    STATUS_STANDBY,
    add_account,
    delete_account,
    find_account,
    get_standby_accounts,
    is_supported_plan,
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
from autoteam.invite import RegisterBlocked  # SPEC-2 shared/add-phone-detection §5 — 5 处 OAuth 调用方 catch
from autoteam.register_failures import MASTER_SUBSCRIPTION_DEGRADED, record_failure
from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)

MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))


# Round 7 P2.5 — 把 wham/usage 原始 rate_limit 子树序列化为字符串,
# 供 record_failure(no_quota_assigned, raw_rate_limit=...) 落 register_failures.json,
# 便于事后排查 OpenAI 协议变化。截断 2000 字符防止 register_failures.json 膨胀(NFR-6)。
_RAW_RATE_LIMIT_MAX_CHARS = 2000


def _extract_raw_rate_limit_str(quota_info) -> str:
    """从 check_codex_quota 返回的 quota_info / exhausted_info 中提取原始 rate_limit 子树并 JSON 序列化。

    输入既可能是 ok 形态的 quota_info(顶层 raw_rate_limit / primary_window),
    也可能是 exhausted/no_quota 形态的 exhausted_info(raw_rate_limit 顶层 + 子层 quota_info)。
    序列化失败时返回空串,不阻塞 record_failure 主流程(R5 缓解)。
    """
    if not isinstance(quota_info, dict):
        return ""
    raw = (
        quota_info.get("raw_rate_limit")
        or (quota_info.get("quota_info") or {}).get("raw_rate_limit")
        or quota_info.get("primary_window")
        or (quota_info.get("quota_info") or {}).get("primary_window")
    )
    if not raw:
        return ""
    try:
        return json.dumps(raw, ensure_ascii=False)[:_RAW_RATE_LIMIT_MAX_CHARS]
    except Exception:
        return ""


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


# Team 子账号(非主号)硬上限。主号 + 4 子号 = 5 席,与 cmd_rotate / cmd_fill 默认 target=5 一致。
# 超过这个数说明有"假 standby / 假 personal"在 Team 里占席位(同步延迟或历史 bug 遗留),
# _reconcile_team_members 会按优先级 kick 多余者,永不让 Team 超出 4 子号。
TEAM_SUB_ACCOUNT_HARD_CAP = 4


def _find_team_auth_file(email):
    """在 auths 目录里找 codex-{email}-team-*.json。找到返回字符串路径,否则 None。

    严格只接 -team-*.json:personal/plus/free 席位 auth 不能用于 Team 子号,
    用错 plan 的 bundle 会被 OAuth 拒收(参考 codex-oauth personal 模式回退)。
    """
    try:
        from autoteam.auth_storage import AUTH_DIR
    except Exception:
        return None
    if not AUTH_DIR.exists():
        return None
    candidates = sorted(AUTH_DIR.glob(f"codex-{email}-team-*.json"))
    return str(candidates[0]) if candidates else None


def _is_quota_exhausted_snapshot(acc):
    """本地 last_quota 表明 5h 和周额度均满(pct=100)→ 耗尽未抛弃。"""
    lq = acc.get("last_quota") or {}
    if not lq:
        return False
    try:
        return int(lq.get("primary_pct", 0)) >= 100 and int(lq.get("weekly_pct", 0)) >= 100
    except (TypeError, ValueError):
        return False


def _check_and_mark_exhausted(acc, email, _safe_update, result):
    """若本地 last_quota 显示耗尽,则标 EXHAUSTED + quota_exhausted_at,返回 True。

    抽出来给两条 auth 补齐路径(STANDBY 错位 / ACTIVE 缺 auth)在补 auth 后调用,
    防止 quota 已满的成员补完 auth 又被当成正常 active 留下,等到下一轮 cmd_check
    才发现耗尽。
    """
    if not _is_quota_exhausted_snapshot(acc):
        return False
    logger.warning(
        "[对账] %s 补齐 auth 后 last_quota 显示耗尽,改标 EXHAUSTED(不立即 kick)",
        email,
    )
    _safe_update(
        acc.get("email"),
        status=STATUS_EXHAUSTED,
        quota_exhausted_at=time.time(),
    )
    result["exhausted_marked"].append(email)
    return True


def _reconcile_team_members(chatgpt_api=None, *, dry_run=False):
    """对账:Team 实际成员 vs 本地 accounts.json,修复一切不一致。

    触发原因:历史 bug(OpenAI /users 同步延迟 → remove_from_team already_absent 误判 →
    DELETE 被跳过)在 Team 里留下"假 standby""假 personal"遗留成员,占 4 子号的席位。

    处理矩阵(第一轮):
        Team里 + 本地 active + auth_file 存在           → 正常
        Team里 + 本地 active + auth_file 缺失           → **残废**
            RECONCILE_KICK_ORPHAN=true(默认): KICK
            RECONCILE_KICK_ORPHAN=false: 标 STATUS_ORPHAN 等人工
        Team里 + 本地 active + last_quota 5h/周 均满    → **耗尽未抛弃**
            标 STATUS_EXHAUSTED + quota_exhausted_at=now,**不立即 kick**
            (避免 token_revoked 风控,让 cmd_replace 走正常流程)
        Team里 + 本地 pending                            → 升 active
        Team里 + 本地 standby                            → **错位**。修正本地 active,
            校验 / 补齐 auth_file 指向 auths/codex-{email}-team-*.json
        Team里 + 本地 exhausted                          → 假 exhausted,KICK
        Team里 + 本地 personal                           → fill-personal 本应踢,KICK
        Team里 + 本地 auth_invalid                       → token 失效,KICK
        Team里 + 本地 orphan                             → 已标记,保留原状
        Team里 + 本地无记录                              → **ghost**
            RECONCILE_KICK_GHOST=true(默认): KICK;否则留给 sync_account_states

    之后若 Team 非主号子账号仍 > TEAM_SUB_ACCOUNT_HARD_CAP,按 orphan → auth_invalid →
    exhausted → personal → standby → 额度最低 active 顺序 kick 到刚好 4 为止。

    dry_run=True 只诊断不动账户,用于 cmd_reconcile_dry_run。
    """
    from autoteam.config import RECONCILE_KICK_GHOST, RECONCILE_KICK_ORPHAN

    result = {
        "kicked": [],
        "flipped_to_active": [],
        "orphan_kicked": [],
        "orphan_marked": [],
        "misaligned_fixed": [],
        "exhausted_marked": [],
        "ghost_kicked": [],
        "ghost_seen": [],
        "over_cap_kicked": [],
        "dry_run": bool(dry_run),
    }
    account_id = get_chatgpt_account_id()
    if not account_id:
        logger.warning("[对账] account_id 为空,跳过对账")
        return result

    need_stop = False
    if not chatgpt_api or not getattr(chatgpt_api, "browser", None):
        try:
            chatgpt_api = ChatGPTTeamAPI()
            chatgpt_api.start()
            need_stop = True
        except Exception as exc:
            logger.warning("[对账] 无法启动 ChatGPTTeamAPI,跳过对账: %s", exc)
            return result

    def _safe_kick(email_to_kick):
        if dry_run:
            logger.info("[对账/dry-run] 将会 KICK %s(未执行)", email_to_kick)
            return "dry_run"
        try:
            return remove_from_team(chatgpt_api, email_to_kick, return_status=True)
        except Exception as exc:
            logger.error("[对账] KICK %s 抛异常: %s", email_to_kick, exc)
            return "error"

    def _safe_update(email_to_update, **fields):
        if dry_run:
            logger.info("[对账/dry-run] update_account(%s, %s)(未执行)", email_to_update, fields)
            return
        update_account(email_to_update, **fields)

    try:
        path = f"/backend-api/accounts/{account_id}/users"
        resp = chatgpt_api._api_fetch("GET", path)
        if resp.get("status") != 200:
            logger.warning("[对账] /users 返回 status=%s,跳过", resp.get("status"))
            return result
        try:
            data = json.loads(resp.get("body") or "{}")
        except Exception as exc:
            logger.warning("[对账] 解析 /users body 失败: %s", exc)
            return result
        members = data.get("items", data.get("users", data.get("members", [])))

        accounts = load_accounts()
        by_email = {(a.get("email") or "").lower(): a for a in accounts}

        # 收集 Team 里非主号成员
        team_subs = []
        for m in members:
            email = (m.get("email") or "").lower()
            if not email or _is_main_account_email(email):
                continue
            team_subs.append((email, m))

        # 第一轮:按状态对账
        for email, _m in team_subs:
            acc = by_email.get(email)
            if not acc:
                # ghost: workspace 有 + 本地完全无记录
                result["ghost_seen"].append(email)
                if RECONCILE_KICK_GHOST:
                    logger.warning("[对账] ghost 成员 %s(本地无记录),KICK", email)
                    rs = _safe_kick(email)
                    if rs in ("removed", "already_absent", "dry_run"):
                        result["ghost_kicked"].append(email)
                else:
                    logger.info(
                        "[对账] ghost 成员 %s,RECONCILE_KICK_GHOST=false,留给 sync 补录",
                        email,
                    )
                continue

            status = acc.get("status")

            if status == STATUS_PENDING:
                logger.info("[对账] %s pending → active(Team 里已存在)", email)
                # 同步当前 workspace 指纹,防止下轮 sync_account_states 把它误打 standby
                _safe_update(acc.get("email"), status=STATUS_ACTIVE, workspace_account_id=account_id)
                result["flipped_to_active"].append(email)
                continue

            if status == STATUS_STANDBY:
                # 错位:workspace 是事实来源,本地 standby 是陈旧状态
                logger.warning("[对账] %s 错位(workspace=active 本地=standby),修正 active", email)
                auth_path = acc.get("auth_file")
                if not auth_path or not Path(auth_path).exists():
                    found = _find_team_auth_file(email)
                    if found:
                        logger.info("[对账] %s 补齐 auth_file=%s", email, found)
                        _safe_update(
                            acc.get("email"),
                            status=STATUS_ACTIVE,
                            auth_file=found,
                            workspace_account_id=account_id,
                        )
                        result["misaligned_fixed"].append(email)
                        # fallthrough:补齐 auth 后仍要做 quota 耗尽检查,
                        # 否则刚补完的 active 成员若 last_quota=0/0 会被漏标 EXHAUSTED
                        _check_and_mark_exhausted(acc, email, _safe_update, result)
                    else:
                        # 错位且找不到 auth → 实为残废,降级
                        logger.warning("[对账] %s 错位但无 auth 文件,降级为残废分支", email)
                        if RECONCILE_KICK_ORPHAN:
                            rs = _safe_kick(email)
                            if rs in ("removed", "already_absent", "dry_run"):
                                result["orphan_kicked"].append(email)
                                # KICK 成功后必须同步本地状态,否则下次 fill 仍按 active 计数(回归 bug)
                                _safe_update(acc.get("email"), status=STATUS_AUTH_INVALID)
                        else:
                            _safe_update(acc.get("email"), status=STATUS_ORPHAN)
                            result["orphan_marked"].append(email)
                else:
                    _safe_update(acc.get("email"), status=STATUS_ACTIVE, workspace_account_id=account_id)
                    result["misaligned_fixed"].append(email)
                    _check_and_mark_exhausted(acc, email, _safe_update, result)
                continue

            if status == STATUS_ACTIVE:
                # 残废检查:workspace + 本地 active 但 auth_file 缺失
                auth_path = acc.get("auth_file")
                if not auth_path or not Path(auth_path).exists():
                    found = _find_team_auth_file(email)
                    if found:
                        logger.info("[对账] %s active + auth_file=null,发现 %s,补上", email, found)
                        _safe_update(acc.get("email"), auth_file=found)
                        # fallthrough:补 auth 后仍要 quota 耗尽检查,避免漏标 EXHAUSTED
                        _check_and_mark_exhausted(acc, email, _safe_update, result)
                        continue
                    if RECONCILE_KICK_ORPHAN:
                        logger.warning("[对账] 残废 %s(workspace 有 + 本地 auth 缺失),KICK", email)
                        rs = _safe_kick(email)
                        if rs in ("removed", "already_absent", "dry_run"):
                            result["orphan_kicked"].append(email)
                            # KICK 成功后必须同步本地状态,否则下次 fill 仍按 active 计数(回归 bug)
                            _safe_update(acc.get("email"), status=STATUS_AUTH_INVALID)
                    else:
                        logger.warning("[对账] 残废 %s,RECONCILE_KICK_ORPHAN=false,标 STATUS_ORPHAN", email)
                        _safe_update(acc.get("email"), status=STATUS_ORPHAN)
                        result["orphan_marked"].append(email)
                    continue

                # 耗尽未抛弃: last_quota 5h/周 均 100% → 标 EXHAUSTED,**不**立即 kick
                if _is_quota_exhausted_snapshot(acc):
                    logger.warning(
                        "[对账] %s active + last_quota 0/0(耗尽未抛弃),标 EXHAUSTED(不立即 kick)",
                        email,
                    )
                    _safe_update(
                        acc.get("email"),
                        status=STATUS_EXHAUSTED,
                        quota_exhausted_at=time.time(),
                    )
                    result["exhausted_marked"].append(email)
                    continue

                # 正常 active
                continue

            if status == STATUS_ORPHAN:
                # 上一轮已经标 orphan,等人工补 auth,不反复 kick
                logger.debug("[对账] %s 已标 STATUS_ORPHAN,跳过", email)
                continue

            if status in (STATUS_EXHAUSTED, STATUS_PERSONAL, STATUS_AUTH_INVALID):
                logger.warning("[对账] %s 本地=%s 但 Team 里仍挂着,KICK", email, status)
                rs = _safe_kick(acc.get("email"))
                if rs in ("removed", "already_absent", "dry_run"):
                    # standby/exhausted 保留原状态,personal 也保留(下次 fill-personal 才真处理)
                    result["kicked"].append(email)
                elif rs != "error":
                    logger.error("[对账] KICK %s 失败: status=%s", email, rs)
                continue

        # 第二轮:硬上限 4 子号。
        # 非 dry_run:kick 完第一轮再 GET /users 拿最新数;
        # dry_run:**不**重新 GET /users —— 否则刚"假装 KICK"的 ghost 仍在 workspace 真实
        # 成员里,会被算进 remaining_subs,over_cap 数量被高估。改用第一轮 team_subs
        # 减去本轮已被标 KICK 的 email,模拟 dry_run 后的 remaining。
        if dry_run:
            kicked_in_round_one = set(result["kicked"] + result["orphan_kicked"] + result["ghost_kicked"])
            remaining_subs = [email for email, _m in team_subs if email not in kicked_in_round_one]
        else:
            resp2 = chatgpt_api._api_fetch("GET", path)
            if resp2.get("status") == 200:
                try:
                    data2 = json.loads(resp2.get("body") or "{}")
                    members2 = data2.get("items", data2.get("users", data2.get("members", [])))
                except Exception:
                    members2 = members
            else:
                members2 = members
            remaining_subs = [
                (m.get("email") or "").lower()
                for m in members2
                if (m.get("email") or "") and not _is_main_account_email(m.get("email"))
            ]
        excess = len(remaining_subs) - TEAM_SUB_ACCOUNT_HARD_CAP
        if excess > 0:
            logger.warning(
                "[对账%s] Team 子号 %d > 硬上限 %d,按优先级 kick %d 个",
                "/dry-run" if dry_run else "",
                len(remaining_subs),
                TEAM_SUB_ACCOUNT_HARD_CAP,
                excess,
            )
            # dry_run 下复用第一轮 by_email,不再读 accounts.json,保持只读纯净
            if dry_run:
                acc_map = by_email
            else:
                accounts_now = load_accounts()
                acc_map = {(a.get("email") or "").lower(): a for a in accounts_now}

            def _priority(email):
                # 优先级越小越先 kick
                a = acc_map.get(email)
                if not a:
                    # ghost(本地无记录):仅当 KICK_GHOST=True 才优先 kick;
                    # 关闭时排到最后,避免绕过 RECONCILE_KICK_GHOST 开关
                    return (0, 0) if RECONCILE_KICK_GHOST else (99, 0)
                st = a.get("status")
                if st == STATUS_ORPHAN:
                    return (1, 0)
                if st == STATUS_AUTH_INVALID:
                    return (1, 1)
                if st == STATUS_EXHAUSTED:
                    return (2, 0)
                if st == STATUS_PERSONAL:
                    return (3, 0)
                if st == STATUS_STANDBY:
                    return (4, 0)
                if st == STATUS_ACTIVE:
                    # active 按额度剩余从低到高 kick
                    lq = a.get("last_quota") or {}
                    p_remain = 100 - lq.get("primary_pct", 0)
                    return (5, p_remain)
                return (6, 0)

            victims = sorted(remaining_subs, key=_priority)[:excess]
            if dry_run:
                # 只预测,不调 _safe_kick(它内部 dry_run 也只是 log),不写 acc 状态
                for email in victims:
                    logger.info(
                        "[对账/dry-run] 超员预测 kick %s (priority=%s)",
                        email,
                        _priority(email),
                    )
                    result["over_cap_kicked"].append(email)
            else:
                for email in victims:
                    try:
                        remove_status = remove_from_team(chatgpt_api, email, return_status=True)
                        if remove_status in ("removed", "already_absent"):
                            acc = acc_map.get(email)
                            if acc and acc.get("status") == STATUS_ACTIVE:
                                update_account(acc.get("email"), status=STATUS_STANDBY)
                            result["over_cap_kicked"].append(email)
                            logger.info("[对账] 超员 kick %s (priority=%s)", email, _priority(email))
                        else:
                            logger.error("[对账] 超员 kick %s 失败: status=%s", email, remove_status)
                    except Exception as exc:
                        logger.error("[对账] 超员 kick %s 抛异常: %s", email, exc)
    finally:
        if need_stop:
            try:
                chatgpt_api.stop()
            except Exception:
                pass

    return result


def _probe_kicked_account(acc):
    """SPEC-2 FR-E1 — 单账号探测:wham/usage 401/403 → 被踢;ok / 其他 → 自然待机/未确认.

    返回 status_str(check_codex_quota 5 分类之一)或 None(无法探测)。
    任何异常吞掉返回 None,让上游降级 STANDBY 旧行为。

    NB: 该函数会从 auth_file 读 access_token,探测时间长(网络往返 + 5s 超时),
    上游必须用 ThreadPoolExecutor 并发调用,否则 N 个账号串行会让 sync 超 30s。
    """
    auth_file = acc.get("auth_file")
    if not auth_file:
        return None
    try:
        path_obj = Path(auth_file)
        if not path_obj.exists():
            return None
        data = json.loads(path_obj.read_text(encoding="utf-8"))
        access_token = data.get("access_token") or (data.get("tokens") or {}).get("access_token")
        if not access_token:
            return None
        status, _ = check_codex_quota(access_token)
        return status
    except Exception as exc:
        logger.debug("[同步] _probe_kicked_account(%s) 异常: %s", acc.get("email"), exc)
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

    # SPEC-2 FR-E1~E4 — 区分人工踢出 vs 自然待机:
    # 不在 Team 的 active 号 → wham/usage 401/403 → AUTH_INVALID(被踢);ok / network → STANDBY(自然)。
    # 探测有并发上限 5 + 单调用 5s + 整体 30s + 30 分钟 last_quota_check_at 去重。
    from autoteam.runtime_config import get_sync_probe_concurrency, get_sync_probe_cooldown_minutes

    cooldown_secs = max(60, int(get_sync_probe_cooldown_minutes()) * 60)
    concurrency = max(1, int(get_sync_probe_concurrency()))
    need_probe = []
    now_ts = time.time()

    for acc in accounts:
        email = acc["email"].lower()
        in_team = email in team_emails

        if in_team and acc["status"] in (STATUS_STANDBY, STATUS_PENDING):
            acc["status"] = STATUS_ACTIVE
            # 把当前 workspace 指纹打到记录上,后续若母号切换可以靠这字段识别
            if account_id:
                acc["workspace_account_id"] = account_id
            changed = True
        elif not in_team and acc["status"] == STATUS_ACTIVE:
            # 守卫(Bug 4A):账号记录的 workspace_account_id 与当前 workspace 不一致 →
            # 这是母号切换造成的"前母号留下号",不是真的被踢出。保留原 active,
            # 不要无脑刷成 standby(否则 sync_to_cpa 会把还可用的 token 文件抹掉)。
            # 仅在两个 ID 都存在且不同时跳过 flip;字段缺失/legacy 记录走原行为。
            acc_ws = acc.get("workspace_account_id")
            if acc_ws and account_id and acc_ws != account_id:
                logger.warning(
                    "[同步] %s 在当前 workspace 不可见,但其 workspace_account_id=%s ≠ 当前 %s(母号切换遗留),保留 active 不 flip",
                    acc["email"],
                    acc_ws,
                    account_id,
                )
                continue
            # FR-E3 探测去重:30 分钟内不重复探测,直接降级 STANDBY 走旧行为
            last_check = float(acc.get("last_quota_check_at") or 0)
            if (now_ts - last_check) < cooldown_secs:
                acc["status"] = STATUS_STANDBY
                changed = True
                continue
            # 收集到批量待探测列表,后面用 ThreadPoolExecutor 并发跑
            if acc.get("auth_file"):
                need_probe.append(acc)
            else:
                # 没 auth_file 无法探测 → 直接降级 STANDBY(等用户重 OAuth)
                acc["status"] = STATUS_STANDBY
                changed = True

    # FR-E2 并发探测被踢识别(只对 need_probe 中的账号)
    if need_probe:
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
                future_map = {ex.submit(_probe_kicked_account, acc): acc for acc in need_probe}
                done, not_done = concurrent.futures.wait(
                    future_map.keys(), timeout=30.0,
                    return_when=concurrent.futures.ALL_COMPLETED,
                )
                for fut in done:
                    acc = future_map[fut]
                    try:
                        status_str = fut.result(timeout=0.1)
                    except Exception:
                        status_str = None
                    finalize_ts = time.time()
                    acc["last_quota_check_at"] = finalize_ts

                    if status_str == "auth_error":
                        acc["status"] = STATUS_AUTH_INVALID
                        acc["last_kicked_at"] = finalize_ts
                        logger.warning(
                            "[同步] %s wham 401/403 → AUTH_INVALID(判定人工踢出)",
                            acc["email"],
                        )
                    elif status_str == "no_quota":
                        # 不会自然恢复
                        acc["status"] = STATUS_AUTH_INVALID
                        logger.warning("[同步] %s wham no_quota → AUTH_INVALID", acc["email"])
                    else:
                        # ok / exhausted / network_error / None → 自然待机
                        acc["status"] = STATUS_STANDBY
                    changed = True

                # 超时未完成的:保持原 ACTIVE,等下轮(避免误标)
                for fut in not_done:
                    acc = future_map[fut]
                    logger.warning("[同步] %s 探测超时,保留 ACTIVE 等下轮", acc["email"])
                    fut.cancel()
        except Exception as exc:
            logger.warning("[同步] 探测段抛异常,降级所有 need_probe 为 STANDBY: %s", exc)
            for acc in need_probe:
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
                        "workspace_account_id": account_id or None,
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

    使用 auth_file 里保存的 account_id 查询 —— Team/Personal/Free 号各自绑定的
    account_id 不同,不能一律 fallback 到主号 team id,否则 Personal 号查到的会是
    主号 Team 的额度(不准确,且被踢出 Team 后还会 401)。
    """
    email = acc["email"]
    auth_file = acc.get("auth_file")

    if not auth_file or not Path(auth_file).exists():
        return "no_auth", None

    auth_data = json.loads(read_text(Path(auth_file)))
    access_token = auth_data.get("access_token")
    rt = auth_data.get("refresh_token")
    acc_id = auth_data.get("account_id") or None

    if not access_token:
        return "no_auth", None

    status, info = check_codex_quota(access_token, account_id=acc_id)

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
            status, info = check_codex_quota(new_tokens["access_token"], account_id=acc_id)
        else:
            logger.error("[%s] token 刷新失败", email)

    return status, info


STANDBY_PROBE_INTERVAL_SEC = 1.5  # 每个 standby 账号探测间隔,限速避免群访 OpenAI 触发风控
STANDBY_PROBE_DEDUP_SEC = 24 * 3600  # 24h 内已探测过的 standby 跳过


def cmd_check(include_standby: bool = False):
    """检查 active 账号的额度,无认证文件或 auth_error 的自动重新登录 Codex。

    参数:
        include_standby: True 时额外探测 standby 池每个账号的 quota(限速 + 24h 去重),
                         401/403 的标记为 STATUS_AUTH_INVALID。默认 False 保持向后兼容。
    """
    from autoteam.config import AUTO_CHECK_THRESHOLD, CLOUDMAIL_DOMAIN

    # API 运行时配置优先（前端可修改）
    try:
        from autoteam.api import _auto_check_config

        threshold = _auto_check_config.get("threshold", AUTO_CHECK_THRESHOLD)
    except ImportError:
        threshold = AUTO_CHECK_THRESHOLD

    def _check_personal_accounts(threshold):
        """Personal 号只拍快照,不动状态:它不参与轮转,但用户希望能在 UI 看到剩余额度。

        与 active 分支的差异:
        - 不写 STATUS_EXHAUSTED(Personal 额度用完只影响 Codex 使用,不触发轮换)
        - 不自动重新登录(personal OAuth 是人工触发,没有可靠的自动补救路径)
        - auth_error 时仅记录日志,保留旧的 last_quota 供 UI 显示(别抹掉历史数据)
        """
        from autoteam.accounts import load_accounts as _reload

        personal_accs = [
            a for a in _reload() if a["status"] == STATUS_PERSONAL and not _is_main_account_email(a.get("email"))
        ]
        personal_with_auth = [a for a in personal_accs if a.get("auth_file") and Path(a["auth_file"]).exists()]
        if not personal_with_auth:
            return
        logger.info("[检查] 检查 %d 个 personal 账号的额度...", len(personal_with_auth))
        for acc in personal_with_auth:
            email = acc["email"]
            try:
                status_str, info = _check_and_refresh(acc)
            except Exception as exc:
                logger.warning("[%s] personal 额度查询异常: %s", email, exc)
                continue
            if status_str == "ok" and isinstance(info, dict):
                update_account(email, last_quota=info)
                p_remain = 100 - info.get("primary_pct", 0)
                w_remain = 100 - info.get("weekly_pct", 0)
                p_reset = info.get("primary_resets_at", 0)
                p_time = time.strftime("%m-%d %H:%M", time.localtime(p_reset)) if p_reset else "?"
                logger.info(
                    "[%s] (personal) 5h剩余 %d%% (重置 %s) | 周剩余 %d%%",
                    email,
                    p_remain,
                    p_time,
                    w_remain,
                )
            elif status_str == "exhausted":
                quota_info = quota_result_quota_info(info) or {}
                if quota_info:
                    update_account(email, last_quota=quota_info)
                window = info.get("window") if isinstance(info, dict) else ""
                logger.warning(
                    "[%s] (personal) %s额度已用完",
                    email,
                    "周" if window == "weekly" else "5h和周" if window == "combined" else "5h",
                )
            elif status_str == "auth_error":
                logger.warning(
                    "[%s] (personal) token 失效或账号无权访问 wham/usage(伪 personal 号被踢出 Team 后常见),保留旧快照",
                    email,
                )
            elif status_str == "network_error":
                logger.warning("[%s] (personal) 额度查询遇到临时网络错误,保留旧快照,等下一轮", email)
            # status_str == "no_auth" 已在 _check_and_refresh 里被 auth_file 判空挡掉

    # 入口先跑一次对账:凡是"Team 里挂着但本地 standby/exhausted/personal"的遗留成员,
    # 统一 kick。顺便把 Team 子号硬压到 TEAM_SUB_ACCOUNT_HARD_CAP(4)以内。
    # 这里失败不影响后续额度检查(已有 try/except 包裹),避免对账异常把整个 check 打挂。
    try:
        recon = _reconcile_team_members()
        if recon.get("kicked") or recon.get("over_cap_kicked") or recon.get("flipped_to_active"):
            logger.info(
                "[检查] 对账结果:kicked=%d, over_cap_kicked=%d, flipped_to_active=%d",
                len(recon.get("kicked", [])),
                len(recon.get("over_cap_kicked", [])),
                len(recon.get("flipped_to_active", [])),
            )
    except Exception as exc:
        logger.warning("[检查] 对账阶段抛异常(跳过,不影响额度检查): %s", exc)

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
                    update_account(email, status=STATUS_ACTIVE, workspace_account_id=get_chatgpt_account_id() or None)
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
            elif status_str == "network_error":
                # 网络抖动/5xx/429:不算"额度用完",也不算"token 失效"。本轮跳过,不动 status,
                # 不进 exhausted_list,也不进 auth_error_list(避免触发昂贵的重登流程)。
                logger.warning("[%s] 额度查询遇到临时网络错误,本轮跳过,等待下一轮重试", email)

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

    # 已 exhausted 但 5h 重置时间已过 → 复测,真的回血则 promote 回 active,避免轮转
    # 多走一遍"kick → standby → re-invite"。token 仍在 Team 里时这个直接 promote 路径
    # 节省一次 Playwright + remove_from_team + invite + OAuth 的开销。
    accounts_now = load_accounts()
    exhausted_to_probe = [
        a
        for a in accounts_now
        if a.get("status") == STATUS_EXHAUSTED
        and not _is_main_account_email(a.get("email"))
        and a.get("auth_file")
        and Path(a["auth_file"]).exists()
        and a.get("quota_resets_at")
        and time.time() >= a["quota_resets_at"]
    ]
    if exhausted_to_probe:
        logger.info("[检查] 复测 %d 个重置时间已过的 exhausted 账号...", len(exhausted_to_probe))
        for acc in exhausted_to_probe:
            email = acc["email"]
            try:
                status_str, info = _check_and_refresh(acc)
            except Exception as exc:
                logger.warning("[%s] exhausted 复测异常,跳过: %s", email, exc)
                continue
            if status_str == "ok" and isinstance(info, dict):
                p_remain = 100 - info.get("primary_pct", 0)
                if p_remain >= threshold:
                    update_account(
                        email,
                        status=STATUS_ACTIVE,
                        last_quota=info,
                        quota_exhausted_at=None,
                        quota_resets_at=None,
                        workspace_account_id=get_chatgpt_account_id() or None,
                    )
                    logger.info(
                        "[%s] 5h 已重置(剩余 %d%%),从 exhausted 回血到 active",
                        email,
                        p_remain,
                    )
                else:
                    update_account(email, last_quota=info)
                    logger.info(
                        "[%s] 复测后剩余 %d%% < %d%%,保留 exhausted",
                        email,
                        p_remain,
                        threshold,
                    )
            elif status_str == "exhausted":
                quota_info = quota_result_quota_info(info) or {}
                new_resets = quota_result_resets_at(info) or acc.get("quota_resets_at")
                if quota_info:
                    update_account(email, last_quota=quota_info, quota_resets_at=new_resets)
                logger.info("[%s] 复测后仍 exhausted,resets_at 已刷新", email)
            elif status_str == "auth_error":
                logger.warning("[%s] 复测时 token 失效,改标 AUTH_INVALID 等 reconcile 处理", email)
                update_account(email, status=STATUS_AUTH_INVALID)
            elif status_str == "network_error":
                logger.info("[%s] 复测遇到临时网络错误,保留 exhausted,下一轮再试", email)

    # Personal 号独立扫描(不参与轮转,但用户需要看到额度)
    try:
        _check_personal_accounts(threshold)
    except Exception as exc:
        logger.warning("[检查] personal 分支异常(不影响 active 结果): %s", exc)

    # Standby 池额度探测(可选):修复"standby 永远无 quota 数据 → _quota_recovered 失真
    # → fill 时盲选踩雷"的问题。限速 + 24h 去重,探到 401/403 标 STATUS_AUTH_INVALID。
    if include_standby:
        try:
            _probe_standby_quota()
        except Exception as exc:
            logger.warning("[检查] standby 探测分支异常(不影响 active/personal 结果): %s", exc)

    return exhausted_list


def _probe_standby_quota():
    """遍历 standby 池,探测每个账号的 quota。

    - 限速:每账号之间 sleep STANDBY_PROBE_INTERVAL_SEC,避免群访 OpenAI wham/usage 触发风控
    - 去重:last_quota_check_at 在 STANDBY_PROBE_DEDUP_SEC 秒内的跳过
    - auth_error(**仅** 401/403):标 STATUS_AUTH_INVALID,等 reconcile 处置
    - network_error(DNS/timeout/SSL/5xx/429/JSON 解析失败/其他临时错误):
      **不写 last_quota_check_at**(允许下一轮立刻重试),**不改 status**,只 log warning。
      这条修复的就是"网络抖动一次,18 个号一起被误标 AUTH_INVALID 然后被 reconcile 全删"事故。
    - exhausted:刷新 quota_exhausted_at / quota_resets_at(修正过期快照),维持 standby
    - ok:仅写回 last_quota + last_quota_check_at,不改 status(standby 的 status 由 fill/rotate 决定)
    - 未知 status:防御分支只 log,不写时间戳,避免去重逻辑卡住未来真正的探测
    """
    standby = get_standby_accounts()
    if not standby:
        logger.info("[检查] standby 池为空,跳过探测")
        return

    now = time.time()
    to_probe = []
    skipped = 0
    no_auth = 0
    for acc in standby:
        auth_file = acc.get("auth_file")
        if not auth_file or not Path(auth_file).exists():
            no_auth += 1
            continue
        last_check = acc.get("last_quota_check_at") or 0
        if last_check and (now - last_check) < STANDBY_PROBE_DEDUP_SEC:
            skipped += 1
            continue
        to_probe.append(acc)

    if not to_probe:
        logger.info(
            "[检查] standby 池共 %d 个,全部在 24h 内已探测或无 auth_file(skipped=%d,no_auth=%d),跳过",
            len(standby),
            skipped,
            no_auth,
        )
        return

    logger.info(
        "[检查] 探测 %d 个 standby 账号的额度(总 %d,跳过 %d 近期已探测,%d 无 auth_file,间隔 %.1fs)...",
        len(to_probe),
        len(standby),
        skipped,
        no_auth,
        STANDBY_PROBE_INTERVAL_SEC,
    )

    for idx, acc in enumerate(to_probe):
        email = acc["email"]
        if idx > 0:
            time.sleep(STANDBY_PROBE_INTERVAL_SEC)
        try:
            status_str, info = _check_and_refresh(acc)
        except Exception as exc:
            logger.warning("[%s] (standby) 探测异常,跳过: %s", email, exc)
            continue

        probe_ts = time.time()
        if status_str == "ok" and isinstance(info, dict):
            update_account(email, last_quota=info, last_quota_check_at=probe_ts)
            p_remain = 100 - info.get("primary_pct", 0)
            w_remain = 100 - info.get("weekly_pct", 0)
            logger.info("[%s] (standby) 探测成功 5h剩余 %d%% | 周剩余 %d%%", email, p_remain, w_remain)
        elif status_str == "exhausted":
            quota_info = quota_result_quota_info(info) or {}
            resets_at = quota_result_resets_at(info) or int(probe_ts + 18000)
            payload = {
                "last_quota_check_at": probe_ts,
                "quota_exhausted_at": probe_ts,
                "quota_resets_at": resets_at,
            }
            if quota_info:
                payload["last_quota"] = quota_info
            update_account(email, **payload)
            window = info.get("window") if isinstance(info, dict) else ""
            logger.warning(
                "[%s] (standby) %s额度仍未恢复,刷新重置时间",
                email,
                "周" if window == "weekly" else "5h",
            )
        elif status_str == "auth_error":
            update_account(email, status=STATUS_AUTH_INVALID, last_quota_check_at=probe_ts)
            logger.warning("[%s] (standby) auth_file 已失效(401/403),标记 %s", email, STATUS_AUTH_INVALID)
        elif status_str == "network_error":
            # 网络抖动 / 5xx / 429 / JSON 解析失败 — 临时性故障。
            # 关键约束:不写 last_quota_check_at(允许下一轮立刻重试)、不改 status,只 log。
            # 否则一次大规模网络故障会让整批 standby 号在 24h 内不再被探测,且如果以前
            # 错误归到 auth_error 还会被批量误标 AUTH_INVALID(就是事故根因)。
            logger.warning("[%s] (standby) 探测遇到临时网络错误,本轮跳过,不更新状态/时间戳", email)
        elif status_str == "no_auth":
            # 理论上入口已过滤,这里兜底:记时间戳避免下一轮重复命中
            update_account(email, last_quota_check_at=probe_ts)
            logger.info("[%s] (standby) auth_file 缺失,跳过", email)
        else:
            # 未知 status 防御分支:同样不写时间戳。如果误吃了未来新加的 status,
            # 写 last_quota_check_at 会让账号在 24h 内不再被探测,屏蔽问题。
            logger.warning("[%s] (standby) 未知探测结果 %s,本轮跳过,不更新时间戳", email, status_str)


def remove_from_team(chatgpt_api, email, *, return_status=False, lookup_retries=3, retry_interval=3.0):
    """将账号从 Team 中移除。

    OpenAI 的 /backend-api/accounts/{id}/users 对"刚加入 Team 的新成员"存在同步
    延迟(注册进 Team 后立刻 GET 可能拿不到新成员)。如果第一次在 members 列表里
    没找到 target_user_id 就直接判定 already_absent、跳过 DELETE,新号就会被遗留
    在 Team 里 —— 这正是 fill-personal "实际没踢出但本地记录 PERSONAL" 的真根因。

    为此找不到时会重试 `lookup_retries` 次,每次间隔 `retry_interval` 秒。只有
    连续多轮都查不到才判定真的 already_absent。这样对于确实已不在 Team 的历史
    账号,最多多耗 ~lookup_retries*retry_interval 秒(可接受),换来对新加入号
    踢出流程的可靠性。
    """
    if _is_main_account_email(email):
        logger.warning("[Team] 跳过移除主号: %s", email)
        return "failed" if return_status else False

    account_id = get_chatgpt_account_id()
    if not account_id:
        logger.error("[Team] account_id 为空，无法移除 %s", email)
        return "failed" if return_status else False

    email_lc = (email or "").lower()
    target_user_id = None
    total_attempts = max(1, int(lookup_retries) + 1)

    for attempt in range(total_attempts):
        path = f"/backend-api/accounts/{account_id}/users"
        result = chatgpt_api._api_fetch("GET", path)
        status = result.get("status")
        body_excerpt = (result.get("body") or "")[:200].replace("\n", " ")

        if status != 200:
            logger.error(
                "[Team] 获取成员列表失败(第 %d/%d 次): status=%s body=%s",
                attempt + 1,
                total_attempts,
                status,
                body_excerpt,
            )
            # 401/403 是 session/权限类错误,重试也不会变好,快速失败
            if status in (401, 403):
                return "failed" if return_status else False
            if attempt < total_attempts - 1:
                time.sleep(retry_interval)
                continue
            return "failed" if return_status else False

        try:
            data = json.loads(result["body"])
            members = data.get("items", data.get("users", data.get("members", [])))
        except Exception as exc:
            logger.error(
                "[Team] 解析成员列表失败(第 %d/%d 次): %s body=%s", attempt + 1, total_attempts, exc, body_excerpt
            )
            if attempt < total_attempts - 1:
                time.sleep(retry_interval)
                continue
            return "failed" if return_status else False

        for member in members:
            if (member.get("email", "") or "").lower() == email_lc:
                target_user_id = member.get("user_id") or member.get("id")
                break

        if target_user_id:
            if attempt > 0:
                logger.info("[Team] 第 %d 次查询命中 %s → user_id=%s", attempt + 1, email, target_user_id)
            break

        if attempt < total_attempts - 1:
            logger.info(
                "[Team] 成员列表里暂无 %s(共 %d 个成员),可能 OpenAI 同步延迟,%.1fs 后重试 (%d/%d)",
                email,
                len(members),
                retry_interval,
                attempt + 1,
                total_attempts - 1,
            )
            time.sleep(retry_interval)

    if not target_user_id:
        logger.info(
            "[Team] 重试 %d 次后仍未在成员列表中找到 %s,判定为已不在 Team",
            total_attempts,
            email,
        )
        return "already_absent" if return_status else True

    delete_path = f"/backend-api/accounts/{account_id}/users/{target_user_id}"
    result = chatgpt_api._api_fetch("DELETE", delete_path)

    if result["status"] in (200, 204):
        logger.info("[Team] 已将 %s 移出 Team (user_id=%s)", email, target_user_id)
        return "removed" if return_status else True
    else:
        body_excerpt = (result.get("body") or "")[:200].replace("\n", " ")
        logger.error(
            "[Team] 移除 %s 失败: status=%s body=%s (user_id=%s)",
            email,
            result["status"],
            body_excerpt,
            target_user_id,
        )
        return "failed" if return_status else False


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
        # Round 8 — SPEC-2 v1.5 §3.7 + shared/master-subscription-health.md M-T1:
        # personal OAuth 入口先验证母号订阅健康度。母号 cancel 时 personal 子号必拿 plan_type=team,
        # 跑完 OAuth 也只是积累一条 plan_drift,所以 fail-fast 不进 OAuth。
        try:
            from autoteam.master_health import is_master_subscription_healthy
            temp_probe_api = ChatGPTTeamAPI()
            try:
                temp_probe_api.start()
                healthy, master_reason, master_evidence = is_master_subscription_healthy(temp_probe_api)
            finally:
                try:
                    temp_probe_api.stop()
                except Exception:
                    pass
        except Exception as probe_exc:
            logger.warning("[注册] master health probe 异常 %s,按既有逻辑放行", probe_exc)
            healthy, master_reason, master_evidence = True, "active", {"detail": str(probe_exc)[:200]}

        if not healthy and master_reason == "subscription_cancelled":
            logger.error(
                "[注册] master 母号订阅已取消(eligible_for_auto_reactivation=true),fail-fast 不进 personal OAuth: %s",
                email,
            )
            record_failure(
                email, MASTER_SUBSCRIPTION_DEGRADED,
                "master subscription cancelled,personal OAuth 必拿 plan_type=team",
                stage="run_post_register_oauth_personal_precheck",
                master_account_id=master_evidence.get("account_id"),
                master_role=master_evidence.get("current_user_role"),
            )
            update_account(email, status=STATUS_STANDBY)
            _record_outcome("master_degraded", reason="master subscription cancelled")
            return None
        if not healthy and master_reason in ("network_error", "auth_invalid"):
            logger.warning(
                "[注册] master health probe 不确定 (%s),按既有逻辑放行,失败由 plan_drift 兜底",
                master_reason,
            )
        elif not healthy and master_reason in ("workspace_missing", "role_not_owner"):
            logger.warning(
                "[注册] master 异常 (%s, role=%s),仍尝试 personal OAuth,reconcile 后续接管",
                master_reason, master_evidence.get("current_user_role"),
            )

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

        # Round 8 — SPEC-2 v1.5 §3.4.5 + shared/oauth-workspace-selection.md §4.3:
        # 删除原 time.sleep(8)。kick 成功不等同于 auth.openai.com 把 default_workspace_id 从
        # Team 切回 Personal — research/sticky-rejoin §1.2-1.3 实测 default 不会自动 unset。
        # sticky 根因是 default 没切,等多久都没用。改用 oauth_workspace.ensure_personal_workspace_selected
        # 在 OAuth 内主动 POST /api/accounts/workspace/select 切到 personal,5 次外层重试触发后端最终一致性。

        # Round 8 — 5 次 personal OAuth 重试外层(spec §3.4 退避表 ±20% jitter)
        # 触发条件:bundle.plan_type != "free" 但 workspace/select 主路径成功 →
        #   后端最终一致性短暂滞后,重试可触发刷新(openai/codex#1977 ejntaylor 实证)
        import random as _random

        from autoteam.register_failures import (
            OAUTH_PLAN_DRIFT_PERSISTENT as _OAUTH_PLAN_DRIFT_PERSISTENT,
        )
        bundle = None
        max_retries = 5
        retry_backoff = (0, 5, 10, 20, 30)
        plan_drift_history = []  # 收集每次拿到的非-free plan_type,fail-fast 时一起 record_failure

        for attempt in range(max_retries):
            if attempt > 0:
                base_delay = retry_backoff[attempt] if attempt < len(retry_backoff) else 30
                jitter = base_delay * _random.uniform(-0.2, 0.2)
                sleep_secs = max(0, base_delay + jitter)
                logger.info(
                    "[注册] personal OAuth 第 %d/%d 次重试,先退避 %.1fs",
                    attempt + 1, max_retries, sleep_secs,
                )
                if sleep_secs > 0:
                    time.sleep(sleep_secs)

            # SPEC-2 §3.1.3 — personal 分支 catch RegisterBlocked + plan_supported 检查
            try:
                bundle = login_codex_via_browser(email, password, mail_client=mail_client, use_personal=True)
            except RegisterBlocked as blocked:
                # add-phone / duplicate 等异常是 terminal,不重试
                if blocked.is_phone:
                    logger.error(
                        "[注册] %s personal OAuth 触发 add-phone (step=%s),从账号池删除",
                        email, blocked.step,
                    )
                    record_failure(
                        email,
                        "oauth_phone_blocked",
                        f"personal OAuth 阶段触发 add-phone (step={blocked.step})",
                        step=blocked.step,
                        stage="run_post_register_oauth_personal",
                        attempt=attempt + 1,
                    )
                    delete_account(email)
                    _record_outcome("oauth_phone_blocked", reason="personal OAuth 触发 add-phone")
                    return None
                logger.error("[注册] %s personal OAuth RegisterBlocked: %s", email, blocked.reason)
                record_failure(
                    email, "exception",
                    f"personal OAuth RegisterBlocked: {blocked.reason}",
                    stage="run_post_register_oauth_personal",
                    attempt=attempt + 1,
                )
                delete_account(email)
                _record_outcome("oauth_failed", reason=f"unexpected RegisterBlocked: {blocked.reason}")
                return None

            if not bundle:
                # bundle 没拿到 — 这是 oauth 路径 abort(workspace_select 失败 / 网络错误),不重试,直接 fail
                logger.error("[注册] %s personal OAuth 第 %d/%d 次未返回 bundle,放弃重试",
                             email, attempt + 1, max_retries)
                break

            bundle_plan = (bundle.get("plan_type") or "").lower()
            if bundle_plan == "free":
                # 拿到 free,直接出循环走后续 plan_supported / quota probe / save_auth_file
                logger.info("[注册] %s 第 %d 次 personal OAuth 拿到 plan=free,出重试循环",
                            email, attempt + 1)
                break

            # plan_type != free → 后端最终一致性滞后,记一笔继续重试
            plan_drift_history.append({
                "attempt": attempt + 1,
                "plan_type": bundle_plan,
                "plan_type_raw": bundle.get("plan_type_raw"),
                "account_id": bundle.get("account_id"),
            })
            logger.warning(
                "[注册] %s 第 %d/%d 次 personal OAuth 拿到 plan=%s(非 free),继续重试",
                email, attempt + 1, max_retries, bundle_plan,
            )
            bundle = None  # 清掉这次的 bundle,不让 fall-through 误用

        # 退出循环:bundle 要么 None(所有 attempt 都 abort 或 plan_drift),要么 plan==free
        if not bundle:
            logger.error(
                "[注册] %s 5 次 personal OAuth 后仍未拿到 plan=free,fail-fast 删除账号",
                email,
            )
            record_failure(
                email, _OAUTH_PLAN_DRIFT_PERSISTENT,
                f"5 次 personal OAuth 后 plan_type 仍非 free (drift_history={len(plan_drift_history)} 条)",
                stage="run_post_register_oauth_personal",
                attempts=max_retries,
                drift_history=plan_drift_history,
            )
            delete_account(email)
            _record_outcome("oauth_failed", reason="5 次 personal OAuth plan_drift 持续")
            return None

        # bundle.plan_type=="free" 已在循环内验证,此处仍走完整白名单 + quota 流程保留对称
        # SPEC-2 shared/plan-type-whitelist §5.2 — plan_supported=False 直接拒,personal 已 leave_workspace 本地无价值
        plan_supported = bundle.get(
            "plan_supported", is_supported_plan(bundle.get("plan_type", ""))
        )
        if not plan_supported:
            logger.error(
                "[注册] %s personal OAuth 拿到 plan_type=%s 不在白名单,从账号池删除",
                email, bundle.get("plan_type_raw") or bundle.get("plan_type"),
            )
            record_failure(
                email, "plan_unsupported",
                f"personal OAuth bundle plan_type={bundle.get('plan_type_raw')} 不在白名单",
                plan_type=bundle.get("plan_type"),
                plan_type_raw=bundle.get("plan_type_raw"),
                stage="run_post_register_oauth_personal",
            )
            delete_account(email)
            _record_outcome("plan_unsupported", plan=bundle.get("plan_type"))
            return None

        auth_file = save_auth_file(bundle)
        update_fields = {
            "status": STATUS_PERSONAL,
            "seat_type": "codex",
            "auth_file": auth_file,
            "last_active_at": time.time(),
            "plan_type_raw": bundle.get("plan_type_raw"),
        }
        # SPEC-2 FR-D3 — personal 分支也 quota probe(对称设计):free plan 也可能"未分配 codex 配额"
        access_token = bundle.get("access_token")
        if access_token:
            try:
                quota_status, quota_info = check_codex_quota(access_token, account_id=bundle.get("account_id"))
                if quota_status == "ok" and isinstance(quota_info, dict):
                    update_fields["last_quota"] = quota_info
                elif quota_status == "no_quota":
                    # personal free 无配额 — 记一笔但仍保留 PERSONAL,让用户决定删不删
                    record_failure(
                        email, "no_quota_assigned",
                        "personal free plan 无 codex 配额",
                        stage="run_post_register_oauth_personal",
                        raw_rate_limit=_extract_raw_rate_limit_str(quota_info),
                    )
            except Exception as exc:
                record_failure(
                    email, "quota_probe_network_error",
                    f"personal quota probe exception: {exc}",
                    stage="run_post_register_oauth_personal",
                )
        # personal 分支:已主动退出 Team,bundle 是个人 free/plus plan,算 codex 席位
        update_account(email, **update_fields)
        logger.info("[注册] 免费号就绪: %s (plan=%s, attempts=%d)",
                    email, bundle.get("plan_type"), len(plan_drift_history) + 1)
        _record_outcome("success", plan=bundle.get("plan_type"))
        return email

    # Round 8 — SPEC-2 v1.5 §3.7 表第 2 行 + shared/master-subscription-health.md M-T2:
    # Team 分支也需对称 master probe。母号 cancel 时 Team invite 同样必拿 plan_type=free,
    # 跑完 OAuth 只是堆 plan_drift(实测已观测 28 条)。fail-fast 不进 OAuth 节省 2 分钟。
    # 与 personal 分支差异:Team 已成功 invite,席位占着 → 标 AUTH_INVALID + 主动 kick 释放席位
    # (不能 delete_account,席位还在 Team)。
    try:
        from autoteam.master_health import is_master_subscription_healthy
        team_probe_api = ChatGPTTeamAPI()
        try:
            team_probe_api.start()
            t_healthy, t_master_reason, t_master_evidence = is_master_subscription_healthy(team_probe_api)
        finally:
            try:
                team_probe_api.stop()
            except Exception:
                pass
    except Exception as probe_exc:
        logger.warning("[注册] Team master health probe 异常 %s,按既有逻辑放行", probe_exc)
        t_healthy, t_master_reason, t_master_evidence = True, "active", {"detail": str(probe_exc)[:200]}

    if not t_healthy and t_master_reason == "subscription_cancelled":
        logger.error(
            "[注册] master 母号订阅已取消(eligible_for_auto_reactivation=true),fail-fast 不进 Team OAuth: %s",
            email,
        )
        record_failure(
            email, MASTER_SUBSCRIPTION_DEGRADED,
            "master subscription cancelled,Team OAuth 必拿 plan_type=free",
            stage="run_post_register_oauth_team_precheck",
            master_account_id=t_master_evidence.get("account_id"),
            master_role=t_master_evidence.get("current_user_role"),
        )
        # Team 已 invite,不能 delete_account → 标 AUTH_INVALID + 主动 kick 释放席位
        # (避免 28 条 plan_drift 子号长期占着 Team 名额)
        update_account(
            email,
            status=STATUS_AUTH_INVALID,
            workspace_account_id=get_chatgpt_account_id() or None,
        )
        try:
            cleanup_api = ChatGPTTeamAPI()
            try:
                cleanup_api.start()
                kick_status = remove_from_team(cleanup_api, email, return_status=True)
                logger.info(
                    "[注册] master degraded → kick Team 残留席位 %s status=%s",
                    email, kick_status,
                )
            finally:
                try:
                    cleanup_api.stop()
                except Exception:
                    pass
        except Exception as kick_exc:
            logger.warning("[注册] master degraded 后 kick %s 抛异常(留给下次对账): %s", email, kick_exc)
        _record_outcome("master_degraded", reason="master subscription cancelled (Team)")
        return None

    # 原有 Team 流程 — SPEC-2 §3.1.2 改造:catch RegisterBlocked + plan_supported 检查 + quota probe
    try:
        bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    except RegisterBlocked as blocked:
        if blocked.is_phone:
            logger.error(
                "[注册] %s Team OAuth 触发 add-phone (step=%s),账号已入 Team 席位标 AUTH_INVALID 待 reconcile",
                email, blocked.step,
            )
            record_failure(
                email, "oauth_phone_blocked",
                f"Team OAuth 阶段触发 add-phone (step={blocked.step})",
                step=blocked.step,
                stage="run_post_register_oauth_team",
            )
            # Team 模式下账号已成功 invite,不能 delete_account(席位仍占着);标 AUTH_INVALID 让 reconcile 接管
            update_account(
                email,
                status=STATUS_AUTH_INVALID,
                workspace_account_id=get_chatgpt_account_id() or None,
            )
            _record_outcome("oauth_phone_blocked", reason="OAuth 阶段触发 add-phone")
            return None
        record_failure(email, "exception", f"Team OAuth RegisterBlocked: {blocked.reason}",
                       stage="run_post_register_oauth_team")
        update_account(email, status=STATUS_AUTH_INVALID,
                       workspace_account_id=get_chatgpt_account_id() or None)
        _record_outcome("oauth_failed", reason=f"unexpected RegisterBlocked: {blocked.reason}")
        return None

    if bundle:
        # SPEC-2 shared/plan-type-whitelist §5 — plan_supported=False:account 已入 Team 但无法用,
        # 标 AUTH_INVALID + 保留 auth_file 供调试,reconcile 会按 auth_invalid 流程接管
        plan_supported = bundle.get(
            "plan_supported", is_supported_plan(bundle.get("plan_type", ""))
        )
        auth_file = save_auth_file(bundle)
        bundle_plan = bundle.get("plan_type", "unknown")  # 已被 _exchange_auth_code 归一化为小写

        if not plan_supported:
            logger.error(
                "[注册] %s Team OAuth 拿到 plan_type=%s 不在白名单 → AUTH_INVALID",
                email, bundle.get("plan_type_raw") or bundle_plan,
            )
            record_failure(
                email, "plan_unsupported",
                f"Team OAuth bundle plan_type={bundle.get('plan_type_raw')} 不在白名单",
                plan_type=bundle_plan,
                plan_type_raw=bundle.get("plan_type_raw"),
                stage="run_post_register_oauth_team",
            )
            update_account(
                email,
                status=STATUS_AUTH_INVALID,
                seat_type="codex",
                auth_file=auth_file,
                plan_type_raw=bundle.get("plan_type_raw"),
                workspace_account_id=get_chatgpt_account_id() or None,
            )
            _record_outcome("plan_unsupported", plan=bundle_plan)
            return None

        # SPEC-2 FR-D1~D4 — quota probe(与 manual_account._finalize_account 对称)
        seat_label = "chatgpt" if bundle_plan == "team" else "codex"
        access_token = bundle.get("access_token")
        account_id = bundle.get("account_id")
        update_fields = {
            "status": STATUS_ACTIVE,
            "seat_type": seat_label,
            "auth_file": auth_file,
            "last_active_at": time.time(),
            "workspace_account_id": get_chatgpt_account_id() or None,
            "plan_type_raw": bundle.get("plan_type_raw"),
        }

        if access_token:
            try:
                quota_status, quota_info = check_codex_quota(access_token, account_id=account_id)
            except Exception as exc:
                # SPEC-2 FR-D4: probe 异常吞,降级 ACTIVE 但记一笔
                record_failure(email, "quota_probe_network_error",
                               f"quota probe exception: {exc}",
                               stage="run_post_register_oauth_team")
                quota_status, quota_info = "network_error", None

            if quota_status == "ok" and isinstance(quota_info, dict):
                update_fields["last_quota"] = quota_info
            elif quota_status == "exhausted":
                snapshot = quota_info.get("quota_info") if isinstance(quota_info, dict) else None
                if snapshot:
                    update_fields["last_quota"] = snapshot
                update_fields["status"] = STATUS_EXHAUSTED
                update_fields["quota_exhausted_at"] = time.time()
                update_fields["quota_resets_at"] = (
                    quota_info.get("resets_at") if isinstance(quota_info, dict) else int(time.time() + 18000)
                )
            elif quota_status == "no_quota":
                snapshot = quota_info.get("quota_info") if isinstance(quota_info, dict) else None
                if snapshot:
                    update_fields["last_quota"] = snapshot
                update_fields["status"] = STATUS_AUTH_INVALID
                record_failure(email, "no_quota_assigned",
                               "wham/usage 返回 no_quota(workspace 未分配 codex 配额)",
                               plan_type=bundle_plan,
                               stage="run_post_register_oauth_team",
                               raw_rate_limit=_extract_raw_rate_limit_str(quota_info))
            elif quota_status == "auth_error":
                update_fields["status"] = STATUS_AUTH_INVALID
                record_failure(email, "auth_error_at_oauth",
                               "wham/usage 返回 401/403,token 失效",
                               stage="run_post_register_oauth_team")
            elif quota_status == "network_error":
                # 保留 ACTIVE,等下轮 cmd_check 校准
                record_failure(email, "quota_probe_network_error",
                               "wham/usage 网络异常,ACTIVE 状态由下轮 cmd_check 校准",
                               stage="run_post_register_oauth_team")

        update_account(email, **update_fields)
        if update_fields["status"] == STATUS_ACTIVE:
            logger.info("[注册] 账号就绪: %s (seat=%s)", email, seat_label)
            _record_outcome("success", plan=bundle_plan)
            return email
        else:
            logger.warning("[注册] %s 入池但状态=%s,需要后续处理", email, update_fields["status"])
            _record_outcome("quota_issue", plan=bundle_plan, status=update_fields["status"])
            return None

    # 部分成功：账号已入 Team(席位被占用)但 auth_file 缺失,需要用户手动"补登录"。
    # 上游 cmd_fill 依 `if email: produced+=1` 按席位计数,所以这里仍返回 email;
    # outcome 打 team_auth_missing 让汇总能显示"这批里有 X 个需要补登录"。
    update_account(email, status=STATUS_ACTIVE, workspace_account_id=get_chatgpt_account_id() or None)
    logger.warning("[注册] 账号已加入 Team 但 Codex 登录失败,需要补登录: %s", email)
    _record_outcome("team_auth_missing", reason="已入 Team 席位但 Codex OAuth 未返回 bundle,需要补登录")
    return email


def _complete_registration(email, password, invite_link, mail_client, *, leave_workspace=False, out_outcome=None):
    """完成注册 + Codex 登录（从已有邀请链接继续）。out_outcome 透传给 _run_post_register_oauth。"""
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
            add_account(inv_email, password, workspace_account_id=get_chatgpt_account_id() or None)

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

    add_account(email, password, cloudmail_account_id=account_id, workspace_account_id=get_chatgpt_account_id() or None)

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

    OAuth 失败(bundle=None)或 plan_type != team 时,必须立刻 kick 残留 Team 成员:
    reinvite 链路(invite → OAuth)的 invite 阶段往往已成功,只有 OAuth 这一步掉队,
    如果不 kick,账号就留在 Team 里占席位,本地却写 standby —— 这正是"假 standby"
    的典型成因。不 kick 的话,下一轮 rotate [4/5] 又会从 standby 选中它 reinvite,
    同样失败,死循环占席位。
    """
    email = acc["email"]
    password = acc.get("password", "")

    logger.info("[轮转] 恢复旧账号: %s（统一 OAuth 登录）", email)

    # 关闭 ChatGPT API 浏览器避免冲突
    if chatgpt_api and chatgpt_api.browser:
        chatgpt_api.stop()

    def _cleanup_team_leftover(reason):
        """OAuth 失败/plan 不对时,兜底 kick 账号,避免假 standby。"""
        try:
            if not chatgpt_api.browser:
                chatgpt_api.start()
            kick_status = remove_from_team(chatgpt_api, email, return_status=True)
            if kick_status == "removed":
                logger.info("[轮转] OAuth 失败(%s),已 kick 残留 Team 成员: %s", reason, email)
            elif kick_status == "already_absent":
                logger.info("[轮转] OAuth 失败(%s),确认 %s 不在 Team", reason, email)
            else:
                logger.warning("[轮转] OAuth 失败(%s)后 kick %s 返回 status=%s", reason, email, kick_status)
        except Exception as exc:
            logger.warning("[轮转] OAuth 失败后 kick %s 抛异常(留给下次对账兜底): %s", email, exc)

    try:
        bundle = login_codex_via_browser(email, password, mail_client=mail_client)
    except RegisterBlocked as exc:
        # OAuth 阶段触发 add-phone / 双重验证 / 重复账号风控 — 不可恢复,锁 AUTH_INVALID
        # 而非 STANDBY,避免下一轮 rotate 又选中它死循环。
        logger.warning("[轮转] %s reinvite OAuth 被 add-phone/duplicate 阻断: %s", email, exc)
        _cleanup_team_leftover("oauth_phone_blocked")
        update_account(email, status=STATUS_AUTH_INVALID, auth_file=None)
        try:
            from autoteam.register_failures import record_failure
            record_failure(email, "oauth_phone_blocked", stage="reinvite_account", detail=str(exc))
        except Exception:
            pass
        return False

    if not bundle:
        logger.warning("[轮转] 旧账号 OAuth 登录失败，保持 standby: %s", email)
        _cleanup_team_leftover("no_bundle")
        update_account(email, status=STATUS_STANDBY)
        return False

    plan_type_raw = bundle.get("plan_type_raw") or bundle.get("plan_type") or ""
    plan_type = (bundle.get("plan_type") or "").lower()
    if not is_supported_plan(plan_type_raw):
        # plan_type 不在白名单(self_serve_business_usage_based / enterprise / unknown 等)
        # → 这种账号即使 OAuth 成功也无法稳定调用 Codex,锁 AUTH_INVALID 而非 standby。
        logger.warning("[轮转] %s reinvite 后 plan_type=%s 不被支持,标 AUTH_INVALID", email, plan_type_raw)
        _cleanup_team_leftover(f"plan_unsupported={plan_type_raw}")
        update_account(email, status=STATUS_AUTH_INVALID, auth_file=None)
        try:
            from autoteam.register_failures import record_failure
            record_failure(email, "plan_unsupported", stage="reinvite_account", detail=f"plan_type={plan_type_raw}")
        except Exception:
            pass
        return False

    if plan_type != "team":
        # plan 漂移(白名单内但不是 team,例如 free) — 邀请阶段成功的 Team 席位但 OAuth 拿到的
        # 是个人 plan,反复 reinvite 也不会变 team。锁 AUTH_INVALID 让下游清账,不再死循环。
        logger.warning("[轮转] %s reinvite 后 plan=%s 漂移,不是 team,标 AUTH_INVALID", email, plan_type or "unknown")
        _cleanup_team_leftover(f"plan_drift={plan_type or 'unknown'}")
        update_account(email, status=STATUS_AUTH_INVALID, auth_file=None)
        try:
            from autoteam.register_failures import record_failure
            record_failure(email, "plan_drift", stage="reinvite_account", detail=f"plan_type={plan_type or 'unknown'}")
        except Exception:
            pass
        return False

    auth_file = save_auth_file(bundle)

    # OAuth 成功 plan=team 不等于"账号真的活了"。存在一类竞态:刚 kick 的账号在 OpenAI
    # 端处于 soft-removed/缓存未刷新状态,OAuth 仍能短暂拿到 team workspace token,但配额
    # 本身没被重置(仍是之前耗尽的 5h)。如果不验,就会把 0% 账号塞回 Team,auto-check 下
    # 一轮立刻再 kick,反复洗同一批耗尽账号。这里用新 token 实测一次 wham,只有确认 ok 且
    # 剩余 >= threshold 才算真复用成功;否则判定"假恢复",kick 掉让 Team 席位交给新号。
    access_token = bundle.get("access_token")
    quota_verified = False
    # fake_recovery 的原因要分清:
    #   "exhausted" → quota 真用完,锁 5h 等自然恢复
    #   "auth_error"/"exception" → token 被 OpenAI 风控 revoke(短时间内反复 invite/kick
    #                              触发的) —— 锁 5h 完全没意义,token revoke 不会等就好,
    #                              只能下次重新走完整 OAuth 拿新 token。锁 5h 反而让账号
    #                              无法被任何流程选中,死锁在 standby。
    fail_reason = "no_attempt"
    if access_token:
        try:
            try:
                from autoteam.api import _auto_check_config
                from autoteam.config import AUTO_CHECK_THRESHOLD

                threshold = _auto_check_config.get("threshold", AUTO_CHECK_THRESHOLD)
            except Exception:
                threshold = 10
            status_str, info = check_codex_quota(access_token)
            if status_str == "ok" and isinstance(info, dict):
                # 不论真假恢复,都写一份最新 last_quota:UI 上看到的额度必须是最新事实,
                # 否则用户/下游看到的还是上次成功时的旧值(比如 0% 剩 100%)误判可用,
                # 这正是之前"01544b9745 last_quota.primary_pct=0 但 status=standby 死锁"
                # 的根因(假恢复分支静默吞了实测结果)。
                update_account(email, last_quota=info)
                p_remain = 100 - info.get("primary_pct", 0)
                if p_remain >= threshold:
                    quota_verified = True
                else:
                    fail_reason = "quota_low"
                    logger.warning(
                        "[轮转] %s OAuth 成功但实测 5h 剩余 %d%% < %d%%,判定假恢复",
                        email,
                        p_remain,
                        threshold,
                    )
            elif status_str == "exhausted":
                # exhausted 路径 check_codex_quota 在 info 里塞了 quota_info 子结构,
                # 拆出来写本地 last_quota。
                quota_info = quota_result_quota_info(info) or {}
                if quota_info:
                    update_account(email, last_quota=quota_info)
                fail_reason = "exhausted"
                logger.warning("[轮转] %s OAuth 成功但实测 exhausted,判定假恢复", email)
            elif status_str == "no_quota":
                # 没有发放任何配额(primary_total=0 或 rate_limit 字段全空),不是耗尽。
                # 锁 5h 没意义,直接 AUTH_INVALID 让下游清账,避免反复假恢复。
                fail_reason = "no_quota_assigned"
                logger.warning("[轮转] %s reinvite 后未分配配额(no_quota),判定 AUTH_INVALID", email)
            elif status_str == "network_error":
                # 网络错误不是 token 风控也不是 quota 用尽。当作"未验证",走 exception 分支
                # 同款处置:不锁 5h(token 还活着),让下一轮自然重试。
                fail_reason = "network_error"
                logger.warning("[轮转] %s 额度验证遇到临时网络错误,本轮判定未验证", email)
            else:
                # auth_error/其他 — token 风控类,wham 401 token_revoked 落这里
                fail_reason = "auth_error"
                logger.warning("[轮转] %s OAuth 成功但额度验证返回 status=%s,判定 token 风控", email, status_str)
        except Exception as exc:
            fail_reason = "exception"
            logger.warning("[轮转] %s 额度验证抛异常,判定 token 风控: %s", email, exc)

    if not quota_verified:
        # 把这个"假恢复"的账号从 Team 里 kick 掉,避免占席位
        _cleanup_team_leftover(f"fake_recovery_{fail_reason}")
        now_ts = time.time()
        if fail_reason in ("exhausted", "quota_low"):
            # 真的 quota 不足 → 锁 5h 等自然恢复
            update_account(
                email,
                status=STATUS_STANDBY,
                auth_file=auth_file,
                quota_exhausted_at=now_ts,
                quota_resets_at=now_ts + 18000,
            )
        elif fail_reason == "no_quota_assigned":
            # 后端没发配额 —— 不是耗尽,锁 5h 没用,锁 AUTH_INVALID 让下游清账。
            update_account(
                email,
                status=STATUS_AUTH_INVALID,
                auth_file=None,
                quota_exhausted_at=None,
                quota_resets_at=None,
            )
            try:
                from autoteam.register_failures import record_failure
                record_failure(email, "no_quota_assigned", stage="reinvite_account",
                               detail="primary_total=0 or rate_limit_empty",
                               raw_rate_limit=_extract_raw_rate_limit_str(info))
            except Exception:
                pass
        else:
            # token 风控/异常 —— 锁 5h 没用,token revoke 等不来。降级到 standby 但
            # 不写 quota_exhausted_at/resets_at,让下次有机会重新尝试 OAuth(说不定
            # 风控窗口已过)。同时清掉旧 last_quota 里的"剩余 100%"幻觉,免得下游
            # 看着 last_quota 把它当可用号反复选中。
            update_account(
                email,
                status=STATUS_STANDBY,
                auth_file=auth_file,
                quota_exhausted_at=None,
                quota_resets_at=None,
            )
        return False

    update_account(
        email,
        status=STATUS_ACTIVE,
        last_active_at=time.time(),
        auth_file=auth_file,
        workspace_account_id=get_chatgpt_account_id() or None,
    )
    logger.info("[轮转] 旧账号已恢复: %s", email)
    return True


def _replace_single(chatgpt, mail_client, email, reason=""):
    """定点替换一个失效子号(内部实现,复用外部传入的 chatgpt_api + mail_client)。

    流程:kick 目标 → 补一个(优先 standby 复用,否则新号)。补位后若 Team 子号已达
    TEAM_SUB_ACCOUNT_HARD_CAP 则停止,不会超员。

    返回 dict: {kicked: bool, filled_by: email|None, method: "reuse"|"new"|None, error: str|None}
    """
    outcome = {"kicked": False, "filled_by": None, "method": None, "error": None}

    if _is_main_account_email(email):
        outcome["error"] = "skip_main"
        logger.warning("[替换] 跳过主号: %s", email)
        return outcome

    # 1. kick 失效账号(新版带 retry,already_absent 也算成功)
    logger.info("[替换] kick %s (reason=%s)", email, reason or "unspecified")
    try:
        kick_status = remove_from_team(chatgpt, email, return_status=True)
    except Exception as exc:
        outcome["error"] = f"kick_exception: {exc}"
        logger.error("[替换] kick %s 抛异常: %s", email, exc)
        return outcome
    if kick_status not in ("removed", "already_absent"):
        outcome["error"] = f"kick_failed: {kick_status}"
        logger.error("[替换] kick %s 失败 status=%s,不补位", email, kick_status)
        return outcome
    outcome["kicked"] = True
    update_account(email, status=STATUS_STANDBY)

    # 2. 确认当前 Team 非主号子号数,判断是否还有空位
    try:
        current_total = get_team_member_count(chatgpt)
    except Exception as exc:
        logger.warning("[替换] 获取 Team 成员数抛异常: %s,跳过补位", exc)
        outcome["error"] = f"count_exception: {exc}"
        return outcome
    if current_total < 0:
        outcome["error"] = "count_failed"
        return outcome
    sub_count = current_total - 1  # 减主号
    if sub_count >= TEAM_SUB_ACCOUNT_HARD_CAP:
        logger.info("[替换] Team 子号已达 %d/%d,无需补位", sub_count, TEAM_SUB_ACCOUNT_HARD_CAP)
        return outcome

    # 3. 优先从 standby 复用,排除刚 kick 的同一 email 防止自环
    email_lc = (email or "").lower()
    standby_list = [
        a
        for a in get_standby_accounts()
        if a.get("_quota_recovered")
        and not _is_main_account_email(a.get("email"))
        and (a.get("email") or "").lower() != email_lc
    ]
    for acc in standby_list:
        skip_reason = _auto_reuse_skip_reason(acc)
        if skip_reason:
            logger.info("[替换] 跳过 %s(%s)", acc.get("email"), skip_reason)
            continue
        cand_email = acc.get("email")

        # 额度二次验证:不能只信 get_standby_accounts() 的 _quota_recovered(它只看
        # quota_resets_at 这种粗估时间)。之前有 bug 就是把还在 exhausted 窗口的
        # standby 反复 reinvite 进 Team,账号一进来就 0% 立马被 kick,把同一批号
        # 来回洗,席位始终干空。这里直接拿 auth_file 的 access_token 打一次 wham,
        # 只有 API 确认 "ok 且剩余 >= threshold" 才允许复用。
        try:
            from autoteam.config import AUTO_CHECK_THRESHOLD

            try:
                from autoteam.api import _auto_check_config

                threshold = _auto_check_config.get("threshold", AUTO_CHECK_THRESHOLD)
            except ImportError:
                threshold = AUTO_CHECK_THRESHOLD
        except Exception:
            threshold = 10

        auth_file = acc.get("auth_file")
        quota_ok = False
        if auth_file and Path(auth_file).exists():
            try:
                auth_data = json.loads(read_text(Path(auth_file)))
                access_token = auth_data.get("access_token")
                if access_token:
                    status_str, info = check_codex_quota(access_token)
                    if status_str == "ok" and isinstance(info, dict):
                        # 实测结果统一刷新 last_quota,避免 UI/下游看到陈旧数据
                        update_account(cand_email, last_quota=info)
                        p_remain = 100 - info.get("primary_pct", 0)
                        if p_remain >= threshold:
                            quota_ok = True
                        else:
                            logger.info("[替换] 跳过 %s(实测 5h 剩余 %d%% < %d%%)", cand_email, p_remain, threshold)
                            continue
                    elif status_str == "exhausted":
                        quota_info = quota_result_quota_info(info) or {}
                        if quota_info:
                            update_account(cand_email, last_quota=quota_info)
                        logger.info("[替换] 跳过 %s(实测 exhausted)", cand_email)
                        continue
                    # auth_error:token 失效,不是"额度真恢复"的证据,跳过
                    elif status_str == "auth_error":
                        logger.info("[替换] 跳过 %s(token auth_error,无法验证额度)", cand_email)
                        continue
                    # network_error:临时网络故障,不能当"额度恢复"凭证,本轮不复用,
                    # 等下一轮再试(不动 acc 状态)
                    elif status_str == "network_error":
                        logger.info("[替换] 跳过 %s(临时网络错误,本轮无法验证额度)", cand_email)
                        continue
            except Exception as exc:
                logger.info("[替换] %s 额度验证抛异常(跳过): %s", cand_email, exc)
                continue
        if not quota_ok:
            # 没 auth_file 或验证没通过都跳过,宁可去创建新号也别把 0% 账号塞回 Team
            logger.info("[替换] 跳过 %s(无 auth_file 或额度未通过验证)", cand_email)
            continue

        logger.info("[替换] 尝试复用 standby: %s", cand_email)
        if not chatgpt.browser:
            chatgpt.start()
        if reinvite_account(chatgpt, mail_client, acc):
            outcome["filled_by"] = cand_email
            outcome["method"] = "reuse"
            logger.info("[替换] 补位成功(复用): %s → %s", email, cand_email)
            return outcome
        # reinvite_account 内部失败已 cleanup,继续下一个候选

    # 4. 无可复用 standby → 创建新号
    logger.info("[替换] 无可复用 standby,创建新号补位...")
    if not chatgpt.browser:
        chatgpt.start()
    try:
        new_email = create_new_account(chatgpt, mail_client)
    except Exception as exc:
        outcome["error"] = f"create_exception: {exc}"
        logger.error("[替换] 创建新号抛异常: %s", exc)
        return outcome
    if new_email:
        outcome["filled_by"] = new_email
        outcome["method"] = "new"
        logger.info("[替换] 补位成功(新号): %s → %s", email, new_email)
    else:
        outcome["error"] = "create_failed"
        logger.error("[替换] 新号创建失败,席位暂缺")
    return outcome


def cmd_replace_one(email, reason=""):
    """立即替换一个失效 Team 子号(外部入口,自建 chatgpt + mail)。

    相比 cmd_rotate 全量走一遍 check + 批量补位,这里只针对单个席位做 kick+补一个,
    响应更快。适合 auto-check 巡检发现失效立即逐个替换的场景。
    """
    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()
    mail_client = CloudMailClient()
    mail_client.login()
    try:
        return _replace_single(chatgpt, mail_client, email, reason=reason)
    finally:
        if chatgpt.browser:
            chatgpt.stop()
        try:
            sync_to_cpa()
        except Exception as exc:
            logger.warning("[替换] sync_to_cpa 抛异常(忽略): %s", exc)


def cmd_replace_batch(emails, trigger=""):
    """批量立即替换:逐个 kick+补一个,复用同一个 ChatGPT/mail 实例(省浏览器启停)。

    串行执行,失败不阻塞后续。返回 outcome 列表。
    用于 auto-check 同轮发现多个失效时一次性处理。
    """
    if not emails:
        return []
    chatgpt = ChatGPTTeamAPI()
    chatgpt.start()
    mail_client = CloudMailClient()
    mail_client.login()
    outcomes = []
    try:
        for email in emails:
            try:
                if not chatgpt.browser:
                    chatgpt.start()
                out = _replace_single(chatgpt, mail_client, email, reason=trigger or "batch")
                outcomes.append({"email": email, **out})
            except Exception as exc:
                logger.error("[替换] %s 单个替换抛异常: %s", email, exc)
                outcomes.append({"email": email, "kicked": False, "filled_by": None, "error": f"exception: {exc}"})
    finally:
        if chatgpt.browser:
            chatgpt.stop()
        try:
            sync_to_cpa()
        except Exception as exc:
            logger.warning("[替换] sync_to_cpa 抛异常(忽略): %s", exc)

    ok = sum(1 for o in outcomes if o.get("filled_by"))
    logger.info("[替换] 批量完成 %d/%d 个补位成功(trigger=%s)", ok, len(outcomes), trigger or "-")
    return outcomes


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
                        # network_error: 临时网络故障,不能当"额度已恢复"凭证。本轮跳过,
                        # 不动 acc 状态,等下一轮再试。
                        if status_str == "network_error":
                            logger.info("[4/5] 跳过 %s（临时网络错误,本轮无法验证额度）", email)
                            quota_skipped.append(acc)
                            continue
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

    # 队列化拒绝(Solution C):Team 子号已满 TEAM_SUB_ACCOUNT_HARD_CAP 时直接拒绝,
    # 不强制踢健康账号腾席位。这样最小化风控暴露面 —— 只在自然 exhausted 或手动腾位置
    # 后才生产免费号。
    cap = TEAM_SUB_ACCOUNT_HARD_CAP
    if len(baseline_emails) >= cap:
        logger.warning(
            "[免费号] Team 子号已满 %d/%d,fill-personal 拒绝执行。"
            "请先等子号自然 exhausted 释放席位,或手动 kick/ replace 腾位置后再试。",
            len(baseline_emails),
            cap,
        )
        return
    # 把本轮目标压到 (cap - baseline) 以内,防止任何批次超员
    quota_for_run = cap - len(baseline_emails)
    if count > quota_for_run:
        logger.warning(
            "[免费号] 目标 %d 超过当前可用席位 %d (Team 已占 %d/%d),自动压到 %d 个",
            count,
            quota_for_run,
            len(baseline_emails),
            cap,
            quota_for_run,
        )
        count = quota_for_run

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
            # Team 席位总上限 TEAM_SUB_ACCOUNT_HARD_CAP(4):baseline 已占了一部分,
            # 本批最多再加 (cap - baseline) 个,严格不超员。若 baseline 已占满,
            # 入口处已经拒绝并 return,这里不会走到。
            max_new_this_batch = TEAM_SUB_ACCOUNT_HARD_CAP - len(baseline_emails)
            this_round = min(BATCH_SIZE, remaining, max_new_this_batch)
            if this_round <= 0:
                logger.warning(
                    "[免费号] 第 %d 批可用席位已耗尽(baseline %d/%d),停止生产",
                    batch_idx,
                    len(baseline_emails),
                    TEAM_SUB_ACCOUNT_HARD_CAP,
                )
                break
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

        # === 末批兜底清理 ===
        # 即使每个子号内部的 remove_from_team 报告成功,OpenAI 的 /users API
        # 对新加入成员存在同步延迟,首次 GET 可能没列出该成员 → 代码误判 already_absent
        # 直接跳过 DELETE。结果:账号本地 status=PERSONAL 认证也拿到了,但 Team 席位里
        # 还挂着 Member(截图里用户看到的正是这种情况)。
        # 不信任内部 kick 报告,以 Team 真实成员列表为权威,强清所有不在 baseline 的新号。
        # 即使某些账号已被踢成功,DELETE 一个不存在的 user_id 只会返回 4xx,副作用可控。
        try:
            api_final = _ensure_chatgpt()
            ok_final, current_non_master = _fetch_team_non_master_emails(api_final)
            if not ok_final:
                logger.warning("[免费号] 末批兜底:无法拉取 Team 成员列表,跳过强制清理")
            else:
                stragglers = sorted(current_non_master - baseline_emails)
                if not stragglers:
                    logger.info(
                        "[免费号] 末批兜底:Team 已回到 baseline(%d 个非主号成员),无需清理",
                        len(baseline_emails),
                    )
                else:
                    logger.warning(
                        "[免费号] 末批兜底:Team 仍残留 %d 个新号未被踢出,强制清理: %s",
                        len(stragglers),
                        stragglers[:10],
                    )
                    cleaned = 0
                    for stray_email in stragglers:
                        try:
                            st = remove_from_team(api_final, stray_email, return_status=True, lookup_retries=1)
                            logger.info("[免费号] 末批兜底 kick %s → %s", stray_email, st)
                            if st == "removed":
                                cleaned += 1
                        except Exception as exc:
                            logger.error("[免费号] 末批兜底 kick %s 抛异常: %s", stray_email, exc)
                    logger.info(
                        "[免费号] 末批兜底清理完成:实际移除 %d / %d 个,剩余由用户手动处理",
                        cleaned,
                        len(stragglers),
                    )
        except Exception as exc:
            logger.error("[免费号] 末批兜底清理出错(不影响已生产账号): %s", exc)
        finally:
            _stop_chatgpt()
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


def _reconcile_master_degraded_subaccounts(*, dry_run: bool = False):
    """Round 8 — SPEC-2 v1.5 §3.5.3:reconcile retroactive 清理。

    若母号订阅已降级(subscription_cancelled),将本地 ACTIVE/EXHAUSTED 账号中
    workspace_account_id 等于当前(已降级)母号 account_id 的子号统一标 STANDBY,
    避免 health-check 把 plan=team 的"已脱身"账号误派出去。

    条件:
        master_health: subscription_cancelled
        account.status in (ACTIVE, EXHAUSTED, AUTH_INVALID) and
        account.workspace_account_id == current_master_account_id

    Returns: dict { "degraded_marked": [emails], "skipped_reason": str|None }
    """
    from autoteam.master_health import is_master_subscription_healthy

    out = {"degraded_marked": [], "skipped_reason": None}
    try:
        temp_api = ChatGPTTeamAPI()
        try:
            temp_api.start()
            healthy, reason, evidence = is_master_subscription_healthy(temp_api)
        finally:
            try:
                temp_api.stop()
            except Exception:
                pass
    except Exception as exc:
        out["skipped_reason"] = f"probe_exception:{exc}"
        return out

    if healthy or reason != "subscription_cancelled":
        out["skipped_reason"] = f"master healthy(reason={reason}),无需 retroactive cleanup"
        return out

    master_aid = (evidence or {}).get("account_id") or get_chatgpt_account_id()
    if not master_aid:
        out["skipped_reason"] = "无法解析当前母号 account_id"
        return out

    candidates = []
    for acc in load_accounts():
        if acc.get("status") not in (STATUS_ACTIVE, STATUS_EXHAUSTED, STATUS_AUTH_INVALID):
            continue
        if (acc.get("workspace_account_id") or "") != master_aid:
            continue
        candidates.append(acc.get("email"))

    logger.info(
        "[对账-retroactive] master degraded(reason=%s),候选 standby 子号 %d 个: %s",
        reason, len(candidates), candidates,
    )

    if dry_run:
        out["degraded_marked"] = candidates
        out["skipped_reason"] = "dry_run"
        return out

    for em in candidates:
        try:
            update_account(em, status=STATUS_STANDBY)
            record_failure(
                em, MASTER_SUBSCRIPTION_DEGRADED,
                "reconcile retroactive cleanup:母号 cancelled,子号统一 standby",
                stage="reconcile_retroactive",
                master_account_id=master_aid,
            )
            out["degraded_marked"].append(em)
        except Exception as exc:
            logger.warning("[对账-retroactive] 标 standby 失败 email=%s err=%s", em, exc)

    return out


def cmd_reconcile(dry_run: bool = False):
    """独立运行一次对账,修正残废 / 错位 / 耗尽未抛弃 / ghost 成员。

    与 cmd_check 内部的 `_reconcile_team_members` 共享同一套逻辑,但:
    - 不做额度检查、不触发 Codex 登录,纯做状态对齐
    - 入口日志更友好,返回结构化 result 便于 API / 脚本消费
    - dry_run=True 等价于 cmd_reconcile_dry_run,只输出诊断不动账户

    Round 8 — 在常规 reconcile 后额外做 master-degraded retroactive cleanup。
    """
    logger.info("[对账] 开始独立对账 dry_run=%s", dry_run)
    recon = _reconcile_team_members(dry_run=dry_run)

    # 汇总日志,方便看出哪些分支命中
    summary_keys = [
        "kicked",
        "flipped_to_active",
        "orphan_kicked",
        "orphan_marked",
        "misaligned_fixed",
        "exhausted_marked",
        "ghost_kicked",
        "ghost_seen",
        "over_cap_kicked",
    ]
    parts = [f"{k}={len(recon.get(k) or [])}" for k in summary_keys]
    logger.info("[对账] %s结果: %s", "(dry-run)" if dry_run else "", ", ".join(parts))

    # 具体列表只在非空时打,避免日志噪声
    for k in summary_keys:
        items = recon.get(k) or []
        if items:
            logger.info("[对账] %s → %s", k, items)

    # Round 8 — retroactive cleanup
    try:
        retro = _reconcile_master_degraded_subaccounts(dry_run=dry_run)
        recon["master_degraded_retroactive"] = retro
        if retro.get("degraded_marked"):
            logger.info(
                "[对账-retroactive] %s标 standby %d 个: %s",
                "(dry-run) " if dry_run else "",
                len(retro["degraded_marked"]),
                retro["degraded_marked"],
            )
    except Exception as exc:
        logger.warning("[对账-retroactive] 异常未阻塞主对账: %s", exc)
        recon["master_degraded_retroactive"] = {
            "degraded_marked": [], "skipped_reason": f"exception:{exc}",
        }

    return recon


def cmd_reconcile_dry_run():
    """诊断模式:只输出报告,不 kick 任何账号、不写 accounts.json。"""
    return cmd_reconcile(dry_run=True)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="manager.py",
        description="ChatGPT Team 账号轮转管理器",
    )
    sub = parser.add_subparsers(dest="command", help="可用命令")

    sub.add_parser("status", help="查看所有账号状态")
    check_p = sub.add_parser("check", help="检查活跃账号 Codex 额度")
    check_p.add_argument(
        "--include-standby",
        action="store_true",
        help="同时探测 standby 池的 quota(限速+24h 去重,会对每个 standby 账号打一次 wham/usage)",
    )
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

    reconcile_p = sub.add_parser(
        "reconcile",
        help="对账 Team 实际成员 vs 本地状态,修复残废 / 错位 / 耗尽未抛弃 / ghost",
    )
    reconcile_p.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出诊断报告,不 kick、不改 accounts.json",
    )

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
        cmd_check(include_standby=getattr(args, "include_standby", False))
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
    elif args.command == "reconcile":
        cmd_reconcile(dry_run=getattr(args, "dry_run", False))
    elif args.command == "api":
        from autoteam.api import start_server

        start_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
