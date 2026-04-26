"""SPEC-4 §3: assert_sync_context 行为验证。

注: SPEC §4.2 原稿用 @pytest.mark.asyncio,但仓库未引入 pytest-asyncio。
为避免新增依赖,asyncio loop 内的测试改用 asyncio.run() 直接驱动协程,
行为等价,在 loop 内调用 assert_sync_context() 仍会触发 RuntimeError。
"""

from __future__ import annotations

import asyncio
import re
import threading

import pytest

from autoteam._playwright_guard import assert_sync_context


def test_passes_in_plain_thread():
    """普通线程无 loop, guard 必须放行"""
    holder: dict = {}

    def worker():
        try:
            assert_sync_context()
            holder["ok"] = True
        except Exception as e:  # noqa: BLE001
            holder["err"] = e

    t = threading.Thread(target=worker, name="test-plain")
    t.start()
    t.join(timeout=2.0)
    assert holder.get("ok") is True
    assert "err" not in holder


def test_blocks_in_asyncio_loop():
    """asyncio loop 内调用必须抛 RuntimeError"""
    async def runner():
        assert_sync_context()

    with pytest.raises(RuntimeError, match=r"\[PlaywrightGuard\].*asyncio loop"):
        asyncio.run(runner())


def test_message_contains_thread_and_loop_id():
    """异常消息应携带 thread 名 + loop id 便于诊断"""
    captured: dict = {}

    async def runner():
        try:
            assert_sync_context()
        except RuntimeError as e:
            captured["msg"] = str(e)

    asyncio.run(runner())
    msg = captured.get("msg", "")
    assert "thread=" in msg
    assert re.search(r"loop_id=0x[0-9a-f]+", msg)
