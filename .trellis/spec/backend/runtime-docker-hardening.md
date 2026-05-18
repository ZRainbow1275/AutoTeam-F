# Runtime and Docker Hardening

## Scenario: Docker-Bounded Playwright Runtime

### 1. Scope / Trigger

- Trigger: any change that touches Docker runtime settings, Playwright lifecycle management, runtime resource probes, background browser probes, `CHATGPT_API_TRANSPORT`, or per-account IPv6 proxy isolation.
- Goal: keep browser automation bounded and observable without changing the free-account registration main flow.
- Applies to:
  - `docker-compose.yml`
  - `Dockerfile.fast`
  - `docker-entrypoint.sh`
  - `src/autoteam/runtime_resources.py`
  - `src/autoteam/playwright_lifecycle.py`
  - `src/autoteam/playwright_probe.py`
  - `src/autoteam/ipv6_pool.py`
  - `src/autoteam/ipv6_proxy.py`
  - `src/autoteam/chatgpt_transport.py`
  - `src/autoteam/chatgpt_api.py`
  - `src/autoteam/cpa_sync.py`
  - `src/autoteam/api.py`

### 2. Signatures

- `collect_runtime_resource_snapshot() -> dict[str, Any]`
- `log_runtime_resource_snapshot(logger: Any, *, label: str = "runtime") -> dict[str, Any]`
- `close_playwright_objects(page=None, context=None, browser=None, playwright=None, *, logger=None, label="playwright") -> None`
- `python -m autoteam.playwright_probe team-member-count`
- `ChatGPTTeamAPI.start_with_session(session_token, account_id, workspace_name="", require_browser=False)`
- `ChatGPTTeamAPI.stop() -> None`
- `build_chatgpt_transport(session_token: str, account_id: str = "", oai_device_id: str = "", proxy_url: str | None = None)`
- `get_chatgpt_http_proxy_url(proxy_url: str | None = None) -> str`
- `get_cliproxy_health() -> dict[str, Any]`
- `cmd_rotate(target_seats=3, force_auth_repair=False, background_post_sync=False)`
- `IPv6Pool.start(active_emails: Iterable[str] | None = None) -> None`
- `IPv6Pool.ensure_proxy_for_account(email: str) -> tuple[str, str]`
- `IPv6Pool.release_proxy_for_account(email: str) -> None`
- `IPv6Pool.status() -> dict[str, Any]`

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
  - `AUTOTEAM_IPV6_POOL_ENABLED`, default `false`
  - `AUTOTEAM_IPV6_POOL_REQUIRED`, default `false`
  - `IPV6_PREFIX` and `IPV6_IFACE`, required only when the IPv6 pool is enabled
  - `IPV6_PROXY_*` keys for local listen, public auth URL, allowed IPs, port range, TTL, and pool persistence
