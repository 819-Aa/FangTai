# T23 真实全链路验收（Gate S5/S6）—— H04 隔离固定数据环境
#
# 规则：
#   - 只允许操作获授权的隔离 Compose project（T23_PROJECT）与其专属容器/卷；绝不复用
#     默认 food_agent_v2_* 或遗留 localhost:8001。
#   - Compose 启动前确认 T23 容器/卷均不存在；存在或无新卷授权 → BLOCKED_T23_EMPTY_ENV。
#   - 遵循 H04 已批准固定构建：不 data-rebuild；data-verify 既有 BuildManifest 后初始化
#     获授权的新空目标；确需重建 → BLOCKED_T23_DATA_POLICY。
#   - 脚本启动并管理绑定同一 T23 环境的 API（非遗留 8001）；等待 API readiness。
#   - live 0 skip：先收集 live 数量，再要求 executed==collected、passed==collected、
#     failed==0、errors==0、skipped==0；否则 NOT_ACCEPTED。
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1

$ErrorActionPreference = "Stop"

# ---- 隔离资源身份 ----
$T23_PROJECT = "food_agent_v2_t23"
$T23_PREFIX = "food_agent_v2_t23"
$MYSQL_PORT = "33307"
$QDRANT_REST_PORT = "36335"
$QDRANT_GRPC_PORT = "36336"
$REDIS_PORT = "36380"
$API_PORT = "38001"          # 隔离 API 端口（绝不使用遗留 8001）
$API = "http://localhost:$API_PORT"

$APPROVED_MANIFEST = ".staging/final/build_manifest.json"  # H04 已批准固定构建

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

# ---- 前置：工作区 clean ----
$ws = git status --porcelain
if (-not [string]::IsNullOrWhiteSpace($ws)) {
    Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "工作区存在未提交改动，拒绝验收：$($ws.Trim())"
}

# ---- 前置：旧资源身份（记录执行前后 ID/状态） ----
Write-Host "旧默认资源状态：" -ForegroundColor Cyan
docker ps -a --filter "name=food_agent_v2_mysql|name=food_agent_v2_qdrant|name=food_agent_v2_redis" --format "{{.Names}} {{.Status}} {{.ID}}" 2>$null
$legacyRunning = docker ps --filter "name=food_agent_v2_mysql|name=food_agent_v2_qdrant|name=food_agent_v2_redis" --format "{{.Names}}"
if ($legacyRunning) {
    Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "默认旧资源在运行（$legacyRunning），拒绝使用旧资源"
}
$legacyPid = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
if ($legacyPid) {
    Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "检测到遗留 localhost:8001 进程（PID $($legacyPid.OwningProcess)），拒绝连接"
}

# ---- 前置：T23 隔离容器/卷必须尚不存在（全新环境） ----
$t23Containers = docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}}"
$t23Volumes = docker volume ls --filter "name=$T23_PROJECT" --format "{{.Name}}"
if ($t23Containers) {
    Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 隔离容器已存在（$t23Containers）；拒绝复用或删除，等待新卷授权"
}
if ($t23Volumes) {
    Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 隔离卷已存在（$t23Volumes）；拒绝复用或删除，等待新卷授权"
}

# ---- 前置：H04 已批准 BuildManifest 必须存在（遵循固定构建，不 data-rebuild） ----
if (-not (Test-Path $APPROVED_MANIFEST)) {
    Fail-Blocked "BLOCKED_T23_DATA_POLICY" "缺少 H04 已批准 BuildManifest：$APPROVED_MANIFEST"
}

# ---- 启动隔离 Compose project（全新卷） ----
Assert-Step "docker compose -p $T23_PROJECT up（隔离端口）" {
    $env:MYSQL_CONTAINER_NAME = "${T23_PREFIX}_mysql"
    $env:QDRANT_CONTAINER_NAME = "${T23_PREFIX}_qdrant"
    $env:REDIS_CONTAINER_NAME = "${T23_PREFIX}_redis"
    $env:MYSQL_PORT = "$MYSQL_PORT"
    $env:QDRANT_REST_PORT = "$QDRANT_REST_PORT"
    $env:QDRANT_GRPC_PORT = "$QDRANT_GRPC_PORT"
    $env:REDIS_PORT = "$REDIS_PORT"
    docker compose -p $T23_PROJECT up -d mysql qdrant redis
    docker ps -a --filter "name=$T23_PREFIX" --format "{{.Names}} {{.ID}} {{.Status}}"
}

