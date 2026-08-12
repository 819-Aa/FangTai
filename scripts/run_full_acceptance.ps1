# T23 真实全链路验收（Gate S5/S6）—— 项目所有者授权的新隔离固定数据环境
#
# 授权范围（仅本项目所有者批准）：
#   - Compose project: food_agent_v2_t23
#   - 全新卷：food_agent_v2_t23_mysql_v2_data / _qdrant_v2_data / _redis_v2_data
#   严禁复用/修改/停止/删除现有 H04 及旧系统容器和数据卷。
#
# 必须显式传入 -ConfirmAuthorizedEmptyT23 才允许创建全新隔离环境。
# 遵循 H04 已批准固定构建：锁定 BuildManifest 的 SHA-256/build_id/builder commit，
# 不 data-rebuild；data-verify 后初始化获授权的新空目标。
# 脚本启动并管理绑定同一 T23 环境的 API；live 0 skip 机器可解析报告。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1 -ConfirmAuthorizedEmptyT23

[CmdletBinding()]
param(
    [switch]$ConfirmAuthorizedEmptyT23
)

$ErrorActionPreference = "Stop"

# ---- 隔离资源身份 ----
$T23_PROJECT = "food_agent_v2_t23"
$T23_PREFIX = "food_agent_v2_t23"
$MYSQL_PORT = "33307"
$QDRANT_REST_PORT = "36335"
$QDRANT_GRPC_PORT = "36336"
$REDIS_PORT = "36380"
$API_PORT = "38001"
$API = "http://localhost:$API_PORT"

$T23_VOLUMES = @(
    "${T23_PROJECT}_mysql_v2_data",
    "${T23_PROJECT}_qdrant_v2_data",
    "${T23_PROJECT}_redis_v2_data"
)
$APPROVED_MANIFEST = ".staging/final/build_manifest.json"
$LIVE_REPORT = ".staging/t23_live_report.xml"
$API_LOG = ".staging/t23_api.log"
$API_ERR = ".staging/t23_api.err"

# ---- H04 已批准 BuildManifest 批准值（严格锁定，禁止 builder_version==HEAD） ----
$APPROVED_MANIFEST_SHA256 = "2283ea029aaa120f0f076437b4d622bb8583f872edf70cd8fdd1ffdc0084d296"
$APPROVED_BUILD_ID = "8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f"
$APPROVED_BUILDER = "4eae6acda5b950d5f98ed9841b3653a80b9b241b"

# 脚本管理的进程（finally 中停止；绝不删除卷）
$Script:ApiProc = $null

# ---- 显式授权参数 ----
if (-not $ConfirmAuthorizedEmptyT23) {
    Write-Host "[BLOCKED_T23_EMPTY_ENV] 必须显式传入 -ConfirmAuthorizedEmptyT23 才允许创建全新隔离环境" -ForegroundColor Red
    exit 2
}

