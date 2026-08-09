"""Shared fixtures for fixed-data initialization integration tests."""

import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from food_agent_v2.b1.quality_gates import verify_build_manifest
from food_agent_v2.b1.rebuild import build_fixed_data_staging


@pytest.fixture(scope="session")
def verified_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    staging = tmp_path_factory.mktemp("verified-fixed-build") / "build"
    builder_version = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = build_fixed_data_staging(
        staging,
        build_id=UUID("33333333-3333-3333-3333-333333333333"),
        builder_version=builder_version,
    )
    manifest_path = Path(result["manifest_path"])
    manifest = verify_build_manifest(manifest_path)
    assert manifest.build_id == UUID("33333333-3333-3333-3333-333333333333")
    return manifest_path
