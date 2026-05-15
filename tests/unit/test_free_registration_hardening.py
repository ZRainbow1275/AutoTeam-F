import pytest

from autoteam import accounts as accounts_mod
from autoteam import api


def test_post_fill_personal_preflight_counts_auth_invalid_as_team_seat(monkeypatch):
    accounts = [
        {"email": "active-1@example.com", "status": accounts_mod.STATUS_ACTIVE},
        {"email": "exhausted-1@example.com", "status": accounts_mod.STATUS_EXHAUSTED},
        {"email": "auth-invalid-1@example.com", "status": accounts_mod.STATUS_AUTH_INVALID},
        {"email": "auth-invalid-2@example.com", "status": accounts_mod.STATUS_AUTH_INVALID},
    ]

    monkeypatch.setattr(accounts_mod, "load_accounts", lambda: accounts)
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: pytest.fail("_start_task should not run"))

    with pytest.raises(api.HTTPException) as exc:
        api.post_fill(api.TaskParams(target=1, leave_workspace=True))

    assert exc.value.status_code == 409
    assert "Team 子号已满 4/4" in str(exc.value.detail)
