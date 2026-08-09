"""T01 执行护栏红/绿测试。

四个必需负例：
1. 越界文件被拒绝        -> check_changed_paths  -> CHANGED_PATH_OUT_OF_ALLOWED
2. 锁定测试被修改时拒绝   -> check_changed_paths  -> CHANGED_PATH_FORBIDDEN
3. spec_hash 不匹配被拒绝 -> verify_task_envelope -> SPEC_HASH_MISMATCH
4. 缺退出码不能通过       -> collect_evidence     -> EVIDENCE_MISSING_EXIT_CODE

每个用例同时断言具体错误码字符串与进程非零退出码，不依赖 Python 抛出任意异常。
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "execution"

# 本地信封自洽性测试使用的 40 位十六进制占位 SHA，不代表真实 commit。
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _run_script(script_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = SCRIPTS / script_name
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def _write_envelope(tmp_path: Path, spec_hash: str) -> Path:
    envelope = {
        "task_id": "T01",
        "spec_hash": spec_hash,
        "base_commit": FAKE_SHA,
        "allowed_paths": ["tests/execution/**"],
        "forbidden_paths": ["docs/contracts/**"],
        "required_tests": ["tests/execution/test_execution_guards.py"],
        "required_invariants": [],
        "required_artifacts": [],
        "human_gates": [],
        "stop_conditions": ["越界 diff 立即拒绝", "spec_hash 不匹配立即拒绝"],
    }
    path = tmp_path / "T01.yaml"
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class TestChangedPaths:
    def test_out_of_allowed_rejected(self) -> None:
        result = _run_script(
            "check_changed_paths.py",
            "--changed", "src/evil.py",
            "--allowed", "allowed/**",
            "--forbidden", "forbidden/**",
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "CHANGED_PATH_OUT_OF_ALLOWED" in result.stderr

    def test_allowed_path_passes(self) -> None:
        result = _run_script(
            "check_changed_paths.py",
            "--changed", "allowed/ok.py",
            "--allowed", "allowed/**",
            "--forbidden", "forbidden/**",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PATHS_OK" in result.stdout

    def test_forbidden_locked_test_rejected(self) -> None:
        # 锁定测试即使同时命中 allowed，也必须被 forbidden 拒绝。
        result = _run_script(
            "check_changed_paths.py",
            "--changed", "tests/execution/test_execution_guards.py",
            "--allowed", "tests/execution/**",
            "--forbidden", "tests/execution/test_execution_guards.py",
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "CHANGED_PATH_FORBIDDEN" in result.stderr


class TestEnvelopeVerification:
    def test_spec_hash_mismatch_rejected(self, tmp_path: Path) -> None:
        envelope = _write_envelope(tmp_path, spec_hash="f" * 64)
        result = _run_script(
            "verify_task_envelope.py",
            "--envelope", str(envelope),
            "--expected-spec-hash", "0" * 64,
            "--head-sha", FAKE_SHA,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "SPEC_HASH_MISMATCH" in result.stderr

    def test_valid_envelope_passes(self, tmp_path: Path) -> None:
        # 先由脚本自身算法得到规范 spec_hash，回写后同一信封应验证通过。
        envelope = _write_envelope(tmp_path, spec_hash="f" * 64)
        result = _run_script("verify_task_envelope.py", "--envelope", str(envelope), "--print-spec-hash")
        assert result.returncode == 0, result.stdout + result.stderr
        canonical = result.stdout.strip().splitlines()[-1]
        assert len(canonical) == 64

        data = json.loads(envelope.read_text(encoding="utf-8"))
        data["spec_hash"] = canonical
        envelope.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        result = _run_script(
            "verify_task_envelope.py",
            "--envelope", str(envelope),
            "--expected-spec-hash", canonical,
            "--head-sha", FAKE_SHA,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ENVELOPE_OK" in result.stdout


class TestEvidenceCollection:
    def test_evidence_missing_exit_code_rejected(self, tmp_path: Path) -> None:
        record = {
            "command": ["python", "-c", "print(1)"],
            "cwd": ".",
            "started_at": "2026-08-09T00:00:00+00:00",
            "finished_at": "2026-08-09T00:00:01+00:00",
            "stdout": "commands/0.stdout",
            "stderr": "commands/0.stderr",
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        }
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps(record), encoding="utf-8")
        result = _run_script("collect_evidence.py", "--validate-json", str(evidence))
        assert result.returncode != 0, result.stdout + result.stderr
        assert "EVIDENCE_MISSING_EXIT_CODE" in result.stderr

    def test_complete_evidence_passes(self, tmp_path: Path) -> None:
        record = {
            "command": ["python", "-c", "print(1)"],
            "cwd": ".",
            "started_at": "2026-08-09T00:00:00+00:00",
            "finished_at": "2026-08-09T00:00:01+00:00",
            "exit_code": 0,
            "stdout": "commands/0.stdout",
            "stderr": "commands/0.stderr",
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        }
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps(record), encoding="utf-8")
        result = _run_script("collect_evidence.py", "--validate-json", str(evidence))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "EVIDENCE_OK" in result.stdout

    def test_record_command_writes_hashed_artifacts(self, tmp_path: Path) -> None:
        out = tmp_path / "evidence"
        result = _run_script(
            "collect_evidence.py",
            "--record",
            "--command", json.dumps([sys.executable, "-c", "print(42)"]),
            "--cwd", str(REPO_ROOT),
            "--output-dir", str(out),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (out / "commands").is_dir()
        assert len(list((out / "commands").glob("*.stdout"))) >= 1
        assert '"exit_code": 0' in result.stdout
        assert '"stdout_sha256"' in result.stdout
