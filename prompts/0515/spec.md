# 0515 SPEC：autoteam-1 能力吸收与 free 注册保护契约

## 1. 总体契约

本任务只允许做“选择性吸收”。所有代码实现必须以当前 `D:\Desktop\AutoTeam` 为基线，以 `D:\Desktop\autoteam-1` 为参考来源。若两者冲突，除非 PRD 明确要求，否则当前项目的 Round 11/12 注册链路优先。

## 2. 文件级迁移规则

### 2.1 允许直接新增或改写的低耦合文件

- `src/autoteam/runtime_resources.py`
- `src/autoteam/playwright_lifecycle.py`
- `src/autoteam/playwright_probe.py`
- `src/autoteam/chatgpt_transport.py`
- `tests/unit/test_runtime_resources.py`
- `tests/unit/test_api_playwright_cleanup.py`
- `tests/unit/test_chatgpt_transport.py`
- `Dockerfile.fast`
- `docker-compose.yml`
- `docs/docker.md`
- `tests/integration/test_docker_guard.py`
- `pyproject.toml` 仅在新增依赖或测试配置确有必要时修改

### 2.2 只允许定点修改的高风险文件

- `src/autoteam/api.py`
  - 只允许增加资源快照字段、Playwright cleanup 调用、executor stop 钩子、health/status 辅助。
  - 不允许改变 `/api/tasks/fill` 的 `leave_workspace` 入参和任务调度语义。
- `src/autoteam/chatgpt_api.py`
  - 只允许增加 `_launch_browser()` 失败清理、`stop()` 幂等清理、Team API transport 的封装入口。
  - transport 默认值按母板为 `auto`；不允许改变 free 注册、Personal OAuth、验证码、workspace UI 的浏览器路径。
- `src/autoteam/config.py`
  - 只允许新增运行时资源阈值配置、HTTP transport 配置。
  - 新配置必须有安全默认值。

### 2.3 禁止覆盖的文件和区域

- `src/autoteam/manager.py` 中 `_run_post_register_oauth` 的 Personal/free 主体逻辑不得被旧实现覆盖。
- `src/autoteam/codex_auth.py` 的 Personal OAuth、workspace select、plan_type 校验不得被旧实现覆盖。
- `src/autoteam/oauth_workspace.py`、`src/autoteam/workspace_pool.py`、`src/autoteam/mail/register_dual_path.py` 不得为迁移旧功能而降级。
- 不得删除 Round 11/12 相关单测。

## 3. Docker 契约

`docker-compose.yml` 必须满足：

- `services.autoteam.init: true`
- `services.autoteam.shm_size` 不小于 `1gb`
- 设置 PID 边界，推荐 `pids_limit: 768`
- 设置内存边界，推荐 `mem_limit: "2g"`；如果同时使用 `deploy.resources`，必须避免与直接字段语义冲突。
- 保留当前 `build.args.GIT_SHA` 与 `build.args.BUILD_TIME`。
- 添加健康检查：
  - `test` 使用 `curl -fsS http://127.0.0.1:8787/api/version`
  - 设置合理 `interval`、`timeout`、`retries`、`start_period`
- 允许容器访问宿主代理的说明写入 `docs/docker.md`，但不默认强制 `network_mode: host`。

`Dockerfile.fast` 若新增，必须满足：

- 继承当前稳定镜像时不得绕开 entrypoint self-check。
- 支持 `GIT_SHA` / `BUILD_TIME` 或明确继承基础镜像中的版本指纹。
- 必须复制 `docker-entrypoint.sh` 并做 CRLF 处理。
- 必须在文档中标明用途：仅用于本地快速迭代，不替代首次完整构建。

## 4. runtime resources 契约

`runtime_resources.py` 必须满足：

