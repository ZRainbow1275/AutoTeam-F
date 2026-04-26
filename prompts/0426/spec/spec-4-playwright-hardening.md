# SPEC-4: Playwright 一致性硬化 实施规范

## 0. 元数据

| 字段 | 值 |
|---|---|
| SPEC 编号 | SPEC-4 |
| 关联 PRD | `prompts/0426/prd/prd-4-playwright-hardening.md` |
| 关联 Issue | #5(待用户补样本) |
| 类型 | hardening only(纯防御性,不动业务逻辑) |
| 状态 | DRAFT-IMPLEMENTABLE |
| 主笔 | prd-playwright |
| 创建日期 | 2026-04-26 |

---

## 1. 文件级修改清单

| 序号 | 路径 | 动作 | 说明 |
|---|---|---|---|
| 1 | `src/autoteam/_playwright_guard.py` | **新增** | 白名单常量 + `assert_sync_context()` + 豁免 marker 常量 |
| 2 | `src/autoteam/api.py` | **修改** | `_PlaywrightExecutor.run_with_timeout` + `_worker` 双入口插入 guard(锚点参考研究 §B.2.5,定位行 173-227) |
| 3 | `src/autoteam/manager.py` | **修改** | 函数内 import 上提至模块顶层(第 1491 行 + 第 1969 行) |
| 4 | `tests/static/__init__.py` | **新增** | 空文件,确保 pytest 能 collect |
| 5 | `tests/static/test_playwright_hygiene.py` | **新增** | 3 个 AST 测试 + 1 个豁免列表测试 |
| 6 | `tests/static/test_playwright_hygiene_negative.py` | **新增** | 反例验证(参见 §7) |
| 7 | `tests/unit/test_playwright_guard.py` | **新增** | runtime guard 行为测试(asyncio loop 内/外) |
| 8 | `pyproject.toml` 或 `pytest.ini` | **修改** | `testpaths` 加入 `tests/static`、`tests/unit` |
| 9 | `CONTRIBUTING.md` | **可选** | 若已存在,追加"Playwright 使用守则"5 行;不存在则跳过 |

**严禁动**:`chatgpt_api.py`、`codex_auth.py`、`invite.py`、`api.py:_PlaywrightExecutor` 之外的代码、所有 `with sync_playwright() as p:` 块本体。

---

## 2. import 白名单常量

落地于 `src/autoteam/_playwright_guard.py`,作为**单一信源**(SSOT),所有静态测试与运行时 guard 共用:

```python
# src/autoteam/_playwright_guard.py
"""Playwright 一致性硬化(SPEC-4)。
本模块提供白名单常量与运行时守卫,禁止 sync_playwright 进入 asyncio loop。
"""
from __future__ import annotations

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
    "ElementHandle",  # 仅类型注解使用,运行时禁止
})

# 任何 from <X> import * 形式的禁止模块
FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "playwright.async_api",
})

# per-file 豁免 marker,文件首 5 行内出现即整文件跳过守卫
EXEMPTION_MARKER: str = "autoteam: allow-async-playwright"
```

---

## 3. `_PlaywrightExecutor` guard 完整代码

`assert_sync_context()` 实现(同样在 `_playwright_guard.py` 内)+ `api.py` 调用点 patch。

### 3.1 guard 函数

```python
# src/autoteam/_playwright_guard.py(续)
import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


def assert_sync_context() -> None:
    """断言当前线程不在 asyncio loop 中。
    仅用于 sync_playwright 调用前的前置检查。
    若检测到 loop,立即抛 RuntimeError(中文堆栈 + thread/loop 标识)。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 期望路径:无 loop
    raise RuntimeError(
        f"[PlaywrightGuard] thread={threading.current_thread().name!r} "
        f"loop_id=0x{id(loop):x} — sync_playwright 不允许在 asyncio loop 中调用。"
        f"详见 PRD-4 §12 R1 opt-out 流程。"
    )
```

### 3.2 `api.py` 双入口 patch(diff 形式)

