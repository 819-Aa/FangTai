"""模块依赖方向静态检查（T04）。

依据 docs/contracts/module-boundaries.md §4/§10：
- 领域服务包（b2-b6、c1-c4、d1、d2、application、api_app）不得直接导入
  不属于自己的基础设施客户端（pymysql/redis/qdrant_client）；
- Agent/工作流包（c3）不得直接连接 MySQL/Qdrant/Redis；
- 模型节点不得直接导入基础设施客户端。
"""

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

#: 每个基础设施客户端只允许其拥有模块导入。
INFRA_OWNERS = {
    "pymysql": "application",
    "redis": "c4",
    "qdrant_client": "c1",
}

#: 在线运行时包（api_app 是单文件入口，单独扫描）。
RUNTIME_PACKAGES = [
    "b2", "b3", "b4", "b5", "b6",
    "c1", "c2", "c3", "c4",
    "d1", "d2", "application",
]


@dataclass(frozen=True)
class InfraImportViolation:
    file: str
    library: str
    detail: str

    def __str__(self) -> str:
        return f"{self.file} 导入 {self.detail}（{self.library} 仅允许 {INFRA_OWNERS.get(self.library)}）"


def _classify_import(module: str) -> str | None:
    """返回基础设施库名（pymysql/redis/qdrant_client）或 None。"""
    root = module.split(".")[0]
    return root if root in INFRA_OWNERS else None


def scan_file_infra_imports(py: Path) -> list[InfraImportViolation]:
    violations: list[InfraImportViolation] = []
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        for module in modules:
            library = _classify_import(module)
            if library is None:
                continue
            owner = INFRA_OWNERS[library]
            owner_dir = SRC / "food_agent_v2" / owner
            if owner_dir not in py.parents:
                violations.append(
                    InfraImportViolation(
                        py.relative_to(REPO_ROOT).as_posix(),
                        library,
                        module,
                    )
                )
    return violations


def scan_package_infra_imports(package_dir: Path) -> list[InfraImportViolation]:
    violations: list[InfraImportViolation] = []
    for py in package_dir.rglob("*.py"):
        violations.extend(scan_file_infra_imports(py))
    return violations


def scan_online_infra_imports() -> list[InfraImportViolation]:
    """扫描全部在线包目录 + api_app.py 单文件入口。"""
    violations: list[InfraImportViolation] = []
    for package in RUNTIME_PACKAGES:
        violations.extend(scan_package_infra_imports(SRC / "food_agent_v2" / package))
    api_app = SRC / "food_agent_v2" / "api_app.py"
    if api_app.exists():
        violations.extend(scan_file_infra_imports(api_app))
    return violations


class TestInfraOwnership:
    def test_domain_services_own_their_infrastructure(self) -> None:
        violations = scan_online_infra_imports()
        assert violations == [], f"领域模块直接导入不属于自己的基础设施客户端: {violations}"

    def test_agent_workflow_package_has_no_infra_imports(self) -> None:
        # Agent/工作流（c3）不得直接连接 MySQL/Qdrant/Redis。
        violations = scan_package_infra_imports(SRC / "food_agent_v2" / "c3")
        assert violations == [], f"c3 直接导入基础设施客户端: {violations}"

    def test_api_and_d2_have_no_infra_imports(self) -> None:
        # API/SSE 与回答/前端适配层不得直接连接基础设施（含 api_app.py 单文件入口）。
        violations: list[InfraImportViolation] = []
        for package in ("d1", "d2"):
            violations.extend(scan_package_infra_imports(SRC / "food_agent_v2" / package))
        violations.extend(scan_file_infra_imports(SRC / "food_agent_v2" / "api_app.py"))
        assert violations == [], f"API/前端适配层直接导入基础设施客户端: {violations}"
