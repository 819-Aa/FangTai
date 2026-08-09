"""不变量追踪矩阵校验（T04）。

校验 tests/contracts/invariant_traceability.yaml：
- 矩阵键必须精确等于 INV-001..INV-024（不得遗漏，不得多余）；
- 每条不变量至少有一个确定性校验点（module::node）和一个测试节点 ID；
- 每个测试节点 ID 必须存在于 pytest 收集结果中（不得悬空）。

任何违约返回具体错误码与退出码，不产生业务副作用。
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = REPO_ROOT / "tests" / "contracts" / "invariant_traceability.yaml"

EXPECTED = [f"INV-{i:03d}" for i in range(1, 25)]

EXIT_CODES = {
    "TEST_COLLECTION_FAILED": 10,
    "MATRIX_IO_ERROR": 11,
    "MATRIX_INCOMPLETE": 12,
    "MATRIX_INVALID": 13,
    "MATRIX_MISSING_RULE": 14,
    "MATRIX_MISSING_CHECKPOINT": 15,
    "MATRIX_MISSING_TEST": 16,
    "DANGLING_TEST_NODE": 17,
}


class TraceabilityError(Exception):
    """携带具体错误码的追踪矩阵违约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def collect_test_nodes() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise TraceabilityError("TEST_COLLECTION_FAILED", result.stderr[:500])
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def verify_matrix(matrix: dict[str, Any], test_nodes: set[str]) -> None:
    if not isinstance(matrix, dict):
        raise TraceabilityError("MATRIX_INVALID", "矩阵必须是 YAML 对象")
    keys = set(matrix)
    missing = [inv for inv in EXPECTED if inv not in keys]
    extra = [key for key in keys if key not in EXPECTED]
    if missing or extra:
        raise TraceabilityError("MATRIX_INCOMPLETE", f"缺失={missing}, 多余={extra}")

    for inv in EXPECTED:
        entry = matrix[inv]
        if not isinstance(entry, dict):
            raise TraceabilityError("MATRIX_INVALID", f"{inv} 必须是对象")
        if not entry.get("rule"):
            raise TraceabilityError("MATRIX_MISSING_RULE", inv)
        if not entry.get("checkpoints"):
            raise TraceabilityError("MATRIX_MISSING_CHECKPOINT", inv)
        tests = entry.get("tests") or []
        if not tests:
            raise TraceabilityError("MATRIX_MISSING_TEST", inv)
        for test_id in tests:
            if test_id not in test_nodes:
                raise TraceabilityError("DANGLING_TEST_NODE", f"{inv}: {test_id} 不存在于收集结果")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验不变量追踪矩阵。")
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX), help="矩阵 YAML 路径")
    args = parser.parse_args(argv)

    try:
        matrix = yaml.safe_load(Path(args.matrix).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"ERROR_CODE=MATRIX_IO_ERROR: {exc}", file=sys.stderr)
        return EXIT_CODES["MATRIX_IO_ERROR"]
    except yaml.YAMLError as exc:
        print(f"ERROR_CODE=MATRIX_IO_ERROR: YAML 解析失败: {exc}", file=sys.stderr)
        return EXIT_CODES["MATRIX_IO_ERROR"]

    try:
        test_nodes = collect_test_nodes()
        verify_matrix(matrix, test_nodes)
    except TraceabilityError as exc:
        print(f"ERROR_CODE={exc.code}: {exc}", file=sys.stderr)
        return EXIT_CODES[exc.code]

    print("TRACEABILITY_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
