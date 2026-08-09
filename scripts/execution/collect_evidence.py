"""命令证据收集与完整性校验（T01）。

record_command 运行命令并把 stdout/stderr 落盘到 output_dir/commands/，
记录 UTC 时间、退出码、路径与 SHA-256；validate_evidence 校验证据完整性，
缺 exit_code 或其它必填字段时拒绝。脚本只收集证据，不替任何一方修改测试结果。

错误码（见 EXIT_CODES）：
- EVIDENCE_MISSING_EXIT_CODE  缺退出码，不能通过
- EVIDENCE_INCOMPLETE         其它必填字段缺失
- EVIDENCE_IO_ERROR           证据文件读取/解析失败
- COMMAND_RUN_ERROR           记录的命令无法启动
- MISSING_MODE                CLI 模式参数缺失
"""

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXIT_CODES = {
    "MISSING_MODE": 30,
    "EVIDENCE_MISSING_EXIT_CODE": 31,
    "EVIDENCE_INCOMPLETE": 32,
    "EVIDENCE_IO_ERROR": 33,
    "COMMAND_RUN_ERROR": 34,
}

REQUIRED_FIELDS = [
    "command",
    "cwd",
    "started_at",
    "finished_at",
    "exit_code",
    "stdout",
    "stderr",
    "stdout_sha256",
    "stderr_sha256",
]


class EvidenceError(Exception):
    """携带具体错误码的证据违约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class CommandEvidence:
    command: list[str]
    cwd: str
    started_at: str
    finished_at: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_command(command: list[str], cwd: Path, output_dir: Path) -> CommandEvidence:
    """运行命令并记录可核验证据。已记录命令自身失败不视为本工具失败。"""
    output_dir = Path(output_dir)
    commands_dir = output_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    tag = _sha256_text(" ".join(command))[:12]
    started_at = datetime.now(UTC).isoformat()
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True)
    except OSError as exc:
        raise EvidenceError("COMMAND_RUN_ERROR", f"无法运行 {command!r}: {exc}") from exc
    finished_at = datetime.now(UTC).isoformat()

    stdout_name = f"{tag}.stdout"
    stderr_name = f"{tag}.stderr"
    (commands_dir / stdout_name).write_text(result.stdout, encoding="utf-8")
    (commands_dir / stderr_name).write_text(result.stderr, encoding="utf-8")

    return CommandEvidence(
        command=list(command),
        cwd=str(cwd),
        started_at=started_at,
        finished_at=finished_at,
        exit_code=result.returncode,
        stdout=str(Path("commands") / stdout_name),
        stderr=str(Path("commands") / stderr_name),
        stdout_sha256=_sha256_text(result.stdout),
        stderr_sha256=_sha256_text(result.stderr),
    )


def validate_evidence(record: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise EvidenceError("EVIDENCE_INCOMPLETE", "证据必须是 JSON 对象")
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    if missing:
        if "exit_code" in missing:
            raise EvidenceError("EVIDENCE_MISSING_EXIT_CODE", f"缺少退出码及其它字段: {missing}")
        raise EvidenceError("EVIDENCE_INCOMPLETE", f"缺少字段: {missing}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记录或校验命令证据。")
    parser.add_argument("--record", action="store_true", help="运行命令并记录证据")
    parser.add_argument("--command", default=None, help="--record 的命令 argv（JSON 数组）")
    parser.add_argument("--cwd", default=None, help="--record 的工作目录")
    parser.add_argument("--output-dir", default=None, help="--record 的证据输出目录")
    parser.add_argument("--validate-json", default=None, help="校验一份证据 JSON 文件")
    args = parser.parse_args(argv)

    if args.validate_json:
        try:
            record = json.loads(Path(args.validate_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR_CODE=EVIDENCE_IO_ERROR: {exc}", file=sys.stderr)
            return EXIT_CODES["EVIDENCE_IO_ERROR"]
        try:
            validate_evidence(record)
        except EvidenceError as exc:
            print(f"ERROR_CODE={exc.code}: {exc}", file=sys.stderr)
            return EXIT_CODES[exc.code]
        print("EVIDENCE_OK")
        return 0

    if args.record:
        if not args.command or not args.cwd or not args.output_dir:
            print("ERROR_CODE=MISSING_MODE: --record 需要 --command、--cwd、--output-dir", file=sys.stderr)
            return EXIT_CODES["MISSING_MODE"]
        try:
            command = json.loads(args.command)
        except json.JSONDecodeError as exc:
            print(f"ERROR_CODE=MISSING_MODE: --command 不是合法 JSON: {exc}", file=sys.stderr)
            return EXIT_CODES["MISSING_MODE"]
        if not isinstance(command, list):
            print("ERROR_CODE=MISSING_MODE: --command 必须是 argv 数组", file=sys.stderr)
            return EXIT_CODES["MISSING_MODE"]
        try:
            evidence = record_command(command, Path(args.cwd), Path(args.output_dir))
        except EvidenceError as exc:
            print(f"ERROR_CODE={exc.code}: {exc}", file=sys.stderr)
            return EXIT_CODES[exc.code]
        print(json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print("ERROR_CODE=MISSING_MODE: 请选择 --record 或 --validate-json", file=sys.stderr)
    return EXIT_CODES["MISSING_MODE"]


if __name__ == "__main__":
    sys.exit(main())
