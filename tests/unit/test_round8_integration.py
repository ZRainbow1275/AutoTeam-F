"""Round 8 — 集成测试:register_failures 新枚举、retroactive cleanup、API 端点。

覆盖:
  - register_failures.py 4 个 Round 8 category 字面量校验
  - manager._reconcile_master_degraded_subaccounts dry-run/live 路径
  - manager._run_post_register_oauth 5 次重试 loop 结构存在
  - api.py /api/admin/master-health 端点存在
  - api.py /api/tasks/fill 加了 master degraded 503 fail-fast
"""
from __future__ import annotations

import inspect


def test_round8_register_failure_categories_present():
    """4 个 Round 8 新分类必须以模块级常量存在(便于代码引用 + register_failures.json 字面)。"""
    from autoteam import register_failures as rf

    assert rf.MASTER_SUBSCRIPTION_DEGRADED == "master_subscription_degraded"
    assert rf.OAUTH_WS_NO_PERSONAL == "oauth_workspace_select_no_personal"
    assert rf.OAUTH_WS_ENDPOINT_ERROR == "oauth_workspace_select_endpoint_error"
    assert rf.OAUTH_PLAN_DRIFT_PERSISTENT == "oauth_plan_drift_persistent"


def test_round8_register_failure_record_accepts_new_categories(tmp_path, monkeypatch):
    """record_failure() 应能写入新 category 而不报错。"""
    fake_file = tmp_path / "rf.json"
    monkeypatch.setattr("autoteam.register_failures.FAILURES_FILE", fake_file)

    from autoteam.register_failures import (
        MASTER_SUBSCRIPTION_DEGRADED,
        OAUTH_PLAN_DRIFT_PERSISTENT,
        OAUTH_WS_ENDPOINT_ERROR,
        OAUTH_WS_NO_PERSONAL,
        list_failures,
        record_failure,
    )

    record_failure("a@x.com", MASTER_SUBSCRIPTION_DEGRADED, "test", master_account_id="aid")
    record_failure("b@x.com", OAUTH_WS_NO_PERSONAL, "ws missing")
    record_failure("c@x.com", OAUTH_WS_ENDPOINT_ERROR, "404")
    record_failure("d@x.com", OAUTH_PLAN_DRIFT_PERSISTENT, "5x drift")

    failures = list_failures(limit=20)
    cats = {f.get("category") for f in failures}
    assert MASTER_SUBSCRIPTION_DEGRADED in cats
    assert OAUTH_WS_NO_PERSONAL in cats
    assert OAUTH_WS_ENDPOINT_ERROR in cats
    assert OAUTH_PLAN_DRIFT_PERSISTENT in cats


def test_round8_personal_branch_has_5_retry_loop():
    """W-I8 — _run_post_register_oauth personal 分支应包含 5 次重试外层 for-loop 结构。"""
    from autoteam import manager

    src = inspect.getsource(manager._run_post_register_oauth)
    # 必须有 max_retries=5 + plan_drift_history + OAUTH_PLAN_DRIFT_PERSISTENT 写入
    assert "max_retries = 5" in src
    assert "plan_drift_history" in src
    assert "OAUTH_PLAN_DRIFT_PERSISTENT" in src
    # 必须有指数退避表
    assert "(0, 5, 10, 20, 30)" in src or "retry_backoff" in src


def test_round8_personal_branch_has_master_health_pre_check():
    """M-T1 — personal OAuth 入口必须先 is_master_subscription_healthy。"""
    from autoteam import manager

    src = inspect.getsource(manager._run_post_register_oauth)
    assert "is_master_subscription_healthy" in src
    assert "MASTER_SUBSCRIPTION_DEGRADED" in src
    assert "subscription_cancelled" in src


def test_round8_reconcile_retroactive_cleanup_function_exists():
    """Round 8 — _reconcile_master_degraded_subaccounts 应该作为模块级函数存在。"""
    from autoteam import manager

    assert hasattr(manager, "_reconcile_master_degraded_subaccounts")
    fn = manager._reconcile_master_degraded_subaccounts
    sig = inspect.signature(fn)
    # 参数应支持 dry_run kw
    assert "dry_run" in sig.parameters


