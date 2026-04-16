import json

from autoteam import admin_state


def test_load_admin_state_migrates_legacy_session_file(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    legacy_file = tmp_path / "session"
    legacy_file.write_text("legacy-session-token", encoding="utf-8")

    monkeypatch.setattr(admin_state, "STATE_FILE", state_file)
    monkeypatch.setattr(admin_state, "LEGACY_SESSION_FILE", legacy_file)

    state = admin_state.load_admin_state()

    assert state["session_token"] == "legacy-session-token"
    assert state_file.exists()
    assert not legacy_file.exists()


def test_update_admin_state_normalizes_and_summary_uses_saved_values(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    legacy_file = tmp_path / "session"

    monkeypatch.setattr(admin_state, "STATE_FILE", state_file)
    monkeypatch.setattr(admin_state, "LEGACY_SESSION_FILE", legacy_file)

    saved = admin_state.update_admin_state(
        email="owner@example.com",
        session_token="token-1",
        password="secret",
        account_id="123e4567-e89b-12d3-a456-426614174000",
        workspace_name="Team A",
    )

    assert saved["updated_at"] is not None
    summary = admin_state.get_admin_state_summary()

    assert summary == {
        "configured": True,
        "email": "owner@example.com",
        "account_id": "123e4567-e89b-12d3-a456-426614174000",
        "workspace_name": "Team A",
        "session_present": True,
        "password_saved": True,
        "updated_at": saved["updated_at"],
    }

    raw_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw_state["workspace_name"] == "Team A"


def test_get_chatgpt_account_id_falls_back_to_env_when_state_id_is_invalid(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    legacy_file = tmp_path / "session"
    state_file.write_text(
        json.dumps(
            {
                "email": "owner@example.com",
                "session_token": "token-1",
                "account_id": "user-123",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(admin_state, "STATE_FILE", state_file)
    monkeypatch.setattr(admin_state, "LEGACY_SESSION_FILE", legacy_file)
    monkeypatch.setenv("CHATGPT_ACCOUNT_ID", "123e4567-e89b-12d3-a456-426614174999")

    assert admin_state.get_chatgpt_account_id() == "123e4567-e89b-12d3-a456-426614174999"


def test_clear_admin_state_keeps_state_file_for_symlink_safety(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    legacy_file = tmp_path / "session"
    state_file.write_text(json.dumps({"session_token": "token-1"}), encoding="utf-8")
    legacy_file.write_text("legacy-token", encoding="utf-8")

    monkeypatch.setattr(admin_state, "STATE_FILE", state_file)
    monkeypatch.setattr(admin_state, "LEGACY_SESSION_FILE", legacy_file)

    admin_state.clear_admin_state()

    assert state_file.exists()
    assert json.loads(state_file.read_text(encoding="utf-8")) == {}
    assert not legacy_file.exists()
