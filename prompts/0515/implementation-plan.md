# 0515 实施计划：先加固外围，再保护主流程

## 当前阶段

状态：已实施并进入收尾审计。Phase 1-4 先按保守方式落地，第二轮已按母板把 Team API transport 默认值校正为 `auto`，并补齐 OAuth/UI 浏览器隔离。

## TODO

- [x] 创建 Trellis planning task：`.trellis/tasks/05-15-autoteam1-hardening-docker-apply`
- [x] 建立 `prompts/0515` 文档目录
- [x] 写入 PRD、SPEC、实施计划、调研记录
- [x] 用户确认实施范围
- [x] Phase 1：Docker/compose 加固
- [x] Phase 2：runtime resources 与 Playwright lifecycle
- [x] Phase 2.5：killable Playwright probe 吸收
- [x] Phase 3：free 注册保护回归
- [x] Phase 4：Team API transport 评估与母板对齐实施
- [x] Phase 5：完整验证与收尾记录
- [x] Phase 6：完成审计，确认 autoteam-1 优秀设计已吸收或被当前更强实现替代

## 推荐实施顺序

### Phase 1：Docker/compose 加固

变更范围：

- `docker-compose.yml`
- `Dockerfile.fast`
- `docs/docker.md`
- `tests/integration/test_docker_guard.py`

工作内容：

- 给 compose 增加 `init: true`、`shm_size: "1gb"`、`mem_limit`、`memswap_limit`、`pids_limit`。
- 增加资源阈值环境变量。
- 增加 `/api/version` healthcheck。
- 新增 `Dockerfile.fast`，但保留当前 Dockerfile 的版本指纹和 entrypoint self-check 约束。
- 扩展 Docker guard 测试覆盖上述静态契约。

验证：

- `python -m pytest tests/integration/test_docker_guard.py`
- `docker compose config`

### Phase 2：runtime resources 与 Playwright lifecycle

变更范围：

- `src/autoteam/runtime_resources.py`
- `src/autoteam/playwright_lifecycle.py`
- `src/autoteam/chatgpt_api.py`
- `src/autoteam/api.py`
- `tests/unit/test_runtime_resources.py`
- `tests/unit/test_api_playwright_cleanup.py`

工作内容：

- 新增资源探针，支持 cgroup memory/pids 和 browser zombie 统计。
- 在低风险 API status 或自动巡检日志中接入资源快照。
- 新增统一 Playwright close helper。
- 修改 `ChatGPTTeamAPI._launch_browser()` 和 `stop()` 以保证失败清理和幂等释放。
- 对 API 中创建 `ChatGPTTeamAPI` 的失败路径补清理。

验证：

- `python -m pytest tests/unit/test_runtime_resources.py`
- `python -m pytest tests/unit/test_api_playwright_cleanup.py`
- `python -m pytest tests/unit/test_playwright_guard.py tests/static/test_playwright_hygiene.py`

### Phase 3：free 注册保护回归

变更范围：

- 原则上不改 free 注册代码，只跑并补充保护测试。

验证：

- `python -m pytest tests/unit/test_round11_personal_oauth_retry.py`
- `python -m pytest tests/unit/test_round11_session_token_injection.py`
- `python -m pytest tests/unit/test_round12_s4_register_dual_path.py`
- `python -m pytest tests/unit/test_manager_fill.py`
- 如 Phase 2 触碰 API task 调度，再补跑 `tests/unit/test_round12_rotate_sse_stream.py`

### Phase 4：Team API transport 评估

已在 Phase 1-3 稳定后实施，并在第二轮对照母板后改为默认 `auto`：

- 新增 `curl-cffi` 依赖和 `chatgpt_transport.py`
- 默认对齐 `autoteam-1` 为 `auto`
- 仅用于非注册、非 OAuth、非 workspace UI 的 backend API fetch
- HTML challenge / token miss / 403 / 401 自动回退 Playwright
- `SessionCodexAuthFlow` 等 OAuth/UI 路径显式 `require_browser=True`

验证：

- 新增 transport 单测
- 确认 free 注册保护测试仍通过

### Phase 6：吸收审计

工作内容：

- 建立 `autoteam-1` 设计点到当前 artifact 的 checklist。
- 对每个显式需求和源端突出设计标记：已吸收 / 当前更强实现替代 / 明确拒绝。
- 用文件、测试、Docker 运行结果和 lint 结果作为证据，不能只用意图或清单。

## 推荐选项

实际执行结果：Phase 1-3 先完成，之后补做 Phase 4；第二轮 codex review 后以 `D:\Desktop\autoteam-1\AutoTeam` 为规范，将默认值校正为 `auto`。

- Docker 和资源清理收益高，风险低，已完成。
- free 注册主流程未主动改动，已通过回归保护。
- `curl_cffi` transport 已按母板默认 `auto` 落地，并通过 `require_browser=True` 与生命周期清理保护真实浏览器路径。
