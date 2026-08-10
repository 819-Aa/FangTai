"""T11 B3 不再解析原始食材字符串 / 不再读取离线文件（INV-023）。

B3 运行时代码（recipe_views / identity_resolver / repository）不得导入
B1 或 b3 解析器，不得引用 data/raw|data/processed|*.jsonl。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
B3 = REPO_ROOT / "src" / "food_agent_v2" / "b3"

_RUNTIME_FILES = (
    B3 / "recipe_views.py",
    B3 / "identity_resolver.py",
    B3 / "repository.py",
)


class TestB3NeverParsesRaw:
    def test_no_parser_imports(self) -> None:
        for py in _RUNTIME_FILES:
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "ingredient_parser" in node.module or "food_agent_v2.b1" in node.module:
                        raise AssertionError(f"{py.name} 导入解析器: {node.module}")

    def test_no_data_file_references(self) -> None:
        for py in _RUNTIME_FILES:
            text = py.read_text(encoding="utf-8")
            for forbidden in (".jsonl", "data/raw", "data/processed"):
                assert forbidden not in text, f"{py.name} 引用离线文件: {forbidden}"

    def test_b3_ingredient_parser_not_used_by_runtime(self) -> None:
        for py in _RUNTIME_FILES:
            assert "ingredient_parser" not in py.read_text(encoding="utf-8"), py.name

    def test_repository_is_only_data_entry(self) -> None:
        # 运行时代码只能从 b3.repository 获取固定事实。
        for py in (B3 / "recipe_views.py", B3 / "identity_resolver.py"):
            text = py.read_text(encoding="utf-8")
            assert "repository" in text, f"{py.name} 未使用 Repository"
