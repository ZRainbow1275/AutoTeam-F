# Apply autoteam-1 hardening and Docker deployment features

## Goal

Apply selected stability, overflow-prevention, and Docker deployment hardening from `D:\Desktop\autoteam-1` to the current `D:\Desktop\AutoTeam` project, while preserving the current free-account registration flow.

## Authoritative Planning Docs

- `prompts/0515/prd.md`
- `prompts/0515/spec.md`
- `prompts/0515/implementation-plan.md`
- `prompts/0515/research-docker-playwright-hardening.md`

## What I Already Know

- Current AutoTeam already contains newer Round 11/12 free registration protections: `leave_workspace=True`, Personal OAuth retry, plan drift recording, workspace pool, register dual path, master health checks, and related tests.
- `D:\Desktop\autoteam-1\AutoTeam` contributes useful low-coupling hardening: Docker resource boundaries, `runtime_resources.py`, `playwright_lifecycle.py`, `Dockerfile.fast`, and optional `chatgpt_transport.py`.
- `D:\Desktop\autoteam-1\codex-watchdog` history warns that automation safety must be conservative and dry-run verifiable.

## Requirements

- Preserve current free-account registration semantics and tests.
- Add Docker/Compose hardening without removing existing build fingerprint and entrypoint self-check contracts.
- Add runtime resource probes and Playwright lifecycle cleanup with unit coverage.
- Keep `curl_cffi` transport out of the first implementation round unless explicitly approved later.

## Acceptance Criteria

- [ ] `prompts/0515` docs are present and accepted before implementation.
- [ ] Docker compose has init, shm, memory/PID boundaries, resource env vars, and healthcheck.
- [ ] Runtime resource probe gracefully handles missing cgroup/proc files.
- [ ] Playwright cleanup is idempotent and cleans partially initialized browser sessions.
- [ ] Free registration regression tests pass.
- [ ] Docker guard and relevant unit tests pass.

## Out of Scope

- Rewriting free registration.
- Directly overwriting current manager/codex OAuth code with older `autoteam-1` files.
- Enabling optional HTTP transport by default in the first round.

## Research References

- `research/docker-playwright-hardening.md`

## Technical Approach

Use a staged implementation:

1. Docker/Compose hardening.
2. Runtime resource and Playwright lifecycle hardening.
3. Free registration regression verification.
4. Optional transport evaluation as a separate later stage.

## Definition of Done

- Tests added/updated where behavior changes.
- Lint/static checks pass.
- Docker config validates.
- No unverified claim of Docker runtime success without a real command result.
- Docs updated with rollout and rollback notes.

