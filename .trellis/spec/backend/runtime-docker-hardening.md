# Runtime and Docker Hardening

## Scenario: Docker-Bounded Playwright Runtime

### 1. Scope / Trigger

- Trigger: any change that touches Docker runtime settings, Playwright lifecycle management, runtime resource probes, background browser probes, or `CHATGPT_API_TRANSPORT`.
- Goal: keep browser automation bounded and observable without changing the free-account registration main flow.
- Applies to:
  - `docker-compose.yml`
  - `Dockerfile.fast`
  - `docker-entrypoint.sh`
  - `src/autoteam/runtime_resources.py`
  - `src/autoteam/playwright_lifecycle.py`
  - `src/autoteam/playwright_probe.py`
  - `src/autoteam/chatgpt_transport.py`
  - `src/autoteam/chatgpt_api.py`
  - `src/autoteam/api.py`

### 2. Signatures

- `collect_runtime_resource_snapshot() -> dict[str, Any]`
- `log_runtime_resource_snapshot(logger: Any, *, label: str = "runtime") -> dict[str, Any]`
- `close_playwright_objects(page=None, context=None, browser=None, playwright=None, *, logger=None, label="playwright") -> None`
- `python -m autoteam.playwright_probe team-member-count`
- `ChatGPTTeamAPI.start_with_session(session_token, account_id, workspace_name="", require_browser=False)`
- `ChatGPTTeamAPI.stop() -> None`
- `build_chatgpt_transport(session_token: str, account_id: str = "", oai_device_id: str = "")`

### 3. Contracts

- Docker Compose must keep:
  - `services.autoteam.init: true`
  - `services.autoteam.shm_size` at least `1gb`
  - `services.autoteam.mem_limit` and `memswap_limit`
  - `services.autoteam.pids_limit`
  - healthcheck using `curl -fsS http://127.0.0.1:8787/api/version`
  - build args `GIT_SHA` and `BUILD_TIME`
- Runtime env keys:
  - `AUTOTEAM_MEMORY_WARN_RATIO`, default `0.85`
  - `AUTOTEAM_ZOMBIE_WARN_THRESHOLD`, default `20` in code; compose may use a stricter value
  - `CHATGPT_API_TRANSPORT`, default `auto` to match `D:\Desktop\autoteam-1\AutoTeam`
  - `CHATGPT_API_HTTP_TIMEOUT`, default `60`
  - `CHATGPT_API_IMPERSONATE`, default `chrome136`
- `CHATGPT_API_TRANSPORT=curl_cffi` or `auto` is allowed only for backend API reads. It must not be used for free registration, Personal OAuth, captcha/challenge flows, or workspace UI selection; those call sites must force `require_browser=True` or launch their own Playwright context.
- `/api/status` may include `runtime_resources`, but resource collection must never block or fail the status response.
- Background Team member count probes must run in a killable subprocess and return unknown (`-1`) on timeout/failure.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| `/proc` or cgroup files missing | Resource snapshot fields become `None`; no exception escapes |
| `ps` unavailable or times out | Browser process counts become zero; no exception escapes |
| cgroup memory ratio >= threshold | Log a warning and run best-effort `gc.collect()` |
| browser zombie count >= threshold | Log a warning that init/reaper should be enabled |
| Playwright page/context/browser/stop raises during cleanup | Log debug when logger exists; continue cleanup and do not re-raise |
| `_launch_browser()` partially initializes then fails | Call `stop()` and re-raise the original exception |
| probe subprocess timeout | Kill the process group/tree and treat count as unknown |
| `curl_cffi` missing or transport init fails | Return `None`; continue with Playwright |
| transport returns HTML/challenge/401 token missing | Fall back to Playwright API fetch |
| `ChatGPTTeamAPI.start()` succeeds via HTTP transport only | Cleanup and reuse checks must use `is_started()` / `_chatgpt_session_ready()`, not `browser` alone |

### 5. Good/Base/Bad Cases

- Good: `docker compose config` shows init, shm, memory, PID, healthcheck, and build args; `docker run --rm autoteam:fast-0515 status` passes self-check.
- Base: local non-Docker startup still uses the same Python modules and defaults to `auto`, while browser-dependent flows force Playwright explicitly.
- Bad: letting `auto` leak into OAuth/UI flows, swallowing Playwright init failures without cleanup, or running periodic Team count checks in the long-lived API process.
- Bad: guarding `stop()` with `if api.browser` after `auto` transport is enabled; HTTP-only sessions would leak.

### 6. Tests Required

- Docker contract: `tests/integration/test_docker_guard.py`
- Resource probe: `tests/unit/test_runtime_resources.py`
- Playwright cleanup and subprocess probe behavior: `tests/unit/test_api_playwright_cleanup.py`
- HTTP transport auto/fallback: `tests/unit/test_chatgpt_transport.py`
- Status endpoint integration: `tests/unit/test_api_status.py`
- Free registration regression:
  - `tests/unit/test_round11_personal_oauth_retry.py`
  - `tests/unit/test_round11_session_token_injection.py`
  - `tests/unit/test_round12_s4_register_dual_path.py`
  - `tests/unit/test_manager_fill.py`

### 7. Wrong vs Correct

#### Wrong

```python
def start_with_session(self, session_token, account_id):
    self.http_transport = build_chatgpt_transport(...)
    # This silently changes all callers to HTTP-first behavior, including flows
    # that require a real browser context.
```

#### Correct

```python
def start_with_session(self, session_token, account_id, workspace_name="", require_browser=False):
    if not require_browser and self._start_transport_session(session_token):
        ...
    self._start_browser_session(session_token)
```

Use `require_browser=True` for any path that depends on a real browser context. Keep the default environment value aligned with `autoteam-1` as `auto`, and keep `curl_cffi` isolated to backend API reads/writes plus browser fallback.