- `CHATGPT_API_TRANSPORT=curl_cffi` or `auto` is allowed only for backend API reads. It must not be used for free registration, Personal OAuth, captcha/challenge flows, or workspace UI selection; those call sites must force `require_browser=True` or launch their own Playwright context.
- `/api/status` may include `runtime_resources`, but resource collection must never block or fail the status response.
- Background Team member count probes must run in a killable subprocess and return unknown (`-1`) on timeout/failure.
- `ChatGPTTeamAPI.start_with_session()` must call `stop()` if the browser fallback path fails after partial initialization, not only when `_launch_browser()` itself fails.
- `ChatGPTTeamAPI.start_with_session()` must also call `stop()` when HTTP transport startup succeeds but later workspace detection or admin-state persistence fails.
- Direct HTTP API fetches must close and clear the bad `http_transport` before browser fallback when the transport raises, returns HTML/challenge, or fails again after a 401-triggered token refresh retry.
- Registration and OAuth Playwright call sites must use `close_playwright_objects()` rather than raw `browser.close()`, `context.close()`, or `page.close()`. Direct registration paths need a `try/finally` guard so unexpected page/navigation errors still release page, context, and browser.
- `SessionCodexAuthFlow.start()` must stop its `ChatGPTTeamAPI` instance if page creation, cookie injection, navigation, or the first `_advance()` fails after `start_with_session(require_browser=True)`.
- API-driven `rotate`, `auto-fill`, and `auto-rotate` must not block task completion on CPA/CLIProxyAPI remote sync. Use `background_post_sync=True` so `cmd_rotate()` schedules final sync after the Playwright-bound operation releases task state.
- IPv6 proxy isolation is opt-in. Disabled-by-default installs must not create local proxy processes, mutate network state, or require IPv6 kernel configuration.
- When `AUTOTEAM_IPV6_POOL_REQUIRED=true`, allocation/preflight failure is a hard business/runtime error. Do not silently fall back to direct network access.
- Account-scoped browser and HTTP transport paths must propagate the allocated local proxy URL into Playwright launch options and `curl_cffi` transport construction. Persisted auth bundles should store the public/auth proxy URL only when one was allocated.
- Temporary registration proxies must be released when registration, duplicate-email retry, phone/add-phone terminal failure, or post-registration OAuth fails before the account becomes usable.
- API startup may pre-warm the IPv6 pool only for non-main, non-disabled active accounts. Shutdown must stop pool proxies after Playwright executor cleanup.
- `/api/status` may include `ipv6_pool`, but IPv6 status collection must never block or fail the status response. Return a diagnostic `ipv6_pool.error` instead of raising a 500.
- `/api/status` may include `cliproxy` and `rotation_validation`. These fields are additive and must not remove or rename existing status fields.
- `cliproxy` health must stay read-only. It may read CLIProxyAPI management metadata (`/v0/management/auth-files`) and provider availability counts, but must never call sync/upload/delete/refresh endpoints.
- `rotation_validation` must distinguish `ok`, `degraded`, and hard `failed` results. Hard post-task validation failure may convert a superficially completed mutation task into `failed`; degraded results keep operation success visible and expose cooldown/follow-up metadata.
- `sync_to_cpa()` must refresh `proxy_url` in auth JSON for active/personal, non-disabled local accounts before upload. In required mode refresh failure must fail the sync; in non-required mode it may warn and upload the existing file.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| `/proc` or cgroup files missing | Resource snapshot fields become `None`; no exception escapes |
| `ps` unavailable or times out | Browser process counts become zero; no exception escapes |
| cgroup memory ratio >= threshold | Log a warning and run best-effort `gc.collect()` |
| browser zombie count >= threshold | Log a warning that init/reaper should be enabled |
| Playwright page/context/browser/stop raises during cleanup | Log debug when logger exists; continue cleanup and do not re-raise |
| `_launch_browser()` partially initializes then fails | Call `stop()` and re-raise the original exception |
| `start_with_session(..., require_browser=True)` or browser fallback starts Playwright then fails during navigation/token/workspace setup | Call `stop()` and re-raise the original exception |
| probe subprocess timeout | Kill the process group/tree and treat count as unknown |
| `curl_cffi` missing or transport init fails | Return `None`; continue with Playwright |
| transport returns HTML/challenge/401 token missing | Fall back to Playwright API fetch |
| `ChatGPTTeamAPI.start()` succeeds via HTTP transport only | Cleanup and reuse checks must use `is_started()` / `_chatgpt_session_ready()`, not `browser` alone |
| `http_transport.request()` raises before or after token refresh retry | Close/clear the transport, then fall back to Playwright using the saved `session_token` |
| invite/direct registration or Codex OAuth opens a Playwright page then raises before a normal return | Cleanup must still close page, context, and browser in dependency order |
| `SessionCodexAuthFlow.start()` fails after creating a `ChatGPTTeamAPI` | Call `stop()` and clear `chatgpt` / `page` fields |
| `collect_runtime_resource_snapshot()` unexpectedly raises inside `/api/status` | Return a status response with a diagnostic `runtime_resources.error`; do not raise a 500 |
| CLIProxyAPI health config is missing or unreachable | Return `cliproxy.ok=false`, `safe_read_only=true`, and a reason; do not raise a 500 |
| CLIProxyAPI auth-file payload is malformed | Return provider/management diagnostic fields; do not treat it as an empty healthy provider set |
| post-task remote sync fails after API-driven rotate | Log a warning, keep the local rotation result, and leave task completion unblocked |
| post-task runtime validation is degraded | Keep the task completed, record reasons and cooldown/follow-up metadata |
| post-task runtime validation is a hard failure | Convert the task to `failed` with a business reason |
| IPv6 pool is disabled | Do not start proxies; status should report disabled/no allocations |
| IPv6 allocation fails while required mode is true | Raise a clear error and do not continue direct |
| IPv6 allocation fails while required mode is false | Log a warning and continue only through the existing direct-path fallback |
| IPv6 status collection raises inside `/api/status` | Return a status response with a diagnostic `ipv6_pool.error`; do not raise a 500 |
| CPA proxy refresh fails while required mode is true | Raise and stop the sync rather than uploading stale direct auth |

### 5. Good/Base/Bad Cases

- Good: `docker compose config` shows init, shm, memory, PID, healthcheck, and build args; `docker run --rm autoteam:fast-0515 status` passes self-check.
- Base: local non-Docker startup still uses the same Python modules and defaults to `auto`, while browser-dependent flows force Playwright explicitly.
- Bad: letting `auto` leak into OAuth/UI flows, swallowing Playwright init failures without cleanup, or running periodic Team count checks in the long-lived API process.
- Bad: guarding `stop()` with `if api.browser` after `auto` transport is enabled; HTTP-only sessions would leak.
- Bad: keeping a failed `http_transport` after browser fallback; the next `_api_fetch()` would retry the same known-bad transport instead of using the live browser session.
- Bad: replacing direct registration's final `browser.close()` but leaving unexpected navigation errors outside a `finally` cleanup guard.
- Bad: adding a proxy URL to saved auth bundles without actually launching browser/API requests through the same account proxy.
- Bad: treating required-mode IPv6 failures as warnings; this hides pool exhaustion and defeats isolation.

### 6. Tests Required

- Docker contract: `tests/integration/test_docker_guard.py`
- Resource probe: `tests/unit/test_runtime_resources.py`
- Playwright cleanup and subprocess probe behavior: `tests/unit/test_api_playwright_cleanup.py`
- API status resource-snapshot boundary: `tests/unit/test_api_status.py`
- HTTP transport auto/fallback: `tests/unit/test_chatgpt_transport.py`
- Direct registration and OAuth cleanup source/exception guards:
  - `tests/unit/test_api_playwright_cleanup.py`
  - `tests/unit/test_round11_session_token_injection.py`
- Status endpoint integration: `tests/unit/test_api_status.py`
- CLIProxyAPI read-only health: `tests/unit/test_cliproxy_health.py`
- Rotate deferred post-sync and validation plumbing: `tests/unit/test_manager_rotate.py` and `tests/unit/test_api_status.py`
- IPv6 pool/proxy persistence and strict required-mode behavior: `tests/unit/test_ipv6_pool.py`
- IPv6 status and status-failure boundary: `tests/unit/test_api_status.py`
- CPA auth proxy refresh before upload: `tests/unit/test_cpa_sync.py`
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
