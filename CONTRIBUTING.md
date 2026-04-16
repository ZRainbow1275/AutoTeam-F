# Contributing

Thanks for contributing to AutoTeam.

## Development Setup

1. Clone the repository.
2. Install development dependencies:

```bash
uv sync --dev
```

3. Copy the example environment file if needed:

```bash
cp .env.example .env
```

4. Update `.env` with your local configuration before running features that depend on CloudMail, CPA, or browser automation.

## Common Commands

Run the main local checks before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python -m compileall -q src/autoteam
```

If formatting is required:

```bash
uv run ruff format .
```

## Branching

- create feature or fix branches from `dev`
- keep pull requests focused on a single topic
- prefer small, reviewable commits

## Pull Requests

Please include:

- a short summary of the problem
- the approach you took
- validation steps you ran locally
- screenshots or log excerpts when UI or browser behavior changes

If your change affects login, sync, quota handling, or Team membership flows, include a brief regression checklist in the PR description.

## Testing Guidance

When adding or changing behavior:

- add or update pytest coverage for the affected logic where possible
- preserve recent regression fixes unless the change intentionally replaces them
- avoid depending on real secrets in tests

## Security and Sensitive Data

- never commit real session tokens, auth files, mailbox credentials, or production `.env` values
- scrub logs and screenshots before sharing them publicly
- follow `SECURITY.md` for vulnerability reporting

## Commit Style

Use short, descriptive commit messages, for example:

- `fix: guard main account removal`
- `test: cover setup wizard non-interactive flow`
- `docs: clarify Docker startup steps`
