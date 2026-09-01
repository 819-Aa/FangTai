"""Current H07 acceptance runner behavior."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_full_acceptance.ps1"


def _run_preflight(
    *,
    shell: str = "pwsh",
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(overrides or {})
    return subprocess.run(
        [
            shell,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-PreflightOnly",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_preflight_accepts_current_h07_qwen_runtime() -> None:
    result = _run_preflight()

    assert result.returncode == 0, result.stderr or result.stdout
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary == {
        "mysql": "127.0.0.1:3309/food_agent_v2_h07",
        "qdrant": "127.0.0.1:6339/recipe_retrieval_v2_h07",
        "redis": "127.0.0.1:6382/v2:h07",
        "reasoning_model": "qwen3.8-max",
        "answer_model": "qwen3.8-max",
        "llm_key_present": True,
    }


def test_preflight_rejects_retired_store_configuration() -> None:
    result = _run_preflight(overrides={"MYSQL_PORT": "3307"})

    assert result.returncode == 2
    assert "H07_CONFIG_MISMATCH" in result.stderr


def test_preflight_supports_windows_powershell() -> None:
    result = _run_preflight(shell="powershell")

    assert result.returncode == 0, result.stderr or result.stdout
