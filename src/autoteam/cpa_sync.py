"""CPA (CLIProxyAPI) 认证文件同步 - 保持本地 codex 认证文件与 CPA 一致"""

import logging
from pathlib import Path

import requests

from autoteam.config import CPA_KEY, CPA_URL

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "auths"


def _headers():
    return {"Authorization": f"Bearer {CPA_KEY}"}


def list_cpa_files():
    """获取 CPA 中所有认证文件"""
    resp = requests.get(f"{CPA_URL}/v0/management/auth-files", headers=_headers(), timeout=10)
    if resp.status_code != 200:
        logger.error("[CPA] 获取文件列表失败: %d", resp.status_code)
        return []
    data = resp.json()
    return data.get("files", [])


def upload_to_cpa(filepath):
    """上传认证文件到 CPA"""
    filepath = Path(filepath)
    if not filepath.exists():
        logger.warning("[CPA] 文件不存在: %s", filepath)
        return False

    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{CPA_URL}/v0/management/auth-files",
            headers=_headers(),
            files={"file": (filepath.name, f, "application/json")},
            timeout=10,
        )

    if resp.status_code == 200:
        logger.info("[CPA] 已上传: %s", filepath.name)
        return True
    else:
        logger.error("[CPA] 上传失败: %d %s", resp.status_code, resp.text[:200])
        return False


def delete_from_cpa(name):
    """从 CPA 删除认证文件"""
    resp = requests.delete(
        f"{CPA_URL}/v0/management/auth-files",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        logger.info("[CPA] 已删除: %s", name)
        return True
    else:
        logger.error("[CPA] 删除失败: %d %s", resp.status_code, resp.text[:200])
        return False


def sync_to_cpa():
    """
    同步本地认证文件到 CPA，只同步 active 状态的账号。
    - active 且 CPA 没有 → 上传
    - CPA 有但不是 active（或本地已删除）→ 从 CPA 删除
    """
    from autoteam.accounts import STATUS_ACTIVE, load_accounts, save_accounts

    accounts = load_accounts()
    local_emails = {a["email"].lower() for a in accounts}

    # 修复断裂的 auth_file 路径
    changed = False
    for acc in accounts:
        auth_path = acc.get("auth_file")
        if auth_path and not Path(auth_path).exists():
            matches = list(AUTH_DIR.glob(f"codex-{acc['email']}-*.json"))
            if matches:
                acc["auth_file"] = str(matches[0].resolve())
                changed = True
    if changed:
        save_accounts(accounts)

    # active 账号的认证文件
    active_files = {}
    for acc in accounts:
        if acc["status"] == STATUS_ACTIVE and acc.get("auth_file"):
            path = Path(acc["auth_file"])
            if path.exists():
                active_files[path.name] = path

    # CPA 认证文件
    cpa_files = list_cpa_files()
    cpa_names = {f["name"]: f for f in cpa_files}

    logger.info("[CPA] active 认证文件: %d, CPA 认证文件: %d", len(active_files), len(cpa_files))

    # 上传：所有 active 认证文件（覆盖同名文件，确保 token 最新）
    uploaded = 0
    for name, path in active_files.items():
        logger.info("[CPA] 上传: %s", name)
        if upload_to_cpa(path):
            uploaded += 1

    # 删除：CPA 中有但不在 active 列表的（仅限本地管理的账号）
    deleted = 0
    for name, cpa_file in cpa_names.items():
        email = cpa_file.get("email", "").lower()
        if email in local_emails and name not in active_files:
            logger.info("[CPA] 删除非 active 文件: %s (%s)", name, email)
            if delete_from_cpa(name):
                deleted += 1

    logger.info("[CPA] 同步完成: 上传 %d, 删除 %d", uploaded, deleted)

    # 最终状态
    final_cpa = list_cpa_files()
    final_local_managed = [f for f in final_cpa if f.get("email", "").lower() in local_emails]
    logger.info("[CPA] CPA 中本地管理: %d, 本地 active: %d", len(final_local_managed), len(active_files))


def sync_main_codex_to_cpa(filepath):
    """同步主号 Codex 认证文件到 CPA。"""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"主号认证文件不存在: {filepath}")

    name = filepath.name
    existing = {item.get("name"): item for item in list_cpa_files()}

    for old_name in existing:
        if old_name and old_name.startswith("codex-main-"):
            logger.info("[CPA] 删除旧主号文件: %s", old_name)
            delete_from_cpa(old_name)

    if not upload_to_cpa(filepath):
        raise RuntimeError(f"上传主号认证文件失败: {name}")

    logger.info("[CPA] 主号 Codex 已同步: %s", name)
    return {"uploaded": name}
