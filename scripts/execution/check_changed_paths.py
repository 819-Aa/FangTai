"""变更路径白名单校验（T01）。

拒绝落在 forbidden 黑名单的路径，并强制全部变更落在 allowed 白名单内。
forbidden 优先于 allowed：锁定契约、测试、计划和任务信封即使出现在 allowed
中也必须被拒绝。

错误码（见 EXIT_CODES）：
- CHANGED_PATH_FORBIDDEN        命中 forbidden 黑名单
- CHANGED_PATH_OUT_OF_ALLOWED   超出 allowed 白名单
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

EXIT_CODES = {
    "CHANGED_PATH_FORBIDDEN": 21,
    "CHANGED_PATH_OUT_OF_ALLOWED": 22,
}


class PathPolicyError(Exception):
    """携带具体错误码的路径策略违约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _pattern_matches(path: str, pattern: str) -> bool:
    """glob 模式匹配，支持 **、*、?；** 可跨目录分隔符。"""
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/"):
        pattern += "**"
    if not any(ch in pattern for ch in "*?["):
        return path == pattern or path.startswith(pattern + "/")
    regex = "^" + re.escape(pattern)
    regex = regex.replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    regex += "$"
    return re.match(regex, path) is not None


def check_changed_paths(
    changed: Sequence[Path],
    allowed: Sequence[str],
    forbidden: Sequence[str],
) -> None:
    """校验变更路径。任何违规抛 PathPolicyError，不返回值。"""
    for item in changed:
        path = Path(str(item)).as_posix()
        if any(_pattern_matches(path, pattern) for pattern in forbidden):
            raise PathPolicyError("CHANGED_PATH_FORBIDDEN", f"{path} 命中 forbidden 黑名单")
        if not any(_pattern_matches(path, pattern) for pattern in allowed):
            raise PathPolicyError("CHANGED_PATH_OUT_OF_ALLOWED", f"{path} 超出 allowed 白名单")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拒绝超出允许集合的变更路径。")
    parser.add_argument("--changed", action="append", required=True, metavar="PATH")
    parser.add_argument("--allowed", action="append", required=True, metavar="PATTERN")
    parser.add_argument("--forbidden", action="append", default=[], metavar="PATTERN")
    args = parser.parse_args(argv)

    try:
        check_changed_paths([Path(p) for p in args.changed], args.allowed, args.forbidden)
    except PathPolicyError as exc:
        print(f"ERROR_CODE={exc.code}: {exc}", file=sys.stderr)
        return EXIT_CODES[exc.code]
    print("PATHS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
