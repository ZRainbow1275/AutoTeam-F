# Issue#5 Playwright async/sync 一致性审计

> 调研时间:2026-04-26
> 调研范围:全项目 Playwright 使用面 + context7 拉取的官方 async/sync 边界
> 结论:**用户描述的"async 函数里大量误用 sync API"在 AutoTeam 源代码层不成立** — 项目根本没用 asyncio,sync_playwright 全部跑在普通同步线程里,完全合规

---

## A. Playwright 官方 API 对比(context7 输出摘要)

来源:`/websites/playwright_dev_python` + `/microsoft/playwright-python`(Context7 高分 doc)

### A.1 Sync API 关键函数

| API | 调用形式 | 入口 |
|---|---|---|
| `sync_playwright()` | 上下文管理器 / `.start()` | `from playwright.sync_api import sync_playwright` |
| `BrowserType.launch()` | 阻塞返回 `Browser` | `p.chromium.launch()` |
| `Browser.new_context()` | 阻塞返回 `BrowserContext` | `browser.new_context(...)` |
| `BrowserContext.new_page()` | 阻塞返回 `Page` | `context.new_page()` |
| `Page.goto / click / fill / locator / evaluate / wait_for_*` | 全阻塞,无 `await` | 同上 |

```python
# 官方 sync 范式
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("https://playwright.dev")
    print(page.title())
    browser.close()
```

### A.2 Async API 关键函数

| API | 调用形式 | 入口 |
|---|---|---|
| `async_playwright()` | `async with` / `await ....start()` | `from playwright.async_api import async_playwright` |
| `await BrowserType.launch()` | 协程 | `await p.chromium.launch()` |
| `await Browser.new_context()` | 协程 | |
| `await Page.goto / click / fill / evaluate / ...` | 全部协程 | |

```python
# 官方 async 范式
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto("https://playwright.dev")
        print(await page.title())
        await browser.close()

asyncio.run(main())
```

### A.3 不可混用的边界(官方明确警告)

> *"Playwright supports two variations of the API: synchronous and asynchronous. **If your modern project uses asyncio, you should use async API.**"*
> — playwright.dev/python/docs/library

> *"Playwright's API is not thread-safe. If you are using Playwright in a multi-threaded environment, you should create a Playwright instance per thread."*
> — playwright.dev/python/docs/library (Threading 小节)

**核心约束矩阵**:

| 调用上下文 | 允许 sync_playwright? | 允许 async_playwright? | 备注 |
|---|---|---|---|
| 普通 `def` 函数(无 asyncio loop) | ✅ | ❌ | 标准 sync 用法 |
| 子线程(无 loop) | ✅ 但**每线程独立实例** | ❌ | 不能跨线程共享 page/browser |
| `async def` 函数(asyncio loop 内) | ❌ **会抛 SyncBecauseInsideAsyncLoop** | ✅ | 这是用户描述的"那个错" |
| FastAPI `def` 路由 | ✅(FastAPI 把 sync 路由转线程池) | ❌ | 路由本身在 anyio 工作线程跑,不在主 loop |
| FastAPI `async def` 路由 | ❌ | ✅ | 直接在 loop 里 |
| `threading.Thread(target=fn)` 里的 fn | ✅ | ⚠ 需新建 loop | AutoTeam 当前用法 |

**典型错误信号**:
```
Error: It looks like you are using Playwright Sync API inside the asyncio loop.
Please use the Async API instead.
```
触发条件:`asyncio.run() / loop.run_until_complete()` 调用栈里出现 `sync_playwright()`。

---

## B. 全项目 Playwright 使用现状

### B.1 import 清单(完整,5 处)

| 文件 | 行 | 形式 | 上下文 |
|---|---|---|---|
| `src/autoteam/chatgpt_api.py` | 12 | 顶层 `from playwright.sync_api import sync_playwright` | 模块顶层 import |
| `src/autoteam/codex_auth.py` | 13 | 顶层 `from playwright.sync_api import sync_playwright` | 模块顶层 import |
| `src/autoteam/invite.py` | 24 | 顶层 `from playwright.sync_api import sync_playwright` | 模块顶层 import |
| `src/autoteam/manager.py` | 1491 | **函数内** `from playwright.sync_api import sync_playwright` | `_complete_registration` 里 |
| `src/autoteam/manager.py` | 1969 | **函数内** `from playwright.sync_api import sync_playwright` | `_register_direct_once` 里 |

