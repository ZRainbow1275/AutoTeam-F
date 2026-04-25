"""账号池管理 - 持久化存储所有账号状态"""

import json
import time
from pathlib import Path

from autoteam.admin_state import get_admin_email
from autoteam.textio import read_text, write_text

PROJECT_ROOT = Path(__file__).parent.parent.parent
ACCOUNTS_FILE = PROJECT_ROOT / "accounts.json"

# 账号状态
STATUS_ACTIVE = "active"  # 在 team 中，额度可用
STATUS_EXHAUSTED = "exhausted"  # 在 team 中，额度用完
STATUS_STANDBY = "standby"  # 已移出 team，等待额度恢复
STATUS_PENDING = "pending"  # 已邀请，等待注册完成
STATUS_PERSONAL = "personal"  # 已主动退出 team，走个人号 Codex OAuth，不再参与 Team 轮转
STATUS_AUTH_INVALID = "auth_invalid"  # auth_file token 已不可用(401/403),待 reconcile 清理或重登
STATUS_ORPHAN = "orphan"  # 在 workspace 里占着席位,但本地没 auth_file(残废,待人工介入或兜底 kick)

# 席位类型:标记该账号在 ChatGPT Team 里被授予的席位种类,用于下游 fill / check 区分对待
SEAT_CHATGPT = "chatgpt"  # 完整 ChatGPT 席位(PATCH invite seat_type=default 成功)
SEAT_CODEX = "codex"  # 仅 Codex 席位(usage_based,PATCH 改 default 失败时保留的兜底)
SEAT_UNKNOWN = "unknown"  # 未知/未记录,老账号或手动导入默认值


def _normalized_email(value):
    return (value or "").strip().lower()


def _is_main_account_email(email):
    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


def load_accounts():
    """加载账号列表"""
    if ACCOUNTS_FILE.exists():
        text = read_text(ACCOUNTS_FILE).strip()
        if text:
            return json.loads(text)
    return []


def save_accounts(accounts):
    """保存账号列表"""
    write_text(ACCOUNTS_FILE, json.dumps(accounts, indent=2, ensure_ascii=False))


def find_account(accounts, email):
    """按邮箱查找账号"""
    for acc in accounts:
        if acc["email"] == email:
            return acc
    return None


def add_account(email, password, cloudmail_account_id=None, seat_type=SEAT_UNKNOWN):
    """添加新账号。seat_type 取值见 SEAT_CHATGPT / SEAT_CODEX / SEAT_UNKNOWN。"""
    accounts = load_accounts()
    if find_account(accounts, email):
        # 已存在仍允许补写 seat_type,避免旧记录一直是 unknown
        if seat_type and seat_type != SEAT_UNKNOWN:
            update_account(email, seat_type=seat_type)
        return

    accounts.append(
        {
            "email": email,
            "password": password,
            "cloudmail_account_id": cloudmail_account_id,
            "status": STATUS_PENDING,
            "seat_type": seat_type or SEAT_UNKNOWN,
            "auth_file": None,  # CPA 认证文件路径
            "quota_exhausted_at": None,  # 额度用完的时间
            "quota_resets_at": None,  # 额度恢复时间
            "last_quota_check_at": None,  # 最近一次 wham/usage 探测时间戳,用于 standby 探测去重
            "created_at": time.time(),
            "last_active_at": None,
        }
    )
    save_accounts(accounts)


def update_account(email, **kwargs):
    """更新账号字段"""
    accounts = load_accounts()
    acc = find_account(accounts, email)
    if acc:
        acc.update(kwargs)
        save_accounts(accounts)
    return acc


def delete_account(email):
    """从账号池彻底移除（不动认证文件、不动 CloudMail 邮箱）。返回是否真的删除了记录。"""
    accounts = load_accounts()
    remaining = [a for a in accounts if a.get("email") != email]
    if len(remaining) == len(accounts):
        return False
    save_accounts(remaining)
    return True


def get_active_accounts():
    """获取所有活跃账号"""
    return [a for a in load_accounts() if a["status"] == STATUS_ACTIVE and not _is_main_account_email(a.get("email"))]


def get_personal_accounts():
    """获取所有已退出 Team、走个人 Codex 授权的账号（不参与席位轮转）"""
    return [a for a in load_accounts() if a["status"] == STATUS_PERSONAL and not _is_main_account_email(a.get("email"))]


def get_standby_accounts():
    """获取所有待命账号（已移出 team，可能额度已恢复）"""
    accounts = load_accounts()
    now = time.time()
    standby = []
    for a in accounts:
        if _is_main_account_email(a.get("email")):
            continue
        if a["status"] == STATUS_STANDBY:
            resets_at = a.get("quota_resets_at")
            if resets_at is None:
                # 没有恢复时间 = 不是因为额度用完被移出的，随时可复用
                a["_quota_recovered"] = True
            else:
                # 有恢复时间，看是否已过
                a["_quota_recovered"] = now >= resets_at
            standby.append(a)
    # 已恢复的排前面
    standby.sort(key=lambda x: (not x.get("_quota_recovered", False), x.get("quota_exhausted_at") or 0))
    return standby


def get_next_reusable_account():
    """获取下一个可重用的 standby 账号（优先额度已恢复的）"""
    standby = get_standby_accounts()
    if standby:
        return standby[0]
    return None
