"""MC-06 acceptance runner static safety contract."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_full_acceptance.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_runner_exposes_mutually_exclusive_existing_and_initialize_modes() -> None:
    text = _text()
    assert "[switch]$UseExistingAuthorizedT23" in text
    assert "[switch]$InitializeAuthorizedEmptyT23" in text
    assert "$UseExistingAuthorizedT23 -eq $InitializeAuthorizedEmptyT23" in text


def test_existing_mode_is_read_only_for_fixed_data_and_docker_resources() -> None:
    text = _text()
    start = text.index("function Assert-ExistingT23Environment")
    end = text.index("function Initialize-T23Environment")
    existing = text[start:end]
    forbidden = (
        "data-rebuild",
        "data-initialize",
        "docker compose",
        "docker volume rm",
        "docker rm",
        "down -v",
    )
    assert all(token not in existing for token in forbidden)


def test_api_gate_uses_readiness_not_liveness() -> None:
    text = _text()
    assert '"$API/ready"' in text
    assert '"$API/health"' not in text
    assert "APPROVED_BUILD_ID" in text


def test_background_api_is_hidden_and_only_owned_pid_is_stopped() -> None:
    text = _text()
    assert "-WindowStyle Hidden" in text
    assert "Get-CimInstance Win32_Process" not in text
    assert "Stop-Process -Id $Script:ApiProc.Id" in text


def test_offline_regression_is_batched_but_requires_exact_total() -> None:
    text = _text()
    assert "function Run-OfflineRegression" in text
    assert '"tests/b1/test_ingredient_identity_rebuild.py::TestFullScale"' in text
    assert '"tests/integration/test_real_qdrant_retrieval.py"' in text
    assert "$executed -ne $collected" in text
    assert "failures -ne 0" in text
    assert "errors -ne 0" in text
    assert "skipped -ne 0" in text
