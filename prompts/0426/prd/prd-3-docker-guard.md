# PRD-3: Docker 镜像守卫

## 0. 元数据

| 字段 | 值 |
|---|---|
| PRD 编号 | PRD-3 |
| 主笔 | prd-docker(autoteam-prd-0426 team) |
| 起草日期 | 2026-04-26 |
| 关联 Issue | issues1 #3(`list_accounts` ImportError) |
| 关联调研 | `prompts/0426/research/issue-3-list-accounts-import.md` |
| 关联代码修复 | commit `cf2f7d3 fix(round-3): exhausted 自愈 + workspace 指纹 + CPA 删除守卫 + 双 typo` |
| 优先级 | P1(防回归 / 用户体验) |
| 风险 | 低(纯外围加固,不动业务代码) |

---

## 1. 背景

issues1 #3 报告:WebUI 点"生成免费号"返回 500,根因是 `api.py` 里 `from autoteam.accounts import list_accounts`(应为 `load_accounts`)的 typo。

代码层 typo 已在 commit `cf2f7d3` 修复并落仓 — `grep -rn "from autoteam\.accounts import.*list_accounts" src` 完全干净。但用户在 11:04 仍看到同样报错,因为:

1. **Dockerfile 第 23-25 行**用的是 `COPY src/ src/`,源代码在 `docker build` 时被烤进镜像
2. **docker-compose.yml** 只挂 `./data:/app/data` 数据卷,**没有挂源代码 volume**
3. 因此 `git pull` 拿到新代码后,**必须 rebuild 镜像才会生效** — 用户漏了这一步

**真因 = 镜像未重建**,不是代码 bug。这一类"代码已修但部署没生效"的 confusion 大概率会复发,本 PRD 目的是把"镜像版本可观察 + 启动期自检 + 静态守卫 + SOP 文档"四道防线建好,让下次再有类似 typo 时:

- CI 静态 lint 直接拦截 → typo 进不了主分支
- 万一漏过,容器启动期 self-check 立即 crash-loop → 用户立刻知道是镜像版本问题
- `/api/version` 端点曝露 git-sha → 用户/排错者能 1 秒确认"当前跑的是哪个 commit"
- `docs/docker.md` 给出标准 rebuild SOP → 用户不再漏步骤

---

## 2. 目标

| ID | 目标 |
|---|---|
| G1 | 容器启动期自检关键 import,失败立即 crash,**不要**让用户在 WebUI 按按钮才发现 |
| G2 | 镜像内嵌 git-sha,通过 `echo $AUTOTEAM_GIT_SHA` / `/api/version` / `docker image inspect` 三种姿势可查 |
| G3 | `pyflakes` / `ruff` 类静态检查接入 CI 与 pre-commit,堵住 typo 类 ImportError 在源头 |
| G4 | `docs/docker.md` 增加"代码更新后如何 rebuild"的标准 SOP,语句直接可复制 |
| G5 | `docker-compose.yml` 增加 `build.args.GIT_SHA`,让 `docker compose build` 一键注入版本 |

---

## 3. 非目标

- ❌ **不**改 `src/autoteam/accounts.py` 加任何向后兼容别名(`list_accounts = load_accounts` 类掩盖性兜底)
- ❌ **不**引入 hot reload / volume mount 源码方案 — 仍维持"镜像 = 不可变制品"语义
- ❌ **不**做 docker 镜像签名 / SBOM / Trivy 扫描等供应链加固(超出本 issue 范围)
- ❌ **不**改业务逻辑、不动 manager.py / api.py 核心路径
- ❌ **不**搞自动化 CI/CD pipeline(本仓库目前无 GH Actions,只在本地 / pre-commit 落地 lint)

---

## 4. 用户故事

### 故事 A:运维更新代码

> 作为运维同学,当 upstream 推送新 commit 后,我能照 docs/docker.md 里 4 条命令完成 rebuild → 启动 → 验证,**不会**因为漏掉 `--no-cache` 或忘了 `docker compose down` 而仍跑旧版镜像。

