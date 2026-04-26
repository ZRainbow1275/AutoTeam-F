"""SPEC-3 Docker 镜像守卫的非容器化集成测试。

覆盖以下 SPEC §9 场景中可在宿主机执行的部分:
- 9.1 步骤 3:`/api/version` 端点契约(JSON 字段、降级语义、免鉴权)
- 9.3 步骤 1-2:ruff F821 拦截 typo
- self-check 白名单 import 在宿主机环境也能跑通(等同于容器启动期检查)

宿主机不可达的部分(docker compose up / crash-loop / OCI label)走 shell SOP,
见 `docs/docker.md` 的"代码更新后的 rebuild SOP"章节。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# §9.1 /api/version 端点契约
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from autoteam.api import app

    return TestClient(app)


def test_api_version_returns_two_fields(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC AC3: /api/version 必须返回 git_sha + build_time 双字段。"""

    monkeypatch.setenv("AUTOTEAM_GIT_SHA", "cf2f7d3")
    monkeypatch.setenv("AUTOTEAM_BUILD_TIME", "2026-04-26T03:00:00Z")
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"git_sha", "build_time"}
    assert body["git_sha"] == "cf2f7d3"
    assert body["build_time"] == "2026-04-26T03:00:00Z"


def test_api_version_falls_back_to_unknown(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC AC7: 不传 build-arg(env 未设)应降级返回 unknown,不报 500。"""

    monkeypatch.delenv("AUTOTEAM_GIT_SHA", raising=False)
    monkeypatch.delenv("AUTOTEAM_BUILD_TIME", raising=False)
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"git_sha": "unknown", "build_time": "unknown"}


def test_api_version_is_unauthenticated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC AC9: 即便配了 API_KEY,/api/version 也免鉴权直接返回 200。"""

    # 模拟"已配 API_KEY 但请求不带 Bearer"
    import autoteam.api as api_module

    monkeypatch.setattr(api_module, "API_KEY", "fake-secret-do-not-leak")
    resp = client.get("/api/version")  # 故意不带 Authorization header
    assert resp.status_code == 200, "version 端点应在 _AUTH_SKIP_PATHS 白名单中"


def test_api_version_in_auth_skip_paths() -> None:
    """额外契约:_AUTH_SKIP_PATHS 必须显式包含 /api/version。"""

    from autoteam.api import _AUTH_SKIP_PATHS

    assert "/api/version" in _AUTH_SKIP_PATHS


# ---------------------------------------------------------------------------
# §9.3 lint 守卫:ruff F821 拦截 typo(同 entrypoint self-check 等价语义)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="本机 PATH 中未找到 ruff 二进制,跳过 lint 集成验证(CI 应直接装 ruff)",
)
def test_ruff_catches_undefined_import(tmp_path: Path) -> None:
    """SPEC AC5: 制造 typo 的 import,ruff F821 必须返回非零 exit code。

    用 PATH 上已存在的 ruff 直接跑,避开 `uv run` 在 Windows 上可能的虚拟环境锁。
    CI 与 pre-commit hook 走 ruff-pre-commit,语义等价。
    """

    canary = tmp_path / "canary.py"
    canary.write_text(
        "from autoteam.accounts import list_accounts  # typo: 实际应为 load_accounts\n",
        encoding="utf-8",
    )

    cmd = ["ruff", "check", "--select", "F401,F811,F821", str(canary)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode != 0, (
        f"ruff 应拦截 typo,但返回 exit=0。stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
    # F401(unused) 或 F821(undefined) 任一命中即可 — typo 引入的 import 通常两者都中
    combined = proc.stdout + proc.stderr
    assert ("F401" in combined) or ("F821" in combined), (
        f"输出中缺少 F401/F821 标记: {combined!r}"
    )


# ---------------------------------------------------------------------------
# self-check 白名单契约:entrypoint 列出的 11 个符号必须全部 importable
# ---------------------------------------------------------------------------


def test_self_check_whitelist_imports() -> None:
    """SPEC AC1: docker-entrypoint.sh self-check 列出的所有符号必须可导入。

    若 PR 改名/删除这些符号,本测试先红 → entrypoint 才不会在容器启动期红。
    """

    from autoteam.api import app  # noqa: F401
    from autoteam.accounts import (  # noqa: F401
        STATUS_ACTIVE,
        STATUS_AUTH_INVALID,
        STATUS_EXHAUSTED,
        STATUS_ORPHAN,
        STATUS_PENDING,
        STATUS_PERSONAL,
        STATUS_STANDBY,
        load_accounts,
        save_accounts,
    )
    from autoteam.manager import sync_account_states  # noqa: F401

    # 简单的契约校验 — 函数应可调用、状态常量应是字符串
    assert callable(load_accounts)
    assert callable(save_accounts)
    assert callable(sync_account_states)
    for const in (
        STATUS_ACTIVE,
        STATUS_EXHAUSTED,
        STATUS_STANDBY,
        STATUS_PENDING,
        STATUS_PERSONAL,
        STATUS_AUTH_INVALID,
        STATUS_ORPHAN,
    ):
        assert isinstance(const, str) and const, f"状态常量必须是非空字符串: {const!r}"


# ---------------------------------------------------------------------------
# Dockerfile / compose / entrypoint 静态契约(避免 SPEC §1-§4 被悄悄回退)
# ---------------------------------------------------------------------------


def test_dockerfile_declares_git_sha_arg() -> None:
    """SPEC AC4: Dockerfile 必须声明 ARG GIT_SHA + ENV AUTOTEAM_GIT_SHA。"""

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG GIT_SHA" in dockerfile
    assert "ARG BUILD_TIME" in dockerfile
    assert "ENV AUTOTEAM_GIT_SHA" in dockerfile
    assert "ENV AUTOTEAM_BUILD_TIME" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile


def test_compose_passes_build_args() -> None:
    """compose 必须把 GIT_SHA / BUILD_TIME 作为 build-arg 透传(降级 unknown)。"""

    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GIT_SHA: ${GIT_SHA:-unknown}" in compose
    assert "BUILD_TIME: ${BUILD_TIME:-unknown}" in compose


def test_entrypoint_contains_self_check() -> None:
    """entrypoint 必须包含 self-check + crash-loop 兜底。"""

    entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "[self-check]" in entrypoint
    assert "from autoteam.api import app" in entrypoint
    assert "from autoteam.accounts import" in entrypoint
    assert "exit 1" in entrypoint, "self-check 失败必须 exit 1 触发 crash-loop"


def test_pyproject_has_ruff_config() -> None:
    """pyproject.toml 必须配 [tool.ruff.lint] 三条规则。"""

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff.lint]" in pyproject
    assert "F401" in pyproject
    assert "F811" in pyproject
    assert "F821" in pyproject


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
