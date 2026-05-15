# 0515 PRD：吸收 autoteam-1 加固能力并保护 free 注册主流程

## 背景

用户要求以 `D:\Desktop\autoteam-1` 为基础，将其中“扬长避短”的能力应用到当前 `D:\Desktop\AutoTeam`：重点是加固、稳定、防止溢出、Docker 部署，同时明确要求不要破坏 free 帐号注册的主流程。本文件是实施前的权威需求文档；在本文件和 `spec.md`、`implementation-plan.md` 决定之前，不修改业务代码。

## 当前事实

- 当前项目路径：`D:\Desktop\AutoTeam`，Trellis task：`.trellis/tasks/05-15-autoteam1-hardening-docker-apply`。
- 当前项目已有 Docker 基础：`Dockerfile`、`docker-compose.yml`、`docker-entrypoint.sh`、`tests/integration/test_docker_guard.py`。
- 当前项目已有 free 注册主流程：`POST /api/tasks/fill { leave_workspace: true }` -> `cmd_fill(..., leave_workspace=True)` -> `_run_post_register_oauth(..., leave_workspace=True)` -> Personal OAuth -> `plan_type=free` 校验和 plan drift 记录。
- 当前项目已有比 `autoteam-1` 更新的 Round 11/12 保护：`master_health`、`oauth_workspace`、`workspace_pool`、`quota_predictor`、`mail/register_dual_path.py`、多 provider 回退、Personal OAuth 重试和大量单测。
- `D:\Desktop\autoteam-1\AutoTeam` 不是应整包覆盖的目标；它更适合作为局部能力来源。
- 记忆中 `D:\Desktop\autoteam-1\codex-watchdog` 的经验提示：自动化加固必须默认保守，不得误打扰活跃 Codex/注册会话；dry-run 和“不注入/不破坏”证据优先。

## 目标

在不破坏 free 帐号注册主流程的前提下，将 `autoteam-1` 中对当前项目有正向收益的稳定性和部署能力按阶段吸收进来，使当前项目具备更可靠的 Docker 运行边界、资源观测、Playwright 生命周期清理和溢出防护。

## 非目标

- 不重写 free 注册流程。
- 不直接用 `autoteam-1` 的旧 `manager.py` / `codex_auth.py` / `invite.py` 覆盖当前实现。
- 不允许 HTTP transport 改变 OAuth、注册、workspace 选择结果；默认 transport 对齐母板为 `auto`，但浏览器依赖路径必须显式强制 Playwright 且可回退。
- 不在未验证前宣称 Docker 镜像可生产部署。
- 不清理与本任务无关的 dirty worktree 文件。

## 需求

### R1. 迁移前对照矩阵

必须先形成“吸收 / 改写 / 拒绝”的矩阵，逐项说明来源文件、目标文件、收益、风险和验证方式。

首批候选：

- 吸收：`runtime_resources.py` -> 当前项目新增轻量资源探针。
- 吸收并适配：`playwright_lifecycle.py` -> 当前 `ChatGPTTeamAPI`、API 登录/探测路径的 best-effort cleanup。
- 吸收并增强：`Dockerfile.fast` -> 当前项目增量构建文件，但保留 build args、OCI label、entrypoint self-check。
- 吸收：compose `init: true`、`shm_size`、内存/PID 边界和资源告警环境变量。
- 按母板吸收：`chatgpt_transport.py` / `curl_cffi`，默认 `auto` 仅用于 Team backend API，不能进入 free 注册 / Personal OAuth / workspace UI 选择路径。
- 吸收：`playwright_probe.py`，将后台 Team 人数探测隔离到可超时杀进程组的短生命周期子进程。
- 拒绝：旧 free/direct register 主流程覆盖当前 Round 11/12 实现。

### R2. Docker 部署加固

`docker-compose.yml` 应具备以下能力：

