"""SPEC-2 — 账号生命周期与配额(共 5 共因 + 3 独属)的单元测试。

覆盖范围:
  共因 A: plan_type 白名单 — Round 7 P2.3 已抽到 tests/unit/test_plan_type_whitelist.py
  共因 B: wham/usage 4+1 分类 — no_quota 触发条件
  共因 C: OAuth add-phone 探针(detect_phone_verification 复用契约 — 通过 import 校验,
          深度 e2e 留给 codex_auth 集成测试)
  共因 D: _run_post_register_oauth quota probe 流转(集成测试,这里仅做 import 健康检查)
  共因 E: sync_account_states 区分被踢 vs 待机(probe helper + 30min 冷却)
  独属 F: PREFERRED_SEAT_TYPE — runtime_config getter/setter
  独属 G: personal 删除解耦(STATUS_PERSONAL 短路 remote_state)
  独属 H: register_failures.record_failure 接受 reason="" 默认(SPEC-2 新增 6 类 category)
"""

from unittest.mock import patch

# ---------------------------------------------------------------------------
# 共因 B — wham/usage no_quota 分类
# ---------------------------------------------------------------------------

def test_get_quota_exhausted_info_detects_primary_total_zero():
    """primary_total=0 是后端没分配配额(不是耗尽),应识别为 no_quota。"""
    from autoteam.codex_auth import get_quota_exhausted_info
    quota_info = {
        "primary_pct": 0,
        "weekly_pct": 0,
        "primary_total": 0,
        "primary_resets_at": 0,
    }
    info = get_quota_exhausted_info(quota_info)
    assert info is not None and info["window"] == "no_quota"
    assert "primary_total==0" in info.get("no_quota_signals", [])


def test_get_quota_exhausted_info_detects_rate_limit_empty():
    """primary_total 缺失 + 全 0 字段 + 未 limit_reached → no_quota(rate_limit_empty 信号)。"""
    from autoteam.codex_auth import get_quota_exhausted_info
    quota_info = {
        "primary_pct": 0,
        "weekly_pct": 0,
        "primary_total": None,
        "primary_resets_at": 0,
    }
    info = get_quota_exhausted_info(quota_info, limit_reached=False)
    assert info is not None and info["window"] == "no_quota"
    assert "rate_limit_empty" in info.get("no_quota_signals", [])


def test_get_quota_exhausted_info_normal_exhausted_path_unchanged():
    """primary_pct=100 + 有 reset 时间 → exhausted(老路径不应被 no_quota 改动影响)。"""
    from autoteam.codex_auth import get_quota_exhausted_info
    quota_info = {
        "primary_pct": 100,
        "weekly_pct": 0,
        "primary_total": 1000,
        "primary_resets_at": 9999999999,
    }
    info = get_quota_exhausted_info(quota_info)
    assert info is not None and info["window"] == "primary"


def test_get_quota_exhausted_info_returns_none_when_neither_exhausted_nor_no_quota():
    """正常使用中(primary_pct=20, total=1000),应返回 None,不触发 exhausted 或 no_quota。"""
    from autoteam.codex_auth import get_quota_exhausted_info
    quota_info = {
        "primary_pct": 20,
        "weekly_pct": 10,
        "primary_total": 1000,
        "primary_resets_at": 9999999999,
    }
    info = get_quota_exhausted_info(quota_info)
    assert info is None


# ---------------------------------------------------------------------------
# 共因 C — OAuth add-phone 探针(import 健康检查)
# ---------------------------------------------------------------------------

def test_codex_auth_imports_assert_not_blocked_from_invite():
    """C-P1~C-P3 三处 OAuth 探针应能 import 到 invite.assert_not_blocked。"""
    from autoteam.invite import RegisterBlocked, assert_not_blocked
    assert callable(assert_not_blocked)
    # RegisterBlocked 必须有 is_phone / is_duplicate 字段供分类
    exc = RegisterBlocked("oauth_about_you", "phone-required", is_phone=True, is_duplicate=False)
    assert exc.is_phone is True
    assert exc.is_duplicate is False


# ---------------------------------------------------------------------------
# 共因 D — _run_post_register_oauth import 健康检查
# ---------------------------------------------------------------------------

def test_manager_module_imports_post_register_oauth_dependencies():
    """manager.py 必须 import is_supported_plan / RegisterBlocked,否则 SPEC-2 落空。"""
    import autoteam.manager as m
    assert hasattr(m, "is_supported_plan")
    assert hasattr(m, "RegisterBlocked")


# ---------------------------------------------------------------------------
# 共因 E — runtime_config 探测节流
# ---------------------------------------------------------------------------

def test_sync_probe_concurrency_default_and_clamp(tmp_path, monkeypatch):
    from autoteam import runtime_config
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "rt.json")

    assert runtime_config.get_sync_probe_concurrency() == 5  # default
    runtime_config.set_sync_probe_concurrency(99)
    assert runtime_config.get_sync_probe_concurrency() == 16  # clamp upper
    runtime_config.set_sync_probe_concurrency(0)
    assert runtime_config.get_sync_probe_concurrency() == 1   # clamp lower
    runtime_config.set_sync_probe_concurrency(7)
    assert runtime_config.get_sync_probe_concurrency() == 7


