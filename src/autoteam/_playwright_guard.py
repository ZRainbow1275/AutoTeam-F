"""Playwright 一致性硬化(SPEC-4)。

本模块提供白名单常量与运行时守卫,禁止 sync_playwright 进入 asyncio loop。
所有静态测试(tests/static/test_playwright_hygiene.py)与运行时 guard
(api.py:_PlaywrightExecutor)共享本文件作为单一信源(SSOT)。
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


# 9 个允许从 playwright.sync_api 导入的符号
ALLOWED_SYNC_NAMES: frozenset[str] = frozenset({
    "sync_playwright",   # 上下文管理器
    "Playwright",        # 顶层句柄类型
    "Browser",           # 浏览器实例类型
    "BrowserContext",    # 浏览器上下文
    "BrowserType",       # chromium/firefox/webkit 类型
    "Page",              # 页面句柄
    "Locator",           # 定位器
    "Error",             # Playwright 通用异常
    "TimeoutError",      # Playwright 超时异常
})

# 1 个 typeshed 类型(仅在 if TYPE_CHECKING 块内允许)
TYPE_CHECKING_ONLY: frozenset[str] = frozenset({
    "ElementHandle",
})

# 任何形式的 from <X> import / import <X> 禁止模块
FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "playwright.async_api",
})

# per-file 豁免 marker,文件首 5 行内出现即整文件跳过守卫
EXEMPTION_MARKER: str = "autoteam: allow-async-playwright"


def assert_sync_context() -> None:
    """断言当前线程不在 asyncio loop 中。

    仅用于 sync_playwright 调用前的前置检查。若检测到 loop,立即抛
    RuntimeError(消息含 thread/loop_id 标识,便于诊断)。

    期望路径: 普通线程 / 专用 worker 线程 → asyncio.get_running_loop()
    抛 RuntimeError → 无 loop → 放行。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"[PlaywrightGuard] thread={threading.current_thread().name!r} "
        f"loop_id=0x{id(loop):x} — sync_playwright 不允许在 asyncio loop 中调用。"
        f"详见 PRD-4 §12 R1 opt-out 流程。"
    )
