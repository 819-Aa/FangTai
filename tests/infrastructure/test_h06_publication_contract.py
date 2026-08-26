from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "publish_h06.ps1"
POWERSHELL = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT_PATH)]
H06_BUILD_ID = "fefd8bd7-dafa-4cc5-be0a-40ca22939392"


@dataclass
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str
    json: dict | None


def write_valid_manifest(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    manifest_dir = repo_root / ".staging" / "h06-nutrition-complete-v3"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "build_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "build_id": H06_BUILD_ID,
                "builder_version": "72f6bc0294910dffe44ae02e5326e76fd7cfbda6",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return repo_root, manifest_path


def make_docker_state(*, include_h06: bool = True, api_port_free: bool = True, h06_health: str = "healthy") -> dict:
    ports: dict[str, dict[str, str]] = {}
    if include_h06:
        ports.update(
            {
                "3306": {"kind": "container", "name": "food_agent_v2_h06_mysql", "health": h06_health},
                "6333": {"kind": "container", "name": "food_agent_v2_h06_qdrant", "health": h06_health},
                "6334": {"kind": "container", "name": "food_agent_v2_h06_qdrant", "health": h06_health},
                "6379": {"kind": "container", "name": "food_agent_v2_h06_redis", "health": h06_health},
            }
        )
    if not api_port_free:
        ports["8000"] = {"kind": "process", "name": "rogue-listener"}
    return {
        "ports": ports,
        "containers": {
            "food_agent_v2_h05_mysql": {
                "running": False,
                "health": "missing",
                "ports": {},
            },
            "food_agent_v2_h05_qdrant": {
                "running": False,
                "health": "missing",
                "ports": {},
            },
            "food_agent_v2_h05_redis": {
                "running": False,
                "health": "missing",
                "ports": {},
            },
            "food_agent_v2_h06_mysql": {
                "running": True,
                "health": h06_health,
                "ports": {"3306/tcp": 3306},
            },
            "food_agent_v2_h06_qdrant": {
                "running": True,
                "health": h06_health,
                "ports": {"6333/tcp": 6333, "6334/tcp": 6334},
            },
            "food_agent_v2_h06_redis": {
                "running": True,
                "health": h06_health,
                "ports": {"6379/tcp": 6379},
            },
        },
    }


def write_docker_state(path: Path, *, include_h06: bool = True, api_port_free: bool = True, h06_health: str = "healthy") -> Path:
    state = make_docker_state(include_h06=include_h06, api_port_free=api_port_free, h06_health=h06_health)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_http_state(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "http://127.0.0.1:8000/health": {
                    "status_code": 200,
                    "json": {"status": "ok", "version": "0.1.0"},
                },
                "http://127.0.0.1:8000/ready": {
                    "status_code": 200,
                    "json": {"status": "ready", "build_id": H06_BUILD_ID},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_docker_sequence(path: Path, states: list[dict]) -> Path:
    path.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_http_sequence(path: Path, responses: dict[str, list[dict]]) -> Path:
    path.write_text(json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def docker_resource_snapshot(docker_state_path: Path) -> dict:
    return json.loads(docker_state_path.read_text(encoding="utf-8"))


def run_h06_script(
    action: str,
    manifest: Path | None,
    *,
    repo_root: Path | None = None,
    dry_run: bool = False,
    docker_state_path: Path | None = None,
    docker_sequence_path: Path | None = None,
    http_state_path: Path | None = None,
    http_sequence_path: Path | None = None,
    start_process_log: Path | None = None,
    compose_log_path: Path | None = None,
    stop_process_log: Path | None = None,
    command_log_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> ScriptResult:
    command = [*POWERSHELL, "-Action", action]
    if manifest is not None:
        command.extend(["-Manifest", str(manifest)])
    if dry_run:
        command.append("-DryRun")
    env = os.environ.copy()
    env["H06_TEST_ROOT"] = str(repo_root or REPO_ROOT)
    if docker_state_path is not None:
        env["H06_TEST_DOCKER_STATE"] = str(docker_state_path)
    if docker_sequence_path is not None:
        env["H06_TEST_DOCKER_SEQUENCE"] = str(docker_sequence_path)
    if http_state_path is not None:
        env["H06_TEST_HTTP_STATE"] = str(http_state_path)
    if http_sequence_path is not None:
        env["H06_TEST_HTTP_SEQUENCE"] = str(http_sequence_path)
    if start_process_log is not None:
        env["H06_TEST_START_PROCESS_LOG"] = str(start_process_log)
    if compose_log_path is not None:
        env["H06_TEST_COMPOSE_LOG"] = str(compose_log_path)
    if stop_process_log is not None:
        env["H06_TEST_STOP_PROCESS_LOG"] = str(stop_process_log)
    if command_log_path is not None:
        env["H06_TEST_COMMAND_LOG"] = str(command_log_path)
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
    return ScriptResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        json=payload,
    )


def test_h06_preflight_dry_run_is_isolated_and_non_mutating(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")

    before = docker_resource_snapshot(docker_state)
    result = run_h06_script(
        "Preflight",
        manifest,
        repo_root=repo_root,
        dry_run=True,
        docker_state_path=docker_state,
    )
    after = docker_resource_snapshot(docker_state)

    assert result.returncode == 0, result.stderr
    assert result.json is not None
    assert result.json["compose_project"] == "food_agent_v2_h06"
    assert result.json["ports"] == {
        "mysql": 3306,
        "qdrant_rest": 6333,
        "qdrant_grpc": 6334,
        "redis": 6379,
        "api": 8000,
    }
    assert result.json["manifest"]["build_id"] == H06_BUILD_ID
    assert after == before


def test_h06_preflight_requires_manifest_argument(tmp_path: Path) -> None:
    repo_root, _manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")

    result = run_h06_script(
        "Preflight",
        None,
        repo_root=repo_root,
        dry_run=True,
        docker_state_path=docker_state,
    )

    assert result.returncode != 0
    assert "Manifest is required" in result.stderr


def test_h06_preflight_treats_missing_h05_inspect_as_expected(tmp_path: Path) -> None:
    """真实 docker inspect 缺失 H05 容器时，Preflight 仍应成功。"""
    repo_root, manifest = write_valid_manifest(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker.cmd").write_text(
        "\n".join(
            [
                "@echo off",
                'if /I "%1"=="inspect" if /I "%2"=="food_agent_v2_h05_mysql" goto missing',
                'if /I "%1"=="inspect" if /I "%2"=="food_agent_v2_h05_qdrant" goto missing',
                'if /I "%1"=="inspect" if /I "%2"=="food_agent_v2_h05_redis" goto missing',
                'if /I "%2"=="food_agent_v2_h06_mysql" echo [{"State":{"Running":true,"Health":{"Status":"healthy"}},"NetworkSettings":{"Ports":{"3306/tcp":[{"HostPort":"3306"}]}}}] & exit /b 0',
                'if /I "%2"=="food_agent_v2_h06_qdrant" echo [{"State":{"Running":true,"Health":{"Status":"healthy"}},"NetworkSettings":{"Ports":{"6333/tcp":[{"HostPort":"6333"}],"6334/tcp":[{"HostPort":"6334"}]}}}] & exit /b 0',
                'if /I "%2"=="food_agent_v2_h06_redis" echo [{"State":{"Running":true,"Health":{"Status":"healthy"}},"NetworkSettings":{"Ports":{"6379/tcp":[{"HostPort":"6379"}]}}}] & exit /b 0',
                "exit /b 0",
                ":missing",
                "echo Error: No such object: %2 1>&2",
                "exit /b 1",
            ]
        ),
        encoding="ascii",
    )

    # Occupy a real target port so Assert-StorePortRestorable must inspect H06.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 3306))
        except OSError as exc:
            pytest.skip(f"standard MySQL port unavailable for isolated listener: {exc}")
        listener.listen(1)
        result = run_h06_script(
            "Preflight",
            manifest,
            repo_root=repo_root,
            dry_run=True,
            extra_env={
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "H06_TEST_API_PORT_FREE": "true",
            },
        )
    finally:
        listener.close()

    assert result.returncode == 0, result.stderr
    assert result.json is not None
    assert all(item["health"] == "missing" for item in result.json["h05"].values())


def test_h06_preflight_rejects_manifest_outside_allowed_staging_root(tmp_path: Path) -> None:
    repo_root, _manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")
    rogue_manifest = tmp_path / "outside" / "build_manifest.json"
    rogue_manifest.parent.mkdir(parents=True)
    rogue_manifest.write_text(json.dumps({"build_id": H06_BUILD_ID}), encoding="utf-8")

    result = run_h06_script(
        "Preflight",
        rogue_manifest,
        repo_root=repo_root,
        dry_run=True,
        docker_state_path=docker_state,
    )

    assert result.returncode != 0
    assert "must stay within" in result.stderr


def test_h06_preflight_fails_closed_for_unexpected_listener(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(
        tmp_path / "docker-state.json",
        api_port_free=False,
    )

    result = run_h06_script(
        "Preflight",
        manifest,
        repo_root=repo_root,
        dry_run=True,
        docker_state_path=docker_state,
    )

    assert result.returncode != 0
    assert "Port 8000 is occupied by unexpected listener" in result.stderr


def test_h06_start_api_uses_hidden_window_and_h06_environment(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")
    http_sequence = write_http_sequence(
        tmp_path / "http-sequence.json",
        {
            "http://127.0.0.1:8000/health": (
                [{"error": "connection-refused"}] * 30
                + [{"status_code": 200, "json": {"status": "ok", "version": "0.1.0"}}]
            ),
            "http://127.0.0.1:8000/ready": (
                [{"status_code": 503, "json": {"error": "SERVICE_NOT_READY"}}] * 30
                + [{"status_code": 200, "json": {"status": "ready", "build_id": H06_BUILD_ID}}]
            ),
        },
    )
    start_process_log = tmp_path / "start-process.json"
    (repo_root / ".env").write_text(
        "\n".join(
            [
                "LLM_API_KEY=test-llm-key",
                "SILICONFLOW_API_KEY=test-sf-key",
                "MYSQL_USER=foodagent",
                "MYSQL_PASSWORD=foodagent_v2",
            ]
        ),
        encoding="utf-8",
    )

    result = run_h06_script(
        "StartApi",
        manifest,
        repo_root=repo_root,
        docker_state_path=docker_state,
        http_sequence_path=http_sequence,
        start_process_log=start_process_log,
    )

    assert result.returncode == 0, result.stderr
    assert result.json is not None
    assert result.json["ready"]["build_id"] == H06_BUILD_ID
    start_payload = json.loads(start_process_log.read_text(encoding="utf-8"))
    assert start_payload["WindowStyle"] == "Hidden"
    assert start_payload["FilePath"] == str(repo_root / ".venv" / "Scripts" / "python.exe")
    assert start_payload["ArgumentList"] == [
        "-m",
        "food_agent_v2.h06_server",
        "--repo-root",
        str(repo_root),
        "--host",
        "127.0.0.1",
        "--api-port",
        "8000",
        "--mysql-port",
        "3306",
        "--qdrant-rest-port",
        "6333",
        "--qdrant-grpc-port",
        "6334",
        "--redis-port",
        "6379",
    ]
    assert start_payload["Environment"]["API_PORT"] == "8000"
    assert start_payload["Environment"]["MYSQL_PORT"] == "3306"
    assert start_payload["Environment"]["QDRANT_REST_PORT"] == "6333"
    assert start_payload["Environment"]["QDRANT_GRPC_PORT"] == "6334"
    assert start_payload["Environment"]["REDIS_PORT"] == "6379"
    assert start_payload["Environment"]["RAG_WARMUP_ON_STARTUP"] == "true"
    assert start_payload["Environment"]["LLM_API_KEY"] == "test-llm-key"
    assert start_payload["Environment"]["SILICONFLOW_API_KEY"] == "test-sf-key"


def test_h06_verify_checks_health_ready_build_id_and_container_health(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")
    http_sequence = write_http_sequence(
        tmp_path / "http-sequence.json",
        {
            "http://127.0.0.1:8000/health": [
                {"error": "connection-refused"},
                {"status_code": 200, "json": {"status": "ok", "version": "0.1.0"}},
            ],
            "http://127.0.0.1:8000/ready": [
                {"status_code": 200, "json": {"status": "ready", "build_id": "wrong-build"}},
                {"status_code": 200, "json": {"status": "ready", "build_id": H06_BUILD_ID}},
            ],
        },
    )

    result = run_h06_script(
        "Verify",
        manifest,
        repo_root=repo_root,
        docker_state_path=docker_state,
        http_sequence_path=http_sequence,
    )

    assert result.returncode == 0, result.stderr
    assert result.json is not None
    assert result.json["health"]["status"] == "ok"
    assert result.json["ready"]["status"] == "ready"
    assert result.json["ready"]["build_id"] == H06_BUILD_ID
    assert result.json["containers"]["food_agent_v2_h06_mysql"]["health"] == "healthy"


def test_h06_start_stores_waits_for_exact_h06_containers_and_uses_repo_root_compose_file(
    tmp_path: Path,
) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json", include_h06=False)
    docker_sequence = write_docker_sequence(
        tmp_path / "docker-sequence.json",
        [make_docker_state(include_h06=True, h06_health="starting")] * 30
        + [make_docker_state(include_h06=True, h06_health="healthy")],
    )
    compose_log = tmp_path / "compose-log.json"

    result = run_h06_script(
        "StartStores",
        manifest,
        repo_root=repo_root,
        docker_state_path=docker_state,
        docker_sequence_path=docker_sequence,
        compose_log_path=compose_log,
    )

    assert result.returncode == 0, result.stderr
    assert result.json is not None
    assert result.json["containers"]["food_agent_v2_h06_mysql"]["health"] == "healthy"
    compose_payload = json.loads(compose_log.read_text(encoding="utf-8"))
    assert compose_payload["WorkingDirectory"] == str(repo_root)
    assert compose_payload["ComposeFile"] == str(repo_root / "docker-compose.yml")


def test_h06_start_stores_times_out_when_h06_never_becomes_healthy(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json", include_h06=False)
    docker_sequence = write_docker_sequence(
        tmp_path / "docker-sequence.json",
        [make_docker_state(include_h06=True, h06_health="starting")] * 4,
    )
    compose_log = tmp_path / "compose-log-timeout.json"

    result = run_h06_script(
        "StartStores",
        manifest,
        repo_root=repo_root,
        docker_state_path=docker_state,
        docker_sequence_path=docker_sequence,
        compose_log_path=compose_log,
        extra_env={"H06_TEST_STORE_TIMEOUT_SECONDS": "6"},
    )

    assert result.returncode != 0
    assert "did not become healthy before timeout" in result.stderr


def test_h06_verify_times_out_when_ready_build_never_matches(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")
    http_sequence = write_http_sequence(
        tmp_path / "http-sequence-timeout.json",
        {
            "http://127.0.0.1:8000/health": [
                {"status_code": 200, "json": {"status": "ok", "version": "0.1.0"}},
            ],
            "http://127.0.0.1:8000/ready": [
                {"status_code": 503, "json": {"error": "SERVICE_NOT_READY"}},
                {"status_code": 200, "json": {"status": "ready", "build_id": "wrong-build"}},
                {"status_code": 200, "json": {"status": "ready", "build_id": "wrong-build"}},
            ],
        },
    )

    result = run_h06_script(
        "Verify",
        manifest,
        repo_root=repo_root,
        docker_state_path=docker_state,
        http_sequence_path=http_sequence,
        extra_env={"H06_TEST_API_TIMEOUT_SECONDS": "6"},
    )

    assert result.returncode != 0
    assert "did not report manifest build ID before timeout" in result.stderr


def test_h06_start_api_stops_started_pid_when_readiness_times_out(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    docker_state = write_docker_state(tmp_path / "docker-state.json")
    http_sequence = write_http_sequence(
        tmp_path / "http-sequence-startapi-timeout.json",
        {
            "http://127.0.0.1:8000/health": [{"error": "connection-refused"}] * 4,
            "http://127.0.0.1:8000/ready": [
                {"status_code": 503, "json": {"error": "SERVICE_NOT_READY"}}
            ] * 4,
        },
    )
    start_process_log = tmp_path / "start-process-timeout.json"
    stop_process_log = tmp_path / "stop-process-timeout.json"

    result = run_h06_script(
        "StartApi",
        manifest,
        repo_root=repo_root,
        docker_state_path=docker_state,
        http_sequence_path=http_sequence,
        start_process_log=start_process_log,
        stop_process_log=stop_process_log,
        extra_env={"H06_TEST_API_TIMEOUT_SECONDS": "6"},
    )

    assert result.returncode != 0
    assert "did not report manifest build ID before timeout" in result.stderr
    stop_payload = json.loads(stop_process_log.read_text(encoding="utf-8"))
    assert stop_payload["Id"] == 42424
    assert stop_payload["Tree"] is True


def test_h06_initialize_runs_precise_data_initialize_command_and_h06_ports(tmp_path: Path) -> None:
    repo_root, manifest = write_valid_manifest(tmp_path)
    command_log = tmp_path / "command-log.json"

    result = run_h06_script(
        "Initialize",
        manifest,
        repo_root=repo_root,
        command_log_path=command_log,
    )

    assert result.returncode == 0, result.stderr
    command_payload = json.loads(command_log.read_text(encoding="utf-8"))
    assert command_payload["FilePath"] == "uv"
    assert command_payload["ArgumentList"] == [
        "run",
        "food-agent-v2",
        "data-initialize",
        "--manifest",
        str(manifest.resolve()),
        "--confirm-empty-v2",
    ]
    assert command_payload["WorkingDirectory"] == str(repo_root)
    assert command_payload["Environment"]["MYSQL_PORT"] == "3306"
    assert command_payload["Environment"]["QDRANT_REST_PORT"] == "6333"
    assert command_payload["Environment"]["QDRANT_GRPC_PORT"] == "6334"
    assert command_payload["Environment"]["REDIS_PORT"] == "6379"
