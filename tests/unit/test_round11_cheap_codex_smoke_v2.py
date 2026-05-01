"""Round 11 v2 — cheap_codex_smoke / _cheap_codex_smoke_network payload schema 升级 + fallback chain。

PRD: .trellis/tasks/04-28-round11-master-resub-models-validate/prd.md (P1-3)
Spike 证据: .trellis/tasks/04-28-round11-master-resub-models-validate/research/platform_pipeline/
            phase_d_smoke_retry_result_*.json — 实测 codex backend 拒绝旧 schema 的 5 类错误信息。

覆盖:
  - 默认 model = gpt-5.5(team-only 主路径,见 PRD Q4)
  - 默认 fallback_models = ["gpt-5.4"](通用模型,team + free 都能用)
  - payload schema:
      * instructions 必填(默认 _CODEX_SMOKE_DEFAULT_INSTRUCTIONS)
      * input 是 list 格式 [{type:message, role:user, content:[{type:input_text, text:"ping"}]}]
      * 必含 store: false
      * 不再含 max_output_tokens
  - mock 200 SSE → ("alive", dict 含 response_text)
  - mock 400 model_not_supported → 触发 fallback 到 gpt-5.4
  - mock 429 usage_limit_reached → ("auth_invalid", "http_429")
  - mock 401 → ("auth_invalid", "http_401")
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _make_sse_resp(status_code: int, lines):
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.iter_lines.return_value = iter(lines)
    return fake_resp


def _make_err_resp(status_code: int, body_text: str):
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.text = body_text
    return fake_resp


# ---------------------------------------------------------------------------
# Payload schema (Round 11 v2)
# ---------------------------------------------------------------------------


def test_v2_payload_instructions_default_filled():
    """payload.instructions 默认填模块级常量(必填,后端拒收无 instructions 请求)。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)

    assert "instructions" in captured["payload"]
    assert captured["payload"]["instructions"] == codex_auth._CODEX_SMOKE_DEFAULT_INSTRUCTIONS
    assert "concise" in captured["payload"]["instructions"].lower()


def test_v2_payload_instructions_custom_passed_through():
    """传 instructions='custom prompt' → payload 透传。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", instructions="say only 'pong'", force=True,
        )

    assert captured["payload"]["instructions"] == "say only 'pong'"


def test_v2_payload_input_is_list_format():
    """payload.input 必须是 list 格式(后端拒收 string)。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)

    payload = captured["payload"]
    assert isinstance(payload["input"], list)
    assert len(payload["input"]) == 1
    msg = payload["input"][0]
    assert msg["type"] == "message"
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    assert msg["content"][0]["type"] == "input_text"
    assert msg["content"][0]["text"] == "ping"


def test_v2_payload_store_is_false():
    """payload.store 必须是 False(后端 'Store must be set to false')。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)

    assert captured["payload"]["store"] is False


def test_v2_payload_no_max_output_tokens():
    """v2 schema 删除 max_output_tokens(后端 'Unsupported parameter')。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke("tok", account_id="acc-1", max_output_tokens=128, force=True)

    assert "max_output_tokens" not in captured["payload"]


def test_v2_payload_default_model_gpt55():
    """默认 model = gpt-5.5(PRD Q4 team-only 主路径)。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)

    assert captured["payload"]["model"] == "gpt-5.5"


def test_v2_required_headers_present():
    """Authorization + Chatgpt-Account-Id + Accept: text/event-stream 必须都在。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke("tok-xyz", account_id="acc-99", force=True)

    headers = captured["headers"]
    assert headers.get("Authorization") == "Bearer tok-xyz"
    assert headers.get("Chatgpt-Account-Id") == "acc-99"
    assert headers.get("Accept") == "text/event-stream"


# ---------------------------------------------------------------------------
# 200 SSE happy path
# ---------------------------------------------------------------------------


def test_v2_alive_returns_dict_with_response_text():
    """200 SSE → ('alive', dict 含 response_text + raw_event=response.completed)。"""
    from autoteam import codex_auth

    sse_lines = [
        'data: {"type": "response.created"}',
        'data: {"type": "response.output_text.delta", "delta": "Hello"}',
        'data: {"type": "response.output_text.delta", "delta": " world"}',
        'data: {"type": "response.completed", "response": {"usage": {"output_tokens": 2}}}',
    ]

    with patch("requests.post", return_value=_make_sse_resp(200, sse_lines)):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", model="gpt-5.5", force=True,
        )

    assert result == "alive"
    assert isinstance(detail, dict)
    assert detail["model"] == "gpt-5.5"
    assert detail["response_text"] == "Hello world"
    assert detail["raw_event"] == "response.completed"
    assert detail["tokens"] == 2


# ---------------------------------------------------------------------------
# Fallback chain (model_not_supported)
# ---------------------------------------------------------------------------


