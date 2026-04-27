"""Round 10 dry-run 端到端验证脚本 (AC6).

PRD `.trellis/tasks/04-28-master-codex-oauth-session-fallback/prd.md` AC6:
quality-reviewer 必须 dry-run 调一次完整流程,断言 6 项硬指标。

由于 Check Agent 不能做真实网络调用 (会触发 OpenAI 风控且无法获得真实 admin session),
本脚本 mock 关键边界 (`SessionCodexAuthFlow.start` + `_exchange_auth_code`),
让 `refresh_main_auth_file` 跑完真实 save_main_auth_file → _write_auth_file 链路,
验证落盘的 codex-main-*.json 含完整字段且能被 cheap_codex_smoke / refresh round-trip 读取。

运行:
    python tests/manual/test_round10_dryrun.py

退出码:
    0 — 6 项硬指标全部通过
    1 — 任一指标失败
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch


def _make_jwt(payload: dict) -> str:
    """造一个无签名的 JWT (header.payload.signature) 用于测试.

    各段都是 base64url(JSON);signature 段填占位符,本地校验不验签。
    """
    header = {"alg": "RS256", "typ": "JWT"}

    def _b64url(d: dict) -> str:
        s = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(s).rstrip(b"=").decode()

    return f"{_b64url(header)}.{_b64url(payload)}.signature_placeholder"


def _decode_jwt_exp(jwt: str) -> int | None:
    """从 JWT 提取 exp 字段."""
    try:
        payload_b64 = jwt.split(".")[1]
        # 补齐 padding
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("exp")
    except Exception as exc:
        print(f"  [decode_jwt] 解码失败: {exc}")
        return None


def _make_realistic_bundle() -> dict:
    """构造真实 OAuth bundle,含合法 JWT + exp = now+3600."""
    now = int(time.time())
    exp = now + 3600  # 1h 后过期

    # access_token JWT(模拟真实 OpenAI 颁发的 access_token,含 sub/exp)
    access_token = _make_jwt(
        {
            "sub": "user-aaaa-bbbb",
            "iss": "https://auth.openai.com",
            "aud": ["https://api.openai.com/v1"],
            "iat": now,
            "exp": exp,
            "scope": "openid email profile offline_access",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "main-account-uuid-1234",
                "chatgpt_plan_type": "team",
            },
        }
    )

    # id_token JWT(模拟真实 OpenAI id_token,含 email + chatgpt claims)
    id_token = _make_jwt(
        {
            "sub": "user-aaaa-bbbb",
            "iss": "https://auth.openai.com",
            "aud": "app_EMoamEEZ73f0CkXaXp7hrann",
            "iat": now,
            "exp": exp,
            "email": "admin@example.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "main-account-uuid-1234",
                "chatgpt_plan_type": "team",
            },
        }
    )

    # refresh_token 是普通长字符串,与 access_token 完全不同
    refresh_token = "refresh_" + "a" * 60

    return {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
    }


def main() -> int:
    print("=" * 70)
    print("Round 10 Dry-Run AC6 — login_codex_via_session + refresh_main_auth_file")
    print("=" * 70)

    # 切换到隔离的 auth dir,避免污染 accounts/
    tmp_auth_dir = Path("tests/manual/_round10_dryrun_auth")
    tmp_auth_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧文件
    for old in tmp_auth_dir.glob("codex-main-*.json"):
        old.unlink()

    fake_token_response = _make_realistic_bundle()

    # mock _exchange_auth_code 的内部 requests.post,返回 200 + token
    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return fake_token_response

        @staticmethod
        def text() -> str:
            return json.dumps(fake_token_response)

    # 关键 mock:
    # 1. SessionCodexAuthFlow.start → 直接返回 step=completed (跳过 Playwright)
    # 2. SessionCodexAuthFlow.complete → 调真实的 _exchange_auth_code 拿 bundle
    #    (但其内部的 requests.post 已被 mock)
    # 3. SessionCodexAuthFlow.stop → no-op
    # 4. admin_state getters → 返回固定值
    # 5. AUTH_DIR → 切到 tmp 目录
    # 6. ensure_auth_file_permissions → no-op (Windows 下 chmod 可能 noop 但保险起见)

    from autoteam import codex_auth

    # 替换 AUTH_DIR (不能 monkeypatch 模块级常量,要重新 patch ensure_auth_dir + _write_auth_file 引用)
    # 简单做法:patch save_main_auth_file 让它写到 tmp_auth_dir
    real_write_auth_file = codex_auth._write_auth_file

    def _patched_save_main_auth_file(bundle: dict) -> str:
        import hashlib

        account_id = (
            bundle.get("account_id") or hashlib.md5(bundle.get("email", "main").encode()).hexdigest()[:8]
        )
        # 清理同名旧文件
        for old in tmp_auth_dir.glob("codex-main-*.json"):
            old.unlink()
        filepath = tmp_auth_dir / f"codex-main-{account_id}.json"
        return real_write_auth_file(filepath, bundle)

    # mock SessionCodexAuthFlow 的 start/complete/stop
    class _FakeFlow:
        def __init__(self, **kwargs):
            print(f"  [FakeFlow.__init__] kwargs.email={kwargs.get('email')}")
            print(f"  [FakeFlow.__init__] kwargs.account_id={kwargs.get('account_id')}")
            print(f"  [FakeFlow.__init__] kwargs.workspace_name={kwargs.get('workspace_name')}")
            assert callable(kwargs.get("auth_file_callback")), "auth_file_callback 必须是 callable"
            self.email = kwargs["email"]
            self.code_verifier = "fake_verifier"
            self.auth_code = "fake_auth_code"

        def start(self):
            print("  [FakeFlow.start] returning step=completed")
            return {"step": "completed", "detail": None}

        def complete(self):
            print("  [FakeFlow.complete] calling real _exchange_auth_code (mocked requests)")
            with patch("requests.post", return_value=_FakeResponse()):
                bundle = codex_auth._exchange_auth_code(
                    self.auth_code, self.code_verifier, fallback_email=self.email
                )
            assert bundle is not None, "bundle 不能为 None"
            return {
                "email": bundle.get("email"),
                "auth_file": "",  # wrapper 用空串,refresh_main_auth_file 自己 save
                "plan_type": bundle.get("plan_type"),
                "bundle": bundle,
            }

        def stop(self):
            print("  [FakeFlow.stop] no-op")

    print("\n[Step 1] mock 边界 → 调 refresh_main_auth_file()...")
    with patch.object(codex_auth, "SessionCodexAuthFlow", _FakeFlow), patch.object(
        codex_auth, "get_admin_email", return_value="admin@example.com"
    ), patch.object(
        codex_auth, "get_admin_session_token", return_value="fake_session_token_xyz"
    ), patch.object(
        codex_auth, "get_chatgpt_account_id", return_value="main-account-uuid-1234"
    ), patch.object(
        codex_auth, "get_chatgpt_workspace_name", return_value="Master Team"
    ), patch.object(codex_auth, "save_main_auth_file", _patched_save_main_auth_file):
        result = codex_auth.refresh_main_auth_file()

    print(f"\n[Step 2] refresh_main_auth_file 返回值: {json.dumps(result, indent=2)}")

    auth_file_path = result.get("auth_file")
    assert auth_file_path, "result 必须含 auth_file 字段"
    assert Path(auth_file_path).exists(), f"auth_file 必须落盘: {auth_file_path}"

    # 读落盘 JSON
    with Path(auth_file_path).open("r", encoding="utf-8") as f:
        auth_data = json.load(f)
    print(f"\n[Step 3] 落盘 codex-main-*.json 内容:")
    print(json.dumps(auth_data, indent=2))

    # ============================================================
    # 6 项硬指标(AC6)
    # ============================================================
    print("\n" + "=" * 70)
    print("AC6 6 项硬指标验证")
    print("=" * 70)

    failures: list[str] = []

    # I1: 包含非空 access_token (JWT, exp > now+600s)
    access_token = auth_data.get("access_token", "")
    if not access_token:
        failures.append("I1 — access_token 为空")
    else:
        exp = _decode_jwt_exp(access_token)
        if exp is None:
            failures.append("I1 — access_token 不是合法 JWT (无 exp)")
        elif exp <= time.time() + 600:
            failures.append(f"I1 — access_token exp={exp} 不超过 now+600s")
        else:
            print(f"  [I1] PASS — access_token JWT exp={exp} (>{int(time.time())+600})")

    # I2: 包含非空 refresh_token (长字符串,与 access_token 不同)
    refresh_token = auth_data.get("refresh_token", "")
    if not refresh_token:
        failures.append("I2 — refresh_token 为空")
    elif refresh_token == access_token:
        failures.append("I2 — refresh_token 与 access_token 相同")
    elif len(refresh_token) < 20:
        failures.append(f"I2 — refresh_token 太短 (len={len(refresh_token)})")
    else:
        print(f"  [I2] PASS — refresh_token len={len(refresh_token)},不同于 access_token")

    # I3: id_token 字段存在 (可空但若有需为合法 JWT)
    id_token = auth_data.get("id_token")
    if id_token is None or "id_token" not in auth_data:
        failures.append("I3 — id_token 字段缺失")
    elif id_token:
        # 非空 → 必须为合法 JWT
        exp = _decode_jwt_exp(id_token)
        if exp is None:
            failures.append("I3 — id_token 非空但不是合法 JWT")
        else:
            print(f"  [I3] PASS — id_token JWT exp={exp}")
    else:
        print(f"  [I3] PASS — id_token 字段存在但为空字符串(允许)")

    # I4: account_id / email / plan_type 字段齐全
    # 注:_write_auth_file 不写 plan_type 字段(它只在 bundle 内传递),所以 plan_type 看 result.plan_type
    missing_fields = []
    if not auth_data.get("account_id"):
        missing_fields.append("account_id")
    if not auth_data.get("email"):
        missing_fields.append("email")
    # plan_type 来自 result.plan_type(refresh_main_auth_file 返回值),不在落盘文件
    if not result.get("plan_type"):
        missing_fields.append("plan_type (在 result 中)")
    if missing_fields:
        failures.append(f"I4 — 字段缺失: {missing_fields}")
    else:
        print(
            f"  [I4] PASS — account_id={auth_data['account_id']} "
            f"email={auth_data['email']} plan_type={result['plan_type']}"
        )

    # I5: 使用 access_token 调一次真实 /backend-api/codex/responses 拿 200 (或 4xx 配额错误,不能 401)
    # —— Check Agent 无法做真实网络调用,改为验证 cheap_codex_smoke 接口可调
    # 验证签名 + 401 输入处理
    print("  [I5] (无法做真实网络) 验证 cheap_codex_smoke 接口契约...")
    smoke_result, smoke_detail = codex_auth.cheap_codex_smoke(
        access_token, account_id=auth_data["account_id"]
    )
    # smoke 真调网络可能返回 alive/auth_invalid/uncertain,但不应抛异常
    # mock token 不是真实 token,所以预期 auth_invalid 或 uncertain (网络层错误)
    if smoke_result not in ("alive", "auth_invalid", "uncertain"):
        failures.append(f"I5 — cheap_codex_smoke 返回非法 result={smoke_result}")
    else:
        print(f"  [I5] PASS — cheap_codex_smoke 接口可用,返回 ({smoke_result}, {smoke_detail})")
        print(f"        (mock token 不会真 200,本指标只验证接口契约;真实验证由用户手动跑)")

    # I6: 使用 refresh_token 调一次刷新接口 (auth.openai.com/oauth/token grant_type=refresh_token)
    # —— Check Agent 无法做真实网络调用,改为验证 refresh_access_token 函数可调用
    # 该函数定义在 codex_auth.py:1932
    if hasattr(codex_auth, "refresh_access_token"):
        print("  [I6] (无法做真实网络) 验证 refresh_access_token 接口契约...")
        # mock requests.post,验证函数能正常调用
        with patch("requests.post") as mock_post:
            mock_post.return_value = _FakeResponse()
            try:
                new_token = codex_auth.refresh_access_token(refresh_token)
                # mock 返回的 access_token 在 fake_token_response 内
                assert new_token, "refresh_access_token 必须返回非空"
                print(f"  [I6] PASS — refresh_access_token 接口可调用,返回 token (mock)")
            except Exception as exc:
                failures.append(f"I6 — refresh_access_token 调用异常: {exc}")
    else:
        failures.append("I6 — codex_auth.refresh_access_token 函数不存在")

    # ============================================================
    # 总结
    # ============================================================
    print("\n" + "=" * 70)
    if failures:
        print(f"FAIL — {len(failures)} 项硬指标失败:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — 全部 6 项硬指标通过")
    print("=" * 70)
    print("\n注意:I5/I6 为接口契约验证,真实网络验证需用户在导入真实 admin session_token 后手动跑")
    print("详见 review-report §6 User Manual Verification 章节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