function Fail-Blocked($code, $msg) {
    Write-Host "[$code] $msg" -ForegroundColor Red
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

# ---- try/finally：无论成败都记录资源状态（不删除任何卷） ----
try {
    # ---- 前置：工作区 clean ----
    $ws = git status --porcelain
    if (-not [string]::IsNullOrWhiteSpace($ws)) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "工作区存在未提交改动：$($ws.Trim())"
    }

    # ---- 前置：旧资源身份（记录执行前后状态） ----
    Write-Host "旧默认资源状态（执行前）：" -ForegroundColor Cyan
    docker ps -a --filter "name=food_agent_v2_mysql|name=food_agent_v2_qdrant|name=food_agent_v2_redis" --format "{{.Names}} {{.Status}} {{.ID}}" 2>$null
    $legacyRunning = docker ps --filter "name=food_agent_v2_mysql|name=food_agent_v2_qdrant|name=food_agent_v2_redis" --format "{{.Names}}"
    if ($legacyRunning) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "默认旧资源在运行（$legacyRunning），严禁复用"
    }
    $legacyPid = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
    if ($legacyPid) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "遗留 localhost:8001 进程（PID $($legacyPid.OwningProcess)），严禁连接"
    }

    # ---- 前置：T23 容器/卷必须尚不存在（全新环境） ----
    $t23Containers = docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}}"
    if ($t23Containers) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 容器已存在（$t23Containers）；严禁复用/删除"
    }
    foreach ($v in $T23_VOLUMES) {
        $exists = docker volume ls --filter "name=$v" --format "{{.Name}}"
        if ($exists) {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 卷已存在（$v）；严禁复用/删除"
        }
    }

    # ---- 前置：端口未被占用 ----
    foreach ($port in @($MYSQL_PORT, $QDRANT_REST_PORT, $REDIS_PORT, $API_PORT)) {
        $p = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($p) {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 端口 $port 已被占用（PID $($p.OwningProcess)）"
        }
    }

    # ---- 锁定 H04 BuildManifest：严格比较批准值 + git cat-file 校验 builder commit ----
    Assert-Step "锁定并校验 H04 BuildManifest（批准值）" {
        if (-not (Test-Path $APPROVED_MANIFEST)) {
            Fail-Blocked "BLOCKED_T23_DATA_POLICY" "缺少 H04 已批准 BuildManifest：$APPROVED_MANIFEST"
        }
        $sha = (Get-FileHash -Algorithm SHA256 $APPROVED_MANIFEST).Hash.ToLowerInvariant()
        $m = Get-Content $APPROVED_MANIFEST -Raw | ConvertFrom-Json
        $builder = [string]$m.builder_version
        Write-Host "MANIFEST_SHA256: $sha (期望 $APPROVED_MANIFEST_SHA256)"
        Write-Host "MANIFEST_BUILD_ID: $($m.build_id) (期望 $APPROVED_BUILD_ID)"
        Write-Host "MANIFEST_BUILDER: $builder (期望 $APPROVED_BUILDER)"
        if ($sha -ne $APPROVED_MANIFEST_SHA256) {
            Fail-Blocked "BLOCKED_T23_DATA_POLICY" "BuildManifest SHA-256 与批准值不一致"
        }
        if ([string]$m.build_id -ne $APPROVED_BUILD_ID) {
            Fail-Blocked "BLOCKED_T23_DATA_POLICY" "BuildManifest.build_id 与批准值不一致"
        }
        if ($builder -ne $APPROVED_BUILDER) {
            Fail-Blocked "BLOCKED_T23_DATA_POLICY" "BuildManifest.builder_version 与批准值不一致"
        }
        git cat-file -e "$builder^{commit}"
        if ($LASTEXITCODE -ne 0) {
            Fail-Blocked "BLOCKED_T23_DATA_POLICY" "批准 builder commit 不存在：$builder"
        }
        # 校验后以锁定值初始化（不 data-rebuild）
        uv run food-agent-v2 data-verify --manifest $APPROVED_MANIFEST
    }

    # ---- 创建全新隔离 Compose project（仅授权卷） ----
    Assert-Step "docker compose -p $T23_PROJECT up（全新卷，隔离端口）" {
        $env:MYSQL_CONTAINER_NAME = "${T23_PREFIX}_mysql"
        $env:QDRANT_CONTAINER_NAME = "${T23_PREFIX}_qdrant"
        $env:REDIS_CONTAINER_NAME = "${T23_PREFIX}_redis"
        $env:MYSQL_PORT = "$MYSQL_PORT"
        $env:QDRANT_REST_PORT = "$QDRANT_REST_PORT"
        $env:QDRANT_GRPC_PORT = "$QDRANT_GRPC_PORT"
        $env:REDIS_PORT = "$REDIS_PORT"
        docker compose -p $T23_PROJECT up -d mysql qdrant redis
        if ($LASTEXITCODE -ne 0) {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 compose 启动失败（exit=$LASTEXITCODE）"
        }
        docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}} {{.ID}} {{.Status}}"
    }

    # ---- 服务 readiness ----
    Assert-Step "服务 readiness 健康检查" {
        $deadline = (Get-Date).AddSeconds(120)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            $m = docker exec "${T23_PREFIX}_mysql" mysqladmin ping -h localhost 2>$null
            $q = curl.exe -s "http://localhost:$QDRANT_REST_PORT/healthz"
            $r = docker exec "${T23_PREFIX}_redis" redis-cli ping 2>$null
            if ($m -match "alive" -and $q -match "healthz check passed" -and $r -eq "PONG") {
                $ready = $true
                break
            }
            Start-Sleep -Seconds 5
        }
        if (-not $ready) {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "服务未就绪"
        }
    }

    # ---- 初始化获授权新空目标（固定构建，不 rebuild） ----
    Assert-Step "data-initialize --confirm-empty-v2（新空目标）" {
        uv run food-agent-v2 data-initialize --manifest $APPROVED_MANIFEST --confirm-empty-v2
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    # ---- 脚本启动并管理绑定同一 T23 环境的 API ----
    Assert-Step "启动 T23 API（$API）" {
        $env:MYSQL_PORT = "$MYSQL_PORT"
        $env:QDRANT_REST_PORT = "$QDRANT_REST_PORT"
        $env:REDIS_PORT = "$REDIS_PORT"
        $env:RAG_WARMUP_ON_STARTUP = "false"
        $Script:ApiProc = Start-Process -FilePath "uv" -ArgumentList "run", "uvicorn", "food_agent_v2.api_app:app", "--host", "127.0.0.1", "--port", "$API_PORT" -PassThru -NoNewWindow -RedirectStandardOutput "$PWD/$API_LOG" -RedirectStandardError "$PWD/$API_ERR"
        $deadline = (Get-Date).AddSeconds(90)
        $up = $false
        while ((Get-Date) -lt $deadline) {
            $code = curl.exe -s -o /dev/null -w "%{http_code}" "$API/health"
            if ($code -eq "200") { $up = $true; break }
            Start-Sleep -Seconds 2
        }
        if (-not $up) {
            Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 API 未就绪（日志见 $API_ERR）"
        }
        Write-Host "API_PID: $($Script:ApiProc.Id) API: $API"
    }

    # ---- live 0 skip：先收集，再要求精确匹配 ----
    Assert-Step "pytest live（T23 环境，29 live）" {
        $env:T23_API_BASE = "$API"
        uv run pytest tests/test_prompts_live.py tests/e2e -q --junitxml="$PWD/$LIVE_REPORT"
        $liveExit = $LASTEXITCODE
        $collect = uv run pytest tests/test_prompts_live.py tests/e2e --collect-only -q -m live 2>&1 | Select-String "tests collected"
        Write-Host "LIVE_COLLECT: $collect"
        $collected = [int](($collect -split "tests collected")[0].Trim())
        $xml = [xml](Get-Content "$PWD/$LIVE_REPORT")
        $suite = $xml.testsuites.testsuite
        $executed = [int]$suite.tests
        $failed = [int]$suite.failures
        $errors = [int]$suite.errors
        $skipped = [int]$suite.skipped
        $passed = $executed - $failed - $errors - $skipped
        Write-Host "LIVE_REPORT: executed=$executed passed=$passed failed=$failed errors=$errors skipped=$skipped collected=$collected exit=$liveExit"
        if ($liveExit -ne 0 -or $executed -ne $collected -or $passed -ne $collected -or `
            $failed -ne 0 -or $errors -ne 0 -or $skipped -ne 0) {
            Fail-Blocked "NOT_ACCEPTED" "live 未全通过/全执行"
        }
    }

    # ---- 后端回归 + 静态 + 前端 ----
    Assert-Step "pytest -m 'not live'（T23 环境）" {
        uv run pytest -m "not live" -q
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    Assert-Step "ruff check src tests scripts" {
        uv run ruff check src tests scripts
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    Assert-Step "npm test -- --run" {
        Push-Location frontend
        npm test -- --run
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { exit $code }
    }
    Assert-Step "npm run build" {
        Push-Location frontend
        npm run build
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { exit $code }
    }
    Assert-Step "playwright 浏览器跨端验收" {
        Push-Location frontend
        $env:T23_API_BASE = "$API"
        npx playwright test --config playwright.config.ts
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { exit $code }
    }

    Write-Host "`n[ACCEPTANCE] 全链路验收通过（Gate S5/S6）" -ForegroundColor Green
}
finally {
    # 停止脚本启动的 API 与前端服务（绝不删除任何卷）
    if ($Script:ApiProc -and -not $Script:ApiProc.HasExited) {
        Stop-Process -Id $Script:ApiProc.Id -Force -ErrorAction SilentlyContinue
        Write-Host "API stopped (PID $($Script:ApiProc.Id))" -ForegroundColor Yellow
    }
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "5174|playwright" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "`n资源状态（执行后，绝不删除任何卷）：" -ForegroundColor Cyan
    docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}} {{.ID}} {{.Status}}"
    docker volume ls --filter "name=$T23_PROJECT" --format "{{.Name}}"
}
