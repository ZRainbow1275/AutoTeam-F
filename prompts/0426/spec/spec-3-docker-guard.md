# SPEC-3: Docker 镜像守卫 实施规范

## 0. 元数据

| 字段 | 值 |
|---|---|
| SPEC 编号 | SPEC-3 |
| 主笔 | prd-docker(autoteam-prd-0426) |
| 起草日期 | 2026-04-26 |
| 上游 PRD | `prompts/0426/prd/prd-3-docker-guard.md` |
| 关联调研 | `prompts/0426/research/issue-3-list-accounts-import.md` |
| 关联代码修复 | commit `cf2f7d3 fix(round-3): ...` |
| 实施优先级 | P1 |
| 预计耗时 | 2-3 小时(单人) |

> 本 SPEC 提供"打开就能编码"的所有具体内容:实际 entrypoint 脚本、Dockerfile diff、ruff 配置文件、`/api/version` 端点 Pydantic 响应模型、`docs/docker.md` SOP 完整文案。直接复制粘贴即可落地,**无需再做技术决策**。

---

## 1. 文件级修改清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `Dockerfile` | 修改 | 在 `WORKDIR /app` 之后增加 ARG/LABEL/ENV |
| `docker-compose.yml` | 修改 | `build: .` 扩为对象 + `args: GIT_SHA / BUILD_TIME` |
| `docker-entrypoint.sh` | 修改 | 在 `exec uv run autoteam` 之前增加 self-check |
| `pyproject.toml` | 修改 | 新增 `[tool.ruff.lint]` 段(dev 依赖已声明 ruff>=0.15.10) |
| `.pre-commit-config.yaml` | 新建 | ruff hook(规则 F401/F811/F821) |
| `src/autoteam/api.py` | 修改 | 在 `app = FastAPI(...)` 之后新增 Pydantic VersionResponse + `/api/version` 路由 |
| `docs/docker.md` | 修改 | 末尾追加"代码更新后的 rebuild SOP"+"版本验证"+"故障排查"三章节 |
| `docs/api.md` | 修改 | 追加 `/api/version` 端点说明 |
| `CHANGELOG.md` | 修改 | 添加 SPEC-3 落地记录 |

---

## 2. entrypoint self-check 脚本(完整文件)

文件:`docker-entrypoint.sh`(在现有内容末尾、`exec uv run autoteam "$@"` 之前**插入**下列段)。

```bash
#!/bin/bash
set -e

# 清理残留锁文件并启动虚拟显示器
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# 确保数据目录存在且可写
mkdir -p /app/data /app/data/auths /app/data/screenshots
chmod -R 777 /app/data

# 数据文件:无条件软链到 data/(确保所有写入都持久化)
for f in .env accounts.json state.json; do
    [ -f "/app/data/$f" ] || touch "/app/data/$f"
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
```

**设计要点**:

1. `uv run python - <<'PYEOF' ... PYEOF` heredoc 写法保证脚本与 entrypoint 同源,不需另起 `scripts/self_check.py`
2. heredoc 用单引号 `'PYEOF'` — 禁止 shell 变量插值,避免 `$variable` 被宿主 bash 解析
3. `||{ ... ; exit 1; }` — Python 进程非零退出时立即输出 rebuild 提示并 `exit 1`,触发 docker `restart: unless-stopped` 的 crash-loop
4. 自检符号只列 **11 个稳定契约**(2 函数 + 7 状态常量 + 1 外部 app + 1 跨模块函数),不列易变函数;改这些符号需同步改自检
5. 整体延迟 **< 1 秒**(冷启动 Python 解释器 + 单文件 import)

---

## 3. Dockerfile diff(完整新版本)

```dockerfile
FROM python:3.12-slim

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    curl \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# === SPEC-3 §3: 镜像版本指纹注入 ===
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"
LABEL org.opencontainers.image.created="${BUILD_TIME}"
ENV AUTOTEAM_GIT_SHA="${GIT_SHA}"
ENV AUTOTEAM_BUILD_TIME="${BUILD_TIME}"
# === SPEC-3 §3 end ===

# 复制项目文件
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

# 安装 Playwright 浏览器
RUN uv run playwright install chromium && uv run playwright install-deps chromium

# 复制源码
COPY src/ src/
COPY web/ web/

# 数据卷
VOLUME ["/app/data"]

# 启动时将数据目录软链到工作目录
RUN mkdir -p /app/data
ENV DISPLAY=:99

EXPOSE 8787

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api"]
```

