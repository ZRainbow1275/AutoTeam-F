"""SignupProfile dataclass + factory.

Round 12 S3 — cherry-pick from upstream. Locally we already had per-call
``random_birthday`` / ``random_full_name`` / ``random_age`` helpers in
:mod:`autoteam.identity`, but each invocation was independent — meaning the
"about-you" page during *registration* and the second "about-you" page during
*Codex OAuth* would receive different name/birthday/age values for the same
account, which OpenAI's risk model can flag.

This module provides a single :class:`SignupProfile` snapshot that callers
generate **once per account** and pass through both ``register_with_invite``
and ``login_codex_via_browser``, so the two pages see consistent data.

Backward-compat: :class:`SignupProfile` keeps the existing field shape used by
callers. The birthday and age are generated together so the same snapshot is
also internally self-consistent.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from autoteam.identity import random_full_name

MIN_SIGNUP_AGE = 22
MAX_SIGNUP_AGE = 42


class _FrozenBirthday(dict[str, str]):
    """Read-compatible birthday mapping that rejects in-place mutation."""

    _ERROR = "SignupProfile.birthday is immutable"

    def __init__(self, values=None, **kwargs):
        super().__init__(values or {}, **kwargs)

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.items())))

    def __setitem__(self, key, value):
        raise TypeError(self._ERROR)

    def __delitem__(self, key):
        raise TypeError(self._ERROR)

    def clear(self):
        raise TypeError(self._ERROR)

    def pop(self, key, default=None):
        raise TypeError(self._ERROR)

    def popitem(self):
        raise TypeError(self._ERROR)

    def setdefault(self, key, default=None):
        raise TypeError(self._ERROR)

    def update(self, *args, **kwargs):
        raise TypeError(self._ERROR)

    def __ior__(self, other):
        raise TypeError(self._ERROR)


@dataclass(frozen=True)
class SignupProfile:
    """Immutable random identity snapshot for a single account registration.

    Fields mirror the values OpenAI's about-you page asks for. Both the invite
    registration page (:func:`autoteam.invite.register_with_invite`) and the
    Codex OAuth about-you page consume the same instance so name/birthday/age
    are perfectly consistent across stages.
    """

    full_name: str
    birthday: dict[str, str] = field(default_factory=dict)
    age: str = ""

    def __post_init__(self) -> None:
        frozen_birthday = _FrozenBirthday(
            {str(key): str(value) for key, value in (self.birthday or {}).items()}
        )
        object.__setattr__(self, "birthday", frozen_birthday)

    # ------------------------------------------------------------ properties
    @property
    def birthday_text(self) -> str:
        """Human-readable birthday text used purely for log messages."""
        bday = self.birthday or {}
        return f"{bday.get('year', '')}-{bday.get('month', '')}-{bday.get('day', '')}"

    @property
    def age_text(self) -> str:
        """Age as a string (the about-you age input expects a string)."""
        return self.age

    @property
    def birth_date(self) -> date:
        """Birthday as a ``date`` for validation and tests."""
        bday = self.birthday or {}
        return date(int(bday["year"]), int(bday["month"]), int(bday["day"]))

    # ---------------------------------------------------- spinbutton helper
    def positional_birthday_orders(self) -> list[list[str]]:
        """Yield candidate orderings for the React-Aria DateField spinbuttons.

        Prefer the canonical ``year / month / day`` ordering, but keep the
        month/day/year and day/month/year fallbacks from the autoteam-1
        template for pages where React-Aria exposes the same spinbuttons in a
        locale-dependent visual order.
        """
        bday = self.birthday or {}
        year = bday.get("year", "")
        month = bday.get("month", "")
        day = bday.get("day", "")
        return [
            [year, month, day],
            [month, day, year],
            [day, month, year],
        ]


def calculate_age(birth_date: date, today: date) -> int:
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _replace_year_safe(value: date, year: int) -> date:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def _birthdate_bounds(
    today: date,
    *,
    min_age: int = MIN_SIGNUP_AGE,
    max_age: int = MAX_SIGNUP_AGE,
) -> tuple[date, date]:
    oldest_allowed = _replace_year_safe(today, today.year - (max_age + 1)) + timedelta(days=1)
    youngest_allowed = _replace_year_safe(today, today.year - min_age)
    return oldest_allowed, youngest_allowed


def _birthday_dict(birth_date: date) -> dict[str, str]:
    return {
        "year": f"{birth_date.year:04d}",
        "month": f"{birth_date.month:02d}",
        "day": f"{birth_date.day:02d}",
    }


def generate_signup_profile(
    *,
    today: date | None = None,
    rng: random.Random | random.SystemRandom | None = None,
) -> SignupProfile:
    """Build a fresh :class:`SignupProfile` for a new account."""
    today = today or date.today()
    rng = rng or random.SystemRandom()
    oldest_allowed, youngest_allowed = _birthdate_bounds(today)
    offset = rng.randrange((youngest_allowed - oldest_allowed).days + 1)
    birth_date = oldest_allowed + timedelta(days=offset)
    age = calculate_age(birth_date, today)
    if not (MIN_SIGNUP_AGE <= age <= MAX_SIGNUP_AGE):
        raise ValueError(f"generated invalid signup age {age} for birth date {birth_date.isoformat()}")

    return SignupProfile(
        full_name=random_full_name(rng=rng),
        birthday=_birthday_dict(birth_date),
        age=str(age),
    )


__all__ = [
    "MAX_SIGNUP_AGE",
    "MIN_SIGNUP_AGE",
    "SignupProfile",
    "calculate_age",
    "generate_signup_profile",
]
