"""零依赖门禁：白名单校验 / 只读沙箱 / 生成器不许 import 任何第三方包。

为什么值得为它单写一个 CI 步骤：

这个项目的核心价值是「安全层是可审计的纯逻辑」——
词法切分、标识符白名单、行数上限、只读沙箱。
它必须能被任何项目直接搬走：不绑 ORM、不绑 SQL 方言库、
不绑 Web 框架。

一旦核心层引入第三方包（比如 sqlglot），这条性质就没了，
而且退化是**无声**的：代码照跑、测试照绿，
直到有人想把这个模块拷到别处才发现拷不动 ——
或者更糟，等第三方库爆出 CVE 才发现深度耦合。

三层检查：

**1. 静态 AST 扫描**：顶层 ``import X`` 的 X 是否在标准库名单里。
**2. 动态导入**：子进程里装 meta-path finder 拦住所有非标准库模块，
    然后真的 ``import_module``（抓间接依赖）。
**3. 反向校验豁免名单**：被豁免的必须真的依赖第三方，
    否则豁免名单会膨胀到"所有人都被豁免"，规则名存实亡。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 核心模块。**这里不许出现 FastAPI / pydantic / sqlglot / sqlalchemy。**
CORE_MODULES: tuple[str, ...] = (
    "app",
    "app.schema",      # 表结构目录与名称解析
    "app.seed",        # 演示数据库生成
    "app.guard",       # SQL 安全层（词法 / 白名单 / 行数上限）
    "app.sandbox",     # SQLite 只读沙箱
    "app.generator",   # 规则 + LLM 生成器
    "app.pipeline",    # 编排
)

EXEMPT_MODULES: tuple[str, ...] = ("app.main",)

EXEMPT_MUST_IMPORT: dict[str, tuple[str, ...]] = {
    "app.main": ("fastapi", "pydantic"),
}

_STDLIB = frozenset(sys.stdlib_module_names)
_STDLIB |= {"__future__", "_thread", "nt", "posix", "os", "typing_extensions"}


def _emit(level: str, module: str, message: str) -> None:
    print(f"  [{level}] {module}: {message}")


def _is_third_party(name: str) -> bool:
    top = name.split(".")[0]
    return top not in _STDLIB and top != "app"


def _module_path(module: str) -> Path | None:
    base = ROOT / module.replace(".", "/")
    source = base.with_suffix(".py")
    if source.exists():
        return source
    init = base / "__init__.py"
    return init if init.exists() else None


# ---------------------------------------------------------------------------

def scan_static(module: str) -> list[str]:
    path = _module_path(module)
    if path is None:
        return [f"找不到模块文件 {ROOT / module.replace('.', '/')}.py"]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []

    def check(node: ast.Import | ast.ImportFrom, depth: int) -> None:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif node.level and node.level > 0:
            # 相对导入（``from .schema import X``）：AST 里 node.module 是
            # 去掉点号的裸名字，直接拿它会把 "schema" 当成第三方包
            return
        else:
            names = [node.module or ""]
        for name in names:
            if not name:
                continue
            if depth == 0 and _is_third_party(name):
                problems.append(f"第 {node.lineno} 行 import {name}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            check(node, _node_depth(tree, node))
    return problems


def _node_depth(tree: ast.Module, target: ast.AST) -> int:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            for child in ast.walk(node):
                if child is target:
                    return 1
    return 0


_CHILD = r'''
import sys, importlib
target = sys.argv[1]

class Blocker:
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top in sys.stdlib_module_names or top in ("app", "__future__"):
            return None
        raise ImportError(f"被拦截：核心层不允许引入第三方包 {name!r}")

sys.meta_path.insert(0, Blocker())
importlib.import_module(target)
print("OK")
'''


def check_dynamic(module: str) -> str | None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, module],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        for line in reversed(lines):
            if "Error" in line or "被拦截" in line:
                return line.strip()
        return lines[-1] if lines else f"退出码 {proc.returncode}"
    return None


def verify_exemptions() -> list[str]:
    problems: list[str] = []
    for module, must in EXEMPT_MUST_IMPORT.items():
        path = _module_path(module)
        if path is None:
            problems.append(f"找不到豁免模块 {module}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for package in must:
            if package not in imported:
                problems.append(
                    f"{module} 被豁免却没有 import {package} —— "
                    "豁免名单不该留着过期的条目")
    return problems


def main() -> int:
    print("=" * 70)
    print("零依赖门禁 · text2sql core modules")
    print("=" * 70)
    failures = 0

    print(f"\n[1/3] 静态扫描 {len(CORE_MODULES)} 个核心模块")
    print("-" * 70)
    for module in CORE_MODULES:
        problems = scan_static(module)
        if problems:
            for p in problems:
                _emit("FAIL", module, p)
            failures += 1
        else:
            _emit("ok", module, "顶层 import 全部来自标准库")
        if module in EXEMPT_MUST_IMPORT:
            _emit("WARN", module, "既是核心模块又在豁免名单里，逻辑冲突")

    print("\n[2/3] 动态导入验证（拦截第三方包）")
    print("-" * 70)
    for module in CORE_MODULES:
        error = check_dynamic(module)
        if error:
            _emit("FAIL", module, error)
            failures += 1
        else:
            _emit("ok", module, "在纯净环境里可导入")

    print(f"\n[3/3] 反向校验 {len(EXEMPT_MODULES)} 个豁免模块")
    print("-" * 70)
    exempt_problems = verify_exemptions()
    for p in exempt_problems:
        _emit("FAIL", "exemptions", p)
        failures += 1
    if not exempt_problems:
        for module in EXEMPT_MODULES:
            _emit("ok", module, "确实依赖第三方，豁免成立")

    print("\n" + "=" * 70)
    if failures:
        print(f"✗ {failures} 项不合格 —— 核心层必须是零第三方依赖的")
    else:
        print(f"✓ 通过：{len(CORE_MODULES)} 个核心模块零第三方依赖，"
              f"{len(EXEMPT_MODULES)} 个豁免模块理由成立")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
