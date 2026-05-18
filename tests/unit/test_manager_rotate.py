from autoteam import manager


class _FakeChatGPT:
    def __init__(self):
        self.browser = True
        self.started = 0
        self.stopped = 0

    def start(self):
        self.browser = True
        self.started += 1

    def stop(self):
        self.browser = False
        self.stopped += 1


class _FakeMailClient:
    def login(self):
        return None


def test_cmd_rotate_skips_google_accounts_during_auto_reuse(monkeypatch):
    chatgpt = _FakeChatGPT()
    count_values = iter([2, 3])
    events = []

    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(manager, "get_team_member_count", lambda _chatgpt: next(count_values))
    monkeypatch.setattr(
        manager,
        "get_standby_accounts",
        lambda: [
            {"email": "bubblehuntr@gmail.com"},
            {"email": "old-2@example.com"},
        ],
    )
    monkeypatch.setattr(
        manager,
        "reinvite_account",
        lambda _chatgpt, _mail, acc: events.append(("reinvite", acc["email"])) or True,
    )
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda _chatgpt, _mail: events.append(("create", None)) or True,
    )
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: events.append(("sync_to_cpa", None)))

    manager.cmd_rotate(target_seats=3)

    assert events == [
        ("sync_account_states", None),
        ("cmd_check", None),
        ("reinvite", "old-2@example.com"),
        ("sync_to_cpa", None),
    ]
    assert chatgpt.stopped == 1


def test_cmd_rotate_can_defer_final_sync_when_running_as_api_task(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(manager, "get_team_member_count", lambda _chatgpt: 3)
    monkeypatch.setattr(manager, "_count_pool_active_accounts", lambda *args, **kwargs: 2)
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda _chatgpt, _mail: (_ for _ in ()).throw(AssertionError("should not create when member count is full")),
    )
    monkeypatch.setattr(
        manager,
        "sync_to_cpa",
        lambda: (_ for _ in ()).throw(AssertionError("final sync should be scheduled, not run inline")),
    )
    monkeypatch.setattr(
        manager,
        "_schedule_post_task_sync",
        lambda stage_label: events.append(("schedule_post_sync", stage_label)),
    )

    manager.cmd_rotate(target_seats=3, background_post_sync=True)

    assert events == [
        ("sync_account_states", None),
        ("cmd_check", None),
        ("schedule_post_sync", "[轮转]"),
    ]
