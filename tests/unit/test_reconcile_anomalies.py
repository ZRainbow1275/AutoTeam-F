"""覆盖 task #3:_reconcile_team_members 识别残废 / 错位 / 耗尽未抛弃 / ghost + dry_run。

直接 mock chatgpt_api._api_fetch + remove_from_team + update_account,构造不同 workspace
成员 × 本地账号状态的组合,断言 result dict 里对应分支命中。
"""

from __future__ import annotations

import json
import types

from autoteam import manager
from autoteam.accounts import (
    STATUS_ACTIVE,
    STATUS_AUTH_INVALID,
    STATUS_EXHAUSTED,
    STATUS_ORPHAN,
    STATUS_STANDBY,
)


def _make_fake_chatgpt(members):
    """构造一个 fake ChatGPTTeamAPI,/users 返回给定成员列表。"""
    body = json.dumps({"items": members})

    def fake_api_fetch(method, path, body_=None):
        if method == "GET" and path.endswith("/users"):
            return {"status": 200, "body": body}
        return {"status": 200, "body": "{}"}

    fake = types.SimpleNamespace(browser=True, _api_fetch=fake_api_fetch)
    return fake


def _common_monkeypatch(monkeypatch, accounts_list, *, main_email="owner@example.com"):
    """统一 patch:account_id 有值,主号识别走 _is_main_account_email。"""
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-xxx")
    monkeypatch.setattr(manager, "load_accounts", lambda: accounts_list)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda e: (e or "").lower() == main_email.lower())
    # 避免第二轮 /users 触发 real logic:返回同样 body
    # 已经由 fake chatgpt 的 _api_fetch 处理
    # time.time 稳定化
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000.0)


