"""Round 9 SPEC v1.1 §11 — _apply_master_degraded_classification helper 单测。

覆盖 5 触发点接入 + 进入/退出/撤回路径 + 永不抛 (M-I11)。
"""
from __future__ import annotations

import base64
import json
import time

import pytest


def _make_id_token_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


@pytest.fixture
def grace_jwt_future():
    """grace_until 在 30 天后 — 仍在 grace 期。"""
    future = time.time() + 30 * 86400
    return _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": future,
        }
    })


@pytest.fixture
def grace_jwt_past():
    """grace_until 已过期(1 天前)— 应转 STANDBY。"""
    past = time.time() - 86400
    return _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": past,
        }
    })


@pytest.fixture(autouse=True)
def _isolated_accounts_file(tmp_path, monkeypatch):
    """各测试隔离 accounts.json,不污染真实状态。"""
    fake = tmp_path / "accounts.json"
    monkeypatch.setattr("autoteam.accounts.ACCOUNTS_FILE", fake)
    monkeypatch.setattr(
        "autoteam.admin_state.get_chatgpt_account_id", lambda: "test-master",
    )
    fake_health_cache = tmp_path / "master_health_cache.json"
    monkeypatch.setattr("autoteam.master_health.CACHE_FILE", fake_health_cache)
    yield


def _seed_accounts(records):
    """直接写 accounts.json — 绕过 add_account 字段填充。"""
    from autoteam.accounts import save_accounts

    save_accounts(records)


def _make_auth_file(tmp_path, name, id_token):
    p = tmp_path / name
    p.write_text(json.dumps({
        "type": "codex",
        "access_token": "ACC",
        "id_token": id_token,
        "email": name,
    }))
    return str(p)


class _StubAPI:
    """模拟 ChatGPTTeamAPI:browser 真值 + _api_fetch 返回固定 master health 响应。"""

    def __init__(self, items, status=200):
        self.browser = object()
        self._items = items
        self._status = status

    def _api_fetch(self, method, path):
        if path == "/backend-api/accounts":
            return {"status": self._status, "body": json.dumps({"items": self._items})}
        return {"status": 404, "body": ""}

    def stop(self):
        self.browser = None


