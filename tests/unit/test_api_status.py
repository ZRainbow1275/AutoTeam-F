import json
import threading
import time

from autoteam import accounts, api, config


def test_get_status_normalizes_main_account_status_from_saved_auth(tmp_path, monkeypatch):
    main_email = "owner@example.com"
    auth_file = tmp_path / "codex-main.json"
    auth_file.write_text(json.dumps({"access_token": "token-main"}), encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": main_email,
                "status": "exhausted",
                "auth_file": "/app/auths/codex-main.json",
                "last_quota": {
                    "primary_pct": 8,
                    "primary_resets_at": 1710000000,
                    "weekly_pct": 1,
                    "weekly_resets_at": 1710600000,
                },
            }
        ],
    )
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == main_email)
    monkeypatch.setattr("autoteam.codex_auth.get_saved_main_auth_file", lambda: str(auth_file))
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda access_token: (
            "ok",
            {
                "primary_pct": 8,
                "primary_resets_at": 1710000000,
                "weekly_pct": 1,
                "weekly_resets_at": 1710600000,
            },
        ),
    )

    result = api.get_status()

    assert result["quota_cache"][main_email]["primary_pct"] == 8
    assert result["accounts"][0]["is_main_account"] is True
    assert result["accounts"][0]["status"] == "active"
    assert result["summary"] == {
        "active": 1,
        "standby": 0,
        "exhausted": 0,
        "pending": 0,
        "personal": 0,
        "auth_invalid": 0,
        "orphan": 0,
        "disabled": 0,
        "total": 1,
    }


def test_get_status_survives_runtime_resource_probe_failure(monkeypatch):
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(
        api,
        "collect_runtime_resource_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("proc unavailable")),
    )

    result = api.get_status()

    assert result["accounts"] == []
    assert result["runtime_resources"] == {"error": "runtime_resource_snapshot_failed"}


def test_get_status_includes_ipv6_pool_status(monkeypatch):
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(
        "autoteam.ipv6_pool.ipv6_pool.status",
        lambda: {"enabled": True, "required": False, "ok": True, "count": 2, "entries": []},
    )

    result = api.get_status()

    assert result["ipv6_pool"]["enabled"] is True
    assert result["ipv6_pool"]["count"] == 2


