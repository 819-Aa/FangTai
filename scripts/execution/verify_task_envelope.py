"""不可变任务信封校验（T01）。

校验 TaskEnvelope：结构必须符合 execution/task-envelope.schema.json，
spec_hash 必须等于除自身外全部字段的规范 SHA-256，且 base_commit 必须等于
当前 HEAD。任何违约立即拒绝，返回具体错误码。

规范 spec_hash 算法：
1. 取信封中除 spec_hash 之外的全部字段；
2. json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))；
3. 对 UTF-8 字节做 SHA-256。
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

EXIT_CODES = {
    "ENVELOPE_NOT_FOUND": 10,
    "ENVELOPE_PARSE_ERROR": 11,
    "ENVELOPE_SCHEMA_INVALID": 12,
    "SPEC_HASH_MISMATCH": 13,
    "BASE_COMMIT_MISMATCH": 14,
    "MISSING_ARGS": 15,
}

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "execution" / "task-envelope.schema.json"


class EnvelopeError(Exception):
    """携带具体错误码的信封校验违约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def load_schema() -> dict[str, Any]:
    try:
        data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvelopeError("ENVELOPE_SCHEMA_INVALID", f"无法读取 schema: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError("ENVELOPE_SCHEMA_INVALID", "schema 必须是 JSON 对象")
    return data


def canonical_spec_hash(envelope: dict[str, Any]) -> str:
    payload = {key: value for key, value in envelope.items() if key != "spec_hash"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_envelope(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EnvelopeError("ENVELOPE_NOT_FOUND", str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvelopeError("ENVELOPE_PARSE_ERROR", f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError("ENVELOPE_PARSE_ERROR", f"{path}: 信封必须是 JSON 对象")
    return data


def validate_structure(envelope: dict[str, Any]) -> None:
    schema = load_schema()
    required = schema.get("required", [])
    missing = [field for field in required if field not in envelope]
    if missing:
        raise EnvelopeError("ENVELOPE_SCHEMA_INVALID", f"缺少必填字段: {missing}")

    if schema.get("additionalProperties") is False:
        allowed = set(schema.get("properties", {}))
        extra = [field for field in envelope if field not in allowed]
        if extra:
            raise EnvelopeError("ENVELOPE_SCHEMA_INVALID", f"多余字段: {extra}")

    type_map = {"string": str, "array": list, "object": dict, "integer": int}
    for field, spec in schema.get("properties", {}).items():
        if field not in envelope:
            continue
        expected = type_map.get(spec.get("type"))
        if expected is not None and not isinstance(envelope[field], expected):
            raise EnvelopeError(
                "ENVELOPE_SCHEMA_INVALID",
                f"字段 {field} 必须为 {spec['type']}",
            )


def verify_envelope(path: Path, *, expected_spec_hash: str, head_sha: str) -> None:
    envelope = load_envelope(path)
    validate_structure(envelope)
    canonical = canonical_spec_hash(envelope)
    if envelope["spec_hash"] != canonical:
        raise EnvelopeError("SPEC_HASH_MISMATCH", "信封 spec_hash 与规范内容不一致")
    if envelope["spec_hash"] != expected_spec_hash:
        raise EnvelopeError("SPEC_HASH_MISMATCH", "信封 spec_hash 与期望值不一致")
    if envelope["base_commit"] != head_sha:
        raise EnvelopeError("BASE_COMMIT_MISMATCH", f"base_commit {envelope['base_commit']} != HEAD {head_sha}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验不可变 TaskEnvelope。")
    parser.add_argument("--envelope", required=True, help="信封文件路径（JSON 兼容 YAML）")
    parser.add_argument("--expected-spec-hash", default=None, help="外部控制者提供的期望 spec_hash")
    parser.add_argument("--head-sha", default=None, help="当前 HEAD commit")
    parser.add_argument(
        "--print-spec-hash",
        action="store_true",
        help="打印信封的规范 spec_hash 后退出（供控制者生成信封）",
    )
    args = parser.parse_args(argv)

    envelope = load_envelope(Path(args.envelope))
    validate_structure(envelope)

    if args.print_spec_hash:
        print(canonical_spec_hash(envelope))
        return 0

    if not args.expected_spec_hash or not args.head_sha:
        print("ERROR_CODE=MISSING_ARGS: --expected-spec-hash 与 --head-sha 为必填", file=sys.stderr)
        return EXIT_CODES["MISSING_ARGS"]

    try:
        verify_envelope(
            Path(args.envelope),
            expected_spec_hash=args.expected_spec_hash,
            head_sha=args.head_sha,
        )
    except EnvelopeError as exc:
        print(f"ERROR_CODE={exc.code}: {exc}", file=sys.stderr)
        return EXIT_CODES[exc.code]
    print("ENVELOPE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
