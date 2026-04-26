# PRD-4: Playwright async/sync 一致性硬化(hardening only)

## 0. 元数据

| 字段 | 值 |
|---|---|
| PRD 编号 | PRD-4 |
| 主题 | Playwright async/sync 一致性硬化(防御性) |
| 关联 Issue | #5(待用户补样本) |
| 来源研究 | `prompts/0426/research/issue-5-playwright-async-sync.md` |
| 优先级 | P2(低,纯防御,不阻塞 P0/P1) |
| 主笔 | prd-playwright |
| 创建日期 | 2026-04-26 |
| 状态 | DRAFT |

---

## 1. 背景

用户在 Issue#5 报告"playwright 中大量 async 函数里误用 sync API"。研究阶段对全项目 5 处 `from playwright.sync_api import sync_playwright` 调用点 + 1 处 `async def`(`api.py:35 auth_middleware`,与 playwright 无关) 完整审计后**结论反转**:

- **0** 处 `async_playwright` 引用
- **0** 处 `asyncio` import / `async with` / `loop.run_until_complete`
- **5** 处 `sync_playwright` 调用全部位于 sync `def` 上下文,且经由 `api.py:155-229 _PlaywrightExecutor` 单例 + 专用 worker 线程串行,严格符合 Playwright 官方 "sync API + 单线程" 推荐模式
- 用户极可能将 `page.evaluate("""async () => {...}""")` 内的 JS 字符串误读为 Python 协程

**用户决策(2026-04-26)**:走 "hardening only" — 不修业务逻辑、不重构现有调用,只新增防御性代码,以**永久杜绝未来回归**。

## 2. 目标

| # | 目标 | 衡量 |
|---|---|---|
| G1 | 全项目 playwright 导入收敛到一处规范的 sync API 白名单 | grep 命中数与白名单一致 |
| G2 | 即使未来误把 `sync_playwright` 植入 `async def`,也能在第一次执行时立即抛错(而非触发 Playwright 内部隐式失败) | guard 单测通过 |
| G3 | CI 阶段静态拒绝 `async def` 函数体内出现 playwright 名称、拒绝 `from playwright.async_api ...` | pytest collect 包含 `tests/static/test_no_async_playwright.py` 且通过 |
| G4 | 提供清晰的 opt-out 出口,未来如确需引入 async_playwright 不被守卫卡死 | 风险登记册中明示 opt-out 路径 |

## 3. 非目标(明确不做)

- **不**重构 `_PlaywrightExecutor` / `chatgpt_api.py` / `codex_auth.py` / `invite.py` / `manager.py` 的任何业务逻辑
- **不**切换到 `async_playwright`(即便未来 FastAPI 改用 async 路由)
- **不**修改 5 处 `sync_playwright` 调用点的语义(本 PRD 仅做 import 路径重写)
- **不**引入新依赖(纯标准库 `ast` + `asyncio`)
- **不**把守卫扩展到 JS 端 `page.evaluate("async () => ...")` 字符串(那是合法 JS,与 Python async 无关)

## 4. 用户故事

### 4.1 运维(operator)

> 作为运维,我希望服务上线后**永远不会**因为某次重构把 `sync_playwright` 误植入 asyncio loop 而无人察觉地"挂机 30s 后报错",我希望第一次错误调用就立即崩溃并打印出清晰的中文错误堆栈,这样我能在生产前的 smoke 中就发现回归。

### 4.2 开发者(contributor)

> 作为新加入的开发者,我希望仅看一眼 import 列表就能确定本仓库**只用 sync API**;我也希望提交 PR 时 CI 能在任何不慎引入 `async_playwright` 或 async-def 内调用 sync API 的代码时立即 fail,而不是在我提了 PR 评审之后才被 reviewer 抓出来。

## 5. 功能需求

### 5.1 顶层 import 统一规范(FR-1)

**规范**:

- 仅允许从 `playwright.sync_api` 导入,白名单:`sync_playwright`, `Page`, `Browser`, `BrowserContext`, `BrowserType`, `Locator`, `Error`, `TimeoutError`, `Playwright`(共 9 个,覆盖现有用法 + 后续可能增量)
- 全部 import 必须在**模块顶层**,不允许函数内 import(对照研究 §B.1 — `manager.py:1491/1969` 现有函数内 import 必须上提)
- 任何文件出现 `from playwright.async_api import ...` 即 CI fail