### 故事 B:故障排查"镜像版本"

> 作为故障排查者,当用户报"我已经更新到最新版了但 bug 还在",我能在 30 秒内通过 `curl http://host:8787/api/version` 或 `docker compose exec autoteam env | grep AUTOTEAM_GIT_SHA` 拿到镜像的 git-sha,直接对比 `git log` 就能判定到底跑的是不是最新 commit。

### 故事 C:开发期 typo 拦截

> 作为开发者,当我把 `load_accounts` 写成 `list_accounts` 这样的 typo 时,**还没 commit** 的 pre-commit 钩子就拒绝我;就算我 `--no-verify` 强推,**容器启动期 self-check** 也立刻 crash 而不是潜伏到运行时。

---

## 5. 功能需求

### 5.1 entrypoint self-check(启动期 import 关键符号自检)

**目的**:把 ImportError 从"运行时偶发"提前到"启动期必现",触发 docker restart_policy crash-loop。

**位置**:`docker-entrypoint.sh` 在最后 `exec uv run autoteam "$@"` 之前。

**自检内容(白名单 import)**:

```bash
# Self-check: critical imports
uv run python -c "
from autoteam.api import app
from autoteam.accounts import (
    load_accounts, save_accounts,
    STATUS_ACTIVE, STATUS_EXHAUSTED, STATUS_STANDBY,
    STATUS_PERSONAL, STATUS_AUTH_INVALID, STATUS_ORPHAN,
)
from autoteam.manager import sync_account_states
print('[self-check] OK')
" || {
    echo "[self-check] FATAL: critical import failed — image is likely stale or broken." >&2
    echo "[self-check] Run: docker compose build --no-cache && docker compose up -d" >&2
    exit 1
}
```

**设计要点**:

- 只检查"曾经被 typo 误伤"或"对外契约稳定"的核心符号 — 避免一改文件就要更新自检列表
- 失败时打印**修复提示语**(rebuild 命令),省去用户 google 时间
- 用 `uv run python -c` 而非 `python -c`,与项目 runtime 一致(避免依赖未装齐导致的伪阴性)
- 自检整体 < 1 秒,**不阻塞**正常启动

### 5.2 镜像 git-sha 标签 + /api/version 端点

**5.2.1 Dockerfile 注入**

```dockerfile
# 在 WORKDIR /app 之后、COPY src/ 之前增加
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
LABEL org.opencontainers.image.created=$BUILD_TIME
ENV AUTOTEAM_GIT_SHA=$GIT_SHA
ENV AUTOTEAM_BUILD_TIME=$BUILD_TIME
```

**5.2.2 /api/version 端点**

在 `src/autoteam/api.py` 现有 `app = FastAPI(...)` 定义之后增加:

```python
import os

@app.get("/api/version")
def api_version() -> dict:
    return {
        "git_sha": os.getenv("AUTOTEAM_GIT_SHA", "unknown"),
        "build_time": os.getenv("AUTOTEAM_BUILD_TIME", "unknown"),
    }
```

**5.2.3 多种查询姿势**(都应能拿到同一个 git-sha):

```bash
# 容器外:HTTP 端点
curl http://localhost:8787/api/version

# 容器内:环境变量
docker compose exec autoteam env | grep AUTOTEAM_GIT_SHA

# 镜像层:OCI label
docker image inspect autoteam-autoteam | grep revision
```

### 5.3 lint 守卫(防止 list_accounts 这类 typo 再次发生)

**工具选型**:`ruff check --select F401,F811,F821`

| Rule | 含义 |
|---|---|
| F401 | imported but unused — 抓"import 了但没用"的死 import |
| F811 | redefinition of unused — 抓重复定义 |
| F821 | undefined name — **核心**,抓 `from autoteam.accounts import list_accounts`(未定义名)这种典型 typo |

**为什么选 ruff 而非 pyflakes/mypy**:

