# Free Registration Round 2 Completion Audit

## Objective

Critically compare `D:\Desktop\autoteam-1\AutoTeam` and `D:\Desktop\AutoTeam` with the former as the mother template, then add another safety-focused hardening round for the free-account registration main flow.

## Safety Boundary

This round deliberately does not add bypasses for captcha, human verification, platform restrictions, rate limits, or anti-abuse systems. The accepted hardening scope is data consistency, local/remote state consistency, early rejection of unsafe tasks, auditability, and tests.

## Prompt-to-Artifact Checklist

| Requirement | Evidence | Verdict |
| --- | --- | --- |
| Critically compare both projects with `autoteam-1` as template | `prompts/0515/free-registration-round2-critical-audit.md` compares manager/codex_auth/invite/signup_profile/accounts/cpa/mail dimensions | Pass |
| Preserve free registration main flow | No change to `_run_post_register_oauth(..., leave_workspace=True)`, Personal OAuth retry count, `plan_type=free`, or `STATUS_PERSONAL` semantics | Pass |
| Absorb mother-template profile consistency | `src/autoteam/signup_profile.py` now derives `age` from generated birthday and validates age range while preserving current `SignupProfile` field shape | Pass |
| Align API and manager Team-seat preflight | `src/autoteam/api.py:post_fill()` now uses `manager._count_local_team_seat_accounts(load_accounts())`, so `STATUS_AUTH_INVALID` contributes to the fill-personal hard cap | Pass |
| Add regression coverage | `tests/unit/test_round12_s3_cherry_pick.py` checks age/birthday consistency; `tests/unit/test_free_registration_hardening.py` checks API 409 when seats include auth_invalid | Pass |
| Preserve documented contract | `.trellis/spec/backend/free-registration-hardening.md` added and indexed | Pass |

## Verification

- `python -m pytest tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_free_registration_hardening.py` -> `45 passed, 1 warning`
- `python -m ruff check src/autoteam/signup_profile.py src/autoteam/api.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_free_registration_hardening.py` -> `All checks passed!`
- `python -m pytest tests/unit/test_round11_personal_oauth_retry.py tests/unit/test_round11_session_token_injection.py tests/unit/test_round12_s4_register_dual_path.py tests/unit/test_manager_fill.py` -> `58 passed`
- `python -m pytest tests/unit/test_chatgpt_transport.py tests/unit/test_api_playwright_cleanup.py tests/unit/test_runtime_resources.py tests/integration/test_docker_guard.py` -> `28 passed, 1 warning`
- `python -m pytest tests/unit/test_round8_integration.py tests/unit/test_api_status.py tests/unit/test_round12_rotate_sse_stream.py` -> `22 passed, 1 warning`
- `python -m ruff check src` -> `All checks passed!`

## Known Non-Blocking State

The repository still has unrelated dirty/untracked files and the known full-suite `ruff src tests` failure in `tests/unit/test_round12_wireup.py` from earlier work. This round does not modify that unrelated WIP.

## Conclusion

Round 2 is complete. The mother-template comparison produced two safe, concrete hardening changes: signup profile age/birthday consistency and API/manager Team-seat preflight alignment. The free registration main flow remains unchanged beyond earlier unsafe-task rejection and consistency safeguards.