**实施动作**:

1. 把 `manager.py` 函数内的两处 `from playwright.sync_api import sync_playwright` 移到模块顶层(若引发循环 import,请改为 `if TYPE_CHECKING:` + 函数内调用本地导入,但**该决定必须在 PR 描述里说明原因**)
2. 编写 `tests/static/test_playwright_imports.py` 校验白名单

### 5.2 `_PlaywrightExecutor` 运行时 guard(FR-2)

**规范**:

在 `api.py:_PlaywrightExecutor.run_with_timeout` 入口(以及 `_worker` 线程入口) **额外**插入一段 guard:

```python
import asyncio
def _assert_no_running_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 期望路径:无 loop
    raise RuntimeError(
        "[PlaywrightGuard] 检测到 sync_playwright 被调度进 asyncio loop;"
        "本项目仅支持 sync API + 专用线程模型。"
        "若确需 async 调用,请走 PRD-4 §12 opt-out 流程。"
    )
```

- `run_with_timeout` 入口断言一次(主线程入队前)
- `_worker` 启动时再断言一次(worker 线程是新线程,正常情况下应无 loop)
- 失败立即 raise,不要 swallow

### 5.3 AST 静态守卫单测(FR-3)

**规范**:

新建 `tests/static/test_no_async_playwright.py`,使用 `ast` 标准库扫 `src/autoteam/**/*.py`,断言:

| 断言 | 说明 |
|---|---|
| A1 | 任何 `from playwright.async_api import ...` 直接 fail |
| A2 | 任何 `import playwright.async_api` 直接 fail |
| A3 | 任何 `async def` 函数体内不出现 `sync_playwright` / `async_playwright` 名称、`Playwright`、`Browser`、`Page`、`BrowserContext` 等 9 个白名单符号(用变量遮蔽视为可疑) |
| A4 | 顶层 `from playwright.sync_api import X, Y, ...` 中 X/Y 必须在白名单内 |

**例外**:`tests/` 目录本身允许 async 测试(pytest-asyncio),但仍**不允许**在 async 测试体内调用 playwright(若未来需要,可在测试代码中单点豁免并加注释)

### 5.4 CI 集成(FR-4)

- 把 `tests/static/test_no_async_playwright.py` + `tests/static/test_playwright_imports.py` 加入 `pyproject.toml` / `pytest.ini` 默认 collect 路径
- 不允许默认 deselect / skip
- 在 PR 模板里加一行勾选项:"已确认未引入 async playwright 调用"

## 6. 非功能需求

| 维度 | 要求 |
|---|---|
| 性能 | guard 在 `run_with_timeout` 入口的开销 ≤ 50µs(`asyncio.get_running_loop()` 单次调用) |
| 兼容性 | 必须在 Python 3.10+ 工作(项目当前最低版本) |
| 可维护性 | guard 实现集中在一个文件 `src/autoteam/_playwright_guard.py`,5 处调用点零侵入 |
| 可观测性 | guard 失败时打印当前线程名 + asyncio loop id,便于诊断 |
| 文档影响 | 仅 `CONTRIBUTING.md`(若不存在则不写)增加 5 行说明 |

## 7. 技术方案

### 7.1 import 规范定义

新建 `src/autoteam/_playwright_guard.py`:

```python
"""Playwright import 守卫与白名单(PRD-4)"""

ALLOWED_SYNC_NAMES = {
    "sync_playwright", "Page", "Browser", "BrowserContext",
    "BrowserType", "Locator", "Error", "TimeoutError", "Playwright",
}
FORBIDDEN_MODULES = {"playwright.async_api"}
```

`tests/static/test_playwright_imports.py` 直接 import 该常量做断言,实现单一信源。

### 7.2 guard 实现细节

```python
# src/autoteam/_playwright_guard.py
import asyncio
import threading

def assert_sync_context() -> None:
    """在调用 sync_playwright 之前确认无 asyncio loop;否则抛 RuntimeError"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"[PlaywrightGuard] thread={threading.current_thread().name} "
        f"loop_id={id(loop)} — sync_playwright 不允许在 asyncio loop 中调用。"
        f"opt-out: 见 PRD-4 §12。"
    )
```