```bash
$ grep -rn "from playwright" D:/Desktop/AutoTeam/src
src/autoteam/chatgpt_api.py:12: from playwright.sync_api import sync_playwright
src/autoteam/codex_auth.py:13:  from playwright.sync_api import sync_playwright
src/autoteam/invite.py:24:      from playwright.sync_api import sync_playwright
src/autoteam/manager.py:1491:       from playwright.sync_api import sync_playwright
src/autoteam/manager.py:1969:       from playwright.sync_api import sync_playwright

$ grep -rn "from playwright.async_api" D:/Desktop/AutoTeam
# (0 行 — 项目根本没引用 async_api)

$ grep -rn "async_playwright" D:/Desktop/AutoTeam
# (0 行)
```

### B.2 async / sync 各模块归属

#### B.2.1 `async def` 函数清单(完整,1 处)

```bash
$ grep -rn "^async def" D:/Desktop/AutoTeam/src
src/autoteam/api.py:35: async def auth_middleware(request: Request, call_next):
```

**唯一一处 `async def`** 是 FastAPI 鉴权中间件:
```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in _AUTH_SKIP_PATHS:
        return await call_next(request)
    if not API_KEY:
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("key", "")
    if token != API_KEY:
        return JSONResponse(status_code=401, content={"detail": "未授权,请提供有效的 API Key"})
    return await call_next(request)
```

**该函数体里没有 sync_playwright / async_playwright 任何引用** — 只是处理 Header 字符串、调 `call_next`。完全合规。

#### B.2.2 `async with` / `asyncio` 全项目搜索

```bash
$ grep -rn "async with"  D:/Desktop/AutoTeam/src     # 0 行
$ grep -rn "\basyncio\b" D:/Desktop/AutoTeam/src     # 0 行
$ grep -rn "import asyncio" D:/Desktop/AutoTeam      # 0 行(包括 tests)
$ grep -rn "loop\.run_until_complete" D:/Desktop/AutoTeam  # 0 行
$ grep -rn "asyncio\.run" D:/Desktop/AutoTeam        # 0 行
```

**项目压根没用 asyncio**。FastAPI 也是用 sync 路由,uvicorn 起 loop 但路由全跑在 anyio 工作线程。

#### B.2.3 FastAPI 路由全部是 sync `def`

```bash
$ grep -nE "^@app\.(get|post|put|delete|patch)" src/autoteam/api.py | wc -l
50

$ grep -nE "^async def (post|get|put|delete|patch|check|set|serve)" src/autoteam/api.py | wc -l
0
```

**50 个路由,0 个 async** — 全是 `def` 同步函数。FastAPI 会自动把 sync 路由调度到 anyio threadpool,不在 asyncio 主 loop 上执行,因此 `sync_playwright` 在路由里调用**完全合规**。

#### B.2.4 Playwright 调用上下文(全部 5 处)

| 文件:行 | 上下文 | 是否合规 |
|---|---|---|
| `chatgpt_api.py:118` | `class ChatGPTAPI._launch_browser` 实例方法,被 sync 路由 / 后台 thread 调用 | ✅ 合规 |
| `codex_auth.py:271` | `with sync_playwright() as p:` 在 sync `def` 函数 `_run_oauth_flow` 内 | ✅ 合规 |
| `invite.py:552` | `with sync_playwright() as p:` 在 sync `def register_with_invite` 内 | ✅ 合规 |
| `manager.py:1496` | `with sync_playwright() as p:` 在 sync `def _complete_registration` 内 | ✅ 合规 |
| `manager.py:1976` | `with sync_playwright() as p:` 在 sync `def _register_direct_once` 内 | ✅ 合规 |

**所有 5 处全部在 sync 函数中**,且这些函数最终被 `_PlaywrightExecutor` 专用线程或后台任务线程调用 — **不接触 asyncio loop**。

#### B.2.5 项目自身的 Playwright 线程模型(api.py:155-229)

`src/autoteam/api.py:173-227` 实现了一个 `_PlaywrightExecutor` 单例:

```python
class _PlaywrightExecutor:
    """将 Playwright 操作派发到专用线程执行,避免跨线程错误"""

    def __init__(self):
        self._queue: _queue.Queue = _queue.Queue()
        self._thread: threading.Thread | None = None

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None: break
            func, args, kwargs, result_event, result_holder = item
            try:
                result_holder["result"] = func(*args, **kwargs)
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_event.set()

    def run_with_timeout(self, timeout, func, *args, **kwargs):
        # 提交到专用线程,主线程 wait 结果
        self._queue.put((func, args, kwargs, result_event, result_holder))
        ...
```

加上 `_playwright_lock = threading.Lock()` 串行所有任务,这套**正是 Playwright 官方文档建议的"sync API + 单线程串行"模式** — 不仅没踩坑,反而是教科书级实现。

#### B.2.6 `page.evaluate(""" async () => { ... } """)` 的"假阳性"

