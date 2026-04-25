"""覆盖 task #1:invite_member POST retry + PATCH fallback + seat_type 落盘。

只测纯逻辑路径:
- _classify_invite_error 的分类结果驱动 retry vs 直接返回
- PATCH seat_type=default 失败时 _seat_type 降级为 usage_based(codex-only)
- accounts.add_account 接 seat_type 字段并写入 accounts.json
"""

from __future__ import annotations

from autoteam import accounts
from autoteam.chatgpt_api import ChatGPTTeamAPI


def _make_api(monkeypatch):
    """构造一个不启动浏览器的 ChatGPTTeamAPI。"""
    monkeypatch.setattr("autoteam.chatgpt_api.get_chatgpt_account_id", lambda: "acct-test-1")
    monkeypatch.setattr("autoteam.chatgpt_api.get_chatgpt_workspace_name", lambda: "TestWS")
    api = ChatGPTTeamAPI()
    # retry sleep 直接短路,避免测试卡住 5s/15s
    monkeypatch.setattr("autoteam.chatgpt_api.time.sleep", lambda *_args, **_kw: None)
    return api


def test_invite_post_retry_on_rate_limited(monkeypatch):
    """429 rate_limited 应该按 _INVITE_POST_RETRY_DELAYS 退避重试,最终成功返回 200。"""
    api = _make_api(monkeypatch)

    calls = []

    def fake_api_fetch(method, path, body=None):
        calls.append((method, path))
        # POST /invites 头两次 429,第三次 200;PATCH 直接 200
        if method == "POST" and path.endswith("/invites"):
            if len([c for c in calls if c[0] == "POST"]) <= 2:
                return {"status": 429, "body": '{"detail":"rate_limit_exceeded"}'}
            return {
                "status": 200,
                "body": '{"account_invites":[{"id":"inv-1"}]}',
            }
        # PATCH 路径
        return {"status": 200, "body": "{}"}

    monkeypatch.setattr(api, "_api_fetch", fake_api_fetch)

    status, data = api.invite_member("a@example.com", seat_type="usage_based")

    assert status == 200
    # 精确:第 1+2 次 POST 失败,第 3 次成功 → 共 3 次 POST + 1 次 PATCH
    post_calls = [c for c in calls if c[0] == "POST"]
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert len(post_calls) == 3
    assert len(patch_calls) == 1
    assert data["_seat_type"] == "chatgpt"  # PATCH 成功,升级


def test_invite_post_no_retry_on_domain_blocked(monkeypatch):
    """domain_blocked 类错误不应 retry,直接返回给上层换号。"""
    api = _make_api(monkeypatch)
    calls = []

    def fake_api_fetch(method, path, body=None):
        calls.append((method, path))
        return {
            "status": 400,
            "body": '{"detail":"domain not allowed"}',
        }

    monkeypatch.setattr(api, "_api_fetch", fake_api_fetch)

    status, data = api.invite_member("bad@blocked.com", seat_type="usage_based")

    assert status == 400
    # 只 POST 一次,没有 retry
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert data.get("_error_kind") == "domain_blocked"
    assert data.get("_seat_type") == "unknown"


def test_invite_patch_failure_returns_codex_only_marker(monkeypatch):
    """PATCH seat_type=default 全部失败时,_seat_type 必须保留为 usage_based(=codex-only)。"""
    api = _make_api(monkeypatch)

    def fake_api_fetch(method, path, body=None):
        if method == "POST":
            return {
                "status": 200,
                "body": '{"account_invites":[{"id":"inv-1"}]}',
            }
        # PATCH 一直 500
        return {"status": 500, "body": '{"detail":"internal"}'}

    monkeypatch.setattr(api, "_api_fetch", fake_api_fetch)

    status, data = api.invite_member("b@example.com", seat_type="usage_based")

    assert status == 200
    # POST 成功,但 PATCH 全败 → 保留 usage_based 作 codex-only 标记
    assert data["_seat_type"] == "usage_based"


def test_invite_patch_success_returns_full_chatgpt_marker(monkeypatch):
    """POST 首次 200 + PATCH 首次 200 → _seat_type=chatgpt(完整席位)。"""
    api = _make_api(monkeypatch)

    def fake_api_fetch(method, path, body=None):
        if method == "POST":
            return {
                "status": 200,
                "body": '{"account_invites":[{"id":"inv-2"}]}',
            }
        return {"status": 200, "body": "{}"}

    monkeypatch.setattr(api, "_api_fetch", fake_api_fetch)

    status, data = api.invite_member("c@example.com", seat_type="usage_based")

    assert status == 200
    assert data["_seat_type"] == "chatgpt"
    # invite_member 约定返回的 data 必定是 dict(即便 body 非 JSON)
    assert isinstance(data, dict)


