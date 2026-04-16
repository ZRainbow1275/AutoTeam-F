# Security Policy

## Supported Versions

Security fixes are applied to:

- the latest code on `dev`
- the latest published release tag, when a release exists

Older branches and historical commits may not receive fixes.

## Reporting a Vulnerability

Please report security issues privately and avoid opening a public issue with sensitive details.

When reporting, include:

- a clear summary of the issue
- affected versions or commit SHA
- reproduction steps or proof of concept
- expected impact
- any suggested mitigation

If GitHub private vulnerability reporting is available for this repository, use it first. Otherwise, contact the maintainers privately before any public disclosure.

## Disclosure Expectations

- Do not publish session tokens, auth files, temporary mailbox credentials, or other secrets in issues or pull requests.
- Give maintainers reasonable time to validate and fix the problem before public disclosure.
- If a fix requires configuration changes, please document the secure default behavior as part of the report.

## Operational Notes

This project handles authentication state and related local files. In production-like environments, operators should:

- restrict access to `.env`, `state.json`, `accounts.json`, and `auths/`
- rotate exposed credentials immediately
- avoid storing exported auth material in shared or world-readable locations
