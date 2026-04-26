#!/bin/bash
set -e

# 清理残留锁文件并启动虚拟显示器
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# 确保数据目录存在且可写
mkdir -p /app/data /app/data/auths /app/data/screenshots
chmod -R 777 /app/data

# 数据文件：无条件软链到 data/（确保所有写入都持久化）
for f in .env accounts.json state.json; do
    # data 里没有就创建空文件
    [ -f "/app/data/$f" ] || touch "/app/data/$f"
    # 删除容器内的真实文件（如果不是软链），然后建软链
    rm -f "/app/$f"
    ln -s "/app/data/$f" "/app/$f"
done

# 目录软链
for d in auths screenshots; do
    rm -rf "/app/$d"
    ln -s "/app/data/$d" "/app/$d"
done

# ---------------------------------------------------------------------------
# Self-check: 关键 import 白名单 — 失败即 crash-loop(SPEC-3 §2)
# ---------------------------------------------------------------------------
echo "[self-check] verifying critical imports..."
uv run python - <<'PYEOF' || {
    echo "[self-check] FATAL: critical import failed - image is likely stale or broken." >&2
    echo "[self-check] If you just pulled new code, the image must be rebuilt:" >&2
    echo "[self-check]   docker compose down" >&2
    echo "[self-check]   docker compose build --no-cache --build-arg GIT_SHA=\$(git rev-parse --short HEAD)" >&2
    echo "[self-check]   docker compose up -d" >&2
    exit 1
}
import sys

# 核心 web 应用入口
from autoteam.api import app  # noqa: F401

# accounts.py 全部对外契约符号(典型 typo 高发区)
from autoteam.accounts import (  # noqa: F401
    load_accounts,
    save_accounts,
    STATUS_ACTIVE,
    STATUS_EXHAUSTED,
    STATUS_STANDBY,
    STATUS_PENDING,
    STATUS_PERSONAL,
    STATUS_AUTH_INVALID,
    STATUS_ORPHAN,
)

# manager → accounts 链路(覆盖跨模块 typo)
from autoteam.manager import sync_account_states  # noqa: F401

print("[self-check] OK: %d critical symbols imported." % 11)
sys.exit(0)
PYEOF
echo "[self-check] passed."

# 执行命令
exec uv run autoteam "$@"
