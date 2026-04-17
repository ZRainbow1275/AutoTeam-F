import types

from autoteam import accounts, manager


def test_reinvite_account_uses_unified_oauth_login_and_marks_active(monkeypatch):
    updates = []

    monkeypatch.setattr(
        manager,
        "login_codex_via_browser",
        lambda email, password, mail_client=None: {
            "email": email,
            "access_token": "token-1",
            "refresh_token": "refresh-1",
            "plan_type": "team",
        },
    )
    monkeypatch.setattr(manager, "save_auth_file", lambda bundle: f"/tmp/{bundle['email']}.json")
    monkeypatch.setattr(
        manager,
        "update_account",
        lambda email, **kwargs: updates.append((email, kwargs)),
    )
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
    assert updates == [
        (
            "tmp-user@example.com",
            {
                "status": accounts.STATUS_ACTIVE,
                "last_active_at": 1234567890,
                "auth_file": "/tmp/tmp-user@example.com.json",
            },
        )
    ]


def test_reinvite_account_marks_standby_when_oauth_login_returns_non_team(monkeypatch):
    updates = []

    monkeypatch.setattr(
        manager,
        "login_codex_via_browser",
        lambda email, password, mail_client=None: {
            "email": email,
            "access_token": "token-1",
            "refresh_token": "refresh-1",
            "plan_type": "free",
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
    assert updates == [("tmp-user@example.com", {"status": accounts.STATUS_STANDBY})]