```python
# api.py:155 区域(_PlaywrightExecutor 类内,run_with_timeout 入口)
class _PlaywrightExecutor:
    def run_with_timeout(self, timeout, func, *args, **kwargs):
+       from autoteam._playwright_guard import assert_sync_context
+       assert_sync_context()  # SPEC-4 §3:主线程前置检查
        result_event = threading.Event()
        result_holder: dict = {}
        self._queue.put((func, args, kwargs, result_event, result_holder))
        ...

    def _worker(self):
+       from autoteam._playwright_guard import assert_sync_context
+       assert_sync_context()  # SPEC-4 §3:worker 启动检查
        while True:
            item = self._queue.get()
            if item is None:
                break
            ...
```

**双入口必要性**:`run_with_timeout` 在调用方线程检查(可能是 anyio threadpool worker),`_worker` 在专用线程启动时再检查一遍 — 防止某次重构里把 worker 的 `target=` 改成在 loop 内启的协程。

---

## 4. AST 守卫单测完整代码

### 4.1 主测试文件(可粘贴运行)

```python
# tests/static/test_playwright_hygiene.py
"""SPEC-4 §4:Playwright import + async/sync 分离静态守卫。
通过 AST 扫描 src/autoteam/**/*.py,断言:
  - 不出现 from playwright.async_api ...
  - async def 函数体内不引用任何 playwright 符号
  - playwright.sync_api 导入仅限白名单
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from autoteam._playwright_guard import (
    ALLOWED_SYNC_NAMES,
    EXEMPTION_MARKER,
    FORBIDDEN_MODULES,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "autoteam"
PLAYWRIGHT_NAMESPACE = ALLOWED_SYNC_NAMES | {"async_playwright"}


def _is_exempt(path: Path) -> bool:
    """文件首 5 行内出现 EXEMPTION_MARKER 即视为豁免。"""
    try:
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
    except OSError:
        return False
    return EXEMPTION_MARKER in head


def _iter_py_files():
    for path in SRC_ROOT.rglob("*.py"):
        if not _is_exempt(path):
            yield path


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_forbidden_module_import():
    """A1+A2: 禁止 from playwright.async_api / import playwright.async_api"""
    bad: list[str] = []
    for path in _iter_py_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
                bad.append(f"{path}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES:
                        bad.append(f"{path}:{node.lineno} import {alias.name}")
    assert not bad, "禁止使用 playwright.async_api:\n" + "\n".join(bad)


def test_no_playwright_in_async_def():
    """A3: async def 函数体内不允许 playwright 符号"""
    bad: list[str] = []
    for path in _iter_py_files():
        tree = _parse(path)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for child in ast.walk(fn):
                name = getattr(child, "id", None) or getattr(child, "attr", None)
                if name in PLAYWRIGHT_NAMESPACE:
                    bad.append(
                        f"{path}:{getattr(child, 'lineno', fn.lineno)} "
                        f"async def {fn.name} 内引用了 {name}"
                    )
    assert not bad, "async def 函数体内禁止 playwright:\n" + "\n".join(bad)


def test_sync_import_whitelist():
    """A4: playwright.sync_api 导入仅限 ALLOWED_SYNC_NAMES"""
    bad: list[str] = []
    for path in _iter_py_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "playwright.sync_api":
                for alias in node.names:
                    if alias.name not in ALLOWED_SYNC_NAMES:
                        bad.append(f"{path}:{node.lineno} 非白名单符号 {alias.name}")
    assert not bad, "sync_api 导入不在白名单:\n" + "\n".join(bad)


def test_exemption_list_intentional():
    """SPEC-4 §5: 豁免文件需登记理由,避免静默扩散"""
    exempt = [p for p in SRC_ROOT.rglob("*.py") if _is_exempt(p)]
    # 当前期望 0 个文件豁免;若未来需要 opt-out,在此处 explicit allow
    expected_exempt: set[str] = set()
    actual = {p.relative_to(SRC_ROOT).as_posix() for p in exempt}
    unexpected = actual - expected_exempt
    assert not unexpected, (
        f"未登记的豁免文件:{unexpected}。"
        f"如需新增,先更新 SPEC-4 §5 + 本测试 expected_exempt 集合"
    )
```

### 4.2 runtime guard 单测

