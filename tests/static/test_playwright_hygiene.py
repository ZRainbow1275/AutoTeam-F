"""SPEC-4 §4: Playwright import + async/sync 分离静态守卫。

通过 AST 扫描 src/autoteam/**/*.py,断言:
  - 不出现 from playwright.async_api ...
  - async def 函数体内不引用任何 playwright 符号
  - playwright.sync_api 导入仅限白名单
  - 已豁免的文件必须显式登记在 expected_exempt 集合
"""

from __future__ import annotations

import ast
from pathlib import Path

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
    expected_exempt: set[str] = set()
    actual = {p.relative_to(SRC_ROOT).as_posix() for p in exempt}
    unexpected = actual - expected_exempt
    assert not unexpected, (
        f"未登记的豁免文件:{unexpected}。"
        f"如需新增,先更新 SPEC-4 §5 + 本测试 expected_exempt 集合"
    )