def test_seat_type_persisted_to_accounts_json(tmp_path, monkeypatch):
    """accounts.add_account(seat_type=SEAT_CHATGPT) 必须把 seat_type 字段落盘。"""
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "")

    # 首次 add_account 带 SEAT_CHATGPT
    accounts.add_account("u1@example.com", "pw", seat_type=accounts.SEAT_CHATGPT)
    stored = accounts.load_accounts()
    assert len(stored) == 1
    assert stored[0]["seat_type"] == accounts.SEAT_CHATGPT

    # 对已存在账号再 add_account 带新 seat_type → 允许补写(从 UNKNOWN 升级)
    accounts.add_account("u2@example.com", "pw", seat_type=accounts.SEAT_UNKNOWN)
    accounts.add_account("u2@example.com", "pw", seat_type=accounts.SEAT_CODEX)
    stored = accounts.load_accounts()
    u2 = next(a for a in stored if a["email"] == "u2@example.com")
    assert u2["seat_type"] == accounts.SEAT_CODEX

    # update_account 直接改 seat_type 也生效
    accounts.update_account("u1@example.com", seat_type=accounts.SEAT_CODEX)
    stored = accounts.load_accounts()
    u1 = next(a for a in stored if a["email"] == "u1@example.com")
    assert u1["seat_type"] == accounts.SEAT_CODEX


def test_invite_5xx_triggers_retry(monkeypatch):
    """5xx server_error 必须按 _INVITE_POST_RETRY_DELAYS 退避重试,直到成功或耗尽。

    这是 task #1 HIGH-1 的回归保护:旧实现把 5xx 归为 other(不重试),
    生产里 OpenAI 后端短暂 502/503 会立刻让账号被放弃。
    """
    api = _make_api(monkeypatch)
    calls = []

    def fake_api_fetch(method, path, body=None):
        calls.append((method, path))
        if method == "POST" and path.endswith("/invites"):
            post_count = sum(1 for c in calls if c[0] == "POST")
            if post_count == 1:
                return {"status": 502, "body": '{"detail":"bad gateway"}'}
            if post_count == 2:
                return {"status": 503, "body": '{"detail":"service unavailable"}'}
            return {
                "status": 200,
                "body": '{"account_invites":[{"id":"inv-5xx"}]}',
            }
        # PATCH 直接 200
        return {"status": 200, "body": "{}"}

    monkeypatch.setattr(api, "_api_fetch", fake_api_fetch)

    status, data = api.invite_member("c@example.com", seat_type="usage_based")

    assert status == 200
    post_calls = [c for c in calls if c[0] == "POST"]
    # 502 → retry, 503 → retry, 200 成功 = 3 次 POST
    assert len(post_calls) == 3
    assert data["_seat_type"] == "chatgpt"


def test_invite_domain_keyword_does_not_match_email_in_body(monkeypatch):
    """email 中的 @gmail.com 之类不能再让 _classify_invite_error 误判 domain_blocked。

    task #1 HIGH-3:旧关键词列表里有单 token "domain",resp_body 含 "gmail.com" 时
    "domain" 子串就被命中,把可重试的 rate_limited / server_error 错误压成 domain_blocked
    直接放弃账号。修复后:
      1) 关键词收窄到明确短语
      2) 不再 fallthrough 到 resp_body
    """
    classify = ChatGPTTeamAPI._classify_invite_error
    # 模拟 errored_emails 里 email 字面量含 "gmail.com",但 detail 是 rate_limit
    body = '{"detail":"too many requests","errored_emails":[{"email":"foo@gmail.com"}]}'
    data = {
        "detail": "too many requests",
        "errored_emails": [{"email": "foo@gmail.com"}],
    }
    assert classify(429, data, body) == "rate_limited"

    # 5xx 即便 body 含 "domain" 字样也必须先归 server_error
    body2 = '{"detail":"upstream domain timeout"}'
    data2 = {"detail": "upstream domain timeout"}
    assert classify(502, data2, body2) == "server_error"

    # 真正的 domain_blocked:detail 明确写 "domain not allowed"
    body3 = '{"detail":"domain is not allowed for this workspace"}'
    data3 = {"detail": "domain is not allowed for this workspace"}
    assert classify(400, data3, body3) == "domain_blocked"


def test_invite_default_falls_back_to_usage_based_inside_invite_member(monkeypatch):
    """seat_type='default' 触发 errored_emails / 业务拒绝时,invite_member 内部
    必须自动重试 seat_type='usage_based' 并返回新结果。

    task #1 HIGH-4 回归保护:fallback 现在收敛在 invite_member 内,
    上层只调一次就能拿到终态(_seat_type / _errored_emails)。
    """
    api = _make_api(monkeypatch)
    seat_types_seen = []

    def fake_api_fetch(method, path, body=None):
        if method == "POST" and path.endswith("/invites"):
            seat_types_seen.append(body.get("seat_type"))
            # default 邀请被 errored_emails 拒
            if body.get("seat_type") == "default":
                return {
                    "status": 200,
                    "body": '{"errored_emails":[{"email":"x@example.com","error":"seat_unavailable"}]}',
                }
            # usage_based 兜底成功
            return {"status": 200, "body": '{"account_invites":[{"id":"inv-fb"}]}'}
        # PATCH 升级失败,保持 codex 席位
        return {"status": 500, "body": '{"detail":"upgrade failed"}'}

    monkeypatch.setattr(api, "_api_fetch", fake_api_fetch)

    status, data = api.invite_member("x@example.com", seat_type="default")

    assert status == 200
    # invite_member 内部应已尝试两轮:default → usage_based
    assert seat_types_seen[:2] == ["default", "usage_based"]
    # PATCH default 升级失败 → 保留 usage_based codex 席位
    assert data["_seat_type"] == "usage_based"
    # 兜底后的响应不应该再带 errored_emails(覆盖到了新 200)
    assert "_errored_emails" not in data
