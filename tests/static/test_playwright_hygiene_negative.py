"""SPEC-4 §7: 反例验证 — 证明 hygiene 守卫真能拦下违规代码。

通过把违规代码字符串直接 ast.parse 后跑相同的 AST 规则,断言它们被识别为
违规。该测试不依赖 SRC_ROOT,默认进 CI 跑;它独立于主 hygiene 测试运行,
因此不会污染主测试的 SRC_ROOT 路径。
"""

from __future__ import annotations

import ast
import textwrap

from autoteam._playwright_guard import (
    ALLOWED_SYNC_NAMES,
    FORBIDDEN_MODULES,
)

PLAYWRIGHT_NAMESPACE = ALLOWED_SYNC_NAMES | {"async_playwright"}


def _has_forbidden_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    return True
    return False


def _has_playwright_in_async_def(tree: ast.AST) -> bool:
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for child in ast.walk(fn):
            name = getattr(child, "id", None) or getattr(child, "attr", None)
            if name in PLAYWRIGHT_NAMESPACE:
                return True
    return False


def _has_non_whitelist_sync_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "playwright.sync_api":
            for alias in node.names:
                if alias.name not in ALLOWED_SYNC_NAMES:
                    return True
    return False


def test_forbidden_async_api_import_is_detected():
    src = "from playwright.async_api import async_playwright\n"
    tree = ast.parse(src)
    assert _has_forbidden_import(tree), "async_api import 应当被守卫识别"


def test_async_def_with_sync_playwright_is_detected():
    src = textwrap.dedent("""\
        from playwright.sync_api import sync_playwright

        async def evil_handler():
            with sync_playwright() as p:
                p.chromium.launch()
    """)
    tree = ast.parse(src)
    assert _has_playwright_in_async_def(tree), "async def 内 sync_playwright 应被识别"


def test_async_def_with_page_attribute_is_detected():
    src = textwrap.dedent("""\
        async def evil_handler(browser):
            page = await browser.new_page()
            return Page  # noqa
    """)
    tree = ast.parse(src)
    assert _has_playwright_in_async_def(tree), "async def 内引用 Page 应被识别"


def test_non_whitelist_sync_import_is_detected():
    src = "from playwright.sync_api import Request\n"
    tree = ast.parse(src)
    assert _has_non_whitelist_sync_import(tree), "白名单外符号应被识别"


def test_clean_file_passes_all_checks():
    """正向反例: 合法的 sync 用法不应被任一规则误伤"""
    src = textwrap.dedent("""\
        from playwright.sync_api import sync_playwright, Page, Browser

        def login_flow():
            with sync_playwright() as p:
                browser: Browser = p.chromium.launch()
                page: Page = browser.new_page()
                page.goto("https://example.com")
                browser.close()
    """)
    tree = ast.parse(src)
    assert not _has_forbidden_import(tree)
    assert not _has_playwright_in_async_def(tree)
    assert not _has_non_whitelist_sync_import(tree)
