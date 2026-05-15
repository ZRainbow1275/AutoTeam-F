# Docker 部署

## 快速开始

```bash
git clone https://github.com/cnitlrt/AutoTeam.git
cd AutoTeam

mkdir -p data
cp .env.example data/.env

# 编辑 data/.env
docker compose up -d
```

常用命令：

```bash
docker compose logs -f
docker compose restart
docker compose down
```

当前 `docker-compose.yml` 默认启用以下运行时加固：

- `init: true`：容器内启用 init/reaper，帮助回收 Chromium / Playwright 子进程。
- `shm_size: "1gb"`：提高 `/dev/shm`，降低 Chromium 在 Docker 默认 64MB shm 下崩溃的概率。
- `mem_limit: "2g"` / `pids_limit: 768`：给浏览器和后台任务设置硬边界，避免异常增长拖垮宿主机。
- `healthcheck`：通过 `http://127.0.0.1:8787/api/version` 检查真实 API 可用性。
- `AUTOTEAM_MEMORY_WARN_RATIO` / `AUTOTEAM_ZOMBIE_WARN_THRESHOLD`：控制运行时资源告警阈值。

运行时默认启用与 `autoteam-1` 对齐的轻量 Team API transport：

- 默认 `CHATGPT_API_TRANSPORT=auto`，Team backend API 读取会先尝试 HTTP transport；如果返回 Cloudflare/HTML/challenge 或鉴权异常，会回退 Playwright。
- 显式设置 `CHATGPT_API_TRANSPORT=playwright` 时，可强制恢复旧的浏览器上下文 fetch 行为。
- 该选项只影响管理员 Team API 读写；free 帐号注册、Personal OAuth、验证码、workspace UI 选择必须继续强制真实浏览器上下文。

## 数据持久化

所有运行数据都存储在 `data/` 目录，通过 volume 挂载到容器：

| 文件 / 目录 | 说明 |
|-------------|------|
| `data/.env` | 配置文件 |
| `data/accounts.json` | 账号池状态 |
| `data/state.json` | 管理员登录态 |
| `data/auths/` | Codex 认证文件 |
| `data/screenshots/` | 调试截图 |

重建容器不会丢失这些数据。

> 如果你使用了 `pull-cpa`，从 CPA 导入的认证文件也会落在 `data/auths/` 中。

## 手动构建

```bash
docker build -t autoteam .
docker run -d -p 8787:8787 -v $(pwd)/data:/app/data autoteam
```

### 快速增量镜像

首次完整构建后，本仓库提供 `Dockerfile.fast` 用于本地快速迭代。它复用 `autoteam:latest` 中已经安装好的系统依赖、uv 和 Playwright Chromium，只覆盖 Python 依赖与源码。

```bash
# 先确保有稳定基础镜像
GIT_SHA=$(git rev-parse --short HEAD) \
BUILD_TIME=$(date -u +%FT%TZ) \
docker build -t autoteam:latest .

# 后续本地快速迭代
GIT_SHA=$(git rev-parse --short HEAD) \
BUILD_TIME=$(date -u +%FT%TZ) \
docker build -f Dockerfile.fast -t autoteam:fast .
```

`Dockerfile.fast` 仅用于开发迭代，不替代首次完整构建；如果系统依赖、Playwright 版本、基础镜像或 `uv.lock` 出现难以解释的问题，回到标准 `Dockerfile` 做 `--no-cache` 构建。

## 配置方式

### 方式一：预先编辑 `.env`

启动前编辑 `data/.env`，容器启动后即可直接使用。

### 方式二：Web 页面配置

不预先配置直接启动，打开：

```text
http://host:8787
```

浏览器中会显示配置向导页面，填写后自动验证连通性。

## 容器中的文件权限

容器以 root 运行，`docker-entrypoint.sh` 会把 `/app/data` 下的文件设为可写。

如果你在宿主机上看到部分认证文件类似：
- `nobody:nogroup`
- `600`

通常不影响容器内运行；如需宿主机直接查看，可手动调整权限。

## 常见问题

### 容器一直重启

查看日志：

```bash
docker compose logs
```

通常是：
- 配置缺失
- CloudMail / CPA 连通性验证失败
- entrypoint self-check 发现镜像代码与契约符号不一致
- `/api/version` healthcheck 持续失败

查看健康状态：

```bash
docker compose ps
docker inspect --format '{{json .State.Health}}' autoteam-autoteam-1 | python -m json.tool
```

查看资源占用和 PID 数：

```bash
docker stats
docker compose top
```

如果日志出现 `[资源] ... browser zombie processes=...`，优先确认 compose 中 `init: true` 仍然存在；如果出现 memory usage warning，先减少并发注册/轮转，再考虑调大 `mem_limit`。

### `data` 目录没有写权限

容器入口会自动 `chmod -R 777 /app/data`。如果宿主机仍无法访问：

```bash
sudo chmod -R 777 data/
```

### 重建后配置丢失

确保 `docker-compose.yml` 中有 volume 挂载：

```yaml
volumes:
  - ./data:/app/data
```

### 反向同步后 `data/auths` 里出现重复文件名风格

新版本会在同步时自动做去重，并统一为本地命名规范。若你怀疑历史版本留下了旧文件，执行一次：

```bash
uv run autoteam pull-cpa
```

即可重新整理。

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
# [self-check] OK: 15 critical symbols imported.
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

### lint 守卫(开发期)

`pyproject.toml` 已配置 ruff(F401/F811/F821 三条规则),`.pre-commit-config.yaml` 也接入了同样的检查。

首次启用:

```bash
uv sync                           # 装 dev 依赖(pre-commit、ruff 已声明)
uv run pre-commit install         # 注入 .git/hooks/pre-commit
uv run pre-commit run --all-files # 一次性扫全仓,确认基线干净
```

之后每次 `git commit` 会自动跑 ruff;手动检查可:

```bash
uv run ruff check src/
```