def test_get_status_exposes_clipproxy_and_rotation_validation(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-child.json"
    auth_file.write_text(json.dumps({"access_token": "token-child"}), encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "child@example.com", "status": accounts.STATUS_ACTIVE, "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {"primary_pct": 20}))
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda: {
            "ok": False,
            "safe_read_only": True,
            "management_api": {"ok": True},
            "provider_auth": {
                "ok": False,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "no_provider_auth",
                "total": 0,
                "available": 0,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )
    monkeypatch.setattr(
        api,
        "_rotation_validation_cooldown",
        {"next_rotate_after": 0.0, "recorded_at": 123.0, "severity": "ok", "reason": ""},
    )

    result = api.get_status()

    assert result["summary"]["active"] == 1
    assert result["cliproxy"]["safe_read_only"] is True
    assert result["cliproxy"]["provider_auth"]["reason"] == "no_provider_auth"
    assert result["rotation_validation"]["severity"] == "ok"
    assert result["rotation_validation"]["cooldown_remaining_seconds"] == 0


def test_get_playwright_context_options_uses_fingerprint_constants(monkeypatch):
    monkeypatch.setattr(config, "PLAYWRIGHT_USER_AGENT", "AutoTeamTest/1.0")
    monkeypatch.setattr(config, "PLAYWRIGHT_LOCALE", "zh-CN")
    monkeypatch.setattr(config, "PLAYWRIGHT_TIMEZONE_ID", "Asia/Shanghai")
    monkeypatch.setattr(config, "PLAYWRIGHT_VIEWPORT_WIDTH", 1440)
    monkeypatch.setattr(config, "PLAYWRIGHT_VIEWPORT_HEIGHT", 900)
    monkeypatch.setattr(config, "PLAYWRIGHT_DEVICE_SCALE_FACTOR", 2)
    monkeypatch.setattr(config, "PLAYWRIGHT_COLOR_SCHEME", "light")

    options = config.get_playwright_context_options()

    assert options["viewport"] == {"width": 1440, "height": 900}
    assert options["user_agent"] == "AutoTeamTest/1.0"
    assert options["locale"] == "zh-CN"
    assert options["timezone_id"] == "Asia/Shanghai"
    assert options["device_scale_factor"] == 2
    assert options["color_scheme"] == "light"
    assert options["extra_http_headers"] == {"Accept-Language": "zh-CN,zh;q=0.9"}


def test_auto_check_cooldown_does_not_delay_real_team_shortage(tmp_path, monkeypatch):

    auth_files = []
    for idx in range(1):
        auth_file = tmp_path / f"active-{idx}.json"
        auth_file.write_text(json.dumps({"access_token": f"token-{idx}"}), encoding="utf-8")
        auth_files.append(auth_file)

    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", time.time())
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": f"active-{idx}@example.com", "status": "active", "auth_file": str(auth_files[idx])}
            for idx in range(1)
        ],
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    api._auto_check_loop()

    assert len(started) == 1
    command, params, args, kwargs = started[0]
    assert command == "auto-fill"
    assert params == {"target_seats": 3}
    assert args == (3,)
    assert kwargs == {"background_post_sync": True}


def test_auto_check_cooldown_keeps_full_team_from_refilling(tmp_path, monkeypatch):

    auth_files = []
    for idx in range(1):
        auth_file = tmp_path / f"active-{idx}.json"
        auth_file.write_text(json.dumps({"access_token": f"token-{idx}"}), encoding="utf-8")
        auth_files.append(auth_file)

    started = []
    probed = []

    def fake_team_count(*args, **kwargs):
        probed.append((args, kwargs))
        return 3

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", time.time())
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", fake_team_count)
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": f"active-{idx}@example.com", "status": "active", "auth_file": str(auth_files[idx])}
            for idx in range(1)
        ],
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda _token: ("ok", {"primary_pct": 10, "primary_resets_at": 0, "weekly_pct": 1}),
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    api._auto_check_loop()

    assert probed
    assert started == []


def test_sanitize_account_keeps_exportable_main_account_active_without_live_quota(tmp_path, monkeypatch):
    main_email = "owner@example.com"
    auth_file = tmp_path / "codex-main.json"
    auth_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == main_email)
    monkeypatch.setattr("autoteam.codex_auth.get_saved_main_auth_file", lambda: str(auth_file))

    sanitized = api._sanitize_account(
        {"email": main_email, "status": "exhausted", "auth_file": "/app/auths/missing.json"}
    )

    assert sanitized["is_main_account"] is True
    assert sanitized["status"] == "active"


def test_sanitize_account_masks_disabled_non_main_status(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)

    sanitized = api._sanitize_account({"email": "user@example.com", "status": "active", "disabled": True})

    assert sanitized["raw_status"] == "active"
    assert sanitized["status"] == "disabled"
    assert sanitized["disabled"] is True


def test_post_rotate_runs_final_sync_in_background(monkeypatch):
    started = []
    rotate_calls = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, func, params, args, kwargs))
        return {"task_id": "rotate-task", "command": command, "params": params}

    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr(
        "autoteam.manager.cmd_rotate",
        lambda *args, **kwargs: rotate_calls.append((args, kwargs)),
    )

    result = api.post_rotate(api.TaskParams(target=3))

    assert result["task_id"] == "rotate-task"
    assert len(started) == 1
    command, func, params, args, kwargs = started[0]
    assert command == "rotate"
    assert params == {"target": 3}
    assert args == (3,)
    assert kwargs == {}

    func(3)
    assert rotate_calls == [((3,), {"force_auth_repair": True, "background_post_sync": True})]


def test_disable_and_enable_account_toggle_local_flag(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "member@example.com", "status": "standby", "disabled": False},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    disabled_result = api.post_disable_account("member@example.com")
    enabled_result = api.post_enable_account("member@example.com")

    assert disabled_result["disabled"] is True
    assert disabled_result["account"]["status"] == "disabled"
    assert disabled_result["account"]["raw_status"] == "standby"
    assert enabled_result["disabled"] is False
    assert enabled_result["account"]["status"] == "standby"
    assert accounts.find_account(accounts.load_accounts(), "member@example.com")["disabled"] is False