- ruff 单一二进制,启动 < 100ms,本地循环不痛
- 已是 Python 生态主流(本仓 pyproject.toml 可顺便接 ruff format)
- mypy 太重(类型完整推断要 5-10s),pre-commit 卡顿;ruff F821 已能覆盖 ImportError 这条主线

**落地点(三层防线)**:

1. **pyproject.toml** 加 `[tool.ruff.lint]` 段配置:
   ```toml
   [tool.ruff.lint]
   select = ["F401", "F811", "F821"]
   exclude = ["tests/", "docs/", "scripts/"]
   ```
2. **pre-commit hook**(`.pre-commit-config.yaml`):
   ```yaml
   repos:
     - repo: https://github.com/astral-sh/ruff-pre-commit
       rev: v0.7.0
       hooks:
         - id: ruff
           args: [--select, "F401,F811,F821"]
   ```
3. **docs/docker.md / docs/getting-started.md** 增加"开发前先 `pip install pre-commit && pre-commit install`"提示

### 5.4 docs/docker.md 增加 rebuild SOP

在 `docs/docker.md` 末尾新增章节:

```markdown
## 代码更新后的 rebuild SOP

> 关键认知:本项目 Dockerfile 用 `COPY src/`(非 volume mount),
> **`git pull` 后必须 rebuild 镜像**,代码改动才会进入容器。

### 标准更新流程(4 步)

```bash
# 1. 拉新代码
cd /path/to/AutoTeam && git pull

# 2. 停旧容器
docker compose down

# 3. 重建镜像(--no-cache 防意外缓存命中,GIT_SHA 注入版本指纹)
docker compose build --no-cache --build-arg GIT_SHA=$(git rev-parse --short HEAD) --build-arg BUILD_TIME=$(date -u +%FT%TZ)

# 4. 启动
docker compose up -d
```

### 验证镜像版本

```bash
# 方式 A:HTTP 端点
curl http://localhost:8787/api/version
# 期望:{"git_sha":"cf2f7d3","build_time":"2026-04-26T..."}

# 方式 B:进容器查环境变量
docker compose exec autoteam env | grep AUTOTEAM_GIT_SHA

# 方式 C:看镜像 OCI label
docker image inspect autoteam-autoteam --format '{{.Config.Labels}}'
```

### 故障排查:为什么修了代码,bug 还在?

99% 是镜像没 rebuild。先跑这条:

```bash
curl -s http://localhost:8787/api/version | python -c "import sys,json; d=json.load(sys.stdin); print('image sha:', d['git_sha']); print('repo head:', open('.git/HEAD').read())"
```

如果 `image sha` 与最新 commit 不一致 → 重做上面 4 步 SOP。
```

### 5.5 docker-compose.yml 增加 build args

修改 `docker-compose.yml`:

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

配套 `Makefile` 或 docs 提供一键命令:

```bash
GIT_SHA=$(git rev-parse --short HEAD) BUILD_TIME=$(date -u +%FT%TZ) docker compose build --no-cache
```

---

## 6. 非功能需求

| 维度 | 要求 |
|---|---|
| 启动延迟 | self-check 增加 < 1 秒 |
| 镜像体积 | 增量 < 5KB(只是增加 LABEL/ENV/几行 entrypoint) |
| 兼容性 | 不破坏 `docker compose up` 老用户体验 — 不传 `GIT_SHA` 时降级为 `unknown` 而非报错 |
| 可观察性 | git-sha 三种姿势可查(端点 + 环境变量 + label),互为冗余 |
| 安全性 | `/api/version` 只暴露 git-sha 与构建时间,**不**暴露任何 secret / 路径 / 主机名 |
| 可回滚 | 所有改动可独立回退(删 entrypoint 那段、删 lint 配置、删 /api/version 端点) |

---

## 7. 技术方案

### 7.1 entrypoint 自检脚本设计(bash + uv run python -c)

**为什么用 `uv run python -c` 而不是单独脚本**:

- 项目 runtime 用 uv 管理虚拟环境,自检必须走同一环境(否则可能用了系统 python 装的旧包,假阳性)
- 单行 inline `-c` 比额外 `scripts/self_check.py` 更易维护(和 entrypoint 同位置,不会失同步)

