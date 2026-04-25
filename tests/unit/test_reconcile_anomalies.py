"""覆盖 task #3:_reconcile_team_members 识别残废 / 错位 / 耗尽未抛弃 / ghost + dry_run。

直接 mock chatgpt_api._api_fetch + remove_from_team + update_account,构造不同 workspace
成员 × 本地账号状态的组合,断言 result dict 里对应分支命中。
"""

from __future__ import annotations

import json
import types

from autoteam import manager
from autoteam.accounts import (
    STATUS_ACTIVE,
    STATUS_EXHAUSTED,
    STATUS_ORPHAN,
    STATUS_STANDBY,
)


def _make_fake_chatgpt(members):
    """构造一个 fake ChatGPTTeamAPI,/users 返回给定成员列表。"""
    body = json.dumps({"items": members})

    def fake_api_fetch(method, path, body_=None):
        if method == "GET" and path.endswith("/users"):
            return {"status": 200, "body": body}
        return {"status": 200, "body": "{}"}

    fake = types.SimpleNamespace(browser=True, _api_fetch=fake_api_fetch)
    return fake


def _common_monkeypatch(monkeypatch, accounts_list, *, main_email="owner@example.com"):
    """统一 patch:account_id 有值,主号识别走 _is_main_account_email。"""
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-xxx")
    monkeypatch.setattr(manager, "load_accounts", lambda: accounts_list)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda e: (e or "").lower() == main_email.lower())
    # 避免第二轮 /users 触发 real logic:返回同样 body
    # 已经由 fake chatgpt 的 _api_fetch 处理
    # time.time 稳定化
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000.0)


def test_reconcile_orphan_kicks_when_no_auth(tmp_path, monkeypatch):
    """workspace 有 + 本地 active + auth_file 缺失 → 残废,按默认 KICK_ORPHAN=true 被 KICK。"""
    acc = {
        "email": "orphan@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,  # 关键:缺 auth
    }
    fake = _make_fake_chatgpt([{"email": "orphan@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    # 强制走 KICK 分支(关闭人工介入)。manager 内部 `from autoteam.config import ...`
    # 走 config 模块命名空间,这里确保 config 默认 True(实际就是 True,这行保险用)
    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)
    # _find_team_auth_file 返回 None (auths 目录里找不到)
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)

    kicked = []

    def fake_remove(_api, email, **kw):
        kicked.append(email)
        return "removed"

    monkeypatch.setattr(manager, "remove_from_team", fake_remove)
    monkeypatch.setattr(manager, "update_account", lambda *_a, **_kw: None)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "orphan@example.com" in result["orphan_kicked"]
    assert kicked == ["orphan@example.com"]


def test_reconcile_status_drift_local_standby_workspace_active(tmp_path, monkeypatch):
    """workspace=active + 本地=standby + auth_file 存在 → 错位,修正 active,不 KICK。"""
    auth_path = tmp_path / "codex-drift@example.com-team-1.json"
    auth_path.write_text("{}", encoding="utf-8")

    acc = {
        "email": "drift@example.com",
        "status": STATUS_STANDBY,
        "auth_file": str(auth_path),
    }
    fake = _make_fake_chatgpt([{"email": "drift@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    # KICK 被调用则测试失败
    def _forbid_kick(*_a, **_kw):
        raise AssertionError("drift case must not KICK")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "drift@example.com" in result["misaligned_fixed"]
    assert updates  # 至少一次 update_account
    # 修正为 STATUS_ACTIVE
    assert any(kw.get("status") == STATUS_ACTIVE for _email, kw in updates)


def test_reconcile_marks_exhausted_when_quota_zero(tmp_path, monkeypatch):
    """workspace=active + 本地=active + auth_file 有 + last_quota 5h/周均 100% → 标 EXHAUSTED,**不 KICK**。"""
    auth_path = tmp_path / "codex-eaten@example.com.json"
    auth_path.write_text("{}", encoding="utf-8")

    acc = {
        "email": "eaten@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": str(auth_path),
        "last_quota": {"primary_pct": 100, "weekly_pct": 100},
    }
    fake = _make_fake_chatgpt([{"email": "eaten@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("exhausted snapshot must not KICK immediately")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "eaten@example.com" in result["exhausted_marked"]
    # 必须带 status=EXHAUSTED 且写 quota_exhausted_at
    assert any(kw.get("status") == STATUS_EXHAUSTED and kw.get("quota_exhausted_at") is not None for _e, kw in updates)


def test_reconcile_dry_run_does_not_mutate(tmp_path, monkeypatch):
    """dry_run=True 即便识别出需要 KICK/update 的异常,也绝不实际 kick 或写 accounts.json。"""
    acc = {
        "email": "ghost-local@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,
    }
    # 同时有本地不存在的 ghost 成员
    fake = _make_fake_chatgpt(
        [
            {"email": "ghost-local@example.com"},
            {"email": "completely-unknown@example.com"},  # ghost:本地无记录
        ]
    )
    _common_monkeypatch(monkeypatch, [acc])
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)
    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)
    monkeypatch.setattr(_cfg, "RECONCILE_KICK_GHOST", True, raising=False)

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("dry_run must not call remove_from_team")

    def _forbid_update(*_a, **_kw):
        raise AssertionError("dry_run must not call update_account")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)
    monkeypatch.setattr(manager, "update_account", _forbid_update)

    result = manager._reconcile_team_members(chatgpt_api=fake, dry_run=True)

    # dry_run 仍然应该"发现"异常并记录到 result
    assert result["dry_run"] is True
    assert "ghost-local@example.com" in result["orphan_kicked"]  # 包含 dry_run 记录
    assert "completely-unknown@example.com" in result["ghost_kicked"]


def test_reconcile_orphan_marked_when_kick_disabled(tmp_path, monkeypatch):
    """RECONCILE_KICK_ORPHAN=False → 残废只标 STATUS_ORPHAN,不 KICK。"""
    acc = {
        "email": "stay@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,
    }
    fake = _make_fake_chatgpt([{"email": "stay@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)
    # 函数内通过 `from autoteam.config import RECONCILE_KICK_ORPHAN`,必须改 config 模块属性
    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", False, raising=False)

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("RECONCILE_KICK_ORPHAN=False must not KICK")

    updates = []
    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "stay@example.com" in result["orphan_marked"]
    # 应只被打 STATUS_ORPHAN 标记
    assert any(kw.get("status") == STATUS_ORPHAN for _e, kw in updates)