- 启用 `init: true`。
- 为 Chromium/Playwright 设置共享内存边界，如 `shm_size: "1gb"`。
- 设置内存和 PID 上限，防止浏览器或任务异常增长拖垮宿主机。
- 设置 `AUTOTEAM_MEMORY_WARN_RATIO`、`AUTOTEAM_ZOMBIE_WARN_THRESHOLD` 等运行时资源告警变量。
- 增加 `healthcheck`，优先检查 `http://127.0.0.1:8787/api/version`。
- 保留现有 build args：`GIT_SHA`、`BUILD_TIME`。

### R3. 运行时资源与溢出防护

新增或吸收轻量资源探针：

- 读取 `/proc/self/status`、cgroup memory、cgroup pids。
- 统计 Chromium/Playwright live/zombie 进程。
- 内存使用比例超过阈值时记录 warning 并执行 `gc.collect()`。
- browser zombie 超阈值时记录 warning，提示 `init` / reaper 配置。
- 资源快照应被 API status 或自动巡检日志可见，但不得阻塞主流程。

### R4. Playwright 生命周期清理

所有新接入清理逻辑必须是 best-effort：

- 关闭顺序固定为 page -> context -> browser -> playwright。
- `_launch_browser()` 失败时必须调用 `stop()` 清理半初始化对象。
- `stop()` 必须幂等，可重复调用。
- API 登录、Team 成员探测、manual account、main codex 等路径若创建 `ChatGPTTeamAPI` 后失败，必须释放对象。
- 不能把 Playwright 清理异常向上传播为业务失败，除非原始业务操作已经失败。

### R5. free 注册主流程保护

以下行为必须保持兼容：

- Web/API 请求 `POST /api/tasks/fill` 的 `leave_workspace=True` 语义不变。
- `cmd_fill(..., leave_workspace=True)` 仍走当前注册、kick、Personal OAuth、plan drift 记录路径。
- `_run_post_register_oauth(..., leave_workspace=True)` 的 5 次 Personal OAuth retry、`plan_type=free` 校验、失败记录和状态落盘不被削弱。
- 不改 `STATUS_PERSONAL` / `STATUS_STANDBY` / `STATUS_AUTH_INVALID` 等状态语义。
- 不改 `register_failures.json` 的关键分类语义。
- 不让 Docker/runtime/transport 默认改变注册浏览器上下文、workspace select 或 OAuth callback 逻辑。

### R6. 验证证据

实施后至少需要以下验证：

- `python -m pytest tests/integration/test_docker_guard.py`
- `python -m pytest tests/unit/test_runtime_resources.py`（新增后）
- `python -m pytest tests/unit/test_api_playwright_cleanup.py`（新增或适配后）
- free 注册保护回归：
  - `python -m pytest tests/unit/test_round11_personal_oauth_retry.py`
  - `python -m pytest tests/unit/test_round11_session_token_injection.py`
  - `python -m pytest tests/unit/test_round12_s4_register_dual_path.py`
  - `python -m pytest tests/unit/test_manager_fill.py`
- `ruff check` 或 `python -m ruff check src tests`（以项目当前工具为准）
- `docker compose config`
- Docker 可用时再执行 `docker compose build` 或最小镜像构建验证。

## 验收标准

- `prompts/0515/prd.md`、`prompts/0515/spec.md`、`prompts/0515/implementation-plan.md` 完整存在并可审阅。
- 代码实施前有明确的迁移矩阵和阶段计划。
- 实施完成后，Docker 配置具备 init、shared memory、资源边界、healthcheck 和 build args。
- 资源探针在非 Linux / 无 cgroup 环境中优雅降级，不抛异常。
- Playwright lifecycle 清理新增测试通过。
- free 注册相关回归测试通过，且没有默认启用影响 free 注册主流程的新 transport。
- 变更说明中明确列出未吸收项和原因。

## 决策

采用“安全分层吸收”方案：

1. 先吸收 Docker/运行时资源/Playwright 清理这些低耦合能力。
2. `curl_cffi` transport 按母板默认 `auto` 吸收；失败、HTML challenge 或鉴权异常必须回退 Playwright，OAuth/UI/free 注册路径必须显式强制浏览器上下文。
3. free 注册链路只加保护性回归测试，不主动重构。

## 外部调研

详见 `research-docker-playwright-hardening.md`。