# ---- 服务 readiness ----
Assert-Step "服务 readiness 健康检查" {
    $deadline = (Get-Date).AddSeconds(90)
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

# ---- 目标为空 + 初始化获授权新空目标（遵循固定构建，不 rebuild） ----
Assert-Step "data-verify（H04 已批准 BuildManifest）" {
    uv run food-agent-v2 data-verify --manifest $APPROVED_MANIFEST
}
Assert-Step "data-initialize --confirm-empty-v2（新空目标）" {
    # 初始化写入当前 config 指向的 MySQL/Qdrant（T23 端口 env 已导出）
    uv run food-agent-v2 data-initialize --manifest $APPROVED_MANIFEST --confirm-empty-v2
}

# ---- 脚本启动并管理绑定同一 T23 环境的 API ----
Assert-Step "启动 T23 API（$API，非遗留 8001）" {
    $env:MYSQL_PORT = "$MYSQL_PORT"
    $env:QDRANT_REST_PORT = "$QDRANT_REST_PORT"
    $env:REDIS_PORT = "$REDIS_PORT"
    $env:RAG_WARMUP_ON_STARTUP = "false"
    $proc = Start-Process -FilePath "uv" -ArgumentList "run", "uvicorn", "food_agent_v2.api_app:app", "--host", "127.0.0.1", "--port", "$API_PORT" -PassThru -NoNewWindow -RedirectStandardOutput "$PWD/.staging/t23_api.log" -RedirectStandardError "$PWD/.staging/t23_api.err"
    $deadline = (Get-Date).AddSeconds(60)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        $code = curl.exe -s -o /dev/null -w "%{http_code}" "$API/health"
        if ($code -eq "200") { $up = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $up) {
        Fail-Blocked "BLOCKED_T23_EMPTY_ENV" "T23 API 未就绪"
    }
    Write-Host "API_PID: $($proc.Id) API: $API"
}

# ---- live 0 skip：先收集，再要求精确匹配 ----
Assert-Step "pytest live（T23 环境）" {
    $env:T23_API_BASE = "$API"
    uv run pytest tests/test_prompts_live.py tests/e2e -q --junitxml="$PWD/.staging/t23_live_report.xml"
    $liveExit = $LASTEXITCODE
    # 立即保存并检查第一次 pytest 退出码（不被 collect-only 覆盖）
    $collect = uv run pytest tests/test_prompts_live.py tests/e2e --collect-only -q -m live 2>&1 | Select-String "tests collected"
    Write-Host "LIVE_COLLECT: $collect"
    $collected = [int](($collect -split "tests collected")[0].Trim())
    $xml = [xml](Get-Content "$PWD/.staging/t23_live_report.xml")
    $suite = $xml.testsuites.testsuite
    $executed = [int]$suite.tests
    $failed = [int]$suite.failures
    $errors = [int]$suite.errors
    $skipped = [int]$suite.skipped
    $passed = $executed - $failed - $errors - $skipped
    Write-Host "LIVE_REPORT: executed=$executed passed=$passed failed=$failed errors=$errors skipped=$skipped collected=$collected exit=$liveExit"
    if ($liveExit -ne 0 -or $executed -ne $collected -or $passed -ne $collected -or `
        $failed -ne 0 -or $errors -ne 0 -or $skipped -ne 0) {
        Fail-Blocked "NOT_ACCEPTED" "live 未全通过/全执行：executed=$executed collected=$collected passed=$passed failed=$failed errors=$errors skipped=$skipped"
    }
}

# ---- 后端回归 + 静态 + 前端 ----
Assert-Step "pytest -m 'not live'（T23 环境）" {
    uv run pytest -m "not live" -q
}
Assert-Step "ruff check src tests scripts" {
    uv run ruff check src tests scripts
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

Write-Host "`n[ACCEPTANCE] 全链路验收通过（Gate S5/S6）" -ForegroundColor Green
