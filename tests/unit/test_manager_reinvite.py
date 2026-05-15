import types

from autoteam import accounts, manager


def test_reinvite_account_uses_unified_oauth_login_and_marks_active(monkeypatch):
    """OAuth + plan=team + 实测配额 ok → STATUS_ACTIVE。

    SPEC-2 §3.3 给 reinvite_account 加了 quota probe(check_codex_quota),
    必须 mock 它返回 ok 才能走到 active 分支。
    """
    updates = []

    monkeypatch.setattr(
        manager,
        "login_codex_via_browser",
        lambda email, password, mail_client=None: {
            "email": email,
            "access_token": "token-1",
            "refresh_token": "refresh-1",
            "plan_type": "team",
            "plan_type_raw": "team",
        },
    )
    monkeypatch.setattr(manager, "save_auth_file", lambda bundle: f"/tmp/{bundle['email']}.json")
    monkeypatch.setattr(
        manager,
        "update_account",
        lambda email, **kwargs: updates.append((email, kwargs)),
    )
    # SPEC-2 quota probe — 返回 ok + primary_pct=0(剩余 100%)
    monkeypatch.setattr(
        manager,
        "check_codex_quota",
        lambda token: ("ok", {"primary_pct": 0, "weekly_pct": 0, "primary_total": 1000}),
    )
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "wsk-1")
    monkeypatch.setattr(manager.time, "time", lambda: 1234567890)
    monkeypatch.setattr(
        manager,
        "_is_email_in_team",
        lambda email: (_ for _ in ()).throw(AssertionError("should not check team membership separately")),
    )

    result = manager.reinvite_account(
        types.SimpleNamespace(browser=False),
        None,
        {"email": "tmp-user@example.com", "password": "secret"},
    )

    assert result is True
    # 第一条 update 是 quota probe 写 last_quota,第二条是 active 终态,
    # Round 12 wire-up C1 之后还会追一条 _auth_repair_reset(清 auth_retry_* 字段).
    assert len(updates) >= 1
    # 找到最后一条带 status 的 update,跳过 _auth_repair_reset 那条(无 status).
    status_updates = [u for u in updates if "status" in u[1]]
    assert status_updates, f"no status update recorded: {updates}"
    final_email, final_kwargs = status_updates[-1]
    assert final_email == "tmp-user@example.com"
    assert final_kwargs["status"] == accounts.STATUS_ACTIVE
    assert final_kwargs["last_active_at"] == 1234567890
    assert final_kwargs["auth_file"] == "/tmp/tmp-user@example.com.json"


def test_reinvite_account_stops_http_transport_session_before_oauth(monkeypatch):
    """HTTP-only Team API sessions must be stopped before browser OAuth starts."""
    updates = []

    class FakeHttpOnlyApi:
        browser = None

        def __init__(self):
            self.stopped = False

        def is_started(self):
            return True

        def stop(self):
            self.stopped = True

    api = FakeHttpOnlyApi()

    monkeypatch.setattr(
        manager,
        "login_codex_via_browser",
        lambda email, password, mail_client=None: {
            "email": email,
            "access_token": "token-1",
            "refresh_token": "refresh-1",
            "plan_type": "team",
            "plan_type_raw": "team",
        },
    )
    monkeypatch.setattr(manager, "save_auth_file", lambda bundle: f"/tmp/{bundle['email']}.json")
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "check_codex_quota", lambda token: ("ok", {"primary_pct": 0}))
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "wsk-1")
    monkeypatch.setattr(manager.time, "time", lambda: 1234567890)

    result = manager.reinvite_account(
        api,
        None,
        {"email": "tmp-user@example.com", "password": "secret"},
    )

    assert result is True
    assert api.stopped is True
    assert any(kwargs.get("status") == accounts.STATUS_ACTIVE for _email, kwargs in updates)


def test_reinvite_account_marks_auth_invalid_when_oauth_login_returns_non_team(monkeypatch):
    """SPEC-2 §3.3 plan_drift — 白名单内但 plan!=team → STATUS_AUTH_INVALID(不是 STANDBY)。

    旧行为 STANDBY 死循环踩同一个号 reinvite,SPEC-2 改为 AUTH_INVALID 让下游清账。
    """
    updates = []

    monkeypatch.setattr(
        manager,
        "login_codex_via_browser",
        lambda email, password, mail_client=None: {
            "email": email,
            "access_token": "token-1",
            "refresh_token": "refresh-1",
            "plan_type": "free",
            "plan_type_raw": "free",
        },
    )
    monkeypatch.setattr(
        manager,
        "update_account",
        lambda email, **kwargs: updates.append((email, kwargs)),
    )
    monkeypatch.setattr(
        manager,
        "_is_email_in_team",
        lambda email: (_ for _ in ()).throw(AssertionError("should not check team membership separately")),
    )

    result = manager.reinvite_account(
        types.SimpleNamespace(browser=False),
        None,
        {"email": "tmp-user@example.com", "password": ""},
    )

    assert result is False
    # Round 12 wire-up C1 — 失败路径除了 status=AUTH_INVALID, 还会调
    # _record_auth_repair_failure → 写 auth_retry_* + final status.
    # 关注 status=AUTH_INVALID 落盘即可,其余字段允许.
    auth_invalid_updates = [
        u for u in updates
        if u[1].get("status") == accounts.STATUS_AUTH_INVALID
    ]
    assert auth_invalid_updates, f"expected at least one AUTH_INVALID update, got {updates}"
    assert auth_invalid_updates[0] == (
        "tmp-user@example.com", {"status": accounts.STATUS_AUTH_INVALID, "auth_file": None}
    )
