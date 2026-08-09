"""T02 固定源与构建清单 Schema 测试。

覆盖 data-artifact-contracts.md §2 的 SourceManifest / BuildManifest 契约：
- 固定 8 字段均为 Literal 闭包，错误编码/散列/表头/行数/绝对路径/../ 一律拒绝；
- canonical JSON 散列黄金向量；
- 一字节源文件变化在 CSV 解析前抛 SOURCE_MANIFEST_MISMATCH；
- builder_version 即 Git commit SHA，缺失拒绝。
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from food_agent_v2.b1.schemas import (
    SOURCE_BYTE_SIZE,
    SOURCE_ENCODING,
    SOURCE_HEADERS,
    SOURCE_ID,
    SOURCE_RELATIVE_PATH,
    SOURCE_ROW_COUNT,
    SOURCE_SHA256,
)
from food_agent_v2.contracts.build import (
    ArtifactEntry,
    BuildManifest,
    QualityGateReport,
    SourceManifest,
    SourceManifestMismatch,
    canonical_json_hash,
    source_manifest_hash,
    verify_source_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CSV = REPO_ROOT / "data" / "raw" / "recipes_sample_2000.csv"

# 当前基线 HEAD，作为 builder_version（Git commit SHA）。
GIT_SHA = "e2b949a6e285cfaccc5ee26f8b4185ca8c4ad170"

# 批准固定源的黄金 source_manifest_hash（冻结值，实现后核对）。
GOLDEN_SOURCE_MANIFEST_HASH = "396fed40d22620368e565c89150b9696ffb7f5303d6cecea9d89ab2823a844bb"


def canonical_kwargs(**overrides):
    data = {
        "schema_version": "1.0.0",
        "source_id": SOURCE_ID,
        "relative_path": SOURCE_RELATIVE_PATH,
        "encoding": SOURCE_ENCODING,
        "byte_size": SOURCE_BYTE_SIZE,
        "sha256": SOURCE_SHA256,
        "row_count": SOURCE_ROW_COUNT,
        "headers": list(SOURCE_HEADERS),
    }
    data.update(overrides)
    return data


def canonical_manifest() -> SourceManifest:
    return SourceManifest(**canonical_kwargs())


class TestSourceManifest:
    def test_canonical_manifest_builds(self) -> None:
        manifest = canonical_manifest()
        assert manifest.source_id == SOURCE_ID
        assert manifest.byte_size == SOURCE_BYTE_SIZE
        assert manifest.sha256 == SOURCE_SHA256
        assert manifest.row_count == SOURCE_ROW_COUNT
        assert manifest.headers == SOURCE_HEADERS

    def test_wrong_encoding_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceManifest(**canonical_kwargs(encoding="UTF-8"))

    def test_wrong_sha256_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceManifest(**canonical_kwargs(sha256="0" * 64))

    def test_wrong_row_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceManifest(**canonical_kwargs(row_count=2001))

    def test_wrong_header_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceManifest(**canonical_kwargs(headers=["错误", "列", "列", "列"]))

    def test_absolute_relative_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceManifest(**canonical_kwargs(relative_path="/etc/passwd"))

    def test_parent_dotdot_relative_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceManifest(**canonical_kwargs(relative_path="../escape.csv"))


class TestCanonicalHashing:
    def test_canonical_json_hash_golden_simple(self) -> None:
        # {"b":2,"a":1} 排序键后为 {"a":1,"b":2}，散列值独立手算锁定。
        assert canonical_json_hash({"b": 2, "a": 1}) == (
            "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
        )

    def test_source_manifest_hash_golden(self) -> None:
        computed = source_manifest_hash(canonical_manifest())
        # 黄金值独立重算，防止实现把同一 bug 复制进测试。
        canonical = json.dumps(
            canonical_manifest().model_dump(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert computed == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert computed == GOLDEN_SOURCE_MANIFEST_HASH


class TestVerifySourceFile:
    def test_real_source_passes(self) -> None:
        verify_source_file(SOURCE_CSV, canonical_manifest())  # 不应抛异常

    def test_one_byte_change_fails_before_parse(self, tmp_path: Path) -> None:
        tampered = tmp_path / "recipes_sample_2000.csv"
        tampered.write_bytes(SOURCE_CSV.read_bytes())
        data = bytearray(tampered.read_bytes())
        data[1000] ^= 0x01  # 数据区翻转一字节，不影响 CSV 结构
        tampered.write_bytes(bytes(data))

        with pytest.raises(SourceManifestMismatch) as excinfo:
            verify_source_file(tampered, canonical_manifest())
        assert excinfo.value.code == "SOURCE_MANIFEST_MISMATCH"
        # 必须在任何 CSV 结构解析之前，由散列校验失败。
        assert "sha256" in str(excinfo.value)


class TestArtifactEntry:
    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactEntry(relative_path="/abs/out.jsonl", row_count=2000, sha256="a" * 64)

    def test_parent_dotdot_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactEntry(relative_path="../out.jsonl", row_count=2000, sha256="a" * 64)

    def test_windows_drive_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactEntry(relative_path="C:/out.jsonl", row_count=2000, sha256="a" * 64)


class TestBuildManifest:
    def _manifest(self) -> BuildManifest:
        return BuildManifest(
            build_id=UUID("12345678-1234-5678-1234-567812345678"),
            source_manifest_hash=source_manifest_hash(canonical_manifest()),
            builder_version=GIT_SHA,
            schema_versions={"source.manifest": "1.0.0"},
            artifacts={
                "recipe_source_rows": ArtifactEntry(
                    relative_path="artifacts/recipe_source_rows.jsonl",
                    row_count=2000,
                    sha256="a" * 64,
                )
            },
            quality_gate_report=QualityGateReport(
                relative_path="quality/report.json",
                sha256="b" * 64,
                passed=True,
            ),
            created_at=datetime(2026, 8, 9, tzinfo=UTC),
        )

    def test_missing_builder_version_rejected(self) -> None:
        data = self._manifest().model_dump()
        del data["builder_version"]
        with pytest.raises(ValidationError):
            BuildManifest(**data)

    def test_non_git_sha_builder_version_rejected(self) -> None:
        data = self._manifest().model_dump()
        data["builder_version"] = "not-a-sha"
        with pytest.raises(ValidationError):
            BuildManifest(**data)

    def test_roundtrip_serialization(self) -> None:
        manifest = self._manifest()
        restored = BuildManifest.model_validate_json(manifest.model_dump_json())
        assert restored == manifest
        assert restored.builder_version == GIT_SHA
        assert restored.created_at == manifest.created_at
        assert restored.quality_gate_report.passed is True
