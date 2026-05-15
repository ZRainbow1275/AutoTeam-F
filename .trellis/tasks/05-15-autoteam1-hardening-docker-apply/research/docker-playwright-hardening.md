# Research: Docker and Playwright hardening for AutoTeam

## Sources

- Docker Compose services reference: <https://docs.docker.com/reference/compose-file/services/>
- Docker Compose deploy resources reference: <https://docs.docker.com/reference/compose-file/deploy/#resources>
- Playwright Python Docker guide: <https://playwright.dev/python/docs/docker>
- Playwright Docker guide: <https://playwright.dev/docs/docker>

## Takeaways

- `init: true` is appropriate for a long-running service that starts browser subprocesses because it helps signal handling and zombie reaping.
- Chromium is sensitive to Docker shared-memory defaults. Playwright recommends host IPC in some workflows; for this project, `shm_size: "1gb"` is the safer compose default because it improves stability without broadening IPC isolation.
- Health checks should hit a real low-cost endpoint. The current project already has `/api/version`, so use it instead of process-only checks.
- Resource boundaries should prevent browser and task growth from consuming the host. Compose-level memory and PID limits plus runtime cgroup probes are both useful.
- Current AutoTeam already has Docker build args, OCI labels, and entrypoint self-checks. Any `Dockerfile.fast` imported from `autoteam-1` must preserve those contracts.

## Local mapping

- Use `D:\Desktop\autoteam-1\AutoTeam\docker-compose.yml` as a reference for `init`, `shm_size`, memory/PID limits, and resource env vars.
- Use `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\runtime_resources.py` as the reference implementation for cgroup and zombie probes.
- Use `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\playwright_lifecycle.py` as the reference implementation for cleanup ordering.
- Do not import old registration logic over current Round 11/12 free registration code.

