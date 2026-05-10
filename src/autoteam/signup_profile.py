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

Backward-compat: :class:`SignupProfile` is created with random_* helpers from
:mod:`autoteam.identity` so behaviour matches the previous fork's per-call
randomness — only the *consistency across the two stages* is new.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from autoteam.identity import random_age, random_birthday, random_full_name


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

    # ---------------------------------------------------- spinbutton helper
    def positional_birthday_orders(self) -> list[list[str]]:
        """Yield candidate orderings for the React-Aria DateField spinbuttons.

        The OpenAI about-you DateField renders three ``role="spinbutton"`` cells
        in order ``year / month / day`` for the en-US locale. We return the
        canonical Y/M/D ordering as the only candidate — the surrounding code
        path tries the locator and falls back to a flat ``input[name='age']``
        if the spinbuttons are absent, so a single best-guess ordering is
        sufficient.
        """
        bday = self.birthday or {}
        return [[bday.get("year", ""), bday.get("month", ""), bday.get("day", "")]]


def generate_signup_profile() -> SignupProfile:
    """Build a fresh :class:`SignupProfile` for a new account."""
    return SignupProfile(
        full_name=random_full_name(),
        birthday=random_birthday(),
        age=random_age(),
    )


__all__ = [
    "SignupProfile",
    "generate_signup_profile",
]