- 不依赖 psutil。
- 所有 `/proc`、`/sys/fs/cgroup` 读取必须捕获 `OSError` / `ValueError` 并返回 `None` 或空统计。
- `collect_runtime_resource_snapshot()` 不抛异常，返回 dict 至少包含：
  - `rss_mb`
  - `cgroup_memory_mb`
  - `cgroup_memory_limit_mb`
  - `cgroup_memory_usage_ratio`
  - `pids_current`
  - `pids_max`
  - `browser_process_total`
  - `browser_process_live`
  - `browser_process_zombie`
- `log_runtime_resource_snapshot(logger, label=...)` 可执行 `gc.collect()`，但不得中断业务流程。
- 阈值从环境变量读取：
  - `AUTOTEAM_MEMORY_WARN_RATIO` 默认 `0.85`
  - `AUTOTEAM_ZOMBIE_WARN_THRESHOLD` 默认 `20`

## 5. Playwright lifecycle 契约

`playwright_lifecycle.py` 必须满足：

- `close_playwright_objects(page, context, browser, playwright, logger=None, label="playwright")`
- 关闭顺序固定：page -> context -> browser -> playwright.stop。
- 任一 close/stop 抛错时只记录 debug，不继续向外抛。
- `ChatGPTTeamAPI.stop()` 必须在释放后清空对象字段。
- `ChatGPTTeamAPI._launch_browser()` 在半初始化失败时必须调用 `stop()` 再重新抛原异常。

## 6. transport 契约

`curl_cffi` transport 按 `D:\Desktop\autoteam-1\AutoTeam` 作为默认 Team API 优化吸收：

- 默认值对齐母板为 `CHATGPT_API_TRANSPORT=auto`。
- 只允许用于无需真实浏览器上下文的 ChatGPT backend API 读写。
- 遇到 HTML challenge、403、401 token missing、结构异常时必须回退浏览器路径。
- 不允许用于 free 注册、Personal OAuth、验证码、workspace UI 选择；这些路径必须显式 `require_browser=True` 或自建 Playwright context。
- 依赖 `curl-cffi` 必须进入 `pyproject.toml` / `uv.lock`，保证 Docker 内默认 `auto` 后真实可用。

## 6.1 Playwright probe 契约

短生命周期探针用于后台稳定性加固：

- `python -m autoteam.playwright_probe team-member-count` 只输出 JSON。
- 父进程 helper 必须设置超时，超时后 best-effort kill 整个进程组。
- 探针失败时自动巡检只能视为 unknown，不得让主 API 线程崩溃。
- 该探针只能用于后台校验，不得替代 free 注册 / OAuth 的浏览器上下文。

## 7. free 注册保护契约

实施时必须维护以下红线：

- 不改变 `FillParams.leave_workspace` 默认值和含义。
- 不改变 `command = "fill-personal" if params.leave_workspace else "fill"`。
- 不改变 `_run_post_register_oauth(..., leave_workspace=True)` 的 5 次 retry 和 `plan_type=free` 接受条件。
- 不改变 plan drift 记录、`record_failure` 分类、Personal quota probe 和状态落盘。
- 不改变 `auths/*-free*.json` 与 Team auth 文件互斥清理策略。

任何触碰上述区域，必须先更新本 PRD/spec，并单独列出兼容性证明。

## 8. 测试契约

每个实施阶段必须带测试：

- Docker 配置：扩展 `tests/integration/test_docker_guard.py`
- 资源探针：新增 `tests/unit/test_runtime_resources.py`
- Playwright 清理：新增或移植 `tests/unit/test_api_playwright_cleanup.py`
- HTTP transport：新增 `tests/unit/test_chatgpt_transport.py`
- free 注册保护：运行现有 Round 11/12 相关单测
- 静态质量：ruff F401/F811/F821 继续通过

## 9. 回滚契约

- Docker/compose 回滚必须不影响本地非 Docker 启动。
- runtime resources 集成失败时，可以保留模块但禁用调用点。
- Playwright cleanup 若导致路径差异，优先回滚调用点而不是删除工具模块。
- transport 若引入，必须可通过环境变量一键回退到 Playwright 路径。