`api.py` 调用点:

```python
# 入队前
class _PlaywrightExecutor:
    def run_with_timeout(self, timeout, func, *args, **kwargs):
        from autoteam._playwright_guard import assert_sync_context
        assert_sync_context()
        ...
    def _worker(self):
        from autoteam._playwright_guard import assert_sync_context
        assert_sync_context()
        ...
```

### 7.3 AST 测试实现示例(可运行,30+ 行)

```python
# tests/static/test_no_async_playwright.py
"""PRD-4 FR-3:静态守卫,禁止 async_playwright + 禁止 async def 内部 playwright 调用"""
import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "autoteam"
ALLOWED_SYNC_NAMES = {
    "sync_playwright", "Page", "Browser", "BrowserContext",
    "BrowserType", "Locator", "Error", "TimeoutError", "Playwright",
}
FORBIDDEN_MODULES = {"playwright.async_api"}
PLAYWRIGHT_NAMESPACE = ALLOWED_SYNC_NAMES | {"async_playwright"}


def _iter_py_files():
    yield from SRC_ROOT.rglob("*.py")


def _collect_async_func_bodies(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for child in ast.walk(node):
                yield node, child


def test_no_forbidden_module_import():
    bad = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
                bad.append(f"{path}:{node.lineno} from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES:
                        bad.append(f"{path}:{node.lineno} import {alias.name}")
    assert not bad, "禁止使用 playwright.async_api:\n" + "\n".join(bad)


def test_no_playwright_in_async_def():
    bad = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn, child in _collect_async_func_bodies(tree):
            name = getattr(child, "id", None) or getattr(child, "attr", None)
            if name in PLAYWRIGHT_NAMESPACE:
                bad.append(f"{path}:{child.lineno} async def {fn.name} 内引用了 {name}")
    assert not bad, "async def 函数体内不允许出现 playwright 符号:\n" + "\n".join(bad)


def test_sync_import_whitelist():
    bad = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "playwright.sync_api":
                for alias in node.names:
                    if alias.name not in ALLOWED_SYNC_NAMES:
                        bad.append(f"{path}:{node.lineno} 未在白名单的符号 {alias.name}")
    assert not bad, "playwright.sync_api 导入不在白名单:\n" + "\n".join(bad)
```

## 8. 验收标准

| AC# | 描述 | 验证方式 |
|---|---|---|
| AC1 | `pytest tests/static/` 全绿 | CI |
| AC2 | 在 `chatgpt_api.py` 临时插入 `async def evil(): sync_playwright()` 应使 AC1 失败 | 人工注入 + 还原 |
| AC3 | 在 `auth_middleware` 内手工调一次 `_PlaywrightExecutor().run_with_timeout(...)` 应抛 `[PlaywrightGuard]` | 人工注入 + 还原 |
| AC4 | `manager.py` 的 2 处函数内 import 已上提 | grep `from playwright` 在 manager.py 仅模块顶层 1 处 |
| AC5 | `pyproject.toml` / `pytest.ini` 包含 `tests/static/` | diff 校验 |

## 9. 测试计划

| 测试 | 类型 | 触发 |
|---|---|---|
| `test_no_async_playwright.py::test_no_forbidden_module_import` | 静态 | pytest |
| `test_no_async_playwright.py::test_no_playwright_in_async_def` | 静态 | pytest |
| `test_no_async_playwright.py::test_sync_import_whitelist` | 静态 | pytest |
| `test_playwright_guard.py::test_runtime_guard_blocks_in_loop` | 运行时(asyncio) | pytest-asyncio |
| `test_playwright_guard.py::test_runtime_guard_passes_in_thread` | 运行时(threading) | pytest |
| 既有 invite/register/sync 集成测试 | 回归 | 整套 pytest |

回归预期:**全部既有测试应保持绿色**(本 PRD 不动业务逻辑)。

## 10. 灰度 / 回滚

- **灰度**:不需要(纯防御代码,无运行时分支切换)
- **回滚**:删除 `tests/static/` + 移除 `assert_sync_context` 调用即可,无数据迁移
- **影响范围**:0(运行路径无 loop,guard 永不触发;静态测试不进生产)