def test_v2_fallback_to_gpt54_when_gpt55_not_supported():
    """gpt-5.5 撞 'not supported' 4xx → 自动 fallback 到 gpt-5.4 拿到 alive。"""
    from autoteam import codex_auth

    call_seq = []
    err_body = json.dumps({
        "detail": "The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."
    })

    def fake_post(*args, **kwargs):
        payload = kwargs.get("json", {})
        call_seq.append(payload["model"])
        if payload["model"] == "gpt-5.5":
            return _make_err_resp(400, err_body)
        # fallback gpt-5.4 → 200 alive
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.output_text.delta", "delta": "ok"}',
            'data: {"type": "response.completed", "response": {"usage": {"output_tokens": 1}}}',
        ])

    with patch("requests.post", side_effect=fake_post):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-free", force=True,
        )

    assert call_seq == ["gpt-5.5", "gpt-5.4"]
    assert result == "alive"
    assert isinstance(detail, dict)
    assert detail["model"] == "gpt-5.4"  # detail.model 反映最终成功用的 model
    assert detail["response_text"] == "ok"


def test_v2_fallback_chain_exhausted_returns_uncertain():
    """主 + fallback 都 model_not_supported → 返回 ('uncertain', 'model_not_supported_xxx')。"""
    from autoteam import codex_auth

    err_body = json.dumps({"detail": "The model is not supported."})
    call_seq = []

    def fake_post(*args, **kwargs):
        payload = kwargs.get("json", {})
        call_seq.append(payload["model"])
        return _make_err_resp(400, err_body)

    with patch("requests.post", side_effect=fake_post):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", model="gpt-5.5",
            fallback_models=["gpt-5.4", "gpt-4o"], force=True,
        )

    assert call_seq == ["gpt-5.5", "gpt-5.4", "gpt-4o"]
    assert result == "uncertain"
    assert isinstance(detail, str)
    assert detail.startswith("model_not_supported")


def test_v2_fallback_skipped_when_first_alive():
    """主 model 直接 alive → 不调 fallback。"""
    from autoteam import codex_auth

    call_seq = []

    def fake_post(*args, **kwargs):
        payload = kwargs.get("json", {})
        call_seq.append(payload["model"])
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        result, _ = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-team", force=True,
        )

    assert result == "alive"
    assert call_seq == ["gpt-5.5"]  # 不应触发 fallback


def test_v2_explicit_empty_fallback_list_works():
    """显式 fallback_models=[] → 不 fallback,主 model 失败直接返回。"""
    from autoteam import codex_auth

    err_body = json.dumps({"detail": "The model is not supported."})

    with patch("requests.post", return_value=_make_err_resp(400, err_body)):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", fallback_models=[], force=True,
        )

    assert result == "uncertain"
    assert detail.startswith("model_not_supported")


# ---------------------------------------------------------------------------
# Error matrix — auth_invalid / uncertain
# ---------------------------------------------------------------------------


def test_v2_429_usage_limit_reached_is_auth_invalid():
    """spike 实测 phase_d:429 usage_limit_reached → auth_invalid http_429。"""
    from autoteam import codex_auth

    err_body = json.dumps({
        "error": {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": "self_serve_business_usage_based",
        }
    })

    with patch("requests.post", return_value=_make_err_resp(429, err_body)):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", force=True,
        )

    assert result == "auth_invalid"
    assert detail == "http_429"


def test_v2_401_returns_auth_invalid():
    """401 → auth_invalid http_401。"""
    from autoteam import codex_auth

    with patch("requests.post", return_value=_make_err_resp(401, '{"error":"unauthorized"}')):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", force=True,
        )

    assert result == "auth_invalid"
    assert detail == "http_401"


def test_v2_403_returns_auth_invalid():
    """403 → auth_invalid http_403。"""
    from autoteam import codex_auth

    with patch("requests.post", return_value=_make_err_resp(403, '{"error":"forbidden"}')):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", force=True,
        )

    assert result == "auth_invalid"
    assert detail == "http_403"


def test_v2_500_returns_uncertain():
    """500 → uncertain http_500(临时性故障,不动账号)。"""
    from autoteam import codex_auth

    with patch("requests.post", return_value=_make_err_resp(500, "Internal Server Error")):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", force=True,
        )

    assert result == "uncertain"
    assert detail == "http_500"


def test_v2_quota_keyword_in_4xx_body_is_auth_invalid():
    """4xx body 含 quota 关键词 → auth_invalid(seat 真失效)。"""
    from autoteam import codex_auth

    body = json.dumps({"error": "your quota has been exceeded"})
    # 必须不包含 not supported(不然走 model_not_supported 路径)
    with patch("requests.post", return_value=_make_err_resp(402, body)):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", force=True,
        )

    assert result == "auth_invalid"
    assert detail == "http_402_quota_hint"


# ---------------------------------------------------------------------------
# Backward compat — 24h cache + alive/auth_invalid sentinel returns
# ---------------------------------------------------------------------------


def test_v2_empty_access_token_returns_auth_invalid_immediately():
    """空 access_token → auth_invalid + 不发请求。"""
    from autoteam import codex_auth

    with patch("requests.post") as mock_post:
        result, detail = codex_auth.cheap_codex_smoke("", account_id="acc-1", force=True)

    assert result == "auth_invalid"
    assert detail == "empty_access_token"
    mock_post.assert_not_called()