```python
# tests/unit/test_playwright_guard.py
"""SPEC-4 §3:assert_sync_context 行为验证"""
import asyncio
import threading

import pytest

from autoteam._playwright_guard import assert_sync_context


def test_passes_in_plain_thread():
    """普通线程无 loop,guard 必须放行(不抛异常)"""
    holder: dict = {}

    def worker():
        try:
            assert_sync_context()
            holder["ok"] = True
        except Exception as e:
            holder["err"] = e

    t = threading.Thread(target=worker, name="test-plain")
    t.start()
    t.join(timeout=2.0)
    assert holder.get("ok") is True
    assert "err" not in holder


@pytest.mark.asyncio
async def test_blocks_in_asyncio_loop():
    """asyncio loop 内调用必须抛 RuntimeError"""
    with pytest.raises(RuntimeError, match=r"\[PlaywrightGuard\].*asyncio loop"):
        assert_sync_context()


def test_message_contains_thread_and_loop_id():
    """异常消息应携带 thread 名 + loop id 便于诊断"""
    async def runner():
        try:
            assert_sync_context()
        except RuntimeError as e:
            return str(e)
        return None

    msg = asyncio.run(runner())
    assert msg is not None
    assert "thread=" in msg
    assert "loop_id=0x" in msg
```

---

## 5. per-file 豁免机制

### 5.1 豁免格式(注释式 marker)

需要使用 `playwright.async_api` 的文件,**必须**在文件首 5 行内出现:

```python
# autoteam: allow-async-playwright reason="<具体原因,如 PR#NN 引入并发抓取>"
```

`reason="..."` 字段是**强制的**,无 reason 视为未豁免(`_is_exempt` 仅检测 marker 字面量,但 review 时人工把关 reason 完整性)。

### 5.2 豁免登记表

`tests/static/test_playwright_hygiene.py::test_exemption_list_intentional` 维护 `expected_exempt: set[str]`,新增豁免**必须同步更新**该集合,否则测试 fail。

当前期望值:`expected_exempt = set()` — **0** 个文件豁免。

### 5.3 豁免审批流程

1. 提案 PR 在该文件首加 marker
2. 在 `expected_exempt` 集合追加文件相对路径(`as_posix()` 形式)
3. PR 描述里链接到本 SPEC §5,说明 reason 与 review 决议
4. 至少 2 名维护者批准,理由不可"开发方便"或"暂时绕过"

---

## 6. CI 集成(pytest collect 配置)

### 6.1 `pyproject.toml`(若使用)

```toml
[tool.pytest.ini_options]
testpaths = [
    "tests/static",   # SPEC-4 静态守卫
    "tests/unit",     # SPEC-4 runtime guard + 项目既有单测
    "tests/integration",
]
addopts = "-ra -q --strict-markers"
asyncio_mode = "auto"  # 启用 pytest-asyncio,服务 §4.2
markers = [
    "static: SPEC-4 AST 守卫,不可 deselect",
]
```

### 6.2 `pytest.ini`(若使用,二选一)

```ini
[pytest]
testpaths = tests/static tests/unit tests/integration
addopts = -ra -q --strict-markers
asyncio_mode = auto
```

### 6.3 GitHub Actions / 本地预提交

```yaml
# .github/workflows/test.yml(片段)
- name: Static Playwright hygiene
  run: pytest tests/static -q
- name: Unit (incl. playwright guard)
  run: pytest tests/unit -q
```

CI 必须**单独**跑 `tests/static`,即便 unit 失败也要拿到静态结果(便于诊断)。

---

## 7. 反例验证(测试守卫真起作用)

`tests/static/test_playwright_hygiene_negative.py` — 以**临时文件**方式构造违例,验证 3 个测试函数确实 fail。该文件**不进 CI**,作为 SPEC-4 实施期的一次性手测。

