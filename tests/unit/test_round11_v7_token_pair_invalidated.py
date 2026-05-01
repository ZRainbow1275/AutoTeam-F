"""Round 11 V7 — 双失效探活 helper + cmd_check 集成测试。

研究报告 v7-v10 §V7 确认:OpenAI 在 user kick 时同步 invalidate access_token
(`token_invalidated` 401)+ refresh_token(`refresh_token_invalidated` 401)。
本套测试覆盖:
- is_token_pair_invalidated 在不同 access_token / refresh_token 状态组合下的判定
- cmd_check 重登入口前的"双死预筛"分支:命中即跳过 Playwright OAuth + 标 AUTH_INVALID + stamp 时间戳
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from autoteam import codex_auth

# ---------------------------------------------------------------------------
# is_token_pair_invalidated
# ---------------------------------------------------------------------------


def _write_auth_file(tmp_path, access_token="AT", refresh_token="RT"):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": "acct-1",
    }), encoding="utf-8")
    return str(p)


def _resp(status_code):
    r = MagicMock()
    r.status_code = status_code
    return r


def test_is_token_pair_invalidated_true_when_both_401(tmp_path):
    auth_path = _write_auth_file(tmp_path)
    with patch("requests.get", return_value=_resp(401)) as mock_get, \
         patch("requests.post", return_value=_resp(401)) as mock_post:
        assert codex_auth.is_token_pair_invalidated(auth_path) is True
    mock_get.assert_called_once()
    mock_post.assert_called_once()


def test_is_token_pair_invalidated_false_when_access_token_alive(tmp_path):
    auth_path = _write_auth_file(tmp_path)
    # /me 返回 200 → access_token 还活着,直接 False(不查 refresh 也不必)
    with patch("requests.get", return_value=_resp(200)) as mock_get, \
         patch("requests.post") as mock_post:
        assert codex_auth.is_token_pair_invalidated(auth_path) is False
    mock_get.assert_called_once()
    mock_post.assert_not_called()


def test_is_token_pair_invalidated_false_when_refresh_token_alive(tmp_path):
    auth_path = _write_auth_file(tmp_path)
    # access_token 401 但 refresh_token 200(refresh 成功) → 仍可救活,False
    with patch("requests.get", return_value=_resp(401)), \
         patch("requests.post", return_value=_resp(200)):
        assert codex_auth.is_token_pair_invalidated(auth_path) is False


def test_is_token_pair_invalidated_false_on_network_error(tmp_path):
    auth_path = _write_auth_file(tmp_path)
    with patch("requests.get", side_effect=ConnectionError("net")):
        assert codex_auth.is_token_pair_invalidated(auth_path) is False


def test_is_token_pair_invalidated_false_when_auth_file_missing(tmp_path):
    missing = tmp_path / "no-such.json"
    assert codex_auth.is_token_pair_invalidated(str(missing)) is False


def test_is_token_pair_invalidated_false_when_path_empty():
    assert codex_auth.is_token_pair_invalidated("") is False
    assert codex_auth.is_token_pair_invalidated(None) is False


def test_is_token_pair_invalidated_false_when_tokens_missing(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"access_token": "", "refresh_token": ""}), encoding="utf-8")
    assert codex_auth.is_token_pair_invalidated(str(p)) is False


def test_is_token_pair_invalidated_false_on_non_401_error(tmp_path):
    """500 之类的 server error 不算双死,保守不动。"""
    auth_path = _write_auth_file(tmp_path)
    with patch("requests.get", return_value=_resp(500)):
        assert codex_auth.is_token_pair_invalidated(auth_path) is False


# ---------------------------------------------------------------------------
# cmd_check 的"双死预筛"集成
# ---------------------------------------------------------------------------


def test_cmd_check_skips_relogin_for_double_dead_account(tmp_path, monkeypatch):
    """cmd_check 在重登入口前,is_token_pair_invalidated=True 的账号:
       - 不进入 Playwright OAuth(login_codex_via_browser 不被调用)
       - 标记为 STATUS_AUTH_INVALID
       - 写入 last_token_pair_invalidated_at 时间戳
    """
    from autoteam import accounts as accounts_mod
    from autoteam import manager

    fake_acc_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", fake_acc_file)

    # 写一个 active 账号,带可疑 auth_file(双死)
    auth_path = tmp_path / "dead-auth.json"
    auth_path.write_text(json.dumps({
        "access_token": "AT", "refresh_token": "RT", "account_id": "acct-1",
    }), encoding="utf-8")
    accounts_mod.add_account("dead@example.com", "pw")
    accounts_mod.update_account(
        "dead@example.com",
        status=accounts_mod.STATUS_ACTIVE,
        auth_file=str(auth_path),
    )

    # 跳过 sync_to_cpa / pending 对账等无关支线
    monkeypatch.setattr(manager, "sync_from_cpa", lambda: None)
    monkeypatch.setattr(manager, "_print_status_table", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_reconcile_team_members", lambda: type("R", (), {"deleted_pending": False, "deleted": []})())

    # _check_and_refresh 返回 auth_error → 触发 auth_error_list 路径
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("auth_error", None))

    # is_token_pair_invalidated 命中 → 应该跳过 mail_client + login_codex_via_browser
    monkeypatch.setattr(manager, "is_token_pair_invalidated", lambda path: True)

    login_called = MagicMock()
    monkeypatch.setattr(manager, "login_codex_via_browser", login_called)

    # CloudMail 不应被构造(短路在 Playwright 之前)
    cloudmail_called = MagicMock()
    monkeypatch.setattr(manager, "CloudMailClient", cloudmail_called)

    # 设一个 dummy CLOUDMAIL_DOMAIN 否则 no_auth 分支会过滤掉账号
    monkeypatch.setattr(
        "autoteam.config.CLOUDMAIL_DOMAIN", "@example.com", raising=False
    )

    manager.cmd_check()

    # 1. login_codex_via_browser 不被调用
    login_called.assert_not_called()
    # 2. CloudMailClient 不构造
    cloudmail_called.assert_not_called()
    # 3. 账号被标 AUTH_INVALID + stamp 时间戳
    rec = accounts_mod.find_account(accounts_mod.load_accounts(), "dead@example.com")
    assert rec["status"] == accounts_mod.STATUS_AUTH_INVALID
    assert rec["last_token_pair_invalidated_at"] is not None
    assert isinstance(rec["last_token_pair_invalidated_at"], (int, float))


def test_cmd_check_proceeds_to_relogin_when_only_access_token_dead(tmp_path, monkeypatch):
    """access_token 死但 refresh_token 活 → is_token_pair_invalidated=False → 走原重登路径。"""
    from autoteam import accounts as accounts_mod
    from autoteam import manager

    fake_acc_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", fake_acc_file)

    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT"}), encoding="utf-8")
    accounts_mod.add_account("alive@example.com", "pw")
    accounts_mod.update_account(
        "alive@example.com",
        status=accounts_mod.STATUS_ACTIVE,
        auth_file=str(auth_path),
    )

    monkeypatch.setattr(manager, "sync_from_cpa", lambda: None)
    monkeypatch.setattr(manager, "_print_status_table", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_reconcile_team_members", lambda: type("R", (), {"deleted_pending": False, "deleted": []})())
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("auth_error", None))
    monkeypatch.setattr(manager, "is_token_pair_invalidated", lambda path: False)

    # 模拟重登成功路径需要的 mail_client + bundle
    fake_mail = MagicMock()
    monkeypatch.setattr(manager, "CloudMailClient", lambda: fake_mail)

    fake_bundle = {
        "access_token": "NEW_AT",
        "refresh_token": "NEW_RT",
        "id_token": "id",
        "account_id": "acct-1",
        "plan_type": "team",
    }
    monkeypatch.setattr(manager, "login_codex_via_browser", lambda *a, **k: fake_bundle)
    monkeypatch.setattr(manager, "save_auth_file", lambda b: str(auth_path))
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("ok", {"primary_pct": 0, "weekly_pct": 0}))
    monkeypatch.setattr(
        "autoteam.config.CLOUDMAIL_DOMAIN", "@example.com", raising=False
    )

    # 不应抛、不应把账号标成 AUTH_INVALID
    manager.cmd_check()

    rec = accounts_mod.find_account(accounts_mod.load_accounts(), "alive@example.com")
    assert rec["status"] != accounts_mod.STATUS_AUTH_INVALID
    assert rec.get("last_token_pair_invalidated_at") is None
