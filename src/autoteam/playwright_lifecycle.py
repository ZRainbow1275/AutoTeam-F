"""Utilities for consistently releasing Playwright resources."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _call_close(obj: Any, method_name: str) -> None:
    method: Callable[[], Any] | None = getattr(obj, method_name, None)
    if callable(method):
        method()


def close_playwright_objects(
    page: Any = None,
    context: Any = None,
    browser: Any = None,
    playwright: Any = None,
    *,
    logger: Any = None,
    label: str = "playwright",
) -> None:
    """Best-effort close for Playwright objects in dependency order."""

    for name, obj, method_name in (
        ("page", page, "close"),
        ("context", context, "close"),
        ("browser", browser, "close"),
        ("playwright", playwright, "stop"),
    ):
        if obj is None:
            continue
        try:
            _call_close(obj, method_name)
        except Exception as exc:
            if logger is not None:
                logger.debug("[%s] close %s failed: %s", label, name, exc)