```python
# tests/static/test_playwright_hygiene_negative.py
"""手测脚本:验证 SPEC-4 §4 的守卫能正确报错。
运行方式: pytest tests/static/test_playwright_hygiene_negative.py -q
默认 skip,需 SPEC4_NEGATIVE=1 环境变量启用。
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

SKIP_REASON = "set SPEC4_NEGATIVE=1 to run negative samples"
ENABLED = os.getenv("SPEC4_NEGATIVE") == "1"


@pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)
def test_async_def_with_sync_playwright_should_fail(tmp_path: Path, monkeypatch):
    """注入一段 async def + sync_playwright 的违规代码,期望主测试 fail"""
    bad_module = tmp_path / "bad.py"
    bad_module.write_text(textwrap.dedent("""\
        from playwright.sync_api import sync_playwright

        async def evil_handler():
            # 这里把 sync_playwright 拖进了协程,正是我们要拦的反模式
            with sync_playwright() as p:
                p.chromium.launch()
    """), encoding="utf-8")
    monkeypatch.setenv("SPEC4_SRC_OVERRIDE", str(tmp_path))
    # 用例期望:在主测试套(切换 SRC_ROOT 至 tmp_path 后)运行
    # test_no_playwright_in_async_def 应抛 AssertionError
    # 实施时:由 implementor 通过将 SRC_ROOT 改为 env-driven 完成此切换


@pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)
def test_async_api_import_should_fail(tmp_path: Path):
    bad = tmp_path / "evil.py"
    bad.write_text(
        "from playwright.async_api import async_playwright\n",
        encoding="utf-8",
    )
    # 期望:test_no_forbidden_module_import 在 SRC_ROOT=tmp_path 时 fail
```

> **注**:为保持主测文件无副作用,反例验证通过 `SPEC4_NEGATIVE=1` 与 `SPEC4_SRC_OVERRIDE` 双 env 触发;implementor 落地时需同步在 `_iter_py_files` / `SRC_ROOT` 加上 env 读取逻辑(`os.getenv("SPEC4_SRC_OVERRIDE", str(default))`),**不影响默认 CI 路径**。

---

## 8. 实施顺序(implementor 工序)

| Step | 操作 | 验证 |
|---|---|---|
| 1 | 新建 `src/autoteam/_playwright_guard.py`(§2 + §3.1) | `python -c "from autoteam._playwright_guard import assert_sync_context"` 无报错 |
| 2 | 在 `api.py:_PlaywrightExecutor` 双入口插入 `assert_sync_context()`(§3.2) | 既有 invite/register 集成测试全绿 |
| 3 | 上提 `manager.py:1491/1969` 函数内 import 至模块顶层 | `python -c "import autoteam.manager"` 无循环 import |
| 4 | 新建 `tests/static/__init__.py` + `test_playwright_hygiene.py`(§4.1) | `pytest tests/static -q` 全绿(因为现状 0 违规) |
| 5 | 新建 `tests/unit/test_playwright_guard.py`(§4.2) | `pytest tests/unit/test_playwright_guard.py -q` 全绿 |
| 6 | 更新 `pyproject.toml` / `pytest.ini`(§6) | `pytest --collect-only` 包含上述测试 |
| 7 | 跑全套 `pytest -q` | 0 fail,0 error |
| 8 | 反例手测(§7,可选) | 注入违规后 §4.1 三测必 fail |

**任一 step 失败必须停止,不允许跳过到下一步。**

---

## 9. 验收清单

- [ ] `_playwright_guard.py` 新增,`ALLOWED_SYNC_NAMES` 包含恰好 9 个符号
- [ ] `api.py` 在 `run_with_timeout` 入口调用 `assert_sync_context()`
- [ ] `api.py` 在 `_worker` 入口调用 `assert_sync_context()`
- [ ] `manager.py` grep `from playwright` 仅命中模块顶层 1 处(或保留豁免并登记理由)
- [ ] `tests/static/test_playwright_hygiene.py` 4 个测试全绿
- [ ] `tests/unit/test_playwright_guard.py` 3 个测试全绿
- [ ] `pyproject.toml` / `pytest.ini` `testpaths` 包含 `tests/static`
- [ ] 既有 invite / register / sync 集成测试 0 回归
- [ ] PR 描述链接 PRD-4 + SPEC-4,勾选"未引入 async playwright"

---

**附:与 PRD-4 §条款 → SPEC-4 §条款映射**

| PRD §  | SPEC § | 实施粒度 |
|---|---|---|
| §5.1 import 白名单 | §2 + §4.1 | 9 个符号常量 + AST 测试 |
| §5.2 runtime guard | §3 | 双入口 patch + 异常消息规范 |
| §5.3 AST 静态守卫 | §4.1 | 3 个测试 + 1 个豁免登记 |
| §5.4 CI 集成 | §6 | testpaths + asyncio_mode |
| §7.3 AST 示例 | §4.1 | 已上升为 production-ready |
| §12 R1 opt-out | §5 | marker + expected_exempt |
| §13 Q1(单点 vs 双入口) | §3.2 | 决议:双入口(主线程 + worker) |