**关键约束**:

- ARG 块**必须在 WORKDIR 之后、`COPY pyproject.toml` 之前** — 这样 build args 变化时不会击穿后续昂贵的 `uv sync` / `playwright install` 缓存层
- 默认值 `unknown` 让"不传 build-arg 直接 build"也能跑成功(降级语义)
- `org.opencontainers.image.*` 是 OCI 标准 label,`docker image inspect` 自动展示

---

## 4. docker-compose.yml diff(完整新版本)

```yaml
services:
  autoteam:
    build:
      context: .
      args:
        GIT_SHA: ${GIT_SHA:-unknown}
        BUILD_TIME: ${BUILD_TIME:-unknown}
    ports:
      - "8787:8787"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

**关键约束**:

- `${GIT_SHA:-unknown}` — Compose 标准变量插值,未设环境变量时回落 `unknown`
- 用户日常 `docker compose build` 仍能成功(只是 sha 显示 unknown,**不报错**)
- 推荐一键命令(写入 `docs/docker.md`):

```bash
GIT_SHA=$(git rev-parse --short HEAD) BUILD_TIME=$(date -u +%FT%TZ) docker compose build --no-cache
```

---

## 5. /api/version 端点契约

文件:`src/autoteam/api.py`,在 **line 25** `app = FastAPI(...)` 定义之后、line 27 注释行之前**插入**:

```python
# ---------------------------------------------------------------------------
# 版本端点 (SPEC-3 §5) - 不鉴权,纯只读
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    """镜像版本指纹响应 - 来自 Dockerfile build args。"""

    git_sha: str
    build_time: str


@app.get(
    "/api/version",
    response_model=VersionResponse,
    summary="返回镜像构建期注入的 git-sha 与时间戳",
    tags=["meta"],
)
def api_version() -> VersionResponse:
    return VersionResponse(
        git_sha=os.getenv("AUTOTEAM_GIT_SHA", "unknown"),
        build_time=os.getenv("AUTOTEAM_BUILD_TIME", "unknown"),
    )
```

**鉴权豁免**:`/api/version` 必须加入 `_AUTH_SKIP_PATHS`(api.py:31)白名单。修改:

```python
_AUTH_SKIP_PATHS = {"/api/auth/check", "/api/setup/status", "/api/setup/save", "/api/version"}
```

**响应示例**:

```json
{
  "git_sha": "cf2f7d3",
  "build_time": "2026-04-26T03:00:00Z"
}
```

**安全保证**:端点只暴露 build-arg 注入的两个字符串,不读 secret / 路径 / 主机名,免鉴权安全。

---

## 6. ruff 配置(pyproject.toml 完整片段)

在 `pyproject.toml` 末尾(line 35 之后)**追加**:

```toml
# ---------------------------------------------------------------------------
# Ruff lint (SPEC-3 §6) - 仅启 F401/F811/F821 三条零误报规则
# ---------------------------------------------------------------------------
[tool.ruff]
target-version = "py310"
line-length = 120
src = ["src", "tests"]

[tool.ruff.lint]
# F401: imported but unused (死 import)
# F811: redefinition of unused (重复定义)
# F821: undefined name (核心 — 抓 typo 类 ImportError)
select = ["F401", "F811", "F821"]

# 测试 / 文档 / 脚本豁免(可能含 docstring 示例 / 故意未定义符号)
exclude = [
    "tests/",
    "docs/",
    "scripts/",
    ".venv/",
    "build/",
    "dist/",
]

[tool.ruff.lint.per-file-ignores]
# __init__.py 允许 re-export
"__init__.py" = ["F401"]
```

**为什么只选 F401/F811/F821**:

- 这三条**零误报**(语义错误而非风格),不打扰现有代码
- F821 是抓 `from autoteam.accounts import list_accounts` 这种 typo 的核心规则
- 后续如要追加 E/W/B 等 style 规则可单独 PR,不在本 SPEC 范围

**手动执行**(开发期自检):

```bash
uv run ruff check src/
```

---

## 7. pre-commit hook 配置

新建文件:`.pre-commit-config.yaml`(项目根目录)。

```yaml
# SPEC-3 §7: pre-commit ruff hook
# 安装:uv run pre-commit install
# 手动跑全仓:uv run pre-commit run --all-files

repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.0
    hooks:
      - id: ruff
        name: ruff (F401/F811/F821)
        args:
          - --select
          - F401,F811,F821
          - --exit-non-zero-on-fix
```

**首次启用步骤**(写入 docs):

```bash
uv sync                           # 装好 dev 依赖(pre-commit 已声明)
uv run pre-commit install         # 注入 .git/hooks/pre-commit
uv run pre-commit run --all-files # 一次性扫全仓,确认基线干净
```

---

## 8. docs/docker.md SOP 章节完整文案

在 `docs/docker.md` 末尾**追加**以下章节(原文档已有的内容保持不变):

```markdown
---

## 代码更新后的 rebuild SOP(SPEC-3 §8)

> **关键认知**:本项目 `Dockerfile` 用 `COPY src/`(非 volume mount),
> **`git pull` 后必须 rebuild 镜像**,代码改动才会进入容器。

### 标准更新流程(4 步)

```bash
# 1. 拉取新代码
cd /path/to/AutoTeam && git pull

# 2. 停掉旧容器
docker compose down

# 3. 重建镜像(--no-cache 防意外缓存命中,GIT_SHA 注入版本指纹)
GIT_SHA=$(git rev-parse --short HEAD) \
BUILD_TIME=$(date -u +%FT%TZ) \
docker compose build --no-cache

# 4. 启动
docker compose up -d
```

### 验证镜像版本(三选一,结果应一致)

```bash
# 方式 A:HTTP 端点(免鉴权)
curl http://localhost:8787/api/version
# 期望:{"git_sha":"cf2f7d3","build_time":"2026-04-26T..."}

# 方式 B:进容器查环境变量
docker compose exec autoteam env | grep AUTOTEAM_GIT_SHA

# 方式 C:看镜像 OCI label(无需启动容器)
docker image inspect autoteam-autoteam --format '{{json .Config.Labels}}'
```

### 启动期 self-check

容器每次启动都会执行 `[self-check]` 段,白名单 import 任一失败立即 `exit 1` → docker 进入 crash-loop。

```bash
docker compose logs autoteam | head -20
# 期望看到:
# [self-check] verifying critical imports...
# [self-check] OK: 11 critical symbols imported.
# [self-check] passed.
```

### 故障排查:为什么修了代码 bug 还在?

**99% 是镜像没 rebuild**。先跑这条快速诊断:

```bash
# 对比 image 内 sha 与 repo HEAD
echo "image:" && curl -s http://localhost:8787/api/version | python -m json.tool
echo "repo HEAD:" && git rev-parse --short HEAD
```

如果 `image.git_sha` 与 `repo HEAD` 不一致 → 重做上面 4 步 SOP。

如果 self-check 报 `FATAL: critical import failed`:
- 说明镜像里的源码与最新代码的契约符号对不上(典型 typo 引入未定义名)
- 解决:回退最近 commit 或修复 typo,再 rebuild
```

---

## 9. 测试用例

### 9.1 本地 docker 端到端(必跑)

```bash
cd D:/Desktop/AutoTeam

# 步骤 1: 全量 rebuild
docker compose down
GIT_SHA=$(git rev-parse --short HEAD) BUILD_TIME=$(date -u +%FT%TZ) docker compose build --no-cache

# 步骤 2: 启动 + 验证 self-check 通过
docker compose up -d
sleep 5
docker compose logs autoteam | grep -E "(self-check|FATAL)"
# 期望: "[self-check] OK" + "[self-check] passed"

# 步骤 3: 验证 /api/version
curl -s http://localhost:8787/api/version | python -m json.tool
# 期望: { "git_sha": "<7-char sha>", "build_time": "<ISO8601>" } 两字段都非 unknown

# 步骤 4: 验证 env 与 OCI label 一致
docker compose exec autoteam env | grep AUTOTEAM_GIT_SHA
docker image inspect autoteam-autoteam --format '{{.Config.Labels}}'
# 期望: 三处 sha 字符串完全相同
```

### 9.2 self-check 故障注入(回归保险)

```bash
# 步骤 1: 临时损坏 accounts.py
sed -i.bak 's/^def load_accounts/def loadaccounts/' src/autoteam/accounts.py

