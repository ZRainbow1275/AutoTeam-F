# Completion Audit - 2026-05-15

## Objective

Continue until the useful and outstanding designs from `D:\Desktop\autoteam-1` are fully absorbed into `D:\Desktop\AutoTeam`, especially hardening, stability, overflow/resource containment, and Docker deployment, while confirming that the free-account registration main flow is not broken.

## Success Criteria

1. Planning artifacts exist under `prompts/0515` before implementation.
2. Source designs from `D:\Desktop\autoteam-1\AutoTeam` are classified as absorbed, replaced by a stronger current implementation, or explicitly rejected with a reason.
3. Docker deployment has concrete resource boundaries, healthcheck, fast image path, and startup self-check.
4. Runtime resource and Playwright lifecycle hardening are implemented and covered by tests.
5. Optional HTTP transport is opt-in and cannot default-change free registration/OAuth behavior.
6. Free registration Round 11/12 regressions pass.
7. Verification uses real commands, not only a checklist.

## Prompt-to-Artifact Checklist

| Requirement | Evidence | Verdict |
| --- | --- | --- |
| Write PRD/spec/plan first under `prompts/0515` | `prompts/0515/prd.md`, `spec.md`, `implementation-plan.md`, `research-docker-playwright-hardening.md`, `autoteam1-absorption-audit.md` exist and define the safe absorption plan | Pass |
| Map source designs to decisions | `prompts/0515/autoteam1-absorption-audit.md` classifies Docker, runtime resources, Playwright lifecycle, probe, transport, mail/provider, CPA, Sub2API/sync-target, old manager/codex_auth/invite, generated artifacts, and codex-watchdog | Pass |
| Absorb Docker resource boundaries | `docker-compose.yml` has `init: true`, `shm_size: "1gb"`, `mem_limit: "2g"`, `memswap_limit: "2g"`, `pids_limit: 768`, resource warning env vars, and `/api/version` healthcheck | Pass |
| Keep build identity and self-check | `Dockerfile.fast` keeps `GIT_SHA`/`BUILD_TIME`, OCI labels, and `docker-entrypoint.sh`; entrypoint imports 15 critical symbols and exits on self-check failure | Pass |
| Add runtime resource hardening | `src/autoteam/runtime_resources.py` reads `/proc/self/status`, cgroup memory/pids, browser process live/zombie counts, warns and runs `gc.collect()` above threshold | Pass |
| Expose resource snapshot safely | `/api/status` returns `runtime_resources`; `_auto_check_loop()` logs `log_runtime_resource_snapshot(...)` | Pass |
| Add Playwright cleanup | `src/autoteam/playwright_lifecycle.py` closes page -> context -> browser -> playwright; `ChatGPTTeamAPI._launch_browser()` calls `stop()` on partial init failure; `stop()` clears fields | Pass |
| Isolate background Team count probe | `src/autoteam/playwright_probe.py` provides `team-member-count`; `src/autoteam/api.py` runs it in a subprocess with timeout and process-group kill fallback | Pass |
| Add opt-in transport only | `.env.example` and `src/autoteam/config.py` default `CHATGPT_API_TRANSPORT=playwright`; `src/autoteam/chatgpt_transport.py` is used only when configured and falls back on missing dependency/init errors/challenge/auth anomalies | Pass |
| Preserve free registration main flow | `FillParams.leave_workspace`, `fill-personal`, `cmd_fill(..., leave_workspace=True)`, `_run_post_register_oauth(..., leave_workspace=True)`, `plan_type=free`, `STATUS_PERSONAL`, plan drift and failure recording remain in the Round 11/12 path | Pass |
| Avoid unsafe source overwrite | Old source `manager.py`, `codex_auth.py`, `invite.py` were not copied over the current Round 11/12 implementations | Pass |
| Treat stronger current implementations correctly | Source `cloudflare_temp_email.py` / `mail_provider.py` are replaced by current `src/autoteam/mail/*`; source `sub2api_sync.py` / `sync_targets.py` are explicitly out of this hardening/Docker scope and recorded as a later standalone candidate | Pass |

## Verification Rerun

- `python -m pytest tests/unit/test_chatgpt_transport.py tests/unit/test_round11_api_fetch_header_sanitize.py tests/unit/test_api_playwright_cleanup.py tests/integration/test_docker_guard.py tests/unit/test_runtime_resources.py` -> `32 passed, 1 warning`
- `python -m pytest tests/unit/test_round11_personal_oauth_retry.py tests/unit/test_round11_session_token_injection.py tests/unit/test_round12_s4_register_dual_path.py tests/unit/test_manager_fill.py` -> `58 passed`
- `python -m pytest tests/unit/test_api_status.py tests/unit/test_playwright_guard.py tests/static/test_playwright_hygiene.py tests/unit/test_round12_rotate_sse_stream.py` -> `15 passed, 1 warning`
- `python -m ruff check src` -> `All checks passed!`
- Targeted ruff for changed code/tests -> `All checks passed!`
- `docker compose config` -> parses `init: true`, `mem_limit: 2147483648`, `memswap_limit: 2147483648`, `pids_limit: 768`, `shm_size: 1073741824`, and `/api/version` healthcheck
- `docker image inspect autoteam:fast-0515` -> label revision `0515-hardening`, created `2026-05-15T00:00:00Z`
- `docker run --rm autoteam:fast-0515 status` -> `[self-check] OK: 15 critical symbols imported.` and `[self-check] passed.`
- `python -m ruff check src tests` -> still fails only in pre-existing `tests/unit/test_round12_wireup.py` with import sorting, unused variables/import, and B009; this is outside this task's touched files and was not fixed here.
- `.trellis/spec/backend/runtime-docker-hardening.md` -> added as the reusable backend code-spec for this Docker/runtime/transport contract
- `.trellis/tasks/05-15-autoteam1-hardening-docker-apply/check.jsonl` -> valid JSONL, 25 lines
- `.trellis/tasks/05-15-autoteam1-hardening-docker-apply/implement.jsonl` -> valid JSONL, 10 lines

## Conclusion

The hardening, stability, overflow/resource-containment, Docker deployment, killable Playwright probe, Playwright lifecycle cleanup, and opt-in transport designs from `D:\Desktop\autoteam-1` have been absorbed or replaced by stronger current implementations. The remaining source-only modules are either generated/runtime artifacts, already covered by the current stronger mail/registration stack, or unrelated product integrations that should not be mixed into this safety-focused task. The free-account registration main flow is protected by the Round 11/12 regression set and remains on the default Playwright path.

No implementation work remains for this objective. The Trellis task has not been archived or committed because the worktree contains unrelated dirty/untracked files and the user did not request a commit.