def test_round8_cmd_reconcile_calls_retroactive_cleanup():
    """cmd_reconcile 应在常规 reconcile 后调用 retroactive cleanup,把结果嵌进返回 dict。"""
    from autoteam import manager

    src = inspect.getsource(manager.cmd_reconcile)
    assert "_reconcile_master_degraded_subaccounts" in src
    assert "master_degraded_retroactive" in src


def test_round8_api_master_health_endpoint_registered():
    """/api/admin/master-health GET 端点应已注册。"""
    from autoteam.api import app

    routes = {r.path: getattr(r, "methods", set()) for r in app.routes}
    assert "/api/admin/master-health" in routes
    assert "GET" in routes["/api/admin/master-health"]


def test_round8_api_diagnose_includes_master_subscription_state():
    """/api/admin/diagnose 返回结构应含 master_subscription_state 字段(spec §6.2)。"""
    from autoteam import api

    src = inspect.getsource(api.get_admin_diagnose)
    assert "master_subscription_state" in src
    assert "is_master_subscription_healthy" in src


def test_round8_api_tasks_fill_has_master_degraded_503_fail_fast():
    """/api/tasks/fill leave_workspace=True 入口应在 master cancelled 时 503。"""
    from autoteam import api

    src = inspect.getsource(api.post_fill)
    assert "master_subscription_degraded" in src
    assert "503" in src
    assert "is_master_subscription_healthy" in src


def test_round8_oauth_workspace_module_exports_4_functions():
    """oauth_workspace 必须暴露 spec 列出的 4 个函数。"""
    from autoteam import oauth_workspace

    assert callable(oauth_workspace.decode_oauth_session_cookie)
    assert callable(oauth_workspace.select_oauth_workspace)
    assert callable(oauth_workspace.force_select_personal_via_ui)
    assert callable(oauth_workspace.ensure_personal_workspace_selected)


def test_round8_master_health_module_exports_main_function():
    """master_health 必须暴露顶层 is_master_subscription_healthy。"""
    from autoteam import master_health

    fn = master_health.is_master_subscription_healthy
    sig = inspect.signature(fn)
    # spec §2.2 — kwargs 集合
    assert "account_id" in sig.parameters
    assert "timeout" in sig.parameters
    assert "cache_ttl" in sig.parameters
    assert "force_refresh" in sig.parameters


def test_round8_codex_auth_calls_ensure_personal_workspace_selected():
    """codex_auth 在 use_personal=True 流程应调用 ensure_personal_workspace_selected。"""
    from autoteam import codex_auth

    src = inspect.getsource(codex_auth)
    assert "ensure_personal_workspace_selected" in src


def test_round8_team_branch_has_master_probe_pre_check():
    """P1-1 — _run_post_register_oauth Team 分支(leave_workspace=False)必须对称 master probe。

    spec/shared/master-subscription-health.md §4 表 M-T2 + spec-2 v1.5 §3.7 表第 2 行。
    Team 路径母号降级时必拿 plan_type=free,不 fail-fast 会堆 plan_drift。
    """
    from autoteam import manager

    src = inspect.getsource(manager._run_post_register_oauth)
    # 必须有 Team 分支独立的 master probe 字面量(stage 命名带 team_precheck 区分)
    assert "run_post_register_oauth_team_precheck" in src
    assert "M-T2" in src  # 注释引用 spec
    # Team 分支 master degraded 处置必须是 STATUS_AUTH_INVALID + 主动 kick(席位还在,不能 delete_account)
    team_precheck_idx = src.index("run_post_register_oauth_team_precheck")
    nearby = src[team_precheck_idx:team_precheck_idx + 1500]
    assert "STATUS_AUTH_INVALID" in nearby
    assert "remove_from_team" in nearby  # 主动 kick 释放席位


def test_round8_team_branch_master_probe_uses_master_subscription_degraded_category():
    """Team 分支 master probe 失败时必须用同一个 MASTER_SUBSCRIPTION_DEGRADED category(便于聚合统计)。"""
    from autoteam import manager

    src = inspect.getsource(manager._run_post_register_oauth)
    # personal 分支 + Team 分支应都用 MASTER_SUBSCRIPTION_DEGRADED
    occurrences = src.count("MASTER_SUBSCRIPTION_DEGRADED")
    assert occurrences >= 2, f"期望 personal + Team 两个分支都引用 MASTER_SUBSCRIPTION_DEGRADED,实际 {occurrences} 处"
