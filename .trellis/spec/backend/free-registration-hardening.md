# Free Registration Hardening

## Scenario: Fill-Personal Safety Boundary

### 1. Scope / Trigger

- Trigger: any change to `POST /api/tasks/fill`, `cmd_fill(..., leave_workspace=True)`, `_cmd_fill_personal()`, `create_account_direct(..., leave_workspace=True)`, `_run_post_register_oauth(..., leave_workspace=True)`, or `SignupProfile`.
- Goal: make the free-account registration flow safer and more diagnosable without changing the successful-path semantics.
- Safety boundary: do not add bypasses for captcha, human verification, platform restrictions, rate limits, or anti-abuse systems. Hardening means consistency, early rejection, cleanup, and auditability.

### 2. Signatures

- `TaskParams.leave_workspace: bool = False`
- `TaskParams.target: int = 3`
- `post_fill(params: TaskParams = TaskParams())`
- `cmd_fill(target=3, leave_workspace=False)`
- `_cmd_fill_personal(count)`
- `create_new_account(chatgpt_api, mail_client=None, *, leave_workspace=False, out_outcome=None, acc=None, path_rotator=None)`
- `create_account_direct(mail_client=None, *, leave_workspace=False, out_outcome=None, acc=None, path_rotator=None)`
- `_run_post_register_oauth(email, password, mail_client, leave_workspace=False, out_outcome=None, chatgpt_session_token=None, signup_profile=None)`
- `generate_signup_profile(*, today: date | None = None, rng: random.Random | random.SystemRandom | None = None) -> SignupProfile`

### 3. Contracts

- API command mapping must remain:
  - `leave_workspace=True` -> `"fill-personal"`
  - `leave_workspace=False` -> `"fill"`
- The free path must remain:
  - register into Team
  - remove from Team with master authority
  - run Personal OAuth
  - accept only `plan_type == "free"`
  - persist `STATUS_PERSONAL`
- API-level fill-personal preflight must use the same local Team-seat definition as `manager._count_local_team_seat_accounts()`.
- Local Team-seat statuses are `STATUS_ACTIVE`, `STATUS_EXHAUSTED`, and `STATUS_AUTH_INVALID`; `STATUS_PERSONAL` is not a Team seat.
- Current Team-seat target contract is `3 = 1 owner + 2 managed children`. Team-target inputs for rotate/fill/auto-check must be clamped to `1..3`; the child-account hard cap is `2`.
- `SignupProfile` must be a single immutable snapshot passed through registration and OAuth. Its nested `birthday` mapping must reject in-place mutation and must be defensively copied from constructor input. Generated birthday and age must also be self-consistent.
- Registration, direct registration, Team OAuth, and Personal OAuth must not use hardcoded fallback identities such as `User`, `1995-06-15`, or age `25` when a `SignupProfile` is available.
- OAuth about-you must consume the same `SignupProfile`, try the profile's supported birthday field orders, and return failure if the page still remains on about-you after all supported orders. The caller must treat that failure as `bundle=None` so the existing retry/failure-classification policy can handle it.
- `CHATGPT_API_TRANSPORT` defaults to `auto` for Team backend API reads, matching `D:\Desktop\autoteam-1\AutoTeam`; free registration and Personal OAuth still require a real browser context and must not rely on HTTP-only transport.
- Direct free-registration setup may use Team backend HTTP transport only before the protected browser/OAuth boundary. The registration page, Team kick, Personal OAuth, about-you, and plan validation path must remain browser-backed or explicitly `require_browser=True`.
- Direct registration must extract the ChatGPT session token before cleanup on the success path, then pass that token plus the same `SignupProfile` into `_run_post_register_oauth(..., leave_workspace=True)`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| local Team seats >= `TEAM_SUB_ACCOUNT_HARD_CAP` before fill-personal | API returns 409 and does not start a background task |
| local Team seats include `STATUS_AUTH_INVALID` | Count them as occupied seats |
| generated birthday implies age outside allowed range | raise `ValueError` during profile generation |
| caller mutates `profile.birthday["year"]` or updates the birthday mapping | raise `TypeError`; profile remains unchanged |
| OAuth about-you appears after registration | Fill it from the same `SignupProfile` used for registration |
| OAuth about-you submit stays on profile page after one birthday order | retry the next supported order from `SignupProfile.positional_birthday_orders()` |
| OAuth about-you stays on profile page after all supported orders | return `None`/failure to the caller; do not continue into consent loop as if profile succeeded |
| Personal OAuth gets `plan_type != "free"` or no bundle | retry up to the existing 5-attempt policy, then record plan drift and fail fast |
| `remove_from_team` fails before Personal OAuth | mark/keep safe local state, record `kick_failed`, and do not run Personal OAuth |
| `RegisterBlocked` phone/add-phone | terminal failure, record category, delete or quarantine according to existing manager logic |
| direct registration page/navigation fails before a normal result | release Playwright page/context/browser and return/raise through the existing retry classifier without losing the local `SignupProfile` contract |

### 5. Good/Base/Bad Cases

- Good: API rejects fill-personal when the local child-seat set already reaches the hard cap, for example `{active, auth_invalid}`.
- Base: a normal `leave_workspace=False` fill does not use the free-path preflight.
- Bad: counting only `active/exhausted` at API level while manager counts `auth_invalid` as a seat, because that starts a task that should have been rejected before browser/mail work.

### 6. Tests Required

- Profile consistency:
  - `tests/unit/test_round12_s3_cherry_pick.py`
  - assert `profile.age == calculate_age(profile.birth_date, today)`
  - assert injected RNG makes `generate_signup_profile(today=..., rng=...)` deterministic
  - assert nested `profile.birthday` mutation raises `TypeError`
  - assert constructor input is copied so later caller-side dict mutation cannot alter the profile
- Registration/OAuth profile propagation:
  - `tests/unit/test_free_registration_hardening.py`
  - assert OAuth about-you consumes the provided `SignupProfile`
  - assert OAuth about-you retries birthday orders and reports failure if no order exits the profile page
  - assert direct registration passes the same `SignupProfile` into `_run_post_register_oauth()`
- API preflight:
  - `tests/unit/test_free_registration_hardening.py`
  - assert `auth_invalid` contributes to Team-seat hard-cap rejection
- Main free registration regression:
  - `tests/unit/test_round11_personal_oauth_retry.py`
  - `tests/unit/test_round11_session_token_injection.py`
  - `tests/unit/test_round12_s4_register_dual_path.py`
  - `tests/unit/test_manager_fill.py`

### 7. Wrong vs Correct

#### Wrong

```python
in_team_local = sum(
    1 for a in load_accounts()
    if a.get("status") in (STATUS_ACTIVE, STATUS_EXHAUSTED)
)
```

This misses `STATUS_AUTH_INVALID`, which the manager treats as a Team-seat occupant.

#### Correct

```python
in_team_local = _count_local_team_seat_accounts(load_accounts())
```

Keep the API entrypoint and manager entrypoint aligned so unsafe fill-personal work is rejected before starting browser or mail-provider operations.