def test_sync_probe_cooldown_minutes_default_and_clamp(tmp_path, monkeypatch):
    from autoteam import runtime_config
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "rt.json")

    assert runtime_config.get_sync_probe_cooldown_minutes() == 30  # default
    runtime_config.set_sync_probe_cooldown_minutes(2000)
    assert runtime_config.get_sync_probe_cooldown_minutes() == 1440
    runtime_config.set_sync_probe_cooldown_minutes(0)
    assert runtime_config.get_sync_probe_cooldown_minutes() == 1


# ---------------------------------------------------------------------------
# 独属 F — PREFERRED_SEAT_TYPE
# ---------------------------------------------------------------------------

def test_preferred_seat_type_default_is_default(tmp_path, monkeypatch):
    from autoteam import runtime_config
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "rt.json")
    assert runtime_config.get_preferred_seat_type() == "default"


def test_preferred_seat_type_accepts_codex_value(tmp_path, monkeypatch):
    from autoteam import runtime_config
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "rt.json")
    runtime_config.set_preferred_seat_type("codex")
    assert runtime_config.get_preferred_seat_type() == "codex"


def test_preferred_seat_type_invalid_falls_back_to_default(tmp_path, monkeypatch):
    from autoteam import runtime_config
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "rt.json")
    runtime_config.set_preferred_seat_type("garbage")
    assert runtime_config.get_preferred_seat_type() == "default"


def test_invite_member_allow_patch_upgrade_signature():
    """chatgpt_api.invite_member 必须接受 allow_patch_upgrade 关键字参数(SPEC-2 FR-G)。"""
    import inspect

    from autoteam.chatgpt_api import ChatGPTTeamAPI
    sig = inspect.signature(ChatGPTTeamAPI.invite_member)
    assert "allow_patch_upgrade" in sig.parameters
    assert sig.parameters["allow_patch_upgrade"].default is True


# ---------------------------------------------------------------------------
# 独属 G — personal 删除解耦
# ---------------------------------------------------------------------------

def test_delete_managed_account_skips_remote_state_for_personal(tmp_path, monkeypatch):
    """STATUS_PERSONAL 账号删除不应触发 ChatGPTTeamAPI 启动(短路 remote_state)。"""
    from autoteam import account_ops
    from autoteam import accounts as accounts_mod

    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(account_ops, "AUTH_DIR", tmp_path / "auths")
    monkeypatch.setattr(accounts_mod, "get_admin_email", lambda: "")

    accounts_mod.save_accounts([{
        "email": "personal@x.com",
        "status": accounts_mod.STATUS_PERSONAL,
        "auth_file": None,
        "cloudmail_account_id": None,
    }])

    # 关键:不应启动 ChatGPTTeamAPI(personal 短路)
    with patch("autoteam.account_ops.fetch_team_state") as mock_fetch:
        with patch("autoteam.cpa_sync.sync_to_cpa"):
            with patch("autoteam.cpa_sync.list_cpa_files", return_value=[]):
                with patch("autoteam.cpa_sync.delete_from_cpa", return_value=True):
                    with patch("autoteam.admin_state.get_chatgpt_account_id", return_value="acc-id"):
                        result = account_ops.delete_managed_account(
                            "personal@x.com",
                            remove_remote=True,  # 即使要求清远端,personal 也短路
                            remove_cloudmail=False,
                            sync_cpa_after=False,
                        )
        assert mock_fetch.call_count == 0  # 未触发远端拉取
    assert result["local_record"] is True


# ---------------------------------------------------------------------------
# 独属 H — register_failures.record_failure 兼容 reason 默认空
# ---------------------------------------------------------------------------

def test_record_failure_accepts_empty_reason_with_detail(tmp_path, monkeypatch):
    from autoteam import register_failures
    monkeypatch.setattr(register_failures, "FAILURES_FILE", tmp_path / "rf.json")

    register_failures.record_failure(
        "ex@x.com", "plan_unsupported",
        stage="reinvite_account", detail="plan_type=enterprise"
    )
    items = register_failures.list_failures(50)
    assert len(items) == 1
    assert items[0]["category"] == "plan_unsupported"
    assert items[0]["reason"] == "plan_type=enterprise"  # 从 detail 兜底
    assert items[0]["stage"] == "reinvite_account"


def test_record_failure_supports_all_spec2_new_categories(tmp_path, monkeypatch):
    from autoteam import register_failures
    monkeypatch.setattr(register_failures, "FAILURES_FILE", tmp_path / "rf.json")

    new_categories = [
        "oauth_phone_blocked",
        "plan_unsupported",
        "no_quota_assigned",
        "plan_drift",
        "auth_error_at_oauth",
        "quota_probe_network_error",
    ]
    for cat in new_categories:
        register_failures.record_failure(f"{cat}@x.com", cat, "test reason")

    counts = register_failures.count_by_category()
    for cat in new_categories:
        assert counts.get(cat) == 1
