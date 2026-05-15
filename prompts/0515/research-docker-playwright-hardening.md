# 0515 调研记录：Docker 与 Playwright 稳定性加固

## 调研目标

本次任务不是从 `D:\Desktop\autoteam-1` 整包迁移，而是在当前 `D:\Desktop\AutoTeam` 的主流程基础上，吸收对稳定性、资源边界、Docker 部署和浏览器生命周期更有价值的部分。

## 外部资料

- Docker Compose services reference: <https://docs.docker.com/reference/compose-file/services/>
- Docker Compose deploy resources reference: <https://docs.docker.com/reference/compose-file/deploy/#resources>
- Playwright Python Docker guide: <https://playwright.dev/python/docs/docker>
- Playwright Docker guide: <https://playwright.dev/docs/docker>

## 可采纳结论

- `init: true` 适合作为容器内 PID 1 兜底，用于信号转发和僵尸进程回收。当前服务长期运行、会启动 Chromium/Playwright 子进程，适合启用。
- Chromium 在 Docker 默认 `/dev/shm` 较小的环境下容易不稳定。Playwright 官方建议 `--ipc=host`；对单机 compose 更保守的做法是先设置 `shm_size: "1gb"`，保持隔离同时提升 Chromium 稳定性。
- `healthcheck` 应检查真实 API 可用性，而不是只检查进程存在。当前项目已有 `/api/version` 免鉴权端点，可作为低风险健康检查目标。
- 资源限制应避免“无限增长”：内存、PID 和浏览器子进程数量需要边界。compose 可使用 `mem_limit` / `pids_limit` 等直接服务字段，也可在兼容场景下补充 `deploy.resources`。
- Playwright 官方镜像包含浏览器和系统依赖；当前项目已有自建 `python:3.12-slim + playwright install-deps chromium` 路径，可以先保留，只新增 `Dockerfile.fast` 作为增量构建辅助，不替换默认稳定构建。

## 本地代码对照

当前 `D:\Desktop\AutoTeam` 已有：

- `Dockerfile`：已有 `GIT_SHA` / `BUILD_TIME` build args、OCI label、`/api/version` 对应环境变量。
- `docker-entrypoint.sh`：已有关键 import self-check，失败会 `exit 1` 触发 crash-loop。
- `tests/integration/test_docker_guard.py`：已有 Dockerfile、compose build args、entrypoint self-check、ruff F821 守卫。
- `src/autoteam/_playwright_guard.py` 与 `api.py:_PlaywrightExecutor`：已有 sync Playwright 专用线程和 asyncio loop 防护。
- free 注册相关链路已经比 `autoteam-1` 更新，包含 `leave_workspace=True`、`_run_post_register_oauth`、Personal OAuth 重试、plan drift 记录、workspace pool、注册双路径和一批 Round 11/12 测试。

`D:\Desktop\autoteam-1\AutoTeam` 可吸收：

- `docker-compose.yml` 中的 `init: true`、`shm_size: "1gb"`、`mem_limit`、`memswap_limit`、`pids_limit`、`AUTOTEAM_MEMORY_WARN_RATIO`、`AUTOTEAM_ZOMBIE_WARN_THRESHOLD`。
- `Dockerfile.fast` 的增量构建思路，但必须补齐当前项目已有的 build args、OCI label 和 self-check 约束。
- `src/autoteam/runtime_resources.py` 的 cgroup memory / pids / browser zombie 轻量探针。
- `src/autoteam/playwright_lifecycle.py` 的统一关闭顺序。
- `src/autoteam/chatgpt_transport.py` 的 `curl_cffi` HTTP transport 思路；按母板默认 `auto` 用于 Team backend API，但不能改变 free 注册和浏览器 OAuth 行为。
- `src/autoteam/playwright_probe.py` 的短生命周期 Playwright 探针，用于把后台人数探测隔离到可超时 kill 的子进程。

## curl_cffi 参考

Context7 查询达到额度限制，本轮回退到官方文档。`curl_cffi` 官方文档说明 `Session` 会复用 cookies/连接，并支持 `impersonate`、`proxies`、`timeout`、`allow_redirects` 等参数；这与本次 HTTP transport 的实现点一致。经第二轮对照母板，当前项目默认值调整为 `CHATGPT_API_TRANSPORT=auto`，但官方能力不等于能稳定绕过 ChatGPT/Cloudflare challenge，遇到 HTML/challenge/401 等情况必须回退浏览器。

参考：<https://curl-cffi.readthedocs.io/en/stable/index.html>、<https://curl-cffi.readthedocs.io/en/stable/_modules/curl_cffi/requests/session.html>

## 约束

- 不直接覆盖当前 `src/autoteam/manager.py`、`codex_auth.py`、`oauth_workspace.py`、`mail/register_dual_path.py`。
- 不把 `autoteam-1` 的旧注册实现回贴到当前项目，因为当前项目已有更晚的 Round 11/12 修复。
- `curl_cffi` 已按母板默认 `auto` 引入；不得替代 free 注册、Personal OAuth、验证码或 workspace UI 的真实浏览器路径，避免改变主流程行为证据。
- 所有 Docker 改动必须有静态测试和 `docker compose config` 验证；真正构建镜像前需要先确认本地 Docker 可用。
