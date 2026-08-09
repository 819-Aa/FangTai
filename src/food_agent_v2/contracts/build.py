"""固定源与构建清单契约（T02）。

依据 docs/contracts/data-artifact-contracts.md §2。固定源 8 字段全部
Literal 闭包；builder_version 即 Git commit SHA，缺失不可发布；canonical
JSON 规则为 sort_keys、ensure_ascii=False、分隔符 (",", ":")。

权威顺序：数据契约 > 执行计划 > 实现。本模块是字段的最终可执行权威。
"""

import csv
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

from food_agent_v2.b1.schemas import (
    SOURCE_BYTE_SIZE,
    SOURCE_ENCODING,
    SOURCE_ID,
    SOURCE_RELATIVE_PATH,
    SOURCE_ROW_COUNT,
    SOURCE_SHA256,
)

#: 表头必须精确等于批准顺序。
SourceHeaders = tuple[
    Literal["名称"],
    Literal["食材清单"],
    Literal["烹饪步骤"],
    Literal["label"],
]

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class SourceManifest(BaseModel):
    """固定 2,000 条菜品源的机器可读事实，全部字段 Literal 闭包。"""

    schema_version: Literal["1.0.0"]
    source_id: Literal[SOURCE_ID]
    relative_path: Literal[SOURCE_RELATIVE_PATH]
    encoding: Literal[SOURCE_ENCODING]
    byte_size: Literal[SOURCE_BYTE_SIZE]
    sha256: Literal[SOURCE_SHA256]
    row_count: Literal[SOURCE_ROW_COUNT]
    headers: SourceHeaders


class ArtifactEntry(BaseModel):
    """单个构建 Artifact 的定位与散列。路径必须相对 staging 根目录。"""

    relative_path: str
    row_count: int
    sha256: str

    @field_validator("relative_path")
    @classmethod
    def _relative_path_safe(cls, value: str) -> str:
        return _validate_relative_path(value)


class QualityGateReport(BaseModel):
    """质量门禁报告引用：relative_path、sha256、passed。"""

    relative_path: str
    sha256: str
    passed: bool

    @field_validator("relative_path")
    @classmethod
    def _relative_path_safe(cls, value: str) -> str:
        return _validate_relative_path(value)


class BuildManifest(BaseModel):
    """构建清单：绑定 build_id、源清单散列与 Git commit SHA。"""

    build_id: UUID
    source_manifest_hash: str
    builder_version: str
    schema_versions: dict[str, str]
    artifacts: dict[str, ArtifactEntry]
    quality_gate_report: QualityGateReport
    created_at: datetime

    @field_validator("builder_version")
    @classmethod
    def _builder_version_is_git_sha(cls, value: str) -> str:
        if not _GIT_SHA_RE.match(value):
            raise ValueError("builder_version 必须是 40 位十六进制 Git commit SHA")
        return value


def _validate_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("绝对路径被禁止")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("目录穿越被禁止")
    return value


def canonical_json_hash(obj: Any) -> str:
    """批准 canonical JSON 规则的 SHA-256（sort_keys、无 ASCII 转义、紧凑分隔符）。"""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_manifest_hash(manifest: SourceManifest) -> str:
    """SourceManifest 的规范散列，作为 BuildManifest.source_manifest_hash。"""
    return canonical_json_hash(manifest.model_dump())


class SourceManifestMismatch(Exception):
    """源文件与 SourceManifest 不一致（构建前校验失败）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def verify_source_file(path: Path, manifest: SourceManifest) -> None:
    """在读取任何业务行之前核验源文件与清单逐项一致。

    校验顺序：存在性 -> byte_size -> sha256 -> 编码 -> 表头 -> 行数。
    sha256/byte_size 在 CSV 结构解析之前完成，一字节变化即失败。
    """
    if not path.exists():
        raise SourceManifestMismatch("SOURCE_MANIFEST_MISMATCH", f"源文件不存在: {path}")

    raw = path.read_bytes()
    if len(raw) != manifest.byte_size:
        raise SourceManifestMismatch(
            "SOURCE_MANIFEST_MISMATCH",
            f"byte_size {len(raw)} != {manifest.byte_size}",
        )

    actual_sha = hashlib.sha256(raw).hexdigest().upper()
    if actual_sha != manifest.sha256.upper():
        raise SourceManifestMismatch(
            "SOURCE_MANIFEST_MISMATCH",
            f"sha256 {actual_sha} != {manifest.sha256}",
        )

    try:
        text = raw.decode(manifest.encoding)
    except UnicodeDecodeError as exc:
        raise SourceManifestMismatch(
            "SOURCE_MANIFEST_MISMATCH",
            f"{manifest.encoding} 解码失败",
        ) from exc

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SourceManifestMismatch("SOURCE_MANIFEST_MISMATCH", "CSV 为空")
    if tuple(rows[0]) != tuple(manifest.headers):
        raise SourceManifestMismatch(
            "SOURCE_MANIFEST_MISMATCH",
            f"headers {rows[0]} != {list(manifest.headers)}",
        )
    data_count = len(rows) - 1
    if data_count != manifest.row_count:
        raise SourceManifestMismatch(
            "SOURCE_MANIFEST_MISMATCH",
            f"row_count {data_count} != {manifest.row_count}",
        )
