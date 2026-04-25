"""覆盖 cmd_check 新增 include_standby 开关 + _probe_standby_quota。

原 task #2:
- include_standby=False(默认) 不探测 standby 池,保持向后兼容
- include_standby=True 调用 _probe_standby_quota,遍历 standby + 限速 + 24h 去重
- 401/403 类 auth_error → STATUS_AUTH_INVALID

task #3 修复回归:
- network_error 不写 last_quota_check_at(允许下一轮重试),不改 status,只 log
- 未知 status 防御分支不写时间戳,避免去重逻辑卡住未来探测
- check_codex_quota 仅 401/403 才返回 auth_error;5xx/429/超时/SSL 错误归 network_error
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


# ---------------------------------------------------------------------------
# task #3 回归保护:network_error 必须不动 status / 不写时间戳
# ---------------------------------------------------------------------------


def test_probe_network_error_keeps_status_unchanged(tmp_path, monkeypatch):
    """5xx/timeout/SSL 异常 → status_str="network_error" → **不写** last_quota_check_at,
    **不改** status。避免一次网络抖动让整批 standby 在 24h 内不再被探测。"""
    stby = {
        "email": "flaky@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "flaky"),
        "last_quota_check_at": None,
    }
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [stby])
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("network_error", None))
    monkeypatch.setattr(manager.time, "sleep", lambda *_a: None)

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    manager._probe_standby_quota()

    # network_error 分支必须既不调 update_account(不改 status),也不写时间戳
    assert updates == [], f"network_error 不应触发 update_account,但收到: {updates}"


def test_probe_unknown_status_does_not_write_timestamp(tmp_path, monkeypatch):
    """未知 status_str → 防御分支不写 last_quota_check_at,避免 24h 去重屏蔽未来真实探测。"""
    stby = {
        "email": "weird@example.com",
        "status": STATUS_STANDBY,
        "auth_file": _fake_auth_file(tmp_path, "weird"),
        "last_quota_check_at": None,
    }
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [stby])
    monkeypatch.setattr(manager, "_check_and_refresh", lambda acc: ("future_unknown_status", None))
    monkeypatch.setattr(manager.time, "sleep", lambda *_a: None)

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    manager._probe_standby_quota()

    assert updates == [], f"未知 status 不应触发 update_account,但收到: {updates}"


def test_probe_auth_error_only_for_401_403(monkeypatch):
    """check_codex_quota 必须严格区分:
    - 401/403            → auth_error
    - 5xx / 429 / 4xx其他 → network_error
    - timeout / SSL / Connection 异常 → network_error
    - 200 但 JSON 解析失败 → network_error
    - 200 + 正常 payload  → ok
    """
    import requests

    from autoteam import codex_auth

    # 关闭外部依赖:account_id 探测
    monkeypatch.setattr(codex_auth, "get_chatgpt_account_id", lambda: None)

    class FakeResp:
        def __init__(self, status_code, payload=None, raise_json=False, text=""):
            self.status_code = status_code
            self._payload = payload
            self._raise_json = raise_json
            self.text = text

        def json(self):
            if self._raise_json:
                raise ValueError("not json")
            return self._payload

    def make_get(resp_or_exc):
        def fake_get(*_a, **_kw):
            if isinstance(resp_or_exc, Exception):
                raise resp_or_exc
            return resp_or_exc

        return fake_get

    # check_codex_quota 内部用 `import requests` 后调 requests.get,
    # 函数内的 import 会查 sys.modules['requests'],因此在 requests 模块上 monkeypatch.get
    # 即可拦截调用。

    # --- 401 → auth_error
    monkeypatch.setattr(requests, "get", make_get(FakeResp(401)))
    assert codex_auth.check_codex_quota("tok")[0] == "auth_error"

    # --- 403 → auth_error
    monkeypatch.setattr(requests, "get", make_get(FakeResp(403)))
    assert codex_auth.check_codex_quota("tok")[0] == "auth_error"

    # --- 500 → network_error(关键回归:以前会被误判 auth_error)
    monkeypatch.setattr(requests, "get", make_get(FakeResp(500, text="upstream down")))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- 502 / 503 / 504 → network_error
    for code in (502, 503, 504):
        monkeypatch.setattr(requests, "get", make_get(FakeResp(code)))
        assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- 429 → network_error(限流是临时性的)
    monkeypatch.setattr(requests, "get", make_get(FakeResp(429)))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- 418 (其他 4xx,非 401/403/429) → network_error(保守归类)
    monkeypatch.setattr(requests, "get", make_get(FakeResp(418)))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- ConnectionError → network_error
    monkeypatch.setattr(requests, "get", make_get(requests.exceptions.ConnectionError("dns boom")))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- Timeout → network_error
    monkeypatch.setattr(requests, "get", make_get(requests.exceptions.Timeout("slow")))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- SSLError → network_error
    monkeypatch.setattr(requests, "get", make_get(requests.exceptions.SSLError("ssl fail")))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- 200 但 JSON 解析失败 → network_error(以前会被误判 auth_error)
    monkeypatch.setattr(requests, "get", make_get(FakeResp(200, raise_json=True)))
    assert codex_auth.check_codex_quota("tok")[0] == "network_error"

    # --- 200 + 健康 payload → ok
    healthy = {
        "rate_limit": {
            "primary_window": {"used_percent": 10, "reset_at": 0},
            "secondary_window": {"used_percent": 5, "reset_at": 0},
            "limit_reached": False,
        }
    }
    monkeypatch.setattr(requests, "get", make_get(FakeResp(200, payload=healthy)))
    status, info = codex_auth.check_codex_quota("tok")
    assert status == "ok"
    assert isinstance(info, dict)
    assert info["primary_pct"] == 10
