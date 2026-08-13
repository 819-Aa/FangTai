# T23 real full-chain acceptance. Existing mode is read-only for fixed data and
# Docker resources; initialize mode requires a separate explicit authorization.
[CmdletBinding()]
param(
    [switch]$UseExistingAuthorizedT23,
    [switch]$InitializeAuthorizedEmptyT23
)

$ErrorActionPreference = "Stop"

if ($UseExistingAuthorizedT23 -eq $InitializeAuthorizedEmptyT23) {
    Write-Host "[BLOCKED_T23_MODE] choose exactly one T23 mode" -ForegroundColor Red
    exit 2
}

$T23_PROJECT = "food_agent_v2_t23"
$T23_PREFIX = "food_agent_v2_t23"
$MYSQL_PORT = "33307"
$QDRANT_REST_PORT = "36335"
$QDRANT_GRPC_PORT = "36336"
$REDIS_PORT = "36380"
$API_PORT = "38001"
$API = "http://localhost:$API_PORT"
$T23_CONTAINERS = @(
    "${T23_PREFIX}_mysql",
    "${T23_PREFIX}_qdrant",
    "${T23_PREFIX}_redis"
)
$T23_VOLUMES = @(
    "${T23_PROJECT}_mysql_v2_data",
    "${T23_PROJECT}_qdrant_v2_data",
    "${T23_PROJECT}_redis_v2_data"
)
$APPROVED_MANIFEST = ".staging/final/build_manifest.json"
$LIVE_REPORT = ".staging/t23_live_report.xml"
$API_LOG = ".staging/t23_api.log"
$API_ERR = ".staging/t23_api.err"
$APPROVED_MANIFEST_SHA256 = "2283ea029aaa120f0f076437b4d622bb8583f872edf70cd8fdd1ffdc0084d296"
$APPROVED_BUILD_ID = "8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f"
$APPROVED_BUILDER = "4eae6acda5b950d5f98ed9841b3653a80b9b241b"
$Script:ApiProc = $null

function Fail-Blocked($code, $message) {
    Write-Host "[$code] $message" -ForegroundColor Red
    exit 2
}

function Assert-Step($name, $scriptBlock) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $scriptBlock
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $name (exit=$LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[PASS] $name" -ForegroundColor Green
}

function Assert-TrackedWorktreeClean {
    git diff --quiet
    if ($LASTEXITCODE -ne 0) {
        Fail-Blocked "BLOCKED_T23_WORKTREE" "tracked working tree changes exist"
    }
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        Fail-Blocked "BLOCKED_T23_WORKTREE" "staged working tree changes exist"
    }
}

function Assert-ApprovedManifest {
    if (-not (Test-Path -LiteralPath $APPROVED_MANIFEST)) {
        Fail-Blocked "BLOCKED_T23_DATA_POLICY" "approved manifest is missing"
    }
    $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $APPROVED_MANIFEST).Hash.ToLowerInvariant()
    $manifest = Get-Content -LiteralPath $APPROVED_MANIFEST -Raw | ConvertFrom-Json
    if ($sha -ne $APPROVED_MANIFEST_SHA256 -or
        [string]$manifest.build_id -ne $APPROVED_BUILD_ID -or
        [string]$manifest.builder_version -ne $APPROVED_BUILDER) {
        Fail-Blocked "BLOCKED_T23_DATA_POLICY" "approved manifest identity mismatch"
    }
    git cat-file -e "$APPROVED_BUILDER`^{commit}"
    if ($LASTEXITCODE -ne 0) {
        Fail-Blocked "BLOCKED_T23_DATA_POLICY" "approved builder commit is unavailable"
    }
    uv run food-agent-v2 data-verify --manifest $APPROVED_MANIFEST
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Assert-ContainerPort($name, $containerPort, $hostPort) {
    $mapping = docker port $name $containerPort 2>$null
    if ($LASTEXITCODE -ne 0 -or -not ($mapping -match ":$hostPort$")) {
        Fail-Blocked "BLOCKED_T23_RESOURCE_IDENTITY" "$name port $containerPort is not bound to $hostPort"
    }
}

