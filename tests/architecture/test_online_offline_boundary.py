"""INV-023 在线/离线静态边界守卫（T04）。

在线模块（b2-b6、c1-c4、d1、d2、application、api_app）不得：
- 导入 B1 解析模块（recipe_cleaning / ingredient_identity / ingredient_parser / source_manifest）；
- 在运行时代码中引用 data/raw、data/processed 或 *.jsonl。

整改期间的历史违规登记在 KNOWN_VIOLATIONS（每条标注消除任务）。守卫保证：
- 检测能力真实且自包含（test_scanner_detects_* 用合成源码验证，不依赖待修复违规）；
- 检测集合 ⊆ 登记集合（新增违规立即失败）；
- 登记集合 ⊆ 检测集合（代码消除后必须同步删除登记，防 stale）。
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

#: B1 中仅离线解析器，在线模块禁止导入。
FORBIDDEN_B1_MODULES = {"recipe_cleaning", "ingredient_identity", "ingredient_parser", "source_manifest"}

#: 在线代码禁止直接引用离线文件。
FORBIDDEN_FILE_RE = re.compile(r"(?:data[/\\](?:raw|processed))|\.jsonl")

#: 在线运行时包（b1 属于离线数据工程，排除在扫描之外）。
ONLINE_PACKAGES = [
    "b2", "b3", "b4", "b5", "b6",
    "c1", "c2", "c3", "c4",
    "d1", "d2", "application",
]

#: 已登记的历史边界违规 -> 消除任务。实现任务消除后必须同步删除对应条目。
KNOWN_VIOLATIONS: dict[str, str] = {
    "src/food_agent_v2/b5/__init__.py::file_ref::time_profiles.jsonl": "T13",
    "src/food_agent_v2/b6/__init__.py::file_ref::nutrition_profiles.jsonl": "T13",
    "src/food_agent_v2/c1/__init__.py::file_ref::rag_documents.jsonl": "T14",
    "src/food_agent_v2/c1/__init__.py::file_ref::time_profiles.jsonl": "T14",
    "src/food_agent_v2/c1/qdrant_client.py::file_ref::rag_documents.jsonl": "T14",
}


@dataclass(frozen=True)
class BoundaryViolation:
    file: str
    kind: str
    detail: str

    def signature(self) -> str:
        return f"{self.file}::{self.kind}::{self.detail}"


def _collect_docstrings(tree: ast.AST) -> set[str]:
    """收集模块/类/函数 docstring 的原始字符串，扫描时按整串精确跳过。"""
    docstrings: set[str] = set()
    for node in [tree, *ast.walk(tree)]:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return docstrings


def _is_forbidden_b1_import(module: str, names: tuple[str, ...]) -> bool:
    """识别全部 b1 解析器导入形式。"""
    if not module.startswith("food_agent_v2.b1"):
        return False
    parts = module.split(".")
    if len(parts) > 2:
        return parts[2] in FORBIDDEN_B1_MODULES
    # from food_agent_v2.b1 import recipe_cleaning
    return any(name in FORBIDDEN_B1_MODULES for name in names)


def scan_source_text(source: str, rel_path: str) -> list[BoundaryViolation]:
    """扫描一段 Python 源码文本中的边界违规（忽略 docstring/注释）。"""
    tree = ast.parse(source)
    docstrings = _collect_docstrings(tree)
    violations: list[BoundaryViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue  # docstring 不是运行时文件引用
            if FORBIDDEN_FILE_RE.search(node.value):
                violations.append(BoundaryViolation(rel_path, "file_ref", node.value))
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_b1_import(alias.name, ()):
                    violations.append(BoundaryViolation(rel_path, "import", alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = tuple(alias.name for alias in node.names)
            if _is_forbidden_b1_import(node.module, names):
                detail = (
                    node.module
                    if len(node.module.split(".")) > 2
                    else f"{node.module}:{','.join(names)}"
                )
                violations.append(BoundaryViolation(rel_path, "import", detail))
    return violations


def scan_module_source(path: Path) -> list[BoundaryViolation]:
    """扫描单个运行时代码文件。"""
    return scan_source_text(path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT).as_posix())


def scan_online_modules(root: Path = SRC) -> list[BoundaryViolation]:
    """扫描全部在线包目录 + api_app.py 单文件入口。"""
    violations: list[BoundaryViolation] = []
    for package in ONLINE_PACKAGES:
        package_dir = root / "food_agent_v2" / package
        for py in package_dir.rglob("*.py"):
            violations.extend(scan_module_source(py))
    api_app = root / "food_agent_v2" / "api_app.py"
    if api_app.exists():
        violations.extend(scan_module_source(api_app))
    return violations


class TestOnlineOfflineBoundary:
    def test_scanner_detects_b1_parser_import_from_submodule(self) -> None:
        hits = scan_source_text(
            "from food_agent_v2.b1.ingredient_identity import normalize\n",
            "synthetic.py",
        )
        assert any(hit.kind == "import" and "ingredient_identity" in hit.detail for hit in hits)

    def test_scanner_detects_b1_parser_import_from_package(self) -> None:
        hits = scan_source_text(
            "from food_agent_v2.b1 import recipe_cleaning\n",
            "synthetic.py",
        )
        assert any(hit.kind == "import" and "recipe_cleaning" in hit.detail for hit in hits)

    def test_scanner_detects_b1_parser_import_statement(self) -> None:
        hits = scan_source_text(
            "import food_agent_v2.b1.ingredient_parser\n",
            "synthetic.py",
        )
        assert any(hit.kind == "import" for hit in hits)

    def test_scanner_detects_data_file_reference(self) -> None:
        hits = scan_source_text(
            "path = CLEANED_DIR / 'time_profiles.jsonl'\n",
            "synthetic.py",
        )
        assert any(hit.kind == "file_ref" and "time_profiles.jsonl" in hit.detail for hit in hits)

    def test_scanner_detects_raw_data_reference(self) -> None:
        hits = scan_source_text(
            "open('data/raw/recipes_sample_2000.csv')\n",
            "synthetic.py",
        )
        assert any(hit.kind == "file_ref" for hit in hits)

    def test_scanner_ignores_docstring_mention(self) -> None:
        hits = scan_source_text(
            '"""消费 B1 离线生成的 time_profiles.jsonl。"""\n',
            "synthetic.py",
        )
        assert hits == []

    def test_no_new_online_offline_violations(self) -> None:
        detected = scan_online_modules()
        unknown = [v.signature() for v in detected if v.signature() not in KNOWN_VIOLATIONS]
        assert unknown == [], f"新增在线/离线边界违规: {unknown}"

    def test_known_violations_still_present(self) -> None:
        detected = {v.signature() for v in scan_online_modules()}
        stale = [sig for sig in KNOWN_VIOLATIONS if sig not in detected]
        assert stale == [], f"代码已消除但登记表未更新: {stale}"

    def test_known_violations_have_fix_task(self) -> None:
        for signature, fix_task in KNOWN_VIOLATIONS.items():
            assert fix_task, f"{signature} 缺少消除任务"
