"""覆盖 task #2:cmd_check 新增 include_standby 开关 + _probe_standby_quota。

- include_standby=False(默认) 不探测 standby 池,保持向后兼容
- include_standby=True 调用 _probe_standby_quota,遍历 standby + 限速 + 24h 去重
- 401/403 类 auth_error → STATUS_AUTH_INVALID
"""

from __future__ import annotations

from autoteam import manager
from autoteam.accounts import STATUS_ACTIVE, STATUS_AUTH_INVALID, STATUS_STANDBY


def _stub_cmd_check_deps(monkeypatch, accounts_list):
    """把 cmd_check 走通但所有外部副作用短路,仅观察 _probe_standby_quota 是否被调用。

    配合 accounts_list 至少包含一个 auth_file 存在的 active 账号,避免 "没有可检查的 active"
    提前 return。
    """
    monkeypatch.setattr(manager, "load_accounts", lambda: accounts_list)
    monkeypatch.setattr(manager, "_reconcile_team_members", lambda *_a, **_kw: {})
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("ok", {"primary_pct": 10, "weekly_pct": 10}))
    monkeypatch.setattr(manager, "update_account", lambda *_a, **_kw: None)
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: None)
    # 屏蔽 personal 分支中 load_accounts 再调(上面已 monkeypatch 生效)
    # CLOUDMAIL_DOMAIN 走 config import,无需额外 mock


def _fake_auth_file(tmp_path, email):
    f = tmp_path / f"codex-{email}.json"
    f.write_text("{}", encoding="utf-8")
    return str(f)


def test_check_skips_standby_by_default(tmp_path, monkeypatch):
    """cmd_check() 不传 include_standby → 默认 False → 不应调用 _probe_standby_quota。"""
    probe_called = {"n": 0}
    monkeypatch.setattr(manager, "_probe_standby_quota", lambda: probe_called.__setitem__("n", probe_called["n"] + 1))

    active = {
        "email": "a@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": _fake_auth_file(tmp_path, "a@example.com"),
    }
    _stub_cmd_check_deps(monkeypatch, [active])

    manager.cmd_check()  # 默认 include_standby=False
    assert probe_called["n"] == 0


def test_check_include_standby_probes_all(tmp_path, monkeypatch):
    """cmd_check(include_standby=True) 必须调用 _probe_standby_quota。"""
    probe_called = {"n": 0}
    monkeypatch.setattr(manager, "_probe_standby_quota", lambda: probe_called.__setitem__("n", probe_called["n"] + 1))

    active = {
        "email": "a@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": _fake_auth_file(tmp_path, "a@example.com"),
    }
    _stub_cmd_check_deps(monkeypatch, [active])

    manager.cmd_check(include_standby=True)
    assert probe_called["n"] == 1


def test_check_rate_limited_between_accounts(tmp_path, monkeypatch):
    """_probe_standby_quota 相邻账号必须 sleep STANDBY_PROBE_INTERVAL_SEC,避免群访风控。"""
    stby_a = {
        "email": "s1@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "s1"),
        "last_quota_check_at": None,
    }
    stby_b = {
        "email": "s2@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "s2"),
        "last_quota_check_at": None,
    }
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [stby_a, stby_b])
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("ok", {"primary_pct": 20, "weekly_pct": 20}))
    monkeypatch.setattr(manager, "update_account", lambda *_a, **_kw: None)

    sleeps = []
    monkeypatch.setattr(manager.time, "sleep", lambda s: sleeps.append(s))

    manager._probe_standby_quota()

    # 2 账号之间应该 sleep 恰好 1 次(第一个前不 sleep),间隔 = STANDBY_PROBE_INTERVAL_SEC
    assert sleeps == [manager.STANDBY_PROBE_INTERVAL_SEC]


def test_check_skips_recently_probed(tmp_path, monkeypatch):
    """last_quota_check_at 在 24h 内的 standby 必须被跳过,不再消耗 wham 配额。"""
    now = 1_700_000_000.0
    monkeypatch.setattr(manager.time, "time", lambda: now)

    recent = {
        "email": "recent@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "recent"),
        "last_quota_check_at": now - 3600,  # 1h 前探测过
    }
    stale = {
        "email": "stale@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "stale"),
        "last_quota_check_at": now - (manager.STANDBY_PROBE_DEDUP_SEC + 60),  # 超过 24h
    }
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [recent, stale])
    monkeypatch.setattr(manager.time, "sleep", lambda *_a: None)

    probed = []

    def fake_check_and_refresh(acc):
        probed.append(acc["email"])
        return ("ok", {"primary_pct": 50, "weekly_pct": 50})

    monkeypatch.setattr(manager, "_check_and_refresh", fake_check_and_refresh)
    monkeypatch.setattr(manager, "update_account", lambda *_a, **_kw: None)

    manager._probe_standby_quota()

    # recent 被 24h 去重跳过,只有 stale 被实际探测
    assert probed == ["stale@example.com"]


def test_check_marks_auth_invalid_on_401(tmp_path, monkeypatch):
    """_check_and_refresh 返回 auth_error(401/403/token 刷新失败) → 标 STATUS_AUTH_INVALID。"""
    stby = {
        "email": "dead@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "dead"),
        "last_quota_check_at": None,
    }
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [stby])
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("auth_error", None))
    monkeypatch.setattr(manager.time, "sleep", lambda *_a: None)

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    manager._probe_standby_quota()

    assert len(updates) == 1
    email, fields = updates[0]
    assert email == "dead@example.com"
    assert fields["status"] == STATUS_AUTH_INVALID
    assert "last_quota_check_at" in fields