def test_bulk_disable_accounts_updates_multiple_rows_and_skips_non_targets(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "first@example.com", "status": "standby", "disabled": False},
            {"email": "second@example.com", "status": "active", "disabled": False},
            {"email": "already@example.com", "status": "standby", "disabled": True},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    result = api.post_bulk_disable_accounts(
        api.AccountDisableParams(
            emails=[
                "first@example.com",
                "second@example.com",
                "already@example.com",
                "owner@example.com",
                "missing@example.com",
                "first@example.com",
            ]
        )
    )

    stored = {acc["email"]: acc for acc in accounts.load_accounts()}

    assert result["updated_count"] == 2
    assert result["updated_emails"] == ["first@example.com", "second@example.com"]
    assert result["unchanged_emails"] == ["already@example.com"]
    assert result["skipped_main_accounts"] == ["owner@example.com"]
    assert result["missing_emails"] == ["missing@example.com"]
    assert stored["first@example.com"]["disabled"] is True
    assert stored["second@example.com"]["disabled"] is True
    assert stored["already@example.com"]["disabled"] is True
    assert stored["owner@example.com"]["disabled"] is False


def test_bulk_enable_accounts_updates_disabled_rows_only(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "first@example.com", "status": "standby", "disabled": True},
            {"email": "second@example.com", "status": "active", "disabled": True},
            {"email": "already@example.com", "status": "standby", "disabled": False},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    result = api.post_bulk_enable_accounts(
        api.AccountDisableParams(
            emails=[
                "first@example.com",
                "second@example.com",
                "already@example.com",
                "owner@example.com",
                "missing@example.com",
            ]
        )
    )

    stored = {acc["email"]: acc for acc in accounts.load_accounts()}

    assert result["updated_count"] == 2
    assert result["updated_emails"] == ["first@example.com", "second@example.com"]
    assert result["unchanged_emails"] == ["already@example.com"]
    assert result["skipped_main_accounts"] == ["owner@example.com"]
    assert result["missing_emails"] == ["missing@example.com"]
    assert stored["first@example.com"]["disabled"] is False
    assert stored["second@example.com"]["disabled"] is False
    assert stored["already@example.com"]["disabled"] is False
    assert stored["owner@example.com"]["disabled"] is False


def test_get_status_counts_disabled_and_skips_disabled_quota_checks(tmp_path, monkeypatch):
    enabled_auth = tmp_path / "enabled.json"
    disabled_auth = tmp_path / "disabled.json"
    enabled_auth.write_text(json.dumps({"access_token": "token-enabled"}), encoding="utf-8")
    disabled_auth.write_text(json.dumps({"access_token": "token-disabled"}), encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": "enabled@example.com", "status": "active", "auth_file": str(enabled_auth), "disabled": False},
            {"email": "disabled@example.com", "status": "active", "auth_file": str(disabled_auth), "disabled": True},
        ],
    )
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)

    seen_tokens = []

    def fake_check_quota(access_token):
        seen_tokens.append(access_token)
        return (
            "ok",
            {
                "primary_pct": 15,
                "primary_resets_at": 1710000000,
                "weekly_pct": 5,
                "weekly_resets_at": 1710600000,
            },
        )

    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", fake_check_quota)

    result = api.get_status()

    assert seen_tokens == ["token-enabled"]
    assert {item["email"]: item["status"] for item in result["accounts"]} == {
        "enabled@example.com": "active",
        "disabled@example.com": "disabled",
    }
    assert result["summary"] == {
        "active": 1,
        "standby": 0,
        "exhausted": 0,
        "pending": 0,
        "personal": 0,
        "auth_invalid": 0,
        "orphan": 0,
        "disabled": 1,
        "total": 2,
    }


def test_post_setup_save_keeps_cpa_url_required_and_generates_api_key(monkeypatch):
    written = {}

    def fake_write_env(key, value):
        written[key] = value

    monkeypatch.setattr("autoteam.setup_wizard._write_env", fake_write_env)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cloudmail", lambda: True)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", lambda: True)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "generated-token")
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "")
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    result = api.post_setup_save(
        api.SetupConfig(
            CLOUDMAIL_BASE_URL="http://mail.example.com",
            CLOUDMAIL_EMAIL="admin@example.com",
            CLOUDMAIL_PASSWORD="secret",
            CLOUDMAIL_DOMAIN="@example.com",
            CPA_URL="",
            CPA_KEY="key-1",
            PLAYWRIGHT_PROXY_URL="",
            PLAYWRIGHT_PROXY_BYPASS="",
            API_KEY="",
        )
    )

    assert written["CPA_URL"] == "http://127.0.0.1:8317"
    assert written["API_KEY"] == "generated-token"
    assert result["api_key"] == "generated-token"
    assert api.API_KEY == "generated-token"
