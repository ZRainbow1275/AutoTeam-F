import pytest

from autoteam import accounts as accounts_mod
from autoteam import api, codex_auth
from autoteam import manager as manager_mod
from autoteam.signup_profile import SignupProfile


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


class _FakeElement:
    def __init__(self, page=None, *, visible=True):
        self.page = page
        self.visible = visible
        self.filled = []
        self.typed = []
        self.clicked = 0

    def is_visible(self, **_kwargs):
        return self.visible

    def is_editable(self, **_kwargs):
        return True

    def fill(self, value):
        self.filled.append(value)

    def click(self, **_kwargs):
        self.clicked += 1
        if self.page is not None:
            self.page.active_element = self
            if self is getattr(self.page, "submit", None):
                self.page.submit_about_you()


class _FakeLocator:
    def __init__(self, elements):
        self.elements = list(elements)

    @property
    def first(self):
        if self.elements:
            return self.elements[0]
        return _FakeElement(visible=False)

    def all(self):
        return self.elements


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, *_args, **_kwargs):
        return None

    def type(self, value, **_kwargs):
        self.page.active_element.typed.append(value)


class _FakeOAuthAboutYouPage:
    def __init__(self, *, spinbuttons=True, expected_order=None):
        self.url = "https://auth.openai.com/about-you"
        self.name = _FakeElement(self)
        self.submit = _FakeElement(self)
        self.label = _FakeElement(self)
        self.age = _FakeElement(self, visible=not spinbuttons)
        self.spinbuttons = [_FakeElement(self), _FakeElement(self), _FakeElement(self)] if spinbuttons else []
        self.active_element = None
        self.keyboard = _FakeKeyboard(self)
        self.expected_order = expected_order
        self.submit_attempts = 0

    def submit_about_you(self):
        self.submit_attempts += 1
        if self.expected_order is None:
            self.url = "https://auth.openai.com/oauth/authorize"
            return
        latest = [button.typed[-1] if button.typed else "" for button in self.spinbuttons]
        if latest == self.expected_order:
            self.url = "https://auth.openai.com/oauth/authorize"

    def locator(self, selector):
        if selector == '[role="spinbutton"]':
            return _FakeLocator(self.spinbuttons)
        if selector == 'input[name="name"]':
            return _FakeLocator([self.name])
        if selector.startswith("input[name=\"age\""):
            return _FakeLocator([self.age])
        if selector == "text=生日日期":
            return _FakeLocator([self.label])
        if "button" in selector:
            return _FakeLocator([self.submit])
        return _FakeLocator([])


def test_oauth_about_you_uses_signup_profile_snapshot(monkeypatch):
    profile = SignupProfile(
        full_name="Alice Carter",
        birthday={"year": "1989", "month": "07", "day": "14"},
        age="36",
    )
    page = _FakeOAuthAboutYouPage(spinbuttons=True)

    monkeypatch.setattr(codex_auth.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_auth, "_screenshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        codex_auth,
        "_wait_for_oauth_about_you_exit",
        lambda page, **_kwargs: "about-you" not in page.url,
    )

    assert codex_auth._complete_oauth_about_you(page, signup_profile=profile) is True
    assert page.name.filled == ["Alice Carter"]
    assert [button.typed for button in page.spinbuttons] == [["1989"], ["07"], ["14"]]
    assert page.submit.clicked == 1


def test_oauth_about_you_retries_birthday_orders_until_page_exits(monkeypatch):
    profile = SignupProfile(
        full_name="Alice Carter",
        birthday={"year": "1989", "month": "07", "day": "14"},
        age="36",
    )
    page = _FakeOAuthAboutYouPage(spinbuttons=True, expected_order=["07", "14", "1989"])

    monkeypatch.setattr(codex_auth.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_auth, "_screenshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        codex_auth,
        "_wait_for_oauth_about_you_exit",
        lambda page, **_kwargs: "about-you" not in page.url,
    )

    assert codex_auth._complete_oauth_about_you(page, signup_profile=profile) is True
    assert page.submit_attempts == 2
    assert [button.typed for button in page.spinbuttons] == [
        ["1989", "07"],
        ["07", "14"],
        ["14", "1989"],
    ]


