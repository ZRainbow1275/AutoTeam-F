"""账号资源清理与远端对账操作。"""

import logging
from pathlib import Path

from autoteam.accounts import STATUS_AUTH_INVALID, STATUS_PERSONAL, find_account, load_accounts, save_accounts
from autoteam.admin_state import get_chatgpt_account_id
from autoteam.cloudmail import CloudMailClient
from autoteam.cpa_sync import delete_from_cpa, list_cpa_files, sync_to_cpa

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "auths"


def fetch_team_state(chatgpt_api):
    """读取 Team 成员和邀请状态。"""
    account_id = get_chatgpt_account_id()
    members = []
    invites = []

    users_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/users")
    if users_resp["status"] == 200:
        import json

        data = json.loads(users_resp["body"])
        members = data.get("items", data.get("users", data.get("members", [])))

    invites_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/invites")
    if invites_resp["status"] == 200:
        import json

        data = json.loads(invites_resp["body"])
        invites = data if isinstance(data, list) else data.get("invites", data.get("account_invites", []))

    return members, invites


def delete_managed_account(
    email,
    *,
    remove_remote=True,
    remove_cloudmail=True,
    sync_cpa_after=True,
    chatgpt_api=None,
    mail_client=None,
    remote_state=None,
):
    """
    删除本地管理账号及其衍生资源。
    返回 cleanup 摘要，设计为幂等操作。
    """
    email_l = email.lower()
    accounts = load_accounts()
    acc = find_account(accounts, email)

    cleanup = {
        "local_record": False,
        "local_auth_files": [],
        "cpa_files": [],
        "team_member_removed": False,
        "invite_removed": False,
        "cloudmail_deleted": False,
    }

    members = []
    invites = []
    own_chatgpt = None
    own_mail_client = None

    try:
        account_id = get_chatgpt_account_id()
        # SPEC-2 FR-H1 (issue #2 独属) + Round 6 PRD-5 FR-P1.2:personal / auth_invalid 账号
        # 删除时不需要拉 remote_state(members/invites)。auth_invalid 的 token 已失效,继续走
        # ChatGPTTeamAPI 会在 wham/usage 401 时拖累整个删除链路;主号 session 失效场景下,
        # 启动 ChatGPTTeamAPI 还会卡死 30s。这条短路同时规避两类资源浪费,纯本地操作。
        # 注意:变量名仍保留 is_personal 以最小化下游契约变化,但语义已扩到"本地清理即可"。
        is_personal = bool(acc and acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID))
        skip_remote = is_personal
        if remove_remote and not skip_remote:
            if remote_state is not None:
                members, invites = remote_state
            else:
                if chatgpt_api is None:
                    from autoteam.chatgpt_api import ChatGPTTeamAPI

                    own_chatgpt = ChatGPTTeamAPI()
                    own_chatgpt.start()
                    chatgpt_api = own_chatgpt
                members, invites = fetch_team_state(chatgpt_api)

            member_matches = [m for m in members if (m.get("email", "") or "").lower() == email_l]
            for member in member_matches:
                user_id = member.get("user_id") or member.get("id")
                if not user_id:
                    continue
                result = chatgpt_api._api_fetch(
                    "DELETE",
                    f"/backend-api/accounts/{account_id}/users/{user_id}",
                )
                if result["status"] not in (200, 204):
                    raise RuntimeError(f"移除 Team 成员失败: {email}")
                cleanup["team_member_removed"] = True

            invite_matches = []
            for inv in invites:
                inv_email = (inv.get("email_address") or inv.get("email") or "").lower()
                if inv_email == email_l:
                    invite_matches.append(inv)

            for inv in invite_matches:
                invite_id = inv.get("id")
                if not invite_id:
                    continue
                result = chatgpt_api._api_fetch(
                    "DELETE",
                    f"/backend-api/accounts/{account_id}/invites/{invite_id}",
                )
                if result["status"] not in (200, 204):
                    raise RuntimeError(f"取消 Team 邀请失败: {email}")
                cleanup["invite_removed"] = True

        auth_candidates = set()
        if acc and acc.get("auth_file"):
            auth_candidates.add(Path(acc["auth_file"]))
        auth_candidates.update(AUTH_DIR.glob(f"codex-{email}-*.json"))

        for path in sorted(auth_candidates):
            if path.exists():
                path.unlink()
                cleanup["local_auth_files"].append(path.name)
                logger.info("[账号] 已删除本地 auth: %s", path.name)

        cpa_names = set(cleanup["local_auth_files"])
        for item in list_cpa_files():
            item_email = (item.get("email") or "").lower()
            item_name = item.get("name") or ""
            if item_email == email_l or item_name in cpa_names:
                if delete_from_cpa(item_name):
                    cleanup["cpa_files"].append(item_name)

        if acc:
            accounts = [item for item in accounts if item["email"].lower() != email_l]
            save_accounts(accounts)
            cleanup["local_record"] = True
            logger.info("[账号] 已删除本地记录: %s", email)

            cloudmail_account_id = acc.get("cloudmail_account_id")
            if remove_cloudmail and cloudmail_account_id:
                try:
                    if mail_client is None:
                        own_mail_client = CloudMailClient()
                        own_mail_client.login()
                        mail_client = own_mail_client
                    resp = mail_client.delete_account(cloudmail_account_id)
                    if resp.get("code") == 200:
                        cleanup["cloudmail_deleted"] = True
                except Exception as exc:
                    logger.warning("[账号] 删除 CloudMail 账户失败: %s", exc)

        if sync_cpa_after:
            sync_to_cpa()

        return cleanup
    finally:
        if own_chatgpt:
            own_chatgpt.stop()
