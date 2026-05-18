from autoteam import invite


class _FakeElement:
    def __init__(self, *, visible=True):
        self._visible = visible

    def is_visible(self, timeout=0):
        return self._visible


class _FakeLocatorGroup:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


def test_recover_blank_invite_page_refreshes_post_auth_blank_page(monkeypatch):
    class _BlankThenReadyPage:
        url = "https://chatgpt.com/"

        def __init__(self):
            self.reloads = 0
            self.waits = 0

        def inner_text(self, selector):
            assert selector == "body"
            return "" if self.reloads == 0 else "Welcome to ChatGPT"

        def locator(self, selector):
            if selector == 'input, button, a, textarea, select, [role="button"]':
                return _FakeLocatorGroup([] if self.reloads == 0 else [_FakeElement(visible=True)])
            return _FakeLocatorGroup([])

        def reload(self, wait_until=None, timeout=0):
            self.reloads += 1

        def wait_for_load_state(self, *_args, **_kwargs):
            self.waits += 1

    page = _BlankThenReadyPage()
    screenshots = []
    monkeypatch.setattr(invite.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(invite, "wait_for_cloudflare", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(invite, "screenshot", lambda _page, name: screenshots.append(name))

    assert invite._is_probably_blank_page(page) is True
    assert invite._recover_blank_invite_page(page, "final", attempts=2) is True

    assert page.reloads == 1
    assert page.waits == 1
    assert invite._is_probably_blank_page(page) is False
    assert screenshots == ["reg_blank_final_1_before.png", "reg_blank_final_1_after.png"]


def test_recover_blank_invite_page_skips_visible_page(monkeypatch):
    class _ReadyPage:
        url = "https://chatgpt.com/"

        def __init__(self):
            self.reloads = 0

        def inner_text(self, selector):
            assert selector == "body"
            return "Welcome"

        def locator(self, selector):
            if selector == 'input, button, a, textarea, select, [role="button"]':
                return _FakeLocatorGroup([_FakeElement(visible=True)])
            return _FakeLocatorGroup([])

        def reload(self, wait_until=None, timeout=0):
            self.reloads += 1

    page = _ReadyPage()
    monkeypatch.setattr(invite, "screenshot", lambda *_args, **_kwargs: None)

    assert invite._recover_blank_invite_page(page, "final", attempts=2) is False
    assert page.reloads == 0