function Assert-ExistingT23Environment {
    $actualContainers = @(docker ps -a --filter "name=^/${T23_PREFIX}_" --format "{{.Names}}")
    if ($LASTEXITCODE -ne 0) {
        Fail-Blocked "BLOCKED_T23_RESOURCE_IDENTITY" "cannot inspect T23 containers"
    }
    $expectedContainers = @($T23_CONTAINERS | Sort-Object)
    $actualContainers = @($actualContainers | Sort-Object)
    if (Compare-Object $expectedContainers $actualContainers) {
        Fail-Blocked "BLOCKED_T23_RESOURCE_IDENTITY" "T23 container set is not exact"
    }
    foreach ($name in $T23_CONTAINERS) {
        $running = docker inspect --format "{{.State.Running}}" $name 2>$null
        $healthy = docker inspect --format "{{.State.Health.Status}}" $name 2>$null
        if ($running -ne "true" -or $healthy -ne "healthy") {
            Fail-Blocked "BLOCKED_T23_RESOURCE_IDENTITY" "$name is not running and healthy"
        }
    }
    $actualVolumes = @(docker volume ls --filter "name=^${T23_PROJECT}_" --format "{{.Name}}" | Sort-Object)
    $expectedVolumes = @($T23_VOLUMES | Sort-Object)
    if (Compare-Object $expectedVolumes $actualVolumes) {
        Fail-Blocked "BLOCKED_T23_RESOURCE_IDENTITY" "T23 volume set is not exact"
    }
    foreach ($index in 0..2) {
        $mounts = docker inspect --format "{{range .Mounts}}{{.Name}} {{end}}" $T23_CONTAINERS[$index]
        if ($mounts -notmatch [regex]::Escape($T23_VOLUMES[$index])) {
            Fail-Blocked "BLOCKED_T23_RESOURCE_IDENTITY" "container/volume identity mismatch"
        }
    }
    Assert-ContainerPort "${T23_PREFIX}_mysql" "3306/tcp" $MYSQL_PORT
    Assert-ContainerPort "${T23_PREFIX}_qdrant" "6333/tcp" $QDRANT_REST_PORT
    Assert-ContainerPort "${T23_PREFIX}_qdrant" "6334/tcp" $QDRANT_GRPC_PORT
    Assert-ContainerPort "${T23_PREFIX}_redis" "6379/tcp" $REDIS_PORT
}