def test_oauth_about_you_returns_false_when_profile_page_never_exits(monkeypatch):
    profile = SignupProfile(
        full_name="Alice Carter",
        birthday={"year": "1989", "month": "07", "day": "14"},
        age="36",
    )
    page = _FakeOAuthAboutYouPage(spinbuttons=True, expected_order=["never", "matches", "this"])

    monkeypatch.setattr(codex_auth.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_auth, "_screenshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        codex_auth,
        "_wait_for_oauth_about_you_exit",
        lambda page, **_kwargs: "about-you" not in page.url,
    )

    assert codex_auth._complete_oauth_about_you(page, signup_profile=profile) is False
    assert page.submit_attempts == 3


def test_create_account_direct_reuses_signup_profile_for_register_and_oauth(monkeypatch):
    profile = SignupProfile(
        full_name="Noah Bennett",
        birthday={"year": "1991", "month": "04", "day": "09"},
        age="35",
    )
    captured = {}

    class _FakeMailClient:
        def create_temp_email(self):
            return "mail-id", "new-user@example.com"

        def delete_account(self, *_args, **_kwargs):
            return None

    def fake_register_once(_mail_client, email, password, *, cloudmail_account_id=None, signup_profile=None):
        captured["register"] = {
            "email": email,
            "password": password,
            "cloudmail_account_id": cloudmail_account_id,
            "signup_profile": signup_profile,
        }
        return True, "session-token"

    def fake_post_oauth(email, password, mail_client, **kwargs):
        captured["oauth"] = {
            "email": email,
            "password": password,
            "mail_client": mail_client,
            **kwargs,
        }
        return email

    monkeypatch.setattr(manager_mod, "generate_signup_profile", lambda: profile)
    monkeypatch.setattr(manager_mod, "random_password", lambda: "Password123!")
    monkeypatch.setattr(manager_mod, "_register_direct_once", fake_register_once)
    monkeypatch.setattr(manager_mod, "_run_post_register_oauth", fake_post_oauth)
    monkeypatch.setattr(manager_mod, "add_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager_mod, "get_chatgpt_account_id", lambda: "workspace-account")

    result = manager_mod.create_account_direct(mail_client=_FakeMailClient(), leave_workspace=True)

    assert result == "new-user@example.com"
    assert captured["register"]["signup_profile"] is profile
    assert captured["oauth"]["signup_profile"] is profile
    assert captured["oauth"]["chatgpt_session_token"] == "session-token"
    assert captured["oauth"]["leave_workspace"] is True


def test_run_post_register_oauth_passes_signup_profile_to_team_oauth(monkeypatch):
    profile = SignupProfile(
        full_name="Mia Wilson",
        birthday={"year": "1990", "month": "02", "day": "03"},
        age="36",
    )
    login_calls = []
    updates = []

    class _FakeChatGPTTeamAPI:
        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(manager_mod, "ChatGPTTeamAPI", _FakeChatGPTTeamAPI)
    monkeypatch.setattr(
        "autoteam.master_health.is_master_subscription_healthy",
        lambda _api: (True, "active", {}),
    )
    monkeypatch.setattr(
        manager_mod,
        "login_codex_via_browser",
        lambda *args, **kwargs: login_calls.append({"args": args, "kwargs": kwargs})
        or {
            "plan_type": "team",
            "plan_type_raw": "team",
            "plan_supported": True,
            "account_id": "codex-account",
        },
    )
    monkeypatch.setattr(manager_mod, "save_auth_file", lambda _bundle: "auths/new-user.json")
    monkeypatch.setattr(manager_mod, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager_mod, "get_chatgpt_account_id", lambda: "workspace-account")

    result = manager_mod._run_post_register_oauth(
        "new-user@example.com",
        "Password123!",
        mail_client=object(),
        signup_profile=profile,
    )

    assert result == "new-user@example.com"
    assert login_calls[0]["kwargs"]["signup_profile"] is profile
    assert updates[-1][0] == "new-user@example.com"
    assert updates[-1][1]["status"] == accounts_mod.STATUS_ACTIVE