**为什么白名单粒度到符号常量**:

- 只检查 `from autoteam.api import app` 不够 — `app` 这种顶层对象 import 失败概率低,而 `STATUS_*` 常量、`load_accounts` 函数才是 typo 高发区
- 检查 `from autoteam.manager import sync_account_states` 顺带覆盖 manager → accounts 的链路

**失败行为**:

- `set -e` 配合 `||{ ...; exit 1; }` → bash 立即非零退出
- docker `restart: unless-stopped` 会触发 crash-loop,容器列表里看到 `Restarting (1)` → 立刻 `docker compose logs` 就能看到自检报错全文

### 7.2 镜像构建注入 GIT_SHA(--build-arg)

**3 处声明,一处真正接收**:

1. `docker-compose.yml`:`build.args.GIT_SHA: ${GIT_SHA:-unknown}` — 接受 shell 环境变量
2. `Dockerfile`:`ARG GIT_SHA=unknown` — 镜像构建期参数
3. `Dockerfile`:`ENV AUTOTEAM_GIT_SHA=$GIT_SHA` — 持久化到镜像运行环境
4. `Dockerfile`:`LABEL org.opencontainers.image.revision=$GIT_SHA` — OCI 标准 label,`docker image inspect` 可查

**降级语义**:

- 用户没传 `GIT_SHA`(直接 `docker compose build`)→ 默认值 `unknown`,**不报错**
- `/api/version` 返回 `{"git_sha": "unknown", ...}` → 提示用户"本镜像未注入版本"
- 这是渐进改进,不强制所有人都用,**老的 build 命令仍能工作**

### 7.3 lint 工具选型(ruff)

**对比表**:

| 工具 | F821 检测 | 启动延迟 | 配置成本 | 选用 |
|---|---|---|---|---|
| **ruff** | ✅ 内建 | < 100ms | 单文件 toml | ✅ |
| pyflakes | ✅ 内建 | ~500ms | 无配置 | ❌(被 ruff 包含) |
| mypy | ✅ 完整类型 | 5-10s | 复杂 | ❌(太重) |
| pylint | ✅ | 3-5s | 复杂 | ❌(太重) |

**只启 F401/F811/F821 三条规则的理由**:

- 这三条是"必然 bug"(死 import / 重复定义 / 未定义名),零误报
- 不开启 style 类规则 → 不打扰现有代码风格
- 后续可逐步追加 E/W/B 等,但本 PRD 不扩张

### 7.4 文档结构变更

- `docs/docker.md`:**追加**章节"代码更新后的 rebuild SOP"+"故障排查"
- `docs/getting-started.md`:**追加**一行 pre-commit 安装提示(可选)
- `docs/troubleshooting.md`:**追加**章节"镜像版本不一致 / 修了代码 bug 还在"
- `pyproject.toml`:**追加** `[tool.ruff.lint]` 段
- `.pre-commit-config.yaml`:**新建**(如不存在)

---

## 8. 验收标准

| ID | 验收点 | 检验方式 |
|---|---|---|
| AC1 | self-check 在镜像内能跑通 | `docker compose exec autoteam bash docker-entrypoint.sh --noop` 无报错 |
| AC2 | self-check 能拦截 typo | 临时把 `accounts.py` 里 `load_accounts` 改名 → rebuild → 启动应 crash-loop |
| AC3 | /api/version 返回正确字段 | `curl /api/version` 返回 `{git_sha, build_time}` 两字段且非空 |
| AC4 | git-sha 三处一致 | env / curl / label 三种方式查到的 sha 字符串完全相同 |
| AC5 | ruff F821 拦截 typo | 写一行 `from autoteam.accounts import list_accounts`,`ruff check src/` 应非零退出 |
| AC6 | pre-commit 触发 | `git commit` 含上述 typo 时被 pre-commit 拒绝 |
| AC7 | docs/docker.md SOP 可复制粘贴跑通 | 新人照文档 4 步操作,可成功 rebuild + 验证 |
| AC8 | 不传 GIT_SHA 不报错 | `docker compose build`(无 --build-arg)仍能成功,`/api/version` 返回 `unknown` |