function Initialize-T23Environment {
    $containers = docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}}"
    if ($containers) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 containers already exist"
    }
    foreach ($volume in $T23_VOLUMES) {
        if (docker volume ls --filter "name=$volume" --format "{{.Name}}") {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 volume already exists: $volume"
        }
    }
    foreach ($port in @($MYSQL_PORT, $QDRANT_REST_PORT, $QDRANT_GRPC_PORT, $REDIS_PORT, $API_PORT)) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 port is occupied: $port"
        }
    }
    $env:MYSQL_CONTAINER_NAME = "${T23_PREFIX}_mysql"
    $env:QDRANT_CONTAINER_NAME = "${T23_PREFIX}_qdrant"
    $env:REDIS_CONTAINER_NAME = "${T23_PREFIX}_redis"
    $env:MYSQL_PORT = $MYSQL_PORT
    $env:QDRANT_REST_PORT = $QDRANT_REST_PORT
    $env:QDRANT_GRPC_PORT = $QDRANT_GRPC_PORT
    $env:REDIS_PORT = $REDIS_PORT
    docker compose -p $T23_PROJECT up -d mysql qdrant redis
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 3
        $healthy = @($T23_CONTAINERS | Where-Object {
            (docker inspect --format "{{.State.Health.Status}}" $_ 2>$null) -eq "healthy"
        }).Count -eq 3
    } while (-not $healthy -and (Get-Date) -lt $deadline)
    if (-not $healthy) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "new T23 services are not healthy"
    }
    uv run food-agent-v2 data-initialize --manifest $APPROVED_MANIFEST --confirm-empty-v2
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Start-T23Api {
    if (Get-NetTCPConnection -LocalPort $API_PORT -State Listen -ErrorAction SilentlyContinue) {
        Fail-Blocked "BLOCKED_T23_API_PORT" "T23 API port is already occupied"
    }
    New-Item -ItemType Directory -Path ".staging" -Force | Out-Null
    $env:MYSQL_PORT = $MYSQL_PORT
    $env:QDRANT_REST_PORT = $QDRANT_REST_PORT
    $env:QDRANT_GRPC_PORT = $QDRANT_GRPC_PORT
    $env:REDIS_PORT = $REDIS_PORT
    $env:RAG_WARMUP_ON_STARTUP = "false"
    $Script:ApiProc = Start-Process -FilePath "uv" `
        -ArgumentList "run", "uvicorn", "food_agent_v2.api_app:app", "--host", "127.0.0.1", "--port", $API_PORT `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $PWD $API_LOG) `
        -RedirectStandardError (Join-Path $PWD $API_ERR)
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 2
        $response = curl.exe -s "$API/ready"
        try { $ready = $response | ConvertFrom-Json } catch { $ready = $null }
    } while (($null -eq $ready -or $ready.status -ne "ready") -and (Get-Date) -lt $deadline)
    if ($null -eq $ready -or $ready.status -ne "ready" -or $ready.build_id -ne $APPROVED_BUILD_ID) {
        Fail-Blocked "BLOCKED_T23_READINESS" "T23 API did not prove approved cross-store readiness"
    }
    Write-Host "API_PID=$($Script:ApiProc.Id) BUILD_ID=$($ready.build_id)"
}

function Run-LiveAcceptance {
    $env:T23_API_BASE = $API
    uv run pytest tests/test_prompts_live.py tests/e2e -q --junitxml="$PWD/$LIVE_REPORT"
    $liveExit = $LASTEXITCODE
    $collectText = uv run pytest tests/test_prompts_live.py tests/e2e --collect-only -q 2>&1
    $collectLine = $collectText | Select-String "tests collected" | Select-Object -Last 1
    if (-not $collectLine) { Fail-Blocked "NOT_ACCEPTED" "cannot parse live collection count" }
    $collected = [int](($collectLine -split "tests collected")[0].Trim())
    $xml = [xml](Get-Content -LiteralPath "$PWD/$LIVE_REPORT")
    $suite = $xml.testsuites.testsuite
    $executed = [int]$suite.tests
    $failed = [int]$suite.failures
    $errors = [int]$suite.errors
    $skipped = [int]$suite.skipped
    $passed = $executed - $failed - $errors - $skipped
    Write-Host "LIVE executed=$executed passed=$passed failed=$failed errors=$errors skipped=$skipped collected=$collected exit=$liveExit"
    if ($liveExit -ne 0 -or $executed -ne $collected -or $passed -ne $collected -or
        $failed -ne 0 -or $errors -ne 0 -or $skipped -ne 0) {
        Fail-Blocked "NOT_ACCEPTED" "live suite was not fully executed and passed"
    }
}

function Invoke-OfflineBatch($name, [string[]]$paths) {
    $safeName = $name -replace "[^A-Za-z0-9_-]", "_"
    $reportPath = ".staging/t24_offline_${safeName}.xml"
    uv run pytest @paths -m "not live" -q --junitxml=$reportPath
    $exitCode = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath $reportPath)) {
        Fail-Blocked "NOT_ACCEPTED" "offline batch $name produced no JUnit report"
    }
    $xml = [xml](Get-Content -LiteralPath $reportPath)
    $suite = $xml.testsuites.testsuite
    $tests = [int]$suite.tests
    $failures = [int]$suite.failures
    $errors = [int]$suite.errors
    $skipped = [int]$suite.skipped
    Write-Host "OFFLINE[$name] tests=$tests failures=$failures errors=$errors skipped=$skipped exit=$exitCode"
    if ($exitCode -ne 0 -or $failures -ne 0 -or $errors -ne 0 -or $skipped -ne 0) {
        Fail-Blocked "NOT_ACCEPTED" "offline batch failed: $name"
    }
    return $tests
}

function Run-OfflineRegression {
    # Keep memory-heavy fixed-data builds and real BGE/Qdrant checks in isolated
    # processes. Every collected non-live test remains included exactly once.
    $batches = @(
        @{ Name = "acceptance_application_architecture"; Paths = @("tests/acceptance", "tests/application", "tests/architecture") },
        @{ Name = "b1_identity_core"; Paths = @("tests/b1/test_ingredient_identity_rebuild.py::TestIdentityRebuild") },
        @{ Name = "b1_identity_forms"; Paths = @("tests/b1/test_ingredient_identity_rebuild.py::TestRejectedAndForms") },
        @{ Name = "b1_identity_full"; Paths = @("tests/b1/test_ingredient_identity_rebuild.py::TestFullScale") },
        @{ Name = "b1_rest"; Paths = @("tests/b1", "--ignore=tests/b1/test_ingredient_identity_rebuild.py") },
        @{ Name = "b2_to_b6"; Paths = @("tests/b2", "tests/b3", "tests/b4", "tests/b5", "tests/b6") },
        @{ Name = "c1_c2"; Paths = @("tests/c1", "tests/c2") },
        @{ Name = "c3"; Paths = @("tests/c3") },
        @{ Name = "c4_contracts_d1"; Paths = @("tests/c4", "tests/contracts", "tests/d1") },
        @{ Name = "integration_real_qdrant"; Paths = @("tests/integration/test_real_qdrant_retrieval.py") },
        @{ Name = "integration_rest"; Paths = @("tests/integration", "--ignore=tests/integration/test_real_qdrant_retrieval.py") },
        @{ Name = "root_tests"; Paths = @(
            "tests/test_b1_pipeline.py", "tests/test_b2_b4_health.py",
            "tests/test_d2_answer.py", "tests/test_invariants.py"
        ) },
        @{ Name = "execution"; Paths = @("tests/execution") }
    )
    $executed = 0
    foreach ($batch in $batches) {
        $executed += Invoke-OfflineBatch $batch.Name $batch.Paths
    }
    $collectOutput = uv run pytest --collect-only -q -m "not live" 2>&1
    $collectLine = $collectOutput | Select-String "tests collected" | Select-Object -Last 1
    if (-not $collectLine) { Fail-Blocked "NOT_ACCEPTED" "cannot parse offline collection count" }
    $collected = [int](($collectLine -split "/")[0].Trim())
    Write-Host "OFFLINE_TOTAL executed=$executed collected=$collected"
    if ($executed -ne $collected) {
        Fail-Blocked "NOT_ACCEPTED" "offline batches do not cover the exact collected suite"
    }
}

try {
    Assert-TrackedWorktreeClean
    Assert-Step "approved manifest" { Assert-ApprovedManifest }
    if ($UseExistingAuthorizedT23) {
        Assert-Step "existing T23 resource identity" { Assert-ExistingT23Environment }
    } else {
        Assert-Step "initialize authorized empty T23" { Initialize-T23Environment }
    }
    Assert-Step "start API and prove cross-store readiness" { Start-T23Api }
    Assert-Step "live API/model/e2e suite with zero skips" { Run-LiveAcceptance }
    Assert-Step "offline backend regression" {
        Run-OfflineRegression
    }
    Assert-Step "ruff" {
        uv run ruff check src tests scripts
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    Assert-Step "frontend unit tests" {
        Push-Location frontend
        npm test -- --run
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { exit $code }
    }
    Assert-Step "frontend production build" {
        Push-Location frontend
        npm run build
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { exit $code }
    }
    Assert-Step "Playwright browser acceptance" {
        Push-Location frontend
        $env:T23_API_BASE = $API
        npx playwright test --config playwright.config.ts
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { exit $code }
    }
    Write-Host "`n[ACCEPTANCE] READY_FOR_OWNER_ACCEPTANCE" -ForegroundColor Green
}
finally {
    if ($Script:ApiProc -and -not $Script:ApiProc.HasExited) {
        Stop-Process -Id $Script:ApiProc.Id -Force -ErrorAction SilentlyContinue
        Write-Host "API stopped (PID $($Script:ApiProc.Id))" -ForegroundColor Yellow
    }
    Write-Host "`nT23 resources retained:" -ForegroundColor Cyan
    docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}} {{.ID}} {{.Status}}"
    docker volume ls --filter "name=$T23_PROJECT" --format "{{.Name}}"
}