这是审计里**最容易误判的点**:

```bash
$ grep -rn '"""async' D:/Desktop/AutoTeam/src
chatgpt_api.py:957:   data = self.page.evaluate("""async (accessToken) => {...}""")
chatgpt_api.py:1195:  result = self.page.evaluate("""async ([accountId, accessToken]) => {...}""")
chatgpt_api.py:1230:  result = self.page.evaluate("""async () => { const resp = await fetch(...); ... }""")
chatgpt_api.py:1287:  js_code = """async ([method, url, headers, body]) => {...}"""
api.py:709:           "async () => { const r = await fetch(...); ... }"
```

**这些 `async () => { ... }` 全部是字符串内容**,通过 `page.evaluate(js_string)` 注入到**浏览器侧 JavaScript 上下文**执行 — 不是 Python 协程。Python 这一侧 `page.evaluate(...)` 仍然是同步阻塞调用(返回 JS Promise resolve 后的 value)。

这是 sync_playwright 的标准用法 — 浏览器 `fetch()` 本来就是 JS Promise/async,在浏览器里写 `async/await` 才能 `await fetch()`,Python 这边等 evaluate 返回即可。

**完全合规,不是 bug。** 但用户口头描述"async 里用 sync API"很可能就是看到这种 evaluate 字符串误判的。

#### B.2.7 用户提到的不存在文件

用户原话:"重点文件:src/autoteam/chatgpt_session.py, codex_oauth.py, login_*.py, browser_manager.py 等"。

**实际盘点 src/autoteam/ 全部 .py 文件**:
```
__init__.py        account_ops.py    accounts.py       admin_state.py
api.py             auth_storage.py   cancel_signal.py  chatgpt_api.py
cloudmail.py       codex_auth.py     config.py         cpa_sync.py
display.py         identity.py       invite.py         manager.py
manual_account.py  register_failures.py  runtime_config.py
setup_wizard.py    textio.py         __main__.py
mail/{base.py, cf_temp_email.py, maillab.py}
```

**`chatgpt_session.py` / `browser_manager.py` / `login_*.py` 不存在**(可能是用户记错文件名,或者在描述其它项目)。最接近的是 `chatgpt_api.py` + `codex_auth.py` + `invite.py`,已在上文逐一审计。

---

## C. 误用清单(file:line + 当前 → 应改)

> **结果:0 处误用**

| # | file:line | 当前代码 | 应改为 | 优先级 |
|---|---|---|---|---|
| — | — | — | — | — |

经过全量扫描:
- 0 处 `async_playwright` 引用
- 0 处 `asyncio` import
- 0 处 `async with`
- 1 处 `async def`(`auth_middleware`,内部不调 playwright)
- 5 处 `sync_playwright` 调用,全部在 sync `def` 上下文 + 已有专用线程隔离

**没有需要改的位点**。

---

## D. 修复策略

### D.1 优先级

**Priority 0 - 不需要修复**

源代码层不存在 async/sync 混用。但有几条**预防性硬化**值得做(下面 D.2、D.3)。

### D.2 重构步骤(预防性,非必须)

虽然现状没问题,但有 3 项可选增强:

#### D.2.1 把 manager.py 的函数内 import 上提到模块顶层

```python
# 现状(manager.py:1491, 1969)
def _complete_registration(...):
    from playwright.sync_api import sync_playwright    # ← 函数内 import
    ...
def _register_direct_once(...):
    from playwright.sync_api import sync_playwright    # ← 函数内 import
    ...

# 建议(顶层 import,与其它 4 个文件风格一致)
from playwright.sync_api import sync_playwright

def _complete_registration(...):
    ...
def _register_direct_once(...):
    ...
```

收益:风格一致,IDE 跳转更顺,启动时一次性失败比"按按钮才发现 import 错"更好。
风险:可能因为循环 import 才被推到函数内的 — 需先 grep 确认 `manager.py` 不会被 `playwright` 间接 import 反向触发。

#### D.2.2 在 `_PlaywrightExecutor` 加官方文档式断言

```python
class _PlaywrightExecutor:
    def _worker(self):
        # 启动时打一句日志,便于运维确认线程隔离正常
        import threading
        logger.info("[Playwright] worker thread started: %s", threading.current_thread().name)
        ...
```

#### D.2.3 引入 `playwright_in_async_loop` 守卫(终极防御)

如果未来有人手贱写了 `async def` 路由 + 内部直接 `sync_playwright`,我们应当让它在第一次执行时炸响,而不是悄悄触发 `Sync API inside the asyncio loop`:

```python
# autoteam/_playwright_guard.py(新文件)
import asyncio
import functools
from playwright.sync_api import sync_playwright as _orig_sync_playwright

@functools.wraps(_orig_sync_playwright)
def sync_playwright():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        raise RuntimeError(
            "禁止在 asyncio loop 内调用 sync_playwright — "
            "改用 await asyncio.to_thread(...) 或重构为 async_playwright"
        )
    return _orig_sync_playwright()
```

然后在 5 个调用点统一 `from autoteam._playwright_guard import sync_playwright`。出现误用立刻抛错,堆栈一目了然。

### D.3 测试要点

#### D.3.1 静态守卫测试

```python
# tests/unit/test_no_playwright_in_async_def.py
import ast
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src"

def _walk(node, parent_async):
    if isinstance(node, ast.AsyncFunctionDef):
        parent_async = True
    if parent_async and isinstance(node, (ast.Call, ast.Attribute, ast.Name)):
        text = ast.unparse(node)
        if "sync_playwright" in text:
            yield node.lineno, text
    for child in ast.iter_child_nodes(node):
        yield from _walk(child, parent_async)

def test_no_sync_playwright_inside_async_def():
    bad = []
    for p in SRC.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for lineno, text in _walk(tree, False):
            bad.append(f"{p}:{lineno} {text}")
    assert not bad, "async def 函数内调用了 sync_playwright:\n" + "\n".join(bad)
```

#### D.3.2 运行时守卫测试

```python
# tests/unit/test_playwright_guard.py
import asyncio
import pytest

from autoteam._playwright_guard import sync_playwright

@pytest.mark.asyncio
async def test_sync_playwright_blocked_in_async_loop():
    with pytest.raises(RuntimeError, match="禁止在 asyncio loop"):
        sync_playwright()

def test_sync_playwright_works_in_sync_context():
    # 仅做 import / 调用前置检查不抛即可,不真启 browser
    pass
```

#### D.3.3 集成 smoke

`/api/tasks/fill` `/api/tasks/check` 等触发 sync_playwright 的端点已有现存测试;只需补一条:**确认从 FastAPI sync 路由到 _PlaywrightExecutor 的链路在 uvicorn loop 下不会抛 "Sync API inside asyncio loop"**。可以通过 `httpx.AsyncClient` 调一次 `/api/auth/check`(轻量端点)+ `/api/tasks/check` 完成。

---

## E. 回归风险

### E.1 当前不动代码的风险:0

不修任何代码 = 当前架构合规,无回归。

### E.2 应用 D.2 / D.3 的风险评估

| 改动 | 风险等级 | 主要风险点 |
|---|---|---|
| **D.2.1** 顶层 import | 🟡 低 | 循环 import 导致启动失败 — 需先 grep 验证 |
| **D.2.2** 启动日志 | 🟢 极低 | 多一行 log 而已 |
| **D.2.3** runtime guard | 🟡 中 | 必须 5 处全部替换 — 漏一处就回到老路;另需在 tests 里 unstub `_orig_sync_playwright` |
| **D.3.1** AST 静态测试 | 🟢 极低 | 误报概率低,纯静态 |
| **D.3.2** runtime guard 测试 | 🟢 低 | 需要 pytest-asyncio 依赖 |

### E.3 用户描述与现实的偏差

用户说"在 playwright 中大量出现一堆 async 里用 sync 的 api 的问题",**这描述与 AutoTeam 当前代码不符**。可能性:
1. 用户看的是**某次运行时报错日志**,但那段日志可能来自其他库(比如 anyio 内部、或 FastAPI 框架自身)
2. 用户把 `page.evaluate("""async () => {...}""")` 的 JS async 字符串误读成 Python async 函数
3. 用户看的是**别的项目** / 历史版本(本仓库 git log 也没出现过 async_playwright)
4. 用户准备好了"接下来要做的事"(比如想引入并发抓取),提前打预防针

**建议下一步**:让用户提供具体的报错堆栈或文件:行号 — 凭描述无法定位,代码层面是干净的。

---

## 总结

| 维度 | 结论 |
|---|---|
| 全项目 `async_playwright` 引用数 | **0** |
| 全项目 `asyncio` 引用数 | **0** |
| 全项目 `async def` 数 | **1**(FastAPI auth_middleware,与 playwright 无关) |
| 全项目 `sync_playwright` 调用数 | **5**,全部在 sync `def` + 专用线程 |
| 误用 / 混用位点 | **0** |
| 当前是否需要修复? | **❌ 不需要** |
| 建议增强(可选) | 顶层 import 统一 + runtime guard + AST 静态测试三连 |
| 用户描述与现实差距 | 可能误读 `page.evaluate("async () => ...")` JS 字符串;请提供具体堆栈定位 |