---

## 9. 测试计划

### 9.1 本地 docker 端到端

```bash
# 步骤 1:基础 rebuild + 启动
cd D:/Desktop/AutoTeam
docker compose down
GIT_SHA=$(git rev-parse --short HEAD) BUILD_TIME=$(date -u +%FT%TZ) docker compose build --no-cache
docker compose up -d

# 步骤 2:验证 self-check 通过
docker compose logs autoteam | grep "[self-check] OK"

# 步骤 3:验证 /api/version
curl http://localhost:8787/api/version
# 期望:{"git_sha":"cf2f7d3","build_time":"2026-04-26T..."}

# 步骤 4:验证 env / label
docker compose exec autoteam env | grep AUTOTEAM_GIT_SHA
docker image inspect autoteam-autoteam | grep revision
```

### 9.2 self-check 故障注入

```bash
# 步骤 1:临时损坏 accounts.py
sed -i.bak 's/^def load_accounts/def loadaccounts/' src/autoteam/accounts.py

# 步骤 2:rebuild + 启动 → 应 crash-loop
docker compose build --no-cache
docker compose up -d
sleep 3
docker compose ps  # 期望:autoteam 状态 Restarting
docker compose logs autoteam | grep "FATAL"

# 步骤 3:恢复
mv src/autoteam/accounts.py.bak src/autoteam/accounts.py
docker compose build --no-cache && docker compose up -d
```

### 9.3 lint 守卫验证

```bash
# 步骤 1:制造 typo
echo "from autoteam.accounts import list_accounts" >> src/autoteam/_lint_canary.py

# 步骤 2:期望 ruff 拦截
ruff check src/ --select F401,F811,F821
# 期望:F821 Undefined name `list_accounts`,exit code 非零

# 步骤 3:期望 pre-commit 拦截
git add src/autoteam/_lint_canary.py
git commit -m "test"  # 期望被拒

# 步骤 4:清理
rm src/autoteam/_lint_canary.py
```

### 9.4 回归测试

- 现有 `tests/unit/`、`tests/integration/` 全套跑通
- WebUI 手测:进 setup wizard、点"生成免费号"、删除账号 — 三条主路径都不应受影响

---

## 10. 灰度 / 回滚

### 灰度策略

本 PRD 改动均为**外围加固**,不影响业务路径,无需金丝雀:

- 直接 merge 到 main → 用户 `git pull && docker compose build --no-cache` 即可生效
- 用户如未传 `GIT_SHA`,`/api/version` 返回 `unknown`,**功能不受影响**

### 回滚预案

| 风险点 | 回滚动作 |
|---|---|
| self-check 误拦截(白名单太严) | 删 `docker-entrypoint.sh` 末尾自检段,rebuild 即可 |
| ruff F821 误报 | `pyproject.toml` 临时关 F821:`select = ["F401","F811"]` |
| `/api/version` 引入异常 | 删 api.py 的 4 行端点定义即可 |
| docker-compose 用户 GIT_SHA 变量解析失败 | 移除 build.args 段,降级回原版 compose |

每一处改动都是**独立可回滚**单元。

---

## 11. 文档影响清单

| 文档 | 类型 | 变更内容 |
|---|---|---|
| `docs/docker.md` | 修改 | 追加"rebuild SOP"+"版本验证"+"故障排查"三章节 |
| `docs/getting-started.md` | 修改 | 追加 pre-commit 安装一行 |
| `docs/troubleshooting.md` | 修改 | 追加"镜像版本不一致"故障案例 |
| `docs/api.md` | 修改 | 追加 `/api/version` 端点说明 |
| `pyproject.toml` | 修改 | 新增 `[tool.ruff.lint]` 段 |
| `Dockerfile` | 修改 | 新增 ARG/LABEL/ENV |
| `docker-compose.yml` | 修改 | `build` 段从字符串扩为对象,新增 `args` |
| `docker-entrypoint.sh` | 修改 | 末尾追加 self-check 段 |
| `src/autoteam/api.py` | 修改 | 新增 `/api/version` 路由 |
| `.pre-commit-config.yaml` | 新建 | ruff hook |
| `CHANGELOG.md` | 修改 | 添加本 PRD 落地记录 |