def test_master_active_no_grace_candidates_skipped(tmp_path):
    """master 健康(active)且无 GRACE 子号 → skipped_reason=master_active_no_grace_candidates。"""
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": False},
    ])
    _seed_accounts([
        {"email": "a@x.com", "status": "active", "workspace_account_id": "test-master"},
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    # 没有 GRACE 子号待撤回 → skipped
    assert result["skipped_reason"] == "master_active_no_grace_candidates"
    assert result["marked_grace"] == []
    assert result["marked_standby"] == []


def test_master_cancelled_grace_period_marks_grace(tmp_path, grace_jwt_future):
    """master cancelled + JWT grace_until 未过期 → ACTIVE → DEGRADED_GRACE。"""
    from autoteam.accounts import STATUS_DEGRADED_GRACE, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])
    auth_file = _make_auth_file(tmp_path, "auth1.json", grace_jwt_future)
    _seed_accounts([
        {
            "email": "a@x.com",
            "status": "active",
            "workspace_account_id": "test-master",
            "auth_file": auth_file,
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    assert "a@x.com" in result["marked_grace"]
    assert result["marked_standby"] == []

    # 状态实际落盘
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_DEGRADED_GRACE
    assert accs[0]["grace_until"] is not None
    assert accs[0]["master_account_id_at_grace"] == "test-master"


def test_master_cancelled_grace_expired_marks_standby(tmp_path, grace_jwt_past):
    """master cancelled + JWT grace_until 已过期 → ACTIVE → STANDBY 直接降级,跳 GRACE。"""
    from autoteam.accounts import STATUS_STANDBY, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])
    auth_file = _make_auth_file(tmp_path, "auth1.json", grace_jwt_past)
    _seed_accounts([
        {
            "email": "a@x.com",
            "status": "active",
            "workspace_account_id": "test-master",
            "auth_file": auth_file,
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    assert "a@x.com" in result["marked_standby"]
    assert "a@x.com" not in result["marked_grace"]

    accs = load_accounts()
    assert accs[0]["status"] == STATUS_STANDBY


def test_grace_account_expired_demotes_to_standby(tmp_path, grace_jwt_past):
    """已经是 GRACE 的子号 + grace_until 已过期 → STANDBY(进入 GRACE 后到期降级)。"""
    from autoteam.accounts import STATUS_DEGRADED_GRACE, STATUS_STANDBY, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])

    expired_ts = time.time() - 100  # past
    _seed_accounts([
        {
            "email": "g@x.com",
            "status": STATUS_DEGRADED_GRACE,
            "workspace_account_id": "test-master",
            "grace_until": expired_ts,
            "grace_marked_at": expired_ts - 1000,
            "master_account_id_at_grace": "test-master",
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    assert "g@x.com" in result["marked_standby"]
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_STANDBY
    assert accs[0]["grace_until"] is None


def test_master_recovered_reverts_grace_to_active(tmp_path):
    """master 从 cancelled 转 active + 子号 status=DEGRADED_GRACE → ACTIVE 撤回。"""
    from autoteam.accounts import STATUS_ACTIVE, STATUS_DEGRADED_GRACE, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": False},
    ])
    _seed_accounts([
        {
            "email": "g@x.com",
            "status": STATUS_DEGRADED_GRACE,
            "workspace_account_id": "test-master",
            "grace_until": time.time() + 1000,
            "master_account_id_at_grace": "test-master",
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    assert "g@x.com" in result["reverted_active"]
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_ACTIVE
    assert accs[0]["grace_until"] is None
    assert accs[0]["master_account_id_at_grace"] is None


def test_helper_dry_run_does_not_persist(tmp_path, grace_jwt_future):
    """dry_run=True → 候选 list 返回但不写盘。"""
    from autoteam.accounts import STATUS_ACTIVE, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])
    auth_file = _make_auth_file(tmp_path, "auth1.json", grace_jwt_future)
    _seed_accounts([
        {
            "email": "a@x.com",
            "status": STATUS_ACTIVE,
            "workspace_account_id": "test-master",
            "auth_file": auth_file,
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api, dry_run=True)
    assert "a@x.com" in result["marked_grace"]

    # 状态没变
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_ACTIVE


def test_helper_workspace_mismatch_skipped(tmp_path, grace_jwt_future):
    """workspace_account_id 不等于降级母号 — 跳过(Round 9:历史遗留 070421bb 子号)。"""
    from autoteam.accounts import STATUS_ACTIVE, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])
    auth_file = _make_auth_file(tmp_path, "auth1.json", grace_jwt_future)
    _seed_accounts([
        {
            "email": "old@x.com",
            "status": STATUS_ACTIVE,
            "workspace_account_id": "old-master-historic",
            "auth_file": auth_file,
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    assert result["marked_grace"] == []
    assert result["marked_standby"] == []
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_ACTIVE  # 保持 — workspace 漂移守卫


def test_helper_jwt_decode_failure_silent(tmp_path):
    """auth_file 缺 id_token → JWT 解析失败 → 候选转 STANDBY 而非抛 (M-I11)。"""
    from autoteam.accounts import STATUS_STANDBY, load_accounts
    from autoteam.master_health import _apply_master_degraded_classification

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])
    p = tmp_path / "auth_no_id.json"
    p.write_text(json.dumps({"type": "codex", "access_token": "ACC"}))  # 没 id_token

    _seed_accounts([
        {
            "email": "a@x.com",
            "status": "active",
            "workspace_account_id": "test-master",
            "auth_file": str(p),
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    # 没 grace_until 可解 → 转 STANDBY 而不是 GRACE
    assert "a@x.com" in result["marked_standby"]
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_STANDBY


def test_helper_chatgpt_api_start_failure_skipped():
    """传入 None chatgpt_api,本地无可启动的 ChatGPTTeamAPI 时 → skipped_reason 路径,不抛 (M-I11)。"""
    from unittest.mock import patch

    from autoteam.master_health import _apply_master_degraded_classification

    with patch("autoteam.chatgpt_api.ChatGPTTeamAPI") as MockApi:
        MockApi.side_effect = RuntimeError("playwright unavailable")
        result = _apply_master_degraded_classification(chatgpt_api=None)
        assert result["skipped_reason"] is not None
        assert "chatgpt_api_start_failed" in result["skipped_reason"]


def test_round8_wrapper_back_compat(tmp_path, grace_jwt_future):
    """_reconcile_master_degraded_subaccounts wrapper 必须保留 degraded_marked 字段(向后兼容)。"""
    from autoteam.manager import _reconcile_master_degraded_subaccounts

    api = _StubAPI([
        {"id": "test-master", "structure": "workspace",
         "current_user_role": "account-owner", "eligible_for_auto_reactivation": True},
    ])
    auth_file = _make_auth_file(tmp_path, "auth1.json", grace_jwt_future)
    _seed_accounts([
        {
            "email": "a@x.com",
            "status": "active",
            "workspace_account_id": "test-master",
            "auth_file": auth_file,
        },
    ])

    result = _reconcile_master_degraded_subaccounts(chatgpt_api=api)
    # 向后兼容字段
    assert "degraded_marked" in result
    assert "skipped_reason" in result
    # 新字段透传
    assert "marked_grace" in result
    assert "a@x.com" in result["degraded_marked"]


def test_helper_5_trigger_points_present():
    """5 触发点 import 必须能通,文件解析无语法错。"""
    import autoteam.api as api_mod  # RT-1 lifespan / RT-2 _auto_check_loop
    import autoteam.manager as mgr_mod  # RT-3/-4/-5 + RT-6 wrapper

    src_api = open(api_mod.__file__, encoding="utf-8").read()
    src_mgr = open(mgr_mod.__file__, encoding="utf-8").read()

    # api.py 应至少包含 lifespan / auto_check 两处对 helper 的调用
    assert src_api.count("_apply_master_degraded_classification") >= 2, (
        "RT-1/RT-2 接入缺失"
    )
    # manager.py 应至少包含 _reconcile_team_members / sync_account_states / cmd_check / cmd_rotate / wrapper 共 ≥4 处
    assert src_mgr.count("_apply_master_degraded_classification") >= 4, (
        "RT-3/RT-4/RT-5 接入或 wrapper 缺失"
    )
