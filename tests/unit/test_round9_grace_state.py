"""Round 9 SPEC v2.0 — STATUS_DEGRADED_GRACE 状态机 + JWT grace_until 解析单测。

覆盖:
  - accounts.STATUS_DEGRADED_GRACE 常量字面量
  - master_health.extract_grace_until_from_jwt 各种 JWT 形态(epoch / ISO-8601 / null / 缺失)
"""
from __future__ import annotations

import base64
import json


def _make_id_token_jwt(payload: dict) -> str:
    """构造合法的 JWT 字符串(无签名校验,只解 payload),供测试用。"""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    body_bytes = json.dumps(payload).encode()
    body = base64.urlsafe_b64encode(body_bytes).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


def test_status_degraded_grace_constant_present():
    """STATUS_DEGRADED_GRACE 常量必须以 'degraded_grace' 字面量存在 — state-machine v2.0 §2.1。"""
    from autoteam.accounts import STATUS_DEGRADED_GRACE

    assert STATUS_DEGRADED_GRACE == "degraded_grace"


def test_extract_grace_until_iso8601_format():
    """JWT id_token 含 chatgpt_subscription_active_until=ISO-8601 → 返回 epoch float。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    iso = "2026-05-25T12:06:23+00:00"
    token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": iso,
        }
    })
    result = extract_grace_until_from_jwt(token)
    assert result is not None
    # 2026-05-25T12:06:23 UTC ≈ 1779451583
    assert 1779000000 < result < 1780000000


def test_extract_grace_until_iso_with_z_suffix():
    """JWT 字段以 'Z' 结尾(常见 OpenAI 格式) → 也能解。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": "2026-05-25T12:06:23Z",
        }
    })
    assert extract_grace_until_from_jwt(token) is not None


def test_extract_grace_until_epoch_int():
    """JWT 字段是 epoch int → 直接 float()。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": 1779451583,
        }
    })
    assert extract_grace_until_from_jwt(token) == 1779451583.0


def test_extract_grace_until_missing_field_returns_none():
    """JWT 缺 chatgpt_subscription_active_until → None,不抛异常。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    token = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": "team"},
    })
    assert extract_grace_until_from_jwt(token) is None


def test_extract_grace_until_invalid_jwt_returns_none():
    """token 非 JWT 格式(无 dot 分隔)→ None,不抛异常 (M-I11 永不抛)。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    assert extract_grace_until_from_jwt("not-a-jwt") is None
    assert extract_grace_until_from_jwt("") is None
    assert extract_grace_until_from_jwt(None) is None


def test_extract_grace_until_malformed_payload_returns_none():
    """JWT 中段不是合法 base64 / JSON → None。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    bad = "header.NOT-BASE64!.sig"
    assert extract_grace_until_from_jwt(bad) is None


def test_extract_grace_until_null_field_returns_none():
    """字段值为 null → None。"""
    from autoteam.master_health import extract_grace_until_from_jwt

    token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": None,
        }
    })
    assert extract_grace_until_from_jwt(token) is None