---

## 12. 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| self-check 白名单失同步(代码改了符号名,没改自检列表) | 中 | 容器启动失败 | 自检列表只列"长期稳定"符号(STATUS_*、load_accounts、save_accounts 等);任何 PR 改这些符号需同步改自检 |
| ruff F821 在动态 import 场景误报 | 低 | 开发体验下降 | F821 默认对动态 import 友好;真出现误报可加 `# noqa: F821` 单行豁免 |
| 用户老镜像没有 self-check,但用了新版 docker-compose.yml | 低 | 不报错,只是缺保护 | 不影响功能,docs SOP 引导用户先 rebuild 再使用 |
| `/api/version` 被滥用作健康探针 | 低 | 偶尔多打几个请求 | 端点本身无副作用,被频繁调用也无成本;真要做 healthcheck 应建议另开 `/api/healthz` |
| pre-commit 安装心智门槛 | 中 | 部分开发不装,守卫只剩本地 ruff | 至少 docs 写明,且 ruff 单独可运行(`ruff check src/`),不强依赖 pre-commit |

---

## 13. 未决问题

| ID | 问题 | 决策路径 |
|---|---|---|
| Q1 | self-check 是否要把所有 mail provider 类也 import?(`from autoteam.mail.maillab import MaillabClient` 等) | 暂**不**加,理由:provider 是可拔插模块,缺失不应 fatal;留待 PRD-1 mail provider 重构后再评估 |
| Q2 | 是否同时上 `docker compose --profile dev` 用于开发期 hot reload? | 暂**不**做,与本 PRD"镜像不可变"语义冲突,留待单独 RFC |
| Q3 | `/api/version` 是否需要鉴权? | **不**需要 — 只暴露 git-sha 不暴露 secret;且未鉴权能让运维无门槛排错 |
| Q4 | 是否要把 self-check 抽到独立脚本 `scripts/self_check.py` 便于 CI 直接调用? | **可选优化**,本 PRD 落地 inline 版本,后续若 CI 需要可重构 |
| Q5 | ruff 版本固定到 v0.7.x 还是跟 latest? | pre-commit 配置固定到 `v0.7.0`,pyproject.toml 不约束 — 让本地开发能用 latest,守卫线统一 |

---

## 14. Story Map

```
[运维更新代码]                        [开发提交 typo]                    [排错者诊断"代码改了 bug 还在"]
    │                                     │                                     │
    ├─ [git pull]                        ├─ [写代码]                          ├─ [curl /api/version]
    │                                     │                                     │
    ├─ [docker compose down]              ├─ [git commit]                      ├─ [对比 git log HEAD]
    │                                     │   │                                 │
    ├─ [docker compose build              │   ├─ [pre-commit ruff F821]        ├─ [发现 sha 不一致]
    │     --no-cache                      │   │       └─ ⛔ 拦截               │
    │     --build-arg GIT_SHA=...]        │   │                                 │
    │                                     │   └─ ✅ 通过 → push                ├─ [告诉用户:rebuild!]
    ├─ [docker compose up -d]             │                                     │
    │                                     ├─ [docker build]                    └─ [按 SOP 重做]
    ├─ [curl /api/version 验证版本]       │   └─ [self-check 启动期再守一道]
    │                                     │
    └─ ✅ 完成                            └─ ✅ 部署成功
                                              [若漏过] → ⛔ crash-loop + 报错日志
```

四道防线层层递进:**lint(开发期) → self-check(启动期) → /api/version(运行期) → docs SOP(操作期)**。
任何一道未拦截 typo 类问题,下一道兜底。

---
