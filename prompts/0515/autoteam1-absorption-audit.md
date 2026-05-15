# 0515 吸收完成审计：autoteam-1 突出设计到当前项目

## 目标重述

在 `D:\Desktop\AutoTeam` 中，以 `D:\Desktop\autoteam-1` 为参考来源，完整吸收其中适合当前项目的优秀设计：加固、稳定、防止资源/进程溢出、Docker 部署和低风险性能增强；同时不破坏 free 帐号注册主流程。

完成标准不是“文件相似”，而是每个源端突出设计都必须落到以下三种结论之一：

- 已吸收：当前项目已有对应 artifact、测试或运行证据。
- 已被当前更强实现替代：源端设计价值存在，但当前项目已有更新、更完整且经过测试的实现。
- 明确不吸收：源端内容不是设计能力、会降级当前 Round 11/12 主流程，或属于独立工具边界。

## prompt-to-artifact checklist

| 要求 / 源端设计 | 当前结论 | 证据 |
| --- | --- | --- |
| 先写 PRD/spec/计划到 `prompts/0515` | 已完成 | `prd.md`、`spec.md`、`implementation-plan.md`、`research-docker-playwright-hardening.md`、本审计文件 |
| Docker compose 资源边界：`init`、`shm_size`、内存、PID、healthcheck | 已吸收 | `docker-compose.yml`；`docker compose config` 通过并解析出 `init: true`、`mem_limit=2g`、`pids_limit=768`、`shm_size=1gb` |
| `Dockerfile.fast` 快速增量镜像 | 已吸收 | `Dockerfile.fast`；`docker build -f Dockerfile.fast ... -t autoteam:fast-0515 .` 通过 |
| entrypoint self-check 保持真实启动守卫 | 已增强 | `docker-entrypoint.sh` self-check 扩展到 `15 critical symbols`；`docker run --rm autoteam:fast-0515 status` 通过 |
| cgroup memory/pids/browser zombie 资源探针 | 已吸收 | `src/autoteam/runtime_resources.py`；`tests/unit/test_runtime_resources.py` |
| 自动巡检资源告警，不阻塞业务 | 已吸收 | `api.py:_auto_check_loop()` 调用 `log_runtime_resource_snapshot(logger, label="auto-check")` |
| `/api/status` 暴露资源快照 | 已吸收 | `api.py:get_status()` 返回 `runtime_resources`；`tests/unit/test_api_status.py` 通过 |
| Playwright 统一 close 顺序和幂等 cleanup | 已吸收 | `src/autoteam/playwright_lifecycle.py`；`ChatGPTTeamAPI.stop()`；`tests/unit/test_api_playwright_cleanup.py` |
| `_launch_browser()` 半初始化失败清理 | 已吸收 | `ChatGPTTeamAPI._launch_browser()` 失败时 `self.stop()`；目标测试覆盖 |
| API 失败路径释放 `ChatGPTTeamAPI` | 已吸收 | `post_admin_login_start()`、`get_team_members()`、kick/remove 路径的 `try/finally`；目标测试覆盖 |
| killable Playwright probe 防卡死/溢出 | 已吸收 | `src/autoteam/playwright_probe.py`；`api.py:_run_playwright_probe()` 超时 kill group；目标测试覆盖 |
| `curl_cffi` Team API transport | 已按母板默认吸收 | `src/autoteam/chatgpt_transport.py`、`pyproject.toml`、`uv.lock`、`.env.example`；默认 `CHATGPT_API_TRANSPORT=auto`，OAuth/UI 路径用 `require_browser=True` 隔离 |
| transport 回退 Playwright | 已吸收 | `_transport_response_requires_browser_fallback()`、`_direct_api_fetch()`；`tests/unit/test_chatgpt_transport.py` |
| free 注册主流程不破坏 | 已验证 | Round 11/12 free 注册保护回归 `58 passed` |
| Round 11 header sanitize 不被 transport 改坏 | 已验证 | `tests/unit/test_round11_api_fetch_header_sanitize.py` 通过 |
| Cloudflare Temp Email / mail provider 设计 | 当前更强实现替代 | 当前 `src/autoteam/mail/` 已有 `MailProvider` 抽象、`cf_temp_email`、`maillab`、fallback、probe 和更多单测；不回贴源端旧 `mail_provider.py` / `cloudflare_temp_email.py` |
| signup profile / OAuth failure recovery | 当前更强实现替代 | 当前 `manager.py` / `codex_auth.py` 已有 Round 11/12 Personal OAuth retry、plan drift、workspace pool、register dual path；free 注册回归覆盖 |
| CPA 同步保护 | 当前更强实现替代 | 当前 `cpa_sync.py` 已包含 active+personal 同步、token 删除守卫、workspace summary，比源端旧 active-only/disabled 逻辑更贴合当前 personal/free 路径 |
| Sub2API / sync target 分发 | 明确不吸收进本任务代码 | 属于新外部产品集成，不是本次加固/稳定/Docker主线；源端实现依赖旧 config/UI/manager 路径，直接引入会扩大 blast radius。已在审计中记录为后续独立任务候选，不影响当前目标的“稳定性突出设计”吸收 |
| 旧 `manager.py` / `codex_auth.py` / `invite.py` 覆盖 | 明确拒绝 | PRD/spec 红线；当前 Round 11/12 版本优先，回归通过 |
| `autoteam-1` 中 data/auths/screenshots/dist/pycache | 明确不吸收 | 运行产物和构建产物，不是设计能力 |
| `codex-watchdog` 独立工具经验 | 已吸收为原则，不迁入 app | 保守自动化、避免误扰动活跃会话的原则写入 PRD；该工具不属于当前 AutoTeam app 代码范围 |

## 实际验证

已通过：

- `python -m pytest tests/unit/test_chatgpt_transport.py tests/unit/test_round11_api_fetch_header_sanitize.py tests/unit/test_api_playwright_cleanup.py tests/integration/test_docker_guard.py tests/unit/test_runtime_resources.py` -> `32 passed, 1 warning`
- `python -m pytest tests/unit/test_round11_personal_oauth_retry.py tests/unit/test_round11_session_token_injection.py tests/unit/test_round12_s4_register_dual_path.py tests/unit/test_manager_fill.py` -> `58 passed`
- `python -m pytest tests/unit/test_api_status.py tests/unit/test_playwright_guard.py tests/static/test_playwright_hygiene.py tests/unit/test_round12_rotate_sse_stream.py` -> `15 passed, 1 warning`
- `python -m ruff check src` -> `All checks passed`
- 变更文件 targeted ruff -> `All checks passed`
- `docker compose config` -> 通过
- `docker build -f Dockerfile.fast --build-arg GIT_SHA=0515-hardening --build-arg BUILD_TIME=2026-05-15T00:00:00Z -t autoteam:fast-0515 .` -> 通过
- `docker run --rm autoteam:fast-0515 status` -> self-check `OK: 15 critical symbols imported`，self-check passed

已知非本轮阻塞：

- `python -m ruff check src tests` 仍失败于既有 `tests/unit/test_round12_wireup.py` 的 import-order、unused variable/import、`getattr` 常量属性问题。`src` 和本轮变更文件均已通过 ruff，本任务不顺手修改该无关 WIP。

## 结论

本轮与“加固、稳定、防止溢出、Docker 部署、默认不破坏 free 注册”直接相关的 `autoteam-1` 突出设计已全部吸收或被当前更强实现替代。剩余未吸收项不是本任务目标内的稳定性设计，或会降级当前 Round 11/12 主流程，已明确拒绝或记录为独立后续候选。