# 步骤 2: rebuild + 启动 → 应 crash-loop
GIT_SHA=test docker compose build --no-cache
docker compose up -d
sleep 5
docker compose ps  # 期望 STATUS = Restarting (1)
docker compose logs autoteam | grep "FATAL"
# 期望:命中 "[self-check] FATAL"

# 步骤 3: 恢复 + rebuild
mv src/autoteam/accounts.py.bak src/autoteam/accounts.py
docker compose build --no-cache && docker compose up -d
```

### 9.3 lint 守卫验证

```bash
# 步骤 1: 制造 typo
echo "from autoteam.accounts import list_accounts" > src/autoteam/_lint_canary.py

# 步骤 2: ruff 应拦截
uv run ruff check src/
# 期望: F821 Undefined name `list_accounts`,exit code = 1

# 步骤 3: pre-commit 应拦截
git add src/autoteam/_lint_canary.py
git commit -m "test"
# 期望:被 ruff hook 拒绝,commit 未生成

# 步骤 4: 清理
rm src/autoteam/_lint_canary.py
git reset HEAD
```

### 9.4 降级路径(不传 build-arg)

```bash
docker compose down
docker compose build --no-cache  # 不传 GIT_SHA / BUILD_TIME
docker compose up -d
sleep 5
curl -s http://localhost:8787/api/version
# 期望: { "git_sha": "unknown", "build_time": "unknown" } — 不报错,正常返回
```

---

## 10. 实施顺序

按依赖顺序串行(每步独立可回滚):

1. **`pyproject.toml` 增 `[tool.ruff]`** → 跑 `uv run ruff check src/` 确认基线干净
2. **`.pre-commit-config.yaml` 新建** → `uv run pre-commit install` + `pre-commit run --all-files`
3. **`src/autoteam/api.py` 增 `/api/version`** → 本地 `uv run uvicorn autoteam.api:app` 测 `curl /api/version`(返 unknown)
4. **`Dockerfile` 增 ARG/LABEL/ENV** → `docker build --build-arg GIT_SHA=test .` 测构建成功
5. **`docker-compose.yml` 改 build 段** → `docker compose config` 验语法
6. **`docker-entrypoint.sh` 增 self-check** → `docker compose build && docker compose up -d` 看日志
7. **`docs/docker.md` + `docs/api.md` 增章节** → 评审文档可读性
8. **`CHANGELOG.md` 加条目**
9. 跑完整 §9.1 ~ §9.4 端到端测试

---

## 11. 验收清单

| ID | 验收点 | 检验方式 | 通过条件 |
|---|---|---|---|
| AC1 | self-check 正常路径通过 | `docker compose logs` 含 "self-check passed" | ✅ 命中 |
| AC2 | self-check 拦截 typo | §9.2 故障注入后容器 crash-loop | ✅ STATUS=Restarting |
| AC3 | `/api/version` 返回正确字段 | `curl /api/version` 返 `git_sha + build_time` 双字段 | ✅ JSON 合法 |
| AC4 | 三处 sha 一致 | env / curl / label 三种姿势 sha 字符串相同 | ✅ 字符串相等 |
| AC5 | ruff F821 拦截 typo | §9.3 第 2 步 exit code = 1 | ✅ 非零退出 |
| AC6 | pre-commit 拦截 commit | §9.3 第 3 步 commit 失败 | ✅ commit 未生成 |
| AC7 | 不传 build-arg 不报错 | §9.4 返 unknown 正常 | ✅ 返 200 |
| AC8 | docs SOP 可粘贴执行 | 新人按 docs/docker.md 4 步操作 | ✅ 一次跑通 |
| AC9 | `/api/version` 免鉴权 | 不带 Bearer 直接 curl | ✅ 返 200 |
| AC10 | 现有功能无回归 | 既有测试套 `uv run pytest` 全绿 | ✅ 0 失败 |

---

**SPEC-3 完。** 实施时严格按 §10 顺序;遇任何违反 §11 验收点的情况,**必须回滚该步**而非强推 — 每步都是独立可逆单元。