## 11. 文档影响清单

- `CONTRIBUTING.md`(若已存在):新增"Playwright 使用守则"小节,5 行
- 不修改 `docs/`、`README.md`、`AGENTS.md`(本 PRD 是开发期硬化,不面向终端用户)

## 12. 风险登记册

| # | 风险 | 等级 | 缓解措施 |
|---|---|---|---|
| R1 | guard 误伤合法 async 调用(未来确需 async_playwright 接并发抓取) | 中 | **opt-out 流程**:在新模块内显式 `from playwright.async_api import async_playwright as _async_playwright_optout`,并在该文件顶部加注释 `# PRD-4 OPT-OUT: <reason>`;同时在 `test_no_async_playwright.py` 的 FORBIDDEN_MODULES 检查中加 per-file 豁免列表(显式 allowlist,需 PR 评审通过) |
| R2 | `manager.py` 函数内 import 上提引发循环 import | 低 | 实施前用 `python -c "import autoteam.manager"` 验证;若爆掉则保留函数内 import 并在静态测试中加 per-file 豁免 |
| R3 | AST 静态测试在 Windows 上路径处理出错 | 低 | 用 `Path.rglob` + `read_text(encoding="utf-8")`,已处理 |
| R4 | guard 单测引入 `pytest-asyncio` 新依赖 | 低 | 已在 dev 依赖中(若没有则添加 `pytest-asyncio>=0.23` 到 `pyproject.toml [project.optional-dependencies].dev`) |
| R5 | 用户描述的"async/sync 混用"实际是其它问题(如 anyio 内部错误) | 中 | 本 PRD 不解决该问题,只防回归;Issue#5 保持 open 等用户补样本 |

## 13. 未决问题

1. **Q1**:是否需要把 guard 扩展到所有 `_PlaywrightExecutor` 之外的直接 `sync_playwright()` 调用点(`codex_auth.py:271`、`invite.py:552`、`manager.py:1496/1976`)?
   - 当前提案:**仅在 `_PlaywrightExecutor` 入口加**,因为这 4 处调用点最终都是经由 executor 调度;若未来某调用点绕过 executor 直接调,应在评审阶段拒绝
   - 备选:每个 `with sync_playwright()` 前加 `assert_sync_context()`(代码侵入度更高)
2. **Q2**:`CONTRIBUTING.md` 不存在时是否新建?(用户偏好"优先编辑现有文件" — 倾向不新建)
3. **Q3**:R1 的 opt-out 豁免列表落在哪个文件?(候选:`pyproject.toml [tool.autoteam.playwright_guard].async_optout_files`)

## 14. Story Map

```
Sprint 0(本 PRD)
├── Story 1: 新建 _playwright_guard.py(白名单常量 + assert_sync_context)
├── Story 2: 在 _PlaywrightExecutor 双入口插入 guard
├── Story 3: 上提 manager.py 两处函数内 import(若不引发循环 import)
├── Story 4: 编写 tests/static/test_no_async_playwright.py
├── Story 5: 编写 tests/static/test_playwright_imports.py
├── Story 6: 编写 tests/unit/test_playwright_guard.py(asyncio loop 内/外)
├── Story 7: 把 tests/static/ 加入 pytest.ini collect 路径
└── Story 8: 跑全套 pytest,确认无回归

实施时间预估:0.5 人日
回归测试:已有套件 + 新增 5 个测试,2 小时
评审重点:R1 的 opt-out 设计是否清晰
```

---

**附:与研究文档(issue-5-playwright-async-sync.md)的对应关系**

| 研究章节 | 本 PRD 章节 |
|---|---|
| §A 官方 API 边界 | §1 背景 |
| §B 全项目使用现状 | §1 背景 + §5.1 |
| §C 误用清单(0 处) | §1 + §3 |
| §D.2.1 顶层 import | §5.1 + §7.1 |
| §D.2.3 runtime guard | §5.2 + §7.2 |
| §D.3.1 AST 静态测试 | §5.3 + §7.3 |
| §D.3.2 runtime guard 测试 | §9 |
| §E.3 用户描述差距 | §1 + R5 |