def test_reconcile_orphan_kicks_when_no_auth(tmp_path, monkeypatch):
    """workspace 有 + 本地 active + auth_file 缺失 → 残废,按默认 KICK_ORPHAN=true 被 KICK。"""
    acc = {
        "email": "orphan@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,  # 关键:缺 auth
    }
    fake = _make_fake_chatgpt([{"email": "orphan@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    # 强制走 KICK 分支(关闭人工介入)。manager 内部 `from autoteam.config import ...`
    # 走 config 模块命名空间,这里确保 config 默认 True(实际就是 True,这行保险用)
    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)
    # _find_team_auth_file 返回 None (auths 目录里找不到)
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)

    kicked = []

    def fake_remove(_api, email, **kw):
        kicked.append(email)
        return "removed"

    monkeypatch.setattr(manager, "remove_from_team", fake_remove)
    monkeypatch.setattr(manager, "update_account", lambda *_a, **_kw: None)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "orphan@example.com" in result["orphan_kicked"]
    assert kicked == ["orphan@example.com"]


def test_reconcile_status_drift_local_standby_workspace_active(tmp_path, monkeypatch):
    """workspace=active + 本地=standby + auth_file 存在 → 错位,修正 active,不 KICK。"""
    auth_path = tmp_path / "codex-drift@example.com-team-1.json"
    auth_path.write_text("{}", encoding="utf-8")

    acc = {
        "email": "drift@example.com",
        "status": STATUS_STANDBY,
        "auth_file": str(auth_path),
    }
    fake = _make_fake_chatgpt([{"email": "drift@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    # KICK 被调用则测试失败
    def _forbid_kick(*_a, **_kw):
        raise AssertionError("drift case must not KICK")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "drift@example.com" in result["misaligned_fixed"]
    assert updates  # 至少一次 update_account
    # 修正为 STATUS_ACTIVE
    assert any(kw.get("status") == STATUS_ACTIVE for _email, kw in updates)


def test_reconcile_marks_exhausted_when_quota_zero(tmp_path, monkeypatch):
    """workspace=active + 本地=active + auth_file 有 + last_quota 5h/周均 100% → 标 EXHAUSTED,**不 KICK**。"""
    auth_path = tmp_path / "codex-eaten@example.com.json"
    auth_path.write_text("{}", encoding="utf-8")

    acc = {
        "email": "eaten@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": str(auth_path),
        "last_quota": {"primary_pct": 100, "weekly_pct": 100},
    }
    fake = _make_fake_chatgpt([{"email": "eaten@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("exhausted snapshot must not KICK immediately")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "eaten@example.com" in result["exhausted_marked"]
    # 必须带 status=EXHAUSTED 且写 quota_exhausted_at
    assert any(kw.get("status") == STATUS_EXHAUSTED and kw.get("quota_exhausted_at") is not None for _e, kw in updates)


def test_reconcile_dry_run_does_not_mutate(tmp_path, monkeypatch):
    """dry_run=True 即便识别出需要 KICK/update 的异常,也绝不实际 kick 或写 accounts.json。"""
    acc = {
        "email": "ghost-local@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,
    }
    # 同时有本地不存在的 ghost 成员
    fake = _make_fake_chatgpt(
        [
            {"email": "ghost-local@example.com"},
            {"email": "completely-unknown@example.com"},  # ghost:本地无记录
        ]
    )
    _common_monkeypatch(monkeypatch, [acc])
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)
    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)
    monkeypatch.setattr(_cfg, "RECONCILE_KICK_GHOST", True, raising=False)

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("dry_run must not call remove_from_team")

    def _forbid_update(*_a, **_kw):
        raise AssertionError("dry_run must not call update_account")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)
    monkeypatch.setattr(manager, "update_account", _forbid_update)

    result = manager._reconcile_team_members(chatgpt_api=fake, dry_run=True)

    # dry_run 仍然应该"发现"异常并记录到 result
    assert result["dry_run"] is True
    assert "ghost-local@example.com" in result["orphan_kicked"]  # 包含 dry_run 记录
    assert "completely-unknown@example.com" in result["ghost_kicked"]


def test_reconcile_orphan_kick_syncs_local_status_to_auth_invalid(tmp_path, monkeypatch):
    """回归保护:残废 KICK 成功后必须把本地 status 改成 STATUS_AUTH_INVALID,
    否则下次 fill 仍按 active 计数,workspace 实际成员数和本地不一致。
    """
    acc = {
        "email": "kicked-orphan@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,
    }
    fake = _make_fake_chatgpt([{"email": "kicked-orphan@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])

    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)

    monkeypatch.setattr(manager, "remove_from_team", lambda *_a, **_kw: "removed")

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "kicked-orphan@example.com" in result["orphan_kicked"]
    # 关键回归断言:必须有一次 update 把 status 改成 STATUS_AUTH_INVALID
    assert any(
        email == "kicked-orphan@example.com" and kw.get("status") == STATUS_AUTH_INVALID for email, kw in updates
    ), f"expected status=AUTH_INVALID write, got: {updates}"


def test_reconcile_orphan_marked_when_kick_disabled(tmp_path, monkeypatch):
    """RECONCILE_KICK_ORPHAN=False → 残废只标 STATUS_ORPHAN,不 KICK。"""
    acc = {
        "email": "stay@example.com",
        "status": STATUS_ACTIVE,
        "auth_file": None,
    }
    fake = _make_fake_chatgpt([{"email": "stay@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)
    # 函数内通过 `from autoteam.config import RECONCILE_KICK_ORPHAN`,必须改 config 模块属性
    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", False, raising=False)

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("RECONCILE_KICK_ORPHAN=False must not KICK")

    updates = []
    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "stay@example.com" in result["orphan_marked"]
    # 应只被打 STATUS_ORPHAN 标记
    assert any(kw.get("status") == STATUS_ORPHAN for _e, kw in updates)


# ---------------------------------------------------------------------------
# task #2 新增:dry_run/ghost priority/auth_file fallback/quota fallthrough
# ---------------------------------------------------------------------------


def test_reconcile_dry_run_includes_over_cap_predictions(tmp_path, monkeypatch):
    """HIGH-1:dry_run 必须**预测**第二轮 over-cap 受害者填进 over_cap_kicked,
    但不能 GET /users 第二次,也不能调 remove_from_team / update_account。
    """
    # 5 子号 + 1 主号 → 超员 1
    auth_ok = tmp_path / "codex-good@example.com-team-1.json"
    auth_ok.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": f"u{i}@example.com",
            "status": STATUS_ACTIVE,
            "auth_file": str(auth_ok),
            "last_quota": {"primary_pct": 10 * i, "weekly_pct": 0},
        }
        for i in range(1, 6)
    ]
    members = [{"email": f"u{i}@example.com"} for i in range(1, 6)]
    members.append({"email": "owner@example.com"})  # 主号

    fake = _make_fake_chatgpt(members)

    # 包装 _api_fetch 计数,确保 dry_run 不会 GET 第二次
    fetch_calls = []
    real_fetch = fake._api_fetch

    def counting_fetch(method, path, body_=None):
        fetch_calls.append((method, path))
        return real_fetch(method, path, body_)

    fake._api_fetch = counting_fetch

    _common_monkeypatch(monkeypatch, accounts)

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("dry_run must not call remove_from_team")

    def _forbid_update(*_a, **_kw):
        raise AssertionError("dry_run must not call update_account")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)
    monkeypatch.setattr(manager, "update_account", _forbid_update)

    result = manager._reconcile_team_members(chatgpt_api=fake, dry_run=True)

    # 第二轮 GET /users 必须被跳过(只有第一轮一次)
    user_get_calls = [c for c in fetch_calls if c[0] == "GET" and c[1].endswith("/users")]
    assert len(user_get_calls) == 1, f"dry_run should not refetch /users, got {fetch_calls}"

    # 必须有 1 个 over_cap_kicked 预测项
    assert len(result["over_cap_kicked"]) == 1, f"expected 1 prediction, got {result['over_cap_kicked']}"
    # 受害者按 _priority 升序,active 按 p_remain (100-primary_pct) 升序 → primary_pct 最高的先 kick
    # u5 primary_pct=50 → p_remain=50 是最低 remain → 第一个被 kick
    assert result["over_cap_kicked"] == ["u5@example.com"]


def test_reconcile_priority_keeps_ghost_when_kick_disabled(tmp_path, monkeypatch):
    """HIGH-2:RECONCILE_KICK_GHOST=False 时,_priority(本地无记录) 必须返回 (99, 0),
    不能让 ghost 在第二轮 over-cap 排序里抢到 (0, 0) 绕过开关被 kick。
    """
    auth_ok = tmp_path / "codex-keep@example.com-team-1.json"
    auth_ok.write_text("{}", encoding="utf-8")

    # 4 个 active 账号 + 1 个 ghost(本地无记录) = 5,超员 1
    accounts = [
        {
            "email": f"u{i}@example.com",
            "status": STATUS_ACTIVE,
            "auth_file": str(auth_ok),
            "last_quota": {"primary_pct": 50, "weekly_pct": 0},
        }
        for i in range(1, 5)
    ]
    members = [
        {"email": "u1@example.com"},
        {"email": "u2@example.com"},
        {"email": "u3@example.com"},
        {"email": "u4@example.com"},
        {"email": "ghost-extra@example.com"},  # 本地无记录
        {"email": "owner@example.com"},
    ]

    fake = _make_fake_chatgpt(members)
    _common_monkeypatch(monkeypatch, accounts)

    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_GHOST", False, raising=False)
    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)

    kicked = []

    def fake_remove(_api, email, **kw):
        kicked.append(email)
        return "removed"

    monkeypatch.setattr(manager, "remove_from_team", fake_remove)
    monkeypatch.setattr(manager, "update_account", lambda *_a, **_kw: None)

    result = manager._reconcile_team_members(chatgpt_api=fake)

    # 第一轮:RECONCILE_KICK_GHOST=False,ghost-extra 不被 kick,只 ghost_seen
    assert "ghost-extra@example.com" in result["ghost_seen"]
    assert "ghost-extra@example.com" not in result["ghost_kicked"]

    # 第二轮 over-cap 1 个:必须挑 u1-u4 中的一个 active(p_remain=50, priority=(5,50))
    # 而不是 ghost-extra(被强制排到 (99, 0) 末尾)
    assert "ghost-extra@example.com" not in result["over_cap_kicked"], (
        f"ghost must not be over-cap kicked when RECONCILE_KICK_GHOST=False: {result['over_cap_kicked']}"
    )
    assert len(result["over_cap_kicked"]) == 1
    assert result["over_cap_kicked"][0] in {f"u{i}@example.com" for i in range(1, 5)}


def test_reconcile_find_team_auth_file_rejects_personal_plan(tmp_path, monkeypatch):
    """MEDIUM-3:_find_team_auth_file 必须只接受 -team-*.json,
    personal/plus/free 席位的 bundle 不能被误用(用错 plan 的 token 会被 OAuth 拒收)。
    """
    # patch AUTH_DIR to tmp_path
    from autoteam import auth_storage

    monkeypatch.setattr(auth_storage, "AUTH_DIR", tmp_path)

    # 只有 personal bundle,没有 team bundle
    (tmp_path / "codex-bob@example.com-personal-1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "codex-bob@example.com-plus-2.json").write_text("{}", encoding="utf-8")
    (tmp_path / "codex-bob@example.com-free.json").write_text("{}", encoding="utf-8")

    # 没有 -team- 前缀 → 必须返回 None
    assert manager._find_team_auth_file("bob@example.com") is None

    # 加一个真正的 -team- bundle → 应能找到
    team_bundle = tmp_path / "codex-bob@example.com-team-9.json"
    team_bundle.write_text("{}", encoding="utf-8")
    assert manager._find_team_auth_file("bob@example.com") == str(team_bundle)


def test_reconcile_misaligned_with_auth_repair_still_checks_exhausted(tmp_path, monkeypatch):
    """MEDIUM-4:STANDBY 错位补 auth 后,fallthrough 到 quota 耗尽检查。
    last_quota=100/100 时必须改标 EXHAUSTED,而不是停留在 active。
    """
    found_path = tmp_path / "codex-rescued@example.com-team-1.json"
    found_path.write_text("{}", encoding="utf-8")

    acc = {
        "email": "rescued@example.com",
        "status": STATUS_STANDBY,
        "auth_file": None,  # 触发补 auth 路径
        "last_quota": {"primary_pct": 100, "weekly_pct": 100},
    }

    fake = _make_fake_chatgpt([{"email": "rescued@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: str(found_path))

    def _forbid_kick(*_a, **_kw):
        raise AssertionError("misaligned + auth repair must not KICK")

    monkeypatch.setattr(manager, "remove_from_team", _forbid_kick)

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    result = manager._reconcile_team_members(chatgpt_api=fake)

    # misaligned_fixed 命中
    assert "rescued@example.com" in result["misaligned_fixed"]
    # **关键回归断言**:fallthrough 后 exhausted_marked 也命中
    assert "rescued@example.com" in result["exhausted_marked"], (
        f"补 auth 后必须 fallthrough 到 quota 检查,实际 result={result}"
    )
    # 必须有一次 update 把 status 改成 EXHAUSTED 且写 quota_exhausted_at
    assert any(
        kw.get("status") == STATUS_EXHAUSTED and kw.get("quota_exhausted_at") is not None for _e, kw in updates
    ), f"missing EXHAUSTED+quota_exhausted_at update: {updates}"


def test_reconcile_misaligned_orphan_kick_syncs_status_to_auth_invalid(tmp_path, monkeypatch):
    """MEDIUM-5(reconcile-reviewer 报的测试缺口):
    STANDBY 错位 + 找不到 auth → 走残废分支 KICK,KICK 成功后必须把本地 status 同步成
    AUTH_INVALID,否则下次 fill 仍按 standby 计数。
    """
    acc = {
        "email": "drifted-orphan@example.com",
        "status": STATUS_STANDBY,
        "auth_file": None,
    }
    fake = _make_fake_chatgpt([{"email": "drifted-orphan@example.com"}])
    _common_monkeypatch(monkeypatch, [acc])
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda email: None)

    import autoteam.config as _cfg

    monkeypatch.setattr(_cfg, "RECONCILE_KICK_ORPHAN", True, raising=False)
    monkeypatch.setattr(manager, "remove_from_team", lambda *_a, **_kw: "removed")

    updates = []
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: updates.append((email, kw)))

    result = manager._reconcile_team_members(chatgpt_api=fake)

    assert "drifted-orphan@example.com" in result["orphan_kicked"]
    # 关键回归断言:KICK 后 status 必须同步成 AUTH_INVALID
    assert any(
        email == "drifted-orphan@example.com" and kw.get("status") == STATUS_AUTH_INVALID for email, kw in updates
    ), f"expected status=AUTH_INVALID write after misaligned KICK, got: {updates}"
